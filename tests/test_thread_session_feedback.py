"""HTTP feedback checks; never run Codex or address a real conversation."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_api
import collab
import manual_ingress
import session_feedback
import test_auto_service as service_fixture


class ThreadFeedbackTests(unittest.TestCase):
    setUp = service_fixture.ServiceTests.setUp
    request = service_fixture.ServiceTests.request

    def manifest(self, name):
        directory = self.root / '.ai-collab/threads' / name
        directory.mkdir(parents=True)
        collab.atomic_write_json(directory / 'thread.json', {
            'protocol_version': 1, 'thread_id': name, 'topic': 'Feedback',
            'objective': 'Fixture', 'participants': ['lead', 'worker'],
            'coordinator': 'lead', 'created_at': datetime.now(timezone.utc).isoformat()})

    def bind(self, thread, name, vendor='codex'):
        sid = str(uuid.uuid4())
        manual_ingress.bind(self.root, name, thread, 'worker', vendor, sid, 'fixture',
                            consent_activity=True, accept_unverified_version=True)
        return sid

    def data(self):
        self.manifest('first')
        self.manifest('second')
        sid = self.bind('first', 'worker-one')
        self.bind('first', 'other-vendor', 'claude')
        self.bind('second', 'worker-two')
        path = auto_api.PREFIX + '/projects/' + self.project + '/threads/first/feedback'
        body = {'binding_id': 'worker-one', 'expected_revision': 1, 'body': 'Example review'}
        return path, body, sid

    def test_only_bound_codex_and_exactly_one_dispatch(self):
        path, body, sid = self.data()
        status, targets = self.request(path=path)
        self.assertEqual(200, status)
        self.assertEqual([
            {'binding_id':'worker-one','participant':'worker','vendor':'codex','revision':1},
            {'participant':'lead','vendor':'listener'}], targets['targets'])
        with patch.object(session_feedback.shutil, 'which', return_value=sys.executable), patch.object(session_feedback.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(403, self.request('POST', path, body, headers={'Origin':'http://example.invalid'})[0])
            self.assertEqual(403, self.request('POST', path, body, headers={'X-Collab-CSRF':'invalid'})[0])
            self.assertEqual(422, self.request('POST', path, dict(body, session_id=sid))[0])
            self.assertEqual(409, self.request('POST', path, dict(body, binding_id='worker-two'))[0])
            self.assertEqual(409, self.request('POST', path, dict(body, expected_revision=2))[0])
            run.assert_not_called()
            status, response = self.request('POST', path, body)
            self.assertEqual((202, 'queued'), (status, response['status']))
            self.assertEqual('queued', self.request('POST', path, body)[1]['status'])
            self.assertEqual(409, self.request('POST', path, dict(body, body='Different'))[0])
            run.assert_called_once()
            self.assertEqual(['queue','--thread='+sid,'--message=Example review'], run.call_args.args[0][1:])
            self.assertFalse(run.call_args.kwargs['shell'])

    def test_listener_feedback_uses_real_cli_without_formal_signal_or_wake(self):
        self.manifest('first')
        self.manifest('second')
        path = auto_api.PREFIX + '/projects/' + self.project + '/threads/first/feedback'
        body = {'participant': 'worker', 'body': '反馈 — test notice'}
        def cli(action, agent, *args):
            return subprocess.run(
                [sys.executable, '-B', str(Path(collab.__file__)), action, '--root',
                 str(self.root), '--agent', agent, *args],
                capture_output=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual([{'participant':'lead','vendor':'listener'}, {'participant':'worker','vendor':'listener'}],
                         self.request(path=path)[1]['targets'])
        self.assertEqual(409, self.request('POST', path, dict(body, participant='outsider'))[0])
        self.assertEqual(422, self.request('POST', path, dict(body, binding_id='worker-one'))[0])
        self.assertEqual(403, self.request('POST', path, body, headers={'X-Collab-CSRF':'invalid'})[0])
        for _ in range(2):
            self.assertEqual((202, {'schema_version': 1, 'status':'inbox'}), self.request('POST', path, body))
        self.assertEqual(409, self.request('POST', path, dict(body, body='Different'))[0])
        result = cli('inbox','worker','--format','json')
        self.assertEqual(0, result.returncode, result.stderr)
        items = json.loads(result.stdout)['items']
        self.assertEqual(1,len(items))
        self.assertEqual(('FEEDBACK', body['body']), (items[0]['decision'],items[0]['body']))
        self.assertIn(body['body'], cli('inbox','worker','--format','text').stdout)
        self.assertEqual(1, cli('inbox','lead','--format','json').returncode)
        self.assertEqual(1, cli('inbox','worker','--thread-id','second').returncode)
        self.assertEqual(1, cli('inbox','worker','--format','prompt').returncode)
        self.assertFalse(collab.inbox_report(self.root,'worker')['needs_attention'])
        self.assertFalse((self.root/'.ai-collab/threads/first/signals').exists())
        ack = ('--thread-id','first','--message-id',items[0]['message_id'])
        self.assertNotEqual(0, cli('feedback-ack','lead',*ack).returncode)
        self.assertEqual(0, cli('feedback-ack','worker',*ack).returncode)
        self.assertEqual(202, self.request('POST',path,body)[0])
        self.assertEqual(1, cli('inbox','worker').returncode)

    def test_latest_poll_client_switches_and_expires_without_changing_delivery(self):
        path, _, _ = self.data()
        def poll(client, agent='worker', format='json'):
            return subprocess.run([sys.executable, '-B', str(Path(collab.__file__)), 'inbox',
                '--root', str(self.root), '--agent', agent, '--client', client, '--format', format],
                capture_output=True, encoding='utf-8')
        def worker():
            return next(t for t in self.request(path=path)[1]['targets'] if t['participant']=='worker')
        self.assertNotIn('client', worker())
        initial = worker()
        self.assertEqual(1, poll('codex').returncode)
        self.assertEqual('codex', worker()['client'])
        self.assertEqual(1, poll('cursor').returncode)
        current = worker()
        self.assertEqual('cursor', current['client'])
        self.assertEqual(initial['binding_id'], current['binding_id'])
        self.assertLess(current['client_seen_at'], current['client_expires_at'])
        self.assertEqual(1, poll('claude-code', format='prompt').returncode)
        self.assertEqual('cursor', worker()['client'])
        self.assertNotEqual(0, poll('codex', agent='outside').returncode)
        self.assertEqual(1, poll('third-party').returncode)
        self.assertEqual('third-party', worker()['client'])
        with manual_ingress.database(self.root) as db:
            db.execute("UPDATE participant_clients SET observed_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(initial, worker())

    def test_timeout_replay_is_not_sent_again_and_revoked_binding_stays_revoked(self):
        path, body, _ = self.data()
        with patch.object(session_feedback.shutil, 'which', return_value=sys.executable), patch.object(session_feedback.subprocess, 'run', side_effect=subprocess.TimeoutExpired('codex', 15)) as run:
            self.assertEqual('unknown', self.request('POST', path, body)[1]['status'])
            manual_ingress.revoke(self.root, 'worker-one')
            self.assertEqual([{'participant':'lead','vendor':'listener'}, {'participant':'worker','vendor':'listener'}],
                             self.request(path=path)[1]['targets'])
            self.assertEqual('unknown', self.request('POST', path, body)[1]['status'])
            self.assertEqual(409, self.request('POST', path, body, key='new-attempt')[0])
            run.assert_called_once()
