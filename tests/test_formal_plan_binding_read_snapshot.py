"""Strict Plan binding verification is a bounded pure read, including under a writer."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_graph import _acquire_research_graph_writer_lock
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


def test_formal_plan_binding_read_cut_preserves_sources_and_writer_boundary(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'formal-plan-cut')
    try:
        _prepare_bundle_request(runtime)
        database, owner = runtime._database, runtime.owners.research_graph
        current = runtime.bundle_stage._discover_active_cycle()
        request = runtime.owners.advancement_engine.query_bundle_stage_request(current.cycle_ref)
        binding = request.accepted_formal_plan
        verifier = owner._receipt_verifier
        original = verifier.verify_formal_plan_decision
        cuts, writes = [], []
        verifying = False

        def inspect(**values):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return original(**values)

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if verifying and statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)

        def verify():
            nonlocal verifying
            verifying = True
            try:
                return verifier.verify_accepted_formal_plan_binding(binding)
            finally:
                verifying = False

        monkeypatch.setattr(verifier, 'verify_formal_plan_decision', inspect)
        event.listen(database._engine, 'before_cursor_execute', observe_sql)
        assert verify() is None
        first_cache = cuts[-1][1]
        assert verify() is None
        assert cuts[-1][1] is not first_cache
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.read_snapshot() as connection:
            assert verify() is None
            assert cuts[-1][0] is connection

        # Mirror TargetGraph acceptance: hold the existing RG writer lock,
        # verify through the external read connection, then continue DML.
        with database.write() as writer:
            _acquire_research_graph_writer_lock(writer)
            assert verify() is None
            assert cuts[-1][0] is not writer
            assert database._read_cut.get() is None and database._read_cache.get() is None
            writer.execute(text('UPDATE research_graph_state SET revision=revision WHERE singleton=\'owner\''))
        assert not writes

        with database.write() as connection:
            row = connection.execute(text('SELECT plan_document_hash, object_path FROM rm_plan_documents WHERE content_ref=:ref'),
                                     {'ref': binding.content_ref}).one()
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:bad WHERE content_ref=:ref'),
                               {'bad': '0' * 64, 'ref': binding.content_ref})
        try:
            with pytest.raises(OwnerConflict):
                verify()
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            with database.write() as connection:
                connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:good WHERE content_ref=:ref'),
                                   {'good': row.plan_document_hash, 'ref': binding.content_ref})
        path = runtime.owners.research_memory._object_store / row.object_path
        saved = path.read_bytes()
        try:
            path.write_bytes(saved + b' ')
            with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                verify()
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            path.write_bytes(saved)
        assert verify() is None
        monkeypatch.setattr(verifier, 'verify_formal_plan_decision', Mock(side_effect=OwnerConflict('proof_unavailable')))
        with pytest.raises(OwnerConflict, match='proof_unavailable'):
            verify()
        assert database._read_cut.get() is None and database._read_cache.get() is None
        assert not writes
        event.remove(database._engine, 'before_cursor_execute', observe_sql)
    finally:
        runtime.close()
