"""Typed local API adapter; execution is owned by the separate supervisor.

Read endpoints never run discovery probes or models. This adapter is reachable
only through the authenticated opt-in auto_api gateway, never the legacy API.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import auto_api
from auto_model import AutoError, binding
from auto_store import AutoStore


class AutoService:
    def __init__(self, providers, *, store_factory=AutoStore, supervisor=None, workspace_manager=None):
        self.providers = providers
        self.store_factory = store_factory
        self.supervisor = supervisor
        self.workspace_manager = workspace_manager
        self._stores = {}
        self._lock = threading.RLock()
        self._refresh = OrderedDict()

    def store(self, root, project_id):
        key = (str(root), project_id)
        with self._lock:
            if key not in self._stores:
                self._stores[key] = self.store_factory(root, project_id)
            return self._stores[key]

    @staticmethod
    def metadata_paths(store):
        paths, after = [], None
        while True:
            tasks = store.list_tasks(after=after, limit=200)
            for task in tasks:
                if task.get('approved_plan_revision'):
                    base = '.scratch/automatic-' + task['id']
                    paths.extend((base + '/spec.md', base + '/issues/01-approved-plan.md'))
            if len(tasks) < 200:
                return paths
            after = tasks[-1]['id']

    def project_capabilities(self, store):
        from auto_workspace import capabilities
        return {'execution_workspace': capabilities(store.root, metadata_paths=self.metadata_paths(store)),
                'application': {'status': 'pending', 'automatic': False}}

    def _project(self, store, task, *, detail=False):
        result = dict(task)
        result['execution_workspace'] = {'kind': 'managed_snapshot', 'supported': None, 'requires_clean': False}
        result['application'] = {'status': 'pending', 'automatic': False}
        if self.workspace_manager is not None:
            workspace = self.workspace_manager.describe(store.root, store.project_id, task['id'])
            if workspace is not None:
                result['execution_workspace'] = {k: v for k, v in workspace.items() if k not in {'project_root', 'path'}}
                result['application'] = {'status': 'pending', 'automatic': False, 'base_commit': workspace.get('base_commit'),
                                         'snapshot_id': workspace.get('snapshot_id'), 'diff_base': workspace.get('diff_base')}
        import task_discovery
        identities = task_discovery.load_identities(store.root)
        result['participants'] = [{**p, 'identity': task_discovery.identity_for(p['participant_id'], identities)} for p in task['participants']]
        result['coordinator'] = {**task['coordinator'], 'identity': task_discovery.identity_for(task['coordinator']['participant_id'], identities)}
        summary = store.job_headers(task['id'], limit=200)
        result['jobs'], result['jobs_truncated'] = summary['jobs'], summary['truncated']
        # Only the recent result window is materialized. Full immutable attempts
        # remain private on disk; never construct a gigabyte HTTP history payload.
        jobs = store.list_jobs(task['id'], states=['succeeded', 'failed', 'unknown', 'cancelled'], limit=21, descending=True) if detail else []
        selected, size = [], 0
        candidates = [j for j in jobs if isinstance(j.get('result'), dict) and isinstance(j['result'].get('output'), str)]
        for j in candidates[:20]:
            body = j['result']['output']
            encoded = body.encode('utf-8')
            if size + len(encoded) > 1024 * 1024:
                break
            selected.append({'id': j['id'], 'participant_id': j['participant_id'], 'kind': j['kind'],
                             'body': body, 'truncated': j['result'].get('output_truncated') is True,
                             'created_at': j.get('finished_at'), 'state': j['state']})
            size += len(encoded)
        result['local_outputs'] = list(reversed(selected))
        result['local_outputs_window'] = {'limit':20, 'truncated': detail and (len(jobs) == 21 or len(selected) < len(candidates) or summary['truncated'])}
        if detail:
            applications = [j['result']['application'] for j in jobs if isinstance(j.get('result'), dict) and isinstance(j['result'].get('application'), dict)]
            if applications:
                result['application'] = {**applications[0], 'automatic': False}
        active = store.list_jobs(task['id'], states=['claimed','unknown'], limit=1)
        # A lease/claim is not a process observation. Fresh process telemetry is
        # supplied by the supervisor, never inferred from task.state='running'.
        result['runtime'] = {'status': 'unknown' if active else 'idle', 'active': False}
        if self.supervisor is not None:
            result['runtime'] = self.supervisor.activity(store.root, task['id'])
        if task.get('thread_id'):
            import task_model
            try:
                view = task_model.task_view(store.root, task['thread_id'])
                if view is not None:
                    result['acceptance'] = view
            except (ValueError, OSError):
                result['acceptance'] = {'status': 'unverified', 'error_code': 'acceptance_unavailable'}
        return result

    @staticmethod
    def _error(exc):
        if exc.code == 'not_found':
            status = 404
        elif exc.code in {'revision_conflict', 'idempotency_conflict', 'approval_conflict', 'approval_required',
                          'invalid_state', 'unsafe_retry', 'process_unknown', 'stale_claim', 'store_mismatch', 'unsafe_path'}:
            status = 409
        else:
            status = 422
        return auto_api.ApiError(exc.code, str(exc), status)

    def dispatch(self, operation, params, root, body, key):
        try:
            return self._dispatch(operation, params, root, body, key)
        except AutoError as exc:
            raise self._error(exc) from None
        except sqlite3.Error:
            raise auto_api.ApiError('store_unavailable', 'local store unavailable', 503) from None

    def _dispatch(self, operation, params, root, body, key):
        if operation == 'providers':
            return {'providers': self.providers.discover()}
        if operation == 'refresh_providers':
            with self._lock:
                if key not in self._refresh:
                    self._refresh[key] = {'providers': self.providers.discover(refresh=True)}
                    if len(self._refresh) > 128:
                        self._refresh.popitem(last=False)
                return self._refresh[key]
        store = self.store(root, params['project_id'])
        if operation == 'capabilities':
            return self.project_capabilities(store)
        task_id = params.get('task_id')
        def launch_guard(current):
            if self.workspace_manager is None:
                return  # read-only/unit-fixture service; real Supervisor requires manager
            capability = self.project_capabilities(store)['execution_workspace']
            if not capability['supported']:
                raise auto_api.ApiError(capability['reason'], 'project snapshot preflight failed; original files are never cleaned or stashed', 409)
        def mutation_guard(current):
            # Executed inside Store's mutation transaction, AFTER durable
            # idempotency lookup. Never turn a valid retry into a new validation.
            if not current.get('thread_id'):
                if operation == 'change_provider':
                    raise auto_api.ApiError('finalization_required', 'provider changes require a finalized thread', 409)
                import collab
                manifest = collab.resolve_repo_path(root, '.ai-collab/threads/' + task_id + '/thread.json', label='automatic manifest')
                if manifest.exists():
                    raise auto_api.ApiError('finalization_pending', 'finish recovery of the approved manifest before revising', 409)
            if operation == 'plan_edit':
                proposed = body['plan']
                expected = current.get('plan') or {}
                if proposed.get('ticket') != expected.get('ticket') or proposed.get('spec') != expected.get('spec'):
                    raise auto_api.ApiError('invalid_plan_paths', 'server-owned plan paths cannot be edited', 422)
                planned_tasks = proposed.get('tasks')
                if isinstance(planned_tasks, list) and any(isinstance(t, dict) and t.get('participant_id') == current['coordinator']['participant_id'] for t in planned_tasks):
                    raise auto_api.ApiError('coordinator_read_only', 'implementation tasks require a worker', 422)
        if operation == 'create_task':
            task = store.create_task(body['title'], body['instruction'], body['coordinator'], idempotency_key=key, guard=launch_guard)
            # Return the durable command result, not a race-dependent fresh task.
            return {'task': task}
        if operation == 'list_tasks':
            values = store.list_tasks(after=params.get('cursor'), limit=100)
            return {'tasks': [self._project(store, task) for task in values],
                    'next_cursor': values[-1]['id'] if len(values) == 100 else None}
        if operation == 'get_task':
            return {'task': self._project(store, store.get_task(task_id), detail=True)}
        if operation == 'get_plan':
            task = store.get_task(task_id)
            return {'revision': task['plan_revision'], 'hash': task['plan_hash'], 'plan': task['plan']}
        if operation in {'messages', 'events'}:
            try:
                cursor = int(params.get('cursor', '0'))
            except ValueError:
                raise auto_api.ApiError('invalid_cursor', 'numeric cursor required', 422) from None
            if not 0 <= cursor <= 9223372036854775807:
                raise auto_api.ApiError('invalid_cursor', 'cursor outside supported range', 422)
            return getattr(store, operation)(task_id, after=cursor, limit=100)
        if operation == 'approve':
            return {'task': store.approve(task_id, **body, idempotency_key=key, guard=launch_guard)}
        if operation == 'plan_edit':
            return {'task': store.edit_plan(task_id, **body, idempotency_key=key, guard=mutation_guard)}
        if operation == 'revise':
            return {'task': store.revise(task_id, **body, idempotency_key=key, guard=mutation_guard)}
        if operation in {'pause', 'resume', 'cancel'}:
            return {'task': store.control(task_id, operation, **body, idempotency_key=key)}
        if operation == 'retry':
            return store.retry(task_id, **body, idempotency_key=key)
        if operation == 'change_provider':
            return store.request_provider_change(task_id, params['participant_id'], **body, idempotency_key=key, guard=mutation_guard)
        if operation == 'send_message':
            return store.send_message(task_id, **body, idempotency_key=key)
        raise auto_api.ApiError('not_found', 'not found', 404)


def main(argv=None):
    """Explicit local service entry point; never started by a read request."""
    import argparse
    import hashlib
    import signal
    import sys
    import ui_server
    from auto_providers import ProviderRegistry
    from auto_runtime import Supervisor

    parser = argparse.ArgumentParser(description='Opt-in registered-project automatic collaboration service')
    parser.add_argument('command', choices=['serve'])
    parser.add_argument('--port', type=int, default=23452)
    parser.add_argument('--max-workers', type=int, default=2, choices=range(1, 5))
    args = parser.parse_args(argv)
    providers = ProviderRegistry()
    from auto_workspace import WorkspaceManager
    manager = WorkspaceManager(ui_server.ui_home() / 'automatic-workspaces')
    service = AutoService(providers, workspace_manager=manager)
    credentials = auto_api.Credentials()
    server = ui_server.make_server(args.port, auto_service=service, auto_credentials=credentials)

    def stores():
        result = []
        # Registry membership is checked afresh; no URL can add a root here.
        for item in ui_server.load_registry(ui_server.registry_path())['projects']:
            raw = item.get('root')
            if not isinstance(raw, str):
                continue
            project_id = hashlib.sha256(raw.encode()).hexdigest()[:12]
            try:
                root = server.auto_gateway.project_root(project_id)
                with service._lock:
                    exists = (str(root), project_id) in service._stores
                if exists or (root / '.ai-collab/local/automatic/state.sqlite3').is_file():
                    result.append(service.store(root, project_id))
            except (auto_api.ApiError, AutoError, OSError):
                # Missing/unavailable projects must not run, never resolve an
                # alternative arbitrary path to keep the scheduler going.
                continue
        return result

    supervisor = Supervisor(stores, providers, max_workers=args.max_workers, workspace_manager=manager)
    service.supervisor = supervisor
    # No implicit probe: the UI owner explicitly refreshes capabilities. The
    # initial task may block until discovery; old queues are still durable.
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, interrupted)
    print(f'Automatic collaboration: http://127.0.0.1:{args.port}/', flush=True)
    print('Local access token (paste only into this local UI): ' + credentials.token, flush=True)
    print('Keep this terminal open. No projects are created; provider discovery is explicit.', flush=True)
    try:
        supervisor.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stop()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
