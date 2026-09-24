"""Retention is a research choice; exact selected states stay verifiable."""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_execution_contract import target_execution_skill_text
from meta_research.target_run_finalizer import TargetRootOwnerRejection, _subject_artifact_paths
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _retention_fixture(tmp_path, monkeypatch, policy, count):
    import test_public_bundle_stage as fixtures

    original = fixtures._formal_candidate

    def candidate(*args, **kwargs):
        value = original(*args, **kwargs)
        value['measurement_contract']['checkpoint_policy'] = policy
        return value

    monkeypatch.setattr(fixtures, '_formal_candidate', candidate)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    assert authority.measurement_contract.checkpoint_policy == policy
    artifacts = [item for item in evidence.handoff.artifacts if item.role != 'checkpoint']
    paths = []
    for index in range(count):
        path = f'outputs/checkpoints/step-{index}.state'
        (workspace / path).parent.mkdir(exist_ok=True)
        (workspace / path).write_bytes(f'actual selected state {index}'.encode())
        paths.append(path)
        artifacts.append(TargetCompletionArtifact(role='checkpoint', relative_path=path))
    evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=tuple(artifacts)))
    result = workspace / 'outputs/metrics.json'
    document = json.loads(result.read_text())
    document['formal_runs'] = [{'run_key': 'selected-state-run', 'checkpoint_paths': paths,
        'evaluations': [{'attempt_key': 'selected-state-assessment', 'metrics': document['metrics'],
                         'checkpoint_paths': paths[-1:]}]}]
    document['metrics'] = {}
    result.write_text(canonical_json(document))
    return runtime, lifecycle, memory, handle, workspace, evidence, paths


@pytest.mark.parametrize('policy,count', [('required', 0), ('forbidden', 3), ('optional', 2)])
def test_owner_accepts_agent_selected_checkpoint_coverage_and_exact_assessment_subset(tmp_path, monkeypatch, policy, count):
    runtime, lifecycle, memory, handle, _, evidence, paths = _retention_fixture(tmp_path, monkeypatch, policy, count)
    try:
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        versions = {entry.declared_relative_path: entry.binding.version_ref
                    for entry in manifest.entries if entry.role == 'checkpoint'}
        assert set(versions) == set(paths)
        checkpoints = [item for item in facts[0]['run_artifacts'] if item['role'] == 'checkpoint_artifact']
        assert {item['version_ref'] for item in checkpoints} == set(versions.values())
        assert facts[0]['evaluation_attempt']['inputs']['checkpoint_refs'] == [versions[path] for path in paths[-1:]]
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
        if paths:
            with runtime._database.write() as connection:
                connection.execute(text("UPDATE rg_experiment_asset_roles SET content_hash=:hash "
                    "WHERE subject_ref=:run AND role='checkpoint_artifact'"),
                    {'hash': '0' * 64, 'run': facts[0]['variant_run_ref']})
            with pytest.raises(OwnerConflict, match='integrity_invalid'):
                graph.query_target_formal_results(handle.target_ref)
    finally:
        runtime.close()


def test_evaluation_cannot_claim_a_published_state_excluded_from_its_run(tmp_path, monkeypatch):
    runtime, lifecycle, memory, handle, workspace, evidence, paths = _retention_fixture(tmp_path, monkeypatch, 'forbidden', 2)
    try:
        result = workspace / 'outputs/metrics.json'
        document = json.loads(result.read_text())
        document['formal_runs'][0]['checkpoint_paths'] = paths[:1]
        result.write_text(canonical_json(document))
        rejected, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        assert isinstance(rejected, TargetRootOwnerRejection)
        assert rejected.code == 'target_root_commit_domain_invalid'
        assert paths[1] in rejected.feedback
        assert 'actual Run or Evaluation owner' in rejected.feedback
        states = [entry for entry in manifest.entries if entry.role == 'checkpoint']
        assert {entry.declared_relative_path for entry in states} == set(paths)
        for entry in states:
            assert runtime.owners.research_memory.materialize_asset(entry.binding.version_ref).content == (
                workspace / entry.declared_relative_path).read_bytes()
        with runtime._database.read() as connection:
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_variant_runs').scalar_one() == 0
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_target_commits').scalar_one() == 0
    finally:
        runtime.close()


def test_system_intake_preserves_agent_state_groups_and_bounds_only_the_index(tmp_path):
    checkpoints = tmp_path / 'outputs' / 'checkpoints'
    checkpoints.mkdir(parents=True)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _subject_artifact_paths(descriptor, 'outputs/checkpoints', 'checkpoint') == ()
        for name in ('step-100', 'step-700'):
            state = checkpoints / name
            state.mkdir()
            for index in range(40):
                (state / f'part-{index}.bin').write_bytes(bytes([index]))
        assert _subject_artifact_paths(descriptor, 'outputs/checkpoints', 'checkpoint') == (
            'outputs/checkpoints/step-100', 'outputs/checkpoints/step-700')
        for index in range(30):
            (checkpoints / f'state-{index}.bin').write_bytes(bytes([index]))
        assert _subject_artifact_paths(descriptor, 'outputs/checkpoints', 'checkpoint') == ('outputs/checkpoints',)
        assert len(list(checkpoints.rglob('*.bin'))) == 110
    finally:
        os.close(descriptor)


def test_injected_target_skill_provides_readable_data_and_formal_work_references():
    skill = target_execution_skill_text()
    path = Path(skill.split('Skill source: ', 1)[1].splitlines()[0])
    assert path.is_absolute() and path.is_file()
    for reference in ('formal-work.md', 'data-preservation.md'):
        assert f'references/{reference}' in skill
        assert (path.parent / 'references' / reference).is_file()
