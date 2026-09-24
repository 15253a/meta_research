"""Actual report-based assessments keep formal provenance without invented scores."""
import json
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _CurrentBindingBundleSkill, _root_finalizer_fixture


def _report_protocol(monkeypatch):
    original = _CurrentBindingBundleSkill._target_plan

    def plan(self, request):
        document = original(self, request)
        for candidate in document['initial_strategy_update']['candidates']:
            protocol = candidate['measurement_contract']['protocol_version']
            protocol['required_metrics'] = []
            protocol['optional_metrics'] = []
            protocol['evaluation_data'] = {'purpose': 'Inspect source completeness and applicability in a report'}
        return document

    monkeypatch.setattr(_CurrentBindingBundleSkill, '_target_plan', plan)


def _report_work(workspace, evidence, *, report=True, status='executed', artifact_paths=None):
    report_path = 'outputs/analysis/evaluation/report.md'
    if report:
        path = workspace / report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Material inspection\n\nThe supplied source contains the recorded observations. '
                        'Its collection period supports this question; missing interview context limits interpretation.\n',
                        encoding='utf-8')
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts, TargetCompletionArtifact(role='analysis', relative_path=report_path))))
    document_path = workspace / 'outputs/metrics.json'
    document = json.loads(document_path.read_text())
    document['metrics'] = {}
    attempt = {'attempt_key': 'material-inspection', 'status': status, 'metrics': {}}
    if artifact_paths is not None:
        attempt['artifact_paths'] = artifact_paths
    document['formal_runs'] = [{'run_key': 'material-collection', 'evaluations': [attempt]}]
    document_path.write_text(canonical_json(document))
    return evidence, report_path


