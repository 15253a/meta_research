from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import cast


REVIEW_CATEGORIES = {
    "question_alignment",
    "material_duplicate",
    "evidence_boundary",
    "falsifiability",
    "plan_usability",
}
DISPOSITION_ACTIONS = {"revised", "not_adopted"}
IDEA_OUTCOME_SCHEMA_REF = "meta-research/idea-outcome/v1"
IDEA_REVIEW_SCHEMA_V1_REF = "meta-research/idea-advisory-review/v1"
IDEA_REVIEW_SCHEMA_REF = "meta-research/idea-advisory-review/v2"
IDEA_CONTEXT_PACK_SCHEMA_REF = "meta-research/idea-context-pack/v1"
IDEA_CONTEXT_PACK_SCHEMA_V2_REF = "meta-research/idea-context-pack/v2"
IDEA_CONTEXT_PACK_SCHEMA_V3_REF = "meta-research/idea-context-pack/v3"
IDEA_CONTEXT_PACK_SCHEMA_V4_REF = "meta-research/idea-context-pack/v4"
MAX_IDEA_CONTEXT_EVIDENCE_REFS = 100
MAX_IDEA_CONTEXT_GUIDANCE_BINDINGS = 100
_IDEA_CONTEXT_PACK_V1_FIELDS = {
    "schema_ref",
    "cycle_ref",
    "accepted_question_binding",
    "accepted_evidence_refs",
    "literature_binding",
    "prior_accepted_bindings",
    "active_guidance_bindings",
}
_IDEA_CONTEXT_PACK_V2_FIELDS = _IDEA_CONTEXT_PACK_V1_FIELDS | {
    "evidence_reference_revision"
}
_IDEA_CONTEXT_PACK_V3_FIELDS = _IDEA_CONTEXT_PACK_V2_FIELDS
_PRIOR_REASONING_BINDING_FIELDS = {
    "stage",
    "commit_ref",
    "cycle_ref",
    "epoch",
    "disposition",
    "receipt",
    "request_ref",
    "run_ref",
    "outcome_ref",
    "outcome_kind",
    "run_completion_receipt",
    "outcome_receipt",
    "closure",
}
_MATERIAL_IDENTITY_FIELDS = {
    "candidate_key",
    "question_ref",
    "context_pack_ref",
    # Idea recommendation is explicitly advisory/non-binding and cannot make a
    # rejected research direction into a materially new successor.
    "recommendation",
}


class IdeaContractError(ValueError):
    """An Idea Outcome or advisory review violates the pure domain contract."""


def validate_idea_content(
    outcome: dict[str, object],
    review: dict[str, object],
    *,
    reviewed_draft: dict[str, object] | None = None,
    question_ref: str | None = None,
    context_pack_ref: str | None = None,
    accepted_evidence_refs: set[str] | None = None,
) -> tuple[str, str]:
    """Validate immutable Owner content and return outcome/review hashes."""

    outcome_hash = validate_idea_outcome(
        outcome,
        question_ref=question_ref,
        context_pack_ref=context_pack_ref,
        accepted_evidence_refs=accepted_evidence_refs,
    )
    if reviewed_draft is None:
        # Compatibility is safe only when no separate draft is claimed. A
        # revised result must carry the actual immutable bytes through AR/RM.
        if review.get("reviewed_draft_hash") != outcome_hash:
            raise IdeaContractError("idea_reviewed_draft_missing")
        reviewed_draft = outcome
    reviewed_draft_hash = validate_idea_outcome(
        reviewed_draft,
        question_ref=question_ref,
        context_pack_ref=context_pack_ref,
        accepted_evidence_refs=accepted_evidence_refs,
    )
    review_hash = validate_advisory_review(
        review,
        outcome_hash=outcome_hash,
        reviewed_draft_hash=reviewed_draft_hash,
    )
    return outcome_hash, review_hash


