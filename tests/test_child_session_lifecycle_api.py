"""Child lifecycle observations through authenticated production HTTP.

Provider stdout and its signed receipt are the external transport boundary.
No lifecycle reader or process verifier is mocked.
"""
import hashlib
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ensure_transport_key, read_transport_envelope, write_transport_envelope,
)
from meta_research.web import create_app
from meta_research.stage_root_observations import _bundle_rolling_unit_ref
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
from test_stage_root_observations import _seed_spool


@pytest.fixture
def child_api(tmp_path):
    runtime = _bundle_runtime(tmp_path / "child-lifecycle-http")
    _prepare_bundle_request(runtime)
    assert runtime.bundle_stage.process_once()
    current = runtime.bundle_stage.query_current()
    request = runtime.owners.advancement_engine.query_bundle_stage_request(
        current["stage_run_request"]["cycle_ref"])
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
    invocation = run.primary_invocation
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=invocation.invocation_ref, operation_ref=invocation.operation_ref,
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        unit_kind="bundle_primary")
    scope = {"run_ref": run.run_ref, "run_kind": "bundle_stage",
             "operation_ref": invocation.operation_ref}
    directory = _seed_spool(runtime.data_root.root, scope, (), operation_name="primary")
    client = TestClient(create_app(runtime, base_url="http://testserver",
                                  control_key="control-secret"), base_url="http://testserver")
    response = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
        json={"token": runtime.authentication.issue_bootstrap_token()})
    assert response.status_code == 200
    try:
        yield runtime, client, request.accepted_question.quest_ref, run, directory
    finally:
        client.close()
        runtime.close()


def record(directory, events, *, sealed=True):
    source = directory / "stdout.jsonl"
    source.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    if sealed:
        _, key = ensure_transport_key(directory.parents[2])
        invocation = read_transport_envelope(directory / "invocation.json", key)
        write_transport_envelope(directory / "supervisor-exit.json", {
            "schema_ref": "meta-research/codex-provider-supervisor-exit/v1",
            "invocation_hash": canonical_hash(invocation), "returncode": 0,
            "termination_reason": "completed",
            "stdout_file_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        }, key)


def collab(tool, children, states, *, actor="native-bundle-root", event_type="item.completed"):
    return {"type": event_type, "item": {"id": tool + "-call", "type": "collab_tool_call",
        "tool": tool, "status": "completed", "sender_thread_id": actor,
        "receiver_thread_ids": children, "agents_states": states}}


def session(client, quest, root):
    response = client.get(f"/api/v1/quests/{quest}/root-sessions")
    assert response.status_code == 200
    return next(item for item in response.json()["sessions"] if item["session_ref"] == root)


def test_parallel_children_keep_exact_parent_and_native_terminal_status(child_api):
    _runtime, client, quest, run, directory = child_api
    record(directory, [
        {"type": "thread.started", "thread_id": "native-bundle-root"},
        collab("spawn_agent", ["child-good", "child-bad"], {
            "child-good": {"status": "pending"}, "child-bad": {"status": "running"}}),
        collab("spawn_agent", ["foreign-child"], {"foreign-child": {"status": "running"}},
               actor="foreign-root"),
        collab("wait", ["child-good", "child-bad"], {
            "child-good": {"status": "completed", "message": "private child result"},
            "child-bad": {"status": "errored", "message": "private failure"}}),
        {"type": "turn.completed", "thread_id": "native-bundle-root"},
    ])
    root = session(client, quest, run.root_session_ref)
    children = root["children"]
    assert [(child["native_session_ref"], child["status"]) for child in children] == [
        ("child-good", "completed"), ("child-bad", "failed")]
    assert all(child["parent_session_ref"] == root["session_ref"]
               and child["parent_native_session_ref"] == "native-bundle-root"
               and child["operation_ref"] == run.primary_invocation.invocation_ref
               and child["provider"] == "codex" for child in children)
    assert len({child["session_ref"] for child in children}) == 2
    assert "private" not in json.dumps(children)


def test_child_terminal_in_resumed_root_call_keeps_its_session_identity(child_api):
    runtime, client, quest, run, directory = child_api
    record(directory, [
        {"type": "thread.started", "thread_id": "native-bundle-root"},
        collab("spawn_agent", ["child-across-turns"], {"child-across-turns": {"status": "running"}}),
    ])
    before = session(client, quest, run.root_session_ref)["children"][0]
    assert before["status"] == "unknown"
    runtime.owners.agent_runtime.acknowledge_provider_safe_point(
        unit_ref=run.primary_invocation.invocation_ref, run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, fence_ref=run.fence_ref)
    unit = _bundle_rolling_unit_ref(operation_ref=run.primary_invocation.operation_ref,
        operation_name="dispatch-2", attempt_ref=run.attempt_ref)
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=unit, operation_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        unit_kind="bundle_review")
    resumed = _seed_spool(runtime.data_root.root, {
        "run_kind": "bundle_stage", "operation_ref": run.primary_invocation.operation_ref},
        (), native_session_ref="native-bundle-root", operation_name="dispatch-2")
    record(resumed, [
        {"type": "thread.started", "thread_id": "native-bundle-root"},
        collab("wait", ["child-across-turns"], {"child-across-turns": {"status": "completed"}}),
        {"type": "turn.completed", "thread_id": "native-bundle-root"},
    ])
    after = session(client, quest, run.root_session_ref)["children"]
    assert [(child["session_ref"], child["status"], child["operation_ref"]) for child in after] == [
        (before["session_ref"], "completed", unit)]


def test_starting_running_and_cancelled_children_require_native_work_and_a_bound_process(child_api):
    _runtime, client, quest, run, directory = child_api
    assert session(client, quest, run.root_session_ref)["children"] == []
    request_path = str((directory / "supervisor-request.json").resolve())
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"],
        start_new_session=True, env={**os.environ, "META_RESEARCH_PROVIDER_OPERATION": request_path})
    try:
        _, key = ensure_transport_key(directory.parents[2])
        invocation = read_transport_envelope(directory / "invocation.json", key)
        write_transport_envelope(directory / "provider-started.json", {
            "schema_ref": "meta-research/codex-provider-started/v2",
            "invocation_hash": canonical_hash(invocation),
            "provider_operation_path": request_path,
            "provider_process_id": process.pid, "provider_process_group": process.pid,
        }, key)
        events = [
            {"type": "thread.started", "thread_id": "native-bundle-root"},
            collab("spawn_agent", ["child-starting", "child-active"], {
                "child-starting": {"status": "pending"}, "child-active": {"status": "running"}}),
        ]
        record(directory, events, sealed=False)
        observed = session(client, quest, run.root_session_ref)
        assert observed["is_executing"] is True
        assert [child["status"] for child in observed["children"]] == ["starting", "running"]
        events.append(collab("close_agent", ["child-active"], {"child-active": {"status": "shutdown"}}))
        record(directory, events, sealed=False)
        assert [child["status"] for child in session(client, quest, run.root_session_ref)["children"]] == [
            "starting", "cancelled"]
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert [child["status"] for child in session(client, quest, run.root_session_ref)["children"]] == [
        "unknown", "cancelled"]


