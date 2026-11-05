import assert from "node:assert/strict";
import test from "node:test";
import { rootSessionStatusUncertain } from "../src/rootSessionObservation.ts";

const fixture = () => {
  const bundle = { session_ref: "bundle", kind: "stage", status: "waiting", is_executing: false, limited: false, operations: [] };
  const target = { session_ref: "target", kind: "target", status: "executing", is_executing: true, limited: true,
    operations: [{ operation_ref: "target-turn-1", status: "executing" }] };
  const idea = { session_ref: "idea", kind: "stage", status: "completed", is_executing: false, limited: true, operations: [] };
  const deepfetch = { session_ref: "deepfetch", kind: "deepfetch", status: "completed", is_executing: false, operations: [] };
  // This is the mixed limitation shape returned by the live 8771 observation.
  const catalog = { limited: true, reasons: [{ code: "deepfetch_root_operation_unavailable" },
    { code: "root_session_operations_limited" }, { code: "root_session_operations_limited" }],
    sessions: [bundle, target, idea, deepfetch], active_session_refs: [target.session_ref] };
  return { catalog, bundle, target, idea, deepfetch };
};

test("mixed historical and provider omissions do not erase observed Bundle or Target state", () => {
  const { catalog, bundle, target, idea, deepfetch } = fixture();
  const before = structuredClone(catalog);
  assert.equal(rootSessionStatusUncertain(catalog, bundle), false);
  assert.equal(rootSessionStatusUncertain(catalog, target), false);
  assert.equal(rootSessionStatusUncertain(catalog, idea), true);
  assert.equal(rootSessionStatusUncertain(catalog, deepfetch), true);
  assert.deepEqual(catalog, before);
});

test("limited history needs all positive execution signals before reporting native execution", () => {
  for (const change of [
    f => { f.target.status = "waiting"; },
    f => { f.target.is_executing = false; },
    f => { f.catalog.active_session_refs = []; },
    f => { f.target.operations[0].status = "completed"; },
    f => { f.target.kind = "deepfetch"; },
  ]) {
    const f = fixture();
    change(f);
    assert.equal(rootSessionStatusUncertain(f.catalog, f.target), true);
  }
});

test("missing, malformed, unknown and global reasons remain uncertain even with a running operation", () => {
  for (const reasons of [undefined, null, [], [null], ["root_session_operations_limited"], [{}],
    [{ code: "new_unknown_reason", session_ref: "other" }], [{ code: "root_session_index_limited", session_ref: "other" }],
    [{ code: "root_session_index_part_unavailable" }], [{ code: "root_session_operations_limited", session_ref: 3 }]]) {
    const { catalog, target } = fixture();
    catalog.reasons = reasons;
    assert.equal(rootSessionStatusUncertain(catalog, target), true, JSON.stringify(reasons));
  }
});

test("known scoped reasons affect their session or provider kind only", () => {
  const { catalog, bundle, deepfetch } = fixture();
  catalog.reasons = [{ code: "deepfetch_root_operation_unavailable" }];
  assert.equal(rootSessionStatusUncertain(catalog, bundle), false);
  assert.equal(rootSessionStatusUncertain(catalog, deepfetch), true);
  catalog.reasons = [{ code: "root_session_operations_limited", session_ref: "other" }];
  delete bundle.limited;
  assert.equal(rootSessionStatusUncertain(catalog, bundle), false);
  catalog.reasons[0].session_ref = bundle.session_ref;
  assert.equal(rootSessionStatusUncertain(catalog, bundle), true);
});

test("a complete legacy observation remains usable and absent observations fail closed", () => {
  const { catalog, bundle } = fixture();
  catalog.limited = false;
  delete bundle.limited;
  assert.equal(rootSessionStatusUncertain(catalog, bundle), false);
  assert.equal(rootSessionStatusUncertain(null, bundle), true);
  assert.equal(rootSessionStatusUncertain(catalog, null), true);
  assert.equal(rootSessionStatusUncertain({ ...catalog, limited: undefined }, bundle), true);
  assert.equal(rootSessionStatusUncertain({ ...catalog, sessions: [] }, bundle), true);
});
