"""CP2.1 · DB 层不变量否定用例（M1a：证明冻结 Appendix-A schema 焊死 I1–I6 + append-only + v2.3/v2.4 表约束）。

范围 = 只由 DDL 触发器 / CHECK / UNIQUE 保证的不变量（纯 SQL 可证伪，不经应用层 Gate）。
应用层判据（gate_close_question 的 target_complete / applicability 同版负向分支、authorizer 隔离拒读）
属 CP2.2。

`seed_minimal` 建一副最小合法图（含一条走完 I3 的 answered 问题——顺带正向验证 schema 可用），
各否定用例从中派生非法写入，断言被 DB 拒。
"""
from __future__ import annotations

import sqlite3

import pytest

# seed_minimal / seeded_conn 夹具在 conftest.py（CP2.1/CP2.2 共用）。


@pytest.fixture()
def conn(seeded_conn):
    """别名：本文件历史用 `conn`，实体是 conftest 的 seeded_conn。"""
    return seeded_conn


def _raises_abort(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def _raises_msg(conn, sql, msg_substr, params=()):
    """断言拒因消息含 msg_substr——用于「多守卫可能都拒同一写」的用例，钉死命中的是目标触发器。"""
    with pytest.raises(sqlite3.IntegrityError, match=msg_substr):
        conn.execute(sql, params)


# ============ append-only：UPDATE 被拒 ============
APPEND_ONLY_UPDATE = [
    "UPDATE goal SET text='x' WHERE id=1",
    "UPDATE protocol SET name='x' WHERE id=1",
    "UPDATE metric_def SET name='x' WHERE id=1",
    "UPDATE protocol_metric SET metric_ver=1 WHERE protocol_id=1",
    "UPDATE metric_result SET value=0.5 WHERE id=1",
    "UPDATE checkpoint SET path='y' WHERE id=1",
    "UPDATE answer SET answer_md='y' WHERE id=1",
    "UPDATE evidence SET claim_md='y' WHERE id=1",
    "UPDATE decision SET type='y' WHERE id=1",
    "UPDATE ledger SET money=1 WHERE id=1",
    "UPDATE phase_commit SET artifact_hash='y' WHERE id=1",
]


@pytest.mark.parametrize("sql", APPEND_ONLY_UPDATE)
def test_append_only_update_rejected(conn, sql):
    _raises_abort(conn, sql)


# ============ append-only：DELETE 被拒 ============
APPEND_ONLY_DELETE = [
    "DELETE FROM goal WHERE id=1",
    "DELETE FROM protocol WHERE id=1",
    "DELETE FROM metric_def WHERE id=1",
    "DELETE FROM protocol_metric WHERE protocol_id=1",
    "DELETE FROM metric_result WHERE id=1",
    "DELETE FROM checkpoint WHERE id=1",
    "DELETE FROM answer WHERE id=1",
    "DELETE FROM evidence WHERE id=1",
    "DELETE FROM decision WHERE id=1",
    "DELETE FROM ledger WHERE id=1",
    "DELETE FROM phase_commit WHERE id=1",
    "DELETE FROM evaluation WHERE id=1",
    "DELETE FROM evaluation_attempt WHERE id=1",
    "DELETE FROM run WHERE id=1",
]


@pytest.mark.parametrize("sql", APPEND_ONLY_DELETE)
def test_append_only_delete_rejected(conn, sql):
    _raises_abort(conn, sql)


# ============ I2：测量只落协议声明过的指标 ============
def test_i2_metric_not_in_protocol_rejected(conn):
    # metric_def(2,1) 存在但未进 protocol_metric → 该 evaluation 不得落它的测量
    conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (2,1,'f1','higher')")
    _raises_abort(conn,
        "INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) "
        "VALUES (1,1,2,1,0.5,'aggregate')")


def test_i2_metric_result_attempt_must_be_under_evaluation(conn):
    # 另建一个 evaluation+attempt（不同 variant 以避开一格子一 eval 唯一约束），把它的 attempt 挂到
    # evaluation 1 的 metric_result → 拒（attempt 不属该 evaluation）
    conn.executescript("""
      INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','planned');
      INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash)
        VALUES (2,2,1,1,'e2','standalone_eval','created',1,'h2');
      INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status)
        VALUES (2,2,1,1,'standalone_eval','success');
    """)
    _raises_abort(conn,
        "INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) "
        "VALUES (1,2,1,1,0.5,'aggregate')")


# ============ I3：关问须有效证据 ============
def test_i3_close_without_evidence_rejected(conn):
    conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                 "VALUES (2,1,1,1,'q2','active','agent')")
    _raises_abort(conn, "UPDATE question SET status='answered' WHERE id=2")


