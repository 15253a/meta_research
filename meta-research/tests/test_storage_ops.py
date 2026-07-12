"""CP11.4c.3b.2a · offline snapshot verify/restore/retention/GC 与容量门。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import database as db
from orchestrator.instance_lease import InstanceBusyError, InstanceLease
import orchestrator.storage_governance as storage_module
import orchestrator.storage_ops as storage_ops_module
from orchestrator.storage_governance import (
    CycleSnapshotPublisher,
    StorageGovernanceError,
)
from orchestrator.storage_ops import (
    GenerationNotRetained,
    SnapshotArchive,
    StorageOperationError,
    main,
)


def _new_chain(work: Path, count: int = 5):
    work.mkdir(mode=0o700)
    path = work / "research.sqlite"
    writer = db.connect(path)
    writer.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    writer.commit()
    publisher = CycleSnapshotPublisher(db_path=path, work_root=work)
    assert publisher.reconcile(startup=True) == []
    for cycle_id in range(1, count + 1):
        writer.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
            "VALUES (?,1,1,'done','v0','2026-07-12T00:00:00Z')", (cycle_id,))
        writer.commit()
        assert publisher.reconcile() == [f"c{cycle_id}"]
    return path, writer, publisher


def _backup_files(publisher: CycleSnapshotPublisher):
    return sorted(path.name for path in publisher.backups.iterdir())


def _tree_metadata(root: Path):
    paths = [root, *sorted(root.rglob("*"))]
    return {
        path.relative_to(root).as_posix(): (
            path.lstat().st_mode, path.lstat().st_size,
            path.lstat().st_mtime_ns, path.lstat().st_ctime_ns)
        for path in paths
    }


@contextmanager
def _archive(work: Path):
    lease = InstanceLease.acquire(work)
    try:
        yield SnapshotArchive(work_root=work, lease=lease)
    finally:
        assert lease.close() is None


def test_verify_is_self_contained_and_deep_checks_three_generations(
        tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer, _publisher = _new_chain(work, 5)
    writer.close()
    db_path.unlink()                         # 活 DB 正是可能损坏/丢失的对象

    with _archive(work) as archive:
        deep_cycles = []
        verify_object = archive.publisher._verify_backup_object

        def track_deep_check(path, **kwargs):
            deep_cycles.append(kwargs["cycle_id"])
            return verify_object(path, **kwargs)

        monkeypatch.setattr(
            archive.publisher, "_verify_backup_object", track_deep_check)
        report = archive.verify()
    assert report["coverage_start_cycle"] == "c1"
    assert report["scope"] == "snapshot_chain_and_retained_sqlite"
    assert report["high_water_cycle"] == "c5"
    assert report["protected_cycles"] == ["c3", "c4", "c5"]
    assert report["available_cycles"] == ["c1", "c2", "c3", "c4", "c5"]
    assert report["expired_cycles"] == []
    assert report["deep_verified_cycles"] == ["c3", "c4", "c5"]
    assert deep_cycles == [3, 4, 5]


def test_verify_read_only_mode_does_not_recreate_missing_layout(tmp_path):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 1)
    writer.close()
    publisher.temporary.rmdir()

    with pytest.raises(StorageGovernanceError, match="布局缺失"):
        with _archive(work):
            pass
    assert not publisher.temporary.exists()


@pytest.mark.parametrize("limited", ["bytes", "inodes"])
def test_backup_capacity_gate_rejects_before_temp_pending_or_git(
        tmp_path, monkeypatch, limited):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    db_path = work / "research.sqlite"
    writer = db.connect(db_path)
    writer.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    writer.commit()
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile(startup=True) == []
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (1,1,1,'done','v0')")
    writer.commit()
    monkeypatch.setattr(
        storage_module.os, "statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=0 if limited == "bytes" else 10 ** 9,
            f_frsize=4096,
            f_favail=(10 ** 9 if limited == "bytes" else
                      publisher.capacity_reserve_inodes
                      + storage_module._BACKUP_OPERATION_INODES - 1)))

    with pytest.raises(StorageGovernanceError, match="容量门拒绝"):
        publisher.reconcile()
    assert list(publisher.backups.iterdir()) == []
    assert list(publisher.pending.iterdir()) == []
    assert list(publisher.temporary.iterdir()) == []
    assert not (publisher.views / ".git").exists()
    writer.close()


def test_restore_selected_generation_to_new_atomic_workroot(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _db_path, writer, _publisher = _new_chain(work, 5)
    writer.close()
    target = tmp_path / "restored"

    with _archive(work) as archive:
        receipt = archive.restore(target=target, cycle="c4")
        assert receipt["scope"] == "sqlite_truth_only"
        assert receipt["continuation_mode"] == "legacy_adoption_on_first_start"
        assert set(path.name for path in target.iterdir()) == {"research.sqlite", "restore.json"}
        restored = sqlite3.connect(f"file:{target / 'research.sqlite'}?mode=ro", uri=True)
        try:
            assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert restored.execute("SELECT MAX(id) FROM cycle").fetchone() == (4,)
            assert restored.execute("SELECT status FROM cycle WHERE id=4").fetchone() == ("done",)
        finally:
            restored.close()
        assert json.loads((target / "restore.json").read_text(encoding="utf-8")) == receipt
        with pytest.raises(StorageOperationError, match="必须不存在"):
            archive.restore(target=target)

        alias = tmp_path / "source-alias"
        alias.symlink_to(work, target_is_directory=True)
        with pytest.raises(StorageOperationError, match="解析后不得位于"):
            archive.restore(target=alias / "nested")
        assert not (work / "nested").exists()

        raced = tmp_path / "raced-target"
        rename_noreplace = storage_ops_module._rename_noreplace

        def create_target_then_publish(source, destination):
            destination.mkdir()
            return rename_noreplace(source, destination)

        monkeypatch.setattr(
            storage_ops_module, "_rename_noreplace", create_target_then_publish)
        with pytest.raises(StorageOperationError, match="并发创建"):
            archive.restore(target=raced, cycle="c5")
        assert raced.is_dir() and not any(raced.iterdir())


def test_gc_dry_run_writes_nothing_then_apply_keeps_last_three(tmp_path):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 5)
    writer.close()
    before = _backup_files(publisher)
    with _archive(work) as archive:
        storage_metadata = _tree_metadata(archive.publisher.storage_root)
        views_metadata = _tree_metadata(archive.publisher.views)
        wrapper = archive.plan_gc()
        assert _tree_metadata(archive.publisher.storage_root) == storage_metadata
        assert _tree_metadata(archive.publisher.views) == views_metadata
        plan = wrapper["plan"]
        assert sorted(item["cycle_ids"] for item in plan["victims"]) == [["c1"], ["c2"]]
        chain = archive._chain(retain=3)
        assert plan["protected"] == [
            {"cycle_id": "c3", "backup_sha256": chain["manifests"][2]["backup"]["sha256"]},
            {"cycle_id": "c4", "backup_sha256": chain["manifests"][3]["backup"]["sha256"]},
            {"cycle_id": "c5", "backup_sha256": chain["manifests"][4]["backup"]["sha256"]},
        ]
        assert _backup_files(publisher) == before
        assert not archive.gc_root.exists()       # plan 本身对 storage/views 零写

        result = archive.apply_gc(
            plan=plan, expected_sha256=wrapper["plan_sha256"])
        assert len(result["deleted"]) == 2
        assert len(_backup_files(publisher)) == 3
        verified = archive.verify()
        assert verified["protected_cycles"] == ["c3", "c4", "c5"]
        assert verified["expired_cycles"] == ["c1", "c2"]
        assert archive.apply_gc(
            plan=plan, expected_sha256=wrapper["plan_sha256"])["deleted"] == []

        with pytest.raises(GenerationNotRetained, match="generation_not_retained"):
            archive.restore(target=tmp_path / "expired", cycle="c1")
        assert archive.restore(target=tmp_path / "retained", cycle="c3")[
            "source_cycle"] == "c3"


def test_gc_stale_plan_and_victim_drift_delete_nothing(tmp_path):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 4)
    with _archive(work) as archive:
        wrapper = archive.plan_gc()
        before = _backup_files(publisher)
        writer.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
            "VALUES (5,1,1,'done','v0')")
        writer.commit()
        assert publisher.reconcile() == ["c5"]
        with pytest.raises(StorageOperationError, match="stale"):
            archive.apply_gc(plan=wrapper["plan"], expected_sha256=wrapper["plan_sha256"])
        assert set(before).issubset(_backup_files(publisher))

        fresh = archive.plan_gc()
        victim = work / fresh["plan"]["victims"][0]["path"]
        victim.chmod(0o600)
        victim.write_bytes(victim.read_bytes() + b"tamper")
        with pytest.raises(
                StorageGovernanceError,
                match="backup CAS|backup 内容漂移|backup 类型/bytes 漂移"):
            archive.apply_gc(plan=fresh["plan"], expected_sha256=fresh["plan_sha256"])
        assert len(_backup_files(publisher)) >= 5
    writer.close()


def test_missing_old_backup_needs_applied_plan_authority(tmp_path):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 4)
    writer.close()
    with _archive(work) as archive:
        chain = archive._chain(retain=3)
        oldest = work / chain["manifests"][0]["backup"]["path"]
        oldest.unlink()

        with pytest.raises(StorageOperationError, match="无 applied-plan authority"):
            archive.verify()
    assert len(_backup_files(publisher)) == 3


def test_gc_expected_hash_and_kill_after_authority_are_replay_safe(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 6)
    writer.close()
    with _archive(work) as archive:
        wrapper = archive.plan_gc()
        with pytest.raises(StorageOperationError, match="expected hash"):
            archive.apply_gc(plan=wrapper["plan"], expected_sha256="0" * 64)
        assert not archive.gc_root.exists()
        assert len(_backup_files(publisher)) == 6

        real_unlink = Path.unlink
        durable_confirm = archive._confirm_durable_authority
        confirmations = []

        def track_durable_confirm(path, **kwargs):
            result = durable_confirm(path, **kwargs)
            confirmations.append(path)
            return result

        monkeypatch.setattr(
            archive, "_confirm_durable_authority", track_durable_confirm)

        def fail_first_backup(path, *args, **kwargs):
            if path.parent == publisher.backups:
                assert len(confirmations) == 1
                raise RuntimeError("kill after applied authority")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_first_backup)
        with pytest.raises(RuntimeError, match="kill after"):
            archive.apply_gc(
                plan=wrapper["plan"], expected_sha256=wrapper["plan_sha256"])
        applied = archive.applied_plans / f"{wrapper['plan_sha256']}.json"
        assert applied.exists() and len(_backup_files(publisher)) == 6
        crash_report = archive.verify()
        assert crash_report["expired_cycles"] == ["c1", "c2", "c3"]
        assert crash_report["expired_but_present_cycles"] == ["c1", "c2", "c3"]
        with pytest.raises(GenerationNotRetained, match="generation_not_retained"):
            archive.restore(target=tmp_path / "retired", cycle="c1")

        confirmations.clear()

        def delete_only_after_reconfirm(path, *args, **kwargs):
            if path.parent == publisher.backups:
                assert len(confirmations) == 1
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", delete_only_after_reconfirm)
        resumed = archive.apply_gc(
            plan=wrapper["plan"], expected_sha256=wrapper["plan_sha256"])
        assert len(confirmations) == 1
        assert len(resumed["deleted"]) == 3
        assert len(_backup_files(publisher)) == 3
        report = archive.verify()
        assert report["expired_cycles"] == ["c1", "c2", "c3"]
        assert report["expired_but_present_cycles"] == []


def test_gc_unpublished_authority_temp_is_ignored_then_cleaned(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 4)
    writer.close()
    with _archive(work) as archive:
        wrapper = archive.plan_gc()
        publish_once = storage_ops_module.sg._publish_once

        def crash_before_authority(path, raw):
            temporary = path.parent / f".{path.name}.tmp-{'a' * 32}"
            temporary.write_bytes(raw)
            raise RuntimeError("kill before applied authority rename")

        monkeypatch.setattr(
            storage_ops_module.sg, "_publish_once", crash_before_authority)
        with pytest.raises(RuntimeError, match="before applied authority"):
            archive.apply_gc(
                plan=wrapper["plan"], expected_sha256=wrapper["plan_sha256"])
        temporary = archive.applied_plans / (
            f".{wrapper['plan_sha256']}.json.tmp-{'a' * 32}")
        assert temporary.exists() and len(_backup_files(publisher)) == 4
        report = archive.verify()
        assert report["expired_cycles"] == []
        assert report["available_cycles"] == ["c1", "c2", "c3", "c4"]

        monkeypatch.setattr(storage_ops_module.sg, "_publish_once", publish_once)
        applied = archive.apply_gc(
            plan=wrapper["plan"], expected_sha256=wrapper["plan_sha256"])
        assert not temporary.exists() and len(applied["deleted"]) == 1

        unknown = archive.applied_plans / "unknown"
        unknown.write_text("x", encoding="utf-8")
        with pytest.raises(StorageOperationError, match="非法条目"):
            archive.verify()
        unknown.unlink()
        unsafe_temp = archive.applied_plans / (
            f".{wrapper['plan_sha256']}.json.tmp-{'b' * 32}")
        unsafe_temp.symlink_to("/dev/null")
        with pytest.raises(StorageOperationError, match="非法条目"):
            archive.verify()


def test_cli_verify_plan_and_explicit_apply_are_offline_and_canonical(
        tmp_path, capsys):
    work = tmp_path / "work"
    _db_path, writer, publisher = _new_chain(work, 4)
    writer.close()

    assert main(["--work-root", str(work), "verify"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["high_water_cycle"] == "c4"
    assert main(["--work-root", str(work), "gc-plan"]) == 0
    plan_output = capsys.readouterr().out
    wrapper = json.loads(plan_output)
    assert [item["cycle_ids"] for item in wrapper["plan"]["victims"]] == [["c1"]]
    assert not (work / "state" / "storage" / "gc").exists()

    plan_file = tmp_path / "gc-plan.json"       # shell output lives outside source
    plan_file.write_text(plan_output, encoding="utf-8")
    assert main([
        "--work-root", str(work), "gc-apply",
        "--plan-file", str(plan_file),
        "--expect-sha256", wrapper["plan_sha256"],
    ]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert len(applied["deleted"]) == 1
    assert len(_backup_files(publisher)) == 3


def test_cli_refuses_missing_source_and_active_owner(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(StorageOperationError, match="source work_root.*缺失"):
        main(["--work-root", str(missing), "verify"])
    assert not missing.exists()

    work = tmp_path / "work"
    _db_path, writer, _publisher = _new_chain(work, 1)
    writer.close()
    other = tmp_path / "other"
    other.mkdir()
    other_lease = InstanceLease.acquire(other)
    try:
        with pytest.raises(StorageOperationError, match="exact work-root lease"):
            SnapshotArchive(work_root=work, lease=other_lease)
    finally:
        assert other_lease.close() is None

    lease = InstanceLease.acquire(work)
    try:
        with pytest.raises(InstanceBusyError):
            main(["--work-root", str(work), "verify"])
    finally:
        assert lease.close() is None
