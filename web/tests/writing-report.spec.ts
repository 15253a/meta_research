import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page, fail = false, failCredential = false) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const companion = snapshot.human_collaboration.companion;
  Object.assign(companion, { status: "ready", workspace_path: "/data/companion/quest-stable", messages: [] });
  snapshot.research_assets.items = [
    { version_ref: "template-v1", content_hash: "a".repeat(64), display_name: "报告模板.docx", version_number: 1 },
    { version_ref: "draft-v1", content_hash: "b".repeat(64), display_name: "研究前稿.docx", version_number: 1 },
    { version_ref: "slides-v1", content_hash: "c".repeat(64), display_name: "演示模板.pptx", version_number: 1 },
  ];
  snapshot.research_assets.has_more = false;
  const writes: Array<{ path: string; body: Record<string, any> }> = [];
  const credentials: Array<Record<string, unknown>> = [];
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/companion/content-credentials") {
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      credentials.push(request.postDataJSON());
      if (failCredential) return json({ detail: { code: request.postDataJSON().api_key } }, 503);
      return json({ api_key_ref: request.postDataJSON().api_key_ref ?? "private-task-credential" });
    }
    if (url.pathname === "/api/v1/companion/messages") {
      const body = request.postDataJSON();
      writes.push({ path: url.pathname, body });
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      if (fail) return json({ detail: { code: "writing_model_unavailable" } }, 409);
      companion.messages.push({ message_ref: "writing:user", scope_ref: companion.scope_ref, role: "user", content: body.message, status: "completed" },
        { message_ref: "writing:assistant", scope_ref: companion.scope_ref, role: "assistant", content: "已收到写作要求，正在读取所选前稿。", status: "completed" });
      return json({ interaction_ref: "writing" });
    }
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  return { snapshot, writes, credentials };
}

for (const kind of ["report", "paper"]) test(`${kind} with PPT submits exact versions to ordinary companion`, async ({ page }) => {
  const state = await workspace(page);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("文稿类型").selectOption(kind);
  await dialog.getByLabel("同时制作配套 PPT").check();
  const requirements = "请保留反证与适用条件。\n按现有证据交稿，实验建议另列。";
  await dialog.getByLabel("写作要求").fill(requirements);
  await dialog.getByLabel("材料用途").selectOption("document_template");
  await dialog.getByLabel("选择材料版本").selectOption("template-v1");
  await dialog.getByRole("button", { name: "加入本次写作" }).click();
  await dialog.getByLabel("材料用途").selectOption("draft");
  await dialog.getByLabel("选择材料版本").selectOption("draft-v1");
  await dialog.getByRole("button", { name: "加入本次写作" }).click();
  await dialog.getByLabel("材料用途").selectOption("presentation_template");
  await dialog.getByLabel("选择材料版本").selectOption("slides-v1");
  await dialog.getByRole("button", { name: "加入本次写作" }).click();
  // A newer inventory version must not change a choice already reviewed here.
  state.snapshot.research_assets.items[1] = { version_ref: "draft-v2", content_hash: "d".repeat(64), display_name: "研究前稿.docx", version_number: 2 };
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(dialog.getByLabel("选择材料版本").locator("option[value=draft-v2]")).toHaveCount(1);
  await dialog.getByLabel("内容模型").selectOption("configured_api");
  await dialog.getByLabel("已配置模型引用").fill("science-model");
  await dialog.getByLabel("输出子目录").fill("reports/november");
  await page.screenshot({ path: test.info().outputPath("writing-request.png"), fullPage: true });
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog).not.toBeVisible();
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  await expect(assistant).toBeVisible();
  await expect(assistant).toContainText("正在读取所选前稿");
  expect(state.writes).toHaveLength(1);
  const call = state.writes[0];
  expect(call.path).toBe("/api/v1/companion/messages");
  expect(call.body.message).toBe(requirements);
  expect(call.body.writing_request).toEqual({ document_type: kind, include_presentation: true, task_scope: "full_document", requirements,
    materials: [
      { version_ref: "template-v1", content_hash: "a".repeat(64), role: "document_template" },
      { version_ref: "draft-v1", content_hash: "b".repeat(64), role: "draft" },
      { version_ref: "slides-v1", content_hash: "c".repeat(64), role: "presentation_template" },
    ], model: { kind: "configured_api", config_ref: "science-model" }, output_directory: "reports/november" });
});

