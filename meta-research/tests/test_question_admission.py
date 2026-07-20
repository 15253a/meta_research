"""Research-question admission: evidence closure and engineering-task isolation."""
from __future__ import annotations

import json

import pytest
from jsonschema.exceptions import ValidationError

from conftest import make_validator
from orchestrator import database as db
from orchestrator.interfaces import Selection
from orchestrator.question_admission import (
    ALLOWED_QUESTION_EVIDENCE,
    QUESTION_CONTRACT_KIND,
    QuestionAdmissionError,
    normalize_question_contract,
)
from orchestrator.statestore import InMemoryStateStore
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.writedaemon import WriteDaemon


POLICY = {
    "policy_version": "question-admission-test-v1",
    "tree_guard": {
        "max_decompose_depth": 3,
        "max_children_per_node": 3,
        "max_open_questions": 20,
    },
    "question_guard": {"max_inconclusive_per_question": 2},
    "answer_review": {"max_reviews_per_cycle": 2},
    "goal_amend": {
        "max_spawn_from_goal_amend": 2,
        "max_closed_revalidate_per_cycle": 3,
    },
}


def _contract(*evidence: str) -> dict:
    return {
        "kind": QUESTION_CONTRACT_KIND,
        "allowed_evidence": list(evidence or ("evaluation",)),
        "answer_criterion_md": "预注册指标达到阈值或方向性假设成立。",
        "refute_criterion_md": "预注册指标未达到阈值或方向性假设不成立。",
    }


def _memory_store() -> InMemoryStateStore:
    store = InMemoryStateStore(POLICY)
    store.create_goal(text="研究目标", predicate_json={"kind": "test"})
    return store


def _memory_root(store: InMemoryStateStore) -> tuple[str, str]:
    cycle = store.open_or_resume_cycle()
    store.set_route(cycle.cycle_id, "bootstrap")
    store.apply_tree_ops(cycle.cycle_id, [{
        "op": "create_root",
        "local_key": "root",
        "text": "模型 A 是否在预注册协议下比模型 B 的 aggregate accuracy 高至少 0.02？",
        "predicate_json": _contract("evaluation"),
    }])
    store.persist_selection(
        cycle.cycle_id,
        Selection(next_question_id="root", next_intent="attack"),
    )
    return cycle.cycle_id, store.cycles[cycle.cycle_id].next_question_id


@pytest.mark.parametrize("text", [
    "如何盘点当前仓库目录？",
    "列出仓库里的所有文件",
    "还缺失哪些实验资产？",
    "怎样修复代码的运行报错？",
    "这个报错的原因是什么？",
    "配置部署环境并安装依赖",
    "如何部署这个服务？",
    "部署是否已经成功？",
    "依赖是否安装齐全？",
    "list the repository directory tree",
    "list all files",
    "debug the build error",
])
def test_admission_rejects_engineering_tasks(text):
    with pytest.raises(QuestionAdmissionError, match="不得进入研究问题树"):
        normalize_question_contract(text, _contract("evaluation"))


def test_admission_keeps_research_use_of_environment_or_deployment_terms():
    """Nouns alone are not a ban: an evidence-testable factor remains research."""
    text, contract, source = normalize_question_contract(
        "部署策略是否使端侧推理的 p95 延迟降低至少 10%？",
        _contract("evaluation"),
    )
    assert text.startswith("部署策略是否")
    assert contract["allowed_evidence"] == ["evaluation"]
    assert source == "explicit"


def test_create_root_persists_explicit_contract_and_audit_in_memory():
    store = _memory_store()
    _cycle_id, qid = _memory_root(store)
    assert store.questions[qid].predicate_json == _contract("evaluation")
    audit = [d for d in store.decisions if d["type"] == "question_admission"]
    assert len(audit) == 1
    assert audit[0]["payload"]["qid"] == qid
    assert audit[0]["payload"]["contract_source"] == "explicit"
    assert len(audit[0]["payload"]["contract_sha256"]) == 64


def test_decompose_engineering_child_rejects_entire_batch():
    store = _memory_store()
    first_cycle, root = _memory_root(store)
    store.mark_cycle_done(first_cycle)
    cycle = store.open_or_resume_cycle()
    store.set_route(cycle.cycle_id, "decompose")
    store.activate_question(root)

    with pytest.raises(QuestionAdmissionError, match="工程任务"):
        store.apply_tree_ops(cycle.cycle_id, [{
            "op": "add_children",
            "parent_question_id": root,
            "children": [
                {
                    "local_key": "research-child",
                    "text": "跨被试方差是否解释模型 A 的优势？",
                    "predicate_json": _contract("evaluation"),
                },
                {
                    "local_key": "engineering-child",
                    "text": "修复训练脚本报错",
                    "predicate_json": _contract("evaluation"),
                },
            ],
        }])

    assert list(store.questions) == [root]
    assert store.questions[root].status == "active"
    assert not store.deps
    assert len([d for d in store.decisions if d["type"] == "question_admission"]) == 1


