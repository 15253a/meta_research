"""CP5.1 · ExecGate 执行生命周期 gates（§4.1.4 池注册家族·执行侧，M4）。

文件库（门禁 mode=ro 独立连接）。gate_seed 在 conftest.seed_minimal 上加 cycle2 + 新鲜 build/eval 目标
（seed 的 cycle1 目标已 complete，供既有链引用）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import conftest
from orchestrator import database as db
from orchestrator.gate_exec import ExecGate
from orchestrator.gate_sqlite import GateReject, open_gate_read_conn
from orchestrator.writedaemon import WriteDaemon


def _seed(conn):
    conftest.seed_minimal(conn)
    conn.executescript("""
    INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
      VALUES (2,1,1,1,'current q2','active','agent');
    INSERT INTO cycle(id,goal_id,goal_ver,status,route,active_question_id,policy_version)
      VALUES (2,1,1,'bundle','attack',2,'v0');
    INSERT INTO baseline(id,slug,canonical_key,status) VALUES (2,'b2','bk2','planned');
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,2,'v2','{}','planned');
    -- bt10：build 目标（cycle2 seq1，pending）；bt11：eval 目标（cycle2 seq2，pending，required metric (1,1)）
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id)
      VALUES (10,2,2,'build',1,'pending',2,2);
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,eval_key,evaluation_source)
      VALUES (11,2,2,'eval',2,'pending',2,'create_evaluation','e10','factory');
    INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) VALUES (11,1,1);
    """)
    conn.commit()


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path); _seed(seed); seed.close()
    daemon = WriteDaemon(db.connect(path))
    gate = ExecGate(daemon, open_gate_read_conn(path))
    return gate, daemon


def _judge_pass(daemon, bt_id, kind, subject_hash):
    """写通过评审：runner_call(audit,success) + DECISION(judge, verdict=pass)。返回 runner_call id。"""
    with daemon.transaction() as conn:
        rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (2,'audit',?,'success')",
                          (kind,)).lastrowid
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'judge',?,?)",
                     (kind, json.dumps({"build_target_id": bt_id, "review_kind": kind, "round_no": 1,
                                        "verdict": "pass", "subject_hash": subject_hash,
                                        "runner_call_id": rc, "policy_hash": "ph"})))
    return rc


# ============ build_target 生命周期 + 串行 + 评审闸 ============
def test_build_target_full_lifecycle_and_review_gate(env):
    gate, d = env
    gate.gate_start_build_target(build_target_id=10)
    assert d.query_one("SELECT status FROM build_target WHERE id=10")[0] == "building"
    assert d.query_one("SELECT status FROM baseline WHERE id=2")[0] == "building"   # build 连动池
    assert d.query_one("SELECT status FROM variant WHERE id=2")[0] == "building"
    with pytest.raises(GateReject, match="串行"):                                    # bt10 在途 → bt11 不可并行
        gate.gate_start_build_target(build_target_id=11)
    gate.gate_progress_build_target(build_target_id=10, to="smoke")
    with pytest.raises(GateReject, match="代码适配评审"):                             # 无评审不得进 running
        gate.gate_progress_build_target(build_target_id=10, to="running", current_subject_hash="sh1")
    _judge_pass(d, 10, "bundle_code_review", "sh1")
    with pytest.raises(GateReject, match="代码适配评审"):                             # subject_hash 当下重算不符 → 旧 pass 失效
        gate.gate_progress_build_target(build_target_id=10, to="running", current_subject_hash="sh-DRIFT")
    gate.gate_progress_build_target(build_target_id=10, to="running", current_subject_hash="sh1")
    assert d.query_one("SELECT status FROM build_target WHERE id=10")[0] == "running"


def test_progress_illegal_transition(env):
    gate, _ = env
    gate.gate_start_build_target(build_target_id=10)
    with pytest.raises(GateReject, match="非法迁移"):
        gate.gate_progress_build_target(build_target_id=10, to="running", current_subject_hash="x")  # 跳过 smoke


def test_code_review_can_be_disabled_without_weakening_lifecycle(env):
    default_gate, d = env
    gate = ExecGate(d, default_gate.read, require_code_review=False)
    gate.gate_start_build_target(build_target_id=10)
    with pytest.raises(GateReject, match="非法迁移"):                 # 关闭评审也不能跳过 smoke
        gate.gate_progress_build_target(build_target_id=10, to="running")
    gate.gate_progress_build_target(build_target_id=10, to="smoke")
    assert gate.review_passed(                                      # 关闭不伪造 durable review
        build_target_id=10, review_kind="bundle_code_review",
        current_subject_hash="absent") is False
    gate.gate_progress_build_target(build_target_id=10, to="running")
    assert d.query_one("SELECT status FROM build_target WHERE id=10")[0] == "running"


def test_review_policy_requires_explicit_booleans(env):
    gate, d = env
    with pytest.raises(TypeError, match="显式 bool"):
        ExecGate(d, gate.read, require_code_review=0)


def test_start_non_pending_and_wrong_order(env):
    gate, d = env
    with d.transaction() as conn:   # 直接把 bt10 置 complete → bt11 成为当前目标
        conn.execute("UPDATE build_target SET status='complete' WHERE id=10")
    with pytest.raises(GateReject, match="非 pending"):
        gate.gate_start_build_target(build_target_id=10)
    gate.gate_start_build_target(build_target_id=11)     # eval 目标 pending→running（不动池）
    assert d.query_one("SELECT status FROM build_target WHERE id=11")[0] == "running"


def test_review_passed_requires_valid_runner_call(env):
    gate, d = env
    with d.transaction() as conn:   # runner_call 非 audit/failed → 评审不算过
        rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (2,'bundle','bundle_code_review','success')").lastrowid
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'judge','bundle_code_review',?)",
                     (json.dumps({"build_target_id": 10, "verdict": "pass", "subject_hash": "sh1", "runner_call_id": rc}),))
    assert gate.review_passed(build_target_id=10, review_kind="bundle_code_review", current_subject_hash="sh1") is False


# ============ run ============
def _to_running(gate, d, bt=10, sh="sh1"):
    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    _judge_pass(d, bt, "bundle_code_review", sh)
    gate.gate_progress_build_target(build_target_id=bt, to="running", current_subject_hash=sh)


def test_run_lifecycle(env):
    gate, d = env
    with pytest.raises(GateReject, match="非 running"):    # target 未到 running 不得开训
        gate.gate_start_run(build_target_id=10, cycle_id="c2", variant_id=2, kind="build")
    _to_running(gate, d)
    rid = gate.gate_start_run(build_target_id=10, cycle_id="c2", variant_id=2, kind="build", seed=7, env_hash="eh")
    assert d.query_one("SELECT status,kind FROM run WHERE id=?", (rid,)) == ("running", "build")
    with pytest.raises(GateReject, match="checkpoint"):    # success 须已登记 checkpoint
        gate.gate_finish_run(run_id=rid, status="success")
    with d.transaction() as conn:
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (2,'final','/p','h','sha256',?)", (rid,))
    gate.gate_finish_run(run_id=rid, status="success", cost=1.5)
    assert d.query_one("SELECT status FROM run WHERE id=?", (rid,))[0] == "success"
    with pytest.raises(GateReject, match="非 running"):    # 终态不可再 finish（触发器另兜 UPDATE 冻结）
        gate.gate_finish_run(run_id=rid, status="failed", failure_kind="runtime")


def test_run_mismatch_and_failed_requires_kind(env):
    gate, d = env
    _to_running(gate, d)
    with pytest.raises(GateReject, match="不一致"):
        gate.gate_start_run(build_target_id=10, cycle_id="c2", variant_id=1, kind="build")   # 错 variant
    rid = gate.gate_start_run(build_target_id=10, cycle_id="c2", variant_id=2, kind="build")
    with pytest.raises(GateReject, match="failure_kind"):
        gate.gate_finish_run(run_id=rid, status="failed")


# ============ attempt / evaluation ============
def _bt11_running(gate, d):
    with d.transaction() as conn:
        conn.execute("UPDATE build_target SET status='complete' WHERE id=10")   # 让 bt11 成为当前目标
    gate.gate_start_build_target(build_target_id=11)


def test_attempt_create_and_finish_success(env):
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    assert r["attempt_no"] == 1
    assert d.query_one("SELECT status FROM evaluation WHERE id=?", (r["evaluation_id"],))[0] == "running"
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success",
                             metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.93}])
    erow = d.query_one("SELECT status, canonical_attempt_id FROM evaluation WHERE id=?", (r["evaluation_id"],))
    assert erow == ("success", r["attempt_id"])                       # 首成功 attempt 封 canonical
    assert d.query_one("SELECT count(*) FROM metric_result WHERE evaluation_attempt_id=?", (r["attempt_id"],))[0] == 1
    gate.gate_finish_build_target(build_target_id=11, status="complete")   # eval 目标 complete：eval success 即可
    assert d.query_one("SELECT status FROM build_target WHERE id=11")[0] == "complete"


def test_attempt_create_duplicate_cell_rejected(env):
    gate, d = env
    _bt11_running(gate, d)
    with pytest.raises(GateReject, match="一格子一"):   # seed 已有 evaluation1 于 (v1,p1@1)
        gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 1, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "eX", "source": "factory", "target_set_hash": "tsh"})


def test_attempt_create_missing_target_set_hash(env):
    gate, d = env
    with pytest.raises(GateReject, match="target_set_hash"):
        gate.gate_start_attempt(cycle_id="c2", purpose="factory",
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "eY", "source": "factory", "target_set_hash": ""})


def test_attempt_mode_exclusive_and_purpose(env):
    gate, _ = env
    with pytest.raises(GateReject, match="恰一模式"):
        gate.gate_start_attempt(cycle_id="c2", purpose="factory")
    with pytest.raises(GateReject, match="purpose 非法"):
        gate.gate_start_attempt(cycle_id="c2", purpose="bogus", evaluation_id=1)


def test_attempt_append_and_metric_i2(env):
    gate, d = env
    r = gate.gate_start_attempt(cycle_id="c2", purpose="metric_append", evaluation_id=1)   # seed eval1（success）
    assert r["attempt_no"] == 2                                       # seed attempt1 已占 1
    with pytest.raises(GateReject, match="I2"):
        gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success",
                                 metric_results=[{"metric_id": 9, "metric_ver": 1, "value": 0.1}])
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success",
                             metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.95}])
    # 已 success 的 evaluation 保留原 canonical（metric_append 不换 canonical）
    assert d.query_one("SELECT canonical_attempt_id FROM evaluation WHERE id=1")[0] == 1


def test_attempt_required_coverage(env):
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="required metric 未覆盖"):
        gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success", metric_results=[])


def test_attempt_failed_requires_kind_then_eval_failed(env):
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="failure_kind"):
        gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="failed")
    with pytest.raises(GateReject, match="在途 attempt"):             # 在途不可 finish_evaluation
        gate.gate_finish_evaluation(evaluation_id=r["evaluation_id"])
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="failed", failure_kind="runtime")
    gate.gate_finish_evaluation(evaluation_id=r["evaluation_id"])
    assert d.query_one("SELECT status FROM evaluation WHERE id=?", (r["evaluation_id"],))[0] == "failed"
    with pytest.raises(GateReject, match="已 success"):
        gate.gate_finish_evaluation(evaluation_id=1)                  # seed eval1 success


# ============ finish_build_target：complete 前置 + failed 连坐 ============
def test_finish_complete_requires_registered_variant(env):
    gate, d = env
    _to_running(gate, d)   # bt10 build 目标到 running（variant2 building、未 legal）
    with pytest.raises(GateReject, match="未注册 legal"):
        gate.gate_finish_build_target(build_target_id=10, status="complete")


def test_finish_failed_cascades_pool(env):
    gate, d = env
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_finish_build_target(build_target_id=10, status="failed", failure_kind="smoke")
    assert d.query_one("SELECT status FROM baseline WHERE id=2")[0] == "build_failed"   # build 连坐 baseline+variant
    assert d.query_one("SELECT status FROM variant WHERE id=2")[0] == "build_failed"
    with pytest.raises(GateReject, match="已终态"):
        gate.gate_finish_build_target(build_target_id=10, status="skipped")


def test_finish_engineering_blocked_cascades_pool(env):
    """内审 BLOCKER 回归：engineering_blocked 亦连坐（§4.1.4「失败/blocked 时」）——否则池对象永卡 building。"""
    gate, d = env
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_finish_build_target(build_target_id=10, status="engineering_blocked")
    assert d.query_one("SELECT status FROM baseline WHERE id=2")[0] == "build_failed"
    assert d.query_one("SELECT status FROM variant WHERE id=2")[0] == "build_failed"


def test_pending_target_cannot_be_skipped_without_early_exit(env):
    gate, d = env
    with pytest.raises(GateReject, match="skipped 仅允许"):
        gate.gate_finish_build_target(build_target_id=10, status="skipped")
    assert d.query_one("SELECT status FROM build_target WHERE id=10")[0] == "pending"


def test_target_must_bind_exact_active_question(env):
    gate, d = env
    with d.transaction() as conn:
        conn.execute("UPDATE build_target SET question_id=1 WHERE id=10")
    with pytest.raises(GateReject, match="exact active current question"):
        gate.gate_start_build_target(build_target_id=10)


def test_critical_failure_skips_successors_idempotently(env):
    gate, d = env
    with d.transaction() as conn:
        conn.execute("UPDATE build_target SET critical=1 WHERE id=10")
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_finish_build_target(build_target_id=10, status="failed", failure_kind="runtime")
    assert gate.gate_skip_remaining_targets(failed_target_id=10) == [11]
    assert gate.gate_skip_remaining_targets(failed_target_id=10) == [11]
    assert d.query_one("SELECT status FROM build_target WHERE id=11")[0] == "skipped"
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='bundle_critical_early_exit'")[0] == 1


def test_noncritical_failure_does_not_authorize_skip(env):
    gate, d = env
    with d.transaction() as conn:
        conn.execute("UPDATE build_target SET critical=0 WHERE id=10")
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_finish_build_target(build_target_id=10, status="failed", failure_kind="runtime")
    with pytest.raises(GateReject, match="未触发早退"):
        gate.gate_skip_remaining_targets(failed_target_id=10)
    with pytest.raises(GateReject, match="skipped 仅允许"):
        gate.gate_finish_build_target(build_target_id=11, status="skipped")
    assert d.query_one("SELECT status FROM build_target WHERE id=11")[0] == "pending"


def test_finish_attempt_bad_payload_clean_reject(env):
    """内审 SHOULD 回归：aggregate 带 checkpoint_id（DDL CHECK 违）→ 干净 GateReject + 审计，非裸 IntegrityError。"""
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="scope/checkpoint"):
        gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success",
                                 metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9, "checkpoint_id": 1}])
    assert d.query_one("SELECT status FROM evaluation_attempt WHERE id=?", (r["attempt_id"],))[0] == "running"  # 无半写


def test_complete_rejects_corrupt_multi_evaluation(env):
    """内审 SHOULD 回归：create 目标下竟有多条 evaluation 且任一非 success → complete 拒（判据确定、不 fetchone 任取）。"""
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="success",
                             metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}])
    with d.transaction() as conn:   # 旁路直插第二条绑同 target 的非 success eval（腐化态；新变体避开一格子一 eval）
        conn.execute("INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (3,2,'v3','{}','planned')")
        conn.execute("INSERT INTO evaluation(variant_id,protocol_id,protocol_ver,eval_key,source,status,"
                     "created_cycle,build_target_id,target_set_hash) VALUES (3,1,1,'corrupt','factory','created',2,11,'h')")
    with pytest.raises(GateReject, match="非 success"):
        gate.gate_finish_build_target(build_target_id=11, status="complete")


def test_append_retry_resets_failed_evaluation_to_running(env):
    """内审 NIT 回归：failed evaluation 追加重试 attempt → 显式 failed→running（§4.1.4）。"""
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="failed", failure_kind="runtime")
    gate.gate_finish_evaluation(evaluation_id=r["evaluation_id"])       # → failed
    r2 = gate.gate_start_attempt(
        cycle_id="c2", purpose="retry", evaluation_id=r["evaluation_id"],
        retry_of=r["attempt_id"])
    assert d.query_one("SELECT status FROM evaluation WHERE id=?", (r["evaluation_id"],))[0] == "running"
    assert r2["attempt_no"] == 2


@pytest.mark.parametrize("terminal", ["failed", "engineering_blocked"])
def test_eval_target_failed_does_not_touch_pool(env, terminal):
    """codex BLOCKER 回归（failed 与 engineering_blocked 双参）：eval 目标终败 **不动池**——eval 目标也带
    variant_id，连坐若不按 kind 守卫会把被评变体误置 build_failed。"""
    gate, d = env
    _bt11_running(gate, d)                                    # bt11 = eval 目标（variant_id=2）
    before = d.query_one("SELECT status FROM variant WHERE id=2")[0]
    gate.gate_finish_build_target(build_target_id=11, status=terminal,
                                  failure_kind="runtime" if terminal == "failed" else None)
    assert d.query_one("SELECT status FROM variant WHERE id=2")[0] == before   # 被评变体不动


def test_finish_evaluation_rejects_if_success_attempt_exists(env):
    """codex SHOULD 回归：判据=「无成功 attempt」——eval.status 未同步（running）但已有 success attempt（腐化态，
    旁路直插模拟；正路 trg_eval_sc_upd 禁 success 回退）→ 拒，不得置 failed。"""
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="failed", failure_kind="runtime")
    with d.transaction() as conn:   # 旁路直插一条 success attempt（eval.status 仍 running——status/attempt 失同步态）
        conn.execute("INSERT INTO evaluation_attempt(evaluation_id,cycle_id,attempt_no,purpose,status) "
                     "VALUES (?,2,99,'retry','success')", (r["evaluation_id"],))
    with pytest.raises(GateReject, match="success attempt"):
        gate.gate_finish_evaluation(evaluation_id=r["evaluation_id"])


def test_review_malformed_newer_decision_fails_closed(env):
    """codex SHOULD 回归：存在**更新的**畸形同类 judge DECISION（target 不可知，可能是本 target 的新 FAIL）
    → review_passed fail closed，不让旧 PASS 静默生效。"""
    gate, d = env
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_progress_build_target(build_target_id=10, to="smoke")
    _judge_pass(d, 10, "bundle_code_review", "sh1")
    assert gate.review_passed(build_target_id=10, review_kind="bundle_code_review", current_subject_hash="sh1") is True
    with d.transaction() as conn:   # 更新的畸形 payload（非 JSON）
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'judge','bundle_code_review','{broken')")
    assert gate.review_passed(build_target_id=10, review_kind="bundle_code_review", current_subject_hash="sh1") is False


def test_review_old_malformed_does_not_block_newer_valid_pass(env):
    """codex NIT 回归：**旧**畸形 decision 不阻断其后的有效 pass（fail-closed 只对「更新的」畸形；恢复语义 =
    畸形后重新评审产新有效 DECISION 即恢复通过）。"""
    gate, d = env
    gate.gate_start_build_target(build_target_id=10)
    gate.gate_progress_build_target(build_target_id=10, to="smoke")
    with d.transaction() as conn:   # 先插旧畸形
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'judge','bundle_code_review','{broken')")
    _judge_pass(d, 10, "bundle_code_review", "sh1")           # 其后有效 pass
    assert gate.review_passed(build_target_id=10, review_kind="bundle_code_review", current_subject_hash="sh1") is True


def test_import_target_start_moves_pool(env):
    """import 目标（CP5.5 物化）：start 连动同 build——占位 baseline+物化变体 → building。"""
    gate, d = env
    with d.transaction() as conn:   # 占位 baseline（import 语义：provenance=external_import 须 license allow）
        conn.execute("INSERT INTO baseline(id,slug,canonical_key,status,provenance,license_status) "
                     "VALUES (3,'imp','ck-imp','planned','external_import','allow')")
        conn.execute("INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (5,3,'imported','{}','planned')")
        conn.execute("INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
                     "VALUES (12,2,2,'import',3,'pending',3,5)")
        conn.execute("UPDATE build_target SET status='complete' WHERE id IN (10,11)")   # 让 12 成当前串行目标
    gate.gate_start_build_target(build_target_id=12)
    assert d.query_one("SELECT status FROM baseline WHERE id=3")[0] == "building"
    assert d.query_one("SELECT status FROM variant WHERE id=5")[0] == "building"


# ============ abandon ============
def test_abandon_cited_evaluation_rejected(env):
    gate, _ = env
    with pytest.raises(GateReject, match="valid evidence"):   # seed eval1 被 evidence1 引用
        gate.gate_abandon_evaluation(evaluation_id=1)


def test_abandon_uncited_evaluation_with_decision(env):
    gate, d = env
    _bt11_running(gate, d)
    r = gate.gate_start_attempt(cycle_id="c2", purpose="factory", build_target_id=11,
                                create={"variant_id": 2, "protocol_id": 1, "protocol_ver": 1,
                                        "eval_key": "e10", "source": "factory", "target_set_hash": "tsh"})
    gate.gate_finish_attempt(attempt_id=r["attempt_id"], status="failed", failure_kind="runtime")
    gate.gate_abandon_evaluation(evaluation_id=r["evaluation_id"], reason_md="不再需要")
    assert d.query_one("SELECT status FROM evaluation WHERE id=?", (r["evaluation_id"],))[0] == "abandoned"
    assert d.query_one("SELECT count(*) FROM decision WHERE type='abandon_evaluation'")[0] == 1


# ============ 拒绝审计 ============
def test_reject_writes_gate_decision(env):
    gate, d = env
    before = d.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0]
    with pytest.raises(GateReject):
        gate.gate_start_build_target(build_target_id=999)
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='gate' AND type='reject'")[0] == before + 1
