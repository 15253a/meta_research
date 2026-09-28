"""Report preparation shares a read cut without retaining it across acceptance."""
from contextlib import contextmanager
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners import agent_runtime as report_owner
from test_bundle_report_receipt_order import committed_targets


def test_report_preparation_is_pure_and_acceptance_keeps_fresh_write_guard(
    committed_targets, monkeypatch,
):
    runtime, graph, run, _closures, source, projection = committed_targets
    for _step in range(8):
        if graph.strategy_complete:
            break
        runtime.bundle_stage.process_once()
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
    assert graph is not None and graph.strategy_complete
    database = runtime._database
    owner = runtime.owners.agent_runtime
    graph_owner = owner._bundle_report_evidence_verifier
    values = dict(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        formal_plan_content_receipt=source.receipt,
        formal_plan_projection_receipt=projection.receipt,
        target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt,
    )
    # The unchanged body is the previous implementation. Compare the complete
    # public payload on the same real Owner state, not a hand-written subset.
    original_prepare = getattr(
        report_owner, '_prepare_bundle_report_material_in_snapshot',
        report_owner._prepare_bundle_report_material,
    )
    with monkeypatch.context() as original:
        original.setattr(report_owner, '_prepare_bundle_report_material', original_prepare)
        expected = owner.build_bundle_report_candidate(disposition='realized', **values)
    original_contract = graph_owner.query_bundle_report_contract
    cuts = []

    def contract_in_snapshot(**kwargs):
        cut = database._read_cut.get()
        assert cut is not None
        cuts.append(cut)
        return original_contract(**kwargs)

    monkeypatch.setattr(graph_owner, 'query_bundle_report_contract', contract_in_snapshot)
    original_write = database.write
    writes = []

    @contextmanager
    def write_after_read_cut():
        assert database._read_cut.get() is None
        assert database._read_cache.get() is None
        writes.append(True)
        with original_write() as connection:
            yield connection

    monkeypatch.setattr(database, 'write', write_after_read_cut)
    candidate = owner.build_bundle_report_candidate(disposition='realized', **values)
    assert candidate == expected
    assert not writes and cuts
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # A caller's outer snapshot is retained; the helper must not replace/clear it.
    with database.read_snapshot() as outer:
        assert owner.build_bundle_report_candidate(disposition='realized', **values) == candidate
        assert database._read_cut.get() is outer
        assert cuts[-1] is outer
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # Formal receipt/currentness validation remains active on the next read cut.
    bad_values = dict(values, target_graph_receipt=replace(graph.head_receipt, payload_hash='0' * 64))
    with pytest.raises(OwnerConflict):
        owner.build_bundle_report_candidate(disposition='realized', **bad_values)
    assert not writes
    assert database._read_cut.get() is None and database._read_cache.get() is None

    accepted = owner.accept_bundle_report(
        report=candidate, idempotency_key='material-cut-acceptance', **values,
    )
    assert accepted.report == candidate
    assert len(accepted.target_commit_receipts) == len(graph.targets)
    assert writes
    assert database._read_cut.get() is None and database._read_cache.get() is None
