"""Completion uses accepted frozen cells, independent of the launch frontier."""

import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_bundle_no_action_coordination import _NoActionBundle
from test_bundle_report_receipt_order import committed_targets
from test_public_bundle_stage import _accept_real_target_root_commit
from test_target_root_finalizer import _current_bundle_runtime, _root_finalizer_fixture, _EvidenceReader


def test_remaining_work_includes_unproposed_frozen_cells_with_no_frontier(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "unproposed", bundle_skill=_NoActionBundle("wait"))
    try:
        _accept_real_target_root_commit(runtime)
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        owner = runtime.owners.research_graph
        graph = owner.query_target_graph(request_ref)
        assert owner.query_target_frontier(graph.graph_ref) == ()
        assert len(owner.query_target_commits(graph.graph_ref)) == len(graph.targets) == 1
        projection = owner.query_target_formal_plan_projection(graph_ref=graph.graph_ref)
        assert owner.query_bundle_remaining_work(graph.graph_ref) == tuple(sorted(
            brief.experiment_key for brief in projection.formal_plan.briefs))
    finally:
        runtime.close()


def test_remaining_work_is_empty_before_root_seals_fully_covered_strategy(committed_targets):
    runtime, graph, _run, _closures, _source, _projection = committed_targets
    assert not graph.strategy_complete
    assert runtime.owners.research_graph.query_bundle_remaining_work(graph.graph_ref) == ()


@pytest.mark.parametrize("disposition", ["negative", None])
def test_accepted_work_counts_without_a_positive_scientific_conclusion(tmp_path, disposition):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        result_path = workspace / "outputs/result.json"
        document = json.loads(result_path.read_text(encoding="utf-8"))
        if disposition is None:
            document.pop("result_disposition")
        else:
            document["result_disposition"] = disposition
        result_path.write_text(canonical_json(document), encoding="utf-8")
        finalized = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)
        assert finalized.status == "completed"
        # Coverage belongs to the accepted result, including the interval
        # before the Target daemon publishes its terminal handoff to Bundle.
        owner = runtime.owners.research_graph
        assert owner.query_target_commits(authority.graph_ref)[0].result_disposition == disposition
        assert owner.query_bundle_remaining_work(authority.graph_ref) == ()
    finally:
        runtime.close()


def test_remaining_work_rejects_altered_accepted_commit(committed_targets):
    runtime, graph, _run, _closures, _source, _projection = committed_targets
    owner = runtime.owners.research_graph
    commit = owner.query_target_commits(graph.graph_ref)[0]
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE rg_target_commits SET receipt_hash=:hash WHERE commit_ref=:ref"),
                           {"hash": "f" * 64, "ref": commit.commit_ref})
    with pytest.raises(OwnerConflict):
        owner.query_bundle_remaining_work(graph.graph_ref)
