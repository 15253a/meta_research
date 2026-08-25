from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

import meta_research.target_run_finalizer as target_run_finalizer_module
import meta_research.target_implementation_bundle as target_bundle_module
from meta_research.bundle_protocol import TargetWorkHandle
from meta_research.feed import DurableFeed
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.agent_runtime_harness import (
    TargetRootCompletionEvidence,
)
from meta_research.owners.research_memory import MAX_ASSET_BYTES
from meta_research.owners.target_root_lifecycle import (
    SQLiteTargetRootLifecycleAuthority,
)
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.target_implementation_bundle import (
    TargetImplementationBundleError,
    build_target_implementation_bundle_from_directory,
)
from meta_research.target_run_finalizer import (
    SQLiteTargetRootCompletionMemoryAuthority,
    TargetRootGraphAcceptance,
    TargetRootOwnerRejection,
    TargetRunFinalizer,
)
from meta_research.target_run_runtime_contract import (
    decode_target_completion_handoff,
)
import test_public_bundle_stage as bundle_fixtures
from test_target_launch_admission import _ready_launch


def _admit_independent_target_root(runtime):
    graph, target, _bundle_run, dispatch, _source_launch_request = _ready_launch(
        runtime
    )
    runtime.owners.research_graph.accept_formal_plan_content(
        formal_plan_ref=graph.formal_plan_ref,
        idempotency_key="accept-target-root-plan-source",
    )
    formal_plan_projection = (
        runtime.owners.research_graph.accept_target_formal_plan_projection(
            graph_ref=graph.graph_ref,
            idempotency_key="accept-target-root-plan-projection",
        )
    )
    candidate_projection = (
        runtime.owners.research_graph.accept_target_candidate_projection(
            target_ref=target.target_ref,
            idempotency_key="accept-target-root-candidate-projection",
        )
    )
    launch_request = runtime.owners.research_graph.query_target_launch_request(
        target.target_ref
    )
    runtime.owners.agent_runtime.admit_target_launch(
        launch_request,
        dispatch_decision_ref=dispatch.decision_ref,
        idempotency_key="formal-target-launch",
    )
    with runtime._database.read() as connection:
        target_run_ref = connection.execute(
            text(
                "SELECT target_run_ref FROM ar_target_launches WHERE "
                "target_ref = :target_ref"
            ),
            {"target_ref": target.target_ref},
        ).scalar_one()
    candidate = candidate_projection.candidate
    formal_plan = formal_plan_projection.formal_plan
    scope = canonical_target_scope_binding(
        target_ref=target.target_ref,
        target_run_ref=target_run_ref,
        target_spec_hash=launch_request.target_spec_binding.content_hash_ref,
        candidate=candidate,
        formal_plan=formal_plan,
        accepted_input_refs=(),
    )
    admission = runtime.harnesses.admit_target_run(
        target_ref=target.target_ref,
        target_run_ref=target_run_ref,
        harness_family="codex",
        model_ref="gpt-target-run",
        auth_profile_ref="harness-profile:codex-default",
        target_scope_binding=scope,
    )
    durable_harness_request = (
        runtime.owners.agent_runtime.harness_runs.query_request(
            admission.run.run_ref
        )
    )
    assert durable_harness_request["provider_operation_timeout_seconds"] == (
        30 * 24 * 60 * 60
    )
    accepted_input = (
        runtime.target_run_authorities.research_graph.accept_execution_input_binding(
            target_ref=target.target_ref,
            target_run_ref=admission.run.run_ref,
            target_attempt_ref=admission.run.attempt_ref,
            target_fence_ref=admission.run.fence_ref,
            target_spec_hash=launch_request.target_spec_binding.content_hash_ref,
            target_scope_binding_hash=canonical_hash(scope),
            input_refs=(),
            idempotency_key="formal-target-input-binding",
        )
    )
    handle = TargetWorkHandle(
        target_ref=target.target_ref,
        target_run_ref=admission.run.run_ref,
        root_session_ref=admission.run.root_session_ref,
        execution_attempt_ref=admission.run.attempt_ref,
        execution_fence_ref=admission.run.fence_ref,
        execution_input_binding_ref=accepted_input.proof.binding_ref,
        execution_input_binding_receipt=accepted_input.proof.acceptance_receipt,
        accepted_input_target_commit_refs=(),
        accepted_input_asset_proofs=(),
        recoverable=True,
    )
    runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
        handle=handle,
        idempotency_key=f"reserve-workspace:{target.target_ref}",
    )
    return target, candidate, formal_plan, admission, handle


