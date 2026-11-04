from __future__ import annotations

from meta_research.research_guidance import SHARED_REFERENCE_PATHS, shared_reference_resources, cycle_role_skill_text, prepare_cycle_role_resources

from meta_research.context_presentation import (
    STAGE_CONTEXT_DELIVERY_GUIDANCE, stage_context_json, stage_position_prompt,
)
from meta_research.resource_preparation import resource_preparation, prepared_resource
from meta_research.native_guidance import native_guidance_operation

from meta_research.runtime_binding_compatibility import bundle_bindings_compatible

from dataclasses import dataclass, replace
import hashlib
import hmac
import os
from importlib.resources import files
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast
from urllib.parse import urlsplit

from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA,
    BundleExhaustionReviewTrace,
    bundle_exhaustion_advisory_review_document,
    validate_bundle_exhaustion_assessment,
)
from meta_research.bundle_contract import (
    MAX_BUNDLE_TARGETS,
    TARGET_PLAN_REVIEW_SCHEMA_REF,
    TARGET_PLAN_SCHEMA_REF,
    BundleContractError,
    validate_bundle_context_pack,
    validate_target_plan,
)
from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
    MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
    NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF,
    PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
    ROLLING_STRATEGY_STATE_SCHEMA_REF,
    BundleTargetContractError,
    apply_strategy_update,
    completion_contract_hash,
    normalized_completion_contract_from_dict,
    rolling_strategy_state_from_dict,
    strategy_update_from_dict,
)
from meta_research.bundle_protocol import (
    BUNDLE_CANONICAL_INTEGER_MAX_ABS,
    BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
    BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES,
    BUNDLE_ROOT_MAX_NODES,
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    TERMINAL_EXTERNAL_OUTCOMES,
)
from meta_research.codex_ledger import CodexSessionLedgerReader
from meta_research.codex_runtime import (
    CODEX_MODEL_REF,
    CODEX_REASONING_EFFORT_BINDING,
)
from meta_research.idea_skill import stage_operation_input, stage_replacement_prompt
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    ProviderTransportLimits,
    _CODEX_JSON_OBJECT_STRING_MARKER,
    _DISABLED_CODEX_FEATURES,
    _compile_codex_output_schema,
    _codex_harness_manifest,
    _file_sha256,
    _shared_codex_adapter_source_hash,
)
from meta_research.harness import (
    FullConformanceBinding,
    HarnessAdmissionError,
    OPERATION_BINDING_V1,
    ResidentMcpChannel,
)
from meta_research.owners.agent_runtime import (
    BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
    BUNDLE_INBOX_CHECKPOINT_SCHEMA,
    BundleRuntimeBinding,
    validate_bundle_inbox_checkpoint_projection,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.plan_contract import MAX_PLAN_EXPERIMENT_BRIEFS
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import transport_key_hash
from meta_research.root_capabilities import merge_root_capability_bindings
BundleSkillContractError = BundleContractError
BundleSkillUnavailable = IdeaSkillUnavailable


class RecoverableBundleSkillCandidateError(BundleSkillContractError):
    """A completed Bundle candidate failed its content/result contract."""
BUNDLE_PROVIDER_TRANSPORT_LIMITS = ProviderTransportLimits(
    prompt_max_bytes=64 * 1024 * 1024,
    stream_max_bytes=64 * 1024 * 1024,
    result_max_bytes=16 * 1024 * 1024,
)
BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES = (
    BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes
)
_FULL_CONFORMANCE_CAPABILITY = "harness-operation-binding-v1"
_FULL_CONFORMANCE_MCP_PREFIX = "harness-operation-binding:semantic-mcp-"
_FULL_CONFORMANCE_RESOURCE_PREFIX = "harness-artifact:operation-binding-"


class BundleFullConformanceAuthority(Protocol):
    def require_operation_binding(
        self,
        *,
        harness_family: str,
        required_operation_ids: tuple[str, ...],
        required_capabilities: tuple[str, ...],
    ) -> FullConformanceBinding: ...

    def require_full_conformance_binding(self) -> FullConformanceBinding: ...

    def issue_resident_mcp_channel(
        self,
        *,
        root_kind: str,
        phase: str,
        subject_policy: str,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> ResidentMcpChannel: ...

    def revoke_resident_mcp_channel(self, channel: ResidentMcpChannel) -> None: ...


def bind_bundle_runtime_to_full_conformance(
    binding: BundleRuntimeBinding,
    conformance: FullConformanceBinding,
    *,
    required_operation_ids: tuple[str, ...],
) -> BundleRuntimeBinding:
    """Freeze one operation-local Harness binding into Bundle AR admission.

    The operation is idempotent so both the Stage boundary and the production
    adapter can independently apply the same current binding without creating
    duplicate bindings.
    """

    _require_exact_bundle_operation_binding(
        conformance,
        required_operation_ids=required_operation_ids,
    )
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
        "harness-artifact:operation-binding-contract:"
        f"{conformance.contract_ref}@sha256:{conformance.contract_hash}",
        "harness-artifact:operation-binding-set:"
        f"{conformance.conformance_ref}@sha256:{conformance.binding_hash}",
        *conformance.profile_receipts,
    )
    return replace(
        binding,
        mcp_bindings=mcp_bindings,
        capability_bindings=capability_bindings,
        resource_bindings=resource_bindings,
    )


def _require_exact_bundle_operation_binding(
    conformance: FullConformanceBinding,
    *,
    required_operation_ids: tuple[str, ...],
) -> None:
    """Reject diagnostic/global or cross-family evidence at production seams."""

    if type(conformance) is not FullConformanceBinding:
        raise BundleSkillUnavailable("bundle_runtime_binding_drift")
    digest_values = (
        conformance.contract_hash,
        conformance.semantic_mcp_catalog_hash,
        conformance.semantic_mcp_operation_bindings_hash,
    )
    if (
        not required_operation_ids
        or len(required_operation_ids) != len(set(required_operation_ids))
        or conformance.contract_ref != OPERATION_BINDING_V1
        or conformance.required_families != ("codex",)
        or conformance.required_capabilities != ("semantic_mcp",)
        or conformance.required_operation_ids != required_operation_ids
        or conformance.profile_receipts != ()
        or any(
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digest_values
        )
        or conformance.conformance_ref
        != "operation_binding_" + conformance.contract_hash[:48]
    ):
        raise BundleSkillUnavailable("bundle_runtime_binding_drift")


@dataclass(frozen=True)
class BundleSkillRequest:
    stage_request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    cycle_ref: str
    question_ref: str
    formal_plan_ref: str
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    plan_document: dict[str, object]
    root_session_ref: str
    runtime_binding: BundleRuntimeBinding
    inbox_checkpoint: dict[str, object]
    native_replacement_context: dict[str, object] | None = None
    predecessor_rejections: tuple[dict[str, object], ...] = ()
    native_session_ref: str | None = None
    predecessor_candidate_ref: str | None = None
    owner_rejection_receipt_ref: str | None = None
    owner_rejection_kind: str | None = None
    owner_feedback: tuple[str, ...] = ()
    job_ref: str | None = None
    provider_feedback: dict[str, object] | None = None


@dataclass(frozen=True)
class BundleSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str
    output_kind: str = "target_plan"


@dataclass(frozen=True)
class BundleSkillResult:
    reviewed_draft: dict[str, object]
    final_target_plan: dict[str, object]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str | None
    adapter_kind: str


@dataclass(frozen=True)
class BundleExhaustionSkillResult:
    reviewed_assessment: dict[str, object]
    reviewed_assessment_hash: str
    findings: tuple[str, ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str | None
    adapter_kind: str
    review_trace: BundleExhaustionReviewTrace | None


@dataclass(frozen=True)
class BundleDispatchRequest:
    stage_request_ref: str
    cycle_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    graph_ref: str
    generation: int
    frontier: tuple[dict[str, object], ...]
    state: dict[str, object]
    root_session_ref: str
    native_session_ref: str | None
    runtime_binding: BundleRuntimeBinding
    inbox_checkpoint: dict[str, object]
    cycle_position: dict[str, object]
    job_ref: str | None = None
    provider_feedback: dict[str, object] | None = None
    native_replacement_context: dict[str, object] | None = None


@dataclass(frozen=True)
class BundleDispatchResult:
    action: str
    selected_target_ref: str | None
    rationale: str
    native_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class BundleTargetBatchRequest:
    stage_request_ref: str
    cycle_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    graph_ref: str
    formal_plan_ref: str
    context_pack_ref: str
    context_pack_hash: str
    plan_document: dict[str, object]
    initial_target_plan: dict[str, object]
    base_generation: int
    base_head_receipt: dict[str, object]
    current_targets: tuple[dict[str, object], ...]
    target_commits: tuple[dict[str, object], ...]
    root_session_ref: str
    native_session_ref: str | None
    runtime_binding: BundleRuntimeBinding
    inbox_checkpoint: dict[str, object]
    cycle_position: dict[str, object]
    job_ref: str | None = None
    provider_feedback: dict[str, object] | None = None
    native_replacement_context: dict[str, object] | None = None


@dataclass(frozen=True)
class BundleTargetBatchResult:
    strategy_update: dict[str, object]
    rationale: str
    native_session_ref: str
    adapter_kind: str


class BundleSkillProvider(Protocol):
    def runtime_binding(self) -> BundleRuntimeBinding: ...

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft: ...

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult | BundleExhaustionSkillResult: ...

    def execute(
        self, request: BundleSkillRequest
    ) -> BundleSkillResult | BundleExhaustionSkillResult: ...

    def schedule_target(
        self, request: BundleDispatchRequest
    ) -> BundleDispatchResult: ...

    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult: ...


def validate_bundle_dispatch_result(
    request: BundleDispatchRequest, result: BundleDispatchResult
) -> str:
    checkpoint_hash = _validate_inbox_checkpoint_projection(
        request.inbox_checkpoint,
        run_ref=request.run_ref,
        attempt_ref=request.attempt_ref,
        fence_ref=request.fence_ref,
    )
    if (
        not request.stage_request_ref
        or not request.run_ref
        or not request.attempt_ref
        or not request.fence_ref
        or not request.graph_ref
        or not request.root_session_ref
        or isinstance(request.generation, bool)
        or request.generation < 1
        or len(request.frontier) > MAX_BUNDLE_TARGETS
        or any(not isinstance(item, dict) for item in request.frontier)
        or not isinstance(request.state, dict)
        or not _rolling_native_result_matches(request, result.native_session_ref)
        or not result.adapter_kind
        or not isinstance(result.rationale, str)
        or not result.rationale.strip()
        or len(result.rationale) > 512
    ):
        raise BundleSkillContractError("bundle_dispatch_invalid")
    matching = [
        item
        for item in request.frontier
        if item.get("target_ref") == result.selected_target_ref
    ]
    if (
        result.action not in {"dispatch", "wait", "replan_required"}
        or (
            result.action == "dispatch"
            and (
                not result.selected_target_ref
                or len(matching) != 1
                or matching[0].get("dispatch_allowed", True) is not True
            )
        )
        or (result.action != "dispatch" and result.selected_target_ref is not None)
    ):
        raise BundleSkillContractError("bundle_dispatch_target_not_in_frontier")
    return canonical_hash(
        {
            "schema_ref": "meta-research/bundle-dispatch-decision/v1",
            "stage_request_ref": request.stage_request_ref,
            "run_ref": request.run_ref,
            "attempt_ref": request.attempt_ref,
            "fence_ref": request.fence_ref,
            "root_session_ref": request.root_session_ref,
            "graph_ref": request.graph_ref,
            "generation": request.generation,
            "frontier_hash": canonical_hash(list(request.frontier)),
            "state_hash": canonical_hash(request.state),
            "inbox_checkpoint_ref": request.inbox_checkpoint["checkpoint_ref"],
            "inbox_checkpoint_hash": checkpoint_hash,
            "action": result.action,
            "selected_target_ref": result.selected_target_ref,
            "rationale": result.rationale,
            "native_session_ref": result.native_session_ref,
            "adapter_kind": result.adapter_kind,
        }
    )


def validate_bundle_target_batch_result(
    request: BundleTargetBatchRequest,
    result: BundleTargetBatchResult,
) -> str:
    checkpoint_hash = _validate_inbox_checkpoint_projection(
        request.inbox_checkpoint,
        run_ref=request.run_ref,
        attempt_ref=request.attempt_ref,
        fence_ref=request.fence_ref,
    )
    if (
        not request.stage_request_ref
        or not request.run_ref
        or not request.attempt_ref
        or not request.fence_ref
        or not request.graph_ref
        or not request.formal_plan_ref
        or not request.context_pack_ref
        or not request.root_session_ref
        or isinstance(request.base_generation, bool)
        or request.base_generation < 0
        or not _rolling_native_result_matches(request, result.native_session_ref)
        or not result.adapter_kind
        or not isinstance(result.strategy_update, dict)
        or not isinstance(result.rationale, str)
        or not result.rationale.strip()
        or len(result.rationale) > 1000
    ):
        raise BundleSkillContractError("bundle_target_batch_invalid")
    validate_target_plan(
        request.initial_target_plan,
        formal_plan_ref=request.formal_plan_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        plan_document=request.plan_document,
    )
    completion_value = request.initial_target_plan.get("completion_contract")
    initial_update_value = request.initial_target_plan.get(
        "initial_strategy_update"
    )
    if not isinstance(completion_value, dict) or not isinstance(
        initial_update_value, dict
    ):
        raise BundleSkillContractError("bundle_target_batch_invalid")
    try:
        completion = normalized_completion_contract_from_dict(
            completion_value,
            plan_document=request.plan_document,
        )
        initial_update = strategy_update_from_dict(
            initial_update_value,
            completion_contract=completion,
        )
    except BundleTargetContractError as error:
        raise BundleSkillContractError(str(error)) from error
    current_specs: list[dict[str, object]] = []
    label_by_target_ref: dict[str, str] = {}
    for current in request.current_targets:
        spec = current.get("spec") if isinstance(current, dict) else None
        target_ref = current.get("target_ref") if isinstance(current, dict) else None
        if not isinstance(spec, dict) or not isinstance(target_ref, str):
            raise BundleSkillContractError("bundle_target_batch_invalid")
        current_specs.append(cast(dict[str, object], spec))
        candidate_value = spec.get("candidate")
        label = (
            candidate_value.get("local_label")
            if isinstance(candidate_value, dict)
            else None
        )
        if not isinstance(label, str) or target_ref in label_by_target_ref:
            raise BundleSkillContractError("bundle_target_batch_invalid")
        label_by_target_ref[target_ref] = label
    initial_candidates = initial_update_value.get("candidates")
    if (
        not isinstance(initial_candidates, list)
        or len(current_specs) < len(initial_candidates)
        or current_specs[: len(initial_candidates)] != initial_candidates
    ):
        raise BundleSkillContractError("bundle_target_batch_history_drift")
    current_state_document = {
        "schema_ref": ROLLING_STRATEGY_STATE_SCHEMA_REF,
        "completion_contract_hash": completion_contract_hash(completion),
        "revision": request.base_generation + 1,
        "candidates": current_specs,
        "strategy_complete": False,
    }
    try:
        state = rolling_strategy_state_from_dict(
            current_state_document,
            completion_contract=completion,
        )
        update = strategy_update_from_dict(
            result.strategy_update,
            completion_contract=completion,
        )
        if update.update.revision != state.strategy.revision + 1:
            raise BundleTargetContractError("strategy_revision_not_monotonic")
        committed_target_refs = {
            commit.get("target_ref")
            for commit in request.target_commits
            if isinstance(commit, dict)
            and isinstance(commit.get("target_ref"), str)
        }
        accepted_labels = frozenset(
            label
            for target_ref, label in label_by_target_ref.items()
            if target_ref in committed_target_refs
        )
        apply_strategy_update(
            state,
            update,
            completion_contract=completion,
            accepted_labels=accepted_labels,
        )
    except BundleTargetContractError as error:
        raise BundleSkillContractError(str(error)) from error
    return canonical_hash(
        {
            "schema_ref": "meta-research/bundle-target-batch-result/v2",
            "graph_ref": request.graph_ref,
            "base_generation": request.base_generation,
            "base_head_receipt": request.base_head_receipt,
            "root_session_ref": request.root_session_ref,
            "inbox_checkpoint_ref": request.inbox_checkpoint["checkpoint_ref"],
            "inbox_checkpoint_hash": checkpoint_hash,
            "strategy_update": result.strategy_update,
            "rationale": result.rationale,
            "native_session_ref": result.native_session_ref,
            "adapter_kind": result.adapter_kind,
        }
    )


def validate_bundle_skill_draft(
    request: BundleSkillRequest, result: BundleSkillDraft
) -> str:
    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    try:
        if result.output_kind == "target_plan":
            return validate_target_plan(
                result.draft,
                formal_plan_ref=request.formal_plan_ref,
                context_pack_ref=request.context_pack_ref,
                context_pack_hash=request.context_pack_hash,
                plan_document=request.plan_document,
            )
        if result.output_kind == "exhaustion_assessment":
            return validate_bundle_exhaustion_assessment(
                result.draft,
                plan_document=request.plan_document,
            )
        raise BundleSkillContractError("bundle_skill_output_kind_invalid")
    except OwnerConflict as error:
        raise RecoverableBundleSkillCandidateError(error.code) from error
    except BundleSkillContractError as error:
        raise RecoverableBundleSkillCandidateError(str(error)) from error


def validate_bundle_skill_result(
    request: BundleSkillRequest, result: BundleSkillResult
) -> tuple[str, str, str]:
    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    try:
        if (
            result.review_mode != "advisory_unobserved"
            or result.reviewer_agent_ref is not None
            or result.findings != ()
            or result.dispositions != ()
            or result.final_target_plan != result.reviewed_draft
        ):
            raise BundleSkillContractError("bundle_advisory_review_invalid")
        draft_hash = validate_target_plan(
            result.reviewed_draft,
            formal_plan_ref=request.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            plan_document=request.plan_document,
        )
        final_hash = validate_target_plan(
            result.final_target_plan,
            formal_plan_ref=request.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            plan_document=request.plan_document,
        )
        review = review_record(
            result, draft_hash=draft_hash, final_target_plan_hash=final_hash
        )
        review_hash = canonical_hash(review)
    except BundleSkillContractError as error:
        raise RecoverableBundleSkillCandidateError(str(error)) from error
    return draft_hash, final_hash, review_hash


def validate_bundle_exhaustion_skill_result(
    request: BundleSkillRequest,
    result: BundleExhaustionSkillResult,
) -> tuple[str, str]:
    """Validate the immutable assessment; review provenance is advisory."""

    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    try:
        if (
            result.review_mode != "advisory_unobserved"
            or result.reviewer_agent_ref is not None
            or result.findings != ()
            or result.review_trace is not None
        ):
            raise BundleSkillContractError(
                "bundle_exhaustion_advisory_review_invalid"
            )
        assessment_hash = validate_bundle_exhaustion_assessment(
            result.reviewed_assessment,
            plan_document=request.plan_document,
        )
        if result.reviewed_assessment_hash != assessment_hash:
            raise BundleSkillContractError(
                "bundle_exhaustion_review_binding_invalid"
            )
        advisory_document = bundle_exhaustion_advisory_review_document(
            reviewed_assessment_hash=assessment_hash,
            reviewer_agent_ref=result.reviewer_agent_ref,
            findings=result.findings,
        )
    except OwnerConflict as error:
        raise RecoverableBundleSkillCandidateError(error.code) from error
    except BundleSkillContractError as error:
        raise RecoverableBundleSkillCandidateError(str(error)) from error
    return assessment_hash, canonical_hash(advisory_document)


def review_record(
    result: BundleSkillResult,
    *,
    draft_hash: str,
    final_target_plan_hash: str,
) -> dict[str, object]:
    if (
        result.review_mode != "advisory_unobserved"
        or result.reviewer_agent_ref is not None
        or result.findings != ()
        or result.dispositions != ()
        or result.final_target_plan != result.reviewed_draft
        or draft_hash != canonical_hash(result.reviewed_draft)
        or final_target_plan_hash != canonical_hash(result.final_target_plan)
    ):
        raise BundleSkillContractError("bundle_advisory_review_invalid")
    return {
        "schema_ref": TARGET_PLAN_REVIEW_SCHEMA_REF,
        "review_mode": "advisory_unobserved",
        "reviewer_agent_ref": None,
        "reviewed_draft_hash": draft_hash,
        "findings": [],
        "dispositions": [],
        "final_target_plan_hash": final_target_plan_hash,
        "independent": False,
        "advisory_only": True,
    }


def _validate_request(request: BundleSkillRequest) -> None:
    for value in (
        request.stage_request_ref,
        request.run_ref,
        request.attempt_ref,
        request.fence_ref,
        request.cycle_ref,
        request.question_ref,
        request.formal_plan_ref,
        request.context_pack_ref,
        request.root_session_ref,
    ):
        if not isinstance(value, str) or not value:
            raise BundleSkillContractError("bundle_skill_request_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise BundleSkillContractError("context_pack_hash_mismatch")
    _validate_inbox_checkpoint_projection(
        request.inbox_checkpoint,
        run_ref=request.run_ref,
        attempt_ref=request.attempt_ref,
        fence_ref=request.fence_ref,
    )
    _validate_predecessor_rejections(request.predecessor_rejections)
    feedback_revision = request.predecessor_candidate_ref is not None
    if feedback_revision != (request.owner_rejection_receipt_ref is not None) or (
        feedback_revision != (request.owner_rejection_kind is not None)
    ):
        raise BundleSkillContractError("owner_feedback_lineage_incomplete")
    if feedback_revision:
        if (
            not isinstance(request.predecessor_candidate_ref, str)
            or not request.predecessor_candidate_ref.strip()
            or not isinstance(request.owner_rejection_receipt_ref, str)
            or not request.owner_rejection_receipt_ref.strip()
            or request.owner_rejection_kind not in {"domain", "completion"}
            or request.native_session_ref is None
            or type(request.owner_feedback) is not tuple
            or not request.owner_feedback
            or any(
                not isinstance(item, str) or not item.strip()
                for item in request.owner_feedback
            )
        ):
            raise BundleSkillContractError("owner_feedback_invalid")
    elif type(request.owner_feedback) is not tuple or request.owner_feedback:
        raise BundleSkillContractError("owner_feedback_without_rejection")
    question = request.context_pack.get("accepted_question_binding")
    formal_plan = request.context_pack.get("accepted_formal_plan_binding")
    if not isinstance(question, dict) or not isinstance(formal_plan, dict):
        raise BundleSkillContractError("bundle_context_pack_invalid")
    plan = validate_bundle_context_pack(
        request.context_pack,
        cycle_ref=request.cycle_ref,
        accepted_question_binding=cast(dict[str, object], question),
        accepted_formal_plan_binding=cast(dict[str, object], formal_plan),
    )
    if (
        question.get("question_ref") != request.question_ref
        or formal_plan.get("formal_plan_ref") != request.formal_plan_ref
        or plan != request.plan_document
        or plan.get("bundle_disposition") != "experiments_required"
    ):
        raise BundleSkillContractError("bundle_skill_request_binding_mismatch")


def _validate_inbox_checkpoint_projection(
    value: object,
    *,
    run_ref: str,
    attempt_ref: str,
    fence_ref: str,
) -> str:
    try:
        return validate_bundle_inbox_checkpoint_projection(
            value,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
        )
    except OwnerConflict as error:
        raise BundleSkillContractError(error.code) from error


def _validate_predecessor_rejections(
    values: tuple[dict[str, object], ...],
) -> None:
    if type(values) is not tuple or len(values) > BUNDLE_PROJECTION_MAX_TUPLE_ITEMS:
        raise BundleSkillContractError("bundle_predecessor_rejections_invalid")
    seen_attempts: set[str] = set()
    seen_submissions: set[str] = set()
    receipt_fields = {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    for item in values:
        if type(item) is not dict or set(item) != {
            "attempt_ref",
            "submission_ref",
            "submission_content_hash",
            "execution_receipt",
            "rejection_receipt",
        }:
            raise BundleSkillContractError("bundle_predecessor_rejections_invalid")
        attempt_ref = item.get("attempt_ref")
        submission_ref = item.get("submission_ref")
        content_hash = item.get("submission_content_hash")
        execution = item.get("execution_receipt")
        rejection = item.get("rejection_receipt")
        if (
            not isinstance(attempt_ref, str)
            or not attempt_ref
            or not isinstance(submission_ref, str)
            or not submission_ref
            or not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
            or attempt_ref in seen_attempts
            or submission_ref in seen_submissions
            or type(execution) is not dict
            or set(execution) != receipt_fields
            or execution.get("status") != "accepted"
            or execution.get("issuer") != "agent_runtime"
            or type(rejection) is not dict
            or set(rejection) != receipt_fields
            or rejection.get("status") != "accepted"
            or rejection.get("issuer") != "research_graph"
        ):
            raise BundleSkillContractError("bundle_predecessor_rejections_invalid")
        for receipt in (execution, rejection):
            if any(
                not isinstance(receipt.get(field), str)
                or not cast(str, receipt[field])
                for field in ("kind", "receipt_ref", "subject_ref")
            ) or (
                not isinstance(receipt.get("payload_hash"), str)
                or len(cast(str, receipt["payload_hash"])) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in cast(str, receipt["payload_hash"])
                )
            ):
                raise BundleSkillContractError(
                    "bundle_predecessor_rejections_invalid"
                )
        seen_attempts.add(attempt_ref)
        seen_submissions.add(submission_ref)
    if (
        len(canonical_json(list(values)).encode("utf-8"))
        > BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes // 2
    ):
        raise BundleSkillContractError("bundle_predecessor_rejections_too_large")


def _validate_identity(
    request: BundleSkillRequest,
    *,
    primary_session_ref: str,
    reviewer_agent_ref: str | None,
    adapter_kind: str,
) -> None:
    if (
        not primary_session_ref
        or not adapter_kind
        or primary_session_ref == request.root_session_ref
        or (
            request.native_session_ref is not None
            and request.native_session_ref != primary_session_ref
        )
        or reviewer_agent_ref is not None
        and not reviewer_agent_ref
    ):
        raise BundleSkillContractError("bundle_skill_session_invalid")


def _owner_rejection_prompt(request: BundleSkillRequest) -> str:
    if not request.owner_feedback:
        return ""
    if request.owner_rejection_kind == "domain":
        instruction = (
            "这是 RG domain rejection 后在同一 Root/native Session 中的新 Attempt。"
            "必须实质修订 TargetPlan 并逐条处理正式 feedback。"
        )
    else:
        instruction = (
            "这是 Owner structured-completion rejection 后在同一 Root/native Session "
            "中的新 Attempt。必须修正被拒绝的 completion 表达并逐条处理正式 feedback。"
        )
    return (
        f"\n{instruction}\n"
        f"predecessor_candidate_ref={request.predecessor_candidate_ref}\n"
        f"owner_rejection_receipt_ref={request.owner_rejection_receipt_ref}\n"
        f"owner_rejection_kind={request.owner_rejection_kind}\n"
        f"owner_feedback={canonical_json(list(request.owner_feedback))}\n"
    )


class CodexBundleSkillAdapter(CodexPlanSkillAdapter):
    _root_agent_kind = "bundle"
    """Production Bundle adapter using one managed native root Session."""

    _provider_transport_limits = BUNDLE_PROVIDER_TRANSPORT_LIMITS

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        timeout_seconds: float | None = None,
        process_runner: Callable[
            [list[str], str, float | None], subprocess.CompletedProcess[str]
        ]
        | None = None,
        codex_ledger_reader: CodexSessionLedgerReader | None = None,
        codex_home: Path | None = None,
    ) -> None:
        super().__init__(
            workspace,
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
            codex_ledger_reader=codex_ledger_reader,
            codex_home=codex_home,
        )
        self._full_conformance_authority: (
            BundleFullConformanceAuthority | None
        ) = None
        self._resident_mcp_base_url: str | None = None
        self._resident_mcp_channels: dict[
            tuple[str, str], ResidentMcpChannel
        ] = {}
        self._batch_plan_reader = None
        self._batch_source_readers = None

    def bind_batch_plan_reader(self, reader) -> None:
        self._batch_plan_reader = reader

    def bind_batch_source_readers(self, reader) -> None:
        self._batch_source_readers = reader

    def _operation_output_schema(self, schema, job_ref, operation_name):
        from meta_research.bundle_output_assembly import full_prose_output_schema
        from meta_research.idea_skill import _read_sealed_operation_invocation
        full = full_prose_output_schema(schema)
        if job_ref is not None:
            path = (self._workspace / "provider-operations" / canonical_hash({"job_ref": job_ref})
                    / operation_name / "invocation.json")
            if path.exists():
                _, key = self._transport_key()
                sealed = _read_sealed_operation_invocation(path, key=key)
                for candidate in (schema, full):
                    if sealed.get("output_schema_hash") == canonical_hash(_compile_codex_output_schema(candidate)):
                        return candidate
                raise BundleSkillUnavailable("codex_operation_identity_conflict")
        return full if os.environ.get("META_RESEARCH_BUNDLE_OUTPUT_MODE") == "full" else schema

    def bind_full_conformance_authority(
        self, authority: BundleFullConformanceAuthority
    ) -> None:
        if (
            self._full_conformance_authority is not None
            and self._full_conformance_authority is not authority
        ):
            raise BundleSkillUnavailable(
                "bundle_harness_conformance_authority_conflict"
            )
        self._full_conformance_authority = authority

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        """Bind the daemon-local Semantic MCP endpoint without persisting it.

        The deployment address is not research state and the bearer credential
        is never part of a RuntimeBinding, prompt, argv, or durable spool.
        """

        if not isinstance(base_url, str):
            raise BundleSkillUnavailable("bundle_semantic_mcp_endpoint_invalid")
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
            raise BundleSkillUnavailable("bundle_semantic_mcp_endpoint_invalid")
        normalized = base_url.rstrip("/")
        if (
            self._resident_mcp_base_url is not None
            and self._resident_mcp_base_url != normalized
        ):
            raise BundleSkillUnavailable("bundle_semantic_mcp_endpoint_conflict")
        self._resident_mcp_base_url = normalized

    def _bundle_semantic_operation_ids(self) -> tuple[str, ...]:
        try:
            from meta_research.semantic_owner_gateway import (
                BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
            )
        except (ImportError, AttributeError) as error:
            raise BundleSkillUnavailable(
                "bundle_semantic_mcp_catalog_unavailable"
            ) from error
        operation_ids = tuple(BUNDLE_ROOT_SEMANTIC_OPERATION_IDS)
        if not operation_ids or len(operation_ids) != len(set(operation_ids)):
            raise BundleSkillUnavailable("bundle_semantic_mcp_catalog_unavailable")
        return operation_ids

    def _release_resident_channel(self, key: tuple[str, str]) -> None:
        channel = self._resident_mcp_channels.pop(key, None)
        authority = self._full_conformance_authority
        if channel is None or authority is None:
            return
        try:
            authority.revoke_resident_mcp_channel(channel)
        except HarnessAdmissionError as error:
            raise BundleSkillUnavailable(error.code) from error

    @resource_preparation
    @native_guidance_operation
    def _invoke_with_resident_mcp(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        runtime_binding: BundleRuntimeBinding,
        operation_name: str,
        prompt: str,
        schema: dict[str, object],
        native_session_ref: str | None,
        job_ref: str | None,
        operation_input: dict[str, object] | None = None,
        inline_guidance: bool = False,
    ) -> tuple[dict[str, object], str | None, str]:
        schema = self._operation_output_schema(schema, job_ref, operation_name)
        prompt, guidance_binding = self._prepare_stage_guidance(
            run_ref=run_ref, attempt_ref=attempt_ref, root_session_ref=root_session_ref,
            fence_ref=fence_ref, runtime_binding_hash=canonical_hash(runtime_binding.as_dict()),
            job_ref=job_ref, operation_name=operation_name, prompt=prompt,
            include_body=inline_guidance,
        )
        authority = self._full_conformance_authority
        base_url = self._resident_mcp_base_url
        if authority is None or base_url is None:
            raise BundleSkillUnavailable("bundle_semantic_mcp_unavailable")
        operation_ids = self._bundle_semantic_operation_ids()
        # The bearer belongs to this exact operation tree. Root reconnects and
        # native children may use its catalog; Owner/currentness checks remain
        # bound to the admitted Bundle scope.
        subject_policy = "operation_tree"
        try:
            conformance = authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=operation_ids,
                required_capabilities=("semantic_mcp",),
            )
        except HarnessAdmissionError as error:
            raise BundleSkillUnavailable(error.code) from error
        rebound = bind_bundle_runtime_to_full_conformance(
            self._base_runtime_binding(),
            conformance,
            required_operation_ids=operation_ids,
        )
        if not bundle_bindings_compatible(rebound, runtime_binding):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        channel_key = (job_ref or run_ref, operation_name)
        channel = self._resident_mcp_channels.get(channel_key)
        if channel is None:
            try:
                channel = authority.issue_resident_mcp_channel(
                    root_kind="bundle",
                    phase=operation_name,
                    subject_policy=subject_policy,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                    capability_binding_hash=canonical_hash(
                        runtime_binding.as_dict()
                    ),
                    operation_ids=operation_ids,
                    **({} if guidance_binding is None else {"guidance_binding": guidance_binding}),
                )
            except HarnessAdmissionError as error:
                raise BundleSkillUnavailable(error.code) from error
            self._resident_mcp_channels[channel_key] = channel
        if (
            getattr(channel.binding, "guidance_binding", None) != guidance_binding
            or channel.connection.grant_ref
            != channel.binding.connection_grant_ref
            or channel.binding.catalog_hash
            != conformance.semantic_mcp_catalog_hash
            or canonical_hash(list(channel.binding.operation_bindings))
            != conformance.semantic_mcp_operation_bindings_hash
        ):
            self._release_resident_channel(channel_key)
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        endpoint_ref = channel.binding.endpoint_ref
        if not endpoint_ref.startswith("/") or "?" in endpoint_ref or "#" in endpoint_ref:
            self._release_resident_channel(channel_key)
            raise BundleSkillUnavailable("bundle_semantic_mcp_endpoint_invalid")
        scope_binding_hash = canonical_hash(
            {
                **({} if guidance_binding is None else {"guidance_binding": guidance_binding.as_dict()}),
                "catalog_hash": channel.binding.catalog_hash,
                "operation_bindings": list(channel.binding.operation_bindings),
            }
        )
        try:
            result = self._invoke(
                operation_name=operation_name,
                prompt=prompt,
                schema=schema,
                native_session_ref=native_session_ref,
                job_ref=job_ref,
                mcp_url=base_url + endpoint_ref,
                mcp_token=channel.connection.token,
                mcp_scope_binding_hash=scope_binding_hash,
                semantic_mcp_protected_environment=True,
                authorized_operation_ids=operation_ids,
                run_ref=run_ref,
                **({} if guidance_binding is None else {"guidance_binding": guidance_binding}),
                workspace_binding=self._workspace_binding(run_ref=run_ref, attempt_ref=attempt_ref,
                    root_session_ref=root_session_ref, fence_ref=fence_ref,
                    runtime_binding_hash=canonical_hash(runtime_binding.as_dict())),
                operation_input=operation_input,
            )
        except BundleSkillUnavailable as error:
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

    def _is_reconciliation_operation_name(self, operation_name: str) -> bool:
        dynamic_generation = next(
            (
                operation_name.removeprefix(prefix)
                for prefix in ("dispatch-", "target-batch-")
                if operation_name.startswith(prefix)
            ),
            "",
        )
        return super()._is_reconciliation_operation_name(operation_name) or (
            bool(dynamic_generation)
            and dynamic_generation.isascii()
            and dynamic_generation.isdigit()
            and dynamic_generation[0] != "0"
        )

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "primary":
            return "bundle_primary_result_contract_invalid"
        if self._is_reconciliation_operation_name(operation_name):
            return "bundle_review_result_contract_invalid"
        raise BundleSkillUnavailable("codex_operation_spool_invalid")

    def _terminal_contract_checkpoint(self, *, marker, operation_name, failure_code,
            detail_code, verified_native_session_ref):
        checkpoint = super()._terminal_contract_checkpoint(marker=marker,
            operation_name=operation_name, failure_code=failure_code, detail_code=detail_code,
            verified_native_session_ref=verified_native_session_ref)
        if (operation_name.startswith(("dispatch-", "target-batch-"))
                and self._is_reconciliation_operation_name(operation_name)
                and isinstance(verified_native_session_ref, str) and verified_native_session_ref):
            return {**checkpoint, "schema_ref": "meta-research/provider-native-contract-failure/v1",
                "native_session_ref": verified_native_session_ref}
        return checkpoint

    @resource_preparation
    def _base_runtime_binding(self) -> BundleRuntimeBinding:
        resources = _bundle_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        shared_adapter_source_hash = _shared_codex_adapter_source_hash()
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        schemas = {
            "target-plan-envelope": _target_plan_envelope_schema(
                _schema_template_request()
            ),
            "target-dispatch": _dispatch_schema(("__target__",)),
            "target-batch": _target_batch_schema(_schema_template_request()),
        }
        from meta_research.bundle_output_assembly import full_prose_output_schema
        schemas.update({name + "-full": full_prose_output_schema(schema)
                        for name, schema in list(schemas.items()) if name != "target-dispatch"})
        schemas = {
            name: _compile_codex_output_schema(schema)
            for name, schema in schemas.items()
        }
        binding = BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _bundle_skill_instructions(resources),
                    "adapter_source_hash": adapter_source_hash,
                    "shared_adapter_source_hash": shared_adapter_source_hash,
                    "supervisor_source_hash": supervisor_source_hash,
                    "dispatch_recovery_source_hash": _file_sha256(
                        Path(__file__).with_name("bundle_dispatch_recovery.py")
                    ),
                    "batch_view_source_hash": _file_sha256(Path(__file__).with_name("bundle_batch_view.py")),
                    "batch_access_source_hash": _file_sha256(Path(__file__).with_name("bundle_batch_access.py")),
                    "output_assembly_source_hash": _file_sha256(Path(__file__).with_name("bundle_output_assembly.py")),
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=(),
            capability_bindings=merge_root_capability_bindings(
                (
                    "approval-policy-never",
                    "filesystem-danger-full-access",
                    "user-config-loaded",
                    "semantic-mcp-resident",
                    "native-session-resume",
                    "structured-output-json-schema",
                    "trusted-local-quest-authorization",
                ),
                self._root_agent_kind, self._model_ref,
            ),
            resource_bindings=tuple(
                f"package:meta_research.skills{'' if name in SHARED_REFERENCE_PATHS else '.bundle_stage'}/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in schemas.items()
            )
            + harness_artifacts
            + (
                "bundle-execution-contract:policy-refresh/v1",
                "adapter-source:meta_research.bundle_dispatch_recovery@sha256:"
                + _file_sha256(Path(__file__).with_name("bundle_dispatch_recovery.py")),
                "adapter-source:meta_research.bundle_skill@sha256:"
                f"{adapter_source_hash}",
                "adapter-source:meta_research.bundle_batch_view@sha256:"
                + _file_sha256(Path(__file__).with_name("bundle_batch_view.py")),
                "adapter-source:meta_research.bundle_batch_access@sha256:"
                + _file_sha256(Path(__file__).with_name("bundle_batch_access.py")),
                "adapter-source:meta_research.bundle_output_assembly@sha256:"
                + _file_sha256(Path(__file__).with_name("bundle_output_assembly.py")),
                "adapter-source:meta_research.idea_skill@sha256:"
                f"{shared_adapter_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                CODEX_REASONING_EFFORT_BINDING,
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"prompt={BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes};"
                f"stream={BUNDLE_PROVIDER_TRANSPORT_LIMITS.stream_max_bytes};"
                f"result={BUNDLE_PROVIDER_TRANSPORT_LIMITS.result_max_bytes}",
                self._provider_wall_clock_binding(),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:" + transport_key_hash(transport_key),
            ),
        )
        return binding

    @resource_preparation
    def runtime_binding(self) -> BundleRuntimeBinding:
        binding = self._base_runtime_binding()
        if self._full_conformance_authority is None:
            return binding
        operation_ids = self._bundle_semantic_operation_ids()
        return bind_bundle_runtime_to_full_conformance(
            binding,
            self._full_conformance_authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=operation_ids,
                required_capabilities=("semantic_mcp",),
            ),
            required_operation_ids=operation_ids,
        )

    @resource_preparation
    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        _validate_request(request)
        if not bundle_bindings_compatible(request.runtime_binding, self.runtime_binding()):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        lineage = _owner_rejection_prompt(request)
        human_resume = (
            ""
            if request.native_session_ref is None
            else "\n若本 Session 曾显式打开 HumanRequest，先以原 effect_id 调 "
            "human_request.open.reconcile，读取 resolution 后再判断"
            "信息是否足够；旧 receipt 不能释放新的 waiter。\n"
        )
        prompt = (
            stage_position_prompt(request, "bundle") + stage_replacement_prompt(request) +
            f"{_bundle_skill_instructions()}\n\n"
            f"{_provider_correction_prompt(request.provider_feedback)}\n"
            f"{human_resume}"
            "本回合按角色方法及 references/contract.md 的正式输出合同形成完整候选，"
            f"{_bundle_output_assembly_prompt()}"
            "完成独立子智能体审阅并由根自主修订后，返回 target_plan 或 exhaustion_assessment。"
            "保持下列准确 FormalPlan、完整采用来源与当前反馈；核验未知效果后再决定是否可提交。\n"
            f"{lineage}\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"cycle_ref={request.cycle_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"formal_plan_ref={request.formal_plan_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"inbox_checkpoint={canonical_json(request.inbox_checkpoint)}\n"
            "predecessor_rejections_hash="
            f"{canonical_hash(list(request.predecessor_rejections))}\n"
            "predecessor_rejections="
            f"{canonical_json(list(request.predecessor_rejections))}\n"
            + STAGE_CONTEXT_DELIVERY_GUIDANCE +
            f"provider_context_view={stage_context_json('bundle', request.context_pack, context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash)}"
        )
        output, session_ref, _primary_stdout = self._invoke_with_resident_mcp(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding,
            operation_name="primary",
            prompt=prompt,
            schema=_target_plan_envelope_schema(request),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
            operation_input=stage_operation_input(request, "bundle"),
        )
        if session_ref is None or type(output) is not dict:
            raise BundleSkillUnavailable("codex_bundle_primary_invalid")
        from meta_research.bundle_output_assembly import assemble_bundle_primary
        from meta_research.bundle_target_contract import BundleTargetContractError
        try:
            output = assemble_bundle_primary(output, request.plan_document)
        except BundleTargetContractError as error:
            raise self._sealed_result_failure(job_ref=request.job_ref, operation_name="primary",
                native_session_ref=session_ref, failure_code="bundle_primary_result_contract_invalid",
                detail_code=str(error), rejected_candidate=output) from error
        # Collaboration trace is advisory provenance.  TargetPlan and
        # exhaustion assessment acceptance stays with the Skill-defined
        # structural/domain validators.
        if set(output) == {"target_plan"} and isinstance(
            output.get("target_plan"), dict
        ):
            return BundleSkillDraft(
                draft=cast(dict[str, object], output["target_plan"]),
                primary_session_ref=session_ref,
                adapter_kind="codex_cli",
                output_kind="target_plan",
            )
        if set(output) == {"exhaustion_assessment"} and isinstance(
            output.get("exhaustion_assessment"), dict
        ):
            return BundleSkillDraft(
                draft=cast(dict[str, object], output),
                primary_session_ref=session_ref,
                adapter_kind="codex_cli",
                output_kind="exhaustion_assessment",
            )
        raise self._sealed_result_failure(
            job_ref=request.job_ref,
            operation_name="primary",
            native_session_ref=session_ref,
            failure_code="bundle_primary_result_contract_invalid",
            detail_code="codex_bundle_primary_invalid",
            rejected_candidate=output,
        )

    @resource_preparation
    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult | BundleExhaustionSkillResult:
        _validate_request(request)
        if (
            not bundle_bindings_compatible(request.runtime_binding, self.runtime_binding())
            or request.native_session_ref != draft.primary_session_ref
        ):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        if draft.output_kind == "exhaustion_assessment":
            return self._review_exhaustion_assessment(request, draft)
        if draft.output_kind != "target_plan":
            raise BundleSkillUnavailable("bundle_skill_output_kind_invalid")
        # The primary TargetPlan has already passed the exact Skill-defined
        # structural/domain validator.  Do not require another provider turn,
        # child choreography, or completion wrapper before persisting it.
        return BundleSkillResult(
            reviewed_draft=draft.draft,
            final_target_plan=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def _review_exhaustion_assessment(
        self,
        request: BundleSkillRequest,
        draft: BundleSkillDraft,
    ) -> BundleExhaustionSkillResult:
        try:
            assessment_hash = validate_bundle_exhaustion_assessment(
                draft.draft,
                plan_document=request.plan_document,
            )
        except OwnerConflict as error:
            raise BundleSkillUnavailable(error.code) from error
        # Exhaustion acceptance is rebuilt from the exact primary assessment,
        # the Owner inventory/frontier, HumanRequest currentness, and the
        # frozen FormalPlan.  A second provider turn or child transcript adds
        # optional advice only, so absence of either cannot reject the usable
        # assessment and must not be replaced with synthetic child events.
        result = BundleExhaustionSkillResult(
            reviewed_assessment=draft.draft,
            reviewed_assessment_hash=assessment_hash,
            findings=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
            review_trace=None,
        )
        validate_bundle_exhaustion_skill_result(request, result)
        return result

    def _bundle_exhaustion_trace_seal(
        self,
        trace: BundleExhaustionReviewTrace,
        *,
        runtime_binding_hash: str,
    ) -> str:
        _key_path, transport_key = self._transport_key()
        payload = canonical_json(
            {
                "runtime_binding_hash": runtime_binding_hash,
                "trace": trace.unsigned_dict(),
            }
        ).encode("utf-8")
        return hmac.new(transport_key, payload, hashlib.sha256).hexdigest()

    def verify_bundle_exhaustion_review_trace(
        self,
        trace: BundleExhaustionReviewTrace,
        *,
        runtime_binding_hash: str,
    ) -> None:
        if (
            type(trace) is not BundleExhaustionReviewTrace
            or canonical_hash(self.runtime_binding().as_dict())
            != runtime_binding_hash
        ):
            raise OwnerConflict("bundle_exhaustion_review_trace_invalid")
        expected = self._bundle_exhaustion_trace_seal(
            trace,
            runtime_binding_hash=runtime_binding_hash,
        )
        if not hmac.compare_digest(trace.transport_seal, expected):
            raise OwnerConflict("bundle_exhaustion_review_trace_invalid")

    @resource_preparation
    def execute(
        self, request: BundleSkillRequest
    ) -> BundleSkillResult | BundleExhaustionSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref), draft
        )

    @resource_preparation
    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        _validate_rolling_native_request(request)
        _validate_inbox_checkpoint_projection(
            request.inbox_checkpoint,
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            fence_ref=request.fence_ref,
        )
        if not bundle_bindings_compatible(request.runtime_binding, self.runtime_binding()):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        target_refs = tuple(cast(str, item["target_ref"]) for item in request.frontier)
        operation_input = {
            "kind": "bundle_dispatch", "identity": _rolling_operation_identity(request),
            "request": {"generation": request.generation,
                "frontier": list(request.frontier), "state": request.state,
                "inbox_checkpoint": request.inbox_checkpoint},
        }
        prompt = (
            _rolling_position_prompt(request) +
            stage_replacement_prompt(request) +
            f"{_bundle_skill_instructions()}\n\n"
            f"{_provider_correction_prompt(request.provider_feedback)}\n"
            "继续使用当前 Bundle 根 Session。先读取这次 durable frontier 与 "
            "TargetCommit/blocker 状态，再自主选择下一项可调度 Target。选择体现当前 "
            "优先级、依赖、已实现结果与局部阻塞；不得把 Agent tree 当成 Target DAG，"
            "选择 frontier 内有当前启动权限且值得投入的 Target 时返回 dispatch。"
            "需要等待证据、依赖或重新评估研究优先级时可返回 wait，并在 rationale "
            "说明等待条件；技术可启动不代表研究上必须立即启动。冻结语义确需 "
            "改变时返回 replan_required，说明已有证据与需由后继 Cycle 承接的修订，"
            "并在这次终态交接前委派原生独立子智能体审阅完整判断和必要原文，根根据"
            "自由格式反馈自主修订；同根自查不替代独立审阅。"
            "保留真实 Owner receipt。frontier 中的高风险 Target 若 "
            "dispatch_allowed=false，它是本次 Bundle 根 Agent 的授权协作义务，"
            "不是可调度 Target。human_request_status=not_open 时，必须原样使用 "
            "human_request_command.semantic_operation_id 和 arguments 调用 resident semantic "
            "MCP 创建 formal HumanRequest；若 effect outcome 不明，必须用同一 "
            "arguments 调用 reconciliation_operation_id，不得换 effect_id 重放。"
            "请求已打开、已满足或已拒绝时不得再造新请求；返回 wait，"
            "由 Owner 评估 response、重验 authorization 并释放 waiter。绝对不得把 "
            "dispatch_allowed=false 的 Target 返回为 dispatch，也不得因 provider、"
            "validator 或 readiness 错误自行伪造 HumanRequest。"
            "返回 wait 或 replan_required 时，selected_target_ref 必须为 null。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"graph_ref={request.graph_ref}\n"
            f"generation={request.generation}\n"
            f"inbox_checkpoint={canonical_json(request.inbox_checkpoint)}\n"
            f"frontier={canonical_json(list(request.frontier))}\n"
            f"state={canonical_json(request.state)}"
        )
        operation_name = f"dispatch-{request.generation}"
        try:
            output, session_ref, _stdout = self._invoke_with_resident_mcp(
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                runtime_binding=request.runtime_binding,
                operation_name=operation_name,
                prompt=prompt,
                schema=_dispatch_schema(target_refs),
                native_session_ref=request.native_session_ref,
                job_ref=request.job_ref,
                operation_input=operation_input,
            )
        except BundleSkillUnavailable as error:
            if error.code in {
                "codex_operation_identity_conflict", "codex_operation_spool_invalid",
            }:
                from meta_research.bundle_dispatch_recovery import recover_rejected_dispatch

                # Changed rolling inputs can fail the sealed prompt comparison
                # before invocation identity. Recovery verifies the old seal.
                recover_rejected_dispatch(self, request, operation_name)
            raise
        action = output.get("action")
        selected_target_ref = output.get("selected_target_ref")
        rationale = output.get("rationale")
        if (
            session_ref is None
            or not isinstance(action, str)
            or (
                selected_target_ref is not None
                and not isinstance(selected_target_ref, str)
            )
            or not isinstance(rationale, str)
        ):
            checkpoint_session = session_ref or request.native_session_ref
            if checkpoint_session is None:
                raise BundleSkillUnavailable("codex_bundle_dispatch_invalid")
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name=operation_name,
                native_session_ref=checkpoint_session,
                failure_code="bundle_review_result_contract_invalid",
                detail_code="codex_bundle_dispatch_invalid",
            )
        result = BundleDispatchResult(
            action=action,
            selected_target_ref=selected_target_ref,
            rationale=rationale,
            native_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        try:
            validate_bundle_dispatch_result(request, result)
        except BundleSkillContractError as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name=operation_name,
                native_session_ref=session_ref,
                failure_code="bundle_review_result_contract_invalid",
                detail_code=str(error),
            ) from error
        return result

    @resource_preparation
    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult:
        _validate_rolling_native_request(request)
        _validate_inbox_checkpoint_projection(
            request.inbox_checkpoint,
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            fence_ref=request.fence_ref,
        )
        prompt_context = {
            "stage_request_ref": request.stage_request_ref,
            "run_ref": request.run_ref,
            "attempt_ref": request.attempt_ref,
            "fence_ref": request.fence_ref,
            "root_session_ref": request.root_session_ref,
            "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()),
            "graph_ref": request.graph_ref,
            "formal_plan_ref": request.formal_plan_ref,
            "context_pack_ref": request.context_pack_ref,
            "context_pack_hash": request.context_pack_hash,
            "base_generation": request.base_generation,
            "base_head_receipt": request.base_head_receipt,
            "inbox_checkpoint": request.inbox_checkpoint,
            "initial_target_plan_hash": canonical_hash(
                request.initial_target_plan
            ),
            "initial_source_bindings": request.initial_target_plan.get(
                "source_bindings"
            ),
            "formal_plan": request.plan_document,
            "current_targets": list(request.current_targets),
            "target_commits": list(request.target_commits),
            "provider_feedback": request.provider_feedback,
        }
        operation_input = {
            "kind": "bundle_target_batch", "identity": _rolling_operation_identity(request),
            "request": prompt_context,
        }
        from meta_research.bundle_batch_view import render_batch_reading_context, render_delta_batch_reading_context
        from meta_research.semantic_mcp import SemanticMcpError

        operation_name = f"target-batch-{request.base_generation + 1}"
        sealed = request.job_ref is not None and (
            self._workspace / "provider-operations" / canonical_hash({"job_ref": request.job_ref})
            / operation_name / "invocation.json").exists()
        reader = None
        sources = None
        baseline = None
        mode = os.environ.get("META_RESEARCH_BUNDLE_BATCH_VIEW", "delta-v2")
        if (not sealed and request.native_session_ref and request.target_commits
                and request.native_replacement_context is None and not request.provider_feedback
                and self._batch_plan_reader is not None):
            try:
                reader = self._batch_plan_reader(request)
                if mode == "delta-v2" and self._batch_source_readers is not None:
                    sources = self._batch_source_readers(request)
                    from meta_research.bundle_batch_delivery import verified_batch_delivery_baseline
                    baseline = verified_batch_delivery_baseline(self, request,
                        verified_proposals=sources.get("proposals", ()),
                        verified_proposal_checkpoints=sources.get("proposal_checkpoints", {}))
            except SemanticMcpError as error:
                raise BundleSkillUnavailable(str(error)) from error
        if mode == "delta-v2":
            reading_context = render_delta_batch_reading_context(request, prompt_context,
                verified_plan_reader=reader, verified_source_readers=sources,
                verified_delivery_baseline=baseline)
        else:
            reading_context = render_batch_reading_context(request, prompt_context,
                verified_plan_reader=reader if mode == "plan-reference-v1" else None)
        prompt = (
            _rolling_position_prompt(request) +
            stage_replacement_prompt(request) +
            f"{_bundle_skill_instructions()}\n\n"
            f"{_provider_correction_prompt(request.provider_feedback)}\n"
            "继续当前 Bundle 根。根据本次正式 TargetCommit、仍未完成的责任及真实反馈，"
            f"{_bundle_output_assembly_prompt()}"
            "按 references/contract.md 形成下一份 FormalStrategyUpdate。需要继续实施时追加候选；"
            "全部所需 cells 已由 current accepted TargetCommit 覆盖时，先读最后结果、整理可复用资源、"
            "完成独立审阅，再以空 candidates、strategy_complete=true 和完整 notes 封口。"
            "保留后续研究线索与零或一个建题推荐，修订事实不改写冻结输入。\n"
            f"batch_context={canonical_json(reading_context)}"
        )
        if len(prompt.encode("utf-8")) > BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES:
            raise BundleSkillUnavailable("bundle_target_batch_prompt_too_large")
        if not bundle_bindings_compatible(request.runtime_binding, self.runtime_binding()):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        operation_name = f"target-batch-{request.base_generation + 1}"
        try:
            output, session_ref, _stdout = self._invoke_with_resident_mcp(
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                runtime_binding=request.runtime_binding,
                operation_name=operation_name,
                prompt=prompt,
                schema=_target_batch_schema(request),
                native_session_ref=request.native_session_ref,
                job_ref=request.job_ref,
                operation_input=operation_input,
                inline_guidance=("formal_plan_reader" in reading_context or "responsibility_index" in reading_context),
            )
        except BundleSkillUnavailable as error:
            if error.code in {
                "codex_operation_identity_conflict", "codex_operation_spool_invalid",
            }:
                from meta_research.bundle_dispatch_recovery import recover_rejected_target_batch

                recover_rejected_target_batch(self, request, operation_name)
            raise
        strategy_update = output.get("strategy_update")
        rationale = output.get("rationale")
        if (
            session_ref is None
            or not isinstance(strategy_update, dict)
            or not isinstance(rationale, str)
        ):
            checkpoint_session = session_ref or request.native_session_ref
            if checkpoint_session is None:
                raise BundleSkillUnavailable("codex_bundle_target_batch_invalid")
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name=operation_name,
                native_session_ref=checkpoint_session,
                failure_code="bundle_review_result_contract_invalid",
                detail_code="codex_bundle_target_batch_invalid",
            )
        from meta_research.bundle_output_assembly import assemble_strategy_update
        from meta_research.bundle_target_contract import BundleTargetContractError
        try:
            strategy_update = assemble_strategy_update(strategy_update, request.plan_document)
        except BundleTargetContractError as error:
            raise self._sealed_result_failure(job_ref=request.job_ref, operation_name=operation_name,
                native_session_ref=session_ref, failure_code="bundle_review_result_contract_invalid",
                detail_code=str(error), rejected_candidate=output) from error
        result = BundleTargetBatchResult(
            strategy_update=cast(dict[str, object], strategy_update),
            rationale=rationale,
            native_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        try:
            validate_bundle_target_batch_result(request, result)
        except BundleSkillContractError as error:
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name=operation_name,
                native_session_ref=session_ref,
                failure_code="bundle_review_result_contract_invalid",
                detail_code=str(error),
            ) from error
        return result


