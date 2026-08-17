#!/usr/bin/env python3
"""Fixture tests for the Reasoning Stage MVP; no production Owner is invoked."""

from __future__ import annotations

import unittest
from typing import Any, Dict, Mapping, Optional

from reasoning_stage_mvp import (
    FailClosed,
    OwnerReply,
    build_scientific_outcome,
    choose_reasoning_transition,
    create_question_then_propose_next_cycle,
    make_candidate_completion,
    make_next_cycle_proposal,
    submit_answer_with_feedback,
    submit_confirmed_completion_candidate,
)


def question_anchor(question_number: str = "42") -> Dict[str, Any]:
    return {
        "kind": "QuestionAnchor",
        "ref": f"question-anchor/{question_number}",
        "question_ref": f"question/{question_number}",
        "formal_question_content_ref": f"formal-question-content/{question_number}",
        "content_hash": f"sha256:question-{question_number}",
        "schema_ref": "formal-question-schema/v1",
        "rm_acceptance_receipt_ref": f"rm-receipt/question-{question_number}",
        "question_accepted_receipt_ref": f"rg-receipt/question-{question_number}",
    }


def selectable_target(
    question_number: str = "42",
    *,
    presence: str = "present",
    research_state: str = "open",
    graph_revision: str = "graph-revision/12",
    quest_ref: str = "quest/3",
    is_current: Optional[bool] = True,
) -> Dict[str, Any]:
    question_ref = f"question/{question_number}"
    anchor = question_anchor(question_number)
    anchor["QuestionProposal"] = {"local_id": "never-export-this"}
    anchor["QuestProposal"] = {"local_id": "never-export-this"}
    return {
        "question_anchor": anchor,
        "graph_presence_fact": {
            "kind": "GraphPresenceFact",
            "ref": f"graph-presence-fact/{question_number}/12",
            "question_ref": question_ref,
            "quest_ref": quest_ref,
            "graph_revision_ref": graph_revision,
            "value": presence,
            "is_current": is_current,
            "blocked_by": ["question/never-export-this"],
        },
        "question_research_state_fact": {
            "kind": "QuestionResearchStateFact",
            "ref": f"question-research-state-fact/{question_number}/12",
            "question_ref": question_ref,
            "quest_ref": quest_ref,
            "graph_revision_ref": graph_revision,
            "value": research_state,
            "is_current": is_current,
            "active": True,
            "CycleStartProposal": {"local_id": "never-export-this"},
        },
        # Deliberately irrelevant: the Reasoning interface must not copy these.
        "blocked_by": ["question/never-export-this"],
        "dependency_route": "never-export-this",
    }


def accepted_target_closure(
    name: str,
    *,
    changed_axes: Optional[list[str]] = None,
    held_fixed: Optional[list[str]] = None,
    with_checkpoint: bool = True,
    with_diagnostics: bool = False,
) -> Dict[str, Any]:
    target_commit_ref = f"target-commit/{name}"
    variant_run_ref = f"variant-run/{name}"
    attempt_ref = f"evaluation-attempt/{name}"

    def role_asset(role: str, *, subject_field: str, subject_ref: str) -> Dict[str, str]:
        slug = role.lower()
        return {
            "role_ref": f"{slug}/{name}",
            "memory_ref": f"memory/{slug}/{name}",
            subject_field: subject_ref,
            "rm_asset_receipt_ref": f"rm-receipt/{slug}/{name}",
            "rg_role_receipt_ref": f"rg-receipt/{slug}/{name}",
        }

    checkpoints = []
    if with_checkpoint:
        checkpoint = role_asset(
            "checkpoint-artifact",
            subject_field="selected_by_evaluation_attempt_ref",
            subject_ref=attempt_ref,
        )
        checkpoint["produced_by_variant_run_ref"] = variant_run_ref
        checkpoint["selected_by_target_commit_ref"] = target_commit_ref
        checkpoints.append(checkpoint)
    logs = []
    analyses = []
    if with_diagnostics:
        log = role_asset(
            "log-asset",
            subject_field="selected_by_target_commit_ref",
            subject_ref=target_commit_ref,
        )
        log["source_subject_kind"] = "VariantRun"
        log["source_subject_ref"] = variant_run_ref
        logs.append(log)
        analysis = role_asset(
            "analysis-asset",
            subject_field="selected_by_target_commit_ref",
            subject_ref=target_commit_ref,
        )
        analysis["source_subject_kind"] = "EvaluationAttempt"
        analysis["source_subject_ref"] = attempt_ref
        analyses.append(analysis)
    return {
        "accepted": True,
        "experiment_key": f"experiment-key/{name}",
        "target_commit_ref": target_commit_ref,
        "semantic_chain": {
            "target_ref": f"target/{name}",
            "baseline_ref": "baseline/shared",
            "variant_ref": f"variant/{name}",
            "variant_run_ref": variant_run_ref,
            "evaluation_ref": f"evaluation/{name}",
            "protocol_version_ref": "protocol-version/frozen-1",
            "evaluation_attempt_ref": attempt_ref,
        },
        "comparison_semantics": {
            "changed_axis_fact_refs": changed_axes or [f"causal-axis/{name}"],
            "held_fixed_fact_refs": held_fixed or ["held-fixed/protocol-1"],
            "provenance_refs": [f"provenance/{name}"],
        },
        "execution_input_bindings": [
            {
                "subject_kind": "VariantRun",
                "subject_ref": variant_run_ref,
                "binding_ref": f"execution-input-binding/variant-run/{name}",
                "causal_inputs": [
                    {
                        "input_ref": f"implementation-revision/{name}",
                        "asset_version_ref": f"asset-version/implementation/{name}",
                        "rm_asset_receipt_ref": f"rm-receipt/implementation/{name}",
                    },
                    {
                        "input_ref": f"training-data/{name}",
                        "asset_version_ref": f"asset-version/training-data/{name}",
                        "rm_asset_receipt_ref": f"rm-receipt/training-data/{name}",
                    },
                ],
                "rg_binding_receipt_ref": f"rg-receipt/binding/variant-run/{name}",
                "ar_execution_receipt_ref": f"ar-receipt/variant-run/{name}",
            },
            {
                "subject_kind": "EvaluationAttempt",
                "subject_ref": attempt_ref,
                "binding_ref": f"execution-input-binding/evaluation-attempt/{name}",
                "causal_inputs": [
                    {
                        "input_ref": "protocol-version/frozen-1",
                        "asset_version_ref": "asset-version/protocol/frozen-1",
                        "rm_asset_receipt_ref": "rm-receipt/protocol/frozen-1",
                    },
                    {
                        "input_ref": f"evaluation-data/{name}",
                        "asset_version_ref": f"asset-version/evaluation-data/{name}",
                        "rm_asset_receipt_ref": f"rm-receipt/evaluation-data/{name}",
                    },
                ],
                "rg_binding_receipt_ref": f"rg-receipt/binding/evaluation-attempt/{name}",
                "ar_execution_receipt_ref": f"ar-receipt/evaluation-attempt/{name}",
            },
        ],
        "asset_roles": {
            "metric_result": role_asset(
                "metric-result",
                subject_field="evaluation_attempt_ref",
                subject_ref=attempt_ref,
            ),
            "checkpoint_artifacts": checkpoints,
            "selected_logs": logs,
            "selected_analyses": analyses,
        },
        "formal_measurement_acceptance": {
            "receipt_ref": f"rg-receipt/formal-measurement/{name}",
            "evaluation_attempt_ref": attempt_ref,
        },
        "target_commit_acceptance": {
            "receipt_ref": f"rg-receipt/target-commit/{name}",
            "target_commit_ref": target_commit_ref,
        },
    }


