from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
import json
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast
from urllib.parse import urlsplit

from meta_research.codex_runtime import (
    CODEX_MODEL_REF,
    CODEX_REASONING_EFFORT_BINDING,
)
from meta_research.harness import (
    FullConformanceBinding,
    HarnessAdmissionError,
    ResidentMcpChannel,
)
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    ProviderTransportLimits,
    _DISABLED_CODEX_FEATURES,
    _compile_codex_output_schema,
    _codex_harness_manifest,
    _file_sha256,
    _shared_codex_adapter_source_hash,
    _verify_child_review_trace,
    _verify_primary_phase_trace,
)
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import transport_key_hash
from meta_research.reasoning_contract import (
    AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
    REASONING_REVIEW_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    ReasoningContractError,
    completion_milestone_basis_refs,
    plan_evidence_reuse_leaves,
    validate_reasoning_autonomous_checkpoint,
    validate_reasoning_stage_output,
    validate_scientific_outcome,
)


ReasoningSkillContractError = ReasoningContractError
ReasoningSkillUnavailable = IdeaSkillUnavailable
REASONING_PROVIDER_TRANSPORT_LIMITS = ProviderTransportLimits(
    prompt_max_bytes=64 * 1024 * 1024,
    stream_max_bytes=64 * 1024 * 1024,
    result_max_bytes=16 * 1024 * 1024,
)
REASONING_ROOT_SEMANTIC_OPERATION_IDS = (
    "advancement_engine.reasoning_stage_run.observe",
    "research_memory.reasoning_evidence.read",
    "research_graph.reasoning_context.read",
)
_REASONING_CURRENTNESS_OPERATION_ID = (
    "advancement_engine.reasoning_stage_run.observe"
)
_FULL_CONFORMANCE_CAPABILITY = "harness-full-conformance-v1"
_FULL_CONFORMANCE_MCP_PREFIX = "harness-full-conformance:semantic-mcp-"
_FULL_CONFORMANCE_RESOURCE_PREFIX = "harness-artifact:full-conformance-"
REASONING_REVIEW_CATEGORIES = frozenset(
    {
        "source_binding",
        "evidence_boundary",
        "disposition_boundary",
        "transition_boundary",
        "owner_boundary",
        "research_synthesis",
    }
)
REASONING_REVIEW_ACTIONS = frozenset({"revised", "not_adopted"})
_SCIENTIFIC_EVIDENCE_KINDS = frozenset({"LiteratureRecord", "MetricResult"})
_DIAGNOSTIC_EVIDENCE_KINDS = frozenset(
    {"CheckpointArtifact", "LogAsset", "AnalysisAsset"}
)


