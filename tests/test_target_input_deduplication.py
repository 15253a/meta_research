"""One accepted AssetVersion occupies one handoff payload across all Targets."""

from dataclasses import replace
import hashlib
import multiprocessing
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_memory import AssetExportDescription, AssetExportEntry, ExportedAsset
from meta_research.owners.target_run_runtime import SQLiteTargetRunAgentAuthority
from test_target_input_startup_cost import accepted_input, registered_input, _streamed_workspace
from test_target_root_frozen_inputs import _Workspace, _fixture


def _two_targets(tmp_path, accepted_input):
    runtime, admitted, binding = accepted_input
    original, frozen, first = _streamed_workspace(tmp_path, accepted_input)
    authority = runtime.target_run_authorities.research_memory
    targets = []
    for ordinal in range(2):
        handle = replace(original, target_ref=f"dedup-target-{ordinal}",
            target_run_ref=f"dedup-run-{ordinal}",
            accepted_input_asset_proofs=(SimpleNamespace(asset_ref=binding.asset_ref),))
        workspace = _Workspace(first._root.parent / f"target-{ordinal}", handle)
        workspace._workspace.workspace_ref = f"dedup-workspace-{ordinal}"
        workspace._memory = SimpleNamespace(
            describe_input_asset=lambda **_kw: authority.describe_input_asset(
                target_ref=admitted.target_ref, asset_ref=binding.asset_ref),
            describe_asset_export=authority.describe_asset_export,
            export_asset=authority.export_asset,
        )
        workspace._graph = SimpleNamespace(input_asset_source_refs=lambda _ref: {},
            selected_evidence_target_commits=lambda _ref: {})
        targets.append((handle, workspace))
    return targets, frozen


def test_exact_version_shared_by_direct_upstream_and_multiple_targets(
    tmp_path, accepted_input,
):
    targets, frozen = _two_targets(tmp_path, accepted_input)
    payloads = []
    for handle, workspace in targets:
        paths = workspace.materialize_target_workspace_inputs(
            handle=handle, accepted_target_commit_inputs=(frozen,))
        payloads.extend(Path(path) for path in paths
            if "/direct/" in path or "/artifacts/" in path)
        workspace.verify_target_workspace_inputs(
            handle=handle, accepted_target_commit_inputs=(frozen,))
    assert len(payloads) == 4
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in payloads}) == 1
    assert all(path.stat().st_mode & 0o222 == 0 for path in payloads)
    assert all(not path.is_symlink() for path in payloads)
    runtime, _handle, binding = accepted_input
    from meta_research.owners.research_memory import _managed_asset_object_path
    custody = runtime.owners.research_memory._object_store / _managed_asset_object_path(binding.content_hash)
    assert not os.path.samefile(payloads[0], custody)
    payloads[0].parent.chmod(0o700)
    payloads[0].unlink()
    assert payloads[1].read_bytes() == custody.read_bytes()


class _ExportMemory:
    """Tiny deterministic custody seam for crash/process tests, with real files."""
    def __init__(self, root, directory):
        self.content = b"immutable research payload\n"
        digest = hashlib.sha256(self.content).hexdigest()
        self.description = AssetExportDescription(memory_ref="asset-version-one",
            file_name="dataset" if directory else "payload.bin", media_type="application/octet-stream",
            kind="directory" if directory else "file", content_hash=digest,
            manifest_hash="a" * 64, byte_count=len(self.content),
            directories=("empty", "nested") if directory else (),
            entries=(AssetExportEntry(path="nested/payload.bin" if directory else "payload.bin",
                sha256=digest, size=len(self.content)),))
        self.calls = root / "exports.log"

    def describe_asset_export(self, ref):
        return replace(self.description, memory_ref=ref)

    def export_asset(self, ref, destination):
        with self.calls.open("ab") as output:
            output.write(b"export\n")
        time.sleep(0.05)
        description = self.describe_asset_export(ref)
        if description.kind == "directory":
            destination.mkdir()
            for name in description.directories:
                (destination / name).mkdir()
            (destination / "nested/payload.bin").write_bytes(self.content)
        else:
            destination.write_bytes(self.content)
        return ExportedAsset(description, destination)


def _small_targets(tmp_path, *, directory=False):
    original, frozen, first, *_rest = _fixture(tmp_path)
    memory = _ExportMemory(tmp_path, directory)
    description = memory.description
    artifact = replace(frozen.artifacts[0], artifact_kind=description.kind,
        declared_relative_path="outputs/data" if directory else "outputs/data.bin",
        media_type=description.media_type, version_ref=description.memory_ref,
        content_hash=description.content_hash, tree_hash=description.content_hash,
        content=None, export_description=description)
    frozen = replace(frozen, artifacts=(artifact, replace(artifact, ordinal=1)))
    targets = []
    for ordinal in range(2):
        handle = replace(original, target_ref=f"target-{ordinal}", target_run_ref=f"run-{ordinal}")
        workspace = _Workspace(first._root.parent / f"target-{ordinal}", handle)
        workspace._workspace.workspace_ref = f"workspace-{ordinal}"
        workspace._memory = memory
        targets.append((handle, workspace))
    return targets, frozen, memory


