"""No polling: production UI network counters and versioned delta refresh."""
import unittest
import test_ui_refresh as refresh

HELPERS = r'''
const refs=content=>content.projects.flatMap(p=>p.threads.map(t=>({project_id:p.id,thread_id:t.thread_id})));
const whole=(content,version='v1')=>({...content,progress:{loaded:refs(content).length,total:refs(content).length,complete:true,cursor:null,delta:true},sync:{reset:true,base_version:null,version,changed:refs(content),removed:[],project_ids:content.projects.map(p=>p.id),summary:{collaborators:content.collaborators,overview:content.overview}}});
const unchanged=(content,base='v1',version='v2')=>({...content,projects:content.projects.map(p=>({...p,threads:[]})),progress:{loaded:0,total:0,complete:true,cursor:null,delta:true},sync:{reset:false,base_version:base,version,changed:[],removed:[],project_ids:content.projects.map(p=>p.id),summary:{collaborators:content.collaborators,overview:content.overview}}});
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
'''


@unittest.skipUnless(refresh.NODE, 'Node.js required')
class UiTriggeredTests(unittest.TestCase):
    def run_dom(self, code):
        refresh.UiRefreshTests.run_dom(self, HELPERS + code)

    def test_completed_scan_does_not_repeat_when_timers_advance_activity_is_separate(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/';snapshot=null;let reads=0,beats=0;
fetch=async url=>{if(url==='/api/activity'){beats++;return response({runs:[]});}reads++;return response(whole(snap()));};
await load();assert.equal(reads,1);
for(const timer of [...timers])if(!timer.cleared){timer.cleared=true;timer.fn();}await flush();assert.equal(reads,1);
await pollActivity();assert.equal(beats,1);
const heartbeat=timers.find(t=>!t.cleared&&t.delay===2000);assert(heartbeat);heartbeat.fn();await flush();
assert.equal(beats,2);assert.equal(reads,1);assert(!timers.some(t=>!t.cleared&&t.delay===3000));
})();
''')

    def test_render_filter_expansion_search_mobile_and_hidden_show_do_not_read_content(self):
        self.run_dom(r'''
(async()=>{
let calls=[];fetch=async url=>{calls.push(url);return response(url==='/api/activity'?{runs:[]}:url.startsWith('/api/snapshot')?whole(snap()):{thread:thread()});};
await load();const baseline=calls.filter(url=>url!=='/api/activity').length;
render();messageFilter='PROPOSAL';render();threadQuery='p';render();mobileThreads=true;render();
const disclosure=document.querySelector('.message')?.querySelector('details');if(disclosure){disclosure.open=true;disclosure.dispatch('toggle');}
taskCategory='waiting-human';render();selectIdentity('agent'); // hash is navigation, but setter itself must not fetch
assert.equal(calls.filter(url=>url!=='/api/activity').length,baseline);
document.hidden=true;document.dispatch('visibilitychange');document.hidden=false;document.dispatch('visibilitychange');await flush();
window.dispatch('pagehide');window.dispatch('pageshow',{persisted:true});await flush();
assert.equal(calls.filter(url=>url!=='/api/activity').length,baseline);
})();
''')

    def test_navigation_and_refresh_reuse_completed_version_and_unchanged_detail_cache(self):
        self.run_dom(r'''
(async()=>{
const data=snap([thread('p'),thread('q')]);let snapshotReads=0,detailReads=0;const urls=[];
fetch=async url=>{urls.push(url);if(url.startsWith('/api/thread')){detailReads++;return response({thread:thread(new URLSearchParams(url.split('?')[1]).get('project_id'))});}
snapshotReads++;return response(snapshotReads===1?whole(data):unchanged(data,completedVersion,'v'+snapshotReads));};
await load();assert.equal(detailReads,1);await load();assert.equal(detailReads,1);assert(urls.some(url=>url.includes('since=v1')));
location.hash='#/threads/q/same';window.dispatch('hashchange');await flush();assert.equal(snapshotReads,3);assert.equal(detailReads,2);
location.hash='#/threads/p/same';window.dispatch('hashchange');await flush();assert.equal(snapshotReads,4);assert.equal(detailReads,2);
assert.equal(details.get(routeKey('q','same')).project_id,'q');
})();
''')

    def test_concurrent_navigation_shares_scan_and_deduplicates_route_requests(self):
        self.run_dom(r'''
