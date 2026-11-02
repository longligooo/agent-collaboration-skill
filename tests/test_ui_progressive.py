"""Progressive loading and 60-second deadline tests execute production app.js."""
import unittest

import test_ui_refresh as refresh


@unittest.skipUnless(refresh.NODE, "Node.js is needed to execute production UI")
class UiProgressiveTests(unittest.TestCase):
    run_dom = refresh.UiRefreshTests.run_dom

    def test_first_batch_renders_before_final_response_and_orders_globally(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';snapshot=null;details.clear();
const old=thread('a');old.last_activity_at='2026-01-01T00:00:00Z';
const newest=thread('z');newest.last_activity_at='2026-11-01T00:00:00Z';
const page=(threads,loaded,cursor)=>({...snap(threads),progress:{loaded,total:117,cursor,complete:!cursor}});
const many=Array.from({length:116},(_,i)=>({...old,thread_id:`old-${i}`}));
let last,finished=false,reads=0;
fetch=async url=>{reads++;if(reads===1)return response(page([],0,'one'));
 if(reads===2)return response({...page([],116,'two'),projects:[{id:'a',name:'a',available:true,threads:many}]});
 return new Promise(resolve=>{last=resolve;});};
const reading=load().then(()=>{finished=true;});
for(let i=0;i<20;i++)await Promise.resolve();
assert.equal(reads,3);assert.equal(finished,false);assert.equal(allThreads().length,116);
assert(root.textContent.includes('old-115'));assert(root.textContent.includes('116 / 117'));
assert(!root.textContent.includes('还没有注册'));
location.hash='#/projects/a';render(); // navigation while a batch is pending
last(response({...page([],117,null),projects:[{id:'a',name:'a',available:true,threads:many},{id:'z',name:'z',available:true,threads:[newest]}]}));
await reading;
assert.equal(route().id,'a');assert.equal(allThreads()[0].project_id,'z');
assert.equal(allThreads().length,117);assert.equal(snapshotProgress,null);
assert.equal(document.querySelector('.refresh').disabled,false);
})();
''')

    def test_failed_partial_refresh_keeps_old_rows_and_successful_retry_reconciles_deletions(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';snapshot=snap([thread('p'),thread('deleted')]);
let count=0;fetch=async()=>{if(++count===1)return response({...snap([thread('p'),thread('new')]),progress:{loaded:2,total:3,cursor:'more',complete:false}});throw new Error('batch offline');};
await load({background:true});
assert.equal(allThreads().length,3);assert(root.textContent.includes('读取中断'));
assert(root.textContent.includes('batch offline'));assert(snapshotProgress);
fetch=async()=>response({...snap([thread('new')]),progress:{loaded:1,total:1,cursor:null,complete:true}});
await load();assert.deepEqual(Array.from(allThreads(),t=>t.project_id),['new']);
assert.equal(error,'');assert.equal(snapshotProgress,null);
})();
''')

    def test_late_batch_after_new_refresh_or_pagehide_cannot_replace_records(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';let resolveOld,calls=0;
fetch=async()=>{if(++calls===1)return response({...snap(),progress:{loaded:1,total:2,cursor:'old',complete:false}});return new Promise(resolve=>{resolveOld=resolve;});};
const first=load();for(let i=0;i<20;i++)await Promise.resolve();
fetch=async()=>response({...snap([thread('fresh')]),progress:{loaded:1,total:1,cursor:null,complete:true}});
await load();resolveOld(response(snap([thread('stale')])));await first;
assert.equal(allThreads()[0].project_id,'fresh');
let late;fetch=()=>new Promise(resolve=>{late=resolve;});const hidden=load();
document.hidden=true;document.dispatch('visibilitychange');late(response(snap([thread('hidden')])));await hidden;
assert.equal(allThreads()[0].project_id,'fresh');assert.equal(document.querySelectorAll('.snapshot-progress').length,0);
})();
''')

    def test_partial_batches_preserve_chat_scroll_expansion_search_and_filter(self):
        self.run_dom(r'''
