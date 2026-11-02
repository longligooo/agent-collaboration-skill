"""End-to-end fixtures only: no installed AI CLI or model is invoked."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collab
import human_events
import runtime_activity
import task_model
from auto_store import AutoStore
from auto_runtime import Supervisor, plan_paths
from auto_providers import LaunchSpec, ProviderError

SCRIPT = r'''
import json, os, sys, time, subprocess
p=json.load(sys.stdin)
mode=sys.argv[1]
trace=os.path.join(os.getcwd(),'fixture-trace.jsonl')
with open(trace,'a') as f:f.write(json.dumps({'action':p['action'],'messages':p['human_messages'],'participant':p.get('participant_id'),'prior_results':p.get('prior_results',[]),'shared_artifact_snapshots':p.get('shared_artifact_snapshots',[]),'execution_workspace':p.get('execution_workspace')})+'\n')
if mode=='slow':time.sleep(2)
if mode=='hang':time.sleep(60)
if mode=='failure':sys.exit(3)
if mode=='big':print('x'*200000);sys.exit(0)
if mode=='child':
 child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
 with open('child.pid','w') as f:f.write(str(child.pid))
 time.sleep(60)
if p['action']=='propose_plan':
 value={'goal':'Fixture goal','spec':p['required_spec'],'ticket':p['required_ticket'],
 'participants':[p['coordinator'],{'participant_id':'worker','provider_id':'claude'}],
 'tasks':[{'id':'work','participant_id':'worker','instruction':'Fixture implementation','acceptance':['Fixture evidence']}],
 'artifacts':['shared.txt'],'limitations':[]}
 result=json.dumps(value)
else:
 if p['action']=='implement':
  with open('shared.txt','w') as f:f.write('fixture change')
 result='result:'+p['action']
print(json.dumps({'result':result,'is_error':False}))
'''


CODEX_SCRIPT = r'''
import sys,os,json,time
sys.path.insert(0,sys.argv[1])
from test_auto_codex import Fixture,CodexSession
f=Fixture(CodexSession('planning','fixture',os.getcwd()))
mode=sys.argv[2]
for line in sys.stdin:
 r=json.loads(line)
 if 'method' not in r:
  open('denial-flushed','w').write(json.dumps(r));time.sleep(60)
 method=r.get('method')
 if method=='turn/start' and mode=='before_ack':sys.exit(0)
 response=f.answer(r)
 if response:print(json.dumps(response),flush=True)
 if method=='turn/start':
  if mode=='after_ack':sys.exit(0)
  if mode=='denial':
   print(json.dumps({'id':999,'method':'item/commandExecution/requestApproval','params':{}}),flush=True)
   continue
  p=json.loads(r['params']['input'][0]['text'])
  plan={'goal':'Fixture goal','spec':p['required_spec'],'ticket':p['required_ticket'],
   'participants':[p['coordinator'],{'participant_id':'worker','provider_id':'claude'}],
   'tasks':[{'id':'work','participant_id':'worker','instruction':'Fixture implementation','acceptance':['Fixture evidence']}],
   'artifacts':[],'limitations':[]}
  base={'threadId':'thread-1','turnId':'turn-1'}
  print(json.dumps({'method':'item/completed','params':{**base,'item':{'id':'final','type':'agentMessage','phase':'final_answer','text':json.dumps(plan)}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{**base,'turn':{'id':'turn-1','status':'completed','error':None}}}),flush=True)
  time.sleep(60) # terminal event, not exit, completes the job
'''


class FakeProviders:
    def __init__(self, script, mode='ok'):
        self.script, self.mode = script, mode
        self.calls = []
    def discover(self, refresh=False):
        return [{'id':'claude','planning_isolation':'model_tools_denied','installed':True}]
    def build_launch(self, provider_id, model_id=None, *, phase, prompt, cwd):
        self.calls.append((phase, provider_id))
        if self.mode=='unavailable':raise ProviderError('isolation_unverified')
        return LaunchSpec((sys.executable,str(self.script),self.mode),prompt.encode(),Path(cwd),dict(os.environ),timeout=10)


@unittest.skipUnless(os.name == 'posix', 'POSIX process groups required')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'repo';self.root.mkdir()
        self.script=self.base/'fake.py';self.script.write_text(SCRIPT)
        self.store=AutoStore(self.root,'project')
        self.providers=FakeProviders(self.script)
        self.supervisor=Supervisor(lambda:[self.store],self.providers,max_workers=2,job_timeout=5)
        self.addCleanup(self.supervisor.stop)
        self.coordinator={'participant_id':'lead','provider_id':'claude'}
    def create(self):
        return self.store.create_task('Private title','PRIVATE ORIGINAL',self.coordinator,idempotency_key='create')
    def until(self, predicate, timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.supervisor.tick()
            if predicate():return
            time.sleep(.02)
        self.fail('runtime condition timed out: '+str(self.supervisor._errors))
    def planned(self):
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='awaiting_approval' and not self.store.list_jobs(task['id'],states=['claimed']))
        return self.store.get_task(task['id'])
    def approve(self,task):
        return self.store.approve(task['id'],expected_revision=task['revision'],plan_revision=task['plan_revision'],plan_hash=task['plan_hash'],idempotency_key='approve')
    def test_plan_gate_to_worker_and_existing_acceptance(self):
        task=self.planned()
        self.assertEqual([('planning','claude')],self.providers.calls)
        self.assertFalse((self.root/'.ai-collab/threads').exists())
        self.assertFalse((self.root/'.scratch').exists())
        self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        current=self.store.get_task(task['id'])
        self.assertEqual(task['id'],current['thread_id'])
        self.assertEqual('fixture change',(self.root/'shared.txt').read_text())
        self.assertEqual(['planning','worker','coordinator'],[c[0] for c in self.providers.calls])
        self.assertFalse(task_model.task_view(self.root,task['id'])['acceptance']['ready'])
        shared=''.join(p.read_text() for d in (self.root/'.scratch',self.root/'.ai-collab/threads') for p in d.rglob('*') if p.is_file())
        self.assertNotIn('PRIVATE ORIGINAL',shared)
        self.assertNotIn('Private title',shared)
        self.assertNotIn('result:implement',shared)
        captured=human_events.read_human_events(self.root,task['id'])
        self.assertEqual([],captured['errors'])
        self.assertEqual('PRIVATE ORIGINAL',captured['events'][0]['body'])
        attempts=list((self.store.directory/'attempts').glob('*.json'))
        self.assertEqual(3,len(attempts))
        retained=json.loads(attempts[0].read_text())
        self.assertIsInstance(retained['pid'],int)
        self.assertTrue(retained['claim_token'])
        self.assertTrue(retained['owner'])
        self.assertIsNotNone(retained['observed_finished_at'])
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in attempts))
        self.assertFalse(self.supervisor.activity(self.root,task['id'])['active'])
    def test_messages_are_next_turn_and_per_recipient(self):
        task=self.planned();self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        event=self.store.send_message(task['id'],'@all keep original  ',idempotency_key='message')
        self.until(lambda:all(d['state']=='delivered' for d in self.store.messages(task['id'])['events'][0]['delivery']))
        self.until(lambda:not self.store.list_jobs(task['id'],states=['claimed','queued']))
        states=self.store.messages(task['id'])['events'][0]
        self.assertEqual(event['body'],states['body'])
        self.assertEqual({'lead','worker'},{d['recipient'] for d in states['delivery']})
        calls=[json.loads(l) for l in (self.root/'fixture-trace.jsonl').read_text().splitlines()]
        messages=[c for c in calls if c['messages']]
        self.assertEqual({'lead','worker'},{c['participant'] for c in messages})
    def test_bounded_message_pages_eventually_queue_and_bridge_all(self):
        task=self.planned();self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        events=[self.store.send_message(task['id'],'@lead event '+str(i),idempotency_key='message'+str(i)) for i in range(5)]
        with patch('auto_runtime._MESSAGE_PAGE',1), patch('auto_runtime._MESSAGE_BATCHES',2):
            self.until(lambda:all(d['state']=='delivered' for e in self.store.messages(task['id'])['events'] for d in e['delivery']))
            self.until(lambda:len(human_events.read_human_events(self.root,task['id'])['events'])==6)
        self.assertEqual({e['event_id'] for e in events},
                         {e['event_id'] for e in human_events.read_human_events(self.root,task['id'])['events'] if e['event_id'].startswith('human-')})

    def test_message_cursor_restart_rescans_idempotently(self):
        task=self.planned();self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        self.until(lambda:not self.store.list_jobs(task['id'],states=['claimed']))
        for i in range(5):
            self.store.send_message(task['id'],'@lead queued '+str(i),idempotency_key='m'+str(i))
        with patch('auto_runtime._MESSAGE_PAGE',1), patch('auto_runtime._MESSAGE_BATCHES',2):
            self.supervisor._prepare(self.store)
            self.supervisor.stop()
            self.supervisor=Supervisor(lambda:[self.store],self.providers)
            self.addCleanup(self.supervisor.stop)
            self.until(lambda:all(d['state']=='delivered' for e in self.store.messages(task['id'])['events'] for d in e['delivery']))
            self.until(lambda:len(human_events.read_human_events(self.root,task['id'])['events'])==6)
        self.assertEqual(6,len(human_events.read_human_events(self.root,task['id'])['events']))

    def test_finalize_serializes_against_human_revision(self):
        import auto_runtime
        from auto_model import AutoError
        task=self.planned();task=self.approve(task)
        entered=threading.Event();release=threading.Event()
        original=auto_runtime._publish_once
        def delayed(root,relative,content):
            if relative.endswith('/thread.json'):
                entered.set();release.wait(3)
            return original(root,relative,content)
        def revise():
            try:
                self.store.revise(task['id'],'new instruction',expected_revision=task['revision'],idempotency_key='revise')
            except AutoError as exc:return exc.code
        with patch('auto_runtime._publish_once',delayed), concurrent.futures.ThreadPoolExecutor(2) as pool:
            frozen=pool.submit(self.supervisor._finalize,self.store,task)
            self.assertTrue(entered.wait(3))
            changed=pool.submit(revise)
            release.set()
            self.assertEqual(task['id'],frozen.result()['thread_id'])
            self.assertEqual('revision_conflict',changed.result())
        self.assertEqual(task['plan_hash'],collab.load_manifest(self.root,task['id'])['approved_plan_hash'])

    def test_recipient_handoff_does_not_relay_other_output(self):
        task=self.planned();self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        calls=[json.loads(l) for l in (self.root/'fixture-trace.jsonl').read_text().splitlines()]
        summary=next(c for c in calls if c['action']=='summarize_execution')
        self.assertTrue(all(r['result'] is None for r in summary['prior_results'] if r['participant_id']=='worker'))
    def test_unavailable_provider_blocks_without_process(self):
        self.providers.mode='unavailable';task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        self.assertFalse((self.root/'fixture-trace.jsonl').exists())
        self.assertFalse(self.supervisor.activity(self.root,task['id'])['active'])
        self.assertEqual('isolation_unverified',self.store.list_jobs()[0]['result']['error_code'])
    def test_timeout_and_output_limit(self):
        self.providers.mode='hang';self.supervisor.job_timeout=.15;task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        self.assertEqual('process_timeout',self.store.list_jobs()[0]['result']['error_code'])
    def test_output_cap_and_exit_failure(self):
        self.providers.mode='big';self.supervisor.max_output=4096;task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        self.assertEqual('output_limit',self.store.list_jobs()[0]['result']['error_code'])
        self.assertFalse(self.supervisor.activity(self.root,task['id'])['active'])
    def test_runtime_activity_uses_real_process_and_expires(self):
        self.providers.mode='slow';task=self.create()
        self.until(lambda:self.supervisor.activity(self.root,task['id'])['active'])
        activity=self.supervisor.activity(self.root,task['id'])
        self.assertIsInstance(activity['process_pid'],int)
        snapshot=runtime_activity.activity_snapshot([self.root])
        self.assertTrue(any(r['observer']=='automatic-runtime' and r['thread_id']==task['id'] for r in snapshot['runs']))
        self.supervisor.stop()
        snapshot=runtime_activity.activity_snapshot([self.root])
        self.assertFalse(any(r.get('active') for r in snapshot['runs']))

    def test_cancel_terminates_owned_descendant_group(self):
        self.providers.mode='child';task=self.create()
        self.until(lambda:(self.root/'child.pid').exists())
        child=int((self.root/'child.pid').read_text())
        current=self.store.get_task(task['id'])
        self.store.control(task['id'],'cancel',expected_revision=current['revision'],idempotency_key='cancel')
        self.until(lambda:self.store.get_task(task['id'])['state']=='cancelled')
        self.assertFalse(runtime_activity.process_alive(child))
        self.assertFalse(self.supervisor.activity(self.root,task['id'])['active'])
    def test_pause_planning_keeps_result_until_resume(self):
        self.providers.mode='slow';task=self.create()
        self.until(lambda:self.supervisor.activity(self.root,task['id'])['active'])
        current=self.store.get_task(task['id'])
        self.store.control(task['id'],'pause',expected_revision=current['revision'],idempotency_key='pause')
        self.until(lambda:self.store.get_task(task['id'])['state']=='paused')
        current=self.store.get_task(task['id'])
        self.assertIsNone(current['plan'])
        self.store.control(task['id'],'resume',expected_revision=current['revision'],idempotency_key='resume')
        self.until(lambda:self.store.get_task(task['id'])['state']=='awaiting_approval')
        self.assertEqual(1,len(self.providers.calls))
    def test_legacy_and_malformed_locks_never_stolen(self):
        task=self.create();lock=self.root/'.ai-collab/watch.lock';lock.write_text('malformed')
        self.supervisor.tick();self.supervisor.tick()
        self.assertEqual('malformed',lock.read_text())
        self.assertEqual([],self.providers.calls)
        self.assertEqual('checkout_busy',self.supervisor.activity(self.root,task['id'])['reason'])
    def test_legacy_watcher_does_not_steal_dead_automatic_marker(self):
        lock=self.root/'.ai-collab/watch.lock'
        for value in ('{"observer":"automatic-runtime","pid":999999999}',
                      '{"observer":"automatic-runtime","pid":'):
            lock.write_text(value)
            self.assertFalse(collab.watch_lock_is_stale(lock))
            with self.assertRaises(collab.CollaborationError):
                collab.acquire_watch_lock(self.root,False)
            self.assertEqual(value,lock.read_text())
        lock.write_text('{"pid":999999999}')
        self.assertTrue(collab.watch_lock_is_stale(lock))

    def test_restart_quarantines_spawn_before_pid(self):
        task=self.create();job=self.store.claim('dead-owner')
        self.supervisor.tick()
        self.assertEqual('unknown',self.store.get_job(job['id'])['state'])
        self.assertEqual([],self.providers.calls)
        self.assertEqual('process_unknown',self.supervisor.activity(self.root,task['id'])['reason'])
    def test_second_supervisor_cannot_recover_live_owner(self):
        self.providers.mode='slow';task=self.create()
        self.until(lambda:self.supervisor.activity(self.root,task['id'])['active'])
        second=Supervisor(lambda:[self.store],self.providers)
        try:
            second.tick()
            self.assertEqual('claimed',self.store.list_jobs()[0]['state'])
            self.assertEqual('runtime_busy',second.activity(self.root,task['id'])['reason'])
        finally:second.stop()
    def test_paused_queue_does_not_starve_another_root(self):
        first=self.create()
        self.store.control(first['id'],'pause',expected_revision=first['revision'],idempotency_key='pause')
        root2=self.base/'repo2';root2.mkdir();other=AutoStore(root2,'project2')
        second=other.create_task('Other','original',self.coordinator,idempotency_key='create')
        self.supervisor.stores=lambda:[self.store,other]
        self.supervisor.max_workers=1
        self.until(lambda:other.get_task(second['id'])['state']=='awaiting_approval')
        self.assertEqual('paused',self.store.get_task(first['id'])['state'])

    def test_global_bounded_concurrency_across_roots(self):
        root2=self.base/'repo2';root2.mkdir();other=AutoStore(root2,'project2')
        other.create_task('Other','original',self.coordinator,idempotency_key='create')
        self.supervisor.stores=lambda:[self.store,other]
        self.providers.mode='slow';task=self.create()
        self.until(lambda:len(self.supervisor._active)==2)
        self.assertLessEqual(len(self.supervisor._futures),2)
        self.assertEqual(1,len(self.store.list_jobs(states=['claimed'])))
        self.assertEqual(1,len(other.list_jobs(states=['claimed'])))
    def test_shutdown_stops_owned_work_and_allows_clean_restart(self):
        self.providers.mode='hang';task=self.create()
        self.until(lambda:self.supervisor.activity(self.root,task['id'])['active'])
        pid=self.store.list_jobs()[0]['pid'];self.supervisor.stop()
        self.assertFalse(runtime_activity.process_alive(pid))
        self.assertFalse((self.root/'.ai-collab/watch.lock').exists())
        self.assertEqual('blocked',self.store.get_task(task['id'])['state'])

    def codex_transport(self, mode='ok'):
        self.script.write_text(CODEX_SCRIPT)
        def build(provider_id, model_id=None, *, phase, prompt, cwd):
            return LaunchSpec((sys.executable,str(self.script),str(Path(__file__).parent.resolve()),mode),
                prompt.encode(),Path(cwd),dict(os.environ),output_format='codex-app-server',timeout=5,
                transport={'phase':phase,'model_id':model_id})
        self.providers.build_launch=build

    def test_codex_terminal_event_completes_without_process_exit(self):
        self.codex_transport()
        task=self.planned()
        job=self.store.list_jobs(task['id'])[0]
        self.assertEqual('succeeded',job['state'])
        self.assertFalse(runtime_activity.process_alive(job['pid']))
        self.assertEqual('Fixture goal',task['plan']['goal'])

    def test_codex_zero_exit_without_terminal_is_failure(self):
        for mode in ('before_ack','after_ack'):
            with self.subTest(mode=mode):
                self.codex_transport(mode)
                task=self.store.create_task('fixture','instruction',self.coordinator,idempotency_key=mode)
                self.store.send_message(task['id'],'private delivery fixture',idempotency_key=mode+'-message')
                self.until(lambda:bool(self.store.list_jobs(task['id'],states=['failed'])))
                self.assertNotEqual('awaiting_approval',self.store.get_task(task['id'])['state'])
                delivery=self.store.messages(task['id'])['events'][0]['delivery'][0]
                self.assertEqual('delivered' if mode=='after_ack' else 'failed',delivery['state'])

    def test_codex_denial_flushed_before_termination(self):
        self.codex_transport('denial');task=self.create()
        actual_write=os.write;writes=[]
        def record(fd,data):
            count=actual_write(fd,data)
            writes.append(bytes(data[:count]))
            return count
        with patch('auto_runtime.os.write',side_effect=record):
            self.until(lambda:bool(self.store.list_jobs(task['id'],states=['failed'])))
        responses=[json.loads(data) for data in writes if b'"id":999' in data]
        self.assertEqual(1,len(responses))
        self.assertEqual('decline',responses[0]['result']['decision'])

    def test_internal_reparse_point_rejected(self):
        from auto_runtime import _safe
        from auto_model import AutoError
        original=__import__('auto_process').is_unsafe_link
        with patch('auto_runtime.auto_process.is_unsafe_link',side_effect=lambda path:path.name=='junction' or original(path)):
            with self.assertRaisesRegex(AutoError,'symlink'):
                _safe(self.root,'junction/private')

    def test_windows_creation_token_uses_readonly_handle_and_closes(self):
        import ctypes
        from unittest.mock import Mock
        kernel=Mock()
        kernel.OpenProcess.return_value=123
        def times(handle,created,*others):
            created._obj.dwHighDateTime=2;created._obj.dwLowDateTime=3
            return True
        kernel.GetProcessTimes.side_effect=times
        with patch('runtime_activity.os.name','nt'), patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            self.assertEqual('windows-start:'+str((2<<32)|3),runtime_activity.process_token(7))
        kernel.OpenProcess.assert_called_once_with(0x1000,False,7)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_real_provider_registry_requires_managed_workspace(self):
        from auto_providers import ProviderRegistry
        with self.assertRaisesRegex(ProviderError,'managed_workspace_required'):
            Supervisor(lambda:[self.store],ProviderRegistry(path=''))

    def test_managed_worker_pending_outputs_leave_original_untouched(self):
        from auto_workspace import WorkspaceManager
        source=Path(__file__).resolve().parents[1]
        self.root=self.base/'cloned-project'
        subprocess.run(['git','-c','core.hooksPath='+os.devnull,'clone','-q','--no-hardlinks','--local',str(source),str(self.root)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.store=AutoStore(self.root,'project')
        manager=WorkspaceManager(self.base/'managed')
        self.supervisor.workspace_manager=manager
        self.script.write_text(SCRIPT.replace("trace=os.path.join(os.getcwd(),'fixture-trace.jsonl')", "trace="+repr(str(self.base/'trace.jsonl'))).replace("result='result:'+p['action']", "result='PRIVATE WORKER ECHO' if p['action']=='implement' else 'result:'+p['action']"))
        readme=self.root/'README.md'
        readme.write_bytes(readme.read_bytes()+b'\nSTAGED USER CHANGE\n')
        subprocess.run(['git','-C',str(self.root),'add','--','README.md'],check=True)
        readme.write_bytes(readme.read_bytes()+b'UNSTAGED USER CHANGE\n')
        (self.root/'existing-untracked.txt').write_text('PREEXISTING USER CONTENT')
        original=readme.read_bytes()
        index_before=(self.root/'.git/index').read_bytes()
        before=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'])
        task=self.planned()
        self.assertFalse((manager.home/'project'/task['id']/'checkout').exists())
        self.approve(task)
        self.until(lambda:self.store.get_task(task['id'])['blocked_reason']=='awaiting_verification')
        worker=next(j for j in self.store.list_jobs(task['id']) if j['kind']=='worker')
        self.assertEqual('succeeded',worker['state'])
        workspace=worker['result']['execution_workspace']
        self.assertEqual('managed_snapshot',workspace['kind'])
        self.assertEqual(workspace['snapshot_id'],worker['result']['application']['diff_base'])
        self.assertEqual(original,(Path(workspace['path'])/'README.md').read_bytes())
        self.assertEqual('PREEXISTING USER CONTENT',(Path(workspace['path'])/'existing-untracked.txt').read_text())
        self.assertFalse((Path(workspace['path'])/'.git').exists())
        self.assertEqual('fixture change',(Path(workspace['path'])/'shared.txt').read_text())
        self.assertFalse((self.root/'shared.txt').exists())
        self.assertEqual(original,(self.root/'README.md').read_bytes())
        self.assertEqual(index_before,(self.root/'.git/index').read_bytes())
        self.assertEqual('PREEXISTING USER CONTENT',(self.root/'existing-untracked.txt').read_text())
        self.assertNotIn('STAGED USER CHANGE',worker['result']['application']['tracked_patch'])
        self.assertEqual(before,subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD']))
        self.assertEqual('pending',worker['result']['application']['status'])
        self.assertIn('shared.txt',worker['result']['application']['untracked_files'])
        self.assertFalse(worker['result']['application']['automatic'])
        self.assertFalse(task_model.task_view(self.root,task['id'])['acceptance']['ready'])
        calls=[json.loads(line) for line in (self.base/'trace.jsonl').read_text().splitlines()]
        coordinator=next(call for call in calls if call['action']=='summarize_execution' and call['participant']=='lead')
        self.assertEqual([{'path':'shared.txt','status':'shared','text':'fixture change'}],coordinator['shared_artifact_snapshots'])
        self.assertNotIn('PRIVATE WORKER ECHO',json.dumps(coordinator))
        self.assertEqual('managed_snapshot',coordinator['execution_workspace']['kind'])
        self.assertEqual(workspace['snapshot_id'],coordinator['execution_workspace']['diff_base'])
        summary=next(j for j in self.store.list_jobs(task['id']) if j['kind']=='message' and j['participant_id']=='lead')
        self.assertEqual(workspace['path'],summary['result']['execution_workspace']['path'])
        # A later worker-directed message reuses the task's managed checkout.
        self.store.send_message(task['id'],'@worker next turn',idempotency_key='managed-message')
        self.until(lambda:any(j['kind']=='message' and j['participant_id']=='worker' and j['state']=='succeeded' for j in self.store.list_jobs(task['id'])))
        message=next(j for j in self.store.list_jobs(task['id']) if j['kind']=='message' and j['participant_id']=='worker')
        self.assertEqual(workspace['path'],message['result']['execution_workspace']['path'])

    def test_windows_publication_flushes_file_without_crt_directory_open(self):
        from auto_runtime import _publish_once
        from types import SimpleNamespace
        native=os.open;opened=[]
        def checked(path,*args,**kwargs):
            opened.append(Path(path))
            if Path(path).is_dir():raise AssertionError('Windows CRT directory open')
            return native(path,*args,**kwargs)
        api=SimpleNamespace(**vars(os));api.name='nt';api.open=checked
        with patch('auto_runtime.os',api):
            _publish_once(self.root,'private/evidence.json','fixture')
        self.assertEqual('fixture',(self.root/'private/evidence.json').read_text())
        self.assertEqual(1,len(opened))
        # POSIX directory flush errors must not silently disappear.
        def fail_directory(path,*args,**kwargs):
            if Path(path).is_dir():raise OSError('directory flush unavailable')
            return native(path,*args,**kwargs)
        api.name='posix';api.open=fail_directory
        with patch('auto_runtime.os',api),self.assertRaises(OSError):
            _publish_once(self.root,'private/other.json','fixture')

    def test_shared_artifact_handoff_is_bounded_and_excludes_private_paths(self):
        from auto_runtime import _artifact_snapshots
        (self.root/'shared.txt').write_text('APPROVED SHARED')
        (self.root/'private.txt').write_text('PRIVATE TARGET ECHO')
        (self.root/'alias.txt').symlink_to(self.root/'private.txt')
        (self.root/'large.txt').write_text('x'*16385)
        (self.root/'binary.txt').write_bytes(b'x\0y')
        os.mkfifo(self.root/'fifo.txt')
        snapshots=_artifact_snapshots({'path':str(self.root)},['shared.txt','fifo.txt','alias.txt','../private.txt','.ai-collab/local/automatic/state.sqlite3','large.txt','binary.txt'])
        encoded=json.dumps(snapshots)
        self.assertIn('APPROVED SHARED',encoded)
        self.assertNotIn('PRIVATE TARGET ECHO',encoded)
        self.assertEqual(1,sum(v['status']=='shared' for v in snapshots))
        self.assertLess(len(encoded),2000)

    def test_large_output_and_diff_retained_without_oversized_store_row(self):
        from auto_runtime import _compact_result
        from auto_model import canonical
        task=self.create()
        job=self.store.claim('fixture')
        result={'output':'\0'*300000,'application':{'status':'pending','automatic':False,
            'tracked_patch':'\0'*300000,'untracked_files':['file-'+str(i) for i in range(10000)],
            'changes':[{'path':'file-'+str(i),'before':None,'after':{'sha256':'a'*64,'size':1}} for i in range(10000)]}}
        retained=self.supervisor._attempt(self.store,job,'succeeded',result)
        compact=_compact_result(result,retained)
        self.assertLess(len(canonical(compact).encode()),1024*1024)
        self.assertTrue(compact['output_truncated']);self.assertTrue(compact['application']['patch_truncated'])
        self.assertTrue(compact['application']['untracked_files_truncated'])
        self.assertTrue(compact['application']['changes_truncated'])
        already={'application':{'tracked_patch':'short','patch_truncated':True,'changes':[],'changes_truncated':True}}
        bounded=_compact_result(already,'private-ref')
        self.assertTrue(bounded['application']['patch_truncated'])
        self.assertTrue(bounded['application']['changes_truncated'])
        self.store.finish(job['id'],job['token'],'succeeded',compact)
        saved=self.store.get_job(job['id'])
        self.assertEqual('succeeded',saved['state'])
        self.assertFalse(self.store.list_jobs(states=['unknown']))
        full=json.loads((self.root/retained).read_text())
        self.assertEqual(result,full['result'])
        self.assertEqual(result['output'],self.supervisor._retained_output(self.store,saved))

if __name__=='__main__':unittest.main()