def _bundle_output_assembly_prompt() -> str:
    if os.environ.get("META_RESEARCH_BUNDLE_OUTPUT_MODE") == "full":
        return "本次按完整输出 schema 携带 semantic_inputs 与 semantic_delta，逐字保留本次 Plan 的 Brief 原文。"
    return "输出只选择 experiment_key、完成单元和固定条件；冻结 Brief 的 semantic_inputs 与 semantic_delta 由装配层按本次 Plan 原文补入。"


def _provider_correction_prompt(feedback: dict[str, object] | None) -> str:
    if not feedback:
        return ""
    return (
        "上一份输出未被接纳。继续当前 native Session，结合保留的上一份输出和下列 "
        "Owner 反馈作局部修正，保留已接纳的 Target/Commit 与冻结研究合同。"
        "修复事实或引用，不通过改称已完成来避开失败。\n"
        f"provider_correction_feedback={canonical_json(feedback)}"
    )


@prepared_resource
def _bundle_skill_resources() -> dict[str, str]:
    package = files("meta_research.skills.bundle_stage")
    resources = (
        ("SKILL.md", package / "SKILL.md"),
        ("references/contract.md", package / "references" / "contract.md"),
        (
            "references/owner-operations.md",
            package / "references" / "owner-operations.md",
        ),
    )
    try:
        prepare_cycle_role_resources()
        return {
            **shared_reference_resources(),
            **{name: cycle_role_skill_text("bundle") if name == "SKILL.md" else resource.read_text(encoding="utf-8") for name, resource in resources},
        }
    except (OSError, UnicodeError, ModuleNotFoundError) as error:
        raise BundleSkillUnavailable("bundle_skill_resource_unavailable") from error


