"""CP5.2 · PoolGate 注册/评审 gates（§4.1.4 池注册家族·注册侧，M4）+ subject manifest 确定性。

文件库（门禁 mode=ro 独立连接）。核心链：claim → build 生命周期（CP5.1）→ result_review →
register_evaluation（§4.2.5(ii) 单事务）→ register_baseline（→legal 入池）→ finish complete。
"""
from __future__ import annotations

import json

import pytest

import conftest
from orchestrator import database as db
from orchestrator import subject_manifest as SM
from orchestrator.gate_pool import PoolGate
from orchestrator.gate_sqlite import GateReject, open_gate_read_conn
from orchestrator.writedaemon import WriteDaemon


def _seed(conn):
    conftest.seed_minimal(conn)
    conn.executescript("""
    INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) VALUES (2,1,1,'bundle','attack','v0');
    """)
    conn.commit()


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "research.sqlite")
    seed = db.connect(path); _seed(seed); seed.close()
    daemon = WriteDaemon(db.connect(path))
    gate = PoolGate(daemon, open_gate_read_conn(path))
    return gate, daemon


def _judge_pass(daemon, bt_id, kind, subject_hash):
    with daemon.transaction() as conn:
        rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (2,'audit',?,'success')",
                          (kind,)).lastrowid
        conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (2,'judge',?,?)",
                     (kind, json.dumps({"build_target_id": bt_id, "review_kind": kind, "round_no": 1,
                                        "verdict": "pass", "subject_hash": subject_hash,
                                        "runner_call_id": rc, "policy_hash": "ph"})))


# ============ subject manifest 确定性 ============
def test_subject_hash_deterministic_and_order_free(env):
    e1 = [{"kind": "config", "ref": "a.yaml", "content_hash": "h1"},
          {"kind": "code_diff", "ref": "worktree_diff", "content_hash": "h2"}]
    assert SM.subject_hash(e1) == SM.subject_hash(list(reversed(e1)))   # 条目序无关（canonical 排序）
    e2 = [dict(e1[0], content_hash="DIFF"), e1[1]]
    assert SM.subject_hash(e1) != SM.subject_hash(e2)                   # 任一 hash 变 → subject_hash 变


def test_subject_manifest_recipes(env):
    code = SM.code_review_manifest(plan_slice_hash="p", code_diff_hash="d", config_hashes={"c.yaml": "ch"},
                                   identity_draft_hash="i", smoke_transcript_ref="s.log", smoke_transcript_hash="sh")
    assert {e["kind"] for e in code} == {"plan_slice", "code_diff", "config", "identity_draft", "smoke_transcript"}
    res = SM.result_review_manifest(metrics_artifact_hash="m", checkpoint_hashes={"final": "ck"},
                                    run_log_hashes={"train.log": "tl"}, parser_obs_hash="po")
    assert {e["kind"] for e in res} == {"metrics_artifact", "checkpoint", "run_log", "parser_observation"}
    assert SM.subject_hash(code) != SM.subject_hash(res)


def test_subject_hash_missing_key_raises(env):
    with pytest.raises(ValueError, match="缺键"):
        SM.subject_hash([{"kind": "config", "ref": "a"}])   # 缺 content_hash


# ============ claim ============
def test_claim_baseline_and_duplicate_key(env):
    gate, d = env
    r = gate.gate_claim_baseline(canonical_key="ck-new", slug="nb", cycle_id="c2", identity_draft_md="# 草稿")
    assert d.query_one("SELECT status,canonical_key FROM baseline WHERE id=?", (r["baseline_id"],)) == ("planned", "ck-new")
    assert d.query_one("SELECT status,baseline_id FROM variant WHERE id=?", (r["variant_id"],)) == ("planned", r["baseline_id"])
    with pytest.raises(GateReject, match="I5"):
        gate.gate_claim_baseline(canonical_key="ck-new", slug="x", cycle_id="c2", identity_draft_md="y")
    with pytest.raises(GateReject, match="identity"):
        gate.gate_claim_baseline(canonical_key="ck2", slug="x", cycle_id="c2", identity_draft_md="  ")


