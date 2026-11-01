from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_root_workspace import _make
from test_public_plan_stage import _confirm_direct_quest


def test_owner_question_asset_page_without_lifecycle_history_stays_readable(tmp_path):
    import json
    runtime = _make(tmp_path / 'owner-question-page')
    try:
        completed = _confirm_direct_quest(runtime)
        question = runtime.owners.research_graph.query_question(completed['initialization_id'])
        memory = runtime.owners.research_memory
        expected = memory.read_question_content(question.content_ref, question.content_hash)
        page = memory.read_asset_content_page(question.content_ref)
        assert json.loads(page['text']) == expected
        assert page['asset_binding']['version_ref'] == question.content_ref
        assert page['lifecycle']['revision'] == 0
        assert page['lifecycle']['state'] == 'unselected'
        assert page['lifecycle']['current_version_ref'] is None
        assert memory.query_asset_lifecycle(question.content_ref)['changes'] == []
        assert memory.query_current_asset(question.content_ref) is None
        from test_public_asset_lifecycle import intake
        regular = intake(memory, b'explicit asset selection', 'explicit-selection')
        with memory._database.write() as connection:
            connection.execute(text('DELETE FROM rm_asset_lifecycle WHERE asset_ref=:ref'),
                {'ref': regular.asset_ref})
        with pytest.raises(OwnerConflict, match='asset_lifecycle_state_invalid'):
            memory.read_asset_content_page(regular.version_ref)
    finally:
        runtime.close()


def test_manual_question_read_verifies_file_once_and_detects_later_drift(tmp_path, monkeypatch):
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    runtime = _make(tmp_path / 'manual-question-consumption')
    try:
        completed = _confirm_direct_quest(runtime)
        human, memory = runtime.owners.human_collaboration, runtime.owners.research_memory
        seeded = _confirm_waived_manual_question(human, quest_ref=completed['quest_ref'],
            parent_question_ref=completed['question_ref'], key_prefix='manual-consumption')
        for _ in range(8):
            if human.query_manual_question_creation(seeded['context_ref'])['status'] == 'completed':
                break
            assert human.reconcile_once()
        accepted = memory.query_manual_question_content(seeded['context_ref'])
        assert accepted is not None
        with memory._database.read() as connection:
            path = memory._object_store / connection.execute(text(
                'SELECT object_path FROM rm_manual_question_contents WHERE content_ref=:ref'),
                {'ref': accepted.content_ref}).scalar_one()
        original = Path.read_bytes
        reads = []
        def observed(candidate):
            result = original(candidate)
            if candidate == path:
                reads.append(len(result))
            return result
        monkeypatch.setattr(Path, 'read_bytes', observed)
        assert memory.query_manual_question_content(seeded['context_ref']) == accepted
        assert len(reads) == 1, reads
        body = memory.read_question_content(accepted.content_ref, accepted.content_hash)
        assert body['unknown_statement']
        assert len(reads) == 2, reads
        raw = original(path)
        path.write_bytes(b' ' + raw[1:])
        with pytest.raises(OwnerConflict, match='question_content_custody_unavailable'):
            memory.read_question_content(accepted.content_ref, accepted.content_hash)
        path.write_bytes(raw)
        assert memory.read_question_content(accepted.content_ref, accepted.content_hash) == body
    finally:
        runtime.close()


def test_question_consumption_verifies_one_read_and_rechecks_next_call(tmp_path, monkeypatch):
    runtime = _make(tmp_path / 'question-consumption')
    try:
        completed = _confirm_direct_quest(runtime)
        question = runtime.owners.research_graph.query_question(completed['initialization_id'])
        memory = runtime.owners.research_memory
        with memory._database.read() as connection:
            path = memory._object_store / connection.execute(text(
                'SELECT object_path FROM rm_formal_question_contents WHERE content_ref=:ref'),
                {'ref': question.content_ref}).scalar_one()
        original = Path.read_bytes
        reads = []
        def observed(candidate):
            result = original(candidate)
            if candidate == path:
                reads.append(len(result))
            return result
        monkeypatch.setattr(Path, 'read_bytes', observed)
        first = memory.read_question_content(question.content_ref, question.content_hash)
        assert first['unknown_statement']
        assert len(reads) == 1, reads
        assert memory.read_question_content(question.content_ref, question.content_hash) == first
        assert len(reads) == 2, reads
        print('QUESTION_CONSUMPTION_IO', {'before_reads_per_call': 2, 'after_reads_per_call': 1, 'bytes_per_read': reads[0]})
        raw = original(path)
        path.write_bytes(b' ' + raw[1:])
        with pytest.raises(OwnerConflict, match='question_content_custody_unavailable'):
            memory.read_question_content(question.content_ref, question.content_hash)
        path.write_bytes(raw)
        assert memory.read_question_content(question.content_ref, question.content_hash) == first
    finally:
        runtime.close()


def test_public_question_pages_reuse_json_representation_but_recheck_files(tmp_path, monkeypatch):
    import meta_research.context_presentation as presentation
    from test_research_content_reader import channel
    runtime = _make(tmp_path / 'question-pages')
    try:
        completed = _confirm_direct_quest(runtime)
        question = runtime.owners.research_graph.query_question(completed['initialization_id'])
        read = channel(runtime, question)
        encoded = []
        original = presentation._json
        def encode(value):
            result = original(value)
            encoded.append(len(result.encode('utf8')))
            return result
        monkeypatch.setattr(presentation, '_json', encode)
        offset = 0
        while True:
            response = read(source_ref=question.question_ref, version_ref=question.content_ref, offset=offset, limit=64)
            assert not response['isError'], response
            page = response['structuredContent']
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        assert len(encoded) == 1, encoded
    finally:
        runtime.close()
