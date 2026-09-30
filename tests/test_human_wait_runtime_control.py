from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.web import create_app
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from test_root_human_request_lifecycle import (
    _bundle_root_channel,
    _initialized_child,
    _open_arguments,
    _runtime,
    _structured,
    _tool_call,
)


def _control(runtime, run, *, action: str, target_scope: str, key: str):
    owner = runtime.owners.agent_runtime
    managed = owner.query_managed_run(run.run_ref)
    assert managed is not None
    foreground = runtime.owners.advancement_engine.query_foreground(
        managed["quest_ref"]
    )
    assert foreground is not None
    target = {
        "quest_ref": managed["quest_ref"],
        "cycle_ref": foreground["cycle_ref"],
        "question_ref": foreground["question_ref"],
        "epoch": foreground["epoch"],
        "target_scope": target_scope,
    }
    if target_scope == "run":
        target["run_ref"] = run.run_ref
    human = runtime.owners.human_collaboration
    confirmed = _confirmed_control(
        human,
        scope_ref=f"quest:{managed['quest_ref']}",
        payload={"action": action, "target": target, "reason": "operator_requested"},
        key=key,
    )
    result = _execute_control(human, confirmed, key)
    assert result["executed"] is True
    assert result["control_execution"]["status"] == "completed"
    return owner.query_managed_run(run.run_ref)


@pytest.mark.parametrize("target_scope", ["cycle", "run"])
def test_pause_resume_preserves_exact_root_human_wait_until_retry(
    tmp_path: Path, target_scope: str
) -> None:
    runtime = _runtime(tmp_path / f"human-wait-control-{target_scope}")
    try:
        run, channel = _bundle_root_channel(runtime)
        owner = runtime.owners.agent_runtime
        with TestClient(
            create_app(runtime, base_url="http://testserver", control_key="control-key"),
            base_url="http://testserver",
        ) as client:
            agent_headers = _initialized_child(client, channel.connection.token)
            opened = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments("preserve-system-human-wait", "system_operation_help"),
                    request_id=30,
                )
            )
            request_ref = str(opened["request_ref"])
            binding = opened["operation_binding"]
            assert owner.query_managed_run(run.run_ref)["status"] == "suspended"

            paused = _control(
                runtime, run, action="pause", target_scope=target_scope, key="human-wait-pause"
            )
            assert paused["status"] == "suspended"
            resumed = _control(
                runtime, run, action="resume", target_scope=target_scope, key="human-wait-resume"
            )
            assert resumed["status"] == "suspended"
            assert resumed["terminal_reason"] == "human_request_wait"
            for name in ("attempt_ref", "root_session_ref", "fence_ref"):
                assert resumed[name] == binding[name]
            current = owner.query_human_request(request_ref)
            assert current["status"] == "open"
            assert current["responses"] == []
            assert current["evaluation"] is None
            assert current["direct_waiters"][0]["status"] == "blocked"

            bootstrap = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap", headers={"Origin": "http://testserver"},
                json={"token": bootstrap},
            )
            assert authenticated.status_code == 200
            retry = client.post(
                f"/api/v1/human-requests/{quote(request_ref, safe='')}/retry",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": authenticated.json()["csrf_token"],
                    "Idempotency-Key": "human-wait-retry",
                },
                json={},
            )
            assert retry.status_code == 200, retry.json()
            assert retry.json()["retry"] == {"status": "succeeded"}
            current = owner.query_human_request(request_ref)
            assert current["status"] == "satisfied"
            assert current["evaluation"]["response_refs"] == [
                current["responses"][0]["response_ref"]
            ]
            assert current["direct_waiters"][0]["status"] == "consumed"
            assert owner.query_managed_run(run.run_ref)["status"] == "running"

            # A consumed historical waiter must not hold a later ordinary resume.
            _control(runtime, run, action="pause", target_scope=target_scope, key="post-wait-pause")
            ordinary = _control(
                runtime, run, action="resume", target_scope=target_scope, key="post-wait-resume"
            )
            assert ordinary["status"] == "running"
            assert ordinary["terminal_reason"] is None
    finally:
        runtime.close()
