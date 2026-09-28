import { expect, test } from "@playwright/test";
import { summaryNeedsAcceptedResultReview, type TimelineSummaryNode } from "../src/timelineSummaries.js";
import type { OverviewCycle, OverviewStageArtifact } from "../src/ResearchOverview.js";
import type { RootSession } from "../src/rootSessionsApi.js";

// Real Plan14 timing: the saved sentence postdates run completion, but its
// source scan predates it. No browser, provider or production API is used.
const scannedAt = 1790587274.4894192;
const completedAt = 1790587368.5733905;
const savedAt = 1790587381.3309774;
const cycle: OverviewCycle = { cycle_ref: "cycle-14", question_ref: "question-14", ordinal: 14, stages: {} };
const node: TimelineSummaryNode = {
  node_key: "stage:cycle-14:plan", kind: "stage", cycle_ref: cycle.cycle_ref, question_ref: cycle.question_ref,
  stage: "plan", target_ref: null, summary: "本阶段缺少可见的方案正文。", status: "ready",
  source_hash: "same-scanned-source", summarized_source_hash: "same-scanned-source", updated_at: savedAt, sources: [],
};
const artifact: OverviewStageArtifact = {
  stage: "plan", status: "accepted", epoch: 14,
  source: { commit_ref: "plan-commit-14", run_ref: "plan-run-14", outcome_ref: "formal-plan-14" },
  content: { obligations: [{ ref: "first" }, { ref: "second" }] }, reason: null,
};
const session: RootSession = {
  session_ref: "plan-root", root_session_ref: "plan-root", kind: "stage", title: "Plan", stage: "plan",
  related_stages: ["plan"], scope_label: "本轮研究", owner_session_ref: null, status: "completed",
  is_executing: false, is_current: false, run_ref: "plan-run-14", target_ref: null,
  cycle_ref: cycle.cycle_ref, question_ref: cycle.question_ref, created_at: scannedAt - 100,
  updated_at: completedAt, operations: [],
};
const check = (n = node, a = artifact, s = session, observed = scannedAt) =>
  summaryNeedsAcceptedResultReview(n, cycle, [a], [s], observed);

test("Plan14 gets a review hint without altering the old prose, source hash or ready state", () => {
  const original = JSON.stringify({ node, artifact, session });
  expect(completedAt).toBeLessThan(savedAt);
  expect(check()).toBe(true);
  expect(check({ ...node, kind: "cycle", node_key: "cycle:cycle-14", stage: null })).toBe(true);
  expect(JSON.stringify({ node, artifact, session })).toBe(original);
});

test("a later source scan clears the time hint; missing or nonfinite times do not infer freshness", () => {
  for (const observed of [completedAt, completedAt + 1, 0, -1, Number.NaN, Number.POSITIVE_INFINITY])
    expect(check(node, artifact, session, observed)).toBe(false);
  for (const updated_at of [null, Number.NaN, Number.POSITIVE_INFINITY])
    expect(check(node, artifact, { ...session, updated_at })).toBe(false);
});

test("a result from another run, cycle, question or stage cannot age this summary", () => {
  for (const changed of [{ run_ref: "another-run" }, { cycle_ref: "another-cycle" },
    { question_ref: "another-question" }, { question_ref: null }, { stage: "reasoning" }])
    expect(check(node, artifact, { ...session, ...changed })).toBe(false);
  expect(check({ ...node, cycle_ref: "another-cycle" })).toBe(false);
  expect(check({ ...node, question_ref: "another-question" })).toBe(false);
  expect(check({ ...node, stage: "reasoning" })).toBe(false);
});

test("unaccepted, skipped, missing-commit and unfinished records are not formal new-result evidence", () => {
  for (const status of ["skipped", "exhausted", "unavailable", "report_accepted"] as const)
    expect(check(node, { ...artifact, status })).toBe(false);
  expect(check(node, { ...artifact, source: { ...artifact.source, run_ref: null } })).toBe(false);
  expect(check(node, { ...artifact, source: { ...artifact.source, commit_ref: null } })).toBe(false);
  for (const status of ["executing", "waiting", "paused", "failed", "pending"] as const)
    expect(check(node, artifact, { ...session, status })).toBe(false);
  expect(check(node, artifact, { ...session, is_executing: true })).toBe(false);
  expect(check(node, artifact, { ...session, kind: "target" })).toBe(false);
  expect(summaryNeedsAcceptedResultReview(node, cycle, [artifact], [], scannedAt)).toBe(false);
  expect(check({ ...node, summary: null })).toBe(false);
});
