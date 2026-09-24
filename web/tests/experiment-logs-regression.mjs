import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Isolated UI acceptance fixture. All network traffic is restricted to this
// ephemeral loopback server; all research writes are rejected.
const webRoot = resolve(process.argv[2]);
const snapshot = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
snapshot.human_collaboration.human_requests.items = [];
const target = snapshot.bundle_stage.target_graph.targets[0];
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://fixture').pathname;
    const file = resolve(webRoot, path.startsWith('/assets/') ? `.${path}` : 'index.html');
    assert.ok(file.startsWith(`${webRoot}/`));
    res.setHeader('Content-Type', { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[extname(file)] ?? 'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' });
let train = Array.from({ length: 4200 }, (_, i) => `Epoch ${String(i).padStart(4, '0')} loss=0.123456 progress=training-fixture\n`).join('');
let evalText = 'Evaluation fixture: accuracy=0.932\n';
let generation = 1;
let empty = false;
let failed = false;
let unavailable = false;
let goneOnce = false;
let pageReads = 0;
const writes = [];
const apiRequests = [];
const catalog = () => ({
  schema_ref: 'meta-research/experiment-log-list/v1',
  target_ref: target.target_ref, target_run_ref: target.target_run_ref, workspace_ref: 'workspace_fixture',
  status: empty ? 'empty' : 'ready', default_log_ref: empty ? null : 'train', truncated: false, reason: null,
  logs: empty ? [] : ['train', 'eval'].map(kind => ({
    log_ref: kind, name: `${kind}.log`, relative_path: `logs/${kind}.log`, kind,
    source_bytes: Buffer.byteLength(kind === 'train' ? train : evalText), modified_at: 1788690000,
  })),
});
await context.route('**/*', async route => {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin !== baseUrl) return route.abort();
  if (!url.pathname.startsWith('/api/')) return route.continue();
  apiRequests.push(url.pathname + url.search);
  const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  if (request.method() !== 'GET') { writes.push(url.pathname); return send({ detail: { code: 'fixture_write_forbidden' } }, 405); }
  if (url.pathname === '/api/v1/snapshot') return send(snapshot);
  if (url.pathname === '/api/v1/research-overview') return send({schema_ref:'meta-research/research-overview/v1',status:'ready',
    ...snapshot.research_control.foreground,cycle_ordinal:1,foreground:snapshot.research_control.foreground,
    findings:{quest:[],question:[],cycle:[]},cycles:[],reason:null});
  if (url.pathname === '/api/v1/research-assets') return send(snapshot.research_assets);
  if (url.pathname.endsWith('/experiment-logs')) {
    assert.equal(url.searchParams.get('target_run_ref'), target.target_run_ref);
    if (failed) return send({ detail: { code: 'fixture_disconnected' } }, 503);
    if (unavailable) return send({ ...catalog(), status: 'unavailable', logs: [], default_log_ref: null,
      reason: { code: 'experiment_log_workspace_unavailable' } });
    return send(catalog());
  }
  const match = url.pathname.match(/\/experiment-logs\/(train|eval)$/);
  if (match) {
    pageReads += 1;
    assert.equal(url.searchParams.get('target_run_ref'), target.target_run_ref);
    if (goneOnce) { goneOnce = false; return send({ detail: { code: 'experiment_log_not_found' } }, 404); }
    const kind = match[1];
    const file = catalog().logs.find(log => log.kind === kind);
    const streamRef = `${kind}:generation-${generation}`;
    const bytes = Buffer.from(kind === 'train' ? train : evalText);
    const limit = Number(url.searchParams.get('limit') ?? 65536);
    const cursor = url.searchParams.get('stream_ref');
    if (cursor && cursor !== streamRef) return send({ detail: { code: 'experiment_log_reset_required' } }, 409);
    const before = url.searchParams.has('before') ? Number(url.searchParams.get('before')) : null;
    const start = before !== null ? Math.max(0, before - limit)
      : url.searchParams.has('after') ? Number(url.searchParams.get('after')) : Math.max(0, bytes.length - limit);
    const end = Math.min(bytes.length, before ?? start + limit);
    if (start > bytes.length) return send({ detail: { code: 'experiment_log_reset_required' } }, 409);
    return send({ ...file, schema_ref: 'meta-research/experiment-log-page/v1',
      target_ref: target.target_ref, target_run_ref: target.target_run_ref, workspace_ref: 'workspace_fixture',
      stream_ref: streamRef, text: bytes.subarray(start, end).toString(), offset: start, next_offset: end,
      source_bytes: bytes.length, has_more: end < bytes.length, source_caught_up: end >= bytes.length, pending_utf8_bytes: 0,
    });
  }
  return route.abort();
});
const page = await context.newPage();
const checks = [];
try {
  await page.goto(baseUrl + '/?workspace=1', { waitUntil: 'domcontentloaded' });
  const launcher = page.getByRole('button', { name: /⌘ 实验日志/ });
  await expect(launcher).toBeEnabled();
  await launcher.click();
  await page.getByRole('button', { name: '日志文件', exact: true }).click();
  const dialog = page.locator('#experiment-log-dialog');
  const log = dialog.getByRole('log');
  await expect(dialog.getByRole('combobox', {name:'选择日志文件'}).locator('option')).toHaveText(['logs/train.log', 'logs/eval.log']);
  await expect(dialog.locator('.experiment-log-source-note')).toHaveText('当前按 train/eval 文件名查找。文件名不代表训练或模型评估已经运行。此处展示文件的原始写入。');
  await expect(log).toContainText('Epoch 4199');
  await expect(log).not.toContainText('Epoch 0000');
  assert.ok((await log.innerText()).length <= 65536);
  assert.ok(await log.evaluate(el => el.scrollHeight > el.clientHeight));
  const box = await dialog.boundingBox();
  assert.ok(box.height <= 571);
  checks.push('initial bounded tail and fixed scrolling window');

  train += 'LIVE_APPEND_BEFORE_PROCESS_EXIT loss=0.111\n';
  await expect(log).toContainText('LIVE_APPEND_BEFORE_PROCESS_EXIT', { timeout: 7000 });
  checks.push('incremental append while process is still running');

  await log.hover();
  await page.mouse.wheel(0, -500);
  await expect(dialog).toContainText('正在阅读已加载内容');
  train += 'APPENDED_WHILE_READING_HISTORY\n';
  await expect(dialog.getByRole('button', { name: /有新日志/ })).toBeVisible({ timeout: 7000 });
  await expect(log).not.toContainText('APPENDED_WHILE_READING_HISTORY');
  await dialog.getByRole('button', { name: /继续跟随/ }).click();
  await expect(log).toContainText('APPENDED_WHILE_READING_HISTORY', { timeout: 7000 });
  checks.push('manual scroll pauses content updates; resume catches up');

  await dialog.getByRole('button', { name: /读取更早日志/ }).click();
  await expect(log).not.toContainText('APPENDED_WHILE_READING_HISTORY');
  await expect(dialog).toContainText('正在阅读已加载内容');
  await dialog.getByRole('button', { name: /继续跟随/ }).click();
  await expect(log).toContainText('APPENDED_WHILE_READING_HISTORY', { timeout: 7000 });
  checks.push('earlier history page and return to current output');

  await dialog.getByRole('combobox', { name: '选择日志文件' }).selectOption('eval');
  await expect(log).toContainText('accuracy=0.932');
  await expect(log).not.toContainText('Epoch');
  evalText += 'EVAL_METRIC_NEW f1=0.918\n';
  await expect(log).toContainText('EVAL_METRIC_NEW', { timeout: 7000 });
  checks.push('train/eval switch isolates output and keeps polling');

  generation += 1;
  evalText = 'ROTATED_EVAL_ONLY accuracy=0.945\n';
  await expect(log).toContainText('ROTATED_EVAL_ONLY', { timeout: 8000 });
  await expect(log).not.toContainText('EVAL_METRIC_NEW');
  goneOnce = true;
  await expect.poll(() => goneOnce, { timeout: 8000 }).toBe(false);
  await expect(log).toContainText('ROTATED_EVAL_ONLY', { timeout: 7000 });
  checks.push('rotation 409 and removed-file 404 relist without mixed output');

  failed = true;
  await expect(dialog).toContainText('正在重连', { timeout: 7000 });
  await expect(log).toContainText('ROTATED_EVAL_ONLY');
  failed = false;
  await dialog.getByRole('button', { name: '重试', exact: true }).click();
  await expect(dialog).toContainText('日志读取正常');
  checks.push('connection error retains last content and recovers');

  unavailable = true;
  await expect(dialog).toContainText('正在重连', { timeout: 7000 });
  await expect(log).toContainText('ROTATED_EVAL_ONLY');
  await expect(dialog).not.toContainText('日志读取正常');
  unavailable = false;
  await dialog.getByRole('button', { name: '重试', exact: true }).click();
  await expect(dialog).toContainText('日志读取正常');
  checks.push('unavailable workspace is not a healthy empty log; retained history');

  await dialog.getByRole('button', { name: '最小化实验日志' }).click();
  const pausedReads = pageReads;
  await new Promise(done => setTimeout(done, 2400));
  assert.equal(pageReads, pausedReads);
  await dialog.getByRole('button', { name: '展开实验日志' }).click();
  await expect.poll(() => pageReads).toBeGreaterThan(pausedReads);
  checks.push('minimize pauses polling; restore refreshes');

  await page.screenshot({ path: new URL('./experiment-logs-desktop.png', import.meta.url).pathname });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(dialog.getByRole('button', { name: '关闭实验日志' })).toBeVisible();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const mobileBox = await dialog.boundingBox();
  assert.ok(mobileBox.width <= 390 && mobileBox.height <= 824);
  await page.screenshot({ path: new URL('./experiment-logs-mobile.png', import.meta.url).pathname });
  await dialog.getByRole('button', { name: '关闭实验日志' }).focus();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(launcher).toBeFocused();
  checks.push('mobile bounds; Escape closes and restores launcher focus');

  empty = true;
  await launcher.click();
  await page.getByRole('button', { name: '日志文件', exact: true }).click();
  await expect(dialog).toContainText('正在等待匹配的日志文件');
  await expect(log).toHaveCount(0);
  assert.equal(apiRequests.some(url => /experiment-logs.*raw-output/.test(url)), false);
  assert.deepEqual(writes, []);
  checks.push('honest empty state; no research writes');
  console.log(JSON.stringify({ status: 'passed', checks, pageReads, researchWrites: writes.length }));
} finally {
  await context.close();
  await browser.close();
  await new Promise(done => server.close(done));
}
