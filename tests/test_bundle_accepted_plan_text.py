"""Accepted Plan prose must survive every Bundle contract consumer unchanged."""

from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import (
    BundleProtocolError,
    CodeReviewScope,
    ContentBindingProof,
    ExperimentBrief,
    ReceiptProof,
    validate_closed_bundle_projection,
)
from meta_research.bundle_target_contract import (
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    BundleTargetContractError,
    apply_strategy_update,
    build_normalized_completion_contract,
    formal_target_candidate_from_dict,
    formal_target_candidate_to_dict,
    measurement_contract_from_dict,
    normalized_completion_contract_from_dict,
    normalized_completion_contract_to_dict,
    rolling_strategy_state_from_dict,
    rolling_strategy_state_to_dict,
    start_rolling_strategy,
    strategy_update_from_dict,
    strategy_update_to_dict,
)
from meta_research.owners.common import canonical_hash
from meta_research.plan_contract import validate_plan_document
from meta_research.target_run_contract import (
    TargetRunContractError,
    validate_protected_execution_admission,
)
import test_bundle_target_contract as bundle_fixtures
import test_plan_skill_adapter as plan_fixtures
import test_target_run_contract as target_fixtures


PLAN_PROSE = "研" * 8192
LONG_NOTES = "研" * 1862  # 5586 UTF-8 bytes, like the accepted production notes.
PROSE_FIELDS = ("goal", "characteristics", "boundary_constraints", "semantic_delta")


def _plan():
    value = deepcopy(plan_fixtures._plan())
    value["experiment_briefs"][0]["experiment_key"] = "experiment-a"
    return value


def _validate_accepted_plan(plan):
    request = plan_fixtures._request()
    return validate_plan_document(
        plan,
        question_ref=request.question_ref,
        idea_set_ref=request.idea_set_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        accepted_idea_set=request.accepted_idea_set,
        evidence_by_ref={},
        evidence_reference_revision=0,
    )


def _candidate(contract):
    value = bundle_fixtures._candidate(contract, "target-a", "cell-a")
    # This shared historical fixture also serves retired v2 reuse-trace tests.
    value["candidate"].pop("reuse_trace", None)
    return value


def _round_trip(plan):
    original = deepcopy(plan)
    accepted_hash = _validate_accepted_plan(plan)
    contract = build_normalized_completion_contract(plan, bundle_fixtures._normalizations())
    assert contract.plan_document_hash == accepted_hash == canonical_hash(plan)
    document = normalized_completion_contract_to_dict(contract)
    restored = normalized_completion_contract_from_dict(document, plan_document=plan)
    assert restored == contract

    candidate_value = _candidate(restored)
    candidate = formal_target_candidate_from_dict(candidate_value, completion_contract=restored)
    assert formal_target_candidate_to_dict(candidate, completion_contract=restored) == candidate_value
    update_value = bundle_fixtures._update(1, [candidate_value], complete=False)
    update = strategy_update_from_dict(update_value, completion_contract=restored)
    assert strategy_update_to_dict(update, completion_contract=restored) == update_value
    state = apply_strategy_update(start_rolling_strategy(restored), update, completion_contract=restored)
    state_value = rolling_strategy_state_to_dict(state, completion_contract=restored)
    assert rolling_strategy_state_from_dict(state_value, completion_contract=restored) == state
    assert plan == original
    assert canonical_hash(plan) == accepted_hash
    return contract, candidate_value


def test_accepted_plan_long_notes_survive_completion_candidate_and_strategy():
    plan = _plan()
    plan["notes"] = LONG_NOTES
    assert len(plan["notes"].encode("utf-8")) > 4096
    _round_trip(plan)


@pytest.mark.parametrize("field", PROSE_FIELDS)
def test_each_accepted_plan_prose_field_survives_complete_bundle_chain(field):
    plan = _plan()
    plan["experiment_briefs"][0][field] = PLAN_PROSE
    assert len(PLAN_PROSE) == 8192 and len(PLAN_PROSE.encode("utf-8")) > 4096
    _round_trip(plan)


