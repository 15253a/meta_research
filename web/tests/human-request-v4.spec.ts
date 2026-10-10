import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const original = snapshot.human_collaboration.human_requests.items[0];
  const request = { ...original, kind: "offline_action", obligation: "交回现场原始记录", required_authorization: null,
    responses: [], evaluation: null, disposition: null, direct_waiters: [] };
  snapshot.human_collaboration.human_requests.items = [request];
  snapshot.human_collaboration.human_requests.waiting = { scope: "local", other_blockers: [], safe_meaningful_runnable_exists: true };
  snapshot.human_collaboration.companion.messages = [];
  snapshot.human_collaboration.companion.agent_proposals = [];
  const writes: string[] = [];
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const operation = route.request(), url = new URL(operation.url());
    if (url.origin !== "http://human-request-v4.test") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (operation.method() !== "GET") { writes.push(url.pathname); return route.abort(); }
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (url.pathname.includes("/handoff")) return json({ request_ref: request.request_ref, revision: request.revision, is_current: true, sections: [] });
    if (url.pathname.startsWith("/api/")) return json({ status: "ready", items: [], sessions: [], cycles: [], questions: [], total: 0 });
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://human-request-v4.test/?workspace=1&panel=offline-operation");
  return { writes, request };
}

test("closing and reopening a HumanRequest keeps unsent response and assistant drafts without submitting", async ({ page }) => {
  const f = await workspace(page);
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog.getByRole("heading", { name: "交回现场原始记录" })).toBeVisible();
  await dialog.getByRole("textbox", { name: "自然语言回应", exact: true }).fill("现场原始记录尚未核对，请保留这段草稿。");
  const assistant = dialog.getByRole("textbox", { name: "就线下操作事项发消息" });
  await assistant.fill("请帮我核对设备编号，尚未发送。");
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("button", { name: "需要你", exact: true }).click();
  const item = dialog.getByRole("button", { name: /交回现场原始记录/ });
  if (await item.isVisible()) await item.click();
  await expect(dialog.getByRole("textbox", { name: "自然语言回应", exact: true })).toHaveValue("现场原始记录尚未核对，请保留这段草稿。");
  await expect(assistant).toHaveValue("请帮我核对设备编号，尚未发送。");
  expect(f.writes).toEqual([]);
});

test("review stays inside its HumanRequest and Escape returns to the editable response", async ({ page }, testInfo) => {
  const f = await workspace(page);
  const outer = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
  await outer.getByRole("textbox", { name: "自然语言回应", exact: true }).fill("设备编号仍需核验，这不是接纳声明。");
  await outer.getByRole("button", { name: "提交", exact: true }).click();
  const review = page.getByRole("dialog", { name: "审阅正式回应", exact: true });
  await expect(review).toContainText("设备编号仍需核验，这不是接纳声明。");
  await expect(review).toContainText(f.request.request_ref);
  expect(f.writes).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(review).toBeHidden();
  await expect(outer).toBeVisible();
  await expect(outer.getByRole("button", { name: "提交", exact: true })).toBeFocused();
  for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 500 }, { width: 390, height: 600 }]) {
    await page.setViewportSize(viewport);
    await expect(outer.getByRole("navigation", { name: "求助处理阶段" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`request-${viewport.width}.png`) });
  }
});
