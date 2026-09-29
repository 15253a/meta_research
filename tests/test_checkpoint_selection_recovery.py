"""Checkpoint declaration mistakes return through the actual Owner revision seam."""
from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _root_finalizer_fixture


DATA = 'outputs/data/modma-neural/fold-0-seed-20260929/pretrained-selected-epoch12.pt'
STATE = 'outputs/checkpoints/fold-0-seed-20260929/pretrained-selected-epoch12.pt'
CONTENT = b'existing completed training state; do not rerun training'
TABLES = ('rg_variant_runs', 'rg_evaluation_attempts', 'rg_metric_results',
          'rg_experiment_asset_roles', 'rg_experiment_input_bindings',
          'rg_target_root_measurements', 'rg_target_commits')


def _setup(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    document = json.loads((workspace / 'outputs/metrics.json').read_text())
    document['formal_runs'] = [{'run_key': 'main_neural_fold_0_seed_20260929',
        'artifact_paths': [DATA, 'logs/train.log'], 'checkpoint_paths': [DATA],
        'evaluations': [{'attempt_key': 'held-out-assessment', 'metrics': document['metrics']}]}]
    (workspace / DATA).parent.mkdir(parents=True)
    (workspace / DATA).write_bytes(CONTENT)
    (workspace / 'outputs/result.json').write_text(canonical_json(document))
    workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
        fence_ref=handle.execution_fence_ref)
    final = 'Training and assessment completed; retain the actual outputs.'
    evidence = replace(evidence, handoff=None, workspace_ref=workspace_ref, final_text=final,
        final_text_sha256=hashlib.sha256(final.encode()).hexdigest())
    kwargs = dict(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph)
    return runtime, lifecycle, memory, handle, workspace, evidence, document, kwargs


def _counts(runtime):
    with runtime._database.read() as db:
        return {table: db.execute(text('SELECT count(*) FROM ' + table)).scalar_one() for table in TABLES}


@pytest.mark.parametrize('retain_data_copy', [False, True], ids=['move-state', 'keep-attributed-data-copy'])
def test_frozen_data_checkpoint_returns_revision_and_same_run_can_complete(tmp_path, retain_data_copy):
    runtime, lifecycle, memory, handle, workspace, evidence, document, kwargs = _setup(tmp_path)
    try:
        # Production already has an accepted RM manifest when RG rejects it.
        frozen_result = TargetRunFinalizer(**kwargs).finalize(handle=handle, evidence=evidence)
        assert frozen_result.status == 'rm_accepted'
        frozen = memory.query(frozen_result.manifest_ref)
        original = next(entry for entry in frozen.entries if entry.declared_relative_path == DATA)
        assert original.role == 'data'
        assert not any(entry.role == 'checkpoint' for entry in frozen.entries)
        before = _counts(runtime)
        finalizer = TargetRunFinalizer(**kwargs, graph_authority=runtime.owners.research_graph)
        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == 'revision_required'
        assert rejected.pending_code == 'target_root_commit_domain_invalid'
        assert rejected.rejection_issuer == 'research_graph'
        assert DATA in rejected.rejection_feedback and 'data' in rejected.rejection_feedback
        assert 'outputs/checkpoints' in rejected.rejection_feedback
        assert 'checkpoint_paths' in rejected.rejection_feedback
        assert _counts(runtime) == before
        assert memory.query(frozen.manifest_ref) == frozen
        assert runtime.owners.research_memory.materialize_asset(original.binding.version_ref).content == CONTENT
        rejected_completion = lifecycle.query_completion(handle.target_ref)
        rejection = lifecycle.query_completion_rejection(rejected_completion.completion_ref)
        assert rejection.manifest_ref == frozen.manifest_ref
        assert rejection.receipt.subject_ref == rejected_completion.completion_ref
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected

        # Model-side correction is simulated only in this isolated workspace.
        (workspace / STATE).parent.mkdir(parents=True)
        if retain_data_copy:
            (workspace / STATE).write_bytes((workspace / DATA).read_bytes())
        else:
            (workspace / DATA).rename(workspace / STATE)
            # Leave no empty conventional data directory needing new attribution.
            (workspace / DATA).parent.rmdir()
            (workspace / 'outputs/data/modma-neural').rmdir()
            (workspace / 'outputs/data').rmdir()
        document['formal_runs'][0]['checkpoint_paths'] = [STATE]
        document['formal_runs'][0]['artifact_paths'] = ([DATA] if retain_data_copy else []) + ['logs/train.log']
        (workspace / 'outputs/result.json').write_text(canonical_json(document))
        successor = replace(evidence, operation_ref='checkpoint-corrected-turn',
            operation_generation=evidence.operation_generation + 1, evidence_ref='checkpoint-corrected-evidence',
            evidence_sequence=evidence.evidence_sequence + 10, observed_at=evidence.observed_at + 1)
        completed = finalizer.finalize(handle=handle, evidence=successor)
        assert completed.status == 'completed' and completed.completion_generation == 2
        next_completion = lifecycle.query_completion(handle.target_ref)
        assert next_completion.handle == rejected_completion.handle == handle
        assert next_completion.predecessor_rejection_ref == rejection.rejection_ref
        assert next_completion.predecessor_completion_ref == rejected_completion.completion_ref
        accepted = memory.query(completed.manifest_ref)
        state = next(entry for entry in accepted.entries if entry.declared_relative_path == STATE)
        assert state.role == 'checkpoint'
        assert runtime.owners.research_memory.materialize_asset(state.binding.version_ref).content == CONTENT
        assert memory.query(frozen.manifest_ref) == frozen
        assert accepted.result_document.metrics == frozen.result_document.metrics
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert facts[0]['evaluation_attempt']['inputs']['checkpoint_refs'] == [state.binding.version_ref]
        # Accepted history depends on immutable assets, not the later live file.
        (workspace / STATE).write_bytes(b'later unaccepted workspace edit')
        assert finalizer.finalize(handle=handle, evidence=successor) == completed
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