class _GraphAcceptance:
    def __init__(self) -> None:
        self.calls = 0

    def accept_target_commit_from_root_completion(
        self, *, completion, manifest, result_document, idempotency_key
    ) -> TargetRootGraphAcceptance:
        self.calls += 1
        commit_ref = "target_commit_root_" + manifest.artifact_snapshot_hash[:32]
        return TargetRootGraphAcceptance(
            target_ref=completion.handle.target_ref,
            target_run_ref=completion.handle.target_run_ref,
            target_commit_ref=commit_ref,
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="target_root_commit_accepted",
                receipt_ref="rg_target_root_commit_receipt_"
                + canonical_hash(idempotency_key)[:32],
                subject_ref=commit_ref,
                payload_hash=canonical_hash(
                    {
                        "commit_ref": commit_ref,
                        "manifest_ref": manifest.manifest_ref,
                        "metrics": result_document.metrics,
                    }
                ),
            ),
        )


class _EvidenceReader:
    def __init__(self, evidence: TargetRootCompletionEvidence) -> None:
        self.evidence = evidence

    def verify_target_root_completion_evidence(
        self, *, handle, evidence, handoff
    ) -> str:
        if (
            evidence != self.evidence
            or evidence.target_ref != handle.target_ref
            or handoff != evidence.handoff
        ):
            raise OwnerConflict("target_root_completion_evidence_invalid")
        return canonical_hash(
            {
                "handle": handle.target_ref,
                "evidence_ref": evidence.evidence_ref,
                "operation_ref": evidence.operation_ref,
                "handoff": canonical_json(
                    {
                        "target_ref": handoff.target_ref,
                        "target_run_ref": handoff.target_run_ref,
                    }
                ),
            }
        )


