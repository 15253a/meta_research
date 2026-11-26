import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

test("first-question preparation shows C progress and live activity for direct generation without opening logs", async ({ page }) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  let output = JSON.stringify({ type: "thread.started", thread_id: "private-thread" }) + "\n";
  let status = "running";
  const startedAt = Date.now() / 1000 - 60;
  const current = () => ({
    initialization_id: "init-progress", creation_context: "quest_initialization", route: "direct", status: "proposal_generating",
    quest_draft: { revision: 1, hash: "a".repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "形成有依据的第一问", completion_criteria: "显示实际准备过程", time_budget: "2h", research_style: "balanced", route: "direct",
      resource_envelope_ref: null, resource_envelope_hash: null, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
    } },
    compute: null, resource_envelope: null,
    proposal_generation: { ref: "generation-progress", basis_revision: 1, basis_hash: "a".repeat(64), status, adapter_kind: "production", attempt_count: 1,
      started_at: startedAt, completed_at: status === "failed" ? startedAt + 64 : null, failure: status === "failed" ? { code: "protected_creation_launch_failed" } : null, literature_snapshot_ref: null },
    proposal: null, confirmation_preview: null, intent_session: null, acquisition_session: null,
    deepfetch: { request_ref: "previous-deepfetch", basis_revision: 1, basis_hash: "a".repeat(64), freshness: "current", status: "failed", activity: "needs_retry", failure: { code: "old-failure" }, run: { attempt_started_at: 100, attempt_completed_at: 200 } },
    recovery: null, canonical_empty_advancement: false, capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } }, receipts: {},
  });
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  const offsets: number[] = [];
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current: current() } });
    if (url.pathname === "/api/v1/quest-initializations/init-progress") return json(current());
    if (url.pathname.endsWith("/proposal-output")) {
      const after = Number(url.searchParams.get("after")); offsets.push(after);
      return json({ generation_ref: "generation-progress", status, availability: "ready", text: output.slice(after), next_offset: output.length, source_bytes: output.length, has_more: false });
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const progress = page.getByTestId("first-question-progress");
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("正在起草第一个研究问题");
  await expect(progress.getByRole("progressbar")).toHaveAttribute("aria-valuemax", "4");
  const activity = progress.getByRole("log", { name: "第一问最近活动" });
  await expect(activity).toContainText("生成会话已启动");
  output += JSON.stringify({ type: "error", message: "Reconnecting 2/5" }) + "\n";
  await expect(activity).toContainText("网络连接重试中（2/5）");
  await expect(progress).not.toHaveClass(/is-failed/);
  output += JSON.stringify({ type: "item.completed", item: { id: "reasoning-1", type: "reasoning", text: "**Reviewing repository structure** Checking existing experiment logs" } }) + "\n";
  output += JSON.stringify({ type: "item.started", item: { id: "command-1", type: "command_execution", command: "bash -lc 'ls -la /data/project'" } }) + "\n";
  output += JSON.stringify({ type: "item.completed", item: { id: "command-2", type: "command_execution", command: "bash -lc 'rg missing_key /data/project/notes.md'", status: "failed", exit_code: 1 } }) + "\n";
  output += JSON.stringify({ type: "item.completed", item: { id: "json-1", type: "agent_message", text: "{\"coverage\":[],\"selection\":[]}" } }) + "\n";
  await expect(activity).toContainText("Reviewing repository structure");
  await expect(activity).toContainText("正在浏览目录：/data/project");
  await expect(activity).toContainText("检查命令返回非零状态（已继续）");
  await expect(activity).not.toContainText("bash -lc");
  await expect(activity).not.toContainText("当前工作说明已更新");
  await expect(progress.locator(".first-question-activity-row.failed")).toHaveCount(0);
  await progress.screenshot({ path: "test-results/first-question-c-running.png" });
  output += JSON.stringify({ type: "turn.failed", error: { message: "private failure detail" } }) + "\n";
  status = "failed";
  await expect(activity).toContainText("本次生成未完成");
  await expect(activity).not.toContainText("private failure detail");
  await expect(progress).toContainText("本次准备未完成");
  expect(offsets.some(offset => offset > 0)).toBe(true);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await progress.scrollIntoViewIfNeeded();
    await progress.screenshot({ path: `test-results/first-question-c-${width}.png` });
    expect(await progress.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  }
});

