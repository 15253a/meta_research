import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Use the actual built application with synthetic read-only research responses.
// A live output channel deliberately belongs to an exited provider: channel state
// alone must not be presented as a running training process.
const webRoot = resolve(process.argv[2]);
const artifactRoot = process.argv[3] ? resolve(process.argv[3]) : null;
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
let train = 'TRAIN_FILE_FIXTURE: epoch 1 loss=0.7\n';
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
        aggregated_output:current===target?'T5_PREPARATION_RECORD: provider exited 101; training not started\n'+rawSuffix:'T6_PREPARATION_RECORD: own workspace only\n',
        status:'completed',exit_code:101}})+'\n';
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
  await expect(files).toContainText('尚未发现 train/eval 命名的日志文件');
  await expect(files.getByRole('button',{name:'日志文件',exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(files.getByRole('button',{name:'查看任务执行记录',exact:true})).toBeVisible();
  assert.equal(requests.some(path=>/\/bundle\/targets\/[^/]+\/raw-output/.test(path)),false);
  checks.push('empty file catalog gives clear execution-record entry without inventing training logs');
  pendingRawHangs=1;
  await files.getByRole('button',{name:'查看任务执行记录',exact:true}).click();
  await expect(files).toHaveCount(0);
  await expect.poll(()=>records.innerText(),{timeout:12000,intervals:[100]}).toContain('任务记录读取超时');
  await expect(records.getByRole('log')).toHaveCount(0);
  await expect(records).toContainText('T5_PREPARATION_RECORD',{timeout:7000});
  await expect(records).not.toContainText('target_raw_output_timeout');
  checks.push('first raw read reaches 8s deadline and automatically recovers');
  pendingRawHangs=2;rawSuffix='RAW_RECORDS_AUTO_RECOVERED\n';
  await expect.poll(()=>records.innerText(),{timeout:14000,intervals:[100]}).toContain('任务记录读取超时');
  await expect(records.getByRole('log')).toContainText('T5_PREPARATION_RECORD');
  await expect.poll(()=>rawHangs,{timeout:14000,intervals:[100]}).toBe(3);
  await expect(records.getByRole('log')).toContainText('RAW_RECORDS_AUTO_RECOVERED',{timeout:14000});
  await expect(records).not.toContainText('target_raw_output_timeout');
  checks.push('two consecutive hung raw refreshes preserve visible records and auto-recover without reload');
  await expect(records).toContainText('尚未收到结束记录');await expect(records.locator('header')).not.toContainText('持续更新');
  await expect(records).not.toContainText('TRAIN_FILE_FIXTURE');
  await records.getByRole('button',{name:'日志文件',exact:true}).click();
  await expect(records).toHaveCount(0);await expect(files).toContainText('尚未发现 train/eval 命名的日志文件');
  checks.push('same TargetRun switches both ways; live channel with failed provider does not claim running training');
  empty=false;
  await expect(files.getByRole('log')).toContainText('TRAIN_FILE_FIXTURE',{timeout:7000});
  train+='FILE_LIVE_APPEND\n';await expect(files.getByRole('log')).toContainText('FILE_LIVE_APPEND',{timeout:7000});
  checks.push('real train file discovery and incremental append continue after switching back');
  hangCatalog=true;
  await expect(files).toContainText('读取日志超时',{timeout:14000});
  await expect(files.getByRole('log')).toContainText('FILE_LIVE_APPEND');
  hangCatalog=false;train+='CATALOG_AUTO_RECOVERED\n';
  await expect(files.getByRole('log')).toContainText('CATALOG_AUTO_RECOVERED',{timeout:7000});
  await expect(files).not.toContainText('读取日志超时');
  checks.push('hung catalog reaches 8s deadline, preserves text, and automatically recovers');
  hangPage=true;
  await expect(files).toContainText('读取日志超时',{timeout:14000});
  await expect(files.getByRole('log')).toContainText('CATALOG_AUTO_RECOVERED');
  hangPage=false;train+='PAGE_AUTO_RECOVERED\n';
  await expect(files.getByRole('log')).toContainText('PAGE_AUTO_RECOVERED',{timeout:7000});
  checks.push('hung file body/page also times out and resumes without reload or lost content');
  await files.getByRole('button',{name:'任务执行记录',exact:true}).click();
  await expect(records).toContainText('T5_PREPARATION_RECORD');
  await page.getByRole('combobox',{name:'选择实验任务',exact:true}).selectOption(second.target_ref);
  await expect(records).toContainText('T6_PREPARATION_RECORD');await expect(records).not.toContainText('T5_PREPARATION_RECORD');
  await records.getByRole('button',{name:'日志文件',exact:true}).click();
  await expect(files.getByRole('log')).toContainText('SECOND_TARGET_TRAIN');await expect(files).not.toContainText('TRAIN_FILE_FIXTURE');
  checks.push('target switch resets both raw and file records with exact TargetRun scope');
  if (artifactRoot) {await mkdir(artifactRoot,{recursive:true});await page.screenshot({path:resolve(artifactRoot,'experiment-output-files-desktop.png')});}
  await page.setViewportSize({width:390,height:844});
  await expect(files.getByRole('button',{name:'任务执行记录',exact:true})).toBeVisible();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const filesBox=await files.boundingBox();assert.ok(filesBox.width<=390&&filesBox.height<=824);
  if (artifactRoot) await page.screenshot({path:resolve(artifactRoot,'experiment-output-files-mobile.png')});
  await files.getByRole('button',{name:'任务执行记录',exact:true}).click();
  await expect(records).toContainText('T6_PREPARATION_RECORD');
  await expect(records.getByRole('button',{name:'日志文件',exact:true})).toBeVisible();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  if (artifactRoot) await page.screenshot({path:resolve(artifactRoot,'experiment-output-records-mobile.png')});
  await page.keyboard.press('Escape');await expect(records).toHaveCount(0);await expect(launcher).toBeFocused();
  checks.push('both views fit mobile; Escape closes execution records and restores the launcher');
  assert.ok(maximumLogRequests<=1,`overlapping log requests: ${maximumLogRequests}`);assert.ok(maximumRawRequests<=1,`overlapping raw requests: ${maximumRawRequests}`);assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  checks.push('single-flight log requests, zero research writes, zero browser errors');
  console.log(JSON.stringify({status:'passed',checks,maximumLogRequests,maximumRawRequests,researchWrites:writes.length},null,2));
} finally {
  await context.close();await browser.close();server.closeAllConnections();await new Promise(done=>server.close(done));
}
