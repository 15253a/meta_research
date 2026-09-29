import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { extname, isAbsolute, relative, resolve } from "node:path";
import { chromium, expect } from "@playwright/test";

// Isolated real-component regression for paths, hashes and exact references in
// human guidance and storage details. No production requests or writes.
const webRoot = resolve(process.argv[2]);
const evidence = resolve(process.argv[3]);
await mkdir(evidence, { recursive: true });
const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
snapshot.human_collaboration.human_requests.items = [];
snapshot.human_collaboration.human_requests.waiting = { scope: "none", safe_meaningful_runnable_exists: true };
const hash = "f9907c65b25a7630885c6924bbb06658f2e1b5e0";
const name = `P-ADIC_${hash}_${hash}_storage_manifest.json`;
const summary = `存储检查：outputs/data/barzi_osf_a65wk；版本 ${hash}；原件保留在 /tmp/P-ADIC_${hash}/outputs/data/barzi_osf_a65wk。`;
const asset = { ...snapshot.research_assets.items[0], display_name: name };
const custody = { ...snapshot.research_assets.custodies[2], source_locator: `/tmp/${name}` };
snapshot.research_assets.items = [asset];
snapshot.research_assets.custodies = [custody];
snapshot.research_assets.total_count = 1;
snapshot.research_assets.has_more = false;
const errors = [], measurements = [], writes = [];
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://fixture");
  const send = (body, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.method !== "GET") writes.push(`${req.method} ${url.pathname}`);
  if (url.pathname === "/api/v1/preferences") return send({ output_language: "zh" });
  if (url.pathname.startsWith("/api/v1/research-library/")) return send({ items: url.pathname.endsWith("/human") ? [{ ref: "human-long-path", name, summary, reader: { source_ref: "human-long-path", version_ref: asset.memory_ref } }] : [], next_offset: null });
  if (url.pathname === "/api/v1/research-content") return send({ text: summary, complete: true, next_offset: null });
  if (url.pathname === "/api/v1/snapshot") return send(snapshot);
  if (url.pathname === "/api/v1/research-assets") return send(snapshot.research_assets);
  if (url.pathname === `/api/v1/research-assets/${asset.memory_ref}`) return send({ item: asset, custodies: [custody], roles: [], holds: [], release_assessments: [], reference_revision: 1 });
  if (url.pathname === "/api/v1/status") return send({ schema_ref: "meta-research/runtime-status/v1", revision: 1, observed_at: new Date().toISOString(), updated_at: new Date().toISOString(), state: "waiting", current_task: null, waiting_reason: null, pending_requests: 0, foreground: snapshot.research_control.foreground, health: { status: "ready", checks: [] } });
  if (url.pathname === "/api/v1/research-overview") return send({ schema_ref: "meta-research/research-overview/v1", status: "ready", foreground: snapshot.research_control.foreground, findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null });
  if (url.pathname.includes("projection") || url.pathname.includes("events")) { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(": fixture\n\n"); return; }
  if (url.pathname.startsWith("/api/")) return send({}, 404);
  try {
    const file = resolve(webRoot, url.pathname.startsWith("/assets/") ? "." + url.pathname : "index.html");
    const path = relative(webRoot, file);
    assert.ok(path && !path.startsWith("..") && !isAbsolute(path));
    res.setHeader("Content-Type", { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" }[extname(file)] ?? "application/octet-stream");
    res.end(await readFile(file));
  } catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, "127.0.0.1", done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: process.env.META_RESEARCH_CHROME ?? "/usr/bin/google-chrome", headless: true });
const context = await browser.newContext();
await context.route("**/*", route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
const page = await context.newPage();
page.on("pageerror", error => errors.push(error.message));
async function measure(label, selectors) {
  const values = await page.locator(selectors).evaluateAll(nodes => nodes.filter(node => node.getClientRects().length).map(node => ({ selector: `${node.tagName}.${node.className}`, client: node.clientWidth, scroll: node.scrollWidth, overflowX: getComputedStyle(node).overflowX, text: node.textContent.slice(0, 120) })));
  measurements.push({ label, viewport: page.viewportSize(), values });
}
try {
  for (const width of [390, 800, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(base + "/?panel=research-assets");
    const dialog = page.locator(".asset-dialog");
    await dialog.getByRole("tab", { name: "人类输入", exact: true }).click();
    await dialog.getByLabel("查找研究材料", { exact: true }).fill("P-ADIC");
    await dialog.getByRole("button", { name: "查找", exact: true }).click();
    await expect(dialog.locator(".library-card h4")).toHaveText(name);
    await expect(dialog.locator(".library-card > p").first()).toHaveText(summary);
    await measure("human-card", ".asset-window,.research-library,.library-columns,.library-results,.library-card,.library-card h4,.library-card p");
    await dialog.getByRole("button", { name: "阅读原文", exact: true }).click();
    await expect(dialog.locator(".library-prose")).toHaveText(summary);
    await dialog.getByText("精确引用", { exact: true }).click();
    await measure("exact-original", ".library-original,.library-original h4,.library-original pre");
    await page.screenshot({ path: resolve(evidence, `library-${width}.png`) });
    await dialog.locator(".asset-storage-details > summary").click();
    await expect(dialog.locator(".asset-detail h3")).toHaveText(name);
    await dialog.getByText("精确保管记录", { exact: true }).click();
    await measure("storage-detail", ".asset-inventory-layout,.asset-detail,.asset-detail > header,.asset-detail h3,.asset-detail details,.asset-detail details p");
    await dialog.locator(".asset-detail").scrollIntoViewIfNeeded();
    await page.screenshot({ path: resolve(evidence, `storage-${width}.png`) });
  }
  const overflows = measurements.flatMap(sample => sample.values.filter(value => value.scroll > value.client + 1).map(value => ({ label: sample.label, width: sample.viewport.width, ...value })));
  await writeFile(resolve(evidence, "measurements.json"), JSON.stringify({ measurements, overflows, errors, writes }, null, 2));
  assert.deepEqual(writes, [], "Browsing must not mutate even the isolated research fixture");
  assert.deepEqual(errors, [], "No browser errors");
  assert.deepEqual(overflows, [], "Asset titles, summaries and exact references must stay readable within their containers");
  console.log(JSON.stringify({ status: "passed", widths: [390, 800, 1440], measurements: measurements.length, writes: writes.length, errors: errors.length }));
} finally {
  await browser.close();
  server.closeAllConnections();
  await new Promise(done => server.close(done));
}
