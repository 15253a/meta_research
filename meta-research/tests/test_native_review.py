from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "native_review_appserver_minimal.jsonl"


def _api():
    from orchestrator.native_review import NativeReviewError, NativeReviewLedger
    return NativeReviewError, NativeReviewLedger


def _receipt(raw: bytes) -> dict:
    return {
        "state": "terminal",
        "outcome": "exit",
        "returncode": 0,
        "group_drained": True,
        "capture_stdout_bytes": len(raw),
        "capture_stdout_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _events(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.splitlines()]


def _raw(events: list[dict]) -> bytes:
    return b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for event in events)


def _without_raw_spawn(raw: bytes) -> bytes:
    return _raw([
        event for event in _events(raw)
        if not (
            event.get("method") == "rawResponseItem/completed"
            and event.get("params", {}).get("item", {}).get("name")
            == "spawn_agent")
    ])


def _finalize(raw: bytes):
    _error, ledger_type = _api()
    ledger = ledger_type()
    ledger.feed(raw)
    return ledger.finalize(receipt=_receipt(raw), captured_stdout=raw)


def _fixture_prefix(last_line: int) -> bytes:
    """Return fixture lines through ``last_line`` (one-based, inclusive)."""
    return b"\n".join(FIXTURE.read_bytes().splitlines()[:last_line]) + b"\n"


def test_redacted_real_app_server_chain_is_accepted():
    raw = FIXTURE.read_bytes()

    completed = _finalize(raw)

    assert len(completed) == 1
    evidence = completed[0]
    assert evidence.parent_thread_id == "thread-parent-1"
    assert evidence.parent_turn_id == "turn-parent-1"
    assert evidence.call_id == "call-spawn-1"
    assert evidence.child_thread_id == "thread-child-1"
    assert evidence.child_turn_id == "turn-child-1"
    assert evidence.final_bytes == b"CHILD_REVIEW_OK"


def test_thread_read_may_normalize_the_child_final_item_id():
    events = _events(FIXTURE.read_bytes())
    child_read = next(
        event for event in events
        if event.get("id") == "native-review-read:thread-child-1")
    child_read["result"]["thread"]["turns"][0]["items"][0]["id"] = "item-3"

    completed = _finalize(_raw(events))

    assert len(completed) == 1
    assert completed[0].final_bytes == b"CHILD_REVIEW_OK"


def test_default_line_bound_matches_the_appserver_transport_bound():
    events = _events(FIXTURE.read_bytes())
    events.insert(3, {
        "method": "warning",
        "params": {"message": "x" * (1100 * 1024)},
    })

    completed = _finalize(_raw(events))

    assert len(completed) == 1


def test_guardian_capture_can_be_replayed_as_durable_child_event_proof(
        tmp_path):
    from orchestrator.native_review import replay_native_review_execution
    from orchestrator.process_supervisor import ExecutionSupervisor

    raw = FIXTURE.read_bytes()
    supervisor = ExecutionSupervisor.standalone(tmp_path / "receipts")
    result = supervisor.run(
        [
            sys.executable, "-c",
            "import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))",
            raw.hex(),
        ],
        capture_output=True, timeout_s=None, kind="codex-resident-stage",
        operation_context={
            "cycle_id": "c3",
            "stage": "bundle",
            "target_id": "5",
            "call_tag": "bundle-main-c3",
            "db_owner_kind": "runner_call",
            "db_owner_id": 17,
            "db_phase": "bundle",
            "db_purpose": "bundle-main-c3",
            "reconcile_protocol": "runner-call-v1",
            "provider": "codex-cli",
            "provider_model": "gpt-test",
            "provider_effort": "high",
            "prompt_sha256": "sha256:" + "a" * 64,
        })

    replay = replay_native_review_execution(
        result.receipt_path,
        expected_runner_call_id=17,
        expected_cycle_id="c3",
        expected_stage="bundle",
        expected_purpose="bundle-main-c3")

    assert replay.runner_call_id == 17
    assert replay.execution_operation_id == result.receipt["operation_id"]
    assert replay.capture_stdout_sha256 == (
        "sha256:" + hashlib.sha256(raw).hexdigest())
    assert len(replay.children) == 1
    assert replay.children[0].parent_thread_id == "thread-parent-1"
    assert replay.children[0].call_id == "call-spawn-1"
    assert replay.children[0].child_thread_id == "thread-child-1"
    assert replay.children[0].child_turn_id == "turn-child-1"

    with pytest.raises(
            ValueError, match="runner_call|execution receipt"):
        replay_native_review_execution(
            result.receipt_path,
            expected_runner_call_id=18,
            expected_cycle_id="c3",
            expected_stage="bundle",
            expected_purpose="bundle-main-c3")


