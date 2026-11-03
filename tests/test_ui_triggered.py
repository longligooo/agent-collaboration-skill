"""Page-specific reads replace global snapshots and cursor scans."""
import unittest
import test_ui_refresh as refresh

@unittest.skipUnless(refresh.NODE, 'Node.js required')
class UiPageTriggerTests(unittest.TestCase):
    run_dom = refresh.UiRefreshTests.run_dom

    def test_each_route_requests_only_its_page_and_detail_is_bundled(self):
        self.run_dom(r'''
(async()=>{
let urls=[];fetch=async url=>{urls.push(url);return response(snap());};
for(const hash of ['#/','#/projects','#/projects/p','#/threads?page=3','#/threads/p/same','#/people','#/people/agent','#/tasks/agent']){
location.hash=hash;await load();const url=urls.at(-1);assert(url.startsWith('/api/page?'));assert.equal(urls.length,1);urls=[];
if(hash.includes('page=3'))assert.equal(new URLSearchParams(url.split('?')[1]).get('page'),'3');
}
})();
''')

    def test_idle_render_and_visibility_do_not_fetch_content(self):
        self.run_dom(r'''
(async()=>{
let reads=0,beats=0;fetch=async url=>{if(url.startsWith('/api/activity')){beats++;return response({runs:[]});}reads++;return response(snap());};
await load();assert.equal(reads,1);render();messageFilter='PROPOSAL';render();threadQuery='typed';render();mobileThreads=true;render();
await pollActivity();assert.equal(beats,1);
const beat=timers.find(t=>!t.cleared&&t.delay===2000);beat.fn();for(let i=0;i<10;i++)await Promise.resolve();
assert.equal(reads,1);assert.equal(beats,2);
document.hidden=true;document.dispatch('visibilitychange');document.hidden=false;document.dispatch('visibilitychange');
for(let i=0;i<10;i++)await Promise.resolve();assert.equal(reads,1);
})();
''')

    def test_pagination_twenty_items_total_and_disabled_bounds(self):
        self.run_dom(r'''
snapshot.page={number:1,size:20,total:41,total_pages:3};location.hash='#/threads';render();
let buttons=document.querySelector('.pagination').querySelectorAll('button');assert(buttons[0].disabled);assert(!buttons[1].disabled);
buttons[1].dispatch('click');assert.equal(location.hash,'#/threads?page=2');
snapshot.page.number=3;render();buttons=document.querySelector('.pagination').querySelectorAll('button');assert(!buttons[0].disabled);assert(buttons[1].disabled);
assert(root.textContent.includes('共 41 条'));
''')

    def test_navigation_aborts_old_and_late_response_cannot_write_cache(self):
        self.run_dom(r'''
(async()=>{
const waiting=[];fetch=(url,options)=>new Promise(resolve=>waiting.push({url,options,resolve}));
location.hash='#/threads?page=1';const first=load();const oldKey=pageCacheKey();
location.hash='#/threads?page=2';const second=load();assert(waiting[0].options.signal.aborted);
waiting[1].resolve(response(snap([thread('second')])));await second;
waiting[0].resolve(response(snap([thread('late')])));await first;
assert.equal(snapshot.projects[0].id,'second');assert.equal(readPageCache(oldKey),null);
})();
''')

    def test_search_is_explicit_submit_not_keystroke_and_resets_page(self):
        self.run_dom(r'''
location.hash='#/threads?page=4';render();let count=0;fetch=()=>{count++;};
const input=document.querySelector('#thread-search');input.dispatch('input',{target:{value:'hello'}});assert.equal(count,0);
input.closest('form').dispatch('submit',{preventDefault(){}});assert.equal(location.hash,'#/threads?page=1&q=hello');
''')

    def test_activity_scopes_to_displayed_projects_and_empty_scope_skips_fetch(self):
        self.run_dom(r'''
(async()=>{
let urls=[];fetch=async url=>{urls.push(url);return response({runs:[]});};location.hash='#/threads';snapshot=snap([thread('p'),thread('q')]);snapshot.projects.push({id:'empty',threads:[]});await pollActivity();
assert.deepEqual([...new URLSearchParams(urls[0].split('?')[1]).getAll('project_id')],['p','q']);
snapshot=snap([]);await pollActivity();assert.equal(urls.length,1);
})();
''')

    def test_no_progressive_protocol_is_left_in_ui(self):
        from pathlib import Path
        source=(Path(__file__).resolve().parents[1]/'ui/app.js').read_text()
        for retired in ('/api/snapshot', 'mergeSnapshot', 'appendSnapshotBatch', 'snapshotProgress', 'completedVersion'):
            self.assertNotIn(retired,source)

    def test_overview_avoids_unused_activity_and_marks_truncated_attention(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/';snapshot=snap();snapshot.overview={pending:25,attention:Array.from({length:20},()=>({project_id:'p',thread_id:'same'}))};render();
assert(root.textContent.includes('需要处理（显示前 20 条）'));let calls=0;fetch=()=>{calls++;};await pollActivity();assert.equal(calls,0);
})();
''')
