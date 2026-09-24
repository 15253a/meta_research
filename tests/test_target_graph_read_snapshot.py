"""Cache only pure graph reconstruction; mutable Plan custody stays live."""
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners import research_graph
from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report


def test_graph_cut_reuses_pure_rows_but_keeps_all_plan_file_checks(committed_targets, monkeypatch):
    runtime, graph, _run, _accepted = _accept_report(committed_targets)
    owner, database = runtime.owners.research_graph, runtime._database
    build = Mock(wraps=research_graph._accepted_target_graph)
    monkeypatch.setattr(research_graph, '_accepted_target_graph', build)

    with database.read_snapshot():
        first = owner.query_target_graph(graph.request_ref)
        assert first == graph
        first.target_plan['mutation_probe'] = True
        first.targets[0].spec['mutation_probe'] = True
        assert owner.query_target_graph(graph.request_ref) == graph
        assert owner.query_target_graph('stage_request:other') is None
        assert owner.query_target_graph(graph.request_ref) == graph
        assert build.call_count == 1
    assert database._read_cut.get() is None and database._read_cache.get() is None

    build.reset_mock()
    assert owner.query_target_graph(graph.request_ref) == graph
    assert owner.query_target_graph(graph.request_ref) == graph
    assert build.call_count == 2

    # Existing invalid-input behavior is retained even inside the cache scope.
    with pytest.raises(Exception) as outside:
        owner.query_target_graph([])
    with database.read_snapshot():
        with pytest.raises(type(outside.value)):
            owner.query_target_graph([])

    # Each new read cut re-verifies SQL content and append receipts.
    with database.read() as connection:
        append_ref = connection.execute(text('SELECT append_ref FROM rg_target_graph_appends WHERE graph_ref=:ref LIMIT 1'), {'ref': graph.graph_ref}).scalar_one()
    cases = [
        ('rg_target_graphs', 'graph_ref', graph.graph_ref, 'target_plan_hash', '0' * 64),
        ('rg_targets', 'target_ref', graph.targets[0].target_ref, 'spec_hash', '0' * 64),
        ('rg_target_graph_appends', 'append_ref', append_ref, 'receipt_hash', '0' * 64),
        ('rm_plan_documents', 'content_ref', graph.plan_content_ref, 'plan_document_json', '{}'),
    ]
    for table, key, ref, column, damaged in cases:
        with database.write() as connection:
            saved = connection.execute(text(f'SELECT {column} FROM {table} WHERE {key}=:ref'), {'ref': ref}).scalar_one()
            connection.execute(text(f'UPDATE {table} SET {column}=:value WHERE {key}=:ref'), {'value': damaged, 'ref': ref})
        try:
            with database.read_snapshot():
                for _repeat in range(2):
                    with pytest.raises(OwnerConflict):
                        owner.query_target_graph(graph.request_ref)
        finally:
            with database.write() as connection:
                connection.execute(text(f'UPDATE {table} SET {column}=:value WHERE {key}=:ref'), {'value': saved, 'ref': ref})
        with database.read_snapshot():
            assert owner.query_target_graph(graph.request_ref) == graph

    # A WAL snapshot cannot freeze the object store. Even after graph is cached,
    # authority must reread and hash its accepted Plan object on every call.
    with database.read() as connection:
        relative = connection.execute(text('SELECT object_path FROM rm_plan_documents WHERE content_ref=:ref'), {'ref': graph.plan_content_ref}).scalar_one()
    plan_file = runtime.owners.research_memory._object_store / relative
    original = plan_file.read_bytes()
    with database.read_snapshot():
        assert owner.query_target_graph(graph.request_ref) == graph
        assert owner.query_target_measurement_domain_authority(graph.targets[0].target_ref) is not None
        try:
            plan_file.write_bytes(original + b' ')
            with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                owner.query_target_measurement_domain_authority(graph.targets[0].target_ref)
        finally:
            plan_file.write_bytes(original)
        assert owner.query_target_measurement_domain_authority(graph.targets[0].target_ref) is not None
    assert database._read_cut.get() is None and database._read_cache.get() is None
