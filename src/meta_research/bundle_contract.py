from __future__ import annotations

import math
from typing import cast

from meta_research.owners.common import canonical_hash


BUNDLE_CONTEXT_PACK_SCHEMA_REF = "meta-research/bundle-context-pack/v1"
TARGET_PLAN_SCHEMA_REF = "meta-research/target-plan/v1"
TARGET_PLAN_REVIEW_SCHEMA_REF = "meta-research/target-plan-review/v1"
MAX_BUNDLE_TARGETS = 64


class BundleContractError(ValueError):
    pass


def target_execution_assertion(
    *,
    quest_ref: str,
    stage_request_ref: str,
    graph_ref: str,
    target_ref: str,
    target_spec_hash: str,
    risk_class: str,
) -> dict[str, object]:
    """Canonical exact subject used by the high-risk Target waiter."""

    return {
        "schema_ref": "meta-research/target-execution-assertion/v1",
        "operation": "execute_target",
        "quest_ref": quest_ref,
        "stage_request_ref": stage_request_ref,
        "graph_ref": graph_ref,
        "target_ref": target_ref,
        "target_spec_hash": target_spec_hash,
        "risk_class": risk_class,
    }


def target_execution_authorization_requirement(
    *,
    quest_ref: str,
    stage_request_ref: str,
    graph_ref: str,
    target_ref: str,
    target_spec_hash: str,
) -> dict[str, object]:
    """Canonical single-Target capability requirement verified by HC."""

    return {
        "capability": "execute_high_risk_target",
        "scope": {
            "authorization_mode": "single_target",
            "quest_ref": quest_ref,
            "stage_request_ref": stage_request_ref,
            "graph_ref": graph_ref,
            "target_ref": target_ref,
            "target_spec_hash": target_spec_hash,
        },
    }


def validate_bundle_context_pack(
    context_pack: dict[str, object],
    *,
    cycle_ref: str,
    accepted_question_binding: dict[str, object],
    accepted_formal_plan_binding: dict[str, object],
) -> dict[str, object]:
    _exact_keys(
        context_pack,
        {
            "schema_ref",
            "cycle_ref",
            "accepted_question_binding",
            "accepted_formal_plan_binding",
        },
        "bundle_context_pack_invalid",
    )
    if (
        context_pack.get("schema_ref") != BUNDLE_CONTEXT_PACK_SCHEMA_REF
        or context_pack.get("cycle_ref") != cycle_ref
        or context_pack.get("accepted_question_binding") != accepted_question_binding
        or context_pack.get("accepted_formal_plan_binding")
        != accepted_formal_plan_binding
    ):
        raise BundleContractError("bundle_context_pack_invalid")
    plan = accepted_formal_plan_binding.get("plan_document")
    if not isinstance(plan, dict):
        raise BundleContractError("bundle_formal_plan_binding_invalid")
    if (
        canonical_hash(plan) != accepted_formal_plan_binding.get("plan_document_hash")
        or plan.get("question_ref") != accepted_question_binding.get("question_ref")
        or plan.get("bundle_disposition")
        not in {"experiments_required", "no_new_experiment_required"}
        or not isinstance(plan.get("gap_set"), list)
        or not isinstance(plan.get("experiment_briefs"), list)
        or not isinstance(plan.get("evidence_reuse_set"), list)
    ):
        raise BundleContractError("bundle_formal_plan_binding_invalid")
    contract = plan.get("answer_contract")
    if not isinstance(contract, dict) or contract.get(
        "answer_contract_hash"
    ) != accepted_formal_plan_binding.get("answer_contract_hash"):
        raise BundleContractError("bundle_formal_plan_binding_invalid")
    gaps = cast(list[object], plan["gap_set"])
    expected = "experiments_required" if gaps else "no_new_experiment_required"
    if plan["bundle_disposition"] != expected:
        raise BundleContractError("bundle_formal_plan_binding_invalid")
    return plan


