import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.execution_journal import ExecutionJournal


def test_snapshot_returns_latest_200_events_in_sequence_order(tmp_path):
    with ExecutionJournal(tmp_path, target_id=17) as journal:
        for index in range(205):
            journal.append("stdout", f"line-{index}\n".encode("utf-8"))

        view = journal.snapshot()

    assert [event.seq for event in view.events] == list(range(6, 206))
    assert view.events[0].text == "line-5\n"
    assert view.events[-1].text == "line-204\n"
    assert view.cursor == 205
    assert view.latest_seq == 205


def test_incremental_cursor_does_not_repeat_events(tmp_path):
    with ExecutionJournal(tmp_path, target_id=23) as journal:
        for text in (b"one\n", b"two\n", b"three\n", b"four\n"):
            journal.append("stdout", text)

        first = journal.incremental(after_seq=2)
        second = journal.incremental(after_seq=first.cursor)

    assert [(event.seq, event.text) for event in first.events] == [
        (3, "three\n"),
        (4, "four\n"),
    ]
    assert first.cursor == 4
    assert second.events == ()
    assert second.cursor == 4


def test_incremental_rejects_an_invalid_cursor(tmp_path):
    with ExecutionJournal(tmp_path, target_id=27) as journal:
        journal.append("stdout", b"one\n")
        with pytest.raises(ValueError, match="non-negative integer"):
            journal.incremental(after_seq=-1)


def test_incremental_requires_snapshot_when_cursor_is_ahead(tmp_path):
    with ExecutionJournal(tmp_path, target_id=28) as journal:
        journal.append("stdout", b"one\n")
        with pytest.raises(ValueError, match="use snapshot"):
            journal.incremental(after_seq=2)


def test_read_limit_has_a_hard_cap_of_1000(tmp_path):
    with ExecutionJournal(tmp_path, target_id=29) as journal:
        with pytest.raises(ValueError, match="between 1 and 1000"):
            journal.snapshot(limit=1001)


def test_utf8_split_across_chunks_emits_only_valid_text_fragments(tmp_path):
    with ExecutionJournal(tmp_path, target_id=31) as journal:
        incomplete = journal.append("stdout", b"\xe4\xb8")
        completed = journal.append("stdout", b"\xad")
        terminated = journal.append("stderr", b"problem\n")
        view = journal.snapshot()

    assert incomplete == ()
    assert [(event.seq, event.stream, event.text, event.fragment) for event in completed] == [
        (1, "stdout", "中", True)
    ]
    assert [
        (event.seq, event.stream, event.text, event.fragment)
        for event in terminated
    ] == [(2, "stderr", "problem\n", False)]
    assert [event.seq for event in view.events] == [1, 2]


def test_one_chunk_is_normalized_into_lines_and_one_trailing_fragment(
    tmp_path,
):
    with ExecutionJournal(tmp_path, target_id=33) as journal:
        events = journal.append("stdout", b"first\nsecond\nhalf")

    assert [
        (event.seq, event.text, event.fragment) for event in events
    ] == [
        (1, "first\n", False),
        (2, "second\n", False),
        (3, "half", True),
    ]


def test_committed_events_survive_reopen_and_sequence_continues(tmp_path):
    with ExecutionJournal(tmp_path, target_id=37) as journal:
        journal.append("stdout", b"before crash\n")

    with ExecutionJournal(tmp_path, target_id=37) as recovered:
        appended = recovered.append("stderr", b"after restart\n")
        view = recovered.snapshot()

    assert [(event.seq, event.text) for event in view.events] == [
        (1, "before crash\n"),
        (2, "after restart\n"),
    ]
    assert appended[0].seq == 2


def test_incomplete_utf8_fragment_survives_reopen(tmp_path):
    with ExecutionJournal(tmp_path, target_id=41) as journal:
        assert journal.append("stdout", b"\xe4\xb8") == ()

    with ExecutionJournal(tmp_path, target_id=41) as recovered:
        events = recovered.append("stdout", b"\xad")

    assert [(event.seq, event.text, event.fragment) for event in events] == [
        (1, "中", True)
    ]


def test_stream_byte_offsets_survive_reopen_including_incomplete_utf8(
        tmp_path):
    with ExecutionJournal(tmp_path, target_id=42) as journal:
        journal.append("stdout", b"\xe4\xb8")
        journal.append("stderr", b"warning\n")
        assert journal.stream_offsets == {
            "stdout": 2,
            "stderr": 8,
        }

    with ExecutionJournal(tmp_path, target_id=42) as recovered:
        assert recovered.stream_offsets == {
            "stdout": 2,
            "stderr": 8,
        }
        recovered.append("stdout", b"\xad\n")
        assert recovered.stream_offsets["stdout"] == 4


