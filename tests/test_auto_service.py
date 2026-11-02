"""Real loopback HTTP + actual SQLite fixtures, never external model calls."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import auto_api
import auto_service
import ui_server


class Providers:
    def __init__(self): self.calls=[]
    def discover(self, refresh=False):
        self.calls.append(refresh)
        return [{'id':'claude','installed':True,'auth_status':'unknown'}]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        self.patch=patch.dict(os.environ,{'COLLAB_UI_HOME':str(Path(self.tmp.name)/'home')});self.patch.start();self.addCleanup(self.patch.stop)
        ui_server.register_project(self.root)
        self.project=ui_server.project_id(str(self.root))
        self.providers=Providers();self.service=auto_service.AutoService(self.providers)
        self.credentials=auto_api.Credentials('t'*32,'c'*32)
        self.server=ui_server.ThreadingHTTPServer(('127.0.0.1',0),ui_server.CollaborationHandler)
        self.server.auto_gateway=auto_api.Gateway(self.credentials,lambda:ui_server.load_registry(ui_server.registry_path()),self.service.dispatch)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.port=self.server.server_address[1]
        self.path=auto_api.PREFIX+'/projects/'+self.project+'/tasks'

    def request(self,method='GET',path=None,body=None,key='test-request-1',headers=None):
        h={'Authorization':'Bearer '+'t'*32,'X-Collab-CSRF':'c'*32,'Origin':f'http://127.0.0.1:{self.port}',
           'Content-Type':'application/json','Idempotency-Key':key}
        h.update(headers or {})
        raw=json.dumps(body).encode() if body is not None else None
        request=Request(f'http://127.0.0.1:{self.port}'+(path or self.path),data=raw,headers=h,method=method)
        try: response=urlopen(request,timeout=5)
        except HTTPError as exc: response=exc
        with response:return response.status,json.loads(response.read())

    def create(self,key='create-task-1'):
        return self.request('POST',body={'title':'Work','instruction':'PRIVATE original','coordinator':{'participant_id':'lead','provider_id':'claude'}},key=key)

    def test_get_and_create_do_not_launch_or_probe(self):
        status, result=self.create();self.assertEqual(status,201)
        task=result['task'];self.assertEqual(task['state'],'planning')
        status, detail=self.request(path=self.path+'/'+task['id']);self.assertEqual(status,200)
        self.assertEqual(detail['task']['runtime'],{'status':'idle','active':False})
        self.assertEqual(len(detail['task']['jobs']),1)
        self.assertEqual(self.providers.calls,[])
        self.assertFalse((self.root/'.ai-collab/threads').exists())
        self.request(path=auto_api.PREFIX+'/providers')
        self.assertEqual(self.providers.calls,[False])

    def test_durable_command_replay_and_conflict(self):
        first=self.create();second=self.create();self.assertEqual(first,second)
        status,result=self.request('POST',body={'title':'Changed','instruction':'text','coordinator':{'participant_id':'lead','provider_id':'claude'}},key='create-task-1')
        self.assertEqual(status,409);self.assertEqual(result['error']['code'],'idempotency_conflict')
        self.assertEqual(len(self.service.store(self.root,self.project).list_jobs()),1)

    def test_exact_approval_http_and_no_early_worker(self):
        _,created=self.create();task=created['task'];store=self.service.store(self.root,self.project)
        plan={'goal':'Do it','ticket':'.scratch/feature/issues/01-task.md','spec':'.scratch/feature/spec.md',
              'participants':[task['coordinator'],{'participant_id':'builder','provider_id':'claude'}],
              'tasks':[{'id':'one','participant_id':'builder','instruction':'Work','acceptance':['verified']}]}
        task=store.submit_plan(task['id'],plan,expected_revision=task['revision'])
        route=self.path+'/'+task['id']+'/approve'
        payload={'expected_revision':task['revision'],'plan_revision':task['plan_revision'],'plan_hash':'0'*64}
        self.assertEqual(self.request('POST',route,payload)[0],409)
        self.assertEqual(self.request('POST',route,{'approved':True})[0],422)
        payload['plan_hash']=task['plan_hash']
        self.assertEqual(self.request('POST',route,payload,key='valid-approval')[0],200)
        self.assertEqual(self.request('POST',route,payload,key='valid-approval')[0],200)
        self.assertEqual(len(store.list_jobs()),1)
        self.assertFalse((self.root/'.ai-collab/threads').exists())

    def test_human_original_local_and_unknown_recipient_rejected(self):
        _,created=self.create();task=created['task'];route=self.path+'/'+task['id']+'/messages'
        original='Please keep this exact. @lead'
        status,event=self.request('POST',route,{'body':original},key='message-first');self.assertEqual(status,202)
        self.assertEqual(event['body'],original);self.assertEqual(event['delivery'][0]['state'],'queued')
        status,event=self.request('POST',route,{'body':'@unknown nope'},key='message-other');self.assertEqual(status,422)
        self.assertFalse((self.root/'.ai-collab/threads').exists())
        status,events=self.request(path=self.path+'/'+task['id']+'/events')
        self.assertNotIn(original,json.dumps(events))

    def test_registration_blocked_and_secrets_not_echoed(self):
        status,_=self.request('POST','/api/register',{'root':'/tmp'});self.assertEqual(status,404)
        status,result=self.request(headers={'Authorization':'Bearer bad'});self.assertEqual(status,401)
        self.assertNotIn('t'*32,json.dumps(result));self.assertNotIn('c'*32,json.dumps(result))

    def test_explicit_discovery_is_idempotent_and_not_model(self):
        route=auto_api.PREFIX+'/providers/refresh'
        self.assertEqual(self.request('POST',route,{},key='refresh-key')[0],200)
        self.assertEqual(self.request('POST',route,{},key='refresh-key')[0],200)
        self.assertEqual(self.providers.calls,[True])

    def test_legacy_api_unchanged_when_opt_out(self):
        del self.server.auto_gateway
        self.assertEqual(self.request(path=auto_api.PREFIX+'/session')[0],404)
        self.assertEqual(self.request(path='/api/health')[0],200)

    def test_manual_activity_is_separate_and_default_empty(self):
        status, activity = self.request(path='/api/activity')
        self.assertEqual(status,200)
        self.assertEqual(activity['manual_observations']['observations'],[])
        import collab
        import manual_ingress
        target=self.root/'.ai-collab/threads/manual';target.mkdir(parents=True)
        collab.atomic_write_json(target/'thread.json',{'protocol_version':1,'thread_id':'manual','topic':'Manual','objective':'Ticket fixture','participants':['worker'],'coordinator':None,'created_at':'2026-11-02T00:00:00Z'})
        manual_ingress.bind(self.root,'binding','manual','worker','claude','private-session','fixture',consent_activity=True,accept_unverified_version=True)
        manual_ingress.self_report(self.root,'binding','working')
        status,activity=self.request(path='/api/activity')
        observations=activity['manual_observations']['observations']
        self.assertEqual(len(observations),2)  # independent hook and self-report channels
        self.assertEqual({o['evidence_kind'] for o in observations},{'agent-self-reported','hook-event-observed'})
        self.assertTrue(all(o['active'] is False for o in observations))
        self.assertNotIn('private-session',json.dumps(activity))
        self.assertEqual(activity['runs'],[])

    def test_provider_output_only_on_authenticated_task_detail(self):
        _,created=self.create();task=created['task'];store=self.service.store(self.root,self.project)
        job=store.claim('fixture')
        store.finish(job['id'],job['token'],'succeeded',{'output':'LOCAL provider echoed private original','output_truncated':True})
        _,detail=self.request(path=self.path+'/'+task['id'])
        self.assertEqual(detail['task']['local_outputs'][0]['body'],'LOCAL provider echoed private original')
        self.assertTrue(detail['task']['local_outputs'][0]['truncated'])
        _,listing=self.request()
        self.assertEqual(listing['tasks'][0]['local_outputs'],[])
        _,snapshot=self.request(path='/api/snapshot')
        self.assertNotIn('LOCAL provider',json.dumps(snapshot))

    def test_auto_server_guards_legacy_read_origins(self):
        self.assertEqual(self.request(path='/api/snapshot',headers={'Host':'evil.example:'+str(self.port)})[0],403)
        self.assertEqual(self.request(path='/api/activity',headers={'Origin':'http://evil.example'})[0],403)

    def test_unbound_manifest_blocks_revision_and_provider_change(self):
        _,created=self.create();task=created['task'];base=self.path+'/'+task['id']
        self.assertEqual(self.request('POST',base+'/participants/lead/provider',{'expected_revision':task['revision'],'provider_id':'claude'},key='change-key')[0],409)
        manifest=self.root/'.ai-collab/threads'/task['id']/'thread.json'
        manifest.parent.mkdir(parents=True);manifest.write_text('{}')
        self.assertEqual(self.request('POST',base+'/revise',{'expected_revision':task['revision'],'instruction':'new'},key='revise-key')[0],409)

    def test_cursor_integer_overflow_is_structured(self):
        _,created=self.create();task=created['task']
        for kind in ('messages','events'):
            status,result=self.request(path=self.path+'/'+task['id']+'/'+kind+'?cursor='+'9'*128)
            self.assertEqual(status,422)
            self.assertEqual(result['error']['code'],'invalid_cursor')

    def test_plan_edit_replay_survives_later_plan_paths(self):
        _,created=self.create();task=created['task'];store=self.service.store(self.root,self.project)
        plan={'goal':'Do it','ticket':'.scratch/feature/issues/01-task.md','spec':'.scratch/feature/spec.md',
              'participants':[task['coordinator'],{'participant_id':'builder','provider_id':'claude'}],
              'tasks':[{'id':'one','participant_id':'builder','instruction':'Work','acceptance':['verified']}]}
        task=store.submit_plan(task['id'],plan,expected_revision=task['revision'])
        payload={'expected_revision':task['revision'],'plan_revision':task['plan_revision'],'plan_hash':task['plan_hash'],'plan':plan}
        route=self.path+'/'+task['id']+'/plan-edit'
        first=self.request('POST',route,payload,key='edit-replay-key')
        self.assertEqual(first[0],200)
        task=store.revise(task['id'],'different',expected_revision=first[1]['task']['revision'],idempotency_key='internal-revise')
        changed=dict(plan,ticket='.scratch/other/issues/01.md',spec='.scratch/other/spec.md')
        store.submit_plan(task['id'],changed,expected_revision=task['revision'])
        self.assertEqual(first,self.request('POST',route,payload,key='edit-replay-key'))

    def test_manifest_guard_inside_mutation_after_service_entry(self):
        _,created=self.create();task=created['task'];store=self.service.store(self.root,self.project)
        original=store.revise
        def race(*args,**kwargs):
            manifest=self.root/'.ai-collab/threads'/task['id']/'thread.json'
            manifest.parent.mkdir(parents=True);manifest.write_text('{}')
            return original(*args,**kwargs)
        with patch.object(store,'revise',side_effect=race):
            status,result=self.request('POST',self.path+'/'+task['id']+'/revise',{'expected_revision':task['revision'],'instruction':'different'},key='race-revise-key')
        self.assertEqual(status,409)
        self.assertEqual(result['error']['code'],'finalization_pending')
        self.assertEqual(store.get_task(task['id'])['instruction'],'PRIVATE original')

    def test_project_capabilities_and_production_launch_guard(self):
        from auto_workspace import WorkspaceManager
        status,result=self.request(path=auto_api.PREFIX+'/projects/'+self.project+'/capabilities')
        self.assertEqual(status,200)
        self.assertFalse(result['execution_workspace']['supported'])
        self.assertEqual(result['execution_workspace']['reason'],'not_git')
        self.service.workspace_manager=WorkspaceManager(Path(self.tmp.name)/'managed')
        status,result=self.create()
        self.assertEqual(status,409)
        self.assertEqual(result['error']['code'],'not_git')
        self.assertEqual(self.service.store(self.root,self.project).list_tasks(),[])

    def test_dirty_project_http_preflight_and_create_preserve_originals(self):
        from auto_workspace import WorkspaceManager
        source=Path(__file__).resolve().parents[1]
        subprocess.run(['git','-c','core.hooksPath='+os.devnull,'clone','--local','--no-hardlinks',str(source),str(self.root)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        tracked=self.root/'README.md';tracked.write_text('user staged content\n')
        subprocess.run(['git','-C',str(self.root),'add','README.md'],check=True)
        index=(self.root/'.git/index').read_bytes()
        tracked.write_text('user staged plus unstaged content\n')
        extra=self.root/'new-source.py';extra.write_text('print("user untracked")\n')
        self.service.workspace_manager=WorkspaceManager(Path(self.tmp.name)/'managed')
        with patch('auto_workspace._read',side_effect=AssertionError('preflight must not read content')):
            status,cap=self.request(path=auto_api.PREFIX+'/projects/'+self.project+'/capabilities')
            self.assertEqual(status,200)
            self.assertTrue(cap['execution_workspace']['supported'])
            self.assertFalse(cap['execution_workspace']['requires_clean'])
            self.assertEqual(cap['execution_workspace']['kind'],'managed_snapshot')
            status,created=self.create()
        self.assertEqual(status,201)
        self.assertEqual(created['task']['state'],'planning')
        self.assertEqual(tracked.read_text(),'user staged plus unstaged content\n')
        self.assertEqual(extra.read_text(),'print("user untracked")\n')
        self.assertEqual((self.root/'.git/index').read_bytes(),index)
        self.assertIsNone(self.service.workspace_manager.describe(self.root,self.project,created['task']['id']))
        self.assertEqual(self.providers.calls,[])

    def test_create_replay_does_not_reapply_later_capability_guard(self):
        from auto_workspace import WorkspaceManager
        first=self.create()
        self.service.workspace_manager=WorkspaceManager(Path(self.tmp.name)/'managed')
        self.assertEqual(first,self.create())

    def test_large_local_output_history_is_bounded_recent_window(self):
        _,created=self.create();task=created['task'];store=self.service.store(self.root,self.project)
        for n in range(25):
            job=store.claim('fixture')
            if job is None:
                store.enqueue(task['id'],'lead','planning',{'instruction':'fixture'},idempotency_key='planning-'+str(n))
                job=store.claim('fixture')
            store.finish(job['id'],job['token'],'succeeded',{'output':str(n)+':'+'x'*70000})
        _,detail=self.request(path=self.path+'/'+task['id'])
        outputs=detail['task']['local_outputs']
        self.assertLessEqual(sum(len(x['body'].encode()) for x in outputs),1024*1024)
        self.assertTrue(outputs[-1]['body'].startswith('24:'))
        self.assertTrue(detail['task']['local_outputs_window']['truncated'])
        self.assertTrue(all('result' not in j for j in detail['task']['jobs']))

if __name__=='__main__':unittest.main()
