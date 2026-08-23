from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meta_research.control_contract import (
    FORCE_FENCE_ACTIONS,
    TERMINAL_ACTIONS,
    signed_owner_preview,
    validate_control_payload,
)
from meta_research.database import Database
from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPaper,
    AcquisitionProvider,
    AcquisitionPreflightRequest,
    AcquisitionRuntimeBinding,
    AcquisitionSession,
    AcquisitionUnavailable,
    aggregate_batch_status,
    canonical_hash as acquisition_hash,
    validate_batch_request,
    validate_item_results,
    validate_preflight_result,
    validate_runtime_binding as validate_acquisition_runtime_binding,
)
from meta_research.deepfetch import (
    DeepFetchProvider,
    DeepFetchProviderRequest,
    DeepFetchRunRequest,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    validate_deepfetch_result,
    validate_runtime_binding,
)
from meta_research.feed import DurableFeed
from meta_research.experiment_contract import (
    EXPERIMENT_MAX_PROVIDER_OPERATION_GENERATIONS,
    EXPERIMENT_RETRYABLE_PROVIDER_FAILURES,
    AcceptedExperimentInputBinding,
    ExperimentDomainAdmission,
    ExperimentProviderResult,
    ExperimentResultComponentManifest,
    ExperimentRuntimeBinding,
    experiment_result_component_manifest,
)
from meta_research.idea_contract import (
    IDEA_REVIEW_SCHEMA_REF,
    IDEA_REVIEW_SCHEMA_V1_REF,
    material_outcome_hash,
)
from meta_research.plan_contract import (
    PLAN_REVIEW_SCHEMA_REF,
    material_plan_hash,
)
from meta_research.bundle_contract import (
    TARGET_PLAN_REVIEW_SCHEMA_REF,
    material_target_plan_hash,
    target_execution_assertion,
    target_execution_authorization_requirement,
)
from meta_research.owners._sqlite_snapshot import (
    OwnerSnapshotQuery,
    SQLiteOwnerSnapshot,
)
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessInterface,
    SQLiteAgentRuntimeHarness,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptanceReceipt,
    DeepFetchRunRequestVerifier,
    FormalPlanDecisionVerifier,
    ExperimentInputBindingVerifier,
    IdeaOutcomeDecisionVerifier,
    OwnerConflict,
    OwnerSnapshot,
    StageRunRequestVerifier,
    TargetGraphReceiptVerifier,
    canonical_hash,
    canonical_json,
    decoded_object,
    new_ref,
)
from meta_research.owners.secret_detection import contains_secret
from meta_research.owners.human_requests import (
    HumanRequestOwnerInterface,
    HumanRequestOwnerMixin,
    HumanResponseVerifier,
)
from meta_research.owners.advancement_engine import StageRunRequest
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    TypedExecutionFence,
    provider_operation_ref as typed_provider_operation_ref,
)
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeProbe,
    HostComputeSnapshot,
)
from meta_research.writing_contract import (
    WRITING_MAX_OUTPUT_BYTES,
    WritingCitationDecisionVerifier,
    WritingIntentBinding,
    WritingRuntimeBinding,
    normalize_report_intent,
    validate_writing_execution_budget,
    writing_child_review_task_hash,
)


@dataclass(frozen=True)
class HostComputeObservation:
    snapshot_ref: str
    status: str
    observed_at: float
    devices: tuple[HostComputeDevice, ...]
    adapter_kind: str
    capabilities_hash: str
    reason_code: str | None = None

    @property
    def capabilities(self) -> dict[str, object]:
        return {"devices": [device.as_dict() for device in self.devices]}

    def as_public_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "snapshot_ref": self.snapshot_ref,
            "status": self.status,
            "observed_at": self.observed_at,
            "devices": [device.as_dict() for device in self.devices],
            "adapter_kind": self.adapter_kind,
            "capabilities_hash": self.capabilities_hash,
        }
        if self.reason_code is not None:
            value["reason"] = {"code": self.reason_code}
        return value


AR_OWNER = "agent_runtime"
ATTEMPT_EXECUTION_SCHEMA = "meta-research/idea-attempt-execution/v2"
IDEA_RUNTIME_BINDING_SCHEMA = "meta-research/idea-runtime-binding/v1"
ATTEMPT_EXECUTION_RECEIPT_KIND = "idea_attempt_execution"
PLAN_ATTEMPT_EXECUTION_SCHEMA = "meta-research/plan-attempt-execution/v1"
PLAN_RUNTIME_BINDING_SCHEMA = "meta-research/plan-runtime-binding/v1"
PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND = "plan_attempt_execution"
BUNDLE_ATTEMPT_EXECUTION_SCHEMA = "meta-research/bundle-attempt-execution/v1"
BUNDLE_RUNTIME_BINDING_SCHEMA = "meta-research/bundle-runtime-binding/v1"
BUNDLE_ATTEMPT_EXECUTION_RECEIPT_KIND = "bundle_attempt_execution"
BUNDLE_DISPATCH_RECEIPT_KIND = "bundle_target_dispatch"
TARGET_RUN_ADMISSION_RECEIPT_KIND = "target_run_admission"
RUN_COMPLETION_RECEIPT_KIND = "run_execution_completed"
DEEPFETCH_EXECUTION_RECEIPT_KIND = "deepfetch_execution_completed"
EXPERIMENT_EXECUTION_RECEIPT_KIND = "experiment_execution_completed"
RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
DEEPFETCH_RECONCILIATION_BASE_SECONDS = 0.5
DEEPFETCH_RECONCILIATION_MAX_SECONDS = 60.0
MAX_DEEPFETCH_RECONCILIATION_ATTEMPTS = 40
FORMAL_STAGES = frozenset({"idea", "plan", "bundle", "reasoning"})
_IDEA_SAFE_CAPABILITIES = {
    "approval-policy-never",
    "filesystem-danger-full-access",
    "global-config-ignored",
    "harness-child-agent-review",
    "mcp-config-empty",
    "native-session-resume",
    "shell-tool-enabled",
    "structured-output-json-schema",
    "trusted-local-quest-authorization",
    "web-search-live",
}
_IDEA_SAFE_RESOURCE_PREFIXES = (
    "adapter-source:",
    "codex-config:",
    "disabled-codex-config:",
    "disabled-codex-features:",
    "harness-artifact:",
    "output-route:",
    "output-schema:",
    "package:",
    "provider-output-limits:",
    "provider-timeout-seconds:",
    "runtime-policy:",
    "sandbox-policy:",
    "transport-seal-key:",
)


@dataclass(frozen=True)
class IdeaRuntimeBinding:
    packaged_skill_bundle_hash: str
    instruction_set_hash: str
    model_ref: str
    harness_adapter_ref: str
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]
    schema_ref: str = IDEA_RUNTIME_BINDING_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "packaged_skill_bundle_hash": self.packaged_skill_bundle_hash,
            "instruction_set_hash": self.instruction_set_hash,
            "model_ref": self.model_ref,
            "harness_adapter_ref": self.harness_adapter_ref,
            "mcp_bindings": list(self.mcp_bindings),
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }


@dataclass(frozen=True)
class PlanRuntimeBinding:
    packaged_skill_bundle_hash: str
    instruction_set_hash: str
    model_ref: str
    harness_adapter_ref: str
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]
    schema_ref: str = PLAN_RUNTIME_BINDING_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "packaged_skill_bundle_hash": self.packaged_skill_bundle_hash,
            "instruction_set_hash": self.instruction_set_hash,
            "model_ref": self.model_ref,
            "harness_adapter_ref": self.harness_adapter_ref,
            "mcp_bindings": list(self.mcp_bindings),
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }


@dataclass(frozen=True)
class BundleRuntimeBinding:
    packaged_skill_bundle_hash: str
    instruction_set_hash: str
    model_ref: str
    harness_adapter_ref: str
    mcp_bindings: tuple[str, ...]
    capability_bindings: tuple[str, ...]
    resource_bindings: tuple[str, ...]
    schema_ref: str = BUNDLE_RUNTIME_BINDING_SCHEMA

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": self.schema_ref,
            "packaged_skill_bundle_hash": self.packaged_skill_bundle_hash,
            "instruction_set_hash": self.instruction_set_hash,
            "model_ref": self.model_ref,
            "harness_adapter_ref": self.harness_adapter_ref,
            "mcp_bindings": list(self.mcp_bindings),
            "capability_bindings": list(self.capability_bindings),
            "resource_bindings": list(self.resource_bindings),
        }


@dataclass(frozen=True)
class AttemptExecution:
    stage: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    submission_ref: str
    native_session_ref: str
    runtime_binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding
    runtime_binding_hash: str
    payload_hash: str
    payload_json: str
    material_outcome_hash: str
    outcome: dict[str, object]
    reviewed_draft: dict[str, object]
    reviewed_draft_hash: str
    review: dict[str, object]
    receipt: AcceptanceReceipt
    predecessor_attempt_ref: str | None = None
    predecessor_outcome_hash: str | None = None
    predecessor_material_outcome_hash: str | None = None
    predecessor_rejection_receipt: AcceptanceReceipt | None = None


@dataclass(frozen=True)
class BundleDispatchDecision:
    decision_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    native_session_ref: str
    graph_ref: str
    generation: int
    frontier: tuple[dict[str, object], ...]
    state: dict[str, object]
    action: str
    selected_target_ref: str | None
    rationale: str
    decision_hash: str
    receipt: AcceptanceReceipt

    def as_public_dict(self) -> dict[str, object]:
        return {
            "decision_ref": self.decision_ref,
            "generation": self.generation,
            "graph_ref": self.graph_ref,
            "frontier": list(self.frontier),
            "state": self.state,
            "action": self.action,
            "selected_target_ref": self.selected_target_ref,
            "rationale": self.rationale,
            "decision_hash": self.decision_hash,
            "receipt": self.receipt.as_public_dict(),
        }


@dataclass(frozen=True)
class TargetRunAdmission:
    admission_ref: str
    target_ref: str
    target_spec_hash: str
    graph_ref: str
    stage_request_ref: str
    quest_ref: str
    target_run_ref: str
    evaluation_attempt_ref: str
    execution_request_ref: str
    definition_hash: str
    dispatch_decision_ref: str
    dispatch_receipt_ref: str
    dispatch_receipt_hash: str
    receipt: AcceptanceReceipt
    human_request_ref: str | None = None
    human_waiter_ref: str | None = None
    human_waiter_generation: int | None = None
    human_authorization_receipt_ref: str | None = None


@dataclass(frozen=True)
class IdeaProviderInvocation:
    invocation_ref: str
    operation_ref: str
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    phase: str
    request_hash: str
    runtime_binding_hash: str
    status: str
    response_hash: str | None


@dataclass(frozen=True)
class IdeaPrimaryDraft:
    request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    native_session_ref: str
    runtime_binding_hash: str
    draft: dict[str, object]
    draft_hash: str
    adapter_kind: str


@dataclass(frozen=True)
class RunCompletion:
    request_ref: str
    run_ref: str
    attempt_ref: str
    outcome_ref: str
    decision_receipt: AcceptanceReceipt
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class IdeaStageRun:
    request_ref: str
    run_ref: str
    cycle_ref: str
    stage: str
    epoch: int
    status: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    runtime_binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding
    runtime_binding_hash: str
    fence_ref: str
    primary_invocation: IdeaProviderInvocation
    review_invocation: IdeaProviderInvocation
    primary_draft: IdeaPrimaryDraft | None
    execution: AttemptExecution | None
    predecessor_execution: AttemptExecution | None
    technical_predecessor_attempt_ref: str | None
    rejection_receipt: AcceptanceReceipt | None
    completion: RunCompletion | None

    @property
    def attempt_execution_receipt(self) -> AcceptanceReceipt | None:
        return None if self.execution is None else self.execution.receipt

    @property
    def completion_receipt(self) -> AcceptanceReceipt | None:
        return None if self.completion is None else self.completion.receipt


PlanStageRun = IdeaStageRun
BundleStageRun = IdeaStageRun


@dataclass(frozen=True)
class DeepFetchRun:
    request_ref: str
    run_ref: str
    correlation_ref: str
    status: str
    attempt_ref: str | None
    attempt_generation: int
    provider_operation_ref: str
    provider_operation_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str | None
    runtime_binding: DeepFetchRuntimeBinding
    runtime_binding_hash: str
    result: dict[str, object] | None
    result_hash: str | None
    execution_receipt: AcceptanceReceipt | None
    failure_code: str | None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "run_ref": self.run_ref,
            "status": self.status,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "native_session_ref": self.native_session_ref,
            "fence_ref": self.fence_ref,
            "runtime_binding_hash": self.runtime_binding_hash,
            "execution_receipt": (
                None
                if self.execution_receipt is None
                else self.execution_receipt.as_public_dict()
            ),
            "failure": (
                None if self.failure_code is None else {"code": self.failure_code}
            ),
        }


@dataclass(frozen=True)
class ExperimentRun:
    run_ref: str
    execution_request_ref: str
    provider_operation_ref: str
    provider_operation_generation: int
    provider_operation_retry_permitted: bool
    evaluation_attempt_ref: str
    variant_run_ref: str
    status: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    fence_ref: str
    fence_status: str
    managed_status: str
    runtime_binding: ExperimentRuntimeBinding
    runtime_binding_hash: str
    result: dict[str, object] | None
    result_hash: str | None
    execution_receipt: AcceptanceReceipt | None
    failure_code: str | None
    events: tuple[dict[str, object], ...] = ()
    event_count: int = 0
    stdout_event_count: int = 0

    def as_public_dict(self, *, include_events: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "run_ref": self.run_ref,
            "execution_request_ref": self.execution_request_ref,
            "provider_operation_ref": self.provider_operation_ref,
            "provider_operation_generation": self.provider_operation_generation,
            "status": self.status,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "fence_ref": self.fence_ref,
            "fence_status": self.fence_status,
            "managed_status": self.managed_status,
            "runtime_binding_hash": self.runtime_binding_hash,
            "execution_receipt": (
                None
                if self.execution_receipt is None
                else self.execution_receipt.as_public_dict()
            ),
            "failure": (
                None if self.failure_code is None else {"code": self.failure_code}
            ),
            "provider_operation_retry_permitted": (
                self.provider_operation_retry_permitted
            ),
        }
        stdout_events = tuple(
            event for event in self.events if event["kind"] == "stdout"
        )
        projected_stdout_count = len(stdout_events)
        dropped_stdout_count = max(0, self.stdout_event_count - projected_stdout_count)
        capture_truncated = self.failure_code in {
            "experiment_provider_output_limit",
            "experiment_provider_output_invalid",
        }
        value["stdout_observation"] = {
            "mode": "raw_stdout",
            "complete": self.status == "executed",
            "total": self.stdout_event_count,
            "count": projected_stdout_count,
            "truncated": capture_truncated or dropped_stdout_count > 0,
            "dropped": dropped_stdout_count,
            "first_sequence": (
                None if not stdout_events else stdout_events[0]["sequence"]
            ),
            "last_sequence": (
                None if not stdout_events else stdout_events[-1]["sequence"]
            ),
            "observed_at": (
                None if not stdout_events else stdout_events[-1]["observed_at"]
            ),
        }
        if include_events:
            value["events"] = list(self.events)
        return value


@dataclass(frozen=True)
class WritingCheckpoint:
    checkpoint_ref: str
    attempt_ref: str
    markdown: str
    markdown_hash: str
    citations: tuple[dict[str, str], ...]
    citations_hash: str
    native_session_ref: str


@dataclass(frozen=True)
class WritingExecution:
    execution_ref: str
    attempt_ref: str
    reviewed_markdown: str
    final_markdown: str
    final_markdown_hash: str
    citations: tuple[dict[str, str], ...]
    citations_hash: str
    review: dict[str, object]
    review_hash: str
    receipt: AcceptanceReceipt


@dataclass(frozen=True)
class WritingRun:
    run_ref: str
    intent_id: str
    quest_ref: str
    status: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    native_session_ref: str | None
    fence_ref: str
    provider_job_ref: str
    runtime_binding: WritingRuntimeBinding
    runtime_binding_hash: str
    execution_budget: dict[str, object]
    output_bytes: int
    failure_code: str | None = None
    checkpoint: WritingCheckpoint | None = None
    execution: WritingExecution | None = None
    predecessor_version_ref: str | None = None
    predecessor_markdown_hash: str | None = None
    feedback: tuple[str, ...] = ()
    decision_status: str | None = None
    decision_receipt: AcceptanceReceipt | None = None


class HostComputeObservationReader(Protocol):
    """Read-only AR seam for already persisted host observations."""

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation: ...

    def reconcile_host_compute(
        self, idempotency_key: str
    ) -> HostComputeObservation | None: ...


class ResearchMaterialResolver(Protocol):
    """Narrow RM Query seam used to validate and materialize accepted content."""

    def query_asset_version(self, memory_ref: str) -> object | None: ...

    def materialize_asset(self, memory_ref: str) -> object: ...


class AgentRuntimeInterface(HumanRequestOwnerInterface, Protocol):
    """Whole public Interface for Run, Attempt, Session, Fence, and host facts."""

    @property
    def harness_runs(self) -> AgentRuntimeHarnessInterface: ...

    def query_snapshot(self) -> OwnerSnapshot: ...

    def query_managed_run(self, run_ref: str) -> dict[str, object] | None: ...

    def query_managed_runs(self, quest_ref: str) -> tuple[dict[str, object], ...]: ...

    def query_runtime_control_receipt(
        self, operation_ref: str
    ) -> dict[str, object] | None: ...

    def verify_runtime_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None: ...

    def verify_runtime_quiescence_receipt(
        self,
        *,
        operation_ref: str,
        target: dict[str, object],
        affected_question_refs: tuple[str, ...],
        receipt: dict[str, object],
    ) -> None: ...

    def preview_runtime_control(
        self,
        payload: dict[str, object],
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> tuple[dict[str, object], int]: ...

    def apply_runtime_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> dict[str, object]: ...

    def prepare_runtime_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> dict[str, object]: ...

    def abort_runtime_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None: ...

    def compensate_runtime_control(
        self, *, operation_ref: str, reason_code: str
    ) -> dict[str, object]: ...

    def query_runtime_control_compensation(
        self, operation_ref: str
    ) -> dict[str, object] | None: ...

    def begin_provider_unit(
        self,
        *,
        unit_ref: str,
        operation_ref: str | None = None,
        run_ref: str,
        attempt_ref: str | None,
        fence_ref: str | None,
        unit_kind: str,
    ) -> None: ...

    def acknowledge_provider_safe_point(
        self,
        *,
        unit_ref: str | None = None,
        run_ref: str,
        attempt_ref: str | None,
        fence_ref: str | None,
    ) -> None: ...

    def reconcile_pending_provider_cleanup(
        self,
        provider: object,
        *,
        unit_kinds: tuple[str, ...],
    ) -> bool: ...

    def bind_provider_quiescence_driver(
        self, provider: object, *, unit_kinds: tuple[str, ...]
    ) -> None: ...

    def provider_quiescence_requested(self, run_ref: str) -> bool: ...

    def query_safe_meaningful_runnable(
        self, quest_ref: str, blocked_waiters: tuple[dict[str, object], ...]
    ) -> tuple[dict[str, object], ...]: ...

    def bind_research_material_resolver(
        self, resolver: ResearchMaterialResolver
    ) -> None: ...

    def bind_writing_citation_verifier(
        self, verifier: WritingCitationDecisionVerifier
    ) -> None: ...

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation: ...

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation: ...

    def prepare_acquisition_session(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        config: dict[str, object],
        provider: AcquisitionProvider,
    ) -> AcquisitionSession: ...

    def query_acquisition_session(
        self,
        *,
        initialization_id: str | None = None,
        session_ref: str | None = None,
        quest_ref: str | None = None,
    ) -> AcquisitionSession | None: ...

    def reconcile_human_request(self, request_ref: str) -> dict[str, object] | None: ...

    def acquire_literature(
        self,
        session_ref: str,
        request: AcquisitionBatchRequest,
        provider: AcquisitionProvider,
    ) -> AcquisitionBatchExecution: ...

    def bind_acquisition_session_to_quest(
        self, initialization_id: str, quest_ref: str
    ) -> AcquisitionSession | None: ...

    def execute_deepfetch(
        self,
        request: DeepFetchRunRequest,
        provider: DeepFetchProvider,
    ) -> DeepFetchRun: ...

    def query_deepfetch_run(self, request_ref: str) -> DeepFetchRun | None: ...

    def cancel_deepfetch(self, request_ref: str) -> DeepFetchRun | None: ...

    def admit_idea_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: IdeaRuntimeBinding,
    ) -> IdeaStageRun: ...

    def query_idea_stage_run(self, request_ref: str) -> IdeaStageRun | None: ...

    def record_idea_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft: ...

    def record_idea_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        outcome: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution: ...

    def query_idea_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None: ...

    def continue_after_idea_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> IdeaStageRun: ...

    def complete_idea_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        outcome_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion: ...

    def query_idea_run_completion(self, run_ref: str) -> RunCompletion | None: ...

    def admit_plan_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: PlanRuntimeBinding,
    ) -> PlanStageRun: ...

    def query_plan_stage_run(self, request_ref: str) -> PlanStageRun | None: ...

    def record_plan_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: PlanRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft: ...

    def record_plan_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: PlanRuntimeBinding,
        plan: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution: ...

    def query_plan_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None: ...

    def continue_after_plan_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> PlanStageRun: ...

    def complete_plan_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        formal_plan_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion: ...

    def query_plan_run_completion(self, run_ref: str) -> RunCompletion | None: ...

    def admit_bundle_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: BundleRuntimeBinding,
    ) -> BundleStageRun: ...

    def query_bundle_stage_run(self, request_ref: str) -> BundleStageRun | None: ...

    def record_bundle_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: BundleRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft: ...

    def record_bundle_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: BundleRuntimeBinding,
        target_plan: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution: ...

    def query_bundle_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None: ...

    def record_bundle_dispatch_decision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        graph_ref: str,
        generation: int,
        frontier: tuple[dict[str, object], ...],
        state: dict[str, object],
        action: str,
        selected_target_ref: str | None,
        rationale: str,
        idempotency_key: str,
    ) -> BundleDispatchDecision: ...

    def query_bundle_dispatch_decisions(
        self, run_ref: str
    ) -> tuple[BundleDispatchDecision, ...]: ...

    def complete_bundle_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        target_graph_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion: ...

    def query_bundle_run_completion(self, run_ref: str) -> RunCompletion | None: ...

    def verify_attempt_execution_receipt(self, **values) -> str: ...

    def verify_target_run_admission_receipt(self, **values) -> None: ...

    def verify_run_completion_receipt(self, **values) -> None: ...

    def verify_deepfetch_execution_receipt(self, **values) -> None: ...

    def verify_experiment_execution_receipt(
        self, **values
    ) -> ExperimentResultComponentManifest: ...

    def verify_writing_execution_receipt(self, **values) -> dict[str, object]: ...

    def admit_experiment(
        self,
        *,
        admission: ExperimentDomainAdmission,
        runtime_binding: ExperimentRuntimeBinding,
        require_idle: bool = False,
    ) -> ExperimentRun: ...

    def query_experiment_run(
        self, evaluation_attempt_ref: str
    ) -> ExperimentRun | None: ...

    def admit_target_run(
        self,
        *,
        target_ref: str,
        target_spec_hash: str,
        graph_ref: str,
        stage_request_ref: str,
        quest_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        idempotency_key: str,
        human_request_ref: str | None = None,
        human_waiter_ref: str | None = None,
        human_waiter_generation: int | None = None,
        human_authorization_receipt_ref: str | None = None,
    ) -> TargetRunAdmission: ...

    def query_target_run_admission(
        self, target_ref: str
    ) -> TargetRunAdmission | None: ...

    def query_active_experiment_run(self) -> ExperimentRun | None: ...

    def query_experiment_events(
        self,
        evaluation_attempt_ref: str,
        *,
        after_sequence: int = 0,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...]: ...

    def claim_next_experiment(self) -> ExperimentRun | None: ...

    def record_experiment_observation(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        kind: str,
        payload: dict[str, object],
        observed_at: float,
    ) -> None: ...

    def complete_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result: dict[str, object],
    ) -> ExperimentRun: ...

    def fail_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> ExperimentRun: ...

    def retry_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> ExperimentRun: ...

    def defer_experiment_reconciliation(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        reason_code: str,
    ) -> ExperimentRun: ...

    def replace_experiment_execution(
        self, evaluation_attempt_ref: str
    ) -> ExperimentRun: ...

    def query_executed_experiment_runs(
        self, *, offset: int = 0, limit: int = 64
    ) -> tuple[ExperimentRun, ...]: ...

    def admit_writing_report(
        self,
        binding: WritingIntentBinding,
        *,
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun: ...

    def query_writing_report(self, run_ref: str) -> WritingRun | None: ...

    def query_writing_report_by_intent(self, intent_id: str) -> WritingRun | None: ...

    def query_active_writing_reports(self) -> tuple[WritingRun, ...]: ...

    def query_writing_reports(self) -> tuple[WritingRun, ...]: ...

    def verify_current_writing_attempt(
        self, *, run_ref: str, attempt_ref: str, fence_ref: str
    ) -> dict[str, object]: ...

    def query_writing_revision_replay(
        self,
        *,
        run_ref: str,
        feedback: tuple[str, ...],
        idempotency_key: str,
    ) -> WritingRun | None: ...

    def fail_writing_report(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> WritingRun: ...

    def record_writing_checkpoint(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        markdown: str,
        citations: tuple[dict[str, str], ...],
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun: ...

    def record_writing_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        reviewed_markdown: str,
        final_markdown: str,
        citations: tuple[dict[str, str], ...],
        review: dict[str, object],
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun: ...

    def begin_writing_revision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        predecessor_version_ref: str,
        feedback: tuple[str, ...],
        decision_receipt: AcceptanceReceipt,
        decision_status: str,
        idempotency_key: str,
    ) -> WritingRun: ...

    def control_writing_report(
        self,
        run_ref: str,
        *,
        action: str,
        idempotency_key: str,
        expected_attempt_ref: str | None = None,
        expected_fence_ref: str | None = None,
        cancellation_confirmation: dict[str, object] | None = None,
    ) -> WritingRun: ...


_SNAPSHOT = OwnerSnapshotQuery(
    owner=AR_OWNER,
    statement=text(
        "SELECT revision, active_run_count, stage_run_count, completed_run_count, "
        "attempt_count, session_count, deepfetch_run_count, "
        "deepfetch_completed_run_count, deepfetch_attempt_count, "
        "deepfetch_session_count, acquisition_session_count, "
        "acquisition_request_count, acquisition_active_slot_count, "
        "human_request_count, "
        "experiment_run_count, experiment_completed_run_count, "
        "experiment_attempt_count, experiment_session_count, "
        "active_experiment_run_count, control_operation_count, safe_point_count, "
        "fenced_attempt_count, writing_run_count, writing_attempt_count, "
        "writing_session_count, "
        "active_writing_run_count "
        "FROM agent_runtime_state WHERE singleton = 'owner'"
    ),
    fact_names=(
        "active_run_count",
        "stage_run_count",
        "completed_run_count",
        "attempt_count",
        "session_count",
        "deepfetch_run_count",
        "deepfetch_completed_run_count",
        "deepfetch_attempt_count",
        "deepfetch_session_count",
        "acquisition_session_count",
        "acquisition_request_count",
        "acquisition_active_slot_count",
        "human_request_count",
        "experiment_run_count",
        "experiment_completed_run_count",
        "experiment_attempt_count",
        "experiment_session_count",
        "active_experiment_run_count",
        "control_operation_count",
        "safe_point_count",
        "fenced_attempt_count",
        "writing_run_count",
        "writing_attempt_count",
        "writing_session_count",
        "active_writing_run_count",
    ),
)

# The production probe is bounded at five seconds. The wider lease prevents a
# healthy claimant from being replaced during probe cleanup and finalization.
_HOST_COMPUTE_CLAIM_LEASE_SECONDS = 15.0
_HOST_COMPUTE_CLAIM_POLL_SECONDS = 0.02
_CONTROL_QUIESCENCE_WAIT_SECONDS = 30.0
_CONTROL_QUIESCENCE_POLL_SECONDS = 0.02
_CONTROL_QUIESCENCE_REDISPATCH_SECONDS = 0.25
_PROVIDER_UNIT_KINDS = frozenset(
    {
        "idea_primary",
        "idea_review",
        "plan_primary",
        "plan_review",
        "bundle_primary",
        "bundle_review",
        "deepfetch",
        "experiment",
    }
)


class SQLiteHostComputeObservationReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE snapshot_ref = "
                    ":snapshot_ref"
                ),
                {"snapshot_ref": snapshot_ref},
            ).first()
        if row is None:
            raise OwnerConflict("host_compute_snapshot_not_found")
        return _observation_from_row(row)


class SQLiteAgentRuntime(HumanRequestOwnerMixin):
    """Agent Runtime owns durable host-capability observations and their integrity."""

    def __init__(
        self,
        database: Database,
        feed: DurableFeed,
        host_compute_probe: HostComputeProbe,
        stage_request_verifier: StageRunRequestVerifier | None = None,
        outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
        formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
        deepfetch_request_verifier: DeepFetchRunRequestVerifier | None = None,
        acquisition_private_root: Path | None = None,
        human_response_verifier: HumanResponseVerifier | None = None,
        experiment_binding_verifier: ExperimentInputBindingVerifier | None = None,
        target_graph_verifier: TargetGraphReceiptVerifier | None = None,
    ) -> None:
        self._database = database
        self._feed = feed
        self._host_compute_probe = host_compute_probe
        self._stage_request_verifier = stage_request_verifier
        self._outcome_verifier = outcome_verifier
        self._formal_plan_verifier = formal_plan_verifier
        self._target_graph_verifier = target_graph_verifier
        self._deepfetch_request_verifier = deepfetch_request_verifier
        self._authorization_verifier = human_response_verifier
        self._research_material_resolver: ResearchMaterialResolver | None = None
        self._writing_citation_verifier: WritingCitationDecisionVerifier | None = None
        self._experiment_binding_verifier = experiment_binding_verifier
        self._acquisition_private_root = (
            acquisition_private_root
            if acquisition_private_root is not None
            else Path(".meta-research-acquisition")
        )
        self._configure_human_request_owner(
            database, feed, AR_OWNER, human_response_verifier
        )
        self._acquisition_private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._receipt_verifier = SQLiteAgentRuntimeReceiptVerifier(
            database, stage_request_verifier, human_response_verifier
        )
        self._host_compute_reader = SQLiteHostComputeObservationReader(database)
        self._snapshot = SQLiteOwnerSnapshot(database, _SNAPSHOT)
        self._harness_runs = SQLiteAgentRuntimeHarness(database, feed)
        self._deepfetch_provider_lock = threading.Lock()
        self._deepfetch_providers: dict[str, DeepFetchProvider] = {}
        self._provider_quiescence_lock = threading.Lock()
        self._provider_quiescence_drivers: dict[str, object] = {}
        self._recover_interrupted_provider_units()
        self._recover_interrupted_acquisition()
        self._recover_acquisition_human_requests()
        self._recover_interrupted_deepfetch()
        self._recover_interrupted_experiments()
        self._recover_interrupted_writing()

    @property
    def harness_runs(self) -> AgentRuntimeHarnessInterface:
        return self._harness_runs

    def bind_research_material_resolver(
        self, resolver: ResearchMaterialResolver
    ) -> None:
        self._research_material_resolver = resolver

    def _recover_interrupted_provider_units(self) -> None:
        """Permanently retire provider Fences left active by a dead daemon.

        A provider call has no durable safe-boundary acknowledgement while its
        unit row is active.  Startup therefore never treats that old process as
        attachable: the old Fence is revoked and Stage Runs receive a replacement
        Attempt under the same logical Run/root Session.
        """

        now = time.time()
        with self._database.write() as connection:
            units = connection.execute(
                text(
                    "SELECT * FROM ar_provider_units WHERE status IN ('active', "
                    "'revocation_pending') ORDER BY started_at, unit_ref"
                )
            ).all()
            recovered = 0
            replaced_unit_refs: list[str] = []
            for unit in units:
                control = connection.execute(
                    text("SELECT * FROM ar_run_controls WHERE run_ref = :run_ref"),
                    {"run_ref": unit.run_ref},
                ).first()
                invocation_status = connection.execute(
                    text(
                        "SELECT status FROM ar_stage_provider_invocations WHERE "
                        "invocation_ref = :unit_ref AND run_ref = :run_ref"
                    ),
                    {"unit_ref": unit.unit_ref, "run_ref": unit.run_ref},
                ).scalar_one_or_none()
                if (
                    control is not None
                    and control.status == "completed"
                    or invocation_status == "completed"
                ):
                    # A committed Owner result proves the provider call crossed a
                    # terminal boundary before the old daemon disappeared.
                    connection.execute(
                        text(
                            "UPDATE ar_provider_units SET status = 'completed', "
                            "completed_at = :now WHERE unit_ref = :unit_ref AND "
                            "status IN ('active', 'revocation_pending')"
                        ),
                        {"now": now, "unit_ref": unit.unit_ref},
                    )
                    continue
                if control is not None and _prepared_control_affects_run(
                    connection, str(unit.run_ref)
                ):
                    # A confirmed control already froze this exact Attempt/Fence.
                    # Startup may mark its physical unit pending, but every control
                    # action must apply (or abort) that same reservation before
                    # technical recovery changes the frozen identity.  Graceful
                    # controls obtain a provider ACK first; force/terminal controls
                    # revoke the old Fence while applying; a normal non-Bundle
                    # handoff installs its replacement only after the CAS succeeds.
                    connection.execute(
                        text(
                            "UPDATE ar_provider_units SET status = "
                            "'revocation_pending' WHERE unit_ref = :unit_ref AND "
                            "status = 'active'"
                        ),
                        {"unit_ref": unit.unit_ref},
                    )
                    continue
                if unit.status == "revocation_pending":
                    # A previous daemon already revoked this Fence.  Repeating
                    # replacement would create an unbounded Attempt chain while
                    # the same detached provider operation is still unknown.
                    continue
                if (
                    control is not None
                    and not _is_formal_stage_run_kind(control.run_kind)
                    and control.status == "running"
                    and control.attempt_ref is None
                ):
                    # DeepFetch/Experiment reconciliation owns its own stable
                    # operation and persisted backoff.  Preserve that boundary
                    # without emitting startup churn or inventing an Attempt.
                    continue
                if unit.fence_ref is not None:
                    inserted = connection.execute(
                        text(
                            "INSERT INTO ar_fence_revocations (fence_ref, "
                            "operation_ref, run_ref, attempt_ref, reason_code, "
                            "revoked_at) VALUES (:fence_ref, :operation_ref, "
                            ":run_ref, :attempt_ref, 'daemon_restart_inflight', :now) "
                            "ON CONFLICT(fence_ref) DO NOTHING"
                        ),
                        {
                            "fence_ref": unit.fence_ref,
                            "operation_ref": (
                                "provider-recovery-"
                                + canonical_hash({"unit_ref": unit.unit_ref})[:48]
                            ),
                            "run_ref": unit.run_ref,
                            "attempt_ref": unit.attempt_ref,
                            "now": now,
                        },
                    )
                    recovered += int(inserted.rowcount or 0)
                # Process death revokes the old Fence, but it is not evidence that
                # a detached durable supervisor has stopped.  Keep the physical
                # unit pending until the same provider operation yields a terminal
                # response or an explicit cancel/exit acknowledgement.
                connection.execute(
                    text(
                        "UPDATE ar_provider_units SET status = 'revocation_pending' "
                        "WHERE unit_ref = :unit_ref AND status = 'active'"
                    ),
                    {"unit_ref": unit.unit_ref},
                )
                if control is not None and control.status == "terminated":
                    # The daemon restart is not proof that an external provider job
                    # was physically cleaned up.  Preserve the durable pending truth
                    # until an adapter/worker safe acknowledgement arrives.
                    connection.execute(
                        text(
                            "UPDATE ar_run_controls SET cleanup_status = 'pending', "
                            "updated_at = :now WHERE run_ref = :run_ref"
                        ),
                        {"now": now, "run_ref": unit.run_ref},
                    )
                    continue
                if (
                    control is None
                    or control.status == "completed"
                    or control.status != "running"
                    or not _is_formal_stage_run_kind(control.run_kind)
                ):
                    continue
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'rejected', "
                        "closed_at = :now WHERE fence_ref = :fence_ref AND status IN "
                        "('current', 'submitted')"
                    ),
                    {"now": now, "fence_ref": unit.fence_ref},
                )
                replacement = self._replace_fenced_managed_attempt(
                    connection, control, now
                )
                replaced_unit_refs.append(str(unit.unit_ref))
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'running', attempt_ref = "
                        ":attempt_ref, fence_ref = :fence_ref, control_revision = "
                        "control_revision + 1, safe_point_ref = NULL, terminal_reason "
                        "= NULL, cleanup_status = 'none', updated_at = :now WHERE "
                        "run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": replacement.attempt_ref,
                        "fence_ref": replacement.fence_ref,
                        "now": now,
                        "run_ref": unit.run_ref,
                    },
                )
            if replaced_unit_refs:
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "fenced_attempt_count = fenced_attempt_count + "
                        ":recovered WHERE singleton = 'owner'"
                    ),
                    {"recovered": recovered},
                )
                self._feed.record(
                    connection,
                    "agent_runtime.provider_units_recovered",
                    {
                        "unit_refs": replaced_unit_refs,
                        "fenced_attempt_count": recovered,
                    },
                )
    def bind_writing_citation_verifier(
        self, verifier: WritingCitationDecisionVerifier
    ) -> None:
        if (
            self._writing_citation_verifier is not None
            and self._writing_citation_verifier is not verifier
        ):
            raise OwnerConflict("writing_citation_verifier_already_bound")
        self._writing_citation_verifier = verifier

    def admit_writing_report(
        self,
        binding: WritingIntentBinding,
        *,
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun:
        _validate_stage_idempotency_key(idempotency_key)
        binding.validate()
        runtime_binding.validate()
        runtime_document = runtime_binding.as_dict()
        runtime_hash = canonical_hash(runtime_document)
        request_document = {
            "command": "admit_writing_report",
            "binding": binding.as_dict(),
            "runtime_binding": runtime_document,
        }
        request_hash = canonical_hash(request_document)
        # A completed admission is a durable fact. Return it before consulting
        # mutable authorization/provider state so a lost response remains
        # replayable after a later revocation or provider upgrade.
        with self._database.read() as connection:
            replay = _writing_command_replay(
                connection,
                idempotency_key,
                "admit_writing_report",
                request_hash,
            )
        if replay is not None:
            result = self.query_writing_report(replay)
            if result is None:
                raise OwnerConflict("writing_run_missing_after_admission")
            return result
        if self._authorization_verifier is None:
            raise OwnerConflict("writing_confirmation_verifier_unavailable")
        self._authorization_verifier.verify_broad_research_authorization(
            quest_ref=binding.quest_ref
        )
        confirmed_draft = self._authorization_verifier.verify_command_confirmation(
            intent_id=binding.intent_id,
            command_kind="writing_report_start",
            draft_revision=binding.draft_revision,
            draft_hash=binding.draft_hash,
            preview_ref=binding.preview_ref,
            preview_hash=binding.preview_hash,
            receipt=binding.confirmation,
        )
        expected_draft = {
            "command_kind": "writing_report_start",
            "payload": {
                "document_type": binding.document_type,
                "quest_ref": binding.quest_ref,
                "intent": binding.intent,
                "intent_hash": binding.intent_hash,
                "snapshot": binding.snapshot,
                "snapshot_ref": binding.snapshot_ref,
                "snapshot_hash": binding.snapshot_hash,
                "execution_budget": binding.execution_budget,
            },
        }
        if confirmed_draft != expected_draft:
            raise OwnerConflict("writing_confirmation_binding_mismatch")
        with self._database.write() as connection:
            command = connection.execute(
                text(
                    "SELECT * FROM ar_writing_commands WHERE idempotency_key = "
                    ":idempotency_key AND command_kind = 'admit_writing_report'"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if command is not None:
                if (
                    command.request_hash != request_hash
                ):
                    raise OwnerConflict("idempotency_conflict")
                run_ref = command.result_ref
            else:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_runs WHERE intent_id = :intent_id"
                    ),
                    {"intent_id": binding.intent_id},
                ).first()
                if existing is not None:
                    if (
                        existing.intent_hash != binding.intent_hash
                        or existing.snapshot_ref != binding.snapshot_ref
                        or existing.snapshot_hash != binding.snapshot_hash
                        or existing.confirmation_ref
                        != binding.confirmation.receipt_ref
                        or existing.confirmation_hash
                        != binding.confirmation.payload_hash
                        or existing.runtime_binding_hash != runtime_hash
                    ):
                        raise OwnerConflict("writing_admission_conflict")
                    run_ref = existing.run_ref
                else:
                    run_ref = new_ref("writing_run")
                    attempt_ref = new_ref("writing_attempt")
                    root_session_ref = new_ref("writing_root_session")
                    fence_ref = new_ref("writing_fence")
                    provider_job_ref = new_ref("writing_job")
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_runs (run_ref, intent_id, "
                            "quest_ref, document_type, intent_json, intent_hash, "
                            "snapshot_ref, snapshot_json, snapshot_hash, "
                            "confirmation_ref, confirmation_hash, status, "
                            "failure_code, execution_budget_json, "
                            "execution_budget_hash, output_bytes, "
                            "attempt_ref, attempt_generation, root_session_ref, "
                            "native_session_ref, fence_ref, runtime_binding_json, "
                            "runtime_binding_hash, created_at, updated_at) VALUES "
                            "(:run_ref, :intent_id, :quest_ref, 'report', "
                            ":intent_json, :intent_hash, :snapshot_ref, "
                            ":snapshot_json, :snapshot_hash, :confirmation_ref, "
                            ":confirmation_hash, 'active', NULL, "
                            ":execution_budget_json, :execution_budget_hash, "
                            "0, :attempt_ref, 1, "
                            ":root_session_ref, :native_session_ref, :fence_ref, "
                            ":runtime_binding_json, :runtime_binding_hash, :now, :now)"
                        ),
                        {
                            "run_ref": run_ref,
                            "intent_id": binding.intent_id,
                            "quest_ref": binding.quest_ref,
                            "intent_json": canonical_json(binding.intent),
                            "intent_hash": binding.intent_hash,
                            "snapshot_ref": binding.snapshot_ref,
                            "snapshot_json": canonical_json(binding.snapshot),
                            "snapshot_hash": binding.snapshot_hash,
                            "confirmation_ref": binding.confirmation.receipt_ref,
                            "confirmation_hash": binding.confirmation.payload_hash,
                            "execution_budget_json": canonical_json(
                                binding.execution_budget
                            ),
                            "execution_budget_hash": canonical_hash(
                                binding.execution_budget
                            ),
                            "attempt_ref": attempt_ref,
                            "root_session_ref": root_session_ref,
                            "native_session_ref": None,
                            "fence_ref": fence_ref,
                            "runtime_binding_json": canonical_json(runtime_document),
                            "runtime_binding_hash": runtime_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_attempts (attempt_ref, run_ref, "
                            "generation, root_session_ref, native_session_ref, "
                            "fence_ref, provider_job_ref, status, created_at) VALUES (:attempt_ref, "
                            ":run_ref, 1, :root_session_ref, :native_session_ref, "
                            ":fence_ref, :provider_job_ref, 'admitted', :now)"
                        ),
                        {
                            "attempt_ref": attempt_ref,
                            "run_ref": run_ref,
                            "root_session_ref": root_session_ref,
                            "native_session_ref": None,
                            "fence_ref": fence_ref,
                            "provider_job_ref": provider_job_ref,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1, "
                            "writing_run_count = writing_run_count + 1, "
                            "writing_attempt_count = writing_attempt_count + 1, "
                            "writing_session_count = writing_session_count + 1, "
                            "active_writing_run_count = active_writing_run_count + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.writing_report_admitted",
                        {
                            "run_ref": run_ref,
                            "intent_id": binding.intent_id,
                            "quest_ref": binding.quest_ref,
                            "attempt_ref": attempt_ref,
                            "fence_ref": fence_ref,
                        },
                    )
                connection.execute(
                    text(
                        "INSERT INTO ar_writing_commands (idempotency_key, "
                        "command_kind, request_hash, result_ref, recorded_at) VALUES "
                        "(:idempotency_key, 'admit_writing_report', :request_hash, "
                        ":result_ref, :recorded_at)"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "result_ref": run_ref,
                        "recorded_at": time.time(),
                    },
                )
        run = self.query_writing_report(run_ref)
        if run is None:
            raise OwnerConflict("writing_run_missing_after_admission")
        return run

    def query_writing_report(self, run_ref: str) -> WritingRun | None:
        return self._query_writing_report("run_ref", run_ref)

    def query_writing_report_by_intent(self, intent_id: str) -> WritingRun | None:
        return self._query_writing_report("intent_id", intent_id)

    def query_active_writing_reports(self) -> tuple[WritingRun, ...]:
        with self._database.read() as connection:
            refs = connection.execute(
                text(
                    "SELECT run_ref FROM ar_writing_runs WHERE status = 'active' "
                    "ORDER BY updated_at, run_ref"
                )
            ).scalars().all()
        runs = tuple(self.query_writing_report(str(run_ref)) for run_ref in refs)
        return tuple(run for run in runs if run is not None)

    def query_writing_reports(self) -> tuple[WritingRun, ...]:
        with self._database.read() as connection:
            refs = connection.execute(
                text(
                    "SELECT run_ref FROM ar_writing_runs ORDER BY created_at DESC, "
                    "run_ref DESC"
                )
            ).scalars().all()
        runs = tuple(self.query_writing_report(str(run_ref)) for run_ref in refs)
        return tuple(run for run in runs if run is not None)

    def verify_current_writing_attempt(
        self, *, run_ref: str, attempt_ref: str, fence_ref: str
    ) -> dict[str, object]:
        with self._database.read() as connection:
            row = _current_writing_run_row(
                connection, run_ref, attempt_ref, fence_ref
            )
        if self._authorization_verifier is None:
            raise OwnerConflict("writing_authorization_verifier_unavailable")
        authorization = (
            self._authorization_verifier.verify_broad_research_authorization(
                quest_ref=row.quest_ref
            )
        )
        return {
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "fence_ref": row.fence_ref,
            "quest_ref": row.quest_ref,
            "authorization": authorization,
        }

    def query_writing_revision_replay(
        self,
        *,
        run_ref: str,
        feedback: tuple[str, ...],
        idempotency_key: str,
    ) -> WritingRun | None:
        _validate_stage_idempotency_key(idempotency_key)
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_writing_commands WHERE command_kind = "
                    "'begin_writing_revision' AND idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            attempt = (
                None
                if row is None or row.result_attempt_ref is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_writing_attempts WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": row.result_attempt_ref},
                ).first()
            )
        if row is None:
            return None
        if row.result_ref != run_ref or attempt is None:
            raise OwnerConflict("idempotency_conflict")
        try:
            recorded_feedback = json.loads(attempt.feedback_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("writing_revision_lineage_invalid") from error
        if (
            recorded_feedback != list(feedback)
            or canonical_hash(recorded_feedback) != attempt.feedback_hash
            or attempt.decision_status not in {"accepted", "rejected"}
        ):
            raise OwnerConflict("idempotency_conflict")
        return self.query_writing_report(run_ref)

    def fail_writing_report(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> WritingRun:
        if (
            not isinstance(failure_code, str)
            or not failure_code
            or len(failure_code) > 128
        ):
            raise OwnerConflict("writing_failure_code_invalid")
        with self._database.write() as connection:
            row = connection.execute(
                text("SELECT * FROM ar_writing_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            if (
                row is not None
                and row.status == "blocked"
                and row.attempt_ref == attempt_ref
                and row.fence_ref == fence_ref
                and row.failure_code == failure_code
            ):
                pass
            else:
                row = _current_writing_run_row(
                    connection, run_ref, attempt_ref, fence_ref
                )
                now = time.time()
                connection.execute(
                    text(
                        "UPDATE ar_writing_runs SET status = 'blocked', "
                        "failure_code = :failure_code, updated_at = :now WHERE "
                        "run_ref = :run_ref AND attempt_ref = :attempt_ref AND "
                        "fence_ref = :fence_ref"
                    ),
                    {
                        "failure_code": failure_code,
                        "now": now,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_writing_attempts SET status = CASE WHEN status "
                        "IN ('admitted', 'running', 'paused') THEN 'failed' ELSE "
                        "status END, failure_code = :failure_code, completed_at = "
                        "CASE WHEN status IN ('admitted', 'running', 'paused') THEN "
                        "COALESCE(completed_at, :now) ELSE completed_at END WHERE "
                        "attempt_ref = :attempt_ref"
                    ),
                    {
                        "failure_code": failure_code,
                        "now": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "active_writing_run_count = active_writing_run_count - 1 "
                        "WHERE singleton = 'owner' AND active_writing_run_count > 0"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.writing_report_blocked",
                    {
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "failure_code": failure_code,
                    },
                )
        result = self.query_writing_report(run_ref)
        if result is None:
            raise OwnerConflict("writing_run_not_found")
        return result

    def record_writing_checkpoint(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        markdown: str,
        citations: tuple[dict[str, str], ...],
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun:
        _validate_stage_idempotency_key(idempotency_key)
        runtime_binding.validate()
        if (
            not native_session_ref
            or len(native_session_ref) > 128
            or not markdown.strip()
        ):
            raise OwnerConflict("writing_checkpoint_invalid")
        markdown_hash = _writing_markdown_hash(markdown)
        citations_hash = canonical_hash(list(citations))
        output_bytes = len(markdown.encode("utf-8")) + len(
            canonical_json(list(citations)).encode("utf-8")
        )
        request_hash = canonical_hash(
            {
                "command": "record_writing_checkpoint",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "native_session_ref": native_session_ref,
                "markdown_hash": markdown_hash,
                "citations_hash": citations_hash,
                "runtime_binding_hash": canonical_hash(runtime_binding.as_dict()),
            }
        )
        with self._database.write() as connection:
            replay = _writing_command_replay(
                connection,
                idempotency_key,
                "record_writing_checkpoint",
                request_hash,
            )
            if replay is None:
                run = _current_writing_run_row(
                    connection, run_ref, attempt_ref, fence_ref
                )
                if self._authorization_verifier is None:
                    raise OwnerConflict("writing_authorization_verifier_unavailable")
                self._authorization_verifier.verify_broad_research_authorization(
                    quest_ref=run.quest_ref
                )
                snapshot = decoded_object(run.snapshot_json)
                if (
                    not isinstance(snapshot, dict)
                    or _validate_writing_citations_for_snapshot(
                        citations, snapshot
                    )
                    != citations_hash
                ):
                    raise OwnerConflict("writing_citations_invalid")
                if run.runtime_binding_hash != canonical_hash(
                    runtime_binding.as_dict()
                ):
                    raise OwnerConflict("writing_runtime_binding_drift")
                budget = validate_writing_execution_budget(
                    decoded_object(run.execution_budget_json)
                )
                if int(run.output_bytes) + output_bytes > int(
                    budget["max_output_bytes"]
                ):
                    raise OwnerConflict("writing_output_budget_exhausted")
                if run.native_session_ref not in {None, native_session_ref}:
                    raise OwnerConflict("writing_native_session_changed")
                session_owner = connection.execute(
                    text(
                        "SELECT run_ref FROM ar_writing_runs WHERE "
                        "native_session_ref = :native_session_ref AND run_ref != "
                        ":run_ref"
                    ),
                    {
                        "native_session_ref": native_session_ref,
                        "run_ref": run_ref,
                    },
                ).first()
                if session_owner is not None:
                    raise OwnerConflict("writing_native_session_conflict")
                checkpoint = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_checkpoints WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": attempt_ref},
                ).first()
                if checkpoint is not None:
                    if (
                        checkpoint.markdown_hash != markdown_hash
                        or checkpoint.citations_hash != citations_hash
                        or checkpoint.native_session_ref != native_session_ref
                    ):
                        raise OwnerConflict("writing_checkpoint_conflict")
                else:
                    checkpoint_ref = new_ref("writing_checkpoint")
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_checkpoints (checkpoint_ref, "
                            "run_ref, attempt_ref, fence_ref, native_session_ref, "
                            "markdown, markdown_hash, citations_json, citations_hash, "
                            "created_at) VALUES (:checkpoint_ref, :run_ref, "
                            ":attempt_ref, :fence_ref, :native_session_ref, :markdown, "
                            ":markdown_hash, :citations_json, :citations_hash, :now)"
                        ),
                        {
                            "checkpoint_ref": checkpoint_ref,
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "fence_ref": fence_ref,
                            "native_session_ref": native_session_ref,
                            "markdown": markdown,
                            "markdown_hash": markdown_hash,
                            "citations_json": canonical_json(list(citations)),
                            "citations_hash": citations_hash,
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_writing_runs SET native_session_ref = "
                            ":native_session_ref, output_bytes = output_bytes + "
                            ":output_bytes, updated_at = :now WHERE run_ref = "
                            ":run_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "output_bytes": output_bytes,
                            "now": now,
                            "run_ref": run_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_writing_attempts SET native_session_ref = "
                            ":native_session_ref, status = 'running', started_at = "
                            "COALESCE(started_at, :now) WHERE attempt_ref = "
                            ":attempt_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "now": now,
                            "attempt_ref": attempt_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.writing_checkpoint_recorded",
                        {
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "checkpoint_ref": checkpoint_ref,
                        },
                    )
                _record_writing_command(
                    connection,
                    idempotency_key,
                    "record_writing_checkpoint",
                    request_hash,
                    run_ref,
                )
        result = self.query_writing_report(run_ref)
        if result is None:
            raise OwnerConflict("writing_run_missing_after_checkpoint")
        return result

    def record_writing_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        reviewed_markdown: str,
        final_markdown: str,
        citations: tuple[dict[str, str], ...],
        review: dict[str, object],
        runtime_binding: WritingRuntimeBinding,
        idempotency_key: str,
    ) -> WritingRun:
        _validate_stage_idempotency_key(idempotency_key)
        runtime_binding.validate()
        reviewed_hash = _writing_markdown_hash(reviewed_markdown)
        final_hash = _writing_markdown_hash(final_markdown)
        citations_hash = canonical_hash(list(citations))
        review_hash = canonical_hash(review)
        output_bytes = (
            len(reviewed_markdown.encode("utf-8"))
            + len(final_markdown.encode("utf-8"))
            + len(canonical_json(list(citations)).encode("utf-8"))
            + len(canonical_json(review).encode("utf-8"))
        )
        if (
            review.get("reviewed_markdown_hash") != reviewed_hash
            or review.get("final_markdown_hash") != final_hash
            or review.get("citations_hash") != citations_hash
            or review.get("review_mode") != "harness_child_agent"
        ):
            raise OwnerConflict("writing_review_invalid")
        execution_payload = {
            "schema_ref": "meta-research/writing-execution/v1",
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "final_markdown_hash": final_hash,
            "citations_hash": citations_hash,
            "review_hash": review_hash,
            "runtime_binding_hash": canonical_hash(runtime_binding.as_dict()),
        }
        payload_hash = canonical_hash(execution_payload)
        request_hash = canonical_hash(
            {"command": "record_writing_execution", **execution_payload}
        )
        with self._database.write() as connection:
            replay = _writing_command_replay(
                connection,
                idempotency_key,
                "record_writing_execution",
                request_hash,
            )
            if replay is None:
                run = _current_writing_run_row(
                    connection, run_ref, attempt_ref, fence_ref
                )
                if self._authorization_verifier is None:
                    raise OwnerConflict("writing_authorization_verifier_unavailable")
                self._authorization_verifier.verify_broad_research_authorization(
                    quest_ref=run.quest_ref
                )
                if run.runtime_binding_hash != canonical_hash(
                    runtime_binding.as_dict()
                ):
                    raise OwnerConflict("writing_runtime_binding_drift")
                budget = validate_writing_execution_budget(
                    decoded_object(run.execution_budget_json)
                )
                if int(run.output_bytes) + output_bytes > int(
                    budget["max_output_bytes"]
                ):
                    raise OwnerConflict("writing_output_budget_exhausted")
                checkpoint = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_checkpoints WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": attempt_ref},
                ).first()
                if (
                    checkpoint is None
                    or checkpoint.markdown_hash != reviewed_hash
                    or checkpoint.fence_ref != fence_ref
                ):
                    raise OwnerConflict("writing_checkpoint_stale")
                snapshot = decoded_object(run.snapshot_json)
                checkpoint_value = _writing_checkpoint_from_row(checkpoint)
                attempt = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_attempts WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": attempt_ref},
                ).first()
                if (
                    not isinstance(snapshot, dict)
                    or checkpoint_value is None
                    or attempt is None
                    or _validate_writing_citations_for_snapshot(
                        citations, snapshot
                    )
                    != citations_hash
                ):
                    raise OwnerConflict("writing_execution_invalid")
                _validate_writing_review_contract(
                    review=review,
                    run=run,
                    attempt=attempt,
                    checkpoint=checkpoint_value,
                    reviewed_markdown=reviewed_markdown,
                    final_markdown=final_markdown,
                    citations=citations,
                    final_hash=final_hash,
                    citations_hash=citations_hash,
                )
                existing = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_executions WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": attempt_ref},
                ).first()
                if existing is not None:
                    if existing.payload_hash != payload_hash:
                        raise OwnerConflict("writing_execution_conflict")
                else:
                    execution_ref = new_ref("writing_execution")
                    receipt_ref = new_ref("writing_execution_receipt")
                    receipt_hash = canonical_hash(
                        {
                            "schema_ref": RECEIPT_SCHEMA,
                            "issuer": AR_OWNER,
                            "kind": "writing_execution_completed",
                            "subject_ref": execution_ref,
                            "payload_hash": payload_hash,
                        }
                    )
                    now = time.time()
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_executions (execution_ref, "
                            "run_ref, attempt_ref, fence_ref, reviewed_markdown, "
                            "reviewed_markdown_hash, final_markdown, "
                            "final_markdown_hash, citations_json, citations_hash, "
                            "review_json, review_hash, payload_hash, receipt_ref, "
                            "receipt_hash, runtime_binding_hash, completed_at) VALUES (:execution_ref, "
                            ":run_ref, :attempt_ref, :fence_ref, :reviewed_markdown, "
                            ":reviewed_hash, :final_markdown, :final_hash, "
                            ":citations_json, :citations_hash, :review_json, "
                            ":review_hash, :payload_hash, :receipt_ref, "
                            ":receipt_hash, :runtime_binding_hash, :now)"
                        ),
                        {
                            "execution_ref": execution_ref,
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "fence_ref": fence_ref,
                            "reviewed_markdown": reviewed_markdown,
                            "reviewed_hash": reviewed_hash,
                            "final_markdown": final_markdown,
                            "final_hash": final_hash,
                            "citations_json": canonical_json(list(citations)),
                            "citations_hash": citations_hash,
                            "review_json": canonical_json(review),
                            "review_hash": review_hash,
                            "payload_hash": payload_hash,
                            "receipt_ref": receipt_ref,
                            "receipt_hash": receipt_hash,
                            "runtime_binding_hash": canonical_hash(
                                runtime_binding.as_dict()
                            ),
                            "now": now,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_writing_attempts SET status = 'completed', "
                            "completed_at = :now WHERE attempt_ref = :attempt_ref"
                        ),
                        {"now": now, "attempt_ref": attempt_ref},
                    )
                    connection.execute(
                        text(
                            "UPDATE ar_writing_runs SET output_bytes = output_bytes + "
                            ":output_bytes, updated_at = :now WHERE "
                            "run_ref = :run_ref"
                        ),
                        {
                            "output_bytes": output_bytes,
                            "now": now,
                            "run_ref": run_ref,
                        },
                    )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1 "
                            "WHERE singleton = 'owner'"
                        )
                    )
                    self._feed.record(
                        connection,
                        "agent_runtime.writing_execution_completed",
                        {
                            "run_ref": run_ref,
                            "attempt_ref": attempt_ref,
                            "execution_ref": execution_ref,
                            "receipt_ref": receipt_ref,
                        },
                    )
                _record_writing_command(
                    connection,
                    idempotency_key,
                    "record_writing_execution",
                    request_hash,
                    run_ref,
                )
        result = self.query_writing_report(run_ref)
        if result is None:
            raise OwnerConflict("writing_run_missing_after_execution")
        return result

    def begin_writing_revision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        predecessor_version_ref: str,
        feedback: tuple[str, ...],
        decision_receipt: AcceptanceReceipt,
        decision_status: str,
        idempotency_key: str,
    ) -> WritingRun:
        _validate_stage_idempotency_key(idempotency_key)
        if (
            self._writing_citation_verifier is None
            or not predecessor_version_ref
            or not feedback
            or any(not item.strip() or len(item) > 4000 for item in feedback)
            or contains_secret(feedback)
            or decision_status not in {"accepted", "rejected"}
        ):
            if contains_secret(feedback):
                raise OwnerConflict("writing_revision_secret_forbidden")
            raise OwnerConflict("writing_revision_feedback_invalid")
        self._writing_citation_verifier.verify_writing_citation_decision(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            version_ref=predecessor_version_ref,
            feedback=feedback,
            receipt=decision_receipt,
            expected_decision=decision_status,
        )
        if self._research_material_resolver is None:
            raise OwnerConflict("research_material_resolver_unavailable")
        predecessor = self._research_material_resolver.query_asset_version(
            predecessor_version_ref
        )
        materialized = self._research_material_resolver.materialize_asset(
            predecessor_version_ref
        )
        try:
            predecessor_markdown = materialized.content.decode("utf-8")
        except (AttributeError, UnicodeDecodeError) as error:
            raise OwnerConflict("writing_predecessor_version_invalid") from error
        if (
            predecessor is None
            or getattr(predecessor, "version_ref", None) != predecessor_version_ref
            or getattr(materialized, "memory_ref", None) != predecessor_version_ref
        ):
            raise OwnerConflict("writing_predecessor_version_invalid")
        predecessor_markdown_hash = _writing_markdown_hash(predecessor_markdown)
        decision_receipt_value = decision_receipt.as_public_dict()
        decision_receipt_hash = canonical_hash(decision_receipt_value)
        request_hash = canonical_hash(
            {
                "command": "begin_writing_revision",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "predecessor_version_ref": predecessor_version_ref,
                "predecessor_markdown_hash": predecessor_markdown_hash,
                "feedback": list(feedback),
                "decision_receipt": decision_receipt.as_public_dict(),
                "decision_status": decision_status,
            }
        )
        with self._database.write() as connection:
            replay = _writing_command_replay(
                connection,
                idempotency_key,
                "begin_writing_revision",
                request_hash,
            )
            if replay is None:
                run = _current_writing_run_row(
                    connection, run_ref, attempt_ref, fence_ref
                )
                execution = connection.execute(
                    text(
                        "SELECT execution_ref FROM ar_writing_executions WHERE "
                        "attempt_ref = :attempt_ref"
                    ),
                    {"attempt_ref": attempt_ref},
                ).first()
                if execution is None or run.native_session_ref is None:
                    raise OwnerConflict("writing_revision_predecessor_incomplete")
                execution_budget = validate_writing_execution_budget(
                    decoded_object(run.execution_budget_json)
                )
                # Recovery/pause replacements copy immutable lineage onto a
                # new execution Attempt.  Count distinct predecessor versions,
                # not Attempt rows, so Fence rotation never consumes a content
                # revision from the frozen run budget.
                content_revision = 2 + int(
                    connection.execute(
                        text(
                            "SELECT COUNT(DISTINCT predecessor_version_ref) FROM "
                            "ar_writing_attempts WHERE run_ref = :run_ref AND "
                            "predecessor_version_ref IS NOT NULL"
                        ),
                        {"run_ref": run_ref},
                    ).scalar_one()
                )
                if content_revision > int(
                    execution_budget["max_content_revisions"]
                ):
                    raise OwnerConflict("writing_revision_budget_exhausted")
                generation = int(run.attempt_generation) + 1
                replacement_ref = new_ref("writing_attempt")
                replacement_fence = new_ref("writing_fence")
                replacement_job_ref = new_ref("writing_job")
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO ar_writing_attempts (attempt_ref, run_ref, "
                        "generation, root_session_ref, native_session_ref, fence_ref, "
                        "provider_job_ref, status, predecessor_version_ref, "
                        "predecessor_markdown_hash, feedback_json, feedback_hash, "
                        "decision_status, decision_receipt_json, "
                        "decision_receipt_hash, created_at) VALUES (:attempt_ref, :run_ref, "
                        ":generation, :root_session_ref, :native_session_ref, "
                        ":fence_ref, :provider_job_ref, 'admitted', :predecessor_version_ref, "
                        ":predecessor_markdown_hash, :feedback_json, "
                        ":feedback_hash, :decision_status, "
                        ":decision_receipt_json, :decision_receipt_hash, :now)"
                    ),
                    {
                        "attempt_ref": replacement_ref,
                        "run_ref": run_ref,
                        "generation": generation,
                        "root_session_ref": run.root_session_ref,
                        "native_session_ref": run.native_session_ref,
                        "fence_ref": replacement_fence,
                        "provider_job_ref": replacement_job_ref,
                        "predecessor_version_ref": predecessor_version_ref,
                        "predecessor_markdown_hash": predecessor_markdown_hash,
                        "feedback_json": canonical_json(list(feedback)),
                        "feedback_hash": canonical_hash(list(feedback)),
                        "decision_status": decision_status,
                        "decision_receipt_json": canonical_json(
                            decision_receipt_value
                        ),
                        "decision_receipt_hash": decision_receipt_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_writing_runs SET attempt_ref = :attempt_ref, "
                        "attempt_generation = :generation, fence_ref = :fence_ref, "
                        "predecessor_version_ref = :predecessor_version_ref, "
                        "feedback_json = :feedback_json, feedback_hash = "
                        ":feedback_hash, updated_at = :now WHERE run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": replacement_ref,
                        "generation": generation,
                        "fence_ref": replacement_fence,
                        "predecessor_version_ref": predecessor_version_ref,
                        "feedback_json": canonical_json(list(feedback)),
                        "feedback_hash": canonical_hash(list(feedback)),
                        "now": now,
                        "run_ref": run_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "writing_attempt_count = writing_attempt_count + 1 WHERE "
                        "singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.writing_revision_admitted",
                    {
                        "run_ref": run_ref,
                        "predecessor_attempt_ref": attempt_ref,
                        "attempt_ref": replacement_ref,
                        "attempt_generation": generation,
                        "fence_ref": replacement_fence,
                        "predecessor_version_ref": predecessor_version_ref,
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                    },
                )
                _record_writing_command(
                    connection,
                    idempotency_key,
                    "begin_writing_revision",
                    request_hash,
                    run_ref,
                    replacement_ref,
                )
        result = self.query_writing_report(run_ref)
        if result is None:
            raise OwnerConflict("writing_revision_missing_after_admission")
        return result

    def control_writing_report(
        self,
        run_ref: str,
        *,
        action: str,
        idempotency_key: str,
        expected_attempt_ref: str | None = None,
        expected_fence_ref: str | None = None,
        cancellation_confirmation: dict[str, object] | None = None,
    ) -> WritingRun:
        _validate_stage_idempotency_key(idempotency_key)
        if action not in {"pause", "resume", "cancel"}:
            raise OwnerConflict("writing_control_action_invalid")
        request_hash = canonical_hash(
            {
                "command": "control_writing_report",
                "run_ref": run_ref,
                "action": action,
                "expected_attempt_ref": expected_attempt_ref,
                "expected_fence_ref": expected_fence_ref,
                "cancellation_confirmation": cancellation_confirmation,
            }
        )
        with self._database.write() as connection:
            replay = _writing_command_replay(
                connection,
                idempotency_key,
                "control_writing_report",
                request_hash,
            )
            if replay is None:
                run = connection.execute(
                    text("SELECT * FROM ar_writing_runs WHERE run_ref = :run_ref"),
                    {"run_ref": run_ref},
                ).first()
                if run is None:
                    raise OwnerConflict("writing_run_not_found")
                if (
                    expected_attempt_ref is not None
                    and expected_fence_ref is not None
                    and (
                        run.attempt_ref != expected_attempt_ref
                        or run.fence_ref != expected_fence_ref
                    )
                ):
                    raise OwnerConflict("writing_control_stale")
                if (expected_attempt_ref is None) != (expected_fence_ref is None):
                    raise OwnerConflict("writing_control_target_invalid")
                if action == "cancel":
                    if (
                        self._authorization_verifier is None
                        or not isinstance(cancellation_confirmation, dict)
                    ):
                        raise OwnerConflict("writing_cancel_confirmation_required")
                    try:
                        confirmation = AcceptanceReceipt(
                            issuer=cast(str, cancellation_confirmation["issuer"]),
                            kind=cast(str, cancellation_confirmation["kind"]),
                            receipt_ref=cast(
                                str, cancellation_confirmation["receipt_ref"]
                            ),
                            subject_ref=cast(
                                str, cancellation_confirmation["subject_ref"]
                            ),
                            payload_hash=cast(
                                str, cancellation_confirmation["payload_hash"]
                            ),
                        )
                        intent_id = cast(
                            str, cancellation_confirmation["intent_id"]
                        )
                        draft_revision = int(
                            cancellation_confirmation["draft_revision"]
                        )
                        draft_hash = cast(
                            str, cancellation_confirmation["draft_hash"]
                        )
                        preview_ref = cast(
                            str, cancellation_confirmation["preview_ref"]
                        )
                        preview_hash = cast(
                            str, cancellation_confirmation["preview_hash"]
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise OwnerConflict(
                            "writing_cancel_confirmation_invalid"
                        ) from error
                    confirmed = self._authorization_verifier.verify_command_confirmation(
                        intent_id=intent_id,
                        command_kind="writing_report_cancel",
                        draft_revision=draft_revision,
                        draft_hash=draft_hash,
                        preview_ref=preview_ref,
                        preview_hash=preview_hash,
                        receipt=confirmation,
                    )
                    expected = {
                        "command_kind": "writing_report_cancel",
                        "payload": {
                            "quest_ref": run.quest_ref,
                            "run_ref": run.run_ref,
                            "attempt_ref": run.attempt_ref,
                            "fence_ref": run.fence_ref,
                            "effect": "terminal_cancel_preserve_history",
                        },
                    }
                    if confirmed != expected:
                        raise OwnerConflict("writing_cancel_confirmation_stale")
                transitions = {
                    ("active", "pause"): "paused",
                    ("paused", "resume"): "active",
                    ("blocked", "resume"): "active",
                    ("active", "cancel"): "cancelled",
                    ("paused", "cancel"): "cancelled",
                    ("blocked", "cancel"): "cancelled",
                }
                target = transitions.get((run.status, action))
                if target is None:
                    if (
                        (run.status == "paused" and action == "pause")
                        or (run.status == "active" and action == "resume")
                        or (run.status == "cancelled" and action == "cancel")
                    ):
                        target = run.status
                    elif run.status in {"cancelled", "completed"}:
                        raise OwnerConflict("writing_run_terminal")
                    else:
                        raise OwnerConflict("writing_control_transition_invalid")
                now = time.time()
                if run.status in {"blocked", "paused"} and target == "active":
                    if run.status == "blocked" and run.failure_code in {
                        "writing_revision_budget_exhausted",
                        "writing_output_budget_exhausted",
                    }:
                        raise OwnerConflict(cast(str, run.failure_code))
                    attempt = connection.execute(
                        text(
                            "SELECT * FROM ar_writing_attempts WHERE attempt_ref = "
                            ":attempt_ref"
                        ),
                        {"attempt_ref": run.attempt_ref},
                    ).first()
                    if attempt is None:
                        raise OwnerConflict("writing_attempt_integrity_invalid")
                    if attempt.status in {"failed", "retired"}:
                        checkpoint = connection.execute(
                            text(
                                "SELECT * FROM ar_writing_checkpoints WHERE "
                                "attempt_ref = :attempt_ref"
                            ),
                            {"attempt_ref": run.attempt_ref},
                        ).first()
                        replacement_ref = new_ref("writing_attempt")
                        replacement_fence = new_ref("writing_fence")
                        replacement_job_ref = new_ref("writing_job")
                        generation = int(run.attempt_generation) + 1
                        replacement_status = (
                            "running" if checkpoint is not None else "admitted"
                        )
                        connection.execute(
                            text(
                                "INSERT INTO ar_writing_attempts (attempt_ref, "
                                "run_ref, generation, root_session_ref, "
                                "native_session_ref, fence_ref, provider_job_ref, status, "
                                "predecessor_version_ref, predecessor_markdown_hash, "
                                "feedback_json, feedback_hash, decision_status, "
                                "decision_receipt_json, decision_receipt_hash, "
                                "created_at, started_at) VALUES (:attempt_ref, "
                                ":run_ref, :generation, :root_session_ref, "
                                ":native_session_ref, :fence_ref, :provider_job_ref, :status, "
                                ":predecessor_version_ref, "
                                ":predecessor_markdown_hash, :feedback_json, "
                                ":feedback_hash, :decision_status, "
                                ":decision_receipt_json, :decision_receipt_hash, "
                                ":now, :started_at)"
                            ),
                            {
                                "attempt_ref": replacement_ref,
                                "run_ref": run_ref,
                                "generation": generation,
                                "root_session_ref": run.root_session_ref,
                                "native_session_ref": run.native_session_ref,
                                "fence_ref": replacement_fence,
                                "provider_job_ref": replacement_job_ref,
                                "status": replacement_status,
                                "predecessor_version_ref": (
                                    attempt.predecessor_version_ref
                                ),
                                "predecessor_markdown_hash": (
                                    attempt.predecessor_markdown_hash
                                ),
                                "feedback_json": attempt.feedback_json,
                                "feedback_hash": attempt.feedback_hash,
                                "decision_status": attempt.decision_status,
                                "decision_receipt_json": (
                                    attempt.decision_receipt_json
                                ),
                                "decision_receipt_hash": (
                                    attempt.decision_receipt_hash
                                ),
                                "now": now,
                                "started_at": now if checkpoint is not None else None,
                            },
                        )
                        if checkpoint is not None:
                            connection.execute(
                                text(
                                    "INSERT INTO ar_writing_checkpoints "
                                    "(checkpoint_ref, run_ref, attempt_ref, "
                                    "fence_ref, native_session_ref, markdown, "
                                    "markdown_hash, citations_json, citations_hash, "
                                    "created_at) VALUES (:checkpoint_ref, :run_ref, "
                                    ":attempt_ref, :fence_ref, :native_session_ref, "
                                    ":markdown, :markdown_hash, :citations_json, "
                                    ":citations_hash, :now)"
                                ),
                                {
                                    "checkpoint_ref": new_ref("writing_checkpoint"),
                                    "run_ref": run_ref,
                                    "attempt_ref": replacement_ref,
                                    "fence_ref": replacement_fence,
                                    "native_session_ref": checkpoint.native_session_ref,
                                    "markdown": checkpoint.markdown,
                                    "markdown_hash": checkpoint.markdown_hash,
                                    "citations_json": checkpoint.citations_json,
                                    "citations_hash": checkpoint.citations_hash,
                                    "now": now,
                                },
                            )
                        connection.execute(
                            text(
                                "UPDATE ar_writing_runs SET status = 'active', "
                                "failure_code = NULL, attempt_ref = :attempt_ref, "
                                "attempt_generation = :generation, fence_ref = "
                                ":fence_ref, updated_at = :now WHERE run_ref = "
                                ":run_ref"
                            ),
                            {
                                "attempt_ref": replacement_ref,
                                "generation": generation,
                                "fence_ref": replacement_fence,
                                "now": now,
                                "run_ref": run_ref,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE agent_runtime_state SET revision = revision "
                                "+ 1, writing_attempt_count = writing_attempt_count "
                                "+ 1, active_writing_run_count = "
                                "active_writing_run_count + 1 WHERE singleton = "
                                "'owner'"
                            )
                        )
                        self._feed.record(
                            connection,
                            "agent_runtime.writing_report_resumed",
                            {
                                "run_ref": run_ref,
                                "attempt_ref": replacement_ref,
                                "fence_ref": replacement_fence,
                            },
                        )
                        target = run.status
                    else:
                        connection.execute(
                            text(
                                "UPDATE ar_writing_runs SET status = 'active', "
                                "failure_code = NULL, updated_at = :now WHERE "
                                "run_ref = :run_ref"
                            ),
                            {"now": now, "run_ref": run_ref},
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_writing_attempts SET failure_code = NULL "
                                "WHERE attempt_ref = :attempt_ref"
                            ),
                            {"attempt_ref": run.attempt_ref},
                        )
                        connection.execute(
                            text(
                                "UPDATE agent_runtime_state SET revision = revision "
                                "+ 1, active_writing_run_count = "
                                "active_writing_run_count + 1 WHERE singleton = "
                                "'owner'"
                            )
                        )
                        self._feed.record(
                            connection,
                            "agent_runtime.writing_report_resumed",
                            {"run_ref": run_ref, "attempt_ref": run.attempt_ref},
                        )
                        target = run.status
                if target != run.status:
                    connection.execute(
                        text(
                            "UPDATE ar_writing_runs SET status = :status, updated_at = "
                            ":now WHERE run_ref = :run_ref"
                        ),
                        {"status": target, "now": now, "run_ref": run_ref},
                    )
                    if target == "cancelled":
                        connection.execute(
                            text(
                                "UPDATE ar_writing_attempts SET status = 'retired', "
                                "completed_at = COALESCE(completed_at, :now) WHERE "
                                "attempt_ref = :attempt_ref AND status IN "
                                "('admitted', 'running', 'paused')"
                            ),
                            {"now": now, "attempt_ref": run.attempt_ref},
                        )
                    elif target == "paused":
                        # Pausing retires the execution Fence. A subsequent
                        # resume creates a successor Attempt so a provider call
                        # that returns late can never cross RM/RG boundaries.
                        connection.execute(
                            text(
                                "UPDATE ar_writing_attempts SET status = 'retired', "
                                "completed_at = COALESCE(completed_at, :now) WHERE "
                                "attempt_ref = :attempt_ref AND status IN "
                                "('admitted', 'running', 'paused')"
                            ),
                            {"now": now, "attempt_ref": run.attempt_ref},
                        )
                    connection.execute(
                        text(
                            "UPDATE agent_runtime_state SET revision = revision + 1, "
                            "active_writing_run_count = active_writing_run_count - "
                            ":retired WHERE singleton = 'owner' AND "
                            "active_writing_run_count >= :retired"
                        ),
                        {
                            "retired": (
                                1
                                if target in {"paused", "cancelled"}
                                and run.status == "active"
                                else 0
                            )
                        },
                    )
                    self._feed.record(
                        connection,
                        f"agent_runtime.writing_report_{target}",
                        {"run_ref": run_ref, "attempt_ref": run.attempt_ref},
                    )
                _record_writing_command(
                    connection,
                    idempotency_key,
                    "control_writing_report",
                    request_hash,
                    run_ref,
                )
        result = self.query_writing_report(run_ref)
        if result is None:
            raise OwnerConflict("writing_run_not_found")
        return result

    def _query_writing_report(self, key: str, value: str) -> WritingRun | None:
        if key not in {"run_ref", "intent_id"} or not value or len(value) > 128:
            raise OwnerConflict("writing_run_ref_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(f"SELECT * FROM ar_writing_runs WHERE {key} = :value"),
                {"value": value},
            ).first()
            attempt_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_writing_attempts WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": row.attempt_ref},
                ).first()
            )
            checkpoint_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_writing_checkpoints WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": row.attempt_ref},
                ).first()
            )
            execution_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_writing_executions WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": row.attempt_ref},
                ).first()
            )
        if row is None:
            return None
        try:
            runtime_value = decoded_object(row.runtime_binding_json)
            runtime_binding = WritingRuntimeBinding(
                packaged_skill_bundle_hash=cast(
                    str, runtime_value["packaged_skill_bundle_hash"]
                ),
                instruction_set_hash=cast(str, runtime_value["instruction_set_hash"]),
                model_ref=cast(str, runtime_value["model_ref"]),
                harness_adapter_ref=cast(str, runtime_value["harness_adapter_ref"]),
                mcp_bindings=tuple(cast(list[str], runtime_value["mcp_bindings"])),
                capability_bindings=tuple(
                    cast(list[str], runtime_value["capability_bindings"])
                ),
                resource_bindings=tuple(
                    cast(list[str], runtime_value["resource_bindings"])
                ),
            )
            runtime_binding.validate()
            execution_budget = validate_writing_execution_budget(
                decoded_object(row.execution_budget_json)
            )
        except (KeyError, TypeError, OwnerConflict) as error:
            raise OwnerConflict("writing_runtime_binding_invalid") from error
        if canonical_json(runtime_binding.as_dict()) != row.runtime_binding_json:
            raise OwnerConflict("writing_runtime_binding_invalid")
        if canonical_hash(runtime_binding.as_dict()) != row.runtime_binding_hash:
            raise OwnerConflict("writing_runtime_binding_invalid")
        if (
            canonical_json(execution_budget) != row.execution_budget_json
            or canonical_hash(execution_budget) != row.execution_budget_hash
            or int(row.output_bytes) < 0
            or int(row.output_bytes)
            > int(execution_budget["max_output_bytes"])
        ):
            raise OwnerConflict("writing_execution_budget_invalid")
        try:
            intent = decoded_object(row.intent_json)
            snapshot = decoded_object(row.snapshot_json)
            normalized_intent = normalize_report_intent(intent)
            snapshot_without_hash = dict(snapshot)
            embedded_snapshot_hash = snapshot_without_hash.pop(
                "snapshot_hash", None
            )
        except (TypeError, ValueError, OwnerConflict) as error:
            raise OwnerConflict("writing_run_integrity_invalid") from error
        if (
            row.document_type != "report"
            or normalized_intent != intent
            or canonical_hash(intent) != row.intent_hash
            or snapshot.get("quest_ref") != row.quest_ref
            or snapshot.get("snapshot_ref") != row.snapshot_ref
            or embedded_snapshot_hash != row.snapshot_hash
            or canonical_hash(snapshot_without_hash) != row.snapshot_hash
        ):
            raise OwnerConflict("writing_run_integrity_invalid")
        allowed_attempt_statuses = {
            "active": {"admitted", "running", "completed"},
            "paused": {"retired", "completed"},
            "blocked": {"failed", "completed"},
            "completed": {"completed"},
            "cancelled": {"retired", "completed", "failed"},
        }
        if (
            attempt_row is None
            or row.status not in allowed_attempt_statuses
            or attempt_row.status not in allowed_attempt_statuses[row.status]
            or attempt_row.run_ref != row.run_ref
            or int(attempt_row.generation) != int(row.attempt_generation)
            or attempt_row.root_session_ref != row.root_session_ref
            or attempt_row.native_session_ref != row.native_session_ref
            or attempt_row.fence_ref != row.fence_ref
            or not isinstance(attempt_row.provider_job_ref, str)
            or not attempt_row.provider_job_ref
            or len(attempt_row.provider_job_ref) > 96
        ):
            raise OwnerConflict("writing_attempt_integrity_invalid")
        if checkpoint_row is not None and (
            checkpoint_row.run_ref != row.run_ref
            or checkpoint_row.attempt_ref != row.attempt_ref
            or checkpoint_row.fence_ref != row.fence_ref
            or checkpoint_row.native_session_ref != row.native_session_ref
        ):
            raise OwnerConflict("writing_checkpoint_integrity_invalid")
        if execution_row is not None and (
            execution_row.run_ref != row.run_ref
            or execution_row.attempt_ref != row.attempt_ref
            or execution_row.fence_ref != row.fence_ref
        ):
            raise OwnerConflict("writing_execution_integrity_invalid")
        if (
            (attempt_row.status == "admitted" and checkpoint_row is not None)
            or (attempt_row.status == "running" and checkpoint_row is None)
            or (attempt_row.status == "completed" and execution_row is None)
            or (execution_row is not None and checkpoint_row is None)
        ):
            raise OwnerConflict("writing_attempt_integrity_invalid")
        checkpoint = _writing_checkpoint_from_row(checkpoint_row)
        execution = _writing_execution_from_row(execution_row)
        if checkpoint is not None:
            _validate_writing_citations_for_snapshot(checkpoint.citations, snapshot)
        if execution is not None:
            if checkpoint is None:
                raise OwnerConflict("writing_attempt_integrity_invalid")
            _validate_writing_citations_for_snapshot(execution.citations, snapshot)
            _validate_writing_review_contract(
                review=execution.review,
                run=row,
                attempt=attempt_row,
                checkpoint=checkpoint,
                reviewed_markdown=execution.reviewed_markdown,
                final_markdown=execution.final_markdown,
                citations=execution.citations,
                final_hash=execution.final_markdown_hash,
                citations_hash=execution.citations_hash,
            )
        try:
            feedback_value = json.loads(attempt_row.feedback_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("writing_feedback_integrity_invalid") from error
        if not isinstance(feedback_value, list) or any(
            not isinstance(item, str) for item in feedback_value
        ) or canonical_hash(feedback_value) != attempt_row.feedback_hash:
            raise OwnerConflict("writing_feedback_integrity_invalid")
        decision_receipt = None
        if attempt_row.decision_receipt_json is not None:
            try:
                receipt_value = decoded_object(attempt_row.decision_receipt_json)
                if (
                    canonical_hash(receipt_value)
                    != attempt_row.decision_receipt_hash
                ):
                    raise OwnerConflict("writing_revision_lineage_invalid")
                decision_receipt = _writing_receipt_from_value(receipt_value)
            except (TypeError, KeyError, OwnerConflict) as error:
                raise OwnerConflict("writing_revision_lineage_invalid") from error
        if (
            row.predecessor_version_ref != attempt_row.predecessor_version_ref
            or row.feedback_json != attempt_row.feedback_json
            or row.feedback_hash != attempt_row.feedback_hash
            or bool(attempt_row.predecessor_version_ref)
            != bool(feedback_value)
            or bool(attempt_row.predecessor_version_ref)
            != bool(attempt_row.predecessor_markdown_hash)
            or bool(attempt_row.predecessor_version_ref)
            != bool(decision_receipt)
            or (
                attempt_row.decision_status is not None
                and attempt_row.decision_status not in {"accepted", "rejected"}
            )
        ):
            raise OwnerConflict("writing_revision_lineage_invalid")
        return WritingRun(
            run_ref=row.run_ref,
            intent_id=row.intent_id,
            quest_ref=row.quest_ref,
            status=row.status,
            attempt_ref=row.attempt_ref,
            attempt_generation=int(row.attempt_generation),
            root_session_ref=row.root_session_ref,
            native_session_ref=row.native_session_ref,
            fence_ref=row.fence_ref,
            provider_job_ref=attempt_row.provider_job_ref,
            runtime_binding=runtime_binding,
            runtime_binding_hash=row.runtime_binding_hash,
            execution_budget=execution_budget,
            output_bytes=int(row.output_bytes),
            failure_code=row.failure_code,
            checkpoint=checkpoint,
            execution=execution,
            predecessor_version_ref=row.predecessor_version_ref,
            predecessor_markdown_hash=attempt_row.predecessor_markdown_hash,
            feedback=tuple(feedback_value),
            decision_status=attempt_row.decision_status,
            decision_receipt=decision_receipt,
        )

    def query_snapshot(self) -> OwnerSnapshot:
        return self._snapshot.query_snapshot()

    def query_managed_run(self, run_ref: str) -> dict[str, object] | None:
        if not isinstance(run_ref, str) or not run_ref or len(run_ref) > 96:
            raise OwnerConflict("run_ref_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM ar_run_controls WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            safe_point = _query_managed_safe_point(connection, row)
        if row is None:
            return None
        return _managed_run_document(row, safe_point)

    def query_managed_runs(self, quest_ref: str) -> tuple[dict[str, object], ...]:
        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 96:
            raise OwnerConflict("quest_ref_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_run_controls WHERE quest_ref = :quest_ref "
                    "ORDER BY updated_at DESC, run_kind, run_ref"
                ),
                {"quest_ref": quest_ref},
            ).all()
            values = tuple(
                _managed_run_document(
                    row, _query_managed_safe_point(connection, row)
                )
                for row in rows
            )
        return values

    def query_runtime_control_receipt(
        self, operation_ref: str
    ) -> dict[str, object] | None:
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("runtime_control_operation_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        return None if row is None else _runtime_control_receipt(row)

    def verify_runtime_control_receipt(self, **values) -> None:
        self._receipt_verifier.verify_runtime_control_receipt(**values)

    def verify_runtime_quiescence_receipt(self, **values) -> None:
        self._receipt_verifier.verify_runtime_quiescence_receipt(**values)

    def begin_provider_unit(
        self,
        *,
        unit_ref: str,
        operation_ref: str | None = None,
        run_ref: str,
        attempt_ref: str | None,
        fence_ref: str | None,
        unit_kind: str,
    ) -> None:
        if unit_kind not in _PROVIDER_UNIT_KINDS:
            raise OwnerConflict("runtime_provider_unit_invalid")
        if not unit_ref or len(unit_ref) > 96:
            raise OwnerConflict("runtime_provider_unit_invalid")
        now = time.time()
        with self._database.write() as connection:
            if operation_ref is None:
                operation_ref = _provider_operation_for_unit(
                    connection, unit_ref=unit_ref, run_ref=run_ref
                )
            if not operation_ref or len(operation_ref) > 128:
                raise OwnerConflict("runtime_provider_unit_invalid")
            control = connection.execute(
                text("SELECT * FROM ar_run_controls WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            if control is None or (
                control.attempt_ref != attempt_ref or control.fence_ref != fence_ref
            ):
                raise OwnerConflict("runtime_provider_unit_stale")
            if fence_ref is not None:
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
            existing = connection.execute(
                text("SELECT * FROM ar_provider_units WHERE unit_ref = :unit_ref"),
                {"unit_ref": unit_ref},
            ).first()
            if existing is not None:
                if (
                    existing.run_ref != run_ref
                    or existing.operation_ref != operation_ref
                    or existing.attempt_ref != attempt_ref
                    or existing.fence_ref != fence_ref
                    or existing.unit_kind != unit_kind
                ):
                    raise OwnerConflict("runtime_provider_unit_conflict")
                if existing.status == "active":
                    return
                if existing.status in {"revoked", "revocation_pending"}:
                    raise OwnerConflict("runtime_provider_unit_stale")
                _assert_provider_derivation_not_quiescing(connection, run_ref)
                active = connection.execute(
                    text(
                        "SELECT unit_ref FROM ar_provider_units WHERE run_ref = "
                        ":run_ref AND status = 'active' LIMIT 1"
                    ),
                    {"run_ref": run_ref},
                ).first()
                if active is not None:
                    raise OwnerConflict("runtime_provider_unit_busy")
                connection.execute(
                    text(
                        "UPDATE ar_provider_units SET status = 'active', started_at = "
                        ":now, completed_at = NULL WHERE unit_ref = :unit_ref AND "
                        "status = 'completed'"
                    ),
                    {"now": now, "unit_ref": unit_ref},
                )
                self._feed.record(
                    connection,
                    "agent_runtime.provider_unit_restarted",
                    {
                        "unit_ref": unit_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "unit_kind": unit_kind,
                    },
                )
                return
            _assert_provider_derivation_not_quiescing(connection, run_ref)
            active = connection.execute(
                text(
                    "SELECT unit_ref FROM ar_provider_units WHERE run_ref = :run_ref "
                    "AND status = 'active' LIMIT 1"
                ),
                {"run_ref": run_ref},
            ).first()
            if active is not None:
                raise OwnerConflict("runtime_provider_unit_busy")
            connection.execute(
                text(
                    "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
                    "attempt_ref, fence_ref, unit_kind, status, started_at, "
                    "completed_at) VALUES (:unit_ref, :operation_ref, :run_ref, "
                    ":attempt_ref, :fence_ref, :unit_kind, 'active', :now, NULL)"
                ),
                {
                    "unit_ref": unit_ref,
                    "operation_ref": operation_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "unit_kind": unit_kind,
                    "now": now,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.provider_unit_started",
                {
                    "unit_ref": unit_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "unit_kind": unit_kind,
                },
            )

    def bind_provider_quiescence_driver(
        self, provider: object, *, unit_kinds: tuple[str, ...]
    ) -> None:
        """Bind restart-reconstructible operation stop seams to provider unit kinds."""

        if (
            not unit_kinds
            or len(set(unit_kinds)) != len(unit_kinds)
            or not set(unit_kinds).issubset(_PROVIDER_UNIT_KINDS)
        ):
            raise OwnerConflict("runtime_provider_unit_invalid")
        with self._provider_quiescence_lock:
            for unit_kind in unit_kinds:
                bound = self._provider_quiescence_drivers.get(unit_kind)
                if bound is not None and bound is not provider:
                    raise OwnerConflict("runtime_provider_quiescence_driver_conflict")
            for unit_kind in unit_kinds:
                self._provider_quiescence_drivers[unit_kind] = provider

    def provider_quiescence_requested(self, run_ref: str) -> bool:
        """Tell a worker that a provider stop is an expected control boundary."""

        if not isinstance(run_ref, str) or not run_ref:
            raise OwnerConflict("runtime_run_ref_invalid")
        with self._database.read() as connection:
            return _provider_quiescence_requested(connection, run_ref)

    def acknowledge_provider_safe_point(
        self,
        *,
        unit_ref: str | None = None,
        run_ref: str,
        attempt_ref: str | None,
        fence_ref: str | None,
    ) -> None:
        if unit_ref is not None and (not unit_ref or len(unit_ref) > 96):
            raise OwnerConflict("runtime_provider_unit_invalid")
        with self._database.write() as connection:
            unit = connection.execute(
                text(
                    "SELECT * FROM ar_provider_units WHERE "
                    "(:unit_ref IS NOT NULL AND unit_ref = :unit_ref) OR "
                    "(:unit_ref IS NULL AND run_ref = :run_ref AND attempt_ref IS "
                    ":attempt_ref AND fence_ref IS :fence_ref AND status IN "
                    "('active', 'revocation_pending')) "
                    "ORDER BY started_at DESC LIMIT 1"
                ),
                {
                    "unit_ref": unit_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                },
            ).first()
            if unit is None:
                terminal = connection.execute(
                    text(
                        "SELECT unit_ref FROM ar_provider_units WHERE run_ref = "
                        ":run_ref AND attempt_ref IS :attempt_ref AND fence_ref IS "
                        ":fence_ref AND status IN ('completed', 'revoked') LIMIT 1"
                    ),
                    {
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                    },
                ).first()
                if terminal is not None:
                    return
                raise OwnerConflict("runtime_provider_unit_not_found")
            if (
                unit.run_ref != run_ref
                or unit.attempt_ref != attempt_ref
                or unit.fence_ref != fence_ref
            ):
                raise OwnerConflict("runtime_provider_unit_stale")
            unit_ref = unit.unit_ref
            if unit.status not in {"active", "revocation_pending"}:
                return
            revoked = (
                fence_ref is not None
                and connection.execute(
                    text(
                        "SELECT fence_ref FROM ar_fence_revocations WHERE fence_ref = "
                        ":fence_ref AND run_ref = :run_ref"
                    ),
                    {"fence_ref": fence_ref, "run_ref": run_ref},
                ).first()
                is not None
            )
            status = "revoked" if revoked else "completed"
            now = time.time()
            connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = :status, completed_at = "
                    ":now WHERE unit_ref = :unit_ref AND status IN ('active', "
                    "'revocation_pending')"
                ),
                {"status": status, "now": now, "unit_ref": unit_ref},
            )
            # Replacement Attempts reuse the external operation_ref.  A verified
            # terminal response for that operation is also the missing safe ACK
            # for any pre-restart physical unit left revocation_pending.
            connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'revoked', completed_at = "
                    ":now WHERE run_ref = :run_ref AND operation_ref = "
                    ":operation_ref AND unit_ref != :unit_ref AND status = "
                    "'revocation_pending'"
                ),
                {
                    "now": now,
                    "run_ref": run_ref,
                    "operation_ref": unit.operation_ref,
                    "unit_ref": unit_ref,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.provider_unit_safe",
                {
                    "unit_ref": unit_ref,
                    "run_ref": run_ref,
                    "status": status,
                },
            )
            cleanup_completed = connection.execute(
                text(
                    "UPDATE ar_run_controls SET cleanup_status = 'completed', "
                    "updated_at = :now WHERE run_ref = :run_ref AND "
                    "cleanup_status = 'pending' AND NOT EXISTS "
                    "(SELECT 1 FROM ar_provider_units pending WHERE pending.run_ref = "
                    ":run_ref AND pending.status IN ('active', "
                    "'revocation_pending'))"
                ),
                {"now": now, "run_ref": run_ref},
            )
            if cleanup_completed.rowcount:
                self._feed.record(
                    connection,
                    "agent_runtime.run_cleanup_completed",
                    {"run_ref": run_ref, "unit_ref": unit_ref},
                )

    def reconcile_pending_provider_cleanup(
        self,
        provider: object,
        *,
        unit_kinds: tuple[str, ...],
    ) -> bool:
        """Close one fenced provider cleanup only after provider-owned exit proof."""

        if (
            not unit_kinds
            or len(set(unit_kinds)) != len(unit_kinds)
            or not set(unit_kinds).issubset(_PROVIDER_UNIT_KINDS)
        ):
            raise OwnerConflict("runtime_provider_unit_invalid")
        reconcile = getattr(provider, "reconcile_cancelled_job", None)
        if not callable(reconcile):
            return False
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT units.* FROM ar_provider_units units JOIN "
                    "ar_run_controls controls ON controls.run_ref = units.run_ref "
                    "WHERE units.status = 'revocation_pending' AND "
                    "controls.cleanup_status = 'pending' ORDER "
                    "BY units.started_at, units.unit_ref"
                )
            ).all()
        for row in rows:
            if row.unit_kind not in unit_kinds:
                continue
            if reconcile(str(row.operation_ref)) is not True:
                continue
            self.acknowledge_provider_safe_point(
                unit_ref=row.unit_ref,
                run_ref=row.run_ref,
                attempt_ref=row.attempt_ref,
                fence_ref=row.fence_ref,
            )
            return True
        return False

    def preview_runtime_control(
        self,
        payload: dict[str, object],
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> tuple[dict[str, object], int]:
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        source_stage = _validated_control_source_stage(action, source_stage)
        with self._database.read() as connection:
            revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM agent_runtime_state WHERE singleton = "
                        "'owner'"
                    )
                ).scalar_one()
            )
            rows = _controlled_runs_for_target(
                connection,
                target,
                action,
                affected_question_refs=affected_question_refs,
            )
            _validate_formal_run_control_scope(target, action, rows)
        affected = [_managed_run_document(row) for row in rows]
        assertion = {
            "owner": AR_OWNER,
            "operation": "control_managed_runs",
            "action": action,
            "target_scope": target["target_scope"],
            "quest_ref": target["quest_ref"],
            "cycle_ref": target["cycle_ref"],
            "epoch": target["epoch"],
            "run_ref": target.get("run_ref"),
            "source_stage": source_stage,
            "affected_question_refs": list(affected_question_refs or ()),
            "affected_runs": [
                {
                    "run_ref": item["run_ref"],
                    "run_kind": item["run_kind"],
                    "attempt_ref": item["attempt_ref"],
                    "root_session_ref": item["root_session_ref"],
                    "fence_ref": item["fence_ref"],
                    "status": item["status"],
                }
                for item in affected
            ],
            "owner_revision": revision,
        }
        if action in FORCE_FENCE_ACTIONS:
            will_happen = [
                "为每个受影响的 managed Run 写入 durable Safe Point",
                "先永久撤销旧 Attempt Fence，再进行异步资源清理",
            ]
        elif action == "pause":
            will_happen = [
                "受影响 Run 在安全边界进入 suspended",
                "root Session 与当前 Attempt 身份保持可恢复",
            ]
        elif action == "normal_switch":
            will_happen = (
                [
                    "冻结当前 Bundle Target Run 集并等待精确 Safe Point",
                    "所有冻结 Target 到达安全边界后才允许前台交接",
                ]
                if source_stage == "bundle"
                else [
                    "当前 StageRun 保持可执行，直到形成 StageCommit",
                    "Agent Runtime 记录精确受影响 Run 集供交接时复核",
                ]
            )
        else:
            will_happen = [
                "只恢复非 terminal Run",
                "若旧 Fence 已撤销，则沿同一 Run revision 建立替代 Attempt",
            ]
        preview = signed_owner_preview(
            source_owner=AR_OWNER,
            target_assertion=assertion,
            will_happen=will_happen,
            will_not_happen=[
                "不会接纳领域结果或提交 Stage",
                "不会把 Harness child agent tree 注册成额外 managed Run",
                "terminal Run 不会被重新打开",
            ],
            risks=[
                "外部 provider 效果未知时会停在 reconciliation_required",
                "force terminate 的物理清理可能晚于逻辑终止",
            ],
            stale_conditions=[
                "Run、Attempt、Session 或 Fence 身份改变",
                "Agent Runtime owner revision 改变",
            ],
        )
        return preview, revision

    def prepare_runtime_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> dict[str, object]:
        _validate_stage_idempotency_key(idempotency_key)
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("runtime_control_operation_invalid")
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        source_stage = _validated_control_source_stage(action, source_stage)
        payload_json = canonical_json(control)
        payload_hash = canonical_hash(control)
        question_scope = list(affected_question_refs or ())
        if len(question_scope) != len(set(question_scope)):
            raise OwnerConflict("question_control_affected_set_invalid")
        question_scope_hash = canonical_hash(question_scope)
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_control_reservations WHERE operation_ref = "
                    ":operation_ref OR idempotency_key = :idempotency_key"
                ),
                {
                    "operation_ref": operation_ref,
                    "idempotency_key": idempotency_key,
                },
            ).first()
            if replay is not None:
                if (
                    replay.operation_ref != operation_ref
                    or replay.payload_hash != payload_hash
                    or int(replay.expected_revision) != expected_revision
                    or replay.affected_question_refs_hash != question_scope_hash
                    or (
                        source_stage is not None
                        and replay.source_stage != source_stage
                    )
                ):
                    raise OwnerConflict("idempotency_conflict")
                if replay.status == "aborted":
                    raise OwnerConflict("runtime_control_repreview_required")
                return _runtime_control_reservation_document(replay)
            if action == "normal_switch" and source_stage is None:
                raise OwnerConflict("runtime_control_source_stage_required")
            revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM agent_runtime_state WHERE singleton = "
                        "'owner'"
                    )
                ).scalar_one()
            )
            if revision != expected_revision:
                raise OwnerConflict("command_preview_stale")
            rows = _controlled_runs_for_target(
                connection,
                target,
                action,
                affected_question_refs=affected_question_refs,
            )
            _validate_formal_run_control_scope(target, action, rows)
            affected = [_managed_run_reservation(row) for row in rows]
            affected_hash = canonical_hash(affected)
            connection.execute(
                text(
                    "INSERT INTO ar_control_reservations (operation_ref, "
                    "idempotency_key, action, payload_json, payload_hash, "
                    "expected_revision, source_stage, affected_runs_json, "
                    "affected_runs_hash, "
                    "affected_question_refs_json, affected_question_refs_hash, "
                    "status, created_at, updated_at) VALUES (:operation_ref, "
                    ":idempotency_key, :action, :payload_json, :payload_hash, "
                    ":expected_revision, :source_stage, :affected_runs_json, "
                    ":affected_runs_hash, "
                    ":affected_question_refs_json, :affected_question_refs_hash, "
                    "'prepared', :now, :now)"
                ),
                {
                    "operation_ref": operation_ref,
                    "idempotency_key": idempotency_key,
                    "action": action,
                    "payload_json": payload_json,
                    "payload_hash": payload_hash,
                    "expected_revision": expected_revision,
                    "source_stage": source_stage,
                    "affected_question_refs_json": canonical_json(question_scope),
                    "affected_question_refs_hash": question_scope_hash,
                    "affected_runs_json": canonical_json(affected),
                    "affected_runs_hash": affected_hash,
                    "now": now,
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_reservations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
        return _runtime_control_reservation_document(row)

    def abort_runtime_control(
        self, *, operation_ref: str, reason_code: str
    ) -> None:
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("runtime_control_operation_invalid")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
            raise OwnerConflict("runtime_control_abort_reason_invalid")
        with self._database.write() as connection:
            completed = connection.execute(
                text(
                    "SELECT operation_ref FROM ar_control_operations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if completed is not None:
                raise OwnerConflict("runtime_control_already_completed")
            connection.execute(
                text(
                    "UPDATE ar_control_reservations SET status = 'aborted', "
                    "updated_at = :now WHERE operation_ref = :operation_ref AND "
                    "status = 'prepared'"
                ),
                {"now": time.time(), "operation_ref": operation_ref},
            )

    def compensate_runtime_control(
        self, *, operation_ref: str, reason_code: str
    ) -> dict[str, object]:
        """Safely roll an applied cross-Owner control back to its prior intent.

        Revoked Fences are never resurrected.  Previously-running Runs receive a
        technical replacement under the same Run/root Session; previously-paused
        Runs remain fenced/suspended and can be resumed explicitly later.
        """

        if not operation_ref or not reason_code or len(reason_code) > 96:
            raise OwnerConflict("runtime_control_compensation_invalid")
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_control_compensations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if replay is not None:
                affected = json.loads(replay.affected_runs_json)
                if canonical_hash(affected) != replay.affected_runs_hash:
                    raise OwnerConflict("runtime_control_compensation_invalid")
                return {
                    "operation_ref": operation_ref,
                    "reason_code": replay.reason_code,
                    "affected_runs": affected,
                }
            operation = connection.execute(
                text(
                    "SELECT operation_ref FROM ar_control_operations WHERE "
                    "operation_ref = :operation_ref AND action IN ('prune', "
                    "'restore', 'normal_switch', 'forced_switch')"
                ),
                {"operation_ref": operation_ref},
            ).first()
            reservation = connection.execute(
                text(
                    "SELECT * FROM ar_control_reservations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if operation is None or reservation is None:
                raise OwnerConflict("runtime_control_compensation_invalid")
            originals = json.loads(reservation.affected_runs_json)
            compensated: list[dict[str, object]] = []
            for original in originals:
                control = connection.execute(
                    text("SELECT * FROM ar_run_controls WHERE run_ref = :run_ref"),
                    {"run_ref": original["run_ref"]},
                ).first()
                if control is None:
                    raise OwnerConflict("runtime_control_compensation_invalid")
                original_status = original["status"]
                if original_status == "running":
                    if control.status in {"completed", "terminated"}:
                        raise OwnerConflict("runtime_control_compensation_invalid")
                    replacement = self._replace_fenced_managed_attempt(
                        connection, control, now
                    )
                    status = "running"
                    attempt_ref = replacement.attempt_ref
                    fence_ref = replacement.fence_ref
                    safe_point_ref = None
                elif original_status in {"suspended", "suspended_fenced"}:
                    status = "suspended_fenced"
                    attempt_ref = control.attempt_ref
                    fence_ref = control.fence_ref
                    safe_point_ref = control.safe_point_ref
                    if fence_ref is not None:
                        connection.execute(
                            text(
                                "INSERT INTO ar_fence_revocations (fence_ref, "
                                "operation_ref, run_ref, attempt_ref, reason_code, "
                                "revoked_at) VALUES (:fence_ref, :operation_ref, "
                                ":run_ref, :attempt_ref, 'runtime_control_compensated', "
                                ":now) ON CONFLICT(fence_ref) DO NOTHING"
                            ),
                            {
                                "fence_ref": fence_ref,
                                "operation_ref": operation_ref,
                                "run_ref": control.run_ref,
                                "attempt_ref": attempt_ref,
                                "now": now,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_execution_fences SET status = 'rejected', "
                                "closed_at = COALESCE(closed_at, :now) WHERE fence_ref "
                                "= :fence_ref AND status IN ('current', 'submitted')"
                            ),
                            {"now": now, "fence_ref": fence_ref},
                        )
                else:
                    status = original_status
                    attempt_ref = control.attempt_ref
                    fence_ref = control.fence_ref
                    safe_point_ref = control.safe_point_ref
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = :status, attempt_ref = "
                        ":attempt_ref, fence_ref = :fence_ref, safe_point_ref = "
                        ":safe_point_ref, terminal_reason = NULL, cleanup_status = "
                        "'none', control_revision = control_revision + 1, updated_at = "
                        ":now WHERE run_ref = :run_ref"
                    ),
                    {
                        "status": status,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "safe_point_ref": safe_point_ref,
                        "now": now,
                        "run_ref": control.run_ref,
                    },
                )
                compensated.append(
                    {
                        "run_ref": control.run_ref,
                        "status": status,
                        "attempt_ref": attempt_ref,
                        "root_session_ref": control.root_session_ref,
                        "fence_ref": fence_ref,
                    }
                )
            affected_hash = canonical_hash(compensated)
            connection.execute(
                text(
                    "INSERT INTO ar_control_compensations (operation_ref, "
                    "reason_code, affected_runs_json, affected_runs_hash, created_at) "
                    "VALUES (:operation_ref, :reason_code, :affected_runs_json, "
                    ":affected_runs_hash, :now)"
                ),
                {
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                    "affected_runs_json": canonical_json(compensated),
                    "affected_runs_hash": affected_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 WHERE "
                    "singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.control_compensated",
                {
                    "operation_ref": operation_ref,
                    "reason_code": reason_code,
                    "affected_run_refs": [item["run_ref"] for item in compensated],
                },
            )
        return {
            "operation_ref": operation_ref,
            "reason_code": reason_code,
            "affected_runs": compensated,
        }

    def query_runtime_control_compensation(
        self, operation_ref: str
    ) -> dict[str, object] | None:
        if not operation_ref:
            raise OwnerConflict("runtime_control_operation_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_compensations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        if row is None:
            return None
        affected = json.loads(row.affected_runs_json)
        if canonical_hash(affected) != row.affected_runs_hash:
            raise OwnerConflict("runtime_control_compensation_invalid")
        return {
            "operation_ref": row.operation_ref,
            "reason_code": row.reason_code,
            "affected_runs": affected,
            "created_at": float(row.created_at),
        }

    def apply_runtime_control(
        self,
        *,
        operation_ref: str,
        payload: dict[str, object],
        expected_revision: int,
        idempotency_key: str,
        affected_question_refs: tuple[str, ...] | None = None,
        source_stage: str | None = None,
    ) -> dict[str, object]:
        _validate_stage_idempotency_key(idempotency_key)
        if not isinstance(operation_ref, str) or not operation_ref:
            raise OwnerConflict("runtime_control_operation_invalid")
        control = validate_control_payload(payload)
        action = cast(str, control["action"])
        target = cast(dict[str, object], control["target"])
        reservation_document = self.prepare_runtime_control(
            operation_ref=operation_ref,
            payload=control,
            expected_revision=expected_revision,
            idempotency_key=(
                "runtime-control-prepare-"
                + canonical_hash({"operation_ref": operation_ref})[:48]
            ),
            affected_question_refs=affected_question_refs,
            source_stage=source_stage,
        )
        persisted_source_stage = reservation_document.get("source_stage")
        if (
            persisted_source_stage is not None
            and persisted_source_stage not in FORMAL_STAGES
        ):
            raise OwnerConflict("runtime_control_reservation_invalid")
        source_stage = cast(str | None, persisted_source_stage)
        command_hash = canonical_hash(
            {
                "command": "apply_runtime_control",
                "operation_ref": operation_ref,
                "payload": control,
                "expected_revision": expected_revision,
                "affected_question_refs": list(affected_question_refs or ()),
                "source_stage": source_stage,
            }
        )
        persisted_question_scope = reservation_document.get(
            "affected_question_refs"
        )
        if not isinstance(persisted_question_scope, list) or not all(
            isinstance(item, str) and item for item in persisted_question_scope
        ):
            raise OwnerConflict("runtime_control_reservation_invalid")
        affected_question_refs = tuple(persisted_question_scope)
        bundle_handoff = action == "normal_switch" and source_stage == "bundle"
        if action in {"pause", "prune"} or bundle_handoff:
            self._wait_for_provider_quiescence(
                tuple(
                    cast(str, item["run_ref"])
                    for item in cast(
                        list[dict[str, object]],
                        reservation_document["affected_runs"],
                    )
                )
            )
        now = time.time()
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_control_operations WHERE idempotency_key = "
                    ":idempotency_key OR operation_ref = :operation_ref"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "operation_ref": operation_ref,
                },
            ).first()
            if replay is not None:
                if replay.command_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _runtime_control_receipt(replay)
            reservation = connection.execute(
                text(
                    "SELECT * FROM ar_control_reservations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
            if reservation.status != "prepared":
                raise OwnerConflict("runtime_control_repreview_required")
            rows = _controlled_runs_for_target(
                connection,
                target,
                action,
                affected_question_refs=affected_question_refs,
            )
            reserved_runs = json.loads(reservation.affected_runs_json)
            current_runs = [_managed_run_reservation(row) for row in rows]
            reservation_changed = current_runs != reserved_runs
            if action == "pause":
                reservation_changed = not _same_managed_run_identities(
                    reserved_runs, current_runs
                )
            if (
                canonical_hash(reserved_runs) != reservation.affected_runs_hash
                or reservation_changed
            ):
                connection.execute(
                    text(
                        "UPDATE ar_control_reservations SET status = 'aborted', "
                        "updated_at = :now WHERE operation_ref = :operation_ref"
                    ),
                    {"now": now, "operation_ref": operation_ref},
                )
                raise OwnerConflict("runtime_control_reservation_stale")
            if (action in {"pause", "prune"} or bundle_handoff) and reserved_runs:
                active_provider = connection.execute(
                    text(
                        "SELECT unit_ref FROM ar_provider_units WHERE run_ref IN "
                        "(SELECT value FROM json_each(:run_refs_json)) AND status IN "
                        "('active', 'revocation_pending') LIMIT 1"
                    ),
                    {
                        "run_refs_json": canonical_json(
                            [item["run_ref"] for item in reserved_runs]
                        )
                    },
                ).first()
                if active_provider is not None:
                    raise OwnerConflict("runtime_quiescence_pending")
            affected_runs: list[dict[str, object]] = []
            safe_points: list[dict[str, object]] = []
            fenced_count = 0
            terminated_count = 0
            for row in rows:
                if row.status in {"completed", "terminated"}:
                    # A Cycle may resume after an operator cancelled its previous
                    # technical Runs.  Those terminal Run identities remain closed;
                    # the next StageRunRequest admits a new logical Run instead.
                    if row.status == "terminated" and action in {"resume", "restore"}:
                        continue
                    if action == "pause" or bundle_handoff:
                        safe_point = _record_safe_point(
                            connection,
                            operation_ref=operation_ref,
                            row=row,
                            action=action,
                            now=now,
                        )
                        safe_points.append(safe_point)
                        connection.execute(
                            text(
                                "UPDATE ar_run_controls SET safe_point_ref = "
                                ":safe_point_ref, control_revision = control_revision + "
                                "1, updated_at = :now WHERE run_ref = :run_ref"
                            ),
                            {
                                "safe_point_ref": safe_point["safe_point_ref"],
                                "now": now,
                                "run_ref": row.run_ref,
                            },
                        )
                        affected_runs.append(
                            {
                                "run_ref": row.run_ref,
                                "run_kind": row.run_kind,
                                "status": row.status,
                                "attempt_ref": row.attempt_ref,
                                "root_session_ref": row.root_session_ref,
                                "fence_ref": row.fence_ref,
                                "safe_point_ref": safe_point["safe_point_ref"],
                                "cleanup_status": row.cleanup_status,
                            }
                        )
                    continue
                if action == "normal_switch" and not bundle_handoff:
                    # A normal handoff is not a pause shortcut.  Resolution #58
                    # requires the source StageRun to remain live until its
                    # StageCommit exists; AE retains the source Grant meanwhile.
                    # If the daemon died after prepare, startup deliberately kept
                    # the reserved identity and marked its physical provider unit
                    # pending.  Only now, after the frozen CAS succeeded, may AR
                    # revoke that technical Fence and resume the same logical
                    # Run/root Session through a replacement Attempt.
                    interrupted_unit = connection.execute(
                        text(
                            "SELECT unit_ref FROM ar_provider_units WHERE run_ref = "
                            ":run_ref AND attempt_ref IS :attempt_ref AND fence_ref "
                            "IS :fence_ref AND status = 'revocation_pending' LIMIT 1"
                        ),
                        {
                            "run_ref": row.run_ref,
                            "attempt_ref": row.attempt_ref,
                            "fence_ref": row.fence_ref,
                        },
                    ).first()
                    if interrupted_unit is not None:
                        if row.fence_ref is not None:
                            inserted = connection.execute(
                                text(
                                    "INSERT INTO ar_fence_revocations (fence_ref, "
                                    "operation_ref, run_ref, attempt_ref, reason_code, "
                                    "revoked_at) VALUES (:fence_ref, :operation_ref, "
                                    ":run_ref, :attempt_ref, "
                                    "'daemon_restart_normal_handoff', :now) ON "
                                    "CONFLICT(fence_ref) DO NOTHING"
                                ),
                                {
                                    "fence_ref": row.fence_ref,
                                    "operation_ref": operation_ref,
                                    "run_ref": row.run_ref,
                                    "attempt_ref": row.attempt_ref,
                                    "now": now,
                                },
                            )
                            fenced_count += int(inserted.rowcount or 0)
                            connection.execute(
                                text(
                                    "UPDATE ar_execution_fences SET status = "
                                    "'rejected', closed_at = COALESCE(closed_at, :now) "
                                    "WHERE fence_ref = :fence_ref AND status IN "
                                    "('current', 'submitted')"
                                ),
                                {"now": now, "fence_ref": row.fence_ref},
                            )
                        row = self._replace_fenced_managed_attempt(
                            connection, row, now
                        )
                    next_status = row.status
                    cleanup_status = row.cleanup_status
                    safe_point_ref = row.safe_point_ref
                elif action == "normal_switch":
                    safe_point = _record_safe_point(
                        connection,
                        operation_ref=operation_ref,
                        row=row,
                        action=action,
                        now=now,
                    )
                    safe_points.append(safe_point)
                    row = self._retire_quiesced_provider_attempt(
                        connection, row, now
                    )
                    next_status = "suspended"
                    cleanup_status = "none"
                    safe_point_ref = safe_point["safe_point_ref"]
                elif action == "resume":
                    if row.status in {
                        "suspended_fenced",
                        "reconciliation_required",
                    }:
                        row = self._replace_fenced_managed_attempt(
                            connection, row, now
                        )
                    next_status = "running"
                    cleanup_status = "none"
                    safe_point_ref = row.safe_point_ref
                elif action == "restore":
                    if row.status in {
                        "suspended_fenced",
                        "reconciliation_required",
                    }:
                        row = self._replace_fenced_managed_attempt(
                            connection, row, now
                        )
                    next_status = "suspended"
                    cleanup_status = "none"
                    safe_point_ref = row.safe_point_ref
                else:
                    safe_point = _record_safe_point(
                        connection,
                        operation_ref=operation_ref,
                        row=row,
                        action=action,
                        now=now,
                    )
                    safe_points.append(safe_point)
                    safe_point_ref = safe_point["safe_point_ref"]
                    if action == "pause":
                        row = self._retire_quiesced_provider_attempt(
                            connection, row, now
                        )
                    pending_cleanup_units = 0
                    if action in FORCE_FENCE_ACTIONS:
                        pending_cleanup_units = int(
                            connection.execute(
                                text(
                                    "SELECT COUNT(*) FROM ar_provider_units WHERE "
                                    "run_ref = :run_ref AND status IN ('active', "
                                    "'revocation_pending')"
                                ),
                                {"run_ref": row.run_ref},
                            ).scalar_one()
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_provider_units SET status = "
                                "'revocation_pending' WHERE run_ref = :run_ref AND "
                                "status = 'active'"
                            ),
                            {"run_ref": row.run_ref},
                        )
                    if action in FORCE_FENCE_ACTIONS and row.fence_ref is not None:
                        inserted = connection.execute(
                            text(
                                "INSERT INTO ar_fence_revocations (fence_ref, "
                                "operation_ref, run_ref, attempt_ref, reason_code, "
                                "revoked_at) VALUES (:fence_ref, :operation_ref, "
                                ":run_ref, :attempt_ref, :reason_code, :now) ON "
                                "CONFLICT(fence_ref) DO NOTHING"
                            ),
                            {
                                "fence_ref": row.fence_ref,
                                "operation_ref": operation_ref,
                                "run_ref": row.run_ref,
                                "attempt_ref": row.attempt_ref,
                                "reason_code": f"runtime_control_{action}",
                                "now": now,
                            },
                        )
                        fenced_count += int(inserted.rowcount or 0)
                    next_status = (
                        "terminated"
                        if action in TERMINAL_ACTIONS
                        else "reconciliation_required"
                        if action in FORCE_FENCE_ACTIONS
                        and row.run_kind in {"deepfetch", "experiment"}
                        else "suspended_fenced"
                        if action in FORCE_FENCE_ACTIONS
                        else "suspended"
                    )
                    cleanup_status = (
                        "pending"
                        if pending_cleanup_units
                        else "completed"
                        if action in TERMINAL_ACTIONS
                        else "none"
                    )
                    if action in TERMINAL_ACTIONS:
                        _terminate_canonical_managed_run(
                            connection,
                            row=row,
                            reason_code=f"runtime_control_{action}",
                            now=now,
                        )
                        # Experiments have their own active counter and have never
                        # contributed to the generic active_run_count.
                        if row.run_kind != "experiment":
                            terminated_count += 1
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = :status, "
                        "attempt_ref = :attempt_ref, root_session_ref = "
                        ":root_session_ref, fence_ref = :fence_ref, "
                        "control_revision = control_revision + 1, safe_point_ref = "
                        ":safe_point_ref, terminal_reason = :terminal_reason, "
                        "cleanup_status = :cleanup_status, updated_at = :now WHERE "
                        "run_ref = :run_ref"
                    ),
                    {
                        "status": next_status,
                        "attempt_ref": row.attempt_ref,
                        "root_session_ref": row.root_session_ref,
                        "fence_ref": row.fence_ref,
                        "safe_point_ref": safe_point_ref,
                        "terminal_reason": (
                            f"runtime_control_{action}"
                            if action in TERMINAL_ACTIONS
                            else None
                        ),
                        "cleanup_status": cleanup_status,
                        "now": now,
                        "run_ref": row.run_ref,
                    },
                )
                affected_runs.append(
                    {
                        "run_ref": row.run_ref,
                        "run_kind": row.run_kind,
                        "status": next_status,
                        "attempt_ref": row.attempt_ref,
                        "root_session_ref": row.root_session_ref,
                        "fence_ref": row.fence_ref,
                        "safe_point_ref": safe_point_ref,
                        "cleanup_status": cleanup_status,
                    }
                )
            receipt_ref = new_ref("ar_control_receipt")
            affected_hash = canonical_hash(affected_runs)
            safe_points_hash = canonical_hash(safe_points)
            question_scope = list(affected_question_refs)
            question_scope_hash = canonical_hash(question_scope)
            receipt_hash = canonical_hash(
                {
                    "issuer": AR_OWNER,
                    "kind": "runtime_control",
                    "subject_ref": operation_ref,
                    "action": action,
                    "target": target,
                    "source_stage": source_stage,
                    "affected_question_refs_hash": question_scope_hash,
                    "affected_runs_hash": affected_hash,
                    "safe_points_hash": safe_points_hash,
                }
            )
            connection.execute(
                text(
                    "INSERT INTO ar_control_operations (operation_ref, "
                    "idempotency_key, action, quest_ref, cycle_ref, epoch, "
                    "target_scope, run_ref, source_stage, "
                    "affected_question_refs_json, affected_question_refs_hash, "
                    "command_hash, expected_revision, affected_runs_json, "
                    "affected_runs_hash, safe_points_json, safe_points_hash, "
                    "receipt_ref, receipt_hash, created_at) VALUES "
                    "(:operation_ref, :idempotency_key, :action, :quest_ref, "
                    ":cycle_ref, :epoch, :target_scope, :run_ref, :source_stage, "
                    ":affected_question_refs_json, :affected_question_refs_hash, "
                    ":command_hash, :expected_revision, "
                    ":affected_json, :affected_hash, :safe_points_json, "
                    ":safe_points_hash, :receipt_ref, :receipt_hash, :now)"
                ),
                {
                    "operation_ref": operation_ref,
                    "idempotency_key": idempotency_key,
                    "action": action,
                    "quest_ref": target["quest_ref"],
                    "cycle_ref": target["cycle_ref"],
                    "epoch": target["epoch"],
                    "target_scope": target["target_scope"],
                    "run_ref": target.get("run_ref"),
                    "source_stage": source_stage,
                    "affected_question_refs_json": canonical_json(question_scope),
                    "affected_question_refs_hash": question_scope_hash,
                    "command_hash": command_hash,
                    "expected_revision": expected_revision,
                    "affected_json": canonical_json(affected_runs),
                    "affected_hash": affected_hash,
                    "safe_points_json": canonical_json(safe_points),
                    "safe_points_hash": safe_points_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_control_reservations SET status = 'applied', "
                    "updated_at = :now WHERE operation_ref = :operation_ref AND "
                    "status = 'prepared'"
                ),
                {"now": now, "operation_ref": operation_ref},
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "control_operation_count = control_operation_count + 1, "
                    "safe_point_count = safe_point_count + :safe_count, "
                    "fenced_attempt_count = fenced_attempt_count + :fenced_count, "
                    "active_run_count = CASE WHEN active_run_count >= "
                    ":terminated_count THEN active_run_count - :terminated_count ELSE "
                    "0 END "
                    "WHERE singleton = 'owner'"
                ),
                {
                    "safe_count": len(safe_points),
                    "fenced_count": fenced_count,
                    "terminated_count": terminated_count,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.managed_runs_controlled",
                {
                    "operation_ref": operation_ref,
                    "action": action,
                    "affected_run_refs": [item["run_ref"] for item in affected_runs],
                    "safe_point_refs": [
                        item["safe_point_ref"] for item in safe_points
                    ],
                },
            )
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).one()
        return _runtime_control_receipt(row)

    def _retire_quiesced_provider_attempt(self, connection, row, now):
        """Leave provider-backed Runs claimable after a graceful pause.

        A safe-boundary acknowledgement means the worker that owned the current
        technical Attempt has stopped.  Stage workers can continue their durable
        Attempt, but queue-backed DeepFetch/Experiment workers need an admitted
        successor before resume; otherwise their canonical queue row remains
        forever busy even though the managed control says ``running``.
        """

        if row.run_kind == "deepfetch":
            status = connection.execute(
                text("SELECT status FROM ar_deepfetch_runs WHERE run_ref = :run_ref"),
                {"run_ref": row.run_ref},
            ).scalar_one_or_none()
        elif row.run_kind == "experiment":
            status = connection.execute(
                text("SELECT status FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                {"run_ref": row.run_ref},
            ).scalar_one_or_none()
        else:
            return row
        if status != "running":
            return row
        return self._replace_fenced_managed_attempt(connection, row, now)

    def _wait_for_provider_quiescence(self, run_refs: tuple[str, ...]) -> None:
        if not run_refs:
            return
        deadline = time.monotonic() + _CONTROL_QUIESCENCE_WAIT_SECONDS
        next_dispatch_by_unit: dict[str, float] = {}
        while True:
            with self._database.read() as connection:
                active = connection.execute(
                    text(
                        "SELECT * FROM ar_provider_units WHERE status IN "
                        "('active', 'revocation_pending') AND run_ref IN ("
                        + ", ".join(
                            f":run_ref_{index}" for index in range(len(run_refs))
                        )
                        + ") ORDER BY started_at, unit_ref"
                    ),
                    {
                        f"run_ref_{index}": run_ref
                        for index, run_ref in enumerate(run_refs)
                    },
                ).all()
            if not active:
                return
            now = time.monotonic()
            dispatch = tuple(
                row
                for row in active
                if next_dispatch_by_unit.get(str(row.unit_ref), 0.0) <= now
            )
            if dispatch:
                self._request_provider_quiescence(dispatch)
                for row in dispatch:
                    next_dispatch_by_unit[str(row.unit_ref)] = (
                        now + _CONTROL_QUIESCENCE_REDISPATCH_SECONDS
                    )
            if time.monotonic() >= deadline:
                raise OwnerConflict("runtime_quiescence_pending")
            threading.Event().wait(_CONTROL_QUIESCENCE_POLL_SECONDS)

    def _request_provider_quiescence(self, units: tuple[object, ...]) -> None:
        with self._provider_quiescence_lock:
            drivers = dict(self._provider_quiescence_drivers)
        for unit in units:
            provider = drivers.get(str(unit.unit_kind))
            if provider is None:
                continue
            cancel_job = getattr(provider, "cancel_job", None)
            if callable(cancel_job):
                try:
                    cancel_job(str(unit.operation_ref))
                except Exception:
                    pass
            reconcile = getattr(provider, "reconcile_cancelled_job", None)
            physically_safe = False
            if callable(reconcile):
                try:
                    physically_safe = reconcile(str(unit.operation_ref)) is True
                except Exception:
                    physically_safe = False
            if physically_safe:
                self.acknowledge_provider_safe_point(
                    unit_ref=str(unit.unit_ref),
                    run_ref=str(unit.run_ref),
                    attempt_ref=(
                        None if unit.attempt_ref is None else str(unit.attempt_ref)
                    ),
                    fence_ref=None if unit.fence_ref is None else str(unit.fence_ref),
                )

    def _replace_fenced_managed_attempt(
        self, connection, control, now, *, reuse_checkpoint: bool = True
    ):
        if control.run_kind == "deepfetch":
            run = connection.execute(
                text("SELECT * FROM ar_deepfetch_runs WHERE run_ref = :run_ref"),
                {"run_ref": control.run_ref},
            ).first()
            if run is None or run.status in {"executed", "cancelled"}:
                raise OwnerConflict("terminal_run_cannot_reopen")
            if run.current_attempt_ref is not None:
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_attempts SET status = 'superseded', "
                        "failure_code = 'runtime_control_reconciliation', "
                        "completed_at = :now WHERE attempt_ref = :attempt_ref AND "
                        "status = 'running'"
                    ),
                    {"now": now, "attempt_ref": run.current_attempt_ref},
                )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'admitted', "
                    "current_attempt_ref = NULL, failure_code = NULL, "
                    "provider_operation_retry_permitted = 0, next_reconcile_at = "
                    "NULL, completed_at = NULL, updated_at = :now WHERE run_ref = "
                    ":run_ref"
                ),
                {"now": now, "run_ref": run.run_ref},
            )
            return SimpleNamespace(
                **{
                    **dict(control._mapping),
                    "attempt_ref": None,
                    "fence_ref": None,
                }
            )
        if control.run_kind == "experiment":
            run = connection.execute(
                text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                {"run_ref": control.run_ref},
            ).first()
            if run is None or run.status in {"executed", "failed"}:
                raise OwnerConflict("terminal_run_cannot_reopen")
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'retired', "
                    "retired_reason = 'runtime_control_reconciliation', "
                    "completed_at = :now WHERE attempt_ref = :attempt_ref AND "
                    "status IN ('admitted', 'running')"
                ),
                {"now": now, "attempt_ref": run.attempt_ref},
            )
            attempt_ref, fence_ref = _insert_replacement_attempt(
                connection,
                run.run_ref,
                int(run.attempt_generation) + 1,
                run.root_session_ref,
                now,
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'admitted', "
                    "attempt_ref = :attempt_ref, attempt_generation = "
                    "attempt_generation + 1, fence_ref = :fence_ref, "
                    "provider_operation_retry_permitted = 0, failure_code = NULL, "
                    "completed_at = NULL, updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                    "run_ref": run.run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET experiment_attempt_count = "
                    "experiment_attempt_count + 1 WHERE singleton = 'owner'"
                )
            )
            return SimpleNamespace(
                **{
                    **dict(control._mapping),
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                }
            )
        if not _is_formal_stage_run_kind(control.run_kind):
            raise OwnerConflict("runtime_reconciliation_required")
        run = connection.execute(
            text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
            {"run_ref": control.run_ref},
        ).first()
        if run is None or run.status == "completed":
            raise OwnerConflict("terminal_run_cannot_reopen")
        if control.run_kind != f"{run.stage}_stage" or run.stage not in FORMAL_STAGES:
            raise OwnerConflict("runtime_reconciliation_required")
        old_attempt = connection.execute(
            text(
                "SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref"
            ),
            {"attempt_ref": control.attempt_ref},
        ).first()
        if old_attempt is None:
            raise OwnerConflict("runtime_attempt_missing")
        if old_attempt.status not in {"running", "executed"}:
            raise OwnerConflict("runtime_reconciliation_required")
        old_invocations = connection.execute(
            text(
                "SELECT phase, operation_ref FROM "
                "ar_stage_provider_invocations WHERE attempt_ref = :attempt_ref"
            ),
            {"attempt_ref": old_attempt.attempt_ref},
        ).all()
        operation_refs = None
        if reuse_checkpoint:
            operation_refs = {
                row.phase: row.operation_ref for row in old_invocations
            }
            if (
                set(operation_refs) != {"primary", "review"}
                or any(
                    not isinstance(value, str) or not value
                    for value in operation_refs.values()
                )
            ):
                raise OwnerConflict("idea_provider_invocation_invalid")
        generation = int(old_attempt.generation) + 1
        attempt_ref = new_ref(f"{run.stage}_attempt")
        fence_ref = new_ref(f"{run.stage}_fence")
        connection.execute(
            text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, "
                "root_session_ref, fence_ref, predecessor_attempt_ref, status, "
                "primary_draft_json, primary_draft_hash, primary_adapter_kind, "
                "primary_recorded_at, created_at) VALUES (:attempt_ref, :run_ref, "
                ":generation, :session_ref, :fence_ref, NULL, 'running', "
                ":primary_draft_json, :primary_draft_hash, :primary_adapter_kind, "
                ":primary_recorded_at, :now)"
            ),
            {
                "attempt_ref": attempt_ref,
                "run_ref": run.run_ref,
                "generation": generation,
                "session_ref": run.root_session_ref,
                "fence_ref": fence_ref,
                "primary_draft_json": (
                    old_attempt.primary_draft_json if reuse_checkpoint else None
                ),
                "primary_draft_hash": (
                    old_attempt.primary_draft_hash if reuse_checkpoint else None
                ),
                "primary_adapter_kind": (
                    old_attempt.primary_adapter_kind if reuse_checkpoint else None
                ),
                "primary_recorded_at": (
                    old_attempt.primary_recorded_at if reuse_checkpoint else None
                ),
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, "
                "generation, status, issued_at) VALUES (:fence_ref, :run_ref, "
                ":attempt_ref, :generation, 'current', :now)"
            ),
            {
                "fence_ref": fence_ref,
                "run_ref": run.run_ref,
                "attempt_ref": attempt_ref,
                "generation": generation,
                "now": now,
            },
        )
        _insert_provider_invocations(
            connection,
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            attempt_ref=attempt_ref,
            generation=generation,
            root_session_ref=run.root_session_ref,
            fence_ref=fence_ref,
            context_pack_ref=run.context_pack_ref,
            context_pack_hash=run.context_pack_hash,
            runtime_binding_hash=run.runtime_binding_hash,
            predecessor_attempt_ref=None,
            prepared_at=now,
            stage=run.stage,
            operation_refs=operation_refs,
        )
        if reuse_checkpoint and old_attempt.primary_draft_json is not None:
            old_primary = connection.execute(
                text(
                    "SELECT response_hash FROM ar_stage_provider_invocations WHERE "
                    "attempt_ref = :attempt_ref AND phase = 'primary' AND status = "
                    "'completed'"
                ),
                {"attempt_ref": old_attempt.attempt_ref},
            ).first()
            if old_primary is None:
                raise OwnerConflict("idea_provider_invocation_invalid")
            connection.execute(
                text(
                    "UPDATE ar_stage_provider_invocations SET status = 'completed', "
                    "response_hash = :response_hash, completed_at = :completed_at "
                    "WHERE attempt_ref = :attempt_ref AND phase = 'primary' AND "
                    "status = 'prepared'"
                ),
                {
                    "response_hash": old_primary.response_hash,
                    "completed_at": old_attempt.primary_recorded_at,
                    "attempt_ref": attempt_ref,
                },
            )
        if not reuse_checkpoint:
            # The logical root Session survives a foreground rebind, but a native
            # provider session is input-specific.  Clearing only that attachment
            # keeps the Run/root identity while allowing the replacement Attempt
            # to establish a fresh, internally consistent provider lineage.
            connection.execute(
                text(
                    "UPDATE ar_stage_sessions SET native_session_ref = NULL, "
                    "status = 'active', updated_at = :now WHERE session_ref = "
                    ":session_ref"
                ),
                {"now": now, "session_ref": run.root_session_ref},
            )
        connection.execute(
            text(
                "INSERT INTO ar_stage_attempt_replacements "
                "(replacement_attempt_ref, run_ref, retired_attempt_ref, "
                "reason_code, created_at) VALUES (:replacement, :run_ref, :retired, "
                "'runtime_fence_replaced', :now)"
            ),
            {
                "replacement": attempt_ref,
                "run_ref": run.run_ref,
                "retired": old_attempt.attempt_ref,
                "now": now,
            },
        )
        connection.execute(
            text(
                "UPDATE ar_stage_runs SET status = 'running', current_attempt_ref = "
                ":attempt_ref, current_fence_ref = :fence_ref, updated_at = :now "
                "WHERE run_ref = :run_ref"
            ),
            {
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "now": now,
                "run_ref": run.run_ref,
            },
        )
        connection.execute(
            text(
                "UPDATE agent_runtime_state SET attempt_count = attempt_count + 1 "
                "WHERE singleton = 'owner'"
            )
        )
        return SimpleNamespace(
            **{
                **dict(control._mapping),
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
            }
        )

    def query_safe_meaningful_runnable(
        self, quest_ref: str, blocked_waiters: tuple[dict[str, object], ...]
    ) -> tuple[dict[str, object], ...]:
        """Return exact current AR work for this Quest that no waiter blocks."""

        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 64:
            raise OwnerConflict("quest_ref_invalid")
        if (
            not isinstance(blocked_waiters, tuple)
            or len(blocked_waiters) > 256
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("waiter_ref"), str)
                or not item["waiter_ref"]
                or len(cast(str, item["waiter_ref"])) > 128
                or not isinstance(item.get("target_assertion"), dict)
                for item in blocked_waiters
            )
        ):
            raise OwnerConflict("human_request_waiter_ref_invalid")
        excluded = _blocked_runtime_work_aliases(blocked_waiters)
        with self._database.read() as connection:
            owner_revision = int(
                connection.execute(
                    text(
                        "SELECT revision FROM agent_runtime_state WHERE singleton = "
                        "'owner'"
                    )
                ).scalar_one()
            )
            stage_rows = connection.execute(
                text(
                    "SELECT runs.run_ref, runs.request_ref, runs.status FROM "
                    "ar_stage_runs AS runs JOIN ae_stage_run_requests AS requests "
                    "ON requests.request_ref = runs.request_ref WHERE "
                    "requests.quest_ref = :quest_ref AND runs.status IN "
                    "('running', 'awaiting_acceptance') ORDER BY runs.created_at, "
                    "runs.run_ref"
                ),
                {"quest_ref": quest_ref},
            ).all()
            acquisition_rows = connection.execute(
                text(
                    "SELECT requests.request_id, requests.status FROM "
                    "ar_acquisition_requests AS requests JOIN "
                    "ar_acquisition_sessions AS sessions ON sessions.session_ref = "
                    "requests.session_ref WHERE sessions.quest_ref = :quest_ref AND "
                    "requests.status = 'running' ORDER BY requests.created_at, "
                    "requests.request_id"
                ),
                {"quest_ref": quest_ref},
            ).all()
        basis: list[dict[str, object]] = []
        for row in stage_rows:
            aliases = {
                str(row.run_ref),
                str(row.request_ref),
                f"stage_run:{row.run_ref}",
                f"stage_request:{row.request_ref}",
            }
            if aliases & excluded:
                continue
            basis.append(
                {
                    "owner": AR_OWNER,
                    "owner_revision": owner_revision,
                    "quest_ref": quest_ref,
                    "work_kind": "stage_run",
                    "work_ref": str(row.run_ref),
                    "status": str(row.status),
                }
            )
        for row in acquisition_rows:
            aliases = {
                str(row.request_id),
                f"acquisition_request:{row.request_id}",
            }
            if aliases & excluded:
                continue
            basis.append(
                {
                    "owner": AR_OWNER,
                    "owner_revision": owner_revision,
                    "quest_ref": quest_ref,
                    "work_kind": "acquisition_request",
                    "work_ref": str(row.request_id),
                    "status": str(row.status),
                }
            )
        return tuple(basis)

    def observe_host_compute(self, idempotency_key: str) -> HostComputeObservation:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("idempotency_key_invalid")
        request_hash = canonical_hash(
            {"command": "observe_host_compute", "schema": "v1"}
        )
        while True:
            replay, claim_token = self._claim_or_replay(idempotency_key, request_hash)
            if replay is not None:
                return replay
            if claim_token is None:
                time.sleep(_HOST_COMPUTE_CLAIM_POLL_SECONDS)
                continue

            try:
                snapshot = self._host_compute_probe.observe()
                _validate_probe_snapshot(snapshot)
                capabilities = {
                    "devices": [device.as_dict() for device in snapshot.devices]
                }
                capabilities_hash = canonical_hash(capabilities)
                observation = self._complete_claim(
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    claim_token=claim_token,
                    snapshot=snapshot,
                    capabilities=capabilities,
                    capabilities_hash=capabilities_hash,
                )
            except BaseException:
                self._release_claim(idempotency_key, claim_token)
                raise
            if observation is not None:
                return observation

    def _claim_or_replay(
        self, idempotency_key: str, request_hash: str
    ) -> tuple[HostComputeObservation | None, str | None]:
        now = time.time()
        with self._database.read() as connection:
            stage_command = connection.execute(
                text(
                    "SELECT 1 FROM ar_stage_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        if stage_command is not None:
            raise OwnerConflict("idempotency_conflict")
        if replay is not None:
            if replay.request_hash != request_hash or (
                claim is not None and claim.request_hash != request_hash
            ):
                raise OwnerConflict("idempotency_conflict")
            return _observation_from_row(replay), None
        if claim is not None:
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")
            if float(claim.lease_expires_at) > now:
                return None, None

        claim_token = new_ref("host_claim")
        with self._database.write() as connection:
            # Serialize only the durable claim transition across daemon processes.
            # The external probe itself runs after this transaction commits.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            claim_now = time.time()
            claim_deadline = claim_now + _HOST_COMPUTE_CLAIM_LEASE_SECONDS
            stage_command = connection.execute(
                text(
                    "SELECT 1 FROM ar_stage_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if stage_command is not None:
                raise OwnerConflict("idempotency_conflict")
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay), None

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is not None:
                if claim.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                if float(claim.lease_expires_at) > claim_now:
                    return None, None
                connection.execute(
                    text(
                        "UPDATE ar_host_compute_observation_claims SET "
                        "claim_token = :claim_token, "
                        "attempt_count = attempt_count + 1, "
                        "claimed_at = :claimed_at, "
                        "lease_expires_at = :lease_expires_at "
                        "WHERE idempotency_key = :idempotency_key"
                    ),
                    {
                        "claim_token": claim_token,
                        "claimed_at": claim_now,
                        "lease_expires_at": claim_deadline,
                        "idempotency_key": idempotency_key,
                    },
                )
                return None, claim_token

            connection.execute(
                text(
                    "INSERT INTO ar_host_compute_observation_claims "
                    "(idempotency_key, request_hash, claim_token, attempt_count, "
                    "claimed_at, lease_expires_at) VALUES "
                    "(:idempotency_key, :request_hash, :claim_token, 1, "
                    ":claimed_at, :lease_expires_at)"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "claim_token": claim_token,
                    "claimed_at": claim_now,
                    "lease_expires_at": claim_deadline,
                },
            )
        return None, claim_token

    def _complete_claim(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        claim_token: str,
        snapshot: HostComputeSnapshot,
        capabilities: dict[str, object],
        capabilities_hash: str,
    ) -> HostComputeObservation | None:
        snapshot_ref = new_ref("host_snapshot")
        with self._database.write() as connection:
            # Avoid a cross-process SQLite read-to-write upgrade race between
            # unrelated observations that finish probing at the same time.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision "
                    "WHERE singleton = 'owner'"
                )
            )
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _observation_from_row(replay)

            claim = connection.execute(
                text(
                    "SELECT * FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if claim is None or claim.claim_token != claim_token:
                return None
            if claim.request_hash != request_hash:
                raise OwnerConflict("idempotency_conflict")

            connection.execute(
                text(
                    "INSERT INTO ar_host_capability_snapshots "
                    "(snapshot_ref, idempotency_key, request_hash, adapter_kind, "
                    "status, capabilities_json, capabilities_hash, reason_code, "
                    "observed_at) VALUES (:snapshot_ref, :idempotency_key, "
                    ":request_hash, :adapter_kind, :status, :capabilities_json, "
                    ":capabilities_hash, :reason_code, :observed_at)"
                ),
                {
                    "snapshot_ref": snapshot_ref,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "adapter_kind": snapshot.adapter_kind,
                    "status": snapshot.status,
                    "capabilities_json": canonical_json(capabilities),
                    "capabilities_hash": capabilities_hash,
                    "reason_code": snapshot.reason_code,
                    "observed_at": snapshot.observed_at,
                },
            )
            connection.execute(
                text(
                    "DELETE FROM ar_host_compute_observation_claims WHERE "
                    "idempotency_key = :idempotency_key AND claim_token = :claim_token"
                ),
                {
                    "idempotency_key": idempotency_key,
                    "claim_token": claim_token,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 WHERE "
                    "singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.host_compute_observed",
                {
                    "snapshot_ref": snapshot_ref,
                    "status": snapshot.status,
                    "capabilities_hash": capabilities_hash,
                },
            )
        return HostComputeObservation(
            snapshot_ref=snapshot_ref,
            status=snapshot.status,
            observed_at=snapshot.observed_at,
            devices=snapshot.devices,
            adapter_kind=snapshot.adapter_kind,
            capabilities_hash=capabilities_hash,
            reason_code=snapshot.reason_code,
        )

    def _release_claim(self, idempotency_key: str, claim_token: str) -> None:
        try:
            with self._database.write() as connection:
                connection.execute(
                    text(
                        "DELETE FROM ar_host_compute_observation_claims WHERE "
                        "idempotency_key = :idempotency_key "
                        "AND claim_token = :claim_token"
                    ),
                    {
                        "idempotency_key": idempotency_key,
                        "claim_token": claim_token,
                    },
                )
        except Exception:
            # A persisted lease is the recovery path when immediate cleanup cannot
            # acquire the database after a probe/finalization failure.
            pass

    def query_host_compute(self, snapshot_ref: str) -> HostComputeObservation:
        return self._host_compute_reader.query_host_compute(snapshot_ref)

    def reconcile_host_compute(
        self, idempotency_key: str
    ) -> HostComputeObservation | None:
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("idempotency_key_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_host_capability_snapshots WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
        return None if row is None else _observation_from_row(row)

    def _recover_interrupted_acquisition(self) -> None:
        """Release daemon-local slots without replaying an unknown download."""

        now = time.time()
        with self._database.write() as connection:
            sessions = connection.execute(
                text(
                    "UPDATE ar_acquisition_sessions SET status = 'waiting_user', "
                    "slot_held = 0, reason_code = "
                    "'acquisition_reconciliation_required', updated_at = :now "
                    "WHERE status IN ('probing', 'acquiring')"
                ),
                {"now": now},
            )
            requests = connection.execute(
                text(
                    "UPDATE ar_acquisition_requests SET status = 'waiting_user', "
                    "results_json = COALESCE(results_json, :results_json), "
                    "results_hash = COALESCE(results_hash, :results_hash), "
                    "updated_at = :now, completed_at = :now WHERE status = 'running'"
                ),
                {
                    "results_json": canonical_json(
                        [
                            {
                                "paper_id": "__batch__",
                                "status": "waiting_user",
                                "path": None,
                                "format": None,
                                "failure": {
                                    "code": "acquisition_reconciliation_required",
                                    "detail": (
                                        "daemon 重启后必须用原 request_id 对账，"
                                        "不得启动新的下载副作用。"
                                    ),
                                },
                            }
                        ]
                    ),
                    "results_hash": canonical_hash(
                        [
                            {
                                "paper_id": "__batch__",
                                "status": "waiting_user",
                                "path": None,
                                "format": None,
                                "failure": {
                                    "code": "acquisition_reconciliation_required",
                                    "detail": (
                                        "daemon 重启后必须用原 request_id 对账，"
                                        "不得启动新的下载副作用。"
                                    ),
                                },
                            }
                        ]
                    ),
                    "now": now,
                },
            )
            recovered = (sessions.rowcount or 0) + (requests.rowcount or 0)
            if recovered:
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "acquisition_active_slot_count = 0 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.acquisition_recovered",
                    {"recovered_record_count": recovered},
                )

    def prepare_acquisition_session(
        self,
        *,
        initialization_id: str,
        draft_revision: int,
        config: dict[str, object],
        provider: AcquisitionProvider,
    ) -> AcquisitionSession:
        if (
            not initialization_id
            or draft_revision < 1
            or set(config) != {"mode", "library_entry_url"}
            or config.get("mode")
            not in {"oa_then_institution", "oa_only", "provided_only"}
            or not isinstance(config.get("library_entry_url"), str)
        ):
            raise OwnerConflict("acquisition_preflight_request_invalid")
        mode = str(config["mode"])
        library_entry_url = str(config["library_entry_url"])
        normalized_config = {
            "schema_ref": "meta-research/acquisition-session-config/v1",
            "mode": mode,
            "library_entry_url": library_entry_url,
        }
        config_json = canonical_json(normalized_config)
        config_hash = canonical_hash(normalized_config)
        try:
            runtime_binding = provider.runtime_binding()
            runtime_binding_hash = validate_acquisition_runtime_binding(runtime_binding)
        except AcquisitionUnavailable as error:
            raise OwnerConflict(error.code) from error
        runtime_binding_json = canonical_json(runtime_binding.as_dict())
        existing_session = self.query_acquisition_session(
            initialization_id=initialization_id
        )
        if (
            existing_session is not None
            and existing_session.config_hash == config_hash
            and existing_session.runtime_binding_hash == runtime_binding_hash
            and existing_session.status == "ready"
        ):
            self._reconcile_acquisition_human_requests(existing_session.session_ref)
        now = time.time()
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_acquisition_sessions WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if row is not None and (
                row.config_hash == config_hash
                and row.runtime_binding_hash == runtime_binding_hash
                and row.status == "ready"
            ):
                return _acquisition_session_from_row(row)
            if row is None:
                session_ref = new_ref("acquisition_session")
                generation = 1
                previous_browser_context_ref = None
                connection.execute(
                    text(
                        "INSERT INTO ar_acquisition_sessions (session_ref, "
                        "initialization_id, config_json, config_hash, mode, "
                        "runtime_binding_json, runtime_binding_hash, status, "
                        "preflight_generation, slot_held, created_at, updated_at) "
                        "VALUES (:session_ref, :initialization_id, :config_json, "
                        ":config_hash, :mode, :runtime_binding_json, "
                        ":runtime_binding_hash, 'probing', :generation, 1, :now, :now)"
                    ),
                    {
                        "session_ref": session_ref,
                        "initialization_id": initialization_id,
                        "config_json": config_json,
                        "config_hash": config_hash,
                        "mode": mode,
                        "runtime_binding_json": runtime_binding_json,
                        "runtime_binding_hash": runtime_binding_hash,
                        "generation": generation,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "acquisition_session_count = acquisition_session_count + 1, "
                        "acquisition_active_slot_count = "
                        "acquisition_active_slot_count + 1 WHERE singleton = 'owner'"
                    )
                )
            else:
                if row.status in {"probing", "acquiring"}:
                    raise OwnerConflict("acquisition_session_busy")
                if row.status == "cancelled":
                    raise OwnerConflict("acquisition_session_cancelled")
                session_ref = str(row.session_ref)
                generation = int(row.preflight_generation) + 1
                previous_browser_context_ref = (
                    None
                    if row.browser_context_ref is None
                    else str(row.browser_context_ref)
                )
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_sessions SET config_json = "
                        ":config_json, config_hash = :config_hash, mode = :mode, "
                        "runtime_binding_json = :runtime_binding_json, "
                        "runtime_binding_hash = :runtime_binding_hash, status = "
                        "'probing', preflight_generation = :generation, "
                        "slot_held = 1, reason_code = NULL, "
                        "evidence_json = NULL, evidence_hash = NULL, updated_at = :now "
                        "WHERE session_ref = :session_ref"
                    ),
                    {
                        "session_ref": session_ref,
                        "config_json": config_json,
                        "config_hash": config_hash,
                        "mode": mode,
                        "runtime_binding_json": runtime_binding_json,
                        "runtime_binding_hash": runtime_binding_hash,
                        "generation": generation,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "acquisition_active_slot_count = "
                        "acquisition_active_slot_count + 1 WHERE singleton = 'owner'"
                    )
                )
            self._feed.record(
                connection,
                "agent_runtime.acquisition_preflight_started",
                {
                    "session_ref": session_ref,
                    "initialization_id": initialization_id,
                    "preflight_generation": generation,
                    "config_hash": config_hash,
                },
            )

        private_state = self._acquisition_private_root / session_ref
        private_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        provider_request = AcquisitionPreflightRequest(
            session_ref=session_ref,
            initialization_id=initialization_id,
            draft_revision=draft_revision,
            config_hash=config_hash,
            mode=mode,  # type: ignore[arg-type]
            library_entry_url=library_entry_url,
            private_state_dir=str(private_state),
            previous_browser_context_ref=previous_browser_context_ref,
        )
        try:
            result = validate_preflight_result(
                provider_request, provider.preflight(provider_request)
            )
        except AcquisitionUnavailable as error:
            result = None
            failure_code = error.code
        except Exception:
            result = None
            failure_code = "acquisition_preflight_provider_error"

        evidence = {} if result is None else result.evidence
        evidence_json = canonical_json(evidence)
        evidence_hash = canonical_hash(evidence)
        final_status = "unavailable" if result is None else result.status
        reason_code = failure_code if result is None else result.reason_code
        browser_context_ref = (
            previous_browser_context_ref
            if result is None
            else result.browser_context_ref
        )
        completed_at = time.time()
        with self._database.write() as connection:
            current = connection.execute(
                text(
                    "SELECT status, preflight_generation, config_hash FROM "
                    "ar_acquisition_sessions WHERE session_ref = :session_ref"
                ),
                {"session_ref": session_ref},
            ).one()
            if (
                current.status != "probing"
                or int(current.preflight_generation) != generation
                or current.config_hash != config_hash
            ):
                raise OwnerConflict("acquisition_preflight_fence_stale")
            connection.execute(
                text(
                    "UPDATE ar_acquisition_sessions SET status = :status, "
                    "browser_context_ref = :browser_context_ref, slot_held = 0, "
                    "reason_code = :reason_code, evidence_json = :evidence_json, "
                    "evidence_hash = :evidence_hash, updated_at = :now, "
                    "last_ready_at = CASE WHEN :status = 'ready' THEN :now ELSE "
                    "last_ready_at END WHERE session_ref = :session_ref"
                ),
                {
                    "session_ref": session_ref,
                    "status": final_status,
                    "browser_context_ref": browser_context_ref,
                    "reason_code": reason_code,
                    "evidence_json": evidence_json,
                    "evidence_hash": evidence_hash,
                    "now": completed_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "acquisition_active_slot_count = "
                    "acquisition_active_slot_count - 1 WHERE singleton = 'owner' "
                    "AND acquisition_active_slot_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.acquisition_preflight_completed",
                {
                    "session_ref": session_ref,
                    "status": final_status,
                    "reason_code": reason_code,
                },
            )
        session = self.query_acquisition_session(session_ref=session_ref)
        assert session is not None
        self._reconcile_acquisition_human_requests(session_ref)
        return session

    def query_acquisition_session(
        self,
        *,
        initialization_id: str | None = None,
        session_ref: str | None = None,
        quest_ref: str | None = None,
    ) -> AcquisitionSession | None:
        selectors = [
            value is not None for value in (initialization_id, session_ref, quest_ref)
        ]
        if sum(selectors) != 1:
            raise OwnerConflict("acquisition_session_query_invalid")
        if initialization_id is not None:
            clause = "initialization_id = :value"
            value = initialization_id
        elif session_ref is not None:
            clause = "session_ref = :value"
            value = session_ref
        else:
            clause = "quest_ref = :value"
            value = quest_ref
        with self._database.read() as connection:
            row = connection.execute(
                text(f"SELECT * FROM ar_acquisition_sessions WHERE {clause}"),
                {"value": value},
            ).first()
        return None if row is None else _acquisition_session_from_row(row)

    def _current_acquisition_human_target(
        self,
        target: dict[str, object],
        *,
        require_reconnected_preflight: bool,
    ):
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT requests.*, sessions.config_hash AS session_config_hash, "
                    "sessions.status AS session_status, sessions.slot_held AS "
                    "session_slot_held, sessions.current_request_id AS "
                    "session_current_request_id, sessions.preflight_generation AS "
                    "session_preflight_generation FROM ar_acquisition_requests AS "
                    "requests JOIN ar_acquisition_sessions AS sessions ON "
                    "sessions.session_ref = requests.session_ref WHERE "
                    "requests.request_id = :request_id AND requests.session_ref = "
                    ":session_ref"
                ),
                {
                    "request_id": target["acquisition_request_id"],
                    "session_ref": target["session_ref"],
                },
            ).first()
        if (
            row is None
            or row.request_hash != target["acquisition_request_hash"]
            or int(row.attempt_count) < target["attempt_count"]
            or row.status != "waiting_user"
            or row.session_config_hash != target["config_hash"]
            or bool(row.session_slot_held)
            or row.session_current_request_id != target["acquisition_request_id"]
            or not _acquisition_target_matches_waiting_item(target, row)
            or (
                require_reconnected_preflight
                and (
                    row.session_status != "ready"
                    or int(row.session_preflight_generation)
                    <= target["blocked_preflight_generation"]
                )
            )
            or (
                not require_reconnected_preflight
                and row.session_status not in {"ready", "waiting_user"}
            )
        ):
            return None
        return row

    def _accepted_material_response_binding(
        self,
        response: dict[str, object],
        acquisition_row,
        *,
        expected_paper_id: str,
    ) -> dict[str, object] | None:
        facts = response.get("facts")
        resolver = self._research_material_resolver
        if not isinstance(facts, dict) or resolver is None:
            return None
        required = {
            "acquisition_paper_id",
            "material_source_ref",
            "material_version_ref",
            "material_content_hash",
            "material_manifest_hash",
            "material_acceptance_receipt_ref",
        }
        if not required.issubset(facts) or any(
            not isinstance(facts[name], str) or not facts[name] for name in required
        ):
            return None
        if facts["acquisition_paper_id"] != expected_paper_id:
            return None
        version_ref = cast(str, facts["material_version_ref"])
        if facts["material_source_ref"] != version_ref:
            return None
        asset = resolver.query_asset_version(version_ref)
        receipt = None if asset is None else getattr(asset, "receipt", None)
        if (
            asset is None
            or getattr(asset, "memory_ref", None) != version_ref
            or getattr(asset, "version_ref", None) != version_ref
            or getattr(asset, "content_hash", None) != facts["material_content_hash"]
            or getattr(asset, "manifest_hash", None) != facts["material_manifest_hash"]
            or receipt is None
            or getattr(receipt, "issuer", None) != "research_memory"
            or getattr(receipt, "subject_ref", None) != version_ref
            or getattr(receipt, "receipt_ref", None)
            != facts["material_acceptance_receipt_ref"]
        ):
            return None
        media_format = _acquisition_format_for_material(
            str(getattr(asset, "media_type", "")),
            str(getattr(asset, "display_name", "")),
        )
        if media_format is None:
            return None
        results = _acquisition_results_from_json(
            acquisition_row.results_json, acquisition_row.results_hash
        )
        if not any(
            result.paper_id == expected_paper_id and result.status == "waiting_user"
            for result in results
        ):
            return None
        return {
            "route": "accepted_material",
            "response_ref": response["response_ref"],
            "paper_id": expected_paper_id,
            "version_ref": version_ref,
            "content_hash": facts["material_content_hash"],
            "manifest_hash": facts["material_manifest_hash"],
            "receipt_ref": facts["material_acceptance_receipt_ref"],
            "format": media_format,
        }

    def _satisfy_acquisition_human_request(
        self,
        request: dict[str, object],
        response: dict[str, object],
        *,
        reason_code: str,
        evidence_refs: tuple[str, ...],
    ) -> dict[str, object]:
        request_ref = cast(str, request["request_ref"])
        if request["status"] == "open":
            request = self.evaluate_human_request(
                request_ref,
                response_refs=(cast(str, response["response_ref"]),),
                decision="satisfied",
                reason_code=reason_code,
                accepted_evidence_refs=evidence_refs,
                idempotency_key="ar-hr-eval:"
                + canonical_hash(
                    {
                        "request_ref": request_ref,
                        "response_ref": response["response_ref"],
                        "reason_code": reason_code,
                        "evidence_refs": evidence_refs,
                    }
                ),
            )
        waiters = cast(list[dict[str, object]], request["direct_waiters"])
        if len(waiters) != 1:
            raise OwnerConflict("acquisition_human_request_invalid")
        waiter = waiters[0]
        if waiter.get("status") in {"released", "consumed"}:
            return request
        validation = self.validate_human_request_waiter(
            request_ref,
            waiter_ref=cast(str, waiter["waiter_ref"]),
            generation=cast(int, waiter["generation"]),
            target_assertion=cast(dict[str, object], waiter["target_assertion"]),
            other_blockers=(),
            idempotency_key="ar-hr-resume:"
            + canonical_hash(
                {
                    "request_ref": request_ref,
                    "waiter_ref": waiter["waiter_ref"],
                    "generation": waiter["generation"],
                }
            ),
        )
        if validation["status"] != "released":
            raise OwnerConflict("acquisition_human_request_not_released")
        current = self.query_human_request(request_ref)
        if current is None:
            raise OwnerConflict("acquisition_human_request_invalid")
        return current

    def _acquisition_resume_route(
        self, human_request: dict[str, object], acquisition_row
    ) -> dict[str, object]:
        evaluation = human_request.get("evaluation")
        responses = cast(list[dict[str, object]], human_request.get("responses", []))
        if (
            not isinstance(evaluation, dict)
            or evaluation.get("decision") != "satisfied"
        ):
            raise OwnerConflict("acquisition_human_request_invalid")
        response_refs = evaluation.get("response_refs")
        if not isinstance(response_refs, list) or len(response_refs) != 1:
            raise OwnerConflict("acquisition_human_request_invalid")
        selected = next(
            (
                response
                for response in responses
                if response.get("response_ref") == response_refs[0]
            ),
            None,
        )
        if selected is None:
            raise OwnerConflict("acquisition_human_request_invalid")
        facts = selected.get("facts")
        target = cast(dict[str, object], human_request["target_assertion"])
        item_binding = {
            "paper_id": target["acquisition_paper_id"],
            "item_hash": target["acquisition_item_hash"],
        }
        if not isinstance(facts, dict):
            raise OwnerConflict("acquisition_human_request_invalid")
        if facts.get("route") == "institutional_browser_reconnected":
            return {
                "route": "institutional_browser_reconnected",
                "response_ref": selected["response_ref"],
                **item_binding,
            }
        if facts.get("route") == "oa_only":
            return {
                "route": "oa_only",
                "response_ref": selected["response_ref"],
                **item_binding,
            }
        material = self._accepted_material_response_binding(
            selected,
            acquisition_row,
            expected_paper_id=cast(str, target["acquisition_paper_id"]),
        )
        if material is None:
            raise OwnerConflict("acquisition_material_binding_invalid")
        return {**material, "item_hash": target["acquisition_item_hash"]}

    def _record_acquisition_resume_route(
        self,
        connection,
        *,
        request_id: str,
        attempt_no: int,
        session_ref: str,
        request_hash: str,
        human_request_ref: str,
        evaluation_ref: str,
        route: dict[str, object],
        consumption: dict[str, object],
    ) -> None:
        effective_mode = _effective_acquisition_mode(route)
        route_json = canonical_json(route)
        route_hash = canonical_hash(route)
        paper_id = cast(str, route["paper_id"])
        item_hash = cast(str, route["item_hash"])
        consumption_receipt = cast(dict[str, object], consumption["receipt"])
        route_ref = new_ref("acquisition_resume_route")
        payload = {
            "schema_ref": "meta-research/acquisition-resume-route/v1",
            "route_ref": route_ref,
            "request_id": request_id,
            "attempt_no": attempt_no,
            "session_ref": session_ref,
            "request_hash": request_hash,
            "paper_id": paper_id,
            "item_hash": item_hash,
            "human_request_ref": human_request_ref,
            "response_ref": route["response_ref"],
            "evaluation_ref": evaluation_ref,
            "consumption_ref": consumption["consumption_ref"],
            "consumption_receipt_ref": consumption_receipt["receipt_ref"],
            "consumption_receipt_hash": consumption_receipt["payload_hash"],
            "effective_mode": effective_mode,
            "route_hash": route_hash,
        }
        receipt_hash = canonical_hash(payload)
        existing = connection.execute(
            text(
                "SELECT * FROM ar_acquisition_resume_routes WHERE request_id = "
                ":request_id AND attempt_no = :attempt_no"
            ),
            {"request_id": request_id, "attempt_no": attempt_no},
        ).first()
        if existing is not None:
            verified = self._verified_acquisition_resume_route_row(connection, existing)
            if verified != route:
                raise OwnerConflict("acquisition_resume_route_conflict")
            return
        connection.execute(
            text(
                "INSERT INTO ar_acquisition_resume_routes (route_ref, request_id, "
                "attempt_no, session_ref, request_hash, paper_id, item_hash, "
                "human_request_ref, response_ref, evaluation_ref, "
                "consumption_ref, consumption_receipt_ref, "
                "consumption_receipt_hash, effective_mode, route_json, route_hash, "
                "receipt_ref, receipt_hash, created_at) VALUES (:route_ref, "
                ":request_id, :attempt_no, :session_ref, :request_hash, :paper_id, "
                ":item_hash, :human_request_ref, :response_ref, "
                ":evaluation_ref, :consumption_ref, :consumption_receipt_ref, "
                ":consumption_receipt_hash, :effective_mode, :route_json, "
                ":route_hash, :receipt_ref, :receipt_hash, :created_at)"
            ),
            {
                **payload,
                "route_json": route_json,
                "receipt_ref": new_ref("owner_receipt"),
                "receipt_hash": receipt_hash,
                "created_at": time.time(),
            },
        )

    def _verified_acquisition_resume_route_row(
        self, connection, row
    ) -> dict[str, object]:
        try:
            route = decoded_object(row.route_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("acquisition_resume_route_invalid") from error
        consumption = connection.execute(
            text(
                "SELECT * FROM owner_human_request_resume_consumptions WHERE "
                "consumption_ref = :consumption_ref"
            ),
            {"consumption_ref": row.consumption_ref},
        ).first()
        payload = {
            "schema_ref": "meta-research/acquisition-resume-route/v1",
            "route_ref": row.route_ref,
            "request_id": row.request_id,
            "attempt_no": int(row.attempt_no),
            "session_ref": row.session_ref,
            "request_hash": row.request_hash,
            "paper_id": row.paper_id,
            "item_hash": row.item_hash,
            "human_request_ref": row.human_request_ref,
            "response_ref": row.response_ref,
            "evaluation_ref": row.evaluation_ref,
            "consumption_ref": row.consumption_ref,
            "consumption_receipt_ref": row.consumption_receipt_ref,
            "consumption_receipt_hash": row.consumption_receipt_hash,
            "effective_mode": row.effective_mode,
            "route_hash": row.route_hash,
        }
        if (
            canonical_json(route) != row.route_json
            or canonical_hash(route) != row.route_hash
            or not isinstance(route, dict)
            or route.get("response_ref") != row.response_ref
            or route.get("paper_id") != row.paper_id
            or route.get("item_hash") != row.item_hash
            or _effective_acquisition_mode(route) != row.effective_mode
            or canonical_hash(payload) != row.receipt_hash
            or consumption is None
            or consumption.request_ref != row.human_request_ref
            or consumption.work_ref
            != "acquisition_item:"
            + canonical_hash(
                {
                    "request_id": row.request_id,
                    "attempt_no": int(row.attempt_no),
                    "paper_id": row.paper_id,
                }
            )
            or consumption.receipt_ref != row.consumption_receipt_ref
            or consumption.receipt_hash != row.consumption_receipt_hash
        ):
            raise OwnerConflict("acquisition_resume_route_invalid")
        return route

    def _query_acquisition_resume_route(
        self, request_id: str, attempt_no: int, session_ref: str, request_hash: str
    ) -> dict[str, object] | None:
        with self._database.read() as connection:
            request_row = connection.execute(
                text(
                    "SELECT results_json, results_hash FROM "
                    "ar_acquisition_requests WHERE request_id = :request_id "
                    "AND session_ref = :session_ref AND request_hash = :request_hash"
                ),
                {
                    "request_id": request_id,
                    "session_ref": session_ref,
                    "request_hash": request_hash,
                },
            ).first()
            if request_row is None:
                raise OwnerConflict("acquisition_resume_route_invalid")
            reconciliation_ids = {
                result.paper_id
                for result in _acquisition_results_from_json(
                    request_row.results_json, request_row.results_hash
                )
                if result.status == "waiting_user"
                and result.failure is not None
                and result.failure.get("code") == "acquisition_reconciliation_required"
            }
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_acquisition_resume_routes WHERE request_id = "
                    ":request_id AND attempt_no <= :attempt_no ORDER BY attempt_no "
                    "DESC"
                ),
                {"request_id": request_id, "attempt_no": attempt_no},
            ).all()
            for row in rows:
                if row.session_ref != session_ref or row.request_hash != request_hash:
                    raise OwnerConflict("acquisition_resume_route_invalid")
                route = self._verified_acquisition_resume_route_row(connection, row)
                if row.paper_id in reconciliation_ids:
                    return route
            return None

    def _prepare_accepted_material_route(
        self,
        session_ref: str,
        request_id: str,
        route: dict[str, object],
    ) -> tuple[object, Path]:
        resolver = self._research_material_resolver
        if resolver is None:
            raise OwnerConflict("acquisition_material_resolver_unavailable")
        materialized = resolver.materialize_asset(cast(str, route["version_ref"]))
        content = getattr(materialized, "content", None)
        if (
            not isinstance(content, bytes)
            or hashlib.sha256(content).hexdigest() != route["content_hash"]
        ):
            raise OwnerConflict("acquisition_material_binding_invalid")
        target_dir = (
            self._acquisition_private_root / session_ref / "requests" / request_id
        )
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return materialized, _store_provided_material(target_dir, route, materialized)

    def reconcile_human_request(self, request_ref: str) -> dict[str, object] | None:
        request = self.query_human_request(request_ref)
        if (
            request is None
            or request.get("issuer") != AR_OWNER
            or request.get("kind") != "library_reconnect"
            or request.get("status") not in {"open", "satisfied"}
        ):
            return request
        target = request.get("target_assertion")
        if (
            not isinstance(target, dict)
            or target.get("schema_ref")
            != "meta-research/acquisition-human-request-target/v1"
            or target.get("operation") != "resume_acquisition_item"
            or not isinstance(target.get("session_ref"), str)
            or not isinstance(target.get("acquisition_request_id"), str)
            or not isinstance(target.get("acquisition_request_hash"), str)
            or not isinstance(target.get("acquisition_paper_id"), str)
            or not isinstance(target.get("acquisition_item_hash"), str)
            or not isinstance(target.get("config_hash"), str)
            or not isinstance(target.get("blocked_preflight_generation"), int)
            or not isinstance(target.get("attempt_count"), int)
        ):
            return request
        responses = cast(list[dict[str, object]], request["responses"])
        declined_refs = tuple(
            cast(str, response["response_ref"])
            for response in responses
            if response.get("decision") == "declined"
        )
        if request["status"] == "open" and declined_refs:
            return self.evaluate_human_request(
                request_ref,
                response_refs=declined_refs,
                decision="declined",
                reason_code="human_declined_institution_reconnect",
                accepted_evidence_refs=(),
                idempotency_key="ar-hr-decline:"
                + canonical_hash(
                    {"request_ref": request_ref, "responses": declined_refs}
                ),
            )
        deferred_refs = tuple(
            cast(str, response["response_ref"])
            for response in responses
            if response.get("decision") == "deferred"
        )
        if (
            request["status"] == "open"
            and deferred_refs
            and not any(
                response.get("decision") == "provided" for response in responses
            )
        ):
            request = self.evaluate_human_request(
                request_ref,
                response_refs=deferred_refs,
                decision="needs_input",
                reason_code="human_deferred_institution_reconnect",
                accepted_evidence_refs=(),
                idempotency_key="ar-hr-defer:"
                + canonical_hash(
                    {"request_ref": request_ref, "responses": deferred_refs}
                ),
            )
        provided = [
            response for response in responses if response.get("decision") == "provided"
        ]
        provided_refs = tuple(
            cast(str, response["response_ref"]) for response in provided
        )
        if not provided_refs:
            return request
        selected = provided[-1]
        facts = selected.get("facts")
        route = facts.get("route") if isinstance(facts, dict) else None
        if route == "institutional_browser_reconnected":
            acquisition_row = self._current_acquisition_human_target(
                target, require_reconnected_preflight=True
            )
            if acquisition_row is None:
                return request
            session = self.query_acquisition_session(
                session_ref=cast(str, target["session_ref"])
            )
            assert session is not None
            evidence_ref = "acquisition_preflight:" + canonical_hash(
                {
                    "session_ref": session.session_ref,
                    "generation": session.preflight_generation,
                    "config_hash": session.config_hash,
                    "evidence_hash": session.evidence_hash,
                }
            )
            return self._satisfy_acquisition_human_request(
                request,
                selected,
                reason_code="institution_route_verified",
                evidence_refs=(evidence_ref,),
            )
        acquisition_row = self._current_acquisition_human_target(
            target, require_reconnected_preflight=False
        )
        if route == "oa_only" and acquisition_row is not None:
            binding_hash = canonical_hash(
                {
                    "route": "oa_only",
                    "session_ref": target["session_ref"],
                    "request_id": target["acquisition_request_id"],
                    "request_hash": target["acquisition_request_hash"],
                    "paper_id": target["acquisition_paper_id"],
                    "item_hash": target["acquisition_item_hash"],
                    "response_ref": selected["response_ref"],
                }
            )
            return self._satisfy_acquisition_human_request(
                request,
                selected,
                reason_code="oa_only_route_selected",
                evidence_refs=("acquisition_route:" + binding_hash,),
            )
        material_binding = (
            None
            if acquisition_row is None
            else self._accepted_material_response_binding(
                selected,
                acquisition_row,
                expected_paper_id=cast(str, target["acquisition_paper_id"]),
            )
        )
        if material_binding is not None:
            return self._satisfy_acquisition_human_request(
                request,
                selected,
                reason_code="accepted_material_bound",
                evidence_refs=(
                    cast(str, material_binding["receipt_ref"]),
                    "acquisition_material:" + canonical_hash(material_binding),
                ),
            )
        if request["status"] != "open":
            return request
        reason_code = (
            "material_binding_validation_required"
            if isinstance(facts, dict)
            and ("material_source_ref" in facts or "material_version_ref" in facts)
            else "institution_reconnect_evidence_required"
        )
        return self.evaluate_human_request(
            request_ref,
            response_refs=(cast(str, selected["response_ref"]),),
            decision="needs_input",
            reason_code=reason_code,
            accepted_evidence_refs=(),
            idempotency_key="ar-hr-needs-input:"
            + canonical_hash(
                {
                    "request_ref": request_ref,
                    "response_ref": selected["response_ref"],
                    "reason_code": reason_code,
                }
            ),
        )

    def _reconcile_acquisition_human_requests(self, session_ref: str) -> None:
        for request in self.query_human_requests(include_history=False):
            target = request.get("target_assertion")
            if (
                isinstance(target, dict)
                and target.get("schema_ref")
                == "meta-research/acquisition-human-request-target/v1"
                and target.get("session_ref") == session_ref
            ):
                self.reconcile_human_request(cast(str, request["request_ref"]))

    def _ensure_acquisition_human_requests(
        self,
        *,
        session_ref: str,
        quest_ref: str | None,
        config_hash: str,
        preflight_generation: int,
        request_id: str,
        request_hash: str,
        attempt_count: int,
    ) -> tuple[dict[str, object], ...]:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT request_json, request_hash, results_json, results_hash "
                    "FROM ar_acquisition_requests WHERE request_id = :request_id "
                    "AND session_ref = :session_ref AND status = 'waiting_user'"
                ),
                {"request_id": request_id, "session_ref": session_ref},
            ).first()
        if row is None or row.request_hash != request_hash:
            raise OwnerConflict("acquisition_human_request_stale")
        request = _acquisition_request_from_json(row.request_json)
        papers = {paper.paper_id: paper for paper in request.papers}
        waiting_items = _waiting_acquisition_item_bindings(
            row.results_json, row.results_hash
        )
        if not waiting_items or any(
            item["paper_id"] not in papers for item in waiting_items
        ):
            raise OwnerConflict("acquisition_human_request_invalid")
        current = self.query_human_requests(include_history=False)
        ensured: list[dict[str, object]] = []
        for item in waiting_items:
            paper_id = cast(str, item["paper_id"])
            target = {
                "schema_ref": "meta-research/acquisition-human-request-target/v1",
                "operation": "resume_acquisition_item",
                "session_ref": session_ref,
                "acquisition_request_id": request_id,
                "acquisition_request_hash": request_hash,
                "acquisition_paper_id": paper_id,
                "acquisition_item_hash": item["item_hash"],
                "config_hash": config_hash,
                "blocked_preflight_generation": preflight_generation,
                "attempt_count": attempt_count,
            }
            waiter = {
                "waiter_ref": "acquisition_item:"
                + canonical_hash({"request_id": request_id, "paper_id": paper_id})[:32],
                "generation": attempt_count,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            }
            obligation = (
                "Restore access or provide an accepted lawful copy for the exact "
                f"literature item {paper_id}."
            )
            conditions = (
                "A newer preflight verifies the exact session and config, or the "
                "Owner accepts an OA-only route for this exact item.",
                "Any provided material is an accepted Research Asset bound to this "
                "exact acquisition item.",
            )
            candidates = [
                candidate
                for candidate in current
                if (
                    candidate.get("status") == "open"
                    or (
                        candidate.get("status") == "satisfied"
                        and candidate.get("target_assertion", {}).get("config_hash")
                        == config_hash
                        and candidate.get("target_assertion", {}).get(
                            "acquisition_item_hash"
                        )
                        == item["item_hash"]
                    )
                )
                and isinstance(candidate.get("target_assertion"), dict)
                and candidate["target_assertion"].get("schema_ref")
                == "meta-research/acquisition-human-request-target/v1"
                and candidate["target_assertion"].get("session_ref") == session_ref
                and candidate["target_assertion"].get("acquisition_request_id")
                == request_id
                and candidate["target_assertion"].get("acquisition_paper_id")
                == paper_id
                and any(
                    waiter.get("status") != "consumed"
                    for waiter in candidate.get("direct_waiters", [])
                    if isinstance(waiter, dict)
                )
            ]
            if len(candidates) > 1:
                raise OwnerConflict("acquisition_human_request_invalid")
            if candidates and (
                candidates[0]["target_assertion"] == target
                or (
                    candidates[0].get("status") == "satisfied"
                    and candidates[0]["target_assertion"].get("config_hash")
                    == config_hash
                    and candidates[0]["target_assertion"].get("acquisition_item_hash")
                    == item["item_hash"]
                )
            ):
                ensured.append(candidates[0])
                continue
            command_binding = {
                "session_ref": session_ref,
                "request_id": request_id,
                "paper_id": paper_id,
                "target": target,
            }
            if candidates:
                result = self.revise_human_request(
                    cast(str, candidates[0]["request_ref"]),
                    expected_revision=cast(int, candidates[0]["revision"]),
                    obligation=obligation,
                    target_assertion=target,
                    acceptance_conditions=conditions,
                    direct_waiters=(waiter,),
                    idempotency_key="ar-acq-hr-revise:"
                    + canonical_hash(command_binding),
                )
            else:
                result = self.open_human_request(
                    request_kind="library_reconnect",
                    obligation=obligation,
                    business_purpose=(
                        "Resume only the exact blocked literature item without "
                        "repeating already obtained material."
                    ),
                    target_assertion=target,
                    acceptance_conditions=conditions,
                    direct_waiter=waiter,
                    idempotency_key="ar-acq-hr:" + canonical_hash(command_binding),
                    quest_ref=quest_ref,
                )
            ensured.append(result)
        return tuple(ensured)

    def _recover_acquisition_human_requests(self) -> None:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT requests.request_id, requests.request_hash, "
                    "requests.attempt_count, sessions.session_ref, "
                    "sessions.quest_ref, sessions.config_hash, "
                    "sessions.preflight_generation, sessions.status AS "
                    "session_status, sessions.reason_code AS session_reason_code, "
                    "requests.results_json, requests.results_hash FROM "
                    "ar_acquisition_requests AS requests "
                    "JOIN ar_acquisition_sessions AS sessions ON "
                    "sessions.session_ref = requests.session_ref WHERE "
                    "requests.status = 'waiting_user'"
                )
            ).all()
        for row in rows:
            if _acquisition_reconciliation_pending(row.results_json, row.results_hash):
                continue
            self._ensure_acquisition_human_requests(
                session_ref=row.session_ref,
                quest_ref=row.quest_ref,
                config_hash=row.config_hash,
                preflight_generation=int(row.preflight_generation),
                request_id=row.request_id,
                request_hash=row.request_hash,
                attempt_count=int(row.attempt_count),
            )
            if row.session_status == "ready":
                self._reconcile_acquisition_human_requests(row.session_ref)

    def acquire_literature(
        self,
        session_ref: str,
        request: AcquisitionBatchRequest,
        provider: AcquisitionProvider,
    ) -> AcquisitionBatchExecution:
        try:
            request_hash = validate_batch_request(request)
            runtime_binding = provider.runtime_binding()
            runtime_binding_hash = validate_acquisition_runtime_binding(runtime_binding)
        except AcquisitionUnavailable as error:
            raise OwnerConflict(error.code) from error
        resume_binding: tuple[str, str, int] | None = None
        resume_target: dict[str, object] | None = None
        resume_route: dict[str, object] | None = None
        human_request: dict[str, object] | None = None
        materialized_asset: object | None = None
        material_path: Path | None = None
        with self._database.read() as connection:
            waiting = connection.execute(
                text(
                    "SELECT requests.status, requests.attempt_count, "
                    "requests.request_json, requests.request_hash, "
                    "requests.results_json, requests.results_hash, "
                    "sessions.quest_ref, sessions.config_hash, "
                    "sessions.preflight_generation, sessions.status AS "
                    "session_status, sessions.reason_code AS session_reason_code "
                    "FROM ar_acquisition_requests "
                    "AS requests JOIN ar_acquisition_sessions AS sessions ON "
                    "sessions.session_ref = requests.session_ref WHERE "
                    "requests.request_id = :request_id AND requests.session_ref = "
                    ":session_ref"
                ),
                {"request_id": request.request_id, "session_ref": session_ref},
            ).first()
        technical_reconciliation = (
            waiting is not None
            and waiting.status == "waiting_user"
            and _acquisition_reconciliation_pending(
                waiting.results_json, waiting.results_hash
            )
        )
        if technical_reconciliation:
            resume_route = self._query_acquisition_resume_route(
                request.request_id,
                int(waiting.attempt_count),
                session_ref,
                request_hash,
            )
            if (
                resume_route is not None
                and resume_route["route"] == "accepted_material"
            ):
                materialized_asset, material_path = (
                    self._prepare_accepted_material_route(
                        session_ref, request.request_id, resume_route
                    )
                )
        if (
            waiting is not None
            and waiting.status == "waiting_user"
            and not technical_reconciliation
        ):
            human_requests = self._ensure_acquisition_human_requests(
                session_ref=session_ref,
                quest_ref=waiting.quest_ref,
                config_hash=waiting.config_hash,
                preflight_generation=int(waiting.preflight_generation),
                request_id=request.request_id,
                request_hash=request_hash,
                attempt_count=int(waiting.attempt_count),
            )
            resumable = []
            for candidate in human_requests:
                candidate_waiters = cast(
                    list[dict[str, object]], candidate["direct_waiters"]
                )
                disposition = candidate.get("disposition")
                if (
                    candidate.get("status") == "satisfied"
                    and isinstance(disposition, dict)
                    and disposition.get("decision") == "satisfied"
                    and len(candidate_waiters) == 1
                    and candidate_waiters[0].get("status") == "released"
                ):
                    resumable.append((candidate, candidate_waiters[0]))
            if not resumable:
                raise OwnerConflict("acquisition_human_request_not_released")
            human_request, waiter = sorted(
                resumable,
                key=lambda item: (
                    str(item[0]["target_assertion"].get("acquisition_paper_id")),
                    str(item[0]["request_ref"]),
                ),
            )[0]
            resume_binding = (
                cast(str, human_request["request_ref"]),
                cast(str, waiter["waiter_ref"]),
                cast(int, waiter["generation"]),
            )
            resume_target = cast(dict[str, object], human_request["target_assertion"])
            resume_route = self._acquisition_resume_route(human_request, waiting)
            if resume_route["route"] == "accepted_material":
                materialized_asset, material_path = (
                    self._prepare_accepted_material_route(
                        session_ref, request.request_id, resume_route
                    )
                )
        now = time.time()
        previous_results: tuple[AcquisitionItemResult, ...] = ()
        reconcile_only = False
        new_request = False
        with self._database.write() as connection:
            session_row = connection.execute(
                text(
                    "SELECT * FROM ar_acquisition_sessions WHERE "
                    "session_ref = :session_ref"
                ),
                {"session_ref": session_ref},
            ).first()
            if session_row is None:
                raise OwnerConflict("acquisition_session_not_found")
            if session_row.runtime_binding_hash != runtime_binding_hash:
                raise OwnerConflict("acquisition_runtime_binding_drift")
            existing = connection.execute(
                text(
                    "SELECT * FROM ar_acquisition_requests WHERE "
                    "request_id = :request_id"
                ),
                {"request_id": request.request_id},
            ).first()
            if existing is not None:
                if (
                    existing.session_ref != session_ref
                    or existing.request_hash != request_hash
                ):
                    raise OwnerConflict("acquisition_request_identity_conflict")
                if existing.status in {"obtained", "partial", "missing"}:
                    return _acquisition_execution_from_row(
                        existing,
                        session_row,
                        self._acquisition_private_root,
                    )
                if existing.status == "running":
                    raise OwnerConflict("acquisition_request_busy")
                if existing.status != "waiting_user":
                    raise OwnerConflict("acquisition_request_not_resumable")
                previous_execution = _acquisition_execution_from_row(
                    existing,
                    session_row,
                    self._acquisition_private_root,
                )
                previous_results = previous_execution.results
                if [result.paper_id for result in previous_results] == ["__batch__"]:
                    previous_results = tuple(
                        _reconciliation_acquisition_item(paper.paper_id)
                        for paper in request.papers
                    )
                    reconcile_only = True
                else:
                    try:
                        validate_item_results(request, previous_results)
                    except AcquisitionUnavailable as error:
                        raise OwnerConflict(error.code) from error
                    reconcile_only = any(
                        result.status == "waiting_user"
                        and result.failure is not None
                        and result.failure.get("code")
                        == "acquisition_reconciliation_required"
                        for result in previous_results
                    )
                attempt_count = int(existing.attempt_count) + 1
            else:
                new_request = True
                attempt_count = 1
                previous_results = tuple(
                    _reconciliation_acquisition_item(paper.paper_id)
                    for paper in request.papers
                )

            if resume_binding is not None:
                assert resume_target is not None and resume_route is not None
                if (
                    new_request
                    or existing is None
                    or existing.status != "waiting_user"
                    or int(existing.attempt_count) < resume_target["attempt_count"]
                    or existing.request_hash
                    != resume_target["acquisition_request_hash"]
                    or not _acquisition_target_matches_waiting_item(
                        resume_target, existing
                    )
                    or session_row.config_hash != resume_target["config_hash"]
                    or session_row.current_request_id
                    != resume_target["acquisition_request_id"]
                    or (
                        resume_route["route"] == "institutional_browser_reconnected"
                        and (
                            session_row.status != "ready"
                            or int(session_row.preflight_generation)
                            <= resume_target["blocked_preflight_generation"]
                        )
                    )
                    or (
                        resume_route["route"] != "institutional_browser_reconnected"
                        and session_row.status not in {"ready", "waiting_user"}
                    )
                ):
                    raise OwnerConflict("acquisition_human_request_stale")

            claimed = connection.execute(
                text(
                    "UPDATE ar_acquisition_sessions SET status = 'acquiring', "
                    "current_request_id = :request_id, slot_held = 1, "
                    "reason_code = NULL, updated_at = :now WHERE session_ref = "
                    ":session_ref AND slot_held = 0 AND (status = 'ready' OR "
                    "(:reconcile_only = 1 AND status = 'waiting_user' AND "
                    "reason_code = 'acquisition_reconciliation_required') OR "
                    "(:human_resume = 1 AND status = 'waiting_user'))"
                ),
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                    "reconcile_only": 1 if reconcile_only else 0,
                    "human_resume": 1 if resume_binding is not None else 0,
                    "now": now,
                },
            )
            if claimed.rowcount != 1:
                raise OwnerConflict("acquisition_session_busy")

            inflight_results = tuple(
                (
                    _reconciliation_acquisition_item(result.paper_id)
                    if result.status == "waiting_user"
                    and (
                        resume_route is None
                        or result.paper_id == resume_route["paper_id"]
                    )
                    else result
                )
                for result in previous_results
            )
            inflight_payload = [result.as_dict() for result in inflight_results]
            if new_request:
                connection.execute(
                    text(
                        "INSERT INTO ar_acquisition_requests (request_id, session_ref, "
                        "request_json, request_hash, route_policy, status, results_json, "
                        "results_hash, attempt_count, created_at, updated_at) VALUES "
                        "(:request_id, :session_ref, :request_json, :request_hash, "
                        ":route_policy, 'running', :results_json, :results_hash, 1, "
                        ":now, :now)"
                    ),
                    {
                        "request_id": request.request_id,
                        "session_ref": session_ref,
                        "request_json": canonical_json(request.identity_payload()),
                        "request_hash": request_hash,
                        "route_policy": request.route_policy,
                        "results_json": canonical_json(inflight_payload),
                        "results_hash": canonical_hash(inflight_payload),
                        "now": now,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_requests SET status = 'running', "
                        "results_json = :results_json, results_hash = :results_hash, "
                        "attempt_count = :attempt_count, updated_at = :now, "
                        "completed_at = NULL WHERE request_id = :request_id"
                    ),
                    {
                        "request_id": request.request_id,
                        "results_json": canonical_json(inflight_payload),
                        "results_hash": canonical_hash(inflight_payload),
                        "attempt_count": attempt_count,
                        "now": now,
                    },
                )
            if resume_binding is not None:
                assert (
                    resume_route is not None
                    and resume_target is not None
                    and human_request is not None
                )
                consumption = self._consume_human_request_waiter(
                    connection,
                    request_ref=resume_binding[0],
                    waiter_ref=resume_binding[1],
                    generation=resume_binding[2],
                    work_ref="acquisition_item:"
                    + canonical_hash(
                        {
                            "request_id": request.request_id,
                            "attempt_no": attempt_count,
                            "paper_id": resume_route["paper_id"],
                        }
                    ),
                    work_hash=canonical_hash(
                        {"request_hash": request_hash, "route": resume_route}
                    ),
                )
                evaluation = cast(dict[str, object], human_request["evaluation"])
                self._record_acquisition_resume_route(
                    connection,
                    request_id=request.request_id,
                    attempt_no=attempt_count,
                    session_ref=session_ref,
                    request_hash=request_hash,
                    human_request_ref=resume_binding[0],
                    evaluation_ref=cast(str, evaluation["evaluation_ref"]),
                    route=resume_route,
                    consumption=consumption,
                )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "acquisition_request_count = acquisition_request_count + "
                    ":request_increment, acquisition_active_slot_count = "
                    "acquisition_active_slot_count + 1 WHERE singleton = 'owner'"
                ),
                {"request_increment": 1 if new_request else 0},
            )
            self._feed.record(
                connection,
                "agent_runtime.acquisition_batch_started",
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                    "route_policy": request.route_policy,
                },
            )
            browser_context_ref = (
                None
                if session_row.browser_context_ref is None
                or (
                    resume_route is not None
                    and resume_route["route"] in {"oa_only", "accepted_material"}
                )
                else str(session_row.browser_context_ref)
            )

        target_dir = (
            self._acquisition_private_root
            / session_ref
            / "requests"
            / request.request_id
        )
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        effective_session_mode = str(session_row.mode)
        if resume_route is not None and resume_route["route"] == "oa_only":
            effective_session_mode = "oa_only"
        elif resume_route is not None and resume_route["route"] == "accepted_material":
            effective_session_mode = "provided_only"
        provider_request = request.bind_to_session(
            session_ref=session_ref,
            session_mode=effective_session_mode,
            browser_context_ref=browser_context_ref,
            provider_state_dir=self._acquisition_private_root / session_ref,
            target_dir=target_dir,
        )
        if resume_route is not None:
            affected_ids = {cast(str, resume_route["paper_id"])}
        elif reconcile_only:
            affected_ids = {
                result.paper_id
                for result in previous_results
                if result.status == "waiting_user"
                and result.failure is not None
                and result.failure.get("code") == "acquisition_reconciliation_required"
            }
        else:
            affected_ids = {
                result.paper_id
                for result in previous_results
                if result.status == "waiting_user"
            }
        affected_papers = tuple(
            paper
            for paper in provider_request.papers
            if new_request or paper.paper_id in affected_ids
        )
        attempt_request = replace(provider_request, papers=affected_papers)
        try:
            reconcile = getattr(provider, "reconcile", None)
            if (
                resume_route is not None
                and resume_route["route"] == "accepted_material"
            ):
                if material_path is None:
                    raise OwnerConflict("acquisition_material_binding_invalid")
                attempted_results = (
                    AcquisitionItemResult(
                        paper_id=cast(str, resume_route["paper_id"]),
                        status="obtained",
                        path=str(material_path),
                        format=cast(str, resume_route["format"]),
                        failure=None,
                    ),
                )
            elif reconcile_only:
                if not callable(reconcile):
                    attempted_results = tuple(
                        _reconciliation_acquisition_item(paper.paper_id)
                        for paper in attempt_request.papers
                    )
                else:
                    attempted_results = tuple(reconcile(attempt_request))
            else:
                attempted_results = tuple(provider.acquire(attempt_request))
            attempted_results = validate_item_results(
                attempt_request, attempted_results
            )
            attempted_by_id = {result.paper_id: result for result in attempted_results}
            previous_by_id = {
                result.paper_id: result
                for result in previous_results
                if result.paper_id not in affected_ids
            }
            results = tuple(
                attempted_by_id.get(paper.paper_id)
                or previous_by_id.get(paper.paper_id)
                or _reconciliation_acquisition_item(paper.paper_id)
                for paper in provider_request.papers
            )
            results = validate_item_results(provider_request, results)
            status = aggregate_batch_status(results)
        except Exception:
            attempted_by_id = {
                paper.paper_id: _reconciliation_acquisition_item(paper.paper_id)
                for paper in attempt_request.papers
            }
            previous_by_id = {
                result.paper_id: result
                for result in previous_results
                if result.paper_id not in affected_ids
            }
            results = tuple(
                attempted_by_id.get(paper.paper_id)
                or previous_by_id.get(paper.paper_id)
                or _reconciliation_acquisition_item(paper.paper_id)
                for paper in provider_request.papers
            )
            status = aggregate_batch_status(results)
        results_payload = [result.as_dict() for result in results]
        results_json = canonical_json(results_payload)
        results_hash = canonical_hash(results_payload)
        completed_at = time.time()
        session_status = "waiting_user" if status == "waiting_user" else "ready"
        reason_code = (
            next(
                (
                    result.failure["code"]
                    for result in results
                    if result.status == "waiting_user" and result.failure is not None
                ),
                "acquisition_waiting_user",
            )
            if status == "waiting_user"
            else None
        )
        with self._database.write() as connection:
            current = connection.execute(
                text(
                    "SELECT status, session_ref, request_hash FROM "
                    "ar_acquisition_requests WHERE request_id = :request_id"
                ),
                {"request_id": request.request_id},
            ).one()
            if (
                current.status != "running"
                or current.session_ref != session_ref
                or current.request_hash != request_hash
            ):
                raise OwnerConflict("acquisition_request_fence_stale")
            connection.execute(
                text(
                    "UPDATE ar_acquisition_requests SET status = :status, "
                    "results_json = :results_json, results_hash = :results_hash, "
                    "updated_at = :now, completed_at = :now WHERE request_id = "
                    ":request_id"
                ),
                {
                    "request_id": request.request_id,
                    "status": status,
                    "results_json": results_json,
                    "results_hash": results_hash,
                    "now": completed_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_acquisition_sessions SET status = :session_status, "
                    "request_count = request_count + :request_increment, "
                    "current_request_id = CASE WHEN :session_status = "
                    "'waiting_user' THEN :request_id ELSE NULL END, slot_held = 0, "
                    "reason_code = :reason_code, updated_at = :now WHERE "
                    "session_ref = :session_ref AND status = 'acquiring'"
                ),
                {
                    "session_ref": session_ref,
                    "session_status": session_status,
                    "request_id": request.request_id,
                    "request_increment": 1 if new_request else 0,
                    "reason_code": reason_code,
                    "now": completed_at,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "acquisition_active_slot_count = "
                    "acquisition_active_slot_count - 1 WHERE singleton = 'owner' "
                    "AND acquisition_active_slot_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.acquisition_batch_completed",
                {
                    "session_ref": session_ref,
                    "request_id": request.request_id,
                    "status": status,
                },
            )
        if status == "waiting_user" and not _acquisition_reconciliation_pending(
            results_json, results_hash
        ):
            self._ensure_acquisition_human_requests(
                session_ref=session_ref,
                quest_ref=session_row.quest_ref,
                config_hash=session_row.config_hash,
                preflight_generation=int(session_row.preflight_generation),
                request_id=request.request_id,
                request_hash=request_hash,
                attempt_count=attempt_count,
            )
        return AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status=status,
            request=provider_request,
            results=results,
        )

    def bind_acquisition_session_to_quest(
        self, initialization_id: str, quest_ref: str
    ) -> AcquisitionSession | None:
        if not initialization_id or not quest_ref:
            raise OwnerConflict("acquisition_quest_binding_invalid")
        with self._database.write() as connection:
            row = connection.execute(
                text(
                    "SELECT quest_ref FROM ar_acquisition_sessions WHERE "
                    "initialization_id = :initialization_id"
                ),
                {"initialization_id": initialization_id},
            ).first()
            if row is None:
                return None
            if row.quest_ref not in {None, quest_ref}:
                raise OwnerConflict("acquisition_quest_binding_conflict")
            if row.quest_ref is None:
                connection.execute(
                    text(
                        "UPDATE ar_acquisition_sessions SET quest_ref = :quest_ref, "
                        "updated_at = :now WHERE initialization_id = "
                        ":initialization_id"
                    ),
                    {
                        "initialization_id": initialization_id,
                        "quest_ref": quest_ref,
                        "now": time.time(),
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.acquisition_bound_to_quest",
                    {
                        "initialization_id": initialization_id,
                        "quest_ref": quest_ref,
                    },
                )
        return self.query_acquisition_session(initialization_id=initialization_id)

    def _recover_interrupted_deepfetch(self) -> None:
        now = time.time()
        with self._database.write() as connection:
            attempts = connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'superseded', "
                    "failure_code = 'daemon_restarted', completed_at = :now "
                    "WHERE status = 'running' AND NOT EXISTS (SELECT 1 FROM "
                    "ar_run_controls controls WHERE controls.run_ref = "
                    "ar_deepfetch_attempts.run_ref AND controls.status != 'running') "
                    "AND NOT EXISTS (SELECT 1 FROM ar_control_reservations reserved, "
                    "json_each(reserved.affected_runs_json) affected WHERE "
                    "reserved.status = 'prepared' AND "
                    "json_extract(affected.value, '$.run_ref') = "
                    "ar_deepfetch_attempts.run_ref)"
                ),
                {"now": now},
            )
            runs = connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'admitted', "
                    "current_attempt_ref = NULL, failure_code = NULL, "
                    "completed_at = NULL, updated_at = :now WHERE status = "
                    "'running' AND NOT EXISTS (SELECT 1 FROM ar_run_controls controls "
                    "WHERE controls.run_ref = ar_deepfetch_runs.run_ref AND "
                    "controls.status != 'running') AND NOT EXISTS (SELECT 1 FROM "
                    "ar_control_reservations reserved, "
                    "json_each(reserved.affected_runs_json) affected WHERE "
                    "reserved.status = 'prepared' AND "
                    "json_extract(affected.value, '$.run_ref') = "
                    "ar_deepfetch_runs.run_ref)"
                ),
                {"now": now},
            )
            recovered = (attempts.rowcount or 0) + (runs.rowcount or 0)
            if recovered:
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET attempt_ref = NULL, fence_ref = "
                        "NULL, control_revision = control_revision + 1, updated_at = "
                        ":now WHERE run_kind = 'deepfetch' AND status = 'running' "
                        "AND run_ref IN (SELECT run_ref FROM ar_deepfetch_runs "
                        "WHERE status = 'admitted' AND current_attempt_ref IS NULL)"
                    ),
                    {"now": now},
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.deepfetch_recovered",
                    {"recovered_record_count": recovered},
                )

    def _verify_deepfetch_acquisition_binding(
        self,
        request: DeepFetchRunRequest,
        *,
        require_ready: bool,
    ) -> AcquisitionSession:
        session = self.query_acquisition_session(
            session_ref=request.acquisition_session_ref
        )
        if (
            session is None
            or session.initialization_id != request.initialization_id
            or session.config_hash != request.acquisition_config_hash
            or session.runtime_binding_hash != request.acquisition_runtime_binding_hash
        ):
            raise OwnerConflict("deepfetch_acquisition_binding_invalid")
        if require_ready and (session.status != "ready" or session.slot_held):
            raise OwnerConflict("deepfetch_acquisition_not_ready")
        if require_ready and session.current_request_id is not None:
            with self._database.read() as connection:
                acquisition_request = connection.execute(
                    text(
                        "SELECT status, attempt_count, request_hash FROM "
                        "ar_acquisition_requests "
                        "WHERE request_id = :request_id AND session_ref = :session_ref"
                    ),
                    {
                        "request_id": session.current_request_id,
                        "session_ref": session.session_ref,
                    },
                ).first()
            if (
                acquisition_request is not None
                and acquisition_request.status == "waiting_user"
            ):
                human_requests = self._find_acquisition_human_requests(
                    session_ref=session.session_ref,
                    request_id=session.current_request_id,
                    attempt_count=int(acquisition_request.attempt_count),
                    config_hash=session.config_hash,
                    request_hash=str(acquisition_request.request_hash),
                )
                if not any(
                    human_request.get("status") == "satisfied"
                    and (human_request.get("disposition") or {}).get("decision")
                    == "satisfied"
                    and len(human_request.get("direct_waiters", [])) == 1
                    and human_request["direct_waiters"][0].get("status") == "released"
                    for human_request in human_requests
                ):
                    raise OwnerConflict("deepfetch_acquisition_not_ready")
        return session

    def _find_acquisition_human_requests(
        self,
        *,
        session_ref: str,
        request_id: str,
        attempt_count: int,
        config_hash: str,
        request_hash: str,
    ) -> tuple[dict[str, object], ...]:
        matches = []
        for request in self.query_human_requests(include_history=False):
            target = request.get("target_assertion")
            if (
                isinstance(target, dict)
                and target.get("schema_ref")
                == "meta-research/acquisition-human-request-target/v1"
                and target.get("operation") == "resume_acquisition_item"
                and target.get("session_ref") == session_ref
                and target.get("acquisition_request_id") == request_id
                and isinstance(target.get("attempt_count"), int)
                and cast(int, target["attempt_count"]) <= attempt_count
                and target.get("config_hash") == config_hash
                and target.get("acquisition_request_hash") == request_hash
            ):
                matches.append(request)
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    str(item["target_assertion"].get("acquisition_paper_id")),
                    str(item["request_ref"]),
                ),
            )
        )

    def execute_deepfetch(
        self,
        request: DeepFetchRunRequest,
        provider: DeepFetchProvider,
    ) -> DeepFetchRun:
        if self._deepfetch_request_verifier is None:
            raise OwnerConflict("deepfetch_request_verifier_unavailable")
        self._deepfetch_request_verifier.verify_deepfetch_run_request(
            request_ref=request.request_ref,
            initialization_id=request.initialization_id,
            correlation_ref=request.correlation_ref,
            draft_revision=request.draft_revision,
            draft_hash=request.draft_hash,
            scope_hash=request.scope_hash,
            material_bindings_hash=canonical_hash(
                list(request.accepted_material_bindings)
            ),
            resource_envelope_ref=request.resource_envelope_ref,
            resource_envelope_hash=request.resource_envelope_hash,
            acquisition_session_ref=request.acquisition_session_ref,
            acquisition_config_hash=request.acquisition_config_hash,
            acquisition_runtime_binding_hash=(request.acquisition_runtime_binding_hash),
            result_route=request.result_route,
            receipt=request.authorization_receipt,
            require_active=False,
            creation_context_kind=request.creation_context_kind,
            creation_context_ref=request.creation_context_ref,
            context_generation=request.context_generation,
            quest_ref=request.quest_ref,
            parent_question_ref=request.parent_question_ref,
            context_basis_hash=request.context_basis_hash,
        )
        if (
            request.result_route
            not in {
                "same_quest_initialization_proposal",
                "same_manual_question_creation_proposal",
            }
            or canonical_hash(request.scope) != request.scope_hash
            or request.draft_revision < 1
            or canonical_hash(request.draft) != request.draft_hash
            or request.creation_context_kind == "manual_question_creation"
            and (
                request.result_route != "same_manual_question_creation_proposal"
                or request.creation_context_ref is None
                or request.context_generation is None
                or request.context_generation < 1
                or request.quest_ref is None
                or request.parent_question_ref is None
                or request.context_basis_hash is None
            )
        ):
            raise OwnerConflict("deepfetch_run_request_invalid")
        self._verify_deepfetch_acquisition_binding(request, require_ready=False)
        try:
            runtime_binding = provider.runtime_binding()
            runtime_binding_hash = validate_runtime_binding(runtime_binding)
        except DeepFetchUnavailable as error:
            raise OwnerConflict(error.code) from error
        runtime_binding_json = canonical_json(runtime_binding.as_dict())
        request_hash = canonical_hash(request.payload())

        existing = self.query_deepfetch_run(request.request_ref)
        if existing is not None and existing.status == "executed":
            if (
                existing.correlation_ref != request.correlation_ref
                or existing.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("deepfetch_run_identity_conflict")
            return existing

        (
            run_ref,
            root_session_ref,
            attempt_ref,
            generation,
            fence_ref,
            native_ref,
            provider_operation_ref,
        ) = self._start_deepfetch_attempt(
            request=request,
            request_hash=request_hash,
            runtime_binding_json=runtime_binding_json,
            runtime_binding_hash=runtime_binding_hash,
        )
        # Provider effects belong to the logical Run, not to a replaceable
        # Attempt. Interrupted successor Fences reconcile the same operation;
        # only a verified terminal receipt authorizes a new operation ref.
        job_ref = provider_operation_ref
        provider_unit_ref = "provider_unit_" + canonical_hash(
            {"provider_operation_ref": job_ref, "attempt_ref": attempt_ref}
        )[:64]
        provider_request = DeepFetchProviderRequest(
            request_ref=request.request_ref,
            initialization_id=request.initialization_id,
            correlation_ref=request.correlation_ref,
            draft_revision=request.draft_revision,
            draft_hash=request.draft_hash,
            scope=request.scope,
            scope_hash=request.scope_hash,
            acquisition_session_ref=request.acquisition_session_ref,
            acquisition_config_hash=request.acquisition_config_hash,
            acquisition_runtime_binding_hash=(request.acquisition_runtime_binding_hash),
            accepted_material_bindings=request.accepted_material_bindings,
            authorization_receipt=request.authorization_receipt,
            runtime_binding=runtime_binding,
            run_ref=run_ref,
            root_session_ref=root_session_ref,
            attempt_ref=attempt_ref,
            attempt_generation=generation,
            fence_ref=fence_ref,
            native_session_ref=native_ref,
            job_ref=job_ref,
        )
        with self._deepfetch_provider_lock:
            self._deepfetch_providers[request.request_ref] = provider
        self.begin_provider_unit(
            unit_ref=provider_unit_ref,
            operation_ref=job_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            unit_kind="deepfetch",
        )
        provider_safe = True
        try:
            result = provider.execute(provider_request)
            result_payload, result_hash = validate_deepfetch_result(
                provider_request, result
            )
            return self._complete_deepfetch_attempt(
                request=request,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=generation,
                fence_ref=fence_ref,
                runtime_binding_hash=runtime_binding_hash,
                result_payload=result_payload,
                result_hash=result_hash,
            )
        except DeepFetchUnavailable as error:
            if error.code == "deepfetch_provider_reconciliation_pending":
                provider_safe = False
            if (
                error.code == "deepfetch_provider_stopped"
                and self.provider_quiescence_requested(run_ref)
            ):
                pass
            elif error.code in {
                "deepfetch_acquisition_waiting_user",
                "deepfetch_provider_stopped",
                "deepfetch_provider_reconciliation_pending",
            }:
                effective_code = self._interrupt_deepfetch_attempt(
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    fence_ref=fence_ref,
                    reason_code=error.code,
                    native_session_ref=error.native_session_ref,
                )
                if effective_code != error.code:
                    raise DeepFetchUnavailable(effective_code) from error
            else:
                requires_verified_terminal = bool(
                    getattr(provider, "requires_verified_terminal_retry", False)
                )
                self._fail_deepfetch_attempt(
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    fence_ref=fence_ref,
                    failure_code=error.code,
                    provider_operation_retry_permitted=(
                        not requires_verified_terminal
                        or error.durable_outcome == "terminal"
                    ),
                    native_session_ref=error.native_session_ref,
                )
            raise
        except OwnerConflict as error:
            if error.code in {
                "runtime_run_suspended",
                "runtime_fence_revoked",
                "runtime_reconciliation_required",
                "terminal_run_cannot_reopen",
            }:
                # A durable control Fence won the race with a provider result.
                # Keep the control state and Safe Point authoritative; this
                # result is a late write, not a new technical failure.
                raise
            if self.provider_quiescence_requested(run_ref):
                raise
            self._fail_deepfetch_attempt(
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                generation=generation,
                fence_ref=fence_ref,
                failure_code="deepfetch_provider_error",
                provider_operation_retry_permitted=False,
                native_session_ref=None,
            )
            raise
        except BaseException:
            if not self.provider_quiescence_requested(run_ref):
                self._fail_deepfetch_attempt(
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    fence_ref=fence_ref,
                    failure_code="deepfetch_provider_error",
                    provider_operation_retry_permitted=False,
                    native_session_ref=None,
                )
            raise
        finally:
            if provider_safe:
                self.acknowledge_provider_safe_point(
                    unit_ref=provider_unit_ref,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    fence_ref=fence_ref,
                )
            with self._deepfetch_provider_lock:
                current_provider = self._deepfetch_providers.get(request.request_ref)
                if current_provider is provider:
                    self._deepfetch_providers.pop(request.request_ref, None)
            finish_job = getattr(provider, "finish_job", None)
            if provider_safe and callable(finish_job):
                finish_job(job_ref)

    def _interrupt_deepfetch_attempt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        generation: int,
        fence_ref: str,
        reason_code: str,
        native_session_ref: str | None,
    ) -> str:
        """Fence a daemon-local wait while leaving the logical Run admitted."""

        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT status, current_attempt_ref, attempt_generation, "
                    "request_ref, reconciliation_attempt_count FROM "
                    "ar_deepfetch_runs WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT status, fence_ref FROM ar_deepfetch_attempts WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).first()
            session = connection.execute(
                text(
                    "SELECT native_session_ref FROM ar_deepfetch_sessions "
                    "WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
            if (
                run is None
                or attempt is None
                or session is None
                or (
                    run.status != "running"
                    or run.current_attempt_ref != attempt_ref
                    or int(run.attempt_generation) != generation
                    or attempt.status != "running"
                    or attempt.fence_ref != fence_ref
                )
            ):
                return reason_code
            if (
                native_session_ref is not None
                and session.native_session_ref is not None
                and str(session.native_session_ref) != native_session_ref
            ):
                return "deepfetch_native_session_changed"
            pending_count = (
                int(run.reconciliation_attempt_count) + 1
                if reason_code == "deepfetch_provider_reconciliation_pending"
                else 0
            )
            if pending_count >= MAX_DEEPFETCH_RECONCILIATION_ATTEMPTS:
                blocker = "deepfetch_provider_outcome_unknown"
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_attempts SET status = 'failed', "
                        "failure_code = :blocker, completed_at = :now WHERE "
                        "attempt_ref = :attempt_ref"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "blocker": blocker,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_runs SET status = 'failed', "
                        "provider_operation_retry_permitted = 0, "
                        "reconciliation_attempt_count = :pending_count, "
                        "next_reconcile_at = NULL, failure_code = :blocker, "
                        "completed_at = :now, updated_at = :now WHERE "
                        "run_ref = :run_ref"
                    ),
                    {
                        "run_ref": run_ref,
                        "pending_count": pending_count,
                        "blocker": blocker,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = "
                        "'reconciliation_required', control_revision = "
                        "control_revision + 1, terminal_reason = :blocker, "
                        "updated_at = :now WHERE run_ref = :run_ref"
                    ),
                    {"blocker": blocker, "now": now, "run_ref": run_ref},
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "active_run_count = active_run_count - 1 WHERE "
                        "singleton = 'owner' AND active_run_count > 0"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.deepfetch_reconciliation_blocked",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "reason_code": blocker,
                    },
                )
                return blocker
            retry_delay = (
                min(
                    DEEPFETCH_RECONCILIATION_MAX_SECONDS,
                    DEEPFETCH_RECONCILIATION_BASE_SECONDS
                    * (2 ** min(pending_count - 1, 10)),
                )
                if pending_count
                else None
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'superseded', "
                    "failure_code = :reason_code, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "reason_code": reason_code,
                    "now": now,
                },
            )
            if native_session_ref is not None:
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_sessions SET native_session_ref = "
                        ":native_session_ref, updated_at = :now WHERE run_ref = "
                        ":run_ref"
                    ),
                    {
                        "run_ref": run_ref,
                        "native_session_ref": native_session_ref,
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'admitted', "
                    "current_attempt_ref = NULL, failure_code = NULL, "
                    "provider_operation_retry_permitted = 0, "
                    "reconciliation_attempt_count = :pending_count, "
                    "next_reconcile_at = :next_reconcile_at, completed_at = NULL, "
                    "updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "pending_count": pending_count,
                    "next_reconcile_at": (
                        None if retry_delay is None else now + retry_delay
                    ),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET status = 'running', attempt_ref = "
                    "NULL, fence_ref = NULL, control_revision = control_revision + "
                    "1, terminal_reason = :reason_code, updated_at = :now WHERE "
                    "run_ref = :run_ref"
                ),
                {
                    "reason_code": reason_code,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_reconciliation_deferred",
                {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "reason_code": reason_code,
                },
            )
        return reason_code

    def _start_deepfetch_attempt(
        self,
        *,
        request: DeepFetchRunRequest,
        request_hash: str,
        runtime_binding_json: str,
        runtime_binding_hash: str,
    ) -> tuple[str, str, str, int, str, str | None, str]:
        now = time.time()
        with self._database.write() as connection:
            # Acquire SQLite's writer reservation before checking HC liveness.
            # Quest cancellation uses the same durable writer, so either it
            # commits first and this admission is rejected, or this Run/Attempt
            # becomes visible before cancellation tries to fence it.
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            verifier = self._deepfetch_request_verifier
            if verifier is None:
                raise OwnerConflict("deepfetch_request_verifier_unavailable")
            verifier.verify_deepfetch_run_request(
                request_ref=request.request_ref,
                initialization_id=request.initialization_id,
                correlation_ref=request.correlation_ref,
                draft_revision=request.draft_revision,
                draft_hash=request.draft_hash,
                scope_hash=request.scope_hash,
                material_bindings_hash=canonical_hash(
                    list(request.accepted_material_bindings)
                ),
                resource_envelope_ref=request.resource_envelope_ref,
                resource_envelope_hash=request.resource_envelope_hash,
                acquisition_session_ref=request.acquisition_session_ref,
                acquisition_config_hash=request.acquisition_config_hash,
                acquisition_runtime_binding_hash=(
                    request.acquisition_runtime_binding_hash
                ),
                result_route=request.result_route,
                receipt=request.authorization_receipt,
                require_active=True,
                creation_context_kind=request.creation_context_kind,
                creation_context_ref=request.creation_context_ref,
                context_generation=request.context_generation,
                quest_ref=request.quest_ref,
                parent_question_ref=request.parent_question_ref,
                context_basis_hash=request.context_basis_hash,
            )
            self._verify_deepfetch_acquisition_binding(request, require_ready=True)
            run = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request.request_ref},
            ).first()
            is_new = run is None
            was_failed = run is not None and run.status == "failed"
            if run is not None:
                if (
                    run.request_hash != request_hash
                    or run.correlation_ref != request.correlation_ref
                    or run.runtime_binding_json != runtime_binding_json
                    or run.runtime_binding_hash != runtime_binding_hash
                ):
                    raise OwnerConflict("deepfetch_run_identity_conflict")
                if run.status == "executed":
                    raise OwnerConflict("deepfetch_run_already_executed")
                if run.status == "running":
                    raise OwnerConflict("deepfetch_run_busy")
                if run.status == "cancelled":
                    raise OwnerConflict("deepfetch_run_cancelled")
                if (
                    run.status == "admitted"
                    and run.next_reconcile_at is not None
                    and float(run.next_reconcile_at) > now
                ):
                    raise OwnerConflict("deepfetch_run_busy")
                control_row = connection.execute(
                    text(
                        "SELECT fence_ref FROM ar_run_controls WHERE run_ref = "
                        ":run_ref"
                    ),
                    {"run_ref": run.run_ref},
                ).first()
                if control_row is not None:
                    _assert_runtime_control_allows(
                        connection,
                        str(run.run_ref),
                        (
                            ""
                            if control_row.fence_ref is None
                            else str(control_row.fence_ref)
                        ),
                    )
                run_ref = str(run.run_ref)
                generation = int(run.attempt_generation) + 1
                provider_operation_generation = int(run.provider_operation_generation)
                provider_operation_ref = str(run.provider_operation_ref)
                reconciliation_attempt_count = int(run.reconciliation_attempt_count)
                if was_failed and bool(run.provider_operation_retry_permitted):
                    provider_operation_generation += 1
                    provider_operation_ref = typed_provider_operation_ref(
                        run_ref,
                        "deepfetch",
                        provider_operation_generation,
                    )
                    reconciliation_attempt_count = 0
                session = connection.execute(
                    text(
                        "SELECT * FROM ar_deepfetch_sessions WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run_ref},
                ).one()
                root_session_ref = str(session.root_session_ref)
                native_session_ref = (
                    None
                    if session.native_session_ref is None
                    else str(session.native_session_ref)
                )
            else:
                run_ref = new_ref("deepfetch_run")
                provider_operation_generation = 1
                provider_operation_ref = typed_provider_operation_ref(
                    run_ref, "deepfetch", 1
                )
                reconciliation_attempt_count = 0
                root_session_ref = new_ref("deepfetch_session")
                native_session_ref = None
                generation = 1
                connection.execute(
                    text(
                        "INSERT INTO ar_deepfetch_runs (run_ref, request_ref, "
                        "correlation_ref, request_hash, runtime_binding_json, "
                        "runtime_binding_hash, status, attempt_generation, "
                        "provider_operation_ref, provider_operation_generation, "
                        "provider_operation_retry_permitted, "
                        "reconciliation_attempt_count, created_at, updated_at) VALUES "
                        "(:run_ref, :request_ref, :correlation_ref, "
                        ":request_hash, :runtime_binding_json, :runtime_binding_hash, "
                        "'admitted', 0, :provider_operation_ref, "
                        ":provider_operation_generation, 0, 0, :now, :now)"
                    ),
                    {
                        "run_ref": run_ref,
                        "request_ref": request.request_ref,
                        "correlation_ref": request.correlation_ref,
                        "request_hash": request_hash,
                        "runtime_binding_json": runtime_binding_json,
                        "runtime_binding_hash": runtime_binding_hash,
                        "provider_operation_ref": provider_operation_ref,
                        "provider_operation_generation": (
                            provider_operation_generation
                        ),
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_deepfetch_sessions (root_session_ref, run_ref, "
                        "status, created_at, updated_at) VALUES (:root_session_ref, "
                        ":run_ref, 'open', :now, :now)"
                    ),
                    {
                        "root_session_ref": root_session_ref,
                        "run_ref": run_ref,
                        "now": now,
                    },
                )
            attempt_ref = new_ref("deepfetch_attempt")
            fence_ref = new_ref("deepfetch_fence")
            try:
                TypedExecutionFence(
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                ).validate()
            except ProviderSupervisorError as error:
                raise OwnerConflict("deepfetch_attempt_identity_invalid") from error
            connection.execute(
                text(
                    "INSERT INTO ar_deepfetch_attempts (attempt_ref, run_ref, "
                    "generation, root_session_ref, fence_ref, status, started_at) "
                    "VALUES (:attempt_ref, :run_ref, :generation, :root_session_ref, "
                    ":fence_ref, 'running', :now)"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "run_ref": run_ref,
                    "generation": generation,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'running', "
                    "current_attempt_ref = :attempt_ref, "
                    "attempt_generation = :generation, failure_code = NULL, "
                    "provider_operation_ref = :provider_operation_ref, "
                    "provider_operation_generation = :provider_operation_generation, "
                    "provider_operation_retry_permitted = 0, "
                    "reconciliation_attempt_count = :reconciliation_attempt_count, "
                    "next_reconcile_at = NULL, completed_at = NULL, "
                    "updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "generation": generation,
                    "provider_operation_ref": provider_operation_ref,
                    "provider_operation_generation": provider_operation_generation,
                    "reconciliation_attempt_count": reconciliation_attempt_count,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, "
                    "cycle_ref, epoch, status, attempt_ref, root_session_ref, "
                    "fence_ref, control_revision, safe_point_ref, "
                    "terminal_reason, cleanup_status, updated_at) VALUES "
                    "(:run_ref, 'deepfetch', :quest_ref, NULL, NULL, 'running', "
                    ":attempt_ref, :root_session_ref, :fence_ref, 1, NULL, NULL, "
                    "'none', :now) ON CONFLICT(run_ref) DO UPDATE SET status = "
                    "'running', quest_ref = COALESCE(ar_run_controls.quest_ref, "
                    "excluded.quest_ref), attempt_ref = excluded.attempt_ref, "
                    "root_session_ref = excluded.root_session_ref, fence_ref = "
                    "excluded.fence_ref, control_revision = control_revision + "
                    "1, safe_point_ref = NULL, terminal_reason = NULL, "
                    "cleanup_status = 'none', updated_at = excluded.updated_at"
                ),
                {
                    "run_ref": run_ref,
                    "quest_ref": request.quest_ref,
                    "attempt_ref": attempt_ref,
                    "root_session_ref": root_session_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                },
            )
            provider_unit_ref = "provider_unit_" + canonical_hash(
                {
                    "provider_operation_ref": provider_operation_ref,
                    "attempt_ref": attempt_ref,
                }
            )[:64]
            connection.execute(
                text(
                    "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
                    "attempt_ref, fence_ref, unit_kind, status, started_at, "
                    "completed_at) VALUES (:unit_ref, :operation_ref, :run_ref, "
                    ":attempt_ref, :fence_ref, 'deepfetch', 'active', :now, NULL)"
                ),
                {
                    "unit_ref": provider_unit_ref,
                    "operation_ref": provider_operation_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count + :active_increment, "
                    "deepfetch_run_count = deepfetch_run_count + :run_increment, "
                    "deepfetch_attempt_count = deepfetch_attempt_count + 1, "
                    "deepfetch_session_count = deepfetch_session_count + "
                    ":session_increment WHERE singleton = 'owner'"
                ),
                {
                    "active_increment": 1 if is_new or was_failed else 0,
                    "run_increment": 1 if is_new else 0,
                    "session_increment": 1 if is_new else 0,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_attempt_started",
                {
                    "request_ref": request.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "attempt_generation": generation,
                    "fence_ref": fence_ref,
                    "provider_unit_ref": provider_unit_ref,
                },
            )
            self._feed.record(
                connection,
                "agent_runtime.provider_unit_started",
                {
                    "unit_ref": provider_unit_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "unit_kind": "deepfetch",
                },
            )
        return (
            run_ref,
            root_session_ref,
            attempt_ref,
            generation,
            fence_ref,
            native_session_ref,
            provider_operation_ref,
        )

    def _complete_deepfetch_attempt(
        self,
        *,
        request: DeepFetchRunRequest,
        run_ref: str,
        attempt_ref: str,
        generation: int,
        fence_ref: str,
        runtime_binding_hash: str,
        result_payload: dict[str, object],
        result_hash: str,
    ) -> DeepFetchRun:
        native_session_ref = str(result_payload["native_session_ref"])
        receipt_ref = new_ref("ar_receipt")
        receipt_bindings = {
            "request_ref": request.request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": generation,
            "fence_ref": fence_ref,
            "native_session_ref": native_session_ref,
            "runtime_binding_hash": runtime_binding_hash,
            "result_hash": result_hash,
        }
        receipt_hash = _owner_receipt_hash(
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run_ref,
            receipt_bindings,
        )
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text("SELECT * FROM ar_deepfetch_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).one()
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_attempts WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).one()
            session = connection.execute(
                text("SELECT * FROM ar_deepfetch_sessions WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).one()
            if (
                run.status != "running"
                or run.current_attempt_ref != attempt_ref
                or int(run.attempt_generation) != generation
                or attempt.status != "running"
                or attempt.fence_ref != fence_ref
                or session.status != "open"
            ):
                raise OwnerConflict("deepfetch_attempt_fence_stale")
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            if session.native_session_ref is not None and (
                session.native_session_ref != native_session_ref
            ):
                raise OwnerConflict("deepfetch_native_session_changed")
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_sessions SET native_session_ref = "
                    ":native_session_ref, status = 'completed', updated_at = :now "
                    "WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "native_session_ref": native_session_ref,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'executed', "
                    "result_hash = :result_hash, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref AND status = 'running'"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "result_hash": result_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'executed', "
                    "result_json = :result_json, result_hash = :result_hash, "
                    "execution_receipt_ref = :receipt_ref, "
                    "execution_receipt_hash = :receipt_hash, failure_code = NULL, "
                    "provider_operation_retry_permitted = 0, "
                    "reconciliation_attempt_count = 0, next_reconcile_at = NULL, "
                    "updated_at = :now, completed_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "result_json": canonical_json(result_payload),
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                    "receipt_hash": receipt_hash,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET status = 'completed', "
                    "control_revision = control_revision + 1, terminal_reason = "
                    "NULL, cleanup_status = 'none', updated_at = :now WHERE "
                    "run_ref = :run_ref"
                ),
                {"now": now, "run_ref": run_ref},
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count - 1, "
                    "deepfetch_completed_run_count = "
                    "deepfetch_completed_run_count + 1 WHERE singleton = 'owner' "
                    "AND active_run_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_executed",
                {
                    "request_ref": request.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "result_hash": result_hash,
                    "receipt_ref": receipt_ref,
                },
            )
        completed = self.query_deepfetch_run(request.request_ref)
        assert completed is not None
        return completed

    def _fail_deepfetch_attempt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        generation: int,
        fence_ref: str,
        failure_code: str,
        provider_operation_retry_permitted: bool,
        native_session_ref: str | None,
    ) -> None:
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT status, current_attempt_ref, attempt_generation, "
                    "request_ref FROM ar_deepfetch_runs WHERE run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT status, fence_ref FROM ar_deepfetch_attempts WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).first()
            if (
                run is None
                or attempt is None
                or (
                    run.status != "running"
                    or run.current_attempt_ref != attempt_ref
                    or int(run.attempt_generation) != generation
                    or attempt.status != "running"
                    or attempt.fence_ref != fence_ref
                )
            ):
                return
            if native_session_ref is not None:
                session = connection.execute(
                    text(
                        "SELECT native_session_ref FROM ar_deepfetch_sessions "
                        "WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run_ref},
                ).one()
                if session.native_session_ref not in {None, native_session_ref}:
                    failure_code = "deepfetch_native_session_changed"
                    provider_operation_retry_permitted = False
                else:
                    connection.execute(
                        text(
                            "UPDATE ar_deepfetch_sessions SET native_session_ref = "
                            ":native_session_ref, updated_at = :now WHERE run_ref = "
                            ":run_ref"
                        ),
                        {
                            "run_ref": run_ref,
                            "native_session_ref": native_session_ref,
                            "now": now,
                        },
                    )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_attempts SET status = 'failed', "
                    "failure_code = :failure_code, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {
                    "attempt_ref": attempt_ref,
                    "failure_code": failure_code,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_deepfetch_runs SET status = 'failed', "
                    "provider_operation_retry_permitted = :retry_permitted, "
                    "next_reconcile_at = NULL, failure_code = :failure_code, "
                    "updated_at = :now, completed_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "run_ref": run_ref,
                    "failure_code": failure_code,
                    "retry_permitted": (1 if provider_operation_retry_permitted else 0),
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET status = :status, "
                    "control_revision = control_revision + 1, terminal_reason = "
                    ":failure_code, cleanup_status = 'none', updated_at = :now "
                    "WHERE run_ref = :run_ref"
                ),
                {
                    "status": (
                        "running"
                        if provider_operation_retry_permitted
                        else "terminated"
                    ),
                    "failure_code": failure_code,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_run_count = active_run_count - 1 WHERE "
                    "singleton = 'owner' AND active_run_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.deepfetch_failed",
                {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "reason_code": failure_code,
                },
            )

    def query_deepfetch_run(self, request_ref: str) -> DeepFetchRun | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT r.*, s.root_session_ref, s.native_session_ref, "
                    "s.status AS session_status, a.attempt_ref AS joined_attempt_ref, "
                    "a.generation AS joined_generation, a.fence_ref, "
                    "a.status AS attempt_status FROM ar_deepfetch_runs r "
                    "JOIN ar_deepfetch_sessions s ON s.run_ref = r.run_ref "
                    "LEFT JOIN ar_deepfetch_attempts a ON a.attempt_ref = "
                    "r.current_attempt_ref WHERE r.request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
        return None if row is None else _deepfetch_run_from_row(row)

    def cancel_deepfetch(self, request_ref: str) -> DeepFetchRun | None:
        provider_operation_ref: str | None = None
        now = time.time()
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_deepfetch_runs WHERE request_ref = :request_ref"
                ),
                {"request_ref": request_ref},
            ).first()
            if run is None or run.status in {"executed", "cancelled"}:
                provider_operation_ref = None
            else:
                # Persist the cancel Fence before invoking external code. A
                # provider callback may release an in-flight result immediately.
                provider_operation_ref = str(run.provider_operation_ref)
                was_active = run.status in {"admitted", "running"}
                if run.current_attempt_ref is not None:
                    connection.execute(
                        text(
                            "UPDATE ar_deepfetch_attempts SET status = 'cancelled', "
                            "failure_code = 'deepfetch_cancelled', completed_at = :now "
                            "WHERE attempt_ref = :attempt_ref AND status = 'running'"
                        ),
                        {"attempt_ref": run.current_attempt_ref, "now": now},
                    )
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_sessions SET status = 'cancelled', "
                        "updated_at = :now WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run.run_ref, "now": now},
                )
                connection.execute(
                    text(
                        "UPDATE ar_deepfetch_runs SET status = 'cancelled', "
                        "provider_operation_retry_permitted = 0, "
                        "next_reconcile_at = NULL, failure_code = "
                        "'deepfetch_cancelled', updated_at = :now, completed_at = :now "
                        "WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run.run_ref, "now": now},
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'terminated', "
                        "control_revision = control_revision + 1, terminal_reason = "
                        "'deepfetch_cancelled', cleanup_status = 'pending', "
                        "updated_at = :now WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": run.run_ref, "now": now},
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "active_run_count = active_run_count - :active_decrement WHERE "
                        "singleton = 'owner' AND active_run_count >= "
                        ":active_decrement"
                    ),
                    {"active_decrement": 1 if was_active else 0},
                )
                self._feed.record(
                    connection,
                    "agent_runtime.deepfetch_cancelled",
                    {"request_ref": request_ref, "run_ref": run.run_ref},
                )
        if provider_operation_ref is not None:
            with self._deepfetch_provider_lock:
                provider = self._deepfetch_providers.get(request_ref)
            if provider is not None:
                cancel_job = getattr(provider, "cancel_job", None)
                if callable(cancel_job):
                    try:
                        cancel_job(provider_operation_ref)
                    except Exception:
                        # The durable Fence is authoritative; provider shutdown
                        # is best-effort and cannot roll cancellation back.
                        pass
        return self.query_deepfetch_run(request_ref)

    def admit_idea_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: IdeaRuntimeBinding,
    ) -> IdeaStageRun:
        return self._admit_stage(
            request,
            idempotency_key,
            runtime_binding=runtime_binding,
            expected_stage="idea",
        )

    def admit_plan_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: PlanRuntimeBinding,
    ) -> PlanStageRun:
        return self._admit_stage(
            request,
            idempotency_key,
            runtime_binding=runtime_binding,
            expected_stage="plan",
        )

    def complete_bundle_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        target_graph_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion:
        return self._complete_stage_run(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            outcome_ref=target_graph_ref,
            decision_receipt=decision_receipt,
            idempotency_key=idempotency_key,
            expected_stage="bundle",
        )

    def admit_bundle_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: BundleRuntimeBinding,
    ) -> BundleStageRun:
        return self._admit_stage(
            request,
            idempotency_key,
            runtime_binding=runtime_binding,
            expected_stage="bundle",
        )

    def _admit_stage(
        self,
        request: StageRunRequest,
        idempotency_key: str,
        *,
        runtime_binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding,
        expected_stage: str,
    ) -> IdeaStageRun:
        _validate_stage_idempotency_key(idempotency_key)
        runtime_binding, runtime_binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding, stage=expected_stage)
        )
        command_kind = f"admit_{expected_stage}_stage"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "context_pack_ref": request.context_pack_ref,
                "context_pack_hash": request.context_pack_hash,
                "request_receipt": request.receipt.as_public_dict(),
                "runtime_binding": runtime_binding.as_dict(),
                "runtime_binding_hash": runtime_binding_hash,
            }
        )
        replay_ref = _query_stage_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        if replay_ref is not None:
            return self._query_stage_run_by_ref(replay_ref, expected_stage)
        if self._authorization_verifier is None:
            raise OwnerConflict("broad_research_authorization_verifier_unavailable")
        self._authorization_verifier.verify_broad_research_authorization(
            quest_ref=request.accepted_question.quest_ref
        )
        if self._stage_request_verifier is None:
            raise OwnerConflict("stage_request_verifier_unavailable")
        if (
            request.stage != expected_stage
            or request.epoch < 1
            or canonical_hash(request.context_pack) != request.context_pack_hash
        ):
            raise OwnerConflict("stage_run_request_invalid")
        with self._database.write() as connection:
            # Serialize the issuer-owned currentness check with the admission write.
            # Foreground control uses this same SQLite writer, so a switch can only
            # commit before verification (and be rejected here) or after the new Run
            # is visible (and include it in the fenced affected set).
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            replay_ref = _stage_command_replay(
                connection, idempotency_key, command_kind, command_hash
            )
            if replay_ref is not None:
                row = connection.execute(
                    text(
                        "SELECT request_ref FROM ar_stage_runs WHERE run_ref = :run_ref"
                    ),
                    {"run_ref": replay_ref},
                ).first()
                if row is None:
                    raise OwnerConflict("stage_command_result_missing")
                replay_request_ref = row.request_ref
            else:
                self._stage_request_verifier.verify_current_stage_run_request(
                    request_ref=request.request_ref,
                    cycle_ref=request.cycle_ref,
                    epoch=request.epoch,
                    context_pack_ref=request.context_pack_ref,
                    context_pack_hash=request.context_pack_hash,
                    receipt=request.receipt,
                )
                replay_request_ref = None
            if replay_request_ref is None:
                existing = connection.execute(
                    text(
                        "SELECT * FROM ar_stage_runs WHERE request_ref = :request_ref"
                    ),
                    {"request_ref": request.request_ref},
                ).first()
                if existing is not None:
                    if existing.admission_hash != command_hash:
                        raise OwnerConflict("stage_run_admission_conflict")
                    _record_stage_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        command_hash,
                        existing.run_ref,
                    )
                    replay_request_ref = existing.request_ref
                else:
                    now = time.time()
                    resumable = connection.execute(
                        text(
                            "SELECT r.*, c.status AS control_status, c.attempt_ref AS "
                            "control_attempt_ref, c.fence_ref AS control_fence_ref, "
                            "c.control_revision FROM ar_stage_runs r JOIN "
                            "ar_run_controls c ON c.run_ref = r.run_ref WHERE "
                            "r.cycle_ref = :cycle_ref AND r.stage = :stage AND "
                            "r.status != 'completed' AND c.status IN "
                            "('suspended_fenced', 'reconciliation_required') ORDER "
                            "BY r.updated_at DESC LIMIT 1"
                        ),
                        {"cycle_ref": request.cycle_ref, "stage": expected_stage},
                    ).first()
                    if resumable is not None and (
                        resumable.runtime_binding_hash == runtime_binding_hash
                        and request.epoch > int(resumable.epoch)
                    ):
                        old_request_ref = resumable.request_ref
                        old_epoch = int(resumable.epoch)
                        reuse_checkpoint = (
                            resumable.context_pack_ref == request.context_pack_ref
                            and resumable.context_pack_hash
                            == request.context_pack_hash
                            and resumable.runtime_binding_hash
                            == runtime_binding_hash
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_stage_runs SET request_ref = :request_ref, "
                                "epoch = :epoch, context_pack_ref = :context_pack_ref, "
                                "context_pack_hash = :context_pack_hash, "
                                "runtime_binding_json = :runtime_binding_json, "
                                "runtime_binding_hash = :runtime_binding_hash, "
                                "request_receipt_ref = :request_receipt_ref, "
                                "request_receipt_hash = :request_receipt_hash, "
                                "admission_key = :admission_key, admission_hash = "
                                ":admission_hash, updated_at = :now WHERE run_ref = "
                                ":run_ref"
                            ),
                            {
                                "request_ref": request.request_ref,
                                "epoch": request.epoch,
                                "context_pack_ref": request.context_pack_ref,
                                "context_pack_hash": request.context_pack_hash,
                                "runtime_binding_json": runtime_binding_json,
                                "runtime_binding_hash": runtime_binding_hash,
                                "request_receipt_ref": request.receipt.receipt_ref,
                                "request_receipt_hash": request.receipt.payload_hash,
                                "admission_key": idempotency_key,
                                "admission_hash": command_hash,
                                "now": now,
                                "run_ref": resumable.run_ref,
                            },
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_execution_fences SET status = 'rejected', "
                                "closed_at = COALESCE(closed_at, :now) WHERE fence_ref "
                                "= :fence_ref AND status IN ('current', 'submitted')"
                            ),
                            {"now": now, "fence_ref": resumable.control_fence_ref},
                        )
                        control = connection.execute(
                            text(
                                "SELECT * FROM ar_run_controls WHERE run_ref = "
                                ":run_ref"
                            ),
                            {"run_ref": resumable.run_ref},
                        ).one()
                        replacement = self._replace_fenced_managed_attempt(
                            connection,
                            control,
                            now,
                            reuse_checkpoint=reuse_checkpoint,
                        )
                        connection.execute(
                            text(
                                "UPDATE ar_run_controls SET epoch = :epoch, status = "
                                "'running', attempt_ref = :attempt_ref, fence_ref = "
                                ":fence_ref, control_revision = control_revision + 1, "
                                "safe_point_ref = NULL, terminal_reason = NULL, "
                                "cleanup_status = 'none', updated_at = :now WHERE "
                                "run_ref = :run_ref"
                            ),
                            {
                                "epoch": request.epoch,
                                "attempt_ref": replacement.attempt_ref,
                                "fence_ref": replacement.fence_ref,
                                "now": now,
                                "run_ref": resumable.run_ref,
                            },
                        )
                        rebind_ref = new_ref("stage_run_rebind")
                        connection.execute(
                            text(
                                "INSERT INTO ar_stage_run_rebindings (rebind_ref, "
                                "run_ref, cycle_ref, stage, old_request_ref, old_epoch, "
                                "new_request_ref, new_epoch, created_at) VALUES "
                                "(:rebind_ref, :run_ref, :cycle_ref, :stage, "
                                ":old_request_ref, :old_epoch, :new_request_ref, "
                                ":new_epoch, :now)"
                            ),
                            {
                                "rebind_ref": rebind_ref,
                                "run_ref": resumable.run_ref,
                                "cycle_ref": request.cycle_ref,
                                "stage": expected_stage,
                                "old_request_ref": old_request_ref,
                                "old_epoch": old_epoch,
                                "new_request_ref": request.request_ref,
                                "new_epoch": request.epoch,
                                "now": now,
                            },
                        )
                        _record_stage_command(
                            connection,
                            idempotency_key,
                            command_kind,
                            command_hash,
                            resumable.run_ref,
                        )
                        connection.execute(
                            text(
                                "UPDATE agent_runtime_state SET revision = revision + "
                                "1 WHERE singleton = 'owner'"
                            )
                        )
                        self._feed.record(
                            connection,
                            "agent_runtime.stage_run_rebound",
                            {
                                "rebind_ref": rebind_ref,
                                "run_ref": resumable.run_ref,
                                "old_request_ref": old_request_ref,
                                "request_ref": request.request_ref,
                                "old_epoch": old_epoch,
                                "epoch": request.epoch,
                                "attempt_ref": replacement.attempt_ref,
                                "fence_ref": replacement.fence_ref,
                            },
                        )
                        replay_request_ref = request.request_ref
                    else:
                        replay_request_ref = self._insert_new_stage_run(
                            connection,
                            request=request,
                            runtime_binding_json=runtime_binding_json,
                            runtime_binding_hash=runtime_binding_hash,
                            expected_stage=expected_stage,
                            idempotency_key=idempotency_key,
                            command_kind=command_kind,
                            command_hash=command_hash,
                            now=now,
                        )
        admitted = self._query_stage_run(replay_request_ref, expected_stage)
        if admitted is None:
            raise OwnerConflict("stage_run_missing_after_admission")
        return admitted

    def _insert_new_stage_run(
        self,
        connection,
        *,
        request: StageRunRequest,
        runtime_binding_json: str,
        runtime_binding_hash: str,
        expected_stage: str,
        idempotency_key: str,
        command_kind: str,
        command_hash: str,
        now: float,
    ) -> str:
        run_ref = new_ref(f"{expected_stage}_run")
        attempt_ref = new_ref(f"{expected_stage}_attempt")
        session_ref = new_ref(f"{expected_stage}_session")
        fence_ref = new_ref(f"{expected_stage}_fence")
        connection.execute(
            text(
                "INSERT INTO ar_stage_runs (run_ref, request_ref, cycle_ref, stage, "
                "epoch, context_pack_ref, context_pack_hash, runtime_binding_json, "
                "runtime_binding_hash, request_receipt_ref, request_receipt_hash, "
                "status, current_attempt_ref, root_session_ref, current_fence_ref, "
                "admission_key, admission_hash, created_at, updated_at) VALUES "
                "(:run_ref, :request_ref, :cycle_ref, :stage, :epoch, "
                ":context_pack_ref, :context_pack_hash, :runtime_binding_json, "
                ":runtime_binding_hash, :request_receipt_ref, "
                ":request_receipt_hash, 'running', :attempt_ref, :session_ref, "
                ":fence_ref, :admission_key, :admission_hash, :now, :now)"
            ),
            {
                "run_ref": run_ref,
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "context_pack_ref": request.context_pack_ref,
                "context_pack_hash": request.context_pack_hash,
                "runtime_binding_json": runtime_binding_json,
                "runtime_binding_hash": runtime_binding_hash,
                "request_receipt_ref": request.receipt.receipt_ref,
                "request_receipt_hash": request.receipt.payload_hash,
                "attempt_ref": attempt_ref,
                "session_ref": session_ref,
                "fence_ref": fence_ref,
                "admission_key": idempotency_key,
                "admission_hash": command_hash,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_stage_sessions (session_ref, run_ref, "
                "native_session_ref, status, created_at, updated_at) VALUES "
                "(:session_ref, :run_ref, NULL, 'active', :now, :now)"
            ),
            {"session_ref": session_ref, "run_ref": run_ref, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, "
                "root_session_ref, fence_ref, status, created_at) VALUES "
                "(:attempt_ref, :run_ref, 1, :session_ref, :fence_ref, 'running', "
                ":now)"
            ),
            {
                "attempt_ref": attempt_ref,
                "run_ref": run_ref,
                "session_ref": session_ref,
                "fence_ref": fence_ref,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, "
                "generation, status, issued_at) VALUES (:fence_ref, :run_ref, "
                ":attempt_ref, 1, 'current', :now)"
            ),
            {
                "fence_ref": fence_ref,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "now": now,
            },
        )
        _insert_provider_invocations(
            connection,
            request_ref=request.request_ref,
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            generation=1,
            root_session_ref=session_ref,
            fence_ref=fence_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            runtime_binding_hash=runtime_binding_hash,
            predecessor_attempt_ref=None,
            prepared_at=now,
            stage=expected_stage,
        )
        connection.execute(
            text(
                "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, "
                "cycle_ref, epoch, status, attempt_ref, root_session_ref, fence_ref, "
                "control_revision, safe_point_ref, terminal_reason, cleanup_status, "
                "updated_at) VALUES (:run_ref, :run_kind, :quest_ref, :cycle_ref, "
                ":epoch, 'running', :attempt_ref, :session_ref, :fence_ref, 1, NULL, "
                "NULL, 'none', :now)"
            ),
            {
                "run_ref": run_ref,
                "run_kind": f"{expected_stage}_stage",
                "quest_ref": request.accepted_question.quest_ref,
                "cycle_ref": request.cycle_ref,
                "epoch": request.epoch,
                "attempt_ref": attempt_ref,
                "session_ref": session_ref,
                "fence_ref": fence_ref,
                "now": now,
            },
        )
        _record_stage_command(
            connection,
            idempotency_key,
            command_kind,
            command_hash,
            run_ref,
        )
        connection.execute(
            text(
                "UPDATE agent_runtime_state SET revision = revision + 1, "
                "active_run_count = active_run_count + 1, "
                "stage_run_count = stage_run_count + 1, attempt_count = "
                "attempt_count + 1, session_count = session_count + 1 WHERE "
                "singleton = 'owner'"
            )
        )
        self._feed.record(
            connection,
            "agent_runtime.stage_run_admitted",
            {
                "request_ref": request.request_ref,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "root_session_ref": session_ref,
                "fence_ref": fence_ref,
                "stage": request.stage,
                "epoch": request.epoch,
                "runtime_binding_hash": runtime_binding_hash,
            },
        )
        return request.request_ref

    def query_idea_stage_run(self, request_ref: str) -> IdeaStageRun | None:
        return self._query_stage_run(request_ref, "idea")

    def query_plan_stage_run(self, request_ref: str) -> PlanStageRun | None:
        return self._query_stage_run(request_ref, "plan")

    def query_bundle_stage_run(self, request_ref: str) -> BundleStageRun | None:
        return self._query_stage_run(request_ref, "bundle")

    def _query_stage_run(
        self, request_ref: str, expected_stage: str
    ) -> IdeaStageRun | None:
        with self._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT runs.*, controls.status AS managed_status FROM "
                    "ar_stage_runs runs JOIN ar_run_controls controls ON "
                    "controls.run_ref = runs.run_ref WHERE runs.request_ref = "
                    ":request_ref AND runs.stage = :stage"
                ),
                {"request_ref": request_ref, "stage": expected_stage},
            ).first()
        if run is None:
            return None
        if run.managed_status not in {"running", "completed"}:
            run = SimpleNamespace(
                **{**dict(run._mapping), "status": run.managed_status}
            )
        return self._stage_run_from_row(run, expected_stage)

    def _idea_stage_run_from_row(self, run) -> IdeaStageRun:
        return self._stage_run_from_row(run, "idea")

    def _stage_run_from_row(self, run, expected_stage: str) -> IdeaStageRun:
        if run.stage != expected_stage:
            raise OwnerConflict("stage_run_integrity_invalid")
        runtime_binding = _runtime_binding_from_row(run)
        with self._database.read() as connection:
            session = connection.execute(
                text(
                    "SELECT * FROM ar_stage_sessions WHERE session_ref = "
                    ":session_ref AND run_ref = :run_ref"
                ),
                {"session_ref": run.root_session_ref, "run_ref": run.run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                    ":attempt_ref AND run_ref = :run_ref"
                ),
                {"attempt_ref": run.current_attempt_ref, "run_ref": run.run_ref},
            ).first()
            fence = connection.execute(
                text(
                    "SELECT * FROM ar_execution_fences WHERE fence_ref = "
                    ":fence_ref AND attempt_ref = :attempt_ref"
                ),
                {
                    "fence_ref": run.current_fence_ref,
                    "attempt_ref": run.current_attempt_ref,
                },
            ).first()
            predecessor = None
            if attempt is not None and attempt.predecessor_attempt_ref is not None:
                predecessor = connection.execute(
                    text(
                        "SELECT * FROM ar_stage_attempts WHERE attempt_ref = "
                        ":attempt_ref AND run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": attempt.predecessor_attempt_ref,
                        "run_ref": run.run_ref,
                    },
                ).first()
            technical_replacement = (
                None
                if attempt is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_stage_attempt_replacements WHERE "
                        "replacement_attempt_ref = :attempt_ref AND run_ref = "
                        ":run_ref"
                    ),
                    {"attempt_ref": attempt.attempt_ref, "run_ref": run.run_ref},
                ).first()
            )
        if (
            session is None
            or attempt is None
            or fence is None
            or (
                attempt.root_session_ref != session.session_ref
                or attempt.fence_ref != fence.fence_ref
                or int(attempt.generation) != int(fence.generation)
            )
        ):
            raise OwnerConflict("stage_run_integrity_invalid")
        with self._database.read() as connection:
            primary_invocation, review_invocation = _provider_invocations(
                connection, run, attempt, fence
            )
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=run.request_ref,
                cycle_ref=run.cycle_ref,
                epoch=int(run.epoch),
                context_pack_ref=run.context_pack_ref,
                context_pack_hash=run.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=run.request_receipt_ref,
                    subject_ref=run.request_ref,
                    payload_hash=run.request_receipt_hash,
                ),
            )
        execution = None
        if attempt.submission_ref is not None:
            execution = _attempt_execution(run, attempt, session)
            self._receipt_verifier.verify_attempt_execution_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                fence_ref=attempt.fence_ref,
                submission_ref=attempt.submission_ref,
                payload_hash=attempt.payload_hash,
                receipt=execution.receipt,
            )
        primary_draft = _primary_draft(run, attempt, session)
        if primary_draft is None:
            if primary_invocation.status != "prepared":
                raise OwnerConflict("idea_provider_invocation_invalid")
        elif (
            primary_invocation.status != "completed"
            or primary_invocation.response_hash
            != _primary_provider_response_hash(
                native_session_ref=primary_draft.native_session_ref,
                draft=primary_draft.draft,
                adapter_kind=primary_draft.adapter_kind,
                stage=run.stage,
            )
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        if execution is None:
            if review_invocation.status != "prepared":
                raise OwnerConflict("idea_provider_invocation_invalid")
        elif (
            primary_draft is None
            or review_invocation.status != "completed"
            or review_invocation.response_hash
            != _review_provider_response_hash(
                native_session_ref=execution.native_session_ref,
                reviewed_draft=execution.reviewed_draft,
                outcome=execution.outcome,
                review=execution.review,
                stage=run.stage,
            )
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        predecessor_execution = None
        rejection_receipt = None
        if predecessor is not None:
            predecessor_execution = _attempt_execution(run, predecessor, session)
            self._receipt_verifier.verify_attempt_execution_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=predecessor.attempt_ref,
                fence_ref=predecessor.fence_ref,
                submission_ref=predecessor.submission_ref,
                payload_hash=predecessor.payload_hash,
                receipt=predecessor_execution.receipt,
            )
            if (
                predecessor.status != "rejected"
                or predecessor.run_ref != run.run_ref
                or predecessor.root_session_ref != session.session_ref
                or int(attempt.generation) != int(predecessor.generation) + 1
                or predecessor.decision_receipt_ref is None
                or predecessor.decision_receipt_subject_ref is None
                or predecessor.decision_receipt_hash is None
            ):
                raise OwnerConflict("rejection_lineage_invalid")
            rejection_receipt = AcceptanceReceipt(
                issuer="research_graph",
                kind=_stage_decision_receipt_kind(expected_stage, accepted=False),
                receipt_ref=predecessor.decision_receipt_ref,
                subject_ref=predecessor.decision_receipt_subject_ref,
                payload_hash=predecessor.decision_receipt_hash,
            )
            self._verify_stage_decision(
                stage=expected_stage,
                request_ref=run.request_ref,
                submission_ref=predecessor.submission_ref,
                decision="rejected",
                outcome_ref=None,
                receipt=rejection_receipt,
            )
            if execution is not None:
                with self._database.read() as connection:
                    _successor_execution_lineage(
                        connection,
                        run,
                        attempt,
                        session,
                        native_session_ref=session.native_session_ref,
                        outcome=execution.outcome,
                    )
        completion = (
            _run_completion(run, attempt) if run.status == "completed" else None
        )
        if completion is not None:
            self._receipt_verifier.verify_run_completion_receipt(
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                outcome_ref=completion.outcome_ref,
                receipt=completion.receipt,
            )
        return IdeaStageRun(
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            cycle_ref=run.cycle_ref,
            stage=run.stage,
            epoch=int(run.epoch),
            status=run.status,
            attempt_ref=attempt.attempt_ref,
            attempt_generation=int(attempt.generation),
            root_session_ref=session.session_ref,
            native_session_ref=session.native_session_ref,
            runtime_binding=runtime_binding,
            runtime_binding_hash=run.runtime_binding_hash,
            fence_ref=fence.fence_ref,
            primary_invocation=primary_invocation,
            review_invocation=review_invocation,
            primary_draft=primary_draft,
            execution=execution,
            predecessor_execution=predecessor_execution,
            technical_predecessor_attempt_ref=(
                None
                if technical_replacement is None
                else technical_replacement.retired_attempt_ref
            ),
            rejection_receipt=rejection_receipt,
            completion=completion,
        )

    def record_idea_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft:
        return self._record_stage_primary_draft(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            draft=draft,
            adapter_kind=adapter_kind,
            idempotency_key=idempotency_key,
            expected_stage="idea",
        )

    def record_plan_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: PlanRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft:
        return self._record_stage_primary_draft(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            draft=draft,
            adapter_kind=adapter_kind,
            idempotency_key=idempotency_key,
            expected_stage="plan",
        )

    def record_bundle_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: BundleRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
    ) -> IdeaPrimaryDraft:
        return self._record_stage_primary_draft(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            draft=draft,
            adapter_kind=adapter_kind,
            idempotency_key=idempotency_key,
            expected_stage="bundle",
        )

    def _record_stage_primary_draft(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding,
        draft: dict[str, object],
        adapter_kind: str,
        idempotency_key: str,
        expected_stage: str,
    ) -> IdeaPrimaryDraft:
        """Bind the real native Session immediately after primary generation.

        Child-agent review is a later provider turn in this same native
        Session. Persisting this transport checkpoint prevents review failure
        or daemon restart from creating a second managed Session for the same
        Attempt/Fence.
        """

        _validate_stage_idempotency_key(idempotency_key)
        if (
            not native_session_ref
            or not isinstance(draft, dict)
            or not adapter_kind
            or len(adapter_kind) > 64
        ):
            raise OwnerConflict("idea_primary_draft_invalid")
        runtime_binding, _binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding, stage=expected_stage)
        )
        draft_json = canonical_json(draft)
        draft_hash = canonical_hash(draft)
        provider_response_hash = _primary_provider_response_hash(
            native_session_ref=native_session_ref,
            draft=draft,
            adapter_kind=adapter_kind,
            stage=expected_stage,
        )
        command_kind = f"record_{expected_stage}_primary_draft"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "native_session_ref": native_session_ref,
                "runtime_binding_hash": runtime_binding_hash,
                "draft_hash": draft_hash,
                "adapter_kind": adapter_kind,
                "provider_response_hash": provider_response_hash,
            }
        )
        _query_stage_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            run, attempt, session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            if replay_ref is None:
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
            if (
                run.stage != expected_stage
                or _runtime_binding_from_row(run) != runtime_binding
                or run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            primary_invocation = _complete_provider_invocation(
                connection,
                run,
                attempt,
                fence,
                phase="primary",
                response_hash=provider_response_hash,
            )
            existing = _primary_draft(run, attempt, session)
            if replay_ref is not None:
                if replay_ref != attempt_ref or existing is None:
                    raise OwnerConflict("stage_command_result_missing")
            elif existing is not None:
                if (
                    existing.native_session_ref != native_session_ref
                    or existing.draft_hash != draft_hash
                    or existing.adapter_kind != adapter_kind
                    or existing.runtime_binding_hash != runtime_binding_hash
                ):
                    raise OwnerConflict("idea_primary_draft_conflict")
                _record_stage_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    attempt_ref,
                )
            else:
                _require_current_fence(run, attempt, fence, "running", "current")
                if native_session_ref == session.session_ref or (
                    session.native_session_ref not in {None, native_session_ref}
                ):
                    raise OwnerConflict("native_session_conflict")
                conflicting_session = connection.execute(
                    text(
                        "SELECT session_ref FROM ar_stage_sessions WHERE "
                        "native_session_ref = :native_session_ref AND "
                        "session_ref != :session_ref LIMIT 1"
                    ),
                    {
                        "native_session_ref": native_session_ref,
                        "session_ref": session.session_ref,
                    },
                ).first()
                if conflicting_session is not None:
                    raise OwnerConflict("native_session_conflict")
                now = time.time()
                try:
                    connection.execute(
                        text(
                            "UPDATE ar_stage_sessions SET native_session_ref = "
                            ":native_session_ref, updated_at = :updated_at WHERE "
                            "session_ref = :session_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "updated_at": now,
                            "session_ref": session.session_ref,
                        },
                    )
                except IntegrityError as error:
                    raise OwnerConflict("native_session_conflict") from error
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET primary_draft_json = "
                        ":draft_json, primary_draft_hash = :draft_hash, "
                        "primary_adapter_kind = :adapter_kind, "
                        "primary_recorded_at = :recorded_at WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {
                        "draft_json": draft_json,
                        "draft_hash": draft_hash,
                        "adapter_kind": adapter_kind,
                        "recorded_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    attempt_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.primary_draft_recorded",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "native_session_ref": native_session_ref,
                        "runtime_binding_hash": runtime_binding_hash,
                        "draft_hash": draft_hash,
                        "invocation_ref": primary_invocation.invocation_ref,
                        "provider_response_hash": provider_response_hash,
                    },
                )
        current = self._query_stage_run(run.request_ref, expected_stage)
        if current is None or current.primary_draft is None:
            raise OwnerConflict("idea_primary_draft_missing_after_commit")
        return current.primary_draft

    def record_idea_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding,
        outcome: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution:
        return self._record_stage_attempt_execution(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            outcome=outcome,
            review=review,
            idempotency_key=idempotency_key,
            reviewed_draft=reviewed_draft,
            expected_stage="idea",
        )

    def record_plan_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: PlanRuntimeBinding,
        plan: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution:
        return self._record_stage_attempt_execution(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            outcome=plan,
            review=review,
            idempotency_key=idempotency_key,
            reviewed_draft=reviewed_draft,
            expected_stage="plan",
        )

    def record_bundle_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: BundleRuntimeBinding,
        target_plan: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None = None,
    ) -> AttemptExecution:
        return self._record_stage_attempt_execution(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            submission_ref=submission_ref,
            native_session_ref=native_session_ref,
            runtime_binding=runtime_binding,
            outcome=target_plan,
            review=review,
            idempotency_key=idempotency_key,
            reviewed_draft=reviewed_draft,
            expected_stage="bundle",
        )

    def _record_stage_attempt_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        native_session_ref: str,
        runtime_binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding,
        outcome: dict[str, object],
        review: dict[str, object],
        idempotency_key: str,
        reviewed_draft: dict[str, object] | None,
        expected_stage: str,
    ) -> AttemptExecution:
        _validate_stage_idempotency_key(idempotency_key)
        if not submission_ref or not native_session_ref:
            raise OwnerConflict("attempt_execution_identity_invalid")
        runtime_binding, _runtime_binding_json, runtime_binding_hash = (
            _validated_runtime_binding(runtime_binding, stage=expected_stage)
        )
        reviewer_agent_ref = _validate_attempt_review_for_write(
            review,
            native_session_ref=native_session_ref,
            stage=expected_stage,
        )
        reviewed_draft = _resolved_reviewed_draft(
            outcome,
            review,
            reviewed_draft,
        )
        outcome_material_hash = _stage_material_hash(expected_stage, outcome)
        payload = {
            "schema_ref": _stage_execution_schema(expected_stage),
            "outcome": outcome,
            "reviewed_draft": reviewed_draft,
            "review": review,
        }
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        provider_response_hash = _review_provider_response_hash(
            native_session_ref=native_session_ref,
            reviewed_draft=reviewed_draft,
            outcome=outcome,
            review=review,
            stage=expected_stage,
        )
        command_kind = f"record_{expected_stage}_attempt_execution"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "submission_ref": submission_ref,
                "native_session_ref": native_session_ref,
                "runtime_binding_hash": runtime_binding_hash,
                "payload_hash": payload_hash,
                "material_outcome_hash": outcome_material_hash,
                "provider_response_hash": provider_response_hash,
            }
        )
        _query_stage_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        with self._database.read() as connection:
            preview_run, preview_attempt, preview_session, preview_fence = (
                _load_stage_fence(connection, run_ref, attempt_ref, fence_ref)
            )
            if reviewer_agent_ref == preview_session.session_ref:
                raise OwnerConflict("attempt_review_independence_invalid")
            if (
                preview_run.stage != expected_stage
                or _runtime_binding_from_row(preview_run) != runtime_binding
                or preview_run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            primary_draft = _primary_draft(
                preview_run, preview_attempt, preview_session
            )
            if primary_draft is None:
                raise OwnerConflict("idea_primary_draft_required")
            if (
                primary_draft.native_session_ref != native_session_ref
                or primary_draft.draft_hash != canonical_hash(reviewed_draft)
            ):
                raise OwnerConflict("idea_primary_draft_conflict")
            primary_invocation, review_invocation = _provider_invocations(
                connection, preview_run, preview_attempt, preview_fence
            )
            if (
                primary_invocation.status != "completed"
                or primary_invocation.response_hash
                != _primary_provider_response_hash(
                    native_session_ref=primary_draft.native_session_ref,
                    draft=primary_draft.draft,
                    adapter_kind=primary_draft.adapter_kind,
                    stage=preview_run.stage,
                )
                or review_invocation.status == "completed"
                and review_invocation.response_hash != provider_response_hash
            ):
                raise OwnerConflict("idea_provider_invocation_invalid")
            _, predecessor_receipt, predecessor_submission_ref = (
                _successor_execution_lineage(
                    connection,
                    preview_run,
                    preview_attempt,
                    preview_session,
                    native_session_ref=native_session_ref,
                    outcome=outcome,
                )
            )
        if predecessor_receipt is not None:
            self._verify_stage_decision(
                stage=expected_stage,
                request_ref=preview_run.request_ref,
                submission_ref=predecessor_submission_ref,
                decision="rejected",
                outcome_ref=None,
                receipt=predecessor_receipt,
            )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is not None:
                replay_submission_ref = replay_ref
            else:
                replay_submission_ref = None
            run, attempt, session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            if replay_submission_ref is None:
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
            if reviewer_agent_ref == session.session_ref:
                raise OwnerConflict("attempt_review_independence_invalid")
            if (
                run.stage != expected_stage
                or _runtime_binding_from_row(run) != runtime_binding
                or run.runtime_binding_hash != runtime_binding_hash
            ):
                raise OwnerConflict("idea_runtime_binding_drift")
            current_primary_draft = _primary_draft(run, attempt, session)
            if current_primary_draft is None:
                raise OwnerConflict("idea_primary_draft_required")
            if (
                current_primary_draft.native_session_ref != native_session_ref
                or current_primary_draft.draft_hash != canonical_hash(reviewed_draft)
            ):
                raise OwnerConflict("idea_primary_draft_conflict")
            _complete_provider_invocation(
                connection,
                run,
                attempt,
                fence,
                phase="review",
                response_hash=provider_response_hash,
            )
            lineage, current_predecessor_receipt, current_predecessor_submission = (
                _successor_execution_lineage(
                    connection,
                    run,
                    attempt,
                    session,
                    native_session_ref=native_session_ref,
                    outcome=outcome,
                )
            )
            if (
                current_predecessor_receipt != predecessor_receipt
                or current_predecessor_submission != predecessor_submission_ref
            ):
                raise OwnerConflict("attempt_successor_lineage_changed")
            if replay_submission_ref is None and attempt.status != "running":
                if (
                    attempt.submission_ref == submission_ref
                    and attempt.payload_hash == payload_hash
                    and session.native_session_ref == native_session_ref
                ):
                    replay_submission_ref = submission_ref
                    _record_stage_command(
                        connection,
                        idempotency_key,
                        command_kind,
                        command_hash,
                        submission_ref,
                    )
                else:
                    raise OwnerConflict("attempt_fence_stale")
            if replay_submission_ref is None:
                _require_current_fence(run, attempt, fence, "running", "current")
                if session.native_session_ref not in {None, native_session_ref}:
                    raise OwnerConflict("native_session_conflict")
                conflicting_session = connection.execute(
                    text(
                        "SELECT session_ref FROM ar_stage_sessions WHERE "
                        "native_session_ref = :native_session_ref AND "
                        "session_ref != :session_ref LIMIT 1"
                    ),
                    {
                        "native_session_ref": native_session_ref,
                        "session_ref": session.session_ref,
                    },
                ).first()
                if conflicting_session is not None:
                    raise OwnerConflict("native_session_conflict")
                now = time.time()
                receipt_ref = new_ref("ar_execution_receipt")
                bindings = {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "submission_ref": submission_ref,
                    "native_session_ref": native_session_ref,
                    "runtime_binding_hash": run.runtime_binding_hash,
                    "payload_hash": payload_hash,
                    "material_outcome_hash": outcome_material_hash,
                    **lineage,
                }
                receipt_hash = _owner_receipt_hash(
                    _stage_execution_receipt_kind(expected_stage),
                    submission_ref,
                    bindings,
                )
                try:
                    connection.execute(
                        text(
                            "UPDATE ar_stage_sessions SET native_session_ref = "
                            ":native_session_ref, updated_at = :updated_at WHERE "
                            "session_ref = :session_ref"
                        ),
                        {
                            "native_session_ref": native_session_ref,
                            "updated_at": now,
                            "session_ref": session.session_ref,
                        },
                    )
                except IntegrityError as error:
                    raise OwnerConflict("native_session_conflict") from error
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'executed', "
                        "submission_ref = :submission_ref, payload_json = "
                        ":payload_json, payload_hash = :payload_hash, "
                        "material_outcome_hash = :material_outcome_hash, "
                        "execution_receipt_ref = :receipt_ref, "
                        "execution_receipt_hash = :receipt_hash, "
                        "predecessor_outcome_hash = :predecessor_outcome_hash, "
                        "predecessor_material_outcome_hash = "
                        ":predecessor_material_outcome_hash, "
                        "predecessor_rejection_receipt_ref = "
                        ":predecessor_rejection_receipt_ref, "
                        "predecessor_rejection_receipt_subject_ref = "
                        ":predecessor_rejection_receipt_subject_ref, "
                        "predecessor_rejection_receipt_hash = "
                        ":predecessor_rejection_receipt_hash, executed_at = "
                        ":executed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "submission_ref": submission_ref,
                        "payload_json": payload_json,
                        "payload_hash": payload_hash,
                        "material_outcome_hash": outcome_material_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        **lineage,
                        "executed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'submitted' "
                        "WHERE fence_ref = :fence_ref"
                    ),
                    {"fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'awaiting_acceptance', "
                        "updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {"updated_at": now, "run_ref": run_ref},
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    submission_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.attempt_executed",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "submission_ref": submission_ref,
                        "payload_hash": payload_hash,
                        "material_outcome_hash": outcome_material_hash,
                        "runtime_binding_hash": runtime_binding_hash,
                        "provider_response_hash": provider_response_hash,
                        "receipt_ref": receipt_ref,
                    },
                )
                replay_submission_ref = submission_ref
        executed = self._query_stage_attempt_execution(
            replay_submission_ref,
            expected_stage,
        )
        if executed is None:
            raise OwnerConflict("attempt_execution_missing_after_commit")
        return executed

    def query_idea_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None:
        return self._query_stage_attempt_execution(submission_ref, "idea")

    def query_plan_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None:
        return self._query_stage_attempt_execution(submission_ref, "plan")

    def query_bundle_attempt_execution(
        self, submission_ref: str
    ) -> AttemptExecution | None:
        return self._query_stage_attempt_execution(submission_ref, "bundle")

    def record_bundle_dispatch_decision(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        native_session_ref: str,
        graph_ref: str,
        generation: int,
        frontier: tuple[dict[str, object], ...],
        state: dict[str, object],
        action: str,
        selected_target_ref: str | None,
        rationale: str,
        idempotency_key: str,
    ) -> BundleDispatchDecision:
        """Persist one rolling choice made by the current Bundle root Session."""

        _validate_stage_idempotency_key(idempotency_key)
        if (
            not native_session_ref
            or not graph_ref
            or isinstance(generation, bool)
            or generation < 1
            or len(frontier) > 64
            or not isinstance(state, dict)
            or not isinstance(rationale, str)
            or not rationale.strip()
            or len(rationale) > 512
            or any(not isinstance(item, dict) for item in frontier)
        ):
            raise OwnerConflict("bundle_dispatch_decision_invalid")
        selected = [
            item for item in frontier if item.get("target_ref") == selected_target_ref
        ]
        if action != "dispatch":
            raise OwnerConflict("bundle_dispatch_authority_missing")
        if (
            action not in {"dispatch", "wait", "replan_required"}
            or (
                action == "dispatch" and (not selected_target_ref or len(selected) != 1)
            )
            or (action != "dispatch" and selected_target_ref is not None)
        ):
            raise OwnerConflict("bundle_dispatch_target_not_in_frontier")
        frontier_json = canonical_json(list(frontier))
        frontier_hash = canonical_hash(list(frontier))
        state_json = canonical_json(state)
        state_hash = canonical_hash(state)
        command_hash = canonical_hash(
            {
                "command": "record_bundle_dispatch_decision",
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "native_session_ref": native_session_ref,
                "graph_ref": graph_ref,
                "generation": generation,
                "frontier_hash": frontier_hash,
                "state_hash": state_hash,
                "action": action,
                "selected_target_ref": selected_target_ref,
                "rationale": rationale,
            }
        )
        with self._database.write() as connection:
            existing = connection.execute(
                text(
                    "SELECT * FROM ar_bundle_dispatch_decisions WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if existing.request_hash != command_hash:
                    raise OwnerConflict("idempotency_conflict")
                decision_ref = existing.decision_ref
            else:
                run, attempt, session, fence = _load_stage_fence(
                    connection, run_ref, attempt_ref, fence_ref
                )
                _require_current_fence(run, attempt, fence, "executed", "submitted")
                if (
                    run.stage != "bundle"
                    or session.native_session_ref != native_session_ref
                ):
                    raise OwnerConflict("bundle_dispatch_fence_invalid")
                verifier = self._target_graph_verifier
                if verifier is None:
                    raise OwnerConflict("target_graph_verifier_unavailable")
                verifier.verify_bundle_dispatch_frontier(
                    request_ref=run.request_ref,
                    run_ref=run_ref,
                    graph_ref=graph_ref,
                    frontier=frontier,
                )
                expected_generation = int(
                    connection.execute(
                        text(
                            "SELECT COALESCE(MAX(generation), 0) + 1 FROM "
                            "ar_bundle_dispatch_decisions WHERE run_ref = :run_ref"
                        ),
                        {"run_ref": run_ref},
                    ).scalar_one()
                )
                if generation != expected_generation:
                    raise OwnerConflict("bundle_dispatch_generation_stale")
                decision = {
                    "schema_ref": "meta-research/bundle-dispatch-decision/v1",
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "native_session_ref": native_session_ref,
                    "graph_ref": graph_ref,
                    "generation": generation,
                    "frontier_hash": frontier_hash,
                    "state_hash": state_hash,
                    "action": action,
                    "selected_target_ref": selected_target_ref,
                    "rationale": rationale,
                }
                decision_hash = canonical_hash(decision)
                decision_ref = new_ref("bundle_dispatch")
                receipt_ref = new_ref("ar_bundle_dispatch_receipt")
                bindings = {
                    **decision,
                    "decision_hash": decision_hash,
                }
                receipt_hash = _owner_receipt_hash(
                    BUNDLE_DISPATCH_RECEIPT_KIND, decision_ref, bindings
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO ar_bundle_dispatch_decisions "
                        "(decision_ref, run_ref, attempt_ref, fence_ref, "
                        "native_session_ref, graph_ref, generation, frontier_json, "
                        "frontier_hash, state_json, state_hash, "
                        "action, selected_target_ref, rationale, decision_hash, "
                        "idempotency_key, request_hash, receipt_ref, receipt_hash, "
                        "created_at) VALUES (:decision_ref, :run_ref, "
                        ":attempt_ref, :fence_ref, :native_session_ref, :graph_ref, "
                        ":generation, :frontier_json, :frontier_hash, :state_json, "
                        ":state_hash, :action, :selected_target_ref, :rationale, "
                        ":decision_hash, :idempotency_key, :request_hash, "
                        ":receipt_ref, :receipt_hash, :created_at)"
                    ),
                    {
                        **bindings,
                        "decision_ref": decision_ref,
                        "frontier_json": frontier_json,
                        "state_json": state_json,
                        "idempotency_key": idempotency_key,
                        "request_hash": command_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.bundle_target_dispatched",
                    {
                        "decision_ref": decision_ref,
                        "run_ref": run_ref,
                        "graph_ref": graph_ref,
                        "generation": generation,
                        "action": action,
                        "selected_target_ref": selected_target_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
        decisions = self.query_bundle_dispatch_decisions(run_ref)
        matching = tuple(
            decision for decision in decisions if decision.decision_ref == decision_ref
        )
        if len(matching) != 1:
            raise OwnerConflict("bundle_dispatch_decision_missing_after_commit")
        return matching[0]

    def query_bundle_dispatch_decisions(
        self, run_ref: str
    ) -> tuple[BundleDispatchDecision, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_bundle_dispatch_decisions WHERE run_ref = "
                    ":run_ref ORDER BY generation"
                ),
                {"run_ref": run_ref},
            ).all()
        return tuple(_bundle_dispatch_decision(row) for row in rows)

    def _query_stage_attempt_execution(
        self, submission_ref: str, expected_stage: str
    ) -> AttemptExecution | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT a.*, r.request_ref, r.cycle_ref, r.epoch, "
                    "r.context_pack_ref, r.context_pack_hash, "
                    "r.runtime_binding_json, r.runtime_binding_hash, "
                    "r.stage, r.request_receipt_ref, r.request_receipt_hash, "
                    "r.admission_hash, "
                    "s.native_session_ref FROM ar_stage_attempts a "
                    "JOIN ar_stage_runs r ON r.run_ref = a.run_ref "
                    "JOIN ar_stage_sessions s ON s.session_ref = a.root_session_ref "
                    "WHERE a.submission_ref = :submission_ref"
                ),
                {"submission_ref": submission_ref},
            ).first()
            if row is None:
                return None
            if row.stage != expected_stage:
                return None
            executed = _attempt_execution(row, row, row)
            _verify_provider_execution_chain(connection, row, executed)
        self._receipt_verifier.verify_attempt_execution_receipt(
            request_ref=row.request_ref,
            run_ref=row.run_ref,
            attempt_ref=row.attempt_ref,
            fence_ref=row.fence_ref,
            submission_ref=row.submission_ref,
            payload_hash=row.payload_hash,
            receipt=executed.receipt,
        )
        return executed

    def continue_after_idea_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> IdeaStageRun:
        return self._continue_after_stage_rejection(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            decision_receipt=decision_receipt,
            idempotency_key=idempotency_key,
            expected_stage="idea",
        )

    def continue_after_plan_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> PlanStageRun:
        return self._continue_after_stage_rejection(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            decision_receipt=decision_receipt,
            idempotency_key=idempotency_key,
            expected_stage="plan",
        )

    def _continue_after_stage_rejection(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
        expected_stage: str,
    ) -> IdeaStageRun:
        _validate_stage_idempotency_key(idempotency_key)
        command_kind = f"continue_after_{expected_stage}_rejection"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "decision_receipt": decision_receipt.as_public_dict(),
            }
        )
        replay = _query_stage_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        if replay is not None:
            return self._query_stage_run_by_ref(run_ref, expected_stage)
        with self._database.read() as connection:
            run, attempt, _session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            _require_current_fence(run, attempt, fence, "executed", "submitted")
            if run.stage != expected_stage:
                raise OwnerConflict("stage_run_integrity_invalid")
            request_ref = run.request_ref
            submission_ref = attempt.submission_ref
        self._verify_stage_decision(
            stage=expected_stage,
            request_ref=request_ref,
            submission_ref=submission_ref,
            decision="rejected",
            outcome_ref=None,
            receipt=decision_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is None:
                run, attempt, session, fence = _load_stage_fence(
                    connection, run_ref, attempt_ref, fence_ref
                )
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
                _require_current_fence(run, attempt, fence, "executed", "submitted")
                if session.native_session_ref is None:
                    raise OwnerConflict("native_session_missing")
                now = time.time()
                next_generation = int(attempt.generation) + 1
                successor_ref = new_ref(f"{expected_stage}_attempt")
                successor_fence_ref = new_ref(f"{expected_stage}_fence")
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'rejected', "
                        "decision_receipt_ref = :decision_receipt_ref, "
                        "decision_receipt_subject_ref = "
                        ":decision_receipt_subject_ref, "
                        "decision_receipt_hash = :decision_receipt_hash, "
                        "closed_at = :closed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "decision_receipt_subject_ref": decision_receipt.subject_ref,
                        "decision_receipt_hash": decision_receipt.payload_hash,
                        "closed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'rejected', "
                        "closed_at = :closed_at WHERE fence_ref = :fence_ref"
                    ),
                    {"closed_at": now, "fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, "
                        "generation, root_session_ref, fence_ref, "
                        "predecessor_attempt_ref, status, created_at) VALUES "
                        "(:attempt_ref, :run_ref, :generation, :session_ref, "
                        ":fence_ref, :predecessor_attempt_ref, 'running', "
                        ":created_at)"
                    ),
                    {
                        "attempt_ref": successor_ref,
                        "run_ref": run_ref,
                        "generation": next_generation,
                        "session_ref": session.session_ref,
                        "fence_ref": successor_fence_ref,
                        "predecessor_attempt_ref": attempt_ref,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_execution_fences (fence_ref, run_ref, "
                        "attempt_ref, generation, status, issued_at) VALUES "
                        "(:fence_ref, :run_ref, :attempt_ref, :generation, "
                        "'current', :issued_at)"
                    ),
                    {
                        "fence_ref": successor_fence_ref,
                        "run_ref": run_ref,
                        "attempt_ref": successor_ref,
                        "generation": next_generation,
                        "issued_at": now,
                    },
                )
                _insert_provider_invocations(
                    connection,
                    request_ref=run.request_ref,
                    run_ref=run_ref,
                    attempt_ref=successor_ref,
                    generation=next_generation,
                    root_session_ref=session.session_ref,
                    fence_ref=successor_fence_ref,
                    context_pack_ref=run.context_pack_ref,
                    context_pack_hash=run.context_pack_hash,
                    runtime_binding_hash=run.runtime_binding_hash,
                    predecessor_attempt_ref=attempt_ref,
                    prepared_at=now,
                    stage=expected_stage,
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'running', "
                        "current_attempt_ref = :attempt_ref, current_fence_ref = "
                        ":fence_ref, updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": successor_ref,
                        "fence_ref": successor_fence_ref,
                        "updated_at": now,
                        "run_ref": run_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'running', "
                        "attempt_ref = :attempt_ref, fence_ref = :fence_ref, "
                        "control_revision = control_revision + 1, "
                        "safe_point_ref = NULL, terminal_reason = NULL, "
                        "cleanup_status = 'none', updated_at = :updated_at "
                        "WHERE run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": successor_ref,
                        "fence_ref": successor_fence_ref,
                        "updated_at": now,
                        "run_ref": run_ref,
                    },
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    successor_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "attempt_count = attempt_count + 1 WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.attempt_rejected",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "successor_attempt_ref": successor_ref,
                        "root_session_ref": session.session_ref,
                        "native_session_ref": session.native_session_ref,
                        "fence_ref": successor_fence_ref,
                    },
                )
        return self._query_stage_run_by_ref(run_ref, expected_stage)

    def complete_idea_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        outcome_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion:
        return self._complete_stage_run(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            outcome_ref=outcome_ref,
            decision_receipt=decision_receipt,
            idempotency_key=idempotency_key,
            expected_stage="idea",
        )

    def complete_plan_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        formal_plan_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
    ) -> RunCompletion:
        return self._complete_stage_run(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            fence_ref=fence_ref,
            outcome_ref=formal_plan_ref,
            decision_receipt=decision_receipt,
            idempotency_key=idempotency_key,
            expected_stage="plan",
        )

    def _complete_stage_run(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        outcome_ref: str,
        decision_receipt: AcceptanceReceipt,
        idempotency_key: str,
        expected_stage: str,
    ) -> RunCompletion:
        _validate_stage_idempotency_key(idempotency_key)
        if not outcome_ref:
            raise OwnerConflict("outcome_ref_invalid")
        command_kind = f"complete_{expected_stage}_run"
        command_hash = canonical_hash(
            {
                "command": command_kind,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "outcome_ref": outcome_ref,
                "decision_receipt": decision_receipt.as_public_dict(),
            }
        )
        replay = _query_stage_command(
            self._database,
            idempotency_key,
            command_kind,
            command_hash,
        )
        if replay is not None:
            completed = self._query_stage_run_completion(run_ref, expected_stage)
            if completed is None:
                raise OwnerConflict("run_completion_missing")
            return completed
        with self._database.read() as connection:
            run, attempt, _session, fence = _load_stage_fence(
                connection, run_ref, attempt_ref, fence_ref
            )
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            _require_current_fence(run, attempt, fence, "executed", "submitted")
            if run.stage != expected_stage:
                raise OwnerConflict("stage_run_integrity_invalid")
            request_ref = run.request_ref
            submission_ref = attempt.submission_ref
        self._verify_stage_decision(
            stage=expected_stage,
            request_ref=request_ref,
            submission_ref=submission_ref,
            decision="accepted",
            outcome_ref=outcome_ref,
            receipt=decision_receipt,
        )
        with self._database.write() as connection:
            replay_ref = _stage_command_replay(
                connection,
                idempotency_key,
                command_kind,
                command_hash,
            )
            if replay_ref is None:
                run, attempt, session, fence = _load_stage_fence(
                    connection, run_ref, attempt_ref, fence_ref
                )
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
                _require_current_fence(run, attempt, fence, "executed", "submitted")
                now = time.time()
                receipt_ref = new_ref("ar_completion_receipt")
                bindings = {
                    "request_ref": run.request_ref,
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "runtime_binding_hash": run.runtime_binding_hash,
                    "outcome_ref": outcome_ref,
                    "decision_receipt_ref": decision_receipt.receipt_ref,
                    "decision_receipt_subject_ref": decision_receipt.subject_ref,
                    "decision_receipt_hash": decision_receipt.payload_hash,
                }
                receipt_hash = _owner_receipt_hash(
                    RUN_COMPLETION_RECEIPT_KIND, run_ref, bindings
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_attempts SET status = 'completed', "
                        "decision_receipt_ref = :decision_receipt_ref, "
                        "decision_receipt_subject_ref = "
                        ":decision_receipt_subject_ref, "
                        "decision_receipt_hash = :decision_receipt_hash, "
                        "closed_at = :closed_at WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "decision_receipt_subject_ref": decision_receipt.subject_ref,
                        "decision_receipt_hash": decision_receipt.payload_hash,
                        "closed_at": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_execution_fences SET status = 'completed', "
                        "closed_at = :closed_at WHERE fence_ref = :fence_ref"
                    ),
                    {"closed_at": now, "fence_ref": fence_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_sessions SET status = 'completed', "
                        "updated_at = :updated_at WHERE session_ref = :session_ref"
                    ),
                    {"updated_at": now, "session_ref": session.session_ref},
                )
                connection.execute(
                    text(
                        "UPDATE ar_stage_runs SET status = 'completed', "
                        "completion_receipt_ref = :receipt_ref, "
                        "completion_receipt_hash = :receipt_hash, outcome_ref = "
                        ":outcome_ref, updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "outcome_ref": outcome_ref,
                        "updated_at": now,
                        "run_ref": run_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'completed', "
                        "control_revision = control_revision + 1, "
                        "terminal_reason = NULL, cleanup_status = 'none', "
                        "updated_at = :updated_at WHERE run_ref = :run_ref"
                    ),
                    {"updated_at": now, "run_ref": run_ref},
                )
                _record_stage_command(
                    connection,
                    idempotency_key,
                    command_kind,
                    command_hash,
                    run_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "active_run_count = active_run_count - 1, "
                        "completed_run_count = completed_run_count + 1 "
                        "WHERE singleton = 'owner' AND active_run_count > 0"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.stage_run_completed",
                    {
                        "request_ref": run.request_ref,
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "outcome_ref": outcome_ref,
                        "decision_receipt_ref": decision_receipt.receipt_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
        completed = self._query_stage_run_completion(run_ref, expected_stage)
        if completed is None:
            raise OwnerConflict("run_completion_missing_after_commit")
        return completed

    def query_idea_run_completion(self, run_ref: str) -> RunCompletion | None:
        return self._query_stage_run_completion(run_ref, "idea")

    def query_plan_run_completion(self, run_ref: str) -> RunCompletion | None:
        return self._query_stage_run_completion(run_ref, "plan")

    def query_bundle_run_completion(self, run_ref: str) -> RunCompletion | None:
        return self._query_stage_run_completion(run_ref, "bundle")

    def _query_stage_run_completion(
        self, run_ref: str, expected_stage: str
    ) -> RunCompletion | None:
        with self._database.read() as connection:
            run = connection.execute(
                text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            if run is None or run.stage != expected_stage or run.status != "completed":
                return None
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": run.current_attempt_ref},
            ).first()
        if attempt is None:
            raise OwnerConflict("run_completion_invalid")
        completed = _run_completion(run, attempt)
        self._receipt_verifier.verify_run_completion_receipt(
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            attempt_ref=attempt.attempt_ref,
            outcome_ref=completed.outcome_ref,
            receipt=completed.receipt,
        )
        return completed

    def _query_idea_run_by_ref(self, run_ref: str) -> IdeaStageRun:
        return self._query_stage_run_by_ref(run_ref, "idea")

    def _query_stage_run_by_ref(
        self, run_ref: str, expected_stage: str
    ) -> IdeaStageRun:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.*, controls.status AS managed_status FROM "
                    "ar_stage_runs runs JOIN ar_run_controls controls ON "
                    "controls.run_ref = runs.run_ref WHERE runs.run_ref = :run_ref"
                ),
                {"run_ref": run_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_not_found")
        if row.managed_status not in {"running", "completed"}:
            row = SimpleNamespace(
                **{**dict(row._mapping), "status": row.managed_status}
            )
        return self._stage_run_from_row(row, expected_stage)

    def _verify_stage_decision(
        self,
        *,
        stage: str,
        request_ref: str,
        submission_ref: str | None,
        decision: str,
        outcome_ref: str | None,
        receipt: AcceptanceReceipt,
    ) -> None:
        if stage == "idea":
            if self._outcome_verifier is None:
                raise OwnerConflict("idea_outcome_verifier_unavailable")
            self._outcome_verifier.verify_idea_outcome_decision(
                request_ref=request_ref,
                submission_ref=submission_ref,
                decision=decision,
                outcome_ref=outcome_ref,
                receipt=receipt,
            )
            return
        if stage == "plan":
            if self._formal_plan_verifier is None:
                raise OwnerConflict("formal_plan_verifier_unavailable")
            self._formal_plan_verifier.verify_formal_plan_decision(
                request_ref=request_ref,
                submission_ref=submission_ref,
                decision=decision,
                formal_plan_ref=outcome_ref,
                receipt=receipt,
            )
            return
        if stage == "bundle":
            if self._target_graph_verifier is None:
                raise OwnerConflict("target_graph_verifier_unavailable")
            if decision != "accepted" or outcome_ref is None:
                raise OwnerConflict("target_graph_decision_invalid")
            self._target_graph_verifier.verify_target_graph_receipt(
                request_ref=request_ref,
                run_ref=self._bundle_run_ref_for_request(request_ref),
                graph_ref=outcome_ref,
                receipt=receipt,
            )
            return
        raise OwnerConflict("stage_run_integrity_invalid")

    def _bundle_run_ref_for_request(self, request_ref: str) -> str:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT run_ref FROM ar_stage_runs WHERE request_ref = "
                    ":request_ref AND stage = 'bundle'"
                ),
                {"request_ref": request_ref},
            ).first()
        if row is None:
            raise OwnerConflict("stage_run_not_found")
        return cast(str, row.run_ref)

    def _recover_interrupted_experiments(self) -> None:
        """Fence interrupted local providers and retain the same domain identities."""

        with self._database.write() as connection:
            candidates = connection.execute(
                text(
                    "SELECT r.* FROM ar_experiment_runs AS r JOIN "
                    "ar_run_controls AS c ON c.run_ref = r.run_ref WHERE "
                    "r.status IN ('admitted', 'running') AND c.status = 'running' "
                    "ORDER BY r.run_ref"
                )
            ).all()
            runs = []
            reconciliation_pending = {
                "status": "reconciliation_pending",
                "reason": {"code": "experiment_provider_reconciliation_pending"},
            }
            for run in candidates:
                if _prepared_control_affects_run(connection, str(run.run_ref)):
                    continue
                if run.status == "running":
                    runs.append(run)
                    continue
                last_status = connection.execute(
                    text(
                        "SELECT payload_json FROM ar_experiment_events WHERE "
                        "run_ref = :run_ref AND attempt_ref = :attempt_ref AND "
                        "kind = 'status' ORDER BY sequence DESC LIMIT 1"
                    ),
                    {
                        "run_ref": run.run_ref,
                        "attempt_ref": run.attempt_ref,
                    },
                ).first()
                if (
                    last_status is not None
                    and last_status.payload_json
                    == canonical_json(reconciliation_pending)
                ):
                    # The previous daemon already delivered part of this
                    # provider ledger to the current Attempt. Its in-memory
                    # cursor is gone, so fence the partial Attempt before a new
                    # provider instance reconciles the same durable operation.
                    runs.append(run)
            for run in runs:
                now = time.time()
                retired_reason = (
                    "daemon_restart"
                    if run.status == "running"
                    else "daemon_restart_reconciliation"
                )
                connection.execute(
                    text(
                        "UPDATE ar_experiment_attempts SET status = 'retired', "
                        "retired_reason = :retired_reason, completed_at = :now "
                        "WHERE attempt_ref = :attempt_ref AND status = :status"
                    ),
                    {
                        "now": now,
                        "attempt_ref": run.attempt_ref,
                        "retired_reason": retired_reason,
                        "status": run.status,
                    },
                )
                attempt_ref, fence_ref = _insert_replacement_attempt(
                    connection,
                    run.run_ref,
                    int(run.attempt_generation) + 1,
                    run.root_session_ref,
                    now,
                )
                connection.execute(
                    text(
                        "UPDATE ar_experiment_runs SET status = 'admitted', "
                        "attempt_ref = :attempt_ref, attempt_generation = "
                        "attempt_generation + 1, fence_ref = :fence_ref, updated_at = "
                        ":now WHERE run_ref = :run_ref AND status = :status"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "now": now,
                        "run_ref": run.run_ref,
                        "status": run.status,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'running', "
                        "attempt_ref = :attempt_ref, fence_ref = :fence_ref, "
                        "control_revision = control_revision + 1, updated_at = "
                        ":now WHERE run_ref = :run_ref"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "now": now,
                        "run_ref": run.run_ref,
                    },
                )
                self._feed.record(
                    connection,
                    "agent_runtime.experiment_recovered",
                    {
                        "run_ref": run.run_ref,
                        "retired_attempt_ref": run.attempt_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                    },
                )
            if runs:
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "experiment_attempt_count = experiment_attempt_count + "
                        ":count WHERE singleton = 'owner'"
                    ),
                    {"count": len(runs)},
                )

    def _recover_interrupted_writing(self) -> None:
        """Rotate a crashed Writing Fence while carrying its durable checkpoint."""

        with self._database.write() as connection:
            runs = connection.execute(
                text(
                    "SELECT run.*, attempt.provider_job_ref AS provider_job_ref FROM "
                    "ar_writing_runs AS run JOIN "
                    "ar_writing_attempts AS attempt ON attempt.attempt_ref = "
                    "run.attempt_ref WHERE run.status = 'active' AND "
                    "attempt.status IN ('admitted', 'running') ORDER BY run.run_ref"
                )
            ).all()
            for run in runs:
                checkpoint = connection.execute(
                    text(
                        "SELECT * FROM ar_writing_checkpoints WHERE attempt_ref = "
                        ":attempt_ref"
                    ),
                    {"attempt_ref": run.attempt_ref},
                ).first()
                if checkpoint is not None:
                    _writing_checkpoint_from_row(checkpoint)
                    if (
                        checkpoint.run_ref != run.run_ref
                        or checkpoint.fence_ref != run.fence_ref
                        or checkpoint.native_session_ref != run.native_session_ref
                    ):
                        raise OwnerConflict("writing_checkpoint_integrity_invalid")

                now = time.time()
                generation = int(run.attempt_generation) + 1
                attempt_ref = new_ref("writing_attempt")
                fence_ref = new_ref("writing_fence")
                attempt_status = "running" if checkpoint is not None else "admitted"
                connection.execute(
                    text(
                        "UPDATE ar_writing_attempts SET status = 'retired', "
                        "completed_at = COALESCE(completed_at, :now) WHERE "
                        "attempt_ref = :attempt_ref AND status IN "
                        "('admitted', 'running')"
                    ),
                    {"now": now, "attempt_ref": run.attempt_ref},
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_writing_attempts (attempt_ref, run_ref, "
                        "generation, root_session_ref, native_session_ref, fence_ref, "
                        "provider_job_ref, status, predecessor_version_ref, "
                        "predecessor_markdown_hash, feedback_json, feedback_hash, "
                        "decision_status, decision_receipt_json, "
                        "decision_receipt_hash, created_at, started_at) VALUES (:attempt_ref, "
                        ":run_ref, :generation, :root_session_ref, "
                        ":native_session_ref, :fence_ref, :provider_job_ref, :status, "
                        ":predecessor_version_ref, :predecessor_markdown_hash, "
                        ":feedback_json, :feedback_hash, :decision_status, "
                        ":decision_receipt_json, :decision_receipt_hash, :now, "
                        ":started_at)"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "run_ref": run.run_ref,
                        "generation": generation,
                        "root_session_ref": run.root_session_ref,
                        "native_session_ref": run.native_session_ref,
                        "fence_ref": fence_ref,
                        "provider_job_ref": run.provider_job_ref,
                        "status": attempt_status,
                        "predecessor_version_ref": run.predecessor_version_ref,
                        "predecessor_markdown_hash": (
                            connection.execute(
                                text(
                                    "SELECT predecessor_markdown_hash FROM "
                                    "ar_writing_attempts WHERE attempt_ref = "
                                    ":attempt_ref"
                                ),
                                {"attempt_ref": run.attempt_ref},
                            ).scalar_one_or_none()
                        ),
                        "feedback_json": run.feedback_json,
                        "feedback_hash": run.feedback_hash,
                        "decision_status": (
                            connection.execute(
                                text(
                                    "SELECT decision_status FROM ar_writing_attempts "
                                    "WHERE attempt_ref = :attempt_ref"
                                ),
                                {"attempt_ref": run.attempt_ref},
                            ).scalar_one_or_none()
                        ),
                        "decision_receipt_json": (
                            connection.execute(
                                text(
                                    "SELECT decision_receipt_json FROM "
                                    "ar_writing_attempts WHERE attempt_ref = "
                                    ":attempt_ref"
                                ),
                                {"attempt_ref": run.attempt_ref},
                            ).scalar_one_or_none()
                        ),
                        "decision_receipt_hash": (
                            connection.execute(
                                text(
                                    "SELECT decision_receipt_hash FROM "
                                    "ar_writing_attempts WHERE attempt_ref = "
                                    ":attempt_ref"
                                ),
                                {"attempt_ref": run.attempt_ref},
                            ).scalar_one_or_none()
                        ),
                        "now": now,
                        "started_at": now if checkpoint is not None else None,
                    },
                )
                checkpoint_ref = None
                if checkpoint is not None:
                    checkpoint_ref = new_ref("writing_checkpoint")
                    connection.execute(
                        text(
                            "INSERT INTO ar_writing_checkpoints (checkpoint_ref, "
                            "run_ref, attempt_ref, fence_ref, native_session_ref, "
                            "markdown, markdown_hash, citations_json, citations_hash, "
                            "created_at) VALUES (:checkpoint_ref, :run_ref, "
                            ":attempt_ref, :fence_ref, :native_session_ref, :markdown, "
                            ":markdown_hash, :citations_json, :citations_hash, :now)"
                        ),
                        {
                            "checkpoint_ref": checkpoint_ref,
                            "run_ref": run.run_ref,
                            "attempt_ref": attempt_ref,
                            "fence_ref": fence_ref,
                            "native_session_ref": checkpoint.native_session_ref,
                            "markdown": checkpoint.markdown,
                            "markdown_hash": checkpoint.markdown_hash,
                            "citations_json": checkpoint.citations_json,
                            "citations_hash": checkpoint.citations_hash,
                            "now": now,
                        },
                    )
                connection.execute(
                    text(
                        "UPDATE ar_writing_runs SET attempt_ref = :attempt_ref, "
                        "attempt_generation = :generation, fence_ref = :fence_ref, "
                        "updated_at = :now WHERE run_ref = :run_ref AND "
                        "attempt_ref = :retired_attempt_ref"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "generation": generation,
                        "fence_ref": fence_ref,
                        "now": now,
                        "run_ref": run.run_ref,
                        "retired_attempt_ref": run.attempt_ref,
                    },
                )
                self._feed.record(
                    connection,
                    "agent_runtime.writing_recovered",
                    {
                        "run_ref": run.run_ref,
                        "retired_attempt_ref": run.attempt_ref,
                        "attempt_ref": attempt_ref,
                        "attempt_generation": generation,
                        "fence_ref": fence_ref,
                        "checkpoint_ref": checkpoint_ref,
                        "native_session_ref": run.native_session_ref,
                    },
                )
            if runs:
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "writing_attempt_count = writing_attempt_count + :count "
                        "WHERE singleton = 'owner'"
                    ),
                    {"count": len(runs)},
                )

    def admit_experiment(
        self,
        *,
        admission: ExperimentDomainAdmission,
        runtime_binding: ExperimentRuntimeBinding,
        require_idle: bool = False,
    ) -> ExperimentRun:
        if self._experiment_binding_verifier is None:
            raise OwnerConflict("experiment_binding_verifier_unavailable")
        runtime_document = runtime_binding.as_dict()
        runtime_hash = canonical_hash(runtime_document)
        identities = admission.identities
        execution_request = admission.execution_request
        variant_binding = admission.variant_run_binding
        measurement_binding = admission.evaluation_attempt_binding
        if (
            execution_request.implementation_binding.content_hash
            != runtime_binding.runner_bundle_hash
        ):
            raise OwnerConflict("experiment_implementation_binding_mismatch")
        if (
            variant_binding.subject_kind != "variant_run"
            or variant_binding.subject_ref != identities.variant_run_ref
            or measurement_binding.subject_kind != "evaluation_attempt"
            or measurement_binding.subject_ref != identities.evaluation_attempt_ref
            or variant_binding.binding_ref == measurement_binding.binding_ref
            or variant_binding.receipt.receipt_ref
            == measurement_binding.receipt.receipt_ref
        ):
            raise OwnerConflict("experiment_input_binding_invalid")
        self._experiment_binding_verifier.verify_experiment_execution_request(
            execution_request_ref=execution_request.execution_request_ref,
            quest_ref=execution_request.quest_ref,
            definition_hash=execution_request.definition_hash,
            implementation_binding=execution_request.implementation_binding,
            receipt=execution_request.receipt,
        )
        for binding in (variant_binding, measurement_binding):
            self._experiment_binding_verifier.verify_experiment_input_binding(
                binding_ref=binding.binding_ref,
                subject_kind=binding.subject_kind,
                subject_ref=binding.subject_ref,
                inputs_hash=binding.inputs_hash,
                receipt=binding.receipt,
            )
        bundle_target_ref = (
            None
            if self._target_graph_verifier is None
            else self._target_graph_verifier.match_bundle_target_candidate(
                quest_ref=execution_request.quest_ref,
                evaluation_attempt_ref=identities.evaluation_attempt_ref,
                execution_request_ref=execution_request.execution_request_ref,
                definition_hash=execution_request.definition_hash,
            )
        )

        with self._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision WHERE "
                    "singleton = 'owner'"
                )
            )
            existing = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_runs WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": identities.evaluation_attempt_ref},
            ).first()
            if existing is not None:
                expected = (
                    existing.execution_request_ref
                    == execution_request.execution_request_ref
                    and existing.quest_ref == execution_request.quest_ref
                    and existing.definition_hash == execution_request.definition_hash
                    and existing.execution_request_receipt_ref
                    == execution_request.receipt.receipt_ref
                    and existing.execution_request_receipt_hash
                    == execution_request.receipt.payload_hash
                    and existing.implementation_asset_ref
                    == execution_request.implementation_binding.asset_ref
                    and existing.implementation_version_ref
                    == execution_request.implementation_binding.version_ref
                    and existing.implementation_content_hash
                    == execution_request.implementation_binding.content_hash
                    and existing.implementation_manifest_hash
                    == execution_request.implementation_binding.manifest_hash
                    and existing.implementation_receipt_ref
                    == execution_request.implementation_binding.receipt.receipt_ref
                    and existing.implementation_receipt_hash
                    == execution_request.implementation_binding.receipt.payload_hash
                    and existing.variant_run_ref == identities.variant_run_ref
                    and existing.variant_input_binding_ref
                    == variant_binding.binding_ref
                    and existing.variant_input_hash == variant_binding.inputs_hash
                    and existing.variant_input_receipt_ref
                    == variant_binding.receipt.receipt_ref
                    and existing.variant_input_receipt_hash
                    == variant_binding.receipt.payload_hash
                    and existing.measurement_input_binding_ref
                    == measurement_binding.binding_ref
                    and existing.measurement_input_hash
                    == measurement_binding.inputs_hash
                    and existing.measurement_input_receipt_ref
                    == measurement_binding.receipt.receipt_ref
                    and existing.measurement_input_receipt_hash
                    == measurement_binding.receipt.payload_hash
                    and existing.runtime_binding_json
                    == canonical_json(runtime_document)
                    and existing.runtime_binding_hash == runtime_hash
                    and existing.bundle_target_ref == bundle_target_ref
                )
                if not expected:
                    raise OwnerConflict("experiment_admission_conflict")
            else:
                if require_idle:
                    active = connection.execute(
                        text(
                            "SELECT run_ref FROM ar_experiment_runs WHERE "
                            "status IN ('admitted', 'running') ORDER BY "
                            "created_at, run_ref LIMIT 1"
                        )
                    ).first()
                    if active is not None:
                        raise OwnerConflict("experiment_execution_busy")
                run_ref = new_ref("experiment_run")
                attempt_ref = new_ref("experiment_execution_attempt")
                root_session_ref = new_ref("experiment_root_session")
                fence_ref = new_ref("experiment_fence")
                try:
                    provider_operation = typed_provider_operation_ref(
                        run_ref, "experiment", 1
                    )
                except ProviderSupervisorError as error:
                    raise OwnerConflict(
                        "experiment_provider_operation_invalid"
                    ) from error
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO ar_experiment_runs (run_ref, "
                        "execution_request_ref, quest_ref, definition_hash, "
                        "execution_request_receipt_ref, "
                        "execution_request_receipt_hash, "
                        "implementation_asset_ref, implementation_version_ref, "
                        "implementation_content_hash, "
                        "implementation_manifest_hash, "
                        "implementation_receipt_ref, "
                        "implementation_receipt_hash, evaluation_attempt_ref, "
                        "variant_run_ref, "
                        "variant_input_binding_ref, variant_input_hash, "
                        "variant_input_receipt_ref, variant_input_receipt_hash, "
                        "measurement_input_binding_ref, measurement_input_hash, "
                        "measurement_input_receipt_ref, "
                        "measurement_input_receipt_hash, status, "
                        "bundle_target_ref, "
                        "provider_operation_ref, provider_operation_generation, "
                        "provider_operation_retry_permitted, attempt_ref, "
                        "attempt_generation, root_session_ref, fence_ref, "
                        "runtime_binding_json, runtime_binding_hash, created_at, "
                        "updated_at) VALUES (:run_ref, :execution_request_ref, "
                        ":quest_ref, :definition_hash, "
                        ":execution_request_receipt_ref, "
                        ":execution_request_receipt_hash, "
                        ":implementation_asset_ref, :implementation_version_ref, "
                        ":implementation_content_hash, "
                        ":implementation_manifest_hash, "
                        ":implementation_receipt_ref, "
                        ":implementation_receipt_hash, "
                        ":evaluation_attempt_ref, "
                        ":variant_run_ref, :variant_input_binding_ref, "
                        ":variant_input_hash, :variant_input_receipt_ref, "
                        ":variant_input_receipt_hash, "
                        ":measurement_input_binding_ref, :measurement_input_hash, "
                        ":measurement_input_receipt_ref, "
                        ":measurement_input_receipt_hash, 'admitted', "
                        ":bundle_target_ref, "
                        ":provider_operation_ref, 1, 0, :attempt_ref, 1, "
                        ":root_session_ref, :fence_ref, "
                        ":runtime_binding_json, :runtime_binding_hash, :now, :now)"
                    ),
                    {
                        "run_ref": run_ref,
                        "execution_request_ref": (
                            execution_request.execution_request_ref
                        ),
                        "quest_ref": execution_request.quest_ref,
                        "definition_hash": execution_request.definition_hash,
                        "execution_request_receipt_ref": (
                            execution_request.receipt.receipt_ref
                        ),
                        "execution_request_receipt_hash": (
                            execution_request.receipt.payload_hash
                        ),
                        "implementation_asset_ref": (
                            execution_request.implementation_binding.asset_ref
                        ),
                        "implementation_version_ref": (
                            execution_request.implementation_binding.version_ref
                        ),
                        "implementation_content_hash": (
                            execution_request.implementation_binding.content_hash
                        ),
                        "implementation_manifest_hash": (
                            execution_request.implementation_binding.manifest_hash
                        ),
                        "implementation_receipt_ref": (
                            execution_request.implementation_binding.receipt.receipt_ref
                        ),
                        "implementation_receipt_hash": (
                            execution_request.implementation_binding.receipt.payload_hash
                        ),
                        "evaluation_attempt_ref": identities.evaluation_attempt_ref,
                        "variant_run_ref": identities.variant_run_ref,
                        "variant_input_binding_ref": variant_binding.binding_ref,
                        "variant_input_hash": variant_binding.inputs_hash,
                        "variant_input_receipt_ref": (
                            variant_binding.receipt.receipt_ref
                        ),
                        "variant_input_receipt_hash": (
                            variant_binding.receipt.payload_hash
                        ),
                        "measurement_input_binding_ref": (
                            measurement_binding.binding_ref
                        ),
                        "measurement_input_hash": measurement_binding.inputs_hash,
                        "measurement_input_receipt_ref": (
                            measurement_binding.receipt.receipt_ref
                        ),
                        "measurement_input_receipt_hash": (
                            measurement_binding.receipt.payload_hash
                        ),
                        "bundle_target_ref": bundle_target_ref,
                        "attempt_ref": attempt_ref,
                        "provider_operation_ref": provider_operation,
                        "root_session_ref": root_session_ref,
                        "fence_ref": fence_ref,
                        "runtime_binding_json": canonical_json(runtime_document),
                        "runtime_binding_hash": runtime_hash,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_experiment_sessions (root_session_ref, "
                        "run_ref, status, created_at, updated_at) VALUES "
                        "(:root_session_ref, :run_ref, 'open', :now, :now)"
                    ),
                    {
                        "root_session_ref": root_session_ref,
                        "run_ref": run_ref,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_experiment_attempts (attempt_ref, run_ref, "
                        "generation, root_session_ref, fence_ref, status, "
                        "created_at) VALUES (:attempt_ref, :run_ref, 1, "
                        ":root_session_ref, :fence_ref, 'admitted', :created_at)"
                    ),
                    {
                        "attempt_ref": attempt_ref,
                        "run_ref": run_ref,
                        "root_session_ref": root_session_ref,
                        "fence_ref": fence_ref,
                        "created_at": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, "
                        "cycle_ref, epoch, status, attempt_ref, root_session_ref, "
                        "fence_ref, control_revision, safe_point_ref, "
                        "terminal_reason, cleanup_status, updated_at) VALUES "
                        "(:run_ref, 'experiment', :quest_ref, NULL, NULL, "
                        "'running', :attempt_ref, :root_session_ref, :fence_ref, "
                        "1, NULL, NULL, 'none', :now)"
                    ),
                    {
                        "run_ref": run_ref,
                        "quest_ref": execution_request.quest_ref,
                        "attempt_ref": attempt_ref,
                        "root_session_ref": root_session_ref,
                        "fence_ref": fence_ref,
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "experiment_run_count = experiment_run_count + 1, "
                        "experiment_attempt_count = experiment_attempt_count + 1, "
                        "experiment_session_count = experiment_session_count + 1, "
                        "active_experiment_run_count = active_experiment_run_count + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.experiment_admitted",
                    {
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "variant_run_ref": identities.variant_run_ref,
                        "evaluation_attempt_ref": identities.evaluation_attempt_ref,
                        "bundle_target_ref": bundle_target_ref,
                    },
                )
        admitted = self.query_experiment_run(identities.evaluation_attempt_ref)
        if admitted is None:
            raise OwnerConflict("experiment_run_missing_after_admission")
        return admitted

    def query_experiment_run(self, evaluation_attempt_ref: str) -> ExperimentRun | None:
        with self._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_runs WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if run is None:
                return None
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_attempts WHERE attempt_ref = "
                    ":attempt_ref"
                ),
                {"attempt_ref": run.attempt_ref},
            ).first()
            session = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_sessions WHERE "
                    "root_session_ref = :root_session_ref"
                ),
                {"root_session_ref": run.root_session_ref},
            ).first()
            control = connection.execute(
                text("SELECT * FROM ar_run_controls WHERE run_ref = :run_ref"),
                {"run_ref": run.run_ref},
            ).first()
            revoked = connection.execute(
                text(
                    "SELECT fence_ref FROM ar_fence_revocations WHERE fence_ref = "
                    ":fence_ref AND run_ref = :run_ref"
                ),
                {"fence_ref": run.fence_ref, "run_ref": run.run_ref},
            ).first()
            event_summary = connection.execute(
                text(
                    "SELECT COUNT(*) AS event_count, COALESCE(SUM(CASE WHEN "
                    "kind = 'stdout' THEN 1 ELSE 0 END), 0) AS "
                    "stdout_event_count FROM ar_experiment_events WHERE "
                    "run_ref = :run_ref AND attempt_ref = :attempt_ref"
                ),
                {"run_ref": run.run_ref, "attempt_ref": run.attempt_ref},
            ).one()
            tail_rows = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_events WHERE run_ref = "
                    ":run_ref AND attempt_ref = :attempt_ref ORDER BY sequence "
                    "DESC LIMIT 256"
                ),
                {"run_ref": run.run_ref, "attempt_ref": run.attempt_ref},
            ).all()
        try:
            TypedExecutionFence(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                generation=int(run.attempt_generation),
                root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref,
            ).validate()
            expected_operation_ref = typed_provider_operation_ref(
                run.run_ref,
                "experiment",
                int(run.provider_operation_generation),
            )
        except (ProviderSupervisorError, TypeError, ValueError) as error:
            raise OwnerConflict("experiment_run_integrity_invalid") from error
        if attempt is None or session is None or control is None or (
            attempt.run_ref != run.run_ref
            or int(attempt.generation) != int(run.attempt_generation)
            or attempt.root_session_ref != run.root_session_ref
            or attempt.fence_ref != run.fence_ref
            or attempt.status != run.status
            or session.run_ref != run.run_ref
            or session.root_session_ref != run.root_session_ref
            or session.status
            != ("closed" if run.status in {"executed", "failed"} else "open")
            or control.run_kind != "experiment"
            or control.run_ref != run.run_ref
            or control.attempt_ref != run.attempt_ref
            or control.root_session_ref != run.root_session_ref
            or control.fence_ref != run.fence_ref
            or expected_operation_ref != run.provider_operation_ref
        ):
            raise OwnerConflict("experiment_run_integrity_invalid")
        fence_status = (
            "revoked"
            if revoked is not None
            or control.status
            in {"terminated", "suspended_fenced", "reconciliation_required"}
            else "completed"
            if control.status == "completed"
            else "current"
        )
        runtime_binding = _experiment_runtime_binding(run.runtime_binding_json)
        if (
            canonical_hash(runtime_binding.as_dict()) != run.runtime_binding_hash
            or run.implementation_content_hash != runtime_binding.runner_bundle_hash
        ):
            raise OwnerConflict("experiment_run_integrity_invalid")
        if self._experiment_binding_verifier is not None:
            self._experiment_binding_verifier.verify_experiment_execution_request(
                execution_request_ref=run.execution_request_ref,
                quest_ref=run.quest_ref,
                definition_hash=run.definition_hash,
                implementation_binding=AcceptedAssetBinding(
                    asset_ref=run.implementation_asset_ref,
                    version_ref=run.implementation_version_ref,
                    content_hash=run.implementation_content_hash,
                    manifest_hash=run.implementation_manifest_hash,
                    receipt=AcceptanceReceipt(
                        issuer="research_memory",
                        kind="asset_acceptance",
                        receipt_ref=run.implementation_receipt_ref,
                        subject_ref=run.implementation_version_ref,
                        payload_hash=run.implementation_receipt_hash,
                    ),
                ),
                receipt=AcceptanceReceipt(
                    issuer="research_graph",
                    kind="experiment_execution_request_acceptance",
                    receipt_ref=run.execution_request_receipt_ref,
                    subject_ref=run.execution_request_ref,
                    payload_hash=run.execution_request_receipt_hash,
                ),
            )
            for subject_kind, subject_ref, prefix in (
                ("variant_run", run.variant_run_ref, "variant"),
                (
                    "evaluation_attempt",
                    run.evaluation_attempt_ref,
                    "measurement",
                ),
            ):
                self._experiment_binding_verifier.verify_experiment_input_binding(
                    binding_ref=getattr(run, f"{prefix}_input_binding_ref"),
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    inputs_hash=getattr(run, f"{prefix}_input_hash"),
                    receipt=AcceptanceReceipt(
                        issuer="research_graph",
                        kind="experiment_input_binding_acceptance",
                        receipt_ref=getattr(run, f"{prefix}_input_receipt_ref"),
                        subject_ref=getattr(run, f"{prefix}_input_binding_ref"),
                        payload_hash=getattr(run, f"{prefix}_input_receipt_hash"),
                    ),
                )
        result: dict[str, object] | None = None
        if run.result_json is not None:
            try:
                result = decoded_object(run.result_json)
            except (TypeError, ValueError) as error:
                raise OwnerConflict("experiment_run_integrity_invalid") from error
            if (
                canonical_json(result) != run.result_json
                or canonical_hash(result) != run.result_hash
            ):
                raise OwnerConflict("experiment_run_integrity_invalid")
        receipt = None
        if attempt.receipt_ref is not None:
            receipt = AcceptanceReceipt(
                issuer=AR_OWNER,
                kind=EXPERIMENT_EXECUTION_RECEIPT_KIND,
                receipt_ref=attempt.receipt_ref,
                subject_ref=run.attempt_ref,
                payload_hash=attempt.receipt_hash,
            )
        event_values = tuple(_experiment_event(row) for row in reversed(tail_rows))
        return ExperimentRun(
            run_ref=run.run_ref,
            execution_request_ref=run.execution_request_ref,
            provider_operation_ref=run.provider_operation_ref,
            provider_operation_generation=int(run.provider_operation_generation),
            provider_operation_retry_permitted=bool(
                run.provider_operation_retry_permitted
            ),
            evaluation_attempt_ref=run.evaluation_attempt_ref,
            variant_run_ref=run.variant_run_ref,
            status=run.status,
            attempt_ref=run.attempt_ref,
            attempt_generation=int(run.attempt_generation),
            root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref,
            fence_status=fence_status,
            managed_status=control.status,
            runtime_binding=runtime_binding,
            runtime_binding_hash=run.runtime_binding_hash,
            result=result,
            result_hash=run.result_hash,
            execution_receipt=receipt,
            failure_code=run.failure_code,
            events=event_values,
            event_count=int(event_summary.event_count),
            stdout_event_count=int(event_summary.stdout_event_count),
        )

    def admit_target_run(
        self,
        *,
        target_ref: str,
        target_spec_hash: str,
        graph_ref: str,
        stage_request_ref: str,
        quest_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        idempotency_key: str,
        human_request_ref: str | None = None,
        human_waiter_ref: str | None = None,
        human_waiter_generation: int | None = None,
        human_authorization_receipt_ref: str | None = None,
    ) -> TargetRunAdmission:
        """Issue the AR-owned admission that a TargetRun binding consumes."""

        _validate_stage_idempotency_key(idempotency_key)
        required = (
            target_ref,
            graph_ref,
            stage_request_ref,
            quest_ref,
            target_run_ref,
            evaluation_attempt_ref,
            execution_request_ref,
        )
        human_values = (
            human_request_ref,
            human_waiter_ref,
            human_waiter_generation,
            human_authorization_receipt_ref,
        )
        has_human_authorization = all(value is not None for value in human_values)
        if (
            any(not isinstance(value, str) or not value for value in required)
            or len(target_spec_hash) != 64
            or len(definition_hash) != 64
            or (
                any(value is not None for value in human_values)
                and not has_human_authorization
            )
            or (
                has_human_authorization
                and (
                    not isinstance(human_waiter_generation, int)
                    or isinstance(human_waiter_generation, bool)
                    or human_waiter_generation < 1
                )
            )
        ):
            raise OwnerConflict("target_run_admission_invalid")
        command = {
            "command": "admit_target_run",
            "target_ref": target_ref,
            "target_spec_hash": target_spec_hash,
            "graph_ref": graph_ref,
            "stage_request_ref": stage_request_ref,
            "quest_ref": quest_ref,
            "target_run_ref": target_run_ref,
            "evaluation_attempt_ref": evaluation_attempt_ref,
            "execution_request_ref": execution_request_ref,
            "definition_hash": definition_hash,
            "human_request_ref": human_request_ref,
            "human_waiter_ref": human_waiter_ref,
            "human_waiter_generation": human_waiter_generation,
            "human_authorization_receipt_ref": human_authorization_receipt_ref,
        }
        request_hash = canonical_hash(command)
        with self._database.write() as connection:
            replay = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_admissions WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                return _target_run_admission(replay)
            verifier = self._target_graph_verifier
            if verifier is None:
                raise OwnerConflict("target_graph_verifier_unavailable")
            risk_class = verifier.verify_target_run_candidate(
                target_ref=target_ref,
                target_spec_hash=target_spec_hash,
                graph_ref=graph_ref,
                stage_request_ref=stage_request_ref,
                quest_ref=quest_ref,
                evaluation_attempt_ref=evaluation_attempt_ref,
                execution_request_ref=execution_request_ref,
                definition_hash=definition_hash,
            )
            if (risk_class == "high") != has_human_authorization:
                raise OwnerConflict("target_run_authorization_invalid")
            existing = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_admissions WHERE "
                    "idempotency_key = :idempotency_key"
                ),
                {"idempotency_key": idempotency_key},
            ).first()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OwnerConflict("idempotency_conflict")
                admission_ref = existing.admission_ref
            else:
                experiment = connection.execute(
                    text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                    {"run_ref": target_run_ref},
                ).first()
                if experiment is None or (
                    experiment.quest_ref != quest_ref
                    or experiment.evaluation_attempt_ref != evaluation_attempt_ref
                    or experiment.execution_request_ref != execution_request_ref
                    or experiment.definition_hash != definition_hash
                    or experiment.bundle_target_ref != target_ref
                    or experiment.status != "admitted"
                ):
                    raise OwnerConflict("target_run_experiment_binding_invalid")
                dispatch_row = connection.execute(
                    text(
                        "SELECT * FROM ar_bundle_dispatch_decisions WHERE "
                        "graph_ref = :graph_ref ORDER BY generation DESC LIMIT 1"
                    ),
                    {"graph_ref": graph_ref},
                ).first()
                if dispatch_row is None:
                    raise OwnerConflict("target_run_dispatch_invalid")
                dispatch = _bundle_dispatch_decision(dispatch_row)
                stage_run = connection.execute(
                    text(
                        "SELECT request_ref, stage FROM ar_stage_runs WHERE "
                        "run_ref = :run_ref"
                    ),
                    {"run_ref": dispatch.run_ref},
                ).first()
                if (
                    dispatch.action != "dispatch"
                    or dispatch.selected_target_ref != target_ref
                    or dispatch.graph_ref != graph_ref
                    or stage_run is None
                    or stage_run.stage != "bundle"
                    or stage_run.request_ref != stage_request_ref
                ):
                    raise OwnerConflict("target_run_dispatch_invalid")
                duplicate = connection.execute(
                    text(
                        "SELECT * FROM ar_target_run_admissions WHERE target_ref = "
                        ":target_ref OR target_run_ref = :target_run_ref"
                    ),
                    {
                        "target_ref": target_ref,
                        "target_run_ref": target_run_ref,
                    },
                ).first()
                if duplicate is not None:
                    raise OwnerConflict("target_run_admission_conflict")
                admission_ref = new_ref("target_run_admission")
                if has_human_authorization:
                    assertion = target_execution_assertion(
                        quest_ref=quest_ref,
                        stage_request_ref=stage_request_ref,
                        graph_ref=graph_ref,
                        target_ref=target_ref,
                        target_spec_hash=target_spec_hash,
                        risk_class=risk_class,
                    )
                    requirement = target_execution_authorization_requirement(
                        quest_ref=quest_ref,
                        stage_request_ref=stage_request_ref,
                        graph_ref=graph_ref,
                        target_ref=target_ref,
                        target_spec_hash=target_spec_hash,
                    )
                    human_request = connection.execute(
                        text(
                            "SELECT * FROM owner_human_requests WHERE issuer = "
                            ":issuer AND request_ref = :request_ref"
                        ),
                        {
                            "issuer": AR_OWNER,
                            "request_ref": human_request_ref,
                        },
                    ).first()
                    waiter = connection.execute(
                        text(
                            "SELECT * FROM owner_human_request_waiters WHERE "
                            "request_ref = :request_ref AND waiter_ref = "
                            ":waiter_ref"
                        ),
                        {
                            "request_ref": human_request_ref,
                            "waiter_ref": human_waiter_ref,
                        },
                    ).first()
                    validation = connection.execute(
                        text(
                            "SELECT * FROM "
                            "owner_human_request_resume_validations WHERE "
                            "request_ref = :request_ref AND waiter_ref = "
                            ":waiter_ref ORDER BY created_at DESC, "
                            "validation_ref DESC LIMIT 1"
                        ),
                        {
                            "request_ref": human_request_ref,
                            "waiter_ref": human_waiter_ref,
                        },
                    ).first()
                    if human_request is None or waiter is None or validation is None:
                        raise OwnerConflict("target_run_authorization_invalid")
                    try:
                        stored_assertion = decoded_object(
                            human_request.target_assertion_json
                        )
                        stored_requirement = decoded_object(
                            human_request.required_authorization_json
                        )
                    except (TypeError, ValueError) as error:
                        raise OwnerConflict(
                            "target_run_authorization_invalid"
                        ) from error
                    if (
                        human_request.kind != "capability_authorization"
                        or human_request.quest_ref != quest_ref
                        or stored_assertion != assertion
                        or stored_requirement != requirement
                        or waiter.target_assertion_hash != canonical_hash(assertion)
                        or int(waiter.generation) != cast(int, human_waiter_generation)
                        or validation.authorization_receipt_ref
                        != human_authorization_receipt_ref
                    ):
                        raise OwnerConflict("target_run_authorization_invalid")
                    verifier = self._authorization_verifier
                    if verifier is None:
                        raise OwnerConflict("human_collaboration_verifier_unavailable")
                    verifier.verify_capability_authorization(
                        requirement=requirement,
                        receipt_ref=cast(str, human_authorization_receipt_ref),
                    )
                    self._consume_human_request_waiter(
                        connection,
                        request_ref=cast(str, human_request_ref),
                        waiter_ref=cast(str, human_waiter_ref),
                        generation=cast(int, human_waiter_generation),
                        work_ref=admission_ref,
                        work_hash=request_hash,
                    )
                bindings = {
                    **{
                        key: value for key, value in command.items() if key != "command"
                    },
                    "dispatch_decision_ref": dispatch.decision_ref,
                    "dispatch_receipt_ref": dispatch.receipt.receipt_ref,
                    "dispatch_receipt_hash": dispatch.receipt.payload_hash,
                }
                receipt_ref = new_ref("ar_target_run_admission_receipt")
                receipt_hash = _owner_receipt_hash(
                    TARGET_RUN_ADMISSION_RECEIPT_KIND,
                    admission_ref,
                    bindings,
                )
                now = time.time()
                connection.execute(
                    text(
                        "INSERT INTO ar_target_run_admissions (admission_ref, "
                        "target_ref, target_spec_hash, graph_ref, "
                        "stage_request_ref, quest_ref, target_run_ref, "
                        "evaluation_attempt_ref, execution_request_ref, "
                        "definition_hash, dispatch_decision_ref, "
                        "dispatch_receipt_ref, dispatch_receipt_hash, "
                        "human_request_ref, human_waiter_ref, "
                        "human_waiter_generation, "
                        "human_authorization_receipt_ref, idempotency_key, "
                        "request_hash, receipt_ref, receipt_hash, admitted_at) "
                        "VALUES (:admission_ref, :target_ref, :target_spec_hash, "
                        ":graph_ref, :stage_request_ref, :quest_ref, "
                        ":target_run_ref, :evaluation_attempt_ref, "
                        ":execution_request_ref, :definition_hash, "
                        ":dispatch_decision_ref, :dispatch_receipt_ref, "
                        ":dispatch_receipt_hash, "
                        ":human_request_ref, :human_waiter_ref, "
                        ":human_waiter_generation, "
                        ":human_authorization_receipt_ref, :idempotency_key, "
                        ":request_hash, :receipt_ref, :receipt_hash, :admitted_at)"
                    ),
                    {
                        **bindings,
                        "admission_ref": admission_ref,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "admitted_at": now,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1 "
                        "WHERE singleton = 'owner'"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.target_run_admitted",
                    {
                        "admission_ref": admission_ref,
                        "target_ref": target_ref,
                        "target_run_ref": target_run_ref,
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "dispatch_decision_ref": dispatch.decision_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
        admitted = self.query_target_run_admission(target_ref)
        if admitted is None or admitted.admission_ref != admission_ref:
            raise OwnerConflict("target_run_admission_missing_after_commit")
        return admitted

    def query_target_run_admission(self, target_ref: str) -> TargetRunAdmission | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_admissions WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": target_ref},
            ).first()
        return None if row is None else _target_run_admission(row)

    def query_experiment_events(
        self,
        evaluation_attempt_ref: str,
        *,
        after_sequence: int = 0,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...]:
        if isinstance(after_sequence, bool) or after_sequence < 0:
            raise OwnerConflict("experiment_event_cursor_invalid")
        if isinstance(limit, bool) or not 1 <= limit <= 512:
            raise OwnerConflict("experiment_event_limit_invalid")
        with self._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT run_ref, attempt_ref FROM ar_experiment_runs WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if run is None:
                raise OwnerConflict("experiment_run_not_found")
            rows = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_events WHERE run_ref = "
                    ":run_ref AND attempt_ref = :attempt_ref AND sequence > "
                    ":after_sequence ORDER BY sequence LIMIT :limit"
                ),
                {
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "after_sequence": after_sequence,
                    "limit": limit,
                },
            ).all()
        return tuple(_experiment_event(row) for row in rows)

    def query_active_experiment_run(self) -> ExperimentRun | None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT runs.evaluation_attempt_ref FROM ar_experiment_runs runs "
                    "JOIN ar_run_controls controls ON controls.run_ref = runs.run_ref "
                    "WHERE runs.status IN ('admitted', 'running') AND "
                    "controls.status = 'running' ORDER BY runs.created_at, "
                    "runs.run_ref LIMIT 1"
                )
            ).first()
        if row is None:
            return None
        active = self.query_experiment_run(row.evaluation_attempt_ref)
        if active is None:
            raise OwnerConflict("experiment_run_integrity_invalid")
        return active

    def query_executed_experiment_runs(
        self, *, offset: int = 0, limit: int = 64
    ) -> tuple[ExperimentRun, ...]:
        if isinstance(offset, bool) or offset < 0:
            raise OwnerConflict("experiment_run_cursor_invalid")
        if isinstance(limit, bool) or not 1 <= limit <= 256:
            raise OwnerConflict("experiment_run_limit_invalid")
        with self._database.read() as connection:
            rows = connection.execute(
                text(
                    "SELECT evaluation_attempt_ref FROM ar_experiment_runs WHERE "
                    "status = 'executed' ORDER BY completed_at, run_ref "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            ).all()
        runs = tuple(
            self.query_experiment_run(row.evaluation_attempt_ref) for row in rows
        )
        if any(run is None for run in runs):
            raise OwnerConflict("experiment_run_integrity_invalid")
        return tuple(run for run in runs if run is not None)

    def claim_next_experiment(self) -> ExperimentRun | None:
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT r.* FROM ar_experiment_runs AS r JOIN "
                    "ar_run_controls AS c ON c.run_ref = r.run_ref WHERE "
                    "r.status = 'admitted' AND c.status = 'running' AND "
                    "(r.bundle_target_ref IS NULL OR EXISTS (SELECT 1 FROM "
                    "ar_target_run_admissions a WHERE a.target_ref = "
                    "r.bundle_target_ref AND a.target_run_ref = r.run_ref)) "
                    "ORDER BY "
                    "r.updated_at, r.run_ref LIMIT 1"
                )
            ).first()
            if run is None:
                return None
            _assert_runtime_control_allows(connection, run.run_ref, run.fence_ref)
            now = time.time()
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'running', "
                    "updated_at = :now WHERE run_ref = :run_ref AND status = "
                    "'admitted'"
                ),
                {"run_ref": run.run_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'running', "
                    "started_at = :now WHERE attempt_ref = :attempt_ref AND "
                    "status = 'admitted'"
                ),
                {"attempt_ref": run.attempt_ref, "now": now},
            )
            provider_unit_ref = new_ref("provider_unit")
            connection.execute(
                text(
                    "INSERT INTO ar_provider_units (unit_ref, operation_ref, run_ref, "
                    "attempt_ref, fence_ref, unit_kind, status, started_at, "
                    "completed_at) VALUES (:unit_ref, :operation_ref, :run_ref, "
                    ":attempt_ref, :fence_ref, 'experiment', 'active', :now, NULL)"
                ),
                {
                    "unit_ref": provider_unit_ref,
                    "operation_ref": run.provider_operation_ref,
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "now": now,
                },
            )
            sequence = _append_experiment_event(
                connection,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                kind="status",
                payload={"status": "running"},
                observed_at=now,
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_started",
                {
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                },
            )
            evaluation_attempt_ref = run.evaluation_attempt_ref
        return self.query_experiment_run(evaluation_attempt_ref)

    def record_experiment_observation(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        kind: str,
        payload: dict[str, object],
        observed_at: float,
    ) -> None:
        if kind not in {"stdout", "telemetry"}:
            raise OwnerConflict("experiment_observation_kind_invalid")
        if not math.isfinite(observed_at) or type(payload) is not dict:
            raise OwnerConflict("experiment_observation_invalid")
        try:
            payload_json = canonical_json(payload)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("experiment_observation_invalid") from error
        if len(payload_json.encode("utf-8")) > 64 * 1024:
            raise OwnerConflict("experiment_observation_invalid")
        if kind == "stdout":
            line = payload.get("line")
            if (
                not isinstance(line, str)
                or "\n" in line
                or "\r" in line
                or len(line) > 4000
            ):
                raise OwnerConflict("experiment_stdout_invalid")
        with self._database.write() as connection:
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            run = _current_experiment_execution(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                required_status="running",
            )
            sequence = _append_experiment_event(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                kind=kind,
                payload=payload,
                observed_at=observed_at,
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET updated_at = :updated_at "
                    "WHERE run_ref = :run_ref"
                ),
                {"updated_at": observed_at, "run_ref": run.run_ref},
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_observed",
                {
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "sequence": sequence,
                    "kind": kind,
                },
            )

    def complete_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result: dict[str, object],
    ) -> ExperimentRun:
        if type(result) is not dict:
            raise OwnerConflict("experiment_result_invalid")
        try:
            result_json = canonical_json(result)
            result_hash = canonical_hash(result)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("experiment_result_invalid") from error
        if len(result_json.encode("utf-8")) > 16 * 1024 * 1024:
            raise OwnerConflict("experiment_result_invalid")
        with self._database.write() as connection:
            existing = connection.execute(
                text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            if existing is not None and existing.status == "executed":
                if (
                    existing.attempt_ref != attempt_ref
                    or existing.fence_ref != fence_ref
                    or existing.result_hash != result_hash
                    or existing.result_json != result_json
                ):
                    raise OwnerConflict("experiment_completion_conflict")
                evaluation_attempt_ref = existing.evaluation_attempt_ref
            else:
                _assert_runtime_control_allows(connection, run_ref, fence_ref)
                run = _current_experiment_execution(
                    connection,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    fence_ref=fence_ref,
                    required_status="running",
                )
                provider_result = ExperimentProviderResult.from_document(result)
                now = time.time()
                receipt_ref = new_ref("ar_experiment_execution_receipt")
                _append_experiment_event(
                    connection,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    fence_ref=fence_ref,
                    kind="status",
                    payload={"status": "executed", "result_hash": result_hash},
                    observed_at=now,
                )
                event_rows = connection.execute(
                    text(
                        "SELECT * FROM ar_experiment_events WHERE run_ref = "
                        ":run_ref AND attempt_ref = :attempt_ref ORDER BY sequence"
                    ),
                    {"run_ref": run_ref, "attempt_ref": attempt_ref},
                ).all()
                result_manifest = experiment_result_component_manifest(
                    provider_result,
                    tuple(_experiment_event(row) for row in event_rows),
                )
                receipt_hash = _experiment_execution_receipt_hash(
                    run,
                    attempt_ref,
                    fence_ref,
                    result_hash,
                    result_manifest,
                )
                connection.execute(
                    text(
                        "UPDATE ar_experiment_attempts SET status = 'executed', "
                        "receipt_ref = :receipt_ref, receipt_hash = :receipt_hash, "
                        "completed_at = :now WHERE attempt_ref = :attempt_ref"
                    ),
                    {
                        "receipt_ref": receipt_ref,
                        "receipt_hash": receipt_hash,
                        "now": now,
                        "attempt_ref": attempt_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_experiment_runs SET status = 'executed', "
                        "result_json = :result_json, result_hash = :result_hash, "
                        "provider_operation_retry_permitted = 0, updated_at = "
                        ":now, completed_at = :now WHERE run_ref = :run_ref"
                    ),
                    {
                        "result_json": result_json,
                        "result_hash": result_hash,
                        "now": now,
                        "run_ref": run_ref,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE ar_experiment_sessions SET status = 'closed', "
                        "updated_at = :now WHERE root_session_ref = "
                        ":root_session_ref"
                    ),
                    {"root_session_ref": run.root_session_ref, "now": now},
                )
                connection.execute(
                    text(
                        "UPDATE ar_run_controls SET status = 'completed', "
                        "control_revision = control_revision + 1, "
                        "terminal_reason = NULL, cleanup_status = 'none', "
                        "updated_at = :now WHERE run_ref = :run_ref"
                    ),
                    {"now": now, "run_ref": run_ref},
                )
                _close_provider_unit(
                    connection,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    fence_ref=fence_ref,
                )
                connection.execute(
                    text(
                        "UPDATE agent_runtime_state SET revision = revision + 1, "
                        "experiment_completed_run_count = "
                        "experiment_completed_run_count + 1, "
                        "active_experiment_run_count = "
                        "active_experiment_run_count - 1 WHERE singleton = "
                        "'owner' AND active_experiment_run_count > 0"
                    )
                )
                self._feed.record(
                    connection,
                    "agent_runtime.experiment_completed",
                    {
                        "run_ref": run_ref,
                        "attempt_ref": attempt_ref,
                        "fence_ref": fence_ref,
                        "evaluation_attempt_ref": run.evaluation_attempt_ref,
                        "receipt_ref": receipt_ref,
                    },
                )
                evaluation_attempt_ref = run.evaluation_attempt_ref
        completed = self.query_experiment_run(evaluation_attempt_ref)
        if completed is None:
            raise OwnerConflict("experiment_completion_missing_after_commit")
        return completed

    def retry_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> ExperimentRun:
        if failure_code not in EXPERIMENT_RETRYABLE_PROVIDER_FAILURES:
            raise OwnerConflict("experiment_retry_not_permitted")
        with self._database.write() as connection:
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            run = _current_experiment_execution(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                required_status="running",
            )
            _close_provider_unit(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
            )
            operation_generation = int(run.provider_operation_generation) + 1
            if operation_generation > EXPERIMENT_MAX_PROVIDER_OPERATION_GENERATIONS:
                raise OwnerConflict("experiment_retry_limit_reached")
            try:
                operation_ref = typed_provider_operation_ref(
                    run_ref,
                    "experiment",
                    operation_generation,
                )
            except ProviderSupervisorError as error:
                raise OwnerConflict("experiment_provider_operation_invalid") from error
            now = time.time()
            _append_experiment_event(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                kind="status",
                payload={"status": "failed", "reason": {"code": failure_code}},
                observed_at=now,
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'failed', "
                    "retired_reason = :failure_code, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {
                    "failure_code": failure_code,
                    "now": now,
                    "attempt_ref": attempt_ref,
                },
            )
            replacement_ref, replacement_fence = _insert_replacement_attempt(
                connection,
                run_ref,
                int(run.attempt_generation) + 1,
                run.root_session_ref,
                now,
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'admitted', "
                    "provider_operation_ref = :provider_operation_ref, "
                    "provider_operation_generation = :operation_generation, "
                    "provider_operation_retry_permitted = 0, attempt_ref = "
                    ":attempt_ref, attempt_generation = attempt_generation + 1, "
                    "fence_ref = :fence_ref, failure_code = NULL, completed_at = "
                    "NULL, updated_at = :now WHERE run_ref = :run_ref"
                ),
                {
                    "provider_operation_ref": operation_ref,
                    "operation_generation": operation_generation,
                    "attempt_ref": replacement_ref,
                    "fence_ref": replacement_fence,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET status = 'running', attempt_ref = "
                    ":attempt_ref, fence_ref = :fence_ref, control_revision = "
                    "control_revision + 1, safe_point_ref = NULL, terminal_reason = "
                    "NULL, cleanup_status = 'none', updated_at = :now WHERE "
                    "run_ref = :run_ref"
                ),
                {
                    "attempt_ref": replacement_ref,
                    "fence_ref": replacement_fence,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "experiment_attempt_count = experiment_attempt_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_replaced",
                {
                    "run_ref": run_ref,
                    "retired_attempt_ref": attempt_ref,
                    "attempt_ref": replacement_ref,
                    "attempt_generation": int(run.attempt_generation) + 1,
                    "root_session_ref": run.root_session_ref,
                    "fence_ref": replacement_fence,
                    "provider_operation_ref": operation_ref,
                    "provider_operation_generation": operation_generation,
                    "failure": {"code": failure_code},
                },
            )
            evaluation_attempt_ref = run.evaluation_attempt_ref
        replacement = self.query_experiment_run(evaluation_attempt_ref)
        if replacement is None:
            raise OwnerConflict("experiment_replacement_missing_after_commit")
        return replacement

    def fail_experiment_execution(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        failure_code: str,
    ) -> ExperimentRun:
        if not failure_code or len(failure_code) > 96:
            raise OwnerConflict("experiment_failure_code_invalid")
        with self._database.write() as connection:
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            run = _current_experiment_execution(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                required_status="running",
            )
            _close_provider_unit(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
            )
            now = time.time()
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'failed', "
                    "retired_reason = :failure_code, completed_at = :now WHERE "
                    "attempt_ref = :attempt_ref"
                ),
                {
                    "failure_code": failure_code,
                    "now": now,
                    "attempt_ref": attempt_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'failed', "
                    "provider_operation_retry_permitted = 0, failure_code = "
                    ":failure_code, updated_at = :now, completed_at = :now "
                    "WHERE run_ref = :run_ref"
                ),
                {"failure_code": failure_code, "now": now, "run_ref": run_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_sessions SET status = 'closed', "
                    "updated_at = :now WHERE root_session_ref = "
                    ":root_session_ref"
                ),
                {"root_session_ref": run.root_session_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE ar_run_controls SET status = 'terminated', "
                    "control_revision = control_revision + 1, terminal_reason = "
                    ":failure_code, cleanup_status = 'none', updated_at = :now "
                    "WHERE run_ref = :run_ref"
                ),
                {
                    "failure_code": failure_code,
                    "now": now,
                    "run_ref": run_ref,
                },
            )
            _append_experiment_event(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                kind="status",
                payload={"status": "failed", "reason": {"code": failure_code}},
                observed_at=now,
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "active_experiment_run_count = active_experiment_run_count - 1 "
                    "WHERE singleton = 'owner' AND active_experiment_run_count > 0"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_failed",
                {
                    "run_ref": run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "failure": {"code": failure_code},
                },
            )
            evaluation_attempt_ref = run.evaluation_attempt_ref
        failed = self.query_experiment_run(evaluation_attempt_ref)
        if failed is None:
            raise OwnerConflict("experiment_failure_missing_after_commit")
        return failed

    def defer_experiment_reconciliation(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        reason_code: str,
    ) -> ExperimentRun:
        if reason_code != "experiment_provider_reconciliation_pending":
            raise OwnerConflict("experiment_reconciliation_reason_invalid")
        with self._database.write() as connection:
            _assert_runtime_control_allows(connection, run_ref, fence_ref)
            run = _current_experiment_execution(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                required_status="running",
            )
            now = time.time()
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'admitted' "
                    "WHERE attempt_ref = :attempt_ref AND status = 'running'"
                ),
                {"attempt_ref": attempt_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'admitted', "
                    "updated_at = :now WHERE run_ref = :run_ref AND status = "
                    "'running'"
                ),
                {"now": now, "run_ref": run_ref},
            )
            _append_experiment_event(
                connection,
                run_ref=run_ref,
                attempt_ref=attempt_ref,
                fence_ref=fence_ref,
                kind="status",
                payload={
                    "status": "reconciliation_pending",
                    "reason": {"code": reason_code},
                },
                observed_at=now,
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_reconciliation_deferred",
                {
                    "run_ref": run.run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "provider_operation_ref": run.provider_operation_ref,
                    "reason": {"code": reason_code},
                },
            )
            evaluation_attempt_ref = run.evaluation_attempt_ref
        deferred = self.query_experiment_run(evaluation_attempt_ref)
        if deferred is None:
            raise OwnerConflict("experiment_reconciliation_defer_missing")
        return deferred

    def replace_experiment_execution(
        self, evaluation_attempt_ref: str
    ) -> ExperimentRun:
        with self._database.write() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_runs WHERE "
                    "evaluation_attempt_ref = :evaluation_attempt_ref"
                ),
                {"evaluation_attempt_ref": evaluation_attempt_ref},
            ).first()
            if run is None:
                raise OwnerConflict("experiment_run_not_found")
            if run.status != "failed" or not bool(
                run.provider_operation_retry_permitted
            ):
                raise OwnerConflict("experiment_replacement_not_allowed")
            operation_generation = int(run.provider_operation_generation) + 1
            if operation_generation > EXPERIMENT_MAX_PROVIDER_OPERATION_GENERATIONS:
                raise OwnerConflict("experiment_retry_limit_reached")
            try:
                operation_ref = typed_provider_operation_ref(
                    run.run_ref,
                    "experiment",
                    operation_generation,
                )
            except ProviderSupervisorError as error:
                raise OwnerConflict("experiment_provider_operation_invalid") from error
            now = time.time()
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'retired', "
                    "retired_reason = COALESCE(retired_reason, "
                    "'technical_replacement') WHERE attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": run.attempt_ref},
            )
            attempt_ref, fence_ref = _insert_replacement_attempt(
                connection,
                run.run_ref,
                int(run.attempt_generation) + 1,
                run.root_session_ref,
                now,
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'admitted', "
                    "provider_operation_ref = :provider_operation_ref, "
                    "provider_operation_generation = :operation_generation, "
                    "provider_operation_retry_permitted = 0, "
                    "attempt_ref = :attempt_ref, attempt_generation = "
                    "attempt_generation + 1, fence_ref = :fence_ref, "
                    "result_json = NULL, result_hash = "
                    "NULL, failure_code = NULL, completed_at = NULL, updated_at = "
                    ":now WHERE run_ref = :run_ref"
                ),
                {
                    "provider_operation_ref": operation_ref,
                    "operation_generation": operation_generation,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "now": now,
                    "run_ref": run.run_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_sessions SET status = 'open', "
                    "updated_at = :now WHERE root_session_ref = "
                    ":root_session_ref"
                ),
                {"root_session_ref": run.root_session_ref, "now": now},
            )
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1, "
                    "experiment_attempt_count = experiment_attempt_count + 1, "
                    "active_experiment_run_count = active_experiment_run_count + 1 "
                    "WHERE singleton = 'owner'"
                )
            )
            self._feed.record(
                connection,
                "agent_runtime.experiment_replaced",
                {
                    "run_ref": run.run_ref,
                    "attempt_ref": attempt_ref,
                    "fence_ref": fence_ref,
                    "attempt_generation": int(run.attempt_generation) + 1,
                    "provider_operation_ref": operation_ref,
                    "provider_operation_generation": operation_generation,
                },
            )
        replacement = self.query_experiment_run(evaluation_attempt_ref)
        if replacement is None:
            raise OwnerConflict("experiment_replacement_missing_after_commit")
        return replacement

    def verify_attempt_execution_receipt(self, **values) -> str:
        return self._receipt_verifier.verify_attempt_execution_receipt(**values)

    def verify_target_run_admission_receipt(self, **values) -> None:
        self._receipt_verifier.verify_target_run_admission_receipt(**values)

    def verify_run_completion_receipt(self, **values) -> None:
        self._receipt_verifier.verify_run_completion_receipt(**values)

    def verify_deepfetch_execution_receipt(self, **values) -> None:
        self._receipt_verifier.verify_deepfetch_execution_receipt(**values)

    def verify_experiment_execution_receipt(
        self, **values
    ) -> ExperimentResultComponentManifest:
        return self._receipt_verifier.verify_experiment_execution_receipt(**values)

    def verify_writing_execution_receipt(self, **values) -> dict[str, object]:
        return self._receipt_verifier.verify_writing_execution_receipt(**values)


class SQLiteAgentRuntimeReceiptVerifier:
    """Narrow AR issuer verifier with optional AE provenance verification."""

    def __init__(
        self,
        database: Database,
        stage_request_verifier: StageRunRequestVerifier | None = None,
        writing_authorization_verifier: HumanResponseVerifier | None = None,
    ) -> None:
        self._database = database
        self._stage_request_verifier = stage_request_verifier
        self._writing_authorization_verifier = writing_authorization_verifier

    def verify_runtime_control_receipt(
        self,
        *,
        operation_ref: str,
        action: str,
        target: dict[str, object],
        receipt: dict[str, object],
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_operations WHERE operation_ref = "
                    ":operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
            compensated = connection.execute(
                text(
                    "SELECT operation_ref FROM ar_control_compensations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).first()
        if row is None or compensated is not None:
            raise OwnerConflict("runtime_control_receipt_invalid")
        persisted = _runtime_control_receipt(row)
        expected_target = {
            "quest_ref": row.quest_ref,
            "cycle_ref": row.cycle_ref,
            "question_ref": target.get("question_ref"),
            "epoch": int(row.epoch),
            "target_scope": row.target_scope,
        }
        for optional in ("run_ref", "target_question_ref", "prune_record_ref"):
            if target.get(optional) is not None:
                expected_target[optional] = target[optional]
        expected_hash = canonical_hash(
            {
                "issuer": AR_OWNER,
                "kind": "runtime_control",
                "subject_ref": operation_ref,
                "action": action,
                "target": expected_target,
                "source_stage": row.source_stage,
                "affected_question_refs_hash": row.affected_question_refs_hash,
                "affected_runs_hash": row.affected_runs_hash,
                "safe_points_hash": row.safe_points_hash,
            }
        )
        if (
            action != row.action
            or row.quest_ref != target.get("quest_ref")
            or row.cycle_ref != target.get("cycle_ref")
            or int(row.epoch) != target.get("epoch")
            or row.target_scope != target.get("target_scope")
            or row.run_ref != target.get("run_ref")
            or row.receipt_hash != expected_hash
            or persisted != receipt
        ):
            raise OwnerConflict("runtime_control_receipt_invalid")

    def verify_runtime_quiescence_receipt(
        self,
        *,
        operation_ref: str,
        target: dict[str, object],
        affected_question_refs: tuple[str, ...],
        receipt: dict[str, object],
    ) -> None:
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_control_operations WHERE operation_ref = "
                    ":operation_ref AND action = 'prune'"
                ),
                {"operation_ref": operation_ref},
            ).first()
            if row is not None:
                affected_runs = json.loads(row.affected_runs_json)
            else:
                affected_runs = []
        expected_questions = list(affected_question_refs)
        if (
            row is None
            or json.loads(row.affected_question_refs_json) != expected_questions
            or row.quest_ref != target.get("quest_ref")
            or row.cycle_ref != target.get("cycle_ref")
            or int(row.epoch) != target.get("epoch")
            or any(not item.get("safe_point_ref") for item in affected_runs)
            or _runtime_quiescence_receipt(row) != receipt
        ):
            raise OwnerConflict("runtime_quiescence_receipt_invalid")

    def verify_attempt_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        submission_ref: str,
        payload_hash: str,
        receipt: AcceptanceReceipt,
    ) -> str:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind
            not in {
                ATTEMPT_EXECUTION_RECEIPT_KIND,
                PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND,
                BUNDLE_ATTEMPT_EXECUTION_RECEIPT_KIND,
            }
            or receipt.subject_ref != submission_ref
        ):
            raise OwnerConflict("attempt_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT a.*, r.request_ref, r.cycle_ref, r.epoch, "
                    "r.context_pack_ref, r.context_pack_hash, "
                    "r.runtime_binding_json, r.runtime_binding_hash, "
                    "r.stage, r.request_receipt_ref, r.request_receipt_hash, "
                    "r.admission_hash, "
                    "s.native_session_ref FROM ar_stage_attempts a "
                    "JOIN ar_stage_runs r ON r.run_ref = a.run_ref "
                    "JOIN ar_stage_sessions s ON s.session_ref = a.root_session_ref "
                    "WHERE a.execution_receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            if row is not None:
                executed = _attempt_execution(row, row, row)
                _verify_provider_execution_chain(connection, row, executed)
        if row is None or (
            row.request_ref != request_ref
            or row.run_ref != run_ref
            or row.attempt_ref != attempt_ref
            or row.fence_ref != fence_ref
            or row.submission_ref != submission_ref
            or row.payload_hash != payload_hash
            or row.execution_receipt_hash != receipt.payload_hash
            or receipt.kind != _stage_execution_receipt_kind(row.stage)
        ):
            raise OwnerConflict("attempt_execution_receipt_invalid")
        outcome = executed.outcome
        _verify_persisted_successor_lineage(self._database, row, outcome)
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=row.request_ref,
                cycle_ref=row.cycle_ref,
                epoch=int(row.epoch),
                context_pack_ref=row.context_pack_ref,
                context_pack_hash=row.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=row.request_receipt_ref,
                    subject_ref=row.request_ref,
                    payload_hash=row.request_receipt_hash,
                ),
            )
        return executed.material_outcome_hash

    def verify_target_run_admission_receipt(
        self,
        *,
        target_ref: str,
        target_spec_hash: str,
        graph_ref: str,
        stage_request_ref: str,
        quest_ref: str,
        target_run_ref: str,
        evaluation_attempt_ref: str,
        execution_request_ref: str,
        definition_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != TARGET_RUN_ADMISSION_RECEIPT_KIND
        ):
            raise OwnerConflict("target_run_admission_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM ar_target_run_admissions WHERE receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            experiment = connection.execute(
                text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                {"run_ref": target_run_ref},
            ).first()
            dispatch_row = (
                None
                if row is None
                else connection.execute(
                    text(
                        "SELECT * FROM ar_bundle_dispatch_decisions WHERE "
                        "decision_ref = :decision_ref"
                    ),
                    {"decision_ref": row.dispatch_decision_ref},
                ).first()
            )
        if row is None or experiment is None or dispatch_row is None:
            raise OwnerConflict("target_run_admission_receipt_invalid")
        admission = _target_run_admission(row)
        dispatch = _bundle_dispatch_decision(dispatch_row)
        if (
            receipt.subject_ref != admission.admission_ref
            or receipt != admission.receipt
            or admission.target_ref != target_ref
            or admission.target_spec_hash != target_spec_hash
            or admission.graph_ref != graph_ref
            or admission.stage_request_ref != stage_request_ref
            or admission.quest_ref != quest_ref
            or admission.target_run_ref != target_run_ref
            or admission.evaluation_attempt_ref != evaluation_attempt_ref
            or admission.execution_request_ref != execution_request_ref
            or admission.definition_hash != definition_hash
            or admission.dispatch_decision_ref != dispatch.decision_ref
            or admission.dispatch_receipt_ref != dispatch.receipt.receipt_ref
            or admission.dispatch_receipt_hash != dispatch.receipt.payload_hash
            or dispatch.action != "dispatch"
            or dispatch.selected_target_ref != target_ref
            or dispatch.graph_ref != graph_ref
            or experiment.quest_ref != quest_ref
            or experiment.bundle_target_ref != target_ref
            or experiment.evaluation_attempt_ref != evaluation_attempt_ref
            or experiment.execution_request_ref != execution_request_ref
            or experiment.definition_hash != definition_hash
        ):
            raise OwnerConflict("target_run_admission_receipt_invalid")

    def verify_writing_execution_receipt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        final_markdown_hash: str,
        citations_hash: str,
        receipt: AcceptanceReceipt,
        quest_ref: str | None = None,
        snapshot_ref: str | None = None,
        snapshot_hash: str | None = None,
        allowed_source_version_refs: tuple[str, ...] | None = None,
        require_current: bool = False,
        require_authorized: bool = False,
    ) -> dict[str, object]:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != "writing_execution_completed"
        ):
            raise OwnerConflict("writing_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT executions.*, runs.runtime_binding_hash, "
                    "runs.quest_ref, runs.snapshot_ref, runs.snapshot_hash, "
                    "runs.snapshot_json, runs.status AS run_status, "
                    "runs.attempt_ref AS current_attempt_ref, "
                    "runs.fence_ref AS current_fence_ref FROM "
                    "ar_writing_executions AS executions JOIN ar_writing_runs AS "
                    "runs ON runs.run_ref = executions.run_ref WHERE "
                    "executions.receipt_ref = :receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        execution = _writing_execution_from_row(row)
        try:
            snapshot = decoded_object(row.snapshot_json) if row is not None else None
            source_rows = (
                snapshot.get("accepted_sources", [])
                if isinstance(snapshot, dict)
                else []
            )
            authoritative_sources = tuple(
                sorted(
                    {
                        cast(str, source["version_ref"])
                        for source in source_rows
                        if isinstance(source, dict)
                        and isinstance(source.get("version_ref"), str)
                    }
                )
            )
        except (KeyError, TypeError) as error:
            raise OwnerConflict("writing_execution_receipt_invalid") from error
        if (
            execution is None
            or row.run_ref != run_ref
            or row.attempt_ref != attempt_ref
            or row.fence_ref != fence_ref
            or row.final_markdown_hash != final_markdown_hash
            or row.citations_hash != citations_hash
            or receipt.subject_ref != row.execution_ref
            or receipt.payload_hash != row.receipt_hash
            or (quest_ref is not None and row.quest_ref != quest_ref)
            or (snapshot_ref is not None and row.snapshot_ref != snapshot_ref)
            or (snapshot_hash is not None and row.snapshot_hash != snapshot_hash)
            or (
                allowed_source_version_refs is not None
                and authoritative_sources != allowed_source_version_refs
            )
            or (
                require_current
                and (
                    row.run_status != "active"
                    or row.current_attempt_ref != attempt_ref
                    or row.current_fence_ref != fence_ref
                )
            )
        ):
            raise OwnerConflict("writing_execution_receipt_invalid")
        if require_authorized:
            if self._writing_authorization_verifier is None:
                raise OwnerConflict("writing_authorization_verifier_unavailable")
            self._writing_authorization_verifier.verify_broad_research_authorization(
                quest_ref=row.quest_ref
            )
        return {
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "fence_ref": row.fence_ref,
            "quest_ref": row.quest_ref,
            "snapshot_ref": row.snapshot_ref,
            "snapshot_hash": row.snapshot_hash,
            "allowed_source_version_refs": authoritative_sources,
            "final_markdown": execution.final_markdown,
            "final_markdown_hash": execution.final_markdown_hash,
            "citations": execution.citations,
            "citations_hash": execution.citations_hash,
            "execution_ref": execution.execution_ref,
        }

    def verify_run_completion_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str | None,
        outcome_ref: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != RUN_COMPLETION_RECEIPT_KIND
            or receipt.subject_ref != run_ref
        ):
            raise OwnerConflict("run_completion_receipt_issuer_invalid")
        with self._database.read() as connection:
            run = connection.execute(
                text(
                    "SELECT * FROM ar_stage_runs WHERE completion_receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
            if run is None:
                raise OwnerConflict("run_completion_receipt_invalid")
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": run.current_attempt_ref},
            ).first()
        if attempt is None or (
            run.request_ref != request_ref
            or run.run_ref != run_ref
            or (attempt_ref is not None and attempt.attempt_ref != attempt_ref)
            or run.outcome_ref != outcome_ref
            or run.status != "completed"
            or attempt.status != "completed"
            or attempt.decision_receipt_subject_ref != run.outcome_ref
            or run.completion_receipt_hash != receipt.payload_hash
            or run.completion_receipt_hash != _run_completion_receipt_hash(run, attempt)
        ):
            raise OwnerConflict("run_completion_receipt_invalid")
        _runtime_binding_from_row(run)
        if self._stage_request_verifier is not None:
            self._stage_request_verifier.verify_stage_run_request(
                request_ref=run.request_ref,
                cycle_ref=run.cycle_ref,
                epoch=int(run.epoch),
                context_pack_ref=run.context_pack_ref,
                context_pack_hash=run.context_pack_hash,
                receipt=AcceptanceReceipt(
                    issuer="advancement_engine",
                    kind="stage_run_request",
                    receipt_ref=run.request_receipt_ref,
                    subject_ref=run.request_ref,
                    payload_hash=run.request_receipt_hash,
                ),
            )

    def verify_deepfetch_execution_receipt(
        self,
        *,
        request_ref: str,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> None:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != DEEPFETCH_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != run_ref
        ):
            raise OwnerConflict("deepfetch_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT r.*, s.root_session_ref, s.native_session_ref, "
                    "a.attempt_ref AS joined_attempt_ref, "
                    "a.generation AS joined_generation, a.fence_ref, "
                    "a.status AS attempt_status, a.result_hash AS "
                    "attempt_result_hash FROM ar_deepfetch_runs r JOIN "
                    "ar_deepfetch_sessions s ON s.run_ref = r.run_ref JOIN "
                    "ar_deepfetch_attempts a ON a.attempt_ref = "
                    "r.current_attempt_ref WHERE r.execution_receipt_ref = "
                    ":receipt_ref"
                ),
                {"receipt_ref": receipt.receipt_ref},
            ).first()
        if row is None:
            raise OwnerConflict("deepfetch_execution_receipt_invalid")
        run = _deepfetch_run_from_row(row)
        expected_hash = _owner_receipt_hash(
            DEEPFETCH_EXECUTION_RECEIPT_KIND,
            run.run_ref,
            {
                "request_ref": run.request_ref,
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "attempt_generation": run.attempt_generation,
                "fence_ref": run.fence_ref,
                "native_session_ref": run.native_session_ref,
                "runtime_binding_hash": run.runtime_binding_hash,
                "result_hash": run.result_hash,
            },
        )
        if (
            run.status != "executed"
            or row.attempt_status != "executed"
            or run.request_ref != request_ref
            or run.run_ref != run_ref
            or run.attempt_ref != attempt_ref
            or run.fence_ref != fence_ref
            or run.result_hash != result_hash
            or row.attempt_result_hash != result_hash
            or row.execution_receipt_hash != receipt.payload_hash
            or receipt.payload_hash != expected_hash
        ):
            raise OwnerConflict("deepfetch_execution_receipt_invalid")

    def verify_experiment_execution_receipt(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        fence_ref: str,
        evaluation_attempt_ref: str,
        result_hash: str,
        receipt: AcceptanceReceipt,
    ) -> ExperimentResultComponentManifest:
        if (
            receipt.issuer != AR_OWNER
            or receipt.kind != EXPERIMENT_EXECUTION_RECEIPT_KIND
            or receipt.subject_ref != attempt_ref
        ):
            raise OwnerConflict("experiment_execution_receipt_issuer_invalid")
        with self._database.read() as connection:
            run = connection.execute(
                text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
                {"run_ref": run_ref},
            ).first()
            attempt = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_attempts WHERE attempt_ref = "
                    ":attempt_ref"
                ),
                {"attempt_ref": attempt_ref},
            ).first()
            event_rows = connection.execute(
                text(
                    "SELECT * FROM ar_experiment_events WHERE run_ref = "
                    ":run_ref AND attempt_ref = :attempt_ref ORDER BY sequence"
                ),
                {"run_ref": run_ref, "attempt_ref": attempt_ref},
            ).all()
        try:
            runtime_binding = (
                None
                if run is None
                else _experiment_runtime_binding(run.runtime_binding_json)
            )
            provider_result = ExperimentProviderResult.from_document(
                decoded_object(run.result_json) if run is not None else {}
            )
            result_manifest = experiment_result_component_manifest(
                provider_result,
                tuple(_experiment_event(row) for row in event_rows),
            )
        except (OwnerConflict, TypeError, ValueError) as error:
            raise OwnerConflict("experiment_execution_receipt_invalid") from error
        if (
            run is None
            or attempt is None
            or (
                run.status != "executed"
                or attempt.status != "executed"
                or run.attempt_ref != attempt_ref
                or run.fence_ref != fence_ref
                or attempt.fence_ref != fence_ref
                or run.evaluation_attempt_ref != evaluation_attempt_ref
                or runtime_binding is None
                or canonical_hash(runtime_binding.as_dict()) != run.runtime_binding_hash
                or runtime_binding.runner_bundle_hash != run.implementation_content_hash
                or run.result_hash != result_hash
                or attempt.receipt_ref != receipt.receipt_ref
                or attempt.receipt_hash != receipt.payload_hash
                or receipt.payload_hash
                != _experiment_execution_receipt_hash(
                    run,
                    attempt_ref,
                    fence_ref,
                    result_hash,
                    result_manifest,
                )
            )
        ):
            raise OwnerConflict("experiment_execution_receipt_invalid")
        return result_manifest


def _acquisition_runtime_binding(value: str) -> AcquisitionRuntimeBinding:
    try:
        payload = decoded_object(value)
        capabilities = payload["capability_bindings"]
        if (
            set(payload)
            != {
                "schema_ref",
                "provider_ref",
                "provider_version",
                "capability_bindings",
            }
            or payload["schema_ref"]
            != "meta-research/nature-downloader-runtime-binding/v1"
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
        ):
            raise TypeError("acquisition runtime binding")
        binding = AcquisitionRuntimeBinding(
            provider_ref=str(payload["provider_ref"]),
            provider_version=str(payload["provider_version"]),
            capability_bindings=tuple(capabilities),
        )
        validate_acquisition_runtime_binding(binding)
        return binding
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        AcquisitionUnavailable,
    ) as error:
        raise OwnerConflict("acquisition_session_invalid") from error


def _acquisition_session_from_row(row) -> AcquisitionSession:
    runtime_binding = _acquisition_runtime_binding(row.runtime_binding_json)
    try:
        config = decoded_object(row.config_json)
        evidence = (
            None if row.evidence_json is None else decoded_object(row.evidence_json)
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("acquisition_session_invalid") from error
    if (
        canonical_json(config) != row.config_json
        or canonical_hash(config) != row.config_hash
        or canonical_json(runtime_binding.as_dict()) != row.runtime_binding_json
        or canonical_hash(runtime_binding.as_dict()) != row.runtime_binding_hash
        or (
            evidence is None
            and (row.evidence_json is not None or row.evidence_hash is not None)
        )
        or (
            evidence is not None
            and (
                canonical_json(evidence) != row.evidence_json
                or canonical_hash(evidence) != row.evidence_hash
            )
        )
        or bool(row.slot_held) != (row.status in {"probing", "acquiring"})
    ):
        raise OwnerConflict("acquisition_session_invalid")
    return AcquisitionSession(
        session_ref=str(row.session_ref),
        initialization_id=str(row.initialization_id),
        quest_ref=None if row.quest_ref is None else str(row.quest_ref),
        status=str(row.status),
        config_hash=str(row.config_hash),
        mode=str(row.mode),
        browser_context_ref=(
            None if row.browser_context_ref is None else str(row.browser_context_ref)
        ),
        runtime_binding=runtime_binding,
        runtime_binding_hash=str(row.runtime_binding_hash),
        preflight_generation=int(row.preflight_generation),
        request_count=int(row.request_count),
        current_request_id=(
            None if row.current_request_id is None else str(row.current_request_id)
        ),
        slot_held=bool(row.slot_held),
        reason_code=None if row.reason_code is None else str(row.reason_code),
        evidence_hash=(None if row.evidence_hash is None else str(row.evidence_hash)),
    )


def _acquisition_request_from_json(value: str) -> AcquisitionBatchRequest:
    try:
        payload = decoded_object(value)
        raw_papers = payload["papers"]
        if set(payload) != {
            "schema_ref",
            "request_id",
            "route_policy",
            "papers",
        } or not isinstance(raw_papers, list):
            raise TypeError("acquisition request")
        papers = tuple(
            AcquisitionPaper(
                paper_id=str(item["paper_id"]),
                title=str(item["title"]),
                doi=None if item["doi"] is None else str(item["doi"]),
                arxiv_id=(None if item["arxiv_id"] is None else str(item["arxiv_id"])),
                source_urls=tuple(str(url) for url in item["source_urls"]),
            )
            for item in raw_papers
            if isinstance(item, dict)
            and set(item) == {"paper_id", "title", "doi", "arxiv_id", "source_urls"}
            and isinstance(item["source_urls"], list)
        )
        if len(papers) != len(raw_papers):
            raise TypeError("acquisition papers")
        request = AcquisitionBatchRequest(
            request_id=str(payload["request_id"]),
            route_policy=payload["route_policy"],
            papers=papers,
        )
        validate_batch_request(request)
        return request
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        AcquisitionUnavailable,
    ) as error:
        raise OwnerConflict("acquisition_request_invalid") from error


def _acquisition_results_from_json(
    results_json: str | None, results_hash: str | None
) -> tuple[AcquisitionItemResult, ...]:
    try:
        raw_results = json.loads(results_json) if results_json is not None else None
        if (
            not isinstance(raw_results, list)
            or canonical_json(raw_results) != results_json
            or canonical_hash(raw_results) != results_hash
        ):
            raise TypeError("acquisition results")
        return tuple(
            AcquisitionItemResult(
                paper_id=item["paper_id"],
                status=item["status"],
                path=item["path"],
                format=item["format"],
                failure=item["failure"],
            )
            for item in raw_results
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OwnerConflict("acquisition_result_invalid") from error


def _waiting_acquisition_item_bindings(
    results_json: str | None, results_hash: str | None
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "paper_id": result.paper_id,
            "item_hash": canonical_hash(result.as_dict()),
        }
        for result in _acquisition_results_from_json(results_json, results_hash)
        if result.status == "waiting_user"
    )


def _acquisition_target_matches_waiting_item(target: dict[str, object], row) -> bool:
    paper_id = target.get("acquisition_paper_id")
    item_hash = target.get("acquisition_item_hash")
    if not isinstance(paper_id, str) or not isinstance(item_hash, str):
        return False
    return any(
        item["paper_id"] == paper_id and item["item_hash"] == item_hash
        for item in _waiting_acquisition_item_bindings(
            row.results_json, row.results_hash
        )
    )


def _acquisition_format_for_material(media_type: str, display_name: str) -> str | None:
    normalized = media_type.split(";", 1)[0].strip().lower()
    suffix = Path(display_name).suffix.lower()
    if normalized == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if normalized in {"text/html", "application/xhtml+xml"} or suffix in {
        ".html",
        ".htm",
    }:
        return "html"
    if normalized in {"application/xml", "text/xml"} or suffix == ".xml":
        return "xml"
    return None


def _effective_acquisition_mode(route: dict[str, object]) -> str:
    kind = route.get("route")
    if kind == "institutional_browser_reconnected":
        return "oa_then_institution"
    if kind == "oa_only":
        return "oa_only"
    if kind == "accepted_material":
        return "provided_only"
    raise OwnerConflict("acquisition_resume_route_invalid")


def _store_provided_material(
    target_dir: Path, binding: dict[str, object], materialized_asset: object
) -> Path:
    content = getattr(materialized_asset, "content", None)
    if (
        not isinstance(content, bytes)
        or hashlib.sha256(content).hexdigest() != binding["content_hash"]
    ):
        raise OwnerConflict("acquisition_material_binding_invalid")
    target = target_dir / (
        "accepted-"
        + cast(str, binding["content_hash"])
        + "."
        + cast(str, binding["format"])
    )
    if target.is_symlink():
        raise OwnerConflict("acquisition_material_binding_invalid")
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as error:
            raise OwnerConflict("acquisition_material_binding_invalid") from error
        if hashlib.sha256(existing).hexdigest() != binding["content_hash"]:
            raise OwnerConflict("acquisition_material_binding_invalid")
        return target
    try:
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o600)
    except (FileExistsError, OSError) as error:
        raise OwnerConflict("acquisition_material_binding_invalid") from error
    return target


def _acquisition_execution_from_row(
    row,
    session_row,
    acquisition_private_root: Path,
) -> AcquisitionBatchExecution:
    request = _acquisition_request_from_json(row.request_json).bind_to_session(
        session_ref=str(row.session_ref),
        session_mode=str(session_row.mode),
        browser_context_ref=(
            None
            if session_row.browser_context_ref is None
            else str(session_row.browser_context_ref)
        ),
        provider_state_dir=acquisition_private_root / str(row.session_ref),
        target_dir=(
            acquisition_private_root
            / str(row.session_ref)
            / "requests"
            / str(row.request_id)
        ),
    )
    results = _acquisition_results_from_json(row.results_json, row.results_hash)
    return AcquisitionBatchExecution(
        request_id=str(row.request_id),
        session_ref=str(row.session_ref),
        status=str(row.status),
        request=request,
        results=results,
    )


def _failed_acquisition_item(paper_id: str, code: str) -> AcquisitionItemResult:
    return AcquisitionItemResult(
        paper_id=paper_id,
        status="missing",
        path=None,
        format=None,
        failure={"code": code, "detail": "Nature Downloader 未形成可接纳正文。"},
    )


def _acquisition_reconciliation_pending(
    results_json: str | None, results_hash: str | None
) -> bool:
    if results_json is None or results_hash is None:
        raise OwnerConflict("acquisition_result_invalid")
    try:
        results = json.loads(results_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise OwnerConflict("acquisition_result_invalid") from error
    if (
        not isinstance(results, list)
        or canonical_json(results) != results_json
        or canonical_hash(results) != results_hash
    ):
        raise OwnerConflict("acquisition_result_invalid")
    return any(
        isinstance(item, dict)
        and item.get("status") == "waiting_user"
        and isinstance(item.get("failure"), dict)
        and cast(dict[str, object], item["failure"]).get("code")
        == "acquisition_reconciliation_required"
        for item in results
    )


def _reconciliation_acquisition_item(paper_id: str) -> AcquisitionItemResult:
    return AcquisitionItemResult(
        paper_id=paper_id,
        status="waiting_user",
        path=None,
        format=None,
        failure={
            "code": "acquisition_reconciliation_required",
            "detail": (
                "既有下载操作尚未形成可验证终态；系统将先对账，不会重复启动下载。"
            ),
        },
    )


def _deepfetch_runtime_binding(value: str) -> DeepFetchRuntimeBinding:
    try:
        decoded = decoded_object(value)
        if (
            set(decoded)
            != {
                "schema_ref",
                "provider_ref",
                "provider_version",
                "model_ref",
                "harness_ref",
                "capability_bindings",
            }
            or decoded["schema_ref"] != "meta-research/deepfetch-runtime-binding/v1"
        ):
            raise TypeError("runtime binding")
        capabilities = decoded["capability_bindings"]
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            raise TypeError("capability bindings")
        binding = DeepFetchRuntimeBinding(
            provider_ref=str(decoded["provider_ref"]),
            provider_version=str(decoded["provider_version"]),
            model_ref=str(decoded["model_ref"]),
            harness_ref=str(decoded["harness_ref"]),
            capability_bindings=tuple(capabilities),
        )
        validate_runtime_binding(binding)
        return binding
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        DeepFetchUnavailable,
    ) as error:
        raise OwnerConflict("deepfetch_runtime_binding_invalid") from error


def _deepfetch_run_from_row(row) -> DeepFetchRun:
    runtime_binding = _deepfetch_runtime_binding(row.runtime_binding_json)
    provider_operation_generation = int(row.provider_operation_generation)
    provider_operation_ref = str(row.provider_operation_ref)
    if (
        canonical_json(runtime_binding.as_dict()) != row.runtime_binding_json
        or canonical_hash(runtime_binding.as_dict()) != row.runtime_binding_hash
    ):
        raise OwnerConflict("deepfetch_runtime_binding_invalid")
    if provider_operation_ref != (
        f"{row.run_ref}:deepfetch:{provider_operation_generation}"
    ):
        raise OwnerConflict("deepfetch_provider_operation_invalid")
    result: dict[str, object] | None = None
    receipt: AcceptanceReceipt | None = None
    if row.result_json is not None:
        try:
            result = decoded_object(row.result_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OwnerConflict("deepfetch_result_invalid") from error
        if (
            canonical_json(result) != row.result_json
            or canonical_hash(result) != row.result_hash
        ):
            raise OwnerConflict("deepfetch_result_invalid")
    if row.execution_receipt_ref is not None:
        receipt = AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=DEEPFETCH_EXECUTION_RECEIPT_KIND,
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.run_ref,
            payload_hash=row.execution_receipt_hash,
        )
    attempt_ref = getattr(row, "joined_attempt_ref", None)
    fence_ref = getattr(row, "fence_ref", None)
    if row.status in {"running", "executed"} and (
        attempt_ref is None or fence_ref is None
    ):
        raise OwnerConflict("deepfetch_attempt_invalid")
    return DeepFetchRun(
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        correlation_ref=row.correlation_ref,
        status=row.status,
        attempt_ref=attempt_ref,
        attempt_generation=int(row.attempt_generation),
        provider_operation_ref=provider_operation_ref,
        provider_operation_generation=provider_operation_generation,
        root_session_ref=row.root_session_ref,
        native_session_ref=row.native_session_ref,
        fence_ref=fence_ref,
        runtime_binding=runtime_binding,
        runtime_binding_hash=row.runtime_binding_hash,
        result=result,
        result_hash=row.result_hash,
        execution_receipt=receipt,
        failure_code=row.failure_code,
    )


def _owner_receipt_hash(
    kind: str, subject_ref: str, bindings: dict[str, object]
) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AR_OWNER,
            "kind": kind,
            "subject_ref": subject_ref,
            "bindings": bindings,
        }
    )


def _stage_execution_schema(stage: str) -> str:
    if stage == "idea":
        return ATTEMPT_EXECUTION_SCHEMA
    if stage == "plan":
        return PLAN_ATTEMPT_EXECUTION_SCHEMA
    if stage == "bundle":
        return BUNDLE_ATTEMPT_EXECUTION_SCHEMA
    raise OwnerConflict("stage_run_integrity_invalid")


def _stage_execution_receipt_kind(stage: str) -> str:
    if stage == "idea":
        return ATTEMPT_EXECUTION_RECEIPT_KIND
    if stage == "plan":
        return PLAN_ATTEMPT_EXECUTION_RECEIPT_KIND
    if stage == "bundle":
        return BUNDLE_ATTEMPT_EXECUTION_RECEIPT_KIND
    raise OwnerConflict("stage_run_integrity_invalid")


def _stage_decision_receipt_kind(stage: str, *, accepted: bool) -> str:
    if stage == "idea":
        return "idea_outcome_accepted" if accepted else "idea_outcome_rejected"
    if stage == "plan":
        return "formal_plan_accepted" if accepted else "formal_plan_rejected"
    if stage == "bundle":
        return "target_graph_accepted" if accepted else "target_graph_rejected"
    raise OwnerConflict("stage_run_integrity_invalid")


def _stage_material_hash(stage: str, outcome: dict[str, object]) -> str:
    if stage == "idea":
        return material_outcome_hash(outcome)
    if stage == "plan":
        return material_plan_hash(outcome)
    if stage == "bundle":
        return material_target_plan_hash(outcome)
    raise OwnerConflict("stage_run_integrity_invalid")


def _execution_bindings(row) -> dict[str, object]:
    return {
        "request_ref": row.request_ref,
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "submission_ref": row.submission_ref,
        "native_session_ref": row.native_session_ref,
        "runtime_binding_hash": row.runtime_binding_hash,
        "payload_hash": row.payload_hash,
        "material_outcome_hash": row.material_outcome_hash,
        "predecessor_attempt_ref": row.predecessor_attempt_ref,
        "predecessor_outcome_hash": row.predecessor_outcome_hash,
        "predecessor_material_outcome_hash": (row.predecessor_material_outcome_hash),
        "predecessor_rejection_receipt_ref": (row.predecessor_rejection_receipt_ref),
        "predecessor_rejection_receipt_subject_ref": (
            row.predecessor_rejection_receipt_subject_ref
        ),
        "predecessor_rejection_receipt_hash": (row.predecessor_rejection_receipt_hash),
    }


def _verify_execution_row(
    row,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    _runtime_binding_from_row(row)
    try:
        payload = decoded_object(row.payload_json)
        outcome = payload["outcome"]
        reviewed_draft = payload["reviewed_draft"]
        review = payload["review"]
        if (
            set(payload) != {"schema_ref", "outcome", "reviewed_draft", "review"}
            or payload["schema_ref"] != _stage_execution_schema(row.stage)
            or not isinstance(outcome, dict)
            or not isinstance(reviewed_draft, dict)
            or not isinstance(review, dict)
        ):
            raise TypeError("payload")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("attempt_execution_payload_invalid") from error
    predecessor_values = (
        row.predecessor_outcome_hash,
        row.predecessor_material_outcome_hash,
        row.predecessor_rejection_receipt_ref,
        row.predecessor_rejection_receipt_subject_ref,
        row.predecessor_rejection_receipt_hash,
    )
    lineage_invalid = (
        row.predecessor_attempt_ref is None
        and (
            int(row.generation) != 1
            or any(value is not None for value in predecessor_values)
        )
    ) or (
        row.predecessor_attempt_ref is not None
        and (
            int(row.generation) <= 1
            or any(
                not isinstance(value, str) or not value for value in predecessor_values
            )
        )
    )
    if (
        not row.native_session_ref
        or lineage_invalid
        or canonical_json(payload) != row.payload_json
        or canonical_hash(payload) != row.payload_hash
        or _stage_material_hash(row.stage, outcome) != row.material_outcome_hash
        or row.execution_receipt_hash
        != _owner_receipt_hash(
            _stage_execution_receipt_kind(row.stage),
            row.submission_ref,
            _execution_bindings(row),
        )
    ):
        raise OwnerConflict("attempt_execution_payload_invalid")
    return outcome, reviewed_draft, review


def _primary_draft(run, attempt, session) -> IdeaPrimaryDraft | None:
    values = (
        attempt.primary_draft_json,
        attempt.primary_draft_hash,
        attempt.primary_adapter_kind,
        attempt.primary_recorded_at,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values) or session.native_session_ref is None:
        raise OwnerConflict("idea_primary_draft_invalid")
    try:
        draft = decoded_object(attempt.primary_draft_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("idea_primary_draft_invalid") from error
    if (
        canonical_json(draft) != attempt.primary_draft_json
        or canonical_hash(draft) != attempt.primary_draft_hash
        or not isinstance(attempt.primary_adapter_kind, str)
        or not attempt.primary_adapter_kind
        or len(attempt.primary_adapter_kind) > 64
    ):
        raise OwnerConflict("idea_primary_draft_invalid")
    return IdeaPrimaryDraft(
        request_ref=run.request_ref,
        run_ref=run.run_ref,
        attempt_ref=attempt.attempt_ref,
        fence_ref=attempt.fence_ref,
        native_session_ref=session.native_session_ref,
        runtime_binding_hash=run.runtime_binding_hash,
        draft=draft,
        draft_hash=attempt.primary_draft_hash,
        adapter_kind=attempt.primary_adapter_kind,
    )


def _provider_invocation_request_hash(
    *,
    invocation_ref: str,
    phase: str,
    request_ref: str,
    run_ref: str,
    attempt_ref: str,
    generation: int,
    root_session_ref: str,
    fence_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    runtime_binding_hash: str,
    predecessor_attempt_ref: str | None,
    stage: str = "idea",
) -> str:
    return canonical_hash(
        {
            "schema_ref": f"meta-research/{stage}-provider-invocation/v1",
            "invocation_ref": invocation_ref,
            "phase": phase,
            "request_ref": request_ref,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "attempt_generation": generation,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
            "context_pack_ref": context_pack_ref,
            "context_pack_hash": context_pack_hash,
            "runtime_binding_hash": runtime_binding_hash,
            "predecessor_attempt_ref": predecessor_attempt_ref,
        }
    )


def _primary_provider_response_hash(
    *,
    native_session_ref: str,
    draft: dict[str, object],
    adapter_kind: str,
    stage: str = "idea",
) -> str:
    return canonical_hash(
        {
            "schema_ref": f"meta-research/{stage}-primary-provider-response/v1",
            "native_session_ref": native_session_ref,
            "draft": draft,
            "adapter_kind": adapter_kind,
        }
    )


def _review_provider_response_hash(
    *,
    native_session_ref: str,
    reviewed_draft: dict[str, object],
    outcome: dict[str, object],
    review: dict[str, object],
    stage: str = "idea",
) -> str:
    return canonical_hash(
        {
            "schema_ref": f"meta-research/{stage}-review-provider-response/v1",
            "native_session_ref": native_session_ref,
            "reviewed_draft": reviewed_draft,
            "outcome": outcome,
            "review": review,
        }
    )


def _insert_provider_invocations(
    connection,
    *,
    request_ref: str,
    run_ref: str,
    attempt_ref: str,
    generation: int,
    root_session_ref: str,
    fence_ref: str,
    context_pack_ref: str,
    context_pack_hash: str,
    runtime_binding_hash: str,
    predecessor_attempt_ref: str | None,
    prepared_at: float,
    stage: str = "idea",
    operation_refs: dict[str, str] | None = None,
) -> None:
    for phase in ("primary", "review"):
        invocation_ref = new_ref(f"{stage}_{phase}_invocation")
        operation_ref = (
            new_ref(f"{stage}_{phase}_operation")
            if operation_refs is None
            else operation_refs[phase]
        )
        connection.execute(
            text(
                "INSERT INTO ar_stage_provider_invocations (invocation_ref, "
                "operation_ref, run_ref, attempt_ref, fence_ref, phase, request_hash, "
                "runtime_binding_hash, status, prepared_at) VALUES "
                "(:invocation_ref, :operation_ref, :run_ref, :attempt_ref, :fence_ref, :phase, "
                ":request_hash, :runtime_binding_hash, 'prepared', :prepared_at)"
            ),
            {
                "invocation_ref": invocation_ref,
                "operation_ref": operation_ref,
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "fence_ref": fence_ref,
                "phase": phase,
                "request_hash": _provider_invocation_request_hash(
                    invocation_ref=invocation_ref,
                    phase=phase,
                    request_ref=request_ref,
                    run_ref=run_ref,
                    attempt_ref=attempt_ref,
                    generation=generation,
                    root_session_ref=root_session_ref,
                    fence_ref=fence_ref,
                    context_pack_ref=context_pack_ref,
                    context_pack_hash=context_pack_hash,
                    runtime_binding_hash=runtime_binding_hash,
                    predecessor_attempt_ref=predecessor_attempt_ref,
                    stage=stage,
                ),
                "runtime_binding_hash": runtime_binding_hash,
                "prepared_at": prepared_at,
            },
        )


def _provider_invocations(
    connection, run, attempt, fence
) -> tuple[IdeaProviderInvocation, IdeaProviderInvocation]:
    rows = connection.execute(
        text(
            "SELECT * FROM ar_stage_provider_invocations WHERE "
            "attempt_ref = :attempt_ref ORDER BY phase"
        ),
        {"attempt_ref": attempt.attempt_ref},
    ).all()
    by_phase = {row.phase: row for row in rows}
    if len(rows) != 2 or set(by_phase) != {"primary", "review"}:
        raise OwnerConflict("idea_provider_invocation_invalid")
    values: list[IdeaProviderInvocation] = []
    for phase in ("primary", "review"):
        row = by_phase[phase]
        operation_ref = row.operation_ref
        expected_hash = _provider_invocation_request_hash(
            invocation_ref=row.invocation_ref,
            phase=phase,
            request_ref=run.request_ref,
            run_ref=run.run_ref,
            attempt_ref=attempt.attempt_ref,
            generation=int(attempt.generation),
            root_session_ref=attempt.root_session_ref,
            fence_ref=fence.fence_ref,
            context_pack_ref=run.context_pack_ref,
            context_pack_hash=run.context_pack_hash,
            runtime_binding_hash=run.runtime_binding_hash,
            predecessor_attempt_ref=attempt.predecessor_attempt_ref,
            stage=run.stage,
        )
        if (
            row.run_ref != run.run_ref
            or not isinstance(operation_ref, str)
            or not operation_ref
            or row.fence_ref != fence.fence_ref
            or row.request_hash != expected_hash
            or row.runtime_binding_hash != run.runtime_binding_hash
            or row.status not in {"prepared", "completed"}
            or row.status == "prepared"
            and (row.response_hash is not None or row.completed_at is not None)
            or row.status == "completed"
            and (not _is_sha256(row.response_hash) or row.completed_at is None)
        ):
            raise OwnerConflict("idea_provider_invocation_invalid")
        values.append(
            IdeaProviderInvocation(
                invocation_ref=row.invocation_ref,
                operation_ref=operation_ref,
                request_ref=run.request_ref,
                run_ref=run.run_ref,
                attempt_ref=attempt.attempt_ref,
                fence_ref=fence.fence_ref,
                phase=phase,
                request_hash=row.request_hash,
                runtime_binding_hash=row.runtime_binding_hash,
                status=row.status,
                response_hash=row.response_hash,
            )
        )
    return values[0], values[1]


def _complete_provider_invocation(
    connection,
    run,
    attempt,
    fence,
    *,
    phase: str,
    response_hash: str,
) -> IdeaProviderInvocation:
    primary, review = _provider_invocations(connection, run, attempt, fence)
    invocation = primary if phase == "primary" else review
    if invocation.status == "completed":
        if invocation.response_hash != response_hash:
            raise OwnerConflict("idea_provider_response_conflict")
        return invocation
    now = time.time()
    connection.execute(
        text(
            "UPDATE ar_stage_provider_invocations SET status = 'completed', "
            "response_hash = :response_hash, completed_at = :completed_at WHERE "
            "invocation_ref = :invocation_ref AND status = 'prepared'"
        ),
        {
            "response_hash": response_hash,
            "completed_at": now,
            "invocation_ref": invocation.invocation_ref,
        },
    )
    return replace(
        invocation,
        status="completed",
        response_hash=response_hash,
    )


def _verify_provider_execution_chain(
    connection, row, execution: AttemptExecution
) -> None:
    primary, review = _provider_invocations(connection, row, row, row)
    checkpoint = _primary_draft(row, row, row)
    if checkpoint is None:
        raise OwnerConflict("idea_primary_draft_required")
    if (
        primary.status != "completed"
        or primary.response_hash
        != _primary_provider_response_hash(
            native_session_ref=checkpoint.native_session_ref,
            draft=checkpoint.draft,
            adapter_kind=checkpoint.adapter_kind,
            stage=row.stage,
        )
        or review.status != "completed"
        or review.response_hash
        != _review_provider_response_hash(
            native_session_ref=execution.native_session_ref,
            reviewed_draft=execution.reviewed_draft,
            outcome=execution.outcome,
            review=execution.review,
            stage=row.stage,
        )
    ):
        raise OwnerConflict("idea_provider_invocation_invalid")


def _attempt_execution(run, attempt, session) -> AttemptExecution:
    row = (
        _ExecutionRow(run, attempt, session)
        if run is not attempt or attempt is not session
        else attempt
    )
    outcome, reviewed_draft, review = _verify_execution_row(row)
    runtime_binding = _runtime_binding_from_row(row)
    return AttemptExecution(
        stage=row.stage,
        request_ref=row.request_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        submission_ref=row.submission_ref,
        native_session_ref=row.native_session_ref,
        runtime_binding=runtime_binding,
        runtime_binding_hash=row.runtime_binding_hash,
        payload_hash=row.payload_hash,
        payload_json=row.payload_json,
        material_outcome_hash=row.material_outcome_hash,
        outcome=outcome,
        reviewed_draft=reviewed_draft,
        reviewed_draft_hash=canonical_hash(reviewed_draft),
        review=review,
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=_stage_execution_receipt_kind(row.stage),
            receipt_ref=row.execution_receipt_ref,
            subject_ref=row.submission_ref,
            payload_hash=row.execution_receipt_hash,
        ),
        predecessor_attempt_ref=row.predecessor_attempt_ref,
        predecessor_outcome_hash=row.predecessor_outcome_hash,
        predecessor_material_outcome_hash=(row.predecessor_material_outcome_hash),
        predecessor_rejection_receipt=(
            None
            if row.predecessor_attempt_ref is None
            else AcceptanceReceipt(
                issuer="research_graph",
                kind=_stage_decision_receipt_kind(row.stage, accepted=False),
                receipt_ref=row.predecessor_rejection_receipt_ref,
                subject_ref=row.predecessor_rejection_receipt_subject_ref,
                payload_hash=row.predecessor_rejection_receipt_hash,
            )
        ),
    )


class _ExecutionRow:
    """Small adapter over separately queried run/attempt/session rows."""

    def __init__(self, run, attempt, session) -> None:
        for name in (
            "attempt_ref",
            "generation",
            "fence_ref",
            "predecessor_attempt_ref",
            "predecessor_outcome_hash",
            "predecessor_material_outcome_hash",
            "predecessor_rejection_receipt_ref",
            "predecessor_rejection_receipt_subject_ref",
            "predecessor_rejection_receipt_hash",
            "submission_ref",
            "payload_json",
            "payload_hash",
            "material_outcome_hash",
            "execution_receipt_ref",
            "execution_receipt_hash",
        ):
            setattr(self, name, getattr(attempt, name))
        self.run_ref = attempt.run_ref
        self.request_ref = run.request_ref
        self.native_session_ref = session.native_session_ref
        for name in (
            "cycle_ref",
            "stage",
            "epoch",
            "context_pack_ref",
            "context_pack_hash",
            "runtime_binding_json",
            "runtime_binding_hash",
            "request_receipt_ref",
            "request_receipt_hash",
            "admission_hash",
        ):
            setattr(self, name, getattr(run, name))


def _successor_execution_lineage(
    connection,
    run,
    attempt,
    session,
    *,
    native_session_ref: str,
    outcome: dict[str, object],
) -> tuple[dict[str, object], AcceptanceReceipt | None, str | None]:
    empty = {
        "predecessor_attempt_ref": None,
        "predecessor_outcome_hash": None,
        "predecessor_material_outcome_hash": None,
        "predecessor_rejection_receipt_ref": None,
        "predecessor_rejection_receipt_subject_ref": None,
        "predecessor_rejection_receipt_hash": None,
    }
    if attempt.predecessor_attempt_ref is None:
        if int(attempt.generation) != 1 or any(
            getattr(attempt, key) is not None
            for key in empty
            if key != "predecessor_attempt_ref"
        ):
            raise OwnerConflict("attempt_successor_lineage_invalid")
        return empty, None, None

    predecessor = connection.execute(
        text(
            "SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref "
            "AND run_ref = :run_ref"
        ),
        {
            "attempt_ref": attempt.predecessor_attempt_ref,
            "run_ref": run.run_ref,
        },
    ).first()
    if predecessor is None or (
        predecessor.status != "rejected"
        or predecessor.run_ref != attempt.run_ref
        or predecessor.root_session_ref != attempt.root_session_ref
        or attempt.root_session_ref != session.session_ref
        or int(attempt.generation) != int(predecessor.generation) + 1
        or session.native_session_ref != native_session_ref
        or predecessor.submission_ref is None
        or predecessor.decision_receipt_ref is None
        or predecessor.decision_receipt_subject_ref is None
        or predecessor.decision_receipt_hash is None
    ):
        raise OwnerConflict("attempt_successor_lineage_invalid")
    predecessor_execution = _attempt_execution(run, predecessor, session)
    predecessor_outcome_hash = canonical_hash(predecessor_execution.outcome)
    predecessor_material_hash = predecessor_execution.material_outcome_hash
    if _stage_material_hash(run.stage, outcome) == predecessor_material_hash:
        raise OwnerConflict("attempt_successor_outcome_unchanged")
    receipt = AcceptanceReceipt(
        issuer="research_graph",
        kind=_stage_decision_receipt_kind(run.stage, accepted=False),
        receipt_ref=predecessor.decision_receipt_ref,
        subject_ref=predecessor.decision_receipt_subject_ref,
        payload_hash=predecessor.decision_receipt_hash,
    )
    lineage = {
        "predecessor_attempt_ref": predecessor.attempt_ref,
        "predecessor_outcome_hash": predecessor_outcome_hash,
        "predecessor_material_outcome_hash": predecessor_material_hash,
        "predecessor_rejection_receipt_ref": receipt.receipt_ref,
        "predecessor_rejection_receipt_subject_ref": receipt.subject_ref,
        "predecessor_rejection_receipt_hash": receipt.payload_hash,
    }
    stored = {
        key: getattr(attempt, key)
        for key in lineage
        if key != "predecessor_attempt_ref"
    }
    if attempt.status == "running":
        if any(value is not None for value in stored.values()):
            raise OwnerConflict("attempt_successor_lineage_invalid")
    elif any(stored[key] != lineage[key] for key in stored):
        raise OwnerConflict("attempt_successor_lineage_invalid")
    return lineage, receipt, predecessor.submission_ref


def _verify_persisted_successor_lineage(
    database: Database,
    row,
    outcome: dict[str, object],
) -> None:
    if row.predecessor_attempt_ref is None:
        return
    with database.read() as connection:
        predecessor = connection.execute(
            text(
                "SELECT p.*, r.request_ref, r.cycle_ref, r.stage, r.epoch, "
                "r.context_pack_ref, r.context_pack_hash, "
                "r.runtime_binding_json, r.runtime_binding_hash, "
                "r.request_receipt_ref, r.request_receipt_hash, "
                "r.admission_hash, s.native_session_ref FROM "
                "ar_stage_attempts p JOIN ar_stage_runs r ON r.run_ref = p.run_ref "
                "JOIN ar_stage_sessions s ON s.session_ref = p.root_session_ref "
                "WHERE p.attempt_ref = :attempt_ref"
            ),
            {"attempt_ref": row.predecessor_attempt_ref},
        ).first()
    if predecessor is None:
        raise OwnerConflict("attempt_successor_lineage_invalid")
    predecessor_outcome, _predecessor_draft, _predecessor_review = (
        _verify_execution_row(predecessor)
    )
    if (
        predecessor.status != "rejected"
        or predecessor.run_ref != row.run_ref
        or predecessor.root_session_ref != row.root_session_ref
        or predecessor.native_session_ref != row.native_session_ref
        or int(row.generation) != int(predecessor.generation) + 1
        or canonical_hash(predecessor_outcome) != row.predecessor_outcome_hash
        or _stage_material_hash(row.stage, predecessor_outcome)
        != row.predecessor_material_outcome_hash
        or _stage_material_hash(row.stage, outcome)
        == row.predecessor_material_outcome_hash
        or predecessor.decision_receipt_ref != row.predecessor_rejection_receipt_ref
        or predecessor.decision_receipt_subject_ref
        != row.predecessor_rejection_receipt_subject_ref
        or predecessor.decision_receipt_hash != row.predecessor_rejection_receipt_hash
    ):
        raise OwnerConflict("attempt_successor_lineage_invalid")


def _run_completion_bindings(run, attempt) -> dict[str, object]:
    return {
        "request_ref": run.request_ref,
        "run_ref": run.run_ref,
        "attempt_ref": attempt.attempt_ref,
        "runtime_binding_hash": run.runtime_binding_hash,
        "outcome_ref": run.outcome_ref,
        "decision_receipt_ref": attempt.decision_receipt_ref,
        "decision_receipt_subject_ref": attempt.decision_receipt_subject_ref,
        "decision_receipt_hash": attempt.decision_receipt_hash,
    }


def _run_completion_receipt_hash(run, attempt) -> str:
    return _owner_receipt_hash(
        RUN_COMPLETION_RECEIPT_KIND,
        run.run_ref,
        _run_completion_bindings(run, attempt),
    )


def _run_completion(run, attempt) -> RunCompletion:
    if (
        run.status != "completed"
        or attempt.status != "completed"
        or not run.outcome_ref
        or attempt.decision_receipt_subject_ref != run.outcome_ref
        or run.completion_receipt_hash != _run_completion_receipt_hash(run, attempt)
    ):
        raise OwnerConflict("run_completion_invalid")
    return RunCompletion(
        request_ref=run.request_ref,
        run_ref=run.run_ref,
        attempt_ref=attempt.attempt_ref,
        outcome_ref=run.outcome_ref,
        decision_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind=_stage_decision_receipt_kind(run.stage, accepted=True),
            receipt_ref=attempt.decision_receipt_ref,
            subject_ref=run.outcome_ref,
            payload_hash=attempt.decision_receipt_hash,
        ),
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=RUN_COMPLETION_RECEIPT_KIND,
            receipt_ref=run.completion_receipt_ref,
            subject_ref=run.run_ref,
            payload_hash=run.completion_receipt_hash,
        ),
    )


def _load_stage_fence(connection, run_ref: str, attempt_ref: str, fence_ref: str):
    run = connection.execute(
        text("SELECT * FROM ar_stage_runs WHERE run_ref = :run_ref"),
        {"run_ref": run_ref},
    ).first()
    attempt = connection.execute(
        text("SELECT * FROM ar_stage_attempts WHERE attempt_ref = :attempt_ref"),
        {"attempt_ref": attempt_ref},
    ).first()
    fence = connection.execute(
        text("SELECT * FROM ar_execution_fences WHERE fence_ref = :fence_ref"),
        {"fence_ref": fence_ref},
    ).first()
    if run is None or attempt is None or fence is None:
        raise OwnerConflict("attempt_fence_invalid")
    _runtime_binding_from_row(run)
    session = connection.execute(
        text("SELECT * FROM ar_stage_sessions WHERE session_ref = :session_ref"),
        {"session_ref": run.root_session_ref},
    ).first()
    if session is None or (
        attempt.run_ref != run_ref
        or attempt.root_session_ref != session.session_ref
        or fence.run_ref != run_ref
        or fence.attempt_ref != attempt_ref
    ):
        raise OwnerConflict("attempt_fence_invalid")
    return run, attempt, session, fence


def _require_current_fence(
    run, attempt, fence, attempt_status: str, fence_status: str
) -> None:
    expected_run_status = (
        "running" if attempt_status == "running" else "awaiting_acceptance"
    )
    if (
        run.status != expected_run_status
        or run.current_attempt_ref != attempt.attempt_ref
        or run.current_fence_ref != fence.fence_ref
        or attempt.fence_ref != fence.fence_ref
        or attempt.status != attempt_status
        or fence.status != fence_status
        or int(attempt.generation) != int(fence.generation)
    ):
        raise OwnerConflict("attempt_fence_stale")


def _validate_stage_idempotency_key(value: str) -> None:
    if not value or len(value) > 128:
        raise OwnerConflict("idempotency_key_invalid")


def _writing_markdown_hash(markdown: str) -> str:
    if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > 2 * 1024 * 1024:
        raise OwnerConflict("writing_markdown_invalid")
    return canonical_hash(
        {"media_type": "text/markdown; charset=utf-8", "content": markdown}
    )


def _writing_receipt_from_value(value: object) -> AcceptanceReceipt:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    } or value.get("status") != "accepted":
        raise OwnerConflict("writing_revision_lineage_invalid")
    receipt = AcceptanceReceipt(
        issuer=cast(str, value["issuer"]),
        kind=cast(str, value["kind"]),
        receipt_ref=cast(str, value["receipt_ref"]),
        subject_ref=cast(str, value["subject_ref"]),
        payload_hash=cast(str, value["payload_hash"]),
    )
    if any(
        not isinstance(item, str) or not item or len(item) > 128
        for item in (
            receipt.issuer,
            receipt.kind,
            receipt.receipt_ref,
            receipt.subject_ref,
        )
    ) or not isinstance(receipt.payload_hash, str) or len(receipt.payload_hash) != 64:
        raise OwnerConflict("writing_revision_lineage_invalid")
    return receipt


def _validate_writing_citations_for_snapshot(
    citations: tuple[dict[str, str], ...], snapshot: dict[str, object]
) -> str:
    sources = snapshot.get("accepted_sources")
    if not isinstance(citations, tuple) or not isinstance(sources, list) or len(
        citations
    ) > 512:
        raise OwnerConflict("writing_citations_invalid")
    allowed = {
        source.get("version_ref")
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("version_ref"), str)
    }
    if len(allowed) > 512:
        raise OwnerConflict("writing_citations_invalid")
    seen: set[str] = set()
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {
            "citation_ref",
            "source_version_ref",
            "locator",
            "claim",
            "source_quote",
        }:
            raise OwnerConflict("writing_citations_invalid")
        citation_ref = citation.get("citation_ref")
        source_ref = citation.get("source_version_ref")
        locator = citation.get("locator")
        claim = citation.get("claim")
        source_quote = citation.get("source_quote")
        if (
            not isinstance(citation_ref, str)
            or not citation_ref.strip()
            or len(citation_ref) > 128
            or citation_ref in seen
            or not isinstance(source_ref, str)
            or source_ref not in allowed
            or not isinstance(locator, str)
            or not locator.strip()
            or len(locator) > 2000
            or not isinstance(claim, str)
            or not claim.strip()
            or len(claim) > 8000
            or not isinstance(source_quote, str)
            or not source_quote.strip()
            or len(source_quote) > 8000
        ):
            raise OwnerConflict("writing_citations_invalid")
        seen.add(citation_ref)
    return canonical_hash(list(citations))


def _validate_writing_review_contract(
    *,
    review: dict[str, object],
    run,
    attempt,
    checkpoint: WritingCheckpoint,
    reviewed_markdown: str,
    final_markdown: str,
    citations: tuple[dict[str, str], ...],
    final_hash: str,
    citations_hash: str,
) -> None:
    if set(review) != {
        "review_mode",
        "reviewer_agent_ref",
        "review_task_hash",
        "reviewed_markdown_hash",
        "findings",
        "dispositions",
        "final_markdown_hash",
        "citations_hash",
        "review_hash",
    }:
        raise OwnerConflict("writing_review_invalid")
    reviewer = review.get("reviewer_agent_ref")
    findings = review.get("findings")
    dispositions = review.get("dispositions")
    claimed_review_hash = review.get("review_hash")
    expected_review_task_hash = writing_child_review_task_hash(
        run_ref=run.run_ref,
        provider_job_ref=attempt.provider_job_ref,
        root_session_ref=run.root_session_ref,
        primary_session_ref=checkpoint.native_session_ref,
        intent_hash=run.intent_hash,
        snapshot_hash=run.snapshot_hash,
        predecessor_version_ref=attempt.predecessor_version_ref,
        predecessor_markdown_hash=attempt.predecessor_markdown_hash,
        feedback_hash=attempt.feedback_hash,
        reviewed_markdown_hash=checkpoint.markdown_hash,
        reviewed_citations_hash=checkpoint.citations_hash,
    )
    review_basis = dict(review)
    review_basis.pop("review_hash")
    if (
        review.get("review_mode") != "harness_child_agent"
        or not isinstance(reviewer, str)
        or not reviewer.strip()
        or len(reviewer) > 128
        or reviewer in {run.root_session_ref, run.native_session_ref}
        or review.get("review_task_hash") != expected_review_task_hash
        or review.get("reviewed_markdown_hash") != checkpoint.markdown_hash
        or reviewed_markdown != checkpoint.markdown
        or review.get("final_markdown_hash") != final_hash
        or review.get("citations_hash") != citations_hash
        or claimed_review_hash != canonical_hash(review_basis)
        or not isinstance(findings, list)
        or not isinstance(dispositions, list)
        or len(findings) > 128
        or len(dispositions) != len(findings)
    ):
        raise OwnerConflict("writing_review_invalid")
    for finding, disposition in zip(findings, dispositions, strict=True):
        if (
            not isinstance(finding, dict)
            or not isinstance(disposition, dict)
            or set(finding) != {"category", "finding"}
            or set(disposition) != {"category", "action", "reason"}
            or finding.get("category") != disposition.get("category")
            or disposition.get("action") not in {"revised", "not_adopted"}
            or any(
                not isinstance(value, str) or not value.strip()
                for value in (
                    finding.get("category"),
                    finding.get("finding"),
                    disposition.get("reason"),
                )
            )
        ):
            raise OwnerConflict("writing_review_invalid")
    if any(
        disposition.get("action") == "revised" for disposition in dispositions
    ) and final_hash == checkpoint.markdown_hash and citations == checkpoint.citations:
        raise OwnerConflict("writing_review_revision_not_material")
    if (
        attempt.predecessor_markdown_hash is not None
        and final_hash == attempt.predecessor_markdown_hash
    ):
        raise OwnerConflict("writing_feedback_revision_not_material")


def _current_writing_run_row(connection, run_ref: str, attempt_ref: str, fence_ref: str):
    row = connection.execute(
        text("SELECT * FROM ar_writing_runs WHERE run_ref = :run_ref"),
        {"run_ref": run_ref},
    ).first()
    attempt = connection.execute(
        text(
            "SELECT * FROM ar_writing_attempts WHERE attempt_ref = :attempt_ref"
        ),
        {"attempt_ref": attempt_ref},
    ).first()
    if (
        row is None
        or attempt is None
        or row.status != "active"
        or row.attempt_ref != attempt_ref
        or row.fence_ref != fence_ref
        or attempt.run_ref != run_ref
        or int(attempt.generation) != int(row.attempt_generation)
        or attempt.root_session_ref != row.root_session_ref
        or attempt.native_session_ref != row.native_session_ref
        or attempt.fence_ref != fence_ref
        or attempt.status not in {"admitted", "running", "completed"}
    ):
        raise OwnerConflict("writing_fence_stale")
    return row


def _writing_command_replay(
    connection, idempotency_key: str, command_kind: str, request_hash: str
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM ar_writing_commands WHERE idempotency_key = "
            ":idempotency_key AND command_kind = :command_kind"
        ),
        {"idempotency_key": idempotency_key, "command_kind": command_kind},
    ).first()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise OwnerConflict("idempotency_conflict")
    return row.result_ref


def _record_writing_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
    result_attempt_ref: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO ar_writing_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, result_attempt_ref, recorded_at) VALUES "
            "(:idempotency_key, :command_kind, :request_hash, :result_ref, "
            ":result_attempt_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "result_attempt_ref": result_attempt_ref,
            "recorded_at": time.time(),
        },
    )


def _writing_checkpoint_from_row(row) -> WritingCheckpoint | None:
    if row is None:
        return None
    try:
        citations_value = json.loads(row.citations_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("writing_checkpoint_integrity_invalid") from error
    if (
        not isinstance(citations_value, list)
        or any(not isinstance(item, dict) for item in citations_value)
        or canonical_hash(citations_value) != row.citations_hash
        or _writing_markdown_hash(row.markdown) != row.markdown_hash
    ):
        raise OwnerConflict("writing_checkpoint_integrity_invalid")
    return WritingCheckpoint(
        checkpoint_ref=row.checkpoint_ref,
        attempt_ref=row.attempt_ref,
        markdown=row.markdown,
        markdown_hash=row.markdown_hash,
        citations=tuple(cast(dict[str, str], item) for item in citations_value),
        citations_hash=row.citations_hash,
        native_session_ref=row.native_session_ref,
    )


def _writing_execution_from_row(row) -> WritingExecution | None:
    if row is None:
        return None
    try:
        citations_value = json.loads(row.citations_json)
        review = decoded_object(row.review_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("writing_execution_integrity_invalid") from error
    payload = {
        "schema_ref": "meta-research/writing-execution/v1",
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "final_markdown_hash": row.final_markdown_hash,
        "citations_hash": row.citations_hash,
        "review_hash": row.review_hash,
        "runtime_binding_hash": row.runtime_binding_hash,
    }
    expected_receipt_hash = canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AR_OWNER,
            "kind": "writing_execution_completed",
            "subject_ref": row.execution_ref,
            "payload_hash": row.payload_hash,
        }
    )
    if (
        not isinstance(citations_value, list)
        or any(not isinstance(item, dict) for item in citations_value)
        or canonical_hash(citations_value) != row.citations_hash
        or canonical_hash(review) != row.review_hash
        or _writing_markdown_hash(row.reviewed_markdown)
        != row.reviewed_markdown_hash
        or _writing_markdown_hash(row.final_markdown) != row.final_markdown_hash
        or canonical_hash(payload) != row.payload_hash
        or expected_receipt_hash != row.receipt_hash
    ):
        raise OwnerConflict("writing_execution_integrity_invalid")
    return WritingExecution(
        execution_ref=row.execution_ref,
        attempt_ref=row.attempt_ref,
        reviewed_markdown=row.reviewed_markdown,
        final_markdown=row.final_markdown,
        final_markdown_hash=row.final_markdown_hash,
        citations=tuple(cast(dict[str, str], item) for item in citations_value),
        citations_hash=row.citations_hash,
        review=review,
        review_hash=row.review_hash,
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind="writing_execution_completed",
            receipt_ref=row.receipt_ref,
            subject_ref=row.execution_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _validated_runtime_binding(
    binding: IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding,
    *,
    stage: str | None = None,
) -> tuple[IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding, str, str]:
    expected = {
        "idea": (IdeaRuntimeBinding, IDEA_RUNTIME_BINDING_SCHEMA),
        "plan": (PlanRuntimeBinding, PLAN_RUNTIME_BINDING_SCHEMA),
        "bundle": (BundleRuntimeBinding, BUNDLE_RUNTIME_BINDING_SCHEMA),
    }
    expected_type, expected_schema = expected.get(
        stage or "idea", (IdeaRuntimeBinding, IDEA_RUNTIME_BINDING_SCHEMA)
    )
    if not isinstance(binding, expected_type):
        raise OwnerConflict("idea_runtime_binding_invalid")
    value = binding.as_dict()
    if (
        set(value)
        != {
            "schema_ref",
            "packaged_skill_bundle_hash",
            "instruction_set_hash",
            "model_ref",
            "harness_adapter_ref",
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        }
        or binding.schema_ref != expected_schema
        or not _is_sha256(binding.packaged_skill_bundle_hash)
        or not _is_sha256(binding.instruction_set_hash)
        or not _runtime_ref(binding.model_ref)
        or not _runtime_ref(binding.harness_adapter_ref)
    ):
        raise OwnerConflict("idea_runtime_binding_invalid")
    for values in (
        binding.mcp_bindings,
        binding.capability_bindings,
        binding.resource_bindings,
    ):
        if (
            not isinstance(values, tuple)
            or not all(_runtime_ref(item) for item in values)
            or len(values) != len(set(values))
        ):
            raise OwnerConflict("idea_runtime_binding_invalid")
    if (
        binding.mcp_bindings
        or any(
            capability not in _IDEA_SAFE_CAPABILITIES
            for capability in binding.capability_bindings
        )
        or any(
            not resource.startswith(_IDEA_SAFE_RESOURCE_PREFIXES)
            or "\n" in resource
            or "\r" in resource
            or "../" in resource
            for resource in binding.resource_bindings
        )
    ):
        raise OwnerConflict("idea_runtime_binding_unauthorized")
    binding_json = canonical_json(value)
    return binding, binding_json, canonical_hash(value)


def _resolved_reviewed_draft(
    outcome: dict[str, object],
    review: dict[str, object],
    reviewed_draft: dict[str, object] | None,
) -> dict[str, object]:
    if reviewed_draft is not None:
        if not isinstance(reviewed_draft, dict):
            raise OwnerConflict("reviewed_draft_invalid")
        return reviewed_draft
    # Compatibility for pre-v2 callers is unambiguous only when the final
    # outcome is itself the reviewed draft. Revised production executions fail
    # closed until their full draft is supplied.
    if review.get("reviewed_draft_hash") != canonical_hash(outcome):
        raise OwnerConflict("reviewed_draft_missing")
    return outcome


def _validate_attempt_review_for_write(
    review: dict[str, object], *, native_session_ref: str, stage: str = "idea"
) -> str:
    """Accept only the child-agent review contract for new AR executions.

    Historical v1 reviews remain readable from their immutable execution
    payloads.  They are not a production write format: their
    ``reviewer_session_ref`` encoded the retired extra-Session topology.
    """

    if stage == "idea" and review.get("schema_ref") == IDEA_REVIEW_SCHEMA_V1_REF:
        raise OwnerConflict("attempt_review_legacy_read_only")
    expected_schema = {
        "idea": IDEA_REVIEW_SCHEMA_REF,
        "plan": PLAN_REVIEW_SCHEMA_REF,
        "bundle": TARGET_PLAN_REVIEW_SCHEMA_REF,
    }.get(stage)
    if expected_schema is None:
        raise OwnerConflict("stage_run_integrity_invalid")
    reviewer_agent_ref = review.get("reviewer_agent_ref")
    if (
        review.get("schema_ref") != expected_schema
        or review.get("review_mode") != "harness_child_agent"
        or not isinstance(reviewer_agent_ref, str)
        or not reviewer_agent_ref.strip()
        or len(reviewer_agent_ref) > 512
        or reviewer_agent_ref == native_session_ref
        or review.get("independent") is not True
        or review.get("advisory_only") is not True
    ):
        raise OwnerConflict("attempt_review_independence_invalid")
    return reviewer_agent_ref


def _runtime_binding_from_row(
    row,
) -> IdeaRuntimeBinding | PlanRuntimeBinding | BundleRuntimeBinding:
    try:
        value = decoded_object(row.runtime_binding_json)
        if set(value) != {
            "schema_ref",
            "packaged_skill_bundle_hash",
            "instruction_set_hash",
            "model_ref",
            "harness_adapter_ref",
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        }:
            raise TypeError("runtime binding")
        for field in (
            "mcp_bindings",
            "capability_bindings",
            "resource_bindings",
        ):
            if not isinstance(value[field], list):
                raise TypeError(field)
        binding_type = {
            "idea": IdeaRuntimeBinding,
            "plan": PlanRuntimeBinding,
            "bundle": BundleRuntimeBinding,
        }.get(row.stage)
        if binding_type is None:
            raise TypeError("stage")
        binding = binding_type(
            packaged_skill_bundle_hash=value["packaged_skill_bundle_hash"],
            instruction_set_hash=value["instruction_set_hash"],
            model_ref=value["model_ref"],
            harness_adapter_ref=value["harness_adapter_ref"],
            mcp_bindings=tuple(value["mcp_bindings"]),
            capability_bindings=tuple(value["capability_bindings"]),
            resource_bindings=tuple(value["resource_bindings"]),
            schema_ref=value["schema_ref"],
        )
        binding, binding_json, binding_hash = _validated_runtime_binding(
            binding, stage=row.stage
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("idea_runtime_binding_invalid") from error
    expected_admission_hash = canonical_hash(
        {
            "command": f"admit_{row.stage}_stage",
            "request_ref": row.request_ref,
            "cycle_ref": row.cycle_ref,
            "stage": row.stage,
            "epoch": int(row.epoch),
            "context_pack_ref": row.context_pack_ref,
            "context_pack_hash": row.context_pack_hash,
            "request_receipt": AcceptanceReceipt(
                issuer="advancement_engine",
                kind="stage_run_request",
                receipt_ref=row.request_receipt_ref,
                subject_ref=row.request_ref,
                payload_hash=row.request_receipt_hash,
            ).as_public_dict(),
            "runtime_binding": binding.as_dict(),
            "runtime_binding_hash": binding_hash,
        }
    )
    if (
        binding_json != row.runtime_binding_json
        or binding_hash != row.runtime_binding_hash
        or expected_admission_hash != row.admission_hash
    ):
        raise OwnerConflict("idea_runtime_binding_invalid")
    return binding


def _runtime_ref(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _stage_command_replay(
    connection, idempotency_key: str, command_kind: str, request_hash: str
) -> str | None:
    row = connection.execute(
        text(
            "SELECT * FROM ar_stage_commands WHERE idempotency_key = :idempotency_key"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if row is not None:
        if row.command_kind != command_kind or row.request_hash != request_hash:
            raise OwnerConflict("idempotency_conflict")
        return row.result_ref
    host_snapshot = connection.execute(
        text(
            "SELECT 1 FROM ar_host_capability_snapshots WHERE idempotency_key = "
            ":idempotency_key UNION ALL SELECT 1 FROM "
            "ar_host_compute_observation_claims WHERE idempotency_key = "
            ":idempotency_key LIMIT 1"
        ),
        {"idempotency_key": idempotency_key},
    ).first()
    if host_snapshot is not None:
        raise OwnerConflict("idempotency_conflict")
    return None


def _query_stage_command(
    database: Database,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
) -> str | None:
    with database.read() as connection:
        return _stage_command_replay(
            connection, idempotency_key, command_kind, request_hash
        )


def _blocked_runtime_work_aliases(
    blocked_waiters: tuple[dict[str, object], ...],
) -> set[str]:
    """Resolve exact Owner work identities from waiter assertions.

    A waiter reference is an audit identity, not necessarily the identity of the
    work it blocks.  Preserve it as a compatibility alias, but derive the
    authoritative exclusions from the waiter target owned by the requester.
    """

    aliases: set[str] = set()
    target_keys = {
        "run_ref": "stage_run",
        "stage_run_ref": "stage_run",
        "request_ref": "stage_request",
        "stage_request_ref": "stage_request",
        "acquisition_request_id": "acquisition_request",
    }
    for waiter in blocked_waiters:
        waiter_ref = cast(str, waiter["waiter_ref"])
        aliases.add(waiter_ref)
        target = cast(dict[str, object], waiter["target_assertion"])
        for key, prefix in target_keys.items():
            value = target.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 128:
                raise OwnerConflict("human_request_waiter_target_invalid")
            aliases.add(value)
            aliases.add(f"{prefix}:{value}")
        work_ref = target.get("work_ref")
        if work_ref is not None:
            if not isinstance(work_ref, str) or not work_ref or len(work_ref) > 128:
                raise OwnerConflict("human_request_waiter_target_invalid")
            aliases.add(work_ref)
            work_kind = target.get("work_kind")
            if work_kind in {"stage_run", "stage_request", "acquisition_request"}:
                aliases.add(f"{work_kind}:{work_ref}")
    return aliases


def _record_stage_command(
    connection,
    idempotency_key: str,
    command_kind: str,
    request_hash: str,
    result_ref: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO ar_stage_commands (idempotency_key, command_kind, "
            "request_hash, result_ref, recorded_at) VALUES (:idempotency_key, "
            ":command_kind, :request_hash, :result_ref, :recorded_at)"
        ),
        {
            "idempotency_key": idempotency_key,
            "command_kind": command_kind,
            "request_hash": request_hash,
            "result_ref": result_ref,
            "recorded_at": time.time(),
        },
    )


def _observation_from_row(row) -> HostComputeObservation:
    try:
        capabilities = decoded_object(row.capabilities_json)
        devices_value = capabilities.get("devices")
        if not isinstance(devices_value, list):
            raise TypeError("devices")
        devices = tuple(
            HostComputeDevice(
                uuid=device["uuid"],
                name=device["name"],
                memory_total_mib=device["memory_total_mib"],
            )
            for device in devices_value
            if isinstance(device, dict)
        )
        if len(devices) != len(devices_value):
            raise TypeError("device")
        snapshot = HostComputeSnapshot(
            status=row.status,
            observed_at=float(row.observed_at),
            devices=devices,
            adapter_kind=row.adapter_kind,
            reason_code=row.reason_code,
        )
        _validate_probe_snapshot(snapshot)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerConflict("host_compute_snapshot_invalid") from error
    if canonical_hash(capabilities) != row.capabilities_hash:
        raise OwnerConflict("host_compute_snapshot_invalid")
    return HostComputeObservation(
        snapshot_ref=row.snapshot_ref,
        status=snapshot.status,
        observed_at=snapshot.observed_at,
        devices=snapshot.devices,
        adapter_kind=snapshot.adapter_kind,
        capabilities_hash=row.capabilities_hash,
        reason_code=snapshot.reason_code,
    )


def _experiment_runtime_binding(value: str) -> ExperimentRuntimeBinding:
    try:
        document = decoded_object(value)
        capabilities = document["capability_bindings"]
        resources = document["resource_bindings"]
        if not isinstance(capabilities, list) or not isinstance(resources, list):
            raise TypeError("bindings")
        binding = ExperimentRuntimeBinding(
            schema_ref=str(document["schema_ref"]),
            runner_bundle_hash=str(document["runner_bundle_hash"]),
            adapter_ref=str(document["adapter_ref"]),
            interpreter_ref=str(document["interpreter_ref"]),
            capability_bindings=tuple(str(item) for item in capabilities),
            resource_bindings=tuple(str(item) for item in resources),
        )
        if canonical_json(binding.as_dict()) != value:
            raise TypeError("canonical binding")
        return binding
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("experiment_runtime_binding_invalid") from error


def _current_experiment_execution(
    connection,
    *,
    run_ref: str,
    attempt_ref: str,
    fence_ref: str,
    required_status: str,
):
    run = connection.execute(
        text("SELECT * FROM ar_experiment_runs WHERE run_ref = :run_ref"),
        {"run_ref": run_ref},
    ).first()
    if run is None:
        raise OwnerConflict("experiment_run_not_found")
    if run.attempt_ref != attempt_ref or run.fence_ref != fence_ref:
        raise OwnerConflict("experiment_fence_stale")
    if run.status != required_status:
        raise OwnerConflict("experiment_execution_state_invalid")
    attempt = connection.execute(
        text("SELECT * FROM ar_experiment_attempts WHERE attempt_ref = :attempt_ref"),
        {"attempt_ref": attempt_ref},
    ).first()
    if attempt is None or (
        attempt.run_ref != run_ref
        or attempt.fence_ref != fence_ref
        or attempt.status != required_status
    ):
        raise OwnerConflict("experiment_execution_state_invalid")
    return run


def _append_experiment_event(
    connection,
    *,
    run_ref: str,
    attempt_ref: str,
    fence_ref: str,
    kind: str,
    payload: dict[str, object],
    observed_at: float,
) -> int:
    last = connection.execute(
        text(
            "SELECT MAX(sequence) AS last_sequence FROM ar_experiment_events "
            "WHERE run_ref = :run_ref AND attempt_ref = :attempt_ref"
        ),
        {"run_ref": run_ref, "attempt_ref": attempt_ref},
    ).first()
    sequence = (
        1 if last is None or last.last_sequence is None else int(last.last_sequence) + 1
    )
    payload_json = canonical_json(payload)
    connection.execute(
        text(
            "INSERT INTO ar_experiment_events (event_ref, run_ref, attempt_ref, "
            "fence_ref, sequence, kind, payload_json, payload_hash, observed_at) "
            "VALUES (:event_ref, :run_ref, :attempt_ref, :fence_ref, :sequence, "
            ":kind, :payload_json, :payload_hash, :observed_at)"
        ),
        {
            "event_ref": new_ref("experiment_event"),
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
            "sequence": sequence,
            "kind": kind,
            "payload_json": payload_json,
            "payload_hash": canonical_hash(payload),
            "observed_at": observed_at,
        },
    )
    return sequence


def _insert_replacement_attempt(
    connection,
    run_ref: str,
    generation: int,
    root_session_ref: str,
    now: float,
) -> tuple[str, str]:
    attempt_ref = new_ref("experiment_execution_attempt")
    fence_ref = new_ref("experiment_fence")
    try:
        TypedExecutionFence(
            run_ref=run_ref,
            attempt_ref=attempt_ref,
            generation=generation,
            root_session_ref=root_session_ref,
            fence_ref=fence_ref,
        ).validate()
    except ProviderSupervisorError as error:
        raise OwnerConflict("experiment_replacement_invalid") from error
    connection.execute(
        text(
            "INSERT INTO ar_experiment_attempts (attempt_ref, run_ref, "
            "generation, root_session_ref, fence_ref, status, created_at) VALUES "
            "(:attempt_ref, :run_ref, :generation, :root_session_ref, "
            ":fence_ref, 'admitted', :created_at)"
        ),
        {
            "attempt_ref": attempt_ref,
            "run_ref": run_ref,
            "generation": generation,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
            "created_at": now,
        },
    )
    return attempt_ref, fence_ref


def _bundle_dispatch_decision(row) -> BundleDispatchDecision:
    try:
        frontier_value = json.loads(row.frontier_json)
        state = decoded_object(row.state_json)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise OwnerConflict("bundle_dispatch_decision_invalid") from error
    if not isinstance(frontier_value, list) or any(
        not isinstance(item, dict) for item in frontier_value
    ):
        raise OwnerConflict("bundle_dispatch_decision_invalid")
    frontier = tuple(cast(dict[str, object], item) for item in frontier_value)
    decision = {
        "schema_ref": "meta-research/bundle-dispatch-decision/v1",
        "run_ref": row.run_ref,
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "native_session_ref": row.native_session_ref,
        "graph_ref": row.graph_ref,
        "generation": int(row.generation),
        "frontier_hash": row.frontier_hash,
        "state_hash": row.state_hash,
        "action": row.action,
        "selected_target_ref": row.selected_target_ref,
        "rationale": row.rationale,
    }
    bindings = {**decision, "decision_hash": row.decision_hash}
    if (
        canonical_json(list(frontier)) != row.frontier_json
        or canonical_hash(list(frontier)) != row.frontier_hash
        or canonical_json(state) != row.state_json
        or canonical_hash(state) != row.state_hash
        or canonical_hash(decision) != row.decision_hash
        or row.action not in {"dispatch", "wait", "replan_required"}
        or (
            row.action == "dispatch"
            and sum(
                item.get("target_ref") == row.selected_target_ref for item in frontier
            )
            != 1
        )
        or (row.action != "dispatch" and row.selected_target_ref is not None)
        or row.receipt_hash
        != _owner_receipt_hash(BUNDLE_DISPATCH_RECEIPT_KIND, row.decision_ref, bindings)
    ):
        raise OwnerConflict("bundle_dispatch_decision_invalid")
    return BundleDispatchDecision(
        decision_ref=row.decision_ref,
        run_ref=row.run_ref,
        attempt_ref=row.attempt_ref,
        fence_ref=row.fence_ref,
        native_session_ref=row.native_session_ref,
        graph_ref=row.graph_ref,
        generation=int(row.generation),
        frontier=frontier,
        state=state,
        action=row.action,
        selected_target_ref=row.selected_target_ref,
        rationale=row.rationale,
        decision_hash=row.decision_hash,
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=BUNDLE_DISPATCH_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.decision_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _target_run_admission(row) -> TargetRunAdmission:
    bindings = {
        "target_ref": row.target_ref,
        "target_spec_hash": row.target_spec_hash,
        "graph_ref": row.graph_ref,
        "stage_request_ref": row.stage_request_ref,
        "quest_ref": row.quest_ref,
        "target_run_ref": row.target_run_ref,
        "evaluation_attempt_ref": row.evaluation_attempt_ref,
        "execution_request_ref": row.execution_request_ref,
        "definition_hash": row.definition_hash,
        "dispatch_decision_ref": row.dispatch_decision_ref,
        "dispatch_receipt_ref": row.dispatch_receipt_ref,
        "dispatch_receipt_hash": row.dispatch_receipt_hash,
        "human_request_ref": row.human_request_ref,
        "human_waiter_ref": row.human_waiter_ref,
        "human_waiter_generation": (
            None
            if row.human_waiter_generation is None
            else int(row.human_waiter_generation)
        ),
        "human_authorization_receipt_ref": row.human_authorization_receipt_ref,
    }
    request_hash = canonical_hash(
        {
            "command": "admit_target_run",
            **{
                key: value
                for key, value in bindings.items()
                if not key.startswith("dispatch_")
            },
        }
    )
    if row.request_hash != request_hash or row.receipt_hash != _owner_receipt_hash(
        TARGET_RUN_ADMISSION_RECEIPT_KIND, row.admission_ref, bindings
    ):
        raise OwnerConflict("target_run_admission_invalid")
    return TargetRunAdmission(
        admission_ref=row.admission_ref,
        target_ref=row.target_ref,
        target_spec_hash=row.target_spec_hash,
        graph_ref=row.graph_ref,
        stage_request_ref=row.stage_request_ref,
        quest_ref=row.quest_ref,
        target_run_ref=row.target_run_ref,
        evaluation_attempt_ref=row.evaluation_attempt_ref,
        execution_request_ref=row.execution_request_ref,
        definition_hash=row.definition_hash,
        dispatch_decision_ref=row.dispatch_decision_ref,
        dispatch_receipt_ref=row.dispatch_receipt_ref,
        dispatch_receipt_hash=row.dispatch_receipt_hash,
        human_request_ref=row.human_request_ref,
        human_waiter_ref=row.human_waiter_ref,
        human_waiter_generation=bindings["human_waiter_generation"],
        human_authorization_receipt_ref=row.human_authorization_receipt_ref,
        receipt=AcceptanceReceipt(
            issuer=AR_OWNER,
            kind=TARGET_RUN_ADMISSION_RECEIPT_KIND,
            receipt_ref=row.receipt_ref,
            subject_ref=row.admission_ref,
            payload_hash=row.receipt_hash,
        ),
    )


def _experiment_execution_receipt_hash(
    run,
    attempt_ref: str,
    fence_ref: str,
    result_hash: str,
    result_manifest: ExperimentResultComponentManifest,
) -> str:
    return canonical_hash(
        {
            "schema_ref": RECEIPT_SCHEMA,
            "issuer": AR_OWNER,
            "kind": EXPERIMENT_EXECUTION_RECEIPT_KIND,
            "subject_ref": attempt_ref,
            "bindings": {
                "run_ref": run.run_ref,
                "attempt_ref": attempt_ref,
                "attempt_generation": int(run.attempt_generation),
                "root_session_ref": run.root_session_ref,
                "fence_ref": fence_ref,
                "execution_request_ref": run.execution_request_ref,
                "provider_operation_ref": run.provider_operation_ref,
                "provider_operation_generation": int(run.provider_operation_generation),
                "execution_request_receipt_ref": run.execution_request_receipt_ref,
                "execution_request_receipt_hash": run.execution_request_receipt_hash,
                "implementation_version_ref": run.implementation_version_ref,
                "implementation_content_hash": run.implementation_content_hash,
                "evaluation_attempt_ref": run.evaluation_attempt_ref,
                "variant_run_ref": run.variant_run_ref,
                "variant_input_binding_ref": run.variant_input_binding_ref,
                "variant_input_hash": run.variant_input_hash,
                "measurement_input_binding_ref": run.measurement_input_binding_ref,
                "measurement_input_hash": run.measurement_input_hash,
                "runtime_binding_hash": run.runtime_binding_hash,
                "result_hash": result_hash,
                "result_component_manifest_hash": canonical_hash(
                    result_manifest.as_dict()
                ),
            },
        }
    )


def _experiment_event(row) -> dict[str, object]:
    try:
        payload = decoded_object(row.payload_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("experiment_event_invalid") from error
    if (
        canonical_json(payload) != row.payload_json
        or canonical_hash(payload) != row.payload_hash
    ):
        raise OwnerConflict("experiment_event_invalid")
    return {
        "event_ref": row.event_ref,
        "sequence": int(row.sequence),
        "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref,
        "kind": row.kind,
        "payload": payload,
        "observed_at": float(row.observed_at),
    }


def _controlled_runs_for_target(
    connection,
    target: dict[str, object],
    action: str,
    *,
    affected_question_refs: tuple[str, ...] | None = None,
):
    if action in {"prune", "restore"}:
        if affected_question_refs is None:
            raise OwnerConflict("question_control_affected_set_required")
        if target.get("question_ref") not in affected_question_refs:
            return []
    target_scope = target["target_scope"]
    if target_scope == "run":
        rows = connection.execute(
            text(
                "SELECT * FROM ar_run_controls WHERE quest_ref = :quest_ref AND "
                "run_ref = :run_ref"
            ),
            {"quest_ref": target["quest_ref"], "run_ref": target["run_ref"]},
        ).all()
        if not rows:
            raise OwnerConflict("managed_run_not_found")
        return rows
    stage_clause = " AND run_kind LIKE '%_stage'" if target_scope == "stage" else ""
    return connection.execute(
        text(
            "SELECT * FROM ar_run_controls WHERE quest_ref = :quest_ref AND "
            "cycle_ref = :cycle_ref AND epoch = :epoch"
            f"{stage_clause} ORDER BY run_kind, run_ref"
        ),
        {
            "quest_ref": target["quest_ref"],
            "cycle_ref": target["cycle_ref"],
            "epoch": target["epoch"],
        },
    ).all()


def _validate_formal_run_control_scope(
    target: dict[str, object], action: str, rows: list[object]
) -> None:
    if (
        target["target_scope"] == "run"
        and action == "cancel"
        and any(_is_formal_stage_run_kind(cast(str, row.run_kind)) for row in rows)
    ):
        # A formal StageRun is bound to an AE StageRunRequest.  Terminating only
        # the AR side would leave the current Epoch/request permanently active
        # with no admissible replacement.  Stage/cycle scope crosses the AE seam
        # and can issue a successor Epoch while keeping the old Run terminal.
        raise OwnerConflict("formal_stage_run_cancel_requires_stage_scope")


def _validated_control_source_stage(action: str, source_stage: str | None) -> str | None:
    if action == "normal_switch":
        if source_stage is None:
            return None
        if source_stage not in FORMAL_STAGES:
            raise OwnerConflict("runtime_control_source_stage_invalid")
        return source_stage
    if source_stage is not None:
        raise OwnerConflict("runtime_control_source_stage_invalid")
    return None


def _managed_run_document(
    row, safe_point: dict[str, object] | None = None
) -> dict[str, object]:
    value = {
        "run_ref": row.run_ref,
        "run_kind": row.run_kind,
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "epoch": None if row.epoch is None else int(row.epoch),
        "status": row.status,
        "attempt_ref": row.attempt_ref,
        "root_session_ref": row.root_session_ref,
        "fence_ref": row.fence_ref,
        "control_revision": int(row.control_revision),
        "safe_point_ref": row.safe_point_ref,
        "terminal_reason": row.terminal_reason,
        "cleanup_status": row.cleanup_status,
        "updated_at": float(row.updated_at),
    }
    if safe_point is not None:
        value["safe_point"] = safe_point
    return value


def _managed_run_reservation(row) -> dict[str, object]:
    """Freeze only AR-owned identities needed to close a control CAS."""

    return {
        "run_ref": row.run_ref,
        "run_kind": row.run_kind,
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "epoch": None if row.epoch is None else int(row.epoch),
        "status": row.status,
        "attempt_ref": row.attempt_ref,
        "root_session_ref": row.root_session_ref,
        "fence_ref": row.fence_ref,
        "control_revision": int(row.control_revision),
    }


def _prepared_control_reservations_for_run(connection, run_ref: str) -> tuple[object, ...]:
    reservations = connection.execute(
        text(
            "SELECT action, source_stage, affected_runs_json, affected_runs_hash "
            "FROM ar_control_reservations WHERE status = 'prepared'"
        )
    ).all()
    matching: list[object] = []
    for reservation in reservations:
        try:
            affected = json.loads(reservation.affected_runs_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict("runtime_control_reservation_invalid") from error
        if (
            not isinstance(affected, list)
            or canonical_hash(affected) != reservation.affected_runs_hash
            or not all(isinstance(item, dict) for item in affected)
        ):
            raise OwnerConflict("runtime_control_reservation_invalid")
        if any(item.get("run_ref") == run_ref for item in affected):
            matching.append(reservation)
    return tuple(matching)


def _prepared_control_affects_run(connection, run_ref: str) -> bool:
    return bool(_prepared_control_reservations_for_run(connection, run_ref))


def _provider_quiescence_requested(connection, run_ref: str) -> bool:
    return any(
        reservation.action in {"pause", "prune"}
        or (
            reservation.action == "normal_switch"
            and reservation.source_stage == "bundle"
        )
        for reservation in _prepared_control_reservations_for_run(connection, run_ref)
    )


def _assert_provider_derivation_not_quiescing(connection, run_ref: str) -> None:
    if _provider_quiescence_requested(connection, run_ref):
        raise OwnerConflict("runtime_quiescence_requested")


def _same_managed_run_identities(
    reserved: list[dict[str, object]], current: list[dict[str, object]]
) -> bool:
    identity_fields = (
        "run_ref",
        "run_kind",
        "quest_ref",
        "cycle_ref",
        "epoch",
        "attempt_ref",
        "root_session_ref",
        "fence_ref",
    )
    return [
        {field: item.get(field) for field in identity_fields} for item in reserved
    ] == [{field: item.get(field) for field in identity_fields} for item in current]


def _runtime_control_reservation_document(row) -> dict[str, object]:
    affected = json.loads(row.affected_runs_json)
    affected_questions = json.loads(row.affected_question_refs_json)
    if (
        canonical_hash(affected) != row.affected_runs_hash
        or canonical_hash(affected_questions) != row.affected_question_refs_hash
        or canonical_hash(decoded_object(row.payload_json)) != row.payload_hash
    ):
        raise OwnerConflict("runtime_control_reservation_invalid")
    return {
        "operation_ref": row.operation_ref,
        "action": row.action,
        "status": row.status,
        "expected_revision": int(row.expected_revision),
        "source_stage": row.source_stage,
        "affected_question_refs": affected_questions,
        "affected_runs": affected,
        "affected_runs_hash": row.affected_runs_hash,
    }


def _query_managed_safe_point(connection, row) -> dict[str, object] | None:
    if row is None or row.safe_point_ref is None:
        return None
    safe_point = connection.execute(
        text(
            "SELECT * FROM ar_safe_points WHERE safe_point_ref = :safe_point_ref"
        ),
        {"safe_point_ref": row.safe_point_ref},
    ).first()
    if safe_point is None:
        raise OwnerConflict("runtime_safe_point_invalid")
    checkpoint = decoded_object(safe_point.checkpoint_json)
    if (
        canonical_json(checkpoint) != safe_point.checkpoint_json
        or canonical_hash(checkpoint) != safe_point.checkpoint_hash
        or safe_point.run_ref != row.run_ref
    ):
        raise OwnerConflict("runtime_safe_point_invalid")
    return {
        "safe_point_ref": safe_point.safe_point_ref,
        "checkpoint_hash": safe_point.checkpoint_hash,
        "checkpoint": checkpoint,
        "created_at": float(safe_point.created_at),
    }


def _record_safe_point(
    connection,
    *,
    operation_ref: str,
    row,
    action: str,
    now: float,
) -> dict[str, object]:
    existing = connection.execute(
        text(
            "SELECT * FROM ar_safe_points WHERE operation_ref = :operation_ref "
            "AND run_ref = :run_ref"
        ),
        {"operation_ref": operation_ref, "run_ref": row.run_ref},
    ).first()
    if existing is not None:
        checkpoint = decoded_object(existing.checkpoint_json)
        if canonical_hash(checkpoint) != existing.checkpoint_hash:
            raise OwnerConflict("runtime_safe_point_invalid")
        return {
            "safe_point_ref": existing.safe_point_ref,
            "run_ref": existing.run_ref,
            "checkpoint_hash": existing.checkpoint_hash,
        }
    provider_operation = None
    if row.run_kind == "deepfetch":
        provider_operation = connection.execute(
            text(
                "SELECT provider_operation_ref, provider_operation_generation, "
                "provider_operation_retry_permitted FROM ar_deepfetch_runs WHERE "
                "run_ref = :run_ref"
            ),
            {"run_ref": row.run_ref},
        ).first()
    elif row.run_kind == "experiment":
        provider_operation = connection.execute(
            text(
                "SELECT provider_operation_ref, provider_operation_generation, "
                "provider_operation_retry_permitted FROM ar_experiment_runs WHERE "
                "run_ref = :run_ref"
            ),
            {"run_ref": row.run_ref},
        ).first()
    checkpoint = {
        "schema_ref": "meta-research/runtime-safe-point/v1",
        "operation_ref": operation_ref,
        "action": action,
        "run_ref": row.run_ref,
        "run_kind": row.run_kind,
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "epoch": None if row.epoch is None else int(row.epoch),
        "run_status": row.status,
        "attempt_ref": row.attempt_ref,
        "root_session_ref": row.root_session_ref,
        "fence_ref": row.fence_ref,
        "control_revision": int(row.control_revision),
        "provider_operation_ref": (
            None
            if provider_operation is None
            else provider_operation.provider_operation_ref
        ),
        "provider_operation_generation": (
            None
            if provider_operation is None
            else int(provider_operation.provider_operation_generation)
        ),
        "provider_operation_retry_permitted": (
            None
            if provider_operation is None
            else bool(provider_operation.provider_operation_retry_permitted)
        ),
    }
    safe_point_ref = new_ref("safe_point")
    checkpoint_hash = canonical_hash(checkpoint)
    connection.execute(
        text(
            "INSERT INTO ar_safe_points (safe_point_ref, operation_ref, run_ref, "
            "attempt_ref, root_session_ref, fence_ref, checkpoint_json, "
            "checkpoint_hash, created_at) VALUES (:safe_point_ref, :operation_ref, "
            ":run_ref, :attempt_ref, :root_session_ref, :fence_ref, "
            ":checkpoint_json, :checkpoint_hash, :now)"
        ),
        {
            "safe_point_ref": safe_point_ref,
            "operation_ref": operation_ref,
            "run_ref": row.run_ref,
            "attempt_ref": row.attempt_ref,
            "root_session_ref": row.root_session_ref,
            "fence_ref": row.fence_ref,
            "checkpoint_json": canonical_json(checkpoint),
            "checkpoint_hash": checkpoint_hash,
            "now": now,
        },
    )
    return {
        "safe_point_ref": safe_point_ref,
        "run_ref": row.run_ref,
        "checkpoint_hash": checkpoint_hash,
    }


def _terminate_canonical_managed_run(
    connection, *, row, reason_code: str, now: float
) -> None:
    """Advance the typed Run ledger before publishing the terminal overlay.

    Existing Run kinds have different historical vocabularies.  This adapter
    maps the generic terminal command onto each canonical ledger in the same AR
    transaction, so queue/admission queries cannot observe a zombie Run.
    """

    if row.run_kind == "deepfetch":
        connection.execute(
            text(
                "UPDATE ar_deepfetch_attempts SET status = 'cancelled', "
                "failure_code = :reason_code, completed_at = :now WHERE "
                "attempt_ref = :attempt_ref AND status = 'running'"
            ),
            {
                "reason_code": reason_code,
                "now": now,
                "attempt_ref": row.attempt_ref,
            },
        )
        connection.execute(
            text(
                "UPDATE ar_deepfetch_sessions SET status = 'cancelled', updated_at = "
                ":now WHERE run_ref = :run_ref AND status = 'open'"
            ),
            {"now": now, "run_ref": row.run_ref},
        )
        connection.execute(
            text(
                "UPDATE ar_deepfetch_runs SET status = 'cancelled', failure_code = "
                ":reason_code, provider_operation_retry_permitted = 0, "
                "next_reconcile_at = NULL, completed_at = :now, updated_at = :now "
                "WHERE run_ref = :run_ref AND status IN ('admitted', 'running')"
            ),
            {"reason_code": reason_code, "now": now, "run_ref": row.run_ref},
        )
        return
    if row.run_kind == "experiment":
        changed = connection.execute(
            text(
                "UPDATE ar_experiment_runs SET status = 'failed', failure_code = "
                ":reason_code, provider_operation_retry_permitted = 0, "
                "completed_at = :now, updated_at = :now WHERE run_ref = :run_ref "
                "AND status IN ('admitted', 'running')"
            ),
            {"reason_code": reason_code, "now": now, "run_ref": row.run_ref},
        )
        connection.execute(
            text(
                "UPDATE ar_experiment_attempts SET status = 'failed', "
                "retired_reason = :reason_code, completed_at = :now WHERE "
                "attempt_ref = :attempt_ref AND status IN ('admitted', 'running')"
            ),
            {
                "reason_code": reason_code,
                "now": now,
                "attempt_ref": row.attempt_ref,
            },
        )
        connection.execute(
            text(
                "UPDATE ar_experiment_sessions SET status = 'closed', updated_at = "
                ":now WHERE run_ref = :run_ref AND status = 'open'"
            ),
            {"now": now, "run_ref": row.run_ref},
        )
        if changed.rowcount:
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET active_experiment_run_count = "
                    "CASE WHEN active_experiment_run_count > 0 THEN "
                    "active_experiment_run_count - 1 ELSE 0 END WHERE singleton = "
                    "'owner'"
                )
            )
        return
    if row.run_kind.endswith("_stage"):
        # Historical Stage tables do not have a cancelled state.  Fence
        # revocation plus ar_run_controls is the authoritative terminal fact;
        # every worker/provider boundary consults that fact before execution.
        return
    raise OwnerConflict("runtime_run_kind_unregistered")


def _is_formal_stage_run_kind(run_kind: str) -> bool:
    stage, separator, suffix = run_kind.rpartition("_")
    return separator == "_" and suffix == "stage" and stage in FORMAL_STAGES


def _provider_operation_for_unit(connection, *, unit_ref: str, run_ref: str) -> str:
    operation_ref = connection.execute(
        text(
            "SELECT operation_ref FROM ar_stage_provider_invocations WHERE "
            "invocation_ref = :unit_ref AND run_ref = :run_ref"
        ),
        {"unit_ref": unit_ref, "run_ref": run_ref},
    ).scalar_one_or_none()
    if operation_ref is not None:
        return str(operation_ref)
    control = connection.execute(
        text("SELECT run_kind FROM ar_run_controls WHERE run_ref = :run_ref"),
        {"run_ref": run_ref},
    ).first()
    if control is not None and control.run_kind == "deepfetch":
        operation_ref = connection.execute(
            text(
                "SELECT provider_operation_ref FROM ar_deepfetch_runs WHERE run_ref = "
                ":run_ref"
            ),
            {"run_ref": run_ref},
        ).scalar_one_or_none()
    elif control is not None and control.run_kind == "experiment":
        operation_ref = connection.execute(
            text(
                "SELECT provider_operation_ref FROM ar_experiment_runs WHERE run_ref = "
                ":run_ref"
            ),
            {"run_ref": run_ref},
        ).scalar_one_or_none()
    if operation_ref is None:
        raise OwnerConflict("runtime_provider_operation_invalid")
    return str(operation_ref)


def _close_provider_unit(
    connection,
    *,
    run_ref: str,
    attempt_ref: str | None,
    fence_ref: str | None,
) -> None:
    now = time.time()
    operation_ref = connection.execute(
        text(
            "SELECT operation_ref FROM ar_provider_units WHERE run_ref = :run_ref AND "
            "attempt_ref IS :attempt_ref AND fence_ref IS :fence_ref AND status IN "
            "('active', 'revocation_pending') ORDER BY started_at DESC LIMIT 1"
        ),
        {
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
        },
    ).scalar_one_or_none()
    connection.execute(
        text(
            "UPDATE ar_provider_units SET status = CASE WHEN EXISTS (SELECT 1 FROM "
            "ar_fence_revocations revocations WHERE revocations.fence_ref IS "
            ":fence_ref AND revocations.run_ref = :run_ref) THEN 'revoked' ELSE "
            "'completed' END, completed_at = :now "
            "WHERE run_ref = :run_ref AND attempt_ref IS :attempt_ref AND fence_ref "
            "IS :fence_ref AND status IN ('active', 'revocation_pending')"
        ),
        {
            "now": now,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "fence_ref": fence_ref,
        },
    )
    if operation_ref is not None:
        connection.execute(
            text(
                "UPDATE ar_provider_units SET status = 'revoked', completed_at = :now "
                "WHERE run_ref = :run_ref AND operation_ref = :operation_ref AND "
                "status = 'revocation_pending'"
            ),
            {
                "now": now,
                "run_ref": run_ref,
                "operation_ref": operation_ref,
            },
        )
    connection.execute(
        text(
            "UPDATE ar_run_controls SET cleanup_status = 'completed', updated_at = "
            ":now WHERE run_ref = :run_ref AND cleanup_status = 'pending' AND NOT "
            "EXISTS (SELECT 1 FROM "
            "ar_provider_units pending WHERE pending.run_ref = :run_ref AND "
            "pending.status IN ('active', 'revocation_pending'))"
        ),
        {"now": now, "run_ref": run_ref},
    )


def _runtime_control_receipt(row) -> dict[str, object]:
    affected_runs = json.loads(row.affected_runs_json)
    safe_points = json.loads(row.safe_points_json)
    affected_questions = json.loads(row.affected_question_refs_json)
    if (
        canonical_hash(affected_runs) != row.affected_runs_hash
        or canonical_hash(safe_points) != row.safe_points_hash
        or canonical_hash(affected_questions) != row.affected_question_refs_hash
    ):
        raise OwnerConflict("runtime_control_receipt_invalid")
    receipt = {
        "status": "completed",
        "issuer": AR_OWNER,
        "kind": "runtime_control",
        "operation_ref": row.operation_ref,
        "action": row.action,
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "epoch": int(row.epoch),
        "target_scope": row.target_scope,
        "run_ref": row.run_ref,
        "source_stage": row.source_stage,
        "affected_question_refs": affected_questions,
        "affected_runs": affected_runs,
        "safe_points": safe_points,
        "receipt_ref": row.receipt_ref,
        "receipt_hash": row.receipt_hash,
    }
    if row.action == "prune":
        receipt["quiescence_receipt"] = _runtime_quiescence_receipt(row)
    return receipt


def _runtime_quiescence_receipt(row) -> dict[str, object]:
    affected_runs = json.loads(row.affected_runs_json)
    affected_questions = json.loads(row.affected_question_refs_json)
    bindings = {
        "operation_ref": row.operation_ref,
        "quest_ref": row.quest_ref,
        "cycle_ref": row.cycle_ref,
        "epoch": int(row.epoch),
        "affected_question_refs_hash": canonical_hash(affected_questions),
        "affected_run_refs_hash": canonical_hash(
            [item["run_ref"] for item in affected_runs]
        ),
        "safe_point_refs_hash": canonical_hash(
            [item["safe_point_ref"] for item in affected_runs]
        ),
    }
    receipt_ref = (
        "ar_quiescence_"
        + canonical_hash({"operation_ref": row.operation_ref})[:48]
    )
    return {
        "issuer": AR_OWNER,
        "kind": "runtime_quiescence",
        "operation_ref": row.operation_ref,
        "affected_question_refs": affected_questions,
        "affected_run_refs": [item["run_ref"] for item in affected_runs],
        "safe_point_refs": [item["safe_point_ref"] for item in affected_runs],
        "receipt_ref": receipt_ref,
        "receipt_hash": canonical_hash(
            {
                "issuer": AR_OWNER,
                "kind": "runtime_quiescence",
                "subject_ref": row.operation_ref,
                "bindings": bindings,
            }
        ),
    }


def _assert_runtime_control_allows(connection, run_ref: str, fence_ref: str) -> None:
    control = connection.execute(
        text(
            "SELECT status, run_kind FROM ar_run_controls WHERE run_ref = :run_ref"
        ),
        {"run_ref": run_ref},
    ).first()
    revoked = connection.execute(
        text(
            "SELECT fence_ref FROM ar_fence_revocations WHERE fence_ref = "
            ":fence_ref AND run_ref = :run_ref"
        ),
        {"fence_ref": fence_ref, "run_ref": run_ref},
    ).first()
    if revoked is not None:
        error = (
            "experiment_fence_stale"
            if control is not None and control.run_kind == "experiment"
            else "runtime_fence_revoked"
        )
        raise OwnerConflict(error)
    if control is None:
        return
    if control.status == "suspended":
        raise OwnerConflict("runtime_run_suspended")
    if control.status == "suspended_fenced":
        raise OwnerConflict("runtime_fence_revoked")
    if control.status == "reconciliation_required":
        raise OwnerConflict("runtime_reconciliation_required")
    if control.status in {"terminated", "completed"}:
        raise OwnerConflict("terminal_run_cannot_reopen")


def _validate_probe_snapshot(snapshot: HostComputeSnapshot) -> None:
    if snapshot.status not in {"ready", "unavailable"}:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "ready" and snapshot.reason_code is not None:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if snapshot.status == "unavailable" and not snapshot.reason_code:
        raise OwnerConflict("host_compute_snapshot_invalid")
    if not math.isfinite(snapshot.observed_at) or not snapshot.adapter_kind:
        raise OwnerConflict("host_compute_snapshot_invalid")
    uuids: set[str] = set()
    for device in snapshot.devices:
        if (
            not device.uuid
            or device.uuid in uuids
            or not device.name
            or device.memory_total_mib <= 0
        ):
            raise OwnerConflict("host_compute_snapshot_invalid")
        uuids.add(device.uuid)


def create_agent_runtime_interface(
    database: Database,
    feed: DurableFeed,
    host_compute_probe: HostComputeProbe,
    stage_request_verifier: StageRunRequestVerifier | None = None,
    outcome_verifier: IdeaOutcomeDecisionVerifier | None = None,
    formal_plan_verifier: FormalPlanDecisionVerifier | None = None,
    deepfetch_request_verifier: DeepFetchRunRequestVerifier | None = None,
    acquisition_private_root: Path | None = None,
    human_response_verifier: HumanResponseVerifier | None = None,
    experiment_binding_verifier: ExperimentInputBindingVerifier | None = None,
    target_graph_verifier: TargetGraphReceiptVerifier | None = None,
) -> AgentRuntimeInterface:
    return SQLiteAgentRuntime(
        database,
        feed,
        host_compute_probe,
        stage_request_verifier,
        outcome_verifier,
        formal_plan_verifier,
        deepfetch_request_verifier,
        acquisition_private_root,
        human_response_verifier,
        experiment_binding_verifier,
        target_graph_verifier,
    )


def create_agent_runtime_receipt_verifier(
    database: Database,
    stage_request_verifier: StageRunRequestVerifier | None = None,
    writing_authorization_verifier: HumanResponseVerifier | None = None,
) -> SQLiteAgentRuntimeReceiptVerifier:
    return SQLiteAgentRuntimeReceiptVerifier(
        database,
        stage_request_verifier,
        writing_authorization_verifier,
    )


def create_host_compute_observation_reader(
    database: Database,
) -> HostComputeObservationReader:
    return SQLiteHostComputeObservationReader(database)
