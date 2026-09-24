"""Whole research artifacts cross the real AR completion / RM boundary."""
from dataclasses import replace
import hashlib
from pathlib import Path

from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _root_finalizer_fixture, _EvidenceReader


def test_large_dataset_and_checkpoint_are_preserved_and_replay_without_live_files(tmp_path, monkeypatch):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        data = workspace / 'outputs/data/acquired'
        data.mkdir(parents=True)
        (data / 'empty-split').mkdir()
        dataset = data / 'samples.bin'
        checkpoint = workspace / 'outputs/large-checkpoint.bin'
        for path, size in ((dataset, 65 * 1024 * 1024 + 3), (checkpoint, 193 * 1024 * 1024 + 7)):
            with path.open('wb') as stream:
                stream.write(b'real retained research artifact\n')
                stream.truncate(size)
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role='data', relative_path='outputs/data/acquired'),
            TargetCompletionArtifact(role='checkpoint', relative_path='outputs/large-checkpoint.bin'),
        )))
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence))
        outcome = finalizer.finalize(handle=handle, evidence=evidence)
        assert outcome.status == 'rm_accepted', outcome
        manifest = memory.query(outcome.manifest_ref)
        assert sum(entry.byte_count for entry in manifest.entries) > 256 * 1024 * 1024
        accepted_data = next(entry for entry in manifest.entries if entry.role == 'data')
        assert accepted_data.media_type == 'application/x-directory'
        assert accepted_data.byte_count == dataset.stat().st_size
        checkpoint_entry = next(entry for entry in manifest.entries
                                if entry.declared_relative_path == 'outputs/large-checkpoint.bin')
        assert checkpoint_entry.byte_count == checkpoint.stat().st_size
        # Projection queries must never fetch large bodies merely to read a receipt.
        old_materialize = memory._asset_memory.materialize_asset
        def bounded_materialize(ref):
            assert ref not in {accepted_data.binding.version_ref, checkpoint_entry.binding.version_ref}
            return old_materialize(ref)
        monkeypatch.setattr(memory._asset_memory, 'materialize_asset', bounded_materialize)
        dataset.write_bytes(b'new workspace contents')
        checkpoint.unlink()
        assert finalizer.finalize(handle=handle, evidence=evidence) == outcome
        exported = runtime.owners.research_memory.export_asset(
            accepted_data.binding.version_ref, tmp_path / 'exported-dataset')
        assert (exported.path / 'samples.bin').stat().st_size == accepted_data.byte_count
        assert (exported.path / 'empty-split').is_dir()
        assert not list(workspace.parent.glob('.target-completion-*'))
    finally:
        runtime.close()
