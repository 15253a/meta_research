import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.context_presentation import CONTEXT_VIEW_MAX_BYTES, stage_context_view
from meta_research.harness_adapters import _summarize_codex_event, _summarize_claude_event
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json
from meta_research.provider_call_observations import (
    observe_provider_files, prompt_observation, reported_usage,
)
from meta_research.provider_supervisor import (
    read_transport_envelope, read_verified_exit_receipt, write_exit_receipt,
)
from meta_research.research_notes import manifest_research_notes, materialize_note_body, read_note_body
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _root_finalizer_fixture


def test_prompt_bytes_are_measured_without_guessing_tokens_or_native_history():
    material = {"execution_contract": {"metrics": ["alpha", "beta"]},
                "research_context": {"notes": "证据不足"}}
    prompt = "Instructions. Exact Owner context:\n" + canonical_json(material)
    measured = prompt_observation(prompt)
    assert measured["prompt_utf8_bytes"] == len(prompt.encode())
    assert measured["section_value_utf8_bytes"]["execution_contract"] == len(canonical_json(material["execution_contract"]).encode())
    assert measured["input_tokens"] is None
    assert measured["native_history_included_in_prompt_bytes"] is False


def test_reported_usage_keeps_turn_and_last_call_scopes_distinct():
    turn = {"type": "turn.completed", "usage": {"input_tokens": 9100,
        "cached_input_tokens": 8000, "output_tokens": 400}}
    summary = _summarize_codex_event(turn)[0]["reported_token_usage"]
    assert summary["input_tokens"] == 9100 and summary["cached_input_tokens"] == 8000
    assert summary["scope"] == "provider_turn_reported"
    single = reported_usage({"type": "event_msg", "payload": {"type": "token_count",
        "info": {"last_token_usage": {"input_tokens": 2500, "output_tokens": 80}}}})
    assert single["scope"] == "provider_last_call_reported"
    assert single["cached_input_tokens"] is None
    assert reported_usage({"usage": {"input_tokens": True, "output_tokens": -1}}) is None
    claude = _summarize_claude_event({"type": "assistant", "message": {
        "usage": {"input_tokens": 300, "cache_read_input_tokens": 8000, "output_tokens": 20}}})[0]
    assert claude["reported_token_usage"]["raw_reported"]["cache_read_input_tokens"] == 8000


def test_signed_call_observation_keeps_existing_receipt_and_exact_compaction_locator(tmp_path):
    directory = tmp_path / "run" / "proposal" / "job"
    directory.mkdir(parents=True)
    prompt, schema, stdout, result = [directory / name for name in (
        "prompt.txt", "output-schema.json", "stdout.jsonl", "result.json")]
    prompt.write_text("真实输入", encoding="utf-8")
    schema.write_text("{}")
    result.write_text("{}")
    events = [{"type": "thread.started", "thread_id": "native"},
              {"type": "context_compacted", "summary": "uncertain: missing independent cohort"},
              {"type": "turn.completed", "usage": {"input_tokens": 1000,
                  "cached_input_tokens": 800, "output_tokens": 200}}]
    raw = b"".join(canonical_json(event).encode() + b"\n" for event in events)
    stdout.write_bytes(raw)
    receipt_path, key = directory / "supervisor-exit.json", b"k" * 32
    write_exit_receipt(receipt_path, key=key, invocation_hash="a" * 64,
        prompt_path=prompt, schema_path=schema, stdout_path=stdout, result_path=result,
        returncode=0, input_bytes=len(prompt.read_bytes()))
    receipt, _ = read_verified_exit_receipt(receipt_path, key=key, invocation_hash="a" * 64,
        prompt_path=prompt, schema_path=schema, stdout_path=stdout, result_path=result)
    assert "usage_events" not in receipt
    observation = read_transport_envelope(directory / "call-observation.json", key)
    assert observation["prompt_utf8_bytes"] == len(prompt.read_bytes())
    assert observation["model_call_usage_available"] is False
    assert observation["usage_events"][0]["input_tokens"] == 1000
    compacted = observation["compactions"][0]
    source = raw[compacted["source_byte_offset"]:][:compacted["source_line_bytes"]]
    assert json.loads(source) == events[1]
    assert hashlib.sha256(canonical_json(events[1]).encode()).hexdigest() == compacted["event_content_sha256"]
    from meta_research.quest_drafting import _remove_durable_job
    from meta_research.provider_call_observations import compaction_observation
    # Harness summaries retain only metadata; only the durable sidecar keeps the event.
    assert "event" not in compaction_observation(events[1])
    _remove_durable_job(directory)
    assert not stdout.exists() and not directory.exists()
    archived = read_transport_envelope(
        tmp_path / "provider-observations/proposal/job-call-observation.json", key)
    recovered = archived["compactions"][0]
    assert recovered["event"] == events[1]
    assert canonical_hash(recovered["event"]) == recovered["event_content_sha256"]


