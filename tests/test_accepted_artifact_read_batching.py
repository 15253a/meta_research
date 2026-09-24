"""Large accepted outputs do not generate a database read per artifact."""
import json
import time
from dataclasses import replace

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.fixture
def many_artifact_root(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        paths = []
        artifacts = [item for item in evidence.handoff.artifacts if item.role != 'checkpoint']
        for index in range(32):
            path = f'outputs/checkpoints/state-{index}.bin'
            (workspace / path).parent.mkdir(exist_ok=True)
            (workspace / path).write_bytes(f'real retained state {index}'.encode())
            paths.append(path)
            artifacts.append(TargetCompletionArtifact(role='checkpoint', relative_path=path))
            path = f'outputs/analysis/raw/observation-{index}.json'
            (workspace / path).parent.mkdir(parents=True, exist_ok=True)
            (workspace / path).write_text(json.dumps({'index': index}))
            artifacts.append(TargetCompletionArtifact(role='analysis', relative_path=path))
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=tuple(artifacts)))
        result = workspace / 'outputs/metrics.json'
        document = json.loads(result.read_text())
        document['formal_runs'] = [{'run_key': 'all-retained-states', 'checkpoint_paths': paths,
            'evaluations': [{'attempt_key': 'assessment', 'metrics': document['metrics'],
                             'checkpoint_paths': paths}]}]
        document['metrics'] = {}
        result.write_text(canonical_json(document))
        accepted, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        yield runtime, handle, accepted
    finally:
        runtime.close()


def test_large_accepted_frontier_batches_native_artifact_reads(many_artifact_root):
    runtime, handle, accepted = many_artifact_root
    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if ('rg_experiment_asset_roles' in statement or
                'rg_evaluation_attempt_checkpoints' in statement):
            queries.append(statement)

    event.listen(runtime._database._engine, 'before_cursor_execute', record)
    started = time.monotonic()
    try:
        with runtime._database.read_snapshot():
            transition = runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
    finally:
        event.remove(runtime._database._engine, 'before_cursor_execute', record)
    assert transition.target_commit_ref == accepted.target_commit_ref
    assert len(transition.canonical_terminal.checkpoint_artifact_refs) == 32
    assert len(queries) <= 8, (
        f'Accepted Target boundary performed {len(queries)} native artifact SQL reads '
        f'in {time.monotonic() - started:.3f}s for 32 checkpoints plus 32 outputs; '
        'read whole subject assignments, not each retained artifact.'
    )


@pytest.mark.parametrize('mutation', ['artifact', 'missing_artifact', 'checkpoint', 'missing_checkpoint'])
def test_batched_accepted_artifact_reads_retain_exact_integrity(many_artifact_root, mutation):
    runtime, handle, _ = many_artifact_root
    with runtime._database.write() as connection:
        if mutation == 'artifact':
            connection.execute(text("UPDATE rg_experiment_asset_roles SET content_hash=:hash WHERE role='analysis_asset'"),
                               {'hash': '0' * 64})
        elif mutation == 'missing_artifact':
            connection.exec_driver_sql("DELETE FROM rg_experiment_asset_roles WHERE role='analysis_asset'")
        elif mutation == 'checkpoint':
            for source, destination in ((0, 32), (1, 0), (32, 1)):
                connection.execute(text('UPDATE rg_evaluation_attempt_checkpoints SET ordinal=:destination '
                    'WHERE ordinal=:source'), {'source': source, 'destination': destination})
        else:
            connection.exec_driver_sql('DELETE FROM rg_evaluation_attempt_checkpoints WHERE ordinal=0')
    with pytest.raises(OwnerConflict):
        runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
