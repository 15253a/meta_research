from __future__ import annotations

from copy import deepcopy

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_PROPOSAL_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    ReasoningContractError,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    plan_evidence_reuse_leaves,
    validate_autonomous_question_proposal,
    validate_reasoning_stage_output,
    validate_reasoning_transition,
    validate_scientific_outcome,
)


def _scientific_outcome(disposition: str = "affirmed") -> dict[str, object]:
    outcome: dict[str, object] = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": "scientific-outcome:1",
        "stage_run_request_ref": "stage-run-request:reasoning:1",
        "cycle_ref": "cycle:1",
        "question_ref": "question:1",
        "quest_ref": "quest:1",
        "goal_revision_ref": "goal-revision:1",
        "foreground_epoch": 7,
        "disposition": disposition,
        "claim": "The bounded evidence supports the accepted Question.",
        "evidence": [
            {
                "kind": "LiteratureRecord",
                "ref": "literature-record:1",
                "finding": "supporting",
            }
        ],
        "missing_evidence": [],
        "uncertainty_basis": [],
        "support_scope": ["The accepted Question within the frozen applicability scope."],
        "limitations": ["No inference is made outside the frozen Quest context."],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": ["literature-record:1"],
            "claim_scope": "The bounded association in the accepted literature.",
            "statement": "The literature supports an association, not an intervention.",
            "sufficiency_rationale": "No causal TargetCommit was frozen.",
            "confounders": ["No controlled intervention was frozen."],
        },
        "research_synthesis": {
            "cycle": {
                "cycle_ref": "cycle:1",
                "impact": "This Cycle adds one bounded accepted finding.",
            },
            "current_question": {
                "question_ref": "question:1",
                "prior_accepted_outcome_refs": ["scientific-outcome:prior"],
                "progress": "The current outcome narrows the prior uncertainty.",
            },
            "parent_questions": [
                {
                    "question_ref": "question:parent",
                    "impact": "material",
                    "statement": "The bounded result supports one parent branch.",
                }
            ],
            "quest": {
                "quest_ref": "quest:1",
                "goal_revision_ref": "goal-revision:1",
                "graph_revision_ref": "graph-revision:1",
                "impact": "One frozen Goal milestone gains bounded support.",
            },
        },
        "is_authoritative": False,
    }
    if disposition == "uncertain":
        outcome["claim"] = "Accepted evidence does not resolve the bounded Question."
        evidence = outcome["evidence"]
        assert isinstance(evidence, list)
        citation = evidence[0]
        assert isinstance(citation, dict)
        citation["finding"] = "partial"
        outcome["uncertainty_basis"] = [
            "Accepted results disagree within the bounded applicability scope."
        ]
    if disposition == "denied":
        outcome["claim"] = "The bounded evidence contradicts the accepted Question."
        evidence = outcome["evidence"]
        assert isinstance(evidence, list)
        citation = evidence[0]
        assert isinstance(citation, dict)
        citation["finding"] = "negative"
    if disposition == "insufficient_evidence":
        outcome["claim"] = None
        outcome["evidence"] = []
        outcome["missing_evidence"] = [
            "No accepted measurement covers the required comparison."
        ]
        causal = outcome["causal_interpretation"]
        assert isinstance(causal, dict)
        causal["attribution_basis_refs"] = []
    return outcome


def _research_context() -> dict[str, object]:
    return {
        "schema_ref": "meta-research/reasoning-research-context/v2",
        "cycle_ref": "cycle:1",
        "quest_ref": "quest:1",
        "question_ref": "question:1",
        "goal_revision_ref": "goal-revision:1",
        "quest_goal_revision": {
            "kind": "QuestGoalRevision",
            "quest_ref": "quest:1",
            "goal_revision_ref": "goal-revision:1",
        },
        "graph_binding": {
            "schema_ref": "meta-research/reasoning-graph-context/v1",
            "issuer": "research_graph",
            "quest_ref": "quest:1",
            "question_ref": "question:1",
            "graph_revision_ref": "graph-revision:1",
            "active_question_refs": ["question:1", "question:parent"],
            "parent_question_bindings": [
                {
                    "question_ref": "question:parent",
                    "parent_question_ref": None,
                    "question_receipt_ref": "rg-question-receipt:parent",
                }
            ],
            "prior_current_question_outcomes": [
                {
                    "cycle_ref": "cycle:prior",
                    "request_ref": "stage-request:prior",
                    "outcome_ref": "scientific-outcome:prior",
                    "disposition": "uncertain",
                    "outcome_receipt_ref": "rg-reasoning-receipt:prior",
                }
            ],
            "binding_ref": "reasoning-graph-context:1",
            "binding_hash": "a" * 64,
        },
        "causal_context": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
        },
        "upstream_stage_commit_refs": [
            "stage-commit:idea",
            "stage-commit:plan",
            "stage-commit:bundle",
        ],
    }


