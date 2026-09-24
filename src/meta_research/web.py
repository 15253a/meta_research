from __future__ import annotations

from meta_research.snapshot_queries import SnapshotQueryCoordinator

import asyncio
import base64
import binascii
import hmac
import ipaddress
import json
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Literal, TypeVar
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError

from meta_research.auth import AuthSession
from meta_research.asset_download import AssetDownloadResponse
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.composition import ProductionRuntime
from meta_research.harness import (
    FullConformanceRequest,
    HarnessAdmissionError,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.secret_detection import contains_secret
from meta_research.owners.research_graph import ASSET_ROLE_QUERY_MAX_PAGE_SIZE
from meta_research.owners.research_memory import (
    ASSET_HISTORY_QUERY_MAX_PAGE_SIZE,
    ASSET_PROJECTION_HISTORY_PER_VERSION,
    AssetIntakeRequest,
)
from meta_research.projection import SnapshotConsistencyUnavailable
from meta_research.runtime_status import RuntimeStatusReader
from meta_research.query_timing import measure_query
from meta_research.research_overview import ResearchOverviewReader
from meta_research.experiment_logs import ExperimentLogError, TargetExperimentLogs
from meta_research.quest_drafting import (
    INTENT_MESSAGE_MAX_LENGTH,
    QUESTION_FIELD_MAX_LENGTHS,
)
from meta_research.runtime_protection import RuntimeProtectionUnavailable
from meta_research.root_capabilities import RootAgentKind
from meta_research.root_operation_diagnostics import (
    RootOperationDiagnosticError,
)
from meta_research.stage_root_observations import StageRootObservationError
from meta_research.root_session_observations import RootSessionObservations
from meta_research.semantic_mcp import MCP_PROTOCOL_VERSION


SESSION_COOKIE = "meta_research_session"
CSRF_COOKIE = "meta_research_csrf"
LOGGER = logging.getLogger(__name__)
MAX_ASSET_INTAKE_REQUEST_BODY_BYTES = 96 * 1024 * 1024
MAX_COMMAND_REQUEST_BODY_BYTES = 1 * 1024 * 1024
MAX_MCP_REQUEST_BODY_BYTES = 1 * 1024 * 1024
# Kept as the public intake-envelope constant used by compatibility tests and
# callers that size a Research Asset request before sending it.
MAX_JSON_REQUEST_BODY_BYTES = MAX_ASSET_INTAKE_REQUEST_BODY_BYTES
MAX_CONCURRENT_ASSET_INTAKE_REQUESTS = 2
MAX_CONCURRENT_ASSET_IO_OPERATIONS = 2
ASSET_WORKER_WATCHDOG_SECONDS = 5.0
ASSET_ROUTE_WATCHDOG_SECONDS = 5.0
PROVIDER_WORKER_WATCHDOG_SECONDS: None = None
WRITING_DELIVERY_STALL_SECONDS = 910.0
REASONING_FOLLOWUP_WORKER_WATCHDOG_SECONDS = 30.0
BACKGROUND_WORKER_STARTUP_GRACE_SECONDS = 0.1
# ``BundleStage.transient_error`` predates the durable pause/wait contract and
# carries both actual failures and normal no-progress states.  Keep this list
# closed: an unfamiliar code must remain fail-closed as worker-unavailable.
_BUNDLE_STAGE_HEALTHY_WAIT_CODES = frozenset(
    {
        # Root policy and rolling-plan control flow.
        "bundle_strategy_incomplete",
        "bundle_replan_required",
        "bundle_root_waiting",
        # Target launch and the independently owned root lifecycle.
        "target_launch_admitted",
        "target_launch_pending",
        "target_root_running",
        # Human/domain waits and mechanical terminal transitions.
        "target_high_risk_authorization_required",
        "target_high_risk_authorization_declined",
        "bundle_report_blocked",
        "bundle_replan_activated",
        "bundle_exhaustion_rejected",
        "bundle_exhaustion_stale",
        "bundle_exhaustion_needs_input",
        "bundle_exhaustion_outcome_unknown",
        "bundle_exhaustion_technical_blocker",
    }
)
_T = TypeVar("_T")


@dataclass
class ReconciliationHealth:
    status: Literal["ready", "unavailable"] = "ready"
    last_error: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class _PendingWorkerOperation:
    operation: asyncio.Future[bool]


@dataclass(frozen=True)
class _TargetRunFlight:
    target_ref: str
    operation: asyncio.Future[bool]


@dataclass
class WorkerHealthUpdates:
    _revision: int = 0
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def revision(self) -> int:
        return self._revision

    def publish(self) -> None:
        self._revision += 1
        self._changed.set()

    async def wait_after(self, revision: int, timeout: float) -> int | None:
        while self._revision <= revision:
            self._changed.clear()
            if self._revision > revision:
                break
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                return None
        return self._revision


class BootstrapExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=256)


class StartHarnessConformanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codex_model_ref: Literal["gpt-6-sol"] = CODEX_MODEL_REF
    codex_auth_profile_ref: str = Field(min_length=1, max_length=160)
    # Accepted only so an older diagnostic client does not fail at the HTTP
    # decoder boundary.  Production selection is Codex-only and ignores them.
    claude_model_ref: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    claude_auth_profile_ref: str | None = Field(
        default=None, min_length=1, max_length=160
    )


class OpenQuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiteratureConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["oa_then_institution", "oa_only", "provided_only"] = (
        "oa_then_institution"
    )
    library_entry_url: str = Field(default="", max_length=4000)
    scope_exclusions: str = Field(default="", max_length=8000)
    accepted_material_bindings: list[dict[str, object]] = Field(
        default_factory=list, max_length=100
    )


class QuestDraftV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(max_length=4000)
    completion_criteria: str = Field(max_length=4000)
    time_budget: Literal["7d", "30d", "90d", "open"] = "open"
    route: Literal["direct", "deepfetch"] = "direct"
    resource_envelope_ref: str | None = Field(default=None, max_length=64)
    resource_envelope_hash: str | None = Field(default=None, max_length=64)
    literature: LiteratureConfigurationRequest = Field(
        default_factory=LiteratureConfigurationRequest
    )
    background_and_initial_direction: str = Field(default="", max_length=12000)


class ReviseQuestDraftV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=1)
    expected_draft_hash: str = Field(min_length=64, max_length=64)
    draft: QuestDraftV2Request


class GenerateProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=1)
    expected_draft_hash: str = Field(min_length=64, max_length=64)


class PrepareAcquisitionSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=1)
    expected_draft_hash: str = Field(min_length=64, max_length=64)


class QuestionContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=QUESTION_FIELD_MAX_LENGTHS["title"])
    unknown_statement: str = Field(
        max_length=QUESTION_FIELD_MAX_LENGTHS["unknown_statement"]
    )
    answer_shape: str = Field(max_length=QUESTION_FIELD_MAX_LENGTHS["answer_shape"])
    applicability_scope: str = Field(
        max_length=QUESTION_FIELD_MAX_LENGTHS["applicability_scope"]
    )
    background_context: str = Field(
        max_length=QUESTION_FIELD_MAX_LENGTHS["background_context"]
    )
    requirements_constraints: str = Field(
        max_length=QUESTION_FIELD_MAX_LENGTHS["requirements_constraints"]
    )


class SaveProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=1)
    expected_draft_hash: str = Field(min_length=64, max_length=64)
    expected_proposal_ref: str = Field(min_length=1, max_length=64)
    expected_proposal_hash: str = Field(min_length=64, max_length=64)
    explicit_review: bool = False
    content: QuestionContentRequest


class ComputeProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_device_uuids: list[str] = Field(default_factory=list, max_length=32)


class IntentMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=1)
    expected_draft_hash: str = Field(min_length=64, max_length=64)
    message: str = Field(min_length=1, max_length=INTENT_MESSAGE_MAX_LENGTH)


class ConfirmationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quest_draft_revision: int = Field(ge=1)
    quest_draft_hash: str = Field(min_length=64, max_length=64)
    proposal_ref: str = Field(min_length=1, max_length=64)
    proposal_hash: str = Field(min_length=64, max_length=64)


class ConfirmQuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quest_draft_revision: int = Field(ge=1)
    quest_draft_hash: str = Field(min_length=64, max_length=64)
    proposal_ref: str = Field(min_length=1, max_length=64)
    proposal_hash: str = Field(min_length=64, max_length=64)
    preview_ref: str = Field(min_length=1, max_length=64)
    preview_hash: str = Field(min_length=64, max_length=64)


class OpenManualQuestionCreationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quest_ref: str = Field(min_length=1, max_length=64)
    parent_question_ref: str = Field(min_length=1, max_length=64)


class ManualCreationSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=12000)
    fields: QuestionContentRequest
    accepted_material_bindings: list[dict[str, object]] = Field(
        default_factory=list, max_length=100
    )
    deepfetch_preference: Literal["use", "skip", "later"] = "later"


class ConfirmManualSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: ManualCreationSeedRequest


class ManualResearchPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_seed_ref: str = Field(min_length=1, max_length=64)
    expected_seed_hash: str = Field(min_length=64, max_length=64)


class ManualDraftingMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_basis_hash: str = Field(min_length=64, max_length=64)
    message: str = Field(min_length=1, max_length=INTENT_MESSAGE_MAX_LENGTH)


class SaveManualQuestionProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_basis_hash: str = Field(min_length=64, max_length=64)
    expected_proposal_ref: str | None = Field(default=None, max_length=64)
    expected_proposal_hash: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    content: QuestionContentRequest


class ConfirmManualQuestionProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_ref: str = Field(min_length=1, max_length=64)
    proposal_hash: str = Field(min_length=64, max_length=64)


class AssetIntakeWebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal[
        "text",
        "file",
        "directory",
        "local_path",
        "repository",
        "link",
        "system_artifact",
    ]
    custody_mode: Literal["managed", "linked_local"]
    display_name: str = Field(min_length=1, max_length=512)
    media_type: str = Field(default="application/octet-stream", max_length=255)
    text: str | None = Field(default=None, max_length=16_000_000)
    content_base64: str | None = Field(default=None, max_length=140_000_000)
    source_locator: str | None = Field(default=None, max_length=16_000)
    provenance: dict[str, object] | None = None
    asset_ref: str | None = Field(default=None, max_length=128)
    asynchronous: bool = False

    def as_owner_request(self) -> AssetIntakeRequest:
        if self.text is not None and self.content_base64 is not None:
            raise HTTPException(
                status_code=422,
                detail={"code": "asset_content_encoding_ambiguous"},
            )
        content: bytes | None
        if self.text is not None:
            content = self.text.encode("utf-8")
        elif self.content_base64 is not None:
            try:
                content = base64.b64decode(self.content_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "asset_content_base64_invalid"},
                ) from error
        else:
            content = None
        request = AssetIntakeRequest(
            source_kind=self.source_kind,
            custody_mode=self.custody_mode,
            display_name=self.display_name,
            media_type=self.media_type,
            content=content,
            source_locator=self.source_locator,
            provenance=self.provenance,
            asset_ref=self.asset_ref,
            asynchronous=self.asynchronous,
        )
        try:
            request.validate()
        except OwnerConflict as error:
            raise HTTPException(
                status_code=422,
                detail={"code": error.code},
            ) from error
        return request


class AssetRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["evidence", "quest_source_material"]
    quest_ref: str = Field(min_length=1, max_length=128)


class AssetHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1024)


class ReleaseEligibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_reference_revision: int | None = Field(default=None, ge=0)


class EmptyCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompanionQuestionViewContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["question"]
    quest_ref: str = Field(min_length=1, max_length=128)
    question_ref: str = Field(min_length=1, max_length=128)
    content_ref: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=64, max_length=64)
    lifecycle_revision: int = Field(ge=1)


class CompanionHumanRequestViewContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human_request"]
    quest_ref: str = Field(min_length=1, max_length=128)
    request_ref: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)


class CompanionMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=INTENT_MESSAGE_MAX_LENGTH)
    view_context: (
        CompanionQuestionViewContext | CompanionHumanRequestViewContext | None
    ) = None


class HumanRequestLinkedLocalMaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_locator: str = Field(min_length=1, max_length=16_000)

    def as_owner_request(
        self,
        *,
        request_ref: str,
        evidence_kind: Literal["external_approval", "offline_result"],
        asynchronous: bool,
    ) -> AssetIntakeRequest:
        request = AssetIntakeRequest(
            source_kind="local_path",
            custody_mode="linked_local",
            display_name=Path(self.source_locator).name or self.source_locator,
            media_type="application/octet-stream",
            source_locator=self.source_locator,
            provenance={
                "submitted_via": "human_request_response",
                "human_request_ref": request_ref,
                "evidence_kind": evidence_kind,
            },
            asynchronous=asynchronous,
        )
        try:
            request.validate()
        except OwnerConflict as error:
            raise HTTPException(
                status_code=422,
                detail={"code": error.code},
            ) from error
        return request


class HumanRequestResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["provided", "declined", "deferred"]
    facts: dict[str, object] = Field(default_factory=dict)
    note: str = Field(default="", max_length=4000)
    linked_local_material: HumanRequestLinkedLocalMaterialRequest | None = None


class AgentProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str = Field(min_length=1, max_length=128)
    proposal: dict[str, object]


class AgentProposalConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_scope_ref: str = Field(min_length=1, max_length=128)
    expected_proposal_hash: str = Field(min_length=64, max_length=64)


class WithdrawSoftConstraintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class CommandDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str = Field(min_length=1, max_length=128)
    command: dict[str, object]


class CommandRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    command: dict[str, object]


class CommandPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_revision: int = Field(ge=1)
    draft_hash: str = Field(min_length=64, max_length=64)


class CommandConfirmationRequest(CommandPreviewRequest):
    preview_ref: str = Field(min_length=1, max_length=64)
    preview_hash: str = Field(min_length=64, max_length=64)


class CommandExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_receipt_ref: str = Field(min_length=1, max_length=96)


class CapabilityAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=64)
    decision: Literal["granted", "denied", "revoked"]
    scope: dict[str, object]
    confirmation_receipt_ref: str = Field(min_length=1, max_length=64)


class WritingIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Literal["report", "paper", "presentation"] = "report"
    quest_ref: str = Field(min_length=1, max_length=96)
    title: str = Field(min_length=1, max_length=512)
    audience: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=4000)
    instructions: str = Field(default="", max_length=12000)


class WritingConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_revision: int = Field(ge=1)
    draft_hash: str = Field(min_length=64, max_length=64)
    preview_ref: str = Field(min_length=1, max_length=128)
    preview_hash: str = Field(min_length=64, max_length=64)


class WritingControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume"]
    expected_attempt_ref: str = Field(min_length=1, max_length=128)
    expected_fence_ref: str = Field(min_length=1, max_length=128)


class WritingRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: list[str] = Field(min_length=1, max_length=64)


class StartQuestCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_outcome_ref: str = Field(min_length=1, max_length=128)
    candidate_completion_ref: str = Field(min_length=1, max_length=128)


class QuestCompletionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_ref: str = Field(min_length=1, max_length=128)
    preview_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["confirmed", "rejected"]