def _root_finalizer_fixture(tmp_path: Path):
    runtime = _current_bundle_runtime(tmp_path / "target-root-finalizer")
    target, candidate, formal_plan, _admission, handle = (
        _admit_independent_target_root(runtime)
    )
    with runtime._database.read() as connection:
        launch_ref = connection.execute(
            text(
                "SELECT launch_ref FROM ar_target_launches WHERE target_ref = "
                ":target_ref"
            ),
            {"target_ref": handle.target_ref},
        ).scalar_one()
    feed = DurableFeed(runtime._database)
    lifecycle = SQLiteTargetRootLifecycleAuthority(
        runtime._database,
        feed,
        runtime.target_run_authorities.agent_runtime,
    )
    lifecycle.activate(
        launch_ref=launch_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        idempotency_key="activate-root-finalizer-fixture",
    )
    memory = SQLiteTargetRootCompletionMemoryAuthority(
        runtime._database,
        feed,
        runtime.owners.research_memory,
        lifecycle,
    )
    authority = runtime.owners.research_graph.query_target_measurement_domain_authority(
        target.target_ref
    )
    assert authority is not None
    _workspace_ref, workspace = (
        runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref,
            attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref,
        )
    )
    (workspace / "implementation" / "train.py").write_text(
        "print('train')\n", encoding="utf-8"
    )
    (workspace / "outputs").mkdir()
    (workspace / "logs").mkdir()
    metrics = {
        key: float(index + 1)
        for index, key in enumerate(
            authority.measurement_contract.protocol_version.required_metric_keys
        )
    }
    result_document = {
        "metrics": metrics,
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    (workspace / "logs" / "train.log").write_text(
        "epoch 1 complete\n", encoding="utf-8"
    )
    artifacts: list[dict[str, str]] = [
        {"role": "implementation", "relative_path": "implementation"},
        {"role": "result", "relative_path": "outputs/metrics.json"},
        {"role": "log", "relative_path": "logs/train.log"},
    ]
    if authority.measurement_contract.checkpoint_policy == "required":
        (workspace / "outputs" / "final.ckpt").write_bytes(b"checkpoint-v1")
        artifacts.insert(
            1,
            {"role": "checkpoint", "relative_path": "outputs/final.ckpt"},
        )
    handoff = canonical_json(
        {
            "artifacts": artifacts,
            "result_document_path": "outputs/metrics.json",
            "schema_ref": "meta-research/target-completion-handoff/v1",
            "status": "completed",
            "summary": "Root finished implementation and training.",
            "target_ref": handle.target_ref,
            "target_run_ref": handle.target_run_ref,
        }
    )
    evidence = TargetRootCompletionEvidence(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref,
        attempt_generation=1,
        root_session_ref=handle.root_session_ref,
        native_session_ref="native_target_root_finalizer_fixture",
        fence_ref=handle.execution_fence_ref,
        operation_ref="harness_target_root_final_turn",
        operation_generation=1,
        evidence_ref="harness_evidence_target_root_final_turn",
        evidence_sequence=10,
        handoff=decode_target_completion_handoff(handoff),
        observed_at=1.0,
    )
    return runtime, lifecycle, memory, authority, handle, workspace, evidence


def _current_bundle_runtime(path: Path):
    """Use the current composition surface, not the legacy port test option."""

    drafting = bundle_fixtures._DeterministicDraftingAdapter()
    runtime = bundle_fixtures.build_production_runtime(
        bundle_fixtures.prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=bundle_fixtures._DeterministicProbe(),
        idea_skill_provider=bundle_fixtures._DeterministicIdeaSkill(),
        plan_skill_provider=bundle_fixtures._DeterministicPlanSkill(
            no_gap=False
        ),
        bundle_skill_provider=bundle_fixtures._DeterministicBundleSkill(),
        harness_adapters=(
            bundle_fixtures._FullConformanceAdapter("codex"),
            bundle_fixtures._FullConformanceAdapter("claude"),
        ),
    )
    runtime.owners.research_graph._target_candidate_proof_verifier = (  # type: ignore[attr-defined]
        bundle_fixtures._AcceptingTargetCandidateProofVerifier()
    )
    if runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(bundle_fixtures._full_request())
        for _turn in range(4):
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
    return runtime


def test_finalizer_freezes_once_and_replays_without_reopening_live_workspace(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
    )
    try:
        first = finalizer.finalize(handle=handle, evidence=evidence)
        assert first.status == "rm_accepted"
        assert first.pending_code == "target_root_graph_acceptance_unavailable"
        completion = lifecycle.query_completion(handle.target_ref)
        manifest = memory.query(first.manifest_ref)
        assert completion is not None and manifest is not None
        assert manifest.artifact_snapshot_hash == completion.artifact_snapshot_hash
        assert manifest.result_document.metrics == {
            key: float(index + 1)
            for index, key in enumerate(
                authority.measurement_contract.protocol_version.required_metric_keys
            )
        }
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM rg_target_commits WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).scalar_one() == 0

        # After RM owns the frozen snapshot, later live-workspace changes are
        # irrelevant to replay and cannot rewrite the accepted manifest.
        (workspace / "implementation" / "train.py").write_text(
            "print('mutated later')\n", encoding="utf-8"
        )
        (workspace / "outputs" / "metrics.json").write_text(
            canonical_json(
                {
                    "metrics": {"forged": 999.0},
                    "result_disposition": "negative",
                    "schema_ref": "forged/result/v1",
                }
            ),
            encoding="utf-8",
        )
        replay = finalizer.finalize(handle=handle, evidence=evidence)
        assert replay == first
        assert memory.query(first.manifest_ref) == manifest

        graph = _GraphAcceptance()
        completing = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=graph,
        )
        completed = completing.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        assert completed.completion_ref == first.completion_ref
        assert completed.manifest_ref == first.manifest_ref
        assert completed.target_commit_ref is not None
        # The finalizer owns AR -> RM -> RG acceptance only.  The light daemon
        # publishes the verified RG transition to Bundle before marking the
        # root lifecycle completed.
        assert lifecycle.query(handle.target_ref).status == "finalizing"  # type: ignore[union-attr]
    finally:
        runtime.close()