def test_evaluation_cannot_select_another_runs_checkpoint(tmp_path):
    runtime, lifecycle, memory, handle, workspace, evidence, document, kwargs = _setup(tmp_path)
    try:
        other = 'outputs/checkpoints/other.pt'
        for path in (STATE, other):
            (workspace / path).parent.mkdir(parents=True, exist_ok=True)
            (workspace / path).write_bytes(path.encode())
        run = document['formal_runs'][0]
        run['checkpoint_paths'] = [STATE]
        run['evaluations'][0]['checkpoint_paths'] = [other]
        document['formal_runs'].append({'run_key': 'other-training', 'checkpoint_paths': [other], 'evaluations': []})
        (workspace / 'outputs/result.json').write_text(canonical_json(document))
        before = _counts(runtime)
        result = TargetRunFinalizer(**kwargs, graph_authority=runtime.owners.research_graph).finalize(handle=handle, evidence=evidence)
        assert result.status == 'revision_required'
        assert result.pending_code == 'target_root_commit_domain_invalid'
        assert other in result.rejection_feedback and 'Evaluation' in result.rejection_feedback
        assert _counts(runtime) == before
        assert memory.query(result.manifest_ref) is not None
    finally:
        runtime.close()


def test_corrupt_frozen_manifest_is_not_reclassified_as_a_revisable_path_error(tmp_path):
    runtime, lifecycle, memory, handle, workspace, evidence, document, kwargs = _setup(tmp_path)
    try:
        seeded = TargetRunFinalizer(**kwargs).finalize(handle=handle, evidence=evidence)
        with runtime._database.write() as db:
            db.execute(text("UPDATE rm_target_root_completion_manifests SET entries_hash=:bad WHERE manifest_ref=:ref"),
                {'bad': '0' * 64, 'ref': seeded.manifest_ref})
        before = _counts(runtime)
        with pytest.raises(OwnerConflict, match='target_root_manifest_integrity_invalid'):
            TargetRunFinalizer(**kwargs, graph_authority=runtime.owners.research_graph).finalize(handle=handle, evidence=evidence)
        assert _counts(runtime) == before
        assert lifecycle.query_completion_rejection(seeded.completion_ref) is None
    finally:
        runtime.close()