class WritingLocalDeliveryTargetRequest(BaseModel):
    """Exact local target asserted before HC previews an external effect."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    permissions: Literal[384]
    expected_existing_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("path")
    @classmethod
    def validate_absolute_canonical_path(cls, value: str) -> str:
        path = Path(value)
        try:
            resolved = str(path.resolve(strict=False))
        except (OSError, RuntimeError) as error:
            raise ValueError("writing_delivery_target_path_invalid") from error
        if (
            not path.is_absolute()
            or value != resolved
            or value == "/"
            or "\x00" in value
        ):
            raise ValueError("writing_delivery_target_path_invalid")
        return value


class WritingRemoteDeliveryTargetRequest(BaseModel):
    """Extensible target envelope for installed send/submit providers."""

    model_config = ConfigDict(extra="forbid")

    target_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    permissions: list[str] = Field(min_length=1, max_length=32)
    expected_existing_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item
            or len(item) > 128
            or not item[0].isalnum()
            or any(
                character
                not in (
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "abcdefghijklmnopqrstuvwxyz"
                    "0123456789._:-"
                )
                for character in item
            )
            for item in value
        ):
            raise ValueError("writing_delivery_permissions_invalid")
        return value


class WritingDeliveryIntentRequest(BaseModel):
    """One exact provider/action/target tuple; it authorizes no future use."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["publish", "overwrite", "delete", "send", "submit"]
    provider_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    target: WritingLocalDeliveryTargetRequest | WritingRemoteDeliveryTargetRequest
    output_format: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$",
    )

    @model_validator(mode="after")
    def validate_provider_target_pair(self) -> "WritingDeliveryIntentRequest":
        if self.provider_ref == "local-filesystem":
            if not isinstance(self.target, WritingLocalDeliveryTargetRequest):
                raise ValueError("writing_delivery_target_invalid")
            expected = self.target.expected_existing_hash
            if (
                self.action not in {"publish", "overwrite", "delete"}
                or (self.action == "publish" and expected is not None)
                or (self.action != "publish" and expected is None)
            ):
                raise ValueError("writing_delivery_target_invalid")
        elif not isinstance(self.target, WritingRemoteDeliveryTargetRequest):
            raise ValueError("writing_delivery_target_invalid")
        return self


class ResearchInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quest_ref: str = Field(min_length=1,max_length=128)
    question_ref: str | None = Field(default=None,min_length=1,max_length=128)
    text: str = Field(min_length=1,max_length=65536)
    asset_bindings: list[dict[str,object]] = Field(default_factory=list,max_length=256)


class OutputLanguagePreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_language: Literal["zh","en"]


class RuntimeConditionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=24000)
    expected_revision: str = Field(min_length=64, max_length=64)


