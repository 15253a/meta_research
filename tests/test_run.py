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
    monkeypatch.setattr(R, "build_system",
                        lambda sr, wr, **kw: orig(sr, wr, runner_factory=_mock_factory([boot_attack])))
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path), "--max-cycles", "3"])
    assert rc == 2 and "CP7.4 装配 attack" in capsys.readouterr().out


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