def _bundle_skill_instruction_resources() -> dict[str, str]:
    """Return the complete, prose-only root-lifecycle instruction set."""

    return _bundle_skill_resources()


def _bundle_skill_instructions(resources: dict[str, str] | None = None) -> str:
    # Load and bind the complete resource set even though the native Harness
    # maintains the role method and references are read only when needed.
    if resources is None:
        _bundle_skill_instruction_resources()
    return (
        "Role skill source: " + str(Path(__file__).with_name("skills") / "bundle_stage" / "SKILL.md")
        + "\n角色方法由 Harness 指令维持；按当前工作读取所需合同及 Owner 操作参考。"
        "Resolve role reference links relative to this source directory."
    )


def _rolling_operation_identity(request: BundleDispatchRequest | BundleTargetBatchRequest) -> dict[str, object]:
    """Business identity recorded beside, independently of, the reading layout."""
    return {**{name: getattr(request, name) for name in (
        "stage_request_ref", "cycle_ref", "run_ref", "attempt_ref", "fence_ref",
        "root_session_ref", "graph_ref")},
        "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()),
        "cycle_position": request.cycle_position,
        "native_replacement_context_hash": canonical_hash(request.native_replacement_context)}


def _validate_rolling_native_request(request) -> None:
    if isinstance(request.native_session_ref, str) and request.native_session_ref:
        if request.native_replacement_context is not None:
            raise BundleSkillUnavailable("bundle_native_replacement_context_invalid")
        return
    context = request.native_replacement_context
    if (request.native_session_ref is not None or not isinstance(context, dict)
            or context.get("kind") != "native_session_replacement"
            or context.get("stage_request_ref") != request.stage_request_ref
            or context.get("retired_attempt_ref") == request.attempt_ref
            or any(not isinstance(context.get(field), str) or not context[field] for field in (
                "safe_point_ref", "retired_attempt_ref", "retired_native_session_ref", "provider_operation_ref"))
            or not isinstance(context.get("frozen_stage_input"), dict)
            or not isinstance(context.get("isolation_basis"), dict)
            or context["isolation_basis"].get("context_pack_hash") != canonical_hash(context["frozen_stage_input"])
            or any(context.get(field) != context["isolation_basis"].get(field) for field in (
                "kind", "stage_request_ref", "safe_point_ref", "retired_attempt_ref",
                "retired_native_session_ref", "provider_operation_ref"))
            or not isinstance(context.get("accepted_target_graph"), dict)
            or context["accepted_target_graph"].get("graph_ref") != request.graph_ref):
        raise BundleSkillUnavailable("bundle_native_replacement_context_invalid")


