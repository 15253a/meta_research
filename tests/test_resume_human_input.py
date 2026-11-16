from dataclasses import replace
import json

import pytest

from meta_research.human_guidance import (
    FrozenGuidanceBinding, FrozenGuidanceCut, FrozenGuidanceDelivery,
    GuidanceOperationIdentity, GuidanceRuntimeScope, guidance_prompt,
    human_response_prompt, live_guidance_prompt, human_input_identities,
)
from meta_research.native_codex_bridge import NativeCodexBridge
from meta_research.native_guidance import native_human_input_history
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.provider_supervisor import ensure_transport_key, read_transport_envelope, write_transport_envelope
from test_native_codex_guidance import _guidance_message


def test_unrelated_or_malformed_prior_text_is_not_a_human_delivery_receipt():
    unrelated = '\n'.join((
        "human_input=not JSON", "human_input=[]", "human_input={}",
        '{"kind":"confirmed_guidance_change","change":{}}',
        '{"kind":"confirmed_guidance_change","change":[]}',
        '{"binding":{},"deliveries":[{"guide":null}]}',
        '{"binding":{},"deliveries":[{"guide":{"guidance":[]}}]}',
        '{"binding":{},"deliveries":null}',
    ))
    assert human_input_identities((unrelated,)) == set()


def test_guidance_first_delivery_and_changes_preserve_original_text_without_repeating_it():
    message = _guidance_message()
    guide = message["change"]["guide"]
    delivery = FrozenGuidanceDelivery("delivery-1", canonical_json(guide), True, None)
    cut = FrozenGuidanceCut(FrozenGuidanceBinding(GuidanceOperationIdentity("idea", "run", "op"),
        "quest-calibration", "snapshot", "a" * 64), "request", (delivery,))
    first = guidance_prompt(cut, include_body=True)
    assert first.count(guide["guidance"]["text"]) == 1
    assert guide["guidance"]["work_materials"]["files"][0]["path"] in first
    assert "snapshot" not in first and "assistant_understanding" not in first
    assert '"scope_confirmation":"legacy_unconfirmed"' in first
    renewed = replace(cut, deliveries=(replace(delivery, delivery_ref="delivery-2"),))
    association = guidance_prompt(renewed, include_body=True, previous_prompts=(first,))
    assert "delivery-2" in association and guide["guidance"]["text"] not in association
    treated = replace(renewed, deliveries=(replace(renewed.deliveries[0], needs_treatment=False),))
    assert guidance_prompt(treated, include_body=True, previous_prompts=(first,)) == ""
    assert guidance_prompt(renewed, include_body=True).count(guide["guidance"]["text"]) == 1
    withdrawn = replace(cut, deliveries=(replace(delivery, guidance_status="withdrawn"),))
    notice = guidance_prompt(withdrawn, previous_prompts=(first,))
    assert "withdrawn" in notice and "calibration-guidance" in notice
    assert guide["guidance"]["text"] not in notice and "calibration.csv" not in notice
    outside = replace(cut, deliveries=(replace(delivery, applies_to_work=False),))
    assert guidance_prompt(outside) == ""
    assert "outside_scope" in guidance_prompt(outside, previous_prompts=(first,))
    message["change"]["status"] = "outside_scope"
    removed = live_guidance_prompt(message)
    assert "outside_scope" in removed and guide["guidance"]["text"] not in removed


def _bridge(tmp_path):
    _, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "aa" / ("a" * 64)
    directory.mkdir(parents=True)
    bridge = NativeCodexBridge(["codex", "exec", "-"], directory, "initial")
    bridge.thread_id, bridge.turn_id = "native-root", "turn-1"
    mailbox = directory / "live-guidance"
    mailbox.mkdir()
    command_id = "b" * 64
    message = _guidance_message()
    write_transport_envelope(mailbox / (command_id + ".json"), {"id": command_id, "message": message}, key)
    return bridge, mailbox / command_id, key, message


def test_uncertain_old_guidance_keeps_its_signed_text_when_renderer_changes(tmp_path, monkeypatch):
    bridge, path, key, message = _bridge(tmp_path)
    old_text = "正式人类指导变化（保留此前工作历史）：\n" + json.dumps(message, ensure_ascii=False)
    sent = {"id": path.name, "thread_id": bridge.thread_id, "turn_id": bridge.turn_id, "text": old_text}
    write_transport_envelope(path.with_suffix(".sending"), sent, key)
    calls = []

    def request(method, params):
        calls.append(method)
        assert method == "thread/items/list"
        return {"nextCursor": None, "data": [{"turnId": bridge.turn_id, "item": {
            "type": "userMessage", "clientId": path.name,
            "content": [{"type": "text", "text": old_text}],
        }}]}

    monkeypatch.setattr(bridge, "request", request)
    monkeypatch.setattr("meta_research.native_codex_bridge.live_guidance_prompt",
        lambda _message: pytest.fail("Unknown sends must not be rendered again"))
    bridge.poll_guidance()
    bridge.poll_guidance()
    assert calls == ["thread/items/list"]
    assert read_transport_envelope(path.with_suffix(".sending"), key) == sent
    assert read_transport_envelope(path.with_suffix(".receipt"), key)["text"] == old_text


@pytest.mark.parametrize("state", ("no_turn", "completed", "closed"))
def test_guidance_never_steers_an_idle_or_closed_turn(tmp_path, monkeypatch, state):
    bridge, path, key, _message = _bridge(tmp_path)
    if state == "no_turn":
        bridge.turn_id = None
    elif state == "completed":
        bridge.terminal = {"id": bridge.turn_id, "status": "completed"}
    else:
        write_transport_envelope(bridge.directory / "live-guidance-closed.json", {"reason": "stale"}, key)
    monkeypatch.setattr(bridge, "request", lambda *_args: pytest.fail("No active native turn"))
    bridge.poll_guidance()
    assert not path.with_suffix(".sending").exists()


