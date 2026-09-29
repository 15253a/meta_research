"""Bundle polling qualifies against one short read cut, then writes outside it."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import (
    _bundle_runtime, _confirm_direct_quest, _finish_idea_stage, _finish_plan_stage,
)


def test_bundle_qualification_uses_fresh_cuts_and_releases_before_next_boundary(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'bundle')
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        worker, database = runtime.bundle_stage, runtime._database
        current = worker._discover_active_cycle()
        assert current is not None
        graph = runtime.owners.research_graph
        query = graph.query_formal_plan_decision
        cuts, writes = [], []

        def inspect(*args, **kwargs):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return query(*args, **kwargs)

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)

        monkeypatch.setattr(graph, 'query_formal_plan_decision', inspect)
        event.listen(database._engine, 'before_cursor_execute', observe_sql)
        expected = worker._qualify(current)
        assert expected[0] is not None
        first_cache = cuts[-1][1]
        assert worker._qualify(current) == expected
        assert cuts[-1][1] is not first_cache
        with database.read_snapshot() as connection:
            assert worker._qualify(current) == expected
            assert cuts[-1][0] is connection
            assert cuts[-1][1] is database._read_cache.get()
        assert database._read_cut.get() is None and database._read_cache.get() is None
        assert writes == []
        event.remove(database._engine, 'before_cursor_execute', observe_sql)

        # The real next worker boundary must still write its formal request.
        assert worker.process_once()
        assert database._read_cut.get() is None and database._read_cache.get() is None
        assert runtime.owners.advancement_engine.query_bundle_stage_request(current.cycle_ref) is not None
        content_ref = expected[0].binding.content_ref
        with database.write() as connection:
            row = connection.execute(text('SELECT plan_document_hash, object_path FROM rm_plan_documents WHERE content_ref=:ref'),
                                     {'ref': content_ref}).one()
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:bad WHERE content_ref=:ref'),
                               {'bad': '0' * 64, 'ref': content_ref})
        with pytest.raises(OwnerConflict):
            worker._qualify(current)
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.write() as connection:
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:good WHERE content_ref=:ref'),
                               {'good': row.plan_document_hash, 'ref': content_ref})
        plan_file = runtime.owners.research_memory._object_store / row.object_path
        saved = plan_file.read_bytes()
        with database.read_snapshot():
            assert worker._qualify(current) == expected
            try:
                plan_file.write_bytes(saved + b' ')
                with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                    worker._qualify(current)
            finally:
                plan_file.write_bytes(saved)
            assert worker._qualify(current) == expected
        monkeypatch.setattr(graph, 'query_formal_plan_decision', Mock(side_effect=OwnerConflict('proof_unavailable')))
        with pytest.raises(OwnerConflict, match='proof_unavailable'):
            worker._qualify(current)
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()
