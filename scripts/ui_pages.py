"""Page-scoped reads. The metadata index never evaluates source code or acceptance.

Global counts/search/order require a lightweight catalog. On a cold service this
reads manifests and bounded turn headers; later requests stat those records and
only re-read changed headers. Full histories/task projections are detail-only.
"""
from __future__ import annotations
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
import collab
import read_budget
import ui_read_model

PAGE_SIZE = 20
INDEX_LIMIT = 10000
INDEX_BYTES = 32 * 1024 * 1024
_index_bytes = 0
HEADER_LIMIT = 65536
_INDEX = OrderedDict()
_LOCK = threading.Lock()


def _header(path):
    # Do not load potentially megabyte-sized turn bodies just to sort a list.
    with path.open('r', encoding='utf-8') as handle:
        chunks = []
        size = 0
        while size < HEADER_LIMIT:
            chunk = handle.readline(min(4096, HEADER_LIMIT - size))
            if not chunk: break
            chunks.append(chunk)
            size += len(chunk)
            if collab.META_END in chunk or collab.META_END in ''.join(chunks[-2:]): break
        prefix = ''.join(chunks)
    if not prefix.startswith(collab.META_START):
        raise collab.CollaborationError('invalid turn metadata')
    end = prefix.find(collab.META_END, len(collab.META_START))
    if end < 0:
        raise collab.CollaborationError('turn metadata exceeds header limit')
    value = json.loads(prefix[len(collab.META_START):end])
    if not isinstance(value, dict):
        raise collab.CollaborationError('invalid turn metadata')
    return value


def _files(root, thread_id):
    directory = collab.resolve_repo_path(root, f'.ai-collab/threads/{thread_id}', must_exist=True, label='thread')
    files = [directory / 'thread.json']
    turns = collab.resolve_repo_path(root, f'.ai-collab/threads/{thread_id}/turns', label='turns')
    if turns.is_dir():
        files.extend(sorted(turns.glob('round-*.md')))
    for relative in (f'.ai-collab/threads/{thread_id}/human-events', f'.ai-collab/local/human-events/{thread_id}'):
        directory = collab.resolve_repo_path(root, relative, label='human event directory')
        if directory.is_dir():
            files.extend(sorted(directory.glob('*.json')))
    signature = []
    safe = []
    for path in files:
        read_budget.check()
        path = collab.resolve_repo_path(root, path.relative_to(root).as_posix(), must_exist=True, label='index input')
        stat = path.stat()
        signature.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        safe.append(path)
    return tuple(signature), safe


