import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { ExternalMcpConfiguration, ExternalMcpConnection, ExternalMcpService } from "../src/api";

const rootKinds = ["idea", "plan", "bundle", "reasoning", "target", "writing", "companion", "deepfetch", "acquisition"];

async function workspace(page: Page, preQuest = false) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  if (preQuest) {
    snapshot.research_space.current_quest = { ...snapshot.research_space.current_quest, status: "not_bound", quest_ref: null, guidance_alignment: [] };
    snapshot.research_space.current_question = { ...snapshot.research_space.current_question, status: "not_bound", quest_ref: null, question_ref: null };
    snapshot.research_space.status = "empty"; snapshot.research_control.foreground = null; snapshot.research_control.quest_ref = null;
  }
  const state = {
    config: { revision: "r1", services: [], root_kinds: rootKinds } as ExternalMcpConfiguration,
    writes: [] as { services: ExternalMcpService[]; expected_revision: string }[],
    tests: [] as { connection: ExternalMcpConnection }[], readFailure: false, stale: false, connectionFailure: false, conditionReads: 0,
  };
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/external-mcp" || url.pathname === "/api/v1/external-mcp/test-connection") {
      if (request.method() === "GET") return state.readFailure ? json({ detail: { code: "unavailable" } }, 503) : json(state.config);
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      if (url.pathname.endsWith("test-connection")) {
        state.tests.push(request.postDataJSON());
        return json(state.connectionFailure ? { status: "failed", reason_code: "authentication_failed" }
          : { status: "ready", server_name: "lab", tool_count: 2 });
      }
      const body = request.postDataJSON(); state.writes.push(body);
      if (state.stale) return json({ detail: { code: "external_mcp_config_stale" } }, 409);
      expect(body.expected_revision).toBe(state.config.revision);
      state.config = { ...state.config, services: body.services, revision: `r${state.writes.length + 1}` };
      return json(state.config);
    }
    if (url.pathname.endsWith("/runtime-conditions")) {
      state.conditionReads += 1;
      return json({ quest_ref: snapshot.research_space.current_quest.quest_ref, text: "Research carefully.", revision: "quest-r1" });
    }
    if (request.method() !== "GET") return route.abort();
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?workspace=1", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "修改配置", exact: true })).toBeEnabled();
  return state;
}

const settings = (page: Page) => page.getByRole("region", { name: "外部 MCP 服务", exact: true });
const open = (page: Page) => page.getByRole("button", { name: "修改配置", exact: true }).click();
const close = (page: Page) => page.getByRole("button", { name: "关闭修改配置", exact: true }).click();

test("pre-Quest settings default to all Roots, save blank instructions and persist explicit none", async ({ page }) => {
  const state = await workspace(page, true);
  await open(page);
  await settings(page).getByRole("button", { name: "添加服务", exact: true }).click();
  await settings(page).getByRole("textbox", { name: "服务名称", exact: true }).fill("Lab instruments");
  await settings(page).getByRole("textbox", { name: "启动命令", exact: true }).fill("python");
  await expect(settings(page).getByRole("checkbox")).toHaveCount(9);
  for (const root of rootKinds) await expect(settings(page).getByRole("checkbox", { name: root, exact: true })).toBeChecked();
  await settings(page).getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(settings(page).getByRole("status")).toContainText("发现 2 个工具；未执行业务操作");
  expect(Object.keys(state.tests[0])).toEqual(["connection"]);
  expect(state.writes).toHaveLength(0);
  await settings(page).getByRole("button", { name: "保存外部 MCP", exact: true }).click();
  await expect(settings(page).getByText("外部 MCP 配置已保存，将用于后续新操作。", { exact: true })).toBeVisible();
  expect(state.writes[0]).toMatchObject({ expected_revision: "r1", services: [{ allowed_root_kinds: rootKinds, research_instructions: "" }] });
  await close(page); await open(page);
  await expect(settings(page).getByRole("textbox", { name: "服务名称", exact: true })).toHaveValue("Lab instruments");
  await settings(page).getByRole("button", { name: "清空选择", exact: true }).click();
  await settings(page).getByRole("button", { name: "保存外部 MCP", exact: true }).click();
  await expect(settings(page).getByText("外部 MCP 配置已保存，将用于后续新操作。", { exact: true })).toBeVisible();
  expect(state.writes[1].services[0].allowed_root_kinds).toEqual([]);
  expect(state.conditionReads).toBe(0);
  await close(page); await open(page);
  for (const root of rootKinds) await expect(settings(page).getByRole("checkbox", { name: root, exact: true })).not.toBeChecked();
});

