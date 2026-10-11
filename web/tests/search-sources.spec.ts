import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { QuestCreationView, SearchSourceForm, SearchSourceSelection, SearchSourceTestReport, SearchSourceView } from "../src/api";

const report: SearchSourceTestReport = {
  test_ref: "test-configuration-1", configuration_token: "private-keyed-token", tested_at: "2026-10-10T12:00:00Z",
  capabilities: {
    connection: { status: "verified", reason: "连接已完成" }, authentication: { status: "unverified", reason: "无需认证" },
    search: { status: "limited", reason: "结果为空，未验证论文发现" }, abstract: { status: "unverified", reason: "未验证摘要" },
    page_read: { status: "verified", reason: "已读取有界网页" }, fulltext: { status: "unverified", reason: "未验证论文全文" },
  }, tools: [], result_count: 0,
};
const source: SearchSourceView = {
  source_id: "source_fixture", version: 1, form: { kind: "website", name: "Saved website", url: "https://papers.example.org/", instructions: "" },
  credential_present: false, connection_present: false, test: null, needs_retest: true,
};
async function workspace(page: Page, preQuest = false, withCreation = false, openManager = true) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const questRef: string = snapshot.research_space.current_quest.quest_ref;
  const creation: QuestCreationView = {
    initialization_id: "init-source-fixture", creation_context: "quest_initialization", route: "direct", status: "draft",
    quest_draft: { revision: 1, hash: "draft-fixture", schema_ref: "quest-draft/v2", value: {
      goal: "Fixture research", completion_criteria: "Compare evidence", time_budget: "30d", research_style: "balanced", route: "direct",
      resource_envelope_ref: null, resource_envelope_hash: null, literature: { mode: "oa_only", library_entry_url: "", institution_required: false, scope_exclusions: "", accepted_material_bindings: [] }, background_and_initial_direction: "",
    } }, compute: null, resource_envelope: null, proposal_generation: null, proposal: null, confirmation_preview: null, intent_session: null, acquisition_session: null, deepfetch: null,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: { human_confirmation: { status: "not_attempted" }, quest_goal: { status: "not_attempted" }, broad_research_authorization: { status: "not_attempted" }, question_content: { status: "not_attempted" }, question_identity: { status: "not_attempted" }, cycle_activation: { status: "not_attempted" } }, recovery: null, canonical_empty_advancement: true,
  };
  if (withCreation) {
    snapshot.quest_creation.current = creation;
    snapshot.readiness.checks = [{ name: "fixture_creation", status: "ready" }];
  }
  if (preQuest) {
    snapshot.research_space.current_quest = { ...snapshot.research_space.current_quest, status: "not_bound", quest_ref: null, guidance_alignment: [] };
    snapshot.research_space.current_question = { ...snapshot.research_space.current_question, status: "not_bound", quest_ref: null, question_ref: null };
    snapshot.research_space.status = "empty"; snapshot.research_control.foreground = null; snapshot.research_control.quest_ref = null;
  }
  const state = {
    snapshot, creation, questRef, sources: [structuredClone(source)],
    selections: { [questRef]: { scope: { kind: "quest", quest_ref: questRef }, revision: 0, allowed_source_ids: [], selection_hash: "empty" } } as Record<string, SearchSourceSelection>,
    tests: [] as Array<{ form: SearchSourceForm; source_id?: string; expected_version?: number; probe_query: string }>,
    saves: [] as Array<{ form: SearchSourceForm; expected_version?: number; matching_test_ref?: string }>,
    selectionSaves: [] as Array<{ scope: string; allowed_source_ids: string[]; expected_revision: number }>,
    staleSource: false, staleSelection: false, failSourceSave: false, failSelectionSave: false,
    mutations: [] as string[], delayedQuest: "", release: null as (() => void) | null, creationReads: 0,
  };
  state.selections[creation.initialization_id] = { scope: { kind: "initialization", initialization_id: creation.initialization_id }, revision: 0, allowed_source_ids: [], selection_hash: "empty" };
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") state.mutations.push(url.pathname);
    if (url.pathname === `/api/v1/quest-initializations/${creation.initialization_id}`) { state.creationReads += 1; return json(creation); }
    if (url.pathname === "/api/v1/search-sources/test") {
      state.tests.push(request.postDataJSON()); return json(report);
    }
    if (url.pathname === "/api/v1/search-sources" && request.method() === "GET") return json({ sources: state.sources, templates: [{ template_id: "crossref", name: "Crossref" }] });
    if (url.pathname === "/api/v1/search-sources" || url.pathname.startsWith("/api/v1/search-sources/")) {
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      const body = request.postDataJSON(); state.saves.push(body);
      if (state.staleSource) return json({ detail: { code: "search_source_stale" } }, 409);
      if (state.failSourceSave) return json({ detail: { code: "search_source_save_unavailable" } }, 503);
      const form: SearchSourceForm = body.form;
      const existing = state.sources.find(item => url.pathname.endsWith(`/${item.source_id}`));
      const saved: SearchSourceView = {
        source_id: existing?.source_id ?? `source_${state.saves.length}`, version: state.saves.length + 1,
        form: form.kind === "website" ? form : form.kind === "api" ? form.template === "crossref"
          ? { kind: "api", template: "crossref", name: form.name, instructions: form.instructions }
          : { kind: "api", template: "custom", name: form.name, instructions: form.instructions, contract: form.contract }
          : { kind: "mcp", name: form.name, instructions: form.instructions, connection_transport: form.connection.mode === "replace" ? form.connection.value.transport : "stdio" },
        credential_present: form.kind === "api" && form.credential.mode !== "clear", connection_present: form.kind === "mcp",
        test: body.matching_test_ref ? report : null, needs_retest: !body.matching_test_ref,
      };
      state.sources = [...state.sources.filter(item => item.source_id !== saved.source_id), saved];
      return json({ source: saved, receipt: { scope: "shared", source_id: saved.source_id, version: saved.version } }, existing ? 200 : 201);
    }
    const selected = /^\/api\/v1\/(?:quests|quest-initializations)\/([^/]+)\/search-sources$/.exec(url.pathname);
    if (selected) {
      const selectedQuest = decodeURIComponent(selected[1]);
      const selection = state.selections[selectedQuest];
      if (request.method() === "GET") {
        const value = structuredClone(selection);
        if (selectedQuest === state.delayedQuest) await new Promise<void>(done => { state.release = done; });
        return json({ selection: value, sources: state.sources }).catch(() => {});
      }
      const body = request.postDataJSON(); state.selectionSaves.push({ scope: selectedQuest, ...body });
      if (state.staleSelection) return json({ detail: { code: "search_source_selection_stale" } }, 409);
      if (state.failSelectionSave) return json({ detail: { code: "search_source_selection_unavailable" } }, 503);
      state.selections[selectedQuest] = { ...selection, revision: selection.revision + 1, allowed_source_ids: body.allowed_source_ids };
      return json({ selection: state.selections[selectedQuest], receipt: { scope: selection.scope, revision: state.selections[selectedQuest].revision } });
    }
    if (url.pathname.endsWith("/runtime-conditions")) return json({ quest_ref: snapshot.research_space.current_quest.quest_ref, text: "Research carefully.", revision: "r1" });
    if (request.method() !== "GET") return route.abort();
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/external-mcp") return json({ revision: "generic-mcp-empty", services: [], root_kinds: [] });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?workspace=1", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "修改配置", exact: true }).click();
  await expect(settings(page)).toBeVisible();
  if (openManager) await openShared(page);
  return state;
}
const settings = (page: Page) => page.getByRole("region", { name: "DeepFetch 搜索源", exact: true });
const shared = (page: Page) => settings(page).getByRole("region", { name: "共享搜索源管理", exact: true });
const selection = (page: Page) => settings(page).getByRole("region", { name: "当前研究允许来源", exact: true });
const management = (page: Page) => settings(page).locator("details").filter({
  has: page.getByRole("region", { name: "共享搜索源管理", exact: true, includeHidden: true }),
});
async function openShared(page: Page) {
  const disclosure = management(page);
  if (await disclosure.getAttribute("open") === null) {
    await disclosure.locator("summary").click();
  }
  await expect(shared(page).getByRole("button", { name: "添加搜索源", exact: true })).toBeEnabled();
}
async function add(page: Page, kind: "website" | "api" | "mcp", name: string) {
  await openShared(page);
  await shared(page).getByRole("button", { name: "添加搜索源", exact: true }).click();
  await shared(page).getByRole("combobox", { name: "来源类型", exact: true }).selectOption(kind);
  await shared(page).getByRole("textbox", { name: "来源名称", exact: true }).fill(name);
}