def _literature_closure() -> list[dict[str, object]]:
    return [
        {
            "kind": "LiteratureRecord",
            "ref": "literature-record:1",
            "evidence_basis": "verified_fulltext",
            "evidence_basis_ref": "reading-result:1",
        }
    ]


def _metric_result_closure() -> list[dict[str, object]]:
    return [
        {
            "kind": "MetricResult",
            "ref": "metric-result:1",
            "source_evaluation_attempt_ref": "evaluation-attempt:1",
            "research_graph_acceptance_receipt_ref": "receipt:metric-result:1",
            "formal_measurement_acceptance_receipt_ref": "receipt:measurement:1",
        }
    ]


def _diagnostic_closure(kind: str) -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "ref": "diagnostic:1",
            "source_subject_ref": "variant-run:1",
            "owner_acceptance_receipt_ref": "receipt:diagnostic:1",
        }
    ]


def _owner_receipt(
    *, issuer: str, kind: str, subject_ref: str
) -> dict[str, object]:
    return {
        "status": "accepted",
        "issuer": issuer,
        "kind": kind,
        "receipt_ref": f"receipt:{kind}:{subject_ref}",
        "subject_ref": subject_ref,
        "payload_hash": "a" * 64,
    }


def _plan_reuse_leaf(
    *, role: str, item_ref: str, role_ref: str, asset_version_ref: str
) -> dict[str, object]:
    source_kind = "VariantRun" if role == "CheckpointArtifact" else "EvaluationAttempt"
    source_ref = (
        "variant-run:prior"
        if source_kind == "VariantRun"
        else "evaluation-attempt:prior"
    )
    return {
        "schema_ref": "meta-research/evidence-reuse-leaf/v1",
        "kind": "EvidenceReuseLeaf",
        "role": role,
        "evidence_ref": "evidence:prior-target",
        "evidence_item_ref": item_ref,
        "source_role_ref": role_ref,
        "source_variant_run_ref": "variant-run:prior",
        "source_evaluation_attempt_ref": "evaluation-attempt:prior",
        "source_subject_kind": source_kind,
        "source_subject_ref": source_ref,
        "target_commit_ref": "target-commit:prior",
        "asset_version_ref": asset_version_ref,
        "evidence_catalog_entry_hash": "b" * 64,
        "evidence_use_hashes": ["c" * 64],
        "evidence_asset_receipt": _owner_receipt(
            issuer="research_memory",
            kind="asset_acceptance",
            subject_ref=asset_version_ref,
        ),
        "evidence_role_receipt": _owner_receipt(
            issuer="research_graph",
            kind="asset_role_acceptance",
            subject_ref=role_ref,
        ),
        "formal_measurement_acceptance_receipt": _owner_receipt(
            issuer="research_graph",
            kind="formal_measurement_acceptance",
            subject_ref="evaluation-attempt:prior",
        ),
        "target_commit_acceptance_receipt": _owner_receipt(
            issuer="research_graph",
            kind="target_commit",
            subject_ref="target-commit:prior",
        ),
    }


