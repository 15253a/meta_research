// Usage: node tests/workspace-fast-observation.mjs DIST SNAPSHOT_JSON REPORT_DIR
// Real loopback HTTP with a deliberately blocked full snapshot; no service writes.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { chromium, expect } from '@playwright/test';
const [distArg, fixtureArg, reportArg] = process.argv.slice(2);
assert.ok(distArg && fixtureArg && reportArg);
const webRoot = resolve(distArg), reportRoot = resolve(reportArg);
await mkdir(reportRoot, { recursive: true });
const snapshot = JSON.parse(await readFile(fixtureArg, 'utf8'));
const original = snapshot.research_control.foreground;
assert.ok(original?.quest_ref);
let foreground = { ...original };
let revision = snapshot.revision;
let failed = true, unavailable = false, releaseSnapshot = false;
const started = performance.now(), observations = [], requests = [], errors = [], outside = [];
const pendingSnapshots = new Set();
const sessions = () => foreground ? ['reasoning', 'bundle', 'idea'].map(stage => ({
  session_ref: `${foreground.quest_ref}-${foreground.cycle_ref}-${stage}`, root_session_ref: `${foreground.quest_ref}-${foreground.cycle_ref}-${stage}`,
  kind: 'stage', title: `隔离测试 ${stage}`, short_title: stage, stage, related_stages: [stage], scope_label: '当前轮',
  owner_session_ref: null, status: stage === foreground.stage ? 'executing' : 'completed', is_executing: stage === foreground.stage,
  is_current: stage === foreground.stage, run_ref: `fixture-${stage}`, target_ref: null, cycle_ref: foreground.cycle_ref,
  question_ref: foreground.question_ref, created_at: 1000, updated_at: 1001, operations: [],
})) : [];
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  requests.push({ path, method: req.method, at: performance.now() - started });
  const send = (value, code = 200) => { if (!res.destroyed) res.writeHead(code, { 'Content-Type': 'application/json' }).end(JSON.stringify(value)); };
  if (path.startsWith('/api/')) {
    if (req.method !== 'GET') return send({}, 405);
    if (path === '/api/v1/status') {
      if (unavailable) return send({}, 503);
      return send({ schema_ref: 'meta-research/runtime-status/v1', revision, observed_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        foreground, state: foreground ? failed ? 'failed' : 'running' : 'idle', current_task: foreground ? { kind: 'stage', title: `当前 ${foreground.stage}`, run_ref: 'fixture', target_ref: null, status: 'running' } : null,
        waiting_reason: null, pending_requests: 0, health: { status: 'ready', checks: foreground ? [{ name: `${foreground.stage}_stage_worker`, status: failed ? 'unavailable' : 'ready', ...(failed ? { reason: { code: 'fixture_phase_blocked' } } : {}) }] : [] } });
    }
    if (path === '/api/v1/snapshot') {
      if (releaseSnapshot) return send(snapshot);
      pendingSnapshots.add(res); res.on('close', () => pendingSnapshots.delete(res)); return;
    }
    if (path.endsWith('/root-sessions')) return send({ schema_ref: 'meta-research/root-sessions/v1', quest_ref: foreground?.quest_ref ?? decodeURIComponent(path.split('/')[4]), observed_at: Date.now() / 1000,
      sessions: sessions(), active_session_refs: [], limited: false, reasons: [] });
    if (path === '/api/v1/research-overview') return send({ schema_ref: 'meta-research/research-overview/v1', status: 'ready', ...original, foreground: original,
      cycle_ordinal: 1, findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null });
    if (path === '/api/v1/research-assets') return send(snapshot.research_assets);
    if (path.includes('events') || path.includes('projection')) { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': fixture\n\n'); return; }
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
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || '/usr/bin/google-chrome', headless: true,
  args: ['--disable-background-networking', '--disable-component-update', '--no-first-run', '--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1'] });
