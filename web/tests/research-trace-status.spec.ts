import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { PublicSnapshot } from "../src/api.js";
import type { RootOperation, RootSession, RootSessions } from "../src/rootSessionsApi.js";

test.setTimeout(120_000);
const sourceTime = Date.parse("2026-09-09T18:47:15+08:00") / 1_000;
const message = (text: string, thread = "native-root") => JSON.stringify({ type: "item.completed", thread_id: thread, item: { type: "agent_message", text } }) + "\n";

// All requests are intercepted: the built UI starts no research service.
async function fixture(page: Page) {
  const snapshot: PublicSnapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const foreground = snapshot.research_control.foreground!;
  const operation = (ref: string, label: string, offset: number, status: RootOperation["status"] = "completed"): RootOperation => ({ operation_ref: ref, label, phase: ref, status, created_at: sourceTime + offset, updated_at: sourceTime + offset });
  const session = (ref: string, kind: RootSession["kind"], title: string, status: RootSession["status"], operations: RootOperation[], current = true): RootSession => ({
    session_ref: ref, root_session_ref: ref, kind, title, status, is_executing: status === "executing", is_current: current,
    stage: kind === "stage" ? "bundle" : null, related_stages: kind === "acquisition" ? [] : ["bundle"], scope_label: kind === "acquisition" ? "Quest 共享" : "当前轮",
    owner_session_ref: kind === "target" ? "session-bundle" : null, run_ref: `run-${ref}`, target_ref: kind === "target" ? `target-${ref}` : null,
    cycle_ref: kind === "acquisition" ? null : foreground.cycle_ref, question_ref: kind === "acquisition" ? null : foreground.question_ref,
    created_at: sourceTime, updated_at: sourceTime, operations,
  });
  const bundle = session("session-bundle", "stage", "Bundle", "waiting", [operation("primary", "初始方案", 0), operation("review", "复核方案", 10), operation("dispatch-1", "安排实验", 20)]);
  const target1 = session("session-t1", "target", "Target T1 · 验证基线", "executing", [operation("target-turn-1", "实验执行", 30, "executing")]);
  const target2 = session("session-t2", "target", "Target T2 · 对照实验", "executing", [operation("target-turn-2", "实验执行", 40, "executing")]);
  target1.short_title = "Target T1"; target2.short_title = "Target T2";
  const acquisition = session("session-acquisition", "acquisition", "Acquisition · 获取资料", "executing", []);
  const history = session("session-history", "stage", "Idea · 上一轮", "completed", [operation("idea-primary", "形成候选", -100)], false);
  history.cycle_ref = "previous-cycle"; history.stage = "idea";
  const catalog: RootSessions = { schema_ref: "meta-research/root-sessions/v1", quest_ref: foreground.quest_ref, observed_at: sourceTime + 1_547,
    sessions: [bundle, target1, target2, acquisition, history], active_session_refs: [target1.session_ref, target2.session_ref, acquisition.session_ref], limited: false, reasons: [] };
  for (const stage of ["idea", "plan", "reasoning"]) {
    const own = session(`session-${stage}`, "stage", stage[0].toUpperCase() + stage.slice(1), "completed", [operation(`${stage}-current`, "研究", 0)]);
    own.stage = stage; own.related_stages = [stage]; catalog.sessions.push(own);
  }
  const initialization = session("session-deepfetch-init", "deepfetch", "DeepFetch", "completed", []);
  initialization.cycle_ref = null; initialization.stage = null; initialization.related_stages = [];
  initialization.creation_context_kind = "quest_initialization";
  initialization.scope_label = "Quest 初始化"; catalog.sessions.push(initialization);
  snapshot.human_collaboration!.human_requests.items = [];
  const initialTargetWorker = snapshot.readiness.checks.find(check => check.name === "target_run_worker")!;
  initialTargetWorker.status = "ready"; delete initialTargetWorker.reason;
  snapshot.bundle_stage!.run = null; snapshot.bundle_stage!.target_graph.targets = []; snapshot.bundle_stage!.target_graph.frontier = [];
  const output = new Map<string, string>([
    ["primary", message("Bundle 已形成初始研究方案。") + message("CHILD_PRIVATE_CONVERSATION", "child-thread") + JSON.stringify({ type: "item.completed", item: { type: "reasoning", text: "HIDDEN_REASONING" } }) + "\n"],
    ["review", message("同一 Bundle 会话已完成复核。")], ["dispatch-1", message("现在等待两个 Target 的证据。")],
    ["target-turn-1", message("T1 正在执行基线实验。")], ["target-turn-2", message("T2 正在执行对照实验。")], ["idea-primary", message("上一轮 Idea 已完成。")],
  ]);
  const state = { listFailed: false, outputFailed: false, staleOnce: false, listReads: 0, outputReads: 0, outputSessionRefs: [] as string[], offsets: [] as { ref: string; after: number }[] };
  const errors: string[] = [], writes: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://research-trace.test") return route.abort();
    if (request.method() !== "GET") { writes.push(request.method() + " " + url.pathname); return route.abort(); }
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname === "/api/v1/research-overview") return json({ schema_ref: "meta-research/research-overview/v1", ...foreground, status: "ready", cycle_ordinal: 1, foreground, findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null });
    if (url.pathname.endsWith("/root-sessions")) { state.listReads += 1; return json(catalog, state.listFailed ? 503 : 200); }
    const raw = url.pathname.match(/\/root-sessions\/([^/]+)\/output$/);
    if (raw) {
      state.outputReads += 1; state.outputSessionRefs.push(raw[1]);
      if (state.outputFailed) return json({}, 503);
      if (state.staleOnce) { state.staleOnce = false; return json({ detail: { code: "root_session_output_cursor_stale" } }, 409); }
      const item = catalog.sessions.find(item => item.session_ref === raw[1])!, ref = url.searchParams.get("operation_ref")!;
      const op = item.operations.find(op => op.operation_ref === ref)!, bytes = Buffer.from(output.get(ref) ?? "");
      const start = Number(url.searchParams.get("after") ?? 0);
      state.offsets.push({ ref, after: start });
      let end = Math.min(bytes.length, start + 65536);
      while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end -= 1;
      return json({ schema_ref: "meta-research/root-session-output/v1", quest_ref: foreground.quest_ref, session_ref: item.session_ref, operation_ref: ref,
        stream_ref: `root-output:${item.session_ref}:${ref}`, status: ["completed", "failed"].includes(op.status) ? "terminal" : op.status === "executing" ? "live" : "waiting",
        native_session_ref: "native-root", text: bytes.subarray(start, end).toString(), offset: start, next_offset: end, source_bytes: bytes.length,
        has_more: end < bytes.length, source_caught_up: end >= bytes.length, source_updated_at: sourceTime, observed_at: sourceTime + 1_547 });
    }
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  return { snapshot, catalog, bundle, target1, target2, acquisition, history, output, state, errors, writes, operation };
}

