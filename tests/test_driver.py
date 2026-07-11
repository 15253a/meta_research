"""CP1.4 驱动器单测：用 ScriptedRunner（照本宣科的假 Runner）测元循环接线，不花 token。

覆盖：bootstrap→attack happy path（route 特化/假执行标记/收口/terminate）、
plan 评审两轮不过（阶段失败=轮正常收尾）、sidecar 摘出（M0 桩语义）。
真 codex 端到端属 M0 验收（scripts/run_m0_acceptance.py），不在单测内。
"""
import json


from conftest import SYSTEM_ROOT

from orchestrator.driver import M0Driver
from orchestrator.interfaces import Artifact


class ScriptedRunner:
    """按脚本顺序吐产物的 Runner 替身（全局单实例：driver 的多个调用点共享一份脚本队列）。"""

    def __init__(self, script):
        self.script = list(script)   # [(files, md), ...]
        self.calls = []

    def run_task(self, *, system_prompt, skill, context_pack):
        assert self.script, f"脚本耗尽（stage={context_pack.stage}）"
        files, md = self.script.pop(0)
        self.calls.append(context_pack.stage)
        return Artifact(stage=context_pack.stage,
                        files=json.loads(json.dumps(files)), md=md)


def make_driver(tmp_path, script) -> M0Driver:
    runner = ScriptedRunner(script)
    return M0Driver(SYSTEM_ROOT, work_root=tmp_path,
                    runner_factory=lambda d, tag: runner)


BOOTSTRAP_FILES = {
    "tree_ops.json": {"ops": [{"op": "create_root", "local_key": "root",
                               "text": "toy 根问题：逻辑回归能否达标？"}]},
    "selection.json": {"next_question_id": "root", "next_intent": "attack", "scores": []},
}

IDEA_DRAFT = {
    "need_innovation": False,
    "candidates": [{
        "candidate_id": "c1", "generation_path": "bypass",
        "audit_mapping": {"source_domain": "教材基线", "target_domain": "toy 二分类",
                          "object_mapping": "逻辑回归→基线A", "shared_relations": "线性可分假设一致"},
        "core_claim": "逻辑回归可达标", "mechanism": "线性判别面",
        "assumptions": ["协方差近似相等"],
        "min_falsifiable_experiment": "固定划分训练并测 accuracy，<0.90 证伪",
        "novelty_type": "表征结构",
        "novelty_status": "联网粗查已启用·文献级待人工验证",
    }],
    "novelty_refs": [],
}

IDEA_AUDIT = {
    "audit_scores": [{
        "candidate_id": "c1",
        "scores": {"structural_depth": 7, "domain_distance": 2, "applicability": 9,
                   "novelty": 1, "unexpectedness": 1, "non_obviousness": 2},
        "decision": "pass", "rationale": "适配 NEED，SD 过线",
    }],
    "selected_id": "c1",
}

PLAN_BUILD = {
    "needs": [{"need_id": "n1", "statement_md": "验证达标", "source": "min_falsifiable_experiment"}],
    "reuse_evidence": [],
    "targets": [{
        "target_key": "t1", "target_kind": "build", "seq": 1, "critical": True,
        "budget_estimate": 1.0, "spec_md": "构建逻辑回归基线并出厂评估",
        "claim": {"canonical_key": "logreg-gauss2d", "slug": "logreg-gauss2d"},
    }],
    "protocol": {"name": "toy-gauss-cls", "version": 1,
                 "scope_spec": {"data": "双高斯 seed=42", "split": "70/30"},
                 "smoke_md": "n=64 子样本跑通"},
    "metric_defs": [{"metric_id": "accuracy", "version": 1, "name": "准确率",
                     "direction": "higher", "compute_spec_md": "留出集一致率"}],
    "readout_rules": [{"metric_id": "accuracy", "metric_ver": 1, "rule_md": "≥0.90 达标"}],
    "build_target_required_metric": [{"target_key": "t1", "metric_id": "accuracy", "metric_ver": 1}],
}

REVIEW_PASS = {"plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}
REVIEW_FAIL = {"plan_review.json": {"verdict": "fail", "round_no": 1,
                                    "issues": [{"item": "覆盖", "why": "n1 无指标"}]}}


def reasoning_close(metric_result_id):
    return {
        "answer.json": {"question_id": "q1", "verdict": "answered",
                        "answer_md": "达标（M0 假执行，source=fake，仅流程契约）",
                        "evidence": [{"kind": "evaluation", "metric_result_id": metric_result_id}]},
        "tree_ops.json": {"ops": []},
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                           "terminate_reason_md": "目标谓词满足（toy），等待人类确认"},
    }