def index_record(root, thread_id, project):
    """Return metadata only, with explicit human identity validation."""
    collab.validate_id(thread_id, 'thread_id')
    global _index_bytes
    signature, files = _files(root, thread_id)
    key = (str(root), thread_id)
    config = json.dumps(project.get('identity_bindings'), sort_keys=True)
    signature = (config, signature)
    with _LOCK:
        cached = _INDEX.get(key)
        if cached and cached[0] == signature:
            _INDEX.move_to_end(key)
            return copy.deepcopy(cached[1])
    manifest = collab.load_manifest(root, thread_id)
    bindings = project.get('identity_bindings') or {}
    record = dict(project_id=project['id'], project_name=project['name'], project_root=str(root),
                  thread_id=thread_id, topic=manifest['topic'], objective=manifest['objective'],
                  participants=list(manifest['participants']), coordinator=collab.designated_coordinator(manifest),
                  started_at=manifest.get('created_at') or '', updated_at='', last_activity_at='',
                  status='NONE', terminal=False, round=0, turns=[], signals=[], coding_task=None)
    for path in files:
        read_budget.check()
        if path.suffix != '.md':
            continue
        try:
            meta = _header(path)
            record['turns'].append({k: meta.get(k) for k in ('round', 'author', 'role', 'kind', 'status', 'requested_action', 'created_at')} | {'document': path.relative_to(root).as_posix()})
        except (OSError, ValueError, collab.CollaborationError) as exc:
            record.setdefault('index_errors', []).append(str(exc))
    record['turns'].sort(key=lambda item: (not isinstance(item.get('round'), int), item.get('round') if isinstance(item.get('round'), int) else 0))
    if record['turns']:
        latest = record['turns'][-1]
        record.update(status=latest.get('status') or 'NONE', round=latest.get('round') or 0,
                      updated_at=latest.get('created_at') or '')
        record['terminal'] = record['status'] in collab.TERMINAL_STATUSES
    import human_events
    # Human provenance validation is required; retain only identities/timestamps,
    # never event bodies in the shared index or unrelated list responses.
    human = human_events.read_human_events(root, thread_id) if any('/human-events/' in str(p) for p in files) else {'events': [], 'errors': []}
    actors = {(event['author'], event['identity']) for event in human['events']}
    record['participant_details'] = [dict(agent=agent, identity=bindings.get(agent, agent),
        actor_type='human' if (agent, bindings.get(agent, agent)) in actors else 'agent',
        is_coordinator=agent == record['coordinator']) for agent in record['participants']]
    for agent in sorted({turn['author'] for turn in record['turns'] if isinstance(turn.get('author'), str)} - set(record['participants'])):
        identity = bindings.get(agent, agent)
        record['participant_details'].append(dict(agent=agent, identity=identity,
            actor_type='human' if (agent, identity) in actors else 'agent', is_coordinator=False))
    for agent, identity in sorted(actors):
        if not any(item['agent'] == agent and item['identity'] == identity for item in record['participant_details']):
            record['participant_details'].append(dict(agent=agent, identity=identity, actor_type='human', is_coordinator=False))
    record['coordinator_identity'] = bindings.get(record['coordinator'], record['coordinator'])
    record['last_activity_at'] = ui_read_model.last_activity_at(record, human['events'])
    record['history_errors'] = human['errors']
    size = len(json.dumps(record, ensure_ascii=False).encode()) + len(repr(signature).encode())
    with _LOCK:
        old = _INDEX.pop(key, None)
        if old: _index_bytes -= old[2]
        if size <= INDEX_BYTES:
            _INDEX[key] = (signature, copy.deepcopy(record), size)
            _index_bytes += size
        while _INDEX and (len(_INDEX) > INDEX_LIMIT or _index_bytes > INDEX_BYTES):
            _, removed = _INDEX.popitem(last=False)
            _index_bytes -= removed[2]
    return record


def _catalog(projects):
    records = []
    for project in projects:
        if not project['available']:
            project['thread_count'] = 0
            continue
        root = Path(project['root'])
        try:
            ids = collab.list_thread_ids(root)
            project['thread_count'] = len(ids)
            for thread_id in ids:
                read_budget.check()
                try:
                    records.append(index_record(root, thread_id, project))
                except (collab.CollaborationError, OSError, ValueError, TypeError, KeyError) as exc:
                    records.append(dict(project_id=project['id'], project_name=project['name'], thread_id=thread_id,
                        topic=thread_id, participants=[], participant_details=[], turns=[], signals=[],
                        status='NONE', terminal=False, round=0, started_at='', updated_at='', last_activity_at='', error=str(exc)))
        except (collab.CollaborationError, OSError) as exc:
            project.update(thread_count=0, error=str(exc))
    records.sort(key=ui_read_model.thread_sort_key)
    return records


