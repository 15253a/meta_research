"""CP3.2 · SqliteRecall / SqliteCtx / 复用判定 selector（M2：§3.6.2 四级可停 + §4.1.5 O(1)）。

核心验收：**复用判定 O(1)**——命中走测量索引 (variant,protocol,ver)，EXPLAIN QUERY PLAN 证明用索引、非全表扫；
四级召回可停（卡片/变体矩阵/测量索引/ctx-fetch）。
"""
from __future__ import annotations

import json

import pytest

import conftest
from orchestrator import database as db
from orchestrator import recall_sqlite as R
from orchestrator import scientific_contract as SC
from orchestrator.execution_sandbox import sandbox_workload_environment_hash
from orchestrator.interfaces import RecallSpec
from orchestrator.runtime_mcp import RuntimeIngestService


def _seed(conn):
    conftest.seed_minimal(conn)
    conn.executescript("""
    -- variant2：可复用命中（success eval + success attempt(env eh1) + aggregate mr(metric 1,1)）
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (2,1,'v2','{}','legal');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash) VALUES (2,2,1,1,'e2','factory','created',1,'h2');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status,env_hash) VALUES (2,2,1,1,'factory','success','eh1');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (2,2,2,1,1,0.91,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=2 WHERE id=2;
    -- variant3：eval 非 success（created）→ 不复用
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (3,1,'v3','{}','legal');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,target_set_hash) VALUES (3,3,1,1,'e3','factory','created',1,'h3');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,purpose,status,env_hash) VALUES (3,3,1,1,'factory','success','eh3');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (3,3,3,1,1,0.7,'aggregate');
    -- variant4：来源 build_target 未 complete（running）→ 不复用
    INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (4,1,'v4','{}','legal');
    INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,eval_key,evaluation_source) VALUES (4,1,1,'eval',4,'running',4,'create_evaluation','e4','factory');
    INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,created_cycle,build_target_id,target_set_hash) VALUES (4,4,1,1,'e4','factory','created',1,4,'h4');
    INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status,env_hash) VALUES (4,4,1,4,1,'factory','success','eh4');
    INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,metric_ver,value,scope) VALUES (4,4,4,1,1,0.8,'aggregate');
    UPDATE evaluation SET status='success', canonical_attempt_id=4 WHERE id=4;
    -- 卡片（level1）+ execution_log（Ctx）
    INSERT INTO card(card_type,ref_id,card_md,src_hash) VALUES ('baseline',1,'标准注意力 baseline 卡：多头注意力','h');
    -- faceted tag：baseline1 挂 tag 'transformer'（card_md 不含此词 → 证 tag 独立命中，§3.6.2 第1级）
    INSERT INTO baseline_tag(baseline_id,tag) VALUES (1,'transformer');
    INSERT INTO execution_log(id,run_id,cycle_id,log_kind,ref,content_hash) VALUES (1,1,1,'train','path/train.log','loghash');
    """)
    conn.commit()


def _forge_native_code_review_without_guardian(
        conn, *, cycle_id: int, build_target_id: int,
        runtime_receipt_hash: bool = False):
    purpose = f"bundle-main-c{cycle_id}"
    runner_call_id = conn.execute(
        "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
        "VALUES (?,'bundle',?,'success')",
        (cycle_id, purpose)).lastrowid
    payload = {
        "protocol": "native-review-receipt-v1",
        "review_request_id": f"forged-request-{build_target_id}",
        "cycle_id": f"c{cycle_id}",
        "stage": "bundle",
        "target_id": str(build_target_id),
        "purpose": purpose,
        "review_kind": "bundle_code",
        "round_no": 1,
        "configured_rounds": 1,
        "reviewed_subject_hash": "sha256:" + "a" * 64,
        "resulting_subject_hash": "sha256:" + "b" * 64,
        "prior_receipt_hash": None,
        "runner_call_id": runner_call_id,
        "parent_thread_id": "forged-parent",
        "parent_turn_id": "forged-parent-turn",
        "child_call_id": "forged-child-call",
        "child_thread_id": "forged-child",
        "child_turn_id": "forged-child-turn",
        "verdict": "pass",
        "review_input_item_id": "forged-review-input",
        "review_input_brief_hash": "sha256:" + "c" * 64,
        "review_input_candidate_manifest_hash": "sha256:" + "d" * 64,
        "findings_ref": "/forged/findings.json",
        "findings_hash": "sha256:" + "e" * 64,
        "dispositions_ref": "/forged/dispositions.json",
        "disposition_hash": "sha256:" + "f" * 64,
        "revised_candidate_manifest_ref": "/forged/candidate.json",
        "revised_candidate_manifest_hash": "sha256:" + "0" * 64,
    }
    payload["receipt_hash"] = (
        RuntimeIngestService._receipt_hash(payload)
        if runtime_receipt_hash
        else "sha256:" + SC.canonical_hash(payload))
    decision_id = conn.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (?,'agent','runtime_review',?)",
        (cycle_id, json.dumps(payload, sort_keys=True))).lastrowid
    conn.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (?,'agent','runtime_stage_submission',?)",
        (cycle_id, json.dumps({
            "stage": "bundle",
            "target_id": str(build_target_id),
            "review_decision_id": decision_id,
            "artifact_hash": payload["resulting_subject_hash"],
        }, sort_keys=True)))
    conn.commit()
    return {
        "protocol": "native-review-receipt-v1",
        "decision_id": decision_id,
        "review_kind": "bundle_code",
        "review_scope": "code_plan_data_boundary",
        "subject_hash": payload["resulting_subject_hash"][7:],
        "receipt_hash": payload["receipt_hash"][7:],
    }


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    _seed(c)
    R.register_parser_suspect_stub(c)
    return c