class ReasoningFullConformanceAuthority(Protocol):
    def require_full_conformance_binding(self) -> FullConformanceBinding: ...

    def issue_resident_mcp_channel(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> ResidentMcpChannel: ...

    def revoke_resident_mcp_channel(self, channel: ResidentMcpChannel) -> None: ...


def bind_reasoning_runtime_to_full_conformance(
    binding: ReasoningRuntimeBinding,
    conformance: FullConformanceBinding,
) -> ReasoningRuntimeBinding:
    """Freeze the current Harness evidence into one Reasoning binding."""

    mcp_bindings = tuple(
        item
        for item in binding.mcp_bindings
        if not item.startswith(_FULL_CONFORMANCE_MCP_PREFIX)
    ) + (
        _FULL_CONFORMANCE_MCP_PREFIX
        + "catalog@sha256:"
        + conformance.semantic_mcp_catalog_hash,
        _FULL_CONFORMANCE_MCP_PREFIX
        + "operation-bindings@sha256:"
        + conformance.semantic_mcp_operation_bindings_hash,
    )
    capability_bindings = tuple(
        item
        for item in binding.capability_bindings
        if item
        not in {
            _FULL_CONFORMANCE_CAPABILITY,
            "mcp-config-empty",
            "semantic-mcp-resident",
        }
    ) + (_FULL_CONFORMANCE_CAPABILITY, "semantic-mcp-resident")
    resource_bindings = tuple(
        item
        for item in binding.resource_bindings
        if not item.startswith(_FULL_CONFORMANCE_RESOURCE_PREFIX)
    ) + (
        "harness-artifact:full-conformance-contract:"
        f"{conformance.contract_ref}@sha256:{conformance.contract_hash}",
        "harness-artifact:full-conformance-set:"
        f"{conformance.conformance_ref}@sha256:{conformance.binding_hash}",
        *conformance.profile_receipts,
    )
    return replace(
        binding,
        mcp_bindings=mcp_bindings,
        capability_bindings=capability_bindings,
        resource_bindings=resource_bindings,
    )


@dataclass(frozen=True)
class ReasoningSkillRequest:
    stage_request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    cycle_ref: str
    question_ref: str
    quest_ref: str
    goal_revision_ref: str
    foreground_epoch: int
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    frozen_evidence_closure: tuple[dict[str, object], ...]
    root_session_ref: str
    runtime_binding: ReasoningRuntimeBinding
    native_session_ref: str | None = None
    job_ref: str | None = None


@dataclass(frozen=True)
class ReasoningSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class ReasoningSkillResult:
    reviewed_draft: dict[str, object]
    scientific_outcome: dict[str, object]
    next_cycle_proposal: dict[str, object] | None
    candidate_completion: dict[str, object] | None
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str

    def outcome_document(self) -> dict[str, object]:
        return {
            "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
            "scientific_outcome": self.scientific_outcome,
            "next_cycle_proposal": self.next_cycle_proposal,
            "candidate_completion": self.candidate_completion,
        }

    @property
    def final_output(self) -> dict[str, object]:
        return self.outcome_document()

    def review_document(self) -> dict[str, object]:
        return {
            "schema_ref": REASONING_REVIEW_SCHEMA_REF,
            "review_mode": self.review_mode,
            "reviewer_agent_ref": self.reviewer_agent_ref,
            "reviewed_draft_hash": canonical_hash(self.reviewed_draft),
            "findings": list(self.findings),
            "dispositions": list(self.dispositions),
            "final_output_hash": canonical_hash(self.outcome_document()),
            "independent": True,
            "advisory_only": True,
        }


@dataclass(frozen=True)
class ReasoningAutonomousCheckpointResult:
    """Independent review of a non-terminal create_question checkpoint."""

    primary_draft: dict[str, object]
    reviewed_checkpoint: dict[str, object]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str

    def review_document(self) -> dict[str, object]:
        return {
            "schema_ref": REASONING_REVIEW_SCHEMA_REF,
            "review_mode": self.review_mode,
            "reviewer_agent_ref": self.reviewer_agent_ref,
            "reviewed_draft_hash": canonical_hash(self.primary_draft),
            "findings": list(self.findings),
            "dispositions": list(self.dispositions),
            "final_output_hash": canonical_hash(self.reviewed_checkpoint),
            "independent": True,
            "advisory_only": True,
        }


class ReasoningSkillProvider(Protocol):
    def runtime_binding(self) -> ReasoningRuntimeBinding: ...

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft: ...

    def review_draft(
        self, request: ReasoningSkillRequest, draft: ReasoningSkillDraft
    ) -> ReasoningSkillResult | ReasoningAutonomousCheckpointResult: ...

    def resume_after_autonomous_creation(
        self,
        request: ReasoningSkillRequest,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> ReasoningSkillResult: ...

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult: ...

    def reconcile_cancelled_job(self, job_ref: str) -> bool: ...


def validate_reasoning_skill_draft(
    request: ReasoningSkillRequest,
    result: ReasoningSkillDraft,
) -> tuple[str, str, str]:
    """Validate one primary checkpoint through the public Skill seam."""

    _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    _validate_output_bindings(request, result.draft)
    if (
        result.draft.get("schema_ref")
        == REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
    ):
        return validate_reasoning_autonomous_checkpoint(
            result.draft,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=cast(
                dict[str, object], request.context_pack["research_context"]
            ),
        )
    return validate_reasoning_stage_output(
        result.draft,
        frozen_evidence_closure=list(request.frozen_evidence_closure),
        frozen_research_context=cast(
            dict[str, object], request.context_pack["research_context"]
        ),
        expected_completion_milestone_basis_refs=(
            _completion_basis_for_output(request, result.draft)
        ),
    )


def validate_reasoning_autonomous_checkpoint_result(
    request: ReasoningSkillRequest,
    draft: ReasoningSkillDraft,
    result: ReasoningAutonomousCheckpointResult,
) -> tuple[str, str, str, str]:
    """Validate the child-reviewed, non-terminal Autonomous checkpoint."""

    _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if (
        result.review_mode != "harness_child_agent"
        or result.primary_draft != draft.draft
        or result.primary_session_ref != draft.primary_session_ref
        or result.adapter_kind != draft.adapter_kind
    ):
        raise ReasoningContractError("reasoning_review_invalid")
    _validate_output_bindings(request, draft.draft)
    _validate_output_bindings(request, result.reviewed_checkpoint)
    draft_hash, _draft_outcome_hash, _draft_scope_hash = (
        validate_reasoning_autonomous_checkpoint(
            draft.draft,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=cast(
                dict[str, object], request.context_pack["research_context"]
            ),
        )
    )
    checkpoint_hash, outcome_hash, scope_hash = (
        validate_reasoning_autonomous_checkpoint(
            result.reviewed_checkpoint,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=cast(
                dict[str, object], request.context_pack["research_context"]
            ),
        )
    )
    review = result.review_document()
    _validate_reasoning_review(
        review,
        reviewed_draft_hash=draft_hash,
        final_output_hash=checkpoint_hash,
    )
    return checkpoint_hash, outcome_hash, scope_hash, canonical_hash(review)


def validate_reasoning_autonomous_resume_result(
    request: ReasoningSkillRequest,
    checkpoint: dict[str, object],
    creation_result: dict[str, object],
    result: ReasoningSkillResult,
) -> tuple[str, str, str, str, str]:
    """Validate the sole closed output after create_question completed."""

    _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent":
        raise ReasoningContractError("reasoning_review_mode_invalid")
    _validate_output_bindings(request, checkpoint)
    checkpoint_hash, _checkpoint_outcome_hash, _checkpoint_scope_hash = (
        validate_reasoning_autonomous_checkpoint(
            checkpoint,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=cast(
                dict[str, object], request.context_pack["research_context"]
            ),
        )
    )
    if result.reviewed_draft != checkpoint:
        raise ReasoningContractError("reasoning_autonomous_resume_lineage_invalid")

    final_output = result.outcome_document()
    _validate_output_bindings(request, final_output)
    final_hash, outcome_hash, transition_hash = validate_reasoning_stage_output(
        final_output,
        frozen_evidence_closure=list(request.frozen_evidence_closure),
        frozen_research_context=cast(
            dict[str, object], request.context_pack["research_context"]
        ),
        expected_completion_milestone_basis_refs=(
            _completion_basis_for_output(request, final_output)
        ),
    )
    review = result.review_document()
    _validate_reasoning_review(
        review,
        reviewed_draft_hash=checkpoint_hash,
        final_output_hash=final_hash,
    )
    checkpoint_outcome = checkpoint.get("scientific_outcome")
    checkpoint_scope = checkpoint.get("autonomous_scope")
    anchor = creation_result.get("question_anchor")
    presence = creation_result.get("graph_presence_fact")
    research_state = creation_result.get("question_research_state_fact")
    next_cycle = result.next_cycle_proposal
    if (
        result.scientific_outcome != checkpoint_outcome
        or not isinstance(anchor, dict)
        or not isinstance(presence, dict)
        or not isinstance(research_state, dict)
        or not isinstance(next_cycle, dict)
        or not isinstance(checkpoint_scope, dict)
        or next_cycle.get("target_question_ref") != anchor.get("question_ref")
        or next_cycle.get("target_question_anchor_ref") != anchor.get("ref")
        or next_cycle.get("entry_stage") != checkpoint_scope.get("entry_stage")
        or next_cycle.get("typed_skip_basis_refs_by_stage")
        != checkpoint_scope.get("typed_skip_basis_refs_by_stage")
        or presence.get("question_ref") != anchor.get("question_ref")
        or presence.get("value") != "present"
        or presence.get("is_current") is not True
        or research_state.get("question_ref") != anchor.get("question_ref")
        or research_state.get("value") != "open"
        or research_state.get("is_current") is not True
        or presence.get("graph_revision_ref")
        != research_state.get("graph_revision_ref")
    ):
        raise ReasoningContractError("reasoning_autonomous_resume_lineage_invalid")
    return (
        checkpoint_hash,
        final_hash,
        outcome_hash,
        transition_hash,
        canonical_hash(review),
    )


def validate_reasoning_skill_result(
    request: ReasoningSkillRequest,
    result: ReasoningSkillResult,
) -> tuple[str, str, str, str, str]:
    """Validate the draft, final output and advisory child-review record."""

    _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent":
        raise ReasoningContractError("reasoning_review_mode_invalid")

    _validate_output_bindings(request, result.reviewed_draft)
    draft_hash, _draft_outcome_hash, _draft_transition_hash = (
        validate_reasoning_stage_output(
            result.reviewed_draft,
            frozen_evidence_closure=list(request.frozen_evidence_closure),
            frozen_research_context=cast(
                dict[str, object], request.context_pack["research_context"]
            ),
            expected_completion_milestone_basis_refs=(
                _completion_basis_for_output(request, result.reviewed_draft)
            ),
        )
    )
    final_output = result.outcome_document()
    _validate_output_bindings(request, final_output)
    final_hash, outcome_hash, transition_hash = validate_reasoning_stage_output(
        final_output,
        frozen_evidence_closure=list(request.frozen_evidence_closure),
        frozen_research_context=cast(
            dict[str, object], request.context_pack["research_context"]
        ),
        expected_completion_milestone_basis_refs=(
            _completion_basis_for_output(request, final_output)
        ),
    )
    review = result.review_document()
    _validate_reasoning_review(
        review,
        reviewed_draft_hash=draft_hash,
        final_output_hash=final_hash,
    )
    return (
        draft_hash,
        final_hash,
        outcome_hash,
        transition_hash,
        canonical_hash(review),
    )


def _validate_reasoning_review(
    review: dict[str, object],
    *,
    reviewed_draft_hash: str,
    final_output_hash: str,
) -> None:
    if set(review) != {
        "schema_ref",
        "review_mode",
        "reviewer_agent_ref",
        "reviewed_draft_hash",
        "findings",
        "dispositions",
        "final_output_hash",
        "independent",
        "advisory_only",
    } or (
        review.get("schema_ref") != REASONING_REVIEW_SCHEMA_REF
        or review.get("review_mode") != "harness_child_agent"
        or review.get("reviewed_draft_hash") != reviewed_draft_hash
        or review.get("final_output_hash") != final_output_hash
        or review.get("independent") is not True
        or review.get("advisory_only") is not True
    ):
        raise ReasoningContractError("reasoning_review_invalid")
    findings = review.get("findings")
    dispositions = review.get("dispositions")
    if not isinstance(findings, list) or not isinstance(dispositions, list):
        raise ReasoningContractError("reasoning_review_invalid")

    finding_ids: list[str] = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"finding_id", "category", "message"}
            or not isinstance(finding.get("finding_id"), str)
            or not finding["finding_id"]
            or finding.get("category") not in REASONING_REVIEW_CATEGORIES
            or not isinstance(finding.get("message"), str)
            or not finding["message"]
        ):
            raise ReasoningContractError("reasoning_review_finding_invalid")
        finding_ids.append(finding["finding_id"])
    if len(finding_ids) != len(set(finding_ids)):
        raise ReasoningContractError("reasoning_review_finding_invalid")

    disposition_ids: list[str] = []
    revised = False
    for disposition in dispositions:
        if (
            not isinstance(disposition, dict)
            or set(disposition) != {"finding_id", "action", "rationale"}
            or not isinstance(disposition.get("finding_id"), str)
            or disposition.get("action") not in REASONING_REVIEW_ACTIONS
            or not isinstance(disposition.get("rationale"), str)
            or not disposition["rationale"]
        ):
            raise ReasoningContractError("reasoning_review_disposition_invalid")
        disposition_ids.append(disposition["finding_id"])
        revised = revised or disposition["action"] == "revised"
    if disposition_ids != finding_ids:
        raise ReasoningContractError("reasoning_review_disposition_invalid")
    if (reviewed_draft_hash != final_output_hash) != revised:
        raise ReasoningContractError("reasoning_review_revision_invalid")


