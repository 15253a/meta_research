"""Legacy handoff acceptance must not manufacture execution under a method."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import text

from meta_research import formal_entities
from meta_research.owners import research_graph as graph_module
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _unexecuted():
    return {
        'execution_hierarchy': {'variant_run': {'status': 'not_instantiated'},
                                'evaluation_attempt': {'status': 'not_started'}},
        'input_admission': {'variant_run_count': 0, 'evaluation_attempt_count': 0,
                            'training_process_started': False, 'evaluation_process_started': False},
        'metrics': {'benchmark_protocol_completion_status': 'not_runnable'},
    }


def test_non_execution_requires_all_frozen_declarations_not_a_metric_label():
    document = _unexecuted()
    evidence = formal_entities.explicit_unexecuted_root_evidence(document)
    assert evidence is not None and 'metrics' not in evidence
    assert formal_entities.explicit_unexecuted_root_evidence({'metrics': document['metrics']}) is None
    for section, key, value in (
        ('input_admission', 'variant_run_count', 1),
        ('input_admission', 'evaluation_attempt_count', False),
        ('input_admission', 'training_process_started', True),
        ('input_admission', 'evaluation_process_started', 0),
        ('execution_hierarchy', 'variant_run', {'status': 'executed'}),
        ('execution_hierarchy', 'evaluation_attempt', {'status': 'executed'}),
    ):
        changed = deepcopy(document)
        changed[section][key] = value
        assert formal_entities.explicit_unexecuted_root_evidence(changed) is None


def test_legacy_unexecuted_handoff_keeps_original_anchors_without_formal_results(tmp_path, monkeypatch):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document.update({key: value for key, value in _unexecuted().items() if key != 'metrics'})
        path.write_text(canonical_json(document))
        # Reproduce the historical v1 acceptance behavior using actual AR/RM
        # issuers, but without pretending that the legacy anchors were entities.
        material = graph_module._target_root_commit_material
        def legacy_material(**values):
            return material(**{**values, 'compact': False})
        with monkeypatch.context() as legacy:
            legacy.setattr(graph_module, '_target_root_commit_material', legacy_material)
            legacy.setattr(formal_entities, 'explicit_unexecuted_root_evidence', lambda _: None)
            legacy.setattr(formal_entities, 'register_root_entities', lambda *args, **kwargs: [])
            _accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        with runtime._database.read() as connection:
            def read(table, key, ref):
                return dict(connection.execute(text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': ref}).mappings().one())
            root = read('rg_target_root_measurements', 'target_ref', handle.target_ref)
            commit = read('rg_target_commits', 'target_ref', handle.target_ref)
            authority = read('rg_target_measurement_domain_authorities', 'authority_ref', root['authority_ref'])
            manifest = read('rm_target_root_completion_manifests', 'manifest_ref', root['manifest_ref'])
            completion = read('ar_target_root_completions', 'completion_ref', root['completion_ref'])
        old_root = {key: value for key, value in root.items() if not key.startswith(('formal_', 'execution_registration_'))}
        old_commit = {key: value for key, value in commit.items() if key != 'formal_evaluation_attempt_ref'}
        kwargs = dict(root=root, authority=authority, manifest=manifest, completion=completion, commit_ref=commit['commit_ref'])
        with runtime._database.write() as connection:
            assert formal_entities.register_root_entities(connection, **kwargs) == []
        with runtime._database.read() as connection:
            current = dict(connection.execute(text('SELECT * FROM rg_target_root_measurements WHERE target_ref=:ref'), {'ref': handle.target_ref}).mappings().one())
            current_commit = dict(connection.execute(text('SELECT * FROM rg_target_commits WHERE target_ref=:ref'), {'ref': handle.target_ref}).mappings().one())
            assert {key: current[key] for key in old_root} == old_root
            assert {key: current_commit[key] for key in old_commit} == old_commit
            registration = json.loads(current['execution_registration_json'])
            assert canonical_hash(registration) == current['execution_registration_hash']
            assert registration['execution_status'] == 'not_executed'
            assert registration['legacy_anchor_refs']['variant_run_ref'] == root['variant_run_ref']
            assert registration['formal_primary_refs'] == dict.fromkeys(('variant_run_ref', 'evaluation_attempt_ref', 'metric_result_ref'))
            assert registration['result_document_hash'] == manifest['result_document_hash']
            for key in ('formal_variant_run_ref', 'formal_evaluation_attempt_ref', 'formal_metric_result_ref'):
                assert current[key] is None
            assert current_commit['formal_evaluation_attempt_ref'] is None
            for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_root_formal_entities'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
            assert formal_entities.register_root_entities(connection, **kwargs, verify_only=True) == []
            assert formal_entities.query_target_formal_results(connection, handle.target_ref) == ()
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_target_root_measurements SET execution_registration_hash=:hash"), {'hash': '0' * 64})
        with pytest.raises(OwnerConflict, match='execution_registration_invalid'):
            with runtime._database.read() as connection:
                formal_entities.register_root_entities(connection, **kwargs, verify_only=True)
    finally:
        runtime.close()
