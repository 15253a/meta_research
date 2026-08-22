from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import math
import threading
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


SESSION_COOKIE = "meta_research_session"
CSRF_COOKIE = "meta_research_csrf"
LOGGER = logging.getLogger(__name__)
MAX_ASSET_INTAKE_REQUEST_BODY_BYTES = 96 * 1024 * 1024
MAX_COMMAND_REQUEST_BODY_BYTES = 1 * 1024 * 1024
# Kept as the public intake-envelope constant used by compatibility tests and
# callers that size a Research Asset request before sending it.
MAX_JSON_REQUEST_BODY_BYTES = MAX_ASSET_INTAKE_REQUEST_BODY_BYTES
MAX_CONCURRENT_ASSET_INTAKE_REQUESTS = 2
MAX_CONCURRENT_ASSET_IO_OPERATIONS = 2
ASSET_WORKER_WATCHDOG_SECONDS = 5.0
ASSET_ROUTE_WATCHDOG_SECONDS = 5.0
DRAFTING_WORKER_WATCHDOG_SECONDS = 190.0
IDEA_STAGE_WORKER_WATCHDOG_SECONDS = 910.0
DEEPFETCH_WORKER_WATCHDOG_SECONDS = 1810.0
_T = TypeVar("_T")


@dataclass
class ReconciliationHealth:
    status: Literal["ready", "unavailable"] = "ready"
    last_error: str | None = None
    retry_count: int = 0


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
    answer_shape: str = Field(
        max_length=QUESTION_FIELD_MAX_LENGTHS["answer_shape"]
    )
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


class CapabilityAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=64)
    decision: Literal["granted", "denied", "revoked"]
    scope: dict[str, object]
    confirmation_receipt_ref: str = Field(min_length=1, max_length=64)


