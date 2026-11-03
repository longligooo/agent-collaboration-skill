"""Synthetic protocol contracts only: never runs Codex or makes a model call."""
import copy
import json
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from auto_providers import ProviderError
from auto_providers.codex import (CodexSession, DISABLED_FEATURES, config_overrides,
    fixed_startup_argv, permission_profile, verify_effective_config, resolve_executable, mcp_overrides, _toml)


def effective(phase, cwd):
    result = {}
    for key, value in config_overrides(phase, cwd).items():
        current = result
        parts = key.split('.')
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = copy.deepcopy(value)
    return result


class Fixture:
    def __init__(self, session):
        self.session = session
        self.sent = []
        self.config = effective(session.phase, session.cwd)
        self.modify = lambda method, result: result

    def answer(self, request):
        self.sent.append(copy.deepcopy(request))
        if 'id' not in request or 'method' not in request:
            return None
        s, method = self.session, request['method']
        if method == 'initialize':
            result = {'userAgent': 'fake-only'}
        elif method == 'config/read':
            result = {'config': self.config}
        elif method == 'experimentalFeature/list':
            result = {'data': [{'name': key, 'enabled': False} for key in DISABLED_FEATURES]
                + [{'name': 'shell_tool', 'enabled': s.phase == 'worker'}], 'nextCursor': None}
        elif method == 'permissionProfile/list':
            result = {'data': [{'id': s.profile, 'allowed': True, 'description': None}], 'nextCursor': None}
        elif method == 'account/read':
            assert request['params'] == {'refreshToken': False}
            result = {'account': {'type': 'chatgpt', 'email': 'PRIVATE'}, 'requiresOpenaiAuth': True}
        elif method == 'model/list':
            result = {'data': [{'id': 'model-a', 'displayName': 'Model A'}], 'nextCursor': None}
        elif method == 'thread/start':
            result = {'thread': {'id': 'thread-1'}, 'activePermissionProfile': {'id': s.profile, 'extends': None},
                'cwd': s.cwd, 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                'model': s.model_id or 'model-a', 'modelProvider': 'openai'}
        elif method == 'turn/start':
            result = {'turn': {'id': 'turn-1', 'status': 'inProgress'}}
        else:
            raise AssertionError(method)
        return {'id': request['id'], 'result': self.modify(method, result)}

    def handshake(self):
        outgoing = self.session.start()
        for _ in range(100):
            if not outgoing:
                return
            replies = []
            for request in outgoing:
                response = self.answer(request)
                if response is not None:
                    replies.extend(self.session.receive(response))
            outgoing = replies
        raise AssertionError('unbounded handshake')

    def event(self, method, **params):
        return self.session.receive({'method': method, 'params': {
            'threadId': 'thread-1', 'turnId': 'turn-1', **params}})

    def finish(self, text='complete'):
        self.event('item/completed', item={'id': 'final', 'type': 'agentMessage', 'text': text, 'phase': 'final_answer'})
        self.event('turn/completed', turn={'id': 'turn-1', 'status': 'completed', 'error': None})


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, phase='planning', **kwargs):
        return CodexSession(phase, 'PRIVATE prompt', self.root, **kwargs)

    def test_fixed_launch_no_prompt_or_permission_bypass(self):
        argv = fixed_startup_argv('/fixed/codex', 'planning', self.root)
        self.assertEqual(argv[:2], ('/fixed/codex', 'app-server'))
        self.assertNotIn('PRIVATE', str(argv))
        self.assertNotIn('--yolo', argv)
        self.assertIn('features.shell_tool=false', argv)
        self.assertIn('features.hooks=false', argv)
        self.assertIn('approval_policy="on-request"', argv)
        # Every JSON-RPC mode gets explicit startup overrides, not config writes.
        self.assertTrue(all(argv[i] == '-c' for i in range(2, len(argv), 2)))

    def test_current_cli_feature_override_shape_and_default_profile(self):
        # CLI 0.166.0-alpha.1 features-list smoke: browser/computer default on;
        # named permission tables fail to load without default_permissions.
        for phase in ('planning', 'worker'):
            values = config_overrides(phase, self.root)
            config = effective(phase, self.root)
            self.assertIn(values['default_permissions'], config['permissions'])
            for feature in ('browser_use', 'computer_use', 'code_mode'):
                self.assertIs(values['features.' + feature], False)
            self.assertNotIn('features.code_mode.enabled', values)
            self.assertIs(values['features.shell_tool'], phase == 'worker')

    def test_external_features_missing_or_enabled_block_before_prompt(self):
        for feature in ('browser_use', 'computer_use', 'code_mode'):
            for missing in (True, False):
                with self.subTest(feature=feature, missing=missing):
                    s = self.session(); f = Fixture(s)
                    def modify(method, result):
                        if method == 'experimentalFeature/list':
                            if missing:
                                result['data'] = [i for i in result['data'] if i['name'] != feature]
                            else:
                                next(i for i in result['data'] if i['name'] == feature)['enabled'] = True
                        return result
                    f.modify = modify
                    f.handshake()
                    self.assertEqual(s.error_code, 'codex_feature_schema_unsupported')
                    self.assertFalse(s.delivered)
                    self.assertFalse(any(r.get('method') == 'thread/start' for r in f.sent))

    def test_profile_only_minimal_and_project_no_inheritance(self):
        p = permission_profile('worker', self.root)
        self.assertEqual(p['filesystem'][':root'], 'deny')
        self.assertEqual(p['filesystem'][':minimal'], 'read')
        for sensitive in ('~/.ssh', '~/.aws', '~/.codex', '~/.claude', '~/.gemini'):
            self.assertEqual(p['filesystem'][sensitive], 'deny')
        self.assertEqual(p['network'], {'enabled': False})
        self.assertNotIn('extends', p)
        access = p['filesystem'][str(self.root.resolve())]
        self.assertEqual(access['.'], 'write')
        for name in ('.ai-collab', '.git', '.codex', '.scratch'):
            self.assertEqual(access[name], 'deny')
        self.assertEqual(permission_profile('planning', self.root)['filesystem'][str(self.root.resolve())]['.'], 'read')

    def test_invalid_inputs(self):
        with self.assertRaisesRegex(ProviderError, 'invalid_phase'):
            self.session('unknown')
        with self.assertRaisesRegex(ProviderError, 'invalid_model'):
            self.session(model_id='--yolo')
        with self.assertRaisesRegex(ProviderError, 'invalid_workspace'):
            CodexSession('worker', 'x', Path(self.root.anchor))

    def test_handshake_and_authoritative_output(self):
        s = self.session(model_id='model-a')
        f = Fixture(s)
        f.handshake()
        self.assertFalse(s.done)
        self.assertTrue(s.delivered)
        methods = [r.get('method') for r in f.sent]
        self.assertEqual(methods, ['initialize', 'initialized', 'config/read', 'experimentalFeature/list',
            'permissionProfile/list', 'account/read', 'model/list', 'thread/start', 'turn/start'])
        for r in f.sent[:-1]:
            self.assertNotIn('PRIVATE prompt', json.dumps(r))
        f.event('item/agentMessage/delta', delta='must not duplicate')
        f.finish('{"plan":"ok"}')
        self.assertTrue(s.done)
        self.assertEqual(s.output, '{"plan":"ok"}')
        self.assertIsNone(s.error_code)
        self.assertNotIn('PRIVATE', json.dumps(s.models))
        self.assertFalse(s.capabilities['actual_account_execution_tested'])

    def test_commentary_does_not_corrupt_final_json(self):
        s = self.session(); f = Fixture(s); f.handshake()
        f.event('item/completed', item={'id': 'commentary', 'type': 'agentMessage',
            'text': 'Let me plan this.', 'phase': 'commentary'})
        f.event('item/completed', item={'id': 'partial', 'type': 'agentMessage',
            'text': 'Partial answer', 'phase': 'partial_answer'})
        f.finish('{"tasks":[]}')
        self.assertEqual(json.loads(s.output), {'tasks': []})

    def test_worker_shell_is_sandbox_scoped_not_approved_escalation(self):
        s = self.session('worker')
        f = Fixture(s)
        f.handshake()
        self.assertTrue(f.config['features']['shell_tool'])
        request = next(r for r in f.sent if r.get('method') == 'thread/start')
        self.assertEqual(request['params']['permissions'], s.profile)
        self.assertNotIn('sandbox', request['params'])
        self.assertEqual(s.capabilities['execution_status'], 'sandboxed_workspace')
        f.finish()
        self.assertIsNone(s.error_code)

    def test_no_model_in_discovery(self):
        s = self.session(discovery_only=True)
        f = Fixture(s)
        f.handshake()
        self.assertTrue(s.done)
        self.assertFalse(s.delivered)
        self.assertEqual(s.models, [{'id': 'model-a', 'label': 'Model A'}])
        self.assertFalse(any(r.get('method') in ('thread/start', 'turn/start') for r in f.sent))

    def test_malformed_nested_start_results_fail_closed(self):
        for method, field, code in (('thread/start', 'thread', 'codex_invalid_thread'),
                                    ('turn/start', 'turn', 'codex_invalid_turn')):
            for bad in (None, [], '', 7, True):
                with self.subTest(method=method, bad=bad):
                    s = self.session(); f = Fixture(s)
                    def modify(received, result):
                        if received == method:
                            result[field] = bad
                        return result
                    f.modify = modify
                    f.handshake()
                    self.assertTrue(s.done)
                    self.assertEqual(s.error_code, code)
                    self.assertFalse(s.delivered)
                    self.assertEqual(s.output, '')
                    if method == 'thread/start':
                        self.assertFalse(any(r.get('method') == 'turn/start' for r in f.sent))

    def test_malformed_nested_notifications_fail_closed(self):
        for method in ('turn/started', 'turn/completed'):
            for bad in (None, [], '', 7, True):
                with self.subTest(method=method, bad=bad):
                    s = self.session(); f = Fixture(s); f.handshake()
                    self.assertEqual(f.event(method, turn=bad), [])
                    self.assertTrue(s.done)
                    self.assertEqual(s.error_code, 'codex_invalid_turn')
                    self.assertEqual(s.output, '')
        for method in ('turn/started', 'turn/completed', 'item/completed', 'unknown'):
            for bad in (None, [], '', 7, True):
                with self.subTest(method=method, params=bad):
                    s = self.session(); f = Fixture(s); f.handshake()
                    self.assertEqual(s.receive({'method': method, 'params': bad}), [])
                    self.assertTrue(s.done)
                    self.assertEqual(s.error_code, 'codex_invalid_response')
                    self.assertEqual(s.output, '')

    def test_policy_mutations_block_before_prompt(self):
        mutations = [
            lambda c: c['features'].update(hooks=True),
            lambda c: c.update(default_permissions='unrestricted'),
            lambda c: c['features'].update(browser_use=True),
            lambda c: c['features'].update(computer_use=True),
            lambda c: c['features'].update(code_mode=True),
            lambda c: c['features'].update(shell_tool=True),
            lambda c: c.update(mcp_servers={'evil': {'command': 'not-run'}}),
            lambda c: c.update(hooks={'SessionStart': [{'command': 'not-run'}]}),
            lambda c: c['shell_environment_policy'].update(set={'KEY': 'SECRET'}),
            lambda c: c['shell_environment_policy'].update(filters={'*': 'include'}),
            lambda c: c.update(browser_use={'origins': {'*': 'allow'}}),
            lambda c: c.update(notify=['not-run']),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                s = self.session()
                f = Fixture(s)
                mutation(f.config)
                f.handshake()
                self.assertTrue(s.done)
                self.assertFalse(s.delivered)
                self.assertIsNotNone(s.error_code)
                self.assertFalse(any(r.get('method') == 'thread/start' for r in f.sent))

    def test_serialized_known_null_defaults_are_semantically_absent(self):
        from auto_providers.codex import (PROFILE_NULL_DEFAULTS,
            FILESYSTEM_NULL_DEFAULTS, NETWORK_NULL_DEFAULTS)
        for phase in ('planning', 'worker'):
            session = self.session(phase)
            fixture = Fixture(session)
            profile = fixture.config['permissions'][session.profile]
            for target, fields in ((profile, PROFILE_NULL_DEFAULTS),
                                   (profile['filesystem'], FILESYSTEM_NULL_DEFAULTS),
                                   (profile['network'], NETWORK_NULL_DEFAULTS)):
                target.update({key: None for key in fields})
            original = copy.deepcopy(fixture.config)
            verify_effective_config(fixture.config, phase, session.cwd)
            self.assertEqual(fixture.config, original)  # no mutation of evidence
            fixture.handshake()
            self.assertIsNone(session.error_code)

    def test_optional_defaults_never_permit_nonnull_or_unknown_fields(self):
        from auto_providers.codex import (PROFILE_NULL_DEFAULTS,
            FILESYSTEM_NULL_DEFAULTS, NETWORK_NULL_DEFAULTS)
        for location, fields in ((None, PROFILE_NULL_DEFAULTS),
                                 ('filesystem', FILESYSTEM_NULL_DEFAULTS),
                                 ('network', NETWORK_NULL_DEFAULTS)):
            for key in fields | {'future_unknown_field'}:
                values = [None, False, 0, '', [], {}, True, 'parent'] if key == 'future_unknown_field' else [False, 0, '', [], {}, True, 'parent']
                for value in values:
                    with self.subTest(location=location, key=key, value=value):
                        session = self.session()
                        fixture = Fixture(session)
                        profile = fixture.config['permissions'][session.profile]
                        (profile if location is None else profile[location])[key] = value
                        fixture.handshake()
                        self.assertEqual(session.error_code, 'codex_effective_config_unverified')
                        self.assertFalse(any(r.get('method') == 'thread/start' for r in fixture.sent))

    def test_security_wire_types_are_strict(self):
        for mutate in (
            lambda config, profile: config['features'].update(hooks=0),
            lambda config, profile: profile['network'].update(enabled=0),
            lambda config, profile: profile['network'].update(enabled=True),
            lambda config, profile: profile['filesystem'].update({'/extra': None}),
            lambda config, profile: profile['filesystem'].update({'/extra': 'write'}),
            lambda config, profile: profile['filesystem'].pop(':root'),
        ):
            session = self.session()
            fixture = Fixture(session)
            mutate(fixture.config, fixture.config['permissions'][session.profile])
            fixture.handshake()
            self.assertEqual(session.error_code, 'codex_effective_config_unverified')
            self.assertFalse(session.delivered)

    def test_profile_broadened_by_merge_rejected(self):
        s = self.session('worker')
        f = Fixture(s)
        f.config['permissions'][s.profile]['filesystem']['/'] = 'write'
        f.handshake()
        self.assertEqual(s.error_code, 'codex_effective_config_unverified')

    def test_missing_feature_fails_not_assumed_supported(self):
        s = self.session()
        f = Fixture(s)
        f.modify = lambda m, r: {**r, 'data': []} if m == 'experimentalFeature/list' else r
        f.handshake()
        self.assertEqual(s.error_code, 'codex_feature_schema_unsupported')
        self.assertFalse(s.delivered)

    def test_profile_denied_or_thread_profile_replaced(self):
        for stage in ('permissionProfile/list', 'thread/start'):
            s = self.session()
            f = Fixture(s)
            def mutate(m, r):
                if m == stage:
                    if m == 'permissionProfile/list':
                        r['data'][0]['allowed'] = False
                    else:
                        r['activePermissionProfile']['id'] = ':danger-full-access'
                return r
            f.modify = mutate
            f.handshake()
            self.assertIsNotNone(s.error_code)
            self.assertFalse(s.delivered)
            self.assertFalse(any(r.get('method') == 'turn/start' for r in f.sent))

    def test_auth_missing_blocks_but_unlisted_model_is_opaque_data(self):
        s=self.session();f=Fixture(s)
        f.modify=lambda m,r:{'account':None,'requiresOpenaiAuth':True} if m=='account/read' else r
        f.handshake();self.assertEqual(s.error_code,'auth_unconfirmed');self.assertFalse(s.delivered)
        s=self.session(model_id='unlisted/provider-model');f=Fixture(s);f.handshake()
        self.assertIsNone(s.error_code);self.assertTrue(s.delivered)
        request=next(r for r in f.sent if r.get('method')=='thread/start')
        self.assertEqual(request['params']['model'],'unlisted/provider-model')
        self.assertIs(request['params']['allowProviderModelFallback'],False)

    def custom_fixture(self, *, discovery=True, model=None, binding=None, keys=('ROUTER_API_KEY',), provider=None):
        session=self.session(discovery_only=discovery, model_id=model,
            credential_env_names=keys, expected_provider_binding=binding)
        fixture=Fixture(session)
        fixture.config.update(model_provider='router', model='vendor/exact:free',
            model_providers={'router': provider or {'base_url':'https://router.example/v1',
                'env_key':'ROUTER_API_KEY', 'wire_api':'responses'}})
        def response(method,result):
            if method=='account/read':return {'account':None,'requiresOpenaiAuth':False}
            if method=='thread/start':result.update(model=session.model_id,modelProvider='router')
            return result
        fixture.modify=response
        return session,fixture

    def test_custom_provider_configured_not_authenticated(self):
        session,fixture=self.custom_fixture()
        fixture.handshake()
        self.assertIsNone(session.error_code)
        self.assertEqual(session.auth_status,'configured')
        self.assertEqual([m['id'] for m in session.models],['vendor/exact:free'])
        self.assertNotIn('model-a',str(session.models))
        self.assertFalse(any(r.get('method')=='thread/start' for r in fixture.sent))

    def test_custom_default_model_preserved_and_no_fallback(self):
        discovery,probe=self.custom_fixture();probe.handshake()
        session,fixture=self.custom_fixture(discovery=False,binding=discovery.provider_binding)
        fixture.handshake()
        self.assertIsNone(session.error_code)
        request=next(r for r in fixture.sent if r.get('method')=='thread/start')
        self.assertEqual(request['params']['model'],'vendor/exact:free')
        self.assertEqual(request['params']['modelProvider'],'router')
        self.assertIs(request['params']['allowProviderModelFallback'],False)
        self.assertTrue(session.delivered)

    def test_custom_account_flag_strict_and_key_required(self):
        for auth in ({'account':None}, {'account':None,'requiresOpenaiAuth':0},
                     {'account':None,'requiresOpenaiAuth':None},
                     {'account':{'type':'apiKey'},'requiresOpenaiAuth':False}):
            session,fixture=self.custom_fixture()
            fixture.modify=lambda method,result:auth if method=='account/read' else result
            fixture.handshake()
            self.assertEqual(session.auth_status,'unknown')
        session,fixture=self.custom_fixture(keys=());fixture.handshake()
        self.assertEqual(session.auth_status,'unknown')

    def test_no_account_does_not_enable_builtin_provider(self):
        session,fixture=self.custom_fixture()
        fixture.config['model_provider']='openai'
        fixture.handshake()
        self.assertEqual(session.auth_status,'unknown')
        self.assertIsNone(session.provider_binding)

    def test_custom_helper_is_only_configured(self):
        session,fixture=self.custom_fixture(keys=(),provider={
            'base_url':'https://router.example/v1','auth':{'command':'/trusted/helper','args':[]}})
        fixture.handshake();self.assertEqual(session.auth_status,'configured')
        session,fixture=self.custom_fixture(keys=(),provider={
            'base_url':'https://router.example/v1','auth':{'command':'','args':[]}})
        fixture.handshake();self.assertEqual(session.auth_status,'unknown')

    def test_custom_model_and_provider_response_must_match_before_turn(self):
        discovery,probe=self.custom_fixture();probe.handshake()
        for field,value in (('model','paid-fallback'),('modelProvider','other'),('model',None)):
            session,fixture=self.custom_fixture(discovery=False,binding=discovery.provider_binding)
            original=fixture.modify
            def response(method,result):
                result=original(method,result)
                if method=='thread/start':result[field]=value
                return result
            fixture.modify=response;fixture.handshake()
            self.assertEqual(session.error_code,'codex_thread_model_unverified')
            self.assertFalse(any(r.get('method')=='turn/start' for r in fixture.sent))

    def test_custom_explicit_model_need_not_appear_in_catalog(self):
        discovery,probe=self.custom_fixture();probe.handshake()
        for model in ('model-a','vendor/other:paid'):
            session,fixture=self.custom_fixture(discovery=False,model=model,binding=discovery.provider_binding)
            fixture.handshake()
            self.assertIsNone(session.error_code)
            self.assertTrue(session.delivered)
            request=next(r for r in fixture.sent if r.get('method')=='thread/start')
            self.assertEqual(request['params']['model'],model)
            self.assertIs(request['params']['allowProviderModelFallback'],False)

    def test_custom_binding_required_and_configuration_change_rejected(self):
        discovery,probe=self.custom_fixture();probe.handshake()
        session,fixture=self.custom_fixture(discovery=False)
        fixture.handshake();self.assertEqual(session.error_code,'codex_provider_configuration_unverified')
        for change in ('endpoint','model','helper','provider'):
            session,fixture=self.custom_fixture(discovery=False,binding=discovery.provider_binding)
            if change=='endpoint':fixture.config['model_providers']['router']['base_url']='https://other.example'
            if change=='model':fixture.config['model']='vendor/other'
            if change=='helper':fixture.config['model_providers']['router']['auth']={'command':'/other/helper'}
            if change=='provider':fixture.config['model_provider']='openai'
            fixture.handshake();self.assertEqual(session.error_code,'codex_provider_configuration_changed')
            self.assertFalse(any(r.get('method')=='thread/start' for r in fixture.sent))

    def test_custom_missing_model_bad_endpoint_and_injection_key_fail_closed(self):
        for change in ('model','endpoint','credential_key'):
            session,fixture=self.custom_fixture(keys=('NODE_OPTIONS',))
            if change=='model':fixture.config['model']=None
            if change=='endpoint':fixture.config['model_providers']['router']['base_url']='https://secret@router.example'
            if change=='credential_key':fixture.config['model_providers']['router']['env_key']='NODE_OPTIONS'
            fixture.handshake()
            self.assertNotEqual(session.auth_status,'configured')
            if change!='credential_key':self.assertIsNotNone(session.error_code)

    def test_telemetry_overrides_are_verified(self):
        for key in ('analytics.enabled','otel.metrics_exporter','otel.exporter','otel.trace_exporter'):
            config=effective('planning',self.root)
            first,last=key.split('.')
            config[first][last]=True if last=='enabled' else 'statsig'
            with self.assertRaisesRegex(ProviderError,'codex_effective_config_unverified'):
                verify_effective_config(config,'planning',self.root)

    def test_mcp_inventory_never_starts_account_model_or_thread(self):
        session=self.session(discovery_only=True,inventory_only=True)
        fixture=Fixture(session)
        fixture.config['mcp_servers']={'one':{'command':'NEVER_EXECUTE'},'a.b':{'enabled':True}}
        fixture.handshake()
        self.assertIsNone(session.error_code)
        self.assertEqual(session.mcp_names,('a.b','one'))
        self.assertEqual([r['method'] for r in fixture.sent if 'id' in r],['initialize','config/read'])
        self.assertFalse(session.delivered)
        with self.assertRaisesRegex(ProviderError,'invalid_inventory_mode'):
            self.session(inventory_only=True)

    def test_mcp_disabled_entries_verified_and_carried_to_thread(self):
        names=('a.b','quoted"name')
        session=self.session(disabled_mcp=names);fixture=Fixture(session)
        fixture.config['mcp_servers']={name:{'command':'NEVER_EXECUTE','enabled':False} for name in names}
        fixture.handshake();self.assertIsNone(session.error_code)
        request=next(r for r in fixture.sent if r.get('method')=='thread/start')
        self.assertEqual(request['params']['config']['mcp_servers'],{n:{'enabled':False} for n in names})
        argv=fixed_startup_argv('/trusted/codex','planning',self.root,disabled_mcp=names)
        import tomllib
        override=next(v for v in argv if v.startswith('mcp_servers='))
        self.assertEqual(tomllib.loads(override)['mcp_servers'],{n:{'enabled':False} for n in names})

    def test_mcp_ignored_overrides_and_new_project_entry_fail_closed(self):
        for value in (True,None,0):
            session=self.session(disabled_mcp=('known',));fixture=Fixture(session)
            fixture.config['mcp_servers']={'known':{'enabled':False},'new':{'enabled':value}}
            fixture.handshake();self.assertEqual(session.error_code,'codex_mcp_enabled')
            self.assertFalse(any(r.get('method')=='thread/start' for r in fixture.sent))

    def test_mcp_inventory_still_rejects_hooks_and_bad_names(self):
        session=self.session(discovery_only=True,inventory_only=True);fixture=Fixture(session)
        fixture.config['hooks']={'bad':'do not execute'};fixture.handshake()
        self.assertEqual(session.error_code,'codex_hooks_configured')
        for names in (('bad\nname',),('bad\x00name',),('',),('x'*257,),('x'*100,)*83):
            with self.assertRaisesRegex(ProviderError,'codex_mcp_inventory_invalid'):
                mcp_overrides(names)

    def test_explicit_opaque_model_echo_mismatch_blocks_before_turn(self):
        for value in ('fallback',None):
            session=self.session(model_id='vendor/模型 name@[free]');fixture=Fixture(session)
            def change(method,result):
                if method=='thread/start':result['model']=value
                return result
            fixture.modify=change;fixture.handshake()
            self.assertEqual(session.error_code,'codex_thread_model_unverified')
            self.assertFalse(any(r.get('method')=='turn/start' for r in fixture.sent))

    def test_paginated_model_catalog(self):
        s = self.session(discovery_only=True)
        f = Fixture(s)
        pages = [0]
        def paginate(m, r):
            if m == 'model/list':
                pages[0] += 1
                if pages[0] == 1:
                    r['nextCursor'] = 'p2'
                else:
                    r['data'][0]['id'] = 'model-b'
            return r
        f.modify = paginate
        f.handshake()
        self.assertEqual(len(s.models), 2)
        self.assertIsNone(s.error_code)

    def test_cursor_loop_is_bounded(self):
        s = self.session()
        f = Fixture(s)
        f.modify = lambda m, r: {'data': [], 'nextCursor': 'repeat'} if m == 'experimentalFeature/list' else r
        f.handshake()
        self.assertEqual(s.error_code, 'invalid_catalog_cursor')

    def test_permission_requests_never_accepted(self):
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                       'item/permissions/requestApproval', 'mcpServer/elicitation/request'):
            s = self.session('worker')
            f = Fixture(s)
            f.handshake()
            outgoing = s.receive({'id': 'server-1', 'method': method, 'params': {
                'threadId': s.thread_id, 'turnId': s.turn_id, 'command': 'SECRET'}})
            self.assertTrue(s.done)
            self.assertEqual(s.error_code, 'approval_needed')
            self.assertNotIn('SECRET', json.dumps(outgoing))
            self.assertNotIn('accept', json.dumps(outgoing))
            self.assertEqual(outgoing[0]['id'], 'server-1')
            if method == 'item/permissions/requestApproval':
                self.assertEqual(outgoing[0]['result']['permissions'], {})

    def test_unknown_requests_fail_closed_even_if_id_collides(self):
        s = self.session()
        initial = s.start()[0]
        outgoing = s.receive({'id': initial['id'], 'method': 'unknown/mutation', 'params': {}})
        self.assertEqual(outgoing[0]['error']['code'], -32601)
        self.assertEqual(s.error_code, 'codex_unknown_server_request')

    def test_eof_or_terminal_without_ack_not_success(self):
        s = self.session()
        s.start(); s.eof()
        self.assertEqual(s.error_code, 'codex_incomplete_turn')
        s = self.session()
        f = Fixture(s); f.handshake()
        s.delivered = False
        f.finish()
        self.assertEqual(s.error_code, 'codex_invalid_turn')

    def test_foreign_turn_and_failed_terminal_rejected(self):
        for bad in ('scope', 'failed'):
            s = self.session(); f = Fixture(s); f.handshake()
            f.event('turn/completed', turn={'id': 'other' if bad == 'scope' else 'turn-1', 'status': 'failed'})
            self.assertTrue(s.done)
            self.assertEqual(s.output, '')
            self.assertIsNotNone(s.error_code)

    def test_configuration_warning_never_ignored(self):
        s = self.session(); s.start()
        s.receive({'method': 'configWarning', 'params': {'summary': 'SECRET'}})
        self.assertEqual(s.error_code, 'codex_configuration_warning')
        self.assertNotIn('SECRET', s.error_code)

    def test_duplicate_start_and_output_limit(self):
        s = self.session(); f = Fixture(s); f.handshake()
        with self.assertRaises(ProviderError):
            s.start()
        f.event('item/completed', item={'id': 'a', 'type': 'agentMessage', 'text': 'x' * (1024 * 1024 + 1)})
        self.assertEqual(s.error_code, 'output_limit')

    def test_real_fake_jsonrpc_process_handshake_and_events(self):
        """Actual Python subprocess; not a real provider, no network or config IO."""
        s = self.session()
        f = Fixture(s)
        answers = {}
        # Build the fixture's response sequence without executing model work.
        outgoing = s.start()
        while outgoing:
            next_messages = []
            for req in outgoing:
                reply = f.answer(req)
                if reply:
                    answers[req['method']] = reply['result']
                    next_messages.extend(s.receive(reply))
            outgoing = next_messages
        script = '''import json,sys
answers=json.loads(sys.argv[1])
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r: continue
 m=r['method']
 if m=='turn/start':
  print(json.dumps({'method':'turn/started','params':{'threadId':'thread-1','turn':{'id':'turn-1'}}}),flush=True)
 print(json.dumps({'id':r['id'],'result':answers[m]}),flush=True)
 if m=='turn/start':
  print(json.dumps({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'final','type':'agentMessage','text':'fake result'}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}}),flush=True)
'''
        proc = subprocess.Popen([sys.executable, '-u', '-c', script, json.dumps(answers)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        q = queue.Queue()
        def reader():
            for line in proc.stdout:
                q.put(line)
        thread = threading.Thread(target=reader, daemon=True); thread.start()
        try:
            s = self.session()
            outgoing = s.start()
            for _ in range(100):
                for message in outgoing:
                    proc.stdin.write(json.dumps(message) + '\n')
                proc.stdin.flush()
                if s.done:
                    break
                outgoing = s.receive(json.loads(q.get(timeout=3)))
            self.assertTrue(s.done)
            self.assertTrue(s.delivered)
            self.assertEqual(s.output, 'fake result')
            self.assertIsNone(s.error_code)
        finally:
            proc.kill(); proc.wait(timeout=3)
            thread.join(timeout=3)
            proc.stdin.close(); proc.stdout.close()



class WindowsCodexResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, *, local=False, arch="x64", nested=False, fallback=False):
        modules = self.root / "node_modules"
        shim = modules / ".bin" / "codex.cmd" if local else self.root / "codex.cmd"
        shim.parent.mkdir(parents=True, exist_ok=True)
        # Poison script content deliberately ignored; execution would fail.
        shim.write_text("DO NOT EXECUTE OR PARSE THIS WRAPPER")
        base = modules / "@openai" / "codex"
        (base / "bin").mkdir(parents=True)
        (base / "bin" / "codex.js").write_text("throw new Error('never execute')")
        tag = "win32-" + arch
        main = {"name": "@openai/codex", "version": "0.164.0", "bin": {"codex": "bin/codex.js"},
                "optionalDependencies": {"@openai/codex-" + tag: "npm:@openai/codex@0.164.0-" + tag}}
        (base / "package.json").write_text(json.dumps(main))
        package = base if fallback else (base / "node_modules" if nested else modules) / "@openai" / ("codex-" + tag)
        if not fallback:
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "@openai/codex",
                "version": "0.164.0-" + tag, "os": ["win32"], "cpu": [arch]}))
        triple = {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}[arch]
        payload = package / "vendor" / triple / "bin" / "codex.exe"
        payload.parent.mkdir(parents=True)
        header = bytearray(64); header[:2] = b"MZ"; header[60:64] = (64).to_bytes(4, "little")
        machine = {"x64": 0x8664, "arm64": 0xaa64}[arch]
        payload.write_bytes(bytes(header) + b"PE\x00\x00" + machine.to_bytes(2, "little"))
        return shim, base, package, payload

    def test_global_hoisted_payload_without_wrapper_execution(self):
        shim, _, _, payload = self.package()
        self.assertEqual(resolve_executable(shim, "win32", "AMD64"), payload.resolve())

    def test_local_nested_arm64_and_legacy_vendor(self):
        shim, _, _, payload = self.package(local=True, nested=True, arch="arm64")
        self.assertEqual(resolve_executable(shim, "win32", "aarch64"), payload.resolve())

    def test_main_vendor_fallback(self):
        shim, _, _, payload = self.package(fallback=True)
        self.assertEqual(resolve_executable(shim, "win32", "x64"), payload.resolve())

    def test_native_and_nonwindows_passthrough(self):
        file = self.root / "codex.exe"; file.write_bytes(b"native fixture")
        self.assertEqual(resolve_executable(file, "win32", "x64"), file.resolve())
        posix = self.root / "codex"; posix.write_text("fixture")
        self.assertEqual(resolve_executable(posix, "linux", "x64"), posix.resolve())

    def test_wrong_package_name_os_arch_version_and_alias_rejected(self):
        shim, base, package, _ = self.package()
        path = package / "package.json"; original = json.loads(path.read_text())
        for key, value in (("name", "@attacker/codex"), ("os", ["linux"]),
                           ("cpu", ["arm64"]), ("version", "0.163.0-win32-x64")):
            path.write_text(json.dumps({**original, key: value}))
            with self.assertRaises(ProviderError):
                resolve_executable(shim, "win32", "x64")
        path.write_text(json.dumps(original))
        main = base / "package.json"; value = json.loads(main.read_text())
        value['optionalDependencies']['@openai/codex-win32-x64'] = 'file:../../attacker'
        main.write_text(json.dumps(value))
        with self.assertRaisesRegex(ProviderError, 'codex_npm_dependency_mismatch'):
            resolve_executable(shim, "win32", "x64")

    def test_wrong_machine_and_script_disguised_as_exe_rejected(self):
        shim, _, _, payload = self.package()
        data = payload.read_bytes()
        for invalid in (b'@echo off', data[:-2] + (0xaa64).to_bytes(2, 'little')):
            payload.write_bytes(invalid)
            with self.assertRaisesRegex(ProviderError, 'codex_native_payload_invalid'):
                resolve_executable(shim, "win32", "x64")

    def test_missing_payload_does_not_execute_or_install(self):
        shim, _, _, payload = self.package(); payload.unlink()
        with self.assertRaisesRegex(ProviderError, 'codex_npm_layout_unavailable'):
            resolve_executable(shim, "win32", "x64")
        self.assertFalse(payload.exists())

    def test_unsupported_arch_and_wrapper(self):
        shim, _, _, _ = self.package()
        with self.assertRaisesRegex(ProviderError, 'codex_windows_arch_unsupported'):
            resolve_executable(shim, "win32", "ia32")
        bad = self.root / 'custom.cmd'; bad.write_text('not executed')
        with self.assertRaisesRegex(ProviderError, 'codex_windows_shim_unsupported'):
            resolve_executable(bad, "win32", "x64")

    def test_payload_symlink_escape_rejected(self):
        shim, _, _, payload = self.package()
        outside = self.root / 'outside.exe'; outside.write_bytes(payload.read_bytes())
        payload.unlink()
        try:
            payload.symlink_to(outside)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(ProviderError, 'codex_npm_path_escape'):
            resolve_executable(shim, "win32", "x64")

    def test_oversized_package_metadata_rejected(self):
        shim, base, _, _ = self.package()
        (base / 'package.json').write_text(' ' * 65537)
        with self.assertRaisesRegex(ProviderError, 'codex_npm_metadata_invalid'):
            resolve_executable(shim, "win32", "x64")


if __name__ == '__main__':
    unittest.main()
