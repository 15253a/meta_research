from __future__ import annotations

from copy import deepcopy

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.plan_contract import (
    PLAN_CONTEXT_PACK_SCHEMA_REF,
    PLAN_DOCUMENT_SCHEMA_REF,
    PlanContractError,
    validate_plan_context_pack,
    validate_plan_document,
)


_QUESTION_BINDING = {
    "initialization_id": "quest_init_1",
    "quest_ref": "quest_1",
    "question_ref": "question_1",
    "content_ref": "question_content_1",
    "content_hash": "a" * 64,
    "schema_ref": "meta-research/question-content/v1",
    "content_receipt": {
        "status": "accepted",
        "issuer": "research_memory",
        "kind": "question_content_acceptance",
        "receipt_ref": "rm_question_receipt_1",
        "subject_ref": "question_content_1",
        "payload_hash": "b" * 64,
    },
    "question_receipt": {
        "status": "accepted",
        "issuer": "research_graph",
        "kind": "root_question_acceptance",
        "receipt_ref": "rg_question_receipt_1",
        "subject_ref": "question_1",
        "payload_hash": "c" * 64,
    },
}

_IDEA_SET = {
    "kind": "IdeaSet",
    "question_ref": "question_1",
    "context_pack_ref": "idea_context_1",
    "candidates": [
        {
            "candidate_key": "topology",
            "direction": "比较拓扑一致性与像素重建。",
            "rationale": "结构偏置不同。",
            "assumptions": ["增强保持拓扑。"],
            "risks": ["可能保留伪影。"],
            "evidence_boundary": {
                "accepted_evidence_refs": ["asset_version_1"],
                "supported": "范围已固定。",
                "inferred": "拓扑约束可能改善保真。",
                "unknown": "跨设备稳健性未知。",
            },
            "falsification_hint": {
                "test": "比较召回率。",
                "would_refute": "召回率未改善。",
            },
            "material_difference": {
                "from_history": "历史中没有同一机制。",
                "from_peers": "干预轴是拓扑。",
                "plan_commitment_change": "Plan 比较两类目标函数。",
            },
        },
        {
            "candidate_key": "frequency",
            "direction": "比较频域遮罩与空间域遮罩。",
            "rationale": "频率支持不同。",
            "assumptions": ["低频承载形态。"],
            "risks": ["频域先验可能过强。"],
            "evidence_boundary": {
                "accepted_evidence_refs": [],
                "supported": "答案需要比较边界。",
                "inferred": "频率支持可能区分形态与噪声。",
                "unknown": "阈值未知。",
            },
            "falsification_hint": {
                "test": "比较频段消融。",
                "would_refute": "频段消融无差异。",
            },
            "material_difference": {
                "from_history": "没有频段消融。",
                "from_peers": "改变频率支持而非损失函数。",
                "plan_commitment_change": "Plan 需要频段对照。",
            },
        },
    ],
    "recommendation": None,
}

_IDEA_BINDING = {
    "outcome_ref": "idea_set_1",
    "outcome_kind": "idea_set",
    "content_ref": "idea_content_1",
    "payload_hash": "d" * 64,
    "outcome_hash": canonical_hash(_IDEA_SET),
    "content_receipt": {
        "status": "accepted",
        "issuer": "research_memory",
        "kind": "idea_outcome_content_acceptance",
        "receipt_ref": "rm_idea_receipt_1",
        "subject_ref": "idea_content_1",
        "payload_hash": "e" * 64,
    },
    "outcome_receipt": {
        "status": "accepted",
        "issuer": "research_graph",
        "kind": "idea_outcome_accepted",
        "receipt_ref": "rg_idea_receipt_1",
        "subject_ref": "idea_set_1",
        "payload_hash": "f" * 64,
    },
    "stage_commit_ref": "idea_commit_1",
    "stage_commit_receipt": {
        "status": "accepted",
        "issuer": "advancement_engine",
        "kind": "stage_commit",
        "receipt_ref": "ae_idea_commit_receipt_1",
        "subject_ref": "idea_commit_1",
        "payload_hash": "1" * 64,
    },
    "idea_set": _IDEA_SET,
}

