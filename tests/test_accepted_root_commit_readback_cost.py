"""Accepted root reads authenticate durable facts without re-admitting the graph."""
import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.fixture
def accepted_root(tmp_path):
    runtime, lifecycle, memory, authority, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        accepted = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        yield runtime, handle, accepted, authority
    finally:
        runtime.close()


def test_accepted_root_readback_does_not_repeat_domain_admission(accepted_root, monkeypatch):
    runtime, handle, accepted, _ = accepted_root
    graph = runtime.owners.research_graph
    expected = graph.query_target_frontier_commit_transition(handle.target_ref)

    def repeated_admission(**_values):
        pytest.fail("accepted upstream Commit re-entered full domain admission")

    monkeypatch.setattr(graph, "_target_root_domain_context", repeated_admission)
    # SQLite query_only enforces that native entity verification stays read-only.
    with runtime._database.read_snapshot():
        for _ in range(3):
            transition = graph.query_target_frontier_commit_transition(handle.target_ref)
            assert transition == expected
            assert transition.target_commit_ref == accepted.target_commit_ref
        assert graph.query_target_formal_results(handle.target_ref)


@pytest.mark.parametrize("table,column,value", [
    ("rg_target_root_measurements", "metrics_hash", "0" * 64),
    ("rg_target_root_measurements", "receipt_hash", "0" * 64),
    ("rg_target_root_measurements", "authority_hash", "0" * 64),
    ("rg_target_root_measurements", "accepted_measurement_hash", "0" * 64),
    ("rg_target_commits", "receipt_hash", "0" * 64),
    ("rm_target_root_completion_manifests", "receipt_hash", "0" * 64),
])
def test_accepted_root_readback_keeps_exact_integrity_guards(accepted_root, table, column, value):
    runtime, handle, _, _ = accepted_root
    with runtime._database.write() as connection:
        connection.execute(text(
            f"UPDATE {table} SET {column} = :value WHERE target_ref = :target_ref"
        ), {"value": value, "target_ref": handle.target_ref})
    with pytest.raises(OwnerConflict):
        runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
