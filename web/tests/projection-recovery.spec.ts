import { expect, test, type Page } from "@playwright/test";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import type { AddressInfo } from "node:net";

// Exercise the shipped React page, HTTP and EventSource in Chrome. Controlled
// server cuts make races repeatable without reaching into React or its hooks.
async function fixture() {
  const original = JSON.parse(await readFile(resolve("tests/snapshot-before.json"), "utf8"));
  original.human_collaboration.human_requests.items = [];
  original.observed_at = "2026-11-01T12:00:00Z";
  original.revision_domain = "fixture-data-a";
  original.readiness.checks = original.readiness.checks.map((check: object) => ({ ...check, status: "ready" }));
  const status = {
    schema_ref: "meta-research/runtime-status/v1", revision: 10, revision_domain: "fixture-data-a",
    observed_at: "2026-11-01T12:00:00Z", updated_at: "2026-11-01T11:59:00Z",
    state: "running", current_task: { kind: "stage", title: "运行观察 A", run_ref: "run-a", target_ref: null, status: "running" },
    waiting_reason: null, pending_requests: 0, foreground: original.research_control.foreground,
    health: { status: "ready", checks: [{ name: "bundle_stage_worker", status: "ready" }] },
    all_research_waiting_for_human: false,
  };
  const calls: { path: string; at: number; assets?: string; history?: string }[] = [];
  const streams = new Set<ServerResponse>();
  let snapshotReads = 0, active = 0, maxActive = 0, failStatus = false, failStream = false;
  const cut = (revision: number, title = `研究详情 ${revision}`, req?: IncomingMessage) => {
    const value = structuredClone(original);
    value.revision = revision;
    value.research_space.current_question.title = title;
    value.question_tree.items[0].title = title;
    value.research_assets.loaded = req?.headers["x-meta-research-snapshot-assets"] !== "defer";
    value.writing.loaded = req?.headers["x-meta-research-snapshot-history"] !== "defer";
    value.human_collaboration.companion.loaded = value.writing.loaded;
    if (!value.research_assets.loaded) value.research_assets.items = [];
    return value;
  };
  const send = (res: ServerResponse, payload: unknown, code = 200, headers = {}) => {
    if (!res.destroyed) res.writeHead(code, { "Content-Type": "application/json", ...headers }).end(JSON.stringify(payload));
  };
  let readSnapshot = async (req: IncomingMessage, res: ServerResponse, _ordinal: number) => send(res, cut(10, undefined, req));
  let overview = {
    schema_ref: "meta-research/research-overview/v1", status: "ready", ...status.foreground,
    cycle_ordinal: 1, foreground: status.foreground, findings: { quest: [], question: [], cycle: [] }, cycles: [] as unknown[], reason: null,
  };
  let overviewFailures = 0;
  const webRoot = resolve("../src/meta_research/web_dist");
  const server = createServer(async (req, res) => {
    const path = new URL(req.url!, "http://fixture").pathname;
    if (path.startsWith("/api/")) {
      calls.push({ path, at: Date.now(), assets: String(req.headers["x-meta-research-snapshot-assets"] ?? ""), history: String(req.headers["x-meta-research-snapshot-history"] ?? "") });
      if (req.method !== "GET") return send(res, {}, 405);
      if (path === "/api/v1/status") return send(res, status, failStatus ? 503 : 200);
      if (path === "/api/v1/snapshot") {
        active++; maxActive = Math.max(maxActive, active);
        res.once("close", () => active--);
        return void await readSnapshot(req, res, ++snapshotReads);
      }
      if (path === "/api/v1/events") {
        if (failStream) return send(res, {}, 503);
        res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(": connected\n\n");
        streams.add(res); res.once("close", () => streams.delete(res)); return;
      }
      if (path === "/api/v1/research-overview") return send(res, overview, overviewFailures-- > 0 ? 503 : 200);
      if (path === "/api/v1/research-assets") return send(res, original.research_assets);
      return send(res, {}, 404);
    }
    try {
      const file = resolve(webRoot, path.startsWith("/assets/") ? `.${path}` : "index.html");
      if (!file.startsWith(webRoot + sep)) return void res.writeHead(404).end();
      res.setHeader("Content-Type", ({ ".js": "application/javascript", ".css": "text/css", ".html": "text/html" } as Record<string, string>)[extname(file)] ?? "application/octet-stream");
      res.end(await readFile(file));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done));
  return {
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, calls, status, cut, send,
    set readSnapshot(value: typeof readSnapshot) { readSnapshot = value; },
    set failStatus(value: boolean) { failStatus = value; },
    set overview(value: typeof overview) { overview = value; },
    get overview() { return overview; },
    set overviewFailures(value: number) { overviewFailures = value; },
    set failStream(value: boolean) { failStream = value; if (value) for (const stream of streams) stream.end(); },
    get maxActive() { return maxActive; },
    async emit(revision: number, event = "projection.updated") {
      await expect.poll(() => streams.size).toBeGreaterThan(0);
      // The first status observation may replace the bootstrap stream with its
      // durable domain. Send to a stable connection, never its closing predecessor.
      let connected = [...streams];
      await expect.poll(async () => {
        await new Promise(done => setTimeout(done, 150));
        const stable = connected.length > 0 && connected.every(stream => streams.has(stream));
        connected = [...streams];
        return stable;
      }).toBe(true);
      for (const stream of streams) stream.write(`id: ${revision}\nevent: ${event}\ndata: {"event_type":"research_graph.question_updated"}\n\n`);
    },
    async close() { server.closeAllConnections(); await new Promise<void>(done => server.close(() => done())); },
  };
}

let app: Awaited<ReturnType<typeof fixture>>;
const errors: string[] = [];
test.beforeEach(async ({ page }) => {
  app = await fixture(); errors.length = 0;
  page.on("pageerror", error => errors.push(error.message));
});
test.afterEach(async ({ page }, info) => {
  await info.attach("HTTP timing", { body: JSON.stringify({ calls: app.calls, maxConcurrentSnapshot: app.maxActive, errors }, null, 2), contentType: "application/json" });
  await page.close(); await app.close();
  expect(errors).toEqual([]);
});
const heading = (page: Page, title: string) => page.getByRole("heading", { name: title, exact: true });

test("home keeps a failed observation quiet, preserves its time, and rechecks on return", async ({ page }) => {
  await page.goto(app.url);
  await expect(heading(page, "运行观察 A")).toBeVisible();
  const observed = await page.locator(".status-home-footer time").textContent();
  app.failStatus = true;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "保留上次成功读取" })).toBeVisible();
  await expect(page.locator(".status-home-current").getByRole("alert")).toHaveCount(0);
  expect(await page.locator(".status-home-footer time").textContent()).toBe(observed);
  app.failStatus = false;
  app.status.current_task.title = "恢复后的同版本观察";
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(heading(page, "恢复后的同版本观察")).toBeVisible({ timeout: 1500 });
});

