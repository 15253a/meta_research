import { expect, test, type Page, type Route } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

type Json = Record<string, any>;
async function workspace(page: Page, respond: (route: Route, snapshot: Json) => Promise<void>, initial = "/?workspace=1&panel=offline-operation", handoff?: (route: Route) => Promise<void>) {
  const snapshot: Json = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const original = snapshot.human_collaboration.human_requests.items[0];
  const request = { ...original, request_ref: "research_graph:HR-273:r1", request_id: "HR-273", kind: "offline_action", status: "open", obligation: "核对现场原始记录", required_authorization: null, responses: [], evaluation: null, disposition: null, direct_waiters: [] };
  const second = { ...request, request_ref: "research_graph:HR-274:r1", request_id: "HR-274", kind: "external_material_api_access", obligation: "另一个请求仍待处理" };
  snapshot.human_collaboration.human_requests.items = [request, second];
  snapshot.human_collaboration.human_requests.waiting = { scope: "local", other_blockers: [], safe_meaningful_runnable_exists: true };
  snapshot.human_collaboration.companion.messages = [];
  snapshot.human_collaboration.companion.agent_proposals = [];
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.context().addCookies([{ name: "meta_research_csrf", value: "hr-recovery-test", url: "http://localhost:18773" }]);
  await page.route("**/*", async route => {
    const operation = route.request(), url = new URL(operation.url());
    if (url.origin !== "http://localhost:18773") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.endsWith("/responses")) return respond(route, snapshot);
    if (operation.method() !== "GET") return route.abort();
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (url.pathname.includes("/handoff")) return handoff ? handoff(route) : json({ request_ref: request.request_ref, revision: request.revision, is_current: true, sections: [] });
    if (url.pathname.startsWith("/api/")) return json({ status: "ready", items: [], sessions: [], cycles: [], questions: [], total: 0 });
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto(`http://localhost:18773${initial}`);
  return { snapshot, request, second };
}
async function submit(page: Page, note: string) {
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
  await dialog.getByRole("textbox", { name: "自然语言回应", exact: true }).fill(note);
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await page.getByRole("dialog", { name: "审阅正式回应", exact: true }).getByRole("button", { name: "确认提交回应", exact: true }).click();
  return dialog;
}

test("lost acknowledgement remains unknown and automatically reconciles the sealed original in its window", async ({ page }, testInfo) => {
  const attempts: Array<{key: string; body: Json}> = [];
  let recover = false;
  await workspace(page, async (route, snapshot) => {
    attempts.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (!recover) return route.abort("connectionreset");
    const item = snapshot.human_collaboration.human_requests.items[0];
    item.status = "satisfied";
    item.responses = [{ response_ref: "original-receipt", ...attempts[0].body }];
    item.evaluation = { decision: "satisfied" };
    item.disposition = { decision: "satisfied" };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ response_ref: "original-receipt", status: "recorded" }) });
  });
  const submittedAt = Date.now();
  const dialog = await submit(page, "封存现场正文");
  await expect(dialog).toContainText("提交结果待确认");
  const pendingAt = Date.now();
  await expect(dialog).not.toContainText("回应没有记录");
  await expect(dialog.getByRole("textbox", { name: "自然语言回应", exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("unknown-submission.png") });
  recover = true;
  await expect(dialog).toContainText("回应已提交", { timeout: 15000 });
  const receiptAt = Date.now();
  await expect(dialog.getByRole("heading", { name: "核对现场原始记录" })).toBeVisible();
  expect(attempts.length).toBeGreaterThan(1);
  expect(attempts.every(attempt => JSON.stringify(attempt) === JSON.stringify(attempts[0]))).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("original-request-receipt.png") });
  const timingPath = testInfo.outputPath("delivery-observations.json");
  await writeFile(timingPath, JSON.stringify({ click_to_pending_ms: pendingAt - submittedAt, pending_to_receipt_ms: receiptAt - pendingAt, attempts: attempts.length, distinct_keys: new Set(attempts.map(a => a.key)).size }, null, 2));
  await testInfo.attach("delivery-observations", { path: timingPath, contentType: "application/json" });
});

test("new request counts notify without opening a window and explicit clicks select the request", async ({ page }, testInfo) => {
  await workspace(page, route => route.abort(), "/?workspace=1");
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
  const notification = page.getByRole("button", { name: "需要你", exact: true });
  await expect(notification).toHaveAttribute("title", "需要你 · 2 项待处理");
  await expect(dialog).toBeHidden();
  expect(new URL(page.url()).search).toBe("?workspace=1");
  await notification.click();
  await dialog.getByRole("button", { name: /线下操作 · 核对现场原始记录/ }).click();
  await expect(dialog.getByRole("heading", { name: "核对现场原始记录" })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await page.reload();
  await expect(notification).toHaveAttribute("title", "需要你 · 2 项待处理");
  await expect(dialog).toBeHidden();
  await page.screenshot({ path: testInfo.outputPath("request-count-without-interruption.png") });
});

test("a business rejection keeps editable input and a corrected submission uses a new identity", async ({ page }) => {
  const attempts: Array<{key: string; body: Json}> = [];
  await workspace(page, async route => {
    attempts.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (attempts.length === 1) return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: { code: "human_response_note_invalid" } }) });
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ response_ref: "corrected-receipt", status: "recorded" }) });
  });
  const dialog = await submit(page, "待修正的记录");
  await expect(dialog).toContainText("回应未提交");
  const note = dialog.getByRole("textbox", { name: "自然语言回应", exact: true });
  await expect(note).toBeEnabled();
  await expect(note).toHaveValue("待修正的记录");
  await submit(page, "已修正的记录");
  await expect(dialog).toContainText("回应已提交");
  expect(attempts).toHaveLength(2);
  expect(attempts[1].key).not.toBe(attempts[0].key);
  expect(attempts[1].body.note).toBe("已修正的记录");
});

