"""CP1.3 资产层接口桩自验：Gate 两级校验 / StateStore 状态机语义 / Compiler 确定性 / Runner 信封。

Runner 用测试替身（METARESEARCH_CODEX_BIN 指向 fake_codex_*.sh），不联网、不花 token；
真 codex 冒烟属 M0 端到端验收（CP1.4）。

local_key 解析已内化到 StateStore（§6.10：同事务解析后再校验），apply_tree_ops -> None；
测试经公开状态（cycle.next_question_id / questions 集）取真实 id，不穿线私有映射。
"""
import json
import stat

import pytest
import yaml

from conftest import FIXTURES_DIR, SYSTEM_ROOT, load_json

from orchestrator.compiler import StubCompiler, StubCtx, StubRecall
from orchestrator.gate import ArtifactIndex, StubGate
from orchestrator.goalbrief import GoalBriefError, parse_goal_brief
from orchestrator.interfaces import Artifact, ContextPack, RecallSpec, Selection
from orchestrator.runner import CodexRunner, RunnerError
from orchestrator.schemas import ARTIFACT_SCHEMA_MAP, SchemaSet
from orchestrator.statestore import InMemoryStateStore


@pytest.fixture()
def policy() -> dict:
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture()
def store(policy) -> InMemoryStateStore:
    s = InMemoryStateStore(policy)
    s.create_goal(text="toy 目标", predicate_json={"kind": "metric_comparison"})
    return s


@pytest.fixture()
def gate(store, tmp_path) -> StubGate:
    return StubGate(SchemaSet(SYSTEM_ROOT / "schemas"), ArtifactIndex(), store, tmp_path)


def bootstrap_root(store, cycle, local_key="root") -> str:
    """建首题并经 persist_selection 解析出真实 id（公开路径，不碰私有映射）。

    create_root 有 bootstrap 轮型护栏，故此处先置 route=bootstrap；测试随后可自行改 route。
    """
    store.set_route(cycle.cycle_id, "bootstrap")
    store.apply_tree_ops(cycle.cycle_id, [
        {"op": "create_root", "local_key": local_key, "text": "根问题"}])
    store.persist_selection(cycle.cycle_id, Selection(next_question_id=local_key, next_intent="attack"))
    return store.cycles[cycle.cycle_id].next_question_id


# ---------------------------------------------------------------------------
# Gate：三级校验（M0 前两级真、业务放过）
# ---------------------------------------------------------------------------

def test_gate_accepts_valid_bundle_fixture(gate, store):
    payload = load_json(FIXTURES_DIR / "valid/bundle_target/exec_complete_fake.json")
    cycle = store.open_or_resume_cycle()
    res = gate.commit(Artifact(stage="bundle", files={"bundle_target.json": payload}, md="正文"),
                      cycle_id=cycle.cycle_id, stage="bundle", target_id="t1")
    assert res.ok, res.errors
    assert (gate.artifacts_root / f"cycles/{cycle.cycle_id}/artifacts/t1.bundle_target.json").exists()
    assert "mr:t1#accuracy@1:aggregate" in gate.index.metric_result_ids


def test_gate_rejects_schema_violation(gate, store):
    bad = load_json(FIXTURES_DIR / "invalid/bundle_target/fake_not_synthetic.json")
    cycle = store.open_or_resume_cycle()
    res = gate.commit(Artifact(stage="bundle", files={"bundle_target.json": bad}, md=""),
                      cycle_id=cycle.cycle_id, stage="bundle", target_id="t9")
    assert not res.ok and any("synthetic" in e for e in res.errors)
    assert any(d["actor"] == "gate" and d["type"] == "reject" for d in store.decisions)


def test_gate_rejects_unknown_filename(gate, store):
    cycle = store.open_or_resume_cycle()
    res = gate.commit(Artifact(stage="idea", files={"mystery.json": {}}, md=""),
                      cycle_id=cycle.cycle_id, stage="idea")
    assert not res.ok and any("未知产物文件名" in e for e in res.errors)