test("closing and reopening retains the Quest source draft without saving it", async ({ page }) => {
  const state = await workspace(page);
  await selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true }).check();
  await add(page, "mcp", "未保存的搜索连接");
  await shared(page).getByRole("textbox", { name: "来源使用说明", exact: true }).fill("仅供当前初始化核对");
  const connection = shared(page).getByLabel("MCP 连接配置", { exact: true });
  await connection.fill('{"transport":"streamable_http","url":"https://example.org/mcp","headers":{}}');
  await page.getByRole("button", { name: "关闭修改配置", exact: true }).click();
  await page.getByRole("button", { name: "修改配置", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true })).toBeChecked();
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("未保存的搜索连接");
  await expect(connection).toHaveValue('{"transport":"streamable_http","url":"https://example.org/mcp","headers":{}}');
  expect(state.mutations).toEqual([]);
});

test("website draft test is separate from shared save and per-Quest selection", async ({ page }) => {
  const state = await workspace(page);
  await add(page, "website", "New website");
  await shared(page).getByRole("textbox", { name: "网站地址", exact: true }).fill("https://example.org/papers");
  await shared(page).getByRole("button", { name: "测试当前配置", exact: true }).click();
  const tested = shared(page).getByRole("region", { name: "当前配置测试结果", exact: true });
  await expect(tested).toContainText("2026-10-10T12:00:00Z");
  await expect(tested).toContainText("未验证论文全文");
  expect(state.mutations).toEqual(["/api/v1/search-sources/test"]);
  expect(state.tests[0]).toEqual({ form: { kind: "website", name: "New website", instructions: "", url: "https://example.org/papers" }, probe_query: "deep learning" });
  await shared(page).getByRole("textbox", { name: "来源使用说明", exact: true }).fill("Read methods.");
  await expect(tested).toHaveCount(0);
  await shared(page).getByRole("button", { name: "测试当前配置", exact: true }).click();
  await expect(tested).toBeVisible();
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(shared(page)).toContainText("当前研究的允许来源未自动改变");
  expect(state.saves[0].matching_test_ref).toBe("test-configuration-1");
  await expect(selection(page).getByRole("checkbox", { name: "允许 New website", exact: true })).not.toBeChecked();
  await selection(page).getByRole("checkbox", { name: "允许 New website", exact: true }).check();
  await expect(selection(page)).toContainText("未保存的修改");
  await selection(page).getByRole("button", { name: "保存当前 Quest 允许来源", exact: true }).click();
  await expect(selection(page)).toContainText("已保存当前 Quest允许来源，修订 1");
  expect(state.selectionSaves).toEqual([{ scope: state.questRef, allowed_source_ids: ["source_1"], expected_revision: 0 }]);
  await page.getByRole("button", { name: "关闭修改配置", exact: true }).click();
  await page.getByRole("button", { name: "修改配置", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 New website", exact: true })).toBeChecked();
  await shared(page).getByRole("button", { name: "New website", exact: true }).click();
  await expect(shared(page).getByRole("region", { name: "当前配置测试结果", exact: true })).toContainText("2026-10-10T12:00:00Z");
});

test("custom GET submits explicit mapping and private API key without secret echo", async ({ page }) => {
  const state = await workspace(page);
  await add(page, "api", "Mapped API");
  await expect(shared(page)).toContainText("无需 API Key");
  await shared(page).getByRole("combobox", { name: "API 模板", exact: true }).selectOption("custom");
  await shared(page).getByRole("textbox", { name: "GET 端点", exact: true }).fill("https://api.example.org/search");
  await shared(page).getByRole("textbox", { name: "查询参数名", exact: true }).fill("search");
  await shared(page).getByRole("textbox", { name: "固定参数", exact: true }).fill('{"format":"json"}');
  await shared(page).getByRole("combobox", { name: "认证方式", exact: true }).selectOption("header");
  await shared(page).getByRole("textbox", { name: "认证字段名", exact: true }).fill("X-Api-Key");
  await shared(page).getByRole("textbox", { name: "结果数组路径", exact: true }).fill("data.results");
  await shared(page).getByRole("textbox", { name: "DOI字段路径", exact: true }).fill("ids.doi");
  await shared(page).getByRole("combobox", { name: "API Key 操作", exact: true }).selectOption("replace");
  await shared(page).getByLabel("新 API Key", { exact: true }).fill("fixture-private-key");
  await expect(shared(page).getByLabel("新 API Key", { exact: true })).toHaveAttribute("type", "password");
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(shared(page)).toContainText("共享来源「Mapped API」已保存");
  expect(state.saves[0].form).toEqual({ kind: "api", name: "Mapped API", instructions: "", template: "custom", credential: { mode: "replace", value: "fixture-private-key" }, contract: {
    endpoint: "https://api.example.org/search", method: "GET", query_parameter: "search", limit_parameter: "limit", fixed_parameters: { format: "json" }, auth: { kind: "header", name: "X-Api-Key" },
    items_path: ["data", "results"], fields: { title: ["title"], url: ["url"], doi: ["ids", "doi"] }, result_kind: "paper_metadata",
  } });
  await expect(shared(page)).not.toContainText("fixture-private-key");
  await expect(shared(page).getByRole("combobox", { name: "API Key 操作", exact: true })).toHaveValue("keep");
  await shared(page).getByRole("button", { name: "重新测试当前配置", exact: true }).click();
  await expect(shared(page).getByRole("region", { name: "当前配置测试结果", exact: true })).toBeVisible();
  expect(state.tests[0]).toMatchObject({ source_id: "source_1", expected_version: 2, form: { credential: { mode: "keep" } } });
});

test("direct MCP works with empty generic MCP and keeps saved private connection", async ({ page }) => {
  const state = await workspace(page, true);
  await add(page, "mcp", "Private MCP");
  const connection = { transport: "stdio", command: "python", arguments: ["fixture.py", "secret-argument"], environment: { TOKEN: "private-token" } };
  await shared(page).getByLabel("MCP 连接配置", { exact: true }).fill(JSON.stringify(connection));
  await expect(shared(page).getByLabel("MCP 连接配置", { exact: true })).toHaveAttribute("type", "password");
  await shared(page).getByRole("button", { name: "测试当前配置", exact: true }).click();
  await expect(shared(page).getByRole("region", { name: "当前配置测试结果", exact: true })).toContainText("2026-10-10T12:00:00Z");
  expect(state.tests[0].form).toEqual({ kind: "mcp", name: "Private MCP", instructions: "", connection: { mode: "replace", value: connection } });
  expect(state.saves).toHaveLength(0);
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(shared(page)).toContainText("MCP 连接已配置");
  await expect(shared(page).getByRole("combobox", { name: "MCP 连接操作", exact: true })).toHaveValue("keep");
  await expect(shared(page).getByLabel("MCP 连接配置", { exact: true })).toHaveCount(0);
  await shared(page).getByRole("button", { name: "重新测试当前配置", exact: true }).click();
  await expect.poll(() => state.tests.length).toBe(2);
  await expect(shared(page).getByRole("region", { name: "当前配置测试结果", exact: true })).toBeVisible();
  expect(state.tests[1].form).toEqual({ kind: "mcp", name: "Private MCP", instructions: "", connection: { mode: "keep" } });
  await expect(selection(page)).toContainText("目前可管理共享来源");
  await expect(selection(page).getByRole("button")).toHaveCount(0);
  expect(state.selectionSaves).toHaveLength(0);
});

test("source and selection conflicts preserve drafts until explicit latest read", async ({ page }) => {
  const state = await workspace(page);
  await shared(page).getByRole("button", { name: "Saved website", exact: true }).click();
  await shared(page).getByRole("textbox", { name: "来源名称", exact: true }).fill("Keep local source");
  state.staleSource = true;
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(settings(page).getByRole("alert")).toContainText("当前草稿已保留");
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("Keep local source");
  await expect(shared(page).getByRole("button", { name: "保存共享来源", exact: true })).toBeDisabled();
  state.sources[0] = { ...state.sources[0], version: 8, form: { ...source.form, name: "Latest website" } };
  await settings(page).getByRole("button", { name: "读取最新来源并替换当前编辑", exact: true }).click();
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("Latest website");
  await selection(page).getByRole("checkbox", { name: "允许 Latest website", exact: true }).check();
  state.staleSelection = true;
  await selection(page).getByRole("button", { name: "保存当前 Quest 允许来源", exact: true }).click();
  await expect(selection(page).getByRole("alert")).toContainText("勾选草稿已保留");
  await expect(selection(page).getByRole("checkbox", { name: "允许 Latest website", exact: true })).toBeChecked();
  state.selections[state.questRef] = { ...state.selections[state.questRef], revision: 9, allowed_source_ids: [] };
  await settings(page).getByRole("button", { name: "读取最新选择并替换当前勾选", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Latest website", exact: true })).not.toBeChecked();
});

test("Quest switch rejects stale selection read and clears editor ownership", async ({ page }) => {
  const state = await workspace(page);
  await page.getByRole("button", { name: "关闭修改配置", exact: true }).click();
  state.delayedQuest = state.questRef;
  await page.getByRole("button", { name: "修改配置", exact: true }).click();
  await expect.poll(() => state.release !== null).toBe(true);
  const nextQuest = "quest-next";
  state.selections[nextQuest] = { scope: { kind: "quest", quest_ref: nextQuest }, revision: 4, allowed_source_ids: ["source_fixture"], selection_hash: "next" };
  state.snapshot.research_space.current_quest.quest_ref = nextQuest;
  state.snapshot.research_space.current_question.quest_ref = nextQuest;
  state.snapshot.research_control.quest_ref = nextQuest;
  state.snapshot.research_control.foreground.quest_ref = nextQuest;
  state.snapshot.revision += 1;
  await expect(settings(page)).toHaveCount(0);
  await page.getByRole("button", { name: "修改配置", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true })).toBeChecked();
  state.release?.();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true })).toBeChecked();
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveCount(0);
  await selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true }).uncheck();
  await selection(page).getByRole("button", { name: "保存当前 Quest 允许来源", exact: true }).click();
  await expect(selection(page)).toContainText("修订 5");
  expect(state.selectionSaves).toEqual([{ scope: nextQuest, allowed_source_ids: [], expected_revision: 4 }]);
});