def idea_used_evidence_refs(outcome: dict[str, object]) -> set[str]:
    """Exact immutable asset-version identities actually cited by this outcome."""
    refs: set[str] = set()
    boundaries = ([candidate.get("evidence_boundary") for candidate in outcome.get("candidates", [])
                   if isinstance(candidate, dict)] if outcome.get("kind") == "IdeaSet"
                  else [outcome.get("evidence_boundary")])
    collections = [item.get("accepted_evidence_refs", []) for item in boundaries if isinstance(item, dict)]
    collections += [item.get("evidence_refs", []) for item in outcome.get("candidate_families_considered", []) if isinstance(item, dict)]
    for values in collections:
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise IdeaContractError("accepted_evidence_ref_unbound")
        refs.update(values)
    if len(refs) > MAX_IDEA_CONTEXT_EVIDENCE_REFS:
        raise IdeaContractError("idea_selected_evidence_too_large")
    return refs


def idea_validation_evidence_refs(context_pack: dict[str, object]) -> set[str] | None:
    # v4 freezes a discovery page, not the full eligible evidence universe.
    # RG authenticates actual cited identities before domain acceptance.
    if context_pack.get("schema_ref") == IDEA_CONTEXT_PACK_SCHEMA_V4_REF:
        return None
    return accepted_evidence_refs(context_pack)


def validate_idea_outcome(
    outcome: dict[str, object],
    *,
    question_ref: str | None,
    context_pack_ref: str | None,
    accepted_evidence_refs: set[str] | None,
) -> str:
    if not isinstance(outcome, dict):
        raise IdeaContractError("idea_outcome_not_object")
    if question_ref is not None and outcome.get("question_ref") != question_ref:
        raise IdeaContractError("idea_outcome_question_mismatch")
    if (
        context_pack_ref is not None
        and outcome.get("context_pack_ref") != context_pack_ref
    ):
        raise IdeaContractError("idea_outcome_context_mismatch")
    _require_text(outcome.get("question_ref"), "question_ref")
    _require_text(outcome.get("context_pack_ref"), "context_pack_ref")
    if "notes" in outcome and not isinstance(outcome["notes"], str):
        raise IdeaContractError("idea_notes_invalid")
    kind = outcome.get("kind")
    if kind == "IdeaSet":
        _validate_idea_set(outcome, accepted_evidence_refs)
    elif kind == "NoViableCandidate":
        _validate_no_viable(outcome, accepted_evidence_refs)
    else:
        raise IdeaContractError("idea_outcome_kind_invalid")
    idea_used_evidence_refs(outcome)
    return _canonical_hash(outcome)


def validate_advisory_review(review: dict[str, object], *, outcome_hash: str, reviewed_draft_hash: str | None = None) -> str:
    """Bind draft and final bytes; review feedback is part of native execution."""
    if (not isinstance(review, dict) or set(review) != {"schema_ref", "reviewed_draft_hash", "final_outcome_hash"}
        or review.get("schema_ref") != IDEA_REVIEW_SCHEMA_REF
        or review.get("final_outcome_hash") != outcome_hash
        or not isinstance(review.get("reviewed_draft_hash"), str)
        or len(review["reviewed_draft_hash"]) != 64
        or (reviewed_draft_hash is not None and review["reviewed_draft_hash"] != reviewed_draft_hash)):
        raise IdeaContractError("idea_review_binding_invalid")
    return _canonical_hash(review)


