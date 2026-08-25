from __future__ import annotations

from typing import cast

from meta_research.bundle_protocol import BUNDLE_ROOT_MAX_SERIALIZED_BYTES
from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
    NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF,
    BundleTargetContractError,
    LegacyV2TargetSpec,
    apply_strategy_update,
    normalized_completion_contract_from_dict,
    parse_legacy_v2_target_spec,
    start_rolling_strategy,
    strategy_update_from_dict,
)
from meta_research.owners.common import canonical_hash, canonical_json


BUNDLE_CONTEXT_PACK_SCHEMA_REF = "meta-research/bundle-context-pack/v1"
BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF = "meta-research/bundle-context-pack/v2"
LEGACY_TARGET_PLAN_SCHEMA_REF = "meta-research/target-plan/v2"
TARGET_PLAN_SCHEMA_REF = "meta-research/target-plan/v3"
TARGET_PLAN_REVIEW_SCHEMA_REF = "meta-research/target-plan-review/v1"
TARGET_GRAPH_APPEND_PROPOSAL_SCHEMA_REF = (
    "meta-research/target-graph-append-proposal/v2"
)
MAX_BUNDLE_TARGETS = 64
MAX_BUNDLE_TARGET_PLAN_BYTES = BUNDLE_ROOT_MAX_SERIALIZED_BYTES


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
    accepted_idea_set_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    schema_ref = context_pack.get("schema_ref")
    expected_keys = {
        "schema_ref",
        "cycle_ref",
        "accepted_question_binding",
        "accepted_formal_plan_binding",
    }
    if schema_ref == BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF:
        expected_keys.add("accepted_idea_set_binding")
    _exact_keys(
        context_pack,
        expected_keys,
        "bundle_context_pack_invalid",
    )
    if (
        schema_ref
        not in {
            BUNDLE_CONTEXT_PACK_SCHEMA_REF,
            BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF,
        }
        or context_pack.get("cycle_ref") != cycle_ref
        or context_pack.get("accepted_question_binding") != accepted_question_binding
        or context_pack.get("accepted_formal_plan_binding")
        != accepted_formal_plan_binding
        or (
            schema_ref == BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF
            and (
                not isinstance(
                    context_pack.get("accepted_idea_set_binding"), dict
                )
                or (
                    accepted_idea_set_binding is not None
                    and context_pack.get("accepted_idea_set_binding")
                    != accepted_idea_set_binding
                )
            )
        )
        or (
            schema_ref == BUNDLE_CONTEXT_PACK_SCHEMA_REF
            and accepted_idea_set_binding is not None
        )
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
    if schema_ref == BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF:
        idea_binding = cast(
            dict[str, object], context_pack["accepted_idea_set_binding"]
        )
        idea_set = idea_binding.get("idea_set")
        if (
            not isinstance(idea_set, dict)
            or idea_set.get("question_ref")
            != accepted_question_binding.get("question_ref")
            or contract.get("source_idea_set_ref")
            != idea_binding.get("outcome_ref")
        ):
            raise BundleContractError("bundle_context_pack_invalid")
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
    if (
        len(canonical_json(target_plan).encode("utf-8"))
        > MAX_BUNDLE_TARGET_PLAN_BYTES
    ):
        raise BundleContractError("target_plan_too_large")
    _exact_keys(
        target_plan,
        {
            "schema_ref",
            "kind",
            "formal_plan_ref",
            "context_pack_ref",
            "completion_contract",
            "initial_strategy_update",
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

    completion_value = target_plan.get("completion_contract")
    update_value = target_plan.get("initial_strategy_update")
    if not isinstance(completion_value, dict) or not isinstance(update_value, dict):
        raise BundleContractError("target_plan_formal_contract_invalid")
    try:
        completion = normalized_completion_contract_from_dict(
            completion_value,
            plan_document=plan_document,
        )
        update = strategy_update_from_dict(
            update_value,
            completion_contract=completion,
        )
        if (
            update.update.revision != 1
            or update.update.requires_accepted_labels
            or not update.candidates
            or len(update.candidates) > MAX_BUNDLE_TARGETS
        ):
            raise BundleTargetContractError("initial_strategy_update_invalid")
        apply_strategy_update(
            start_rolling_strategy(completion),
            update,
            completion_contract=completion,
        )
    except BundleTargetContractError as error:
        raise BundleContractError(str(error)) from error
    return canonical_hash(target_plan)


def diagnose_legacy_target_plan_v2(
    target_plan: dict[str, object],
) -> tuple[LegacyV2TargetSpec, ...]:
    """Read a v2 minimal plan for diagnostics; never yield completion state.

    A legacy document which asserted ``strategy_complete=true`` is rejected:
    the old gap/count slice is not allowed to masquerade as the fixed Bundle
    completion contract.
    """

    _exact_keys(
        target_plan,
        {
            "schema_ref",
            "kind",
            "formal_plan_ref",
            "context_pack_ref",
            "targets",
            "strategy_complete",
            "source_bindings",
        },
        "legacy_target_plan_v2_invalid",
    )
    targets = target_plan.get("targets")
    if (
        target_plan.get("schema_ref") != LEGACY_TARGET_PLAN_SCHEMA_REF
        or target_plan.get("kind") != "TargetPlan"
        or not _text(target_plan.get("formal_plan_ref"))
        or not _text(target_plan.get("context_pack_ref"))
        or target_plan.get("strategy_complete") is not False
        or not isinstance(target_plan.get("source_bindings"), dict)
        or not isinstance(targets, list)
        or not targets
        or len(targets) > MAX_BUNDLE_TARGETS
    ):
        raise BundleContractError("legacy_target_plan_v2_invalid")
    try:
        return tuple(parse_legacy_v2_target_spec(value) for value in targets)
    except BundleTargetContractError as error:
        raise BundleContractError(str(error)) from error


def target_graph_append_proposal(
    *,
    graph_ref: str,
    base_generation: int,
    base_head_receipt: dict[str, object],
    strategy_update: dict[str, object],
) -> dict[str, object]:
    """Build the canonical AR-reviewed slice offered to RG.

    This envelope intentionally carries target keys rather than TargetRefs.  RG
    remains the sole allocator of Target identity and dependency refs.
    """

    value: dict[str, object] = {
        "schema_ref": TARGET_GRAPH_APPEND_PROPOSAL_SCHEMA_REF,
        "kind": "TargetGraphAppendProposal",
        "graph_ref": graph_ref,
        "base_generation": base_generation,
        "base_head_receipt": base_head_receipt,
        "strategy_update": strategy_update,
    }
    validate_target_graph_append_proposal(value)
    return value


def validate_target_graph_append_proposal(
    proposal: dict[str, object],
) -> str:
    _exact_keys(
        proposal,
        {
            "schema_ref",
            "kind",
            "graph_ref",
            "base_generation",
            "base_head_receipt",
            "strategy_update",
        },
        "target_graph_append_proposal_invalid",
    )
    base_generation = proposal.get("base_generation")
    update = proposal.get("strategy_update")
    receipt = proposal.get("base_head_receipt")
    if (
        proposal.get("schema_ref") != TARGET_GRAPH_APPEND_PROPOSAL_SCHEMA_REF
        or proposal.get("kind") != "TargetGraphAppendProposal"
        or not _text(proposal.get("graph_ref"))
        or not isinstance(base_generation, int)
        or isinstance(base_generation, bool)
        or base_generation < 0
        or not isinstance(receipt, dict)
        or not isinstance(update, dict)
    ):
        raise BundleContractError("target_graph_append_proposal_invalid")
    _exact_keys(
        update,
        {
            "schema_ref",
            "revision",
            "candidates",
            "requires_accepted_labels",
            "strategy_complete",
        },
        "target_graph_append_proposal_invalid",
    )
    revision = update.get("revision")
    candidates = update.get("candidates")
    required = update.get("requires_accepted_labels")
    strategy_complete = update.get("strategy_complete")
    if (
        update.get("schema_ref") != FORMAL_STRATEGY_UPDATE_SCHEMA_REF
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(candidates, list)
        or len(candidates) > MAX_BUNDLE_TARGETS
        or any(not isinstance(candidate, dict) for candidate in candidates)
        or not isinstance(required, list)
        or any(not _text(label) for label in required)
        or len(required) != len(set(cast(list[str], required)))
        or not isinstance(strategy_complete, bool)
        or (not candidates and strategy_complete is not True)
    ):
        raise BundleContractError("target_graph_append_proposal_invalid")
    return canonical_hash(proposal)


def material_target_plan_hash(target_plan: dict[str, object]) -> str:
    """Hash only a closed formal v3 envelope.

    Full Plan-semantic validation happens at admission, where the accepted
    PlanDocument is available.  This storage hook still rejects legacy or
    open-shaped documents rather than assigning them a formal outcome hash.
    """

    _exact_keys(
        target_plan,
        {
            "schema_ref",
            "kind",
            "formal_plan_ref",
            "context_pack_ref",
            "completion_contract",
            "initial_strategy_update",
            "source_bindings",
        },
        "target_plan_invalid",
    )
    completion = target_plan.get("completion_contract")
    update = target_plan.get("initial_strategy_update")
    if (
        target_plan.get("schema_ref") != TARGET_PLAN_SCHEMA_REF
        or target_plan.get("kind") != "TargetPlan"
        or not _text(target_plan.get("formal_plan_ref"))
        or not _text(target_plan.get("context_pack_ref"))
        or not isinstance(completion, dict)
        or completion.get("schema_ref")
        != NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF
        or not isinstance(update, dict)
        or update.get("schema_ref") != FORMAL_STRATEGY_UPDATE_SCHEMA_REF
        or not isinstance(target_plan.get("source_bindings"), dict)
        or len(canonical_json(target_plan).encode("utf-8"))
        > MAX_BUNDLE_TARGET_PLAN_BYTES
    ):
        raise BundleContractError("target_plan_invalid")
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


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleContractError(code)
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise BundleContractError(code)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
