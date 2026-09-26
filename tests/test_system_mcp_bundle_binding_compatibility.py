from dataclasses import replace

import pytest

from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillUnavailable,
    CodexBundleSkillAdapter,
)
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_bundle_skill_adapter import (
    _FullConformanceAuthority,
    _SequenceRunner,
    _fake_codex,
    _inbox_checkpoint,
)


@pytest.fixture
def upgraded_bundle(tmp_path, pre_system_mcp_binding, scoped_system_mcp):
    registry, assert_loaded = scoped_system_mcp("bundle")
    runner = _SequenceRunner([
        {"action": "dispatch", "selected_target_ref": "target:followup", "rationale": "Continue."}
    ])
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
        system_mcp_registry=registry,
    )
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    current = adapter.runtime_binding()
    baseline = pre_system_mcp_binding(current)
    policy = "package:meta_research.skills.bundle_stage/SKILL.md@sha256:"
    historical = replace(
        baseline,
        instruction_set_hash="a" * 64,
        packaged_skill_bundle_hash="b" * 64,
        resource_bindings=tuple(
            policy + "c" * 64 if entry.startswith(policy) else entry
            for entry in baseline.resource_bindings
        ),
    )
    return adapter, runner, authority, assert_loaded, current, baseline, historical


def _request(binding):
    return BundleDispatchRequest(
        stage_request_ref="stage-request:1",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1",
        graph_ref="target-graph:1",
        generation=1,
        frontier=({"target_ref": "target:followup", "target_key": "followup"},),
        state={
            "schema_ref": "meta-research/bundle-dispatch-state/v1",
            "target_commit_refs": [], "running_targets": [], "blocked_targets": [],
        },
        root_session_ref="ar-session:1",
        native_session_ref="codex-bundle-primary:1",
        runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        ),
    )


def test_system_mcp_upgrade_preserves_prior_bundle_policy_execution(upgraded_bundle):
    adapter, runner, authority, assert_loaded, current, baseline, historical = upgraded_bundle
    original = historical.as_dict()
    assert bundle_bindings_compatible(historical, baseline)

    result = adapter.schedule_target(_request(historical))

    assert result.selected_target_ref == "target:followup"
    assert bundle_bindings_compatible(historical, current)
    assert bundle_bindings_compatible(current, historical)
    assert historical.as_dict() == original
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(original)
    assert len(runner.calls) == 1
    assert_loaded(runner.calls[0][0])


@pytest.mark.parametrize("side", ["historical", "current"])
@pytest.mark.parametrize("change", ["capability", "source"])
def test_system_mcp_bundle_policy_bridge_rejects_unreviewed_execution_changes(
    upgraded_bundle, side, change,
):
    adapter, runner, authority, _, current, _, historical = upgraded_bundle
    subject, other = (historical, current) if side == "historical" else (current, historical)
    if change == "capability":
        drift = replace(subject, capability_bindings=subject.capability_bindings + ("unreviewed",))
    else:
        source = "adapter-source:meta_research.bundle_skill@sha256:"
        drift = replace(subject, resource_bindings=tuple(
            source + "f" * 64 if entry.startswith(source) else entry
            for entry in subject.resource_bindings
        ))

    assert not bundle_bindings_compatible(drift, other)
    assert not bundle_bindings_compatible(other, drift)
    if side == "historical":
        with pytest.raises(BundleSkillUnavailable, match="bundle_runtime_binding_drift"):
            adapter.schedule_target(_request(drift))
    assert runner.calls == []
    assert authority.issued == []
