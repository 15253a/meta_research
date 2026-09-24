"""Verify signed interrupted stdout remains readable without inventing events."""
import json

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    ensure_transport_key,
    write_exit_receipt,
)
from meta_research.target_raw_output import TargetRawOutputStore, TargetRawOutputUnavailable


ROOT = b'{"type":"thread.started","thread_id":"native-target"}\n'
MESSAGE = b'{"type":"item.completed","item":{"type":"agent_message","text":"progress"}}\n'
PREFIX = ROOT + MESSAGE
TAIL = b'{"type":"item.started","item":{"text":"unfinished'
INVOCATION = "a" * 64
OPERATION = "target-run:interrupted:harness_turn:1"


def _spool(tmp_path, stdout, *, reason="stopped", returncode=143, sealed=True):
    workspace = tmp_path / "transport"
    _, key = ensure_transport_key(workspace)
    directory = workspace / "provider-operations" / INVOCATION[:2] / INVOCATION
    directory.mkdir(parents=True)
    (directory / "prompt.txt").write_text("Retain the accepted Target input.")
    (directory / "output-schema.json").write_text('{"type":"object"}')
    (directory / "stdout.jsonl").write_bytes(stdout)

    def seal():
        write_exit_receipt(
            directory / "supervisor-exit.json", key=key, invocation_hash=INVOCATION,
            prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
            returncode=returncode,
            input_bytes=(directory / "prompt.txt").stat().st_size if reason == "completed" and returncode == 0 else 0,
            termination_reason=reason,
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )

    if sealed:
        seal()
    store = TargetRawOutputStore(workspace)
    store.bind_operation(OPERATION, INVOCATION, family="codex")
    return store, directory, seal


def _read(store, **kwargs):
    return store.query(OPERATION, expected_native_session_ref="native-target", terminal=True, **kwargs)


@pytest.mark.parametrize("reason,code", [("stopped", 143), ("timeout", 124), ("completed", 101)])
def test_signed_failure_keeps_complete_prefix_when_live_tail_becomes_terminal(tmp_path, reason, code):
    store, directory, _ = _spool(tmp_path, PREFIX + TAIL, reason=reason, returncode=code)
    before = (directory / "stdout.jsonl").read_bytes()
    assert store.query(OPERATION, terminal=False).text.encode() == PREFIX
    page = _read(store)
    assert page.text.encode() == PREFIX
    assert page.as_dict()["incomplete_tail_bytes"] == len(TAIL)
    assert page.source_bytes == len(before)
    assert page.mapped_bytes == len(PREFIX)
    assert page.status == "complete" and page.source_caught_up and not page.has_more
    assert (directory / "stdout.jsonl").read_bytes() == before


def test_signed_failure_binding_restores_prefix_after_reader_restart_and_pages_exactly(tmp_path):
    _, directory, _ = _spool(tmp_path, PREFIX + TAIL)
    store = TargetRawOutputStore(tmp_path / "transport")
    receipt = {
        "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
        "spool_ref": "provider-spool:" + INVOCATION,
        "transport_invocation_hash": INVOCATION,
        "supervisor_receipt_hash": canonical_hash(json.loads((directory / "supervisor-exit.json").read_text())),
        "termination_reason": "stopped", "provider_returncode": 143,
    }
    store.bind_verified_transport_receipt(OPERATION, receipt)
    result = b""
    after = 0
    while True:
        page = _read(store, after=after, limit=16)
        result += page.text.encode()
        assert page.incomplete_tail_bytes == len(TAIL)
        if not page.has_more:
            break
        assert page.next_offset > after
        after = page.next_offset
    assert result == PREFIX


@pytest.mark.parametrize("tail", [b'{"type":', b'{"type":"abc', b'{"flag":tru', b'{"text":"\xe4\xb8'])
def test_final_json_or_utf8_eof_fragment_is_ignored_only_after_signed_failure(tmp_path, tail):
    store, _, _ = _spool(tmp_path, PREFIX + tail)
    page = _read(store)
    assert page.text.encode() == PREFIX
    assert page.incomplete_tail_bytes == len(tail)


