from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from meta_research.owners.agent_runtime import BundleRuntimeBinding, ReasoningRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import reasoning_bindings_compatible


def _load(name):
    fixture_root = Path(__file__).parent / "fixtures" / "reasoning_binding_compatibility"
    value = json.loads((fixture_root / name).read_text())["runtime_binding"]
    for field in ("resource_bindings", "mcp_bindings", "capability_bindings"):
        value[field] = tuple(value[field])
    return ReasoningRuntimeBinding(**value)


@pytest.fixture
def pair():
    return _load("reasoning-binding-before.json"), _load("reasoning-binding-after.json")


def test_actual_deployment_pair_preserves_frozen_bindings(pair):
    before, after = pair
    original = deepcopy((before.as_dict(), after.as_dict()))
    assert before != after
    assert canonical_hash(before.as_dict()) != canonical_hash(after.as_dict())
    assert reasoning_bindings_compatible(before, before)
    assert reasoning_bindings_compatible(after, after)
    assert reasoning_bindings_compatible(before, after)
    assert reasoning_bindings_compatible(after, before)
    assert (before.as_dict(), after.as_dict()) == original


def test_release_diff_contains_only_reviewed_prose_and_adapter(pair):
    before, after = pair
    assert len(before.resource_bindings) == len(after.resource_bindings)
    assert before.model_ref == after.model_ref
    assert before.harness_adapter_ref == after.harness_adapter_ref
    assert before.schema_ref == after.schema_ref
    assert before.mcp_bindings == after.mcp_bindings
    assert before.capability_bindings == after.capability_bindings
    allowed = (
        "package:meta_research.skills.reasoning_stage/SKILL.md@sha256:",
        "package:meta_research.skills.reasoning_stage/references/contract.md@sha256:",
        "package:meta_research.skills.reasoning_stage/references/owner-operations.md@sha256:",
        "adapter-source:meta_research.reasoning_skill@sha256:",
    )
    changed = [(old, new) for old, new in zip(before.resource_bindings, after.resource_bindings) if old != new]
    assert changed
    for old, new in changed:
        assert any(old.startswith(prefix) and new.startswith(prefix) for prefix in allowed)


@pytest.mark.parametrize("side", (0, 1))
@pytest.mark.parametrize("field", ("model_ref", "harness_adapter_ref", "schema_ref", "instruction_set_hash", "packaged_skill_bundle_hash", "mcp_bindings", "capability_bindings"))
def test_any_unreviewed_binding_field_fails(pair, side, field):
    subject, other = pair[side], pair[1-side]
    original = getattr(subject, field)
    value = original + ("unreviewed",) if isinstance(original, tuple) else "unreviewed:" + original
    drift = replace(subject, **{field: value})
    assert not reasoning_bindings_compatible(drift, other)
    assert not reasoning_bindings_compatible(other, drift)


@pytest.mark.parametrize("side", (0, 1))
@pytest.mark.parametrize("mutation", ("replace", "missing", "duplicate", "reorder"))
def test_every_resource_boundary_is_exact(pair, side, mutation):
    subject, other = pair[side], pair[1-side]
    for index in range(len(subject.resource_bindings)):
        resources = list(subject.resource_bindings)
        if mutation == "replace":
            resources[index] += ":unreviewed"
        elif mutation == "missing":
            resources.pop(index)
        elif mutation == "duplicate":
            resources.insert(index, resources[index])
        else:
            second = (index + 1) % len(resources)
            resources[index], resources[second] = resources[second], resources[index]
        drift = replace(subject, resource_bindings=tuple(resources))
        assert not reasoning_bindings_compatible(drift, other), (index, mutation)
        assert not reasoning_bindings_compatible(other, drift), (index, mutation)
    extra = replace(subject, resource_bindings=subject.resource_bindings + ("unreviewed-resource",))
    assert not reasoning_bindings_compatible(extra, other)


def test_later_prose_revision_does_not_inherit_this_exception(pair):
    before, after = pair
    resources = tuple(item + ":later-edit" if "/SKILL.md@sha256:" in item else item for item in after.resource_bindings)
    later = replace(after, instruction_set_hash="a" * 64, packaged_skill_bundle_hash="b" * 64, resource_bindings=resources)
    assert not reasoning_bindings_compatible(before, later)
    assert not reasoning_bindings_compatible(after, later)


def test_other_binding_types_do_not_cross_stage(pair):
    before, after = pair
    values = before.as_dict()
    values.pop("schema_ref")
    for field in ("resource_bindings", "mcp_bindings", "capability_bindings"):
        values[field] = tuple(values[field])
    bundle = BundleRuntimeBinding(**values)
    assert not reasoning_bindings_compatible(bundle, after)
    assert not reasoning_bindings_compatible(before.as_dict(), after)
    assert not reasoning_bindings_compatible(None, after)
