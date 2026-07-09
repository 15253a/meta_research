"""CP5.4 · attack 轮 advance 全链（M4）：idea→plan→bundle（两段提交+真子进程）→reasoning（真证据关问）。

核心验收：真 SQLite 上完整 attack 轮——idea 入表、plan 落 build 目标+池占位、bundle 真训练/评估+双评审+
注册入池、reasoning 以真 metric_result 证据关问；phase_commit 幂等/conflict；kill-9 阶段边界恢复。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from orchestrator import database as db
from orchestrator import obs_parser as OP
from orchestrator.advancer import SqliteAdvancer
from orchestrator.attack_stages import AttackStages
from orchestrator.compiler_sqlite import SqliteCompiler
from orchestrator.gate_pool import PoolGate
from orchestrator.gate_sqlite import SqliteGate, open_gate_read_conn
from orchestrator.manifest import canon_hash as manifest_canon
from orchestrator.schemas import SchemaSet
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]

# 步⑧ CP8.2：命令由 bundle 产的 manifest 承载、跑物化的代码文件（cwd=run/eval staging；train.py 写 ckpt.bin、
# eval.py 读 {ckpt} 打印 int 绑定 metric_value）。行为参数化以复用各恢复剧本（bad train/smoke、lying eval）。
TRAIN_OK = ("import pathlib; print('loss: 1.0'); print('loss: 0.5'); print('loss: 0.2'); "
            "pathlib.Path('ckpt.bin').write_text('weights-v1'); print('wall_clock_sec: 1.0')")
EVAL_OK = ("import sys, pathlib; assert pathlib.Path(sys.argv[1]).read_text() == 'weights-v1'; "
           "print('loss: 0.2'); print('metric_value: 1@1=0.93')")
SMOKE_OK = "print('loss: 0.9'); print('smoke ok')"


def _idea_set():
    """冻结 idea_set.schema 的确定性产物（cand-1 选中、cand-2 审计 fail——防重复造轮全量入账）。"""
    _am = {"source_domain": "线性模型", "target_domain": "toy 分类", "object_mapping": "权重→决策",
           "shared_relations": "线性可分"}
    _six = {"structural_depth": 8, "domain_distance": 7, "applicability": 8, "novelty": 6,
            "unexpectedness": 6, "non_obviousness": 7}
    _low = {"structural_depth": 3, "domain_distance": 3, "applicability": 3, "novelty": 2,
            "unexpectedness": 2, "non_obviousness": 3}
    _NS = "联网粗查已启用·文献级待人工验证"
    return {"idea_set.json": {
        "candidates": [
            {"candidate_id": "cand-1", "generation_path": "bypass", "audit_mapping": _am,
             "core_claim": "线性基线可达 0.9", "mechanism": "最小二乘拟合", "assumptions": ["数据近似线性可分"],
             "min_falsifiable_experiment": "训练线性模型，acc<0.9 即否证", "novelty_type": "训练目标",
             "novelty_status": _NS},
            {"candidate_id": "cand-2", "generation_path": "bypass", "audit_mapping": _am,
             "core_claim": "弱想法", "mechanism": "随机猜", "assumptions": ["无"],
             "min_falsifiable_experiment": "无对照", "novelty_type": "训练目标", "novelty_status": _NS}],
        "audit_scores": [
            {"candidate_id": "cand-1", "scores": _six, "decision": "pass", "rationale": "结构深"},
            {"candidate_id": "cand-2", "scores": _low, "decision": "fail", "rationale": "太浅"}],
        "selected_id": "cand-1", "novelty_refs": []}}


def _plan_json(ck="ck-attack", slug="toy-b"):
    """冻结 plan.schema 的**抽象** plan（一 build 目标 + 协议 + 指标；命令不在此——由 bundle manifest 承载）。"""
    return {"plan.json": {
        "needs": [{"need_id": "n1", "statement_md": "toy 基线可达 0.9"}],
        "reuse_evidence": [],
        "targets": [{"target_key": "t1", "target_kind": "build", "seq": 1, "critical": True,
                     "budget_estimate": 1.0, "spec_md": "训练线性 toy 基线并出厂评估", "need_ids": ["n1"],
                     "claim": {"canonical_key": ck, "slug": slug}}],
        "protocol": {"name": "toy-proto", "version": 1,
                     "scope_spec": {"dataset": "toy", "split": "holdout"}, "smoke_md": "快速跑一步"},
        "metric_defs": [{"metric_id": "m_acc", "version": 1, "name": "acc", "direction": "higher",
                         "compute_spec_md": "正确率"}],
        "readout_rules": [{"metric_id": "m_acc", "metric_ver": 1, "rule_md": "越高越好"}],
        "build_target_required_metric": [{"target_key": "t1", "metric_id": "m_acc", "metric_ver": 1}]}}


def _bundle_provider(daemon, *, train_body=TRAIN_OK, eval_body=EVAL_OK, smoke_body=SMOKE_OK):
    """bundle provider（真 Codex 范式：读 pack 的 plan_slice_hash → 回引，产 manifest + 代码 + identity.md）。
    测试从 DB 读切片自算 plan_slice_hash（真 Codex 从 pack 照抄）；命令跑物化代码文件。"""
    def bundle(cyc, pack):
        bt = int(pack.target_id)
        slice_ = json.loads(daemon.query_one("SELECT plan_ref FROM build_target WHERE id=?", (bt,))[0])
        # exec 目标：plan claim.config_json 是配置决定者 → manifest 须照抄（cross_check 强制相等）
        cfg = (slice_.get("claim") or {}).get("config_json") or {"lr": 0.1}
        manifest = {
            "manifest_version": 1,
            "target_ref": {"target_key": slice_["target_key"], "target_kind": slice_["target_kind"],
                           "seq": slice_["seq"], "plan_slice_hash": manifest_canon(slice_)},
            "protocol_ref": {"protocol_id": slice_["protocol_id"], "protocol_ver": slice_["protocol_ver"]},
            "env_hash": "toy-env", "config_json": cfg,
            "code_files": ["train.py", "eval.py", "smoke.py"],
            "commands": {"smoke": {"argv": [sys.executable, "{src}/smoke.py"]},
                         "train": {"argv": [sys.executable, "{src}/train.py"]},
                         "eval": {"argv": [sys.executable, "{src}/eval.py", "{ckpt}"]}},
            "expected_outputs": {"checkpoint": "ckpt.bin"},
            "repro_cmd_md": "python train.py 后 python eval.py <ckpt>"}
        return {"execution_manifest.json": manifest, "identity.md": "# toy 基线\n结构: 线性\n\n## 能力\nacc≈0.93",
                "train.py": train_body, "eval.py": eval_body, "smoke.py": smoke_body}
    return bundle


def _providers(daemon, *, bundle=None):
    """确定性 providers（生产 = 真 Codex 会话；judge 写真 runner_call+DECISION 行）。"""
    def idea(cyc, pack):
        return _idea_set()

    def plan(cyc, pack):
        return _plan_json()

    def judge(cycle_id, bt_id, kind, subject_hash):
        from orchestrator.ids import cnum
        with daemon.transaction() as conn:
            rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (?,'audit',?,'success')",
                              (cnum(cycle_id), kind)).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                         (cnum(cycle_id), kind, json.dumps({"build_target_id": bt_id, "review_kind": kind,
                                                            "round_no": 1, "verdict": "pass",
                                                            "subject_hash": subject_hash,
                                                            "runner_call_id": rc, "policy_hash": "ph"})))

    def reasoning(cyc, pack):
        mr = daemon.query_one("SELECT id FROM metric_result ORDER BY id DESC LIMIT 1")[0]
        return {"answer.json": {"question_id": cyc.question_id, "verdict": "answered",
                                "evidence": [{"kind": "evaluation", "metric_result_id": f"mr{mr}",
                                              "claim_md": "toy 基线 acc=0.93"}],
                                "answer_md": "以出厂测量关问"},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}

    return {"idea": idea, "plan": plan, "judge": judge, "reasoning": reasoning,
            "bundle": bundle if bundle is not None else _bundle_provider(daemon)}


def _mk_env(path, work, *, train_body=TRAIN_OK, eval_body=EVAL_OK, smoke_body=SMOKE_OK):
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="toy 研究目标")
    pool = PoolGate(daemon, open_gate_read_conn(path))
    obs_conn = db.connect(path)
    close_gate = SqliteGate(daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"),
                            parser_suspect=lambda aid: OP.suspect_for_attempt(obs_conn, aid, OBS))
    bundle = _bundle_provider(daemon, train_body=train_body, eval_body=eval_body, smoke_body=smoke_body)
    attack = AttackStages(state=state, compiler=compiler, pool_gate=pool, close_gate=close_gate,
                          providers=_providers(daemon, bundle=bundle), obs_policy=OBS, work_root=str(work),
                          schemas=SchemaSet(SYSTEM_ROOT / "schemas"), policy=POLICY)
    return daemon, state, compiler, attack


def _bootstrap_attack(state):
    """创世：goal + root 问题（open）+ 上轮 selection 指向 attack root。
    **步⑧**：协议/指标不再预插——plan 阶段经 gate_new_protocol 真注册（derive 空表 → protocol/metric id=1）。"""
    state.create_goal(text="toy 研究目标", predicate_json={})
    c0 = state.open_or_resume_cycle()
    state.set_route(c0.cycle_id, "bootstrap")
    with state.atomic():
        state.apply_tree_ops(c0.cycle_id, [{"op": "create_root", "text": "toy 基线能到 0.9 吗", "local_key": "r"}])
        state.persist_selection(c0.cycle_id, __import__("orchestrator.interfaces", fromlist=["Selection"]).Selection(
            next_question_id="r", next_intent="attack", scores=[]))
        state.mark_cycle_done(c0.cycle_id)


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "work")
    _bootstrap_attack(state)
    adv = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack)
    return {"path": path, "daemon": daemon, "state": state, "adv": adv, "tmp": tmp_path}


# ============ 全链 e2e ============
def test_full_attack_cycle(env):
    d = env["daemon"]
    ids = env["adv"].run_cycles(max_cycles=4)
    assert len(ids) == 1                                            # attack 轮跑完即 terminate 停机
    # idea 入表（含 failed 候选——防重复造轮全量入账）
    assert d.query_one("SELECT count(*) FROM idea")[0] == 2
    assert d.query_one("SELECT count(*) FROM idea WHERE status='selected'")[0] == 1
    # plan 落 target + 池占位 → bundle 全链后 complete + legal
    assert d.query_one("SELECT status FROM build_target WHERE target_kind='build'")[0] == "complete"
    assert d.query_one("SELECT status FROM baseline WHERE canonical_key='ck-attack'")[0] == "legal"
    # 真 run + checkpoint + 观测（run log + attempt log 双 ingest）
    assert d.query_one("SELECT status FROM run WHERE kind='build'")[0] == "success"
    assert d.query_one("SELECT count(*) FROM checkpoint WHERE ckpt_key LIKE 'final-r%'")[0] == 1
    assert d.query_one("SELECT count(*) FROM execution_observation WHERE source='parser'")[0] == 2
    # 真 evaluation + metric_result + 证据回溯 + 关问（eval_key=target_key='t1'，步⑧派生）
    assert d.query_one("SELECT status FROM evaluation WHERE source='factory' AND eval_key='t1'")[0] == "success"
    # plan 阶段真注册协议（gate_new_protocol；派生空表 → id=1）
    assert d.query_one("SELECT count(*) FROM protocol WHERE name='toy-proto'")[0] == 1
    assert d.query_one("SELECT id FROM metric_def WHERE name='acc'")[0] == 1
    assert d.query_one("SELECT value FROM metric_result ORDER BY id DESC LIMIT 1")[0] == 0.93
    assert d.query_one("SELECT status FROM question WHERE id=1")[0] == "answered"
    ev = d.query_one("SELECT kind, valid, metric_result_id FROM evidence ORDER BY id DESC LIMIT 1")
    assert ev[0] == "evaluation" and ev[1] == 1 and ev[2] is not None
    # phase_commit：idea/plan 各一 + bundle 每 target 一
    assert d.query_one("SELECT count(*) FROM phase_commit WHERE stage='idea'")[0] == 1
    assert d.query_one("SELECT count(*) FROM phase_commit WHERE stage='plan'")[0] == 1
    assert d.query_one("SELECT count(*) FROM phase_commit WHERE stage='bundle' AND target_id IS NOT NULL")[0] == 1
    # cycle 终态 done
    assert env["state"].last_done_cycle().next_intent == "terminate"


def test_advance_idempotent_after_done(env):
    env["adv"].run_cycles(max_cycles=4)
    before = env["daemon"].query_one("SELECT count(*) FROM idea")[0]
    assert env["adv"].run_cycles(max_cycles=4) == []                # terminate 停机、无新轮
    assert env["daemon"].query_one("SELECT count(*) FROM idea")[0] == before


# ============ kill-9 阶段边界恢复（§7.1 M4 恢复扩展）============
def _final_state(path):
    """终库确定性面（排除 timestamp/自增 id 序/staging 绝对路径 ref）：含 run/execution_log/observation
    （内审 SHOULD：不比这些表就测不出 (i)/(ii) 缝隙的观测缺失）。"""
    c = db.connect(path)
    out = {
        "cycles": c.execute("SELECT id,status,route,next_intent FROM cycle ORDER BY id").fetchall(),
        "questions": c.execute("SELECT id,status FROM question ORDER BY id").fetchall(),
        "idea": c.execute("SELECT question_id,content_md,status FROM idea ORDER BY id").fetchall(),
        "targets": c.execute("SELECT id,target_kind,status FROM build_target ORDER BY id").fetchall(),
        "baseline": c.execute("SELECT canonical_key,status FROM baseline ORDER BY id").fetchall(),
        "evaluation": c.execute("SELECT eval_key,source,status FROM evaluation ORDER BY id").fetchall(),
        "metrics": c.execute("SELECT metric_id,metric_ver,value,scope FROM metric_result ORDER BY id").fetchall(),
        "pc": c.execute("SELECT stage, target_id IS NOT NULL FROM phase_commit ORDER BY id").fetchall(),
        "run": c.execute("SELECT kind,status,failure_kind FROM run ORDER BY id").fetchall(),
        "exec_log": c.execute("SELECT log_kind, run_id IS NOT NULL, evaluation_attempt_id IS NOT NULL, "
                              "content_hash FROM execution_log ORDER BY id").fetchall(),
        "obs": c.execute("SELECT source,nan_seen,loss_trend,parser_version FROM execution_observation "
                         "ORDER BY id").fetchall(),
    }
    c.close()
    return out


def test_kill9_mid_attack_recovery(tmp_path):
    """在 plan 阶段已提交、bundle 未开始时 SIGKILL → 新进程续跑 → 终库与不杀一致（排除非确定字段）。"""
    ref = str(tmp_path / "ref.sqlite")
    d0, s0, _, a0 = _mk_env(ref, tmp_path / "wref")
    _bootstrap_attack(s0)
    SqliteAdvancer(s0, a0.compiler, lambda c, p: None, attack=a0).run_cycles(max_cycles=4)
    d0.conn.close()

    path = str(tmp_path / "research.sqlite")
    d1, s1, _, _ = _mk_env(path, tmp_path / "w1")
    _bootstrap_attack(s1)
    d1.conn.close()

    marker = tmp_path / "ready.flag"
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(f"""
        import sys, time, json, pathlib
        sys.path.insert(0, {str(SYSTEM_ROOT)!r}); sys.path.insert(0, {str(SYSTEM_ROOT / 'tests')!r})
        import test_attack_advance as T
        from orchestrator.advancer import SqliteAdvancer
        daemon, state, compiler, attack = T._mk_env({path!r}, {str(tmp_path / 'w2')!r})
        real_plan = attack.p["plan"]
        def plan_then_hang(cyc, pack):
            files = real_plan(cyc, pack)
            return files
        attack.p["plan"] = plan_then_hang
        real_bundle = attack._bundle_stage
        def bundle_hang(cyc):
            open({str(marker)!r}, "w").close()   # plan 已提交、bundle 将始 → 信号父可杀
            time.sleep(60)
        attack._bundle_stage = bundle_hang
        SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    """), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(worker)])
    try:
        for _ in range(300):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "worker 未到 bundle 挂起点"
        proc.kill(); proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    d2, s2, _, a2 = _mk_env(path, tmp_path / "w3")                  # 全新进程续跑
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    d2.conn.close()
    assert _final_state(path) == _final_state(ref)


def test_crash_between_register_and_ingest_recovers(tmp_path):
    """内审 BLOCKER 回归：崩在 gate_register_evaluation（已提交）与 eval log ingest 之间 → 全新实例续跑
    **不楔死**（补登幂等重导出），终库与不杀一致。"""
    ref = str(tmp_path / "ref.sqlite")
    d0, s0, _, a0 = _mk_env(ref, tmp_path / "wref")
    _bootstrap_attack(s0)
    SqliteAdvancer(s0, a0.compiler, lambda c, p: None, attack=a0).run_cycles(max_cycles=4)
    d0.conn.close()

    path = str(tmp_path / "research.sqlite")
    d1, s1, _, a1 = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(s1)
    orig = a1._register_and_ingest_log
    def crash_on_eval(cycle_id, log_path, **kw):          # 模拟 kill：eval 补登点炸（register_evaluation 已提交）
        if kw.get("evaluation_attempt_id") is not None:
            raise SystemExit("SIM-KILL9-after-register_evaluation")
        return orig(cycle_id, log_path, **kw)
    a1._register_and_ingest_log = crash_on_eval
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    assert d1.query_one("SELECT status FROM evaluation")[0] == "success"     # 注册段已落、补登未落
    assert d1.query_one("SELECT count(*) FROM execution_log WHERE evaluation_attempt_id IS NOT NULL")[0] == 0
    d1.conn.close()

    d2, s2, _, a2 = _mk_env(path, tmp_path / "w")          # 同 work_root（staging 存活跨重启）
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    d2.conn.close()
    assert _final_state(path) == _final_state(ref)


def test_failed_train_target_cycle_closes_clean(tmp_path):
    """§7.1 判例④ 雏形：训练失败 → run(failed)+target(failed) 入账；无证据不关问、Qn 置 inconclusive、
    轮正常收尾（不楔死、不入树）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w",
                                              train_body="import sys; print('loss: 1.0'); sys.exit(1)")
    _bootstrap_attack(state)
    attack.p["reasoning"] = lambda cyc, pack: {   # 无 answer（无测量可证）；只 selection
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert daemon.query_one("SELECT status,failure_kind FROM run")[:2] == ("failed", "runtime")   # 入账
    assert daemon.query_one("SELECT status FROM build_target")[0] == "failed"
    assert daemon.query_one("SELECT count(*) FROM evidence")[0] == 0                              # 不入树
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "inconclusive"        # Qn 收干净
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done"
    daemon.conn.close()


def test_crash_after_eval_final_before_register_recovers(tmp_path):
    """codex BLOCKER 回归：崩在「eval 跑完（final 已改名）→ register 前」→ resume 从存活 final 续注册、
    不重跑（否则撞 run_staged 同名 final 拒→永久楔死）；终库与不杀一致。"""
    ref = str(tmp_path / "ref.sqlite")
    d0, s0, _, a0 = _mk_env(ref, tmp_path / "wref")
    _bootstrap_attack(s0)
    SqliteAdvancer(s0, a0.compiler, lambda c, p: None, attack=a0).run_cycles(max_cycles=4)
    d0.conn.close()

    path = str(tmp_path / "research.sqlite")
    d1, s1, _, a1 = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(s1)
    real_judge = a1.p["judge"]
    def judge_crash_on_result(cycle_id, bt_id, kind, sh):   # eval final 已在（result 评审前）→ 模拟 kill
        if kind == "bundle_result_review":
            raise SystemExit("SIM-KILL9-after-eval-final")
        return real_judge(cycle_id, bt_id, kind, sh)
    a1.p["judge"] = judge_crash_on_result
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    assert d1.query_one("SELECT count(*) FROM evaluation")[0] == 0     # 未注册
    d1.conn.close()

    d2, s2, _, a2 = _mk_env(path, tmp_path / "w")          # 同 work_root：eval final 存活
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    d2.conn.close()
    assert _final_state(path) == _final_state(ref)


def test_eval_log_tamper_fails_loud(tmp_path):
    """codex BLOCKER 回归：崩在 register 与补登之间且 staging eval.log 被改写 → 补登哈希锚不符 → fail loud
    （不得把 suspect attempt 洗成 clean）。"""
    path = str(tmp_path / "research.sqlite")
    d1, s1, _, a1 = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(s1)
    orig = a1._register_and_ingest_log
    def crash_on_eval(cycle_id, log_path, **kw):
        if kw.get("evaluation_attempt_id") is not None:
            raise SystemExit("SIM-KILL9")
        return orig(cycle_id, log_path, **kw)
    a1._register_and_ingest_log = crash_on_eval
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    d1.conn.close()
    for p in (tmp_path / "w").rglob("eval.log"):           # 篡改 staging（洗白企图）
        p.write_text("loss: 0.1\nmetric_value: 1@1=0.99\n")
    d2, s2, _, a2 = _mk_env(path, tmp_path / "w")
    with pytest.raises(RuntimeError, match="哈希不符"):
        SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    assert d2.query_one("SELECT count(*) FROM build_target WHERE status='complete'")[0] == 0
    d2.conn.close()


def test_smoke_failure_fails_target(tmp_path):
    """codex SHOULD 回归：smoke 子进程非 0 → target failed(smoke)（exit code 不得忽略）；
    codex 第2轮 BLOCKER 回归：终态早退也落 phase_commit(bundle,target)（否则杀/不杀分裂）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w", smoke_body="import sys; sys.exit(2)")
    _bootstrap_attack(state)
    attack.p["reasoning"] = lambda cyc, pack: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "smoke")
    assert daemon.query_one("SELECT count(*) FROM run")[0] == 0        # 未开训
    assert daemon.query_one("SELECT count(*) FROM phase_commit WHERE stage='bundle' AND target_id IS NOT NULL")[0] == 1
    daemon.conn.close()


def test_failed_eval_resume_not_registered(tmp_path):
    """codex 第2轮 BLOCKER 回归：eval 进程输出合法 metrics 但 exit≠0——崩在 final 改名后（finish-failed 前）
    → resume 读 exit 侧车、同一判定 → target failed，**不得**把失败进程续注册成成功。终库与不杀一致。"""
    lying_eval = "import sys; print('metric_value: 1@1=0.99'); sys.exit(1)"

    def _mk(path, work):
        d, s, c, a = _mk_env(path, work, eval_body=lying_eval)
        a.p["reasoning"] = lambda cyc, pack: {
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
        return d, s, c, a

    ref = str(tmp_path / "ref.sqlite")                     # 参照：不杀跑完（target failed(runtime)）
    d0, s0, _, a0 = _mk(ref, tmp_path / "wref")
    _bootstrap_attack(s0)
    SqliteAdvancer(s0, a0.compiler, lambda c, p: None, attack=a0).run_cycles(max_cycles=4)
    assert d0.query_one("SELECT count(*) FROM evaluation")[0] == 0
    d0.conn.close()

    path = str(tmp_path / "research.sqlite")               # 断点：eval final 已落、finish-failed 前炸
    d1, s1, _, a1 = _mk(path, tmp_path / "w")
    _bootstrap_attack(s1)
    orig_finish = a1.gate.gate_finish_build_target
    state_box = {"crashed": False}
    def crash_first_fail(**kw):
        if kw.get("status") == "failed" and not state_box["crashed"]:
            state_box["crashed"] = True
            raise SystemExit("SIM-KILL9-after-eval-final")
        return orig_finish(**kw)
    a1.gate.gate_finish_build_target = crash_first_fail
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    d1.conn.close()

    d2, s2, _, a2 = _mk(path, tmp_path / "w")              # 同 work_root 续跑：读 exit 侧车 → 同判失败
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    assert d2.query_one("SELECT count(*) FROM evaluation")[0] == 0     # 绝未注册
    d2.conn.close()
    assert _final_state(path) == _final_state(ref)


def test_judge_replay_safe(tmp_path):
    """codex 第2轮 SHOULD 回归：崩在 judge 已写 DECISION 与 gate 消费之间 → resume 不重调 judge
    （同 (target,kind,subject_hash) 复用既有裁决）。"""
    path = str(tmp_path / "research.sqlite")
    d1, s1, _, a1 = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(s1)
    judge_calls = []
    real_judge = a1.p["judge"]
    def counting_judge(cycle_id, bt_id, kind, sh):
        judge_calls.append(kind)
        return real_judge(cycle_id, bt_id, kind, sh)
    a1.p["judge"] = counting_judge
    orig_prog = a1.gate.gate_progress_build_target
    box = {"crashed": False}
    def crash_on_running(**kw):                            # judge(code) 已写 → progress(running) 前炸
        if kw.get("to") == "running" and not box["crashed"]:
            box["crashed"] = True
            raise SystemExit("SIM-KILL9-after-judge")
        return orig_prog(**kw)
    a1.gate.gate_progress_build_target = crash_on_running
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    assert judge_calls == ["bundle_code_review"]
    d1.conn.close()

    d2, s2, _, a2 = _mk_env(path, tmp_path / "w")          # 续跑：code review 裁决复用、只补 result review
    real_judge2 = a2.p["judge"]                            # 新实例自己的 judge（旧闭包挂在已关连接上）
    a2.p["judge"] = lambda cycle_id, bt_id, kind, sh: (judge_calls.append(kind), real_judge2(cycle_id, bt_id, kind, sh))[1]
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    assert judge_calls == ["bundle_code_review", "bundle_result_review"]   # code 未重调
    assert d2.query_one("SELECT status FROM build_target")[0] == "complete"
    d2.conn.close()


# ============ phase_commit conflict ============


# ============ phase_commit conflict ============
def test_attack_judge_fail_settles_target(tmp_path):
    """lockstep 回归（codex CP5.5 BLOCKER 的 attack 侧）：judge FAIL → target failed(review_failed)、
    轮正常收尾（Qn inconclusive），不确定性重试死循环。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    def fail_judge(cycle_id, bt_id, kind, sh):
        from orchestrator.ids import cnum
        with daemon.transaction() as conn:
            rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) VALUES (?,'audit',?,'success')",
                              (cnum(cycle_id), kind)).lastrowid
            conn.execute("INSERT INTO decision(cycle_id,actor,type,payload_json) VALUES (?,'judge',?,?)",
                         (cnum(cycle_id), kind, json.dumps({"build_target_id": bt_id, "review_kind": kind,
                                                            "round_no": 1, "verdict": "fail", "subject_hash": sh,
                                                            "runner_call_id": rc, "policy_hash": "ph"})))
    attack.p["judge"] = fail_judge
    attack.p["reasoning"] = lambda cyc, pack: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "review_failed")
    assert daemon.query_one("SELECT count(*) FROM evaluation")[0] == 0            # 测量整包不注册
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "inconclusive"
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done"
    daemon.conn.close()


