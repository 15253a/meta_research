"""The host preserves the exact artifact boundaries chosen by real producers."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.formal_entities import verify_retained_products
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer, _pin_workspace_root, _system_target_completion_handoff
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture
from test_research_notes_and_call_observations import _SystemEvidenceReader


def _discover(workspace, document):
    (workspace / 'implementation').mkdir(exist_ok=True)
    (workspace / 'implementation/method.py').write_text('pass\n')
    (workspace / 'outputs').mkdir(exist_ok=True)
    (workspace / 'outputs/result.json').write_text(canonical_json(document))
    handle = SimpleNamespace(target_ref='target:boundaries', target_run_ref='run:boundaries')
    final = 'Retained the completed work at its declared boundaries.'
    evidence = SimpleNamespace(final_text=final, final_text_sha256=hashlib.sha256(final.encode()).hexdigest())
    pinned = _pin_workspace_root('workspace:boundaries', workspace)
    try:
        return _system_target_completion_handoff(handle=handle, evidence=evidence, root_descriptor=pinned.descriptor)
    finally:
        os.close(pinned.descriptor)


@pytest.mark.parametrize('declaration', ['run', 'evaluation'])
def test_host_preserves_explicit_parent_log_directory(tmp_path, declaration):
    (tmp_path / 'logs').mkdir()
    for i in range(11):
        (tmp_path / f'logs/audit-{i}.log').write_text(f'completed {i}\n')
    run = {'run_key': 'audit', 'evaluations': [{'attempt_key': 'check', 'metrics': {}}]}
    (run if declaration == 'run' else run['evaluations'][0])['artifact_paths'] = ['logs']
    handoff = _discover(tmp_path, {'metrics': {}, 'formal_runs': [run]})
    assert [a.relative_path for a in handoff.artifacts if a.role == 'log'] == ['logs']


def test_host_preserves_explicit_nested_evaluation_file_and_siblings(tmp_path):
    base = tmp_path / 'outputs/analysis/evaluation'
    base.mkdir(parents=True)
    (base / 'report.json').write_text('{"observed": 1}')
    (base / 'other.txt').write_text('retained sibling')
    document = {'metrics': {}, 'formal_runs': [{'run_key': 'run', 'evaluations': [
        {'attempt_key': 'assessment', 'metrics': {}, 'artifact_paths': ['outputs/analysis/evaluation/report.json']}
    ]}]}
    handoff = _discover(tmp_path, document)
    assert {a.relative_path for a in handoff.artifacts if a.role == 'analysis'} == {
        'outputs/analysis/evaluation/report.json', 'outputs/analysis/evaluation/other.txt'}


def test_explicit_parent_and_child_are_both_exact_assets_without_new_semantic_restriction(tmp_path):
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs/train.log').write_text('train completed')
    document = {'metrics': {}, 'formal_runs': [{'run_key': 'run', 'artifact_paths': ['logs'],
        'evaluations': [{'attempt_key': 'assessment', 'metrics': {}, 'artifact_paths': ['logs/train.log']}]}]}
    handoff = _discover(tmp_path, document)
    assert {a.relative_path for a in handoff.artifacts if a.role == 'log'} == {'logs', 'logs/train.log'}


def test_frozen_split_logs_reject_recoverably_and_same_run_accepts_exact_parent(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/metrics.json'
        document = json.loads(path.read_text())
        document['formal_runs'] = [{'run_key': 'audit', 'artifact_paths': ['logs'],
            'evaluations': [{'attempt_key': 'check', 'metrics': document['metrics']}]}]
        path.write_text(canonical_json(document))
        kwargs = dict(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph)
        frozen_result = TargetRunFinalizer(**kwargs, evidence_reader=_EvidenceReader(evidence)).finalize(handle=handle, evidence=evidence)
        assert frozen_result.status == 'rm_accepted'
        frozen = memory.query(frozen_result.manifest_ref)
        finalizer = TargetRunFinalizer(**kwargs, evidence_reader=_EvidenceReader(evidence), graph_authority=runtime.owners.research_graph)
        rejection = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejection.status == 'revision_required'
        assert rejection.rejection_issuer == 'research_graph'
        assert rejection.pending_code == 'target_root_commit_domain_invalid'
        assert 'logs' in rejection.rejection_feedback
        assert memory.query(frozen_result.manifest_ref) == frozen
        assert lifecycle.query(handle.target_ref).status == 'running'
        with runtime._database.read() as db:
            assert db.execute(text('SELECT COUNT(*) FROM rg_target_commits')).scalar_one() == 0
            assert db.execute(text('SELECT COUNT(*) FROM rg_variant_runs')).scalar_one() == 0
        successor = replace(evidence,
            handoff=replace(evidence.handoff, artifacts=tuple(
                TargetCompletionArtifact(role='log', relative_path='logs') if a.role == 'log' else a
                for a in evidence.handoff.artifacts)),
            operation_ref='corrected-boundary-turn', operation_generation=evidence.operation_generation + 1,
            evidence_ref='corrected-boundary-evidence', evidence_sequence=evidence.evidence_sequence + 10,
            observed_at=evidence.observed_at + 1)
        completed = TargetRunFinalizer(**kwargs, evidence_reader=_EvidenceReader(successor),
            graph_authority=runtime.owners.research_graph).finalize(handle=handle, evidence=successor)
        assert completed.status == 'completed'
        assert completed.completion_generation == 2
        assert memory.query(frozen_result.manifest_ref) == frozen
        accepted = memory.query(completed.manifest_ref)
        assert any(e.declared_relative_path == 'logs' for e in accepted.entries)
        assert accepted.result_document.as_dict()['formal_runs'] == document['formal_runs']
        with runtime._database.read() as db:
            assert db.execute(text("SELECT COUNT(*) FROM rg_experiment_asset_roles WHERE role='log_asset'")).scalar_one() == 1
    finally:
        runtime.close()


@pytest.mark.parametrize('path', ['logs/missing.log', 'logs/../private.txt', '/tmp/private.txt', 'implementation'])
def test_unbound_unsafe_or_wrong_role_is_rejected_with_actionable_feedback(path):
    document = {'metrics': {}, 'formal_runs': [{'run_key': 'audit', 'artifact_paths': [path]}]}
    entries = [{'role': 'log', 'declared_relative_path': 'logs/train.log'},
               {'role': 'implementation', 'declared_relative_path': 'implementation'}]
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, entries)
    assert failure.value.code == 'target_root_commit_domain_invalid'
    assert getattr(failure.value, 'feedback', None)


def test_file_as_intermediate_directory_gets_signed_revision_feedback(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        document = json.loads((workspace / 'outputs/metrics.json').read_text())
        invalid_path = 'logs/train.log/not-a-directory'
        document['formal_runs'] = [{'run_key': 'audit', 'artifact_paths': [invalid_path],
            'evaluations': [{'attempt_key': 'check', 'metrics': document['metrics']}]}]
        (workspace / 'outputs/result.json').write_text(canonical_json(document))
        checkpoint = workspace / 'outputs/final.ckpt'
        if checkpoint.exists():
            (workspace / 'outputs/checkpoints').mkdir()
            (workspace / 'outputs/checkpoints/final.ckpt').write_bytes(checkpoint.read_bytes())
        final = 'Retained completed work for formal registration.'
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        evidence = replace(evidence, handoff=None, workspace_ref=workspace_ref, final_text=final,
            final_text_sha256=hashlib.sha256(final.encode()).hexdigest())
        result = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
            evidence_reader=_SystemEvidenceReader()).finalize(handle=handle, evidence=evidence)
        assert result.status == 'revision_required'
        assert result.rejection_issuer == 'research_graph'
        assert result.pending_code == 'target_root_commit_domain_invalid'
        assert invalid_path in result.rejection_feedback
        assert lifecycle.query(handle.target_ref).status == 'running'
        manifest = memory.query(result.manifest_ref)
        assert any(e.declared_relative_path == 'logs/train.log' for e in manifest.entries)
        with runtime._database.read() as db:
            assert db.execute(text('SELECT COUNT(*) FROM rg_target_commits')).scalar_one() == 0
            assert db.execute(text('SELECT COUNT(*) FROM rg_variant_runs')).scalar_one() == 0
    finally:
        runtime.close()