def request_fixture() -> Dict[str, Any]:
    return {
        "type": "StageRunRequest",
        "ref": "stage-run-request/reasoning-7",
        "foreground_epoch_ref": "foreground-epoch/7",
        "stage": "Reasoning",
        "is_current": True,
        "question_ref": "question/42",
        "root_session_ref": "agent-session/reasoning-7",
        "accepted_question_binding": {
            "kind": "AcceptedQuestionBinding",
            "ref": "accepted-question-binding/reasoning-7",
            "currentness_fact_ref": "graph-currentness-fact/reasoning-7",
            "question_anchor": question_anchor(),
        },
        "frozen": {
            "upstream_stage_closure": [
                {
                    "stage": "Idea",
                    "stage_commit_ref": "stage-commit/cycle-7/idea",
                    "outcome": "Completed",
                },
                {
                    "stage": "Plan",
                    "stage_commit_ref": "stage-commit/cycle-7/plan",
                    "outcome": "Completed",
                },
                {
                    "stage": "Bundle",
                    "stage_commit_ref": "stage-commit/cycle-7/bundle",
                    "outcome": "Completed",
                },
            ],
            "plan_evidence_input": {
                "kind": "accepted",
                "formal_plan_ref": "formal-plan/2",
                "evidence_reuse_set_ref": "reuse-set/9",
                "evidence_reuse_leaves": [
                    {
                        "ref": "reuse-evidence/1",
                        "role": "MetricResult",
                        "asset_version_ref": "asset-version/history-metric-1",
                        "target_commit_root_ref": "target-commit/history-1",
                        "source_evaluation_attempt_ref": "evaluation-attempt/history-1",
                        "source_variant_run_ref": "variant-run/history-1",
                        "source_subject_kind": "EvaluationAttempt",
                        "source_subject_ref": "evaluation-attempt/history-1",
                        "provenance_closure_refs": [
                            "reuse-evidence/1",
                            "asset-version/history-metric-1",
                            "target-commit/history-1",
                            "evaluation-attempt/history-1",
                            "variant-run/history-1",
                            "rg-receipt/target-commit/history-1",
                            "rg-receipt/formal-measurement/history-1",
                            "rg-receipt/role/history-metric-1",
                        ],
                        "capabilities": ["supports metric comparison"],
                        "eligibility_token_ref": "rg-eligibility/history-1",
                        "integrity_receipt_ref": "rm-integrity/history-1",
                        "availability_receipt_ref": "rm-availability/history-1",
                        "currentness_receipt_ref": "rg-currentness/history-1",
                        "source_target_commit_acceptance_receipt_ref": (
                            "rg-receipt/target-commit/history-1"
                        ),
                        "source_formal_measurement_acceptance_receipt_ref": (
                            "rg-receipt/formal-measurement/history-1"
                        ),
                        "source_role_acceptance_receipt_ref": (
                            "rg-receipt/role/history-metric-1"
                        ),
                        "supported_claim": (
                            "historical metric value under the frozen protocol"
                        ),
                        "support_boundary": "only that accepted attempt",
                    }
                ],
            },
            "question_literature_input": {
                "kind": "revision",
                "revision_ref": "question-literature-revision/4",
                "records": [
                    {
                        "ref": "literature-record/2",
                        "evidence_basis": "citation_context",
                        "evidence_basis_ref": "literature-observation/2/citation-context",
                    }
                ],
            },
            "research_context": {
                "ref": "reasoning-research-context/7",
                "hash": "sha256:reasoning-research-context-7",
                "current_cycle_ref": "research-cycle/7",
                "current_question_ref": "question/42",
                "prior_question_outcome_refs": ["scientific-outcome/42/1"],
                "parent_question_refs": ["question/7"],
                "active_graph_snapshot_ref": "active-graph-snapshot/11",
                "quest_ref": "quest/3",
                "goal_revision_ref": "goal-revision/5",
            },
            "accepted_target_commit_closures": [
                accepted_target_closure(
                    "negative-control",
                    changed_axes=[
                        "causal-axis/model-change",
                        "causal-axis/training-change",
                    ],
                    with_diagnostics=True,
                ),
                accepted_target_closure(
                    "partial-replication",
                    changed_axes=["causal-axis/replication"],
                    with_checkpoint=False,
                ),
            ],
            "bundle_replan_candidates": [],
        },
    }


def evidence_fixture() -> list[Dict[str, Any]]:
    return [
        {
            "ref": "reuse-evidence/1",
            "kind": "EvidenceReuseLeaf",
            "finding": "context",
        },
        {
            "ref": "metric-result/negative-control",
            "kind": "TargetClosureLeaf",
            "role": "MetricResult",
            "source_target_commit_ref": "target-commit/negative-control",
            "source_evaluation_attempt_ref": "evaluation-attempt/negative-control",
            "finding": "negative",
        },
        {
            "ref": "metric-result/partial-replication",
            "kind": "TargetClosureLeaf",
            "role": "MetricResult",
            "source_target_commit_ref": "target-commit/partial-replication",
            "source_evaluation_attempt_ref": "evaluation-attempt/partial-replication",
            "finding": "partial",
        },
    ]


def outcome_proposal(**overrides: Any) -> Dict[str, Any]:
    proposal = {
        "disposition": "uncertain",
        "claim": "The effect is not established across tested conditions.",
        "support_scope": "negative control plus a partial replication",
        "limitations": ["the conclusion is limited to tested conditions"],
        "missing_evidence": [],
        "uncertainty_basis": [
            "accepted measurements do not support one stable direction across conditions"
        ],
        "causal_interpretation": {
            "target_commit_refs": [
                "target-commit/negative-control",
                "target-commit/partial-replication",
            ],
            "changed_axis_fact_refs": [
                "causal-axis/model-change",
                "causal-axis/training-change",
                "causal-axis/replication",
            ],
            "held_fixed_fact_refs": ["held-fixed/protocol-1"],
            "provenance_refs": [
                "provenance/negative-control",
                "provenance/partial-replication",
            ],
            "claim_scope": "joint effects in the tested comparisons",
            "attribution_basis_refs": [
                "metric-result/negative-control",
                "metric-result/partial-replication",
            ],
            "statement": "The observed joint effects do not isolate one axis.",
            "sufficiency_rationale": (
                "The frozen comparisons support a bounded joint interpretation, "
                "not an axis-specific attribution."
            ),
            "confounders": ["model and training changes co-vary in one comparison"],
        },
        "bundle_replan_interpretations": [],
        "research_synthesis": {
            "context_ref": "reasoning-research-context/7",
            "scope_refs": [
                "research-cycle/7",
                "question/42",
                "scientific-outcome/42/1",
                "question/7",
                "quest/3",
                "goal-revision/5",
            ],
            "narrative": (
                "This Cycle adds a negative control and only a partial replication. "
                "Across Cycles the current Question remains uncertain; that leaves "
                "the parent Question unresolved and the Quest Goal unmet."
            ),
            "uncertainties": ["observed effects differ across tested conditions"],
        },
    }
    proposal.update(overrides)
    return proposal


