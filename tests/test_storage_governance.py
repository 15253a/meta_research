"""CP11.4c.3b.1 · terminal cycle backup + views Git + immutable manifest。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import conftest
from orchestrator import database as db
import orchestrator.storage_governance as storage_module
from orchestrator.storage_governance import (
    CycleSnapshotPublisher,
    StorageGovernanceError,
)


def _seed_done(work: Path):
    work.mkdir(mode=0o700)
    path = work / "research.sqlite"
    conn = db.connect(path)
    conftest.seed_minimal(conn)
    conn.execute(
        "UPDATE cycle SET status='done', finished_at='2026-07-12T00:00:00Z' WHERE id=1")
    conn.commit()
    return path, conn


def _pointer(work: Path, cycle_id: int = 1):
    pointer = json.loads(
        (work / "state" / "storage" / "cycles" / f"c{cycle_id}.json").read_text(
            encoding="utf-8"))
    manifest_path = work / pointer["manifest_path"]
    raw = manifest_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == pointer["manifest_sha256"]
    return pointer, json.loads(raw)


def _git(work: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(work / "views"), *args], check=True,
        text=True, capture_output=True).stdout.strip()


def test_terminal_cycle_publishes_consistent_backup_views_and_manifest(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)

    assert publisher.reconcile() == ["c1"]
    pointer, manifest = _pointer(work)
    backup = work / manifest["backup"]["path"]
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == manifest["backup"]["sha256"]
    restored = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored.execute("SELECT status FROM cycle WHERE id=1").fetchone() == ("done",)
    finally:
        restored.close()

    assert {item.name for item in (work / "views").iterdir()} == {
        ".git", "goal.md", "tree.md", "pool.md", "digest.md"}
    assert "# 轮次 c1 存储投影" in (work / "views" / "goal.md").read_text(encoding="utf-8")
    assert "## 问题树" in (work / "views" / "tree.md").read_text(encoding="utf-8")
    pool_view = (work / "views" / "pool.md").read_text(encoding="utf-8")
    assert all(section in pool_view for section in (
        "正式代码路径", "## 协议", "产物哈希锚", "## 正式池发布", "## 资产卡片"))
    assert "## 模型调用" in (work / "views" / "digest.md").read_text(encoding="utf-8")
    assert _git(work, "rev-list", "--count", "HEAD") == "1"
    message = _git(work, "show", "-s", "--format=%B", "HEAD")
    assert "Cycle: c1" in message
    assert f"DB-Backup-SHA256: {manifest['backup']['sha256']}" in message
    assert f"Asset-Inventory-SHA256: {manifest['asset_inventory_sha256']}" in message
    assert manifest["views"]["commit"] == _git(work, "rev-parse", "HEAD")
    assert manifest["adoption_baseline"] is False
    assert manifest["bootstrap_before_cycle"] is None
    assert manifest["assets"][0] == {
        "artifact_type": "algorithm", "content_hash": "h", "hash_alg": "sha256",
        "manifest_hash": None, "origin": "none", "owner": "checkpoint",
        "owner_id": 1, "ref": "/x", "retention": "registered_forever"}
    assert pointer["cycle_id"] == manifest["cycle_id"] == "c1"

    # 重放不产生第二 backup / commit / manifest。
    assert publisher.reconcile() == []
    assert _git(work, "rev-list", "--count", "HEAD") == "1"
    assert len(list((work / "state" / "storage" / "backups" / "sha256").iterdir())) == 1
    writer.close()


def test_formal_pool_manifest_and_code_tree_are_in_snapshot_inventory_and_view(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    code_hash = "a" * 64
    manifest_hash = "b" * 64
    manifest_ref = f"pool/manifests/{manifest_hash}.json"
    writer.execute(
        "UPDATE baseline SET code_ref='baselines/b-deadbeef/src',commit_hash=? WHERE id=1",
        ("sha256-tree-v1:" + code_hash,))
    writer.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (1,'gate','pool_publication',?)",
        (json.dumps({
            "manifest_ref": manifest_ref, "manifest_hash": manifest_hash,
            "baseline_id": 1, "variant_id": 1, "evaluation_id": 1, "attempt_id": 1,
        }, sort_keys=True, separators=(",", ":")),))
    writer.commit()

    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile() == ["c1"]
    _pointer_doc, manifest = _pointer(work)
    assets = {(item["owner"], item["owner_id"]): item for item in manifest["assets"]}
    assert assets[("baseline_code", 1)]["content_hash"] == code_hash
    assert assets[("pool_publication", 2)]["ref"] == manifest_ref
    pool_view = (work / "views" / "pool.md").read_text(encoding="utf-8")
    assert manifest_ref in pool_view and manifest_hash in pool_view
    writer.close()


def test_native_genesis_survives_crash_before_first_backup(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    db_path = work / "research.sqlite"
    writer = db.connect(db_path)
    conftest.seed_minimal(writer)
    first = CycleSnapshotPublisher(db_path=db_path, work_root=work)

    assert first.reconcile(startup=True) == []
    genesis = json.loads(first.genesis_path.read_text(encoding="utf-8"))
    assert genesis == {
        "schema": storage_module.GENESIS_SCHEMA,
        "coverage_start_cycle": 1,
        "adoption_baseline": False,
        "bootstrap_before_cycle": None,
    }
    writer.execute(
        "UPDATE cycle SET status='done', finished_at='2026-07-12T00:00:00Z' WHERE id=1")
    writer.commit()

    # 模拟 terminal commit 后、backup/pending 前 kill：新实例仍按 genesis 做原生 c1。
    recovered = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert recovered.reconcile(startup=True) == ["c1"]
    _, manifest = _pointer(work)
    assert manifest["adoption_baseline"] is False
    assert manifest["bootstrap_before_cycle"] is None
    writer.close()


def test_crash_after_backup_reuses_pending_recovery_point(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)

    def fail_views(*_args, **_kwargs):
        raise StorageGovernanceError("injected after backup")

    monkeypatch.setattr(publisher, "_publish_views", fail_views)
    with pytest.raises(StorageGovernanceError, match="injected"):
        publisher.reconcile()
    pending = json.loads(
        (work / "state" / "storage" / "pending" / "c1.json").read_text(encoding="utf-8"))
    first_backup_hash = pending["backup"]["sha256"]

    # DB 在副作用失败后继续出现带外写；恢复必须沿用 pending 指向的原 recovery point。
    writer.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'later','{}')")
    writer.commit()
    recovered = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert recovered.reconcile() == ["c1"]
    _, manifest = _pointer(work)
    assert manifest["backup"]["sha256"] == first_backup_hash
    backup = sqlite3.connect(f"file:{work / manifest['backup']['path']}?mode=ro", uri=True)
    try:
        assert backup.execute("SELECT count(*) FROM goal WHERE id=2").fetchone() == (0,)
    finally:
        backup.close()
    assert not (work / "state" / "storage" / "pending" / "c1.json").exists()
    writer.close()


def test_pending_cannot_be_rebound_to_older_valid_backup(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile() == ["c1"]
    _, first_manifest = _pointer(work, 1)
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'failed','v0','2026-07-12T00:01:00Z')")
    writer.commit()

    monkeypatch.setattr(
        publisher, "_publish_views",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StorageGovernanceError("injected after c2 backup")))
    with pytest.raises(StorageGovernanceError, match="injected"):
        publisher.reconcile()
    pending_path = publisher.pending / "c2.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["backup"] = first_manifest["backup"]
    pending_path.write_text(
        json.dumps(pending, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")

    with pytest.raises(StorageGovernanceError, match="未包含预期终态"):
        CycleSnapshotPublisher(db_path=db_path, work_root=work).reconcile()
    assert not (publisher.cycles / "c2.json").exists()
    writer.close()


def test_normal_pending_cannot_be_rebound_to_later_db_cut(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile() == ["c1"]
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'done','v0','2026-07-12T00:01:00Z')")
    writer.commit()

    monkeypatch.setattr(
        publisher, "_publish_views",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StorageGovernanceError("injected after c2 backup")))
    with pytest.raises(StorageGovernanceError, match="injected"):
        publisher.reconcile()
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (3,1,1,'reasoning','v0')")
    writer.commit()
    later_backup = publisher._backup(3, "reasoning", allow_later_cycles=True)
    pending_path = publisher.pending / "c2.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["backup"] = later_backup
    pending_path.write_text(
        json.dumps(pending, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")

    with pytest.raises(StorageGovernanceError, match="不是该轮精确终态切面"):
        CycleSnapshotPublisher(db_path=db_path, work_root=work).reconcile()
    assert not (publisher.cycles / "c2.json").exists()
    writer.close()


def test_existing_history_gets_honest_bootstrap_then_one_snapshot_per_new_cycle(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'failed','v0','2026-07-12T00:01:00Z')")
    writer.commit()
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)

    assert publisher.reconcile(startup=True) == ["c2"]
    assert not (work / "state" / "storage" / "cycles" / "c1.json").exists()
    _, baseline = _pointer(work, 2)
    assert baseline["adoption_baseline"] is True
    assert baseline["bootstrap_before_cycle"] == 1

    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (3,1,1,'aborted','v0','2026-07-12T00:02:00Z')")
    writer.commit()
    assert publisher.reconcile() == ["c3"]
    _, third = _pointer(work, 3)
    assert third["previous_manifest_sha256"] == _pointer(work, 2)[0]["manifest_sha256"]
    assert _git(work, "rev-list", "--count", "HEAD") == "2"
    writer.close()


def test_startup_with_one_legacy_cycle_is_explicit_adoption(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)

    assert publisher.reconcile(startup=True) == ["c1"]
    _, manifest = _pointer(work)
    assert manifest["adoption_baseline"] is True
    assert manifest["bootstrap_before_cycle"] == 0
    writer.close()


def test_manifest_tamper_is_fail_closed(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    publisher.reconcile()
    pointer, _ = _pointer(work)
    manifest_path = work / pointer["manifest_path"]
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises(StorageGovernanceError, match="manifest hash"):
        publisher.reconcile()
    writer.close()


def test_manifest_digest_and_parser_consume_the_same_opened_bytes(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    publisher.reconcile()
    pointer, original = _pointer(work)
    manifest_path = work / pointer["manifest_path"]
    real_read = storage_module._read
    reads = {"manifest": 0}

    def replace_after_read(path, *, maximum=storage_module._MAX_JSON_BYTES):
        raw = real_read(path, maximum=maximum)
        if Path(path) == manifest_path:
            reads["manifest"] += 1
            if reads["manifest"] == 1:
                replacement = dict(original)
                replacement["cycle_status"] = "failed"
                manifest_path.chmod(0o600)
                manifest_path.write_text(
                    json.dumps(replacement, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n",
                    encoding="utf-8")
        return raw

    monkeypatch.setattr(storage_module, "_read", replace_after_read)
    validated = publisher._validate_pointer(1)
    assert validated["cycle_status"] == "done"       # parsed A, the bytes whose digest matched
    assert reads["manifest"] == 1                    # never reopened path to parse replacement B
    with pytest.raises(StorageGovernanceError, match="manifest hash"):
        publisher._validate_pointer(1)
    writer.close()


def test_owner_loss_after_git_commit_reuses_commit_without_publishing_old_pointer(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    state = {"armed": False, "failed": False}

    def owner_guard():
        head = work / "views" / ".git" / "HEAD"
        committed = False
        if head.exists():
            value = head.read_text(encoding="utf-8").strip()
            if value.startswith("ref: "):
                committed = (work / "views" / ".git" / value[5:]).exists()
        if state["armed"] and committed and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("lost owner after git commit")

    publisher = CycleSnapshotPublisher(
        db_path=db_path, work_root=work, owner_guard=owner_guard)
    state["armed"] = True
    with pytest.raises(RuntimeError, match="lost owner"):
        publisher.reconcile()
    assert not (work / "state" / "storage" / "cycles" / "c1.json").exists()
    assert _git(work, "rev-list", "--count", "HEAD") == "1"

    assert CycleSnapshotPublisher(db_path=db_path, work_root=work).reconcile() == ["c1"]
    assert _git(work, "rev-list", "--count", "HEAD") == "1"
    writer.close()


def test_later_cycle_owner_loss_reuses_exact_orphan_commit(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    state = {"armed": False, "failed": False}

    def owner_guard():
        git_dir = work / "views" / ".git"
        if not state["armed"] or not git_dir.is_dir():
            return
        count = subprocess.run(
            ["git", "-C", str(work / "views"), "rev-list", "--count", "HEAD"],
            check=False, text=True, capture_output=True).stdout.strip()
        if count == "2" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("lost owner after c2 git commit")

    publisher = CycleSnapshotPublisher(
        db_path=db_path, work_root=work, owner_guard=owner_guard)
    assert publisher.reconcile() == ["c1"]
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'done','v0','2026-07-12T00:01:00Z')")
    writer.commit()
    state["armed"] = True
    with pytest.raises(RuntimeError, match="c2 git commit"):
        publisher.reconcile()
    assert not (publisher.cycles / "c2.json").exists()
    assert _git(work, "rev-list", "--count", "HEAD") == "2"

    assert CycleSnapshotPublisher(db_path=db_path, work_root=work).reconcile() == ["c2"]
    assert _git(work, "rev-list", "--count", "HEAD") == "2"
    writer.close()


def test_pending_rejects_same_tree_rogue_git_commit(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile() == ["c1"]
    writer.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
        "VALUES (2,1,1,'done','v0','2026-07-12T00:01:00Z')")
    writer.commit()

    monkeypatch.setattr(
        publisher, "_publish_views",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StorageGovernanceError("injected before c2 views")))
    with pytest.raises(StorageGovernanceError, match="injected"):
        publisher.reconcile()
    pending = json.loads((publisher.pending / "c2.json").read_text(encoding="utf-8"))
    backup_path = work / pending["backup"]["path"]
    for name, rendered in publisher._render_views(2, "done", backup_path).items():
        (publisher.views / name).write_text(rendered, encoding="utf-8")
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(publisher.views), "add", "--", *storage_module.VIEW_FILES],
        check=True, text=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(publisher.views), "commit", "-m", "rogue same-tree commit"],
        check=True, text=True, capture_output=True)

    with pytest.raises(StorageGovernanceError, match="commit 身份或父链漂移"):
        CycleSnapshotPublisher(db_path=db_path, work_root=work).reconcile()
    assert not (publisher.cycles / "c2.json").exists()
    writer.close()


def test_multiple_unpublished_terminal_cycles_are_not_forged_from_one_live_cut(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert publisher.reconcile() == ["c1"]
    writer.executescript("""
      INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at)
        VALUES (2,1,1,'done','v0','2026-07-12T00:01:00Z');
      INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at)
        VALUES (3,1,1,'failed','v0','2026-07-12T00:02:00Z');
    """)
    writer.commit()

    with pytest.raises(StorageGovernanceError, match="多个未发布终态"):
        publisher.reconcile()
    assert not (work / "state" / "storage" / "cycles" / "c2.json").exists()
    writer.close()


def test_missing_pointer_inside_published_high_water_is_rejected(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    publisher.reconcile()
    for cycle_id, status in ((2, "done"), (3, "failed")):
        writer.execute(
            "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version,finished_at) "
            "VALUES (?,1,1,?,'v0','2026-07-12T00:03:00Z')", (cycle_id, status))
        writer.commit()
        assert publisher.reconcile() == [f"c{cycle_id}"]
    (work / "state" / "storage" / "cycles" / "c2.json").unlink()

    with pytest.raises(StorageGovernanceError, match="pointer 缺口"):
        publisher.reconcile()
    writer.close()


def test_rogue_git_head_blocks_before_another_research_round(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    publisher.reconcile()
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(work / "views"), "commit", "--allow-empty", "-m", "rogue"],
        check=True, text=True, capture_output=True)

    with pytest.raises(StorageGovernanceError, match="HEAD 超出"):
        publisher.reconcile()
    writer.close()


def test_validation_does_not_recreate_missing_git_repo(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    publisher.reconcile()
    (work / "views" / ".git").rename(work / "lost-views-git")

    with pytest.raises(StorageGovernanceError, match="Git 仓缺失"):
        publisher.reconcile()
    assert not (work / "views" / ".git").exists()
    writer.close()


def test_kill_left_atomic_temps_are_discarded_before_reconcile(tmp_path):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    first = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    names = {
        first.cycles / (".c1.json.tmp-" + "a" * 32),
        first.pending / (".c1.json.tmp-" + "b" * 32),
        first.manifests / ("." + "c" * 64 + ".json.tmp-" + "d" * 32),
        first.views / (".goal.md.tmp-" + "e" * 32),
        first.temporary / ("c1-" + "f" * 32 + ".sqlite"),
    }
    for path in names:
        path.write_bytes(b"partial")

    recovered = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert all(not path.exists() for path in names)
    assert recovered.reconcile() == ["c1"]
    writer.close()


def test_ambient_path_cannot_replace_trusted_git(tmp_path, monkeypatch):
    work = tmp_path / "work"
    db_path, writer = _seed_done(work)
    attacker = tmp_path / "attacker-bin"
    attacker.mkdir()
    fake_git = attacker / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(attacker))

    publisher = CycleSnapshotPublisher(db_path=db_path, work_root=work)
    assert Path(publisher.git_binary) != fake_git
    assert publisher.reconcile() == ["c1"]
    writer.close()