def test_gate_level2_rejects_dangling_refs(gate, store):
    """引用完整性真做：answer 悬空 metric / tree_ops 悬空 parent / selection 悬空 id / plan 悬空 evaluation_id。"""
    cycle = store.open_or_resume_cycle()
    root = bootstrap_root(store, cycle)

    answer = {"question_id": root, "verdict": "answered", "answer_md": "结论",
              "evidence": [{"kind": "evaluation", "metric_result_id": "mr:不存在"}]}
    res = gate.commit(Artifact(stage="reasoning", files={"answer.json": answer}, md=""),
                      cycle_id=cycle.cycle_id, stage="reasoning")
    assert not res.ok and any("metric_result_id" in e for e in res.errors)

    ops = {"ops": [{"op": "spawn_question", "kind": "diagnosis",
                    "parent_question_id": "q404", "text": "挂在不存在的问题下"}]}
    res = gate.commit(Artifact(stage="reasoning", files={"tree_ops.json": ops}, md=""),
                      cycle_id=cycle.cycle_id, stage="reasoning")
    assert not res.ok and any("parent_question_id 不存在" in e for e in res.errors)

    sel = {"next_question_id": "q404", "next_intent": "attack", "scores": []}
    res = gate.commit(Artifact(stage="reasoning", files={"selection.json": sel}, md=""),
                      cycle_id=cycle.cycle_id, stage="reasoning")
    assert not res.ok and any("next_question_id" in e for e in res.errors)

    plan = load_json(FIXTURES_DIR / "valid/plan/attack.json")   # 其 eval target 引用不存在的格子
    res = gate.commit(Artifact(stage="plan", files={"plan.json": plan}, md=""),
                      cycle_id=cycle.cycle_id, stage="plan")
    assert not res.ok and any("evaluation_id 无既有格子" in e for e in res.errors)


def test_artifact_map_covers_all_process_files():
    """Gate 校验对象清单；resource_request.json（sidecar）故意不在其中（§6.11 非 Gate 产物）。"""
    expected = {
        "idea_set.json", "idea_set.draft.json", "idea_audit.json",
        "plan.json", "plan_review.json", "bundle_target.json",
        "answer.json", "tree_ops.json", "selection.json",
    }
    assert set(ARTIFACT_SCHEMA_MAP) == expected


def test_gate_rejects_sidecar_as_artifact(gate, store):
    """sidecar 不入研究库：驱动器忘摘出时 Gate 必须显式拒（防污染，§6.11）。"""
    sidecar = load_json(FIXTURES_DIR / "valid/resource_request/dataset.json")
    cycle = store.open_or_resume_cycle()
    res = gate.commit(Artifact(stage="plan", files={"resource_request.json": sidecar}, md=""),
                      cycle_id=cycle.cycle_id, stage="plan")
    assert not res.ok and any("sidecar 非 Gate 产物" in e for e in res.errors)


def test_gate_rejects_bundle_target_key_mismatch(gate, store):
    payload = load_json(FIXTURES_DIR / "valid/bundle_target/eval_complete_fake.json")
    cycle = store.open_or_resume_cycle()
    res = gate.commit(Artifact(stage="bundle", files={"bundle_target.json": payload}, md=""),
                      cycle_id=cycle.cycle_id, stage="bundle", target_id="t999")
    assert not res.ok and any("不一致" in e for e in res.errors)


# ---------------------------------------------------------------------------
# StateStore：主链路 + 拒绝判据
# ---------------------------------------------------------------------------