def no_scientific_input_request() -> Dict[str, Any]:
    request = request_fixture()
    request["frozen"]["upstream_stage_closure"] = [
        {
            "stage": "Idea",
            "stage_commit_ref": "stage-commit/cycle-7/idea",
            "outcome": "Exhausted",
            "exhaustion_proposal_ref": "exhaustion-proposal/idea/7",
            "exhaustion_evidence_refs": ["exhaustion-evidence/idea/7"],
        },
        {
            "stage": "Plan",
            "stage_commit_ref": "stage-commit/cycle-7/plan",
            "outcome": "Skipped",
            "typed_basis_refs": ["stage-commit/cycle-7/idea"],
        },
        {
            "stage": "Bundle",
            "stage_commit_ref": "stage-commit/cycle-7/bundle",
            "outcome": "Skipped",
            "typed_basis_refs": ["stage-commit/cycle-7/idea"],
        },
    ]
    request["frozen"]["plan_evidence_input"] = {
        "kind": "none",
        "basis_stage_commit_refs": [
            "stage-commit/cycle-7/idea",
            "stage-commit/cycle-7/plan",
        ],
    }
    request["frozen"]["question_literature_input"] = {"kind": "none"}
    request["frozen"]["accepted_target_commit_closures"] = []
    request["frozen"]["bundle_replan_candidates"] = []
    return request


def no_scientific_input_proposal(**overrides: Any) -> Dict[str, Any]:
    proposal = outcome_proposal(
        disposition="insufficient_evidence",
        claim=None,
        support_scope="no accepted Plan, literature revision or TargetCommit",
        limitations=["upstream research work exhausted before measurement"],
        missing_evidence=["an accepted scientific observation"],
        uncertainty_basis=[],
        causal_interpretation={
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "claim_scope": "no causal claim",
            "attribution_basis_refs": [],
            "statement": "No causal attribution is established.",
            "sufficiency_rationale": "No accepted measurement is available.",
            "confounders": [],
        },
        research_synthesis={
            "context_ref": "reasoning-research-context/7",
            "scope_refs": [
                "research-cycle/7",
                "question/42",
                "scientific-outcome/42/1",
                "question/7",
                "quest/3",
                "goal-revision/5",
            ],
            "narrative": (
                "This Cycle produced no accepted scientific observation. "
                "Across Cycles the required evidence gap leaves the current Question "
                "unanswered, its parent unresolved, and the Quest Goal unmet."
            ),
            "uncertainties": ["the required scientific observation is unavailable"],
        },
    )
    proposal.update(overrides)
    return proposal


def bundle_replan_candidate() -> Dict[str, Any]:
    return {
        "kind": "BundleReplanRequiredCandidate",
        "ref": "bundle-replan-candidate/7",
        "source_bundle_stage_commit_ref": "stage-commit/cycle-7/bundle",
        "experiment_key": "experiment-key/partial-replication",
        "experiment_brief_ref": "experiment-brief/partial-replication",
        "accepted_partial_target_commit_refs": [
            "target-commit/partial-replication"
        ],
        "unrealized_item_refs": ["experiment-item/independent-replication"],
        "semantic_change_basis": [
            {
                "frozen_slot": "SemanticDelta",
                "basis_refs": ["semantic-barrier/independent-replication"],
                "required_change": "change the frozen replication intervention",
            }
        ],
    }


class FakePorts:
    """Explicit fixture: its responses are never formal Owner receipts."""

    def __init__(self) -> None:
        self.answer_submissions: list[Mapping[str, Any]] = []
        self.completion_submissions: list[Mapping[str, Any]] = []
        self.question_creation_directions: list[Mapping[str, Any]] = []

    def submit_answer_candidate(self, candidate: Mapping[str, Any]) -> OwnerReply:
        self.answer_submissions.append(candidate)
        if len(self.answer_submissions) == 1:
            return OwnerReply(
                "rejected",
                "fixture-rejection/1",
                True,
                candidate["stage_run_request_ref"],
                candidate["question_ref"],
                candidate["root_session_ref"],
            )
        return OwnerReply(
            "accepted",
            "fixture-answer-acceptance/2",
            True,
            candidate["stage_run_request_ref"],
            candidate["question_ref"],
            candidate["root_session_ref"],
        )

    def submit_confirmed_completion_candidate(
        self, candidate: Mapping[str, Any]
    ) -> OwnerReply:
        self.completion_submissions.append(candidate)
        return OwnerReply(
            "accepted",
            "fixture-goal-acceptance/1",
            True,
            candidate["stage_run_request_ref"],
            candidate["question_ref"],
            candidate["root_session_ref"],
        )

    def create_question(self, source: Mapping[str, Any]) -> Mapping[str, Any]:
        self.question_creation_directions.append(source)
        return {
            "selectable_target": selectable_target(
                "43", graph_revision="graph-revision/after-create"
            )
        }