def test_plan_reject_graceful_no_wedge(tmp_path):
    """步⑧ 全自动不楔死回归：非法抽象 plan（required 引用未声明 metric）→ 业务拒（decision(plan_rejected)
    + 零 target），轮正常收尾（Qn inconclusive、cycle done），**不 raise 死循环**。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)

    def bad_plan(cyc, pack):
        p = _plan_json()
        p["plan.json"]["build_target_required_metric"] = [
            {"target_key": "t1", "metric_id": "ghost", "metric_ver": 1}]   # 未声明的 metric
        return p
    attack.p["plan"] = bad_plan
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1                                                    # 轮跑完（未楔死）
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0    # 零 target
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM baseline")[0] == 0        # 未占坑（派生期拒，先于 gate）
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "inconclusive"
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done"
    # phase_commit(plan) 已落 → 重跑幂等不重做（终态）
    assert daemon.query_one("SELECT count(*) FROM phase_commit WHERE stage='plan'")[0] == 1
    daemon.conn.close()


@pytest.mark.parametrize("mutate,tag", [
    (lambda p: p.pop("protocol"), "缺 protocol 键（结构非法→曾裸 KeyError 楔死）"),
    (lambda p: p["targets"][0].__setitem__("target_kind", "exec"), "exec 目标（CP8.6 未接）"),
    (lambda p: p["targets"].append({**p["targets"][0], "seq": 2}), "同轮 canonical_key 重复"),
    (lambda p: (p["targets"].append({**p["targets"][0], "target_key": "t1", "seq": 2,
                                     "claim": {"canonical_key": "ck2", "slug": "s2"}})),
     "同轮 target_key 重复（codex BLOCKER：claims 覆盖错绑）"),
    (lambda p: p["targets"].append({**p["targets"][0], "target_key": "t2", "seq": 1,
                                    "claim": {"canonical_key": "ck2", "slug": "s2"}}),
     "同轮 seq 重复（撞 UNIQUE(cycle,seq)）"),
    (lambda p: p["metric_defs"].append({"metric_id": "m_acc", "version": 1, "name": "dup",
                                        "direction": "higher", "compute_spec_md": "x"}),
     "metric_id 重复（plan 内 join 键）"),
    (lambda p: p["build_target_required_metric"].__setitem__(0, {"target_key": "t1", "metric_id": "m_acc", "metric_ver": 9}),
     "required 版本与 metric_defs 不符"),
    (lambda p: p["build_target_required_metric"].__setitem__(0, {"target_key": "tX", "metric_id": "m_acc", "metric_ver": 1}),
     "required.target_key 悬挂引用"),
])
def test_illegal_plan_graceful_no_wedge(tmp_path, mutate, tag):
    """内审 SHOULD-高 回归：结构非法 / 未支持 kind / 同轮重复 canonical_key / required 版本不符——**任何**
    站不住的 plan 都 → 业务拒（decision(plan_rejected) + 零 target），轮正常收尾，**绝不 raise 楔死驱动循环**。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)

    def bad_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        mutate(p)
        return {"plan.json": p}
    attack.p["plan"] = bad_plan
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1, f"{tag}: 轮未跑完（疑楔死）"
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0, tag
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1, tag
    assert daemon.query_one("SELECT status FROM question WHERE id=1")[0] == "inconclusive", tag
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done", tag
    daemon.conn.close()