test("local revision preserves scope and requirements when selected model fails", async ({ page }) => {
  const state = await workspace(page, true);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("工作范围").selectOption("revision");
  await dialog.getByLabel("写作要求").fill("只改摘要最后两句。");
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog.getByRole("alert")).toContainText("所选模型暂时不可用");
  await expect(dialog.getByLabel("写作要求")).toHaveValue("只改摘要最后两句。");
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].body.writing_request.task_scope).toBe("revision");
  expect(state.writes[0].body.writing_request.model).toEqual({ kind: "current" });
});

for (const [protocol, endpoint] of [
  ["openai_chat_completions", "https://writing.example/v1/chat/completions"],
  ["openai_responses", "https://writing.example/v1/responses"],
  ["anthropic_messages", "https://writing.example/v1/messages"],
]) test(`task API ${protocol} submits the chosen protocol and only a credential reference`, async ({ page }) => {
  const state = await workspace(page);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("工作范围").selectOption("outline");
  await dialog.getByLabel("写作要求").fill("给跨专业读者写提纲，保留证据限制。");
  await dialog.getByLabel("内容模型").selectOption("task_api");
  await dialog.getByLabel("API 地址", { exact: true }).fill(endpoint);
  await dialog.getByLabel("模型名称", { exact: true }).fill("chosen-science-model");
  await dialog.getByLabel("接口协议").selectOption(protocol);
  await dialog.getByLabel("凭据来源").selectOption("reference");
  await dialog.getByLabel("凭据引用", { exact: true }).fill("writing-key");
  await dialog.getByLabel("调用说明（可选）", { exact: true }).fill("先组织论据，再写结论。不要输出推理草稿。");
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog).not.toBeVisible();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].body.writing_request.model).toEqual({
    kind: "task_api", endpoint, model: "chosen-science-model",
    protocol, api_key_ref: "writing-key", instructions: "先组织论据，再写结论。不要输出推理草稿。",
  });
});

