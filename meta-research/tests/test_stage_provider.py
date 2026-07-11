"""CP7.2 · StageProvider 真 Codex→真组件阶段回调（M6）。

核心验收面：把 CodexRunner 一次会话 + 信封解析 + 逐产物 schema 校验 + artifact_parse 重试封成
(cyc, pack)→files；阶段必产在场、在场 optional 校验；结构非法重试并附反馈、用尽即 RunnerError；
用真 SqliteAdvancer 端到端（mock runner）跑通一轮，证适配器契约与组件对得上。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import hashlib
import json

import pytest
import yaml

from orchestrator import database as db
from orchestrator.interfaces import Artifact
from orchestrator.runner import RunnerError
from orchestrator.schemas import SchemaSet
from orchestrator.stage_provider import PlanReviewProvider, StageProvider

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
NO_BUDGET_POLICY = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")
SKILLS = {s: f"[skill:{s}]" for s in ("idea", "plan", "bundle", "reasoning")}

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
                       policy=NO_BUDGET_POLICY, system_prompt="SYS", skills=SKILLS, work_root=str(work))
    return sp, runner


def _pack(stage):
    sources = []
    if stage == "plan":
        plan_hash = hashlib.sha256(json.dumps(
            _PLAN, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        sources.append(f"staging:plan-draft:{plan_hash}")
    return NS(cycle_id="c1", stage=stage, target_id=None, anchor_md="", neighborhood_md="",
              retrieval_md="", refs=[], sources=sources)


_FIX = SYSTEM_ROOT / "tests" / "fixtures" / "valid"
_IDEA = json.loads((_FIX / "idea_set" / "wildidea.json").read_text(encoding="utf-8"))
_PLAN = json.loads((_FIX / "plan" / "attack.json").read_text(encoding="utf-8"))
_IMPORT_SEARCH_REQUEST = json.loads(
    (_FIX / "import_search_request" / "new_structure.json").read_text(encoding="utf-8"))


# ============ 正常产出（三阶段各直测 schema 校验路径）============
def test_idea_returns_validated_files(tmp_path):
    sp, _ = _provider([{"idea_set.json": _IDEA}], tmp_path)
    out = sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert out == {"idea_set.json": _IDEA}                     # 真 idea_set fixture 过 schema


def test_plan_returns_validated_files(tmp_path):
    sp, _ = _provider([{"plan.json": _PLAN}], tmp_path)
    out = sp.plan(NS(cycle_id="c1"), _pack("plan"))
    assert out == {"plan.json": _PLAN}


def test_plan_may_return_import_search_control_sidecar_alone(tmp_path):
    sp, _ = _provider(
        [{"import_search_request.json": _IMPORT_SEARCH_REQUEST}], tmp_path)
    out = sp.plan(NS(cycle_id="c1"), _pack("plan"))
    assert out == {"import_search_request.json": _IMPORT_SEARCH_REQUEST}


def test_plan_search_sidecar_cannot_coexist_with_plan(tmp_path):
    sp, runner = _provider([
        {"import_search_request.json": _IMPORT_SEARCH_REQUEST, "plan.json": _PLAN},
        {"plan.json": _PLAN},
    ], tmp_path)
    assert sp.plan(NS(cycle_id="c1"), _pack("plan")) == {"plan.json": _PLAN}
    assert "独占 files" in runner.skills_seen[1]


def test_plan_search_sidecar_rejects_stuck_direct_trigger(tmp_path):
    bad = {**_IMPORT_SEARCH_REQUEST, "trigger_kind": "stuck"}
    sp, runner = _provider([
        {"import_search_request.json": bad}, {"plan.json": _PLAN}], tmp_path)
    assert sp.plan(NS(cycle_id="c1"), _pack("plan")) == {"plan.json": _PLAN}
    assert "new_structure" in runner.skills_seen[1]


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
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path))
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "stage 漂移" in runner.skills_seen[1]               # 反馈含漂移原因


def test_transcript_purpose_unique_per_call(tmp_path):
    """调用序号递增（transcript 文件名唯一，P6 回放防覆盖）。"""
    seen = []
    sp = StageProvider(runner_factory=lambda td, pt: (seen.append(pt), MockRunner(
        [{"selection.json": _GOOD_SELECTION}]))[1], schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
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
    compiler = SqliteCompiler(db.connect(path), POLICY)
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
    compiler = SqliteCompiler(db.connect(path), POLICY)
    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "创世即终止"}}
    sp, _ = _provider([boot], tmp_path)
    adv = SqliteAdvancer(state, compiler, sp.reasoning)        # 直接把 provider.reasoning 注入
    ids = adv.run_cycles(max_cycles=3)
    assert len(ids) == 1                                       # bootstrap 轮跑通 + terminate
    assert state.cycle(ids[0]).status == "done"
    assert daemon.query_one("SELECT count(*) FROM question WHERE text='根问题'")[0] == 1   # 真落库


# ============ CP8.3 · bundle 阶段（passthrough）============
_MANIFEST = json.loads((_FIX / "execution_manifest" / "build_toy.json").read_text(encoding="utf-8"))


def _bundle_envelope():
    return {"execution_manifest.json": _MANIFEST, "identity.md": "# toy\n## 复现命令\npython train.py",
            "train.py": "print('t')", "eval.py": "print('e')", "cfg.json": {"lr": 0.1}}


def test_bundle_passthrough_all_files(tmp_path):
    """bundle 信封全量透传（代码文件名任意、不可枚举）——required 校验后原样返回，物化归组件。"""
    sp, _ = _provider([_bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert out == _bundle_envelope()                            # 含代码文件与 cfg.json（未被丢弃）


def test_bundle_missing_identity_retried_then_ok(tmp_path):
    bad = {k: v for k, v in _bundle_envelope().items() if k != "identity.md"}
    sp, runner = _provider([bad, _bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert "identity.md" in out
    assert "identity.md" in runner.skills_seen[1]               # 重试反馈里点名缺的文件


def test_bundle_blank_identity_rejected(tmp_path):
    bad = {**_bundle_envelope(), "identity.md": "   "}
    sp, _ = _provider([bad] * (POLICY["flow"]["retry"]["artifact_parse"] + 1), tmp_path)
    with pytest.raises(RunnerError, match="identity.md"):
        sp.bundle(NS(cycle_id="c1"), _pack("bundle"))


def test_bundle_invalid_manifest_retried_with_feedback(tmp_path):
    bad_manifest = {**_MANIFEST, "commands": {"eval": _MANIFEST["commands"]["eval"]}}   # build 缺 train/smoke
    sp, runner = _provider([{**_bundle_envelope(), "execution_manifest.json": bad_manifest},
                            _bundle_envelope()], tmp_path)
    out = sp.bundle(NS(cycle_id="c1"), _pack("bundle"))
    assert out["execution_manifest.json"] == _MANIFEST
    assert "execution_manifest.json" in runner.skills_seen[1]   # schema 错误反馈进重试 skill


# ============ plan answerability 独立评审装配 ============
def test_plan_review_provider_records_audit_call_and_durable_verdict(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([{
        "plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, decision_id = provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["verdict"] == "pass"
    payload = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE id=?", (decision_id,))[0])
    assert payload["round_no"] == 1 and payload["plan_hash"]
    assert daemon.query_one(
        "SELECT status,phase,purpose FROM runner_call WHERE id=?",
        (payload["runner_call_id"],)) == ("success", "audit", "plan_review")


def test_plan_review_provider_retries_bad_round_envelope(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        {"plan_review.json": {"verdict": "pass", "round_no": 2, "issues": []}},
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    review, _decision_id = provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))

    assert review["round_no"] == 1
    assert "期望 1" in runner.skills_seen[1]
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 1


def test_plan_review_provider_rejects_pack_for_different_plan(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([{
        "plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))
    pack = _pack("plan")
    pack.sources = ["staging:plan-draft:" + "0" * 64]

    with pytest.raises(ValueError, match="exact plan hash"):
        provider(NS(cycle_id="c1"), _PLAN, 1, pack)
    assert runner.skills_seen == []
    assert daemon.query_one("SELECT count(*) FROM runner_call WHERE purpose='plan_review'")[0] == 0


def test_plan_review_provider_does_not_retry_runner_failure(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    runner = MockRunner([
        RunnerError("transport down", failure_kind="transport"),
        {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}},
    ])
    provider = PlanReviewProvider(
        runner_factory=lambda _td, _purpose: runner, schemas=SCHEMAS,
        policy=NO_BUDGET_POLICY, system_prompt="SYS", skill="[skill:plan]",
        daemon=daemon, work_root=str(work))

    with pytest.raises(RunnerError, match="transport down") as error:
        provider(NS(cycle_id="c1"), _PLAN, 1, _pack("plan"))
    assert error.value.failure_kind == "transport"
    assert len(runner.skills_seen) == 1
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 0


# ============ CP8.3 · JudgeProvider（真 Codex 双评审装配）============
def _judge_env(tmp_path):
    """真 SQLite（goal/cycle/question/baseline/variant/build_target[plan_ref=切片]）+ staging 物化材料。"""
    from orchestrator.writedaemon import WriteDaemon
    import conftest
    path = str(tmp_path / "j.sqlite")
    seed = db.connect(path)
    conftest.seed_minimal(seed)                                  # goal/cycle1/question1/baseline1(variant1)
    seed.execute("INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
                 "VALUES (1,1,'build',3,'smoke',1,1,?)", (json.dumps({"target_key": "t1", "spec_md": "toy"}),))  # seq=3：seed_minimal 已占 1/2
    seed.commit(); seed.close()
    daemon = WriteDaemon(db.connect(path))
    bt_id = daemon.query_one("SELECT id FROM build_target WHERE seq=3")[0]
    src = tmp_path / "work" / "c1" / f"t{bt_id}" / "src"
    src.mkdir(parents=True)
    (src / "train.py").write_text("print('train')", encoding="utf-8")
    (src / "identity.md").write_text("# toy 身份", encoding="utf-8")
    smoke = tmp_path / "work" / "c1" / f"t{bt_id}" / "smoke"
    smoke.mkdir(parents=True)
    (smoke / "smoke-1.log").write_text("smoke ok", encoding="utf-8")
    return daemon, bt_id, tmp_path / "work"


def _judge(daemon, work, scripted):
    from orchestrator.stage_provider import JudgeProvider
    runner = MockRunner(scripted)
    jp = JudgeProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="SYS", skill="[skill:judge]", daemon=daemon, work_root=str(work))
    return jp, runner


def test_judge_records_runner_call_and_decision(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, runner = _judge(daemon, work, [{"review_verdict.json": {"verdict": "pass", "issues": []}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    rc = daemon.query_one("SELECT phase, purpose, status FROM runner_call ORDER BY id DESC LIMIT 1")
    assert rc == ("audit", "bundle_code_review", "success")
    payload = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE actor='judge' ORDER BY id DESC LIMIT 1")[0])
    assert payload["verdict"] == "pass" and payload["subject_hash"] == "sh-1"
    assert payload["build_target_id"] == bt_id and payload["round_no"] == 1
    assert payload["runner_call_id"] is not None and len(payload["policy_hash"]) == 64
    # subject 材料真装配：物化代码 + smoke transcript 进 anchor（judge 只读材料、不碰仓库）
    # MockRunner 未存 pack；用 _subject_md 直接断言装配面
    md = jp._subject_md("c1", bt_id, "bundle_code_review")
    assert "train.py" in md and "smoke ok" in md and "toy" in md


def test_judge_fail_verdict_recorded_with_round_increment(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, _ = _judge(daemon, work, [
        {"review_verdict.json": {"verdict": "pass", "issues": []}},
        {"review_verdict.json": {"verdict": "fail", "issues": [{"item": "指标硬编码", "why": "eval 不读 ckpt"}]}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    jp("c1", bt_id, "bundle_code_review", "sh-2")               # 产物变 → 重评审 → round_no 递增
    rows = daemon.query("SELECT json_extract(payload_json,'$.round_no'), json_extract(payload_json,'$.verdict') "
                        "FROM decision WHERE actor='judge' ORDER BY id")
    assert rows == [(1, "pass"), (2, "fail")]


def test_judge_invalid_verdict_retries_then_raises(tmp_path):
    daemon, bt_id, work = _judge_env(tmp_path)
    bad = {"review_verdict.json": {"verdict": "fail", "issues": []}}     # fail 必至少一条 issue（schema）
    jp, runner = _judge(daemon, work, [bad] * (POLICY["flow"]["retry"]["artifact_parse"] + 1))
    with pytest.raises(RunnerError, match="review_verdict"):
        jp("c1", bt_id, "bundle_code_review", "sh-1")
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 0   # 非法裁决不落库
    assert daemon.query_one("SELECT count(*) FROM runner_call")[0] == 0
    assert "should be non-empty" in runner.skills_seen[-1]      # schema 反馈进重试


def test_judge_result_review_subject_includes_logs(tmp_path):
    """result review 材料装配：train/eval log 尾部 + checkpoint 哈希 + identity（log 仅供评审读）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        rid = conn.execute("INSERT INTO run(cycle_id,variant_id,build_target_id,kind,status) "
                           "VALUES (1,1,?,'build','success')", (bt_id,)).lastrowid
        conn.execute("INSERT INTO checkpoint(variant_id,ckpt_key,path,content_hash,hash_alg,produced_by_run) "
                     "VALUES (1,'final-r1','/x/ckpt.bin','ab12','sha256',?)", (rid,))
    t_dir = work / "c1" / f"t{bt_id}"
    (t_dir / f"run{rid}").mkdir(parents=True)
    (t_dir / f"run{rid}" / "train.log").write_text("loss: 0.2", encoding="utf-8")
    (t_dir / f"eval{rid}").mkdir(parents=True)
    (t_dir / f"eval{rid}" / "eval.log").write_text("metric_value: 1@1=0.93", encoding="utf-8")
    jp, _ = _judge(daemon, work, [{"review_verdict.json": {"verdict": "pass", "issues": []}}])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "loss: 0.2" in md and "metric_value: 1@1=0.93" in md and "ab12" in md and "toy 身份" in md


