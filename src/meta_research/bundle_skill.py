from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
from importlib.resources import files
import json
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast
from urllib.parse import urlsplit

from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    BUNDLE_EXHAUSTION_REVIEW_RESPONSE_SCHEMA,
    BundleExhaustionReviewTrace,
    bundle_exhaustion_review_response_document,
    bundle_exhaustion_review_task_hash,
    validate_bundle_exhaustion_assessment,
)
from meta_research.bundle_contract import (
    MAX_BUNDLE_TARGETS,
    TARGET_PLAN_REVIEW_SCHEMA_REF,
    TARGET_PLAN_SCHEMA_REF,
    BundleContractError,
    validate_bundle_context_pack,
    validate_target_plan,
    validate_target_plan_review,
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
    BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
    TERMINAL_EXTERNAL_OUTCOMES,
)
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    ProviderTransportLimits,
    _DISABLED_CODEX_FEATURES,
    _codex_harness_manifest,
    _file_sha256,
    _verify_child_review_trace,
)
from meta_research.harness import (
    FullConformanceBinding,
    HarnessAdmissionError,
    ResidentMcpChannel,
)
from meta_research.owners.agent_runtime import (
    BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
    BUNDLE_INBOX_CHECKPOINT_SCHEMA,
    BundleRuntimeBinding,
    validate_bundle_inbox_checkpoint_projection,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import transport_key_hash
BundleSkillContractError = BundleContractError
BundleSkillUnavailable = IdeaSkillUnavailable
BUNDLE_PROVIDER_TRANSPORT_LIMITS = ProviderTransportLimits(
    prompt_max_bytes=64 * 1024 * 1024,
    stream_max_bytes=64 * 1024 * 1024,
    result_max_bytes=16 * 1024 * 1024,
)
BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES = (
    BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes
)
_FULL_CONFORMANCE_CAPABILITY = "harness-full-conformance-v1"
_FULL_CONFORMANCE_MCP_PREFIX = "harness-full-conformance:semantic-mcp-"
_FULL_CONFORMANCE_RESOURCE_PREFIX = "harness-artifact:full-conformance-"


class BundleFullConformanceAuthority(Protocol):
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


def bind_bundle_runtime_to_full_conformance(
    binding: BundleRuntimeBinding,
    conformance: FullConformanceBinding,
) -> BundleRuntimeBinding:
    """Freeze one current Harness evidence set into Bundle AR admission.

    The operation is idempotent so both the Stage boundary and the production
    adapter can independently apply the same current evidence without creating
    duplicate bindings.
    """

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
    predecessor_rejections: tuple[dict[str, object], ...] = ()
    native_session_ref: str | None = None
    job_ref: str | None = None


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
    reviewer_agent_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class BundleExhaustionSkillResult:
    reviewed_assessment: dict[str, object]
    reviewed_assessment_hash: str
    findings: tuple[str, ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str
    review_trace: BundleExhaustionReviewTrace


@dataclass(frozen=True)
class BundleDispatchRequest:
    stage_request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    graph_ref: str
    generation: int
    frontier: tuple[dict[str, object], ...]
    state: dict[str, object]
    root_session_ref: str
    native_session_ref: str
    runtime_binding: BundleRuntimeBinding
    inbox_checkpoint: dict[str, object]
    job_ref: str | None = None


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
    native_session_ref: str
    runtime_binding: BundleRuntimeBinding
    inbox_checkpoint: dict[str, object]
    job_ref: str | None = None


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
        or not request.native_session_ref
        or result.native_session_ref != request.native_session_ref
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
    if request.frontier and result.action != "dispatch":
        raise BundleSkillContractError("bundle_dispatch_requires_authoritative_blocker")
    if (
        result.action not in {"dispatch", "wait", "replan_required"}
        or (
            result.action == "dispatch"
            and (not result.selected_target_ref or len(matching) != 1)
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
        or not request.native_session_ref
        or result.native_session_ref != request.native_session_ref
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
    if result.output_kind == "target_plan":
        return validate_target_plan(
            result.draft,
            formal_plan_ref=request.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            plan_document=request.plan_document,
        )
    if result.output_kind == "exhaustion_assessment":
        try:
            return validate_bundle_exhaustion_assessment(
                result.draft,
                plan_document=request.plan_document,
            )
        except OwnerConflict as error:
            raise BundleSkillContractError(error.code) from error
    raise BundleSkillContractError("bundle_skill_output_kind_invalid")


def validate_bundle_skill_result(
    request: BundleSkillRequest, result: BundleSkillResult
) -> tuple[str, str, str]:
    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent" or result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise BundleSkillContractError("target_plan_review_not_independent")
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
    review_hash = validate_target_plan_review(
        review,
        reviewed_draft_hash=draft_hash,
        final_target_plan_hash=final_hash,
    )
    return draft_hash, final_hash, review_hash


def validate_bundle_exhaustion_skill_result(
    request: BundleSkillRequest,
    result: BundleExhaustionSkillResult,
) -> tuple[str, str]:
    """Validate the immutable assessment and exact independent review."""

    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent" or result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise BundleSkillContractError("bundle_exhaustion_review_not_independent")
    try:
        assessment_hash = validate_bundle_exhaustion_assessment(
            result.reviewed_assessment,
            plan_document=request.plan_document,
        )
    except OwnerConflict as error:
        raise BundleSkillContractError(error.code) from error
    if (
        result.reviewed_assessment_hash != assessment_hash
        or result.findings
        or result.review_trace.run_ref != request.run_ref
        or result.review_trace.attempt_ref != request.attempt_ref
        or result.review_trace.fence_ref != request.fence_ref
        or result.review_trace.primary_session_ref != result.primary_session_ref
        or result.review_trace.reviewer_agent_ref != result.reviewer_agent_ref
        or result.review_trace.reviewed_assessment_hash != assessment_hash
        or result.review_trace.review_task_hash
        != bundle_exhaustion_review_task_hash(
            reviewed_assessment_hash=assessment_hash,
            formal_plan_content_hash=canonical_hash(request.plan_document),
        )
    ):
        raise BundleSkillContractError("bundle_exhaustion_review_binding_invalid")
    review_document = bundle_exhaustion_review_response_document(
        reviewer_agent_ref=result.reviewer_agent_ref,
        reviewed_assessment_hash=assessment_hash,
    )
    review_hash = canonical_hash(review_document)
    if result.review_trace.review_response_hash != review_hash:
        raise BundleSkillContractError("bundle_exhaustion_review_binding_invalid")
    return assessment_hash, review_hash


def review_record(
    result: BundleSkillResult,
    *,
    draft_hash: str,
    final_target_plan_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": TARGET_PLAN_REVIEW_SCHEMA_REF,
        "review_mode": result.review_mode,
        "reviewer_agent_ref": result.reviewer_agent_ref,
        "reviewed_draft_hash": draft_hash,
        "findings": list(result.findings),
        "dispositions": list(result.dispositions),
        "final_target_plan_hash": final_target_plan_hash,
        "independent": True,
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


class CodexBundleSkillAdapter(CodexPlanSkillAdapter):
    """Production Bundle adapter using one managed native root Session."""

    _provider_transport_limits = BUNDLE_PROVIDER_TRANSPORT_LIMITS

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = "gpt-5.4",
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
            BundleFullConformanceAuthority | None
        ) = None
        self._resident_mcp_base_url: str | None = None
        self._resident_mcp_channels: dict[
            tuple[str, str], ResidentMcpChannel
        ] = {}

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
    ) -> tuple[dict[str, object], str | None, str]:
        authority = self._full_conformance_authority
        base_url = self._resident_mcp_base_url
        if authority is None or base_url is None:
            raise BundleSkillUnavailable("bundle_semantic_mcp_unavailable")
        operation_ids = self._bundle_semantic_operation_ids()
        try:
            conformance = authority.require_full_conformance_binding()
        except HarnessAdmissionError as error:
            raise BundleSkillUnavailable(error.code) from error
        if not set(operation_ids) <= set(conformance.required_operation_ids):
            raise BundleSkillUnavailable(
                "bundle_semantic_mcp_conformance_incomplete"
            )
        channel_key = (job_ref or run_ref, operation_name)
        channel = self._resident_mcp_channels.get(channel_key)
        if channel is None:
            try:
                channel = authority.issue_resident_mcp_channel(
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                    capability_binding_hash=canonical_hash(
                        runtime_binding.as_dict()
                    ),
                    operation_ids=operation_ids,
                )
            except HarnessAdmissionError as error:
                raise BundleSkillUnavailable(error.code) from error
            self._resident_mcp_channels[channel_key] = channel
        endpoint_ref = channel.binding.endpoint_ref
        if not endpoint_ref.startswith("/") or "?" in endpoint_ref or "#" in endpoint_ref:
            self._release_resident_channel(channel_key)
            raise BundleSkillUnavailable("bundle_semantic_mcp_endpoint_invalid")
        scope_binding_hash = canonical_hash(
            {
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

    def runtime_binding(self) -> BundleRuntimeBinding:
        resources = _bundle_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        schemas = {
            "target-plan-envelope": _target_plan_envelope_schema(
                _schema_template_request()
            ),
            "target-plan-review": _review_schema(_schema_template_request()),
            "target-dispatch": _dispatch_schema(("__target__",)),
            "target-batch": _target_batch_schema(_schema_template_request()),
        }
        binding = BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _bundle_skill_instructions(),
                    "adapter_source_hash": adapter_source_hash,
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
                f"package:meta_research.skills.bundle_stage/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in schemas.items()
            )
            + harness_artifacts
            + (
                "adapter-source:meta_research.bundle_skill@sha256:"
                f"{adapter_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"prompt={BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes};"
                f"stream={BUNDLE_PROVIDER_TRANSPORT_LIMITS.stream_max_bytes};"
                f"result={BUNDLE_PROVIDER_TRANSPORT_LIMITS.result_max_bytes}",
                "provider-timeout-seconds:" + format(self._timeout_seconds, ".17g"),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:" + transport_key_hash(transport_key),
            ),
        )
        if self._full_conformance_authority is None:
            return binding
        return bind_bundle_runtime_to_full_conformance(
            binding,
            self._full_conformance_authority.require_full_conformance_binding(),
        )

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        _validate_request(request)
        if request.runtime_binding != self.runtime_binding():
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            '你是 Bundle 根 Agent。返回且只返回下列两个闭合分支之一：'
            '{"target_plan": ...} 的 formal v3，或固定合同中的 '
            '{"exhaustion_assessment": ...}。不得同时返回、混合或扩展两分支。'
            "target_plan 分支必须是 formal v3："
            "直接序列化完整 NormalizedCompletionContract 与 initial StrategyUpdate，"
            "不得输出 legacy v2 targets/gap-count 切片。先从全部 ExperimentBrief 的 "
            "Goal、Characteristics、BoundaryConstraints、SemanticDelta 与 "
            "required_measurement_unit_keys 冻结所有 measurement completion cells；候选列表"
            "不能反向缩小这份合同。每个 formal TargetCandidate 必须恰含一个 cell、完整 "
            "reuse tier/source/version/content/license/patch/eligibility（按层级适用）、"
            "held-fixed slot revision、语义保持 route、真实 dependency/direct accepted asset "
            "refs。canonical TargetCandidate 本体不扩展；"
            "formal wrapper 另以必填 risk_class=normal|high 冻结 Owner admission metadata，"
            "缺失或未知风险必须 fail closed，绝不能默认 normal。"
            "同一 formal wrapper 还必须冻结完整纯领域 measurement_contract，且其 "
            "ExperimentKeys 与唯一 measurement cell 必须和 canonical candidate exact 一致。"
            "合同完整携带 baseline forward、variant recipe、evaluation protocol lineage，"
            "以及包含 evaluation data、split、preprocessing 的 ProtocolVersion candidate；"
            "required/optional ordered Metric definitions 必须按声明顺序逐项携带 MetricKey "
            "与非空完整 definition，"
            "不得只列名字。internal parts 保持声明顺序；有 parts 就必须同时冻结 aggregation "
            "rule ref 与完整 rule content，无 parts 则禁止 aggregation。每条 preregistered stop "
            "rule 同样冻结 ref 与完整 content，并冻结 checkpoint policy、result schema ref 与完整"
            " result schema content。这四类领域文档和规则不得在根层携带 provider、adapter、"
            "image、command 或 execution 路由。微型均值 runner 只是兼容样例，不是 Target "
            "类型，也不是正式 Bundle 合同。TargetPlan 不选择 provider、adapter "
            "或 execution payload；实际执行只能由后续 TargetRun 根 Agent 在代码、自检、"
            "fresh child code review 与 Owner admission 后调用单一通用执行端口。"
            "当前 initial StrategyUpdate revision=1、"
            "requires_accepted_labels=[]、candidates 非空；若该批已精确覆盖全部 frozen "
            "cells，可在同一 update 令 strategy_complete=true，否则为 false。后续同样可在"
            "非空追加批次原子 seal；空 candidates 时则必须 complete=true。只 append/seal，"
            "不得回写旧 Target。Target identity、DAG、frontier、TargetCommit 都由 Research "
            "Graph 接纳；receipt 必须来自实际 Owner 操作，不得自造。Agent Session "
            "绝不是 Target 或 TargetRun；risk_class 只供 Owner/Human Collaboration 的正式 "
            "admission 使用，不进入 canonical TargetCandidate 本体。\n"
            "只有在冻结 FormalPlan 内确实没有实质不同且可接纳的候选，"
            "才能使用 exhaustion_assessment。该分支必须序列化完整 "
            "NormalizedCompletionContract，并对每个 measurement cell 的每条已探索"
            "保语义 route 给出一条记录（一个 cell 可有多 route）。"
            "每条记录必须携带 exact held-fixed bindings、RouteSpec、完整 RouteDisposition "
            "与可重算 frozen fingerprint。未执行且经冻结语义审阅关闭的 route 只能标为 "
            "duplicate_frozen_semantics 或 semantically_ineligible；实际提交并被 Owner "
            "拒绝的 route 只能标为 attempted_rejected，并在 evidence_refs 中精确引用下方"
            " issuer-verified predecessor rejection inventory。Route 声明过的每个 external "
            "operation 必须有同 subject 的 terminal reconciliation receipt。无历史时"
            " predecessor_rejections 必须为空。任何可执行 route、semantic "
            "barrier、active/blocked/pending/unknown work、HumanRequest、未对账 external "
            "operation 或 accepted-unconsumed result 都禁止该分支。失败次数、"
            "attempt 上限、耗时、费用和只失败一条 route 都不是耗尽证明。\n"
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
            f"plan_document={canonical_json(request.plan_document)}"
        )
        output, session_ref, _stdout = self._invoke_with_resident_mcp(
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
        )
        if session_ref is None or type(output) is not dict:
            raise BundleSkillUnavailable("codex_bundle_primary_invalid")
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
        raise BundleSkillUnavailable("codex_bundle_primary_invalid")

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult | BundleExhaustionSkillResult:
        _validate_request(request)
        if (
            request.runtime_binding != self.runtime_binding()
            or request.native_session_ref != draft.primary_session_ref
        ):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        if draft.output_kind == "exhaustion_assessment":
            return self._review_exhaustion_assessment(request, draft)
        if draft.output_kind != "target_plan":
            raise BundleSkillUnavailable("bundle_skill_output_kind_invalid")
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "在当前 Bundle 根 Session 内使用 Harness 原生 spawn_agent，以 "
            'fork_turns="none" 启动一个短命、全新上下文 child reviewer，并 wait '
            "到完成。它只审查 FormalPlan lineage、"
            "GapSet 闭合、去重、DAG、可执行性与 Owner 边界，不批准 Target。根 Agent "
            "逐条处置 finding 后返回 reviewer_agent_ref、findings、final_target_plan、"
            "dispositions。审查必须覆盖完整 normalization/cells、reuse proof、held-fixed、"
            "implementation revision 与 initial append-only update，并逐 Target 检查 exact "
            "ExperimentKeys/cell 绑定及完整 measurement contract：四份领域文档、ordered "
            "Metric definitions、parts/aggregation、preregistered stop rules、checkpoint policy "
            "和完整 result schema 都必须进入所审 exact revision/hash。不得把 legacy v2 "
            "最小切片判成正式合同；TargetPlan 不得选择执行 provider/adapter，执行资格和"
            "通用 operation handle 只能由后续 TargetRun Owner 门禁产生。不得创建第二个"
            "顶层 Session。\n"
            f"formal_plan_ref={request.formal_plan_ref}\n"
            f"inbox_checkpoint={canonical_json(request.inbox_checkpoint)}\n"
            "predecessor_rejections_hash="
            f"{canonical_hash(list(request.predecessor_rejections))}\n"
            "predecessor_rejections="
            f"{canonical_json(list(request.predecessor_rejections))}\n"
            f"plan_document={canonical_json(request.plan_document)}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        output, session_ref, stdout = self._invoke_with_resident_mcp(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding,
            operation_name="review",
            prompt=prompt,
            schema=_review_schema(request, draft=draft),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
        )
        reviewer = output.get("reviewer_agent_ref")
        findings = output.get("findings")
        final = output.get("final_target_plan")
        dispositions = output.get("dispositions")
        if (
            session_ref != draft.primary_session_ref
            or not isinstance(reviewer, str)
            or not isinstance(findings, list)
            or not isinstance(final, dict)
            or not isinstance(dispositions, list)
        ):
            raise BundleSkillUnavailable("codex_bundle_review_invalid")
        _verify_child_review_trace(
            stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
        )
        return BundleSkillResult(
            reviewed_draft=draft.draft,
            final_target_plan=cast(dict[str, object], final),
            findings=tuple(cast(dict[str, str], item) for item in findings),
            dispositions=tuple(cast(dict[str, str], item) for item in dispositions),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer,
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
        task_hash = bundle_exhaustion_review_task_hash(
            reviewed_assessment_hash=assessment_hash,
            formal_plan_content_hash=canonical_hash(request.plan_document),
        )
        child_task = (
            "你是一个全新上下文的 Bundle exhaustion 审查 child。"
            "严格审查整份 primary assessment，不得只抽样单个 cell 或 route；"
            "核对 exact completion cells、所有已声明 routes、不存在 frozen-contract "
            "内实质不同可接纳候选，也不存在应归为 replan 的 semantic "
            "barrier。拒绝 attempt/count/time/cost shortcut。只返回给根 Agent "
            "明确的 accepted 或 findings；不调用 Owner 写入。\n"
            f"review_task_hash={task_hash}\n"
            f"reviewed_assessment_hash={assessment_hash}\n"
            f"formal_plan_content_hash={canonical_hash(request.plan_document)}\n"
            f"inbox_checkpoint={canonical_json(request.inbox_checkpoint)}\n"
            "predecessor_rejections_hash="
            f"{canonical_hash(list(request.predecessor_rejections))}\n"
            "predecessor_rejections="
            f"{canonical_json(list(request.predecessor_rejections))}\n"
            f"reviewed_assessment={canonical_json(draft.draft)}"
        )
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "在当前 Bundle 根 Session 内使用 Harness 原生 spawn_agent，"
            'fork_turns="none"，且将下面 child_task 原样作为 prompt。'
            "wait 直到该独立 child terminal completed。根 Agent 不得自审，"
            "不得改写 primary assessment。只有 child 无 finding 时，"
            "返回闭合 review response：reviewer_agent_ref 必须是该 child identity，"
            "reviewed_assessment_hash 必须是下面 exact hash，accepted=true，findings=[]。\n"
            f"child_task={canonical_json(child_task)}\n"
            f"reviewed_assessment_hash={assessment_hash}"
        )
        output, session_ref, stdout = self._invoke_with_resident_mcp(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding,
            operation_name="review",
            prompt=prompt,
            schema=_review_schema(request, draft=draft),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
        )
        reviewer = output.get("reviewer_agent_ref")
        findings = output.get("findings")
        if (
            session_ref != draft.primary_session_ref
            or type(reviewer) is not str
            or type(findings) is not list
            or output
            != bundle_exhaustion_review_response_document(
                reviewer_agent_ref=reviewer,
                reviewed_assessment_hash=assessment_hash,
            )
        ):
            raise BundleSkillUnavailable("codex_bundle_exhaustion_review_invalid")
        _verify_child_review_trace(
            stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
            expected_spawn_prompt=child_task,
        )
        spawn_hash, completion_hash = _child_review_event_hashes(
            stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
        )
        unsigned_trace = BundleExhaustionReviewTrace(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            fence_ref=request.fence_ref,
            primary_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
            reviewed_assessment_hash=assessment_hash,
            review_task_hash=task_hash,
            review_response_hash=canonical_hash(output),
            spawn_event_hash=spawn_hash,
            completion_event_hash=completion_hash,
            transport_seal="0" * 64,
        )
        trace = replace(
            unsigned_trace,
            transport_seal=self._bundle_exhaustion_trace_seal(
                unsigned_trace,
                runtime_binding_hash=canonical_hash(
                    request.runtime_binding.as_dict()
                ),
            ),
        )
        result = BundleExhaustionSkillResult(
            reviewed_assessment=draft.draft,
            reviewed_assessment_hash=assessment_hash,
            findings=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer,
            adapter_kind=draft.adapter_kind,
            review_trace=trace,
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

    def execute(
        self, request: BundleSkillRequest
    ) -> BundleSkillResult | BundleExhaustionSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref), draft
        )

    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        _validate_inbox_checkpoint_projection(
            request.inbox_checkpoint,
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            fence_ref=request.fence_ref,
        )
        if request.runtime_binding != self.runtime_binding():
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        target_refs = tuple(cast(str, item["target_ref"]) for item in request.frontier)
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "继续使用当前 Bundle 根 Session。先读取这次 durable frontier 与 "
            "TargetCommit/blocker 摘要，再自主选择下一项可调度 Target。选择体现当前 "
            "优先级、依赖、已实现结果与局部阻塞；不得把 Agent tree 当成 Target DAG，"
            "不得选择 frontier 之外的 Target，也不得伪造 Owner receipt。有可执行 "
            "Target 时返回 dispatch；只有技术/授权等待返回 wait；只有冻结语义确需 "
            "改变才返回 replan_required。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"run_ref={request.run_ref}\n"
            f"attempt_ref={request.attempt_ref}\n"
            f"fence_ref={request.fence_ref}\n"
            f"graph_ref={request.graph_ref}\n"
            f"generation={request.generation}\n"
            f"inbox_checkpoint={canonical_json(request.inbox_checkpoint)}\n"
            f"frontier={canonical_json(list(request.frontier))}\n"
            f"state={canonical_json(request.state)}"
        )
        output, session_ref, _stdout = self._invoke_with_resident_mcp(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding,
            operation_name=f"dispatch-{request.generation}",
            prompt=prompt,
            schema=_dispatch_schema(target_refs),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
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
            raise BundleSkillUnavailable("codex_bundle_dispatch_invalid")
        result = BundleDispatchResult(
            action=action,
            selected_target_ref=selected_target_ref,
            rationale=rationale,
            native_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        validate_bundle_dispatch_result(request, result)
        return result

    def propose_target_batch(
        self, request: BundleTargetBatchRequest
    ) -> BundleTargetBatchResult:
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
        }
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "继续使用同一个 Bundle 根 Session。当前 batch 的全部 Target 已形成正式 "
            "TargetCommit。基于冻结 FormalPlan、当前 append-only Target 集和真实 commit "
            "closure，直接返回下一份 FormalStrategyUpdate。若仍需新的独立 measurement "
            "closure，update.candidates 非空且通常 strategy_complete=false；若该追加批次"
            "恰好补齐 immutable NormalizedCompletionContract 的全部 cells，可在同一非空 "
            "update 原子 seal；空 candidates 只允许作为 complete=true 的最终 seal。"
            "revision 必须恰为当前 revision+1；"
            "requires_accepted_labels 只引用真实已接纳 label，并同时进入每个自适应候选的 "
            "depends_on_labels。不得改写旧 Target/Commit，不得把 Agent tree 当 Target DAG，"
            "每个新 formal wrapper 仍必须携带与 candidate ExperimentKeys/cell exact 绑定的完整"
            "纯领域 measurement_contract；ordered Metric definitions、evaluation data/split/"
            "preprocessing、parts 与完整 aggregation rule、完整 preregistered stop rules、"
            "checkpoint policy 及 result schema 均不可缩水、改成名字列表或换成运行时路由。"
            "不得编造 Owner ref，也不得在 TargetPlan 中选择 provider/adapter 或伪造"
            "execution operation；后续 TargetRun 只能在 Owner 接受 exact implementation "
            "revision 后调用单一通用执行端口。\n"
            f"canonical_batch_context={canonical_json(prompt_context)}"
        )
        if len(prompt.encode("utf-8")) > BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES:
            raise BundleSkillUnavailable("bundle_target_batch_prompt_too_large")
        if request.runtime_binding != self.runtime_binding():
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        output, session_ref, _stdout = self._invoke_with_resident_mcp(
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding,
            operation_name=f"target-batch-{request.base_generation + 1}",
            prompt=prompt,
            schema=_target_batch_schema(request),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
        strategy_update = output.get("strategy_update")
        rationale = output.get("rationale")
        if (
            session_ref is None
            or not isinstance(strategy_update, dict)
            or not isinstance(rationale, str)
        ):
            raise BundleSkillUnavailable("codex_bundle_target_batch_invalid")
        result = BundleTargetBatchResult(
            strategy_update=cast(dict[str, object], strategy_update),
            rationale=rationale,
            native_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        validate_bundle_target_batch_result(request, result)
        return result


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
        return {
            name: resource.read_text(encoding="utf-8") for name, resource in resources
        }
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise BundleSkillUnavailable("bundle_skill_resource_unavailable") from error


def _bundle_skill_instruction_resources() -> dict[str, str]:
    """Return the complete, prose-only root-lifecycle instruction set."""

    return _bundle_skill_resources()


def _bundle_skill_instructions() -> str:
    return "\n\n".join(
        f"<!-- bundled resource: {name} -->\n{content}"
        for name, content in _bundle_skill_instruction_resources().items()
    )


def _child_review_event_hashes(
    stdout: str,
    *,
    root_session_ref: str,
    reviewer_agent_ref: str,
) -> tuple[str, str]:
    """Hash the exact successful spawn and terminal wait event sequence.

    `_verify_child_review_trace` is the semantic validator.  This helper only
    extracts the already-validated immutable transport evidence so the
    production adapter can seal it for AR.
    """

    spawns: list[dict[str, object]] = []
    terminal_waits: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if type(event) is not dict or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            type(item) is not dict
            or item.get("type") != "collab_tool_call"
            or item.get("status") != "completed"
            or item.get("sender_thread_id") != root_session_ref
        ):
            continue
        if item.get("tool") == "spawn_agent":
            spawns.append(cast(dict[str, object], event))
            continue
        if item.get("tool") != "wait":
            continue
        states = item.get("agents_states")
        if (
            type(states) is dict
            and type(states.get(reviewer_agent_ref)) is dict
            and cast(dict[str, object], states[reviewer_agent_ref]).get("status")
            == "completed"
        ):
            terminal_waits.append(cast(dict[str, object], event))
    if len(spawns) != 1 or not terminal_waits:
        raise BundleSkillUnavailable("codex_child_review_trace_invalid")
    return canonical_hash(spawns[0]), canonical_hash(terminal_waits)


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
    return {
        "oneOf": [
            target_review,
            _exhaustion_review_schema(reviewed_assessment_hash=None),
        ]
    }


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
            "findings": {"type": "array", "maxItems": 0},
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
    experiment_keys = [
        cast(str, brief["experiment_key"])
        for brief in _plan_briefs(request)
        if isinstance(brief.get("experiment_key"), str)
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "record_ref": _ref_schema(),
            "experiment_key": {
                "type": "string",
                "enum": experiment_keys or ["__missing_experiment__"],
            },
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
                        "items": {
                            "type": "string",
                            "enum": experiment_keys
                            or ["__missing_experiment__"],
                        },
                    },
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "duplicate_frozen_semantics",
                            "semantically_ineligible",
                            "attempted_rejected",
                        ],
                    },
                    "required_changes": {"type": "array", "maxItems": 0},
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
                    {
                        "type": "string",
                        "enum": list(target_refs) or ["__no_dispatch_target__"],
                    },
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
    experiment_schemas = [
        _normalized_experiment_schema(brief) for brief in briefs
    ]
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
                "items": {
                    "oneOf": experiment_schemas
                    or [{"type": "object", "maxProperties": 0}],
                },
            },
        },
        "required": ["schema_ref", "plan_document_hash", "experiments"],
    }


