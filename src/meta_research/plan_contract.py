from __future__ import annotations

from typing import cast

from meta_research.idea_contract import (
    DISPOSITION_ACTIONS,
    REVIEW_CATEGORIES,
    IdeaContractError,
    validate_idea_outcome,
)
from meta_research.owners.common import canonical_hash, canonical_json


PLAN_CONTEXT_PACK_SCHEMA_REF = "meta-research/plan-context-pack/v1"
PLAN_DOCUMENT_SCHEMA_REF = "meta-research/plan-document/v1"
PLAN_REVIEW_SCHEMA_REF = "meta-research/plan-advisory-review/v1"
EVIDENCE_REF_SCHEMA_REF = "meta-research/evidence-ref/v1"
MAX_PLAN_OBLIGATIONS = 64
MAX_PLAN_EVIDENCE_REFS = 256
MAX_PLAN_EXPERIMENT_BRIEFS = 64

_QUESTION_TRACE_FIELDS = {
    "unknown_statement",
    "answer_shape",
    "applicability_scope",
}
_IDEA_ROLES = {"query_lens", "experiment_lens", "not_relevant"}
_COVERAGE_DISPOSITIONS = {"covered", "gap"}
_BUNDLE_DISPOSITIONS = {
    "experiments_required",
    "no_new_experiment_required",
}


class PlanContractError(ValueError):
    """A Plan candidate or its frozen invocation closure is invalid."""


