import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { QuestCreationView } from "../src/api";

async function creationFixture(page: Page, startedAt = Date.now() / 1000) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const hash = "a".repeat(64);
  const creation: QuestCreationView = {
    initialization_id: "init-recovery", creation_context: "quest_initialization", route: "direct", status: "draft",
    quest_draft: { revision: 1, hash, schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "根据实际资料准备第一问", completion_criteria: "保留来源和研究限制", time_budget: "4h", research_style: "balanced", route: "direct",
      resource_envelope_ref: "envelope-recovery", resource_envelope_hash: hash, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
    } },
    compute: { snapshot_ref: "compute-recovery", status: "ready", adapter_kind: "fixture", observed_at: 1, devices: [], reason: null },
    resource_envelope: { ref: "envelope-recovery", hash, schema_ref: "fixture", status: "current", host_snapshot_ref: "compute-recovery",
      time_budget: "4h", hard_ceiling: { kind: "wall_clock", seconds: 14_400 }, selected_device_uuids: [] },
    proposal_generation: null, proposal: null, confirmation_preview: null, intent_session: null, acquisition_session: null,
    deepfetch: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: { human_confirmation: { status: "not_attempted" }, quest_goal: { status: "not_attempted" }, broad_research_authorization: { status: "not_attempted" },
      question_content: { status: "not_attempted" }, question_identity: { status: "not_attempted" }, cycle_activation: { status: "not_attempted" } },
  };
  const calls = { reads: 0, generations: 0 };
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.context().addCookies([{ name: "meta_research_csrf", value: "fixture", url: "http://127.0.0.1:18768" }]);
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current: creation } });
    if (url.pathname === "/api/v1/quest-initializations/init-recovery") { calls.reads += 1; return json(creation); }
    if (url.pathname.endsWith("/proposal-generations")) {
      calls.generations += 1;
      creation.status = "proposal_generating";
      creation.proposal_generation = { ref: "generation-recovery", basis_revision: 1, basis_hash: hash, status: "running",
        adapter_kind: "production", attempt_count: 1, started_at: startedAt, failure: null, literature_snapshot_ref: null };
      return json(creation);
    }
    if (url.pathname.endsWith("/proposal-output")) return json({ generation_ref: creation.proposal_generation?.ref, status: creation.proposal_generation?.status,
      availability: "ready", text: "", next_offset: 0, source_bytes: 0, has_more: false });
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  return { creation, calls };
}

test("direct generation continues observing after three minutes without another submission", async ({ page }) => {
  const started = new Date("2026-11-12T01:00:00Z");
  await page.clock.setFixedTime(started);
  const { calls } = await creationFixture(page, started.getTime() / 1000);
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "生成第一个问题", exact: true }).click();
  const progress = page.getByTestId("first-question-progress");
  await expect(progress).toContainText("正在起草第一个研究问题");
  const reads = calls.reads;
  await page.clock.setFixedTime(new Date(started.getTime() + 240_000));
  await expect.poll(() => calls.reads).toBeGreaterThan(reads + 2);
  await expect(page.getByText("后台操作仍未收敛；可以关闭窗口，稍后从同一入口恢复。")).toHaveCount(0);
  await expect(progress).toContainText("已用时 04:00");
  expect(calls.generations).toBe(1);
  await page.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(progress).not.toBeVisible();
});

test("reopening a failed generation retains a readable reason and collapses its technical code", async ({ page }) => {
  const { creation, calls } = await creationFixture(page);
  creation.proposal_generation = { ref: "failed-generation", basis_revision: 1, basis_hash: "a".repeat(64), status: "failed",
    adapter_kind: "production", attempt_count: 1, started_at: 10, completed_at: 20,
    failure: { code: "proposal_output_invalid" }, literature_snapshot_ref: null };
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const progress = page.getByTestId("first-question-progress");
  await expect(progress).toContainText("系统未能完成本次准备结果的接纳，尚未形成可确认的问题草稿。");
  await expect(progress.locator("code")).not.toBeVisible();
  await progress.getByText("查看原因代码", { exact: true }).click();
  await expect(progress.locator("code")).toHaveText("proposal_output_invalid");
  await page.reload();
  await expect(progress).toContainText("系统未能完成本次准备结果的接纳，尚未形成可确认的问题草稿。");
  await expect(progress.locator("code")).not.toBeVisible();
  expect(calls.generations).toBe(0);
  // A historical generation cannot explain a newly edited draft's current state.
  creation.quest_draft.revision = 2;
  creation.quest_draft.hash = "b".repeat(64);
  await page.reload();
  await expect(progress).toHaveCount(0);
});
