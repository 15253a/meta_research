"""Resume the actual frozen Bundle after the reviewed system-help guidance fix."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible


FIXTURES = Path(__file__).parent / "fixtures" / "bundle_frozen_input_recovery_20260930"
BEFORE_HASH = "43b138c196cae3a8e205c2b8b66fc2c9d6ce3b71bf041bede7283ad1e18f95ad"
AFTER_HASH = "a72d15e5a107435726c06beccfbe4178c6dad3cabd8a9d826876a57c1fad8996"
OWNER_RESOURCE = "package:meta_research.skills.bundle_stage/references/owner-operations.md@sha256:"


def _binding(name: str) -> BundleRuntimeBinding:
    value = json.loads((FIXTURES / f"binding-{name}.json").read_text(encoding="utf-8"))
    for field in ("mcp_bindings", "capability_bindings", "resource_bindings"):
        value[field] = tuple(value[field])
    return BundleRuntimeBinding(**value)


def _replace_resource(binding: BundleRuntimeBinding, prefix: str) -> BundleRuntimeBinding:
    assert sum(entry.startswith(prefix) for entry in binding.resource_bindings) == 1
    return replace(binding, resource_bindings=tuple(
        prefix + "f" * 64 if entry.startswith(prefix) else entry
        for entry in binding.resource_bindings
    ))


def test_actual_frozen_run_passes_worker_gate_without_rebinding() -> None:
    frozen, candidate = _binding("before"), _binding("after")
    original = frozen.as_dict()
    assert canonical_hash(original) == BEFORE_HASH
    assert canonical_hash(candidate.as_dict()) == AFTER_HASH
    assert bundle_bindings_compatible(frozen, candidate)
    assert bundle_bindings_compatible(candidate, frozen)

    worker = object.__new__(BundleStageWorker)
    worker._transient_error = None
    worker._current_runtime_binding = lambda: candidate
    assert worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=frozen))
    assert worker._transient_error is None
    assert frozen.as_dict() == original


def test_complete_pair_matches_actual_instruction_resources_and_sources() -> None:
    before, after = _binding("before").as_dict(), _binding("after").as_dict()
    assert [field for field in before if before[field] != after[field]] == [
        "packaged_skill_bundle_hash", "instruction_set_hash", "resource_bindings",
    ]
    changed_resources = {(old, new) for old, new in zip(before["resource_bindings"], after["resource_bindings"]) if old != new}
    assert changed_resources == {
        (next(entry for entry in before["resource_bindings"] if entry.startswith(prefix)),
         next(entry for entry in after["resource_bindings"] if entry.startswith(prefix)))
        for prefix in (OWNER_RESOURCE, "adapter-source:meta_research.bundle_skill@sha256:")
    }
    sources = {
        "adapter_source_hash": "bundle_skill.py",
        "shared_adapter_source_hash": "idea_skill.py",
        "supervisor_source_hash": "provider_supervisor.py",
        "dispatch_recovery_source_hash": "bundle_dispatch_recovery.py",
    }
    source_hashes = {}
    for field, filename in sources.items():
        path = Path(bundle_skill.__file__).with_name(filename)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert f"adapter-source:meta_research.{filename[:-3]}@sha256:{digest}" in after["resource_bindings"]
        source_hashes[field] = digest

    resources = bundle_skill._bundle_skill_resources()
    assert "system_operation_help" in resources["references/owner-operations.md"]
    for name, binding in (("before", before), ("after", after)):
        texts = dict(resources)
        if name == "before":
            texts["references/owner-operations.md"] = (FIXTURES / "owner-operations-before.md").read_text(encoding="utf-8")
        bound_source_hashes = {}
        for field, filename in sources.items():
            prefix = f"adapter-source:meta_research.{filename[:-3]}@sha256:"
            matches = [entry.removeprefix(prefix) for entry in binding["resource_bindings"] if entry.startswith(prefix)]
            assert len(matches) == 1
            bound_source_hashes[field] = matches[0]
        if name == "after":
            assert bound_source_hashes == source_hashes
        assert binding["packaged_skill_bundle_hash"] == canonical_hash(texts)
        instructions = "\n\n".join(f"<!-- bundled resource: {resource} -->\n{content}" for resource, content in texts.items())
        assert binding["instruction_set_hash"] == canonical_hash({"skill_instructions": instructions, **bound_source_hashes})


@pytest.mark.parametrize("field,value", [
    ("model_ref", "unreviewed-model"),
    ("harness_adapter_ref", "unreviewed-harness"),
    ("schema_ref", "unreviewed-schema"),
    ("mcp_bindings", ("unreviewed-tool-catalog",)),
    ("capability_bindings", ("unreviewed-permissions",)),
    ("instruction_set_hash", "f" * 64),
    ("packaged_skill_bundle_hash", "f" * 64),
])
def test_exact_pair_rejects_other_execution_binding_fields(field: str, value: object) -> None:
    assert not bundle_bindings_compatible(_binding("before"), replace(_binding("after"), **{field: value}))


@pytest.mark.parametrize("prefix", [
    OWNER_RESOURCE,
    "package:meta_research.skills.bundle_stage/SKILL.md@sha256:",
    "adapter-source:meta_research.bundle_skill@sha256:",
    "adapter-source:meta_research.idea_skill@sha256:",
    "adapter-source:meta_research.provider_supervisor@sha256:",
    "adapter-source:meta_research.bundle_dispatch_recovery@sha256:",
    "output-schema:target-plan-envelope@sha256:",
    "transport-seal-key:sha256:",
    "harness-artifact:operation-binding-set:",
])
def test_exact_pair_preserves_code_tools_owner_protocol_and_transport(prefix: str) -> None:
    before, after = _binding("before"), _binding("after")
    assert not bundle_bindings_compatible(before, _replace_resource(after, prefix))
    assert not bundle_bindings_compatible(_replace_resource(before, prefix), after)


def test_same_guidance_change_on_another_deployment_is_not_reviewed() -> None:
    before, after = _binding("before"), _binding("after")
    prefix = "transport-seal-key:sha256:"
    assert not bundle_bindings_compatible(_replace_resource(before, prefix), _replace_resource(after, prefix))
