import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { chromium, expect } from '@playwright/test';

const webRoot = resolve(process.argv[2] || '../../bundle-snapshot-delivery-20260910/frontend-build');
const full = await readFile('tests/snapshot-before.json', 'utf8');
let scenario = '', ordinal = 0, events = [], writes = 0, ready = false;
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  const send = (value, status = 200, extra = {}) => {
    if (!res.destroyed) res.writeHead(status, {'Content-Type':'application/json', ...extra}).end(value);
  };
  if (path.startsWith('/api/')) {
    if (req.method !== 'GET') { writes++; return send('{}', 405); }
    if (path === '/api/v1/snapshot') {
      const runScenario = scenario, started = Date.now(), call = ++ordinal;
      const observed = events;
      res.once('close', () => observed.push({event:'request_closed',call,ms:Date.now()-started,headersSent:res.headersSent}));
      if (runScenario === 'late-body') {
        res.writeHead(200, {'Content-Type':'application/json'});res.write(full.slice(0, 1));
        await new Promise(done=>setTimeout(done,350));
        if (!res.destroyed) res.end(full.slice(1));
        return;
      }
      if (!ready) {
        await new Promise(done=>setTimeout(done,call === 1 ? 350 : 150));
        observed.push({event:'attempted_503',call,ms:Date.now()-started,alreadyClosed:res.destroyed});
        return send(JSON.stringify({detail:{code:'snapshot_query_timeout'}}),503,{'Retry-After':'.25'});
      }
      return send(full);
    }
    if (path.includes('events') || path.includes('projection')) {
      res.writeHead(200, {'Content-Type':'text/event-stream'});return res.write(': fixture\n\n');
    }
    return send('{}',404);
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
try {
  for (const testScenario of ['retry-after-abort']) {
    scenario=testScenario;ordinal=0;events=[];
    const context=await browser.newContext();
    await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
    await context.addInitScript(()=>{
      const timer=window.setTimeout;
      window.setTimeout=(callback,delay,...args)=>timer(callback,delay===22000?220:delay,...args);
      const originalFetch=window.fetch;
      window.deliveryReadEvents=[];
      window.fetch=async (...args)=>{
        if (args[0] !== '/api/v1/snapshot') return originalFetch(...args);
        const start=performance.now();
        try {
          const response=await originalFetch(...args);
          window.deliveryReadEvents.push({event:'headers',status:response.status,ms:performance.now()-start});
          const originalJson=response.json.bind(response);
          response.json=async ()=>{
            try {return await originalJson();}
            catch(error){window.deliveryReadEvents.push({event:'body_error',name:error.name,ms:performance.now()-start});throw error;}
          };
          return response;
        } catch(error) {
          window.deliveryReadEvents.push({event:'fetch_error',name:error.name,ms:performance.now()-start});throw error;
        }
      };
    });
    const page=await context.newPage(),pageErrors=[];
    page.on('pageerror',error=>pageErrors.push(error.message));
    await page.goto(base+'/?workspace=1');
    const shell=page.locator('[data-testid="product-shell"]');
    await expect(shell).toHaveAttribute('data-shell-state','first-error',{timeout:1500});
    await expect.poll(()=>ordinal,{timeout:7000}).toBeGreaterThanOrEqual(3);
    // The second fetchSnapshot is now retrying explicit busy responses. It must
    // display its current read state, rather than the first request's failure.
    await expect(shell).toHaveAttribute('data-shell-state','loading',{timeout:700});
    const busyRequests=ordinal;
    ready=true;
    await expect(shell).toHaveAttribute('data-shell-state','ready-active',{timeout:1500});
    console.log(JSON.stringify({scenario:testScenario,busyRequests,shellState:'ready-active',events,browserEvents:await page.evaluate(()=>window.deliveryReadEvents),pageErrors,writes}));
    assert.deepEqual(pageErrors,[]);
    await context.close();
  }
  assert.equal(writes,0);
} finally {await browser.close();server.closeAllConnections();await new Promise(done=>server.close(done));}