def test_driver_happy_path_two_cycles(tmp_path):
    script = [
        (BOOTSTRAP_FILES, "首轮出题"),
        ({"idea_set.draft.json": IDEA_DRAFT}, "生成"),
        ({"idea_audit.json": IDEA_AUDIT}, "判官"),
        ({"plan.json": PLAN_BUILD}, "计划"),
        (REVIEW_PASS, "评审"),
        (reasoning_close("mr:c2:t1#accuracy@1:aggregate"), "收口"),
    ]
    d = make_driver(tmp_path, script)
    done = d.run_cycles(5)
    assert done == ["c1", "c2"] and d.terminated
    assert d.state.cycles["c1"].status == "done" and d.state.cycles["c2"].status == "done"
    assert d.state.cycles["c1"].route == "bootstrap"
    assert d.state.cycles["c2"].route == "attack"          # 含 build → attack 特化
    assert d.state.questions["q1"].status == "answered"    # I3 流程性关问
    bundle_file = tmp_path / "cycles/c2/artifacts/t1.bundle_target.json"
    payload = json.loads(bundle_file.read_text(encoding="utf-8"))
    assert payload["evaluation"]["source"] == "fake"       # M0 假执行显式标记
    assert all(log["synthetic"] for log in payload["execution_logs"])
    assert payload["execution_observation"]["source"] == "fake"


def test_driver_plan_review_exhausted_is_normal_close(tmp_path):
    script = [
        (BOOTSTRAP_FILES, ""),
        ({"idea_set.draft.json": IDEA_DRAFT}, ""),
        ({"idea_audit.json": IDEA_AUDIT}, ""),
        ({"plan.json": PLAN_BUILD}, ""),
        (REVIEW_FAIL, ""),
        ({"plan.json": PLAN_BUILD}, ""),
        (REVIEW_FAIL, ""),                                  # 两轮上限
        ({"tree_ops.json": {"ops": []},
          "selection.json": {"next_question_id": "q1", "next_intent": "attack", "scores": []}}, "无证据收尾"),
    ]
    d = make_driver(tmp_path, script)
    d.run_cycles(2)
    assert d.state.cycles["c2"].status == "done"            # 阶段失败 = 研究轮正常收尾（§4.2.3）
    assert d.state.questions["q1"].status == "inconclusive"
    assert d.state.questions["q1"].visit_count == 1
    assert not (tmp_path / "cycles/c2/artifacts/plan.json").exists()   # 评审未过不入库


def test_driver_decompose_round(tmp_path):
    """bootstrap 超预算 → decompose 轮（activate 父 → add_children 释放父+写子 dep → 选子攻坚）。"""
    bootstrap_decompose = {
        "tree_ops.json": {"ops": [{"op": "create_root", "local_key": "root",
                                   "text": "超预算根问题"}]},
        "selection.json": {"next_question_id": "root", "next_intent": "decompose",
                           "scores": [{"question_id": "root", "score": 0.4, "est_cost": 9.0}]},
    }
    decompose_files = {
        "tree_ops.json": {"ops": [{"op": "add_children", "parent_question_id": "q1",
                                   "children": [{"local_key": "a", "text": "子 A：逻辑回归"},
                                                {"local_key": "b", "text": "子 B：MLP"}]}]},
        "selection.json": {"next_question_id": "a", "next_intent": "attack",
                           "scores": [{"question_id": "a", "score": 0.6, "est_cost": 4.0},
                                      {"question_id": "b", "score": 0.55, "est_cost": 4.0}]},
    }
    script = [
        (BOOTSTRAP_FILES | {"selection.json": bootstrap_decompose["selection.json"]}, ""),
        (decompose_files, ""),
    ]
    d = make_driver(tmp_path, script)
    d.run_cycles(2)
    assert d.state.cycles["c1"].status == "done" and d.state.cycles["c2"].status == "done"
    assert d.state.cycles["c2"].route == "decompose"
    root = d.state.questions["q1"]
    assert root.status == "open" and root.decompose_count == 1     # 父释放、不增 visit
    kids = [q for q in d.state.questions.values() if q.parent_id == "q1"]
    assert len(kids) == 2 and all(q.source == "decompose" for q in kids)
    assert not d.state.is_schedulable("q1")                        # pending 子 dep 排除父
    assert d._next_route == "attack" and d._next_question in {q.qid for q in kids}


PLAN_EVAL_ONLY = {
    "needs": [{"need_id": "n1", "statement_md": "对既有基线补测指标"}],
    "reuse_evidence": [],
    "targets": [{
        "target_key": "t1", "target_kind": "eval", "seq": 1, "critical": True,
        "budget_estimate": 0.5, "spec_md": "免训练补测",
        "eval_action": "create_evaluation", "attempt_purpose": "protocol_upgrade",
        "eval_key": "up-1", "evaluation_source": "protocol_upgrade",
        "claim": {"baseline_ref": "existing-family", "variant_key": "base"},
    }],
    "protocol": {"name": "toy-gauss-cls", "version": 2, "scope_spec": {"data": "升版场景"}},
    "metric_defs": [{"metric_id": "accuracy", "version": 1, "name": "准确率",
                     "direction": "higher", "compute_spec_md": "一致率"}],
    "readout_rules": [{"metric_id": "accuracy", "metric_ver": 1, "rule_md": "≥0.88 达标"}],
    "build_target_required_metric": [{"target_key": "t1", "metric_id": "accuracy", "metric_ver": 1}],
}


