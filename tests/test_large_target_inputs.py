from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tracemalloc

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture
from test_target_root_frozen_inputs import _fixture


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@pytest.mark.parametrize("directory", [False, True])
def test_large_completed_artifact_reaches_next_target_as_read_only_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: bool,
) -> None:
    """Real completion/RM custody feeds the frozen-input authority seam."""
    runtime, lifecycle, memory, _authority, handle, root, evidence = _root_finalizer_fixture(tmp_path)
    source = root / "outputs" / ("dataset" if directory else "checkpoint.bin")
    if directory:
        (source / "empty").mkdir(parents=True)
        payload = source / "samples.bin"
    else:
        payload = source
    with payload.open("wb") as output:
        output.seek(65 * 1024 * 1024 - 1)
        output.write(b"x")
    expected_hash = _hash(payload)
    evidence = replace(evidence, handoff=replace(evidence.handoff,
        artifacts=evidence.handoff.artifacts + (TargetCompletionArtifact(
            role="analysis", relative_path=source.relative_to(root).as_posix()),)))
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence))
    try:
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted", accepted
        manifest = memory.query(accepted.manifest_ref)
        assert manifest is not None
        frozen = memory.materialize_target_commit_input(
            target_commit_ref="target-commit-upstream", manifest=manifest)
        exported_artifact = next(item for item in frozen.artifacts
                                 if item.declared_relative_path == source.relative_to(root).as_posix())
        assert exported_artifact.content is None
        assert exported_artifact.export_description.byte_count == 65 * 1024 * 1024

        # The next Target's admission is exercised at its already-authenticated
        # authority seam; the source manifest and all asset bytes are real RM.
        downstream_handle, _old, workspace, *_rest = _fixture(tmp_path / "next-target")
        workspace._memory = runtime.target_run_authorities.research_memory
        original_read_bytes = Path.read_bytes
        def bounded_read_bytes(path: Path):
            if path.stat().st_size > 64 * 1024 * 1024:
                raise AssertionError("large asset was loaded into memory")
            return original_read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", bounded_read_bytes)
        tracemalloc.start()
        paths = workspace.materialize_target_workspace_inputs(handle=downstream_handle,
            accepted_target_commit_inputs=(frozen,))
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 16 * 1024 * 1024
        input_manifest = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        commit = next(entry for entry in input_manifest["entries"] if entry["kind"] == "target_commit")
        item = next(item for item in commit["artifacts"]
                    if item["version_ref"] == exported_artifact.version_ref)
        delivered = Path(paths[0]).parent / item["relative_path"]
        assert not delivered.name.endswith(".zip")
        delivered_file = delivered / "samples.bin" if directory else delivered
        assert _hash(delivered_file) == expected_hash
        assert delivered_file.stat().st_mode & 0o222 == 0
        if directory:
            assert (delivered / "empty").is_dir()
        assert len(Path(paths[0]).read_bytes()) < 64 * 1024

        # Managed custody is independent of the old Target's working output.
        payload.write_bytes(b"workspace changed after completion")
        assert workspace.materialize_target_workspace_inputs(handle=downstream_handle,
            accepted_target_commit_inputs=(frozen,)) == paths
        workspace.verify_target_workspace_inputs(handle=downstream_handle,
            accepted_target_commit_inputs=(frozen,))
        delivered_file.chmod(0o600)
        with delivered_file.open("r+b") as output:
            output.write(b"tampered")
        with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
            workspace.verify_target_workspace_inputs(handle=downstream_handle,
                accepted_target_commit_inputs=(frozen,))
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        runtime.close()


def test_export_description_cannot_substitute_another_exact_version(tmp_path: Path) -> None:
    from meta_research.owners.research_memory import AssetExportDescription
    handle, frozen, workspace, *_rest = _fixture(tmp_path)
    artifact = frozen.artifacts[0]
    description = AssetExportDescription(memory_ref="another-version", file_name="result.json",
        media_type="application/json", kind="file", content_hash=artifact.content_hash,
        manifest_hash="a" * 64, byte_count=len(artifact.content), directories=(), entries=())
    forged = replace(frozen, artifacts=(replace(artifact, content=None,
        export_description=description),))
    with pytest.raises(OwnerConflict, match="target_root_upstream_input_invalid"):
        workspace.materialize_target_workspace_inputs(handle=handle,
            accepted_target_commit_inputs=(forged,))


def test_large_artifact_intake_retry_survives_completion_snapshot_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, lifecycle, memory, _authority, handle, root, evidence = _root_finalizer_fixture(tmp_path)
    source = root / "outputs" / "data.bin"
    with source.open("wb") as output:
        output.seek(65 * 1024 * 1024 - 1)
        output.write(b"x")
    evidence = replace(evidence, handoff=replace(evidence.handoff,
        artifacts=evidence.handoff.artifacts + (TargetCompletionArtifact(
            role="analysis", relative_path="outputs/data.bin"),)))
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence))
    owner = runtime.owners.research_memory
    original = owner.submit_asset_intake
    def interrupted_intake(request, **kwargs):
        result = original(request, **kwargs)
        if result.asset is not None and result.asset.byte_count == 65 * 1024 * 1024:
            raise OwnerConflict("injected_transient_after_large_asset_acceptance")
        return result
    try:
        monkeypatch.setattr(owner, "submit_asset_intake", interrupted_intake)
        with pytest.raises(OwnerConflict, match="injected_transient"):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert memory.query_for_completion(lifecycle.query_completion(handle.target_ref).completion_ref) is None
        before = len(owner.query_asset_inventory())
        monkeypatch.setattr(owner, "submit_asset_intake", original)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        assert len(owner.query_asset_inventory()) == before
    finally:
        runtime.close()


def test_queued_large_intake_keeps_stable_source_until_owner_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text
    import meta_research.owners.research_memory as rm_module
    runtime, lifecycle, memory, _authority, handle, root, evidence = _root_finalizer_fixture(tmp_path)
    source = root / "outputs" / "data.bin"
    with source.open("wb") as output:
        output.seek(65 * 1024 * 1024 - 1)
        output.write(b"x")
    evidence = replace(evidence, handoff=replace(evidence.handoff,
        artifacts=evidence.handoff.artifacts + (TargetCompletionArtifact(
            role="analysis", relative_path="outputs/data.bin"),)))
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence))
    owner = runtime.owners.research_memory
    original = owner._prepare_asset
    def interrupted_prepare(request):
        if request.get("source_locator"):
            raise RuntimeError("transient I/O interruption")
        return original(request)
    try:
        monkeypatch.setattr(owner, "_prepare_asset", interrupted_prepare)
        monkeypatch.setattr(rm_module, "ASSET_INTAKE_RETRY_BASE_SECONDS", 0)
        with pytest.raises(OwnerConflict, match="target_root_artifact_intake_unavailable"):
            finalizer.finalize(handle=handle, evidence=evidence)
        with runtime._database.read() as connection:
            row = connection.execute(text(
                "SELECT job_ref, request_json FROM rm_asset_intakes WHERE status='queued'"
            )).one()
        locator = Path(json.loads(row.request_json)["source_locator"])
        assert locator.exists()
        assert ".target-completion-intakes" in locator.parts
        monkeypatch.setattr(owner, "_prepare_asset", original)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        assert owner.query_asset_intake(row.job_ref).status == "accepted"
        assert not locator.exists()
    finally:
        runtime.close()
