"""CP6.2 · query 只读应答链 + 中介 + status_card 发布（§4.6.2/4.6.5/4.6.6；M5）。

核心验收面（§7.1 M5）：**query 只读边界负例**（responder 连接写 decision/route/question/cycle/
metric_result 全被拒）；grounding 校验不过退模板；中介重建前后回答一致；ACK/query p95<2s；
发布快照原子（读不到半完成态）。文件库（mode=ro 连接须同库文件，同 gate 约定）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.interaction import InteractionIngest
from orchestrator.mediator import (Mediator, TemplateResponder, grounding_check,
                                   open_responder_read_conn, render_fallback)
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path):
    """文件库 + 已发布卡：daemon（写）/ ro conn（应答器）/ mediator。"""
    dbp = str(tmp_path / "m.sqlite")
    daemon = WriteDaemon(db.connect(dbp))
    conftest.seed_minimal(daemon.conn)
    daemon.conn.execute("UPDATE cycle SET active_question_id=1, route='attack' WHERE id=1")
    daemon.conn.commit()
    card_path = tmp_path / "state" / "status_card.json"
    pub = SC.SqliteStatusPublisher(open_responder_read_conn(dbp), policy=POLICY,
                                   goal_body_md="目标首行\n次行", out_path=str(card_path))
    pub.publish("c1")
    return {"dbp": dbp, "daemon": daemon, "pub": pub, "card_path": card_path,
            "med": Mediator(daemon, str(card_path))}


# ============ 只读边界负例（§7.1 M5：responder 写库全被拒）============
def test_responder_conn_write_denied_matrix(env):
    ro = open_responder_read_conn(env["dbp"])
    writes = [
        ("INSERT INTO decision(actor,type,payload_json) VALUES ('human','x','{}')", ()),
        ("UPDATE cycle SET route='bootstrap' WHERE id=1", ()),
        ("INSERT INTO question(goal_id,goal_ver,born_goal_ver,text,status,source) "
         "VALUES (1,1,1,'x','open','agent')", ()),
        ("UPDATE cycle SET status='aborted' WHERE id=1", ()),
        ("UPDATE metric_result SET value=999 WHERE id=1", ()),
        ("DELETE FROM interaction_message", ()),
        ("INSERT INTO directive(kind,hardness,status,consume_at,payload_json) "
         "VALUES ('note','soft','pending','immediate','{}')", ()),
    ]
    for sql, params in writes:
        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro.execute(sql, params)
    # 读照常
    assert ro.execute("SELECT count(*) FROM cycle").fetchone()[0] >= 1


def test_responder_conn_temp_schema_also_denied(env):
    """外审 BLOCKER 回归：mode=ro 只锁主库，**temp schema 仍可写**——CREATE TEMP TABLE / CREATE
    VIRTUAL TABLE（可落 temp）必须靠 authorizer 拒。VTABLE 用不存在的模块名：authorizer 在 prepare 期
    先于模块解析拒（错误必须是 not authorized，非 no such module——证明拒在 authorizer 而非环境缺模块）。"""
    ro = open_responder_read_conn(env["dbp"])
    for sql in ("CREATE TEMP TABLE leak(x)",
                "CREATE VIRTUAL TABLE temp.v USING nomodule_xyz(a)",
                "CREATE TEMP VIEW lv AS SELECT 1"):
        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError), match="authorized"):
            ro.execute(sql)


def test_responder_conn_is_physically_ro(env):
    """mode=ro 物理只读是 authorizer 之下的第二道锁：无 authorizer 的裸 mode=ro 连接也写不进。"""
    from urllib.parse import quote
    bare = sqlite3.connect(f"file:{quote(env['dbp'])}?mode=ro", uri=True)   # 不装 authorizer
    with pytest.raises(sqlite3.OperationalError):
        bare.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('human','x','{}')")


# ============ grounding 校验 ============
def test_grounding_rejects_state_change_claims():
    card = {"snapshot_cycle": "c1", "counts": {"open": 1}}
    assert grounding_check("好的，已暂停系统。", card) is not None
    assert grounding_check("I have paused the run.", card) is not None
    assert grounding_check("日志显示 loss 在降。", card) is not None            # 引日志作证
    assert grounding_check("已创建指令并将执行。", card) is not None            # 自产 directive
    assert grounding_check("当前快照 c1，open 1。", card) is None               # 卡内事实通过


def test_grounding_rejects_offcard_entities():
    card = {"snapshot_cycle": "c1", "active_question": {"id": "q2"}}
    assert grounding_check("q2 正在推进。", card) is None
    assert grounding_check("q99 已经答完。", card) is not None                  # 卡外实体
    assert grounding_check("轮 c7 的结果不错。", card) is not None


def test_grounding_cjk_adjacent_entities_no_space():
    """内审 BLOCKER 回归：CJK 是 \\w、无空格分词——"轮c7" 用 \\b 检不到（教训同 console._hit）。
    环视实现下 CJK 紧邻照样命中；纯拉丁长 token 不误切；实体对实体比对防 "c1"⊂"c12" 子串假过。"""
    card = {"snapshot_cycle": "c12", "active_question": {"id": "q2"}}
    assert grounding_check("轮c7的结果不错。", card) is not None               # 无空格卡外实体：必须拒
    assert grounding_check("使用q99模型跑一遍", card) is not None
    assert grounding_check("q2已在推进", card) is None                          # 无空格卡内实体：通过
    assert grounding_check("变量 abc7def 无关", card) is None                   # 拉丁长 token 不误切
    assert grounding_check("轮c1如何", card) is not None                        # c1 非卡内实体（卡只有 c12）
    assert grounding_check("Q99 的进展如何", card) is not None                  # 大写写法不绕过（外审 NIT）
    assert grounding_check("Q2 呢", card) is None                               # 大写卡内实体照常通过


def test_handle_query_grounded_and_fallback(env):
    med = env["med"]
    ing = InteractionIngest(env["daemon"])
    m1 = ing.inbound(connector="qq", raw_text="现在进展如何？", idempotency_key="q-1", goal_id=1, goal_ver=1)
    r = med.handle_query(message_id=m1)          # 查询文本按 message_id 从持久层取（审计链）
    assert r["grounded"] is True and r["snapshot_cycle"] == "c1"
    assert "c1" in r["reply_text"]                                              # 模板应答含卡内快照
    # 幻觉应答器 → grounding 拒 → 模板回退
    class Hallucinator:
        kind = "template"
        def answer(self, q, card):
            return "好的，已暂停系统，q99 也剪掉了。"
    med2 = Mediator(env["daemon"], str(env["card_path"]), responder=Hallucinator())
    m2 = ing.inbound(connector="qq", raw_text="状态？", idempotency_key="q-2", goal_id=1, goal_ver=1)
    r2 = med2.handle_query(message_id=m2)
    assert r2["grounded"] is False and r2["fallback_reason"]
    assert r2["reply_text"] == render_fallback(json.loads(env["card_path"].read_text(encoding="utf-8")))
    # 回复全部 append-only 入库、不写 decision（P1）
    d = env["daemon"]
    assert d.query_one("SELECT count(*) FROM interaction_reply")[0] == 2
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='human'")[0] == 0


def test_handle_query_requires_declared_kind_and_known_message(env):
    """外审 SHOULD 回归：缺 kind 的应答器 fail loud（不静默错标 template 入库）；message 不存在干净拒。"""
    ing = InteractionIngest(env["daemon"])
    mid = ing.inbound(connector="qq", raw_text="状态？", idempotency_key="q-k", goal_id=1, goal_ver=1)
    class NoKind:
        def answer(self, q, card):
            return "x"
    with pytest.raises(NotImplementedError, match="kind"):
        Mediator(env["daemon"], str(env["card_path"]), responder=NoKind()).handle_query(message_id=mid)
    with pytest.raises(ValueError, match="不存在"):
        env["med"].handle_query(message_id=99999)


def test_query_reads_published_snapshot_not_live_db(env):
    """应答器读**发布快照**：DB 已变、未再发布 → 回答仍基于旧卡（非半完成态）。"""
    med, d = env["med"], env["daemon"]
    d.conn.execute("UPDATE cycle SET status='bundle' WHERE id=1")
    d.conn.commit()                                # DB 变了，但没到阶段边界（未 publish）
    ing = InteractionIngest(d)
    mid = ing.inbound(connector="qq", raw_text="轮状态？", idempotency_key="q-s", goal_id=1, goal_ver=1)
    r = med.handle_query(message_id=mid)
    assert "reasoning" in r["reply_text"] and "bundle" not in r["reply_text"]   # 旧快照
    env["pub"].publish("c1")                       # 阶段边界发布后新快照可见
    mid2 = ing.inbound(connector="qq", raw_text="轮状态？？", idempotency_key="q-s2", goal_id=1, goal_ver=1)
    r2 = med.handle_query(message_id=mid2)
    assert "bundle" in r2["reply_text"]


# ============ 发布原子性 ============
def test_publish_atomic_no_partial(env):
    """发布 = tmp→rename：latest 文件任意时刻是完整 JSON（此处验证 tmp 不残留 + 覆盖后仍完整可解）。"""
    p = env["pub"].publish("c1")
    assert not Path(p + ".tmp").exists()
    card = json.loads(Path(p).read_text(encoding="utf-8"))
    assert card["snapshot_cycle"] == "c1"
    env["pub"].publish("c1")                       # 幂等重发布：原子覆盖
    assert json.loads(Path(p).read_text(encoding="utf-8"))["snapshot_cycle"] == "c1"


def test_advancer_publishes_at_stage_boundary(tmp_path):
    """advancer 接线：run_cycles 每格 advance 后发布 → bootstrap 一轮跑完，latest 卡存在且 snapshot=该轮。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    dbp = str(tmp_path / "a.sqlite")
    daemon = WriteDaemon(db.connect(dbp))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(dbp), POLICY, goal_body_md="g")
    card_path = tmp_path / "sc.json"
    pub = SC.SqliteStatusPublisher(open_responder_read_conn(dbp), policy=POLICY,
                                   goal_body_md="g", out_path=str(card_path))

    def provider(cyc, pack):    # 创世轮产物：建根 + 终止（最短可跑）
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "r"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}

    adv = SqliteAdvancer(state, compiler, provider, status_publisher=pub)
    ids = adv.run_cycles(3)
    assert ids and card_path.exists()
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["snapshot_cycle"] == ids[-1]
    assert card["cycle_status"] == "done"          # 最后一格发布在 mark_done 之后


