"""Actual Owner reads and scoped grants; no model or injected handler failures."""
import json
import os
from types import SimpleNamespace

import pytest

from meta_research.owners.common import canonical_json
from meta_research.recorder_sources import READ_SOURCE
from test_creation_material_consumption import _call as gateway_call
from test_public_plan_stage import _confirm_direct_quest, _owner_revisions
from test_root_workspace import _make, _channel, _call


def _idea_channel(runtime):
    quest = _confirm_direct_quest(runtime)
    admitted = runtime.idea_stage.start("reader-diagnostic")
    request_ref = admitted["stage_run_request"]["request_ref"]
    run = runtime.owners.agent_runtime.query_idea_stage_run(request_ref)
    channel = _channel(runtime, SimpleNamespace(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
        fence_ref=run.fence_ref, capability_binding_hash=run.runtime_binding_hash,
        root_kind="idea", phase="primary"))
    request = runtime.owners.advancement_engine.query_idea_stage_request(run.cycle_ref)
    return quest, request, channel


@pytest.mark.parametrize("invalid_refs", [{}, {"environment_ref": "extra", "environment_reference_ref": "extra"}])
def test_environment_reference_diagnostic_corrects_in_original_grant(tmp_path, invalid_refs):
    runtime = _make(tmp_path)
    try:
        _confirm_direct_quest(runtime)
        admitted = runtime.idea_stage.start("environment-reference-diagnostic")
        run = runtime.owners.agent_runtime.query_idea_stage_run(admitted["stage_run_request"]["request_ref"])
        channel = _channel(runtime, SimpleNamespace(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref, capability_binding_hash=run.runtime_binding_hash,
            root_kind="idea", phase="primary"))
        registered = _call(runtime, channel, "research_graph.environments.register",
            effect_id="retained-environment", semantic_key="existing-equipment", name="Existing equipment",
            source="Controlled local equipment fixture", meaning="Retained usable equipment description")
        assert not registered["isError"], registered
        original = registered["structuredContent"]["result"]
        before = _owner_revisions(runtime)
        rejected = _call(runtime, channel, "research_graph.environments.read", **invalid_refs)
        failure = rejected["structuredContent"]
        assert rejected["isError"] and failure["code"] == "environment_read_ref_invalid"
        assert failure["details"]["fields"] == ["environment_ref", "environment_reference_ref"]
        assert "exactly one" in failure["details"]["message"]
        assert "environment_ref" in failure["details"]["correction"]
        assert json.loads(rejected["content"][0]["text"]) == failure
        continued = _call(runtime, channel, "research_graph.environments.read",
                          environment_ref=original["environment_ref"])
        assert not continued["isError"], continued
        assert continued["structuredContent"]["result"] == original
        reconciled = _call(runtime, channel, "research_graph.environments.register.reconcile",
                           effect_id="retained-environment")
        assert reconciled["structuredContent"]["result"] == original
        assert _owner_revisions(runtime) == before
    finally:
        runtime.close()


def test_recorder_offset_diagnostic_corrects_in_same_frozen_job(tmp_path):
    runtime = _make(tmp_path)
    try:
        quest = _confirm_direct_quest(runtime)
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:18770")
        channels = runtime.recorder_channels
        node = next(item for item in channels.reader.read(quest["quest_ref"]) if item["kind"] == "question")
        source_ref = node["sources"][0]["ref"]
        expected = channels.reader.read_source(quest["quest_ref"], node, source_ref)
        serialized = canonical_json(expected)
        basis = {"quest_ref": quest["quest_ref"], "nodes": [node]}
        before = _owner_revisions(runtime)
        with channels.grant("retained-recorder-job", basis, "retained-native-session") as connection:
            arguments = {"node_key": node["node_key"], "source_ref": source_ref}
            rejected = gateway_call(channels.gateway, connection, READ_SOURCE, **arguments, offset=len(serialized))
            failure = rejected["structuredContent"]
            assert rejected["isError"] and failure["code"] == "content_offset_invalid"
            assert failure["details"]["path"] == ["offset"]
            assert failure["details"]["maximum"] == len(serialized) - 1
            assert "next_offset" in failure["details"]["correction"]
            assert json.loads(rejected["content"][0]["text"]) == failure
            parts, offset = [], 0
            while offset is not None:
                continued = gateway_call(channels.gateway, connection, READ_SOURCE, **arguments, offset=offset)
                assert not continued["isError"], continued
                page = continued["structuredContent"]
                assert page["source_hash"] == node["source_hash"]
                parts.append(page["text"])
                offset = page["next_offset"]
            assert "".join(parts) == serialized
            assert channels.is_current(connection.token)
            assert _owner_revisions(runtime) == before
    finally:
        runtime.close()


