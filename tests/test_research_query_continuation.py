import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from meta_research.research_content import discover_questions
from test_public_manual_question_lifecycle import _build_runtime as _runtime, _accept_root_question

def _quest(runtime, prefix):
    quest_ref, question_ref = _accept_root_question(runtime, prefix)
    return runtime.owners.research_graph.query_question_history_by_ref(question_ref)


def test_question_page_binds_query_and_owner_state_not_other_quests(tmp_path):
    runtime = _runtime(tmp_path / 'query')
    try:
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        question = _quest(runtime, 'one')
        first = discover_questions(graph, memory, quest_ref=question.quest_ref, limit=1)
        _quest(runtime, 'unrelated')
        statements = []
        def capture(conn, cursor, statement, params, context, many):
            statements.append(statement)
        event.listen(graph._database._engine, 'before_cursor_execute', capture)
        second = discover_questions(graph, memory, quest_ref=question.quest_ref,
                                    offset=1, limit=1, query_revision=first['query_revision'])
        event.remove(graph._database._engine, 'before_cursor_execute', capture)
        assert second['items'] == []
        assert second['result_state'] == 'exhausted'
        assert len([s for s in statements if s.startswith('SELECT')]) <= 2
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            discover_questions(graph, memory, quest_ref=question.quest_ref,
                               query='different', offset=1, query_revision=first['query_revision'])
        with graph._database.write() as connection:
            connection.execute(text('UPDATE rg_question_lifecycle SET status=:status WHERE question_ref=:ref'),
                               {'status': 'pruned', 'ref': question.question_ref})
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            discover_questions(graph, memory, quest_ref=question.quest_ref,
                               offset=1, query_revision=first['query_revision'])
        with pytest.raises(OwnerConflict, match='research_query_revision_required'):
            discover_questions(graph, memory, quest_ref=question.quest_ref, offset=1)
    finally:
        runtime.close()


def test_empty_question_search_has_explicit_meaning(tmp_path):
    runtime = _runtime(tmp_path / 'empty')
    try:
        question = _quest(runtime, 'one')
        result = discover_questions(runtime.owners.research_graph, runtime.owners.research_memory,
                                    quest_ref=question.quest_ref, query='absent-term')
        assert result['result_state'] == 'no_match'
        assert result['scan_complete'] is True
    finally:
        runtime.close()


@pytest.mark.parametrize('entry', ['datasets', 'environments'])
def test_resource_entry_continuation_binds_selected_owner_records(tmp_path, entry):
    from test_research_datasets import _dataset
    from test_research_environments import _environment
    runtime = _runtime(tmp_path / entry)
    try:
        graph = runtime.owners.research_graph
        create = (lambda key: _dataset(graph, key)) if entry == 'datasets' else (
            lambda key: _environment(graph, key=key, semantic_key='equipment:' + key))
        read = getattr(graph, 'query_' + entry)
        create('one'); create('two')
        first = read(limit=1)
        second = read(limit=1, offset=first['next_offset'], query_revision=first['query_revision'])
        assert len(second['items']) == 1 and second['next_offset'] is None
        create('three')
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            read(limit=1, offset=first['next_offset'], query_revision=first['query_revision'])
    finally:
        runtime.close()


def test_literature_empty_scan_page_is_not_absence_and_reader_stays_exact(tmp_path):
    from test_literature_bounded_content import make_corpus, SmallCorpusProvider
    from meta_research.research_content import discover_literature, read_content
    runtime, request, snapshot, revision = make_corpus(tmp_path / 'literature', SmallCorpusProvider())
    try:
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        args = dict(quest_ref=request.accepted_question.quest_ref, query='no-such-title', limit=1)
        page = discover_literature(graph, memory, **args)
        assert page['items'] == [] and page['result_state'] == 'page_pending'
        assert not page['scan_complete']
        final = discover_literature(graph, memory, **args,
            offset=page['next_offset'], query_revision=page['query_revision'])
        assert final['items'] == []
        selected = discover_literature(graph, memory, quest_ref=args['quest_ref'], limit=1)['items'][0]
        result = read_content(graph, memory, quest_ref=args['quest_ref'], **selected['reader'], limit=128)
        assert result['returned_bytes'] <= 128 and result['source_ref'] == selected['reader']['source_ref']
        # A newly changed judgment is part of this directory's result cut even
        # when the literature revision itself stays unchanged.
        with graph._database.write() as connection:
            connection.execute(text("UPDATE rm_idea_outcome_contents SET payload_hash=:hash WHERE "
                "request_ref IN (SELECT request_ref FROM ae_stage_run_requests WHERE quest_ref=:quest)"),
                {'hash':'0' * 64,'quest':args['quest_ref']})
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            discover_literature(graph,memory,**args,offset=page['next_offset'],query_revision=page['query_revision'])
    finally:
        runtime.close()
