from pathlib import Path
import json
from dataclasses import replace

import pytest

from meta_research.runtime_binding_compatibility import target_request_catalog_compatible, bundle_bindings_compatible
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.semantic_owner_gateway import TARGET_ROOT_SEMANTIC_OPERATION_IDS


FIXTURES = Path(__file__).parent / "fixtures/data_protocol_20260914"


def _protocol_catalog():
    # Freeze the 8767 catalog from its recorded binding and the four reviewed
    # 2026-09-14 reads. Deriving history from today's grants silently imports
    # later effects (such as Dataset derivation) into this historical fixture.
    before = tuple(json.loads((FIXTURES / "target-binding-before.json").read_text(
        encoding="utf-8-sig"))["required_operation_ids"])
    return (
        "research_memory.research_notes.read", before[0],
        "research_graph.baselines.page", "research_graph.baselines.read",
        "research_graph.target_formal_results.read", *before[1:],
    )


def test_pre_protocol_target_request_remains_readable_with_its_exact_old_catalog():
    binding = json.loads((FIXTURES / "target-binding-before.json").read_text(encoding="utf-8-sig"))
    operations = tuple(binding["required_operation_ids"])
    assert target_request_catalog_compatible(operations, binding, _protocol_catalog())
    assert operations != _protocol_catalog()
    assert not target_request_catalog_compatible(operations, binding, TARGET_ROOT_SEMANTIC_OPERATION_IDS)


@pytest.mark.parametrize("added_operation", [
    "research_graph.datasets.derive", "research_graph.datasets.derive.reconcile",
    "human_request.read", "agent_runtime.target_run.progress", "unreviewed.tool.effect",
])
def test_historical_catalog_does_not_gain_later_or_unknown_operations(added_operation):
    binding = json.loads((FIXTURES / "target-binding-before.json").read_text(encoding="utf-8-sig"))
    operations = tuple(binding["required_operation_ids"])
    assert added_operation not in _protocol_catalog()
    assert not target_request_catalog_compatible(operations, binding, _protocol_catalog() + (added_operation,))


def test_current_target_catalog_requires_its_exact_operation_sequence():
    current = TARGET_ROOT_SEMANTIC_OPERATION_IDS
    binding = {"required_operation_ids": list(current)}
    # This check only compares catalog scope; Harness independently verifies
    # the full conformance binding hash and its exact admitted request.
    assert target_request_catalog_compatible(current, binding, current)
    assert "research_graph.datasets.derive" in current
    assert not target_request_catalog_compatible(current[:-1], binding, current)
    assert not target_request_catalog_compatible(tuple(reversed(current)), binding, current)


@pytest.mark.parametrize("field,value", [
    ("semantic_mcp_catalog_hash", "0" * 64),
    ("semantic_mcp_operation_bindings_hash", "0" * 64),
    ("contract_hash", "0" * 64),
    ("required_capabilities", ["semantic_mcp", "shell"]),
    ("required_families", ["claude"]),
])
def test_unreviewed_old_target_catalog_changes_are_rejected(field, value):
    binding = json.loads((FIXTURES / "target-binding-before.json").read_text(encoding="utf-8-sig"))
    operations = tuple(binding["required_operation_ids"])
    binding[field] = value
    assert not target_request_catalog_compatible(operations, binding, _protocol_catalog())


def _bundle(which):
    value = json.loads((FIXTURES / f"bundle-{which}.json").read_text())
    for field in ("mcp_bindings", "capability_bindings", "resource_bindings"):
        value[field] = tuple(value[field])
    return BundleRuntimeBinding(**value)


def test_reviewed_protocol_deployment_accepts_only_the_exact_bundle_binding_pair():
    before, after = _bundle("before"), _bundle("after")
    assert before != after
    assert bundle_bindings_compatible(before, after)
    assert bundle_bindings_compatible(after, before)
    assert not bundle_bindings_compatible(before, replace(after, model_ref="different-model"))
    assert not bundle_bindings_compatible(before, replace(after, capability_bindings=after.capability_bindings + ("new-effect",)))
    assert not bundle_bindings_compatible(before, replace(after, resource_bindings=after.resource_bindings[:-1]))
    assert not bundle_bindings_compatible(before, replace(after, mcp_bindings=before.mcp_bindings))


def test_catalog_delta_preserves_every_existing_binding_and_adds_only_four_reads():
    def load(which):
        return {row["semantic_operation_id"]: row for row in json.loads((FIXTURES / f"operations-{which}.json").read_text())}
    before, after = load("before"), load("after")
    assert {key: after[key] for key in before} == before
    additions = set(after) - set(before)
    assert additions == {"research_graph.baselines.page", "research_graph.baselines.read",
        "research_graph.target_formal_results.read", "research_memory.research_notes.read"}
    assert all(after[key]["access_mode"] == "read" for key in additions)
