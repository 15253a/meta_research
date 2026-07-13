"""CP11.4c.3b.2b.3 · registered checkpoint/log hydration and path lineage."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from orchestrator import database as db
import orchestrator.storage_assets as storage_assets_module
from orchestrator.instance_lease import (
    RESTORE_IN_PROGRESS_NAME,
    InstanceLease,
    restore_parent_claim_name,
)
from orchestrator.storage_assets import RegisteredAssetArchive, StorageAssetError
from orchestrator.storage_governance import CycleSnapshotPublisher
from orchestrator.storage_imports import (
    ImportMaterializationArchive,
    StorageImportError,
)
from orchestrator.storage_ops import SnapshotArchive, StorageOperationError, main
from orchestrator.storage_paths import registered_path_roots, resolve_registered_path
from orchestrator.storage_restore_contract import (
    IMPORT_RESTORE_MARKER,
    REGISTERED_COMPLETION_RELATIVE,
    REGISTERED_RESTORE_MARKER,
    canonical,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _new_registered_chain(
        work: Path, *, duplicate_checkpoint: bool = False,
        checkpoint_relative: str = (
            "questions/q1/cycles/c1/staging/run1/model.bin"),
        log_relative: str = "registered-logs/train.log"):
    work.mkdir(mode=0o700)
    checkpoint = work / checkpoint_relative
    checkpoint.parent.mkdir(parents=True, mode=0o700)
    checkpoint_raw = b"checkpoint\x00payload\xff"
    checkpoint.write_bytes(checkpoint_raw)
    checkpoint.chmod(0o600)
    log = work / log_relative
    log.parent.mkdir(parents=True, mode=0o700)
    log_raw = b"train\x00log\n"
    log.write_bytes(log_raw)
    log.chmod(0o600)

    path = work / "research.sqlite"
    writer = db.connect(path)
    writer.executescript("""
    INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}');
    INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version)
      VALUES (1,1,1,'bundle','v0');
    INSERT INTO baseline(id,slug,canonical_key,status)
      VALUES (1,'b','bk1','planned');
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status)
      VALUES (1,1,'v1','{}','planned');
    INSERT INTO build_target(id,cycle_id,target_kind,seq,status,variant_id)
      VALUES (1,1,'build',1,'complete',1);
    INSERT INTO run(id,cycle_id,variant_id,build_target_id,kind,status)
      VALUES (1,1,1,1,'build','success');
    """)
    writer.execute(
        "INSERT INTO checkpoint(id,variant_id,ckpt_key,path,content_hash,hash_alg,"
        "artifact_type,origin,produced_by_run) "
        "VALUES (1,1,'final-r1',?,?,'sha256','checkpoint','run_produced',1)",
        (str(checkpoint), _sha(checkpoint_raw)))
    if duplicate_checkpoint:
        writer.execute(
            "INSERT INTO checkpoint(id,variant_id,ckpt_key,path,content_hash,hash_alg,"
            "artifact_type,origin,produced_by_run) "
            "VALUES (2,1,'fold-r1',?,?,'sha256','checkpoint','run_produced',1)",
            (checkpoint.relative_to(work).as_posix(), _sha(checkpoint_raw)))
    writer.execute(
        "INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash,bytes) "
        "VALUES (1,1,1,'train',?,?,?)",
        (str(log), _sha(log_raw), len(log_raw)))
    writer.execute(
        "UPDATE cycle SET status='done',finished_at='2026-07-13T00:00:00Z' WHERE id=1")
    writer.commit()
    publisher = CycleSnapshotPublisher(db_path=path, work_root=work)
    assert publisher.reconcile(startup=True) == ["c1"]
    return writer, publisher, checkpoint, checkpoint_raw, log, log_raw


@contextmanager
def _archive(work: Path):
    lease = InstanceLease.acquire(work)
    try:
        yield SnapshotArchive(work_root=work, lease=lease)
    finally:
        assert lease.close() is None


def test_checkpoint_mirror_deduplicates_indexes_and_keeps_original(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, raw, _log, _log_raw = _new_registered_chain(
        work, duplicate_checkpoint=True)
    writer.close()
    before = (checkpoint.lstat().st_mode, checkpoint.lstat().st_mtime_ns,
              checkpoint.lstat().st_ctime_ns)

    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        report = assets.mirror_checkpoints()
        assert report["scope"] == "db_registered_checkpoints_only"
        assert report["published"] == [1, 2]
        assert len(list(assets.checkpoint_objects.iterdir())) == 1
        assert len(list(assets.checkpoint_indexes.iterdir())) == 2
        obj = next(assets.checkpoint_objects.iterdir())
        assert obj.name == _sha(raw)
        assert obj.read_bytes() == raw
        assert assets.verify_checkpoint_mirrors()["mirrors_verified"] == 2
        assert assets.mirror_checkpoints()["reused"] == [1, 2]

    assert (checkpoint.lstat().st_mode, checkpoint.lstat().st_mtime_ns,
            checkpoint.lstat().st_ctime_ns) == before


@pytest.mark.parametrize("drift", ["content", "missing", "symlink", "hardlink"])
def test_checkpoint_mirror_rejects_original_identity_drift(tmp_path, drift):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, raw, _log, _log_raw = _new_registered_chain(work)
    writer.close()
    if drift == "content":
        checkpoint.write_bytes(b"X" + raw[1:])
    elif drift == "missing":
        checkpoint.unlink()
    elif drift == "symlink":
        other = checkpoint.with_name("other.bin")
        other.write_bytes(raw)
        checkpoint.unlink()
        checkpoint.symlink_to(other.name)
    else:
        other = checkpoint.with_name("other.bin")
        checkpoint.rename(other)
        os.link(other, checkpoint)

    with _archive(work) as archive:
        with pytest.raises(StorageAssetError, match="checkpoint.*(original|原件|路径|hash)"):
            RegisteredAssetArchive(archive).mirror_checkpoints()


@pytest.mark.parametrize("kind", ["checkpoint", "execution_log"])
def test_registered_mirror_rejects_state_control_plane_paths(tmp_path, kind):
    work = tmp_path / "work"
    kwargs = (
        {"checkpoint_relative": "state/status_card.json"}
        if kind == "checkpoint"
        else {"log_relative": "state/console_inbox.jsonl"})
    writer, _publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(work, **kwargs))
    writer.close()
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        operation = (
            assets.mirror_checkpoints if kind == "checkpoint"
            else assets.mirror_logs)
        with pytest.raises(StorageAssetError, match="state.*(控制面|保留)"):
            operation()


def test_combined_registered_restore_hydrates_and_relocates(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, checkpoint_raw, log, log_raw = _new_registered_chain(work)
    writer.close()
    target = tmp_path / "restored"

    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        mirrored = assets.mirror_registered_assets()
        assert mirrored["checkpoints"]["published"] == [1]
        assert mirrored["execution_logs"]["published"] == [1]
        archive.restore(
            target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        restored = assets.restore_registered_assets(target=target)
        assert restored["hydrated_checkpoints"] == 1
        assert restored["hydrated_execution_logs"] == 1
        assert (target / RESTORE_IN_PROGRESS_NAME).read_bytes() == REGISTERED_RESTORE_MARKER
        ImportMaterializationArchive(archive).restore(target=target)

    assert not (target / RESTORE_IN_PROGRESS_NAME).exists()
    checkpoint_target = target / checkpoint.relative_to(work)
    log_target = target / log.relative_to(work)
    assert checkpoint_target.read_bytes() == checkpoint_raw
    assert log_target.read_bytes() == log_raw
    assert resolve_registered_path(target, str(checkpoint)) == checkpoint_target
    assert resolve_registered_path(target, str(log)) == log_target
    assert registered_path_roots(target) == (target.resolve(), work.resolve())
    lease = InstanceLease.acquire(target)
    assert lease.close() is None


def test_registered_restore_replays_after_files_before_receipt(tmp_path, monkeypatch):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, checkpoint_raw, log, log_raw = _new_registered_chain(work)
    writer.close()
    target = tmp_path / "restored"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        archive.restore(target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        publish_once = assets._publish_restore_receipt

        def fail_receipt(*_args, **_kwargs):
            raise RuntimeError("kill before registered receipt")

        monkeypatch.setattr(assets, "_publish_restore_receipt", fail_receipt)
        with pytest.raises(RuntimeError, match="before registered receipt"):
            assets.restore_registered_assets(target=target)
        assert (target / checkpoint.relative_to(work)).read_bytes() == checkpoint_raw
        assert (target / log.relative_to(work)).read_bytes() == log_raw
        assert (target / RESTORE_IN_PROGRESS_NAME).exists()

        monkeypatch.setattr(assets, "_publish_restore_receipt", publish_once)
        report = assets.restore_registered_assets(target=target)
        assert report["reused"] == 2
        ImportMaterializationArchive(archive).restore(target=target)


def test_import_restore_cannot_clear_registered_marker_before_hydration(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(work))
    writer.close()
    target = tmp_path / "restored"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        archive.restore(
            target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        with pytest.raises(StorageImportError, match="registered asset completion"):
            ImportMaterializationArchive(archive).restore(target=target)
        assert (target / RESTORE_IN_PROGRESS_NAME).read_bytes() == (
            REGISTERED_RESTORE_MARKER)
        expected = assets.registered_restore_authority()
        forged = dict(expected)
        forged["files"] = []
        completion = target / REGISTERED_COMPLETION_RELATIVE
        completion.parent.mkdir(parents=True, exist_ok=True)
        completion.write_bytes(canonical(forged))
        completion.chmod(0o400)
        with pytest.raises(StorageImportError, match="registered asset completion"):
            ImportMaterializationArchive(archive).restore(target=target)
        assert (target / RESTORE_IN_PROGRESS_NAME).read_bytes() == (
            REGISTERED_RESTORE_MARKER)
        completion.unlink()
        assets.restore_registered_assets(target=target)
        ImportMaterializationArchive(archive).restore(target=target)
    assert not (target / RESTORE_IN_PROGRESS_NAME).exists()


def test_registered_restore_refuses_import_only_continuation(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(work))
    writer.close()
    target = tmp_path / "import-only"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        archive.restore(target=target, continuation_marker=IMPORT_RESTORE_MARKER)
        with pytest.raises(StorageAssetError, match="registered.*continuation"):
            assets.restore_registered_assets(target=target)
    assert (target / RESTORE_IN_PROGRESS_NAME).read_bytes() == IMPORT_RESTORE_MARKER
    assert not (target / REGISTERED_COMPLETION_RELATIVE).exists()


def test_registered_restore_refuses_conflicting_preseeded_destination(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, _checkpoint_raw, _log, _log_raw = _new_registered_chain(work)
    writer.close()
    target = tmp_path / "restored"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        archive.restore(target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        destination = target / checkpoint.relative_to(work)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"attacker-preseed")
        with pytest.raises(StorageAssetError, match="target.*漂移|恢复目标.*冲突"):
            assets.restore_registered_assets(target=target)
        assert destination.read_bytes() == b"attacker-preseed"
        assert (target / RESTORE_IN_PROGRESS_NAME).exists()


def test_registered_restore_uses_mirrors_after_originals_are_lost(tmp_path):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, checkpoint_raw, log, log_raw = _new_registered_chain(work)
    writer.close()
    target = tmp_path / "restored"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        checkpoint.unlink()
        log.unlink()
        archive.restore(target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        report = assets.restore_registered_assets(target=target)
        assert report["published"] == 2
        ImportMaterializationArchive(archive).restore(target=target)
    assert (target / checkpoint.relative_to(work)).read_bytes() == checkpoint_raw
    assert (target / log.relative_to(work)).read_bytes() == log_raw


def test_complete_restore_rejects_bad_mirror_and_old_cycle_before_target(tmp_path):
    work = tmp_path / "work"
    writer, publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(work))
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'done','v0','2026-07-13T00:01:00Z')")
    writer.commit()
    assert publisher.reconcile() == ["c2"]
    writer.close()
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()

    historical_target = tmp_path / "historical"
    with pytest.raises(StorageAssetError, match="latest mirrored high-water"):
        main([
            "--work-root", str(work), "restore-with-registered-assets",
            "--target", str(historical_target), "--cycle", "c1",
        ])
    assert not historical_target.exists()

    mirror = next(
        (work / "state/storage/checkpoint-mirrors/objects/sha256").iterdir())
    mirror.chmod(0o600)
    mirror.write_bytes(mirror.read_bytes() + b"tamper")
    mirror.chmod(0o400)
    corrupt_target = tmp_path / "corrupt"
    with pytest.raises(StorageAssetError, match="mirror.*漂移"):
        main([
            "--work-root", str(work), "restore-with-registered-assets",
            "--target", str(corrupt_target),
        ])
    assert not corrupt_target.exists()


def test_registered_path_lineage_survives_second_restore(tmp_path):
    source = tmp_path / "source"
    writer, _publisher, checkpoint, checkpoint_raw, log, log_raw = (
        _new_registered_chain(source))
    writer.close()
    first = tmp_path / "first"
    assert main(["--work-root", str(source), "mirror-registered-assets"]) == 0
    assert main([
        "--work-root", str(source), "restore-with-registered-assets",
        "--target", str(first),
    ]) == 0
    first_publisher = CycleSnapshotPublisher(
        db_path=first / "research.sqlite", work_root=first)
    assert first_publisher.reconcile(startup=True) == ["c1"]
    second = tmp_path / "second"
    assert main(["--work-root", str(first), "mirror-registered-assets"]) == 0
    assert main([
        "--work-root", str(first), "restore-with-registered-assets",
        "--target", str(second),
    ]) == 0

    assert registered_path_roots(second) == (
        second.resolve(), first.resolve(), source.resolve())
    assert resolve_registered_path(second, str(checkpoint)).read_bytes() == checkpoint_raw
    assert resolve_registered_path(second, str(log)).read_bytes() == log_raw


def test_second_restore_rejects_target_nested_in_historical_lineage(tmp_path):
    source = tmp_path / "source"
    writer, _publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(source))
    writer.close()
    first = tmp_path / "first"
    assert main(["--work-root", str(source), "mirror-registered-assets"]) == 0
    assert main([
        "--work-root", str(source), "restore-with-registered-assets",
        "--target", str(first),
    ]) == 0
    first_publisher = CycleSnapshotPublisher(
        db_path=first / "research.sqlite", work_root=first)
    assert first_publisher.reconcile(startup=True) == ["c1"]
    assert main(["--work-root", str(first), "mirror-registered-assets"]) == 0

    nested = source / "nested-restore"
    claim = nested.parent / restore_parent_claim_name(nested)
    with pytest.raises(StorageOperationError, match="lineage|嵌套"):
        main([
            "--work-root", str(first), "restore-with-registered-assets",
            "--target", str(nested),
        ])
    assert not nested.exists()
    assert not claim.exists()


@pytest.mark.parametrize("kind", ["checkpoint", "execution_log"])
def test_registered_restore_wraps_mirror_open_failure(tmp_path, monkeypatch, kind):
    work = tmp_path / "work"
    writer, _publisher, _checkpoint, _checkpoint_raw, _log, _log_raw = (
        _new_registered_chain(work))
    writer.close()
    target = tmp_path / "restored"
    with _archive(work) as archive:
        assets = RegisteredAssetArchive(archive)
        assets.mirror_registered_assets()
        archive.restore(target=target, continuation_marker=REGISTERED_RESTORE_MARKER)
        authority = assets.registered_restore_authority()
        selected = next(item for item in authority["files"] if item["kind"] == kind)
        mirror = work / selected["mirror_path"]
        real_open = os.open
        copying = False
        method_name = (
            "_copy_checkpoint_restore" if kind == "checkpoint"
            else "_copy_log_restore")
        real_copy = getattr(assets, method_name)

        def fail_copy_open(path, flags, *args, **kwargs):  # noqa: ANN001
            if copying and Path(path) == mirror:
                raise FileNotFoundError("injected mirror disappearance")
            return real_open(path, flags, *args, **kwargs)

        def copy_with_disappearance(*args, **kwargs):  # noqa: ANN002,ANN003
            nonlocal copying
            copying = True
            try:
                return real_copy(*args, **kwargs)
            finally:
                copying = False

        monkeypatch.setattr(storage_assets_module.os, "open", fail_copy_open)
        monkeypatch.setattr(assets, method_name, copy_with_disappearance)
        with pytest.raises(StorageAssetError, match="mirror.*(不可打开|缺失)"):
            assets.restore_registered_assets(target=target)
    assert (target / RESTORE_IN_PROGRESS_NAME).exists()
    assert not [
        path for path in target.rglob(".*")
        if ".registered-restore-" in path.name and path.name.endswith(".tmp")]


def test_registered_asset_cli_mirror_verify_and_complete_restore(tmp_path, capsys):
    work = tmp_path / "work"
    writer, _publisher, checkpoint, checkpoint_raw, log, log_raw = _new_registered_chain(work)
    writer.close()
    target = tmp_path / "restored"

    assert main(["--work-root", str(work), "mirror-registered-assets"]) == 0
    mirrored = json.loads(capsys.readouterr().out)
    assert mirrored["checkpoints"]["published"] == [1]
    assert main(["--work-root", str(work), "verify-registered-assets"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["checkpoints"]["mirrors_verified"] == 1
    assert main([
        "--work-root", str(work), "restore-with-registered-assets",
        "--target", str(target),
    ]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["schema"] == "meta-research-complete-registered-restore/v1"
    assert restored["registered_assets"]["hydrated_checkpoints"] == 1
    assert (target / checkpoint.relative_to(work)).read_bytes() == checkpoint_raw
    assert (target / log.relative_to(work)).read_bytes() == log_raw
    assert not (target / RESTORE_IN_PROGRESS_NAME).exists()
    with sqlite3.connect(f"file:{target / 'research.sqlite'}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
