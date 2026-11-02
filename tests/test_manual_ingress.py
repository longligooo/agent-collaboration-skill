"""Offline synthetic documented hook structures; no vendor processes or history."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collab
import human_events
import manual_ingress as ingress
import manual_hook_adapters as adapters


class ManualIngressTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        target = self.root / '.ai-collab/threads/feature'
        target.mkdir(parents=True)
        collab.atomic_write_json(target / 'thread.json', {
            'protocol_version': 1, 'thread_id': 'feature', 'topic': 'Manual ingress',
            'objective': 'Ticket 01', 'participants': ['lead', 'worker'], 'coordinator': 'lead',
            'created_at': '2026-10-30T09:00:00+00:00'})

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, vendor='codex', human=False, identifier='binding', session='session-1'):
        return ingress.bind(self.root, identifier, 'feature', 'worker', vendor, session,
                            'test-fixture-version', consent_activity=True, consent_human=human,
                            accept_unverified_version=True)

    def payload(self, vendor='codex', event=None, turn='turn-1'):
        if vendor == 'cursor':
            p = {'conversation_id': 'session-1', 'generation_id': turn,
                 'cursor_version': 'test-fixture-version', 'workspace_roots': [str(self.root)],
                 'hook_event_name': event or 'beforeSubmitPrompt', 'prompt': 'SECRET current input'}
        else:
            p = {'session_id': 'session-1', 'cwd': str(self.root),
                 'hook_event_name': event or 'UserPromptSubmit', 'prompt': 'SECRET current input'}
            if vendor == 'codex':
                p['turn_id'] = turn
        p.update(transcript_path='/DO-NOT-READ/SECRET', tool_input={'secret':'SECRET'},
                 last_assistant_message='SECRET response')
        return p

    def hook(self, payload=None, **kw):
        return ingress.record_hook(self.root, 'binding', payload or self.payload(),
                                   'test-fixture-version', **kw)

    def human(self):
        return {'event_id':'human-one', 'actor_type':'human', 'author':'owner',
                'body':'明确提供的原文', 'created_at':ingress.now_iso(), 'recipients':['worker'],
                'provenance':{'kind':'manual','supplied_authorized':True,'original_text':True}}

    def test_binding_requires_explicit_consent_and_version_review(self):
        for options in ({}, {'consent_activity':True}):
            with self.assertRaises(ValueError):
                ingress.bind(self.root,'binding','feature','worker','codex','session-1','v',**options)
        self.assertFalse(ingress.storage_path(self.root).exists())
        b=self.bind()
        self.assertNotIn('session_id', b)
        with self.assertRaises(ValueError): self.bind(identifier='other')
        with self.assertRaises(ValueError):
            ingress.bind(self.root,'other','feature','stranger','codex','new','v',
                         consent_activity=True,accept_unverified_version=True)

    def test_explicit_identity_captured(self):
        task_dir=self.root / '.ai-collab'
        collab.atomic_write_json(task_dir/'identities.json', {'schema_version':1,'participants':{'worker':'person-one'}})
        self.assertEqual(self.bind()['identity'],'person-one')

    def test_all_vendors_no_content_or_history_capture(self):
        for vendor in adapters.PROFILES:
            with self.subTest(vendor=vendor):
                name='bind-'+vendor
                self.bind(vendor, identifier=name, session='session-1')
                result=ingress.record_hook(self.root,name,self.payload(vendor),'test-fixture-version')
                self.assertFalse(result['observation']['event']['human_original'])
                self.assertNotIn('SECRET',json.dumps(result))
        data=ingress.storage_path(self.root).read_bytes()
        self.assertNotIn(b'SECRET',data)
        self.assertEqual(human_events.read_human_events(self.root,'feature')['events'], [])

    def test_unknown_schema_version_session_project_fail(self):
        self.bind()
        cases=[{**self.payload(),'hook_event_name':'FutureEvent'},
               {**self.payload(),'session_id':'different'},
               {**self.payload(),'cwd':'/different'},
               {**self.payload(),'turn_id':None}]
        for payload in cases:
            with self.assertRaises(ValueError): self.hook(payload)
        with self.assertRaises(ValueError):
            ingress.record_hook(self.root,'binding',self.payload(),'different-version')
        with self.assertRaises(ValueError): adapters.normalize_hook('unknown',self.payload())

    def test_cursor_embedded_version_mismatch(self):
        self.bind('cursor')
        with self.assertRaises(ValueError):
            self.hook({**self.payload('cursor'),'cursor_version':'changed'})

    def test_duplicate_sequence_and_out_of_order(self):
        self.bind(); t=ingress.now_iso()
        first=self.hook(delivery_id='delivery-1',sequence=1,observed_at=t)
        self.assertEqual(first['result'],'RECORDED')
        self.assertEqual(self.hook(delivery_id='delivery-1')['result'],'DUPLICATE')
        with self.assertRaises(ValueError): self.hook(delivery_id='other',sequence=1)
        with self.assertRaises(ValueError): self.hook(self.payload(event='Stop'),delivery_id='delivery-1')
        with self.assertRaises(ValueError): self.hook(observed_at='2000-01-01T00:00:00Z')
        with self.assertRaises(ValueError): self.hook(observed_at='2100-01-01T00:00:00Z')

    def test_hook_lease_is_not_running_and_expires(self):
        self.bind(); result=self.hook()
        at=ingress.timestamp(result['observation']['observed_at'])
        s=ingress.snapshot([self.root],now=at)['observations'][0]
        self.assertTrue(s['evidence_active']); self.assertFalse(s['active'])
        s=ingress.snapshot([self.root],now=at+timedelta(seconds=16))['observations'][0]
        self.assertEqual(s['status'],'unknown'); self.assertFalse(s['evidence_active'])

    def test_stop_cannot_reopen_same_turn_or_old_turn_corrupt_new(self):
        self.bind(); self.hook(); self.hook(self.payload(event='Stop'))
        self.hook(self.payload(event='PostToolUse'))
        self.hook()  # replayed prompt in the same turn cannot reopen it
        self.assertEqual(ingress.snapshot([self.root])['observations'][0]['status'],'ended')
        self.hook(self.payload(turn='turn-2'))
        with self.assertRaises(ValueError): self.hook(self.payload(turn='turn-1'))
        self.hook(self.payload(event='PostToolUse',turn='turn-2'))
        with self.assertRaises(ValueError): self.hook(self.payload(event='Stop',turn='turn-1'))
        self.assertEqual(ingress.snapshot([self.root])['observations'][0]['status'],'recent-event')
        self.hook(self.payload(event='Interrupt',turn='turn-2'))
        self.assertEqual(ingress.snapshot([self.root])['observations'][0]['status'],'ended')

    def test_cursor_terminal_variants(self):
        self.bind('cursor')
        for i,status in enumerate(('completed','aborted','error')):
            self.hook(self.payload('cursor',turn=str(i)))
            self.hook({**self.payload('cursor',event='stop',turn=str(i)), 'status':status})
            self.assertEqual(ingress.snapshot([self.root])['observations'][0]['status'],'ended')

    def test_session_end_requires_rebind(self):
        self.bind(); self.hook(); self.hook(self.payload(event='SessionEnd'))
        with self.assertRaises(ValueError): self.hook(self.payload(turn='new-turn'))

    def test_revocation_closes_both_evidence_kinds(self):
        self.bind(human=True); self.hook(); ingress.self_report(self.root,'binding','working')
        ingress.revoke(self.root,'binding')
        self.assertTrue(all(x['status']=='revoked' for x in ingress.snapshot([self.root])['observations']))
        with self.assertRaises(ValueError): self.hook()
        with self.assertRaises(ValueError): ingress.record_human(self.root,'binding',self.human())
        with self.assertRaises(ValueError): ingress.self_report(self.root,'binding','working')

    def test_self_report_separate_and_expiring(self):
        self.bind(); self.hook()
        sr=ingress.self_report(self.root,'binding','working')
        observations=ingress.snapshot([self.root])['observations']
        self.assertEqual({o['evidence_kind'] for o in observations},{'hook-event-observed','agent-self-reported'})
        self.assertTrue(all(not o['active'] for o in observations))
        later=ingress.timestamp(sr['expires_at'])+timedelta(seconds=1)
        self.assertTrue(all(not o['evidence_active'] for o in ingress.snapshot([self.root],now=later)['observations']))

    def test_human_original_explicit_separate_local_only(self):
        self.bind()
        with self.assertRaises(ValueError): ingress.record_human(self.root,'binding',self.human())
        ingress.revoke(self.root,'binding'); self.bind(human=True,identifier='human-binding')
        p=self.human()
        result=ingress.record_human(self.root,'human-binding',p)
        self.assertEqual(result['result'],'RECORDED')
        self.assertTrue((self.root/'.ai-collab/local/human-events/feature/human-one.json').exists())
        self.assertFalse((self.root/'.ai-collab/threads/feature/human-events').exists())
        with self.assertRaises(ValueError):
            ingress.record_human(self.root,'human-binding',{**p,'recipients':['lead']})
        with self.assertRaises(collab.CollaborationError):
            ingress.record_human(self.root,'human-binding',{**p,'provenance':{'kind':'manual','original_text':False,'supplied_authorized':True}})

    def test_human_text_current_entry_needs_no_json_but_explicit_declarations(self):
        self.bind(human=True)
        with self.assertRaises(ValueError):
            ingress.record_human_text(self.root,'binding',' original ','owner')
        body='  用户明确当前原文。\n 保留空白。 '
        result=ingress.record_human_text(self.root,'binding',body,'owner',
                                        confirm_original=True,confirm_authorized=True)
        self.assertEqual(result['event']['body'],body)
        self.assertEqual(result['event']['provenance']['kind'],'manual')
        self.assertIsNone(result['event']['provenance']['source_message_id'])
        self.assertEqual(result['event']['recipients'],['worker'])

    def test_import_requires_real_matching_source_and_no_fake_ids(self):
        self.bind(human=True); p=self.human()
        p['provenance']={'kind':'authorized-import','original_text':True,'supplied_authorized':True,
                         'source_conversation_id':'other','source_message_id':'source-message','source_author':'owner'}
        with self.assertRaises(ValueError): ingress.record_human(self.root,'binding',p)
        p['provenance']['source_conversation_id']='session-1'
        del p['provenance']['source_message_id']
        with self.assertRaises(collab.CollaborationError): ingress.record_human(self.root,'binding',p)

    def test_read_only_empty_snapshot_and_symlink_guard(self):
        self.assertEqual(ingress.snapshot([self.root])['observations'],[])
        self.assertFalse((self.root/'.ai-collab/local').exists())
        (self.root/'.ai-collab/local').mkdir()
        (self.root/'.ai-collab/local/manual-ingress').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError): self.bind()
        self.assertTrue(ingress.snapshot([self.root])['errors'])

    def test_changed_stable_identity_requires_explicit_rebind(self):
        self.bind(human=True)
        collab.atomic_write_json(self.root/'.ai-collab/identities.json',
                                {'schema_version':1,'participants':{'worker':'another-person'}})
        with self.assertRaises(ValueError): self.hook()
        with self.assertRaises(ValueError): ingress.record_human(self.root,'binding',self.human())
        ingress.revoke(self.root,'binding')

    def test_tampered_long_lease_cannot_keep_stale_observation_active(self):
        self.bind(); result=self.hook()
        with ingress.database(self.root) as con:
            row=con.execute('SELECT data FROM states').fetchone()
            state=json.loads(row[0]); state['expires_at']='2100-01-01T00:00:00Z'
            con.execute('UPDATE states SET data=?',(json.dumps(state),))
        later=ingress.timestamp(result['observation']['observed_at'])+timedelta(seconds=16)
        self.assertFalse(ingress.snapshot([self.root],now=later)['observations'][0]['evidence_active'])

    def test_corrupt_snapshot_never_projects_arbitrary_stored_content(self):
        self.bind(); self.hook()
        with ingress.database(self.root) as con:
            row=con.execute('SELECT data FROM states').fetchone()
            state=json.loads(row[0]); state['prompt']='SECRET injected field'
            con.execute('UPDATE states SET data=?',(json.dumps(state),))
        result=ingress.snapshot([self.root])
        self.assertTrue(result['errors']); self.assertEqual(result['observations'],[])
        self.assertNotIn('SECRET',json.dumps(result))

    def test_project_copy_cannot_import_foreign_binding(self):
        import shutil
        self.bind(); other=self.root/'copied'; other.mkdir()
        shutil.copytree(self.root/'.ai-collab',other/'.ai-collab')
        with self.assertRaises(ValueError): ingress.revoke(other,'binding')
        self.assertTrue(ingress.snapshot([other])['errors'])

    def test_cli_neutral_output_and_no_secret_errors(self):
        self.bind()
        base=[sys.executable,str(SCRIPTS/'manual_ingress.py'),'--root',str(self.root),'hook',
              '--binding-id','binding','--tool-version','test-fixture-version']
        result=subprocess.run(base,input=json.dumps(self.payload()),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(json.loads(result.stdout),{})
        result=subprocess.run(base,input=json.dumps({**self.payload(),'session_id':'SECRET'}),text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0); self.assertNotIn('SECRET',result.stderr)


if __name__=='__main__': unittest.main()
