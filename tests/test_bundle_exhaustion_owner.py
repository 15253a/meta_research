from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    BUNDLE_EXHAUSTION_BASIS_KIND,
    BundleExhaustionReviewTrace,
    bundle_exhaustion_review_response_document,
    bundle_exhaustion_review_task_hash,
    bundle_exhaustion_route_fingerprint,
)
from meta_research.bundle_protocol import RouteSpec
from meta_research.bundle_skill import (
    BundleExhaustionSkillResult,
    BundleSkillDraft,
    BundleSkillRequest,
    BundleSkillResult,
)
from meta_research.bundle_target_contract import (
    build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _bundle_runtime,
    _prepare_bundle_request,
)
from test_target_run_owner import (
    _records as _target_records,
    _runtime as _target_owner_runtime,
    _seed_admitted_launch,
)


class _ExhaustionBundleSkill(_DeterministicBundleSkill):
    """Deterministic fixed-contract Bundle root used through process_once."""

    def __init__(
        self,
        *,
        corrupt_trace_seal: bool = False,
        claim_fake_rejection: bool = False,
        claim_fake_operation: bool = False,
    ) -> None:
        self._corrupt_trace_seal = corrupt_trace_seal
        self._claim_fake_rejection = claim_fake_rejection
        self._claim_fake_operation = claim_fake_operation

    def _assessment(self, request: BundleSkillRequest) -> dict[str, object]:
        briefs = cast(
            list[dict[str, object]], request.plan_document["experiment_briefs"]
        )
        normalization = []
        records = []
        predecessor_submission_refs = tuple(
            cast(str, item["submission_ref"])
            for item in request.predecessor_rejections
        )
        for ordinal, brief in enumerate(briefs, start=1):
            experiment_key = cast(str, brief["experiment_key"])
            cell = f"measurement:{experiment_key}"
            route_ref = f"route:semantic:{experiment_key}"
            normalization.append(
                {
                    "experiment_key": experiment_key,
                    "held_fixed_slots": [],
                    "required_measurement_unit_keys": [cell],
                }
            )
            outcome = (
                "attempted_rejected"
                if (
                    ordinal == 1
                    and (self._claim_fake_rejection or predecessor_submission_refs)
                )
                else "semantically_ineligible"
            )
            evidence_refs = (
                ["submission:not-issued-by-ar"]
                if self._claim_fake_rejection and ordinal == 1
                else list(predecessor_submission_refs)
                if predecessor_submission_refs and ordinal == 1
                else [f"semantic-evidence:{experiment_key}"]
            )
            route = RouteSpec(
                route_ref=route_ref,
                known_external_operation_refs=(
                    ("target-operation:not-issued",)
                    if self._claim_fake_operation and ordinal == 1
                    else ()
                ),
            )
            external_reconciliations = (
                [
                    {
                        "operation_ref": route.known_external_operation_refs[0],
                        "receipt": {
                            "receipt_ref": "target-exit:not-issued",
                            "subject_ref": route.known_external_operation_refs[0],
                            "verified": True,
                            "currentness_known": True,
                            "current": True,
                        },
                        "outcome": "rejected",
                    }
                ]
                if route.known_external_operation_refs
                else []
            )
            records.append(
                {
                    "record_ref": f"exploration:{ordinal:04d}",
                    "experiment_key": experiment_key,
                    "measurement_unit_key": cell,
                    "held_fixed_bindings": [],
                    "route": {
                        "route_ref": route_ref,
                        "known_external_operation_refs": list(
                            route.known_external_operation_refs
                        ),
                    },
                    "route_disposition": {
                        "disposition_ref": f"route-disposition:{ordinal:04d}",
                        "route_ref": route_ref,
                        "experiment_keys": [experiment_key],
                        "outcome": outcome,
                        "required_changes": [],
                        "evidence_refs": evidence_refs,
                        "external_reconciliations": external_reconciliations,
                    },
                    "frozen_semantic_fingerprint": bundle_exhaustion_route_fingerprint(
                        formal_plan_content_hash=canonical_hash(
                            request.plan_document
                        ),
                        experiment_key=experiment_key,
                        measurement_unit_key=cell,
                        held_fixed_bindings=(),
                        route=route,
                    ),
                }
            )
        completion = build_normalized_completion_contract(
            request.plan_document, tuple(normalization)
        )
        return {
            "exhaustion_assessment": {
                "schema_ref": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
                "completion_contract": normalized_completion_contract_to_dict(
                    completion
                ),
                "exploration_records": records,
            }
        }

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        return BundleSkillDraft(
            draft=self._assessment(request),
            primary_session_ref=(
                request.native_session_ref or "bundle-primary-session:exhaustion"
            ),
            adapter_kind="test_fixed_exhaustion",
            output_kind="exhaustion_assessment",
        )

    @staticmethod
    def _transport_seal(
        trace: BundleExhaustionReviewTrace, *, runtime_binding_hash: str
    ) -> str:
        return canonical_hash(
            {
                "schema_ref": "test/bundle-exhaustion-transport-seal/v1",
                "runtime_binding_hash": runtime_binding_hash,
                "trace": trace.unsigned_dict(),
            }
        )

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleExhaustionSkillResult:
        assessment_hash = canonical_hash(draft.draft)
        reviewer = "bundle-independent-reviewer:exhaustion"
        trace = BundleExhaustionReviewTrace(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            fence_ref=request.fence_ref,
            primary_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
            reviewed_assessment_hash=assessment_hash,
            review_task_hash=bundle_exhaustion_review_task_hash(
                reviewed_assessment_hash=assessment_hash,
                formal_plan_content_hash=canonical_hash(request.plan_document),
            ),
            review_response_hash=canonical_hash(
                bundle_exhaustion_review_response_document(
                    reviewer_agent_ref=reviewer,
                    reviewed_assessment_hash=assessment_hash,
                )
            ),
            spawn_event_hash=canonical_hash(
                {"event": "spawn", "reviewer": reviewer}
            ),
            completion_event_hash=canonical_hash(
                {"event": "completed", "reviewer": reviewer}
            ),
            transport_seal="0" * 64,
        )
        seal = self._transport_seal(
            trace,
            runtime_binding_hash=canonical_hash(request.runtime_binding.as_dict()),
        )
        trace = replace(
            trace,
            transport_seal=("f" * 64 if self._corrupt_trace_seal else seal),
        )
        return BundleExhaustionSkillResult(
            reviewed_assessment=draft.draft,
            reviewed_assessment_hash=assessment_hash,
            findings=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer,
            adapter_kind=draft.adapter_kind,
            review_trace=trace,
        )

    def verify_bundle_exhaustion_review_trace(
        self,
        trace: BundleExhaustionReviewTrace,
        *,
        runtime_binding_hash: str,
    ) -> None:
        expected = self._transport_seal(
            replace(trace, transport_seal="0" * 64),
            runtime_binding_hash=runtime_binding_hash,
        )
        if trace.transport_seal != expected:
            raise OwnerConflict("bundle_exhaustion_review_trace_seal_invalid")


