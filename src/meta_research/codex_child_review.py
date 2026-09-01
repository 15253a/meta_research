from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast


_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_TASK_PATH_PATTERN = re.compile(r"/root/([a-z][a-z0-9_]{0,96})")
_ENCRYPTED_TASK_PATTERN = re.compile(r"gAAAAA[A-Za-z0-9_-]{80,}={0,2}")


class CodexChildReviewEvidenceError(RuntimeError):
    """A trusted child-review proof could not be established."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SealedReviewOperationEvidence:
    invocation_hash: str
    prompt: str
    stdout: str
    result_text: str
    result: dict[str, object]
    exit_marker: dict[str, object]
    stdout_hash: str
    result_hash: str
    exit_hash: str


@dataclass(frozen=True, slots=True)
class TrustedChildReviewRequest:
    root_session_ref: str
    expected_working_directory: str
    expected_cli_version: str
    expected_model_ref: str
    expected_reasoning_effort: str
    expected_sandbox_mode: str
    expected_multi_agent_version: str
    reviewer_agent_ref: str
    structured_result: dict[str, object]
    operation: SealedReviewOperationEvidence
    expected_spawn_message: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedChildReviewProof:
    root_turn_ref: str
    reviewer_task_path: str
    reviewer_native_session_ref: str
    spawn_hash: str
    completion_hash: str
    delivery_hash: str
    root_ledger_hash: str
    child_ledger_hash: str
    stdout_hash: str
    result_hash: str
    exit_hash: str


class _SessionLedgerReader(Protocol):
    def read(self, session_ref: str) -> tuple[dict[str, object], ...]: ...


@dataclass(frozen=True, slots=True)
class _RootTurn:
    turn_ref: str
    start: int
    end: int
    prompt_index: int
    completion_index: int
    context: dict[str, object]


@dataclass(frozen=True, slots=True)
class _SpawnProof:
    call_index: int
    activity_index: int
    output_index: int
    task_path: str
    native_session_ref: str
    message: str
    evidence_hash: str


@dataclass(frozen=True, slots=True)
class _ChildProof:
    task_index: int
    terminal_index: int
    terminal_text: str


@dataclass(frozen=True, slots=True)
class _WaitProof:
    first_call_index: int
    final_output_index: int


@dataclass(frozen=True, slots=True)
class _DeliveryProof:
    index: int
    evidence_hash: str


class TrustedChildReviewVerifier:
    """Verify one resumed root turn against trusted root and child ledgers."""

    def __init__(self, reader: _SessionLedgerReader) -> None:
        self._reader = reader

    def verify(
        self, request: TrustedChildReviewRequest
    ) -> VerifiedChildReviewProof:
        _validate_request(request)
        _verify_operation(request.operation)

        root_records = self._read_ledger(
            request.root_session_ref,
            code="codex_child_review_root_ledger_invalid",
        )
        root_metadata = _one_session_metadata(
            root_records,
            code="codex_child_review_root_ledger_invalid",
        )
        _verify_root_metadata(root_metadata, request)
        root_turn = _locate_root_turn(root_records, request.operation)
        _verify_runtime_context(
            root_turn.context,
            request,
            code="codex_child_review_root_runtime_invalid",
        )

        spawn = _verify_spawn(root_records, root_turn, request)
        child_records = self._read_ledger(
            spawn.native_session_ref,
            code="codex_child_review_child_ledger_invalid",
        )
        child = _verify_child(
            child_records,
            native_session_ref=spawn.native_session_ref,
            task_path=spawn.task_path,
            spawn_message=spawn.message,
            request=request,
        )
        wait = _verify_wait(root_records, root_turn)
        delivery = _verify_delivery(
            root_records,
            root_turn,
            task_path=spawn.task_path,
            child_terminal=child.terminal_text,
        )
        _verify_causal_order(
            root_records,
            root_turn=root_turn,
            spawn=spawn,
            child_records=child_records,
            child=child,
            wait=wait,
            delivery=delivery,
        )
        _verify_stdout_identities(
            request.operation.stdout,
            root_session_ref=request.root_session_ref,
            task_path=spawn.task_path,
            native_session_ref=spawn.native_session_ref,
            expected_spawn_message=request.expected_spawn_message,
        )

        return VerifiedChildReviewProof(
            root_turn_ref=root_turn.turn_ref,
            reviewer_task_path=spawn.task_path,
            reviewer_native_session_ref=spawn.native_session_ref,
            spawn_hash=spawn.evidence_hash,
            completion_hash=_canonical_hash(
                [
                    child_records[child.terminal_index],
                    root_records[wait.final_output_index],
                ]
            ),
            delivery_hash=delivery.evidence_hash,
            root_ledger_hash=_canonical_hash(root_records),
            child_ledger_hash=_canonical_hash(child_records),
            stdout_hash=request.operation.stdout_hash,
            result_hash=request.operation.result_hash,
            exit_hash=request.operation.exit_hash,
        )

    def _read_ledger(
        self, session_ref: str, *, code: str
    ) -> tuple[dict[str, object], ...]:
        try:
            records = self._reader.read(session_ref)
        except (OSError, TypeError, ValueError):
            raise CodexChildReviewEvidenceError(code) from None
        if (
            not isinstance(records, tuple)
            or not records
            or any(not isinstance(record, dict) for record in records)
        ):
            raise CodexChildReviewEvidenceError(code)
        previous: datetime | None = None
        for record in records:
            current = _timestamp(record, code=code)
            if previous is not None and current < previous:
                raise CodexChildReviewEvidenceError(code)
            previous = current
        try:
            _canonical_hash(records)
        except (TypeError, ValueError):
            raise CodexChildReviewEvidenceError(code) from None
        return records


def _validate_request(request: TrustedChildReviewRequest) -> None:
    expected_strings = (
        request.root_session_ref,
        request.expected_working_directory,
        request.expected_cli_version,
        request.expected_model_ref,
        request.expected_reasoning_effort,
        request.expected_sandbox_mode,
        request.expected_multi_agent_version,
        request.reviewer_agent_ref,
    )
    if (
        any(not isinstance(value, str) or not value for value in expected_strings)
        or not _is_uuid(request.root_session_ref)
        or _TASK_PATH_PATTERN.fullmatch(request.reviewer_agent_ref) is None
        or not isinstance(request.structured_result, dict)
        or (
            request.expected_spawn_message is not None
            and (
                not isinstance(request.expected_spawn_message, str)
                or not request.expected_spawn_message
            )
        )
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_request_invalid")


def _verify_operation(operation: SealedReviewOperationEvidence) -> None:
    if (
        not _valid_digest(operation.invocation_hash)
        or not isinstance(operation.prompt, str)
        or not operation.prompt
        or not isinstance(operation.stdout, str)
        or not isinstance(operation.result_text, str)
        or not operation.result_text
        or not isinstance(operation.result, dict)
        or not isinstance(operation.exit_marker, dict)
        or not _valid_digest(operation.stdout_hash)
        or not _valid_digest(operation.result_hash)
        or not _valid_digest(operation.exit_hash)
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_operation_invalid"
        )
    try:
        decoded_result = json.loads(operation.result_text)
        computed_stdout_hash = _canonical_hash(operation.stdout)
        computed_result_hash = _canonical_hash(operation.result)
        computed_exit_hash = _canonical_hash(operation.exit_marker)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_operation_invalid"
        ) from None
    if (
        not isinstance(decoded_result, dict)
        or decoded_result != operation.result
        or operation.stdout_hash != computed_stdout_hash
        or operation.result_hash != computed_result_hash
        or operation.exit_hash != computed_exit_hash
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_hash_mismatch")

    marker = operation.exit_marker
    result_file_hash = hashlib.sha256(
        operation.result_text.encode("utf-8")
    ).hexdigest()
    if (
        marker.get("schema_ref") != "meta-research/codex-provider-exit/v1"
        or marker.get("invocation_hash") != operation.invocation_hash
        or type(marker.get("returncode")) is not int
        or marker.get("returncode") != 0
        or type(marker.get("provider_returncode")) is not int
        or marker.get("provider_returncode") != 0
        or marker.get("termination_reason") != "completed"
        or marker.get("prompt_hash") != _canonical_hash(operation.prompt)
        or marker.get("stdout_hash") != operation.stdout_hash
        or marker.get("result_file_hash") != result_file_hash
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_operation_invalid"
        )


def _one_session_metadata(
    records: tuple[dict[str, object], ...], *, code: str
) -> dict[str, object]:
    values = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta"
        and isinstance(record.get("payload"), dict)
    ]
    if len(values) != 1:
        raise CodexChildReviewEvidenceError(code)
    return cast(dict[str, object], values[0])


def _verify_root_metadata(
    metadata: dict[str, object], request: TrustedChildReviewRequest
) -> None:
    if (
        metadata.get("id") != request.root_session_ref
        or metadata.get("session_id") != request.root_session_ref
        or metadata.get("cwd") != request.expected_working_directory
        or metadata.get("originator") != "codex_exec"
        or metadata.get("source") != "exec"
        or metadata.get("thread_source") != "user"
        or metadata.get("cli_version") != request.expected_cli_version
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_root_runtime_invalid"
        )


def _locate_root_turn(
    records: tuple[dict[str, object], ...],
    operation: SealedReviewOperationEvidence,
) -> _RootTurn:
    prompt_indices = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "response_item"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and _message_text(payload.get("content")) == operation.prompt
    ]
    completion_indices = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_complete"
        and payload.get("last_agent_message") == operation.result_text
    ]
    if len(prompt_indices) != 1 or len(completion_indices) != 1:
        raise CodexChildReviewEvidenceError("codex_child_review_turn_invalid")
    prompt_index = prompt_indices[0]
    completion_index = completion_indices[0]
    if prompt_index >= completion_index:
        raise CodexChildReviewEvidenceError("codex_child_review_turn_invalid")

    starts = [
        (index, payload.get("turn_id"))
        for index, record in enumerate(records)
        if index <= prompt_index
        and record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_started"
        and isinstance(payload.get("turn_id"), str)
        and payload.get("turn_id")
    ]
    if not starts:
        raise CodexChildReviewEvidenceError("codex_child_review_turn_invalid")
    start, turn_ref_value = starts[-1]
    turn_ref = cast(str, turn_ref_value)
    following_starts = [
        index
        for index, record in enumerate(records)
        if index > start
        and record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_started"
    ]
    end = following_starts[0] if following_starts else len(records)
    prior_terminals = [
        record
        for record in records[:start]
        if record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_complete"
    ]
    terminals = [
        index
        for index, record in enumerate(records[start:end], start=start)
        if record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_complete"
    ]
    contexts = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(records[start:end], start=start)
        if record.get("type") == "turn_context"
        and isinstance(record.get("payload"), dict)
    ]
    context_index = contexts[0][0] if len(contexts) == 1 else -1
    context = contexts[0][1] if len(contexts) == 1 else {}
    if (
        not prior_terminals
        or not (start < prompt_index < completion_index < end)
        or not (start < context_index < prompt_index)
        or terminals != [completion_index]
        or len(contexts) != 1
        or context.get("turn_id") != turn_ref
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_turn_invalid")
    return _RootTurn(
        turn_ref=turn_ref,
        start=start,
        end=end,
        prompt_index=prompt_index,
        completion_index=completion_index,
        context=context,
    )


def _verify_runtime_context(
    context: dict[str, object],
    request: TrustedChildReviewRequest,
    *,
    code: str,
) -> None:
    sandbox = context.get("sandbox_policy")
    if (
        context.get("cwd") != request.expected_working_directory
        or context.get("model") != request.expected_model_ref
        or context.get("effort") != request.expected_reasoning_effort
        or context.get("approval_policy") != "never"
        or context.get("multi_agent_version")
        != request.expected_multi_agent_version
        or not isinstance(sandbox, dict)
        or sandbox.get("type") != request.expected_sandbox_mode
    ):
        raise CodexChildReviewEvidenceError(code)


def _verify_spawn(
    records: tuple[dict[str, object], ...],
    turn: _RootTurn,
    request: TrustedChildReviewRequest,
) -> _SpawnProof:
    spawn_calls = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(
            records[turn.start : turn.end], start=turn.start
        )
        if record.get("type") == "response_item"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "function_call"
        and cast(dict[str, object], record["payload"]).get("name")
        == "spawn_agent"
    ]
    if len(spawn_calls) != 1:
        raise CodexChildReviewEvidenceError("codex_child_review_spawn_invalid")
    call_index, call = spawn_calls[0]
    call_ref = call.get("call_id")
    arguments = _json_object(
        call.get("arguments"), code="codex_child_review_spawn_invalid"
    )
    task_name = arguments.get("task_name")
    message = arguments.get("message")
    if (
        not isinstance(call_ref, str)
        or not call_ref
        or arguments.get("fork_turns") != "none"
        or not isinstance(task_name, str)
        or not isinstance(message, str)
        or not message
        or _TASK_PATH_PATTERN.fullmatch(f"/root/{task_name}") is None
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_spawn_invalid")
    if not _spawn_message_matches_expected(message, request):
        raise CodexChildReviewEvidenceError("codex_child_review_task_mismatch")
    task_path = f"/root/{task_name}"

    activities = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(
            records[turn.start : turn.end], start=turn.start
        )
        if record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "sub_agent_activity"
        and cast(dict[str, object], record["payload"]).get("kind")
        == "started"
    ]
    if len(activities) != 1:
        raise CodexChildReviewEvidenceError("codex_child_review_spawn_invalid")
    activity_index, activity = activities[0]
    native_session_ref = activity.get("agent_thread_id")
    if (
        activity.get("event_id") != call_ref
        or activity.get("agent_path") != task_path
        or not isinstance(native_session_ref, str)
        or not _is_uuid(native_session_ref)
        or native_session_ref == request.root_session_ref
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_ref_mismatch")

    outputs = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(
            records[turn.start : turn.end], start=turn.start
        )
        if record.get("type") == "response_item"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "function_call_output"
        and cast(dict[str, object], record["payload"]).get("call_id")
        == call_ref
    ]
    if len(outputs) != 1:
        raise CodexChildReviewEvidenceError("codex_child_review_spawn_invalid")
    output_index, output = outputs[0]
    decoded_output = _json_object(
        output.get("output"), code="codex_child_review_spawn_invalid"
    )
    if (
        set(decoded_output) != {"task_name"}
        or decoded_output.get("task_name") != task_path
        or request.reviewer_agent_ref != task_path
        or request.structured_result.get("reviewer_agent_ref") != task_path
        or request.operation.result.get("reviewer_agent_ref") != task_path
        or not (
            turn.prompt_index < call_index < activity_index < output_index
        )
    ):
        raise CodexChildReviewEvidenceError("codex_child_review_ref_mismatch")
    return _SpawnProof(
        call_index=call_index,
        activity_index=activity_index,
        output_index=output_index,
        task_path=task_path,
        native_session_ref=cast(str, native_session_ref),
        message=message,
        evidence_hash=_canonical_hash(
            [records[call_index], records[activity_index], records[output_index]]
        ),
    )


def _verify_child(
    records: tuple[dict[str, object], ...],
    *,
    native_session_ref: str,
    task_path: str,
    spawn_message: str,
    request: TrustedChildReviewRequest,
) -> _ChildProof:
    metadata = _one_session_metadata(
        records, code="codex_child_review_child_ledger_invalid"
    )
    source = metadata.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if (
        metadata.get("id") != native_session_ref
        or metadata.get("session_id") != request.root_session_ref
        or metadata.get("parent_thread_id") != request.root_session_ref
        or metadata.get("agent_path") != task_path
        or not isinstance(spawn, dict)
        or spawn.get("parent_thread_id") != request.root_session_ref
        or spawn.get("depth") != 1
        or spawn.get("agent_path") != task_path
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_lineage_invalid"
        )
    if (
        metadata.get("cwd") != request.expected_working_directory
        or metadata.get("originator") != "codex_exec"
        or metadata.get("thread_source") != "subagent"
        or metadata.get("cli_version") != request.expected_cli_version
        or metadata.get("multi_agent_version")
        != request.expected_multi_agent_version
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_child_runtime_invalid"
        )

    starts = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(records)
        if record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "task_started"
    ]
    contexts = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(records)
        if record.get("type") == "turn_context"
        and isinstance(record.get("payload"), dict)
    ]
    terminals = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(records)
        if record.get("type") == "event_msg"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "task_complete"
    ]
    received_tasks = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(records)
        if record.get("type") == "response_item"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "agent_message"
        and cast(dict[str, object], record["payload"]).get("author")
        == "/root"
        and cast(dict[str, object], record["payload"]).get("recipient")
        == task_path
    ]
    if (
        len(starts) != 1
        or len(contexts) != 1
        or len(terminals) != 1
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_terminal_invalid"
        )
    if len(received_tasks) != 1:
        raise CodexChildReviewEvidenceError(
            "codex_child_review_task_mismatch"
        )
    start_index, started = starts[0]
    context_index, context = contexts[0]
    terminal_index, terminal = terminals[0]
    task_index, received_task = received_tasks[0]
    terminal_text = terminal.get("last_agent_message")
    if (
        not isinstance(terminal_text, str)
        or not terminal_text
        or started.get("turn_id") != context.get("turn_id")
        or not (start_index < context_index < terminal_index)
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_terminal_invalid"
        )
    if (
        _encrypted_task_message(received_task.get("content"))
        != spawn_message
        or not (context_index < task_index < terminal_index)
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_task_mismatch"
        )
    _verify_runtime_context(
        context,
        request,
        code="codex_child_review_child_runtime_invalid",
    )
    return _ChildProof(
        task_index=task_index,
        terminal_index=terminal_index,
        terminal_text=terminal_text,
    )


def _verify_wait(
    records: tuple[dict[str, object], ...], turn: _RootTurn
) -> _WaitProof:
    calls = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(
            records[turn.start : turn.end], start=turn.start
        )
        if record.get("type") == "response_item"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "function_call"
        and cast(dict[str, object], record["payload"]).get("name")
        == "wait_agent"
    ]
    if not calls:
        raise CodexChildReviewEvidenceError("codex_child_review_wait_invalid")
    seen_call_refs: set[str] = set()
    pairs: list[tuple[int, int, bool]] = []
    previous_output_index = -1
    for call_index, call in calls:
        call_ref = call.get("call_id")
        if (
            not isinstance(call_ref, str)
            or not call_ref
            or call_ref in seen_call_refs
            or call_index <= previous_output_index
        ):
            raise CodexChildReviewEvidenceError(
                "codex_child_review_wait_invalid"
            )
        seen_call_refs.add(call_ref)
        arguments = _json_object(
            call.get("arguments"), code="codex_child_review_wait_invalid"
        )
        timeout_ms = arguments.get("timeout_ms")
        if type(timeout_ms) is not int or cast(int, timeout_ms) <= 0:
            raise CodexChildReviewEvidenceError(
                "codex_child_review_wait_invalid"
            )
        outputs = [
            (index, cast(dict[str, object], record["payload"]))
            for index, record in enumerate(
                records[turn.start : turn.end], start=turn.start
            )
            if record.get("type") == "response_item"
            and isinstance(record.get("payload"), dict)
            and cast(dict[str, object], record["payload"]).get("type")
            == "function_call_output"
            and cast(dict[str, object], record["payload"]).get("call_id")
            == call_ref
        ]
        if len(outputs) != 1:
            raise CodexChildReviewEvidenceError(
                "codex_child_review_wait_invalid"
            )
        output_index, output = outputs[0]
        decoded = _json_object(
            output.get("output"), code="codex_child_review_wait_invalid"
        )
        timed_out = decoded.get("timed_out")
        if type(timed_out) is not bool or output_index <= call_index:
            raise CodexChildReviewEvidenceError(
                "codex_child_review_wait_invalid"
            )
        pairs.append((call_index, output_index, cast(bool, timed_out)))
        previous_output_index = output_index
    if pairs[-1][2]:
        raise CodexChildReviewEvidenceError("codex_child_review_wait_invalid")
    return _WaitProof(
        first_call_index=pairs[0][0], final_output_index=pairs[-1][1]
    )


def _verify_delivery(
    records: tuple[dict[str, object], ...],
    turn: _RootTurn,
    *,
    task_path: str,
    child_terminal: str,
) -> _DeliveryProof:
    incoming = [
        (index, cast(dict[str, object], record["payload"]))
        for index, record in enumerate(
            records[turn.start : turn.end], start=turn.start
        )
        if record.get("type") == "response_item"
        and isinstance(record.get("payload"), dict)
        and cast(dict[str, object], record["payload"]).get("type")
        == "agent_message"
        and cast(dict[str, object], record["payload"]).get("recipient")
        == "/root"
    ]
    if len(incoming) != 1:
        raise CodexChildReviewEvidenceError(
            "codex_child_review_delivery_invalid"
        )
    index, payload = incoming[0]
    delivered_text = _message_text(payload.get("content"))
    if (
        payload.get("author") != task_path
        or delivered_text is None
        or not delivered_text.endswith(child_terminal)
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_delivery_invalid"
        )
    return _DeliveryProof(
        index=index, evidence_hash=_canonical_hash(records[index])
    )


def _verify_causal_order(
    root_records: tuple[dict[str, object], ...],
    *,
    root_turn: _RootTurn,
    spawn: _SpawnProof,
    child_records: tuple[dict[str, object], ...],
    child: _ChildProof,
    wait: _WaitProof,
    delivery: _DeliveryProof,
) -> None:
    if not (
        root_turn.prompt_index
        < spawn.call_index
        < spawn.activity_index
        < spawn.output_index
        < wait.first_call_index
        <= wait.final_output_index
        < delivery.index
        < root_turn.completion_index
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_causal_order_invalid"
        )
    root_spawn_time = _timestamp(
        root_records[spawn.activity_index],
        code="codex_child_review_causal_order_invalid",
    )
    child_start_time = _timestamp(
        child_records[0], code="codex_child_review_causal_order_invalid"
    )
    child_task_time = _timestamp(
        child_records[child.task_index],
        code="codex_child_review_causal_order_invalid",
    )
    child_completion_time = _timestamp(
        child_records[child.terminal_index],
        code="codex_child_review_causal_order_invalid",
    )
    wait_completion_time = _timestamp(
        root_records[wait.final_output_index],
        code="codex_child_review_causal_order_invalid",
    )
    delivery_time = _timestamp(
        root_records[delivery.index],
        code="codex_child_review_causal_order_invalid",
    )
    root_completion_time = _timestamp(
        root_records[root_turn.completion_index],
        code="codex_child_review_causal_order_invalid",
    )
    if not (
        root_spawn_time
        <= child_start_time
        <= child_task_time
        <= child_completion_time
        <= wait_completion_time
        <= delivery_time
        <= root_completion_time
    ):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_causal_order_invalid"
        )


def _verify_stdout_identities(
    stdout: str,
    *,
    root_session_ref: str,
    task_path: str,
    native_session_ref: str,
    expected_spawn_message: str | None,
) -> None:
    events: list[dict[str, object]] = []
    try:
        for line in stdout.splitlines():
            if not line:
                raise ValueError("empty JSONL record")
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("JSONL record is not an object")
            events.append(cast(dict[str, object], event))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise CodexChildReviewEvidenceError(
            "codex_child_review_stdout_invalid"
        ) from None
    root_refs = [
        event.get("thread_id")
        for event in events
        if event.get("type") == "thread.started"
    ]
    if root_refs != [root_session_ref]:
        raise CodexChildReviewEvidenceError(
            "codex_child_review_stdout_identity_mismatch"
        )
    allowed_child_refs = {task_path, native_session_ref}
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            continue
        sender = item.get("sender_thread_id")
        if sender is not None and sender != root_session_ref:
            raise CodexChildReviewEvidenceError(
                "codex_child_review_stdout_identity_mismatch"
            )
        receivers = item.get("receiver_thread_ids")
        states = item.get("agents_states")
        if receivers is not None:
            if (
                not isinstance(receivers, list)
                or any(
                    not isinstance(value, str)
                    or value not in allowed_child_refs
                    for value in receivers
                )
                or len(receivers) != len(set(cast(list[str], receivers)))
            ):
                raise CodexChildReviewEvidenceError(
                    "codex_child_review_stdout_identity_mismatch"
                )
        if states is not None:
            if (
                not isinstance(states, dict)
                or any(key not in allowed_child_refs for key in states)
                or any(
                    not isinstance(state, dict)
                    or not _collaboration_state_identities_match(
                        state,
                        root_session_ref=root_session_ref,
                        task_path=task_path,
                        native_session_ref=native_session_ref,
                    )
                    for state in states.values()
                )
                or (
                    isinstance(receivers, list)
                    and set(states) != set(receivers)
                )
            ):
                raise CodexChildReviewEvidenceError(
                    "codex_child_review_stdout_identity_mismatch"
                )
        prompt = item.get("prompt")
        if (
            item.get("tool") == "spawn_agent"
            and expected_spawn_message is not None
            and prompt is not None
            and prompt != expected_spawn_message
            and not _is_encrypted_task_message(prompt)
        ):
            raise CodexChildReviewEvidenceError(
                "codex_child_review_stdout_identity_mismatch"
            )


def _collaboration_state_identities_match(
    value: object,
    *,
    root_session_ref: str,
    task_path: str,
    native_session_ref: str,
) -> bool:
    if isinstance(value, list):
        return all(
            _collaboration_state_identities_match(
                item,
                root_session_ref=root_session_ref,
                task_path=task_path,
                native_session_ref=native_session_ref,
            )
            for item in value
        )
    if not isinstance(value, dict):
        return True
    child_identity_fields = {
        "agent_thread_id",
        "child_thread_id",
        "receiver_thread_id",
        "thread_id",
        "agent_path",
        "task_path",
    }
    root_identity_fields = {
        "sender_thread_id",
        "parent_thread_id",
        "root_thread_id",
    }
    for key, item in value.items():
        if key in child_identity_fields and item not in {
            task_path,
            native_session_ref,
        }:
            return False
        if key == "task_name" and item not in {
            task_path,
            task_path.removeprefix("/root/"),
        }:
            return False
        if key in root_identity_fields and item != root_session_ref:
            return False
        if not _collaboration_state_identities_match(
            item,
            root_session_ref=root_session_ref,
            task_path=task_path,
            native_session_ref=native_session_ref,
        ):
            return False
    return True


def _json_object(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise CodexChildReviewEvidenceError(code)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise CodexChildReviewEvidenceError(code) from None
    if not isinstance(decoded, dict):
        raise CodexChildReviewEvidenceError(code)
    return cast(dict[str, object], decoded)


def _message_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    if not value:
        return None
    parts: list[str] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or item.get("type") not in {"input_text", "text", "output_text"}
            or not isinstance(item.get("text"), str)
        ):
            return None
        parts.append(cast(str, item["text"]))
    return "".join(parts)


def _encrypted_task_message(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    encrypted = [
        item.get("encrypted_content")
        for item in value
        if isinstance(item, dict)
        and item.get("type") == "encrypted_content"
        and isinstance(item.get("encrypted_content"), str)
    ]
    if len(encrypted) != 1 or not encrypted[0]:
        return None
    return cast(str, encrypted[0])


def _spawn_message_matches_expected(
    message: str, request: TrustedChildReviewRequest
) -> bool:
    expected = request.expected_spawn_message
    if expected is None or message == expected:
        return True
    if not _is_encrypted_task_message(message):
        return False
    encoded = json.dumps(
        expected,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return request.operation.prompt.count(f"child_task={encoded}") == 1


def _is_encrypted_task_message(value: object) -> bool:
    return (
        isinstance(value, str)
        and _ENCRYPTED_TASK_PATTERN.fullmatch(value) is not None
    )


def _timestamp(record: dict[str, object], *, code: str) -> datetime:
    value = record.get("timestamp")
    if not isinstance(value, str) or not value:
        raise CodexChildReviewEvidenceError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CodexChildReviewEvidenceError(code) from None
    if parsed.tzinfo is None:
        raise CodexChildReviewEvidenceError(code)
    return parsed


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (AttributeError, ValueError):
        return False


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CodexChildReviewEvidenceError",
    "SealedReviewOperationEvidence",
    "TrustedChildReviewRequest",
    "TrustedChildReviewVerifier",
    "VerifiedChildReviewProof",
]