def _plan_reuse_context() -> dict[str, object]:
    evidence_use = {
        "obligation_key": "obligation:1",
        "evidence_ref": "evidence:prior-target",
        "supported_claim": "The prior accepted Target supports one bound.",
        "support_boundary": "Only the accepted prior Target.",
        "contributing_idea_refs": ["idea:1"],
    }
    leaves = [
        _plan_reuse_leaf(
            role="MetricResult",
            item_ref="metric-result:prior",
            role_ref="result-role:prior",
            asset_version_ref="asset-version:result",
        ),
        _plan_reuse_leaf(
            role="CheckpointArtifact",
            item_ref="checkpoint-role:prior",
            role_ref="checkpoint-role:prior",
            asset_version_ref="asset-version:checkpoint",
        ),
        _plan_reuse_leaf(
            role="LogAsset",
            item_ref="log-role:prior",
            role_ref="log-role:prior",
            asset_version_ref="asset-version:log",
        ),
        _plan_reuse_leaf(
            role="AnalysisAsset",
            item_ref="analysis-role:prior",
            role_ref="analysis-role:prior",
            asset_version_ref="asset-version:analysis",
        ),
    ]
    for leaf in leaves:
        leaf["evidence_use_hashes"] = [canonical_hash(evidence_use)]
    return {
        "plan_evidence_input": {
            "kind": "accepted",
            "formal_plan_binding": {"formal_plan_ref": "formal-plan:prior"},
            "evidence_reuse_set": [evidence_use],
            "evidence_reuse_closure": leaves,
        }
    }


def _autonomous_question_proposal() -> dict[str, object]:
    return {
        "schema_ref": AUTONOMOUS_QUESTION_PROPOSAL_SCHEMA_REF,
        "kind": "QuestionProposal",
        "creation_mode": "AutonomousCreation",
        "source_quest_ref": "quest:1",
        "source_cycle_ref": "cycle:1",
        "source_reasoning_stage_run_request_ref": "stage-run-request:reasoning:1",
        "source_scientific_outcome_ref": "scientific-outcome:1",
        "source_question_ref": "question:1",
        "source_foreground_epoch": 7,
        "question": {
            "title": "Which condition resolves the remaining uncertainty?",
            "unknown_statement": "It is unknown which condition is causal.",
            "answer_shape": "A bounded comparison with a falsifiable result.",
            "applicability_scope": "The current Quest and accepted dataset.",
            "background_context": "The prior accepted results do not converge.",
            "requirements_constraints": "Reuse only accepted evidence bindings.",
        },
        "is_authoritative": False,
    }


def _next_cycle_proposal() -> dict[str, object]:
    return {
        "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
        "kind": "NextCycleProposal",
        "source_quest_ref": "quest:1",
        "source_cycle_ref": "cycle:1",
        "source_reasoning_stage_run_request_ref": "stage-run-request:reasoning:1",
        "source_scientific_outcome_ref": "scientific-outcome:1",
        "source_question_ref": "question:1",
        "source_foreground_epoch": 7,
        "target_question_ref": "question:2",
        "target_question_anchor_ref": "question-anchor:2",
        "entry_stage": "idea",
        "typed_skip_basis_refs_by_stage": {},
        "is_authoritative": False,
    }


def _candidate_completion() -> dict[str, object]:
    return {
        "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
        "kind": "CandidateCompletion",
        "source_quest_ref": "quest:1",
        "source_cycle_ref": "cycle:1",
        "source_reasoning_stage_run_request_ref": "stage-run-request:reasoning:1",
        "source_scientific_outcome_ref": "scientific-outcome:1",
        "source_question_ref": "question:1",
        "source_foreground_epoch": 7,
        "current_quest_ref": "quest:1",
        "current_goal_revision_ref": "goal-revision:1",
        "completion_milestone_basis_refs": ["milestone:accepted:1"],
        "rationale": "The current accepted Goal milestones are satisfied.",
        "is_authoritative": False,
    }


def _stage_output(*, completion: bool = False) -> dict[str, object]:
    return {
        "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
        "scientific_outcome": _scientific_outcome(),
        "next_cycle_proposal": None if completion else _next_cycle_proposal(),
        "candidate_completion": _candidate_completion() if completion else None,
    }


def test_affirmed_accepts_a_frozen_literature_record() -> None:
    outcome_hash = validate_scientific_outcome(
        _scientific_outcome(),
        frozen_evidence_closure=_literature_closure(),
        frozen_research_context=_research_context(),
    )

    assert len(outcome_hash) == 64


def test_scientific_outcome_requires_complete_multiscale_synthesis() -> None:
    outcome = _scientific_outcome()
    synthesis = outcome["research_synthesis"]
    assert isinstance(synthesis, dict)
    synthesis["parent_questions"] = []

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_research_synthesis_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize(
    "disposition",
    ["affirmed", "denied", "uncertain", "insufficient_evidence"],
)
def test_exactly_four_scientific_outcomes_are_supported(disposition: str) -> None:
    outcome_hash = validate_scientific_outcome(
        _scientific_outcome(disposition),
        frozen_evidence_closure=_literature_closure(),
        frozen_research_context=_research_context(),
    )

    assert len(outcome_hash) == 64


