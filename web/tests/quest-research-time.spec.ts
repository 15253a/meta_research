import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const quest = snapshot.research_space.current_quest.quest_ref;
  const request = { ...snapshot.human_collaboration.human_requests.items[0],
    request_ref: "agent_runtime:HR-time:r1", request_id: "HR-time", quest_ref: quest,
    kind: "research_time_extension", status: "open", obligation: "完成核对与交接还需要两小时",
    target_assertion: { condition: { time_extension: { budget_revision: "budget-1", requested_additional_seconds: 7200 } } },
    responses: [], evaluation: null, disposition: null, required_authorization: null, direct_waiters: [] };
  snapshot.human_collaboration.human_requests.items = [request];
  snapshot.human_collaboration.companion.messages = [];
  snapshot.human_collaboration.companion.agent_proposals = [];
  const state = { request, snapshot, writes: [] as Record<string, any>[], budget: {
    quest_ref: quest, budget_revision: "budget-1", budget_kind: "finite", approved_seconds: 14400,
    used_seconds: 10800, confirmed_used_seconds: 10800, remaining_seconds: 3600, overrun_seconds: 0,
    observed_at: 1794232800, accuracy: "exact", status: "running", unknown_intervals: [], shared: true,
  } };
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.context().addCookies([{ name: "meta_research_csrf", value: "time-test", url: "http://localhost:18773" }]);
  await page.route("**/*", async route => {
    const operation = route.request(), url = new URL(operation.url());
    if (url.origin !== "http://localhost:18773") return route.abort();
    const json = (body: unknown) => route.fulfill({ json: body });
    if (url.pathname.endsWith("/responses")) {
      state.writes.push(operation.postDataJSON());
      Object.assign(request, { status: "satisfied", responses: [{ response_ref: "response-time", ...state.writes.at(-1),
        time_budget_approval: { status: "applied", budget_revision: "budget-2", previous_approved_seconds: 14400, approved_seconds: 21600, applied_at: 1794232860 } }],
        evaluation: { decision: "satisfied" }, disposition: { decision: "satisfied" } });
      Object.assign(state.budget, { budget_revision: "budget-2", approved_seconds: 21600, used_seconds: 10860, remaining_seconds: 10740 });
      snapshot.revision += 1;
      return json({ response_ref: "response-time", status: "recorded" });
    }
    if (url.pathname.endsWith("/research-time")) return json(state.budget);
    if (operation.method() !== "GET") return route.abort();
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (url.pathname.includes("/handoff")) return json({ request_ref: request.request_ref, revision: 1, is_current: true, sections: [] });
    if (url.pathname.startsWith("/api/")) return json({ status: "ready", items: [], sessions: [], cycles: [], questions: [], total: 0 });
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://localhost:18773/?workspace=1&panel=research-time-extension");
  return state;
}

test("human reviews an exact extension and reads applied budget separately from research resumption", async ({ page }, testInfo) => {
  const state = await workspace(page);
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
  await expect(dialog).toContainText("剩余 1 小时");
  await expect(dialog).toContainText("等待回应期间继续计时");
  await dialog.getByLabel("增加研究时间（小时）").fill("2");
  await dialog.getByRole("button", { name: "同意增加时间", exact: true }).click();
  expect(state.writes).toHaveLength(0);
  const review = page.getByRole("dialog", { name: "审阅正式回应", exact: true });
  await expect(review).toContainText("增加 2 小时");
  await review.getByRole("button", { name: "确认提交回应", exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ decision: "provided", facts: { time_budget_decision: {
    decision: "approve", expected_revision: "budget-1", additional_seconds: 7200,
  } } });
  await expect(dialog).toContainText("预算已生效");
  await expect(dialog).toContainText("尚未确认研究已恢复");
  await page.screenshot({ path: testInfo.outputPath("budget-applied.png") });
});

test("human can set a new total and explicitly correct an uncertain remaining allowance", async ({ page }) => {
  const state = await workspace(page);
  state.budget.accuracy = "unknown";
  await page.reload();
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
  await expect(dialog).toContainText("剩余时间暂不能准确确定");
  await dialog.getByLabel("批准额度方式").selectOption("total");
  await dialog.getByLabel("新的总时长（小时）").fill("6");
  await dialog.getByText("缺少历史计时依据时，明确校正余额", { exact: true }).click();
  await dialog.getByLabel("同时确认缺证后的当前剩余额度").check();
  await dialog.getByLabel("确认当前剩余（小时）").fill("2.5");
  await expect(dialog.getByRole("button", { name: "同意新的总时长" })).toBeDisabled();
  await dialog.getByLabel("校正依据").fill("依据已核对的运行记录，确认从当前时点还可用2.5小时。");
  await dialog.getByRole("button", { name: "同意新的总时长" }).click();
  const review = page.getByRole("dialog", { name: "审阅正式回应" });
  await expect(review).toContainText("确认当前剩余 2.5 小时");
  await review.getByRole("button", { name: "确认提交回应" }).click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].facts.time_budget_decision).toEqual({ decision: "approve", expected_revision: "budget-1",
    total_seconds: 21600, confirmed_remaining_seconds: 9000, correction_reason: "依据已核对的运行记录，确认从当前时点还可用2.5小时。" });
});
