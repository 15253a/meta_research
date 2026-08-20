from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator, Callable, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from meta_research.auth import AuthSession
from meta_research.composition import ProductionRuntime
from meta_research.owners.common import OwnerConflict
from meta_research.projection import SnapshotConsistencyUnavailable
from meta_research.quest_drafting import (
    INTENT_MESSAGE_MAX_LENGTH,
    QUESTION_FIELD_MAX_LENGTHS,
)


SESSION_COOKIE = "meta_research_session"
CSRF_COOKIE = "meta_research_csrf"
LOGGER = logging.getLogger(__name__)


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


def create_app(
    runtime: ProductionRuntime, *, base_url: str, control_key: str
) -> FastAPI:
    reconciliation_task: asyncio.Task[None] | None = None
    drafting_task: asyncio.Task[None] | None = None
    reconciliation_health = ReconciliationHealth()
    drafting_health = ReconciliationHealth()
    worker_health_updates = WorkerHealthUpdates()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal reconciliation_task, drafting_task
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
        try:
            yield
        finally:
            tasks = tuple(
                task
                for task in (reconciliation_task, drafting_task)
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
        ]
        snapshot["readiness"] = {
            "status": (
                "ready"
                if readiness["status"] == "ready"
                and all(check["status"] == "ready" for check in checks)
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

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(OwnerConflict)
    async def owner_conflict(_request: Request, error: OwnerConflict) -> JSONResponse:
        status_code = 404 if error.code == "quest_initialization_not_found" else 409
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

    @app.post("/api/v1/quest-initializations", status_code=201)
    def create_quest_initialization(
        request: Request,
        draft: OpenQuestRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.create_quest(
            draft.model_dump(), _idempotency_key(request)
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/draft")
    def revise_quest_initialization(
        initialization_id: str,
        request: Request,
        draft: ReviseQuestDraftV2Request,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.revise_quest_draft(
            initialization_id,
            draft.draft.model_dump(),
            draft.expected_draft_hash,
            _idempotency_key(request),
            draft.expected_draft_revision,
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/proposal",
        status_code=202,
    )
    def generate_question_proposal(
        initialization_id: str,
        request: Request,
        generation: GenerateProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.generate_question_proposal(
            initialization_id,
            generation.expected_draft_hash,
            _idempotency_key(request),
            generation.expected_draft_revision,
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/proposal-generations",
        status_code=202,
    )
    def enqueue_question_proposal(
        initialization_id: str,
        request: Request,
        generation: GenerateProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.generate_question_proposal(
            initialization_id,
            generation.expected_draft_hash,
            _idempotency_key(request),
            generation.expected_draft_revision,
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/proposal")
    def save_question_proposal(
        initialization_id: str,
        request: Request,
        proposal: SaveProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.save_question_proposal(
            initialization_id,
            proposal.expected_draft_hash,
            proposal.content.model_dump(),
            _idempotency_key(request),
            proposal.expected_draft_revision,
            proposal.expected_proposal_ref,
            proposal.expected_proposal_hash,
            proposal.explicit_review,
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
    def preview_quest_confirmation(
        initialization_id: str,
        request: Request,
        preview: ConfirmationPreviewRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.preview_confirmation(
            initialization_id,
            quest_draft_revision=preview.quest_draft_revision,
            quest_draft_hash=preview.quest_draft_hash,
            proposal_ref=preview.proposal_ref,
            proposal_hash=preview.proposal_hash,
            idempotency_key=_idempotency_key(request),
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/confirmation",
        status_code=202,
    )
    def confirm_quest_initialization(
        initialization_id: str,
        request: Request,
        confirmation: ConfirmQuestRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.confirm_quest(
            initialization_id,
            quest_draft_revision=confirmation.quest_draft_revision,
            quest_draft_hash=confirmation.quest_draft_hash,
            proposal_ref=confirmation.proposal_ref,
            proposal_hash=confirmation.proposal_hash,
            preview_ref=confirmation.preview_ref,
            preview_hash=confirmation.preview_hash,
            idempotency_key=_idempotency_key(request),
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

    @app.get("/api/v1/snapshot")
    def query_snapshot() -> dict[str, object]:
        return public_snapshot()

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
    if not 1 <= len(value) <= 128 or any(character.isspace() for character in value):
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
            advanced = await asyncio.to_thread(
                runtime.owners.human_collaboration.reconcile_once
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
            advanced = await asyncio.to_thread(
                runtime.owners.human_collaboration.process_drafting_once
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
