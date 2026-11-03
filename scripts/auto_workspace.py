"""Bounded current-working-content snapshots, never mutate the original project directory.

Git is used only for names/identity, never checkout, filters, status, or patches.
Capabilities is metadata-only; ensure captures bytes after task approval.
"""
from __future__ import annotations
import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import uuid
import unicodedata
from pathlib import Path

import auto_process
from auto_model import AutoError, identifier

MAX_OUTPUT = 4 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PATH_BYTES = 1024
EXCLUDED_DIRS = {'.git', '.ai-collab', '.codex', '.claude', '.aws', '.ssh', '.gemini', '.gnupg'}
EXCLUDED_NAMES = {'credentials', 'credentials.json', 'credentials.yml', 'credentials.yaml',
                  'secrets.json', 'secrets.yml', 'secrets.yaml', 'id_rsa', 'id_ed25519',
                  '.git-credentials', '.npmrc', '.pypirc', '.netrc'}
PLAIN_EXCLUDED_DIRS = {'node_modules', '.venv', 'venv', '__pycache__', '.cache'}
DIRECTORY_EXCLUSION_POLICY = 'Plain directory: no Git executable required. Folders containing .gitignore/.ignore/.gitmodules are blocked rather than silently ignoring exclusions. Control/credential paths and common dependency/cache directories are excluded; .gitignore is not interpreted. Embedded secrets in ordinary documents cannot be identified automatically.'
EXCLUSION_POLICY = 'Git-ignored untracked files; control/credential directories; .env*; credential names; private key extensions; exact broker metadata. Embedded secrets in ordinary source cannot be identified automatically.'

def _git(root, *args, input=None, timeout=15):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP') if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1')
    # Disable executable local helpers even for read queries. User config is
    # otherwise read, so unsupported filters can be detected instead of ignored.
    argv = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
            '-c', 'core.hooksPath=' + os.devnull, '-c', 'protocol.allow=never', '-C', str(root), *args]
    process = None
    chunks, output_size, overflow = [], [0], threading.Event()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        def read_output():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    output_size[0] += len(chunk)
                    if output_size[0] > MAX_OUTPUT:
                        overflow.set(); process.kill(); break
                    chunks.append(chunk)
            except (OSError, ValueError):
                overflow.set()
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        if input is not None:
            process.stdin.write(input); process.stdin.close()
        process.wait(timeout=timeout)
        reader.join(timeout=2)
        if reader.is_alive():
            raise AutoError('git_unavailable', 'Git output stream did not close')
        if overflow.is_set():
            raise AutoError('git_output_limit', 'Git result exceeds limit')
        return process.returncode, b''.join(chunks)
    except (OSError, subprocess.TimeoutExpired):
        raise AutoError('git_unavailable', 'bounded Git operation unavailable') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill(); process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()



def _error(code, message):
    raise AutoError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _name(raw):
    try:
        value = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        encoded = value.encode('utf-8')
    except (UnicodeError, AttributeError):
        _error('unsupported_filename', 'snapshot names must be valid UTF-8')
    parts = value.split('/')
    if (not value or len(encoded) > MAX_PATH_BYTES or value.startswith('/') or
        any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts) or
        any(unicodedata.category(c) == 'Cc' or c in '\\:*?"<>|' for c in value) or
        any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)):
        _error('unsupported_filename', 'snapshot filename is unsafe or unsupported')
    return value


def _omitted(name, metadata=()):
    parts = name.lower().split('/')
    return (name in metadata or any(p in EXCLUDED_DIRS for p in parts) or
            any(p.startswith('.env') or p in EXCLUDED_NAMES or p.endswith(('.pem', '.key', '.p12', '.pfx')) for p in parts))


def _root(root):
    root = Path(root).absolute()
    if auto_process.is_unsafe_link(root) or not root.is_dir():
        _error('unsafe_path', 'canonical project directory required')
    return root


def _safe(root, relative):
    relative = _name(relative)
    cursor = root
    if auto_process.is_unsafe_link(cursor):
        _error('unsafe_path', 'snapshot root alias rejected')
    for part in relative.split('/'):
        cursor /= part
        if auto_process.is_unsafe_link(cursor):
            _error('unsafe_path', 'snapshot aliases are not followed')
    return cursor


