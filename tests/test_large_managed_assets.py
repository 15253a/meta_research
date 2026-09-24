from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tracemalloc

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
import meta_research.owners.research_memory as rm_module
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root


def _sparse_file(path: Path, size: int, marker: bytes = b"x") -> None:
    with path.open("wb") as output:
        output.seek(size - 1)
        output.write(marker)


def _file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@pytest.mark.parametrize("size", [65 * 1024 * 1024, 2049 * 1024 * 1024],
                         ids=["65-mib", "over-2-gib"])
def test_large_file_is_streamed_immutable_replayable_and_exportable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int,
) -> None:
    source = tmp_path / "checkpoint.bin"
    _sparse_file(source, size)
    original_hash = _file_hash(source)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    request = AssetIntakeRequest(
        source_kind="system_artifact", custody_mode="managed",
        display_name="checkpoint.bin", source_locator=str(source),
    )
    try:
        def no_in_memory_corpus(*_args, **_kwargs):
            raise AssertionError("large corpus must not use bytes materialization")

        monkeypatch.setattr(rm_module, "_read_exact_file", no_in_memory_corpus)
        tracemalloc.start()
        result = memory.submit_asset_intake(request, idempotency_key="large-checkpoint")
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result.status == "accepted"
        assert result.asset is not None
        assert result.asset.byte_count == size
        assert result.asset.content_hash == original_hash
        assert peak < 16 * 1024 * 1024

        source.write_bytes(b"later Target work changed the old workspace file")
        replay = memory.submit_asset_intake(request, idempotency_key="large-checkpoint")
        assert replay == result
        description = memory.describe_asset_export(result.asset.version_ref)
        assert description.kind == "file"
        assert description.entries[0].sha256 == original_hash
        assert description.entries[0].size == size
        exported = memory.export_asset(result.asset.version_ref, tmp_path / "input.bin")
        assert exported.description == description
        assert _file_hash(exported.path) == original_hash
        assert exported.path.stat().st_size == size
        assert memory.query_asset_inventory_item(result.asset.version_ref).integrity == "verified"
        with pytest.raises(OwnerConflict, match="asset_materialization_unsupported"):
            memory.materialize_asset(result.asset.version_ref)
        with pytest.raises(OwnerConflict, match="asset_export_destination_exists"):
            memory.export_asset(result.asset.version_ref, exported.path)
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        runtime.close()


def test_dataset_directory_over_256_mib_preserves_tree_without_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "processed-dataset"
    (source / "empty").mkdir(parents=True)
    for index in range(3):
        _sparse_file(source / f"part-{index}.bin", 90 * 1024 * 1024, bytes([index]))
    expected = {path.name: _file_hash(path) for path in source.glob("*.bin")}
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        monkeypatch.setattr(rm_module, "MAX_ASSET_FILES", 2)
        result = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="directory", custody_mode="managed",
            display_name="processed-dataset", source_locator=str(source),
            asynchronous=True,
        ), idempotency_key="large-dataset")
        assert result.status == "queued"
        assert memory.process_asset_intake_once()
        result = memory.query_asset_intake(result.job_ref)
        assert result.status == "accepted"
        assert result.asset is not None
        assert result.asset.byte_count == 270 * 1024 * 1024
        source.joinpath("part-1.bin").unlink()
        export = memory.export_asset(result.asset.version_ref, tmp_path / "reused-dataset")
        assert export.description.kind == "directory"
        assert export.description.directories == ("empty",)
        assert (export.path / "empty").is_dir()
        assert {path.name: _file_hash(path) for path in export.path.glob("*.bin")} == expected
        assert sorted(path.name for path in export.path.iterdir()) == [
            "empty", "part-0.bin", "part-1.bin", "part-2.bin",
        ]
    finally:
        runtime.close()


def test_description_is_metadata_only_and_export_detects_actual_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        result = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="file", custody_mode="managed", display_name="data.txt",
            content=b"exact data\n",
        ), idempotency_key="export-corruption")
        assert result.asset is not None
        with runtime._database.read() as connection:
            manifest_json = connection.execute(text(
                "SELECT manifest_json FROM rm_asset_versions WHERE version_ref = :ref"
            ), {"ref": result.asset.version_ref}).scalar_one()
        entry = json.loads(manifest_json)["entries"][0]
        object_path = memory._object_store / entry["object_path"]
        object_path.write_bytes(b"corrupted\n")

        def no_corpus_probe(*_args, **_kwargs):
            raise AssertionError("metadata description read corpus bytes")

        monkeypatch.setattr(rm_module, "_asset_current_state", no_corpus_probe)
        monkeypatch.setattr(rm_module, "_file_matches", no_corpus_probe)
        description = memory.describe_asset_export(result.asset.version_ref)
        assert description.content_hash == result.asset.content_hash
        destination = tmp_path / "bad-input.txt"
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            memory.export_asset(result.asset.version_ref, destination)
        assert not destination.exists()
        assert not list(tmp_path.glob(".asset-export-*"))
    finally:
        runtime.close()


