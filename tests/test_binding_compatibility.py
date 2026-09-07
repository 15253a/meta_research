from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.bundle_skill import BundleDispatchRequest, BundleSkillUnavailable, CodexBundleSkillAdapter
from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.common import canonical_hash
from meta_research.runtime_binding_compatibility import bundle_bindings_compatible
from test_bundle_skill_adapter import _FullConformanceAuthority, _SequenceRunner, _fake_codex, _inbox_checkpoint


def legacy(binding):
    replacements = {
        'adapter-source:meta_research.bundle_skill@sha256:': 'f383baed577536980e67174d63e27b2be92caefcb7502ad90f3e1c4b5e38e336',
        'adapter-source:meta_research.provider_supervisor@sha256:': '4a397daf6c33153a06e578a1f165c7269e6cbdd0c155caeb8605883e6eeb60c5',
    }
    replacements.update({
        'package:meta_research.skills.bundle_stage/SKILL.md@sha256:': 'e7d96919184e6b7eb8ecf39ef3701c32247a261b92c50ee84a6c0792c629550a',
        'package:meta_research.skills.bundle_stage/references/contract.md@sha256:': '95be373445b671cdf5a0e39eb7f57cc797e94aea06e7cd8919e066681f0eabd8',
    })
    resources = tuple(next((prefix+digest for prefix,digest in replacements.items() if value.startswith(prefix)),value) for value in binding.resource_bindings if not value.startswith(('bundle-execution-contract:', 'adapter-source:meta_research.bundle_dispatch_recovery@sha256:')))
    return replace(binding, packaged_skill_bundle_hash='4d2743769cf8549b776c1c2c119c3dfea78957095d6d7e016870cf2ad2df5f90', instruction_set_hash='267be1b0ce9594d094a5b88a7480b0111cbbbc0249d165909a4e2d6feb137853', resource_bindings=resources)


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
        stage_request_ref='stage-request:1', run_ref='bundle-run:1', attempt_ref='bundle-attempt:1', fence_ref='bundle-fence:1', graph_ref='target-graph:1', generation=1,
        frontier=({'target_ref':'target:followup','target_key':'followup'},),
        state={'schema_ref':'meta-research/bundle-dispatch-state/v1','target_commit_refs':[],'running_targets':[],'blocked_targets':[]},
        root_session_ref='ar-session:1', native_session_ref='codex-bundle-primary:1', runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(run_ref='bundle-run:1',attempt_ref='bundle-attempt:1',fence_ref='bundle-fence:1'),
    )


def test_compatibility_preserves_exact_equality_hashes_and_inputs(adapter):
    provider, _, _ = adapter
    current = provider.runtime_binding()
    old = legacy(current)
    old_value, current_value = old.as_dict(), current.as_dict()
    assert current != old
    assert canonical_hash(old_value) != canonical_hash(current_value)
    assert bundle_bindings_compatible(old,current)
    assert bundle_bindings_compatible(current,old)
    assert old.as_dict() == old_value
    assert current.as_dict() == current_value


def test_frozen_binding_executes_through_resident_dispatch(adapter):
    provider, runner, authority = adapter
    frozen = legacy(provider.runtime_binding())
    result = provider.schedule_target(request(frozen))
    assert result.selected_target_ref == 'target:followup'
    assert len(runner.calls) == 1
    assert authority.issued[0]['capability_binding_hash'] == canonical_hash(frozen.as_dict())
    assert len(authority.revoked) == 1


def test_stage_gate_accepts_reviewed_transport_and_rejects_model_drift(adapter):
    provider, _, _ = adapter
    frozen = legacy(provider.runtime_binding())
    worker = object.__new__(BundleStageWorker)
    worker._harnesses = object()
    worker._provider = provider
    worker._transient_error = None
    assert worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=frozen))
    assert not worker._runtime_binding_is_current(SimpleNamespace(runtime_binding=replace(frozen,model_ref='other-model')))
    assert worker.transient_error == 'bundle_runtime_binding_drift'


@pytest.mark.parametrize('field', ['model_ref','harness_adapter_ref','packaged_skill_bundle_hash','instruction_set_hash','schema_ref','capability_bindings','mcp_bindings','resource_bindings','unknown_supervisor','duplicate_supervisor'])
def test_unreviewed_drift_is_rejected_before_provider(adapter, field):
    provider, runner, authority = adapter
    current = provider.runtime_binding()
    old = legacy(current)
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
