"""Manual Draft must stream before completion through its real owner and HTTP."""
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.quest_drafting import IntentTurnResult
from meta_research.web import SESSION_COOKIE, create_app
from test_public_manual_question_lifecycle import (
    DeterministicDraftingAdapter, _build_runtime, _queue_manual_drafting_turn,
)


class StreamingProvider(DeterministicDraftingAdapter):
    def __init__(self):
        super().__init__()
        self.started, self.finish = Event(), Event()
        self.preview = ""
        self.job_ref = None

    def reply(self, request):
        self.job_ref = request.job_ref
        self.started.set()
        assert self.finish.wait(10)
        return IntentTurnResult("Complete answer.", "manual-stream-test", "test_stream")

    def observe_reply(self, job_ref):
        assert job_ref == self.job_ref
        return self.preview


@pytest.fixture
def manual(tmp_path):
    provider = StreamingProvider()
    runtime = _build_runtime(tmp_path / "manual-stream", drafting=provider)
    try:
        context, turn = _queue_manual_drafting_turn(runtime, key_prefix="stream")
        yield runtime, provider, context["context_ref"], turn["ref"]
    finally:
        provider.finish.set()
        runtime.close()


def url(context, turn):
    return f"/api/v1/manual-question-creations/{context}/drafting-session/turns/{turn}/stream"


def test_manual_http_delivers_fragments_before_provider_finishes(manual):
    runtime, provider, context, turn = manual
    human = runtime.owners.human_collaboration
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(runtime, base_url=base, control_key="test"), lifespan="off", log_level="error"))
    session = runtime.authentication.issue_session()
    with ThreadPoolExecutor(max_workers=2) as executor:
        serving = executor.submit(server.run, sockets=[listener])
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < deadline
                time.sleep(.01)
            producing = executor.submit(human.process_drafting_once)
            assert provider.started.wait(5)
            revision = runtime.feed.current_revision()
            provider.preview = "First part."
            with httpx.Client(base_url=base, timeout=3, cookies={SESSION_COOKIE: session.token}) as client:
                with client.stream("GET", url(context, turn)) as response:
                    assert response.status_code == 200
                    events = (json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: "))
                    assert next(events) == {"turn_ref": turn, "status": "running", "text": "First part."}
                    provider.preview = "First part. Second part."
                    assert next(events)["text"] == provider.preview
                    assert not producing.done()
                    assert runtime.feed.current_revision() == revision
                    with runtime._database.read() as connection:
                        assert connection.execute(text("SELECT assistant_content FROM hc_manual_drafting_turns WHERE turn_ref=:ref"), {"ref": turn}).scalar_one() is None
                    provider.finish.set()
                    assert next(events) == {"turn_ref": turn, "status": "completed", "text": "Complete answer."}
                    assert list(events) == []
                assert producing.result(timeout=5)
                replay = client.get(url(context, turn))
                assert replay.status_code == 200
                assert '"status":"completed"' in replay.text
        finally:
            provider.finish.set()
            server.should_exit = True
            serving.result(timeout=5)
            listener.close()


def test_manual_stream_rejects_other_context_and_tampered_final(manual):
    runtime, provider, context, turn = manual
    human = runtime.owners.human_collaboration
    with pytest.raises(OwnerConflict):
        human.query_manual_drafting_reply("other-context", turn)
    with pytest.raises(OwnerConflict, match="manual_drafting_turn_not_found"):
        human.query_manual_drafting_reply(context, "other-turn")
    provider.finish.set()
    assert human.process_drafting_once()
    assert human.query_manual_drafting_reply(context, turn)["text"] == "Complete answer."
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE hc_manual_drafting_turns SET assistant_content='tampered' WHERE turn_ref=:ref"), {"ref": turn})
    with pytest.raises(OwnerConflict, match="manual_drafting_turn_invalid"):
        human.query_manual_drafting_reply(context, turn)


def test_manual_cancelled_turn_is_terminal_and_has_no_preview(manual):
    runtime, provider, context, turn = manual
    human = runtime.owners.human_collaboration
    assert human.query_manual_drafting_reply(context, turn) == {"turn_ref": turn, "status": "queued", "text": ""}
    human.cancel_manual_question_creation(context, "cancel-stream")
    provider.preview = "must never be shown"
    assert human.query_manual_drafting_reply(context, turn) == {"turn_ref": turn, "status": "failed", "text": ""}