# ============ 中介重建一致（§7.1 M5）============
def test_mediator_rebuild_consistent_answers(env):
    med, d = env["med"], env["daemon"]
    ing = InteractionIngest(d)
    mid = ing.inbound(connector="qq", raw_text="进展？", idempotency_key="rb-1", goal_id=1, goal_ver=1)
    a1 = med.handle_query(message_id=mid)["reply_text"]
    med2 = Mediator(d, str(env["card_path"]))      # 杀掉重建：新实例只靠持久层
    ctx = med2.rebuild("qq")
    assert ctx["card"]["snapshot_cycle"] == "c1"
    assert any(h["message_id"] == mid and h["reply"] == a1 for h in ctx["history"])   # 历史含既有问答
    mid2 = ing.inbound(connector="qq", raw_text="进展？", idempotency_key="rb-2", goal_id=1, goal_ver=1)
    a2 = med2.handle_query(message_id=mid2)["reply_text"]
    assert a2 == a1                                                             # 重建前后回答逐字节一致


def test_rebuild_multi_reply_takes_latest_no_fanout(env):
    """外审 SHOULD 回归：一条 message 多回复（ACK+应答）时——reply 取**最新**一条、窗口 N 按 message
    数不被 join 扇出截短。"""
    med, d = env["med"], env["daemon"]
    ing = InteractionIngest(d)
    mid = ing.inbound(connector="qq", raw_text="双回复", idempotency_key="fan-1", goal_id=1, goal_ver=1)
    ing.ack(message_id=mid, reply_text="收到（ACK）")
    r = med.handle_query(message_id=mid)                        # 第二条回复（应答）
    mids = [ing.inbound(connector="qq", raw_text=f"填充{i}", idempotency_key=f"fan-f{i}",
                        goal_id=1, goal_ver=1) for i in range(3)]
    ctx = Mediator(d, str(env["card_path"]), rebuild_last_n=4).rebuild("qq")
    assert [h["message_id"] for h in ctx["history"]] == [mid] + mids            # 4 条 message，无扇出重复
    assert ctx["history"][0]["reply"] == r["reply_text"]                        # 取最新回复（非 ACK）


