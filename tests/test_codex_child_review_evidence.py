from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import cast

import pytest

from meta_research.codex_child_review import (
    CodexChildReviewEvidenceError,
    SealedReviewOperationEvidence,
    TrustedChildReviewRequest,
    TrustedChildReviewVerifier,
)


ROOT_SESSION_REF = "01a04b6b-36e8-73a2-bc36-6996523d5fdb"
CHILD_SESSION_REF = "01a04b71-2c39-7cf3-b8d2-863b5742ed73"
ROOT_TURN_REF = "01a04b6e-e5ba-70d2-9fbb-ac9c0e87b846"
CHILD_TURN_REF = "01a04b71-2c66-7031-83e2-1c0ed94f8bd9"
WORKING_DIRECTORY = "/srv/meta-research/research-workspace"
TASK_NAME = "idea_review_bd8f646f"
TASK_PATH = f"/root/{TASK_NAME}"
SPAWN_CALL_REF = "call_spawn"
WAIT_CALL_REF = "call_wait"
PROMPT = "sealed review prompt"
SPAWN_MESSAGE = "review the frozen draft"
CHILD_RESULT = '{"findings":["bounded finding"]}'
OTHER_ROOT_SESSION_REF = "01a04b6b-ffff-73a2-bc36-6996523d5fdb"
OTHER_CHILD_SESSION_REF = "01a04b71-ffff-7cf3-b8d2-863b5742ed73"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _MemoryLedgerReader:
    def __init__(
        self, records: dict[str, tuple[dict[str, object], ...]]
    ) -> None:
        self.records = records

    def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
        try:
            return self.records[session_ref]
        except KeyError as error:
            raise OSError("ledger unavailable") from error


class _FailingLedgerReader:
    def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
        del session_ref
        raise OSError("session ledger missing or ambiguous")


@dataclass(frozen=True)
class _Fixture:
    reader: _MemoryLedgerReader
    request: TrustedChildReviewRequest


def _runtime_context(turn_ref: str) -> dict[str, object]:
    return {
        "type": "turn_context",
        "payload": {
            "turn_id": turn_ref,
            "cwd": WORKING_DIRECTORY,
            "model": "gpt-5.6-sol",
            "effort": "max",
            "multi_agent_version": "v2",
            "approval_policy": "never",
            "sandbox_policy": {"type": "danger-full-access"},
        },
    }


