"""CP12.1 · Web 多研究任务 registry 与 quest 物理隔离。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import quest_registry as quest_registry_module
from orchestrator.quest_registry import (
    QuestConflictError,
    QuestCorruptError,
    QuestRegistry,
)
from orchestrator.qualification_firewall import CONTRACT_RELATIVE_PATH


SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _brief(name: str) -> str:
    return f"""---
predicate_json: {{"kind": "test", "name": "{name}"}}
---

# {name}

只用于 quest 隔离回归。
"""


def _scalar(db_path: Path, sql: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _contract(marker: str = "one") -> dict:
    return {"task": "T1", "marker": marker}


def _canonical(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _fake_qualification_installer(monkeypatch, *, fail: bool = False,
                                  observations=None):
    def install(work_root, value):
        work = Path(work_root)
        if observations is not None:
            observations.append({
                "brief_exists": (work / "goal_brief.md").is_file(),
                "db_exists": (work / "research.sqlite").exists(),
            })
        raw = _canonical(dict(value))
        path = work / CONTRACT_RELATIVE_PATH
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(raw)
        path.chmod(0o400)
        if fail:
            raise RuntimeError("injected qualification install failure")
        return SimpleNamespace(
            task=value["task"],
            contract_sha256="sha256:" + hashlib.sha256(raw).hexdigest())

    monkeypatch.setattr(quest_registry_module, "install_contract", install)


def test_create_two_quests_owns_separate_db_pool_tree_and_is_idempotent(tmp_path):
    registry = QuestRegistry(tmp_path / "research-quests", SYSTEM_ROOT)

    first = registry.create(
        quest_id="alpha", title="Alpha 研究", goal_brief_md=_brief("alpha"))
    second = registry.create(
        quest_id="beta", title="Beta 研究", goal_brief_md=_brief("beta"))

    assert first.created is True and second.created is True
    assert first.work_root != second.work_root
    assert first.db_path.stat().st_ino != second.db_path.stat().st_ino
    assert _scalar(first.db_path, "SELECT text FROM goal") == "# alpha\n\n只用于 quest 隔离回归。"
    assert _scalar(second.db_path, "SELECT text FROM goal") == "# beta\n\n只用于 quest 隔离回归。"

    # baseline/question 是每个 quest 自己的 SQLite 命名空间，不是 registry 共用池。
    conn = sqlite3.connect(first.db_path)
    conn.execute(
        "INSERT INTO baseline(slug,canonical_key,status) VALUES ('only-a','alpha-only','planned')")
    conn.execute(
        "INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
        "VALUES (1,1,1,'alpha question','open','human')")
    conn.commit()
    conn.close()
    assert _scalar(first.db_path, "SELECT count(*) FROM baseline") == 1
    assert _scalar(second.db_path, "SELECT count(*) FROM baseline") == 0
    assert _scalar(first.db_path, "SELECT count(*) FROM question") == 1
    assert _scalar(second.db_path, "SELECT count(*) FROM question") == 0

    replay = registry.create(
        quest_id="alpha", title="Alpha 研究", goal_brief_md=_brief("alpha"))
    assert replay.created is False
    assert replay.work_root == first.work_root
    assert [q.quest_id for q in registry.list()] == ["alpha", "beta"]


def test_create_conflict_and_path_escape_fail_closed(tmp_path):
    registry = QuestRegistry(tmp_path / "research-quests", SYSTEM_ROOT)
    registry.create(quest_id="alpha", title="Alpha", goal_brief_md=_brief("alpha"))

    with pytest.raises(QuestConflictError):
        registry.create(quest_id="alpha", title="Changed", goal_brief_md=_brief("other"))
    for unsafe in ("../escape", "a/b", ".hidden", "A_UPPER", "", "a" * 65):
        with pytest.raises(ValueError):
            registry.create(quest_id=unsafe, title="x", goal_brief_md=_brief("x"))
    assert not (tmp_path / "escape").exists()


def test_create_request_idempotency_key_is_durably_bound_to_body(tmp_path):
    root = tmp_path / "research-quests"
    key = "a" * 32
    body = {"quest_id": "alpha", "title": "Alpha", "goal_brief_md": _brief("alpha")}
    registry = QuestRegistry(root, SYSTEM_ROOT)
    registry.bind_create_request(key, body)
    registry.bind_create_request(key, dict(body))

    # A fresh registry instance sees the durable receipt, while changing even
    # one field under the same transport operation is a conflict.
    restarted = QuestRegistry(root, SYSTEM_ROOT)
    restarted.bind_create_request(key, body)
    with pytest.raises(QuestConflictError, match="Idempotency-Key"):
        restarted.bind_create_request(key, {**body, "quest_id": "beta"})
    receipt = root / "state" / "quest-create-requests" / f"{key}.json"
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_qualification_contract_is_installed_before_database_and_bound_in_manifest(
        tmp_path, monkeypatch):
    observations = []
    _fake_qualification_installer(monkeypatch, observations=observations)
    real_connect = quest_registry_module.database.connect

    def checked_connect(path):
        work = Path(path).parent
        contract_path = work / CONTRACT_RELATIVE_PATH
        assert contract_path.is_file()
        assert contract_path.stat().st_mode & 0o777 == 0o400
        return real_connect(path)

    monkeypatch.setattr(quest_registry_module.database, "connect", checked_connect)
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    quest = registry.create(
        quest_id="qualified", title="Qualified", goal_brief_md=_brief("qualified"),
        qualification_profile_id="t1-local",
        qualification_contract=_contract())

    assert observations == [{"brief_exists": True, "db_exists": False}]
    assert quest.created is True
    assert quest.qualification_profile_id == "t1-local"
    assert quest.qualification_task == "T1"
    assert quest.qualification_contract_sha256.startswith("sha256:")
    assert quest.public_dict()["qualification"] == {
        "profile_id": "t1-local", "task": "T1",
        "contract_sha256": quest.qualification_contract_sha256,
        "installed": True,
    }
    manifest = json.loads(
        (quest.work_root / "quest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["qualification"] == {
        "profile_id": "t1-local", "task": "T1",
        "contract_sha256": quest.qualification_contract_sha256,
    }
    assert quest.db_path.is_file()


def test_qualification_install_failure_never_publishes_partial_quest(
        tmp_path, monkeypatch):
    _fake_qualification_installer(monkeypatch, fail=True)
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)

    with pytest.raises(RuntimeError, match="injected qualification"):
        registry.create(
            quest_id="qualified", title="Qualified",
            goal_brief_md=_brief("qualified"),
            qualification_profile_id="t1-local",
            qualification_contract=_contract())

    assert not (registry.quests_dir / "qualified").exists()
    assert not list(registry.quests_dir.glob(".creating-qualified-*"))
    assert registry.list() == []


def test_manifest_v1_ordinary_quest_remains_readable_but_cannot_hide_contract(
        tmp_path):
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    quest = registry.create(
        quest_id="legacy", title="Legacy", goal_brief_md=_brief("legacy"))
    manifest_path = quest.work_root / "quest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.pop("qualification") is None
    manifest["version"] = 1
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n", encoding="utf-8")

    loaded = QuestRegistry(registry.root, SYSTEM_ROOT).get("legacy")
    assert loaded.qualification_profile_id is None
    assert loaded.qualification_task is None
    assert loaded.qualification_contract_sha256 is None
    assert loaded.public_dict()["qualification"] is None

    contract_path = loaded.work_root / CONTRACT_RELATIVE_PATH
    contract_path.parent.mkdir(parents=True, mode=0o700)
    contract_path.write_bytes(_canonical(_contract()))
    contract_path.chmod(0o400)
    with pytest.raises(QuestCorruptError, match="未声明 qualification"):
        registry.get("legacy")


def test_manifest_v2_null_qualification_cannot_hide_contract(tmp_path):
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    quest = registry.create(
        quest_id="ordinary", title="Ordinary", goal_brief_md=_brief("ordinary"))
    contract_path = quest.work_root / CONTRACT_RELATIVE_PATH
    contract_path.parent.mkdir(parents=True, mode=0o700)
    contract_path.write_bytes(_canonical(_contract()))
    contract_path.chmod(0o400)

    with pytest.raises(QuestCorruptError, match="未声明 qualification"):
        registry.get("ordinary")


def test_qualification_contract_missing_or_tampered_is_corrupt(
        tmp_path, monkeypatch):
    _fake_qualification_installer(monkeypatch)
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    first = registry.create(
        quest_id="missing", title="Missing", goal_brief_md=_brief("missing"),
        qualification_profile_id="t1-local",
        qualification_contract=_contract("missing"))
    missing_path = first.work_root / CONTRACT_RELATIVE_PATH
    missing_path.unlink()
    with pytest.raises(QuestCorruptError, match="contract"):
        registry.get("missing")

    second = registry.create(
        quest_id="tampered", title="Tampered", goal_brief_md=_brief("tampered"),
        qualification_profile_id="t1-local",
        qualification_contract=_contract("original"))
    tampered_path = second.work_root / CONTRACT_RELATIVE_PATH
    tampered_path.chmod(0o600)
    tampered_path.write_bytes(_canonical(_contract("changed")))
    tampered_path.chmod(0o400)
    with pytest.raises(QuestCorruptError, match="identity 漂移"):
        registry.get("tampered")

    third = registry.create(
        quest_id="wrong-mode", title="Wrong mode",
        goal_brief_md=_brief("wrong-mode"),
        qualification_profile_id="t1-local",
        qualification_contract=_contract("wrong-mode"))
    mode_path = third.work_root / CONTRACT_RELATIVE_PATH
    mode_path.chmod(0o600)
    with pytest.raises(QuestCorruptError, match="权限"):
        registry.get("wrong-mode")


def test_qualification_replay_is_exact_and_conflicting_identity_is_rejected(
        tmp_path, monkeypatch):
    _fake_qualification_installer(monkeypatch)
    registry = QuestRegistry(tmp_path / "registry", SYSTEM_ROOT)
    common = {
        "quest_id": "qualified", "title": "Qualified",
        "goal_brief_md": _brief("qualified"),
    }
    first = registry.create(
        **common, qualification_profile_id="t1-local",
        qualification_contract=_contract("one"))
    replay = registry.create(
        **common, qualification_profile_id="t1-local",
        qualification_contract=_contract("one"))
    assert first.created is True and replay.created is False
    assert replay.qualification_contract_sha256 == first.qualification_contract_sha256

    with pytest.raises(QuestConflictError):
        registry.create(
            **common, qualification_profile_id="t1-local",
            qualification_contract=_contract("two"))
    with pytest.raises(QuestConflictError):
        registry.create(
            **common, qualification_profile_id="another-profile",
            qualification_contract=_contract("one"))
    with pytest.raises(QuestConflictError):
        registry.create(**common)
    with pytest.raises(ValueError, match="成对"):
        registry.create(
            quest_id="bad-pair", title="Bad pair", goal_brief_md=_brief("bad"),
            qualification_profile_id="t1-local")
    with pytest.raises(ValueError, match="成对"):
        registry.create(
            quest_id="bad-pair", title="Bad pair", goal_brief_md=_brief("bad"),
            qualification_contract=_contract())


def test_t1_template_is_registry_bootstrap_input_and_keeps_dreamer_sealed(tmp_path):
    registry = QuestRegistry(tmp_path / "research-quests", SYSTEM_ROOT)
    quest = registry.create_from_template(
        quest_id="t1-eeg-universal", title="T1（开放 · 创新）跨数据集 EEG 通用规律发现",
        template_id="t1-eeg-universal")

    brief = quest.goal_brief_path.read_text(encoding="utf-8")
    assert "SEED / SEED-IV / FACED" in brief
    assert "主 claim ≤ 3" in brief
    assert "DREAMER" in brief and "仅作 D 阶段" in brief and "只评一次" in brief
    assert "label-permutation" in brief and "subject-ID / dataset-ID / trial-ID-only" in brief
    assert "可审计负结论" in brief
    assert _scalar(quest.db_path, "SELECT count(*) FROM goal") == 1
