"""Bounded, cursor-based progressive snapshots over synthetic local projects."""
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ui_read_model
import ui_server


class ProgressiveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.registry = self.base / 'registry.json'
        ui_server.SNAPSHOT_SESSIONS.clear()
        ui_server.SNAPSHOT_VERSIONS.clear()

    def tearDown(self):
        ui_server.SNAPSHOT_SESSIONS.clear()
        ui_server.SNAPSHOT_VERSIONS.clear()
        self.tmp.cleanup()

    def project(self, name, count, timestamp='2026-01-01T00:00:00Z'):
        root = self.base / name
        root.mkdir(exist_ok=True)
        for i in range(count):
            thread_id = f'thread-{i:03}'
            directory = root / '.ai-collab/threads' / thread_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'thread.json').write_text(json.dumps({
                'protocol_version': 1, 'thread_id': thread_id, 'topic': thread_id,
                'objective': 'Synthetic batch acceptance', 'participants': ['lead', 'builder'],
                'coordinator': 'lead', 'created_at': timestamp,
            }))
        ui_server.register_project(root, self.registry)
        return root

    def start(self):
        return ui_server.progressive_snapshot(path=self.registry)

    def finish(self, page, initial=None):
        # Reconstruct once on the consumer, never ask the backend to reaggregate
        # already-returned records. Compare the result with legacy projection.
        projects = {p['id']: copy.deepcopy(p) for p in (initial or {'projects': []})['projects']}
        while True:
            self.assertTrue(page['progress']['delta'])
            for project in page['projects']:
                if project['id'] not in projects:
                    projects[project['id']] = {**copy.deepcopy(project), 'threads': []}
                projects[project['id']]['threads'].extend(copy.deepcopy(project['threads']))
            if page['progress']['complete']:
                break
            page = ui_server.progressive_snapshot(page['progress']['cursor'])
        for project in projects.values():
            project['threads'].sort(key=ui_read_model.thread_sort_key)
        result = ui_server.snapshot_from_projects(list(projects.values()))
        result['progress'] = page['progress']
        return result

    def test_shell_does_not_scan_and_first_batch_precedes_last_delayed_thread(self):
        self.project('a', 16)
        self.project('b', 2)
        original = ui_server.scan_thread
        called = []
        def slow(*args):
            called.append(args[1])
            time.sleep(0.025)
            return original(*args)
        with mock.patch.object(ui_server, 'scan_thread', side_effect=slow):
            start = time.monotonic()
            shell = self.start()
            self.assertEqual(called, [])
            self.assertEqual(shell['progress']['loaded'], 0)
            first = ui_server.progressive_snapshot(shell['progress']['cursor'])
            first_time = time.monotonic() - start
            self.assertGreater(first['progress']['loaded'], 0)
            self.assertLess(first['progress']['loaded'], 18)
            self.assertTrue(all(project['threads'] for project in first['projects']))
            final = self.finish(first, shell)
            full_time = time.monotonic() - start
        self.assertEqual(final['progress']['loaded'], 18)
        self.assertLess(first_time, full_time / 2)
        print(f'\nSynthetic 18-thread delayed scan: first data {first_time:.3f}s; complete {full_time:.3f}s')

    def test_117_threads_round_robin_global_order_final_legacy_parity_and_replay(self):
        self.project('a-large', 116)
        new = self.project('z-new', 1, '2026-10-30T09:00:00Z')
        shell = self.start()
        cursor = shell['progress']['cursor']
        first = ui_server.progressive_snapshot(cursor)
        self.assertEqual(first, ui_server.progressive_snapshot(cursor))
        self.assertLessEqual(first['progress']['loaded'], ui_server.SNAPSHOT_BATCH_SIZE)
        self.assertEqual(sorted([t for p in first['projects'] for t in p['threads']],
                                key=ui_read_model.thread_sort_key)[0]['project_id'],
                         ui_server.project_id(str(new)))
        frozen_first = copy.deepcopy(first)
        final = self.finish(first, shell)
        self.assertEqual(first, frozen_first, 'previous returned pages must never mutate')
        legacy = ui_server.snapshot(self.registry)
        final.pop('progress')
        final.pop('generated_at'); legacy.pop('generated_at')
        self.assertEqual(final, legacy)
        self.assertEqual(final['overview']['threads'], 117)
        self.assertEqual(final['collaborators'][0]['threads'][0]['project_id'], ui_server.project_id(str(new)))
        with self.assertRaises(ui_server.SnapshotRequestError) as error:
            ui_server.progressive_snapshot(cursor)
        self.assertEqual(error.exception.status, 410)

    def test_many_projects_produce_data_before_remaining_scans(self):
        for i in range(12):
            self.project(f'project-{i:02}', 10)
        original = ui_server.scan_thread
        calls = []
        def scan(*args):
            calls.append((args[2]['id'], args[1]))
            return original(*args)
        with mock.patch.object(ui_server, 'scan_thread', side_effect=scan):
            start = time.monotonic()
            shell = self.start()
            first = ui_server.progressive_snapshot(shell['progress']['cursor'])
            first_time = time.monotonic() - start
            self.assertGreater(first['progress']['loaded'], 0)
            self.assertLessEqual(len(calls), 8)
            self.assertEqual(len({project for project, _ in calls}), len(calls))
            final = self.finish(first, shell)
            full_time = time.monotonic() - start
        self.assertEqual(len(calls), 120)
        self.assertEqual(len(set(calls)), 120)
        self.assertEqual(final['overview']['threads'], 120)
        self.assertLess(first_time, full_time)
        print(f'\nSynthetic 12-project/120-thread scan: first data {first_time:.3f}s; complete {full_time:.3f}s')

    def test_1000_thread_no_io_pages_do_not_reaggregate_or_retain_old_records(self):
        project = {'id': 'synthetic', 'name': 'synthetic', 'root': '/synthetic',
                   'available': True, 'threads': [], 'tools': {}, 'identity_bindings': {}}
        ids = [f'thread-{i:04}' for i in range(1000)]
        def scan(root, thread_id, metadata):
            return {'project_id': 'synthetic', 'thread_id': thread_id, 'topic': thread_id,
                    'participants': ['lead', 'builder'], 'coordinator': 'lead', 'status': 'NONE',
                    'updated_at': '', 'started_at': '2026-01-01T00:00:00Z',
                    'last_activity_at': '2026-01-01T00:00:00Z', 'turns': [], 'signals': []}
        with mock.patch.object(ui_server, 'load_registry', return_value={'projects': [{'root': '/synthetic'}]}), \
             mock.patch.object(ui_server, 'project_shell', side_effect=lambda _: copy.deepcopy(project)), \
             mock.patch.object(ui_server.collab, 'list_thread_ids', return_value=ids), \
             mock.patch.object(ui_server, 'scan_thread', side_effect=scan):
            start = time.monotonic()
            page = self.start()
            output = []
            while not page['progress']['complete']:
                page = ui_server.progressive_snapshot(page['progress']['cursor'])
                records = [t for p in page['projects'] for t in p['threads']]
                self.assertLessEqual(len(records), ui_server.SNAPSHOT_BATCH_SIZE)
                self.assertLessEqual(page['overview']['threads'], ui_server.SNAPSHOT_BATCH_SIZE)
                output.extend(records)
                session = next(iter(ui_server.SNAPSHOT_SESSIONS.values()))
                self.assertTrue(all(not p['threads'] for p in session['projects']))
                self.assertLessEqual(session['last_payload']['overview']['threads'], ui_server.SNAPSHOT_BATCH_SIZE)
            delta_time = time.monotonic() - start
            start = time.monotonic()
            legacy = ui_server.snapshot(self.registry)
            legacy_time = time.monotonic() - start
        self.assertEqual(len(output), 1000)
        self.assertEqual(len({r['thread_id'] for r in output}), 1000)
        self.assertEqual(sorted(output, key=ui_read_model.thread_sort_key), legacy['projects'][0]['threads'])
        # Structural bounds above are the non-flaky performance regression; report
        # timing separately because shared CI machines can be descheduled.
        print(f'\n1000-thread no-I/O benchmark: delta pages {delta_time:.3f}s; legacy {legacy_time:.3f}s')

    def test_worklist_freezes_registry_and_thread_membership_until_next_refresh(self):
        root = self.project('a', 2)
        shell = self.start()
        self.project('a', 3)
        self.project('new', 1)
        final = self.finish(shell)
        self.assertEqual(final['overview']['projects'], 1)
        self.assertEqual(final['overview']['threads'], 2)
        fresh = self.finish(self.start())
        self.assertEqual(fresh['overview']['threads'], 4)
        self.registry.write_text('{"projects": []}')
        empty = self.start()
        self.assertTrue(empty['progress']['complete'])
        self.assertEqual(empty['projects'], [])

    def test_expiry_validation_and_session_count_bound(self):
        self.project('a', 1)
        shell = self.start()
        cursor = shell['progress']['cursor']
        with mock.patch.object(ui_server.time, 'monotonic', return_value=time.monotonic() + 121):
            with self.assertRaises(ui_server.SnapshotRequestError) as error:
                ui_server.progressive_snapshot(cursor)
        self.assertEqual(error.exception.status, 410)
        for invalid in ['', '../../etc/passwd', 'a' * 32 + ':x', 'a' * 32 + ':999999999999999']:
            with self.assertRaises(ui_server.SnapshotRequestError) as error:
                ui_server.progressive_snapshot(invalid)
            self.assertEqual(error.exception.status, 400)
        for _ in range(12):
            self.start()
            self.assertLessEqual(len(ui_server.SNAPSHOT_SESSIONS), ui_server.SNAPSHOT_MAX_SESSIONS)

    def test_same_cursor_concurrent_scan_rejected_and_worker_budget_enforced(self):
        self.project('a', 1)
        shell = self.start()
        cursor = shell['progress']['cursor']
        entered, release = threading.Event(), threading.Event()
        original = ui_server.scan_thread
        result = []
        def held(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        with mock.patch.object(ui_server, 'scan_thread', side_effect=held):
            worker = threading.Thread(target=lambda: result.append(ui_server.progressive_snapshot(cursor)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ui_server.SnapshotRequestError) as error:
                    ui_server.progressive_snapshot(cursor)
                self.assertEqual(error.exception.status, 503)
                self.assertTrue(ui_server.SNAPSHOT_WORKERS.acquire(blocking=False))
                try:
                    with self.assertRaises(ui_server.SnapshotRequestError) as error:
                        self.start()
                    self.assertEqual(error.exception.status, 503)
                finally:
                    ui_server.SNAPSHOT_WORKERS.release()
            finally:
                release.set(); worker.join(5)
        self.assertEqual(result[0]['progress']['loaded'], 1)
        self.assertEqual(result[0], ui_server.progressive_snapshot(cursor))

    def test_http_query_validation_and_activity_not_blocked_by_slow_page(self):
        self.project('a', 1)
        server = ui_server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.CollaborationHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        def get(route):
            with urlopen(f'http://127.0.0.1:{port}{route}', timeout=3) as response:
                return json.load(response)
        entered, release = threading.Event(), threading.Event()
        original = ui_server.scan_thread
        def held(*args):
            entered.set(); release.wait(5)
            return original(*args)
        try:
            with mock.patch.object(ui_server, 'registry_path', return_value=self.registry):
                self.assertEqual(get('/api/snapshot')['overview']['threads'], 1)
                for query in ['progressive=0', 'progressive=1&progressive=1', 'cursor=', 'root=/tmp', 'progressive=1&cursor=x']:
                    with self.assertRaises(HTTPError) as error:
                        get('/api/snapshot?' + query)
                    self.assertEqual(error.exception.code, 400)
                shell = get('/api/snapshot?progressive=1')
                with mock.patch.object(ui_server, 'scan_thread', side_effect=held), \
                     mock.patch('runtime_activity.activity_snapshot', return_value={'runs': []}), \
                     mock.patch('task_discovery.registered_roots', return_value=[]):
                    results = []
                    worker = threading.Thread(target=lambda: results.append(get('/api/snapshot?cursor=' + shell['progress']['cursor'])))
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        activity = get('/api/activity')
                        self.assertEqual(activity['runs'], [])
                        self.assertEqual(activity['manual_observations']['observations'], [])
                        self.assertTrue(worker.is_alive())
                    finally:
                        release.set(); worker.join(5)
                    self.assertTrue(results[0]['progress']['complete'])
        finally:
            release.set(); server.shutdown(); server.server_close(); thread.join(5)
