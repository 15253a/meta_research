import { expect, test, type Page } from "@playwright/test";
import { openTimeline, QUEST_REF } from "./research-timeline-fixture.js";

function revision(index: number, text = `第 ${index + 1} 个正式研究目标。`) {
  return { goal_revision_ref: `goal-version-${index + 1}`, ...(index ? { sequence: index } : {}),
    goal: { goal: text, completion_criteria: "保留准确的研究适用边界。" },
    conditions: null, conditions_review: null };
}

async function goalWorkspace(page: Page, count = 1) {
  const fixture = await openTimeline(page);
  const writes: string[] = [];
  page.on("request", request => {
    if (!["GET", "HEAD"].includes(request.method())) writes.push(new URL(request.url()).pathname);
  });
  const items = Array.from({ length: count }, (_, index) => revision(index));
  Object.assign(fixture.snapshot.research_space.current_quest, {
    goal_revision_ref: items.at(-1)!.goal_revision_ref, goal: items.at(-1)!.goal.goal,
    draft_revision: 99, origin: count === 1 ? "initialization" : "evolution",
    sequence: count === 1 ? undefined : count - 1,
    history: { items: items.slice(0, 20), offset: 0, next_offset: count > 20 ? 20 : null },
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  const section = page.getByRole("region", { name: "研究目标版本", exact: true });
  await expect(section).toBeVisible();
  return { ...fixture, section, writes, items };
}

test("workspace displays the accepted goal version, never its draft revision or current Question", async ({ page }) => {
  const { section, writes } = await goalWorkspace(page);
  await expect(section.locator("li > b")).toHaveText(["v1"]);
  await expect(section).toContainText("第 1 个正式研究目标。");
  await expect(section).not.toContainText("v99");
  await expect(section).not.toContainText("非完整 Stage 路径");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(section.getByTitle("第 1 个正式研究目标。", { exact: true })).toBeVisible();
    expect(await section.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  }
  expect(writes).toEqual([]);
});

test("goal history reads exact accepted versions on demand without renumbering or writes", async ({ page }) => {
  const { section, items, writes } = await goalWorkspace(page, 22);
  const reads: string[] = [];
  await page.route(`**/api/v1/quests/${QUEST_REF}/goal/history?*`, route => {
    reads.push(new URL(route.request().url()).search);
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: items.slice(20), offset: 20, next_offset: null }) });
  });
  await expect(section.locator(":scope > .quest-goal-lines li > b")).toHaveText(["v22"]);
  expect(reads).toEqual([]);
  await section.getByText("查看目标版本", { exact: true }).click();
  await section.getByRole("button", { name: "读取更多目标版本" }).click();
  await expect(section.locator("details li > b")).toHaveText(Array.from({ length: 21 }, (_, index) => `v${index + 1}`));
  await expect(section.getByRole("button", { name: "读取更多目标版本" })).toHaveCount(0);
  expect(reads).toEqual(["?offset=20&limit=20"]);
  expect(writes).toEqual([]);
});

test("a late goal-history page cannot leak across data instances with identical goal references", async ({ page }) => {
  test.setTimeout(40_000);
  const { section, snapshot, runtime, writes } = await goalWorkspace(page, 22);
  snapshot.revision_domain = "goal-data-a";
  await page.reload({ waitUntil: "domcontentloaded" });
  let release!: () => void;
  let started = false;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/v1/quests/${QUEST_REF}/goal/history?*`, async route => {
    started = true;
    await gate;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({
      items: [revision(20, "旧实例迟到的历史目标。")], offset: 20, next_offset: null,
    }) });
  });
  await section.getByText("查看目标版本", { exact: true }).click();
  await section.getByRole("button", { name: "读取更多目标版本" }).click();
  await expect.poll(() => started).toBe(true);
  const currentFull = section.locator(':scope > .quest-goal-lines .quest-goal-full');
  await section.locator('summary[aria-label="查看 v22 目标全文"]').press("Enter");
  await expect(currentFull).toBeVisible();
  snapshot.revision_domain = "goal-data-b";
  snapshot.revision += 1;
  Object.assign(snapshot.research_space.current_quest, {
    goal: "新实例的正式研究目标。", history: { items: [], offset: 0, next_offset: null },
  });
  runtime.status = { schema_ref: "meta-research/runtime-status/v1", revision: snapshot.revision,
    revision_domain: snapshot.revision_domain, observed_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    state: "waiting", current_task: null, waiting_reason: null, pending_requests: 0,
    foreground: snapshot.research_control.foreground, health: { status: "ready", checks: [] } };
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(section).toContainText("新实例的正式研究目标。", { timeout: 20_000 });
  await expect(currentFull).toBeHidden();
  const lateResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/goal/history"));
  release();
  await lateResponse;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve(null))));
  await expect(section).not.toContainText("旧实例迟到的历史目标。");
  expect(writes).toEqual([]);
});

test.describe("accessible goal wording", () => {
  test.use({ hasTouch: true });

  test("each goal can reveal complete wording and judgment using touch, keyboard and pointer", async ({ page }, testInfo) => {
    const { snapshot, section, items, writes } = await goalWorkspace(page, 2);
    const initial = "最初的研究目标：" + "逐项核对不同来源与条件下的证据，保留无法判断的部分。".repeat(8);
    const evolved = "调整后的研究目标：" + "依据新发现收紧研究边界，分别说明支持、反对和仍待验证的内容。".repeat(8);
    items[0].goal.goal = initial;
    Object.assign(items[0], { judgment: "首版保留不同来源的原始研究边界。" });
    Object.assign(snapshot.research_space.current_quest, { goal: evolved, judgment: "已有证据不足以支持原先较宽的范围，因此先收紧结论。" });
    await page.reload({ waitUntil: "domcontentloaded" });
    const first = section.locator('[data-goal-revision="goal-version-1"]');
    const current = section.locator('[data-goal-revision="goal-version-2"]');
    await expect(first.locator(".quest-goal-full")).toBeHidden();
    await expect(current.locator(".quest-goal-full")).toBeHidden();
    await page.setViewportSize({ width: 390, height: 844 });
    const firstSummary = first.locator("summary");
    expect((await firstSummary.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await firstSummary.tap();
    await expect(first.locator(".quest-goal-full > p")).toBeVisible();
    await expect(first.locator(".quest-goal-full > p")).toHaveText(initial);
    await expect(first.locator(".quest-goal-full")).toContainText("首版保留不同来源的原始研究边界。");
    expect(await section.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await section.screenshot({ path: testInfo.outputPath("goal-full-mobile.png") });
    await firstSummary.tap();
    await expect(first.locator(".quest-goal-full")).toBeHidden();
    await page.setViewportSize({ width: 1440, height: 900 });
    const currentSummary = current.locator("summary");
    await currentSummary.focus();
    await currentSummary.press("Enter");
    await expect(current.locator(".quest-goal-full > p")).toHaveText(evolved);
    await expect(current.locator(".quest-goal-full")).toContainText("已有证据不足以支持原先较宽的范围，因此先收紧结论。");
    await currentSummary.press("Space");
    await expect(current.locator(".quest-goal-full")).toBeHidden();
    await currentSummary.click();
    await expect(current.locator(".quest-goal-full > p")).toBeVisible();
    await section.screenshot({ path: testInfo.outputPath("goal-full-desktop.png") });
    expect(writes).toEqual([]);
  });
});