def test_capture_offsets_are_scoped_to_one_execution_and_survive_reopen(
        tmp_path):
    with ExecutionJournal(tmp_path, target_id=46) as journal:
        smoke = journal.open_capture("capture-smoke")
        assert smoke == {
            "capture_id": "capture-smoke",
            "frame_offset": 0,
            "stream_offsets": {"stdout": 0, "stderr": 0},
        }
        journal.append_capture(
            "capture-smoke", "stdout", b"smoke-log\n", 20)
        train = journal.open_capture("capture-train")
        assert train["frame_offset"] == 0
        assert train["stream_offsets"] == {"stdout": 0, "stderr": 0}
        journal.append_capture(
            "capture-train", "stderr", b"train-warning\n", 24)

    with ExecutionJournal(tmp_path, target_id=46) as recovered:
        train = recovered.open_capture("capture-train")
        assert train["frame_offset"] == 24
        assert train["stream_offsets"] == {
            "stdout": 0,
            "stderr": len(b"train-warning\n"),
        }
        assert recovered.stream_offsets == {
            "stdout": len(b"smoke-log\n"),
            "stderr": len(b"train-warning\n"),
        }


def test_incomplete_utf8_cannot_cross_execution_capture_identity(tmp_path):
    with ExecutionJournal(tmp_path, target_id=51) as journal:
        journal.open_capture("capture-smoke")
        journal.append_capture(
            "capture-smoke", "stdout", b"\xe4\xb8", 20)

        with pytest.raises(
                ValueError, match="prior execution.*incomplete UTF-8"):
            journal.open_capture("capture-train")

        # Recovery of the same exact guardian operation may supply its suffix.
        journal.open_capture("capture-smoke")
        journal.append_capture(
            "capture-smoke", "stdout", b"\xad\n", 40)
        assert journal.snapshot().events[0].text == "中\n"

        journal.open_capture("capture-train")
        journal.append_capture(
            "capture-train", "stdout", b"fresh\n", 20)
        assert journal.snapshot().events[-1].text == "fresh\n"


def test_capture_frame_cursor_commits_atomically_with_event_state(
        tmp_path, monkeypatch):
    journal = ExecutionJournal(tmp_path, target_id=48)
    journal.open_capture("capture-train")
    real_persist = journal._persist_decoder_state  # noqa: SLF001
    calls = 0

    def fail_capture_commit():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("owner-lost-before-capture-state-replace")
        real_persist()

    monkeypatch.setattr(
        journal, "_persist_decoder_state", fail_capture_commit)
    with pytest.raises(
            OSError, match="owner-lost-before-capture-state-replace"):
        journal.append_capture(
            "capture-train", "stdout", b"uncommitted\n", 21)
    journal.close()

    with ExecutionJournal(tmp_path, target_id=48) as recovered:
        checkpoint = recovered.open_capture("capture-train")
        assert checkpoint["frame_offset"] == 0
        assert checkpoint["stream_offsets"] == {
            "stdout": 0, "stderr": 0}
        assert recovered.snapshot().events == ()


def test_large_history_uses_disk_index_and_reads_only_bounded_windows(
        tmp_path):
    payload = b"".join(
        f"line-{index:04d}\n".encode("ascii")
        for index in range(1500)
    )
    with ExecutionJournal(tmp_path, target_id=49) as journal:
        journal.append("stdout", payload)
        assert not hasattr(journal, "_event_offsets")
        tail = journal.snapshot(limit=10)
        assert tail.latest_seq == 1500
        assert [event.seq for event in tail.events] == list(
            range(1491, 1501))

    index_path = tmp_path / "target-49.events.idx"
    assert index_path.stat().st_size == 1500 * 16
    with ExecutionJournal(tmp_path, target_id=49) as recovered:
        window = recovered.incremental(after_seq=1495, limit=5)
        assert [event.seq for event in window.events] == [
            1496, 1497, 1498, 1499, 1500]


def test_append_chunk_has_a_hard_memory_bound(tmp_path):
    with ExecutionJournal(tmp_path, target_id=50) as journal:
        with pytest.raises(ValueError, match="65536"):
            journal.append("stdout", b"x" * (64 * 1024 + 1))


def test_legacy_empty_event_state_derives_only_pending_stream_bytes(tmp_path):
    with ExecutionJournal(tmp_path, target_id=44) as journal:
        journal.append("stdout", b"\xe4\xb8")

    state_path = tmp_path / "target-44.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for field in (
            "event_file_bytes", "event_count", "index_file_bytes",
            "stream_bytes"):
        state.pop(field)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with ExecutionJournal(tmp_path, target_id=44) as recovered:
        assert recovered.stream_offsets == {"stdout": 2, "stderr": 0}
        recovered.append("stdout", b"\xad")


def test_legacy_state_with_events_fails_closed_without_stream_checkpoint(
        tmp_path):
    with ExecutionJournal(tmp_path, target_id=45) as journal:
        journal.append("stdout", b"committed\n")

    state_path = tmp_path / "target-45.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for field in (
            "event_file_bytes", "event_count", "index_file_bytes",
            "stream_bytes"):
        state.pop(field)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="atomic checkpoint"):
        ExecutionJournal(tmp_path, target_id=45)


