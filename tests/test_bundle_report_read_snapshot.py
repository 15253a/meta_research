"""Reuse existing projection proofs only inside a report's immutable read cut."""
from time import perf_counter
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets


def _accept_report(fixture):
    runtime, graph, run, _closures, source, projection = fixture
    for _step in range(8):
        if graph.strategy_complete:
            break
        runtime.bundle_stage.process_once()
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph is not None
    assert graph.strategy_complete
    values = dict(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        formal_plan_content_receipt=source.receipt, formal_plan_projection_receipt=projection.receipt,
        target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt)
    owner = runtime.owners.agent_runtime
    candidate = owner.build_bundle_report_candidate(disposition='realized', **values)
    accepted = owner.accept_bundle_report(report=candidate, idempotency_key='snapshot-report', **values)
    return runtime, graph, run, accepted


def test_report_queries_scope_existing_projection_cache_and_revalidate_after_exit(committed_targets, monkeypatch):
    runtime, graph, run, accepted = _accept_report(committed_targets)
    database = runtime._database
    authority = runtime.target_run_authorities.research_graph
    plan = Mock(wraps=authority._current_formal_plan_facts)
    candidate = Mock(wraps=authority._current_candidate_projection_facts)
    monkeypatch.setattr(authority, '_current_formal_plan_facts', plan)
    monkeypatch.setattr(authority, '_current_candidate_projection_facts', candidate)
    owner = runtime.owners.agent_runtime
    write = Mock(wraps=database.write)
    monkeypatch.setattr(database, 'write', write)

    assert database._read_cut.get() is None and database._read_cache.get() is None
    started = perf_counter()
    observed = owner.query_bundle_run_report(run.run_ref)
    elapsed = perf_counter() - started
    print(f'REPORT_READ seconds={elapsed:.6f} plan_facts={plan.call_count} candidate_facts={candidate.call_count}', flush=True)
    assert observed == accepted
    # No worker outer snapshot is required; nested report queries supply it.
    assert plan.call_count == 1
    assert candidate.call_count == len(graph.targets)
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None

    plan.reset_mock()
    candidate.reset_mock()
    assert owner.verify_bundle_report_receipt(report_ref=accepted.report_ref, receipt=accepted.receipt) == accepted
    assert plan.call_count == 1 and candidate.call_count == len(graph.targets)

    plan.reset_mock()
    candidate.reset_mock()
    with database.read_snapshot() as outer_connection:
        outer_cache = database._read_cache.get()
        assert owner.query_bundle_run_report(run.run_ref) == accepted
        assert owner.verify_bundle_report_receipt(report_ref=accepted.report_ref, receipt=accepted.receipt) == accepted
        assert database._read_cut.get() is outer_connection
        assert database._read_cache.get() is outer_cache
        assert plan.call_count == 1 and candidate.call_count == len(graph.targets)
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # Writes after the pure read return are not accidentally enclosed by its cut.
    target_ref = graph.targets[0].target_ref
    with database.write() as connection:
        original = connection.execute(text('SELECT source_spec_hash FROM rg_target_candidate_projections WHERE target_ref=:target'), {'target': target_ref}).scalar_one()
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': '0' * 64, 'target': target_ref})
    with pytest.raises(OwnerConflict):
        owner.query_bundle_run_report(run.run_ref)
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': original, 'target': target_ref})
    plan.reset_mock()
    candidate.reset_mock()
    assert owner.query_bundle_run_report(run.run_ref) == accepted
    assert plan.call_count == 1 and candidate.call_count == len(graph.targets)

    # A new cut rechecks the report's Run/Attempt/Fence binding as well.
    with database.write() as connection:
        original_fence = connection.execute(text('SELECT fence_ref FROM ar_bundle_reports WHERE report_ref=:report'), {'report': accepted.report_ref}).scalar_one()
        other_fence = connection.execute(text('SELECT fence_ref FROM ar_execution_fences WHERE fence_ref<>:fence LIMIT 1'), {'fence': original_fence}).scalar_one()
        connection.execute(text('UPDATE ar_bundle_reports SET fence_ref=:fence WHERE report_ref=:report'), {'fence': other_fence, 'report': accepted.report_ref})
    with pytest.raises(OwnerConflict):
        owner.verify_bundle_report_receipt(report_ref=accepted.report_ref, receipt=accepted.receipt)
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE ar_bundle_reports SET fence_ref=:fence WHERE report_ref=:report'), {'fence': original_fence, 'report': accepted.report_ref})
    assert owner.query_bundle_run_report(run.run_ref) == accepted
