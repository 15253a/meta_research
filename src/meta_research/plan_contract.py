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
EVIDENCE_SOURCE_REF_SCHEMA = "meta-research/evidence-source-ref/v1"
GENERIC_EVIDENCE_KINDS = frozenset({"HumanInput", "ScientificOutcome", "AssetVersion", "LiteratureSnapshot"})
MAX_PLAN_OBLIGATIONS = 64
MAX_PLAN_EVIDENCE_REFS = 256
MAX_PLAN_EXPERIMENT_BRIEFS = 64
MAX_SELECTED_PLAN_EVIDENCE_REFS = 32

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
        } | ({"evidence_catalog_page"} if "evidence_catalog_page" in context_pack else set()),
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

    page = context_pack.get("evidence_catalog_page")
    if page is not None:
        if not isinstance(page, dict):
            raise PlanContractError("plan_evidence_page_invalid")
        _exact_keys(page, {"total_candidates", "candidate_unit", "candidate_count", "scanned_count", "offset", "limit", "next_offset", "question_ref", "selection", "snapshot_hash", "projections"}, "plan_evidence_page_invalid")
        if (any(type(page.get(key)) is not int or page[key] < 0 for key in
                ("total_candidates", "candidate_count", "scanned_count", "offset", "limit"))
                or not 1 <= page["limit"] <= MAX_PLAN_EVIDENCE_REFS
                or page["candidate_unit"] != "evidence_asset_role"
                or page["candidate_count"] != len(catalog)
                or not len(catalog) <= page["scanned_count"] <= page["limit"]
                or page["scanned_count"] > max(0, page["total_candidates"] - page["offset"])
                or page["question_ref"] != question_ref
                or page["selection"] != "current_question_then_recent_active_or_shared"
                or page["snapshot_hash"] != canonical_hash(catalog)
                or not isinstance(page["projections"], list)):
            raise PlanContractError("plan_evidence_page_invalid")
        next_offset = page["offset"] + page["scanned_count"]
        expected_next = next_offset if next_offset < page["total_candidates"] else None
        if page["next_offset"] != expected_next:
            raise PlanContractError("plan_evidence_page_invalid")

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
    if page is not None and page["projections"]:
        projections = page["projections"]
        if len(projections) != len(catalog):
            raise PlanContractError("plan_evidence_page_invalid")
        for evidence, projection in zip(catalog, projections, strict=True):
            if not isinstance(projection, dict):
                raise PlanContractError("plan_evidence_page_invalid")
            if (projection.get("evidence_ref") != evidence["evidence_ref"]
                    or projection.get("target_commit_ref") != evidence["target_commit_root_ref"]
                    or projection.get("asset_version_ref") != evidence["asset_version_ref"]
                    or projection.get("content_hash") != evidence["content_hash"]
                    or not _text(projection.get("question_ref"))
                    or not _text(projection.get("cycle_ref"))
                    or not _valid_evidence_discovery_projection(projection)
                    or projection.get("exact_content_reader") != "research_memory.plan_evidence.read"):
                raise PlanContractError("plan_evidence_page_invalid")
    return evidence_by_ref



def _valid_evidence_discovery_projection(projection: dict[str, object]) -> bool:
    # Legacy complete projections retain their exact content check. New discovery
    # summaries are explicitly partial and never establish evidence authority.
    if "target_spec" in projection:
        return (isinstance(projection.get("target_spec"), dict)
                and canonical_hash(projection["target_spec"]) == projection.get("target_spec_hash")
                and isinstance(projection.get("metric_result"), dict))
    summary = projection.get("research_summary")
    digest = projection.get("target_spec_hash")
    return (isinstance(digest, str) and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest)
            and isinstance(summary, dict)
            and summary.get("schema_ref") == "meta-research/evidence-discovery-summary/v1"
            and summary.get("summary_only") is True)


