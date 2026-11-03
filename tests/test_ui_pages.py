"""Page scoped reads must not pay for full histories or code acceptance."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collab
import ui_server
import ui_pages

class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.registry = self.base / 'registry.json'
        ui_pages._INDEX.clear()
        ui_pages._index_bytes = 0

    def tearDown(self): self.tmp.cleanup()

    def project(self, name, count):
        root = self.base / name
        root.mkdir()
        for i in range(count):
            directory = root / '.ai-collab/threads' / f'thread-{i:04d}'
            directory.mkdir(parents=True)
            (directory / 'thread.json').write_text(json.dumps(dict(protocol_version=1,thread_id=directory.name,
                topic=f'Topic {i}', objective='Synthetic page tests', participants=['lead','builder'],
                coordinator='lead',created_at=f'2026-01-{i%28+1:02d}T00:00:00Z')))
        ui_server.register_project(root, self.registry)
        return root

    def page(self, view='threads', **kwargs):
        return ui_pages.page_data(view, path=self.registry, **kwargs)

    def threads(self, payload): return [t for p in payload['projects'] for t in p['threads']]

    def test_only_twenty_records_no_full_projection_and_no_source_fingerprint(self):
        self.project('a', 45)
        with mock.patch.object(ui_server, 'scan_thread', side_effect=AssertionError('full thread projection forbidden')), \
             mock.patch.object(ui_server, 'project_code_fingerprint', side_effect=AssertionError('source fingerprint forbidden')):
            first = self.page()
            second = self.page(page=2)
        self.assertEqual(len(self.threads(first)), 20)
        self.assertEqual(len(self.threads(second)), 20)
        self.assertEqual(first['page']['total'],45)
        self.assertFalse(set(t['thread_id'] for t in self.threads(first)) & set(t['thread_id'] for t in self.threads(second)))
        self.assertEqual(len(self.threads(self.page(page=999))), 5)
        self.assertEqual(self.page(page=999)['page']['number'],3)

    def test_projects_does_not_open_manifests(self):
        self.project('a',45)
        with mock.patch.object(collab,'load_manifest', side_effect=AssertionError('unrelated content')):
            result=self.page('projects')
        self.assertEqual(result['projects'][0]['thread_count'],45)
        self.assertEqual(result['projects'][0]['threads'],[])

    def test_project_does_not_read_unrelated_root(self):
        a=self.project('a',25); b=self.project('b',25)
        original=ui_pages.index_record
        def check(root,*args):
            self.assertNotEqual(root,b)
            return original(root,*args)
        with mock.patch.object(ui_pages,'index_record',side_effect=check):
            result=self.page('project',project_id=ui_server.project_id(str(a)))
        self.assertEqual(result['page']['total'],25)
        self.assertEqual(len(result['projects']),1)

    def test_warm_index_does_not_reparse_unchanged_manifests(self):
        self.project('a',45); self.page()
        with mock.patch.object(collab,'load_manifest', side_effect=AssertionError('unchanged index reparse')):
            result=self.page(page=2)
        self.assertEqual(len(self.threads(result)),20)

    def test_changed_deleted_new_and_search_across_pages(self):
        root=self.project('a',45); self.page()
        path=root/'.ai-collab/threads/thread-0000/thread.json'
        data=json.loads(path.read_text()); data['topic']='Unique needle'; path.write_text(json.dumps(data))
        result=self.page(q='unique needle')
        self.assertEqual(result['page']['total'],1)
        self.assertEqual(self.threads(result)[0]['thread_id'],'thread-0000')
        import shutil
        shutil.rmtree(root/'.ai-collab/threads/thread-0000')
        self.assertEqual(self.page()['page']['total'],44)

    def test_turn_timestamp_not_mtime_drives_sort_and_header_is_bounded(self):
        root=self.project('a',2)
        turn=root/'.ai-collab/threads/thread-0000/turns/round-0001-from-lead-proposal.md'
        turn.parent.mkdir()
        metadata=dict(round=1,author='lead',kind='PROPOSAL',status='OPEN',created_at='2027-01-01T00:00:00Z')
        turn.write_text(collab.META_START+json.dumps(metadata)+collab.META_END+'\n'+'x'*1000000)
        with mock.patch.object(collab,'parse_turn',side_effect=AssertionError('body parser forbidden')):
            result=self.page()
        self.assertEqual(self.threads(result)[0]['thread_id'],'thread-0000')
        metadata['created_at']='2025-01-01T00:00:00Z'
        turn.write_text(collab.META_START+json.dumps(metadata)+collab.META_END+'\nbody')
        self.assertEqual(self.threads(self.page())[0]['thread_id'],'thread-0001')

    def test_human_activity_and_rebinding_do_not_reassign_historical_actor(self):
        import human_events
        import task_discovery
        root=self.project('a',2)
        task_discovery.bind_identity(root,'builder','old-owner')
        with mock.patch.object(collab,'now_iso',return_value='2027-01-01T00:00:00Z'):
            human_events.record_human_event(root,'thread-0000',{
                'event_id':'original', 'actor_type':'human','author':'builder','body':'Original request',
                'created_at':'2025-01-01T00:00:00Z','recipients':['lead'],
                'provenance':{'kind':'manual','supplied_authorized':True,'original_text':True}})
        task_discovery.bind_identity(root,'builder','new-owner',replace=True)
        result=self.page()
        self.assertEqual(self.threads(result)[0]['thread_id'],'thread-0000')
        self.assertNotIn('body',json.dumps(result))
        project=ui_server.project_shell({'root':str(root)})
        project['tools']={'builder':'codex'}
        records=ui_pages._catalog([project])
        people=ui_pages._people([project],records)
        old=next(person for person in people if person['identity']=='old-owner')
        self.assertEqual(old['tools'],[]);self.assertEqual(old['turns'],0)
        project['identity_error']='corrupt binding'
        self.assertEqual(ui_pages._people([project],records),[])

    def test_badges_only_hydrate_selected_page_and_overview_recent_is_global(self):
        root=self.project('a',25)
        for i in range(25):
            directory=root/f'.ai-collab/threads/thread-{i:04d}/signals'
            directory.mkdir();(directory/'to-lead.json').write_text('{}')
        with mock.patch.object(collab,'inspect_inbox',return_value={'results':[{'decision':'NEW','message':{'from':'builder'}}]}) as inbox:
            result=self.page()
        self.assertEqual(inbox.call_count,20)
        self.assertTrue(all(t['signals'][0]['decision']=='NEW' for t in self.threads(result)))
        directory=root/'.ai-collab/threads/thread-0000/turns';directory.mkdir()
        for i in range(15):
            metadata=dict(round=i,author='lead',kind='PROPOSAL',status='OPEN',created_at=f'2027-01-{i+1:02d}T00:00:00Z')
            (directory/f'round-{i:04d}-from-lead-proposal.md').write_text(collab.META_START+json.dumps(metadata)+collab.META_END+'\nbody')
        with mock.patch.object(collab,'inspect_inbox',return_value={'results':[]}):
            recent=self.page('overview')['overview']['recent']
        self.assertEqual(len(recent),12)
        self.assertEqual([r['round'] for r in recent],list(range(14,2,-1)))

    def test_validation_and_empty(self):
        ui_server.save_registry(self.registry,{'projects':[]})
        self.assertEqual(self.page()['page']['total'],0)
        for args in ({'page':0},{'page':-1},{'view':'unknown'},{'q':'x'*201}):
            with self.assertRaises(collab.CollaborationError): self.page(**args)
        with self.assertRaises(collab.CollaborationError): self.page('project',project_id='unregistered')

class PageReadConcurrencyTests(unittest.TestCase):
    def test_duplicate_work_shared_and_saturation_recovers_without_stale_replay(self):
        import threading
        import time
        reads=ui_pages.PageReads(seconds=1)
        gate=threading.Event(); started=threading.Event(); values=[]; calls=[]
        def work():
            calls.append(1); started.set(); gate.wait(1); return {'fresh':len(calls)}
        first=threading.Thread(target=lambda:values.append(reads.read('same',work)))
        duplicate=threading.Thread(target=lambda:values.append(reads.read('same',work)))
        first.start();self.assertTrue(started.wait(1));duplicate.start()
        other=threading.Thread(target=lambda:reads.read('other',lambda:gate.wait(1)))
        other.start()
        deadline=time.monotonic()+1
        while len(reads.active)<2 and time.monotonic()<deadline: time.sleep(.001)
        with self.assertRaises(ui_pages.PageReadError) as raised: reads.read('third',work)
        self.assertEqual(raised.exception.status,503)
        gate.set();first.join();duplicate.join();other.join()
        self.assertEqual(calls,[1]);self.assertEqual(values,[{'fresh':1},{'fresh':1}])
        self.assertEqual(reads.read('same',work),{'fresh':2})
        self.assertFalse(reads.active)

    def test_timeout_retains_actual_capacity_until_work_returns(self):
        import threading
        import time
        reads=ui_pages.PageReads(capacity=1,seconds=.03)
        gate=threading.Event(); started=threading.Event(); failures=[]
        def work(): started.set();gate.wait(1);return {}
        def owner():
            try: reads.read('slow',work)
            except Exception as exc: failures.append(exc)
        thread=threading.Thread(target=owner);thread.start();self.assertTrue(started.wait(1))
        with self.assertRaises(ui_pages.PageReadError) as wait: reads.read('slow',work)
        self.assertEqual(wait.exception.status,504)
        with self.assertRaises(ui_pages.PageReadError) as full: reads.read('different',work)
        self.assertEqual(full.exception.status,503)
        gate.set();thread.join()
        self.assertTrue(failures)
        self.assertEqual(reads.read('new',lambda:{'ok':True}),{'ok':True})

    def test_http_disconnected_client_does_not_duplicate_work(self):
        import socket
        import threading
        import time
        from urllib.request import urlopen
        server=ui_server.ThreadingHTTPServer(('127.0.0.1',0),ui_server.CollaborationHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        gate=threading.Event();started=threading.Event();results=[];calls=[]
        def work(**kwargs):
            calls.append(1);started.set();gate.wait(2)
            return {'projects':[],'collaborators':[],'overview':{}}
        port=server.server_address[1]
        try:
            with mock.patch.object(ui_pages,'READS',ui_pages.PageReads(seconds=3)), mock.patch.object(ui_pages,'page_data',side_effect=work):
                connection=socket.create_connection(('127.0.0.1',port))
                connection.sendall(b'GET /api/page?view=threads HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                self.assertTrue(started.wait(1));connection.close()
                def duplicate():
                    with urlopen(f'http://127.0.0.1:{port}/api/page?view=threads',timeout=3) as response: results.append(response.status)
                thread=threading.Thread(target=duplicate);thread.start();time.sleep(.03)
                self.assertEqual(len(calls),1);gate.set();thread.join()
                self.assertEqual(results,[200]);self.assertEqual(len(calls),1)
        finally:
            gate.set();server.shutdown();server.server_close()

if __name__=='__main__': unittest.main()
