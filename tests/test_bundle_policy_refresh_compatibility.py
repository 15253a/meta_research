from dataclasses import replace
from types import SimpleNamespace

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import BundleSkillUnavailable, BundleTargetBatchRequest, CodexBundleSkillAdapter
from meta_research.bundle_stage import BundleStageWorker
from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_binding_compatibility import adapter, request
from test_bundle_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex,
    _inbox_checkpoint, _plan_document, _target_plan,
)


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


@pytest.mark.parametrize("revision", ["future-release-a", "future-release-b"])
def test_policy_refresh_needs_no_registry_for_a_new_shared_execution_version(adapter, revision):
    provider, _, _ = adapter
    original = provider.runtime_binding()
    code_prefixes = (
        "adapter-source:meta_research.bundle_skill@sha256:",
        "adapter-source:meta_research.idea_skill@sha256:",
        "adapter-source:meta_research.provider_supervisor@sha256:",
        "adapter-source:meta_research.bundle_dispatch_recovery@sha256:",
    )
    frozen = replace(original, resource_bindings=tuple(
        next((prefix + canonical_hash([revision, prefix])
              for prefix in code_prefixes if entry.startswith(prefix)), entry)
        for entry in original.resource_bindings
    ))
    frozen_value = frozen.as_dict()
    for update in range(3):
        refreshed = replace(
            frozen,
            packaged_skill_bundle_hash=canonical_hash([revision, update, "package"]),
            instruction_set_hash=canonical_hash([revision, update, "instructions"]),
            resource_bindings=tuple(
                entry.split("@sha256:")[0] + "@sha256:" + canonical_hash([revision, update, entry])
                if entry.startswith((
                    "package:meta_research.skills.bundle_stage/SKILL.md@sha256:",
                    "package:meta_research.skills.bundle_stage/references/contract.md@sha256:",
                )) else entry
                for entry in frozen.resource_bindings
            ),
        )
        assert bundle_bindings_compatible(frozen, refreshed)
        assert bundle_bindings_compatible(refreshed, frozen)
        assert frozen.as_dict() == frozen_value
        # Sharing a newer executable is allowed; switching executable is not.
        assert not bundle_bindings_compatible(original, refreshed)


def test_completed_target_allows_next_batch_after_policy_refresh(tmp_path, monkeypatch):
    runner = _SequenceRunner([{
        "strategy_update": {
            "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
            "revision": 2, "candidates": [],
            "requires_accepted_labels": [], "strategy_complete": True,
        },
        "rationale": "The accepted TargetCommit closes the frozen measurement cell.",
    }])
    provider = CodexBundleSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    authority = _FullConformanceAuthority()
    provider.bind_full_conformance_authority(authority)
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    frozen = provider.runtime_binding()
    frozen_value = frozen.as_dict()
    plan = _plan_document()
    context_hash = canonical_hash({"context": "completed-target"})
    target_plan = _target_plan(plan, context_hash)
    target_plan["completion_contract"]["experiments"][0]["brief"][
        "required_measurement_unit_keys"
    ] = ["cell:structure-primary"]
    spec = target_plan["initial_strategy_update"]["candidates"][0]
    batch = BundleTargetBatchRequest(
        stage_request_ref="stage-request:1", run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        graph_ref="target-graph:1", formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1", context_pack_hash=context_hash,
        plan_document=plan, initial_target_plan=target_plan, base_generation=0,
        base_head_receipt={
            "receipt_ref": "target-graph-head-receipt:1",
            "receipt_kind": "target_graph_head_acceptance", "owner": "research_graph",
            "subject_ref": "target-graph:1", "payload_hash": "b" * 64, "bindings": {},
        },
        current_targets=({
            "target_ref": "target:accepted-structure", "target_key": "target:structure",
            "spec_hash": canonical_hash(spec), "spec": spec,
            "dependency_refs": [], "receipt": {},
        },),
        target_commits=({
            "commit_ref": "target-commit:1", "target_ref": "target:accepted-structure",
            "closure_hash": "c" * 64,
        },),
        root_session_ref="ar-session:1", native_session_ref="codex-bundle-primary:1",
        runtime_binding=frozen,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        ),
    )
    resources = bundle_skill._bundle_skill_resources()
    resources["SKILL.md"] += "\nUse accepted source artifacts in later batches.\n"
    resources["references/contract.md"] += "\nRetain source lineage on continuation.\n"
    monkeypatch.setattr(bundle_skill, "_bundle_skill_resources", lambda: dict(resources))
    worker = object.__new__(BundleStageWorker)
    worker._harnesses, worker._provider, worker._transient_error = object(), provider, None
    assert worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=frozen))
    result = provider.propose_target_batch(batch)
    assert result.strategy_update["strategy_complete"] is True
    assert len(runner.calls) == 1
    assert "target-commit:1" in runner.calls[0][1]
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(frozen_value)
    assert frozen.as_dict() == frozen_value


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
