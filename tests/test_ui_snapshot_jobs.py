"""Cache-first page reads replace frontend snapshot-job polling."""
import unittest
import test_ui_refresh as refresh

@unittest.skipUnless(refresh.NODE, 'Node.js required')
class UiPageCacheTests(unittest.TestCase):
    run_dom = refresh.UiRefreshTests.run_dom

    def test_localstorage_visible_synchronously_before_revalidation(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads?page=2';const key=pageCacheKey(),cached=snap([thread('cached')]);savePageCache(key,cached);memoryPages.clear();snapshot=null;
let finish;fetch=()=>new Promise(resolve=>{finish=resolve;});const reading=load();
assert(root.textContent.includes('cached'));assert.equal(snapshot.projects[0].id,'cached');assert.equal(loading,false);
finish(response(snap([thread('fresh')])));await reading;assert.equal(snapshot.projects[0].id,'fresh');
assert.equal(JSON.parse(localStorage.getItem(key)).payload.projects[0].id,'fresh');
})();
''')

    def test_failed_revalidation_preserves_cache_and_success_replaces_not_merges(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';savePageCache(pageCacheKey(),snap([thread('old'),thread('deleted')]));
fetch=async()=>{throw Error('offline');};await load();assert.equal(allThreads().length,2);assert(error);
fetch=async()=>response(snap([thread('new')]));await load();assert.equal(allThreads().length,1);assert.equal(allThreads()[0].project_id,'new');assert.equal(error,'');
})();
''')

    def test_cache_key_separates_origins_routes_projects_pages_and_queries(self):
        self.run_dom(r'''
const keys=[];for(const hash of ['#/threads?page=1','#/threads?page=2','#/threads?page=1&q=hello','#/threads/p/same','#/threads/q/same','#/tasks/agent']){location.hash=hash;keys.push(pageCacheKey());}
assert.equal(new Set(keys).size,keys.length);const before=pageCacheKey();location.origin='http://localhost:9999';assert.notEqual(pageCacheKey(),before);
''')

    def test_corrupt_blocked_or_full_storage_never_prevents_fresh_read(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';localStorage.setItem(pageCacheKey(),'{broken');assert.equal(readPageCache(pageCacheKey()),null);
localStorage.getItem=()=>{throw Error('blocked');};localStorage.setItem=()=>{throw Error('quota');};
fetch=async()=>response(snap([thread('fresh')]));await load();assert.equal(snapshot.projects[0].id,'fresh');assert.equal(error,'');
})();
''')

    def test_storage_is_bounded_and_other_app_keys_are_untouched(self):
        self.run_dom(r'''
localStorage.setItem('other-app','keep');for(let i=1;i<=30;i++){location.hash=`#/threads?page=${i}`;savePageCache(pageCacheKey(),snap());}
assert.equal(memoryPages.size,12);assert(localStorage.length<=13);assert.equal(localStorage.getItem('other-app'),'keep');
''')

    def test_malformed_cache_is_rejected_and_page_number_is_sanitized(self):
        self.run_dom(r'''
location.hash='#/threads?page=-2';assert.equal(currentPage(),1);localStorage.setItem(pageCacheKey(),JSON.stringify({schema:1,payload:{projects:'bad'}}));assert.equal(readPageCache(pageCacheKey()),null);
location.hash='#/threads?page=1e300';assert.equal(currentPage(),1);
''')

    def test_request_has_one_deadline_and_no_polling(self):
        self.run_dom(r'''
(async()=>{
let calls=0;fetch=(url,{signal})=>{calls++;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{const e=Error();e.name='AbortError';reject(e);}));};
const promise=load();const timeout=timers.find(t=>!t.cleared&&t.delay===60000);assert(timeout);timeout.fn();await promise;assert.equal(calls,1);assert(error.includes('读取超时'));assert.equal(loading,false);
})();
''')

    def test_registry_scope_change_evicts_previous_service_cache(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';acceptCacheScope('old-registry');const old=pageCacheKey();savePageCache(old,snap([thread('old')]));
fetch=async()=>response({...snap([thread('new')]),cache_scope:'new-registry'});await load();
assert.equal(localStorage.getItem(old),null);assert(pageCacheKey().includes('new-registry'));assert.equal(snapshot.projects[0].id,'new');
assert.equal(readPageCache(pageCacheKey()).projects[0].id,'new');
})();
''')

    def test_malformed_cached_shapes_do_not_block_network(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';const key=pageCacheKey();localStorage.setItem(key,JSON.stringify({schema:1,payload:{schema_version:1,view:'threads',projects:[null],collaborators:[]}}));
let calls=0;fetch=async()=>{calls++;return response(snap([thread('fresh')]));};await load();assert.equal(calls,1);assert.equal(snapshot.projects[0].id,'fresh');
})();
''')

    def test_deep_corrupt_cached_render_still_fetches_fresh_page(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/';const bad={...snap(),overview:{attention:[null]}};localStorage.setItem(pageCacheKey(),JSON.stringify({schema:1,payload:bad}));
let calls=0;fetch=async()=>{calls++;return response(snap());};await load();assert.equal(calls,1);assert.equal(error,'');assert(snapshot);
})();
''')

    def test_mismatched_response_view_cannot_replace_or_cache_current_page(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';savePageCache(pageCacheKey(),snap([thread('old')]));fetch=async()=>response({...snap([thread('wrong')]),view:'projects'});await load();
assert.equal(snapshot.projects[0].id,'old');assert(error);assert.equal(readPageCache(pageCacheKey()).projects[0].id,'old');
})();
''')

    def test_scope_is_remembered_without_storage_access(self):
        self.run_dom(r'''
localStorage.getItem=()=>{throw Error('blocked');};localStorage.setItem=()=>{throw Error('blocked');};
acceptCacheScope('one');savePageCache(pageCacheKey(),snap());acceptCacheScope('one');assert.equal(memoryPages.size,1);assert(pageCacheKey().includes(':one:'));
''')
