from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator, Callable, Literal, TypeVar
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from meta_research.auth import AuthSession
from meta_research.composition import ProductionRuntime
from meta_research.experiment import ExperimentIntent
from meta_research.harness import (
    FullConformanceRequest,
    HarnessAdmissionError,
)
from meta_research.owners.common import OwnerConflict
from meta_research.owners.secret_detection import contains_secret
from meta_research.owners.research_graph import ASSET_ROLE_QUERY_MAX_PAGE_SIZE
from meta_research.owners.research_memory import (
    ASSET_HISTORY_QUERY_MAX_PAGE_SIZE,
    ASSET_PROJECTION_HISTORY_PER_VERSION,
    AssetIntakeRequest,
)
from meta_research.projection import SnapshotConsistencyUnavailable
from meta_research.quest_drafting import (
    INTENT_MESSAGE_MAX_LENGTH,
    QUESTION_FIELD_MAX_LENGTHS,
)
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
DRAFTING_WORKER_WATCHDOG_SECONDS = 190.0
IDEA_STAGE_WORKER_WATCHDOG_SECONDS = 910.0
PLAN_STAGE_WORKER_WATCHDOG_SECONDS = 910.0
BUNDLE_STAGE_WORKER_WATCHDOG_SECONDS = 910.0
DEEPFETCH_WORKER_WATCHDOG_SECONDS = 1810.0
EXPERIMENT_WORKER_WATCHDOG_SECONDS = 30.0
WRITING_WORKER_WATCHDOG_SECONDS = 910.0
HARNESS_CONFORMANCE_WORKER_WATCHDOG_SECONDS = 310.0
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
class _PendingWorkerRetirement:
    operation: asyncio.Future[bool]
    retirement: asyncio.Future[None]


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

    codex_model_ref: str = Field(min_length=1, max_length=160)
    codex_auth_profile_ref: str = Field(min_length=1, max_length=160)
    claude_model_ref: str = Field(min_length=1, max_length=160)
    claude_auth_profile_ref: str = Field(min_length=1, max_length=160)


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


class CompanionMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=INTENT_MESSAGE_MAX_LENGTH)


class HumanRequestResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["provided", "declined", "deferred"]
    facts: dict[str, object] = Field(default_factory=dict)
    note: str = Field(default="", max_length=4000)


class AgentProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str = Field(min_length=1, max_length=128)
    proposal: dict[str, object]


class AgentProposalConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_scope_ref: str = Field(min_length=1, max_length=128)
    expected_proposal_hash: str = Field(min_length=64, max_length=64)


class SoftConstraintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_ref: str = Field(min_length=1, max_length=128)
    guidance: dict[str, object]


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


class StartExperimentRequest(BaseModel):
    """Web intent for one stable, Owner-authorized experiment request."""

    model_config = ConfigDict(extra="forbid")

    execution_request_ref: str = Field(min_length=1, max_length=96)
    quest_ref: str = Field(min_length=1, max_length=96)
    title: str = Field(min_length=1, max_length=512)
    hypothesis: str = Field(min_length=1, max_length=4000)
    variant_parameter: float = Field(allow_inf_nan=False)
    sample_count: int = Field(ge=4, le=4096)
    request_kind: Literal["retrain", "remeasure"] = "retrain"
    source_variant_run_ref: str | None = Field(default=None, max_length=96)
    selected_checkpoint_role_refs: list[str] = Field(
        default_factory=list,
        max_length=32,
    )

    def as_intent(self) -> ExperimentIntent:
        value = self.model_dump()
        value["selected_checkpoint_role_refs"] = tuple(
            self.selected_checkpoint_role_refs
        )
        return ExperimentIntent(**value)


class WritingIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def create_app(
    runtime: ProductionRuntime, *, base_url: str, control_key: str
) -> FastAPI:
    runtime.bundle_stage.configure_resident_mcp_endpoint(base_url)
    runtime.target_run_runtime.configure_resident_mcp_endpoint(base_url)
    harness_conformance_task: asyncio.Task[None] | None = None
    reconciliation_task: asyncio.Task[None] | None = None
    drafting_task: asyncio.Task[None] | None = None
    deepfetch_task: asyncio.Task[None] | None = None
    idea_stage_task: asyncio.Task[None] | None = None
    plan_stage_task: asyncio.Task[None] | None = None
    bundle_stage_task: asyncio.Task[None] | None = None
    target_run_task: asyncio.Task[None] | None = None
    experiment_task: asyncio.Task[None] | None = None
    writing_task: asyncio.Task[None] | None = None
    research_asset_task: asyncio.Task[None] | None = None
    research_asset_verification_task: asyncio.Task[None] | None = None
    reconciliation_health = ReconciliationHealth()
    drafting_health = ReconciliationHealth()
    deepfetch_health = ReconciliationHealth()
    idea_stage_health = ReconciliationHealth()
    plan_stage_health = ReconciliationHealth()
    bundle_stage_health = ReconciliationHealth()
    target_run_health = ReconciliationHealth()
    experiment_health = ReconciliationHealth()
    writing_health = ReconciliationHealth()
    research_asset_health = ReconciliationHealth()
    research_asset_verification_health = ReconciliationHealth()
    worker_health_updates = WorkerHealthUpdates()
    asset_intake_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_INTAKE_REQUESTS)
    asset_io_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_IO_OPERATIONS)
    asset_intake_recovery_slots = asyncio.Semaphore(1)
    asset_handoff_singleflight = _AssetIOSingleFlight()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal harness_conformance_task
        nonlocal reconciliation_task, drafting_task, deepfetch_task, idea_stage_task
        nonlocal plan_stage_task, bundle_stage_task, target_run_task
        nonlocal experiment_task, writing_task
        nonlocal research_asset_task, research_asset_verification_task
        harness_conformance_task = asyncio.create_task(
            _process_harness_conformance(runtime, base_url)
        )
        harness_conformance_task.add_done_callback(_log_reconciliation_exit)
        reconciliation_task = asyncio.create_task(
            _reconcile_quest_initializations(
                runtime,
                reconciliation_health,
                worker_health_updates.publish,
            )
        )
        reconciliation_task.add_done_callback(_log_reconciliation_exit)
        drafting_task = asyncio.create_task(
            _process_quest_drafting(
                runtime,
                drafting_health,
                worker_health_updates.publish,
            )
        )
        drafting_task.add_done_callback(_log_reconciliation_exit)
        deepfetch_task = asyncio.create_task(
            _process_first_question_deepfetch(
                runtime,
                deepfetch_health,
                worker_health_updates.publish,
            )
        )
        deepfetch_task.add_done_callback(_log_reconciliation_exit)
        idea_stage_task = asyncio.create_task(
            _process_idea_stage(
                runtime,
                idea_stage_health,
                worker_health_updates.publish,
            )
        )
        idea_stage_task.add_done_callback(_log_reconciliation_exit)
        plan_stage_task = asyncio.create_task(
            _process_plan_stage(
                runtime,
                plan_stage_health,
                worker_health_updates.publish,
            )
        )
        plan_stage_task.add_done_callback(_log_reconciliation_exit)
        bundle_stage_task = asyncio.create_task(
            _process_bundle_stage(
                runtime,
                bundle_stage_health,
                worker_health_updates.publish,
            )
        )
        bundle_stage_task.add_done_callback(_log_reconciliation_exit)
        target_run_task = asyncio.create_task(
            _process_target_runs(
                runtime,
                target_run_health,
                worker_health_updates.publish,
            )
        )
        target_run_task.add_done_callback(_log_reconciliation_exit)
        experiment_task = asyncio.create_task(
            _process_experiments(
                runtime,
                experiment_health,
                worker_health_updates.publish,
            )
        )
        experiment_task.add_done_callback(_log_reconciliation_exit)
        writing_task = asyncio.create_task(
            _process_writing(
                runtime,
                writing_health,
                worker_health_updates.publish,
            )
        )
        writing_task.add_done_callback(_log_reconciliation_exit)
        research_asset_task = asyncio.create_task(
            _process_research_assets(
                runtime,
                research_asset_health,
                worker_health_updates.publish,
            )
        )
        research_asset_task.add_done_callback(_log_reconciliation_exit)
        research_asset_verification_task = asyncio.create_task(
            _verify_research_assets(
                runtime,
                research_asset_verification_health,
                worker_health_updates.publish,
            )
        )
        research_asset_verification_task.add_done_callback(_log_reconciliation_exit)
        try:
            yield
        finally:
            tasks = tuple(
                task
                for task in (
                    harness_conformance_task,
                    reconciliation_task,
                    drafting_task,
                    deepfetch_task,
                    idea_stage_task,
                    plan_stage_task,
                    bundle_stage_task,
                    target_run_task,
                    experiment_task,
                    writing_task,
                    research_asset_task,
                    research_asset_verification_task,
                )
                if task is not None
            )
            try:
                await asyncio.to_thread(runtime.request_stop)
            except Exception:
                LOGGER.exception("provider shutdown request failed")
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

    def worker_check(
        name: str,
        task: asyncio.Task[None] | None,
        health: ReconciliationHealth,
    ) -> dict[str, object]:
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

    def public_snapshot() -> dict[str, object]:
        snapshot = runtime.projection.query_snapshot()
        readiness = snapshot["readiness"]
        checks = [
            *readiness["checks"],
            runtime.query_target_root_readiness(),
            worker_check(
                "quest_reconciliation_worker",
                reconciliation_task,
                reconciliation_health,
            ),
            worker_check("quest_drafting_worker", drafting_task, drafting_health),
            worker_check(
                "first_question_deepfetch_worker",
                deepfetch_task,
                deepfetch_health,
            ),
            worker_check("idea_stage_worker", idea_stage_task, idea_stage_health),
            worker_check("plan_stage_worker", plan_stage_task, plan_stage_health),
            worker_check("bundle_stage_worker", bundle_stage_task, bundle_stage_health),
            worker_check("target_run_worker", target_run_task, target_run_health),
            worker_check(
                "experiment_worker",
                experiment_task,
                experiment_health,
            ),
            worker_check("writing_worker", writing_task, writing_health),
            worker_check(
                "research_asset_intake_worker",
                research_asset_task,
                research_asset_health,
            ),
            worker_check(
                "research_asset_verification_worker",
                research_asset_verification_task,
                research_asset_verification_health,
            ),
        ]
        core_checks = [
            check
            for check in checks
            if check["name"]
            not in {
                "idea_stage_worker",
                "plan_stage_worker",
                "bundle_stage_worker",
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
            if not await asyncio.to_thread(
                runtime.authentication.session_is_valid, session_token
            ):
                return _error(401, "authentication_required")
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
        if unsafe_api_route or path == "/auth/logout":
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
                "experiment_not_found",
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
    def internal_readiness() -> dict[str, object]:
        snapshot = public_snapshot()
        reconciliation = worker_check(
            "quest_reconciliation_worker",
            reconciliation_task,
            reconciliation_health,
        )
        drafting = worker_check("quest_drafting_worker", drafting_task, drafting_health)
        deepfetch = worker_check(
            "first_question_deepfetch_worker", deepfetch_task, deepfetch_health
        )
        idea_stage = worker_check(
            "idea_stage_worker", idea_stage_task, idea_stage_health
        )
        plan_stage = worker_check(
            "plan_stage_worker", plan_stage_task, plan_stage_health
        )
        bundle_stage = worker_check(
            "bundle_stage_worker", bundle_stage_task, bundle_stage_health
        )
        target_runs = worker_check(
            "target_run_worker", target_run_task, target_run_health
        )
        research_assets = worker_check(
            "research_asset_intake_worker",
            research_asset_task,
            research_asset_health,
        )
        research_asset_verification = worker_check(
            "research_asset_verification_worker",
            research_asset_verification_task,
            research_asset_verification_health,
        )
        experiment = worker_check(
            "experiment_worker", experiment_task, experiment_health
        )
        writing = worker_check("writing_worker", writing_task, writing_health)
        target_root = runtime.query_target_root_readiness()
        return {
            "status": snapshot["readiness"]["status"],
            "revision": snapshot["revision"],
            "reconciliation": {
                "status": reconciliation["status"],
                "last_error": reconciliation_health.last_error,
            },
            "drafting": {
                "status": drafting["status"],
                "last_error": drafting_health.last_error,
            },
            "deepfetch": {
                "status": deepfetch["status"],
                "last_error": deepfetch_health.last_error,
            },
            "idea_stage": {
                "status": idea_stage["status"],
                "last_error": idea_stage_health.last_error,
            },
            "plan_stage": {
                "status": plan_stage["status"],
                "last_error": plan_stage_health.last_error,
            },
            "bundle_stage": {
                "status": bundle_stage["status"],
                "last_error": bundle_stage_health.last_error,
            },
            "target_runs": {
                "status": target_runs["status"],
                "last_error": target_run_health.last_error,
            },
            "experiment": {
                "status": experiment["status"],
                "last_error": experiment_health.last_error,
            },
            "writing": {
                "status": writing["status"],
                "last_error": writing_health.last_error,
            },
            "target_root": target_root,
            "research_assets": {
                "status": research_assets["status"],
                "last_error": research_asset_health.last_error,
            },
            "research_asset_verification": {
                "status": research_asset_verification["status"],
                "last_error": research_asset_verification_health.last_error,
            },
        }

    @app.get("/internal/doctor")
    def internal_doctor() -> dict[str, object]:
        harness = runtime.harnesses.query_status()
        target_root = runtime.query_target_root_readiness()
        return {
            **harness,
            "status": (
                "ready"
                if harness.get("status") == "ready"
                and target_root.get("status") == "ready"
                else "unavailable"
            ),
            "target_root": target_root,
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
        status, payload = await asyncio.to_thread(
            runtime.harnesses.dispatch_mcp, token, message
        )
        if payload is None:
            return Response(status_code=status)
        return JSONResponse(payload, status_code=status)

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
        if not runtime.authentication.revoke_session(session_token, csrf_token):
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
        return runtime.owners.human_collaboration.send_companion_message(
            message.scope_ref or "workspace",
            message.message,
            _idempotency_key(request),
        )

    @app.post("/api/v1/human-requests/{request_ref}/responses", status_code=201)
    def respond_to_human_request(
        request_ref: str,
        request: Request,
        response: HumanRequestResponseRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.respond_to_human_request(
            request_ref,
            decision=response.decision,
            facts=response.facts,
            note=response.note,
            idempotency_key=_idempotency_key(request),
        )

    @app.post("/api/v1/human-collaboration/agent-proposals", status_code=201)
    def record_agent_proposal(
        request: Request, proposal: AgentProposalRequest
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.record_agent_proposal(
            proposal.scope_ref,
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
        return (
            runtime.owners.human_collaboration.convert_agent_proposal_to_command_draft(
                proposal_ref,
                expected_scope_ref=conversion.expected_scope_ref,
                expected_proposal_hash=conversion.expected_proposal_hash,
                idempotency_key=_idempotency_key(request),
            )
        )

    @app.post("/api/v1/human-collaboration/soft-constraints", status_code=201)
    def record_soft_constraint(
        request: Request, constraint: SoftConstraintRequest
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.record_soft_constraint(
            constraint.scope_ref,
            constraint.guidance,
            _idempotency_key(request),
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
        return runtime.owners.human_collaboration.decide_capability_authorization(
            str(command["scope_ref"]),
            authorization.model_dump(),
            _idempotency_key(request),
        )

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
        materialized = await _await_bounded_asset_io(
            lambda: runtime.owners.research_memory.materialize_asset(memory_ref),
            slots=asset_io_slots,
            timeout_code="asset_materialization_io_timeout",
        )
        return Response(
            content=materialized.content,
            media_type=materialized.media_type,
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    + quote(materialized.file_name, safe="")
                )
            },
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

    @app.get("/api/v1/snapshot")
    def query_snapshot() -> dict[str, object]:
        return public_snapshot()

    @app.get("/api/v1/writing")
    def query_writing() -> dict[str, object]:
        return runtime.writing.query_overview()

    @app.post("/api/v1/writing/intents", status_code=201)
    def create_writing_intent(
        request: Request, intent: WritingIntentRequest
    ) -> dict[str, object]:
        return runtime.writing.create_report_intent(
            **intent.model_dump(), idempotency_key=_idempotency_key(request)
        )

    @app.post("/api/v1/writing/intents/{intent_id}/preview")
    def preview_writing_intent(
        request: Request, intent_id: str, _command: EmptyCommandRequest
    ) -> dict[str, object]:
        return runtime.writing.preview_report_intent(
            intent_id, idempotency_key=_idempotency_key(request)
        )

    @app.post("/api/v1/writing/intents/{intent_id}/confirmation")
    def confirm_writing_intent(
        request: Request,
        intent_id: str,
        confirmation: WritingConfirmationRequest,
    ) -> dict[str, object]:
        return runtime.writing.confirm_report_intent(
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
        format: Literal["markdown"] = Query(default="markdown"),
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
            media_type="text/markdown",
            headers={
                "X-Writing-Version-Ref": str(rendered["version_ref"]),
                "X-Writing-Render-Hash": str(rendered["render_hash"]),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/v1/experiments", status_code=201)
    async def start_experiment(
        request: Request,
        experiment: StartExperimentRequest,
    ) -> dict[str, object]:
        idempotency_key = _idempotency_key(request)
        return await _await_bounded_asset_io(
            lambda: runtime.experiment.start(
                experiment.as_intent(), idempotency_key, require_idle=True
            ),
            slots=asset_io_slots,
            timeout_code="experiment_admission_io_timeout",
        )

    @app.get("/api/v1/experiments/current")
    def query_current_experiment() -> dict[str, object]:
        current = runtime.experiment.query_current()
        return {
            "status": "idle" if current is None else "active",
            "current": current,
        }

    @app.get("/api/v1/experiments/{evaluation_attempt_ref}")
    def query_experiment(evaluation_attempt_ref: str) -> dict[str, object]:
        return runtime.experiment.query(evaluation_attempt_ref)

    @app.get("/api/v1/experiments/{evaluation_attempt_ref}/events")
    def query_experiment_events(
        evaluation_attempt_ref: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=256, ge=1, le=512),
    ) -> dict[str, object]:
        items = runtime.experiment.query_events(
            evaluation_attempt_ref,
            after_sequence=after,
            limit=limit,
        )
        return {
            "items": list(items),
            "after_sequence": after,
            "limit": limit,
            "next_after_sequence": (after if not items else items[-1]["sequence"]),
        }

    @app.get("/api/v1/idea-stage/current")
    def query_current_idea_stage() -> dict[str, object]:
        return runtime.idea_stage.query_current()

    @app.get("/api/v1/plan-stage/current")
    def query_current_plan_stage() -> dict[str, object]:
        return runtime.plan_stage.query_current()

    @app.get("/api/v1/bundle-stage/current")
    def query_current_bundle_stage() -> dict[str, object]:
        return runtime.bundle_stage.query_current()

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
    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.owners.human_collaboration.reconcile_once,
                health=health,
                timeout_code="quest_reconciliation_io_timeout",
                on_health_change=on_health_change,
                timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("quest reconciliation attempt failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 1.0)


async def _process_quest_drafting(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.owners.human_collaboration.process_drafting_once,
                health=health,
                timeout_code="quest_drafting_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=DRAFTING_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("quest drafting attempt failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_first_question_deepfetch(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance one durable, authorization-bound first-question DeepFetch run."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.deepfetch.process_once,
                health=health,
                timeout_code="deepfetch_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=DEEPFETCH_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("first-question DeepFetch attempt failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_experiments(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance execution, RM assets, then atomic Formal Measurement."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.experiment.process_once,
                health=health,
                timeout_code="experiment_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=EXPERIMENT_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("experiment worker attempt failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_writing(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance autonomous Writing independently of any browser connection."""

    quarantined: dict[
        tuple[str, str, str], _PendingWorkerRetirement | None
    ] = {}
    while True:
        try:
            for pending_claim, pending in tuple(quarantined.items()):
                if pending is None:
                    continue
                if pending.operation.done():
                    try:
                        pending.operation.result()
                    except Exception:
                        # The retired Fence prevents any late result from
                        # crossing a durable Owner boundary.
                        pass
                if not pending.retirement.done():
                    continue
                try:
                    pending.retirement.result()
                except Exception:
                    # An unexpected retirement failure is fail-closed for this
                    # process. A new Fence (control/resume) is a different
                    # claim and remains runnable.
                    quarantined[pending_claim] = None
                else:
                    quarantined.pop(pending_claim, None)
            claim = await _daemon_thread_call(
                lambda: runtime.writing.next_runnable_claim(
                    excluded_claims=frozenset(quarantined)
                )
            )
            if claim is None:
                advanced = False
            else:
                run_ref, attempt_ref, fence_ref = claim
                outcome = await _await_monitored_worker_call(
                    lambda: runtime.writing.process_once(
                        expected_run_ref=run_ref,
                        expected_attempt_ref=attempt_ref,
                        expected_fence_ref=fence_ref,
                    ),
                    health=health,
                    timeout_code="writing_operation_timeout",
                    on_health_change=on_health_change,
                    on_timeout=lambda: runtime.writing.block_writing_claim(
                        run_ref=run_ref,
                        attempt_ref=attempt_ref,
                        fence_ref=fence_ref,
                    ),
                    timeout_seconds=WRITING_WORKER_WATCHDOG_SECONDS,
                )
                if isinstance(outcome, _PendingWorkerRetirement):
                    quarantined[claim] = outcome
                    advanced = False
                else:
                    advanced = outcome
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
            if quarantined:
                changed = (
                    health.status != "unavailable"
                    or health.last_error
                    != "writing_claim_retirement_pending"
                )
                health.status = "unavailable"
                health.last_error = "writing_claim_retirement_pending"
            else:
                changed = (
                    health.status != "ready" or health.last_error is not None
                )
                health.status = "ready"
                health.last_error = None
                health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_research_assets(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Finish durable asynchronous Asset Intake jobs without scan starvation."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.owners.research_memory.process_asset_intake_once,
                health=health,
                timeout_code="asset_intake_io_timeout",
                on_health_change=on_health_change,
                timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("research asset intake failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0.05 if advanced else 0.2)


async def _verify_research_assets(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance the bounded durable verifier independently of intake latency."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.owners.research_memory.verify_asset_inventory_once,
                health=health,
                timeout_code="asset_verification_io_timeout",
                on_health_change=on_health_change,
                timeout_seconds=ASSET_WORKER_WATCHDOG_SECONDS,
            )
        except Exception as error:
            if not isinstance(error, (OSError, OwnerConflict, SQLAlchemyError)):
                LOGGER.exception("research asset verification failed unexpectedly")
            error_code = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0.05 if advanced else 0.2)


async def _await_monitored_worker_call(
    call: Callable[[], bool],
    *,
    health: ReconciliationHealth,
    timeout_code: str,
    on_health_change: Callable[[], None] | None,
    on_timeout: Callable[[], None] | None = None,
    timeout_seconds: float,
) -> bool | _PendingWorkerRetirement:
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
    if on_timeout is not None:
        retirement = _daemon_thread_call(on_timeout)
        retired, _pending_retirement = await asyncio.wait(
            {retirement}, timeout=timeout_seconds
        )
        if retired:
            retirement.result()
        else:
            if changed and on_health_change is not None:
                on_health_change()
            return _PendingWorkerRetirement(operation, retirement)
    if changed and on_health_change is not None:
        on_health_change()
    if on_timeout is not None:
        # The timed-out thread may still return, but the Owner callback has
        # retired its durable Fence. Let the worker loop continue with the next
        # runnable Run.
        return False
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
                timeout_seconds=HARNESS_CONFORMANCE_WORKER_WATCHDOG_SECONDS,
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
    """Advance one verified Idea boundary at a time and retry transient adapters."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.idea_stage.process_once,
                health=health,
                timeout_code="idea_stage_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=IDEA_STAGE_WORKER_WATCHDOG_SECONDS,
            )
            transient_error = runtime.idea_stage.transient_error
            if transient_error is not None:
                raise _IdeaStageTransientError(transient_error)
        except Exception as error:
            if not isinstance(
                error,
                (OSError, OwnerConflict, SQLAlchemyError, _IdeaStageTransientError),
            ):
                LOGGER.exception("idea stage attempt failed unexpectedly")
            error_code = (
                error.code
                if isinstance(error, (OwnerConflict, _IdeaStageTransientError))
                else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_plan_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance one verified Plan boundary at a time under daemon ownership."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.plan_stage.process_once,
                health=health,
                timeout_code="plan_stage_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=PLAN_STAGE_WORKER_WATCHDOG_SECONDS,
            )
            transient_error = runtime.plan_stage.transient_error
            if transient_error is not None:
                raise _PlanStageTransientError(transient_error)
        except Exception as error:
            if not isinstance(
                error,
                (OSError, OwnerConflict, SQLAlchemyError, _PlanStageTransientError),
            ):
                LOGGER.exception("plan stage attempt failed unexpectedly")
            error_code = (
                error.code
                if isinstance(error, (OwnerConflict, _PlanStageTransientError))
                else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


async def _process_target_runs(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Fairly wake each admitted or running Target root, independent of Bundle."""

    flights: dict[str, _TargetRunFlight] = {}
    cancel_flights: dict[str, _TargetRunFlight] = {}

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
        operation_error: str | None = None
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
                except Exception as error:
                    if not isinstance(
                        error, (OSError, OwnerConflict, SQLAlchemyError)
                    ):
                        LOGGER.exception("TargetRun boundary failed unexpectedly")
                    if operation_error is None:
                        operation_error = (
                            error.code
                            if isinstance(error, OwnerConflict)
                            else type(error).__name__
                        )

        if discovery_error is not None or operation_error is not None:
            set_health("unavailable", discovery_error or operation_error)
        else:
            set_health("ready", None)

        await asyncio.sleep(
            0 if advanced else (0.05 if flights or cancel_flights else 0.2)
        )


async def _process_bundle_stage(
    runtime: ProductionRuntime,
    health: ReconciliationHealth,
    on_health_change: Callable[[], None] | None = None,
) -> None:
    """Advance one verified Bundle boundary at a time under daemon ownership."""

    while True:
        try:
            advanced = await _await_monitored_worker_call(
                runtime.bundle_stage.process_once,
                health=health,
                timeout_code="bundle_stage_operation_timeout",
                on_health_change=on_health_change,
                timeout_seconds=BUNDLE_STAGE_WORKER_WATCHDOG_SECONDS,
            )
            transient_error = runtime.bundle_stage.transient_error
            if (
                transient_error is not None
                and transient_error not in _BUNDLE_STAGE_HEALTHY_WAIT_CODES
            ):
                raise _BundleStageTransientError(transient_error)
        except Exception as error:
            if not isinstance(
                error,
                (
                    OSError,
                    OwnerConflict,
                    SQLAlchemyError,
                    _BundleStageTransientError,
                ),
            ):
                LOGGER.exception("bundle stage attempt failed unexpectedly")
            error_code = (
                error.code
                if isinstance(error, (OwnerConflict, _BundleStageTransientError))
                else type(error).__name__
            )
            changed = health.status != "unavailable" or health.last_error != error_code
            health.status = "unavailable"
            health.last_error = error_code
            health.retry_count += 1
            if changed and on_health_change is not None:
                on_health_change()
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            changed = health.status != "ready" or health.last_error is not None
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            if changed and on_health_change is not None:
                on_health_change()
            await asyncio.sleep(0 if advanced else 0.2)


class _IdeaStageTransientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _PlanStageTransientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _BundleStageTransientError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
