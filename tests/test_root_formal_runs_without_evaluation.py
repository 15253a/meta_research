"""Actual runs survive an unfinished evaluation without manufactured results."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.formal_entities import formal_inventory, root_work_items
from meta_research.owners.common import OwnerConflict, canonical_json
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _work_items(runs):
    return root_work_items(
        payload={'measurement_ref': 'measurement-current', 'variant_run_ref': 'run-primary',
                 'evaluation_attempt_ref': 'attempt-primary', 'metric_result_ref': 'metric-primary'},
        identities={'variant_ref': 'variant-1', 'evaluation_ref': 'evaluation-1'},
        result_document={'metrics': {'quality': 1.0}, 'formal_runs': runs},
    )


def test_actual_evaluation_is_primary_even_when_unassessed_run_was_declared_first():
    items = _work_items([
        {'run_key': 'ran-first', 'evaluations': [{'status': 'blocked'}]},
        {'run_key': 'measured-later', 'evaluations': [{'attempt_key': 'actual', 'metrics': {'quality': 1.0}}]},
        {'run_key': 'ran-without-evaluation', 'evaluations': []},
        {'run_key': 'never-ran', 'status': 'not_executed'},
    ])
    assert [item['run_key'] for item in items] == ['measured-later', 'ran-first', 'ran-without-evaluation']
    assert [item['ordinal'] for item in items] == [0, 1, 2]
    assert items[0]['variant_run_ref'] == 'run-primary'
    assert len({item['variant_run_ref'] for item in items}) == 3
    for item, inventory in zip(items[1:], formal_inventory(items)[1:]):
        assert item['evaluation_attempt_ref'] is item['metric_result_ref'] is item['attempt_key'] is None
        assert item['metrics'] is inventory['metrics_hash'] is None


@pytest.mark.parametrize('evaluations', [[], [{'status': 'blocked'}]])
def test_inventory_without_any_actual_evaluation_can_complete_real_work(evaluations):
    items = _work_items([{'run_key': 'executed', 'evaluations': evaluations}])
    assert items[0]['variant_run_ref'] == 'run-primary'
    assert items[0]['evaluation_attempt_ref'] is items[0]['metric_result_ref'] is None


def test_inventory_without_any_actual_execution_cannot_claim_execution():
    with pytest.raises(OwnerConflict, match='target_formal_work_has_no_executed_result'):
        _work_items([{'run_key': 'never-ran', 'status': 'cancelled'}])


def _with_unassessed_runs(workspace):
    path = workspace / 'outputs/metrics.json'
    document = json.loads(path.read_text())
    document['formal_runs'] = [
        {'run_key': 'analysis-executed', 'checkpoint_paths': [], 'evaluations': [{'attempt_key': 'blocked', 'status': 'blocked'}]},
        {'run_key': 'audit-measured', 'checkpoint_paths': ['outputs/final.ckpt'] if (workspace / 'outputs/final.ckpt').exists() else [],
         'evaluations': [{'attempt_key': 'verified', 'metrics': document['metrics']}]},
        {'run_key': 'proof-produced', 'checkpoint_paths': [], 'evaluations': []},
        {'run_key': 'cancelled-before-execution', 'status': 'cancelled'},
    ]
    path.write_text(canonical_json(document))


def test_completed_target_preserves_actual_runs_with_no_evaluation_and_replays(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _with_unassessed_runs(workspace)
        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert [fact['run_key'] for fact in facts] == ['audit-measured', 'analysis-executed', 'proof-produced']
        assert facts[0]['metric_result']['metrics']
        for fact in facts[1:]:
            assert fact['variant_run']['status'] == 'executed'
            assert fact['variant_run']['inputs']['run_key'] == fact['run_key']
            assert fact['evaluation_attempt'] is fact['metric_result'] is None
            assert fact['evaluation_attempt_ref'] is fact['metric_result_ref'] is fact['attempt_key'] is None
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_variant_runs').scalar_one() == 3
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_evaluation_attempts').scalar_one() == 1
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_metric_results').scalar_one() == 1
            counts = connection.exec_driver_sql('SELECT variant_run_count,evaluation_attempt_count,formal_measurement_count FROM research_graph_state').one()
            assert tuple(counts) == (3, 1, 1)
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
        replay, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts

        # Database existence constraints cannot permit half of an evaluation.
        with pytest.raises(IntegrityError):
            with runtime._database.write() as connection:
                connection.execute(text('UPDATE rg_target_root_formal_entities SET metric_result_ref=:metric WHERE link_ref=:link'),
                                   {'metric': facts[0]['metric_result_ref'], 'link': facts[1]['link_ref']})
    finally:
        runtime.close()


def test_unassessed_run_still_requires_a_real_variant_and_rolls_back_all_entities(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _with_unassessed_runs(workspace)
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document['formal_runs'][0]['variant_ref'] = 'variant-does-not-exist'
        path.write_text(canonical_json(document))
        with pytest.raises(OwnerConflict, match='target_formal_variant_definition_invalid'):
            _accept(runtime, lifecycle, memory, handle, evidence)
        with runtime._database.read() as connection:
            for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_root_measurements', 'rg_target_commits'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()


def test_target_with_only_blocked_evaluation_commits_work_without_formal_result(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document['formal_runs'] = [{'run_key': 'executed', 'evaluations': [{'status': 'blocked'}]}]
        document['metrics'] = {}
        path.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert accepted.target_commit_ref
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert facts[0]['evaluation_attempt'] is facts[0]['metric_result'] is None
        assert facts[0]['run_artifacts'][0]['role'] in {'checkpoint_artifact', 'log_asset'}
        closure = runtime.owners.research_graph.query_target_root_commit_transition(handle.target_ref).canonical_terminal
        assert closure.formal_measurement_accepted is False and closure.metric_values == ()
        replay, replay_manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted and replay_manifest == manifest
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_variant_runs').scalar_one() == 1
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_target_commits').scalar_one() == 1
            for table in ('rg_evaluation_attempts', 'rg_metric_results', 'rg_target_root_unassessed_runs'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()
