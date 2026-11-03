"""Opaque model names remain data across persistence, catalogs, and CLI argv."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_model_id import MODEL_ID
from auto_model import AutoError, binding
from auto_providers import _models
from auto_providers.claude import configured_models
from auto_providers.environment import cli_environment


class ModelNameTests(unittest.TestCase):
    def test_opaque_names_preserved_end_to_end(self):
        names = ['vendor/model@2026[free]', '模型 名称（测试）', 'a+b=$value;quoted"name',
                 ' leading and trailing ', 'x' * 1024, 'a' + '\u200d' + 'b']
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / '.claude'
            config.mkdir()
            for name in names:
                with self.subTest(name=name[:40]):
                    self.assertTrue(MODEL_ID.fullmatch(name))
                    data = dict(participant_id='worker', provider_id='claude', model_id=name)
                    self.assertEqual(binding(data)['model_id'], name)
                    self.assertEqual(_models([{'id': name}])[0]['id'], name)
                    (config / 'settings.json').write_text(json.dumps({'model': name}))
                    env = cli_environment('claude', '/bin', {'HOME': home})
                    self.assertEqual(env['ANTHROPIC_MODEL'], name)
                    self.assertEqual(configured_models(env)[0]['id'], name)

    def test_transport_invalid_names_rejected(self):
        for name in ['', '   ', '--model=other', '-x', 'x\x00y', 'x\ny', 'x\ty',
                     'x\x7fy', 'x\x85y', 'x\ud800y', 'x' * 1025]:
            with self.subTest(name=repr(name)):
                self.assertIsNone(MODEL_ID.fullmatch(name))
                with self.assertRaises(AutoError):
                    binding(dict(participant_id='worker', provider_id='claude', model_id=name))

    def test_ui_model_is_editable_and_preserves_exact_value(self):
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('ui/auto-ui.js','utf8').replace('return {configure,render,','return {_providerFields:providerFields,configure,render,');
const box={module:{exports:{}}};vm.runInNewContext(source,box);const ui=box.module.exports;
ui.configure({h:(tag,props,...children)=>({tag,props,children})});
let change;const tree=ui._providerFields('test',{provider_id:'claude',model_id:'vendor/a@b[free]'},v=>change=v);
function flatten(n){return [n,...(n?.children||[]).flat(Infinity).flatMap(x=>typeof x==='object'?flatten(x):[])];}
const nodes=flatten(tree),input=nodes.find(n=>n.tag==='input');
assert.equal(input.props.type,'text');assert.equal(input.props.value,'vendor/a@b[free]');
assert(nodes.some(n=>n.tag==='datalist'&&n.props.id===input.props.list));
const exact=' 模型 @test[free] ';input.props.oninput({target:{value:exact}});assert.equal(change.model_id,exact);
input.props.oninput({target:{value:''}});assert.equal(change.model_id,null);
'''
        subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], check=True)
