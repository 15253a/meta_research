"""Keep the reviewed instruction update separate from execution authorization."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research import root_capabilities
from meta_research.owners.agent_runtime import (
    BundleRuntimeBinding, _runtime_binding_from_row, _validated_runtime_binding,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.root_operation_diagnostics import RootOperationDiagnosticStore
from meta_research.runtime_binding_compatibility import (
    bundle_bindings_compatible, reviewed_historical_root_profile_hashes,
)


FIXTURES = Path(__file__).parent / "fixtures" / "root_prompt_20260929"
HANDOFF_PROFILES = Path(__file__).parent / "fixtures" / "root_prompt_bundle_handoff_20260929" / "profiles.json"
WORKSPACE_PROFILES = Path(__file__).parent / "fixtures" / "root_workspace_20261007" / "profiles.json"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def binding(value):
    return BundleRuntimeBinding(**{
        key: tuple(item) if key.endswith("_bindings") else item
        for key, item in value.items()
    })


@pytest.mark.parametrize("root_kind", root_capabilities.ROOT_AGENT_KINDS)
def test_workspace_guidance_preserves_exact_historical_read_profiles(root_kind):
    document = json.loads(WORKSPACE_PROFILES.read_text(encoding="utf-8"))
    before, after = document["before_profile"], document["after_profile"]
    assert canonical_hash(before) == document["before_profile_hash"]
    assert canonical_hash(after) == document["after_profile_hash"]
    assert root_capabilities.root_capability_profile(root_kind).as_dict() == after
    assert [key for key in before if before[key] != after[key]] == ["research_system_prompt_hash"]
    assert reviewed_historical_root_profile_hashes(document["after_profile_hash"]) == {
        load("before.json")["root_profile_hash"], load("after.json")["root_profile_hash"],
        json.loads(HANDOFF_PROFILES.read_text(encoding="utf-8"))["current_profile_hash"],
        document["before_profile_hash"],
    }


@pytest.fixture
def pair():
    return tuple(binding(load(name)["runtime_binding"]) for name in ("before.json", "after.json"))


def test_actual_pair_changes_only_reviewed_research_text(pair):
    before_doc, after_doc = load("before.json"), load("after.json")
    before, after = pair
    for document in (before_doc, after_doc):
        assert canonical_hash(document["root_profile"]) == document["root_profile_hash"]
        assert canonical_hash(document["runtime_binding"]) == document["runtime_binding_hash"]
    # This pair remains immutable evidence of the preceding deployment.
    assert after_doc["root_profile_hash"] in reviewed_historical_root_profile_hashes(
        root_capabilities.root_capability_profile("bundle").digest,
    )
    assert [key for key in before_doc["root_profile"] if before_doc["root_profile"][key] != after_doc["root_profile"][key]] == ["research_system_prompt_hash"]
    assert [key for key in before.as_dict() if before.as_dict()[key] != after.as_dict()[key]] == ["capability_bindings"]
    assert len(before.capability_bindings) == len(after.capability_bindings)
    changes = [(a, b) for a, b in zip(before.capability_bindings, after.capability_bindings) if a != b]
    assert changes == [(
        "root-capability-profile:sha256:" + before_doc["root_profile_hash"],
        "root-capability-profile:sha256:" + after_doc["root_profile_hash"],
    )]
    original = deepcopy((before.as_dict(), after.as_dict()))
    assert bundle_bindings_compatible(before, after)
    assert bundle_bindings_compatible(after, before)
    assert (before.as_dict(), after.as_dict()) == original


@pytest.mark.parametrize("root_kind", root_capabilities.ROOT_AGENT_KINDS)
def test_handoff_profiles_change_only_research_text_for_every_root(root_kind):
    document = json.loads(HANDOFF_PROFILES.read_text(encoding="utf-8"))
    # This immutable fixture describes the preceding prompt-only deployment.
    # The current model upgrade has its own full-profile fixture and tests.
    reviewed_profile = document["current_profile"]
    reviewed_hash = canonical_hash(reviewed_profile)
    assert document["reviewed_root_kinds"] == list(root_capabilities.ROOT_AGENT_KINDS)
    assert root_kind in document["reviewed_root_kinds"]
    assert reviewed_hash == document["current_profile_hash"]
    historical_hashes = set()
    for historical in document["historical_profiles"]:
        original = load(historical["original_fixture"])
        assert historical["root_profile"] == original["root_profile"]
        assert historical["root_profile_hash"] == original["root_profile_hash"]
        assert canonical_hash(historical["root_profile"]) == historical["root_profile_hash"]
        assert [
            key for key in historical["root_profile"]
            if historical["root_profile"][key] != reviewed_profile[key]
        ] == ["research_system_prompt_hash"]
        historical_hashes.add(historical["root_profile_hash"])
    assert reviewed_historical_root_profile_hashes(reviewed_hash) == historical_hashes


@pytest.mark.parametrize("root_kind", root_capabilities.ROOT_AGENT_KINDS)
@pytest.mark.parametrize("profile_fixture", ("before.json", "after.json"))
def test_each_reviewed_profile_diagnostic_preserves_its_identity(root_kind, profile_fixture):
    # Synthetic diagnostics isolate profile compatibility; the genuine signed
    # diagnostic fixture is verified separately below and is never rewritten.
    diagnostic = root_capabilities.root_capability_profile(root_kind).public_diagnostics()
    diagnostic["capability_profile_hash"] = load(profile_fixture)["root_profile_hash"]
    original = deepcopy(diagnostic)
    assert root_capabilities.validate_root_capability_diagnostics(diagnostic) == original
    assert diagnostic == original


@pytest.mark.parametrize("stage", ("idea", "plan", "bundle", "reasoning"))
@pytest.mark.parametrize("profile_fixture", ("before.json", "after.json"))
def test_each_reviewed_profile_is_read_only_for_stage_bindings(stage, profile_fixture):
    original = _runtime_binding_from_row(SimpleNamespace(**load("historical-stage-rows.json")[stage]))
    historical = replace(original, capability_bindings=tuple(
        "root-capability-profile:sha256:" + load(profile_fixture)["root_profile_hash"]
        if item.startswith("root-capability-profile:sha256:") else item
        for item in original.capability_bindings
    ))
    accepted, _serialized, digest = _validated_runtime_binding(
        historical, stage=stage, allow_historical_root_profile=True,
    )
    assert accepted == historical
    assert digest == canonical_hash(historical.as_dict())
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        _validated_runtime_binding(historical, stage=stage)


def test_historical_profile_read_support_does_not_grant_bundle_execution(pair):
    for historical in pair:
        current_profile_only = replace(historical, capability_bindings=tuple(
            "root-capability-profile:sha256:" + root_capabilities.root_capability_profile("bundle").digest
            if item.startswith("root-capability-profile:sha256:") else item
            for item in historical.capability_bindings
        ))
        assert not bundle_bindings_compatible(historical, current_profile_only)
        assert not bundle_bindings_compatible(current_profile_only, historical)


@pytest.mark.parametrize("side", (0, 1))
@pytest.mark.parametrize("field", (
    "model_ref", "harness_adapter_ref", "schema_ref", "instruction_set_hash",
    "packaged_skill_bundle_hash", "mcp_bindings", "capability_bindings", "resource_bindings",
))
def test_execution_exception_requires_the_complete_exact_pair(pair, side, field):
    subject, other = pair[side], pair[1 - side]
    original = getattr(subject, field)
    changed = original + ("unreviewed",) if isinstance(original, tuple) else "unreviewed:" + original
    drift = replace(subject, **{field: changed})
    assert not bundle_bindings_compatible(drift, other)
    assert not bundle_bindings_compatible(other, drift)


@pytest.mark.parametrize("side", (0, 1))
def test_every_execution_resource_remains_exact(pair, side):
    subject, other = pair[side], pair[1 - side]
    for field in ("mcp_bindings", "capability_bindings", "resource_bindings"):
        original = getattr(subject, field)
        for index in range(len(original)):
            for mutated in (
                original[:index] + original[index + 1:],
                original[:index] + (original[index] + ":changed",) + original[index + 1:],
                original[:index] + (original[index],) + original[index:],
                tuple(reversed(original)),
            ):
                assert not bundle_bindings_compatible(replace(subject, **{field: mutated}), other)


@pytest.mark.parametrize("stage", ("idea", "plan", "bundle", "reasoning"))
def test_real_historical_admission_reads_preserve_hashes_but_cannot_admit_again(stage):
    row = SimpleNamespace(**load("historical-stage-rows.json")[stage])
    old = _runtime_binding_from_row(row)
    assert canonical_hash(old.as_dict()) == row.runtime_binding_hash
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        _validated_runtime_binding(old, stage=stage)
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        _validated_runtime_binding(
            replace(old, capability_bindings=old.capability_bindings + ("unreviewed-grant",)),
            stage=stage, allow_historical_root_profile=True,
        )
    row.runtime_binding_hash = "f" * 64
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_invalid"):
        _runtime_binding_from_row(row)


def test_real_historical_diagnostic_preserves_its_original_profile_and_hash():
    document = load("historical-diagnostic.json")
    original = deepcopy(document)
    validated = RootOperationDiagnosticStore._validated_record(document)
    assert validated.as_public_dict() == document
    projected = root_capabilities.project_codex_post_turn_diagnostics(document["diagnostics"], "")
    assert projected["capability_profile_hash"] == document["diagnostics"]["capability_profile_hash"]
    assert root_capabilities.validate_root_capability_diagnostics(projected) == projected
    assert document == original


@pytest.mark.parametrize("change", ("model", "reasoning", "prompt"))
def test_future_configuration_does_not_inherit_historical_exceptions(monkeypatch, change):
    field = {"model": "CODEX_MODEL_REF", "reasoning": "CODEX_REASONING_EFFORT", "prompt": "RESEARCH_SYSTEM_PROMPT"}[change]
    monkeypatch.setattr(root_capabilities, field, getattr(root_capabilities, field) + "-unreviewed")
    profile = root_capabilities.root_capability_profile("bundle")
    assert reviewed_historical_root_profile_hashes(profile.digest) == frozenset()
    with pytest.raises(ValueError, match="root_capability_diagnostic_invalid"):
        root_capabilities.validate_root_capability_diagnostics(load("historical-diagnostic.json")["diagnostics"])
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        _runtime_binding_from_row(SimpleNamespace(**load("historical-stage-rows.json")["bundle"]))


def test_unknown_profile_and_malformed_diagnostics_stay_rejected():
    diagnostic = load("historical-diagnostic.json")["diagnostics"]
    unknown = {**diagnostic, "capability_profile_hash": "f" * 64}
    with pytest.raises(ValueError, match="root_capability_diagnostic_invalid"):
        root_capabilities.validate_root_capability_diagnostics(unknown)
    malformed = deepcopy(diagnostic)
    malformed["side_effect_authorization"]["status"] = "unrestricted"
    with pytest.raises(ValueError, match="root_capability_diagnostic_invalid"):
        root_capabilities.validate_root_capability_diagnostics(malformed)
    # Historical support is directional and never authorizes rollback reads.
    assert reviewed_historical_root_profile_hashes(load("before.json")["root_profile_hash"]) == frozenset()
