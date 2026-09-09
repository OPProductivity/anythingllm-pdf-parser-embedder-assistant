"""Execute the actual initial-page guard with deterministic JS clocks and faults."""
import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.offline_deterministic


HARNESS = r'''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const o = input.options;
let now=0, next=0, reloaded=0, fetches=0, full=!!o.full, controls=!!o.controls;
const timers=new Map(), nodes=new Map(), listeners=new Map(), observers=[];
const storage=new Map(o.attempted ? [['rag-initial-ui-retry','attempted']] : []);
class Element {
  constructor(){this.children=[];this.dataset={};}
  setAttribute(){}
  appendChild(node){this.children.push(node);if(node.id)nodes.set(node.id,node);return node;}
  append(...children){children.forEach(node=>this.appendChild(node));}
  remove(){nodes.delete(this.id);}
  addEventListener(){}
}
const document={body:o.bodyDelayed?null:new Element(),documentElement:{dataset:{}},
  createElement:()=>new Element(),getElementById:id=>nodes.get(id),
  querySelector:s=>s.includes(',') ? (controls||full ? {}:null) : (full?{}:null),
  addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},
  removeEventListener:(name,fn)=>listeners.get(name)?.delete(fn)};
function mutate(){for(const obs of [...observers])if(obs.active)obs.callback([]);}
function interact(){for(const fn of listeners.get('input')||[])fn({isTrusted:true});}
const window={setTimeout:(fn,ms)=>{const id=++next;timers.set(id,{fn,at:now+ms});return id;},
  clearTimeout:id=>timers.delete(id),location:{reload:()=>reloaded++}};
const context={window,document,Boolean,AbortController,
  requestAnimationFrame:fn=>window.setTimeout(fn,16),
  MutationObserver:class {constructor(callback){this.callback=callback;observers.push(this);}observe(){this.active=true;}disconnect(){this.active=false;}},
  sessionStorage:{getItem:key=>{if(o.storageBlocked)throw Error('blocked');return storage.get(key)||null;},
    setItem:(key,value)=>{if(o.storageBlocked)throw Error('blocked');storage.set(key,value);},removeItem:key=>storage.delete(key)},
  fetch:(_url,opts)=>{fetches++;
    if(o.mountDuringFetch){full=true;controls=true;mutate();}
    if(o.interactDuringFetch)interact();
    if(o.hungFetch)return new Promise((_resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Error('timeout'))));
    return Promise.resolve({ok:!o.offline});}};
vm.runInNewContext('('+input.guard+')()',context);
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
async function advance(until){let n=0;while(true){
  const due=[...timers].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
  if(!due)break;if(++n>1000)throw Error('timer loop');
  now=due[1].at;timers.delete(due[0]);due[1].fn();await flush();}
  now=until;await flush();}
(async()=>{
  // Deliberately never dispatch DOMContentLoaded: a deferred module is stuck.
  if(o.bodyDelayed){document.body=new Element();mutate();}
  if(o.interact)interact();
  if(o.churn){for(let i=1;i<100;i++){await advance(i*100);mutate();}}
  await advance(o.until||25000);
  if(o.lateMount){full=true;controls=true;mutate();await advance(26000);}
  const notice=nodes.get('rag-initial-ui-recovery');
  process.stdout.write(JSON.stringify({reloaded,fetches,state:document.documentElement.dataset.ragInitialUi,
    overlay:nodes.has('rag-initial-ui-overlay'),notice:!!notice,
    message:notice?.children[0]?.textContent||'',retryMarker:storage.get('rag-initial-ui-retry')||null,
    activeObservers:observers.filter(o=>o.active).length,
    listeners:[...listeners.values()].reduce((n,s)=>n+s.size,0)}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def probe(**options):
    from rag_pdf_gradio_app import APP_BROWSER_THEME_HEAD

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the browser guard')
    guard = APP_BROWSER_THEME_HEAD.split('const installInitialShellGuard = ', 1)[1]
    guard = guard.split('    installInitialShellGuard();', 1)[0].strip().removesuffix(';')
    result = subprocess.run([node, '-e', HARNESS], input=json.dumps({'guard':guard,'options':options}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('options', [{'full':True}, {'full':True,'churn':True}, {'full':True,'attempted':True}])
def test_success_disposes_initial_guard_without_health_requests(options):
    r=probe(**options)
    assert r['state']=='ready' and not r['overlay'] and not r['notice']
    assert r['reloaded']==r['fetches']==r['activeObservers']==r['listeners']==0
    assert r['retryMarker'] is None


def test_cover_deadline_does_not_claim_frontend_is_ready():
    r=probe(until=9000)
    assert r['state']=='waiting' and not r['overlay'] and r['reloaded']==0


def test_failed_mount_gets_one_bounded_retry():
    r=probe()
    assert r['state']=='retrying' and r['reloaded']==1 and r['fetches']==1
    assert r['retryMarker']=='attempted' and r['activeObservers']==r['listeners']==0


@pytest.mark.parametrize('full', [False, True])
def test_guard_starts_before_dom_content_loaded(full):
    r=probe(bodyDelayed=True,full=full)
    assert r['state']==('ready' if full else 'retrying')
    assert r['reloaded']==(0 if full else 1)
    assert r['activeObservers']==r['listeners']==0


@pytest.mark.parametrize('options', [{'attempted':True}, {'storageBlocked':True}, {'offline':True}, {'hungFetch':True}])
def test_no_reload_loop_or_retry_when_server_is_unavailable(options):
    r=probe(**options)
    assert r['reloaded']==0 and r['notice'] and r['state']=='failed'
    assert r['fetches']==1 and r['listeners']==0
    assert 'not responding' in r['message'] if options.get('offline') or options.get('hungFetch') else 'did not finish' in r['message']


@pytest.mark.parametrize('options', [{'controls':True}, {'interact':True}, {'mountDuringFetch':True}, {'interactDuringFetch':True}])
def test_partial_interface_or_user_activity_prevents_automatic_reload(options):
    r=probe(**options)
    assert r['reloaded']==0 and not r['notice'] and r['activeObservers']==r['listeners']==0


def test_late_controls_remove_failed_load_retry_notice():
    r=probe(attempted=True,lateMount=True)
    assert not r['notice'] and r['reloaded']==0 and r['activeObservers']==0


def test_only_successful_root_html_gets_uncached_policy():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, Response
    from fastapi.testclient import TestClient
    from rag_pdf_gradio_app import LocalServerConnectionWatchdogMiddleware

    app=FastAPI()
    app.add_middleware(LocalServerConnectionWatchdogMiddleware)

    @app.get('/')
    def root():
        return HTMLResponse('<html><head></head><body></body></html>', headers={
            'Cache-Control':'public, max-age=3600', 'ETag':'old', 'Last-Modified':'old', 'X-Test':'preserved'})

    @app.get('/assets/test.js')
    def asset():
        return Response('const value=1;',media_type='text/javascript',headers={'Cache-Control':'public, max-age=3600'})

    @app.get('/failure')
    def failure():
        return HTMLResponse('error',status_code=500)

    client=TestClient(app)
    result=client.get('/')
    assert result.headers['cache-control']=='no-store'
    assert 'etag' not in result.headers and 'last-modified' not in result.headers
    assert result.headers['x-test']=='preserved'
    assert result.text.count('id="rag-local-theme-controls"')==1
    static=client.get('/assets/test.js')
    assert static.text=='const value=1;'
    assert static.headers['cache-control']=='public, max-age=3600'
    assert client.get('/failure').status_code==500
