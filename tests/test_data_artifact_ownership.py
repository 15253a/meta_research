"""Retained data follows its actual producer without forcing Dataset registration."""
import json
import sqlite3
from dataclasses import replace

import pytest

from meta_research.migration import upgrade_database
from meta_research.owners.common import canonical_json
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from meta_research.target_run_finalizer import TargetRootOwnerRejection
from test_migration_recovery import _upgrade_to_revision
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.parametrize('multiple,explicit', [(False, False), (True, False), (True, True)])
def test_data_products_are_defaulted_only_for_one_actual_producer(tmp_path, multiple, explicit):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/data/observations.txt'
        path.parent.mkdir(parents=True)
        path.write_text('Original collected observation', encoding='utf-8')
        relative = 'outputs/data/observations.txt'
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts, TargetCompletionArtifact(role='data', relative_path=relative))))
        document_path = workspace / 'outputs/metrics.json'
        document = json.loads(document_path.read_text())
        checkpoints = [item.relative_path for item in evidence.handoff.artifacts if item.role == 'checkpoint']
        document['formal_runs'] = [{'run_key': 'collection', 'checkpoint_paths': checkpoints,
                                   'evaluations': [{'attempt_key': 'assessment', 'metrics': document['metrics']}]}]
        if multiple:
            document['formal_runs'].append({'run_key': 'other-work', 'checkpoint_paths': [], 'evaluations': []})
        if explicit:
            document['formal_runs'][0]['artifact_paths'] = [relative]
        document_path.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        data = next(entry for entry in manifest.entries if entry.role == 'data')
        assert runtime.owners.research_memory.materialize_asset(data.binding.version_ref).content == path.read_bytes()
        if multiple and not explicit:
            assert isinstance(accepted, TargetRootOwnerRejection)
            assert accepted.code == 'target_root_commit_domain_invalid'
            assert relative in accepted.feedback and 'actual Run or Evaluation owner' in accepted.feedback
            assert facts == ()
            with runtime._database.read() as connection:
                for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_commits'):
                    assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
            assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
            return
        assert accepted.target_commit_ref
        data_roles = [item for item in facts[0]['run_artifacts'] if item['role'] == 'data_asset']
        assert [item['version_ref'] for item in data_roles] == (
            [data.binding.version_ref] if not multiple or explicit else [])
        assert not any(item['role'] == 'data_asset' for fact in facts for item in fact['evaluation_artifacts'])
        if multiple:
            assert not any(item['role'] == 'data_asset' for item in facts[1]['run_artifacts'])
        assert runtime.owners.research_graph.query_datasets()['items'] == []
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
    finally:
        runtime.close()


def test_data_role_migration_preserves_existing_roles_and_constraints(tmp_path):
    database = tmp_path / 'roles.sqlite3'
    _upgrade_to_revision(database, '0051_dataset_derivations')
    values = ('old-role', 'variant_run', 'old-run', 'log_asset', 0, 'old-asset', 'old-version',
              'a' * 64, 'b' * 64, 'asset-receipt', 'c' * 64, 'role-receipt', 'd' * 64, 1.0)
    insert = 'INSERT INTO rg_experiment_asset_roles VALUES (' + ','.join(['?'] * len(values)) + ')'
    with sqlite3.connect(database) as connection:
        connection.execute(insert, values)
        before = connection.execute('SELECT * FROM rg_experiment_asset_roles').fetchall()
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT * FROM rg_experiment_asset_roles').fetchall() == before
        data = list(values)
        data[0], data[3], data[11] = 'new-role', 'data_asset', 'new-receipt'
        connection.execute(insert, data)
        invalid = data.copy()
        invalid[0], invalid[1], invalid[11] = 'invalid-role', 'quest', 'invalid-receipt'
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, invalid)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
