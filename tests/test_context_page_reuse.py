import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import meta_research.context_presentation as presentation
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.stage_context_access import stage_context_operations
from test_context_boundaries import auth_objects


def channel(pack, graph=None):
    context, runtime, engine = auth_objects(pack)
    operation = stage_context_operations(advancement_engine=engine,
        agent_runtime=runtime, research_graph=graph, research_memory=None)[0]
    return context, runtime, engine, operation.handler


def test_exact_stage_pages_encode_and_digest_selected_representation_once(monkeypatch):
    pack = {'left': '研究😀' * 1000, 'right': 'Other scientific material'}
    context, runtime, engine, read = channel(pack)
    raw = canonical_json(pack['left']).encode('utf8')
    expected_hash = hashlib.sha256(raw).hexdigest()
    encoded, digested = [], []
    original_json, original_hash = presentation._json, hashlib.sha256
    def encode(value):
        result = original_json(value)
        encoded.append(len(result.encode('utf8')))
        return result
    def digest(value):
        digested.append(len(value))
        return original_hash(value)
    monkeypatch.setattr(presentation, '_json', encode)
    monkeypatch.setattr(presentation.hashlib, 'sha256', digest)
    args = dict(context_pack_ref='ctx', source='context_pack', path=['left'], offset=0, limit=127)
    result = b''
    pages = 0
    while True:
        page = read(context, args)
        assert page['content_hash'] == expected_hash
        result += page['text'].encode('utf8')
        pages += 1
        if page['next_offset'] is None:
            break
        args['offset'] = page['next_offset']
    assert result == raw
    assert encoded == [len(raw)]
    assert digested == [len(raw)]
    assert runtime.verify_root_agent_runtime_scope.call_count == pages
    print('CONTEXT_PAGE_COST', dict(pages=pages, before_encoding_bytes=pages*len(raw),
        after_encoding_bytes=sum(encoded), before_hash_bytes=pages*len(raw), after_hash_bytes=sum(digested)))
    other = read(context, {**args, 'path': ['right'], 'offset': 0})
    assert other['text'] == '"Other scientific material"'
    assert other['content_hash'] != expected_hash
    with pytest.raises(SemanticMcpError, match='stage_context_offset_invalid'):
        read(context, {**args, 'offset': 2})
    with pytest.raises(SemanticMcpError, match='stage_context_ref_unbound'):
        read(context, {**args, 'context_pack_ref': 'wrong'})
    runtime.verify_root_agent_runtime_scope.side_effect = OwnerConflict('revoked')
    with pytest.raises(SemanticMcpError, match='semantic_call_scope_stale'):
        read(context, {**args, 'offset': 0})


def test_current_history_never_reuses_an_unrevisioned_page():
    graph = SimpleNamespace(query_question_research_history=Mock(side_effect=[
        {'items': ['old'], 'offset': 0, 'next_offset': None},
        {'items': ['new'], 'offset': 0, 'next_offset': None}]))
    context, _, _, read = channel({}, graph)
    args = dict(context_pack_ref='ctx', source='question_history', path=[], offset=0, limit=1024)
    first, second = read(context, args), read(context, args)
    assert 'old' in first['text'] and 'new' in second['text']
    assert first['content_hash'] != second['content_hash']


def test_representation_cache_bounds_revision_and_failed_read(monkeypatch):
    cache = presentation.ExactContextPages(max_bytes=32, max_entries=2)
    encoded = []
    original = presentation._json
    def encode(value):
        encoded.append(value)
        return original(value)
    monkeypatch.setattr(presentation, '_json', encode)
    def read(value, identity, **args):
        return presentation.context_read_page(value, path=[], offset=0, limit=32,
            cache=cache, identity=(identity,), **args)
    assert read('one', 'v1')['text'] == '"one"'
    assert read('two', 'v2')['text'] == '"two"'
    assert read('one', 'v1')['text'] == '"one"'
    assert encoded == ['one', 'two']
    read('three', 'v3')
    read('two', 'v2')  # entry LRU eviction
    assert encoded == ['one', 'two', 'three', 'two']
    read('x' * 40, 'oversized')
    read('x' * 40, 'oversized')
    assert encoded[-2:] == ['x' * 40, 'x' * 40]
    with pytest.raises(TypeError):
        read(object(), 'failed')
    assert read('recovered', 'failed')['text'] == '"recovered"'
    fresh = presentation.ExactContextPages(max_bytes=8)
    for identity, value in [('first', '1111'), ('second', '2222'), ('first', '1111')]:
        presentation.context_read_page(value, path=[], offset=0, limit=32,
            cache=fresh, identity=(identity,))
    assert encoded[-3:] == ['1111', '2222', '1111']  # byte budget eviction