# ============ 复用判定 O(1)（M2 核心）============
def test_reuse_hit(conn):
    r = R.reuse_selector(
        conn, variant_id=2, protocol_id=1, protocol_ver=1,
        env_hash="eh1", required=[(1, 1)],
        require_scientific_contract=False)
    assert r["hit"] is True
    assert r["results"] == [{"metric_id": 1, "metric_ver": 1, "metric_result_id": 2, "value": 0.91}]


def test_reuse_default_fails_closed_without_scientific_receipt(conn):
    assert R.reuse_selector(
        conn, variant_id=2, protocol_id=1, protocol_ver=1,
        env_hash="eh1", required=[(1, 1)])["hit"] is False


def test_recall_rejects_canonical_hash_native_review_forgery(
        conn):
    receipt = _forge_native_code_review_without_guardian(
        conn, cycle_id=1, build_target_id=2)

    assert R._scientific_review_receipt_valid(
        conn, build_target_id=2, receipt=receipt) is False


def test_recall_rejects_schema_valid_native_review_without_durable_child_proof(
        conn):
    receipt = _forge_native_code_review_without_guardian(
        conn, cycle_id=1, build_target_id=2,
        runtime_receipt_hash=True)

    assert R._scientific_review_receipt_valid(
        conn, build_target_id=2, receipt=receipt) is False


def test_reuse_miss_env_hash(conn):
    assert R.reuse_selector(
        conn, variant_id=2, protocol_id=1, protocol_ver=1,
        env_hash="wrong", required=[(1, 1)],
        require_scientific_contract=False)["hit"] is False


def test_cpu_and_gpu_workload_hashes_cannot_cross_reuse(conn):
    runtime_hash = "sha256:" + "a" * 64
    gpu_hash = sandbox_workload_environment_hash(runtime_hash, True)
    for identity, env_hash in ((5, runtime_hash), (6, gpu_hash)):
        conn.execute(
            "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
            "VALUES (?,1,?,'{}','legal')", (identity, f"v{identity}"))
        conn.execute(
            "INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,"
            "source,status,created_cycle,target_set_hash) "
            "VALUES (?,?,1,1,?,'factory','created',1,?)",
            (identity, identity, f"e{identity}", f"h{identity}"))
        conn.execute(
            "INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,attempt_no,"
            "purpose,status,env_hash) VALUES (?,?,1,1,'factory','success',?)",
            (identity, identity, env_hash))
        conn.execute(
            "INSERT INTO metric_result(id,evaluation_id,evaluation_attempt_id,metric_id,"
            "metric_ver,value,scope) VALUES (?,?,?,1,1,0.9,'aggregate')",
            (identity, identity, identity))
        conn.execute(
            "UPDATE evaluation SET status='success',canonical_attempt_id=? WHERE id=?",
            (identity, identity))
    assert R.reuse_selector(
        conn, variant_id=5, protocol_id=1, protocol_ver=1,
        env_hash=runtime_hash, required=[(1, 1)],
        require_scientific_contract=False)["hit"] is True
    assert R.reuse_selector(
        conn, variant_id=5, protocol_id=1, protocol_ver=1,
        env_hash=gpu_hash, required=[(1, 1)],
        require_scientific_contract=False)["hit"] is False
    assert R.reuse_selector(
        conn, variant_id=6, protocol_id=1, protocol_ver=1,
        env_hash=runtime_hash, required=[(1, 1)],
        require_scientific_contract=False)["hit"] is False
    assert R.reuse_selector(
        conn, variant_id=6, protocol_id=1, protocol_ver=1,
        env_hash=gpu_hash, required=[(1, 1)],
        require_scientific_contract=False)["hit"] is True


