#!/usr/bin/env python3
"""Fixture-only MVP for the canonical Reasoning Stage Skill.

This module validates the Skill's key semantic boundaries without implementing
Research Graph, Advancement Engine, Research Memory, or Agent Runtime.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence


ALLOWED_DISPOSITIONS = {
    "affirmed",
    "denied",
    "uncertain",
    "insufficient_evidence",
}
ALLOWED_ENTRY_STAGES = {"Idea", "Plan", "Bundle", "Reasoning"}
ORDERED_STAGES = ("Idea", "Plan", "Bundle", "Reasoning")
UPSTREAM_STAGES = ORDERED_STAGES[:-1]
ALLOWED_STAGE_COMMIT_OUTCOMES = {"Completed", "Skipped", "Exhausted"}
ALLOWED_EVIDENCE_FINDINGS = {"supporting", "negative", "partial", "context"}
ALLOWED_QUESTION_RESEARCH_STATES = {"open", "resolved", "dead_end"}
ALLOWED_LITERATURE_EVIDENCE_BASES = {
    "title_lead",
    "citation_context",
    "abstract",
    "verified_fulltext",
}
ALLOWED_REUSE_ROLES = {
    "CheckpointArtifact",
    "MetricResult",
    "LogAsset",
    "AnalysisAsset",
}
ALLOWED_DIAGNOSTIC_SOURCE_KINDS = {"VariantRun", "EvaluationAttempt"}
ALLOWED_TARGET_EVIDENCE_ROLES = {
    "TargetCommit",
    "Baseline",
    "Variant",
    "VariantRun",
    "Evaluation",
    "ProtocolVersion",
    "EvaluationAttempt",
    "MetricResult",
    "CheckpointArtifact",
    "LogAsset",
    "AnalysisAsset",
}
ALLOWED_REPLAN_FROZEN_SLOTS = {
    "Goal",
    "Characteristics",
    "BoundaryConstraints",
    "SemanticDelta",
    "HeldFixed",
}
QUESTION_ANCHOR_FIELDS = (
    "kind",
    "ref",
    "question_ref",
    "formal_question_content_ref",
    "content_hash",
    "schema_ref",
    "rm_acceptance_receipt_ref",
    "question_accepted_receipt_ref",
)
SELECTION_FACT_FIELDS = (
    "kind",
    "ref",
    "question_ref",
    "quest_ref",
    "graph_revision_ref",
    "value",
    "is_current",
)


class FailClosed(RuntimeError):
    """Raised when the fixture cannot prove a semantic safety precondition."""


@dataclass(frozen=True)
class OwnerReply:
    disposition: str
    receipt_ref: str
    is_current: Optional[bool]
    subject_stage_run_request_ref: str
    subject_question_ref: str
    subject_root_session_ref: str


class ReasoningSemanticPorts(Protocol):
    """Semantic seams only; production ownership remains outside this module."""

    def submit_answer_candidate(self, candidate: Mapping[str, Any]) -> OwnerReply: ...

    def submit_confirmed_completion_candidate(
        self, candidate: Mapping[str, Any]
    ) -> OwnerReply: ...

    def create_question(self, source: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _require_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailClosed(f"{field} must be a non-empty stable reference")
    return value


def _require_ref_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise FailClosed(f"{field} must be a list")
    return [
        _require_ref(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailClosed(f"{field} must be non-empty text")
    return value


def _require_text_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise FailClosed(f"{field} must be a list")
    return [
        _require_text(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _verify_question_anchor(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") != "QuestionAnchor":
        raise FailClosed("an accepted QuestionAnchor is required")
    for field in (
        "ref",
        "question_ref",
        "formal_question_content_ref",
        "content_hash",
        "schema_ref",
        "rm_acceptance_receipt_ref",
        "question_accepted_receipt_ref",
    ):
        _require_ref(value.get(field), f"question_anchor.{field}")
    return {field: value[field] for field in QUESTION_ANCHOR_FIELDS}


def _verify_upstream_stage_closure(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise FailClosed("upstream_stage_closure must be a list")
    if len(value) != len(UPSTREAM_STAGES):
        raise FailClosed("Reasoning needs exactly the Idea, Plan and Bundle commits")

    normalized: List[Dict[str, Any]] = []
    seen_commit_refs = set()
    exhausted_index: Optional[int] = None
    exhausted_commit_ref: Optional[str] = None
    for index, expected_stage in enumerate(UPSTREAM_STAGES):
        item = value[index]
        if not isinstance(item, Mapping) or item.get("stage") != expected_stage:
            raise FailClosed("upstream StageCommit order must be Idea, Plan, Bundle")
        commit_ref = _require_ref(
            item.get("stage_commit_ref"),
            f"upstream_stage_closure[{index}].stage_commit_ref",
        )
        if commit_ref in seen_commit_refs:
            raise FailClosed("upstream StageCommit refs must be unique")
        seen_commit_refs.add(commit_ref)
        outcome = item.get("outcome")
        if outcome not in ALLOWED_STAGE_COMMIT_OUTCOMES:
            raise FailClosed("upstream StageCommit outcome is not recognized")

        entry: Dict[str, Any] = {
            "stage": expected_stage,
            "stage_commit_ref": commit_ref,
            "outcome": outcome,
        }
        if outcome == "Skipped":
            basis_refs = _require_ref_list(
                item.get("typed_basis_refs"),
                f"upstream_stage_closure[{index}].typed_basis_refs",
            )
            if not basis_refs:
                raise FailClosed("a Skipped StageCommit needs typed basis refs")
            entry["typed_basis_refs"] = basis_refs
        elif item.get("typed_basis_refs") not in (None, []):
            raise FailClosed("only a Skipped StageCommit carries typed skip basis")

        if outcome == "Exhausted":
            if exhausted_index is not None:
                raise FailClosed("an upstream path may contain at most one exhaustion")
            exhausted_index = index
            exhausted_commit_ref = commit_ref
            entry["exhaustion_proposal_ref"] = _require_ref(
                item.get("exhaustion_proposal_ref"),
                f"upstream_stage_closure[{index}].exhaustion_proposal_ref",
            )
            evidence_refs = _require_ref_list(
                item.get("exhaustion_evidence_refs"),
                f"upstream_stage_closure[{index}].exhaustion_evidence_refs",
            )
            if not evidence_refs:
                raise FailClosed("an Exhausted StageCommit needs closure evidence refs")
            entry["exhaustion_evidence_refs"] = evidence_refs
        elif any(
            field in item
            for field in ("exhaustion_proposal_ref", "exhaustion_evidence_refs")
        ):
            raise FailClosed("only an Exhausted StageCommit carries exhaustion closure")
        normalized.append(entry)

    if exhausted_index is not None:
        assert exhausted_commit_ref is not None
        for entry in normalized[exhausted_index + 1 :]:
            if entry["outcome"] != "Skipped":
                raise FailClosed("every optional Stage after exhaustion must be Skipped")
            if exhausted_commit_ref not in entry["typed_basis_refs"]:
                raise FailClosed(
                    "post-exhaustion skip basis must cite the Exhausted StageCommit"
                )
    return normalized


def _verify_plan_evidence_input(
    value: Any, upstream: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FailClosed("plan_evidence_input must be an explicit union")
    kind = value.get("kind")
    by_stage = {item["stage"]: item for item in upstream}
    if kind == "accepted":
        if any(
            by_stage[stage]["outcome"] == "Exhausted"
            for stage in ("Idea", "Plan")
        ):
            raise FailClosed(
                "Idea or Plan exhaustion cannot be paired with invented Plan evidence"
            )
        formal_plan_ref = _require_ref(
            value.get("formal_plan_ref"), "plan_evidence_input.formal_plan_ref"
        )
        reuse_set_ref = _require_ref(
            value.get("evidence_reuse_set_ref"),
            "plan_evidence_input.evidence_reuse_set_ref",
        )
        if "basis_stage_commit_refs" in value:
            raise FailClosed("accepted Plan evidence cannot also be the none branch")
        leaves = value.get("evidence_reuse_leaves")
        if not isinstance(leaves, Sequence) or isinstance(leaves, str):
            raise FailClosed("evidence_reuse_leaves must be a list")
        normalized_leaves: List[Dict[str, Any]] = []
        seen_refs = set()
        for index, leaf in enumerate(leaves):
            if not isinstance(leaf, Mapping):
                raise FailClosed(f"evidence_reuse_leaves[{index}] is not structured")
            ref = _require_ref(leaf.get("ref"), f"evidence_reuse_leaves[{index}].ref")
            if ref in seen_refs:
                raise FailClosed("EvidenceReuseSet leaves must be unique")
            seen_refs.add(ref)
            role = leaf.get("role")
            if role not in ALLOWED_REUSE_ROLES:
                raise FailClosed("EvidenceReuseSet leaf role is not recognized")
            asset_version_ref = _require_ref(
                leaf.get("asset_version_ref"),
                f"evidence_reuse_leaves[{index}].asset_version_ref",
            )
            target_ref = _require_ref(
                leaf.get("target_commit_root_ref"),
                f"evidence_reuse_leaves[{index}].target_commit_root_ref",
            )
            attempt_ref = _require_ref(
                leaf.get("source_evaluation_attempt_ref"),
                f"evidence_reuse_leaves[{index}].source_evaluation_attempt_ref",
            )
            variant_run_ref = _require_ref(
                leaf.get("source_variant_run_ref"),
                f"evidence_reuse_leaves[{index}].source_variant_run_ref",
            )
            source_subject_kind = leaf.get("source_subject_kind")
            if source_subject_kind not in ALLOWED_DIAGNOSTIC_SOURCE_KINDS:
                raise FailClosed("reuse leaf source subject kind is not recognized")
            source_subject_ref = _require_ref(
                leaf.get("source_subject_ref"),
                f"evidence_reuse_leaves[{index}].source_subject_ref",
            )
            expected_source_ref = (
                variant_run_ref
                if source_subject_kind == "VariantRun"
                else attempt_ref
            )
            if source_subject_ref != expected_source_ref:
                raise FailClosed("reuse leaf role source escapes its frozen Run/Attempt")
            if role == "MetricResult" and source_subject_kind != "EvaluationAttempt":
                raise FailClosed("a reused MetricResult must come from its accepted Attempt")
            if role == "CheckpointArtifact" and source_subject_kind != "VariantRun":
                raise FailClosed("a reused CheckpointArtifact must retain its producing Run")
            provenance_refs = _require_ref_list(
                leaf.get("provenance_closure_refs"),
                f"evidence_reuse_leaves[{index}].provenance_closure_refs",
            )
            capabilities = _require_text_list(
                leaf.get("capabilities"),
                f"evidence_reuse_leaves[{index}].capabilities",
            )
            if not capabilities:
                raise FailClosed("an EvidenceReuseSet leaf needs a declared capability")
            receipt_fields = (
                "eligibility_token_ref",
                "integrity_receipt_ref",
                "availability_receipt_ref",
                "currentness_receipt_ref",
                "source_target_commit_acceptance_receipt_ref",
                "source_formal_measurement_acceptance_receipt_ref",
                "source_role_acceptance_receipt_ref",
            )
            receipts = {
                name: _require_ref(
                    leaf.get(name), f"evidence_reuse_leaves[{index}].{name}"
                )
                for name in receipt_fields
            }
            required_provenance = {
                ref,
                asset_version_ref,
                target_ref,
                attempt_ref,
                variant_run_ref,
                source_subject_ref,
                receipts["source_target_commit_acceptance_receipt_ref"],
                receipts["source_formal_measurement_acceptance_receipt_ref"],
                receipts["source_role_acceptance_receipt_ref"],
            }
            if not required_provenance.issubset(set(provenance_refs)):
                raise FailClosed(
                    "a reuse leaf must retain its typed TargetCommit, Attempt, role and provenance"
                )
            normalized_leaves.append(
                {
                    "ref": ref,
                    "role": role,
                    "asset_version_ref": asset_version_ref,
                    "target_commit_root_ref": target_ref,
                    "source_evaluation_attempt_ref": attempt_ref,
                    "source_variant_run_ref": variant_run_ref,
                    "source_subject_kind": source_subject_kind,
                    "source_subject_ref": source_subject_ref,
                    "provenance_closure_refs": provenance_refs,
                    "capabilities": capabilities,
                    **receipts,
                    "supported_claim": _require_text(
                        leaf.get("supported_claim"),
                        f"evidence_reuse_leaves[{index}].supported_claim",
                    ),
                    "support_boundary": _require_text(
                        leaf.get("support_boundary"),
                        f"evidence_reuse_leaves[{index}].support_boundary",
                    ),
                }
            )
        return {
            "kind": "accepted",
            "formal_plan_ref": formal_plan_ref,
            "evidence_reuse_set_ref": reuse_set_ref,
            "evidence_reuse_leaves": normalized_leaves,
        }

    if kind != "none":
        raise FailClosed("plan_evidence_input kind must be accepted or none")
    forbidden = {
        "formal_plan_ref",
        "evidence_reuse_set_ref",
        "evidence_reuse_leaves",
    }
    if forbidden.intersection(value):
        raise FailClosed("the none Plan branch cannot carry placeholder Plan inputs")
    basis_refs = _require_ref_list(
        value.get("basis_stage_commit_refs"),
        "plan_evidence_input.basis_stage_commit_refs",
    )
    if not basis_refs:
        raise FailClosed("missing Plan evidence needs explicit StageCommit basis")
    stage_commit_refs = {item["stage_commit_ref"] for item in upstream}
    if not set(basis_refs).issubset(stage_commit_refs):
        raise FailClosed("Plan absence basis escapes the upstream StageCommit closure")
    if by_stage["Plan"]["stage_commit_ref"] not in basis_refs:
        raise FailClosed("Plan absence basis must include the Plan StageCommit")
    if by_stage["Plan"]["outcome"] == "Completed":
        raise FailClosed("a completed Plan Stage needs accepted Plan evidence")
    if by_stage["Bundle"]["outcome"] != "Skipped":
        raise FailClosed("Bundle execution needs a real accepted Plan input")
    return {"kind": "none", "basis_stage_commit_refs": basis_refs}


def _verify_question_literature_input(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FailClosed("question_literature_input must be an explicit union")
    kind = value.get("kind")
    if kind == "none":
        if any(field in value for field in ("revision_ref", "records")):
            raise FailClosed("literature none cannot carry a fake revision or records")
        return {"kind": "none"}
    if kind != "revision":
        raise FailClosed("question_literature_input kind must be revision or none")
    revision_ref = _require_ref(
        value.get("revision_ref"), "question_literature_input.revision_ref"
    )
    records = value.get("records")
    if not isinstance(records, Sequence) or isinstance(records, str):
        raise FailClosed("question_literature_input.records must be a list")
    normalized_records: List[Dict[str, Any]] = []
    seen_refs = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise FailClosed(f"question literature record {index} is not structured")
        ref = _require_ref(record.get("ref"), f"literature_records[{index}].ref")
        if ref in seen_refs:
            raise FailClosed("Question literature records must be unique")
        seen_refs.add(ref)
        evidence_basis = record.get("evidence_basis")
        if evidence_basis not in ALLOWED_LITERATURE_EVIDENCE_BASES:
            raise FailClosed("literature evidence basis is not recognized")
        normalized_record = {
            "ref": ref,
            "evidence_basis": evidence_basis,
            "evidence_basis_ref": _require_ref(
                record.get("evidence_basis_ref"),
                f"literature_records[{index}].evidence_basis_ref",
            ),
        }
        if record.get("reading_result_ref") is not None:
            normalized_record["reading_result_ref"] = _require_ref(
                record.get("reading_result_ref"),
                f"literature_records[{index}].reading_result_ref",
            )
        normalized_records.append(normalized_record)
    return {
        "kind": "revision",
        "revision_ref": revision_ref,
        "records": normalized_records,
    }


def _verify_asset_role(
    value: Any,
    *,
    field: str,
    expected_subject_field: str,
    expected_subject_ref: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FailClosed(f"{field} must be a role-bound accepted asset")
    result = {
        "role_ref": _require_ref(value.get("role_ref"), f"{field}.role_ref"),
        "memory_ref": _require_ref(value.get("memory_ref"), f"{field}.memory_ref"),
        expected_subject_field: _require_ref(
            value.get(expected_subject_field), f"{field}.{expected_subject_field}"
        ),
        "rm_asset_receipt_ref": _require_ref(
            value.get("rm_asset_receipt_ref"), f"{field}.rm_asset_receipt_ref"
        ),
        "rg_role_receipt_ref": _require_ref(
            value.get("rg_role_receipt_ref"), f"{field}.rg_role_receipt_ref"
        ),
    }
    if result[expected_subject_field] != expected_subject_ref:
        raise FailClosed(f"{field} is bound to the wrong semantic subject")
    return result


def _verify_accepted_target_commit_closure(
    value: Any, index: int
) -> Dict[str, Any]:
    field = f"accepted_target_commit_closures[{index}]"
    if not isinstance(value, Mapping) or value.get("accepted") is not True:
        raise FailClosed(f"{field} lacks TargetCommit acceptance")
    experiment_key = _require_ref(value.get("experiment_key"), f"{field}.experiment_key")
    target_commit_ref = _require_ref(
        value.get("target_commit_ref"), f"{field}.target_commit_ref"
    )
    chain = value.get("semantic_chain")
    if not isinstance(chain, Mapping):
        raise FailClosed(f"{field}.semantic_chain is required")
    chain_fields = (
        "target_ref",
        "baseline_ref",
        "variant_ref",
        "variant_run_ref",
        "evaluation_ref",
        "protocol_version_ref",
        "evaluation_attempt_ref",
    )
    semantic_chain = {
        name: _require_ref(chain.get(name), f"{field}.semantic_chain.{name}")
        for name in chain_fields
    }

    comparison = value.get("comparison_semantics")
    if not isinstance(comparison, Mapping):
        raise FailClosed(f"{field}.comparison_semantics is required")
    comparison_semantics = {
        "changed_axis_fact_refs": _require_ref_list(
            comparison.get("changed_axis_fact_refs"),
            f"{field}.comparison_semantics.changed_axis_fact_refs",
        ),
        "held_fixed_fact_refs": _require_ref_list(
            comparison.get("held_fixed_fact_refs"),
            f"{field}.comparison_semantics.held_fixed_fact_refs",
        ),
        "provenance_refs": _require_ref_list(
            comparison.get("provenance_refs"),
            f"{field}.comparison_semantics.provenance_refs",
        ),
    }
    if not comparison_semantics["provenance_refs"]:
        raise FailClosed("comparison semantics must preserve provenance")

    raw_bindings = value.get("execution_input_bindings")
    if not isinstance(raw_bindings, Sequence) or isinstance(raw_bindings, str):
        raise FailClosed(f"{field}.execution_input_bindings must be a list")
    if len(raw_bindings) != 2:
        raise FailClosed("each closure needs VariantRun and EvaluationAttempt bindings")
    bindings_by_kind: Dict[str, Dict[str, Any]] = {}
    for binding_index, raw_binding in enumerate(raw_bindings):
        if not isinstance(raw_binding, Mapping):
            raise FailClosed(f"{field}.execution_input_bindings[{binding_index}] invalid")
        subject_kind = raw_binding.get("subject_kind")
        if subject_kind not in {"VariantRun", "EvaluationAttempt"}:
            raise FailClosed("execution input binding subject kind is not recognized")
        if subject_kind in bindings_by_kind:
            raise FailClosed("execution input bindings need exactly one of each subject")
        expected_subject_ref = semantic_chain[
            "variant_run_ref"
            if subject_kind == "VariantRun"
            else "evaluation_attempt_ref"
        ]
        subject_ref = _require_ref(
            raw_binding.get("subject_ref"),
            f"{field}.execution_input_bindings[{binding_index}].subject_ref",
        )
        if subject_ref != expected_subject_ref:
            raise FailClosed("execution input binding is attached to the wrong subject")
        raw_causal_inputs = raw_binding.get("causal_inputs")
        if not isinstance(raw_causal_inputs, Sequence) or isinstance(
            raw_causal_inputs, str
        ):
            raise FailClosed("Execution Input Binding causal_inputs must be a list")
        causal_inputs: List[Dict[str, Any]] = []
        seen_input_refs = set()
        for input_index, raw_input in enumerate(raw_causal_inputs):
            input_field = (
                f"{field}.execution_input_bindings[{binding_index}]"
                f".causal_inputs[{input_index}]"
            )
            if not isinstance(raw_input, Mapping):
                raise FailClosed(f"{input_field} is not structured")
            input_ref = _require_ref(raw_input.get("input_ref"), f"{input_field}.input_ref")
            if input_ref in seen_input_refs:
                raise FailClosed("an Execution Input Binding repeats a causal input")
            seen_input_refs.add(input_ref)
            causal_inputs.append(
                {
                    "input_ref": input_ref,
                    "asset_version_ref": _require_ref(
                        raw_input.get("asset_version_ref"),
                        f"{input_field}.asset_version_ref",
                    ),
                    "rm_asset_receipt_ref": _require_ref(
                        raw_input.get("rm_asset_receipt_ref"),
                        f"{input_field}.rm_asset_receipt_ref",
                    ),
                }
            )
        if not causal_inputs:
            raise FailClosed("an Execution Input Binding needs exact causal inputs")
        bindings_by_kind[subject_kind] = {
            "subject_kind": subject_kind,
            "subject_ref": subject_ref,
            "binding_ref": _require_ref(
                raw_binding.get("binding_ref"),
                f"{field}.execution_input_bindings[{binding_index}].binding_ref",
            ),
            "causal_inputs": causal_inputs,
            "rg_binding_receipt_ref": _require_ref(
                raw_binding.get("rg_binding_receipt_ref"),
                f"{field}.execution_input_bindings[{binding_index}].rg_binding_receipt_ref",
            ),
            "ar_execution_receipt_ref": _require_ref(
                raw_binding.get("ar_execution_receipt_ref"),
                f"{field}.execution_input_bindings[{binding_index}].ar_execution_receipt_ref",
            ),
        }
    execution_input_bindings = [
        bindings_by_kind["VariantRun"],
        bindings_by_kind["EvaluationAttempt"],
    ]
    if len({item["binding_ref"] for item in execution_input_bindings}) != 2:
        raise FailClosed("VariantRun and EvaluationAttempt need distinct bindings")

    asset_roles = value.get("asset_roles")
    if not isinstance(asset_roles, Mapping):
        raise FailClosed(f"{field}.asset_roles is required")
    metric_result = _verify_asset_role(
        asset_roles.get("metric_result"),
        field=f"{field}.asset_roles.metric_result",
        expected_subject_field="evaluation_attempt_ref",
        expected_subject_ref=semantic_chain["evaluation_attempt_ref"],
    )

    checkpoints: List[Dict[str, Any]] = []
    raw_checkpoints = asset_roles.get("checkpoint_artifacts")
    if not isinstance(raw_checkpoints, Sequence) or isinstance(raw_checkpoints, str):
        raise FailClosed(f"{field}.asset_roles.checkpoint_artifacts must be a list")
    for role_index, raw_role in enumerate(raw_checkpoints):
        role_field = f"{field}.asset_roles.checkpoint_artifacts[{role_index}]"
        role = _verify_asset_role(
            raw_role,
            field=role_field,
            expected_subject_field="selected_by_evaluation_attempt_ref",
            expected_subject_ref=semantic_chain["evaluation_attempt_ref"],
        )
        produced_by = _require_ref(
            raw_role.get("produced_by_variant_run_ref")
            if isinstance(raw_role, Mapping)
            else None,
            f"{role_field}.produced_by_variant_run_ref",
        )
        if produced_by != semantic_chain["variant_run_ref"]:
            raise FailClosed("CheckpointArtifact came from another VariantRun")
        role["produced_by_variant_run_ref"] = produced_by
        selected_by_target = _require_ref(
            raw_role.get("selected_by_target_commit_ref")
            if isinstance(raw_role, Mapping)
            else None,
            f"{role_field}.selected_by_target_commit_ref",
        )
        if selected_by_target != target_commit_ref:
            raise FailClosed("CheckpointArtifact was not selected by this TargetCommit")
        role["selected_by_target_commit_ref"] = selected_by_target
        checkpoints.append(role)

    def selected_assets(name: str) -> List[Dict[str, Any]]:
        raw_assets = asset_roles.get(name)
        if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, str):
            raise FailClosed(f"{field}.asset_roles.{name} must be a list")
        normalized_assets: List[Dict[str, Any]] = []
        for role_index, raw_role in enumerate(raw_assets):
            role_field = f"{field}.asset_roles.{name}[{role_index}]"
            role = _verify_asset_role(
                raw_role,
                field=role_field,
                expected_subject_field="selected_by_target_commit_ref",
                expected_subject_ref=target_commit_ref,
            )
            if not isinstance(raw_role, Mapping):
                raise FailClosed(f"{role_field} is not structured")
            source_subject_kind = raw_role.get("source_subject_kind")
            if source_subject_kind not in ALLOWED_DIAGNOSTIC_SOURCE_KINDS:
                raise FailClosed("selected diagnostic asset source kind is not recognized")
            source_subject_ref = _require_ref(
                raw_role.get("source_subject_ref"),
                f"{role_field}.source_subject_ref",
            )
            expected_source_ref = semantic_chain[
                "variant_run_ref"
                if source_subject_kind == "VariantRun"
                else "evaluation_attempt_ref"
            ]
            if source_subject_ref != expected_source_ref:
                raise FailClosed(
                    "selected diagnostic asset came from another Run or Attempt"
                )
            role["source_subject_kind"] = source_subject_kind
            role["source_subject_ref"] = source_subject_ref
            normalized_assets.append(role)
        return normalized_assets

    logs = selected_assets("selected_logs")
    analyses = selected_assets("selected_analyses")
    role_refs = [
        target_commit_ref,
        *[semantic_chain[name] for name in chain_fields],
        metric_result["role_ref"],
        *[item["role_ref"] for item in checkpoints],
        *[item["role_ref"] for item in logs],
        *[item["role_ref"] for item in analyses],
    ]
    if len(role_refs) != len(set(role_refs)):
        raise FailClosed("one ref cannot impersonate multiple semantic or asset roles")

    formal_acceptance = value.get("formal_measurement_acceptance")
    if not isinstance(formal_acceptance, Mapping):
        raise FailClosed(f"{field}.formal_measurement_acceptance is required")
    formal_measurement_acceptance = {
        "receipt_ref": _require_ref(
            formal_acceptance.get("receipt_ref"),
            f"{field}.formal_measurement_acceptance.receipt_ref",
        ),
        "evaluation_attempt_ref": _require_ref(
            formal_acceptance.get("evaluation_attempt_ref"),
            f"{field}.formal_measurement_acceptance.evaluation_attempt_ref",
        ),
    }
    if (
        formal_measurement_acceptance["evaluation_attempt_ref"]
        != semantic_chain["evaluation_attempt_ref"]
    ):
        raise FailClosed("formal measurement acceptance binds another Attempt")

    target_acceptance = value.get("target_commit_acceptance")
    if not isinstance(target_acceptance, Mapping):
        raise FailClosed(f"{field}.target_commit_acceptance is required")
    target_commit_acceptance = {
        "receipt_ref": _require_ref(
            target_acceptance.get("receipt_ref"),
            f"{field}.target_commit_acceptance.receipt_ref",
        ),
        "target_commit_ref": _require_ref(
            target_acceptance.get("target_commit_ref"),
            f"{field}.target_commit_acceptance.target_commit_ref",
        ),
    }
    if target_commit_acceptance["target_commit_ref"] != target_commit_ref:
        raise FailClosed("TargetCommit acceptance binds another TargetCommit")

    return {
        "accepted": True,
        "experiment_key": experiment_key,
        "target_commit_ref": target_commit_ref,
        "semantic_chain": semantic_chain,
        "comparison_semantics": comparison_semantics,
        "execution_input_bindings": execution_input_bindings,
        "asset_roles": {
            "metric_result": metric_result,
            "checkpoint_artifacts": checkpoints,
            "selected_logs": logs,
            "selected_analyses": analyses,
        },
        "formal_measurement_acceptance": formal_measurement_acceptance,
        "target_commit_acceptance": target_commit_acceptance,
    }


def _verify_bundle_replan_candidates(
    value: Any,
    upstream: Sequence[Mapping[str, Any]],
    plan_evidence_input: Mapping[str, Any],
    target_closures: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise FailClosed("bundle_replan_candidates must be a list")
    bundle_commit = next(item for item in upstream if item["stage"] == "Bundle")
    targets_by_ref = {
        item["target_commit_ref"]: item for item in target_closures
    }
    target_refs = set(targets_by_ref)
    normalized: List[Dict[str, Any]] = []
    seen_refs = set()
    for index, candidate in enumerate(value):
        field = f"bundle_replan_candidates[{index}]"
        if not isinstance(candidate, Mapping) or candidate.get("kind") != "BundleReplanRequiredCandidate":
            raise FailClosed(f"{field} is not a Bundle replan candidate")
        ref = _require_ref(candidate.get("ref"), f"{field}.ref")
        if ref in seen_refs:
            raise FailClosed("Bundle replan candidate refs must be unique")
        seen_refs.add(ref)
        source_commit_ref = _require_ref(
            candidate.get("source_bundle_stage_commit_ref"),
            f"{field}.source_bundle_stage_commit_ref",
        )
        if source_commit_ref != bundle_commit["stage_commit_ref"]:
            raise FailClosed("Bundle replan candidate cites another StageCommit")
        if bundle_commit["outcome"] != "Completed":
            raise FailClosed("a skipped or exhausted Bundle cannot supply replan")
        if plan_evidence_input["kind"] != "accepted":
            raise FailClosed("Bundle replan needs the actual accepted Plan input")
        experiment_key = _require_ref(
            candidate.get("experiment_key"), f"{field}.experiment_key"
        )
        partial_refs = _require_ref_list(
            candidate.get("accepted_partial_target_commit_refs", []),
            f"{field}.accepted_partial_target_commit_refs",
        )
        if not set(partial_refs).issubset(target_refs):
            raise FailClosed("Bundle replan cites a non-frozen partial TargetCommit")
        if any(
            targets_by_ref[target_ref]["experiment_key"] != experiment_key
            for target_ref in partial_refs
        ):
            raise FailClosed("Bundle replan mixes another ExperimentKey's TargetCommit")
        unrealized_refs = _require_ref_list(
            candidate.get("unrealized_item_refs"), f"{field}.unrealized_item_refs"
        )
        if not unrealized_refs:
            raise FailClosed("Bundle replan needs remaining unrealized work")
        raw_basis = candidate.get("semantic_change_basis")
        if not isinstance(raw_basis, Sequence) or isinstance(raw_basis, str):
            raise FailClosed(f"{field}.semantic_change_basis must be a list")
        if not raw_basis:
            raise FailClosed("Bundle replan needs a frozen semantic-change basis")
        semantic_change_basis: List[Dict[str, Any]] = []
        for basis_index, basis in enumerate(raw_basis):
            if not isinstance(basis, Mapping):
                raise FailClosed(f"{field}.semantic_change_basis[{basis_index}] invalid")
            frozen_slot = basis.get("frozen_slot")
            if frozen_slot not in ALLOWED_REPLAN_FROZEN_SLOTS:
                raise FailClosed("replan basis does not name a frozen Plan slot")
            basis_refs = _require_ref_list(
                basis.get("basis_refs"),
                f"{field}.semantic_change_basis[{basis_index}].basis_refs",
            )
            if not basis_refs:
                raise FailClosed("semantic-change basis needs evidence refs")
            semantic_change_basis.append(
                {
                    "frozen_slot": frozen_slot,
                    "basis_refs": basis_refs,
                    "required_change": _require_text(
                        basis.get("required_change"),
                        f"{field}.semantic_change_basis[{basis_index}].required_change",
                    ),
                }
            )
        normalized.append(
            {
                "kind": "BundleReplanRequiredCandidate",
                "ref": ref,
                "source_bundle_stage_commit_ref": source_commit_ref,
                "experiment_key": experiment_key,
                "experiment_brief_ref": _require_ref(
                    candidate.get("experiment_brief_ref"),
                    f"{field}.experiment_brief_ref",
                ),
                "accepted_partial_target_commit_refs": partial_refs,
                "unrealized_item_refs": unrealized_refs,
                "semantic_change_basis": semantic_change_basis,
            }
        )
    return normalized


def verify_stage_run_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Verify the frozen input surface used by this fixture.

    TODO-IMPL(advancement-engine.verify_stage_run_request; source=#58,#105):
    replace fixture checks with the Owner's receipt/currentness verification.
    """

    if request.get("type") != "StageRunRequest":
        raise FailClosed("only a typed StageRunRequest may invoke Reasoning")
    if request.get("stage") != "Reasoning":
        raise FailClosed("the request is not for the Reasoning Stage")
    if request.get("is_current") is not True:
        raise FailClosed("StageRunRequest currentness is unknown or false")

    _require_ref(request.get("ref"), "stage_run_request.ref")
    _require_ref(
        request.get("foreground_epoch_ref"),
        "stage_run_request.foreground_epoch_ref",
    )
    question_ref = _require_ref(
        request.get("question_ref"), "stage_run_request.question_ref"
    )
    _require_ref(request.get("root_session_ref"), "stage_run_request.root_session_ref")

    binding = request.get("accepted_question_binding")
    if not isinstance(binding, Mapping) or binding.get("kind") != "AcceptedQuestionBinding":
        raise FailClosed("StageRunRequest needs an AcceptedQuestionBinding")
    _require_ref(binding.get("ref"), "accepted_question_binding.ref")
    _require_ref(
        binding.get("currentness_fact_ref"),
        "accepted_question_binding.currentness_fact_ref",
    )
    anchor = _verify_question_anchor(binding.get("question_anchor"))
    if anchor.get("question_ref") != question_ref:
        raise FailClosed("AcceptedQuestionBinding is not bound to this Question")

    raw_frozen = request.get("frozen")
    if not isinstance(raw_frozen, Mapping):
        raise FailClosed("StageRunRequest.frozen is required")
    upstream = _verify_upstream_stage_closure(
        raw_frozen.get("upstream_stage_closure")
    )
    plan_evidence_input = _verify_plan_evidence_input(
        raw_frozen.get("plan_evidence_input"), upstream
    )
    question_literature_input = _verify_question_literature_input(
        raw_frozen.get("question_literature_input")
    )

    research_context = raw_frozen.get("research_context")
    if not isinstance(research_context, Mapping):
        raise FailClosed("frozen.research_context is required")
    for field in (
        "ref",
        "hash",
        "current_cycle_ref",
        "current_question_ref",
        "active_graph_snapshot_ref",
        "quest_ref",
        "goal_revision_ref",
    ):
        _require_ref(research_context.get(field), f"research_context.{field}")
    if research_context.get("current_question_ref") != question_ref:
        raise FailClosed("research context is not bound to this Question")
    _require_ref_list(
        research_context.get("prior_question_outcome_refs", []),
        "research_context.prior_question_outcome_refs",
    )
    _require_ref_list(
        research_context.get("parent_question_refs", []),
        "research_context.parent_question_refs",
    )

    target_commits = raw_frozen.get("accepted_target_commit_closures")
    if not isinstance(target_commits, Sequence) or isinstance(target_commits, str):
        raise FailClosed("frozen.accepted_target_commit_closures must be a list")
    target_closures = [
        _verify_accepted_target_commit_closure(target, index)
        for index, target in enumerate(target_commits)
    ]
    target_refs = [item["target_commit_ref"] for item in target_closures]
    if len(target_refs) != len(set(target_refs)):
        raise FailClosed("accepted TargetCommit closures must be unique")

    bundle_replan_candidates = _verify_bundle_replan_candidates(
        raw_frozen.get("bundle_replan_candidates"),
        upstream,
        plan_evidence_input,
        target_closures,
    )
    return {
        "upstream_stage_closure": upstream,
        "plan_evidence_input": plan_evidence_input,
        "question_literature_input": question_literature_input,
        "research_context": {
            "ref": research_context["ref"],
            "hash": research_context["hash"],
            "current_cycle_ref": research_context["current_cycle_ref"],
            "current_question_ref": research_context["current_question_ref"],
            "prior_question_outcome_refs": list(
                research_context.get("prior_question_outcome_refs", [])
            ),
            "parent_question_refs": list(
                research_context.get("parent_question_refs", [])
            ),
            "active_graph_snapshot_ref": research_context[
                "active_graph_snapshot_ref"
            ],
            "quest_ref": research_context["quest_ref"],
            "goal_revision_ref": research_context["goal_revision_ref"],
        },
        "accepted_target_commit_closures": target_closures,
        "bundle_replan_candidates": bundle_replan_candidates,
    }


