from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from meta_research.owners.common import canonical_hash


SCIENTIFIC_OUTCOME_SCHEMA_REF = "meta-research/scientific-outcome-candidate/v1"
REASONING_STAGE_OUTPUT_SCHEMA_REF = "meta-research/reasoning-stage-output/v1"
REASONING_REVIEW_SCHEMA_REF = "meta-research/reasoning-review/v1"
REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF = (
    "meta-research/reasoning-autonomous-checkpoint/v1"
)
AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF = (
    "meta-research/autonomous-question-scope/v1"
)
AUTONOMOUS_QUESTION_PROPOSAL_SCHEMA_REF = (
    "meta-research/autonomous-question-proposal/v1"
)
NEXT_CYCLE_PROPOSAL_SCHEMA_REF = "meta-research/next-cycle-proposal/v1"
CANDIDATE_COMPLETION_SCHEMA_REF = "meta-research/candidate-completion/v1"
SCIENTIFIC_OUTCOMES = frozenset(
    {"affirmed", "denied", "uncertain", "insufficient_evidence"}
)
FORMAL_QUESTION_FIELDS = (
    "title",
    "unknown_statement",
    "answer_shape",
    "applicability_scope",
    "background_context",
    "requirements_constraints",
)

_OUTCOME_FIELDS = {
    "schema_ref",
    "kind",
    "outcome_ref",
    "stage_run_request_ref",
    "cycle_ref",
    "question_ref",
    "quest_ref",
    "goal_revision_ref",
    "foreground_epoch",
    "disposition",
    "claim",
    "evidence",
    "missing_evidence",
    "uncertainty_basis",
    "support_scope",
    "limitations",
    "causal_interpretation",
    "research_synthesis",
    "is_authoritative",
}
_LITERATURE_EVIDENCE_BASES = {
    "title_lead",
    "citation_context",
    "abstract",
    "verified_fulltext",
}
_SCIENTIFIC_FINDINGS = {"supporting", "negative", "partial", "context"}
_SUBSTANTIVE_EVIDENCE_KINDS = {"LiteratureRecord", "MetricResult"}
_DIAGNOSTIC_EVIDENCE_KINDS = {"LogAsset", "AnalysisAsset", "CheckpointArtifact"}
_EVIDENCE_REUSE_LEAF_FIELDS = {
    "schema_ref",
    "kind",
    "role",
    "evidence_ref",
    "evidence_item_ref",
    "source_role_ref",
    "source_variant_run_ref",
    "source_evaluation_attempt_ref",
    "source_subject_kind",
    "source_subject_ref",
    "target_commit_ref",
    "asset_version_ref",
    "evidence_catalog_entry_hash",
    "evidence_use_hashes",
    "evidence_asset_receipt",
    "evidence_role_receipt",
    "formal_measurement_acceptance_receipt",
    "target_commit_acceptance_receipt",
}
_AUTONOMOUS_PROPOSAL_FIELDS = {
    "schema_ref",
    "kind",
    "creation_mode",
    "source_quest_ref",
    "source_cycle_ref",
    "source_reasoning_stage_run_request_ref",
    "source_scientific_outcome_ref",
    "source_question_ref",
    "source_foreground_epoch",
    "question",
    "is_authoritative",
}
_AUTONOMOUS_SCOPE_FIELDS = {
    "schema_ref",
    "kind",
    "creation_mode",
    "mode",
    "source_quest_ref",
    "source_cycle_ref",
    "source_reasoning_stage_run_request_ref",
    "source_scientific_outcome_ref",
    "source_question_ref",
    "source_foreground_epoch",
    "question_blueprint",
    "parent_question_ref",
    "decomposition_basis_refs",
    "entry_stage",
    "typed_skip_basis_refs_by_stage",
    "is_authoritative",
}
_REASONING_SOURCE_FIELDS = {
    "source_quest_ref",
    "source_cycle_ref",
    "source_reasoning_stage_run_request_ref",
    "source_scientific_outcome_ref",
    "source_question_ref",
    "source_foreground_epoch",
}
_NEXT_CYCLE_PROPOSAL_FIELDS = _REASONING_SOURCE_FIELDS | {
    "schema_ref",
    "kind",
    "target_question_ref",
    "target_question_anchor_ref",
    "entry_stage",
    "typed_skip_basis_refs_by_stage",
    "is_authoritative",
}
_CANDIDATE_COMPLETION_FIELDS = _REASONING_SOURCE_FIELDS | {
    "schema_ref",
    "kind",
    "current_quest_ref",
    "current_goal_revision_ref",
    "completion_milestone_basis_refs",
    "rationale",
    "is_authoritative",
}


class ReasoningContractError(ValueError):
    """A Reasoning candidate or one of its frozen bindings is invalid."""


@dataclass(frozen=True)
class VerifiedReasoningCompletionLineage:
    """RM-verified immutable lineage for one completion transition."""

    request_ref: str
    content_ref: str
    content_receipt_ref: str
    context_pack_ref: str
    context_pack_hash: str
    source_outcome_ref: str
    transition_ref: str
    transition_hash: str
    quest_ref: str
    goal_revision_ref: str
    completion_milestone_basis_refs: tuple[str, ...]