def _normalized_experiment_schema(brief: dict[str, object]) -> dict[str, object]:
    key = cast(str, brief["experiment_key"])
    semantic_delta = cast(str, brief["semantic_delta"])
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "semantic_inputs": _semantic_inputs_schema(brief),
            "brief": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "experiment_key": {"const": key},
                    "semantic_delta": {"const": semantic_delta},
                    "held_fixed_slots": _ref_array_schema(min_items=0),
                    "required_measurement_unit_keys": _ref_array_schema(
                        min_items=1
                    ),
                },
                "required": [
                    "experiment_key",
                    "semantic_delta",
                    "held_fixed_slots",
                    "required_measurement_unit_keys",
                ],
            },
        },
        "required": ["semantic_inputs", "brief"],
    }


def _semantic_inputs_schema(brief: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "experiment_key": {"const": brief["experiment_key"]},
            "goal": {"const": brief["goal"]},
            "characteristics": {"const": brief["characteristics"]},
            "boundary_constraints": {"const": brief["boundary_constraints"]},
            "semantic_delta": {"const": brief["semantic_delta"]},
        },
        "required": [
            "experiment_key",
            "goal",
            "characteristics",
            "boundary_constraints",
            "semantic_delta",
        ],
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
        },
        "required": [
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
    briefs = _plan_briefs(request)
    experiment_keys = [cast(str, brief["experiment_key"]) for brief in briefs]
    semantic_variants = [_semantic_inputs_schema(brief) for brief in briefs]
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
                        "items": {
                            "type": "string",
                            "enum": experiment_keys
                            or ["__missing_experiment__"],
                        },
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
                    "reuse_trace": _reuse_trace_schema(),
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
                    "reuse_trace",
                    "routes",
                    "depends_on_labels",
                    "direct_accepted_input_asset_refs",
                ],
            },
            "semantic_inputs": {
                "type": "array",
                "minItems": 1,
                "maxItems": max(1, len(briefs)),
                "items": {
                    "oneOf": semantic_variants
                    or [{"type": "object", "maxProperties": 0}],
                },
            },
            "measurement_contract": _measurement_contract_schema(
                experiment_keys
            ),
            "risk_class": {"type": "string", "enum": ["normal", "high"]},
        },
        "required": [
            "schema_ref",
            "candidate",
            "semantic_inputs",
            "measurement_contract",
            "risk_class",
        ],
    }


