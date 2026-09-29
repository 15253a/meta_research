"""Target source reconstruction shares proofs only within its own read cut."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_plan_asset_target_input import _accepted_target, _asset, _origin, _runtime
import test_public_bundle_stage as fixtures


@pytest.mark.parametrize('query_kind', ['research_context', 'selected_sources'])
def test_target_input_reads_pin_fresh_cuts_without_crossing_writes(tmp_path, monkeypatch, query_kind):
    runtime, plan, bundle = _runtime(tmp_path / query_kind)
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        asset = _asset(runtime, 'exact-input')
        _origin(runtime, asset, quest['quest_ref'], 'exact-input-origin')
        plan.source_ref = bundle.source_ref = asset.version_ref
        target, _plan = _accepted_target(runtime, quest)
        owner = runtime.target_run_authorities.research_graph
        database = runtime._database
        reader = owner._domain_reader
        method = 'query_target_graph' if query_kind == 'research_context' else 'resolve_plan_evidence_reuse_leaves'
        original = getattr(reader, method)
        cuts, writes = [], []

        def inspect(*args, **kwargs):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return original(*args, **kwargs)

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)

        monkeypatch.setattr(reader, method, inspect)
        query = (lambda: owner.query_target_research_context(target_ref=target.target_ref)) if query_kind == 'research_context' else (lambda: owner.selected_evidence_target_commits(target.target_ref))
        event.listen(database._engine, 'before_cursor_execute', observe_sql)
        expected = query()
        first_cache = cuts[-1][1]
        assert query() == expected
        assert cuts[-1][1] is not first_cache
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.read_snapshot() as connection:
            assert query() == expected
            assert cuts[-1][0] is connection
            assert cuts[-1][1] is database._read_cache.get()
        event.remove(database._engine, 'before_cursor_execute', observe_sql)
        assert writes == []

        # The cut ends before the next effect, and a new read sees that write.
        with database.read() as connection:
            original_hash = connection.execute(text('SELECT spec_hash FROM rg_targets WHERE target_ref=:ref'), {'ref': target.target_ref}).scalar_one()
        with database.write() as connection:
            connection.execute(text('UPDATE rg_targets SET spec_hash=:hash WHERE target_ref=:ref'), {'hash': '0' * 64, 'ref': target.target_ref})
        with pytest.raises(OwnerConflict):
            query()
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.write() as connection:
            connection.execute(text('UPDATE rg_targets SET spec_hash=:hash WHERE target_ref=:ref'), {'hash': original_hash, 'ref': target.target_ref})
        assert query() == expected

        monkeypatch.setattr(reader, method, Mock(side_effect=OwnerConflict('source_unavailable')))
        with pytest.raises(OwnerConflict, match='source_unavailable'):
            query()
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()
