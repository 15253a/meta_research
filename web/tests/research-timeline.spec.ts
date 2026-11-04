import { expect, test, type Locator, type Page } from "@playwright/test";
import type { ResearchOverviewData } from "../src/ResearchOverview.js";
import type { TimelineSummaries, TimelineSummaryNode } from "../src/timelineSummaries.js";
import { openTimeline, QUEST_REF, CYCLE_REF, QUESTION_REF, SOURCE_TIME, CURRENT_QUESTION_SUMMARY, CURRENT_PLAN_SUMMARY, CURRENT_BUNDLE_SUMMARY, CURRENT_TARGET_SUMMARY, timelineSession, recorderNode } from "./research-timeline-fixture.js";

type JsonRecord = Record<string, any>;
test.describe.configure({ timeout: 120_000 });
const cycleNode = (timeline: Locator, cycleRef = CYCLE_REF) => timeline.locator(`.research-timeline-cycle[data-cycle-ref="${cycleRef}"]`);
const stageSummary = (timeline: Locator, stage: string, cycleRef = CYCLE_REF) => cycleNode(timeline, cycleRef).locator(`.research-timeline-stage[data-stage="${stage}"] .research-timeline-stage-summary .timeline-summary-text`);
const summaryNode = (timeline: Locator, key: string) => timeline.locator(`.timeline-summary[data-summary-key="${key}"]`);

test("timeline browsing includes formal Questions without Cycles and other Quests without changing foreground", async ({ page }) => {
  const { timeline, snapshot, history } = await openTimeline(page);
  const foreground = JSON.stringify(snapshot.research_control.foreground);
  history.quests.push({ quest_ref: "quest-history", goal: "历史研究目标" });
  history.questions.push({ question_ref: "question-without-cycle", name: "尚未推进的正式问题", status: "retired" });
  history.otherOverview = { schema_ref: "meta-research/research-overview/v1", status: "idle", quest_ref: "quest-history",
    question_ref: null, cycle_ref: null, cycle_ordinal: null, foreground: null, findings: { quest: [], question: [], cycle: [] }, reason: null,
    cycles: [{ cycle_ref: "cycle-history", question_ref: "question-history", ordinal: 1, stages: {} }] };
  await page.reload({ waitUntil: "domcontentloaded" });
  const noCycle = timeline.locator('[data-question-ref="question-without-cycle"]');
  await expect(noCycle).toContainText("尚未推进的正式问题");
  await expect(noCycle).toContainText("已退役");
  await timeline.getByLabel("浏览研究").selectOption("quest-history");
  await expect(timeline.locator('[data-cycle-ref="cycle-history"]')).toBeVisible();
  await expect(timeline.locator('[data-cycle-ref="cycle-history"]')).toHaveAttribute("data-current", "false");
  expect(JSON.stringify(snapshot.research_control.foreground)).toBe(foreground);
  await timeline.getByRole("button", { name: "返回当前工作", exact: true }).click();
  await expect(timeline.getByLabel("浏览研究")).toHaveValue(QUEST_REF);
  await expect(cycleNode(timeline)).toBeVisible();
});

