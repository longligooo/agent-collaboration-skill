"""Triggered read refresh tests against production app.js (no browser package needed)."""
import json
import subprocess
import unittest

from test_ui_v04 import DOM_HARNESS, MODEL, NODE

FIXTURE = r'''
const entry=(id,body=id)=>({id,type:'turn',author:'agent',identity:'agent',kind:'PROPOSAL',role:'implementer',summary:id,body,created_at:'2026-01-01T00:00:00Z',recipients:[]});
const thread=(project='p',items=[entry('one','Full body')])=>({project_id:project,project_name:project,thread_id:'same',topic:project,participants:['agent'],coordinator:null,signals:[],turns:[],history:items});
const snap=(threads=[thread()],stamp='2026-01-01T00:00:00Z')=>({schema_version:1,view:route().name,projects:threads.map(t=>({id:t.project_id,name:t.project_id,available:true,threads:[t]})),collaborators:[],overview:{},generated_at:stamp});
location.hash='#/threads/p/same';snapshot=snap();snapshot.detail=thread();displayedPageKey=pageCacheKey();details.set(routeKey('p','same'),thread());render();
const response=payload=>({ok:true,json:async()=>payload.thread?{...snap([payload.thread]),detail:payload.thread}:payload.projects?{...payload,detail:payload.detail||payload.projects[0]?.threads[0]}:payload});
'''


