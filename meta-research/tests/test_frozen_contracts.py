"""步⑧ CP8.2 回归锁：证「补齐 plan 契约缺口」未偷改任何冻结件。

核心承诺（与用户 + codex-chatgpt 联合设计）：**不解冻 plan.schema、不改 DDL/MIGRATION 锁**——命令改由
新增 additive 的 execution_manifest 承载。本套把冻结锚钉成字面值，任何对 plan.schema / DDL 的改动都会
在此炸出（防未来 session 无意漂移冻结契约）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from orchestrator import database as db

SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# 步⑧开工时 plan.schema.json 的字面 sha256（冻结锚）。若确需改 plan.schema（决策性），更新此常量并
# 走完整评审 + 记 build_log 说明为何——绝不为「让测试过」而顺手改锚。
_PLAN_SCHEMA_SHA256 = "84ac7f86b8b0f032a0b255d13671dbf547f21f1d01fc53948e5343f203e1531e"


def test_plan_schema_frozen():
    """plan.schema.json 未被塞入执行字段（train_cmd 等）——字面 sha256 锚。"""
    assert _sha256(SYSTEM_ROOT / "schemas" / "plan.schema.json") == _PLAN_SCHEMA_SHA256, (
        "plan.schema.json 变了——步⑧承诺不解冻 plan.schema（命令应在 execution_manifest）；"
        "若确为有意改动，更新 _PLAN_SCHEMA_SHA256 并走评审 + 记 build_log")


def test_plan_schema_has_no_execution_fields():
    """plan.schema 语义锁（比字面 sha 更抗无害排版漂移）：抽象 target 不得含执行/身份具体字段。"""
    text = (SYSTEM_ROOT / "schemas" / "plan.schema.json").read_text(encoding="utf-8")
    for forbidden in ("train_cmd", "smoke_cmd", "eval_cmd", "ckpt_path", "ckpt_name",
                      "identity_draft_md", "repro_cmd", "target_set_hash", "env_hash"):
        assert forbidden not in text, f"plan.schema 出现执行/身份具体字段 {forbidden!r}——契约分层被破坏（命令应在 execution_manifest）"


def test_migration_checksum_unchanged():
    """DDL 三重锁的 MIGRATION_SHA256 未变（步⑧全程零 DDL 改动的机器证明）。"""
    digest = _sha256(SYSTEM_ROOT / "db" / "migrations" / "0001_appendix_a.sql")
    assert digest == db.MIGRATION_SHA256, "migration 文件漂移——步⑧承诺不改 DDL"