let result;
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
  await context.route('**/*', route => { if (new URL(route.request().url()).origin === base) return route.continue(); outside.push(route.request().url()); return route.abort(); });
  const page = await context.newPage(); page.on('pageerror', error => errors.push(error.message));
  const began = performance.now(); await page.goto(base + '/?workspace=1', { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('workspace-live-overview')).toBeVisible({ timeout: 4000 });
  await expect(page.locator('.root-session-conversation')).toContainText(`隔离测试 ${foreground.stage}`, { timeout: 4000 });
  observations.push({ check: 'first_conversation_before_full_snapshot', milliseconds: performance.now() - began });
  assert.ok(pendingSnapshots.size > 0);
  await expect(page.getByTestId('research-current-worker-blocker')).toContainText('fixture_phase_blocked');
  // Preserve an explicit session selection while the full snapshot arrives.
  await page.locator('[data-testid="workspace-live-overview"] [data-stage="bundle"] .spectrum-stage-open').click();
  await expect(page.locator('.root-session-conversation')).toContainText('隔离测试 bundle');
  const selected = await page.locator('.root-session-conversation').getAttribute('data-session-ref');
  failed = false;
  await expect(page.getByTestId('research-current-worker-blocker')).toHaveCount(0, { timeout: 7000 });
  observations.push({ check: 'health_recovery_without_full_snapshot' });
  releaseSnapshot = true;
  for (const response of pendingSnapshots) response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(snapshot));
  await expect(page.getByTestId('product-shell')).toHaveAttribute('data-shell-state', 'ready-active', { timeout: 5000 });
  await expect(page.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', selected);
  await expect(page.getByTestId('research-current-worker-blocker')).toHaveCount(0);
  observations.push({ check: 'selection_preserved_and_stale_snapshot_error_not_revived' });
  foreground = { ...foreground, quest_ref: 'fixture-next-quest', cycle_ref: 'fixture-next-cycle', question_ref: 'fixture-next-question', stage: 'idea' }; revision += 1;
  await expect(page.getByTestId('workspace-live-overview')).toHaveAttribute('data-cycle-ref', 'fixture-next-cycle', { timeout: 7000 });
  await expect(page.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'fixture-next-quest-fixture-next-cycle-idea', { timeout: 5000 });
  await expect(page.locator('.lumen-quest-context')).toContainText('研究详情更新中');
  await expect(page.getByRole('button', { name: '暂停研究', exact: true })).toBeDisabled();
  observations.push({ check: 'new_quest_and_cycle_drop_old_selection' });
  unavailable = true;
  await expect(page.locator('.root-conversations .research-connection')).toContainText('状态待确认', { timeout: 7000 });
  await page.screenshot({ path: resolve(reportRoot, 'workspace-fast.png'), fullPage: false });
  observations.push({ check: 'failed_status_poll_labels_observation_stale' });
  unavailable = false; foreground = null; revision += 1;
  await expect(page.getByTestId('workspace-live-overview')).toContainText('当前没有运行中的阶段', { timeout: 7000 });
  await expect(page.getByTestId('current-cycle-overview')).toHaveCount(0);
  await expect(page.locator('.root-session-conversation')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '暂停研究', exact: true })).toBeDisabled();
  observations.push({ check: 'empty_current_foreground_does_not_revive_old_cycle' });
  await page.goto('about:blank');
  const atClose = requests.length;
  await new Promise(done => setTimeout(done, 5500));
  assert.equal(requests.length, atClose, 'polling must stop on unmount');
  observations.push({ check: 'unmount_stops_polling' });
  assert.deepEqual(errors, []); assert.deepEqual(outside, []);
  assert.equal(requests.filter(row => row.method !== 'GET').length, 0);
  result = { status: 'passed', observations, errors, outside, requests, production_requests: 0 };
} catch (error) {
  result = { status: 'failed', error: String(error), observations, errors, outside, requests, production_requests: 0 };
  process.exitCode = 1;
} finally {
  await writeFile(resolve(reportRoot, 'workspace-fast-results.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, requests: result.requests.length }, null, 2));
  await browser.close(); server.closeAllConnections(); await new Promise(done => server.close(done));
}