def _rolling_native_result_matches(request, native_session_ref) -> bool:
    if not isinstance(native_session_ref, str) or not native_session_ref:
        return False
    if request.native_session_ref is not None:
        return native_session_ref == request.native_session_ref
    try:
        _validate_rolling_native_request(request)
    except BundleSkillUnavailable:
        return False
    return native_session_ref != request.native_replacement_context["retired_native_session_ref"]


def _rolling_position_prompt(request: BundleDispatchRequest | BundleTargetBatchRequest) -> str:
    from meta_research.cycle_position import validate_cycle_position
    position = request.cycle_position
    try:
        validate_cycle_position(position, cycle_ref=request.cycle_ref)
    except (TypeError, ValueError) as error:
        raise BundleSkillUnavailable("bundle_cycle_position_invalid") from error
    return "cycle_position=" + canonical_json({**position, "stage": "bundle"}) + "\n"


def _schema_template_request() -> BundleSkillRequest:
    plan = {
        "gap_set": ["__gap__"],
        "experiment_briefs": [
            {
                "experiment_key": "__experiment__",
                "gap_obligation_keys": ["__gap__"],
                "goal": "__goal__",
                "characteristics": "__characteristics__",
                "boundary_constraints": "__boundary__",
                "semantic_delta": "__delta__",
                "contributing_idea_refs": ["__idea__"],
            }
        ],
        "bundle_disposition": "experiments_required",
    }
    context = {
        "cycle_position": {"cycle_ref": "__cycle__", "round": 1,
            "cycle_focus": "__focus__", "focus_source": {"request_ref": "__request__"}},
        "accepted_question_binding": {"question_ref": "__question__"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "__formal_plan__",
            "plan_document": plan,
        },
    }
    checkpoint_payload = {
        "schema_ref": BUNDLE_INBOX_CHECKPOINT_SCHEMA,
        "checkpoint_ref": "__inbox_checkpoint__",
        "run_ref": "__run__",
        "attempt_ref": "__attempt__",
        "fence_ref": "__fence__",
        "checkpoint_revision": 1,
        "cursor": 0,
        "generation": 0,
        "batch_hash": "0" * 64,
        "closed": True,
    }
    inbox_checkpoint = {
        **checkpoint_payload,
        "checkpoint_hash": canonical_hash(checkpoint_payload),
        "receipt": {
            "status": "accepted",
            "issuer": "agent_runtime",
            "kind": BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
            "receipt_ref": "__inbox_checkpoint_receipt__",
            "subject_ref": "__inbox_checkpoint__",
            "payload_hash": "0" * 64,
        },
    }
    return BundleSkillRequest(
        stage_request_ref="__request__",
        run_ref="__run__",
        attempt_ref="__attempt__",
        fence_ref="__fence__",
        cycle_ref="__cycle__",
        question_ref="__question__",
        formal_plan_ref="__formal_plan__",
        context_pack_ref="__context__",
        context_pack_hash="0" * 64,
        context_pack=context,
        plan_document=plan,
        root_session_ref="__root_session__",
        runtime_binding=BundleRuntimeBinding(
            packaged_skill_bundle_hash="0" * 64,
            instruction_set_hash="0" * 64,
            model_ref="__model__",
            harness_adapter_ref="__harness__",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        ),
        inbox_checkpoint=inbox_checkpoint,
    )


