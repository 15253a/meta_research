import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function creationDialog(page: Page, busy = false) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const value = { goal: "保留这个创建目标", completion_criteria: "保留已经审阅的边界", time_budget: "30d", research_style: "balanced", route: "direct", resource_envelope_ref: null, resource_envelope_hash: null,
    background_and_initial_direction: "保留现有草稿和材料归属", literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] } };
  let nativeSessionRef: string | null = "native-before";
  const current = {
    initialization_id: "init-native-switch", creation_context: "quest_initialization", route: "direct", status: "draft",
    quest_draft: { revision: 1, hash: "1".repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value },
    compute: null, resource_envelope: null, proposal_generation: null, proposal: null, confirmation_preview: null,
    intent_session: { ref: "creation-intent", status: "open", native_session_generation: 1, get native_session_ref() { return nativeSessionRef; }, workspace_ref: "creation-workspace", workspace_path: "/data/init-native-switch",
      can_start_new_session: !busy, switch_block_reason: busy ? "companion_session_busy" : null, native_sessions: [{ generation: 1, native_session_ref: "native-before", created_at: 1 }],
      turns: [{ ref: "old-turn", ordinal: 1, basis_revision: 1, basis_hash: "1".repeat(64), user_content: "已有创建讨论", assistant_content: "旧会话产物仍可按需读取。", assistant_status: "completed", native_session_generation: 1, native_session_ref: "native-before" }] },
    acquisition_session: null, deepfetch: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } }, receipts: {},
  };
  snapshot.quest_creation.current = current;
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  let releaseSwitch: (() => void) | null = null;
  const switchGate = new Promise<void>(done => { releaseSwitch = done; });
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/quest-initializations/current" || url.pathname === "/api/v1/quest-initializations/init-native-switch") return json(current);
    if (url.pathname === "/api/v1/quest-initializations/init-native-switch/intent-session/new") {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      if (busy) return json({ detail: { code: "companion_session_busy" } }, 409);
      await switchGate;
      current.intent_session.native_session_generation = 2;
      nativeSessionRef = null;
      return json(current);
    }
    if (url.pathname.endsWith("/intent-session/messages")) {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      return json(current);
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  return { current, writes, release: () => releaseSwitch?.() };
}

test("creation /new rotates its actual initialization session without sending chat or changing the draft", async ({ page }) => {
  const state = await creationDialog(page);
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const assistant = dialog.getByTestId("quest-intent-session");
  const input = assistant.getByLabel("在 Quest Drafting Session 中发消息");
  await input.fill("/new");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  try {
    await expect.poll(() => state.writes.at(-1)?.path).toBe("/api/v1/quest-initializations/init-native-switch/intent-session/new");
    await expect(input).toBeDisabled();
    await expect(assistant.getByTestId("creation-intent-session-state")).toContainText("正在准备新会话");
  } finally { state.release(); }
  await expect(assistant.getByTestId("creation-intent-session-state")).toContainText("会话 2 · 等待首条消息启动");
  await expect(assistant).toContainText("旧会话产物仍可按需读取。");
  await expect(assistant).toContainText("/data/init-native-switch");
  await expect(input).toHaveValue("");
  await expect(input).toBeEnabled();
  await expect(dialog.getByLabel("目标", { exact: true })).toHaveValue("保留这个创建目标");
  await expect(dialog.getByLabel("边界", { exact: true })).toHaveValue("保留已经审阅的边界");
  expect(state.writes).toEqual([{ path: "/api/v1/quest-initializations/init-native-switch/intent-session/new", body: {} }]);
  expect(state.current.quest_draft.value.goal).toBe("保留这个创建目标");
  expect(state.current.quest_draft.revision).toBe(1);
  expect(state.current.intent_session.native_session_ref).toBeNull();
});

test("creation /new explains busy work and keeps the command for retry without sending it as chat", async ({ page }) => {
  const state = await creationDialog(page, true);
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const assistant = dialog.getByTestId("quest-intent-session");
  const input = assistant.getByLabel("在 Quest Drafting Session 中发消息");
  await input.fill("/new");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("当前创建工作仍在执行或排队，请结束后再输入 /new。");
  await expect(input).toHaveValue("/new");
  await expect(input).toBeEnabled();
  expect(state.writes).toEqual([]);
  expect(state.current.intent_session.native_session_generation).toBe(1);
});
