from pathlib import Path
from contextlib import contextmanager
from dataclasses import replace

import pytest

import meta_research.target_run_finalizer as finalizer_module
from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _GraphAcceptance, _root_finalizer_fixture


@contextmanager
def _pending_completion(tmp_path, monkeypatch, *, directory=False):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    monkeypatch.setattr(finalizer_module, "TARGET_ROOT_INLINE_ARTIFACT_BYTES", 1)
    if directory:
        def streamed_directory(_descriptor):
            raise finalizer_module.TargetImplementationBundleError("target_implementation_bundle_too_large")
        monkeypatch.setattr(finalizer_module, "build_target_implementation_bundle_from_open_directory", streamed_directory)

    class UnavailableMemory:
        def __getattr__(self, name):
            return getattr(memory, name)

        def accept(self, **values):
            raise OwnerConflict("target_root_artifact_intake_unavailable")

    try:
        with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
            TargetRunFinalizer(
                lifecycle=lifecycle, memory=UnavailableMemory(),
                workspace_resolver=runtime.target_run_authorities.agent_runtime,
                evidence_reader=_EvidenceReader(evidence),
            ).finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None
        saved = list((workspace.parent / ".target-completion-intakes").glob("*/artifact-*"))
        assert saved
        if directory:
            assert any(path.is_dir() for path in saved)
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
        )
        yield lifecycle, memory, handle, workspace, evidence, completion, saved, finalizer
    finally:
        runtime.close()


@pytest.mark.parametrize("directory", [False, True])
def test_ar_only_retry_reuses_private_frozen_bytes_without_copying(tmp_path, monkeypatch, directory):
    with _pending_completion(tmp_path, monkeypatch, directory=directory) as values:
        lifecycle, memory, handle, workspace, evidence, completion, saved, finalizer = values
        # Streamed output custody is already private; later workspace edits do
        # not supersede the signed completion's original frozen bytes.
        (workspace / "logs/train.log").write_text("later workspace log")
        original_open = Path.open

        def no_new_snapshot_copy(path, mode="r", *args, **kwargs):
            if mode == "xb" and any(part.startswith(".target-completion-") for part in path.parts):
                raise OSError(122, "fixture quota rejects another snapshot copy")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", no_new_snapshot_copy)
        result = finalizer.finalize(handle=handle, evidence=evidence)
        assert result.status == "rm_accepted"
        assert result.completion_ref == completion.completion_ref
        assert result.completion_generation == 1


@pytest.mark.parametrize("directory", [False, True])
def test_accepted_replay_retries_failed_cleanup_before_delivery(tmp_path, monkeypatch, directory):
    with _pending_completion(tmp_path, monkeypatch, directory=directory) as values:
        lifecycle, memory, handle, workspace, evidence, completion, saved, finalizer = values
        graph = _GraphAcceptance()
        finalizer._graph_authority = graph
        rmtree = finalizer_module.shutil.rmtree

        def failed_cleanup(path, *args, **kwargs):
            if Path(path) == saved[0].parent:
                raise OSError("temporary frozen cleanup failed")
            return rmtree(path, *args, **kwargs)

        with monkeypatch.context() as first_cleanup:
            first_cleanup.setattr(finalizer_module.shutil, "rmtree", failed_cleanup)
            with pytest.raises(OSError, match="temporary frozen cleanup failed"):
                finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query_for_completion(completion.completion_ref)
        assert manifest is not None
        assert graph.calls == 0
        assert all(path.exists() for path in saved)
        pending = saved[0].parent.parent / "another-pending-completion"
        pending.mkdir()
        (pending / "artifact-0000").write_bytes(b"still pending")

        def forbidden(*args, **kwargs):
            pytest.fail("accepted cleanup reopened or revalidated the live workspace")

        monkeypatch.setattr(finalizer, "_freeze", forbidden)
        monkeypatch.setattr(finalizer._workspace_resolver, "resolve_target_workspace", forbidden)
        monkeypatch.setattr(finalizer._workspace_resolver, "query_target_workspace", forbidden)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed"
        assert accepted.manifest_ref == manifest.manifest_ref
        assert graph.calls == 1
        assert not saved[0].parent.exists()
        assert (pending / "artifact-0000").read_bytes() == b"still pending"
        assert (workspace / "logs/train.log").is_file()
        # Cleanup is idempotent, and the accepted copy remains readable.
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        manifest = memory.query(accepted.manifest_ref)
        log = next(entry for entry in manifest.entries if entry.declared_relative_path == "logs/train.log")
        assert memory._asset_memory.materialize_asset(log.binding.version_ref).content == b"epoch 1 complete\n"


@pytest.mark.parametrize("changed", ["frozen_bytes", "frozen_directory", "inline_bytes", "frozen_symlink", "operation"])
def test_pending_frozen_replay_keeps_content_and_identity_checks(tmp_path, monkeypatch, changed):
    with _pending_completion(tmp_path, monkeypatch, directory=changed == "frozen_directory") as values:
        lifecycle, memory, handle, workspace, evidence, completion, saved, finalizer = values
        if changed == "frozen_bytes":
            next(path for path in saved if path.is_file()).write_bytes(b"tampered original")
        elif changed == "frozen_directory":
            (next(path for path in saved if path.is_dir()) / "unexpected.txt").write_text("changed tree")
        elif changed == "inline_bytes":
            (workspace / "implementation/train.py").write_text("print('changed')\n")
        elif changed == "frozen_symlink":
            path = next(path for path in saved if path.is_file())
            path.unlink()
            path.symlink_to(workspace / "logs/train.log")
        else:
            evidence = replace(evidence, operation_ref="different-operation")
            finalizer._evidence_reader = _EvidenceReader(evidence)
        with pytest.raises(OwnerConflict):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert lifecycle.query_completion(handle.target_ref) == completion
        assert memory.query_for_completion(completion.completion_ref) is None