def test_claim_variant_requires_legal_baseline(env):
    gate, d = env
    with pytest.raises(GateReject, match="legal baseline"):   # baseline1 是 planned（seed）
        gate.gate_claim_variant(baseline_id=1, variant_key="vx", config_json='{"lr":1}', cycle_id="c2", seq=50)
    with d.transaction() as conn:
        conn.execute("UPDATE baseline SET status='legal' WHERE id=1")
    r = gate.gate_claim_variant(baseline_id=1, variant_key="vx", config_json='{"lr":1}', cycle_id="c2", seq=50)
    assert d.query_one("SELECT target_kind,status,variant_id FROM build_target WHERE id=?",
                       (r["build_target_id"],)) == ("exec", "pending", r["variant_id"])
    with pytest.raises(GateReject, match="variant_key 已占"):
        gate.gate_claim_variant(baseline_id=1, variant_key="vx", config_json='{"x":1}', cycle_id="c2", seq=51)
    with pytest.raises(GateReject, match="config_json 空"):
        gate.gate_claim_variant(baseline_id=1, variant_key="vy", config_json="{}", cycle_id="c2", seq=52)


# ============ 全链：claim → build 生命周期 → 双评审 → register_evaluation → register_baseline → complete ============
def _build_chain(gate, d):
    """走到「run success + target running + 结果评审 pass」的自建 baseline 现场。返回 ids。"""
    r = gate.gate_claim_baseline(canonical_key="ck-b", slug="b", cycle_id="c2", identity_draft_md="# id 草稿")
    bid, vid = r["baseline_id"], r["variant_id"]
    with d.transaction() as conn:   # plan 落 build 目标 + required metric（(1,1) 已在 seed protocol 声明）
        bt = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
                          "VALUES (2,1,'build',1,'pending',?,?)", (bid, vid)).lastrowid
        conn.execute("INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) VALUES (?,1,1)", (bt,))
    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    _judge_pass(d, bt, "bundle_code_review", "code-sh")
    gate.gate_progress_build_target(build_target_id=bt, to="running", current_subject_hash="code-sh")
    rid = gate.gate_start_run(build_target_id=bt, cycle_id="c2", variant_id=vid, kind="build", env_hash="eh")
    with d.transaction() as conn:
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (?,'final','/p','ckh','sha256',?)", (vid, rid))
    gate.gate_finish_run(run_id=rid, status="success", cost=1.0)
    _judge_pass(d, bt, "bundle_result_review", "res-sh")
    return {"baseline_id": bid, "variant_id": vid, "bt": bt, "run": rid}


def test_full_registration_chain(env):
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"}, env_hash="eh")
    erow = d.query_one("SELECT status, canonical_attempt_id FROM evaluation WHERE id=?", (reg["evaluation_id"],))
    assert erow == ("success", reg["attempt_id"])                      # §4.2.5(ii) 单事务：eval+attempt(success)+mr
    gate.gate_register_baseline(baseline_id=ids["baseline_id"], variant_id=ids["variant_id"],
                                build_target_id=ids["bt"], evaluation_id=reg["evaluation_id"],
                                cycle_id="c2", current_subject_hash="res-sh",
                                identity_doc="# identity", repro_cmd="python train.py", run_id=ids["run"])
    assert d.query_one("SELECT status FROM baseline WHERE id=?", (ids["baseline_id"],))[0] == "legal"   # 入池
    assert d.query_one("SELECT status FROM variant WHERE id=?", (ids["variant_id"],))[0] == "legal"
    gate.gate_finish_build_target(build_target_id=ids["bt"], status="complete")   # CP5.1 complete 前置现可满足
    assert d.query_one("SELECT status FROM build_target WHERE id=?", (ids["bt"],))[0] == "complete"


def test_register_evaluation_requires_result_review(env):
    gate, d = env
    ids = _build_chain(gate, d)
    with pytest.raises(GateReject, match="结果评审"):
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="WRONG",
            metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}],
            create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                    "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})