def test_statestore_bootstrap_to_aggregate_lifecycle(store):
    # bootstrap：建首题（local_key 由 persist_selection 同事务解析）
    c1 = store.open_or_resume_cycle()
    store.set_route(c1.cycle_id, "bootstrap")
    root = bootstrap_root(store, c1)
    assert root in store.questions
    store.mark_cycle_done(c1.cycle_id)

    # decompose：父问题释放 + 子 dep pending → 父不可调度、子可调度
    store.activate_question(root)
    c2 = store.open_or_resume_cycle()
    assert c2.cycle_id != c1.cycle_id
    store.set_route(c2.cycle_id, "decompose")
    store.apply_tree_ops(c2.cycle_id, [
        {"op": "add_children", "parent_question_id": root,
         "children": [{"local_key": "a", "text": "子 A"}, {"local_key": "b", "text": "子 B"}]}])
    qa, qb = sorted(q.qid for q in store.questions.values() if q.parent_id == root)
    assert store.questions[root].status == "open"          # active→open 释放
    assert store.questions[root].visit_count == 0          # 等待非尝试、不增 visit
    assert not store.is_schedulable(root)                  # pending dep 排除调度
    assert store.is_schedulable(qa) and store.is_schedulable(qb)
    # 同事务的 selection 可用 local_key 选子题
    store.persist_selection(c2.cycle_id, Selection(next_question_id="a", next_intent="attack"))
    assert store.cycles[c2.cycle_id].next_question_id == qa
    store.mark_cycle_done(c2.cycle_id)

    # 子问题闭环 → 父回可调度集（聚合轮解锁）
    store.activate_question(qa)
    ca = store.open_or_resume_cycle()
    store.close_question(ca.cycle_id, qa, "answered", [{"kind": "evaluation", "metric_result_id": "mr:x"}], "答")
    store.mark_cycle_done(ca.cycle_id)
    assert not store.is_schedulable(root)                  # 还剩 qb pending
    store.activate_question(qb)
    cb = store.open_or_resume_cycle()
    store.close_question(cb.cycle_id, qb, "refuted", [{"kind": "evaluation", "metric_result_id": "mr:y"}], "驳")
    store.mark_cycle_done(cb.cycle_id)
    assert store.is_schedulable(root)                      # 全 satisfied → 父解锁


def test_statestore_inconclusive_and_attack_limit(store, policy):
    c = store.open_or_resume_cycle()
    qid = bootstrap_root(store, c)
    limit = policy["question_guard"]["max_inconclusive_per_question"]
    for _ in range(limit):
        store.activate_question(qid)
        store.mark_inconclusive(qid)
    assert store.questions[qid].visit_count == limit
    assert not store.is_schedulable(qid, for_intent="attack")     # 到限对 attack 不可选
    assert store.is_schedulable(qid, for_intent="decompose")      # 仍可作 decompose 对象
    with pytest.raises(ValueError, match="不可调度"):
        store.persist_selection(c.cycle_id, Selection(next_question_id=qid, next_intent="attack"))


def test_statestore_selection_rejections(store):
    c = store.open_or_resume_cycle()
    with pytest.raises(ValueError, match="next_question_id 必须为 null"):
        store.persist_selection(c.cycle_id, Selection(next_question_id="q1", next_intent="terminate"))
    with pytest.raises(ValueError, match="缺失或不存在"):
        store.persist_selection(c.cycle_id, Selection(next_question_id="q404", next_intent="attack"))


def test_statestore_scores_writeback_resolves_local_keys(store):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "bootstrap")
    store.apply_tree_ops(c.cycle_id, [{"op": "create_root", "local_key": "r", "text": "q"}])
    store.persist_selection(c.cycle_id, Selection(
        next_question_id="r", next_intent="attack",
        scores=[{"question_id": "r", "score": 0.7, "est_cost": 2.5}]))   # scores 里的 local_key 也解析
    qid = store.cycles[c.cycle_id].next_question_id
    assert store.questions[qid].score == 0.7 and store.questions[qid].est_cost == 2.5


def test_statestore_terminal_frozen_and_prune_rules(store):
    c = store.open_or_resume_cycle()
    qid = bootstrap_root(store, c)
    store.activate_question(qid)
    store.close_question(c.cycle_id, qid, "answered", [{"kind": "human", "human_ref": "d1"}], "答")
    with pytest.raises(ValueError, match="终态"):
        store.close_question(c.cycle_id, qid, "refuted", [{"kind": "human", "human_ref": "d2"}], "改口")
    with pytest.raises(ValueError, match="剪枝"):
        store.apply_tree_ops(c.cycle_id, [{"op": "propose_prune", "question_id": qid, "reason_md": "不该剪终态"}])


