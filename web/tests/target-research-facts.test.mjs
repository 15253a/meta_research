import assert from "node:assert/strict";
import test from "node:test";
import { observedTargetRetry, targetResearchFacts } from "../src/targetResearchFacts.ts";

const target = { target_ref: "target:1", target_key: "trial", target_run_ref: "work:1", spec_hash: "hash:1", dependency_refs: [], status: "committed" };
const commit = (measurement, disposition = "uncertain") => ({ target_ref: "target:1", target_run_ref: "work:1", target_spec_hash: "hash:1", commit_ref: "commit:1", result_disposition: disposition,
  closure: { root_measurement: measurement, research_notes: [{ version_ref: "note:v1" }] } });

test("accepted work without an overall classification retains evaluation, missing metrics and exact sources", () => {
  const measurement = { execution_status: "executed", evaluation_status: "pending",
    metrics: { observed_loss: 0.25, validation_accuracy: null },
    formal_entities: [{ variant_run_ref: "run:1", evaluation_attempt_ref: null }],
    result_asset: { asset_ref: "observations", version_ref: "observations:v2" } };
  const unclassified = commit(measurement);
  delete unclassified.result_disposition;
  for (const accepted of [unclassified, { ...unclassified, result_disposition: null }]) {
    const facts = targetResearchFacts(target, accepted);
    assert.equal(facts.summary, "研究记录已接纳");
    assert.equal(facts.result, null);
    assert.equal(facts.facts.find(item => item.label === "接纳").text, "研究记录已接纳");
    assert.equal(facts.pendingEvaluation, true);
    assert.equal(facts.technicalFailure, false);
    assert.equal(facts.facts.find(item => item.label === "评价").text, "执行已记录 · 评价待办");
    assert.deepEqual(facts.sources.root_measurement, measurement);
    assert.equal(facts.sources.commit_ref, "commit:1");
    assert.equal(facts.sources.target_spec_hash, "hash:1");
    assert.deepEqual(facts.readableAssets, [
      { versionRef: "observations:v2", label: "读取结果原文" },
      { versionRef: "note:v1", label: "读取交接说明" },
    ]);
  }
});

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

const workerFailure = {
  state: "failed",
  current_task: { kind: "target", target_ref: "target:1", run_ref: "work:1" },
  health: { checks: [{ name: "target_run_worker", status: "unavailable", reason: { code: "target_root_artifact_storage_unavailable" } }] },
};

test("an exact live Target worker failure overrides a still-running frontier without inventing accepted work", () => {
  const facts = targetResearchFacts({ ...target, status: "running" }, undefined, [], workerFailure);
  assert.equal(facts.summary, "Target 推进受阻");
  assert.equal(facts.technicalFailure, true);
  assert.equal(facts.result, "研究结果尚未接纳");
  assert.deepEqual(facts.artifactRefs, []);
});

test("runtime failures never leak into another Target, another Run, or accepted research facts", () => {
  for (const observation of [
    { ...workerFailure, current_task: { ...workerFailure.current_task, target_ref: "target:other" } },
    { ...workerFailure, current_task: { ...workerFailure.current_task, run_ref: "work:old" } },
    { ...workerFailure, current_task: { ...workerFailure.current_task, run_ref: null } },
    { ...workerFailure, state: "running" },
    { ...workerFailure, health: { checks: [{ name: "bundle_stage_worker", status: "unavailable" }] } },
    null,
  ]) {
    const facts = targetResearchFacts({ ...target, status: "running" }, undefined, [], observation);
    assert.equal(facts.summary, "Target 正在开展工作");
    assert.equal(facts.technicalFailure, false);
  }
  const accepted = targetResearchFacts(target, commit({ formal_entities: [] }), [], workerFailure);
  assert.match(accepted.summary, /研究记录已接纳/);
  assert.equal(accepted.technicalFailure, false);
});

test("a paused current Target keeps its records and never pauses another Run or an accepted result", () => {
  const paused = { ...workerFailure, state: "paused" };
  const facts = targetResearchFacts({ ...target, status: "running" }, undefined, [], paused);
  assert.equal(facts.summary, "研究已暂停，保留 Target 记录");
  assert.equal(facts.technicalFailure, false);
  assert.equal(facts.sources.target_run_ref, "work:1");
  assert.deepEqual(facts.artifactRefs, []);
  assert.equal(targetResearchFacts({ ...target, target_run_ref: "work:other", status: "running" }, undefined, [], paused).summary, "Target 正在开展工作");
  assert.match(targetResearchFacts(target, commit({}), [], paused).summary, /研究记录已接纳/);
});

const retryStatus = { ...workerFailure, current_task: { ...workerFailure.current_task, status: "running" },
  foreground: { quest_ref: "quest:1", cycle_ref: "cycle:1", question_ref: "question:1", stage: "bundle", status: "active" } };
const retryRoots = { quest_ref: "quest:1", limited: false, active_session_refs: ["session:1"], sessions: [{ session_ref: "session:1",
  kind: "target", stage: "bundle", cycle_ref: "cycle:1", question_ref: "question:1", target_ref: "target:1", run_ref: "work:1",
  is_current: true, is_executing: true, status: "executing" }] };

test("an exact active retry retains the previous error without presenting the Target as stalled", () => {
  const retry = observedTargetRetry(retryStatus, retryRoots);
  const facts = targetResearchFacts({ ...target, status: "running" }, undefined, [], retryStatus, retry);
  assert.equal(facts.summary, "Target 正在重试，上次入库受阻");
  assert.equal(facts.technicalFailure, true);
  assert.deepEqual(facts.artifactRefs, []);
  const otherFailure = { ...retryStatus, health: { checks: [{ ...workerFailure.health.checks[0], reason: { code: "provider_transport_unavailable" } }] } };
  assert.equal(observedTargetRetry(otherFailure, retryRoots).summary, "Target 正在重试，上次推进受阻");
  assert.match(targetResearchFacts(target, commit({}), [], retryStatus, retry).summary, /已接纳/);
  assert.equal(targetResearchFacts({ ...target, target_run_ref: "work:other", status: "running" }, undefined, [], retryStatus, retry).summary, "Target 正在开展工作");
});

test("finalizing, uncertain or mismatched sessions cannot claim an active retry", () => {
  assert.equal(observedTargetRetry({ ...retryStatus, current_task: { ...retryStatus.current_task, status: "finalizing" } }, retryRoots), null);
  assert.equal(observedTargetRetry({ ...retryStatus, state: "paused" }, retryRoots), null);
  for (const roots of [null, { ...retryRoots, limited: true }, { ...retryRoots, quest_ref: "quest:other" },
    { ...retryRoots, active_session_refs: [] }, ...[
      { run_ref: "work:old" }, { target_ref: "target:other" }, { cycle_ref: "cycle:old" },
      { question_ref: "question:other" }, { is_current: false }, { is_executing: false }, { status: "waiting" },
    ].map(change => ({ ...retryRoots, sessions: [{ ...retryRoots.sessions[0], ...change }] }))]) {
    assert.equal(observedTargetRetry(retryStatus, roots), null);
  }
});