def test_register_evaluation_required_coverage_and_i2(env):
    gate, d = env
    ids = _build_chain(gate, d)
    base = dict(cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
                create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                        "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="required"):
        gate.gate_register_evaluation(metric_results=[], **base)
    with pytest.raises(GateReject, match="I2"):
        gate.gate_register_evaluation(metric_results=[{"metric_id": 9, "metric_ver": 1, "value": 0.1}], **base)


def test_register_baseline_negative_paths(env):
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    args = dict(baseline_id=ids["baseline_id"], variant_id=ids["variant_id"], build_target_id=ids["bt"],
                evaluation_id=reg["evaluation_id"], cycle_id="c2", current_subject_hash="res-sh",
                identity_doc="# identity", repro_cmd="python train.py", run_id=ids["run"])
    with pytest.raises(GateReject, match="identity/复现命令"):
        gate.gate_register_baseline(**{**args, "identity_doc": " "})
    with pytest.raises(GateReject, match="结果评审"):
        gate.gate_register_baseline(**{**args, "current_subject_hash": "DRIFT"})
    with pytest.raises(GateReject, match="非 success"):   # run 换成不存在→缺失拒
        gate.gate_register_baseline(**{**args, "run_id": 999})
    with pytest.raises(GateReject, match="evaluation 属 variant"):   # seed eval1 属 variant1
        gate.gate_register_baseline(**{**args, "evaluation_id": 1})


def _register_full_baseline(gate, d):
    """跑完 baseline 注册全链（复用 test_full_registration_chain 的路径），返回 ids+eval。"""
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    gate.gate_register_baseline(baseline_id=ids["baseline_id"], variant_id=ids["variant_id"],
                                build_target_id=ids["bt"], evaluation_id=reg["evaluation_id"],
                                cycle_id="c2", current_subject_hash="res-sh",
                                identity_doc="# identity", repro_cmd="python train.py", run_id=ids["run"])
    gate.gate_finish_build_target(build_target_id=ids["bt"], status="complete")
    return {**ids, "evaluation_id": reg["evaluation_id"]}


def test_register_variant_full_exec_chain(env):
    """**真 exec 链**（codex BLOCKER 修后：register_variant 须 exec 目标）：baseline legal 后 claim_variant
    （产 exec 目标）→ 生命周期 → 双评审 → register_evaluation → register_variant → 仅 variant legal。"""
    gate, d = env
    base = _register_full_baseline(gate, d)
    cl = gate.gate_claim_variant(baseline_id=base["baseline_id"], variant_key="v-exec",
                                 config_json='{"lr":0.01}', cycle_id="c2", seq=10)
    vid, bt = cl["variant_id"], cl["build_target_id"]
    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    _judge_pass(d, bt, "bundle_code_review", "code-sh2")
    gate.gate_progress_build_target(build_target_id=bt, to="running", current_subject_hash="code-sh2")
    rid = gate.gate_start_run(build_target_id=bt, cycle_id="c2", variant_id=vid, kind="exec", env_hash="eh2")
    with d.transaction() as conn:
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (?,'final','/p2','ckh2','sha256',?)", (vid, rid))
    gate.gate_finish_run(run_id=rid, status="success")
    _judge_pass(d, bt, "bundle_result_review", "res-sh2")
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=bt, purpose="factory", current_subject_hash="res-sh2",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.93}],
        create={"variant_id": vid, "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac2", "source": "factory", "target_set_hash": "tsh2"})
    before_bl = d.query_one("SELECT status FROM baseline WHERE id=?", (base["baseline_id"],))[0]
    gate.gate_register_variant(variant_id=vid, build_target_id=bt, evaluation_id=reg["evaluation_id"],
                               cycle_id="c2", current_subject_hash="res-sh2", run_id=rid)
    assert d.query_one("SELECT status FROM variant WHERE id=?", (vid,))[0] == "legal"
    assert d.query_one("SELECT status FROM baseline WHERE id=?", (base["baseline_id"],))[0] == before_bl   # baseline 不动
    gate.gate_finish_build_target(build_target_id=bt, status="complete")


def test_register_variant_rejects_build_target_kind(env):
    """codex BLOCKER 回归：register_variant 须 exec 目标——拿 build 目标注册 → 拒（防评审/target 错配入池）。"""
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="须 exec"):
        gate.gate_register_variant(variant_id=ids["variant_id"], build_target_id=ids["bt"],
                                   evaluation_id=reg["evaluation_id"], cycle_id="c2",
                                   current_subject_hash="res-sh", run_id=ids["run"])


