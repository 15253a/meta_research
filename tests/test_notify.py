"""CP6.3 · 通知矩阵 outbox + 文件请求全流水 + 全局等待（§4.6.6/§4.6.8/§4.4.1；M5 收尾）。

核心验收面（§7.1 M5）：directive 每状态迁移均外显（**逐态推送断言**）；outbox 幂等投递；文件请求
全流水（创建拒绝三负例 + schema 拒 + uploads hash 入账并入 input/user_provided/ + 恢复推进）；
**全局等待**（pending 请求 → Advancer 不发新研究 runner call **而 query/通知照常**）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import status_card as SC
from orchestrator.console import Console
from orchestrator.interaction import InteractionIngest
from orchestrator.mediator import Mediator, open_responder_read_conn
from orchestrator.notify import (DirectiveNotifier, FileRequestNotifier, FileRequestReject,
                                 FileRequestService, Outbox, make_advancer_precheck)
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")


class FakeConnector:
    """收集式 connector（M5 测试）：send 记录事件；可注入故障。"""
    def __init__(self):
        self.sent = []
        self.fail_after = None
    def send(self, payload):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("connector 故障注入")
        self.sent.append(payload)
        return {"ok": True}


@pytest.fixture()
def env(tmp_path):
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    outbox = Outbox(str(tmp_path / "outbox"))
    return {"d": daemon, "c": Console(daemon), "ob": outbox,
            "dn": DirectiveNotifier(daemon, outbox), "tmp": tmp_path}


def _items(n=1):
    return [{"kind": "dataset", "desc": f"数据集{i}", "expected_files": ["data.bin"],
             "attempted_paths": ["/data/公共区已找过"], "failure_reason": "镜像不含该集",
             "dest_hint": "input/"} for i in range(n)]


def _request(n=1):
    return {"summary_md": "需要外部数据集：镜像不含、无法自行获取", "items": _items(n)}


# ============ outbox 幂等 ============
def test_outbox_idempotent_emit_and_deliver(env):
    ob = env["ob"]
    assert ob.emit("k1", "x", {"a": 1}) is True
    assert ob.emit("k1", "x", {"a": 1}) is False                  # 重 emit 不重排队
    ob.emit("k2", "x", {})
    conn = FakeConnector()
    assert ob.deliver_pending(conn) == ["k1", "k2"]
    assert ob.deliver_pending(conn) == []                          # 重投递不重发
    assert [e["event_key"] for e in conn.sent] == ["k1", "k2"]


def test_outbox_delivery_resumes_after_connector_failure(env):
    ob = env["ob"]
    for k in ("a", "b", "c"):
        ob.emit(k, "x", {})
    bad = FakeConnector(); bad.fail_after = 1                      # 发出 a 后故障
    with pytest.raises(RuntimeError):
        ob.deliver_pending(bad)
    good = FakeConnector()
    assert ob.deliver_pending(good) == ["b", "c"]                  # a 已标记不重发；b/c 续投


def test_outbox_torn_tail_tolerated_and_repaired(env, tmp_path):
    """内审 SHOULD 回归：append 崩溃撕裂尾行 → 不楔死（半行=未入队，重扫会补）；下次 emit 修复
    （截半行再追加，不粘行）；中段坏行仍 fail loud（非崩溃可造成，别的故障别吞）。"""
    from orchestrator.notify import Outbox
    ob = Outbox(str(tmp_path / "torn"))
    ob.emit("k1", "x", {})
    with ob.queue_path.open("a", encoding="utf-8") as f:
        f.write('{"event_key": "k2", "kind": "x", "pay')           # 模拟 kill-9 半行
    ob2 = Outbox(str(tmp_path / "torn"))                           # 新实例（缓存冷）
    assert ob2.emit("k3", "x", {}) is True                         # 不楔死；修复后追加
    assert ob2.deliver_pending(FakeConnector()) == ["k1", "k3"]    # k2 半行被丢（未 emit 语义）
    assert ob2.emit("k2", "x", {}) is True                         # 重扫补发路径畅通
    # 中段坏行 fail loud
    bad = Outbox(str(tmp_path / "mid"))
    bad.queue_path.write_text('not-json\n{"event_key": "z", "kind": "x", "payload": {}}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        Outbox(str(tmp_path / "mid"))._queued_keys()


def test_outbox_unterminated_complete_json_not_lost(tmp_path):
    """外审 BLOCKER 回归：崩溃可留下"完整 JSON 但无尾换行"——committed 判据必须是换行终止：
    该段不入 _seen、emit 截修丢弃后**可重新入队**（旧实现会把它算入 _seen 又截掉 → 事件永久丢失）。"""
    from orchestrator.notify import Outbox
    ob = Outbox(str(tmp_path / "unterm"))
    ob.emit("k1", "x", {})
    with ob.queue_path.open("a", encoding="utf-8") as f:
        f.write('{"event_key": "k2", "kind": "x", "payload": {}}')   # 完整 JSON、无 \n
    ob2 = Outbox(str(tmp_path / "unterm"))
    assert ob2.emit("k3", "x", {}) is True                           # 触发截修
    assert ob2.emit("k2", "x", {}) is True                           # k2 未 committed → 可重入队（不丢）
    assert ob2.deliver_pending(FakeConnector()) == ["k1", "k3", "k2"]


def test_resolve_symlinked_item_dir_not_ingested(env, tmp_path):
    """外审 BLOCKER 回归：item 目录**本身**是 symlink（指向外部目录）→ 其内常规文件不得并入
    （逐文件 is_symlink 挡不住这条路）。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "secret.txt").write_bytes(b"SECRET")
    up = tmp_path / "up" / str(rid); up.mkdir(parents=True)
    (up / "1").symlink_to(outside, target_is_directory=True)         # item 目录=symlink
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="传了", idempotency_key="sym-1",
                                       goal_id=1, goal_ver=1)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert out["resolution"][0] == {"unavailable": "用户未提供该条目文件"}   # 不跟随、不并入
    assert not (tmp_path / "input" / "user_provided" / str(rid)).exists()


