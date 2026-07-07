"""CP2.4 · M1–M3 隔离拒绝用例（M1c）：证 v2.3/v2.4 子系统（import / 人机）在假执行期不破 M0–M3 边界。

§7.1「M1–M3 隔离拒绝用例」：① 外部候选/选择不产 executable target、不入 baseline pool(legal)、不参与 evidence；
② 入站消息不触发真 responder、不改 route/decision/question；pending interaction_request 不触发真通知；
③ import 分支恒 deferred（占位 baseline(planned) + pending question_dep；真物化只 M4）。
"""
from __future__ import annotations

import pytest

import conftest
from orchestrator import database as db
from orchestrator.importer import DeferredImporter
from orchestrator.interaction import InteractionIngest
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon

TEST_POLICY = {
    "policy_version": "t",
    "tree_guard": {"max_decompose_depth": 3, "max_children_per_node": 3, "max_open_questions": 20},
    "question_guard": {"max_inconclusive_per_question": 2},
    "answer_review": {"max_reviews_per_cycle": 2},
    "goal_amend": {"max_spawn_from_goal_amend": 2, "max_closed_revalidate_per_cycle": 3},
}


@pytest.fixture()
def env():
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)                     # goal1/cycle1/question1(answered)/池对象…
    with daemon.transaction() as conn:                     # q2：open，作 import 触发问题
        conn.execute("INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
                     "VALUES (2,1,1,1,'q2','open','agent')")
    return {"daemon": daemon, "importer": DeferredImporter(daemon),
            "interaction": InteractionIngest(daemon), "state": SQLiteStateStore(daemon, TEST_POLICY)}


def _register_and_select(env, decision="allow"):
    imp, d = env["importer"], env["daemon"]
    cid = imp.register_candidate(question_id="q2", discovered_cycle="c1", trigger_kind="sota_reference",
                                 trigger_snapshot_hash="tsh", need_summary="need", source_kind="paper",
                                 canonical_uri="uri", search_snapshot_json="{}", search_snapshot_hash="ssh",
                                 rank=0, retrieved_at="t")
    lic = imp.review_license(candidate_id=cid, decision=decision,
                             license_scope_json="{}" if decision == "allow" else None)
    return cid, lic