test("timeline discovery reaches Quest and formal Question pages beyond one hundred with honest retry feedback", async ({ page }) => {
  const { timeline, history } = await openTimeline(page);
  history.pageSize = 25;
  for (let index = 0; index < 105; index += 1) {
    history.quests.push({ quest_ref: `quest-page-${index}`, goal: `历史研究 ${index}` });
    history.questions.push({ question_ref: `question-page-${index}`, name: `未进入轮次的问题 ${index}`, status: index === 104 ? "pruned" : "active" });
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(timeline.getByLabel("浏览研究").locator("option")).toHaveCount(106);
  const last = timeline.locator('[data-question-ref="question-page-104"]');
  await expect(last).toContainText("未进入轮次的问题 104");
  await expect(last).toContainText("pruned");
  await page.route("**/api/v1/research-timeline/quests?*", route => route.fulfill({ status: 503, contentType: "application/json", body: "{}" }));
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(timeline).toContainText("Quest 列表读取失败");
  await expect(last).toBeVisible();
  await page.unroute("**/api/v1/research-timeline/quests?*");
  await timeline.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(timeline.getByLabel("浏览研究").locator("option")).toHaveCount(106);
  await expect(timeline).not.toContainText("Quest 列表读取失败");
});

test("timeline keeps the reader position through a new Cycle and provides keyboard scrolling and explicit return", async ({ page }) => {
  const { timeline, snapshot, overview } = await openTimeline(page);
  for (let ordinal = 4; ordinal <= 38; ordinal += 1) overview.cycles.push({ cycle_ref: `cycle-scroll-${ordinal}`, question_ref: QUESTION_REF, ordinal, stages: {} });
  await page.reload({ waitUntil: "domcontentloaded" });
  const scroll = timeline.getByLabel("按时间排列的研究记录");
  await expect(timeline.locator('[data-cycle-ref="cycle-scroll-38"]')).toBeVisible();
  for (const viewport of [{ width: 1440, height: 420 }, { width: 390, height: 520 }]) {
    await page.setViewportSize(viewport);
    await scroll.focus();
    await scroll.press("End");
    await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBeGreaterThan(100);
    await scroll.press("Home");
    await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBe(0);
    const metrics = await scroll.evaluate(node => ({ height: node.clientHeight, content: node.scrollHeight,
      overflow: getComputedStyle(node).overflowY, scrollbar: getComputedStyle(node).scrollbarColor }));
    expect(metrics.content).toBeGreaterThan(metrics.height);
    expect(["auto", "scroll"]).toContain(metrics.overflow);
    expect(metrics.scrollbar).not.toBe("auto");
  }
  await scroll.evaluate(node => { node.scrollTop = 17; });
  Object.assign(snapshot.research_control.foreground, { cycle_ref: "cycle-scroll-39" });
  Object.assign(overview, { cycle_ref: "cycle-scroll-39", cycle_ordinal: 39 });
  overview.cycles.push({ cycle_ref: "cycle-scroll-39", question_ref: QUESTION_REF, ordinal: 39, stages: {} });
  await advanceSnapshot(page, snapshot);
  await expect(timeline.locator('[data-cycle-ref="cycle-scroll-39"]')).toHaveAttribute("data-current", "true");
  expect(await scroll.evaluate(node => node.scrollTop)).toBe(17);
  await timeline.getByRole("button", { name: "返回当前工作", exact: true }).click();
  await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBeGreaterThan(100);
  await expect(timeline.locator('[data-cycle-ref="cycle-scroll-39"] > details')).toHaveAttribute("open", "");
});

function updateSummary(recorder: { data: TimelineSummaries }, key: string, patch: Partial<TimelineSummaryNode>) {
  const node = recorder.data.nodes.find(item => item.node_key === key);
  if (!node) throw new Error(`Missing summary fixture: ${key}`);
  Object.assign(node, patch);
  recorder.data.revision += 1;
}

async function advanceSnapshot(page: Page, snapshot: JsonRecord) {
  snapshot.revision += 1;
  await expect(page.locator(".lumen-connection code")).toHaveText(`状态 ${snapshot.revision}`);
}

async function expandHistory(timeline: Locator) {
  for (const question of await timeline.locator(".research-timeline-question").all()) {
    const details = question.locator(":scope > details");
    if (await details.count() && await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
    await expect(question.locator(":scope > details > .research-timeline-cycles")).toBeVisible();
  }
  for (const cycle of await timeline.locator(".research-timeline-cycle").all()) {
    const details = cycle.locator(":scope > details");
    if (await details.count() && await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
    await expect(cycle.locator(":scope > details > .research-timeline-stages")).toBeVisible();
  }
}

test("the independent tree displays saved recorder sentences verbatim and keeps original results available", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page, { runningBundle: true });
  await expect(timeline).toBeVisible();
  await expect(page.locator(".spectrum-stage-summary")).toHaveCount(0);
  await expect(page.locator(".research-current-heading .research-timeline")).toHaveCount(0);
  const questions = timeline.locator(".research-timeline-questions > .research-timeline-question");
  await expect(questions).toHaveCount(3);
  await expect(summaryNode(timeline, `question:${QUESTION_REF}`).locator(".timeline-summary-text")).toHaveText(CURRENT_QUESTION_SUMMARY);
  const current = cycleNode(timeline);
  await expect(current).toHaveAttribute("data-current", "true");
  await expect(current.locator(":scope > details")).toHaveAttribute("open", "");
  await expect(current.locator(".research-timeline-stage-summary")).toHaveCount(4);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(stageSummary(timeline, "plan")).toHaveAttribute("title", /研究记录与已接纳结果/);
  const target = current.locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await expect(target.locator(".research-timeline-target-state")).toContainText("正在执行");
  await expect(stageSummary(timeline, "bundle")).not.toContainText(/阶段结果尚未接纳|正在开展「/);

  await current.locator('.research-timeline-stage[data-stage="idea"] .research-timeline-stage-result').click();
  const ideaDialog = page.getByRole("dialog");
  await expect(ideaDialog).toContainText("延续第二轮方向，补足样本规模并控制基准异质性");
  await expect(ideaDialog).toContainText("直接回应上一轮证据不足的问题。");
  await ideaDialog.getByLabel("关闭研究思路").click();
  await current.locator('.research-timeline-stage[data-stage="plan"] .research-timeline-stage-result').click();
  const planDialog = page.getByRole("dialog");
  await expect(planDialog).toContainText("异质基准上的增益是否可复现？");
  await expect(planDialog).toContainText("约束形式的敏感性分析");
  await planDialog.getByLabel("关闭验证计划").click();
  expect(errors).toEqual([]);
});

test("saved summary time separates an old Target sentence from its completed live state", async ({ page }) => {
  const { timeline, recorder, catalog, snapshot, errors } = await openTimeline(page);
  const key = `target:${CYCLE_REF}:target-143-a`;
  const oldSentence = "本项目尚无独立资格判断结果。";
  updateSummary(recorder, key, { summary: oldSentence, updated_at: SOURCE_TIME });
  snapshot.bundle_stage.target_graph.targets[0].status = "completed";
  Object.assign(catalog.sessions.find(session => session.target_ref === "target-143-a")!, {
    status: "completed", is_executing: false,
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await timeline.scrollIntoViewIfNeeded();
  const target = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".research-timeline-target-state")).toHaveText("已完成");
  await expect(target.locator(".timeline-summary-text")).toHaveText(oldSentence);
  await expect(target.locator(".timeline-summary-saved-at")).toHaveText(/^总结保存于 2026\/09\/09 \d{2}:\d{2}$/);
  await expect(summaryNode(timeline, key)).toHaveAttribute("data-summary-stale", "false");
  expect(errors).toEqual([]);
});

test("saved summary time separates old Reasoning prose and preserves missing-time pending and failed notices", async ({ page }) => {
  const { timeline, recorder, errors } = await openTimeline(page);
  const key = `stage:${CYCLE_REF}:reasoning`;
  const oldSentence = "本轮尚无可供综合核对的研究结果或实验依据。";
  updateSummary(recorder, key, { summary: oldSentence, updated_at: SOURCE_TIME });
  await page.reload({ waitUntil: "domcontentloaded" });
  await timeline.scrollIntoViewIfNeeded();
  const reasoning = summaryNode(timeline, key);
  await expect(reasoning.locator(".timeline-summary-text")).toHaveText(oldSentence);
  await expect(reasoning.locator(".timeline-summary-saved-at")).toHaveText(/^总结保存于 2026\/09\/09 \d{2}:\d{2}$/);
  await expect(cycleNode(timeline).locator('[data-stage="reasoning"] .research-timeline-stage-state')).toContainText("当前阶段");
  updateSummary(recorder, key, { updated_at: null, status: "pending" });
  await expect(reasoning.locator(".timeline-summary-saved-at")).toHaveText("既有总结（保存时间未知）");
  await expect(reasoning.locator(".timeline-summary-status")).toHaveText("总结待更新");
  updateSummary(recorder, key, { status: "failed" });
  await expect(reasoning.locator(".timeline-summary-status")).toHaveText("更新暂不可用");
  await expect(reasoning.locator(".timeline-summary-saved-at")).toHaveText("既有总结（保存时间未知）");
  await expect(reasoning.locator(".timeline-summary-text")).toHaveText(oldSentence);
  expect(errors).toEqual([]);
});

test("missing recorder sentences never fall back to clipped source text or assembled status strings", async ({ page }) => {
  const { timeline, recorder, errors } = await openTimeline(page, { runningBundle: true, summaryMode: "pending" });
  const plan = summaryNode(timeline, `stage:${CYCLE_REF}:plan`);
  await expect(plan.locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  for (const summary of await timeline.locator(".timeline-summary-text").all()) await expect(summary).toHaveText("记录员正在整理…");
  await expect(timeline).not.toContainText(/研究建议：|首个实验目标：|本轮重点：|阶段结果尚未接纳|本轮先完成扩展评测/);
  updateSummary(recorder, `stage:${CYCLE_REF}:plan`, { status: "failed" });
  await expect(plan.locator(".timeline-summary-text")).toHaveText("总结暂未生成");
  updateSummary(recorder, `stage:${CYCLE_REF}:plan`, { status: "ready", summary: CURRENT_PLAN_SUMMARY, summarized_source_hash: "source-v1", updated_at: SOURCE_TIME + 60 });
  await expect(plan.locator(".timeline-summary-text")).toHaveText(CURRENT_PLAN_SUMMARY);
  const facts = page.getByRole("region", { name: "当前研究工作状态", exact: true });
  await expect(facts.locator(".target-research-facts")).not.toBeVisible();
  await facts.getByText("查看输入、产物与交接", { exact: true }).click();
  await expect(facts.locator(".target-research-facts dt")).toHaveCount(7);
  expect(errors).toEqual([]);
});

test("recorder revisions update without snapshot changes and preserve old sentences through generation failures", async ({ page }) => {
  const { timeline, snapshot, recorder, reads, errors } = await openTimeline(page, { runningBundle: true });
  const key = `stage:${CYCLE_REF}:bundle`;
  const bundle = summaryNode(timeline, key);
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const initialRevision = snapshot.revision, initialReads = reads.overview;
  const savedSnapshot = JSON.stringify(snapshot);
  updateSummary(recorder, key, { status: "updating", source_hash: "source-v2", summarized_source_hash: null, summary: null });
  await expect(bundle.locator(".timeline-summary-status")).toHaveText("正在更新总结");
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(bundle).toHaveAttribute("data-summary-stale", "true");
  updateSummary(recorder, key, { status: "failed" });
  await expect(bundle.locator(".timeline-summary-status")).toHaveText("更新暂不可用");
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const nextSentence = "被试划分已核验，下一步比较预处理设置对评测结果的影响。";
  updateSummary(recorder, key, { status: "ready", summary: nextSentence, summarized_source_hash: "source-v2", updated_at: SOURCE_TIME + 120 });
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(nextSentence);
  await expect(bundle.locator(".timeline-summary-status")).toHaveCount(0);
  expect(JSON.stringify(snapshot)).toBe(savedSnapshot);
  await expect(page.locator(".lumen-connection code")).toHaveText(`状态 ${initialRevision}`);
  expect(reads.overview).toBe(initialReads);
  expect(reads.summaries).toBeGreaterThan(1);
  await advanceSnapshot(page, snapshot);
  await advanceSnapshot(page, snapshot);
  expect(reads.overview).toBe(initialReads);
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(nextSentence);
  expect(errors).toEqual([]);
});

test("switching Quests clears old sentences and rejects an answer carrying the previous Quest identity", async ({ page }) => {
  const { timeline, snapshot, overview, recorder, reads, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const nextQuest = "quest-recorder-next", nextQuestion = "question-recorder-next", nextCycle = "cycle-recorder-next";
  Object.assign(snapshot.research_space.current_quest, { quest_ref: nextQuest });
  snapshot.research_control.quest_ref = nextQuest;
  Object.assign(snapshot.research_control.foreground, { quest_ref: nextQuest, question_ref: nextQuestion, cycle_ref: nextCycle });
  Object.assign(snapshot.research_space.current_question, { quest_ref: nextQuest, question_ref: nextQuestion });
  snapshot.question_tree.items = [{ ...snapshot.question_tree.items[0], quest_ref: nextQuest, question_ref: nextQuestion }];
  Object.assign(overview, { quest_ref: nextQuest, question_ref: nextQuestion, cycle_ref: nextCycle, cycle_ordinal: 1,
    cycles: [{ cycle_ref: nextCycle, question_ref: nextQuestion, ordinal: 1, stages: {} }] });
  const forbiddenSentence = "上一研究的回答不能出现在新研究里。";
  recorder.data.nodes = [recorderNode("question", nextQuestion, null, forbiddenSentence)];
  recorder.data.revision += 1;
  // Its node key deliberately matches the new tree, but its Quest envelope is wrong.
  await advanceSnapshot(page, snapshot);
  await expect(summaryNode(timeline, `question:${nextQuestion}`).locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  await expect.poll(() => reads.summaryQuests.includes(nextQuest)).toBe(true);
  await expect(timeline.locator(".timeline-summary-availability")).toBeVisible();
  await expect(timeline).not.toContainText(CURRENT_PLAN_SUMMARY);
  await expect(timeline).not.toContainText(forbiddenSentence);
  recorder.data.quest_ref = nextQuest;
  recorder.data.nodes[0].summary = "检验新研究问题的关键假设，明确需要补足的证据。";
  recorder.data.revision += 1;
  await expect(summaryNode(timeline, `question:${nextQuestion}`).locator(".timeline-summary-text")).toHaveText(recorder.data.nodes[0].summary!);
  await expect(timeline.locator(".timeline-summary-availability")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a paused runtime changes only the live badges while saved recorder sentences remain readable", async ({ page }) => {
  const { timeline, snapshot, runtime, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const observedAt = new Date().toISOString();
  runtime.status = {
    schema_ref: "meta-research/runtime-status/v1", revision: snapshot.revision + 1,
    observed_at: observedAt, updated_at: observedAt, state: "paused",
    current_task: { kind: "target", title: "pilot_subject_level_eeg", run_ref: "target-run-143-a", target_ref: "target-143-a", status: "paused" },
    waiting_reason: null, pending_requests: 0,
    foreground: { ...snapshot.research_control.foreground, status: "paused", grant_status: "suspended" },
    health: { status: "ready", checks: [] },
  };
  await expect(cycleNode(timeline).locator('.research-timeline-stage[data-stage="bundle"] .research-timeline-stage-state')).toContainText("已暂停");
  const target = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".research-timeline-target-state")).toHaveText("已暂停");
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  expect(snapshot.research_control.foreground.status).toBe("active");
  expect(errors).toEqual([]);
});

test("historical recorder sentences retain question cycle stage and Target identities with source details", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page);
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expandHistory(timeline);
  expect(await timeline.locator(".research-timeline-cycle").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-cycle-ref")))).toEqual(["cycle-r1", "cycle-r2", CYCLE_REF]);
  await expect(summaryNode(timeline, "question:question-r1").locator(".timeline-summary-text")).toHaveText("验证跨模态一致性约束能否稳定改善零样本迁移。");
  await expect(summaryNode(timeline, "question:question-r2").locator(".timeline-summary-text")).toHaveText("考察扩大样本规模后迁移增益的稳定性。");
  await expect(cycleNode(timeline).locator(".research-timeline-target")).toHaveCount(2);
  await expect(cycleNode(timeline, "cycle-r1").locator(".research-timeline-target .timeline-summary-text")).toHaveText("围绕小规模基准验证保留可核验的实验记录。");
  await cycleNode(timeline, "cycle-r1").locator(".research-timeline-stage-result").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("以跨模态一致性损失对齐表征");
  await dialog.getByLabel("关闭研究思路").click();
  const secondDetails = cycleNode(timeline, "cycle-r2").locator(":scope > details");
  await secondDetails.locator(":scope > summary").click();
  await expect(secondDetails).not.toHaveAttribute("open", "");
  await secondDetails.locator(":scope > summary").click();
  await expect(stageSummary(timeline, "plan", "cycle-r2")).toHaveText(CURRENT_PLAN_SUMMARY);
  expect(errors).toEqual([]);
});

test("new cycles and revisited questions retain chronological rows while their summaries arrive independently", async ({ page }) => {
  const { timeline, snapshot, overview, recorder, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const enterCycle = async (cycleRef: string, questionRef: string, ordinal: number) => {
    const question = snapshot.question_tree.items.find((item: JsonRecord) => item.question_ref === questionRef);
    Object.assign(snapshot.research_control.foreground, { cycle_ref: cycleRef, question_ref: questionRef, stage: "idea", epoch: 1 });
    Object.assign(snapshot.research_space.current_question, { ...question, graph_revision: snapshot.revision + 1 });
    Object.assign(snapshot.idea_stage.eligibility, { status: "eligible", cycle_ref: cycleRef, question_ref: questionRef });
    snapshot.idea_stage.stage_run_request = { cycle_ref: cycleRef, epoch: 1, accepted_question_binding: { quest_ref: QUEST_REF, question_ref: questionRef } };
    snapshot.idea_stage.run = { status: "running", run_ref: `idea-run-${cycleRef}` };
    snapshot.research_control.managed_runs = [];
    Object.assign(overview, { cycle_ref: cycleRef, question_ref: questionRef, cycle_ordinal: ordinal });
    overview.cycles.push({ cycle_ref: cycleRef, question_ref: questionRef, ordinal, stages: {} });
    await advanceSnapshot(page, snapshot);
    await expect(cycleNode(timeline, cycleRef)).toBeVisible();
    await expect(cycleNode(timeline, cycleRef)).toHaveAttribute("data-current", "true");
    await expect(stageSummary(timeline, "idea", cycleRef)).toHaveText("记录员正在整理…");
  };
  await enterCycle("cycle-r4", QUESTION_REF, 4);
  await expect(timeline.locator(`.research-timeline-question[data-question-ref="${QUESTION_REF}"]`)).toHaveCount(1);
  await enterCycle("cycle-r5", "question-143-sibling", 5);
  await enterCycle("cycle-r6", QUESTION_REF, 6);
  const newSentence = "沿用已有证据重新审视假设，优先澄清跨基准差异的来源。";
  recorder.data.nodes.push(recorderNode("stage", QUESTION_REF, "cycle-r6", newSentence, { stage: "idea" }));
  recorder.data.revision += 1;
  await expect(stageSummary(timeline, "idea", "cycle-r6")).toHaveText(newSentence);
  await expandHistory(timeline);
  expect(await timeline.locator(".research-timeline-question").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-question-ref")))).toEqual([
    "question-r1", "question-r2", QUESTION_REF, "question-143-sibling", QUESTION_REF,
  ]);
  expect(await timeline.locator(".research-timeline-cycle").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-cycle-ref")))).toEqual([
    "cycle-r1", "cycle-r2", CYCLE_REF, "cycle-r4", "cycle-r5", "cycle-r6",
  ]);
  await expect(stageSummary(timeline, "plan", CYCLE_REF)).toHaveText(CURRENT_PLAN_SUMMARY);
  await expect(timeline.locator('.research-timeline-cycle[data-current="true"]')).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("Target runtime changes and new recorder sentences preserve graph order even when Target two starts first", async ({ page }) => {
  const { timeline, snapshot, catalog, recorder, reads, errors } = await openTimeline(page, { runningBundle: true, secondTargetFirst: true });
  const targets = cycleNode(timeline).locator('.research-timeline-stage[data-stage="bundle"] .research-timeline-target');
  const first = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  const targetRefs = () => targets.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-target-ref")));
  const targetNumbers = () => targets.locator(".research-timeline-target-open > b").evaluateAll(nodes => nodes.map(node => node.textContent?.match(/(\d+)$/)?.[1]));
  await expect(targets).toHaveCount(2);
  await expect(first.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  expect(await targetRefs()).toEqual(["target-143-a", "target-143-b"]);
  expect(await targetNumbers()).toEqual(["1", "2"]);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("等待启动");
  const initialReads = reads.overview;
  catalog.sessions.unshift(timelineSession("target-r3a", "target", "Target T1 · pilot_subject_level_eeg", CYCLE_REF, "executing", {
    short_title: "Target T1", question_ref: QUESTION_REF, target_ref: "target-143-a", run_ref: "target-run-143-a", is_current: true, created_at: SOURCE_TIME + 60,
  }));
  snapshot.bundle_stage.target_graph.targets[0].status = "running";
  snapshot.bundle_stage.target_graph.targets[0].target_run_ref = "target-run-143-a";
  await advanceSnapshot(page, snapshot);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("正在执行");
  await expect(first.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  catalog.sessions[0].status = "completed";
  catalog.sessions[0].is_executing = false;
  snapshot.bundle_stage.target_graph.targets[0].status = "completed";
  await advanceSnapshot(page, snapshot);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("已完成");
  const completedSummary = "被试独立划分的复核已完成，现有评测记录可用于后续证据核验。";
  updateSummary(recorder, `target:${CYCLE_REF}:target-143-a`, { summary: completedSummary, source_hash: "target-v2", summarized_source_hash: "target-v2" });
  await expect(first.locator(".timeline-summary-text")).toHaveText(completedSummary);
  catalog.sessions.push(timelineSession("target-r3c", "target", "Target T3 · 独立数据复核", CYCLE_REF, "pending", {
    short_title: null, question_ref: QUESTION_REF, target_ref: "target-143-c", run_ref: "target-run-143-c", is_current: true, created_at: SOURCE_TIME + 121,
  }));
  await expect(targets).toHaveCount(3);
  expect(await targetRefs()).toEqual(["target-143-a", "target-143-b", "target-143-c"]);
  expect(await targetNumbers()).toEqual(["1", "2", "3"]);
  await expect(summaryNode(timeline, `target:${CYCLE_REF}:target-143-c`).locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  expect(reads.overview).toBe(initialReads);
  expect(errors).toEqual([]);
});

test("a resumed Target keeps its current root and saved sentence when a superseded root was recorded later", async ({ page }) => {
  const { timeline, catalog, reads, errors } = await openTimeline(page, { runningBundle: true });
  const target = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  const previousReads = reads.roots;
  catalog.sessions.push(timelineSession("target-r3a-old", "target", "Target T3 · 恢复前的历史会话", CYCLE_REF, "waiting", {
    short_title: "Target T3 历史", question_ref: QUESTION_REF, target_ref: "target-143-a", run_ref: "target-run-143-a",
    is_current: false, is_executing: false, activity_label: "已由新会话接续", created_at: SOURCE_TIME + 900, updated_at: SOURCE_TIME + 900,
  }));
  await expect.poll(() => reads.roots).toBeGreaterThan(previousReads);
  await expect(page.locator('.stage-root-links [data-session-ref="target-r3a-old"]')).toBeVisible();
  await expect(target).toHaveCount(1);
  await expect(target.locator(".research-timeline-target-state")).toHaveText("正在执行");
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await target.locator(".research-timeline-target-open").click();
  await expect(page.locator('.root-session-conversation[data-session-ref="target-r3a"]')).toBeVisible();
  await expect(page.locator('.root-session-conversation[data-session-ref="target-r3a-old"]')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("saved recorder sentences retain readable tree indentation on desktop and mobile", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(summaryNode(timeline, "question:question-r1").locator(".timeline-summary-text")).toHaveText("验证跨模态一致性约束能否稳定改善零样本迁移。");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await expect(timeline).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    const question = timeline.locator(`.research-timeline-question[data-question-ref="${QUESTION_REF}"]`);
    const cycle = cycleNode(timeline);
    const stage = cycle.locator('.research-timeline-stage[data-stage="bundle"]');
    const target = stage.locator('.research-timeline-target[data-target-ref="target-143-a"]');
    const [questionBox, cycleBox, stageBox, targetBox] = await Promise.all([question.boundingBox(), cycle.boundingBox(), stage.boundingBox(), target.boundingBox()]);
    expect(cycleBox!.x).toBeGreaterThan(questionBox!.x);
    expect(stageBox!.x).toBeGreaterThan(cycleBox!.x);
    expect(targetBox!.x).toBeGreaterThan(stageBox!.x);
    for (const summary of await cycle.locator(".timeline-summary-text").all()) await expect(summary).toBeVisible();
    await timeline.locator(".research-timeline-scroll").evaluate(node => { node.scrollTop = 0; });
    const screenshotStyle = ".output-language-control.floating { visibility: hidden !important; }";
    await timeline.screenshot({ path: `test-results/timeline-tree-${width}.png`, style: screenshotStyle });
    if (width === 390) {
      await timeline.locator(".research-timeline-scroll").evaluate(node => { node.scrollTop = node.scrollHeight; });
      await timeline.screenshot({ path: "test-results/timeline-tree-390-lower.png", style: screenshotStyle });
    }
  }
  expect(errors).toEqual([]);
});

test("the Quest timeline remains available after the foreground ends and on a fresh page load", async ({ page }) => {
  const { timeline, snapshot, overview, catalog, errors } = await openTimeline(page);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const historicalRecord = "历史验证完成：两个公开基准均观察到稳定的迁移增益。";
  const historicalStream = JSON.stringify({ type: "item.completed", thread_id: "native-target-r1", item: { type: "agent_message", text: historicalRecord } }) + "\n";
  catalog.sessions[0].operations = [{ operation_ref: "historical-target-operation", label: "历史验证", phase: "work", status: "completed", created_at: SOURCE_TIME, updated_at: SOURCE_TIME }];
  await page.route("**/root-sessions/target-r1/output?*", route => {
    const offset = Number(new URL(route.request().url()).searchParams.get("after") ?? 0);
    const bytes = new TextEncoder().encode(historicalStream).length;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({
      schema_ref: "meta-research/root-session-output/v1", quest_ref: QUEST_REF, session_ref: "target-r1", operation_ref: "historical-target-operation",
      stream_ref: "historical-target-stream", text: offset === 0 ? historicalStream : "", offset, next_offset: bytes, source_bytes: bytes,
      has_more: false, source_caught_up: true, source_updated_at: SOURCE_TIME, observed_at: SOURCE_TIME, native_session_ref: "native-target-r1", status: "terminal",
    }) });
  });
  snapshot.research_control.foreground = null;
  snapshot.research_space.foreground_cycle_count = 0;
  overview.foreground = null;
  overview.cycle_ref = null;
  overview.question_ref = null;
  overview.cycle_ordinal = null;
  overview.status = "idle";
  await advanceSnapshot(page, snapshot);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expect(timeline.locator('.research-timeline-cycle[data-current="true"]')).toHaveCount(0);
  await page.reload({ waitUntil: "domcontentloaded" });
  await timeline.scrollIntoViewIfNeeded();
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expandHistory(timeline);
  await expect(timeline.locator(".research-timeline-cycle")).toHaveCount(3);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  await cycleNode(timeline, "cycle-r1").locator('.research-timeline-target[data-target-ref="target-target-r1"] .research-timeline-target-open').click();
  const historicalConversation = page.locator('.root-session-conversation[data-session-ref="target-r1"]');
  await expect(historicalConversation).toBeVisible();
  await expect(historicalConversation).toContainText("历史轮次");
  await expect(historicalConversation).toContainText(historicalRecord);
  expect(errors).toEqual([]);
});
