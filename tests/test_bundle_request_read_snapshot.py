"""Bundle request validation shares one read cut and releases before admission."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


def test_bundle_request_read_cut_preserves_validation_and_write_boundary(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'bundle-request')
    try:
        _prepare_bundle_request(runtime)
        worker, database = runtime.bundle_stage, runtime._database
        current = worker._discover_active_cycle()
        assert current is not None
        owner = runtime.owners.advancement_engine
        expected = owner.query_bundle_stage_request(current.cycle_ref)
        assert expected is not None and expected.accepted_formal_plan is not None
        convert = owner._stage_request_from_row
        cuts, writes = [], []

        def inspect(row):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return convert(row)

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)

        monkeypatch.setattr(owner, '_stage_request_from_row', inspect)
        event.listen(database._engine, 'before_cursor_execute', observe_sql)
        assert owner.query_bundle_stage_request(current.cycle_ref) == expected
        first_cache = cuts[-1][1]
        assert owner.query_bundle_stage_request(current.cycle_ref) == expected
        assert cuts[-1][1] is not first_cache
        with database.read_snapshot() as connection:
            value = owner.query_bundle_stage_request(current.cycle_ref)
            assert value == expected and cuts[-1][0] is connection
            value.context_pack['test_mutation'] = True
            assert owner.query_bundle_stage_request(current.cycle_ref) == expected
        assert owner.query_bundle_stage_request('cycle-missing') is None
        assert database._read_cut.get() is None and database._read_cache.get() is None
        assert not writes
        event.remove(database._engine, 'before_cursor_execute', observe_sql)

        # Production polling must leave the cut before AR writes its admission.
        assert runtime.owners.agent_runtime.query_bundle_stage_run(expected.request_ref) is None
        assert worker.process_once()
        assert runtime.owners.agent_runtime.query_bundle_stage_run(expected.request_ref) is not None
        assert database._read_cut.get() is None and database._read_cache.get() is None

        content_ref = expected.accepted_formal_plan.content_ref
        with database.write() as connection:
            row = connection.execute(text('SELECT plan_document_hash, object_path FROM rm_plan_documents WHERE content_ref=:ref'),
                                     {'ref': content_ref}).one()
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:bad WHERE content_ref=:ref'),
                               {'bad': '0' * 64, 'ref': content_ref})
        with pytest.raises(OwnerConflict):
            owner.query_bundle_stage_request(current.cycle_ref)
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.write() as connection:
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:good WHERE content_ref=:ref'),
                               {'good': row.plan_document_hash, 'ref': content_ref})
        plan_file = runtime.owners.research_memory._object_store / row.object_path
        saved = plan_file.read_bytes()
        with database.read_snapshot():
            assert owner.query_bundle_stage_request(current.cycle_ref) == expected
            try:
                plan_file.write_bytes(saved + b' ')
                with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                    owner.query_bundle_stage_request(current.cycle_ref)
            finally:
                plan_file.write_bytes(saved)
            assert owner.query_bundle_stage_request(current.cycle_ref) == expected

        monkeypatch.setattr(owner, '_stage_request_from_row', Mock(side_effect=OwnerConflict('proof_unavailable')))
        with pytest.raises(OwnerConflict, match='proof_unavailable'):
            owner.query_bundle_stage_request(current.cycle_ref)
        assert database._read_cut.get() is None and database._read_cache.get() is None
        monkeypatch.setattr(owner, '_stage_request_from_row', inspect)
        assert owner.query_bundle_stage_request(current.cycle_ref) == expected
    finally:
        runtime.close()
