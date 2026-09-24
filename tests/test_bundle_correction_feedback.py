from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from test_public_bundle_stage import (
    _confirm_direct_quest, _finish_idea_stage, _finish_plan_stage,
)
from test_stage_terminal_contract_authority import _provider_evidence
from test_target_root_finalizer import _current_bundle_runtime


def test_owner_correction_reaches_successor_provider_prompt_with_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _current_bundle_runtime(tmp_path / "bundle-feedback")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        # Stop at the actual admitted primary invocation, before provider work.
        original = None
        for _ in range(5):
            assert runtime.bundle_stage.process_once()
            projection = runtime.bundle_stage.query_current()["stage_run_request"]
            if projection is not None:
                original = runtime.owners.agent_runtime.query_bundle_stage_run(
                    projection["request_ref"]
                )
                if original is not None:
                    break
        assert original is not None and original.primary_draft is None
        owner = runtime.owners.agent_runtime
        assert owner.query_stage_provider_correction_feedback(
            run_ref=original.run_ref, attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref,
        ) is None

        unit_ref = "bundle-primary-rejected-measurement-cell"
        owner.begin_provider_unit(
            unit_ref=unit_ref, operation_ref=original.primary_invocation.operation_ref,
            run_ref=original.run_ref, attempt_ref=original.attempt_ref,
            fence_ref=original.fence_ref, unit_kind="bundle_primary",
        )
        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref, run_ref=original.run_ref,
            attempt_ref=original.attempt_ref, fence_ref=original.fence_ref,
            failure_code="bundle_primary_result_contract_invalid",
            provider_exit=_provider_evidence(
                failure_code="bundle_primary_result_contract_invalid",
                detail_code="candidate_measurement_cell_not_required",
            ),
        )
        successor = owner.query_bundle_stage_run(original.request_ref)
        assert successor is not None
        assert successor.attempt_ref != original.attempt_ref
        assert successor.fence_ref != original.fence_ref
        assert successor.root_session_ref == original.root_session_ref
        assert successor.primary_invocation.operation_ref != original.primary_invocation.operation_ref
        feedback = owner.query_stage_provider_correction_feedback(
            run_ref=successor.run_ref, attempt_ref=successor.attempt_ref,
            fence_ref=successor.fence_ref,
        )
        assert feedback is not None
        assert feedback["detail_code"] == "candidate_measurement_cell_not_required"
        assert feedback["provider_unit_ref"] == unit_ref
        assert feedback["provider_operation_ref"] == original.primary_invocation.operation_ref
        assert feedback["rejected_attempt_ref"] == original.attempt_ref
        assert "operation_name" not in feedback
        for stale_attempt, stale_fence in (
            (original.attempt_ref, original.fence_ref),
            (successor.attempt_ref, original.fence_ref),
        ):
            assert owner.query_stage_provider_correction_feedback(
                run_ref=successor.run_ref, attempt_ref=stale_attempt,
                fence_ref=stale_fence,
            ) is None

        # Keep fixture output deterministic while using the real coordinator
        # request and production adapter prompt builder at the provider seam.
        provider = runtime.bundle_stage._provider
        original_generate = provider.generate_draft
        adapter = CodexBundleSkillAdapter(tmp_path / "provider-prompt")
        monkeypatch.setattr(adapter, "runtime_binding", provider.runtime_binding)
        captured = []

        def generate(request):
            draft = original_generate(request)
            assert request.attempt_ref == successor.attempt_ref
            assert request.provider_feedback == feedback

            def invoke(**invocation):
                captured.append(invocation)
                return {"target_plan": draft.draft}, draft.primary_session_ref, ""

            monkeypatch.setattr(adapter, "_invoke_with_resident_mcp", invoke)
            return adapter.generate_draft(request)

        monkeypatch.setattr(provider, "generate_draft", generate)
        assert runtime.bundle_stage.process_once()
        assert len(captured) == 1
        invocation = captured[0]
        assert invocation["attempt_ref"] == successor.attempt_ref
        assert invocation["fence_ref"] == successor.fence_ref
        assert invocation["job_ref"] == successor.primary_invocation.operation_ref
        assert invocation["operation_name"] == "primary"
        assert "provider_correction_feedback=" + canonical_json(feedback) in invocation["prompt"]
        assert "candidate_measurement_cell_not_required" in invocation["prompt"]

        # Hash-valid but incorrectly rebound metadata is not source authority.
        with runtime._database.write() as connection:
            row = connection.execute(text(
                "SELECT checkpoint_json FROM ar_safe_points WHERE safe_point_ref=:ref"
            ), {"ref": feedback["safe_point_ref"]}).one()
            checkpoint = json.loads(row.checkpoint_json)
            checkpoint["root_session_ref"] = "another-root-session"
            connection.execute(text(
                "UPDATE ar_safe_points SET checkpoint_json=:payload, checkpoint_hash=:hash "
                "WHERE safe_point_ref=:ref"
            ), {
                "payload": canonical_json(checkpoint), "hash": canonical_hash(checkpoint),
                "ref": feedback["safe_point_ref"],
            })
        with pytest.raises(OwnerConflict, match="stage_provider_feedback_invalid"):
            owner.query_stage_provider_correction_feedback(
                run_ref=successor.run_ref, attempt_ref=successor.attempt_ref,
                fence_ref=successor.fence_ref,
            )
    finally:
        runtime.close()
