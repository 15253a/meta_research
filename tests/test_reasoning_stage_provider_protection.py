from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillUnavailable,
)

from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _confirm_deepfetch_quest,
    _finish_idea_stage,
    _reasoning_runtime,
    _tick_reasoning,
)
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill,
    _reach_autonomous_checkpoint,
)


class _CountingReasoningSkill(_DeterministicReasoningSkill):
    def __init__(self) -> None:
        super().__init__()
        self.generate_calls = 0
        self.review_calls = 0

    def generate_draft(self, request: ReasoningSkillRequest):
        self.generate_calls += 1
        return super().generate_draft(request)

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ):
        self.review_calls += 1
        return super().review_draft(request, draft)


class _CountingAutonomousReasoningSkill(_AutonomousReasoningSkill):
    def __init__(self) -> None:
        super().__init__()
        self.resume_calls = 0

    def resume_after_autonomous_creation(
        self,
        request,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ):
        self.resume_calls += 1
        return super().resume_after_autonomous_creation(
            request,
            checkpoint,
            creation_result,
        )


def _signed_ceiling_evidence(reason: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/provider-hard-ceiling/v1",
        "termination_reason": reason,
        "invocation_hash": canonical_hash({"invocation": reason}),
        "prompt_hash": canonical_hash({"prompt": reason}),
        "output_schema_hash": canonical_hash({"schema": reason}),
        "stdout_hash": canonical_hash({"stdout": reason}),
        "result_file_hash": canonical_hash({"result": reason}),
        "supervisor_receipt_hash": canonical_hash({"receipt": reason}),
    }


class _CeilingReasoningSkill(_CountingReasoningSkill):
    def generate_draft(self, request: ReasoningSkillRequest):
        del request
        self.generate_calls += 1
        raise ReasoningSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


class _ReviewCeilingReasoningSkill(_CountingReasoningSkill):
    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ):
        del request, draft
        self.review_calls += 1
        raise ReasoningSkillUnavailable(
            "codex_operation_output_limit",
            recovery_checkpoint=_signed_ceiling_evidence("output_limit"),
        )


class _AutonomousCeilingReasoningSkill(_CountingAutonomousReasoningSkill):
    def resume_after_autonomous_creation(
        self,
        request,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ):
        del request, checkpoint, creation_result
        self.resume_calls += 1
        raise ReasoningSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


def _reach_reasoning_run(runtime) -> dict[str, object]:
    _confirm_deepfetch_quest(runtime)
    _finish_idea_stage(runtime)
    requested = _tick_reasoning(runtime)
    assert requested["run"] is None
    admitted = _tick_reasoning(runtime)
    assert admitted["run"] is not None
    return admitted


def test_reasoning_primary_waits_on_typed_provider_protection_before_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CountingReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-primary-provider-protection",
        reasoning_skill=provider,
    )
    try:
        _reach_reasoning_run(runtime)

        def blocked_provider_unit(**_values) -> None:
            raise OwnerConflict("power_inhibitor_acquisition_failed")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "begin_provider_unit",
            blocked_provider_unit,
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == (
            "power_inhibitor_acquisition_failed"
        )
        assert provider.generate_calls == 0
        assert provider.review_calls == 0
    finally:
        runtime.close()


def test_reasoning_review_waits_on_typed_provider_protection_before_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CountingReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-review-provider-protection",
        reasoning_skill=provider,
    )
    try:
        _reach_reasoning_run(runtime)
        primary = _tick_reasoning(runtime)
        assert primary["run"]["primary_draft_checkpoint"]["status"] == "recorded"
        assert provider.generate_calls == 1
        assert provider.review_calls == 0

        def blocked_provider_unit(**_values) -> None:
            raise OwnerConflict("power_inhibitor_acquisition_failed")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "begin_provider_unit",
            blocked_provider_unit,
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == (
            "power_inhibitor_acquisition_failed"
        )
        assert provider.generate_calls == 1
        assert provider.review_calls == 0
    finally:
        runtime.close()