def test_rebuild_last_n_window(env):
    med, d = env["med"], env["daemon"]
    ing = InteractionIngest(d)
    for i in range(25):
        ing.inbound(connector="qq", raw_text=f"消息{i}", idempotency_key=f"w-{i}", goal_id=1, goal_ver=1)
    ctx = Mediator(d, str(env["card_path"]), rebuild_last_n=20).rebuild("qq")
    assert len(ctx["history"]) == 20
    assert ctx["history"][-1]["text"] == "消息24"                               # 最近 N 条、时序升序


# ============ SLA（§7.1 M5：ACK/query p95<2s）============
def test_ack_and_query_p95_under_2s(env):
    med, d = env["med"], env["daemon"]
    ing = InteractionIngest(d)
    lat_ack, lat_q = [], []
    for i in range(20):
        mid = ing.inbound(connector="qq", raw_text=f"进展 {i}？", idempotency_key=f"sla-{i}", goal_id=1, goal_ver=1)
        t0 = time.monotonic()
        ing.ack(message_id=mid, reply_text="收到，处理中")
        lat_ack.append(time.monotonic() - t0)
        t1 = time.monotonic()
        med.handle_query(message_id=mid)
        lat_q.append(time.monotonic() - t1)
    p95 = lambda xs: sorted(xs)[int(len(xs) * 0.95) - 1]
    assert p95(lat_ack) < POLICY["interaction"]["ack_sla_s"]
    assert p95(lat_q) < POLICY["interaction"]["query_sla_s"]