for (const width of [1440, 390]) test(`task API failure and closing retain the task and exact draft at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const state = await workspace(page, true);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("工作范围").selectOption("revision");
  await dialog.getByLabel("写作要求").fill("仅修改所选前稿的摘要，保留其他内容。");
  await dialog.getByLabel("材料用途").selectOption("draft");
  await dialog.getByLabel("选择材料版本").selectOption("draft-v1");
  await dialog.getByRole("button", { name: "加入本次写作" }).click();
  await dialog.getByLabel("内容模型").selectOption("task_api");
  await dialog.getByLabel("API 地址", { exact: true }).fill("https://writing.example/v1/messages");
  await dialog.getByLabel("模型名称", { exact: true }).fill("chosen-science-model");
  await dialog.getByLabel("接口协议").selectOption("anthropic_messages");
  await dialog.getByLabel("凭据来源").selectOption("reference");
  await dialog.getByLabel("凭据引用", { exact: true }).fill("writing-key");
  await dialog.getByLabel("调用说明（可选）", { exact: true }).fill("保留反证和必要专业术语。");
  await dialog.getByLabel("输出子目录").fill("revisions");
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog.getByRole("alert")).toContainText("所选模型暂时不可用");
  expect(state.writes).toHaveLength(1);
  await dialog.getByRole("button", { name: "关闭写作" }).click();
  await expect(dialog).not.toBeVisible();
  expect(state.writes).toHaveLength(1);
  await page.getByRole("button", { name: "写作", exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("工作范围")).toHaveValue("revision");
  await expect(dialog.getByLabel("写作要求")).toHaveValue("仅修改所选前稿的摘要，保留其他内容。");
  await expect(dialog.getByLabel("API 地址", { exact: true })).toHaveValue("https://writing.example/v1/messages");
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue("chosen-science-model");
  await expect(dialog.getByLabel("接口协议")).toHaveValue("anthropic_messages");
  await expect(dialog.getByLabel("凭据引用", { exact: true })).toHaveValue("writing-key");
  await expect(dialog.getByLabel("调用说明（可选）")).toHaveValue("保留反证和必要专业术语。");
  await expect(dialog.getByLabel("输出子目录")).toHaveValue("revisions");
  expect(state.writes[0].body.writing_request.materials).toEqual([
    { version_ref: "draft-v1", content_hash: "b".repeat(64), role: "draft" },
  ]);
  await dialog.getByLabel("内容模型").scrollIntoViewIfNeeded();
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("task-api-restored.png") });
  await dialog.getByRole("region", { name: "本次 API 模型" }).evaluate(element => element.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: test.info().outputPath("task-api-fields.png") });
});

test("closing clears an unsubmitted key and a credential error cannot expose it or submit writing", async ({ page }) => {
  const state = await workspace(page, false, true);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("写作要求").fill("保留本次要求。");
  await dialog.getByLabel("内容模型").selectOption("task_api");
  await dialog.getByLabel("API 地址", { exact: true }).fill("https://writing.example/v1/responses");
  await dialog.getByLabel("模型名称", { exact: true }).fill("chosen-science-model");
  const keyInput = dialog.getByLabel("API 密钥", { exact: true });
  const key = "test-key-that-must-not-be-shown";
  await keyInput.fill(key);
  await dialog.getByRole("button", { name: "关闭写作" }).click();
  expect(state.credentials).toHaveLength(0);
  expect(state.writes).toHaveLength(0);
  await page.getByRole("button", { name: "写作", exact: true }).click();
  await expect(keyInput).toHaveValue("");
  await keyInput.fill(key);
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog.getByRole("alert")).toContainText("凭据未确认保存");
  await expect(dialog).not.toContainText(key);
  await expect(keyInput).toHaveValue("");
  await expect(dialog.getByLabel("写作要求")).toHaveValue("保留本次要求。");
  expect(state.writes).toHaveLength(0);
  expect(state.credentials).toHaveLength(1);
});

test("new API key is provisioned privately and only its reference survives a failed task submission", async ({ page }) => {
  const state = await workspace(page, true);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("写作要求").fill("根据已选材料写报告。");
  await dialog.getByLabel("内容模型").selectOption("task_api");
  await dialog.getByLabel("API 地址", { exact: true }).fill("https://writing.example/v1/responses");
  await dialog.getByLabel("模型名称", { exact: true }).fill("chosen-science-model");
  await dialog.getByLabel("接口协议").selectOption("openai_responses");
  const key = "test-key-sensitive-do-not-retain";
  const keyInput = dialog.getByLabel("API 密钥", { exact: true });
  await expect(keyInput).toHaveAttribute("type", "password");
  await keyInput.fill(key);
  await dialog.getByRole("button", { name: "开始写作" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  expect(state.credentials).toEqual([{ scope_ref: state.snapshot.human_collaboration.companion.scope_ref,
    endpoint: "https://writing.example/v1/responses", model: "chosen-science-model", api_key: key }]);
  await expect(dialog.getByLabel("凭据引用", { exact: true })).toHaveValue("private-task-credential");
  expect(JSON.stringify(state.writes)).not.toContain(key);
  expect(state.writes[0].body.writing_request.model.api_key_ref).toBe("private-task-credential");
  await dialog.getByRole("button", { name: "关闭写作" }).click();
  await page.getByRole("button", { name: "写作", exact: true }).click();
  await expect(dialog.getByLabel("凭据引用", { exact: true })).toHaveValue("private-task-credential");
  await dialog.getByLabel("凭据来源").selectOption("key");
  await expect(keyInput).toHaveValue("");
  expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))).not.toContain(key);
});

test("repairing a credential preserves its reference without submitting a new writing task", async ({ page }) => {
  const state = await workspace(page);
  await page.goto("http://127.0.0.1:18768/?workspace=1&panel=writing");
  const dialog = page.getByRole("dialog", { name: "交给研究助手写作" });
  await dialog.getByLabel("内容模型").selectOption("task_api");
  await dialog.getByLabel("API 地址", { exact: true }).fill("https://writing.example/v1/responses");
  await dialog.getByLabel("模型名称", { exact: true }).fill("chosen-science-model");
  await dialog.getByLabel("凭据来源").selectOption("reference");
  const reference = "writing-0123456789abcdef0123456789abcdef";
  await dialog.getByLabel("凭据引用", { exact: true }).fill(reference);
  await dialog.getByLabel("凭据来源").selectOption("key");
  await expect(dialog).toContainText("替换此引用的密钥");
  await dialog.getByLabel("API 密钥", { exact: true }).fill("test-repaired-private-key");
  await dialog.getByRole("button", { name: "保存密钥", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("在原对话中继续");
  await expect(dialog.getByLabel("凭据引用", { exact: true })).toHaveValue(reference);
  expect(state.credentials).toEqual([{ scope_ref: state.snapshot.human_collaboration.companion.scope_ref,
    endpoint: "https://writing.example/v1/responses", model: "chosen-science-model",
    api_key: "test-repaired-private-key", api_key_ref: reference }]);
  expect(state.writes).toHaveLength(0);
});

test("writing conversation shows exact human request with complete input in collapsed details", async ({ page }) => {
  const state = await workspace(page);
  const original = "  只修改摘要最后两句。\n保留反证与原始数字。  ";
  const complete = `${original}\n\n写作任务（普通助手会话）：\n${JSON.stringify({
    model: { kind: "task_api", endpoint: "https://writing.example/v1/responses", api_key_ref: "writing-key" },
    materials: [{ version_ref: "draft-v1", content_hash: "b".repeat(64), path: "/data/exact-draft.docx" }],
  }, null, 2)}`;
  const ordinary = "普通讨论提到：写作任务（普通助手会话）：\n{这段原文不能被解析删除}";
  const scope_ref = state.snapshot.human_collaboration.companion.scope_ref;
  state.snapshot.human_collaboration.companion.messages = [
    { message_ref: "ordinary:user", scope_ref, role: "user", content: ordinary, status: "completed" },
    { message_ref: "writing:user", scope_ref, role: "user", content: complete, display_message: original, status: "completed" },
    { message_ref: "writing:assistant", scope_ref, role: "assistant", content: "已收到，将保留反证与数字。", status: "completed" },
  ];
  await page.goto("http://127.0.0.1:18768/?workspace=1&companion=1");
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  const writing = assistant.locator("article.me").filter({ has: page.getByText("写作设置与来源", { exact: true }) });
  expect(await writing.locator(".companion-human-message").textContent()).toBe(original);
  const details = writing.locator("details");
  await expect(details).not.toHaveAttribute("open", "");
  await expect(details.locator("pre")).not.toBeVisible();
  expect(await assistant.locator("article.me").first().locator(".companion-human-message").textContent()).toBe(ordinary);
  await expect(assistant.locator("article.me").first().locator("details")).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await details.locator("summary").click();
    await expect(details.locator("pre")).toBeVisible();
    expect(await details.locator("pre").textContent()).toBe(complete);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`writing-details-${width}.png`), fullPage: true });
    await details.locator("summary").click();
    await expect(details.locator("pre")).not.toBeVisible();
  }
  expect(state.writes).toHaveLength(0);
});