def test_reasoning_autonomous_resume_waits_on_typed_protection_before_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CountingAutonomousReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-autonomous-resume-provider-protection",
        reasoning_skill=provider,
    )
    try:
        _quest, _view, checkpoint = _reach_autonomous_checkpoint(runtime)
        checkpoint_ref = str(checkpoint["checkpoint_ref"])

        def ready_creation(queried_ref: str):
            assert queried_ref == checkpoint_ref
            return {
                "status": "ready_for_reasoning_resume",
                "checkpoint": {"ref": checkpoint_ref},
            }

        monkeypatch.setattr(runtime.autonomous_creation, "query", ready_creation)

        def blocked_provider_unit(**_values) -> None:
            raise OwnerConflict("power_inhibitor_acquisition_failed")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "begin_provider_unit",
            blocked_provider_unit,
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == (
            "power_inhibitor_acquisition_failed"
        )
        assert provider.resume_calls == 0
    finally:
        runtime.close()


def test_reasoning_primary_records_signed_hard_ceiling_without_safe_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _CeilingReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-primary-hard-ceiling",
        reasoning_skill=provider,
    )
    try:
        current = _reach_reasoning_run(runtime)
        request_ref = str(current["stage_run_request"]["request_ref"])
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert run is not None
        recorded: list[dict[str, object]] = []
        safe_acks: list[dict[str, object]] = []
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "record_stage_provider_hard_ceiling",
            lambda **values: recorded.append(values),
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lambda **values: safe_acks.append(values),
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == "codex_operation_timeout"
        assert provider.generate_calls == 1
        assert provider.review_calls == 0
        assert safe_acks == []
        assert recorded == [
            {
                "unit_ref": run.primary_invocation.invocation_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
                "failure_code": "codex_operation_timeout",
                "provider_exit": _signed_ceiling_evidence("timeout"),
            }
        ]
    finally:
        runtime.close()


def test_reasoning_review_records_signed_hard_ceiling_without_safe_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ReviewCeilingReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-review-hard-ceiling",
        reasoning_skill=provider,
    )
    try:
        _reach_reasoning_run(runtime)
        primary = _tick_reasoning(runtime)
        request_ref = str(primary["stage_run_request"]["request_ref"])
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert run is not None
        recorded: list[dict[str, object]] = []
        safe_acks: list[dict[str, object]] = []
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "record_stage_provider_hard_ceiling",
            lambda **values: recorded.append(values),
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lambda **values: safe_acks.append(values),
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == (
            "codex_operation_output_limit"
        )
        assert provider.generate_calls == 1
        assert provider.review_calls == 1
        assert safe_acks == []
        assert recorded == [
            {
                "unit_ref": run.review_invocation.invocation_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
                "failure_code": "codex_operation_output_limit",
                "provider_exit": _signed_ceiling_evidence("output_limit"),
            }
        ]
    finally:
        runtime.close()


def test_reasoning_autonomous_resume_records_hard_ceiling_without_safe_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AutonomousCeilingReasoningSkill()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-autonomous-resume-hard-ceiling",
        reasoning_skill=provider,
    )
    try:
        _quest, current, checkpoint = _reach_autonomous_checkpoint(runtime)
        request_ref = str(current["stage_run_request"]["request_ref"])
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(request_ref)
        assert run is not None
        checkpoint_ref = str(checkpoint["checkpoint_ref"])

        def ready_creation(queried_ref: str):
            assert queried_ref == checkpoint_ref
            return {
                "status": "ready_for_reasoning_resume",
                "checkpoint": {"ref": checkpoint_ref},
            }

        monkeypatch.setattr(runtime.autonomous_creation, "query", ready_creation)
        recorded: list[dict[str, object]] = []
        safe_acks: list[dict[str, object]] = []
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "record_stage_provider_hard_ceiling",
            lambda **values: recorded.append(values),
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "acknowledge_provider_safe_point",
            lambda **values: safe_acks.append(values),
        )

        assert not runtime.reasoning_stage.process_once()
        assert runtime.reasoning_stage.transient_error == "codex_operation_timeout"
        assert provider.resume_calls == 1
        assert safe_acks == []
        assert recorded == [
            {
                "unit_ref": run.review_invocation.invocation_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
                "failure_code": "codex_operation_timeout",
                "provider_exit": _signed_ceiling_evidence("timeout"),
            }
        ]
    finally:
        runtime.close()
