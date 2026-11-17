"""Bound manual-session feedback through the existing local HTTP gateway."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid

import auto_api
import client_presence
import collab
import manual_ingress
import passive_feedback


def _target(binding, thread_id):
    if binding['thread_id'] != thread_id or binding['vendor'] != 'codex':
        return False
    try:
        return str(uuid.UUID(binding['session_id'])) == binding['session_id']
    except ValueError:
        return False


def targets(root, thread_id):
    manifest = collab.load_manifest(root, thread_id)
    path = manual_ingress.storage_path(root)
    with_roster = []
    clients = {}
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            clients = client_presence.fresh_clients(db, manifest['participants'])
            for (binding_id,) in db.execute('SELECT id FROM bindings ORDER BY id'):
                try:
                    b = manual_ingress.binding_from(db, binding_id, root)
                except ValueError:
                    continue
                if _target(b, thread_id):
                    with_roster.append({k: b[k] for k in ('binding_id', 'participant', 'vendor', 'revision')})
    direct = {t['participant'] for t in with_roster}
    with_roster.extend({'participant': name, 'vendor': 'listener'}
                       for name in manifest['participants'] if name not in direct)
    for target in with_roster:
        target.update(clients.get(target['participant'], {}))
    return {'targets': with_roster}


def _queue_codex(executable, session_id, message):
    try:
        result = subprocess.run(
            [str(Path(executable).resolve()), 'queue', '--thread=' + session_id, '--message=' + message],
            cwd=Path.home(), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    return 'queued' if result.returncode == 0 else 'unknown'


def notify_published(root, signal):
    """Best-effort reminder only; the signal remains the source of truth."""
    if not manual_ingress.storage_path(root).is_file():
        return None
    thread, sender, recipient = signal['thread_id'], signal['from'], signal['to']
    identity = (thread, sender, recipient, signal['message_id'])
    with manual_ingress.database(root) as db:
        clients = client_presence.fresh_clients(db, (sender, recipient))
        if any(clients.get(name, {}).get('client') != 'codex' for name in (sender, recipient)):
            return None
        matches = []
        for (binding_id,) in db.execute('SELECT id FROM bindings ORDER BY id'):
            try:
                binding = manual_ingress.binding_from(db, binding_id, root)
            except ValueError:
                continue
            if binding['participant'] == recipient and _target(binding, thread):
                matches.append(binding)
        if len(matches) != 1:
            return 'no_unique_binding'
        executable = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
        if not executable:
            return 'cli_unavailable'
        db.execute("""CREATE TABLE IF NOT EXISTS codex_publish_notices (
            thread_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            message_id TEXT NOT NULL, PRIMARY KEY(thread_id, sender, recipient, message_id))""")
        claim = db.execute('INSERT OR IGNORE INTO codex_publish_notices VALUES (?, ?, ?, ?)', identity)
        if claim.rowcount != 1:
            return 'already_attempted'
    # Once claimed, an ambiguous CLI result cannot safely be retried.
    message = (f"有新的协作消息，请查收。线程 {thread}，第 {signal['round']} 轮，"
               f"message_id {signal['message_id']}，来自 {sender}。"
               "请在关联项目中以自己的协作者身份运行 collab.py inspect，核对后读取正式协作记录。")
    return _queue_codex(executable, matches[0]['session_id'], message)


def send(root, thread_id, body, key):
    message = body['body']
    if '\x00' in message or len(message.encode('utf8')) > 4000:
        raise auto_api.ApiError('feedback_size', '反馈不得超过 4000 字节且不能含 NUL', 422)
    if 'participant' in body:
        try:
            passive_feedback.enqueue(root, thread_id, body['participant'], message, key)
        except passive_feedback.FeedbackConflict as exc:
            raise auto_api.ApiError('feedback_recipient', str(exc), 409) from None
        return {'status': 'inbox'}
    digest = hashlib.sha256(json.dumps([thread_id, body], sort_keys=True).encode()).hexdigest()
    if not manual_ingress.storage_path(root).is_file():
        raise auto_api.ApiError('feedback_binding', '当前线程没有可用的会话绑定', 409)
    with manual_ingress.database(root) as db:
        db.execute('CREATE TABLE IF NOT EXISTS session_feedback (key TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL)')
        old = db.execute('SELECT digest, status FROM session_feedback WHERE key=?', (key,)).fetchone()
        if old:
            if old[0] != digest:
                raise auto_api.ApiError('idempotency_conflict', '同一请求编号不能发送另一条反馈', 409)
            return {'status': old[1]}
        try:
            binding = manual_ingress.binding_from(db, body['binding_id'], root)
        except ValueError:
            raise auto_api.ApiError('feedback_binding', '会话绑定已失效，请重新选择', 409) from None
        if not _target(binding, thread_id) or binding['revision'] != body['expected_revision']:
            raise auto_api.ApiError('feedback_binding', '会话不属于当前线程，或绑定已变更', 409)
        executable = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
        if not executable:
            raise auto_api.ApiError('feedback_cli', '服务进程找不到本机 Codex CLI', 503)
        # Persist the claim before doing anything that could deliver the message.
        db.execute('INSERT INTO session_feedback VALUES (?, ?, ?)', (key, digest, 'unknown'))
    if _queue_codex(executable, binding['session_id'], message) != 'queued':
        return {'status': 'unknown'}
    with manual_ingress.database(root) as db:
        db.execute("UPDATE session_feedback SET status='queued' WHERE key=?", (key,))
    return {'status': 'queued'}