class ReasoningStageMvpTests(unittest.TestCase):
    def test_exhausted_upstream_without_plan_literature_or_targets_still_reasons(self) -> None:
        candidate = build_scientific_outcome(
            no_scientific_input_request(),
            [],
            no_scientific_input_proposal(),
        )
        self.assertEqual(candidate["disposition"], "insufficient_evidence")
        self.assertEqual(
            candidate["frozen_input_refs"]["plan_evidence_input"]["kind"],
            "none",
        )
        self.assertEqual(
            candidate["frozen_input_refs"]["question_literature_input"]["kind"],
            "none",
        )
        self.assertEqual(
            [
                item["outcome"]
                for item in candidate["frozen_input_refs"][
                    "upstream_stage_closure"
                ]
            ],
            ["Exhausted", "Skipped", "Skipped"],
        )
        self.assertNotIn("StageCommit", candidate)

    def test_invalid_skipped_exhausted_or_plan_union_paths_fail_closed(self) -> None:
        after_exhaustion_completed = no_scientific_input_request()
        after_exhaustion_completed["frozen"]["upstream_stage_closure"][1] = {
            "stage": "Plan",
            "stage_commit_ref": "stage-commit/cycle-7/plan",
            "outcome": "Completed",
        }

        skipped_without_basis = request_fixture()
        skipped_without_basis["frozen"]["upstream_stage_closure"][0][
            "outcome"
        ] = "Skipped"

        completed_plan_without_plan = request_fixture()
        completed_plan_without_plan["frozen"]["plan_evidence_input"] = {
            "kind": "none",
            "basis_stage_commit_refs": ["stage-commit/cycle-7/plan"],
        }

        bundle_exhausted_without_plan = no_scientific_input_request()
        bundle_exhausted_without_plan["frozen"]["upstream_stage_closure"] = [
            {
                "stage": "Idea",
                "stage_commit_ref": "stage-commit/cycle-7/idea",
                "outcome": "Skipped",
                "typed_basis_refs": ["typed-basis/direct-plan-skip"],
            },
            {
                "stage": "Plan",
                "stage_commit_ref": "stage-commit/cycle-7/plan",
                "outcome": "Skipped",
                "typed_basis_refs": ["typed-basis/direct-plan-skip"],
            },
            {
                "stage": "Bundle",
                "stage_commit_ref": "stage-commit/cycle-7/bundle",
                "outcome": "Exhausted",
                "exhaustion_proposal_ref": "exhaustion-proposal/bundle/7",
                "exhaustion_evidence_refs": ["exhaustion-evidence/bundle/7"],
            },
        ]

        accepted_plan_after_idea_exhaustion = no_scientific_input_request()
        accepted_plan_after_idea_exhaustion["frozen"]["plan_evidence_input"] = (
            request_fixture()["frozen"]["plan_evidence_input"]
        )

        accepted_plan_after_plan_exhaustion = no_scientific_input_request()
        accepted_plan_after_plan_exhaustion["frozen"]["upstream_stage_closure"] = [
            {
                "stage": "Idea",
                "stage_commit_ref": "stage-commit/cycle-7/idea",
                "outcome": "Completed",
            },
            {
                "stage": "Plan",
                "stage_commit_ref": "stage-commit/cycle-7/plan",
                "outcome": "Exhausted",
                "exhaustion_proposal_ref": "exhaustion-proposal/plan/7",
                "exhaustion_evidence_refs": ["exhaustion-evidence/plan/7"],
            },
            {
                "stage": "Bundle",
                "stage_commit_ref": "stage-commit/cycle-7/bundle",
                "outcome": "Skipped",
                "typed_basis_refs": ["stage-commit/cycle-7/plan"],
            },
        ]
        accepted_plan_after_plan_exhaustion["frozen"]["plan_evidence_input"] = (
            request_fixture()["frozen"]["plan_evidence_input"]
        )

        for request in (
            after_exhaustion_completed,
            skipped_without_basis,
            completed_plan_without_plan,
            bundle_exhausted_without_plan,
            accepted_plan_after_idea_exhaustion,
            accepted_plan_after_plan_exhaustion,
        ):
            with self.subTest(request=request):
                with self.assertRaises(FailClosed):
                    build_scientific_outcome(
                        request, [], no_scientific_input_proposal()
                    )

    def test_literature_revision_none_and_honest_empty_revision_are_distinct(self) -> None:
        none_request = request_fixture()
        none_request["frozen"]["question_literature_input"] = {"kind": "none"}
        candidate = build_scientific_outcome(
            none_request, evidence_fixture(), outcome_proposal()
        )
        self.assertEqual(
            candidate["frozen_input_refs"]["question_literature_input"],
            {"kind": "none"},
        )

        empty_revision = request_fixture()
        empty_revision["frozen"]["question_literature_input"] = {
            "kind": "revision",
            "revision_ref": "question-literature-revision/honest-empty",
            "records": [],
        }
        candidate = build_scientific_outcome(
            empty_revision, evidence_fixture(), outcome_proposal()
        )
        self.assertEqual(
            candidate["frozen_input_refs"]["question_literature_input"]["records"],
            [],
        )

        fake_none = request_fixture()
        fake_none["frozen"]["question_literature_input"] = {
            "kind": "none",
            "revision_ref": "question-literature-revision/fake",
            "records": [],
        }
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                fake_none, evidence_fixture(), outcome_proposal()
            )

    def test_literature_evidence_basis_cannot_be_silently_upgraded(self) -> None:
        evidence = [
            *evidence_fixture(),
            {
                "kind": "LiteratureRecord",
                "ref": "literature-record/2",
                "evidence_basis": "verified_fulltext",
                "finding": "supporting",
            },
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), evidence, outcome_proposal()
            )

    def test_caller_cannot_relabel_or_duplicate_frozen_evidence_roles(self) -> None:
        duplicated_reuse = [
            {
                "kind": "EvidenceReuseLeaf",
                "ref": "reuse-evidence/1",
                "role": "MetricResult",
                "finding": "supporting",
            },
            {
                "kind": "EvidenceReuseLeaf",
                "ref": "reuse-evidence/1",
                "role": "LogAsset",
                "finding": "negative",
            },
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), duplicated_reuse, outcome_proposal()
            )

        relabeled_literature = [
            {
                "kind": "LiteratureRecord",
                "ref": "literature-record/2",
                "role": "MetricResult",
                "evidence_basis": "citation_context",
                "finding": "supporting",
            }
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), relabeled_literature, outcome_proposal()
            )

    def test_target_roles_attempts_and_measurement_receipts_are_not_flattened(self) -> None:
        log_as_metric = [
            {
                "kind": "TargetClosureLeaf",
                "role": "MetricResult",
                "ref": "log-asset/negative-control",
                "source_target_commit_ref": "target-commit/negative-control",
                "source_evaluation_attempt_ref": "evaluation-attempt/negative-control",
                "finding": "negative",
            }
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), log_as_metric, outcome_proposal()
            )

        crossed_attempt = evidence_fixture()
        crossed_attempt[1] = {
            **crossed_attempt[1],
            "source_evaluation_attempt_ref": "evaluation-attempt/partial-replication",
        }
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), crossed_attempt, outcome_proposal()
            )

        misbound_acceptance = request_fixture()
        misbound_acceptance["frozen"]["accepted_target_commit_closures"][0][
            "formal_measurement_acceptance"
        ]["evaluation_attempt_ref"] = "evaluation-attempt/other"
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                misbound_acceptance, evidence_fixture(), outcome_proposal()
            )

        misbound_checkpoint = request_fixture()
        misbound_checkpoint["frozen"]["accepted_target_commit_closures"][0][
            "asset_roles"
        ]["checkpoint_artifacts"][0][
            "selected_by_target_commit_ref"
        ] = "target-commit/other"
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                misbound_checkpoint, evidence_fixture(), outcome_proposal()
            )

        one_binding = request_fixture()
        one_binding["frozen"]["accepted_target_commit_closures"][0][
            "execution_input_bindings"
        ].pop()
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                one_binding, evidence_fixture(), outcome_proposal()
            )

        missing_causal_asset_receipt = request_fixture()
        del missing_causal_asset_receipt["frozen"][
            "accepted_target_commit_closures"
        ][0]["execution_input_bindings"][0]["causal_inputs"][0][
            "rm_asset_receipt_ref"
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                missing_causal_asset_receipt,
                evidence_fixture(),
                outcome_proposal(),
            )

        semantic_role_collision = request_fixture()
        semantic_role_collision["frozen"]["accepted_target_commit_closures"][0][
            "semantic_chain"
        ]["baseline_ref"] = "metric-result/negative-control"
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                semantic_role_collision, evidence_fixture(), outcome_proposal()
            )

        diagnostic_from_another_run = request_fixture()
        diagnostic_from_another_run["frozen"][
            "accepted_target_commit_closures"
        ][0]["asset_roles"]["selected_logs"][0][
            "source_subject_ref"
        ] = "variant-run/other"
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                diagnostic_from_another_run,
                evidence_fixture(),
                outcome_proposal(),
            )

    def test_diagnostics_cannot_replace_substantive_scientific_evidence(self) -> None:
        diagnostic_request = request_fixture()
        diagnostic_request["frozen"]["accepted_target_commit_closures"] = []
        diagnostic_request["frozen"]["question_literature_input"] = {"kind": "none"}
        diagnostic_leaf = diagnostic_request["frozen"]["plan_evidence_input"][
            "evidence_reuse_leaves"
        ][0]
        diagnostic_leaf.update(
            {
                "role": "AnalysisAsset",
                "asset_version_ref": "asset-version/history-analysis-1",
                "provenance_closure_refs": [
                    "reuse-evidence/1",
                    "asset-version/history-analysis-1",
                    "target-commit/history-1",
                    "evaluation-attempt/history-1",
                    "variant-run/history-1",
                    "rg-receipt/target-commit/history-1",
                    "rg-receipt/formal-measurement/history-1",
                    "rg-receipt/role/history-analysis-1",
                ],
                "capabilities": ["supports bounded interpretation"],
                "source_role_acceptance_receipt_ref": (
                    "rg-receipt/role/history-analysis-1"
                ),
                "supported_claim": "diagnostic interpretation of the prior attempt",
            }
        )
        diagnostic_only = [
            {
                "kind": "EvidenceReuseLeaf",
                "ref": "reuse-evidence/1",
                "finding": "context",
            }
        ]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                diagnostic_request,
                diagnostic_only,
                outcome_proposal(
                    disposition="denied",
                    claim="No effect",
                    uncertainty_basis=[],
                ),
            )
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                diagnostic_request, diagnostic_only, outcome_proposal()
            )
        explanatory_candidate = build_scientific_outcome(
            diagnostic_request,
            diagnostic_only,
            no_scientific_input_proposal(
                support_scope="one accepted AnalysisAsset without a formal measurement",
                limitations=["the analysis remains useful only as context"],
                missing_evidence=["a role-valid scientific observation"],
            ),
        )
        self.assertEqual(
            explanatory_candidate["evidence"][0]["source_subject_kind"],
            "EvaluationAttempt",
        )
        self.assertEqual(
            explanatory_candidate["evidence"][0]["source_subject_ref"],
            "evaluation-attempt/history-1",
        )

        metric_reuse_request = request_fixture()
        metric_reuse_request["frozen"]["accepted_target_commit_closures"] = []
        reuse_only_evidence = [
            {
                "kind": "EvidenceReuseLeaf",
                "ref": "reuse-evidence/1",
                "finding": "negative",
            }
        ]
        reuse_only_proposal = no_scientific_input_proposal(
            disposition="denied",
            claim="The historical measurement does not support the effect.",
            support_scope="one accepted historical measurement",
            limitations=["only the frozen historical attempt is reusable"],
            missing_evidence=[],
            uncertainty_basis=[],
            causal_interpretation={
                "target_commit_refs": [],
                "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [],
                "provenance_refs": [],
                "claim_scope": "historical attempt only",
                "attribution_basis_refs": ["reuse-evidence/1"],
                "statement": "The accepted historical metric is negative.",
                "sufficiency_rationale": "The claim is limited to that attempt.",
                "confounders": [],
            },
        )
        candidate = build_scientific_outcome(
            metric_reuse_request, reuse_only_evidence, reuse_only_proposal
        )
        self.assertEqual(candidate["disposition"], "denied")

        for diagnostic_role in ("LogAsset", "AnalysisAsset", "CheckpointArtifact"):
            with self.subTest(diagnostic_role=diagnostic_role):
                diagnostic_reuse_request = request_fixture()
                diagnostic_reuse_request["frozen"][
                    "accepted_target_commit_closures"
                ] = []
                diagnostic_reuse_request["frozen"]["plan_evidence_input"][
                    "evidence_reuse_leaves"
                ][0]["role"] = diagnostic_role
                if diagnostic_role == "CheckpointArtifact":
                    diagnostic_reuse_request["frozen"]["plan_evidence_input"][
                        "evidence_reuse_leaves"
                    ][0].update(
                        {
                            "source_subject_kind": "VariantRun",
                            "source_subject_ref": "variant-run/history-1",
                        }
                    )
                with self.assertRaises(FailClosed):
                    build_scientific_outcome(
                        diagnostic_reuse_request,
                        reuse_only_evidence,
                        reuse_only_proposal,
                    )

    def test_multiple_changed_axes_do_not_mechanically_downgrade_disposition(self) -> None:
        candidate = build_scientific_outcome(
            request_fixture(),
            evidence_fixture(),
            outcome_proposal(
                disposition="affirmed",
                claim="A bounded joint effect is supported in the tested comparison.",
                missing_evidence=[],
                uncertainty_basis=[],
            ),
        )
        self.assertEqual(candidate["disposition"], "affirmed")
        self.assertGreater(
            len(candidate["causal_interpretation"]["changed_axis_fact_refs"]),
            1,
        )

        omitted_axis = outcome_proposal()
        omitted_axis["causal_interpretation"] = {
            **omitted_axis["causal_interpretation"],
            "changed_axis_fact_refs": ["causal-axis/model-change"],
        }
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), evidence_fixture(), omitted_axis
            )

    def test_reasoning_only_interprets_frozen_bundle_replan_candidates(self) -> None:
        request = request_fixture()
        request["frozen"]["bundle_replan_candidates"] = [
            bundle_replan_candidate()
        ]
        proposal = outcome_proposal(
            bundle_replan_interpretations=[
                {
                    "source_candidate_ref": "bundle-replan-candidate/7",
                    "source_basis_refs": [
                        "semantic-barrier/independent-replication"
                    ],
                    "interpretation": (
                        "The barrier limits this Cycle and supports returning to Plan."
                    ),
                }
            ]
        )
        candidate = build_scientific_outcome(
            request, evidence_fixture(), proposal
        )
        self.assertEqual(
            candidate["bundle_replan_interpretations"][0][
                "source_candidate_ref"
            ],
            "bundle-replan-candidate/7",
        )
        self.assertNotIn("replan_required", candidate)

        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(),
                evidence_fixture(),
                outcome_proposal(replan_required=True),
            )

        mismatched_experiment = request_fixture()
        candidate = bundle_replan_candidate()
        candidate["experiment_key"] = "experiment-key/another-experiment"
        mismatched_experiment["frozen"]["bundle_replan_candidates"] = [candidate]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                mismatched_experiment,
                evidence_fixture(),
                outcome_proposal(),
            )
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request, evidence_fixture(), outcome_proposal()
            )

    def test_negative_and_partial_evidence_can_be_uncertain_then_revised(self) -> None:
        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        ports = FakePorts()

        def revise(old: Mapping[str, Any], receipt: OwnerReply) -> Mapping[str, Any]:
            return {
                **old,
                "revision_of": receipt.receipt_ref,
                "limitations": [
                    *old["limitations"],
                    "Owner requested narrower wording",
                ],
            }

        reply = submit_answer_with_feedback(
            ports, candidate, revise, request=request_fixture()
        )
        self.assertEqual(reply.disposition, "accepted")
        self.assertEqual(len(ports.answer_submissions), 2)
        self.assertFalse(candidate["is_owner_accepted"])
        self.assertFalse(candidate["is_stage_advanced"])
        self.assertEqual(
            candidate["frozen_input_refs"]["accepted_target_commit_closures"][0][
                "asset_roles"
            ]["metric_result"]["role_ref"],
            "metric-result/negative-control",
        )
        self.assertEqual(candidate["evidence"][1]["finding"], "negative")
        self.assertEqual(candidate["evidence"][2]["finding"], "partial")

    def test_rejected_revision_revalidates_identity_evidence_and_disposition(self) -> None:
        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )

        def revision(
            old: Mapping[str, Any], receipt: OwnerReply, **changes: Any
        ) -> Mapping[str, Any]:
            return {**old, "revision_of": receipt.receipt_ref, **changes}

        malicious_revisions = {
            "unknown disposition": lambda old, receipt: revision(
                old, receipt, disposition="invented"
            ),
            "changed foreground epoch": lambda old, receipt: revision(
                old, receipt, foreground_epoch_ref="foreground-epoch/other"
            ),
            "external evidence": lambda old, receipt: revision(
                old,
                receipt,
                evidence=[
                    *old["evidence"],
                    {
                        "kind": "LiteratureRecord",
                        "ref": "literature-record/not-frozen",
                        "finding": "supporting",
                    },
                ],
            ),
            "wrong rejection receipt": lambda old, receipt: revision(
                old, receipt, revision_of="fixture-rejection/other"
            ),
            "missing rejection receipt": lambda old, receipt: dict(old),
            "local replan flag": lambda old, receipt: revision(
                old, receipt, replan_required=True
            ),
            "uncertain with required gap": lambda old, receipt: revision(
                old,
                receipt,
                missing_evidence=["a required independent measurement"],
            ),
        }
        for label, revise in malicious_revisions.items():
            with self.subTest(label=label):
                ports = FakePorts()
                with self.assertRaises(FailClosed):
                    submit_answer_with_feedback(
                        ports, candidate, revise, request=request_fixture()
                    )
                self.assertEqual(len(ports.answer_submissions), 1)

        in_place_ports = FakePorts()

        def mutate_in_place(
            old: Mapping[str, Any], receipt: OwnerReply
        ) -> Mapping[str, Any]:
            assert isinstance(old, dict)
            old["revision_of"] = receipt.receipt_ref
            old["foreground_epoch_ref"] = "foreground-epoch/forged"
            old["evidence"] = [
                *old["evidence"],
                {
                    "kind": "LiteratureRecord",
                    "ref": "literature-record/not-frozen",
                    "finding": "supporting",
                },
            ]
            return old

        with self.assertRaises(FailClosed):
            submit_answer_with_feedback(
                in_place_ports,
                candidate,
                mutate_in_place,
                request=request_fixture(),
            )
        self.assertEqual(len(in_place_ports.answer_submissions), 1)

    def test_initial_answer_submission_is_rebuilt_before_owner_side_effect(self) -> None:
        request = request_fixture()
        candidate = build_scientific_outcome(
            request, evidence_fixture(), outcome_proposal()
        )
        for field, forged in (
            ("kind", "ForgedOutcome"),
            ("disposition", "invented"),
            ("foreground_epoch_ref", "foreground-epoch/forged"),
        ):
            with self.subTest(field=field):
                ports = FakePorts()
                with self.assertRaises(FailClosed):
                    submit_answer_with_feedback(
                        ports,
                        {**candidate, field: forged},
                        lambda old, reply: old,
                        request=request,
                    )
                self.assertEqual(ports.answer_submissions, [])

    def test_open_form_synthesis_covers_cycle_question_parent_and_quest(self) -> None:
        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        scope = set(candidate["research_synthesis"]["scope_refs"])
        self.assertTrue(
            {
                "research-cycle/7",
                "question/42",
                "scientific-outcome/42/1",
                "question/7",
                "quest/3",
                "goal-revision/5",
            }.issubset(scope)
        )
        self.assertIn("Across Cycles", candidate["research_synthesis"]["narrative"])

    def test_synthesis_missing_scope_or_escaping_context_fails_closed(self) -> None:
        missing_parent = outcome_proposal()
        missing_parent["research_synthesis"] = {
            **missing_parent["research_synthesis"],
            "scope_refs": [
                ref
                for ref in missing_parent["research_synthesis"]["scope_refs"]
                if ref != "question/7"
            ],
        }
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(), evidence_fixture(), missing_parent
            )

        outside = outcome_proposal()
        outside["research_synthesis"] = {
            **outside["research_synthesis"],
            "scope_refs": [
                *outside["research_synthesis"]["scope_refs"],
                "question/not-frozen",
            ],
        }
        with self.assertRaises(FailClosed):
            build_scientific_outcome(request_fixture(), evidence_fixture(), outside)

    def test_new_question_stays_internal_until_it_is_a_selectable_target(self) -> None:
        ports = FakePorts()
        proposal = create_question_then_propose_next_cycle(
            request_fixture(),
            ports,
            {
                "question_text": "Does the effect reproduce independently?",
                "rationale": "partial evidence leaves the claim uncertain",
            },
            "Bundle",
            {
                "Idea": ["skip-basis/idea-satisfied"],
                "Plan": ["skip-basis/plan-satisfied"],
            },
        )
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal["kind"], "NextCycleProposal")
        self.assertEqual(proposal["question_ref"], "question/43")
        self.assertEqual(proposal["entry_stage"], "Bundle")
        self.assertEqual(
            set(proposal["typed_skip_basis_refs_by_stage"]), {"Idea", "Plan"}
        )
        self.assertFalse(proposal["is_authoritative"])
        self.assertEqual(
            ports.question_creation_directions[0]["kind"],
            "AutonomousQuestionDirection",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["creation_mode"],
            "AutonomousCreation",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["source_stage_run_request_ref"],
            "stage-run-request/reasoning-7",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["source_foreground_epoch_ref"],
            "foreground-epoch/7",
        )
        self.assertEqual(
            ports.question_creation_directions[0][
                "source_accepted_question_binding_ref"
            ],
            "accepted-question-binding/reasoning-7",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["source_cycle_ref"],
            "research-cycle/7",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["source_question_ref"],
            "question/42",
        )
        self.assertEqual(
            ports.question_creation_directions[0]["source_quest_ref"],
            "quest/3",
        )
        self.assertNotIn("QuestionProposal", str(proposal))

    def test_decomposition_context_reaches_creation_but_not_the_transition(self) -> None:
        ports = FakePorts()
        proposal = create_question_then_propose_next_cycle(
            request_fixture(),
            ports,
            {
                "mode": "decompose",
                "question_text": "Which mechanism explains the partial result?",
                "parent_question_ref": "question/42",
                "decomposition_basis_refs": ["scientific-outcome/42/2"],
                "blocked_by": ["question/should-not-pass"],
            },
        )
        self.assertIsNotNone(proposal)
        direction = ports.question_creation_directions[0]
        self.assertEqual(direction["mode"], "decompose")
        self.assertEqual(direction["parent_question_ref"], "question/42")
        self.assertEqual(
            direction["decomposition_basis_refs"], ["scientific-outcome/42/2"]
        )
        self.assertNotIn("blocked_by", direction)
        self.assertNotIn("decomposition_basis_refs", proposal)

    def test_question_draft_or_missing_port_cannot_escape_as_a_transition(self) -> None:
        class DraftOnlyPorts(FakePorts):
            def create_question(self, source: Mapping[str, Any]) -> Mapping[str, Any]:
                self.question_creation_directions.append(source)
                return {
                    "question_proposal_ref": "question-proposal/9",
                    "local_id": "local-9",
                }

        proposal = create_question_then_propose_next_cycle(
            request_fixture(), DraftOnlyPorts(), {"question_text": "A new direction"}
        )
        self.assertIsNone(proposal)
        self.assertIsNone(
            create_question_then_propose_next_cycle(
                request_fixture(), object(), {"question_text": "Another direction"}
            )
        )

        class LegacyProposeOnlyPorts:
            def propose_question(
                self, direction: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                raise AssertionError("Reasoning must use create_question")

        self.assertIsNone(
            create_question_then_propose_next_cycle(
                request_fixture(),
                LegacyProposeOnlyPorts(),
                {"question_text": "Do not use the legacy proposal seam"},
            )
        )

    def test_only_a_current_reasoning_source_can_enter_autonomous_creation(self) -> None:
        for field, value in (("stage", "Bundle"), ("is_current", False)):
            with self.subTest(field=field):
                request = request_fixture()
                request[field] = value
                ports = FakePorts()
                with self.assertRaises(FailClosed):
                    create_question_then_propose_next_cycle(
                        request,
                        ports,
                        {"question_text": "A child direction"},
                    )
                self.assertEqual(ports.question_creation_directions, [])

        ports = FakePorts()
        with self.assertRaises(FailClosed):
            create_question_then_propose_next_cycle(
                request_fixture(),
                ports,
                {
                    "creation_mode": "ManualCreation",
                    "question_text": "A human-only direction",
                },
            )
        self.assertEqual(ports.question_creation_directions, [])

        for entry_stage, skip_basis in (
            ("Unknown", None),
            ("Bundle", {"Idea": ["skip-basis/idea-only"]}),
        ):
            with self.subTest(entry_stage=entry_stage):
                ports = FakePorts()
                with self.assertRaises(FailClosed):
                    create_question_then_propose_next_cycle(
                        request_fixture(),
                        ports,
                        {"question_text": "Must not be created"},
                        entry_stage,
                        skip_basis,
                    )
                self.assertEqual(ports.question_creation_directions, [])

    def test_present_open_questions_are_free_choices_at_any_entry_stage(self) -> None:
        current = make_next_cycle_proposal(
            request_fixture(), selectable_target("42"), "Idea"
        )
        self.assertEqual(current["question_ref"], "question/42")
        bases = {
            "Idea": None,
            "Plan": {"Idea": ["skip-basis/idea-satisfied"]},
            "Bundle": {
                "Idea": ["skip-basis/idea-satisfied"],
                "Plan": ["skip-basis/plan-satisfied"],
            },
            "Reasoning": {
                "Idea": ["skip-basis/idea-satisfied"],
                "Plan": ["skip-basis/plan-satisfied"],
                "Bundle": ["skip-basis/evidence-ready"],
            },
        }
        for entry_stage, basis in bases.items():
            with self.subTest(entry_stage=entry_stage):
                other = make_next_cycle_proposal(
                    request_fixture(),
                    selectable_target("99", graph_revision="graph-revision/13"),
                    entry_stage,
                    basis,
                )
                self.assertEqual(other["question_ref"], "question/99")
                self.assertEqual(other["entry_stage"], entry_stage)

    def test_pruned_resolved_dead_end_or_noncurrent_questions_are_not_selectable(self) -> None:
        cases = (
            selectable_target(presence="pruned"),
            selectable_target(research_state="resolved"),
            selectable_target(research_state="dead_end"),
            selectable_target(research_state="paused"),
            selectable_target(quest_ref="quest/foreign"),
            selectable_target(is_current=None),
        )
        for target in cases:
            with self.subTest(target=target):
                with self.assertRaises(FailClosed):
                    make_next_cycle_proposal(request_fixture(), target, "Idea")

    def test_selection_facts_must_share_one_graph_revision(self) -> None:
        target = selectable_target()
        target["question_research_state_fact"][
            "graph_revision_ref"
        ] = "graph-revision/other"
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(request_fixture(), target, "Idea")

    def test_later_entry_requires_skip_basis_and_idea_rejects_one(self) -> None:
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(
                request_fixture(), selectable_target(), "Plan"
            )
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(
                request_fixture(),
                selectable_target(),
                "Idea",
                {"Idea": ["skip-basis/unused"]},
            )
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(
                request_fixture(),
                selectable_target(),
                "Bundle",
                {"Idea": ["skip-basis/only-one-stage"]},
            )
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(
                request_fixture(),
                selectable_target(),
                "Plan",
                ["skip-basis/untyped-shape"],
            )
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(
                request_fixture(), selectable_target(), "Unknown"
            )

    def test_transition_is_exactly_one_next_cycle_or_completion(self) -> None:
        outcome = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        next_cycle = make_next_cycle_proposal(
            request_fixture(), selectable_target(), "Idea"
        )
        completion = make_candidate_completion(
            request_fixture(),
            "The current Goal and completion milestone appear satisfied.",
            ["scientific-outcome/42/2", "completion-milestone-evidence/3"],
        )
        self.assertEqual(
            choose_reasoning_transition(outcome, next_cycle=next_cycle)["kind"],
            "NextCycleProposal",
        )
        self.assertEqual(
            choose_reasoning_transition(outcome, completion=completion)["kind"],
            "CandidateCompletion",
        )
        with self.assertRaises(FailClosed):
            choose_reasoning_transition(outcome)
        with self.assertRaises(FailClosed):
            choose_reasoning_transition(
                outcome, next_cycle=next_cycle, completion=completion
            )

    def test_completion_candidate_binds_current_goal_but_does_not_end_quest(self) -> None:
        candidate = make_candidate_completion(
            request_fixture(),
            "The current Goal and completion milestone appear satisfied.",
            ["scientific-outcome/42/2", "completion-milestone-evidence/3"],
        )
        self.assertEqual(candidate["quest_ref"], "quest/3")
        self.assertEqual(candidate["goal_revision_ref"], "goal-revision/5")
        self.assertFalse(candidate["is_authoritative"])
        self.assertNotIn("quest_completed", candidate)

    def test_completion_submission_requires_confirmation_ref_and_owner_acceptance_is_not_end(self) -> None:
        candidate = make_candidate_completion(
            request_fixture(),
            "The current Goal and completion milestone appear satisfied.",
            ["scientific-outcome/42/2", "completion-milestone-evidence/3"],
        )
        ports = FakePorts()
        with self.assertRaises(FailClosed):
            submit_confirmed_completion_candidate(ports, candidate, "")
        self.assertEqual(ports.completion_submissions, [])

        reply = submit_confirmed_completion_candidate(
            ports, candidate, "user-confirmation-receipt/completion-3"
        )
        self.assertEqual(reply.disposition, "accepted")
        self.assertEqual(
            ports.completion_submissions[0]["user_confirmation_receipt_ref"],
            "user-confirmation-receipt/completion-3",
        )
        self.assertNotIn("user_confirmation_receipt_ref", candidate)
        self.assertNotIn("quest_completed", ports.completion_submissions[0])

    def test_dependency_routes_are_not_exposed(self) -> None:
        outcome = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        proposal = make_next_cycle_proposal(
            request_fixture(), selectable_target("99"), "Idea"
        )
        injected = {
            **proposal,
            "blocked_by": ["question/injected"],
            "SelectProposal": {"local_id": "injected"},
        }
        injected["question_anchor"] = {
            **injected["question_anchor"],
            "dependency_route": "injected",
        }
        sanitized = choose_reasoning_transition(outcome, next_cycle=injected)
        serialized = str(sanitized)
        for forbidden in (
            "blocked_by",
            "dependency_route",
            "SelectProposal",
            "QuestionProposal",
            "CycleStartProposal",
            "QuestProposal",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("active", sanitized["question_research_state_fact"])

    def test_transition_must_bind_the_same_outcome(self) -> None:
        outcome = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        proposal = make_next_cycle_proposal(
            request_fixture(), selectable_target(), "Idea"
        )
        misbound = {**proposal, "stage_run_request_ref": "stage-run-request/other"}
        with self.assertRaises(FailClosed):
            choose_reasoning_transition(outcome, next_cycle=misbound)
        with self.assertRaises(FailClosed):
            choose_reasoning_transition(
                outcome,
                next_cycle={
                    "kind": "NextCycleProposal",
                    "is_authoritative": False,
                },
            )

    def test_insufficient_evidence_is_honest_candidate_not_stage_exhaustion(self) -> None:
        candidate = build_scientific_outcome(
            request_fixture(),
            evidence_fixture(),
            outcome_proposal(
                disposition="insufficient_evidence",
                claim=None,
                missing_evidence=["a completed independent measurement"],
                uncertainty_basis=[],
                limitations=["available result is partial"],
            ),
        )
        self.assertEqual(candidate["disposition"], "insufficient_evidence")
        self.assertNotIn("exhausted", str(candidate).lower())

    def test_uncertain_and_insufficient_evidence_have_disjoint_shapes(self) -> None:
        uncertain = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        self.assertEqual(uncertain["missing_evidence"], [])
        self.assertTrue(uncertain["uncertainty_basis"])

        invalid_uncertain = (
            outcome_proposal(missing_evidence=["a required replication"]),
            outcome_proposal(uncertainty_basis=[]),
        )
        for proposal in invalid_uncertain:
            with self.assertRaises(FailClosed):
                build_scientific_outcome(
                    request_fixture(), evidence_fixture(), proposal
                )

        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(),
                evidence_fixture(),
                outcome_proposal(
                    disposition="insufficient_evidence",
                    claim="The effect is absent.",
                    missing_evidence=["a required replication"],
                    uncertainty_basis=[],
                ),
            )

    def test_unknown_currentness_or_missing_owner_port_fails_closed(self) -> None:
        request = request_fixture()
        request["is_current"] = None
        with self.assertRaises(FailClosed):
            build_scientific_outcome(request, evidence_fixture(), outcome_proposal())

        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        with self.assertRaises(FailClosed):
            submit_answer_with_feedback(
                object(),
                candidate,
                lambda old, reply: old,
                request=request_fixture(),
            )
        completion = make_candidate_completion(
            request_fixture(), "Goal appears met.", ["completion-basis/1"]
        )
        with self.assertRaises(FailClosed):
            submit_confirmed_completion_candidate(
                object(), completion, "user-confirmation-receipt/completion-1"
            )

    def test_missing_or_misbound_accepted_question_binding_fails_closed(self) -> None:
        no_epoch = request_fixture()
        del no_epoch["foreground_epoch_ref"]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                no_epoch, evidence_fixture(), outcome_proposal()
            )

        missing = request_fixture()
        del missing["accepted_question_binding"]
        with self.assertRaises(FailClosed):
            build_scientific_outcome(missing, evidence_fixture(), outcome_proposal())

        misbound = request_fixture()
        misbound["accepted_question_binding"]["question_anchor"][
            "question_ref"
        ] = "question/other"
        with self.assertRaises(FailClosed):
            build_scientific_outcome(misbound, evidence_fixture(), outcome_proposal())

    def test_incomplete_selectable_target_fails_closed(self) -> None:
        target = selectable_target()
        del target["question_anchor"]["question_accepted_receipt_ref"]
        with self.assertRaises(FailClosed):
            make_next_cycle_proposal(request_fixture(), target, "Idea")

    def test_unknown_owner_feedback_currentness_fails_closed(self) -> None:
        class UnknownFeedbackPorts(FakePorts):
            def submit_answer_candidate(self, candidate: Mapping[str, Any]) -> OwnerReply:
                return OwnerReply(
                    "accepted",
                    "fixture-answer/unknown",
                    None,
                    candidate["stage_run_request_ref"],
                    candidate["question_ref"],
                    candidate["root_session_ref"],
                )

        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        with self.assertRaises(FailClosed):
            submit_answer_with_feedback(
                UnknownFeedbackPorts(),
                candidate,
                lambda old, reply: old,
                request=request_fixture(),
            )

    def test_misbound_owner_feedback_fails_closed(self) -> None:
        class MisboundFeedbackPorts(FakePorts):
            def submit_answer_candidate(self, candidate: Mapping[str, Any]) -> OwnerReply:
                return OwnerReply(
                    "accepted",
                    "fixture-answer/misbound",
                    True,
                    candidate["stage_run_request_ref"],
                    "question/other",
                    candidate["root_session_ref"],
                )

        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        with self.assertRaises(FailClosed):
            submit_answer_with_feedback(
                MisboundFeedbackPorts(),
                candidate,
                lambda old, reply: old,
                request=request_fixture(),
            )

    def test_misbound_owner_feedback_session_fails_closed(self) -> None:
        class MisboundSessionPorts(FakePorts):
            def submit_answer_candidate(self, candidate: Mapping[str, Any]) -> OwnerReply:
                return OwnerReply(
                    "accepted",
                    "fixture-answer/misbound-session",
                    True,
                    candidate["stage_run_request_ref"],
                    candidate["question_ref"],
                    "agent-session/other",
                )

        candidate = build_scientific_outcome(
            request_fixture(), evidence_fixture(), outcome_proposal()
        )
        with self.assertRaises(FailClosed):
            submit_answer_with_feedback(
                MisboundSessionPorts(),
                candidate,
                lambda old, reply: old,
                request=request_fixture(),
            )

    def test_evidence_outside_closure_or_evidence_free_denial_fails_closed(self) -> None:
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(),
                [
                    {
                        "ref": "metric-result/not-frozen",
                        "kind": "TargetClosureLeaf",
                        "role": "MetricResult",
                        "source_target_commit_ref": "target-commit/not-frozen",
                        "source_evaluation_attempt_ref": "evaluation-attempt/not-frozen",
                        "finding": "negative",
                    }
                ],
                outcome_proposal(),
            )
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(),
                [],
                outcome_proposal(
                    disposition="denied",
                    claim="No effect",
                    uncertainty_basis=[],
                ),
            )

    def test_technical_blocker_cannot_become_scientific_closure(self) -> None:
        with self.assertRaises(FailClosed):
            build_scientific_outcome(
                request_fixture(),
                evidence_fixture(),
                outcome_proposal(
                    disposition="denied",
                    claim="No effect",
                    uncertainty_basis=[],
                ),
                technical_blockers=["external outcome unknown"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