def _measurement_contract_schema(
    experiment_keys: list[str],
) -> dict[str, object]:
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
                "maxItems": max(1, len(experiment_keys)),
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": experiment_keys or ["__missing_experiment__"],
                },
            },
            "measurement_unit_key": _ref_schema(),
            "baseline_forward_contract": _domain_document_schema(),
            "variant_recipe": _domain_document_schema(),
            "evaluation_protocol_lineage": _domain_document_schema(),
            "protocol_version": _protocol_version_candidate_schema(),
            "checkpoint_policy": {
                "type": "string",
                "enum": ["forbidden", "optional", "required"],
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
                "minItems": 1,
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
        "type": "object",
        "minProperties": 1,
        "maxProperties": BUNDLE_PROJECTION_MAX_TUPLE_ITEMS,
        "propertyNames": {
            "not": {
                "enum": [
                    "adapter",
                    "adapter_kind",
                    "argv",
                    "command",
                    "container_image",
                    "entrypoint",
                    "execution",
                    "execution_payload",
                    "image",
                    "provider",
                    "provider_registry",
                    "runtime_binding",
                ]
            }
        },
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


def _reuse_trace_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tier_decisions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
                "items": _reuse_tier_decision_schema(),
            },
            "greenfield_exception": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": [
                            "simple-implementation",
                            "implementation-is-semantic-delta",
                        ],
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["tier_decisions", "greenfield_exception"],
    }


