"""Real RM selection reads keep parent identity and exact child semantics."""
from dataclasses import replace
import shutil
import zipfile

import pytest
from sqlalchemy import text

from meta_research.content_selection import ContentSelection
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import (
    AssetIntakeRequest, SQLiteResearchMemory, SQLiteResearchMemoryReceiptVerifier,
)
from meta_research.paths import prepare_data_root


@pytest.fixture
def memory(tmp_path):
    root = prepare_data_root(tmp_path / "rm")
    upgrade_database(root.database)
    database = Database(root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    verifier = SQLiteResearchMemoryReceiptVerifier(database, root.objects)
    owner = SQLiteResearchMemory(database, root.objects, feed, None, None, verifier)
    yield owner
    database.close()


def intake(memory, path, *, custody="managed", key="parent"):
    result = memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="local_path", custody_mode=custody,
        display_name=path.name, source_locator=str(path),
    ), idempotency_key=key)
    assert result.asset is not None, result
    return result.asset.as_binding()


def tree(tmp_path):
    root = tmp_path / "source"
    (root / "single").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "single" / "config.txt").write_bytes(b"abcdef")
    (root / "sibling.txt").write_bytes(b"unselected")
    return root


@pytest.mark.parametrize("selected,kind,entries", [
    (None, "directory", ("sibling.txt", "single/config.txt")),
    ("single", "directory", ("config.txt",)),
    ("single/config.txt", "file", ("config.txt",)),
    ("empty", "directory", ()),
])
def test_native_selection_survives_original_deletion_and_exports(memory, tmp_path, selected, kind, entries):
    source = tree(tmp_path)
    binding = intake(memory, source)
    shutil.rmtree(source)
    description = memory.describe_selection(binding, selected)
    assert description.kind == kind
    assert tuple(entry.path for entry in description.entries) == entries
    assert description.asset_binding == binding
    assert ContentSelection.from_dict(ContentSelection(binding, selected).as_dict()) == ContentSelection(binding, selected)
    page = memory.read_selection_page(binding, selected, limit=1)
    assert page["kind"] == kind
    if kind == "directory":
        assert len(page["entries"]) <= 1
        assert len(page["directories"]) <= 1
    assert page["asset_binding"] == binding.as_dict()
    assert page["selection"]["path"] == selected
    output = tmp_path / "delivered"
    exported = memory.export_selection(binding, selected, output)
    assert exported.description == description
    assert output.is_dir() == (kind == "directory")
    if selected == "single":
        assert (output / "config.txt").read_bytes() == b"abcdef"
        assert memory.read_selection_page(binding, selected, entry_path="config.txt", offset=1, limit=3)["text"] == "bcd"
    if selected == "single/config.txt":
        assert output.read_bytes() == b"abcdef"
        assert memory.read_selection_page(binding, selected, offset=2, limit=2)["text"] == "cd"
    if selected is None:
        assert (output / "empty").is_dir()
    with memory._database.read() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM rm_asset_versions")).scalar_one() == 1


def test_directory_with_one_member_does_not_turn_into_file(memory, tmp_path):
    source = tmp_path / "single"
    source.mkdir()
    (source / "config.txt").write_bytes(b"one")
    binding = intake(memory, source)
    assert memory.read_asset_content_page(binding.version_ref)["kind"] == "directory"
    assert memory.read_selection_page(binding)["kind"] == "directory"


def test_outside_or_missing_selection_is_rejected(memory, tmp_path):
    binding = intake(memory, tree(tmp_path))
    for path in ("../single", "/single", "single/", "single//config.txt", "single/../sibling.txt",
                 "single\\config.txt", "C:/single", "", ".", "missing"):
        with pytest.raises(OwnerConflict):
            memory.describe_selection(binding, path)


def test_entry_path_is_relative_to_selection_and_offsets_are_bounded(memory, tmp_path):
    binding = intake(memory, tree(tmp_path))
    nested = memory.read_selection_page(binding, entry_path="single")
    assert nested["kind"] == "directory"
    assert [entry["path"] for entry in nested["entries"]] == ["single/config.txt"]
    assert memory.read_selection_page(binding, entry_path="empty")["entries"] == []
    for path in ("../sibling.txt", "single/config.txt", "sibling.txt"):
        with pytest.raises(OwnerConflict):
            memory.read_selection_page(binding, "single", entry_path=path)
    with pytest.raises(OwnerConflict, match="content_offset_invalid"):
        memory.read_selection_page(binding, "single/config.txt", offset=7)
    with pytest.raises(OwnerConflict, match="content_offset_invalid"):
        memory.read_selection_page(binding, "empty", offset=1)


def test_parent_identity_and_receipt_are_never_replaced_with_selected_hash(memory, tmp_path):
    binding = intake(memory, tree(tmp_path))
    selected_hash = memory.describe_selection(binding, "single").selected_hash
    with pytest.raises(OwnerConflict):
        memory.describe_selection(replace(binding, content_hash=selected_hash), "single")
    with pytest.raises(OwnerConflict):
        memory.describe_selection(replace(binding, version_ref="asset_version_wrong"), "single")
    with memory._database.write() as connection:
        connection.execute(text("UPDATE rm_asset_versions SET receipt_hash=:hash WHERE version_ref=:ref"),
                           {"hash": "0" * 64, "ref": binding.version_ref})
    with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
        memory.read_selection_page(binding, "single/config.txt")


def test_selected_read_verifies_actual_bytes_on_each_page(memory, tmp_path):
    source = tree(tmp_path)
    binding = intake(memory, source, custody="linked_local")
    assert memory.read_selection_page(binding, "single/config.txt", limit=2)["text"] == "ab"
    (source / "single" / "config.txt").write_bytes(b"xbcdef")
    with pytest.raises(OwnerConflict, match="content_drifted"):
        memory.read_selection_page(binding, "single/config.txt", offset=2, limit=2)


def test_parent_sibling_does_not_change_selected_content_fingerprint(memory, tmp_path):
    source = tree(tmp_path)
    first = intake(memory, source)
    (source / "sibling.txt").write_bytes(b"changed")
    second = intake(memory, source, key="second")
    assert first.content_hash != second.content_hash
    assert memory.describe_selection(first, "single").selected_hash == memory.describe_selection(second, "single").selected_hash


def test_zip_asset_remains_an_opaque_file(memory, tmp_path):
    source = tmp_path / "implementation.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("single/config.txt", b"abcdef")
    binding = intake(memory, source)
    assert memory.describe_selection(binding).kind == "file"
    output = tmp_path / "exported.zip"
    memory.export_selection(binding, None, output)
    assert output.read_bytes() == source.read_bytes()
    with pytest.raises(OwnerConflict, match="content_selection_not_found"):
        memory.describe_selection(binding, "single")