def test_reuse_protocol_missing_metric_binding_rejected(tmp_path):
    """codex BLOCKER-1 回归：复用既有 protocol（同名同 scope，_register_protocol 跳过 gate）但 required 的
    metric 未绑定到该 protocol_metric → 派生期 _PlanReject（否则跑到 gate_register_evaluation 才 I2 拒→楔死）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    with daemon.transaction() as conn:            # 模拟前轮：toy-proto@1 已注册，但只绑 acc(1@1)，未绑 f1(2@1)
        conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'toy-proto',?)",
                     (json.dumps({"dataset": "toy", "split": "holdout"}, sort_keys=True),))
        conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher')")
        conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (2,1,'f1','higher')")
        conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1)")

    def reuse_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        p["metric_defs"] = [{"metric_id": "m_f1", "version": 1, "name": "f1", "direction": "higher",
                             "compute_spec_md": "F1"}]                       # 复用 toy-proto@1，却要 f1（未绑）
        p["readout_rules"] = [{"metric_id": "m_f1", "metric_ver": 1, "rule_md": "越高越好"}]
        p["build_target_required_metric"] = [{"target_key": "t1", "metric_id": "m_f1", "metric_ver": 1}]
        return {"plan.json": p}
    attack.p["plan"] = reuse_plan
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1                                                        # 未楔死
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0        # 派生期拒、未占坑
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM baseline")[0] == 0
    daemon.conn.close()


def test_import_defer_rejected_not_silently_dropped(tmp_path):
    """CP8.4 回归：plan 含 import_defer（schema 合法、targets 必空）——CP8.6 未接线，**显式业务拒**留痕
    （decision(plan_rejected) 记录导入意图），不得静默当空 plan 丢意图。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)

    def defer_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        p["targets"] = []
        del p["protocol"], p["metric_defs"], p["readout_rules"]
        p["needs"], p["build_target_required_metric"] = [], []
        p["import_defer"] = {"reason_md": "须引入公认外部基线", "candidate_set_hash": "csh",
                             "selection_key": "sel", "policy_hash": "ph",
                             "placeholder_baseline_identity": {"canonical_key_draft": "ext-b",
                                                               "slug_draft": "ext", "identity_md": "外部基线"}}
        return {"plan.json": p}
    attack.p["plan"] = defer_plan
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1
    rej = daemon.query_one("SELECT payload_json FROM decision WHERE type='plan_rejected'")[0]
    assert "import_defer" in rej                                # 意图留痕，不静默
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0
    daemon.conn.close()


