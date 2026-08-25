from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.owners.research_graph import EvidenceReuseLeaf
from meta_research.paths import prepare_data_root
from meta_research.reasoning_contract import (
    CANDIDATE_COMPLETION_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    ReasoningContractError,
)
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
    validate_reasoning_skill_draft,
)

from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _FixtureTargetCommitEvidenceAuthority,
    _install_fixture_plan_evidence,
)
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
)
from test_public_reasoning_stage import _research_synthesis


class _EvidenceReuseAuthority(_FixtureTargetCommitEvidenceAuthority):
    """Typed authority fixture; production uses TargetCommitEvidenceCatalog."""

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        evidence_reuse_set: list[dict[str, object]],
    ) -> tuple[EvidenceReuseLeaf, ...]:
        self.verify_plan_evidence_catalog(
            quest_ref=quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            require_current=False,
            require_complete=False,
            selected_evidence_refs=frozenset(
                str(item["evidence_ref"]) for item in evidence_reuse_set
            ),
        )
        evidence = evidence_catalog[0]
        target_commit_ref = str(evidence["target_commit_root_ref"])
        formal_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="formal_measurement_acceptance",
            receipt_ref="rg_formal_measurement_receipt_fixture",
            subject_ref="evaluation_attempt_fixture",
            payload_hash=canonical_hash({"formal": "fixture"}),
        )
        target_receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="target_commit",
            receipt_ref="rg_target_commit_receipt_fixture",
            subject_ref=target_commit_ref,
            payload_hash=canonical_hash({"target": "fixture"}),
        )
        common = {
            "evidence_ref": str(evidence["evidence_ref"]),
            "source_variant_run_ref": "variant_run_fixture",
            "source_evaluation_attempt_ref": "evaluation_attempt_fixture",
            "target_commit_ref": target_commit_ref,
            "evidence_catalog_entry_hash": canonical_hash(evidence),
            "evidence_use_hashes": tuple(
                canonical_hash(item) for item in evidence_reuse_set
            ),
            "formal_measurement_acceptance_receipt": formal_receipt,
            "target_commit_acceptance_receipt": target_receipt,
        }

        def leaf(
            *,
            role: str,
            item_ref: str,
            subject_kind: str,
            subject_ref: str,
        ) -> EvidenceReuseLeaf:
            asset_version_ref = f"asset_version_{role.lower()}_fixture"
            role_ref = f"role_{role.lower()}_fixture"
            return EvidenceReuseLeaf(
                **common,
                role=role,  # type: ignore[arg-type]
                evidence_item_ref=item_ref,
                source_role_ref=role_ref,
                source_subject_kind=subject_kind,  # type: ignore[arg-type]
                source_subject_ref=subject_ref,
                asset_version_ref=asset_version_ref,
                evidence_asset_receipt=AcceptanceReceipt(
                    issuer="research_memory",
                    kind="asset_acceptance",
                    receipt_ref=f"rm_receipt_{role.lower()}_fixture",
                    subject_ref=asset_version_ref,
                    payload_hash=canonical_hash({"asset": role}),
                ),
                evidence_role_receipt=AcceptanceReceipt(
                    issuer="research_graph",
                    kind="experiment_asset_role_acceptance",
                    receipt_ref=f"rg_role_receipt_{role.lower()}_fixture",
                    subject_ref=role_ref,
                    payload_hash=canonical_hash({"role": role}),
                ),
            )

        return (
            leaf(
                role="MetricResult",
                item_ref="metric_result_fixture",
                subject_kind="EvaluationAttempt",
                subject_ref="evaluation_attempt_fixture",
            ),
            leaf(
                role="CheckpointArtifact",
                item_ref="checkpoint_role_fixture",
                subject_kind="VariantRun",
                subject_ref="variant_run_fixture",
            ),
            leaf(
                role="LogAsset",
                item_ref="log_role_fixture",
                subject_kind="EvaluationAttempt",
                subject_ref="evaluation_attempt_fixture",
            ),
            leaf(
                role="AnalysisAsset",
                item_ref="analysis_role_fixture",
                subject_kind="EvaluationAttempt",
                subject_ref="evaluation_attempt_fixture",
            ),
        )


def _receipt(value: object) -> AcceptanceReceipt:
    assert isinstance(value, dict)
    return AcceptanceReceipt(
        issuer=str(value["issuer"]),
        kind=str(value["kind"]),
        receipt_ref=str(value["receipt_ref"]),
        subject_ref=str(value["subject_ref"]),
        payload_hash=str(value["payload_hash"]),
    )


