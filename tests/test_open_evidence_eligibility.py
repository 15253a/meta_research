"""Accepted work is adoptable evidence regardless of material type (ADR 0005).

Unmeasured TargetCommits (observation, analysis, negative results) enter the
Plan evidence catalog, resolve to WorkProduct leaves, and can carry an
affirmed/denied/uncertain disposition in Reasoning without a manufactured
measurement.
"""
from __future__ import annotations

import json

import pytest

from meta_research.owners.common import canonical_json
from meta_research.reasoning_contract import (
    ReasoningContractError,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    validate_scientific_outcome,
)
from meta_research.target_commit_evidence import (
    TargetCommitEvidenceCatalog,
    target_commit_evidence_provenance,
)
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _accept_unmeasured(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    path = workspace / "outputs/metrics.json"
    document = json.loads(path.read_text())
    document["formal_runs"] = [{"run_key": "analysis-work", "evaluations": []}]
    document["metrics"] = {}
    document["result_disposition"] = "uncertain"
    path.write_text(canonical_json(document))
    accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
    graph = runtime.owners.research_graph
    commit = next(item for item in graph.query_target_commits(authority.graph_ref)
                  if item.commit_ref == accepted.target_commit_ref)
    runtime.bundle_stage._publish_target_commit_evidence(
        quest_ref=graph.query_target_graph(authority.stage_request_ref).quest_ref, commit=commit)
    return runtime, graph, commit


def test_unmeasured_commit_enters_plan_evidence_catalog(tmp_path):
    runtime, graph, commit = _accept_unmeasured(tmp_path)
    try:
        quest_ref = _quest_ref(runtime)
        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        _, entries = catalog.query_plan_evidence_catalog(
            quest_ref=quest_ref, target_commit_refs=(commit.commit_ref,))
        assert len(entries) == 1
        entry = entries[0]
        provenance = target_commit_evidence_provenance(commit)
        assert "metric_result" not in provenance["capabilities"]
        assert list(provenance["capabilities"]) == ["experiment_result", "query_support"]
        assert entry["target_commit_root_ref"] == commit.commit_ref
    finally:
        runtime.close()


def test_unmeasured_commit_resolves_work_product_leaf(tmp_path):
    runtime, graph, commit = _accept_unmeasured(tmp_path)
    try:
        quest_ref = _quest_ref(runtime)
        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        leaves = catalog.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref, target_commit_refs=(commit.commit_ref,))
        assert len(leaves) == 1
        leaf = leaves[0]
        assert leaf.role == "WorkProduct"
        assert leaf.evidence_item_ref == commit.commit_ref
        assert leaf.source_subject_kind == "VariantRun"
        assert leaf.formal_measurement_acceptance_receipt is None
        # The Owner wrapper accepts the same closure including unmeasured work.
        assert graph.resolve_reasoning_target_evidence_leaves(
            quest_ref=quest_ref, target_commit_refs=(commit.commit_ref,)) == leaves
    finally:
        runtime.close()


def test_commit_ref_resolves_as_work_product_historical_evidence(tmp_path):
    runtime, graph, commit = _accept_unmeasured(tmp_path)
    try:
        quest_ref = _quest_ref(runtime)
        resolved = graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=quest_ref, ref=commit.commit_ref)
        facts = graph.query_target_formal_results(commit.target_ref)
        run_ref = next(fact["variant_run"]["variant_run_ref"] for fact in facts
                       if fact.get("variant_run"))
        assert resolved == {
            "kind": "WorkProduct",
            "ref": commit.commit_ref,
            "source_subject_ref": run_ref,
            "owner_acceptance_receipt_ref": commit.receipt.receipt_ref,
        }
        assert graph.resolve_reasoning_historical_evidence_leaf(
            quest_ref=quest_ref, ref="target_commit_does_not_exist") is None
    finally:
        runtime.close()


def test_work_product_citation_satisfies_substantive_gate():
    outcome = _outcome_with_work_product()
    context = _research_context()
    def resolver(ref):
        if ref == "target-commit:work:1":
            return {
                "kind": "WorkProduct",
                "ref": "target-commit:work:1",
                "source_subject_ref": "variant-run:1",
                "owner_acceptance_receipt_ref": "receipt:commit:1",
            }
        return None
    assert validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=[],
        frozen_research_context=context,
        historical_resolver=resolver,
    )
    unresolved = dict(outcome)
    unresolved["evidence"] = [
        {"kind": "WorkProduct", "ref": "target-commit:missing:1", "finding": "supporting"}
    ]
    with pytest.raises(ReasoningContractError, match="scientific_outcome_evidence_invalid"):
        validate_scientific_outcome(
            unresolved,
            frozen_evidence_closure=[],
            frozen_research_context=context,
            historical_resolver=resolver,
        )


def _quest_ref(runtime):
    from sqlalchemy import text
    with runtime._database.read() as connection:
        return connection.execute(text("SELECT quest_ref FROM rg_quests LIMIT 1")).scalar_one()


def _outcome_with_work_product() -> dict[str, object]:
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
        "claim": "The distribution audit found device/time confounding.",
        "evidence": [
            {"kind": "WorkProduct", "ref": "target-commit:work:1", "finding": "supporting"}
        ],
        "missing_evidence": [],
        "uncertainty_basis": [],
        "support_scope": ["The audit covers the frozen sample metadata."],
        "limitations": ["Descriptive only; no causal attribution."],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": ["target-commit:work:1"],
            "claim_scope": "A bounded observational statement.",
            "statement": "The audit observes imbalance without intervention.",
            "sufficiency_rationale": "The work product is the direct audit record.",
            "confounders": ["Device and season remain confounded."],
        },
        "research_synthesis": {
            "cycle": {"cycle_ref": "cycle:1",
                      "impact": "One audit finding enters the Question history."},
            "current_question": {"question_ref": "question:1",
                                 "prior_accepted_outcome_refs": ["scientific-outcome:prior"],
                                 "progress": "Confounding is now documented."},
            "parent_questions": [{"question_ref": "question:parent", "impact": "material",
                                  "statement": "Supports one parent branch."}],
            "quest": {"quest_ref": "quest:1", "goal_revision_ref": "goal-revision:1",
                      "graph_revision_ref": "graph-revision:1",
                      "impact": "One milestone gains bounded observational support."},
        },
        "is_authoritative": False,
    }


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
                {"question_ref": "question:parent", "parent_question_ref": None,
                 "question_receipt_ref": "rg-question-receipt:parent"}
            ],
            "prior_current_question_outcomes": [
                {"cycle_ref": "cycle:prior", "request_ref": "stage-request:prior",
                 "outcome_ref": "scientific-outcome:prior", "disposition": "uncertain",
                 "outcome_receipt_ref": "rg-reasoning-receipt:prior"}
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
            "stage-commit:idea", "stage-commit:plan", "stage-commit:bundle",
        ],
    }