def test_create_checked_idempotent_retry_wins_over_quota(env, tmp_path):
    """外审 SHOULD 回归：幂等先于 quota——同 (goal,request_hash) 重试在配额已满时仍返回既有单。"""
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    pmid = InteractionIngest(d).inbound(connector="qq", raw_text="铺底", idempotency_key="quota-seed",
                                        goal_id=1, goal_ver=1)
    with d.transaction() as conn:   # 铺满配额（连同已存在的 pending 共 ≥5）
        for i in range(5):
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
                         "(1,1,'plan','resolved','s','[]',?,'[]',CURRENT_TIMESTAMP,?)", (f"q{i}", pmid))
    assert svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1)) == rid  # 幂等放行
    two = _request(2)
    with pytest.raises(FileRequestReject, match="上限"):              # 新单仍被 quota 拒
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=two)


def test_second_pending_different_hash_business_reject(env, tmp_path):
    """外审 NIT 回归：同 goal 第二张不同 hash 的 pending → 业务拒因（不外泄 uq_ireq_one_pending DDL 错）。"""
    svc = FileRequestService(env["d"], SCHEMAS, POLICY, str(tmp_path / "input"))
    svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    with pytest.raises(FileRequestReject, match="pending"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(2))


def test_resolve_provenance_goal_mismatch_rejected(env, tmp_path):
    """外审 SHOULD 回归：终态 provenance 消息须与请求同 goal（未绑定 goal 也拒，fail closed）。"""
    d = env["d"]
    with d.transaction() as conn:
        conn.execute("INSERT INTO goal(id,version,text,predicate_json) VALUES (2,1,'g2','{}')")
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    ing = InteractionIngest(d)
    other = ing.inbound(connector="qq", raw_text="别的 goal 的消息", idempotency_key="pv-1",
                        goal_id=2, goal_ver=1)
    unbound = ing.inbound(connector="qq", raw_text="未绑定 goal", idempotency_key="pv-2")
    up = tmp_path / "pv"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    with pytest.raises(ValueError, match="goal"):
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=other)
    with pytest.raises(ValueError, match="goal"):
        svc.cancel(request_id=rid, reason="r", resolved_message_id=unbound)
    assert env["d"].query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"