def test_completed_child_can_be_read_and_claimed_before_parent_turn_finishes():
    error_type, ledger_type = _api()
    ledger = ledger_type()
    # Lines 1..9 contain the parent binding and a complete/read-verified child,
    # but deliberately omit the parent final answer and terminal event.
    ledger.feed(_fixture_prefix(9))

    assert ledger.parent_identity() == (
        "thread-parent-1", "turn-parent-1")
    evidence = ledger.completed_child("thread-child-1")
    assert evidence.parent_thread_id == "thread-parent-1"
    assert evidence.child_thread_id == "thread-child-1"
    assert evidence.final_bytes == b"CHILD_REVIEW_OK"
    assert ledger.completed_children() == (evidence,)

    assert ledger.claim_completed_child(
        "thread-child-1", claim_id="review-request-1") == evidence
    # Retrying the same durable request is idempotent, but the same child
    # cannot authorize a second request.
    assert ledger.claim_completed_child(
        "thread-child-1", claim_id="review-request-1") == evidence
    with pytest.raises(error_type, match="claimed"):
        ledger.claim_completed_child(
            "thread-child-1", claim_id="review-request-2")


def test_one_review_request_cannot_claim_two_completed_children():
    error_type, ledger_type = _api()
    ledger = ledger_type()
    ledger.feed(_fixture_prefix(9))
    second_child_events = _events(FIXTURE.read_bytes())[4:9]
    second_child_raw = _raw(second_child_events)
    for old, new in (
            (b"call-spawn-1", b"call-spawn-2"),
            (b"fc-spawn-1", b"fc-spawn-2"),
            (b"thread-child-1", b"thread-child-2"),
            (b"turn-child-1", b"turn-child-2"),
            (b"msg-child-1", b"msg-child-2")):
        second_child_raw = second_child_raw.replace(old, new)
    ledger.feed(second_child_raw)

    ledger.claim_completed_child(
        "thread-child-1", claim_id="review-request-1")
    with pytest.raises(error_type, match="review request"):
        ledger.claim_completed_child(
            "thread-child-2", claim_id="review-request-1")


def test_late_second_child_for_claimed_request_poisoned_finalize():
    error_type, ledger_type = _api()
    request_id = "review-request-1"
    result_text = json.dumps({
        "protocol": "native-review-result-v1",
        "review_request_id": request_id,
    }, sort_keys=True, separators=(",", ":"))
    first_child_events = _events(FIXTURE.read_bytes())[:9]
    first_child_events[6]["params"]["item"]["text"] = result_text
    first_child_events[8]["result"]["thread"]["turns"][0]["items"][0][
        "text"] = result_text
    first_child_raw = _raw(first_child_events)
    ledger = ledger_type()
    ledger.feed(first_child_raw)
    ledger.claim_completed_child(
        "thread-child-1", claim_id=request_id)

    second_child_events = json.loads(json.dumps(first_child_events[4:9]))
    second_child_raw = _raw(second_child_events)
    for old, new in (
            (b"call-spawn-1", b"call-spawn-2"),
            (b"fc-spawn-1", b"fc-spawn-2"),
            (b"thread-child-1", b"thread-child-2"),
            (b"turn-child-1", b"turn-child-2"),
            (b"msg-child-1", b"msg-child-2")):
        second_child_raw = second_child_raw.replace(old, new)
    parent_terminal_raw = _raw(_events(FIXTURE.read_bytes())[9:12])
    ledger.feed(second_child_raw + parent_terminal_raw)
    captured = first_child_raw + second_child_raw + parent_terminal_raw

    with pytest.raises(error_type, match="multiple completed children"):
        ledger.finalize(
            receipt=_receipt(captured), captured_stdout=captured)


