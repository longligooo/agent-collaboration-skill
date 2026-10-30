"""Read-only UI model/security regressions for the approved collaboration UI.

The production model is executed directly in Node; fixtures stay in this test
module and never become bundled UI data. Browser interaction QA is separate.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "ui" / "view-model.js"
NODE = shutil.which("node")


DOM_HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm');
class FakeNode {
  constructor(tag='#text',value=''){this.tagName=tag;this.nodeType=tag==='#text'?3:tag==='#fragment'?11:1;this.valueText=value;this.children=[];this.attributes={};this.dataset={};this.listeners={};this.parentNode=null;this.scrollTop=0;this.clientHeight=300;this.scrollHeight=600;}
  set className(value){this.attributes.class=String(value);}get className(){return this.attributes.class||'';}
  get classList(){const n=this;return {contains(c){return n.className.split(/\s+/).includes(c);},toggle(c,force){const a=n.className.split(/\s+/).filter(Boolean),found=a.includes(c),yes=force===undefined?!found:force;n.className=(yes?[...new Set([...a,c])]:a.filter(x=>x!==c)).join(' ');return yes;}};}
  setAttribute(k,v){this.attributes[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);if(k==='open')this.open=true;}
  getAttribute(k){return Object.prototype.hasOwnProperty.call(this.attributes,k)?this.attributes[k]:null;}
  hasAttribute(k){return Object.prototype.hasOwnProperty.call(this.attributes,k);}
  append(...values){for(let value of values){if(value instanceof FakeNode&&value.nodeType===11){this.append(...value.children);continue;}if(!(value instanceof FakeNode))value=new FakeNode('#text',String(value));value.parentNode=this;this.children.push(value);}}
  replaceChildren(...values){this.children=[];this.append(...values);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);this.parentNode=null;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  dispatch(name,event={}){for(const listener of this.listeners[name]||[])listener({target:this,...event});}
  get textContent(){return this.nodeType===3?this.valueText:this.children.map(n=>n.textContent).join('');}
  set textContent(value){this.replaceChildren(new FakeNode('#text',String(value)));}
  querySelectorAll(selector){return walk(this).slice(1).filter(n=>matches(n,selector));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  closest(selector){for(let n=this;n;n=n.parentNode)if(matches(n,selector))return n;return null;}
  focus(){documentMock.activeElement=this;}
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
  toString(){return this.nodeType===3?'[object Text]':'[object HTMLElement]';}
}
function walk(root){return [root,...root.children.flatMap(walk)];}
function matches(node,selector){
  if(node.nodeType!==1)return false;
  const match=selector.match(/^([\w-]+)?(?:#([\w-]+))?((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
  if(!match)throw new Error('Unsupported test selector: '+selector);
  const [,tag,id,classes,attr,value]=match;
  return (!tag||node.tagName===tag)&&(!id||node.getAttribute('id')===id)&&classes.split('.').filter(Boolean).every(c=>node.classList.contains(c))&&(!attr||(value===undefined?node.hasAttribute(attr):node.getAttribute(attr)===value));
}
const root=new FakeNode('div');root.setAttribute('id','app');
const documentMock={hidden:false,activeElement:null,createElement:tag=>new FakeNode(tag),createElementNS:(_,tag)=>new FakeNode(tag),createTextNode:value=>new FakeNode('#text',value),createDocumentFragment:()=>new FakeNode('#fragment'),querySelector:selector=>selector==='#app'?root:root.querySelector(selector),querySelectorAll:selector=>root.querySelectorAll(selector),addEventListener(){}};
const timers=[];
const context=vm.createContext({window:{CollabUI:M,addEventListener(){}},document:documentMock,Node:FakeNode,location:{hash:'#/'},localStorage:{getItem(){return null;},setItem(){}},setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},AbortController,URLSearchParams,fetch(){throw new Error('Unexpected test network request');},assert,walk,root,timers});
let appSource=fs.readFileSync(MODEL_PATH.replace(/view-model\.js$/,'app.js'),'utf8');
appSource=appSource.replace(/\nload\(\);\s*\npollActivity\(\);\s*$/,'');
vm.runInContext(appSource,context);
"""


