"""Formal outputs belong to their declared run and evaluation."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text

from meta_research.formal_entities import _register_run_checkpoints
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _with_checkpoint(workspace, evidence):
    if not any(artifact.relative_path == 'outputs/final.ckpt' for artifact in evidence.handoff.artifacts):
        (workspace / 'outputs/final.ckpt').write_bytes(b'actual-first-state')
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=evidence.handoff.artifacts + (
            TargetCompletionArtifact(role='checkpoint', relative_path='outputs/final.ckpt'),)))
    return evidence


def _document(workspace, runs):
    path = workspace / 'outputs/metrics.json'
    document = json.loads(path.read_text())
    document['formal_runs'] = [dict(run, evaluations=[dict(evaluation, metrics=document['metrics'])
        for evaluation in run['evaluations']]) for run in runs]
    path.write_text(canonical_json(document))


def test_two_actual_runs_have_distinct_checkpoint_roles_and_evaluation_subsets(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _with_checkpoint(workspace, evidence)
        (workspace / 'outputs/other.ckpt').write_bytes(b'actual-second-state')
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=evidence.handoff.artifacts + (
            TargetCompletionArtifact(role='checkpoint', relative_path='outputs/other.ckpt'),)))
        _document(workspace, [
            {'run_key': 'first', 'checkpoint_paths': ['outputs/final.ckpt'], 'evaluations': [
                {'attempt_key': 'state'}, {'attempt_key': 'stateless', 'checkpoint_paths': []}]},
            {'run_key': 'second', 'checkpoint_paths': ['outputs/other.ckpt'], 'evaluations': [{'attempt_key': 'state'}]},
        ])
        _accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        with runtime._database.read() as connection:
            roles = connection.execute(text("SELECT subject_ref,version_ref,role_ref FROM rg_experiment_asset_roles WHERE role='checkpoint_artifact'")).mappings().all()
            entries = json.loads(connection.execute(text('SELECT entries_json FROM rm_target_root_completion_manifests WHERE manifest_ref=:ref'), {'ref': manifest.manifest_ref}).scalar_one())
            versions = {entry['declared_relative_path']: entry['binding']['version_ref'] for entry in entries}
            assert {(row['subject_ref'], row['version_ref']) for row in roles} == {
                (facts[0]['variant_run_ref'], versions['outputs/final.ckpt']),
                (facts[2]['variant_run_ref'], versions['outputs/other.ckpt'])}
            for fact, expected in zip(facts, (1, 0, 1)):
                count = connection.execute(text('SELECT count(*) FROM rg_evaluation_attempt_checkpoints WHERE evaluation_attempt_ref=:ref'), {'ref': fact['evaluation_attempt_ref']}).scalar_one()
                assert count == expected
    finally:
        runtime.close()


@pytest.mark.parametrize('runs,error', [
    ([{'run_key': 'one', 'evaluations': [{'attempt_key': 'done'}]},
      {'run_key': 'two', 'evaluations': [{'attempt_key': 'done'}]}], 'checkpoint_assignment_required'),
    ([{'run_key': 'one', 'checkpoint_paths': [], 'evaluations': [
        {'attempt_key': 'done', 'checkpoint_paths': ['outputs/final.ckpt']}]}], 'checkpoint_path_not_bound'),
])
def test_ambiguous_or_cross_run_checkpoint_claim_rolls_back(tmp_path, runs, error):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _with_checkpoint(workspace, evidence)
        _document(workspace, runs)
        from meta_research.target_run_finalizer import TargetRootOwnerRejection
        rejected, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert isinstance(rejected, TargetRootOwnerRejection)
        assert rejected.code == 'target_root_commit_domain_invalid'
        assert 'outputs/final.ckpt' in rejected.feedback
        assert memory.query(manifest.manifest_ref) == manifest
        with runtime._database.read() as connection:
            for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_commits'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()


def test_reused_run_reads_original_roles_without_claiming_current_completion_outputs(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _with_checkpoint(workspace, evidence)
        _accept(runtime, lifecycle, memory, handle, evidence)
        fact = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)[0]
        def unexpected_write(*args, **kwargs):
            raise AssertionError('Reusing a run cannot create new output roles')
        with runtime._database.read() as connection:
            item = {'variant_run_ref': fact['variant_run_ref'], 'reuse_variant_run': True,
                    'checkpoint_paths': ['outputs/final.ckpt']}
            records = _register_run_checkpoints(connection, ensure=unexpected_write, item=item,
                entries=[{'declared_relative_path': 'outputs/unrelated-new.ckpt'}], accepted_at='unused')
            assert len(records) == 1
            assert records[0]['declared_relative_path'] == 'outputs/final.ckpt'
            item['checkpoint_paths'] = ['outputs/unrelated-new.ckpt']
            with pytest.raises(OwnerConflict, match='checkpoint_path_not_bound'):
                _register_run_checkpoints(connection, ensure=unexpected_write, item=item, entries=[], accepted_at='unused')
    finally:
        runtime.close()


def test_same_reused_run_under_two_work_keys_selects_each_declared_checkpoint_subset(tmp_path):
    from meta_research.formal_entities import register_root_entities, root_work_items
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _with_checkpoint(workspace, evidence)
        (workspace / 'outputs/other.ckpt').write_bytes(b'actual-second-state')
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=evidence.handoff.artifacts + (
            TargetCompletionArtifact(role='checkpoint', relative_path='outputs/other.ckpt'),)))
        _accept(runtime, lifecycle, memory, handle, evidence)
        # Exercise the transaction writer with two new assessments of the same
        # real run. The first existing fact retains its immutable anchor.
        with runtime._database.write() as connection:
            def row(table, key, ref):
                return dict(connection.execute(text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': ref}).mappings().one())
            root = row('rg_target_root_measurements', 'target_ref', handle.target_ref)
            authority = row('rg_target_measurement_domain_authorities', 'authority_ref', root['authority_ref'])
            manifest = row('rm_target_root_completion_manifests', 'manifest_ref', root['manifest_ref'])
            completion = row('ar_target_root_completions', 'completion_ref', root['completion_ref'])
            commit = row('rg_target_commits', 'target_ref', handle.target_ref)
            result = json.loads(manifest['result_document_json'])
            result['formal_runs'] = [
                {'run_key': 'primary', 'variant_run_ref': root['variant_run_ref'], 'evaluations': [
                    {'attempt_key': 'primary', 'evaluation_attempt_ref': root['evaluation_attempt_ref'],
                     'metric_result_ref': root['metric_result_ref'], 'metrics': result['metrics']}]},
                {'run_key': 'first-subset', 'variant_run_ref': root['variant_run_ref'],
                 'checkpoint_paths': ['outputs/final.ckpt'], 'evaluations': [{'attempt_key': 'new', 'metrics': result['metrics']}]},
                {'run_key': 'second-subset', 'variant_run_ref': root['variant_run_ref'],
                 'checkpoint_paths': ['outputs/other.ckpt'], 'evaluations': [{'attempt_key': 'new', 'metrics': result['metrics']}]},
            ]
            items = root_work_items(payload=json.loads(root['measurement_payload_json']), identities=authority, result_document=result)
            register_root_entities(connection, root=root, authority=authority, manifest=manifest,
                                   completion=completion, commit_ref=commit['commit_ref'], work_items=items)
            checkpoints = []
            for item in items[1:]:
                checkpoints.append(connection.execute(text('SELECT r.version_ref FROM rg_evaluation_attempt_checkpoints a '
                    'JOIN rg_experiment_asset_roles r ON r.role_ref=a.checkpoint_role_ref WHERE a.evaluation_attempt_ref=:ref'),
                    {'ref': item['evaluation_attempt_ref']}).scalars().all())
            entries = json.loads(manifest['entries_json'])
            versions = {entry['declared_relative_path']: entry['binding']['version_ref'] for entry in entries}
            assert checkpoints == [[versions['outputs/final.ckpt']], [versions['outputs/other.ckpt']]]
            assert connection.execute(text('SELECT count(*) FROM rg_variant_runs')).scalar_one() == 1
            assert connection.execute(text('SELECT count(*) FROM rg_evaluation_attempts')).scalar_one() == 3
    finally:
        runtime.close()