def _fixture() -> _Fixture:
    structured_result: dict[str, object] = {
        "reviewer_agent_ref": TASK_PATH,
        "findings": [{"finding_ref": "finding-1"}],
    }
    result_text = json.dumps(
        structured_result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    stdout = "\n".join(
        (
            json.dumps(
                {"type": "thread.started", "thread_id": ROOT_SESSION_REF}
            ),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "status": "completed",
                        "sender_thread_id": ROOT_SESSION_REF,
                        "receiver_thread_ids": [],
                        "agents_states": {},
                    },
                }
            ),
            json.dumps({"type": "turn.completed"}),
        )
    )
    invocation_hash = "1" * 64
    stdout_hash = _canonical_hash(stdout)
    result_hash = _canonical_hash(structured_result)
    exit_marker: dict[str, object] = {
        "schema_ref": "meta-research/codex-provider-exit/v1",
        "invocation_hash": invocation_hash,
        "returncode": 0,
        "provider_returncode": 0,
        "termination_reason": "completed",
        "prompt_hash": _canonical_hash(PROMPT),
        "stdout_hash": stdout_hash,
        "result_file_hash": hashlib.sha256(result_text.encode()).hexdigest(),
    }
    operation = SealedReviewOperationEvidence(
        invocation_hash=invocation_hash,
        prompt=PROMPT,
        stdout=stdout,
        result_text=result_text,
        result=structured_result,
        exit_marker=exit_marker,
        stdout_hash=stdout_hash,
        result_hash=result_hash,
        exit_hash=_canonical_hash(exit_marker),
    )
    root_records: tuple[dict[str, object], ...] = (
        {
            "type": "session_meta",
            "payload": {
                "id": ROOT_SESSION_REF,
                "session_id": ROOT_SESSION_REF,
                "cwd": WORKING_DIRECTORY,
                "originator": "codex_exec",
                "source": "exec",
                "thread_source": "user",
                "cli_version": "0.147.0",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "primary-turn"},
        },
        _runtime_context("primary-turn"),
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "primary result",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": ROOT_TURN_REF},
        },
        _runtime_context(ROOT_TURN_REF),
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": PROMPT}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": SPAWN_CALL_REF,
                "arguments": json.dumps(
                    {
                        "fork_turns": "none",
                        "message": SPAWN_MESSAGE,
                        "task_name": TASK_NAME,
                    }
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "kind": "started",
                "event_id": SPAWN_CALL_REF,
                "agent_path": TASK_PATH,
                "agent_thread_id": CHILD_SESSION_REF,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": SPAWN_CALL_REF,
                "output": json.dumps({"task_name": TASK_PATH}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": WAIT_CALL_REF,
                "arguments": json.dumps({"timeout_ms": 3_600_000}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": WAIT_CALL_REF,
                "output": json.dumps({"timed_out": False}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": TASK_PATH,
                "recipient": "/root",
                "content": (
                    "Message Type: FINAL_ANSWER\n"
                    f"Task name: {TASK_PATH}\n"
                    f"Payload:\n{CHILD_RESULT}"
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": result_text,
            },
        },
    )
    root_timestamps = (
        "2026-08-29T02:48:32.813Z",
        "2026-08-29T02:48:32.814Z",
        "2026-08-29T02:48:32.815Z",
        "2026-08-29T02:52:16.294Z",
        "2026-08-29T02:52:34.111Z",
        "2026-08-29T02:52:37.443Z",
        "2026-08-29T02:52:37.456Z",
        "2026-08-29T02:55:03.220Z",
        "2026-08-29T02:55:03.271Z",
        "2026-08-29T02:55:03.275Z",
        "2026-08-29T02:55:08.323Z",
        "2026-08-29T02:56:52.923Z",
        "2026-08-29T02:56:52.929Z",
        "2026-08-29T02:58:29.800Z",
    )
    root_records = tuple(
        {"timestamp": timestamp, **record}
        for timestamp, record in zip(root_timestamps, root_records, strict=True)
    )
    child_records: tuple[dict[str, object], ...] = (
        {
            "type": "session_meta",
            "payload": {
                "id": CHILD_SESSION_REF,
                "session_id": ROOT_SESSION_REF,
                "parent_thread_id": ROOT_SESSION_REF,
                "cwd": WORKING_DIRECTORY,
                "originator": "codex_exec",
                "thread_source": "subagent",
                "agent_path": TASK_PATH,
                "cli_version": "0.147.0",
                "multi_agent_version": "v2",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": ROOT_SESSION_REF,
                            "depth": 1,
                            "agent_path": TASK_PATH,
                        }
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": CHILD_TURN_REF},
        },
        _runtime_context(CHILD_TURN_REF),
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "/root",
                "recipient": TASK_PATH,
                "content": [
                    {"type": "input_text", "text": "review assignment"},
                    {
                        "type": "encrypted_content",
                        "encrypted_content": SPAWN_MESSAGE,
                    },
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": CHILD_RESULT,
            },
        },
    )
    child_timestamps = (
        "2026-08-29T02:55:03.273Z",
        "2026-08-29T02:55:03.274Z",
        "2026-08-29T02:55:06.367Z",
        "2026-08-29T02:55:06.368Z",
        "2026-08-29T02:56:52.880Z",
    )
    child_records = tuple(
        {"timestamp": timestamp, **record}
        for timestamp, record in zip(
            child_timestamps, child_records, strict=True
        )
    )
    reader = _MemoryLedgerReader(
        {ROOT_SESSION_REF: root_records, CHILD_SESSION_REF: child_records}
    )
    request = TrustedChildReviewRequest(
        root_session_ref=ROOT_SESSION_REF,
        expected_working_directory=WORKING_DIRECTORY,
        expected_cli_version="0.147.0",
        expected_model_ref="gpt-5.6-sol",
        expected_reasoning_effort="max",
        expected_sandbox_mode="danger-full-access",
        expected_multi_agent_version="v2",
        reviewer_agent_ref=TASK_PATH,
        structured_result=structured_result,
        operation=operation,
        expected_spawn_message=SPAWN_MESSAGE,
    )
    return _Fixture(reader=reader, request=request)


def _with_stdout(
    request: TrustedChildReviewRequest, stdout: str
) -> TrustedChildReviewRequest:
    operation = request.operation
    stdout_hash = _canonical_hash(stdout)
    exit_marker = {
        **operation.exit_marker,
        "stdout_hash": stdout_hash,
    }
    return replace(
        request,
        operation=replace(
            operation,
            stdout=stdout,
            stdout_hash=stdout_hash,
            exit_marker=exit_marker,
            exit_hash=_canonical_hash(exit_marker),
        ),
    )


def _payload(
    fixture: _Fixture, session_ref: str, index: int
) -> dict[str, object]:
    records = fixture.reader.records[session_ref]
    return cast(dict[str, object], records[index]["payload"])


def _with_result(
    request: TrustedChildReviewRequest, result: dict[str, object]
) -> TrustedChildReviewRequest:
    result_text = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    operation = request.operation
    exit_marker = {
        **operation.exit_marker,
        "result_file_hash": hashlib.sha256(result_text.encode()).hexdigest(),
    }
    return replace(
        request,
        structured_result=result,
        operation=replace(
            operation,
            result_text=result_text,
            result=result,
            result_hash=_canonical_hash(result),
            exit_marker=exit_marker,
            exit_hash=_canonical_hash(exit_marker),
        ),
    )


def _assert_rejected(
    fixture: _Fixture,
    code: str,
    *,
    request: TrustedChildReviewRequest | None = None,
) -> None:
    with pytest.raises(CodexChildReviewEvidenceError) as caught:
        TrustedChildReviewVerifier(fixture.reader).verify(
            request or fixture.request
        )
    assert caught.value.code == code
    assert str(caught.value) == code
    assert PROMPT not in str(caught.value)


def test_verifies_real_0147_shape_with_lossy_stdout() -> None:
    fixture = _fixture()

    proof = TrustedChildReviewVerifier(fixture.reader).verify(fixture.request)

    assert proof.root_turn_ref == ROOT_TURN_REF
    assert proof.reviewer_task_path == TASK_PATH
    assert proof.reviewer_native_session_ref == CHILD_SESSION_REF
    assert proof.stdout_hash == fixture.request.operation.stdout_hash
    assert proof.result_hash == fixture.request.operation.result_hash
    assert proof.exit_hash == fixture.request.operation.exit_hash
    expected_completion_hash = _canonical_hash(
        [
            fixture.reader.records[CHILD_SESSION_REF][4],
            fixture.reader.records[ROOT_SESSION_REF][11],
        ]
    )
    assert proof.completion_hash == expected_completion_hash
    assert all(
        len(value) == 64
        for value in (
            proof.spawn_hash,
            proof.completion_hash,
            proof.delivery_hash,
            proof.root_ledger_hash,
            proof.child_ledger_hash,
        )
    )


def test_allows_domain_result_to_differ_from_sealed_wire_result() -> None:
    fixture = _fixture()
    domain_result = {
        "reviewer_agent_ref": TASK_PATH,
        "domain_projection": {"accepted": True},
    }

    proof = TrustedChildReviewVerifier(fixture.reader).verify(
        replace(fixture.request, structured_result=domain_result)
    )

    assert proof.reviewer_task_path == TASK_PATH


def test_rejects_nested_stdout_collaboration_identity_mismatch() -> None:
    fixture = _fixture()
    events = [json.loads(line) for line in fixture.request.operation.stdout.splitlines()]
    events.insert(
        -1,
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "status": "completed",
                "sender_thread_id": ROOT_SESSION_REF,
                "receiver_thread_ids": [TASK_PATH],
                "agents_states": {
                    TASK_PATH: {
                        "status": "completed",
                        "agent_thread_id": "01a04b71-ffff-7cf3-b8d2-863b5742ed73",
                    }
                },
            },
        },
    )
    request = _with_stdout(
        fixture.request, "\n".join(json.dumps(event) for event in events)
    )

    with pytest.raises(CodexChildReviewEvidenceError) as caught:
        TrustedChildReviewVerifier(fixture.reader).verify(request)

    assert caught.value.code == "codex_child_review_stdout_identity_mismatch"
    assert str(caught.value) == caught.value.code
    assert PROMPT not in str(caught.value)


