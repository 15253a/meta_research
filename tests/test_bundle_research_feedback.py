from dataclasses import replace

import pytest

from meta_research.bundle_skill import BundleDispatchResult, BundleSkillContractError, validate_bundle_dispatch_result
from test_binding_compatibility import adapter, request


@pytest.mark.parametrize('action', ['wait', 'replan_required'])
def test_research_decision_can_defer_a_launchable_target(adapter, action):
    provider, _, _ = adapter
    req = request(provider.runtime_binding())
    result = BundleDispatchResult(action, None, 'Reassess conflicting evidence before expanding this experiment.', req.native_session_ref, 'codex_cli')
    assert validate_bundle_dispatch_result(req, result)


def test_dispatch_still_requires_current_launch_authority(adapter):
    provider, _, _ = adapter
    req = request(provider.runtime_binding())
    req = replace(req, frontier=({'target_ref': 'target:followup', 'dispatch_allowed': False},))
    result = BundleDispatchResult('dispatch', 'target:followup', 'Run it.', req.native_session_ref, 'codex_cli')
    with pytest.raises(BundleSkillContractError, match='target_not_in_frontier'):
        validate_bundle_dispatch_result(req, result)


def test_status_current_worker_failure_overrides_ordinary_acceptance_wait():
    from meta_research.runtime_status import project_worker_health
    status = {'state': 'waiting', 'waiting_reason': '等待系统接纳', 'foreground': {'stage': 'bundle'}, 'current_task': {'kind': 'stage'}}
    health = {'status': 'unavailable', 'checks': [{'name': 'bundle_stage_worker', 'status': 'unavailable', 'reason': {'code': 'codex_operation_identity_conflict'}}]}
    project_worker_health(status, health)
    assert status['state'] == 'failed'
    assert 'codex_operation_identity_conflict' in status['waiting_reason']
    assert status['health'] is health


def test_status_does_not_turn_unrelated_worker_failure_into_active_stage_failure():
    from meta_research.runtime_status import project_worker_health
    status = {'state': 'running', 'waiting_reason': None, 'foreground': {'stage': 'idea'}, 'current_task': {'kind': 'stage'}}
    project_worker_health(status, {'status': 'unavailable', 'checks': [{'name': 'writing_worker', 'status': 'unavailable'}]})
    assert status['state'] == 'running'


def test_completed_stage_does_not_hide_failure_advancing_active_cycle():
    from meta_research.runtime_status import project_worker_health
    status = {'state': 'completed', 'waiting_reason': None, 'foreground': {'stage': 'bundle', 'status': 'active'}, 'current_task': {'kind': 'stage'}}
    project_worker_health(status, {'status': 'unavailable', 'checks': [{'name': 'bundle_stage_worker', 'status': 'unavailable', 'reason': {'code': 'bundle_report_completion_invalid'}}]})
    assert status['state'] == 'failed'
    assert 'bundle_report_completion_invalid' in status['waiting_reason']
