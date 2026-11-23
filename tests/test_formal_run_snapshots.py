"""Real result.json -> system finalizer -> RM -> RG execution bindings."""
from dataclasses import replace
import hashlib
import json
import subprocess
import sys

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _root_finalizer_fixture


def _scenario(tmp_path, mutate=lambda doc: None, *, runtime=None, ready=None):
    runtime, lifecycle, memory, authority, handle, workspace, old = _root_finalizer_fixture(tmp_path, runtime=runtime, ready=ready)
    for index, number in enumerate((6, 13)):
        impl = workspace / f'implementation/v{index+1}'
        impl.mkdir()
        script = impl / 'calculate.py'
        script.write_text(f'print({number} * {number})\n' if index == 0 else
            'from pathlib import Path\nprint(int(Path("outputs/data/run1.txt").read_text()) + 133)\n')
        output = subprocess.check_output([sys.executable, str(script)], cwd=workspace)
        artifact = workspace / f'outputs/data/run{index+1}.txt'
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(output)
    metrics = json.loads((workspace / 'outputs/result.json').read_text())
    evaluator=workspace/'implementation/v2/evaluate.py'
    evaluator.write_text('from pathlib import Path\nimport json\na=int(Path("outputs/data/run1.txt").read_text())\nb=int(Path("outputs/data/run2.txt").read_text())\nprint(json.dumps({"metric:effect":b-a}))\n')
    metrics['metrics']=json.loads(subprocess.check_output([sys.executable,str(evaluator)],cwd=workspace))
    doc = {**metrics, 'formal_runs': [
        {'run_key': 'first', 'implementation_paths': ['implementation/v1'],
         'checkpoint_paths': [], 'artifact_paths': ['outputs/data/run1.txt'], 'evaluations': []},
        {'run_key': 'second', 'implementation_paths': ['implementation/v2'],
         'local_inputs': [{'producer_run_key': 'first', 'artifact_path': 'outputs/data/run1.txt'}],
         'checkpoint_paths': [], 'artifact_paths': ['outputs/data/run2.txt'],
         'evaluations': [{'attempt_key': 'check', 'metrics': metrics['metrics']}]},
    ]}
    mutate(doc)
    (workspace / 'outputs/result.json').write_text(canonical_json(doc))
    workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
        fence_ref=handle.execution_fence_ref)
    final_text = 'Squared 6 and 13 with two retained method snapshots.'
    evidence = replace(old, workspace_ref=workspace_ref, final_text=final_text,
        final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest())
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph)
    return runtime, lifecycle, memory, handle, evidence, finalizer


def test_two_real_snapshots_and_local_input_are_frozen_and_readable(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path)
    try:
        frozen = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query(frozen.manifest_ref)
        graph = runtime.owners.research_graph
        accepted = graph.accept_target_commit_from_root_completion(
            completion=lifecycle.query_completion(handle.target_ref), manifest=manifest,
            result_document=manifest.result_document, idempotency_key='two-real-runs')
        assert getattr(accepted, 'issuer', None) != 'research_graph', getattr(accepted, 'feedback', '')
        rows = {row['run_key']: row for row in graph.query_target_formal_results(handle.target_ref)}
        entries = {entry.declared_relative_path: entry for entry in manifest.entries}
        first, second = [rows[key]['variant_run']['inputs'] for key in ('first','second')]
        from meta_research.formal_run_bindings import implementation_binding, manifest_selection_entries
        logical = manifest_selection_entries(manifest)
        assert first['implementation_revision_ref'] == implementation_binding(logical, ['implementation/v1'])[0]
        assert second['implementation_revision_ref'] == implementation_binding(logical, ['implementation/v2'])[0]
        assert first['implementation_revision_ref'] != second['implementation_revision_ref']
        assert entries['outputs/data/run1.txt'].binding.version_ref in second['input_refs']
        for path, expected in [('outputs/data/run1.txt', b'36\n'), ('outputs/data/run2.txt', b'169\n')]:
            assert runtime.owners.research_memory.materialize_asset(entries[path].binding.version_ref).content == expected
        assert graph.query_target_root_commit_transition(handle.target_ref).target_commit_ref == accepted.target_commit_ref
        assert finalizer.finalize(handle=handle, evidence=evidence) == frozen
    finally:
        runtime.close()


@pytest.mark.parametrize('case, code', [('revision','implementation_revision_mismatch'),
    ('second_revision','implementation_revision_mismatch'),
    ('cycle','local_input_order_invalid'), ('unowned','local_input_ownership_invalid')])
def test_false_run_bindings_are_rejected_after_real_freezing(tmp_path, case, code):
    def mutate(doc):
        if case == 'revision': doc['formal_runs'][0]['implementation_revision_ref'] = 'invented'
        elif case == 'second_revision': doc['formal_runs'][1]['implementation_revision_ref'] = '\x01' * 1024
        elif case == 'cycle': doc['formal_runs'][0]['local_inputs'] = [
            {'producer_run_key': 'second', 'artifact_path': 'outputs/data/run2.txt'}]
        else: doc['formal_runs'][1]['local_inputs'][0]['artifact_path'] = 'outputs/data/run2.txt'
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, mutate)
    try:
        frozen = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query(frozen.manifest_ref)
        rejected = runtime.owners.research_graph.accept_target_commit_from_root_completion(
            completion=lifecycle.query_completion(handle.target_ref), manifest=manifest,
            result_document=manifest.result_document, idempotency_key='false-run-binding')
        assert rejected.issuer == 'research_graph'
        assert code in rejected.code
        if case in {'revision', 'second_revision'}:
            from meta_research.formal_run_bindings import implementation_binding, manifest_selection_entries
            index = 0 if case == 'revision' else 1
            run = manifest.result_document.as_dict()['formal_runs'][index]
            revision, _ = implementation_binding(manifest_selection_entries(manifest), run['implementation_paths'])
            assert f'$.formal_runs[{index}].implementation_revision_ref' in rejected.feedback
            assert ('"invented"' if index == 0 else '1024 characters') in rejected.feedback
            assert revision in rejected.feedback and 'optional' in rejected.feedback
            assert len(rejected.feedback) <= 4096
        with runtime._database.read() as connection:
            assert connection.execute(text('SELECT COUNT(*) FROM rg_variant_runs')).scalar_one() == 0
    finally:
        runtime.close()
