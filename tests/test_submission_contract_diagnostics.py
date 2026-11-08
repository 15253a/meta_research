"""Correct rejected submissions through the original authorized tool channel."""
import json

import pytest

from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpGateway
from meta_research.target_run_semantic import target_run_semantic_operations
from test_target_root_finalizer import _root_finalizer_fixture
from test_dataset_effect_scope_recovery import _scope
from test_root_workspace import _make, _external_context, _channel
from test_public_plan_stage import _confirm_direct_quest
from test_formal_run_snapshots import _scenario
from meta_research.human_guidance_operations import human_guidance_operations
from test_human_guidance_inboxes import _InboxIdea, _run_idea, _finish
from test_root_human_request_lifecycle import _open_arguments


def _call(gateway, channel, name, **arguments):
    status, response = gateway.dispatch(channel.token, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert status == 200, response
    result = response["result"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result


@pytest.mark.parametrize("arguments,field", [
    ({"mode": "raw", "stream_ref": "stream", "before": 0}, "log_ref"),
    ({"mode": "raw", "log_ref": "log", "before": 0}, "stream_ref"),
    ({"mode": "raw", "log_ref": "log", "stream_ref": "stream"}, "before"),
    ({"mode": "raw", "log_ref": "log", "stream_ref": "stream", "before": 0,
      "cursor": "cursor"}, "cursor"),
    ({"mode": "progress", "log_ref": "log"}, "log_ref"),
])
def test_target_progress_explains_mode_fields_and_same_grant_continues(tmp_path, arguments, field):
    runtime, _, _, _, handle, _, _ = _root_finalizer_fixture(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        run = owner.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        gateway = SemanticMcpGateway(target_run_semantic_operations(
            agent_runtime=owner, target_agent=runtime.target_run_authorities.agent_runtime))
        channel, _ = gateway.issue_channel(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.capability_binding_hash,
            operation_ids=("agent_runtime.target_run.progress",),
            root_kind="target", phase="target_root_lifecycle")
        rejected = _call(gateway, channel, "agent_runtime.target_run.progress",
                         target_ref=handle.target_ref, **arguments)
        assert rejected["isError"]
        error = rejected["structuredContent"]
        assert error["code"] == "experiment_log_cursor_invalid"
        assert field in error["details"]["message"]
        assert "mode" in error["details"]["message"]
        assert error["details"]["correction"]

        corrected = _call(gateway, channel, "agent_runtime.target_run.progress",
                          target_ref=handle.target_ref, mode="progress")
        assert not corrected["isError"], corrected
        assert corrected["structuredContent"]["fragments"][0]["text"] == "epoch 1 complete\n"
        assert corrected["structuredContent"]["target_run_ref"] == run.run_ref
    finally:
        runtime.close()


def test_material_acquire_requires_one_source_before_recording_and_same_effect_can_continue(tmp_path):
    source = tmp_path / "observation.txt"
    source.write_text("Retained uncalibrated observation.\n")
    runtime, _, _, handle, _, _ = _scenario(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        owner.harness_runs.start_operation(run_ref=handle.target_run_ref,
            operation_ref="material-diagnostic-turn", generation=1, invocation_hash="a" * 64, resume=False)
        run = owner.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.capability_binding_hash, "target", "target_root", "")
        channel = _channel(runtime, context).connection
        gateway = runtime.harnesses._gateway
        command = {"effect_id": "original-acquisition", "description": "Acquire the reported observation."}
        for invalid in ({}, {"selection": {}, "absolute_path": str(source)}):
            rejected = _call(gateway, channel, "research_workspace.materials.acquire", **command, **invalid)
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == "material_acquisition_source_invalid"
            assert "exactly one" in error["details"]["message"]
            assert all(name in error["details"]["message"] for name in ("selection", "absolute_path"))
            assert error["details"]["execution_stage"] == "before_effect_reservation"
            assert error["details"]["effect_status"] == "not_recorded"
            absent = _call(gateway, channel, "research_workspace.materials.acquire.reconcile",
                           effect_id=command["effect_id"])
            assert absent["structuredContent"] == {"state": "absent"}

        acquired = _call(gateway, channel, "research_workspace.materials.acquire",
                         **command, absolute_path=str(source))
        assert not acquired["isError"], acquired
        assert len(acquired["structuredContent"]["references"]) == 1
        reconciled = _call(gateway, channel, "research_workspace.materials.acquire.reconcile",
                          effect_id=command["effect_id"])
        assert reconciled["structuredContent"] == acquired["structuredContent"]
        assert source.read_text() == "Retained uncalibrated observation.\n"
    finally:
        runtime.close()


def test_guidance_read_names_missing_selection_before_effect_and_same_operation_continues(tmp_path):
    observed = {}

    def primary(human, operation):
        cut = human.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        scope = operation.scope
        operations = human_guidance_operations(human)
        gateway = SemanticMcpGateway(operations)
        channel, _ = gateway.issue_channel(
            run_ref=scope.run_ref, attempt_ref=scope.attempt_ref,
            root_session_ref=scope.root_session_ref, fence_ref=scope.fence_ref,
            capability_binding_hash=scope.runtime_binding_hash,
            operation_ids=tuple(item.semantic_operation_id for item in operations),
            root_kind="idea", phase="primary", guidance_binding=cut.binding)
        observed["errors"] = [
            _call(gateway, channel, "human_guidance.read", **arguments)
            for arguments in ({"effect_id": "original-read"}, {"offset": 1},
                              {"delivery_ref": delivery.delivery_ref})]
        observed["before"] = human.query_guidance_deliveries(observed["guide_ref"])
        command = {"delivery_ref": delivery.delivery_ref, "effect_id": "original-read"}
        observed["corrected"] = _call(gateway, channel, "human_guidance.read", **command)
        observed["reconciled"] = _call(gateway, channel, "human_guidance.read.reconcile", **command)

    provider = _InboxIdea(primary, lambda human, operation: None)
    runtime, quest = _run_idea(tmp_path, provider)
    try:
        guide = runtime.owners.human_collaboration.submit_human_guidance(
            quest_ref=quest, original_text="Preserve the independent audit.", idempotency_key="diagnostic-guidance")
        observed["guide_ref"] = guide["constraint_ref"]
        _finish(runtime)
        for rejected, code, field in zip(observed["errors"],
                ("guidance_delivery_required", "guidance_delivery_required", "guidance_effect_id_invalid"),
                ("delivery_ref", "delivery_ref", "effect_id")):
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == code
            assert field in error["details"]["message"]
            assert error["details"]["execution_stage"] == "before_effect_lookup"
            assert error["details"]["effect_status"] == "unknown"
            assert error["details"]["call_execution"] == "not_started"
        assert all(item["read_at"] is None for item in observed["before"])
        assert not observed["corrected"]["isError"]
        assert observed["corrected"]["structuredContent"]["original_text"] == "Preserve the independent audit."
        assert observed["reconciled"]["structuredContent"] == observed["corrected"]["structuredContent"]
    finally:
        runtime.close()


def test_human_request_conditions_and_reader_selectors_explain_fields_on_same_target_grant(tmp_path):
    runtime, _, _, handle, _, _ = _scenario(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        owner.harness_runs.start_operation(run_ref=handle.target_run_ref,
            operation_ref="human-request-diagnostic-turn", generation=1, invocation_hash="a" * 64, resume=False)
        run = owner.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.capability_binding_hash, "target", "target_root", "")
        channel = _channel(runtime, context).connection
        gateway = runtime.harnesses._gateway
        authorization = _open_arguments("original-help", "capability_authorization")
        missing = {key: value for key, value in authorization.items() if key != "required_authorization"}
        ordinary = _open_arguments("original-help", "offline_action")
        for arguments, field in ((missing, "required_authorization"),
                ({**ordinary, "required_authorization": authorization["required_authorization"]}, "required_authorization"),
                ({**ordinary, "acceptance_conditions": [" Exact observation. ", "Exact observation."]}, "acceptance_conditions")):
            rejected = _call(gateway, channel, "human_request.open", **arguments)
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == "root_agent_human_request_condition_invalid"
            assert field in error["details"]["message"]
            assert error["details"]["effect_status"] == "unknown"
            assert error["details"]["call_execution"] == "not_started"

        request_ref = "request:selected"
        for arguments, code, field in (
                ({"entry_kind": "responses"}, "research_help_selection_invalid", "request_ref"),
                ({"constraint_ref": "guidance", "request_ref": request_ref}, "research_help_selection_invalid", "constraint_ref"),
                ({"constraint_ref": "guidance", "response_ref": "response"}, "research_help_selection_invalid", "response_ref"),
                ({"constraint_ref": "guidance", "cursor": "cursor"}, "research_help_selection_invalid", "cursor"),
                ({"request_ref": request_ref, "cursor": "cursor"}, "research_help_cursor_invalid", "cursor")):
            rejected = _call(gateway, channel, "human_request.read", **arguments)
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == code
            assert field in error["details"]["message"]
            assert error["details"]["correction"]
        corrected_read = _call(gateway, channel, "human_request.read")
        assert not corrected_read["isError"], corrected_read
        assert json.loads(corrected_read["structuredContent"]["text"])["summary_only"] is True
        opened = _call(gateway, channel, "human_request.open", **ordinary)
        assert not opened["isError"], opened
        assert opened["structuredContent"]["request_ref"]
    finally:
        runtime.close()


def test_human_reader_reports_public_index_aliases_and_continues_exact_revision(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        human = runtime.owners.human_collaboration
        for index in range(13):
            human.submit_human_guidance(quest_ref=quest, original_text=f"Audit device {index}.",
                                        idempotency_key=f"alias-guidance-{index}")
            human.submit_research_input(quest_ref=quest, text_content=f"Observation {index}.",
                                        idempotency_key=f"alias-input-{index}")
        context = _external_context(_scope(runtime, quest_ref=quest))
        channel = _channel(runtime, context).connection
        gateway = runtime.harnesses._gateway
        for selectors, offset_name, revision_name, section in (
                ({"entry_kind": "guidance"}, "guidance_offset", "query_revision", None),
                ({}, "input_offset", "input_query_revision", "research_inputs")):
            first = _call(gateway, channel, "human_request.read", **selectors, limit=16384)
            assert not first["isError"], first
            value = json.loads(first["structuredContent"]["text"])
            page = value if section is None else value[section]
            assert page["next_offset"] == 12
            rejected = _call(gateway, channel, "human_request.read", **selectors,
                             **{offset_name: 12, revision_name: "0" * 64, "offset": 1})
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == "research_query_changed"
            assert error["details"]["path"] == [offset_name]
            assert f"{offset_name}=0" in error["details"]["correction"]
            assert "offset=0" in error["details"]["correction"]
            assert revision_name in error["details"]["correction"]
            continued = _call(gateway, channel, "human_request.read", **selectors,
                              **{offset_name: 12, revision_name: page["query_revision"], "offset": 0})
            assert not continued["isError"], continued
            value = json.loads(continued["structuredContent"]["text"])
            last_page = value if section is None else value[section]
            assert len(last_page["items"]) == 1
            assert last_page["query_revision"] == page["query_revision"]
    finally:
        runtime.close()


def test_asset_intake_explains_payload_and_source_fields_then_reuses_original_effect(tmp_path):
    runtime, _, _, handle, _, _ = _scenario(tmp_path)
    try:
        owner = runtime.owners.agent_runtime
        owner.harness_runs.start_operation(run_ref=handle.target_run_ref,
            operation_ref="asset-diagnostic-turn", generation=1, invocation_hash="a" * 64, resume=False)
        run = owner.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.capability_binding_hash, "target", "target_root", "")
        channel = _channel(runtime, context).connection
        gateway = runtime.harnesses._gateway
        intake = {"source_kind": "text", "custody_mode": "managed", "display_name": "observation.txt"}
        for payload, code, field in (
                ({}, "asset_source_payload_ambiguous", "exactly one"),
                ({"text": "observation", "source": {}}, "asset_source_payload_ambiguous", "target_workspace_note"),
                ({"source_kind": "local_path", "source": {"kind": "workspace", "path": "notes.txt"}},
                 "asset_source_selector_invalid", "workspace_ref"),
                ({"source_kind": "local_path", "source": {"kind": "material", "path": ""}},
                 "asset_source_selector_invalid", "reference_ref"),
                ({"source_kind": "local_path", "source": {"kind": "material", "path": "", "reference_ref": "ref", "workspace_ref": "other"}},
                 "asset_source_selector_invalid", "reference_ref"),
                ({"source_kind": "local_path", "source": {"kind": "other", "path": ""}},
                 "asset_source_selector_invalid", "kind")):
            rejected = _call(gateway, channel, "research_memory.assets.intake",
                             effect_id="original-intake", intake={**intake, **payload})
            assert rejected["isError"]
            error = rejected["structuredContent"]
            assert error["code"] == code
            assert field in error["details"]["message"]
            assert error["details"]["correction"]
            assert error["details"]["effect_status"] == "unknown"
            assert error["details"]["call_execution"] == "not_started"
        command = {"effect_id": "original-intake", "intake": {**intake, "text": "Retained reported observation.\n"}}
        accepted = _call(gateway, channel, "research_memory.assets.intake", **command)
        assert not accepted["isError"], accepted
        assert accepted["structuredContent"]["status"] == "accepted"
        malformed = _call(gateway, channel, "research_memory.assets.intake",
                          effect_id=command["effect_id"], intake=intake)
        assert malformed["isError"]
        prior_error = malformed["structuredContent"]
        assert prior_error["code"] == "asset_source_payload_ambiguous"
        assert prior_error["details"]["call_execution"] == "not_started"
        assert prior_error["details"]["effect_status"] == "unknown"
        assert "original" in prior_error["details"]["correction"]
        reconciled = _call(gateway, channel, "research_memory.assets.intake.reconcile", **command)
        assert reconciled["structuredContent"]["result"] == accepted["structuredContent"]
        asset = accepted["structuredContent"]["asset"]
        assert runtime.owners.research_memory.materialize_asset(asset["version_ref"]).content == b"Retained reported observation.\n"
    finally:
        runtime.close()
