from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_root_workspace import _make
from test_public_plan_stage import _confirm_direct_quest


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