def test_live_child_capability_rejects_missing_or_incomplete_child():
    error_type, ledger_type = _api()
    ledger = ledger_type()
    ledger.feed(_fixture_prefix(3))
    with pytest.raises(error_type, match="child"):
        ledger.completed_child("thread-child-1")

    ledger.feed(b"\n".join(FIXTURE.read_bytes().splitlines()[4:8]) + b"\n")
    with pytest.raises(error_type, match="thread/read"):
        ledger.completed_child("thread-child-1")


def test_jsonl_split_at_every_byte_boundary():
    raw = FIXTURE.read_bytes()
    _error, ledger_type = _api()

    for boundary in range(len(raw) + 1):
        ledger = ledger_type()
        ledger.feed(raw[:boundary])
        ledger.feed(raw[boundary:])
        completed = ledger.finalize(
            receipt=_receipt(raw), captured_stdout=raw)
        assert len(completed) == 1, boundary


@pytest.mark.parametrize("raw", [
    b'{"id":0,"result":{}}\n\xff\n',
    b'{"id":0,"result":{}}\n{"broken":]\n',
    b'{"id":0,"id":1}\n',
])
def test_malformed_utf8_json_and_duplicate_keys_are_rejected(raw):
    error_type, ledger_type = _api()
    ledger = ledger_type()

    with pytest.raises(error_type):
        ledger.feed(raw)


def test_oversized_line_and_stream_are_rejected():
    error_type, ledger_type = _api()
    line_ledger = ledger_type(max_line_bytes=32, max_stream_bytes=1024)
    with pytest.raises(error_type, match="line"):
        line_ledger.feed(b'{"x":"' + b"a" * 64 + b'"}\n')

    stream_ledger = ledger_type(max_line_bytes=64, max_stream_bytes=70)
    stream_ledger.feed(b'{"x":"aaaaaaaaaaaaaaaa"}\n')
    stream_ledger.feed(b'{"x":"bbbbbbbbbbbbbbbb"}\n')
    with pytest.raises(error_type, match="stream"):
        stream_ledger.feed(b'{"x":"cccccccccccccccc"}\n')


def test_trailing_partial_and_events_after_finalization_are_rejected():
    error_type, ledger_type = _api()
    raw = FIXTURE.read_bytes()
    ledger = ledger_type()
    ledger.feed(raw + b"{")
    with pytest.raises(error_type, match="partial"):
        ledger.finalize(receipt=_receipt(raw + b"{"), captured_stdout=raw + b"{")

    ledger = ledger_type()
    ledger.feed(raw)
    ledger.finalize(receipt=_receipt(raw), captured_stdout=raw)
    with pytest.raises(error_type, match="final"):
        ledger.feed(b"{}\n")


def test_parent_prompt_prose_and_mailbox_delivery_are_not_child_evidence():
    events = _events(FIXTURE.read_bytes())
    kept = [
        event for event in events
        if not (
            event.get("method") in {"item/completed", "turn/completed"}
            and event.get("params", {}).get("threadId") == "thread-child-1")
        and event.get("id") != "native-review-read:thread-child-1"
        and not (
            event.get("method") == "item/completed"
            and event.get("params", {}).get("item", {}).get("type")
            == "subAgentActivity")
        and not (
            event.get("method") == "rawResponseItem/completed"
            and event.get("params", {}).get("item", {}).get("name")
            == "spawn_agent")
    ]

    assert _finalize(_raw(kept)) == ()


def test_missing_raw_spawn_rejects_linked_activity():
    error_type, _ledger_type = _api()
    raw = _without_raw_spawn(FIXTURE.read_bytes())

    with pytest.raises(error_type, match="spawn"):
        _finalize(raw)