def test_bad_manifest_target_failed_not_wedge(tmp_path):
    """codex SHOULD-2 回归：Codex 产的 manifest 交叉核不过（换协议=旁路）→ 目标 failed(artifact_invalid)
    + 落 pc，轮正常收尾（不楔死）。resume 语义：已物化 manifest 校验不过属损毁 fail-loud（不在此测）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    real_bundle = attack.p["bundle"]

    def evil_bundle(cyc, pack):
        files = real_bundle(cyc, pack)
        files["execution_manifest.json"]["protocol_ref"]["protocol_ver"] = 99   # 换协议 → cross_check 拒
        return files
    attack.p["bundle"] = evil_bundle
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1                                                        # 未楔死
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "artifact_invalid")
    assert daemon.query_one("SELECT count(*) FROM evaluation")[0] == 0          # 未注册
    assert daemon.query_one("SELECT count(*) FROM phase_commit WHERE stage='bundle' AND target_id IS NOT NULL")[0] == 1
    assert daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "done"
    daemon.conn.close()


def test_eval_missing_required_metric_target_failed(tmp_path):
    """codex SHOULD-2/第2轮 BLOCKER 回归：eval 打印的 metric 不覆盖 required（gate_register_evaluation
    GateReject，**唯一**被转业务失败的 gate 调用点）→ 目标 failed(protocol_violation) + pc，不楔死。"""
    path = str(tmp_path / "research.sqlite")
    # eval 打印一个不在 required(1@1) 的 metric → required 未覆盖 → register GateReject
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w",
                                              eval_body="import sys, pathlib; print('metric_value: 7@1=0.5')")
    _bootstrap_attack(state)
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1
    assert daemon.query_one("SELECT status,failure_kind FROM build_target")[:2] == ("failed", "protocol_violation")
    assert daemon.query_one("SELECT count(*) FROM phase_commit WHERE stage='bundle' AND target_id IS NOT NULL")[0] == 1
    daemon.conn.close()


def test_statemachine_gatereject_still_fails_loud(tmp_path):
    """codex 第2轮 BLOCKER 回归（反面）：状态机类 GateReject（如 progress 非法转移）**不得**被吞成
    failed 终态——必须 fail loud 上抛（误终态化会掩埋恢复/篡改问题）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    from orchestrator.gate_sqlite import GateReject
    orig = attack.gate.gate_progress_build_target
    def corrupt_progress(**kw):                   # 模拟状态机损毁类拒（非 register_evaluation 调用点）
        raise GateReject("SIM-状态机损毁")
    attack.gate.gate_progress_build_target = corrupt_progress
    with pytest.raises(GateReject, match="状态机损毁"):
        SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert daemon.query_one("SELECT count(*) FROM build_target WHERE status='failed'")[0] == 0  # 未被误终态化
    daemon.conn.close()


