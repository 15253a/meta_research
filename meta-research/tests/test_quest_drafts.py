"""Web-first quest draft storage: durability, replay, and filesystem closure."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orchestrator import quest_drafts as quest_drafts_module
from orchestrator.quest_drafts import (
    DraftConflictError,
    DraftCorruptError,
    QuestDraftRegistry,
)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _draft(registry: QuestDraftRegistry, *, key: str = "a" * 32,
           quest_id: str = "alpha") -> dict:
    return registry.create(
        {
            "quest_id": quest_id,
            "title": f"{quest_id.title()} research",
            "template_id": "default",
        },
        key,
    )


def _draft_root(registry: QuestDraftRegistry, draft_id: str) -> Path:
    return registry.root / "drafts" / draft_id


def _upload_root(registry: QuestDraftRegistry, draft_id: str,
                 relative: str) -> Path:
    token = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return _draft_root(registry, draft_id) / "incoming" / token


def test_create_is_durable_idempotent_and_exposes_only_public_summary(tmp_path):
    root = tmp_path / "draft-registry"
    registry = QuestDraftRegistry(root)
    spec = {
        "quest_id": "custom-research",
        "title": "Custom research",
        "goal_brief_md": "# Private goal\n\nDo not expose this body in summaries.\n",
        "qualification_profile_id": "local-safe",
    }
    key = "1" * 32
    runtime_profile = {
        "version": 1,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }
    effective_spec = {**spec, "runtime_profile": runtime_profile}

    created = registry.create(spec, key)
    assert re.fullmatch(r"[0-9a-f]{32}", created["draft_id"])
    assert created == {
        "draft_id": created["draft_id"],
        "quest_id": "custom-research",
        "title": "Custom research",
        "created_at": created["created_at"],
        "template_id": None,
        "qualification_profile_id": "local-safe",
        "runtime_profile": runtime_profile,
        "file_count": 0,
        "total_declared_bytes": 0,
    }
    assert "Private goal" not in json.dumps(created, ensure_ascii=False)

    draft_root = _draft_root(registry, created["draft_id"])
    assert stat.S_IMODE(draft_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((draft_root / "draft.json").stat().st_mode) == 0o600
    for name in ("incoming", "files"):
        assert stat.S_IMODE((draft_root / name).stat().st_mode) == 0o700
    receipt = root / "draft-create-requests" / f"{key}.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    restarted = QuestDraftRegistry(root)
    assert restarted.create(dict(spec), key) == created
    assert restarted.get(created["draft_id"]) == created
    assert restarted.list() == [created]
    private_spec = restarted.spec(created["draft_id"])
    assert private_spec == effective_spec
    assert private_spec["goal_brief_md"].startswith("# Private goal")
    private_spec["title"] = "caller mutation"
    assert restarted.spec(created["draft_id"]) == effective_spec
    assert restarted.files_root(created["draft_id"]) == draft_root / "files"
    with pytest.raises(DraftConflictError):
        restarted.create({**spec, "title": "Different"}, key)

    # The durable binding can reconstruct a draft lost before publication.
    shutil.rmtree(draft_root)
    recovered = QuestDraftRegistry(root).create(spec, key)
    assert recovered == created
    assert draft_root.is_dir()


def test_concurrent_constructors_and_create_calls_share_one_binding(tmp_path):
    root = tmp_path / "registry"
    spec = {
        "quest_id": "concurrent",
        "title": "Concurrent",
        "template_id": "default",
    }

    def create_once(_index):
        return QuestDraftRegistry(root).create(spec, "e" * 32)["draft_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        draft_ids = list(pool.map(create_once, range(24)))
    assert len(set(draft_ids)) == 1
    assert len(QuestDraftRegistry(root).list()) == 1


def test_legacy_create_receipt_accepts_explicit_default_profile_only(tmp_path):
    root = tmp_path / "registry"
    registry = QuestDraftRegistry(root)
    key = "f" * 32
    spec = {
        "quest_id": "legacy", "title": "Legacy",
        "template_id": "default",
    }
    created = registry.create(spec, key)
    draft_path = _draft_root(registry, created["draft_id"]) / "draft.json"
    receipt_path = root / "draft-create-requests" / f"{key}.json"

    def canonical(value):
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n")

    draft_record = json.loads(draft_path.read_text(encoding="utf-8"))
    draft_record["spec"].pop("runtime_profile")
    draft_path.write_text(canonical(draft_record), encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["spec"].pop("runtime_profile")
    receipt["spec_sha256"] = _digest(canonical(receipt["spec"]).encode("utf-8"))
    receipt_path.write_text(canonical(receipt), encoding="utf-8")

    default = {
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }
    replay = QuestDraftRegistry(root).create(
        {**spec, "runtime_profile": default}, key)
    assert replay["runtime_profile"] == default
    assert "runtime_profile" not in QuestDraftRegistry(root).spec(
        created["draft_id"])
    with pytest.raises(DraftConflictError, match="legacy"):
        QuestDraftRegistry(root).create({
            **spec,
            "runtime_profile": {
                "version": 1, "compute_profile_id": "local-cpu",
                "review_intensity": "once",
            },
        }, key)


@pytest.mark.parametrize("spec", [
    {"quest_id": "alpha", "title": "A"},
    {
        "quest_id": "alpha", "title": "A", "template_id": "default",
        "goal_brief_md": "# both",
    },
    {
        "quest_id": "alpha", "title": "A", "template_id": "default",
        "unknown": True,
    },
    {
        "quest_id": "alpha", "title": "A", "template_id": "default",
        "runtime_profile": {
            "version": 1, "compute_profile_id": "local-gpu",
            "review_intensity": "twice",
        },
    },
    {
        "quest_id": "alpha", "title": "A", "template_id": "default",
        "runtime_profile": {
            "version": 1, "compute_profile_id": "local-gpu",
            "review_intensity": "once", "gpu_index": 7,
        },
    },
])
def test_create_spec_has_an_exact_closed_shape(tmp_path, spec):
    registry = QuestDraftRegistry(tmp_path / "registry")
    with pytest.raises(ValueError):
        registry.create(spec, "2" * 32)


def test_chunk_replay_conflicts_finalize_and_restart(tmp_path):
    root = tmp_path / "registry"
    registry = QuestDraftRegistry(root)
    draft = _draft(registry)
    draft_id = draft["draft_id"]
    relative = "datasets/subject-01/data.bin"
    first = b"first chunk\x00"
    second = b"and the second chunk"
    payload = first + second

    assert registry.begin_file(draft_id, relative, len(payload)) == {
        "path": relative,
        "size": len(payload),
        "sha256": None,
        "status": "uploading",
    }
    assert registry.begin_file(draft_id, relative, len(payload))["status"] == "uploading"
    registry.append_chunk(
        draft_id, relative, 0, bytes=first, chunk_sha256=_digest(first))
    # Exact replay is idempotent, including after a registry restart.
    restarted = QuestDraftRegistry(root)
    restarted.append_chunk(
        draft_id, relative, 0, bytes=first, chunk_sha256=_digest(first))
    with pytest.raises(DraftConflictError):
        restarted.append_chunk(
            draft_id, relative, 0, bytes=b"different",
            chunk_sha256=_digest(b"different"))
    with pytest.raises(DraftConflictError):
        restarted.append_chunk(
            draft_id, relative, len(first) + 1, bytes=second,
            chunk_sha256=_digest(second))

    restarted.append_chunk(
        draft_id, relative, len(first), bytes=second,
        chunk_sha256=_digest(second))
    # Old offsets remain safely replayable after later chunks commit.
    restarted.append_chunk(
        draft_id, relative, 0, bytes=first, chunk_sha256=_digest(first))
    with pytest.raises(DraftConflictError):
        restarted.finalize_file(draft_id, relative, _digest(b"wrong"))

    completed = restarted.finalize_file(draft_id, relative, _digest(payload))
    assert completed == {
        "path": relative,
        "size": len(payload),
        "sha256": _digest(payload),
        "status": "complete",
    }
    assert restarted.finalize_file(draft_id, relative, _digest(payload)) == completed
    assert restarted.begin_file(draft_id, relative, len(payload)) == completed
    with pytest.raises(DraftConflictError):
        restarted.append_chunk(
            draft_id, relative, 0, bytes=first,
            chunk_sha256=_digest(first))

    final_path = _draft_root(restarted, draft_id) / "files" / relative
    assert final_path.read_bytes() == payload
    assert stat.S_IMODE(final_path.stat().st_mode) == 0o600
    assert QuestDraftRegistry(root).files_manifest(draft_id) == [completed]
    assert set(completed) == {"path", "size", "sha256", "status"}


def test_unreceipted_chunk_tail_is_truncated_before_retry(tmp_path):
    root = tmp_path / "registry"
    registry = QuestDraftRegistry(root)
    draft_id = _draft(registry)["draft_id"]
    relative = "retry.bin"
    payload = b"retry payload"
    registry.begin_file(draft_id, relative, len(payload))

    # Simulate power loss after data fsync but before the chunk receipt exists.
    data_path = _upload_root(registry, draft_id, relative) / "data.part"
    data_path.write_bytes(b"uncommitted tail")
    restarted = QuestDraftRegistry(root)
    restarted.append_chunk(
        draft_id, relative, 0, bytes=payload,
        chunk_sha256=_digest(payload))
    restarted.finalize_file(draft_id, relative, _digest(payload))
    assert (root / "drafts" / draft_id / "files" / relative).read_bytes() == payload


@pytest.mark.parametrize("relative", [
    "",
    "/absolute",
    "../escape",
    "a/../escape",
    "a/./file",
    "a//file",
    "a/file/",
    "a\\file",
    "a\x00file",
    ".hidden",
    "a/.hidden/file",
    "/".join(["a"] * 65),
    "测" * 342,
])
def test_upload_paths_are_strict_bounded_posix_relatives(tmp_path, relative):
    registry = QuestDraftRegistry(tmp_path / "registry")
    draft_id = _draft(registry)["draft_id"]
    with pytest.raises(ValueError):
        registry.begin_file(draft_id, relative, 1)


def test_size_chunk_and_draft_quotas_are_enforced(tmp_path, monkeypatch):
    registry = QuestDraftRegistry(tmp_path / "registry")
    draft_id = _draft(registry)["draft_id"]
    with pytest.raises(ValueError):
        registry.begin_file(draft_id, "negative.bin", -1)
    with pytest.raises(ValueError):
        registry.begin_file(
            draft_id, "too-large.bin", quest_drafts_module._MAX_FILE_BYTES + 1)

    registry.begin_file(draft_id, "chunk.bin", 1)
    with pytest.raises(ValueError):
        registry.append_chunk(
            draft_id, "chunk.bin", 0, bytes=b"", chunk_sha256=_digest(b""))
    oversized = b"x" * (quest_drafts_module._MAX_CHUNK_BYTES + 1)
    with pytest.raises(ValueError):
        registry.append_chunk(
            draft_id, "chunk.bin", 0, bytes=oversized,
            chunk_sha256=_digest(oversized))
    with pytest.raises(ValueError):
        registry.append_chunk(
            draft_id, "chunk.bin", 0, bytes=b"x", chunk_sha256="not-a-hash")
    with pytest.raises(ValueError):
        registry.append_chunk(
            draft_id, "chunk.bin", 0, bytes=b"x",
            chunk_sha256=_digest(b"y"))

    monkeypatch.setattr(quest_drafts_module, "_MAX_FILES", 2)
    limited = QuestDraftRegistry(tmp_path / "file-limit")
    limited_id = _draft(limited, key="b" * 32, quest_id="file-limit")["draft_id"]
    limited.begin_file(limited_id, "one", 0)
    limited.begin_file(limited_id, "two", 0)
    with pytest.raises(DraftConflictError):
        limited.begin_file(limited_id, "three", 0)

    monkeypatch.setattr(quest_drafts_module, "_MAX_FILES", 100)
    monkeypatch.setattr(quest_drafts_module, "_MAX_TOTAL_BYTES", 10)
    total = QuestDraftRegistry(tmp_path / "total-limit")
    total_id = _draft(total, key="c" * 32, quest_id="total-limit")["draft_id"]
    total.begin_file(total_id, "six", 6)
    total.begin_file(total_id, "four", 4)
    with pytest.raises(DraftConflictError):
        total.begin_file(total_id, "overflow", 1)


def test_file_ancestor_collisions_are_rejected(tmp_path):
    registry = QuestDraftRegistry(tmp_path / "registry")
    draft_id = _draft(registry)["draft_id"]
    registry.begin_file(draft_id, "dataset", 0)
    with pytest.raises(DraftConflictError):
        registry.begin_file(draft_id, "dataset/file.bin", 0)

    other_id = _draft(registry, key="d" * 32, quest_id="other")["draft_id"]
    registry.begin_file(other_id, "dataset/file.bin", 0)
    with pytest.raises(DraftConflictError):
        registry.begin_file(other_id, "dataset", 0)


def test_symlink_hardlink_and_unknown_entries_fail_closed(tmp_path):
    symlink_registry = QuestDraftRegistry(tmp_path / "symlink-registry")
    symlink_id = _draft(symlink_registry)["draft_id"]
    symlink_registry.begin_file(symlink_id, "data.bin", 1)
    data_path = _upload_root(symlink_registry, symlink_id, "data.bin") / "data.part"
    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    data_path.unlink()
    data_path.symlink_to(outside)
    with pytest.raises(DraftCorruptError):
        symlink_registry.append_chunk(
            symlink_id, "data.bin", 0, bytes=b"x",
            chunk_sha256=_digest(b"x"))

    hardlink_registry = QuestDraftRegistry(tmp_path / "hardlink-registry")
    hardlink_id = _draft(hardlink_registry, key="b" * 32)["draft_id"]
    hardlink_registry.begin_file(hardlink_id, "data.bin", 1)
    linked_data = _upload_root(
        hardlink_registry, hardlink_id, "data.bin") / "data.part"
    os.link(linked_data, tmp_path / "second-link")
    with pytest.raises(DraftCorruptError):
        hardlink_registry.append_chunk(
            hardlink_id, "data.bin", 0, bytes=b"x",
            chunk_sha256=_digest(b"x"))

    unknown_registry = QuestDraftRegistry(tmp_path / "unknown-registry")
    unknown_id = _draft(unknown_registry, key="c" * 32)["draft_id"]
    (_draft_root(unknown_registry, unknown_id) / "unknown").write_text(
        "not allowed", encoding="utf-8")
    with pytest.raises(DraftCorruptError):
        unknown_registry.get(unknown_id)


def test_get_and_list_do_not_rehash_completed_files(tmp_path, monkeypatch):
    registry = QuestDraftRegistry(tmp_path / "registry")
    draft_id = _draft(registry)["draft_id"]
    payload = b"completed content"
    registry.begin_file(draft_id, "complete.bin", len(payload))
    registry.append_chunk(
        draft_id, "complete.bin", 0, bytes=payload,
        chunk_sha256=_digest(payload))
    registry.finalize_file(draft_id, "complete.bin", _digest(payload))

    original = quest_drafts_module._hash_fd
    calls = []

    def spy(fd, *, expected_size, label):
        calls.append(label)
        return original(fd, expected_size=expected_size, label=label)

    monkeypatch.setattr(quest_drafts_module, "_hash_fd", spy)
    assert registry.get(draft_id)["file_count"] == 1
    assert registry.list()[0]["file_count"] == 1
    assert registry.files_root(draft_id) == (
        _draft_root(registry, draft_id) / "files")
    assert calls == []
    assert registry.files_manifest(draft_id)[0]["status"] == "complete"
    assert calls == ["files/complete.bin"]


def test_files_root_validates_tree_metadata_without_publishing(tmp_path):
    registry = QuestDraftRegistry(tmp_path / "registry")
    draft_id = _draft(registry)["draft_id"]
    files = _draft_root(registry, draft_id) / "files"
    (files / "unknown.bin").write_bytes(b"not declared")

    with pytest.raises(DraftCorruptError):
        registry.files_root(draft_id)
    # Validation never adopts or moves the unknown file.
    assert (files / "unknown.bin").read_bytes() == b"not declared"