def _people(projects, records):
    # This aggregation needs metadata, not inbox validation or task acceptance.
    by_project = {p['id']: p for p in projects}
    people = {}
    for record in records:
        project = by_project[record['project_id']]
        if project.get('identity_error'): continue
        for actor in record.get('participant_details', []):
            identity, agent = actor['identity'], actor['agent']
            person = people.setdefault(identity, dict(id=identity, identity=identity, actor_type=actor['actor_type'],
                aliases=[], projects=[], threads=[], thread_count=0, pending=None, turns=0, tools=[], last_at=''))
            if actor['actor_type'] == 'human': person['actor_type'] = 'human'
            alias = dict(project_id=project['id'], agent=agent)
            if alias not in person['aliases']: person['aliases'].append(alias)
            if not any(p['id'] == project['id'] for p in person['projects']):
                person['projects'].append({k: project[k] for k in ('id', 'name', 'root')})
            current_actor = identity == (project.get('identity_bindings') or {}).get(agent, agent)
            tool = project.get('tools', {}).get(agent) if current_actor else None
            if tool and tool not in person['tools']: person['tools'].append(tool)
            key = (record['project_id'], record['thread_id'])
            if key not in person.setdefault('_keys', set()):
                person['_keys'].add(key)
                person['thread_count'] += 1
            if current_actor:
                person['turns'] += sum(turn.get('author') == agent for turn in record['turns'])
            person['last_at'] = max(person['last_at'], record.get('last_activity_at', ''))
    for person in people.values(): person.pop('_keys', None)
    return sorted(people.values(), key=lambda p: p['id'])


def _page(records, number):
    total = len(records)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    number = min(number, pages)
    return records[(number - 1) * PAGE_SIZE:number * PAGE_SIZE], dict(number=number, size=PAGE_SIZE, total=total, total_pages=pages)


def _summary(record):
    result = copy.deepcopy(record)
    # Do not return the catalog's full set of turn headers for list cards.
    result['turns'] = result['turns'][-1:]
    root = Path(record.get('project_root') or '')
    for agent in record.get('participants', []):
        read_budget.check()
        # Fast absence check avoids reopening manifests on the common empty inbox.
        try:
            signal = collab.resolve_repo_path(root, f".ai-collab/threads/{record['thread_id']}/signals/to-{agent}.json", label='signal')
            if not signal.is_file(): continue
            for item in collab.inspect_inbox(root, agent, record['thread_id'])['results']:
                message = item.get('message') or {}
                result['signals'].append({**message, 'to': agent, 'decision': item['decision'], 'error': item.get('error')})
        except (collab.CollaborationError, OSError) as exc:
            result.setdefault('index_errors', []).append(str(exc))
    return result


