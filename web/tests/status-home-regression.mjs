import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium, expect } from '@playwright/test';

const webRoot = resolve(process.argv[2]);
const full = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
full.human_collaboration.human_requests.items = [];
full.query_warnings = [{section:'bundle_stage',code:'target_frontier_integrity_invalid'}];
full.observed_at = '2026-09-06T14:00:00+00:00';
const foreground = full.research_control.foreground;
const value = {
  schema_ref:'meta-research/runtime-status/v1', revision:2000,
  observed_at:'2026-09-06T14:00:00+00:00', updated_at:'2026-09-06T13:59:40+00:00',
  state:'running', current_task:{kind:'target',title:'T2 训练完整性验证',run_ref:'run_t2',target_ref:'target_t2',status:'running'},
  waiting_reason:null, pending_requests:0, foreground,
  health:{status:'ready',checks:[{name:'bundle_stage_worker',status:'ready'},{name:'target_run_worker',status:'ready'}]},
};
let failed = false, delay = 0, hanging = false;
let activeStatus = 0, maxStatus = 0, activeSnapshot = 0, maxSnapshot = 0;
const requests = [], headers = [], errors = [];
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://fixture');
  const path = url.pathname;
  const send = (payload,code=200) => { res.writeHead(code,{'Content-Type':'application/json'}); res.end(JSON.stringify(payload)); };
  if (path.startsWith('/api/')) {
    requests.push(path);
    if (req.method !== 'GET') return send({},405);
    if (path === '/api/v1/status') {
      activeStatus++; maxStatus=Math.max(maxStatus,activeStatus);
      let finished=false;
      const end=()=>{if(!finished){finished=true;activeStatus--;}};
      res.on('close',end);
      if (hanging) return;
      await new Promise(done=>setTimeout(done,delay));
      send(value,failed?503:200); end(); return;
    }
    if (path === '/api/v1/snapshot') {
      headers.push(req.headers);
      activeSnapshot++; maxSnapshot=Math.max(maxSnapshot,activeSnapshot);
      await new Promise(done=>setTimeout(done,250));
      send(full); activeSnapshot--; return;
    }
    if (path === '/api/v1/research-overview') return send({
      schema_ref:'meta-research/research-overview/v1',status:'ready',...foreground,
      cycle_ordinal:1,foreground,findings:{quest:[],question:[],cycle:[]},cycles:[],reason:null,
    });
    if (path === '/api/v1/research-assets') return send(full.research_assets);
    if (path === '/api/v1/health') return send(value.health);
    if (path.includes('projection') || path.includes('events')) {
      res.writeHead(200,{'Content-Type':'text/event-stream'}); res.write(': fixture stream\n\n'); return;
    }
    return send({},404);
  }
  try {
    const file=resolve(webRoot,path.startsWith('/assets/')?`.${path}`:'index.html');
    assert.ok(file.startsWith(webRoot+'/'));
    res.setHeader('Content-Type',{'.js':'application/javascript','.css':'text/css','.html':'text/html'}[extname(file)]??'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
await context.route('**/*', route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
const page=await context.newPage();
page.on('pageerror',error=>errors.push(error.message));
const checks=[];
try {
  await page.goto(base);
  await expect(page.getByRole('heading',{name:'T2 训练完整性验证'})).toBeVisible();
  await page.screenshot({path:new URL('./status-home-desktop.png',import.meta.url).pathname});
  await new Promise(done=>setTimeout(done,5500));
  assert.deepEqual([...new Set(requests)],['/api/v1/status']);
  assert.equal(maxStatus,1);
  checks.push('home performs only bounded status GETs, including StrictMode remount');
  delay=1500;
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await expect(page.getByRole('button',{name:'刷新中…',exact:true})).toBeDisabled();
  await expect(page.getByRole('button',{name:'刷新状态',exact:true})).toBeEnabled();
  assert.equal(maxStatus,1);
  checks.push('slow refresh does not overlap or clear the last result');
  delay=0;failed=true;
  const timestamp=await page.locator('.status-home-footer time').innerText();
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('保留上次成功读取的状态');
  await expect(page.getByRole('heading',{name:'T2 训练完整性验证'})).toBeVisible();
  assert.equal(await page.locator('.status-home-footer time').innerText(),timestamp);
  checks.push('failed refresh keeps a clearly stale result and its original timestamp');
  failed=false;hanging=true;
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await expect(page.getByRole('button',{name:'刷新状态',exact:true})).toBeEnabled({timeout:5500});
  await expect(page.getByRole('alert')).toBeVisible();
  assert.equal(maxStatus,1);
  hanging=false;
  await page.getByRole('button',{name:'刷新状态',exact:true}).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  checks.push('hung status request times out, then recovers without a reload');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:new URL('./status-home-mobile.png',import.meta.url).pathname,fullPage:true});
  checks.push('mobile layout fits the viewport');
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole('link',{name:/当前任务与实验日志/}).click();
  await expect(page.locator('[data-testid="current-cycle-overview"]')).toBeVisible();
  await new Promise(done=>setTimeout(done,3000));
  assert.equal(headers.length,1,'opening details must not enqueue an identical second refresh');
  assert.ok(headers.length);
  assert.equal(headers[0]['x-meta-research-snapshot-assets'],'defer');
  assert.equal(headers[0]['x-meta-research-snapshot-history'],'defer');
  assert.equal(requests.filter(path=>path==='/api/v1/research-overview').length,1,
    'current Cycle ordinal should be fetched once while detailed history stays closed');
  await expect(page.locator('[data-testid="workspace-partial-warning"]')).toContainText('部分执行状态暂不可用');
  await expect(page.locator('[data-testid="product-shell"]')).toHaveAttribute('data-shell-state','ready-active');
  checks.push('partial execution failure retains a usable workspace and a visible warning');
  await page.getByRole('button',{name:'查看本轮记录与历史结果'}).click();
  await expect.poll(()=>requests.filter(path=>path==='/api/v1/research-overview').length).toBe(2);
  assert.equal(maxSnapshot,1);
  checks.push('current Cycle loads once; assets/history stay deferred until opened');
  await page.goto(base+'/?panel=research-assets');
  await expect.poll(()=>headers.at(-1)?.['x-meta-research-snapshot-assets']).toBe('include');
  assert.equal(maxSnapshot,1);
  checks.push('resource entry requests its asset section on demand');
  failed=true;
  await page.goto(base);
  await expect(page.getByRole('heading',{name:'暂时无法读取运行状态'})).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('尚未取得有效状态');
  assert.deepEqual(errors,[]);
  checks.push('first-load failure reports unknown status; no browser exceptions');
  console.log(JSON.stringify({status:'passed',checks,maxConcurrentStatus:maxStatus,maxConcurrentSnapshot:maxSnapshot}));
} finally {
  await context.close(); await browser.close();
  server.closeAllConnections(); await new Promise(done=>server.close(done));
}
