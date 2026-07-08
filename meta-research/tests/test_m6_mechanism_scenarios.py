"""CP7.4 · §7.3 机制验收剧本（M6）——**命名场景 + 端到端串联断言**。

范式仿 test_m4_semantic_cases（含其**跨测试模块 import 脚手架**的约定：依赖 pytest 默认 importmode +
无 tests/__init__.py；改 importlib 模式或加 __init__.py 会破坏本套与 m4 套的 collection）：不重造设置，
**复用**既有真组件脚手架（test_attack_advance / test_import_worker / harness / obs_parser / console /
mediator），把 §7.3 四个机制剧本各写成一条**显式命名**的验收场景，断言其 §7.3 判据（happy + 失败路径）。
验的是**状态机 + 不变量 I1–I3**（mock provider 驱动真组件）——非真 Codex（真 Codex attack 受 plan 契约
缺口阻塞，ROADMAP 载；机制正确性与真 Codex 生成能力正交）。

**价值定位**（内审记）：剧本 1 的 I1/I2/I3 因果链 join 是**新验证**（无既有测试在一处断言 I1 orphan-join
+ I2 metric_result→run→checkpoint 溯源）；剧本 2/4 是既有覆盖的 **§7.3 命名验收层**（贡献 §7.3 标签 +
端到端串联，非新机制）——同 test_m4_semantic_cases 的取舍。

§7.3 四剧本（reference/第三部分）：
  1. 主链路 baseline→变体→evaluation→对照下结论（I1 协议口径不可变 / I2 测量履约 / I3 关问需证据）
  2. import 对照失败路径（license deny / smoke 失败 / factory eval 失败——三者皆不入活跃池）
  3. 运行日志分析（观测 suspect → 不作正向证据，fail-closed）
  4. 人机 + 安全负例（低置信 unclear 不改状态 / 未确认硬指令 consume 拒 / responder 写库被拒）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator import harness as H
from orchestrator import obs_parser as OP
from orchestrator.writedaemon import WriteDaemon

# 复用既有真组件脚手架（tests/ 在 sys.path，conftest 已保证）
from test_attack_advance import _mk_env, _bootstrap_attack
from test_import_worker import _mk_worker, _seed_deferred, _fetch_ok

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]
NAN_LOG = "loss: 1.0\nloss: nan\nloss: 0.9\n"


# ============ 剧本 1 · 主链路（I1 协议口径不可变 / I2 测量履约 / I3 关问需证据）============
def test_scenario1_main_chain_invariants(tmp_path):
    """attack 全链跑通后，显式验三不变量的**因果链闭合**（非仅状态字段）。"""
    from orchestrator.advancer import SqliteAdvancer
    path = str(tmp_path / "r.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)

    # I1 协议口径不可变：每个 metric_result 的 (metric,ver) 必在其 evaluation 的 protocol 的 metric_defs 内。
    # 先断言确有 metric_result（否则 orphan==0 空真）——非空自证；DB 层 trg_mr_i2_ins 触发器是真执法者，
    # 此 orphan-join 在验收层复核其结果。
    assert daemon.query_one("SELECT count(*) FROM metric_result")[0] >= 1
    orphan = daemon.query_one(
        "SELECT count(*) FROM metric_result mr JOIN evaluation e ON e.id=mr.evaluation_id "
        "LEFT JOIN protocol_metric pm ON pm.protocol_id=e.protocol_id AND pm.protocol_ver=e.protocol_ver "
        "AND pm.metric_id=mr.metric_id AND pm.metric_ver=mr.metric_ver WHERE pm.metric_id IS NULL")[0]
    assert orphan == 0                                              # 无越界指标（I1）

    # I2 测量履约：metric_result → attempt(success) → build_target → run(**success**) + checkpoint 全链可回溯。
    # 显式断言 run.status='success'（外审 SHOULD：不然一条 failed run + 同 variant 旧 checkpoint 也会误过）。
    row = daemon.query_one(
        "SELECT mr.id, ea.status, r.status, ck.id FROM metric_result mr "
        "JOIN evaluation_attempt ea ON ea.id=mr.evaluation_attempt_id "
        "JOIN evaluation e ON e.id=mr.evaluation_id "
        "JOIN build_target bt ON bt.id=COALESCE(ea.build_target_id, e.build_target_id) "
        "JOIN run r ON r.build_target_id=bt.id AND r.kind='build' "
        "JOIN checkpoint ck ON ck.variant_id=r.variant_id "
        "ORDER BY mr.id DESC LIMIT 1")
    assert row is not None and row[1] == "success" and row[2] == "success" and row[3] is not None  # 锚到成功 run+checkpoint
    assert daemon.query_one("SELECT count(*) FROM execution_log")[0] >= 1    # 有真执行日志

    # I3 关问需证据：**每个 answered 问题**都有 valid evaluation 证据带真 metric_result（answered↔证据绑定，
    # 外审 SHOULD：反向断言不存在「answered 但无有效 evaluation 证据」的问题——非仅取最新一条 evidence）。
    answered_no_evidence = daemon.query_one(
        "SELECT count(*) FROM question q WHERE q.status='answered' AND NOT EXISTS ("
        "  SELECT 1 FROM answer a JOIN evidence e ON e.answer_id=a.id WHERE a.question_id=q.id "
        "  AND e.kind='evaluation' AND e.valid=1 AND e.metric_result_id IS NOT NULL)")[0]
    assert answered_no_evidence == 0                                        # 无「关而无证据」（I3）
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "answered"
    # I3 结构保证**显式断言**（不止注释）：evidence.kind 封闭词表拒 execution_log（DDL CHECK）——即
    # 「关问永不引 log/观测作证」由 schema 焊死
    a1 = daemon.query_one("SELECT id, question_id FROM answer ORDER BY id DESC LIMIT 1")
    # 消歧（外审 NIT）：补全非 kind 必填列（claim_md），使拒因**只**能是 kind CHECK——match "CHECK"
    # 确证是词表约束触发（非 NOT NULL 等别的约束误绿；否则将来 kind 词表松了本断言也会假过）
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with daemon.transaction() as conn:
            conn.execute("INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,claim_md) "
                         "VALUES (?,?,1,1,'execution_log','x')", (a1[0], a1[1]))


# ============ 剧本 2 · import 对照三失败路径（皆不入活跃池）============
def test_scenario2_import_license_deny_not_materialized(tmp_path):
    """license scope 越权（缺 allow_publish_pool）→ 不物化、baseline 未动、dep 仍锁。"""
    path = str(tmp_path / "r.sqlite")
    daemon, state, w = _mk_worker(path, tmp_path / "w")
    sel = _seed_deferred(daemon, state, scope='{"allow_eval": true}')       # 缺 allow_publish_pool = 越权
    w.materialize_pending()
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "planned"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0             # 不物化：无 run
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert daemon.query_one("SELECT status FROM question_dep WHERE question_id=1")[0] == "pending"  # dep 仍锁
    assert state.is_schedulable("q1") is False                             # 问题不因失败物化被调度


def test_scenario2_import_smoke_fail_no_target_ready(tmp_path):
    """smoke 失败 → build_target failed(smoke)、baseline build_failed、不入活跃池、不 imported。"""
    path = str(tmp_path / "r.sqlite")
    fetch = lambda c: {**_fetch_ok(c), "smoke_cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    w.materialize_pending()
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "smoke")
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert state.is_schedulable("q1") is False                             # 失败物化不解锁调度（dep 仍锁，外审 SHOULD）


def test_scenario2_import_eval_fail_no_pool_publish(tmp_path):
    """factory eval 失败（smoke 过）→ 无 evaluation 注册、不 imported（不 pool_publish）。"""
    path = str(tmp_path / "r.sqlite")
    fetch = lambda c: {**_fetch_ok(c), "eval_cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    daemon, state, w = _mk_worker(path, tmp_path / "w", fetch=fetch)
    sel = _seed_deferred(daemon, state)
    w.materialize_pending()
    assert daemon.query_one("SELECT count(*) FROM evaluation")[0] == 0     # 未注册测量
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    assert daemon.query_one("SELECT count(*) FROM external_import WHERE action='materialize_failed'")[0] == 1
    assert daemon.query_one("SELECT status FROM baseline WHERE id=?", (sel["baseline_id"],))[0] == "build_failed"  # 不入活跃池
    assert state.is_schedulable("q1") is False                             # 失败物化不解锁调度（dep 仍锁，外审 SHOULD）


# ============ 剧本 3 · 运行日志分析（suspect **不作正向证据**，fail-closed 于消费侧）============
def test_scenario3_suspect_evidence_rejected_by_gate(tmp_path):
    """§7.3 剧本3 的**消费侧**判据（外审 BLOCKER）：nan log → 观测 suspect → 用该 suspect attempt 的测量
    作证据**关问被 gate 拒**（fail-closed，不作正向证据），问题保持不关。非仅验 parser 打标。"""
    from orchestrator.gate_sqlite import SqliteGate, open_gate_read_conn, GateReject
    from orchestrator.schemas import SchemaSet
    path = str(tmp_path / "r.sqlite")
    daemon = WriteDaemon(db.connect(path))
    conftest.seed_minimal(daemon.conn)                              # 含 attempt1 + metric_result（success 链）
    daemon.conn.commit()
    mr = daemon.query_one("SELECT id FROM metric_result ORDER BY id LIMIT 1")[0]
    with daemon.transaction() as conn:                             # 新开一个待关问题（复用同证据链）
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (2,1,1,1,'suspect 证据能关吗','active','agent')")
    # 训练 log 含 nan → attempt1 当前口径 suspect
    elid = H.register_execution_log(daemon, cycle_id="c1", log_kind="eval", ref="st/nan.log",
                                    content_hash=_hash(NAN_LOG), n_bytes=len(NAN_LOG), evaluation_attempt_id=1)
    OP.ingest_observation(daemon, execution_log_id=elid, log_bytes=NAN_LOG.encode(), obs_policy=OBS)
    obs_conn = db.connect(path)
    assert OP.suspect_for_attempt(obs_conn, 1, OBS) == 1            # 打标（setup 前提）

    gate = SqliteGate(daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"),
                      parser_suspect=lambda aid: OP.suspect_for_attempt(db.connect(path), aid, OBS))
    with pytest.raises(GateReject, match="存疑"):                   # 消费侧 fail-closed：suspect 证据关问被拒
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                 evidence=[{"kind": "evaluation", "metric_result_id": f"mr{mr}",
                                            "claim_md": "用存疑测量关问"}], answer_md="试图关问")
    assert daemon.query_one("SELECT status FROM question WHERE id=2")[0] == "active"   # 未被关（保持）


# ============ 剧本 4 · 人机 + 安全负例（§7.3 item-4 三向负例，逐条映射）============
def test_scenario4_neg1_ambiguous_query_not_autoanswered_as_query(tmp_path):
    """§7.3 item-4 负例①「query 误判方向」：疑似指令的礼貌式话语（"停掉好吗"含疑问助词）**不被当 query
    自动作答**、也不当 directive 执行——保守分类归 unclear（进澄清环）。防「query↔directive 边界」误放。"""
    from orchestrator.console import Console, KeywordClassifier
    assert KeywordClassifier().classify({"raw_text": "把这个停掉好吗"})["intent"] == "unclear"   # 不判 query
    daemon = WriteDaemon(db.connect(":memory:")); conftest.seed_minimal(daemon.conn)
    r = Console(daemon).handle_inbound(connector="qq", raw_text="把这个停掉好吗", idempotency_key="s4a",
                                       goal_id=1, goal_ver=1)
    assert r["intent"] == "unclear" and r["directive_id"] is None          # 不产 directive
    assert daemon.query_one("SELECT count(*) FROM directive")[0] == 0
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='human'")[0] == 0   # 不改状态


def test_scenario4_neg2_directive_not_executed_without_confirm(tmp_path):
    """§7.3 item-4 负例②「directive 误判方向 / 未确认不执行」：硬指令须回显确认后才能消费——未确认
    consume 拒、状态不变（不把可能误判的指令静默作状态变更）。"""
    from orchestrator.console import Console
    daemon = WriteDaemon(db.connect(":memory:")); conftest.seed_minimal(daemon.conn)
    c = Console(daemon)
    r = c.handle_inbound(connector="qq", raw_text="暂停", idempotency_key="s4b", goal_id=1, goal_ver=1)
    with pytest.raises(ValueError, match="未经回显确认"):
        c.consume_directive(directive_id=r["directive_id"], cycle_id="c1")
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (r["directive_id"],))[0] == "pending"
    assert c.has_blocking_pause() is False                                 # 未确认硬 pause 不阻断（不静默生效）


def test_scenario4_neg3_responder_write_denied(tmp_path):
    """§7.3 item-4 负例③：query 只读应答器连接写库被 authorizer 拒（P1 只读边界）。"""
    from orchestrator.mediator import open_responder_read_conn
    path = str(tmp_path / "r.sqlite")
    daemon = WriteDaemon(db.connect(path)); conftest.seed_minimal(daemon.conn); daemon.conn.commit()
    ro = open_responder_read_conn(path)
    for sql in ("UPDATE question SET score=9 WHERE id=1",
                "INSERT INTO decision(actor,type,payload_json) VALUES ('human','x','{}')"):
        with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
            ro.execute(sql)


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()
