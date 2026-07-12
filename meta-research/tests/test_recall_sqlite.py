"""CP3.2 · SqliteRecall / SqliteCtx / 复用判定 selector（M2：§3.6.2 四级可停 + §4.1.5 O(1)）。

核心验收：**复用判定 O(1)**——命中走测量索引 (variant,protocol,ver)，EXPLAIN QUERY PLAN 证明用索引、非全表扫；
四级召回可停（卡片/变体矩阵/测量索引/ctx-fetch）。
"""
from __future__ import annotations

import pytest

import conftest
from orchestrator import database as db
from orchestrator import recall_sqlite as R
from orchestrator.execution_sandbox import sandbox_workload_environment_hash
from orchestrator.interfaces import RecallSpec


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


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    _seed(c)
    R.register_parser_suspect_stub(c)
    return c


# ============ 复用判定 O(1)（M2 核心）============
def test_reuse_hit(conn):
    r = R.reuse_selector(conn, variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])
    assert r["hit"] is True
    assert r["results"] == [{"metric_id": 1, "metric_ver": 1, "metric_result_id": 2, "value": 0.91}]


def test_reuse_miss_env_hash(conn):
    assert R.reuse_selector(conn, variant_id=2, protocol_id=1, protocol_ver=1, env_hash="wrong", required=[(1, 1)])["hit"] is False


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
        env_hash=runtime_hash, required=[(1, 1)])["hit"] is True
    assert R.reuse_selector(
        conn, variant_id=5, protocol_id=1, protocol_ver=1,
        env_hash=gpu_hash, required=[(1, 1)])["hit"] is False
    assert R.reuse_selector(
        conn, variant_id=6, protocol_id=1, protocol_ver=1,
        env_hash=runtime_hash, required=[(1, 1)])["hit"] is False
    assert R.reuse_selector(
        conn, variant_id=6, protocol_id=1, protocol_ver=1,
        env_hash=gpu_hash, required=[(1, 1)])["hit"] is True


def test_reuse_miss_required_metric_absent(conn):
    assert R.reuse_selector(conn, variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1), (9, 1)])["hit"] is False


def test_reuse_miss_non_success_evaluation(conn):
    assert R.reuse_selector(conn, variant_id=3, protocol_id=1, protocol_ver=1, env_hash="eh3", required=[(1, 1)])["hit"] is False


def test_reuse_miss_target_not_complete(conn):
    assert R.reuse_selector(conn, variant_id=4, protocol_id=1, protocol_ver=1, env_hash="eh4", required=[(1, 1)])["hit"] is False


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
        R.reuse_selector(c, variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])


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
    r = R.SqliteRecall(conn).level3_reuse(variant_id=2, protocol_id=1, protocol_ver=1, env_hash="eh1", required=[(1, 1)])
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