def validate_idea_context_pack(
    context_pack: dict[str, object],
    *,
    cycle_ref: str,
    accepted_question_binding: dict[str, object],
) -> set[str]:
    """Validate the pure shape of the current Idea invocation closure.

    The RG Owner verifies non-empty Evidence refs at command boundaries.  Empty
    packs remain valid historical artifacts after Evidence support is enabled.
    """

    if not isinstance(context_pack, dict):
        raise IdeaContractError("idea_context_pack_invalid")
    schema_ref = context_pack.get("schema_ref")
    if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_V4_REF:
        return _validate_context_pack_v4(context_pack, cycle_ref=cycle_ref,
            accepted_question_binding=accepted_question_binding)
    expected_fields = (
        _IDEA_CONTEXT_PACK_V3_FIELDS
        if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_V3_REF
        else (
            _IDEA_CONTEXT_PACK_V2_FIELDS
            if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_V2_REF
            else _IDEA_CONTEXT_PACK_V1_FIELDS
        )
    )
    if set(context_pack) != expected_fields:
        raise IdeaContractError("idea_context_pack_invalid")
    if (
        schema_ref
        not in {
            IDEA_CONTEXT_PACK_SCHEMA_REF,
            IDEA_CONTEXT_PACK_SCHEMA_V2_REF,
            IDEA_CONTEXT_PACK_SCHEMA_V3_REF,
        }
        or context_pack["cycle_ref"] != cycle_ref
        or context_pack["accepted_question_binding"] != accepted_question_binding
    ):
        raise IdeaContractError("idea_context_pack_invalid")
    literature = context_pack["literature_binding"]
    if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_REF:
        if literature is not None or context_pack["prior_accepted_bindings"] != []:
            raise IdeaContractError("idea_context_pack_invalid")
    elif schema_ref == IDEA_CONTEXT_PACK_SCHEMA_V2_REF:
        if context_pack["prior_accepted_bindings"] != []:
            raise IdeaContractError("idea_context_pack_invalid")
        if literature is not None:
            _validate_literature_binding(literature)
    else:
        _validate_question_literature_revision_binding(
            literature,
            question_ref=cast(dict[str, object], accepted_question_binding).get(
                "question_ref"
            ),
        )
        _validate_prior_reasoning_bindings(
            context_pack["prior_accepted_bindings"]
        )
    if schema_ref in {
        IDEA_CONTEXT_PACK_SCHEMA_V2_REF,
        IDEA_CONTEXT_PACK_SCHEMA_V3_REF,
    }:
        revision = context_pack["evidence_reference_revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise IdeaContractError("idea_context_pack_invalid")
    refs = context_pack["accepted_evidence_refs"]
    if schema_ref == IDEA_CONTEXT_PACK_SCHEMA_REF and refs != []:
        # V1 never carried Evidence authority. Keep that immutable schema
        # violation on the original public error contract.
        raise IdeaContractError("idea_context_pack_invalid")
    if (
        not isinstance(refs, list)
        or len(refs) > MAX_IDEA_CONTEXT_EVIDENCE_REFS
        or not all(isinstance(item, str) and item for item in refs)
        or refs != sorted(set(refs))
    ):
        raise IdeaContractError("context_pack_evidence_bindings_invalid")
    guidance = context_pack["active_guidance_bindings"]
    if (
        not isinstance(guidance, list)
        or len(guidance) > MAX_IDEA_CONTEXT_GUIDANCE_BINDINGS
    ):
        raise IdeaContractError("idea_context_guidance_bindings_invalid")
    guidance_keys: list[tuple[str, str, int]] = []
    receipt_refs: list[str] = []
    expected_guidance_fields = {
        "schema_ref",
        "issuer",
        "constraint_ref",
        "scope_ref",
        "revision",
        "guidance",
        "guidance_hash",
        "receipt_ref",
        "receipt_hash",
    }
    for binding in guidance:
        if (
            not isinstance(binding, dict)
            or set(binding) != expected_guidance_fields
            or binding.get("schema_ref")
            != "meta-research/active-guidance-binding/v1"
            or binding.get("issuer") != "human_collaboration"
            or not isinstance(binding.get("revision"), int)
            or isinstance(binding.get("revision"), bool)
            or cast(int, binding.get("revision")) < 1
            or not isinstance(binding.get("guidance"), dict)
            or not binding.get("guidance")
            or not _is_hash(binding.get("guidance_hash"))
            or _canonical_hash(binding.get("guidance"))
            != binding.get("guidance_hash")
            or not _is_hash(binding.get("receipt_hash"))
        ):
            raise IdeaContractError("idea_context_guidance_bindings_invalid")
        _require_text(binding.get("constraint_ref"), "constraint_ref")
        _require_text(binding.get("scope_ref"), "scope_ref")
        _require_text(binding.get("receipt_ref"), "receipt_ref")
        constraint_ref = cast(str, binding["constraint_ref"])
        scope_ref = cast(str, binding["scope_ref"])
        receipt_ref = cast(str, binding["receipt_ref"])
        guidance_keys.append(
            (scope_ref, constraint_ref, cast(int, binding["revision"]))
        )
        receipt_refs.append(receipt_ref)
    if (
        guidance_keys != sorted(set(guidance_keys))
        or len(receipt_refs) != len(set(receipt_refs))
    ):
        raise IdeaContractError("idea_context_guidance_bindings_invalid")
    return set(cast(list[str], refs))


def literature_binding(
    context_pack: dict[str, object],
) -> dict[str, object] | None:
    value = context_pack.get("literature_binding")
    if value is None:
        return None
    if context_pack.get("schema_ref") == IDEA_CONTEXT_PACK_SCHEMA_V4_REF:
        _validate_literature_reference(value, context_pack["accepted_question_binding"].get("question_ref"))
        return value
    if context_pack.get("schema_ref") == IDEA_CONTEXT_PACK_SCHEMA_V3_REF:
        accepted = context_pack.get("accepted_question_binding")
        question_ref = (
            accepted.get("question_ref") if isinstance(accepted, dict) else None
        )
        _validate_question_literature_revision_binding(
            value, question_ref=question_ref
        )
    else:
        _validate_literature_binding(value)
    return cast(dict[str, object], value)


def _validate_question_literature_revision_binding(
    value: object, *, question_ref: object
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "revision_ref",
        "question_ref",
        "literature_snapshot_ref",
        "records",
        "rm_acceptance_receipt_ref",
        "rg_question_association_receipt_ref",
        "receipt",
    }:
        raise IdeaContractError("idea_context_pack_invalid")
    receipt = value.get("receipt")
    if (
        value.get("kind") != "QuestionLiteratureRevision"
        or value.get("question_ref") != question_ref
        or not isinstance(value.get("records"), list)
        or not all(
            isinstance(value.get(field), str) and value.get(field)
            for field in (
                "revision_ref",
                "question_ref",
                "literature_snapshot_ref",
                "rm_acceptance_receipt_ref",
                "rg_question_association_receipt_ref",
            )
        )
        or not isinstance(receipt, dict)
        or receipt.get("status") != "accepted"
        or receipt.get("issuer") != "research_memory"
        or receipt.get("kind") != "question_literature_revision_acceptance"
        or receipt.get("subject_ref") != value.get("revision_ref")
        or receipt.get("receipt_ref") != value.get("rm_acceptance_receipt_ref")
        or not _is_hash(receipt.get("payload_hash"))
    ):
        raise IdeaContractError("idea_context_pack_invalid")


def _validate_prior_reasoning_bindings(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise IdeaContractError("idea_context_pack_invalid")
    identities: list[tuple[str, int]] = []
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != (
            _PRIOR_REASONING_BINDING_FIELDS
        ):
            raise IdeaContractError("idea_context_pack_invalid")
        closure = binding.get("closure")
        if (
            binding.get("stage") != "reasoning"
            or binding.get("disposition") != "completed"
            or binding.get("outcome_kind") != "reasoning_outcome"
            or not isinstance(binding.get("epoch"), int)
            or isinstance(binding.get("epoch"), bool)
            or cast(int, binding["epoch"]) < 1
            or not all(
                isinstance(binding.get(field), str) and binding.get(field)
                for field in (
                    "commit_ref",
                    "cycle_ref",
                    "request_ref",
                    "run_ref",
                    "outcome_ref",
                )
            )
            or not isinstance(closure, dict)
            or closure.get("transition_kind")
            not in {"next_cycle_proposal", "candidate_completion"}
        ):
            raise IdeaContractError("idea_context_pack_invalid")
        _validate_public_receipt(
            binding.get("run_completion_receipt"),
            issuer="agent_runtime",
            subject_ref=binding.get("run_ref"),
        )
        _validate_public_receipt(
            binding.get("outcome_receipt"),
            issuer="research_graph",
            subject_ref=binding.get("outcome_ref"),
        )
        _validate_public_receipt(
            binding.get("receipt"),
            issuer="advancement_engine",
            subject_ref=binding.get("commit_ref"),
        )
        identities.append(
            (cast(str, binding["cycle_ref"]), cast(int, binding["epoch"]))
        )
    if identities != sorted(set(identities), key=lambda item: item[1]):
        raise IdeaContractError("idea_context_pack_invalid")


def _validate_public_receipt(
    value: object, *, issuer: str, subject_ref: object
) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        }
        or value.get("status") != "accepted"
        or value.get("issuer") != issuer
        or value.get("subject_ref") != subject_ref
        or not isinstance(value.get("kind"), str)
        or not value.get("kind")
        or not isinstance(value.get("receipt_ref"), str)
        or not value.get("receipt_ref")
        or not _is_hash(value.get("payload_hash"))
    ):
        raise IdeaContractError("idea_context_pack_invalid")