@unittest.skipUnless(NODE, "Node.js is needed to execute the production UI model")
class UiV04ModelTests(unittest.TestCase):
    def run_node(self, source):
        prelude = (
            '"use strict";\n'
            'const assert = require("node:assert/strict");\n'
            f"const M = require({json.dumps(str(MODEL))});\n"
        )
        result = subprocess.run(
            [NODE, "-"],
            input=prelude + source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout}\nstderr={result.stderr}",
        )
        return result.stdout

    def run_dom(self, source):
        return self.run_node(
            f"const MODEL_PATH={json.dumps(str(MODEL))};\n"
            + DOM_HARNESS
            + f"\nPromise.resolve(vm.runInContext({json.dumps(source)},context)).catch(error=>{{console.error(error);process.exitCode=1;}});\n"
        )

    def test_dom_helper_preserves_false_aria_states_and_html_boolean_semantics(self):
        self.run_dom("""
const button=h('button',{disabled:false,hidden:false,'aria-pressed':false,'aria-expanded':false},'toggle');
assert.equal(button.hasAttribute('disabled'),false);
assert.equal(button.hasAttribute('hidden'),false);
assert.equal(button.getAttribute('aria-pressed'),'false');
assert.equal(button.getAttribute('aria-expanded'),'false');
assert.equal(h('button',{disabled:true}).hasAttribute('disabled'),true);
assert.equal(h('option',{selected:false}).hasAttribute('selected'),false);
assert.equal(h('option',{selected:true}).hasAttribute('selected'),true);
let clicked=0;const action=h('button',{onclick:()=>clicked++});action.dispatch('click');assert.equal(clicked,1);
assert.equal(action.hasAttribute('onclick'),false);
""")

    def test_message_body_and_provenance_render_untrusted_markup_as_text(self):
        self.run_dom("""
const attack='<img src=x onerror=globalThis.PWNED=1><script>globalThis.PWNED=1</script>';
const thread={project_id:'p',thread_id:'t',participants:['agent'],coordinator:'agent',history:[]};
const human={id:'h',type:'human-input',actor_type:'human',identity:'person',author:attack,
 kind:'HUMAN',body:attack+'\\n\\n```html\\n'+attack+'\\n```',created_at:'2026-01-01T00:00:00Z',
 recipient_source:'explicit-human-input',recipients:[{agent:attack,identity:'agent'}],
 provenance:{kind:'manual',supplied_authorized:true,original_text:true,source_conversation_id:attack,source_message_id:attack,source_author:attack}};
const rendered=messageNode(human,thread,0);
assert(rendered.classList.contains('human-message'));
assert(rendered.textContent.includes(attack));
assert.equal(walk(rendered).filter(n=>['img','script','iframe'].includes(n.tagName)).length,0);
assert.equal(globalThis.PWNED,undefined);
for(const node of walk(rendered))for(const key of Object.keys(node.attributes))assert(!/^on/i.test(key));
const absent={...human,provenance:null};
assert(!messageNode(absent,thread,1).classList.contains('human-message'));
""")

    def test_legacy_and_coordinator_message_alignment_stays_thread_scoped(self):
        self.run_dom("""
const plain={project_id:'p',thread_id:'one',participants:['lead'],coordinator:null,turns:[]};
const entry={author:'lead',identity:'lead',kind:'PROPOSAL',summary:'text',created_at:'2026-01-01T00:00:00Z'};
assert(!messageNode(entry,plain,0).classList.contains('own'));
assert(messageNode(entry,{...plain,coordinator:'lead'},0).classList.contains('own'));
assert(!messageNode({...entry,is_coordinator:false},{...plain,coordinator:'lead',history:[]},0).classList.contains('own'));
assert(messageNode({...entry,is_coordinator:true},{...plain,history:[]},0).classList.contains('own'));
""")

    def test_activity_repaint_inserts_nodes_and_stops_all_matching_avatars(self):
        self.run_dom("""
const thread={project_id:'p',project_name:'project',thread_id:'t',topic:'topic',objective:'goal',participants:['agent','peer'],coordinator:null,signals:[],turns:[],history:[]};
snapshot={projects:[{id:'p',name:'project',available:true,threads:[thread]}],collaborators:[],overview:{}};
location.hash='#/threads/p/t';
activity={runs:[{run_id:'run-a',project_id:'p',thread_id:'t',identity:'agent',agent:'agent',status:'running',active:true,expires_at:'2099-01-01T00:00:00Z'},
 {run_id:'run-b',project_id:'p',thread_id:'elsewhere',identity:'peer',agent:'peer',status:'running',active:true,expires_at:'2099-01-01T00:00:00Z'}]};
render();
const slot=document.querySelector('.live-messages');
assert(slot);
assert(slot.children.some(node=>node.tagName==='article'));
assert(!slot.textContent.includes('[object HTMLElement]'));
const firstAvatars=document.querySelectorAll('.avatar[data-avatar-id]').filter(n=>n.dataset.avatarId==='agent');
assert(firstAvatars.length>=2);
assert(firstAvatars.every(n=>n.classList.contains('is-working')));
activity.runs[0].active=false;activity.runs[0].status='completed';paintActivity();
assert(document.querySelectorAll('.avatar[data-avatar-id]').filter(n=>n.dataset.avatarId==='agent').every(n=>!n.classList.contains('is-working')));
assert(document.querySelectorAll('.avatar[data-avatar-id]').filter(n=>n.dataset.avatarId==='peer').every(n=>n.classList.contains('is-working')));
assert.equal(document.querySelector('.live-messages').children.length,0);
activityError='offline';paintActivity();
assert(document.querySelectorAll('.avatar[data-avatar-id]').every(n=>!n.classList.contains('is-working')));
""")

    def test_late_thread_response_never_replaces_a_newer_navigation(self):
        self.run_dom("""
(async()=>{
const make=id=>({project_id:'p',project_name:'project',thread_id:id,topic:id,objective:'goal',participants:['agent'],coordinator:null,signals:[],turns:[],history:[]});
snapshot={projects:[{id:'p',name:'project',available:true,threads:[make('first'),make('second')]}],collaborators:[],overview:{}};
const waiting=[];fetch=url=>new Promise(resolve=>waiting.push({url,resolve}));
location.hash='#/threads/p/first';const first=ensureRouteData();
location.hash='#/threads/p/second';const second=ensureRouteData();
waiting[1].resolve({ok:true,json:async()=>({thread:{...make('second'),topic:'SECOND_CURRENT'}})});
await second;
assert(root.textContent.includes('SECOND_CURRENT'));
waiting[0].resolve({ok:true,json:async()=>({thread:{...make('first'),topic:'FIRST_LATE'}})});
await first;
assert(root.textContent.includes('SECOND_CURRENT'));
assert(!root.textContent.includes('FIRST_LATE'));
assert.equal(details.get(routeKey('p','first')).topic,'FIRST_LATE');
})();
""")

    def test_manual_refresh_generation_discards_inflight_old_detail(self):
        self.run_dom("""
(async()=>{
const thread={project_id:'p',project_name:'project',thread_id:'t',topic:'topic',participants:['agent'],signals:[],turns:[]};
snapshot={projects:[{id:'p',name:'project',available:true,threads:[thread]}],collaborators:[],overview:{}};
let resolve;fetch=()=>new Promise(done=>{resolve=done;});
location.hash='#/threads/p/t';const pending=ensureRouteData();
contentGeneration+=1;details.clear();detailPending.clear();
resolve({ok:true,json:async()=>({thread:{...thread,topic:'STALE_DETAIL'}})});
await pending;
assert.equal(details.has(routeKey('p','t')),false);
})();
""")

    def test_activity_poll_ignores_late_results_and_clears_on_error(self):
        self.run_dom("""
(async()=>{
snapshot={projects:[],collaborators:[],overview:{}};
const pending=[];fetch=url=>new Promise(resolve=>pending.push(resolve));
const one=pollActivity(),two=pollActivity();
const run=id=>({run_id:id,identity:id,active:true,status:'running',expires_at:'2099-01-01T00:00:00Z'});
pending[1]({ok:true,json:async()=>({generated_at:new Date().toISOString(),runs:[run('new')]})});
await two;
pending[0]({ok:true,json:async()=>({generated_at:new Date().toISOString(),runs:[run('old')]})});
await one;
assert.equal(activity.runs[0].identity,'new');
fetch=async()=>{throw new Error('offline');};
await pollActivity();
assert.equal(activity,null);
assert(activityError);
})();
""")

    def test_all_read_only_routes_render_without_inventing_data(self):
        self.run_dom("""
const thread={project_id:'p',project_name:'project',thread_id:'t',topic:'A <b>literal</b> title',objective:'goal',participants:['agent'],coordinator:null,status:'OPEN',round:1,signals:[],turns:[]};
snapshot={projects:[{id:'p',name:'project',root:'/safe/path',available:true,threads:[thread]}],collaborators:[{id:'agent',identity:'agent',projects:[{id:'p'}],threads:[thread],tools:[],aliases:[]}],overview:{projects:1,threads:1,collaborators:1,pending:0,attention:[],recent:[]}};
tasks.set('agent',{items:[],errors:[]});
for(const hash of ['#/','#/projects','#/projects/p','#/threads','#/threads/p/t','#/people','#/people/agent','#/tasks/agent','#/threads/%/t']) {
 location.hash=hash;render();assert(root.children.length>0,hash);
 assert.equal(walk(root).filter(n=>n.tagName==='script').length,0);
}
assert(root.textContent.includes('地址格式无效'));
""")

    def test_avatar_is_deterministic_across_processes_aliases_and_threads(self):
        source = """
const first = {thread_id:'one',participants:['local-a'],participant_details:[
  {agent:'local-a',identity:'stable-alice',actor_type:'agent',is_coordinator:false}
]};
const second = {thread_id:'two',participants:['renamed-a'],participant_details:[
  {agent:'renamed-a',identity:'stable-alice',actor_type:'agent',is_coordinator:true}
]};
const one = M.actorFor(first,'local-a');
const two = M.actorFor(second,'renamed-a');
assert.equal(one.identity,two.identity);
assert.deepEqual(M.avatarSpec(one.identity),M.avatarSpec(two.identity));
console.log(JSON.stringify(M.avatarSpec(one.identity)));
"""
        self.assertEqual(self.run_node(source), self.run_node(source))

    def test_representative_collaborators_have_distinct_visible_avatar_specs(self):
        self.run_node("""
const specs = ['codex','claude','gemini','lead'].map(identity => {
  const {palette,accessory,accent} = M.avatarSpec(identity);
  assert(palette.every(color => /^#[0-9a-f]{6}$/i.test(color)));
  return JSON.stringify({palette,accessory,accent});
});
assert.equal(new Set(specs).size,specs.length);
assert.equal(M.hashIdentity('协作者-一'),M.hashIdentity('协作者-一'));
""")

    def test_human_avatar_style_is_explicit_not_guessed_from_identity_name(self):
        self.run_node("""
const regular=M.avatarSpec('human:owner');
const human=M.avatarSpec('human:owner',true);
assert.notDeepEqual(regular.palette,human.palette);
assert.equal(human.accessory,4);
assert.equal(regular.seed,human.seed);
""")

    def test_routes_decode_identifiers_and_reject_malformed_percent_encoding(self):
        self.run_node("""
assert.deepEqual(M.parseRoute('#/threads/project%20one/%E4%BB%BB%E5%8A%A1'),
  {name:'thread',projectId:'project one',threadId:'任务'});
assert.deepEqual(M.parseRoute('#/tasks/stable%3Aalice'),{name:'tasks',id:'stable:alice'});
assert.deepEqual(M.parseRoute('#/people/a%2Fb'),{name:'person',id:'a/b'});
assert.deepEqual(M.parseRoute('#/threads/%E0%A4%A/x'),{name:'invalid'});
assert.deepEqual(M.parseRoute('#/tasks/%'),{name:'invalid'});
assert.deepEqual(M.parseRoute(''),{name:'overview'});
assert.deepEqual(M.parseRoute('#/threads'),{name:'threads'});
""")

    def test_explicit_stable_participants_override_legacy_name_fallback(self):
        self.run_node("""
const t={participants:['worker','reviewer','worker'],coordinator:'reviewer',
  participant_details:[{agent:'worker',identity:'stable-worker',actor_type:'agent',is_coordinator:false}]};
assert.deepEqual(M.participants(t),[
  {agent:'worker',identity:'stable-worker',actor_type:'agent',is_coordinator:false},
  {agent:'reviewer',identity:'reviewer',actor_type:'agent',is_coordinator:true}
]);
assert.equal(M.actorFor(t,'worker').identity,'stable-worker');
assert.equal(M.actorFor(t,'reviewer').is_coordinator,true);
assert.equal(M.actorFor(t,'unknown').identity,'unknown');
""")

    def test_coordinator_is_thread_scoped_and_never_invented(self):
        self.run_node("""
const plain={participants:['lead','worker'],coordinator:null};
assert(M.participants(plain).every(p=>!p.is_coordinator));
const designated={participants:['worker'],coordinator:'lead'};
assert.equal(M.participants(designated).find(p=>p.agent==='lead').is_coordinator,true);
const authoritative={participants:['lead'],coordinator:'lead',participant_details:[
  {agent:'lead',identity:'same',actor_type:'agent',is_coordinator:false}
]};
assert.equal(M.participants(authoritative)[0].is_coordinator,false);
""")

    def test_legacy_turn_is_never_promoted_to_human_by_name_or_role(self):
        self.run_node("""
const turns=[{author:'human',identity:'fake',actor_type:'human',role:'human',
  kind:'PROPOSAL',type:'human-input',provenance:{kind:'manual'},body:'invented source',
  document:'turns/one.md',summary:'An agent summary',created_at:'2026-01-01T00:00:00Z'},
  {author:'ignored',error:'broken document'}];
const result=M.history({participants:['human'],coordinator:null,turns});
assert.equal(result.length,1);
assert.equal(result[0].type,'turn');
assert.equal(result[0].actor_type,'agent');
assert.equal(result[0].identity,'human');
assert.equal(result[0].provenance,null);
assert.equal(result[0].recipient_source,'unknown');
assert.equal(result[0].body,'');
assert.equal(result[0].legacy,true);
assert(!M.isHumanEntry(result[0]));
""")

    def test_authoritative_history_can_be_empty_without_reviving_legacy_turns(self):
        self.run_node("""
const supplied=[{id:'provided',type:'turn',body:'full body'}];
assert.deepEqual(M.history({history:supplied,turns:[{author:'old'}]}),supplied);
assert.deepEqual(M.history({history:[],turns:[{author:'old'}]}),[]);
assert.deepEqual(M.history(null),[]);
""")

    def test_human_card_requires_explicit_event_kind_authority_and_original_text(self):
        self.run_node("""
const human={type:'human-input',actor_type:'human',identity:'person-one',
 provenance:{kind:'manual',supplied_authorized:true,original_text:true}};
assert(M.isHumanEntry(human)); // Manual entry needs no invented external IDs.
const imported={...human,provenance:{...human.provenance,kind:'authorized-import',
 source_conversation_id:'session-1',source_message_id:'msg-1'}};
assert(M.isHumanEntry(imported));
for(const change of [{type:'turn'},{actor_type:'agent'},{identity:''},{identity:'   '},{provenance:null},
 {provenance:{...human.provenance,kind:'agent-summary'}},
 {provenance:{...human.provenance,supplied_authorized:false}},
 {provenance:{...human.provenance,supplied_authorized:'true'}},
 {provenance:{...human.provenance,original_text:false}},
 {provenance:{...human.provenance,original_text:undefined}}]) {
 assert(!M.isHumanEntry({...human,...change}),JSON.stringify(change));
}
for(const change of [{source_conversation_id:''},{source_message_id:''},
 {source_conversation_id:'   '},{source_message_id:123}]) {
 assert(!M.isHumanEntry({...imported,provenance:{...imported.provenance,...change}}));
}
assert(!M.isHumanEntry(null));
assert(!M.isHumanEntry({author:'human',role:'human',body:'@worker please act'}));
""")

    def test_explicit_identity_never_inherits_a_different_author_coordinator_role(self):
        self.run_node("""
const thread={participants:['lead','worker'],coordinator:'lead',participant_details:[
 {agent:'lead',identity:'stable-lead',is_coordinator:true},
 {agent:'worker',identity:'stable-worker',is_coordinator:false}
]};
assert.equal(M.actorFor(thread,'lead','stable-worker').is_coordinator,false);
assert.equal(M.actorFor(thread,'worker','stable-lead').is_coordinator,true);
assert.equal(M.actorFor(thread,'lead','unknown-stable').is_coordinator,false);
""")

    def test_recipient_mentions_require_explicit_mapping_not_message_text(self):
        self.run_node("""
const targets=[{agent:'worker',identity:'stable-worker'}];
assert.deepEqual(M.recipients({recipient_source:'explicit-human-input',recipients:targets}),targets);
assert.deepEqual(M.recipients({recipient_source:'current-signal',recipients:targets}),targets);
for(const source of ['unknown','guessed','agent-summary','']) {
 assert.deepEqual(M.recipients({recipient_source:source,recipients:targets,
   body:'@worker',source_conversation_id:'worker'}),[]);
}
assert.deepEqual(M.recipients({body:'@worker please act'}),[]);
assert.deepEqual(M.recipients({recipient_source:'explicit-human-input',recipients:[null,{},
 {agent:3,identity:'a'},{agent:'a',identity:null},...targets]}),targets);
""")

    def test_active_requires_observed_boolean_running_state_and_future_expiry(self):
        self.run_node("""
const now=Date.parse('2026-01-01T00:00:00Z');
const run={run_id:'one',identity:'stable-worker',active:true,status:'running',
 expires_at:'2026-01-01T00:00:10Z'};
assert.deepEqual(M.activeRuns({runs:[run]},now),[run]);
for(const change of [{active:false},{active:'true'},{active:1},{status:'waiting'},
 {status:'unknown'},{status:'queued'},{status:'completed'},{status:'failed'},
 {status:'cancelled'},{status:'executing'},{status:'RUNNING'},
 {expires_at:'2025-12-31T23:59:59Z'},{expires_at:'2026-01-01T00:00:00Z'},
 {expires_at:'not a date'},{expires_at:null},{identity:null},{identity:''},{identity:'  '}]) {
 assert.deepEqual(M.activeRuns({runs:[{...run,...change}]},now),[],JSON.stringify(change));
}
assert.deepEqual(M.activeRuns({runs:[{identity:'a',phase:'implementing',pending:5}]},now),[]);
""")

    def test_human_activity_is_excluded_even_with_a_live_run_record(self):
        self.run_node("""
const now=Date.parse('2026-01-01T00:00:00Z');
const run={identity:'owner',active:true,status:'running',expires_at:'2026-01-01T00:00:10Z'};
assert.deepEqual(M.activeRuns({runs:[{...run,actor_type:'human'}]},now),[]);
assert.deepEqual(M.activeRuns({runs:[run]},now,['owner']),[]);
assert.equal(M.activeRunFor({runs:[run]},'owner',now,['owner']),null);
""")

    def test_activity_is_stable_identity_scoped_across_threads_and_ends_globally(self):
        self.run_node("""
const now=Date.parse('2026-01-01T00:00:00Z');
const first={run_id:'one',identity:'alice',agent:'local-a',thread_id:'thread-one',
 active:true,status:'running',expires_at:'2026-01-01T00:00:10Z'};
const second={...first,run_id:'two',identity:'bob',agent:'local-b',thread_id:'thread-two'};
const state={runs:[first,second]};
assert.equal(M.activeRunFor(state,'alice',now).thread_id,'thread-one');
assert.equal(M.activeRunFor(state,'local-a',now),null);
assert.equal(M.activeRunFor(state,'unrelated',now),null);
first.active=false;first.status='completed';
assert.equal(M.activeRunFor(state,'alice',now),null);
assert.equal(M.activeRunFor(state,'bob',now).run_id,'two');
assert.equal(M.activeRunFor(state,'bob',now+11000),null);
""")

    def test_model_helpers_are_pure_and_do_not_mutate_supplied_data(self):
        self.run_node("""
function freeze(value){if(value&&typeof value==='object'){Object.freeze(value);Object.values(value).forEach(freeze);}return value;}
const thread=freeze({participants:['a'],coordinator:'a',turns:[{author:'a',kind:'PROPOSAL',summary:'safe'}]});
const before=JSON.stringify(thread);
M.participants(thread);M.actorFor(thread,'a');M.history(thread);
assert.equal(JSON.stringify(thread),before);
const state=freeze({runs:[{identity:'a',active:true,status:'running',expires_at:'2099-01-01T00:00:00Z'}]});
M.activeRuns(state,0);M.activeRunFor(state,'a',0);
""")

    def test_missing_collections_and_unknown_labels_have_safe_fallbacks(self):
        self.run_node("""
assert.deepEqual(M.participants(null),[]);
assert.deepEqual(M.activeRuns(null),[]);
assert.deepEqual(M.recipients(null),[]);
assert.deepEqual(M.array({}),[]);
assert.equal(M.categoryLabel('actionable'),'可继续处理');
assert.equal(M.categoryLabel('waiting-peer'),'等待同伴');
assert.equal(M.categoryLabel('waiting-human'),'等待人工');
assert.equal(M.categoryLabel('not-known'),'状态未分类');
assert.equal(M.phaseLabel(null),'未提供阶段');
assert.equal(M.phaseLabel('future-phase'),'future-phase');
for(const key of ['__proto__','constructor','toString']) {
 assert.equal(M.phaseLabel(key),key);
 assert.equal(M.categoryLabel(key),'状态未分类');
}
""")


class UiV04AssetTests(unittest.TestCase):
    def test_production_assets_do_not_interpret_record_html_or_ship_demo_state(self):
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"\.(?:innerHTML|outerHTML)\s*=")
        self.assertNotRegex(script, r"\b(?:insertAdjacentHTML|eval)\s*\(")
        self.assertNotIn("document.write(", script)
        self.assertIn("document.createTextNode", script)
        self.assertNotIn("simulatedRuns", script)
        self.assertNotIn("demoCompleted", script)
        self.assertNotIn("PROTOTYPE v", script)

    def test_reduced_motion_stops_animations_but_keeps_work_indicator(self):
        css = (ROOT / "ui" / "app.css").read_text(encoding="utf-8")
        match = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}",
            css,
        )
        self.assertIsNotNone(match)
        reduced = match.group(1)
        self.assertIn(".avatar-activity", reduced)
        self.assertIn(".typing-dots", reduced)
        self.assertIn("animation: none", reduced)
        self.assertNotIn("display: none", reduced)
        self.assertNotIn("visibility: hidden", reduced)

    def test_pure_model_loads_before_ui_script(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('/view-model.js'), html.index('/app.js'))


if __name__ == "__main__":
    unittest.main()
