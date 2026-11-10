"""Retired provider execution does not erase readable historical child metadata."""
import json

import pytest

from meta_research.child_session_observations import read_child_sessions
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2, ensure_transport_key, transport_file_sha256,
    write_transport_envelope,
)


def test_signed_historical_claude_child_spool_retains_metadata_and_rejects_tampering(tmp_path):
    directory = tmp_path / 'provider-operations' / 'historical-operation' / 'primary'
    directory.mkdir(parents=True)
    source = directory / 'stdout.jsonl'
    events = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'old-root'},
        {'type': 'assistant', 'session_id': 'old-root', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'Agent', 'input': {'prompt': 'private research'}}]}},
        {'type': 'system', 'subtype': 'task_started', 'session_id': 'old-root',
         'task_id': 'old-child', 'tool_use_id': 'tool-1', 'task_type': 'local_agent'},
        {'type': 'system', 'subtype': 'task_notification', 'session_id': 'old-root',
         'task_id': 'old-child', 'status': 'completed', 'output': 'private result'},
        {'type': 'result', 'session_id': 'old-root'},
    ]
    source.write_text(''.join(json.dumps(event) + '\n' for event in events))
    _, key = ensure_transport_key(tmp_path)
    write_transport_envelope(directory / 'supervisor-exit.json', {
        'schema_ref': SUPERVISOR_EXIT_SCHEMA_V2, 'invocation_hash': 'a' * 64,
        'stdout_file_hash': transport_file_sha256(source)}, key)
    args = dict(invocation_hash='a' * 64, parent_session_ref='owner-root',
                operation_ref='historical-operation', executing=False,
                expected_native='old-root', provider='claude')
    children, limited = read_child_sessions(directory, **args)
    assert limited is False and len(children) == 1
    assert children[0]['native_session_ref'] == 'old-child'
    assert children[0]['status'] == 'completed'
    assert children[0]['parent_native_session_ref'] == 'old-root'
    assert 'private research' not in json.dumps(children) and 'private result' not in json.dumps(children)
    assert read_child_sessions(directory, **args) == (children, False)
    source.write_text(source.read_text().replace('old-child', 'changed-child'))
    with pytest.raises(ValueError, match='child_session_source_unavailable'):
        read_child_sessions(directory, **args)