def test_statestore_route_and_dep_rejections(store):
    c = store.open_or_resume_cycle()
    with pytest.raises(ValueError, match="7 形态"):
        store.set_route(c.cycle_id, "terminate")           # terminate 不是 route（§2.3）
    qid = bootstrap_root(store, c)
    with pytest.raises(ValueError, match="dep 目标不存在"):
        store.record_question_dep(qid, dep_type="question", target="q404")
    with pytest.raises(ValueError, match="自依赖"):
        store.record_question_dep(qid, dep_type="question", target=qid)


def test_statestore_goal_amend_route_guard(store):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "attack")
    with pytest.raises(ValueError, match="goal_amend"):
        store.apply_tree_ops(c.cycle_id, [{"op": "amend_goal", "new_goal_text": "新", "rationale_md": "r"}])


def test_statestore_op_route_guards(store):
    """§4.2.4 轮型护栏：create_root 限 bootstrap、add_children 限 decompose + active 父、
    goal_retarget 限 goal_amend。"""
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    store.set_route(c.cycle_id, "attack")
    with pytest.raises(ValueError, match="bootstrap"):
        store.apply_tree_ops(c.cycle_id, [{"op": "create_root", "text": "第二个根"}])
    with pytest.raises(ValueError, match="decompose 轮"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "add_children", "parent_question_id": root,
             "children": [{"local_key": "x", "text": "子"}]}])
    store.set_route(c.cycle_id, "decompose")
    with pytest.raises(ValueError, match="active"):   # 父问题未选中（open）不可分解
        store.apply_tree_ops(c.cycle_id, [
            {"op": "add_children", "parent_question_id": root,
             "children": [{"local_key": "x", "text": "子"}]}])
    with pytest.raises(ValueError, match="goal_amend"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "spawn_question", "kind": "goal_retarget", "parent_question_id": None, "text": "新root"}])
    store.set_route(c.cycle_id, "goal_amend")
    with pytest.raises(ValueError, match="bootstrap"):    # goal 改版新 root 走 goal_retarget、不走 create_root
        store.apply_tree_ops(c.cycle_id, [{"op": "create_root", "text": "第二个根"}])


def test_statestore_goal_amend_spawn_cap_and_decompose_source(store, policy):
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    # add_children 的子问题 source=decompose（DDL 枚举）
    store.activate_question(root)
    store.set_route(c.cycle_id, "decompose")
    store.apply_tree_ops(c.cycle_id, [
        {"op": "add_children", "parent_question_id": root,
         "children": [{"local_key": "a", "text": "子 A"}]}])
    qa = next(q.qid for q in store.questions.values() if q.parent_id == root)
    assert store.questions[qa].source == "decompose"
    # goal_amend 轮 spawn 受 max_spawn_from_goal_amend
    store.set_route(c.cycle_id, "goal_amend")
    cap = policy["goal_amend"]["max_spawn_from_goal_amend"]
    for i in range(cap):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "spawn_question", "kind": "goal_retarget", "parent_question_id": None,
             "text": f"改版新题{i}"}])
    with pytest.raises(ValueError, match="max_spawn_from_goal_amend"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "spawn_question", "kind": "goal_retarget", "parent_question_id": None, "text": "超限"}])


def test_statestore_apply_tree_ops_rolls_back_on_failure(store):
    """事务语义（§4.2.5）：批内后续 op 被拒 → 整批回滚，不留半写。"""
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    store.activate_question(root)
    store.set_route(c.cycle_id, "decompose")
    before_q = set(store.questions)
    before_deps = len(store.deps)
    with pytest.raises(ValueError, match="不在封闭词表"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "add_children", "parent_question_id": root,
             "children": [{"local_key": "a", "text": "子 A"}]},
            {"op": "reopen_question", "question_id": root},   # 非法 op → 整批回滚
        ])
    assert set(store.questions) == before_q                  # 子问题未留下
    assert len(store.deps) == before_deps                     # dep 未留下
    assert store.questions[root].status == "active"           # 父问题释放被回滚
    # 回滚后同批 local_key 也不可解析
    with pytest.raises(ValueError, match="缺失或不存在"):
        store.persist_selection(c.cycle_id, Selection(next_question_id="a", next_intent="attack"))


