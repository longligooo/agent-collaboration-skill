import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from auto_providers import ProviderError, ProviderRegistry
from auto_providers.claude import resolve_executable, verify_hidden_max_turns, parse_auth_status, configured_models

class ClaudeCompatTests(unittest.TestCase):
    def test_hidden_flag_readonly_probe(self):
        calls=[]
        def command(args):
            calls.append(args)
            if args[0]=='--max-turns':return 1,json.dumps({'loggedIn':False})
            return 1,"error: unknown option '--collab-unsupported-probe-flag'"
        verify_hidden_max_turns(command)
        self.assertEqual(calls[1],('--max-turns','1','auth','status'))
        self.assertTrue(all(args[-2:]==('auth','status') for args in calls))

    def test_hidden_flag_permissive_or_error_rejected(self):
        for result in [(0,'ok'),(1,'crash'),(1,'{"loggedIn":false}')]:
            with self.assertRaisesRegex(ProviderError,'claude_max_turns_unverified'):
                verify_hidden_max_turns(lambda args: result)
        def unsupported(args):
            return (1,"error: unknown option '--collab-unsupported-probe-flag'") if args[0]!='--max-turns' else (1,"error: unknown option '--max-turns'")
        with self.assertRaises(ProviderError):verify_hidden_max_turns(unsupported)

    def test_discovery_hidden_flag_and_runtime_turn_limit(self):
        flags = '--print --output-format --safe-mode --settings --tools --disallowedTools --strict-mcp-config --mcp-config --disable-slash-commands --permission-mode --no-session-persistence --restricted --permission-prompts'
        with tempfile.TemporaryDirectory() as d:
            cli=Path(d)/'claude'
            cli.write_text('#!'+sys.executable+'\nimport sys,json\n'+
                "a=sys.argv[1:]\n"+
                "if a==['--help']:print("+repr(flags)+")\n"+
                "elif a==['--version']:print('2.1.317')\n"+
                "elif a in (['auth','status'],['--safe-mode','--restricted','auth','status'],['--max-turns','1','auth','status']):print(json.dumps({'loggedIn':True}))\n"+
                "elif a==['--collab-unsupported-probe-flag','auth','status']:\n print(\"error: unknown option '--collab-unsupported-probe-flag'\",file=sys.stderr);sys.exit(1)\n"+
                "else:raise RuntimeError('unexpected model operation')\n")
            cli.chmod(0o700)
            registry=ProviderRegistry(path=d)
            record=registry.discover(refresh=True)[1]
            self.assertEqual(record['planning_isolation'],'model_tools_denied')
            launch=registry.build_launch('claude',phase='planning',prompt='never executed',cwd=d)
            self.assertEqual(launch.argv[launch.argv.index('--max-turns')+1],'1')
            for model in ['vendor/model@2026[free]', '模型 名称', 'x;$(echo nope)', ' spaced ']:
                launch=registry.build_launch('claude',model,phase='planning',prompt='never executed',cwd=d)
                self.assertEqual(launch.argv[launch.argv.index('--model')+1],model)
                self.assertEqual(launch.argv.count(model),1)

    def test_auth_status_is_cli_proof_not_key_presence(self):
        self.assertEqual(parse_auth_status(0, '{"loggedIn":true}'), 'authenticated')
        self.assertEqual(parse_auth_status(1, '{"loggedIn":false}'), 'unauthenticated')
        self.assertEqual(parse_auth_status(1, '{"loggedIn":true}'), 'unknown')
        with self.assertRaises(ProviderError):parse_auth_status(0, 'not json')

    def test_local_models_are_configured_not_verified_or_secret(self):
        env={'ANTHROPIC_MODEL':'local-kimi', 'ANTHROPIC_DEFAULT_SONNET_MODEL':'local-kimi',
             'ANTHROPIC_DEFAULT_OPUS_MODEL':'local-opus', 'ANTHROPIC_DEFAULT_HAIKU_MODEL':'--bad',
             'ANTHROPIC_AUTH_TOKEN':'NEVER_EXPOSE', 'ANTHROPIC_BASE_URL':'https://private.example'}
        result=configured_models(env)
        self.assertEqual([x['id'] for x in result],['local-kimi','local-opus'])
        self.assertNotIn('NEVER_EXPOSE',json.dumps(result))
        self.assertNotIn('private.example',json.dumps(result))
        self.assertTrue(all('未验证' in x['label'] for x in result))

    def layout(self, root, local=False):
        prefix=root/'node_modules'/'.bin' if local else root
        prefix.mkdir(parents=True,exist_ok=True)
        shim=prefix/'claude.cmd';shim.write_text('must never run')
        package=root/'node_modules'/'@anthropic-ai'/'claude-code';(package/'bin').mkdir(parents=True)
        (package/'package.json').write_text(json.dumps({'name':'@anthropic-ai/claude-code','version':'2.1.317','bin':{'claude':'bin/claude.exe'}}))
        header=bytearray(70);header[:2]=b'MZ';header[60:64]=(64).to_bytes(4,'little');header[64:]=b'PE\0\0'+(0x8664).to_bytes(2,'little')
        binary=package/'bin'/'claude.exe';binary.write_bytes(header)
        return shim,package,binary

    def test_native_npm_global_and_local(self):
        for local in (False,True):
            with tempfile.TemporaryDirectory() as d:
                shim,_,binary=self.layout(Path(d),local)
                self.assertEqual(resolve_executable(shim,'win32','x64'),binary)

    def test_native_rejects_legacy_and_fake_pe(self):
        with tempfile.TemporaryDirectory() as d:
            shim,package,binary=self.layout(Path(d))
            binary.write_bytes(b'echo malicious')
            with self.assertRaisesRegex(ProviderError,'native_payload_invalid'):resolve_executable(shim,'win32','x64')
            (package/'package.json').write_text(json.dumps({'name':'@anthropic-ai/claude-code','version':'2.1.317','bin':{'claude':'cli.js'}}))
            with self.assertRaisesRegex(ProviderError,'native_windows_install_required'):resolve_executable(shim,'win32','x64')

    def test_native_rejects_wrong_arch_and_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);shim,package,binary=self.layout(root)
            with self.assertRaisesRegex(ProviderError,'native_payload_invalid'):resolve_executable(shim,'win32','arm64')
            external=root/'outside.exe';binary.replace(external);binary.symlink_to(external)
            with self.assertRaisesRegex(ProviderError,'npm_layout_unavailable'):resolve_executable(shim,'win32','x64')

    def test_native_registry_selection(self):
        with tempfile.TemporaryDirectory() as d:
            shim,_,binary=self.layout(Path(d))
            with patch('auto_providers.sys.platform','win32'),patch('platform.machine',return_value='x64'):
                self.assertEqual(ProviderRegistry._resolve_executable('claude',shim),binary)

if __name__=='__main__':unittest.main()