def _validate_request(request: ReasoningSkillRequest) -> None:
    for field in (
        "stage_request_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "cycle_ref",
        "question_ref",
        "quest_ref",
        "goal_revision_ref",
        "context_pack_ref",
        "root_session_ref",
    ):
        value = getattr(request, field)
        if not isinstance(value, str) or not value.strip():
            raise ReasoningContractError("reasoning_skill_request_invalid")
    if type(request.foreground_epoch) is not int or request.foreground_epoch < 1:
        raise ReasoningContractError("reasoning_skill_request_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise ReasoningContractError("reasoning_context_pack_hash_mismatch")
    if request.context_pack.get("schema_ref") != (
        "meta-research/reasoning-context-pack/v1"
    ):
        raise ReasoningContractError("reasoning_context_pack_invalid")
    binding = request.context_pack.get("accepted_question_binding")
    research_context = request.context_pack.get("research_context")
    if (
        not isinstance(binding, dict)
        or binding.get("question_ref") != request.question_ref
        or binding.get("quest_ref") != request.quest_ref
        or request.context_pack.get("cycle_ref") != request.cycle_ref
        or request.context_pack.get("foreground_epoch") != request.foreground_epoch
        or not isinstance(research_context, dict)
        or research_context.get("cycle_ref") != request.cycle_ref
        or research_context.get("question_ref") != request.question_ref
        or research_context.get("quest_ref") != request.quest_ref
    ):
        raise ReasoningContractError("reasoning_context_pack_binding_mismatch")
    if not isinstance(request.frozen_evidence_closure, tuple):
        raise ReasoningContractError("reasoning_evidence_closure_invalid")
    reused_leaves = plan_evidence_reuse_leaves(request.context_pack)
    if any(
        leaf not in request.frozen_evidence_closure
        for leaf in reused_leaves
    ):
        raise ReasoningContractError("reasoning_plan_evidence_closure_invalid")

    # The contract validates the frozen closure while checking an outcome.  A
    # deliberately non-authoritative insufficient-evidence probe lets the
    # adapter reject a malformed input closure before launching a provider.
    validate_scientific_outcome(
        {
            "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
            "kind": "ScientificOutcomeCandidate",
            "outcome_ref": "reasoning-closure-validation-probe",
            "stage_run_request_ref": request.stage_request_ref,
            "cycle_ref": request.cycle_ref,
            "question_ref": request.question_ref,
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "foreground_epoch": request.foreground_epoch,
            "disposition": "insufficient_evidence",
            "claim": None,
            "evidence": [],
            "missing_evidence": ["closure-validation-probe"],
            "uncertainty_basis": [],
            "support_scope": ["Frozen accepted Question and Quest context."],
            "limitations": ["This probe makes no scientific claim."],
            "causal_interpretation": {
                **cast(dict[str, object], research_context["causal_context"]),
                "attribution_basis_refs": [],
                "claim_scope": "No causal claim.",
                "statement": "No causal interpretation is made.",
                "sufficiency_rationale": "This is an input validation probe.",
                "confounders": [],
            },
            "research_synthesis": _research_synthesis_probe(
                request, research_context
            ),
            "is_authoritative": False,
        },
        frozen_evidence_closure=list(request.frozen_evidence_closure),
        frozen_research_context=research_context,
    )


def _research_synthesis_probe(
    request: ReasoningSkillRequest,
    research_context: dict[str, object],
) -> dict[str, object]:
    graph = research_context.get("graph_binding")
    if not isinstance(graph, dict):
        raise ReasoningContractError("reasoning_research_context_invalid")
    prior = graph.get("prior_current_question_outcomes")
    parents = graph.get("parent_question_bindings")
    if not isinstance(prior, list) or not isinstance(parents, list):
        raise ReasoningContractError("reasoning_research_context_invalid")
    return {
        "cycle": {
            "cycle_ref": request.cycle_ref,
            "impact": "Input validation probe only.",
        },
        "current_question": {
            "question_ref": request.question_ref,
            "prior_accepted_outcome_refs": [
                value.get("outcome_ref") for value in prior if isinstance(value, dict)
            ],
            "progress": "Input validation probe only.",
        },
        "parent_questions": [
            {
                "question_ref": value.get("question_ref"),
                "impact": "unknown",
                "statement": "Input validation probe only.",
            }
            for value in parents
            if isinstance(value, dict)
        ],
        "quest": {
            "quest_ref": request.quest_ref,
            "goal_revision_ref": request.goal_revision_ref,
            "graph_revision_ref": graph.get("graph_revision_ref"),
            "impact": "Input validation probe only.",
        },
    }


def _validate_output_bindings(
    request: ReasoningSkillRequest,
    output: dict[str, object],
) -> None:
    outcome = output.get("scientific_outcome")
    if not isinstance(outcome, dict) or any(
        outcome.get(field) != expected
        for field, expected in (
            ("stage_run_request_ref", request.stage_request_ref),
            ("cycle_ref", request.cycle_ref),
            ("question_ref", request.question_ref),
            ("quest_ref", request.quest_ref),
            ("goal_revision_ref", request.goal_revision_ref),
            ("foreground_epoch", request.foreground_epoch),
        )
    ):
        raise ReasoningContractError("reasoning_skill_output_binding_mismatch")


def _completion_basis_for_output(
    request: ReasoningSkillRequest,
    output: dict[str, object],
) -> tuple[str, ...] | None:
    if output.get("candidate_completion") is None:
        return None
    return completion_milestone_basis_refs(request.context_pack)


def _validate_result_identity(
    request: ReasoningSkillRequest,
    *,
    primary_session_ref: str,
    reviewer_agent_ref: str | None,
    adapter_kind: str,
) -> None:
    if (
        not isinstance(primary_session_ref, str)
        or not primary_session_ref
        or not isinstance(adapter_kind, str)
        or not adapter_kind
        or primary_session_ref == request.root_session_ref
        or request.native_session_ref is not None
        and primary_session_ref != request.native_session_ref
        or reviewer_agent_ref is not None
        and (
            not reviewer_agent_ref
            or reviewer_agent_ref
            in {request.root_session_ref, primary_session_ref}
        )
    ):
        raise ReasoningContractError("reasoning_skill_session_invalid")


class CodexReasoningSkillAdapter(CodexPlanSkillAdapter):
    """Production Reasoning adapter using one managed native root Session."""

    _provider_transport_limits = REASONING_PROVIDER_TRANSPORT_LIMITS

    def _is_reconciliation_operation_name(self, operation_name: str) -> bool:
        return operation_name == "autonomous-resume" or (
            super()._is_reconciliation_operation_name(operation_name)
        )

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "primary":
            return "reasoning_primary_result_contract_invalid"
        if operation_name in {"review", "autonomous-resume"}:
            return "reasoning_review_result_contract_invalid"
        raise ReasoningSkillUnavailable("codex_operation_spool_invalid")

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        timeout_seconds: float = 15 * 60,
        process_runner: Callable[
            [list[str], str, float], subprocess.CompletedProcess[str]
        ]
        | None = None,
    ) -> None:
        super().__init__(
            workspace,
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
        )
        self._full_conformance_authority: (
            ReasoningFullConformanceAuthority | None
        ) = None
        self._resident_mcp_base_url: str | None = None
        self._resident_mcp_channels: dict[
            tuple[str, str], ResidentMcpChannel
        ] = {}

    def bind_full_conformance_authority(
        self, authority: ReasoningFullConformanceAuthority
    ) -> None:
        if (
            self._full_conformance_authority is not None
            and self._full_conformance_authority is not authority
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_harness_conformance_authority_conflict"
            )
        self._full_conformance_authority = authority

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        if not isinstance(base_url, str):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_endpoint_invalid"
            )
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_endpoint_invalid"
            )
        normalized = base_url.rstrip("/")
        if (
            self._resident_mcp_base_url is not None
            and self._resident_mcp_base_url != normalized
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_endpoint_conflict"
            )
        self._resident_mcp_base_url = normalized

    def _reasoning_semantic_operation_ids(self) -> tuple[str, ...]:
        try:
            from meta_research.semantic_owner_gateway import (
                REASONING_ROOT_SEMANTIC_OPERATION_IDS as registered_ids,
            )
        except (ImportError, AttributeError) as error:
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_catalog_unavailable"
            ) from error
        operation_ids = tuple(registered_ids)
        if operation_ids != REASONING_ROOT_SEMANTIC_OPERATION_IDS:
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_catalog_unavailable"
            )
        return operation_ids

    def _release_resident_channel(self, key: tuple[str, str]) -> None:
        channel = self._resident_mcp_channels.pop(key, None)
        authority = self._full_conformance_authority
        if channel is None or authority is None:
            return
        try:
            authority.revoke_resident_mcp_channel(channel)
        except HarnessAdmissionError as error:
            raise ReasoningSkillUnavailable(error.code) from error

    def _invoke_with_resident_mcp(
        self,
        *,
        request: ReasoningSkillRequest,
        operation_name: str,
        prompt: str,
        schema: dict[str, object],
        native_session_ref: str | None,
    ) -> tuple[dict[str, object], str | None, str]:
        authority = self._full_conformance_authority
        base_url = self._resident_mcp_base_url
        if authority is None or base_url is None:
            raise ReasoningSkillUnavailable("reasoning_semantic_mcp_unavailable")
        operation_ids = self._reasoning_semantic_operation_ids()
        try:
            conformance = authority.require_full_conformance_binding()
        except HarnessAdmissionError as error:
            raise ReasoningSkillUnavailable(error.code) from error
        if not set(operation_ids) <= set(conformance.required_operation_ids):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_conformance_incomplete"
            )
        channel_key = (request.job_ref or request.run_ref, operation_name)
        channel = self._resident_mcp_channels.get(channel_key)
        if channel is None:
            try:
                channel = authority.issue_resident_mcp_channel(
                    run_ref=request.run_ref,
                    attempt_ref=request.attempt_ref,
                    root_session_ref=request.root_session_ref,
                    fence_ref=request.fence_ref,
                    capability_binding_hash=canonical_hash(
                        request.runtime_binding.as_dict()
                    ),
                    operation_ids=operation_ids,
                )
            except HarnessAdmissionError as error:
                raise ReasoningSkillUnavailable(error.code) from error
            self._resident_mcp_channels[channel_key] = channel

        try:
            _validate_reasoning_resident_channel(channel, operation_ids)
            endpoint = urlsplit(channel.binding.endpoint_ref)
            if (
                endpoint.scheme
                or endpoint.netloc
                or endpoint.query
                or endpoint.fragment
                or not endpoint.path.startswith("/")
                or endpoint.path.startswith("//")
            ):
                raise ReasoningSkillUnavailable(
                    "reasoning_semantic_mcp_endpoint_invalid"
                )
            scope_binding_hash = canonical_hash(
                {
                    "catalog_hash": channel.binding.catalog_hash,
                    "operation_bindings": list(
                        channel.binding.operation_bindings
                    ),
                }
            )
            result = self._invoke(
                operation_name=operation_name,
                prompt=prompt,
                schema=schema,
                native_session_ref=native_session_ref,
                job_ref=request.job_ref,
                mcp_url=base_url + endpoint.path,
                mcp_token=channel.connection.token,
                mcp_scope_binding_hash=scope_binding_hash,
            )
            try:
                _verify_reasoning_semantic_trace(result[2], operation_ids)
            except (
                ReasoningSkillContractError,
                ReasoningSkillUnavailable,
            ) as error:
                detail_code = (
                    error.code
                    if isinstance(error, ReasoningSkillUnavailable)
                    else str(error)
                )
                raise self._sealed_result_failure(
                    job_ref=request.job_ref,
                    operation_name=operation_name,
                    native_session_ref=cast(str, result[1]),
                    failure_code=self._transport_contract_failure_code(
                        operation_name
                    ),
                    detail_code=detail_code,
                ) from error
        except ReasoningSkillUnavailable as error:
            if error.code != "codex_operation_reconciliation_pending":
                self._release_resident_channel(channel_key)
            raise
        except Exception:
            self._release_resident_channel(channel_key)
            raise
        self._release_resident_channel(channel_key)
        return result

    def request_stop(self) -> None:
        try:
            super().request_stop()
        finally:
            for key in tuple(self._resident_mcp_channels):
                self._release_resident_channel(key)

    def finish_job(self, job_ref: str) -> None:
        try:
            super().finish_job(job_ref)
        finally:
            for key in tuple(self._resident_mcp_channels):
                if key[0] == job_ref:
                    self._release_resident_channel(key)

    def reconcile_cancelled_job(self, job_ref: str) -> bool:
        reconciled = super().reconcile_cancelled_job(job_ref)
        if reconciled:
            for key in tuple(self._resident_mcp_channels):
                if key[0] == job_ref:
                    self._release_resident_channel(key)
        return reconciled

    def runtime_binding(self) -> ReasoningRuntimeBinding:
        resources = _reasoning_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        shared_adapter_source_hash = _shared_codex_adapter_source_hash()
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        template = _schema_template_request()
        schemas = {
            "reasoning-primary-output": _reasoning_primary_output_schema(template),
            "reasoning-review": _reasoning_review_response_schema(
                template, _reasoning_stage_output_schema(template)
            ),
            "reasoning-autonomous-review": _reasoning_review_response_schema(
                template, _reasoning_autonomous_checkpoint_schema(template)
            ),
        }
        schemas = {
            name: _compile_codex_output_schema(schema)
            for name, schema in schemas.items()
        }
        binding = ReasoningRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _reasoning_skill_instructions(),
                    "adapter_source_hash": adapter_source_hash,
                    "shared_adapter_source_hash": shared_adapter_source_hash,
                    "supervisor_source_hash": supervisor_source_hash,
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=(),
            capability_bindings=(
                "approval-policy-never",
                "filesystem-danger-full-access",
                "global-config-ignored",
                "harness-child-agent-review",
                "semantic-mcp-resident",
                "native-session-resume",
                "shell-tool-enabled",
                "structured-output-json-schema",
                "trusted-local-quest-authorization",
                "web-search-live",
            ),
            resource_bindings=tuple(
                "package:meta_research.skills.reasoning_stage/"
                f"{name}@sha256:{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in schemas.items()
            )
            + harness_artifacts
            + (
                "adapter-source:meta_research.reasoning_skill@sha256:"
                f"{adapter_source_hash}",
                "adapter-source:meta_research.idea_skill@sha256:"
                f"{shared_adapter_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:"
                + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                CODEX_REASONING_EFFORT_BINDING,
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"prompt={REASONING_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes};"
                f"stream={REASONING_PROVIDER_TRANSPORT_LIMITS.stream_max_bytes};"
                f"result={REASONING_PROVIDER_TRANSPORT_LIMITS.result_max_bytes}",
                "provider-timeout-seconds:"
                + format(self._timeout_seconds, ".17g"),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:"
                + transport_key_hash(transport_key),
            ),
        )
        authority = self._full_conformance_authority
        if authority is None:
            return binding
        try:
            conformance = authority.require_full_conformance_binding()
        except HarnessAdmissionError as error:
            raise ReasoningSkillUnavailable(error.code) from error
        operation_ids = self._reasoning_semantic_operation_ids()
        if not set(operation_ids) <= set(conformance.required_operation_ids):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_conformance_incomplete"
            )
        return bind_reasoning_runtime_to_full_conformance(binding, conformance)

    def generate_draft(
        self, request: ReasoningSkillRequest
    ) -> ReasoningSkillDraft:
        _validate_request(request)
        if request.runtime_binding != self.runtime_binding():
            raise ReasoningSkillUnavailable("reasoning_runtime_binding_drift")
        prompt = (
            f"{_reasoning_skill_instructions()}\n\n"
            "本回合仅执行 Primary draft phase。禁止调用 spawn_agent 或 wait，禁止委派 "
            "child、独立评审或预先处理 review；必须先返回 frozen draft。独立评审只能在 "
            "Owner 记录该 draft 后的下一次 resumed review turn 中进行。"
            "你是当前 Reasoning 根 Agent，而不是 State Owner。先通过 resident "
            "Semantic MCP 严格按顺序调用 advancement_engine.reasoning_stage_run.observe、"
            "research_memory.reasoning_evidence.read、research_graph.reasoning_context.read，"
            "并核对 exact request/run/attempt/fence、Foreground epoch、Question/Quest/Goal "
            "binding 与冻结 evidence closure；任何 missing、stale、unknown 或不一致都停止，"
            "不得输出看似成功的候选。若目标是已存在且可选择的 Question，或提出完成，"
            "返回闭合 reasoning-stage-output：ScientificOutcomeCandidate 必须是 "
            "affirmed | denied | uncertain | insufficient_evidence 之一，并且恰有一个 "
            "NextCycleProposal | CandidateCompletion。若科学上必须新建或分解 Question，"
            "改为返回非终态 reasoning-autonomous-checkpoint：同一 scientific_outcome 加"
            "一个 internal AutonomousQuestionScope；它只包含 reviewed question blueprint、"
            "mode、entry stage 与 typed skip basis，绝不能伪装成 outward transition。"
            "NextCycleProposal 必须闭合 entry_stage 与 exact typed skip basis；"
            "source-current root/manual Question 的稳定 anchor ref 是其 question_ref，"
            "但 present/open 仍由 RG 独立接纳。ScientificOutcome 不得冒充 IdeaSet 或 "
            "FormalPlan skip basis。不得自称接纳内容、领域语义、创建 Question/Cycle、结束 "
            "Quest 或形成 StageCommit。Log/Analysis/Checkpoint 只能以 finding=context "
            "解释、限制、溯源或复现；affirmed/denied/uncertain 仍必须至少引用一项 "
            "LiteratureRecord 或 MetricResult，诊断资产绝不能单独满足 substantive gate。"
            "Provider 是此 managed root Session 内的研究执行角色，不得创建第二个顶层 "
            "supervisor 或 Session。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"run_ref={request.run_ref}\n"
            f"attempt_ref={request.attempt_ref}\n"
            f"fence_ref={request.fence_ref}\n"
            f"cycle_ref={request.cycle_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"quest_ref={request.quest_ref}\n"
            f"goal_revision_ref={request.goal_revision_ref}\n"
            f"foreground_epoch={request.foreground_epoch}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"context_pack={canonical_json(request.context_pack)}\n"
            "frozen_evidence_closure="
            f"{canonical_json(list(request.frozen_evidence_closure))}"
        )
        output, session_ref, primary_stdout = self._invoke_with_resident_mcp(
            request=request,
            operation_name="primary",
            prompt=prompt,
            schema=_reasoning_primary_output_schema(request),
            native_session_ref=request.native_session_ref,
        )
        if session_ref is None or not isinstance(output, dict):
            raise ReasoningSkillUnavailable("codex_reasoning_primary_invalid")
        try:
            _verify_primary_phase_trace(primary_stdout)
        except ReasoningSkillUnavailable as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="primary",
                native_session_ref=session_ref,
                failure_code=error.code,
            ) from error
        draft = ReasoningSkillDraft(
            draft=output,
            primary_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        try:
            validate_reasoning_skill_draft(request, draft)
        except ReasoningContractError as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="primary",
                native_session_ref=session_ref,
                failure_code="reasoning_primary_result_contract_invalid",
                detail_code=str(error.args[0]),
            ) from error
        return draft

    def review_draft(
        self,
        request: ReasoningSkillRequest,
        draft: ReasoningSkillDraft,
    ) -> ReasoningSkillResult | ReasoningAutonomousCheckpointResult:
        _validate_request(request)
        if (
            request.runtime_binding != self.runtime_binding()
            or request.native_session_ref != draft.primary_session_ref
        ):
            raise ReasoningSkillUnavailable("reasoning_runtime_binding_drift")
        try:
            validate_reasoning_skill_draft(request, draft)
        except ReasoningContractError as error:
            raise ReasoningSkillUnavailable(error.args[0]) from error
        prompt = (
            f"{_reasoning_skill_instructions()}\n\n"
            "本回合是 Review phase。必须针对下方当前 frozen reviewed_draft 现在新建一个 "
            "child reviewer；不得复用 Primary phase、任何先前 child 或先前评审结论。"
            "你仍是同一个 Reasoning 根 Agent。先再次通过三项 resident Semantic MCP "
            "操作核对 current request/fence/epoch 与冻结 evidence/context。然后必须在当前 "
            "managed native Session 内使用 Harness 原生 spawn_agent，以 "
            'fork_turns="none" 启动一个全新上下文的短命 child reviewer，并 wait 到完成；'
            "不得创建第二个顶层 supervisor 或 Session。child 只审查 source binding、"
            "evidence role/disposition 边界、transition 或 internal autonomous scope 边界与 "
            "Owner 权限，不批准结论。"
            "根 Agent 对每条 finding 给出 revised | not_adopted；revised 必须实际改变 "
            "output。只返回 schema_ref、reviewer_agent_ref、findings、final_output、"
            "dispositions。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"frozen_evidence_closure={canonical_json(list(request.frozen_evidence_closure))}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        reviewed, resumed_session, stdout = self._invoke_with_resident_mcp(
            request=request,
            operation_name="review",
            prompt=prompt,
            schema=_reasoning_review_response_schema(
                request,
                (
                    _reasoning_autonomous_checkpoint_schema(request)
                    if draft.draft.get("schema_ref")
                    == REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
                    else _reasoning_stage_output_schema(request)
                ),
            ),
            native_session_ref=draft.primary_session_ref,
        )
        if (
            resumed_session != draft.primary_session_ref
            or set(reviewed)
            != {
                "schema_ref",
                "reviewer_agent_ref",
                "findings",
                "final_output",
                "dispositions",
            }
            or reviewed.get("schema_ref") != REASONING_REVIEW_SCHEMA_REF
            or not isinstance(reviewed.get("reviewer_agent_ref"), str)
            or not isinstance(reviewed.get("findings"), list)
            or not isinstance(reviewed.get("final_output"), dict)
            or not isinstance(reviewed.get("dispositions"), list)
        ):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="review",
                native_session_ref=draft.primary_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code="codex_reasoning_review_invalid",
            )
        reviewer_agent_ref = cast(str, reviewed["reviewer_agent_ref"])
        try:
            _verify_child_review_trace(
                stdout,
                root_session_ref=draft.primary_session_ref,
                reviewer_agent_ref=reviewer_agent_ref,
            )
        except ReasoningSkillUnavailable as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="review",
                native_session_ref=draft.primary_session_ref,
                failure_code=error.code,
            ) from error
        final_output = cast(dict[str, object], reviewed["final_output"])
        if (
            draft.draft.get("schema_ref")
            == REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
        ):
            checkpoint_result = ReasoningAutonomousCheckpointResult(
                primary_draft=draft.draft,
                reviewed_checkpoint=final_output,
                findings=tuple(
                    cast(dict[str, str], item)
                    for item in cast(list[object], reviewed["findings"])
                ),
                dispositions=tuple(
                    cast(dict[str, str], item)
                    for item in cast(list[object], reviewed["dispositions"])
                ),
                primary_session_ref=draft.primary_session_ref,
                review_mode="harness_child_agent",
                reviewer_agent_ref=reviewer_agent_ref,
                adapter_kind=draft.adapter_kind,
            )
            try:
                validate_reasoning_autonomous_checkpoint_result(
                    request, draft, checkpoint_result
                )
            except ReasoningContractError as error:
                raise self._sealed_result_failure(
                    job_ref=request.job_ref,
                    operation_name="review",
                    native_session_ref=draft.primary_session_ref,
                    failure_code="reasoning_review_result_contract_invalid",
                    detail_code=str(error.args[0]),
                ) from error
            return checkpoint_result
        scientific_outcome = final_output.get("scientific_outcome")
        next_cycle = final_output.get("next_cycle_proposal")
        completion = final_output.get("candidate_completion")
        if (
            not isinstance(scientific_outcome, dict)
            or next_cycle is not None
            and not isinstance(next_cycle, dict)
            or completion is not None
            and not isinstance(completion, dict)
        ):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="review",
                native_session_ref=draft.primary_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code="codex_reasoning_review_invalid",
            )
        result = ReasoningSkillResult(
            reviewed_draft=draft.draft,
            scientific_outcome=scientific_outcome,
            next_cycle_proposal=cast(dict[str, object] | None, next_cycle),
            candidate_completion=cast(dict[str, object] | None, completion),
            findings=tuple(
                cast(dict[str, str], item)
                for item in cast(list[object], reviewed["findings"])
            ),
            dispositions=tuple(
                cast(dict[str, str], item)
                for item in cast(list[object], reviewed["dispositions"])
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer_agent_ref,
            adapter_kind=draft.adapter_kind,
        )
        try:
            validate_reasoning_skill_result(request, result)
        except ReasoningContractError as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="review",
                native_session_ref=draft.primary_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code=str(error.args[0]),
            ) from error
        return result

    def resume_after_autonomous_creation(
        self,
        request: ReasoningSkillRequest,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> ReasoningSkillResult:
        """Resume the same native Session after create_question is selectable."""

        _validate_request(request)
        if request.runtime_binding != self.runtime_binding():
            raise ReasoningSkillUnavailable("reasoning_runtime_binding_drift")
        try:
            validate_reasoning_autonomous_checkpoint(
                checkpoint,
                frozen_evidence_closure=list(request.frozen_evidence_closure),
                frozen_research_context=cast(
                    dict[str, object], request.context_pack["research_context"]
                ),
            )
        except ReasoningContractError as error:
            raise ReasoningSkillUnavailable(error.args[0]) from error
        if request.native_session_ref is None:
            raise ReasoningSkillUnavailable("reasoning_native_session_missing")
        prompt = (
            f"{_reasoning_skill_instructions()}\n\n"
            "本回合是 Review phase。必须针对下方当前 frozen checkpoint 与当前 creation "
            "result 现在新建一个 child reviewer；不得复用 Primary phase、任何先前 child "
            "或先前评审结论。"
            "继续同一 Reasoning root/native Session。create_question 已通过五 Owner 公共 "
            "seam 返回 RG accepted QuestionAnchor、同一 graph revision 的 present/open facts "
            "以及真实 QuestionLiteratureRevision。先按既定顺序重查三项 resident Semantic "
            "MCP currentness/evidence/context，再用 fresh-context child reviewer 审查 exact "
            "checkpoint 与 creation result。ScientificOutcome 必须逐字节复用 checkpoint；"
            "只形成一个指向 accepted Anchor 的 NextCycleProposal，不创建 Cycle、不形成 "
            "StageCommit。reviewed_draft 必须视为 checkpoint；根 Agent逐条 disposition。\n"
            f"checkpoint={canonical_json(checkpoint)}\n"
            f"creation_result={canonical_json(creation_result)}"
        )
        reviewed, resumed_session, stdout = self._invoke_with_resident_mcp(
            request=request,
            operation_name="autonomous-resume",
            prompt=prompt,
            schema=_reasoning_review_response_schema(
                request, _reasoning_stage_output_schema(request)
            ),
            native_session_ref=request.native_session_ref,
        )
        if (
            resumed_session != request.native_session_ref
            or set(reviewed)
            != {
                "schema_ref",
                "reviewer_agent_ref",
                "findings",
                "final_output",
                "dispositions",
            }
            or reviewed.get("schema_ref") != REASONING_REVIEW_SCHEMA_REF
            or not isinstance(reviewed.get("reviewer_agent_ref"), str)
            or not isinstance(reviewed.get("findings"), list)
            or not isinstance(reviewed.get("final_output"), dict)
            or not isinstance(reviewed.get("dispositions"), list)
        ):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="autonomous-resume",
                native_session_ref=request.native_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code="codex_reasoning_review_invalid",
            )
        reviewer_agent_ref = cast(str, reviewed["reviewer_agent_ref"])
        try:
            _verify_child_review_trace(
                stdout,
                root_session_ref=request.native_session_ref,
                reviewer_agent_ref=reviewer_agent_ref,
            )
        except ReasoningSkillUnavailable as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="autonomous-resume",
                native_session_ref=request.native_session_ref,
                failure_code=error.code,
            ) from error
        final_output = cast(dict[str, object], reviewed["final_output"])
        outcome = final_output.get("scientific_outcome")
        next_cycle = final_output.get("next_cycle_proposal")
        if not isinstance(outcome, dict) or not isinstance(next_cycle, dict):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="autonomous-resume",
                native_session_ref=request.native_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code="codex_reasoning_review_invalid",
            )
        result = ReasoningSkillResult(
            reviewed_draft=checkpoint,
            scientific_outcome=outcome,
            next_cycle_proposal=next_cycle,
            candidate_completion=None,
            findings=tuple(
                cast(dict[str, str], item)
                for item in cast(list[object], reviewed["findings"])
            ),
            dispositions=tuple(
                cast(dict[str, str], item)
                for item in cast(list[object], reviewed["dispositions"])
            ),
            primary_session_ref=request.native_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer_agent_ref,
            adapter_kind="codex_cli",
        )
        try:
            validate_reasoning_autonomous_resume_result(
                request, checkpoint, creation_result, result
            )
        except ReasoningContractError as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="autonomous-resume",
                native_session_ref=request.native_session_ref,
                failure_code="reasoning_review_result_contract_invalid",
                detail_code=str(error.args[0]),
            ) from error
        return result

    def execute(self, request: ReasoningSkillRequest) -> ReasoningSkillResult:
        draft = self.generate_draft(request)
        result = self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref),
            draft,
        )
        if isinstance(result, ReasoningAutonomousCheckpointResult):
            raise ReasoningSkillUnavailable("reasoning_autonomous_creation_pending")
        return result


