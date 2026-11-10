"""Measure real Bundle source work and keep external-file integrity live."""
import os
from pathlib import Path

import pytest
from sqlalchemy import event

from meta_research.owners.common import OwnerConflict
from meta_research.owners import research_graph as rg, research_memory as rm
from test_bundle_report_receipt_order import committed_targets


def _contract(fixture):
    runtime, graph, run, _closures, source, projection = fixture
    return runtime.owners.research_graph.query_bundle_report_contract(
        request_ref=run.request_ref, run_ref=run.run_ref, graph_ref=graph.graph_ref,
        head_receipt=graph.head_receipt, formal_plan_content_receipt=source.receipt,
        formal_plan_projection_receipt=projection.receipt)


def test_bundle_contract_reuses_graph_reconstruction_and_plan_parse(committed_targets, monkeypatch):
    runtime = committed_targets[0]
    counts = {'graph_rebuilds': 0, 'plan_payload_parses': 0, 'sql': 0, 'plan_reads': 0}
    def count(name, function):
        def run(*args, **kwargs):
            counts[name] += 1
            return function(*args, **kwargs)
        return run
    monkeypatch.setattr(rg, '_accepted_target_graph', count('graph_rebuilds', rg._accepted_target_graph))
    monkeypatch.setattr(rm, '_read_verified_plan_payload', count('plan_payload_parses', rm._read_verified_plan_payload))
    monkeypatch.setattr(rm, '_read_plan_object', count('plan_reads', rm._read_plan_object))
    def sql(*_args):
        counts['sql'] += 1
    event.listen(runtime._database._engine, 'before_cursor_execute', sql)
    try:
        result = _contract(committed_targets)
    finally:
        event.remove(runtime._database._engine, 'before_cursor_execute', sql)
    print(counts)
    assert result['target_refs']
    assert counts['graph_rebuilds'] == 1


def test_next_public_bundle_read_rejects_same_size_same_mtime_plan_replacement(committed_targets):
    runtime, graph, *_ = committed_targets
    _contract(committed_targets)
    with runtime._database.read() as connection:
        row = connection.exec_driver_sql('SELECT object_path FROM rm_plan_documents WHERE content_ref=?', (graph.plan_content_ref,)).one()
    root = runtime.data_root.root / 'objects'
    # Use RM's actual custody root, not an inferred filesystem convention.
    root = runtime.owners.research_memory._object_store
    path = root / row.object_path
    before = path.read_bytes()
    metadata = path.stat()
    replacement = bytearray(before)
    replacement[len(replacement) // 2] ^= 1
    path.write_bytes(replacement)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    with pytest.raises(OwnerConflict):
        _contract(committed_targets)