(async()=>{
snapshot=snap([thread('p'),thread('q')]);location.hash='#/';let release,snapshotReads=0,detailReads=0,finishDetail;
fetch=url=>{if(url.startsWith('/api/snapshot')){snapshotReads++;return new Promise(resolve=>{release=resolve;});}
detailReads++;if(detailReads>1)return Promise.resolve(response({thread:thread('q')}));return new Promise(resolve=>{finishDetail=resolve;});};
const reading=load();location.hash='#/threads/q/same';window.dispatch('hashchange');window.dispatch('hashchange');await flush();
assert.equal(snapshotReads,1);assert.equal(detailReads,1);
finishDetail(response({thread:thread('q')}));await flush();
release(response(whole(snap([thread('p'),thread('q')]))));await reading;
assert.equal(snapshotReads,1);assert.equal(route().projectId,'q');
})();
''')

    def test_partial_failure_does_not_advance_version_or_delete_and_retry_reconciles(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';const old=snap([thread('p'),thread('deleted')]);fetch=async()=>response(whole(old));await load();
const updated={...thread('p'),topic:'new topic'}, fresh=snap([updated,thread('added')]);let calls=0;
const shell={...snap([]),projects:fresh.projects.map(p=>({...p,threads:[]})),progress:{loaded:0,total:2,complete:false,cursor:'first',delta:true},sync:{reset:false,base_version:'v1',version:null,changed:refs(fresh),removed:[{project_id:'deleted',thread_id:'same'}],project_ids:['p','added']}};
fetch=async url=>{if(++calls===1){assert(url.includes('since=v1'));return response(shell);}if(calls===2)return response({...snap([updated]),progress:{loaded:1,total:2,complete:false,cursor:'last',delta:true},sync:{reset:false,base_version:'v1',version:null}});throw Error('partial offline');};
await load();assert.equal(completedVersion,'v1');assert(findThread('deleted','same'));assert.equal(findThread('p','same').topic,'new topic');
calls=0;fetch=async url=>{if(++calls===1){assert(url.includes('since=v1'));return response(shell);}return response({...fresh,progress:{loaded:2,total:2,complete:true,cursor:null,delta:true},sync:{reset:false,base_version:'v1',version:'v2',summary:{collaborators:[],overview:{threads:2}}}});};
await load();assert.equal(completedVersion,'v2');assert.equal(findThread('deleted','same'),null);assert(findThread('added','same'));assert.equal(allThreads().length,2);
})();
''')

    def test_expired_baseline_and_full_browser_reload_reset_explicitly(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';fetch=async()=>response(whole(snap([thread('old')])));await load();
fetch=async url=>{assert(url.includes('since=v1'));return response(whole(snap([thread('new')]),'v2'));};await load();
assert.equal(findThread('old','same'),null);assert.equal(completedVersion,'v2');
snapshot=null;completedSnapshot=null;completedVersion=null;details.clear();tasks.clear();
fetch=async url=>{assert(!url.includes('since='));return response(whole(snap([thread('fresh')]),'v3'));};await load();assert.equal(completedVersion,'v3');
})();
''')

    def test_new_change_set_invalidates_older_pending_detail_before_promoting_version(self):
        self.run_dom(r'''