def test_judge_eval_only_subject_includes_existing_checkpoint_and_attempt_log(tmp_path):
    """eval-only 无 run：结果评审仍必须看到既有 checkpoint hash 与本 target attempt log。"""
    daemon, _bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        bt_id = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,variant_id,"
            "evaluation_id,eval_action,attempt_purpose,plan_ref) "
            "VALUES (1,1,'eval',4,'running',1,1,'append_attempt','repro_eval','{}')").lastrowid
        aid = conn.execute(
            "INSERT INTO evaluation_attempt(evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status) "
            "VALUES (1,1,?,2,'repro_eval','running')", (bt_id,)).lastrowid
    eval_dir = work / "c1" / f"t{bt_id}" / f"eval-a{aid}"
    eval_dir.mkdir(parents=True)
    (eval_dir / "eval.log").write_text(
        "loss: 0.1\nmetric_value: 1@1=0.95\n", encoding="utf-8")
    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "checkpoint ck1" in md and "h" in md
    assert "metric_value: 1@1=0.95" in md and "loss: 0.1" in md


def test_judge_import_subject_uses_import_snapshot_layout(tmp_path):
    daemon, _bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        candidate_id = conn.execute(
            "INSERT INTO external_candidate(question_id,discovered_cycle,trigger_kind,"
            "trigger_snapshot_hash,need_summary,source_kind,canonical_uri,revision,"
            "search_snapshot_json,search_snapshot_hash,rank,retrieved_at) "
            "VALUES (1,1,'sota_reference','th','need','repo','https://example.invalid/r','rev','{}','sh',0,'t')"
        ).lastrowid
        license_id = conn.execute(
            "INSERT INTO license_review(candidate_id,decision,actor,license_scope_json,decided_cycle,policy_hash) "
            "VALUES (?,'allow','auto','{\"allow_eval\":true,\"allow_publish_pool\":true}',1,'ph')",
            (candidate_id,)).lastrowid
        baseline_id = conn.execute(
            "INSERT INTO baseline(slug,canonical_key,status,provenance,license_status,born_cycle) "
            "VALUES ('ext','ext','planned','external_import','allow',1)").lastrowid
        variant_id = conn.execute(
            "INSERT INTO variant(baseline_id,variant_key,config_json,status) "
            "VALUES (?,'imported','{}','planned')", (baseline_id,)).lastrowid
        external_import_id = conn.execute(
            "INSERT INTO external_import(question_id,candidate_id,action,action_cycle,candidate_set_hash,"
            "selection_key,policy_hash,license_decision_snapshot_hash,license_review_id,baseline_id) "
            "VALUES (1,?,'selected_for_materialization',1,'csh','rank_asc','ph','lh',?,?)",
            (candidate_id, license_id, baseline_id)).lastrowid
        bt_id = conn.execute(
            "INSERT INTO build_target(cycle_id,question_id,target_kind,seq,status,baseline_id,variant_id,plan_ref) "
            "VALUES (1,1,'import',4,'smoke',?,?,?)",
            (baseline_id, variant_id, json.dumps({"frozen": True}))).lastrowid
    clone = work / f"import{external_import_id}" / "clone"
    clone.mkdir(parents=True)
    (clone / "model.py").write_text("print('imported-code')", encoding="utf-8")
    smoke = work / f"import{external_import_id}" / "smoke"
    smoke.mkdir(parents=True)
    (smoke / "smoke-1.log").write_text("import smoke ok", encoding="utf-8")

    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_code_review")

    assert "model.py" in md and "imported-code" in md
    assert "import smoke ok" in md and '"frozen": true' in md.lower()