def plan_evidence_reuse_leaves(
    context_pack: dict[str, object],
) -> list[dict[str, object]]:
    """Validate and normalize a Plan-selected Target evidence closure.

    The ContextPack carries the exact accepted FormalPlan uses and a typed
    issuer-owned leaf for every role selected from each EvidenceRef's exact
    TargetCommit.  Consumers cite the role identity, never the catalog
    identity.  Diagnostic roles remain contextual; only the MetricResult leaf
    is substantive.
    """

    plan_input = context_pack.get("plan_evidence_input")
    if not isinstance(plan_input, dict) or plan_input.get("kind") not in {
        "none",
        "accepted",
    }:
        raise ReasoningContractError("reasoning_plan_evidence_binding_invalid")
    if plan_input.get("kind") == "none":
        if set(plan_input) != {"kind", "basis_stage_commit_refs"}:
            raise ReasoningContractError(
                "reasoning_plan_evidence_binding_invalid"
            )
        return []
    _exact_keys(
        plan_input,
        {
            "kind",
            "formal_plan_binding",
            "evidence_reuse_set",
            "evidence_reuse_closure",
        },
        "reasoning_plan_evidence_binding_invalid",
    )
    if not isinstance(plan_input.get("formal_plan_binding"), dict):
        raise ReasoningContractError("reasoning_plan_evidence_binding_invalid")
    uses = plan_input.get("evidence_reuse_set")
    leaves = plan_input.get("evidence_reuse_closure")
    if not isinstance(uses, list) or not isinstance(leaves, list):
        raise ReasoningContractError("reasoning_plan_evidence_closure_invalid")
    uses_by_ref: dict[str, list[dict[str, object]]] = {}
    for value in uses:
        if not isinstance(value, dict):
            raise ReasoningContractError(
                "reasoning_plan_evidence_closure_invalid"
            )
        evidence_ref = _require_text(
            value.get("evidence_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        uses_by_ref.setdefault(evidence_ref, []).append(value)

    normalized: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    item_refs: list[str] = []
    roles_by_evidence_ref: dict[str, list[str]] = {}
    for value in leaves:
        if not isinstance(value, dict):
            raise ReasoningContractError(
                "reasoning_plan_evidence_closure_invalid"
            )
        _exact_keys(
            value,
            _EVIDENCE_REUSE_LEAF_FIELDS,
            "reasoning_plan_evidence_closure_invalid",
        )
        if (
            value.get("schema_ref")
            != "meta-research/evidence-reuse-leaf/v1"
            or value.get("kind") != "EvidenceReuseLeaf"
            or value.get("role")
            not in _SUBSTANTIVE_EVIDENCE_KINDS | _DIAGNOSTIC_EVIDENCE_KINDS
            or value.get("role") == "LiteratureRecord"
        ):
            raise ReasoningContractError(
                "reasoning_plan_evidence_closure_invalid"
            )
        evidence_ref = _require_text(
            value.get("evidence_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        role = cast(str, value["role"])
        item_ref = _require_text(
            value.get("evidence_item_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        role_ref = _require_text(
            value.get("source_role_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        variant_run_ref = _require_text(
            value.get("source_variant_run_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        attempt_ref = _require_text(
            value.get("source_evaluation_attempt_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        target_commit_ref = _require_text(
            value.get("target_commit_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        asset_version_ref = _require_text(
            value.get("asset_version_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        _require_text(
            value.get("evidence_catalog_entry_hash"),
            "reasoning_plan_evidence_closure_invalid",
        )
        use_hashes = _require_text_list(
            value.get("evidence_use_hashes"),
            "reasoning_plan_evidence_closure_invalid",
        )
        expected_use_hashes = [
            canonical_hash(use) for use in uses_by_ref.get(evidence_ref, [])
        ]
        if not expected_use_hashes or use_hashes != expected_use_hashes:
            raise ReasoningContractError(
                "reasoning_plan_evidence_closure_invalid"
            )
        asset_receipt = _validate_reasoning_owner_receipt(
            value.get("evidence_asset_receipt"),
            issuer="research_memory",
            kind="asset_acceptance",
            subject_ref=asset_version_ref,
        )
        role_receipt = _validate_reasoning_owner_receipt(
            value.get("evidence_role_receipt"),
            issuer="research_graph",
            kind=frozenset(
                {"asset_role_acceptance", "experiment_asset_role_acceptance"}
            ),
            subject_ref=role_ref,
        )
        formal_receipt = _validate_reasoning_owner_receipt(
            value.get("formal_measurement_acceptance_receipt"),
            issuer="research_graph",
            kind="formal_measurement_acceptance",
            subject_ref=attempt_ref,
        )
        target_receipt = _validate_reasoning_owner_receipt(
            value.get("target_commit_acceptance_receipt"),
            issuer="research_graph",
            kind="target_commit",
            subject_ref=target_commit_ref,
        )
        source_subject_kind = value.get("source_subject_kind")
        source_subject_ref = _require_text(
            value.get("source_subject_ref"),
            "reasoning_plan_evidence_closure_invalid",
        )
        expected_subject_ref = (
            variant_run_ref
            if source_subject_kind == "VariantRun"
            else attempt_ref
            if source_subject_kind == "EvaluationAttempt"
            else None
        )
        if (
            source_subject_ref != expected_subject_ref
            or role == "MetricResult"
            and source_subject_kind != "EvaluationAttempt"
            or role == "CheckpointArtifact"
            and source_subject_kind != "VariantRun"
            or role in {"LogAsset", "AnalysisAsset"}
            and source_subject_kind not in {"VariantRun", "EvaluationAttempt"}
        ):
            raise ReasoningContractError(
                "reasoning_plan_evidence_closure_invalid"
            )

        evidence_refs.append(evidence_ref)
        item_refs.append(item_ref)
        roles_by_evidence_ref.setdefault(evidence_ref, []).append(role)
        if role == "MetricResult":
            normalized.append(
                {
                    "kind": "MetricResult",
                    "ref": item_ref,
                    "source_evaluation_attempt_ref": attempt_ref,
                    "research_graph_acceptance_receipt_ref": target_receipt[
                        "receipt_ref"
                    ],
                    "formal_measurement_acceptance_receipt_ref": formal_receipt[
                        "receipt_ref"
                    ],
                }
            )
        else:
            normalized.append(
                {
                    "kind": role,
                    "ref": item_ref,
                    "source_subject_ref": source_subject_ref,
                    "owner_acceptance_receipt_ref": role_receipt[
                        "receipt_ref"
                    ],
                }
            )
        # Both receipt layers are deliberately consumed above.  The normalized
        # candidate-facing closure exposes only stable role identities and
        # receipt refs; it cannot be used to mutate or relabel an Owner fact.
        del asset_receipt
    if (
        evidence_refs != sorted(evidence_refs)
        or len(item_refs) != len(set(item_refs))
        or set(evidence_refs) != set(uses_by_ref)
        or any(
            roles.count("MetricResult") != 1
            for roles in roles_by_evidence_ref.values()
        )
    ):
        raise ReasoningContractError("reasoning_plan_evidence_closure_invalid")
    return normalized


# Internal compatibility for code written during the same feature branch.
# The returned closure is no longer MetricResult-only.
plan_evidence_reuse_metric_leaves = plan_evidence_reuse_leaves


def current_target_evidence_leaves(
    context_pack: dict[str, object],
) -> list[dict[str, object]]:
    """Validate the issuer-closed evidence roles of this Cycle's TargetCommits."""

    target_closures = context_pack.get("accepted_target_commit_closures")
    frozen_leaves = context_pack.get("current_target_evidence_closure")
    if not isinstance(target_closures, list) or not isinstance(
        frozen_leaves, list
    ):
        raise ReasoningContractError("reasoning_target_evidence_closure_invalid")
    expected_target_refs = {
        value.get("target_commit_ref")
        for value in target_closures
        if isinstance(value, dict)
        and isinstance(value.get("target_commit_ref"), str)
    }
    if len(expected_target_refs) != len(target_closures):
        raise ReasoningContractError("reasoning_target_evidence_closure_invalid")
    if not frozen_leaves:
        if expected_target_refs:
            raise ReasoningContractError(
                "reasoning_target_evidence_closure_invalid"
            )
        return []

    copied: list[dict[str, object]] = []
    actual_target_refs: set[object] = set()
    evidence_refs: set[str] = set()
    for value in frozen_leaves:
        if (
            not isinstance(value, dict)
            or value.get("evidence_use_hashes") != []
            or not isinstance(value.get("evidence_ref"), str)
            or not isinstance(value.get("target_commit_ref"), str)
        ):
            raise ReasoningContractError(
                "reasoning_target_evidence_closure_invalid"
            )
        actual_target_refs.add(cast(str, value["target_commit_ref"]))
        evidence_refs.add(cast(str, value["evidence_ref"]))
        copied.append(dict(value))
    if actual_target_refs != expected_target_refs:
        raise ReasoningContractError("reasoning_target_evidence_closure_invalid")

    synthetic_uses = [
        {"evidence_ref": evidence_ref} for evidence_ref in sorted(evidence_refs)
    ]
    use_hashes = {
        cast(str, use["evidence_ref"]): canonical_hash(use)
        for use in synthetic_uses
    }
    for value in copied:
        value["evidence_use_hashes"] = [
            use_hashes[cast(str, value["evidence_ref"])]
        ]
    try:
        return plan_evidence_reuse_leaves(
            {
                "plan_evidence_input": {
                    "kind": "accepted",
                    "formal_plan_binding": {"kind": "current-target-closure"},
                    "evidence_reuse_set": synthetic_uses,
                    "evidence_reuse_closure": copied,
                }
            }
        )
    except ReasoningContractError as error:
        raise ReasoningContractError(
            "reasoning_target_evidence_closure_invalid"
        ) from error


def _validate_reasoning_owner_receipt(
    value: object,
    *,
    issuer: str,
    kind: str | frozenset[str],
    subject_ref: str | None = None,
) -> dict[str, str]:
    fields = {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("status") != "accepted"
        or value.get("issuer") != issuer
        or (
            value.get("kind") not in kind
            if isinstance(kind, frozenset)
            else value.get("kind") != kind
        )
        or subject_ref is not None
        and value.get("subject_ref") != subject_ref
        or any(
            not isinstance(value.get(field), str) or not value[field]
            for field in fields - {"status"}
        )
    ):
        raise ReasoningContractError("reasoning_plan_evidence_closure_invalid")
    return cast(dict[str, str], value)


def completion_milestone_basis_refs(
    context_pack: dict[str, object],
) -> tuple[str, ...]:
    """Return the exact ordered StageCommit basis frozen for Reasoning."""

    closure = context_pack.get("upstream_stage_closure")
    if not isinstance(closure, list) or not closure:
        raise ReasoningContractError("reasoning_context_pack_invalid")
    refs: list[str] = []
    for item in closure:
        if not isinstance(item, dict):
            raise ReasoningContractError("reasoning_context_pack_invalid")
        refs.append(
            _require_text(item.get("commit_ref"), "reasoning_context_pack_invalid")
        )
    if len(refs) != len(set(refs)):
        raise ReasoningContractError("reasoning_context_pack_invalid")
    return tuple(refs)


def validate_reasoning_stage_output(
    output: dict[str, object],
    *,
    frozen_evidence_closure: list[dict[str, object]],
    frozen_research_context: dict[str, object],
    expected_completion_milestone_basis_refs: tuple[str, ...] | None = None,
) -> tuple[str, str, str]:
    """Validate the one closed execution document emitted by Reasoning.

    AR persists this document as an execution fact.  RM independently
    validates the scientific candidate and the exclusive transition before it
    can issue a content receipt.
    """

    _exact_keys(
        output,
        {
            "schema_ref",
            "scientific_outcome",
            "next_cycle_proposal",
            "candidate_completion",
        },
        "reasoning_stage_output_invalid",
    )
    if output.get("schema_ref") != REASONING_STAGE_OUTPUT_SCHEMA_REF:
        raise ReasoningContractError("reasoning_stage_output_invalid")
    scientific_outcome = output.get("scientific_outcome")
    if not isinstance(scientific_outcome, dict):
        raise ReasoningContractError("reasoning_stage_output_invalid")
    next_cycle = output.get("next_cycle_proposal")
    completion = output.get("candidate_completion")
    if next_cycle is not None and not isinstance(next_cycle, dict):
        raise ReasoningContractError("reasoning_stage_output_invalid")
    if completion is not None and not isinstance(completion, dict):
        raise ReasoningContractError("reasoning_stage_output_invalid")
    outcome_hash = validate_scientific_outcome(
        scientific_outcome,
        frozen_evidence_closure=frozen_evidence_closure,
        frozen_research_context=frozen_research_context,
    )
    transition_hash = validate_reasoning_transition(
        scientific_outcome,
        next_cycle=cast(dict[str, object] | None, next_cycle),
        candidate_completion=cast(dict[str, object] | None, completion),
        expected_completion_milestone_basis_refs=(
            expected_completion_milestone_basis_refs
        ),
    )
    return canonical_hash(output), outcome_hash, transition_hash


def validate_reasoning_autonomous_checkpoint(
    checkpoint: dict[str, object],
    *,
    frozen_evidence_closure: list[dict[str, object]],
    frozen_research_context: dict[str, object],
) -> tuple[str, str, str]:
    """Validate the non-terminal Reasoning checkpoint used by create_question.

    The checkpoint is not a Reasoning Stage output and deliberately has no
    outward transition.  It freezes one reviewed scientific candidate plus an
    internal autonomous scope.  Only after the Question lifecycle returns an
    accepted selectable target may the same Run derive its one closed
    ``NextCycleProposal``.
    """

    _exact_keys(
        checkpoint,
        {"schema_ref", "scientific_outcome", "autonomous_scope"},
        "reasoning_autonomous_checkpoint_invalid",
    )
    if (
        checkpoint.get("schema_ref")
        != REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
    ):
        raise ReasoningContractError("reasoning_autonomous_checkpoint_invalid")
    outcome = checkpoint.get("scientific_outcome")
    scope = checkpoint.get("autonomous_scope")
    if not isinstance(outcome, dict) or not isinstance(scope, dict):
        raise ReasoningContractError("reasoning_autonomous_checkpoint_invalid")
    outcome_hash = validate_scientific_outcome(
        outcome,
        frozen_evidence_closure=frozen_evidence_closure,
        frozen_research_context=frozen_research_context,
    )
    scope_hash = validate_autonomous_question_scope(
        scope,
        source_outcome=outcome,
    )
    return canonical_hash(checkpoint), outcome_hash, scope_hash


def validate_autonomous_question_scope(
    scope: dict[str, object],
    *,
    source_outcome: dict[str, object],
) -> str:
    """Validate Reasoning's internal, non-authoritative creation scope.

    ``question_blueprint`` is reviewed inside the current Reasoning Run, but it
    is not yet a QuestionProposal.  The creation lifecycle forms that proposal
    only after mandatory DeepFetch has an accepted snapshot.
    """

    _exact_keys(
        scope,
        _AUTONOMOUS_SCOPE_FIELDS,
        "autonomous_question_scope_invalid",
    )
    if (
        scope.get("schema_ref") != AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF
        or scope.get("kind") != "AutonomousQuestionScope"
        or scope.get("creation_mode") != "AutonomousCreation"
        or scope.get("is_authoritative") is not False
        or scope.get("mode") not in {"new", "decompose"}
        or scope.get("entry_stage")
        not in {"idea", "plan", "bundle", "reasoning"}
    ):
        raise ReasoningContractError("autonomous_question_scope_invalid")
    _validate_reasoning_source_bindings(scope, source_outcome)

    blueprint = scope.get("question_blueprint")
    if not isinstance(blueprint, dict):
        raise ReasoningContractError("autonomous_question_six_fields_incomplete")
    _validate_formal_question(blueprint)

    parent_question_ref = scope.get("parent_question_ref")
    decomposition_basis_refs = _require_text_list(
        scope.get("decomposition_basis_refs"),
        "autonomous_question_decomposition_invalid",
    )
    if scope.get("mode") == "new":
        if parent_question_ref is not None or decomposition_basis_refs:
            raise ReasoningContractError(
                "autonomous_question_decomposition_invalid"
            )
    elif (
        not isinstance(parent_question_ref, str)
        or not parent_question_ref.strip()
        or not decomposition_basis_refs
        or len(decomposition_basis_refs) != len(set(decomposition_basis_refs))
    ):
        raise ReasoningContractError("autonomous_question_decomposition_invalid")

    skip_basis = scope.get("typed_skip_basis_refs_by_stage")
    if not isinstance(skip_basis, dict):
        raise ReasoningContractError("autonomous_question_skip_basis_invalid")
    stage_order = ("idea", "plan", "bundle", "reasoning")
    entry_stage = cast(str, scope["entry_stage"])
    expected_skips = set(stage_order[: stage_order.index(entry_stage)])
    if set(skip_basis) != expected_skips:
        raise ReasoningContractError("autonomous_question_skip_basis_invalid")
    for refs in skip_basis.values():
        normalized = _require_text_list(
            refs,
            "autonomous_question_skip_basis_invalid",
        )
        if not normalized or len(normalized) != len(set(normalized)):
            raise ReasoningContractError("autonomous_question_skip_basis_invalid")
    return canonical_hash(scope)


def autonomous_question_proposal_from_scope(
    scope: dict[str, object],
    *,
    source_outcome: dict[str, object],
) -> dict[str, object]:
    """Form the internal six-field proposal after mandatory DeepFetch."""

    validate_autonomous_question_scope(scope, source_outcome=source_outcome)
    proposal = {
        "schema_ref": AUTONOMOUS_QUESTION_PROPOSAL_SCHEMA_REF,
        "kind": "QuestionProposal",
        "creation_mode": "AutonomousCreation",
        **{
            field: scope[field]
            for field in _REASONING_SOURCE_FIELDS
        },
        "question": dict(cast(dict[str, object], scope["question_blueprint"])),
        "is_authoritative": False,
    }
    validate_autonomous_question_proposal(
        proposal,
        source_outcome=source_outcome,
    )
    return proposal


def validate_reasoning_transition(
    source_outcome: dict[str, object],
    *,
    next_cycle: dict[str, object] | None = None,
    candidate_completion: dict[str, object] | None = None,
    expected_completion_milestone_basis_refs: tuple[str, ...] | None = None,
) -> str:
    """Validate and hash exactly one non-authoritative Reasoning transition."""

    _require_source_outcome(source_outcome)
    if (next_cycle is None) == (candidate_completion is None):
        raise ReasoningContractError("reasoning_transition_xor_invalid")
    chosen = next_cycle if next_cycle is not None else candidate_completion
    if not isinstance(chosen, dict):
        raise ReasoningContractError("reasoning_transition_invalid")
    _validate_reasoning_source_bindings(chosen, source_outcome)

    if next_cycle is not None:
        _exact_keys(
            next_cycle,
            _NEXT_CYCLE_PROPOSAL_FIELDS,
            "next_cycle_proposal_invalid",
        )
        if (
            next_cycle.get("schema_ref") != NEXT_CYCLE_PROPOSAL_SCHEMA_REF
            or next_cycle.get("kind") != "NextCycleProposal"
            or next_cycle.get("is_authoritative") is not False
        ):
            raise ReasoningContractError("next_cycle_proposal_invalid")
        _require_text(
            next_cycle.get("target_question_ref"), "next_cycle_proposal_invalid"
        )
        _require_text(
            next_cycle.get("target_question_anchor_ref"),
            "next_cycle_proposal_invalid",
        )
        _validate_successor_route(
            next_cycle,
            invalid_code="next_cycle_proposal_invalid",
            skip_code="next_cycle_proposal_skip_basis_invalid",
        )
        return canonical_hash(next_cycle)

    completion = cast(dict[str, object], candidate_completion)
    _exact_keys(
        completion,
        _CANDIDATE_COMPLETION_FIELDS,
        "candidate_completion_invalid",
    )
    if (
        completion.get("schema_ref") != CANDIDATE_COMPLETION_SCHEMA_REF
        or completion.get("kind") != "CandidateCompletion"
        or completion.get("is_authoritative") is not False
        or completion.get("current_quest_ref") != source_outcome.get("quest_ref")
        or completion.get("current_goal_revision_ref")
        != source_outcome.get("goal_revision_ref")
    ):
        raise ReasoningContractError("candidate_completion_current_binding_invalid")
    rationale = completion.get("rationale")
    _require_text(rationale, "candidate_completion_invalid")
    basis_refs = _require_text_list(
        completion.get("completion_milestone_basis_refs"),
        "candidate_completion_basis_invalid",
    )
    if (
        not basis_refs
        or len(basis_refs) != len(set(basis_refs))
        or expected_completion_milestone_basis_refs is not None
        and tuple(basis_refs) != expected_completion_milestone_basis_refs
    ):
        raise ReasoningContractError("candidate_completion_basis_invalid")
    return canonical_hash(completion)


def validate_autonomous_question_proposal(
    proposal: dict[str, object],
    *,
    source_outcome: dict[str, object],
) -> str:
    """Validate an internal six-field proposal against its exact Reasoning source."""

    _exact_keys(
        proposal,
        _AUTONOMOUS_PROPOSAL_FIELDS,
        "autonomous_question_proposal_invalid",
    )
    if (
        proposal.get("schema_ref") != AUTONOMOUS_QUESTION_PROPOSAL_SCHEMA_REF
        or proposal.get("kind") != "QuestionProposal"
        or proposal.get("creation_mode") != "AutonomousCreation"
        or proposal.get("is_authoritative") is not False
    ):
        raise ReasoningContractError("autonomous_question_proposal_invalid")
    _require_source_outcome(source_outcome)
    expected_bindings = {
        "source_quest_ref": source_outcome["quest_ref"],
        "source_cycle_ref": source_outcome["cycle_ref"],
        "source_reasoning_stage_run_request_ref": source_outcome[
            "stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": source_outcome["outcome_ref"],
        "source_question_ref": source_outcome["question_ref"],
        "source_foreground_epoch": source_outcome["foreground_epoch"],
    }
    for field, expected in expected_bindings.items():
        if proposal.get(field) != expected:
            raise ReasoningContractError("autonomous_question_source_binding_invalid")

    question = proposal.get("question")
    if not isinstance(question, dict):
        raise ReasoningContractError("autonomous_question_six_fields_incomplete")
    _validate_formal_question(question)
    return canonical_hash(proposal)


def _validate_formal_question(question: dict[str, object]) -> None:
    _exact_keys(
        question,
        set(FORMAL_QUESTION_FIELDS),
        "autonomous_question_six_fields_incomplete",
    )
    for field in FORMAL_QUESTION_FIELDS:
        _require_text(
            question.get(field), "autonomous_question_six_fields_incomplete"
        )


def validate_scientific_outcome(
    outcome: dict[str, object],
    *,
    frozen_evidence_closure: list[dict[str, object]],
    frozen_research_context: dict[str, object],
) -> str:
    """Validate an evidence-bounded scientific outcome candidate."""

    _exact_keys(outcome, _OUTCOME_FIELDS, "scientific_outcome_invalid")
    if (
        outcome.get("schema_ref") != SCIENTIFIC_OUTCOME_SCHEMA_REF
        or outcome.get("kind") != "ScientificOutcomeCandidate"
        or outcome.get("is_authoritative") is not False
    ):
        raise ReasoningContractError("scientific_outcome_invalid")
    for field in (
        "outcome_ref",
        "stage_run_request_ref",
        "cycle_ref",
        "question_ref",
        "quest_ref",
        "goal_revision_ref",
    ):
        _require_text(outcome.get(field), "scientific_outcome_binding_invalid")
    epoch = outcome.get("foreground_epoch")
    if type(epoch) is not int or cast(int, epoch) < 1:
        raise ReasoningContractError("scientific_outcome_binding_invalid")
    disposition = outcome.get("disposition")
    if disposition not in SCIENTIFIC_OUTCOMES:
        raise ReasoningContractError("scientific_outcome_disposition_invalid")

    expected_context = _validate_frozen_research_context(
        frozen_research_context,
        cycle_ref=cast(str, outcome["cycle_ref"]),
        question_ref=cast(str, outcome["question_ref"]),
        quest_ref=cast(str, outcome["quest_ref"]),
        goal_revision_ref=cast(str, outcome["goal_revision_ref"]),
    )

    closure_by_ref: dict[str, dict[str, object]] = {}
    for value in frozen_evidence_closure:
        if not isinstance(value, dict):
            raise ReasoningContractError("reasoning_evidence_closure_invalid")
        kind = value.get("kind")
        if kind == "LiteratureRecord":
            _exact_keys(
                value,
                {"kind", "ref", "evidence_basis", "evidence_basis_ref"},
                "reasoning_evidence_closure_invalid",
            )
            if value.get("evidence_basis") not in _LITERATURE_EVIDENCE_BASES:
                raise ReasoningContractError("reasoning_evidence_closure_invalid")
            _require_text(
                value.get("evidence_basis_ref"),
                "reasoning_evidence_closure_invalid",
            )
        elif kind == "MetricResult":
            _exact_keys(
                value,
                {
                    "kind",
                    "ref",
                    "source_evaluation_attempt_ref",
                    "research_graph_acceptance_receipt_ref",
                    "formal_measurement_acceptance_receipt_ref",
                },
                "reasoning_evidence_closure_invalid",
            )
            for field in (
                "source_evaluation_attempt_ref",
                "research_graph_acceptance_receipt_ref",
                "formal_measurement_acceptance_receipt_ref",
            ):
                _require_text(
                    value.get(field), "reasoning_evidence_closure_invalid"
                )
        elif kind in _DIAGNOSTIC_EVIDENCE_KINDS:
            _exact_keys(
                value,
                {
                    "kind",
                    "ref",
                    "source_subject_ref",
                    "owner_acceptance_receipt_ref",
                },
                "reasoning_evidence_closure_invalid",
            )
            _require_text(
                value.get("source_subject_ref"),
                "reasoning_evidence_closure_invalid",
            )
            _require_text(
                value.get("owner_acceptance_receipt_ref"),
                "reasoning_evidence_closure_invalid",
            )
        else:
            raise ReasoningContractError("reasoning_evidence_closure_invalid")
        evidence_ref = _require_text(
            value.get("ref"), "reasoning_evidence_closure_invalid"
        )
        if evidence_ref in closure_by_ref:
            raise ReasoningContractError("reasoning_evidence_closure_invalid")
        closure_by_ref[evidence_ref] = value

    evidence = outcome.get("evidence")
    if not isinstance(evidence, list):
        raise ReasoningContractError("scientific_outcome_evidence_invalid")
    cited_refs: set[str] = set()
    has_substantive_evidence = False
    for value in evidence:
        if not isinstance(value, dict):
            raise ReasoningContractError("scientific_outcome_evidence_invalid")
        _exact_keys(
            value,
            {"kind", "ref", "finding"},
            "scientific_outcome_evidence_invalid",
        )
        evidence_ref = _require_text(
            value.get("ref"), "scientific_outcome_evidence_invalid"
        )
        frozen = closure_by_ref.get(evidence_ref)
        frozen_kind = None if frozen is None else frozen.get("kind")
        finding = value.get("finding")
        if (
            frozen is None
            or value.get("kind") != frozen_kind
            or finding not in _SCIENTIFIC_FINDINGS
            or (
                frozen_kind in _DIAGNOSTIC_EVIDENCE_KINDS
                and finding != "context"
            )
            or evidence_ref in cited_refs
        ):
            raise ReasoningContractError("scientific_outcome_evidence_invalid")
        cited_refs.add(evidence_ref)
        if frozen_kind in _SUBSTANTIVE_EVIDENCE_KINDS:
            has_substantive_evidence = True

    missing_evidence = _require_text_list(
        outcome.get("missing_evidence"), "scientific_outcome_missing_evidence_invalid"
    )
    uncertainty_basis = _require_text_list(
        outcome.get("uncertainty_basis"), "scientific_outcome_uncertainty_invalid"
    )
    if disposition != "insufficient_evidence" and not has_substantive_evidence:
        raise ReasoningContractError("scientific_outcome_substantive_evidence_missing")
    _validate_outcome_scope_and_synthesis(
        outcome,
        cited_refs=cited_refs,
        expected_context=expected_context,
    )
    if disposition == "insufficient_evidence":
        if (
            outcome.get("claim") is not None
            or not missing_evidence
            or uncertainty_basis
        ):
            raise ReasoningContractError(
                "scientific_outcome_insufficient_evidence_invalid"
            )
        return canonical_hash(outcome)

    _require_text(outcome.get("claim"), "scientific_outcome_claim_invalid")
    if missing_evidence:
        raise ReasoningContractError("scientific_outcome_disposition_boundary_invalid")
    if disposition == "uncertain":
        if not uncertainty_basis:
            raise ReasoningContractError("scientific_outcome_uncertainty_invalid")
    elif uncertainty_basis:
        raise ReasoningContractError("scientific_outcome_disposition_boundary_invalid")
    return canonical_hash(outcome)


def _validate_frozen_research_context(
    context: dict[str, object],
    *,
    cycle_ref: str,
    question_ref: str,
    quest_ref: str,
    goal_revision_ref: str,
) -> dict[str, object]:
    _exact_keys(
        context,
        {
            "schema_ref",
            "cycle_ref",
            "quest_ref",
            "question_ref",
            "goal_revision_ref",
            "quest_goal_revision",
            "graph_binding",
            "causal_context",
            "upstream_stage_commit_refs",
        },
        "reasoning_research_context_invalid",
    )
    if (
        context.get("schema_ref") != "meta-research/reasoning-research-context/v2"
        or context.get("cycle_ref") != cycle_ref
        or context.get("question_ref") != question_ref
        or context.get("quest_ref") != quest_ref
        or context.get("goal_revision_ref") != goal_revision_ref
    ):
        raise ReasoningContractError("reasoning_research_context_invalid")
    goal = context.get("quest_goal_revision")
    graph = context.get("graph_binding")
    if (
        not isinstance(goal, dict)
        or goal.get("kind") != "QuestGoalRevision"
        or goal.get("quest_ref") != quest_ref
        or goal.get("goal_revision_ref") != goal_revision_ref
        or not isinstance(graph, dict)
    ):
        raise ReasoningContractError("reasoning_research_context_invalid")
    _exact_keys(
        graph,
        {
            "schema_ref",
            "issuer",
            "quest_ref",
            "question_ref",
            "graph_revision_ref",
            "active_question_refs",
            "parent_question_bindings",
            "prior_current_question_outcomes",
            "binding_ref",
            "binding_hash",
        },
        "reasoning_research_context_invalid",
    )
    if (
        graph.get("schema_ref") != "meta-research/reasoning-graph-context/v1"
        or graph.get("issuer") != "research_graph"
        or graph.get("quest_ref") != quest_ref
        or graph.get("question_ref") != question_ref
    ):
        raise ReasoningContractError("reasoning_research_context_invalid")
    for field in ("graph_revision_ref", "binding_ref", "binding_hash"):
        _require_text(graph.get(field), "reasoning_research_context_invalid")
    active = _require_text_list(
        graph.get("active_question_refs"), "reasoning_research_context_invalid"
    )
    if active != sorted(set(active)) or question_ref not in active:
        raise ReasoningContractError("reasoning_research_context_invalid")
    parents = graph.get("parent_question_bindings")
    prior = graph.get("prior_current_question_outcomes")
    if not isinstance(parents, list) or not isinstance(prior, list):
        raise ReasoningContractError("reasoning_research_context_invalid")
    causal = context.get("causal_context")
    if not isinstance(causal, dict):
        raise ReasoningContractError("reasoning_research_context_invalid")
    _exact_keys(
        causal,
        {"target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs", "provenance_refs"},
        "reasoning_research_context_invalid",
    )
    causal_expected: dict[str, list[str]] = {}
    for field in ("target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs", "provenance_refs"):
        values = _require_text_list(causal.get(field), "reasoning_research_context_invalid")
        if values != sorted(set(values)):
            raise ReasoningContractError("reasoning_research_context_invalid")
        causal_expected[field] = values
    parent_refs: list[str] = []
    for parent in parents:
        if not isinstance(parent, dict):
            raise ReasoningContractError("reasoning_research_context_invalid")
        _exact_keys(
            parent,
            {"question_ref", "parent_question_ref", "question_receipt_ref"},
            "reasoning_research_context_invalid",
        )
        parent_ref = _require_text(
            parent.get("question_ref"), "reasoning_research_context_invalid"
        )
        ancestor = parent.get("parent_question_ref")
        if ancestor is not None and not isinstance(ancestor, str):
            raise ReasoningContractError("reasoning_research_context_invalid")
        _require_text(
            parent.get("question_receipt_ref"),
            "reasoning_research_context_invalid",
        )
        if parent_ref in parent_refs or parent_ref not in active:
            raise ReasoningContractError("reasoning_research_context_invalid")
        parent_refs.append(parent_ref)
    prior_refs: list[str] = []
    for accepted in prior:
        if not isinstance(accepted, dict):
            raise ReasoningContractError("reasoning_research_context_invalid")
        _exact_keys(
            accepted,
            {
                "cycle_ref",
                "request_ref",
                "outcome_ref",
                "disposition",
                "outcome_receipt_ref",
            },
            "reasoning_research_context_invalid",
        )
        for field in ("cycle_ref", "request_ref", "outcome_ref", "outcome_receipt_ref"):
            _require_text(accepted.get(field), "reasoning_research_context_invalid")
        if accepted.get("disposition") not in SCIENTIFIC_OUTCOMES:
            raise ReasoningContractError("reasoning_research_context_invalid")
        accepted_ref = cast(str, accepted["outcome_ref"])
        if accepted_ref in prior_refs:
            raise ReasoningContractError("reasoning_research_context_invalid")
        prior_refs.append(accepted_ref)
    commits = _require_text_list(
        context.get("upstream_stage_commit_refs"),
        "reasoning_research_context_invalid",
    )
    if len(commits) != 3 or len(commits) != len(set(commits)):
        raise ReasoningContractError("reasoning_research_context_invalid")
    return {
        "parent_question_refs": parent_refs,
        "prior_outcome_refs": prior_refs,
        "graph_revision_ref": graph["graph_revision_ref"],
        "causal_context": causal_expected,
    }


def _validate_outcome_scope_and_synthesis(
    outcome: dict[str, object],
    *,
    cited_refs: set[str],
    expected_context: dict[str, object],
) -> None:
    support_scope = _require_text_list(
        outcome.get("support_scope"), "scientific_outcome_support_scope_invalid"
    )
    limitations = _require_text_list(
        outcome.get("limitations"), "scientific_outcome_limitations_invalid"
    )
    if not support_scope or len(support_scope) != len(set(support_scope)) or len(limitations) != len(set(limitations)):
        raise ReasoningContractError("scientific_outcome_support_scope_invalid")
    causal = outcome.get("causal_interpretation")
    if not isinstance(causal, dict):
        raise ReasoningContractError("scientific_outcome_causal_interpretation_invalid")
    _exact_keys(
        causal,
        {
            "target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs",
            "provenance_refs", "attribution_basis_refs", "claim_scope", "statement",
            "sufficiency_rationale", "confounders",
        },
        "scientific_outcome_causal_interpretation_invalid",
    )
    for field in ("target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs", "provenance_refs", "attribution_basis_refs", "confounders"):
        values = _require_text_list(causal.get(field), "scientific_outcome_causal_interpretation_invalid")
        if len(values) != len(set(values)):
            raise ReasoningContractError("scientific_outcome_causal_interpretation_invalid")
    for field in ("claim_scope", "statement", "sufficiency_rationale"):
        _require_text(causal.get(field), "scientific_outcome_causal_interpretation_invalid")
    attribution = set(cast(list[str], causal["attribution_basis_refs"]))
    if not attribution.issubset(cited_refs):
        raise ReasoningContractError("scientific_outcome_causal_interpretation_invalid")
    frozen_causal = cast(dict[str, list[str]], expected_context["causal_context"])
    for field in ("target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs", "provenance_refs"):
        if causal[field] != frozen_causal[field]:
            raise ReasoningContractError("scientific_outcome_causal_interpretation_invalid")

    synthesis = outcome.get("research_synthesis")
    if not isinstance(synthesis, dict):
        raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
    _exact_keys(synthesis, {"cycle", "current_question", "parent_questions", "quest"}, "scientific_outcome_research_synthesis_invalid")
    cycle = synthesis.get("cycle")
    current = synthesis.get("current_question")
    parents = synthesis.get("parent_questions")
    quest = synthesis.get("quest")
    if not all(isinstance(value, dict) for value in (cycle, current, quest)) or not isinstance(parents, list):
        raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
    assert isinstance(cycle, dict) and isinstance(current, dict) and isinstance(quest, dict)
    _exact_keys(cycle, {"cycle_ref", "impact"}, "scientific_outcome_research_synthesis_invalid")
    _exact_keys(current, {"question_ref", "prior_accepted_outcome_refs", "progress"}, "scientific_outcome_research_synthesis_invalid")
    _exact_keys(quest, {"quest_ref", "goal_revision_ref", "graph_revision_ref", "impact"}, "scientific_outcome_research_synthesis_invalid")
    if cycle.get("cycle_ref") != outcome.get("cycle_ref") or current.get("question_ref") != outcome.get("question_ref") or quest.get("quest_ref") != outcome.get("quest_ref") or quest.get("goal_revision_ref") != outcome.get("goal_revision_ref") or quest.get("graph_revision_ref") != expected_context["graph_revision_ref"]:
        raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
    for value in (cycle.get("impact"), current.get("progress"), quest.get("impact")):
        _require_text(value, "scientific_outcome_research_synthesis_invalid")
    prior_refs = _require_text_list(current.get("prior_accepted_outcome_refs"), "scientific_outcome_research_synthesis_invalid")
    if prior_refs != expected_context["prior_outcome_refs"]:
        raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
    actual_parent_refs: list[str] = []
    for value in parents:
        if not isinstance(value, dict):
            raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
        _exact_keys(value, {"question_ref", "impact", "statement"}, "scientific_outcome_research_synthesis_invalid")
        actual_parent_refs.append(_require_text(value.get("question_ref"), "scientific_outcome_research_synthesis_invalid"))
        if value.get("impact") not in {"material", "no_material", "unknown"}:
            raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")
        _require_text(value.get("statement"), "scientific_outcome_research_synthesis_invalid")
    if actual_parent_refs != expected_context["parent_question_refs"]:
        raise ReasoningContractError("scientific_outcome_research_synthesis_invalid")


def _exact_keys(
    value: dict[str, object],
    expected: set[str],
    code: str,
) -> None:
    if set(value) != expected:
        raise ReasoningContractError(code)


def _validate_successor_route(
    value: dict[str, object],
    *,
    invalid_code: str,
    skip_code: str,
) -> None:
    stage_order = ("idea", "plan", "bundle", "reasoning")
    entry_stage = value.get("entry_stage")
    skip_basis = value.get("typed_skip_basis_refs_by_stage")
    if entry_stage not in stage_order or not isinstance(skip_basis, dict):
        raise ReasoningContractError(invalid_code)
    expected_stages = set(stage_order[: stage_order.index(cast(str, entry_stage))])
    if set(skip_basis) != expected_stages:
        raise ReasoningContractError(skip_code)
    for refs in skip_basis.values():
        normalized = _require_text_list(refs, skip_code)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ReasoningContractError(skip_code)


def _require_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReasoningContractError(code)
    return value


def _require_text_list(value: object, code: str) -> list[str]:
    if not isinstance(value, list):
        raise ReasoningContractError(code)
    normalized: list[str] = []
    for item in value:
        normalized.append(_require_text(item, code))
    return normalized


def _require_source_outcome(outcome: dict[str, object]) -> None:
    if (
        not isinstance(outcome, dict)
        or outcome.get("kind") != "ScientificOutcomeCandidate"
        or outcome.get("disposition") not in SCIENTIFIC_OUTCOMES
    ):
        raise ReasoningContractError("scientific_outcome_source_invalid")
    for field in (
        "outcome_ref",
        "stage_run_request_ref",
        "cycle_ref",
        "question_ref",
        "quest_ref",
        "goal_revision_ref",
    ):
        _require_text(outcome.get(field), "scientific_outcome_source_invalid")
    epoch = outcome.get("foreground_epoch")
    if type(epoch) is not int or cast(int, epoch) < 1:
        raise ReasoningContractError("scientific_outcome_source_invalid")


def _validate_reasoning_source_bindings(
    candidate: dict[str, object],
    source_outcome: dict[str, object],
) -> None:
    expected = {
        "source_quest_ref": source_outcome["quest_ref"],
        "source_cycle_ref": source_outcome["cycle_ref"],
        "source_reasoning_stage_run_request_ref": source_outcome[
            "stage_run_request_ref"
        ],
        "source_scientific_outcome_ref": source_outcome["outcome_ref"],
        "source_question_ref": source_outcome["question_ref"],
        "source_foreground_epoch": source_outcome["foreground_epoch"],
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise ReasoningContractError("reasoning_transition_source_binding_invalid")