def test_finalizing_stream_keeps_trailing_half_line_as_fragment(tmp_path):
    with ExecutionJournal(tmp_path, target_id=43) as journal:
        emitted = journal.append("stdout", b"trailing half line")
        finalized = journal.append("stdout", b"", final=True)
        with pytest.raises(RuntimeError, match="already finalized"):
            journal.append("stdout", b"too late")

    assert [(event.text, event.fragment) for event in emitted] == [
        ("trailing half line", True)
    ]
    assert finalized == ()


def test_reopen_discards_only_a_torn_uncommitted_tail(tmp_path):
    with ExecutionJournal(tmp_path, target_id=47) as journal:
        journal.append("stdout", b"committed\n")
        event_path = journal.event_path

    with event_path.open("ab") as handle:
        handle.write(b'{"fragment":false,"seq":2')
        handle.flush()

    with ExecutionJournal(tmp_path, target_id=47) as recovered:
        recovered.append("stderr", b"still valid\n")
        view = recovered.snapshot()

    assert [(event.seq, event.text) for event in view.events] == [
        (1, "committed\n"),
        (2, "still valid\n"),
    ]


def test_reopen_discards_a_complete_event_beyond_atomic_state_checkpoint(
        tmp_path):
    with ExecutionJournal(tmp_path, target_id=48) as journal:
        journal.append("stdout", b"committed\n")
        event_path = journal.event_path

    uncommitted = {
        "fragment": False,
        "seq": 2,
        "stream": "stderr",
        "text": "must-replay\n",
    }
    with event_path.open("ab") as handle:
        handle.write(
            json.dumps(
                uncommitted, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8") + b"\n")
        handle.flush()

    with ExecutionJournal(tmp_path, target_id=48) as recovered:
        assert recovered.stream_offsets == {
            "stdout": len(b"committed\n"),
            "stderr": 0,
        }
        recovered.append("stderr", b"must-replay\n")
        view = recovered.snapshot()

    assert [(event.seq, event.stream, event.text) for event in view.events] == [
        (1, "stdout", "committed\n"),
        (2, "stderr", "must-replay\n"),
    ]


def test_compact_status_changes_have_a_separate_monotonic_revision(tmp_path):
    with ExecutionJournal(tmp_path, target_id=53) as journal:
        first_revision = journal.publish_status(
            {"state": "running", "attempt": 1}
        )
        unchanged_revision = journal.publish_status(
            {"state": "running", "attempt": 1}
        )
        view = journal.snapshot()

    assert first_revision == 1
    assert unchanged_revision == 1
    assert view.status == {"state": "running", "attempt": 1}
    assert view.status_revision == 1
    assert view.terminal is False


def test_wait_returns_when_a_new_event_is_committed(tmp_path):
    with ExecutionJournal(tmp_path, target_id=59) as journal:
        started = threading.Event()

        def wait_for_output():
            started.set()
            return journal.wait(
                after_seq=0,
                after_status_revision=0,
                timeout_s=1.0,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(wait_for_output)
            assert started.wait(timeout=1.0)
            journal.append("stdout", b"ready\n")
            view = future.result(timeout=1.0)

    assert view.reason == "events"
    assert [(event.seq, event.text) for event in view.events] == [(1, "ready\n")]
    assert view.cursor == 1


def test_wait_returns_when_compact_status_changes(tmp_path):
    with ExecutionJournal(tmp_path, target_id=61) as journal:
        started = threading.Event()

        def wait_for_status():
            started.set()
            return journal.wait(
                after_seq=0,
                after_status_revision=0,
                timeout_s=1.0,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(wait_for_status)
            assert started.wait(timeout=1.0)
            journal.publish_status({"state": "evaluating"})
            view = future.result(timeout=1.0)

    assert view.reason == "status"
    assert view.events == ()
    assert view.status == {"state": "evaluating"}
    assert view.status_revision == 1


def test_wait_returns_immediately_for_terminal_state(tmp_path):
    with ExecutionJournal(tmp_path, target_id=67) as journal:
        revision = journal.publish_status({"state": "complete"}, terminal=True)

        view = journal.wait(
            after_seq=0,
            after_status_revision=revision,
            timeout_s=1.0,
        )

    assert view.reason == "terminal"
    assert view.terminal is True
    assert view.status == {"state": "complete"}


def test_wait_returns_a_bounded_timeout_view(tmp_path):
    with ExecutionJournal(tmp_path, target_id=71) as journal:
        view = journal.wait(
            after_seq=0,
            after_status_revision=0,
            timeout_s=0,
        )

    assert view.reason == "timeout"
    assert view.events == ()
    assert view.cursor == 0


def test_concurrent_stdout_and_stderr_appends_keep_one_monotonic_sequence(
    tmp_path,
):
    with ExecutionJournal(tmp_path, target_id=73) as journal:

        def emit(index):
            stream = "stdout" if index % 2 == 0 else "stderr"
            journal.append(stream, f"event-{index}\n".encode("utf-8"))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(emit, range(200)))
        view = journal.snapshot(limit=200)

    assert [event.seq for event in view.events] == list(range(1, 201))
    assert {event.text for event in view.events} == {
        f"event-{index}\n" for index in range(200)
    }