class _MetricReuseReasoningSkill:
    def __init__(self) -> None:
        self.requests: list[ReasoningSkillRequest] = []

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        return ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "metric-reuse"}),
            instruction_set_hash=canonical_hash(
                {"instructions": "metric-reuse"}
            ),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def _output(self, request: ReasoningSkillRequest) -> dict[str, object]:
        metric = next(
            item
            for item in request.frozen_evidence_closure
            if item.get("kind") == "MetricResult"
        )
        log = next(
            item
            for item in request.frozen_evidence_closure
            if item.get("kind") == "LogAsset"
        )
        outcome_ref = "scientific-outcome:" + canonical_hash(
            {
                "stage_request_ref": request.stage_request_ref,
                "attempt_ref": request.attempt_ref,
            }
        )[:24]
        outcome = {
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
            "claim": "The accepted reused metric supports the bounded claim.",
            "evidence": [
                {
                    "kind": "MetricResult",
                    "ref": metric["ref"],
                    "finding": "supporting",
                },
                {
                    "kind": "LogAsset",
                    "ref": log["ref"],
                    "finding": "context",
                },
            ],
            "missing_evidence": [],
            "uncertainty_basis": [],
            "support_scope": [
                "The exact MetricResult selected by the accepted FormalPlan."
            ],
            "limitations": [
                "No evidence outside the frozen Plan catalog is considered."
            ],
            "causal_interpretation": {
                **request.context_pack["research_context"]["causal_context"],
                "attribution_basis_refs": [str(metric["ref"])],
                "claim_scope": "The accepted Plan evidence-use boundary.",
                "statement": "The reused metric supports a bounded association.",
                "sufficiency_rationale": (
                    "The FormalPlan selected an issuer-accepted MetricResult."
                ),
                "confounders": [
                    "The prior Target protocol limits generalization."
                ],
            },
            "research_synthesis": _research_synthesis(request),
            "is_authoritative": False,
        }
        completion = {
            "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
            "kind": "CandidateCompletion",
            "source_quest_ref": request.quest_ref,
            "source_cycle_ref": request.cycle_ref,
            "source_reasoning_stage_run_request_ref": request.stage_request_ref,
            "source_scientific_outcome_ref": outcome_ref,
            "source_question_ref": request.question_ref,
            "source_foreground_epoch": request.foreground_epoch,
            "current_quest_ref": request.quest_ref,
            "current_goal_revision_ref": request.goal_revision_ref,
            "completion_milestone_basis_refs": [
                item["commit_ref"]
                for item in request.context_pack["upstream_stage_closure"]
            ],
            "rationale": "The accepted reused metric closes the current goal.",
            "is_authoritative": False,
        }
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": outcome,
            "next_cycle_proposal": None,
            "candidate_completion": completion,
        }

    def generate_draft(self, request: ReasoningSkillRequest) -> ReasoningSkillDraft:
        self.requests.append(request)
        return ReasoningSkillDraft(
            draft=self._output(request),
            primary_session_ref="metric-reuse-primary",
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningSkillResult:
        output = draft.draft
        outcome = output["scientific_outcome"]
        completion = output["candidate_completion"]
        assert isinstance(outcome, dict)
        assert isinstance(completion, dict)
        return ReasoningSkillResult(
            reviewed_draft=output,
            scientific_outcome=outcome,
            next_cycle_proposal=None,
            candidate_completion=completion,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref="metric-reuse-reviewer",
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        return self.review_draft(request, self.generate_draft(request))


def _runtime(path: Path, authority, provider):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_DeterministicIdeaSkill(),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=True),
        bundle_skill_provider=_DeterministicBundleSkill(),
        reasoning_skill_provider=provider,
        target_commit_evidence_authority=authority,
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
    )
    if runtime.harnesses.query_status()["status"] != "ready":
        runtime.harnesses.start_full_conformance(_full_request())
        for _turn in range(4):
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
    return runtime


def _finish_plan_and_skipped_bundle(runtime) -> None:
    for _step in range(16):
        if runtime.plan_stage.query_current()["stage_commit"] is not None:
            break
        assert runtime.plan_stage.process_once()
    else:
        raise AssertionError("Plan did not complete")
    for _step in range(8):
        if runtime.bundle_stage.query_current()["stage_commit"] is not None:
            return
        assert runtime.bundle_stage.process_once()
    raise AssertionError("No-gap Bundle did not skip")


