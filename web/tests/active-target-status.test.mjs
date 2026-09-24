import assert from "node:assert/strict";
import test from "node:test";
import { observedActiveTarget } from "../src/activeTargetStatus.ts";

const now = Date.parse("2026-09-14T12:00:00Z");
const fixture = () => ({
  status: {
    state: "waiting", current_task: { kind: "stage", title: "实验与证据", run_ref: "bundle:1", target_ref: null, status: "awaiting_acceptance" },
    waiting_reason: "等待 Target 完成", foreground: { quest_ref: "quest:1", cycle_ref: "cycle:1", question_ref: "question:1", stage: "bundle", status: "active" },
    health: { status: "ready", checks: [{ name: "bundle_stage_worker", status: "ready" }] },
  },
  roots: {
    quest_ref: "quest:1", observed_at: now / 1_000, limited: false, active_session_refs: ["session:target"],
    sessions: [{ session_ref: "session:target", kind: "target", title: "Target T1 · measured trial", stage: "bundle",
      cycle_ref: "cycle:1", question_ref: "question:1", is_current: true, is_executing: true, status: "executing",
      target_ref: "target:1", run_ref: "target-run:1", updated_at: now / 1_000 }],
  },
});

test("a current observed Target resolves the Bundle wait without modifying the source status", () => {
  const { status, roots } = fixture();
  const result = observedActiveTarget(status, roots, now);
  assert.equal(result.status.state, "running");
  assert.equal(result.status.current_task.target_ref, "target:1");
  assert.equal(result.status.current_task.run_ref, "target-run:1");
  assert.equal(result.status.waiting_reason, null);
  assert.equal(result.observedAt, "2026-09-14T12:00:00.000Z");
  assert.equal(status.state, "waiting");
  assert.equal(status.current_task.kind, "stage");
});

test("late results from a different Quest, Cycle or Question never replace current status", () => {
  for (const name of ["quest_ref", "cycle_ref", "question_ref"]) {
    const { status, roots } = fixture();
    status.foreground[name] = "changed-scope";
    assert.equal(observedActiveTarget(status, roots, now), null);
  }
});

test("real failure, pause, completion and worker faults always remain visible", () => {
  for (const state of ["failed", "paused", "completed", "idle", "running"]) {
    const { status, roots } = fixture();
    status.state = state;
    assert.equal(observedActiveTarget(status, roots, now), null);
  }
  for (const mutate of [
    status => { status.foreground.status = "paused"; },
    status => { status.health.status = "unavailable"; },
    status => { status.health.checks.push({ name: "target_run_worker", status: "unavailable" }); },
    status => { status.current_task.kind = "target"; },
    status => { status.foreground.stage = "reasoning"; },
  ]) {
    const { status, roots } = fixture();
    mutate(status);
    assert.equal(observedActiveTarget(status, roots, now), null);
  }
});

test("incomplete, old and implausibly future observations cannot assert execution", () => {
  for (const change of [{ limited: true }, { limited: undefined }, { observed_at: (now - 45_001) / 1_000 },
    { observed_at: (now + 5_001) / 1_000 }, { observed_at: NaN }]) {
    const { status, roots } = fixture();
    Object.assign(roots, change);
    assert.equal(observedActiveTarget(status, roots, now), null);
  }
});

test("an old session, terminal Provider or unbound Target is not execution evidence", () => {
  for (const change of [{ is_current: false }, { is_executing: false }, { status: "completed" },
    { kind: "stage" }, { run_ref: null }, { target_ref: null }]) {
    const { status, roots } = fixture();
    Object.assign(roots.sessions[0], change);
    assert.equal(observedActiveTarget(status, roots, now), null);
  }
  const { status, roots } = fixture();
  roots.active_session_refs = [];
  assert.equal(observedActiveTarget(status, roots, now), null);
});
