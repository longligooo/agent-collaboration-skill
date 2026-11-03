import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_service
import auto_api
import ui_server
import collab
from auto_store import AutoStore

class UnifiedStartupTests(unittest.TestCase):
    def test_ui_default_delegates_and_read_only_opt_out(self):
        with patch('auto_service.serve') as automatic:
            ui_server.serve(23451)
            automatic.assert_called_once_with(23451, require_session_resume=True, token_required=True)
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch('ui_server.make_server', return_value=server), patch('auto_service.serve') as automatic:
            ui_server.serve(23451, automatic=False)
            automatic.assert_not_called()
            server.server_close.assert_called_once()

    def test_parser_default_and_opt_out(self):
        self.assertFalse(collab.build_parser().parse_args(['ui']).read_only)
        self.assertTrue(collab.build_parser().parse_args(['ui','--read-only']).read_only)

    def test_previous_tasks_require_explicit_resume_new_tasks_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AutoStore(root, 'project')
            previous = store.create_task('Old', 'Instruction', {'participant_id':'lead','provider_id':'codex'}, idempotency_key='old')
            service = auto_service.AutoService(Mock(), require_session_resume=True)
            service._stores[(str(root), 'project')] = store
            self.assertFalse(service.task_enabled(store, previous['id']))
            self.assertIsNone(store.claim('owner', task_enabled=lambda t:service.task_enabled(store,t)))
            params={'project_id':'project','task_id':previous['id']}
            with self.assertRaises(auto_api.ApiError):
                service.dispatch('resume', params, root, {'expected_revision':previous['revision']+1}, 'stale-resume')
            self.assertFalse(service.task_enabled(store, previous['id']))
            service.dispatch('resume', params, root, {'expected_revision':previous['revision']}, 'valid-resume')
            self.assertTrue(service.task_enabled(store, previous['id']))
            service.dispatch('resume', params, root, {'expected_revision':previous['revision']}, 'valid-resume')
            restarted = auto_service.AutoService(Mock(), require_session_resume=True)
            restarted._stores[(str(root), 'project')] = store
            with self.assertRaises(auto_api.ApiError):
                restarted.dispatch('resume', params, root, {'expected_revision':previous['revision']}, 'valid-resume')
            self.assertFalse(restarted.task_enabled(store, previous['id']))
            self.assertIsNotNone(store.claim('owner', task_enabled=lambda t:service.task_enabled(store,t)))
            task = service.dispatch('create_task', {'project_id':'project'}, root, {'title':'New','instruction':'Build','coordinator':{'participant_id':'lead','provider_id':'codex'}}, 'new-task')['task']
            self.assertTrue(service.task_enabled(store, task['id']))
            reopened = auto_service.AutoService(Mock(), require_session_resume=True)
            reopened._stores[(str(root), 'project')] = store
            reopened.dispatch('create_task', {'project_id':'project'}, root, {'title':'New','instruction':'Build','coordinator':{'participant_id':'lead','provider_id':'codex'}}, 'new-task')
            self.assertFalse(reopened.task_enabled(store, task['id']))

    def test_explicit_auto_entry_preserves_default(self):
        with patch('auto_service.serve') as run:
            auto_service.main(['serve','--port','23999','--max-workers','3'])
            run.assert_called_once_with(23999,3, token_required=True)

class UnifiedIntegrationTests(unittest.TestCase):
    def test_foreground_auth_reuse_modes_conflicts_and_shutdown(self):
        import socket, subprocess, time
        from urllib.request import urlopen
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'; root.mkdir()
            subprocess.run(['git','init','-q',str(root)], check=True)
            home = Path(tmp) / 'home'
            env = dict(os.environ, COLLAB_UI_HOME=str(home))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
            command = [sys.executable, str(collab.SCRIPT_PATH), 'ui', '--root',str(root),'--port',str(port)]
            process = subprocess.Popen(command+['--serve'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    with patch.dict(os.environ, {'COLLAB_UI_HOME':str(home)}): health=ui_server.read_health(port)
                    if health: break
                    if process.poll() is not None: self.fail(process.communicate()[1].decode())
                    time.sleep(.05)
                self.assertTrue(health['automatic_enabled'])
                self.assertIn(str(root),health['projects'])
                with self.assertRaises(HTTPError) as error:
                    urlopen(f'http://127.0.0.1:{port}/api/auto/v1/providers',timeout=2)
                self.assertEqual(error.exception.code,401)
                error.exception.close()
                result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertFalse(json.loads(result.stdout)['started'])
                self.assertEqual(json.loads(result.stdout)['token_source'],'terminal')
                self.assertIsNone(json.loads(result.stdout)['token_log'])
                mismatch=subprocess.run(command+['--read-only'],env=env,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(mismatch.returncode,0)
                other=dict(env,COLLAB_UI_HOME=str(Path(tmp)/'other'))
                mismatch=subprocess.run(command,env=other,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(mismatch.returncode,0)
                occupied=subprocess.run(command+['--serve'],env=env,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(occupied.returncode,0)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                out,err=process.communicate(timeout=15)
            self.assertIsNone(ui_server.read_health(port))
            readonly=subprocess.Popen(command+['--serve','--read-only'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                health=None
                for _ in range(100):
                    health=ui_server.read_health(port)
                    if health:break
                    time.sleep(.05)
                self.assertFalse(health['automatic_enabled'])
            finally:
                readonly.terminate();readonly.communicate(timeout=15)

if __name__ == '__main__': unittest.main()