@pytest.mark.parametrize("disposition", ["affirmed", "denied", "uncertain"])
def test_conclusive_or_uncertain_outcome_accepts_a_formal_metric_result(
    disposition: str,
) -> None:
    outcome = _scientific_outcome(disposition)
    outcome["evidence"] = [
        {
            "kind": "MetricResult",
            "ref": "metric-result:1",
            "finding": "partial" if disposition == "uncertain" else "supporting",
        }
    ]
    causal = outcome["causal_interpretation"]
    assert isinstance(causal, dict)
    causal["attribution_basis_refs"] = ["metric-result:1"]

    outcome_hash = validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=_metric_result_closure(),
        frozen_research_context=_research_context(),
    )

    assert len(outcome_hash) == 64


@pytest.mark.parametrize("kind", ["LogAsset", "AnalysisAsset", "CheckpointArtifact"])
@pytest.mark.parametrize("finding", ["supporting", "negative", "partial"])
def test_diagnostic_assets_cannot_masquerade_as_substantive_evidence(
    kind: str,
    finding: str,
) -> None:
    outcome = _scientific_outcome()
    outcome["evidence"] = [
        {"kind": kind, "ref": "diagnostic:1", "finding": finding}
    ]

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_evidence_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_diagnostic_closure(kind),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize("kind", ["LogAsset", "AnalysisAsset", "CheckpointArtifact"])
def test_scientific_outcome_accepts_contextual_diagnostic_with_substantive_evidence(
    kind: str,
) -> None:
    outcome = _scientific_outcome()
    evidence = outcome["evidence"]
    assert isinstance(evidence, list)
    evidence.append(
        {"kind": kind, "ref": "diagnostic:1", "finding": "context"}
    )

    outcome_hash = validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=[
            *_literature_closure(),
            *_diagnostic_closure(kind),
        ],
        frozen_research_context=_research_context(),
    )

    assert len(outcome_hash) == 64


@pytest.mark.parametrize("disposition", ["affirmed", "denied", "uncertain"])
@pytest.mark.parametrize("kind", ["LogAsset", "AnalysisAsset", "CheckpointArtifact"])
def test_diagnostic_context_alone_never_satisfies_substantive_gate(
    disposition: str,
    kind: str,
) -> None:
    outcome = _scientific_outcome(disposition)
    outcome["evidence"] = [
        {"kind": kind, "ref": "diagnostic:1", "finding": "context"}
    ]

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_substantive_evidence_missing",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_diagnostic_closure(kind),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize("kind", ["LogAsset", "AnalysisAsset", "CheckpointArtifact"])
def test_insufficient_evidence_preserves_available_diagnostic_context(
    kind: str,
) -> None:
    outcome = _scientific_outcome("insufficient_evidence")
    outcome["evidence"] = [
        {"kind": kind, "ref": "diagnostic:1", "finding": "context"}
    ]

    outcome_hash = validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=_diagnostic_closure(kind),
        frozen_research_context=_research_context(),
    )

    assert len(outcome_hash) == 64


def test_plan_reuse_preserves_metric_and_diagnostic_role_closure() -> None:
    leaves = plan_evidence_reuse_leaves(_plan_reuse_context())

    assert [leaf["kind"] for leaf in leaves] == [
        "MetricResult",
        "CheckpointArtifact",
        "LogAsset",
        "AnalysisAsset",
    ]
    assert [leaf["ref"] for leaf in leaves] == [
        "metric-result:prior",
        "checkpoint-role:prior",
        "log-role:prior",
        "analysis-role:prior",
    ]
    assert all(
        isinstance(leaf["owner_acceptance_receipt_ref"], str)
        for leaf in leaves[1:]
    )


