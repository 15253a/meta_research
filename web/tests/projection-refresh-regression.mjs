import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { chromium, expect } from "@playwright/test";

// Exercise the production SSE client with real browser EventSource delivery.
// Compile only this exported entry point so no application or live Owner is needed.
const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const sourceFile = ts.createSourceFile("api.ts", source, ts.ScriptTarget.Latest, true);
const declaration = sourceFile.statements.find(node =>
  ts.isFunctionDeclaration(node) && node.name?.text === "followProjection");
assert.ok(declaration);
const compiled = ts.transpileModule(declaration.getText(sourceFile), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

const connections = [];
const server = createServer((request, response) => {
  const url = new URL(request.url, "http://fixture");
  if (url.pathname === "/api/v1/events") {
    response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    response.write(": connected\n\n");
    connections.push({ after: Number(url.searchParams.get("after")), response });
  } else {
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end("<!doctype html><title>Projection refresh fixture</title>");
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: process.env.META_RESEARCH_CHROME
  ?? (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe"
    : "/root/.cache/ms-playwright/chromium-1193/chrome-linux/chrome"), headless: true });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", error => errors.push(error.message));
await page.route("**/*", route => new URL(route.request().url()).origin === origin
  ? route.continue() : route.abort());
const send = (type, value, revision) => connections.at(-1).response.write(
  `${revision === undefined ? "" : `id: ${revision}\n`}event: ${type}\ndata: ${JSON.stringify(value)}\n\n`);
const state = () => page.evaluate(() => window.observed);
try {
  await page.goto(origin);
  await page.evaluate(code => {
    const exports = {};
    new Function("exports", code)(exports);
    window.observed = { revisions: [], pointers: [], activities: [], connections: [], snapshotRequired: 0 };
    window.stopProjection = exports.followProjection(100,
      revision => window.observed.revisions.push(revision),
      () => { window.observed.snapshotRequired += 1; },
      connected => window.observed.connections.push(connected),
      () => 100,
      pointer => window.observed.pointers.push(pointer),
      activity => window.observed.activities.push(activity));
  }, compiled);
  await expect.poll(() => connections.length).toBe(1);
  const pointer = { target_ref: "target-a", target_run_ref: "run-a", stream_ref: "stream-a", head_cursor: "cursor-a" };
  send("agent_runtime.target_root_observations_available", pointer);
  send("projection.updated", { event_type: "agent_runtime.target_root_observations_available", revision: 101 }, 101);
  await expect.poll(async () => (await state()).activities.length).toBe(1);
  // The client's coalescing timer is 50 ms; allow it to fire if wrongly armed.
  await page.waitForTimeout(150);
  assert.deepEqual((await state()).pointers, [pointer]);
  assert.deepEqual((await state()).revisions, [], "log-only observations must not reload the full snapshot");

  connections.at(-1).response.end();
  await expect.poll(() => connections.length).toBe(2);
  assert.equal(connections[1].after, 101, "skipped reload still advances the reconnect cursor");
  send("projection.updated", { event_type: "research_graph.target_root_committed", revision: 102 }, 102);
  await expect.poll(async () => (await state()).revisions).toEqual([102]);
  send("projection.updated", { event_type: "research_memory.asset_accepted", revision: 103 }, 103);
  send("projection.updated", { event_type: "human_collaboration.human_request_response_recorded", revision: 104 }, 104);
  await expect.poll(async () => (await state()).revisions).toEqual([102, 104]);
  send("projection.updated", { event_type: "agent_runtime.future_state_event", revision: 105 }, 105);
  await expect.poll(async () => (await state()).revisions).toEqual([102, 104, 105]);
  send("snapshot.required", { reason: "revision_gap", snapshot_revision: 120 }, 120);
  await expect.poll(async () => (await state()).snapshotRequired).toBe(1);
  connections.at(-1).response.end();
  await expect.poll(() => connections.length).toBe(3);
  assert.equal(connections[2].after, 120);
  const result = await state();
  assert.deepEqual(result.activities.map(x => x.event_type), [
    "agent_runtime.target_root_observations_available", "research_graph.target_root_committed",
    "research_memory.asset_accepted", "human_collaboration.human_request_response_recorded",
    "agent_runtime.future_state_event",
  ]);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: "passed", checks: [
    "observation-pointer-and-activity-preserved-without-snapshot-reload",
    "observation-reconnect-cursor-advances", "subsequent-target-commit-refreshes",
    "assets-and-human-response-refresh", "unknown-events-still-refresh",
    "revision-gap-refresh-and-reconnect-cursor-preserved",
  ], revisions: result.revisions, reconnectAfter: connections.map(x => x.after) }));
} finally {
  await page.evaluate(() => window.stopProjection?.()).catch(() => {});
  await browser.close();
  for (const connection of connections) connection.response.end();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