def page_data(view='overview', *, project_id='', thread_id='', identity='', page=1, q='', path=None):
    import ui_server
    if view not in {'overview', 'projects', 'project', 'threads', 'thread', 'people', 'person', 'tasks', 'auto'}:
        raise collab.CollaborationError('unknown page view')
    if not isinstance(page, int) or not 1 <= page <= 1000000:
        raise collab.CollaborationError('page must be a positive integer')
    if len(q) > 200: raise collab.CollaborationError('search is too long')
    path = path or ui_server.registry_path()
    entries = ui_server.load_registry(path)['projects']
    # A route-scoped request never opens an unrelated project's files.
    if view in {'project', 'thread', 'auto'}:
        entries = [entry for entry in entries if ui_server.project_id(str(Path(entry['root']))) == project_id]
        if not entries: raise collab.CollaborationError('unknown registered project')
    projects = sorted([ui_server.project_shell(entry) for entry in entries], key=lambda p: (p['name'], p['id']))
    scope = hashlib.sha256((str(path.resolve()) + '\0' + json.dumps(sorted(entry['root'] for entry in ui_server.load_registry(path)['projects']))).encode()).hexdigest()[:20]
    result = dict(schema_version=1, cache_scope=scope, view=view, generated_at=collab.now_iso(),
                  projects=projects, collaborators=[], overview={})
    if view in {'projects', 'auto'}:
        for project in projects:
            try: project['thread_count'] = len(collab.list_thread_ids(Path(project['root']))) if project['available'] else 0
            except (collab.CollaborationError, OSError) as exc: project.update(thread_count=0, error=str(exc))
        return result
    records = _catalog(projects)
    # People are required only for their own views and the overview count.
    if view in {'overview', 'people', 'person', 'tasks'}:
        result['collaborators'] = _people(projects, records)
    if view == 'people': return result
    if view == 'tasks':
        import task_discovery
        identity = identity or next((p['identity'] for p in result['collaborators']), '')
        result['tasks'] = task_discovery.query_tasks([Path(p['root']) for p in projects], identity) if identity else {'identity': '', 'items': [], 'errors': []}
        return result
    if view == 'person':
        records = [r for r in records if any(a['identity'] == identity for a in r.get('participant_details', []))]
    if q:
        needle = q.casefold()
        records = [r for r in records if needle in ' '.join(str(r.get(k, '')) for k in ('thread_id', 'topic', 'objective', 'project_name')).casefold()]
    selected, result['page'] = _page(records, page)
    if view == 'overview':
        selected = records[:PAGE_SIZE]
        recent = [{**turn, 'project_id': record['project_id'], 'project_name': record['project_name'], 'thread_id': record['thread_id'], 'topic': record['topic']} for record in records for turn in record['turns'] if ui_read_model.activity_timestamp(turn.get('created_at')) is not None]
        recent.sort(key=lambda turn: ui_read_model.activity_timestamp(turn['created_at']), reverse=True)
        attention = []
        for record in records:
            read_budget.check()
            root = Path(next(p for p in projects if p['id'] == record['project_id'])['root'])
            for agent in record['participants']:
                try:
                    signal = collab.resolve_repo_path(root, f".ai-collab/threads/{record['thread_id']}/signals/to-{agent}.json", label='signal')
                    if not signal.is_file(): continue
                    for item in collab.inspect_inbox(root, agent, record['thread_id'])['results']:
                        if item['decision'] in ui_server.ATTENTION:
                            message = item.get('message') or {}
                            attention.append(dict(project_id=record['project_id'], project_name=record['project_name'],
                                thread_id=record['thread_id'], topic=record['topic'], to=agent,
                                **{k: message.get(k) for k in ('from', 'requested_action', 'created_at')}, decision=item['decision']))
                except (collab.CollaborationError, OSError) as exc:
                    result.setdefault('errors', []).append(str(exc))
        attention.sort(key=lambda item: item.get('created_at') or '')
        result['overview'] = dict(projects=len(projects), threads=len(records), collaborators=len(result['collaborators']),
                                  pending=len(attention), attention=attention[:20], recent=recent[:12], scope='summary', attention_limit=20, recent_limit=12)
        result.pop('page', None)
    for record in selected:
        next(p for p in projects if p['id'] == record['project_id'])['threads'].append(_summary(record))
    if view == 'person':
        for person in result['collaborators']:
            if person['identity'] == identity: person['threads'] = [_summary(r) for r in selected]
    if view == 'thread':
        detail = ui_server.thread_detail(project_id, thread_id, path)['thread']
        result['detail'] = detail
        # A direct-linked selected thread can be outside the sidebar's page.
    return result


class PageReadError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class PageReads:
    """Bound actual work, coalesce identical in-flight reads, never replay old data.

    This is one HTTP response, not a cursor/job polling protocol. Disconnected
    callers cannot start unlimited filesystem work. A blocked OS call retains its
    slot until it actually returns; waiting duplicates have a bounded deadline.
    """
    def __init__(self, capacity=2, seconds=30):
        self.capacity = capacity
        self.seconds = seconds
        self.lock = threading.Lock()
        self.active = {}

    def read(self, key, operation):
        with self.lock:
            state = self.active.get(key)
            owner = state is None
            if owner:
                if len(self.active) >= self.capacity:
                    raise PageReadError('page readers busy; retry after current read finishes', 503)
                state = {'event': threading.Event()}
                self.active[key] = state
        if not owner:
            if not state['event'].wait(self.seconds):
                raise PageReadError('page read timed out', 504)
            if 'error' in state: raise state['error']
            return copy.deepcopy(state['result'])
        try:
            with read_budget.budget(time.monotonic() + self.seconds):
                state['result'] = operation()
            return state['result']
        except Exception as exc:
            state['error'] = exc
            raise
        finally:
            with self.lock:
                self.active.pop(key, None)
                state['event'].set()


READS = PageReads()
