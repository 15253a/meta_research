"""Exercise production Bundle binding construction at the Owner admission seam."""

from dataclasses import replace

import pytest

from meta_research.owners.agent_runtime import (
    IdeaRuntimeBinding,
    PlanRuntimeBinding,
    ReasoningRuntimeBinding,
    _validated_runtime_binding,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_binding_compatibility import adapter
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


POLICY_CONTRACT = "bundle-execution-contract:policy-refresh/v1"


@pytest.fixture
def pending_bundle(tmp_path):
    runtime = _bundle_runtime(tmp_path / "bundle-binding-admission")
    try:
        _prepare_bundle_request(runtime)
        request_value = runtime.bundle_stage.query_current()["stage_run_request"]
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            request_value["cycle_ref"]
        )
        assert request is not None
        assert runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref) is None
        yield runtime, request
    finally:
        runtime.close()


def test_production_bundle_binding_admits_and_persists_exact_identity(
    adapter, pending_bundle,
):
    provider, runner, _authority = adapter
    runtime, request = pending_bundle
    binding = provider.runtime_binding()
    assert POLICY_CONTRACT in binding.resource_bindings

    run = runtime.owners.agent_runtime.admit_bundle_stage(
        request, "production-bundle-binding-admit", runtime_binding=binding,
    )

    assert run.stage == "bundle"
    assert run.status == "running"
    assert run.runtime_binding == binding
    assert run.runtime_binding_hash == canonical_hash(binding.as_dict())
    persisted = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
    assert persisted is not None
    assert persisted.run_ref == run.run_ref
    assert persisted.runtime_binding == binding
    assert persisted.runtime_binding_hash == run.runtime_binding_hash
    assert runner.calls == []


@pytest.mark.parametrize("mutation", [
    "unknown_contract_version", "unapproved_capability", "missing_mcp_binding",
])
def test_bundle_contract_marker_does_not_bypass_other_admission_checks(
    adapter, pending_bundle, mutation,
):
    provider, runner, _authority = adapter
    runtime, request = pending_bundle
    binding = provider.runtime_binding()
    if mutation == "unknown_contract_version":
        binding = replace(binding, resource_bindings=tuple(
            "bundle-execution-contract:policy-refresh/v2"
            if resource == POLICY_CONTRACT else resource
            for resource in binding.resource_bindings
        ))
    elif mutation == "unapproved_capability":
        binding = replace(binding, capability_bindings=(
            *binding.capability_bindings, "owner-admin-unrestricted",
        ))
    else:
        assert len(binding.mcp_bindings) == 2
        binding = replace(binding, mcp_bindings=binding.mcp_bindings[:-1])

    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        runtime.owners.agent_runtime.admit_bundle_stage(
            request, f"invalid-bundle-binding-{mutation}", runtime_binding=binding,
        )

    assert runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref) is None
    assert runner.calls == []


@pytest.mark.parametrize("stage,binding_type", [
    ("idea", IdeaRuntimeBinding),
    ("plan", PlanRuntimeBinding),
    ("reasoning", ReasoningRuntimeBinding),
])
def test_bundle_policy_contract_is_not_authorized_for_other_stages(stage, binding_type):
    binding = binding_type(
        packaged_skill_bundle_hash="1" * 64,
        instruction_set_hash="2" * 64,
        model_ref="test-model-v1",
        harness_adapter_ref="test-deterministic-v1",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )
    # Establish that the only newly rejected field is the Bundle-only resource.
    _validated_runtime_binding(binding, stage=stage)

    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        _validated_runtime_binding(
            replace(binding, resource_bindings=(POLICY_CONTRACT,)), stage=stage,
        )