def test_judge_unknown_kind_fails_loud(tmp_path):
    """codex SHOULD 回归：拼错的 review_kind 当场拒（否则写任意 decision.type，下游永远看不到期望评审）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, _ = _judge(daemon, work, [])
    with pytest.raises(ValueError, match="review_kind"):
        jp("c1", bt_id, "bundle_code_reviw", "sh-1")            # typo kind
    assert daemon.query_one("SELECT count(*) FROM runner_call")[0] == 0


def test_smoke_latest_is_numeric_order(tmp_path):
    """codex SHOULD 回归：smoke-10.log 数值序 > smoke-2.log（字典序会取错「最新」）。
    attack subject 构造与 judge 材料装配共用 harness.latest_smoke_log 同一口径。"""
    from orchestrator.harness import latest_smoke_log
    daemon, bt_id, work = _judge_env(tmp_path)
    smoke = work / "c1" / f"t{bt_id}" / "smoke"
    (smoke / "smoke-2.log").write_text("OLD-2", encoding="utf-8")
    (smoke / "smoke-10.log").write_text("NEW-10", encoding="utf-8")
    assert latest_smoke_log(smoke).name == "smoke-10.log"
    jp, _ = _judge(daemon, work, [])
    assert "NEW-10" in jp._subject_md("c1", bt_id, "bundle_code_review")


def test_judge_result_review_includes_code_and_full_metrics(tmp_path):
    """codex BLOCKER 回归：result review 材料须含**代码**（判据「据结果反查代码」）与 metric_value 行
    **全量**（不受 log tail 截断）。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    with daemon.transaction() as conn:
        rid = conn.execute("INSERT INTO run(cycle_id,variant_id,build_target_id,kind,status) "
                           "VALUES (1,1,?,'build','success')", (bt_id,)).lastrowid
    t_dir = work / "c1" / f"t{bt_id}"
    (t_dir / f"eval{rid}").mkdir(parents=True)
    big_log = ("filler\n" * 2000) + "metric_value: 1@1=0.93\n" + ("post\n" * 600)   # metric 行不在尾部 2000 字符内
    (t_dir / f"eval{rid}" / "eval.log").write_text(big_log, encoding="utf-8")
    jp, _ = _judge(daemon, work, [])
    md = jp._subject_md("c1", bt_id, "bundle_result_review")
    assert "print('train')" in md                               # 代码在场（反查代码）
    assert "metric_value: 1@1=0.93" in md                       # metric 行全量显式列出，未被 tail 截掉


