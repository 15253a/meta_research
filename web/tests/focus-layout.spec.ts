import { expect, test, type Locator, type Page } from "@playwright/test";
import { resolve } from "node:path";
import type { RootSession, RootSessions } from "../src/rootSessionsApi.js";
import { CYCLE_REF, QUESTION_REF, QUEST_REF, SOURCE_TIME, openTimeline, timelineSession } from "./research-timeline-fixture.js";

const cycle = (timeline: Locator, ref = CYCLE_REF) => timeline.locator(`.research-timeline-cycle[data-cycle-ref="${ref}"]`);
const output = (page: Page) => page.locator("#research-output-panel");
const transcript = (page: Page, ref: string) => output(page).locator(`.root-session-conversation[data-session-ref="${ref}"]`);
const outputText = (ref: string) => `${ref} 的独立研究输出`;
const evidencePath = (name: string) => resolve(process.env.META_RESEARCH_LAYOUT_EVIDENCE ?? "test-results", name);
const observationResize = (page: Page) => page.getByRole("separator", { name: "调整时间线与输出宽度", exact: true });

function withOperation(session: RootSession): RootSession {
  return { ...session, operations: [{ operation_ref: `operation-${session.session_ref}`, label: session.title,
    phase: "work", status: session.status, created_at: SOURCE_TIME, updated_at: SOURCE_TIME }] };
}

async function openFocusWorkspace(page: Page, busyDetails = false) {
  const writes: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") writes.push(request.method() + " " + new URL(request.url()).pathname); });
  const fixture = await openTimeline(page);
  fixture.catalog.sessions.push(
    timelineSession("stage-current-plan", "stage", "当前轮 Plan", CYCLE_REF, "completed", {
      stage: "plan", related_stages: ["plan"], question_ref: QUESTION_REF,
    }),
    timelineSession("stage-history-idea", "stage", "历史轮 Idea", "cycle-r1", "completed", {
      stage: "idea", related_stages: ["idea"], question_ref: "question-r1",
    }),
  );
  fixture.catalog.sessions = fixture.catalog.sessions.map(withOperation);
  if (busyDetails) {
    fixture.catalog.limited = true;
    fixture.catalog.reasons = [{ code: "partial_session_index" }];
    fixture.snapshot.readiness.checks.push({ name: "reasoning_stage_worker", status: "unavailable", reason: { code: "codex_usage_limit" } });
    Object.assign(fixture.snapshot.research_control.foreground, { status: "paused", grant_status: "suspended" });
    const historySession = fixture.catalog.sessions.find(session => session.session_ref === "stage-history-idea")!;
    historySession.operations = Array.from({ length: 3 }, (_, index) => ({ ...historySession.operations[0],
      operation_ref: `operation-${historySession.session_ref}-${index + 1}`, label: `历史阶段长记录 · 第 ${index + 1} 次研究执行`,
      created_at: SOURCE_TIME + index, updated_at: SOURCE_TIME + index,
    }));
  }
  const historical: RootSessions = { schema_ref: "meta-research/root-sessions/v1", quest_ref: "quest-history",
    observed_at: SOURCE_TIME, sessions: [withOperation(timelineSession("other-quest-idea", "stage", "其他研究 Idea", "cycle-history", "completed", {
      stage: "idea", related_stages: ["idea"], question_ref: "question-history",
    }))], active_session_refs: [], limited: false, reasons: [] };
  fixture.history.quests.push({ quest_ref: historical.quest_ref, goal: "历史研究目标" });
  fixture.history.otherOverview = { schema_ref: "meta-research/research-overview/v1", status: "idle", quest_ref: historical.quest_ref,
    question_ref: null, cycle_ref: null, cycle_ordinal: null, foreground: null, findings: { quest: [], question: [], cycle: [] }, reason: null,
    cycles: [{ cycle_ref: "cycle-history", question_ref: "question-history", ordinal: 1, stages: {} }] };
  await page.route("**/api/v1/quests/quest-history/root-sessions", route => route.fulfill({ contentType: "application/json", body: JSON.stringify(historical) }));
  await page.route("**/root-sessions/*/output?*", route => {
    const url = new URL(route.request().url());
    const questRef = decodeURIComponent(url.pathname.split("/")[4]);
    const sessionRef = decodeURIComponent(url.pathname.split("/")[6]);
    const catalog = questRef === QUEST_REF ? fixture.catalog : historical;
    const session = catalog.sessions.find(item => item.session_ref === sessionRef);
    if (!session) return route.abort();
    const operationRef = url.searchParams.get("operation_ref");
    if (!session.operations.some(operation => operation.operation_ref === operationRef)) return route.abort();
    const text = JSON.stringify({ type: "item.completed", thread_id: `native-${sessionRef}`, item: {
      type: "agent_message", text: outputText(sessionRef) + "\n\n" + Array.from({ length: 45 }, (_, index) => `第 ${index + 1} 条研究记录：保留已核验的实验依据和阅读位置。`).join("\n\n"),
    } }) + "\n";
    const bytes = Buffer.from(text), offset = Number(url.searchParams.get("after") ?? 0);
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ schema_ref: "meta-research/root-session-output/v1",
      quest_ref: questRef, session_ref: sessionRef, operation_ref: operationRef, stream_ref: `stream-${sessionRef}-${operationRef}`,
      native_session_ref: `native-${sessionRef}`, text: bytes.subarray(offset).toString(), offset, next_offset: bytes.length,
      source_bytes: bytes.length, has_more: false, source_caught_up: true, source_updated_at: SOURCE_TIME, observed_at: SOURCE_TIME, status: "terminal",
    }) });
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(fixture.timeline).toBeVisible();
  return { ...fixture, writes };
}