def test_due_timings_matrix():
    """内审 SHOULD 回归：reasoning_start 只在下一格将进 reasoning 时到期——attack 轮以 cycle.status
    为"最后已提交阶段"游标（attack_stages.advance_stage）：created/idea/plan 均不消费，'bundle' 才
    （下一格=reasoning）；reasoning-only 轮每格即 reasoning；开轮前（None）只 immediate+stage_boundary。"""
    from types import SimpleNamespace as NS
    from orchestrator.notify import _due_timings
    assert _due_timings(None) == ["immediate", "stage_boundary"]
    for st in ("created", "idea", "plan", "reasoning"):
        assert "reasoning_start" not in _due_timings(NS(route="attack", status=st)), st
    assert "reasoning_start" in _due_timings(NS(route="attack", status="bundle"))
    for route in ("bootstrap", "decompose"):
        assert "reasoning_start" in _due_timings(NS(route=route, status="created"))


# ============ directive 逐态推送（§7.1 M5 矩阵断言）============
def _keys(ob):
    return set(ob._queued_keys())


def test_directive_hard_full_lifecycle_events(env):
    d, c, ob, dn = env["d"], env["c"], env["ob"], env["dn"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="n-1", goal_id=1, goal_ver=1)
    did = r["directive_id"]
    dn.scan()
    assert _keys(ob) == {f"directive:{did}:received", f"directive:{did}:classified",
                         f"directive:{did}:pending_confirmation"}          # 硬未确认：三态，无 pending_effect
    ev = [json.loads(l) for l in (ob.queue_path.read_text().splitlines())]
    pc = next(e for e in ev if e["kind"] == "directive_pending_confirmation")
    assert pc["payload"]["polished"].startswith("[pause]")                  # 确认事件展示润色稿
    c.confirm_directive(directive_id=did, confirm_message_id=r["message_id"])
    dn.scan()
    assert f"directive:{did}:pending_effect" in _keys(ob)                   # 确认后 → 就绪态
    c.consume_directive(directive_id=did, cycle_id="c1")
    new = dn.scan()
    assert f"directive:{did}:applied" in new
    applied = [json.loads(l) for l in ob.queue_path.read_text().splitlines()
               if json.loads(l)["kind"] == "directive_applied"][0]
    assert applied["payload"]["consumed_cycle"] == "c1"                     # applied 带消费轮+效果摘要
    assert applied["payload"]["effect"]["kind"] == "pause"
    dn.scan()
    assert dn.scan() == []                                                  # 重扫幂等：无新事件


def test_directive_rejected_and_superseded_events(env):
    d, c, ob, dn = env["d"], env["c"], env["ob"], env["dn"]
    # 软指令系统不从 → rejected 附理由（理由在 decline 决策）
    r1 = c.handle_inbound(connector="qq", raw_text="注入问题：试试量子计算", idempotency_key="n-r",
                          goal_id=1, goal_ver=1)
    c.reject_directive(directive_id=r1["directive_id"], reason="与目标谓词无关", by_decision=True, cycle_id="c1")
    # pause 被 resume 覆盖 → superseded
    r2 = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="n-p", goal_id=1, goal_ver=1)
    r3 = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="n-c", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r3["directive_id"], confirm_message_id=r3["message_id"])
    c.consume_directive(directive_id=r3["directive_id"], cycle_id="c1")
    dn.scan()
    ks = _keys(ob)
    assert f"directive:{r1['directive_id']}:rejected" in ks
    rej = [json.loads(l) for l in ob.queue_path.read_text().splitlines()
           if json.loads(l)["event_key"] == f"directive:{r1['directive_id']}:rejected"][0]
    assert rej["payload"]["reason"] == "与目标谓词无关"                     # rejected 附理由
    assert f"directive:{r2['directive_id']}:superseded" in ks


# ============ 文件请求：创建负例 + schema 拒 ============
def test_create_checked_schema_rejects_missing_attempted_paths(env, tmp_path):
    svc = FileRequestService(env["d"], SCHEMAS, POLICY, str(tmp_path / "input"))
    bad = _request(1)
    del bad["items"][0]["attempted_paths"]
    with pytest.raises(FileRequestReject, match="schema"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=bad)