def test_rejects_wrong_root_reference_without_reader_detail() -> None:
    fixture = _fixture()
    request = replace(
        fixture.request, root_session_ref=OTHER_ROOT_SESSION_REF
    )

    _assert_rejected(
        fixture,
        "codex_child_review_root_ledger_invalid",
        request=request,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", OTHER_ROOT_SESSION_REF),
        ("cwd", "/wrong/workspace"),
    ),
)
def test_rejects_wrong_root_identity_or_working_directory(
    field: str, value: str
) -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 0)[field] = value

    _assert_rejected(fixture, "codex_child_review_root_runtime_invalid")


def test_rejects_duplicate_spawn_in_resumed_turn() -> None:
    fixture = _fixture()
    records = list(fixture.reader.records[ROOT_SESSION_REF])
    duplicate = deepcopy(records[7])
    duplicate["timestamp"] = records[7]["timestamp"]
    records.insert(8, duplicate)
    fixture.reader.records[ROOT_SESSION_REF] = tuple(records)

    _assert_rejected(fixture, "codex_child_review_spawn_invalid")


@pytest.mark.parametrize(
    ("record_index", "field", "value"),
    (
        (8, "agent_path", "/root/another_review"),
        (9, "output", json.dumps({"task_name": "/root/another_review"})),
    ),
)
def test_rejects_task_path_binding_mismatch(
    record_index: int, field: str, value: str
) -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, record_index)[field] = value

    _assert_rejected(fixture, "codex_child_review_ref_mismatch")


