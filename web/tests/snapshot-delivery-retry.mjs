import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { chromium, expect } from '@playwright/test';

const webRoot = resolve(process.argv[2]);
const full = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
let requests = 0, active = 0, maxActive = 0, writes = 0, scenario = 'timeout';
const headers = [], starts = [];
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  const send = (value, status = 200, extra = {}) => {
    if (!res.destroyed) res.writeHead(status, {'Content-Type':'application/json', ...extra}).end(JSON.stringify(value));
  };
  if (path.startsWith('/api/')) {
    if (req.method !== 'GET') {writes++;return send({},405);}
    if (path === '/api/v1/snapshot') {
      const ordinal = ++requests; active++; maxActive = Math.max(maxActive,active);
      starts.push(Date.now());
      res.once('close',()=>active--);
      headers.push([req.headers['x-meta-research-snapshot-assets'],req.headers['x-meta-research-snapshot-history']]);
      if (scenario === 'hang') return;
      if (scenario === 'timeout' && ordinal <= 2 || scenario === 'cancel') {
        await new Promise(done=>setTimeout(done,180));
        return send({detail:{code:'snapshot_query_timeout'}},503,{'Retry-After':'.25'});
      }
      return send(full);
    }
    if (path.includes('events') || path.includes('projection')) {
      res.writeHead(200,{'Content-Type':'text/event-stream'});return res.write(': fixture\n\n');
    }
    return send({},404);
  }
  try {
    const file = resolve(webRoot,path.startsWith('/assets/')?`.${path}`:'index.html');
    assert.ok(file.startsWith(webRoot+sep));
    res.setHeader('Content-Type',{'.js':'application/javascript','.css':'text/css','.html':'text/html'}[extname(file)]??'application/octet-stream');
    res.end(await readFile(file));
  } catch {res.writeHead(404).end();}
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext();
await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
await context.addInitScript(()=>{
  // Scale only the snapshot deadline; server delays model a read finishing past
  // that original budget, and keep the production Retry-After delay intact.
  const timer=window.setTimeout;
  window.setTimeout=(callback, delay, ...args)=>timer(callback,delay>=22000&&delay<=27000?delay-22000+220:delay,...args);
  window.snapshotErrorStates=[];
  new MutationObserver(()=>{
    const state=document.querySelector('[data-testid="product-shell"]')?.getAttribute('data-shell-state');
    if(state==='first-error') window.snapshotErrorStates.push(state);
  }).observe(document,{subtree:true,attributes:true,childList:true});
});
const page=await context.newPage(),errors=[];
page.on('pageerror',error=>errors.push(error.message));
try {
  await page.goto(base+'/?workspace=1');
  await expect(page.locator('[data-testid="product-shell"]')).toHaveAttribute('data-shell-state','ready-active',{timeout:2500});
  assert.equal(requests,3);
  assert.deepEqual(await page.evaluate(()=>window.snapshotErrorStates),[]);
  assert.ok(headers.every(item=>item[0]==='defer'&&item[1]==='defer'));
  assert.equal(maxActive,1);
  assert.ok(starts[1]-starts[0]>=400 && starts[2]-starts[1]>=400, 'Retry-After must pace subsequent requests');
  scenario='cancel';requests=0;
  await page.goto(base+'/?workspace=1');
  await expect.poll(()=>requests).toBe(1);
  await new Promise(done=>setTimeout(done,200));
  await page.goto(base+'/not-a-workspace');
  const after=requests;
  await new Promise(done=>setTimeout(done,400));
  assert.equal(requests,after);
  scenario='hang';requests=0;
  await page.goto(base+'/?workspace=1');
  await expect(page.locator('[data-testid="product-shell"]')).toHaveAttribute('data-shell-state','first-error',{timeout:1500});
  assert.equal(requests,1);
  assert.equal(writes,0);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',checks:['timeout retry survives the original request budget without an empty error page','deferred headers and single in-flight request retained','Retry-After paces retries; navigation abort cancels waiting','a hanging network request still reaches its single-request deadline'],writes,maxActive}));
} finally {await context.close();await browser.close();server.closeAllConnections();await new Promise(done=>server.close(done));}
