"""The independent Target writer may commit after Bundle reads its commit list."""
from unittest.mock import Mock

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _root_finalizer_fixture, _EvidenceReader
from test_bundle_snapshot_frontier_reads import admitted_target


def test_dispatch_observes_existing_frontier_when_target_commits_after_list_read(tmp_path, monkeypatch):
    runtime, lifecycle, memory, authority, handle, _workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        request_ref = runtime.bundle_stage.query_current()['stage_run_request']['request_ref']
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert graph is not None and run is not None
        earlier_commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        assert earlier_commits == ()

        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == 'completed'
        latest_commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        assert any(commit.target_ref == handle.target_ref for commit in latest_commits)

        # This is a correct first-launch rejection, and must remain strict.
        with pytest.raises(OwnerConflict, match='target_launch_frontier_invalid'):
            runtime.owners.agent_runtime.query_admitted_target_launch(handle.target_ref)
        verified_frontier = runtime.owners.agent_runtime.query_target_frontier_entry(handle.target_ref)
        assert verified_frontier is not None
        assert verified_frontier.current_handle.target_run_ref == handle.target_run_ref

        owner = runtime.owners.agent_runtime
        launch = Mock(wraps=owner.query_admitted_target_launch)
        frontier = Mock(wraps=owner.query_target_frontier_entry)
        monkeypatch.setattr(owner, 'query_admitted_target_launch', launch)
        monkeypatch.setattr(owner, 'query_target_frontier_entry', frontier)
        state = runtime.bundle_stage._dispatch_state(graph, earlier_commits, run=run)
        assert state['running_targets'] == [{'target_ref':handle.target_ref, 'target_run_ref':handle.target_run_ref}]
        launch.assert_not_called()
        frontier.assert_called_once_with(handle.target_ref)

        # The next fresh read consumes the accepted result and clears in-flight work.
        current = runtime.bundle_stage._dispatch_state(graph, latest_commits, run=run)
        assert current['running_targets'] == []
        assert accepted.target_commit_ref in current['target_commit_refs']
    finally:
        runtime.close()


def _dispatch_inputs(runtime):
    request_ref = runtime.bundle_stage.query_current()['stage_run_request']['request_ref']
    graph = runtime.owners.research_graph.query_target_graph(request_ref)
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
    assert graph is not None and run is not None
    return graph, run


def test_dispatch_reads_verified_launch_when_frontier_is_absent(admitted_target, monkeypatch):
    runtime, target, _candidate, _formal_plan, handle, _launch = admitted_target
    graph, run = _dispatch_inputs(runtime)
    owner = runtime.owners.agent_runtime
    assert owner.query_target_frontier_entry(target.target_ref) is None
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(owner, 'query_admitted_target_launch', launch)
    state = runtime.bundle_stage._dispatch_state(graph, (), run=run)
    assert state['running_targets'] == [{'target_ref':target.target_ref, 'target_run_ref':handle.target_run_ref}]
    launch.assert_called_once_with(target.target_ref)


def test_dispatch_does_not_suppress_invalid_frontier(admitted_target, monkeypatch):
    runtime, _target, *_rest = admitted_target
    graph, run = _dispatch_inputs(runtime)
    owner = runtime.owners.agent_runtime
    monkeypatch.setattr(owner, 'query_target_frontier_entry', Mock(side_effect=OwnerConflict('target_frontier_integrity_invalid')))
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(owner, 'query_admitted_target_launch', launch)
    with pytest.raises(OwnerConflict, match='target_frontier_integrity_invalid'):
        runtime.bundle_stage._dispatch_state(graph, (), run=run)
    launch.assert_not_called()


def test_dispatch_does_not_suppress_invalid_launch_fallback(admitted_target, monkeypatch):
    runtime, target, *_rest = admitted_target
    graph, run = _dispatch_inputs(runtime)
    owner = runtime.owners.agent_runtime
    assert owner.query_target_frontier_entry(target.target_ref) is None
    monkeypatch.setattr(owner, 'query_admitted_target_launch', Mock(side_effect=OwnerConflict('target_launch_ack_invalid')))
    with pytest.raises(OwnerConflict, match='target_launch_ack_invalid'):
        runtime.bundle_stage._dispatch_state(graph, (), run=run)


def test_process_rechecks_existing_frontier_if_target_commits_after_rg_frontier_read(tmp_path, monkeypatch):
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        graph_owner = runtime.owners.research_graph
        query_frontier = graph_owner.query_target_frontier
        accepted = []

        def commit_after_frontier_read(graph_ref):
            observed = query_frontier(graph_ref)
            if not accepted:
                assert any(target.target_ref == handle.target_ref for target in observed)
                accepted.append(None)
                result = finalizer.finalize(handle=handle, evidence=evidence)
                assert result.status == 'completed'
                accepted[0] = result
            return observed

        monkeypatch.setattr(graph_owner, 'query_target_frontier', commit_after_frontier_read)
        owner = runtime.owners.agent_runtime
        launch = Mock(wraps=owner.query_admitted_target_launch)
        monkeypatch.setattr(owner, 'query_admitted_target_launch', launch)
        runtime.bundle_stage.process_once()
        assert accepted and accepted[0].target_commit_ref
        launch.assert_not_called()
    finally:
        runtime.close()