# ============ CP8.5 · sidecar→file_request 桥 ============
_SIDECAR = {"summary_md": "需要 EEG 数据集", "items": [{
    "kind": "dataset", "desc": "EEG 原始数据", "expected_files": ["eeg.zip"],
    "attempted_paths": ["/data/eeg"], "failure_reason": "无读取权限", "dest_hint": "input/user_provided/"}]}


def test_sidecar_bridged_to_file_request(tmp_path):
    """已接桥：sidecar → 桥落请求单 → StageBlockedOnResources（信封其余产物弃用——工人自述缺文件）。"""
    from orchestrator.interfaces import StageBlockedOnResources
    seen = {}

    def bridge(stage, request, cyc):
        seen.update(stage=stage, request=request, cyc=cyc)
        return 42
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR}])
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    cyc = NS(cycle_id="c1", question_id="q1")
    with pytest.raises(StageBlockedOnResources) as ei:
        sp.reasoning(cyc, _pack("reasoning"))
    assert ei.value.request_id == 42 and ei.value.stage == "reasoning"
    assert seen["stage"] == "reasoning" and seen["request"] == _SIDECAR and seen["cyc"] is cyc


def test_sidecar_bridge_reject_feeds_retry(tmp_path):
    """桥拒（sidecar 非法/quota 尽）→ 计入重试反馈（工人可修正或放弃 sidecar），有界后 fail loud。"""
    from orchestrator.notify import FileRequestReject

    def bridge(stage, request, cyc):
        raise FileRequestReject("quota 已达上限")   # 只有业务拒进重试；其余异常 fail loud（内审 NIT）
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR},
                         {"selection.json": _GOOD_SELECTION}])                    # 第 2 次放弃 sidecar
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    out = sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert out == {"selection.json": _GOOD_SELECTION}
    assert "sidecar 被拒" in runner.skills_seen[1] and "quota" in runner.skills_seen[1]


