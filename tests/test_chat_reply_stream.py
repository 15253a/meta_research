from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from urllib.parse import quote

import pytest
from sqlalchemy import text
from starlette.requests import Request

from meta_research import web
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import DraftingUnavailable, IntentTurnResult
from test_public_human_collaboration_web import (
    _DeterministicDraftingProvider,
    _DeterministicProbe,
    _authenticated_client,
)


class _StreamingProvider(_DeterministicDraftingProvider):
    def __init__(self):
        self.started = Event()
        self.finish = Event()
        self.job_ref = None
        self.preview = ""
        self.observed_jobs = []
        self.fail = False

    def reply(self, request):
        self.job_ref = request.job_ref
        self.started.set()
        assert self.finish.wait(10), "test must release the provider"
        if self.fail:
            raise DraftingUnavailable("codex_cli_unavailable")
        return IntentTurnResult(
            reply="最终完整回复。", native_session_ref="stream-test-native",
            adapter_kind="deterministic_stream_test",
        )

    def observe_reply(self, job_ref):
        self.observed_jobs.append(job_ref)
        return self.preview if job_ref == self.job_ref else ""


@pytest.fixture
def runtime_and_provider(tmp_path: Path):
    provider = _StreamingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "chat-stream"),
        proposal_drafter=provider, intent_drafting_provider=provider,
        host_compute_probe=_DeterministicProbe(),
    )
    try:
        yield runtime, provider
    finally:
        provider.finish.set()
        runtime.close()


def _queue(runtime, kind):
    hc = runtime.owners.human_collaboration
    if kind == "companion":
        queued = hc.send_companion_message("workspace", "请解释当前配置。", "stream-companion")
        turn_ref = queued["interaction_ref"]
        return "workspace", turn_ref
    opened = hc.create_quest({}, "stream-quest")
    initialization_id = opened["initialization_id"]
    queued = hc.send_intent_message(
        initialization_id,
        expected_draft_revision=opened["quest_draft"]["revision"],
        expected_draft_hash=opened["quest_draft"]["hash"],
        message="请解释当前配置。", idempotency_key="stream-intent",
    )
    return initialization_id, queued["intent_session"]["turns"][-1]["ref"]


def _query(runtime, kind, scope, turn_ref):
    hc = runtime.owners.human_collaboration
    if kind == "companion":
        return hc.query_companion_reply(scope, turn_ref)
    return hc.query_intent_reply(scope, turn_ref)


def _url(kind, scope, turn_ref):
    if kind == "companion":
        return f"/api/v1/companion/messages/{quote(turn_ref, safe='')}/stream?scope_ref={quote(scope, safe='')}"
    return f"/api/v1/quest-initializations/{quote(scope, safe='')}/intent-session/turns/{quote(turn_ref, safe='')}/stream"


def _events(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _request(session_token):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []}, receive)
    request.state.session_token = session_token
    return request


@pytest.mark.parametrize("kind", ["companion", "intent"])
def test_reply_progress_is_read_only_and_final_is_owner_accepted(runtime_and_provider, kind):
    runtime, provider = runtime_and_provider
    scope, turn_ref = _queue(runtime, kind)
    assert _query(runtime, kind, scope, turn_ref) == {
        "turn_ref": turn_ref, "status": "queued", "text": "",
    }
    assert provider.observed_jobs == []
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(runtime.owners.human_collaboration.process_drafting_once)
        try:
            assert provider.started.wait(5)
            expected_status = "processing" if kind == "companion" else "running"
            revision = runtime.feed.current_revision()
            for preview in ["正在分析", "正在分析你的问题。"]:
                provider.preview = preview
                assert _query(runtime, kind, scope, turn_ref) == {
                    "turn_ref": turn_ref, "status": expected_status, "text": preview,
                }
                assert runtime.feed.current_revision() == revision
                table = "hc_companion_turns" if kind == "companion" else "hc_intent_drafting_turns"
                column = "interaction_ref" if kind == "companion" else "turn_ref"
                with runtime._database.read() as connection:
                    persisted = connection.execute(text(
                        f"SELECT assistant_content, assistant_content_hash FROM {table} WHERE {column} = :turn_ref"
                    ), {"turn_ref": turn_ref}).one()
                assert tuple(persisted) == (None, None)
            assert provider.observed_jobs == [provider.job_ref, provider.job_ref]
        finally:
            provider.finish.set()
        assert task.result(timeout=5)
    provider.preview = "obsolete preview must not replace final content"
    observed_count = len(provider.observed_jobs)
    assert _query(runtime, kind, scope, turn_ref) == {
        "turn_ref": turn_ref, "status": "completed", "text": "最终完整回复。",
    }
    assert len(provider.observed_jobs) == observed_count


