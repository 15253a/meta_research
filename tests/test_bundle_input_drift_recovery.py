from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json

import pytest

from meta_research.bundle_skill import (
    BundleDispatchRequest, BundleTargetBatchRequest, CodexBundleSkillAdapter,
)
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.owners.common import canonical_hash
from test_bundle_skill_adapter import (
    _SequenceRunner, _FullConformanceAuthority, _inbox_checkpoint, _fake_codex,
    _plan_document, _target_plan,
)
from test_target_root_finalizer import _current_bundle_runtime
from test_target_launch_admission import _ready_launch


def _adapter(tmp_path, output):
    runner = _SequenceRunner([output])
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8767")
    return adapter, runner


def _request(adapter, kind):
    scope = dict(
        stage_request_ref="stage-request:drift", run_ref="bundle-run:drift",
        attempt_ref="bundle-attempt:drift", fence_ref="bundle-fence:drift",
        graph_ref="target-graph:drift", root_session_ref="root-session:drift",
        native_session_ref="codex-bundle-primary:1", runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:drift", attempt_ref="bundle-attempt:drift", fence_ref="bundle-fence:drift"),
        job_ref="bundle-review:drift",
    )
    if kind == "dispatch":
        return BundleDispatchRequest(
            **scope, generation=1,
            frontier=({"target_ref": "target:ready", "target_key": "target:ready"},),
            state={"schema_ref": "meta-research/bundle-dispatch-state/v1",
                   "target_commit_refs": [], "running_targets": [], "blocked_targets": []},
        )
    plan = _plan_document()
    target_plan = _target_plan(plan, "7" * 64)
    target_plan["completion_contract"]["experiments"][0]["brief"]["required_measurement_unit_keys"] = ["cell:structure-primary"]
    spec = target_plan["initial_strategy_update"]["candidates"][0]
    return BundleTargetBatchRequest(
        **scope, formal_plan_ref="formal-plan:bundle-1", context_pack_ref="context-pack:bundle-1",
        context_pack_hash="7" * 64, plan_document=plan, initial_target_plan=target_plan,
        base_generation=0, base_head_receipt={"receipt_ref": "head:1"},
        current_targets=({"target_ref": "target:ready", "target_key": "target:structure",
                          "spec_hash": canonical_hash(spec), "spec": spec,
                          "dependency_refs": [], "receipt": {}},),
        target_commits=({"commit_ref": "commit:ready", "target_ref": "target:ready", "closure_hash": "8" * 64},),
    )


def _output(kind):
    if kind == "dispatch":
        return {"action": "wait", "selected_target_ref": None, "rationale": "Wait for upstream evidence."}
    return {"strategy_update": {"schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
             "revision": 2, "candidates": [], "requires_accepted_labels": [], "strategy_complete": True},
            "rationale": "The frozen cell has been committed."}


def _changed(request, kind):
    if kind == "dispatch":
        return replace(request, state={**request.state, "target_commit_refs": ["commit:arrived"]})
    return replace(request, target_commits=(*request.target_commits, {"commit_ref": "commit:arrived", "target_ref": "target:other"}))


@pytest.mark.parametrize("kind", ("dispatch", "batch"))
def test_completed_input_drift_retains_result_and_requests_owner_correction(tmp_path, kind):
    adapter, runner = _adapter(tmp_path, _output(kind))
    request = _request(adapter, kind)
    invoke = adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch
    original = invoke(request)
    assert invoke(request) == original
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    original_files = {name: (operation / name).read_bytes() for name in ("invocation.json", "prompt.txt", "last-message.json", "completed.json")}
    with pytest.raises(IdeaSkillUnavailable) as failure:
        invoke(_changed(request, kind))
    assert failure.value.code == "bundle_review_result_contract_invalid"
    checkpoint = failure.value.recovery_checkpoint
    assert checkpoint["contract_failure_detail_code"] == "bundle_operation_inputs_changed"
    assert checkpoint["termination_reason"] == "completed"
    assert len(runner.calls) == 1
    assert {name: (operation / name).read_bytes() for name in original_files} == original_files


@pytest.mark.parametrize("kind", ("dispatch", "batch"))
def test_pending_changed_input_only_reconciles_original_operation(tmp_path, kind):
    adapter, runner = _adapter(tmp_path, _output(kind))
    request = _request(adapter, kind)
    invoke = adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch
    invoke(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    # Model a process whose output exists but has no authenticated terminal seal.
    for name in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation / name).unlink()
    with pytest.raises(IdeaSkillUnavailable) as failure:
        invoke(_changed(request, kind))
    assert failure.value.code == "codex_operation_reconciliation_pending"
    assert failure.value.recovery_checkpoint is None
    assert len(runner.calls) == 1