(async()=>{
messageFilter='PROPOSAL';render();
const original=document.querySelector('.message[data-message-key]');
original.querySelector('details').open=true;original.querySelector('details').dispatch('toggle');
document.querySelector('.messages').scrollTop=120;
const search=document.querySelector('#thread-search');threadQuery='p';search.focus();search.setSelectionRange(0,1);
let finish,reads=0;
fetch=async url=>{if(!url.startsWith('/api/snapshot'))return response({thread:thread()});
if(++reads===1)return response({...snap(),progress:{loaded:1,total:2,cursor:'next',complete:false}});
return new Promise(resolve=>{finish=resolve;});};
const reading=load({background:true});for(let i=0;i<20;i++)await Promise.resolve();
assert.equal(document.querySelector('.message[data-message-key]'),original);
assert.equal(document.querySelector('.messages').scrollTop,120);
assert.equal(document.querySelector('details').open,true);assert.equal(messageFilter,'PROPOSAL');
assert.equal(document.activeElement.getAttribute('id'),'thread-search');
finish(response({...snap([thread(),thread('q')]),progress:{loaded:2,total:2,cursor:null,complete:true}}));await reading;
assert.equal(document.querySelector('.message[data-message-key]'),original);
assert.equal(document.querySelector('.messages').scrollTop,120);assert.equal(threadQuery,'p');
assert.equal(document.activeElement.selectionEnd,1);
})();
''')

    def test_all_content_requests_accept_6360ms_and_abort_at_exactly_60000ms(self):
        self.run_dom(r'''
(async()=>{
let now=0,id=0;const queue=new Map();
setTimeout=(fn,delay)=>{const key=++id;queue.set(key,{fn,at:now+delay});return key;};clearTimeout=key=>queue.delete(key);
const advance=ms=>{now+=ms;for(const [key,t] of [...queue])if(t.at<=now){queue.delete(key);t.fn();}};
for(const mode of ['snapshot','thread','tasks']){
location.hash=mode==='snapshot'?'#/':mode==='thread'?'#/threads/p/same':'#/tasks/agent';
let signal,respond;fetch=(url,options)=>new Promise((resolve,reject)=>{signal=options.signal;respond=resolve;signal.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);});});
const read=mode==='snapshot'?load({background:true}):ensureRouteData({force:true,background:true});
advance(6360);assert.equal(signal.aborted,false,mode+' must accept the reported 6.36s latency');
respond(response(mode==='snapshot'?snap():mode==='thread'?{thread:thread()}:{items:[]}));await read;