def test_accepted_plan_metadata_and_evidence_prose_do_not_get_projection_short_limits():
    plan = _plan()
    obligation = plan["answer_contract"]["obligations"][0]
    obligation["statement"] = PLAN_PROSE
    obligation["minimum_support"] = PLAN_PROSE
    obligation["idea_relevance"][0]["rationale"] = PLAN_PROSE
    contract_without_hash = {key: value for key, value in plan["answer_contract"].items()
                             if key != "answer_contract_hash"}
    plan["answer_contract"]["answer_contract_hash"] = canonical_hash(contract_without_hash)
    plan["coverage"][0]["insufficiency"] = PLAN_PROSE
    source_ref = "scientific-outcome:accepted-source"
    use = {"obligation_key": "cross-device", "evidence_ref": source_ref,
           "supported_claim": PLAN_PROSE, "support_boundary": PLAN_PROSE,
           "contributing_idea_refs": ["topology"]}
    plan["coverage"][0]["evidence_uses"] = [use]
    plan["evidence_reuse_set"] = [deepcopy(use)]
    plan["source_bindings"]["selected_evidence_catalog"] = [
        {"schema_ref": "meta-research/evidence-source-ref/v1", "evidence_ref": source_ref,
         "source_kind": "ScientificOutcome", "source_ref": source_ref}
    ]
    _round_trip(plan)


def test_long_accepted_plan_semantics_and_hash_are_still_exact():
    plan = _plan()
    plan["notes"] = LONG_NOTES
    for field in PROSE_FIELDS:
        plan["experiment_briefs"][0][field] = PLAN_PROSE
    contract, candidate_value = _round_trip(plan)
    completion_value = normalized_completion_contract_to_dict(contract)
    changed_plan = deepcopy(plan)
    changed_plan["notes"] += "变"
    with pytest.raises(BundleTargetContractError, match="completion_contract_plan_drift"):
        normalized_completion_contract_from_dict(completion_value, plan_document=changed_plan)
    changed_completion = deepcopy(completion_value)
    changed_completion["experiments"][0]["semantic_inputs"]["goal"] = "变" + PLAN_PROSE[1:]
    with pytest.raises(BundleTargetContractError, match="completion_contract_semantic_drift"):
        normalized_completion_contract_from_dict(changed_completion, plan_document=plan)
    changed_candidate = deepcopy(candidate_value)
    changed_candidate["semantic_inputs"][0]["characteristics"] = "变" + PLAN_PROSE[1:]
    with pytest.raises(BundleTargetContractError, match="candidate_semantic_input_drift"):
        formal_target_candidate_from_dict(changed_candidate, completion_contract=contract)


def test_accepted_plan_still_obeys_complete_root_byte_budget():
    plan = _plan()
    plan["notes"] = "x" * BUNDLE_ROOT_MAX_SERIALIZED_BYTES
    _validate_accepted_plan(plan)
    with pytest.raises(BundleTargetContractError, match="exceeds byte budget"):
        build_normalized_completion_contract(plan, bundle_fixtures._normalizations())


def test_long_plan_prose_does_not_widen_candidate_reference_budget():
    plan = _plan()
    plan["notes"] = LONG_NOTES
    contract, candidate_value = _round_trip(plan)
    candidate_value["candidate"]["local_label"] = "x" * 4097
    with pytest.raises(BundleTargetContractError, match="Target local label_invalid"):
        formal_target_candidate_from_dict(candidate_value, completion_contract=contract)


def test_typed_experiment_brief_uses_plan_prose_contract_and_keeps_other_budgets():
    brief = ExperimentBrief(experiment_key="experiment-a", semantic_delta=PLAN_PROSE,
                            held_fixed_slots=("shared-model",),
                            required_measurement_unit_keys=("cell-a",))
    assert len(validate_closed_bundle_projection(brief)) == 64
    with pytest.raises(BundleProtocolError, match="oversized text"):
        validate_closed_bundle_projection(replace(brief, semantic_delta=PLAN_PROSE + "研"))
    with pytest.raises(BundleProtocolError, match="oversized text"):
        validate_closed_bundle_projection(replace(brief, experiment_key="x" * 4097))
    with pytest.raises(BundleProtocolError, match="root projection byte budget"):
        validate_closed_bundle_projection(brief, max_serialized_bytes=4096)