test("visible history automatically retries a failed read at the same snapshot revision", async ({ page }) => {
  app.overviewFailures = 1;
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await expect(page.locator(".research-cycle-badge")).toContainText("第 1 轮", { timeout: 8000 });
  expect(app.calls.filter(call => call.path === "/api/v1/research-overview").length).toBe(2);
});

test("skipped Bundle presents its exact accepted Plan support, gaps and unscheduled reason", async ({ page }) => {
  const scope = app.status.foreground;
  app.overview = { ...app.overview, cycles: [{ cycle_ref: scope.cycle_ref, question_ref: scope.question_ref, ordinal: 1,
    stages: {
      plan: [{ stage: "plan", status: "accepted", epoch: 1, source: { outcome_ref: "plan-original", content_hash: "accepted-hash" }, reason: null,
        content: { answer_contract: { obligations: [{ obligation_key: "o1", statement: "已有实验支持的责任" }, { obligation_key: "o2", statement: "仍需外部数据的责任" }] },
          coverage: [{ obligation_key: "o1", disposition: "covered", evidence_uses: [{ supported_claim: "既有证据支持局部判断", support_boundary: "限于已测条件" }], insufficiency: null },
            { obligation_key: "o2", disposition: "uncovered", evidence_uses: [], insufficiency: "缺少另一环境的数据" }],
          gap_set: ["o2"], experiment_briefs: [], bundle_disposition: "no_new_experiment_required", notes: "本轮等待外部数据，因此不安排新实验。" } },
        { stage: "plan", status: "accepted", epoch: 2, source: { outcome_ref: "plan-later" }, reason: null, content: { notes: "不属于这次跳过的后续计划" } }],
      bundle: [{ stage: "bundle", status: "skipped", epoch: 1, source: { formal_plan_ref: "plan-original" }, content: null, reason: null }],
    } }] };
  await page.goto(app.url + "/?workspace=1&stage=bundle");
  const dialog = page.getByRole("dialog", { name: "实验与证据" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("既有证据支持局部判断");
  await expect(dialog).toContainText("仍需外部数据的责任");
  await expect(dialog).toContainText("缺少另一环境的数据");
  await expect(dialog).toContainText("本轮等待外部数据，因此不安排新实验。");
  await expect(dialog).not.toContainText("不属于这次跳过的后续计划");
  await expect(dialog).toContainText("不表示研究问题已完成");
});

test("one event catches old cuts up to the actually rendered target without another event", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  let reads = 0;
  app.readSnapshot = async (req, res) => {
    reads++;
    app.send(res, app.cut(reads < 3 ? 11 : 12, undefined, req));
  };
  const start = Date.now();
  await app.emit(12);
  await expect(heading(page, "研究详情 11")).toBeVisible();
  await expect(page.getByTestId("projection-refresh")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("catchup-local-hint.png") });
  await expect(heading(page, "研究详情 12")).toBeVisible({ timeout: 12_000 });
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
  expect(reads).toBeGreaterThanOrEqual(3);
  expect(reads).toBeLessThanOrEqual(4);
  expect(app.maxActive).toBe(1);
  console.log(JSON.stringify({ scenario: "old-cut-catchup", renderedAfterMs: Date.now() - start, reads }));
});