def test_recoverable_rg_rejection_is_append_only_and_replays_as_revision_required(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )

    class RejectingGraph:
        calls = 0

        def accept_target_commit_from_root_completion(
            self, *, completion, manifest, result_document, idempotency_key
        ):
            self.calls += 1
            rejection_ref = "rg_target_root_rejection_schema"
            return TargetRootOwnerRejection(
                issuer="research_graph",
                rejection_ref=rejection_ref,
                code="target_root_result_schema_rejected",
                feedback=(
                    "Result document omitted the preregistered metric key."
                ),
                receipt=AcceptanceReceipt(
                    issuer="research_graph",
                    kind="target_root_completion_rejected",
                    receipt_ref="rg_target_root_rejection_receipt_schema",
                    subject_ref=completion.completion_ref,
                    payload_hash=canonical_hash(
                        {
                            "completion_ref": completion.completion_ref,
                            "manifest_ref": manifest.manifest_ref,
                            "rejection_ref": rejection_ref,
                            "code": "target_root_result_schema_rejected",
                        }
                    ),
                ),
            )

    graph = RejectingGraph()
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
        graph_authority=graph,
    )
    try:
        first = finalizer.finalize(handle=handle, evidence=evidence)

        assert first.status == "revision_required"
        assert first.completion_generation == 1
        assert first.rejection_ref == "rg_target_root_rejection_schema"
        assert first.rejection_issuer == "research_graph"
        assert first.rejection_feedback == (
            "Result document omitted the preregistered metric key."
        )
        assert first.pending_code == "target_root_result_schema_rejected"
        assert first.target_commit_ref is None
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
        assert lifecycle.query(handle.target_ref).completion_ref is None  # type: ignore[union-attr]

        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None and completion.generation == 1
        rejection = lifecycle.query_completion_rejection(
            completion.completion_ref
        )
        assert rejection is not None
        assert rejection.completion_ref == completion.completion_ref
        assert rejection.manifest_ref == first.manifest_ref
        assert rejection.issuer == "research_graph"
        assert rejection.receipt.subject_ref == completion.completion_ref
        assert memory.query(first.manifest_ref) is not None

        replay = finalizer.finalize(handle=handle, evidence=evidence)
        assert replay == first
        assert graph.calls == 1
        assert lifecycle.query_completion_by_ref(completion.completion_ref) == (
            completion
        )
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_target_root_completions")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_target_root_completion_manifests")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_target_root_completion_rejections")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0

        workspace = _workspace
        (workspace / "implementation" / "train.py").write_text(
            "print('train revision 2')\n", encoding="utf-8"
        )
        successor_handoff = replace(
            evidence.handoff,
            summary="Root revised the result after exact RG feedback.",
        )
        successor_evidence = replace(
            evidence,
            operation_ref="harness_target_root_successor_turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_target_root_successor_turn",
            evidence_sequence=evidence.evidence_sequence + 10,
            handoff=successor_handoff,
            observed_at=evidence.observed_at + 1.0,
        )
        accepting = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(successor_evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=_GraphAcceptance(),
        )
        successor = accepting.finalize(
            handle=handle, evidence=successor_evidence
        )
        assert successor.status == "completed"
        assert successor.completion_generation == 2
        successor_completion = lifecycle.query_completion(handle.target_ref)
        assert successor_completion is not None
        assert successor_completion.generation == 2
        assert successor_completion.predecessor_completion_ref == (
            completion.completion_ref
        )
        assert successor_completion.predecessor_rejection_ref == (
            rejection.rejection_ref
        )
        assert successor_completion.handle == completion.handle == handle
        assert lifecycle.query_completion_by_ref(completion.completion_ref) == (
            completion
        )
        assert lifecycle.query_completion_rejection(
            completion.completion_ref
        ) == rejection
        assert memory.query(first.manifest_ref) is not None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_target_root_completions")
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_target_root_completion_manifests")
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_target_root_completion_rejections")
            ).scalar_one() == 1
    finally:
        runtime.close()