def _validate_literature_binding(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_ref",
        "snapshot_ref",
        "snapshot_hash",
        "initialization_id",
        "draft_revision",
        "draft_hash",
        "receipt",
    }:
        raise IdeaContractError("idea_context_pack_invalid")
    receipt = value.get("receipt")
    revision = value.get("draft_revision")
    if (
        value.get("schema_ref")
        != "meta-research/idea-literature-binding/v1"
        or not all(
            isinstance(value.get(field), str) and value.get(field)
            for field in ("snapshot_ref", "initialization_id")
        )
        or not isinstance(value.get("snapshot_hash"), str)
        or len(cast(str, value["snapshot_hash"])) != 64
        or not isinstance(value.get("draft_hash"), str)
        or len(cast(str, value["draft_hash"])) != 64
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(receipt, dict)
        or set(receipt)
        != {
            "status",
            "issuer",
            "kind",
            "receipt_ref",
            "subject_ref",
            "payload_hash",
        }
        or receipt.get("status") != "accepted"
        or receipt.get("issuer") != "research_memory"
        or receipt.get("kind") != "literature_snapshot_acceptance"
        or receipt.get("subject_ref") != value.get("snapshot_ref")
        or not isinstance(receipt.get("receipt_ref"), str)
        or not receipt.get("receipt_ref")
        or not isinstance(receipt.get("payload_hash"), str)
        or len(cast(str, receipt["payload_hash"])) != 64
    ):
        raise IdeaContractError("idea_context_pack_invalid")


