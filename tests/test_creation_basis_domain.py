import hashlib

import pytest

from meta_research.creation_basis import (
    _material_revision_understanding,
    empty_understanding,
    validate_understanding,
)
from meta_research.owners.common import OwnerConflict


def material():
    content = b"Room calibration helps. Cold calibration increases error."
    manifest = {"entries": [{"material_key": "notes", "bytes": len(content)}]}
    witness = {"material_key": "notes", "offset": 0, "length": len(content),
        "chunk_sha256": hashlib.sha256(content).hexdigest(), "location": "notes lines 1-2"}
    return content, manifest, witness


def test_key_claims_require_observed_bytes_and_keep_unread_entrances():
    content, manifest, witness = material()
    value = empty_understanding()
    value["coverage"] = [{"material_key": "notes", "kind": "read", "read_ranges": [witness], "unread_description": ""}]
    value["claims_and_conditions"] = [{"ref": "claim-room", "text": "Calibration improves the room result and worsens the cold result.",
        "kind": "reported_work", "conditions": ["Room and cold conditions differ."], "sources": [witness]}]
    value["selection"] = [{"material_key": "notes", "reason": "Retain the contradictory results."}]
    accepted = validate_understanding(value, manifest, lambda entry, offset, length: content[offset:offset + length])
    assert accepted["claims_and_conditions"][0]["conditions"] == ["Room and cold conditions differ."]
    value["claims_and_conditions"][0]["sources"][0] = {**witness, "chunk_sha256": "0" * 64}
    with pytest.raises(OwnerConflict, match="creation_understanding_invalid"):
        validate_understanding(value, manifest, lambda entry, offset, length: content[offset:offset + length])


def test_an_unread_source_can_be_selected_but_cannot_support_a_content_claim():
    content, manifest, witness = material()
    value = empty_understanding()
    value["coverage"] = [{"material_key": "notes", "kind": "unread", "read_ranges": [], "unread_description": "Retained for later review."}]
    value["selection"] = [{"material_key": "notes", "reason": "Retain the supplied notes without claiming they were read."}]
    accepted = validate_understanding(value, manifest, lambda entry, offset, length: content[offset:offset + length])
    assert accepted["coverage"][0]["unread_description"] == "Retained for later review."
    value["work_already_done"] = [{"ref": "work", "text": "Calibration was verified.", "kind": "reported_work", "conditions": [], "sources": [witness]}]
    with pytest.raises(OwnerConflict, match="creation_understanding_invalid"):
        validate_understanding(value, manifest, lambda entry, offset, length: content[offset:offset + length])


def test_literature_only_revision_coverage_is_kept_out_of_material_validation():
    value = empty_understanding()
    value["coverage"] = [{
        "material_key": "literature_snapshot_1",
        "kind": "partial",
        "read_ranges": [],
        "unread_description": "The snapshot has no byte witness in this basis.",
    }]
    accepted = _material_revision_understanding(
        value, {"entries": []}, "literature_snapshot_1"
    )
    assert accepted["coverage"] == []


@pytest.mark.parametrize(
    "invalid", ["foreign_key", "snapshot_source", "snapshot_selection", "snapshot_read", "snapshot_without_limit"]
)
def test_literature_only_revision_rejects_unbound_evidence(invalid):
    value = empty_understanding()
    value["coverage"] = [{
        "material_key": "literature_snapshot_1" if invalid != "foreign_key" else "literature_snapshot_2",
        "kind": "read" if invalid == "snapshot_read" else "partial",
        "read_ranges": [],
        "unread_description": "" if invalid == "snapshot_without_limit" else "The snapshot has no byte witness in this basis.",
    }]
    if invalid == "snapshot_source":
        value["claims_and_conditions"] = [{
            "ref": "unsupported",
            "text": "The snapshot supports this claim.",
            "kind": "reported_work",
            "conditions": [],
            "sources": [{"material_key": "literature_snapshot_1"}],
        }]
    if invalid == "snapshot_selection":
        value["selection"] = [{
            "material_key": "literature_snapshot_1",
            "reason": "Select the snapshot.",
        }]
    with pytest.raises(ValueError):
        _material_revision_understanding(
            value, {"entries": []}, "literature_snapshot_1"
        )


@pytest.mark.parametrize("field", ["work_already_done", "claims_and_conditions", "conflicts"])
def test_reference_schema_rejects_uncited_runtime_facts_like_content_validator(field):
    from jsonschema import Draft202012Validator, ValidationError
    from meta_research.creation_basis import understanding_schema, validate_reference_understanding
    from meta_research.creation_inputs import CreationAnchor, CreationInputIdentity, MaterialSet

    value = empty_understanding()
    value[field] = [{"ref": "work-parent-boundary", "text": "The native parent ran a program and rejected external writes.",
        "kind": "reported_work", "conditions": ["Runtime receipt only."], "sources": []}]
    schema = Draft202012Validator(understanding_schema(references=True))
    with pytest.raises(ValidationError):
        schema.validate(value)
    anchor = CreationAnchor("quest_initialization", "initialization", None, 1, "a" * 64)
    inputs = MaterialSet(anchor, ())
    with pytest.raises(OwnerConflict, match="creation_understanding_invalid"):
        validate_reference_understanding(value, inputs, CreationInputIdentity(anchor, inputs.set_hash, ()), None)
    value[field] = []
    value["gaps"] = [{"ref": "gap", "text": "No research evidence supports this runtime observation.",
        "kind": "agent_inference", "conditions": [], "sources": []}]
    schema.validate(value)
    validate_reference_understanding(value, inputs, CreationInputIdentity(anchor, inputs.set_hash, ()), None)
