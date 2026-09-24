import assert from "node:assert/strict";
import test from "node:test";
import { targetResearchFacts } from "../src/targetResearchFacts.ts";

const target = { target_ref: "target:1", target_key: "trial", target_run_ref: "work:1", spec_hash: "hash:1", dependency_refs: [], status: "committed" };
const commit = (measurement, disposition = "uncertain") => ({ target_ref: "target:1", target_run_ref: "work:1", target_spec_hash: "hash:1", commit_ref: "commit:1", result_disposition: disposition,
  closure: { root_measurement: measurement, research_notes: [{ version_ref: "note:v1" }] } });

test("accepted run-only work keeps its evaluation pending and its negative finding out of technical failure", () => {
  const facts = targetResearchFacts(target, commit({ execution_status: "executed", evaluation_status: "pending", formal_entities: [{ variant_run_ref: "run:1", evaluation_attempt_ref: null }] }, "negative"));
  assert.equal(facts.pendingEvaluation, true);
  assert.equal(facts.technicalFailure, false);
  assert.deepEqual(facts.runRefs, ["run:1"]);
  assert.deepEqual(facts.attemptRefs, []);
  assert.match(facts.summary, /已接纳.*负面/);
  assert.match(facts.facts.find(item => item.label === "评价").text, /评价待办/);
});

test("mixed work retains pending evaluation even when another execution has an attempt", () => {
  const facts = targetResearchFacts(target, commit({ formal_entities: [{ variant_run_ref: "run:1", evaluation_attempt_ref: "eval:1" }, { variant_run_ref: "run:2", evaluation_attempt_ref: null }] }));
  assert.equal(facts.pendingEvaluation, true);
  assert.deepEqual(facts.attemptRefs, ["eval:1"]);
  assert.match(facts.facts.find(item => item.label === "评价").text, /部分已评价/);
});

test("failed evaluation is displayed separately from the accepted uncertain research record and runs are counted once", () => {
  const facts = targetResearchFacts(target, commit({ formal_entities: [
    { variant_run_ref: "run:1", evaluation_attempt_ref: "eval:1", evaluation_status: "completed" },
    { variant_run_ref: "run:1", evaluation_attempt_ref: "eval:2", evaluation_status: "failed", metric_result_ref: null },
  ] }));
  assert.deepEqual(facts.runRefs, ["run:1"]);
  assert.equal(facts.failedAttempts, 1);
  assert.equal(facts.technicalFailure, false);
  assert.match(facts.facts.find(item => item.label === "评价").text, /1 项评价执行失败.*重试/);
  assert.match(facts.summary, /已接纳.*不确定/);
});

test("a Target process and empty metrics cannot fabricate an actual method execution or evaluation", () => {
  const facts = targetResearchFacts({ ...target, status: "running" }, commit({ metrics: {}, formal_entities: [] }));
  assert.deepEqual(facts.runRefs, []);
  assert.deepEqual(facts.attemptRefs, []);
  assert.match(facts.facts.find(item => item.label === "实际执行").text, /方法执行待记录/);
  assert.match(facts.facts.find(item => item.label === "评价").text, /尚无可确认/);
});

test("mismatched historical commits cannot mark current execution accepted", () => {
  const facts = targetResearchFacts({ ...target, target_run_ref: "work:2", status: "failed" }, commit({ variant_run_ref: "run:1", evaluation_attempt_ref: "eval:1" }));
  assert.equal(facts.technicalFailure, true);
  assert.deepEqual(facts.runRefs, []);
  assert.match(facts.summary, /技术执行受阻/);
});

test("a changed Target spec never borrows the old run's accepted assets", () => {
  const facts = targetResearchFacts({ ...target, spec_hash: "hash:2" }, commit({ variant_run_ref: "run:1" }));
  assert.deepEqual(facts.runRefs, []);
  assert.equal(facts.sources.commit_ref, undefined);
});

test("human waiting is target scoped and preserves recorded responses for handoff", () => {
  const request = { request_ref: "request:1", target_assertion: { target_ref: "target:1" }, status: "open", obligation: "请判断投入范围", responses: [{ note: "先完成局部核验" }] };
  const facts = targetResearchFacts(target, undefined, [request, { ...request, request_ref: "other", target_assertion: { target_ref: "target:2" } }]);
  assert.match(facts.summary, /待人类答复/);
  assert.equal(facts.sources.human_requests.length, 1);
  assert.equal(facts.sources.human_requests[0].responses[0].note, "先完成局部核验");
});

test("research coordination is shown as adjustment rather than technical failure", () => {
  const facts = targetResearchFacts({ ...target, status: "blocked", blocker: { code: "target_semantic_change_required" } });
  assert.equal(facts.technicalFailure, false);
  assert.equal(facts.summary, "等待研究调整");
});