def test_i3_evidence_must_point_to_successful_measurement(conn):
    """新问题 + answer，但 evaluation 证据指向一个非成功 evaluation 的测量 → 拒。"""
    conn.executescript("""
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
        VALUES (2,1,1,1,'q2','active','agent');
      INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md)
        VALUES (2,2,1,1,1,'answered','a2');
      -- evaluation 3 保持 created（非 success），其 attempt success、有 metric_result（用 variant 2 避开一格子一 eval 唯一约束）
      INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','planned');
      INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash)
        VALUES (3,2,1,1,'e3','standalone_eval','created',1,'h3');
      INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status)
        VALUES (3,3,1,1,'standalone_eval','success');
      INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope)
        VALUES (3,3,3,1,1,0.7,'aggregate');
    """)
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,"
        "evaluation_id,evaluation_attempt_id,metric_result_id,metric_id,metric_ver,scope,claim_md) "
        "VALUES (2,2,1,1,'evaluation',3,3,3,1,1,'aggregate','x')")


def test_answer_goalver_must_match_question(conn):
    conn.executescript("""
      INSERT INTO goal(id,version,text,predicate_json,previous_version) VALUES (1,2,'g2','{}',1);
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
        VALUES (2,1,1,1,'q2','active','agent');
    """)
    # answer 用 goal_ver=2 但 question 在 goal_ver=1 → 拒
    _raises_abort(conn,
        "INSERT INTO answer(question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) "
        "VALUES (2,1,2,1,'answered','a')")


# ============ evaluation 身份冻结 / 不可回退 ============
def test_evaluation_identity_frozen(conn):
    _raises_abort(conn, "UPDATE evaluation SET target_set_hash='changed' WHERE id=1")


def test_evaluation_eval_key_frozen(conn):
    _raises_abort(conn, "UPDATE evaluation SET eval_key='changed' WHERE id=1")


def test_evaluation_success_no_rollback(conn):
    """success evaluation 不可回退到 failed（trg_eval_sc_upd）。用**未被证据引用**的 eval 隔离——
    seed 的 eval1 被 evidence 引用，改 failed 会先撞 trg_eval_no_abandon_if_cited（见
    test_cited_success_evaluation_cannot_be_abandoned）。"""
    conn.executescript("""
      INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','planned');
      INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash)
        VALUES (2,2,1,1,'e2','standalone_eval','created',1,'h2');
      INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status) VALUES (2,2,1,1,'standalone_eval','success');
      UPDATE evaluation SET status='success', canonical_attempt_id=2 WHERE id=2;
    """)
    _raises_msg(conn, "UPDATE evaluation SET status='failed', canonical_attempt_id=NULL WHERE id=2", "不可回退")


def test_cited_success_evaluation_cannot_be_abandoned(conn):
    _raises_abort(conn, "UPDATE evaluation SET status='abandoned', canonical_attempt_id=NULL WHERE id=1")


def test_attempt_terminal_frozen(conn):
    _raises_abort(conn, "UPDATE evaluation_attempt SET cost=1 WHERE id=1")   # OLD.status=success → 冻结


def test_run_terminal_frozen(conn):
    _raises_abort(conn, "UPDATE run SET cost=1 WHERE id=1")                  # OLD.status=success → 冻结


# ============ question 状态机 ============
def test_dead_end_requires_prune_decision(conn):
    conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                 "VALUES (2,1,1,1,'q2','active','agent')")
    _raises_abort(conn, "UPDATE question SET status='dead_end' WHERE id=2")


def test_closed_question_no_reopen(conn):
    _raises_abort(conn, "UPDATE question SET status='open' WHERE id=1")      # q1 已 answered


def test_question_initial_status_open_or_active(conn):
    _raises_abort(conn, "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                        "VALUES (9,1,1,1,'q9','answered','agent')")


def test_question_parent_frozen(conn):
    conn.execute("INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                 "VALUES (2,1,1,1,1,'q2','open','decompose')")
    _raises_abort(conn, "UPDATE question SET parent_id=NULL WHERE id=2")


# ============ 证据多态互斥 ============
def test_evidence_polymorphism_check(conn):
    # kind='literature' 却带 evaluation_id → 违反互斥 CHECK
    conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                 "VALUES (2,1,1,1,'q2','active','agent')")
    conn.execute("INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) "
                 "VALUES (2,2,1,1,1,'answered','a2')")
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,evaluation_id,literature_ref,claim_md) "
        "VALUES (2,2,1,1,'literature',1,'ref','x')")


# ============ I5：占坑互斥 ============
def test_i5_baseline_canonical_key_unique(conn):
    _raises_abort(conn, "INSERT INTO baseline(id,slug,canonical_key,status) VALUES (2,'b2','bk1','planned')")


def test_i5_variant_key_unique_per_baseline(conn):
    _raises_abort(conn, "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
                        "VALUES (2,1,'v1','{}','planned')")


# ============ 外部导入基线 license 一致性 ============
def test_external_baseline_requires_allow_license(conn):
    _raises_abort(conn, "INSERT INTO baseline(id,slug,canonical_key,status,provenance,license_status) "
                        "VALUES (2,'b2','bk2','planned','external_import','deny')")