@pytest.mark.parametrize("observed", (True, False))
def test_unknown_steer_history_blocks_new_delivery_until_original_input_is_proven(tmp_path, monkeypatch, observed):
    bridge, path, key, message = _bridge(tmp_path)
    sent = {"id": path.name, "thread_id": bridge.thread_id, "turn_id": bridge.turn_id,
        "text": live_guidance_prompt(message)}
    write_transport_envelope(path.with_suffix(".sending"), sent, key)
    write_transport_envelope(bridge.directory / "native-provider-argv.json", {"argv": ["codex", "exec", "-"]}, key)

    def reconcile_original_turn(directory):
        items = [{"type": "userMessage", "clientId": path.name,
            "content": [{"type": "text", "text": sent["text"]}]}] if observed else []
        write_transport_envelope(directory / "native-reconciled.json", {
            "thread_id": bridge.thread_id, "turn": {"id": bridge.turn_id,
                "status": "completed", "items": items}}, key)
        return True

    monkeypatch.setattr("meta_research.native_codex_bridge.reconcile_native_operation", reconcile_original_turn)
    if observed:
        assert native_human_input_history(tmp_path, bridge.thread_id) == (sent["text"],)
        assert read_transport_envelope(path.with_suffix(".receipt"), key)["reconciled"] is True
    else:
        with pytest.raises(OwnerConflict, match="human_input_delivery_pending"):
            native_human_input_history(tmp_path, bridge.thread_id)
        assert not path.with_suffix(".receipt").exists()
    assert read_transport_envelope(path.with_suffix(".sending"), key) == sent


def test_accepted_initial_human_input_is_also_delivery_evidence(tmp_path):
    bridge, path, key, message = _bridge(tmp_path)
    original = live_guidance_prompt(message)
    start = {"thread_id": bridge.thread_id, "client_user_message_id": "initial-input",
        "parameters": {"input": [{"type": "text", "text": original}]}}
    write_transport_envelope(bridge.directory / "native-turn-start.json", start, key)
    assert native_human_input_history(tmp_path, bridge.thread_id) == ()
    write_transport_envelope(bridge.directory / "native-turn-id.json", {
        "thread_id": bridge.thread_id, "turn_id": bridge.turn_id,
        "client_user_message_id": "initial-input"}, key)
    assert native_human_input_history(tmp_path, bridge.thread_id) == (original,)
    assert native_human_input_history(tmp_path, "replacement-root") == ()


def test_terminal_metadata_without_original_evidence_does_not_release_unknown_send(tmp_path, monkeypatch):
    from meta_research.native_codex_bridge import NativeTerminalTurnError

    bridge, path, key, message = _bridge(tmp_path)
    sent = {"id": path.name, "thread_id": bridge.thread_id, "turn_id": bridge.turn_id,
        "text": live_guidance_prompt(message)}
    write_transport_envelope(path.with_suffix(".sending"), sent, key)

    def missing_evidence(_directory):
        raise NativeTerminalTurnError(status="failed", native_session_ref=bridge.thread_id,
            stdout="", checkpoint={})

    monkeypatch.setattr("meta_research.native_codex_bridge.reconcile_native_operation", missing_evidence)
    with pytest.raises(OwnerConflict, match="human_input_delivery_pending"):
        native_human_input_history(tmp_path, bridge.thread_id)
    assert not path.with_suffix(".receipt").exists()
    assert read_transport_envelope(path.with_suffix(".sending"), key) == sent


def test_response_projection_reads_raw_reply_without_releasing_a_new_waiter(tmp_path):
    from fastapi.testclient import TestClient
    from meta_research.web import create_app
    from test_root_human_request_lifecycle import _runtime, _bundle_root_channel, _initialized_child, _tool_call, _structured, _open_arguments

    runtime = _runtime(tmp_path / "runtime")
    try:
        run, channel = _bundle_root_channel(runtime)
        scope = GuidanceRuntimeScope("bundle", run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash)
        client = TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key"))
        try:
            headers = _initialized_child(client, channel.connection.token)
            opened = _structured(_tool_call(client, headers, operation_id="human_request.open",
                arguments=_open_arguments("raw-reply", "offline_action"), request_id=1))
            human = runtime.owners.human_collaboration
            response = human.respond_to_human_request(opened["request_ref"], decision="deferred",
                facts={"observed": "negative result"}, note="Exact human response, with uncertainty intact.",
                idempotency_key="raw-response")
            prompt = human_response_prompt(human, scope)
            assert response["note"] in prompt and "negative result" in prompt
            assert response["response_ref"] in prompt and opened["request_ref"] in prompt
            assert human_response_prompt(human, scope, (prompt,)) == ""
            successor = _structured(_tool_call(client, headers, operation_id="human_request.open",
                arguments=_open_arguments("new-waiter", "offline_action", predecessor_request_ref=opened["request_ref"]),
                request_id=2))
            human_response_prompt(human, scope)
            current = runtime.owners.agent_runtime.query_human_request(successor["request_ref"])
            assert current["direct_waiters"][0]["status"] == "blocked"
            assert current["direct_waiters"][0]["resume_validation"] is None
            with pytest.raises(OwnerConflict, match="scope_stale"):
                human_response_prompt(human, replace(scope, fence_ref="foreign-fence"))
        finally:
            client.close()
    finally:
        runtime.close()
