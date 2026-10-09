import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page, busy = false) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const companion = snapshot.human_collaboration.companion;
  snapshot.human_collaboration.human_requests.items = [];
  Object.assign(companion, {
    native_session_generation: 1, native_session_ref: "native-original", can_start_new_session: !busy,
    switch_block_reason: busy ? "companion_session_busy" : null,
    workspace_ref: "workspace-companion", workspace_path: "/data/companion/quest-stable",
    native_sessions: [{ generation: 1, native_session_ref: "native-original", created_at: 1 }],
    messages: [{ message_ref: "old:assistant", scope_ref: companion.scope_ref, role: "assistant", content: "旧会话核验结果及限制仍可回看。", status: "completed", native_session_generation: 1, native_session_ref: "native-original" }],
  });
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/companion/sessions/new") {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      if (busy) return json({ detail: { code: "companion_session_busy" } }, 409);
      companion.native_session_generation = 2;
      companion.native_session_ref = null;
      return json({ scope_ref: companion.scope_ref, session_ref: companion.session_ref, status: "switched", native_session_generation: 2, native_session_ref: null, previous_native_session_ref: "native-original", switch_ref: "switch-1" });
    }
    if (url.pathname === "/api/v1/companion/messages") {
      const body = request.postDataJSON();
      writes.push({ path: url.pathname, body });
      companion.native_session_ref = "native-second";
      companion.messages.push({ message_ref: "next:user", scope_ref: companion.scope_ref, role: "user", content: body.message, status: "completed", native_session_generation: 2, native_session_ref: "native-second" },
        { message_ref: "next:assistant", scope_ref: companion.scope_ref, role: "assistant", content: "新会话已按需读取旧文件。", status: "completed", native_session_generation: 2, native_session_ref: "native-second" });
      return json({ interaction_ref: "next" });
    }
    const humanRequest = /^\/api\/v1\/human-requests\/([^/]+)(\/handoff|\/responses)?$/.exec(url.pathname);
    if (humanRequest) {
      const item = snapshot.human_collaboration.human_requests.items.find((value: { request_ref: string }) => value.request_ref === decodeURIComponent(humanRequest[1]));
      if (humanRequest[2] === "/handoff") return json({ schema_ref: "meta-research/human-request-handoff/v1", request_ref: item.request_ref, revision: item.revision, status: item.status, is_current: true, title: item.obligation, sections: [{ key: "return", title: "需要提交", paragraphs: ["交回原始核验结果；原请求根负责接纳。"] }] });
      if (humanRequest[2] === "/responses") {
        const body = request.postDataJSON();
        writes.push({ path: url.pathname, body });
        item.responses.push(body);
        return json(item);
      }
      return json(item);
    }
    if (url.pathname === "/api/v1/server-materials/browse") return json({ server: { server_ref: "server", hostname: "test-host", platform: "linux", permission_context: "test" }, absolute_path: url.searchParams.get("path"), entries: [], next_cursor: null, unexpanded: true });
    if (url.pathname === "/api/v1/server-materials/inspect") return json({ server: { server_ref: "server", hostname: "test-host", platform: "linux", permission_context: "test" }, absolute_path: url.searchParams.get("path"), kind: "file", description: url.searchParams.get("description") ?? "", observation: { device: "1", inode: "2", kind: "file", size: "13", modified_ns: "1", changed_ns: "1", observation_ref: "observation-1" }, availability: "available" });
    if (url.pathname === "/api/v1/events") return route.abort();
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  return { snapshot, companion, writes };
}

test("/new rotates the public session while preserving workspace and visible old work", async ({ page }) => {
  const state = await workspace(page);
  await page.goto("http://127.0.0.1:18768/?workspace=1&companion=1", { waitUntil: "domcontentloaded" });
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  const input = assistant.getByLabel("给研究助手发消息");
  await input.fill("/new");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(assistant.getByTestId("companion-session-state")).toContainText("等待首条消息启动");
  await expect(assistant).toContainText("/data/companion/quest-stable");
  await expect(assistant).toContainText("旧会话核验结果及限制仍可回看。");
  expect(state.writes).toEqual([{ path: "/api/v1/companion/sessions/new", body: { scope_ref: state.companion.scope_ref } }]);
  await input.fill("读取保留的核验文件");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(assistant).toContainText("新会话已按需读取旧文件。");
  await expect(assistant.getByTestId("companion-session-state")).toContainText("native-second");
  await expect(assistant).toContainText("会话 1");
  await expect(assistant).toContainText("会话 2");
});

