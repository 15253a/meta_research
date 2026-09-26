import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function setup(page: Page) {
  const state = { revision: 0, servers: [] as Record<string, unknown>[], root_kinds: ["idea", "plan", "bundle", "reasoning", "writing", "companion", "acquisition", "target", "deepfetch"], connection_check: "unsupported", conflict: false };
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/system/mcp")) {
      const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
      if (request.method() === "GET") return json(state);
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      const body = request.postDataJSON();
      if (state.conflict) return json({ detail: { code: "system_mcp_revision_conflict" } }, 409);
      expect(body.expected_revision).toBe(state.revision);
      state.revision += 1;
      if (request.method() === "DELETE") state.servers = [];
      else state.servers = [{ ...body.config, revision: state.revision, connection_status: { status: "unknown", revision: state.revision, checked_at: null } }];
      return json(state, request.method() === "POST" ? 201 : 200);
    }
    if (url.pathname.startsWith("/api/")) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?settings=mcp");
  await expect(page.getByRole("button", { name: "添加 MCP 服务" })).toBeEnabled();
  return state;
}

test("user registers scoped HTTP service, toggles it and removes it", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "添加 MCP 服务" }).click();
  await page.getByLabel("服务标识").fill("camera");
  await page.getByLabel("显示名称").fill("测试摄像头");
  await page.getByLabel("服务 URL").fill("http://127.0.0.1:9000/mcp");
  await page.getByLabel("选择根类型").check();
  await expect(page.getByRole("button", { name: "保存配置" })).toBeDisabled();
  await page.getByLabel("研究助手与草拟", { exact: true }).check();
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("heading", { name: "测试摄像头" })).toBeVisible();
  expect(state.servers[0].scope).toEqual({ mode: "root_kinds", root_kinds: ["companion"] });
  await expect(page.getByText("未知 · 尚无连接证据")).toBeVisible();
  await page.getByRole("button", { name: "停用", exact: true }).click();
  await expect(page.getByText("已停用", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "移除", exact: true }).click();
  await page.getByRole("button", { name: "确认移除" }).click();
  await expect(page.getByText("还没有注册外部 MCP 服务。添加后可供所选根会话使用。")).toBeVisible();
});

test("revision conflict preserves typed stdio connection and mobile layout", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await setup(page);
  await page.getByRole("button", { name: "添加 MCP 服务" }).click();
  await page.getByLabel("服务标识").fill("fixture");
  await page.getByLabel("显示名称").fill("Fixture");
  await page.getByLabel("连接方式", { exact: true }).selectOption("stdio");
  await page.getByLabel("命令", { exact: true }).fill("/usr/bin/python3");
  await page.getByLabel("参数数组（JSON）").fill('["/srv/mcp/server.py", "with spaces"]');
  state.conflict = true;
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("alert")).toContainText("配置已被其他操作更新");
  await expect(page.getByLabel("命令", { exact: true })).toHaveValue("/usr/bin/python3");
  await expect(page.getByLabel("参数数组（JSON）")).toHaveValue('["/srv/mcp/server.py", "with spaces"]');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