def create_app(
    runtime: ProductionRuntime, *, base_url: str, control_key: str
) -> FastAPI:
    runtime.configure_resident_mcp_endpoint(base_url)
    runtime.bundle_stage.configure_resident_mcp_endpoint(base_url)
    runtime.reasoning_stage.configure_resident_mcp_endpoint(base_url)
    runtime.target_run_runtime.configure_resident_mcp_endpoint(base_url)
    worker_operations = {
        "quest_reconciliation_worker": _reconcile_quest_initializations,
        "quest_drafting_worker": _process_quest_drafting,
        "first_question_deepfetch_worker": _process_first_question_deepfetch,
        "idea_stage_worker": _process_idea_stage,
        "plan_stage_worker": _process_plan_stage,
        "bundle_stage_worker": _process_bundle_stage,
        "reasoning_stage_worker": _process_reasoning_stage,
        "autonomous_creation_worker": _process_autonomous_creation,
        "quest_completion_worker": _process_quest_completion,
        "target_run_worker": _process_target_runs,
        "writing_worker": _process_writing,
        "research_asset_intake_worker": _process_research_assets,
        "research_asset_verification_worker": _verify_research_assets,
    }
    worker_health = {name: ReconciliationHealth() for name in worker_operations}
    worker_tasks: dict[str, asyncio.Task[None]] = {}
    recorder = getattr(runtime, 'timeline_summaries', None)
    worker_health_updates = WorkerHealthUpdates()
    asset_intake_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_INTAKE_REQUESTS)
    asset_io_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_IO_OPERATIONS)
    asset_intake_recovery_slots = asyncio.Semaphore(1)
    asset_handoff_singleflight = _AssetIOSingleFlight()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        worker_tasks["harness_conformance_worker"] = _start_background_worker(
            lambda: _process_harness_conformance(runtime, base_url)
        )
        for name, operation in worker_operations.items():
            worker_tasks[name] = _start_background_worker(
                lambda operation=operation, name=name: operation(
                    runtime, worker_health[name], worker_health_updates.publish
                )
            )
        # Display notes are independently retried and never gate research health.
        if recorder is not None:
            worker_tasks['research_recorder'] = _start_background_worker(
                lambda: _process_background_operation(
                    recorder.process_once,
                    health=ReconciliationHealth(), worker_label='research recorder',
                    timeout_code='timeline_summary_timeout', timeout_seconds=None,
                    on_health_change=None, idle_delay=5.0, active_delay=1.0,
                )
            )
        try:
            yield
        finally:
            try:
                await asyncio.to_thread(runtime.request_stop)
            except Exception:
                LOGGER.exception("provider shutdown request failed")
            tasks = tuple(worker_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=2.0
                    )
                except TimeoutError:
                    LOGGER.error("quest workers did not stop within 2 seconds")

    app = FastAPI(
        title="Meta-research vNext",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    web_root = Path(str(files("meta_research") / "web_dist")).resolve()
    expected_host = urlsplit(base_url).netloc
    base_url_host = urlsplit(base_url).hostname
    try:
        base_url_is_loopback = bool(
            base_url_host and ipaddress.ip_address(base_url_host).is_loopback
        )
    except ValueError:
        base_url_is_loopback = False
    trust_ssh_loopback = (
        os.environ.get("META_RESEARCH_TRUST_SSH_LOOPBACK") == "1"
        and base_url_is_loopback
    )

    def worker_check(name: str) -> dict[str, object]:
        task = worker_tasks.get(name)
        health = worker_health[name]
        running = task is not None and not task.done()
        if running and health.status == "ready":
            return {"name": name, "status": "ready"}
        reason_code = health.last_error or (
            "worker_not_started" if task is None else "worker_exited"
        )
        return {
            "name": name,
            "status": "unavailable",
            "reason": {"code": reason_code},
        }

    status_reader = RuntimeStatusReader(runtime._database)
    latest_status_revision: int | None = None
    snapshot_queries = SnapshotQueryCoordinator(
        lambda **options: public_snapshot(**options),
        # Worker failures/recovery do not necessarily record an Owner event.
        # Their public readiness must invalidate a retained snapshot as well.
        feed_revision=lambda: (
            runtime.feed.query_readiness().current_revision,
            worker_health_updates.revision,
        ),
    )

    def worker_health_status() -> dict[str, object]:
        checks = [runtime.query_target_root_readiness(),
                  *(worker_check(name) for name in worker_operations)]
        return {
            "status": "ready" if all(check["status"] == "ready" for check in checks) else "unavailable",
            "revision": latest_status_revision,
            "checks": checks,
        }

    def public_snapshot(*, include_assets: bool = True, include_history: bool = True) -> dict[str, object]:
        snapshot = runtime.projection.query_snapshot(
            include_assets=include_assets, include_history=include_history,
        )
        readiness = snapshot["readiness"]
        checks = [
            *readiness["checks"],
            runtime.query_target_root_readiness(),
            *(worker_check(name) for name in worker_operations),
        ]
        core_checks = [
            check
            for check in checks
            if check["name"]
            not in {
                "idea_stage_worker",
                "plan_stage_worker",
                "bundle_stage_worker",
                "reasoning_stage_worker",
                "autonomous_creation_worker",
                "quest_completion_worker",
                "target_run_worker",
                "writing_worker",
                "research_asset_intake_worker",
                "research_asset_verification_worker",
            }
        ]
        snapshot["readiness"] = {
            "status": (
                "ready"
                if readiness["status"] == "ready"
                and all(check["status"] == "ready" for check in core_checks)
                else "unavailable"
            ),
            "checks": checks,
        }
        return snapshot

    def require_current_collaboration_scope(
        requested_scope_ref: str | None, *, conflict_code: str
    ) -> str:
        current_scope_ref = (
            runtime.owners.human_collaboration.query_collaboration_scope()
        )
        if (
            requested_scope_ref is not None
            and requested_scope_ref != current_scope_ref
        ):
            raise OwnerConflict(conflict_code)
        return current_scope_ref

    @app.middleware("http")
    async def protect_every_request(request: Request, call_next):
        path = request.url.path
        if request.headers.get("host") != expected_host:
            return _error(400, "host_invalid")
        public_auth_route = request.method == "POST" and path in {
            "/auth/bootstrap",
            "/auth/launch",
        }
        internal_route = path.startswith("/internal/")
        mcp_route = path == "/mcp"
        issued_session: AuthSession | None = None
        session_was_valid = False

        if mcp_route:
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
            )
            if request.method == "POST" and content_type != "application/json":
                return _error(415, "json_required")
            origin = request.headers.get("origin")
            if origin is not None and origin != base_url:
                return _error(403, "origin_invalid")
            accepted = {
                item.split(";", 1)[0].strip()
                for item in request.headers.get("accept", "").split(",")
            }
            if request.method == "POST" and not {
                "application/json",
                "text/event-stream",
            }.issubset(accepted):
                return _error(406, "mcp_accept_required")

        if internal_route:
            supplied = request.headers.get("x-meta-research-control")
            if not runtime.authentication.control_key_matches(supplied, control_key):
                return _error(401, "control_authentication_required")
        elif not public_auth_route and not mcp_route:
            session_token = request.cookies.get(SESSION_COOKIE)
            session_was_valid = await asyncio.to_thread(
                runtime.authentication.session_is_valid, session_token
            )
            if not session_was_valid:
                if not trust_ssh_loopback:
                    return _error(401, "authentication_required")
                if path != "/auth/logout":
                    issued_session = await asyncio.to_thread(
                        runtime.authentication.issue_session
                    )
                    session_token = issued_session.token
            request.state.session_token = session_token

        json_auth_route = request.method == "POST" and path in {
            "/auth/bootstrap",
            "/auth/logout",
        }
        unsafe_api_route = path.startswith("/api/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        is_asset_intake = (
            request.method == "POST" and path == "/api/v1/research-assets/intakes"
        )
        if json_auth_route or unsafe_api_route:
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
            )
            if content_type != "application/json":
                return _error(415, "json_required")
            if request.headers.get("origin") != base_url:
                return _error(403, "origin_invalid")
        csrf_protected_route = unsafe_api_route or path == "/auth/logout"
        trusted_session_was_issued = trust_ssh_loopback and issued_session is not None
        trusted_logout_without_session = (
            trust_ssh_loopback
            and path == "/auth/logout"
            and not session_was_valid
        )
        if csrf_protected_route and not (
            trusted_session_was_issued or trusted_logout_without_session
        ):
            csrf_header = request.headers.get("x-csrf-token")
            csrf_cookie = request.cookies.get(CSRF_COOKIE)
            if (
                csrf_header is None
                or csrf_cookie is None
                or not hmac.compare_digest(csrf_header, csrf_cookie)
                or not await asyncio.to_thread(
                    runtime.authentication.csrf_matches,
                    request.state.session_token,
                    csrf_header,
                )
            ):
                return _error(403, "csrf_invalid")

        async def dispatch() -> Response:
            if json_auth_route or unsafe_api_route or mcp_route:
                request_body_limit = (
                    MAX_ASSET_INTAKE_REQUEST_BODY_BYTES
                    if is_asset_intake
                    else (
                        MAX_MCP_REQUEST_BODY_BYTES
                        if mcp_route
                        else MAX_COMMAND_REQUEST_BODY_BYTES
                    )
                )
                content_length = request.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        return _error(400, "content_length_invalid")
                    if declared_length < 0 or declared_length > request_body_limit:
                        return _error(413, "request_body_too_large")
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > request_body_limit:
                        return _error(413, "request_body_too_large")
                    body.extend(chunk)
                request._body = bytes(body)

            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                "img-src 'self' data:; object-src 'none'; script-src 'self'; "
                "style-src 'self'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            if issued_session is not None:
                _set_session_cookie(response, issued_session)
            return response

        if not is_asset_intake:
            return await dispatch()
        try:
            await asyncio.wait_for(asset_intake_slots.acquire(), timeout=0.05)
        except TimeoutError:
            return _error(503, "asset_intake_busy")
        try:
            return await dispatch()
        finally:
            asset_intake_slots.release()

    @app.exception_handler(OwnerConflict)
    async def owner_conflict(_request: Request, error: OwnerConflict) -> JSONResponse:
        status_code = (
            404
            if error.code
            in {
                "asset_intake_not_found",
                "asset_not_found",
                "manual_question_creation_not_found",
                "quest_initialization_not_found",
                "writing_run_not_found",
            }
            else 409
        )
        detail: dict[str, object] = {"code": error.code}
        if error.code in {
            "research_memory_asset_intake_not_delivered",
            "deepfetch_not_delivered",
        }:
            detail["status"] = "capability_unavailable"
        return JSONResponse(status_code=status_code, content={"detail": detail})

    @app.exception_handler(HarnessAdmissionError)
    async def harness_admission_error(
        _request: Request, error: HarnessAdmissionError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": error.code}},
        )

    @app.exception_handler(SnapshotConsistencyUnavailable)
    async def snapshot_consistency_unavailable(
        _request: Request, _error: SnapshotConsistencyUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "snapshot_consistency_unavailable",
                    "status": "capability_unavailable",
                }
            },
        )

    @app.post("/internal/bootstrap-token")
    def issue_bootstrap_token() -> dict[str, str]:
        return {"bootstrap_token": runtime.authentication.issue_bootstrap_token()}

    @app.post("/internal/browser-grant")
    def issue_browser_grant() -> dict[str, str]:
        return {"browser_grant": runtime.authentication.issue_browser_grant()}

    @app.post("/internal/browser-grant-status")
    def browser_grant_status(exchange: BootstrapExchange) -> dict[str, bool]:
        return {
            "consumed": runtime.authentication.browser_grant_was_consumed(
                exchange.token
            )
        }

    @app.get("/internal/readiness")
    async def internal_readiness() -> dict[str, object]:
        health = worker_health_status()
        reconciliation = worker_check("quest_reconciliation_worker")
        drafting = worker_check("quest_drafting_worker")
        deepfetch = worker_check("first_question_deepfetch_worker")
        idea_stage = worker_check("idea_stage_worker")
        plan_stage = worker_check("plan_stage_worker")
        bundle_stage = worker_check("bundle_stage_worker")
        reasoning_stage = worker_check("reasoning_stage_worker")
        autonomous_creation = worker_check("autonomous_creation_worker")
        quest_completion = worker_check("quest_completion_worker")
        target_runs = worker_check("target_run_worker")
        research_assets = worker_check("research_asset_intake_worker")
        research_asset_verification = worker_check("research_asset_verification_worker")
        writing = worker_check("writing_worker")
        target_root = runtime.query_target_root_readiness()
        return {
            "status": health["status"],
            "revision": health["revision"],
            "reconciliation": {
                "status": reconciliation["status"],
                "last_error": worker_health["quest_reconciliation_worker"].last_error,
            },
            "drafting": {
                "status": drafting["status"],
                "last_error": worker_health["quest_drafting_worker"].last_error,
            },
            "deepfetch": {
                "status": deepfetch["status"],
                "last_error": worker_health["first_question_deepfetch_worker"].last_error,
            },
            "idea_stage": {
                "status": idea_stage["status"],
                "last_error": worker_health["idea_stage_worker"].last_error,
            },
            "plan_stage": {
                "status": plan_stage["status"],
                "last_error": worker_health["plan_stage_worker"].last_error,
            },
            "bundle_stage": {
                "status": bundle_stage["status"],
                "last_error": worker_health["bundle_stage_worker"].last_error,
            },
            "reasoning_stage": {
                "status": reasoning_stage["status"],
                "last_error": worker_health["reasoning_stage_worker"].last_error,
            },
            "autonomous_creation": {
                "status": autonomous_creation["status"],
                "last_error": worker_health["autonomous_creation_worker"].last_error,
            },
            "quest_completion": {
                "status": quest_completion["status"],
                "last_error": worker_health["quest_completion_worker"].last_error,
            },
            "target_runs": {
                "status": target_runs["status"],
                "last_error": worker_health["target_run_worker"].last_error,
            },
            "writing": {
                "status": writing["status"],
                "last_error": worker_health["writing_worker"].last_error,
            },
            "target_root": target_root,
            "research_assets": {
                "status": research_assets["status"],
                "last_error": worker_health["research_asset_intake_worker"].last_error,
            },
            "research_asset_verification": {
                "status": research_asset_verification["status"],
                "last_error": worker_health["research_asset_verification_worker"].last_error,
            },
        }

    @app.get("/internal/doctor")
    def internal_doctor() -> dict[str, object]:
        harness = runtime.harnesses.query_status()
        target_root = runtime.query_target_root_readiness()
        runtime_protection = runtime.query_runtime_observability()
        inhibitor = runtime_protection.get("inhibitor")
        capability = (
            inhibitor.get("capability")
            if isinstance(inhibitor, dict)
            else None
        )
        return {
            **harness,
            "status": (
                "ready"
                if harness.get("status") == "ready"
                and target_root.get("status") == "ready"
                and runtime_protection.get("status") == "ready"
                and isinstance(capability, dict)
                and capability.get("status") == "ready"
                else "unavailable"
            ),
            "target_root": target_root,
            "runtime_protection": runtime_protection,
        }

    @app.post("/internal/harness-conformance")
    def start_harness_conformance(
        request: StartHarnessConformanceRequest,
    ) -> dict[str, object]:
        return runtime.harnesses.start_full_conformance(
            FullConformanceRequest(**request.model_dump())
        ).as_public_dict()

    @app.post("/auth/bootstrap")
    def exchange_bootstrap(exchange: BootstrapExchange) -> JSONResponse:
        session = runtime.authentication.exchange_bootstrap_token(exchange.token)
        if session is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "bootstrap_token_invalid"},
            )
        response = JSONResponse(
            {
                "status": "authenticated",
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
            }
        )
        _set_session_cookie(response, session)
        return response

    @app.post("/mcp")
    async def semantic_mcp(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else None
        )
        try:
            message = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = None
        method = message.get("method") if isinstance(message, dict) else None
        if method != "initialize":
            protocol_version = request.headers.get("mcp-protocol-version")
            if protocol_version is None:
                return _error(400, "mcp_protocol_version_required")
            if protocol_version != MCP_PROTOCOL_VERSION:
                return _error(400, "mcp_protocol_version_unsupported")
        status, payload, response_session_id = await asyncio.to_thread(
            runtime.harnesses.dispatch_mcp_http,
            token,
            message,
            mcp_session_id=request.headers.get("mcp-session-id"),
        )
        response_headers = (
            {"Mcp-Session-Id": response_session_id}
            if response_session_id is not None
            else None
        )
        if payload is None:
            return Response(status_code=status, headers=response_headers)
        return JSONResponse(
            payload,
            status_code=status,
            headers=response_headers,
        )

    @app.post("/auth/launch")
    async def exchange_browser_grant(request: Request) -> FileResponse:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=415, detail={"code": "form_required"})
        if request.headers.get("origin") != "null":
            raise HTTPException(status_code=403, detail={"code": "origin_invalid"})
        body = await request.body()
        if len(body) > 512:
            raise HTTPException(status_code=400, detail={"code": "grant_invalid"})
        try:
            values = parse_qs(body.decode("ascii"), strict_parsing=True)
            grants = values["token"]
            if len(grants) != 1:
                raise ValueError
            grant = grants[0]
        except (KeyError, UnicodeDecodeError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail={"code": "grant_invalid"}
            ) from error
        session = await asyncio.to_thread(
            runtime.authentication.exchange_browser_grant, grant
        )
        if session is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "browser_grant_invalid"},
            )
        response = FileResponse(web_root / "index.html")
        _set_session_cookie(response, session)
        return response

    @app.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        csrf_token = request.headers.get("x-csrf-token", "")
        session_token = request.state.session_token
        if session_token is not None and not runtime.authentication.revoke_session(
            session_token, csrf_token
        ):
            if not trust_ssh_loopback:
                raise HTTPException(status_code=403, detail={"code": "csrf_invalid"})
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")
        return response

    @app.get("/api/v1/session")
    def session_status() -> dict[str, str]:
        return {"status": "authenticated"}

    @app.post("/api/v1/companion/messages", status_code=202)
    def send_companion_message(
        request: Request, message: CompanionMessageRequest
    ) -> dict[str, object]:
        scope_ref = require_current_collaboration_scope(
            message.scope_ref,
            conflict_code="companion_scope_stale",
        )
        return runtime.owners.human_collaboration.send_companion_message(
            scope_ref,
            message.message,
            _idempotency_key(request),
            view_context=(
                None
                if message.view_context is None
                else message.view_context.model_dump()
            ),
        )

    async def conversation_stream(
        request: Request, query: Callable[[], dict[str, object]]
    ) -> StreamingResponse:
        # Resolve the exact Owner identity before committing the HTTP status.
        initial = await asyncio.to_thread(query)
        return StreamingResponse(
            _chat_reply_stream(runtime, request, query, initial),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/companion/messages/{interaction_ref}/stream")
    async def stream_companion_reply(
        interaction_ref: str, request: Request, scope_ref: str
    ) -> StreamingResponse:
        return await conversation_stream(
            request,
            lambda: runtime.owners.human_collaboration.query_companion_reply(
                scope_ref, interaction_ref
            ),
        )

    @app.get(
        "/api/v1/quest-initializations/{initialization_id}/intent-session/"
        "turns/{turn_ref}/stream"
    )
    async def stream_intent_reply(
        initialization_id: str, turn_ref: str, request: Request
    ) -> StreamingResponse:
        return await conversation_stream(
            request,
            lambda: runtime.owners.human_collaboration.query_intent_reply(
                initialization_id, turn_ref
            ),
        )

    @app.get(
        "/api/v1/manual-question-creations/{context_ref}/drafting-session/"
        "turns/{turn_ref}/stream"
    )
    async def stream_manual_drafting_reply(
        context_ref: str, turn_ref: str, request: Request
    ) -> StreamingResponse:
        return await conversation_stream(
            request,
            lambda: runtime.owners.human_collaboration.query_manual_drafting_reply(
                context_ref, turn_ref
            ),
        )

    @app.post("/api/v1/human-requests/{request_ref}/responses", status_code=201)
    async def respond_to_human_request(
        request_ref: str,
        request: Request,
        response: HumanRequestResponseRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        material = response.linked_local_material
        if material is None:
            return runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision=response.decision,
                facts=response.facts,
                note=response.note,
                idempotency_key=idempotency_key,
            )

        current = runtime.owners.human_collaboration.query_human_request(request_ref)
        expected_material = {
            "external_material_api_access": ("material", "external_approval"),
            "offline_action": ("result", "offline_result"),
        }.get(None if current is None else current.get("kind"))
        if current is None or response.decision != "provided" or expected_material is None:
            raise OwnerConflict("human_request_material_response_invalid")
        fact_prefix, evidence_kind = expected_material

        sync_request = material.as_owner_request(
            request_ref=request_ref,
            evidence_kind=evidence_kind,
            asynchronous=False,
        )
        intake_key = "human-request-linked:" + canonical_hash(
            {
                "request_ref": request_ref,
                "response_idempotency_key": idempotency_key,
                "source_locator": material.source_locator,
                "fact_prefix": fact_prefix,
            }
        )
        facts = {
            **response.facts,
            f"{fact_prefix}_path": material.source_locator,
        }
        if current.get("status") != "open":
            responses = current.get("responses")
            if (
                not isinstance(responses, list)
                or not responses
                or not isinstance(responses[-1], dict)
                or not isinstance(responses[-1].get("facts"), dict)
            ):
                raise OwnerConflict("human_request_material_response_invalid")
            recorded_facts = responses[-1]["facts"]
            binding_names = tuple(
                f"{fact_prefix}_{suffix}"
                for suffix in (
                    "source_ref",
                    "version_ref",
                    "content_hash",
                    "manifest_hash",
                    "acceptance_receipt_ref",
                )
            )
            for name in binding_names:
                if name in recorded_facts:
                    facts[name] = recorded_facts[name]
            recorded = runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision=response.decision,
                facts=facts,
                note=response.note,
                idempotency_key=idempotency_key,
            )
            replay_request = (
                sync_request
                if all(name in recorded_facts for name in binding_names)
                else material.as_owner_request(
                    request_ref=request_ref,
                    evidence_kind=evidence_kind,
                    asynchronous=True,
                )
            )
            intake = runtime.owners.research_memory.query_asset_intake_by_idempotency_key(
                intake_key,
                replay_request,
            )
            if intake is None and replay_request.asynchronous:
                try:
                    intake = runtime.owners.research_memory.submit_asset_intake(
                        replay_request,
                        idempotency_key=intake_key,
                    )
                except Exception:
                    # The HumanResponse replay already succeeded. A later replay
                    # may retry this existing queue seam without gating it.
                    pass
            return {
                **recorded,
                "asset_intake": (
                    {
                        "job_ref": None,
                        "status": "not_queued",
                        "failure": {"code": "asset_intake_not_queued"},
                    }
                    if intake is None
                    else intake.as_public_dict()
                ),
            }
        try:
            intake_mode = await _await_bounded_asset_io(
                lambda: runtime.owners.research_memory.linked_local_intake_mode(
                    material.source_locator
                ),
                slots=asset_io_slots,
                timeout_code="asset_intake_classification_timeout",
            )
        except HTTPException:
            # A stalled mount is precisely the uncertain case: preserve the
            # human response first and let the existing durable worker inspect it.
            intake_mode = "asynchronous"
        if intake_mode == "synchronous":
            intake = await _await_bounded_asset_io(
                lambda: runtime.owners.research_memory.submit_asset_intake(
                    sync_request,
                    idempotency_key=intake_key,
                ),
                slots=asset_io_slots,
                timeout_code="asset_intake_operation_timeout",
            )
            if intake.status != "accepted" or intake.asset is None:
                raise OwnerConflict(
                    intake.failure_code or "asset_intake_not_terminal"
                )
            asset = intake.asset
            facts.update(
                {
                    f"{fact_prefix}_source_ref": asset.memory_ref,
                    f"{fact_prefix}_version_ref": asset.version_ref,
                    f"{fact_prefix}_content_hash": asset.content_hash,
                    f"{fact_prefix}_manifest_hash": asset.manifest_hash,
                    f"{fact_prefix}_acceptance_receipt_ref": (
                        asset.receipt.receipt_ref
                    ),
                }
            )
            recorded = runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision=response.decision,
                facts=facts,
                note=response.note,
                idempotency_key=idempotency_key,
            )
            return {**recorded, "asset_intake": intake.as_public_dict()}

        recorded = runtime.owners.human_collaboration.respond_to_human_request(
            request_ref,
            decision=response.decision,
            facts=facts,
            note=response.note,
            idempotency_key=idempotency_key,
        )
        async_request = material.as_owner_request(
            request_ref=request_ref,
            evidence_kind=evidence_kind,
            asynchronous=True,
        )
        try:
            intake = runtime.owners.research_memory.submit_asset_intake(
                async_request,
                idempotency_key=intake_key,
            )
            intake_public = intake.as_public_dict()
        except Exception as error:
            intake_public = {
                "job_ref": None,
                "status": "not_queued",
                "failure": {
                    "code": (
                        error.code
                        if isinstance(error, OwnerConflict)
                        else "asset_intake_enqueue_unavailable"
                    )
                },
            }
        return {**recorded, "asset_intake": intake_public}

    @app.post("/api/v1/human-requests/{request_ref}/retry")
    def retry_human_request_operation(
        request_ref: str,
        request: Request,
        _command: EmptyCommandRequest,
    ) -> dict[str, object]:
        def agent_retry_result(current: dict[str, object]) -> dict[str, object]:
            open_effect = current.get("open_effect")
            binding = (
                None
                if not isinstance(open_effect, dict)
                else open_effect.get("operation_binding")
            )
            waiters = current.get("direct_waiters")
            waiter = (
                waiters[0]
                if isinstance(waiters, list)
                and len(waiters) == 1
                and isinstance(waiters[0], dict)
                else None
            )
            validation = (
                None if waiter is None else waiter.get("resume_validation")
            )
            consumption = (
                None
                if not isinstance(validation, dict)
                else validation.get("consumption")
            )
            succeeded = bool(
                current.get("status") == "satisfied"
                and waiter is not None
                and waiter.get("status") == "consumed"
                and isinstance(binding, dict)
                and isinstance(consumption, dict)
                and consumption.get("work_ref") == binding.get("task_ref")
            )
            return {
                **current,
                "retry": {
                    "status": "succeeded" if succeeded else "processing"
                },
            }

        current = runtime.owners.agent_runtime.query_human_request(request_ref)
        target = None if current is None else current.get("target_assertion")
        idempotency_key = _idempotency_key(request)
        if (
            isinstance(target, dict)
            and target.get("schema_ref")
            == "meta-research/writing-system-operation-help/v1"
        ):
            return runtime.writing.retry_system_operation_help(
                request_ref,
                idempotency_key=idempotency_key,
            )
        if (
            current is None
            or current.get("kind") != "system_operation_help"
            or not isinstance(current.get("open_effect"), dict)
        ):
            raise OwnerConflict("system_operation_retry_unavailable")
        response_key = "system-operation-retry:" + canonical_hash(
            {"request_ref": request_ref, "idempotency_key": idempotency_key}
        )
        if current.get("status") == "satisfied":
            # Only the original command identity may replay a terminal Retry.
            runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision="provided",
                facts={"action": "retry"},
                note="",
                idempotency_key=response_key,
            )
            reconciled = runtime.owners.agent_runtime.reconcile_root_human_request(
                request_ref
            )
            return agent_retry_result(current if reconciled is None else reconciled)
        if current.get("status") != "open":
            raise OwnerConflict("system_operation_retry_unavailable")
        runtime.owners.human_collaboration.respond_to_human_request(
            request_ref,
            decision="provided",
            facts={"action": "retry"},
            note="",
            idempotency_key=response_key,
        )
        reconciled = runtime.owners.agent_runtime.reconcile_root_human_request(
            request_ref
        )
        if reconciled is None:
            reconciled = runtime.owners.agent_runtime.query_human_request(request_ref)
        if reconciled is None:
            raise OwnerConflict("human_request_not_found")
        return agent_retry_result(reconciled)

    @app.post("/api/v1/human-collaboration/agent-proposals", status_code=201)
    def record_agent_proposal(
        request: Request, proposal: AgentProposalRequest
    ) -> dict[str, object]:
        scope_ref = require_current_collaboration_scope(
            proposal.scope_ref,
            conflict_code="agent_proposal_scope_stale",
        )
        return runtime.owners.human_collaboration.record_agent_proposal(
            scope_ref,
            proposal.proposal,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/agent-proposals/{proposal_ref}/soft-constraint",
        status_code=201,
    )
    def convert_agent_proposal_to_soft_constraint(
        proposal_ref: str,
        request: Request,
        conversion: AgentProposalConversionRequest,
    ) -> dict[str, object]:
        require_current_collaboration_scope(
            conversion.expected_scope_ref,
            conflict_code="agent_proposal_scope_stale",
        )
        return runtime.owners.human_collaboration.convert_agent_proposal_to_soft_constraint(
            proposal_ref,
            expected_scope_ref=conversion.expected_scope_ref,
            expected_proposal_hash=conversion.expected_proposal_hash,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/agent-proposals/{proposal_ref}/command-draft",
        status_code=201,
    )
    def convert_agent_proposal_to_command_draft(
        proposal_ref: str,
        request: Request,
        conversion: AgentProposalConversionRequest,
    ) -> dict[str, object]:
        require_current_collaboration_scope(
            conversion.expected_scope_ref,
            conflict_code="agent_proposal_scope_stale",
        )
        return (
            runtime.owners.human_collaboration.convert_agent_proposal_to_command_draft(
                proposal_ref,
                expected_scope_ref=conversion.expected_scope_ref,
                expected_proposal_hash=conversion.expected_proposal_hash,
                idempotency_key=_idempotency_key(request),
            )
        )

    @app.post(
        "/api/v1/human-collaboration/soft-constraints/{constraint_ref}/withdrawals"
    )
    def withdraw_soft_constraint(
        constraint_ref: str,
        request: Request,
        withdrawal: WithdrawSoftConstraintRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.withdraw_soft_constraint(
            constraint_ref,
            withdrawal.expected_revision,
            _idempotency_key(request),
        )

    @app.post("/api/v1/human-collaboration/commands", status_code=201)
    def create_command_draft(
        request: Request, command: CommandDraftRequest
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.create_command_draft(
            command.scope_ref,
            command.command,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/commands/{intent_id}/revisions",
        status_code=201,
    )
    def revise_command_draft(
        intent_id: str,
        request: Request,
        revision: CommandRevisionRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.revise_command_draft(
            intent_id,
            revision.expected_revision,
            revision.command,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/commands/{intent_id}/previews",
        status_code=201,
    )
    def preview_command(
        intent_id: str,
        request: Request,
        preview: CommandPreviewRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.preview_command(
            intent_id,
            preview.draft_revision,
            preview.draft_hash,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/commands/{intent_id}/confirmations",
        status_code=201,
    )
    def confirm_command(
        intent_id: str,
        request: Request,
        confirmation: CommandConfirmationRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.confirm_command(
            intent_id,
            confirmation.draft_revision,
            confirmation.draft_hash,
            confirmation.preview_ref,
            confirmation.preview_hash,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/commands/{intent_id}/executions",
        status_code=201,
    )
    def execute_confirmed_command(
        intent_id: str,
        request: Request,
        execution: CommandExecutionRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.execute_confirmed_command(
            intent_id,
            execution.confirmation_receipt_ref,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/human-collaboration/commands/{intent_id}/authorizations",
        status_code=201,
    )
    def decide_capability_authorization(
        intent_id: str,
        request: Request,
        authorization: CapabilityAuthorizationRequest,
    ) -> dict[str, object]:
        command = runtime.owners.human_collaboration.query_command(intent_id)
        receipt = command.get("confirmation_receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("receipt_ref") != authorization.confirmation_receipt_ref
        ):
            raise OwnerConflict("authorization_confirmation_invalid")
        decision = authorization.model_dump()
        if decision.get("capability") == "opentelemetry_export":
            scope = decision.get("scope")
            if not isinstance(scope, dict):
                raise OwnerConflict("telemetry_authorization_scope_invalid")
            runtime.validate_telemetry_authorization_request(
                scope_ref=str(command["scope_ref"]),
                capability="opentelemetry_export",
                decision=str(decision.get("decision")),
                scope=scope,
                confirmation_receipt_ref=authorization.confirmation_receipt_ref,
            )
        recorded = runtime.owners.human_collaboration.decide_capability_authorization(
            str(command["scope_ref"]),
            decision,
            _idempotency_key(request),
        )
        if recorded.get("capability") == "opentelemetry_export":
            try:
                runtime.apply_telemetry_authorization(recorded)
            except RuntimeProtectionUnavailable as error:
                raise OwnerConflict(error.code) from error
            except (OSError, ValueError) as error:
                raise OwnerConflict("telemetry_provider_unavailable") from error
        return recorded

    @app.post("/api/v1/quest-initializations", status_code=201)
    async def create_quest_initialization(
        request: Request,
        draft: OpenQuestRequest,
    ) -> dict[str, object]:
        owner_draft = draft.model_dump()
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.create_quest(
                owner_draft, idempotency_key
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/draft")
    async def revise_quest_initialization(
        initialization_id: str,
        request: Request,
        draft: ReviseQuestDraftV2Request,
    ) -> dict[str, object]:
        owner_draft = draft.draft.model_dump()
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.revise_quest_draft(
                initialization_id,
                owner_draft,
                draft.expected_draft_hash,
                idempotency_key,
                draft.expected_draft_revision,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/proposal",
        status_code=202,
    )
    async def generate_question_proposal(
        initialization_id: str,
        request: Request,
        generation: GenerateProposalRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.generate_question_proposal(
                initialization_id,
                generation.expected_draft_hash,
                idempotency_key,
                generation.expected_draft_revision,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/proposal-generations",
        status_code=202,
    )
    async def enqueue_question_proposal(
        initialization_id: str,
        request: Request,
        generation: GenerateProposalRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.generate_question_proposal(
                initialization_id,
                generation.expected_draft_hash,
                idempotency_key,
                generation.expected_draft_revision,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/proposal")
    async def save_question_proposal(
        initialization_id: str,
        request: Request,
        proposal: SaveProposalRequest,
    ) -> dict[str, object]:
        content = proposal.content.model_dump()
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.save_question_proposal(
                initialization_id,
                proposal.expected_draft_hash,
                content,
                idempotency_key,
                proposal.expected_draft_revision,
                proposal.expected_proposal_ref,
                proposal.expected_proposal_hash,
                proposal.explicit_review,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.post("/api/v1/quest-initializations/{initialization_id}/compute-probe")
    def observe_host_compute(
        initialization_id: str,
        request: Request,
        selection: ComputeProbeRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.observe_host_compute(
            initialization_id,
            selection.selected_device_uuids,
            _idempotency_key(request),
        )

    @app.post("/api/v1/quest-initializations/{initialization_id}/acquisition-session")
    def prepare_acquisition_session(
        initialization_id: str,
        request: Request,
        preparation: PrepareAcquisitionSessionRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.prepare_acquisition_session(
            initialization_id,
            preparation.expected_draft_hash,
            _idempotency_key(request),
            preparation.expected_draft_revision,
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/intent-session/messages",
        status_code=202,
    )
    def send_intent_message(
        initialization_id: str,
        request: Request,
        message: IntentMessageRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.send_intent_message(
            initialization_id,
            expected_draft_revision=message.expected_draft_revision,
            expected_draft_hash=message.expected_draft_hash,
            message=message.message,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/confirmation-preview",
        status_code=201,
    )
    async def preview_quest_confirmation(
        initialization_id: str,
        request: Request,
        preview: ConfirmationPreviewRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.preview_confirmation(
                initialization_id,
                quest_draft_revision=preview.quest_draft_revision,
                quest_draft_hash=preview.quest_draft_hash,
                proposal_ref=preview.proposal_ref,
                proposal_hash=preview.proposal_hash,
                idempotency_key=idempotency_key,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/confirmation",
        status_code=202,
    )
    async def confirm_quest_initialization(
        initialization_id: str,
        request: Request,
        confirmation: ConfirmQuestRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.confirm_quest(
                initialization_id,
                quest_draft_revision=confirmation.quest_draft_revision,
                quest_draft_hash=confirmation.quest_draft_hash,
                proposal_ref=confirmation.proposal_ref,
                proposal_hash=confirmation.proposal_hash,
                preview_ref=confirmation.preview_ref,
                preview_hash=confirmation.preview_hash,
                idempotency_key=idempotency_key,
            ),
            slots=asset_io_slots,
            timeout_code="quest_material_io_timeout",
        )

    @app.post("/api/v1/quest-initializations/{initialization_id}/cancel")
    def cancel_quest_initialization(
        initialization_id: str, request: Request
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.cancel_quest(
            initialization_id, _idempotency_key(request)
        )

    @app.get("/api/v1/quest-initializations/current")
    def query_current_quest_initialization() -> dict[str, object] | None:
        return runtime.owners.human_collaboration.query_current_quest_creation()

    @app.get("/api/v1/quest-initializations/{initialization_id}")
    def query_quest_initialization(
        initialization_id: str,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )

    @app.get("/api/v1/quest-initializations/{initialization_id}/proposal-output")
    def query_proposal_output(
        initialization_id: str,
        generation_ref: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=65536, ge=4, le=262144),
    ) -> JSONResponse:
        from meta_research.proposal_output import read_proposal_output
        from meta_research.provider_supervisor import ProviderSupervisorError

        creation = runtime.owners.human_collaboration.query_quest_creation(initialization_id)
        generation = creation.get("proposal_generation")
        if not generation or generation["ref"] != generation_ref:
            return _error(409, "proposal_output_generation_changed")
        try:
            page = read_proposal_output(runtime.data_root.root, generation, after, limit)
        except (OSError, ValueError, ProviderSupervisorError) as error:
            code = str(error) if isinstance(error, ValueError) else "proposal_output_unavailable"
            return _error(409 if "cursor" in code else 503, code)
        return JSONResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/quest-initializations/{initialization_id}/intent-session")
    def query_intent_session(initialization_id: str) -> dict[str, object]:
        view = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        return {"intent_session": view["intent_session"]}

    @app.post("/api/v1/manual-question-creations", status_code=201)
    def open_manual_question_creation(
        request: Request,
        target: OpenManualQuestionCreationRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.open_manual_question_creation(
            quest_ref=target.quest_ref,
            parent_question_ref=target.parent_question_ref,
            idempotency_key=_idempotency_key(request),
        )

    @app.get("/api/v1/manual-question-creations/current")
    def query_current_manual_question_creation(
        quest_ref: str = Query(min_length=1, max_length=64),
        parent_question_ref: str = Query(min_length=1, max_length=64),
    ) -> dict[str, object] | None:
        return (
            runtime.owners.human_collaboration.query_current_manual_question_creation(
                quest_ref=quest_ref,
                parent_question_ref=parent_question_ref,
            )
        )

    @app.get("/api/v1/manual-question-creations/{context_ref}")
    def query_manual_question_creation(context_ref: str) -> dict[str, object]:
        return runtime.owners.human_collaboration.query_manual_question_creation(
            context_ref
        )

    @app.post(
        "/api/v1/manual-question-creations/{context_ref}/seed-confirmation",
        status_code=201,
    )
    async def confirm_manual_creation_seed(
        context_ref: str,
        request: Request,
        confirmation: ConfirmManualSeedRequest,
    ) -> dict[str, object]:
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.confirm_manual_creation_seed(
                context_ref,
                seed=confirmation.seed.model_dump(),
                idempotency_key=_idempotency_key(request),
            ),
            slots=asset_io_slots,
            timeout_code="manual_creation_material_io_timeout",
        )

    @app.post(
        "/api/v1/manual-question-creations/{context_ref}/deepfetch",
        status_code=202,
    )
    def start_manual_creation_deepfetch(
        context_ref: str,
        request: Request,
        selection: ManualResearchPathRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.start_manual_creation_deepfetch(
            context_ref,
            expected_seed_ref=selection.expected_seed_ref,
            expected_seed_hash=selection.expected_seed_hash,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/manual-question-creations/{context_ref}/deepfetch-waiver",
        status_code=201,
    )
    def record_manual_deepfetch_waiver(
        context_ref: str,
        request: Request,
        selection: ManualResearchPathRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.record_manual_deepfetch_waiver(
            context_ref,
            expected_seed_ref=selection.expected_seed_ref,
            expected_seed_hash=selection.expected_seed_hash,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/manual-question-creations/{context_ref}/drafting-session/messages"
    )
    def send_manual_drafting_message(
        context_ref: str,
        request: Request,
        turn: ManualDraftingMessageRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.send_manual_drafting_message(
            context_ref,
            expected_basis_hash=turn.expected_basis_hash,
            message=turn.message,
            idempotency_key=_idempotency_key(request),
        )

    @app.put("/api/v1/manual-question-creations/{context_ref}/proposal")
    async def save_manual_question_proposal(
        context_ref: str,
        request: Request,
        proposal: SaveManualQuestionProposalRequest,
    ) -> dict[str, object]:
        return await _await_bounded_asset_io(
            lambda: runtime.owners.human_collaboration.save_manual_question_proposal(
                context_ref,
                content=proposal.content.model_dump(),
                expected_basis_hash=proposal.expected_basis_hash,
                expected_proposal_ref=proposal.expected_proposal_ref,
                expected_proposal_hash=proposal.expected_proposal_hash,
                idempotency_key=_idempotency_key(request),
            ),
            slots=asset_io_slots,
            timeout_code="manual_creation_material_io_timeout",
        )

    @app.post(
        "/api/v1/manual-question-creations/{context_ref}/proposal-confirmation",
        status_code=202,
    )
    def confirm_manual_question_proposal(
        context_ref: str,
        request: Request,
        confirmation: ConfirmManualQuestionProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.confirm_manual_question_proposal(
            context_ref,
            proposal_ref=confirmation.proposal_ref,
            proposal_hash=confirmation.proposal_hash,
            idempotency_key=_idempotency_key(request),
        )

    @app.post("/api/v1/manual-question-creations/{context_ref}/cancel")
    def cancel_manual_question_creation(
        context_ref: str,
        request: Request,
        _empty: EmptyCommandRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.cancel_manual_question_creation(
            context_ref, _idempotency_key(request)
        )

    @app.get("/api/v1/preferences")
    def get_preferences() -> dict[str, object]:
        from meta_research.system_prompt import read_output_language
        return {"output_language":read_output_language(runtime.data_root.root)}

    @app.get("/api/v1/quests/{quest_ref}/runtime-conditions")
    def get_runtime_conditions(quest_ref: str) -> dict[str, str]:
        from meta_research.runtime_conditions import read_runtime_conditions
        return read_runtime_conditions(runtime.data_root.root, quest_ref)

    @app.put("/api/v1/quests/{quest_ref}/runtime-conditions")
    def put_runtime_conditions(quest_ref: str, conditions: RuntimeConditionsRequest) -> dict[str, str]:
        from meta_research.runtime_conditions import save_runtime_conditions
        return save_runtime_conditions(runtime.data_root.root, quest_ref,
                                       **conditions.model_dump())

    @app.put("/api/v1/preferences")
    def set_preferences(preference: OutputLanguagePreference) -> dict[str, object]:
        payload = preference.model_dump()
        import os
        import tempfile
        if set(payload)!={"output_language"} or payload["output_language"] not in {"zh","en"}:
            raise OwnerConflict("output_language_invalid")
        path=runtime.data_root.root / "user-preferences.json"
        fd,name=tempfile.mkstemp(prefix=".user-preferences-",dir=path.parent)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as f:
                json.dump(payload,f);f.flush();os.fsync(f.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
        return payload

    @app.post("/api/v1/research-inputs",status_code=201)
    def submit_research_input(request: Request, research_input: ResearchInputRequest) -> dict[str, object]:
        payload = research_input.model_dump()
        if set(payload)-{"quest_ref","question_ref","text","asset_bindings"}:
            raise OwnerConflict("human_input_invalid")
        return runtime.owners.human_collaboration.submit_research_input(
            quest_ref=payload.get("quest_ref"),question_ref=payload.get("question_ref"),
            text_content=payload.get("text"),asset_bindings=payload.get("asset_bindings",[]),
            idempotency_key=_idempotency_key(request))

    @app.get("/api/v1/research-formal")
    def read_research_formal(quest_ref: str,ref: str) -> dict[str,object]:
        graph=runtime.owners.research_graph
        result=graph.query_formal_result_by_ref(ref,quest_ref=quest_ref)
        if result is None:raise OwnerConflict("formal_result_quest_scope_invalid")
        items=[]
        for artifact in result.get("run_artifacts",[])+result.get("evaluation_artifacts",[]):
            version=artifact.get("version_ref") or artifact.get("asset_version_ref")
            if version:
                items.append({"ref":artifact.get("role_ref"),"name":artifact.get("role","Research product"),
                              "summary":artifact.get("declared_relative_path",artifact.get("relative_path","")),
                              "reader":{"source_ref":version,"version_ref":version},"details":artifact})
        return {"result":result,"items":items,"next_offset":None}

    @app.get("/api/v1/research-content")
    def read_research_content(quest_ref: str,source_ref: str,version_ref: str,
            offset: int=Query(default=0,ge=0),limit: int=Query(default=8192,ge=1,le=65536),
            entry_path: str|None=None) -> dict[str, object]:
        from meta_research.research_content import read_content
        return read_content(runtime.owners.research_graph,runtime.owners.research_memory,
            quest_ref=quest_ref,source_ref=source_ref,version_ref=version_ref,offset=offset,limit=limit,
            entry_path=entry_path,human_collaboration=runtime.owners.human_collaboration)

    @app.get("/api/v1/research-library/{entry}")
    def query_research_library(entry: str,quest_ref: str,query: str="",offset: int=Query(default=0,ge=0),
            limit: int=Query(default=12,ge=1,le=100),dataset_ref: str|None=None,environment_ref: str|None=None,
            baseline_ref: str|None=None,variant_ref: str|None=None,question_ref: str|None=None,
            request_cursor: str|None=Query(default=None,max_length=512)) -> dict[str,object]:
        from meta_research.research_content import discover_questions,discover_literature
        graph=runtime.owners.research_graph;memory=runtime.owners.research_memory
        if graph.query_quest_by_ref(quest_ref) is None:raise OwnerConflict("research_library_quest_invalid")
        args={"quest_ref":quest_ref,"query":query,"offset":offset,"limit":limit}
        if entry=="questions":
            if question_ref:
                question=graph.query_question_history_by_ref(question_ref)
                if question is None or question.quest_ref!=quest_ref:
                    raise OwnerConflict("research_history_source_unbound")
                page=graph.query_question_research_history(quest_ref=quest_ref,question_ref=question_ref,
                                                          offset=offset,limit=min(limit,12))
                page["items"]=[{**item,"ref":item["source_ref"],"name":question_ref,
                    "summary":item.get("claim",{}).get("text",""),
                    "reader":{"source_ref":item["source_ref"],"version_ref":item["source_ref"]}}
                    for item in page["items"]]
                return page
            return discover_questions(graph,memory,**args)
        if entry=="literature":return discover_literature(graph,memory,**args)
        if entry=="baselines":
            if baseline_ref:
                result=graph.query_baseline(baseline_ref,quest_ref=quest_ref)
                if result is None:return {"items":[],"next_offset":None}
                return graph.query_baseline_variants(baseline_ref,variant_ref=variant_ref,quest_ref=quest_ref,offset=offset,limit=limit)
            page=graph.query_baselines(**args)
            page["items"]=[{**x,"name":x.get("method_key") or x["baseline_ref"],"ref":x["baseline_ref"]} for x in page["items"]]
            return page
        if entry=="datasets":
            if dataset_ref:args={**args,"dataset_ref":dataset_ref,"query":""}
            page=graph.query_datasets(**args)
            for item in page["items"]:
                item["summary"]=item.get("meaning","")
                item["name"]=item.get("name",item.get("version_label",""))
                item["ref"]=item.get("dataset_version_ref",item.get("dataset_ref"))
                bindings=item.get("asset_bindings",[])
                if bindings:
                    item["reader"]={"source_ref":item["dataset_version_ref"],"version_ref":bindings[0]["version_ref"]}
                    item["readers"]=[{"source_ref":item["dataset_version_ref"],"version_ref":b["version_ref"]} for b in bindings]
            return page
        if entry=="environments":
            if environment_ref:args={**args,"environment_ref":environment_ref,"query":""}
            page=graph.query_environments(**args)
            for item in page["items"]:
                if "environment_reference_ref" in item:
                    item["summary"]=item["purpose"]
                    item["ref"]=item["environment_reference_ref"]
                    continue
                item["summary"]=item["meaning"]
                item["ref"]=item["environment_ref"]
                item["readers"]=[{"source_ref":item["environment_ref"],"version_ref":binding["version_ref"]}
                                 for binding in item["asset_bindings"]]
                if item["readers"]:
                    item["reader"]=item["readers"][0]
            return page
        if entry=="human":
            hc=runtime.owners.human_collaboration
            page=hc.query_research_inputs(**args) if request_cursor is None else {"items":[],"next_offset":None}
            if offset==0 or request_cursor is not None:
                requests=hc.query_research_help_page(quest_ref=quest_ref,cursor=request_cursor)
                for request in requests["items"]:
                    if query and query.casefold() not in str(request).casefold():continue
                    for response in request.get("responses",[]):
                        page["items"].append({"ref":response["response_ref"],"name":request.get("obligation","Human request"),
                            "summary":response.get("note","")[:1200],"status":request["status"],
                            "reader":{"source_ref":response["response_ref"],"version_ref":canonical_hash(response)}})
                page["request_next_cursor"]=requests["next_cursor"]
            return page
        raise OwnerConflict("research_library_entry_invalid")

    @app.get("/api/v1/literature-snapshots/{snapshot_ref}")
    def query_literature_snapshot(snapshot_ref: str) -> dict[str, object]:
        return runtime.owners.research_memory.read_literature_snapshot(snapshot_ref)

    @app.get("/api/v1/research-assets")
    def query_research_assets(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, object]:
        return runtime.projection.query_snapshot(
            asset_offset=offset,
            asset_limit=limit,
            include_history=False,
            include_stages=False,
        )["research_assets"]

    @app.post("/api/v1/research-assets/intakes")
    async def submit_research_asset(
        request: Request,
        intake: AssetIntakeWebRequest,
    ) -> JSONResponse:
        owner_request = intake.as_owner_request()
        idempotency_key = _idempotency_key(request)
        try:
            result = await _await_bounded_asset_io(
                lambda: runtime.owners.research_memory.submit_asset_intake(
                    owner_request,
                    idempotency_key=idempotency_key,
                ),
                slots=asset_io_slots,
                timeout_code="asset_intake_io_timeout",
            )
        except HTTPException as error:
            if error.detail != {"code": "asset_intake_io_timeout"}:
                raise
            recover_intake = (
                runtime.owners.research_memory.query_asset_intake_by_idempotency_key
            )
            result = await _await_bounded_asset_io(
                lambda: recover_intake(idempotency_key, owner_request),
                slots=asset_intake_recovery_slots,
                timeout_code="asset_intake_recovery_io_timeout",
            )
            if result is None:
                raise error
        return JSONResponse(
            status_code=(202 if result.status in {"queued", "processing"} else 201),
            content=result.as_public_dict(),
        )

    @app.get("/api/v1/research-assets/intakes/{job_ref}")
    def query_research_asset_intake(job_ref: str) -> dict[str, object]:
        return runtime.owners.research_memory.query_asset_intake(
            job_ref
        ).as_public_dict()

    @app.get("/api/v1/research-assets/roles")
    def query_research_asset_roles(
        quest_ref: str | None = None,
        role: Literal["evidence", "quest_source_material"] | None = None,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=50, ge=1, le=ASSET_ROLE_QUERY_MAX_PAGE_SIZE),
    ) -> dict[str, object]:
        before_timestamp, before_ref = _decode_history_cursor(cursor)
        for _attempt in range(3):
            revision_before = (
                runtime.owners.research_graph.query_asset_reference_revision()
            )
            rows = runtime.owners.research_graph.query_asset_roles(
                quest_ref=quest_ref,
                role=role,
                limit=limit + 1,
                newest_first=True,
                before_timestamp=before_timestamp,
                before_ref=before_ref,
            )
            revision_after = (
                runtime.owners.research_graph.query_asset_reference_revision()
            )
            if revision_before == revision_after:
                page = _history_page(
                    rows,
                    limit=limit,
                    timestamp_field="accepted_at",
                    ref_field="role_ref",
                )
                return {**page, "reference_revision": revision_after}
        raise SnapshotConsistencyUnavailable

    @app.get("/api/v1/research-assets/{memory_ref}")
    def query_research_asset(memory_ref: str) -> dict[str, object]:
        for _attempt in range(3):
            feed_before = runtime.feed.query_readiness().current_revision
            research_memory_revision = (
                runtime.owners.research_memory.query_projection_snapshot().revision
            )
            item = runtime.owners.research_memory.query_asset_projection_inventory_item(
                memory_ref
            )
            if item is None:
                raise OwnerConflict("asset_not_found")
            custodies = runtime.owners.research_memory.query_asset_custodies(memory_ref)
            roles = runtime.owners.research_graph.query_asset_roles(
                version_refs=(memory_ref,),
                limit=ASSET_PROJECTION_HISTORY_PER_VERSION,
                newest_first=True,
            )
            holds = runtime.owners.research_memory.query_asset_holds(
                memory_refs=(memory_ref,),
                limit_per_version=ASSET_PROJECTION_HISTORY_PER_VERSION,
            )
            assessments = (
                runtime.owners.research_memory.query_release_eligibility_assessments(
                    memory_ref,
                    limit=ASSET_PROJECTION_HISTORY_PER_VERSION,
                    newest_first=True,
                )
            )
            reference_revision = (
                runtime.owners.research_graph.query_asset_reference_revision()
            )
            feed_after = runtime.feed.query_readiness().current_revision
            if feed_before == feed_after:
                return {
                    **item.as_public_dict(),
                    "custodies": [row.as_public_dict() for row in custodies],
                    "roles": [row.as_public_dict() for row in roles],
                    "holds": [row.as_public_dict() for row in holds],
                    "release_assessments": [
                        row.as_public_dict() for row in assessments
                    ],
                    "revision": feed_after,
                    "inventory_revision": research_memory_revision,
                    "reference_revision": reference_revision,
                }
        raise SnapshotConsistencyUnavailable

    @app.get("/api/v1/research-assets/{memory_ref}/roles")
    def query_research_asset_role_history(
        memory_ref: str,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE),
    ) -> dict[str, object]:
        before_timestamp, before_ref = _decode_history_cursor(cursor)
        rows = runtime.owners.research_graph.query_asset_roles(
            version_refs=(memory_ref,),
            limit=limit + 1,
            newest_first=True,
            before_timestamp=before_timestamp,
            before_ref=before_ref,
        )
        return _history_page(
            rows,
            limit=limit,
            timestamp_field="accepted_at",
            ref_field="role_ref",
        )

    @app.get("/api/v1/research-assets/{memory_ref}/holds")
    def query_research_asset_hold_history(
        memory_ref: str,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE),
    ) -> dict[str, object]:
        before_timestamp, before_ref = _decode_history_cursor(cursor)
        rows = runtime.owners.research_memory.query_asset_holds(
            memory_ref,
            limit=limit + 1,
            newest_first=True,
            before_timestamp=before_timestamp,
            before_ref=before_ref,
        )
        return _history_page(
            rows,
            limit=limit,
            timestamp_field="placed_at",
            ref_field="hold_ref",
        )

    @app.get("/api/v1/research-assets/{memory_ref}/release-assessments")
    def query_research_asset_release_history(
        memory_ref: str,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE),
    ) -> dict[str, object]:
        before_timestamp, before_ref = _decode_history_cursor(cursor)
        rows = runtime.owners.research_memory.query_release_eligibility_assessments(
            memory_ref,
            limit=limit + 1,
            newest_first=True,
            before_timestamp=before_timestamp,
            before_ref=before_ref,
        )
        return _history_page(
            rows,
            limit=limit,
            timestamp_field="assessed_at",
            ref_field="assessment_ref",
        )

    @app.get("/api/v1/research-assets/{memory_ref}/content")
    async def materialize_research_asset(memory_ref: str) -> Response:
        return AssetDownloadResponse(
            memory=runtime.owners.research_memory, memory_ref=memory_ref,
            root=runtime.data_root.run / "asset-downloads", slots=asset_io_slots,
        )

    @app.post("/api/v1/research-assets/{memory_ref}/custody/managed")
    async def handoff_research_asset_custody(
        memory_ref: str,
        request: Request,
        _command: EmptyCommandRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        accepted = await asset_handoff_singleflight.run(
            (memory_ref, idempotency_key),
            lambda: runtime.owners.research_memory.handoff_asset_to_managed(
                memory_ref,
                idempotency_key=idempotency_key,
            ),
            slots=asset_io_slots,
            timeout_code="asset_custody_io_timeout",
        )
        return accepted.as_public_dict()

    @app.post("/api/v1/research-assets/{memory_ref}/roles", status_code=201)
    async def accept_research_asset_role(
        memory_ref: str,
        request: Request,
        role: AssetRoleRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)

        def command():
            accepted = runtime.owners.research_memory.query_asset_version(memory_ref)
            if accepted is None:
                raise OwnerConflict("asset_not_found")
            return runtime.owners.research_graph.accept_asset_role(
                binding=accepted.as_binding(),
                role=role.role,
                quest_ref=role.quest_ref,
                idempotency_key=idempotency_key,
            )

        accepted_role = await _await_bounded_asset_io(
            command,
            slots=asset_io_slots,
            timeout_code="asset_role_io_timeout",
        )
        return accepted_role.as_public_dict()

    @app.post("/api/v1/research-assets/{memory_ref}/holds", status_code=201)
    def place_research_asset_hold(
        memory_ref: str,
        request: Request,
        hold: AssetHoldRequest,
    ) -> dict[str, object]:
        return runtime.owners.research_memory.place_asset_hold(
            memory_ref,
            reason=hold.reason,
            idempotency_key=_idempotency_key(request),
        ).as_public_dict()

    @app.post("/api/v1/research-assets/holds/{hold_ref}/release")
    def release_research_asset_hold(
        hold_ref: str,
        request: Request,
        _command: EmptyCommandRequest,
    ) -> dict[str, object]:
        return runtime.owners.research_memory.release_asset_hold(
            hold_ref,
            idempotency_key=_idempotency_key(request),
        ).as_public_dict()

    @app.post(
        "/api/v1/research-assets/{memory_ref}/release-eligibility",
        status_code=201,
    )
    async def assess_research_asset_release(
        memory_ref: str,
        request: Request,
        assessment: ReleaseEligibilityRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        result = await _await_bounded_asset_io(
            lambda: runtime.owners.research_memory.assess_release_eligibility(
                memory_ref,
                expected_reference_revision=(assessment.expected_reference_revision),
                idempotency_key=idempotency_key,
            ),
            slots=asset_io_slots,
            timeout_code="asset_release_io_timeout",
        )
        return result.as_public_dict()

    @app.get("/api/v1/questions/{question_ref}/history")
    def query_question_history(
        question_ref: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, object]:
        return runtime.projection.query_question_history(
            question_ref, offset=offset, limit=limit
        )

    @app.get("/api/v1/questions/{question_ref}/evidence")
    def query_question_evidence(question_ref: str) -> dict[str, object]:
        return runtime.projection.query_question_evidence(question_ref)

    @app.get("/api/v1/health")
    async def query_health() -> dict[str, object]:
        # Pure in-memory checks also stay responsive if the sync thread pool is busy.
        return worker_health_status()

    @app.get("/api/v1/status")
    def query_runtime_status() -> dict[str, object]:
        nonlocal latest_status_revision
        result = status_reader.query()
        latest_status_revision = result["revision"]
        from .runtime_status import project_worker_health

        project_worker_health(result, worker_health_status())
        return result

    @app.get("/api/v1/snapshot")
    async def query_snapshot(
        request: Request, include_assets: bool = True, include_history: bool = True,
    ) -> dict[str, object]:
        include_assets = include_assets and request.headers.get("X-Meta-Research-Snapshot-Assets") != "defer"
        include_history = include_history and request.headers.get("X-Meta-Research-Snapshot-History") != "defer"
        try:
            return await snapshot_queries.query(
                include_assets=include_assets, include_history=include_history,
            )
        except TimeoutError as error:
            raise HTTPException(status_code=503, detail={"code": "snapshot_query_timeout"}, headers={"Retry-After": "5"}) from error

    @app.get("/api/v1/research-overview")
    def query_research_overview(
        quest_ref: str = Query(min_length=1, max_length=128),
    ) -> dict[str, object]:
        owners = runtime.owners
        reader = ResearchOverviewReader(
            owners.research_graph,
            owners.advancement_engine,
            owners.research_memory,
            owners.agent_runtime,
        )
        try:
            with runtime._database.read_snapshot(), measure_query("research_overview"):
                return reader._query_once(quest_ref)
        except OwnerConflict as error:
            raise HTTPException(
                status_code=(
                    404 if error.code == "research_overview_quest_not_found" else 503
                ),
                detail={"code": error.code},
            ) from error

    @app.get("/api/v1/quests/{quest_ref}/timeline-summaries")
    def query_timeline_summaries(quest_ref: str) -> dict[str, object]:
        if len(quest_ref) > 128 or runtime.owners.research_graph.query_quest_by_ref(quest_ref) is None:
            raise HTTPException(status_code=404, detail={"code": "quest_not_found"})
        if recorder is None:
            raise HTTPException(status_code=503, detail={"code": "timeline_summary_unavailable"})
        try:
            return recorder.query(quest_ref)
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(status_code=503, detail={"code": "timeline_summary_unavailable"}) from error

    @app.get("/api/v1/writing")
    def query_writing() -> dict[str, object]:
        return runtime.writing.query_overview()

    @app.post("/api/v1/writing/intents", status_code=201)
    def create_writing_intent(
        request: Request, intent: WritingIntentRequest
    ) -> dict[str, object]:
        return runtime.writing.create_intent(
            **intent.model_dump(), idempotency_key=_idempotency_key(request)
        )

    @app.post("/api/v1/writing/intents/{intent_id}/preview")
    def preview_writing_intent(
        request: Request, intent_id: str, _command: EmptyCommandRequest
    ) -> dict[str, object]:
        return runtime.writing.preview_intent(
            intent_id, idempotency_key=_idempotency_key(request)
        )

    @app.post("/api/v1/writing/intents/{intent_id}/confirmation")
    def confirm_writing_intent(
        request: Request,
        intent_id: str,
        confirmation: WritingConfirmationRequest,
    ) -> dict[str, object]:
        return runtime.writing.confirm_intent(
            intent_id,
            **confirmation.model_dump(),
            idempotency_key=_idempotency_key(request),
        )

    @app.get("/api/v1/writing/runs/{run_ref}")
    def query_writing_run(run_ref: str) -> dict[str, object]:
        return runtime.writing.query_writing_report(run_ref)

    @app.post("/api/v1/writing/runs/{run_ref}/control")
    def control_writing_run(
        request: Request, run_ref: str, command: WritingControlRequest
    ) -> dict[str, object]:
        return runtime.writing.control_report(
            run_ref,
            action=command.action,
            expected_attempt_ref=command.expected_attempt_ref,
            expected_fence_ref=command.expected_fence_ref,
            idempotency_key=_idempotency_key(request),
        )

    @app.post("/api/v1/writing/runs/{run_ref}/cancellation-intents")
    def preview_writing_cancellation(
        request: Request, run_ref: str, _command: EmptyCommandRequest
    ) -> dict[str, object]:
        return runtime.writing.preview_report_cancellation(
            run_ref, idempotency_key=_idempotency_key(request)
        )

    @app.post(
        "/api/v1/writing/runs/{run_ref}/cancellation-intents/"
        "{cancellation_intent_id}/confirmation"
    )
    def confirm_writing_cancellation(
        request: Request,
        run_ref: str,
        cancellation_intent_id: str,
        confirmation: WritingConfirmationRequest,
    ) -> dict[str, object]:
        return runtime.writing.confirm_report_cancellation(
            run_ref,
            cancellation_intent_id,
            **confirmation.model_dump(),
            idempotency_key=_idempotency_key(request),
        )

    @app.post("/api/v1/writing/runs/{run_ref}/revisions")
    def revise_writing_run(
        request: Request, run_ref: str, revision: WritingRevisionRequest
    ) -> dict[str, object]:
        return runtime.writing.request_revision(
            run_ref,
            feedback=tuple(revision.feedback),
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/writing/runs/{run_ref}/delivery-intents",
        status_code=201,
    )
    def create_writing_delivery_intent(
        request: Request,
        run_ref: str,
        intent: WritingDeliveryIntentRequest,
    ) -> dict[str, object]:
        value = intent.model_dump()
        return runtime.writing.create_delivery_intent(
            run_ref,
            action=value["action"],
            provider_ref=value["provider_ref"],
            target=value["target"],
            output_format=value["output_format"],
            idempotency_key=_idempotency_key(request),
        )

    @app.post("/api/v1/writing/delivery-intents/{intent_id}/preview")
    def preview_writing_delivery_intent(
        request: Request,
        intent_id: str,
        _command: EmptyCommandRequest,
    ) -> dict[str, object]:
        return runtime.writing.preview_delivery_intent(
            intent_id,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/writing/delivery-intents/{intent_id}/confirmation"
    )
    def confirm_writing_delivery_intent(
        request: Request,
        intent_id: str,
        confirmation: WritingConfirmationRequest,
    ) -> dict[str, object]:
        return runtime.writing.confirm_delivery_intent(
            intent_id,
            **confirmation.model_dump(),
            idempotency_key=_idempotency_key(request),
        )

    @app.get("/api/v1/writing/deliveries/{operation_ref}")
    def query_writing_delivery_operation(
        operation_ref: str,
    ) -> dict[str, object]:
        return runtime.writing.query_delivery_operation(operation_ref)

    @app.get("/api/v1/writing/runs/{run_ref}/compare")
    def compare_writing_versions(
        run_ref: str,
        left_version_ref: str = Query(min_length=1, max_length=96),
        right_version_ref: str = Query(min_length=1, max_length=96),
    ) -> dict[str, object]:
        return runtime.writing.compare_report_versions(
            run_ref,
            left_version_ref=left_version_ref,
            right_version_ref=right_version_ref,
        )

    @app.get(
        "/api/v1/writing/runs/{run_ref}/versions/{version_ref}/content"
    )
    def view_writing_report_version(run_ref: str, version_ref: str) -> Response:
        viewed = runtime.writing.view_report_version(
            run_ref,
            version_ref=version_ref,
        )
        return Response(
            content=viewed["content"],
            media_type="text/markdown",
            headers={
                "X-Writing-Version-Ref": str(viewed["version_ref"]),
                "X-Writing-Content-Hash": str(viewed["content_hash"]),
                "X-Writing-Citation-Status": str(viewed["citation_status"]),
                "X-Writing-Formal-Renderer": "false",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/writing/runs/{run_ref}/render")
    def render_writing_report(
        run_ref: str,
        format: str | None = Query(default=None, min_length=1, max_length=32),
        version_ref: str | None = Query(
            default=None, min_length=1, max_length=96
        ),
    ) -> Response:
        rendered = runtime.writing.render_report(
            run_ref,
            version_ref=version_ref,
            format=format,
        )
        return Response(
            content=rendered["content"],
            media_type=str(rendered["media_type"]).split(";", 1)[0],
            headers={
                "X-Writing-Version-Ref": str(rendered["version_ref"]),
                "X-Writing-Render-Hash": str(rendered["render_hash"]),
                "X-Writing-Render-Format": str(rendered["format"]),
                "Content-Disposition": (
                    "attachment; filename=\""
                    + str(rendered.get("file_name", "writing-output"))
                    + "\""
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/root-capability-diagnostics")
    def query_root_capability_diagnostics(
        root_kind: RootAgentKind | None = Query(default=None),
        operation_ref: str | None = Query(
            default=None, min_length=1, max_length=512
        ),
        limit: int = Query(default=128, ge=1, le=256),
    ) -> dict[str, object]:
        try:
            return runtime.root_operation_diagnostics.query_page(
                root_kind=root_kind,
                operation_ref=operation_ref,
                limit=limit,
            )
        except RootOperationDiagnosticError as error:
            raise HTTPException(
                status_code=503,
                detail={"code": error.code},
            ) from error

    @app.get("/api/v1/idea-stage/current")
    def query_current_idea_stage() -> dict[str, object]:
        return runtime.idea_stage.query_current()

    @app.get("/api/v1/plan-stage/current")
    def query_current_plan_stage() -> dict[str, object]:
        return runtime.plan_stage.query_current()

    @app.get("/api/v1/bundle-stage/current")
    def query_current_bundle_stage() -> dict[str, object]:
        return runtime.bundle_stage.query_current()

    @app.get("/api/v1/reasoning-stage/current")
    def query_current_reasoning_stage() -> dict[str, object]:
        return runtime.reasoning_stage.query_current()

    @app.get("/api/v1/autonomous-creations/current")
    def query_current_autonomous_creation() -> dict[str, object] | None:
        return runtime.autonomous_creation.query_current()

    @app.get("/api/v1/quest-completions/current")
    def query_current_quest_completion() -> dict[str, object] | None:
        return runtime.quest_completion.query_current()

    @app.post("/api/v1/quest-completions", status_code=201)
    def start_quest_completion(
        request: Request,
        command: StartQuestCompletionRequest,
    ) -> dict[str, object]:
        return runtime.quest_completion.start(
            source_outcome_ref=command.source_outcome_ref,
            candidate_completion_ref=command.candidate_completion_ref,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/quest-completions/{context_ref}/decision"
    )
    def decide_quest_completion(
        request: Request,
        context_ref: str,
        command: QuestCompletionDecisionRequest,
    ) -> dict[str, object]:
        current = runtime.quest_completion.query(context_ref)
        if current is None:
            raise OwnerConflict("quest_completion_context_unavailable")
        human = current.get("human_confirmation")
        preview = human.get("preview") if isinstance(human, dict) else None
        if (
            current.get("status") in {"stale", "ended"}
            or not isinstance(preview, dict)
            or preview.get("status") != "current"
            or preview.get("ref") != command.preview_ref
            or preview.get("hash") != command.preview_hash
        ):
            raise OwnerConflict("quest_completion_preview_stale")
        runtime.owners.human_collaboration.decide_quest_completion(
            preview_ref=command.preview_ref,
            preview_hash=command.preview_hash,
            decision=command.decision,
            idempotency_key=_idempotency_key(request),
        )
        refreshed = runtime.quest_completion.query(context_ref)
        if refreshed is None:
            raise OwnerConflict("quest_completion_context_unavailable")
        return refreshed

    root_session_reader = None

    def _root_session_reader():
        nonlocal root_session_reader
        if root_session_reader is None:
            root_session_reader = RootSessionObservations(runtime)
        return root_session_reader

    def _root_session_http_error(error: ValueError) -> HTTPException:
        code = str(error)
        if not code.startswith("root_session_") or not code.replace("_", "").isalnum():
            code = "root_session_observation_unavailable"
        if code in {
            "root_session_not_found",
            "root_session_operation_not_found",
            "root_session_quest_not_found",
        }:
            status_code = 404
        elif "cursor" in code or code.endswith("_invalid"):
            status_code = 409
        else:
            status_code = 503
        return HTTPException(status_code=status_code, detail={"code": code})

    @app.get("/api/v1/quests/{quest_ref}/root-sessions")
    def query_root_sessions(quest_ref: str) -> dict[str, object]:
        try:
            return _root_session_reader().query(quest_ref)
        except ValueError as error:
            raise _root_session_http_error(error) from error

    @app.get("/api/v1/quests/{quest_ref}/root-sessions/{session_ref}/output")
    def query_root_session_output(
        quest_ref: str,
        session_ref: str,
        operation_ref: str = Query(min_length=1, max_length=256),
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=4, le=256 * 1024),
    ) -> dict[str, object]:
        try:
            return _root_session_reader().query_output(
                quest_ref, session_ref, operation_ref=operation_ref,
                after=after, limit=limit,
            )
        except ValueError as error:
            raise _root_session_http_error(error) from error

    @app.get(
        "/api/v1/bundle/targets/{target_ref}/root-observations"
    )
    def query_target_root_observations(
        target_ref: str,
        after: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=128, ge=1, le=256),
    ) -> dict[str, object]:
        page = runtime.harnesses.query_target_root_observations(
            target_ref,
            after_cursor=after,
            limit=limit,
        )
        return page.as_dict()

    @app.get("/api/v1/stage-runs/{run_ref}/root-observations")
    def query_stage_root_observations(
        run_ref: str,
        after: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=128, ge=1, le=256),
    ) -> dict[str, object]:
        try:
            page = runtime.query_stage_root_observations(
                run_ref,
                after_cursor=after,
                limit=limit,
            )
        except StageRootObservationError as error:
            status_code = (
                404
                if error.code == "stage_root_observation_run_not_found"
                else (
                    503
                    if error.code == "stage_root_observation_spool_unavailable"
                    else 409
                )
            )
            raise HTTPException(
                status_code=status_code,
                detail={"code": error.code},
            ) from error
        return page.as_dict()

    @app.get("/api/v1/stage-runs/{run_ref}/raw-output")
    def query_stage_raw_output(
        run_ref: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=4, le=256 * 1024),
        phase: Literal["primary", "review"] | None = None,
    ) -> dict[str, object]:
        try:
            page = runtime.query_stage_raw_output(
                run_ref,
                after=after,
                limit=limit,
                **({"phase": phase} if phase is not None else {}),
            )
        except StageRootObservationError as error:
            status_code = (
                404
                if error.code == "stage_root_observation_run_not_found"
                else (
                    409
                    if error.code
                    in {
                        "stage_raw_output_cursor_invalid",
                        "stage_raw_output_cursor_stale",
                    }
                    else 503
                )
            )
            raise HTTPException(
                status_code=status_code,
                detail={"code": error.code},
            ) from error
        return page.as_dict()

    experiment_logs = TargetExperimentLogs(
        runtime.target_run_authorities.agent_runtime,
        runtime.data_root.run / "target-workspaces",
    )

    @app.get("/api/v1/bundle/targets/{target_ref}/progress")
    def query_target_progress(
        target_ref: str,
        target_run_ref: str = Query(min_length=1, max_length=128),
        cursor: str | None = Query(default=None, max_length=32768),
    ) -> dict[str, object]:
        try:
            return runtime.target_run_authorities.agent_runtime.query_target_progress(
                target_ref, target_run_ref=target_run_ref, cursor=cursor)
        except ExperimentLogError as error:
            raise HTTPException(status_code=error.status, detail={"code": error.code}) from error

    @app.get("/api/v1/bundle/targets/{target_ref}/experiment-logs")
    def query_target_experiment_logs(
        target_ref: str,
        target_run_ref: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        try:
            return experiment_logs.list(target_ref, target_run_ref=target_run_ref)
        except ExperimentLogError as error:
            raise HTTPException(status_code=error.status, detail={"code": error.code}) from error

    @app.get("/api/v1/bundle/targets/{target_ref}/experiment-logs/{log_ref}")
    def query_target_experiment_log(
        target_ref: str,
        log_ref: str,
        target_run_ref: str | None = Query(default=None, min_length=1, max_length=128),
        after: int | None = Query(default=None, ge=0),
        before: int | None = Query(default=None, ge=0),
        stream_ref: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=64 * 1024, ge=4, le=256 * 1024),
    ) -> dict[str, object]:
        try:
            return experiment_logs.read(
                target_ref, log_ref, target_run_ref=target_run_ref,
                after=after, before=before, stream_ref=stream_ref, limit=limit,
            )
        except ExperimentLogError as error:
            raise HTTPException(status_code=error.status, detail={"code": error.code}) from error

    @app.get("/api/v1/bundle/targets/{target_ref}/raw-output")
    def query_target_raw_output(
        target_ref: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=4, le=256 * 1024),
    ) -> dict[str, object]:
        try:
            return runtime.harnesses.query_target_raw_output(
                target_ref,
                after=after,
                limit=limit,
            )
        except HarnessAdmissionError as error:
            raise HTTPException(
                status_code=503,
                detail={"code": error.code},
            ) from error

    @app.get("/api/v1/events")
    async def stream_events(request: Request) -> StreamingResponse:
        raw_revision = (
            request.headers.get("last-event-id")
            or request.query_params.get("after")
            or "0"
        )
        try:
            last_revision = int(raw_revision)
            if last_revision < 0:
                raise ValueError
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail={"code": "last_event_id_invalid"}
            ) from error
        return StreamingResponse(
            _event_stream(
                runtime,
                request,
                last_revision,
                worker_health_updates,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/")
    def web_shell() -> FileResponse:
        index = web_root / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503, detail={"code": "web_shell_unavailable"}
            )
        return FileResponse(index)

    @app.get("/assets/{asset_path:path}")
    def web_asset(asset_path: str) -> FileResponse:
        asset_root = (web_root / "assets").resolve()
        candidate = (asset_root / asset_path).resolve()
        if not candidate.is_relative_to(asset_root) or not candidate.is_file():
            raise HTTPException(status_code=404, detail={"code": "asset_not_found"})
        return FileResponse(candidate)

    return app


async def _chat_reply_stream(
    runtime: ProductionRuntime,
    request: Request,
    query: Callable[[], dict[str, object]],
    initial: dict[str, object],
) -> AsyncIterator[str]:
    """Replay the current reply on reconnect, then send real changed snapshots."""
    current = initial
    previous: str | None = None
    last_keepalive = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        if not await _sse_session_is_valid(runtime, request):
            return
        payload = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        if payload != previous:
            yield f"event: message\ndata: {payload}\n\n"
            previous = payload
            last_keepalive = time.monotonic()
        elif time.monotonic() - last_keepalive >= 10:
            yield ": keep-alive\n\n"
            last_keepalive = time.monotonic()
        if current["status"] not in {"queued", "processing", "running"}:
            return
        await asyncio.sleep(0.25)
        if not await _sse_session_is_valid(runtime, request):
            return
        try:
            current = await asyncio.to_thread(query)
        except OwnerConflict as error:
            current = {
                "turn_ref": initial["turn_ref"],
                "status": "failed",
                "text": "",
                "reason": {"code": error.code},
            }


async def _event_stream(
    runtime: ProductionRuntime,
    request: Request,
    last_revision: int,
    worker_health_updates: WorkerHealthUpdates | None = None,
) -> AsyncIterator[str]:
    cursor = last_revision
    first_page = True
    health_revision = (
        worker_health_updates.revision if worker_health_updates is not None else 0
    )
    if worker_health_updates is not None and health_revision > 0:
        if not await _sse_session_is_valid(runtime, request):
            return
        payload = json.dumps(
            {
                "reason": "worker_health_changed",
                "snapshot_url": "/api/v1/snapshot",
                "snapshot_revision": last_revision,
            },
            separators=(",", ":"),
        )
        yield (f"event: snapshot.required\ndata: {payload}\n\n")
    while True:
        if not await _sse_session_is_valid(runtime, request):
            return
        page = await asyncio.to_thread(runtime.feed.read_after, cursor)
        if not await _sse_session_is_valid(runtime, request):
            return
        if page.revision_gap:
            payload = json.dumps(
                {
                    "reason": "revision_gap",
                    "snapshot_url": "/api/v1/snapshot",
                    "snapshot_revision": page.current_revision,
                },
                separators=(",", ":"),
            )
            yield (
                f"id: {page.current_revision}\n"
                "event: snapshot.required\n"
                f"data: {payload}\n\n"
            )
            return
        for event in page.events:
            if not await _sse_session_is_valid(runtime, request):
                return
            payload = json.dumps(event.payload, separators=(",", ":"))
            yield (f"event: {event.event_type}\ndata: {payload}\n\n")
            if not await _sse_session_is_valid(runtime, request):
                return
            projection_payload = json.dumps(
                {"revision": event.revision, "event_type": event.event_type},
                separators=(",", ":"),
            )
            yield (
                f"id: {event.revision}\n"
                "event: projection.updated\n"
                f"data: {projection_payload}\n\n"
            )
            cursor = event.revision
        if first_page and page.events:
            first_page = False
            continue
        first_page = False
        if await request.is_disconnected():
            return
        if not await _sse_session_is_valid(runtime, request):
            return
        if worker_health_updates is None:
            yield ": keep-alive\n\n"
            await asyncio.sleep(1)
            continue
        next_health_revision = await worker_health_updates.wait_after(
            health_revision, timeout=1.0
        )
        if not await _sse_session_is_valid(runtime, request):
            return
        if next_health_revision is None:
            yield ": keep-alive\n\n"
            continue
        health_revision = next_health_revision
        payload = json.dumps(
            {
                "reason": "worker_health_changed",
                "snapshot_url": "/api/v1/snapshot",
                "snapshot_revision": page.current_revision,
            },
            separators=(",", ":"),
        )
        yield (f"event: snapshot.required\ndata: {payload}\n\n")


async def _sse_session_is_valid(runtime: ProductionRuntime, request: Request) -> bool:
    session_token = getattr(request.state, "session_token", None)
    return await asyncio.to_thread(
        runtime.authentication.session_is_valid, session_token
    )


def _set_session_cookie(response, session: AuthSession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=12 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        session.csrf_token,
        max_age=12 * 60 * 60,
        httponly=False,
        samesite="strict",
        secure=False,
        path="/",
    )


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if (
        not 1 <= len(value) <= 128
        or any(character.isspace() for character in value)
        or contains_secret(value)
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "idempotency_key_invalid"},
        )
    return value


async def _reconcile_quest_initializations(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.owners.human_collaboration.reconcile_once,
        health=health,
        worker_label='quest reconciliation',
        timeout_code='quest_reconciliation_io_timeout',
        timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        idle_delay=1.0,
    )


async def _process_quest_drafting(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.owners.human_collaboration.process_drafting_once,
        health=health,
        worker_label='quest drafting',
        timeout_code='quest_drafting_operation_timeout',
        timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
    )


async def _process_first_question_deepfetch(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.deepfetch.process_once,
        health=health,
        worker_label='first-question DeepFetch',
        timeout_code='first_question_deepfetch_operation_timeout',
        timeout_seconds=None,
        on_health_change=on_health_change,
    )


async def _process_writing(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance autonomous Writing independently of any browser connection."""

    pending_deliveries: dict[str, _PendingWorkerOperation] = {}
    delivery_sweep_exclusions: set[str] = set()
    delivery_sweep_error_code: str | None = None
    while True:
        delivery_sweep_restarted = False
        try:
            claim = await _daemon_thread_call(runtime.writing.next_runnable_claim)
            if claim is None:
                advanced = False
            else:
                run_ref, attempt_ref, fence_ref = claim
                advanced = await _await_monitored_worker_call(
                    lambda: runtime.writing.process_once(
                        expected_run_ref=run_ref,
                        expected_attempt_ref=attempt_ref,
                        expected_fence_ref=fence_ref,
                    ),
                    health=health,
                    timeout_code="writing_operation_timeout",
                    on_health_change=on_health_change,
                    timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
                )
            delivery_advanced = False
            for operation_ref, pending_delivery in tuple(
                pending_deliveries.items()
            ):
                if not pending_delivery.operation.done():
                    continue
                pending_deliveries.pop(operation_ref)
                pending_advanced = pending_delivery.operation.result()
                delivery_advanced = bool(pending_advanced or delivery_advanced)
                if not pending_advanced:
                    delivery_sweep_exclusions.add(operation_ref)
            next_delivery_ref = await _daemon_thread_call(
                lambda: runtime.writing.next_runnable_delivery_operation_ref(
                    excluded_operation_refs=frozenset(
                        pending_deliveries.keys() | delivery_sweep_exclusions
                    )
                )
            )
            if next_delivery_ref is None:
                # Every exact operation gets at most one no-progress attempt per
                # sweep. Clearing only after the scan is exhausted preserves
                # fairness while the normal idle delay bounds the next retry.
                delivery_sweep_restarted = bool(delivery_sweep_exclusions)
                delivery_sweep_exclusions.clear()
            else:
                try:
                    delivery_outcome = await _await_monitored_worker_call(
                        lambda: runtime.writing.process_delivery_once(
                            expected_operation_ref=next_delivery_ref
                        ),
                        health=health,
                        timeout_code="writing_delivery_operation_timeout",
                        on_health_change=on_health_change,
                        retain_operation_on_timeout=True,
                        timeout_seconds=WRITING_DELIVERY_STALL_SECONDS,
                    )
                except Exception as error:
                    # Only a failure from this exact selected operation belongs
                    # to its sweep cursor. Selector/query/global failures happen
                    # outside this catch and cannot exclude an unrelated ref.
                    delivery_sweep_exclusions.add(next_delivery_ref)
                    delivery_sweep_error_code = (
                        error.code
                        if isinstance(error, OwnerConflict)
                        else str(getattr(error, "code", type(error).__name__))
                    )
                    raise
                if isinstance(delivery_outcome, _PendingWorkerOperation):
                    # A stable provider operation cannot be retired or safely
                    # duplicated. Quarantine its exact ref while later internal
                    # claims and independently confirmed deliveries advance.
                    pending_deliveries[next_delivery_ref] = delivery_outcome
                else:
                    delivery_advanced = bool(
                        delivery_outcome or delivery_advanced
                    )
                    if not delivery_outcome:
                        delivery_sweep_exclusions.add(next_delivery_ref)
            advanced = bool(advanced or delivery_advanced)
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("writing worker attempt failed unexpectedly")
            error_code = (
                error.code
                if isinstance(error, OwnerConflict)
                else str(getattr(error, "code", type(error).__name__))
            )
            changed = (
                health.status != "unavailable" or health.last_error != error_code
            )
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            if pending_deliveries:
                changed = (
                    health.status != "unavailable"
                    or health.last_error
                    != "writing_delivery_operation_timeout"
                )
                health.status = "unavailable"
                health.last_error = "writing_delivery_operation_timeout"
            elif delivery_sweep_error_code is not None:
                changed = (
                    health.status != "unavailable"
                    or health.last_error != delivery_sweep_error_code
                )
                health.status = "unavailable"
                health.last_error = delivery_sweep_error_code
            else:
                changed = (
                    health.status != "ready" or health.last_error is not None
                )
                health.status = "ready"
                health.last_error = None
                health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(
                0 if advanced and not delivery_sweep_restarted else 0.2
            )
            if delivery_sweep_restarted:
                delivery_sweep_error_code = None


async def _process_research_assets(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.owners.research_memory.process_asset_intake_once,
        health=health,
        worker_label='research asset intake',
        timeout_code='asset_intake_io_timeout',
        timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        active_delay=0.05,
    )


async def _verify_research_assets(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    last_cleanup = 0.0

    def verify_and_reclaim() -> bool:
        nonlocal last_cleanup
        worked = runtime.owners.research_memory.verify_asset_inventory_once()
        now = time.monotonic()
        if now - last_cleanup >= 60:
            last_cleanup = now
            try:
                report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, limit=10)
                for item in report:
                    if item.get("action") == "removed":
                        LOGGER.info("Completed research workspace reclaimed: %s", item)
            except (OSError, OwnerConflict, ValueError):
                LOGGER.exception("Completed workspace cleanup deferred")
        return worked

    await _process_background_operation(
        verify_and_reclaim,
        health=health,
        worker_label='research asset verification',
        timeout_code='asset_verification_io_timeout',
        timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        active_delay=0.05,
    )


async def _await_monitored_worker_call(
    call: Callable[[], bool],
    *,
    health: ReconciliationHealth,
    timeout_code: str,
    on_health_change: Callable[[], None] | None,
    retain_operation_on_timeout: bool = False,
    timeout_seconds: float | None,
) -> bool | _PendingWorkerOperation:
    """Keep worker stalls outside the event loop and expose a watchdog.

    Python cannot safely cancel a thread blocked inside an arbitrary FUSE/NFS
    syscall. A dedicated daemon thread therefore owns exactly one operation;
    the coroutine publishes a typed blocker after a bounded interval without
    spawning duplicate attempts. If the mount recovers, the same durable job
    resumes. A stuck daemon thread cannot hold process shutdown open.
    """

    operation = _daemon_thread_call(call)
    done, _pending = await asyncio.wait(
        {operation},
        timeout=timeout_seconds,
    )
    if done:
        return operation.result()
    changed = (
        health.status != "unavailable"
        or health.last_error != timeout_code
    )
    health.status = "unavailable"
    health.last_error = timeout_code
    health.retry_count += 1
    if changed and on_health_change is not None:
        on_health_change()
    if retain_operation_on_timeout:
        return _PendingWorkerOperation(operation)
    # Existing workers without a durable timeout/Fence seam must not be
    # duplicated. Keep exposing the watchdog state until their one operation
    # returns, then let the caller restore ready health.
    return await operation


async def _await_bounded_asset_io(
    call: Callable[[], _T],
    *,
    slots: asyncio.Semaphore,
    timeout_code: str,
) -> _T:
    """Run a public deep-I/O operation without occupying AnyIO's thread pool."""

    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.05)
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "asset_io_busy"},
        ) from error
    try:
        operation = _daemon_thread_call(call)
    except BaseException:
        slots.release()
        raise

    def release_slot(_completed: asyncio.Future[_T]) -> None:
        slots.release()

    operation.add_done_callback(release_slot)
    return await _await_asset_io_operation(operation, timeout_code=timeout_code)


class _AssetIOSingleFlight:
    """Keep retries of one serialized Owner operation off the shared I/O slots."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._key: tuple[str, str] | None = None
        self._operation: asyncio.Future[object] | None = None

    async def run(
        self,
        key: tuple[str, str],
        call: Callable[[], _T],
        *,
        slots: asyncio.Semaphore,
        timeout_code: str,
    ) -> _T:
        async with self._lock:
            operation = self._operation
            if operation is not None and not operation.done():
                if self._key != key:
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "asset_custody_busy"},
                    )
            else:
                try:
                    await asyncio.wait_for(slots.acquire(), timeout=0.05)
                except TimeoutError as error:
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "asset_io_busy"},
                    ) from error
                try:
                    operation = _daemon_thread_call(call)
                except BaseException:
                    slots.release()
                    raise
                self._key = key
                self._operation = operation

                def finish(completed: asyncio.Future[object]) -> None:
                    slots.release()
                    if self._operation is completed:
                        self._key = None
                        self._operation = None

                operation.add_done_callback(finish)
        return await _await_asset_io_operation(
            operation,
            timeout_code=timeout_code,
        )


async def _await_asset_io_operation(
    operation: asyncio.Future[_T], *, timeout_code: str
) -> _T:
    done, _pending = await asyncio.wait(
        {operation},
        timeout=ASSET_ROUTE_WATCHDOG_SECONDS,
    )
    if done:
        return operation.result()
    raise HTTPException(
        status_code=503,
        detail={"code": timeout_code},
    )


def _daemon_thread_call(call: Callable[[], _T]) -> asyncio.Future[_T]:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[_T] = loop.create_future()

    def consume_late_error(completed: asyncio.Future[_T]) -> None:
        if not completed.cancelled():
            completed.exception()

    result.add_done_callback(consume_late_error)

    def deliver_value(value: _T) -> None:
        if not result.done():
            result.set_result(value)

    def deliver_error(error: BaseException) -> None:
        if not result.done():
            result.set_exception(error)

    def run() -> None:
        try:
            value = call()
        except BaseException as error:
            try:
                loop.call_soon_threadsafe(deliver_error, error)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(deliver_value, value)
            except RuntimeError:
                pass

    threading.Thread(
        target=run,
        name="meta-research-worker-operation",
        daemon=True,
    ).start()
    return result


async def _run_after_background_worker_startup_grace(
    worker_factory: Callable[[], Awaitable[None]],
) -> None:
    """Let the public server accept work before daemon polling begins."""

    await asyncio.sleep(BACKGROUND_WORKER_STARTUP_GRACE_SECONDS)
    await worker_factory()


def _start_background_worker(
    worker_factory: Callable[[], Awaitable[None]],
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _run_after_background_worker_startup_grace(worker_factory)
    )
    task.add_done_callback(_log_reconciliation_exit)
    return task


async def _process_harness_conformance(
    runtime: ProductionRuntime, base_url: str
) -> None:
    """Advance only explicitly admitted full-contract Harness Runs."""

    health = ReconciliationHealth()
    while True:
        try:
            advanced = await _await_monitored_worker_call(
                lambda: runtime.harnesses.advance_full_conformance(
                    mcp_base_url=base_url
                ),
                health=health,
                timeout_code="harness_conformance_operation_timeout",
                on_health_change=None,
                timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
            )
        except HarnessAdmissionError as error:
            LOGGER.warning("Harness conformance turn unavailable: %s", error.code)
            await asyncio.sleep(0.2)
        except Exception:
            LOGGER.exception("Harness conformance turn failed unexpectedly")
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_idea_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.idea_stage.process_once,
        health=health,
        worker_label='idea stage',
        timeout_code='idea_stage_operation_timeout',
        timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        transient_error=lambda: runtime.idea_stage.transient_error,
    )


async def _process_plan_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.plan_stage.process_once,
        health=health,
        worker_label='plan stage',
        timeout_code='plan_stage_operation_timeout',
        timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        transient_error=lambda: runtime.plan_stage.transient_error,
    )


async def _process_target_runs(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Fairly wake each admitted or running Target root, independent of Bundle."""

    flights: dict[str, _TargetRunFlight] = {}
    cancel_flights: dict[str, _TargetRunFlight] = {}
    operation_errors: dict[str, str] = {}
    idle_sweeps = 0

    def set_health(status: Literal["ready", "unavailable"], code: str | None) -> None:
        changed = health.status != status or health.last_error != code
        health.status = status
        health.last_error = code
        if status == "ready":
            health.retry_count = 0
        elif changed:
            health.retry_count += 1
        if changed and on_health_change is not None:
            on_health_change()

    while True:
        try:
            target_refs = await asyncio.to_thread(
                runtime.owners.agent_runtime.list_target_root_work_refs
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("TargetRun frontier discovery failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            set_health("unavailable", error_code)
            await asyncio.sleep(min(2.0, 0.2 * (2 ** min(health.retry_count, 4))))
            continue

        # An in-flight retry, or another Target's success, does not prove a
        # failed boundary recovered. Retire its error only after that Target
        # returns a valid result or leaves the authoritative work inventory.
        for target_ref in operation_errors.keys() - set(target_refs):
            del operation_errors[target_ref]

        discovery_error: str | None = None
        for target_ref in target_refs:
            if target_ref in flights:
                if target_ref in cancel_flights:
                    continue
                has_pending_cancel = getattr(
                    runtime.target_run_runtime,
                    "has_pending_cancel",
                    None,
                )
                if not callable(has_pending_cancel):
                    continue
                try:
                    pending_cancel = await asyncio.to_thread(
                        has_pending_cancel,
                        target_ref,
                    )
                except Exception as error:
                    if not isinstance(
                        error, (OSError, OwnerConflict, SQLAlchemyError)
                    ):
                        LOGGER.exception(
                            "TargetRun cancel discovery failed unexpectedly"
                        )
                    discovery_error = (
                        error.code
                        if isinstance(error, OwnerConflict)
                        else type(error).__name__
                    )
                    continue
                if pending_cancel:
                    operation = _daemon_thread_call(
                        lambda target_ref=target_ref: (
                            runtime.target_run_runtime.process_once(target_ref)
                        )
                    )
                    cancel_flights[target_ref] = _TargetRunFlight(
                        target_ref=target_ref,
                        operation=operation,
                    )
                continue
            if target_ref in cancel_flights:
                continue
            operation = _daemon_thread_call(
                lambda target_ref=target_ref: runtime.target_run_runtime.process_once(
                    target_ref
                )
            )
            flights[target_ref] = _TargetRunFlight(
                target_ref=target_ref,
                operation=operation,
            )

        all_flights = (*flights.values(), *cancel_flights.values())
        completed = {
            flight.operation for flight in all_flights if flight.operation.done()
        }
        if not completed and all_flights:
            completed, _pending = await asyncio.wait(
                {flight.operation for flight in all_flights},
                timeout=0.2,
                return_when=asyncio.FIRST_COMPLETED,
            )

        advanced = False
        for flight_map in (flights, cancel_flights):
            for target_ref, flight in tuple(flight_map.items()):
                if (
                    flight.operation not in completed
                    and not flight.operation.done()
                ):
                    continue
                del flight_map[target_ref]
                try:
                    result = flight.operation.result()
                    if type(result) is not bool:
                        raise TypeError(
                            "TargetRun process_once returned a non-bool value"
                        )
                    advanced = advanced or result
                    operation_errors.pop(target_ref, None)
                except Exception as error:
                    if not isinstance(
                        error, (OSError, OwnerConflict, SQLAlchemyError)
                    ):
                        LOGGER.exception("TargetRun boundary failed unexpectedly")
                    operation_errors[target_ref] = (
                        error.code
                        if isinstance(error, OwnerConflict)
                        else type(error).__name__
                    )

        operation_error = next(iter(operation_errors.values()), None)
        if discovery_error is not None or operation_error is not None:
            set_health("unavailable", discovery_error or operation_error)
        else:
            set_health("ready", None)

        if advanced:
            idle_sweeps = 0
            await asyncio.sleep(0)
        elif flights or cancel_flights:
            # A boundary is still executing; keep collecting promptly.
            idle_sweeps = 0
            await asyncio.sleep(0.05)
        else:
            # Every idle sweep re-runs each Target root's full read
            # verification; polling that at the base rate pins a core
            # without research progress. Back off while the inventory
            # stays quiescent and snap back on the first advance.
            idle_sweeps = min(idle_sweeps + 1, 7)
            await asyncio.sleep(min(0.2 * (2 ** (idle_sweeps - 1)), 30.0))


async def _process_bundle_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.bundle_stage.process_once,
        health=health,
        worker_label='bundle stage',
        timeout_code='bundle_stage_operation_timeout',
        timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        transient_error=lambda: (
            None
            if runtime.bundle_stage.transient_error in _BUNDLE_STAGE_HEALTHY_WAIT_CODES
            else runtime.bundle_stage.transient_error
        ),
    )


async def _process_reasoning_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.reasoning_stage.process_once,
        health=health,
        worker_label='reasoning stage',
        timeout_code='reasoning_stage_operation_timeout',
        timeout_seconds=PROVIDER_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
        transient_error=lambda: runtime.reasoning_stage.transient_error,
    )


async def _process_autonomous_creation(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.autonomous_creation.process_once,
        health=health,
        worker_label='autonomous creation',
        timeout_code='autonomous_creation_operation_timeout',
        timeout_seconds=REASONING_FOLLOWUP_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
    )


async def _process_quest_completion(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    await _process_background_operation(
        runtime.quest_completion.process_once,
        health=health,
        worker_label='quest completion',
        timeout_code='quest_completion_operation_timeout',
        timeout_seconds=REASONING_FOLLOWUP_WORKER_WATCHDOG_SECONDS,
        on_health_change=on_health_change,
    )


async def _process_background_operation(
    operation: Callable[[], bool],
    *,
    health: ReconciliationHealth,
    worker_label: str,
    timeout_code: str,
    timeout_seconds: float | None,
    on_health_change: Callable[[], None] | None,
    idle_delay: float = 0.2,
    active_delay: float = 0,
    transient_error: Callable[[], str | None] | None = None,
) -> None:
    """Share retry and health reporting while each worker owns its operation.

    The watchdog retains a stalled operation instead of launching duplicates.
    Stage-specific waiting semantics stay with the caller.
    """

    consecutive_idle_passes = 0
    while True:
        try:
            advanced = await _await_monitored_worker_call(
                operation,
                health=health,
                timeout_code=timeout_code,
                on_health_change=on_health_change,
                timeout_seconds=timeout_seconds,
            )
            error_code = transient_error() if transient_error is not None else None
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("%s attempt failed unexpectedly", worker_label)
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )

        if error_code is not None:
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if advanced:
                consecutive_idle_passes = 0
                delay = active_delay
            else:
                # An idle pass re-reads and re-verifies durable state;
                # polling it at the base rate keeps the daemon pinned at
                # full CPU with no research progress. Back off between
                # idle passes and snap back the moment work appears.
                consecutive_idle_passes = min(consecutive_idle_passes + 1, 6)
                delay = min(
                    idle_delay * (2 ** (consecutive_idle_passes - 1)), 30.0
                )
        if changed and on_health_change is not None:
            on_health_change()
        await asyncio.sleep(delay)


def _log_reconciliation_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        LOGGER.exception(
            "quest reconciliation worker exited unexpectedly",
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})


def _history_page(
    rows,
    *,
    limit: int,
    timestamp_field: str,
    ref_field: str,
) -> dict[str, object]:
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_history_cursor(
            float(getattr(last, timestamp_field)),
            str(getattr(last, ref_field)),
        )
    return {
        "items": [item.as_public_dict() for item in page],
        "limit": limit,
        "has_more": len(rows) > limit,
        "next_cursor": next_cursor,
    }


def _encode_history_cursor(timestamp: float, ref: str) -> str:
    payload = json.dumps(
        {"timestamp": timestamp, "ref": ref},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str | None) -> tuple[float | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        document = json.loads(
            base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        )
        timestamp = document["timestamp"]
        ref = document["ref"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
        raise OwnerConflict("asset_history_cursor_invalid") from error
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or not isinstance(ref, str)
        or not ref
        or len(ref) > 128
        or set(document) != {"timestamp", "ref"}
    ):
        raise OwnerConflict("asset_history_cursor_invalid")
    return float(timestamp), ref
