from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.bundle_skill import BundleDispatchRequest, BundleSkillUnavailable, CodexBundleSkillAdapter
from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_bundle_skill_adapter import _FullConformanceAuthority, _SequenceRunner, _fake_codex, _inbox_checkpoint




@pytest.fixture
def adapter(tmp_path):
    runner = _SequenceRunner([{'action':'dispatch','selected_target_ref':'target:followup','rationale':'Continue the accepted frontier.'}])
    adapter = CodexBundleSkillAdapter(tmp_path/'provider', executable=str(_fake_codex(tmp_path/'codex')), process_runner=runner)
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint('http://127.0.0.1:8765')
    return adapter, runner, authority


def request(binding):
    return BundleDispatchRequest(
        cycle_ref="cycle:bundle-1",
        cycle_position={"cycle_ref": "cycle:bundle-1", "round": 1, "cycle_focus": "Review frozen inputs", "focus_source": {"request_ref": "fixture-start"}},
        stage_request_ref='stage-request:1', run_ref='bundle-run:1', attempt_ref='bundle-attempt:1', fence_ref='bundle-fence:1', graph_ref='target-graph:1', generation=1,
        frontier=({'target_ref':'target:followup','target_key':'followup'},),
        state={'schema_ref':'meta-research/bundle-dispatch-state/v1','target_commit_refs':[],'running_targets':[],'blocked_targets':[]},
        root_session_ref='ar-session:1', native_session_ref='codex-bundle-primary:1', runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(run_ref='bundle-run:1',attempt_ref='bundle-attempt:1',fence_ref='bundle-fence:1'),
    )








@pytest.mark.parametrize('field', ['model_ref','harness_adapter_ref','schema_ref','capability_bindings','mcp_bindings','resource_bindings','unknown_supervisor','duplicate_supervisor'])
def test_unreviewed_drift_is_rejected_before_provider(adapter, field):
    provider, runner, authority = adapter
    current = provider.runtime_binding()
    old = current
    if field == 'unknown_supervisor':
        value = tuple('adapter-source:meta_research.provider_supervisor@sha256:'+'f'*64 if item.startswith('adapter-source:meta_research.provider_supervisor@sha256:') else item for item in old.resource_bindings)
        drift = replace(old,resource_bindings=value)
    elif field == 'duplicate_supervisor':
        source = next(item for item in old.resource_bindings if item.startswith('adapter-source:meta_research.provider_supervisor@sha256:'))
        drift = replace(old,resource_bindings=old.resource_bindings+(source,))
    elif field.endswith('_bindings'):
        drift = replace(old,**{field:getattr(old,field)+('unexpected-binding',)})
    else:
        drift = replace(old,**{field:'f'*64 if field.endswith('_hash') else 'unexpected-value'})
    assert not bundle_bindings_compatible(drift,current)
    with pytest.raises(BundleSkillUnavailable,match='bundle_runtime_binding_drift'):
        provider.schedule_target(request(drift))
    assert runner.calls == []
    assert authority.issued == []


def test_current_binding_executes_and_preserves_exact_authorization(adapter):
    provider, runner, authority = adapter
    frozen = provider.runtime_binding()
    result = provider.schedule_target(request(frozen))
    assert result.selected_target_ref == "target:followup"
    assert len(runner.calls) == 1
    assert authority.issued[0]["capability_binding_hash"] == canonical_hash(frozen.as_dict())
    assert bundle_bindings_compatible(frozen, frozen)


def test_stage_reports_source_drift_without_changing_frozen_run(adapter):
    from meta_research.bundle_stage import BundleObservation
    provider, runner, authority = adapter
    current = provider.runtime_binding()
    old = replace(current, resource_bindings=current.resource_bindings + ("adapter-source:retired@sha256:" + "a" * 64,))
    value = old.as_dict()
    worker = object.__new__(BundleStageWorker)
    worker._harnesses, worker._provider = object(), provider
    worker._observation = BundleObservation("ready")
    assert not worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=old))
    assert worker.observation == BundleObservation("failure", "bundle_runtime_binding_drift")
    assert old.as_dict() == value
    assert runner.calls == [] and authority.issued == []