def _validate_reasoning_resident_channel(
    channel: ResidentMcpChannel,
    operation_ids: tuple[str, ...],
) -> None:
    if channel.binding.connection_grant_ref != channel.connection.grant_ref:
        raise ReasoningSkillUnavailable("reasoning_semantic_mcp_scope_invalid")
    bindings = channel.binding.operation_bindings
    by_id: dict[str, dict[str, object]] = {}
    expected_fields = {
        "semantic_operation_id",
        "operation_contract_version",
        "owning_module",
        "access_mode",
        "input_schema_hash",
        "output_schema_hash",
        "reconciliation_operation_id",
        "discovered_tool_name",
    }
    for value in bindings:
        if (
            not isinstance(value, dict)
            or set(value) != expected_fields
            or not isinstance(value.get("semantic_operation_id"), str)
            or value.get("discovered_tool_name")
            != value.get("semantic_operation_id")
            or value.get("access_mode")
            not in {"read", "verify", "effect", "reconcile"}
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_operation_binding_invalid"
            )
        by_id[cast(str, value["semantic_operation_id"])] = value
    if [value.get("semantic_operation_id") for value in bindings] != list(
        operation_ids
    ) or len(by_id) != len(operation_ids):
        if _REASONING_CURRENTNESS_OPERATION_ID not in by_id:
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_currentness_unavailable"
            )
        raise ReasoningSkillUnavailable(
            "reasoning_semantic_mcp_operation_binding_invalid"
        )
    currentness = by_id.get(_REASONING_CURRENTNESS_OPERATION_ID)
    if currentness is None or currentness.get("access_mode") not in {
        "read",
        "verify",
    }:
        raise ReasoningSkillUnavailable(
            "reasoning_semantic_mcp_currentness_unavailable"
        )
    for operation_id, binding in by_id.items():
        if binding.get("access_mode") != "effect":
            continue
        reconciliation_id = binding.get("reconciliation_operation_id")
        reconciliation = (
            by_id.get(reconciliation_id)
            if isinstance(reconciliation_id, str)
            else None
        )
        if (
            reconciliation is None
            or reconciliation.get("access_mode") != "reconcile"
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_reconciliation_unavailable"
            )
        if operation_id == reconciliation_id:
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_reconciliation_unavailable"
            )


