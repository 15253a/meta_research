import json

from meta_research.chat_progress import read_chat_reply


def test_reply_is_visible_before_provider_turn_completes(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    event = {"type": "item.updated", "item": {"id": "answer", "type": "agent_message", "text": '{"reply":"Hello'}}
    spool.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert read_chat_reply(spool) == "Hello"


def test_supervisor_publishes_small_stdout_chunk_before_eof():
    import os
    import threading
    from meta_research.provider_supervisor import _bounded_stdout_drain

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    received = threading.Event()
    captured = bytearray()
    class Destination:
        def write(self, value):
            captured.extend(value)
            received.set()

    errors = []
    drainer = threading.Thread(target=_bounded_stdout_drain, args=(
        stream, Destination(), 1024, threading.Event(), errors
    ))
    drainer.start()
    try:
        os.write(write_fd, b'{"type":"turn.started"}\n')
        assert received.wait(1), "small provider output remained buffered until EOF"
        assert captured == b'{"type":"turn.started"}\n'
    finally:
        os.close(write_fd)
        drainer.join(2)
    assert not drainer.is_alive()
    assert errors == []


def _event(text, identifier="answer", kind="agent_message", event_type="item.completed", **fields):
    return json.dumps({"type": event_type, "item": {"id": identifier, "type": kind, "text": text, **fields}}, ensure_ascii=False).encode("utf-8") + b"\n"


def test_commentary_accumulates_and_final_reply_replaces_preview(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    first = _event("First paragraph.", "first")
    spool.write_bytes(first)
    assert read_chat_reply(spool) == "First paragraph."
    second = _event("Second paragraph.", "second")
    spool.write_bytes(first + second + second)
    assert read_chat_reply(spool) == "First paragraph.\n\nSecond paragraph."
    final = _event('{"reply":"Final answer.","agent_proposal":{"text":"private structure"}}', "final")
    spool.write_bytes(first + second + final)
    assert read_chat_reply(spool) == "Final answer."


def test_updated_message_replaces_same_id_without_repeating(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event('{"reply":"Hel', event_type="item.updated") + _event('{"reply":"Hello', event_type="item.updated"))
    assert read_chat_reply(spool) == "Hello"
    assert read_chat_reply(spool) == "Hello"


def test_partial_jsonl_and_utf8_wait_for_complete_record(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    prefix = _event("Earlier", "earlier")
    record = _event('{"reply":"中文😀"}')
    boundary = record.index("中".encode("utf-8")) + 1
    spool.write_bytes(prefix + record[:boundary])
    assert read_chat_reply(spool) == "Earlier"
    spool.write_bytes(prefix + record)
    assert read_chat_reply(spool) == "中文😀"


def test_partial_structured_reply_decodes_only_complete_escapes(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event(r'{"reply":"A\nB\u4e2d\ud83d'))
    assert read_chat_reply(spool) == "A\nB中"
    spool.write_bytes(_event(r'{"reply":"A\nB\u4e2d\ud83d\ude00'))
    assert read_chat_reply(spool) == "A\nB中😀"
    spool.write_bytes(_event('{"reply":"A' + chr(92)))
    assert read_chat_reply(spool) == "A"


def test_only_assistant_text_and_top_level_reply_are_public(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(
        _event("secret command", "tool", "command_execution")
        + _event("secret reasoning", "reason", "reasoning")
        + _event("secret analysis", "analysis", phase="analysis")
        + _event("secret tool result", "mcp", "mcp_tool_call")
        + _event('{"tool":{"reply":"secret nested"},"reply":"Public answer"}', "answer")
    )
    assert read_chat_reply(spool) == "Public answer"
    spool.write_bytes(_event('{"agent_proposal":{"reply":"secret nested"}}'))
    assert read_chat_reply(spool) == ""
    spool.write_bytes(_event('[{"reply":"secret array"}]'))
    assert read_chat_reply(spool) == ""


def test_large_tool_output_is_bounded_and_does_not_hide_recent_answer(tmp_path, monkeypatch):
    from pathlib import Path
    from meta_research.chat_progress import CHAT_PROGRESS_READ_MAX_BYTES
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event("x" * (CHAT_PROGRESS_READ_MAX_BYTES + 100), "tool", "command_execution") + _event('{"reply":"Recent"}'))
    original_open = Path.open
    reads = []
    class BoundedRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def seek(self, *args):
            return self.stream.seek(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= CHAT_PROGRESS_READ_MAX_BYTES
            return self.stream.read(size)
    def tracked_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return BoundedRead(stream) if path == spool else stream
    monkeypatch.setattr(Path, "open", tracked_open)
    assert read_chat_reply(spool) == ""
    assert read_chat_reply(spool) == "Recent"
    assert reads == [CHAT_PROGRESS_READ_MAX_BYTES] * 2
    with original_open(spool, "ab") as stream:
        stream.write(_event('{"reply":"Recent updated"}', "final"))
    reads.clear()
    assert read_chat_reply(spool) == "Recent updated"
    assert sum(reads) <= CHAT_PROGRESS_READ_MAX_BYTES
    reads.clear()
    assert read_chat_reply(spool) == "Recent updated"
    assert reads == []


def test_missing_and_invalid_spool_have_no_preview(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    assert read_chat_reply(spool) == ""
    spool.write_bytes(b"invalid json\n" + b"\xff\n")
    assert read_chat_reply(spool) == ""


def test_companion_preview_checks_job_and_signature(tmp_path):
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.idea_skill import _sealed_operation_invocation
    from meta_research.owners.common import canonical_hash
    adapter = CodexCompanionAdapter(tmp_path)
    job = "current-job"
    directory = tmp_path / "provider-operations" / canonical_hash({"job_ref": job}) / "companion-turn"
    directory.mkdir(parents=True)
    invocation = {
        "schema_ref": "meta-research/codex-provider-operation/v3",
        "job_ref": job,
        "operation_name": "companion-turn",
        "transport_mode": "durable_supervisor",
        "prompt_max_bytes": 1024 * 1024,
        "stream_max_bytes": 1024 * 1024,
        "result_max_bytes": 1024 * 1024,
    }
    _key_path, key = adapter._transport_key()
    path = directory / "invocation.json"
    path.write_text(_sealed_operation_invocation(invocation, key))
    (directory / "stdout.jsonl").write_bytes(_event('{"reply":"Current answer"}'))
    assert adapter.observe_reply(job) == "Current answer"
    assert adapter.observe_reply("other-job") == ""
    invocation["job_ref"] = "other-job"
    path.write_text(_sealed_operation_invocation(invocation, key))
    assert adapter.observe_reply(job) == ""
    invocation["job_ref"] = job
    path.write_text(_sealed_operation_invocation(invocation, b"wrong-key"))
    assert adapter.observe_reply(job) == ""


def test_drafting_preview_checks_job_and_excludes_proposals(tmp_path):
    from meta_research.quest_drafting import CodexDraftingAdapter, DRAFTING_JOB_SCHEMA_V1
    adapter = CodexDraftingAdapter(tmp_path)
    directory = adapter._durable_job_directory("current-job")
    directory.mkdir(parents=True)
    invocation = {
        "schema_ref": DRAFTING_JOB_SCHEMA_V1,
        "job_ref": "current-job",
        "prompt_hash": "a" * 64,
        "schema_hash": "b" * 64,
        "native_session_ref": None,
        "ephemeral": False,
        "transport_mode": "durable_supervisor",
    }
    path = directory / "invocation.json"
    path.write_text(json.dumps(invocation))
    (directory / "stdout.jsonl").write_bytes(_event('{"reply":"Current answer"}'))
    assert adapter.observe_reply("current-job") == "Current answer"
    assert adapter.observe_reply("other-job") == ""
    invocation["ephemeral"] = True
    path.write_text(json.dumps(invocation))
    assert adapter.observe_reply("current-job") == ""
    invocation["ephemeral"] = False
    invocation["job_ref"] = "other-job"
    path.write_text(json.dumps(invocation))
    assert adapter.observe_reply("current-job") == ""


def test_reply_providers_request_public_paragraph_progress(tmp_path, monkeypatch):
    from meta_research.chat_progress import CHAT_REPLY_PROGRESS_INSTRUCTION
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.quest_drafting import CodexDraftingAdapter, IntentTurnRequest
    request = IntentTurnRequest(
        initialization_id="init", draft_revision=1, draft_hash="a" * 64,
        draft={}, message="User question", native_session_ref="session",
    )
    prompts = []
    companion = CodexCompanionAdapter(tmp_path / "companion")
    def companion_invoke(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"reply": "Complete answer"}, "session", ""
    monkeypatch.setattr(companion, "_invoke_optional_root_task_operation", companion_invoke)
    assert companion.reply(request).reply == "Complete answer"
    drafting = CodexDraftingAdapter(tmp_path / "drafting")
    def drafting_invoke(prompt, schema, **kwargs):
        prompts.append(prompt)
        return {"reply": "Complete answer"}, "session"
    monkeypatch.setattr(drafting, "_invoke", drafting_invoke)
    assert drafting.reply(request).reply == "Complete answer"
    assert len(prompts) == 2
    assert all(CHAT_REPLY_PROGRESS_INSTRUCTION in prompt for prompt in prompts)


def test_supervisor_progress_preserves_byte_limit_and_overflow():
    import io
    import threading
    from meta_research.provider_supervisor import _bounded_stdout_drain
    source = io.BytesIO(b"abcdef")
    destination = io.BytesIO()
    exceeded = threading.Event()
    errors = []
    _bounded_stdout_drain(source, destination, 4, exceeded, errors)
    assert destination.getvalue() == b"abcd"
    assert exceeded.is_set()
    assert source.closed
    assert errors == []


def test_json_fences_do_not_expose_transport_fields(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    for opening in ("```", "```json", "```JSON"):
        spool.write_bytes(_event(opening + '\n{"reply":"Public","agent_proposal":{"text":"Hidden"}}\n```'))
        assert read_chat_reply(spool) == "Public"


def test_non_public_message_roles_or_channels_are_ignored(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event("user input", "user", role="user") + _event("reasoning", "reason", channel="analysis") + _event("unknown channel", "other", phase="internal") + _event("Public", "public", phase="commentary"))
    assert read_chat_reply(spool) == "Public"


def test_large_tool_gap_does_not_drop_previously_streamed_paragraphs(tmp_path):
    from meta_research.chat_progress import CHAT_PROGRESS_READ_MAX_BYTES
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event("First paragraph", "first"))
    assert read_chat_reply(spool) == "First paragraph"
    with spool.open("ab") as stream:
        stream.write(_event("x" * (CHAT_PROGRESS_READ_MAX_BYTES + 100), "tool", "command_execution"))
    assert read_chat_reply(spool) == "First paragraph"
    with spool.open("ab") as stream:
        stream.write(_event("Second paragraph", "second"))
    for _ in range(3):
        preview = read_chat_reply(spool)
        assert preview.startswith("First paragraph")
    assert preview == "First paragraph\n\nSecond paragraph"


def test_drafting_recovers_existing_job_with_original_prompt(tmp_path, monkeypatch):
    from dataclasses import replace
    from meta_research.chat_progress import CHAT_REPLY_PROGRESS_INSTRUCTION
    from meta_research.quest_drafting import (
        CODEX_DRAFTING_LOCKED_VERSION, CodexDraftingAdapter, IntentTurnRequest,
        _reply_schema, _seal_durable_job, _write_durable_json,
    )
    calls = []
    def runner(*args):
        calls.append(args)
        raise AssertionError("completed job must not launch a provider again")
    adapter = CodexDraftingAdapter(tmp_path, process_runner=runner)
    request = IntentTurnRequest("init", 1, "a" * 64, {}, "Question", "session")
    actual_invoke = adapter._invoke
    captured = []
    def capture(prompt, *args, **kwargs):
        captured.append(prompt)
        return {"reply": "Saved answer"}, "session"
    monkeypatch.setattr(adapter, "_invoke", capture)
    adapter.reply(request)
    legacy_prompt = captured[0].replace(CHAT_REPLY_PROGRESS_INSTRUCTION, "", 1)
    job = "old-drafting-job"
    directory = adapter._durable_job_directory(job)
    directory.mkdir(parents=True)
    invocation = adapter._drafting_invocation(
        legacy_prompt, _reply_schema(), native_session_ref="session",
        ephemeral=False, job_ref=job, directory=directory,
        provider_version=CODEX_DRAFTING_LOCKED_VERSION,
    )
    _write_durable_json(directory / "invocation.json", invocation)
    _seal_durable_job(directory, invocation, ({"reply": "Saved answer"}, "session"))
    before = (directory / "invocation.json").read_bytes()
    monkeypatch.setattr(adapter, "_invoke", actual_invoke)
    assert adapter.reply(replace(request, job_ref=job)).reply == "Saved answer"
    assert (directory / "invocation.json").read_bytes() == before
    assert calls == []


def test_companion_keeps_original_prompt_for_signed_existing_job(tmp_path, monkeypatch):
    from dataclasses import replace
    from meta_research.chat_progress import CHAT_REPLY_PROGRESS_INSTRUCTION
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.idea_skill import _read_operation_invocation, _sealed_operation_invocation
    from meta_research.owners.common import canonical_hash
    from meta_research.quest_drafting import IntentTurnRequest
    adapter = CodexCompanionAdapter(tmp_path)
    request = IntentTurnRequest("init", 1, "a" * 64, {}, "Question", "session")
    captured = []
    def capture(**kwargs):
        captured.append(kwargs["prompt"])
        return {"reply": "Saved answer"}, "session", ""
    monkeypatch.setattr(adapter, "_invoke_optional_root_task_operation", capture)
    adapter.reply(request)
    legacy_prompt = captured[0].replace(CHAT_REPLY_PROGRESS_INSTRUCTION, "", 1)
    job = "old-companion-job"
    directory = tmp_path / "provider-operations" / canonical_hash({"job_ref": job}) / "companion-turn"
    directory.mkdir(parents=True)
    invocation = {
        "schema_ref": "meta-research/codex-provider-operation/v3",
        "job_ref": job, "operation_name": "companion-turn",
        "prompt_hash": canonical_hash(legacy_prompt),
        "transport_mode": "durable_supervisor",
        "prompt_max_bytes": 1024 * 1024,
        "stream_max_bytes": 1024 * 1024,
        "result_max_bytes": 1024 * 1024,
    }
    _key_path, key = adapter._transport_key()
    path = directory / "invocation.json"
    path.write_text(_sealed_operation_invocation(invocation, key))
    before = path.read_bytes()
    def recover(**kwargs):
        expected = {key: value for key, value in invocation.items() if key != "transport_mode"}
        expected["prompt_hash"] = canonical_hash(kwargs["prompt"])
        _read_operation_invocation(path, key=key, expected_base=expected)
        return {"reply": "Saved answer"}, "session", ""
    monkeypatch.setattr(adapter, "_invoke_optional_root_task_operation", recover)
    assert adapter.reply(replace(request, job_ref=job)).reply == "Saved answer"
    assert path.read_bytes() == before


def test_progress_cache_resets_on_truncation_and_same_size_replacement(tmp_path):
    import os
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event("First answer", "first"))
    assert read_chat_reply(spool) == "First answer"
    spool.write_bytes(_event("Short", "short"))
    assert read_chat_reply(spool) == "Short"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(_event("Other", "other"))
    assert replacement.stat().st_size == spool.stat().st_size
    os.replace(replacement, spool)
    assert read_chat_reply(spool) == "Other"
    previous = spool.stat()
    spool.write_bytes(_event("Third", "third"))
    os.utime(spool, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    assert read_chat_reply(spool) == "Third"


def test_progress_cache_eviction_replays_from_start_with_bounded_catchup(tmp_path, monkeypatch):
    import meta_research.chat_progress as progress
    monkeypatch.setattr(progress, "CHAT_PROGRESS_CACHE_MAX_FILES", 2)
    first = tmp_path / "first.jsonl"
    first.write_bytes(_event("First paragraph", "first") + _event("x" * (progress.CHAT_PROGRESS_READ_MAX_BYTES + 10), "tool", "command_execution") + _event("Second paragraph", "second"))
    assert read_chat_reply(first) == "First paragraph"
    for name in ("other", "another"):
        spool = tmp_path / (name + ".jsonl")
        spool.write_bytes(_event(name))
        assert read_chat_reply(spool) == name
    assert len(progress._reply_progress) == 2
    assert read_chat_reply(first) == "First paragraph"
    assert read_chat_reply(first) == "First paragraph\n\nSecond paragraph"
    assert len(progress._reply_progress) == 2


def test_concurrent_progress_readers_do_not_duplicate_paragraphs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event("First", "first") + _event("Second", "second"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        previews = list(pool.map(lambda _: read_chat_reply(spool), range(16)))
    assert previews == ["First\n\nSecond"] * 16


def test_schema_wrapped_paragraphs_remain_visible_until_complete_reply(tmp_path):
    """The real CLI applies the reply schema to commentary messages as well."""
    spool = tmp_path / "stdout.jsonl"
    first = _event(json.dumps({"reply": "First paragraph.", "agent_proposal": None}), "first")
    second = _event(json.dumps({"reply": "Second paragraph.", "agent_proposal": None}), "second")
    spool.write_bytes(first)
    assert read_chat_reply(spool) == "First paragraph."
    with spool.open("ab") as stream:
        stream.write(second)
    expected = "First paragraph.\n\nSecond paragraph."
    assert read_chat_reply(spool) == expected
    # Retransmitted items cannot duplicate already displayed paragraphs.
    with spool.open("ab") as stream:
        stream.write(second)
    assert read_chat_reply(spool) == expected
    # A schema-wrapped final carries the whole answer, not a third paragraph.
    with spool.open("ab") as stream:
        stream.write(_event(json.dumps({"reply": expected, "agent_proposal": {"text": "Hidden"}}), "final"))
    assert read_chat_reply(spool) == expected
    with spool.open("ab") as stream:
        stream.write(b'{"type":"turn.completed"}\n')
    assert read_chat_reply(spool) == expected


def test_schema_wrapped_paragraph_updates_and_cumulative_prefix_do_not_repeat(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event('{"reply":"First"}', "first") + _event('{"reply":"Sec"}', "second", event_type="item.updated"))
    assert read_chat_reply(spool) == "First\n\nSec"
    with spool.open("ab") as stream:
        stream.write(_event('{"reply":"Second"}', "second", event_type="item.updated"))
    assert read_chat_reply(spool) == "First\n\nSecond"
    with spool.open("ab") as stream:
        stream.write(_event('{"reply":"First"}', "final", event_type="item.updated"))
    assert read_chat_reply(spool) == "First\n\nSecond"
    with spool.open("ab") as stream:
        stream.write(_event('{"reply":"First\\n\\nSecond\\n\\nThird"}', "final"))
    assert read_chat_reply(spool) == "First\n\nSecond\n\nThird"


def test_terminal_reply_can_revise_structured_paragraphs(tmp_path):
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event('{"reply":"First draft"}', "first") + _event('{"reply":"Second draft"}', "second"))
    assert read_chat_reply(spool) == "First draft\n\nSecond draft"
    with spool.open("ab") as stream:
        stream.write(_event('{"reply":"Revised complete answer"}', "final") + b'{"type":"turn.completed"}\n')
    assert read_chat_reply(spool) == "Revised complete answer"


def test_structured_paragraph_accumulation_stays_bounded_and_private(tmp_path):
    from meta_research.chat_progress import CHAT_REPLY_MAX_LENGTH
    spool = tmp_path / "stdout.jsonl"
    spool.write_bytes(_event(json.dumps({"reply": "A" * 8000, "agent_proposal": {"text": "PRIVATE"}}), "first"))
    assert len(read_chat_reply(spool)) == 8000
    with spool.open("ab") as stream:
        stream.write(_event(json.dumps({"reply": "B" * 8000, "tool": {"reply": "PRIVATE"}}), "second"))
        stream.write(_event('{"agent_proposal":{"reply":"PRIVATE"}}', "no-reply"))
    preview = read_chat_reply(spool)
    assert preview == "A" * 8000 + "\n\n" + "B" * (CHAT_REPLY_MAX_LENGTH - 8002)
    assert "PRIVATE" not in preview