(async()=>{
const base=snap([thread('p'),thread('q')]);location.hash='#/';fetch=async()=>response(whole(base));await load();
let shell,last,oldDetail,newDetail;let snapshots=0,detailReads=0;
fetch=url=>{if(url.startsWith('/api/snapshot'))return new Promise(resolve=>{if(++snapshots===1)shell=resolve;else last=resolve;});
return new Promise(resolve=>{if(++detailReads===1)oldDetail=resolve;else newDetail=resolve;});};
const reading=load();location.hash='#/threads/q/same';window.dispatch('hashchange');await flush();assert.equal(detailReads,1);
shell(response({...base,projects:base.projects.map(p=>({...p,threads:[]})),progress:{loaded:0,total:1,complete:false,cursor:'changed',delta:true},sync:{reset:false,base_version:'v1',version:null,changed:[{project_id:'q',thread_id:'same'}],removed:[],project_ids:['p','q']}}));await flush();assert.equal(detailReads,2);
oldDetail(response({thread:thread('q',[entry('stale')])}));await flush();
newDetail(response({thread:thread('q',[entry('fresh')])}));await flush();
last(response({...snap([thread('q')]),progress:{loaded:1,total:1,complete:true,cursor:null,delta:true},sync:{reset:false,base_version:'v1',version:'v2',summary:{collaborators:[],overview:{}}}}));await reading;
assert.equal(details.get(routeKey('q','same')).history[0].id,'fresh');assert.equal(completedVersion,'v2');assert(!detailDirty.has(routeKey('q','same')));
})();
''')

    def test_real_backend_incremental_change_and_delete_match_full_snapshot(self):
        import json
        import shutil
        from unittest import mock
        import test_ui_batches as batches
        fixture = batches.ProgressiveSnapshotTests()
        fixture.setUp()
        try:
            root = fixture.project('project', 2)
            server = batches.ui_server
            first = [fixture.start()]
            while not first[-1]['progress']['complete']:
                first.append(server.progressive_snapshot(first[-1]['progress']['cursor']))
            version = first[-1]['sync']['version']
            directory = root / '.ai-collab/threads'
            manifest = json.loads((directory / 'thread-000/thread.json').read_text())
            manifest['topic'] = 'changed topic'
            (directory / 'thread-000/thread.json').write_text(json.dumps(manifest))
            shutil.rmtree(directory / 'thread-001')
            manifest['thread_id'] = 'thread-002'
            (directory / 'thread-002').mkdir()
            (directory / 'thread-002/thread.json').write_text(json.dumps(manifest))
            with mock.patch.object(server, 'scan_thread', wraps=server.scan_thread) as scan:
                second = [server.progressive_snapshot(path=fixture.registry, since=version)]
                while not second[-1]['progress']['complete']:
                    second.append(server.progressive_snapshot(second[-1]['progress']['cursor']))
                self.assertEqual(scan.call_count, 2)
                same = server.progressive_snapshot(path=fixture.registry, since=second[-1]['sync']['version'])
                self.assertEqual(scan.call_count, 2)
                self.assertTrue(same['progress']['complete'])
                self.assertEqual(same['sync']['changed'], [])
            expected = server.snapshot(fixture.registry)
            expected.pop('generated_at')
            self.run_dom('const first=' + json.dumps(first) + ';const second=' + json.dumps(second) + ';const same=' + json.dumps(same) + ';const expected=' + json.dumps(expected) + r''';
(async()=>{
location.hash='#/';snapshot=null;let pages=first;fetch=async()=>response(pages.shift());await load();const oldVersion=completedVersion;
pages=second;fetch=async url=>{if(!url.includes('cursor='))assert(url.includes('since='+oldVersion));return response(pages.shift());};await load();
fetch=async()=>response(same);await load();
const {generated_at,...actual}=snapshot;
const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
assert.equal(JSON.stringify(canonical(actual)),JSON.stringify(canonical(expected)));
assert.equal(allThreads().length,2);assert(allThreads().every(t=>t.thread_id!=='thread-001'));
})();
''')
        finally:
            fixture.tearDown()

    def test_capacity_error_is_explicit_preserves_baseline_and_does_not_retry_itself(self):
        self.run_dom(r'''
(async()=>{
location.hash='#/threads';fetch=async()=>response(whole(snap()));await load();
let reads=0;fetch=async()=>{reads++;return {ok:false,status:413};};await load();
assert(error.includes('缓存容量'));assert.equal(completedVersion,'v1');assert.equal(allThreads().length,1);
for(const timer of [...timers])if(!timer.cleared){timer.cleared=true;timer.fn();}await flush();assert.equal(reads,1);
})();
''')