def test_judge_rejects_sidecar_with_feedback(tmp_path):
    """判官不受理 sidecar（评审材料已全量给出）——反馈重试，不静默丢弃。"""
    daemon, bt_id, work = _judge_env(tmp_path)
    jp, runner = _judge(daemon, work, [
        {"review_verdict.json": {"verdict": "pass", "issues": []}, "resource_request.json": _SIDECAR},
        {"review_verdict.json": {"verdict": "pass", "issues": []}}])
    jp("c1", bt_id, "bundle_code_review", "sh-1")
    assert "不受理 resource_request" in runner.skills_seen[1]
    assert daemon.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 1


def test_sidecar_bridge_nonbusiness_error_fails_loud(tmp_path):
    """codex NIT 回归（关键异常边界钉牢）：桥抛**非** FileRequestReject（如 DB 损坏）→ 原样 fail loud，
    不进 artifact_parse 重试（重试会把损坏掩成「工人产物问题」）。"""
    import sqlite3 as _sqlite3

    def bridge(stage, request, cyc):
        raise _sqlite3.OperationalError("database disk image is malformed")
    runner = MockRunner([{"selection.json": _GOOD_SELECTION, "resource_request.json": _SIDECAR},
                         {"selection.json": _GOOD_SELECTION}])   # 若误重试会吃到第 2 项
    sp = StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=NO_BUDGET_POLICY,
                       system_prompt="S", skills=SKILLS, work_root=str(tmp_path), file_request_bridge=bridge)
    with pytest.raises(_sqlite3.OperationalError, match="malformed"):
        sp.reasoning(NS(cycle_id="c1", question_id=None), _pack("reasoning"))
    assert len(runner.skills_seen) == 1                          # 未重试（原样上抛）