def _reuse_tier_decision_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tier": {
                "type": "string",
                "enum": [
                    "accepted-local",
                    "related-history",
                    "global-baseline-pool",
                    "mature-external",
                    "self-implementation",
                ],
            },
            "disposition": {
                "type": "string",
                "enum": ["selected", "rejected", "not_found", "not_applicable"],
            },
            "reason_ref": _ref_schema(),
            "source_proofs": {
                "type": "array",
                "uniqueItems": True,
                "items": _reuse_source_proof_schema(),
            },
        },
        "required": ["tier", "disposition", "reason_ref", "source_proofs"],
    }


def _reuse_source_proof_schema() -> dict[str, object]:
    optional_ref = {"anyOf": [_ref_schema(), {"type": "null"}]}
    optional_binding = {
        "anyOf": [_content_binding_schema(), {"type": "null"}]
    }
    optional_receipt = {"anyOf": [_receipt_schema(), {"type": "null"}]}
    properties = {
        "source_ref": _ref_schema(),
        "exact_version_ref": _ref_schema(),
        "implementation_revision_ref": _ref_schema(),
        "eligible_tier": {
            "type": "string",
            "enum": [
                "accepted-local",
                "related-history",
                "global-baseline-pool",
                "mature-external",
                "self-implementation",
            ],
        },
        "verification_receipt": _receipt_schema(),
        "implementation_binding": _content_binding_schema(),
        "implementation_acceptance_receipt": _receipt_schema(),
        "eligibility_anchor_ref": optional_ref,
        "eligibility_binding": optional_binding,
        "eligibility_receipt": optional_receipt,
        "license_ref": optional_ref,
        "content_hash_ref": optional_ref,
        "patch_ref": optional_ref,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _content_binding_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject_ref": _ref_schema(),
            "content_hash_ref": _sha256_schema(),
        },
        "required": ["subject_ref", "content_hash_ref"],
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