def test_phase_commit_conflict_rejected(env):
    """staging 被改写后重做 → 同键异 hash → conflict 拒（§4.2.5 防误判已提交）。"""
    d = env["daemon"]
    env["adv"].run_cycles(max_cycles=4)
    from orchestrator.phase_commit import SqlitePhaseCommit
    pc = SqlitePhaseCommit(d)
    cid = env["state"].last_done_cycle().cycle_id
    assert pc.check_or_record(cycle_id=cid, stage="idea", target_id=None, artifact_hash="DRIFT") == "conflict"
    assert pc.check_or_record(cycle_id=cid, stage="reasoning", target_id=None, artifact_hash="h") == "new"
    assert pc.check_or_record(cycle_id=cid, stage="reasoning", target_id=None, artifact_hash="h") == "duplicate"


# ============ 管线强制先 ingest 再 complete ============
def test_pipeline_requires_current_obs(env, monkeypatch):
    """ingest 被跳过（模拟管线 bug）→ complete 前强制核当前口径观测 → RuntimeError，target 不 complete。"""
    import orchestrator.attack_stages as AS
    monkeypatch.setattr(OP, "ingest_observation", lambda *a, **k: 0)   # 空操作（bug 模拟）
    with pytest.raises(RuntimeError, match="先 ingest"):
        env["adv"].run_cycles(max_cycles=4)
    assert env["daemon"].query_one("SELECT count(*) FROM build_target WHERE status='complete'")[0] == 0

