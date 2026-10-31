"""Research planning keeps uncovered responsibilities distinct from arranged work."""
from copy import deepcopy

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.plan_contract import PlanContractError, validate_plan_document
from test_plan_skill_contract import _EVIDENCE_REF, _IDEA_SET, _plan_document, _selected_binding


def _validate(document):
    return validate_plan_document(
        document, question_ref="question_1", idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1", context_pack_hash="6" * 64,
        accepted_idea_set=_IDEA_SET, evidence_by_ref={"evidence_1": _EVIDENCE_REF},
        evidence_reference_revision=1,
    )


def test_plan_may_leave_a_documented_gap_without_arranging_an_experiment():
    document = _plan_document()
    document["experiment_briefs"] = []
    document["bundle_disposition"] = "no_new_experiment_required"
    document["coverage"][1]["insufficiency"] = (
        "The cross-device evidence is missing; no usable second device is available "
        "this Cycle. The existing single-device finding remains limited to that device."
    )

    assert _validate(document) == canonical_hash(document)


def test_complete_adoption_keeps_materials_used_only_by_a_brief_without_a_page_quota():
    document = _plan_document()
    material_bindings = [
        {
            "evidence_ref": f"material:{number}", "source_kind": "AssetVersion",
            "source_ref": f"material:{number}",
            "source_binding": {"asset_ref": f"asset:{number}", "version_ref": f"material:{number}",
                               "receipt": {"receipt_ref": f"receipt:{number}"}},
            "reader": {"operation": "research_memory.content.read", "args": {
                "source_ref": f"asset:{number}", "version_ref": f"material:{number}",
            }},
        }
        for number in range(300)
    ]
    document["source_bindings"]["selected_evidence_bindings"] = [
        _selected_binding(_EVIDENCE_REF), *material_bindings,
    ]
    document["experiment_briefs"][0]["reuse_material_refs"] = [
        "material:0", "material:299",
    ]

    assert _validate(document) == canonical_hash(document)
    assert len(document["evidence_reuse_set"]) == 1


@pytest.mark.parametrize("references", [[], ["boundary", "boundary"], ["effect"], ["unknown"]])
def test_arranged_brief_references_nonempty_unique_existing_gaps(references):
    document = _plan_document()
    document["experiment_briefs"][0]["gap_obligation_keys"] = references
    with pytest.raises(PlanContractError, match="plan_gap_brief_closure_invalid"):
        _validate(document)


def test_multiple_briefs_may_advance_the_same_gap():
    document = _plan_document()
    second = deepcopy(document["experiment_briefs"][0])
    second["experiment_key"] = "independent-cross-device-check"
    document["experiment_briefs"].append(second)
    assert _validate(document) == canonical_hash(document)


def test_partial_arrangement_preserves_the_unarranged_gap_in_the_same_plan():
    document = _plan_document()
    document["coverage"][0] = {
        "obligation_key": "effect", "disposition": "gap", "evidence_uses": [],
        "insufficiency": "A comparable effect estimate is unavailable and remains outside this Cycle.",
    }
    document["evidence_reuse_set"] = []
    document["gap_set"] = ["effect", "boundary"]
    assert _validate(document) == canonical_hash(document)


def test_a_discovery_entry_is_not_implicitly_adopted():
    document = _plan_document()
    document["source_bindings"]["selected_evidence_bindings"] = []
    with pytest.raises(PlanContractError, match="plan_evidence_ref_unbound"):
        _validate(document)


@pytest.mark.parametrize("mutation", ["duplicate", "unbound_material", "old_source_field"])
def test_complete_adoption_retains_identity_and_explicit_reference_checks(mutation):
    document = _plan_document()
    if mutation == "duplicate":
        document["source_bindings"]["selected_evidence_bindings"] *= 2
        code = "plan_selected_evidence_invalid"
    elif mutation == "unbound_material":
        document["experiment_briefs"][0]["reuse_material_refs"] = ["unadopted-material"]
        code = "plan_reuse_material_ref_invalid"
    else:
        document["source_bindings"]["selected_evidence_catalog"] = document["source_bindings"].pop("selected_evidence_bindings")
        code = "plan_selected_evidence_invalid"
    with pytest.raises(PlanContractError, match=code):
        _validate(document)