@pytest.mark.parametrize("kind", ("dispatch", "batch"))
def test_foreign_root_and_damaged_seals_cannot_authorize_input_correction(tmp_path, kind):
    adapter, runner = _adapter(tmp_path, _output(kind))
    request = _request(adapter, kind)
    invoke = adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch
    invoke(request)
    with pytest.raises(IdeaSkillUnavailable) as wrong_root:
        invoke(replace(_changed(request, kind), root_session_ref="another-root"))
    assert wrong_root.value.code == "codex_operation_identity_conflict"
    assert wrong_root.value.recovery_checkpoint is None
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    invocation = operation / "invocation.json"
    envelope = json.loads(invocation.read_text())
    envelope["seal"] = "0" * 64
    invocation.write_text(json.dumps(envelope))
    with pytest.raises(IdeaSkillUnavailable) as damaged:
        invoke(_changed(request, kind))
    assert damaged.value.code == "codex_operation_identity_conflict"
    assert damaged.value.recovery_checkpoint is None
    assert len(runner.calls) == 1


def test_input_correction_creates_new_attempt_and_operation_with_same_native_session(tmp_path, monkeypatch):
    runtime = _current_bundle_runtime(tmp_path / "owner")
    try:
        _graph, _target, original, _dispatch, _launch = _ready_launch(runtime)
        provider_path = tmp_path / "adapter"
        provider_path.mkdir()
        adapter, runner = _adapter(provider_path, _output("dispatch"))
        # Use actual admitted Owner identities while retaining deterministic
        # provider output and the production durable invocation/correction path.
        monkeypatch.setattr(adapter, "runtime_binding", lambda: original.runtime_binding)
        original_call = runner.__call__
        def run_job(job_ref, argv, prompt, timeout, environment=None):
            result = original_call(argv, prompt, timeout, environment)
            return replace_completed_stdout(result, original.native_session_ref)
        monkeypatch.setattr(runner, "run_job", run_job)
        def invoke(**arguments):
            return adapter._invoke(**{
                key: arguments[key] for key in (
                    "operation_name", "prompt", "schema", "native_session_ref", "job_ref"
                )
            }, mcp_url="http://127.0.0.1:8767/mcp", mcp_token="test-resident-token", mcp_scope_binding_hash="9" * 64)
        monkeypatch.setattr(adapter, "_invoke_with_resident_mcp", invoke)
        request = replace(
            _request(adapter, "dispatch"), stage_request_ref=original.request_ref,
            run_ref=original.run_ref, attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref, root_session_ref=original.root_session_ref,
            native_session_ref=original.native_session_ref, job_ref=original.review_invocation.operation_ref,
            inbox_checkpoint=_inbox_checkpoint(run_ref=original.run_ref, attempt_ref=original.attempt_ref, fence_ref=original.fence_ref),
        )
        adapter.schedule_target(request)
        with pytest.raises(IdeaSkillUnavailable) as failed:
            adapter.schedule_target(_changed(request, "dispatch"))
        owner = runtime.owners.agent_runtime
        unit_ref = "bundle-input-cut-recovery"
        owner.begin_provider_unit(
            unit_ref=unit_ref, operation_ref=original.review_invocation.operation_ref,
            run_ref=original.run_ref, attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref, unit_kind="bundle_review",
        )
        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref, run_ref=original.run_ref,
            attempt_ref=original.attempt_ref, fence_ref=original.fence_ref,
            failure_code=failed.value.code, provider_exit=failed.value.recovery_checkpoint,
        )
        successor = owner.query_bundle_stage_run(original.request_ref)
        assert successor.attempt_ref != original.attempt_ref
        assert successor.fence_ref != original.fence_ref
        assert successor.review_invocation.operation_ref != original.review_invocation.operation_ref
        assert successor.root_session_ref == original.root_session_ref
        assert successor.native_session_ref == original.native_session_ref
        feedback = owner.query_stage_provider_correction_feedback(
            run_ref=successor.run_ref, attempt_ref=successor.attempt_ref, fence_ref=successor.fence_ref,
        )
        assert feedback["detail_code"] == "bundle_operation_inputs_changed"
        assert feedback["provider_unit_ref"] == unit_ref
        assert len(runner.calls) == 1
    finally:
        runtime.close()


def replace_completed_stdout(result, session):
    import subprocess
    return subprocess.CompletedProcess(result.args, result.returncode,
        stdout=result.stdout.replace("codex-bundle-primary:1", session), stderr=result.stderr)
