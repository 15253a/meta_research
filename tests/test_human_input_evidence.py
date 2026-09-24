"""Answered human professional opinion is adoptable formal evidence (ADR 0005).

A verified HumanRequest response of THIS Quest resolves to a HumanInput
leaf that binds the exact response identity and content hash, satisfies
the substantive evidence gate alone, and fails closed when the ref is
unknown, the stored content is tampered, the request is withdrawn, or the
ref belongs to a foreign Quest.  HumanInput never disturbs the per-commit
MetricResult/WorkProduct counting invariants.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    IntentTurnResult,
    ProposalDraftResult,
)
from meta_research.reasoning_contract import (
    ReasoningContractError,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    validate_scientific_outcome,
)
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture

QUEST_REF = "quest:human-input:1"


class _DeterministicDraftingProvider:
    def draft(self, _request) -> ProposalDraftResult:
        return ProposalDraftResult(
            content={
                "title": "A bounded question",
                "unknown_statement": "What remains unknown?",
                "answer_shape": "A falsifiable answer",
                "applicability_scope": "This Quest",
                "background_context": "",
                "requirements_constraints": "",
            },
            adapter_kind="deterministic_test_adapter",
        )

    def reply(self, request) -> IntentTurnResult:
        return IntentTurnResult(
            reply=f"Acknowledged: {request.message}",
            native_session_ref=request.native_session_ref
            or "native_test_session",
            adapter_kind="deterministic_test_adapter",
        )


def _answered_request(tmp_path, *, quest_ref=QUEST_REF):
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "human-input-evidence"),
        proposal_drafter=_DeterministicDraftingProvider(),
        intent_drafting_provider=_DeterministicDraftingProvider(),
    )
    graph = runtime.owners.research_graph
    request = graph.open_human_request(
        request_kind="offline_action",
        obligation="Provide the domain expert's judgement on the audit.",
        business_purpose="Adopt the professional opinion as a research basis.",
        target_assertion={"topic": "device-season confounding"},
        acceptance_conditions=("The response states the expert's judgement.",),
        direct_waiter={
            "waiter_ref": "expert_opinion_1",
            "generation": 1,
            "target_assertion": {"topic": "device-season confounding"},
            "wait_scope": "local",
            "other_blockers": [],
        },
        idempotency_key="open-human-input-evidence",
        quest_ref=quest_ref,
    )
    response = runtime.owners.human_collaboration.respond_to_human_request(
        request["request_ref"],
        decision="provided",
        facts={
            "judgement": (
                "Winter collection may shift the device mix; check intake logs."
            )
        },
        note="Professional opinion for the audit, provided by the domain expert.",
        idempotency_key="open-human-input-evidence-response",
    )
    return runtime, graph, request, response


def test_answered_response_resolves_human_input_leaf(tmp_path):
    runtime, graph, request, response = _answered_request(tmp_path)
    try:
        resolved = graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=QUEST_REF, ref=response["response_ref"]
        )
        assert resolved == {
            "kind": "HumanInput",
            "ref": response["response_ref"],
            "response_ref": response["response_ref"],
            "request_ref": request["request_ref"],
            "content_hash": canonical_hash(response),
            "owner_acceptance_receipt_ref": response["receipt_ref"],
        }
        assert graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=QUEST_REF, ref="human_response_does_not_exist"
        ) is None
    finally:
        runtime.close()


def test_foreign_quest_response_fails_closed(tmp_path):
    runtime, graph, request, response = _answered_request(tmp_path)
    try:
        assert graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref="quest:human-input:other", ref=response["response_ref"]
        ) is None
    finally:
        runtime.close()


def test_tampered_response_content_fails_closed(tmp_path):
    runtime, graph, request, response = _answered_request(tmp_path)
    try:
        with runtime._database.write() as connection:
            changed = connection.execute(
                text(
                    "UPDATE hc_human_request_responses SET facts_json = :facts "
                    "WHERE response_ref = :ref"
                ),
                {
                    "facts": canonical_json({"judgement": "tampered"}),
                    "ref": response["response_ref"],
                },
            )
            assert changed.rowcount == 1
        assert graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=QUEST_REF, ref=response["response_ref"]
        ) is None
    finally:
        runtime.close()


def test_withdrawn_request_fails_closed(tmp_path):
    runtime, graph, request, response = _answered_request(tmp_path)
    try:
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "INSERT INTO owner_human_request_dispositions "
                    "(disposition_ref, request_ref, decision, evaluation_ref, "
                    "receipt_ref, receipt_hash, created_at) VALUES "
                    "(:disposition_ref, :request_ref, 'withdrawn', NULL, "
                    ":receipt_ref, :receipt_hash, :now)"
                ),
                {
                    "disposition_ref": "human_request_disposition_withdrawn_1",
                    "request_ref": request["request_ref"],
                    "receipt_ref": "hc_receipt_withdrawn_test_1",
                    "receipt_hash": "a" * 64,
                    "now": 1.0,
                },
            )
        assert graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=QUEST_REF, ref=response["response_ref"]
        ) is None
    finally:
        runtime.close()


def _human_input_leaf(ref="human_response_1"):
    return {
        "kind": "HumanInput",
        "ref": ref,
        "response_ref": ref,
        "request_ref": "human_request_1",
        "content_hash": "c" * 64,
        "owner_acceptance_receipt_ref": "hc_receipt_1",
    }


def test_human_input_citation_satisfies_substantive_gate():
    outcome = _outcome_with_human_input()
    context = _research_context()

    def resolver(ref):
        return _human_input_leaf(ref) if ref == "human_response_1" else None

    assert validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=[],
        frozen_research_context=context,
        historical_resolver=resolver,
    )
    # A frozen closure leaf with the same exact shape carries the gate too.
    assert validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=[_human_input_leaf()],
        frozen_research_context=context,
    )
    unresolved = dict(outcome)
    unresolved["evidence"] = [
        {
            "kind": "HumanInput",
            "ref": "human_response_missing",
            "finding": "supporting",
        }
    ]
    with pytest.raises(
        ReasoningContractError, match="scientific_outcome_evidence_invalid"
    ):
        validate_scientific_outcome(
            unresolved,
            frozen_evidence_closure=[],
            frozen_research_context=context,
            historical_resolver=resolver,
        )


def test_human_input_leaf_shape_is_exact():
    outcome = _outcome_with_human_input()
    context = _research_context()
    leaf = _human_input_leaf()
    for mutated in (
        dict(leaf, ref="human_response_2"),
        dict(leaf, response_ref="human_response_2"),
        dict(leaf, content_hash=""),
        dict(leaf, request_ref=""),
        dict(leaf, owner_acceptance_receipt_ref=""),
        {k: v for k, v in leaf.items() if k != "content_hash"},
        dict(leaf, source_evaluation_attempt_ref="evaluation-attempt:1"),
        dict(
            leaf,
            formal_measurement_acceptance_receipt_ref="receipt:metric:1",
        ),
    ):
        with pytest.raises(
            ReasoningContractError, match="reasoning_evidence_closure_invalid"
        ):
            validate_scientific_outcome(
                outcome,
                frozen_evidence_closure=[mutated],
                frozen_research_context=context,
            )


def _quest_ref(runtime):
    with runtime._database.read() as connection:
        return connection.execute(
            text("SELECT quest_ref FROM rg_quests LIMIT 1")
        ).scalar_one()


def _accept_root_commit(tmp_path, *, unmeasured):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    if unmeasured:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        document["formal_runs"] = [
            {"run_key": "analysis-work", "evaluations": []}
        ]
        document["metrics"] = {}
        document["result_disposition"] = "uncertain"
        path.write_text(canonical_json(document))
    accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
    graph = runtime.owners.research_graph
    commit = next(
        item
        for item in graph.query_target_commits(authority.graph_ref)
        if item.commit_ref == accepted.target_commit_ref
    )
    runtime.bundle_stage._publish_target_commit_evidence(
        quest_ref=graph.query_target_graph(
            authority.stage_request_ref
        ).quest_ref,
        commit=commit,
    )
    return runtime, graph, commit


@pytest.mark.parametrize(
    ("unmeasured", "metric_count", "work_count"),
    [(False, 1, 0), (True, 0, 1)],
)
def test_human_input_does_not_disturb_commit_leaf_counts(
    tmp_path, unmeasured, metric_count, work_count
):
    runtime, graph, commit = _accept_root_commit(
        tmp_path, unmeasured=unmeasured
    )
    try:
        quest_ref = _quest_ref(runtime)
        request = graph.open_human_request(
            request_kind="offline_action",
            obligation=(
                "Provide the domain expert's judgement on the audit."
            ),
            business_purpose=(
                "Adopt the professional opinion as a research basis."
            ),
            target_assertion={"topic": "device-season confounding"},
            acceptance_conditions=(
                "The response states the expert's judgement.",
            ),
            direct_waiter={
                "waiter_ref": "expert_opinion_commit_counts",
                "generation": 1,
                "target_assertion": {
                    "topic": "device-season confounding"
                },
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key=f"open-human-input-{metric_count}{work_count}",
            quest_ref=quest_ref,
        )
        response = (
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={
                    "judgement": (
                        "Winter collection may shift the device mix."
                    )
                },
                note="Professional opinion alongside the committed work.",
                idempotency_key=(
                    f"respond-human-input-{metric_count}{work_count}"
                ),
            )
        )
        leaves = graph.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref, target_commit_refs=(commit.commit_ref,)
        )
        roles = [leaf.role for leaf in leaves]
        assert roles.count("MetricResult") == metric_count
        assert roles.count("WorkProduct") == work_count
        assert "HumanInput" not in roles
        resolved = graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=quest_ref, ref=response["response_ref"]
        )
        assert resolved is not None and resolved["kind"] == "HumanInput"
    finally:
        runtime.close()


def _outcome_with_human_input():
    return {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": "scientific-outcome:1",
        "stage_run_request_ref": "stage-run-request:reasoning:1",
        "cycle_ref": "cycle:1",
        "question_ref": "question:1",
        "quest_ref": "quest:1",
        "goal_revision_ref": "goal-revision:1",
        "foreground_epoch": 7,
        "disposition": "affirmed",
        "claim": "The expert audit flags device and season confounding risk.",
        "evidence": [
            {
                "kind": "HumanInput",
                "ref": "human_response_1",
                "finding": "supporting",
            }
        ],
        "missing_evidence": [],
        "uncertainty_basis": [],
        "support_scope": [
            "The expert opinion covers the frozen intake period."
        ],
        "limitations": [
            "Professional judgement; no causal attribution by itself."
        ],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": ["human_response_1"],
            "claim_scope": "A bounded expert-informed statement.",
            "statement": "The expert observes a seasonal intake risk.",
            "sufficiency_rationale": (
                "The verified response is the direct expert record."
            ),
            "confounders": ["Device and season remain confounded."],
        },
        "research_synthesis": {
            "cycle": {
                "cycle_ref": "cycle:1",
                "impact": (
                    "One expert finding enters the Question history."
                ),
            },
            "current_question": {
                "question_ref": "question:1",
                "prior_accepted_outcome_refs": [
                    "scientific-outcome:prior"
                ],
                "progress": "Seasonal intake risk is documented.",
            },
            "parent_questions": [
                {
                    "question_ref": "question:parent",
                    "impact": "material",
                    "statement": "Supports one parent branch.",
                }
            ],
            "quest": {
                "quest_ref": "quest:1",
                "goal_revision_ref": "goal-revision:1",
                "graph_revision_ref": "graph-revision:1",
                "impact": (
                    "One milestone gains expert-informed support."
                ),
            },
        },
        "is_authoritative": False,
    }


def _research_context():
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