test("503 releases the request and the scheduler respects Retry-After before recovering", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  let reads = 0;
  app.readSnapshot = async (req, res) => {
    if (++reads <= 2) app.send(res, { detail: { code: "snapshot_query_timeout" } }, 503, { "Retry-After": "1" });
    else app.send(res, app.cut(12, undefined, req));
  };
  const start = Date.now();
  await app.emit(12);
  await expect(page.getByTestId("projection-refresh")).toContainText(/自动重试/);
  await expect(page.getByRole("alert").filter({ hasText: /详细|网络|断网/ })).toHaveCount(0);
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await expect(heading(page, "研究详情 12")).toBeVisible({ timeout: 12_000 });
  const requests = app.calls.filter(call => call.path === "/api/v1/snapshot").slice(1);
  expect(reads).toBe(3);
  expect(requests[1].at - requests[0].at).toBeGreaterThanOrEqual(950);
  expect(requests[2].at - requests[1].at).toBeGreaterThanOrEqual(950);
  expect(app.maxActive).toBe(1);
  console.log(JSON.stringify({ scenario: "503-recovery", renderedAfterMs: Date.now() - start, reads }));
});

test("same revision worker health refreshes while detail and return from background remain usable", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  app.status.state = "failed";
  app.status.health = { status: "unavailable", checks: [{ name: "bundle_stage_worker", status: "unavailable" }] };
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator(".lumen-connection")).toContainText("研究推进受阻", { timeout: 1500 });
  await expect(page.locator(".lumen-connection")).not.toContainText(/断网|离线/);
  await expect(heading(page, "研究详情 10")).toBeVisible();
  app.status.state = "running";
  app.status.health = { status: "ready", checks: [{ name: "bundle_stage_worker", status: "ready" }] };
  app.readSnapshot = async (req, res) => app.send(res, app.cut(11, undefined, req));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(heading(page, "研究详情 11")).toBeVisible({ timeout: 4000 });
});

test("event channel failure says which channel failed while HTTP continues catching up", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  app.failStream = true;
  await expect(page.getByRole("alert").filter({ hasText: /事件|活动/ })).toBeVisible();
  await expect(page.locator(".lumen-connection")).not.toContainText(/浏览器断网|网络离线/);
  app.status.revision = 11;
  app.readSnapshot = async (req, res) => app.send(res, app.cut(11, undefined, req));
  await expect(heading(page, "研究详情 11")).toBeVisible({ timeout: 9000 });
});

