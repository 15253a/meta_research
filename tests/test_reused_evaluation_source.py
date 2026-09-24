"""Reusing an evaluation must retain its original verified result provenance."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.formal_entities import register_root_entities, root_work_items, _verify_reused_evaluation_source
from meta_research.owners.common import OwnerConflict
from test_root_checkpoint_assignment import _with_checkpoint
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _source_packet(runtime, target_ref):
    with runtime._database.read() as connection:
        def row(table, key, ref):
            return dict(connection.execute(text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': ref}).mappings().one())
        root = row('rg_target_root_measurements', 'target_ref', target_ref)
        authority = row('rg_target_measurement_domain_authorities', 'authority_ref', root['authority_ref'])
        manifest = row('rm_target_root_completion_manifests', 'manifest_ref', root['manifest_ref'])
        completion = row('ar_target_root_completions', 'completion_ref', root['completion_ref'])
        commit = row('rg_target_commits', 'target_ref', target_ref)
    items = root_work_items(payload=json.loads(root['measurement_payload_json']), identities=authority,
                           result_document=json.loads(manifest['result_document_json']))
    for item in items:
        item['reuse_variant_run'] = True
        item['reuse_evaluation_attempt'] = True
    return dict(root=root, authority=authority, manifest=manifest, completion=completion,
                commit_ref=commit['commit_ref'], work_items=items)


def test_original_evaluation_can_be_reused_and_reverified_without_new_facts(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _accept(runtime, lifecycle, memory, handle, _with_checkpoint(workspace, evidence))
        packet = _source_packet(runtime, handle.target_ref)
        with runtime._database.read() as connection:
            items = register_root_entities(connection, **packet, source_owner=runtime.owners.research_graph, verify_only=True)
            assert register_root_entities(connection, **packet, source_owner=runtime.owners.research_graph, verify_only=True) == items
            # The migration-only path reconstructs the exact original AR/RM
            # receipts and all native rows without requiring a running service.
            assert register_root_entities(connection, **packet, verify_only=True) == items
            for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_commits'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('corruption', ['result_role', 'metric_receipt', 'source_manifest'])
def test_reuse_rejects_a_valid_but_wrong_result_role_or_changed_original_source(tmp_path, corruption):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _accept(runtime, lifecycle, memory, handle, _with_checkpoint(workspace, evidence))
        packet = _source_packet(runtime, handle.target_ref)
        with runtime._database.write() as connection:
            if corruption == 'result_role':
                wrong = connection.execute(text("SELECT role_ref FROM rg_experiment_asset_roles WHERE role='checkpoint_artifact'")).scalar_one()
                connection.execute(text('UPDATE rg_metric_results SET result_role_ref=:ref'), {'ref': wrong})
                assert connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
            elif corruption == 'metric_receipt':
                connection.execute(text('UPDATE rg_metric_results SET receipt_hash=:hash'), {'hash': '0' * 64})
            else:
                connection.execute(text("UPDATE rm_target_root_completion_manifests SET result_document_json='{}'"))
        with pytest.raises(OwnerConflict):
            with runtime._database.read() as connection:
                register_root_entities(connection, **packet, source_owner=runtime.owners.research_graph, verify_only=True)
    finally:
        runtime.close()


def test_legacy_native_result_uses_its_existing_formal_measurement_verifier():
    calls = []
    def original_query(ref):
        calls.append(ref)
        return SimpleNamespace(metric_result_ref='native-metric')
    owner = SimpleNamespace(query_target_formal_metric_result=original_query)
    binding = SimpleNamespace(inputs={'schema_ref': 'legacy-native-inputs'})
    _verify_reused_evaluation_source(None, attempt_ref='native-attempt', metric_ref='native-metric', binding=binding, source_owner=owner)
    assert calls == ['native-attempt']
    with pytest.raises(OwnerConflict, match='source_invalid'):
        _verify_reused_evaluation_source(None, attempt_ref='native-attempt', metric_ref='other-metric', binding=binding, source_owner=owner)


def test_recursive_source_verification_fails_explicitly():
    binding = SimpleNamespace(inputs={'schema_ref': 'legacy-native-inputs'})
    def recursive_query(ref):
        return _verify_reused_evaluation_source(None, attempt_ref=ref, metric_ref='metric', binding=binding, source_owner=owner)
    owner = SimpleNamespace(query_target_formal_metric_result=recursive_query)
    with pytest.raises(OwnerConflict, match='source_cycle'):
        _verify_reused_evaluation_source(None, attempt_ref='attempt', metric_ref='metric', binding=binding, source_owner=owner)