def test_rejects_activity_uuid_that_does_not_match_child_ledger() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 8)[
        "agent_thread_id"
    ] = OTHER_CHILD_SESSION_REF
    fixture.reader.records[OTHER_CHILD_SESSION_REF] = fixture.reader.records[
        CHILD_SESSION_REF
    ]

    _assert_rejected(fixture, "codex_child_review_lineage_invalid")


def test_rejects_wrong_child_parent_lineage() -> None:
    fixture = _fixture()
    metadata = _payload(fixture, CHILD_SESSION_REF, 0)
    metadata["parent_thread_id"] = OTHER_ROOT_SESSION_REF

    _assert_rejected(fixture, "codex_child_review_lineage_invalid")


def test_rejects_wrong_child_top_level_task_path() -> None:
    fixture = _fixture()
    _payload(fixture, CHILD_SESSION_REF, 0)[
        "agent_path"
    ] = "/root/another_review"

    _assert_rejected(fixture, "codex_child_review_lineage_invalid")


def test_rejects_wrong_child_metadata_runtime_version() -> None:
    fixture = _fixture()
    _payload(fixture, CHILD_SESSION_REF, 0)["multi_agent_version"] = "v1"

    _assert_rejected(fixture, "codex_child_review_child_runtime_invalid")


def test_rejects_missing_child_terminal() -> None:
    fixture = _fixture()
    fixture.reader.records[CHILD_SESSION_REF] = fixture.reader.records[
        CHILD_SESSION_REF
    ][:-1]

    _assert_rejected(fixture, "codex_child_review_terminal_invalid")


