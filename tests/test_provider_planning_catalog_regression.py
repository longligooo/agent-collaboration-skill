"""Regression fixtures only; never invoke an installed model or AI CLI."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_providers import parse_result
from auto_providers.claude import configured_models
import test_auto_runtime as runtime_fixture
SCRIPT = runtime_fixture.SCRIPT
FakeProviders = runtime_fixture.FakeProviders

class PlanningRegressionTests(unittest.TestCase):
    setUp = runtime_fixture.RuntimeTests.setUp
    create = runtime_fixture.RuntimeTests.create
    def until(self, predicate, timeout=8):
        # Wait for this fixture job, not concurrent SQLite sidecar inspection.
        self.supervisor.tick()
        for future in list(self.supervisor._futures.values()):
            future.result(timeout=timeout)
        self.assertTrue(predicate(), self.supervisor._errors)

    def test_whole_json_fence_reaches_approval_without_worker(self):
        self.script.write_text(SCRIPT.replace('result=json.dumps(value)', r"result='```json\n'+json.dumps(value)+'\n```'"))
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state'] in ('awaiting_approval','blocked'))
        current=self.store.get_task(task['id'])
        self.assertEqual('awaiting_approval',current['state'])
        self.assertEqual([('planning','claude')],self.providers.calls)
        self.assertFalse((self.root/'.ai-collab/threads').exists())

    def test_invalid_plan_keeps_local_evidence_and_specific_error(self):
        self.script.write_text(SCRIPT.replace('result=json.dumps(value)', "result='not a JSON plan'"))
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        job=self.store.list_jobs(task['id'])[0]
        self.assertEqual('invalid_plan_json',job['result']['error_code'])
        records=list((self.store.directory/'attempts').glob('*.json'))
        self.assertEqual(1,len(records))
        retained=json.loads(records[0].read_text())
        self.assertEqual('not a JSON plan',retained['result']['output'])
        self.assertFalse((self.root/'.ai-collab/threads').exists())

    def test_surrounding_prose_is_not_silently_accepted_as_plan(self):
        self.script.write_text(SCRIPT.replace('result=json.dumps(value)', r"result='Explanation before plan: '+json.dumps(value)"))
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        self.assertEqual('invalid_plan_json',self.store.list_jobs(task['id'])[0]['result']['error_code'])
        self.assertFalse((self.root/'.ai-collab/threads').exists())

    def test_valid_json_still_requires_original_plan_schema(self):
        self.script.write_text(SCRIPT.replace('result=json.dumps(value)', "result=json.dumps({'goal':'incomplete'})"))
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state']=='blocked')
        self.assertNotEqual('awaiting_approval',self.store.get_task(task['id'])['state'])
        self.assertFalse((self.root/'.ai-collab/threads').exists())

    def test_stale_discovery_is_rechecked_before_explicit_job_launch(self):
        class Stale(FakeProviders):
            fresh=False
            def discover(self,refresh=False):
                if refresh:self.fresh=True
                return [{'id':'claude','unavailable_reason':None if self.fresh else 'discovery_stale'}]
            def build_launch(self,*args,**kwargs):
                if not self.fresh:raise RuntimeError('stale catalogue used')
                return super().build_launch(*args,**kwargs)
        providers=Stale(self.script)
        self.providers=providers;self.supervisor.providers=providers
        task=self.create()
        self.until(lambda:self.store.get_task(task['id'])['state'] in ('awaiting_approval','blocked'))
        self.assertTrue(providers.fresh)
        self.assertEqual('awaiting_approval',self.store.get_task(task['id'])['state'])

class ModelPickerRegressionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name);self.config=self.home/'.claude';self.config.mkdir()
        self.env={'HOME':str(self.home),'ANTHROPIC_MODEL':'legacy-default'}
    def write(self,picker):
        (self.config/'settings.json').write_text(json.dumps({'modelPicker':picker,'env':{'ANTHROPIC_AUTH_TOKEN':'PRIVATE-DO-NOT-EXPOSE'}}))
    def test_picker_models_preserve_order_labels_and_opaque_ids(self):
        options=[{'model':'vendor/model[1M]','label':'Model One'},{'model':'model@two','label':'Model Two'}]
        self.write({'replaceBuiltInOptions':True,'options':options})
        result=configured_models(self.env)
        self.assertEqual([x['model'] for x in options],[x['id'] for x in result])
        self.assertTrue(all(result[i]['label'].startswith(options[i]['label']) for i in range(2)))
        self.assertNotIn('PRIVATE-DO-NOT-EXPOSE',json.dumps(result))
    def test_additive_picker_retains_configured_default_without_duplicates(self):
        self.write({'replaceBuiltInOptions':False,'options':[{'model':'other','label':'Other'},{'model':'other','label':'Duplicate'}]})
        result=configured_models(self.env)
        self.assertEqual(['other','legacy-default'],[x['id'] for x in result])
    def test_empty_replacement_picker_does_not_invent_defaults(self):
        self.write({'replaceBuiltInOptions':True,'options':[]})
        self.assertEqual([],configured_models(self.env))
    def test_invalid_picker_fails_closed_without_returning_credentials(self):
        from auto_providers import ProviderError
        for option in [{'model':'--unsafe','label':'bad'},{'model':'valid','label':'line\nfeed'}]:
            self.write({'replaceBuiltInOptions':True,'options':[option]})
            with self.assertRaises(ProviderError) as caught:configured_models(self.env)
            self.assertEqual('invalid_catalog',caught.exception.code)
            self.assertNotIn('PRIVATE-DO-NOT-EXPOSE',str(caught.exception))
    def test_unconfigured_picker_keeps_legacy_behaviour(self):
        self.assertEqual(['legacy-default'],[x['id'] for x in configured_models(self.env)])
    def test_null_structured_field_keeps_plain_cli_result(self):
        self.assertEqual('plain result',parse_result('claude',json.dumps({'result':'plain result','structured_output':None,'is_error':False})))
    def test_claude_structured_result_is_preserved(self):
        plan={'goal':'safe','participants':[]}
        result=parse_result('claude',json.dumps({'result':'','structured_output':plan,'is_error':False}))
        self.assertEqual(plan,json.loads(result))

if __name__=='__main__':unittest.main()
