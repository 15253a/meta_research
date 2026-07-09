"""CP9.3 · 人类控制台入站闭环（ConsoleInboxIngest + precheck 装配）。

验收：控制台命令经 console_server 落 console_inbox.jsonl → run 进程 precheck 边界 ingest 进权威入站链：
①directive 意图 → 落 pending directive（幂等）；②query 意图 → mediator 应答写 interaction_reply；
③游标持久化 + 幂等重放不重复；④torn-tail/坏行容错；⑤入站失败不崩推进主循环（辅助面健壮性）；
⑥precheck 装配端到端：pause→确认→precheck 阻断，resume→确认→解阻断。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.console import Console
from orchestrator.console_ingest import ConsoleInboxIngest
from orchestrator.mediator import Mediator, open_responder_read_conn
from orchestrator.notify import make_advancer_precheck
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
    ingest = ConsoleInboxIngest(console, mediator, str(work))
    inbox = work / "state" / "console_inbox.jsonl"
    return {"daemon": daemon, "console": console, "mediator": mediator, "ingest": ingest,
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
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "2"
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


# ---------------- ⑤ 入站失败不崩推进主循环 ----------------
def test_handle_inbound_failure_does_not_crash(env, monkeypatch):
    def boom(**kw):
        raise RuntimeError("模拟入站崩")                # 非 OperationalError = 毒消息（非瞬时）
    monkeypatch.setattr(env["console"], "handle_inbound", boom)
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "现在进展如何"))
    assert env["ingest"].ingest() == 0                 # 毒消息不计入 processed（只计 ok）；不抛
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "2"  # 游标照进（毒消息不永久阻塞）


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
    # 回显确认（provenance = 另一条入站消息）→ 下一拍 precheck 消费 pause → 阻断
    cmid = console.ingest.inbound(connector="console", raw_text="确认暂停", idempotency_key="confirm-1")
    console.confirm_directive(directive_id=did, confirm_message_id=cmid)
    assert precheck() == "pause 指令生效中（等待 resume）"
    # resume 入站 + 确认 → 解阻断
    _spool(env["inbox"], _rec(1, "暂停一下"), _rec(2, "继续跑"))
    precheck()                                         # ingest resume（生成 pending 硬指令）
    rid = daemon.query_one("SELECT id FROM directive WHERE kind='resume'")[0]
    rmid = console.ingest.inbound(connector="console", raw_text="确认继续", idempotency_key="confirm-2")
    console.confirm_directive(directive_id=rid, confirm_message_id=rmid)
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
    def boom(_seq):
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
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "1"   # 已推进（不饿死后续）


# ---------------- ⑬ 无序号坏尾行也被行数游标消费（不每拍重扫；外审 SHOULD）----------------
def test_seqless_bad_tail_line_consumed(env):
    inbox = env["inbox"]; inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps(_rec(1, "暂停一下")) + "\n" + "{坏 json 尾行}\n", encoding="utf-8")
    assert env["ingest"].ingest() == 1                 # 好行处理（ok=1），坏尾行跳过
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "2"   # 行数游标消费坏尾行
    assert env["ingest"].ingest() == 0                 # 再拍：坏尾行不重扫、不重复告警


# ---------------- ⑭ 合法 JSON 但非对象（"x" / [] / 3）也跳过并推进（外审 R2 SHOULD）----------------
def test_non_dict_json_line_skipped(env):
    inbox = env["inbox"]; inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text('"justastring"\n' + json.dumps(_rec(2, "暂停一下")) + "\n", encoding="utf-8")
    assert env["ingest"].ingest() == 1                 # 非对象行 poison 跳过、好行处理（不因 rec.get 抛而卡队列）
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "2"


# ---------------- ⑮ no-loss 闭合：终态回执也写不进时**绝不推进**游标（外审 R2 BLOCKER）----------------
def test_terminal_ack_failure_does_not_advance(env, monkeypatch):
    import sqlite3
    orig_ack = env["console"].ingest.ack

    def q_boom(**kw):
        raise RuntimeError("应答器持久故障")

    def ack_boom(**kw):
        raise sqlite3.OperationalError("终态回执也写不进")

    monkeypatch.setattr(env["mediator"], "handle_query", q_boom)
    monkeypatch.setattr(env["console"].ingest, "ack", ack_boom)
    _spool(env["inbox"], _rec(1, "现在进展如何"))
    for _ in range(env["ingest"]._MAX_ATTEMPTS + 2):
        assert env["ingest"].ingest() == 0             # 无 durable reply → 每拍都不推进
    mid = env["daemon"].query_one("SELECT id FROM interaction_message WHERE idempotency_key='console-1'")[0]
    assert env["daemon"].query_one("SELECT id FROM interaction_reply WHERE message_id=?", (mid,)) is None
    assert not (env["work"] / "state" / "console_inbox.cursor").exists()   # no-loss：无回执绝不越过该行
    monkeypatch.setattr(env["console"].ingest, "ack", orig_ack)           # 恢复 → 补写终态回执并推进
    env["ingest"].ingest()
    assert env["daemon"].query_one("SELECT id FROM interaction_reply WHERE message_id=?", (mid,)) is not None
    assert (env["work"] / "state" / "console_inbox.cursor").read_text().strip() == "1"
