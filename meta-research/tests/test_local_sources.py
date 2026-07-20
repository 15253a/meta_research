"""Local-machine source selection stays safe and path-free at the Web edge."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orchestrator import local_sources as local_sources_module
from orchestrator.local_sources import (
    LocalSourceChangedError,
    LocalSourceConflictError,
    LocalSourceError,
    LocalSourceRegistry,
)


_DRAFT = "d" * 32


def _canonical_on_disk(path: Path) -> dict:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    assert raw == (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n").encode("utf-8")
    return value


def test_attach_is_durable_idempotent_and_public_values_are_path_free(tmp_path):
    data = tmp_path / "private-dataset"
    (data / "subject-01").mkdir(parents=True)
    (data / "subject-01" / "eeg.bin").write_bytes(b"eeg bytes")
    (data / "README.md").write_text("reference notes", encoding="utf-8")
    root = tmp_path / "state" / "local-sources"
    registry = LocalSourceRegistry(root, allowed_roots=[tmp_path])

    attached = registry.attach(_DRAFT, "dataset", data, "1" * 32)
    assert attached == {
        "source_id": attached["source_id"],
        "label": "private-dataset",
        "kind": "dataset",
        "file_count": 2,
        "total_bytes": len(b"eeg bytes") + len(b"reference notes"),
        "status": "attached",
    }
    encoded = json.dumps(attached, ensure_ascii=False)
    assert str(data) not in encoded
    assert "source_path" not in encoded and "source_root" not in encoded
    assert registry.list(_DRAFT) == [attached]

    # Both the idempotency binding and attachment are canonical, private and
    # restart-safe.
    receipt_path = root / "attach-requests" / f"{'1' * 32}.json"
    receipt = _canonical_on_disk(receipt_path)
    attachment_path = root / "attachments" / f"{attached['source_id']}.json"
    assert _canonical_on_disk(attachment_path) == receipt["record"]
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(attachment_path.stat().st_mode) == 0o600
    restarted = LocalSourceRegistry(root, allowed_roots=[tmp_path])
    assert restarted.attach(_DRAFT, "dataset", data, "1" * 32) == attached
    with pytest.raises(LocalSourceConflictError):
        restarted.attach(_DRAFT, "references", data, "1" * 32)

    # Receipt-first publication can recover the only expected crash window.
    attachment_path.unlink()
    assert LocalSourceRegistry(root, allowed_roots=[tmp_path]).attach(
        _DRAFT, "dataset", data, "1" * 32) == attached
    assert _canonical_on_disk(attachment_path) == receipt["record"]


def test_concurrent_registries_share_one_durable_idempotency_binding(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "data.bin").write_bytes(b"data")
    root = tmp_path / "registry"

    def attach_once(_index):
        return LocalSourceRegistry(root, allowed_roots=[tmp_path]).attach(
            _DRAFT, "dataset", source, "a" * 32)["source_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        source_ids = list(pool.map(attach_once, range(24)))
    assert len(set(source_ids)) == 1
    restarted = LocalSourceRegistry(root, allowed_roots=[tmp_path])
    assert len(restarted.list(_DRAFT)) == 1


def test_preflight_is_metadata_only_and_verified_manifest_hashes_content(
        tmp_path, monkeypatch):
    source = tmp_path / "papers"
    source.mkdir()
    first = source / "one.txt"
    second = source / "nested" / "two.pdf"
    second.parent.mkdir()
    first.write_bytes(b"paper one")
    second.write_bytes(b"paper two")
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])

    target_inodes = {first.stat().st_ino, second.stat().st_ino}
    reads = []
    original_read = local_sources_module.os.read

    def tracking_read(fd, amount):
        if os.fstat(fd).st_ino in target_inodes:
            reads.append(fd)
        return original_read(fd, amount)

    monkeypatch.setattr(local_sources_module.os, "read", tracking_read)
    registry.attach(_DRAFT, "references", source, "2" * 32)
    internal = registry.preflight_manifest(_DRAFT)
    assert reads == []
    assert internal["status"] == "preflighted"
    assert internal["sources"][0]["source_path"] == str(source)
    assert internal["sources"][0]["source_root"] == str(source)
    assert [row["path"] for row in internal["sources"][0]["files"]] == [
        "nested/two.pdf", "one.txt",
    ]
    assert all("sha256" not in row for row in internal["sources"][0]["files"])

    public = registry.public_preflight(_DRAFT)
    assert str(source) not in json.dumps(public, ensure_ascii=False)
    assert set(public["sources"][0]) == {
        "source_id", "label", "kind", "file_count", "total_bytes", "status",
    }
    assert reads == []

    verified = registry.verified_manifest(_DRAFT)
    assert reads
    assert verified["status"] == "verified"
    by_path = {
        row["path"]: row for row in verified["sources"][0]["files"]
    }
    assert by_path["one.txt"]["sha256"] == (
        "sha256:" + hashlib.sha256(b"paper one").hexdigest())
    assert by_path["nested/two.pdf"]["sha256"] == (
        "sha256:" + hashlib.sha256(b"paper two").hexdigest())


def test_relative_tilde_and_absolute_inputs_are_normalized_under_allow_roots(
        tmp_path, monkeypatch):
    local = tmp_path / "workspace"
    home = tmp_path / "home"
    local.mkdir()
    home.mkdir()
    (local / "relative.txt").write_bytes(b"relative")
    (home / "home.txt").write_bytes(b"home")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")

    monkeypatch.chdir(local)
    monkeypatch.setenv("HOME", str(home))
    default_registry = LocalSourceRegistry(local / "registry")
    relative = default_registry.attach(
        _DRAFT, "references", "./relative.txt", "3" * 32)
    assert relative["label"] == "relative.txt"
    with pytest.raises(LocalSourceError, match="outside"):
        default_registry.attach(
            _DRAFT, "references", outside, "4" * 32)

    broad = LocalSourceRegistry(
        local / "registry-broad", allowed_roots=[tmp_path])
    home_attached = broad.attach(
        _DRAFT, "references", "~/home.txt", "5" * 32)
    absolute = broad.attach(
        _DRAFT, "dataset", outside, "6" * 32)
    assert home_attached["label"] == "home.txt"
    assert absolute["label"] == "outside.txt"


def test_full_machine_access_requires_an_explicit_root_allowlist(tmp_path):
    selected = tmp_path / "selected.txt"
    selected.write_bytes(b"selected")
    service_cwd = tmp_path / "service"
    service_cwd.mkdir()
    old_cwd = Path.cwd()
    try:
        os.chdir(service_cwd)
        restricted = LocalSourceRegistry(service_cwd / "restricted")
        with pytest.raises(LocalSourceError):
            restricted.attach(
                _DRAFT, "dataset", selected, "7" * 32)
        local_machine = LocalSourceRegistry(
            service_cwd / "machine", allowed_roots=["/"])
        assert local_machine.attach(
            _DRAFT, "dataset", selected, "8" * 32)["file_count"] == 1
    finally:
        os.chdir(old_cwd)


def test_symlinks_and_special_files_fail_closed_without_opening_them(tmp_path):
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.bin").write_bytes(b"data")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(LocalSourceError, match="symbolic link"):
        registry.attach(_DRAFT, "dataset", link, "9" * 32)

    nested_link = real / "nested-link"
    nested_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(LocalSourceError, match="symbolic link"):
        registry.attach(_DRAFT, "dataset", real, "a" * 32)
    nested_link.unlink()

    fifo = real / "stream.fifo"
    os.mkfifo(fifo)
    with pytest.raises(LocalSourceError, match="FIFO"):
        registry.attach(_DRAFT, "dataset", real, "b" * 32)
    fifo.unlink()

    sock_path = real / "service.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        with pytest.raises(LocalSourceError, match="socket"):
            registry.attach(_DRAFT, "dataset", real, "c" * 32)
    finally:
        server.close()


def test_source_and_registry_storage_must_not_overlap(tmp_path):
    root = tmp_path / "app-state" / "local-sources"
    registry = LocalSourceRegistry(root, allowed_roots=[tmp_path])
    with pytest.raises(LocalSourceError, match="overlaps"):
        registry.attach(_DRAFT, "references", tmp_path, "d" * 32)
    with pytest.raises(LocalSourceError, match="overlaps"):
        registry.attach(_DRAFT, "references", root, "e" * 32)


@pytest.mark.parametrize(
    ("constant", "value", "fixture_kind"),
    [
        ("_MAX_FILES", 1, "two-files"),
        ("_MAX_ENTRIES", 1, "two-files"),
        ("_MAX_FILE_BYTES", 3, "large-file"),
        ("_MAX_TOTAL_BYTES", 3, "large-file"),
        ("_MAX_DEPTH", 0, "nested"),
    ],
)
def test_enumeration_budgets_are_enforced(
        tmp_path, monkeypatch, constant, value, fixture_kind):
    source = tmp_path / "bounded"
    source.mkdir()
    if fixture_kind == "two-files":
        (source / "a").write_bytes(b"a")
        (source / "b").write_bytes(b"b")
    elif fixture_kind == "large-file":
        (source / "large").write_bytes(b"1234")
    else:
        (source / "nested").mkdir()
        (source / "nested" / "file").write_bytes(b"x")
    monkeypatch.setattr(local_sources_module, constant, value)
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])
    with pytest.raises(LocalSourceError, match="budget"):
        registry.attach(_DRAFT, "dataset", source, "f" * 32)


def test_verification_detects_file_stat_and_inode_drift(tmp_path, monkeypatch):
    source = tmp_path / "changing"
    source.mkdir()
    target = source / "payload.bin"
    target.write_bytes(b"original payload")
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])
    registry.attach(_DRAFT, "dataset", source, "0" * 32)

    inode = target.stat().st_ino
    original_read = local_sources_module.os.read
    mutated = False

    def mutating_read(fd, amount):
        nonlocal mutated
        payload = original_read(fd, amount)
        if os.fstat(fd).st_ino == inode and payload and not mutated:
            mutated = True
            with target.open("ab") as stream:
                stream.write(b" changed")
                stream.flush()
                os.fsync(stream.fileno())
        return payload

    monkeypatch.setattr(local_sources_module.os, "read", mutating_read)
    with pytest.raises(LocalSourceChangedError):
        registry.verified_manifest(_DRAFT)


def test_metadata_scan_detects_directory_entry_drift(tmp_path, monkeypatch):
    source = tmp_path / "changing-dir"
    source.mkdir()
    (source / "first").write_bytes(b"1")
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])
    inode = source.stat().st_ino
    original_listdir = local_sources_module.os.listdir
    mutated = False

    def mutating_listdir(path_or_fd):
        nonlocal mutated
        names = original_listdir(path_or_fd)
        if (isinstance(path_or_fd, int)
                and os.fstat(path_or_fd).st_ino == inode and not mutated):
            mutated = True
            (source / "second").write_bytes(b"2")
        return names

    monkeypatch.setattr(local_sources_module.os, "listdir", mutating_listdir)
    with pytest.raises(LocalSourceChangedError):
        registry.attach(_DRAFT, "dataset", source, "1" * 31 + "2")


@pytest.mark.parametrize("kind", ["data", "reference", "", None])
def test_attachment_shape_is_closed(tmp_path, kind):
    source = tmp_path / "file"
    source.write_bytes(b"x")
    registry = LocalSourceRegistry(
        tmp_path / "registry", allowed_roots=[tmp_path])
    with pytest.raises(ValueError):
        registry.attach(_DRAFT, kind, source, "2" * 32)