def test_spawn_question_rejects_engineering_followup():
    store = _memory_store()
    cycle_id, root = _memory_root(store)
    with pytest.raises(QuestionAdmissionError, match="工程任务"):
        store.apply_tree_ops(cycle_id, [{
            "op": "spawn_question",
            "kind": "diagnosis",
            "parent_question_id": root,
            "text": "排查 CUDA 环境并修复错误",
            "predicate_json": _contract("evaluation"),
        }])
    assert list(store.questions) == [root]


def test_question_contract_restricts_in_memory_close_evidence_kind():
    store = _memory_store()
    cycle = store.open_or_resume_cycle()
    store.set_route(cycle.cycle_id, "bootstrap")
    store.apply_tree_ops(cycle.cycle_id, [{
        "op": "create_root",
        "text": "既有系统综述是否支持假设 H？",
        "predicate_json": _contract("literature"),
    }])
    qid = next(iter(store.questions))
    store.activate_question(qid)
    with pytest.raises(ValueError, match="closure contract"):
        store.close_question(
            cycle.cycle_id, qid, "answered", [{"kind": "evaluation"}], "结论")
    answer_id = store.close_question(
        cycle.cycle_id, qid, "answered", [{"kind": "literature"}], "结论")
    assert answer_id == "a1"


def test_sqlite_persists_contract_in_existing_question_column_and_decision():
    store = SQLiteStateStore(WriteDaemon(db.connect(":memory:")), POLICY)
    store.create_goal(text="研究目标", predicate_json={"kind": "test"})
    cycle = store.open_or_resume_cycle()
    store.set_route(cycle.cycle_id, "bootstrap")
    contract = _contract("evaluation", "child_answer")
    store.apply_tree_ops(cycle.cycle_id, [{
        "op": "create_root",
        "local_key": "root",
        "text": "跨数据集聚合结果是否支持假设 H？",
        "predicate_json": contract,
    }])

    predicate_raw = store.daemon.conn.execute(
        "SELECT predicate_json FROM question WHERE id=1").fetchone()[0]
    assert json.loads(predicate_raw) == contract
    decision_raw = store.daemon.conn.execute(
        "SELECT payload_json FROM decision WHERE question_id=1 "
        "AND type='question_admission'").fetchone()[0]
    payload = json.loads(decision_raw)
    assert payload["operation"] == "create_root"
    assert payload["contract_source"] == "explicit"
    assert payload["predicate_json"] == contract


def test_sqlite_statestore_rejects_engineering_root_without_partial_rows():
    store = SQLiteStateStore(WriteDaemon(db.connect(":memory:")), POLICY)
    store.create_goal(text="研究目标", predicate_json={"kind": "test"})
    cycle = store.open_or_resume_cycle()
    store.set_route(cycle.cycle_id, "bootstrap")
    with pytest.raises(QuestionAdmissionError, match="工程任务"):
        store.apply_tree_ops(cycle.cycle_id, [{
            "op": "create_root",
            "text": "盘点仓库目录并列出所有文件",
            "predicate_json": _contract("evaluation"),
        }])
    assert store.daemon.query_one("SELECT count(*) FROM question")[0] == 0
    assert store.daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='question_admission'")[0] == 0


def test_legacy_text_is_materialized_as_auditable_non_null_contract():
    text, contract, source = normalize_question_contract(
        "假设 H 是否得到有效证据支持？", None)
    assert text.endswith("？")
    assert source == "legacy_default"
    assert contract["kind"] == QUESTION_CONTRACT_KIND
    assert contract["allowed_evidence"] == list(ALLOWED_QUESTION_EVIDENCE)


def test_tree_ops_schema_defines_the_same_closed_evidence_vocabulary():
    validator = make_validator("tree_ops")
    valid = {
        "ops": [{
            "op": "create_root",
            "text": "假设 H 是否成立？",
            "predicate_json": _contract("evaluation", "literature"),
        }],
    }
    validator.validate(valid)

    invalid = json.loads(json.dumps(valid, ensure_ascii=False))
    invalid["ops"][0]["predicate_json"]["allowed_evidence"] = ["raw_log"]
    with pytest.raises(ValidationError):
        validator.validate(invalid)

    duplicate = json.loads(json.dumps(valid, ensure_ascii=False))
    duplicate["ops"][0]["predicate_json"]["allowed_evidence"] = [
        "evaluation", "evaluation"]
    with pytest.raises(ValidationError):
        validator.validate(duplicate)
