from dataclasses import replace
from types import SimpleNamespace

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import BundleSkillUnavailable
from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_binding_compatibility import adapter, legacy, request


@pytest.mark.parametrize("name", ["SKILL.md", "references/contract.md"])
def test_later_policy_edits_resume_existing_run_without_hash_registration(adapter, monkeypatch, name):
    provider, runner, authority = adapter
    frozen = provider.runtime_binding()
    original = frozen.as_dict()
    resources = bundle_skill._bundle_skill_resources()
    resources[name] += "\nRevised scheduling guidance for the existing accepted plan.\n"
    monkeypatch.setattr(bundle_skill, "_bundle_skill_resources", lambda: dict(resources))
    current = provider.runtime_binding()
    assert frozen != current
    assert frozen.instruction_set_hash != current.instruction_set_hash
    assert frozen.packaged_skill_bundle_hash != current.packaged_skill_bundle_hash
    assert bundle_bindings_compatible(frozen, current)
    assert bundle_bindings_compatible(current, frozen)
    worker = object.__new__(BundleStageWorker)
    worker._harnesses = object()
    worker._provider = provider
    worker._transient_error = None
    assert worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=frozen))
    result = provider.schedule_target(request(frozen))
    assert result.selected_target_ref == "target:followup"
    assert len(runner.calls) == 1
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(original)
    assert frozen.as_dict() == original
    resources[name] += "\nAnother later policy clarification.\n"
    assert bundle_bindings_compatible(frozen, provider.runtime_binding())
    assert bundle_bindings_compatible(legacy(current), provider.runtime_binding())


@pytest.mark.parametrize("prefix", [
    "output-schema:target-dispatch@sha256:",
    "adapter-source:meta_research.bundle_skill@sha256:",
    "adapter-source:meta_research.idea_skill@sha256:",
    "adapter-source:meta_research.provider_supervisor@sha256:",
    "adapter-source:meta_research.bundle_dispatch_recovery@sha256:",
    "package:meta_research.skills.bundle_stage/references/owner-operations.md@sha256:",
    "transport-seal-key:sha256:",
    "harness-artifact:operation-binding-set:",
])
def test_policy_refresh_does_not_accept_code_tools_or_protocol_drift(adapter, prefix):
    provider, runner, authority = adapter
    frozen = provider.runtime_binding()
    assert any(entry.startswith(prefix) for entry in frozen.resource_bindings)
    resources = tuple(prefix + "f" * 64 if entry.startswith(prefix) else entry for entry in frozen.resource_bindings)
    drift = replace(frozen, resource_bindings=resources, instruction_set_hash="f" * 64)
    assert not bundle_bindings_compatible(drift, frozen)
    with pytest.raises(BundleSkillUnavailable, match="bundle_runtime_binding_drift"):
        provider.schedule_target(request(drift))
    assert runner.calls == []
    assert authority.issued == []


@pytest.mark.parametrize("field,value", [
    ("model_ref", "changed-model"),
    ("harness_adapter_ref", "changed-harness"),
    ("schema_ref", "changed-schema"),
    ("capability_bindings", ("changed-authorization",)),
    ("mcp_bindings", ("changed-tool-schema",)),
])
def test_policy_refresh_preserves_model_and_authorization_boundaries(adapter, field, value):
    provider, _, _ = adapter
    frozen = provider.runtime_binding()
    assert not bundle_bindings_compatible(frozen, replace(frozen, **{field: value}))


@pytest.mark.parametrize("mutation", ["missing_contract", "unknown_contract", "duplicate_contract", "missing_recovery", "duplicate_recovery", "malformed_policy_hash"])
def test_policy_contract_requires_complete_unambiguous_metadata(adapter, mutation):
    provider, _, _ = adapter
    frozen = provider.runtime_binding()
    resources = frozen.resource_bindings
    marker = next(entry for entry in resources if entry.startswith("bundle-execution-contract:"))
    recovery = next(entry for entry in resources if entry.startswith("adapter-source:meta_research.bundle_dispatch_recovery@sha256:"))
    if mutation == "missing_contract":
        resources = tuple(entry for entry in resources if entry != marker)
    elif mutation == "unknown_contract":
        resources = tuple("bundle-execution-contract:policy-refresh/v2" if entry == marker else entry for entry in resources)
    elif mutation == "duplicate_contract":
        resources += (marker,)
    elif mutation == "missing_recovery":
        resources = tuple(entry for entry in resources if entry != recovery)
    elif mutation == "duplicate_recovery":
        resources += (recovery,)
    else:
        resources = tuple("package:meta_research.skills.bundle_stage/SKILL.md@sha256:invalid" if entry.startswith("package:meta_research.skills.bundle_stage/SKILL.md@sha256:") else entry for entry in resources)
    assert not bundle_bindings_compatible(frozen, replace(frozen, resource_bindings=resources))
