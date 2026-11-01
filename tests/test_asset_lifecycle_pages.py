import pytest

from test_public_asset_lifecycle import intake
from test_research_datasets import _runtime
from test_asset_lifecycle_adapters import channel
import meta_research.owners.asset_lifecycle as lifecycle_module


def test_asset_content_loads_only_selected_lifecycle(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path / 'selected-lifecycle')
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        first = intake(memory, b'original scientific observation', 'first')
        previous = first
        for revision in range(1, 8):
            previous = intake(memory, f'observation {revision}'.encode(), f'next-{revision}',
                asset_ref=first.asset_ref,
                change={'kind': 'supplement', 'predecessor_version_ref': previous.version_ref,
                    'expected_revision': revision, 'explanation': f'Adds observation {revision}'})
        full = memory.query_asset_lifecycle(first.asset_ref)
        calls = []
        original = lifecycle_module._accepted_change
        def observed(row):
            calls.append(row.change_ref)
            return original(row)
        monkeypatch.setattr(lifecycle_module, '_accepted_change', observed)
        monkeypatch.setattr(memory, 'query_asset_lifecycle', lambda *a: pytest.fail('ordinary pages reconstructed full history'))
        page = memory.read_asset_content_page(first.version_ref, limit=8)
        assert page['text'] == 'original'
        summary = page['lifecycle']
        assert summary['version_ref'] == first.version_ref and summary['state'] == 'superseded'
        assert summary['current_version_ref'] == previous.version_ref
        assert 'versions' not in summary and 'changes' not in summary
        assert summary['history_reader']['args']['version_ref'] == first.version_ref
        assert len(calls) <= 4, calls
        assert len(full['versions']) == 8
        print('LIFECYCLE_FACTS', {'versions': 8, 'before_per_page': 8, 'after_per_page': len(calls)})
    finally:
        runtime.close()


def test_public_discovery_and_current_offer_an_accurate_history_reader(tmp_path, monkeypatch):
    from test_root_workspace import _make
    from test_public_plan_stage import _confirm_direct_quest
    runtime = _make(tmp_path / 'lifecycle-channel')
    try:
        memory = runtime.owners.research_memory
        completed = _confirm_direct_quest(runtime)
        question = runtime.owners.research_graph.query_question(completed['initialization_id'])
        first = intake(memory, b'initial', 'initial', origin_quest_ref=question.quest_ref)
        second = intake(memory, b'supplemented', 'supplement', origin_quest_ref=question.quest_ref,
            asset_ref=first.asset_ref, change={'kind': 'supplement', 'predecessor_version_ref': first.version_ref,
                'expected_revision': 1, 'explanation': 'New observation extends the earlier material.'})
        read = channel(runtime, question)
        full_history = memory.query_asset_lifecycle
        monkeypatch.setattr(memory, 'query_asset_lifecycle', lambda *a: pytest.fail('ordinary page rebuilt history'))
        for operation, args in [('research_memory.assets.page', {}),
                                ('research_memory.assets.current', {'version_ref': first.version_ref})]:
            result = read(operation, **args)
            assert not result['isError'], result
            body = result['structuredContent']
            selected = body['items'][0] if 'items' in body else body
            assert selected['asset']['version_ref'] == second.version_ref
            assert selected['lifecycle']['version_ref'] == second.version_ref
            assert selected['lifecycle']['state'] == 'current'
            assert 'changes' not in selected['lifecycle']
        monkeypatch.setattr(memory, 'query_asset_lifecycle', full_history)
        reader = selected['lifecycle']['history_reader']
        result = read(reader['operation'], **reader['args'])
        assert not result['isError'], result
        assert len(result['structuredContent']['versions']) == 2
        assert result['structuredContent']['changes'][1]['explanation'] == 'New observation extends the earlier material.'
    finally:
        runtime.close()
