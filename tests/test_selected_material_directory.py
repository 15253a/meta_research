"""Selected Writing inputs reuse exact bytes with one topology inspection."""
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import event

from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root


@pytest.fixture
def selected_directory(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    for index in range(256):
        (source / f"entry-{index:03d}.txt").write_bytes(b"accepted evidence\n" * 4)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    try:
        memory = runtime.owners.research_memory
        accepted = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="directory", custody_mode="managed", display_name="evidence",
            source_locator=str(source)), idempotency_key="selected-directory")
        assert accepted.status == "accepted", accepted.failure_code
        asset = accepted.asset
        request = WritingRequest(requirements="Preserve the accepted evidence.", materials=[{
            "version_ref": asset.version_ref, "content_hash": asset.content_hash, "role": "source"}])

        def prepare():
            return writing_message(request, "Read the selected evidence.", memory,
                workspace_root=tmp_path / "workspace", input_key="same-writing-request")

        message = prepare()
        task, _ = json.JSONDecoder().raw_decode(message.split("写作任务（普通助手会话）：\n", 1)[1])
        destination = Path(task["materials"][0]["path"])
        yield runtime, prepare, destination, message
    finally:
        runtime.close()


def test_selected_directory_retry_walks_once_and_still_hashes_every_file(
    selected_directory, monkeypatch,
):
    runtime, prepare, destination, message = selected_directory
    counts = {"rglob_calls": 0, "topology_entries": 0, "file_hash_calls": 0,
              "file_hash_bytes": 0, "sql_calls": 0}
    rglob, file_digest = Path.rglob, hashlib.file_digest

    def walk(path, *args, **kwargs):
        if path != destination:
            yield from rglob(path, *args, **kwargs)
            return
        counts["rglob_calls"] += 1
        for entry in rglob(path, *args, **kwargs):
            counts["topology_entries"] += 1
            yield entry

    class CountedInput:
        def __init__(self, stream):
            self.stream = stream

        def readable(self):
            return self.stream.readable()

        def readinto(self, buffer):
            count = self.stream.readinto(buffer)
            counts["file_hash_bytes"] += count
            return count

    def digest(stream, algorithm):
        counts["file_hash_calls"] += 1
        return file_digest(CountedInput(stream), algorithm)

    def sql(*_args):
        counts["sql_calls"] += 1

    monkeypatch.setattr(Path, "rglob", walk)
    monkeypatch.setattr(hashlib, "file_digest", digest)
    event.listen(runtime._database._engine, "before_cursor_execute", sql)
    try:
        start = perf_counter()
        replay = prepare()
        counts["elapsed_seconds"] = perf_counter() - start
    finally:
        event.remove(runtime._database._engine, "before_cursor_execute", sql)
    print("SELECTED_DIRECTORY_WORK " + json.dumps(counts, sort_keys=True))
    assert replay == message
    assert counts["file_hash_calls"] == 256
    assert counts["file_hash_bytes"] == 18432
    assert counts["rglob_calls"] == 1
    assert counts["topology_entries"] == 256


@pytest.mark.parametrize("change", [
    "same_size_and_mtime", "missing_file", "extra_file", "file_replaced_by_directory",
    "file_symlink", "extra_directory_symlink", "destination_symlink", "destination_file",
])
def test_selected_directory_retry_rejects_changed_paths_and_recovers_without_overwrite(
    selected_directory, tmp_path, change,
):
    _runtime, prepare, destination, message = selected_directory
    selected = destination / "entry-000.txt"
    original = selected.read_bytes()
    before = selected.stat()
    saved = tmp_path / "saved-export"
    extra = destination / "unselected"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(original)
    code = "writing_input_changed"

    if change == "same_size_and_mtime":
        selected.write_bytes(b"!" + original[1:])
        os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "missing_file":
        selected.unlink()
    elif change == "extra_file":
        extra.write_bytes(b"not selected")
    elif change == "file_replaced_by_directory":
        selected.unlink()
        selected.mkdir()
    elif change == "file_symlink":
        selected.unlink()
        selected.symlink_to(outside)
    elif change == "extra_directory_symlink":
        empty = tmp_path / "unselected-empty-directory"
        empty.mkdir()
        extra.symlink_to(empty, target_is_directory=True)
    else:
        destination.rename(saved)
        if change == "destination_symlink":
            destination.symlink_to(saved, target_is_directory=True)
            code = "writing_input_path_invalid"
        else:
            destination.write_bytes(b"a file replaced the directory")

    with pytest.raises(OwnerConflict, match="^" + code + "$"):
        prepare()
    assert outside.read_bytes() == original
    if change == "same_size_and_mtime":
        assert selected.read_bytes() == b"!" + original[1:]
        selected.write_bytes(original)
    elif change == "missing_file":
        assert not selected.exists()
        selected.write_bytes(original)
    elif change in {"extra_file", "extra_directory_symlink"}:
        extra.unlink()
    elif change == "file_replaced_by_directory":
        selected.rmdir()
        selected.write_bytes(original)
    elif change == "file_symlink":
        assert selected.is_symlink()
        selected.unlink()
        selected.write_bytes(original)
    else:
        destination.unlink()
        saved.rename(destination)
    assert prepare() == message


def test_failed_topology_read_is_not_remembered_as_a_success(selected_directory, monkeypatch):
    _runtime, prepare, destination, message = selected_directory
    rglob = Path.rglob

    def unavailable(path, *args, **kwargs):
        if path == destination:
            raise OSError("temporary directory read failure")
        return rglob(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rglob", unavailable)
        with pytest.raises(OwnerConflict, match="^writing_input_changed$"):
            prepare()
    assert prepare() == message