test("a new data instance establishes its own lower revision baseline", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  app.status.revision_domain = "fixture-data-b";
  app.status.revision = 1;
  app.readSnapshot = async (req, res) => {
    const value = app.cut(1, "另一数据实例", req);
    value.revision_domain = "fixture-data-b";
    app.send(res, value);
  };
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(heading(page, "另一数据实例")).toBeVisible({ timeout: 6000 });
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
});

test("deferred assets load only when opened and late reads cannot replace the returned scope", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  expect(app.calls.filter(call => call.path === "/api/v1/snapshot").every(call => call.assets === "defer")).toBe(true);
  let held: { req: IncomingMessage; res: ServerResponse } | undefined;
  app.readSnapshot = async (req, res) => {
    if (req.headers["x-meta-research-snapshot-assets"] === "include") held = { req, res };
    else app.send(res, app.cut(12, "返回后读取的详情", req));
  };
  await page.getByRole("button", { name: "研究资料 ↗", exact: true }).click();
  await expect.poll(() => held !== undefined).toBe(true);
  await expect(page.getByTestId("projection-refresh")).toBeVisible();
  await page.getByRole("button", { name: "关闭研究资料", exact: true }).click();
  await expect(heading(page, "返回后读取的详情")).toBeVisible();
  // An old response with a numerically larger revision is still the wrong scope.
  app.send(held!.res, app.cut(999, "已离开范围的迟到详情", held!.req));
  await expect(heading(page, "返回后读取的详情")).toBeVisible();
  await expect(heading(page, "已离开范围的迟到详情")).toHaveCount(0);
});

test("an opened section stays pending until its real content arrives even at the target revision", async ({ page }) => {
  let includes = 0;
  app.readSnapshot = async (req, res) => {
    const value = app.cut(10, undefined, req);
    if (req.headers["x-meta-research-snapshot-assets"] === "include" && ++includes < 3) value.research_assets.loaded = false;
    app.send(res, value);
  };
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await page.getByRole("button", { name: "研究资料 ↗", exact: true }).click();
  await expect(page.getByTestId("projection-refresh")).toBeVisible();
  await expect.poll(() => includes, { timeout: 8000 }).toBeGreaterThanOrEqual(3);
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
  expect(app.maxActive).toBe(1);
});

test("a feed gap and many events coalesce into one latest visible target", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  let reads = 0;
  app.readSnapshot = async (req, res) => {
    reads++;
    await new Promise(done => setTimeout(done, 200));
    app.send(res, app.cut(20, "缺口核对后的详情", req));
  };
  await app.emit(20, "snapshot.required");
  await expect(heading(page, "缺口核对后的详情")).toBeVisible();
  for (let revision = 21; revision <= 25; revision++) await app.emit(revision);
  app.readSnapshot = async (req, res) => { reads++; app.send(res, app.cut(25, "合并后的最新详情", req)); };
  await expect(heading(page, "合并后的最新详情")).toBeVisible({ timeout: 8000 });
  expect(reads).toBeLessThanOrEqual(4);
  expect(app.maxActive).toBe(1);
});

test("a hung read exits at the 22 second ceiling, stays quiet, and recovers automatically", async ({ page }) => {
  test.setTimeout(45_000);
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  let reads = 0;
  let started = 0, closed = 0;
  app.readSnapshot = async (req, res) => {
    if (++reads === 1) { started = Date.now(); res.once("close", () => { closed = Date.now(); }); return; }
    app.send(res, app.cut(12, "超时后自动恢复", req));
  };
  await app.emit(12);
  await expect.poll(() => started).toBeGreaterThan(0);
  await expect(heading(page, "超时后自动恢复")).toBeVisible({ timeout: 28_000 });
  expect(closed - started).toBeGreaterThanOrEqual(21_500);
  expect(closed - started).toBeLessThan(23_000);
  expect(app.maxActive).toBe(1);
  await expect(page.getByRole("alert").filter({ hasText: /详细|网络|断网/ })).toHaveCount(0);
  console.log(JSON.stringify({ scenario: "hung-read", releasedAfterMs: closed - started, reads }));
});


