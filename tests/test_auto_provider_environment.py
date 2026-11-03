import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_providers import ProviderError
from auto_providers.environment import cli_environment


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {'HOME': str(self.home)}

    def claude_settings(self, data):
        path = self.home / '.claude'
        path.mkdir(exist_ok=True)
        (path / 'settings.json').write_text(json.dumps(data))

    def test_only_provider_and_safe_network_environment(self):
        self.env.update(ANTHROPIC_AUTH_TOKEN='secret', ANTHROPIC_BASE_URL='https://gateway.example/api',
                        OPENAI_API_KEY='other', NODE_OPTIONS='inject', BASH_ENV='inject',
                        LD_PRELOAD='inject', PYTHONPATH='inject', NODE_TLS_REJECT_UNAUTHORIZED='0',
                        HTTPS_PROXY='http://proxy.example:80', NODE_EXTRA_CA_CERTS='/ca.pem')
        result = cli_environment('claude', '/bin', self.env)
        self.assertEqual(result['ANTHROPIC_AUTH_TOKEN'], 'secret')
        for key in ('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC','DISABLE_TELEMETRY','DISABLE_ERROR_REPORTING','DISABLE_AUTOUPDATER'):
            self.assertEqual(result[key], '1')
        self.assertEqual(result['HTTPS_PROXY'], 'http://proxy.example:80')
        self.assertEqual(result['NODE_EXTRA_CA_CERTS'], '/ca.pem')
        for k in ('OPENAI_API_KEY','NODE_OPTIONS','BASH_ENV','LD_PRELOAD','PYTHONPATH','NODE_TLS_REJECT_UNAUTHORIZED'):
            self.assertNotIn(k,result)

    def test_trusted_user_settings_allowlist_and_env_precedence(self):
        self.claude_settings({'model':'local/default', 'env':{'ANTHROPIC_AUTH_TOKEN':'stored',
            'ANTHROPIC_BASE_URL':'https://gateway.example/api', 'NODE_OPTIONS':'bad',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL':'local/small'}, 'hooks':{'anything':'bad'},
            'permissions':{'defaultMode':'bypassPermissions'}})
        self.env['ANTHROPIC_AUTH_TOKEN']='process'
        result=cli_environment('claude','/bin',self.env)
        self.assertEqual(result['ANTHROPIC_AUTH_TOKEN'],'process')
        self.assertEqual(result['ANTHROPIC_MODEL'],'local/default')
        self.assertEqual(result['ANTHROPIC_DEFAULT_HAIKU_MODEL'],'local/small')
        self.assertNotIn('NODE_OPTIONS',result)
        self.assertNotIn('hooks',result)

    def test_url_rejection_never_exposes_secret(self):
        for value in ('https://secret@gateway.example', 'https://gateway.example/?token=secret',
                      'https://gateway.example/#secret','http://remote.example','file:///secret',
                      'https://gateway.example/\nsecret','https://gateway.example:invalid'):
            with self.subTest(value=value):
                self.env['ANTHROPIC_BASE_URL']=value
                with self.assertRaises(ProviderError) as error:
                    cli_environment('claude','/bin',self.env)
                self.assertEqual(str(error.exception),'provider_endpoint_invalid')

    def test_loopback_http_and_https_allowed(self):
        for value in ('http://127.0.0.1:8080/v1','http://[::1]:8080','http://localhost:9000','https://gateway.example/api'):
            self.env['ANTHROPIC_BASE_URL']=value
            self.assertEqual(cli_environment('claude','/bin',self.env)['ANTHROPIC_BASE_URL'],value)

    def test_proxy_credentials_rejected(self):
        self.env['HTTPS_PROXY']='http://user:secret@proxy.example'
        with self.assertRaisesRegex(ProviderError,'provider_endpoint_invalid'):
            cli_environment('claude','/bin',self.env)

    def codex_config(self, text):
        path=self.home/'.codex';path.mkdir(exist_ok=True)
        (path/'config.toml').write_text(text)

    def test_active_codex_provider_only(self):
        self.codex_config('model_provider="chosen"\n[model_providers.chosen]\nenv_key="ROUTER_API_KEY"\nbase_url="https://router.example/v1"\n[model_providers.other]\nenv_key="OTHER_API_KEY"\n')
        self.env.update(ROUTER_API_KEY='chosen-secret',OTHER_API_KEY='other-secret')
        result=cli_environment('codex','/bin',self.env)
        self.assertEqual(result['ROUTER_API_KEY'],'chosen-secret')
        self.assertNotIn('OTHER_API_KEY',result)

    def test_active_codex_profile(self):
        self.codex_config('model_provider="other"\nprofile="dev"\n[profiles.dev]\nmodel_provider="chosen"\n[model_providers.chosen]\nenv_key="ROUTER_API_KEY"\n')
        self.env['ROUTER_API_KEY']='value'
        self.assertEqual(cli_environment('codex','/bin',self.env)['ROUTER_API_KEY'],'value')

    def test_codex_injection_env_key_rejected(self):
        for name in ('NODE_OPTIONS','LD_PRELOAD','PATH','BASH_ENV','PYTHONPATH','HOME','evil;TOKEN'):
            self.codex_config('model_provider="custom"\n[model_providers.custom]\nenv_key='+json.dumps(name))
            with self.assertRaisesRegex(ProviderError,'provider_auth_env_unsupported'):
                cli_environment('codex','/bin',self.env)

    def test_malformed_settings_fail_sanitized(self):
        path=self.home/'.claude';path.mkdir();(path/'settings.json').write_text('SECRET-not-json')
        with self.assertRaisesRegex(ProviderError,'^provider_user_config_invalid$'):
            cli_environment('claude','/bin',self.env)

    def test_config_file_change_invalidates_private_signature(self):
        from auto_providers import ProviderRegistry
        self.codex_config('model_provider="custom"\n[model_providers.custom]\nbase_url="https://one.example"\n')
        with patch.dict(os.environ, self.env, clear=True):
            registry=ProviderRegistry(path='/bin')
            before=registry._env_signature('codex')
            self.codex_config('model_provider="custom"\n[model_providers.custom]\nbase_url="https://two.example"\n')
            self.assertNotEqual(before,registry._env_signature('codex'))

    def test_settings_cannot_redirect_config_directory(self):
        self.claude_settings({'env':{'CLAUDE_CONFIG_DIR':'/other/user'}})
        self.assertNotIn('CLAUDE_CONFIG_DIR',cli_environment('claude','/bin',self.env))

    @unittest.skipUnless(hasattr(os,'mkfifo'),'POSIX FIFO test')
    def test_nonregular_config_rejected_without_open(self):
        path=self.home/'.claude';path.mkdir();os.mkfifo(path/'settings.json')
        with self.assertRaisesRegex(ProviderError,'provider_user_config_invalid'):
            cli_environment('claude','/bin',self.env)

    def test_project_config_never_read(self):
        project=self.home/'project';(project/'.claude').mkdir(parents=True)
        (project/'.claude'/'settings.json').write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"project-secret"}}')
        with patch('os.getcwd',return_value=str(project)):
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN',cli_environment('claude','/bin',self.env))

if __name__=='__main__':unittest.main()