def evidence_reference_revision(context_pack: dict[str, object]) -> int | None:
    value = context_pack.get("evidence_reference_revision")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IdeaContractError("idea_context_pack_invalid")
    return value


def material_text(value: str) -> str:
    """Return a comparison key that ignores Unicode spacing and punctuation."""

    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character)[0] not in {"P", "Z"}
        and unicodedata.category(character) != "Cf"
    )


def material_outcome_hash(outcome: dict[str, object]) -> str:
    """Hash research meaning while excluding identity-only cosmetic changes."""

    return _canonical_hash(_material_value(outcome))


def _material_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _material_value(item)
            for key, item in value.items()
            if key not in _MATERIAL_IDENTITY_FIELDS
            and not (key == "notes" and isinstance(item, str) and not item.strip())
        }
    if isinstance(value, list):
        # Outcome list order and duplicate copies are not research changes.
        normalized = (_material_value(item) for item in value)
        unique = {_canonical_json(item): item for item in normalized}
        return [unique[key] for key in sorted(unique)]
    if isinstance(value, str):
        return material_text(value)
    return value


def accepted_evidence_refs(context_pack: dict[str, object]) -> set[str]:
    value = context_pack.get("accepted_evidence_refs", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise IdeaContractError("context_pack_evidence_bindings_invalid")
    return set(cast(list[str], value))


def _validate_idea_set(
    outcome: dict[str, object], accepted_refs: set[str] | None
) -> None:
    if set(outcome) != {
        "kind",
        "question_ref",
        "context_pack_ref",
        "candidates",
        "recommendation",
    } | ({"notes"} if "notes" in outcome else set()):
        raise IdeaContractError("idea_set_shape_invalid")
    candidates = outcome["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise IdeaContractError("idea_set_empty")
    keys: list[str] = []
    material_candidates: list[str] = []
    for candidate in candidates:
        required = {"candidate_key", "direction", "rationale", "evidence_boundary"}
        optional = {"assumptions", "risks", "falsification_hint", "material_difference", "notes"}
        if (not isinstance(candidate, dict) or not required <= set(candidate)
                or not set(candidate) <= required | optional):
            raise IdeaContractError("idea_candidate_shape_invalid")
        if "notes" in candidate and not isinstance(candidate["notes"], str):
            raise IdeaContractError("idea_notes_invalid")
        for field in ("candidate_key", "direction", "rationale"):
            _require_text(candidate[field], field)
        keys.append(cast(str, candidate["candidate_key"]))
        for field in ("assumptions", "risks"):
            if field in candidate:
                _require_text_list(candidate[field], "candidate_" + field)
        _validate_evidence_boundary(candidate["evidence_boundary"], accepted_refs)
        if "falsification_hint" in candidate:
            _validate_text_object(candidate["falsification_hint"], {"test", "would_refute"}, "falsification_hint")
        if "material_difference" in candidate:
            _validate_text_object(candidate["material_difference"], {"from_history", "from_peers", "plan_commitment_change"}, "material_difference")
        material_candidate = dict(candidate)
        material_candidate.pop("candidate_key")
        if "material_difference" in candidate:
            difference = dict(cast(dict[str, object], candidate["material_difference"]))
            difference.pop("from_peers")
            material_candidate["material_difference"] = difference
        material_candidates.append(
            _canonical_hash(_material_value(material_candidate))
        )
    if len(keys) != len(set(keys)):
        raise IdeaContractError("idea_candidate_key_duplicate")
    if len(material_candidates) != len(set(material_candidates)):
        raise IdeaContractError("idea_candidate_material_duplicate")
    recommendation = outcome["recommendation"]
    if recommendation is not None:
        if not isinstance(recommendation, dict) or set(recommendation) != {
            "note",
            "binding",
        }:
            raise IdeaContractError("idea_recommendation_shape_invalid")
        _require_text(recommendation["note"], "recommendation_note")
        if recommendation["binding"] is not False:
            raise IdeaContractError("idea_recommendation_must_be_advisory")


def _validate_no_viable(
    outcome: dict[str, object], accepted_refs: set[str] | None
) -> None:
    if set(outcome) != {
        "kind",
        "question_ref",
        "context_pack_ref",
        "exploration_scope",
        "candidate_families_considered",
        "evidence_boundary",
        "overturn_conditions",
        "why_plan_cannot_proceed",
    } | ({"notes"} if "notes" in outcome else set()):
        raise IdeaContractError("no_viable_shape_invalid")
    _require_text(outcome["exploration_scope"], "exploration_scope")
    _require_text(outcome["why_plan_cannot_proceed"], "why_plan_cannot_proceed")
    _require_text_list(outcome["overturn_conditions"], "overturn_conditions")
    _validate_evidence_boundary(outcome["evidence_boundary"], accepted_refs)
    families = outcome["candidate_families_considered"]
    if not isinstance(families, list) or not families:
        raise IdeaContractError("candidate_families_missing")
    for family in families:
        if not isinstance(family, dict) or set(family) != {
            "family",
            "why_not_viable",
            "evidence_refs",
        }:
            raise IdeaContractError("candidate_family_shape_invalid")
        _require_text(family["family"], "candidate_family")
        _require_text(family["why_not_viable"], "why_not_viable")
        refs = family["evidence_refs"]
        if not isinstance(refs, list) or not all(
            isinstance(item, str)
            and item
            and (accepted_refs is None or item in accepted_refs)
            for item in refs
        ):
            raise IdeaContractError("candidate_family_evidence_unbound")


def _validate_evidence_boundary(value: object, accepted_refs: set[str] | None) -> None:
    if not isinstance(value, dict) or set(value) != {
        "accepted_evidence_refs",
        "supported",
        "inferred",
        "unknown",
    }:
        raise IdeaContractError("evidence_boundary_shape_invalid")
    refs = value["accepted_evidence_refs"]
    if not isinstance(refs, list) or not all(
        isinstance(item, str)
        and item
        and (accepted_refs is None or item in accepted_refs)
        for item in refs
    ):
        raise IdeaContractError("accepted_evidence_ref_unbound")
    for field in ("supported", "inferred", "unknown"):
        _require_text(value[field], field)


def _validate_text_object(value: object, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise IdeaContractError(f"{label}_shape_invalid")
    for field in fields:
        _require_text(value[field], f"{label}_{field}")


def _require_text_list(value: object, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise IdeaContractError(f"{label}_missing")
    for item in value:
        _require_text(item, label)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise IdeaContractError(f"{label}_invalid")


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_hash(value: object) -> str:
    payload = _canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_literature_reference(value, question_ref):
    if value is None:
        return
    fields={"kind","revision_ref","question_ref","literature_snapshot_ref",
        "rm_acceptance_receipt_ref","rg_question_association_receipt_ref","receipt",
        "records_hash","record_count","records_preview"}
    if not isinstance(value,dict) or set(value)!=fields or value["kind"]!="QuestionLiteratureReference" or type(value["record_count"])is not int or value["record_count"]<0 or not _is_hash(value["records_hash"]):
        raise IdeaContractError("idea_context_pack_invalid")
    # Pure receipt/identity shape check; Owner separately compares this reference
    # to the exact accepted revision. The empty records here are never authority.
    structural={k:v for k,v in value.items() if k not in {"records_hash","record_count","records_preview"}}
    structural.update(kind="QuestionLiteratureRevision",records=[])
    _validate_question_literature_revision_binding(structural,question_ref=question_ref)
    page=value["records_preview"]
    if not isinstance(page,dict) or type(page.get("items"))is not list or page.get("shown_count")!=len(page["items"]) or page.get("total_count")!=value["record_count"] or len(page["items"])>24 or page.get("truncated")!=(len(page["items"])<value["record_count"]):
        raise IdeaContractError("idea_context_pack_invalid")


def _validate_context_pack_v4(pack, *, cycle_ref, accepted_question_binding):
    if set(pack)!=_IDEA_CONTEXT_PACK_V2_FIELDS|{"evidence_page"}:
        raise IdeaContractError("idea_context_pack_invalid")
    legacy={k:v for k,v in pack.items() if k!="evidence_page"}
    legacy.update(schema_ref=IDEA_CONTEXT_PACK_SCHEMA_V2_REF,literature_binding=None,prior_accepted_bindings=[])
    refs=validate_idea_context_pack(legacy,cycle_ref=cycle_ref,accepted_question_binding=accepted_question_binding)
    page=pack["evidence_page"]
    if not isinstance(page,dict) or set(page)!={"schema_ref","total_count","shown_count","offset","limit","next_offset","selection","complete","references_hash"} or page["schema_ref"]!="meta-research/evidence-reference-page/v1" or type(page["total_count"])is not int or page["total_count"]<len(refs) or page["shown_count"]!=len(refs) or page["total_count"]!=pack["evidence_reference_revision"] or page["offset"]!=0 or page["limit"]!=32 or len(refs)>32 or page["references_hash"]!=_canonical_hash(sorted(refs)) or page["complete"]!=(len(refs)==page["total_count"]) or page["next_offset"]!=(len(refs) if len(refs)<page["total_count"] else None):
        raise IdeaContractError("idea_context_pack_invalid")
    _validate_literature_reference(pack["literature_binding"],accepted_question_binding.get("question_ref"))
    prior=pack["prior_accepted_bindings"]
    if not isinstance(prior,list) or len(prior)>1:
        raise IdeaContractError("idea_context_pack_invalid")
    for item in prior:
        if not isinstance(item,dict) or set(item)-{"research_notes","handoff_notes"}!={"kind","cycle_ref","commit_ref","outcome_ref","closure_hash","receipt","outcome_receipt","scientific_summary","exact_reader"} or item["kind"]!="ReasoningHandoffReference" or not _is_hash(item["closure_hash"]) or item["exact_reader"]!="research_memory.stage_context.read":
            raise IdeaContractError("idea_context_pack_invalid")
        if "research_notes" in item and not isinstance(item["research_notes"], list):
            raise IdeaContractError("idea_context_pack_invalid")
        if "handoff_notes" in item:
            note = item["handoff_notes"]
            if (not isinstance(note, dict) or set(note) != {"text", "source_utf8_bytes", "truncated"}
                or not isinstance(note["text"], str) or len(note["text"].encode()) > 2048
                or type(note["source_utf8_bytes"]) is not int
                or note["source_utf8_bytes"] < len(note["text"].encode())
                or note["truncated"] != (note["source_utf8_bytes"] > len(note["text"].encode()))):
                raise IdeaContractError("idea_context_pack_invalid")
        _validate_public_receipt(item["receipt"],issuer="advancement_engine",subject_ref=item["commit_ref"])
        _validate_public_receipt(item["outcome_receipt"],issuer="research_graph",subject_ref=item["outcome_ref"])
    return refs
