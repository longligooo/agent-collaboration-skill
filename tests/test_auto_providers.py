import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_providers import ProviderRegistry, ProviderError, parse_result


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def cli(self, name, source):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)
        return path

    def registry(self, **kwargs):
        return ProviderRegistry(path=str(self.root), **kwargs)

    def test_missing_and_cache_only_no_probe(self):
        self.cli('codex', 'raise RuntimeError("must not run")\n')
        records = self.registry().discover()
        self.assertTrue(records[0]['installed'])
        self.assertEqual(records[0]['unavailable_reason'], 'discovery_required')
        self.assertFalse(records[2]['installed'])
        records = ProviderRegistry(path='').discover(refresh=True)
        self.assertTrue(all(r['unavailable_reason'] == 'not_installed' for r in records))

    def codex(self, extra=''):
        self.cli('codex', "import sys,os,json\nsys.path.insert(0," + repr(str(Path(__file__).resolve().parent)) + ")\n" + '''
from test_auto_codex import Fixture, CodexSession
assert sys.argv[1] == 'app-server'
f=Fixture(CodexSession('planning','',os.getcwd(),discovery_only=True))
for line in sys.stdin:
 r=json.loads(line);method=r.get('method')
 response=f.answer(r)
 if response is None:continue
 out=response['result']
 if method=='model/list':
  if not r['params'].get('cursor'):out={'data':[{'id':'model-a','displayName':'Model A'}],'nextCursor':'page2'}
  else:out={'data':[{'id':'model-b','displayName':'Model B'}],'nextCursor':None}
 ''' + extra + '''
 print(json.dumps({'id':r['id'],'result':out}),flush=True)
''')

    def test_codex_pagination_sanitized_and_cache_copy(self):
        self.codex()
        registry = self.registry()
        record = registry.discover(refresh=True)[0]
        self.assertEqual(record['models'], [{'id':'model-a','label':'Model A'},{'id':'model-b','label':'Model B'}])
        self.assertEqual(record['auth_status'], 'authenticated')
        self.assertEqual(record['headless_status'], 'available')
        self.assertNotIn('NEVER_RETURN', json.dumps(record))
        record['models'].clear()
        self.assertEqual(len(registry.discover()[0]['models']), 2)
        registry._checked -= 400
        self.assertEqual(registry.discover()[0]['catalog_status'], 'stale')

    def test_codex_cursor_loop_fail_without_partial_catalog(self):
        self.codex("if method=='model/list':out['nextCursor']='repeat'")
        record = self.registry().discover(refresh=True)[0]
        self.assertEqual(record['probe_error'], 'invalid_catalog_cursor')
        self.assertEqual(record['models'], [])

    def test_codex_bad_json(self):
        self.cli('codex', "import sys\nsys.stdin.readline()\nprint('not json',flush=True)\nsys.stdin.read()\n")
        self.assertEqual(self.registry().discover(refresh=True)[0]['probe_error'], 'invalid_probe_response')

    def test_codex_reject_server_requests(self):
        self.cli('codex', "import json,sys\nsys.stdin.readline()\nprint(json.dumps({'id':99,'method':'account/login/start','params':{}}),flush=True)\nreply=json.loads(sys.stdin.readline())\nassert reply['error']['code']==-32601\n")
        self.assertEqual(self.registry().discover(refresh=True)[0]['probe_error'], 'codex_unknown_server_request')

    def test_timeout_bounded_and_no_output_leak(self):
        self.cli('codex', 'import time\ntime.sleep(10)\n')
        start = time.monotonic()
        record = self.registry(timeout=.1).discover(refresh=True)[0]
        self.assertEqual(record['probe_error'], 'probe_timeout')
        self.assertLess(time.monotonic()-start, 3)

    def test_output_bounded(self):
        self.cli('codex', "import sys\nsys.stdin.readline()\nprint('SECRET'*10000,flush=True)\nsys.stdin.read()\n")
        record = self.registry(max_output=1024).discover(refresh=True)[0]
        self.assertEqual(record['probe_error'], 'output_limit')
        self.assertNotIn('SECRET', json.dumps(record))

    def test_claude_auth_no_invented_models(self):
        self.cli('claude', '''import sys,json
if sys.argv[1:] in (['auth','status'],['--safe-mode','--restricted','auth','status']): print(json.dumps({'loggedIn':True,'email':'PRIVATE','authMethod':'oauth_token'}))
elif sys.argv[1:]==['--help']: print('--print --output-format')
else:raise RuntimeError('not a supported command')
''')
        record = self.registry().discover(refresh=True)[1]
        self.assertEqual(record['auth_status'], 'authenticated')
        self.assertEqual(record['headless_status'], 'available')
        self.assertEqual(record['catalog_status'], 'unknown')
        self.assertEqual(record['catalog_reason'], 'sdk_discovery_not_available')
        self.assertNotIn('PRIVATE', json.dumps(record))

    def test_claude_auth_invalid_remains_unknown(self):
        self.cli('claude', 'print("secret malformed response")\n')
        record = self.registry().discover(refresh=True)[1]
        self.assertEqual(record['auth_status'], 'unknown')
        self.assertEqual(record['probe_error'], 'invalid_auth_response')

    def test_agy_no_gemini_fallback_and_unstructured_catalog(self):
        self.cli('gemini', 'raise RuntimeError("must not execute")\n')
        self.assertFalse(self.registry().discover()[2]['installed'])
        self.cli('agy', '''import sys
if sys.argv[1:]==['--help']:print('-p --output-format')
elif sys.argv[1:]==['models']:print('Model ID | Status\\nexample-model | available')
else:raise RuntimeError('unsupported command')
''')
        record = self.registry().discover(refresh=True)[2]
        self.assertEqual(record['headless_status'], 'available')
        self.assertEqual(record['auth_status'], 'unknown')
        self.assertEqual(record['models'], [])
        self.assertEqual(record['catalog_reason'], 'unstructured_catalog')

    def test_runtime_injection_env_not_inherited(self):
        with patch.dict(os.environ, {'NODE_OPTIONS':'execute-secret','PYTHONPATH':'bad','BASH_ENV':'bad'}):
            env = self.registry()._env()
            self.assertNotIn('NODE_OPTIONS', env)
            self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('BASH_ENV', env)

    def test_credentials_are_provider_scoped(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'a','OPENAI_API_KEY':'o','GEMINI_API_KEY':'g'}):
            registry=self.registry()
            self.assertEqual(registry._env('claude')['ANTHROPIC_API_KEY'], 'a')
            self.assertNotIn('OPENAI_API_KEY', registry._env('claude'))
            self.assertNotIn('GEMINI_API_KEY', registry._env('claude'))
            self.assertNotIn('ANTHROPIC_API_KEY', registry._env('codex'))
            self.assertNotIn('OPENAI_API_KEY', registry._env('agy'))

    def test_environment_change_requires_new_discovery(self):
        self.modern_claude()
        registry = self.registry()
        registry.discover(refresh=True)
        with patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN':'changed-secret'}):
            with self.assertRaisesRegex(ProviderError, 'provider_configuration_changed'):
                registry.build_launch('claude',prompt='synthetic',cwd=self.root)
            registry.discover(refresh=True)
            launch = registry.build_launch('claude',prompt='synthetic',cwd=self.root)
            self.assertEqual(launch.env['ANTHROPIC_AUTH_TOKEN'], 'changed-secret')
            self.assertNotIn('changed-secret', repr(launch))
            self.assertNotIn('changed-secret', json.dumps(registry.discover()))

    def test_execution_fail_closed_and_option_injection_rejected(self):
        registry = self.registry()
        for provider in ('codex','claude','agy'):
            with self.assertRaisesRegex(ProviderError, 'isolation_unverified'):
                registry.build_launch(provider,prompt='Plan',cwd=self.root)
            with self.assertRaisesRegex(ProviderError, 'invalid_model'):
                registry.build_launch(provider,'--yolo',prompt='Plan',cwd=self.root)
        with self.assertRaisesRegex(ProviderError, 'unknown_provider'):
            registry.build_launch('/bin/sh',prompt='Plan',cwd=self.root)

    def modern_claude(self, version='2.1.301', extra_flags=''):
        flags = '--print --output-format --safe-mode --settings --tools --disallowedTools --strict-mcp-config --mcp-config --disable-slash-commands --permission-mode --no-session-persistence --max-turns --restricted --permission-prompts'
        flags += ' ' + extra_flags
        self.cli('claude', "import sys,json\nif sys.argv[1:] in (['auth','status'],['--safe-mode','--restricted','auth','status']): print(json.dumps({'loggedIn':True}))\nelif sys.argv[1:]==['--help']:print("+repr(flags)+")\nelif sys.argv[1:]==['--version']:print("+repr(version)+")\nelse:raise RuntimeError('discovery attempted model call')\n")

    def test_claude_tool_free_planning_and_finite_worker_launch(self):
        self.modern_claude()
        registry = self.registry()
        record = registry.discover(refresh=True)[1]
        self.assertEqual(record['planning_isolation'], 'model_tools_denied')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'SECRET'}):
            registry.discover(refresh=True)
            plan = registry.build_launch('claude',prompt='PRIVATE prompt',cwd=self.root)
        self.assertEqual(plan.stdin, b'PRIVATE prompt')
        self.assertNotIn('PRIVATE', repr(plan))
        self.assertNotIn('SECRET', repr(plan))
        self.assertEqual(plan.argv[plan.argv.index('--tools')+1], '')
        self.assertEqual(plan.argv[plan.argv.index('--disallowedTools')+1], '*')
        self.assertIn('--restricted', plan.argv)
        self.assertIn('--safe-mode', plan.argv)
        self.assertNotIn('--bare', plan.argv)
        self.assertTrue(json.loads(plan.argv[plan.argv.index('--settings')+1])['disableAllHooks'])
        self.assertEqual(plan.argv[plan.argv.index('--max-turns')+1], '1')
        registry.discover(refresh=True)
        worker = registry.build_launch('claude','model-x',phase='worker',prompt='edit',cwd=self.root)
        self.assertEqual(worker.argv[worker.argv.index('--tools')+1], 'Read,Edit,Write,Glob,Grep')
        self.assertIn('Bash', worker.argv)
        self.assertEqual(worker.argv[worker.argv.index('--max-turns')+1], '8')
        self.assertIn('mcp__*', worker.argv)
        self.assertIn('Edit(./.ai-collab/**)', worker.argv)
        self.assertIn('Edit(./.git)', worker.argv)
        self.assertIn('Edit(./.ai-collab)', worker.argv)
        self.assertIn('Edit(./.scratch/automatic-*/**)', worker.argv)
        self.assertEqual(worker.argv[worker.argv.index('--permission-prompts')+1], 'none')
        self.assertNotIn('--dangerously-skip-permissions',worker.argv)
        self.assertEqual(worker.argv[-2:], ('--model','model-x'))

    def test_claude_unknown_version_fails_closed(self):
        for version in ('2.1.300','3.0.0','garbage'):
            self.modern_claude(version)
            registry=self.registry()
            self.assertEqual(registry.discover(refresh=True)[1]['planning_isolation'], 'unverified')
            with self.assertRaisesRegex(ProviderError,'isolation_unverified'):
                registry.build_launch('claude',prompt='plan',cwd=self.root)

    def test_executable_change_and_stale_discovery_block_launch(self):
        self.modern_claude()
        registry=self.registry()
        registry.discover(refresh=True)
        self.cli('claude','print("different executable")\n')
        with self.assertRaisesRegex(ProviderError,'executable_changed'):
            registry.build_launch('claude',prompt='plan',cwd=self.root)
        registry._checked -= 400
        snapshot=registry.discover()[1]
        self.assertEqual(snapshot['unavailable_reason'], 'discovery_stale')
        self.assertEqual(snapshot['capabilities_status'], 'stale')
        with self.assertRaisesRegex(ProviderError,'discovery_stale'):
            registry.build_launch('claude',prompt='plan',cwd=self.root)

    def test_parse_results(self):
        self.assertEqual(parse_result('claude', '{"result":"hello","is_error":false}'), 'hello')
        self.assertEqual(parse_result('agy', b'{"status":"SUCCESS","response":"hello"}'), 'hello')
        for raw in ('[]','{}','{"is_error":true}','not json'):
            with self.assertRaises(ProviderError):parse_result('claude',raw)
        with self.assertRaisesRegex(ProviderError, 'unsupported_result_format'):
            parse_result('codex','{}')

    def test_claude_sandbox_fixed_policy_no_unconditional_shell_grant(self):
        self.modern_claude(extra_flags='--settings')
        self.cli('bwrap','pass');self.cli('socat','pass')
        registry=self.registry()
        with patch('auto_providers.claude.sys.platform','linux'):
            record=registry.discover(refresh=True)[1]
        self.assertEqual('sandbox_required',record['execution_status'])
        self.assertFalse(record['sandbox_startup_tested'])
        original=self.root/'original';original.mkdir()
        managed=self.root/'managed';managed.mkdir();cwd=managed/'checkout';cwd.mkdir()
        with self.assertRaisesRegex(ProviderError,'protected_project_root_required'):
            registry.build_launch('claude',phase='worker',prompt='test',cwd=cwd)
        launch=registry.build_launch('claude',phase='worker',prompt='test',cwd=cwd,protected_roots=(original,managed))
        self.assertEqual('Read,Edit,Write,Glob,Grep,Bash',launch.argv[launch.argv.index('--tools')+1])
        self.assertNotIn('--allowedTools',launch.argv)
        self.assertNotIn('Bash',launch.argv)
        self.assertEqual('acceptEdits',launch.argv[launch.argv.index('--permission-mode')+1])
        config=json.loads(launch.argv[launch.argv.index('--settings')+1]);sandbox=config['sandbox']
        self.assertTrue(sandbox['enabled']);self.assertTrue(sandbox['failIfUnavailable'])
        self.assertFalse(sandbox['allowUnsandboxedCommands']);self.assertEqual([],sandbox['excludedCommands'])
        self.assertFalse(sandbox['filesystem']['disabled'])
        self.assertIn(str(original),sandbox['filesystem']['denyWrite'])
        self.assertIn(str(original),sandbox['filesystem']['denyRead'])
        self.assertNotIn(str(managed),sandbox['filesystem']['denyWrite'])
        self.assertIn(str(cwd/'.git'),sandbox['filesystem']['denyWrite'])
        self.assertEqual(['*'],sandbox['network']['deniedDomains'])
        self.assertTrue(sandbox['network']['strictAllowlist'])
        self.assertEqual('1',launch.env['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'])
        self.assertIn('Edit(./.claude)',launch.argv);self.assertIn('Edit(./.codex/**)',launch.argv)
        plan=registry.build_launch('claude',prompt='plan',cwd=original)
        self.assertEqual('',plan.argv[plan.argv.index('--tools')+1]);self.assertEqual({'disableAllHooks':True},json.loads(plan.argv[plan.argv.index('--settings')+1]))

    def test_claude_native_windows_and_missing_dependencies_file_only(self):
        from auto_providers.claude import sandbox_capability
        with patch('auto_providers.claude.sys.platform','win32'):
            self.assertEqual('claude_sandbox_requires_wsl2',sandbox_capability('', '--settings'))
        with patch('auto_providers.claude.sys.platform','linux'):
            self.assertEqual('claude_sandbox_dependencies_missing',sandbox_capability('', '--settings'))

    def test_probe_unknown_stop_still_closes_owned_resources(self):
        from auto_providers import _Process
        from unittest.mock import Mock
        process=_Process.__new__(_Process)
        process.stop=Mock();process.reader=Mock();process.writer=Mock();process.process=Mock()
        with patch('auto_providers._stop',side_effect=ProviderError('process_unknown')):
            with self.assertRaisesRegex(ProviderError,'process_unknown'):process.close()
        process.reader.join.assert_called_once();process.writer.join.assert_called_once()
        process.process.close.assert_called_once()

    def test_discovery_cleanup_failure_invalidates_partially_verified_record(self):
        self.cli('codex','pass')
        registry=self.registry()
        def failed(executable,cwd,record):
            record.update(planning_isolation='provider_read_only_profile',execution_status='sandboxed_workspace',
                          auth_status='authenticated',catalog_status='fresh')
            raise ProviderError('process_unknown')
        with patch.object(registry,'_codex',side_effect=failed):
            record=registry.discover(refresh=True)[0]
        self.assertEqual('process_unknown',record['unavailable_reason'])
        self.assertEqual('unverified',record['planning_isolation'])
        self.assertEqual('blocked',record['execution_status'])
        self.assertEqual('stale',record['catalog_status'])
        with self.assertRaisesRegex(ProviderError,'isolation_unverified'):
            registry.build_launch('codex',prompt='do not run',cwd=self.root)

    def test_resolved_native_payload_and_wrapper_revalidated_without_shell(self):
        self.codex()
        wrapper=self.root/'codex';payload=self.root/'payload.exe'
        wrapper.rename(payload)
        self.cli('codex',"raise RuntimeError('WRAPPER MUST NOT RUN')")
        registry=self.registry()
        with patch.object(registry,'_resolve_executable',return_value=payload) as resolve:
            record=registry.discover(refresh=True)[0]
            self.assertEqual('authenticated',record['auth_status'])
            launch=registry.build_launch('codex',prompt='fixture',cwd=self.root)
            self.assertEqual(str(payload),launch.argv[0])
            self.assertNotIn('cmd.exe',launch.argv)
            self.assertGreaterEqual(resolve.call_count,2)
            wrapper.write_text(wrapper.read_text()+'\n# changed')
            with self.assertRaisesRegex(ProviderError,'executable_changed'):
                registry.build_launch('codex',prompt='fixture',cwd=self.root)
            registry.discover(refresh=True)
            payload.write_text(payload.read_text()+'\n# changed native')
            with self.assertRaisesRegex(ProviderError,'executable_changed'):
                registry.build_launch('codex',prompt='fixture',cwd=self.root)


if __name__ == '__main__':
    unittest.main()