test("failed shared save retains its draft through folding and explicit cancel does not apply a source", async ({ page }, testInfo) => {
  const state = await workspace(page, false, false, false);
  await expect(management(page)).not.toHaveAttribute("open");
  await expect(shared(page)).toBeHidden();
  await expect(settings(page)).toContainText("OpenAlex");
  await expect(settings(page)).toContainText("Web");
  await add(page, "website", "待保存的网站");
  await shared(page).getByRole("textbox", { name: "网站地址", exact: true }).fill("https://example.org/pending");
  state.failSourceSave = true;
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(settings(page).getByRole("alert")).toContainText("当前编辑已保留");
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("待保存的网站");
  await management(page).locator("summary").click();
  await expect(shared(page)).toBeHidden();
  await expect(settings(page).getByRole("alert")).toBeVisible();
  await openShared(page);
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("待保存的网站");
  await expect(shared(page).getByRole("textbox", { name: "网站地址", exact: true })).toHaveValue("https://example.org/pending");
  await settings(page).screenshot({ path: testInfo.outputPath("shared-save-failed-retained.png") });
  await shared(page).getByRole("button", { name: "取消编辑", exact: true }).click();
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveCount(0);
  await expect(settings(page).getByRole("alert")).toHaveCount(0);
  expect(state.sources).toEqual([source]);
  expect(state.selectionSaves).toEqual([]);
  expect(state.mutations).toEqual(["/api/v1/search-sources"]);
});

