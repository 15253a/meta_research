"""CP7.2 · StageProvider 真 Codex→真组件阶段回调（M6）。

核心验收面：把 CodexRunner 一次会话 + 信封解析 + 逐产物 schema 校验 + artifact_parse 重试封成
(cyc, pack)→files；阶段必产在场、在场 optional 校验；结构非法重试并附反馈、用尽即 RunnerError；
用真 SqliteAdvancer 端到端（mock runner）跑通一轮，证适配器契约与组件对得上。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import json

import pytest
import yaml

from orchestrator import database as db
from orchestrator.interfaces import Artifact
from orchestrator.runner import RunnerError
from orchestrator.schemas import SchemaSet
from orchestrator.stage_provider import StageProvider

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")
SKILLS = {s: f"[skill:{s}]" for s in ("idea", "plan", "reasoning")}

_GOOD_SELECTION = {"next_question_id": None, "next_intent": "terminate", "scores": [],
                   "terminate_reason_md": "目标达成"}


class MockRunner:
    """脚本化 runner：按调用次序吐预置 Artifact（或抛 RunnerError）。记录收到的 skill 供断言反馈。"""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.skills_seen = []

    def run_task(self, *, system_prompt, skill, context_pack):
        self.skills_seen.append(skill)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return Artifact(stage=context_pack.stage, files=item, md="")


def _provider(scripted, work):
    runner = MockRunner(scripted)
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS,
                       policy=POLICY, system_prompt="SYS", skills=SKILLS, work_root=str(work))
    return sp, runner


def _pack(stage):
    return NS(cycle_id="c1", stage=stage, target_id=None, anchor_md="", neighborhood_md="",
              retrieval_md="", refs=[])


_FIX = SYSTEM_ROOT / "tests" / "fixtures" / "valid"
_IDEA = json.loads((_FIX / "idea_set" / "wildidea.json").read_text(encoding="utf-8"))
_PLAN = json.loads((_FIX / "plan" / "attack.json").read_text(encoding="utf-8"))


# ============ 正常产出（三阶段各直测 schema 校验路径）============
def test_idea_returns_validated_files(tmp_path):
    sp, _ = _provider([{"idea_set.json": _IDEA}], tmp_path)
    out = sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert out == {"idea_set.json": _IDEA}                     # 真 idea_set fixture 过 schema


def test_plan_returns_validated_files(tmp_path):
    sp, _ = _provider([{"plan.json": _PLAN}], tmp_path)
    out = sp.plan(NS(cycle_id="c1"), _pack("plan"))
    assert out == {"plan.json": _PLAN}


def test_reasoning_returns_validated_files(tmp_path):
    sp, _ = _provider([{"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}          # 必产在场、过 schema


def test_optional_files_passed_through_when_present(tmp_path):
    files = {"selection.json": {"next_question_id": "q1", "next_intent": "decompose", "scores": []},
             "tree_ops.json": {"ops": [{"op": "add_children", "parent_question_id": "q1",
                                        "children": [{"text": "子", "local_key": "c"}]}]}}
    sp, _ = _provider([files], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id="q1"), _pack("reasoning"))
    assert set(out) == {"selection.json", "tree_ops.json"}     # optional 在场 → 一并返回


# ============ 阶段必产缺失 → 重试 ============
def test_missing_required_file_retries_then_succeeds(tmp_path):
    sp, runner = _provider([{"md_only": 1}, {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "缺阶段必产文件" in runner.skills_seen[1]           # 第 2 次调用带上了缺失反馈


# ============ schema 非法 → 重试并附反馈 ============
def test_schema_invalid_retries_with_feedback(tmp_path):
    bad = {"selection.json": {"next_intent": "terminate"}}     # 缺 next_question_id/scores
    sp, runner = _provider([bad, {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "schema 校验失败" in runner.skills_seen[1]          # 反馈含 schema 错误


def test_retries_exhausted_raises(tmp_path):
    bad = {"selection.json": {"bogus": 1}}
    n = POLICY["flow"]["retry"]["artifact_parse"] + 1
    sp, runner = _provider([bad] * n, tmp_path)
    with pytest.raises(RunnerError, match="重试.*用尽"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert len(runner.skills_seen) == n                        # 恰好 N+1 次调用（首次 + N 重试）


def test_runner_error_counts_as_retry(tmp_path):
    sp, _ = _provider([RunnerError("超时"), {"selection.json": _GOOD_SELECTION}], tmp_path)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}          # 进程失败也走重试


def test_stage_drift_retries(tmp_path):
    """外审 SHOULD 回归：文件结构对但 envelope stage 漂移 → 计入重试（审计/回放语义）。"""
    class DriftRunner(MockRunner):
        def run_task(self, *, system_prompt, skill, context_pack):
            self.skills_seen.append(skill)
            item = self.scripted.pop(0)
            st, files = item                                   # (stage, files) 元组：显式指定 envelope stage
            return Artifact(stage=st, files=files, md="")
    runner = DriftRunner([("plan", {"selection.json": _GOOD_SELECTION}),      # 漂移
                          ("reasoning", {"selection.json": _GOOD_SELECTION})])
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path))
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "stage 漂移" in runner.skills_seen[1]               # 反馈含漂移原因


def test_transcript_purpose_unique_per_call(tmp_path):
    """调用序号递增（transcript 文件名唯一，P6 回放防覆盖）。"""
    seen = []
    sp = StageProvider(runner_factory=lambda td, pt: (seen.append(pt), MockRunner(
        [{"selection.json": _GOOD_SELECTION}]))[1], schemas=SCHEMAS, policy=POLICY,
        system_prompt="S", skills=SKILLS, work_root=str(tmp_path))
    sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert seen == ["reasoning-n1", "reasoning-n2"]


# ============ sidecar fail-loud（内审 SHOULD：不静默丢弃资源请求）============
def test_resource_request_sidecar_fails_loud(tmp_path):
    files = {"selection.json": _GOOD_SELECTION, "resource_request.json": {"x": 1}}
    sp, _ = _provider([files], tmp_path)
    with pytest.raises(RunnerError, match="resource_request"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))


# ============ answer.json 语义边界（内审 #2：reasoning-only 轮不因幻觉 answer 误关问）============
def test_spurious_answer_in_bootstrap_does_not_close(tmp_path):
    """StageProvider 只保证 answer.json 结构合法、透传；语义由组件把关——advancer 的 reasoning-only 轮
    根本不读 answer.json（关问经 attack 轮 gate_close_question 的 I3 证据闸），故幻觉 answer 不误关。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    from orchestrator.writedaemon import WriteDaemon
    ans = json.loads((_FIX / "answer" / sorted(p.name for p in (_FIX / "answer").iterdir())[0]).read_text("utf-8"))
    path = str(tmp_path / "sa.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY); state.create_goal(text="g", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="g")
    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "止"},
            "answer.json": ans}                               # 幻觉 answer 混进 bootstrap 产物
    sp, _ = _provider([boot], tmp_path)
    SqliteAdvancer(state, compiler, sp.reasoning).run_cycles(max_cycles=2)
    assert daemon.query_one("SELECT count(*) FROM answer")[0] == 0   # 无问题被关（answer 被 advancer 忽略）


