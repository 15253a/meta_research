"""CP2.3 · SqliteGate（M1a-Gate：authorizer 隔离 + 三级校验 + gate_close_question）。

用**文件库**（门禁只读连接是独立连接；:memory: 每连接独立库，故须文件库共享）。
gate_seed 在 conftest.seed_minimal 上加：待关问 q2、非成功 eval(mr2)、target 未完成 eval(mr3)、
带 blocked applicability 的子问题（q4/answer2）——供 gate_close_question 各否定分支。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import conftest
from orchestrator import database as db
from orchestrator.gate_sqlite import (
    GATE_DENY_TABLES,
    GATE_INPUT_VIEW_NAMES,
    GateInvariantError,
    GateReject,
    SqliteGate,
    _SerializedReadConnection,
    open_gate_read_conn,
)
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent


def _gate_seed(conn):
    conftest.seed_minimal(conn)
    conn.executescript("""
    -- 待关问 q2（本轮 active；关问 Gate 严格核 cycle↔Qn↔current goal lineage）
    INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (2,1,1,1,'q2','active','agent');
    UPDATE cycle SET active_question_id=2 WHERE id=1;
    -- 非成功 eval：eval2(created) + success attempt2 + mr2（用 variant2 避开一格子一 eval 唯一约束）
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','planned');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash)
      VALUES (2,2,1,1,'e2','standalone_eval','created',1,'h2');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status) VALUES (2,2,1,1,'standalone_eval','success');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (2,2,2,1,1,0.5,'aggregate');
    -- target 未完成：build_target3(running) + success eval3 + attempt3(build_target3) + mr3
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (3,1,'v3','{}','planned');
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,eval_key,evaluation_source)
      VALUES (3,1,1,'eval',3,'running',3,'create_evaluation','e3','factory');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,build_target_id,target_set_hash)
      VALUES (3,3,1,1,'e3','factory','created',1,3,'h3');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status) VALUES (3,3,1,3,1,'factory','success');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (3,3,3,1,1,0.7,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=3 WHERE id=3;
    -- 子问题 q4（answered）+ answer2 + blocked applicability（供 applicability 同版负向）
    INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (4,2,1,1,1,'q4','active','decompose');
    INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) VALUES (2,4,1,1,1,'answered','a4md');
    INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,literature_ref,claim_md) VALUES (2,4,1,1,'literature','r','c');
    UPDATE question SET status='answered' WHERE id=4;
    INSERT INTO answer_applicability(answer_id,goal_id,goal_ver,status,rationale_md,spawned_question_id)
      VALUES (2,1,1,'blocked','stale',NULL);
    -- q6：q2 子树内、answered、无 applicability（供 child_answer happy）
    INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (6,2,1,1,1,'q6','active','decompose');
    INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) VALUES (3,6,1,1,1,'answered','a6md');
    INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,literature_ref,claim_md) VALUES (3,6,1,1,'literature','r','c');
    UPDATE question SET status='answered' WHERE id=6;
    -- human 证据用：一条 actor=human 的 decision
    INSERT INTO decision(id,actor,type,payload_json) VALUES (5,'human','directive_ack','{}');
    """)
    conn.commit()


@pytest.fixture()
def gate_env(tmp_path):
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path); _gate_seed(seed); seed.close()
    daemon = WriteDaemon(db.connect(path))
    gate = SqliteGate(daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"))
    return gate, daemon


# ============ authorizer 隔离（§6.13(2)）============
def test_authorizer_denies_9_tables(gate_env):
    gate, _ = gate_env
    for t in sorted(GATE_DENY_TABLES):
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
            gate.read.execute(f"SELECT count(*) FROM {t}").fetchone()


def test_authorizer_denies_trajectory_view(gate_env):
    gate, _ = gate_env
    with pytest.raises(sqlite3.DatabaseError, match="prohibited|not authorized"):
        gate.read.execute("SELECT count(*) FROM v_metric_result_trajectory").fetchone()


def test_authorizer_allows_gate_input_tables(gate_env):
    gate, _ = gate_env
    for t in ["question", "answer", "evidence", "metric_result", "evaluation", "evaluation_attempt",
              "answer_applicability", "build_target"]:
        assert gate.read.execute(f"SELECT count(*) FROM {t}").fetchone()[0] >= 0   # 放行


def test_gate_input_views_closure_excludes_denied(gate_env):
    """§6.13(2)③：门禁判据只从 gate_input_* 视图取数，其依赖闭包**不含**任何禁表——
    逐视图在 authorizer 下可查即证（若闭包含禁表，读它时 authorizer 会拒→抛）。"""
    gate, _ = gate_env
    for v in GATE_INPUT_VIEW_NAMES:
        gate.read.execute(f"SELECT * FROM {v} LIMIT 1").fetchone()


def test_gate_read_connection_serializes_cursor_calls_across_workers():
    """Concurrent target gates must never enter one sqlite connection twice."""
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    start = threading.Barrier(2)

    class Cursor:
        def fetchone(self):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return (1,)

    class Connection:
        def execute(self, _sql, _params=()):
            return Cursor()

    read = _SerializedReadConnection(Connection())
    results = []

    def query():
        start.wait()
        results.append(read.execute("SELECT 1").fetchone())

    workers = [threading.Thread(target=query) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1)

    assert all(not worker.is_alive() for worker in workers)
    assert results == [(1,), (1,)]
    assert maximum_active == 1


# ============ gate_close_question happy ============
def test_close_with_evaluation_evidence(gate_env):
    gate, daemon = gate_env
    aid = gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                   evidence=[{"kind": "evaluation", "metric_result_id": "mr1"}], answer_md="因")
    assert daemon.query_one("SELECT status FROM question WHERE id=2")[0] == "answered"
    assert daemon.query_one("SELECT active_question_id FROM cycle WHERE id=1")[0] is None
    row = daemon.query_one("SELECT kind,metric_result_id,evaluation_id FROM evidence WHERE answer_id=?", (int(aid[1:]),))
    assert row == ("evaluation", 1, 1)


def test_close_enforces_question_evidence_closure_contract(gate_env):
    gate, daemon = gate_env
    contract = {
        "kind": "evidence_closure_v1",
        "allowed_evidence": ["literature"],
        "answer_criterion_md": "文献证据支持命题。",
        "refute_criterion_md": "文献证据否定命题。",
    }
    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE question SET predicate_json=? WHERE id=2",
            (json.dumps(contract, ensure_ascii=False),))

    assert "closure contract" in _reject(
        gate, question_id="q2", verdict="answered",
        evidence=[{"kind": "evaluation", "metric_result_id": "mr1"}])
    aid = gate.gate_close_question(
        cycle_id="c1", question_id="q2", verdict="answered",
        evidence=[{"kind": "literature", "citation_md": "systematic-review"}],
        answer_md="文献结论")
    assert aid.startswith("a")


def test_close_fails_closed_on_corrupt_question_contract(gate_env):
    gate, daemon = gate_env
    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE question SET predicate_json=? WHERE id=2",
            (json.dumps({
                "kind": "evidence_closure_v1",
                "allowed_evidence": ["literature"],
                "answer_criterion_md": "",
                "refute_criterion_md": "有否定证据。",
            }, ensure_ascii=False),))
    assert "criterion" in _reject(
        gate, question_id="q2", verdict="answered",
        evidence=[{"kind": "literature", "citation_md": "review"}])


def _predicate_gate(tmp_path, *, op, value):
    predicate = {
        "kind": "metric_comparison", "protocol": "proto", "protocol_ver": 1,
        "metric_id": "acc", "metric_ver": 1, "scope": "aggregate",
        "success": {"op": op, "value": value},
    }
    path = str(tmp_path / f"predicate-{op.replace('>', 'gt').replace('=', 'eq')}.sqlite")
    seed = db.connect(path)
    seed.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g',?)",
        (json.dumps(predicate),))
    seed.executescript("""
    INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) VALUES (1,1,1,'reasoning','v0');
    INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,active_cycle)
      VALUES (1,1,1,1,'root','active','agent',1);
    UPDATE cycle SET active_question_id=1 WHERE id=1;
    INSERT INTO baseline(id,slug,canonical_key,status) VALUES (1,'b','bk','planned');
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (1,1,'v','{}','planned');
    INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'proto','{}');
    INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher');
    INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1);
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,
                             eval_action,eval_key,evaluation_source)
      VALUES (1,1,1,'eval',1,'complete',1,'create_evaluation','e1','factory');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,
                           created_cycle,build_target_id,target_set_hash)
      VALUES (1,1,1,1,'e1','factory','created',1,1,'h');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status)
      VALUES (1,1,1,1,1,'factory','success');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope)
      VALUES (1,1,1,1,1,0.9,'aggregate');
    UPDATE evaluation SET status='success',canonical_attempt_id=1 WHERE id=1;
    """)
    seed.commit(); seed.close()
    daemon = WriteDaemon(db.connect(path))
    gate = SqliteGate(
        daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"))
    return gate, daemon


def test_root_answer_must_satisfy_metric_comparison_goal_predicate(tmp_path):
    gate, daemon = _predicate_gate(tmp_path, op=">=", value=0.9)
    aid = gate.gate_close_question(
        cycle_id="c1", question_id="q1", verdict="answered",
        evidence=[{"kind": "evaluation", "metric_result_id": "mr1"}],
        answer_md="predicate is measured")
    assert aid.startswith("a")


def test_root_answer_rejects_metric_below_goal_predicate(tmp_path):
    gate, daemon = _predicate_gate(tmp_path, op=">", value=0.9)
    with pytest.raises(GateReject, match="metric_comparison"):
        gate.gate_close_question(
            cycle_id="c1", question_id="q1", verdict="answered",
            evidence=[{"kind": "evaluation", "metric_result_id": "mr1"}],
            answer_md="must not round 0.9 upward")
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "active"


def test_root_metric_predicate_follows_valid_child_answer_evidence(tmp_path):
    gate, daemon = _predicate_gate(tmp_path, op=">=", value=0.9)
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (2,1,1,1,1,'child','active','decompose')")
        conn.execute(
            "INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) "
            "VALUES (1,2,1,1,1,'answered','child measured answer')")
        conn.execute(
            "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,evaluation_id,"
            "evaluation_attempt_id,metric_result_id,metric_id,metric_ver,scope,claim_md,valid) "
            "VALUES (1,2,1,1,'evaluation',1,1,1,1,1,'aggregate','measured',1)")
        conn.execute("UPDATE question SET status='answered' WHERE id=2")
    aid = gate.gate_close_question(
        cycle_id="c1", question_id="q1", verdict="answered",
        evidence=[{"kind": "child_answer", "child_question_id": "q2"}],
        answer_md="aggregate direct child")
    assert aid == "a2"


def test_close_with_literature_and_human_evidence(gate_env):
    gate, daemon = gate_env
    aid = gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                   evidence=[{"kind": "literature", "citation_md": "paper X"},
                                             {"kind": "human", "human_ref": "d5"}], answer_md="因")
    kinds = {r[0] for r in daemon.query("SELECT kind FROM evidence WHERE answer_id=?", (int(aid[1:]),))}
    assert kinds == {"literature", "human"}


def test_close_in_caller_transaction_rolls_back_as_one_unit(gate_env):
    gate, daemon = gate_env
    with pytest.raises(RuntimeError, match="after-close"):
        with daemon.transaction() as conn:
            gate.gate_close_question_in_txn(
                conn, cycle_id="c1", question_id="q2", verdict="answered",
                evidence=[{"kind": "evaluation", "metric_result_id": "mr1"}], answer_md="因")
            raise RuntimeError("after-close")
    assert daemon.query_one("SELECT status FROM question WHERE id=2")[0] == "active"
    assert daemon.query_one("SELECT active_question_id FROM cycle WHERE id=1")[0] == 2
    assert daemon.query_one("SELECT count(*) FROM answer WHERE question_id=2")[0] == 0
    assert daemon.query_one("SELECT count(*) FROM evidence WHERE question_id=2")[0] == 0


def test_close_with_child_answer_in_subtree(gate_env):
    """child_answer happy：q6 在 q2 子树、answered、无 applicability → 关 q2 引用 q6 成功、evidence 落库。"""
    gate, daemon = gate_env
    aid = gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                   evidence=[{"kind": "child_answer", "child_question_id": "q6"}], answer_md="因")
    row = daemon.query_one("SELECT kind,child_answer_id FROM evidence WHERE answer_id=?", (int(aid[1:]),))
    assert row == ("child_answer", 3)   # answer3 = q6 的 answer


def test_close_resolves_parent_dep(gate_env):
    """关问会把「依赖本问题」的 pending question_dep 置 satisfied。"""
    gate, daemon = gate_env
    with daemon.transaction() as conn:   # q2 依赖 q7（pending）
        conn.execute("UPDATE question SET status='open',active_cycle=NULL WHERE id=2")
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,active_cycle) "
                     "VALUES (7,1,1,1,'q7','active','agent',1)")
        conn.execute("UPDATE cycle SET active_question_id=7 WHERE id=1")
        conn.execute("INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status) VALUES (2,'question',7,'pending')")
    gate.gate_close_question(cycle_id="c1", question_id="q7", verdict="answered",
                             evidence=[{"kind": "literature", "citation_md": "x"}], answer_md="因")
    assert daemon.query_one("SELECT status FROM question_dep WHERE question_id=2 AND depends_on_question_id=7")[0] == "satisfied"


# ============ gate_close_question 否定 ============
def _reject(gate, **kw):
    with pytest.raises(GateReject) as ei:
        gate.gate_close_question(cycle_id="c1", answer_md="x", **kw)
    return str(ei.value)


def test_reject_terminal_question(gate_env):
    gate, _ = gate_env   # q1 已 answered（seed）
    assert "已终态" in _reject(gate, question_id="q1", verdict="answered",
                               evidence=[{"kind": "literature", "citation_md": "x"}])


def test_reject_no_evidence(gate_env):
    gate, _ = gate_env
    assert "≥1 条证据" in _reject(gate, question_id="q2", verdict="answered", evidence=[])


def test_reject_metric_result_not_exist(gate_env):
    gate, _ = gate_env
    assert "metric_result_id 不存在" in _reject(gate, question_id="q2", verdict="answered",
                                                evidence=[{"kind": "evaluation", "metric_result_id": "mr999"}])


def test_reject_non_success_evaluation(gate_env):
    gate, _ = gate_env   # mr2 的 eval2='created'
    assert "非成功测量" in _reject(gate, question_id="q2", verdict="answered",
                                   evidence=[{"kind": "evaluation", "metric_result_id": "mr2"}])


def test_reject_target_not_complete(gate_env):
    gate, _ = gate_env   # mr3 的 build_target3='running'
    assert "target_complete" in _reject(gate, question_id="q2", verdict="answered",
                                        evidence=[{"kind": "evaluation", "metric_result_id": "mr3"}])


def test_reject_child_answer_applicability_not_still_applicable(gate_env):
    gate, _ = gate_env   # q4 的 answer2 有 blocked applicability（当前 goal_ver）
    assert "applicability" in _reject(gate, question_id="q2", verdict="answered",
                                      evidence=[{"kind": "child_answer", "child_question_id": "q4"}])


def test_reject_child_answer_no_answer(gate_env):
    gate, daemon = gate_env
    # 造一个无 answer 的子问题 q5
    with daemon.transaction() as conn:
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (5,1,1,1,'q5','open','agent')")
    assert "无 answer" in _reject(gate, question_id="q2", verdict="answered",
                                  evidence=[{"kind": "child_answer", "child_question_id": "q5"}])


def test_reject_records_gate_decision(gate_env):
    gate, daemon = gate_env
    with pytest.raises(GateReject):
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered", evidence=[], answer_md="x")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0] >= 1


def test_trigger_abort_becomes_clean_reject(gate_env):
    """BLOCKER 回归：过了 gate 预检但撞触发器（child_answer 引用非子树的 q1）→ 转干净 GateReject + 记 DECISION，
    而非裸 sqlite3.IntegrityError 逃逸、无半写、无决策。"""
    gate, daemon = gate_env
    before = daemon.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0]
    with pytest.raises(GateReject, match="DB 不变量拒绝|子树"):   # q1 answered、无 applicability，但非 q2 子树 → trg_evidence_child_scope
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                 evidence=[{"kind": "child_answer", "child_question_id": "q1"}], answer_md="因")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0] == before + 1
    assert daemon.query_one("SELECT status FROM question WHERE id=2")[0] == "active"        # 未半写
    assert daemon.query_one("SELECT count(*) FROM answer WHERE question_id=2")[0] == 0


def test_unknown_integrity_error_fails_loud_as_gate_invariant(gate_env, monkeypatch):
    """只有已知 I3 焊死触发器属业务拒；未知 IntegrityError 不得被统一洗成 GateReject。"""
    gate, daemon = gate_env
    before = daemon.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0]

    def corrupt_write(*args, **kwargs):
        raise sqlite3.IntegrityError("SIM unexpected internal constraint")

    monkeypatch.setattr(gate, "_insert_evidence", corrupt_write)
    with pytest.raises(GateInvariantError, match="非预期 DB 约束"):
        gate.gate_close_question(cycle_id="c1", question_id="q2", verdict="answered",
                                 evidence=[{"kind": "literature", "citation_md": "x"}], answer_md="因")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0] == before
    assert daemon.query_one("SELECT count(*) FROM answer WHERE question_id=2")[0] == 0


def test_reject_malformed_evidence(gate_env):
    gate, _ = gate_env
    assert "缺必需键" in _reject(gate, question_id="q2", verdict="answered", evidence=[{"kind": "evaluation"}])
    assert "kind 非法" in _reject(gate, question_id="q2", verdict="answered", evidence=[{"kind": "bogus"}])


def test_reject_nonexistent_question_null_fk_decision(gate_env):
    """BLOCKER 回归（codex 第1轮）：拒因是引用不存在时，decision 的 FK 列写 NULL、attempted 入 payload，
    _reject 自身不撞 FK（否则「干净拒 + 记 DECISION」失效）。"""
    gate, daemon = gate_env
    with pytest.raises(GateReject, match="question 不存在"):
        gate.gate_close_question(cycle_id="c1", question_id="q999", verdict="answered",
                                 evidence=[{"kind": "literature", "citation_md": "x"}], answer_md="因")
    fk, payload = daemon.query_one("SELECT question_id,payload_json FROM decision WHERE actor='gate' AND type='reject' ORDER BY id DESC LIMIT 1")
    assert fk is None and json.loads(payload)["attempted_question"] == "q999"   # FK 空、原始串入结构化字段


def test_reject_nonexistent_cycle_null_fk_decision(gate_env):
    gate, daemon = gate_env
    with pytest.raises(GateReject, match="cycle 不存在"):
        gate.gate_close_question(cycle_id="c999", question_id="q2", verdict="answered",
                                 evidence=[{"kind": "literature", "citation_md": "x"}], answer_md="因")
    fk, payload = daemon.query_one("SELECT cycle_id,payload_json FROM decision WHERE actor='gate' AND type='reject' ORDER BY id DESC LIMIT 1")
    assert fk is None and json.loads(payload)["attempted_cycle"] == "c999"


# ============ 门禁只读连接 + preview ============
def test_gate_read_conn_is_readonly(gate_env):
    gate, _ = gate_env   # 门禁连接 mode=ro 物理只读：写被拒（护单写路径 P1）
    with pytest.raises(sqlite3.DatabaseError, match="readonly|not authorized"):
        gate.read.execute("INSERT INTO decision(actor,type,payload_json) VALUES ('gate','x','{}')")


def test_preview_answer_refs(gate_env):
    gate, _ = gate_env
    from orchestrator.interfaces import Artifact
    ok = Artifact(stage="reasoning", files={"answer.json": {
        "question_id": "q2", "verdict": "answered", "answer_md": "因",
        "evidence": [{"kind": "evaluation", "metric_result_id": "mr1"}]}})
    assert gate.preview(ok).ok
    bad = Artifact(stage="reasoning", files={"answer.json": {
        "question_id": "q999", "verdict": "answered", "answer_md": "因",
        "evidence": [{"kind": "evaluation", "metric_result_id": "mr999"}]}})
    res = gate.preview(bad)
    assert not res.ok and any("question_id 不存在" in e for e in res.errors)
