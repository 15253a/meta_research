import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { PublicSnapshot } from "../src/api";

// Route fixtures isolate the sidebar's public browser behavior. Endpoint contract
// acceptance remains covered separately through the real #233 HTTP service.
test("sidebar reviews understanding and scope before confirming the exact revised guidance", async ({ page }) => {
  const snapshot = JSON.parse(readFileSync(resolve("tests/snapshot-before.json"), "utf8")) as PublicSnapshot;
  const baseUrl = "http://localhost:19829";
  await page.context().addCookies([{ name: "meta_research_csrf", value: "guidance-fixture-session", url: baseUrl }]);
  await page.route(`${baseUrl}/**`, route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith("/api/")) return route.fulfill({ json: {} });
    const file = resolve("../src/meta_research/web_dist", pathname === "/" ? "index.html" : pathname.slice(1));
    return route.fulfill({ body: readFileSync(file), contentType: pathname.endsWith(".js") ? "text/javascript" : pathname.endsWith(".css") ? "text/css" : "text/html" });
  });
  const companion = snapshot.human_collaboration!.companion;
  Object.assign(companion, { status: "ready", scope_ref: "quest:quest_chrome_1", session_ref: "guidance-session", messages: [], agent_proposals: [], soft_constraints: [] });
  snapshot.human_collaboration!.human_requests.items = [];
  const body = {
    proposal_kind: "soft_constraint", text: "本次实验只改学习率，总目标不变。",
    assistant_understanding: "只改变这次实验的学习率。", applies_to: ["仅所选实验"],
    semantic_scope: { kind: "target", quest_ref: "quest_chrome_1", question_ref: "question_history", cycle_ref: "cycle_history", target_ref: "target_history" },
    strength: 5, preserve_conditions: ["总目标不变"], work_materials: null,
  };
  const proposal = { proposal_ref: "proposal-guidance", scope_ref: companion.scope_ref, proposal_hash: "a".repeat(64), status: "proposed", proposal: body };
  const writes: Array<{ path: string; body: Record<string, unknown>; key: string | undefined }> = [];
  await page.route("**/api/v1/events*", route => route.abort());
  await page.route("**/api/v1/snapshot", route => route.fulfill({ json: snapshot }));
  await page.route("**/api/v1/companion/messages", route => {
    writes.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
    companion.agent_proposals = [proposal]; snapshot.revision += 1;
    return route.fulfill({ json: { interaction_ref: "guidance-turn", status: "queued" } });
  });
  await page.route("**/agent-proposals/*/revisions", route => {
    const revised = route.request().postDataJSON();
    writes.push({ path: new URL(route.request().url()).pathname, body: revised, key: route.request().headers()["idempotency-key"] });
    return route.fulfill({ json: { ...proposal, proposal_ref: "proposal-revised", proposal_hash: "b".repeat(64), proposal: revised.proposal } });
  });
  await page.route("**/agent-proposals/*/soft-constraint", route => {
    writes.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] });
    if (writes.filter(write => write.path.endsWith("/soft-constraint")).length === 1) return route.abort("failed");
    return route.fulfill({ json: { proposal: { ...proposal, status: "converted" }, soft_constraint: { status: "active" } } });
  });
  await page.goto(`${baseUrl}/?workspace=1`);
  const sidebar = page.getByRole("complementary", { name: "研究助手" });
  await sidebar.getByText("指导力度与可选材料", { exact: true }).click();
  await expect(sidebar.getByLabel("指导力度", { exact: true })).toHaveValue("3");
  await expect(sidebar.getByRole("button", { name: "保存指导", exact: true })).toHaveCount(0);
  await sidebar.getByLabel("指导力度", { exact: true }).selectOption("5");
  await sidebar.getByLabel("给研究助手发消息").fill(body.text);
  await sidebar.getByRole("button", { name: "发送消息", exact: true }).click();
  const review = sidebar.getByRole("article", { name: "确认研究指导" });
  await expect(review).toBeVisible();
  expect(writes[0].body.guidance_options).toEqual({ strength: 5, work_materials: null });
  await expect(review.getByLabel("指导原文", { exact: true })).toHaveValue(body.text);
  await expect(review).toContainText("target_history");
  await expect(review).not.toContainText("Quest 全局目标更新待对齐");
  await review.getByRole("button", { name: "取消审阅", exact: true }).click();
  await expect(sidebar).toContainText("没有交给研究");
  expect(writes).toHaveLength(1);
  await sidebar.getByRole("button", { name: "继续审阅指导", exact: true }).click();
  await review.getByLabel("助手理解", { exact: true }).fill("只在所选实验中改变学习率，保留其余条件。");
  await expect(review.getByRole("button", { name: "明确确认并交给研究", exact: true })).toBeDisabled();
  await review.getByRole("button", { name: "保存修改并重新审阅", exact: true }).click();
  await expect(review.getByRole("button", { name: "明确确认并交给研究", exact: true })).toBeEnabled();
  await review.getByRole("button", { name: "明确确认并交给研究", exact: true }).click();
  await expect(review.getByRole("alert")).toContainText("重试仍使用同一请求身份");
  await review.getByRole("button", { name: "明确确认并交给研究", exact: true }).click();
  await expect(review).toContainText("已确认保存");
  expect(writes[1].body.expected_proposal_hash).toBe("a".repeat(64));
  expect(writes[1].body.proposal).toMatchObject({ assistant_understanding: "只在所选实验中改变学习率，保留其余条件。", semantic_scope: body.semantic_scope });
  expect(writes[2]).toMatchObject({ path: "/api/v1/human-collaboration/agent-proposals/proposal-revised/soft-constraint", body: { expected_scope_ref: companion.scope_ref, expected_proposal_hash: "b".repeat(64), strength: 5 } });
  expect(writes.every(write => Boolean(write.key))).toBe(true);
  expect(writes[3]).toEqual(writes[2]);
});

