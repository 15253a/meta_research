from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meta_research.bundle_protocol import TargetFrontierEntry, TargetWorkHandle
from meta_research.owners.common import OwnerConflict
from test_semantic_bundle_target_catalog import _call, _gateway, _issue, _launch_request


FRONTIER = 'agent_runtime.target_frontier.read'
LAUNCH = 'research_graph.target_launch_request.read'


@pytest.fixture
def bundle_read():
    gateway, graph, _memory, runtime = _gateway()
    request = _launch_request()
    handle = TargetWorkHandle(request.target_ref, 'target-run:1', 'target-session:1',
        'target-attempt:1', 'target-fence:1', 'input:1', request.target_spec_acceptance_receipt, (), (), True)
    entry = TargetFrontierEntry(request.target_ref, request.target_spec_binding,
        request.target_spec_acceptance_receipt, 2, 'terminal', handle, 'commit:1', True, True)
    runtime.frontier = entry
    graph.query_target_graph = Mock(return_value=SimpleNamespace(request_ref='stage-request:1',
        run_ref='bundle-run:1', targets=(SimpleNamespace(target_ref=request.target_ref),)))
    graph.query_target_launch_request = Mock(side_effect=OwnerConflict('target_launch_frontier_invalid'))
    graph.query_target_frontier_commit_transition = Mock(return_value=SimpleNamespace(
        target_ref=request.target_ref, target_run_ref=handle.target_run_ref,
        execution_attempt_ref=handle.execution_attempt_ref, execution_fence_ref=handle.execution_fence_ref,
        target_commit_ref=entry.terminal_fact_ref))
    connection = _issue(gateway, (FRONTIER, LAUNCH))

    def read(operation=FRONTIER):
        return _call(gateway, connection, operation, {'target_ref': request.target_ref})

    return read, graph, runtime


def test_terminal_frontier_reads_accepted_commit_without_relaunching(bundle_read):
    read, graph, _ = bundle_read
    result = read()
    assert not result['isError'] and result['structuredContent']['status'] == 'present'
    assert result['structuredContent']['entry']['terminal_fact_ref'] == 'commit:1'
    graph.query_target_launch_request.assert_not_called()


@pytest.mark.parametrize('mismatch', ['graph', 'target', 'commit', 'commit_target', 'run', 'attempt', 'fence'])
def test_terminal_frontier_rejects_foreign_bindings(bundle_read, mismatch):
    read, graph, _ = bundle_read
    if mismatch == 'graph':
        graph.query_target_graph.return_value.run_ref = 'another-run'
    elif mismatch == 'target':
        graph.query_target_graph.return_value.targets = (SimpleNamespace(target_ref='another-target'),)
    elif mismatch == 'commit':
        graph.query_target_frontier_commit_transition.return_value.target_commit_ref = 'another-commit'
    elif mismatch == 'commit_target':
        graph.query_target_frontier_commit_transition.return_value.target_ref = 'another-target'
    elif mismatch == 'run':
        graph.query_target_frontier_commit_transition.return_value.target_run_ref = 'another-run'
    elif mismatch == 'attempt':
        graph.query_target_frontier_commit_transition.return_value.execution_attempt_ref = 'another-attempt'
    else:
        graph.query_target_frontier_commit_transition.return_value.execution_fence_ref = 'another-fence'
    result = read()
    assert result['isError'] and result['structuredContent']['code'] in {'target_outside_bundle_graph', 'target_frontier_currentness_invalid'}


def test_stale_scope_cannot_read_terminal_record_or_launch_envelope(bundle_read):
    read, graph, runtime = bundle_read
    runtime.stale = True
    for op in (FRONTIER, LAUNCH):
        assert read(op)['structuredContent']['code'] == 'semantic_call_scope_stale'
    graph.query_target_frontier_commit_transition.assert_not_called()
    graph.query_target_launch_request.assert_not_called()


def test_known_target_without_projection_has_no_launch_envelope(bundle_read):
    read, graph, _ = bundle_read
    graph.query_target_launch_request.side_effect = OwnerConflict('target_candidate_projection_required')
    reply = read(LAUNCH)
    assert reply['isError'] and reply['structuredContent']['code'] == 'target_candidate_projection_required'
    graph.query_target_graph.return_value.targets = ()
    assert read(LAUNCH)['structuredContent']['code'] == 'target_outside_bundle_graph'


def test_running_frontier_keeps_current_launch_verification(bundle_read):
    read, graph, runtime = bundle_read
    runtime.frontier = replace(runtime.frontier, state='running', terminal_fact_ref=None)
    graph.query_target_launch_request.side_effect = None
    graph.query_target_launch_request.return_value = _launch_request()
    assert read()['structuredContent']['status'] == 'present'
    graph.query_target_launch_request.assert_called_once_with('target:1')
    graph.query_target_frontier_commit_transition.assert_not_called()


def test_absent_frontier_is_only_observed_within_current_graph(bundle_read):
    read, graph, runtime = bundle_read
    runtime.frontier = None
    assert read()['structuredContent'] == {'status': 'absent'}
    graph.query_target_graph.return_value.targets = ()
    assert read()['structuredContent']['code'] == 'target_outside_bundle_graph'


@pytest.mark.parametrize('code', ['target_launch_asset_proof_invalid', 'target_candidate_projection_integrity_invalid'])
def test_other_launch_failures_keep_the_owner_reason(bundle_read, code):
    read, graph, _ = bundle_read
    graph.query_target_launch_request.side_effect = OwnerConflict(code)
    result = read(LAUNCH)
    assert result['isError'] and result['structuredContent']['code'] == code
