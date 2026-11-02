import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page, fail = false) {
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
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
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
  return { snapshot, writes };
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
  await expect(dialog.getByRole("alert")).toContainText("writing_model_unavailable");
  await expect(dialog.getByLabel("写作要求")).toHaveValue("只改摘要最后两句。");
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].body.writing_request.task_scope).toBe("revision");
  expect(state.writes[0].body.writing_request.model).toEqual({ kind: "current" });
});
