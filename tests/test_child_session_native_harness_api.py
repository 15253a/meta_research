"""Historical signed Claude rows remain readable through authenticated HTTP.

The fixture seeds persisted Owner records and signed spool bytes. It never
admits or executes a new Claude Harness operation in the current runtime.
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2, ensure_transport_key, write_exit_receipt,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.web import create_app
from test_public_bundle_stage import _bundle_runtime
from test_target_launch_admission import _ready_launch


def seed_historical_claude_root(runtime):
    graph, target, _bundle_run, dispatch, _request = _ready_launch(runtime)
    rg = runtime.owners.research_graph
    rg.accept_formal_plan_content(formal_plan_ref=graph.formal_plan_ref,
                                 idempotency_key="child-accept-plan")
    formal = rg.accept_target_formal_plan_projection(graph_ref=graph.graph_ref,
                                                   idempotency_key="child-project-plan")
    candidate = rg.accept_target_candidate_projection(target_ref=target.target_ref,
                                                      idempotency_key="child-project-candidate")
    launch = rg.query_target_launch_request(target.target_ref)
    runtime.owners.agent_runtime.admit_target_launch(launch,
        dispatch_decision_ref=dispatch.decision_ref, idempotency_key="child-admit-launch")
    with runtime._database.read() as connection:
        run_ref = connection.execute(text("SELECT target_run_ref FROM ar_target_launches "
            "WHERE target_ref=:target"), {"target": target.target_ref}).scalar_one()
    scope = canonical_target_scope_binding(target_ref=target.target_ref, target_run_ref=run_ref,
        target_spec_hash=launch.target_spec_binding.content_hash_ref, candidate=candidate.candidate,
        formal_plan=formal.formal_plan, accepted_input_refs=())
    # Seed the storage contract directly. The public current Harness rejects
    # Claude admission; historical read support must not reopen that route.
    binding = {"contract_ref": "historical/claude-operation-binding/v1"}
    request = {"request_ref": "historical-claude:" + run_ref,
        "target_ref": target.target_ref, "target_run_ref": run_ref,
        "harness_family": "claude", "model_ref": "claude-opus-5-5",
        "auth_profile_ref": "historical-claude-profile",
        "full_conformance_binding": binding,
        "full_conformance_binding_hash": canonical_hash(binding),
        "target_scope_binding_hash": canonical_hash(scope)}
    owner = runtime.owners.agent_runtime.harness_runs
    run = owner.reserve_admission(request=request, idempotency_key=request["request_ref"],
        request_hash=canonical_hash(request), capability_binding_hash="a" * 64,
        authoritative_run_ref=run_ref)
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE ar_harness_runs SET status='admitted' WHERE run_ref=:ref"),
                           {"ref": run.run_ref})
    return target, owner.query_run(run.request_ref), graph.quest_ref


def seed_signed_native_stream(runtime, run, source_bytes, native_root):
    """Materialize a prior operation's exact signed storage contract."""
    owner = runtime.owners.agent_runtime.harness_runs
    operation = run.run_ref + ":harness_turn:1"
    store = runtime.harnesses._target_raw_output_store
    invocation_hash = canonical_hash({"historical_operation": operation,
                                      "source_hash": canonical_hash(source_bytes.hex())})
    directory = store._source_path(invocation_hash).parent
    directory.mkdir(parents=True)
    source = directory / "stdout.jsonl"
    source.write_bytes(source_bytes)
    prompt = directory / "prompt.txt"
    schema = directory / "output-schema.json"
    prompt.write_text("Historical native lifecycle fixture", encoding="utf-8")
    schema.write_text('{"type":"object"}', encoding="utf-8")
    _, key = ensure_transport_key(store._workspace)
    write_exit_receipt(directory / "supervisor-exit.json", key=key,
        invocation_hash=invocation_hash, prompt_path=prompt, schema_path=schema,
        stdout_path=source, result_path=directory / "last-message.json", returncode=0,
        input_bytes=prompt.stat().st_size, schema_ref=SUPERVISOR_EXIT_SCHEMA_V2)
    envelope = json.loads((directory / "supervisor-exit.json").read_text())
    receipt = {"provider_operation_ref": operation,
        "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
        "spool_ref": "provider-spool:" + invocation_hash,
        "transport_invocation_hash": invocation_hash,
        "supervisor_receipt_hash": canonical_hash(envelope),
        "termination_reason": "completed", "provider_returncode": 0}
    owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1,
                          invocation_hash=invocation_hash, resume=False)
    owner.complete_operation(operation_ref=operation, run_ref=run.run_ref,
        native_session_ref=native_root, profile={"provider_transport_receipts": [receipt]},
        evidence_events=())
    return operation, source