def _target_commit_closures(frozen: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [dict(target) for target in frozen["accepted_target_commit_closures"]]


def _verify_evidence_closure(
    frozen: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        raise FailClosed("evidence must be a list")
    plan_input = frozen["plan_evidence_input"]
    reusable = {
        item["ref"]: item
        for item in plan_input.get("evidence_reuse_leaves", [])
    }
    literature_input = frozen["question_literature_input"]
    literature = {
        item["ref"]: item for item in literature_input.get("records", [])
    }
    target_closures = {
        item["target_commit_ref"]: item
        for item in frozen["accepted_target_commit_closures"]
    }
    normalized: List[Dict[str, Any]] = []
    seen = set()

    def register(identity: Any) -> None:
        if identity in seen:
            raise FailClosed("the same evidence role cannot be listed twice")
        seen.add(identity)

    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise FailClosed(f"evidence[{index}] is not structured")
        ref = _require_ref(item.get("ref"), f"evidence[{index}].ref")
        kind = item.get("kind")
        finding = item.get("finding")
        if finding not in ALLOWED_EVIDENCE_FINDINGS:
            raise FailClosed(f"evidence[{index}] needs a supported scientific finding")
        if kind == "EvidenceReuseLeaf" and ref in reusable:
            leaf = reusable[ref]
            if "role" in item:
                raise FailClosed("reuse evidence role comes only from the frozen leaf")
            register((kind, leaf["role"], ref))
            normalized.append(
                {
                    "kind": kind,
                    "ref": ref,
                    "role": leaf["role"],
                    "finding": finding,
                    "source_evidence_reuse_set_ref": plan_input[
                        "evidence_reuse_set_ref"
                    ],
                    "asset_version_ref": leaf["asset_version_ref"],
                    "target_commit_root_ref": leaf["target_commit_root_ref"],
                    "source_evaluation_attempt_ref": leaf[
                        "source_evaluation_attempt_ref"
                    ],
                    "source_variant_run_ref": leaf["source_variant_run_ref"],
                    "source_subject_kind": leaf["source_subject_kind"],
                    "source_subject_ref": leaf["source_subject_ref"],
                    "provenance_closure_refs": list(
                        leaf["provenance_closure_refs"]
                    ),
                    "capabilities": list(leaf["capabilities"]),
                    "eligibility_token_ref": leaf["eligibility_token_ref"],
                    "integrity_receipt_ref": leaf["integrity_receipt_ref"],
                    "availability_receipt_ref": leaf[
                        "availability_receipt_ref"
                    ],
                    "currentness_receipt_ref": leaf["currentness_receipt_ref"],
                    "source_target_commit_acceptance_receipt_ref": leaf[
                        "source_target_commit_acceptance_receipt_ref"
                    ],
                    "source_formal_measurement_acceptance_receipt_ref": leaf[
                        "source_formal_measurement_acceptance_receipt_ref"
                    ],
                    "source_role_acceptance_receipt_ref": leaf[
                        "source_role_acceptance_receipt_ref"
                    ],
                    "supported_claim": leaf["supported_claim"],
                    "support_boundary": leaf["support_boundary"],
                }
            )
            continue

        if kind == "LiteratureRecord" and ref in literature:
            if "role" in item:
                raise FailClosed("LiteratureRecord cannot take a caller-supplied role")
            record = literature[ref]
            if item.get("evidence_basis") != record["evidence_basis"]:
                raise FailClosed("literature evidence basis was upgraded or rewritten")
            normalized_record = {
                "kind": kind,
                "ref": ref,
                "finding": finding,
                "source_question_literature_revision_ref": literature_input[
                    "revision_ref"
                ],
                "evidence_basis": record["evidence_basis"],
                "evidence_basis_ref": record["evidence_basis_ref"],
            }
            if "reading_result_ref" in record:
                normalized_record["reading_result_ref"] = record[
                    "reading_result_ref"
                ]
            register((kind, ref))
            normalized.append(normalized_record)
            continue

        if kind == "TargetClosureLeaf":
            role = item.get("role")
            if role not in ALLOWED_TARGET_EVIDENCE_ROLES:
                raise FailClosed("target evidence role is not recognized")
            source_target_ref = _require_ref(
                item.get("source_target_commit_ref"),
                f"evidence[{index}].source_target_commit_ref",
            )
            source_attempt_ref = _require_ref(
                item.get("source_evaluation_attempt_ref"),
                f"evidence[{index}].source_evaluation_attempt_ref",
            )
            target = target_closures.get(source_target_ref)
            if target is None:
                raise FailClosed("target evidence cites a non-frozen TargetCommit")
            chain = target["semantic_chain"]
            if source_attempt_ref != chain["evaluation_attempt_ref"]:
                raise FailClosed("target evidence crosses EvaluationAttempt boundaries")
            role_refs: Dict[str, List[Dict[str, Any]]] = {
                "TargetCommit": [{"role_ref": target["target_commit_ref"]}],
                "Baseline": [{"role_ref": chain["baseline_ref"]}],
                "Variant": [{"role_ref": chain["variant_ref"]}],
                "VariantRun": [{"role_ref": chain["variant_run_ref"]}],
                "Evaluation": [{"role_ref": chain["evaluation_ref"]}],
                "ProtocolVersion": [{"role_ref": chain["protocol_version_ref"]}],
                "EvaluationAttempt": [
                    {"role_ref": chain["evaluation_attempt_ref"]}
                ],
                "MetricResult": [target["asset_roles"]["metric_result"]],
                "CheckpointArtifact": target["asset_roles"][
                    "checkpoint_artifacts"
                ],
                "LogAsset": target["asset_roles"]["selected_logs"],
                "AnalysisAsset": target["asset_roles"]["selected_analyses"],
            }
            matches = [entry for entry in role_refs[role] if entry["role_ref"] == ref]
            if len(matches) != 1:
                raise FailClosed("evidence ref does not have that role in the closure")
            register((kind, role, ref, source_target_ref, source_attempt_ref))
            normalized_item = {
                "kind": kind,
                "role": role,
                "ref": ref,
                "finding": finding,
                "source_target_commit_ref": source_target_ref,
                "source_evaluation_attempt_ref": source_attempt_ref,
            }
            role_entry = matches[0]
            for field in (
                "memory_ref",
                "rm_asset_receipt_ref",
                "rg_role_receipt_ref",
                "source_subject_kind",
                "source_subject_ref",
            ):
                if field in role_entry:
                    normalized_item[field] = role_entry[field]
            normalized.append(normalized_item)
            continue

        raise FailClosed(f"evidence[{index}] is outside the frozen evidence closure")
    return normalized


def _collect_reference_values(value: Any) -> set:
    refs = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "ref" or key.endswith("_ref"):
                if isinstance(child, str) and child.strip():
                    refs.add(child)
            elif key.endswith("_refs") and isinstance(child, Sequence) and not isinstance(child, str):
                refs.update(
                    item for item in child if isinstance(item, str) and item.strip()
                )
            refs.update(_collect_reference_values(child))
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for child in value:
            refs.update(_collect_reference_values(child))
    return refs


def _verify_causal_interpretation(
    frozen: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    proposed: Mapping[str, Any],
) -> Dict[str, Any]:
    value = proposed.get("causal_interpretation")
    if not isinstance(value, Mapping):
        raise FailClosed("causal_interpretation is required")
    target_closures = frozen["accepted_target_commit_closures"]
    expected_targets = [item["target_commit_ref"] for item in target_closures]
    target_refs = _require_ref_list(
        value.get("target_commit_refs"), "causal_interpretation.target_commit_refs"
    )
    if len(target_refs) != len(set(target_refs)) or set(target_refs) != set(expected_targets):
        raise FailClosed("causal interpretation must cover every frozen TargetCommit")

    expected_changed = {
        ref
        for target in target_closures
        for ref in target["comparison_semantics"]["changed_axis_fact_refs"]
    }
    expected_held_fixed = {
        ref
        for target in target_closures
        for ref in target["comparison_semantics"]["held_fixed_fact_refs"]
    }
    expected_provenance = {
        ref
        for target in target_closures
        for ref in target["comparison_semantics"]["provenance_refs"]
    }
    changed_refs = _require_ref_list(
        value.get("changed_axis_fact_refs"),
        "causal_interpretation.changed_axis_fact_refs",
    )
    held_fixed_refs = _require_ref_list(
        value.get("held_fixed_fact_refs"),
        "causal_interpretation.held_fixed_fact_refs",
    )
    provenance_refs = _require_ref_list(
        value.get("provenance_refs"), "causal_interpretation.provenance_refs"
    )
    if len(changed_refs) != len(set(changed_refs)) or set(changed_refs) != expected_changed:
        raise FailClosed("causal interpretation changed or omitted actual variation axes")
    if (
        len(held_fixed_refs) != len(set(held_fixed_refs))
        or set(held_fixed_refs) != expected_held_fixed
    ):
        raise FailClosed("causal interpretation changed or omitted held-fixed facts")
    if (
        len(provenance_refs) != len(set(provenance_refs))
        or set(provenance_refs) != expected_provenance
    ):
        raise FailClosed("causal interpretation changed or omitted provenance")

    attribution_basis_refs = _require_ref_list(
        value.get("attribution_basis_refs"),
        "causal_interpretation.attribution_basis_refs",
    )
    allowed_basis = _collect_reference_values(frozen) | _collect_reference_values(
        evidence
    )
    if not set(attribution_basis_refs).issubset(allowed_basis):
        raise FailClosed("causal attribution basis escapes the frozen closure")
    return {
        "target_commit_refs": target_refs,
        "changed_axis_fact_refs": changed_refs,
        "held_fixed_fact_refs": held_fixed_refs,
        "provenance_refs": provenance_refs,
        "claim_scope": _require_text(
            value.get("claim_scope"), "causal_interpretation.claim_scope"
        ),
        "attribution_basis_refs": attribution_basis_refs,
        "statement": _require_text(
            value.get("statement"), "causal_interpretation.statement"
        ),
        "sufficiency_rationale": _require_text(
            value.get("sufficiency_rationale"),
            "causal_interpretation.sufficiency_rationale",
        ),
        "confounders": _require_text_list(
            value.get("confounders", []), "causal_interpretation.confounders"
        ),
    }


def _verify_bundle_replan_interpretations(
    frozen: Mapping[str, Any], proposed: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    if "replan_required" in proposed:
        raise FailClosed("Reasoning cannot mint a local replan_required value")
    raw = proposed.get("bundle_replan_interpretations", [])
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise FailClosed("bundle_replan_interpretations must be a list")
    candidates = {
        item["ref"]: item for item in frozen["bundle_replan_candidates"]
    }
    if len(raw) != len(candidates):
        raise FailClosed("Reasoning must interpret each and only each Bundle candidate")
    normalized: List[Dict[str, Any]] = []
    seen_refs = set()
    for index, interpretation in enumerate(raw):
        if not isinstance(interpretation, Mapping):
            raise FailClosed(f"bundle_replan_interpretations[{index}] invalid")
        source_ref = _require_ref(
            interpretation.get("source_candidate_ref"),
            f"bundle_replan_interpretations[{index}].source_candidate_ref",
        )
        if source_ref in seen_refs or source_ref not in candidates:
            raise FailClosed("Bundle replan interpretation is duplicate or unfrozen")
        seen_refs.add(source_ref)
        expected_basis = {
            ref
            for basis in candidates[source_ref]["semantic_change_basis"]
            for ref in basis["basis_refs"]
        }
        source_basis_refs = _require_ref_list(
            interpretation.get("source_basis_refs"),
            f"bundle_replan_interpretations[{index}].source_basis_refs",
        )
        if len(source_basis_refs) != len(set(source_basis_refs)) or set(source_basis_refs) != expected_basis:
            raise FailClosed("Reasoning cannot rewrite Bundle replan basis")
        normalized.append(
            {
                "source_candidate_ref": source_ref,
                "source_bundle_stage_commit_ref": candidates[source_ref][
                    "source_bundle_stage_commit_ref"
                ],
                "source_basis_refs": source_basis_refs,
                "interpretation": _require_text(
                    interpretation.get("interpretation"),
                    f"bundle_replan_interpretations[{index}].interpretation",
                ),
            }
        )
    return normalized


def _verify_research_synthesis(
    frozen: Mapping[str, Any], proposed: Mapping[str, Any]
) -> Dict[str, Any]:
    research_context = frozen["research_context"]
    synthesis = proposed.get("research_synthesis")
    if not isinstance(synthesis, Mapping):
        raise FailClosed("research_synthesis is required")
    if synthesis.get("context_ref") != research_context["ref"]:
        raise FailClosed("research_synthesis is not bound to the frozen context")

    narrative = _require_text(synthesis.get("narrative"), "research_synthesis.narrative")
    scope_refs = _require_ref_list(
        synthesis.get("scope_refs"), "research_synthesis.scope_refs"
    )
    uncertainties = _require_text_list(
        synthesis.get("uncertainties", []),
        "research_synthesis.uncertainties",
    )

    allowed_scope = {
        research_context["current_cycle_ref"],
        research_context["current_question_ref"],
        research_context["active_graph_snapshot_ref"],
        research_context["quest_ref"],
        research_context["goal_revision_ref"],
        *research_context.get("prior_question_outcome_refs", []),
        *research_context.get("parent_question_refs", []),
    }
    outside = set(scope_refs) - allowed_scope
    if outside:
        raise FailClosed("research_synthesis scope escapes the frozen context")

    required_scope = {
        research_context["current_cycle_ref"],
        research_context["current_question_ref"],
        research_context["quest_ref"],
        research_context["goal_revision_ref"],
        *research_context.get("prior_question_outcome_refs", []),
        *research_context.get("parent_question_refs", []),
    }
    missing_scope = required_scope - set(scope_refs)
    if missing_scope:
        raise FailClosed(
            "research_synthesis omits a required Cycle/Question/parent/Quest scope"
        )
    return {
        "context_ref": research_context["ref"],
        "scope_refs": list(scope_refs),
        "narrative": narrative,
        "uncertainties": uncertainties,
    }


def _has_substantive_evidence(evidence: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        item["kind"] == "LiteratureRecord"
        or (
            item["kind"] == "EvidenceReuseLeaf"
            and item.get("role") == "MetricResult"
        )
        or (
            item["kind"] == "TargetClosureLeaf"
            and item.get("role") == "MetricResult"
        )
        for item in evidence
    )


def _verify_disposition_boundary(
    disposition: str,
    claim: Any,
    evidence: Sequence[Mapping[str, Any]],
    missing_evidence: Sequence[str],
    uncertainty_basis: Sequence[str],
    *,
    field_prefix: str = "",
) -> Optional[str]:
    """Keep evidence insufficiency distinct from an indeterminate result."""

    label = f"{field_prefix} " if field_prefix else ""
    if disposition == "insufficient_evidence":
        if not missing_evidence:
            raise FailClosed(
                f"{label}insufficient evidence must identify required evidence gaps"
            )
        if uncertainty_basis:
            raise FailClosed(
                f"{label}insufficient evidence cannot carry an uncertainty basis"
            )
        if claim is not None:
            raise FailClosed(
                f"{label}insufficient evidence cannot assert a scientific claim"
            )
        return None

    if missing_evidence:
        raise FailClosed(
            f"{label}{disposition} cannot carry unresolved required evidence gaps"
        )
    normalized_claim = _require_text(claim, f"{label}claim".strip())
    if not _has_substantive_evidence(evidence):
        raise FailClosed(
            f"{label}{disposition} needs a frozen LiteratureRecord or MetricResult"
        )
    if disposition == "uncertain":
        if not uncertainty_basis:
            raise FailClosed(
                f"{label}uncertain must explain why accepted evidence is indeterminate"
            )
    elif uncertainty_basis:
        raise FailClosed(
            f"{label}{disposition} cannot carry an uncertainty disposition basis"
        )
    return normalized_claim


def build_scientific_outcome(
    request: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    proposed: Mapping[str, Any],
    *,
    technical_blockers: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build an unaccepted, evidence-bounded outcome candidate."""

    frozen = verify_stage_run_request(request)
    if technical_blockers:
        raise FailClosed(
            "unhealed technical blocker: "
            + "; ".join(str(item) for item in technical_blockers)
        )
    disposition = proposed.get("disposition")
    if disposition not in ALLOWED_DISPOSITIONS:
        raise FailClosed("scientific disposition is not recognized")
    missing = _require_text_list(
        proposed.get("missing_evidence", []), "missing_evidence"
    )
    uncertainty_basis = _require_text_list(
        proposed.get("uncertainty_basis", []), "uncertainty_basis"
    )
    limitations = _require_text_list(proposed.get("limitations", []), "limitations")
    normalized_evidence = _verify_evidence_closure(frozen, evidence)
    claim = _verify_disposition_boundary(
        disposition,
        proposed.get("claim"),
        normalized_evidence,
        missing,
        uncertainty_basis,
    )
    causal_interpretation = _verify_causal_interpretation(
        frozen, normalized_evidence, proposed
    )
    bundle_replan_interpretations = _verify_bundle_replan_interpretations(
        frozen, proposed
    )
    research_synthesis = _verify_research_synthesis(frozen, proposed)
    research_context = frozen["research_context"]

    return {
        "kind": "ScientificOutcomeCandidate",
        "stage_run_request_ref": request["ref"],
        "foreground_epoch_ref": request["foreground_epoch_ref"],
        "accepted_question_binding_ref": request["accepted_question_binding"]["ref"],
        "question_anchor_ref": request["accepted_question_binding"]["question_anchor"][
            "ref"
        ],
        "question_ref": request["question_ref"],
        "root_session_ref": request["root_session_ref"],
        "quest_ref": research_context["quest_ref"],
        "goal_revision_ref": research_context["goal_revision_ref"],
        "active_graph_snapshot_ref": research_context["active_graph_snapshot_ref"],
        "frozen_input_refs": {
            "upstream_stage_closure": list(frozen["upstream_stage_closure"]),
            "plan_evidence_input": dict(frozen["plan_evidence_input"]),
            "question_literature_input": dict(
                frozen["question_literature_input"]
            ),
            "accepted_target_commit_closures": _target_commit_closures(frozen),
            "bundle_replan_candidates": list(
                frozen["bundle_replan_candidates"]
            ),
            "research_context_ref": research_context["ref"],
            "research_context_hash": research_context["hash"],
        },
        "disposition": disposition,
        "claim": claim,
        "support_scope": _require_text(
            proposed.get("support_scope", "not yet established"), "support_scope"
        ),
        "limitations": limitations,
        "missing_evidence": missing,
        "uncertainty_basis": uncertainty_basis,
        "causal_interpretation": causal_interpretation,
        "evidence": normalized_evidence,
        "research_synthesis": research_synthesis,
        "bundle_replan_interpretations": bundle_replan_interpretations,
        "is_owner_accepted": False,
        "is_stage_advanced": False,
    }


def _verify_selection_fact(
    value: Any,
    *,
    kind: str,
    question_ref: str,
    quest_ref: str,
    expected_value: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") != kind:
        raise FailClosed(f"selectable target needs a {kind}")
    _require_ref(value.get("ref"), f"{kind}.ref")
    _require_ref(value.get("graph_revision_ref"), f"{kind}.graph_revision_ref")
    if value.get("question_ref") != question_ref:
        raise FailClosed(f"{kind} is not bound to the selected Question")
    if value.get("quest_ref") != quest_ref:
        raise FailClosed(f"{kind} is not bound to the current Quest view")
    if value.get("value") != expected_value:
        raise FailClosed(
            f"{kind} must prove {expected_value!r}, not {value.get('value')!r}"
        )
    if value.get("is_current") is not True:
        raise FailClosed(f"{kind} currentness is unknown or false")
    return {field: value[field] for field in SELECTION_FACT_FIELDS}


def _verify_selectable_target(value: Any, quest_ref: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FailClosed("selectable target must be structured")
    anchor = _verify_question_anchor(value.get("question_anchor"))
    question_ref = anchor["question_ref"]
    if value.get("question_ref", question_ref) != question_ref:
        raise FailClosed("selectable target QuestionRef conflicts with its QuestionAnchor")
    presence_fact = _verify_selection_fact(
        value.get("graph_presence_fact"),
        kind="GraphPresenceFact",
        question_ref=question_ref,
        quest_ref=quest_ref,
        expected_value="present",
    )
    raw_state_fact = value.get("question_research_state_fact")
    if (
        isinstance(raw_state_fact, Mapping)
        and raw_state_fact.get("value") not in ALLOWED_QUESTION_RESEARCH_STATES
    ):
        raise FailClosed("Question research state is not recognized")
    state_fact = _verify_selection_fact(
        raw_state_fact,
        kind="QuestionResearchStateFact",
        question_ref=question_ref,
        quest_ref=quest_ref,
        expected_value="open",
    )
    if presence_fact["graph_revision_ref"] != state_fact["graph_revision_ref"]:
        raise FailClosed("Question selection facts come from different graph revisions")
    return {
        "question_anchor": anchor,
        "graph_presence_fact": presence_fact,
        "question_research_state_fact": state_fact,
    }


def _verify_skip_basis_refs_by_stage(
    value: Any, entry_stage: str
) -> Dict[str, List[str]]:
    if entry_stage not in ALLOWED_ENTRY_STAGES:
        raise FailClosed("next-Cycle entry Stage is not recognized")
    required_stages = list(ORDERED_STAGES[: ORDERED_STAGES.index(entry_stage)])
    if value is None:
        provided: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        provided = value
    else:
        raise FailClosed("typed skip basis must be grouped by skipped Stage")
    if set(provided) != set(required_stages):
        raise FailClosed("typed skip basis must cover exactly the skipped Stages")
    normalized: Dict[str, List[str]] = {}
    for stage in required_stages:
        refs = _require_ref_list(
            provided.get(stage), f"typed_skip_basis_refs_by_stage.{stage}"
        )
        if not refs:
            raise FailClosed(f"skipped Stage {stage} needs typed basis refs")
        normalized[stage] = refs
    return normalized


def make_next_cycle_proposal(
    request: Mapping[str, Any],
    selectable_target: Mapping[str, Any],
    entry_stage: str,
    typed_skip_basis_refs_by_stage: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Build the sole continuation form without starting a Cycle."""

    frozen = verify_stage_run_request(request)
    quest_ref = frozen["research_context"]["quest_ref"]
    target = _verify_selectable_target(selectable_target, quest_ref)
    skip_refs_by_stage = _verify_skip_basis_refs_by_stage(
        typed_skip_basis_refs_by_stage, entry_stage
    )

    anchor = target["question_anchor"]
    return {
        "kind": "NextCycleProposal",
        "stage_run_request_ref": request["ref"],
        "foreground_epoch_ref": request["foreground_epoch_ref"],
        "root_session_ref": request["root_session_ref"],
        "quest_ref": quest_ref,
        "source_question_ref": request["question_ref"],
        "question_ref": anchor["question_ref"],
        "question_anchor": dict(anchor),
        "graph_presence_fact": dict(target["graph_presence_fact"]),
        "question_research_state_fact": dict(
            target["question_research_state_fact"]
        ),
        "entry_stage": entry_stage,
        "typed_skip_basis_refs_by_stage": skip_refs_by_stage,
        "is_authoritative": False,
    }


def create_question_then_propose_next_cycle(
    request: Mapping[str, Any],
    ports: ReasoningSemanticPorts,
    direction: Mapping[str, Any],
    entry_stage: str = "Idea",
    typed_skip_basis_refs_by_stage: Optional[Mapping[str, Sequence[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Keep Question creation internal until a formal selectable target exists.

    TODO-IMPL(question-creation.create_question; source=#85,#105) and
    TODO-IMPL(advancement-engine.start_successor_cycle; source=#58,#104,#105)
    remain semantic seams. create_question is the high-level lifecycle entry;
    the fake creates neither the Question nor the Cycle.
    """

    frozen = verify_stage_run_request(request)
    normalized_skip_basis = _verify_skip_basis_refs_by_stage(
        typed_skip_basis_refs_by_stage, entry_stage
    )
    requested_creation_mode = direction.get(
        "creation_mode", "AutonomousCreation"
    )
    if requested_creation_mode != "AutonomousCreation":
        raise FailClosed("Reasoning can only invoke AutonomousCreation")

    question_text = _require_text(direction.get("question_text"), "question direction")
    question_operation = direction.get("mode", "new")
    if question_operation not in {"new", "decompose"}:
        raise FailClosed("Question operation must be new or decompose")
    internal_direction = {
        "kind": "AutonomousQuestionDirection",
        "creation_mode": "AutonomousCreation",
        "mode": question_operation,
        "source_stage_run_request_ref": request["ref"],
        "source_foreground_epoch_ref": request["foreground_epoch_ref"],
        "source_accepted_question_binding_ref": request[
            "accepted_question_binding"
        ]["ref"],
        "source_cycle_ref": frozen["research_context"]["current_cycle_ref"],
        "source_question_ref": request["question_ref"],
        "source_quest_ref": frozen["research_context"]["quest_ref"],
        "question_text": question_text,
        "rationale": _require_text(
            direction.get(
                "rationale", "follow-up suggested by current evidence"
            ),
            "question direction rationale",
        ),
    }
    parent_question_ref = direction.get("parent_question_ref")
    if parent_question_ref is not None:
        internal_direction["parent_question_ref"] = _require_ref(
            parent_question_ref, "parent_question_ref"
        )
    if question_operation == "decompose":
        if parent_question_ref is None:
            raise FailClosed("decomposition needs a parent QuestionRef")
        decomposition_basis_refs = _require_ref_list(
            direction.get("decomposition_basis_refs"),
            "decomposition_basis_refs",
        )
        if not decomposition_basis_refs:
            raise FailClosed("decomposition needs explicit basis refs")
        internal_direction["decomposition_basis_refs"] = decomposition_basis_refs
    create_question = getattr(ports, "create_question", None)
    if not callable(create_question):
        return None
    response = create_question(internal_direction)
    if not isinstance(response, Mapping):
        raise FailClosed("Question creation port returned an invalid response")
    selectable_target = response.get("selectable_target")
    if selectable_target is None:
        return None
    return make_next_cycle_proposal(
        request,
        selectable_target,
        entry_stage,
        normalized_skip_basis,
    )


def make_candidate_completion(
    request: Mapping[str, Any],
    rationale: str,
    completion_basis_refs: Sequence[str],
) -> Dict[str, Any]:
    """Build a non-authoritative claim that the current Quest Goal is met."""

    frozen = verify_stage_run_request(request)
    research_context = frozen["research_context"]
    basis_refs = _require_ref_list(
        completion_basis_refs, "completion_basis_refs"
    )
    if not basis_refs:
        raise FailClosed("CandidateCompletion needs an explicit basis")
    return {
        "kind": "CandidateCompletion",
        "stage_run_request_ref": request["ref"],
        "foreground_epoch_ref": request["foreground_epoch_ref"],
        "accepted_question_binding_ref": request["accepted_question_binding"]["ref"],
        "question_ref": request["question_ref"],
        "root_session_ref": request["root_session_ref"],
        "quest_ref": research_context["quest_ref"],
        "goal_revision_ref": research_context["goal_revision_ref"],
        "active_graph_snapshot_ref": research_context["active_graph_snapshot_ref"],
        "rationale": _require_text(rationale, "completion rationale"),
        "completion_basis_refs": basis_refs,
        "is_authoritative": False,
    }


def _normalize_candidate_completion(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") != "CandidateCompletion":
        raise FailClosed("a CandidateCompletion is required")
    if value.get("is_authoritative") is not False:
        raise FailClosed("CandidateCompletion must remain non-authoritative")
    for field in (
        "stage_run_request_ref",
        "foreground_epoch_ref",
        "accepted_question_binding_ref",
        "question_ref",
        "root_session_ref",
        "quest_ref",
        "goal_revision_ref",
        "active_graph_snapshot_ref",
    ):
        _require_ref(value.get(field), f"candidate_completion.{field}")
    basis_refs = _require_ref_list(
        value.get("completion_basis_refs"), "completion_basis_refs"
    )
    if not basis_refs:
        raise FailClosed("CandidateCompletion needs an explicit basis")
    return {
        "kind": "CandidateCompletion",
        "stage_run_request_ref": value["stage_run_request_ref"],
        "foreground_epoch_ref": value["foreground_epoch_ref"],
        "accepted_question_binding_ref": value["accepted_question_binding_ref"],
        "question_ref": value["question_ref"],
        "root_session_ref": value["root_session_ref"],
        "quest_ref": value["quest_ref"],
        "goal_revision_ref": value["goal_revision_ref"],
        "active_graph_snapshot_ref": value["active_graph_snapshot_ref"],
        "rationale": _require_text(value.get("rationale"), "completion rationale"),
        "completion_basis_refs": basis_refs,
        "is_authoritative": False,
    }


def choose_reasoning_transition(
    outcome: Mapping[str, Any],
    *,
    next_cycle: Optional[Mapping[str, Any]] = None,
    completion: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Bind exactly one sanitized transition to the same scientific outcome."""

    if not isinstance(outcome, Mapping) or outcome.get("kind") != "ScientificOutcomeCandidate":
        raise FailClosed("a ScientificOutcomeCandidate is required")
    if outcome.get("disposition") not in ALLOWED_DISPOSITIONS:
        raise FailClosed("scientific outcome disposition is not recognized")
    for field in (
        "stage_run_request_ref",
        "foreground_epoch_ref",
        "accepted_question_binding_ref",
        "question_ref",
        "root_session_ref",
        "quest_ref",
        "goal_revision_ref",
        "active_graph_snapshot_ref",
    ):
        _require_ref(outcome.get(field), f"scientific_outcome.{field}")
    if not isinstance(outcome.get("research_synthesis"), Mapping):
        raise FailClosed("scientific outcome needs its research synthesis")

    if (next_cycle is None) == (completion is None):
        raise FailClosed(
            "Reasoning must choose exactly one NextCycleProposal or CandidateCompletion"
        )
    chosen = next_cycle if next_cycle is not None else completion
    if not isinstance(chosen, Mapping):
        raise FailClosed("Reasoning transition candidate must be structured")
    expected_kind = "NextCycleProposal" if next_cycle is not None else "CandidateCompletion"
    if chosen.get("kind") != expected_kind or chosen.get("is_authoritative") is not False:
        raise FailClosed("Reasoning transition candidate has an invalid boundary")

    shared_bindings = (
        "stage_run_request_ref",
        "foreground_epoch_ref",
        "root_session_ref",
        "quest_ref",
    )
    for field in shared_bindings:
        if chosen.get(field) != outcome.get(field):
            raise FailClosed(f"Reasoning transition is not bound to the same {field}")

    if next_cycle is not None:
        if chosen.get("source_question_ref") != outcome.get("question_ref"):
            raise FailClosed("NextCycleProposal has the wrong source Question")
        target = _verify_selectable_target(chosen, outcome["quest_ref"])
        anchor = target["question_anchor"]
        if chosen.get("question_ref") != anchor["question_ref"]:
            raise FailClosed("NextCycleProposal target QuestionRef is inconsistent")
        entry_stage = chosen.get("entry_stage")
        skip_refs_by_stage = _verify_skip_basis_refs_by_stage(
            chosen.get("typed_skip_basis_refs_by_stage"), entry_stage
        )
        return {
            "kind": "NextCycleProposal",
            "stage_run_request_ref": outcome["stage_run_request_ref"],
            "foreground_epoch_ref": outcome["foreground_epoch_ref"],
            "root_session_ref": outcome["root_session_ref"],
            "quest_ref": outcome["quest_ref"],
            "source_question_ref": outcome["question_ref"],
            "question_ref": anchor["question_ref"],
            "question_anchor": anchor,
            "graph_presence_fact": target["graph_presence_fact"],
            "question_research_state_fact": target[
                "question_research_state_fact"
            ],
            "entry_stage": entry_stage,
            "typed_skip_basis_refs_by_stage": skip_refs_by_stage,
            "is_authoritative": False,
        }

    normalized_completion = _normalize_candidate_completion(chosen)
    for field in ("accepted_question_binding_ref", "question_ref", "goal_revision_ref"):
        if normalized_completion.get(field) != outcome.get(field):
            raise FailClosed(f"CandidateCompletion is not bound to the same {field}")
    if normalized_completion.get("active_graph_snapshot_ref") != outcome.get(
        "active_graph_snapshot_ref"
    ):
        raise FailClosed("CandidateCompletion is not bound to the analysis snapshot")
    return normalized_completion


def _verify_current_reply(
    reply: OwnerReply, candidate: Mapping[str, Any], seam: str
) -> None:
    _require_ref(reply.receipt_ref, f"{seam} feedback receipt")
    if reply.is_current is not True:
        raise FailClosed(f"{seam} feedback currentness is unknown or false")
    if reply.subject_stage_run_request_ref != candidate.get("stage_run_request_ref"):
        raise FailClosed(f"{seam} feedback is not bound to this StageRunRequest")
    if reply.subject_question_ref != candidate.get("question_ref"):
        raise FailClosed(f"{seam} feedback is not bound to this Question")
    if reply.subject_root_session_ref != candidate.get("root_session_ref"):
        raise FailClosed(f"{seam} feedback is not bound to this root Session")


def _normalize_scientific_outcome_for_submission(
    request: Mapping[str, Any], candidate: Any
) -> Dict[str, Any]:
    """Rebuild a candidate from its request before any Owner side effect."""

    if not isinstance(candidate, Mapping):
        raise FailClosed("Answer/Evidence submission needs a structured candidate")
    snapshot = deepcopy(dict(candidate))
    raw_evidence = snapshot.get("evidence")
    if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, str):
        raise FailClosed("Answer/Evidence candidate evidence must be a list")
    evidence_citations: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_evidence):
        if not isinstance(item, Mapping):
            raise FailClosed(f"candidate evidence[{index}] is not structured")
        kind = item.get("kind")
        citation = {
            "kind": kind,
            "ref": item.get("ref"),
            "finding": item.get("finding"),
        }
        if kind == "LiteratureRecord":
            citation["evidence_basis"] = item.get("evidence_basis")
        elif kind == "TargetClosureLeaf":
            citation.update(
                {
                    "role": item.get("role"),
                    "source_target_commit_ref": item.get(
                        "source_target_commit_ref"
                    ),
                    "source_evaluation_attempt_ref": item.get(
                        "source_evaluation_attempt_ref"
                    ),
                }
            )
        elif kind != "EvidenceReuseLeaf":
            raise FailClosed("candidate evidence kind is not recognized")
        evidence_citations.append(citation)

    proposed = {
        "disposition": snapshot.get("disposition"),
        "claim": snapshot.get("claim"),
        "support_scope": snapshot.get("support_scope"),
        "limitations": deepcopy(snapshot.get("limitations")),
        "missing_evidence": deepcopy(snapshot.get("missing_evidence")),
        "uncertainty_basis": deepcopy(snapshot.get("uncertainty_basis")),
        "causal_interpretation": deepcopy(snapshot.get("causal_interpretation")),
        "bundle_replan_interpretations": deepcopy(
            snapshot.get("bundle_replan_interpretations")
        ),
        "research_synthesis": deepcopy(snapshot.get("research_synthesis")),
    }
    rebuilt = build_scientific_outcome(request, evidence_citations, proposed)
    if snapshot != rebuilt:
        raise FailClosed(
            "Answer/Evidence candidate is not the canonical result of its StageRunRequest"
        )
    return rebuilt


def _normalize_scientific_outcome_revision(
    original: Mapping[str, Any],
    value: Any,
    rejection_receipt_ref: str,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") != "ScientificOutcomeCandidate":
        raise FailClosed("Answer/Evidence revision must remain a ScientificOutcomeCandidate")
    if value.get("revision_of") != rejection_receipt_ref:
        raise FailClosed("Answer/Evidence revision must bind the rejection receipt")
    if "replan_required" in value:
        raise FailClosed("Reasoning revision cannot mint a local replan_required value")
    immutable_fields = (
        "stage_run_request_ref",
        "foreground_epoch_ref",
        "accepted_question_binding_ref",
        "question_anchor_ref",
        "question_ref",
        "root_session_ref",
        "quest_ref",
        "goal_revision_ref",
        "active_graph_snapshot_ref",
        "frozen_input_refs",
        "evidence",
    )
    for field in immutable_fields:
        if value.get(field) != original.get(field):
            raise FailClosed(f"Answer/Evidence revision changed immutable {field}")
    if value.get("is_owner_accepted") is not False:
        raise FailClosed("a revised candidate cannot self-accept")
    if value.get("is_stage_advanced") is not False:
        raise FailClosed("a revised candidate cannot advance its Stage")

    disposition = value.get("disposition")
    if disposition not in ALLOWED_DISPOSITIONS:
        raise FailClosed("revised scientific disposition is not recognized")
    evidence = original.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        raise FailClosed("original scientific evidence closure is invalid")
    missing = _require_text_list(
        value.get("missing_evidence", []), "revised missing_evidence"
    )
    uncertainty_basis = _require_text_list(
        value.get("uncertainty_basis", []), "revised uncertainty_basis"
    )
    limitations = _require_text_list(
        value.get("limitations", []), "revised limitations"
    )
    claim = _verify_disposition_boundary(
        disposition,
        value.get("claim"),
        evidence,
        missing,
        uncertainty_basis,
        field_prefix="revised",
    )

    original_causal = original.get("causal_interpretation")
    revised_causal = value.get("causal_interpretation")
    if not isinstance(original_causal, Mapping) or not isinstance(
        revised_causal, Mapping
    ):
        raise FailClosed("revised causal interpretation is required")
    causal_ref_fields = (
        "target_commit_refs",
        "changed_axis_fact_refs",
        "held_fixed_fact_refs",
        "provenance_refs",
        "attribution_basis_refs",
    )
    for field in causal_ref_fields:
        if revised_causal.get(field) != original_causal.get(field):
            raise FailClosed(f"revised causal interpretation changed {field}")
    normalized_causal = {
        field: list(original_causal[field]) for field in causal_ref_fields
    }
    normalized_causal.update(
        {
            "claim_scope": _require_text(
                revised_causal.get("claim_scope"), "revised causal claim_scope"
            ),
            "statement": _require_text(
                revised_causal.get("statement"), "revised causal statement"
            ),
            "sufficiency_rationale": _require_text(
                revised_causal.get("sufficiency_rationale"),
                "revised causal sufficiency_rationale",
            ),
            "confounders": _require_text_list(
                revised_causal.get("confounders", []),
                "revised causal confounders",
            ),
        }
    )

    original_synthesis = original.get("research_synthesis")
    revised_synthesis = value.get("research_synthesis")
    if not isinstance(original_synthesis, Mapping) or not isinstance(
        revised_synthesis, Mapping
    ):
        raise FailClosed("revised research synthesis is required")
    for field in ("context_ref", "scope_refs"):
        if revised_synthesis.get(field) != original_synthesis.get(field):
            raise FailClosed(f"revised research synthesis changed {field}")
    normalized_synthesis = {
        "context_ref": original_synthesis["context_ref"],
        "scope_refs": list(original_synthesis["scope_refs"]),
        "narrative": _require_text(
            revised_synthesis.get("narrative"), "revised research narrative"
        ),
        "uncertainties": _require_text_list(
            revised_synthesis.get("uncertainties", []),
            "revised research uncertainties",
        ),
    }

    original_replans = original.get("bundle_replan_interpretations")
    revised_replans = value.get("bundle_replan_interpretations")
    if (
        not isinstance(original_replans, Sequence)
        or isinstance(original_replans, str)
        or not isinstance(revised_replans, Sequence)
        or isinstance(revised_replans, str)
        or len(revised_replans) != len(original_replans)
    ):
        raise FailClosed("revised Bundle replan interpretation set changed")
    normalized_replans: List[Dict[str, Any]] = []
    for index, original_replan in enumerate(original_replans):
        revised_replan = revised_replans[index]
        if not isinstance(original_replan, Mapping) or not isinstance(
            revised_replan, Mapping
        ):
            raise FailClosed("revised Bundle replan interpretation is invalid")
        for field in (
            "source_candidate_ref",
            "source_bundle_stage_commit_ref",
            "source_basis_refs",
        ):
            if revised_replan.get(field) != original_replan.get(field):
                raise FailClosed(f"revised Bundle replan changed {field}")
        normalized_replans.append(
            {
                "source_candidate_ref": original_replan["source_candidate_ref"],
                "source_bundle_stage_commit_ref": original_replan[
                    "source_bundle_stage_commit_ref"
                ],
                "source_basis_refs": list(original_replan["source_basis_refs"]),
                "interpretation": _require_text(
                    revised_replan.get("interpretation"),
                    f"revised Bundle replan interpretation {index}",
                ),
            }
        )

    return {
        "kind": "ScientificOutcomeCandidate",
        **{field: original[field] for field in immutable_fields},
        "disposition": disposition,
        "claim": claim,
        "support_scope": _require_text(
            value.get("support_scope"), "revised support_scope"
        ),
        "limitations": limitations,
        "missing_evidence": missing,
        "uncertainty_basis": uncertainty_basis,
        "causal_interpretation": normalized_causal,
        "research_synthesis": normalized_synthesis,
        "bundle_replan_interpretations": normalized_replans,
        "revision_of": rejection_receipt_ref,
        "is_owner_accepted": False,
        "is_stage_advanced": False,
    }


def submit_answer_with_feedback(
    ports: ReasoningSemanticPorts,
    candidate: Mapping[str, Any],
    revise_after_rejection: Callable[
        [Mapping[str, Any], OwnerReply], Mapping[str, Any]
    ],
    *,
    request: Mapping[str, Any],
) -> OwnerReply:
    """Exercise a same-Session rejection/revision loop with a fake Owner port.

    TODO-CONTRACT(#64: real Answer/Evidence identity, submission, validity,
    revalidation, and feedback receipt semantics remain unresolved).
    """

    submit = getattr(ports, "submit_answer_candidate", None)
    if not callable(submit):
        raise FailClosed("Answer/Evidence port is unavailable")
    canonical = _normalize_scientific_outcome_for_submission(request, candidate)
    reply = submit(deepcopy(canonical))
    _verify_current_reply(reply, canonical, "Answer/Evidence")
    if reply.disposition == "accepted":
        return reply
    if reply.disposition != "rejected":
        raise FailClosed(
            f"answer feedback is {reply.disposition!r}, not safely actionable"
        )
    revision = _normalize_scientific_outcome_revision(
        canonical,
        revise_after_rejection(deepcopy(canonical), reply),
        reply.receipt_ref,
    )
    second_reply = submit(deepcopy(revision))
    _verify_current_reply(second_reply, revision, "revised Answer/Evidence")
    if second_reply.disposition not in {"accepted", "rejected"}:
        raise FailClosed("revised Answer/Evidence feedback is not terminal")
    return second_reply


def submit_confirmed_completion_candidate(
    ports: ReasoningSemanticPorts,
    candidate: Mapping[str, Any],
    user_confirmation_receipt_ref: str,
) -> OwnerReply:
    """Submit a user-confirmed candidate through the unresolved Goal seam.

    TODO-CONTRACT(#89: exact user-confirmation binding, Goal/Completion Owner
    operation, acceptance/rejection, reopen, and AE ending semantics remain
    unresolved). This helper never changes a Goal or Quest; even a fixture
    Owner acceptance is not an Advancement Engine ending fact.
    """

    normalized = _normalize_candidate_completion(candidate)
    confirmation_ref = _require_ref(
        user_confirmation_receipt_ref, "user_confirmation_receipt_ref"
    )
    submit = getattr(ports, "submit_confirmed_completion_candidate", None)
    if not callable(submit):
        raise FailClosed("Goal/Completion port is unavailable")
    confirmed_submission = {
        **normalized,
        "user_confirmation_receipt_ref": confirmation_ref,
    }
    reply = submit(confirmed_submission)
    _verify_current_reply(reply, confirmed_submission, "Goal/Completion")
    if reply.disposition not in {"accepted", "rejected"}:
        raise FailClosed(f"completion feedback is {reply.disposition!r}, not terminal")
    return reply