def _target_plan_envelope_schema(
    request: BundleSkillRequest,
) -> dict[str, object]:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target_plan": _target_plan_schema(request)
                },
                "required": ["target_plan"],
            },
            _exhaustion_assessment_envelope_schema(request),
        ]
    }


def _review_schema(
    request: BundleSkillRequest,
    *,
    draft: BundleSkillDraft | None = None,
) -> dict[str, object]:
    if draft is not None and draft.output_kind == "exhaustion_assessment":
        return _exhaustion_review_schema(
            reviewed_assessment_hash=canonical_hash(draft.draft)
        )
    target_review = _target_plan_review_schema(request)
    if draft is not None:
        if draft.output_kind != "target_plan":
            raise BundleSkillUnavailable("bundle_skill_output_kind_invalid")
        return target_review
    return target_review


def _target_plan_review_schema(
    request: BundleSkillRequest,
) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reviewer_agent_ref": text,
            "findings": {
                "type": "array",
                "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": text,
                        "category": {
                            "type": "string",
                            "enum": [
                                "lineage",
                                "dag",
                                "dedup",
                                "feasibility",
                                "owner_boundary",
                            ],
                        },
                        "message": text,
                    },
                    "required": ["finding_id", "category", "message"],
                },
            },
            "final_target_plan": _target_plan_schema(request),
            "dispositions": {
                "type": "array",
                "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": text,
                        "action": {
                            "type": "string",
                            "enum": ["revised", "not_adopted"],
                        },
                        "rationale": text,
                    },
                    "required": ["finding_id", "action", "rationale"],
                },
            },
        },
        "required": [
            "reviewer_agent_ref",
            "findings",
            "final_target_plan",
            "dispositions",
        ],
    }