test("overview Retry-After survives a newer snapshot arriving during its failed read", async ({ page }) => {
  let reads = 0;
  let release!: () => void;
  const held = new Promise<void>(done => { release = done; });
  await page.clock.install();
  await page.route("**/api/v1/research-overview?*", async route => {
    if (++reads === 1) {
      await held;
      await route.fulfill({ status: 503, headers: { "Retry-After": "30" }, json: { detail: { code: "busy" } } });
    } else await route.fulfill({ json: app.overview });
  });
  await page.goto(app.url + "/?workspace=1&stage=bundle");
  await expect.poll(() => reads).toBe(1);
  app.status.revision = 11;
  app.readSnapshot = async (req, res) => app.send(res, app.cut(11, undefined, req));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator(".lumen-connection code")).toContainText("11");
  release();
  await expect(page.getByRole("dialog", { name: "实验与证据" })).toContainText("暂");
  await page.clock.runFor(5_000);
  expect(reads).toBe(1);
  await page.clock.runFor(26_000);
  await expect.poll(() => reads).toBe(2);
});


test("required section read failure cannot acknowledge a rendered target", async ({ page }) => {
  let reads = 0;
  let recover = false;
  app.readSnapshot = async (req, res) => {
    const value = app.cut(10, undefined, req);
    reads++;
    if (!recover) value.question_tree = { loaded: true, status: "unavailable", items: [], reason: { code: "query_in_progress" } };
    app.send(res, value);
  };
  await page.goto(app.url + "/?workspace=1&panel=question-tree");
  await expect.poll(() => reads, { timeout: 8000 }).toBeGreaterThanOrEqual(2);
  await page.waitForTimeout(200);
  await expect(page.getByTestId("projection-refresh")).toBeVisible();
  recover = true;
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
});


test("a failed active stage read cannot finish catch-up at the target revision", async ({ page }) => {
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
  let reads = 0, recover = false;
  app.status.revision = 11;
  app.readSnapshot = async (req, res) => {
    const value = app.cut(11, undefined, req);
    reads++;
    if (!recover) {
      delete value.bundle_stage;
      value.query_warnings = [{ section: "bundle_stage", code: "stage_read_temporarily_unavailable" }];
    }
    app.send(res, value);
  };
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => reads).toBeGreaterThanOrEqual(2);
  await page.waitForTimeout(200);
  await expect(page.getByTestId("projection-refresh")).toBeVisible();
  await expect(page.getByTestId("workspace-partial-warning")).toBeVisible();
  recover = true;
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
  await expect(page.getByTestId("workspace-partial-warning")).toHaveCount(0);
});

test("only server-proven whole research human waiting displays the business blocker", async ({ page }) => {
  let allWaiting = false;
  app.readSnapshot = async (req, res) => {
    const value = app.cut(allWaiting ? 11 : 10, undefined, req);
    value.human_collaboration.human_requests.waiting = { scope: allWaiting ? "quest" : "local", safe_meaningful_runnable_exists: !allWaiting, other_blockers: [] };
    app.send(res, value);
  };
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await expect(page.getByTestId("research-human-wait")).toHaveCount(0);
  allWaiting = true;
  app.status.revision = 11;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.getByTestId("research-human-wait")).toContainText("暂没有可独立继续的工作");
  await expect(page.locator(".lumen-connection")).not.toContainText(/断网|离线/);
});


test("an optional observation warning does not block delivered active stage content", async ({ page }) => {
  app.readSnapshot = async (req, res) => {
    const value = app.cut(10, undefined, req);
    value.query_warnings = [{ section: "bundle_stage", code: "target_observation_unavailable", target_ref: "optional-target" }];
    app.send(res, value);
  };
  await page.goto(app.url + "/?workspace=1");
  await expect(heading(page, "研究详情 10")).toBeVisible();
  await expect(page.getByTestId("workspace-partial-warning")).toBeVisible();
  await expect(page.getByTestId("projection-refresh")).toHaveCount(0);
});
