"""Research scope and missing knowledge must not be distorted by bookkeeping."""
from copy import deepcopy

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.plan_contract import PlanContractError, selected_plan_evidence_catalog, validate_plan_document
from meta_research.reasoning_contract import validate_scientific_outcome, ReasoningContractError
from test_plan_skill_contract import _plan_document, _EVIDENCE_REF, _IDEA_SET
from test_reasoning_contract import _scientific_outcome, _research_context, _literature_closure


def _validate_plan(document, catalog, revision):
    return validate_plan_document(document, question_ref="question_1", idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1", context_pack_hash="6" * 64,
        accepted_idea_set=_IDEA_SET, evidence_by_ref={row["evidence_ref"]: row for row in catalog},
        evidence_reference_revision=revision)


def test_later_page_selected_binding_does_not_expand_frozen_input():
    document = _plan_document()
    document["source_bindings"]["evidence_reference_revision"] = 0
    document["source_bindings"]["selected_evidence_catalog"] = [deepcopy(_EVIDENCE_REF)]
    frozen = []
    assert _validate_plan(document, frozen, 0) == canonical_hash(document)
    assert frozen == []
    assert selected_plan_evidence_catalog(document, frozen) == [_EVIDENCE_REF]


@pytest.mark.parametrize("mutation", ["unused", "duplicate", "conflicting", "receipt", "oversize"])
def test_additional_bindings_reject_unusable_or_ambiguous_identity(mutation):
    document = _plan_document()
    selected = deepcopy(_EVIDENCE_REF)
    document["source_bindings"]["selected_evidence_catalog"] = [selected]
    if mutation == "unused":
        document["evidence_reuse_set"] = []
    elif mutation == "duplicate":
        document["source_bindings"]["selected_evidence_catalog"].append(deepcopy(selected))
    elif mutation == "conflicting":
        selected["content_hash"] = "9" * 64
    elif mutation == "receipt":
        selected["integrity_receipt_ref"] = "unverified_receipt"
    else:
        document["source_bindings"]["selected_evidence_catalog"] *= 33
    with pytest.raises(PlanContractError):
        selected_plan_evidence_catalog(document, [_EVIDENCE_REF])


def test_plan_only_records_relevant_idea_obligation_relationships():
    document = _plan_document()
    contract = document["answer_contract"]
    contract["obligations"][0]["question_trace"] = ["unknown_statement"]
    contract["obligations"][0]["idea_relevance"].pop()
    for trace in document["idea_trace"]:
        if trace["idea_ref"] == "frequency":
            trace["obligation_roles"] = [row for row in trace["obligation_roles"] if row["obligation_key"] != "effect"]
    contract.pop("answer_contract_hash")
    contract["answer_contract_hash"] = canonical_hash(contract)
    assert _validate_plan(document, [_EVIDENCE_REF], 1) == canonical_hash(document)


@pytest.mark.parametrize("disposition", ["affirmed", "denied", "uncertain", "insufficient_evidence"])
def test_outcome_can_honestly_retain_missing_evidence_and_uncertainty(disposition):
    outcome = _scientific_outcome(disposition)
    outcome["missing_evidence"] = ["External replication remains unavailable."]
    outcome["uncertainty_basis"] = ["The available cohort does not settle external validity."]
    assert validate_scientific_outcome(outcome, frozen_evidence_closure=_literature_closure(),
        frozen_research_context=_research_context()) == canonical_hash(outcome)


def test_uncertain_claim_can_state_missing_evidence_without_external_material():
    outcome = _scientific_outcome("uncertain")
    outcome["evidence"] = []
    outcome["missing_evidence"] = ["No experiment has run yet."]
    outcome["causal_interpretation"]["attribution_basis_refs"] = []
    assert validate_scientific_outcome(outcome, frozen_evidence_closure=[], frozen_research_context=_research_context()) == canonical_hash(outcome)


def test_history_preview_is_not_a_false_exhaustive_active_question_set():
    context = _research_context()
    graph = context["graph_binding"]
    graph["schema_ref"] = "meta-research/reasoning-graph-context/v2"
    graph["active_question_refs"] = ["question:1"]
    graph["history_page"] = {
        "schema_ref": "meta-research/research-history-page/v1", "limit": 12, "summary_only": True,
        "active_total": 1000, "active_shown": 1, "active_next_offset": 1,
        "prior_total": 1000, "prior_shown": 1, "prior_next_offset": 1,
        "parent_shown": 1, "parent_has_more": False, "parent_next_question_ref": None,
    }
    outcome = _scientific_outcome()
    assert validate_scientific_outcome(outcome, frozen_evidence_closure=_literature_closure(),
        frozen_research_context=context) == canonical_hash(outcome)
    graph["history_page"]["prior_shown"] = 1000
    with pytest.raises(ReasoningContractError):
        validate_scientific_outcome(outcome, frozen_evidence_closure=_literature_closure(), frozen_research_context=context)
