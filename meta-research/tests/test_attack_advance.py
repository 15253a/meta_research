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
from orchestrator.schemas import SchemaSet
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
OBS = POLICY["observation"]

TRAIN_CMD = [sys.executable, "-c",
             "import pathlib; print('loss: 1.0'); print('loss: 0.5'); print('loss: 0.2'); "
             "pathlib.Path('ckpt.bin').write_text('weights-v1'); print('wall_clock_sec: 1.0')"]
EVAL_CMD = [sys.executable, "-c", "print('loss: 0.2'); print('metric_value: 1@1=0.93')"]
SMOKE_CMD = [sys.executable, "-c", "print('loss: 0.9'); print('smoke ok')"]


def _target_spec(seq=1, ck="ck-attack"):
    return {"kind": "build", "seq": seq, "canonical_key": ck, "slug": "toy-b", "variant_key": "base",
            "identity_draft_md": "# toy baseline\n结构:线性", "repro_cmd": "python train.py",
            "train_cmd": TRAIN_CMD, "smoke_cmd": SMOKE_CMD, "eval_cmd": EVAL_CMD,
            "ckpt_name": "ckpt.bin", "eval_key": "fac", "target_set_hash": "tsh-1",
            "protocol_id": 1, "protocol_ver": 1, "required": [[1, 1]], "env_hash": "toy-env"}


def _providers(daemon):
    """确定性 providers（生产 = 真 Codex 会话；judge 写真 runner_call+DECISION 行）。"""
    def idea(cyc, pack):
        return {"idea_set.json": {"candidates": [
            {"candidate_id": "cand-1", "content_md": "线性基线", "audit_score": 8.0},
            {"candidate_id": "cand-2", "content_md": "弱想法", "audit_score": 3.0, "status": "failed"}],
            "selected_id": "cand-1"}}

    def plan(cyc, pack):
        return {"plan.json": {"protocol": {"id": 1, "version": 1}, "targets": [_target_spec()]}}

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

    return {"idea": idea, "plan": plan, "judge": judge, "reasoning": reasoning}


def _mk_env(path, work):
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="toy 研究目标")
    pool = PoolGate(daemon, open_gate_read_conn(path))
    obs_conn = db.connect(path)
    close_gate = SqliteGate(daemon, open_gate_read_conn(path), SchemaSet(SYSTEM_ROOT / "schemas"),
                            parser_suspect=lambda aid: OP.suspect_for_attempt(obs_conn, aid, OBS))
    attack = AttackStages(state=state, compiler=compiler, pool_gate=pool, close_gate=close_gate,
                          providers=_providers(daemon), obs_policy=OBS, work_root=str(work))
    return daemon, state, compiler, attack


def _bootstrap_attack(state):
    """创世：goal + 协议/指标注册 + root 问题（open）+ 上轮 selection 指向 attack root。"""
    state.create_goal(text="toy 研究目标", predicate_json={})
    with state.daemon.transaction() as conn:   # 协议注册（gate_new_protocol 语义；测试直插同构行）
        conn.execute("INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (1,1,'proto','{}')")
        conn.execute("INSERT INTO metric_def(id,version,name,direction) VALUES (1,1,'acc','higher')")
        conn.execute("INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (1,1,1,1)")
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
    # 真 evaluation + metric_result + 证据回溯 + 关问
    assert d.query_one("SELECT status FROM evaluation WHERE source='factory' AND eval_key='fac'")[0] == "success"
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
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    bad_spec = _target_spec()
    bad_spec["train_cmd"] = [sys.executable, "-c", "import sys; print('loss: 1.0'); sys.exit(1)"]
    attack.p["plan"] = lambda cyc, pack: {"plan.json": {"protocol": {"id": 1, "version": 1}, "targets": [bad_spec]}}
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
    daemon, state, compiler, attack = _mk_env(path, tmp_path / "w")
    _bootstrap_attack(state)
    bad = _target_spec()
    bad["smoke_cmd"] = [sys.executable, "-c", "import sys; sys.exit(2)"]
    attack.p["plan"] = lambda cyc, pack: {"plan.json": {"protocol": {"id": 1, "version": 1}, "targets": [bad]}}
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
    lying_eval = [sys.executable, "-c", "import sys; print('metric_value: 1@1=0.99'); sys.exit(1)"]

    def _mk(path, work):
        d, s, c, a = _mk_env(path, work)
        bad = _target_spec()
        bad["eval_cmd"] = lying_eval
        a.p["plan"] = lambda cyc, pack: {"plan.json": {"protocol": {"id": 1, "version": 1}, "targets": [bad]}}
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