def create_app(
    runtime: ProductionRuntime, *, base_url: str, control_key: str
) -> FastAPI:
    reconciliation_task: asyncio.Task[None] | None = None
    drafting_task: asyncio.Task[None] | None = None
    deepfetch_task: asyncio.Task[None] | None = None
    idea_stage_task: asyncio.Task[None] | None = None
    research_asset_task: asyncio.Task[None] | None = None
    research_asset_verification_task: asyncio.Task[None] | None = None
    reconciliation_health = ReconciliationHealth()
    drafting_health = ReconciliationHealth()
    deepfetch_health = ReconciliationHealth()
    idea_stage_health = ReconciliationHealth()
    research_asset_health = ReconciliationHealth()
    research_asset_verification_health = ReconciliationHealth()
    worker_health_updates = WorkerHealthUpdates()
    asset_intake_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_INTAKE_REQUESTS)
    asset_io_slots = asyncio.Semaphore(MAX_CONCURRENT_ASSET_IO_OPERATIONS)
    asset_intake_recovery_slots = asyncio.Semaphore(1)
    asset_handoff_singleflight = _AssetIOSingleFlight()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal reconciliation_task, drafting_task, deepfetch_task, idea_stage_task
        nonlocal research_asset_task, research_asset_verification_task
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
                    reconciliation_task,
                    drafting_task,
                    deepfetch_task,
                    idea_stage_task,
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

        if internal_route:
            supplied = request.headers.get("x-meta-research-control")
            if not runtime.authentication.control_key_matches(supplied, control_key):
                return _error(401, "control_authentication_required")
        elif not public_auth_route:
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
        unsafe_api_route = (
            path.startswith("/api/")
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        )
        is_asset_intake = (
            request.method == "POST"
            and path == "/api/v1/research-assets/intakes"
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
            if json_auth_route or unsafe_api_route:
                request_body_limit = (
                    MAX_ASSET_INTAKE_REQUEST_BODY_BYTES
                    if is_asset_intake
                    else MAX_COMMAND_REQUEST_BODY_BYTES
                )
                content_length = request.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        return _error(400, "content_length_invalid")
                    if (
                        declared_length < 0
                        or declared_length > request_body_limit
                    ):
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
                "quest_initialization_not_found",
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
        drafting = worker_check(
            "quest_drafting_worker", drafting_task, drafting_health
        )
        deepfetch = worker_check(
            "first_question_deepfetch_worker", deepfetch_task, deepfetch_health
        )
        idea_stage = worker_check(
            "idea_stage_worker", idea_stage_task, idea_stage_health
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
            "research_assets": {
                "status": research_assets["status"],
                "last_error": research_asset_health.last_error,
            },
            "research_asset_verification": {
                "status": research_asset_verification["status"],
                "last_error": research_asset_verification_health.last_error,
            },
        }

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

    @app.post(
        "/api/v1/human-requests/{request_ref}/responses", status_code=201
    )
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
        return runtime.owners.human_collaboration.convert_agent_proposal_to_command_draft(
            proposal_ref,
            expected_scope_ref=conversion.expected_scope_ref,
            expected_proposal_hash=conversion.expected_proposal_hash,
            idempotency_key=_idempotency_key(request),
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
            or receipt.get("receipt_ref")
            != authorization.confirmation_receipt_ref
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

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/compute-probe"
    )
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

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/acquisition-session"
    )
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

    @app.get(
        "/api/v1/quest-initializations/{initialization_id}/intent-session"
    )
    def query_intent_session(initialization_id: str) -> dict[str, object]:
        view = runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )
        return {"intent_session": view["intent_session"]}

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
            status_code=(
                202 if result.status in {"queued", "processing"} else 201
            ),
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
        limit: int = Query(
            default=50, ge=1, le=ASSET_ROLE_QUERY_MAX_PAGE_SIZE
        ),
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
            item = (
                runtime.owners.research_memory.query_asset_projection_inventory_item(
                    memory_ref
                )
            )
            if item is None:
                raise OwnerConflict("asset_not_found")
            custodies = runtime.owners.research_memory.query_asset_custodies(
                memory_ref
            )
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
        limit: int = Query(
            default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE
        ),
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
        limit: int = Query(
            default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE
        ),
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
        limit: int = Query(
            default=50, ge=1, le=ASSET_HISTORY_QUERY_MAX_PAGE_SIZE
        ),
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
                expected_reference_revision=(
                    assessment.expected_reference_revision
                ),
                idempotency_key=idempotency_key,
            ),
            slots=asset_io_slots,
            timeout_code="asset_release_io_timeout",
        )
        return result.as_public_dict()

    @app.get("/api/v1/snapshot")
    def query_snapshot() -> dict[str, object]:
        return public_snapshot()

    @app.get("/api/v1/idea-stage/current")
    def query_current_idea_stage() -> dict[str, object]:
        return runtime.idea_stage.query_current()

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
        yield (
            "event: snapshot.required\n"
            f"data: {payload}\n\n"
        )
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
            yield (
                f"event: {event.event_type}\n"
                f"data: {payload}\n\n"
            )
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
        yield (
            "event: snapshot.required\n"
            f"data: {payload}\n\n"
        )


async def _sse_session_is_valid(
    runtime: ProductionRuntime, request: Request
) -> bool:
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
    timeout_seconds: float,
) -> bool:
    """Keep worker stalls outside the event loop and expose a watchdog.

    Python cannot safely cancel a thread blocked inside an arbitrary FUSE/NFS
    syscall. A dedicated daemon thread therefore owns exactly one operation;
    the coroutine publishes a typed blocker after a bounded interval without
    spawning duplicate attempts. If the mount recovers, the same durable job
    resumes. A stuck daemon thread cannot hold process shutdown open.
    """

    operation = _daemon_thread_call(call)
    timed_out = False
    while True:
        done, _pending = await asyncio.wait(
            {operation},
            timeout=timeout_seconds,
        )
        if done:
            return operation.result()
        if timed_out:
            continue
        timed_out = True
        changed = (
            health.status != "unavailable"
            or health.last_error != timeout_code
        )
        health.status = "unavailable"
        health.last_error = timeout_code
        health.retry_count += 1
        if changed and on_health_change is not None:
            on_health_change()


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


class _IdeaStageTransientError(RuntimeError):
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
