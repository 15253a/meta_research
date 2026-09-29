"""Display reads keep exact accepted rows without replaying all admission work."""
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.research_overview import ResearchOverviewReader
from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _runtime,
    _confirm_direct_quest, _finish_idea_stage,
)


@pytest.fixture
def accepted_idea(tmp_path):
    runtime = _runtime(tmp_path / 'display', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        yield runtime, quest
    finally:
        runtime.close()


def _reader(runtime):
    owners = runtime.owners
    return ResearchOverviewReader(owners.research_graph, owners.advancement_engine,
                                  owners.research_memory, owners.agent_runtime)


def test_display_history_avoids_admission_replay_and_keeps_exact_row_bindings(accepted_idea, monkeypatch):
    runtime, quest = accepted_idea
    owner, database = runtime.owners.advancement_engine, runtime._database
    request = Mock(wraps=owner._stage_request_from_row)
    commit = Mock(wraps=owner._stage_commit_from_row)
    monkeypatch.setattr(owner, '_stage_request_from_row', request)
    monkeypatch.setattr(owner, '_stage_commit_from_row', commit)
    with database.read_snapshot():
        display = owner.query_quest_stage_history_display(quest['quest_ref'])
        request.assert_not_called()
        commit.assert_not_called()
        assert owner.query_quest_stage_history(quest['quest_ref']) == display
        assert request.call_count > 0 and commit.call_count > 0
    # Only overview selects the display seam. Full history remains the default
    # for execution and Writing; rendering must not quietly select it again.
    monkeypatch.setattr(owner, 'query_quest_stage_history',
                        Mock(side_effect=AssertionError('overview_replayed_admission')))
    with database.read_snapshot():
        assert _reader(runtime)._query_once(quest['quest_ref'])['status'] == 'ready'

    for table, key, field, code in (
        ('ae_stage_run_requests', 'request_ref', 'context_pack_hash', 'stage_run_request_invalid'),
        ('ae_stage_commits', 'commit_ref', 'receipt_hash', 'stage_commit_receipt_invalid'),
        ('ae_cycles', 'cycle_ref', 'question_ref', 'quest_stage_history_binding_invalid'),
    ):
        with database.read() as connection:
            row = connection.execute(text(f'SELECT {key}, {field} FROM {table} LIMIT 1')).one()
        try:
            with database.write() as connection:
                connection.execute(text(f'UPDATE {table} SET {field}=:value WHERE {key}=:ref'),
                                   {'value': '0' * 64, 'ref': row[0]})
            with database.read_snapshot(), pytest.raises(OwnerConflict, match=code):
                owner.query_quest_stage_history_display(quest['quest_ref'])
        finally:
            with database.write() as connection:
                connection.execute(text(f'UPDATE {table} SET {field}=:value WHERE {key}=:ref'),
                                   {'value': row[1], 'ref': row[0]})
    assert owner.query_quest_stage_history_display(quest['quest_ref']) == display


def test_display_history_still_rejects_damaged_displayed_original(accepted_idea):
    runtime, quest = accepted_idea
    reader = _reader(runtime)
    with runtime._database.read_snapshot():
        before = reader._query_once(quest['quest_ref'])
    idea = before['cycles'][0]['stages']['idea'][0]
    with runtime._database.read() as connection:
        relative = connection.execute(text(
            'SELECT object_path FROM rm_idea_outcome_contents WHERE content_ref=:ref'),
            {'ref': idea['source']['content_ref']}).scalar_one()
    path = runtime.owners.research_memory._object_store / relative
    saved = path.read_bytes()
    try:
        path.write_bytes(b'{}')
        with runtime._database.read_snapshot():
            damaged = reader._query_once(quest['quest_ref'])
        item = damaged['cycles'][0]['stages']['idea'][0]
        assert damaged['status'] == 'limited'
        assert item['content'] is None and item['status'] == 'unavailable'
        assert item['reason']['code'] == 'idea_content_custody_unavailable'
        assert damaged['findings']['quest'] == []
    finally:
        path.write_bytes(saved)
    with runtime._database.read_snapshot():
        assert reader._query_once(quest['quest_ref']) == before