def test_create_checked_policy_negatives(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    with pytest.raises(FileRequestReject, match="超上限"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(11))   # >max_items=10
    off = json.loads(json.dumps(POLICY)); off["interaction_request"]["enabled"] = False
    with pytest.raises(FileRequestReject, match="未启用"):
        FileRequestService(d, SCHEMAS, off, str(tmp_path / "i2")).create_checked(
            goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    # 同 goal (pending+resolved) 5 条后第 6 条拒（DDL 一 goal 一 pending → 5 条 resolved 铺底；
    # 终态 CHECK 要求 resolved_message_id 非空 → 先落一条入站消息作 provenance）
    pmid = InteractionIngest(d).inbound(connector="qq", raw_text="铺底", idempotency_key="neg-seed",
                                        goal_id=1, goal_ver=1)
    with d.transaction() as conn:
        for i in range(5):
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash,resolution_json,resolved_at,resolved_message_id) VALUES "
                         "(1,1,'plan','resolved','s','[]',?,'[]',CURRENT_TIMESTAMP,?)", (f"h{i}", pmid))
    with pytest.raises(FileRequestReject, match="上限"):
        svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))


# ============ 文件请求：resolve 全流水（uploads→hash→并入→终态）============
def test_resolve_pipeline_hash_and_ingest(env, tmp_path):
    import hashlib
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(2))
    up = tmp_path / "uploads" / str(rid)
    (up / "1").mkdir(parents=True); (up / "1" / "data.bin").write_bytes(b"DATA-1")
    # 条目 2 用户未提供（目录缺失）→ unavailable
    ing = InteractionIngest(d)
    mid = ing.inbound(connector="qq", raw_text="文件已上传", idempotency_key="up-1", goal_id=1, goal_ver=1)
    out = svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    prov = out["resolution"][0]["provided"][0]
    assert prov["hash"] == hashlib.sha256(b"DATA-1").hexdigest()            # hash 入账
    dest = Path(prov["path"])
    assert dest.exists() and dest.read_bytes() == b"DATA-1"                 # 并入 input/user_provided/<rid>/
    assert str(tmp_path / "input" / "user_provided" / str(rid)) in str(dest)
    assert out["resolution"][1] == {"unavailable": "用户未提供该条目文件"}
    row = d.query_one("SELECT status, resolved_message_id FROM interaction_request WHERE id=?", (rid,))
    assert row == ("resolved", mid)                                         # 终态 + 入站 provenance
    with pytest.raises(ValueError, match="非 pending"):                     # 一次性迁移：二次 resolve 拒
        svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)


def test_cancel_pipeline(env, tmp_path):
    d = env["d"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="不用了", idempotency_key="cx-1",
                                       goal_id=1, goal_ver=1)
    svc.cancel(request_id=rid, reason="用户改用公开数据", resolved_message_id=mid)
    st, res = d.query_one("SELECT status, resolution_json FROM interaction_request WHERE id=?", (rid,))
    assert st == "cancelled" and json.loads(res)["reason"] == "用户改用公开数据"