test("a request selects completed assistant work and server artifacts before explicitly submitting to its original root", async ({ page }, testInfo) => {
  const state = await workspace(page);
  const questRef = state.snapshot.research_space.current_quest.quest_ref;
  const requests = ["first", "second"].map(name => ({ request_ref: `HR-${name}:r1`, request_id: `HR-${name}`, revision: 1, quest_ref: questRef, issuer: "agent_runtime", kind: "offline_action", status: "open", obligation: `${name} 原始核验请求`, business_purpose: "只交回当前请求原根", acceptance_conditions: [], direct_waiters: [{ waiter_ref: `root-${name}`, generation: 1, wait_scope: "local", status: "blocked", other_blockers: [] }], responses: [], evaluation: null, disposition: null }));
  state.snapshot.human_collaboration.human_requests.items = requests;
  state.companion.messages = [
    { message_ref: "completed:assistant", scope_ref: state.companion.scope_ref, role: "assistant", status: "completed", content: "已运行核验脚本，结果 4；限制是样本仅一项。", native_session_generation: 1, native_session_ref: "native-original", view_context: { kind: "human_request", quest_ref: questRef, request_ref: requests[0].request_ref, revision: 1 } },
    { message_ref: "unselected:assistant", scope_ref: state.companion.scope_ref, role: "assistant", status: "completed", content: "未选结果不能进入正式回应。", native_session_generation: 1, view_context: { kind: "human_request", quest_ref: questRef, request_ref: requests[0].request_ref, revision: 1 } },
  ];
  await page.goto("http://127.0.0.1:18768/?panel=offline-operation", { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const assistant = dialog.getByRole("complementary", { name: "线下操作相关交流" });
  const formal = dialog.getByRole("main");
  await expect(assistant).toContainText("已运行核验脚本");
  expect(state.writes).toEqual([]);
  await assistant.getByRole("checkbox", { name: "选入正式回应" }).first().check();
  await expect(formal.getByRole("region", { name: "已选助手说明" })).toContainText("限制是样本仅一项");
  await formal.getByRole("button", { name: "选择服务器文件或目录", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择服务器上的原始材料" });
  await expect(picker.getByLabel("服务器绝对路径", { exact: true })).toHaveValue("/data/companion/quest-stable");
  await picker.getByLabel("服务器绝对路径", { exact: true }).fill("/data/companion/quest-stable/result.txt");
  await picker.getByLabel("原始材料说明", { exact: true }).fill("助手实际核验产物");
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker).toBeHidden();
  expect(state.writes).toEqual([]);
  await assistant.getByLabel("就线下操作事项发消息").fill("/new");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(assistant.getByTestId("companion-session-state")).toContainText("会话 2 · 等待首条消息启动");
  await expect(formal.getByRole("region", { name: "已选助手说明" })).toContainText("限制是样本仅一项");
  await expect(formal.locator(".server-material-picker__candidate")).toContainText("/data/companion/quest-stable/result.txt");
  expect(state.writes.filter(write => write.path.endsWith("/responses"))).toEqual([]);
  for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 900 }, { width: 390, height: 620 }]) {
    await page.setViewportSize(viewport);
    const work = await assistant.boundingBox(), response = await formal.boundingBox();
    expect(work).not.toBeNull();
    expect(response).not.toBeNull();
    if (viewport.width === 1440) {
      expect(work!.x + work!.width).toBeLessThanOrEqual(response!.x + 1);
      expect(work!.width).toBeGreaterThan(response!.width);
    } else expect(work!.y + work!.height).toBeLessThanOrEqual(response!.y + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await formal.getByRole("button", { name: "提交", exact: true }).scrollIntoViewIfNeeded();
    await expect(formal.getByRole("button", { name: "提交", exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`companion-human-request-${viewport.width}.png`) });
  }
  await formal.getByRole("button", { name: "提交", exact: true }).click();
  await expect(formal).toContainText("回应已提交");
  await expect(assistant.getByText("已选入正式回应", { exact: true })).toBeVisible();
  const delivered = state.writes.filter(write => write.path.endsWith("/responses"));
  expect(delivered).toHaveLength(1);
  expect(delivered[0].path).toBe("/api/v1/human-requests/HR-first%3Ar1/responses");
  expect(delivered[0].body.note).toContain("已运行核验脚本，结果 4；限制是样本仅一项。");
  expect(JSON.stringify(delivered[0].body)).not.toContain("未选结果不能进入");
  expect(delivered[0].body.materials).toMatchObject([{ kind: "server_reference", selection: { absolute_path: "/data/companion/quest-stable/result.txt" } }]);
  await dialog.getByRole("button", { name: "查看下一个待办" }).click();
  await expect(dialog.getByRole("heading", { name: "second 原始核验请求" })).toBeVisible();
  await expect(formal.getByRole("region", { name: "已选助手说明" })).toHaveCount(0);
  await expect(formal.locator(".server-material-picker__candidate")).toHaveCount(0);
  await expect(formal.getByLabel("自然语言回应", { exact: true })).toHaveValue("");
});

test("busy session switching explains queued work and retains the command without creating a turn", async ({ page }) => {
  const state = await workspace(page, true);
  await page.goto("http://127.0.0.1:18768/?workspace=1&companion=1", { waitUntil: "domcontentloaded" });
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  await assistant.getByLabel("给研究助手发消息").fill("/new");
  await assistant.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(assistant).toContainText("还有消息在执行或排队；等待结束后再输入 /new。");
  await expect(assistant.getByLabel("给研究助手发消息")).toHaveValue("/new");
  expect(state.writes).toEqual([]);
});
