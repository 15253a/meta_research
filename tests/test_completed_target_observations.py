import json
import pytest
from sqlalchemy import text
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from meta_research.owners.common import canonical_json, canonical_hash
from test_harness_target_root_recovery import _owner_with_active_root, _replace_channel
from test_target_root_observations import _event


def _recovered_history(tmp_path, monkeypatch):
    monkeypatch.setattr('meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS', 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / 'history.sqlite3')
    run = owner.query_run(request.request_ref)
    scope = {'schema_ref': 'meta-research/target-root-observation-scope/v1', 'target_run_ref': run.run_ref, 'attempt_ref': run.attempt_ref, 'attempt_generation': run.attempt_generation, 'root_session_ref': run.root_session_ref, 'fence_ref': run.fence_ref, 'native_session_ref': None}
    old_ref = f'{run.run_ref}:harness_turn:1'
    owner.start_operation(run_ref=run.run_ref, operation_ref=old_ref, generation=1, invocation_hash='a' * 64, resume=False)
    first = _event(1, 'original failed Provider output', scope=scope)
    owner.append_target_root_events(old_ref, (first,))
    owner.record_operation_failure(old_ref, 'provider_stopped')
    owner.reopen_failed_target_root(request.request_ref)
    _replace_channel(owner, request, suffix='recovered')
    new_ref = f'{run.run_ref}:harness_turn:2'
    owner.start_operation(run_ref=run.run_ref, operation_ref=new_ref, generation=2, invocation_hash='b' * 64, resume=False)
    second = _event(2, 'new successful Provider output', scope=scope)
    second['target_root_observation']['root_native_session_ref'] = 'native-recovered-target'
    owner.append_target_root_events(new_ref, (second,))
    owner.complete_operation(operation_ref=new_ref, run_ref=run.run_ref, native_session_ref='native-recovered-target', profile={}, evidence_events=(second,))
    return database, owner, handle.target_ref, first, second


def test_completed_target_reads_failed_history_and_current_success(tmp_path, monkeypatch):
    database, owner, target_ref, _, _ = _recovered_history(tmp_path, monkeypatch)
    try:
        first = owner.query_target_root_observations(target_ref, limit=1)
        assert first.status == 'turn_complete'
        assert first.native_session_ref == 'native-recovered-target'
        assert [(x.operation_generation, x.text) for x in first.items] == [(1, 'original failed Provider output')]
        second = owner.query_target_root_observations(target_ref, after_cursor=first.next_cursor)
        assert [(x.operation_generation, x.text) for x in second.items] == [(2, 'new successful Provider output')]
        assert second.has_more is False
    finally:
        database.close()


@pytest.mark.parametrize('damage', ('historical_hash', 'historical_scope', 'current_native', 'historical_native_conflict', 'historical_native_missing'))
def test_completed_observation_keeps_hash_scope_and_current_native_checks(tmp_path, monkeypatch, damage):
    database, owner, target_ref, first, second = _recovered_history(tmp_path, monkeypatch)
    try:
        event = second if damage == 'current_native' else first
        if damage in {'historical_native_conflict', 'historical_native_missing'}:
            with database.fenced_write() as connection:
                row = connection.execute(text('SELECT operation_ref FROM ar_harness_evidence_events WHERE event_ref=:ref'), {'ref': first['event_ref']}).one()
                conflicting = _event(19, 'same operation conflicting native', scope=first['target_run_scope'])
                conflicting['target_root_observation']['root_native_session_ref'] = None if damage == 'historical_native_missing' else 'conflicting-historical-native'
                connection.execute(text('INSERT INTO ar_harness_evidence_events (event_ref,operation_ref,sequence,summary_json,summary_hash,recorded_at) VALUES (:ref,:op,:seq,:body,:hash,1.0)'), {'ref': conflicting['event_ref'], 'op': row.operation_ref, 'seq': conflicting['sequence'], 'body': canonical_json(conflicting), 'hash': canonical_hash(conflicting)})
            with pytest.raises(AgentRuntimeHarnessError, match='target_root_observation_integrity_invalid'):
                owner.query_target_root_observations(target_ref, limit=1)
            return
        if damage == 'current_native':
            event['target_root_observation']['root_native_session_ref'] = 'wrong-current-native'
        elif damage == 'historical_scope':
            event['target_run_scope'] = {**event['target_run_scope'], 'fence_ref': 'wrong-fence'}
        else:
            event['target_root_observation']['text'] = 'modified-without-matching-hash'
        with database.fenced_write() as connection:
            connection.execute(text('UPDATE ar_harness_evidence_events SET summary_json=:value, summary_hash=:hash WHERE event_ref=:ref'), {'value': canonical_json(event), 'hash': canonical_hash(event) if damage != 'historical_hash' else '0' * 64, 'ref': event['event_ref']})
        with pytest.raises(AgentRuntimeHarnessError, match='target_root_observation_integrity_invalid'):
            owner.query_target_root_observations(target_ref)
    finally:
        database.close()