def test_code_review_scope_retains_inherited_plan_prose_without_widening_refs():
    binding = ContentBindingProof(subject_ref="accepted-source", content_hash_ref="a" * 64)
    receipt = ReceiptProof(receipt_ref="accepted-receipt", subject_ref="accepted-source",
                           verified=True, currentness_known=True, current=True)
    scope = CodeReviewScope(candidate_revision_binding=binding, target_spec_binding=binding,
                            target_spec_acceptance_receipt=receipt, formal_plan_binding=binding,
                            formal_plan_acceptance_receipt=receipt, experiment_keys=("experiment-a",),
                            semantic_deltas=(PLAN_PROSE,), held_fixed_bindings=(),
                            accepted_input_refs=(), reuse_provenance_refs=(), repository_standards_refs=())
    assert len(validate_closed_bundle_projection(scope)) == 64
    with pytest.raises(BundleProtocolError, match="oversized text"):
        validate_closed_bundle_projection(replace(scope, semantic_deltas=(PLAN_PROSE + "研",)))
    with pytest.raises(BundleProtocolError, match="oversized text"):
        validate_closed_bundle_projection(replace(scope, accepted_input_refs=("x" * 4097,)))


@pytest.mark.parametrize("delta", [
    " 保留正文边界 ", "保留\n正文边界", "保留\r\n正文边界",
], ids=["padded", "newline", "crlf"])
def test_accepted_plan_prose_survives_real_protected_execution_preflight(delta):
    plan = _plan()
    plan["experiment_briefs"][0]["semantic_delta"] = delta
    plan_hash = _validate_accepted_plan(plan)
    handle = target_fixtures._handle("plan-prose")
    revision = "implementation-plan-prose"
    base_scope = target_fixtures._scope(handle, revision, "plan-prose")
    scope = replace(
        base_scope,
        formal_plan_binding=replace(base_scope.formal_plan_binding, content_hash_ref=plan_hash),
        formal_plan_acceptance_receipt=replace(base_scope.formal_plan_acceptance_receipt,
                                              subject_ref=plan_hash),
        experiment_keys=(plan["experiment_briefs"][0]["experiment_key"],),
        semantic_deltas=(delta,),
    )
    preflight, actual_scope = target_fixtures._preflight(handle, revision, "plan-prose", scope=scope)
    assert actual_scope == scope and preflight.review_scope.semantic_deltas == (delta,)
    admission = {"expected_review_scope": scope,
                 "expected_implementation_revision_ref": revision, "expected_code_changed": True}
    assert len(validate_protected_execution_admission(handle, preflight, **admission)) == 64
    with pytest.raises(TargetRunContractError, match="exact authoritative scope"):
        validate_protected_execution_admission(
            handle, preflight, **{**admission, "expected_review_scope": replace(scope, semantic_deltas=(delta + "变",))})
    assert plan["experiment_briefs"][0]["semantic_delta"] == delta
    assert canonical_hash(plan) == plan_hash


def test_generic_measurement_domain_cannot_use_plan_like_keys_to_widen_text_budget():
    value = bundle_fixtures._measurement_contract("domain", "cell-a", experiment_keys=("experiment-a",))
    # Field names alone do not establish that this is inherited accepted Plan prose.
    value["baseline_forward_contract"] = {
        "method": {"experiment_key": "experiment-a", "goal": "研" * 1366,
                   "characteristics": "x", "boundary_constraints": "x", "semantic_delta": "x"}
    }
    assert len(value["baseline_forward_contract"]["method"]["goal"]) < 8192
    assert len(value["baseline_forward_contract"]["method"]["goal"].encode("utf-8")) > 4096
    with pytest.raises(BundleTargetContractError, match="oversized text"):
        measurement_contract_from_dict(value)