# ============ ① / ③ import 恒 deferred + 不越界 ============
def test_import_deferred_three_writes(env):
    """select_deferred 三写入：占位 baseline(planned) + external_import(selected_for_materialization) + pending baseline dep。"""
    cid, lic = _register_and_select(env)
    r = env["importer"].select_deferred(
        question_id="q2", candidate_id=cid, license_review_id=lic, action_cycle="c1",
        candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
        license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-1")
    d = env["daemon"]
    assert d.query_one("SELECT status,provenance,license_status FROM baseline WHERE id=?", (r["baseline_id"],)) == ("planned", "external_import", "allow")
    assert d.query_one("SELECT action FROM external_import WHERE id=?", (r["external_import_id"],))[0] == "selected_for_materialization"
    assert d.query_one("SELECT dep_type,depends_on_baseline_id,status FROM question_dep WHERE id=?", (r["question_dep_id"],)) == ("baseline", r["baseline_id"], "pending")


def test_import_produces_no_executable_target_or_pool_entry(env):
    """① 隔离：import 不产可执行 target、占位 baseline 不入池(legal)、不产 evidence、无物化(imported)。"""
    cid, lic = _register_and_select(env)
    r = env["importer"].select_deferred(
        question_id="q2", candidate_id=cid, license_review_id=lic, action_cycle="c1",
        candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
        license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-1")
    d = env["daemon"]
    # import 不产**任何** target（占位 baseline 无 build_target；q2 也无 import 派生 target）——比只查 build/exec 更硬
    assert d.query_one("SELECT count(*) FROM build_target WHERE baseline_id=?", (r["baseline_id"],))[0] == 0
    assert d.query_one("SELECT count(*) FROM build_target WHERE question_id=2")[0] == 0
    # 占位 baseline 非 legal（不入池）
    assert d.query_one("SELECT count(*) FROM baseline WHERE id=? AND status='legal'", (r["baseline_id"],))[0] == 0
    # 无物化事件
    assert d.query_one("SELECT count(*) FROM external_import WHERE action='imported'")[0] == 0
    # import 不产 evidence（seed 里唯一 evidence 属 q1 的自建链，与 import 无关）
    assert d.query_one("SELECT count(*) FROM evidence WHERE question_id=2")[0] == 0
    # import 不写 decision 账（provenance 全在 external_import；与 interaction 同隔离纪律）
    assert d.query_one("SELECT count(*) FROM decision WHERE type='import_defer'")[0] == 0


def test_pending_baseline_dep_blocks_scheduling(env):
    """③ pending question_dep（占位 baseline 未 legal）使问题不可调度（§4.2.1）。"""
    cid, lic = _register_and_select(env)
    assert env["state"].is_schedulable("q2") is True       # 选择前 q2 可调度
    env["importer"].select_deferred(
        question_id="q2", candidate_id=cid, license_review_id=lic, action_cycle="c1",
        candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
        license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-1")
    assert env["state"].is_schedulable("q2") is False      # 选择后 pending baseline dep → 不可调度
    assert "q2" not in [q["question_id"] for q in env["state"].list_schedulable_questions()]


def test_license_deny_no_placeholder_baseline(env):
    """license deny 的候选不得 select_deferred（不产占位 baseline）；只走 rejected_by_license。"""
    cid, lic = _register_and_select(env, decision="deny")
    with pytest.raises(ValueError, match="decision=allow"):
        env["importer"].select_deferred(
            question_id="q2", candidate_id=cid, license_review_id=lic, action_cycle="c1",
            candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
            license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-1")
    env["importer"].reject_by_license(question_id="q2", candidate_id=cid, action_cycle="c1",
                                      candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
                                      license_review_id=lic)
    d = env["daemon"]
    assert d.query_one("SELECT count(*) FROM baseline WHERE provenance='external_import'")[0] == 0
    assert d.query_one("SELECT action FROM external_import ORDER BY id DESC LIMIT 1")[0] == "rejected_by_license"


def test_select_rejects_candidate_of_other_question(env):
    """候选须属本 question：q2 发现的候选不得为 q1 select（防错挂 provenance / 错阻别问题）。"""
    cid, lic = _register_and_select(env)   # 候选属 q2
    with pytest.raises(ValueError, match="不属于 question"):
        env["importer"].select_deferred(
            question_id="q1", candidate_id=cid, license_review_id=lic, action_cycle="c1",
            candidate_set_hash="csh", selection_key="sk", policy_hash="ph",
            license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-x")


def test_select_deferred_atomic_rollback(env):
    """三写入原子：**中段**（第②写 external_import）失败 → 第①写 baseline 也回滚（整批无残留）——
    比「第①写失败」更强地证同一事务。用 candidate_set_hash=None 令 ② 撞 NOT NULL（① 已成功后）。"""
    import sqlite3
    cid, lic = _register_and_select(env)
    d = env["daemon"]
    bl_before = d.query_one("SELECT count(*) FROM baseline")[0]
    with pytest.raises(sqlite3.IntegrityError):     # ② external_import.candidate_set_hash NOT NULL 违反
        env["importer"].select_deferred(
            question_id="q2", candidate_id=cid, license_review_id=lic, action_cycle="c1",
            candidate_set_hash=None, selection_key="sk", policy_hash="ph",
            license_decision_snapshot_hash="ldsh", placeholder_canonical_key="import-cand-mid")
    assert d.query_one("SELECT count(*) FROM baseline")[0] == bl_before   # ① baseline 随 ② 失败一起回滚
    assert d.query_one("SELECT count(*) FROM external_import")[0] == 0
    assert d.query_one("SELECT count(*) FROM question_dep WHERE dep_type='baseline'")[0] == 0


# ============ ② 人机入站不越界 ============
def test_inbound_durable_and_idempotent(env):
    ing, d = env["interaction"], env["daemon"]
    m1 = ing.inbound(connector="qq", raw_text="进展如何？", idempotency_key="k1", goal_id=1, goal_ver=1)
    assert d.query_one("SELECT raw_text FROM interaction_message WHERE id=?", (m1,))[0] == "进展如何？"
    assert ing.inbound(connector="qq", raw_text="进展如何？", idempotency_key="k1", goal_id=1, goal_ver=1) == m1   # 幂等


def test_inbound_and_ack_write_no_decision_no_state_change(env):
    """② 隔离铁律：入站/ACK 不写 decision（P1）、不改 question/cycle/route。"""
    d = env["daemon"]
    before_dec = d.query_one("SELECT count(*) FROM decision")[0]
    before_q = d.query_one("SELECT status FROM question WHERE id=2")[0]
    before_cyc = d.query_one("SELECT status,route FROM cycle WHERE id=1")
    m = env["interaction"].inbound(connector="qq", raw_text="继续", idempotency_key="k2", goal_id=1, goal_ver=1)
    env["interaction"].ack(message_id=m, reply_text="已在继续，当前快照 cycle 1")
    assert d.query_one("SELECT count(*) FROM decision")[0] == before_dec    # 无新 decision
    assert d.query_one("SELECT status FROM question WHERE id=2")[0] == before_q
    assert d.query_one("SELECT status,route FROM cycle WHERE id=1") == before_cyc
    assert d.query_one("SELECT count(*) FROM interaction_reply WHERE message_id=?", (m,))[0] == 1   # 回话入 interaction_* 审计流


def test_file_request_pending_isolated(env):
    """pending interaction_request 登记但不触发真通知/等待（M1–M3）、不改调度、不写 decision。"""
    ing, d = env["interaction"], env["daemon"]
    before_dec = d.query_one("SELECT count(*) FROM decision")[0]
    rid = ing.create_file_request(goal_id=1, goal_ver=1, stage="plan", summary_md="需数据集",
                                  items_json="[]", request_hash="rh1", question_id="q2")
    assert d.query_one("SELECT status FROM interaction_request WHERE id=?", (rid,))[0] == "pending"
    assert d.query_one("SELECT count(*) FROM decision")[0] == before_dec   # 文件请求不写 decision（P1）
    assert ing.create_file_request(goal_id=1, goal_ver=1, stage="plan", summary_md="需数据集",
                                   items_json="[]", request_hash="rh1") == rid   # 幂等 (goal,request_hash)
    # pending 请求单不改 q2 调度（q2 无 baseline dep 时仍可调度）
    assert env["state"].is_schedulable("q2") is True


def test_second_pending_file_request_per_goal_rejected(env):
    """同 goal 至多一 pending interaction_request（uq_ireq_one_pending，DDL 焊）。"""
    import sqlite3
    ing = env["interaction"]
    ing.create_file_request(goal_id=1, goal_ver=1, stage="plan", summary_md="A", items_json="[]", request_hash="rhA")
    with pytest.raises(sqlite3.IntegrityError):
        ing.create_file_request(goal_id=1, goal_ver=1, stage="plan", summary_md="B", items_json="[]", request_hash="rhB")


# ============ 输入校验（前缀化 id / both-or-neither，内审 SHOULD） ============
def test_typed_id_prefix_validated(env):
    """importer/interaction 的 id 解码校验前缀——类型错 id 干净拒（防静默命中别表同号行）。"""
    with pytest.raises(ValueError, match="前缀"):
        env["importer"].register_candidate(question_id="c9", discovered_cycle="c1", trigger_kind="stuck",
                                           trigger_snapshot_hash="t", need_summary="n", source_kind="paper",
                                           canonical_uri="u", search_snapshot_json="{}", search_snapshot_hash="s",
                                           rank=0, retrieved_at="t")   # 'c9' 处于 question 位
    with pytest.raises(ValueError, match="前缀"):
        env["interaction"].inbound(connector="qq", raw_text="x", idempotency_key="kx", cycle_id="q1")  # 'q1' 处于 cycle 位


def test_inbound_goal_both_or_neither(env):
    with pytest.raises(ValueError, match="goal_id 与 goal_ver"):
        env["interaction"].inbound(connector="qq", raw_text="x", idempotency_key="kg", goal_id=1)  # 缺 goal_ver