test("long research sessions stay bounded and live polls request only newly appended bytes", async ({ page }) => {
  const f = await fixture(page);
  f.bundle.operations = Array.from({ length: 1000 }, (_, index) => f.operation(`history-${index}`, `探索 ${index}`, index));
  for (const operation of f.bundle.operations) f.output.set(operation.operation_ref, message(operation.label));
  await page.goto("http://research-trace.test/?workspace=1");
  const timeline = page.locator(".root-session-conversation");
  await expect(timeline.locator(".root-operation")).toHaveCount(4);
  await expect(timeline).toContainText("探索 999");
  const beforeReads = f.state.outputReads;
  await timeline.getByRole("button", { name: /读取更早工作/ }).click();
  await expect(timeline).toContainText("探索 995");
  await expect(timeline).not.toContainText("探索 999");
  await expect(timeline.locator(".root-operation")).toHaveCount(4);
  await expect.poll(() => f.state.outputReads).toBeGreaterThan(beforeReads);
  await timeline.getByRole("button", { name: "继续跟随 ↓" }).click();
  await expect(timeline).toContainText("探索 999");
  await page.locator('button[data-session-ref="session-t1"]').click();
  await expect(timeline).toContainText("T1 正在执行基线实验。");
  const originalBytes = Buffer.byteLength(f.output.get("target-turn-1")!);
  await expect.poll(() => f.state.offsets.filter(item => item.ref === "target-turn-1").length).toBeGreaterThan(1);
  {
    const offsets = f.state.offsets.filter(item => item.ref === "target-turn-1");
    expect(offsets[0].after).toBe(0);
    expect(offsets.at(-1)!.after).toBe(originalBytes);
  }
  f.output.set("target-turn-1", f.output.get("target-turn-1") + message("新尝试仍然不确定，保留当前范围继续调查。"));
  await expect(timeline).toContainText("新尝试仍然不确定，保留当前范围继续调查。");
  await expect(timeline).toContainText("T1 正在执行基线实验。");
  expect(f.errors).toEqual([]);
  expect(f.writes).toEqual([]);
});

