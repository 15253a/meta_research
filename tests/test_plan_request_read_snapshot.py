"""Idea/Plan request construction uses fresh cuts and releases before admission."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _confirm_direct_quest,
    _finish_idea_stage, _runtime,
)


@pytest.mark.parametrize('stage', ['idea', 'plan'])
def test_stage_request_cut_preserves_epoch_and_write_boundary(tmp_path, monkeypatch, stage):
    runtime = _runtime(tmp_path / 'plan-request', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        if stage == 'plan':
            _finish_idea_stage(runtime)
        worker = getattr(runtime, stage + '_stage')
        assert worker.process_once()
        owner, database = runtime.owners.advancement_engine, runtime._database
        query = getattr(owner, 'query_' + stage + '_stage_request')
        query_run = getattr(runtime.owners.agent_runtime, 'query_' + stage + '_stage_run')
        expected = query(quest['cycle_ref'])
        assert expected is not None and expected.stage == stage
        if stage == 'plan':
            assert expected.accepted_idea_set is not None
        convert = owner._stage_request_from_row
        cuts, writes = [], []

        def inspect(row):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return convert(row)

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                assert database._read_cut.get() is None and database._read_cache.get() is None
                writes.append(statement)

        monkeypatch.setattr(owner, '_stage_request_from_row', inspect)
        event.listen(database._engine, 'before_cursor_execute', observe)
        try:
            assert query(quest['cycle_ref']) == expected
            assert query(quest['cycle_ref']) == expected
            assert cuts[0][1] is not cuts[1][1]
            with database.read_snapshot() as connection:
                value = query(quest['cycle_ref'])
                assert value == expected and cuts[-1][0] is connection
                value.context_pack['mutation_probe'] = True
                assert query(quest['cycle_ref']) == expected
            assert query('cycle-missing') is None
            assert not writes
            assert database._read_cut.get() is None and database._read_cache.get() is None
            monkeypatch.setattr(owner, '_stage_request_from_row', Mock(side_effect=OwnerConflict('proof_unavailable')))
            with pytest.raises(OwnerConflict, match='proof_unavailable'):
                query(quest['cycle_ref'])
            assert database._read_cut.get() is None and database._read_cache.get() is None
            monkeypatch.setattr(owner, '_stage_request_from_row', inspect)
            assert query_run(expected.request_ref) is None
            assert worker.process_once()
            assert query_run(expected.request_ref) is not None
            assert writes and database._read_cut.get() is None
            with database.write() as connection:
                connection.execute(text('UPDATE ae_foreground_heads SET epoch=epoch+1 WHERE cycle_ref=:ref'),
                                   {'ref': quest['cycle_ref']})
            assert query(quest['cycle_ref']) is None
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            event.remove(database._engine, 'before_cursor_execute', observe)
    finally:
        runtime.close()