def test_exec_variant_of_legal_baseline(tmp_path):
    """步⑧ CP8.6：exec 目标——既有 legal baseline 上建变体（gate_claim_variant 自建 bt）→ manifest 驱动
    真训练/评估 → gate_register_variant 入池（baseline 身份不动，只本变体 legal）→ 真证据关问。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    # 预置：一个 legal baseline（'ck-base' 的 base 变体）+ 已注册协议/指标（模拟前轮 build 产物）
    with daemon.transaction() as conn:
        bl = conn.execute("INSERT INTO baseline(slug,canonical_key,identity_doc,born_cycle,status) "
                          "VALUES ('toy-b','ck-base','# base',1,'legal')").lastrowid
        conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,'base','{}','legal')", (bl,))
        conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'toy-proto',?)",
                     (json.dumps({"dataset": "toy", "split": "holdout"}, sort_keys=True),))
        conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher')")
        conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1)")

    def exec_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        p["targets"][0].update({"target_kind": "exec",
                                "claim": {"baseline_ref": "ck-base", "variant_key": "lr01", "config_json": {"lr": 0.01}}})
        return {"plan.json": p}
    attack.p["plan"] = exec_plan
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    # exec 变体入池 legal（baseline 身份不动——仍 1 个 baseline，2 个 variant）
    assert daemon.query_one("SELECT count(*) FROM baseline")[0] == 1
    assert daemon.query_one("SELECT status FROM variant WHERE variant_key='lr01'")[0] == "legal"
    assert json.loads(daemon.query_one("SELECT config_json FROM variant WHERE variant_key='lr01'")[0]) == {"lr": 0.01}
    assert daemon.query_one("SELECT target_kind, status FROM build_target")[0:2] == ("exec", "complete")
    # 真训练/评估 + 出厂测量 + 关问（exec 目标 → run.kind='exec'）
    assert daemon.query_one("SELECT status FROM run WHERE kind='exec'")[0] == "success"
    assert daemon.query_one("SELECT value FROM metric_result ORDER BY id DESC LIMIT 1")[0] == 0.93
    assert daemon.query_one("SELECT status FROM question WHERE text LIKE 'toy 基线%'")[0] == "answered"
    # gate_register_variant（非 register_baseline）：baseline 表未新增身份行
    assert daemon.query_one("SELECT count(*) FROM baseline WHERE canonical_key='ck-attack'")[0] == 0
    daemon.conn.close()


def test_exec_baseline_ref_not_legal_rejected(tmp_path):
    """exec baseline_ref 未解析到 legal baseline（含池空/首攻新家族）→ 派生期业务拒（须先 build），不楔死。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)

    def exec_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        p["targets"][0].update({"target_kind": "exec",
                                "claim": {"baseline_ref": "ck-nonexist", "variant_key": "v1", "config_json": {"lr": 1}}})
        return {"plan.json": p}
    attack.p["plan"] = exec_plan
    attack.p["reasoning"] = lambda c, pk: {
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": []}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=4)
    assert len(ids) == 1
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 0
    rej = daemon.query_one("SELECT payload_json FROM decision WHERE type='plan_rejected'")[0]
    assert "legal baseline" in rej
    daemon.conn.close()


def _exec_env(path, work):
    """exec 测试栈：预置 legal baseline 'ck-base'/base + 协议/指标，plan 产 exec 目标。
    **幂等**（续跑复用同 DB）：goal 已在 → 跳过创世/播种（对齐既有恢复测试的 resume 走 _mk_env 语义）。"""
    daemon, state, compiler, attack = _mk_env(path, work)
    if daemon.query_one("SELECT 1 FROM goal LIMIT 1") is None:
        _bootstrap_attack(state)
        with daemon.transaction() as conn:
            bl = conn.execute("INSERT INTO baseline(slug,canonical_key,identity_doc,born_cycle,status) "
                              "VALUES ('toy-b','ck-base','# base',1,'legal')").lastrowid
            conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,'base','{}','legal')", (bl,))
            conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'toy-proto',?)",
                         (json.dumps({"dataset": "toy", "split": "holdout"}, sort_keys=True),))
            conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher')")
            conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1)")

    def exec_plan(cyc, pack):
        p = _plan_json()["plan.json"]
        p["targets"][0].update({"target_kind": "exec",
                                "claim": {"baseline_ref": "ck-base", "variant_key": "lr01", "config_json": {"lr": 0.01}}})
        return {"plan.json": p}
    attack.p["plan"] = exec_plan
    return daemon, state, compiler, attack