@pytest.mark.parametrize("reader", ["history", "relations", "human", "stage"])
def test_owner_pagination_details_survive_actual_reader_and_grant(tmp_path, reader):
    runtime = _make(tmp_path)
    try:
        quest, request, channel = _idea_channel(runtime)
        operation, arguments = {
            "history": ("research_graph.question_history.read", {"question_ref": quest["question_ref"]}),
            "relations": ("research_graph.question_relations.read", {"question_ref": quest["question_ref"]}),
            "human": ("human_request.read", {"entry_kind": "guidance"}),
            "stage": ("research_memory.stage_context.read", {
                "context_pack_ref": request.context_pack_ref, "source": "question_history",
                "path": [], "offset": 0, "limit": 8192}),
        }[reader]
        before = _owner_revisions(runtime)
        rejected = _call(runtime, channel, operation, **arguments, query_revision="0" * 64)
        failure = rejected["structuredContent"]
        assert rejected["isError"] and failure["code"] == "research_query_changed"
        field = {"stage": "index_offset", "human": "guidance_offset"}.get(reader, "offset")
        assert failure["details"]["path"] == [field]
        assert failure["details"]["message"] and failure["details"]["correction"]
        assert json.loads(rejected["content"][0]["text"]) == failure
        continued = _call(runtime, channel, operation, **arguments)
        assert not continued["isError"], continued
        if reader == "stage":
            required = _call(runtime, channel, operation, **arguments, index_offset=1)
            detail = required["structuredContent"]["details"]
            assert required["structuredContent"]["code"] == "research_query_revision_required"
            assert detail["path"] == ["query_revision"]
            assert "index_offset=0" in detail["correction"] and "offset=0" in detail["correction"]
            revision = continued["structuredContent"]["index_page"]["query_revision"]
            next_page = _call(runtime, channel, operation, **arguments, index_offset=1, query_revision=revision)
            assert not next_page["isError"], next_page
        assert _owner_revisions(runtime) == before
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["path", "offset", "limit"])
def test_context_page_reports_field_without_echoing_content_and_continues(tmp_path, field):
    runtime = _make(tmp_path)
    try:
        _, request, channel = _idea_channel(runtime)
        operation = "research_memory.stage_context.read"
        arguments = {"context_pack_ref": request.context_pack_ref, "source": "question",
                     "path": [], "offset": 0, "limit": 8192}
        original = _call(runtime, channel, operation, **arguments)
        assert not original["isError"], original
        raw = original["structuredContent"]["text"].encode("utf-8")
        bad = {"path": ["private-selector-must-not-echo"], "offset": next(
            i for i, value in enumerate(raw) if value & 0xC0 == 0x80), "limit": 1}
        supplied = {field: bad[field]}
        if field == "limit":
            supplied["offset"] = next(i for i, value in enumerate(raw) if value >= 0xC0)
        rejected = _call(runtime, channel, operation, **{**arguments, **supplied})
        failure = rejected["structuredContent"]
        assert rejected["isError"]
        assert failure["details"]["path"] == [field]
        assert failure["details"]["message"] and failure["details"]["correction"]
        assert "private-selector-must-not-echo" not in rejected["content"][0]["text"]
        assert json.loads(rejected["content"][0]["text"]) == failure
        assert _call(runtime, channel, operation, **arguments)["structuredContent"] == original["structuredContent"]
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires protected creation grant")
def test_creation_source_missing_selector_is_argument_error_and_original_basis_survives(tmp_path):
    from fastapi.testclient import TestClient
    from meta_research.creation_basis import InitializationUnderstandingResult
    from meta_research.web import create_app
    from test_creation_reference_basis import _understood
    from test_creation_material_copy import _native
    source = tmp_path / "original.txt"
    source.write_bytes(b"original 16\n")
    runtime = _make(tmp_path / "runtime")
    work = None
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            request, value, identity, prior, _, _, binding = _understood(runtime, client, source, "basis-reader")
            basis = runtime.owners.research_memory.creation_bases.accept_reference_prepared(request,
                InitializationUnderstandingResult(value, input_identity=identity, work=prior))
            work = runtime.root_workspaces.protected_creation(binding, request.inputs, operation_ref="basis-read")
            work.bind_context_readers(basis, None)
            assert _native(work, "echo ready").returncode == 0
            channel = work.channel(("research_memory.creation_basis.read",))
            gateway = runtime.harnesses._gateway
            arguments = {"basis_ref": basis["basis_ref"], "expected_basis_hash": basis["basis_hash"], "view": "source"}
            material = basis["sources"][0]
            original = gateway_call(gateway, channel, "research_memory.creation_basis.read", **arguments,
                                    material_key=material["material_key"])
            assert not original["isError"], original
            rejected = gateway_call(gateway, channel, "research_memory.creation_basis.read", **arguments)
            failure = rejected["structuredContent"]
            assert rejected["isError"] and failure["code"] == "semantic_input_schema_mismatch"
            assert failure["details"]["path"] == ["material_key"]
            assert failure["details"]["correction"]
            assert json.loads(rejected["content"][0]["text"]) == failure
            continued = gateway_call(gateway, channel, "research_memory.creation_basis.read", **arguments,
                                      material_key=material["material_key"])
            assert not continued["isError"], continued
            assert continued["structuredContent"] == original["structuredContent"]
            assert continued["structuredContent"]["text"] == "original 16\n"
            assert continued["structuredContent"]["basis_hash"] == basis["basis_hash"]
            page_failure = gateway_call(gateway, channel, "research_memory.creation_basis.read",
                **{**arguments, "view": "understanding"}, offset=10**9)
            assert page_failure["structuredContent"]["code"] == "stage_context_offset_invalid"
            assert page_failure["structuredContent"]["details"]["path"] == ["offset"]
            assert source.read_bytes() == b"original 16\n"
    finally:
        if work is not None and not work._terminal:
            work.fail()
        runtime.close()


