"""CP11.4c.3b.2b.2 · SQLite-rooted repository/dependency CAS closure and restore."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
from orchestrator import database as db
from orchestrator.import_materialization_contract import spec_ref
from orchestrator.instance_lease import (
    RESTORE_IN_PROGRESS_NAME,
    InstanceBusyError,
    InstanceLease,
    InstanceLeaseError,
)
from orchestrator.repository_materialization_common import _canonical, _value_hash
from orchestrator.storage_governance import CycleSnapshotPublisher
import orchestrator.storage_imports as storage_imports_module
import orchestrator.storage_ops as storage_ops_module
from orchestrator.storage_imports import (
    ImportMaterializationArchive,
    StorageImportError,
)
from orchestrator.storage_ops import SnapshotArchive, main


@contextmanager
def _archive(work: Path):
    lease = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    try:
        yield SnapshotArchive(work_root=work, lease=lease)
    finally:
        assert lease.close() is None


def _source(
        tmp_path, *, targets=1, dependency=False, drift_plan=False,
        drop_root_hash=False, drift_dependency_base=False):
    work = tmp_path / "work"
    work.mkdir(mode=0o700, parents=True)
    repository_digest = "a" * 64
    dependency_digest = "b" * 64
    repository_objects = work / "state" / "import-materializations" / "objects"
    repository_indexes = work / "state" / "import-materializations" / "indexes"
    repository_object = repository_objects / repository_digest
    repository_object.mkdir(parents=True)
    repository_indexes.mkdir()
    (repository_object / "frozen.bin").write_bytes(b"repository object")
    capability = None
    if dependency:
        capability = {
            "version": 1, "provider": "python-wheel-image-v1",
            "closure_hash": "sha256:" + dependency_digest,
            "receipt_hash": "sha256:" + "c" * 64,
            "environment_hash": "sha256:" + "d" * 64,
            "image": "sha256:" + "e" * 64,
            "image_id": "sha256:" + "e" * 64,
        }
        dependency_object = (
            work / "state" / "dependency-images" / "objects"
            / dependency_digest)
        dependency_object.mkdir(parents=True)
        (dependency_object / "image.tar").write_bytes(b"dependency object")
    ledger = [{
        "path": "artifact.bin", "sha256": "sha256:" + "1" * 64,
        "bytes": 1, "git_mode": "100644",
    }]
    spec = {
        "smoke_cmd": ["python", "smoke.py"],
        "eval_cmd": ["python", "eval.py"],
        "protocol_id": 1, "protocol_ver": 1,
        "eval_key": "factory", "target_set_hash": "targets",
        "required": [[1, 1]], "artifact_relpath": "artifact.bin",
        "artifact_type": "external_model",
        "env_hash": capability["environment_hash"] if capability else "env",
        "supply_chain": {},
    }
    if capability is not None:
        spec["execution_image"] = capability
    result = {
        **spec,
        "source_tree": str(repository_object / "tree"),
        "file_ledger": ledger,
        "snapshot_receipt": str(repository_object / "receipt.json"),
        "repository_snapshot_hash": "sha256:" + repository_digest,
    }
    receipt = {
        "repository": "acme/model", "revision": "2" * 40,
        "config_hash": "sha256:" + "3" * 64,
        "environment_hash": "sha256:" + "4" * 64,
        "object_hash": "sha256:" + repository_digest,
    }
    inspection = {
        "receipt": receipt, "ledger": ledger, "spec": spec,
        "transport": [], "result": result,
    }
    identity = {
        "candidate_id": 7,
        "canonical_uri": "https://github.com/acme/model",
        "revision": receipt["revision"],
        "search_snapshot_hash": "sha256:" + "5" * 64,
        "config_hash": receipt["config_hash"],
        "environment_hash": receipt["environment_hash"],
    }
    index = {"version": 1, **identity, "object_hash": receipt["object_hash"]}
    index_name = _value_hash(identity).removeprefix("sha256:") + ".json"
    (repository_indexes / index_name).write_bytes(_canonical(index))

    database_path = work / "research.sqlite"
    writer = db.connect(database_path)
    conftest.seed_minimal(writer)
    writer.execute(
        "INSERT INTO external_candidate("
        "id,question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
        "need_summary,source_kind,canonical_uri,revision,search_snapshot_json,"
        "search_snapshot_hash,rank,retrieved_at) VALUES "
        "(7,1,1,'human_named',?,'test import','repo',?,?,?, ?,0,?)",
        ("sha256:" + "6" * 64, identity["canonical_uri"],
         identity["revision"], "{}", identity["search_snapshot_hash"],
         "2026-07-12T00:00:00Z"))
    writer.execute(
        "INSERT INTO external_import("
        "id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash,license_decision_snapshot_hash,baseline_id) "
        "VALUES (9,1,7,'selected_for_materialization',1,?,?,?,?,1)",
        ("sha256:" + "7" * 64, "candidate-7",
         "sha256:" + "8" * 64, "sha256:" + "9" * 64))
    writer.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (1,'orchestrator','import_worker_cycle',?)",
        (json.dumps({"external_import_id": 9, "question_id": 1},
                    sort_keys=True),))
    plan = spec_ref(result)
    if drift_plan:
        plan = {**plan, "total_bytes": plan["total_bytes"] + 1}
    if drop_root_hash:
        plan = dict(plan)
        del plan["repository_snapshot_hash"]
    for offset in range(targets):
        writer.execute(
            "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,"
            "baseline_id,variant_id,plan_ref) VALUES (?,?,1,'import',?,'complete',1,1,?)",
            (3 + offset, 1, 3 + offset,
             json.dumps(plan, ensure_ascii=False, sort_keys=True)))
    writer.execute(
        "UPDATE cycle SET status='done',finished_at='2026-07-12T00:00:00Z' WHERE id=1")
    writer.commit()
    publisher = CycleSnapshotPublisher(db_path=database_path, work_root=work)
    assert publisher.reconcile(startup=True) == ["c1"]
    writer.close()
    return {
        "work": work, "inspection": inspection, "capability": capability,
        "repository_digest": repository_digest,
        "dependency_digest": dependency_digest,
        "index_name": index_name,
        "drift_dependency_base": drift_dependency_base,
    }


def _fake_inspectors(monkeypatch, source):
    calls = {"repository": 0, "dependency": 0}

    def inspect_repository(path, *, owner_guard=None):
        if owner_guard is not None:
            owner_guard()
        assert Path(path).name == source["repository_digest"]
        calls["repository"] += 1
        return source["inspection"]

    def inspect_dependency(
            path, *, expected_capability=None, owner_guard=None):
        if owner_guard is not None:
            owner_guard()
        assert Path(path).name == source["dependency_digest"]
        assert expected_capability == source["capability"]
        calls["dependency"] += 1
        return {
            "closure_hash": source["capability"]["closure_hash"],
            "base_environment_hash": (
                "sha256:" + "f" * 64 if source["drift_dependency_base"]
                else source["inspection"]["receipt"]["environment_hash"]),
        }, dict(source["capability"])

    monkeypatch.setattr(
        storage_imports_module, "inspect_repository_snapshot_object",
        inspect_repository)
    monkeypatch.setattr(
        storage_imports_module, "inspect_dependency_image_object",
        inspect_dependency)
    return calls


def test_verify_deduplicates_many_db_roots_and_closes_dependency(
        tmp_path, monkeypatch):
    source = _source(tmp_path, targets=100, dependency=True)
    calls = _fake_inspectors(monkeypatch, source)
    with _archive(source["work"]) as archive:
        report = ImportMaterializationArchive(archive).verify()
    assert report["scope"] == "sqlite_registered_repository_and_dependency_cas"
    assert report["import_targets"] == 100
    assert len(report["repository_objects"]) == 1
    assert report["repository_objects"][0]["target_ids"] == list(range(3, 103))
    assert report["dependency_objects"] == [
        "sha256:" + source["dependency_digest"]]
    assert calls == {"repository": 1, "dependency": 1}


def test_verify_rejects_db_plan_drift_and_missing_index(tmp_path, monkeypatch):
    drift = _source(tmp_path / "drift", drift_plan=True)
    _fake_inspectors(monkeypatch, drift)
    with _archive(drift["work"]) as archive:
        with pytest.raises(StorageImportError, match="plan_ref/object"):
            ImportMaterializationArchive(archive).verify()

    missing = _source(tmp_path / "missing")
    _fake_inspectors(monkeypatch, missing)
    (missing["work"] / "state" / "import-materializations" / "indexes"
     / missing["index_name"]).unlink()
    alias_identity = {
        "candidate_id": 999,
        "canonical_uri": "https://github.com/acme/model",
        "revision": "2" * 40,
        "search_snapshot_hash": "sha256:" + "0" * 64,
        "config_hash": "sha256:" + "3" * 64,
        "environment_hash": "sha256:" + "4" * 64,
    }
    alias = {
        "version": 1, **alias_identity,
        "object_hash": "sha256:" + missing["repository_digest"],
    }
    alias_name = _value_hash(alias_identity).removeprefix("sha256:") + ".json"
    (missing["work"] / "state" / "import-materializations" / "indexes"
     / alias_name).write_bytes(_canonical(alias))
    with _archive(missing["work"]) as archive:
        with pytest.raises(StorageImportError, match="exact index"):
            ImportMaterializationArchive(archive).verify()


def test_verify_rejects_repository_plan_downgrade_and_base_environment_drift(
        tmp_path, monkeypatch):
    downgraded = _source(tmp_path / "downgraded", drop_root_hash=True)
    _fake_inspectors(monkeypatch, downgraded)
    with _archive(downgraded["work"]) as archive:
        with pytest.raises(StorageImportError, match="plan_ref shape"):
            ImportMaterializationArchive(archive).verify()

    drift = _source(
        tmp_path / "base-drift", dependency=True,
        drift_dependency_base=True)
    _fake_inspectors(monkeypatch, drift)
    with _archive(drift["work"]) as archive:
        with pytest.raises(StorageImportError, match="base environment"):
            ImportMaterializationArchive(archive).verify()


def test_verify_reports_unregistered_orphans_without_deleting(tmp_path, monkeypatch):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    orphan = (source["work"] / "state" / "import-materializations"
              / "objects" / ("f" * 64))
    orphan.mkdir()
    with _archive(source["work"]) as archive:
        report = ImportMaterializationArchive(archive).verify()
    assert report["orphan_repository_objects"] == ["f" * 64]
    assert orphan.is_dir()


def test_restore_publishes_dependency_then_repository_then_indexes_and_replays(
        tmp_path, monkeypatch):
    source = _source(tmp_path, dependency=True)
    _fake_inspectors(monkeypatch, source)
    target = tmp_path / "restored"
    with _archive(source["work"]) as archive:
        archive.restore(target=target)
        assets = ImportMaterializationArchive(archive)
        first = assets.restore(target=target)
        assert first["published_dependency_objects"] == [
            "sha256:" + source["dependency_digest"]]
        assert first["published_repository_objects"] == [
            "sha256:" + source["repository_digest"]]
        assert first["published_indexes"] == [source["index_name"]]
        second = assets.restore(target=target)
        assert second["published_dependency_objects"] == []
        assert second["published_repository_objects"] == []
        assert second["published_indexes"] == []
        assert second["reused_indexes"] == [source["index_name"]]
    assert (target / "state" / "dependency-images" / "objects"
            / source["dependency_digest"] / "image.tar").is_file()
    assert (target / "state" / "import-materializations" / "objects"
            / source["repository_digest"] / "frozen.bin").is_file()
    assert (target / "state" / "import-materializations" / "indexes"
            / source["index_name"]).is_file()
    assert (target / "state" / "import-materializations"
            / "storage-restore.json").is_file()


def test_restore_capacity_fails_before_objects_and_active_target_is_refused(
        tmp_path, monkeypatch):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    target = tmp_path / "restored"
    with _archive(source["work"]) as archive:
        archive.restore(target=target)
        monkeypatch.setattr(
            storage_imports_module.os, "statvfs",
            lambda _path: SimpleNamespace(
                f_bavail=0, f_frsize=4096, f_bsize=4096,
                f_favail=1_000_000))
        with pytest.raises(StorageImportError, match="容量门拒绝"):
            ImportMaterializationArchive(archive).restore(target=target)
        assert not list((target / "state" / "import-materializations"
                         / "objects").glob("[0-9a-f]" * 64))

    monkeypatch.undo()
    _fake_inspectors(monkeypatch, source)
    with _archive(source["work"]) as archive:
        ImportMaterializationArchive(archive).restore(target=target)
    target_lease = InstanceLease.acquire(target, heartbeat_interval_s=0.02)
    try:
        with _archive(source["work"]) as archive:
            with pytest.raises(InstanceBusyError):
                ImportMaterializationArchive(archive).restore(target=target)
    finally:
        assert target_lease.close() is None


@pytest.mark.parametrize("failure_point", ["object", "index", "receipt"])
def test_restore_crash_windows_remain_startup_fenced_and_replay(
        tmp_path, monkeypatch, failure_point):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    target = tmp_path / "restored"
    with _archive(source["work"]) as archive:
        archive.restore(target=target)
        assets = ImportMaterializationArchive(archive)
        with monkeypatch.context() as crash:
            if failure_point == "object":
                original = ImportMaterializationArchive._copy_object

                def fail_after_object(self, **kwargs):
                    original(self, **kwargs)
                    raise RuntimeError("crash after object publication")

                crash.setattr(
                    ImportMaterializationArchive, "_copy_object",
                    fail_after_object)
            elif failure_point == "index":
                original = storage_imports_module.sg._publish_once

                def fail_after_index(path, raw):
                    original(path, raw)
                    if Path(path).parent.name == "indexes":
                        raise RuntimeError("crash after index publication")

                crash.setattr(
                    storage_imports_module.sg, "_publish_once",
                    fail_after_index)
            else:
                original = ImportMaterializationArchive._sync_file

                def fail_after_receipt(path, owner_guard):
                    if Path(path).name == "storage-restore.json":
                        raise RuntimeError("crash after receipt publication")
                    return original(path, owner_guard)

                crash.setattr(
                    ImportMaterializationArchive, "_sync_file",
                    staticmethod(fail_after_receipt))
            with pytest.raises(RuntimeError, match="crash after"):
                assets.restore(target=target)

        assert (target / RESTORE_IN_PROGRESS_NAME).is_file()
        with pytest.raises(InstanceLeaseError, match="restore"):
            InstanceLease.acquire(target)
        replay = assets.restore(target=target)
        assert replay["repository_objects"] == [
            "sha256:" + source["repository_digest"]]
    assert not (target / RESTORE_IN_PROGRESS_NAME).exists()


def test_restore_capacity_counts_target_blocks_for_small_files(
        tmp_path, monkeypatch):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    repository = (source["work"] / "state" / "import-materializations"
                  / "objects" / source["repository_digest"])
    for number in range(300):
        (repository / f"small-{number:03d}").write_bytes(b"x")
    target = tmp_path / "restored"
    with _archive(source["work"]) as archive:
        archive.restore(target=target)
        monkeypatch.setattr(
            storage_imports_module.os, "statvfs",
            lambda _path: SimpleNamespace(
                f_bavail=300, f_frsize=4096, f_bsize=4096,
                f_favail=1_000_000))
        with pytest.raises(StorageImportError, match="容量门拒绝"):
            ImportMaterializationArchive(archive).restore(target=target)


@pytest.mark.parametrize("failure_point", ["claim", "marker", "sqlite_receipt"])
def test_combined_restore_replays_vepfs_publication_windows(
        tmp_path, monkeypatch, capsys, failure_point):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    target = tmp_path / "restored"
    monkeypatch.setattr(
        storage_ops_module, "_try_rename_noreplace",
        lambda _source, _destination: False)

    with monkeypatch.context() as crash:
        if failure_point == "claim":
            original = storage_ops_module._publish_parent_claim

            def fail_after_claim(destination, *, token):
                original(destination, token=token)
                raise RuntimeError("crash after parent claim")

            crash.setattr(
                storage_ops_module, "_publish_parent_claim",
                fail_after_claim)
        elif failure_point == "marker":
            original = storage_ops_module.sg._publish_once

            def fail_before_marker(path, raw):
                if Path(path).name == RESTORE_IN_PROGRESS_NAME:
                    raise RuntimeError("crash before continuation marker")
                return original(path, raw)

            crash.setattr(
                storage_ops_module.sg, "_publish_once",
                fail_before_marker)
        else:
            original = storage_ops_module.os.rename

            def fail_before_sqlite_receipt(source_path, destination_path):
                if Path(source_path).name == "restore.json":
                    raise RuntimeError("crash before SQLite receipt")
                return original(source_path, destination_path)

            crash.setattr(
                storage_ops_module.os, "rename",
                fail_before_sqlite_receipt)
        with pytest.raises(RuntimeError, match="crash"):
            main([
                "--work-root", str(source["work"]),
                "restore-with-import-materializations",
                "--target", str(target)])

    with pytest.raises(InstanceLeaseError, match="restore|parent claim"):
        InstanceLease.acquire(target)
    assert main([
        "--work-root", str(source["work"]),
        "restore-with-import-materializations",
        "--target", str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["repository_objects"] == [
        "sha256:" + source["repository_digest"]]
    assert not (target / RESTORE_IN_PROGRESS_NAME).exists()
    lease = InstanceLease.acquire(target, heartbeat_interval_s=0.02)
    assert lease.close() is None


def test_import_materialization_cli_verify_and_restore(
        tmp_path, monkeypatch, capsys):
    source = _source(tmp_path)
    _fake_inspectors(monkeypatch, source)
    assert main([
        "--work-root", str(source["work"]),
        "verify-import-materializations"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["import_targets"] == 1
    assert report["source_cycle"] == "c1"

    target = tmp_path / "restored"
    original_restore = ImportMaterializationArchive.restore
    fenced = []

    def assert_sqlite_to_import_gap_is_fenced(self, **kwargs):
        assert (target / RESTORE_IN_PROGRESS_NAME).is_file()
        with pytest.raises(InstanceLeaseError, match="restore"):
            InstanceLease.acquire(target)
        fenced.append(True)
        return original_restore(self, **kwargs)

    monkeypatch.setattr(
        ImportMaterializationArchive, "restore",
        assert_sqlite_to_import_gap_is_fenced)
    assert main([
        "--work-root", str(source["work"]),
        "restore-with-import-materializations",
        "--target", str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert fenced == [True]
    assert restored["published_repository_objects"] == [
        "sha256:" + source["repository_digest"]]
