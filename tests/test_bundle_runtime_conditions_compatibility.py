"""An admitted Bundle continues after the reviewed runtime-conditions update."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research.bundle_skill import BundleSkillUnavailable, BundleTargetBatchRequest, CodexBundleSkillAdapter
from meta_research.bundle_stage import BundleStageWorker
from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import read_supervisor_request, read_transport_key_for_operation, write_exit_receipt
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_binding_compatibility import adapter, request
from test_bundle_skill_adapter import _FullConformanceAuthority, _SequenceRunner, _fake_codex, _inbox_checkpoint, _plan_document, _target_plan


# Captured from the admitted 8768 Bundle and the installed 09e0ac7 adapter.
BEFORE_INSTRUCTIONS = "67c1c3d4005ab5a73e8c0eaa7a45c9eee10bdacb8b0481dd819af6a50a4a02b4"
AFTER_INSTRUCTIONS = "9762d6d048f08186f950a010bd489f6a1a113b2511821698409629da3d391a96"
BEFORE_SOURCES = {
    "adapter-source:meta_research.bundle_skill@sha256:": "ffea3bacb7082e8cfbf7b3948780d6ded827c05811cdfab45245b3ea5ae820ec",
    "adapter-source:meta_research.idea_skill@sha256:": "3c3b6e2cfd827981ec5cbd79c166b00f005849a9c9880d05d19cd9954255d5ad",
}
AFTER_SOURCES = {
    "adapter-source:meta_research.bundle_skill@sha256:": "a68b18ac34873d2cb3357938bbdf23a5b9d7c3e54926b6806b396cff2ce0246e",
    "adapter-source:meta_research.idea_skill@sha256:": "653e4374f2440a6b93ad40b520db472dd9c8b5b6dde80a707a59c380ff3e0a25",
}
# These three resources are byte-identical in 09e0ac7 and bff2e08. Pin the
# reviewed historical fixture rather than mixing today's six-entry guidance
# into that earlier instruction/source profile. Test harness identities and
# the temporary transport key remain local to each test.
REVIEWED_SKILL_HASH = "300a27a4242d126de4f42417c34bbd5e3aa78a0cf6f4f3fed0c7260e5cb97d6f"
REVIEWED_SKILL_RESOURCES = {
    "package:meta_research.skills.bundle_stage/SKILL.md@sha256:": "2b0f236d83da70c683866e14c069844692e44fb243b9aee205ae30bc074f5717",
    "package:meta_research.skills.bundle_stage/references/contract.md@sha256:": "1e268379c6b889413c451ae0831eb96bbe09721d87f194820f8df3285fadd050",
    "package:meta_research.skills.bundle_stage/references/owner-operations.md@sha256:": "e4b2778f0847ea49e7aba4b6f03d44f2947cc725959bca8e11ecd195a5a55fa2",
}


def reviewed_after_conditions(current):
    replacements = {**AFTER_SOURCES, **REVIEWED_SKILL_RESOURCES}
    return replace(current, instruction_set_hash=AFTER_INSTRUCTIONS,
        packaged_skill_bundle_hash=REVIEWED_SKILL_HASH, resource_bindings=tuple(
            next((prefix + digest for prefix, digest in replacements.items() if value.startswith(prefix)), value)
            for value in current.resource_bindings))


def use_reviewed_conditions_adapter(provider, monkeypatch):
    binding = reviewed_after_conditions(provider._base_runtime_binding())
    monkeypatch.setattr(provider, "_base_runtime_binding", lambda: binding)
    return provider.runtime_binding()


def frozen_before_conditions(current):
    return replace(current, instruction_set_hash=BEFORE_INSTRUCTIONS, resource_bindings=tuple(
        next((prefix + digest for prefix, digest in BEFORE_SOURCES.items() if value.startswith(prefix)), value)
        for value in current.resource_bindings
    ))


def test_three_committed_targets_allow_next_dispatch_without_rebinding(adapter, monkeypatch):
    provider, runner, authority = adapter
    current = use_reviewed_conditions_adapter(provider, monkeypatch)
    assert current.instruction_set_hash == AFTER_INSTRUCTIONS
    frozen = frozen_before_conditions(current)
    frozen_value = frozen.as_dict()
    dispatch = request(frozen)
    dispatch = replace(dispatch, state={**dispatch.state, "target_commit_refs": [
        "target-commit:one", "target-commit:two", "target-commit:three",
    ]})
    worker = object.__new__(BundleStageWorker)
    worker._harnesses, worker._provider, worker._transient_error = object(), provider, None
    assert worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=frozen))
    assert bundle_bindings_compatible(current, frozen)

    result = provider.schedule_target(dispatch)

    assert result.selected_target_ref == "target:followup"
    assert len(runner.calls) == 1
    assert all(ref in runner.calls[0][1] for ref in dispatch.state["target_commit_refs"])
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(frozen_value)
    assert frozen.as_dict() == frozen_value
    assert frozen != current


def test_three_committed_targets_allow_next_batch_without_rebinding(tmp_path, monkeypatch):
    runner = _SequenceRunner([{
        "strategy_update": {
            "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
            "revision": 2, "candidates": [],
            "requires_accepted_labels": [], "strategy_complete": True,
        },
        "rationale": "The three accepted TargetCommits complete the frozen plan.",
    }])
    provider = CodexBundleSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    authority = _FullConformanceAuthority()
    provider.bind_full_conformance_authority(authority)
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    frozen = frozen_before_conditions(use_reviewed_conditions_adapter(provider, monkeypatch))
    original = frozen.as_dict()
    plan = _plan_document()
    context_hash = canonical_hash({"context": "three-completed-targets"})
    target_plan = _target_plan(plan, context_hash)
    cells = [f"cell:structure-{index}" for index in range(3)]
    target_plan["completion_contract"]["experiments"][0]["brief"][
        "required_measurement_unit_keys"
    ] = cells
    template = target_plan["initial_strategy_update"]["candidates"][0]
    # This shared fixture predates the removal of TargetCandidate.reuse_trace.
    template["candidate"].pop("reuse_trace")
    specs = []
    for index, cell in enumerate(cells):
        spec = deepcopy(template)
        spec["candidate"]["local_label"] = f"target:structure-{index}"
        spec["candidate"]["measurement_unit_keys"] = [cell]
        spec["measurement_contract"]["measurement_unit_key"] = cell
        specs.append(spec)
    target_plan["initial_strategy_update"]["candidates"] = specs
    commits = tuple({
        "commit_ref": f"target-commit:{index}",
        "target_ref": f"target:accepted-{index}",
        "closure_hash": canonical_hash({"closure": index}),
    } for index in range(3))
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
        current_targets=tuple({
            "target_ref": commit["target_ref"], "target_key": f"target:structure-{index}",
            "spec_hash": canonical_hash(specs[index]), "spec": specs[index],
            "dependency_refs": [], "receipt": {},
        } for index, commit in enumerate(commits)),
        target_commits=commits,
        root_session_ref="ar-session:1", native_session_ref="codex-bundle-primary:1",
        runtime_binding=frozen,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        ),
    )

    result = provider.propose_target_batch(batch)

    assert result.strategy_update["strategy_complete"] is True
    assert len(runner.calls) == 1
    assert all(commit["commit_ref"] in runner.calls[0][1] for commit in commits)
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(original)
    assert frozen.as_dict() == original


@pytest.mark.parametrize("prefix", [
    *BEFORE_SOURCES,
    "adapter-source:meta_research.provider_supervisor@sha256:",
    "adapter-source:meta_research.bundle_dispatch_recovery@sha256:",
    "output-schema:target-dispatch@sha256:",
    "transport-seal-key:sha256:",
    "harness-artifact:operation-binding-set:",
    "package:meta_research.skills.bundle_stage/references/owner-operations.md@sha256:",
    "package:meta_research.skills.bundle_stage/SKILL.md@sha256:",
])
def test_conditions_migration_rejects_unknown_source_and_other_resource_drift(adapter, prefix, monkeypatch):
    provider, runner, authority = adapter
    current = use_reviewed_conditions_adapter(provider, monkeypatch)
    frozen = frozen_before_conditions(current)
    assert any(entry.startswith(prefix) for entry in frozen.resource_bindings)
    drift = replace(frozen, resource_bindings=tuple(
        prefix + "f" * 64 if entry.startswith(prefix) else entry
        for entry in frozen.resource_bindings
    ))
    assert not bundle_bindings_compatible(drift, current)
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
    ("instruction_set_hash", AFTER_INSTRUCTIONS),
    ("packaged_skill_bundle_hash", "f" * 64),
])
def test_conditions_migration_requires_exact_instruction_source_combo_and_identity(adapter, field, value, monkeypatch):
    provider, runner, authority = adapter
    current = use_reviewed_conditions_adapter(provider, monkeypatch)
    drift = replace(frozen_before_conditions(current), **{field: value})
    assert not bundle_bindings_compatible(drift, current)
    with pytest.raises(BundleSkillUnavailable, match="bundle_runtime_binding_drift"):
        provider.schedule_target(request(drift))
    assert runner.calls == []
    assert authority.issued == []


@pytest.mark.parametrize("mutation", ["duplicate_source", "missing_source", "duplicate_contract"])
def test_conditions_migration_rejects_ambiguous_metadata(adapter, mutation, monkeypatch):
    provider, _, _ = adapter
    current = use_reviewed_conditions_adapter(provider, monkeypatch)
    frozen = frozen_before_conditions(current)
    source = next(entry for entry in frozen.resource_bindings if entry.startswith(
        "adapter-source:meta_research.idea_skill@sha256:"
    ))
    resources = frozen.resource_bindings
    if mutation == "duplicate_source":
        resources += (source,)
    elif mutation == "missing_source":
        resources = tuple(entry for entry in resources if entry != source)
    else:
        resources += ("bundle-execution-contract:policy-refresh/v1",)
    assert not bundle_bindings_compatible(replace(frozen, resource_bindings=resources), current)


def test_current_six_entry_guidance_does_not_inherit_historical_conditions_compatibility(adapter):
    provider, runner, authority = adapter
    current = provider.runtime_binding()
    historical = reviewed_after_conditions(current)
    assert current.instruction_set_hash != historical.instruction_set_hash
    assert current.packaged_skill_bundle_hash != historical.packaged_skill_bundle_hash
    for frozen in (historical, frozen_before_conditions(historical)):
        assert not bundle_bindings_compatible(frozen, current)
        assert not bundle_bindings_compatible(current, frozen)
        with pytest.raises(BundleSkillUnavailable, match="bundle_runtime_binding_drift"):
            provider.schedule_target(request(frozen))
    assert runner.calls == []
    assert authority.issued == []


class _SignedDispatchRunner(_SequenceRunner):
    def run_durable_job(self, job_ref, argv, prompt, timeout, stdout_path,
                        pid_path, request_path, environment=None):
        result = self(argv, prompt, timeout, environment)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        _, key = read_transport_key_for_operation(request_path.parent)
        sealed = read_supervisor_request(request_path, key)
        write_exit_receipt(
            Path(sealed["receipt_path"]), key=key,
            invocation_hash=sealed["invocation_hash"],
            prompt_path=Path(sealed["prompt_path"]),
            schema_path=Path(sealed["schema_path"]), stdout_path=stdout_path,
            result_path=Path(sealed["result_path"]), returncode=0,
            input_bytes=len(prompt.encode("utf-8")),
        )
        return result


def test_compatible_binding_keeps_sealed_legacy_prompt_and_refreshes_only_new_call(tmp_path, monkeypatch):
    conditions = {"value": "", "reads": 0}
    def render(workspace, **scope):
        conditions["reads"] += 1
        return conditions["value"]
    monkeypatch.setattr("meta_research.idea_skill.render_runtime_conditions", render)
    output = {"action": "dispatch", "selected_target_ref": "target:followup", "rationale": "Continue."}
    runner = _SignedDispatchRunner([output, output])
    provider = CodexBundleSkillAdapter(
        tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner,
    )
    provider.bind_full_conformance_authority(_FullConformanceAuthority())
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    frozen = frozen_before_conditions(use_reviewed_conditions_adapter(provider, monkeypatch))
    dispatch = replace(request(frozen), job_ref="bundle-job:legacy")
    first = provider.schedule_target(dispatch)
    directory = tmp_path / "provider" / "provider-operations" / canonical_hash({"job_ref": dispatch.job_ref}) / "dispatch-1"
    sealed_before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    conditions["value"] = "Selected GPU: GPU-new; budget: 30d"

    assert provider.schedule_target(dispatch) == first
    assert len(runner.calls) == 1
    assert conditions["reads"] == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == sealed_before
    assert "GPU-new" not in runner.calls[0][1]

    provider.schedule_target(replace(dispatch, job_ref="bundle-job:next", generation=2))
    assert len(runner.calls) == 2
    assert "GPU-new" in runner.calls[1][1]
    assert runner.calls[1][0][-3:] == ["resume", "codex-bundle-primary:1", "-"]
    assert conditions["reads"] == 2