def test_statestore_tree_guards_on_add_children(store, policy):
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    store.activate_question(root)
    store.set_route(c.cycle_id, "decompose")
    too_many = [{"local_key": f"k{i}", "text": f"子{i}"}
                for i in range(policy["tree_guard"]["max_children_per_node"] + 1)]
    with pytest.raises(ValueError, match="max_children_per_node"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "add_children", "parent_question_id": root, "children": too_many}])


def test_statestore_applicability_bindings(store):
    """§4.2.4 全量绑定：answer 存在 + 回看题 source=revalidate 且 parent=answer 所属问题。"""
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    store.activate_question(root)
    aid = store.close_question(c.cycle_id, root, "answered", [{"kind": "human", "human_ref": "d1"}], "答")

    with pytest.raises(ValueError, match="answer 不存在"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "mark_answer_applicability", "answer_id": "a404",
             "status": "still_applicable", "rationale_md": "悬空"}])
    with pytest.raises(ValueError, match="revalidate"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "mark_answer_applicability", "answer_id": aid,
             "status": "needs_revalidation", "rationale_md": "回指非回看题", "spawned_question_ref": root}])
    # 合法：同批 spawn revalidate 题（parent=被回看 answer 所属问题）+ 回指 local_key
    store.apply_tree_ops(c.cycle_id, [
        {"op": "spawn_question", "local_key": "rv", "kind": "revalidate",
         "parent_question_id": root, "text": "复核题"},
        {"op": "mark_answer_applicability", "answer_id": aid,
         "status": "needs_revalidation", "rationale_md": "要回看", "spawned_question_ref": "rv"}])
    assert store.applicability[-1]["status"] == "needs_revalidation"


def test_statestore_review_per_cycle_guard(store, policy):
    c = store.open_or_resume_cycle()
    root = bootstrap_root(store, c)
    store.activate_question(root)
    aid = store.close_question(c.cycle_id, root, "answered", [{"kind": "human", "human_ref": "d"}], "答")
    limit = policy["answer_review"]["max_reviews_per_cycle"]
    for _ in range(limit):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "mark_answer_applicability", "answer_id": aid,
             "status": "still_applicable", "rationale_md": "复核"}])
    with pytest.raises(ValueError, match="max_reviews_per_cycle"):
        store.apply_tree_ops(c.cycle_id, [
            {"op": "mark_answer_applicability", "answer_id": aid,
             "status": "still_applicable", "rationale_md": "超限"}])


# ---------------------------------------------------------------------------
# Compiler：确定性 + manifest 溯源 + 切片
# ---------------------------------------------------------------------------

def _mk_compiler(store, tmp_path, policy):
    return StubCompiler(store, ArtifactIndex(), policy, goal_body_md="目标正文", cycles_root=tmp_path)


