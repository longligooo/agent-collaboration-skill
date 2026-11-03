"""Async snapshot jobs retain one deadline and cancel only their UI consumer."""
import unittest
import test_ui_refresh as refresh

HELPERS = r'''
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const jobA='a'.repeat(32),jobB='b'.repeat(32);
const pending=(id=jobA,delay=100)=>({ok:true,status:202,json:async()=>({pending:true,job_id:id,retry_after_ms:delay})});
const tick=async()=>{const timer=timers.find(t=>!t.cleared&&t.delay>=100&&t.delay<=1000);assert(timer,'expected pending poll');timer.cleared=true;timer.fn();await flush();};
'''

@unittest.skipUnless(refresh.NODE, "Node.js is needed to execute production UI")
class UiSnapshotJobTests(unittest.TestCase):
    run_dom = refresh.UiRefreshTests.run_dom

    def test_initial_and_cursor_jobs_poll_without_resetting_batch_deadline(self):
        self.run_dom(HELPERS + r'''
(async()=>{
location.hash='#/threads';snapshot=null;const urls=[],signals=[];let reads=0;
fetch=async(url,{signal})=>{urls.push(url);signals.push(signal);reads++;
 if(reads===1||reads===2)return pending(jobA,100);
 if(reads===3)return response({...snap(),progress:{loaded:1,total:2,complete:false,cursor:'next'}});
 if(reads===4)return pending(jobB,500);
 return response({...snap([thread('p'),thread('q')]),progress:{loaded:2,total:2,complete:true,cursor:null}});};
const reading=load();await flush();assert.equal(reads,1);assert(urls[0].includes('async=1'));
assert.equal(timers.filter(t=>t.delay===60000).length,1);
await tick();assert.equal(reads,2);await tick();assert.equal(reads,4);
assert.equal(timers.filter(t=>t.delay===60000).length,2);assert.equal(allThreads().length,1);
assert(urls[3].includes('cursor=next'));assert(urls[3].includes('async=1'));
await tick();await reading;assert.equal(allThreads().length,2);
assert.equal(urls[1],'/api/snapshot?job='+jobA);assert.equal(urls[2],urls[1]);
assert.equal(urls[4],'/api/snapshot?job='+jobB);assert(signals.every(s=>s===signals[0]));
assert.equal(error,'');assert.equal(snapshotProgress,null);
})();
''')

    def test_abort_during_poll_wait_clears_timer_and_sends_no_more_requests(self):
        self.run_dom(HELPERS + r'''
(async()=>{
let reads=0;fetch=async()=>{reads++;return pending();};const controller=new AbortController();
const reading=getJSON('/api/snapshot?progressive=1&async=1',controller.signal).then(()=>{throw Error('must abort');},e=>assert.equal(e.name,'AbortError'));
await flush();controller.abort();await reading;assert.equal(reads,1);
assert(!timers.some(t=>!t.cleared&&t.delay===100));
})();
''')

    def test_navigation_shares_pending_job_and_new_refresh_discards_old_result(self):
        self.run_dom(HELPERS + r'''
(async()=>{
location.hash='#/';snapshot=null;let reads=0,late;const signals=[];
fetch=(url,{signal})=>{reads++;signals.push(signal);if(reads===1)return Promise.resolve(pending());return new Promise(resolve=>{late=resolve;});};
const old=load();await flush();location.hash='#/projects';window.dispatch('hashchange');await flush();assert.equal(reads,1);
await tick();assert.equal(reads,2);
fetch=async url=>{assert(url.includes('progressive=1'));return response(snap([thread('fresh')]));};
await load();assert.equal(signals[0].aborted,true);late(response(snap([thread('stale')])));await old;
assert.equal(allThreads()[0].project_id,'fresh');assert.equal(route().name,'projects');
assert.equal(error,'');assert.equal(document.querySelector('.refresh').disabled,false);
})();
''')

    def test_hidden_page_aborts_wait_and_visible_page_does_not_restart_content(self):
        self.run_dom(HELPERS + r'''
(async()=>{
location.hash='#/';let reads=0;fetch=async url=>{if(url==='/api/activity')return response({runs:[]});reads++;return pending();};
const reading=load();await flush();document.hidden=true;document.dispatch('visibilitychange');await reading;
assert.equal(reads,1);assert(!timers.some(t=>!t.cleared&&t.delay===100));
document.hidden=false;document.dispatch('visibilitychange');await flush();assert.equal(reads,1);
})();
''')

    def test_invalid_or_changed_job_ids_fail_without_following_urls(self):
        self.run_dom(HELPERS + r'''
(async()=>{
for(const id of ['../outside','https://evil.example/'+jobA,'A'.repeat(32),null]){
 let reads=0;fetch=async()=>{reads++;return pending(id);};
 await assert.rejects(getJSON('/api/snapshot?progressive=1',new AbortController().signal),/后台读取状态无效/);assert.equal(reads,1);
}
let reads=0;fetch=async()=>pending(++reads===1?jobA:jobB);const reading=getJSON('/api/snapshot?progressive=1',new AbortController().signal);
await flush();await tick();await assert.rejects(reading,/后台读取状态无效/);assert.equal(reads,2);
})();
''')

    def test_poll_delays_are_bounded_and_deadline_is_not_extended(self):
        self.run_dom(HELPERS + r'''
(async()=>{
location.hash='#/';let reads=0;fetch=async()=>{reads++;return pending(jobA,1e12);};
const reading=load();await flush();assert(timers.some(t=>!t.cleared&&t.delay===1000));
const deadline=timers.find(t=>!t.cleared&&t.delay===60000);deadline.fn();await reading;
assert.equal(reads,1);assert(error.includes('读取超时'));assert(!timers.some(t=>!t.cleared&&t.delay===1000));
for(const delay of [-99,0,'bad',null]){
 const controller=new AbortController();fetch=async()=>pending(jobA,delay);
 const read=getJSON('/api/snapshot?progressive=1',controller.signal).catch(e=>assert.equal(e.name,'AbortError'));
 await flush();assert(timers.some(t=>!t.cleared&&t.delay===100));controller.abort();await read;
}
})();
''')

    def test_stable_error_codes_are_safe_and_old_server_status_remains_supported(self):
        self.run_dom(HELPERS + r'''
(async()=>{
location.hash='#/';const cases=[['snapshot_timeout',503,'读取超时'],['snapshot_busy',503,'后台仍在读取'],['snapshot_expired',410,'结果已过期'],['unknown',503,'读取繁忙']];
for(const [error_code,status,expected] of cases){
 fetch=async()=>({ok:false,status,json:async()=>({error_code,error:'secret traceback text'})});await load();assert(error.includes(expected));assert(error.includes('保留已加载记录'));assert(!error.includes('secret'));
}
fetch=async()=>({ok:false,status:503});await load();assert(error.includes('读取繁忙'));
fetch=async()=>pending();assert.equal((await getJSON('/api/activity')).pending,true);
})();
''')