def test_resumed_appserver_accepts_server_child_lineage_without_raw_spawn(
        tmp_path):
    from orchestrator.native_review import (
        NativeReviewLedger,
        replay_native_review_execution,
        replay_native_review_live_snapshot,
    )
    from orchestrator.process_supervisor import ExecutionSupervisor

    raw = _without_raw_spawn(FIXTURE.read_bytes())
    mode = "appserver-resume-lineage-v1"
    ledger = NativeReviewLedger(spawn_proof_mode=mode)
    ledger.feed(raw)
    completed = ledger.finalize(
        receipt=_receipt(raw), captured_stdout=raw)
    assert len(completed) == 1
    assert completed[0].child_thread_id == "thread-child-1"

    snapshot = tmp_path / "live-review.jsonl"
    snapshot.write_bytes(raw)
    snapshot_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    live = replay_native_review_live_snapshot(
        snapshot,
        expected_snapshot_hash=snapshot_hash,
        expected_snapshot_bytes=len(raw),
        expected_runner_call_id=17,
        expected_cycle_id="c3",
        expected_stage="idea",
        expected_purpose="idea-main-c3-n2-a1",
        expected_parent_thread_id="thread-parent-1",
        expected_parent_turn_id="turn-parent-1",
        expected_spawn_proof_mode=mode)
    assert live.spawn_proof_mode == mode
    assert len(live.children) == 1

    supervisor = ExecutionSupervisor.standalone(tmp_path / "receipts")
    result = supervisor.run(
        [
            sys.executable, "-c",
            "import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))",
            raw.hex(),
        ],
        capture_output=True, timeout_s=5, kind="codex-stage-main",
        operation_context={
            "cycle_id": "c3",
            "stage": "idea",
            "target_id": None,
            "call_tag": "idea-main-c3-n2",
            "db_owner_kind": "runner_call",
            "db_owner_id": 17,
            "db_phase": "idea",
            "db_purpose": "idea-main-c3-n2-a1",
            "reconcile_protocol": "runner-call-v1",
            "provider": "codex-cli",
            "provider_model": "gpt-test",
            "provider_effort": "high",
            "prompt_sha256": "sha256:" + "a" * 64,
            "native_review_spawn_proof_mode": mode,
        })
    durable = replay_native_review_execution(
        result.receipt_path,
        expected_runner_call_id=17,
        expected_cycle_id="c3",
        expected_stage="idea",
        expected_purpose="idea-main-c3-n2-a1")
    assert durable.spawn_proof_mode == mode
    assert len(durable.children) == 1


@pytest.mark.parametrize("fork_turns", [None, "all", "2"])
def test_native_reviewer_spawn_requires_clean_context_from_raw_args(fork_turns):
    error_type, _ledger_type = _api()
    events = _events(FIXTURE.read_bytes())
    spawn = next(
        event for event in events
        if event.get("params", {}).get("item", {}).get("name") == "spawn_agent")
    arguments = json.loads(spawn["params"]["item"]["arguments"])
    if fork_turns is None:
        arguments.pop("fork_turns")
    else:
        arguments["fork_turns"] = fork_turns
    spawn["params"]["item"]["arguments"] = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"))

    with pytest.raises(error_type, match="fork_turns"):
        _finalize(_raw(events))


def test_wrong_parent_or_turn_rejects_activity_binding():
    error_type, _ledger_type = _api()
    for field, wrong in (("threadId", "thread-other"), ("turnId", "turn-other")):
        events = _events(FIXTURE.read_bytes())
        activity = next(
            event for event in events
            if event.get("params", {}).get("item", {}).get("type")
            == "subAgentActivity")
        activity["params"][field] = wrong
        raw = _raw(events)
        with pytest.raises(error_type):
            _finalize(raw)


def test_duplicate_spawn_or_child_binding_is_rejected():
    error_type, _ledger_type = _api()
    events = _events(FIXTURE.read_bytes())
    spawn = next(
        event for event in events
        if event.get("params", {}).get("item", {}).get("name") == "spawn_agent")
    events.insert(events.index(spawn) + 1, json.loads(json.dumps(spawn)))
    raw = _raw(events)
    with pytest.raises(error_type, match="duplicate"):
        _finalize(raw)

    events = _events(FIXTURE.read_bytes())
    activity = next(
        event for event in events
        if event.get("params", {}).get("item", {}).get("type")
        == "subAgentActivity")
    other = json.loads(json.dumps(activity))
    other["params"]["item"]["agentThreadId"] = "thread-child-2"
    events.insert(events.index(activity) + 1, other)
    raw = _raw(events)
    with pytest.raises(error_type):
        _finalize(raw)