def test_compiler_deterministic_and_manifest(store, tmp_path, policy):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "bootstrap")
    comp = _mk_compiler(store, tmp_path, policy)
    p1 = comp.render(cycle_id=c.cycle_id, stage="reasoning")
    p2 = comp.render(cycle_id=c.cycle_id, stage="reasoning")
    assert p1.pack_hash == p2.pack_hash and p1.anchor_md == p2.anchor_md
    manifest = json.loads((tmp_path / f"cycles/{c.cycle_id}/context_pack/reasoning.manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["pack_hash"] == p1.pack_hash
    assert any(s.startswith("policy:") for s in manifest["sources"])


def test_compiler_plan_gets_normalized_selected_only(store, tmp_path, policy):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "attack")
    c.question_id = bootstrap_root(store, c)
    comp = _mk_compiler(store, tmp_path, policy)
    idea = load_json(FIXTURES_DIR / "valid/idea_set/wildidea.json")
    comp.index.register(cycle_id=c.cycle_id, stage="idea", target_id=None,
                        filename="idea_set.json", payload=idea)
    pack = comp.render(cycle_id=c.cycle_id, stage="plan")
    assert "core_claim" in pack.anchor_md and "wildidea_extra" not in pack.anchor_md


def test_compiler_bundle_slice_scopes_to_target(store, tmp_path, policy):
    c = store.open_or_resume_cycle()
    store.set_route(c.cycle_id, "attack")
    c.question_id = bootstrap_root(store, c)
    comp = _mk_compiler(store, tmp_path, policy)
    plan = load_json(FIXTURES_DIR / "valid/plan/attack.json")
    comp.index.register(cycle_id=c.cycle_id, stage="plan", target_id=None,
                        filename="plan.json", payload=plan)
    pack = comp.render(cycle_id=c.cycle_id, stage="bundle", target_id="t1")
    slice_ = json.loads(pack.anchor_md.split("```json\n")[1].split("\n```")[0])
    assert slice_["target"]["target_key"] == "t1"
    assert all(m["target_key"] == "t1" for m in slice_["required_metrics"])
    assert slice_["protocol"]["name"] == plan["protocol"]["name"]


def test_stub_ctx_recall_shapes():
    assert isinstance(StubCtx().fetch("execution_log:1"), str)
    assert StubRecall().query(RecallSpec(query="任意", stage="plan")) == []


# ---------------------------------------------------------------------------
# Runner：信封解析与失败路径（测试替身，不花 token）
# ---------------------------------------------------------------------------

def _runner_with_fake(tmp_path, fake_name, monkeypatch) -> CodexRunner:
    fake = SYSTEM_ROOT / "tests" / fake_name
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("METARESEARCH_CODEX_BIN", str(fake))
    monkeypatch.setenv("METARESEARCH_RUNNER_TIMEOUT_S", "30")
    return CodexRunner(transcripts_dir=tmp_path / "transcripts")


def _pack() -> ContextPack:
    return ContextPack(cycle_id="c1", stage="idea", target_id=None,
                       anchor_md="锚", neighborhood_md="", retrieval_md="")


def test_runner_parses_envelope(tmp_path, monkeypatch):
    runner = _runner_with_fake(tmp_path, "fake_codex_ok.sh", monkeypatch)
    art = runner.run_task(system_prompt="系统", skill="技能", context_pack=_pack())
    assert art.files["probe.json"]["ok"] is True and art.md == "中文正文"
    assert list((tmp_path / "transcripts").glob("*.prompt.md")), "prompt 快照须归档（P6 回放）"


def test_runner_raises_on_unparseable_envelope(tmp_path, monkeypatch):
    runner = _runner_with_fake(tmp_path, "fake_codex_bad.sh", monkeypatch)
    with pytest.raises(RunnerError, match="信封不可解析"):
        runner.run_task(system_prompt="系统", skill="技能", context_pack=_pack())


def test_runner_raises_on_nonzero_exit(tmp_path, monkeypatch):
    runner = _runner_with_fake(tmp_path, "fake_codex_fail.sh", monkeypatch)
    with pytest.raises(RunnerError, match="进程失败"):
        runner.run_task(system_prompt="系统", skill="技能", context_pack=_pack())


# ---------------------------------------------------------------------------
# goalbrief：启动契约（唯一实现在 orchestrator，tests 反向 import）
# ---------------------------------------------------------------------------

def test_goalbrief_parses_repo_sample():
    parsed = parse_goal_brief(SYSTEM_ROOT / "input" / "goal_brief.md")
    assert parsed["predicate_json"]["metric_id"] == "accuracy" and parsed["body_md"]


def test_goalbrief_rejects_missing_predicate(tmp_path):
    bad = tmp_path / "goal_brief.md"
    bad.write_text("---\ntitle: 没有谓词\n---\n\n正文\n", encoding="utf-8")
    with pytest.raises(GoalBriefError, match="predicate_json"):
        parse_goal_brief(bad)


def test_goalbrief_rejects_no_frontmatter(tmp_path):
    bad = tmp_path / "goal_brief.md"
    bad.write_text("# 只有正文\n", encoding="utf-8")
    with pytest.raises(GoalBriefError, match="frontmatter"):
        parse_goal_brief(bad)
