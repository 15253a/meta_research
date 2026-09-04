from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import meta_research.owners.research_memory as research_memory_module
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from test_public_human_collaboration_web import (
    _authenticated_client,
    _open_request,
    _runtime,
    _write_headers,
)
from test_root_human_request_lifecycle import (
    _bundle_root_channel,
    _initialized_child,
    _open_arguments,
    _runtime as _root_runtime,
    _structured,
    _tool_call,
)


def _linked_material(path: Path) -> dict[str, object]:
    return {"source_locator": str(path.resolve())}


def test_linked_local_response_uses_sync_for_small_and_response_first_for_large(
    tmp_path: Path,
    monkeypatch,
) -> None:
    small = tmp_path / "small-result.txt"
    small.write_text("small result\n", encoding="utf-8")
    large = tmp_path / "large-result"
    large.mkdir()
    (large / "one.txt").write_text("one\n", encoding="utf-8")
    (large / "two.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr(research_memory_module, "MAX_ASSET_FILES", 1)
    monkeypatch.setattr(research_memory_module, "ASSET_INTAKE_MAX_ATTEMPTS", 1)

    runtime = _runtime(tmp_path / "human-request-linked-local")
    owner = runtime.owners.agent_runtime
    small_request = _open_request(
        owner,
        request_kind="external_material_api_access",
        waiter_ref="small-linked-material",
        wait_scope="local",
        other_blockers=(),
        idempotency_key="small-linked-material-open",
    )
    large_request = _open_request(
        owner,
        request_kind="offline_action",
        waiter_ref="large-linked-material",
        wait_scope="local",
        other_blockers=(),
        idempotency_key="large-linked-material-open",
    )
    unknown = tmp_path / "not-mounted-yet"
    unknown_request = _open_request(
        owner,
        request_kind="external_material_api_access",
        waiter_ref="unknown-linked-material",
        wait_scope="local",
        other_blockers=(),
        idempotency_key="unknown-linked-material-open",
    )
    client, auth = _authenticated_client(runtime)
    try:
        with client:
            small_response = client.post(
                "/api/v1/human-requests/"
                f"{quote(small_request['request_ref'], safe='')}/responses",
                headers=_write_headers(auth, "small-linked-material-response"),
                json={
                    "decision": "provided",
                    "facts": {"result": "human accepted the request"},
                    "note": "Use this exact local file.",
                    "linked_local_material": _linked_material(small),
                },
            )
            assert small_response.status_code == 201, small_response.json()
            small_body = small_response.json()
            assert small_body["asset_intake"]["status"] == "accepted"
            small_facts = small_body["facts"]
            assert small_facts["material_path"] == str(small.resolve())
            assert small_facts["material_version_ref"] == (
                small_body["asset_intake"]["asset"]["version_ref"]
            )
            assert small_facts["result"] == "human accepted the request"

            large_response = client.post(
                "/api/v1/human-requests/"
                f"{quote(large_request['request_ref'], safe='')}/responses",
                headers=_write_headers(auth, "large-linked-material-response"),
                json={
                    "decision": "provided",
                    "facts": {"result": "continue even if intake later fails"},
                    "note": "The raw directory path is the immediate response.",
                    "linked_local_material": _linked_material(large),
                },
            )
            assert large_response.status_code == 201, large_response.json()
            large_body = large_response.json()
            assert large_body["asset_intake"]["status"] == "queued"
            assert large_body["facts"] == {
                "result": "continue even if intake later fails",
                "result_path": str(large.resolve()),
            }

            recorded = owner.query_human_request(large_request["request_ref"])
            assert recorded is not None
            assert recorded["responses"][-1]["facts"] == large_body["facts"]
            assert recorded["responses"][-1]["note"] == (
                "The raw directory path is the immediate response."
            )

            for child in large.iterdir():
                child.unlink()
            large.rmdir()
            assert runtime.owners.research_memory.process_asset_intake_once()
            failed = runtime.owners.research_memory.query_asset_intake(
                large_body["asset_intake"]["job_ref"]
            )
            assert failed.status == "failed"
            assert failed.failure_code == "asset_intake_retry_exhausted"
            still_recorded = owner.query_human_request(
                large_request["request_ref"]
            )
            assert still_recorded is not None
            assert still_recorded["responses"][-1]["response_ref"] == (
                large_body["response_ref"]
            )

            unknown_response = client.post(
                "/api/v1/human-requests/"
                f"{quote(unknown_request['request_ref'], safe='')}/responses",
                headers=_write_headers(auth, "unknown-linked-material-response"),
                json={
                    "decision": "provided",
                    "facts": {},
                    "note": "The mount is not currently inspectable.",
                    "linked_local_material": _linked_material(unknown),
                },
            )
            assert unknown_response.status_code == 201, unknown_response.json()
            unknown_body = unknown_response.json()
            assert unknown_body["asset_intake"]["status"] == "queued"
            assert unknown_body["facts"] == {
                "material_path": str(unknown.resolve()),
            }
            assert runtime.owners.research_memory.process_asset_intake_once()
            unknown_intake = runtime.owners.research_memory.query_asset_intake(
                unknown_body["asset_intake"]["job_ref"]
            )
            assert unknown_intake.status == "failed"
            unknown_recorded = owner.query_human_request(
                unknown_request["request_ref"]
            )
            assert unknown_recorded is not None
            assert unknown_recorded["responses"][-1]["response_ref"] == (
                unknown_body["response_ref"]
            )
    finally:
        client.close()
        runtime.close()


def test_large_async_linked_local_streams_manifest_without_old_sync_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "large-linked-directory"
    source.mkdir()
    (source / "one.txt").write_text("one\n", encoding="utf-8")
    (source / "two.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr(research_memory_module, "MAX_ASSET_FILES", 1)
    runtime = _runtime(tmp_path / "large-linked-streaming")
    memory = runtime.owners.research_memory
    try:
        assert memory.linked_local_intake_mode(str(source.resolve())) == (
            "asynchronous"
        )
        queued = memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                source_locator=str(source.resolve()),
                asynchronous=True,
            ),
            idempotency_key="large-linked-streaming",
        )
        assert queued.status == "queued"
        assert memory.process_asset_intake_once()
        accepted = memory.query_asset_intake(queued.job_ref)
        assert accepted.status == "accepted"
        assert accepted.failure_code is None
        assert accepted.asset is not None
        assert accepted.asset.byte_count == 8
        assert accepted.asset.custody_modes == ("linked_local",)
    finally:
        runtime.close()