async function expandCycle(timeline: Locator, ref: string) {
  for (const question of await timeline.locator(".research-timeline-question").all()) {
    const details = question.locator(":scope > details");
    if (await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
  }
  const row = cycle(timeline, ref);
  const details = row.locator(":scope > details");
  if (await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
  return row;
}

test("timeline and output share a desktop row, logs follow beneath, and mobile stays within the viewport", async ({ page }) => {
  const { timeline, errors, writes } = await openFocusWorkspace(page);
  await expect(transcript(page, "target-r3a")).toContainText(outputText("target-r3a"));
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1034, height: 813 }, { width: 900, height: 720 }, { width: 1440, height: 720 }]) {
    await page.setViewportSize(viewport);
    const [tree, conversation, logs] = await Promise.all([timeline.boundingBox(), output(page).boundingBox(),
      page.getByRole("region", { name: "实验日志区域" }).boundingBox()]);
    expect(Math.abs(tree!.y - conversation!.y)).toBeLessThan(2);
    expect(tree!.x + tree!.width).toBeLessThanOrEqual(conversation!.x);
    const grid = (await page.locator(".research-observation-grid").boundingBox())!;
    expect(Math.abs(tree!.x - grid.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(conversation!.x + conversation!.width - grid.x - grid.width)).toBeLessThanOrEqual(1);
    expect(logs!.y).toBeGreaterThanOrEqual(Math.max(tree!.y + tree!.height, conversation!.y + conversation!.height));
    const [leftTitle, rightTitle] = await Promise.all([timeline.getByRole("heading", { level: 2 }).boundingBox(), output(page).getByRole("heading", { name: "研究过程", exact: true }).boundingBox()]);
    expect.soft(Math.abs(leftTitle!.y - rightTitle!.y), `${viewport.width}px title text aligns`).toBeLessThanOrEqual(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    await timeline.scrollIntoViewIfNeeded();
    await page.screenshot({ path: evidencePath(`focus-layout-${viewport.width}x${viewport.height}.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(timeline).toBeVisible();
  await expect(output(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  const narrowTree = (await timeline.boundingBox())!;
  expect((await output(page).boundingBox())!.y).toBeGreaterThanOrEqual(narrowTree.y + narrowTree.height);
  await timeline.scrollIntoViewIfNeeded();
  await page.screenshot({ path: evidencePath("focus-layout-390.png") });
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("each observation panel can be hidden and restored without losing timeline expansion, selection, or reading position", async ({ page }) => {
  const { timeline, errors } = await openFocusWorkspace(page, true);
  await page.setViewportSize({ width: 1440, height: 720 });
  const historical = await expandCycle(timeline, "cycle-r1");
  await historical.getByRole("button", { name: "查看 Idea 输出", exact: true }).click();
  const selected = transcript(page, "stage-history-idea");
  await expect(selected).toContainText(outputText("stage-history-idea"));
  for (const notice of [output(page).getByTestId("research-current-worker-blocker"),
    output(page).locator(".root-session-controls > .root-session-warning")]) {
    await notice.scrollIntoViewIfNeeded();
    await expect(notice).toBeInViewport();
  }
  await expect(selected.locator(".root-operation")).toHaveCount(3);
  const scroll = selected.locator(".root-session-scroll");
  const treeScroll = timeline.locator(".research-timeline-scroll");
  await page.screenshot({ path: evidencePath("focus-layout-busy-before-details.png") });
  for (const area of [treeScroll, scroll]) {
    await area.evaluate(node => { node.scrollTop = node.scrollHeight; });
    const metrics = await area.evaluate(node => ({ top: node.scrollTop, height: node.clientHeight, content: node.scrollHeight,
      overflow: getComputedStyle(node).overflowY }));
    expect(metrics.height).toBeGreaterThan(100);
    expect(metrics.content).toBeGreaterThan(metrics.height);
    expect(["auto", "scroll"]).toContain(metrics.overflow);
    expect(Math.abs(metrics.top + metrics.height - metrics.content)).toBeLessThanOrEqual(1);
  }
  await expect(scroll.getByText("第 45 条研究记录：保留已核验的实验依据和阅读位置。", { exact: true }).last()).toBeInViewport();
  const details = selected.locator(".root-session-details");
  await details.locator(":scope > summary").click();
  await expect(details).toHaveAttribute("open", "");
  for (const width of [1440, 900]) {
    await page.setViewportSize({ width, height: 720 });
    await output(page).scrollIntoViewIfNeeded();
    const panel = (await output(page).boundingBox())!;
    for (const area of [details, selected.locator(":scope > .research-output-footer")]) {
      const box = (await area.boundingBox())!;
      expect(box.y).toBeGreaterThanOrEqual(panel.y);
      expect(box.y + box.height, `${width}px ${await area.getAttribute("class")} stays within output panel`).toBeLessThanOrEqual(panel.y + panel.height);
      expect(box.x + box.width).toBeLessThanOrEqual(panel.x + panel.width);
    }
    expect(await scroll.evaluate(node => node.clientHeight)).toBeGreaterThan(0);
    await scroll.evaluate(node => { node.scrollTop = node.scrollHeight; });
    await page.screenshot({ path: evidencePath(`focus-layout-reading-open-${width}x720.png`) });
    await expect(scroll.getByText("第 45 条研究记录：保留已核验的实验依据和阅读位置。", { exact: true }).last()).toBeInViewport();
    await details.locator(":scope > summary").scrollIntoViewIfNeeded();
    await expect(details.locator(":scope > summary")).toBeInViewport();
    await selected.locator(":scope > .research-output-footer button").scrollIntoViewIfNeeded();
    await expect(selected.locator(":scope > .research-output-footer button")).toBeInViewport();
    const lastExecution = details.locator(".root-operation-details").last();
    await expect(details.locator(".root-operation-details")).toHaveCount(3);
    await lastExecution.getByRole("button", { name: "最早记录", exact: true }).scrollIntoViewIfNeeded();
    await expect(lastExecution.getByRole("button", { name: "最早记录", exact: true })).toBeInViewport();
    await expect(details.locator(":scope > summary")).toBeInViewport();
    await expect(selected.locator(":scope > .research-output-footer button")).toBeInViewport();
    const detailsBox = (await details.boundingBox())!, contentBox = (await details.locator(":scope > div").boundingBox())!;
    const lastControl = (await lastExecution.getByRole("button", { name: "最早记录", exact: true }).boundingBox())!;
    const footerBox = (await selected.locator(":scope > .research-output-footer").boundingBox())!;
    expect(contentBox.y + contentBox.height).toBeLessThanOrEqual(detailsBox.y + detailsBox.height);
    expect(lastControl.y + lastControl.height).toBeLessThanOrEqual(detailsBox.y + detailsBox.height);
    expect(detailsBox.y + detailsBox.height).toBeLessThanOrEqual(footerBox.y);
    await page.screenshot({ path: evidencePath(`focus-layout-reading-details-${width}x720.png`) });
  }
  await details.locator(":scope > summary").click();
  await page.setViewportSize({ width: 1440, height: 720 });
  await scroll.evaluate(node => { node.scrollTop = 120; });
  const scrollTop = await scroll.evaluate(node => node.scrollTop);
  expect(scrollTop).toBeGreaterThan(0);
  const initialWidth = (await output(page).boundingBox())!.width;
  await expect(observationResize(page)).toBeVisible();
  await timeline.getByRole("button", { name: "隐藏研究时间线", exact: true }).click();
  await expect(timeline).toBeHidden();
  await expect(observationResize(page)).toBeHidden();
  expect((await output(page).boundingBox())!.width).toBeGreaterThan(initialWidth);
  await page.getByRole("button", { name: "隐藏研究过程", exact: true }).click();
  await expect(output(page)).toBeHidden();
  await expect(page.getByRole("region", { name: "实验日志区域" })).toBeVisible();
  await page.getByRole("button", { name: "展开研究时间线" }).click();
  await expect(observationResize(page)).toBeHidden();
  await expect(historical.locator(":scope > details")).toHaveAttribute("open", "");
  expect((await timeline.boundingBox())!.width).toBeGreaterThan(initialWidth);
  await page.getByRole("button", { name: "展开研究过程" }).click();
  await expect(observationResize(page)).toBeVisible();
  expect(Math.abs((await output(page).boundingBox())!.width - initialWidth)).toBeLessThanOrEqual(2);
  await expect(selected).toBeVisible();
  await expect(output(page).getByLabel("选择查看的会话")).toHaveValue("stage-history-idea");
  expect(await scroll.evaluate(node => node.scrollTop)).toBe(scrollTop);
  expect(errors).toEqual([]);
});

test("timeline and output resize together, retain the chosen split, and fit narrow windows", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { timeline, errors, writes } = await openFocusWorkspace(page);
  const handle = observationResize(page);
  const timelineWidth = async () => (await timeline.boundingBox())!.width;
  await expect(handle).toBeVisible();
  await expect(handle).toHaveAttribute("aria-orientation", "vertical");
  const initialLeft = await timelineWidth(), initialRight = (await output(page).boundingBox())!.width;
  await handle.scrollIntoViewIfNeeded();
  const grip = (await handle.boundingBox())!;
  await page.mouse.move(grip.x + grip.width / 2, grip.y + Math.min(grip.height / 2, 120));
  await page.mouse.down();
  await page.mouse.move(grip.x + grip.width / 2 + 120, grip.y + Math.min(grip.height / 2, 120), { steps: 8 });
  await page.mouse.up();
  const draggedLeft = await timelineWidth(), draggedRight = (await output(page).boundingBox())!.width;
  expect(draggedLeft).toBeGreaterThan(initialLeft + 80);
  expect(draggedRight).toBeLessThan(initialRight - 80);
  expect(Math.abs(draggedLeft + draggedRight - initialLeft - initialRight)).toBeLessThanOrEqual(2);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(handle).toBeVisible();
  await expect.poll(timelineWidth).toBeCloseTo(draggedLeft, 0);
  await handle.press("Home");
  const minimum = Number(await handle.getAttribute("aria-valuemin"));
  expect(Math.abs(await timelineWidth() - minimum)).toBeLessThanOrEqual(2);
  await handle.press("ArrowRight");
  expect(await timelineWidth()).toBeGreaterThan(minimum + 10);
  await handle.press("ArrowLeft");
  expect(Math.abs(await timelineWidth() - minimum)).toBeLessThanOrEqual(2);
  await handle.press("End");
  const maximum = Number(await handle.getAttribute("aria-valuemax"));
  expect(Math.abs(await timelineWidth() - maximum)).toBeLessThanOrEqual(2);
  const preferredWideWidth = await timelineWidth();
  await page.setViewportSize({ width: 900, height: 720 });
  await expect(handle).toBeVisible();
  expect(await timelineWidth()).toBeLessThan(preferredWideWidth);
  expect(await timelineWidth()).toBeLessThanOrEqual(Number(await handle.getAttribute("aria-valuemax")) + 2);
  expect((await output(page).boundingBox())!.width).toBeGreaterThan(150);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(900);
  for (const width of [880, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(handle).toBeHidden();
    const tree = (await timeline.boundingBox())!, conversation = (await output(page).boundingBox())!;
    expect(conversation.y).toBeGreaterThanOrEqual(tree.y + tree.height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(handle).toBeVisible();
  await expect.poll(timelineWidth).toBeCloseTo(preferredWideWidth, 0);
  await handle.dblclick();
  await expect.poll(timelineWidth).toBeCloseTo(initialLeft, 0);
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("timeline summaries remain complete at different widths and result details stay available beside output navigation", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { timeline, errors, writes } = await openFocusWorkspace(page);
  await observationResize(page).press("End");
  const historical = await expandCycle(timeline, "cycle-r1");
  const idea = historical.getByRole("button", { name: "查看 Idea 输出", exact: true });
  const summaries = [
    historical.locator(":scope > details > summary .timeline-summary-text"),
    idea.locator(".timeline-summary-text"),
  ];
  const originalText = await Promise.all(summaries.map(summary => summary.innerText()));
  for (const width of [1440, 900, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await historical.scrollIntoViewIfNeeded();
    for (const [index, summary] of summaries.entries()) {
      await expect(summary).toBeVisible();
      await expect(summary).toHaveText(originalText[index]);
      expect(await summary.getAttribute("title")).toContain(originalText[index]);
      expect((await summary.boundingBox())!.width, `${width}px summary remains wide enough to read`).toBeGreaterThan(60);
    }
    expect(await timeline.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await timeline.screenshot({ path: evidencePath(`focus-layout-compact-timeline-${width}.png`) });
  }
  for (const width of [1440, 900]) {
    await page.setViewportSize({ width, height: 900 });
    await observationResize(page).press("Home");
    for (const summary of summaries) {
      expect((await summary.boundingBox())!.width, `${width}px minimum split preserves readable summary width`).toBeGreaterThan(60);
    }
    expect(await timeline.evaluate(node => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    await timeline.screenshot({ path: evidencePath(`focus-layout-compact-timeline-minimum-${width}.png`) });
  }
  await idea.click();
  await expect(transcript(page, "stage-history-idea")).toContainText(outputText("stage-history-idea"));
  await historical.locator('[data-stage="reasoning"] > .research-timeline-stage-result').click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("the assistant minus frees the sidebar width and preserves an unsent draft when restored", async ({ page }) => {
  const { errors, writes } = await openFocusWorkspace(page);
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  const input = assistant.getByLabel("给研究助手发消息");
  await input.fill("暂不发送：优先观察当前实验。");
  const main = page.locator("#main-content");
  const before = (await main.boundingBox())!.width;
  await page.getByRole("button", { name: "隐藏研究助手", exact: true }).click();
  await expect(assistant).toBeHidden();
  expect((await main.boundingBox())!.width).toBeGreaterThan(before + 100);
  await page.locator("#research-timeline-panel").scrollIntoViewIfNeeded();
  const restore = page.getByRole("banner").getByRole("button", { name: "展开研究助手" });
  await expect(restore).toBeVisible();
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1034, height: 813 }, { width: 900, height: 720 }]) {
    await page.setViewportSize(viewport);
    const buttonBox = (await restore.boundingBox())!, headerBox = (await page.getByRole("banner").boundingBox())!;
    expect(buttonBox.x + buttonBox.width).toBeLessThanOrEqual(headerBox.x + headerBox.width);
    expect(buttonBox.y + buttonBox.height).toBeLessThanOrEqual(headerBox.y + headerBox.height);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    await page.locator("#research-timeline-panel").scrollIntoViewIfNeeded();
    await page.screenshot({ path: evidencePath(`focus-layout-assistant-hidden-${viewport.width}.png`) });
  }
  await restore.click();
  await expect(input).toHaveValue("暂不发送：优先观察当前实验。");
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("assistant dragging reallocates workspace width and remembers the choice across reload and hiding", async ({ page }) => {
  const { errors, writes } = await openFocusWorkspace(page);
  const assistant = page.getByRole("complementary", { name: "研究助手", exact: true });
  const handle = page.getByRole("separator", { name: "调整研究助手宽度", exact: true });
  const width = async () => (await assistant.boundingBox())!.width;
  const main = page.locator("#main-content");
  await expect(handle).toBeVisible();
  const initialWidth = await width(), initialMain = (await main.boundingBox())!.width;
  const grip = (await handle.boundingBox())!;
  const x = grip.x + grip.width / 2, y = grip.y + Math.min(grip.height / 2, 120);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 96, y, { steps: 8 });
  await page.mouse.up();
  const draggedWidth = await width();
  expect(draggedWidth).toBeGreaterThan(initialWidth + 70);
  expect((await main.boundingBox())!.width).toBeLessThan(initialMain - 70);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(handle).toBeVisible();
  await expect.poll(width).toBeCloseTo(draggedWidth, 0);
  await page.getByRole("button", { name: "隐藏研究助手", exact: true }).click();
  await expect(handle).toBeHidden();
  await page.getByRole("banner").getByRole("button", { name: "展开研究助手" }).click();
  await expect.poll(width).toBeCloseTo(draggedWidth, 0);
  await handle.press("Home");
  const minimum = Number(await handle.getAttribute("aria-valuemin"));
  await expect.poll(width).toBeCloseTo(minimum, 0);
  await handle.press("ArrowLeft");
  expect(await width()).toBeGreaterThan(minimum + 10);
  await handle.press("End");
  await expect.poll(width).toBeCloseTo(Number(await handle.getAttribute("aria-valuemax")), 0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("current and historical progress select their matching inline stage or Target output", async ({ page }) => {
  const { timeline, errors, writes, snapshot } = await openFocusWorkspace(page);
  snapshot.research_control.foreground.stage = "bundle";
  await page.reload({ waitUntil: "domcontentloaded" });
  const currentTargetFacts = page.getByRole("region", { name: "当前研究工作状态", exact: true });
  await expect(currentTargetFacts).toBeVisible();
  await cycle(timeline).getByRole("button", { name: "查看 Plan 输出", exact: true }).click();
  await expect(transcript(page, "stage-current-plan")).toContainText(outputText("stage-current-plan"));
  await page.getByRole("button", { name: "隐藏研究过程", exact: true }).click();
  await cycle(timeline).locator('[data-target-ref="target-143-a"] .research-timeline-target-open').click();
  await expect(transcript(page, "target-r3a")).toContainText(outputText("target-r3a"));
  await expect(currentTargetFacts).toBeVisible();
  const historical = await expandCycle(timeline, "cycle-r1");
  await historical.getByRole("button", { name: "查看 Idea 输出", exact: true }).click();
  await expect(transcript(page, "stage-history-idea")).toContainText(outputText("stage-history-idea"));
  await historical.locator(".research-timeline-target-open").click();
  await expect(transcript(page, "target-r1")).toContainText(outputText("target-r1"));
  await expect(currentTargetFacts).toBeHidden();
  await output(page).getByRole("button", { name: "返回当前执行", exact: true }).click();
  await expect(transcript(page, "target-r3a")).toContainText(outputText("target-r3a"));
  await expect(currentTargetFacts).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("other Quests use their own output and stages with no session never fall back to current execution", async ({ page }) => {
  const { timeline, errors, snapshot, writes } = await openFocusWorkspace(page);
  const foreground = JSON.stringify(snapshot.research_control.foreground);
  await timeline.getByLabel("浏览研究").selectOption("quest-history");
  await expect(timeline.locator('[data-question-ref="question-history"]')).toBeVisible();
  const historical = await expandCycle(timeline, "cycle-history");
  await historical.getByRole("button", { name: "查看 Idea 输出", exact: true }).click();
  await expect(transcript(page, "other-quest-idea")).toContainText(outputText("other-quest-idea"));
  await historical.getByRole("button", { name: "查看 Plan 输出", exact: true }).click();
  await expect(output(page)).toContainText("所选轮次的此阶段尚无会话。");
  await expect(output(page).locator(".root-session-conversation")).toHaveCount(0);
  await output(page).getByRole("button", { name: "返回当前执行", exact: true }).click();
  await expect(transcript(page, "target-r3a")).toContainText(outputText("target-r3a"));
  expect(JSON.stringify(snapshot.research_control.foreground)).toBe(foreground);
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});