@unittest.skipUnless(NODE, "Node.js is needed to execute production UI refresh")
class UiRefreshTests(unittest.TestCase):
    def run_dom(self, source):
        script = ('"use strict";const assert=require("node:assert/strict");'
                  f'const M=require({json.dumps(str(MODEL))});const MODEL_PATH={json.dumps(str(MODEL))};\n'
                  + DOM_HARNESS + '\nlet settled=false;process.on("beforeExit",()=>{if(!settled){console.error("UI test promise did not settle");process.exitCode=1;}});Promise.resolve(vm.runInContext('
                  + json.dumps(FIXTURE + source) + ',context)).then(()=>{settled=true;}).catch(error=>{settled=true;console.error(error);process.exitCode=1;});')
        result = subprocess.run([NODE, '-'], input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, f'{result.stdout}\n{result.stderr}')

    def test_display_rejects_numeric_and_rollover_timestamps(self):
        self.run_dom(r'''
for(const invalid of [42, null, {}, [], true]) {
 assert.equal(formatTime(invalid),'时间未提供');assert.equal(clockTime(invalid),'时间未提供');
 assert.equal(formatDuration(invalid,'2026-10-30T15:00:00Z'),'');
}
for(const invalid of ['2026-02-30T12:00:00Z','not a date','42']) {
 assert.equal(formatTime(invalid),invalid);assert.equal(clockTime(invalid),'时间未提供');
}
assert.equal(formatDuration('2026-10-30T14:00:00Z','2026-10-30T15:00:00Z'),'1 小时');
assert.notEqual(clockTime('2026-10-30T15:00:00Z'),'时间未提供');
''')

    def test_unchanged_refresh_does_not_remount_and_only_sampling_time_changes(self):
        self.run_dom(r'''
(async()=>{
const shell=root.children[0], message=document.querySelector('.message[data-message-key]');
let count=0;fetch=async url=>{count++;return response(url.startsWith('/api/page')?snap([thread()],'2026-01-02T00:00:00Z'):{thread:thread()});};
await load({background:true});
assert.equal(count,1);assert.equal(root.children[0],shell);
assert.equal(document.querySelector('.message[data-message-key]'),message);
assert.equal(document.querySelector('.snapshot-time').textContent, '更新于 '+formatTime('2026-01-02T00:00:00Z'));
assert(!timers.some(t=>!t.cleared&&t.delay===3000));
})();
''')

    def test_incremental_refresh_reuses_nodes_retains_expansion_scroll_and_input_focus(self):
        self.run_dom(r'''
(async()=>{
const original=document.querySelector('.message[data-message-key]');
const disclosure=original.querySelector('details');disclosure.open=true;disclosure.dispatch('toggle');
const messages=document.querySelector('.messages');messages.scrollTop=120;
const field=document.querySelector('#thread-search');field.focus();field.setSelectionRange(1,3);threadQuery='p';
const next=thread('p',[entry('one','Full body'),entry('two'),entry('two')]);
fetch=async()=>response({thread:next});await load({background:true});
assert.equal(document.querySelectorAll('.message[data-message-key]').length,2);
assert.equal(document.querySelector('.message[data-message-key]'),original);
assert.equal(original.querySelector('details'),disclosure);assert.equal(disclosure.open,true);
assert.equal(document.querySelector('.messages').scrollTop,120);
assert.equal(document.activeElement.getAttribute('id'),'thread-search');
assert.equal(document.activeElement.selectionStart,1);assert.equal(document.activeElement.selectionEnd,3);
assert.equal(document.activeElement.getAttribute('value'),'p');
const nextShell=root.children[0];await load({background:true});assert.equal(root.children[0],nextShell);
})();
''')

    def test_scroll_anchor_is_preserved_when_older_import_is_inserted_above(self):
        self.run_dom(r'''
(async()=>{
Node.prototype.getBoundingClientRect=function(){if(this.classList.contains('messages'))return {top:0,bottom:300};
const container=this.closest('.messages');const index=container?container.children.indexOf(this)-1:0;
const top=index*100-(container?.scrollTop||0);return {top,bottom:top+100};};
const before=thread('p',[entry('one'),entry('two'),entry('three'),entry('four')]);details.set(routeKey('p','same'),before);render();
document.querySelector('.messages').scrollTop=125;
const next=thread('p',[entry('earlier'),...before.history]);fetch=async()=>response({thread:next});
await load({background:true});
assert.equal(document.querySelector('.messages').scrollTop,225);
})();
''')

    def test_mobile_document_scroll_preserves_anchor_across_older_insertion(self):
        self.run_dom(r'''
(async()=>{
window.getComputedStyle=()=>({overflowY:'visible'});window.scrollY=325;window.innerHeight=390;
window.scrollTo=({top})=>{window.scrollY=top;};document.scrollingElement={scrollHeight:2000,scrollTop:325};
Node.prototype.getBoundingClientRect=function(){if(this.classList.contains('messages'))return {top:200-window.scrollY,bottom:1200-window.scrollY};
const container=this.closest('.messages'),index=container?container.children.indexOf(this)-1:0;
const top=200+index*100-window.scrollY;return {top,bottom:top+100};};
const before=thread('p',[entry('one'),entry('two'),entry('three'),entry('four')]);details.set(routeKey('p','same'),before);render();
window.scrollY=325;
fetch=async()=>response({thread:thread('p',[entry('earlier'),...before.history])});
await load({background:true});
assert.equal(window.scrollY,425);assert.equal(document.querySelector('.messages').scrollTop,0);
})();
''')

    def test_at_bottom_follows_new_messages(self):
        self.run_dom(r'''
(async()=>{
document.querySelector('.messages').scrollTop=300;
fetch=async()=>response({thread:thread('p',[entry('one','Full body'),entry('two')])});
await load({background:true});
const messages=document.querySelector('.messages');assert.equal(messages.scrollTop,messages.scrollHeight);
})();
''')

    def test_same_key_superseded_response_cannot_overwrite_newer_history(self):
        self.run_dom(r'''
(async()=>{
const waiting=[];fetch=()=>new Promise(resolve=>waiting.push(resolve));
const first=load({background:true}), second=load({background:true});
waiting[1](response({thread:thread('p',[entry('new')])}));await second;
waiting[0](response({thread:thread('p',[entry('old')])}));await first;
assert.equal(details.get(routeKey('p','same')).history[0].id,'new');
assert(document.querySelector('.messages').textContent.includes('new'));
assert(!document.querySelector('.messages').textContent.includes('old'));
})();
''')

    def test_project_thread_and_entry_collisions_are_separate_and_late_route_is_not_rendered(self):
        self.run_dom(r'''
(async()=>{
snapshot=snap([thread('p'),thread('other')]);
const waiting=[];fetch=()=>new Promise(resolve=>waiting.push(resolve));
const first=load({background:true});
location.hash='#/threads/other/same';const second=load({background:true});
waiting[1](response({thread:thread('other',[entry('one','OTHER BODY')])}));await second;
const otherNode=document.querySelector('.message[data-message-key]');
waiting[0](response({thread:thread('p',[entry('one','ORIGINAL BODY')])}));await first;
assert.equal(document.querySelector('.message[data-message-key]'),otherNode);
assert.equal(details.has(routeKey('p','same')),false);
assert.equal(details.get(routeKey('other','same')).history[0].body,'OTHER BODY');
location.hash='#/threads/p/same';fetch=async()=>response({thread:thread('p')});await load();
assert.notEqual(document.querySelector('.message[data-message-key]').dataset.messageKey,otherNode.dataset.messageKey);
})();
''')

    def test_manual_refresh_wins_over_inflight_background_snapshot(self):
        self.run_dom(r'''
(async()=>{
const waiting=[];fetch=url=>url.startsWith('/api/page')?new Promise(resolve=>waiting.push(resolve)):Promise.resolve(response({thread:thread('p',[entry('fresh')])}));
const first=load({background:true}),second=load();
const newSnapshot=snap();newSnapshot.projects[0].name='FRESH PROJECT';waiting[1](response(newSnapshot));await second;
const oldSnapshot=snap();oldSnapshot.projects[0].name='STALE PROJECT';waiting[0](response(oldSnapshot));await first;
assert.equal(snapshot.projects[0].name,'FRESH PROJECT');
assert.equal(details.get(routeKey('p','same')).history[0].id,'one');
})();
''')

    def test_offline_keeps_complete_history_and_repeated_failure_does_not_remount(self):
        self.run_dom(r'''
(async()=>{
const original=document.querySelector('.message[data-message-key]');
fetch=async()=>{throw new Error('offline');};await load({background:true});
assert.equal(details.get(routeKey('p','same')).history.length,1);
assert.equal(document.querySelector('.message[data-message-key]'),original);
assert(root.textContent.includes('可能不是最新状态'));
assert(root.textContent.includes('保留缓存数据'));
assert.equal(document.querySelectorAll('.avatar.is-working').length,0);
const shell=root.children[0];await load({background:true});assert.equal(root.children[0],shell);
})();
''')

    def test_hidden_page_discards_late_response_and_resume_only_restarts_activity(self):
        self.run_dom(r'''
(async()=>{
const waiting=[];fetch=url=>new Promise(resolve=>waiting.push({url,resolve}));
const old=load({background:true});document.hidden=true;document.dispatch('visibilitychange');
waiting[0].resolve(response(snap([thread('p',[entry('late-hidden')])])));await old;
assert.equal(details.get(routeKey('p','same')).history[0].id,'one');
assert(!timers.some(t=>!t.cleared&&t.delay===3000));
const count=waiting.length;document.hidden=false;document.dispatch('visibilitychange');
assert.equal(waiting.length,count+1); // activity only, no content scan
pauseContent();activitySequence+=1;activityController.abort();
})();
''')

    def test_interrupted_manual_refresh_restores_enabled_button_after_unchanged_resume(self):
        self.run_dom(r'''
(async()=>{
let resolve;fetch=()=>new Promise(done=>{resolve=done;});const pending=load();
assert.equal(document.querySelector('.refresh').disabled,false);
pauseContent();resolve(response(snap()));await pending;
fetch=async url=>response(url.startsWith('/api/page')?snap():{thread:thread()});
await load({background:true});assert.equal(document.querySelector('.refresh').disabled,false);
assert.equal(document.querySelector('.refresh').textContent,'刷新');
})();
''')

    def test_page_cache_restore_and_mismatched_thread_fail_closed(self):
        self.run_dom(r'''
(async()=>{
fetch=async()=>response({thread:thread('wrong-project')});await load({background:true});
assert.equal(details.get(routeKey('p','same')).project_id,'p');assert(error);
window.dispatch('pagehide');assert.equal(contentSuspended,true);
let calls=0;fetch=async url=>{calls++;return response(url.startsWith('/api/activity')?{runs:[]}:url.startsWith('/api/page')?snap():{thread:thread()});};
await load({background:true});assert.equal(calls,0);
window.dispatch('pageshow',{persisted:true});await new Promise(resolve=>Promise.resolve().then(resolve));
assert.equal(contentSuspended,false);assert.equal(calls,1);
})();
''')