def _exhaustion_review_schema(
    *, reviewed_assessment_hash: str | None
) -> dict[str, object]:
    reviewed_hash_schema: dict[str, object] = _sha256_schema()
    if reviewed_assessment_hash is not None:
        reviewed_hash_schema = {"const": reviewed_assessment_hash}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA},
            "reviewer_agent_ref": _ref_schema(),
            "reviewed_assessment_hash": reviewed_hash_schema,
            "accepted": {"const": True},
            "findings": {
                "type": "array",
                "maxItems": 0,
                "items": {"type": "string"},
            },
        },
        "required": [
            "schema_ref",
            "reviewer_agent_ref",
            "reviewed_assessment_hash",
            "accepted",
            "findings",
        ],
    }


def _exhaustion_assessment_envelope_schema(
    request: BundleSkillRequest,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "exhaustion_assessment": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schema_ref": {"const": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA},
                    "completion_contract": _normalized_completion_contract_schema(
                        request
                    ),
                    "exploration_records": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                        "items": _exhaustion_exploration_record_schema(request),
                    },
                },
                "required": [
                    "schema_ref",
                    "completion_contract",
                    "exploration_records",
                ],
            }
        },
        "required": ["exhaustion_assessment"],
    }


def _exhaustion_exploration_record_schema(
    request: BundleSkillRequest,
) -> dict[str, object]:
    del request
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "record_ref": _ref_schema(),
            "experiment_key": _plan_semantic_text_schema(),
            "measurement_unit_key": _ref_schema(),
            "held_fixed_bindings": {
                "type": "array",
                "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                "uniqueItems": True,
                "items": _held_fixed_binding_schema(),
            },
            "route": _route_schema(),
            "route_disposition": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "disposition_ref": _ref_schema(),
                    "route_ref": _ref_schema(),
                    "experiment_keys": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                        "uniqueItems": True,
                        "items": _plan_semantic_text_schema(),
                    },
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "duplicate_frozen_semantics",
                            "semantically_ineligible",
                            "attempted_rejected",
                        ],
                    },
                    "required_changes": {
                        "type": "array",
                        "maxItems": 0,
                        "items": {"type": "string"},
                    },
                    "evidence_refs": _ref_array_schema(min_items=1),
                    "external_reconciliations": {
                        "type": "array",
                        "maxItems": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "operation_ref": _ref_schema(),
                                "receipt": _receipt_schema(),
                                "outcome": {
                                    "type": "string",
                                    "enum": sorted(
                                        TERMINAL_EXTERNAL_OUTCOMES
                                    ),
                                },
                            },
                            "required": [
                                "operation_ref",
                                "receipt",
                                "outcome",
                            ],
                        },
                    },
                },
                "required": [
                    "disposition_ref",
                    "route_ref",
                    "experiment_keys",
                    "outcome",
                    "required_changes",
                    "evidence_refs",
                    "external_reconciliations",
                ],
            },
            "frozen_semantic_fingerprint": _sha256_schema(),
        },
        "required": [
            "record_ref",
            "experiment_key",
            "measurement_unit_key",
            "held_fixed_bindings",
            "route",
            "route_disposition",
            "frozen_semantic_fingerprint",
        ],
    }