def test_complete_json_without_newline_is_still_included_after_interruption(tmp_path):
    tail = b'{"type":"turn.completed"}'
    store, _, _ = _spool(tmp_path, PREFIX + tail)
    page = _read(store)
    assert page.text.encode() == PREFIX + tail
    assert page.incomplete_tail_bytes == 0


@pytest.mark.parametrize("stdout,reason,code", [
    (PREFIX + TAIL, "completed", 0),
    (PREFIX + TAIL, "stopped", 0),
    (PREFIX + TAIL + b"\n", "stopped", 143),
    (ROOT + b'{bad}\n' + TAIL, "stopped", 143),
    (PREFIX + b'{"type":invalid}', "stopped", 143),
    (PREFIX + b'[]', "stopped", 143),
    (TAIL, "stopped", 143),
])
def test_success_complete_corruption_middle_corruption_and_missing_identity_still_reject(tmp_path, stdout, reason, code):
    store, _, _ = _spool(tmp_path, stdout, reason=reason, returncode=code)
    with pytest.raises(TargetRawOutputUnavailable, match="target_raw_output_event_invalid"):
        _read(store)


def test_missing_failure_seal_does_not_discard_buffer_and_can_retry_when_published(tmp_path):
    store, directory, seal = _spool(tmp_path, PREFIX + TAIL, sealed=False)
    with pytest.raises(TargetRawOutputUnavailable):
        _read(store)
    assert (directory / "stdout.jsonl").read_bytes() == PREFIX + TAIL
    seal()
    assert _read(store).text.encode() == PREFIX


@pytest.mark.parametrize("damage", ["exit_seal", "stdout_hash", "prompt_hash"])
def test_failure_seal_and_all_bound_artifact_hashes_remain_required(tmp_path, damage):
    store, directory, _ = _spool(tmp_path, PREFIX + TAIL)
    if damage == "exit_seal":
        path = directory / "supervisor-exit.json"
        envelope = json.loads(path.read_text())
        envelope["payload"]["returncode"] = 101
        path.write_text(json.dumps(envelope))
    elif damage == "stdout_hash":
        (directory / "stdout.jsonl").write_bytes(PREFIX + TAIL + b"x")
    else:
        (directory / "prompt.txt").write_text("Different input.")
    with pytest.raises(TargetRawOutputUnavailable, match="target_raw_output_receipt_unavailable"):
        _read(store)


def test_wrong_root_identity_still_rejects_before_tail_exception(tmp_path):
    store, _, _ = _spool(tmp_path, PREFIX.replace(b"native-target", b"other-target") + TAIL)
    with pytest.raises(TargetRawOutputUnavailable, match="target_raw_output_root_identity_mismatch"):
        _read(store)


@pytest.mark.parametrize("middle_corrupt", [False, True])
def test_legacy_failed_cache_is_rebuilt_and_every_complete_event_revalidated(tmp_path, middle_corrupt):
    stdout = ROOT + (b'{bad}\n' if middle_corrupt else MESSAGE) + TAIL
    store, _, _ = _spool(tmp_path, stdout)
    # Recreate the old mapper's terminal-error state: it consumed the source
    # fragment and retained only earlier complete events in its in-memory cache.
    state = store._state(INVOCATION)
    state.source_offset = len(stdout)
    state.source_buffer = b""
    state.root_native_session_ref = "native-target"
    state.mapped.extend(ROOT if middle_corrupt else PREFIX)
    state.failure_code = "target_raw_output_event_invalid"
    if middle_corrupt:
        with pytest.raises(TargetRawOutputUnavailable, match="target_raw_output_event_invalid"):
            _read(store)
    else:
        page = _read(store)
        assert page.text.encode() == PREFIX
        assert page.incomplete_tail_bytes == len(TAIL)


def test_sealed_truncated_stream_cannot_grow_after_ignored_fragment(tmp_path):
    store, directory, _ = _spool(tmp_path, PREFIX + TAIL)
    assert _read(store).text.encode() == PREFIX
    with (directory / "stdout.jsonl").open("ab") as output:
        output.write(b'"}}\n')
    with pytest.raises(TargetRawOutputUnavailable, match="target_raw_output_source_changed"):
        _read(store)