def test_exec_crash_between_claim_and_terminal_recovers(tmp_path):
    """内审 BLOCKER 回归：exec kill-9 落在 gate_claim_variant（已提交，plan_ref=NULL 孤儿 bt）与终局
    _commit_plan_terminal 之间 → 稳定 work_root 续跑**不楔死**（复用自占分支补 plan_ref），终库与不杀一致。"""
    ref = str(tmp_path / "ref.sqlite")
    d0, s0, _, a0 = _exec_env(ref, tmp_path / "wref")
    SqliteAdvancer(s0, a0.compiler, lambda c, p: None, attack=a0).run_cycles(max_cycles=4)
    d0.conn.close()

    path = str(tmp_path / "research.sqlite")
    d1, s1, _, a1 = _exec_env(path, tmp_path / "w")
    orig = a1.gate.gate_claim_variant
    box = {"crashed": False}
    def crash_after_claim(**kw):                       # claim 提交后、终局 UPDATE 前炸
        r = orig(**kw)
        if not box["crashed"]:
            box["crashed"] = True
            raise SystemExit("SIM-KILL9-after-claim_variant")
        return r
    a1.gate.gate_claim_variant = crash_after_claim
    with pytest.raises(SystemExit):
        SqliteAdvancer(s1, a1.compiler, lambda c, p: None, attack=a1).run_cycles(max_cycles=4)
    assert d1.query_one("SELECT plan_ref FROM build_target WHERE target_kind='exec'")[0] is None
    d1.conn.close()

    d2, s2, _, a2 = _exec_env(path, tmp_path / "w")    # 同 work_root 续跑（真实生产重启）
    SqliteAdvancer(s2, a2.compiler, lambda c, p: None, attack=a2).run_cycles(max_cycles=4)
    d2.conn.close()
    assert _final_state(path) == _final_state(ref)     # 终库与不杀逐字节一致（复用自占、补 plan_ref、完成）
    c = db.connect(path)
    assert c.execute("SELECT status FROM variant WHERE variant_key='lr01'").fetchone()[0] == "legal"
    assert c.execute("SELECT status FROM build_target WHERE target_kind='exec'").fetchone()[0] == "complete"
    c.close()


