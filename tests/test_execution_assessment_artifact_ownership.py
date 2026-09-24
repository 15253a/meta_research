"""Execution output and assessment output keep distinct exact RM provenance."""
import json
import os
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import _subject_artifact_paths
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _prepare(workspace, evidence):
    paths = [('log', 'logs/evaluate.log'), ('analysis', 'outputs/observations.json'),
             ('analysis', 'outputs/evaluation-report.md')]
    for role, path in paths:
        (workspace / path).write_text('Actual ' + path, encoding='utf-8')
    evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
        *evidence.handoff.artifacts,
        *(TargetCompletionArtifact(role=role, relative_path=path) for role, path in paths))))
    result = workspace / 'outputs/metrics.json'
    document = json.loads(result.read_text())
    document['formal_runs'] = [{'run_key': 'observed',
        'artifact_paths': ['logs/train.log', 'outputs/observations.json'],
        'evaluations': [{'attempt_key': 'assessed', 'metrics': document['metrics'],
            'artifact_paths': ['logs/evaluate.log', 'outputs/evaluation-report.md']}]}]
    # Each attempt owns its metrics. The Target summary need not copy them.
    document['metrics'] = {}
    result.write_text(canonical_json(document))
    return evidence


def test_exact_execution_and_assessment_artifacts_survive_acceptance_and_replay(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _prepare(workspace, evidence)
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        by_path = {entry.declared_relative_path: entry.binding.version_ref for entry in manifest.entries}
        run_versions = {item['version_ref'] for item in facts[0]['run_artifacts']}
        evaluation_versions = {item['version_ref'] for item in facts[0]['evaluation_artifacts']}
        assert {by_path['logs/train.log'], by_path['outputs/observations.json']} <= run_versions
        assert {by_path['logs/evaluate.log'], by_path['outputs/evaluation-report.md'],
                by_path['outputs/metrics.json']} == evaluation_versions
        assert not run_versions & evaluation_versions
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_experiment_asset_roles SET content_hash=:hash "
                "WHERE subject_kind='variant_run' AND role='log_asset'"), {'hash': '0' * 64})
        with pytest.raises(OwnerConflict, match='integrity_invalid'):
            graph.query_target_formal_results(handle.target_ref)
    finally:
        runtime.close()


def test_unbound_subject_artifact_rolls_back_graph_facts(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _prepare(workspace, evidence)
        result = workspace / 'outputs/metrics.json'
        document = json.loads(result.read_text())
        document['formal_runs'][0]['artifact_paths'].append('not-published.log')
        result.write_text(canonical_json(document))
        rejection, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert rejection.issuer == 'research_graph'
        assert rejection.code == 'target_root_commit_domain_invalid'
        assert 'not-published.log' in rejection.feedback
        assert rejection.receipt.kind == 'target_root_completion_rejected'
        assert manifest is not None
        with runtime._database.read() as connection:
            for table in ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results', 'rg_target_commits'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()


@pytest.mark.parametrize('grouped_logs', [False, True])
def test_single_implicit_assessment_defaults_bind_only_dedicated_product_paths(tmp_path, grouped_logs):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        run_log = 'logs/execution' if grouped_logs else 'logs/train.log'
        evaluation_log = 'logs/evaluation' if grouped_logs else 'logs/eval.log'
        if grouped_logs:
            for directory in (run_log, evaluation_log):
                (workspace / directory).mkdir()
                for index in range(40):
                    (workspace / directory / f'step-{index}.log').write_text(str(index))
            evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=tuple(
                artifact for artifact in evidence.handoff.artifacts if artifact.role != 'log')))
        else:
            (workspace / evaluation_log).write_text('Actual assessment log')
        for path in ('outputs/analysis/raw/observations.json',
                     'outputs/analysis/evaluation/report.md',
                     'outputs/analysis/research-note.md', 'logs/target.log'):
            (workspace / path).parent.mkdir(parents=True, exist_ok=True)
            (workspace / path).write_text('Actual ' + path)
        paths = [('log', evaluation_log), ('analysis', 'outputs/analysis/raw'),
                 ('analysis', 'outputs/analysis/evaluation'),
                 ('analysis', 'outputs/analysis/research-note.md'), ('log', 'logs/target.log')]
        if grouped_logs:
            paths.append(('log', run_log))
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            *(TargetCompletionArtifact(role=role, relative_path=path) for role, path in paths))))
        assert 'formal_runs' not in json.loads((workspace / 'outputs/metrics.json').read_text())
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        by_path = {entry.declared_relative_path: entry.binding.version_ref for entry in manifest.entries}
        assert {asset['version_ref'] for asset in facts[0]['run_artifacts']} == {
            by_path[run_log], by_path['outputs/analysis/raw']}
        assert {asset['version_ref'] for asset in facts[0]['evaluation_artifacts']} == {
            by_path[evaluation_log], by_path['outputs/analysis/evaluation'], by_path['outputs/metrics.json']}
        assert by_path['outputs/analysis/research-note.md'] and by_path['logs/target.log']
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