def test_self_relation_rejected_then_original_effect_can_be_corrected(tmp_path):
    from test_public_reasoning_stage import (_reasoning_runtime, _DeterministicReasoningSkill,
        _confirm_deepfetch_quest, _finish_idea_stage, _tick_reasoning)
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_first_question_deepfetch import RecordingAcquisitionProvider
    runtime = _reasoning_runtime(tmp_path, reasoning_skill=_DeterministicReasoningSkill(),
                                 acquisition_provider=RecordingAcquisitionProvider())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        human = runtime.owners.human_collaboration
        seeded = _confirm_waived_manual_question(human, quest_ref=quest["quest_ref"],
            parent_question_ref=quest["question_ref"], key_prefix="existing-related-question")
        for _ in range(10):
            manual = human.query_manual_question_creation(seeded["context_ref"])
            if manual["status"] == "completed":
                break
            assert human.reconcile_once()
        assert manual["status"] == "completed"
        other = next(item.question_ref for item in runtime.owners.research_graph.query_question_tree(
            quest_ref=quest["quest_ref"]) if item.question_ref != quest["question_ref"])
        _finish_idea_stage(runtime)
        _tick_reasoning(runtime)
        current = _tick_reasoning(runtime)
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(current["stage_run_request"]["request_ref"])
        channel = _channel(runtime, SimpleNamespace(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash, root_kind="reasoning", phase="primary"))
        operation = "research_graph.question_relations.record"
        arguments = {"effect_id": "original-relation", "left_question_ref": quest["question_ref"],
            "right_question_ref": quest["question_ref"], "notes": "Existing questions share the same unresolved mechanism."}
        rejected = _call(runtime, channel, operation, **arguments)
        failure = rejected["structuredContent"]
        assert rejected["isError"] and failure["code"] == "question_relation_invalid"
        assert failure["details"]["fields"] == ["left_question_ref", "right_question_ref"]
        assert "different" in failure["details"]["message"]
        assert json.loads(rejected["content"][0]["text"]) == failure
        absent = _call(runtime, channel, operation + ".reconcile", effect_id=arguments["effect_id"])
        assert absent["structuredContent"]["status"] == "not_found"
        corrected = _call(runtime, channel, operation, **{**arguments, "right_question_ref": other})
        assert not corrected["isError"], corrected
        original = corrected["structuredContent"]
        assert _call(runtime, channel, operation + ".reconcile", effect_id=arguments["effect_id"])["structuredContent"]["relation"] == original
    finally:
        runtime.close()