test("unsent sidebar drafts remain isolated when the projected Quest changes and returns", async ({ page }) => {
  await page.addInitScript(() => {
    const Native = window.EventSource;
    const streams: EventTarget[] = [];
    Object.assign(window, { guidanceEvents: streams });
    window.EventSource = new Proxy(Native, { construct() {
      const stream = Object.assign(new EventTarget(), { close() {} });
      streams.push(stream); return stream;
    } });
  });
  const snapshot = JSON.parse(readFileSync(resolve("tests/snapshot-before.json"), "utf8")) as PublicSnapshot;
  const companion = snapshot.human_collaboration!.companion;
  Object.assign(companion, { status: "ready", scope_ref: "quest:first", session_ref: "draft-session", messages: [], agent_proposals: [], soft_constraints: [] });
  snapshot.human_collaboration!.human_requests.items = [];
  const base = "http://localhost:19830";
  await page.context().addCookies([{ name: "meta_research_csrf", value: "draft-fixture", url: base }]);
  await page.route(`${base}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/snapshot") return route.fulfill({ json: snapshot });
    if (path.startsWith("/api/")) return route.fulfill({ json: {} });
    return route.fulfill({ body: readFileSync(resolve("../src/meta_research/web_dist", path === "/" ? "index.html" : path.slice(1))), contentType: path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.goto(`${base}/?workspace=1`);
  const sidebar = page.getByRole("complementary", { name: "研究助手" });
  const input = sidebar.getByLabel("给研究助手发消息");
  await input.fill("第一项研究未发送的原文");
  await sidebar.getByText("指导力度与可选材料", { exact: true }).click();
  await sidebar.getByLabel("指导力度", { exact: true }).selectOption("5");
  companion.scope_ref = "quest:second"; snapshot.revision += 1;
  await page.evaluate(() => {
    const streams = Reflect.get(window, "guidanceEvents") as EventTarget[];
    streams.at(-1)!.dispatchEvent(new MessageEvent("snapshot.required", { data: "{}" }));
  });
  await expect(input).toHaveValue("");
  await expect(sidebar.getByLabel("指导力度", { exact: true })).toHaveValue("3");
  await input.fill("第二项研究自己的草稿");
  companion.scope_ref = "quest:first"; snapshot.revision += 1;
  await page.evaluate(() => {
    const streams = Reflect.get(window, "guidanceEvents") as EventTarget[];
    streams.at(-1)!.dispatchEvent(new MessageEvent("snapshot.required", { data: "{}" }));
  });
  await expect(input).toHaveValue("第一项研究未发送的原文");
  await expect(sidebar.getByLabel("指导力度", { exact: true })).toHaveValue("5");
});