def _verify_reasoning_semantic_trace(
    stdout: str,
    operation_ids: tuple[str, ...],
) -> None:
    observed: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "mcp_tool_call"
            or item.get("server") != "meta_research"
        ):
            continue
        tool = item.get("tool")
        result = item.get("result")
        if (
            not isinstance(tool, str)
            or item.get("status") not in {None, "completed", "succeeded"}
            or isinstance(result, dict)
            and (result.get("isError") is True or result.get("is_error") is True)
        ):
            raise ReasoningSkillUnavailable(
                "reasoning_semantic_mcp_observation_failed"
            )
        observed.append(tool)
    if not observed or observed[0] != _REASONING_CURRENTNESS_OPERATION_ID:
        raise ReasoningSkillUnavailable(
            "reasoning_semantic_mcp_currentness_unobserved"
        )
    if not set(operation_ids) <= set(observed):
        raise ReasoningSkillUnavailable(
            "reasoning_semantic_mcp_operation_unobserved"
        )
    first_observations = tuple(dict.fromkeys(observed))
    if first_observations != operation_ids:
        raise ReasoningSkillUnavailable(
            "reasoning_semantic_mcp_observation_order_invalid"
        )


def _reasoning_skill_resources() -> dict[str, str]:
    package = files("meta_research.skills.reasoning_stage")
    resources = (
        ("SKILL.md", package / "SKILL.md"),
        ("agents/openai.yaml", package / "agents" / "openai.yaml"),
        ("references/contract.md", package / "references" / "contract.md"),
        (
            "references/owner-operations.md",
            package / "references" / "owner-operations.md",
        ),
    )
    try:
        return {
            name: resource.read_text(encoding="utf-8")
            for name, resource in resources
        }
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise ReasoningSkillUnavailable(
            "reasoning_skill_resource_unavailable"
        ) from error