def _safe_directory(path):
    path = Path(path).absolute()
    if auto_process.is_unsafe_link(path):
        _error('unsafe_path', 'snapshot service path must be canonical')
    missing = []; cursor = path
    while not cursor.exists():
        missing.append(cursor); cursor = cursor.parent
    if not cursor.is_dir() or auto_process.is_unsafe_link(cursor):
        _error('unsafe_path', 'snapshot parent is invalid')
    for entry in reversed(missing):
        entry.mkdir(mode=0o700)
    return path


def _fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, stat.S_IMODE(st.st_mode))


def _regular(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        _error('unsupported_file_type', 'only regular files can enter a snapshot')
    if st.st_size > MAX_FILE_BYTES:
        _error('snapshot_limit', 'snapshot file exceeds size limit')
    return st


def _read(root, name, expected=None, *, private=False):
    path = _safe(root, name); before = _regular(path)
    if private and before.st_nlink != 1:
        _error('unsafe_workspace', 'isolated content must not have hardlink aliases')
    if expected is not None and _fingerprint(before) != expected:
        _error('project_changed', 'file metadata changed during capture')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (private and opened.st_nlink != 1) or _fingerprint(opened) != _fingerprint(before):
            _error('project_changed', 'file identity changed during capture')
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES or _fingerprint(after) != _fingerprint(before) or _fingerprint(path.lstat()) != _fingerprint(before):
        _error('project_changed', 'file changed during capture')
    return data, bool(before.st_mode & 0o111)


def _index_identity(root):
    code, raw = _git(root, 'rev-parse', '--git-path', 'index')
    if code:
        _error('git_index_unavailable', 'Git index location unavailable')
    path = Path(os.fsdecode(raw).strip())
    if not path.is_absolute(): path = root / path
    path = path.absolute()
    if auto_process.is_unsafe_link(path):
        _error('unsafe_path', 'Git index alias rejected')
    return _fingerprint(path.stat()) if path.exists() else None


def _inventory(root, metadata=()):
    # Check markers without invoking Git: plain folders work with Git absent.
    # A malformed/unreadable repository never silently becomes a plain folder.
    for ancestor in (root, *root.parents):
        marker = ancestor / '.git'
        if os.path.lexists(marker):
            if auto_process.is_unsafe_link(marker):
                _error('unsafe_path', 'Git marker alias rejected')
            return _git_inventory(root, metadata)
    if (root / 'HEAD').exists() and (root / 'objects').is_dir() and (root / 'refs').is_dir():
        _error('not_git', 'bare repository cannot be used as a document folder')
    return _directory_inventory(root, metadata)


def _directory_inventory(root, metadata=()):
    entries, names, omitted, total, candidates = {}, [], [], 0, 0
    stack = [root]
    while stack:
        directory = stack.pop()
        _safe(root, directory.relative_to(root).as_posix()) if directory != root else _root(root)
        try:
            with os.scandir(directory) as children:
                for child in children:
                    candidates += 1
                    if candidates > 32768:
                        _error('snapshot_limit', 'too many source entries')
                    path = Path(directory) / child.name
                    name = path.relative_to(root).as_posix()
                    if _omitted(name, metadata):
                        omitted.append(name); continue
                    _name(name); _safe(root, name)
                    st = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        if child.name.lower() in PLAIN_EXCLUDED_DIRS:
                            omitted.append(name); continue
                        if os.path.lexists(path / '.git'):
                            _error('unsupported_nested_repository', 'nested repositories require a separate registered project')
                        if len(stack) >= MAX_FILES * 2:
                            _error('snapshot_limit', 'too many source directories')
                        stack.append(path)
                        continue
                    if child.name.lower() in ('.gitignore', '.ignore', '.gitmodules'):
                        _error('plain_ignore_rules_unsupported', 'plain folders with ignore rules require explicit review; files are not silently included')
                    st = _regular(path)
                    names.append(name); entries[name] = _fingerprint(st); total += st.st_size
                    if len(entries) > MAX_FILES or total > MAX_TOTAL_BYTES:
                        _error('snapshot_limit', 'snapshot file count or total bytes exceeds limit')
        except OSError:
            _error('snapshot_unreadable', 'project directory could not be completely enumerated')
    return {'source_kind': 'directory', 'head': None, 'index': None,
            'names': sorted(names), 'entries': dict(sorted(entries.items())),
            'omitted': sorted(omitted), 'bytes': total}


def _git_inventory(root, metadata=()):
    code, top = _git(root, 'rev-parse', '--show-toplevel')
    if code or Path(os.fsdecode(top).strip()).resolve() != root:
        _error('not_git', 'registered root must be a Git repository root')
    code, head = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    if code:
        _error('unborn_git_head', 'repository has no committed source identity')
    head = head.decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        _error('git_unavailable', 'invalid Git source identity')
    code, stages = _git(root, 'ls-files', '--stage', '-z')
    if code:
        _error('git_index_unavailable', 'cannot enumerate index')
    if any(v.startswith(b'160000 ') for v in stages.split(b'\0')):
        _error('unsupported_submodule', 'submodule snapshots are not supported')
    code, raw = _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    if code:
        _error('git_index_unavailable', 'cannot enumerate working files')
    names = sorted(set(_name(v) for v in raw.split(b'\0') if v))
    if len(names) > 32768:
        _error('snapshot_limit', 'too many candidate names')
    entries, omitted, total = {}, [], 0
    for name in names:
        if _omitted(name, metadata):
            omitted.append(name); continue
        path = _safe(root, name)
        try: st = _regular(path)
        except FileNotFoundError: continue  # tracked deletion is current content
        entries[name] = _fingerprint(st); total += st.st_size
        if len(entries) > MAX_FILES or total > MAX_TOTAL_BYTES:
            _error('snapshot_limit', 'snapshot file count or total bytes exceeds limit')
    return {'source_kind': 'git', 'head': head, 'index': _index_identity(root), 'names': names,
            'entries': entries, 'omitted': omitted, 'bytes': total}


def _visible_names(names, budget=24576):
    result = []
    for name in names[:128]:
        if len(_json(result + [name])) > budget: break
        result.append(name)
    return result


def capabilities(root, *, metadata_paths=()):
    root = _root(root)
    result = {'kind': 'managed_snapshot', 'supported': False, 'requires_clean': False,
              'reason': None, 'capture_status': 'not_captured', 'capture_timing': 'first_worker_after_approval',
              'exclusion_policy': EXCLUSION_POLICY,
              'limits': {'files': MAX_FILES, 'file_bytes': MAX_FILE_BYTES, 'total_bytes': MAX_TOTAL_BYTES}}
    try:
        inv = _inventory(root, metadata_paths)
    except AutoError as exc:
        result['reason'] = exc.code; return result
    result.update(supported=True, source_kind=inv['source_kind'], exclusion_policy=DIRECTORY_EXCLUSION_POLICY if inv['source_kind'] == 'directory' else EXCLUSION_POLICY, base_commit=inv['head'], file_count=len(inv['entries']),
                  estimated_bytes=inv['bytes'], omitted_count=len(inv['omitted']),
                  omitted_paths=_visible_names(inv['omitted']), omitted_paths_truncated=len(inv['omitted']) != len(_visible_names(inv['omitted'])))
    return result


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def _load_json(path, limit=MAX_OUTPUT):
    if auto_process.is_unsafe_link(path) or not path.is_file() or path.stat().st_nlink != 1:
        _error('unsafe_workspace', 'snapshot metadata is not a private regular file')
    with path.open('rb') as stream: raw = stream.read(limit + 1)
    if len(raw) > limit:
        _error('workspace_conflict', 'snapshot metadata exceeds limit')
    try: return json.loads(raw), raw
    except (ValueError, UnicodeError): _error('workspace_conflict', 'invalid snapshot metadata')


def _scan(root):
    root = _root(root)
    def unreadable(error):
        _error('snapshot_unreadable', 'snapshot directory could not be completely enumerated')
    entries, total, directory_count = {}, 0, 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        directory_count += 1
        if directory_count > MAX_FILES * 2: _error('snapshot_limit', 'too many output directories')
        for name in dirs:
            path = Path(directory) / name
            if auto_process.is_unsafe_link(path):
                _error('unsafe_workspace', 'snapshot output contains directory alias')
        for name in files:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            relative = _name(relative)
            path = _safe(root, relative); st = _regular(path)
            total += st.st_size
            if len(entries) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                _error('snapshot_limit', 'snapshot output exceeds limits')
            data, executable = _read(root, relative, _fingerprint(st), private=True)
            entries[relative] = {'sha256': _hash(data), 'size': len(data), 'executable': executable}
    return dict(sorted(entries.items()))


class WorkspaceManager:
    def __init__(self, home):
        self.home = _safe_directory(home)
        self._lock = threading.RLock()

    def _directory(self, root, project_id, task_id):
        identifier(project_id, 'project_id'); identifier(task_id, 'task_id')
        root = _root(root)
        if root == self.home or root in self.home.parents:
            _error('unsafe_workspace', 'snapshot service directory must be outside the project')
        directory = self.home / project_id / task_id
        for part in (self.home, directory.parent, directory):
            if auto_process.is_unsafe_link(part): _error('unsafe_workspace', 'snapshot directory alias')
        return root, directory

    def _load(self, root, project_id, task_id):
        root, directory = self._directory(root, project_id, task_id)
        if not directory.exists(): return None
        value, _ = _load_json(directory / 'workspace.json', 65536)
        if not isinstance(value, dict):
            _error('workspace_conflict', 'snapshot descriptor must be an object')
        if (not isinstance(value.get('snapshot_id'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['snapshot_id']) or
            value.get('diff_base') != value['snapshot_id'] or
            not ((value.get('source_kind', 'git') == 'git' and isinstance(value.get('base_commit'), str) and
                  re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['base_commit'])) or
                 (value.get('source_kind') == 'directory' and value.get('base_commit') is None)) or
            value.get('requires_clean') is not False or value.get('supported') is not True or
            value.get('capture_status') != 'captured' or type(value.get('file_count')) is not int):
            _error('workspace_conflict', 'snapshot descriptor scalars are invalid')
        if (value.get('kind') != 'managed_snapshot' or value.get('project_root') != str(root) or
            value.get('project_id') != project_id or value.get('task_id') != task_id):
            _error('workspace_conflict', 'snapshot binding differs; old workspaces are not adopted')
        entries, raw = _load_json(directory / 'baseline.json')
        if not isinstance(entries, dict) or len(entries) > MAX_FILES or _hash(raw) != value.get('snapshot_id'):
            _error('workspace_conflict', 'snapshot baseline manifest differs')
        total = 0
        if value['file_count'] != len(entries):
            _error('workspace_conflict', 'snapshot file count differs')
        for name, entry in entries.items():
            _name(name)
            if (not isinstance(entry, dict) or set(entry) != {'sha256', 'size', 'executable'} or
                not isinstance(entry.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                type(entry.get('size')) is not int or not 0 <= entry['size'] <= MAX_FILE_BYTES or
                type(entry.get('executable')) is not bool):
                _error('workspace_conflict', 'invalid baseline entry')
            total += entry['size']
        if total > MAX_TOTAL_BYTES:
            _error('workspace_conflict', 'baseline exceeds total size limit')
        for child in ('baseline', 'checkout'):
            path = directory / child
            if auto_process.is_unsafe_link(path) or not path.is_dir():
                _error('unsafe_workspace', 'snapshot content directory missing or aliased')
        return value, entries, directory

    def describe(self, root, project_id, task_id):
        loaded = self._load(root, project_id, task_id)
        if loaded is None: return None
        value, _, directory = loaded
        return {**value, 'path': str(directory / 'checkout'),
                'application': {'status': 'pending', 'automatic': False, 'diff_base': value['snapshot_id']}}

    def ensure(self, root, project_id, task_id, *, metadata_paths=()):
        root, directory = self._directory(root, project_id, task_id)
        parent = _safe_directory(directory.parent)
        lock = auto_process.AdvisoryLock(parent / ('.' + task_id + '.lock'))
        with self._lock:
            lock.acquire()
            try:
                loaded = self._load(root, project_id, task_id)
                if loaded is not None:
                    _, entries, existing = loaded
                    if _scan(existing / 'baseline') != entries:
                        _error('workspace_conflict', 'immutable snapshot baseline changed')
                    _scan(existing / 'checkout')  # Reject persisted aliases before another provider gets cwd.
                    return self.describe(root, project_id, task_id)
                before = _inventory(root, metadata_paths)
                stage = parent / ('.' + task_id + '-capture-' + uuid.uuid4().hex)
                stage.mkdir(mode=0o700)
                (stage / 'baseline').mkdir(mode=0o700); (stage / 'checkout').mkdir(mode=0o700)
                entries = {}
                for name, fingerprint in before['entries'].items():
                    data, executable = _read(root, name, fingerprint)
                    entries[name] = {'sha256': _hash(data), 'size': len(data), 'executable': executable}
                    _write(stage / 'baseline' / name, data, 0o500 if executable else 0o400)
                    _write(stage / 'checkout' / name, data, 0o700 if executable else 0o600)
                # Two matching metadata/name passes plus a complete source hash pass.
                if _inventory(root, metadata_paths) != before:
                    _error('project_changed', 'source names/index/HEAD/metadata changed during capture')
                for name, fingerprint in before['entries'].items():
                    data, executable = _read(root, name, fingerprint)
                    if _hash(data) != entries[name]['sha256'] or executable != entries[name]['executable']:
                        _error('project_changed', 'source content changed during capture')
                if _inventory(root, metadata_paths) != before:
                    _error('project_changed', 'source changed before publication')
                raw = _json(entries)
                if len(raw) > MAX_OUTPUT: _error('snapshot_limit', 'snapshot manifest exceeds limit')
                snapshot_id = _hash(raw)
                value = {'kind': 'managed_snapshot', 'project_id': project_id, 'task_id': task_id,
                         'project_root': str(root), 'source_kind': before['source_kind'], 'base_commit': before['head'], 'snapshot_id': snapshot_id,
                         'diff_base': snapshot_id, 'supported': True, 'requires_clean': False,
                         'capture_status': 'captured', 'reason': None,
                         'exclusion_policy': DIRECTORY_EXCLUSION_POLICY if before['source_kind'] == 'directory' else EXCLUSION_POLICY,
                         'file_count': len(entries), 'omitted_count': len(before['omitted']),
                         'omitted_paths': _visible_names(before['omitted']), 'omitted_paths_truncated': len(before['omitted']) != len(_visible_names(before['omitted']))}
                _write(stage / 'baseline.json', raw, 0o400)
                encoded = _json(value)
                if len(encoded) > 65536: _error('snapshot_limit', 'snapshot descriptor exceeds limit')
                _write(stage / 'workspace.json', encoded, 0o400)
                if directory.exists(): _error('workspace_conflict', 'snapshot target appeared during capture')
                os.rename(stage, directory)  # one complete directory, under owned advisory lock
                return self.describe(root, project_id, task_id)
            finally:
                lock.close()

    def diff(self, workspace):
        loaded = self._load(workspace['project_root'], workspace['project_id'], workspace['task_id'])
        if loaded is None: _error('workspace_missing', 'snapshot missing')
        value, baseline, directory = loaded
        if workspace.get('snapshot_id') != value['snapshot_id'] or Path(workspace['path']) != directory / 'checkout':
            _error('workspace_conflict', 'snapshot request binding differs')
        if _scan(directory / 'baseline') != baseline:
            _error('workspace_conflict', 'immutable baseline changed')
        current = _scan(directory / 'checkout')
        changes, patches, patch_bytes, patch_truncated = [], [], 0, False
        for name in sorted(set(baseline) | set(current)):
            old, new = baseline.get(name), current.get(name)
            if old == new: continue
            change = {'path': name, 'kind': 'added' if old is None else 'deleted' if new is None else 'modified',
                      'before': old, 'after': new}
            changes.append(change)
            old_data = _read(directory / 'baseline', name, private=True)[0] if old else b''
            new_data = _read(directory / 'checkout', name, private=True)[0] if new else b''
            if _hash(old_data) != (old or {}).get('sha256', _hash(b'')) or _hash(new_data) != (new or {}).get('sha256', _hash(b'')):
                _error('project_changed', 'snapshot output changed while generating diff')
            try:
                if b'\0' in old_data + new_data or len(old_data) + len(new_data) > 256 * 1024:
                    raise UnicodeError()
                def lines(data):
                    parts = data.decode('utf-8').split('\n')
                    return [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
                fragments = difflib.unified_diff(lines(old_data), lines(new_data),
                                                fromfile='baseline/' + name, tofile='checkout/' + name)
                patch = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                                for line in fragments)
                encoded = len(patch.encode('utf-8'))
                if patch_bytes + encoded <= 128 * 1024:
                    patches.append(patch); patch_bytes += encoded
                else: patch_truncated = True
            except UnicodeError:
                change['binary_or_large'] = True
        if _scan(directory / 'checkout') != current:
            _error('project_changed', 'output changed before export publication')
        complete = {'format': 'snapshot_changes_v1', 'diff_base': value['snapshot_id'],
                    'baseline_manifest': str(directory / 'baseline.json'),
                    'project_root': value['project_root'], 'source_kind': value.get('source_kind', 'git'), 'base_commit': value['base_commit'],
                    'changes': changes, 'application': {'status': 'pending', 'automatic': False},
                    'apply_rule': 'Explicit human action only. Before any write, verify original file hash/mode or expected absence against every before entry; abort on any conflict. Never overwrite unrelated later edits.'}
        raw = _json(complete)
        if len(raw) > MAX_OUTPUT: _error('snapshot_limit', 'diff manifest exceeds limit')
        exports = _safe_directory(directory / 'diffs')
        bundle = exports / _hash(raw)
        lock = auto_process.AdvisoryLock(directory / '.exports.lock')
        lock.acquire()
        try:
            if bundle.exists():
                if auto_process.is_unsafe_link(bundle) or _load_json(bundle / 'manifest.json')[1] != raw:
                    _error('workspace_conflict', 'retained export differs')
                expected = {change['path']: change['after'] for change in changes if change['after'] is not None}
                if _scan(bundle / 'after') != expected:
                    _error('workspace_conflict', 'retained export bytes differ')
            else:
                stage = directory / ('.export-' + uuid.uuid4().hex)
                stage.mkdir(mode=0o700); (stage / 'after').mkdir(mode=0o700)
                for change in changes:
                    if change['after'] is None: continue
                    data, executable = _read(directory / 'checkout', change['path'], private=True)
                    if _hash(data) != change['after']['sha256'] or executable != change['after']['executable']:
                        _error('project_changed', 'output changed during export copy')
                    _write(stage / 'after' / change['path'], data, 0o500 if executable else 0o400)
                if _scan(directory / 'checkout') != current:
                    _error('project_changed', 'output changed before export publication')
                _write(stage / 'manifest.json', raw, 0o400)
                os.rename(stage, bundle)
        finally:
            lock.close()
        ref = bundle / 'manifest.json'
        shown, names = [], []
        for change in changes:
            if len(_json(shown + [change])) > 192 * 1024: break
            shown.append(change)
        for change in changes:
            if change['kind'] == 'added' and len(_json(names + [change['path']])) <= 64 * 1024:
                names.append(change['path'])
        return {'status': 'pending', 'automatic': False, 'source_kind': value.get('source_kind', 'git'), 'base_commit': value['base_commit'],
                'diff_base': value['snapshot_id'], 'tracked_patch': ''.join(patches), 'patch_truncated': patch_truncated,
                'patch_sha256': _hash(''.join(patches).encode('utf-8')), 'untracked_files': names,
                'untracked_files_truncated': len(names) != sum(c['kind'] == 'added' for c in changes),
                'changes': shown, 'changes_truncated': len(shown) != len(changes),
                'change_count': len(changes), 'retained_diff': str(ref),
                'artifact_directory': str(bundle), 'export_format': 'snapshot_changes_v1'}
