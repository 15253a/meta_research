"""Accepted Plans retain unscheduled research gaps at downstream seams."""
from meta_research.bundle_contract import validate_bundle_context_pack
from meta_research.bundle_target_contract import build_normalized_completion_contract
from meta_research.owners.common import canonical_hash
from meta_research.reasoning_contract import plan_evidence_reuse_leaves
from test_bundle_target_contract import _normalizations, _plan


def test_bundle_normalizes_only_the_briefs_actually_scheduled_by_plan():
    plan = _plan()
    plan["gap_set"].append("gap-for-later")
    # The remaining responsibility stays in Plan; Bundle must not manufacture
    # a cell or Target for an unscheduled obligation.
    plan["coverage"] = [{
        "obligation_key": "gap-for-later",
        "decision": "gap",
        "evidence_uses": [],
        "insufficiency": "The current material does not resolve this branch; defer it until data arrives.",
    }]
    contract = build_normalized_completion_contract(plan, _normalizations())
    assert [item.semantic_inputs.experiment_key for item in contract.experiments] == ["experiment-a"]
    assert contract.experiments[0].brief.required_measurement_unit_keys == ("cell-a", "cell-b")


def test_empty_briefs_retain_gaps_in_the_exact_accepted_plan_binding():
    plan = _plan()
    plan["experiment_briefs"] = []
    plan["bundle_disposition"] = "no_new_experiment_required"
    question_binding = {"question_ref": plan["question_ref"]}
    formal_binding = {
        "plan_document": plan,
        "plan_document_hash": canonical_hash(plan),
        "answer_contract_hash": plan["answer_contract"]["answer_contract_hash"],
    }
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle-1",
        "cycle_position": {"cycle_ref": "cycle-1", "round": 1,
                           "cycle_focus": "Resolve the available branch",
                           "focus_source": {"request_ref": "start-1"}},
        "accepted_question_binding": question_binding,
        "accepted_formal_plan_binding": formal_binding,
    }
    accepted = validate_bundle_context_pack(
        context,
        cycle_ref="cycle-1",
        accepted_question_binding=question_binding,
        accepted_formal_plan_binding=formal_binding,
    )
    assert accepted["gap_set"] == ["gap-a"]
    assert accepted["experiment_briefs"] == []


def test_bundle_preserves_brief_only_reuse_materials_in_plan_identity():
    plan = _plan()
    materials = [f"asset-version:{index}" for index in range(33)]
    plan["experiment_briefs"][0]["reuse_material_refs"] = materials
    plan["source_bindings"] = {"selected_evidence_bindings": [
        {"evidence_ref": ref, "source_kind": "AssetVersion", "source_ref": ref,
         "source_binding": {"kind": "AssetVersion", "ref": ref}, "reader": {"ref": ref}}
        for ref in materials
    ]}
    contract = build_normalized_completion_contract(plan, _normalizations())
    assert contract.plan_document_hash == canonical_hash(plan)
    assert plan["evidence_reuse_set"] == []
    assert plan["experiment_briefs"][0]["reuse_material_refs"] == materials


def test_reasoning_consumes_generic_evidence_from_the_complete_adopted_binding():
    source = {"kind": "HumanInput", "ref": "human_input_1", "input_ref": "human_input_1",
              "content_hash": "c" * 64, "owner_acceptance_receipt_ref": "human-receipt-1"}
    selected = {"evidence_ref": "human_input_1", "source_kind": "HumanInput", "source_ref": "human_input_1",
                "source_binding": source,
                "reader": {"operation": "research_memory.content.read",
                           "args": {"source_ref": "human_input_1", "version_ref": "c" * 64}}}
    use = {"evidence_ref": "human_input_1", "obligation_key": "human-observation"}
    leaf = {
        "schema_ref": "meta-research/evidence-reuse-leaf/v1", "kind": "EvidenceReuseLeaf",
        "role": "HumanInput", "evidence_ref": "human_input_1", "evidence_item_ref": "human_input_1",
        "source_role_ref": None, "source_variant_run_ref": None, "source_evaluation_attempt_ref": None,
        "source_subject_kind": "HumanInput", "source_subject_ref": "human_input_1", "target_commit_ref": None,
        "asset_version_ref": None, "evidence_catalog_entry_hash": canonical_hash(selected),
        "evidence_use_hashes": [canonical_hash(use)], "evidence_asset_receipt": None,
        "evidence_role_receipt": None, "formal_measurement_acceptance_receipt": None,
        "target_commit_acceptance_receipt": None, "source_binding": source,
    }
    context = {"plan_evidence_input": {
        "kind": "accepted", "formal_plan_binding": {"plan_document": {
            "source_bindings": {"selected_evidence_bindings": [selected]},
        }}, "evidence_reuse_set": [use], "evidence_reuse_closure": [leaf],
    }}
    assert plan_evidence_reuse_leaves(context) == [source]