@pytest.mark.parametrize("kind", ["companion", "intent"])
def test_failed_reply_hides_previous_preview_and_stream_closes(runtime_and_provider, kind):
    runtime, provider = runtime_and_provider
    scope, turn_ref = _queue(runtime, kind)
    _query(runtime, kind, scope, turn_ref)
    provider.fail = True
    provider.preview = "未完成的输出"
    provider.finish.set()
    assert runtime.owners.human_collaboration.process_drafting_once()
    result = _query(runtime, kind, scope, turn_ref)
    failed_status = "failed" if kind == "companion" else "unavailable"
    assert result == {"turn_ref": turn_ref, "status": failed_status, "text": ""}
    client, _ = _authenticated_client(runtime)
    try:
        response = client.get(_url(kind, scope, turn_ref))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert _events(response.text) == [result]
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["companion", "intent"])
def test_stream_reconnect_replays_completed_reply_and_rejects_wrong_scope(runtime_and_provider, kind):
    runtime, provider = runtime_and_provider
    scope, turn_ref = _queue(runtime, kind)
    _query(runtime, kind, scope, turn_ref)
    provider.finish.set()
    assert runtime.owners.human_collaboration.process_drafting_once()
    client, _ = _authenticated_client(runtime)
    try:
        for _ in range(2):
            response = client.get(_url(kind, scope, turn_ref))
            assert response.status_code == 200
            assert _events(response.text) == [{
                "turn_ref": turn_ref, "status": "completed", "text": "最终完整回复。",
            }]
        with pytest.raises(OwnerConflict):
            _query(runtime, kind, "different-scope", turn_ref)
        response = client.get(_url(kind, "different-scope", turn_ref))
        assert response.status_code == 409
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["companion", "intent"])
def test_completed_reply_requires_owner_content_hash(runtime_and_provider, kind):
    runtime, provider = runtime_and_provider
    scope, turn_ref = _queue(runtime, kind)
    _query(runtime, kind, scope, turn_ref)
    provider.finish.set()
    assert runtime.owners.human_collaboration.process_drafting_once()
    table = "hc_companion_turns" if kind == "companion" else "hc_intent_drafting_turns"
    column = "interaction_ref" if kind == "companion" else "turn_ref"
    with runtime._database.write() as connection:
        connection.execute(text(
            f"UPDATE {table} SET assistant_content = :content WHERE {column} = :turn_ref"
        ), {"content": "tampered", "turn_ref": turn_ref})
    with pytest.raises(OwnerConflict):
        _query(runtime, kind, scope, turn_ref)


def test_stream_replays_progress_then_closes_on_real_session_revocation(runtime_and_provider):
    runtime, _ = runtime_and_provider
    stream_factory = web._chat_reply_stream
    session = runtime.authentication.issue_session()
    request = _request(session.token)
    state = {"turn_ref": "turn-live", "status": "processing", "text": "first"}
    queries = []

    def query():
        queries.append(True)
        return dict(state)

    async def exercise():
        stream = stream_factory(runtime, request, query, dict(state))
        try:
            assert _events(await anext(stream)) == [state]
            state["text"] = "first second"
            assert _events(await asyncio.wait_for(anext(stream), 2)) == [state]
            assert runtime.authentication.revoke_session(session.token, session.csrf_token)
            count = len(queries)
            state["text"] = "private after logout"
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 2)
            assert len(queries) == count
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_stream_checks_session_again_after_query_returns(runtime_and_provider):
    runtime, _ = runtime_and_provider
    stream_factory = web._chat_reply_stream
    session = runtime.authentication.issue_session()
    request = _request(session.token)
    initial = {"turn_ref": "turn-live", "status": "processing", "text": "public"}

    def query():
        assert runtime.authentication.revoke_session(session.token, session.csrf_token)
        return {"turn_ref": "turn-live", "status": "completed", "text": "private after logout"}

    async def exercise():
        stream = stream_factory(runtime, request, query, initial)
        try:
            assert _events(await anext(stream)) == [initial]
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 2)
        finally:
            await stream.aclose()

    asyncio.run(exercise())