def test_operation_bound_large_linked_response_replays_one_response_and_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "replayed-large-linked-directory"
    source.mkdir()
    (source / "one.txt").write_text("one\n", encoding="utf-8")
    (source / "two.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr(research_memory_module, "MAX_ASSET_FILES", 1)
    runtime = _root_runtime(tmp_path / "replayed-large-linked-response")
    try:
        _run, channel = _bundle_root_channel(runtime)
        client, auth = _authenticated_client(runtime)
        try:
            agent_headers = _initialized_child(client, channel.connection.token)
            opened = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(
                        "replayed-large-linked-response",
                        "external_material_api_access",
                    ),
                    request_id=10,
                )
            )
            request_ref = str(opened["request_ref"])
            url = (
                "/api/v1/human-requests/"
                f"{quote(request_ref, safe='')}/responses"
            )
            headers = _write_headers(auth, "replayed-large-linked-response")
            body = {
                "decision": "provided",
                "facts": {"message": "Use this exact large local directory."},
                "note": "The local result is ready.",
                "linked_local_material": _linked_material(source),
            }

            first = client.post(url, headers=headers, json=body)
            assert first.status_code == 201, first.json()
            replay = client.post(url, headers=headers, json=body)
            assert replay.status_code == 201, replay.json()
            assert replay.json() == first.json()

            current = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert current is not None
            assert current["status"] == "satisfied"
            assert current["responses"] == [
                {
                    key: value
                    for key, value in first.json().items()
                    if key != "asset_intake"
                }
            ]
            assert first.json()["asset_intake"]["job_ref"] is not None
            assert replay.json()["asset_intake"]["job_ref"] == first.json()[
                "asset_intake"
            ]["job_ref"]

            successor = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(
                        "successor-large-linked-response",
                        "external_material_api_access",
                        predecessor_request_ref=request_ref,
                    ),
                    request_id=11,
                )
            )
            assert successor["request_ref"] != request_ref
            stale_new_response = client.post(
                url,
                headers=_write_headers(auth, "stale-large-linked-response"),
                json=body,
            )
            assert stale_new_response.status_code == 409
            assert stale_new_response.json()["detail"]["code"] == (
                "human_request_not_current"
            )
        finally:
            client.close()
    finally:
        runtime.close()


def test_large_linked_response_survives_unexpected_intake_enqueue_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "enqueue-failure-large-linked-directory"
    source.mkdir()
    (source / "one.txt").write_text("one\n", encoding="utf-8")
    (source / "two.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr(research_memory_module, "MAX_ASSET_FILES", 1)
    runtime = _root_runtime(tmp_path / "enqueue-failure-linked-response")
    try:
        _run, channel = _bundle_root_channel(runtime)
        client, auth = _authenticated_client(runtime)
        try:
            agent_headers = _initialized_child(client, channel.connection.token)
            opened = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(
                        "enqueue-failure-linked-response",
                        "external_material_api_access",
                    ),
                    request_id=20,
                )
            )
            request_ref = str(opened["request_ref"])

            def fail_enqueue(*_args, **_kwargs):
                raise RuntimeError("simulated queue transport failure")

            submit_asset_intake = (
                runtime.owners.research_memory.submit_asset_intake
            )
            monkeypatch.setattr(
                runtime.owners.research_memory,
                "submit_asset_intake",
                fail_enqueue,
            )
            response = client.post(
                "/api/v1/human-requests/"
                f"{quote(request_ref, safe='')}/responses",
                headers=_write_headers(auth, "enqueue-failure-linked-response"),
                json={
                    "decision": "provided",
                    "facts": {"message": "Use the raw path now."},
                    "note": "Do not revoke this response if intake fails.",
                    "linked_local_material": _linked_material(source),
                },
            )
            assert response.status_code == 201, response.json()
            assert response.json()["asset_intake"] == {
                "job_ref": None,
                "status": "not_queued",
                "failure": {"code": "asset_intake_enqueue_unavailable"},
            }
            current = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert current is not None
            assert current["status"] == "satisfied"
            assert current["responses"][-1]["response_ref"] == response.json()[
                "response_ref"
            ]

            monkeypatch.setattr(
                runtime.owners.research_memory,
                "submit_asset_intake",
                submit_asset_intake,
            )
            replay = client.post(
                response.request.url.path,
                headers=_write_headers(auth, "enqueue-failure-linked-response"),
                json={
                    "decision": "provided",
                    "facts": {"message": "Use the raw path now."},
                    "note": "Do not revoke this response if intake fails.",
                    "linked_local_material": _linked_material(source),
                },
            )
            assert replay.status_code == 201, replay.json()
            assert replay.json()["response_ref"] == response.json()["response_ref"]
            assert replay.json()["asset_intake"]["status"] == "queued"
            assert replay.json()["asset_intake"]["job_ref"] is not None
        finally:
            client.close()
    finally:
        runtime.close()