def authenticated_client(runtime):
    client = TestClient(create_app(runtime, base_url="http://testserver",
        control_key="control-secret"), base_url="http://testserver")
    response = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
        json={"token": runtime.authentication.issue_bootstrap_token()})
    assert response.status_code == 200
    return client


def target_root(client, quest, root_ref):
    response = client.get(f"/api/v1/quests/{quest}/root-sessions")
    assert response.status_code == 200
    return next(s for s in response.json()["sessions"] if s["session_ref"] == root_ref)


def test_signed_claude_native_child_is_public_under_its_target_and_survives_restart(tmp_path):
    data = tmp_path / "native-harness-http"
    runtime = _bundle_runtime(data)
    client = None
    try:
        target, run, quest = seed_historical_claude_root(runtime)
        events = [
            {"type": "system", "subtype": "init", "session_id": "native-claude-root"},
            {"type": "assistant", "session_id": "native-claude-root", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "id": "call-alpha", "name": "Agent"}]}},
            {"type": "system", "subtype": "task_started", "session_id": "native-claude-root",
             "task_id": "native-alpha", "tool_use_id": "call-alpha", "task_type": "local_agent"},
            {"type": "system", "subtype": "task_progress", "session_id": "native-claude-root",
             "task_id": "native-alpha", "tool_use_id": "call-alpha"},
            {"type": "assistant", "session_id": "native-claude-root", "parent_tool_use_id": "call-alpha",
             "message": {"content": [{"type": "text", "text": "private child output"}]}},
            {"type": "system", "subtype": "task_notification", "session_id": "native-claude-root",
             "task_id": "native-alpha", "tool_use_id": "call-alpha", "status": "completed",
             "summary": "private child summary"},
            {"type": "result", "session_id": "native-claude-root", "is_error": False},
        ]
        source = "".join(json.dumps(e) + "\n" for e in events).encode()
        operation, path = seed_signed_native_stream(runtime, run, source, "native-claude-root")
        client = authenticated_client(runtime)
        before = target_root(client, quest, run.root_session_ref)
        assert [(c["native_session_ref"], c["status"]) for c in before["children"]] == [
            ("native-alpha", "completed")]
        child = before["children"][0]
        assert child["parent_session_ref"] == run.root_session_ref
        assert child["parent_native_session_ref"] == "native-claude-root"
        assert child["provider"] == "claude" and child["operation_ref"] == operation
        assert "private" not in json.dumps(before["children"])
        assert path.read_bytes() == source
        client.close()
        client = None
        runtime.close()
        runtime = _bundle_runtime(data, harness_ready=False)
        client = authenticated_client(runtime)
        after = target_root(client, quest, run.root_session_ref)
        assert after["children"] == before["children"]
        output = client.get(f"/api/v1/quests/{quest}/root-sessions/{run.root_session_ref}/output",
                            params={"operation_ref": operation})
        assert output.status_code == 503
        # Restart uses the durable signed artifacts, never a cached child list.
        path.write_bytes(source + b"\n")
        damaged = target_root(client, quest, run.root_session_ref)
        assert damaged["children"] == [] and damaged["limited"] is True
    finally:
        if client is not None:
            client.close()
        runtime.close()
