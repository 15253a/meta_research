"""Real-SQLite behavioral tests for Part 1: Plan & Semantic MCP deadlocks/livelocks."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.error_audit import ErrorAuditStore
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.paths import prepare_data_root
from meta_research.plan_contract import (
    PlanContractError,
    validate_plan_document,
)
from meta_research.plan_skill import _with_derived_answer_contract_hash
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from meta_research.semantic_mcp import (
    SemanticMcpError,
    SemanticMcpGateway,
    SemanticOperation,
    _valid_schema_contract,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
)
from meta_research.target_commit_evidence import target_adoption_binding
from meta_research.target_run_finalizer import (
    SQLiteTargetRootCompletionMemoryAuthority,
    TargetRootCompletionEvidence,
    TargetRunFinalizer,
)

from meta_research.reasoning_contract import (
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
)
from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from test_public_bundle_stage import (
    _TwoTargetBundleSkill,
    _TogglePowerInhibitor,
)
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicProbe,
    _confirm_direct_quest,
)
from test_public_reasoning_stage import (
    _MultiRunIdeaSkill,
    _MultiRunPlanSkill,
    _MultiRunReasoningSkill,
    _research_synthesis,
)
from test_research_datasets import _asset
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _CurrentBindingBundleSkill
from test_two_cycle_actual_work import _finish_stage


class _TwoTargetCurrentBundleSkill(_CurrentBindingBundleSkill):
    _target_plan = _TwoTargetBundleSkill._target_plan
    _second_dependencies = _TwoTargetBundleSkill._second_dependencies


class _Cycle2AccountingPlanSkill(_MultiRunPlanSkill):
    """Cycle 1 plans a gap; Cycle 2 discovers 2 TargetCommits from RG and sets accounting."""

    def __init__(self, *, mode: str = "adopt_one_decline_one") -> None:
        super().__init__(no_gap=False)
        self.mode = mode
        self.runtime = None
        self.discovered_entries: list[dict[str, object]] = []
        self.cycle2_requests = []

    def _document(self, request):
        assert self.runtime is not None
        quest_ref = str(request.context_pack["accepted_question_binding"]["quest_ref"])
        page, entries = self.runtime.owners.research_graph.query_plan_evidence_page(
            quest_ref=quest_ref,
            question_ref=request.question_ref,
            limit=32,
        )
        assert page["next_offset"] is None
        if not entries:
            self.no_gap = False
            self.selected_evidence_bindings = []
            document = super()._document(request)
            document["evidence_accounting"] = []
            return _with_derived_answer_contract_hash(document, request)

        self.cycle2_requests.append(request)
        self.discovered_entries = [dict(item) for item in entries]
        assert len(self.discovered_entries) == 2
        first, second = self.discovered_entries[0], self.discovered_entries[1]
        self.no_gap = True
        self.selected_evidence_bindings = [target_adoption_binding(first)]
        document = super()._document(request)
        document["coverage"][0]["evidence_uses"][0]["supported_claim"] = (
            "Adopted first TargetCommit as bounded evidence for this obligation."
        )
        document["coverage"][0]["evidence_uses"][0]["support_boundary"] = (
            "Replication TargetCommit was intentionally not adopted for this cycle."
        )
        if self.mode == "adopt_one_decline_one":
            document["evidence_accounting"] = [
                {
                    "evidence_ref": first["evidence_ref"],
                    "target_commit_ref": first["target_commit_root_ref"],
                    "status": "adopted",
                    "reason": "Primary TargetCommit satisfies the current obligation.",
                },
                {
                    "evidence_ref": second["evidence_ref"],
                    "target_commit_ref": second["target_commit_root_ref"],
                    "status": "not_used",
                    "reason": "Replication TargetCommit is outside this Cycle's obligation scope.",
                },
            ]
        elif self.mode == "omit_second":
            document["evidence_accounting"] = [
                {
                    "evidence_ref": first["evidence_ref"],
                    "target_commit_ref": first["target_commit_root_ref"],
                    "status": "adopted",
                    "reason": "Primary TargetCommit satisfies the current obligation.",
                },
            ]
        elif self.mode == "unknown_not_used":
            document["evidence_accounting"] = [
                {
                    "evidence_ref": first["evidence_ref"],
                    "target_commit_ref": first["target_commit_root_ref"],
                    "status": "adopted",
                    "reason": "Primary TargetCommit satisfies the current obligation.",
                },
                {
                    "evidence_ref": second["evidence_ref"],
                    "target_commit_ref": second["target_commit_root_ref"],
                    "status": "not_used",
                    "reason": "Replication TargetCommit is outside this Cycle's obligation scope.",
                },
                {
                    "evidence_ref": "evidence_forged_unknown",
                    "target_commit_ref": "target_commit_forged_unknown",
                    "status": "not_used",
                    "reason": "Non-existent commit included to verify RG rejection diagnostics.",
                },
            ]
        else:
            raise AssertionError(f"Unknown test mode: {self.mode}")
        return _with_derived_answer_contract_hash(document, request)


class _Cycle1ReasoningSkill(_MultiRunReasoningSkill):
    def finalize_draft(self, request, draft):
        return replace(
            super().finalize_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )

    def _result_parts(self, request):
        metric = next(
            leaf
            for leaf in request.frozen_evidence_closure
            if leaf["kind"] == "MetricResult"
        )
        closure = request.context_pack["upstream_stage_closure"]
        assert isinstance(closure, list)
        idea_ref = next(
            str(item["outcome_ref"])
            for item in closure
            if isinstance(item, dict) and item.get("stage") == "idea"
        )
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
            }
        )[:24]
        scientific_outcome = {
            "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
            "kind": "ScientificOutcomeCandidate",
            "outcome_ref": outcome_ref,
            "stage_run_request_ref": request.stage_request_ref,
            "cycle_ref": request.cycle_ref,
            "question_ref": request.question_ref,
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "foreground_epoch": request.foreground_epoch,
            "disposition": "affirmed",
            "claim": "Both executed Target commits were recorded; continue to Cycle 2 Plan.",
            "evidence": [
                {
                    "kind": "MetricResult",
                    "ref": metric["ref"],
                    "finding": "supporting",
                }
            ],
            "missing_evidence": [],
            "uncertainty_basis": [],
            "support_scope": ["The exact MetricResult from Cycle 1 executed Targets."],
            "limitations": ["No evidence outside the frozen Cycle 1 targets."],
            "causal_interpretation": {
                **request.context_pack["research_context"]["causal_context"],
                "attribution_basis_refs": [str(metric["ref"])],
                "claim_scope": "Cycle 1 executed Targets.",
                "statement": "The executed Target metric supports bounded observation.",
                "sufficiency_rationale": "Issuer-accepted MetricResult is present.",
                "confounders": ["Limited to Cycle 1 test condition."],
            },
            "research_synthesis": _research_synthesis(request),
            "is_authoritative": False,
        }
        next_cycle = {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            "cycle_focus": "Reassess Plan evidence accounting in Cycle 2",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "target_question_ref": request.question_ref,
            "target_question_anchor_ref": request.question_ref,
            "entry_stage": "plan",
            "typed_skip_basis_refs_by_stage": {"idea": [idea_ref]},
            "is_authoritative": False,
        }
        return scientific_outcome, next_cycle


def _build_cycle_runtime(
    data_root: Path,
    *,
    plan_skill: _Cycle2AccountingPlanSkill,
    bundle_skill: _TwoTargetCurrentBundleSkill,
    reasoning_skill: _Cycle1ReasoningSkill,
):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(data_root),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_MultiRunIdeaSkill(),
        plan_skill_provider=plan_skill,
        bundle_skill_provider=bundle_skill,
        reasoning_skill_provider=reasoning_skill,
        harness_adapters=(_FullConformanceAdapter("codex"),),
        power_inhibitor=_TogglePowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    plan_skill.runtime = runtime
    if runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(_full_request())
        for _turn in range(4):
            if runtime.harnesses.query_status()["status"] == "ready":
                break
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
        assert runtime.harnesses.query_status()["status"] == "ready"
    return runtime


def _dispatch_and_finalize_next_target(
    runtime, *, seen_target_refs: set[str], ordinal: int
) -> str:
    target = None
    graph = None
    run = None
    decision = None
    launch = None
    for _step in range(24):
        assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        req = current["stage_run_request"]
        if req is None:
            continue
        run = runtime.owners.agent_runtime.query_bundle_stage_run(req["request_ref"])
        if run is None:
            continue
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        for cand_decision in reversed(decisions):
            for ref in cand_decision.selected_target_refs:
                if ref not in seen_target_refs:
                    candidate_graph = runtime.owners.research_graph.query_target_graph(
                        run.request_ref
                    )
                    assert candidate_graph is not None
                    candidate_target = next(
                        item for item in candidate_graph.targets if item.target_ref == ref
                    )
                    if (
                        runtime.owners.research_graph.query_target_candidate_projection(
                            target_ref=candidate_target.target_ref
                        )
                        is not None
                    ):
                        graph = candidate_graph
                        target = candidate_target
                        decision = cand_decision
                        launch = runtime.owners.research_graph.query_target_launch_request(
                            candidate_target.target_ref
                        )
                        break
            if target is not None:
                break
        if target is not None:
            break
    assert target is not None and graph is not None and decision is not None and launch is not None
    seen_target_refs.add(target.target_ref)

    key_prefix = f"cycle1-target-{ordinal}-"
    runtime.owners.research_graph.accept_formal_plan_content(
        formal_plan_ref=graph.formal_plan_ref,
        idempotency_key=key_prefix + "plan-source",
    )
    formal_plan_projection = (
        runtime.owners.research_graph.accept_target_formal_plan_projection(
            graph_ref=graph.graph_ref,
            idempotency_key=key_prefix + "plan-proj",
        )
    )
    candidate_projection = (
        runtime.owners.research_graph.accept_target_candidate_projection(
            target_ref=target.target_ref,
            idempotency_key=key_prefix + "cand-proj",
        )
    )
    runtime.owners.agent_runtime.admit_target_launch(
        launch,
        dispatch_decision_ref=decision.decision_ref,
        idempotency_key=key_prefix + "admit-launch",
    )
    with runtime._database.read() as connection:
        target_run_ref = connection.execute(
            text("SELECT target_run_ref FROM ar_target_launches WHERE target_ref = :t"),
            {"t": target.target_ref},
        ).scalar_one()
    input_refs = tuple(
        sorted((*launch.accepted_input_target_commit_refs, *launch.accepted_input_asset_refs))
    )
    input_proofs = tuple(
        runtime.target_run_authorities.research_graph.query_bundle_input_asset_proof(
            target_ref=target.target_ref, asset_ref=ref
        )
        for ref in launch.accepted_input_asset_refs
    )
    scope = canonical_target_scope_binding(
        target_ref=target.target_ref,
        target_run_ref=target_run_ref,
        target_spec_hash=launch.target_spec_binding.content_hash_ref,
        candidate=candidate_projection.candidate,
        formal_plan=formal_plan_projection.formal_plan,
        accepted_input_refs=input_refs,
    )
    admission = runtime.harnesses.admit_target_run(
        target_ref=target.target_ref,
        target_run_ref=target_run_ref,
        harness_family="codex",
        model_ref="gpt-target-run",
        auth_profile_ref="harness-profile:codex-default",
        target_scope_binding=scope,
    )
    accepted_input = (
        runtime.target_run_authorities.research_graph.accept_execution_input_binding(
            target_ref=target.target_ref,
            target_run_ref=admission.run.run_ref,
            target_attempt_ref=admission.run.attempt_ref,
            target_fence_ref=admission.run.fence_ref,
            target_spec_hash=launch.target_spec_binding.content_hash_ref,
            target_scope_binding_hash=canonical_hash(scope),
            input_refs=input_refs,
            idempotency_key=key_prefix + "input-binding",
        )
    )
    handle = replace(
        _make_handle(
            target_ref=target.target_ref,
            target_run_ref=admission.run.run_ref,
            root_session_ref=admission.run.root_session_ref,
            execution_attempt_ref=admission.run.attempt_ref,
            execution_fence_ref=admission.run.fence_ref,
            execution_input_binding_ref=accepted_input.proof.binding_ref,
            execution_input_binding_receipt=accepted_input.proof.acceptance_receipt,
            accepted_input_target_commit_refs=launch.accepted_input_target_commit_refs,
            accepted_input_asset_proofs=input_proofs,
        )
    )
    runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
        handle=handle,
        idempotency_key=key_prefix + "reserve-ws",
    )
    with runtime._database.read() as connection:
        launch_ref = connection.execute(
            text("SELECT launch_ref FROM ar_target_launches WHERE target_ref = :t"),
            {"t": target.target_ref},
        ).scalar_one()
    lifecycle = SQLiteTargetRootLifecycleAuthority(
        runtime._database,
        runtime.feed,
        runtime.target_run_authorities.agent_runtime,
    )
    lifecycle.activate(
        launch_ref=launch_ref,
        handle=handle,
        candidate=candidate_projection.candidate,
        formal_plan=formal_plan_projection.formal_plan,
        idempotency_key=key_prefix + "activate",
    )
    memory = SQLiteTargetRootCompletionMemoryAuthority(
        runtime._database,
        runtime.feed,
        runtime.owners.research_memory,
        lifecycle,
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(),
        measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph,
    )
    if handle.accepted_input_target_commit_refs or handle.accepted_input_asset_proofs:
        finalizer.materialize_inputs(handle=handle)
    authority = runtime.owners.research_graph.query_target_measurement_domain_authority(
        target.target_ref
    )
    assert authority is not None
    workspace_ref, workspace = (
        runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref,
            attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref,
        )
    )
    (workspace / "implementation" / "train.py").write_text(
        f"print('train target {ordinal}')\n", encoding="utf-8"
    )
    (workspace / "outputs").mkdir(exist_ok=True)
    (workspace / "logs").mkdir(exist_ok=True)
    metrics = {
        key: float(index + ordinal)
        for index, key in enumerate(
            authority.measurement_contract.protocol_version.required_metric_keys
        )
    }
    log_rel = f"logs/train_{ordinal}.log"
    (workspace / log_rel).write_text(
        f"target {ordinal} epoch 1 complete\n", encoding="utf-8"
    )
    result_document: dict[str, object] = {
        "metrics": metrics,
        "result_disposition": "positive",
        "schema_ref": authority.measurement_contract.result_schema_ref,
        "formal_runs": [
            {
                "run_key": f"run_{ordinal}",
                "artifact_paths": [log_rel],
                "checkpoint_paths": [],
                "evaluations": [
                    {
                        "attempt_key": f"eval_{ordinal}",
                        "metrics": metrics,
                        "artifact_paths": [],
                    }
                ],
            }
        ],
        "retained_artifacts": [{"role": "log", "relative_path": log_rel}],
    }
    if authority.measurement_contract.checkpoint_policy == "required":
        ckpt_rel = f"outputs/final_{ordinal}.ckpt"
        (workspace / ckpt_rel).write_bytes(f"ckpt-{ordinal}".encode())
        result_document["formal_runs"][0]["checkpoint_paths"] = [ckpt_rel]  # type: ignore[index]
        result_document["retained_artifacts"].append(  # type: ignore[union-attr]
            {"role": "checkpoint", "relative_path": ckpt_rel}
        )
    (workspace / "outputs" / "result.json").write_text(
        canonical_json(result_document), encoding="utf-8"
    )
    final_text = f"Target {ordinal} completed training and evaluation."
    evidence = TargetRootCompletionEvidence(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref,
        attempt_generation=1,
        root_session_ref=handle.root_session_ref,
        native_session_ref=f"native_target_{ordinal}",
        fence_ref=handle.execution_fence_ref,
        operation_ref=f"harness_turn_target_{ordinal}",
        operation_generation=1,
        evidence_ref=f"harness_evidence_target_{ordinal}",
        evidence_sequence=10 + ordinal,
        workspace_ref=workspace_ref,
        final_text=final_text,
        final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest(),
        observed_at=float(ordinal),
    )
    completed = finalizer.finalize(handle=handle, evidence=evidence)
    assert completed.status == "completed"
    assert completed.target_commit_ref is not None
    runtime.owners.agent_runtime.publish_target_root_completion(
        target_ref=handle.target_ref,
        completion_ref=completed.completion_ref,
        target_commit_ref=completed.target_commit_ref,
    )
    lifecycle.mark_completed(
        target_ref=handle.target_ref,
        completion_ref=completed.completion_ref,
    )
    return completed.target_commit_ref


def _make_handle(**kwargs):
    from meta_research.bundle_protocol import TargetWorkHandle

    return TargetWorkHandle(**kwargs, recoverable=True)


def _advance_to_cycle2_plan_request(data_root: Path, *, mode: str):
    plan_skill = _Cycle2AccountingPlanSkill(mode=mode)
    bundle_skill = _TwoTargetCurrentBundleSkill()
    reasoning_skill = _Cycle1ReasoningSkill(entry_stage="plan")
    runtime = _build_cycle_runtime(
        data_root,
        plan_skill=plan_skill,
        bundle_skill=bundle_skill,
        reasoning_skill=reasoning_skill,
    )
    quest = _confirm_direct_quest(runtime)
    _finish_stage(runtime, "idea")
    _finish_stage(runtime, "plan")
    seen_targets: set[str] = set()
    commit1 = _dispatch_and_finalize_next_target(runtime, seen_target_refs=seen_targets, ordinal=1)
    commit2 = _dispatch_and_finalize_next_target(runtime, seen_target_refs=seen_targets, ordinal=2)
    assert commit1 != commit2
    _finish_stage(runtime, "bundle")
    _finish_stage(runtime, "reasoning")

    foreground = runtime.owners.advancement_engine.query_foreground(str(quest["quest_ref"]))
    assert foreground is not None
    assert foreground["cycle_ref"] != quest["cycle_ref"]
    assert foreground["stage"] == "plan"

    # Tick Plan once so the Cycle 2 Plan StageRunRequest exists in SQLite.
    assert runtime.plan_stage.process_once()
    plan_request = runtime.owners.advancement_engine.query_plan_stage_request(
        str(foreground["cycle_ref"])
    )
    assert plan_request is not None
    assert plan_request.context_pack["evidence_catalog"] == []
    assert plan_request.context_pack["evidence_reference_revision"] == 0
    return runtime, plan_skill, quest, foreground, (commit1, commit2)


def test_plan_cycle2_adopts_one_and_declines_one_target_commit_with_empty_frozen_catalog(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "cycle2-accounting-e2e"
    runtime, plan_skill, _quest, foreground, (commit1, commit2) = (
        _advance_to_cycle2_plan_request(data_root, mode="adopt_one_decline_one")
    )
    submission_ref = None
    expected_accounting = None
    try:
        for _step in range(20):
            current = runtime.plan_stage.query_current()
            run_info = current.get("run") or {}
            if run_info.get("completion_rejection"):
                raise AssertionError(
                    f"Skill/completion rejected Plan candidate: {run_info['completion_rejection']}"
                )
            acceptance = current.get("plan_acceptance") or {}
            if (acceptance.get("domain") or {}).get("status") == "rejected":
                raise AssertionError(f"RG rejected Plan candidate: {acceptance}")
            if current["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once(), runtime.plan_stage.transient_error
        current = runtime.plan_stage.query_current()
        assert current["stage_commit"] is not None
        assert current["plan_acceptance"]["status"] == "accepted"
        assert current["plan_acceptance"]["domain"]["status"] == "accepted"

        submission_ref = str(current["run"]["submission_ref"])
        stored_doc = runtime.owners.research_memory.query_plan_document(submission_ref)
        rg_decision = runtime.owners.research_graph.query_formal_plan_decision(submission_ref)
        assert stored_doc is not None and rg_decision is not None
        assert rg_decision.decision == "accepted"
        expected_accounting = stored_doc.plan_document["evidence_accounting"]
        discovered_by_status = {
            str(plan_skill.discovered_entries[0]["target_commit_root_ref"]): "adopted",
            str(plan_skill.discovered_entries[1]["target_commit_root_ref"]): "not_used",
        }
        assert set(discovered_by_status) == {commit1, commit2}
        assert {entry["target_commit_ref"]: entry["status"] for entry in expected_accounting} == discovered_by_status
        assert len(plan_skill.cycle2_requests) == 1
    finally:
        runtime.close()

    # Reopen from the same SQLite database and verify full persistence.
    reopened = _build_cycle_runtime(
        data_root,
        plan_skill=_Cycle2AccountingPlanSkill(mode="adopt_one_decline_one"),
        bundle_skill=_TwoTargetCurrentBundleSkill(),
        reasoning_skill=_Cycle1ReasoningSkill(entry_stage="plan"),
    )
    try:
        assert submission_ref is not None and expected_accounting is not None
        reloaded = reopened.owners.research_memory.query_plan_document(submission_ref)
        assert reloaded is not None
        assert reloaded.plan_document["evidence_accounting"] == expected_accounting
    finally:
        reopened.close()


def test_plan_evidence_accounting_boundary_diagnostics(tmp_path: Path) -> None:
    # Case A: omitted TargetCommit 2 -> RG rejects as incomplete and names missing ref.
    runtime_omit, plan_omit, _q1, fg1, (c1, c2) = _advance_to_cycle2_plan_request(
        tmp_path / "case-omit", mode="omit_second"
    )
    try:
        decision = None
        for _step in range(16):
            runtime_omit.plan_stage.process_once()
            current = runtime_omit.plan_stage.query_current()
            run_info = current.get("run") or {}
            assert not run_info.get("completion_rejection"), run_info.get("completion_rejection")
            acceptance = current["plan_acceptance"]
            if (acceptance.get("domain") or {}).get("status") == "rejected":
                decision = acceptance
                break
        assert decision is not None
        assert decision["domain"]["reason"]["code"] == "plan_evidence_accounting_incomplete"
        feedback_text = " ".join(decision["rejection"]["feedback"])
        omitted_ref = str(plan_omit.discovered_entries[1]["target_commit_root_ref"])
        assert {c1, c2} == {
            str(item["target_commit_root_ref"]) for item in plan_omit.discovered_entries
        }
        assert omitted_ref in feedback_text
    finally:
        runtime_omit.close()

    # Case B: unknown not_used TargetCommit -> Skill/RM let non-adopted pass to RG,
    # and RG rejects as plan_evidence_accounting_invalid naming the unknown entry.
    runtime_bad, plan_bad, _q2, fg2, _commits2 = _advance_to_cycle2_plan_request(
        tmp_path / "case-unknown", mode="unknown_not_used"
    )
    try:
        decision = None
        for _step in range(16):
            runtime_bad.plan_stage.process_once()
            current = runtime_bad.plan_stage.query_current()
            run_info = current.get("run") or {}
            assert not run_info.get("completion_rejection"), run_info.get("completion_rejection")
            acceptance = current["plan_acceptance"]
            if (acceptance.get("domain") or {}).get("status") == "rejected":
                decision = acceptance
                break
        assert decision is not None
        assert decision["domain"]["reason"]["code"] == "plan_evidence_accounting_invalid"
        feedback_text = " ".join(decision["rejection"]["feedback"])
        assert "target_commit_forged_unknown" in feedback_text
        assert "without a disposition" not in feedback_text

        # Case C: local adopted vs selected mismatch MUST still fail at validate_plan_document.
        req = plan_bad.cycle2_requests[0]
        base_doc = plan_bad.documents[-1]
        forged_doc = deepcopy(base_doc)
        second_entry = plan_bad.discovered_entries[1]
        forged_doc["evidence_accounting"] = [
            {
                "evidence_ref": plan_bad.discovered_entries[0]["evidence_ref"],
                "target_commit_ref": plan_bad.discovered_entries[0]["target_commit_root_ref"],
                "status": "adopted",
                "reason": "Adopted.",
            },
            {
                "evidence_ref": second_entry["evidence_ref"],
                "target_commit_ref": second_entry["target_commit_root_ref"],
                "status": "adopted",
                "reason": "Wrongly marked adopted without selected_evidence_bindings.",
            },
        ]
        with pytest.raises(PlanContractError, match="plan_evidence_accounting_invalid") as exc_info:
            validate_plan_document(
                forged_doc,
                question_ref=req.question_ref,
                idea_set_ref=req.idea_set_ref,
                context_pack_ref=req.context_pack_ref,
                context_pack_hash=req.context_pack_hash,
                accepted_idea_set=req.accepted_idea_set,
                evidence_by_ref={},
                evidence_reference_revision=0,
            )
        assert exc_info.value.diagnostic
        assert str(second_entry["target_commit_root_ref"]) in exc_info.value.diagnostic

        # Case D: duplicate evidence_accounting entry raises invalid with duplicate ref diagnostic.
        dup_doc = deepcopy(base_doc)
        dup_doc["evidence_accounting"] = [
            {
                "evidence_ref": plan_bad.discovered_entries[0]["evidence_ref"],
                "target_commit_ref": plan_bad.discovered_entries[0]["target_commit_root_ref"],
                "status": "adopted",
                "reason": "Adopted.",
            },
            {
                "evidence_ref": second_entry["evidence_ref"],
                "target_commit_ref": second_entry["target_commit_root_ref"],
                "status": "not_used",
                "reason": "Declined once.",
            },
            {
                "evidence_ref": second_entry["evidence_ref"],
                "target_commit_ref": second_entry["target_commit_root_ref"],
                "status": "not_used",
                "reason": "Duplicate entry.",
            },
        ]
        with pytest.raises(PlanContractError, match="plan_evidence_accounting_invalid") as dup_exc:
            validate_plan_document(
                dup_doc,
                question_ref=req.question_ref,
                idea_set_ref=req.idea_set_ref,
                context_pack_ref=req.context_pack_ref,
                context_pack_hash=req.context_pack_hash,
                accepted_idea_set=req.accepted_idea_set,
                evidence_by_ref={},
                evidence_reference_revision=0,
            )
        assert dup_exc.value.diagnostic
        assert str(second_entry["target_commit_root_ref"]) in dup_exc.value.diagnostic
    finally:
        runtime_bad.close()


def test_non_owner_effect_and_reconcile_admission_returns_rejected_without_reconcile_livelock(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "admission-livelock"),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        gateway = runtime.harnesses._gateway
        for root_kind, ops in (
            (
                "bundle",
                (
                    "research_graph.question_relations.record",
                    "research_graph.question_relations.record.reconcile",
                ),
            ),
            (
                "companion",
                (
                    "research_memory.literature.register",
                    "research_memory.literature.register.reconcile",
                ),
            ),
        ):
            conn, _ = gateway.issue_channel(
                run_ref=f"run-{root_kind}",
                attempt_ref=f"attempt-{root_kind}",
                root_session_ref=f"session-{root_kind}",
                fence_ref=f"fence-{root_kind}",
                capability_binding_hash="a" * 64,
                operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind],
                root_kind=root_kind,
                phase="primary",
            )
            for req_id, op_id in enumerate(ops, start=1):
                status, response = gateway.dispatch(
                    conn.token,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "tools/call",
                        "params": {"name": op_id, "arguments": {}},
                    },
                )
                assert status == 200 and response is not None
                result = response["result"]
                assert result["isError"] is True
                payload = result["structuredContent"]
                assert payload["code"] == "capability_unavailable"
                details = payload["details"]
                assert details["reason"] == "root_kind_not_owner"
                assert details["call_execution"] == "not_started"
                assert details["effect_status"] == "rejected"
                assert details["reconcile_same_effect"] is False
                assert details["next_action"] == "correct_input"
                assert details["retry_allowed"] is False
                assert details["operator_required"] is False
                assert details["recovery_owner"] == "agent"

        # Transient admission unavailability (non-owner-mismatch) stays not_started without reconcile.
        transient_gw = SemanticMcpGateway(
            (
                SemanticOperation(
                    "example.effect",
                    "example",
                    "Transient unavailable effect.",
                    lambda _c, _a: {},
                    {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"]},
                    {"type": "object"},
                    access_mode="effect",
                    reconciliation_operation_id="example.effect.reconcile",
                    unavailable_reason=lambda _c: "service_disabled",
                ),
                SemanticOperation(
                    "example.effect.reconcile",
                    "example",
                    "Reconcile.",
                    lambda _c, _a: {},
                    {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"]},
                    {"type": "object"},
                    access_mode="reconcile",
                ),
            ),
            error_audit_store=runtime.harnesses._gateway._error_audit_store,
        )
        t_conn, _ = transient_gw.issue_channel(
            run_ref="run-t",
            attempt_ref="att-t",
            root_session_ref="sess-t",
            fence_ref="fence-t",
            capability_binding_hash="b" * 64,
            operation_ids=("example.effect", "example.effect.reconcile"),
            root_kind="target",
            phase="execution",
        )
        _, t_resp = transient_gw.dispatch(
            t_conn.token,
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "example.effect", "arguments": {"effect_id": "e-1"}},
            },
        )
        t_details = t_resp["result"]["structuredContent"]["details"]
        assert t_details["call_execution"] == "not_started"
        assert t_details["effect_status"] == "not_started"
        assert t_details["reconcile_same_effect"] is False
        assert t_details["next_action"] != "reconcile"
    finally:
        runtime.close()


def test_readonly_bad_args_never_escalate_on_audit_failure_or_touch_audit_write_lock(
    tmp_path: Path,
) -> None:
    class _LockedAuditStore(ErrorAuditStore):
        def __init__(self, database, feed):
            super().__init__(database, feed)
            self.record_calls = 0

        def record(self, contract):
            self.record_calls += 1
            raise sqlite3.OperationalError("database is locked")

    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "readonly-audit"),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        broken_audit = _LockedAuditStore(runtime._database, runtime.feed)
        base_ops = tuple(
            op
            for op_id, op in runtime.harnesses._gateway._operations.items()
            if not op_id.startswith("tools.")
        )
        extra_ops = (
            SemanticOperation(
                "test.readonly.agent_error",
                "test",
                "Readonly op raising agent-recoverable error.",
                lambda _c, _a: (_ for _ in ()).throw(
                    SemanticMcpError("invalid_cursor", {"recovery_owner": "agent"})
                ),
                {"type": "object", "properties": {}},
                {"type": "object"},
                access_mode="read",
            ),
            SemanticOperation(
                "test.effect.handler_error",
                "test",
                "Effect op failing inside handler.",
                lambda _c, _a: (_ for _ in ()).throw(RuntimeError("boom")),
                {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"]},
                {"type": "object"},
                access_mode="effect",
                reconciliation_operation_id="test.effect.handler_error.reconcile",
            ),
            SemanticOperation(
                "test.effect.handler_error.reconcile",
                "test",
                "Reconcile test effect.",
                lambda _c, _a: {"status": "unknown_outcome"},
                {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"]},
                {"type": "object"},
                access_mode="reconcile",
            ),
        )
        gateway = SemanticMcpGateway(
            (*base_ops, *extra_ops),
            error_audit_store=broken_audit,
        )
        conn, _ = gateway.issue_channel(
            run_ref="run-ro",
            attempt_ref="att-ro",
            root_session_ref="sess-ro",
            fence_ref="fence-ro",
            capability_binding_hash="c" * 64,
            operation_ids=(
                *ROOT_AGENT_SEMANTIC_OPERATION_IDS["plan"],
                "test.readonly.agent_error",
                "test.effect.handler_error",
                "test.effect.handler_error.reconcile",
            ),
            root_kind="plan",
            phase="primary",
        )

        # 1) Read-only bad args must NOT touch ErrorAuditStore or escalate to operator.
        for req_id, op_name, args in (
            (1, "research_memory.content.read", {}),
            (2, "tools.catalog.page", {"limit": 101}),
            (3, "tools.call", {"operation_id": "research_memory.content.read"}),
        ):
            _, resp = gateway.dispatch(
                conn.token,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": op_name, "arguments": args},
                },
            )
            payload = resp["result"]["structuredContent"]
            assert payload["code"] == "semantic_input_schema_mismatch"
            details = payload["details"]
            assert details["operator_required"] is False
            assert details["recovery_owner"] == "agent"
            assert details["next_action"] == "correct_input"
            assert details["feedback_stage_agent"] is True
        assert broken_audit.record_calls == 0

        _, ro_handler_resp = gateway.dispatch(
            conn.token,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "test.readonly.agent_error", "arguments": {}},
            },
        )
        ro_details = ro_handler_resp["result"]["structuredContent"]["details"]
        assert broken_audit.record_calls == 0
        assert ro_details["audit_ref"] is None
        assert ro_details["audit_status"] == "skipped_read_only"
        assert ro_details["operator_required"] is False
        assert ro_details["recovery_owner"] == "agent"
        assert ro_details["next_action"] == "correct_input"

        # 3) Effect operation handler failure with broken audit MUST still escalate to operator.
        _, eff_resp = gateway.dispatch(
            conn.token,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "test.effect.handler_error", "arguments": {"effect_id": "eff-1"}},
            },
        )
        eff_details = eff_resp["result"]["structuredContent"]["details"]
        assert broken_audit.record_calls == 1
        assert eff_details["audit_status"] == "unavailable"
        assert eff_details["operator_required"] is True
        assert eff_details["recovery_owner"] == "operator"
    finally:
        runtime.close()


def test_schema_problem_enforces_oneof_anyof_pattern_const_minitems_with_field_diagnostics(
    tmp_path: Path,
) -> None:
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "schema-diagnostics"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_MultiRunIdeaSkill(),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        idea_req = runtime.owners.advancement_engine.query_idea_stage_request(str(quest["cycle_ref"]))
        assert idea_req is not None
        idea_run = runtime.owners.agent_runtime.query_idea_stage_run(idea_req.request_ref)
        assert idea_run is not None
        gateway = runtime.harnesses._gateway
        conn, _ = gateway.issue_channel(
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            root_session_ref=idea_run.root_session_ref,
            fence_ref=idea_run.fence_ref,
            capability_binding_hash=idea_run.runtime_binding_hash,
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["idea"],
            root_kind="idea",
            phase="primary",
        )

        def call_op(req_id: int, op_name: str, args: dict[str, object]) -> dict[str, object]:
            _, resp = gateway.dispatch(
                conn.token,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": op_name, "arguments": args},
                },
            )
            return resp["result"]

        # 1) pattern on research_graph.baselines.page
        res_pattern = call_op(1, "research_graph.baselines.page", {"query_revision": "not-64-hex"})
        assert res_pattern["isError"] is True
        p_payload = res_pattern["structuredContent"]
        assert p_payload["code"] == "semantic_input_schema_mismatch"
        assert p_payload["details"]["path"] == ["query_revision"]
        assert p_payload["details"]["requirement"] == "pattern"
        assert p_payload["details"]["expected"] == "^[0-9a-f]{64}$"

        # 2) minItems on research_graph.quest_goal.evolve
        res_min = call_op(
            2,
            "research_graph.quest_goal.evolve",
            {
                "effect_id": "eff-min",
                "decision": {
                    "expected_revision": "rev-1",
                    "conditions_basis": "cb-1",
                    "work_basis": "wb-1",
                    "cause": {"kind": "evidence", "evidence": []},
                    "replacement": {"goal": "g", "completion_criteria": "c"},
                    "criteria_review": "cr",
                    "judgment": "j",
                    "conditions": {"runtime_conditions_ref": "rc-1", "assessments": []},
                    "arrangements": {"decisions": []},
                    "following_direction": "next",
                },
            },
        )
        assert res_min["isError"] is True
        m_payload = res_min["structuredContent"]
        assert m_payload["code"] == "semantic_input_schema_mismatch"
        assert m_payload["details"]["path"] == ["decision", "cause", "evidence"]
        assert m_payload["details"]["requirement"] == "minItems"
        assert m_payload["details"]["expected"] == 1

        # 3) oneOf + const on research_memory.assets.intake
        # 3a) Nested oneOf branch selected by discriminator kind="workspace", missing path
        res_oneof_ws = call_op(
            3,
            "research_memory.assets.intake",
            {
                "effect_id": "eff-oneof-ws",
                "intake": {
                    "source_kind": "local_path",
                    "custody_mode": "managed",
                    "display_name": "sample",
                    "source": {"kind": "workspace", "workspace_ref": "ws-1"},
                },
            },
        )
        assert res_oneof_ws["isError"] is True
        ws_payload = res_oneof_ws["structuredContent"]
        assert ws_payload["code"] == "semantic_input_schema_mismatch"
        assert ws_payload["details"]["path"] == ["intake", "source", "path"]
        assert ws_payload["details"]["requirement"] == "required"

        # 3b) Unknown oneOf discriminator must not echo submitted secret field names/values
        res_oneof_bad = call_op(
            4,
            "research_memory.assets.intake",
            {
                "effect_id": "eff-oneof-bad",
                "intake": {
                    "source_kind": "local_path",
                    "custody_mode": "managed",
                    "display_name": "sample",
                    "source": {"kind": "bogus_kind", "secret": "canary-secret-xyz"},
                },
            },
        )
        assert res_oneof_bad["isError"] is True
        assert res_oneof_bad["structuredContent"]["code"] == "semantic_input_schema_mismatch"
        assert "canary-secret-xyz" not in json.dumps(res_oneof_bad)

        # 3c) Top-level oneOf + const: text/file MUST have custody_mode == "managed"
        res_const = call_op(
            5,
            "research_memory.assets.intake",
            {
                "effect_id": "eff-const",
                "intake": {
                    "source_kind": "text",
                    "custody_mode": "linked_local",
                    "display_name": "sample",
                    "text": "hello",
                },
            },
        )
        assert res_const["isError"] is True
        assert res_const["structuredContent"]["code"] == "semantic_input_schema_mismatch"

        # 4) anyOf + if/then on research_memory.assets.intake change.correction
        res_anyof = call_op(
            6,
            "research_memory.assets.intake",
            {
                "effect_id": "eff-anyof",
                "intake": {
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "sample",
                    "text": "updated",
                    "change": {
                        "kind": "correction",
                        "predecessor_version_ref": "v-1",
                        "expected_revision": 1,
                        "explanation": "fix typo",
                        "error": "typo",
                        "scope": "local",
                        "evidence_bindings": [
                            {
                                "asset_ref": "a-1",
                                "version_ref": "v-0",
                                "content_hash": "a" * 64,
                                "manifest_hash": "b" * 64,
                                "receipt": {
                                    "issuer": "research_memory",
                                    "kind": "asset_version_acceptance",
                                    "receipt_ref": "r-1",
                                    "subject_ref": "v-0",
                                    "payload_hash": "c" * 64,
                                },
                            }
                        ],
                    },
                },
            },
        )
        assert res_anyof["isError"] is True
        any_payload = res_anyof["structuredContent"]
        assert any_payload["code"] == "semantic_input_schema_mismatch"
        assert any_payload["details"]["path"][:2] == ["intake", "change"]

        # 5) const distinguishes bool True from int 1
        bool_const_gw = SemanticMcpGateway(
            (
                SemanticOperation(
                    "const.probe",
                    "test",
                    "Check const bool vs int.",
                    lambda _c, _a: {},
                    {
                        "type": "object",
                        "properties": {
                            "flag": {"const": True},
                            "count": {"const": 1},
                        },
                        "required": ["flag", "count"],
                        "additionalProperties": False,
                    },
                    {"type": "object"},
                ),
            )
        )
        c_conn, _ = bool_const_gw.issue_channel(
            run_ref="r", attempt_ref="a", root_session_ref="s", fence_ref="f",
            capability_binding_hash="e" * 64,
            operation_ids=("const.probe",),
            root_kind="plan", phase="primary",
        )
        _, bad_bool = bool_const_gw.dispatch(
            c_conn.token,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "const.probe", "arguments": {"flag": 1, "count": 1}}},
        )
        assert bad_bool["result"]["structuredContent"]["code"] == "semantic_input_schema_mismatch"
        assert bad_bool["result"]["structuredContent"]["details"]["path"] == ["flag"]
        assert bad_bool["result"]["structuredContent"]["details"]["requirement"] == "const"
        _, bad_int = bool_const_gw.dispatch(
            c_conn.token,
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "const.probe", "arguments": {"flag": True, "count": True}}},
        )
        assert bad_int["result"]["structuredContent"]["code"] == "semantic_input_schema_mismatch"
        assert bad_int["result"]["structuredContent"]["details"]["path"] == ["count"]
        assert bad_int["result"]["structuredContent"]["details"]["requirement"] == "const"

        # 6) exclusiveMinimum and uniqueItems
        extra_kw_gw = SemanticMcpGateway(
            (
                SemanticOperation(
                    "keywords.probe",
                    "test",
                    "Check exclusiveMinimum and uniqueItems.",
                    lambda _c, _a: {},
                    {
                        "type": "object",
                        "properties": {
                            "threshold": {"type": "number", "exclusiveMinimum": 0},
                            "items": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                        },
                        "required": ["threshold", "items"],
                        "additionalProperties": False,
                    },
                    {"type": "object"},
                ),
            )
        )
        k_conn, _ = extra_kw_gw.issue_channel(
            run_ref="rk", attempt_ref="ak", root_session_ref="sk", fence_ref="fk",
            capability_binding_hash="d" * 64,
            operation_ids=("keywords.probe",),
            root_kind="plan", phase="primary",
        )
        _, bad_excl = extra_kw_gw.dispatch(
            k_conn.token,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "keywords.probe", "arguments": {"threshold": 0, "items": ["a", "b"]}}},
        )
        assert bad_excl["result"]["structuredContent"]["details"]["path"] == ["threshold"]
        assert bad_excl["result"]["structuredContent"]["details"]["requirement"] == "exclusiveMinimum"
        _, bad_uniq = extra_kw_gw.dispatch(
            k_conn.token,
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "keywords.probe", "arguments": {"threshold": 0.5, "items": ["dup", "dup"]}}},
        )
        assert bad_uniq["result"]["structuredContent"]["details"]["path"] == ["items"]
        assert bad_uniq["result"]["structuredContent"]["details"]["requirement"] == "uniqueItems"
    finally:
        runtime.close()


def test_all_registered_production_mcp_schemas_pass_contract_validation(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "all-schemas"),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        operations = runtime.harnesses._gateway._operations
        assert len(operations) >= 90
        for op_id, operation in operations.items():
            assert _valid_schema_contract(operation.input_schema), op_id
            assert _valid_schema_contract(operation.output_schema), op_id
    finally:
        runtime.close()


def test_tools_catalog_overview_exposes_session_call_permissions(tmp_path: Path) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "catalog-overview"),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        gateway = runtime.harnesses._gateway
        for role in ("companion", "bundle", "target"):
            conn, _ = gateway.issue_channel(
                run_ref=f"run-{role}",
                attempt_ref=f"att-{role}",
                root_session_ref=f"sess-{role}",
                fence_ref=f"fence-{role}",
                capability_binding_hash="f" * 64,
                operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[role],
                root_kind=role,
                phase="primary",
            )
            _, resp = gateway.dispatch(
                conn.token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "tools.catalog.page", "arguments": {"view": "overview"}},
                },
            )
            assert resp is not None and resp["result"]["isError"] is False
            overview = resp["result"]["structuredContent"]
            assert overview["current_root_kind"] == role
            assert isinstance(overview["callable_total"], int)
            assert 0 < overview["callable_total"] < overview["total"]
            assert all(isinstance(item.get("callable_in_current_session"), bool) for item in overview["items"])
            by_purpose = {entry["purpose"]: entry for entry in overview["purposes"]}
            for entry in by_purpose.values():
                assert isinstance(entry["callable_in_current_session"], bool)
                assert entry["callable_operations"] + entry["inspectable_only_operations"] == entry["total_operations"]
            if role == "companion":
                assert by_purpose["WF"]["inspectable_only_operations"] > 0
                assert by_purpose["RG"]["inspectable_only_operations"] > 0
                assert by_purpose["RM"]["inspectable_only_operations"] > 0
    finally:
        runtime.close()