def _dispatch_schema(target_refs: tuple[str, ...]) -> dict[str, object]:
    del target_refs
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "wait", "replan_required"],
            },
            "selected_target_ref": {
                "anyOf": [
                    text,
                    {"type": "null"},
                ],
            },
            "rationale": {**text, "maxLength": 512},
        },
        "required": ["action", "selected_target_ref", "rationale"],
    }


def _target_batch_schema(
    request: BundleSkillRequest | BundleTargetBatchRequest,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "strategy_update": _formal_strategy_update_schema(
                request,
                initial=False,
            ),
            "rationale": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            },
        },
        "required": ["strategy_update", "rationale"],
    }


def _target_plan_schema(
    request: BundleSkillRequest | BundleTargetBatchRequest,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": TARGET_PLAN_SCHEMA_REF},
            "kind": {"const": "TargetPlan"},
            "notes": {"type": "string"},
            "formal_plan_ref": {"const": request.formal_plan_ref},
            "context_pack_ref": {"const": request.context_pack_ref},
            "completion_contract": _normalized_completion_contract_schema(request),
            "initial_strategy_update": _formal_strategy_update_schema(
                request,
                initial=True,
            ),
            "source_bindings": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "formal_plan_ref": {"const": request.formal_plan_ref},
                    "plan_document_hash": {
                        "const": canonical_hash(request.plan_document)
                    },
                    "context_pack_ref": {"const": request.context_pack_ref},
                    "context_pack_hash": {"const": request.context_pack_hash},
                },
                "required": [
                    "formal_plan_ref",
                    "plan_document_hash",
                    "context_pack_ref",
                    "context_pack_hash",
                ],
            },
        },
        "required": [
            "schema_ref",
            "kind",
            "notes",
            "formal_plan_ref",
            "context_pack_ref",
            "completion_contract",
            "initial_strategy_update",
            "source_bindings",
        ],
    }