def validate_target_plan(
    target_plan: dict[str, object],
    *,
    formal_plan_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    plan_document: dict[str, object],
) -> str:
    _exact_keys(
        target_plan,
        {
            "schema_ref",
            "kind",
            "formal_plan_ref",
            "context_pack_ref",
            "targets",
            "source_bindings",
        },
        "target_plan_invalid",
    )
    if (
        target_plan.get("schema_ref") != TARGET_PLAN_SCHEMA_REF
        or target_plan.get("kind") != "TargetPlan"
        or target_plan.get("formal_plan_ref") != formal_plan_ref
        or target_plan.get("context_pack_ref") != context_pack_ref
    ):
        raise BundleContractError("target_plan_source_invalid")
    source = _object(target_plan.get("source_bindings"), "target_plan_source_invalid")
    if source != {
        "formal_plan_ref": formal_plan_ref,
        "plan_document_hash": canonical_hash(plan_document),
        "context_pack_ref": context_pack_ref,
        "context_pack_hash": context_pack_hash,
    }:
        raise BundleContractError("target_plan_source_invalid")

    plan_gaps = plan_document.get("gap_set")
    briefs = plan_document.get("experiment_briefs")
    targets = target_plan.get("targets")
    if (
        not isinstance(plan_gaps, list)
        or not isinstance(briefs, list)
        or not isinstance(targets, list)
        or not targets
        or len(targets) > MAX_BUNDLE_TARGETS
    ):
        raise BundleContractError("target_plan_invalid")
    brief_by_key: dict[str, dict[str, object]] = {}
    for value in briefs:
        brief = _object(value, "target_plan_brief_invalid")
        key = brief.get("experiment_key")
        if not _text(key) or key in brief_by_key:
            raise BundleContractError("target_plan_brief_invalid")
        brief_by_key[cast(str, key)] = brief

    by_key: dict[str, dict[str, object]] = {}
    covered_gaps: set[str] = set()
    for value in targets:
        target = _object(value, "target_spec_invalid")
        _exact_keys(
            target,
            {
                "target_key",
                "title",
                "target_type",
                "experiment_key",
                "gap_obligation_keys",
                "depends_on",
                "goal",
                "hypothesis",
                "variant_parameter",
                "sample_count",
                "boundary_constraints",
                "semantic_delta",
                "contributing_idea_refs",
                "risk_class",
            },
            "target_spec_invalid",
        )
        key = target.get("target_key")
        experiment_key = target.get("experiment_key")
        depends_on = target.get("depends_on")
        gaps = target.get("gap_obligation_keys")
        variant = target.get("variant_parameter")
        sample_count = target.get("sample_count")
        if (
            not _text(key)
            or key in by_key
            or experiment_key not in brief_by_key
            or target.get("target_type") != "micro_experiment"
            or target.get("risk_class") not in {"normal", "high"}
            or not isinstance(depends_on, list)
            or len(depends_on) != len(set(depends_on))
            or not all(_text(item) for item in depends_on)
            or not isinstance(gaps, list)
            or not gaps
            or len(gaps) != len(set(gaps))
            or not all(_text(item) for item in gaps)
            or not isinstance(variant, (int, float))
            or isinstance(variant, bool)
            or not math.isfinite(float(variant))
            or not isinstance(sample_count, int)
            or isinstance(sample_count, bool)
            or not 4 <= sample_count <= 4096
        ):
            raise BundleContractError("target_spec_invalid")
        for field in (
            "title",
            "goal",
            "hypothesis",
            "boundary_constraints",
            "semantic_delta",
        ):
            if not _text(target.get(field)):
                raise BundleContractError("target_spec_invalid")
        brief = brief_by_key[cast(str, experiment_key)]
        if (
            set(cast(list[str], gaps))
            != set(cast(list[str], brief["gap_obligation_keys"]))
            or target.get("goal") != brief.get("goal")
            or target.get("boundary_constraints") != brief.get("boundary_constraints")
            or target.get("semantic_delta") != brief.get("semantic_delta")
            or target.get("contributing_idea_refs")
            != brief.get("contributing_idea_refs")
        ):
            raise BundleContractError("target_spec_brief_drift")
        by_key[cast(str, key)] = target
        covered_gaps.update(cast(list[str], gaps))
    if covered_gaps != set(cast(list[str], plan_gaps)):
        raise BundleContractError("target_plan_gap_closure_invalid")
    if set(brief_by_key) != {
        cast(str, target["experiment_key"]) for target in by_key.values()
    }:
        raise BundleContractError("target_plan_brief_closure_invalid")

    for key, target in by_key.items():
        dependencies = cast(list[str], target["depends_on"])
        if key in dependencies or any(item not in by_key for item in dependencies):
            raise BundleContractError("target_dag_invalid")
    _assert_acyclic(by_key)
    return canonical_hash(target_plan)