def test_register_evaluation_rejects_null_variant_target(env):
    """codex 第2轮 BLOCKER 回归：build/exec 目标 variant_id=NULL **不作通配**——评审过了也拒（未绑=非法态，
    防 NULL 目标成为任意 variant 的入池跳板）。"""
    gate, d = env
    with d.transaction() as conn:   # variant_id=NULL 的 build 目标 + 直推 running + 评审 pass
        btn = conn.execute("INSERT INTO build_target(cycle_id,target_kind,seq,status) "
                           "VALUES (2,'build',70,'running')").lastrowid
        vb = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) "
                          "VALUES (1,'v-null','{}','planned')").lastrowid
    _judge_pass(d, btn, "bundle_result_review", "sh-null")
    with pytest.raises(GateReject, match="NULL=未绑"):
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=btn, purpose="factory", current_subject_hash="sh-null",
            metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}],
            create={"variant_id": vb, "protocol_id": 1, "protocol_ver": 1,
                    "eval_key": "nul", "source": "factory", "target_set_hash": "t"})


def test_register_evaluation_rejects_cross_variant_target(env):
    """codex BLOCKER 回归：不许拿 variant A 的 target/评审注册 variant B 的测量（create 侧绑定核）。"""
    gate, d = env
    ids = _build_chain(gate, d)
    with d.transaction() as conn:   # 另一变体 B（空格子，绕开一格子一 eval 的先行拒，直击绑定核）
        vb = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) "
                          "VALUES (1,'v-cross','{}','planned')").lastrowid
    with pytest.raises(GateReject, match="target 绑定不符"):
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
            metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}],
            create={"variant_id": vb, "protocol_id": 1, "protocol_ver": 1,
                    "eval_key": "cross", "source": "factory", "target_set_hash": "t"})


def test_register_evaluation_append_mode(env):
    """append 模式：绑定核（target 不符拒）+ 已 success 的 evaluation 追加保留原 canonical + abandoned 拒。"""
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    eid, first_aid = reg["evaluation_id"], reg["attempt_id"]
    with d.transaction() as conn:   # 另一 eval 目标（append 型须带 evaluation_id，DDL CHECK）
        bt2 = conn.execute("INSERT INTO build_target(cycle_id,target_kind,seq,status,variant_id,eval_action,evaluation_id) "
                           "VALUES (2,'eval',90,'running',?,'append_attempt',?)", (ids["variant_id"], eid)).lastrowid
    _judge_pass(d, bt2, "bundle_result_review", "res-sh2")
    with pytest.raises(GateReject, match="target 绑定不符"):    # eval 绑 ids["bt"]，传别的 target
        gate.gate_register_evaluation(cycle_id="c2", build_target_id=bt2, purpose="metric_append",
                                      current_subject_hash="res-sh2", metric_results=[], evaluation_id=eid)
    r2 = gate.gate_register_evaluation(   # 合法 append（同 target）：保留原 canonical
        cycle_id="c2", build_target_id=ids["bt"], purpose="metric_append", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.92}], evaluation_id=eid)
    assert d.query_one("SELECT canonical_attempt_id FROM evaluation WHERE id=?", (eid,))[0] == first_aid
    assert r2["attempt_id"] != first_aid
    with d.transaction() as conn:   # abandoned 拒（先造一个可弃 eval：未被引用、非 legal 变体出厂）
        conn.execute("UPDATE evaluation SET status='abandoned', canonical_attempt_id=NULL WHERE id=?", (eid,))
    with pytest.raises(GateReject, match="abandoned"):
        gate.gate_register_evaluation(cycle_id="c2", build_target_id=ids["bt"], purpose="metric_append",
                                      current_subject_hash="res-sh", metric_results=[], evaluation_id=eid)


def test_register_evaluation_create_cell_collision(env):
    """create 撞一格子一 evaluation（I6）→ 拒（seed eval1 已占 (v1,p1@1)）。"""
    gate, d = env
    ids = _build_chain(gate, d)
    with pytest.raises(GateReject, match="一格子一"):
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
            metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}],
            create={"variant_id": 1, "protocol_id": 1, "protocol_ver": 1,
                    "eval_key": "x", "source": "factory", "target_set_hash": "tsh"})


def test_register_baseline_rejects_non_factory_and_foreign_variant(env):
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(   # source=standalone_eval（非 factory）
        cycle_id="c2", build_target_id=ids["bt"], purpose="standalone_eval", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "sa", "source": "standalone_eval", "target_set_hash": "tsh"})
    args = dict(baseline_id=ids["baseline_id"], variant_id=ids["variant_id"], build_target_id=ids["bt"],
                evaluation_id=reg["evaluation_id"], cycle_id="c2", current_subject_hash="res-sh",
                identity_doc="# id", repro_cmd="cmd", run_id=ids["run"])
    with pytest.raises(GateReject, match="factory"):
        gate.gate_register_baseline(**args)
    with pytest.raises(GateReject, match="不属 baseline"):     # variant1 属 baseline1，非本 claim baseline
        gate.gate_register_baseline(**{**args, "variant_id": 1})