class _RejectedThenExhaustionBundleSkill(_ExhaustionBundleSkill):
    """Submit once, consume the exact RG rejection, then assess exhaustion."""

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        if request.predecessor_rejections:
            return super().generate_draft(request)
        return _DeterministicBundleSkill.generate_draft(self, request)

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult | BundleExhaustionSkillResult:
        if draft.output_kind == "target_plan":
            return _DeterministicBundleSkill.review_draft(self, request, draft)
        return super().review_draft(request, draft)


class _RejectingTargetCandidateProofVerifier:
    def verify_reuse_source_receipt(self, **_values) -> None:
        raise OwnerConflict("reuse_source_version_receipt_invalid")


def _request(runtime):
    current = runtime.bundle_stage.query_current()
    request_value = current["stage_run_request"]
    assert isinstance(request_value, dict)
    request = runtime.owners.advancement_engine.query_bundle_stage_request(
        request_value["cycle_ref"]
    )
    assert request is not None
    return request


def _advance_to_evidence(runtime):
    _prepare_bundle_request(runtime)
    request = _request(runtime)
    for _step in range(8):
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        if run is not None:
            evidence = (
                runtime.owners.agent_runtime.query_bundle_exhaustion_evidence_for_run(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                )
            )
            if evidence is not None:
                return request, run, evidence
        assert runtime.bundle_stage.process_once()
    raise AssertionError("Bundle exhaustion evidence was not accepted")


