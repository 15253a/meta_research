"""Preserve the actual accepted Chinese root synthesis at the report boundary."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from meta_research.bundle_protocol import BundleReport, BundleProtocolError, canonical_projection_bytes, validate_bundle_report, validate_closed_bundle_projection
from meta_research.owners.common import canonical_json
from test_bundle_batch_replan import _BatchReplan
from test_public_bundle_stage import _accept_real_target_root_commit
from test_target_root_finalizer import _current_bundle_runtime
from test_quest_research_time import _control


def _actual_basis():
    return json.loads((Path(__file__).parent/'fixtures/bundle_replan_basis_real_274.json').read_text(encoding='utf-8'))['basis']


class _ActualSynthesis(_BatchReplan):
    def propose_target_batch(self, request):
        result = super().propose_target_batch(request)
        self.basis = _actual_basis()
        # Bind the reproduced text to this test's actual Commit set. The
        # source production decision and text remain preserved in the fixture.
        self.basis['source_target_commit_refs'] = [item['commit_ref'] for item in request.target_commits]
        result.replan_basis = self.basis
        return result


def test_accepted_chinese_synthesis_survives_pause_restart_without_new_model(tmp_path):
    provider = _ActualSynthesis()
    runtime = _current_bundle_runtime(tmp_path/'real-synthesis', bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        owner = runtime.owners.agent_runtime
        request_ref = runtime.bundle_stage.query_current()['stage_run_request']['request_ref']
        run = owner.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        for _ in range(8):
            runtime.bundle_stage.process_once()
            decisions = owner.query_bundle_dispatch_decisions(run.run_ref)
            if decisions and decisions[-1].action == 'replan_required':
                break
        decision = decisions[-1]
        assert decision.state['batch_replan']['basis'] == provider.basis
        encoded = canonical_json(provider.basis)
        assert len(encoded.encode('utf-8')) > 4096
        assert owner.query_bundle_run_report(run.run_ref) is None
        _control(runtime, graph.quest_ref, 'pause', 'accepted-synthesis-maintenance-pause')
        root = runtime.data_root.root
        runtime.close()
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        owner = runtime.owners.agent_runtime
        _control(runtime, graph.quest_ref, 'resume', 'accepted-synthesis-maintenance-resume')
        for _ in range(16):
            runtime.bundle_stage.process_once()
            if runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref) is not None:
                break
        report = owner.query_bundle_run_report(run.run_ref)
        assert report is not None
        assert encoded in report.report.semantic_change_required
        assert report.report.remaining_experiment_keys
        assert report.report.accepted_target_commit_refs == tuple(provider.basis['source_target_commit_refs'])
        assert owner.query_bundle_dispatch_decisions(run.run_ref)[-1] == decision
        assert owner.query_bundle_stage_run(request_ref).attempt_ref == decision.attempt_ref
        assert len(provider.batches) == 1 and provider.coordination == []
        committed = runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref)
        assert committed is not None
        assert encoded in committed.closure['bundle_report']['semantic_change_required']
        # The next Stage reads the actual accepted closure, not an enum-only
        # substitute. Do not invoke a new model in this engineering regression.
        runtime.reasoning_stage.process_once()
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(graph.cycle_ref)
        assert request is not None
        assert _contains(request.context_pack, encoded)
    finally:
        runtime.close()


def _contains(value, expected):
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, expected) for item in value)
    return False


def _report(text):
    return BundleReport(disposition='replan_required', stage_request_ref='request', formal_plan_ref='plan',
        accepted_target_commit_refs=tuple(_actual_basis()['source_target_commit_refs']), accepted_evaluation_attempt_refs=(), metric_result_refs=(),
        execution_attempt_refs=(), execution_fence_refs=(), checkpoint_artifact_refs=(),
        realized_experiment_keys=(), remaining_experiment_keys=('unfinished',), semantic_change_required=(text,))


def test_long_synthesis_is_exact_and_still_obeys_whole_report_capacity():
    value = canonical_json(_actual_basis())
    report = _report(value)
    assert len(validate_bundle_report(report)) == 64
    assert report.semantic_change_required == (value,)
    with pytest.raises(BundleProtocolError, match='budget'):
        validate_closed_bundle_projection(report, max_serialized_bytes=len(canonical_projection_bytes(report))-1)


@pytest.mark.parametrize('damage', ['unstructured-text', 'extra-field', 'duplicate-key', 'foreign-commit', 'noncanonical', 'reference-field'])
def test_long_synthesis_does_not_weaken_other_shapes_or_references(damage):
    basis = _actual_basis()
    value = canonical_json(basis)
    if damage == 'unstructured-text':
        value = '无法核验的长文本' * 700
    elif damage == 'extra-field':
        value = canonical_json({**basis, 'unrecognized': True})
    elif damage == 'duplicate-key':
        value = value[:-1] + ',"next_focus":"duplicated"}'
    elif damage == 'foreign-commit':
        value = canonical_json({**basis, 'source_target_commit_refs': ['foreign-commit']})
    elif damage == 'noncanonical':
        value = json.dumps(basis, ensure_ascii=False, indent=2)
    report = _report(value)
    if damage == 'reference-field':
        report = replace(report, semantic_change_required=('short reason',), evidence_refs=(value,))
    with pytest.raises(BundleProtocolError, match='oversized text'):
        validate_bundle_report(report)
