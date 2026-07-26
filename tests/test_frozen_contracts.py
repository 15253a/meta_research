"""步⑧ CP8.2 回归锁：证「补齐 plan 契约缺口」未偷改任何冻结件。

核心承诺（与用户 + codex-chatgpt 联合设计）：plan 只承载抽象科学/资源意图，命令与具体执行身份由
execution_manifest 承载；DDL/MIGRATION 锁不动。本套把冻结锚钉成字面值，任何对 plan.schema / DDL 的改动
都会在此炸出（防未来 session 无意漂移冻结契约）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orchestrator import database as db

SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# CP13.1 有意加入 durable DAG / published-source / abstract resource 契约，
# 把训练 seed 从 variant identity 分离到 target replicate / durable run，并
# 将 parent_baseline 限定为会创建新 baseline identity 的 build target。
# 命令、env_hash 与具体 physical device identity 仍由下方语义锁禁止。
# 若再改 plan.schema（决策性），须更新此常量并走完整评审 + 记 build_log，绝不静默漂移。
_PLAN_SCHEMA_SHA256 = "713556e6adc71adb68607841d2c4e1beaf0835f45eee4e5e0e1d81d11fe3bd5c"


def test_plan_schema_frozen():
    """plan.schema.json 未被塞入命令/具体身份字段——字面 sha256 锚。"""
    assert _sha256(SYSTEM_ROOT / "schemas" / "plan.schema.json") == _PLAN_SCHEMA_SHA256, (
        "plan.schema.json 变了——抽象 plan 契约漂移（命令/具体身份应在 execution_manifest）；"
        "若确为有意改动，更新 _PLAN_SCHEMA_SHA256 并走评审 + 记 build_log")


def test_plan_schema_has_no_execution_fields():
    """plan.schema 语义锁（比字面 sha 更抗无害排版漂移）：抽象 target 不得含执行/身份具体字段。"""
    schema = json.loads(
        (SYSTEM_ROOT / "schemas" / "plan.schema.json").read_text(encoding="utf-8"))

    def property_names(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                yield from props
            for value in node.values():
                yield from property_names(value)
        elif isinstance(node, list):
            for value in node:
                yield from property_names(value)

    names = set(property_names(schema))
    for forbidden in ("train_cmd", "smoke_cmd", "eval_cmd", "ckpt_path", "ckpt_name",
                      "identity_draft_md", "repro_cmd", "target_set_hash", "env_hash"):
        assert forbidden not in names, (
            f"plan.schema 出现执行/身份具体字段 {forbidden!r}——契约分层被破坏"
            "（命令应在 execution_manifest）")


def test_migration_checksum_unchanged():
    """原始附录 A migration 保持 byte-for-byte frozen。"""
    digest = _sha256(SYSTEM_ROOT / "db" / "migrations" / "0001_appendix_a.sql")
    assert digest == db.MIGRATION_SHA256, "0001 migration 文件漂移"


def test_bundle_dag_additive_migration_checksum_locked():
    digest = _sha256(
        SYSTEM_ROOT / "db" / "migrations" / "0002_bundle_target_dag.sql")
    assert digest == db.BUNDLE_DAG_MIGRATION_SHA256