test("a late original acknowledgement does not select over another request or clear its new draft", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const attempts: Array<{key: string; body: Json}> = [];
  await workspace(page, async route => {
    attempts.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (attempts.length === 1) return route.abort("connectionreset");
    await gate;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ response_ref: "late-original", status: "recorded" }) });
  });
  const dialog = await submit(page, "原请求的封存回应");
  await expect(dialog).toContainText("提交结果待确认");
  await dialog.getByRole("button", { name: /2\. 线上获取/ }).click();
  await expect(dialog.getByRole("heading", { name: "另一个请求仍待处理" })).toBeVisible();
  const note = dialog.getByRole("textbox", { name: "自然语言回应", exact: true });
  await note.fill("另一请求的新草稿，不可被迟到回执覆盖");
  await expect.poll(() => attempts.length).toBe(2);
  release();
  await expect(note).toHaveValue("另一请求的新草稿，不可被迟到回执覆盖");
  await expect(dialog.getByRole("heading", { name: "另一个请求仍待处理" })).toBeVisible();
  await dialog.getByRole("button", { name: /3\. 现实世界操作/ }).click();
  await expect(dialog).toContainText("回应已提交");
  expect(attempts[1]).toEqual(attempts[0]);
  await dialog.getByRole("button", { name: /2\. 线上获取/ }).click();
  await expect(note).toHaveValue("另一请求的新草稿，不可被迟到回执覆盖");
});

test("reconciliation continues after more than four failures without a new event", async ({ page }) => {
  await page.clock.install();
  const attempts: Array<{key: string; body: Json}> = [];
  await workspace(page, async route => {
    attempts.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (attempts.length <= 6) return route.abort("connectionreset");
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ response_ref: "recovered-without-events", status: "recorded" }) });
  });
  const dialog = await submit(page, "无新事件也继续核对原提交");
  await expect(dialog).toContainText("提交结果待确认");
  for (let count = 2; count <= 7; count += 1) {
    await page.clock.runFor(32_000);
    await expect.poll(() => attempts.length).toBeGreaterThanOrEqual(count);
  }
  await expect(dialog).toContainText("回应已提交");
  expect(attempts.every(attempt => JSON.stringify(attempt) === JSON.stringify(attempts[0]))).toBeTruthy();
});

test("handoff retries a transient read quietly without a new request revision", async ({ page }) => {
  let reads = 0;
  await workspace(page, route => route.abort(), undefined, async route => {
    if (++reads === 1) return route.fulfill({ status: 503, headers: { "Retry-After": "1" }, body: "busy" });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ request_ref: "research_graph:HR-273:r1", revision: 1, is_current: true,
      sections: [{ key: "work", title: "原请求的工作依据", paragraphs: ["已恢复读取，仍归属原请求。"] }] }) });
  });
  const handoff = page.getByRole("region", { name: "求助交接材料" });
  await expect(handoff.getByRole("status")).toContainText("自动重试");
  await expect(handoff.getByRole("alert")).toHaveCount(0);
  await expect(handoff).toContainText("已恢复读取，仍归属原请求。", { timeout: 6000 });
  expect(reads).toBe(2);
});

test("handoff Retry-After survives background returns while prior text, expansion and draft remain", async ({ page }) => {
  await page.clock.install();
  let reads = 0;
  const { snapshot } = await workspace(page, route => route.abort(), undefined, async route => {
    if (++reads === 2) return route.fulfill({ status: 503, headers: { "Retry-After": "30" }, body: "busy" });
    const label = reads === 1 ? "仍可阅读的原说明" : "已更新的当前说明";
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ request_ref: "research_graph:HR-273:r1", revision: 1, is_current: true,
      sections: [{ key: "work", title: "原请求的工作依据", paragraphs: Array.from({ length: 12 }, (_, i) => `${label} ${i}：原始材料和执行条件保持可见。`) }] }) });
  });
  const handoff = page.getByRole("region", { name: "求助交接材料" });
  await expect(handoff).toContainText("仍可阅读的原说明");
  const note = page.getByRole("textbox", { name: "自然语言回应", exact: true });
  await note.fill("读取恢复不覆盖我的未提交回应");
  const details = page.locator(".hc-request-details");
  await details.locator("summary").click();
  await page.locator(".hc-request-core").evaluate(el => { el.scrollTop = 200; });
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(handoff.getByRole("status")).toContainText("自动重试");
  await expect(handoff).toContainText("仍可阅读的原说明");
  snapshot.revision++;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.clock.runFor(29_000);
  expect(reads).toBe(2);
  await page.clock.runFor(1_100);
  await expect(handoff).toContainText("已更新的当前说明");
  expect(reads).toBe(3);
  await expect(note).toHaveValue("读取恢复不覆盖我的未提交回应");
  await expect(details).toHaveAttribute("open", "");
  expect(await page.locator(".hc-request-core").evaluate(el => el.scrollTop)).toBeGreaterThan(100);
  await expect(handoff.getByRole("alert")).toHaveCount(0);
});
