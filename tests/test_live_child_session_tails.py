"""A verified live JSONL writer may be between writes; hard read limits remain."""
import json

import pytest

import meta_research.child_session_observations as observations
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2, ensure_transport_key, transport_file_sha256,
    write_transport_envelope,
)


def _event(event):
    return json.dumps(event).encode() + b'\n'


def _prefix(provider='codex'):
    if provider == 'claude':
        return b''.join(map(_event, [
            {'type': 'system', 'subtype': 'init', 'session_id': 'root'},
            {'type': 'assistant', 'session_id': 'root', 'message': {'content': [
                {'type': 'tool_use', 'id': 'tool-1', 'name': 'Agent', 'input': {'prompt': 'private prompt'}}]}},
            {'type': 'system', 'subtype': 'task_started', 'session_id': 'root',
             'task_id': 'child', 'tool_use_id': 'tool-1', 'task_type': 'local_agent'},
        ]))
    return b''.join(map(_event, [
        {'type': 'thread.started', 'thread_id': 'root'},
        {'type': 'item.completed', 'thread_id': 'root', 'item': {
            'type': 'collab_tool_call', 'tool': 'spawn_agent', 'status': 'completed',
            'receiver_thread_ids': ['child'], 'agents_states': {'child': {'status': 'running'}}}},
    ]))


def _read(directory, **kwargs):
    return observations.observe_child_sessions(directory, invocation_hash='a' * 64,
        parent_session_ref='owner-root', operation_ref='operation', expected_native='root',
        **{'executing': True, **kwargs})


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_live_tail_preserves_prefix_until_same_writer_finishes(tmp_path, provider):
    source = tmp_path / 'stdout.jsonl'
    tail = _event({'type': 'fixture', 'private': 'private output'})
    source.write_bytes(_prefix(provider) + tail[:12])
    pending = _read(tmp_path, provider=provider)
    assert pending.pending_tail is True and pending.limited is False
    assert pending.children[0]['status'] == 'running'
    assert 'private' not in json.dumps(pending.children)
    with source.open('ab') as stream:
        stream.write(tail[12:])
    caught_up = _read(tmp_path, provider=provider)
    assert caught_up.pending_tail is False and caught_up.limited is False
    assert caught_up.children[0]['session_ref'] == pending.children[0]['session_ref']
    assert caught_up.children[0]['status'] == 'running'
    assert caught_up.children[0]['updated_at'] >= pending.children[0]['updated_at']


@pytest.mark.parametrize('terminal', ['inactive', 'turn.completed', 'turn.failed', 'signed-exit', 'claude-result'])
def test_terminal_incomplete_tail_remains_limited(tmp_path, terminal):
    directory = tmp_path / 'provider-operations' / 'operation' / 'primary'
    directory.mkdir(parents=True)
    provider = 'claude' if terminal == 'claude-result' else 'codex'
    prefix = _prefix(provider)
    if terminal in {'turn.completed', 'turn.failed'}:
        prefix += _event({'type': terminal, 'thread_id': 'root'})
    elif terminal == 'claude-result':
        prefix += _event({'type': 'result', 'session_id': 'root'})
    source = directory / 'stdout.jsonl'
    source.write_bytes(prefix + b'{"type":')
    if terminal == 'signed-exit':
        _, key = ensure_transport_key(tmp_path)
        write_transport_envelope(directory / 'supervisor-exit.json', {
            'schema_ref': SUPERVISOR_EXIT_SCHEMA_V2, 'invocation_hash': 'a' * 64,
            'stdout_file_hash': transport_file_sha256(source)}, key)
    result = _read(directory, executing=terminal != 'inactive', provider=provider)
    assert result.limited is True and result.pending_tail is False
    assert result.children[0]['status'] == 'unknown'


@pytest.mark.parametrize('invalid', [b'{broken}\n', b'\xff\n', b'[]\n'])
def test_complete_invalid_line_is_a_read_failure_even_while_live(tmp_path, invalid):
    (tmp_path / 'stdout.jsonl').write_bytes(_prefix() + invalid)
    result = _read(tmp_path)
    assert result.limited is True and result.pending_tail is False
    assert result.children[0]['status'] == 'unknown'


def test_live_unfinished_line_cannot_bypass_line_cap(tmp_path, monkeypatch):
    prefix = _prefix()
    cap = len(prefix)
    monkeypatch.setattr(observations, '_MAX_LINE', cap)
    (tmp_path / 'stdout.jsonl').write_bytes(prefix + b'x' * (cap + 1))
    result = _read(tmp_path)
    assert result.limited is True and result.pending_tail is False


def test_live_unfinished_line_cannot_bypass_source_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(observations, '_MAX_BYTES', 10)
    (tmp_path / 'stdout.jsonl').write_bytes(_prefix() + b'{')
    with pytest.raises(ValueError, match='child_session_source_unavailable'):
        _read(tmp_path)


def test_pending_tail_does_not_clear_an_independent_child_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(observations, '_MAX_CHILDREN', 0)
    (tmp_path / 'stdout.jsonl').write_bytes(_prefix() + b'{')
    result = _read(tmp_path)
    assert result.limited is True and result.pending_tail is True