def test_reuse_miss_required_metric_absent(conn):
    assert R.reuse_selector(
        conn, variant_id=2, protocol_id=1, protocol_ver=1,
        env_hash="eh1", required=[(1, 1), (9, 1)],
        require_scientific_contract=False)["hit"] is False


def test_reuse_miss_non_success_evaluation(conn):
    assert R.reuse_selector(
        conn, variant_id=3, protocol_id=1, protocol_ver=1,
        env_hash="eh3", required=[(1, 1)],
        require_scientific_contract=False)["hit"] is False


def test_reuse_miss_target_not_complete(conn):
    assert R.reuse_selector(
        conn, variant_id=4, protocol_id=1, protocol_ver=1,
        env_hash="eh4", required=[(1, 1)],
        require_scientific_contract=False)["hit"] is False


def test_reuse_uses_measurement_index_not_full_scan(conn):
    """§7.1 M2：EXPLAIN QUERY PLAN 证明命中走测量索引 (variant,protocol,ver) + mr/ea 亦走索引、无全表扫。"""
    plan = R.explain_reuse(conn, variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])
    text = "\n".join(plan)
    assert "SEARCH e USING INDEX" in text and "variant_id=? AND protocol_id=? AND protocol_ver=?" in text
    assert "SEARCH mr USING INDEX uq_mr_agg" in text     # 承重的 metric_result 亦走部分唯一索引（O(1) 全链，内审 SHOULD）
    # 大表 evaluation/attempt/metric_result 别名 e/ea/mr 均未全表扫（仅 CTE r/ranked/subquery 允许 SCAN）
    for alias in ("e", "ea", "mr"):
        assert not any(l.strip() == f"SCAN {alias}" or l.strip().startswith(f"SCAN {alias} ") for l in plan)


def test_reuse_requires_suspect_stub_registered():
    """SHOULD 回归：连接未注册 parser_result_suspect → 可行动 RuntimeError（非裸 OperationalError）。"""
    c = db.connect(":memory:"); _seed(c)   # 故意不 register_parser_suspect_stub
    with pytest.raises(RuntimeError, match="register_parser_suspect_stub"):
        R.reuse_selector(
            c, variant_id=2, protocol_id=1, protocol_ver=1,
            env_hash="eh1", required=[(1, 1)],
            require_scientific_contract=False)


# ============ 渐进四级召回（可停）============
def test_recall_level1_cards(conn):
    hits = R.SqliteRecall(conn).level1_cards("注意力", k=5)
    assert len(hits) == 1 and hits[0].ref == "card:baseline:1" and "多头注意力" in hits[0].card_md


def test_recall_level1_no_match(conn):
    assert R.SqliteRecall(conn).level1_cards("不存在的关键词", k=5) == []


def test_recall_level1_faceted_tag(conn):
    """§3.6.2 第1级 faceted tag：baseline1 的 card_md 不含 'transformer'，但其 baseline_tag 命中 → 仍召回该卡。"""
    md = R.SqliteRecall(conn).level1_cards("transformer", k=5)[0].card_md
    assert "多头注意力" in md and "transformer" not in md   # 命中来自 tag，非 card_md 文本


def test_recall_level2_variants(conn):
    hits = R.SqliteRecall(conn).level2_variants(baseline_id=1)
    assert {h.ref for h in hits} >= {"variant:1", "variant:2", "variant:3", "variant:4"}   # baseline1 下全变体


def test_recall_level3_reuse(conn):
    r = R.SqliteRecall(
        conn, require_scientific_contract=False).level3_reuse(
            variant_id=2, protocol_id=1, protocol_ver=1,
            env_hash="eh1", required=[(1, 1)])
    assert r["hit"] is True


def test_recall_query_entry_level1(conn):
    hits = R.SqliteRecall(conn).query(RecallSpec(query="注意力", stage="plan", k=3))
    assert len(hits) == 1


# ============ ctx-fetch 深潜（第4级）============
def test_ctx_fetch_execlog(conn):
    out = R.SqliteCtx(conn).fetch("execlog:1")
    assert "path/train.log" in out and "loghash" in out


def test_ctx_fetch_unknown_ref_honest(conn):
    assert "未知 ref" in R.SqliteCtx(conn).fetch("bogus:1")
    assert "不存在" in R.SqliteCtx(conn).fetch("execlog:999")
