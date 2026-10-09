import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname, relative, isAbsolute } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Exercise the built public workspace UI with its existing read-only HTTP seam.
const webRoot = resolve(process.argv[2]);
const artifacts = process.argv[3] ? resolve(process.argv[3]) : null;
const snapshot = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
snapshot.human_collaboration.human_requests.items = [];
const first = snapshot.bundle_stage.target_graph.targets[0];
Object.assign(first, { target_key: 'Training A', status: 'running', blocker: null, target_run_ref: 'run-a' });
const second = { ...structuredClone(first), target_ref: 'target-b', target_run_ref: 'run-b', target_key: 'Training B' };
const empty = { ...structuredClone(first), target_ref: 'target-empty', target_run_ref: 'run-empty', target_key: 'No files yet' };
snapshot.bundle_stage.target_graph.targets = [first, second, empty];
const targets = [first, second, empty];
const texts = new Map([
  ['run-a:train', Array.from({ length: 3000 }, (_, i) => `A epoch ${String(i).padStart(4, '0')} loss=0.123456\n`).join('')],
  ['run-a:eval', 'A evaluation accuracy=0.93\n'],
  ['run-b:train', 'B epoch 0001 loss=0.6\n'],
]);
const writes = [], errors = [], reads = [];
let hideTrain = false;
let trainGeneration = 1;
let noFiles = false;
let holdNextTrainPage = false;
let heldTrainPage = null;
let executionStatus = 'admitted';
let temporarilyUnavailable = false;
const catalog = target => ({
  schema_ref: 'meta-research/experiment-log-list/v1', target_ref: target.target_ref, target_run_ref: target.target_run_ref,
  target_status: target === first ? executionStatus : 'running',
  workspace_ref: 'workspace-' + target.target_run_ref, status: noFiles || target === empty ? 'empty' : 'ready',
  default_log_ref: noFiles || target === empty ? null : 'train', truncated: false, reason: null,
  logs: (noFiles || target === empty ? [] : target === first ? hideTrain ? ['eval'] : ['train', 'eval'] : ['train']).map(kind => ({
    log_ref: kind, name: kind + '.log', relative_path: 'logs/' + kind + '.log', kind,
    source_bytes: Buffer.byteLength(texts.get(target.target_run_ref + ':' + kind)), modified_at: 1788760000,
    stream_ref: kind + ':' + target.target_run_ref + (target === first && kind === 'train' ? ':' + trainGeneration : ''),
  })),
});
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  const send = (value, status = 200) => res.writeHead(status, { 'Content-Type': 'application/json' }).end(JSON.stringify(value));
  if (url.pathname.startsWith('/api/')) {
    if (req.method !== 'GET') { writes.push(url.pathname); return send({}, 405); }
    reads.push(url.pathname + url.search);
    if (url.pathname === '/api/v1/snapshot') return send(snapshot);
    if (url.pathname === '/api/v1/research-overview') return send({ schema_ref: 'meta-research/research-overview/v1', status: 'ready',
      ...snapshot.research_control.foreground, foreground: snapshot.research_control.foreground, cycle_ordinal: 1,
      findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null });
    if (url.pathname === '/api/v1/research-assets') return send(snapshot.research_assets);
    if (url.pathname.includes('/events') || url.pathname.includes('/projection')) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': fixture\n\n'); return;
    }
    const match = url.pathname.match(/\/bundle\/targets\/([^/]+)\/experiment-logs(?:\/(train|eval))?$/);
    if (!match) return send({}, 404);
    const target = targets.find(item => item.target_ref === decodeURIComponent(match[1]));
    assert.ok(target);
    assert.equal(url.searchParams.get('target_run_ref'), target.target_run_ref);
    const listed = catalog(target);
    if (!match[2]) return send(temporarilyUnavailable && target === first ? {
      ...listed, status: 'unavailable', target_status: null, workspace_ref: null,
      logs: [], default_log_ref: null, reason: { code: 'experiment_log_workspace_unavailable' },
    } : listed);
    const file = listed.logs.find(item => item.log_ref === match[2]);
    assert.ok(file, 'only a discovered file is read');
    if (url.searchParams.has('stream_ref') && url.searchParams.get('stream_ref') !== file.stream_ref) {
      return send({ detail: { code: 'experiment_log_reset_required' } }, 409);
    }
    const bytes = Buffer.from(texts.get(target.target_run_ref + ':' + match[2]));
    const limit = Number(url.searchParams.get('limit') ?? 65536);
    const before = url.searchParams.has('before') ? Number(url.searchParams.get('before')) : null;
    const start = before !== null ? Math.max(0, before - limit)
      : url.searchParams.has('after') ? Number(url.searchParams.get('after')) : Math.max(0, bytes.length - limit);
    const end = Math.min(bytes.length, before ?? start + limit);
    const result = { ...file, schema_ref: 'meta-research/experiment-log-page/v1', target_ref: target.target_ref,
      target_run_ref: target.target_run_ref, workspace_ref: listed.workspace_ref, text: bytes.subarray(start, end).toString(),
      offset: start, next_offset: end, source_bytes: bytes.length, has_more: end < bytes.length, source_caught_up: end >= bytes.length,
      pending_utf8_bytes: 0 };
    if (holdNextTrainPage && target === first && match[2] === 'train') {
      holdNextTrainPage = false;
      heldTrainPage = () => { send(result); heldTrainPage = null; };
      return;
    }
    return send(result);
  }
  try {
    const file = resolve(webRoot, url.pathname.startsWith('/assets/') ? '.' + url.pathname : 'index.html');
    const inside = relative(webRoot, file); assert.ok(!inside.startsWith('..') && !isAbsolute(inside));
    res.setHeader('Content-Type', { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[extname(file)] ?? 'application/octet-stream');
    const body = await readFile(file);
    res.end(extname(file) === '.html' && process.env.TEST_BUNDLE
      ? body.toString().replace(/\/assets\/index-[^"\s]+\.js/, '/assets/' + process.env.TEST_BUNDLE) : body);
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH ?? 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' });
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
const checks = [];
try {
  await page.goto(base + '/?workspace=1', { waitUntil: 'domcontentloaded' });
  const logs = page.getByRole('region', { name: '当前执行日志', exact: true });
  await expect(logs.getByRole('log')).toHaveCount(3);
  await expect(logs.getByRole('log').nth(0)).toContainText('A epoch 2999');
  await expect(logs.getByRole('log').nth(1)).toContainText('A evaluation accuracy=0.93');
  await expect(logs.getByRole('log').nth(2)).toContainText('B epoch 0001');
  await expect(logs).not.toContainText('No files yet');
  await expect(logs).not.toContainText('尚未发现');
  checks.push('all existing files across independent TargetRuns are expanded on the workspace; absent files have no empty panes');
  const trainPane = logs.locator('[data-target-run-ref="run-a"][data-log-ref="train"]');
  const trainLog = trainPane.getByRole('log');
  const evalPane = logs.locator('[data-target-run-ref="run-a"][data-log-ref="eval"]');
  await expect(trainPane.locator('.experiment-log-states')).toContainText('执行状态：等待执行');
  await expect(trainPane.locator('.experiment-log-states')).not.toContainText('执行中');
  executionStatus = 'running';
  await expect(trainPane.locator('.experiment-log-states')).toContainText('执行状态：执行中', { timeout: 7000 });
  await trainLog.evaluate(node => { node.scrollTop -= 300; node.dispatchEvent(new Event('scroll')); });
  await expect(trainPane).toContainText('正在阅读已加载内容');
  const readingText = await trainLog.innerText();
  const readingPosition = await trainLog.evaluate(node => node.scrollTop);
  texts.set('run-a:train', texts.get('run-a:train') + 'TRAIN_NEW_WHILE_READING\n');
  texts.set('run-a:eval', texts.get('run-a:eval') + 'EVAL_CONTINUES_INDEPENDENTLY\n');
  await expect(evalPane.getByRole('log')).toContainText('EVAL_CONTINUES_INDEPENDENTLY', { timeout: 7000 });
  assert.equal(await trainLog.innerText(), readingText);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), readingPosition);
  hideTrain = true;
  await expect(trainPane).toContainText('文件暂时未发现', { timeout: 7000 });
  await expect(trainLog).toHaveText(readingText);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), readingPosition);
  checks.push('one file keeps its history and reading position through append and a temporarily missing rotation entry while another file follows independently');
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '问题树', exact: true }).click();
  await expect(logs).toBeHidden();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '研究总览', exact: true }).click();
  await expect(trainLog).toHaveText(readingText);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), readingPosition);
  await expect(trainPane).toContainText('正在阅读已加载内容');
  checks.push('opening the question tree and returning keeps each same-run file and reading position');
  hideTrain = false; trainGeneration += 1;
  const rotatedText = Array.from({ length: 3000 }, (_, i) => `rotated epoch ${i} loss=0.12\n`).join('') + 'ROTATED_CURRENT_TRAIN epoch=1\n';
  texts.set('run-a:train', rotatedText);
  await expect(trainPane).toContainText('日志文件已轮换', { timeout: 7000 });
  await expect(trainLog).toHaveText(readingText);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), readingPosition);
  await trainPane.getByRole('button', { name: /继续跟随/ }).click();
  await expect(trainLog).toContainText('ROTATED_CURRENT_TRAIN epoch=1\n', { timeout: 7000 });
  await expect(trainLog).not.toContainText('A epoch');
  await expect(evalPane.getByRole('log')).toContainText('EVAL_CONTINUES_INDEPENDENTLY');
  checks.push('a rotated file preserves history until an explicit return to its own current stream');
  holdNextTrainPage = true;
  texts.set('run-a:train', rotatedText + 'RESPONSE_ARRIVES_AFTER_READER_SCROLLS\n');
  await expect.poll(() => heldTrainPage !== null, { timeout: 7000 }).toBe(true);
  await trainLog.evaluate(node => { node.scrollTop -= 300; node.dispatchEvent(new Event('scroll')); });
  await expect(trainPane).toContainText('正在阅读已加载内容');
  const beforeResponse = await trainLog.innerText();
  const beforeResponsePosition = await trainLog.evaluate(node => node.scrollTop);
  const responseFinished = page.waitForResponse(response => /experiment-logs\/train/.test(response.url()) && response.status() === 200);
  heldTrainPage();
  await (await responseFinished).finished();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  assert.equal(await trainLog.innerText(), beforeResponse);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), beforeResponsePosition);
  checks.push('an already-started append response cannot replace or scroll a reader who entered history before it arrived');
  temporarilyUnavailable = true;
  await expect(trainPane.locator('.experiment-log-states')).toContainText('正在重连', { timeout: 7000 });
  await expect(trainLog).toHaveCount(1);
  assert.equal(await trainLog.innerText(), beforeResponse);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), beforeResponsePosition);
  temporarilyUnavailable = false;
  texts.set('run-a:train', texts.get('run-a:train') + 'RECOVERED_AFTER_UNAVAILABLE_NULL_STATUS\n');
  await expect(trainPane.locator('.experiment-log-states')).toContainText('日志读取正常', { timeout: 7000 });
  assert.equal(await trainLog.innerText(), beforeResponse);
  assert.equal(await trainLog.evaluate(node => node.scrollTop), beforeResponsePosition);
  await trainPane.getByRole('button', { name: /继续跟随/ }).click();
  await expect(trainLog).toContainText('RECOVERED_AFTER_UNAVAILABLE_NULL_STATUS', { timeout: 7000 });
  await trainLog.evaluate(node => { node.scrollTop -= 300; node.dispatchEvent(new Event('scroll')); });
  await expect(trainPane).toContainText('正在阅读已加载内容');
  const recoveredHistory = await trainLog.innerText();
  checks.push('an unavailable same-run catalog with null execution status preserves history and scroll, then reconnects and resumes explicitly');
  executionStatus = 'executed';
  await expect(trainPane.locator('.experiment-log-states')).toContainText('执行状态：已结束', { timeout: 7000 });
  await expect(trainPane.locator('.experiment-log-states')).not.toContainText('执行中');
  await expect(trainLog).toHaveText(recoveredHistory);
  checks.push('the public execution status distinguishes admission, running and finished logs even when the graph Target still says running');
  if (artifacts) { await mkdir(artifacts, { recursive: true }); await logs.screenshot({ path: resolve(artifacts, 'parallel-logs-desktop.png') }); }
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(logs.getByRole('log')).toHaveCount(3);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.ok((await trainPane.boundingBox()).width <= 390);
  if (artifacts) await logs.screenshot({ path: resolve(artifacts, 'parallel-logs-mobile.png') });
  checks.push('all existing logs remain expanded and fit a narrow mobile workspace');
  noFiles = true;
  await page.goto(base + '/?workspace=1', { waitUntil: 'domcontentloaded' });
  await expect(logs).toContainText('尚未发现当前执行日志', { timeout: 7000 });
  await expect(logs.getByRole('log')).toHaveCount(0);
  checks.push('an execution without discovered log files has a readable empty state without reserving log panes');
  for (const target of targets) target.target_run_ref = null;
  await page.goto(base + '/?workspace=1', { waitUntil: 'domcontentloaded' });
  await expect(logs).toContainText('当前没有实验执行日志；其他工作输出可在研究会话中查看。');
  await expect(logs.getByRole('log')).toHaveCount(0);
  checks.push('no actual TargetRun is distinct from an execution without discovered files');
  assert.deepEqual(writes, []); assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', checks, researchWrites: writes.length }, null, 2));
} finally {
  await context.close(); await browser.close(); server.closeAllConnections(); await new Promise(done => server.close(done));
}
