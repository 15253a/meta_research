from __future__ import annotations

import errno
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
import meta_research.owners.research_memory as rm_module
from meta_research.paths import prepare_data_root


@pytest.mark.parametrize("entry_only", [False, True])
@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "mkdir"])
def test_destination_failure_is_reported_without_blame_on_accepted_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_only: bool, failure: str,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        result = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="file", custody_mode="managed", display_name="data.txt",
            content=b"exact accepted upstream data\n",
        ), idempotency_key="destination-error")
        assert result.asset is not None
        destination = tmp_path / "delivery" / "input.txt"
        export = memory.export_asset_entry if entry_only else memory.export_asset
        original_open, original_mkdir = Path.open, Path.mkdir
        attempts = []

        def fail():
            attempts.append(failure)
            raise OSError(errno.EDQUOT, "injected destination quota exhausted")

        class Output:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                self.raw.__enter__()
                return self

            def __exit__(self, *args):
                return self.raw.__exit__(*args)

            def write(self, value):
                if failure == "write":
                    fail()
                return self.raw.write(value)

            def flush(self):
                if failure == "flush":
                    fail()
                return self.raw.flush()

            def fileno(self):
                return self.raw.fileno()

        def open_path(path, mode="r", *args, **kwargs):
            raw = original_open(path, mode, *args, **kwargs)
            return Output(raw) if mode == "wb" and path.name == "content" else raw

        def mkdir(path, *args, **kwargs):
            if path == destination.parent:
                fail()
            return original_mkdir(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", open_path)
            if failure == "fsync":
                patch.setattr(rm_module.os, "fsync", lambda _fd: fail())
            if failure == "mkdir":
                patch.setattr(Path, "mkdir", mkdir)
            with pytest.raises(OwnerConflict, match="^asset_export_destination_unavailable$") as caught:
                export(result.asset.version_ref, destination)
            assert isinstance(caught.value.__cause__, OSError)
            assert caught.value.__cause__.errno == errno.EDQUOT
        assert attempts == [failure]
        assert not destination.exists()
        assert not list(destination.parent.glob(".asset-*"))
        # Recovery uses the same accepted version and publishes only complete bytes.
        export(result.asset.version_ref, destination)
        assert destination.read_bytes() == b"exact accepted upstream data\n"
    finally:
        runtime.close()


@pytest.mark.parametrize("entry_only", [False, True])
def test_source_replaced_by_nonregular_file_uses_exact_alternate_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_only: bool,
) -> None:
    source = tmp_path / "accepted.txt"
    content = b"exact accepted source remains available\n"
    source.write_bytes(content)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        result = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="local_path", custody_mode="managed", display_name=source.name,
            source_locator=str(source),
        ), idempotency_key="source-type-race")
        assert result.asset is not None
        managed_path = memory._object_store / rm_module._managed_asset_object_path(
            result.asset.content_hash
        )
        assert managed_path.is_file()
        opened = []
        original_open = rm_module._open_asset_regular_file

        def open_source(path, unavailable_code):
            opened.append(path)
            if path == managed_path:
                # Simulate a source changing after enumeration. The real file
                # opener must reject it and the exporter must try the next
                # receipt-bound source, never classify it as a write failure.
                managed_path.unlink()
                managed_path.mkdir()
            return original_open(path, unavailable_code)

        destination = tmp_path / "delivery" / "input.txt"
        export = memory.export_asset_entry if entry_only else memory.export_asset
        with monkeypatch.context() as patch:
            patch.setattr(rm_module, "_open_asset_regular_file", open_source)
            export(result.asset.version_ref, destination)
        assert opened == [managed_path, source]
        assert destination.read_bytes() == content
        assert not list(destination.parent.glob(".asset-*"))
    finally:
        runtime.close()