def test_missing_or_failed_child_terminal_is_rejected():
    error_type, _ledger_type = _api()
    events = [
        event for event in _events(FIXTURE.read_bytes())
        if not (
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("threadId") == "thread-child-1")
    ]
    raw = _raw(events)
    with pytest.raises(error_type, match="terminal"):
        _finalize(raw)

    events = _events(FIXTURE.read_bytes())
    terminal = next(
        event for event in events
        if event.get("method") == "turn/completed"
        and event.get("params", {}).get("threadId") == "thread-child-1")
    terminal["params"]["turn"]["status"] = "failed"
    raw = _raw(events)
    with pytest.raises(error_type, match="completed"):
        _finalize(raw)


def test_parent_child_and_read_turn_require_explicit_null_error():
    error_type, _ledger_type = _api()
    for location in ("parent", "child", "read"):
        for value in ("missing", {"message": "failed"}):
            events = _events(FIXTURE.read_bytes())
            if location == "read":
                turn = next(
                    event["result"]["thread"]["turns"][0]
                    for event in events
                    if event.get("id")
                    == "native-review-read:thread-child-1")
            else:
                thread_id = (
                    "thread-parent-1" if location == "parent"
                    else "thread-child-1")
                turn = next(
                    event["params"]["turn"]
                    for event in events
                    if event.get("method") == "turn/completed"
                    and event.get("params", {}).get("threadId") == thread_id)
            if value == "missing":
                turn.pop("error", None)
            else:
                turn["error"] = value
            with pytest.raises(error_type, match="error"):
                _finalize(_raw(events))


def test_missing_or_mismatched_thread_read_is_rejected():
    error_type, _ledger_type = _api()
    events = [
        event for event in _events(FIXTURE.read_bytes())
        if event.get("id") != "native-review-read:thread-child-1"]
    raw = _raw(events)
    with pytest.raises(error_type, match="thread/read"):
        _finalize(raw)

    events = _events(FIXTURE.read_bytes())
    read = next(
        event for event in events
        if event.get("id") == "native-review-read:thread-child-1")
    read["result"]["thread"]["parentThreadId"] = "thread-other"
    raw = _raw(events)
    with pytest.raises(error_type, match="parent"):
        _finalize(raw)

    events = _events(FIXTURE.read_bytes())
    read = next(
        event for event in events
        if event.get("id") == "native-review-read:thread-child-1")
    read["result"]["thread"]["turns"][0]["items"][0]["text"] = "changed"
    raw = _raw(events)
    with pytest.raises(error_type, match="final"):
        _finalize(raw)


def test_nonterminal_parent_and_capture_identity_mismatch_are_rejected():
    error_type, ledger_type = _api()
    events = [
        event for event in _events(FIXTURE.read_bytes())
        if not (
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("threadId") == "thread-parent-1")]
    raw = _raw(events)
    with pytest.raises(error_type, match="parent"):
        _finalize(raw)

    original = FIXTURE.read_bytes()
    ledger = ledger_type()
    ledger.feed(original)
    bad_receipt = _receipt(original)
    bad_receipt["capture_stdout_bytes"] += 1
    with pytest.raises(error_type, match="bytes"):
        ledger.finalize(receipt=bad_receipt, captured_stdout=original)

    ledger = ledger_type()
    ledger.feed(original)
    bad_receipt = _receipt(original)
    bad_receipt["capture_stdout_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(error_type, match="sha256"):
        ledger.finalize(receipt=bad_receipt, captured_stdout=original)

    ledger = ledger_type()
    ledger.feed(original)
    changed = original.replace(b"PARENT_DONE", b"PARENT_GONE")
    with pytest.raises(error_type, match="capture"):
        ledger.finalize(receipt=_receipt(changed), captured_stdout=changed)


@pytest.mark.parametrize("patch", [
    {"returncode": 7},
    {"returncode": True},
    {"group_drained": False},
    {"group_drained": None},
])
def test_finalize_rejects_nonzero_or_undrained_guardian_receipt(patch):
    error_type, ledger_type = _api()
    raw = FIXTURE.read_bytes()
    ledger = ledger_type()
    ledger.feed(raw)
    receipt = _receipt(raw)
    receipt.update(patch)

    with pytest.raises(error_type, match="receipt"):
        ledger.finalize(receipt=receipt, captured_stdout=raw)