def test_register_requires_run_id_when_run_produced_checkpoint(env):
    """内审 SHOULD 回归（防御）：variant 有 run_produced checkpoint 却不给 run_id → 拒（不得跳过 run success 核）。"""
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    with pytest.raises(GateReject, match="须给 run_id"):
        gate.gate_register_baseline(baseline_id=ids["baseline_id"], variant_id=ids["variant_id"],
                                    build_target_id=ids["bt"], evaluation_id=reg["evaluation_id"],
                                    cycle_id="c2", current_subject_hash="res-sh",
                                    identity_doc="# id", repro_cmd="cmd", run_id=None)


def test_claim_variant_seq_collision_clean_reject(env):
    """内审 SHOULD 回归：UNIQUE(cycle,seq) 撞 → 干净 GateReject（非裸 IntegrityError）。"""
    gate, d = env
    with d.transaction() as conn:
        conn.execute("UPDATE baseline SET status='legal' WHERE id=1")
        conn.execute("INSERT INTO build_target(cycle_id,target_kind,seq,status,variant_id) VALUES (2,'build',60,'pending',1)")
    with pytest.raises(GateReject, match="约束拒绝"):
        gate.gate_claim_variant(baseline_id=1, variant_key="vz", config_json='{"a":1}', cycle_id="c2", seq=60)


def test_result_manifest_requires_checkpoint(env):
    """内审 NIT 回归：result_review manifest 空 checkpoint 集 → ValueError（checkpoint 须在防篡面）。"""
    with pytest.raises(ValueError, match="checkpoint"):
        SM.result_review_manifest(metrics_artifact_hash="m", checkpoint_hashes={},
                                  run_log_hashes={}, parser_obs_hash="po")


# ============ gate_new_protocol（I1）============
def test_new_protocol_version_and_i1(env):
    gate, d = env
    with pytest.raises(GateReject, match="I1"):   # (1,1) 已在 seed
        gate.gate_new_protocol(protocol_id=1, version=1, name="p", scope_spec_json="{}", cycle_id="c2")
    gate.gate_new_protocol(protocol_id=1, version=2, name="proto-v2", scope_spec_json='{"scope":2}', cycle_id="c2",
                           metric_defs=[{"id": 2, "version": 1, "name": "f1", "direction": "higher"}],
                           metrics=[(1, 1), (2, 1)])
    assert d.query_one("SELECT name FROM protocol WHERE id=1 AND version=2")[0] == "proto-v2"
    assert d.query_one("SELECT count(*) FROM protocol_metric WHERE protocol_id=1 AND protocol_ver=2")[0] == 2
    with pytest.raises(GateReject, match="口径不同"):   # metric_def (1,1)=('acc','higher')（seed）
        gate.gate_new_protocol(protocol_id=1, version=3, name="p3", scope_spec_json="{}", cycle_id="c2",
                               metric_defs=[{"id": 1, "version": 1, "name": "acc", "direction": "lower"}])
    with pytest.raises(GateReject, match="口径不同"):   # codex BLOCKER 回归：unit/compute_spec 亦入口径比较
        gate.gate_new_protocol(protocol_id=1, version=3, name="p3", scope_spec_json="{}", cycle_id="c2",
                               metric_defs=[{"id": 1, "version": 1, "name": "acc", "direction": "higher",
                                             "unit": "percent"}])
    with pytest.raises(GateReject, match="批内重复"):
        gate.gate_new_protocol(protocol_id=1, version=3, name="p3", scope_spec_json="{}", cycle_id="c2",
                               metric_defs=[{"id": 5, "version": 1, "name": "x", "direction": "higher"},
                                            {"id": 5, "version": 1, "name": "x", "direction": "higher"}])
    with pytest.raises(GateReject, match="不存在的 metric_def"):
        gate.gate_new_protocol(protocol_id=1, version=3, name="p3", scope_spec_json="{}", cycle_id="c2",
                               metrics=[(99, 1)])
