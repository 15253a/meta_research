from __future__ import annotations

import json
import pytest
from sqlalchemy import text
from meta_research.owners.common import OwnerConflict, canonical_json

from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_root_acceptance_registers_queryable_formal_entities_and_replays(tmp_path):
    runtime, lifecycle, memory, _, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        seeded = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        manifest = memory.query(seeded.manifest_ref)
        graph = runtime.owners.research_graph
        kwargs = dict(completion=completion, manifest=manifest,
                      result_document=manifest.result_document, idempotency_key='formal-root')
        accepted = graph.accept_target_commit_from_root_completion(**kwargs)
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        fact = facts[0]
        assert fact['variant_run']['status'] == 'executed'
        assert fact['evaluation_attempt']['variant_run_ref'] == fact['variant_run']['variant_run_ref']
        assert fact['metric_result']['metrics'] == manifest.result_document.metrics
        assert fact['target_commit_ref'] == accepted.target_commit_ref
        assert graph.accept_target_commit_from_root_completion(**kwargs) == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


def _accept(runtime, lifecycle, memory, handle, evidence):
    seeded = TargetRunFinalizer(
        lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
        measurement_authority=runtime.owners.research_graph,
    ).finalize(handle=handle, evidence=evidence)
    completion = lifecycle.query_completion(handle.target_ref)
    manifest = memory.query(seeded.manifest_ref)
    return runtime.owners.research_graph.accept_target_commit_from_root_completion(
        completion=completion, manifest=manifest, result_document=manifest.result_document,
        idempotency_key='formal-root'), manifest


def test_multiple_actual_runs_and_assessments_are_not_collapsed(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        first = document['metrics']
        second = {key: 7.0 for key in first}
        checkpoints = [artifact.relative_path for artifact in evidence.handoff.artifacts
                       if artifact.role == 'checkpoint']
        document['formal_runs'] = [
            {'run_key': 'audit-a', 'checkpoint_paths': checkpoints, 'evaluations': [
                {'attempt_key': 'criterion-a', 'metrics': first},
                {'attempt_key': 'criterion-b', 'metrics': second}]},
            {'run_key': 'audit-b', 'checkpoint_paths': checkpoints, 'evaluations': [{'attempt_key': 'criterion-a', 'metrics': second}]},
            {'run_key': 'debug-cancelled', 'status': 'cancelled'},
            {'run_key': 'not-started', 'status': 'not_executed'},
        ]
        path.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 3
        assert len({fact['variant_run_ref'] for fact in facts}) == 2
        assert len({fact['evaluation_attempt_ref'] for fact in facts}) == 3
        assert [fact['metric_result']['metrics'] for fact in facts] == [first, second, second]
        commit = graph.query_target_commits(graph.query_target_measurement_domain_authority(handle.target_ref).graph_ref)[0]
        assert commit.closure['schema_ref'].endswith('/v2')
        assert 'result_document' not in commit.closure['root_measurement']
        assert commit.closure['result_content']['asset']['structured_content_hash'] == manifest.result_document_hash
        assert graph.query_target_root_commit_transition(handle.target_ref).target_commit_ref == accepted.target_commit_ref
    finally:
        runtime.close()


def test_invalid_extra_evaluation_rolls_back_entire_acceptance(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document['formal_runs'] = [{'run_key': 'actual', 'evaluations': [
            {'attempt_key': 'valid', 'metrics': document['metrics']},
            {'attempt_key': 'invalid', 'evaluation_ref': 'does-not-exist', 'metrics': document['metrics']}]}]
        path.write_text(canonical_json(document))
        with pytest.raises(OwnerConflict, match='evaluation_definition_invalid'):
            _accept(runtime, lifecycle, memory, handle, evidence)
        with runtime._database.read() as connection:
            for table in ('rg_target_commits', 'rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_root_measurements'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()


def test_native_metric_tampering_cannot_be_hidden_by_unchanged_closure(tmp_path):
    runtime, lifecycle, memory, _, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence)
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_metric_results SET metrics_json = '{}'"))
        with pytest.raises(OwnerConflict, match='entity_integrity_invalid'):
            runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
    finally:
        runtime.close()


def test_explicit_unexecuted_completion_preserves_assets_without_fabricating_results(tmp_path):
    from meta_research.target_run_finalizer import TargetRootOwnerRejection
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document['execution_hierarchy'] = {
            'variant_run': {'status': 'not_instantiated'},
            'evaluation_attempt': {'status': 'not_started'},
        }
        document['input_admission'] = {
            'variant_run_count': 0, 'evaluation_attempt_count': 0,
            'training_process_started': False, 'evaluation_process_started': False,
        }
        path.write_text(canonical_json(document))
        rejected, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert isinstance(rejected, TargetRootOwnerRejection)
        assert 'no VariantRun or EvaluationAttempt started' in rejected.feedback
        assert memory.query(manifest.manifest_ref) == manifest
        with runtime._database.read() as connection:
            for table in ('rg_target_commits', 'rg_variant_runs', 'rg_evaluation_attempts',
                          'rg_metric_results', 'rg_target_root_measurements'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()
