from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from meta_research.auth import AuthSession
from meta_research.composition import ProductionRuntime
from meta_research.owners.common import OwnerConflict


SESSION_COOKIE = "meta_research_session"
CSRF_COOKIE = "meta_research_csrf"
LOGGER = logging.getLogger(__name__)


@dataclass
class ReconciliationHealth:
    status: Literal["ready", "unavailable"] = "ready"
    last_error: str | None = None
    retry_count: int = 0


class BootstrapExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=256)


class QuestDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    completion_criteria: str = Field(min_length=1, max_length=4000)
    key_configuration: str = Field(min_length=1, max_length=4000)
    literature_scope: Literal[
        "comprehensive", "open_access", "provided_materials"
    ]
    initial_question_direction: str = Field(min_length=1, max_length=4000)
    material_receipts: list[str] = Field(default_factory=list, max_length=100)


class ReviseQuestDraftRequest(QuestDraftRequest):
    expected_draft_hash: str = Field(min_length=64, max_length=64)


class GenerateProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_hash: str = Field(min_length=64, max_length=64)


class QuestionContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=500)
    unknown_statement: str = Field(max_length=8000)
    answer_shape: str = Field(max_length=8000)
    applicability_scope: str = Field(max_length=8000)
    background_context: str = Field(max_length=12000)
    requirements_constraints: str = Field(max_length=12000)


class SaveProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_hash: str = Field(min_length=64, max_length=64)
    content: QuestionContentRequest


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
    app = FastAPI(
        title="Meta-research vNext",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    web_root = Path(str(files("meta_research") / "web_dist")).resolve()
    expected_host = urlsplit(base_url).netloc
    reconciliation_task: asyncio.Task[None] | None = None
    reconciliation_health = ReconciliationHealth()

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
            if not runtime.authentication.session_is_valid(session_token):
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
                or not runtime.authentication.csrf_matches(
                    request.state.session_token, csrf_header
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

    @app.on_event("startup")
    async def start_quest_reconciliation() -> None:
        nonlocal reconciliation_task
        reconciliation_task = asyncio.create_task(
            _reconcile_quest_initializations(runtime, reconciliation_health)
        )
        reconciliation_task.add_done_callback(_log_reconciliation_exit)

    @app.on_event("shutdown")
    async def stop_quest_reconciliation() -> None:
        if reconciliation_task is None:
            return
        reconciliation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconciliation_task

    @app.post("/internal/bootstrap-token")
    async def issue_bootstrap_token() -> dict[str, str]:
        return {"bootstrap_token": runtime.authentication.issue_bootstrap_token()}

    @app.post("/internal/browser-grant")
    async def issue_browser_grant() -> dict[str, str]:
        return {"browser_grant": runtime.authentication.issue_browser_grant()}

    @app.post("/internal/browser-grant-status")
    async def browser_grant_status(exchange: BootstrapExchange) -> dict[str, bool]:
        return {
            "consumed": runtime.authentication.browser_grant_was_consumed(
                exchange.token
            )
        }

    @app.get("/internal/readiness")
    async def internal_readiness() -> dict[str, object]:
        snapshot = runtime.projection.query_snapshot()
        worker_ready = (
            reconciliation_health.status == "ready"
            and reconciliation_task is not None
            and not reconciliation_task.done()
        )
        return {
            "status": (
                snapshot["readiness"]["status"] if worker_ready else "unavailable"
            ),
            "revision": snapshot["revision"],
            "reconciliation": {
                "status": "ready" if worker_ready else "unavailable",
                "last_error": reconciliation_health.last_error,
            },
        }

    @app.post("/auth/bootstrap")
    async def exchange_bootstrap(exchange: BootstrapExchange) -> JSONResponse:
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
        session = runtime.authentication.exchange_browser_grant(grant)
        if session is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "browser_grant_invalid"},
            )
        response = FileResponse(web_root / "index.html")
        _set_session_cookie(response, session)
        return response

    @app.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        csrf_token = request.headers.get("x-csrf-token", "")
        session_token = request.state.session_token
        if not runtime.authentication.revoke_session(session_token, csrf_token):
            raise HTTPException(status_code=403, detail={"code": "csrf_invalid"})
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")
        response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")
        return response

    @app.get("/api/v1/session")
    async def session_status() -> dict[str, str]:
        return {"status": "authenticated"}

    @app.post("/api/v1/quest-initializations", status_code=201)
    async def create_quest_initialization(
        request: Request, draft: QuestDraftRequest
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.create_quest(
            draft.model_dump(), _idempotency_key(request)
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/draft")
    async def revise_quest_initialization(
        initialization_id: str,
        request: Request,
        draft: ReviseQuestDraftRequest,
    ) -> dict[str, object]:
        value = draft.model_dump(exclude={"expected_draft_hash"})
        return runtime.owners.human_collaboration.revise_quest_draft(
            initialization_id,
            value,
            draft.expected_draft_hash,
            _idempotency_key(request),
        )

    @app.post(
        "/api/v1/quest-initializations/{initialization_id}/proposal",
        status_code=201,
    )
    async def generate_question_proposal(
        initialization_id: str,
        request: Request,
        generation: GenerateProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.generate_question_proposal(
            initialization_id,
            generation.expected_draft_hash,
            _idempotency_key(request),
        )

    @app.put("/api/v1/quest-initializations/{initialization_id}/proposal")
    async def save_question_proposal(
        initialization_id: str,
        request: Request,
        proposal: SaveProposalRequest,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.save_question_proposal(
            initialization_id,
            proposal.expected_draft_hash,
            proposal.content.model_dump(),
            _idempotency_key(request),
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
    async def confirm_quest_initialization(
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
    async def cancel_quest_initialization(
        initialization_id: str, request: Request
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.cancel_quest(
            initialization_id, _idempotency_key(request)
        )

    @app.get("/api/v1/quest-initializations/{initialization_id}")
    async def query_quest_initialization(
        initialization_id: str,
    ) -> dict[str, object]:
        return runtime.owners.human_collaboration.query_quest_creation(
            initialization_id
        )

    @app.get("/api/v1/snapshot")
    async def query_snapshot() -> dict[str, object]:
        return runtime.projection.query_snapshot()

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
            _event_stream(runtime, request, last_revision),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/")
    async def web_shell() -> FileResponse:
        index = web_root / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503, detail={"code": "web_shell_unavailable"}
            )
        return FileResponse(index)

    @app.get("/assets/{asset_path:path}")
    async def web_asset(asset_path: str) -> FileResponse:
        asset_root = (web_root / "assets").resolve()
        candidate = (asset_root / asset_path).resolve()
        if not candidate.is_relative_to(asset_root) or not candidate.is_file():
            raise HTTPException(status_code=404, detail={"code": "asset_not_found"})
        return FileResponse(candidate)

    return app


async def _event_stream(
    runtime: ProductionRuntime, request: Request, last_revision: int
) -> AsyncIterator[str]:
    cursor = last_revision
    first_page = True
    while True:
        page = runtime.feed.read_after(cursor)
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
            cursor = event.revision
            payload = json.dumps(event.payload, separators=(",", ":"))
            yield (
                f"id: {event.revision}\n"
                f"event: {event.event_type}\n"
                f"data: {payload}\n\n"
            )
        if first_page and page.events:
            first_page = False
            continue
        first_page = False
        if await request.is_disconnected():
            return
        yield ": keep-alive\n\n"
        await asyncio.sleep(1)


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
    runtime: ProductionRuntime, health: ReconciliationHealth
) -> None:
    while True:
        try:
            advanced = await asyncio.to_thread(
                runtime.owners.human_collaboration.reconcile_once
            )
        except (OSError, OwnerConflict, SQLAlchemyError) as error:
            health.status = "unavailable"
            health.last_error = (
                error.code if isinstance(error, OwnerConflict) else type(error).__name__
            )
            health.retry_count += 1
            retry_delay = min(2.0, 0.2 * (2 ** min(health.retry_count - 1, 4)))
            await asyncio.sleep(retry_delay)
        else:
            health.status = "ready"
            health.last_error = None
            health.retry_count = 0
            await asyncio.sleep(0 if advanced else 1.0)


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
