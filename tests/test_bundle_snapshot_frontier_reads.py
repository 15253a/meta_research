"""Exercise actual Owner admission/frontier reads behind Bundle presentation."""
from unittest.mock import Mock

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import TargetCommit
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)


@pytest.fixture
def admitted_target(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "snapshot-frontier")
    try:
        target, candidate, formal_plan, _, handle = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        yield runtime, target, candidate, formal_plan, handle, launch
    finally:
        runtime.close()


def _activate(fixture):
    runtime, target, candidate, formal_plan, handle, launch = fixture
    runtime.target_root_lifecycle.activate(
        launch_ref=launch.launch_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        idempotency_key="activate-snapshot-frontier",
    )
    assert runtime.owners.agent_runtime.query_target_frontier_entry(target.target_ref) is not None


def _row(projection, target_ref):
    return next(row for row in projection["target_graph"]["targets"] if row["target_ref"] == target_ref)


def test_verified_frontier_is_read_once_without_unused_launch_revalidation(admitted_target, monkeypatch):
    _activate(admitted_target)
    runtime, target, _, _, handle, _ = admitted_target
    owner = runtime.owners.agent_runtime
    frontier = Mock(wraps=owner.query_target_frontier_entry)
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(owner, "query_target_frontier_entry", frontier)
    monkeypatch.setattr(owner, "query_admitted_target_launch", launch)
    projection = runtime.bundle_stage.query_current()
    row = _row(projection, target.target_ref)
    assert row["status"] == "running"
    assert row["target_run_ref"] == handle.target_run_ref
    frontier.assert_called_once_with(target.target_ref)
    launch.assert_not_called()


def test_admitted_target_without_frontier_keeps_verified_launch_fallback(admitted_target, monkeypatch):
    runtime, target, _, _, handle, _ = admitted_target
    owner = runtime.owners.agent_runtime
    assert owner.query_target_frontier_entry(target.target_ref) is None
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(owner, "query_admitted_target_launch", launch)
    row = _row(runtime.bundle_stage.query_current(), target.target_ref)
    assert row["status"] == "running"
    assert row["target_run_ref"] == handle.target_run_ref
    launch.assert_called_once_with(target.target_ref)


def test_frontier_verification_failure_preserves_blocker_and_launch_fallback(admitted_target, monkeypatch):
    _activate(admitted_target)
    runtime, target, _, _, handle, _ = admitted_target
    owner = runtime.owners.agent_runtime
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(owner, "query_admitted_target_launch", launch)
    monkeypatch.setattr(owner, "query_target_frontier_entry", Mock(side_effect=OwnerConflict("target_frontier_integrity_invalid")))
    projection = runtime.bundle_stage.query_current()
    row = _row(projection, target.target_ref)
    assert row["status"] == "blocked"
    assert row["blocker"] == {"code": "target_frontier_integrity_invalid"}
    assert row["target_run_ref"] == handle.target_run_ref
    assert projection["target_graph"]["observation_errors"] == [{"target_ref": target.target_ref, "code": "target_frontier_integrity_invalid"}]
    launch.assert_called_once_with(target.target_ref)


def test_invalid_launch_fallback_still_raises_when_frontier_absent(admitted_target, monkeypatch):
    runtime, _, _, _, _, _ = admitted_target
    monkeypatch.setattr(runtime.owners.agent_runtime, "query_admitted_target_launch", Mock(side_effect=OwnerConflict("target_launch_ack_invalid")))
    with pytest.raises(OwnerConflict, match="target_launch_ack_invalid"):
        runtime.bundle_stage.query_current()


def test_committed_target_retains_accepted_run_without_active_execution_reads(admitted_target, monkeypatch):
    runtime, target, _, _, handle, _ = admitted_target
    owner = runtime.owners.agent_runtime
    # Supply the Owner's accepted-commit result at its existing query boundary.
    # The real Bundle query must map its identity without reopening execution.
    closure = {"protocol": {}}
    commit = TargetCommit(
        commit_ref="target-commit:snapshot-completed",
        target_ref=target.target_ref,
        target_run_ref=handle.target_run_ref,
        evaluation_attempt_ref="evaluation-attempt:snapshot-completed",
        target_spec_hash=target.spec_hash,
        closure=closure,
        closure_hash=canonical_hash(closure),
        result_disposition="denied",
        receipt=target.receipt,
    )
    commits = Mock(return_value=(commit,))
    frontier = Mock(wraps=owner.query_target_frontier_entry)
    launch = Mock(wraps=owner.query_admitted_target_launch)
    monkeypatch.setattr(runtime.owners.research_graph, "query_target_commits", commits)
    monkeypatch.setattr(owner, "query_target_frontier_entry", frontier)
    monkeypatch.setattr(owner, "query_admitted_target_launch", launch)

    projection = runtime.bundle_stage.query_current()
    row = _row(projection, target.target_ref)
    assert row["status"] == "committed"
    assert row["target_run_ref"] == commit.target_run_ref
    assert row["blocker"] is None
    assert projection["target_graph"]["observation_errors"] == []
    assert projection["target_commits"][0]["target_run_ref"] == commit.target_run_ref
    commits.assert_called_once()
    frontier.assert_not_called()
    launch.assert_not_called()