def test_persisted_compaction_content_respects_event_and_line_limits(tmp_path):
    prompt, stdout = tmp_path / "prompt.txt", tmp_path / "stdout.jsonl"
    prompt.write_text("Inspect evidence boundaries.")
    stdout.write_bytes(b"".join(canonical_json({"type": "context_compacted",
        "summary": f"boundary {index}"}).encode() + b"\n" for index in range(129)))
    observation = observe_provider_files(prompt, stdout)
    assert observation["limited"] is True
    assert len(observation["compactions"]) == 128
    assert observation["compactions"][-1]["event"]["summary"] == "boundary 127"
    stdout.write_bytes(canonical_json({"type": "context_compacted",
        "summary": "x" * (8 * 1024 * 1024)}).encode() + b"\n")
    oversized = observe_provider_files(prompt, stdout)
    assert oversized["limited"] is True and oversized["compactions"] == []


def test_signed_native_session_sample_survives_append_and_proposal_cleanup(tmp_path):
    from meta_research.quest_drafting import _remove_durable_job
    session = "01a09bb2-95ed-7931-bd38-3b8ebd2de28b"
    native = tmp_path / "codex-home/sessions/2026/09/13" / ("rollout-2026-09-13T16-56-07-" + session + ".jsonl")
    native.parent.mkdir(parents=True)
    usage = {"input_tokens": 241036, "cached_input_tokens": 240000,
        "output_tokens": 762, "cache_write_input_tokens": 0, "total_tokens": 241798}
    response = {"type": "token_usage_record", "timestamp": "2026-09-13T16:56:26Z",
        "payload": {"thread_id": session, "session_id": session, "response_id": "resp-one",
            "turn_id": "turn-one", "root_turn_id": "turn-one", "usage": usage,
            "turn_token_usage": usage, "thread_token_usage": usage}}
    snapshot = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": usage, "total_token_usage": usage, "model_context_window": 258400}}}
    compacted = {"type": "compacted", "payload": {"message": "",
        "replacement_history": [{"type": "compaction", "encrypted_content": "opaque-state"}],
        "guardian_history": [{"type": "message", "role": "user", "content": "uncertain: missing cohort"}]}}
    raw = b"".join(canonical_json(item).encode() + b"\n" for item in (
        {"type": "session_meta", "payload": {"id": session}}, response, response, snapshot, snapshot, compacted))
    native.write_bytes(raw)
    directory = tmp_path / "run/proposal/job"
    directory.mkdir(parents=True)
    prompt, schema, stdout, result = [directory / name for name in (
        "prompt.txt", "output-schema.json", "stdout.jsonl", "result.json")]
    prompt.write_text("Research within the selected Target.")
    schema.write_text("{}")
    result.write_text("{}")
    stdout.write_text(canonical_json({"type": "thread.started", "thread_id": session}) + "\n")
    key = b"k" * 32
    write_exit_receipt(directory / "supervisor-exit.json", key=key, invocation_hash="a" * 64,
        prompt_path=prompt, schema_path=schema, stdout_path=stdout, result_path=result,
        returncode=0, input_bytes=len(prompt.read_bytes()), codex_home=tmp_path / "codex-home")
    observation = read_transport_envelope(directory / "call-observation.json", key)
    sample = observation["native_session"]
    assert observation["model_call_usage_available"] is True
    assert sample["status"] == "available" and sample["session_id"] == session
    assert sample["observed_prefix_bytes"] == len(raw)
    assert sample["observed_prefix_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(sample["model_responses"]) == 1 and sample["duplicate_response_records"] == 1
    assert sample["model_responses"][0]["response_id"] == "resp-one"
    assert sample["model_responses"][0]["input_tokens"] == 241036
    assert len(sample["last_token_usage_snapshots"]) == 1 and sample["duplicate_token_snapshots"] == 1
    assert sample["model_context_windows"] == [258400]
    assert sample["compactions"][0]["event"] == compacted
    native.write_bytes(raw + canonical_json({**response, "timestamp": "later"}).encode() + b"\n")
    _remove_durable_job(directory)
    archived = read_transport_envelope(tmp_path / "provider-observations/proposal/job-call-observation.json", key)
    assert archived["native_session"] == sample
    assert canonical_hash(sample["compactions"][0]["event"]) == sample["compactions"][0]["event_content_sha256"]


def test_native_session_authentication_and_bounded_sampling_keep_stdout_fallback(tmp_path, monkeypatch):
    import meta_research.provider_call_observations as observations
    session = "01a09bb2-95ed-7931-bd38-3b8ebd2de28b"
    native = tmp_path / "codex-home/sessions/2026/09/13" / ("rollout-2026-09-13T16-56-07-" + session + ".jsonl")
    native.parent.mkdir(parents=True)
    prompt, stdout = tmp_path / "prompt", tmp_path / "stdout"
    prompt.write_text("required input")
    stdout.write_text(canonical_json({"type": "thread.started", "thread_id": session}) + "\n" +
        canonical_json({"type": "turn.completed", "usage": {"input_tokens": 100}}) + "\n")
    native.write_text(canonical_json({"type": "session_meta", "payload": {"id": "unrelated"}}) + "\n")
    mismatch = observe_provider_files(prompt, stdout, codex_home=tmp_path / "codex-home")
    assert mismatch["native_session"]["reason"] == "native_session_metadata_mismatch"
    assert mismatch["usage_events"][0]["input_tokens"] == 100
    assert mismatch["model_call_usage_available"] is False
    metadata = canonical_json({"type": "session_meta", "payload": {"id": session}}).encode() + b"\n"
    events = b"".join(canonical_json({"type": "compacted", "payload": {"message": str(i)}}).encode() + b"\n" for i in range(129))
    native.write_bytes(metadata + events)
    limited = observe_provider_files(prompt, stdout, codex_home=tmp_path / "codex-home")
    assert limited["limited"] is True and len(limited["native_session"]["compactions"]) == 128
    monkeypatch.setattr(observations, "_NATIVE_PREFIX_LIMIT", len(metadata) + 5)
    prefix = observe_provider_files(prompt, stdout, codex_home=tmp_path / "codex-home")["native_session"]
    assert prefix["limited"] and prefix["observed_prefix_bytes"] == len(metadata) + 5
    assert prefix["observed_prefix_sha256"] == hashlib.sha256((metadata + events)[:len(metadata) + 5]).hexdigest()
    assert prefix["compactions"] == []
    monkeypatch.setattr(observations, "_NATIVE_PREFIX_LIMIT", 5)
    assert observe_provider_files(prompt, stdout, codex_home=tmp_path / "codex-home")["native_session"]["status"] == "unavailable"


def test_required_current_contract_and_selected_inputs_survive_context_budget(caplog):
    contract = {"rules": "MUST PRESERVE " * 10000}
    selected = {"path": "/immutable/input.csv", "version_ref": "asset-v7"}
    pack = {"execution_contract": contract, "selected_input_manifest": selected,
            "history": [{"body": "old " * 10000} for _ in range(100)]}
    with caplog.at_level("INFO", logger="meta_research.context_presentation"):
        view = stage_context_view("reasoning", pack, context_pack_ref="ctx", context_pack_hash=canonical_hash(pack))
    assert view["required_material"]["execution_contract"] == contract
    assert view["required_material"]["selected_input_manifest"] == selected
    optional = {key: value for key, value in view.items() if key != "required_material"}
    assert len(canonical_json(optional).encode()) <= CONTEXT_VIEW_MAX_BYTES
    assert "context_projection" in caplog.text and "compression_location" in caplog.text


class _SystemEvidenceReader:
    def verify_target_root_completion_evidence(self, *, handle, evidence, handoff):
        assert handoff is None and evidence.target_ref == handle.target_ref
        return canonical_hash({"evidence_ref": evidence.evidence_ref,
                               "final_text_sha256": evidence.final_text_sha256})


def test_research_note_and_final_statement_are_immutable_assets_with_replay(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, old = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "outputs/result.json").write_bytes((workspace / "outputs/metrics.json").read_bytes())
        analysis = workspace / "outputs/analysis"
        analysis.mkdir()
        body = "Current understanding: uncertain. Missing independent cohort.\n" * 100
        (analysis / "research-note.md").write_text(body, encoding="utf-8")
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        final_text = "八项审计完成；uncertain，外部泛化证据仍缺。"
        evidence = replace(old, handoff=None, workspace_ref=workspace_ref,
                           final_text=final_text,
                           final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        manifest = memory.query(accepted.manifest_ref)
        notes = manifest_research_notes(manifest)
        assert {note["kind"] for note in notes} == {"research_note", "final_statement"}
        note = next(note for note in notes if note["kind"] == "research_note")
        assert note["source_bytes_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert note["summary"]["truncated"] and len(note["summary"]["text"].encode()) <= 2048
        assert read_note_body(runtime.owners.research_memory, note) == body
        statement = next(note for note in notes if note["kind"] == "final_statement")
        assert runtime.owners.research_memory.materialize_asset(statement["version_ref"]).content == final_text.encode()
        # Exercise the actual composition wrapper used by Target reading contexts.
        target_memory = runtime.target_run_authorities.research_memory
        assert target_memory.query_research_notes_for_target(handle.target_ref) == (
            runtime.owners.research_memory.query_research_notes_for_target(handle.target_ref))
        body_path = materialize_note_body(target_memory, note, tmp_path / "reading-context")
        assert Path(body_path).read_bytes() == body.encode()
        with runtime._database.read() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM rm_asset_versions")).scalar_one()
        (analysis / "research-note.md").write_text("Later, unrelated understanding.")
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        assert manifest_research_notes(memory.query(accepted.manifest_ref)) == notes
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rm_asset_versions")).scalar_one() == count
        completion = lifecycle.query_completion(handle.target_ref)
        lifecycle.reject_completion(completion=completion, manifest_ref=manifest.manifest_ref,
            issuer="research_graph", rejection_ref="test_note_revision",
            code="target_root_result_schema_rejected", feedback="Clarify the evidence boundary.",
            receipt=AcceptanceReceipt(issuer="research_graph", kind="target_root_completion_rejected",
                receipt_ref="test_note_revision_receipt", subject_ref=completion.completion_ref,
                payload_hash=canonical_hash({"test": "owner revision"})),
            idempotency_key="test_note_revision")
        successor_evidence = replace(evidence, operation_ref="second_note_turn",
            operation_generation=2, evidence_ref="second_note_evidence",
            evidence_sequence=20, observed_at=2.0)
        successor = finalizer.finalize(handle=handle, evidence=successor_evidence)
        new_manifest = memory.query(successor.manifest_ref)
        new_note = next(item for item in manifest_research_notes(new_manifest)
                        if item["kind"] == "research_note")
        assert new_note["asset_ref"] == note["asset_ref"]
        assert new_note["version_ref"] != note["version_ref"]
        asset = runtime.owners.research_memory.query_asset_version(new_note["version_ref"])
        assert asset.provenance["predecessor_version_ref"] == note["version_ref"]
        assert manifest_research_notes(memory.query(accepted.manifest_ref)) == notes
    finally:
        runtime.close()


def test_historical_statement_backfill_preserves_legacy_manifest_and_scoped_body(tmp_path, monkeypatch):
    runtime, lifecycle, memory, authority, handle, workspace, old = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "outputs/result.json").write_bytes((workspace / "outputs/metrics.json").read_bytes())
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        body = "uncertain: eight audit criteria assessed; independent generalization evidence is missing."
        evidence = replace(old, handoff=None, workspace_ref=workspace_ref,
            final_text=body, final_text_sha256=hashlib.sha256(body.encode()).hexdigest())
        reader = _SystemEvidenceReader()
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=reader, measurement_authority=runtime.owners.research_graph)
        original_freeze = finalizer._freeze
        def legacy_freeze(**arguments):
            arguments["final_text"] = None
            return original_freeze(**arguments)
        monkeypatch.setattr(finalizer, "_freeze", legacy_freeze)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query(accepted.manifest_ref)
        assert manifest_research_notes(manifest) == []
        with runtime._database.read() as connection:
            original = tuple(connection.execute(text("SELECT * FROM rm_target_root_completion_manifests "
                "WHERE manifest_ref=:ref"), {"ref": accepted.manifest_ref}).one())
            scope = connection.execute(text("SELECT g.quest_ref,r.question_ref FROM rg_targets t "
                "JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
                "JOIN ae_stage_run_requests r ON r.request_ref=g.request_ref WHERE t.target_ref=:ref"),
                {"ref": handle.target_ref}).one()
        note = memory.accept_historical_research_note(manifest_ref=manifest.manifest_ref,
            evidence=evidence, evidence_reader=reader)
        assert memory.accept_historical_research_note(manifest_ref=manifest.manifest_ref,
            evidence=evidence, evidence_reader=reader) == note
        assert memory.query_research_notes_for_target(handle.target_ref) == [note]
        page = runtime.owners.research_memory.query_question_research_notes(
            quest_ref=scope.quest_ref, question_ref=scope.question_ref)
        assert page["items"] == [note]
        exact = runtime.owners.research_memory.read_question_research_note(
            quest_ref=scope.quest_ref, question_ref=scope.question_ref, version_ref=note["version_ref"])
        assert exact["body"] == body
        with pytest.raises(OwnerConflict, match="research_note_source_unbound"):
            runtime.owners.research_memory.read_question_research_note(
                quest_ref=scope.quest_ref, question_ref="foreign_question", version_ref=note["version_ref"])
        with pytest.raises(OwnerConflict, match="target_root_completion_evidence_invalid"):
            memory.accept_historical_research_note(manifest_ref=manifest.manifest_ref,
                evidence=replace(evidence, final_text="forged"), evidence_reader=reader)
        with runtime._database.read() as connection:
            assert tuple(connection.execute(text("SELECT * FROM rm_target_root_completion_manifests "
                "WHERE manifest_ref=:ref"), {"ref": accepted.manifest_ref}).one()) == original
            assert connection.execute(text("SELECT COUNT(*) FROM rm_target_research_notes")).scalar_one() == 1
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()
