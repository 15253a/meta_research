import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Isolated static app plus intercepted APIs. No production write is possible.
const webRoot = resolve(process.argv[2]);
const fixture = JSON.parse(await readFile(new URL('./snapshot-before.json', import.meta.url), 'utf8'));
const server = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://fixture').pathname;
    const file = resolve(webRoot, path.startsWith('/assets/') ? `.${path}` : 'index.html');
    if (!file.startsWith(`${webRoot}/`)) throw new Error('path outside fixture');
    const types = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' };
    res.setHeader('Content-Type', types[extname(file)] ?? 'application/octet-stream');
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true });
const scenario = process.argv[3] ?? 'accept';
const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, serviceWorkers: 'block' });
await context.addCookies([{ name: 'meta_research_csrf', value: 'fixture-csrf', url: baseUrl }]);
const snapshot = structuredClone(fixture);
const hc = snapshot.human_collaboration;
const request = structuredClone(hc.human_requests.items.find(item => item.kind === 'capability_authorization'));
request.request_ref = 'human_request_fixture:r1';
request.request_id = 'human_request_fixture';
request.status = 'open';
request.current = true;
request.responses = [];
request.evaluation = null;
request.disposition = null;
request.impact_preview = null;
request.response_rejections = [];
hc.human_requests.items = [request];
hc.companion.messages = [];
hc.companion.agent_proposals = [];
hc.commands.items = [];
hc.commands.authorizations = [];
const command = {
  intent_id: 'intent_fixture', scope_ref: `human_request:${request.request_ref}`,
  status: 'draft', draft_revision: 1, draft_hash: 'a'.repeat(64), executed: false,
  draft: { command_kind: 'capability_authorization', payload: {
    ...request.required_authorization, decision: 'granted',
  } }, impact_preview: null, confirmation_receipt: null,
};
const authorization = {
  authorization_ref: 'authorization_fixture', scope_ref: command.scope_ref,
  decision: 'granted', status: 'granted', is_current: true,
  requirement: request.required_authorization,
  confirmation_receipt_ref: 'confirmation_fixture', receipt_ref: 'receipt_fixture',
};
const posts = [];
let snapshotReads = 0;
let staleSent = false;
let heldResponses = 0;
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (url.origin !== baseUrl) return route.abort();
  if (!url.pathname.startsWith('/api/')) return route.continue();
  const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  if (url.pathname === '/api/v1/snapshot') {
    snapshotReads += 1;
    return send(snapshot);
  }
  if (route.request().method() === 'GET') return route.abort();
  const body = route.request().postDataJSON();
  posts.push({ path: url.pathname, body });
  if (url.pathname === '/api/v1/human-collaboration/commands') {
    assert.equal(body.scope_ref, command.scope_ref);
    assert.deepEqual(body.command, command.draft);
    hc.commands.items = [structuredClone(command)];
    // Keep the snapshot one step behind to catch stale projected state.
    if (scenario === 'double') await new Promise(done => setTimeout(done, 300));
    return send(command, 201);
  }
  if (url.pathname.endsWith('/previews')) {
    command.impact_preview = { preview_ref: 'preview_fixture', preview_hash: 'b'.repeat(64),
      draft_revision: 1, draft_hash: command.draft_hash, status: 'current', owner_previews: [] };
    return send(command, 201);
  }
  if (url.pathname.endsWith('/confirmations')) {
    assert.equal(body.preview_ref, 'preview_fixture');
    command.confirmation_receipt = { receipt_ref: 'confirmation_fixture' };
    command.status = 'confirmed';
    return send(command, 201);
  }
  if (url.pathname.endsWith('/authorizations')) {
    assert.equal(body.confirmation_receipt_ref, 'confirmation_fixture');
    if (scenario === 'wrong-scope') return send({ ...authorization, requirement: {
      ...authorization.requirement, scope: { target_ref: 'a-different-target' },
    } }, 201);
    return send(authorization, 201);
  }
  if (url.pathname.endsWith('/responses')) {
    if (scenario.startsWith('stale') && !staleSent) {
      staleSent = true;
      request.status = 'superseded';
      request.current = false;
      return send({ detail: { code: scenario === 'stale-root'
        ? 'root_human_request_scope_stale' : 'root_agent_human_request_scope_stale' } }, 409);
    }
    if (scenario === 'retry' && heldResponses++ === 0) return route.abort('connectionfailed');
    request.responses = [{ response_ref: 'response_fixture', ...body }];
    request.status = 'satisfied';
    return send({ response_ref: 'response_fixture', status: 'recorded' }, 201);
  }
  throw new Error(`Unexpected fixture mutation: ${url.pathname}`);
});
const page = await context.newPage();
try {
  await page.goto(`${baseUrl}/?panel=permission-request`, { waitUntil: 'domcontentloaded' });
  const dialog = page.getByRole('dialog').first();
  await expect(dialog.getByRole('button', { name: '接受', exact: true })).toBeVisible();
  if (scenario === 'reject') {
    await dialog.getByRole('button', { name: '拒绝', exact: true }).click();
    await expect.poll(() => posts.filter(p => p.path.endsWith('/responses')).length, { timeout: 4000 }).toBe(1);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.decision, 'declined');
  } else if (scenario === 'layout') {
    assert.equal(posts.length, 0, 'opening the panel must not authorize');
    await page.screenshot({ path: new URL('./permission-desktop.png', import.meta.url).pathname });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: new URL('./permission-mobile.png', import.meta.url).pathname });
    await expect(dialog.getByRole('button', { name: '接受', exact: true })).toBeVisible();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  } else {
    const accept = dialog.getByRole('button', { name: '接受', exact: true });
    if (scenario === 'double') await accept.dblclick();
    else await accept.click();
    if (scenario === 'wrong-scope') {
      await expect(dialog.getByRole('alert')).toContainText('capability_authorization_invalid');
      assert.equal(posts.filter(p => p.path.endsWith('/responses')).length, 0);
      console.log(JSON.stringify({ scenario, status: 'passed', writes: posts.length }));
      process.exitCode = 0;
    } else {
    await expect.poll(() => posts.filter(p => p.path.endsWith('/responses')).length, { timeout: 4000,
      message: 'One Accept click must submit the exact authorization response with no extra user steps' }).toBe(1);
    const response = posts.at(-1).body;
    assert.equal(response.decision, 'provided');
    assert.deepEqual(response.facts, { authorization_receipt_ref: 'receipt_fixture' });
    assert.equal(posts.filter(p => p.path.endsWith('/authorizations')).length, 1);
    if (scenario.startsWith('stale')) {
      await expect.poll(() => snapshotReads, { timeout: 4000 }).toBeGreaterThan(1);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect.poll(() => snapshotReads).toBeGreaterThan(2);
      assert.equal(posts.filter(p => p.path.endsWith('/responses')).length, 1, 'never replay approval onto a new request');
    } else if (scenario === 'retry') {
      await expect(accept).toBeEnabled();
      await accept.click();
      await expect.poll(() => posts.filter(p => p.path.endsWith('/responses')).length).toBe(2);
      assert.equal(posts.filter(p => p.path.endsWith('/authorizations')).length, 1, 'reuse the already issued exact receipt');
    } else {
      await expect(dialog).toContainText(/这件事已处理|回应已提交/);
    }
    }
  }
  console.log(JSON.stringify({ scenario, status: 'passed', writes: posts.length, snapshotReads }));
} finally {
  await context.close();
  await browser.close();
  await new Promise(done => server.close(done));
}
