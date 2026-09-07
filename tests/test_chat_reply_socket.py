"""Exercise actual HTTP flushing while the reply provider remains blocked."""
from __future__ import annotations

import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

from meta_research.web import SESSION_COOKIE, create_app
from test_chat_reply_stream import _queue, _url, runtime_and_provider


@pytest.mark.parametrize("kind", ["companion", "intent"])
def test_http_stream_delivers_both_fragments_before_provider_finishes(
    runtime_and_provider, kind
):
    runtime, provider = runtime_and_provider
    scope, turn_ref = _queue(runtime, kind)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(runtime, base_url=base_url, control_key="socket-test")
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    session = runtime.authentication.issue_session()
    with ThreadPoolExecutor(max_workers=2) as executor:
        serving = executor.submit(server.run, sockets=[listener])
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert time.monotonic() < deadline, "HTTP server did not start"
                time.sleep(0.01)
            producing = executor.submit(
                runtime.owners.human_collaboration.process_drafting_once
            )
            assert provider.started.wait(5)
            provider.preview = "第一段答复。"
            with httpx.Client(
                base_url=base_url, timeout=3,
                cookies={SESSION_COOKIE: session.token},
            ) as client:
                with client.stream("GET", _url(kind, scope, turn_ref)) as response:
                    assert response.status_code == 200
                    events = (
                        json.loads(line[6:]) for line in response.iter_lines()
                        if line.startswith("data: ")
                    )
                    first = next(events)
                    assert first["text"] == "第一段答复。"
                    assert not producing.done()
                    provider.preview = "第一段答复。第二段答复。"
                    second = next(events)
                    assert second["text"] == provider.preview
                    assert not producing.done()
                    provider.finish.set()
                    terminal = next(events)
                    assert terminal == {
                        "turn_ref": turn_ref, "status": "completed",
                        "text": "最终完整回复。",
                    }
                    assert list(events) == []
            assert producing.result(timeout=5)
        finally:
            provider.finish.set()
            server.should_exit = True
            serving.result(timeout=5)
            listener.close()
