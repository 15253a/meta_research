import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { AcquisitionSessionProjection } from "../src/api";

async function openCreation(page: Page, failedAcquisition = false) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const receiver = { kind: "creation", initialization_id: "init-material-ui", workspace_ref: "creation-workspace" };
  const source = {
    server: { server_ref: "fixture-server", hostname: "research-host", platform: "Linux", permission_context: "researcher" },
    absolute_path: "/research/materials", kind: "directory", description: "此前研究的论文与结果。",
    observation: { device: "1", inode: "2", kind: "directory", size: "128", modified_ns: "1", changed_ns: "1", observation_ref: "observation-ui" },
    availability: "available",
  };
  const receipt = { submission_ref: "submission-ui", receiver, references: [{
    reference_ref: "reference-ui", submission_ref: "submission-ui", receiver, source,
    description: source.description, availability: "available", read_state: "not_read", read_ranges: [], failures: [], unexpanded: true, treatments: [],
  }] };
  const acquisitionSession: AcquisitionSessionProjection | null = failedAcquisition ? {
    session_ref: "acquisition-ui", status: "unavailable", freshness: "current", mode: "oa_only",
    preflight_generation: 1, request_count: 0, current_request_id: null, slot_held: false,
    browser_context: "unavailable", reason: { code: "codex_operation_failed" },
  } : null;
  const current = {
    initialization_id: "init-material-ui", creation_context: "quest_initialization", route: "direct", status: "draft",
    quest_draft: { revision: 1, hash: "1".repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "接续已有研究", completion_criteria: "保留材料并核对第一问", time_budget: "30d", research_style: "balanced", route: "direct",
      resource_envelope_ref: null, resource_envelope_hash: null, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
    } },
    compute: null, resource_envelope: null, proposal_generation: null, proposal: null, confirmation_preview: null,
    intent_session: null, acquisition_session: acquisitionSession, deepfetch: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: {}, work_materials: [] as Array<typeof receipt>,
  };
  snapshot.quest_creation.current = current;
  snapshot.readiness.checks = [{ name: "fixture_creation", status: "ready" }];
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/search-sources") return json({ sources: [], templates: [] });
    if (url.pathname.endsWith("/search-sources")) return json({ selection: { scope: { kind: "initialization", initialization_id: current.initialization_id }, revision: 0, allowed_source_ids: [], selection_hash: "empty" }, sources: [] });
    if (url.pathname === "/api/v1/quest-initializations/current" || url.pathname === "/api/v1/quest-initializations/init-material-ui") return json(current);
    if (url.pathname.endsWith("/material-receiver")) return json(receiver);
    if (url.pathname === "/api/v1/server-materials/browse") return json({ server: source.server, absolute_path: "/", entries: [], next_cursor: null, unexpanded: true });
    if (url.pathname === "/api/v1/server-materials/inspect") return json(source);
    if (url.pathname.endsWith("/material-references") && request.method() === "POST") {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      current.work_materials = [receipt];
      return json(receipt, 201);
    }
    if (url.pathname.endsWith("/acquisition-session") && request.method() === "POST") {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      current.acquisition_session = { ...current.acquisition_session!, status: "ready", preflight_generation: 2, reason: null };
      return json(current);
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  return { current, writes, errors };
}

test("fulltext resources and first-question route are clearly placed in the left Quest form", async ({ page }) => {
  await openCreation(page);
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const main = dialog.getByRole("main");
  await expect(main.getByLabel("文献全文获取范围", { exact: true })).toHaveValue("oa_only");
  await expect(main.getByRole("option", { name: "仅获取开放全文", exact: true })).toHaveCount(1);
  await expect(main.getByTestId("oa-only-primary-route")).toContainText("仅从公开资源获取文献全文");
  await expect(main.locator("[data-journey-section='route']")).toHaveCount(1);
  await expect(main.getByRole("button", { name: "直接根据目标生成", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("complementary").getByRole("button", { name: "先运行 DeepFetch", exact: true })).toHaveCount(0);
});

test("OA fulltext preparation can retry a visible failure without switching modes", async ({ page }) => {
  const state = await openCreation(page, true);
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const fulltext = dialog.getByRole("main").getByTestId("oa-only-primary-route");
  const status = fulltext.getByTestId("acquisition-session-status");
  const mode = dialog.getByLabel("文献全文获取范围", { exact: true });
  await expect(mode).toHaveValue("oa_only");
  await expect(status).toHaveText("unavailable · current · codex_operation_failed");
  const retry = fulltext.getByRole("button", { name: "重新检测全文获取", exact: true });
  await expect(retry).toBeEnabled();
  await retry.click();
  await expect(status).toHaveText("ready · current · browser not_required");
  await expect(mode).toHaveValue("oa_only");
  expect(state.writes).toEqual([{
    path: "/api/v1/quest-initializations/init-material-ui/acquisition-session",
    body: { expected_draft_revision: 1, expected_draft_hash: "1".repeat(64) },
  }]);
  expect(state.current.quest_draft.revision).toBe(1);
  expect(state.errors).toEqual([]);
});

test("saved research materials keep one compact receipt after ACK and refresh and allow another selection", async ({ page }, testInfo) => {
  const state = await openCreation(page);
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const materials = dialog.getByRole("main").locator("[data-journey-section='materials']");
  await materials.getByRole("button", { name: "选择服务器文件或目录", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择服务器上的原始材料", exact: true });
  await picker.getByLabel("服务器绝对路径").fill("/research/materials");
  await picker.getByLabel("原始材料说明").fill("此前研究的论文与结果。");
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await materials.getByRole("button", { name: "保存材料引用到创建草稿", exact: true }).click();
  await expect.poll(() => state.current.work_materials.length).toBe(1);
  await expect(materials.getByRole("article")).toHaveCount(1);
  await expect(materials.getByText("已保存，尚未读取", { exact: true })).toHaveCount(1);
  await expect(materials.getByText("已保存，尚未读取", { exact: true })).toBeVisible();
  await expect(materials.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeEnabled();
  await expect(materials.getByText("来源与读取记录", { exact: true })).toBeVisible();
  await expect(materials.getByRole("article").getByText("运行账户", { exact: false })).toBeHidden();
  expect(state.writes).toHaveLength(1);
  for (const [width, height] of [[1440, 900], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await materials.scrollIntoViewIfNeeded();
    const size = await materials.evaluate(element => ({ scroll: element.scrollWidth, client: element.clientWidth }));
    expect(size.scroll).toBeLessThanOrEqual(size.client + 1);
    await materials.screenshot({ path: testInfo.outputPath(`materials-${width}.png`) });
  }
  expect(state.errors).toEqual([]);
});
