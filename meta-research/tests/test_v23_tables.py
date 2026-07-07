"""CP2.1 · v2.3/v2.4 表 DB 层否定用例（M1a：证明 log/import/interaction 表的 append-only /
provenance / license / 恰一 owner / 一 goal 一 pending 约束焊死在 DDL）。

隔离**行为**断言（这些表不产 target / 不入 gate / 不触发真通知）属 M1c（CP2.4）；本文件只证 DB 约束。
用 conftest.seeded_conn（含 goal1/cycle1/question1/run1/attempt1/baseline1）。
"""
from __future__ import annotations

import sqlite3

import pytest


def _abort(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def _candidate(conn, cid=1, source_kind="paper", revision=None):
    conn.execute(
        "INSERT INTO external_candidate(id,question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
        "need_summary,source_kind,canonical_uri,revision,search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
        "VALUES (?,1,1,'sota_reference','tsh','need',?, 'uri',?,'{}','ssh',0,'t')",
        (cid, source_kind, revision))


def _license(conn, lid=1, cid=1, decision="allow", scope="{}"):
    conn.execute("INSERT INTO license_review(id,candidate_id,decision,actor,license_scope_json) "
                 "VALUES (?,?,?,'auto',?)", (lid, cid, decision, scope))


def _imsg(conn, mid=1):
    conn.execute("INSERT INTO interaction_message(id,connector,raw_text,raw_hash,idempotency_key) "
                 "VALUES (?,'qq','hi','rh',?)", (mid, f"idem{mid}"))


def _ireq_pending(conn, rid=1, goal_id=1):
    conn.execute(
        "INSERT INTO interaction_request(id,goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (?,?,1,'idea','pending','s','[]','rh')", (rid, goal_id))


# ================= external_candidate =================
def test_extcand_append_only_update(seeded_conn):
    _candidate(seeded_conn)
    _abort(seeded_conn, "UPDATE external_candidate SET need_summary='x' WHERE id=1")


def test_extcand_no_delete(seeded_conn):
    _candidate(seeded_conn)
    _abort(seeded_conn, "DELETE FROM external_candidate WHERE id=1")


def test_extcand_repo_requires_revision(seeded_conn):
    # source_kind='repo' 无 revision → CHECK 拒（repo/model_hub 须 pinned）
    _abort(seeded_conn,
        "INSERT INTO external_candidate(id,question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
        "need_summary,source_kind,canonical_uri,search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
        "VALUES (1,1,1,'sota_reference','tsh','need','repo','uri','{}','ssh',0,'t')")


# ================= license_review =================
def test_licrev_allow_requires_scope(seeded_conn):
    _candidate(seeded_conn)
    _abort(seeded_conn, "INSERT INTO license_review(id,candidate_id,decision,actor) VALUES (1,1,'allow','auto')")


def test_licrev_append_only(seeded_conn):
    _candidate(seeded_conn)
    _license(seeded_conn)
    _abort(seeded_conn, "UPDATE license_review SET decision='deny' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM license_review WHERE id=1")


# ================= external_import =================
def test_import_imported_requires_allow_license(seeded_conn):
    """action='imported' 但引用的 license_review 是 deny → trg_extimport_license 拒。"""
    _candidate(seeded_conn)
    _license(seeded_conn, decision="deny", scope=None)
    _abort(seeded_conn,
        "INSERT INTO external_import(id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id,manifest_hash) "
        "VALUES (1,1,1,'imported',1,'csh','sk','ph','ldsh',1,1,'mh')")


def test_import_imported_requires_baseline_and_manifest(seeded_conn):
    """license 允许通过，但 imported 缺 baseline_id → CHECK 拒（隔离出 CHECK 而非 license 触发器）。"""
    _candidate(seeded_conn)
    _license(seeded_conn, decision="allow")
    _abort(seeded_conn,
        "INSERT INTO external_import(id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,manifest_hash) "
        "VALUES (1,1,1,'imported',1,'csh','sk','ph','ldsh',1,'mh')")


def test_import_selected_for_materialization_forbids_manifest(seeded_conn):
    _candidate(seeded_conn)
    _abort(seeded_conn,
        "INSERT INTO external_import(id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash,license_decision_snapshot_hash,baseline_id,manifest_hash) "
        "VALUES (1,1,1,'selected_for_materialization',1,'csh','sk','ph','ldsh',1,'mh')")


def test_import_action_cycle_not_before_discovery(seeded_conn):
    """imported 的 action_cycle 不得早于候选 discovered_cycle（trg_extimport_license 第二守卫，护 I6 溯源）。"""
    seeded_conn.execute("INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) VALUES (2,1,1,'idea','v0')")
    seeded_conn.execute(
        "INSERT INTO external_candidate(id,question_id,discovered_cycle,trigger_kind,trigger_snapshot_hash,"
        "need_summary,source_kind,canonical_uri,search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
        "VALUES (1,1,2,'sota_reference','tsh','need','paper','uri','{}','ssh',0,'t')")   # 发现于 cycle 2
    _license(seeded_conn, decision="allow")
    _abort(seeded_conn,   # action_cycle=1 < discovered_cycle=2
        "INSERT INTO external_import(id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id,manifest_hash) "
        "VALUES (1,1,1,'imported',1,'csh','sk','ph','ldsh',1,1,'mh')")


def test_import_append_only(seeded_conn):
    _candidate(seeded_conn)
    seeded_conn.execute(
        "INSERT INTO external_import(id,question_id,candidate_id,action,action_cycle,candidate_set_hash,"
        "selection_key,policy_hash) VALUES (1,1,1,'selected',1,'csh','sk','ph')")
    _abort(seeded_conn, "UPDATE external_import SET action='superseded' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM external_import WHERE id=1")


# ================= execution_log：恰一 owner =================
def test_execlog_exactly_one_owner_none(seeded_conn):
    # 两 owner 都空 → CHECK ((run_id IS NULL)!=(attempt IS NULL)) 拒
    _abort(seeded_conn,
        "INSERT INTO execution_log(cycle_id,log_kind,ref,content_hash) VALUES (1,'train','r','h')")


def test_execlog_exactly_one_owner_both(seeded_conn):
    # 两 owner 都非空 → 同一 CHECK 拒（双向约束）。log_kind='stderr' 对 run/attempt owner 均合法，
    # 故 trg_execlog_owner 放行、由 CHECK 兜底，干净证「恰一 owner」的另一半
    _abort(seeded_conn,
        "INSERT INTO execution_log(run_id,evaluation_attempt_id,cycle_id,log_kind,ref,content_hash) "
        "VALUES (1,1,1,'stderr','r','h')")


def test_execlog_owner_log_kind_consistency(seeded_conn):
    # run owner 但 log_kind='eval'（非 run 合法集）→ trg_execlog_owner 拒
    _abort(seeded_conn,
        "INSERT INTO execution_log(run_id,cycle_id,log_kind,ref,content_hash) VALUES (1,1,'eval','r','h')")


def test_execlog_append_only(seeded_conn):
    seeded_conn.execute(
        "INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash) VALUES (1,1,1,'train','r','h')")
    _abort(seeded_conn, "UPDATE execution_log SET ref='x' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM execution_log WHERE id=1")


# ================= execution_observation：codex 不写机器事实 =================
def _execlog(conn):
    conn.execute(
        "INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash) VALUES (1,1,1,'train','r','h')")


def test_execobs_codex_cannot_write_machine_facts(seeded_conn):
    _execlog(seeded_conn)
    # source='codex' 却写机器事实列 nan_seen → CHECK 拒（codex 只准 digest_ref+hash）
    _abort(seeded_conn,
        "INSERT INTO execution_observation(execution_log_id,source,nan_seen,digest_ref,digest_hash) "
        "VALUES (1,'codex',1,'dr','dh')")


def test_execobs_parser_cannot_write_digest(seeded_conn):
    _execlog(seeded_conn)
    _abort(seeded_conn,
        "INSERT INTO execution_observation(execution_log_id,source,last_loss,digest_ref) "
        "VALUES (1,'parser',0.1,'dr')")


def test_execobs_append_only(seeded_conn):
    _execlog(seeded_conn)
    seeded_conn.execute(
        "INSERT INTO execution_observation(id,execution_log_id,source,digest_ref,digest_hash) "
        "VALUES (1,1,'codex','dr','dh')")
    _abort(seeded_conn, "UPDATE execution_observation SET digest_ref='x' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM execution_observation WHERE id=1")


# ================= interaction_message / classification / reply =================
def test_imsg_append_only(seeded_conn):
    _imsg(seeded_conn)
    _abort(seeded_conn, "UPDATE interaction_message SET raw_text='x' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM interaction_message WHERE id=1")


def test_classification_intent_directive_id_check(seeded_conn):
    """CHECK((intent='directive')=(directive_id IS NOT NULL))：非 directive 却带 directive_id → 拒。

    反向触发（note+directive_id）干净隔离 CHECK——provenance 触发器只在 intent='directive' 时点火、
    挡不住本例。正向（directive 无 directive_id）会先撞 provenance 触发器，见
    test_classification_directive_provenance。"""
    _imsg(seeded_conn)
    seeded_conn.execute("INSERT INTO directive(id,kind,hardness,status,consume_at,payload_json) "
                        "VALUES (1,'note','soft','pending','immediate','{}')")
    _abort(seeded_conn,
        "INSERT INTO interaction_classification(message_id,intent,directive_id) VALUES (1,'note',1)")


def test_classification_directive_provenance(seeded_conn):
    """directive 不回指本 message（source_interaction_message_id≠message）→ trg_iclass_directive_prov 拒。"""
    _imsg(seeded_conn)
    seeded_conn.execute("INSERT INTO directive(id,kind,hardness,status,consume_at,payload_json) "
                        "VALUES (1,'note','soft','pending','immediate','{}')")   # 无 source_interaction_message_id
    _abort(seeded_conn,
        "INSERT INTO interaction_classification(message_id,intent,directive_id) VALUES (1,'directive',1)")


def test_reply_codex_requires_interaction_query_phase(seeded_conn):
    _imsg(seeded_conn)
    seeded_conn.execute("INSERT INTO runner_call(id,phase,purpose,status) VALUES (1,'idea','x','success')")
    _abort(seeded_conn,
        "INSERT INTO interaction_reply(message_id,reply_ref,reply_hash,responder_kind,runner_call_id) "
        "VALUES (1,'rr','rh','codex',1)")


def test_classification_append_only(seeded_conn):
    _imsg(seeded_conn)
    seeded_conn.execute("INSERT INTO interaction_classification(id,message_id,intent) VALUES (1,1,'note')")
    _abort(seeded_conn, "UPDATE interaction_classification SET intent='query' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM interaction_classification WHERE id=1")


def test_reply_append_only(seeded_conn):
    _imsg(seeded_conn)
    # template 应答无需 runner_call；建一条合法出站回复后证 append-only
    seeded_conn.execute("INSERT INTO interaction_reply(id,message_id,reply_ref,reply_hash,responder_kind) "
                        "VALUES (1,1,'rr','rh','template')")
    _abort(seeded_conn, "UPDATE interaction_reply SET reply_ref='x' WHERE id=1")
    _abort(seeded_conn, "DELETE FROM interaction_reply WHERE id=1")


# ================= interaction_request（v2.4） =================
def test_ireq_one_pending_per_goal(seeded_conn):
    _ireq_pending(seeded_conn, rid=1)
    _abort(seeded_conn,
        "INSERT INTO interaction_request(id,goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
        "VALUES (2,1,1,'idea','pending','s','[]','rh2')")


def test_ireq_terminal_requires_provenance(seeded_conn):
    # status='resolved' 但 resolved_message_id 缺 → CHECK((pending)=(resolved_message_id IS NULL)) 拒
    _abort(seeded_conn,
        "INSERT INTO interaction_request(id,goal_id,goal_ver,stage,status,summary_md,items_json,request_hash,"
        "resolution_json,resolved_at) VALUES (1,1,1,'idea','resolved','s','[]','rh','{}','t')")


def test_ireq_terminal_frozen(seeded_conn):
    _imsg(seeded_conn)
    seeded_conn.execute(
        "INSERT INTO interaction_request(id,goal_id,goal_ver,stage,status,summary_md,items_json,request_hash,"
        "resolution_json,resolved_at,resolved_message_id) VALUES (1,1,1,'idea','resolved','s','[]','rh','{}','t',1)")
    # 改 resolution_json（非身份列）→ 只 trg_ireq_terminal_frozen 可拒（identity_frozen 不管此列），隔离干净
    _abort(seeded_conn, "UPDATE interaction_request SET resolution_json='{\"x\":1}' WHERE id=1")


def test_ireq_identity_frozen_on_pending(seeded_conn):
    _ireq_pending(seeded_conn)
    # 改身份列（summary_md）而非一次性迁终态 → trg_ireq_identity_frozen 拒
    _abort(seeded_conn, "UPDATE interaction_request SET summary_md='x' WHERE id=1")


def test_ireq_no_delete(seeded_conn):
    _ireq_pending(seeded_conn)
    _abort(seeded_conn, "DELETE FROM interaction_request WHERE id=1")