# ============ I3 强化：命名旁路路径（§5.7 三触发器 + child_answer 子域） ============
def test_i3_closed_question_goalver_frozen(conn):
    """已关闭问题不可改 goal 版本（trg_q_closed_goalfrozen）。用 dead_end 隔离——
    answered/refuted 改 goal_ver 会同时撞 trg_q_i3（新版无证据），dead_end 不入 trg_q_i3
    （其 WHEN NEW.status IN answered/refuted）。"""
    conn.executescript("""
      INSERT INTO goal(id,version,text,predicate_json,previous_version) VALUES (1,2,'g2','{}',1);
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (2,1,1,1,'q2','active','agent');
      INSERT INTO decision(id,question_id,actor,type,payload_json) VALUES (2,2,'agent','prune_branch','{}');
      UPDATE question SET status='dead_end' WHERE id=2;
    """)
    _raises_msg(conn, "UPDATE question SET goal_ver=2 WHERE id=2", "不可改 goal 版本")


def _q2_with_answer(conn):
    """建 q2(active)+其 answer a2，供 child_answer 证据用例派生（a2 尚未落 evidence、q2 未关）。"""
    conn.executescript("""
      INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (2,1,1,1,'q2','active','agent');
      INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) VALUES (2,2,1,1,1,'answered','a2');
    """)


def test_i3_child_answer_out_of_subtree_rejected(conn):
    """child_answer 证据引用非本问题子树、且无 satisfied dep 的 answer → trg_evidence_child_scope 拒。"""
    _q2_with_answer(conn)   # q2 与 q1 无血缘；child_answer 指向 a1(q1 的 answer)
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,child_answer_id,claim_md) "
        "VALUES (2,2,1,1,'child_answer',1,'x')")


def test_i3_child_answer_same_question_rejected(conn):
    """child_answer 不能引用同一问题的 answer（trg_evidence_child 子检 1）。"""
    _q2_with_answer(conn)
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,child_answer_id,claim_md) "
        "VALUES (2,2,1,1,'child_answer',2,'x')")


def test_i3_child_answer_child_not_closed_rejected(conn):
    """child_answer 的子问题须已关闭（trg_evidence_child 子检 2）：指向 active 子问题的 answer → 拒。

    q3 设为 q2 的子问题（在子树内），令 trg_evidence_child_scope 放行、隔离出「子问题未关」判据；
    q3 保持 active 但先落一条 answer（DB 允许 active 问题有 answer 行），供证据引用。"""
    _q2_with_answer(conn)
    conn.executescript("""
      INSERT INTO question(id,parent_id,goal_id,goal_ver,born_goal_ver,text,status,source) VALUES (3,2,1,1,1,'q3','active','decompose');
      INSERT INTO answer(id,question_id,goal_id,goal_ver,cycle_id,verdict,answer_md) VALUES (3,3,1,1,1,'answered','a3');
    """)
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,child_answer_id,claim_md) "
        "VALUES (2,2,1,1,'child_answer',3,'x')")


# ============ 身份冻结 / 一致性（NIT-5：核心对象模型触发器） ============
def test_baseline_identity_frozen(conn):
    _raises_abort(conn, "UPDATE baseline SET canonical_key='changed' WHERE id=1")


def test_variant_identity_frozen(conn):
    _raises_abort(conn, "UPDATE variant SET config_json='{\"changed\":1}' WHERE id=1")


def test_i2_checkpoint_variant_mismatch_rejected(conn):
    """fold metric_result 的 checkpoint 与 evaluation 不同 variant → trg_mr_i2_ins 检 (c) 拒。"""
    conn.executescript("""
      INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','planned');
      INSERT INTO checkpoint(id,variant_id,ckpt_key,path,content_hash,hash_alg,artifact_type,origin)
        VALUES (2,2,'c2','/y','h2','sha256','algorithm','none');
    """)
    # evaluation 1 属 variant 1，checkpoint 2 属 variant 2 → 不同 variant
    _raises_abort(conn,
        "INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,checkpoint_id,metric_id,metric_ver,value,scope) "
        "VALUES (1,1,2,1,1,0.5,'fold')")


def test_evidence_question_must_match_answer(conn):
    """evidence.question_id 须等于 answer.question_id（trg_evidence_qa）。"""
    _q2_with_answer(conn)   # answer a2 属 q2；evidence 却挂 question_id=1
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,literature_ref,claim_md) "
        "VALUES (2,1,1,1,'literature','ref','x')")


def test_human_evidence_must_point_to_human_decision(conn):
    """human 证据须指向 actor=human 的 decision（trg_evidence_human）；seed decision 1 是 agent。"""
    _q2_with_answer(conn)
    _raises_abort(conn,
        "INSERT INTO evidence(answer_id,question_id,goal_id,goal_ver,kind,human_decision_id,claim_md) "
        "VALUES (2,2,1,1,'human',1,'x')")
