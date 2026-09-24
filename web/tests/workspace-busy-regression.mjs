import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { chromium, expect } from '@playwright/test';

const webRoot = resolve(process.argv[2]);
const full = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
let scenario = 'transient', requests = 0, active = 0, maxActive = 0;
let mutationCount = 0;
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  const send = (value, status = 200, headers = {}) => {
    res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
    res.end(JSON.stringify(value));
  };
  if (path.startsWith('/api/')) {
    if (req.method !== 'GET') { mutationCount++; return send({}, 405); }
    if (path === '/api/v1/snapshot') {
      requests++; active++; maxActive = Math.max(maxActive, active);
      res.once('close', () => active--);
      if (scenario === 'transient' && requests <= 2 || scenario === 'persistent') {
        return send({ detail: { code: 'snapshot_query_in_progress' } }, 503, { 'Retry-After': '1' });
      }
      if (scenario === 'failure' || scenario === 'generic-503') {
        return send({ detail: { code: 'unexpected_failure' } }, scenario === 'failure' ? 500 : 503);
      }
      return send(full);
    }
    if (path.includes('events') || path.includes('projection')) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      return res.write(': fixture stream\n\n');
    }
    return send({}, 404);
  }
  try {
    const file = resolve(webRoot, path.startsWith('/assets/') ? `.${path}` : 'index.html');
    assert.ok(file.startsWith(webRoot + sep));
    res.setHeader('Content-Type', { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[extname(file)] ?? 'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
await context.addInitScript(() => {
  const timer = window.setTimeout;
  window.setTimeout = (callback, delay, ...args) => timer(callback, delay === 22000 ? 500 : delay, ...args);
});
const page = await context.newPage();
const errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
const shell = page.locator('[data-testid="product-shell"]');
try {
  await page.goto(base + '/?workspace=1');
  await expect.poll(() => requests).toBeGreaterThanOrEqual(1);
  await expect(shell).toHaveAttribute('data-shell-state', 'loading', { timeout: 700 });
  await expect.poll(() => requests, { timeout: 3500 }).toBeGreaterThanOrEqual(2);
  await expect(shell).toHaveAttribute('data-shell-state', 'loading', { timeout: 700 });
  await expect(shell).toHaveAttribute('data-shell-state', 'ready-active', { timeout: 4000 });
  assert.equal(requests, 3);
  assert.equal(maxActive, 1);
  checks.push('two busy responses keep loading across request windows and recover');

  scenario = 'persistent'; requests = 0;
  await page.goto(base + '/?workspace=1');
  await expect.poll(() => requests, { timeout: 4000 }).toBeGreaterThanOrEqual(3);
  await expect(shell).toHaveAttribute('data-shell-state', 'loading');
  assert.equal(maxActive, 1);
  checks.push('explicit persistent contention keeps loading beyond the old total deadline');

  scenario = 'success';
  await expect(shell).toHaveAttribute('data-shell-state', 'ready-active');
  checks.push('automatic recovery works once the shared read becomes available');

  for (const failedScenario of ['failure', 'generic-503']) {
    scenario = failedScenario; requests = 0;
    await page.goto(base + '/?workspace=1');
    await expect(shell).toHaveAttribute('data-shell-state', 'first-error');
    assert.equal(requests, 1);
  }
  checks.push('HTTP 500 and non-retryable HTTP 503 still fail immediately');

  scenario = 'success';
  await page.getByRole('button', { name: '重新读取研究状态', exact: true }).click();
  await expect(shell).toHaveAttribute('data-shell-state', 'ready-active');
  checks.push('manual recovery works after non-retryable failure');

  scenario = 'persistent'; requests = 0;
  await page.goto(base + '/?workspace=1');
  await expect.poll(() => requests).toBeGreaterThanOrEqual(1);
  await page.goto(base + '/not-a-workspace');
  const afterLeave = requests;
  await new Promise(done => setTimeout(done, 1400));
  assert.equal(requests, afterLeave);
  checks.push('leaving the workspace cancels pending busy retries');
  assert.equal(mutationCount, 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', checks, maxConcurrentSnapshot: maxActive, mutationCount, errors }));
} finally {
  await context.close(); await browser.close();
  server.closeAllConnections(); await new Promise(done => server.close(done));
}