test("accepted execution with pending evaluation exposes seven separate research facts", async ({ page }, testInfo) => {
  const f = await fixture(page);
  const target = { target_ref: "target-run-only", target_key: "局部调查", target_run_ref: "target-work-1", spec_hash: "s".repeat(64), dependency_refs: [], status: "committed", blocker: null };
  f.snapshot.bundle_stage!.target_graph.targets = [target];
  f.snapshot.bundle_stage!.target_graph.status = "accepted";
  const accepted = { status: "realized", commit_ref: "commit-run-only", target_ref: target.target_ref, target_run_ref: target.target_run_ref!,
    evaluation_attempt_ref: "", target_spec_hash: target.spec_hash, closure_hash: "c".repeat(64), result_disposition: "uncertain",
    closure: { root_measurement: { evaluation_status: "pending", formal_entities: [{ variant_run_ref: "actual-run-1", evaluation_attempt_ref: null }], result_asset: { version_ref: "observations-v1" } } } };
  f.snapshot.bundle_stage!.target_commits = [accepted];
  await page.goto("http://research-trace.test/?workspace=1");
  const card = page.getByRole("region", { name: "当前研究工作状态" });
  await expect(card).toBeVisible();
  await expect(card).toContainText("执行已记录 · 评价待办");
  await expect(card).toContainText("研究记录已接纳 · 结果不确定");
  await expect(card.locator(".target-research-facts dt")).toHaveText(["输入", "实际执行", "产物", "评价", "接纳", "交接", "求助"]);
  await expect(card).not.toContainText("技术执行受阻");
  await expect(card.locator("pre")).toHaveCount(0);
  await card.getByText("查看精确输入、产物与交接来源", { exact: true }).click();
  await expect(card.locator("pre")).toContainText("actual-run-1");
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("target-facts-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("target-facts-mobile.png"), fullPage: true });
  expect(f.errors).toEqual([]);
  expect(f.writes).toEqual([]);
});

test("root sessions preserve continuous work, parallel activity, historical scope and failure recovery", async ({ page }, testInfo) => {
  const f = await fixture(page);
  await page.goto("http://research-trace.test/?workspace=1");
  const spectrum = page.getByRole("region", { name: "研究光谱" });
  const activity = spectrum.locator('.spectrum-stage[data-stage="bundle"]');
  await expect(spectrum.locator(".spectrum-stage")).toHaveCount(4);
  for (const [stage, count] of Object.entries({ idea: 2, plan: 2, bundle: 4 })) {
    const card = spectrum.locator(`.spectrum-stage[data-stage="${stage}"]`);
    await expect(card.locator(".stage-root-count")).toHaveText(`${count} 个根 session`);
    await expect(card.locator('.stage-root-links').first().locator('button[data-session-ref="session-acquisition"]')).toHaveCount(1);
  }
  for (const text of ["Bundle", "Target T1", "Target T2", "Acquisition"]) await expect(activity).toContainText(text);
  await expect(page.locator('.root-session-list, .root-current-activity')).toHaveCount(0);
  await expect(spectrum.locator('button button')).toHaveCount(0);
  await expect(activity).not.toContainText("DeepFetch");
  await expect(spectrum.locator('.spectrum-stage[data-stage="reasoning"] .stage-root-count')).toHaveText("3 个根 session 入口 · 2 个已建立 · 1 个待启动");
  const trace = page.locator("#research-activity");
  await expect(trace.locator(".root-session-conversation")).toHaveAttribute("data-session-ref", "session-bundle");
  await activity.locator('button[data-session-ref="session-bundle"]').click();
  const timeline = trace.getByRole("region", { name: "Bundle 会话", exact: true });
  for (const text of ["暂无模型调用在执行", "Bundle 已形成初始研究方案。", "同一 Bundle 会话已完成复核。", "现在等待两个 Target 的证据。"]) await expect(timeline).toContainText(text);
  await expect(timeline.locator(".root-operation-time")).toHaveText([/初始方案/, /复核方案/, /安排实验/]);
  await expect(trace.getByRole("button", { name: /^(当前输出|主任务输出|审查输出|审查与调度)$/ })).toHaveCount(0);
  await expect(timeline).not.toContainText("CHILD_PRIVATE_CONVERSATION"); await expect(timeline).not.toContainText("HIDDEN_REASONING");
  await expect(timeline.getByText(/其他原始记录/)).toHaveCount(0);
  const completedClock = timeline.locator(".research-execution-clock").first();
  await expect(completedClock).toContainText("输出已结束"); const clockText = await completedClock.innerText(), reads = f.state.listReads;
  await expect.poll(() => f.state.listReads).toBeGreaterThan(reads); await expect(completedClock).toHaveText(clockText);
  f.bundle.operations.push(f.operation("target-batch-2", "根据证据接续", 50)); f.output.set("target-batch-2", message("后续调用已接在同一 Bundle 会话。"));
  await expect(timeline).toContainText("后续调用已接在同一 Bundle 会话。");
  await expect(activity.locator('button[data-session-ref="session-bundle"]')).toHaveAttribute("aria-pressed", "true");
  await activity.locator('button[data-session-ref="session-t2"]').click(); await expect(trace).toContainText("T2 正在执行对照实验。"); await expect(trace).not.toContainText("T1 正在执行基线实验。");
  const liveClock = trace.locator(".research-execution-clock"); await expect(liveClock).toContainText("距上次执行记录");
  const seconds = Number(await liveClock.locator("b").innerText()); await expect.poll(async () => Number(await liveClock.locator("b").innerText())).toBeGreaterThan(seconds);
  f.state.staleOnce = true; await expect.poll(() => f.state.staleOnce).toBe(false); await expect(trace).toContainText("T2 正在执行对照实验。");
  const idea = spectrum.locator('.spectrum-stage[data-stage="idea"]');
  await idea.locator('.spectrum-stage-open').click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-idea');
  const plan = spectrum.locator('.spectrum-stage[data-stage="plan"]');
  await plan.locator('.spectrum-stage-open').click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-plan');
  const reasoning = spectrum.locator('.spectrum-stage[data-stage="reasoning"]');
  await reasoning.locator('.spectrum-stage-open').click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-reasoning');
  await expect(reasoning.locator('button[data-session-ref="session-deepfetch-init"]')).toHaveCount(0);
  await reasoning.locator('button[data-root-role="deepfetch"]').click();
  await expect(trace).toContainText("Reasoning 尚未建立由本阶段发起的 DeepFetch 根会话。");
  await expect(trace.locator(".root-session-conversation")).toHaveCount(0);
  await reasoning.locator('button[data-session-ref="session-acquisition"]').click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-acquisition');
  await plan.locator('button[data-session-ref="session-acquisition"]').click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-acquisition');
  await expect(plan.locator('button[data-session-ref="session-acquisition"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(reasoning.locator('button[data-session-ref="session-acquisition"]')).toHaveAttribute('aria-pressed', 'false');
  await idea.locator(".stage-root-history > summary").click(); await idea.locator('button[data-session-ref="session-history"]').click();
  await expect(trace.locator(".root-session-heading")).toContainText("历史轮次"); await expect(trace).toContainText("上一轮 Idea 已完成。");
  const worker = f.snapshot.readiness.checks.find(check => check.name === "bundle_stage_worker")!;
  worker.status = "unavailable"; worker.reason = { code: "bundle_fixture_failure" }; f.snapshot.revision += 1;
  await expect(trace.getByTestId("research-current-worker-blocker")).toContainText("bundle_fixture_failure"); await expect(trace).toContainText("上一轮 Idea 已完成。");
  worker.status = "ready"; delete worker.reason; f.snapshot.revision += 1; await expect(trace.getByTestId("research-current-worker-blocker")).toHaveCount(0);
  const targetWorker = f.snapshot.readiness.checks.find(check => check.name === "target_run_worker")!;
  targetWorker.status = "unavailable"; targetWorker.reason = { code: "target_measurement_domain_authority_invalid" }; f.snapshot.revision += 1;
  await expect(trace.getByTestId("research-current-worker-blocker")).toContainText("Target 启动受阻");
  await expect(trace.getByTestId("research-current-worker-blocker")).toContainText("target_measurement_domain_authority_invalid");
  await expect(trace).toContainText("上一轮 Idea 已完成。");
  targetWorker.status = "ready"; delete targetWorker.reason; f.snapshot.revision += 1;
  await expect(trace.getByTestId("research-current-worker-blocker")).toHaveCount(0);
  await trace.getByRole("button", { name: "返回当前阶段 ↗" }).click(); await expect(trace).toContainText("Bundle 已形成初始研究方案。");
  await activity.locator('button[data-session-ref="session-t1"]').click(); await expect(trace).toContainText("T1 正在执行基线实验。");
  f.state.outputFailed = true; await expect(trace).toContainText("记录暂时无法更新，已保留上次内容"); await expect(trace).toContainText("T1 正在执行基线实验。");
  f.state.outputFailed = false; await trace.getByRole("button", { name: "重试", exact: true }).click(); await expect(trace).not.toContainText("记录暂时无法更新，已保留上次内容");
  f.state.listFailed = true; await expect(trace.locator(".root-session-heading")).toContainText("状态待确认"); await expect(activity).toContainText("上次记录");
  f.state.listFailed = false; await trace.getByRole("button", { name: "重新读取", exact: true }).click(); await expect(trace.locator(".root-session-heading")).not.toContainText("状态待确认");
  await page.setViewportSize({ width: 1440, height: 960 }); await page.locator("main.lumen-main").evaluate(node => { node.scrollTop = 0; }); await page.screenshot({ path: testInfo.outputPath("root-sessions-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 }); expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.locator("main.lumen-main").evaluate(node => { node.scrollTop = 0; }); await page.screenshot({ path: testInfo.outputPath("root-sessions-mobile.png"), fullPage: true }); expect(f.errors).toEqual([]); expect(f.writes).toEqual([]);
});

test("large calls retain an explicit path to older bytes and resume the latest page", async ({ page }) => {
  const f = await fixture(page); f.catalog.sessions = [f.bundle]; f.catalog.active_session_refs = [];
  f.bundle.operations = [f.operation("large-call", "较长研究调用", 0)];
  f.output.set("large-call", message("这次调用最早的公开记录。") + " ".repeat(8 * 1024 * 1024 + 65536) + "\n" + message("这次调用最新的公开记录。"));
  await page.goto("http://research-trace.test/?workspace=1"); const trace = page.locator("#research-activity");
  await expect(trace).toContainText("这次调用最新的公开记录。", { timeout: 60_000 }); await expect(trace).toContainText("这次调用较早的记录已收起"); await expect(trace).not.toContainText("这次调用最早的公开记录。");
  await trace.getByText("查看这次调用的分页记录", { exact: true }).click(); await trace.getByRole("button", { name: "最早记录", exact: true }).click();
  await expect(trace).toContainText("这次调用最早的公开记录。"); await expect(trace).not.toContainText("这次调用最新的公开记录。");
  await trace.getByRole("button", { name: "下一页", exact: true }).click(); await expect(trace.getByRole("button", { name: "上一页", exact: true })).toBeEnabled();
  await trace.getByRole("button", { name: "返回最新记录", exact: true }).click(); await expect(trace).toContainText("这次调用最新的公开记录。"); expect(f.errors).toEqual([]); expect(f.writes).toEqual([]);
});

test("stage defaults select the current root while explicit same-stage history stays pinned until Cycle changes", async ({ page }) => {
  const f = await fixture(page);
  const oldBundle: RootSession = { ...f.bundle, session_ref: "session-bundle-old", root_session_ref: "session-bundle-old", is_current: false,
    status: "completed", created_at: sourceTime - 100, operations: [f.operation("old-bundle-operation", "早先安排", -100)] };
  f.output.set("old-bundle-operation", message("同轮旧 Bundle 根会话记录。"));
  f.catalog.sessions.unshift(oldBundle);
  await page.goto("http://research-trace.test/?workspace=1");
  const card = page.locator('.spectrum-stage[data-stage="bundle"]'), trace = page.locator('#research-activity');
  await expect(card.locator('.stage-root-count')).toHaveText('5 个根 session');
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', f.bundle.session_ref);
  await card.locator('button[data-session-ref="session-bundle-old"]').click();
  await expect(trace).toContainText('同轮旧 Bundle 根会话记录。');
  const reads = f.state.listReads;
  await expect.poll(() => f.state.listReads).toBeGreaterThan(reads);
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', 'session-bundle-old');
  await trace.getByRole('button', { name: '返回当前阶段 ↗' }).click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', f.bundle.session_ref);
  await card.locator('button[data-session-ref="session-bundle-old"]').click();
  f.catalog.sessions = f.catalog.sessions.filter(item => item.session_ref !== oldBundle.session_ref);
  await expect(trace).toContainText('所选根会话暂不可读取');
  await expect(trace.locator('.root-session-conversation')).toHaveCount(0);
  f.catalog.sessions.push(oldBundle);
  await expect(trace).toContainText('同轮旧 Bundle 根会话记录。');
  const nextCycle = "next-cycle";
  const next: RootSession = { ...f.bundle, session_ref: "session-next-bundle", root_session_ref: "session-next-bundle", cycle_ref: nextCycle, created_at: sourceTime + 100,
    operations: [f.operation("next-bundle-operation", "新一轮安排", 100)] };
  f.output.set("next-bundle-operation", message("新一轮 Bundle 根会话记录。"));
  f.catalog.sessions.push(next);
  f.snapshot.research_control.foreground!.cycle_ref = nextCycle; f.snapshot.revision += 1;
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', next.session_ref);
  await expect(trace).toContainText('新一轮 Bundle 根会话记录。');
  await expect(trace).not.toContainText('同轮旧 Bundle 根会话记录。');
  expect(f.errors).toEqual([]); expect(f.writes).toEqual([]);
});

test("Reasoning keeps three roles, excludes unrelated DeepFetch roots and binds pending roles only to their real owner", async ({ page }, testInfo) => {
  const f = await fixture(page);
  const foreground = f.snapshot.research_control.foreground!;
  const main = f.catalog.sessions.find(item => item.session_ref === "session-reasoning")!;
  f.catalog.sessions = f.catalog.sessions.filter(item => item.session_ref !== main.session_ref);
  const init = f.catalog.sessions.find(item => item.session_ref === "session-deepfetch-init")!;
  const manual: RootSession = { ...init, session_ref: "manual-deepfetch", root_session_ref: "manual-deepfetch", creation_context_kind: "manual_question_creation", scope_label: "手动创建问题" };
  const wrongOwner: RootSession = { ...init, session_ref: "wrong-owner-deepfetch", root_session_ref: "wrong-owner-deepfetch", creation_context_kind: "autonomous_question_creation",
    stage: "reasoning", related_stages: ["reasoning"], cycle_ref: foreground.cycle_ref, question_ref: foreground.question_ref, owner_session_ref: "session-plan" };
  const oldMain: RootSession = { ...main, session_ref: "historical-reasoning", root_session_ref: "historical-reasoning", cycle_ref: "old-cycle", is_current: false };
  const historical: RootSession = { ...wrongOwner, session_ref: "historical-reasoning-deepfetch", root_session_ref: "historical-reasoning-deepfetch", cycle_ref: "old-cycle", owner_session_ref: oldMain.session_ref, question_ref: null };
  const wrongCycle: RootSession = { ...wrongOwner, session_ref: "wrong-cycle-deepfetch", root_session_ref: "wrong-cycle-deepfetch", owner_session_ref: oldMain.session_ref };
  f.catalog.sessions.push(manual, wrongOwner, oldMain, historical, wrongCycle);
  await page.goto("http://research-trace.test/?workspace=1");
  const card = page.locator('.spectrum-stage[data-stage="reasoning"]'), trace = page.locator('#research-activity');
  const current = card.getByRole('group', { name: 'Reasoning 本轮根会话', exact: true });
  await expect(card.locator('.stage-root-count')).toHaveText('3 个根 session 入口 · 1 个已建立 · 2 个待启动');
  await expect(current.locator('button')).toHaveCount(3);
  for (const root of [init, manual, wrongOwner, wrongCycle]) await expect(card.locator(`button[data-session-ref="${root.session_ref}"]`)).toHaveCount(0);
  await expect(current.locator(`button[data-session-ref="${historical.session_ref}"]`)).toHaveCount(0);
  await card.locator('.spectrum-stage-open').click();
  await expect(trace.getByRole('region', { name: 'Reasoning 待启动会话', exact: true })).toContainText('当前轮尚未建立 Reasoning 根会话。');
  await current.locator('button[data-root-role="deepfetch"]').click();
  await expect(trace.getByRole('region', { name: 'DeepFetch 待启动会话', exact: true })).toContainText('Reasoning 尚未建立由本阶段发起的 DeepFetch 根会话。');
  await expect(trace.locator('.root-session-conversation')).toHaveCount(0);
  await page.locator('main.lumen-main').evaluate(node => { node.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath('reasoning-pending-roles.png'), fullPage: true });

  // The child alone cannot prove that it belongs to the missing Reasoning parent.
  const deepfetch: RootSession = { ...wrongOwner, session_ref: 'actual-reasoning-deepfetch', root_session_ref: 'actual-reasoning-deepfetch', owner_session_ref: main.session_ref,
    question_ref: null, scope_label: 'Reasoning · 创建问题', status: 'executing', is_executing: true, operations: [f.operation('reasoning-deepfetch-op', '检索资料', 80, 'executing')] };
  f.output.set('reasoning-deepfetch-op', message('由当前 Reasoning 发起的实际 DeepFetch 对话。'));
  f.catalog.sessions.push(deepfetch); f.catalog.limited = true;
  const reads = f.state.listReads;
  await expect.poll(() => f.state.listReads).toBeGreaterThan(reads);
  await expect(card.locator('.stage-root-count')).toContainText('其余状态待确认');
  await expect(trace.getByRole('region', { name: 'DeepFetch 待启动会话', exact: true })).toContainText('正在确认此入口的根会话状态。');
  await expect(trace.locator('.root-session-conversation')).toHaveCount(0);

  f.catalog.sessions.push(main); f.catalog.limited = false;
  await expect(card.locator('.stage-root-count')).toHaveText('3 个根 session 入口 · 3 个已建立 · 0 个待启动');
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', deepfetch.session_ref);
  await expect(trace).toContainText('由当前 Reasoning 发起的实际 DeepFetch 对话。');
  await trace.locator('.root-session-context summary').click();
  await expect(trace.locator('.root-session-context')).toContainText(`发起会话 ${main.session_ref}`);
  expect(f.state.outputSessionRefs.some(ref => [init.session_ref, manual.session_ref, wrongOwner.session_ref, wrongCycle.session_ref].includes(ref))).toBe(false);

  // A later root never steals the selection which was resolved from the pending role.
  const second = { ...deepfetch, session_ref: 'later-reasoning-deepfetch', root_session_ref: 'later-reasoning-deepfetch', created_at: sourceTime + 90, operations: [] };
  f.catalog.sessions.unshift(second);
  await expect(card.locator('.stage-root-count')).toHaveText('3 个根 session 入口 · 4 个已建立 · 0 个待启动');
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', deepfetch.session_ref);
  await card.locator('.stage-root-history > summary').click();
  await card.locator(`button[data-session-ref="${historical.session_ref}"]`).click();
  await expect(trace.locator('.root-session-conversation')).toHaveAttribute('data-session-ref', historical.session_ref);
  await expect(trace.locator('.root-session-heading')).toContainText('历史轮次');
  await trace.locator('.root-session-context summary').click();
  await expect(trace.locator('.root-session-context')).toContainText(`发起会话 ${oldMain.session_ref}`);
  expect(f.errors).toEqual([]); expect(f.writes).toEqual([]);
});