def test_rejects_final_wait_timeout() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 11)["output"] = json.dumps(
        {"timed_out": True}
    )

    _assert_rejected(fixture, "codex_child_review_wait_invalid")


def test_rejects_nonpositive_wait_timeout() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 10)["arguments"] = json.dumps(
        {"timeout_ms": 0}
    )

    _assert_rejected(fixture, "codex_child_review_wait_invalid")


def test_rejects_missing_child_delivery() -> None:
    fixture = _fixture()
    records = list(fixture.reader.records[ROOT_SESSION_REF])
    del records[12]
    fixture.reader.records[ROOT_SESSION_REF] = tuple(records)

    _assert_rejected(fixture, "codex_child_review_delivery_invalid")


def test_rejects_structured_result_reviewer_path_mismatch() -> None:
    fixture = _fixture()
    result = {
        **fixture.request.structured_result,
        "reviewer_agent_ref": "/root/another_review",
    }
    request = _with_result(fixture.request, result)
    _payload(fixture, ROOT_SESSION_REF, 13)[
        "last_agent_message"
    ] = request.operation.result_text

    _assert_rejected(
        fixture, "codex_child_review_ref_mismatch", request=request
    )


@pytest.mark.parametrize("hash_field", ("stdout_hash", "result_hash", "exit_hash"))
def test_rejects_completion_hash_mismatch(hash_field: str) -> None:
    fixture = _fixture()
    operation = replace(
        fixture.request.operation,
        **{hash_field: "2" * 64},
    )

    _assert_rejected(
        fixture,
        "codex_child_review_hash_mismatch",
        request=replace(fixture.request, operation=operation),
    )


def test_rejects_tampered_root_ledger_prompt() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 6)["content"] = [
        {"type": "input_text", "text": "tampered prompt"}
    ]

    _assert_rejected(fixture, "codex_child_review_turn_invalid")


def test_rejects_prompt_message_with_unsealed_extra_content() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 6)["content"] = [
        {"type": "input_text", "text": PROMPT},
        {"type": "image", "url": "sealed-out-of-band-content"},
    ]

    _assert_rejected(fixture, "codex_child_review_turn_invalid")


def test_rejects_ambiguous_ledger_reader_result_without_detail() -> None:
    fixture = _fixture()

    with pytest.raises(CodexChildReviewEvidenceError) as caught:
        TrustedChildReviewVerifier(_FailingLedgerReader()).verify(
            fixture.request
        )

    assert caught.value.code == "codex_child_review_root_ledger_invalid"
    assert str(caught.value) == caught.value.code


def test_rejects_delivery_before_final_wait_completion() -> None:
    fixture = _fixture()
    wait_output = deepcopy(_payload(fixture, ROOT_SESSION_REF, 11))
    delivery = deepcopy(_payload(fixture, ROOT_SESSION_REF, 12))
    _payload(fixture, ROOT_SESSION_REF, 11).clear()
    _payload(fixture, ROOT_SESSION_REF, 11).update(delivery)
    _payload(fixture, ROOT_SESSION_REF, 12).clear()
    _payload(fixture, ROOT_SESSION_REF, 12).update(wait_output)

    _assert_rejected(fixture, "codex_child_review_causal_order_invalid")


def test_rejects_runtime_with_interactive_approval_policy() -> None:
    fixture = _fixture()
    _payload(fixture, ROOT_SESSION_REF, 5)[
        "approval_policy"
    ] = "on-request"

    _assert_rejected(fixture, "codex_child_review_root_runtime_invalid")
