"""Exercise public tool contracts against scoped, newly created Owner state."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.context_presentation import stage_context_view
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import research_content_operations
from meta_research.root_resident_mcp import RootResidentMcpChannels, RootResidentMcpError
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.stage_context_access import stage_context_operations
from test_dataset_effect_scope_recovery import _scope
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
from test_public_plan_stage import _confirm_direct_quest
from test_research_datasets import _asset
from test_target_root_finalizer import _root_finalizer_fixture


def _call(gateway, channel, operation, arguments):
    status, response = gateway.dispatch(channel.token, {
        "jsonrpc": "2.0", "id": "original-request", "method": "tools/call",
        "params": {"name": operation, "arguments": arguments},
    })
    assert status == 200
    assert response["id"] == "original-request"
    return response["result"]


def _channel(gateway, scope, *, root_kind="companion", operation_ids=None):
    channel, _ = gateway.issue_channel(
        run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind=root_kind,
        phase="primary", operation_ids=operation_ids or gateway.operation_ids,
    )
    return channel


def _notes_gateway(runtime):
    return SemanticMcpGateway(stage_context_operations(
        advancement_engine=runtime.owners.advancement_engine,
        agent_runtime=runtime.owners.agent_runtime,
        research_graph=runtime.owners.research_graph,
        research_memory=runtime.owners.research_memory,
    ))


def _question(runtime):
    created = _confirm_direct_quest(runtime)
    return runtime.owners.research_graph.query_question(created["initialization_id"])


def test_stage_note_pointer_is_callable_without_stripping_display_arguments(tmp_path):
    runtime = _bundle_runtime(tmp_path / "stage")
    try:
        _prepare_bundle_request(runtime)
        current = runtime.bundle_stage.query_current()
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            current["stage_run_request"]["cycle_ref"])
        assert runtime.bundle_stage.process_once()  # Admit the current Bundle root.
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        view = stage_context_view("bundle", request.context_pack,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash)
        assert view["section_delivery"]["accepted_formal_plan_binding"] == "complete"
        pointer = dict(view["research_notes_reader"])
        operation = pointer.pop("operation")
        gateway = _notes_gateway(runtime)
        channel = _channel(gateway, {
            key: getattr(run, key) for key in (
                "run_ref", "attempt_ref", "root_session_ref", "fence_ref", "runtime_binding_hash")
        }, root_kind="bundle")
        result = _call(gateway, channel, operation, pointer)
        assert result["isError"] is False, result
        catalog = json.loads(result["structuredContent"]["text"])
        assert catalog["summary_only"] is True
        assert catalog["items"] == []
        malformed = _call(gateway, channel, operation, {**pointer, "summary_only": True})
        assert malformed["structuredContent"] == {"code": "semantic_input_schema_mismatch"}
    finally:
        runtime.close()


def test_target_note_pointer_uses_its_real_authorized_owner_handler(tmp_path):
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = (
        _root_finalizer_fixture(tmp_path))
    try:
        runtime.owners.agent_runtime.harness_runs.start_operation(
            run_ref=handle.target_run_ref, operation_ref="reader-contract-turn",
            generation=1, invocation_hash="a" * 64, resume=False)
        with runtime._database.read() as connection:
            binding_hash = connection.execute(text(
                "SELECT capability_binding_hash FROM ar_harness_runs WHERE run_ref=:ref"),
                {"ref": handle.target_run_ref}).scalar_one()
        reading = runtime.target_run_authorities.research_graph.query_target_reading_context(
            target_ref=handle.target_ref, research_note_directory=tmp_path / "reading")
        pointer = dict(reading["research_notes_reader"])
        operation = pointer.pop("operation")
        gateway = _notes_gateway(runtime)
        channel = _channel(gateway, {
            "run_ref": handle.target_run_ref, "attempt_ref": handle.execution_attempt_ref,
            "root_session_ref": handle.root_session_ref, "fence_ref": handle.execution_fence_ref,
            "runtime_binding_hash": binding_hash,
        }, root_kind="target", operation_ids=(operation,))
        result = _call(gateway, channel, operation, pointer)
        assert result["isError"] is False, result
        assert json.loads(result["structuredContent"]["text"])["items"] == []
        malformed = _call(gateway, channel, operation, {**pointer, "summary_only": True})
        assert malformed["structuredContent"] == {"code": "semantic_input_schema_mismatch"}
    finally:
        runtime.close()


def test_public_content_keeps_three_distinct_pagination_endings_and_exact_errors(tmp_path):
    runtime = _bundle_runtime(tmp_path / "content")
    try:
        question = _question(runtime)
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        scope = _scope(runtime, quest_ref=question.quest_ref)
        gateway = SemanticMcpGateway(research_content_operations(
            research_graph=graph, research_memory=memory,
            agent_runtime=runtime.owners.agent_runtime))
        operation = "research_memory.content.read"
        channel = _channel(gateway, scope, operation_ids=(operation,))
        source = {"source_ref": question.question_ref, "version_ref": question.content_ref}
        chunks, offset = [], 0
        while True:
            result = _call(gateway, channel, operation, {**source, "offset": offset, "limit": 31})
            assert result["isError"] is False, result
            page = result["structuredContent"]
            chunks.append(page["text"].encode())
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert offset > 0 and page["complete"] is False
        assert hashlib.sha256(b"".join(chunks)).hexdigest() == page["content_hash"]
        assert json.loads(b"".join(chunks)) == memory.read_question_content(
            question.content_ref, question.content_hash)

        asset = _asset(runtime, content="研究原文α".encode())
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=question.quest_ref,
            idempotency_key="content-origin")
        file_page = _call(gateway, channel, operation, {
            "source_ref": asset.version_ref, "version_ref": asset.version_ref,
            "offset": 6, "limit": 65536})["structuredContent"]
        assert file_page["complete"] is True
        assert file_page["text"].encode() == "研究原文α".encode()[6:]

        directory = tmp_path / "directory"
        directory.mkdir()
        (directory / "a.txt").write_text("first", encoding="utf-8")
        (directory / "b.txt").write_text("second", encoding="utf-8")
        intake = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="local_path", custody_mode="linked_local", display_name="directory",
            source_locator=str(directory)), idempotency_key="directory")
        binding = intake.asset.as_binding()
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref,
            idempotency_key="directory-origin")
        directory_args = {"source_ref": binding.version_ref, "version_ref": binding.version_ref,
            "offset": 0, "limit": 1}
        first = _call(gateway, channel, operation, directory_args)["structuredContent"]
        assert first["offset_unit"] == "entries" and first["next_offset"] == 1
        last = _call(gateway, channel, operation,
            {**directory_args, "offset": first["next_offset"]})["structuredContent"]
        assert last["offset_unit"] == "entries" and last["next_offset"] is None

        bad_version = _call(gateway, channel, operation, {**source, "version_ref": "wrong-version"})
        assert bad_version["isError"] is True
        assert bad_version["structuredContent"] == {"code": "content_version_unbound"}
        assert _call(gateway, channel, operation, source)["isError"] is False
        for unavailable in ("research_memory.literature.page", "unregistered.read"):
            error = _call(gateway, channel, unavailable, {})
            assert error["isError"] is True
            assert error["structuredContent"] == {"code": "capability_unavailable"}
        gateway.revoke_channel(channel.token)
        status, revoked = gateway.dispatch(channel.token, {
            "jsonrpc": "2.0", "id": "revoked", "method": "tools/list"})
        assert status == 401
        assert revoked["error"]["code"] == "mcp_channel_authentication_required"
    finally:
        runtime.close()


def test_owner_rejection_details_and_original_effect_reconciliation_survive_error_wrapper(tmp_path):
    runtime = _bundle_runtime(tmp_path / "effects")
    try:
        question = _question(runtime)
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        binding = _asset(runtime)
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref,
            idempotency_key="protected-origin")
        scope = _scope(runtime, quest_ref=question.quest_ref)
        gateway = SemanticMcpGateway(research_content_operations(
            research_graph=graph, research_memory=memory,
            agent_runtime=runtime.owners.agent_runtime))
        channel = _channel(gateway, scope)
        lifecycle = _call(gateway, channel, "research_memory.assets.lifecycle",
            {"version_ref": binding.version_ref})["structuredContent"]
        arguments = {"version_ref": binding.version_ref, "expected_revision": lifecycle["revision"],
            "expected_reference_revision": lifecycle["reference_revision"],
            "explanation": "A proposed retirement must retain referenced research.",
            "low_value": True, "obsolete": True, "incorrect": True,
            "impact_understood": True, "has_explanation_value": False,
            "effect_id": "original-retirement-effect"}
        rejected = _call(gateway, channel, "research_memory.assets.retire", arguments)
        assert rejected["isError"] is True
        structured = rejected["structuredContent"]
        assert set(structured) == {"code", "details"}
        assert structured["code"] == "asset_retirement_blocked"
        assert structured["details"]["reasons"] == ["active_references"]
        assert structured["details"]["active_reference_refs"]
        assert structured["details"]["observed_reference_revision"] == lifecycle["reference_revision"]
        reconciled = _call(gateway, channel, "research_memory.assets.retire.reconcile", arguments)
        assert reconciled["isError"] is False
        assert reconciled["structuredContent"] == {"status": "not_found", "result": None}
        assert _call(gateway, channel, "research_memory.content.read", {
            "source_ref": binding.version_ref, "version_ref": binding.version_ref})["isError"] is False
    finally:
        runtime.close()


def test_resident_client_consumes_real_owner_error_code_without_a_status(monkeypatch, tmp_path):
    runtime = _bundle_runtime(tmp_path / "client")
    try:
        question = _question(runtime)
        scope = _scope(runtime, quest_ref=question.quest_ref)
        gateway = SemanticMcpGateway(research_content_operations(
            research_graph=runtime.owners.research_graph,
            research_memory=runtime.owners.research_memory,
            agent_runtime=runtime.owners.agent_runtime))
        operation = "research_memory.content.read"
        channel = _channel(gateway, scope, operation_ids=(operation,))
        channels = RootResidentMcpChannels("companion")
        channels.bind_authority(SimpleNamespace(dispatch_mcp=gateway.dispatch))
        # Reuse a real issued bearer; only acquisition/release are outside this client check.
        monkeypatch.setattr(channels, "acquire", lambda **_: ("issued-channel", channel))
        monkeypatch.setattr(channels, "release", lambda _key: None)
        credentials = {key: scope[key] for key in (
            "run_ref", "attempt_ref", "root_session_ref", "fence_ref")}
        credentials.update(capability_binding_hash=scope["runtime_binding_hash"],
            phase="primary", job_ref=None, operation_id=operation)
        with pytest.raises(RootResidentMcpError, match="^content_version_unbound$"):
            channels.call_operation(**credentials, arguments={
                "source_ref": question.question_ref, "version_ref": "wrong-version"})
        success = channels.call_operation(**credentials, arguments={
            "source_ref": question.question_ref, "version_ref": question.content_ref})
        assert success["source_ref"] == question.question_ref
        assert success["text"]
    finally:
        runtime.close()
