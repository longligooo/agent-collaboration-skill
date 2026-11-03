"""Opt-in real-disk/HTTP scale check: python tests/page_scale_smoke.py.

Creates 12 Git projects, 1,200 threads, 2,400 turn documents, 12 task profiles
and 1,800 source files in a
temporary directory. Does not call models or touch registered user projects.
"""
import json
import subprocess
import threading
import time
from urllib.request import urlopen
from unittest import mock

import test_ui_batches as batches
import ui_pages
import collab
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
            for number in range(100):
                directory = root / f'.ai-collab/threads/thread-{number:03}/turns'
                directory.mkdir()
                for turn in (1,2):
                    metadata = dict(round=turn, author='lead', role='reviewer', kind='REVIEW', status='OPEN',
                                    requested_action='REVISE', created_at=f'2026-10-30T10:00:0{turn}Z')
                    (directory / f'round-{turn:04}-from-lead-review.md').write_text(
                        collab.META_START + json.dumps(metadata) + collab.META_END + '\n# Synthetic\n' + 'x' * 8192)

        httpd = s.ThreadingHTTPServer(('127.0.0.1', 0), s.CollaborationHandler)
        ui_pages._INDEX.clear(); ui_pages._index_bytes = 0
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        def request(query, expected_rows, expected_manifests=None):
            started = time.monotonic()
            with mock.patch.object(s, 'scan_thread', side_effect=AssertionError('rich projection forbidden')), \
                 mock.patch.object(s, 'project_code_fingerprint', side_effect=AssertionError('source scan forbidden')), \
                 mock.patch.object(collab, 'load_manifest', wraps=collab.load_manifest) as manifests:
                with urlopen(f'http://127.0.0.1:{port}/api/page?{query}', timeout=10) as response:
                    payload = json.load(response)
                    assert response.status == 200
                rows = sum(len(p['threads']) for p in payload['projects'])
                assert rows == expected_rows, rows
                if expected_manifests is not None: assert manifests.call_count == expected_manifests, manifests.call_count
                result = dict(query=query, seconds=round(time.monotonic()-started,3), rows=rows,
                    manifest_reads=manifests.call_count, rich_projections=0, source_scans=0,
                    bytes=len(json.dumps(payload).encode()), total=payload.get('page',{}).get('total'))
                print(json.dumps(result),flush=True)
                return payload
        with mock.patch.object(s, 'registry_path', return_value=fixture.registry):
            request('view=projects',0,0)
            request('view=threads&page=1',20,1200)
            request('view=threads&page=2',20,0)
            request('view=threads&page=60',20,0)
            request('view=project&project_id='+s.project_id(str(roots[7])),20,0)
            changed = roots[7] / '.ai-collab/threads/thread-055/thread.json'
            data=json.loads(changed.read_text());data['topic']='UniqueChanged';changed.write_text(json.dumps(data))
            request('view=threads&page=1',20,1)
            found=request('view=threads&q=UniqueChanged',1,0)
            assert found['page']['total']==1
            import shutil
            shutil.rmtree(roots[0]/'.ai-collab/threads/thread-001')
            result=request('view=threads&page=60',19,0)
            assert result['page']['total']==1199
    finally:
        if httpd:
            httpd.shutdown(); httpd.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