def test_real_rg_semantic_rejection_returns_revision_required_after_rm_freeze(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    required = authority.measurement_contract.protocol_version.required_metric_keys
    invalid_result = {
        "metrics": {
            **{
                key: float(index + 1)
                for index, key in enumerate(required)
            },
            "metric:unregistered": 999.0,
        },
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(invalid_result), encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph,
    )
    try:
        result = finalizer.finalize(handle=handle, evidence=evidence)

        assert result.status == "revision_required"
        assert result.rejection_issuer == "research_graph"
        assert result.pending_code == "target_root_commit_domain_invalid"
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
        assert memory.query(result.manifest_ref) is not None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_root_measurements")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_invalid_result_candidate_is_rejected_before_rm_and_can_be_revised(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    (workspace / "outputs" / "metrics.json").write_text(
        "{not-json}", encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.completion_generation == 1
        assert rejected.manifest_ref is None
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.pending_code == "target_root_result_document_invalid"
        assert rejected.rejection_feedback == (
            "The declared result document is not valid canonical JSON for the "
            "Target result schema. Rewrite it and submit a new completion handoff."
        )
        first_completion = lifecycle.query_completion(handle.target_ref)
        assert first_completion is not None
        assert first_completion.generation == 1
        first_rejection = lifecycle.query_completion_rejection(
            first_completion.completion_ref
        )
        assert first_rejection is not None
        assert first_rejection.manifest_ref is None
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected

        valid_result = {
            "metrics": {
                key: float(index + 1)
                for index, key in enumerate(
                    authority.measurement_contract.protocol_version.required_metric_keys
                )
            },
            "result_disposition": "positive",
            "schema_ref": authority.measurement_contract.result_schema_ref,
        }
        (workspace / "outputs" / "metrics.json").write_text(
            canonical_json(valid_result), encoding="utf-8"
        )
        successor_evidence = replace(
            evidence,
            operation_ref="harness_target_root_revised_final_turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_target_root_revised_final_turn",
            evidence_sequence=evidence.evidence_sequence + 1,
            handoff=replace(
                evidence.handoff,
                summary="Root corrected the rejected result document.",
            ),
            observed_at=evidence.observed_at + 1.0,
        )
        successor = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(successor_evidence),
            measurement_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=successor_evidence)

        assert successor.status == "rm_accepted"
        assert successor.completion_generation == 2
        assert successor.manifest_ref is not None
        second_completion = lifecycle.query_completion(handle.target_ref)
        assert second_completion is not None
        assert second_completion.predecessor_completion_ref == (
            first_completion.completion_ref
        )
        assert second_completion.predecessor_rejection_ref == (
            first_rejection.rejection_ref
        )
    finally:
        runtime.close()