def _reasoning_skill_instructions() -> str:
    resources = _reasoning_skill_resources()
    return "\n\n".join(
        f"<!-- bundled resource: {name} -->\n{resources[name]}"
        for name in (
            "SKILL.md",
            "references/contract.md",
            "references/owner-operations.md",
        )
    )


def _schema_template_request() -> ReasoningSkillRequest:
    context_pack: dict[str, object] = {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": "__cycle_ref__",
        "foreground_epoch": 1,
        "accepted_question_binding": {
            "question_ref": "__question_ref__",
            "quest_ref": "__quest_ref__",
        },
        "upstream_stage_closure": [
            {"stage": "idea", "commit_ref": "__idea_stage_commit_ref__"},
            {"stage": "plan", "commit_ref": "__plan_stage_commit_ref__"},
            {"stage": "bundle", "commit_ref": "__bundle_stage_commit_ref__"},
        ],
        "research_context": {
            "schema_ref": "meta-research/reasoning-research-context/v2",
            "cycle_ref": "__cycle_ref__",
            "question_ref": "__question_ref__",
            "quest_ref": "__quest_ref__",
            "goal_revision_ref": "__goal_revision_ref__",
            "quest_goal_revision": {
                "kind": "QuestGoalRevision",
                "quest_ref": "__quest_ref__",
                "goal_revision_ref": "__goal_revision_ref__",
            },
            "graph_binding": {
                "schema_ref": "meta-research/reasoning-graph-context/v1",
                "issuer": "research_graph",
                "quest_ref": "__quest_ref__",
                "question_ref": "__question_ref__",
                "graph_revision_ref": "__graph_revision_ref__",
                "active_question_refs": ["__question_ref__"],
                "parent_question_bindings": [],
                "prior_current_question_outcomes": [],
                "binding_ref": "__reasoning_graph_context_ref__",
                "binding_hash": "0" * 64,
            },
            "causal_context": {
                "target_commit_refs": [],
                "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [],
                "provenance_refs": [],
            },
            "upstream_stage_commit_refs": [
                "__idea_stage_commit_ref__",
                "__plan_stage_commit_ref__",
                "__bundle_stage_commit_ref__",
            ],
        },
    }
    return ReasoningSkillRequest(
        stage_request_ref="__stage_request_ref__",
        run_ref="__run_ref__",
        attempt_ref="__attempt_ref__",
        fence_ref="__fence_ref__",
        cycle_ref="__cycle_ref__",
        question_ref="__question_ref__",
        quest_ref="__quest_ref__",
        goal_revision_ref="__goal_revision_ref__",
        foreground_epoch=1,
        context_pack_ref="__context_pack_ref__",
        context_pack_hash=canonical_hash(context_pack),
        context_pack=context_pack,
        frozen_evidence_closure=(),
        root_session_ref="__root_session_ref__",
        runtime_binding=ReasoningRuntimeBinding(
            packaged_skill_bundle_hash="0" * 64,
            instruction_set_hash="0" * 64,
            model_ref="__model_ref__",
            harness_adapter_ref="__harness_adapter_ref__",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        ),
    )