def test_completed_plan_reuse_metric_is_frozen_for_reasoning_and_restart(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "reasoning-plan-evidence-reuse"
    authority = _EvidenceReuseAuthority()
    provider = _MetricReuseReasoningSkill()
    runtime = _runtime(data_root, authority, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        _install_fixture_plan_evidence(
            runtime,
            authority,
            quest_ref=str(quest["quest_ref"]),
        )
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)

        bundle_request = (
            runtime.owners.advancement_engine.query_bundle_stage_request(
                str(quest["cycle_ref"])
            )
        )
        assert bundle_request is not None
        accepted_plan = bundle_request.accepted_formal_plan
        assert accepted_plan is not None
        forged_document = deepcopy(accepted_plan.plan_document)
        forged_document["evidence_reuse_set"][0]["evidence_ref"] = (
            "forged-evidence-ref"
        )
        with pytest.raises(OwnerConflict, match="bundle_formal_plan_binding_invalid"):
            runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
                quest_ref=str(quest["quest_ref"]),
                accepted_formal_plan=replace(
                    accepted_plan,
                    plan_document=forged_document,
                ),
            )
        with pytest.raises(OwnerConflict, match="bundle_formal_plan_binding_invalid"):
            runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
                quest_ref=str(quest["quest_ref"]),
                accepted_formal_plan=replace(
                    accepted_plan,
                    formal_plan_receipt=replace(
                        accepted_plan.formal_plan_receipt,
                        receipt_ref="forged-formal-plan-receipt",
                    ),
                ),
            )

        assert runtime.reasoning_stage.process_once()
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(
            str(quest["cycle_ref"])
        )
        assert request is not None
        plan_input = request.context_pack["plan_evidence_input"]
        assert plan_input["kind"] == "accepted"
        reuse_closure = plan_input["evidence_reuse_closure"]
        assert [leaf["role"] for leaf in reuse_closure] == [
            "MetricResult",
            "CheckpointArtifact",
            "LogAsset",
            "AnalysisAsset",
        ]
        assert request.context_pack["question_literature_input"] == {"kind": "none"}
        assert request.context_pack["accepted_target_commit_closures"] == []

        for _step in range(6):
            current = runtime.reasoning_stage.query_current()
            if current["reasoning_acceptance"]["status"] == "accepted":
                break
            assert runtime.reasoning_stage.process_once()
        assert provider.requests
        evidence = provider.requests[0].frozen_evidence_closure
        assert [item["kind"] for item in evidence] == [
            "MetricResult",
            "CheckpointArtifact",
            "LogAsset",
            "AnalysisAsset",
        ]
        assert current["reasoning_acceptance"]["disposition"] == "affirmed"

        original_request = provider.requests[0]
        original_draft = ReasoningSkillDraft(
            draft=provider._output(original_request),
            primary_session_ref="metric-reuse-primary",
            adapter_kind="test_deterministic",
        )
        for field, forged_value in (
            ("evidence_item_ref", "forged-metric-result"),
            (
                "target_commit_acceptance_receipt",
                {
                    **request.context_pack["plan_evidence_input"][
                        "evidence_reuse_closure"
                    ][0]["target_commit_acceptance_receipt"],
                    "receipt_ref": "forged-target-commit-receipt",
                },
            ),
        ):
            forged_context = deepcopy(original_request.context_pack)
            forged_context["plan_evidence_input"]["evidence_reuse_closure"][0][
                field
            ] = forged_value
            forged_request = replace(
                original_request,
                context_pack=forged_context,
                context_pack_hash=canonical_hash(forged_context),
            )
            with pytest.raises(
                ReasoningContractError,
                match="reasoning_plan_evidence_closure_invalid",
            ):
                validate_reasoning_skill_draft(
                    forged_request,
                    original_draft,
                )
    finally:
        runtime.close()

    restarted_authority = _EvidenceReuseAuthority()
    restarted_authority.quest_ref = authority.quest_ref
    restarted_authority.catalog = authority.catalog
    restarted = _runtime(
        data_root,
        restarted_authority,
        _MetricReuseReasoningSkill(),
    )
    try:
        recovered = restarted.owners.advancement_engine.query_reasoning_stage_request(
            str(quest["cycle_ref"])
        )
        assert recovered is not None
        assert recovered.context_pack == request.context_pack
        assert restarted.reasoning_stage.query_current()["reasoning_acceptance"][
            "disposition"
        ] == "affirmed"
    finally:
        restarted.close()
