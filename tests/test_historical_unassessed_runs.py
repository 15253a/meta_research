"""Historical run-only rows retain original issuer and receipt checks."""
import json

import pytest
from sqlalchemy import event, text

from meta_research.formal_entities import _ensure_input, _ref, _register_run_checkpoints
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.run_only_registration import _source_inputs
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_run_only_registration import _declare
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _historical_run(runtime, lifecycle, memory, handle, workspace, evidence):
    # Preserve real AR completion, RM bytes/manifest/receipts and RG authority.
    # Only the discontinued registration rows are seeded in their original format.
    path, document = _declare(workspace, evidence)
    checkpoint = 'outputs/historical.ckpt'
    (workspace / checkpoint).write_bytes(b'actual historical checkpoint')
    document['retained_artifacts'].append({'role': 'checkpoint', 'relative_path': checkpoint})
    document['formal_runs'] = document['formal_runs'][:1]
    document['formal_runs'][0]['checkpoint_paths'] = [checkpoint]
    document['metrics'] = {}
    path.write_text(canonical_json(document))
    graph = runtime.owners.research_graph
    finalized = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence), measurement_authority=graph,
    ).finalize(handle=handle, evidence=evidence)
    assert finalized.status == 'rm_accepted'
    completion = lifecycle.query_completion(handle.target_ref)
    manifest = memory.query_for_completion(completion.completion_ref)
    target, authority, _, _ = graph._target_root_domain_context(
        completion=completion, manifest=manifest, result_document=manifest.result_document)
    run = document['formal_runs'][0]
    run_ref = _ref('variant_run', completion.completion_ref, run['run_key'])
    binding_ref = _ref('variant_input', run_ref)
    inputs = _source_inputs(completion, manifest, target, authority, run,
                           source_owner=graph, legacy_input_selection=True)
    assert 'input_selection_recorded' not in inputs
    with runtime._database.write() as connection:
        def insert(table, _key, values):
            connection.execute(text('INSERT INTO ' + table + ' (' + ','.join(values)
                + ') VALUES (' + ','.join(':' + key for key in values) + ')'), values)
        _ensure_input(insert, binding_ref, 'variant_run', run_ref, inputs, completion.accepted_at)
        insert('rg_variant_runs', 'variant_run_ref', {
            'variant_run_ref': run_ref, 'variant_ref': authority.identities.variant_ref,
            'input_binding_ref': binding_ref,
            'implementation_revision_ref': inputs['implementation_revision_ref'],
            'status': 'executed', 'created_at': completion.accepted_at,
            'updated_at': completion.accepted_at})
        checkpoints = _register_run_checkpoints(connection, ensure=insert,
            item={'variant_run_ref': run_ref, 'reuse_variant_run': False,
                  'checkpoint_paths': run['checkpoint_paths']},
            entries=[entry.as_dict() for entry in manifest.entries if entry.role == 'checkpoint'],
            accepted_at=completion.accepted_at)
        insert('rg_target_root_unassessed_runs', 'association_ref', {
            'association_ref': _ref('root_unassessed_run', completion.completion_ref, run['run_key']),
            'target_ref': target.target_ref, 'completion_ref': completion.completion_ref,
            'manifest_ref': manifest.manifest_ref, 'authority_ref': authority.authority_ref,
            'run_key': run['run_key'], 'variant_run_ref': run_ref,
            'input_binding_ref': binding_ref, 'input_binding_hash': canonical_hash(inputs),
            'checkpoint_roles_json': canonical_json(checkpoints),
            'checkpoint_roles_hash': canonical_hash(checkpoints)})
    return run_ref, binding_ref, manifest


def test_public_history_reads_real_unassessed_rows_without_writing(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        run_ref, binding_ref, manifest = _historical_run(
            runtime, lifecycle, memory, handle, workspace, evidence)
        writes = []
        def record(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(statement)
        event.listen(runtime._database._engine, 'before_cursor_execute', record)
        try:
            graph = runtime.owners.research_graph
            first = graph.query_target_formal_results(handle.target_ref)
            assert first == graph.query_target_formal_results(handle.target_ref)
        finally:
            event.remove(runtime._database._engine, 'before_cursor_execute', record)
        assert writes == []
        assert len(first) == 1 and first[0]['variant_run_ref'] == run_ref
        assert first[0]['variant_run']['input_binding_ref'] == binding_ref
        assert first[0]['manifest_ref'] == manifest.manifest_ref
        assert first[0]['checkpoint_roles']
        assert first[0]['evaluation_attempt'] is first[0]['metric_result'] is first[0]['target_commit_ref'] is None
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_target_root_unassessed_runs').scalar_one() == 1
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    finally:
        runtime.close()


@pytest.mark.parametrize('problem', ['binding_hash', 'checkpoint_hash', 'run_key', 'binding_source'])
def test_public_history_rejects_tampered_unassessed_source(tmp_path, problem):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _, binding_ref, _ = _historical_run(runtime, lifecycle, memory, handle, workspace, evidence)
        graph = runtime.owners.research_graph
        assert len(graph.query_target_formal_results(handle.target_ref)) == 1
        with runtime._database.write() as connection:
            if problem == 'binding_hash':
                connection.exec_driver_sql("UPDATE rg_target_root_unassessed_runs SET input_binding_hash='0000000000000000000000000000000000000000000000000000000000000000'")
            elif problem == 'checkpoint_hash':
                connection.exec_driver_sql("UPDATE rg_target_root_unassessed_runs SET checkpoint_roles_hash='0000000000000000000000000000000000000000000000000000000000000000'")
            elif problem == 'run_key':
                connection.exec_driver_sql("UPDATE rg_target_root_unassessed_runs SET run_key='wrong-original-run'")
            else:
                row = connection.execute(text('SELECT * FROM rg_experiment_input_bindings WHERE binding_ref=:ref'),
                                         {'ref': binding_ref}).mappings().one()
                inputs = json.loads(row['inputs_json'])
                inputs['completion_payload_hash'] = '0' * 64
                # Recompute internally consistent hashes/receipt: original issuer check must still fail.
                def rewrite(table, key, values):
                    connection.execute(text('UPDATE ' + table + ' SET ' + ','.join(k + '=:' + k for k in values)
                                            + ' WHERE ' + key + '=:' + key), values)
                _ensure_input(rewrite, binding_ref, row['subject_kind'], row['subject_ref'], inputs, row['accepted_at'])
                connection.execute(text('UPDATE rg_target_root_unassessed_runs SET input_binding_hash=:hash'),
                                   {'hash': canonical_hash(inputs)})
        with pytest.raises(OwnerConflict, match='unassessed_execution_integrity_invalid'):
            graph.query_target_formal_results(handle.target_ref)
    finally:
        runtime.close()