test("HTTP configuration and research instructions share runtime entry and show useful test failures", async ({ page }) => {
  const state = await workspace(page);
  await open(page);
  await settings(page).getByRole("button", { name: "添加服务", exact: true }).click();
  await settings(page).getByRole("textbox", { name: "服务名称", exact: true }).fill("HTTP Lab");
  await settings(page).getByRole("combobox", { name: "连接方式", exact: true }).selectOption("streamable_http");
  await settings(page).getByRole("textbox", { name: "服务地址", exact: true }).fill("https://lab.example/mcp");
  await settings(page).getByRole("textbox", { name: "请求头", exact: true }).fill('{"Authorization":"Bearer fixture-only"}');
  await settings(page).getByRole("textbox", { name: "研究使用说明", exact: true }).fill("Use the laboratory observations to compare temperatures.");
  state.connectionFailure = true;
  await settings(page).getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(settings(page).getByRole("status")).toContainText("服务拒绝认证，请检查请求头");
  await settings(page).getByRole("button", { name: "保存外部 MCP", exact: true }).click();
  await expect(settings(page).getByText("外部 MCP 配置已保存，将用于后续新操作。", { exact: true })).toBeVisible();
  expect(state.writes[0].services[0]).toMatchObject({ connection: { transport: "streamable_http", url: "https://lab.example/mcp", headers: { Authorization: "Bearer fixture-only" } }, research_instructions: "Use the laboratory observations to compare temperatures." });
  await close(page); await open(page);
  await expect(settings(page).getByRole("textbox", { name: "研究使用说明", exact: true })).toHaveValue("Use the laboratory observations to compare temperatures.");
  expect(state.conditionReads).toBe(2);
});

test("read retry and stale revision preserve visible edits without an overwrite", async ({ page }) => {
  const state = await workspace(page, true);
  state.readFailure = true; await open(page);
  await expect(settings(page).getByRole("alert")).toHaveText("外部 MCP 配置读取失败，请重试。");
  state.readFailure = false;
  await settings(page).getByRole("button", { name: "重新读取外部 MCP", exact: true }).click();
  await settings(page).getByRole("button", { name: "添加服务", exact: true }).click();
  await settings(page).getByRole("textbox", { name: "服务名称", exact: true }).fill("Keep my draft");
  await settings(page).getByRole("textbox", { name: "启动命令", exact: true }).fill("python");
  await settings(page).getByRole("textbox", { name: "研究使用说明", exact: true }).fill("Keep this instruction.");
  state.stale = true;
  await settings(page).getByRole("button", { name: "保存外部 MCP", exact: true }).click();
  await expect(settings(page).getByRole("alert")).toContainText("当前编辑已保留");
  await expect(settings(page).getByRole("textbox", { name: "研究使用说明", exact: true })).toHaveValue("Keep this instruction.");
  await expect(settings(page).getByRole("button", { name: "保存外部 MCP", exact: true })).toBeDisabled();
  expect(state.config.services).toEqual([]);
});

test("system MCP keeps an unsaved connection when configuration is reopened", async ({ page }) => {
  const state = await workspace(page, true);
  await open(page);
  await settings(page).getByRole("button", { name: "添加服务", exact: true }).click();
  await settings(page).getByRole("textbox", { name: "服务名称", exact: true }).fill("尚未保存的系统服务");
  await settings(page).getByRole("textbox", { name: "启动命令", exact: true }).fill("python");
  await close(page); await open(page);
  await expect(settings(page).getByRole("textbox", { name: "服务名称", exact: true })).toHaveValue("尚未保存的系统服务");
  await expect(settings(page).getByRole("textbox", { name: "启动命令", exact: true })).toHaveValue("python");
  expect(state.writes).toEqual([]);
});