def validate_plan_context_pack(
    context_pack: dict[str, object],
    *,
    cycle_ref: str,
    accepted_question_binding: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Validate the exact Plan inputs and return EvidenceRefs by identity."""

    _exact_keys(
        context_pack,
        {
            "schema_ref",
            "cycle_ref",
            "accepted_question_binding",
            "accepted_idea_set_binding",
            "evidence_catalog",
            "evidence_reference_revision",
        },
        "plan_context_pack_invalid",
    )
    if (
        context_pack.get("schema_ref") != PLAN_CONTEXT_PACK_SCHEMA_REF
        or context_pack.get("cycle_ref") != cycle_ref
        or context_pack.get("accepted_question_binding")
        != accepted_question_binding
    ):
        raise PlanContractError("plan_context_pack_invalid")

    question_ref = accepted_question_binding.get("question_ref")
    if not _text(question_ref):
        raise PlanContractError("plan_context_pack_invalid")
    idea_binding = _object(
        context_pack.get("accepted_idea_set_binding"),
        "plan_idea_set_binding_invalid",
    )
    _validate_idea_set_binding(idea_binding, question_ref=cast(str, question_ref))

    revision = context_pack.get("evidence_reference_revision")
    catalog = context_pack.get("evidence_catalog")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(catalog, list)
        or len(catalog) > MAX_PLAN_EVIDENCE_REFS
        or revision != len(catalog)
    ):
        raise PlanContractError("plan_evidence_catalog_invalid")

    evidence_by_ref: dict[str, dict[str, object]] = {}
    version_refs: set[str] = set()
    for value in catalog:
        evidence = _object(value, "plan_evidence_ref_invalid")
        _validate_evidence_ref(evidence)
        evidence_ref = cast(str, evidence["evidence_ref"])
        version_ref = cast(str, evidence["asset_version_ref"])
        if evidence_ref in evidence_by_ref or version_ref in version_refs:
            raise PlanContractError("plan_evidence_catalog_invalid")
        evidence_by_ref[evidence_ref] = evidence
        version_refs.add(version_ref)
    return evidence_by_ref


def validate_plan_document(
    document: dict[str, object],
    *,
    question_ref: str,
    idea_set_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    accepted_idea_set: dict[str, object],
    evidence_by_ref: dict[str, dict[str, object]],
    evidence_reference_revision: int,
) -> str:
    """Validate a complete PlanDocument and return its immutable content hash."""

    _exact_keys(
        document,
        {
            "schema_ref",
            "kind",
            "question_ref",
            "idea_set_ref",
            "context_pack_ref",
            "answer_contract",
            "evidence_reuse_set",
            "coverage",
            "gap_set",
            "experiment_briefs",
            "idea_trace",
            "bundle_disposition",
            "source_bindings",
        },
        "plan_document_invalid",
    )
    if (
        document.get("schema_ref") != PLAN_DOCUMENT_SCHEMA_REF
        or document.get("kind") != "PlanDocument"
        or document.get("question_ref") != question_ref
        or document.get("idea_set_ref") != idea_set_ref
        or document.get("context_pack_ref") != context_pack_ref
    ):
        raise PlanContractError("plan_document_source_invalid")
    _sha256(context_pack_hash, "plan_document_source_invalid")
    if not isinstance(evidence_reference_revision, int) or isinstance(
        evidence_reference_revision, bool
    ):
        raise PlanContractError("plan_document_source_invalid")

    candidate_refs = _accepted_candidate_refs(
        accepted_idea_set,
        question_ref=question_ref,
    )
    contract = _object(document.get("answer_contract"), "answer_contract_invalid")
    obligation_roles = _validate_answer_contract(
        contract,
        question_ref=question_ref,
        idea_set_ref=idea_set_ref,
        candidate_refs=candidate_refs,
    )
    obligation_keys = tuple(obligation_roles)

    coverage_value = document.get("coverage")
    if not isinstance(coverage_value, list) or len(coverage_value) != len(
        obligation_keys
    ):
        raise PlanContractError("plan_coverage_incomplete")
    coverage_by_key: dict[str, dict[str, object]] = {}
    flattened_uses: list[dict[str, object]] = []
    gap_keys: set[str] = set()
    for value in coverage_value:
        coverage = _object(value, "plan_coverage_invalid")
        _exact_keys(
            coverage,
            {
                "obligation_key",
                "disposition",
                "evidence_uses",
                "insufficiency",
            },
            "plan_coverage_invalid",
        )
        key = coverage.get("obligation_key")
        disposition = coverage.get("disposition")
        evidence_uses = coverage.get("evidence_uses")
        if (
            not _text(key)
            or key not in obligation_roles
            or key in coverage_by_key
            or disposition not in _COVERAGE_DISPOSITIONS
            or not isinstance(evidence_uses, list)
        ):
            raise PlanContractError("plan_coverage_invalid")
        parsed_uses = [
            _validate_evidence_use(
                use,
                obligation_key=cast(str, key),
                evidence_by_ref=evidence_by_ref,
                candidate_refs=candidate_refs,
            )
            for use in evidence_uses
        ]
        if disposition == "covered":
            if not parsed_uses or coverage.get("insufficiency") is not None:
                raise PlanContractError("plan_coverage_invalid")
        else:
            _require_text(coverage.get("insufficiency"), "plan_coverage_invalid")
            gap_keys.add(cast(str, key))
        coverage_by_key[cast(str, key)] = coverage
        flattened_uses.extend(parsed_uses)
    if set(coverage_by_key) != set(obligation_keys):
        raise PlanContractError("plan_coverage_incomplete")

    evidence_reuse_set = document.get("evidence_reuse_set")
    if not isinstance(evidence_reuse_set, list) or canonical_json(
        evidence_reuse_set
    ) != canonical_json(flattened_uses):
        raise PlanContractError("plan_evidence_reuse_set_invalid")

    gap_set = document.get("gap_set")
    if (
        not isinstance(gap_set, list)
        or not all(_text(key) for key in gap_set)
        or len(gap_set) != len(set(cast(list[str], gap_set)))
        or set(cast(list[str], gap_set)) != gap_keys
    ):
        raise PlanContractError("plan_gap_set_invalid")

    briefs = document.get("experiment_briefs")
    if not isinstance(briefs, list) or len(briefs) > MAX_PLAN_EXPERIMENT_BRIEFS:
        raise PlanContractError("plan_experiment_brief_invalid")
    covered_by_brief: set[str] = set()
    experiment_keys: set[str] = set()
    for value in briefs:
        brief = _object(value, "plan_experiment_brief_invalid")
        brief_gaps, experiment_key = _validate_experiment_brief(
            brief,
            gap_keys=gap_keys,
            candidate_refs=candidate_refs,
        )
        if experiment_key in experiment_keys:
            raise PlanContractError("plan_experiment_brief_invalid")
        experiment_keys.add(experiment_key)
        covered_by_brief.update(brief_gaps)
    if covered_by_brief != gap_keys:
        raise PlanContractError("plan_gap_brief_closure_invalid")

    _validate_idea_trace(
        document.get("idea_trace"),
        candidate_refs=candidate_refs,
        obligation_roles=obligation_roles,
    )
    expected_disposition = (
        "experiments_required"
        if gap_keys
        else "no_new_experiment_required"
    )
    if document.get("bundle_disposition") != expected_disposition:
        raise PlanContractError("plan_bundle_disposition_invalid")

    source_bindings = _object(
        document.get("source_bindings"), "plan_document_source_invalid"
    )
    _exact_keys(
        source_bindings,
        {
            "question_ref",
            "idea_set_ref",
            "context_pack_ref",
            "context_pack_hash",
            "evidence_reference_revision",
        },
        "plan_document_source_invalid",
    )
    if source_bindings != {
        "question_ref": question_ref,
        "idea_set_ref": idea_set_ref,
        "context_pack_ref": context_pack_ref,
        "context_pack_hash": context_pack_hash,
        "evidence_reference_revision": evidence_reference_revision,
    }:
        raise PlanContractError("plan_document_source_invalid")
    return canonical_hash(document)


def validate_plan_review(
    review: dict[str, object],
    *,
    reviewed_draft_hash: str,
    final_plan_hash: str,
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
            "final_plan_hash",
            "independent",
            "advisory_only",
        },
        "plan_review_invalid",
    )
    if (
        review.get("schema_ref") != PLAN_REVIEW_SCHEMA_REF
        or review.get("reviewed_draft_hash") != reviewed_draft_hash
        or review.get("final_plan_hash") != final_plan_hash
        or review.get("advisory_only") is not True
    ):
        raise PlanContractError("plan_review_invalid")
    review_mode = review.get("review_mode")
    reviewer_agent_ref = review.get("reviewer_agent_ref")
    if review_mode == "advisory_unobserved":
        if reviewer_agent_ref is not None or review.get("independent") is not False:
            raise PlanContractError("plan_review_invalid")
    elif review_mode == "harness_child_agent":
        # Immutable pre-ADR-0003 content remains readable. Current Skill and
        # Agent Runtime write gates reject this historical provenance shape.
        if not _text(reviewer_agent_ref) or review.get("independent") is not True:
            raise PlanContractError("plan_review_invalid")
    else:
        raise PlanContractError("plan_review_invalid")
    _sha256(reviewed_draft_hash, "plan_review_invalid")
    _sha256(final_plan_hash, "plan_review_invalid")
    findings = review.get("findings")
    dispositions = review.get("dispositions")
    if not isinstance(findings, list) or not isinstance(dispositions, list):
        raise PlanContractError("plan_review_invalid")
    finding_ids: set[str] = set()
    for value in findings:
        finding = _object(value, "plan_review_invalid")
        _exact_keys(
            finding,
            {"finding_id", "category", "message"},
            "plan_review_invalid",
        )
        if (
            not _text(finding.get("finding_id"))
            or finding["finding_id"] in finding_ids
            or finding.get("category") not in REVIEW_CATEGORIES
        ):
            raise PlanContractError("plan_review_invalid")
        _require_text(finding.get("message"), "plan_review_invalid")
        finding_ids.add(cast(str, finding["finding_id"]))
    disposition_ids: set[str] = set()
    revised = False
    for value in dispositions:
        disposition = _object(value, "plan_review_invalid")
        _exact_keys(
            disposition,
            {"finding_id", "action", "rationale"},
            "plan_review_invalid",
        )
        finding_id = disposition.get("finding_id")
        if (
            finding_id not in finding_ids
            or finding_id in disposition_ids
            or disposition.get("action") not in DISPOSITION_ACTIONS
        ):
            raise PlanContractError("plan_review_invalid")
        _require_text(disposition.get("rationale"), "plan_review_invalid")
        disposition_ids.add(cast(str, finding_id))
        revised = revised or disposition.get("action") == "revised"
    if disposition_ids != finding_ids:
        raise PlanContractError("plan_review_invalid")
    changed = reviewed_draft_hash != final_plan_hash
    if changed != revised:
        raise PlanContractError(
            "plan_review_revision_not_material"
            if revised
            else "plan_changed_without_review_revision"
        )
    return canonical_hash(review)


def material_plan_hash(document: dict[str, object]) -> str:
    """Hash Plan meaning while ignoring invocation-only source identities."""

    return canonical_hash(_material_value(document))


def _validate_idea_set_binding(
    binding: dict[str, object], *, question_ref: str
) -> None:
    _exact_keys(
        binding,
        {
            "outcome_ref",
            "outcome_kind",
            "content_ref",
            "payload_hash",
            "outcome_hash",
            "content_receipt",
            "outcome_receipt",
            "stage_commit_ref",
            "stage_commit_receipt",
            "idea_set",
        },
        "plan_idea_set_binding_invalid",
    )
    for field in ("outcome_ref", "content_ref", "stage_commit_ref"):
        _require_text(binding.get(field), "plan_idea_set_binding_invalid")
    for field in ("payload_hash", "outcome_hash"):
        _sha256(binding.get(field), "plan_idea_set_binding_invalid")
    if binding.get("outcome_kind") != "idea_set":
        raise PlanContractError("plan_idea_set_binding_invalid")
    idea_set = _object(binding.get("idea_set"), "plan_idea_set_binding_invalid")
    accepted_refs: set[str] = set()
    candidates = idea_set.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            boundary = candidate.get("evidence_boundary")
            if isinstance(boundary, dict):
                refs = boundary.get("accepted_evidence_refs")
                if isinstance(refs, list):
                    accepted_refs.update(
                        ref for ref in refs if isinstance(ref, str) and ref
                    )
    try:
        validated_hash = validate_idea_outcome(
            idea_set,
            question_ref=question_ref,
            context_pack_ref=cast(str, idea_set.get("context_pack_ref")),
            accepted_evidence_refs=accepted_refs,
        )
    except (IdeaContractError, TypeError) as error:
        raise PlanContractError("plan_idea_set_binding_invalid") from error
    if validated_hash != binding.get("outcome_hash"):
        raise PlanContractError("plan_idea_set_binding_invalid")
    _validate_receipt(
        binding.get("content_receipt"),
        issuer="research_memory",
        kind="idea_outcome_content_acceptance",
        subject_ref=cast(str, binding["content_ref"]),
        code="plan_idea_set_binding_invalid",
    )
    _validate_receipt(
        binding.get("outcome_receipt"),
        issuer="research_graph",
        kind="idea_outcome_accepted",
        subject_ref=cast(str, binding["outcome_ref"]),
        code="plan_idea_set_binding_invalid",
    )
    _validate_receipt(
        binding.get("stage_commit_receipt"),
        issuer="advancement_engine",
        kind="stage_commit",
        subject_ref=cast(str, binding["stage_commit_ref"]),
        code="plan_idea_set_binding_invalid",
    )


def _validate_evidence_ref(evidence: dict[str, object]) -> None:
    _exact_keys(
        evidence,
        {
            "schema_ref",
            "evidence_ref",
            "asset_version_ref",
            "asset_ref",
            "content_hash",
            "manifest_hash",
            "target_commit_root_ref",
            "provenance_closure_refs",
            "capabilities",
            "eligibility_token_ref",
            "integrity_receipt_ref",
            "availability_receipt_ref",
            "currentness_receipt_ref",
            "asset_receipt",
            "role_ref",
            "role_receipt",
        },
        "plan_evidence_ref_invalid",
    )
    if evidence.get("schema_ref") != EVIDENCE_REF_SCHEMA_REF:
        raise PlanContractError("plan_evidence_ref_invalid")
    for field in (
        "evidence_ref",
        "asset_version_ref",
        "asset_ref",
        "target_commit_root_ref",
        "eligibility_token_ref",
        "integrity_receipt_ref",
        "availability_receipt_ref",
        "currentness_receipt_ref",
        "role_ref",
    ):
        _require_text(evidence.get(field), "plan_evidence_ref_invalid")
    for field in ("content_hash", "manifest_hash"):
        _sha256(evidence.get(field), "plan_evidence_ref_invalid")
    for field in ("provenance_closure_refs", "capabilities"):
        values = evidence.get(field)
        if (
            not isinstance(values, list)
            or not values
            or not all(_text(value) for value in values)
            or len(values) != len(set(cast(list[str], values)))
        ):
            raise PlanContractError("plan_evidence_ref_invalid")
    asset_receipt = _validate_receipt(
        evidence.get("asset_receipt"),
        issuer="research_memory",
        kind=None,
        subject_ref=cast(str, evidence["asset_version_ref"]),
        code="plan_evidence_ref_invalid",
    )
    role_receipt = _validate_receipt(
        evidence.get("role_receipt"),
        issuer="research_graph",
        kind="asset_role_acceptance",
        subject_ref=cast(str, evidence["role_ref"]),
        code="plan_evidence_ref_invalid",
    )
    if (
        evidence["integrity_receipt_ref"] != asset_receipt["receipt_ref"]
        or evidence["availability_receipt_ref"] != asset_receipt["receipt_ref"]
        or evidence["eligibility_token_ref"] != role_receipt["receipt_ref"]
        or evidence["currentness_receipt_ref"] != role_receipt["receipt_ref"]
    ):
        raise PlanContractError("plan_evidence_ref_invalid")


def _accepted_candidate_refs(
    idea_set: dict[str, object], *, question_ref: str
) -> tuple[str, ...]:
    if idea_set.get("kind") != "IdeaSet" or idea_set.get("question_ref") != question_ref:
        raise PlanContractError("plan_accepted_idea_set_invalid")
    candidates = idea_set.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise PlanContractError("plan_accepted_idea_set_invalid")
    refs: list[str] = []
    for value in candidates:
        candidate = _object(value, "plan_accepted_idea_set_invalid")
        ref = candidate.get("candidate_key")
        if not _text(ref) or ref in refs:
            raise PlanContractError("plan_accepted_idea_set_invalid")
        refs.append(cast(str, ref))
    return tuple(refs)


def _validate_answer_contract(
    contract: dict[str, object],
    *,
    question_ref: str,
    idea_set_ref: str,
    candidate_refs: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    _exact_keys(
        contract,
        {
            "source_question_ref",
            "source_idea_set_ref",
            "obligations",
            "answer_contract_hash",
        },
        "answer_contract_invalid",
    )
    if (
        contract.get("source_question_ref") != question_ref
        or contract.get("source_idea_set_ref") != idea_set_ref
    ):
        raise PlanContractError("answer_contract_source_invalid")
    contract_without_hash = {
        key: value for key, value in contract.items() if key != "answer_contract_hash"
    }
    if contract.get("answer_contract_hash") != canonical_hash(contract_without_hash):
        raise PlanContractError("answer_contract_hash_invalid")
    obligations = contract.get("obligations")
    if (
        not isinstance(obligations, list)
        or not 1 <= len(obligations) <= MAX_PLAN_OBLIGATIONS
    ):
        raise PlanContractError("answer_contract_invalid")
    result: dict[str, dict[str, str]] = {}
    for value in obligations:
        obligation = _object(value, "answer_contract_invalid")
        _exact_keys(
            obligation,
            {
                "obligation_key",
                "statement",
                "minimum_support",
                "question_trace",
                "idea_relevance",
            },
            "answer_contract_invalid",
        )
        key = obligation.get("obligation_key")
        _require_text(key, "answer_contract_invalid")
        _require_text(obligation.get("statement"), "answer_contract_invalid")
        _require_text(obligation.get("minimum_support"), "answer_contract_invalid")
        if key in result:
            raise PlanContractError("answer_contract_invalid")
        trace = obligation.get("question_trace")
        if (
            not isinstance(trace, list)
            or len(trace) != len(set(cast(list[str], trace)))
            or not set(cast(list[str], trace)) <= _QUESTION_TRACE_FIELDS
            or "answer_shape" not in trace
            or len(trace) < 2
        ):
            raise PlanContractError("answer_contract_trace_invalid")
        relevance = obligation.get("idea_relevance")
        if not isinstance(relevance, list) or len(relevance) != len(candidate_refs):
            raise PlanContractError("plan_idea_matrix_incomplete")
        roles: dict[str, str] = {}
        for item_value in relevance:
            item = _object(item_value, "plan_idea_matrix_invalid")
            _exact_keys(
                item,
                {"idea_ref", "role", "rationale"},
                "plan_idea_matrix_invalid",
            )
            idea_ref = item.get("idea_ref")
            role = item.get("role")
            if idea_ref not in candidate_refs or idea_ref in roles or role not in _IDEA_ROLES:
                raise PlanContractError("plan_idea_matrix_invalid")
            _require_text(item.get("rationale"), "plan_idea_matrix_invalid")
            roles[cast(str, idea_ref)] = cast(str, role)
        if set(roles) != set(candidate_refs):
            raise PlanContractError("plan_idea_matrix_incomplete")
        result[cast(str, key)] = roles
    return result


def _validate_evidence_use(
    value: object,
    *,
    obligation_key: str,
    evidence_by_ref: dict[str, dict[str, object]],
    candidate_refs: tuple[str, ...],
) -> dict[str, object]:
    use = _object(value, "plan_evidence_use_invalid")
    _exact_keys(
        use,
        {
            "obligation_key",
            "evidence_ref",
            "supported_claim",
            "support_boundary",
            "contributing_idea_refs",
        },
        "plan_evidence_use_invalid",
    )
    if use.get("obligation_key") != obligation_key:
        raise PlanContractError("plan_evidence_use_invalid")
    evidence_ref = use.get("evidence_ref")
    if evidence_ref not in evidence_by_ref:
        raise PlanContractError("plan_evidence_ref_unbound")
    _require_text(use.get("supported_claim"), "plan_evidence_use_invalid")
    _require_text(use.get("support_boundary"), "plan_evidence_use_invalid")
    _validate_idea_ref_list(
        use.get("contributing_idea_refs"),
        candidate_refs=candidate_refs,
        code="plan_evidence_use_invalid",
    )
    return use


def _validate_experiment_brief(
    brief: dict[str, object],
    *,
    gap_keys: set[str],
    candidate_refs: tuple[str, ...],
) -> tuple[set[str], str]:
    _exact_keys(
        brief,
        {
            "experiment_key",
            "gap_obligation_keys",
            "goal",
            "characteristics",
            "boundary_constraints",
            "semantic_delta",
            "contributing_idea_refs",
        },
        "plan_experiment_brief_invalid",
    )
    experiment_key = brief.get("experiment_key")
    _require_text(experiment_key, "plan_experiment_brief_invalid")
    for field in ("goal", "characteristics", "boundary_constraints", "semantic_delta"):
        _require_text(brief.get(field), "plan_experiment_brief_invalid")
    values = brief.get("gap_obligation_keys")
    if (
        not isinstance(values, list)
        or not values
        or not all(_text(value) for value in values)
        or len(values) != len(set(cast(list[str], values)))
        or not set(cast(list[str], values)) <= gap_keys
    ):
        raise PlanContractError("plan_gap_brief_closure_invalid")
    _validate_idea_ref_list(
        brief.get("contributing_idea_refs"),
        candidate_refs=candidate_refs,
        code="plan_experiment_brief_invalid",
    )
    return set(cast(list[str], values)), cast(str, experiment_key)


def _validate_idea_trace(
    value: object,
    *,
    candidate_refs: tuple[str, ...],
    obligation_roles: dict[str, dict[str, str]],
) -> None:
    if not isinstance(value, list) or len(value) != len(candidate_refs):
        raise PlanContractError("plan_idea_trace_invalid")
    seen: set[str] = set()
    for item_value in value:
        item = _object(item_value, "plan_idea_trace_invalid")
        _exact_keys(
            item,
            {"idea_ref", "obligation_roles"},
            "plan_idea_trace_invalid",
        )
        idea_ref = item.get("idea_ref")
        roles = item.get("obligation_roles")
        if idea_ref not in candidate_refs or idea_ref in seen or not isinstance(roles, list):
            raise PlanContractError("plan_idea_trace_invalid")
        expected = {
            key: by_idea[cast(str, idea_ref)] for key, by_idea in obligation_roles.items()
        }
        actual: dict[str, str] = {}
        for role_value in roles:
            role = _object(role_value, "plan_idea_trace_invalid")
            _exact_keys(
                role,
                {"obligation_key", "role"},
                "plan_idea_trace_invalid",
            )
            key = role.get("obligation_key")
            role_name = role.get("role")
            if key not in expected or key in actual or role_name not in _IDEA_ROLES:
                raise PlanContractError("plan_idea_trace_invalid")
            actual[cast(str, key)] = cast(str, role_name)
        if actual != expected:
            raise PlanContractError("plan_idea_trace_invalid")
        seen.add(cast(str, idea_ref))
    if seen != set(candidate_refs):
        raise PlanContractError("plan_idea_trace_invalid")


def _validate_idea_ref_list(
    value: object, *, candidate_refs: tuple[str, ...], code: str
) -> None:
    if (
        not isinstance(value, list)
        or not all(_text(item) for item in value)
        or len(value) != len(set(cast(list[str], value)))
        or not set(cast(list[str], value)) <= set(candidate_refs)
    ):
        raise PlanContractError(code)


def _validate_receipt(
    value: object,
    *,
    issuer: str,
    kind: str | None,
    subject_ref: str,
    code: str,
) -> dict[str, object]:
    receipt = _object(value, code)
    _exact_keys(
        receipt,
        {
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        },
        code,
    )
    if (
        receipt.get("status") != "accepted"
        or receipt.get("issuer") != issuer
        or (kind is not None and receipt.get("kind") != kind)
        or receipt.get("subject_ref") != subject_ref
        or not _text(receipt.get("kind"))
        or not _text(receipt.get("receipt_ref"))
    ):
        raise PlanContractError(code)
    _sha256(receipt.get("payload_hash"), code)
    return receipt


def _material_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _material_value(item)
            for key, item in value.items()
            if key
            not in {
                "context_pack_ref",
                "context_pack_hash",
                "evidence_reference_revision",
            }
        }
    if isinstance(value, list):
        return [_material_value(item) for item in value]
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlanContractError(code)
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise PlanContractError(code)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 8_192


def _require_text(value: object, code: str) -> None:
    if not _text(value):
        raise PlanContractError(code)


def _sha256(value: object, code: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PlanContractError(code)