def _reasoning_stage_output_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    next_cycle = _next_cycle_proposal_schema(request)
    completion = _candidate_completion_schema(request)
    base_properties = {
        "schema_ref": {"const": REASONING_STAGE_OUTPUT_SCHEMA_REF},
        "scientific_outcome": _scientific_outcome_schema(request),
    }
    return {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **base_properties,
                    "next_cycle_proposal": next_cycle,
                    "candidate_completion": {"type": "null"},
                },
                "required": [
                    *base_properties,
                    "next_cycle_proposal",
                    "candidate_completion",
                ],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **base_properties,
                    "next_cycle_proposal": {"type": "null"},
                    "candidate_completion": completion,
                },
                "required": [
                    *base_properties,
                    "next_cycle_proposal",
                    "candidate_completion",
                ],
            },
        ]
    }


def _reasoning_primary_output_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    closed = _reasoning_stage_output_schema(request)
    autonomous = _reasoning_autonomous_checkpoint_schema(request)
    closed_variants = cast(list[dict[str, object]], closed["anyOf"])
    return {
        "oneOf": [
            *closed_variants,
            autonomous,
        ]
    }


def _reasoning_autonomous_checkpoint_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    question_properties = {
        field: text
        for field in (
            "title",
            "unknown_statement",
            "answer_shape",
            "applicability_scope",
            "background_context",
            "requirements_constraints",
        )
    }
    source = _reasoning_source_properties(request)
    scope_base_properties = {
        "schema_ref": {"const": AUTONOMOUS_QUESTION_SCOPE_SCHEMA_REF},
        "kind": {"const": "AutonomousQuestionScope"},
        "creation_mode": {"const": "AutonomousCreation"},
        "mode": {"type": "string", "enum": ["new", "decompose"]},
        **source,
        "question_blueprint": {
            "type": "object",
            "additionalProperties": False,
            "properties": question_properties,
            "required": list(question_properties),
        },
        "parent_question_ref": {"anyOf": [text, {"type": "null"}]},
        "decomposition_basis_refs": {
            "type": "array",
            "uniqueItems": True,
            "items": text,
        },
        "is_authoritative": {"const": False},
    }
    scope_variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **scope_base_properties,
                "entry_stage": {"const": entry_stage},
                "typed_skip_basis_refs_by_stage": _typed_skip_basis_schema(
                    entry_stage, text
                ),
            },
            "required": [
                *scope_base_properties,
                "entry_stage",
                "typed_skip_basis_refs_by_stage",
            ],
        }
        for entry_stage in ("idea", "plan", "bundle", "reasoning")
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {
                "const": REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
            },
            "scientific_outcome": _scientific_outcome_schema(request),
            "autonomous_scope": {"anyOf": scope_variants},
        },
        "required": ["schema_ref", "scientific_outcome", "autonomous_scope"],
    }