test("multi-page catch-up on reload keeps historical rows un-animated until has_more clears", async ({ page }) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const startedAt = Date.now() / 1000 - 30;
  const current = {
    initialization_id: "init-paged", creation_context: "quest_initialization", route: "direct", status: "proposal_generating",
    quest_draft: { revision: 1, hash: "a".repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "分页恢复测试", completion_criteria: "历史行不重复动画", time_budget: "1h", research_style: "balanced", route: "direct",
      resource_envelope_ref: null, resource_envelope_hash: null, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
    } },
    compute: null, resource_envelope: null,
    proposal_generation: { ref: "generation-paged", basis_revision: 1, basis_hash: "a".repeat(64), status: "running", adapter_kind: "production", attempt_count: 1,
      started_at: startedAt, completed_at: null, failure: null, literature_snapshot_ref: null },
    proposal: null, confirmation_preview: null, intent_session: null, acquisition_session: null,
    deepfetch: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } }, receipts: {},
  };
  const page1 = JSON.stringify({ type: "thread.started", thread_id: "t1" }) + "\n"
    + JSON.stringify({ type: "item.completed", item: { id: "item_0", type: "reasoning", summary: ["Page 1 historical reasoning"] } }) + "\n";
  const page2 = JSON.stringify({ type: "item.completed", item: { id: "item_1", type: "reasoning", summary: ["Page 2 historical reasoning"] } }) + "\n";
  const page3 = JSON.stringify({ type: "item.completed", item: { id: "item_2", type: "reasoning", summary: ["Live arriving reasoning"] } }) + "\n";
  let callCount = 0;
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current } });
    if (url.pathname === "/api/v1/quest-initializations/init-paged") return json(current);
    if (url.pathname.endsWith("/proposal-output")) {
      callCount += 1;
      if (callCount === 1) {
        return json({ generation_ref: "generation-paged", status: "running", availability: "ready", text: page1, next_offset: page1.length, source_bytes: page1.length + page2.length, has_more: true, started_at: startedAt, last_event_at: startedAt + 10 });
      }
      if (callCount === 2) {
        return json({ generation_ref: "generation-paged", status: "running", availability: "ready", text: page2, next_offset: page1.length + page2.length, source_bytes: page1.length + page2.length, has_more: false, started_at: startedAt, last_event_at: startedAt + 12 });
      }
      if (callCount === 3) {
        return json({ generation_ref: "generation-paged", status: "running", availability: "ready", text: page3, next_offset: page1.length + page2.length + page3.length, source_bytes: page1.length + page2.length + page3.length, has_more: false, started_at: startedAt, last_event_at: startedAt + 15 });
      }
      return json({ generation_ref: "generation-paged", status: "running", availability: "ready", text: "", next_offset: page1.length + page2.length + page3.length, source_bytes: page1.length + page2.length + page3.length, has_more: false, started_at: startedAt, last_event_at: startedAt + 15 });
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const activity = page.getByRole("log", { name: "第一问最近活动" });
  await expect(activity).toContainText("Live arriving reasoning");
  const rows = activity.locator(".first-question-activity-row");
  await expect(rows).toHaveCount(4);
  // Historical page 1 + page 2 rows must NOT have data-entering="true"
  await expect(rows.nth(0)).not.toHaveAttribute("data-entering", "true");
  await expect(rows.nth(1)).not.toHaveAttribute("data-entering", "true");
  await expect(rows.nth(2)).not.toHaveAttribute("data-entering", "true");
  // Live row arriving after hydration completed MUST have data-entering="true"
  await expect(rows.nth(3)).toHaveAttribute("data-entering", "true");
});
