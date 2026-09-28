from dataclasses import replace
import json

import pytest
from sqlalchemy import text

import meta_research.owners.research_memory as memory_module
import meta_research.target_run_finalizer as finalizer_module
from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _resume(runtime, key):
    owner = runtime.owners.advancement_engine
    foreground = owner.query_active_foregrounds()[0]
    quest = foreground["quest_ref"]
    human = runtime.owners.human_collaboration
    for action in ("pause", "resume"):
        current = owner.query_foreground(quest)
        command = _confirmed_control(human, scope_ref=f"quest:{quest}", payload={
            "action": action, "reason": "operator_requested",
            "target": {name: current[name] for name in (
                "quest_ref", "cycle_ref", "question_ref", "epoch")},
        }, key=f"{key}-{action}")
        result = _execute_control(human, command, f"{key}-{action}")
        assert result["control_execution"]["status"] == "completed"


@pytest.mark.parametrize("storage_recovers", [True, False])
def test_formal_resume_retries_same_exhausted_job_only_once(tmp_path, monkeypatch, storage_recovers):
    runtime, lifecycle, _memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    memory = runtime.owners.research_memory
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle, memory=runtime.target_run_finalizer._memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    monkeypatch.setattr(finalizer_module, "TARGET_ROOT_INLINE_ARTIFACT_BYTES", 1)
    monkeypatch.setattr(memory_module, "ASSET_INTAKE_MAX_ATTEMPTS", 1)
    original_store = memory._store_asset_file
    original_submit = memory.submit_asset_intake
    requests = {}

    def remember(request, **values):
        requests[values["idempotency_key"]] = request
        return original_submit(request, **values)

    monkeypatch.setattr(memory, "submit_asset_intake", remember)

    def quota(*args, **kwargs):
        raise OSError(122, "fixture quota exhausted")

    monkeypatch.setattr(memory, "_store_asset_file", quota)
    try:
        with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
            finalizer.finalize(handle=handle, evidence=evidence)
        with runtime._database.read() as connection:
            failed = connection.execute(text("SELECT * FROM rm_asset_intakes WHERE status='failed'")).one()
        assert failed.failure_code == "asset_intake_retry_exhausted"
        assert failed.request_payload_scrubbed == 1
        assert failed.attempt_count == 1
        completion = lifecycle.query_completion(handle.target_ref)
        with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert memory.query_asset_intake(failed.job_ref).attempt_count == 1
        with pytest.raises(OwnerConflict, match="asset_intake_idempotency_conflict"):
            memory.retry_asset_intake_after_resume(
                replace(requests[failed.idempotency_key], display_name="different-request"),
                idempotency_key=failed.idempotency_key,
            )
        _resume(runtime, "storage-recovery")
        owner = runtime.owners.advancement_engine
        authority = runtime.owners.research_graph.query_target_measurement_domain_authority(handle.target_ref)
        original_query = owner._query_stage_request_by_ref
        stage_request = original_query(authority.stage_request_ref)
        for changed in (replace(stage_request, epoch=stage_request.epoch + 1),
                        replace(stage_request, cycle_ref="other-cycle")):
            with monkeypatch.context() as context:
                context.setattr(owner, "_query_stage_request_by_ref", lambda _ref: changed)
                assert owner.query_completed_resume_after(
                    stage_request_ref=authority.stage_request_ref, failed_at=failed.completed_at,
                ) is None
        with monkeypatch.context() as context:
            context.setattr(owner, "_query_stage_request_by_ref", lambda _ref: replace(stage_request, stage="reasoning"))
            with pytest.raises(OwnerConflict, match="asset_intake_recovery_scope_invalid"):
                owner.query_completed_resume_after(
                    stage_request_ref=authority.stage_request_ref, failed_at=failed.completed_at,
                )
        for field in ("completion_ref", "target_run_ref"):
            document = memory_module._asset_request_document(requests[failed.idempotency_key])
            document["provenance"] = {**document["provenance"], field: "different-identity"}
            with pytest.raises(OwnerConflict):
                runtime.target_run_finalizer._memory.query_asset_intake_recovery(
                    document, failed_at=failed.completed_at,
                )
        if storage_recovers:
            monkeypatch.setattr(memory, "_store_asset_file", original_store)
            result = finalizer.finalize(handle=handle, evidence=evidence)
            assert result.status == "rm_accepted"
            assert result.completion_ref == completion.completion_ref
        else:
            with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
                finalizer.finalize(handle=handle, evidence=evidence)
            with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
                finalizer.finalize(handle=handle, evidence=evidence)
            assert lifecycle.query_completion_rejection(completion.completion_ref) is None
            assert list((workspace.parent / ".target-completion-intakes").glob("*/artifact-*"))
        current = memory.query_asset_intake(failed.job_ref)
        assert current.attempt_count == 2
        assert current.status == ("accepted" if storage_recovers else "failed")
        with runtime._database.read() as connection:
            row = connection.execute(text("SELECT * FROM rm_asset_intakes WHERE job_ref=:job"), {"job": failed.job_ref}).one()
            events = [json.loads(value) for value in connection.execute(text(
                "SELECT payload_json FROM durable_feed WHERE event_type='research_memory.asset_intake_recovery_authorized'"
            )).scalars()]
            failures = [json.loads(value) for value in connection.execute(text(
                "SELECT payload_json FROM durable_feed WHERE event_type='research_memory.asset_intake_failed'"
            )).scalars()]
        assert row.request_hash == failed.request_hash
        assert row.idempotency_key == failed.idempotency_key
        assert len(events) == 1
        assert events[0]["previous_attempt_count"] == 1
        assert events[0]["recovery"]["completion_ref"] == completion.completion_ref
        assert events[0]["recovery"]["receipt"]["issuer"] == "advancement_engine"
        assert owner.query_completed_resume_after(
            stage_request_ref=authority.stage_request_ref,
            failed_at=events[0]["recovery"]["completed_at"],
        ) is None
        assert failures[-1]["errno"] == 122
        assert failures[-1]["phase"] == "prepare_asset"
    finally:
        runtime.close()
