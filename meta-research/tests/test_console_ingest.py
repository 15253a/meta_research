"""CP9.3 · 人类控制台入站闭环（ConsoleInboxIngest + precheck 装配）。

验收：控制台命令经 console_server 落 console_inbox.jsonl → run 进程 precheck 边界 ingest 进权威入站链：
①directive 意图 → 落 pending directive（幂等）；②query 意图 → mediator 应答写 interaction_reply；
③游标持久化 + 幂等重放不重复；④torn-tail/坏行容错；⑤入站失败不崩推进主循环（辅助面健壮性）；
⑥precheck 装配端到端：pause→确认→precheck 阻断，resume→确认→解阻断。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.console import Console
from orchestrator.console_ingest import ConsoleInboxIngest
from orchestrator.mediator import Mediator, open_responder_read_conn
from orchestrator.notify import FileRequestService, make_advancer_precheck
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path):
    """文件库 + 已发布卡 + Console/Mediator/ingest + 空 console_inbox spool。"""
    dbp = str(tmp_path / "r.sqlite")
    daemon = WriteDaemon(db.connect(dbp))
    conftest.seed_minimal(daemon.conn)
    daemon.conn.execute("UPDATE cycle SET active_question_id=1, route='attack' WHERE id=1")
    daemon.conn.commit()
    work = tmp_path                                    # work_root：state/ 落 card + inbox + cursor
    card_path = work / "state" / "status_card.json"
    pub = SC.SqliteStatusPublisher(open_responder_read_conn(dbp), policy=POLICY,
                                   goal_body_md="目标首行\n次行", out_path=str(card_path))
    pub.publish("c1")
    console = Console(daemon)
    mediator = Mediator(daemon, str(card_path))
    file_requests = FileRequestService(daemon, SchemaSet(SYSTEM_ROOT / "schemas"), POLICY,
                                       str(tmp_path / "managed-input"))
    ingest = ConsoleInboxIngest(console, mediator, str(work), file_requests=file_requests,
                                system_root=str(SYSTEM_ROOT))
    inbox = work / "state" / "console_inbox.jsonl"
    return {"daemon": daemon, "console": console, "mediator": mediator, "ingest": ingest,
            "file_requests": file_requests, "managed_input": tmp_path / "managed-input",
            "inbox": inbox, "work": work, "card_path": card_path, "pub": pub}


def _spool(inbox: Path, *recs, terminated: bool = True) -> None:
    """把若干 {connector,raw_text,seq,idempotency_key} 记录写进 spool（模拟 console_server append）。"""
    inbox.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    if not terminated and body.endswith("\n"):
        body = body[:-1]                               # 末行去换行 = console_server append 中途（未 committed）
    inbox.write_text(body, encoding="utf-8")


def _rec(seq: int, raw: str, connector: str = "console") -> dict:
    return {"connector": connector, "raw_text": raw, "seq": seq, "idempotency_key": f"console-{seq}"}


def _action_rec(seq: int, action: str, directive_id: int, *, reason: str = "") -> dict:
    rec = _rec(seq, f"{'确认' if action == 'confirm' else '拒绝'}指令 d{directive_id}")
    rec.update({"action": action, "directive_id": directive_id})
    if action == "reject":
        rec["reason"] = reason
    return rec


def _file_action_rec(seq: int, action: str, request_id: int, *, source_ref: str = "", reason: str = "") -> dict:
    raw = (f"解决文件请求 r{request_id}，来源 {source_ref}" if action == "resolve" else
           f"取消文件请求 r{request_id}：{reason}")
    rec = _rec(seq, raw)
    rec.update({"action_target": "file_request", "action": action, "request_id": request_id})
    if action == "resolve":
        rec["source_ref"] = source_ref
    else:
        rec["reason"] = reason
    return rec


def _file_request():
    return {"summary_md": "需要用户提供数据", "items": [{
        "kind": "dataset", "desc": "toy 数据", "expected_files": ["data.bin"],
        "attempted_paths": ["/data/toy"], "failure_reason": "本机不存在", "dest_hint": "input/"}]}


def _restart_ingest(env):
    """模拟默认 CLI/进程重启：重试预算只能来自 work/state sidecar。"""
    return ConsoleInboxIngest(
        env["console"], env["mediator"], str(env["work"]),
        file_requests=env["file_requests"], system_root=str(SYSTEM_ROOT))


def _cursor_at_inbox_end(env) -> bool:
    cursor = json.loads((env["work"] / "state" / "console_inbox.cursor").read_text())
    return cursor["offset"] == env["inbox"].stat().st_size


# ---------------- ① directive 意图 → 落 pending directive ----------------
def test_ingest_directive_creates_pending(env):
    _spool(env["inbox"], _rec(1, "暂停一下"))
    assert env["ingest"].ingest() == 1
    msg = env["daemon"].query_one("SELECT id FROM interaction_message WHERE connector='console' AND idempotency_key='console-1'")
    assert msg is not None
    d = env["daemon"].query_one("SELECT kind, status, json_extract(payload_json,'$.confirmed') FROM directive WHERE kind='pause'")
    assert d == ("pause", "pending", 0)                # 硬指令 confirmed=false（待回显确认）


# ---------------- ② query 意图 → mediator 应答写 reply ----------------
def test_ingest_query_writes_reply(env):
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    assert env["ingest"].ingest() == 1
    mid = env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    rep = env["daemon"].query_one("SELECT id FROM interaction_reply WHERE message_id=?", (mid,))
    assert rep is not None                             # query → 应答落 interaction_reply


# ---------------- ③ 幂等 + 游标 ----------------
def test_idempotent_reingest_and_cursor(env):
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "现在进展如何"))
    assert env["ingest"].ingest() == 2
    assert env["ingest"].ingest() == 0                 # 游标挡住重扫
    # 只一条 pause directive（未因重放重复）
    n = env["daemon"].query_one("SELECT COUNT(*) FROM directive WHERE kind='pause'")[0]
    assert n == 1
    assert _cursor_at_inbox_end(env)
    # 追加新行 → 只处理增量
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "现在进展如何"), _rec(3, "备注：留意长上下文"))
    assert env["ingest"].ingest() == 1


def test_cursor_lost_replays_safely(env):
    """游标丢失 → 全量重放安全（interaction_message UNIQUE 兜底，不重复建 message）。"""
    _spool(env["inbox"], _rec(1, "暂停一下"))
    env["ingest"].ingest()
    (env["work"] / "state" / "console_inbox.cursor").unlink()   # 模拟游标丢失
    env["ingest"].ingest()                             # 重放
    n = env["daemon"].query_one("SELECT COUNT(*) FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert n == 1                                      # 幂等：不重复


def test_confirm_action_durable_provenance_and_replay(env):
    """控件 confirm 先落 interaction_message，再绑定 confirmation provenance；游标丢失重放 no-op。"""
    _spool(env["inbox"], _rec(1, "暂停一下"))
    assert env["ingest"].ingest() == 1
    did = env["daemon"].query_one("SELECT id FROM directive WHERE kind='pause'")[0]
    _spool(env["inbox"], _rec(1, "暂停一下"), _action_rec(2, "confirm", did))
    assert env["ingest"].ingest() == 1
    mid = env["daemon"].query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key='console-2'")[0]
    assert env["daemon"].query_one(
        "SELECT intent, directive_id FROM interaction_classification WHERE message_id=?", (mid,)) == ("unclear", None)
    assert env["daemon"].query_one(
        "SELECT json_extract(payload_json,'$.confirmed'), "
        "json_extract(payload_json,'$.confirmation_message_id') FROM directive WHERE id=?", (did,)) == (1, mid)

    (env["work"] / "state" / "console_inbox.cursor").unlink()
    assert env["ingest"].ingest() == 2                 # 全量重放成功
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_message")[0] == 2
    assert env["daemon"].query_one(
        "SELECT json_extract(payload_json,'$.confirmation_message_id') FROM directive WHERE id=?", (did,))[0] == mid


def test_reject_action_is_idempotent(env):
    _spool(env["inbox"], _rec(1, "暂停一下"))
    env["ingest"].ingest()
    did = env["daemon"].query_one("SELECT id FROM directive WHERE kind='pause'")[0]
    _spool(env["inbox"], _rec(1, "暂停一下"), _action_rec(2, "reject", did, reason="不是我的意思"))
    assert env["ingest"].ingest() == 1
    assert env["daemon"].query_one(
        "SELECT status, json_extract(payload_json,'$.rejection_reason'), "
        "json_extract(payload_json,'$.rejection_message_id') FROM directive WHERE id=?", (did,)
    ) == ("rejected", "不是我的意思", 2)
    (env["work"] / "state" / "console_inbox.cursor").unlink()
    assert env["ingest"].ingest() == 2
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_message")[0] == 2


def test_unicode_line_separator_inside_json_is_not_a_spool_boundary(env):
    """JSON 合法 U+2028/U+2029 只是字符串内容，cursor 只能按物理 LF 计数。"""
    _spool(env["inbox"], _rec(1, "备注: 第一行\u2028仍是同一条"), _rec(2, "备注: 第二条"))
    assert env["ingest"].ingest() == 2
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_message")[0] == 2
    assert _cursor_at_inbox_end(env)


@pytest.mark.parametrize("bad_id", [1.0, 1.9, "01", (1 << 63), 10 ** 100, True])
def test_directive_action_bad_id_is_poison_not_alias_or_cursor_wedge(env, bad_id):
    created = env["console"].handle_inbound(
        connector="seed", raw_text="暂停", idempotency_key="seed-pause")
    rec = _action_rec(1, "confirm", bad_id)
    _spool(env["inbox"], rec, _rec(2, "备注: 后续仍可消费"))
    assert env["ingest"].ingest() == 1                         # 坏动作跳过，后续 note 成功
    assert env["daemon"].query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
        (created["directive_id"],))[0] == 0                    # 1.9 绝不能截断成 d1
    assert _cursor_at_inbox_end(env)


@pytest.mark.parametrize("bad_id", [1.0, 1.9, "01", (1 << 63), 10 ** 100, True])
def test_file_action_bad_id_does_not_wedge_following_message(env, bad_id):
    _spool(env["inbox"], _file_action_rec(1, "cancel", bad_id, reason="x"),
           _rec(2, "备注: 后续仍可消费"))
    assert env["ingest"].ingest() == 1
    assert _cursor_at_inbox_end(env)


@pytest.mark.parametrize("bad", [
    {"idempotency_key": "x" * 257},
    {"connector": "qq"},
])
def test_spool_identity_boundaries_poison_without_wedging_following_message(env, bad):
    rec = _rec(1, "备注: 不可信 identity")
    rec.update(bad)
    _spool(env["inbox"], rec, _rec(2, "备注: 后续仍可消费"))
    assert env["ingest"].ingest() == 1
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-2'") == (1,)
    assert _cursor_at_inbox_end(env)


def test_action_idempotency_collision_does_not_pollute_old_message(env):
    """撞键 action 只拒当前 spool 行；不能给旧 query 追加失败 reply 或改变目标 directive。"""
    console, daemon = env["console"], env["daemon"]
    original = console.handle_inbound(
        connector="console", raw_text="现在进展如何", idempotency_key="collision-old")
    env["mediator"].handle_query(message_id=original["message_id"])
    pause = console.handle_inbound(
        connector="console", raw_text="暂停", idempotency_key="collision-target")
    rec = _action_rec(1, "confirm", pause["directive_id"])
    rec["idempotency_key"] = "collision-old"
    _spool(env["inbox"], rec)

    assert env["ingest"].ingest() == 0
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_reply WHERE message_id=?", (original["message_id"],)) == (1,)
    assert daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
        (pause["directive_id"],)) == (0,)
    assert _cursor_at_inbox_end(env)


def test_same_raw_nonce_cannot_upgrade_ordinary_message_into_directive_action(env):
    """普通消息即使逐字伪装成控件 raw，也不能被同 nonce 的后续 action 升权。"""
    console, daemon = env["console"], env["daemon"]
    pause = console.handle_inbound(
        connector="seed", raw_text="暂停", idempotency_key="domain-target")
    did = pause["directive_id"]
    ordinary = _rec(1, f"确认指令 d{did}")
    action = _action_rec(2, "confirm", did)
    action["idempotency_key"] = ordinary["idempotency_key"]
    _spool(env["inbox"], ordinary, action)

    assert env["ingest"].ingest() == 1
    assert daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?", (did,)) == (0,)
    assert daemon.query_one(
        "SELECT session_ref FROM interaction_message WHERE connector='console' "
        "AND idempotency_key=?", (ordinary["idempotency_key"],)) == ("console-op:message:v1",)
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_message WHERE connector='console' "
        "AND idempotency_key=?", (ordinary["idempotency_key"],)) == (1,)
    assert _cursor_at_inbox_end(env)


def test_directive_and_file_actions_cannot_share_operation_nonce(env):
    """首个 directive action 可成功；同 nonce 的 file action 必须零迁移、零新 provenance。"""
    console, daemon = env["console"], env["daemon"]
    pause = console.handle_inbound(
        connector="seed", raw_text="暂停", idempotency_key="domain-pause")
    did = pause["directive_id"]
    rid = env["file_requests"].create_checked(
        goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    first = _action_rec(1, "confirm", did)
    second = _file_action_rec(2, "cancel", rid, reason="不得执行")
    second["idempotency_key"] = first["idempotency_key"]
    _spool(env["inbox"], first, second)

    assert env["ingest"].ingest() == 1
    assert daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?", (did,)) == (1,)
    assert daemon.query_one(
        "SELECT status,resolved_message_id FROM interaction_request WHERE id=?", (rid,)) == ("pending", None)
    assert daemon.query_one(
        "SELECT COUNT(*) FROM interaction_message WHERE connector='console' "
        "AND idempotency_key=?", (first["idempotency_key"],)) == (1,)
    assert daemon.query_one(
        "SELECT session_ref FROM interaction_message WHERE connector='console' "
        "AND idempotency_key=?", (first["idempotency_key"],)) == (
            "console-op:directive-action:v1",)
    assert _cursor_at_inbox_end(env)


def test_action_failure_receipt_is_terminal_across_cursor_replay(env):
    """路径连续五次不可用并回执后，即使后来变合法且 cursor 丢失，也不得把失败动作复活。"""
    svc, d = env["file_requests"], env["daemon"]
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    rec = _file_action_rec(1, "resolve", rid, source_ref=f"work/uploads/r{rid}")
    _spool(env["inbox"], rec)
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        assert env["ingest"].ingest() == 0                      # 前四次保留队首，允许上传完成
    assert env["ingest"].ingest() == 0                          # 第五次统一转 terminal failure receipt
    mid = d.query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert d.query_one("SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))[0].startswith(
        "[console-action-failed]")
    src = env["work"] / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"TOO-LATE")
    (env["work"] / "state" / "console_inbox.cursor").unlink()
    assert env["ingest"].ingest() == 0
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_persistent_resolve_failure_gets_receipt_then_following_cancel_runs(env, monkeypatch):
    """跨 run 重启仍累计五次；durable failure reply 后推进，后续 cancel 可达。"""
    svc, d = env["file_requests"], env["daemon"]
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    src = env["work"] / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"DATA")
    monkeypatch.setattr(svc, "resolve", lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
    _spool(env["inbox"], _file_action_rec(1, "resolve", rid, source_ref=f"work/uploads/r{rid}"),
           _file_action_rec(2, "cancel", rid, reason="改为取消"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        env["ingest"] = _restart_ingest(env)
        assert env["ingest"].ingest() == 0
    env["ingest"] = _restart_ingest(env)
    assert env["ingest"].ingest() == 1                         # 第 5 次回执失败 resolve，并继续 cancel
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "cancelled"
    assert d.query_one("SELECT COUNT(*) FROM interaction_reply WHERE reply_text LIKE '[console-action-failed]%' ")[0] == 1
    retry_state = json.loads((env["work"] / "state" / ".console_inbox.retry.json").read_text())
    assert retry_state == {}                                   # 终态成功后清理持久计数


def test_directive_success_then_retry_clear_failure_replays_without_false_failure(env, monkeypatch):
    """确认已提交后 sidecar 持续失败：只探测 sidecar，恢复后按 provenance 收敛而不重做确认。"""

    _spool(env["inbox"], _rec(1, "暂停"))
    assert env["ingest"].ingest() == 1
    did = env["daemon"].query_one("SELECT id FROM directive WHERE kind='pause'")[0]
    _spool(env["inbox"], _rec(1, "暂停"), _action_rec(2, "confirm", did))

    real_confirm = env["console"].confirm_directive
    monkeypatch.setattr(
        env["console"], "confirm_directive",
        lambda **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    assert env["ingest"].ingest() == 0                          # 先制造 durable retry count=1
    confirm_calls = {"n": 0}

    def counted_confirm(**kwargs):
        confirm_calls["n"] += 1
        return real_confirm(**kwargs)

    monkeypatch.setattr(env["console"], "confirm_directive", counted_confirm)

    real_store = env["ingest"].spool.store_retry_counts
    monkeypatch.setattr(
        env["ingest"].spool, "store_retry_counts",
        lambda _counts: (_ for _ in ()).throw(OSError("disk full")))
    assert env["ingest"].ingest() == 0                          # confirm 已提交，clear 失败，cursor 保留
    assert confirm_calls["n"] == 1
    mid = env["daemon"].query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key='console-2'")[0]
    assert env["daemon"].query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?", (did,)) == (1,)
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_reply WHERE message_id=?", (mid,)) is None

    # sidecar 仍坏时只重试持久化旧计数，绝不能再次触发已经成功的业务动作。
    assert env["ingest"].ingest() == 0
    assert env["ingest"].ingest() == 0
    assert confirm_calls["n"] == 1

    monkeypatch.setattr(env["ingest"].spool, "store_retry_counts", real_store)
    assert env["ingest"].ingest() == 0                          # 恢复拍只修复 rolled-back sidecar 状态
    assert confirm_calls["n"] == 1
    assert env["ingest"].ingest() == 1                          # 下一拍按 durable provenance 清计数并推进
    assert confirm_calls["n"] == 1
    assert json.loads(env["ingest"].retry_path.read_text()) == {}
    assert _cursor_at_inbox_end(env)


def test_file_success_then_retry_clear_failure_replays_without_false_failure(env, monkeypatch):
    """文件已原子 resolve 后 sidecar 持续失败：恢复前后都不得再次复制/resolve。"""
    svc, d = env["file_requests"], env["daemon"]
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    src = env["work"] / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"DATA")
    _spool(env["inbox"], _file_action_rec(1, "resolve", rid, source_ref=f"work/uploads/r{rid}"))

    real_resolve = svc.resolve
    monkeypatch.setattr(svc, "resolve", lambda **_kw: (_ for _ in ()).throw(OSError("transient")))
    assert env["ingest"].ingest() == 0
    resolve_calls = {"n": 0}

    def counted_resolve(**kwargs):
        resolve_calls["n"] += 1
        return real_resolve(**kwargs)

    monkeypatch.setattr(svc, "resolve", counted_resolve)
    real_store = env["ingest"].spool.store_retry_counts
    monkeypatch.setattr(
        env["ingest"].spool, "store_retry_counts",
        lambda _counts: (_ for _ in ()).throw(OSError("disk full")))
    assert env["ingest"].ingest() == 0
    assert resolve_calls["n"] == 1
    status, mid = d.query_one(
        "SELECT status,resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert status == "resolved"
    assert d.query_one("SELECT 1 FROM interaction_reply WHERE message_id=?", (mid,)) is None

    assert env["ingest"].ingest() == 0
    assert env["ingest"].ingest() == 0
    assert resolve_calls["n"] == 1

    monkeypatch.setattr(env["ingest"].spool, "store_retry_counts", real_store)
    assert env["ingest"].ingest() == 0                          # sidecar recovery probe
    assert resolve_calls["n"] == 1
    assert env["ingest"].ingest() == 1
    assert resolve_calls["n"] == 1
    assert json.loads(env["ingest"].retry_path.read_text()) == {}
    assert _cursor_at_inbox_end(env)


def test_corrupt_retry_state_fails_closed_until_repaired(env):
    """坏 sidecar 不能静默重置为 0；修复前不消费 spool、不写 cursor。"""
    retry = env["work"] / "state" / ".console_inbox.retry.json"
    retry.write_text("{broken", encoding="utf-8")
    env["ingest"] = _restart_ingest(env)
    _spool(env["inbox"], _rec(1, "备注: 不得越过坏 retry state"))
    assert env["ingest"].ingest() == 0
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-1'") is None
    assert retry.read_text(encoding="utf-8") == "{broken"       # fail-closed，不覆盖取证内容

    retry.write_text("{}", encoding="utf-8")
    assert env["ingest"].ingest() == 1                          # 同实例每拍重载，修复后自愈
    assert _cursor_at_inbox_end(env)


def test_retry_persist_failure_does_not_advance_or_keep_memory_only_count(env, monkeypatch):
    """计数落盘失败时既不推进 cursor，也不把仅内存计数当成跨重启真相。"""
    _spool(env["inbox"], _rec(1, "暂停"))
    monkeypatch.setattr(
        env["console"], "handle_inbound",
        lambda **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    monkeypatch.setattr(
        env["ingest"].spool, "store_retry_counts",
        lambda _counts: (_ for _ in ()).throw(OSError("disk full")))
    assert env["ingest"].ingest() == 0
    assert env["ingest"]._attempts == {}
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()


def test_corrupt_cursor_beyond_file_resets_to_durable_replay(env):
    _spool(env["inbox"], _rec(1, "备注: 不得被越界 cursor 永久跳过"))
    cursor = env["work"] / "state" / "console_inbox.cursor"
    cursor.write_text("999999", encoding="utf-8")
    assert env["ingest"].ingest() == 1
    assert _cursor_at_inbox_end(env)


def test_file_request_resolve_action_provenance_replay_and_unblock(env):
    """spool resolve 由单写 ingest 复制/hash/迁终态；游标丢失按 resolved_message_id 幂等重放。"""
    svc, d = env["file_requests"], env["daemon"]
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    assert "文件请求" in make_advancer_precheck(env["console"], d)()
    src = env["work"] / "uploads" / f"r{rid}" / "1"
    src.mkdir(parents=True); (src / "data.bin").write_bytes(b"CONSOLE-DATA")
    rec = _file_action_rec(1, "resolve", rid, source_ref=f"work/uploads/r{rid}")
    _spool(env["inbox"], rec)

    assert env["ingest"].ingest() == 1
    status, mid = d.query_one(
        "SELECT status,resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert status == "resolved"
    assert d.query_one("SELECT goal_id,goal_ver,raw_text FROM interaction_message WHERE id=?", (mid,)) == (
        1, 1, rec["raw_text"])
    assert d.query_one("SELECT intent FROM interaction_classification WHERE message_id=?", (mid,))[0] == "unclear"
    copied = env["managed_input"] / "user_provided" / str(rid) / "1" / "asset-1"
    assert copied.read_bytes() == b"CONSOLE-DATA"
    assert make_advancer_precheck(env["console"], d)() is None

    (env["work"] / "state" / "console_inbox.cursor").unlink()
    assert env["ingest"].ingest() == 1                         # 同 action/message replay no-op
    assert d.query_one("SELECT COUNT(*) FROM interaction_message WHERE idempotency_key='console-1'")[0] == 1
    assert d.query_one("SELECT resolved_message_id FROM interaction_request WHERE id=?", (rid,))[0] == mid


def test_file_request_cancel_action(env):
    svc, d = env["file_requests"], env["daemon"]
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_file_request())
    _spool(env["inbox"], _file_action_rec(1, "cancel", rid, reason="暂时无法提供"))
    assert env["ingest"].ingest() == 1
    status, resolution, mid = d.query_one(
        "SELECT status,resolution_json,resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert status == "cancelled" and json.loads(resolution)["reason"] == "暂时无法提供"
    assert d.query_one("SELECT goal_id FROM interaction_message WHERE id=?", (mid,))[0] == 1


# ---------------- ④ torn-tail / 坏行容错 ----------------
def test_torn_tail_deferred(env):
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "现在进展如何"), terminated=False)
    assert env["ingest"].ingest() == 1                 # 末行未 committed → 只处理第 1 行
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "现在进展如何"), terminated=True)  # 补上换行
    assert env["ingest"].ingest() == 1                 # 下轮处理第 2 行


def test_poison_line_skipped_cursor_advances(env):
    inbox = env["inbox"]; inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps(_rec(1, "暂停一下")) + "\n"
                     + "{坏 json 不完整\n"
                     + json.dumps(_rec(3, "现在进展如何")) + "\n", encoding="utf-8")
    assert env["ingest"].ingest() == 2                 # 坏行跳过，两条好行处理
    assert env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-3'") is not None


def test_oversized_committed_record_is_poison_and_following_action_runs(env):
    """有界 reader 对超长已提交行给出可推进 poison，不让后续动作永久卡住。"""
    from orchestrator.console_spool import MAX_RECORD_BYTES

    env["inbox"].parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(_rec(2, "备注: 超长坏行之后仍可达"), ensure_ascii=False).encode("utf-8")
    env["inbox"].write_bytes(b"x" * (MAX_RECORD_BYTES + 1) + b"\n" + good + b"\n")
    assert env["ingest"].ingest() == 1
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-2'") == (1,)
    assert _cursor_at_inbox_end(env)


def test_invalid_utf8_record_is_poison_and_following_action_runs(env):
    good = json.dumps(_rec(2, "备注: 非 UTF-8 坏行之后仍可达"), ensure_ascii=False).encode("utf-8")
    env["inbox"].parent.mkdir(parents=True, exist_ok=True)
    env["inbox"].write_bytes(b'{"raw_text":"\xff"}\n' + good + b"\n")
    assert env["ingest"].ingest() == 1
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-2'") == (1,)
    assert _cursor_at_inbox_end(env)


# ---------------- ⑤ 入站内部持久故障 → durable unclear + reply 后才推进 ----------------
@pytest.mark.parametrize("error_type", [RuntimeError, sqlite3.OperationalError])
def test_handle_inbound_persistent_failure_gets_terminal_receipt_and_does_not_wedge(env, monkeypatch,
                                                                                   error_type):
    """通过 shape 闸后的任意内部异常都有限重试；达上限后留下权威终态，后续消息仍可达。"""
    classifier = env["console"].classifier
    real_classify = classifier.classify
    failed_raw = "无法分类的控制台消息"
    calls = {"n": 0}

    def fail_first(message):
        if message["raw_text"] == failed_raw:
            calls["n"] += 1
            raise error_type("模拟分类器持久故障")
        return real_classify(message)

    monkeypatch.setattr(classifier, "classify", fail_first)
    _spool(env["inbox"], _rec(1, failed_raw), _rec(2, "备注: 后续仍可消费"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        assert env["ingest"].ingest() == 0
        assert not (env["work"] / "state" / "console_inbox.cursor").exists()

    assert env["ingest"].ingest() == 1                 # 首条终态化（不计 ok），同批后续 note 成功
    assert calls["n"] == env["ingest"]._MAX_ATTEMPTS
    mid = env["daemon"].query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert env["daemon"].query_one(
        "SELECT intent,directive_id FROM interaction_classification WHERE message_id=?", (mid,)) == (
            "unclear", None)
    replies = env["daemon"].query(
        "SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))
    assert len(replies) == 1 and replies[0][0].startswith("[console-inbound-failed]")
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-2'") == (1,)
    assert json.loads(env["ingest"].retry_path.read_text()) == {}
    assert _cursor_at_inbox_end(env)


def test_pre_message_persistent_failure_terminalizes_in_correct_operation_domain(env, monkeypatch):
    """handle_inbound 在 message 落库前持续失败时，第五拍仍须先 durable unclear+reply 才越过记录。"""
    console = env["console"]
    real_handle = console.handle_inbound
    failed_raw = "写权威消息前持续失败"
    calls = {"n": 0}

    def fail_before_inbound(**kwargs):
        if kwargs["raw_text"] == failed_raw:
            calls["n"] += 1
            raise sqlite3.OperationalError("模拟 message 落库前持久故障")
        return real_handle(**kwargs)

    monkeypatch.setattr(console, "handle_inbound", fail_before_inbound)
    _spool(env["inbox"], _rec(1, failed_raw), _rec(2, "备注: 前置失败后续仍可达"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        assert env["ingest"].ingest() == 0
        assert env["daemon"].query_one(
            "SELECT 1 FROM interaction_message WHERE idempotency_key='console-1'") is None
        assert not (env["work"] / "state" / "console_inbox.cursor").exists()

    assert env["ingest"].ingest() == 1
    assert calls["n"] == env["ingest"]._MAX_ATTEMPTS
    mid, session_ref = env["daemon"].query_one(
        "SELECT id,session_ref FROM interaction_message WHERE idempotency_key='console-1'")
    assert session_ref == "console-op:message:v1"
    assert env["daemon"].query_one(
        "SELECT intent,directive_id FROM interaction_classification WHERE message_id=?", (mid,)) == (
            "unclear", None)
    assert env["daemon"].query_one(
        "SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))[0].startswith(
            "[console-inbound-failed]")
    assert env["daemon"].query_one(
        "SELECT 1 FROM interaction_message WHERE idempotency_key='console-2'") == (1,)
    assert json.loads(env["ingest"].retry_path.read_text()) == {}
    assert _cursor_at_inbox_end(env)


# ---------------- ⑥ precheck 装配端到端：pause→确认→阻断→resume→解阻断 ----------------
def test_precheck_wrapper_pause_resume_end_to_end(env):
    console, daemon, ingest = env["console"], env["daemon"], env["ingest"]
    base = make_advancer_precheck(console, daemon)

    def precheck(cyc=None):                            # 复刻 run.py 装配（ingest 先跑、再 base）
        ingest.ingest(cyc)
        return base(cyc)

    # 无入站 → 放行
    assert precheck() is None
    # pause 入站 → 生成 pending（硬，未确认）→ 不阻断（未确认硬指令不进消费队）
    _spool(env["inbox"], _rec(1, "暂停一下"))
    assert precheck() is None
    did = daemon.query_one("SELECT id FROM directive WHERE kind='pause'")[0]
    # 回显确认也经 spool/ingest，先 durable message 再绑 provenance → precheck 消费 pause 并阻断
    _spool(env["inbox"], _rec(1, "暂停一下"), _action_rec(2, "confirm", did))
    assert precheck() == "pause 指令生效中（等待 resume）"
    # resume 入站 + 确认 → 解阻断
    _spool(env["inbox"], _rec(1, "暂停一下"), _action_rec(2, "confirm", did),
           _rec(3, "继续跑"))
    precheck()                                         # ingest resume（生成 pending 硬指令）
    rid = daemon.query_one("SELECT id FROM directive WHERE kind='resume'")[0]
    _spool(env["inbox"], _rec(1, "暂停一下"), _action_rec(2, "confirm", did),
           _rec(3, "继续跑"), _action_rec(4, "confirm", rid))
    assert precheck() is None                          # resume 消费 → 解阻断


def test_precheck_wrapper_query_replies(env):
    """装配下 query 入站也在 precheck 边界被应答（reply 落库）。"""
    console, daemon, ingest = env["console"], env["daemon"], env["ingest"]
    base = make_advancer_precheck(console, daemon)
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    ingest.ingest(); base()
    mid = daemon.query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert daemon.query_one("SELECT id FROM interaction_reply WHERE message_id=?", (mid,)) is not None


def test_no_inbox_file_noop(env):
    assert env["ingest"].ingest() == 0                 # spool 不存在 → 0，不炸


# ---------------- ⑦ query-once：游标丢失/重放不重复回复（内审 BLOCKER 修复钉）----------------
def test_query_not_answered_twice_on_cursor_loss(env):
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    env["ingest"].ingest()
    mid = env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_reply WHERE message_id=?", (mid,))[0] == 1
    (env["work"] / "state" / "console_inbox.cursor").unlink()   # 游标丢失 → 全量重放
    env["ingest"].ingest()
    # handle_query 非幂等，但 reply 存在性守卫 → 不重复回复（query-once 落在持久层，非游标）
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_reply WHERE message_id=?", (mid,))[0] == 1


def test_multiple_queries_in_batch(env):
    _spool(env["inbox"], _rec(1, "现在进展如何"), _rec(2, "结果是多少"))
    assert env["ingest"].ingest() == 2
    n = env["daemon"].query_one("SELECT COUNT(*) FROM interaction_reply")[0]
    assert n == 2                                      # 每条 query 各一 reply


# ---------------- ⑧ cyc 绑定：directive.created_cycle 取自当前轮 ----------------
def test_cyc_binding_sets_created_cycle(env):
    cyc = type("Cyc", (), {"cycle_id": "c1"})()        # 假 cycle（seed_minimal 有 cycle 1）
    _spool(env["inbox"], _rec(1, "暂停一下"))
    env["ingest"].ingest(cyc)
    cc = env["daemon"].query_one("SELECT created_cycle FROM directive WHERE kind='pause'")[0]
    assert cc == 1                                     # cnum('c1')=1


# ---------------- ⑨ 可重试 vs 毒消息：瞬时故障不推进游标、不丢消息 ----------------
def test_retryable_failure_does_not_advance_cursor(env, monkeypatch):
    import sqlite3
    real = env["console"].handle_inbound
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")   # 瞬时故障
        return real(**kw)

    monkeypatch.setattr(env["console"], "handle_inbound", flaky)
    _spool(env["inbox"], _rec(1, "暂停一下"))
    assert env["ingest"].ingest() == 0                 # 停批、不推进（不丢消息）
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()
    assert env["ingest"].ingest() == 1                 # 下轮重试成功
    assert env["daemon"].query_one("SELECT id FROM directive WHERE kind='pause'") is not None


# ---------------- ⑩ 顶层兜底：写游标/读 spool 的 I/O 故障不崩推进 ----------------
def test_io_error_top_level_guard(env, monkeypatch):
    def boom(_batch, _offset):
        raise OSError("磁盘满")
    monkeypatch.setattr(env["ingest"], "_set_cursor", boom)
    _spool(env["inbox"], _rec(1, "暂停一下"))
    assert env["ingest"].ingest() == 0                 # 顶层兜底吞掉 I/O 故障，不抛（推进主循环不受影响）


# ---------------- ⑪ query 应答瞬时故障（卡片未发布）→ 重试不丢，卡片就绪后应答（外审 BLOCKER 修复钉）----------------
def test_query_before_card_retries_then_answers(env):
    env["card_path"].unlink()                          # 删卡 → handle_query 抛 FileNotFoundError（瞬时：卡尚未发布）
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    assert env["ingest"].ingest() == 0                 # 瞬时故障 → 停批、不推进（不漏答）
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()
    mid = env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert env["daemon"].query_one("SELECT id FROM interaction_reply WHERE message_id=?", (mid,)) is None
    env["pub"].publish("c1")                           # 卡片发布后
    assert env["ingest"].ingest() == 1                 # 补答（恰一次）
    assert env["daemon"].query_one("SELECT COUNT(*) FROM interaction_reply WHERE message_id=?", (mid,))[0] == 1


# ---------------- ⑫ query 应答持久故障 → 超限写终态失败回执（不漏答、不双答、不饿死）----------------
def test_query_persistent_failure_writes_terminal_fallback(env, monkeypatch):
    def always_fail(**kw):
        raise RuntimeError("模拟应答器持久故障")
    monkeypatch.setattr(env["mediator"], "handle_query", always_fail)
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        assert env["ingest"].ingest() == 0             # 前几拍：重试、不推进
        assert not (env["work"] / "state" / "console_inbox.cursor").exists()
    env["ingest"].ingest()                             # 第 MAX 拍：超限 → 写终态回执 + 推进
    mid = env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    reps = env["daemon"].query("SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))
    assert len(reps) == 1 and "应答暂不可用" in reps[0][0]   # 恰一条终态失败回执（不漏、不双）
    assert _cursor_at_inbox_end(env)                            # 已推进（不饿死后续）


# ---------------- ⑬ 无序号坏尾行也被行数游标消费（不每拍重扫；外审 SHOULD）----------------
def test_seqless_bad_tail_line_consumed(env):
    inbox = env["inbox"]; inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps(_rec(1, "暂停一下")) + "\n" + "{坏 json 尾行}\n", encoding="utf-8")
    assert env["ingest"].ingest() == 1                 # 好行处理（ok=1），坏尾行跳过
    assert _cursor_at_inbox_end(env)                            # byte cursor 消费坏尾行
    assert env["ingest"].ingest() == 0                 # 再拍：坏尾行不重扫、不重复告警


# ---------------- ⑭ 合法 JSON 但非对象（"x" / [] / 3）也跳过并推进（外审 R2 SHOULD）----------------
def test_non_dict_json_line_skipped(env):
    inbox = env["inbox"]; inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text('"justastring"\n' + json.dumps(_rec(2, "暂停一下")) + "\n", encoding="utf-8")
    assert env["ingest"].ingest() == 1                 # 非对象行 poison 跳过、好行处理（不因 rec.get 抛而卡队列）
    assert _cursor_at_inbox_end(env)


# ---------------- ⑮ no-loss 闭合：终态事务失败时**绝不推进**游标 ----------------
def test_terminal_receipt_transaction_failure_does_not_advance(env, monkeypatch):
    """直接让 unclear+reply 终态事务失败；不能用只 mock 某个 ACK 路径的假阳性测试替代。"""
    classifier = env["console"].classifier
    classify_calls = {"n": 0}

    def classify_boom(_message):
        classify_calls["n"] += 1
        raise RuntimeError("分类器持久故障")

    monkeypatch.setattr(classifier, "classify", classify_boom)
    _spool(env["inbox"], _rec(1, "普通消息的终态回执测试"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS - 1):
        assert env["ingest"].ingest() == 0

    daemon = env["daemon"]
    real_transaction = daemon.transaction
    transaction_calls = {"n": 0}

    @contextmanager
    def fail_terminal_transaction():
        transaction_calls["n"] += 1
        # 第五拍：handle_inbound.inbound（1）、terminalize.inbound（2）、
        # unclear+reply 终态事务（3）。只让最后一个失败。
        if transaction_calls["n"] == 3:
            raise sqlite3.OperationalError("终态回执事务无法提交")
        with real_transaction() as conn:
            yield conn

    with monkeypatch.context() as receipt_patch:
        receipt_patch.setattr(daemon, "transaction", fail_terminal_transaction)
        assert env["ingest"].ingest() == 0

    mid = daemon.query_one(
        "SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert classify_calls["n"] == env["ingest"]._MAX_ATTEMPTS
    assert daemon.query_one(
        "SELECT 1 FROM interaction_classification WHERE message_id=?", (mid,)) is None
    assert daemon.query_one("SELECT 1 FROM interaction_reply WHERE message_id=?", (mid,)) is None
    assert env["ingest"]._attempts["console-1"] == env["ingest"]._MAX_ATTEMPTS
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()

    # 事务恢复后不再重跑分类器；补齐同一 durable 终态，再推进游标。
    assert env["ingest"].ingest() == 0
    assert classify_calls["n"] == env["ingest"]._MAX_ATTEMPTS
    assert daemon.query_one(
        "SELECT intent FROM interaction_classification WHERE message_id=?", (mid,)) == ("unclear",)
    assert daemon.query_one(
        "SELECT reply_text FROM interaction_reply WHERE message_id=?", (mid,))[0].startswith(
            "[console-inbound-failed]")
    assert json.loads(env["ingest"].retry_path.read_text()) == {}
    assert _cursor_at_inbox_end(env)