# ============ 与真 SqliteAdvancer 端到端（mock runner）============
def test_end_to_end_with_real_advancer(tmp_path):
    """真 SqliteAdvancer + 真 SqliteCompiler + StageProvider(mock runner) 跑通 bootstrap 创世轮：
    证 (cyc,pack)→files 契约与组件对得上（组件渲 pack→调 provider→落库）。"""
    from orchestrator.advancer import SqliteAdvancer
    from orchestrator.compiler_sqlite import SqliteCompiler
    from orchestrator.statestore_sqlite import SQLiteStateStore
    from orchestrator.writedaemon import WriteDaemon
    path = str(tmp_path / "e.sqlite")
    daemon = WriteDaemon(db.connect(path))
    state = SQLiteStateStore(daemon, POLICY)
    state.create_goal(text="EEG 通用规律", predicate_json={})
    compiler = SqliteCompiler(db.connect(path), POLICY, goal_body_md="EEG 通用规律")
    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "创世即终止"}}
    sp, _ = _provider([boot], tmp_path)
    adv = SqliteAdvancer(state, compiler, sp.reasoning)        # 直接把 provider.reasoning 注入
    ids = adv.run_cycles(max_cycles=3)
    assert len(ids) == 1                                       # bootstrap 轮跑通 + terminate
    assert state.cycle(ids[0]).status == "done"
    assert daemon.query_one("SELECT count(*) FROM question WHERE text='根问题'")[0] == 1   # 真落库