_EVIDENCE_REF = {
    "schema_ref": "meta-research/evidence-ref/v1",
    "evidence_ref": "evidence_1",
    "asset_version_ref": "asset_version_1",
    "asset_ref": "asset_1",
    "content_hash": "2" * 64,
    "manifest_hash": "3" * 64,
    "target_commit_root_ref": "asset_root_1",
    "provenance_closure_refs": ["source_1"],
    "capabilities": ["evidence"],
    "eligibility_token_ref": "rg_asset_role_receipt_1",
    "integrity_receipt_ref": "rm_asset_receipt_1",
    "availability_receipt_ref": "rm_asset_receipt_1",
    "currentness_receipt_ref": "rg_asset_role_receipt_1",
    "asset_receipt": {
        "status": "accepted",
        "issuer": "research_memory",
        "kind": "asset_version_acceptance",
        "receipt_ref": "rm_asset_receipt_1",
        "subject_ref": "asset_version_1",
        "payload_hash": "4" * 64,
    },
    "role_ref": "asset_role_1",
    "role_receipt": {
        "status": "accepted",
        "issuer": "research_graph",
        "kind": "asset_role_acceptance",
        "receipt_ref": "rg_asset_role_receipt_1",
        "subject_ref": "asset_role_1",
        "payload_hash": "5" * 64,
    },
}


def _context_pack() -> dict[str, object]:
    return {
        "schema_ref": PLAN_CONTEXT_PACK_SCHEMA_REF,
        "cycle_ref": "cycle_1",
        "accepted_question_binding": deepcopy(_QUESTION_BINDING),
        "accepted_idea_set_binding": deepcopy(_IDEA_BINDING),
        "evidence_catalog": [deepcopy(_EVIDENCE_REF)],
        "evidence_reference_revision": 1,
    }


def _answer_contract() -> dict[str, object]:
    contract = {
        "source_question_ref": "question_1",
        "source_idea_set_ref": "idea_set_1",
        "obligations": [
            {
                "obligation_key": "effect",
                "statement": "比较两种约束对稀有形态召回的影响。",
                "minimum_support": "至少一项可复查结果和适用边界。",
                "question_trace": ["unknown_statement", "answer_shape"],
                "idea_relevance": [
                    {
                        "idea_ref": "topology",
                        "role": "query_lens",
                        "rationale": "已有证据可按损失函数组织查询。",
                    },
                    {
                        "idea_ref": "frequency",
                        "role": "not_relevant",
                        "rationale": "频段变化不回答此义务。",
                    },
                ],
            },
            {
                "obligation_key": "boundary",
                "statement": "确定改善是否跨设备成立。",
                "minimum_support": "至少覆盖两种设备或形成明确缺口。",
                "question_trace": ["answer_shape", "applicability_scope"],
                "idea_relevance": [
                    {
                        "idea_ref": "topology",
                        "role": "experiment_lens",
                        "rationale": "拓扑机制需跨设备验证。",
                    },
                    {
                        "idea_ref": "frequency",
                        "role": "experiment_lens",
                        "rationale": "频段阈值需跨设备验证。",
                    },
                ],
            },
        ],
    }
    contract["answer_contract_hash"] = canonical_hash(contract)
    return contract


def _plan_document() -> dict[str, object]:
    contract = _answer_contract()
    evidence_use = {
        "obligation_key": "effect",
        "evidence_ref": "evidence_1",
        "supported_claim": "已接纳结果支持拓扑约束提高召回。",
        "support_boundary": "只覆盖单一设备。",
        "contributing_idea_refs": ["topology"],
    }
    return {
        "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
        "kind": "PlanDocument",
        "question_ref": "question_1",
        "idea_set_ref": "idea_set_1",
        "context_pack_ref": "plan_context_1",
        "answer_contract": contract,
        "evidence_reuse_set": [evidence_use],
        "coverage": [
            {
                "obligation_key": "effect",
                "disposition": "covered",
                "evidence_uses": [evidence_use],
                "insufficiency": None,
            },
            {
                "obligation_key": "boundary",
                "disposition": "gap",
                "evidence_uses": [],
                "insufficiency": "现有证据没有跨设备比较。",
            },
        ],
        "gap_set": ["boundary"],
        "experiment_briefs": [
            {
                "experiment_key": "cross-device",
                "gap_obligation_keys": ["boundary"],
                "goal": "验证两类机制的跨设备稳健性。",
                "characteristics": "同一数据协议下比较设备域变化。",
                "boundary_constraints": "固定训练预算、标注规则和主指标。",
                "semantic_delta": "仅改变设备域；保留方法和评价协议。",
                "contributing_idea_refs": ["topology", "frequency"],
            }
        ],
        "idea_trace": [
            {
                "idea_ref": "topology",
                "obligation_roles": [
                    {"obligation_key": "effect", "role": "query_lens"},
                    {"obligation_key": "boundary", "role": "experiment_lens"},
                ],
            },
            {
                "idea_ref": "frequency",
                "obligation_roles": [
                    {"obligation_key": "effect", "role": "not_relevant"},
                    {"obligation_key": "boundary", "role": "experiment_lens"},
                ],
            },
        ],
        "bundle_disposition": "experiments_required",
        "source_bindings": {
            "question_ref": "question_1",
            "idea_set_ref": "idea_set_1",
            "context_pack_ref": "plan_context_1",
            "context_pack_hash": "6" * 64,
            "evidence_reference_revision": 1,
        },
    }