@pytest.mark.parametrize('explicit_assignment', [False, True])
def test_report_only_assessment_is_accepted_queryable_and_replayable(tmp_path, monkeypatch, explicit_assignment):
    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        report_path = 'outputs/analysis/evaluation/report.md'
        evidence, _ = _report_work(workspace, evidence,
            artifact_paths=[report_path] if explicit_assignment else None)
        assert authority.measurement_contract.protocol_version.required_metric_keys == ()
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        fact = facts[0]
        assert fact['evaluation_attempt']['status'] == 'measurement_accepted'
        assert fact['metric_result']['metrics'] == {}
        assert fact['metric_result_ref'] is not None
        report = next(entry for entry in manifest.entries if entry.declared_relative_path == report_path)
        assert report.binding.version_ref in {item['version_ref'] for item in fact['evaluation_artifacts']}
        assert report.binding.version_ref not in {item['version_ref'] for item in fact['run_artifacts']}
        retained = runtime.owners.research_memory.materialize_asset(report.binding.version_ref).content
        assert b'missing interview context' in retained
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
        # Bundle and later evidence readers consume the report-based result
        # through the same verified result identity, with no invented score.
        from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog, target_commit_metric_result
        commit = next(item for item in graph.query_target_commits(authority.graph_ref)
                      if item.commit_ref == accepted.target_commit_ref)
        assert target_commit_metric_result(commit)['metrics'] == {}
        graph_record = graph.query_target_graph(authority.stage_request_ref)
        runtime.bundle_stage._publish_target_commit_evidence(quest_ref=graph_record.quest_ref, commit=commit)
        catalog = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory)
        leaves = catalog.resolve_reasoning_target_evidence_leaves(quest_ref=graph_record.quest_ref,
            target_commit_refs=(commit.commit_ref,))
        assert len(leaves) == 1 and leaves[0].evidence_item_ref == fact['metric_result_ref']
        from meta_research.formal_entities import register_root_entities
        from test_reused_evaluation_source import _source_packet
        packet = _source_packet(runtime, handle.target_ref)
        with runtime._database.read() as connection:
            reused = register_root_entities(connection, **packet, source_owner=graph, verify_only=True)
            assert reused[0]['metric_result_ref'] == fact['metric_result_ref']
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_evaluation_attempts').scalar_one() == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('report,paths', [(False, None), (True, []), (False, ['logs/train.log'])])
def test_empty_metrics_need_an_assigned_report_not_only_logs_or_unassigned_text(tmp_path, monkeypatch, report, paths):
    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence, _ = _report_work(workspace, evidence, report=report, artifact_paths=paths)
        from meta_research.target_run_finalizer import TargetRootOwnerRejection
        rejected, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert isinstance(rejected, TargetRootOwnerRejection)
        assert rejected.code == 'target_root_commit_domain_invalid'
        assert 'report-only assessment' in rejected.feedback
        with runtime._database.read() as connection:
            for table in ('rg_target_commits', 'rg_evaluation_attempts', 'rg_metric_results'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == 0
    finally:
        runtime.close()


def test_report_does_not_waive_declared_required_metrics(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        assert authority.measurement_contract.protocol_version.required_metric_keys
        evidence, _ = _report_work(workspace, evidence)
        from meta_research.target_run_finalizer import TargetRootOwnerRejection
        rejected, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert isinstance(rejected, TargetRootOwnerRejection)
        assert rejected.code == 'target_root_commit_domain_invalid'
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
    finally:
        runtime.close()


@pytest.mark.parametrize('status', ['blocked', 'failed'])
def test_report_file_does_not_turn_unperformed_or_failed_assessment_into_result(tmp_path, monkeypatch, status):
    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence, report_path = _report_work(workspace, evidence, status=status)
        if status == 'blocked':
            # The retained report belongs to the work that actually happened;
            # an unperformed Evaluation cannot own a report artifact.
            result_path = workspace / 'outputs/metrics.json'
            document = json.loads(result_path.read_text())
            document['formal_runs'][0]['artifact_paths'] = [report_path]
            result_path.write_text(canonical_json(document))
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert accepted.target_commit_ref
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        report = next(entry for entry in manifest.entries if entry.declared_relative_path == report_path)
        assert runtime.owners.research_memory.materialize_asset(report.binding.version_ref).content == (workspace / report_path).read_bytes()
        owner_artifacts = fact['run_artifacts' if status == 'blocked' else 'evaluation_artifacts']
        assert report.binding.version_ref in {item['version_ref'] for item in owner_artifacts}
        assert fact['metric_result'] is None
        assert (fact['evaluation_attempt'] is None) == (status == 'blocked')
        if status == 'failed':
            assert fact['evaluation_attempt']['status'] == 'failed'
        with runtime._database.read() as connection:
            assert connection.execute(text('SELECT count(*) FROM rg_metric_results')).scalar_one() == 0
    finally:
        runtime.close()


def test_missing_report_returns_owner_feedback_and_next_root_turn_completes(tmp_path, monkeypatch):
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_target_root_finalizer import _EvidenceReader
    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        evidence, _ = _report_work(workspace, evidence, report=False)

        def finalizer(current_evidence):
            return TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
                workspace_resolver=runtime.target_run_authorities.agent_runtime,
                evidence_reader=_EvidenceReader(current_evidence),
                measurement_authority=runtime.owners.research_graph,
                graph_authority=runtime.owners.research_graph)

        first = finalizer(evidence).finalize(handle=handle, evidence=evidence)
        assert first.status == 'revision_required'
        assert 'supply or assign the missing report' in first.rejection_feedback
        assert lifecycle.query(handle.target_ref).status == 'running'
        original = lifecycle.query_completion(handle.target_ref)
        rejection = lifecycle.query_completion_rejection(original.completion_ref)
        assert finalizer(evidence).finalize(handle=handle, evidence=evidence) == first
        revised, _ = _report_work(workspace, evidence)
        revised = replace(revised, operation_ref='root-report-correction',
            operation_generation=evidence.operation_generation + 1,
            evidence_ref='root-report-correction-evidence', evidence_sequence=evidence.evidence_sequence + 1,
            observed_at=evidence.observed_at + 1)
        completed = finalizer(revised).finalize(handle=handle, evidence=revised)
        assert completed.status == 'completed'
        successor = lifecycle.query_completion(handle.target_ref)
        assert successor.generation == 2 and successor.predecessor_completion_ref == original.completion_ref
        assert successor.predecessor_rejection_ref == rejection.rejection_ref
        assert successor.handle == original.handle == handle
        assert lifecycle.query_completion_by_ref(original.completion_ref) == original
        assert memory.query(first.manifest_ref) is not None
        assert finalizer(revised).finalize(handle=handle, evidence=revised) == completed
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert fact['evaluation_attempt']['status'] == 'measurement_accepted'
        assert fact['metric_result']['metrics'] == {}
    finally:
        runtime.close()