@pytest.mark.parametrize(
    ("role", "forged_field", "forged_value"),
    [
        ("LogAsset", "source_subject_kind", "VariantRun"),
        ("AnalysisAsset", "source_subject_ref", "variant-run:prior"),
        (
            "CheckpointArtifact",
            "evidence_asset_receipt",
            _owner_receipt(
                issuer="research_memory",
                kind="asset_acceptance",
                subject_ref="asset-version:forged",
            ),
        ),
    ],
)
def test_plan_reuse_diagnostic_owner_or_source_forgery_fails_closed(
    role: str,
    forged_field: str,
    forged_value: object,
) -> None:
    context = _plan_reuse_context()
    plan_input = context["plan_evidence_input"]
    assert isinstance(plan_input, dict)
    closure = plan_input["evidence_reuse_closure"]
    assert isinstance(closure, list)
    leaf = next(item for item in closure if item["role"] == role)
    leaf[forged_field] = forged_value

    with pytest.raises(
        ReasoningContractError,
        match="reasoning_plan_evidence_closure_invalid",
    ):
        plan_evidence_reuse_leaves(context)


def test_autonomous_question_proposal_binds_source_and_all_six_fields() -> None:
    proposal_hash = validate_autonomous_question_proposal(
        _autonomous_question_proposal(),
        source_outcome=_scientific_outcome("uncertain"),
    )

    assert len(proposal_hash) == 64


def test_reasoning_transition_requires_exactly_one_candidate() -> None:
    outcome = _scientific_outcome()
    next_cycle = _next_cycle_proposal()
    completion = _candidate_completion()

    assert len(validate_reasoning_transition(outcome, next_cycle=next_cycle)) == 64
    assert (
        len(
            validate_reasoning_transition(
                outcome,
                candidate_completion=completion,
            )
        )
        == 64
    )
    with pytest.raises(ReasoningContractError, match="reasoning_transition_xor_invalid"):
        validate_reasoning_transition(outcome)
    with pytest.raises(ReasoningContractError, match="reasoning_transition_xor_invalid"):
        validate_reasoning_transition(
            outcome,
            next_cycle=next_cycle,
            candidate_completion=completion,
        )


@pytest.mark.parametrize("completion", [False, True])
def test_reasoning_stage_output_closes_outcome_and_exactly_one_transition(
    completion: bool,
) -> None:
    output_hash, outcome_hash, transition_hash = validate_reasoning_stage_output(
        _stage_output(completion=completion),
        frozen_evidence_closure=_literature_closure(),
        frozen_research_context=_research_context(),
    )

    assert len(output_hash) == len(outcome_hash) == len(transition_hash) == 64


def test_reasoning_stage_output_rejects_unknown_or_dual_transition_fields() -> None:
    output = _stage_output()
    output["latest_question"] = {"question_ref": "question:stale"}
    with pytest.raises(ReasoningContractError, match="reasoning_stage_output_invalid"):
        validate_reasoning_stage_output(
            output,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )

    output = _stage_output()
    output["candidate_completion"] = _candidate_completion()
    with pytest.raises(ReasoningContractError, match="reasoning_transition_xor_invalid"):
        validate_reasoning_stage_output(
            output,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )


def test_unknown_scientific_outcome_fails_closed() -> None:
    outcome = _scientific_outcome()
    outcome["disposition"] = "completed"

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_disposition_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )


def test_uncertain_requires_present_but_nonconvergent_evidence() -> None:
    outcome = _scientific_outcome("uncertain")
    outcome["evidence"] = []
    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_substantive_evidence_missing",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )

    outcome = _scientific_outcome("uncertain")
    outcome["uncertainty_basis"] = []
    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_uncertainty_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("claim", "This must not be a scientific claim."),
        ("missing_evidence", []),
        ("uncertainty_basis", ["Accepted evidence conflicts."]),
    ],
)
def test_insufficient_evidence_names_missing_evidence_only(
    field: str,
    invalid_value: object,
) -> None:
    outcome = _scientific_outcome("insufficient_evidence")
    outcome[field] = invalid_value

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_insufficient_evidence_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_literature_closure(),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize(
    "receipt_field",
    [
        "research_graph_acceptance_receipt_ref",
        "formal_measurement_acceptance_receipt_ref",
    ],
)
def test_metric_result_is_not_formal_without_owner_receipts(
    receipt_field: str,
) -> None:
    closure = _metric_result_closure()
    del closure[0][receipt_field]

    with pytest.raises(
        ReasoningContractError,
        match="reasoning_evidence_closure_invalid",
    ):
        validate_scientific_outcome(
            {
                **_scientific_outcome(),
                "evidence": [
                    {
                        "kind": "MetricResult",
                        "ref": "metric-result:1",
                        "finding": "supporting",
                    }
                ],
            },
            frozen_evidence_closure=closure,
            frozen_research_context=_research_context(),
        )