def _materialize(target, frozen):
    handle, workspace = target
    return workspace.materialize_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,))


@pytest.mark.parametrize("directory", [False, True])
def test_concurrent_processes_export_once_and_preserve_complete_readonly_tree(tmp_path, directory):
    targets, frozen, memory = _small_targets(tmp_path, directory=directory)
    context = multiprocessing.get_context("fork")
    gate = context.Event()
    def run(target):
        gate.wait(5)
        _materialize(target, frozen)
    processes = [context.Process(target=run, args=(target,)) for target in targets]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert memory.calls.read_bytes() == b"export\n"
    payloads = []
    for target in targets:
        paths = _materialize(target, frozen)
        payloads.extend(Path(path) for path in paths if "/artifacts/" in path)
        handle, workspace = target
        workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))
        if directory:
            assert len(list(Path(paths[0]).parent.rglob("empty"))) == 2
    assert len({path.stat().st_ino for path in payloads}) == 1
    assert all(path.stat().st_mode & 0o222 == 0 for path in payloads)


def test_distinct_exact_versions_do_not_share_even_when_content_matches(tmp_path):
    targets, frozen, memory = _small_targets(tmp_path)
    alternate = replace(frozen.artifacts[1], version_ref="asset-version-two",
        export_description=memory.describe_asset_export("asset-version-two"))
    frozen = replace(frozen, artifacts=(frozen.artifacts[0], alternate))
    paths = _materialize(targets[0], frozen)
    payloads = [Path(path) for path in paths if "/artifacts/" in path]
    assert payloads[0].read_bytes() == payloads[1].read_bytes()
    assert not os.path.samefile(*payloads)
    assert memory.calls.read_bytes() == b"export\nexport\n"


@pytest.mark.parametrize("interruption", ["cache_publish", "projection_link"])
def test_interrupted_publication_retries_without_partial_inputs(tmp_path, monkeypatch, interruption):
    targets, frozen, memory = _small_targets(tmp_path, directory=True)
    _handle, workspace = targets[0]
    rename = os.rename
    link = os.link
    def interrupt_rename(source, destination, *args, **kwargs):
        if Path(source).name.startswith(".staging-"):
            raise OSError(122, "injected cache publication interruption")
        return rename(source, destination, *args, **kwargs)
    def interrupt_link(source, destination, *args, **kwargs):
        if "nested" in Path(destination).parts:
            raise OSError(122, "injected projection interruption")
        return link(source, destination, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "rename" if interruption == "cache_publish" else "link",
            interrupt_rename if interruption == "cache_publish" else interrupt_link)
        with pytest.raises(OwnerConflict, match="target_run_workspace_input_storage_unavailable"):
            _materialize(targets[0], frozen)
    assert not list(tmp_path.rglob(".staging-*"))
    assert not list(tmp_path.rglob(".input-projection-*"))
    paths = _materialize(targets[0], frozen)
    assert paths
    workspace.verify_target_workspace_inputs(handle=targets[0][0], accepted_target_commit_inputs=(frozen,))
    assert memory.calls.read_bytes().count(b"export\n") == (2 if interruption == "cache_publish" else 1)


@pytest.mark.parametrize("drift", ["descriptor", "symlink", "size", "writable", "extra"])
def test_shared_cache_structure_drift_is_rejected_before_next_target(tmp_path, drift):
    targets, frozen, memory = _small_targets(tmp_path)
    _materialize(targets[0], frozen)
    workspace = targets[0][1]
    identity = workspace._target_input_asset_identity(memory.description)
    cached = workspace._target_input_asset_cache_root() / canonical_hash(identity)
    cached.chmod(0o700)
    payload = cached / "content"
    if drift == "descriptor":
        metadata = cached / "description.json"
        metadata.chmod(0o600)
        metadata.write_bytes(b"{}")
    elif drift == "symlink":
        payload.unlink()
        payload.symlink_to(memory.calls)
    elif drift == "size":
        payload.chmod(0o600)
        payload.write_bytes(b"wrong length")
        payload.chmod(0o400)
    elif drift == "writable":
        payload.chmod(0o600)
    else:
        (cached / "unlisted").write_bytes(b"x")
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        _materialize(targets[1], frozen)
    assert memory.calls.read_bytes() == b"export\n"


def test_version_lock_recovers_after_process_death_with_stale_staging(tmp_path):
    targets, frozen, memory = _small_targets(tmp_path, directory=True)
    workspace = targets[0][1]
    base = workspace._target_input_asset_cache_root()
    key = canonical_hash(workspace._target_input_asset_identity(memory.description))
    context = multiprocessing.get_context("fork")
    def crash():
        def interrupted_export(_ref, destination):
            destination.mkdir()
            (destination / "partial").write_bytes(b"interrupted")
            os._exit(31)
        workspace._memory.export_asset = interrupted_export
        _materialize(targets[0], frozen)
    process = context.Process(target=crash)
    process.start()
    process.join(10)
    assert process.exitcode == 31
    assert (base / (".staging-" + key) / "content/partial").exists()
    _materialize(targets[0], frozen)
    assert not (base / (".staging-" + key)).exists()
    assert memory.calls.read_bytes() == b"export\n"