@pytest.mark.parametrize("damage", ["stdout", "invocation"])
def test_changed_signed_source_cannot_publish_child_status(child_api, damage):
    _runtime, client, quest, run, directory = child_api
    record(directory, [
        {"type": "thread.started", "thread_id": "native-bundle-root"},
        collab("spawn_agent", ["child"], {"child": {"status": "completed"}}),
    ])
    assert session(client, quest, run.root_session_ref)["children"][0]["status"] == "completed"
    if damage == "stdout":
        with (directory / "stdout.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(collab("spawn_agent", ["forged"], {"forged": {"status": "completed"}})) + "\n")
    else:
        (directory / "invocation.json").write_text('{"not":"signed"}', encoding="utf-8")
    observed = session(client, quest, run.root_session_ref)
    assert observed["children"] == []
    assert observed["limited"] is True


@pytest.mark.parametrize("native_status, expected", [
    ("completed", "completed"), ("failed", "failed"), ("killed", "cancelled"),
])
def test_claude_verified_task_update_is_terminal_without_a_later_notification(tmp_path, native_status, expected):
    from test_child_session_native_harness_api import (
        admit_claude_root, authenticated_client, replay_native_stream, target_root,
    )
    runtime = _bundle_runtime(tmp_path / "native-update-http")
    client = None
    try:
        _target, run, quest = admit_claude_root(runtime)
        events = [
            {"type": "system", "subtype": "init", "session_id": "native-claude-root"},
            {"type": "assistant", "session_id": "native-claude-root", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "id": "call-child", "name": "Agent"}]}},
            {"type": "system", "subtype": "task_started", "session_id": "native-claude-root",
             "task_id": "native-child", "tool_use_id": "call-child", "task_type": "local_agent"},
            {"type": "system", "subtype": "task_updated", "session_id": "native-claude-root",
             "task_id": "unbound-task", "patch": {"status": "completed"}},
            {"type": "system", "subtype": "task_updated", "session_id": "native-claude-root",
             "task_id": "native-child", "patch": {"status": native_status,
                                                     "error": "private task details"}},
        ]
        source = "".join(json.dumps(event) + "\n" for event in events).encode()
        operation, _path = replay_native_stream(runtime, run, source, "native-claude-root")
        client = authenticated_client(runtime)
        children = target_root(client, quest, run.root_session_ref)["children"]
        assert [(child["native_session_ref"], child["status"]) for child in children] == [
            ("native-child", expected)]
        assert children[0]["operation_ref"] == operation
        assert children[0]["parent_session_ref"] == run.root_session_ref
        assert "private" not in json.dumps(children)
    finally:
        if client is not None:
            client.close()
        runtime.close()
