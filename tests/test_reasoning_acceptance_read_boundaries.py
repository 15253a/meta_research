"""Reasoning reads reuse proofs without carrying a cut into acceptance writes."""
from dataclasses import replace

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill, _confirm_deepfetch_quest, _finish_idea_stage,
    _reasoning_runtime, _tick_reasoning,
)


@pytest.fixture
def pending_reasoning(tmp_path):
    runtime = _reasoning_runtime(tmp_path / 'reasoning', reasoning_skill=_DeterministicReasoningSkill())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(5):
            current = _tick_reasoning(runtime)
        assert current['reasoning_acceptance']['status'] == 'awaiting_domain'
        content = runtime.owners.research_memory.query_reasoning_content(current['run']['submission_ref'])
        assert content is not None
        yield runtime, quest, content
    finally:
        runtime.close()


def test_reasoning_request_constructs_in_fresh_cut_and_preserves_current_epoch(pending_reasoning, monkeypatch):
    runtime, quest, content = pending_reasoning
    owner, database = runtime.owners.advancement_engine, runtime._database
    expected = owner.query_reasoning_stage_request(quest['cycle_ref'])
    convert = owner._stage_request_from_row
    caches = []

    def inspect(row):
        assert database._read_cut.get() is not None
        cache = database._read_cache.get()
        assert cache is not None
        caches.append(cache)
        return convert(row)

    monkeypatch.setattr(owner, '_stage_request_from_row', inspect)
    assert owner.query_reasoning_stage_request(quest['cycle_ref']) == expected
    assert owner.query_reasoning_stage_request(quest['cycle_ref']) == expected
    assert caches[0] is not caches[1]
    with database.read_snapshot() as connection:
        value = owner.query_reasoning_stage_request(quest['cycle_ref'])
        assert value == expected and database._read_cut.get() is connection
        value.context_pack['mutation_probe'] = True
        assert owner.query_reasoning_stage_request(quest['cycle_ref']) == expected
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE ae_foreground_heads SET epoch=epoch+1 WHERE cycle_ref=:ref'),
                           {'ref': quest['cycle_ref']})
    assert owner.query_reasoning_stage_request(quest['cycle_ref']) is None
    assert database._read_cut.get() is None and database._read_cache.get() is None


def test_reasoning_content_proofs_end_before_domain_write_and_remain_live(pending_reasoning, monkeypatch):
    runtime, quest, content = pending_reasoning
    graph, memory, database = runtime.owners.research_graph, runtime.owners.research_memory, runtime._database
    verifier = memory._receipt_verifier
    verify = verifier.verify_reasoning_content_receipt
    caches, writes = [], []

    def inspect(**values):
        assert database._read_cut.get() is not None
        cache = database._read_cache.get()
        assert cache is not None
        caches.append(cache)
        return verify(**values)

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            assert database._read_cut.get() is None and database._read_cache.get() is None
            writes.append(statement)

    monkeypatch.setattr(verifier, 'verify_reasoning_content_receipt', inspect)
    source_kind = graph._source_current_reasoning_question_kind

    def current_question(**values):
        assert database._read_cut.get() is None and database._read_cache.get() is None
        return source_kind(**values)

    monkeypatch.setattr(graph, '_source_current_reasoning_question_kind', current_question)
    event.listen(database._engine, 'before_cursor_execute', observe)
    try:
        assert memory.query_reasoning_content(content.submission_ref) == content
        assert memory.query_reasoning_content(content.submission_ref) == content
        assert caches[-1] is not caches[-2]
        assert not writes
        with database.read_snapshot() as connection:
            assert memory.query_reasoning_content(content.submission_ref) == content
            assert database._read_cut.get() is connection
        with pytest.raises(OwnerConflict, match='reasoning_content_receipt_invalid'):
            graph.decide_reasoning_outcome(content=replace(content, payload_hash='0' * 64))
        assert not writes
        assert database._read_cut.get() is None and database._read_cache.get() is None
        with database.read() as connection:
            path = connection.execute(text('SELECT object_path FROM rm_reasoning_contents WHERE content_ref=:ref'),
                                      {'ref': content.content_ref}).scalar_one()
        original = memory._object_store / path
        saved = original.read_bytes()
        with database.read_snapshot():
            assert memory.query_reasoning_content(content.submission_ref) == content
            try:
                original.write_bytes(saved + b' ')
                with pytest.raises(OwnerConflict):
                    memory.query_reasoning_content(content.submission_ref)
            finally:
                original.write_bytes(saved)
        assert database._read_cut.get() is None and database._read_cache.get() is None
        assert not writes
        offset = len(caches)
        decision = graph.decide_reasoning_outcome(content=content)
        assert decision.decision == 'accepted' and writes
        assert len(caches) >= offset + 2 and caches[offset] is not caches[-1]
        with database.read_snapshot():
            assert graph.query_reasoning_outcome_decision(content.submission_ref) == decision
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        event.remove(database._engine, 'before_cursor_execute', observe)
