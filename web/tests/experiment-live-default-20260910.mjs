import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Use the actual built application with synthetic read-only research responses.
// A live output channel deliberately belongs to an exited provider: channel state
// alone must not be presented as a running training process.
const webRoot = resolve(process.argv[2]);
const artifactRoot = process.argv[3] && !process.argv[3].startsWith('--') ? resolve(process.argv[3]) : null;
const classificationOnly = process.argv.includes('--classification-only');
const snapshot = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
snapshot.human_collaboration.human_requests.items = [];
const target = snapshot.bundle_stage.target_graph.targets[0];
target.target_key = 'T5_preparation_fixture';
target.status = 'running';
target.blocker = null;
target.target_run_ref ||= 'target-run-fixture-5';
const second = {...structuredClone(target), target_ref: 'target-fixture-6', target_run_ref: 'target-run-fixture-6', target_key:'T6_independent_fixture'};
snapshot.bundle_stage.target_graph.targets = [target, second];
const targets = [target, second];
let empty = true, hangCatalog = false, hangPage = false;
let pendingRawHangs = 0, rawHangs = 0;
let rawSuffix = "";
let train = 'AUDIT_ONLY: source lineage checked; training_runs=0; evaluation_runs=0\n';
const requests = [], writes = [], errors = [];
let activeLogRequests = 0, maximumLogRequests = 0;
let activeRawRequests = 0, maximumRawRequests = 0;
const catalog = current => ({
  schema_ref:'meta-research/experiment-log-list/v1', target_ref:current.target_ref,
  target_run_ref:current.target_run_ref, workspace_ref:'workspace-'+current.target_ref,
  status:empty?'empty':'ready', default_log_ref:empty?null:'train', truncated:false, reason:null,
  logs:empty?[]:[{log_ref:'train',name:'train.log',relative_path:'logs/train.log',kind:'train',
    source_bytes:Buffer.byteLength(current===target?train:'SECOND_TARGET_TRAIN\n'),modified_at:1788760000}],
});
const server = createServer(async (req,res) => {
  const url = new URL(req.url,'http://fixture');
  const path = url.pathname;
  const send = (value,status=200) => {if (!res.destroyed) res.writeHead(status,{'Content-Type':'application/json'}).end(JSON.stringify(value));};
  if (path.startsWith('/api/')) {
    requests.push(path + url.search);
    if (req.method !== 'GET') {writes.push(path);return send({},405);}
    if (path === '/api/v1/snapshot') return send(snapshot);
    if (path === '/api/v1/research-overview') return send({schema_ref:'meta-research/research-overview/v1',status:'ready',...snapshot.research_control.foreground,
      cycle_ordinal:1,foreground:snapshot.research_control.foreground,findings:{quest:[],question:[],cycle:[]},cycles:[],reason:null});
    if (path === '/api/v1/research-assets') return send(snapshot.research_assets);
    if (path.includes('/events') || path.includes('/projection')) {
      res.writeHead(200,{'Content-Type':'text/event-stream'});res.write(': isolated fixture\n\n');return;
    }
    const match=path.match(/\/bundle\/targets\/([^/]+)\/(experiment-logs(?:\/train)?|raw-output)$/);
    if (!match) return send({},404);
    const current=targets.find(item=>item.target_ref===decodeURIComponent(match[1]));
    assert.ok(current,'request belongs to one of the fixture Targets');
    if (match[2] === 'raw-output') {
      activeRawRequests++;maximumRawRequests=Math.max(maximumRawRequests,activeRawRequests);
      let rawReleased=false;const releaseRaw=()=>{if(!rawReleased){rawReleased=true;activeRawRequests--;}};res.on('close',releaseRaw);
      // Hold exactly the requested responses, independent of UI assertion timing.
      if (pendingRawHangs > 0) {pendingRawHangs--;rawHangs++;return;}
      const full=JSON.stringify({type:'item.completed',item:{id:'fixture-command',type:'command_execution',command:'python prepare_inputs.py',
        aggregated_output:current===target?'T5_PREPARATION_RECORD: provider exited 101; training not started\n':'T6_PREPARATION_RECORD: own workspace only\n',
        status:'completed',exit_code:101}})+'\n'+(current===target?rawSuffix:'');
      const after=Number(url.searchParams.get('after')??0);const bytes=Buffer.from(full);const text=bytes.subarray(after).toString();
      const invocationHash=(current===target?'a':'b').repeat(64);
      return send({schema_ref:'meta-research/target-raw-output-page/v1',target_ref:current.target_ref,target_run_ref:current.target_run_ref,
        attempt_ref:'attempt-fixture',attempt_generation:1,root_session_ref:'root-fixture',native_session_ref:'native-fixture',root_native_session_ref:'native-fixture',
        fence_ref:'fence-fixture',operation_ref:'operation-'+current.target_ref,operation_generation:1,operation_status:'failed',operation_outcome_code:'provider_exit_evidence_missing',
        transport_invocation_hash:invocationHash,stream_ref:'target-raw-output:'+invocationHash,status:'live',text,offset:after,next_offset:bytes.length,
        mapped_bytes:bytes.length,source_bytes:bytes.length,has_more:false,source_caught_up:true,exact:true,unredacted:true});
    }
    assert.equal(url.searchParams.get('target_run_ref'),current.target_run_ref);
    activeLogRequests++;maximumLogRequests=Math.max(maximumLogRequests,activeLogRequests);
    let released=false;const release=()=>{if(!released){released=true;activeLogRequests--;}};res.on('close',release);
    if (match[2] === 'experiment-logs') {
      if (hangCatalog) return;
      send(catalog(current));release();return;
    }
    if (hangPage) return;
    const file=catalog(current).logs[0];
    const bytes=Buffer.from(current===target?train:'SECOND_TARGET_TRAIN\n');
    const after=Number(url.searchParams.get('after')??0);
    send({...file,schema_ref:'meta-research/experiment-log-page/v1',target_ref:current.target_ref,target_run_ref:current.target_run_ref,
      workspace_ref:'workspace-'+current.target_ref,stream_ref:'train:'+current.target_run_ref,text:bytes.subarray(after).toString(),offset:after,
      next_offset:bytes.length,source_bytes:bytes.length,has_more:false,source_caught_up:true,pending_utf8_bytes:0});release();return;
  }
  try {
    const file=resolve(webRoot,path.startsWith('/assets/')?`.${path}`:'index.html');assert.ok(file.startsWith(webRoot+'/'));
    res.setHeader('Content-Type',{'.js':'application/javascript','.css':'text/css','.html':'text/html'}[extname(file)]??'application/octet-stream');
    res.end(await readFile(file));
  } catch {res.writeHead(404).end();}
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:960},serviceWorkers:'block'});
await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
const checks=[];
try {
  await page.goto(base+'/?workspace=1',{waitUntil:'domcontentloaded'});
  const launcher=page.getByRole('button',{name:/⌘ 实验日志/});await expect(launcher).toBeEnabled();await launcher.click();
  const files=page.locator('#experiment-log-dialog');const records=page.locator('#target-output-dialog');
  if (classificationOnly) {
    empty = false;
    // Both old and corrected navigation reach the same raw-file assertion.
    await records.getByRole('button',{name:/^(日志文件|训练与评估日志)$/,exact:true}).click();
    await expect(files.getByRole('log')).toContainText('AUDIT_ONLY',{timeout:7000});
    assert.equal(await files.getByRole('log').textContent(), train);
    await expect(files.locator('.experiment-log-select select option:checked')).toHaveText('logs/train.log');
    await expect(files.locator('.experiment-log-source-note')).toHaveText('当前按 train/eval 文件名查找。文件名不代表训练或模型评估已经运行。此处展示文件的原始写入。');
    await files.getByRole('button',{name:'最小化实验日志',exact:true}).click();
    await expect(files.locator('.experiment-log-source-note')).toBeVisible();
    await expect(files.getByRole('button',{name:'关闭实验日志',exact:true})).toBeVisible();
    await page.setViewportSize({width:390,height:844});
    await expect(files.locator('.experiment-log-source-note')).toBeVisible();
    await expect(files.getByRole('button',{name:'关闭实验日志',exact:true})).toBeVisible();
    const small = await files.boundingBox();
    assert.ok(small.x >= 0 && small.x + small.width <= 391 && small.height < 250);
    await files.getByRole('button',{name:'关闭实验日志',exact:true}).click();
    await expect(files).toHaveCount(0);
    assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed',check:'raw audit-only train.log is named by path without inferred training classification',researchWrites:0}));
  } else {
  await expect(records).toBeVisible({timeout:5000});
  await expect(records).toContainText('T5_PREPARATION_RECORD',{timeout:7000});
  await expect(files).toHaveCount(0);
  checks.push('experiment entry defaults to real execution output while train/eval files are absent');
  rawSuffix=JSON.stringify({type:'item.completed',item:{id:'next-command',type:'command_execution',command:'python next_audit.py',aggregated_output:'PREPARATION_LIVE_APPEND\n',status:'completed',exit_code:0}})+'\n';
  await expect(records.getByRole('log')).toContainText('PREPARATION_LIVE_APPEND',{timeout:7000});
  checks.push('execution output grows without modal reopen or page reload');
  await records.getByRole('button',{name:'日志文件',exact:true}).click();
  await expect(files).toContainText('正在等待匹配的日志文件');
  await expect(files).not.toContainText('日志读取正常');
  const catalogReads=()=>requests.filter(path=>/\/experiment-logs\?/.test(path)).length;
  const beforeReads=catalogReads();
  await expect.poll(catalogReads,{timeout:7000}).toBeGreaterThan(beforeReads);
  checks.push('empty file catalog states waiting and continues polling');
  empty=false;
  await expect(files.getByRole('log')).toContainText('AUDIT_ONLY',{timeout:7000});
  await expect(files.locator('.experiment-log-header')).toContainText('日志文件 · 原始写入');
  await expect(files.locator('.experiment-log-source-note')).toHaveText('当前按 train/eval 文件名查找。文件名不代表训练或模型评估已经运行。此处展示文件的原始写入。');
  await expect(files.getByRole('combobox', {name:'选择日志文件',exact:true}).locator('option:checked')).toHaveText('logs/train.log');
  await expect(files.locator('.experiment-log-states')).toContainText('Target 状态：执行中');
  assert.equal(await files.getByRole('log').textContent(), train);
  await expect(files.getByRole('button',{name:'训练与评估日志',exact:true})).toHaveCount(0);
  checks.push('audit-only train.log stays raw text under a neutral filename without asserting training or evaluation');
  if (artifactRoot) {await mkdir(artifactRoot,{recursive:true});await page.screenshot({path:resolve(artifactRoot,'neutral-audit-file-desktop.png'),fullPage:true});}
  train+='FILE_LIVE_APPEND\n';
  await expect(files.getByRole('log')).toContainText('FILE_LIVE_APPEND',{timeout:7000});
  checks.push('newly created file is discovered and appended live while dialog remains open');
  await files.getByRole('button',{name:'任务执行记录',exact:true}).click();
  await expect(records).toContainText('PREPARATION_LIVE_APPEND');
  await page.getByRole('combobox',{name:'选择实验任务',exact:true}).selectOption(second.target_ref);
  await expect(records).toContainText('T6_PREPARATION_RECORD');await expect(records).not.toContainText('T5_PREPARATION_RECORD');
  await records.getByRole('button',{name:'日志文件',exact:true}).click();
  await expect(files.getByRole('log')).toContainText('SECOND_TARGET_TRAIN');await expect(files).not.toContainText('AUDIT_ONLY');
  checks.push('both views keep exact TargetRun identity on task switch');
  await page.keyboard.press('Escape');await expect(files).toHaveCount(0);
  await launcher.click();await expect(records).toContainText('T6_PREPARATION_RECORD');
  checks.push('reopened experiment entry starts with live execution output');
  assert.ok(maximumLogRequests<=1);assert.ok(maximumRawRequests<=1);assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',checks,maximumLogRequests,maximumRawRequests,researchWrites:writes.length},null,2));
  }
} finally {
  await context.close();await browser.close();server.closeAllConnections();await new Promise(done=>server.close(done));
}