def test_valid_plan_accounts_for_every_obligation_and_idea() -> None:
    context = _context_pack()

    evidence_by_ref = validate_plan_context_pack(
        context,
        cycle_ref="cycle_1",
        accepted_question_binding=_QUESTION_BINDING,
    )
    plan_hash = validate_plan_document(
        _plan_document(),
        question_ref="question_1",
        idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1",
        context_pack_hash="6" * 64,
        accepted_idea_set=_IDEA_SET,
        evidence_by_ref=evidence_by_ref,
        evidence_reference_revision=1,
    )

    assert set(evidence_by_ref) == {"evidence_1"}
    assert plan_hash == canonical_hash(_plan_document())


def test_plan_rejects_an_incomplete_obligation_by_idea_matrix() -> None:
    document = _plan_document()
    document["answer_contract"]["obligations"][0]["idea_relevance"].pop()
    contract = document["answer_contract"]
    contract.pop("answer_contract_hash")
    contract["answer_contract_hash"] = canonical_hash(contract)

    with pytest.raises(PlanContractError, match="plan_idea_matrix_incomplete"):
        validate_plan_document(
            document,
            question_ref="question_1",
            idea_set_ref="idea_set_1",
            context_pack_ref="plan_context_1",
            context_pack_hash="6" * 64,
            accepted_idea_set=_IDEA_SET,
            evidence_by_ref={"evidence_1": _EVIDENCE_REF},
            evidence_reference_revision=1,
        )


@pytest.mark.parametrize("mutation", ("covered_has_brief", "gap_has_no_brief"))
def test_only_gaps_have_experiment_briefs(mutation: str) -> None:
    document = _plan_document()
    if mutation == "covered_has_brief":
        document["experiment_briefs"][0]["gap_obligation_keys"].append("effect")
    else:
        document["experiment_briefs"] = []

    with pytest.raises(PlanContractError, match="plan_gap_brief_closure_invalid"):
        validate_plan_document(
            document,
            question_ref="question_1",
            idea_set_ref="idea_set_1",
            context_pack_ref="plan_context_1",
            context_pack_hash="6" * 64,
            accepted_idea_set=_IDEA_SET,
            evidence_by_ref={"evidence_1": _EVIDENCE_REF},
            evidence_reference_revision=1,
        )


def test_plan_rejects_evidence_outside_the_frozen_catalog() -> None:
    document = _plan_document()
    document["coverage"][0]["evidence_uses"][0]["evidence_ref"] = "mutable-card"
    document["evidence_reuse_set"][0]["evidence_ref"] = "mutable-card"

    with pytest.raises(PlanContractError, match="plan_evidence_ref_unbound"):
        validate_plan_document(
            document,
            question_ref="question_1",
            idea_set_ref="idea_set_1",
            context_pack_ref="plan_context_1",
            context_pack_hash="6" * 64,
            accepted_idea_set=_IDEA_SET,
            evidence_by_ref={"evidence_1": _EVIDENCE_REF},
            evidence_reference_revision=1,
        )


def test_no_gap_plan_derives_no_new_experiment_disposition() -> None:
    document = _plan_document()
    second_use = deepcopy(document["evidence_reuse_set"][0])
    second_use["obligation_key"] = "boundary"
    second_use["supported_claim"] = "已接纳结果覆盖跨设备边界。"
    document["evidence_reuse_set"].append(second_use)
    document["coverage"][1] = {
        "obligation_key": "boundary",
        "disposition": "covered",
        "evidence_uses": [second_use],
        "insufficiency": None,
    }
    document["gap_set"] = []
    document["experiment_briefs"] = []
    document["bundle_disposition"] = "no_new_experiment_required"

    validate_plan_document(
        document,
        question_ref="question_1",
        idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1",
        context_pack_hash="6" * 64,
        accepted_idea_set=_IDEA_SET,
        evidence_by_ref={"evidence_1": _EVIDENCE_REF},
        evidence_reference_revision=1,
    )


def test_context_pack_rejects_an_evidence_ref_with_receipt_mismatch() -> None:
    context = _context_pack()
    context["evidence_catalog"][0]["integrity_receipt_ref"] = "different_receipt"

    with pytest.raises(PlanContractError, match="plan_evidence_ref_invalid"):
        validate_plan_context_pack(
            context,
            cycle_ref="cycle_1",
            accepted_question_binding=_QUESTION_BINDING,
        )
