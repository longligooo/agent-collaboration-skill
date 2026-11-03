"""HTTP recovery from aborts/slow scans, with real synthetic local projects."""
import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.request import urlopen
from urllib.error import HTTPError

import test_ui_batches as batches
import read_budget
import snapshot_jobs
import task_model

server = batches.ui_server


class JobRecoveryTests(unittest.TestCase):
    setUp = batches.ProgressiveSnapshotTests.setUp
    tearDown = batches.ProgressiveSnapshotTests.tearDown
    project = batches.ProgressiveSnapshotTests.project

    def http(self, port, route):
        try:
            response = urlopen(f'http://127.0.0.1:{port}{route}', timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def wait_job(self, jobs, token):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, data = jobs.poll(token)
            if status != 202:
                return status, data
            time.sleep(.005)
        self.fail('job did not complete')

    def test_aborted_initial_requests_share_one_worker_and_refresh_is_fresh(self):
        self.project('a', 3)
        entered, release = threading.Event(), threading.Event()
        original = server.project_config_fingerprint
        calls = []
        def slow(*args):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.CollaborationHandler)
        httpd.snapshot_jobs = snapshot_jobs.SnapshotJobs()
        httpd.handle_error = lambda *args: None
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        port = httpd.server_address[1]
        try:
            with mock.patch.object(server, 'registry_path', return_value=self.registry), mock.patch.object(server, 'project_config_fingerprint', side_effect=slow):
                # Client disconnects before consuming the202 response, then opens
                # many new pages: the backend still performs only one initial scan.
                for _ in range(12):
                    client = socket.create_connection(('127.0.0.1', port))
                    client.sendall(b'GET /api/snapshot?progressive=1&async=1 HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
                    client.close()
                self.assertTrue(entered.wait(2))
                responses = [self.http(port, '/api/snapshot?progressive=1&async=1') for _ in range(12)]
                self.assertEqual({status for status, _ in responses}, {202})
                tokens = {data['job_id'] for _, data in responses}
                self.assertEqual(len(tokens), 1)
                self.assertEqual(httpd.snapshot_jobs.running, 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.http(port, '/api/health')[0], 200)
                release.set()
                token = next(iter(tokens))
                status, shell = self.wait_job(httpd.snapshot_jobs, token)
                self.assertEqual(status, 200)
                self.assertEqual(shell['progress']['total'], 3)
                # A later explicit refresh must not reuse the consumed initial job.
                status, fresh = self.http(port, '/api/snapshot?progressive=1&async=1')
                self.assertIn(status, {200, 202})
                if status == 202:
                    self.assertNotEqual(fresh['job_id'], token)
                    self.assertEqual(self.wait_job(httpd.snapshot_jobs, fresh['job_id'])[0], 200)
                self.assertGreater(len(calls), 2)
        finally:
            release.set(); httpd.shutdown(); httpd.server_close()

    def test_abandoned_completed_job_does_not_hide_later_changes(self):
        jobs = snapshot_jobs.SnapshotJobs()
        old = jobs.submit('initial', lambda: {'value': 'old'})[1]['job_id']
        # Simulate loss of the initial202; nobody polls/consumes the result.
        self.assertTrue(jobs.jobs[old]['ready'].wait(2))
        fresh = jobs.submit('initial', lambda: {'value': 'new'})[1]['job_id']
        self.assertNotEqual(old, fresh)
        self.assertEqual(self.wait_job(jobs, fresh), (200, {'value': 'new'}))
        self.assertEqual(jobs.poll(old), (200, {'value': 'old'}))

    def test_capacity_is_bounded_even_after_deadline_until_os_read_finishes(self):
        jobs = snapshot_jobs.SnapshotJobs(workers=2, timeout=.05)
        release = threading.Event()
        def stuck():
            release.wait(3)
            return {'projects': []}
        a = jobs.submit('a', stuck)[1]['job_id']; b = jobs.submit('b', stuck)[1]['job_id']
        try:
            time.sleep(.07)
            self.assertEqual(jobs.poll(a)[1]['error_code'], 'snapshot_timeout')
            self.assertEqual(jobs.submit('a', stuck)[1]['error_code'], 'snapshot_timeout')
            for i in range(20):
                self.assertEqual(jobs.submit(str(i), stuck)[1]['error_code'], 'snapshot_busy')
            self.assertEqual(jobs.running, 2)
            self.assertEqual(len(jobs.jobs), 2)
        finally:
            release.set()
        self.assertEqual(self.wait_job(jobs, a)[1]['error_code'], 'snapshot_timeout')
        self.assertEqual(self.wait_job(jobs, b)[1]['error_code'], 'snapshot_timeout')
        deadline = time.monotonic() + 2
        while jobs.running and time.monotonic() < deadline: time.sleep(.005)
        self.assertEqual(jobs.running, 0)
        c = jobs.submit('c', lambda: {'projects': []})[1]['job_id']
        self.assertEqual(self.wait_job(jobs, c)[0], 200)

    def test_result_cache_count_bytes_ttl_and_replay_are_bounded(self):
        jobs = snapshot_jobs.SnapshotJobs(max_results=4, max_bytes=100, ttl=.05)
        for i in range(12):
            token = jobs.submit(str(i), lambda: {'value': 'x' * 20})[1]['job_id']
            self.assertEqual(self.wait_job(jobs, token)[0], 200)
            self.assertLessEqual(sum(j['bytes'] for j in jobs.jobs.values()), 100)
            self.assertLessEqual(len(jobs.jobs), 4)
        self.assertEqual(jobs.poll(token)[0], 200)
        time.sleep(.06)
        self.assertEqual(jobs.poll(token)[0], 410)
        token = jobs.submit('large', lambda: {'value': 'x' * 101})[1]['job_id']
        self.assertEqual(self.wait_job(jobs, token)[0], 413)

    def test_git_budget_kills_subprocess_and_reaches_nested_task_projection(self):
        root = self.project('a', 1)
        original = subprocess.run
        observed = []
        def slow_git(*args, **kwargs):
            observed.append(kwargs.get('timeout'))
            return original([sys.executable, '-c', 'import time;time.sleep(5)'], **kwargs)
        for operation in (lambda: server.project_code_fingerprint(root), lambda: task_model.code_version(root)):
            started = time.monotonic()
            with mock.patch.object(subprocess, 'run', side_effect=slow_git), self.assertRaises(read_budget.ReadTimeout):
                with read_budget.budget(time.monotonic() + .08): operation()
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(0 < value <= .08 for value in observed))
        # Budget is thread-local and cleared after exceptions.
        read_budget.check()

    def test_metadata_and_final_verification_honor_deadline(self):
        self.project('a', 3)
        shell = server.progressive_snapshot(path=self.registry)
        original = server.thread_fingerprint
        calls = []
        def slow(*args, **kwargs):
            calls.append(1)
            time.sleep(.01)
            return original(*args, **kwargs)
        with mock.patch.object(server, 'thread_fingerprint', side_effect=slow), self.assertRaises(read_budget.ReadTimeout):
            with read_budget.budget(time.monotonic() + .005):
                server.progressive_snapshot(shell['progress']['cursor'])
        self.assertTrue(calls)
        self.assertTrue(server.SNAPSHOT_WORKERS.acquire(blocking=False))
        server.SNAPSHOT_WORKERS.release()

    def test_final_validation_timeout_does_not_publish_a_new_version(self):
        root = self.project('a', 1)
        shell = server.progressive_snapshot(path=self.registry)
        baseline = server.progressive_snapshot(shell['progress']['cursor'])
        version = baseline['sync']['version']
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        data = json.loads(manifest.read_text()); data['topic'] = 'Modified'
        manifest.write_text(json.dumps(data))
        shell = server.progressive_snapshot(path=self.registry, since=version)
        versions = set(server.SNAPSHOT_VERSIONS)
        original = server.project_config_fingerprint
        calls = []
        def slow_final(*args):
            calls.append(1)
            if len(calls) == 2: time.sleep(.04)
            return original(*args)
        with mock.patch.object(server, 'project_config_fingerprint', side_effect=slow_final), self.assertRaises(read_budget.ReadTimeout):
            with read_budget.budget(time.monotonic() + .03):
                server.progressive_snapshot(shell['progress']['cursor'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(server.SNAPSHOT_VERSIONS), versions)
        self.assertIn(version, server.SNAPSHOT_VERSIONS)
        fresh = server.progressive_snapshot(path=self.registry, since=version)
        self.assertEqual(fresh['progress']['total'], 1)

    def test_worker_start_failure_does_not_leak_capacity(self):
        jobs = snapshot_jobs.SnapshotJobs()
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('no thread')):
            self.assertEqual(jobs.submit('initial', lambda: {})[0], 503)
        self.assertEqual(jobs.running, 0)
        self.assertEqual(jobs.jobs, {})
        self.assertEqual(jobs.active, {})

    def test_version_verification_rejects_old_process_and_accepts_identical_sources(self):
        expected = {'source_fingerprint': 'current', 'commit': 'sha', 'source_root': '/source'}
        with mock.patch.object(server, 'ui_build_info', return_value=expected):
            for health in ({'pid': 11}, {'pid': 12, 'build': {'source_fingerprint': 'old'}}):
                with self.assertRaisesRegex(server.collab.CollaborationError, 'Stop that UI process'):
                    server.verify_running_ui(health)
            server.verify_running_ui({'pid': 13, 'build': expected})