def _scientific_outcome_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    # Frozen-closure membership, kind/ref pairing, and the diagnostic-only
    # ``context`` finding are all checked by ``validate_scientific_outcome``.
    # Keep the provider projection fixed-size so a valid large closure cannot
    # overflow Structured Outputs' aggregate enum/property limits.
    citation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": sorted(
                    _SCIENTIFIC_EVIDENCE_KINDS
                    | _DIAGNOSTIC_EVIDENCE_KINDS
                ),
            },
            "ref": text,
            "finding": {
                "type": "string",
                "enum": [
                    "supporting",
                    "negative",
                    "partial",
                    "context",
                ],
            },
        },
        "required": ["kind", "ref", "finding"],
    }
    research_context = cast(
        dict[str, object], request.context_pack["research_context"]
    )
    graph = cast(dict[str, object], research_context["graph_binding"])
    parent_bindings = cast(
        list[dict[str, object]], graph["parent_question_bindings"]
    )
    prior_outcomes = cast(
        list[dict[str, object]], graph["prior_current_question_outcomes"]
    )
    parent_refs = [cast(str, value["question_ref"]) for value in parent_bindings]
    prior_refs = [cast(str, value["outcome_ref"]) for value in prior_outcomes]
    frozen_causal = cast(dict[str, object], research_context["causal_context"])
    text_array = {"type": "array", "items": text, "uniqueItems": True}
    causal_ref_arrays = {
        field: {
            "type": "array",
            "minItems": len(cast(list[object], frozen_causal[field])),
            "maxItems": len(cast(list[object], frozen_causal[field])),
            "items": text,
        }
        for field in (
            "target_commit_refs",
            "changed_axis_fact_refs",
            "held_fixed_fact_refs",
            "provenance_refs",
        )
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {
                "const": SCIENTIFIC_OUTCOME_SCHEMA_REF
            },
            "kind": {"const": "ScientificOutcomeCandidate"},
            "outcome_ref": text,
            "stage_run_request_ref": {"const": request.stage_request_ref},
            "cycle_ref": {"const": request.cycle_ref},
            "question_ref": {"const": request.question_ref},
            "quest_ref": {"const": request.quest_ref},
            "goal_revision_ref": {"const": request.goal_revision_ref},
            "foreground_epoch": {"const": request.foreground_epoch},
            "disposition": {
                "type": "string",
                "enum": [
                    "affirmed",
                    "denied",
                    "uncertain",
                    "insufficient_evidence",
                ],
            },
            "claim": {"anyOf": [text, {"type": "null"}]},
            "evidence": {
                "type": "array",
                "uniqueItems": True,
                "items": citation,
            },
            "missing_evidence": {"type": "array", "items": text},
            "uncertainty_basis": {"type": "array", "items": text},
            "support_scope": {
                "type": "array", "items": text, "minItems": 1, "uniqueItems": True
            },
            "limitations": text_array,
            "causal_interpretation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **causal_ref_arrays,
                    "attribution_basis_refs": {
                        "type": "array",
                        "items": text,
                        "uniqueItems": True,
                    },
                    "claim_scope": text,
                    "statement": text,
                    "sufficiency_rationale": text,
                    "confounders": text_array,
                },
                "required": [
                    "target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs",
                    "provenance_refs", "attribution_basis_refs", "claim_scope", "statement",
                    "sufficiency_rationale", "confounders",
                ],
            },
            "research_synthesis": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cycle": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"cycle_ref": {"const": request.cycle_ref}, "impact": text},
                        "required": ["cycle_ref", "impact"],
                    },
                    "current_question": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "question_ref": {"const": request.question_ref},
                            "prior_accepted_outcome_refs": {
                                "type": "array",
                                "items": text,
                                "minItems": len(prior_refs),
                                "maxItems": len(prior_refs),
                                "uniqueItems": True,
                            },
                            "progress": text,
                        },
                        "required": ["question_ref", "prior_accepted_outcome_refs", "progress"],
                    },
                    "parent_questions": {
                        "type": "array", "minItems": len(parent_refs), "maxItems": len(parent_refs),
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "question_ref": text,
                                "impact": {"type": "string", "enum": ["material", "no_material", "unknown"]},
                                "statement": text,
                            },
                            "required": ["question_ref", "impact", "statement"],
                        },
                    },
                    "quest": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "quest_ref": {"const": request.quest_ref},
                            "goal_revision_ref": {"const": request.goal_revision_ref},
                            "graph_revision_ref": {"const": graph["graph_revision_ref"]},
                            "impact": text,
                        },
                        "required": ["quest_ref", "goal_revision_ref", "graph_revision_ref", "impact"],
                    },
                },
                "required": ["cycle", "current_question", "parent_questions", "quest"],
            },
            "is_authoritative": {"const": False},
        },
        "required": [
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
        ],
    }


def _reasoning_source_properties(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    return {
        "source_quest_ref": {"const": request.quest_ref},
        "source_cycle_ref": {"const": request.cycle_ref},
        "source_reasoning_stage_run_request_ref": {
            "const": request.stage_request_ref
        },
        "source_scientific_outcome_ref": text,
        "source_question_ref": {"const": request.question_ref},
        "source_foreground_epoch": {"const": request.foreground_epoch},
    }


def _next_cycle_proposal_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    base_properties = {
        "schema_ref": {"const": NEXT_CYCLE_PROPOSAL_SCHEMA_REF},
        "kind": {"const": "NextCycleProposal"},
        **_reasoning_source_properties(request),
        "target_question_ref": {"type": "string", "minLength": 1},
        "target_question_anchor_ref": {"type": "string", "minLength": 1},
        "is_authoritative": {"const": False},
    }
    return {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **base_properties,
                    "entry_stage": {"const": entry_stage},
                    "typed_skip_basis_refs_by_stage": _typed_skip_basis_schema(
                        entry_stage, text
                    ),
                },
                "required": [
                    *base_properties,
                    "entry_stage",
                    "typed_skip_basis_refs_by_stage",
                ],
            }
            for entry_stage in ("idea", "plan", "bundle", "reasoning")
        ],
    }


def _typed_skip_basis_schema(
    entry_stage: str,
    text_schema: dict[str, object],
) -> dict[str, object]:
    stage_order = ("idea", "plan", "bundle", "reasoning")
    skipped_stages = stage_order[: stage_order.index(entry_stage)]
    properties = {
        stage: {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": text_schema,
        }
        for stage in skipped_stages
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _candidate_completion_schema(
    request: ReasoningSkillRequest,
) -> dict[str, object]:
    milestone_basis_refs = completion_milestone_basis_refs(request.context_pack)
    properties = {
        "schema_ref": {"const": CANDIDATE_COMPLETION_SCHEMA_REF},
        "kind": {"const": "CandidateCompletion"},
        **_reasoning_source_properties(request),
        "current_quest_ref": {"const": request.quest_ref},
        "current_goal_revision_ref": {"const": request.goal_revision_ref},
        "completion_milestone_basis_refs": {
            "type": "array",
            "minItems": len(milestone_basis_refs),
            "maxItems": len(milestone_basis_refs),
            "items": {"type": "string", "minLength": 1},
        },
        "rationale": {"type": "string", "minLength": 1},
        "is_authoritative": {"const": False},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _reasoning_review_response_schema(
    request: ReasoningSkillRequest,
    final_output_schema: dict[str, object],
) -> dict[str, object]:
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "finding_id": {"type": "string", "minLength": 1},
            "category": {
                "type": "string",
                "enum": sorted(REASONING_REVIEW_CATEGORIES),
            },
            "message": {"type": "string", "minLength": 1},
        },
        "required": ["finding_id", "category", "message"],
    }
    disposition = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "finding_id": {"type": "string", "minLength": 1},
            "action": {
                "type": "string",
                "enum": sorted(REASONING_REVIEW_ACTIONS),
            },
            "rationale": {"type": "string", "minLength": 1},
        },
        "required": ["finding_id", "action", "rationale"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": REASONING_REVIEW_SCHEMA_REF},
            "reviewer_agent_ref": {"type": "string", "minLength": 1},
            "findings": {"type": "array", "items": finding},
            "final_output": final_output_schema,
            "dispositions": {"type": "array", "items": disposition},
        },
        "required": [
            "schema_ref",
            "reviewer_agent_ref",
            "findings",
            "final_output",
            "dispositions",
        ],
    }
