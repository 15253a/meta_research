"""CP7.3 · run.py 全系统装配入口（M6）。

核心验收面：一条命令把**真组件 + StageProvider(注入 runner)** 接成全自动元循环并跑到停机；每个注入
组件（真状态机/编译器/发布器/StopController/precheck）端到端接对：assembly→run→落库+发布；重启同
work_root 续跑（goal 不重建）；durable 停机与全局等待端到端生效。多阶段 kill-9 恢复由 advancer 层
测试覆盖（同机制），本层验装配正确性。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import database as db
from orchestrator.interfaces import Artifact
from orchestrator.run import build_system
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)

_BOOT_TERMINATE = {
    "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题：EEG 有跨数据集通用规律吗？",
                               "local_key": "root"}]},
    "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                       "terminate_reason_md": "创世即达成（测试固定）"},
}


def _mock_factory(files_seq):
    """runner 工厂：每次 run_task 吐序列里的下一份 files（Artifact）。stage 取 pack.stage（不漂移）。"""
    box = {"seq": list(files_seq)}

    class MockRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            return Artifact(stage=context_pack.stage, files=box["seq"].pop(0), md="")
    return lambda td, pt: MockRunner()


# ============ 全装配端到端（reasoning-only 闭环）============
def test_build_and_run_bootstrap_terminate(tmp_path):
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    ids = sys.run(max_cycles=5)
    assert len(ids) == 1                                        # bootstrap 一轮 + terminate 停机
    # 真组件落库
    assert sys.daemon.query_one("SELECT count(*) FROM goal")[0] == 1
    assert sys.daemon.query_one("SELECT status FROM cycle WHERE id=?", (int(ids[0][1:]),))[0] == "done"
    assert sys.daemon.query_one("SELECT count(*) FROM question WHERE text LIKE '根问题%'")[0] == 1
    # StatusPublisher 端到端：阶段边界发布了卡
    card = tmp_path / "state" / "status_card.json"
    assert card.exists() and json.loads(card.read_text())["snapshot_cycle"] == ids[0]
    assert (tmp_path / "research.sqlite").exists()             # 真冻结库落盘


def test_resume_same_work_root_no_goal_recreate(tmp_path):
    """重启同 work_root 续跑：goal 不重建（幂等）、上轮 terminate → 本次 0 轮。"""
    build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE])).run(5)
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))   # 无需再调 runner
    assert sys2.run(max_cycles=5) == []                        # 已 terminate，无新轮
    assert sys2.daemon.query_one("SELECT count(*) FROM goal")[0] == 1   # goal 唯一（未重建）


# ============ 注入组件端到端接线 ============
def test_durable_stop_honored_end_to_end(tmp_path):
    """StopController 端到端：预置 global_stop → run() 启动即拒推进（provider 一次未调）。"""
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    with sys.daemon.transaction() as conn:
        conn.execute("INSERT INTO decision(actor,type,payload_json) VALUES "
                     "('orchestrator','global_stop','{\"reason\":\"budget_exhausted\"}')")
    assert sys.run(max_cycles=5) == []
    assert sys.last_stop_reason == "budget_exhausted"


def test_tau_score_floor_self_stop_end_to_end(tmp_path):
    """外审 SHOULD 回归：τ 判据①（分数衰退）经 run.py→run_cycles 的**轮后 check_after_round** 端到端
    自终止——bootstrap 造低分根+选 decompose（本会续跑），前沿全评分且 < floor → 停。"""
    boot_low = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "低价值根", "local_key": "root"}]},
                "selection.json": {"next_question_id": "root", "next_intent": "decompose",
                                   "scores": [{"question_id": "root", "score": 0.1, "est_cost": 1.0}]}}
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([boot_low]))
    sys.advancer.stop_controller.score_floor = 0.25            # 收紧 tau 到单轮即触发（测试注入）
    sys.advancer.stop_controller.consecutive_rounds = 1
    ids = sys.run(max_cycles=5)
    assert len(ids) == 1 and sys.last_stop_reason == "score_floor"   # 第 1 轮后自停、decompose 轮不开
    assert sys.daemon.query_one("SELECT count(*) FROM decision WHERE type='global_stop'")[0] == 1


def test_goal_body_from_db_not_edited_brief(tmp_path, monkeypatch):
    """外审 SHOULD 回归：重启时 goal_body_md 取 DB goal.text（权威），不受 goal_brief.md 编辑影响
    （防绕过 goal_amend 的静默漂移）。"""
    sys1 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    sys1.run(5)
    db_goal = sys1.daemon.query_one("SELECT text FROM goal WHERE id=1")[0]
    # 重启：即便 parse_goal_brief 返回被"编辑过"的 body，装配也用 DB 正文
    import orchestrator.run as R
    monkeypatch.setattr(R, "parse_goal_brief", lambda p: {"body_md": "【被篡改的目标】", "predicate_json": {},
                                                          "frontmatter": {}})
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    assert sys2.advancer.compiler.goal_body_md == db_goal      # 用 DB 正文，非篡改 brief
    assert "篡改" not in sys2.advancer.compiler.goal_body_md


def test_main_cli_smoke(tmp_path, monkeypatch, capsys):
    """main() argparse→build→run→print 全路径（注入 mock runner，不调真 Codex）。"""
    import orchestrator.run as R
    monkeypatch.setattr(R.CodexRunner, "__new__", lambda cls, **kw: object())   # 不会被调（terminate 前无 runner）
    # 用 build_system 的注入点：monkeypatch build_system 塞 mock runner
    orig = R.build_system
    monkeypatch.setattr(R, "build_system",
                        lambda sr, wr, **kw: orig(sr, wr, runner_factory=_mock_factory([_BOOT_TERMINATE])))
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path), "--max-cycles", "3"])
    assert rc == 0 and "推进 1 轮" in capsys.readouterr().out


def test_main_cli_attack_clean_error(tmp_path, monkeypatch, capsys):
    """外审 NIT 回归：attack 续轮 NotImplementedError → main 干净报 exit 2（非裸 traceback）。"""
    import orchestrator.run as R
    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack",
                                      "scores": [{"question_id": "root", "score": 0.9, "est_cost": 1.0}]}}
    orig = R.build_system
    monkeypatch.setattr(R, "build_system",           # attack=False：验证退化装配仍干净拒（CP8.4 后 attack 默认全装）
                        lambda sr, wr, **kw: orig(sr, wr, runner_factory=_mock_factory([boot_attack]), attack=False))
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path), "--max-cycles", "3"])
    assert rc == 2 and "尚未装配的组件" in capsys.readouterr().out


def test_stop_reason_print_prefers_block(tmp_path, monkeypatch, capsys):
    """外审 SHOULD 回归：全局等待时 CLI 停因输出阻断原因（不被 prior-terminate/idle 掩盖）。"""
    import orchestrator.run as R
    def factory(sr, wr, **kw):
        s = build_system(sr, wr, runner_factory=_mock_factory([]))   # 真 build_system + 塞 pending 请求
        with s.daemon.transaction() as conn:
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash) VALUES (1,1,'plan','pending','需数据','[]','rh')")
        return s
    monkeypatch.setattr(R, "build_system", factory)
    R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path), "--max-cycles", "3"])
    assert "文件请求" in capsys.readouterr().out


def test_global_wait_honored_end_to_end(tmp_path):
    """precheck 端到端：pending 文件请求 → run() 不发起新研究推进（provider 一次未调）。"""
    called = {"n": 0}

    def counting_factory(td, pt):
        class R:
            def run_task(self, **kw):
                called["n"] += 1
                return Artifact(stage="reasoning", files=_BOOT_TERMINATE, md="")
        return R()
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    with sys.daemon.transaction() as conn:                     # 造 pending 文件请求
        conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                     "request_hash) VALUES (1,1,'plan','pending','需数据','[]','rh')")
    assert sys.run(max_cycles=5) == []
    assert called["n"] == 0                                     # 阻断：一次 runner 都未调
    assert "文件请求" in sys.advancer.last_block_reason


# ============ CP8.4 · attack 全装配端到端（真子进程执行 + 真 judge 落库链）============
def _lazy_factory(items):
    """runner 工厂：items 元素为 dict（原样吐）或 callable(context_pack)→dict（吐前按当下 DB/staging 现算
    ——bundle 须回引 plan_slice_hash、attack reasoning 须引用真 metric_result id，均只在调用时可知）。"""
    box = {"seq": list(items)}

    class MockRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            item = box["seq"].pop(0)
            files = item(context_pack) if callable(item) else item
            return Artifact(stage=context_pack.stage, files=files, md="")
    return lambda td, pt: MockRunner()


def test_full_attack_flow_end_to_end(tmp_path):
    """步⑧步级验证①：run.py 装配的**全系统**跑通完整流程——bootstrap→attack（idea→plan[真 gate 注册
    协议/占坑]→bundle[manifest→harness 真子进程 smoke/train/eval]→双评审[JudgeProvider 真落库链]→
    注册入池→真证据关问）→terminate。runner 为脚本化 mock（Codex 替身），其余全为真组件。"""
    import sys as _sys
    import test_attack_advance as TA
    from orchestrator.manifest import canon_hash

    db_path = str(tmp_path / "research.sqlite")

    def bundle_env(pack):                       # 按 pack.target_id 读切片、回引 hash、产真 toy 代码
        conn = db.connect(db_path)
        slice_ = json.loads(conn.execute("SELECT plan_ref FROM build_target WHERE id=?",
                                         (int(pack.target_id),)).fetchone()[0])
        conn.close()
        return {"execution_manifest.json": {
                    "manifest_version": 1,
                    "target_ref": {"target_key": slice_["target_key"], "target_kind": "build",
                                   "seq": slice_["seq"], "plan_slice_hash": canon_hash(slice_)},
                    "protocol_ref": {"protocol_id": slice_["protocol_id"], "protocol_ver": slice_["protocol_ver"]},
                    "env_hash": "toy-env", "config_json": {"lr": 0.1},
                    "code_files": ["train.py", "eval.py", "smoke.py"],
                    "commands": {"smoke": {"argv": [_sys.executable, "{src}/smoke.py"]},
                                 "train": {"argv": [_sys.executable, "{src}/train.py"]},
                                 "eval": {"argv": [_sys.executable, "{src}/eval.py", "{ckpt}"]}},
                    "expected_outputs": {"checkpoint": "ckpt.bin"},
                    "repro_cmd_md": "python train.py 后 python eval.py <ckpt>"},
                "identity.md": "# toy 基线\n结构: 线性\n\n## 复现命令\npython train.py",
                "train.py": TA.TRAIN_OK, "eval.py": TA.EVAL_OK, "smoke.py": TA.SMOKE_OK}

    def attack_reasoning(pack):                 # 以真 metric_result 关问 + terminate
        conn = db.connect(db_path)
        mr = conn.execute("SELECT id FROM metric_result ORDER BY id DESC LIMIT 1").fetchone()[0]
        qid = conn.execute("SELECT active_question_id FROM cycle WHERE id=?",
                           (int(pack.cycle_id[1:]),)).fetchone()[0]
        conn.close()
        return {"answer.json": {"question_id": f"q{qid}", "verdict": "answered",
                                "evidence": [{"kind": "evaluation", "metric_result_id": f"mr{mr}",
                                              "note_md": "toy 基线 acc=0.93"}],
                                "answer_md": "以出厂测量关问"},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                                   "terminate_reason_md": "toy 目标已以真测量关问"}}

    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "toy 基线能到 0.9 吗",
                                              "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack", "scores": []}}
    verdict_pass = {"review_verdict.json": {"verdict": "pass", "issues": []}}
    seq = [boot_attack,                          # c1 bootstrap（reasoning）
           TA._idea_set(), TA._plan_json(),      # c2 attack：idea → plan（冻结 schema 真形态）
           bundle_env,                           # bundle 信封（manifest+代码）
           verdict_pass, verdict_pass,           # judge：code review → result review（经 JudgeProvider 落库）
           attack_reasoning]                     # 轮尾：真证据关问 + terminate
    sys_ = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory(seq))
    ids = sys_.run(max_cycles=6)
    assert len(ids) == 2                                         # bootstrap + attack 两轮后 terminate
    d = sys_.daemon
    # 全链断言：协议真注册 / 池 legal / 真测量 / 双评审真落库（JudgeProvider 链）/ 真证据关问
    assert d.query_one("SELECT count(*) FROM protocol WHERE name='toy-proto'")[0] == 1
    assert d.query_one("SELECT status FROM baseline WHERE canonical_key='ck-attack'")[0] == "legal"
    assert d.query_one("SELECT status, eval_key FROM evaluation WHERE source='factory'")[0:2] == ("success", "t1")
    assert d.query_one("SELECT value FROM metric_result ORDER BY id DESC LIMIT 1")[0] == 0.93
    assert d.query_one("SELECT count(*) FROM runner_call WHERE phase='audit' AND status='success'")[0] == 2
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 2
    assert d.query_one("SELECT status FROM question WHERE text LIKE 'toy 基线%'")[0] == "answered"
    assert d.query_one("SELECT count(*) FROM build_target WHERE status='complete'")[0] == 1
    # 执行是真子进程：checkpoint 文件真实存在且被登记
    ck = d.query_one("SELECT path, content_hash FROM checkpoint")
    assert Path(ck[0]).exists() and len(ck[1]) == 64
    assert sys_.last_stop_reason is None                         # 正常 terminate（非 τ/阻断）


def test_attack_assembly_optional_off(tmp_path):
    """attack=False 退化装配（诊断用）：遇 attack 续轮仍干净拒（NotImplementedError），不静默。"""
    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack", "scores": []}}
    sys_ = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory([boot_attack]),
                        attack=False)
    with pytest.raises(NotImplementedError):
        sys_.run(max_cycles=3)


def test_plan_reject_feedback_in_next_pack(tmp_path):
    """CP8.4 自纠环：plan 业务拒后，同一问题下一 attack 轮的 plan pack 含「上轮 plan 被拒原因」
    （冒烟实证：无此反馈真 Codex 连续 3 轮重复同一被拒 plan）。"""
    import test_attack_advance as TA
    from orchestrator.advancer import SqliteAdvancer
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w")
    TA._bootstrap_attack(state)

    def exec_plan(cyc, pack):                    # 被拒的 plan（exec 目标 CP8.6 未接）
        p = TA._plan_json()["plan.json"]
        p["targets"][0]["target_kind"] = "exec"
        p["targets"][0]["claim"] = {"baseline_ref": "b1", "variant_key": "v2", "config_json": {"lr": 1}}
        return {"plan.json": p}
    attack.p["plan"] = exec_plan
    attack.p["reasoning"] = lambda c, pk: {      # 拒后收尾：继续攻同一问题
        "selection.json": {"next_question_id": TA_root_qid(daemon), "next_intent": "attack", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=1)
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    # 下一轮同问题的 plan pack：拒因在锚区
    c2 = state.open_or_resume_cycle()
    state.set_route(c2.cycle_id, "attack")
    state.activate_question(TA_root_qid(daemon))
    pack = compiler.render(cycle_id=c2.cycle_id, stage="plan")
    assert "最近一次 plan 被拒原因" in pack.anchor_md and "只支持 build" in pack.anchor_md
    daemon.conn.close()


def TA_root_qid(daemon):
    return f"q{daemon.query_one('SELECT id FROM question ORDER BY id LIMIT 1')[0]}"


def test_plan_reject_feedback_suppressed_after_success(tmp_path):
    """codex SHOULD 回归：拒因之后本问题已有成功 plan（更晚 cycle 落过 build_target）→ 反馈不再渲染
    （陈旧拒因会在 CP8.6 后把本已合法的 exec/eval 引导走偏）。"""
    import test_attack_advance as TA
    from orchestrator.advancer import SqliteAdvancer
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w")
    TA._bootstrap_attack(state)
    box = {"n": 0}
    real_plan = attack.p["plan"]

    def flip_plan(cyc, pack):                    # 第 1 轮产被拒 plan（exec），第 2 轮产合法 build plan
        box["n"] += 1
        if box["n"] == 1:
            p = TA._plan_json()["plan.json"]
            p["targets"][0]["target_kind"] = "exec"
            p["targets"][0]["claim"] = {"baseline_ref": "b1", "variant_key": "v2", "config_json": {"lr": 1}}
            return {"plan.json": p}
        return real_plan(cyc, pack)
    attack.p["plan"] = flip_plan
    rq = TA_root_qid(daemon)
    sels = iter([{"selection.json": {"next_question_id": rq, "next_intent": "attack", "scores": []}},
                 {"answer.json": None, "selection.json": {"next_question_id": None, "next_intent": "terminate",
                                                          "scores": [], "terminate_reason_md": "done"}}])
    def reasoning(c, pk):
        files = dict(next(sels))
        if files.get("answer.json") is None:
            files.pop("answer.json", None)
        return files
    attack.p["reasoning"] = reasoning
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=3)
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 1     # 第 2 轮成功 plan 落了 target
    c3 = state.open_or_resume_cycle()
    state.set_route(c3.cycle_id, "attack")
    state.activate_question(rq)
    pack = compiler.render(cycle_id=c3.cycle_id, stage="plan")
    assert "plan 被拒原因" not in pack.anchor_md                # 已有更晚成功 plan → 反馈静默
    daemon.conn.close()