def test_exec_replay_config_drift_rejected(tmp_path):
    """codex 第2轮 BLOCKER 回归：exec 自占复用严核——若崩溃后重放 plan 换了 config（身份漂移），
    自占放行逃生口不认（config 不符），转业务拒（不把新 plan_ref 写到旧 config 的 variant 上）。
    实测：手工造一个本 cycle pending+plan_ref NULL 的 exec 占坑（config A），plan 却要 config B → 拒。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _exec_env(path, tmp_path / "w")
    ci = int(state.open_or_resume_cycle().cycle_id[1:])   # 开一个 attack 轮
    state.set_route(f"c{ci}", "attack"); state.activate_question("q1")
    bl = daemon.query_one("SELECT id FROM baseline WHERE canonical_key='ck-base'")[0]
    with daemon.transaction() as conn:   # 手工残留：config={"lr":0.99} 的 pending exec 占坑（模拟前次崩溃、且 config 漂移）
        v = conn.execute("INSERT INTO variant(baseline_id,variant_key,config_json,status) VALUES (?,'lr01',?,'planned')",
                         (bl, json.dumps({"lr": 0.99}, sort_keys=True))).lastrowid
        conn.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id) "
                     "VALUES (?,1,'exec',1,'pending',?,?)", (ci, bl, v))
    # plan 要 lr=0.01（与残留 lr=0.99 不符）→ 派生自占核 config 不符 → _PlanReject
    attack._plan_stage(state.cycle(f"c{ci}"))
    rej = daemon.query_one("SELECT payload_json FROM decision WHERE type='plan_rejected'")
    assert rej is not None and "已占" in rej[0]
    assert daemon.query_one("SELECT count(*) FROM build_target WHERE plan_ref IS NOT NULL")[0] == 0
    daemon.conn.close()


def test_reasoning_selection_ineligible_question_no_wedge(tmp_path):
    """步⑧ CP8.8 回归（部署首跑实录）：attack 轮反复攻同一题、visit 达 max_inconclusive_per_question 上限后，
    Codex 仍选 next=该题 intent=attack（现对 attack 不可调度）→ **不楔死**：记 decision(selection_invalid) +
    改持久 terminate 干净收尾（否则持久化 reasoning 重启确定性重崩=永久楔死）。"""
    path = str(tmp_path / "research.sqlite")
    # 坏 train：attack 轮不产 answer → Qn 置 inconclusive、visit 增（本轮把 root 从 limit-1 顶到 limit）
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w",
                                              train_body="import sys; print('loss:1.0'); sys.exit(1)")
    _bootstrap_attack(state)
    limit = POLICY["question_guard"]["max_inconclusive_per_question"]
    with daemon.transaction() as conn:   # 预置 root：再 attack 一轮即达上限
        conn.execute("UPDATE question SET status='inconclusive', visit_count=? WHERE id=1", (limit - 1,))
    # reasoning 选回本题 attack——达上限后对 attack 不可调度（Codex 路由错误，编排器不代其重选）
    attack.p["reasoning"] = lambda cyc, pack: {
        "selection.json": {"next_question_id": "q1", "next_intent": "attack",
                           "scores": [{"question_id": "q1", "score": 0.5, "est_cost": 1.0}]}}
    ids = SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=3)
    assert len(ids) == 1                                        # 轮跑完（未楔死、无 traceback）
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='selection_invalid'")[0] == 1
    assert daemon.query_one("SELECT next_intent FROM cycle WHERE id=?", (int(ids[0][1:]),))[0] == "terminate"
    assert daemon.query_one("SELECT visit_count FROM question WHERE id=1")[0] == limit   # 达上限
    daemon.conn.close()
    # **真实重启**（全新进程/连接/组件，同 DB + 同 work_root：reasoning.json 仍在盘）：原生 bug 的永久楔死点
    # ——terminate 已持久 → 干净停、不重崩（内审 NIT：用全新实例更忠实复现跨进程楔死）
    d2, s2, c2, a2 = _mk_env(path, tmp_path / "w",
                             train_body="import sys; print('loss:1.0'); sys.exit(1)")
    assert SqliteAdvancer(s2, c2, lambda c, p: None, attack=a2).run_cycles(max_cycles=3) == []
    d2.conn.close()


def test_open_set_annotates_attack_ineligible(tmp_path):
    """CP8.8 Fix1：可调度问题集向 Codex 标注「attack 已达上限」的题（防它选不可调度的 attack）。"""
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    limit = POLICY["question_guard"]["max_inconclusive_per_question"]
    with daemon.transaction() as conn:
        conn.execute("UPDATE question SET status='inconclusive', visit_count=? WHERE id=1", (limit,))
        conn.execute("UPDATE cycle SET active_question_id=NULL WHERE id=1")
    c = state.open_or_resume_cycle()
    state.set_route(c.cycle_id, "decompose"); 
    with daemon.transaction() as conn:
        conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=?", (int(c.cycle_id[1:]),))
    pack = compiler.render(cycle_id=c.cycle_id, stage="reasoning")
    assert "attack 已达上限" in pack.anchor_md and "只可 decompose" in pack.anchor_md
    daemon.conn.close()