test("pending creation sources guide generation to explicit apply and failed apply can be discarded without losing shared edits", async ({ page }, testInfo) => {
  const state = await workspace(page, true, true, false);
  state.creation.resource_envelope = {
    ref: "envelope-fixture", hash: "envelope-hash", schema_ref: "quest-resource-envelope/v1", status: "current",
    host_snapshot_ref: "host-fixture", time_budget: "30d", hard_ceiling: { kind: "wall_clock", seconds: 30 * 24 * 3600 },
    selected_device_uuids: [],
  };
  const draft = state.creation.quest_draft.value;
  if (!("resource_envelope_ref" in draft)) throw new Error("This fixture requires a v2 Quest draft.");
  draft.resource_envelope_ref = "envelope-fixture";
  draft.resource_envelope_hash = "envelope-hash";
  await page.getByRole("button", { name: "关闭修改配置", exact: true }).click();
  await page.getByRole("button", { name: "创建研究任务", exact: true }).click();
  const creation = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题", exact: true });
  const proposal = creation.locator("[data-journey-section='question-proposal']");
  const generate = proposal.getByRole("button", { name: "生成第一个问题", exact: true });
  await expect(generate).toBeEnabled();
  await add(page, "website", "独立保留的编辑");
  await shared(page).getByRole("textbox", { name: "网站地址", exact: true }).fill("https://example.org/still-editing");
  await selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true }).check();
  await expect(proposal).toContainText("补充来源尚未保存，先保存或撤销后再生成。");
  await expect(generate).toBeDisabled();
  const apply = selection(page).getByRole("button", { name: "保存创建草稿允许来源", exact: true });
  await proposal.getByRole("button", { name: "查看并保存搜索源选择", exact: true }).click();
  await expect(apply).toBeFocused();
  state.failSelectionSave = true;
  await apply.click();
  await expect(selection(page).getByRole("alert")).toContainText("当前编辑已保留");
  await expect(selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true })).toBeChecked();
  await expect(generate).toBeDisabled();
  expect(state.selections[state.creation.initialization_id].allowed_source_ids).toEqual([]);
  await proposal.screenshot({ path: testInfo.outputPath("pending-source-generation-guidance.png") });
  await selection(page).getByRole("button", { name: "撤销选择修改", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Saved website", exact: true })).not.toBeChecked();
  await expect(apply).toHaveCount(0);
  await expect(generate).toBeEnabled();
  await expect(shared(page).getByRole("textbox", { name: "来源名称", exact: true })).toHaveValue("独立保留的编辑");
  expect(state.selectionSaves).toEqual([{ scope: state.creation.initialization_id, allowed_source_ids: ["source_fixture"], expected_revision: 0 }]);
  expect(state.saves).toEqual([]);
  expect(state.mutations).toEqual(["/api/v1/quest-initializations/init-source-fixture/search-sources"]);
});

