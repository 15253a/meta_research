from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root

from test_idea_stage_recovery import _IdeaProvider, _confirm_question, _runtime


_SHARED_CHILD_TRACE_FAILURES = (
    "codex_child_review_spawn_invalid",
    "codex_child_review_ref_mismatch",
    "codex_child_review_task_mismatch",
    "codex_child_review_wait_invalid",
    "codex_child_review_result_missing",
)
_TERMINAL_SHA256_FIELDS = (
    "invocation_hash",
    "prompt_hash",
    "output_schema_hash",
    "stdout_hash",
    "result_file_hash",
    "supervisor_receipt_hash",
)


def _provider_evidence(
    *,
    failure_code: str,
    detail_code: str | None = None,
    terminal_contract: bool = True,
    result_file_hash: str | None = None,
) -> dict[str, object]:
    reason = failure_code
    evidence: dict[str, object] = {
        "schema_ref": (
            "meta-research/provider-terminal-contract-failure/v1"
            if terminal_contract
            else "meta-research/provider-hard-ceiling/v1"
        ),
        "termination_reason": "completed" if terminal_contract else "timeout",
        "invocation_hash": canonical_hash({"invocation": reason}),
        "prompt_hash": canonical_hash({"prompt": reason}),
        "output_schema_hash": canonical_hash({"schema": reason}),
        "stdout_hash": canonical_hash({"stdout": reason}),
        "result_file_hash": (
            canonical_hash({"result": reason})
            if result_file_hash is None and terminal_contract
            else result_file_hash
        ),
        "supervisor_receipt_hash": canonical_hash({"receipt": reason}),
    }
    if terminal_contract:
        evidence["contract_failure_code"] = failure_code
        evidence["contract_failure_detail_code"] = detail_code or failure_code
    return evidence


def _active_stage_unit(tmp_path: Path, *, stage: str, phase: str):
    runtime = _runtime(
        prepare_data_root(tmp_path / f"{stage}-{phase}"),
        _IdeaProvider(),
    )
    completed = _confirm_question(runtime)
    runtime.idea_stage.start(f"{stage}-{phase}-start")
    request = runtime.owners.advancement_engine.query_idea_stage_request(
        completed["cycle_ref"]
    )
    assert request is not None
    run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
    assert run is not None
    if stage != "idea":
        # The test fixture changes only the formal Stage label. The public Owner
        # seam below remains responsible for resolving the actual persisted
        # provider unit kind and rejecting caller-supplied failure authority.
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET run_kind = :run_kind WHERE "
                    "run_ref = :run_ref"
                ),
                {"run_kind": f"{stage}_stage", "run_ref": run.run_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_stage_runs SET stage = :stage WHERE run_ref = "
                    ":run_ref"
                ),
                {"stage": stage, "run_ref": run.run_ref},
            )
    invocation = (
        run.primary_invocation if phase == "primary" else run.review_invocation
    )
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=invocation.invocation_ref,
        operation_ref=invocation.operation_ref,
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref,
        unit_kind=f"{stage}_{phase}",
    )
    return runtime, run, invocation.invocation_ref


@pytest.mark.parametrize("stage", ("idea", "plan", "bundle", "reasoning"))
@pytest.mark.parametrize("phase", ("primary", "review"))
def test_owner_accepts_only_the_stage_phase_result_contract_code(
    tmp_path: Path,
    stage: str,
    phase: str,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage=stage, phase=phase
    )
    failure_code = f"{stage}_{phase}_result_contract_invalid"
    detail_code = f"{stage}_{phase}_validator_contract_invalid"
    try:
        runtime.owners.agent_runtime.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=failure_code,
            provider_exit=_provider_evidence(
                failure_code=failure_code,
                detail_code=detail_code,
            ),
        )

        managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert managed is not None
        assert managed["status"] == "suspended_fenced"
        assert managed["terminal_reason"] == failure_code
        assert managed["safe_point"]["checkpoint"]["provider_exit"] == (
            _provider_evidence(
                failure_code=failure_code,
                detail_code=detail_code,
            )
        )
    finally:
        runtime.close()


def test_owner_accepts_primary_phase_and_review_child_trace_contract_codes(
    tmp_path: Path,
) -> None:
    accepted = (
        ("idea", "primary", "codex_primary_review_phase_invalid"),
        *(
            ("idea", "review", code)
            for code in _SHARED_CHILD_TRACE_FAILURES
        ),
        ("bundle", "review", "codex_child_review_trace_invalid"),
    )
    for index, (stage, phase, failure_code) in enumerate(accepted):
        runtime, run, unit_ref = _active_stage_unit(
            tmp_path / str(index), stage=stage, phase=phase
        )
        try:
            runtime.owners.agent_runtime.record_stage_provider_hard_ceiling(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=failure_code,
                provider_exit=_provider_evidence(failure_code=failure_code),
            )
            managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
            assert managed is not None
            assert managed["terminal_reason"] == failure_code
        finally:
            runtime.close()


