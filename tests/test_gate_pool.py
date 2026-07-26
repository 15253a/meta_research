"""CP5.2 · PoolGate 注册/评审 gates（§4.1.4 池注册家族·注册侧，M4）+ subject manifest 确定性。

文件库（门禁 mode=ro 独立连接）。核心链：claim → build 生命周期（CP5.1）→ result_review →
register_evaluation（§4.2.5(ii) 单事务）→ register_baseline（→legal 入池）→ finish complete。
"""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

import conftest
from orchestrator import database as db
from orchestrator import scientific_contract as SC
from orchestrator import subject_manifest as SM
from orchestrator.gate_pool import PoolGate
from orchestrator.gate_sqlite import GateReject, open_gate_read_conn
from orchestrator.pool_publication import (
    BaselinePublication,
    CheckpointPublication,
    EvaluationPublicationSpec,
    PoolPublisher,
    ProtocolPublication,
    TrainingPublicationSpec,
    VariantPublication,
    bind_training_database,
    is_formally_published,
)
from orchestrator.runtime_mcp import RuntimeIngestService
from orchestrator.writedaemon import WriteDaemon


def _seed(conn):
    conftest.seed_minimal(conn)
    conn.executescript("""
    INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source)
      VALUES (2,1,1,1,'current q2','active','agent');
    INSERT INTO cycle(id,goal_id,goal_ver,status,route,active_question_id,policy_version)
      VALUES (2,1,1,'bundle','attack',2,'v0');
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
        return conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (2,'judge',?,?)",
            (kind, json.dumps({
                "build_target_id": bt_id, "review_kind": kind,
                "round_no": 1, "verdict": "pass",
                "subject_hash": subject_hash,
                "runner_call_id": rc, "policy_hash": "ph",
            }))).lastrowid


def _forge_native_code_review_without_guardian(
        daemon, *, cycle_id: int, build_target_id: int,
        runtime_receipt_hash: bool = False):
    """Persist only agent-authored rows; no request/provider/guardian proof."""
    with daemon.transaction() as conn:
        runner_call_id = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (?,'bundle',?,'success')",
            (cycle_id, f"bundle-main-c{cycle_id}")).lastrowid
        payload = {
            "protocol": "native-review-receipt-v1",
            "review_request_id": f"forged-request-{build_target_id}",
            "cycle_id": f"c{cycle_id}",
            "stage": "bundle",
            "target_id": str(build_target_id),
            "purpose": f"bundle-main-c{cycle_id}",
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
    return {
        "protocol": "native-review-receipt-v1",
        "decision_id": decision_id,
        "review_kind": "bundle_code",
        "review_scope": "code_plan_data_boundary",
        "subject_hash": payload["resulting_subject_hash"][7:],
        "receipt_hash": payload["receipt_hash"][7:],
    }


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


def test_pool_rejects_canonical_hash_native_review_forgery(
        env):
    gate, daemon = env
    ids = _build_chain(gate, daemon)
    receipt = _forge_native_code_review_without_guardian(
        daemon, cycle_id=2, build_target_id=ids["bt"])

    assert gate._scientific_review_receipt_valid(
        cycle_id=2, build_target_id=ids["bt"], receipt=receipt) is False


def test_pool_rejects_schema_valid_native_review_without_durable_child_proof(
        env):
    gate, daemon = env
    ids = _build_chain(gate, daemon)
    receipt = _forge_native_code_review_without_guardian(
        daemon, cycle_id=2, build_target_id=ids["bt"],
        runtime_receipt_hash=True)

    assert gate._scientific_review_receipt_valid(
        cycle_id=2, build_target_id=ids["bt"], receipt=receipt) is False


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
    with pytest.raises(GateReject, match="run/replicate"):
        gate.gate_claim_baseline(
            canonical_key="ck-seeded", slug="seeded", cycle_id="c2",
            identity_draft_md="# seeded",
            config_json='{"training":{"seed":37}}')
    assert d.query_one(
        "SELECT 1 FROM baseline WHERE canonical_key='ck-seeded'") is None


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
    with pytest.raises(GateReject, match="run/replicate"):
        gate.gate_claim_variant(
            baseline_id=1, variant_key="seeded",
            config_json='{"lr":1,"seed":37}', cycle_id="c2", seq=52)
    assert d.query_one(
        "SELECT 1 FROM variant WHERE baseline_id=1 AND variant_key='seeded'") is None


# ============ 全链：claim → build 生命周期 → 双评审 → register_evaluation → register_baseline → complete ============
def _build_chain(gate, d):
    """走到「run success + target running + 结果评审 pass」的自建 baseline 现场。返回 ids。"""
    r = gate.gate_claim_baseline(canonical_key="ck-b", slug="b", cycle_id="c2", identity_draft_md="# id 草稿")
    bid, vid = r["baseline_id"], r["variant_id"]
    with d.transaction() as conn:   # plan 落 build 目标 + required metric（(1,1) 已在 seed protocol 声明）
        contract = {
            "validity_gates": [
                {"gate_id": "required", "kind": "required_metrics_present"},
                {"gate_id": "parser", "kind": "parser_not_suspect"},
                {
                    "gate_id": "independent_review",
                    "kind":
                        "independent_code_plan_data_boundary_review_receipt_present",
                },
            ],
            "outcome_rules": [{
                "rule_id": "primary", "metric_id": 1, "metric_ver": 1,
                "operator": "ge", "threshold": 0.8,
                "if_true": "supported", "if_false": "refuted",
            }],
        }
        bt = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,"
            "baseline_id,variant_id,plan_ref) "
            "VALUES (2,2,'build',1,'pending',?,?,?)",
            (bid, vid, json.dumps({
                "target_key": "gate-target",
                "scientific_contract": contract,
            }, sort_keys=True))).lastrowid
        conn.execute("INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) VALUES (?,1,1)", (bt,))
    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    code_subject = "c" * 64
    code_review = _judge_pass(
        d, bt, "bundle_code_review", code_subject)
    gate.gate_progress_build_target(
        build_target_id=bt, to="running",
        current_subject_hash=code_subject)
    rid = gate.gate_start_run(build_target_id=bt, cycle_id="c2", variant_id=vid, kind="build", env_hash="eh")
    with d.transaction() as conn:
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (?,'final','/p','ckh','sha256',?)", (vid, rid))
    gate.gate_finish_run(run_id=rid, status="success", cost=1.0)
    _judge_pass(d, bt, "bundle_result_review", "res-sh")
    return {
        "baseline_id": bid, "variant_id": vid, "bt": bt, "run": rid,
        "code_review_decision_id": code_review,
        "scientific_contract": contract,
    }


def _scientific_decision(d, *, bt, evaluation_id, attempt_id,
                         validity="valid", outcome="refuted",
                         eligibility="eligible", metric_value=0.2,
                         duplicate=False):
    target = d.query_one(
        "SELECT plan_ref FROM build_target WHERE id=?", (bt,))
    plan_ref = json.loads(target[0])
    review_row = d.query_one(
        "SELECT id,payload_json FROM decision WHERE cycle_id=2 "
        "AND actor='judge' AND type='bundle_code_review' "
        "AND json_extract(payload_json,'$.build_target_id')=? "
        "ORDER BY id DESC LIMIT 1", (bt,))
    review = json.loads(review_row[1])
    review_receipt = {
        "protocol": "legacy-bundle-code-review-v1",
        "decision_id": review_row[0],
        "review_kind": "bundle_code",
        "review_scope": "code_plan_data_boundary",
        "subject_hash": review["subject_hash"],
        "receipt_hash": SC.canonical_hash({
            "decision_id": review_row[0], "payload": review,
        }),
    }
    payload = SC.build_scientific_decision_payload(
        build_target_id=bt,
        evaluation_id=evaluation_id,
        evaluation_attempt_id=attempt_id,
        contract=plan_ref["scientific_contract"],
        execution_status="succeeded",
        required_metrics={(1, 1)},
        metric_results=[{
            "metric_id": 1, "metric_ver": 1,
            "value": metric_value, "scope": "aggregate",
        }],
        eval_log_hash="e" * 64,
        parser={
            "version": "test-parser-v1",
            "policy_hash": "a" * 64,
            "fields": {},
            "suspect": validity == "invalid",
        },
        independent_review_receipt=review_receipt,
    )
    assert payload["validity_status"] == validity
    assert payload["scientific_outcome"] == outcome
    assert payload["pool_eligibility"] == eligibility
    with d.transaction() as conn:
        for _ in range(2 if duplicate else 1):
            conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (2,'orchestrator','bundle_scientific_contract',?)",
                (json.dumps(payload, sort_keys=True),))


@pytest.mark.parametrize(
    ("decision_kind", "accepted"),
    [
        ("missing", False),
        ("invalid", False),
        ("valid_negative", True),
        ("duplicate", False),
    ],
)
def test_scientific_gate_recomputes_unique_bound_decision(
        env, decision_kind, accepted):
    old_gate, d = env
    gate = PoolGate(
        d, old_gate.read, require_scientific_contract=True,
        require_code_review=True)
    ids = _build_chain(gate, d)
    started = gate.gate_start_attempt(
        cycle_id="c2", purpose="factory", build_target_id=ids["bt"],
        create={
            "variant_id": ids["variant_id"],
            "protocol_id": 1,
            "protocol_ver": 1,
            "eval_key": "scientific-gate",
            "source": "factory",
            "target_set_hash": "scientific-gate-targets",
        })
    args = {
        "cycle_id": "c2",
        "build_target_id": ids["bt"],
        "purpose": "factory",
        "current_subject_hash": "res-sh",
        "metric_results": [{
            "metric_id": 1, "metric_ver": 1,
            "value": 0.2, "scope": "aggregate",
        }],
        "attempt_id": started["attempt_id"],
        "artifact_ref": "sha256:" + "e" * 64,
    }

    if decision_kind != "missing":
        _scientific_decision(
            d, bt=ids["bt"], evaluation_id=started["evaluation_id"],
            attempt_id=started["attempt_id"],
            validity=("invalid" if decision_kind == "invalid" else "valid"),
            outcome=("unavailable"
                     if decision_kind == "invalid" else "refuted"),
            eligibility=("ineligible"
                         if decision_kind == "invalid" else "eligible"),
            duplicate=decision_kind == "duplicate")
    if not accepted:
        with pytest.raises(GateReject, match="科学合同"):
            gate.gate_register_evaluation(**args)
    else:
        assert gate.gate_register_evaluation(**args) == {
            "evaluation_id": started["evaluation_id"],
            "attempt_id": started["attempt_id"],
        }


def _formal_registration_ready(gate, d, tmp_path, *, with_execution_log=True):
    """Build a real, verified publication around one pre-started factory attempt."""
    final_identity = "# identity\n\n## 复现命令\npython train.py"
    claimed = gate.gate_claim_baseline(
        canonical_key="ck-formal", slug="formal", cycle_id="c2",
        identity_draft_md="# planning draft")
    bid, vid = claimed["baseline_id"], claimed["variant_id"]
    with d.transaction() as conn:
        bt = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
            "VALUES (2,2,'build',1,'pending',?,?)", (bid, vid)).lastrowid
        conn.execute(
            "INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) "
            "VALUES (?,1,1)", (bt,))

    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    _judge_pass(d, bt, "bundle_code_review", "formal-code-sh")
    gate.gate_progress_build_target(
        build_target_id=bt, to="running", current_subject_hash="formal-code-sh")
    rid = gate.gate_start_run(
        build_target_id=bt, cycle_id="c2", variant_id=vid,
        kind="build", env_hash="formal-env")

    inputs = tmp_path / "formal-inputs"
    code = inputs / "src"
    code.mkdir(parents=True)
    (code / "model.py").write_text(
        "def forward(x):\n    return x + 1\n", encoding="utf-8")
    identity = inputs / "identity.md"
    identity.write_text("# identity", encoding="utf-8")
    checkpoint = inputs / "final.pt"
    checkpoint.write_bytes(b"formal-checkpoint\x00")
    work = tmp_path / "work"
    work.mkdir()
    publisher = PoolPublisher(work)
    training = publisher.publish_training(TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=bid, slug="formal", canonical_key="ck-formal",
            identity_source=identity, code_source=code,
            repro_cmd_md="python train.py"),
        variant=VariantPublication(
            variant_id=vid, variant_key="base", config={}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=None, ckpt_key="final", source=checkpoint)],
    ))
    checkpoint_binding = training.checkpoint_bindings[0]
    with d.transaction() as conn:
        checkpoint_id = conn.execute(
            "INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
            "VALUES (?,?,?,?,?,?)",
            (vid, checkpoint_binding["ckpt_key"], checkpoint_binding["path"],
             checkpoint_binding["content_hash"], checkpoint_binding["hash_alg"], rid),
        ).lastrowid
        bind_training_database(
            conn, training, updated_cycle=2,
            checkpoint_ids={"final": checkpoint_id}, run_id=rid)
    gate.gate_finish_run(run_id=rid, status="success")
    _judge_pass(d, bt, "bundle_result_review", "formal-result-sh")

    started = gate.gate_start_attempt(
        cycle_id="c2", purpose="factory", build_target_id=bt,
        create={
            "variant_id": vid, "protocol_id": 1, "protocol_ver": 1,
            "eval_key": "formal-eval", "source": "factory",
            "target_set_hash": "formal-targets",
        })
    results = inputs / "evaluation"
    results.mkdir()
    (results / "eval.log").write_text(
        "metric_value accuracy=0.91\n", encoding="utf-8")
    metrics = [{
        "metric_id": 1, "metric_ver": 1, "value": 0.91,
        "scope": "aggregate",
    }]
    publication = publisher.publish_evaluation(EvaluationPublicationSpec(
        training=training, evaluation_id=started["evaluation_id"],
        eval_key="formal-eval", attempt_id=started["attempt_id"],
        attempt_no=started["attempt_no"], results_source=results,
        primary_artifact="eval.log", metrics=metrics,
        protocol=ProtocolPublication(
            protocol_id=1, version=1, name="proto", scope_spec={}),
        checkpoint_ids={"final": checkpoint_id},
    ))
    log_binding = publication.database_bindings["evaluation_attempt"]
    primary = publication.payload["objects"]["evaluation"]["primary_artifact"]
    if with_execution_log:
        with d.transaction() as conn:
            conn.execute(
                "INSERT INTO execution_log(evaluation_attempt_id,cycle_id,log_kind,ref,content_hash,bytes) "
                "VALUES (?,2,'eval',?,?,?)",
                (started["attempt_id"], log_binding["execution_log_ref"],
                 log_binding["execution_log_hash"], primary["bytes"]))
    production_gate = PoolGate(
        d, gate.read, pool_publisher=publisher,
        require_formal_publication=True)
    return {
        "gate": production_gate, "publisher": publisher, "training": training,
        "publication": publication, "work": work,
        "baseline_id": bid, "variant_id": vid, "build_target_id": bt,
        "run_id": rid, "checkpoint_id": checkpoint_id,
        **started,
        "metric_results": metrics, "final_identity": final_identity,
        "subject_hash": "formal-result-sh", "primary": primary,
        "log_binding": log_binding,
    }


def _register_formal_evaluation_args(prepared, publication):
    return {
        "cycle_id": "c2",
        "build_target_id": prepared["build_target_id"],
        "purpose": "factory",
        "current_subject_hash": prepared["subject_hash"],
        "attempt_id": prepared["attempt_id"],
        "metric_results": prepared["metric_results"],
        "publication": publication,
    }


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


@pytest.mark.parametrize(
    ("invalid_kind", "message"),
    [
        ("missing", "缺 verified pool publication"),
        ("forged", "receipt 与文件复验结果不一致"),
        ("forged_hash", "pool manifest hash 不符"),
        ("stale", "formal evaluation artifact hash 失配"),
    ],
)
def test_production_evaluation_rejects_missing_forged_or_stale_publication(
        env, tmp_path, invalid_kind, message):
    legacy_gate, d = env
    prepared = _formal_registration_ready(legacy_gate, d, tmp_path)
    publication = prepared["publication"]
    if invalid_kind == "missing":
        supplied = None
    elif invalid_kind == "forged":
        supplied = replace(publication, payload={})
    elif invalid_kind == "forged_hash":
        supplied = replace(publication, manifest_hash="0" * 64)
    else:
        (prepared["work"] / prepared["primary"]["path"]).write_bytes(b"stale")
        supplied = publication

    with pytest.raises(GateReject, match=message):
        prepared["gate"].gate_register_evaluation(
            **_register_formal_evaluation_args(prepared, supplied))

    assert d.query_one(
        "SELECT status,artifact_ref,transcript_ref FROM evaluation_attempt WHERE id=?",
        (prepared["attempt_id"],)) == ("running", None, None)
    assert d.query_one(
        "SELECT status,canonical_attempt_id FROM evaluation WHERE id=?",
        (prepared["evaluation_id"],)) == ("running", None)
    assert d.query_one(
        "SELECT count(*) FROM metric_result WHERE evaluation_id=?",
        (prepared["evaluation_id"],))[0] == 0
    assert d.query_one(
        "SELECT identity_doc,status FROM baseline WHERE id=?",
        (prepared["baseline_id"],)) == ("# planning draft", "building")
    assert d.query_one(
        "SELECT status FROM variant WHERE id=?", (prepared["variant_id"],))[0] == "building"
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication'")[0] == 0
    assert d.query_one("SELECT count(*) FROM card")[0] == 0


def test_production_evaluation_and_legal_transition_require_atomic_publication_closure(
        env, tmp_path):
    legacy_gate, d = env
    prepared = _formal_registration_ready(
        legacy_gate, d, tmp_path, with_execution_log=False)
    gate, publication = prepared["gate"], prepared["publication"]
    register = _register_formal_evaluation_args(prepared, publication)

    # Even a byte-valid publication cannot make the evaluation successful when
    # its formal execution-log ref/hash is absent.  All earlier writes roll back.
    with pytest.raises(GateReject, match="formal evaluation execution_log"):
        gate.gate_register_evaluation(**register)
    assert d.query_one(
        "SELECT status,artifact_ref,transcript_ref FROM evaluation_attempt WHERE id=?",
        (prepared["attempt_id"],)) == ("running", None, None)
    assert d.query_one(
        "SELECT status,canonical_attempt_id FROM evaluation WHERE id=?",
        (prepared["evaluation_id"],)) == ("running", None)
    assert d.query_one(
        "SELECT identity_doc,status FROM baseline WHERE id=?",
        (prepared["baseline_id"],)) == ("# planning draft", "building")
    assert d.query_one(
        "SELECT count(*) FROM metric_result WHERE evaluation_id=?",
        (prepared["evaluation_id"],))[0] == 0
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication'")[0] == 0

    primary = prepared["primary"]
    binding = prepared["log_binding"]
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO execution_log(evaluation_attempt_id,cycle_id,log_kind,ref,content_hash,bytes) "
            "VALUES (?,2,'eval',?,?,?)",
            (prepared["attempt_id"], binding["execution_log_ref"],
             binding["execution_log_hash"], primary["bytes"]))
    assert gate.gate_register_evaluation(**register) == {
        "evaluation_id": prepared["evaluation_id"],
        "attempt_id": prepared["attempt_id"],
    }
    assert d.query_one(
        "SELECT status,artifact_ref,transcript_ref FROM evaluation_attempt WHERE id=?",
        (prepared["attempt_id"],)) == (
            "success", binding["artifact_ref"], publication.manifest_ref)
    assert d.query_one(
        "SELECT status,canonical_attempt_id FROM evaluation WHERE id=?",
        (prepared["evaluation_id"],)) == ("success", prepared["attempt_id"])
    assert d.query_one(
        "SELECT identity_doc,status FROM baseline WHERE id=?",
        (prepared["baseline_id"],)) == (prepared["final_identity"], "building")
    assert d.query_one(
        "SELECT status FROM variant WHERE id=?", (prepared["variant_id"],))[0] == "building"
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication'")[0] == 1
    assert d.query_one(
        "SELECT count(*) FROM card WHERE card_type IN ('baseline','variant','protocol')")[0] == 3
    with d.transaction() as conn:
        assert is_formally_published(conn, variant_id=prepared["variant_id"]) is False

    gate.gate_register_baseline(
        baseline_id=prepared["baseline_id"], variant_id=prepared["variant_id"],
        build_target_id=prepared["build_target_id"],
        evaluation_id=prepared["evaluation_id"], cycle_id="c2",
        current_subject_hash=prepared["subject_hash"],
        identity_doc="# identity", repro_cmd="python train.py",
        run_id=prepared["run_id"], publication=publication)
    assert d.query_one(
        "SELECT status FROM baseline WHERE id=?", (prepared["baseline_id"],))[0] == "legal"
    assert d.query_one(
        "SELECT status FROM variant WHERE id=?", (prepared["variant_id"],))[0] == "legal"
    with d.transaction() as conn:
        assert is_formally_published(conn, variant_id=prepared["variant_id"]) is True
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication'")[0] == 1


def test_eval_only_registration_itself_requires_and_binds_formal_publication(
        env, tmp_path):
    legacy_gate, d = env
    prepared = _formal_registration_ready(legacy_gate, d, tmp_path)
    gate, publication = prepared["gate"], prepared["publication"]
    # Isolate the evaluation-registration gate on the same fully constructed
    # evidence graph: there is no later baseline/variant registration step for
    # an eval-only target, so this transaction must create the DB publication anchor.
    with d.transaction() as conn:
        conn.execute(
            "UPDATE build_target SET target_kind='eval',"
            "eval_action='create_evaluation',attempt_purpose='standalone_eval',"
            "evaluation_source='factory',eval_key='formal-eval' WHERE id=?",
            (prepared["build_target_id"],))
        conn.execute(
            "UPDATE baseline SET identity_doc=?,status='legal' WHERE id=?",
            (prepared["final_identity"], prepared["baseline_id"]))
        conn.execute(
            "UPDATE variant SET status='legal' WHERE id=?",
            (prepared["variant_id"],))
        assert is_formally_published(
            conn, variant_id=prepared["variant_id"]) is False

    with pytest.raises(GateReject, match="缺 verified pool publication"):
        gate.gate_register_evaluation(
            **_register_formal_evaluation_args(prepared, None))
    assert d.query_one(
        "SELECT status FROM evaluation_attempt WHERE id=?",
        (prepared["attempt_id"],))[0] == "running"

    gate.gate_register_evaluation(
        **_register_formal_evaluation_args(prepared, publication))
    assert d.query_one(
        "SELECT status FROM evaluation_attempt WHERE id=?",
        (prepared["attempt_id"],))[0] == "success"
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication' "
        "AND json_extract(payload_json,'$.evaluation_id')=?",
        (prepared["evaluation_id"],))[0] == 1
    with d.transaction() as conn:
        assert is_formally_published(
            conn, variant_id=prepared["variant_id"]) is True


def test_formal_append_attempt_binds_new_publication_without_replacing_canonical(
        env, tmp_path):
    legacy_gate, d = env
    prepared = _formal_registration_ready(legacy_gate, d, tmp_path)
    gate, first = prepared["gate"], prepared["publication"]
    gate.gate_register_evaluation(
        **_register_formal_evaluation_args(prepared, first))
    gate.gate_register_baseline(
        baseline_id=prepared["baseline_id"], variant_id=prepared["variant_id"],
        build_target_id=prepared["build_target_id"],
        evaluation_id=prepared["evaluation_id"], cycle_id="c2",
        current_subject_hash=prepared["subject_hash"],
        identity_doc="# identity", repro_cmd="python train.py",
        run_id=prepared["run_id"], publication=first)
    canonical_attempt = prepared["attempt_id"]

    appended = gate.gate_start_attempt(
        cycle_id="c2", purpose="repro_eval",
        build_target_id=prepared["build_target_id"],
        evaluation_id=prepared["evaluation_id"])
    results = tmp_path / "append-evaluation"
    results.mkdir()
    (results / "eval.log").write_text(
        "metric_value accuracy=0.92\n", encoding="utf-8")
    metrics = [{
        "metric_id": 1, "metric_ver": 1, "value": 0.92,
        "scope": "aggregate",
    }]
    publication = prepared["publisher"].publish_evaluation(
        EvaluationPublicationSpec(
            training=prepared["training"],
            evaluation_id=prepared["evaluation_id"], eval_key="formal-eval",
            attempt_id=appended["attempt_id"], attempt_no=appended["attempt_no"],
            results_source=results, primary_artifact="eval.log", metrics=metrics,
            protocol=ProtocolPublication(
                protocol_id=1, version=1, name="proto", scope_spec={}),
            checkpoint_ids={"final": prepared["checkpoint_id"]},
        ))
    binding = publication.database_bindings["evaluation_attempt"]
    primary = publication.payload["objects"]["evaluation"]["primary_artifact"]
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO execution_log(evaluation_attempt_id,cycle_id,log_kind,ref,content_hash,bytes) "
            "VALUES (?,2,'eval',?,?,?)",
            (appended["attempt_id"], binding["execution_log_ref"],
             binding["execution_log_hash"], primary["bytes"]))

    gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=prepared["build_target_id"],
        purpose="repro_eval", current_subject_hash=prepared["subject_hash"],
        attempt_id=appended["attempt_id"], metric_results=metrics,
        publication=publication)
    assert d.query_one(
        "SELECT status,canonical_attempt_id FROM evaluation WHERE id=?",
        (prepared["evaluation_id"],)) == ("success", canonical_attempt)
    assert d.query_one(
        "SELECT status,artifact_ref,transcript_ref FROM evaluation_attempt WHERE id=?",
        (appended["attempt_id"],)) == (
            "success", binding["artifact_ref"], publication.manifest_ref)
    assert d.query_one(
        "SELECT count(*) FROM decision WHERE type='pool_publication' "
        "AND json_extract(payload_json,'$.variant_id')=?",
        (prepared["variant_id"],))[0] == 2
    with d.transaction() as conn:
        assert is_formally_published(
            conn, variant_id=prepared["variant_id"]) is True


def test_register_existing_running_attempt(env):
    """生产 factory 时序：评估执行前 attempt 已 running，结果评审后原子收口同一 ID。"""
    gate, d = env
    ids = _build_chain(gate, d)
    started = gate.gate_start_attempt(
        cycle_id="c2", purpose="factory", build_target_id=ids["bt"],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "prestarted", "source": "factory", "target_set_hash": "tsh"})
    assert d.query_one(
        "SELECT status FROM evaluation_attempt WHERE id=?", (started["attempt_id"],))[0] == "running"
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory",
        current_subject_hash="res-sh", attempt_id=started["attempt_id"],
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        artifact_ref="sha256:abc", transcript_ref="receipt.json")
    assert reg == {"evaluation_id": started["evaluation_id"], "attempt_id": started["attempt_id"]}
    assert d.query_one(
        "SELECT status,artifact_ref,transcript_ref FROM evaluation_attempt WHERE id=?",
        (started["attempt_id"],)) == ("success", "sha256:abc", "receipt.json")
    assert d.query_one(
        "SELECT canonical_attempt_id FROM evaluation WHERE id=?", (started["evaluation_id"],))[0] == started["attempt_id"]


def test_register_evaluation_requires_result_review(env):
    gate, d = env
    ids = _build_chain(gate, d)
    with pytest.raises(GateReject, match="结果评审"):
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="WRONG",
            metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.9}],
            create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                    "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})


def test_reviews_can_be_disabled_without_weakening_registration_gates(env):
    default_gate, d = env
    gate = PoolGate(
        d, default_gate.read,
        require_code_review=False, require_result_review=False)
    claimed = gate.gate_claim_baseline(
        canonical_key="ck-no-review", slug="no-review", cycle_id="c2",
        identity_draft_md="# draft")
    bid, vid = claimed["baseline_id"], claimed["variant_id"]
    with d.transaction() as conn:
        bt = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
            "VALUES (2,2,'build',1,'pending',?,?)", (bid, vid)).lastrowid
        conn.execute(
            "INSERT INTO build_target_required_metric(build_target_id,metric_id,metric_ver) VALUES (?,1,1)",
            (bt,))

    gate.gate_start_build_target(build_target_id=bt)
    gate.gate_progress_build_target(build_target_id=bt, to="smoke")
    gate.gate_progress_build_target(build_target_id=bt, to="running")
    rid = gate.gate_start_run(
        build_target_id=bt, cycle_id="c2", variant_id=vid, kind="build", env_hash="eh")
    with d.transaction() as conn:
        conn.execute(
            "INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
            "VALUES (?,'final','/p','ckh','sha256',?)", (vid, rid))
    gate.gate_finish_run(run_id=rid, status="success")

    create = {
        "variant_id": vid, "protocol_id": 1, "protocol_ver": 1,
        "eval_key": "fac-no-review", "source": "factory", "target_set_hash": "tsh",
    }
    with pytest.raises(GateReject, match="required metric"):         # metric gate 仍保留
        gate.gate_register_evaluation(
            cycle_id="c2", build_target_id=bt, purpose="factory",
            current_subject_hash="unused", metric_results=[], create=create)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=bt, purpose="factory",
        current_subject_hash="unused",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create=create)
    assert gate.review_passed(
        build_target_id=bt, review_kind="bundle_result_review",
        current_subject_hash="unused") is False
    gate.gate_register_baseline(
        baseline_id=bid, variant_id=vid, build_target_id=bt,
        evaluation_id=reg["evaluation_id"], cycle_id="c2",
        current_subject_hash="unused", identity_doc="# identity",
        repro_cmd="python train.py", run_id=rid)
    assert d.query_one("SELECT status FROM baseline WHERE id=?", (bid,))[0] == "legal"
    assert d.query_one("SELECT status FROM variant WHERE id=?", (vid,))[0] == "legal"


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
        btn = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status) "
                           "VALUES (2,2,'build',70,'running')").lastrowid
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
    """append 模式（步⑧ CP8.6 收准）：**同 variant 的别的 target 可追加**（跨轮 metric_append/repro_eval
    的 eval 目标正是 §3.3.1 情形三/四——evaluation.build_target_id 是创建者锚，append 者身份记 attempt 侧）；
    **不同 variant 的 target 仍拒**（target↔variant 真不变量）；success 追加保留原 canonical + abandoned 拒。"""
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    eid, first_aid = reg["evaluation_id"], reg["attempt_id"]
    with d.transaction() as conn:   # 另一 variant 上的 eval 目标（append 型须带 evaluation_id，DDL CHECK）
        bl2 = conn.execute("INSERT INTO baseline(slug,canonical_key,identity_doc,born_cycle,status) "
                           "VALUES ('b2','ck-b2','id2',2,'legal')").lastrowid
        vid2 = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) "
                            "VALUES (?,'base','{}','legal')", (bl2,)).lastrowid
        bt_bad = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,evaluation_id) "
                              "VALUES (2,2,'eval',89,'running',?,'append_attempt',?)", (vid2, eid)).lastrowid
        bt2 = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,evaluation_id) "
                           "VALUES (2,2,'eval',90,'running',?,'append_attempt',?)", (ids["variant_id"], eid)).lastrowid
    _judge_pass(d, bt_bad, "bundle_result_review", "res-sh-bad")
    with pytest.raises(GateReject, match="target 绑定不符"):    # bt_bad 绑 variant2，eval 属 variant1 → 拒
        gate.gate_register_evaluation(cycle_id="c2", build_target_id=bt_bad, purpose="metric_append",
                                      current_subject_hash="res-sh-bad", metric_results=[], evaluation_id=eid)
    _judge_pass(d, bt2, "bundle_result_review", "res-sh")
    r2 = gate.gate_register_evaluation(   # 合法 append（同 variant 的别的 target）：保留原 canonical
        cycle_id="c2", build_target_id=bt2, purpose="metric_append", current_subject_hash="res-sh",
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
        conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id) "
                     "VALUES (2,2,'build',60,'pending',1)")
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


def test_register_evaluation_append_wrong_cell_rejected(env):
    """codex 第2轮 BLOCKER 回归：同 variant 但 append 目标未显式绑定该 evaluation（build_target.evaluation_id
    指向别的格子/NULL）→ 拒（防同 variant 多格子错格污染）。"""
    gate, d = env
    ids = _build_chain(gate, d)
    reg = gate.gate_register_evaluation(
        cycle_id="c2", build_target_id=ids["bt"], purpose="factory", current_subject_hash="res-sh",
        metric_results=[{"metric_id": 1, "metric_ver": 1, "value": 0.91}],
        create={"variant_id": ids["variant_id"], "protocol_id": 1, "protocol_ver": 1,
                "eval_key": "fac", "source": "factory", "target_set_hash": "tsh"})
    eid = reg["evaluation_id"]
    with d.transaction() as conn:   # 同 variant、但第二个格子（protocol@2）+ 一个指向它的 append 目标
        conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,2,'p','{}')")
        eid2 = conn.execute("INSERT INTO evaluation(variant_id,protocol_id,protocol_ver,eval_key,source,status,"
                            "created_cycle,target_set_hash) VALUES (?,1,2,'e2','factory','created',2,'t2')",
                            (ids["variant_id"],)).lastrowid
        bt_wrong = conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id,eval_action,evaluation_id) "
                                "VALUES (2,2,'eval',91,'running',?,'append_attempt',?)", (ids["variant_id"], eid2)).lastrowid
    _judge_pass(d, bt_wrong, "bundle_result_review", "sh-w")
    with pytest.raises(GateReject, match="未显式绑定"):   # bt_wrong 绑 eid2，却往 eid 追加 → 拒
        gate.gate_register_evaluation(cycle_id="c2", build_target_id=bt_wrong, purpose="metric_append",
                                      current_subject_hash="sh-w", metric_results=[], evaluation_id=eid)