const slow=mode==='snapshot'?load({background:true}):ensureRouteData({force:true,background:true});
advance(59999);assert.equal(signal.aborted,false,mode+' must not timeout early');
advance(1);assert.equal(signal.aborted,true,mode+' must timeout at60s');
await slow;assert(mode==='snapshot'?error.includes('读取超时'):mode==='thread'?detailErrors.get(routeKey('p','same'))==='读取超时':taskErrors.get('agent')==='读取超时');

}
assert.equal(REQUEST_TIMEOUT_MS,60000);
})();
''')

    def test_timeout_resets_per_batch_and_invalid_cursor_cannot_loop(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';let pending=[],reads=0;
fetch=(url,{signal})=>new Promise((resolve,reject)=>{pending.push({resolve,signal});});
const running=load();assert.equal(pending.length,1);
pending[0].resolve(response({...snap(),progress:{loaded:1,total:2,cursor:'repeat',complete:false}}));
for(let i=0;i<20;i++)await Promise.resolve();
const deadlines=timers.filter(t=>t.delay===60000);assert.equal(deadlines.length,2);assert.equal(deadlines[0].cleared,true);assert(!deadlines[1].cleared);
pending[1].resolve(response({...snap(),progress:{loaded:1,total:2,cursor:'repeat',complete:false}}));
await running;assert(error.includes('进度无效'));assert.equal(pending.length,2);
})();
''')

    def test_tasks_wait_for_discovered_default_identity_not_full_scan(self):
        self.run_dom(r''' 
(async()=>{
location.hash='#/tasks';snapshot=null;let final,reads=0,taskReads=0;
const person={id:'agent',identity:'agent',projects:[],aliases:[],threads:[],tools:[]};
fetch=async url=>{if(url.startsWith('/api/tasks')){taskReads++;return response({items:[]});}
if(++reads===1)return response({...snap([]),progress:{loaded:0,total:2,cursor:'first',complete:false}});
if(reads===2)return response({...snap(),collaborators:[person],progress:{loaded:1,total:2,cursor:'last',complete:false}});
return new Promise(resolve=>{final=resolve;});};
const reading=load();for(let i=0;i<30;i++)await Promise.resolve();
assert.equal(taskReads,1);assert(tasks.has('agent'));assert(snapshotProgress);
final(response({...snap(),collaborators:[person],progress:{loaded:2,total:2,cursor:null,complete:true}}));await reading;
})();
''')

    def test_partial_identity_merge_preserves_human_type_and_old_aliases(self):
        self.run_dom(r'''
const old={...snap(),collaborators:[{id:'same',identity:'same',actor_type:'human',aliases:[{project_id:'a',agent:'human'}],projects:[{id:'a'}],tools:[],threads:[]}]};
const fresh={...snap(),collaborators:[{id:'same',identity:'same',actor_type:'agent',aliases:[{project_id:'b',agent:'worker'}],projects:[{id:'b'}],tools:['codex'],threads:[]}]};
const merged=mergeSnapshot(old,fresh);assert.equal(merged.collaborators[0].actor_type,'human');
assert.equal(merged.collaborators[0].aliases.length,2);assert.equal(merged.collaborators[0].projects.length,2);
assert.equal(old.collaborators[0].aliases.length,1);
''')

    def test_reordered_default_identity_loads_newly_visible_tasks_within_same_scan(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/tasks';snapshot=null;let reads=0;const taskReads=[];
const person=id=>({id,identity:id,projects:[],aliases:[],threads:[],tools:[]});
fetch=async url=>{if(url.startsWith('/api/tasks')){taskReads.push(url);return response({items:[]});}
if(++reads===1)return response({...snap(),collaborators:[person('a')],progress:{loaded:1,total:2,cursor:'last',complete:false}});
return response({...snap(),collaborators:[person('b'),person('a')],progress:{loaded:2,total:2,cursor:null,complete:true}});};
await load();assert.equal(taskReads.length,2);assert(tasks.has('a'));assert(tasks.has('b'));
assert(!root.textContent.includes('尚未读取任务'));
})();
''')

    def test_real_backend_delta_pages_assemble_to_legacy_snapshot_in_production_frontend(self):
        import json
        from unittest import mock
        import test_ui_batches as batches
        fixture = batches.ProgressiveSnapshotTests()
        fixture.setUp()
        try:
            fixture.project('a', 10)
            fixture.project('z', 7, '2026-10-30T09:00:00Z')
            server = batches.ui_server
            original = server.scan_thread
            def decorated(root, thread_id, project):
                result = original(root, thread_id, project)
                second = int(thread_id.rsplit('-', 1)[1]) + (20 if project['name'] == 'z' else 0)
                stamp = f'2026-10-30T09:00:{second:02}Z'
                result['turns'] = [{'author': 'builder', 'created_at': stamp, 'round': 1, 'kind': 'PROPOSAL'}]
                result['signals'] = [{'from': 'builder', 'to': 'lead', 'decision': 'NEW', 'created_at': stamp}]
                result['last_activity_at'] = stamp
                return result
            with mock.patch.object(server, 'scan_thread', side_effect=decorated):
                pages = [fixture.start()]
                while not pages[-1]['progress']['complete']:
                    pages.append(server.progressive_snapshot(pages[-1]['progress']['cursor']))
                expected = server.snapshot(fixture.registry)
            expected.pop('generated_at')
            self.run_dom('const pages=' + json.dumps(pages) + ';const expected=' + json.dumps(expected) + r''';
(async()=>{
location.hash='#/';snapshot=null;fetch=async()=>response(pages.shift());
await load();assert.equal(pages.length,0);assert.equal(snapshotProgress,null);
const {generated_at,...actual}=snapshot;
const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
assert.equal(JSON.stringify(canonical(actual)),JSON.stringify(canonical(expected)));
assert.equal(snapshot.overview.pending,17);assert.equal(snapshot.overview.recent.length,12);
assert.equal(snapshot.collaborators.find(p=>p.id==='lead').pending,17);
assert.equal(snapshot.collaborators.find(p=>p.id==='builder').turns,17);
})();
''')
        finally:
            fixture.tearDown()

    def test_throttled_batch_is_painted_while_next_network_read_stalls(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';snapshot=null;let now=1000;Date.now=()=>now;
let reads=0,finish;fetch=async()=>{
if(++reads===1)return response({...snap([thread('p')]),progress:{loaded:1,total:3,cursor:'second',complete:false,delta:true}});
if(reads===2)return response({...snap([thread('q')]),progress:{loaded:2,total:3,cursor:'third',complete:false,delta:true}});
return new Promise(resolve=>{finish=resolve;});};
const reading=load();for(let i=0;i<20;i++)await Promise.resolve();
assert.equal(reads,3);assert.equal(document.querySelectorAll('.thread-card').length,1);
const paint=timers.find(t=>!t.cleared&&t.delay===100);assert(paint);
now+=100;paint.fn();assert.equal(document.querySelectorAll('.thread-card').length,2);
assert(root.textContent.includes('2 / 3'));
finish(response({...snap([thread('r')]),progress:{loaded:3,total:3,cursor:null,complete:true,delta:true}}));await reading;
assert.equal(document.querySelectorAll('.thread-card').length,3);assert.equal(contentPaintTimer,null);
})();
''')
