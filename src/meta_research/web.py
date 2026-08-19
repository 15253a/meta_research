from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from meta_research.auth import AuthSession
from meta_research.composition import ProductionRuntime


SESSION_COOKIE = "meta_research_session"


class BootstrapExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=256)


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

        if request.method == "POST" and path in {"/auth/bootstrap", "/auth/logout"}:
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
            )
            if content_type != "application/json":
                return _error(415, "json_required")
            if request.headers.get("origin") != base_url:
                return _error(403, "origin_invalid")

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
        return {
            "status": snapshot["readiness"]["status"],
            "revision": snapshot["revision"],
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
        return response

    @app.get("/api/v1/session")
    async def session_status() -> dict[str, str]:
        return {"status": "authenticated"}

    @app.get("/api/v1/snapshot")
    async def query_snapshot() -> dict[str, object]:
        return runtime.projection.query_snapshot()

    @app.get("/api/v1/events")
    async def stream_events(request: Request) -> StreamingResponse:
        raw_revision = request.headers.get("last-event-id", "0")
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


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})