def test_large_linked_dataset_can_gain_stable_managed_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "linked.bin"
    source.write_bytes(b"valuable dataset" * 1024)
    expected_hash = _file_hash(source)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        monkeypatch.setattr(rm_module, "MAX_ASSET_BYTES", 1024)
        result = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="local_path", custody_mode="linked_local",
            display_name="linked.bin", source_locator=str(source), asynchronous=True,
        ), idempotency_key="large-linked")
        memory.process_asset_intake_once()
        result = memory.query_asset_intake(result.job_ref)
        assert result.status == "accepted"
        assert result.asset is not None
        memory.verify_asset_binding(
            asset_ref=result.asset.asset_ref,
            version_ref=result.asset.version_ref,
            content_hash=result.asset.content_hash,
            manifest_hash=result.asset.manifest_hash,
            receipt=result.asset.receipt,
        )
        handoff = memory.handoff_asset_to_managed(
            result.asset.version_ref, idempotency_key="preserve-large-linked"
        )
        source.unlink()
        assert handoff.custody_mode == "managed"
        assert memory.handoff_asset_to_managed(
            result.asset.version_ref, idempotency_key="preserve-large-linked"
        ) == handoff
        export = memory.export_asset(result.asset.version_ref, tmp_path / "preserved.bin")
        assert _file_hash(export.path) == expected_hash
    finally:
        runtime.close()


def test_partial_intake_replays_with_stable_locator_and_recovers_queued_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "artifact-0000"
    second = tmp_path / "artifact-0001"
    first.write_bytes(b"accepted dataset snapshot")
    second.write_bytes(b"checkpoint snapshot retained while queued")
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    def request(path: Path) -> AssetIntakeRequest:
        return AssetIntakeRequest(
            source_kind="local_path", custody_mode="managed",
            display_name=path.name, source_locator=str(path),
        )
    first_request, second_request = request(first), request(second)
    try:
        accepted = memory.submit_asset_intake(first_request, idempotency_key="part-0")
        assert accepted.status == "accepted"
        original_store = memory._store_asset_file

        def temporary_failure(source, size, expected_hash=None):
            raise OwnerConflict("asset_source_unavailable")

        monkeypatch.setattr(memory, "_store_asset_file", temporary_failure)
        queued = memory.submit_asset_intake(second_request, idempotency_key="part-1")
        assert queued.status == "queued"
        assert queued.attempt_count == 1
        monkeypatch.setattr(memory, "_store_asset_file", original_store)

        first.unlink()  # Accepted staging can disappear; its exact request stays replayable.
        assert memory.submit_asset_intake(first_request, idempotency_key="part-0") == accepted
        second_source_hash = _file_hash(second)
        retry_at = rm_module.time.time() + 5.0
        monkeypatch.setattr(rm_module.time, "time", lambda: retry_at)
        recovered = memory.submit_asset_intake(second_request, idempotency_key="part-1")
        assert recovered.status == "accepted"
        assert recovered.attempt_count == 2
        assert recovered.asset.content_hash == second_source_hash
        assert memory.submit_asset_intake(second_request, idempotency_key="part-1") == recovered

        different_locator = AssetIntakeRequest(
            source_kind="local_path", custody_mode="managed",
            display_name=second.name,
            source_locator=str(tmp_path / "new-random-snapshot"),
        )
        with pytest.raises(OwnerConflict, match="asset_intake_idempotency_conflict"):
            memory.submit_asset_intake(different_locator, idempotency_key="part-1")
    finally:
        runtime.close()


def test_terminal_intake_failure_requires_new_operation_after_storage_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "snapshot.bin"
    source.write_bytes(b"retained snapshot for explicit retry")
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    request = AssetIntakeRequest(
        source_kind="local_path", custody_mode="managed",
        display_name=source.name, source_locator=str(source),
    )
    try:
        original_store = memory._store_asset_file
        monkeypatch.setattr(rm_module, "ASSET_INTAKE_MAX_ATTEMPTS", 1)

        def unavailable(source, size, expected_hash=None):
            raise OwnerConflict("asset_source_unavailable")

        monkeypatch.setattr(memory, "_store_asset_file", unavailable)
        failed = memory.submit_asset_intake(request, idempotency_key="failed-intake")
        assert failed.status == "failed"
        assert failed.failure_code == "asset_intake_retry_exhausted"
        monkeypatch.setattr(memory, "_store_asset_file", original_store)
        assert memory.submit_asset_intake(request, idempotency_key="failed-intake") == failed
        recovery = memory.submit_asset_intake(request, idempotency_key="new-intake")
        assert recovery.status == "accepted"
        assert recovery.asset.content_hash == _file_hash(source)
    finally:
        runtime.close()