def _advance_to_rg_rejection(runtime):
    _prepare_bundle_request(runtime)
    request = _request(runtime)
    for _step in range(10):
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        if run is not None and run.execution is not None:
            rejection = runtime.owners.research_graph.query_target_graph_rejection(
                run.execution.submission_ref
            )
            if rejection is not None:
                return request, run, rejection
        assert runtime.bundle_stage.process_once()
    raise AssertionError("RG did not durably reject the first TargetPlan")


def _advance_to_exhaustion_operation(runtime):
    request, _run, _evidence = _advance_to_evidence(runtime)
    for _step in range(6):
        operation = (
            runtime.owners.advancement_engine.query_bundle_exhaustion_for_request(
                request.request_ref
            )
        )
        if operation is not None:
            return request, operation
        assert runtime.bundle_stage.process_once()
    raise AssertionError("AE did not record an ExhaustionProposal decision")


def test_public_process_once_restarts_and_commits_exact_exhaustion_chain(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "public-exhaustion-restart"
    runtime = _bundle_runtime(
        data_root, bundle_skill_provider=_ExhaustionBundleSkill()
    )
    request, run, evidence = _advance_to_evidence(runtime)
    assert run.status == "running"
    runtime.close()

    restarted = _bundle_runtime(
        data_root, bundle_skill_provider=_ExhaustionBundleSkill()
    )
    try:
        verified = restarted.owners.agent_runtime.verify_bundle_exhaustion_evidence_receipt(
            evidence_ref=evidence.evidence_ref,
            evidence_hash=evidence.evidence.evidence_hash,
            receipt=evidence.receipt,
            phase="submission",
        )
        assert verified == evidence
        for _step in range(8):
            operation = (
                restarted.owners.advancement_engine.query_bundle_exhaustion_for_request(
                    request.request_ref
                )
            )
            if operation is not None and operation.status == "accepted":
                break
            assert restarted.bundle_stage.process_once()
        else:
            raise AssertionError("AE did not accept the ExhaustionProposal")

        pre_completion = restarted.bundle_stage.query_current()
        assert pre_completion["stage_commit"] is None
        owner_run = restarted.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert owner_run is not None and owner_run.status == "running"
        assert pre_completion["disposition"]["status"] == "accepted"
        assert pre_completion["disposition"]["report_disposition"] is None
        assert pre_completion["bundle_exhaustion"]["proposal_ref"] == (
            operation.accepted_proposal_ref
        )

        for _step in range(12):
            commit = restarted.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            if commit is not None:
                break
            assert restarted.bundle_stage.process_once()
        else:
            raise AssertionError("Bundle exhaustion did not reach StageCommit")

        operation = (
            restarted.owners.advancement_engine.query_bundle_exhaustion_for_request(
                request.request_ref
            )
        )
        completed = restarted.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert operation is not None and operation.status == "accepted"
        assert completed is not None and completed.status == "completed"
        assert completed.completion is not None
        assert commit.disposition == "exhausted"
        assert commit.basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND
        assert commit.basis_ref == operation.accepted_proposal_ref
        assert completed.completion.outcome_ref == operation.accepted_proposal_ref
    finally:
        restarted.close()


def test_real_rg_rejection_successor_restarts_then_commits_exhaustion(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "public-two-round-exhaustion"
    runtime = _bundle_runtime(
        data_root,
        bundle_skill_provider=_RejectedThenExhaustionBundleSkill(),
    )
    runtime.owners.research_graph._target_candidate_proof_verifier = (  # type: ignore[attr-defined]
        _RejectingTargetCandidateProofVerifier()
    )
    request, first_run, rejection = _advance_to_rg_rejection(runtime)
    assert first_run.execution is not None
    assert rejection.submission_ref == first_run.execution.submission_ref
    assert rejection.target_plan_hash == first_run.execution.material_outcome_hash
    runtime.close()

    restarted = _bundle_runtime(
        data_root,
        bundle_skill_provider=_RejectedThenExhaustionBundleSkill(),
    )
    try:
        for _step in range(24):
            commit = restarted.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            if commit is not None:
                break
            assert restarted.bundle_stage.process_once()
        else:
            raise AssertionError("two-round exhaustion did not reach StageCommit")

        run = restarted.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert run is not None and run.status == "completed"
        evidence = (
            restarted.owners.agent_runtime.query_bundle_exhaustion_evidence_for_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
            )
        )
        assert evidence is not None
        history = evidence.evidence.rejected_submissions
        assert len(history) == 1
        assert history[0].submission_ref == rejection.submission_ref
        assert history[0].rejection_receipt == rejection.receipt
        attempted = tuple(
            record
            for record in evidence.evidence.exploration_records
            if record.route_disposition.outcome == "attempted_rejected"
        )
        assert len(attempted) == 1
        assert attempted[0].route_disposition.evidence_refs == (
            rejection.submission_ref,
        )

        public = restarted.bundle_stage.query_current()
        assert public["disposition"]["status"] == "completed"
        assert public["disposition"]["report_disposition"] == "exhausted"
        assert public["bundle_exhaustion"]["kind"] == "BundleExhaustion"
        assert public["bundle_exhaustion"]["proposal_ref"] == commit.basis_ref
        assert public["bundle_exhaustion"]["basis_receipt"] == (
            commit.basis_receipt.as_public_dict()
        )
        assert public["stage_commit"]["outcome_kind"] == "BundleExhaustion"
        assert public["stage_commit"]["basis_ref"] == commit.basis_ref
        assert public["stage_commit"]["basis_receipt"] == (
            commit.basis_receipt.as_public_dict()
        )
    finally:
        restarted.close()


def test_tampered_rg_rejection_cannot_create_bundle_successor(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "tampered-target-graph-rejection"
    runtime = _bundle_runtime(
        data_root,
        bundle_skill_provider=_RejectedThenExhaustionBundleSkill(),
    )
    runtime.owners.research_graph._target_candidate_proof_verifier = (  # type: ignore[attr-defined]
        _RejectingTargetCandidateProofVerifier()
    )
    request, first_run, rejection = _advance_to_rg_rejection(runtime)
    runtime.close()

    with sqlite3.connect(data_root / "meta-research.sqlite3") as connection:
        connection.execute(
            "UPDATE rg_target_graph_rejections SET receipt_hash = ? WHERE "
            "rejection_ref = ?",
            ("0" * 64, rejection.rejection_ref),
        )
        connection.commit()

    restarted = _bundle_runtime(
        data_root,
        bundle_skill_provider=_RejectedThenExhaustionBundleSkill(),
    )
    try:
        with pytest.raises(
            OwnerConflict,
            match="target_graph_rejection_integrity_invalid",
        ):
            restarted.bundle_stage.process_once()
        current = restarted.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert current is not None
        assert current.attempt_ref == first_run.attempt_ref
        assert current.attempt_generation == first_run.attempt_generation
        assert current.execution is not None
        with restarted._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ar_bundle_exhaustion_evidence"
            ).scalar_one() == 0
    finally:
        restarted.close()


def test_target_root_inventory_fails_closed_before_exhaustion_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "target-root-inventory-pending",
        bundle_skill_provider=_ExhaustionBundleSkill(),
    )
    try:
        queried_run_refs: list[str] = []

        def pending_for_this_bundle(run_ref: str) -> tuple[str, ...]:
            queried_run_refs.append(run_ref)
            return ("target:root-still-running",)

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "list_bundle_target_root_work_refs",
            pending_for_this_bundle,
        )
        request, operation = _advance_to_exhaustion_operation(runtime)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert run is not None
        assert queried_run_refs == [run.run_ref]
        assert operation.status == "rejected"
        assert operation.feedback == (
            "A Target root owned by this Bundle run remains open or running.",
        )
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            is None
        )
        assert run.status == "running"
    finally:
        runtime.close()