def material_target_plan_hash(target_plan: dict[str, object]) -> str:
    return canonical_hash(target_plan)


def validate_target_plan_review(
    review: dict[str, object],
    *,
    reviewed_draft_hash: str,
    final_target_plan_hash: str,
) -> str:
    _exact_keys(
        review,
        {
            "schema_ref",
            "review_mode",
            "reviewer_agent_ref",
            "reviewed_draft_hash",
            "findings",
            "dispositions",
            "final_target_plan_hash",
            "independent",
            "advisory_only",
        },
        "target_plan_review_invalid",
    )
    findings = review.get("findings")
    dispositions = review.get("dispositions")
    if (
        review.get("schema_ref") != TARGET_PLAN_REVIEW_SCHEMA_REF
        or review.get("review_mode") != "harness_child_agent"
        or not _text(review.get("reviewer_agent_ref"))
        or review.get("reviewed_draft_hash") != reviewed_draft_hash
        or review.get("final_target_plan_hash") != final_target_plan_hash
        or review.get("independent") is not True
        or review.get("advisory_only") is not True
        or not isinstance(findings, list)
        or not isinstance(dispositions, list)
    ):
        raise BundleContractError("target_plan_review_invalid")
    finding_ids: set[str] = set()
    for value in findings:
        finding = _object(value, "target_plan_review_invalid")
        _exact_keys(
            finding,
            {"finding_id", "category", "message"},
            "target_plan_review_invalid",
        )
        if (
            not _text(finding.get("finding_id"))
            or finding["finding_id"] in finding_ids
            or finding.get("category")
            not in {"lineage", "dag", "dedup", "feasibility", "owner_boundary"}
            or not _text(finding.get("message"))
        ):
            raise BundleContractError("target_plan_review_invalid")
        finding_ids.add(cast(str, finding["finding_id"]))
    disposition_ids: set[str] = set()
    revised = False
    for value in dispositions:
        disposition = _object(value, "target_plan_review_invalid")
        _exact_keys(
            disposition,
            {"finding_id", "action", "rationale"},
            "target_plan_review_invalid",
        )
        if (
            disposition.get("finding_id") not in finding_ids
            or disposition["finding_id"] in disposition_ids
            or disposition.get("action") not in {"revised", "not_adopted"}
            or not _text(disposition.get("rationale"))
        ):
            raise BundleContractError("target_plan_review_invalid")
        disposition_ids.add(cast(str, disposition["finding_id"]))
        revised = revised or disposition.get("action") == "revised"
    if disposition_ids != finding_ids:
        raise BundleContractError("target_plan_review_invalid")
    changed = reviewed_draft_hash != final_target_plan_hash
    if changed != revised:
        raise BundleContractError(
            "target_plan_review_revision_not_material"
            if revised
            else "target_plan_changed_without_review_revision"
        )
    return canonical_hash(review)


def _assert_acyclic(by_key: dict[str, dict[str, object]]) -> None:
    active: set[str] = set()
    complete: set[str] = set()

    def visit(key: str) -> None:
        if key in complete:
            return
        if key in active:
            raise BundleContractError("target_dag_invalid")
        active.add(key)
        for dependency in cast(list[str], by_key[key]["depends_on"]):
            visit(dependency)
        active.remove(key)
        complete.add(key)

    for key in by_key:
        visit(key)


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleContractError(code)
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise BundleContractError(code)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