def test_oversized_result_document_is_a_recoverable_rm_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    monkeypatch.setattr(
        target_run_finalizer_module,
        "TARGET_ROOT_MAX_RESULT_DOCUMENT_BYTES",
        1,
        raising=False,
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_document_too_large"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_rejects_more_metrics_than_the_formal_protocol_can_name(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {f"metric:{index:03d}": index for index in range(129)},
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_metrics_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_maps_unbounded_integer_to_typed_metric_rejection(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {"metric:unbounded": 10**309},
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_metrics_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_rejects_finite_integer_beyond_bundle_canonical_range(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {"metric:out-of-range": 2**63},
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_metrics_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_accepts_bundle_canonical_integer_boundary(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {"metric:boundary": (2**63) - 1},
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        accepted = finalizer.finalize(handle=handle, evidence=evidence)

        assert accepted.status == "rm_accepted"
        manifest = memory.query(accepted.manifest_ref)
        assert manifest is not None
        assert manifest.result_document.metrics == {
            "metric:boundary": (2**63) - 1
        }
        assert lifecycle.query(handle.target_ref).status == "finalizing"  # type: ignore[union-attr]
    finally:
        runtime.close()


def test_result_document_maps_deep_json_to_recoverable_typed_rejection(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    (workspace / "outputs" / "metrics.json").write_text(
        "[" * 10_000 + "0" + "]" * 10_000,
        encoding="utf-8",
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_document_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_maps_surrogate_schema_to_recoverable_invalid_document(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {"metric:score": 1},
        "result_disposition": "positive",
        "schema_ref": "\ud800",
    }
    (workspace / "outputs" / "metrics.json").write_text(
        json.dumps(
            result_document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_document_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_result_document_maps_surrogate_metric_key_to_recoverable_invalid_metrics(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    result_document = {
        "metrics": {"\ud800": 1},
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
    }
    (workspace / "outputs" / "metrics.json").write_text(
        json.dumps(
            result_document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_metrics_invalid"
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.manifest_ref is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def _remove_declared_log(workspace: Path) -> None:
    (workspace / "logs" / "train.log").unlink()


def _oversize_declared_log(workspace: Path) -> None:
    with (workspace / "logs" / "train.log").open("wb") as stream:
        stream.truncate(MAX_ASSET_BYTES + 1)


@pytest.mark.parametrize(
    ("prepare_invalid_artifact", "expected_code"),
    (
        (
            _remove_declared_log,
            "target_root_artifact_missing",
        ),
        (
            _oversize_declared_log,
            "target_root_artifact_too_large",
        ),
    ),
)
def test_missing_or_oversized_artifact_is_a_recoverable_rm_rejection(
    tmp_path: Path,
    prepare_invalid_artifact,
    expected_code: str,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    prepare_invalid_artifact(workspace)
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.manifest_ref is None
        assert rejected.pending_code == expected_code
        assert rejected.rejection_issuer == "research_memory"
        assert rejected.rejection_feedback
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_artifact_io_unavailability_remains_finalization_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    original_open = target_run_finalizer_module.os.open

    def unavailable_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "train.log" and dir_fd is not None:
            raise OSError("artifact storage unavailable")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        target_run_finalizer_module.os,
        "open",
        unavailable_open,
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        with pytest.raises(
            OwnerConflict, match="target_root_artifact_unavailable"
        ):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert lifecycle.query_completion(handle.target_ref) is None
        assert lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
    finally:
        runtime.close()


def test_aggregate_artifact_budget_is_a_recoverable_rm_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    monkeypatch.setattr(
        target_run_finalizer_module,
        "TARGET_ROOT_MAX_ARTIFACT_SET_BYTES",
        1,
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.manifest_ref is None
        assert rejected.pending_code == "target_root_artifact_set_too_large"
        assert rejected.rejection_issuer == "research_memory"
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_transient_rm_unavailability_replays_same_frozen_completion(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )

    class UnavailableMemory:
        def __getattr__(self, name: str):
            return getattr(memory, name)

        def accept(self, **_values):
            raise OwnerConflict("target_root_artifact_intake_unavailable")

    unavailable = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=UnavailableMemory(),  # type: ignore[arg-type]
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        with pytest.raises(
            OwnerConflict, match="target_root_artifact_intake_unavailable"
        ):
            unavailable.finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None
        assert completion.candidate_rejection_code is None
        assert (
            lifecycle.query_completion_rejection(completion.completion_ref)
            is None
        )
        assert (
            lifecycle.query(handle.target_ref).status == "finalizing"  # type: ignore[union-attr]
        )

        replayed = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
        ).finalize(handle=handle, evidence=evidence)
        assert replayed.status == "rm_accepted"
        assert replayed.completion_ref == completion.completion_ref
        assert replayed.completion_generation == 1
        assert (
            lifecycle.query_completion_rejection(completion.completion_ref)
            is None
        )
    finally:
        runtime.close()


def test_deterministic_rm_intake_failure_is_an_issuer_owned_rejection(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )

    class RejectingMemory:
        calls = 0

        def __getattr__(self, name: str):
            return getattr(memory, name)

        def accept(self, **_values):
            self.calls += 1
            raise OwnerConflict("asset_content_too_large")

    rejecting_memory = RejectingMemory()
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=rejecting_memory,  # type: ignore[arg-type]
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.manifest_ref is None
        assert rejected.pending_code == "asset_content_too_large"
        assert rejected.rejection_issuer == "research_memory"
        assert rejecting_memory.calls == 1
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        assert rejecting_memory.calls == 1
        assert memory.query_for_completion(rejected.completion_ref) is None
    finally:
        runtime.close()


def test_prefreeze_rejection_store_unavailability_replays_exact_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    (workspace / "outputs" / "metrics.json").write_text(
        "{not-json}", encoding="utf-8"
    )
    original_reject = lifecycle.reject_completion
    attempts = 0

    def reject_once_unavailable(**values):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OwnerConflict("target_root_rejection_store_unavailable")
        return original_reject(**values)

    monkeypatch.setattr(lifecycle, "reject_completion", reject_once_unavailable)
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        with pytest.raises(
            OwnerConflict, match="target_root_rejection_store_unavailable"
        ):
            finalizer.finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None
        assert completion.candidate_rejection_code == (
            "target_root_result_document_invalid"
        )
        assert completion.candidate_rejection_feedback
        assert (
            lifecycle.query_completion_rejection(completion.completion_ref)
            is None
        )
        assert (
            lifecycle.query(handle.target_ref).status == "finalizing"  # type: ignore[union-attr]
        )

        # Even if live bytes drift before retry, replay uses the exact failure
        # already frozen into the immutable candidate generation.
        (workspace / "outputs" / "metrics.json").write_text(
            canonical_json(
                {
                    "metrics": {"later": 1.0},
                    "result_disposition": "positive",
                    "schema_ref": "later/schema/v1",
                }
            ),
            encoding="utf-8",
        )
        replayed = finalizer.finalize(handle=handle, evidence=evidence)
        assert replayed.status == "revision_required"
        assert replayed.completion_ref == completion.completion_ref
        assert replayed.pending_code == "target_root_result_document_invalid"
        assert (
            replayed.rejection_feedback
            == completion.candidate_rejection_feedback
        )
        assert attempts == 2
        assert (
            lifecycle.query(handle.target_ref).status == "running"  # type: ignore[union-attr]
        )
    finally:
        runtime.close()


def test_finalizer_rejects_symlinked_declared_artifact_before_ar_or_rm(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    (workspace / "logs" / "train.log").unlink()
    (workspace / "logs" / "train.log").symlink_to(
        workspace / "outputs" / "metrics.json"
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
    )
    try:
        with pytest.raises(
            OwnerConflict, match="target_root_artifact_symlink_forbidden"
        ):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert lifecycle.query_completion(handle.target_ref) is None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_target_root_completion_manifests")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_directory_bundle_freeze_never_reads_through_a_swapped_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace" / "implementation"
    model = source / "model"
    model.mkdir(parents=True)
    safe_content = b"workspace-weights\n"
    (model / "weights.bin").write_bytes(safe_content)
    outside = tmp_path / "host-private-model"
    outside.mkdir()
    # Match the workspace byte count so the old lstat/Path.read_bytes split
    # cannot merely notice a size difference after following the new link.
    host_secret = b"HOST-SECRET-12345\n"
    (outside / "weights.bin").write_bytes(host_secret)
    parked = source / "model-before-race"
    original_read_bytes = Path.read_bytes
    swapped = False

    def swap_ancestor_before_path_read(path: Path) -> bytes:
        nonlocal swapped
        if path == model / "weights.bin" and not swapped:
            model.rename(parked)
            model.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swap_ancestor_before_path_read)
    try:
        bundle = build_target_implementation_bundle_from_directory(source)
    finally:
        if swapped:
            model.unlink()
            parked.rename(model)

    assert bundle.entry("model/weights.bin").content == safe_content
    assert host_secret not in bundle.bundle_bytes


def test_directory_bundle_enforces_one_budget_across_nested_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace" / "implementation"
    for directory in ("a", "b", "z"):
        (source / directory).mkdir(parents=True)
    (source / "a" / "first.bin").write_bytes(b"1234")
    (source / "b" / "second.bin").write_bytes(b"5678")
    outside = tmp_path / "HOST-SECRET"
    outside.write_bytes(b"must-not-be-opened")
    (source / "z" / "later.bin").symlink_to(outside)
    monkeypatch.setattr(
        target_bundle_module,
        "IMPLEMENTATION_TOTAL_MAX_BYTES",
        5,
    )

    with pytest.raises(
        TargetImplementationBundleError,
        match="target_implementation_bundle_too_large",
    ):
        build_target_implementation_bundle_from_directory(source)


def test_directory_bundle_bounds_fd_enumeration_before_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace" / "implementation"
    source.mkdir(parents=True)
    for name in ("a.bin", "b.bin", "c.bin"):
        (source / name).write_bytes(name.encode("ascii"))
    monkeypatch.setattr(
        target_bundle_module,
        "IMPLEMENTATION_ENTRY_MAX_COUNT",
        2,
    )

    def forbidden_unbounded_listdir(_descriptor: int):
        raise AssertionError("workspace enumeration must be fd-based and bounded")

    monkeypatch.setattr(
        target_bundle_module.os,
        "listdir",
        forbidden_unbounded_listdir,
    )

    with pytest.raises(
        TargetImplementationBundleError,
        match="target_implementation_bundle_too_large",
    ):
        build_target_implementation_bundle_from_directory(source)


def test_directory_bundle_maps_raw_bytes_filename_to_typed_rejection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace" / "implementation"
    source.mkdir(parents=True)
    directory_descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        file_descriptor = os.open(
            b"bad-\xff.py",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            os.write(file_descriptor, b"HOST-RAW-NAME\n")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(directory_descriptor)

    with pytest.raises(
        TargetImplementationBundleError,
        match="target_implementation_workspace_entry_unsupported",
    ):
        build_target_implementation_bundle_from_directory(source)


def test_finalizer_maps_raw_implementation_filename_to_prefreeze_rejection(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    implementation_descriptor = os.open(
        workspace / "implementation", os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        file_descriptor = os.open(
            b"bad-\xff.py",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=implementation_descriptor,
        )
        os.close(file_descriptor)
    finally:
        os.close(implementation_descriptor)
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.manifest_ref is None
        assert rejected.pending_code == (
            "target_implementation_workspace_entry_unsupported"
        )
        assert rejected.rejection_issuer == "research_memory"
        root_state = lifecycle.query(handle.target_ref)
        assert root_state is not None
        assert root_state.status == "running"
        assert memory.query_for_completion(rejected.completion_ref) is None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_target_root_completion_manifests")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("invalid_shape", ("empty", "padded-name"))
def test_finalizer_maps_invalid_implementation_shape_to_prefreeze_rejection(
    tmp_path: Path,
    invalid_shape: str,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    implementation_file = workspace / "implementation" / "train.py"
    if invalid_shape == "empty":
        implementation_file.unlink()
    else:
        implementation_file.rename(
            workspace / "implementation" / " train.py"
        )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.manifest_ref is None
        assert rejected.pending_code == (
            "target_implementation_workspace_entry_unsupported"
        )
        assert rejected.rejection_issuer == "research_memory"
        root_state = lifecycle.query(handle.target_ref)
        assert root_state is not None
        assert root_state.status == "running"
        assert memory.query_for_completion(rejected.completion_ref) is None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_target_root_completion_manifests")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_finalizer_never_reads_plain_file_through_a_swapped_ancestor_into_rm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    logs = workspace / "logs"
    safe_log = (logs / "train.log").read_bytes()
    parked = workspace / "logs-before-race"
    outside = tmp_path / "host-private-logs"
    outside.mkdir()
    host_secret = b"HOST-SECRET-must-never-enter-RM\n"
    (outside / "train.log").write_bytes(host_secret)
    original_open = target_run_finalizer_module.os.open
    swapped = False

    def swap_ancestor_before_path_open(
        path, flags, mode=0o777, *, dir_fd=None
    ):
        nonlocal swapped
        if (
            dir_fd is None
            and Path(path) == logs / "train.log"
            and not swapped
        ):
            logs.rename(parked)
            logs.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        target_run_finalizer_module.os,
        "open",
        swap_ancestor_before_path_open,
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        result = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query(result.manifest_ref)
        assert manifest is not None
        log_entry = next(entry for entry in manifest.entries if entry.role == "log")
        materialized = runtime.owners.research_memory.materialize_asset(
            log_entry.binding.version_ref
        )

        assert materialized.content == safe_log
        assert host_secret not in materialized.content
    finally:
        if swapped:
            logs.unlink()
            parked.rename(logs)
        runtime.close()