def test_agent_runtime_target_root_inventory_is_scoped_to_exact_bundle_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-inventory-scope.sqlite3"
    upgrade_database(path)
    _candidate, _formal_plan, handle, _preflight, request = _target_records()
    _seed_admitted_launch(path, request, handle)
    owner, database = _target_owner_runtime(path, harness=None)
    try:
        assert owner.list_bundle_target_root_work_refs("bundle_run_1") == (
            handle.target_ref,
        )
        assert owner.list_bundle_target_root_work_refs("bundle_run_other") == ()
    finally:
        database.close()


def test_legacy_external_operation_claim_is_not_target_root_authority(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "legacy-operation-claim",
        bundle_skill_provider=_ExhaustionBundleSkill(claim_fake_operation=True),
    )
    try:
        request, operation = _advance_to_exhaustion_operation(runtime)
        assert operation.status == "rejected"
        assert operation.feedback == (
            "Legacy Target execution-operation claims are not issuer-owned.",
        )
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            is None
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "provider, error_code",
    (
        (
            _ExhaustionBundleSkill(corrupt_trace_seal=True),
            "bundle_exhaustion_review_trace_seal_invalid",
        ),
        (
            _ExhaustionBundleSkill(claim_fake_rejection=True),
            "bundle_exhaustion_rejected_submission_coverage_invalid",
        ),
    ),
)
def test_fake_review_or_rejection_evidence_has_zero_ar_write(
    tmp_path: Path, provider: _ExhaustionBundleSkill, error_code: str
) -> None:
    runtime = _bundle_runtime(
        tmp_path / error_code, bundle_skill_provider=provider
    )
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()  # Run admission.
        assert runtime.bundle_stage.process_once()  # Primary assessment.
        with pytest.raises(OwnerConflict, match=error_code):
            runtime.bundle_stage.process_once()  # Independent review acceptance.
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ar_bundle_exhaustion_evidence"
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ar_bundle_exhaustion_evidence_records"
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_restart_detects_persisted_evidence_tamper(tmp_path: Path) -> None:
    data_root = tmp_path / "exhaustion-evidence-tamper"
    runtime = _bundle_runtime(
        data_root, bundle_skill_provider=_ExhaustionBundleSkill()
    )
    request, run, evidence = _advance_to_evidence(runtime)
    runtime.close()

    with sqlite3.connect(data_root / "meta-research.sqlite3") as connection:
        connection.execute(
            "UPDATE ar_bundle_exhaustion_evidence SET evidence_json = '{}' "
            "WHERE evidence_ref = ?",
            (evidence.evidence_ref,),
        )

    restarted = _bundle_runtime(
        data_root, bundle_skill_provider=_ExhaustionBundleSkill()
    )
    try:
        with pytest.raises(
            OwnerConflict, match="bundle_exhaustion_evidence_integrity_invalid"
        ):
            restarted.owners.agent_runtime.query_bundle_exhaustion_evidence_for_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
            )
        assert (
            restarted.owners.advancement_engine.query_bundle_exhaustion_for_request(
                request.request_ref
            )
            is None
        )
    finally:
        restarted.close()