def selected_plan_evidence_catalog(
    document: dict[str, object], frozen_catalog: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Join a discovery page with only explicitly selected exact later bindings.

    This validates structure, not authority. RG validates every selected receipt,
    source, exact content and Quest eligibility before accepting the FormalPlan.
    Keeping these selected bindings in the Plan preserves historical readback
    without expanding or mutating the original Stage request.
    """
    bindings = document.get("source_bindings")
    additions = bindings.get("selected_evidence_catalog", []) if isinstance(bindings, dict) else []
    if not isinstance(additions, list) or len(additions) > MAX_SELECTED_PLAN_EVIDENCE_REFS:
        raise PlanContractError("plan_selected_evidence_invalid")
    uses = document.get("evidence_reuse_set", [])
    if not isinstance(uses, list):
        raise PlanContractError("plan_evidence_reuse_set_invalid")
    used = {item.get("evidence_ref") for item in uses if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)}
    result = {str(item["evidence_ref"]): item for item in frozen_catalog}
    seen: set[str] = set()
    versions = {str(item.get("asset_version_ref", item.get("source_ref"))): str(item["evidence_ref"]) for item in frozen_catalog}
    for raw in additions:
        entry = _object(raw, "plan_selected_evidence_invalid")
        _validate_evidence_ref(entry)
        ref = cast(str, entry["evidence_ref"])
        version = cast(str, entry.get("asset_version_ref", entry.get("source_ref")))
        if ref not in used or ref in seen or (ref in result and result[ref] != entry):
            raise PlanContractError("plan_selected_evidence_invalid")
        if version in versions and versions[version] != ref:
            raise PlanContractError("plan_selected_evidence_invalid")
        seen.add(ref)
        versions[version] = ref
        result[ref] = entry
    if len(result) > MAX_PLAN_EVIDENCE_REFS:
        raise PlanContractError("plan_selected_evidence_invalid")
    return list(result.values())


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
        } | ({"notes"} if "notes" in document else set()),
        "plan_document_invalid",
    )
    if "notes" in document and not isinstance(document["notes"], str):
        raise PlanContractError("plan_notes_invalid")
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

    evidence_by_ref = {str(item["evidence_ref"]): item for item in selected_plan_evidence_catalog(document, list(evidence_by_ref.values()))}

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
        } | ({"selected_evidence_catalog"} if "selected_evidence_catalog" in source_bindings else set()),
        "plan_document_source_invalid",
    )
    if {key: value for key, value in source_bindings.items() if key != "selected_evidence_catalog"} != {
        "question_ref": question_ref,
        "idea_set_ref": idea_set_ref,
        "context_pack_ref": context_pack_ref,
        "context_pack_hash": context_pack_hash,
        "evidence_reference_revision": evidence_reference_revision,
    }:
        raise PlanContractError("plan_document_source_invalid")
    return canonical_hash(document)


def validate_plan_review(review: dict[str, object], *, final_plan_hash: str, reviewed_draft_hash: str) -> str:
    """Bind draft and final bytes; review feedback is part of native execution."""
    if (not isinstance(review, dict) or set(review) != {"schema_ref", "reviewed_draft_hash", "final_plan_hash"}
        or review.get("schema_ref") != PLAN_REVIEW_SCHEMA_REF
        or review.get("final_plan_hash") != final_plan_hash
        or not isinstance(review.get("reviewed_draft_hash"), str)
        or len(review["reviewed_draft_hash"]) != 64
        or (reviewed_draft_hash is not None and review["reviewed_draft_hash"] != reviewed_draft_hash)):
        raise PlanContractError("plan_review_binding_invalid")
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
    if evidence.get("schema_ref") == EVIDENCE_SOURCE_REF_SCHEMA:
        _exact_keys(evidence, {"schema_ref", "evidence_ref", "source_kind", "source_ref"},
                    "plan_evidence_ref_invalid")
        if (evidence.get("source_kind") not in GENERIC_EVIDENCE_KINDS
                or not _text(evidence.get("source_ref"))
                or evidence.get("evidence_ref") != evidence.get("source_ref")):
            raise PlanContractError("plan_evidence_ref_invalid")
        return
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
            or not trace
        ):
            raise PlanContractError("answer_contract_trace_invalid")
        relevance = obligation.get("idea_relevance")
        if not isinstance(relevance, list) or len(relevance) > len(candidate_refs):
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
    expected_refs = {ref for roles in obligation_roles.values() for ref in roles}
    if not isinstance(value, list) or len(value) != len(expected_refs):
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
            key: by_idea[cast(str, idea_ref)] for key, by_idea in obligation_roles.items() if idea_ref in by_idea
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
    if seen != expected_refs:
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