def _normalized_completion_contract_schema(
    request: BundleSkillRequest | BundleTargetBatchRequest,
) -> dict[str, object]:
    briefs = _plan_briefs(request)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": NORMALIZED_COMPLETION_CONTRACT_SCHEMA_REF},
            "plan_document_hash": {"const": canonical_hash(request.plan_document)},
            "experiments": {
                "type": "array",
                "minItems": len(briefs),
                "maxItems": len(briefs),
                "items": _normalized_experiment_schema(),
            },
        },
        "required": ["schema_ref", "plan_document_hash", "experiments"],
    }


def _normalized_experiment_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "brief": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "experiment_key": _plan_semantic_text_schema(),
                    "held_fixed_slots": _ref_array_schema(min_items=0),
                    "required_measurement_unit_keys": _ref_array_schema(
                        min_items=1
                    ),
                },
                "required": [
                    "experiment_key",
                    "held_fixed_slots",
                    "required_measurement_unit_keys",
                ],
            },
        },
        "required": ["brief"],
    }


def _formal_strategy_update_schema(
    request: BundleSkillRequest | BundleTargetBatchRequest,
    *,
    initial: bool,
) -> dict[str, object]:
    revision: dict[str, object]
    if initial:
        revision = {"const": 1}
    elif isinstance(request, BundleTargetBatchRequest):
        revision = {"const": request.base_generation + 2}
    else:
        revision = {"type": "integer", "minimum": 2}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": FORMAL_STRATEGY_UPDATE_SCHEMA_REF},
            "revision": revision,
            "candidates": {
                "type": "array",
                "minItems": 1 if initial else 0,
                "maxItems": MAX_BUNDLE_TARGETS,
                "items": _formal_target_candidate_schema(
                    request,
                ),
            },
            "requires_accepted_labels": _ref_array_schema(min_items=0),
            "strategy_complete": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "notes",
            "schema_ref",
            "revision",
            "candidates",
            "requires_accepted_labels",
            "strategy_complete",
        ],
    }


def _formal_target_candidate_schema(
    request: BundleSkillRequest | BundleTargetBatchRequest,
) -> dict[str, object]:
    text = _ref_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": FORMAL_TARGET_CANDIDATE_SCHEMA_REF},
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "local_label": text,
                    "experiment_keys": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": _plan_semantic_text_schema(),
                    },
                    "measurement_unit_keys": {
                        **_ref_array_schema(min_items=1),
                        "maxItems": 1,
                    },
                    "held_fixed_bindings": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": _held_fixed_binding_schema(),
                    },
                    "implementation_revision_ref": text,
                    "code_changed": {"type": "boolean"},
                    "routes": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": _route_schema(),
                    },
                    "depends_on_labels": _ref_array_schema(min_items=0),
                    "direct_accepted_input_asset_refs": _ref_array_schema(
                        min_items=0
                    ),
                },
                "required": [
                    "local_label",
                    "experiment_keys",
                    "measurement_unit_keys",
                    "held_fixed_bindings",
                    "implementation_revision_ref",
                    "code_changed",
                    "routes",
                    "depends_on_labels",
                    "direct_accepted_input_asset_refs",
                ],
            },
            "measurement_contract": _measurement_contract_schema(),
            "risk_class": {"type": "string", "enum": ["normal", "high"]},
        },
        "required": [
            "schema_ref",
            "candidate",
            "measurement_contract",
            "risk_class",
        ],
    }


def _measurement_contract_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {
                "const": MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF
            },
            "experiment_keys": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_EXPERIMENT_BRIEFS,
                "uniqueItems": True,
                "items": _plan_semantic_text_schema(),
            },
            "measurement_unit_key": _ref_schema(),
            "baseline_forward_contract": _domain_document_schema(),
            "variant_recipe": _domain_document_schema(),
            "evaluation_protocol_lineage": _domain_document_schema(),
            "protocol_version": _protocol_version_candidate_schema(),
            "checkpoint_policy": {
                "type": "string",
                "enum": ["forbidden", "optional", "required"],
                "description": "既有保存建议；Target Agent 按研究价值、未来复用与存储成本自主选择实际保存的状态与粒度。",
            },
            "result_schema_ref": _ref_schema(),
            "result_schema": _domain_document_schema(),
        },
        "required": [
            "schema_ref",
            "experiment_keys",
            "measurement_unit_key",
            "baseline_forward_contract",
            "variant_recipe",
            "evaluation_protocol_lineage",
            "protocol_version",
            "checkpoint_policy",
            "result_schema_ref",
            "result_schema",
        ],
    }


def _protocol_version_candidate_schema() -> dict[str, object]:
    rule = _frozen_rule_schema()
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF},
            "evaluation_data": _domain_document_schema(),
            "split": _domain_document_schema(),
            "preprocessing": _domain_document_schema(),
            "required_metrics": {
                "type": "array",
                "minItems": 0,
                "maxItems": 64,
                "uniqueItems": True,
                "items": _metric_definition_schema(),
            },
            "optional_metrics": {
                "type": "array",
                "minItems": 0,
                "maxItems": 64,
                "uniqueItems": True,
                "items": _metric_definition_schema(),
            },
            "internal_part_keys": _ref_array_schema(min_items=0),
            "aggregation": {"anyOf": [rule, {"type": "null"}]},
            "preregistered_stop_rules": {
                "type": "array",
                "minItems": 0,
                "maxItems": 1024,
                "uniqueItems": True,
                "items": _frozen_rule_schema(),
            },
        },
        "required": [
            "schema_ref",
            "evaluation_data",
            "split",
            "preprocessing",
            "required_metrics",
            "optional_metrics",
            "internal_part_keys",
            "aggregation",
            "preregistered_stop_rules",
        ],
    }


def _metric_definition_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "metric_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "definition": _domain_document_schema(),
        },
        "required": ["metric_key", "definition"],
    }


def _frozen_rule_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rule_ref": _ref_schema(),
            "rule": _domain_document_schema(),
        },
        "required": ["rule_ref", "rule"],
    }


def _domain_document_schema() -> dict[str, object]:
    return {
        _CODEX_JSON_OBJECT_STRING_MARKER: {
            "max_collection_items": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
            "max_depth": 64,
            "max_integer_abs": BUNDLE_CANONICAL_INTEGER_MAX_ABS,
            "max_nodes": BUNDLE_ROOT_MAX_NODES,
            "max_serialized_bytes": BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
            "max_string_bytes": BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES,
        }
    }


def _held_fixed_binding_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "semantic_slot": _ref_schema(),
            "implementation_revision_ref": _ref_schema(),
        },
        "required": ["semantic_slot", "implementation_revision_ref"],
    }


def _route_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route_ref": _ref_schema(),
            "known_external_operation_refs": _ref_array_schema(min_items=0),
        },
        "required": ["route_ref", "known_external_operation_refs"],
    }


def _receipt_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "receipt_ref": _ref_schema(),
            "subject_ref": _ref_schema(),
            "verified": {"const": True},
            "currentness_known": {"const": True},
            "current": {"const": True},
        },
        "required": [
            "receipt_ref",
            "subject_ref",
            "verified",
            "currentness_known",
            "current",
        ],
    }


def _plan_briefs(
    request: BundleSkillRequest | BundleTargetBatchRequest,
) -> list[dict[str, object]]:
    value = request.plan_document.get("experiment_briefs")
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _ref_schema() -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": 4096}


def _plan_semantic_text_schema() -> dict[str, object]:
    # Accepted Plan text is request-specific.  Exact equality to the frozen
    # Plan remains enforced by the Bundle domain validators after decode.
    return {"type": "string", "minLength": 1, "maxLength": 8192}


def _sha256_schema() -> dict[str, object]:
    return {"type": "string", "pattern": "^[0-9a-f]{64}$"}


def _ref_array_schema(*, min_items: int) -> dict[str, object]:
    return {
        "type": "array",
        "minItems": min_items,
        "maxItems": 1024,
        "uniqueItems": True,
        "items": _ref_schema(),
    }
