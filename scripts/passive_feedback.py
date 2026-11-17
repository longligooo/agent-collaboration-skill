"""Local, participant-scoped WebUI feedback; never a collaboration wake signal."""
from contextlib import closing
import hashlib
import json
import sqlite3
import uuid

import collab
import manual_ingress


class FeedbackConflict(ValueError):
    pass


def enqueue(root, thread_id, participant, message, key):
    manifest = collab.load_manifest(root, thread_id)
    if participant not in manifest["participants"]:
        raise FeedbackConflict("收件人不属于当前线程")
    generation = manifest["created_at"]
    digest = hashlib.sha256(json.dumps([thread_id, generation, participant, message]).encode()).hexdigest()
    with manual_ingress.database(root, create=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS passive_feedback (
            message_id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
            thread_id TEXT NOT NULL, generation TEXT NOT NULL, participant TEXT NOT NULL,
            body TEXT, created_at TEXT NOT NULL)""")
        db.execute("""CREATE INDEX IF NOT EXISTS passive_feedback_waiting
            ON passive_feedback(participant, thread_id, created_at) WHERE body IS NOT NULL""")
        old = db.execute("SELECT digest FROM passive_feedback WHERE key=?", (key,)).fetchone()
        if old:
            if old[0] != digest:
                raise FeedbackConflict("同一请求编号不能发送另一条反馈")
            return
        db.execute("INSERT INTO passive_feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (str(uuid.uuid4()), key, digest, thread_id, generation, participant, message, collab.now_iso()))


def pending(root, participant, thread_id=None):
    path = manual_ingress.storage_path(root)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passive_feedback'").fetchone()
        if not table:
            return []
        sql = """SELECT thread_id, generation, message_id, body, created_at
            FROM passive_feedback WHERE participant=? AND body IS NOT NULL"""
        params = [participant]
        if thread_id is not None:
            sql += " AND thread_id=?"
            params.append(thread_id)
        rows = db.execute(sql + " ORDER BY created_at, message_id", params)
        manifests = {}
        items = []
        for thread, generation, message_id, body, created in rows:
            if thread not in manifests:
                try:
                    manifests[thread] = collab.load_manifest(root, thread)
                except collab.CollaborationError:
                    manifests[thread] = None
            m = manifests[thread]
            if m and m["created_at"] == generation and participant in m["participants"]:
                items.append({"decision": "FEEDBACK", "thread_id": thread,
                              "message_id": message_id, "from": "WebUI",
                              "requested_action": "READ_FEEDBACK", "body": body,
                              "created_at": created})
                if len(items) == 20:
                    break
        return items


def acknowledge(root, participant, thread_id, message_id):
    manifest = collab.load_manifest(root, thread_id)
    if participant not in manifest["participants"]:
        return False
    if not manual_ingress.storage_path(root).is_file():
        return False
    with manual_ingress.database(root) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passive_feedback'").fetchone():
            return False
        result = db.execute("""UPDATE passive_feedback SET body=NULL
            WHERE participant=? AND thread_id=? AND generation=? AND message_id=?""",
            (participant, thread_id, manifest["created_at"], message_id))
        return result.rowcount == 1