def test_candidate_cannot_relabel_a_frozen_diagnostic_asset() -> None:
    outcome = _scientific_outcome()
    outcome["evidence"] = [
        {
            "kind": "MetricResult",
            "ref": "diagnostic:1",
            "finding": "supporting",
        }
    ]

    with pytest.raises(
        ReasoningContractError,
        match="scientific_outcome_evidence_invalid",
    ):
        validate_scientific_outcome(
            outcome,
            frozen_evidence_closure=_diagnostic_closure("LogAsset"),
            frozen_research_context=_research_context(),
        )


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "unknown_statement",
        "answer_shape",
        "applicability_scope",
        "background_context",
        "requirements_constraints",
    ],
)
def test_autonomous_question_requires_each_formal_question_field(field: str) -> None:
    proposal = _autonomous_question_proposal()
    question = proposal["question"]
    assert isinstance(question, dict)
    del question[field]

    with pytest.raises(
        ReasoningContractError,
        match="autonomous_question_six_fields_incomplete",
    ):
        validate_autonomous_question_proposal(
            proposal,
            source_outcome=_scientific_outcome("uncertain"),
        )


@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("source_quest_ref", "quest:stale"),
        ("source_cycle_ref", "cycle:stale"),
        (
            "source_reasoning_stage_run_request_ref",
            "stage-run-request:reasoning:stale",
        ),
        ("source_scientific_outcome_ref", "scientific-outcome:stale"),
        ("source_question_ref", "question:stale"),
        ("source_foreground_epoch", 6),
    ],
)
def test_autonomous_question_rejects_a_stale_source_binding(
    field: str,
    stale_value: object,
) -> None:
    proposal = _autonomous_question_proposal()
    proposal[field] = stale_value

    with pytest.raises(
        ReasoningContractError,
        match="autonomous_question_source_binding_invalid",
    ):
        validate_autonomous_question_proposal(
            proposal,
            source_outcome=_scientific_outcome("uncertain"),
        )


def test_autonomous_question_cannot_silently_become_manual_creation() -> None:
    proposal = _autonomous_question_proposal()
    proposal["creation_mode"] = "ManualCreation"

    with pytest.raises(
        ReasoningContractError,
        match="autonomous_question_proposal_invalid",
    ):
        validate_autonomous_question_proposal(
            proposal,
            source_outcome=_scientific_outcome("uncertain"),
        )


@pytest.mark.parametrize(
    "field",
    [
        "source_quest_ref",
        "source_cycle_ref",
        "source_reasoning_stage_run_request_ref",
        "source_scientific_outcome_ref",
        "source_question_ref",
        "source_foreground_epoch",
        "current_quest_ref",
        "current_goal_revision_ref",
        "completion_milestone_basis_refs",
    ],
)
def test_candidate_completion_requires_complete_current_bindings(field: str) -> None:
    completion = _candidate_completion()
    del completion[field]

    with pytest.raises(ReasoningContractError):
        validate_reasoning_transition(
            _scientific_outcome(),
            candidate_completion=completion,
        )


@pytest.mark.parametrize(
    ("field", "stale_value"),
    [
        ("current_quest_ref", "quest:stale"),
        ("current_goal_revision_ref", "goal-revision:stale"),
    ],
)
def test_candidate_completion_rejects_stale_quest_or_goal(
    field: str,
    stale_value: str,
) -> None:
    completion = _candidate_completion()
    completion[field] = stale_value

    with pytest.raises(
        ReasoningContractError,
        match="candidate_completion_current_binding_invalid",
    ):
        validate_reasoning_transition(
            _scientific_outcome(),
            candidate_completion=completion,
        )


def test_candidate_completion_requires_nonempty_milestone_basis() -> None:
    completion = deepcopy(_candidate_completion())
    completion["completion_milestone_basis_refs"] = []

    with pytest.raises(
        ReasoningContractError,
        match="candidate_completion_basis_invalid",
    ):
        validate_reasoning_transition(
            _scientific_outcome(),
            candidate_completion=completion,
        )
