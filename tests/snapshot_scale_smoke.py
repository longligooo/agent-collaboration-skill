"""Opt-in real-disk/HTTP scale check: python tests/snapshot_scale_smoke.py.

Creates12 Git projects,1200 threads,12 task profiles and1800 source files in a
temporary directory. Does not call models or touch registered user projects.
"""
import json
import subprocess
import threading
import time
from urllib.request import urlopen
from unittest import mock

import test_ui_batches as batches
import snapshot_jobs
import task_model


def main():
    s = batches.ui_server
    fixture = batches.ProgressiveSnapshotTests(); fixture.setUp()
    httpd = None
    try:
        roots = []
        for p in range(12):
            root = fixture.project(f'project-{p:02}', 100); roots.append(root)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('.ai-collab/\n')
            for i in range(150): (root / f'module_{i:03}.py').write_text(f'VALUE = {i}\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            task_model.initialize_task(root, 'thread-000', 'builder', [{'id': 'ac1', 'text': 'Synthetic acceptance'}])
        httpd = s.ThreadingHTTPServer(('127.0.0.1', 0), s.CollaborationHandler)
        httpd.snapshot_jobs = snapshot_jobs.SnapshotJobs()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        def complete(since=None, expected_scans=1200):
            url = '/api/snapshot?progressive=1&async=1' + (f'&since={since}' if since else '')
            timings, statuses, pages = [], [], []
            started = time.monotonic()
            with mock.patch.object(s, 'scan_thread', wraps=s.scan_thread) as scan:
                while True:
                    before = time.monotonic()
                    with urlopen(f'http://127.0.0.1:{port}{url}', timeout=3) as response:
                        payload = json.load(response); status = response.status
                    timings.append(time.monotonic() - before); statuses.append(status)
                    assert status in {200, 202}, status
                    if status == 202:
                        url = '/api/snapshot?job=' + payload['job_id']
                        time.sleep(.1)
                    else:
                        pages.append(payload)
                        if payload['progress']['complete']: break
                        url = '/api/snapshot?cursor=' + payload['progress']['cursor'] + '&async=1'
                    assert time.monotonic() - started < 60, 'scale check did not finish'
                assert scan.call_count == expected_scans, (scan.call_count, expected_scans)
            assert payload['sync']['summary']['overview']['threads'] == 1200
            result = {'projects': 12, 'threads': 1200, 'tasks': 12, 'source_files': 1800,
                      'http_requests': len(timings), 'pending_responses': statuses.count(202),
                      'data_pages': len(pages), 'elapsed_seconds': round(time.monotonic() - started, 3),
                      'max_http_seconds': round(max(timings), 3), 'thread_scans': expected_scans}
            print(json.dumps(result), flush=True)
            return payload['sync']['version']
        with mock.patch.object(s, 'registry_path', return_value=fixture.registry):
            version = complete()
            version = complete(version, 0)
            changed = roots[7] / '.ai-collab/threads/thread-055/thread.json'
            data = json.loads(changed.read_text()); data['topic'] = 'Changed after baseline'
            changed.write_text(json.dumps(data))
            complete(version, 1)
    finally:
        if httpd:
            httpd.shutdown(); httpd.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