test("creation saves initialization selection without rewriting the draft or starting research", async ({ page }, testInfo) => {
  const state = await workspace(page, true, true);
  await page.getByRole("button", { name: "关闭修改配置", exact: true }).click();
  await page.getByRole("button", { name: "创建研究任务", exact: true }).click();
  await expect(selection(page).getByRole("button", { name: "保存创建草稿允许来源", exact: true })).toHaveCount(0);
  await add(page, "api", "Creation Crossref");
  await shared(page).getByRole("button", { name: "测试当前配置", exact: true }).click();
  await expect(shared(page).getByRole("region", { name: "当前配置测试结果", exact: true })).toBeVisible();
  expect(state.mutations).toEqual(["/api/v1/search-sources/test"]);
  await shared(page).getByRole("button", { name: "保存共享来源", exact: true }).click();
  await expect(selection(page).getByRole("checkbox", { name: "允许 Creation Crossref", exact: true })).not.toBeChecked();
  await selection(page).getByRole("checkbox", { name: "允许 Creation Crossref", exact: true }).check();
  await selection(page).getByRole("button", { name: "保存创建草稿允许来源", exact: true }).click();
  await expect(selection(page)).toContainText("已保存创建草稿允许来源，修订 1");
  expect(state.selectionSaves).toEqual([{ scope: "init-source-fixture", allowed_source_ids: ["source_1"], expected_revision: 0 }]);
  expect(state.mutations).toEqual(["/api/v1/search-sources/test", "/api/v1/search-sources", "/api/v1/quest-initializations/init-source-fixture/search-sources"]);
  await expect.poll(() => state.creationReads).toBe(2);
  await expect(page.getByRole("textbox", { name: "目标", exact: true })).toHaveValue("Fixture research");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await settings(page).scrollIntoViewIfNeeded();
    const bounds = await settings(page).boundingBox();
    expect(bounds?.x).toBeGreaterThanOrEqual(0);
    expect((bounds?.x ?? width) + (bounds?.width ?? width)).toBeLessThanOrEqual(width);
    await settings(page).screenshot({ path: testInfo.outputPath(`search-sources-creation-${width}.png`) });
  }
});