# ============ 文件请求 3 事件（reminder 分档幂等；now 注入保确定性）============
def test_file_request_events_pending_reminder_resolved(env, tmp_path):
    d, ob = env["d"], env["ob"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(tmp_path / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    fn = FileRequestNotifier(d, ob, remind_interval_h=POLICY["interaction_request"]["remind_interval_h"])
    created = float(d.query_one("SELECT strftime('%s', created_at) FROM interaction_request WHERE id=?", (rid,))[0])
    assert f"filereq:{rid}:pending" in fn.scan(now_ts=created + 60)          # 立即：pending 事件
    assert fn.scan(now_ts=created + 60) == []                                # 同态重扫幂等
    assert f"filereq:{rid}:reminder:1" in fn.scan(now_ts=created + 25 * 3600)   # 过 24h：第 1 档提醒
    assert fn.scan(now_ts=created + 25 * 3600) == []                         # 同档幂等
    assert f"filereq:{rid}:reminder:2" in fn.scan(now_ts=created + 49 * 3600)   # 第 2 档
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="上传了", idempotency_key="ev-1",
                                       goal_id=1, goal_ver=1)
    up = tmp_path / "up"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    assert f"filereq:{rid}:resolved" in fn.scan(now_ts=created + 50 * 3600)  # 终态事件


# ============ 全局等待（§7.1 M5：Advancer 停、query/通知照常）============
@pytest.fixture()
def adv_env(tmp_path):
    """文件库真组件 advancer + console + mediator + 通知（端到端全局等待）。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    dbp = str(tmp_path / "w.sqlite")
    daemon = WriteDaemon(db.connect(dbp))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="g", predicate_json={})
    console = Console(daemon)
    card_path = tmp_path / "sc.json"
    pub = SC.SqliteStatusPublisher(open_responder_read_conn(dbp), policy=POLICY,
                                   goal_body_md="g", out_path=str(card_path))

    def provider(cyc, pack):
        return {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "r"}]},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}

    adv = SqliteAdvancer(state, SqliteCompiler(db.connect(dbp), POLICY, goal_body_md="g"), provider,
                         status_publisher=pub, precheck=make_advancer_precheck(console, daemon))
    return {"d": daemon, "c": console, "adv": adv, "tmp": tmp_path, "card": card_path, "dbp": dbp}


def test_global_wait_pending_request_blocks_advancer_but_not_query(adv_env, tmp_path):
    d, adv = adv_env["d"], adv_env["adv"]
    svc = FileRequestService(d, SCHEMAS, POLICY, str(adv_env["tmp"] / "input"))
    rid = svc.create_checked(goal_id=1, goal_ver=1, stage="plan", request=_request(1))
    assert adv.run_cycles(2) == []                                           # 不开新轮、不发研究执行
    assert "文件请求" in adv.last_block_reason
    assert d.query_one("SELECT count(*) FROM cycle")[0] == 0                 # 一个轮都没开
    # query/通知照常（不走 Advancer）
    ob = Outbox(str(adv_env["tmp"] / "ob"))
    fn = FileRequestNotifier(d, ob, remind_interval_h=24)
    created = float(d.query_one("SELECT strftime('%s', created_at) FROM interaction_request WHERE id=?", (rid,))[0])
    assert fn.scan(now_ts=created + 1)                                       # 通知照常
    conn = FakeConnector(); assert ob.deliver_pending(conn)                  # 投递照常
    # resolve 后恢复推进
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="上传", idempotency_key="gw-1",
                                       goal_id=1, goal_ver=1)
    up = adv_env["tmp"] / "u"; (up / "1").mkdir(parents=True); (up / "1" / "f").write_bytes(b"x")
    svc.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    ids = adv.run_cycles(2)
    assert ids and adv_env["card"].exists()                                  # 恢复推进 + 发布照常


def test_global_wait_query_answers_during_block(adv_env):
    """阻断期间 mediator 应答照常（发布快照已存在时）。"""
    d, adv = adv_env["d"], adv_env["adv"]
    ids = adv.run_cycles(1)                                                  # 先正常跑一轮 → 卡已发布
    assert ids
    r = Console(d).handle_inbound(connector="qq", raw_text="暂停", idempotency_key="gw-p", goal_id=1, goal_ver=1)
    Console(d).confirm_directive(directive_id=r["directive_id"], confirm_message_id=r["message_id"])
    assert adv.run_cycles(1) == []                                           # precheck 消费 pause → 阻断
    assert "pause" in adv.last_block_reason
    med = Mediator(d, str(adv_env["card"]))
    mid = InteractionIngest(d).inbound(connector="qq", raw_text="现在状态？", idempotency_key="gw-q",
                                       goal_id=1, goal_ver=1)
    ans = med.handle_query(message_id=mid)                                   # query 照常
    assert ans["reply_text"] and ans["snapshot_cycle"] == ids[-1]


def test_precheck_consumes_immediate_directive_with_decision(adv_env):
    """前置检查按时机消费：immediate pause 在 run_cycles 入口被消费（DECISION 落账）后生效阻断。"""
    d, c, adv = adv_env["d"], adv_env["c"], adv_env["adv"]
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="pc-1", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r["directive_id"], confirm_message_id=r["message_id"])
    assert adv.run_cycles(1) == []
    st = d.query_one("SELECT status, consumed_cycle FROM directive WHERE id=?", (r["directive_id"],))
    assert st[0] == "consumed" and st[1] is None                             # 开轮前消费：无在途轮，cycle 空
    assert d.query_one("SELECT count(*) FROM decision WHERE directive_id=? AND actor='human'",
                       (r["directive_id"],))[0] == 1                        # 同记 DECISION
    # resume 解除后恢复
    r2 = c.handle_inbound(connector="qq", raw_text="继续", idempotency_key="pc-2", goal_id=1, goal_ver=1)
    c.confirm_directive(directive_id=r2["directive_id"], confirm_message_id=r2["message_id"])
    assert adv.run_cycles(1)                                                 # resume 被消费 → 放行
