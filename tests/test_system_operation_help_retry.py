from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from test_public_human_collaboration_web import _authenticated_client, _write_headers
from test_public_writing_report import (
    _SelectivelyFailingWritingSkill,
    _admit_report,
    _confirm_direct_quest,
    _runtime,
)


class _SimulatedProcessExit(BaseException):
    pass


def test_writing_fallback_retry_revises_same_request_then_releases_exact_waiter(
    tmp_path: Path,
) -> None:
    provider = _SelectivelyFailingWritingSkill()
    runtime = _runtime(tmp_path / "system-operation-help", provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(
        runtime,
        quest["quest_ref"],
        "system-operation-help",
        title="永久失败的报告",
    )
    run_ref = admitted["run"]["run_ref"]
    failed_attempt_ref = admitted["run"]["attempt_ref"]
    failed_fence_ref = admitted["run"]["fence_ref"]
    assert runtime.writing.process_once()
    requests = runtime.owners.agent_runtime.query_human_requests(
        quest_ref=quest["quest_ref"]
    )
    request = next(item for item in requests if item["kind"] == "system_operation_help")
    assert request["kind"] == "system_operation_help"
    assert request["obligation"] == "writing_test_provider_blocked"
    assert request["target_assertion"]["run_ref"] == run_ref
    assert request["target_assertion"]["attempt_ref"] == failed_attempt_ref
    assert request["target_assertion"]["fence_ref"] == failed_fence_ref

    client, auth = _authenticated_client(runtime)
    try:
        with client:
            accepted_retry = client.post(
                "/api/v1/human-requests/"
                f"{quote(request['request_ref'], safe='')}/retry",
                headers=_write_headers(auth, "writing-system-help-retry-1"),
                json={},
            )
            assert accepted_retry.status_code == 200, accepted_retry.json()
            processing = accepted_retry.json()
            assert processing["retry"] == {"status": "processing"}
            assert processing["request_ref"] == request["request_ref"]
            assert processing["status"] == "open"
            assert processing["evaluation"] is None
            assert processing["disposition"] is None
            assert processing["direct_waiters"][0]["status"] == "blocked"
            assert processing["responses"][-1]["decision"] == "provided"
            assert processing["responses"][-1]["facts"] == {
                "action": "retry",
                "effect_id": request["target_assertion"]["effect_id"],
            }
            resumed = runtime.owners.agent_runtime.query_writing_report(run_ref)
            assert resumed is not None
            assert resumed.status == "active"
            assert resumed.attempt_ref != failed_attempt_ref
            assert resumed.fence_ref != failed_fence_ref

            replay = client.post(
                "/api/v1/human-requests/"
                f"{quote(request['request_ref'], safe='')}/retry",
                headers=_write_headers(auth, "writing-system-help-retry-1"),
                json={},
            )
            assert replay.status_code == 200, replay.json()
            assert replay.json() == processing

            assert runtime.writing.process_once()
            original = runtime.owners.agent_runtime.query_human_request(
                request["request_ref"]
            )
            assert original is not None
            assert original["status"] == "superseded"
            assert original["disposition"]["decision"] == "superseded"
            successor_ref = original["successor_request_ref"]
            revised = runtime.owners.agent_runtime.query_human_request(successor_ref)
            assert revised is not None
            assert revised["request_id"] == request["request_id"]
            assert revised["revision"] == 2
            assert revised["predecessor_request_ref"] == request["request_ref"]
            assert revised["status"] == "open"
            assert revised["target_assertion"]["effect_id"] == (
                request["target_assertion"]["effect_id"]
            )
            assert revised["target_assertion"]["attempt_ref"] == resumed.attempt_ref
            assert revised["target_assertion"]["fence_ref"] == resumed.fence_ref
            assert revised["target_assertion"]["failure_code"] == (
                "writing_test_provider_blocked"
            )
            assert revised["responses"] == []

            provider.fail_broken = False
            retried = client.post(
                "/api/v1/human-requests/"
                f"{quote(revised['request_ref'], safe='')}/retry",
                headers=_write_headers(auth, "writing-system-help-retry-2"),
                json={},
            )
            assert retried.status_code == 200, retried.json()
            assert retried.json()["retry"] == {"status": "processing"}
            assert retried.json()["status"] == "open"

            assert runtime.writing.process_once()
            succeeded = client.post(
                "/api/v1/human-requests/"
                f"{quote(revised['request_ref'], safe='')}/retry",
                headers=_write_headers(auth, "writing-system-help-retry-2"),
                json={},
            )
            assert succeeded.status_code == 200, succeeded.json()
            completed = succeeded.json()
            assert completed["retry"]["status"] == "succeeded"
            assert completed["request_id"] == request["request_id"]
            assert completed["revision"] == 2
            assert completed["status"] == "satisfied"
            assert completed["evaluation"]["decision"] == "satisfied"
            assert completed["disposition"]["decision"] == "satisfied"
            waiter = completed["direct_waiters"][0]
            assert waiter["status"] == "consumed"
            assert waiter["resume_validation"]["status"] == "released"
            assert waiter["resume_validation"]["consumption"]["work_ref"] == (
                run_ref
            )
            writing = runtime.owners.agent_runtime.query_writing_report(run_ref)
            assert writing is not None
            assert writing.status == "active"
            assert writing.attempt_ref != resumed.attempt_ref
            assert writing.fence_ref != resumed.fence_ref
            assert writing.checkpoint is not None

            completed_replay = client.post(
                "/api/v1/human-requests/"
                f"{quote(revised['request_ref'], safe='')}/retry",
                headers=_write_headers(auth, "writing-system-help-retry-2"),
                json={},
            )
            assert completed_replay.status_code == 200, completed_replay.json()
            assert completed_replay.json() == completed
    finally:
        client.close()
        runtime.close()


def test_writing_fallback_recovers_open_and_retry_crash_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "system-operation-help-restart"
    provider = _SelectivelyFailingWritingSkill()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    admitted = _admit_report(
        runtime,
        quest["quest_ref"],
        "system-operation-help-restart",
        title="永久失败的报告",
    )
    run_ref = admitted["run"]["run_ref"]
    failed_attempt_ref = admitted["run"]["attempt_ref"]
    failed_fence_ref = admitted["run"]["fence_ref"]
    monkeypatch.setattr(
        runtime.owners.agent_runtime,
        "fail_writing_report",
        lambda **_values: (_ for _ in ()).throw(_SimulatedProcessExit()),
    )
    with pytest.raises(_SimulatedProcessExit):
        runtime.writing.process_once()
    interrupted = runtime.owners.agent_runtime.query_writing_report(run_ref)
    assert interrupted is not None
    assert interrupted.status == "active"
    requests = runtime.owners.agent_runtime.query_human_requests(
        quest_ref=quest["quest_ref"]
    )
    assert len(requests) == 1
    request = requests[0]
    assert request["status"] == "open"
    assert request["responses"] == []
    assert request["target_assertion"]["attempt_ref"] == failed_attempt_ref
    assert request["target_assertion"]["fence_ref"] == failed_fence_ref
    runtime.close()

    restarted = _runtime(root, provider)
    restarted_before = restarted.owners.agent_runtime.query_writing_report(run_ref)
    assert restarted_before is not None
    assert restarted.writing.process_once()
    still_waiting = restarted.owners.agent_runtime.query_writing_report(run_ref)
    assert still_waiting is not None
    assert still_waiting.status == "active"
    assert still_waiting.attempt_ref == restarted_before.attempt_ref
    assert still_waiting.fence_ref == restarted_before.fence_ref
    assert still_waiting.checkpoint is None
    assert restarted.owners.agent_runtime.query_human_request(
        request["request_ref"]
    )["responses"] == []

    client, auth = _authenticated_client(restarted)
    retry_url = (
        "/api/v1/human-requests/"
        f"{quote(request['request_ref'], safe='')}/retry"
    )
    try:
        processing = client.post(
            retry_url,
            headers=_write_headers(auth, "writing-system-help-restart-retry-1"),
            json={},
        )
        assert processing.status_code == 200, processing.json()
        assert processing.json()["retry"] == {"status": "processing"}
        recorded = restarted.owners.agent_runtime.query_human_request(
            request["request_ref"]
        )
        assert recorded is not None
        assert len(recorded["responses"]) == 1
        assert recorded["responses"][0]["facts"]["action"] == "retry"
        assert recorded["status"] == "open"

        monkeypatch.setattr(
            restarted.owners.agent_runtime,
            "fail_writing_report",
            lambda **_values: (_ for _ in ()).throw(_SimulatedProcessExit()),
        )
        with pytest.raises(_SimulatedProcessExit):
            restarted.writing.process_once()
        superseded = restarted.owners.agent_runtime.query_human_request(
            request["request_ref"]
        )
        assert superseded is not None
        assert superseded["status"] == "superseded"
        revised = restarted.owners.agent_runtime.query_human_request(
            superseded["successor_request_ref"]
        )
        assert revised is not None
        assert revised["request_id"] == request["request_id"]
        assert revised["revision"] == 2
        assert revised["responses"] == []
    finally:
        client.close()
        restarted.close()

    provider.fail_broken = False
    recovered = _runtime(root, provider)
    try:
        recovered_before = recovered.owners.agent_runtime.query_writing_report(run_ref)
        assert recovered_before is not None
        assert recovered.writing.process_once()
        still_gated = recovered.owners.agent_runtime.query_writing_report(run_ref)
        assert still_gated is not None
        assert still_gated.status == "active"
        assert still_gated.attempt_ref == recovered_before.attempt_ref
        assert still_gated.fence_ref == recovered_before.fence_ref
        assert still_gated.checkpoint is None

        retry_client, retry_auth = _authenticated_client(recovered)
        revised_retry_url = (
            "/api/v1/human-requests/"
            f"{quote(revised['request_ref'], safe='')}/retry"
        )
        try:
            processing = retry_client.post(
                revised_retry_url,
                headers=_write_headers(
                    retry_auth, "writing-system-help-restart-retry-2"
                ),
                json={},
            )
            assert processing.status_code == 200, processing.json()
            assert processing.json()["retry"] == {"status": "processing"}
        finally:
            retry_client.close()
    finally:
        recovered.close()

    completed_runtime = _runtime(root, provider)
    try:
        assert completed_runtime.writing.process_once()
        completed = completed_runtime.owners.agent_runtime.query_human_request(
            revised["request_ref"]
        )
        assert completed is not None
        assert completed["status"] == "satisfied"
        assert completed["direct_waiters"][0]["status"] == "consumed"
        assert completed["direct_waiters"][0]["resume_validation"][
            "consumption"
        ]["work_ref"] == run_ref
        assert len(completed["responses"]) == 1

        replay_client, replay_auth = _authenticated_client(completed_runtime)
        try:
            replay = replay_client.post(
                revised_retry_url,
                headers=_write_headers(
                    replay_auth, "writing-system-help-restart-retry-2"
                ),
                json={},
            )
            assert replay.status_code == 200, replay.json()
            assert replay.json()["retry"] == {"status": "succeeded"}
            assert len(replay.json()["responses"]) == 1
        finally:
            replay_client.close()
    finally:
        completed_runtime.close()