@pytest.mark.parametrize(
    "failure_code",
    (
        "invented_terminal_contract_failure",
        "plan_primary_result_contract_invalid",
        "idea_review_result_contract_invalid",
    ),
)
def test_rejected_code_does_not_mutate_the_active_unit_or_fence(
    tmp_path: Path,
    failure_code: str,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage="idea", phase="primary"
    )
    owner = runtime.owners.agent_runtime
    before = owner.query_managed_run(run.run_ref)
    assert before is not None
    try:
        with pytest.raises(
            OwnerConflict, match="stage_provider_hard_ceiling_invalid"
        ):
            owner.record_stage_provider_hard_ceiling(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=failure_code,
                provider_exit=_provider_evidence(failure_code=failure_code),
            )
        assert owner.query_managed_run(run.run_ref) == before

        # A valid retry against the same public identities proves the rejected
        # transaction left both the provider unit active and the fence current.
        valid_code = "idea_primary_result_contract_invalid"
        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=valid_code,
            provider_exit=_provider_evidence(failure_code=valid_code),
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("invalid_hash_field", _TERMINAL_SHA256_FIELDS)
def test_completed_terminal_contract_rejects_non_sha256_evidence_without_mutation(
    tmp_path: Path,
    invalid_hash_field: str,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage="reasoning", phase="review"
    )
    owner = runtime.owners.agent_runtime
    failure_code = "reasoning_review_result_contract_invalid"
    before = owner.query_managed_run(run.run_ref)
    assert before is not None
    evidence = _provider_evidence(failure_code=failure_code)
    evidence[invalid_hash_field] = "z" * 64
    try:
        with pytest.raises(
            OwnerConflict, match="stage_provider_hard_ceiling_invalid"
        ):
            owner.record_stage_provider_hard_ceiling(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=failure_code,
                provider_exit=evidence,
            )
        assert owner.query_managed_run(run.run_ref) == before

        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=failure_code,
            provider_exit=_provider_evidence(failure_code=failure_code),
        )
    finally:
        runtime.close()


def test_completed_terminal_contract_requires_a_result_file_hash(
    tmp_path: Path,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage="reasoning", phase="review"
    )
    owner = runtime.owners.agent_runtime
    failure_code = "reasoning_review_result_contract_invalid"
    before = owner.query_managed_run(run.run_ref)
    assert before is not None
    evidence = _provider_evidence(failure_code=failure_code)
    evidence["result_file_hash"] = None
    try:
        with pytest.raises(
            OwnerConflict, match="stage_provider_hard_ceiling_invalid"
        ):
            owner.record_stage_provider_hard_ceiling(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=failure_code,
                provider_exit=evidence,
            )
        assert owner.query_managed_run(run.run_ref) == before

        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=failure_code,
            provider_exit=_provider_evidence(failure_code=failure_code),
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "invalid_detail_code",
    (None, "", "Review_Failure", "review-failure", "1review_failure", "a" * 97),
)
def test_terminal_contract_rejects_invalid_detail_without_mutation(
    tmp_path: Path,
    invalid_detail_code: str | None,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage="plan", phase="review"
    )
    owner = runtime.owners.agent_runtime
    failure_code = "plan_review_result_contract_invalid"
    before = owner.query_managed_run(run.run_ref)
    assert before is not None
    evidence = _provider_evidence(failure_code=failure_code)
    if invalid_detail_code is None:
        evidence.pop("contract_failure_detail_code")
    else:
        evidence["contract_failure_detail_code"] = invalid_detail_code
    try:
        with pytest.raises(
            OwnerConflict, match="stage_provider_hard_ceiling_invalid"
        ):
            owner.record_stage_provider_hard_ceiling(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code=failure_code,
                provider_exit=evidence,
            )
        assert owner.query_managed_run(run.run_ref) == before

        owner.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=failure_code,
            provider_exit=_provider_evidence(
                failure_code=failure_code,
                detail_code="plan_review_revision_not_material",
            ),
        )
    finally:
        runtime.close()


def test_legacy_hard_ceiling_still_accepts_a_missing_result_file(
    tmp_path: Path,
) -> None:
    runtime, run, unit_ref = _active_stage_unit(
        tmp_path, stage="plan", phase="primary"
    )
    try:
        runtime.owners.agent_runtime.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code="codex_operation_timeout",
            provider_exit=_provider_evidence(
                failure_code="codex_operation_timeout",
                terminal_contract=False,
                result_file_hash=None,
            ),
        )
        managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert managed is not None
        assert managed["terminal_reason"] == "codex_operation_timeout"
    finally:
        runtime.close()