def test_driver_eval_only_route_and_fabrication(tmp_path):
    """仅 eval targets → route=eval_only；假 eval 产物不带 run、schema 合法。"""
    script = [
        (BOOTSTRAP_FILES, ""),
        ({"idea_set.draft.json": IDEA_DRAFT}, ""),
        ({"idea_audit.json": IDEA_AUDIT}, ""),
        ({"plan.json": PLAN_EVAL_ONLY}, ""),
        (REVIEW_PASS, ""),
        (reasoning_close("mr:c2:t1#accuracy@1:aggregate"), ""),
    ]
    d = make_driver(tmp_path, script)
    d.run_cycles(2)
    assert d.state.cycles["c2"].route == "eval_only"
    payload = json.loads((tmp_path / "cycles/c2/artifacts/t1.bundle_target.json").read_text(encoding="utf-8"))
    assert payload["target_kind"] == "eval" and "run" not in payload
    assert payload["evaluation"]["source"] == "fake" and payload["status"] == "complete"


def test_driver_answer_wrong_question_fails_cycle_not_run(tmp_path):
    """answer.question_id 存在但 ≠ 本轮 Qn → 本轮 failed + Qn 回 open 不增 visit，整跑不炸
    （§4.2.3 第 2 行；防"悄悄关别的问题"的静默腐坏）。"""
    close_q1_and_spawn = {
        "answer.json": {"question_id": "q1", "verdict": "answered", "answer_md": "关 q1",
                        "evidence": [{"kind": "evaluation",
                                      "metric_result_id": "mr:c2:t1#accuracy@1:aggregate"}]},
        "tree_ops.json": {"ops": [{"op": "spawn_question", "local_key": "f", "kind": "followup",
                                   "parent_question_id": "q1", "text": "后续题"}]},
        "selection.json": {"next_question_id": "f", "next_intent": "attack", "scores": []},
    }
    wrong_close = {   # 攻坚 q2 却试图（再次）关 q1——存在、能过 gate 引用检查，但非本轮 Qn
        "answer.json": {"question_id": "q1", "verdict": "refuted", "answer_md": "关错题",
                        "evidence": [{"kind": "evaluation",
                                      "metric_result_id": "mr:c2:t1#accuracy@1:aggregate"}]},
        "tree_ops.json": {"ops": []},
        "selection.json": {"next_question_id": "q2", "next_intent": "attack", "scores": []},
    }
    script = [
        (BOOTSTRAP_FILES, ""),
        ({"idea_set.draft.json": IDEA_DRAFT}, ""), ({"idea_audit.json": IDEA_AUDIT}, ""),
        ({"plan.json": PLAN_BUILD}, ""), (REVIEW_PASS, ""),
        (close_q1_and_spawn, ""),
        ({"idea_set.draft.json": IDEA_DRAFT}, ""), ({"idea_audit.json": IDEA_AUDIT}, ""),
        ({"plan.json": PLAN_BUILD}, ""), (REVIEW_PASS, ""),
        (wrong_close, ""),
    ]
    d = make_driver(tmp_path, script)
    done = d.run_cycles(3)
    assert done == ["c1", "c2", "c3"]
    assert d.state.cycles["c3"].status == "failed"          # 单轮失败、不炸整跑
    assert d.state.questions["q1"].status == "answered"     # 已关问题未被二次改写
    assert d.state.questions["q2"].status == "open"         # 本轮 Qn 释放回 open
    assert d.state.questions["q2"].visit_count == 0         # 系统失败非研究尝试、不增 visit


def test_driver_sidecar_stub_path(tmp_path):
    sidecar = {
        "summary_md": "需要外部数据集，系统无法自行获取",
        "items": [{"kind": "dataset", "desc": "DEAP", "expected_files": ["deap.zip"],
                   "attempted_paths": ["官方页（需 EULA）"], "failure_reason": "需人工签署",
                   "dest_hint": "input/corpus/deap/"}],
    }
    script = [
        (BOOTSTRAP_FILES, ""),
        ({"resource_request.json": sidecar}, "只发请求单、无主产物"),   # idea 生成调用
        ({"tree_ops.json": {"ops": []},
          "selection.json": {"next_question_id": "q1", "next_intent": "attack", "scores": []}}, ""),
    ]
    d = make_driver(tmp_path, script)
    d.run_cycles(2)
    assert d.state.cycles["c2"].status == "done"            # M0 桩：不等待、按失败观测收尾
    assert d.state.questions["q1"].status == "inconclusive"
    archived = tmp_path / "cycles/c2/interaction/resource_request.json"
    assert archived.exists()                                # 请求单已校验归档
    assert any(dec["type"] == "resource_request_stub" for dec in d.state.decisions)