@pytest.mark.parametrize('explicit_paths', [False, True])
def test_multiple_producers_keep_unassigned_logs_in_manifest_until_explicitly_assigned(tmp_path, explicit_paths):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        document_path = workspace / 'outputs/metrics.json'
        document = json.loads(document_path.read_text())
        checkpoints = [artifact.relative_path for artifact in evidence.handoff.artifacts
                       if artifact.role == 'checkpoint']
        runs, paths = [], []
        for key in ('a', 'b'):
            run_log, eval_log = f'logs/train-{key}.log', f'logs/eval-{key}.log'
            for path in (run_log, eval_log):
                (workspace / path).write_text('Actual ' + path)
                paths.append(path)
            run = {'run_key': key, 'checkpoint_paths': checkpoints, 'evaluations': [
                {'attempt_key': key, 'metrics': document['metrics']}]}
            if explicit_paths:
                run['artifact_paths'] = [run_log]
                run['evaluations'][0]['artifact_paths'] = [eval_log]
            runs.append(run)
        document['formal_runs'] = runs
        document_path.write_text(canonical_json(document))
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            *(TargetCompletionArtifact(role='log', relative_path=path) for path in paths))))
        _, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        by_path = {entry.declared_relative_path: entry.binding.version_ref for entry in manifest.entries}
        assert all(path in by_path for path in paths)
        for index, key in enumerate(('a', 'b')):
            assert {asset['version_ref'] for asset in facts[index]['run_artifacts']} == (
                {by_path[f'logs/train-{key}.log']} if explicit_paths else set())
            expected = {by_path['outputs/metrics.json']}
            if explicit_paths:
                expected.add(by_path[f'logs/eval-{key}.log'])
            assert {asset['version_ref'] for asset in facts[index]['evaluation_artifacts']} == expected
    finally:
        runtime.close()


@pytest.mark.parametrize('run_status,assessed', [('failed', False), ('executed', True), ('failed', True)])
def test_actual_failures_keep_run_attempt_and_artifacts_without_metrics(tmp_path, run_status, assessed):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence = _prepare(workspace, evidence)
        result = workspace / 'outputs/metrics.json'
        document = json.loads(result.read_text())
        document['metrics'] = {}
        document['result_disposition'] = 'uncertain'
        run = document['formal_runs'][0]
        run['status'] = run_status
        if assessed:
            run['evaluations'][0]['status'] = 'failed'
            run['evaluations'][0].pop('metrics')
        else:
            # Retained diagnostics belong to the actual failed Run; no
            # Evaluation exists to own them when no assessment was performed.
            run['artifact_paths'].extend(run['evaluations'][0]['artifact_paths'])
            run['evaluations'] = []
        result.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert accepted.target_commit_ref
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert facts[0]['variant_run']['status'] == run_status
        assert facts[0]['metric_result'] is None
        if assessed:
            assert facts[0]['evaluation_attempt']['status'] == 'failed'
            assert len(facts[0]['evaluation_artifacts']) == 2
        else:
            assert facts[0]['evaluation_attempt'] is None
            report = next(entry for entry in manifest.entries
                if entry.declared_relative_path == 'outputs/evaluation-report.md')
            assert report.binding.version_ref in {item['version_ref'] for item in facts[0]['run_artifacts']}
            assert runtime.owners.research_memory.materialize_asset(report.binding.version_ref).content == (
                workspace / report.declared_relative_path).read_bytes()
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        terminal = graph.query_target_root_commit_transition(handle.target_ref).canonical_terminal
        assert terminal.metric_result_ref is terminal.rg_formal_measurement_receipt is None
        assert terminal.evaluation_attempt_ref == facts[0]['evaluation_attempt_ref']
        assert (terminal.evaluation_attempt_input_binding is not None) == assessed
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_evaluation_attempts').scalar_one() == int(assessed)
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_metric_results').scalar_one() == 0
            registration = json.loads(connection.exec_driver_sql(
                'SELECT execution_registration_json FROM rg_target_root_measurements').scalar_one())
            assert registration['execution_status'] == run_status
            assert registration['evaluation_status'] == ('failed' if assessed else 'pending')
            assert registration['formal_primary_refs']['metric_result_ref'] is None
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    finally:
        runtime.close()


def test_conventional_asset_split_is_bounded_and_keeps_both_log_streams(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    for name in ('train.log', 'eval.log'):
        (logs / name).write_text(name)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _subject_artifact_paths(descriptor, 'logs', 'log') == ('logs/eval.log', 'logs/train.log')
        for index in range(30):
            (logs / f'old-{index}.log').write_text(str(index))
        assert _subject_artifact_paths(descriptor, 'logs', 'log') == ('logs',)
    finally:
        os.close(descriptor)
