"""Native child metadata through the signed Claude Harness and public HTTP seam.

The provider subprocess is deterministic. Its small literals reflect actual
Claude task envelopes; larger captured real Opus samples use the same fixture
from the local ticket acceptance probe.
"""
import json
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.harness_adapters import HarnessSupervisorTransport
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.web import create_app
from test_public_bundle_stage import _bundle_runtime
from test_target_launch_admission import _ready_launch


def admit_claude_root(runtime):
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
    admission = runtime.harnesses.admit_target_run(target_ref=target.target_ref,
        target_run_ref=run_ref, harness_family="claude", model_ref="claude-opus-5-5",
        auth_profile_ref="harness-profile:claude-default", target_scope_binding=scope)
    return target, admission.run, graph.quest_ref


def replay_native_stream(runtime, run, source_bytes, native_root):
    """Actual subprocess -> existing transport signature -> Owner evidence ledger."""
    owner = runtime.owners.agent_runtime.harness_runs
    operation = run.run_ref + ":harness_turn:1"
    store = runtime.harnesses._target_raw_output_store
    workspace = store._workspace
    provider = runtime.data_root.root / "replay-native-provider.py"
    source = runtime.data_root.root / "captured-native.jsonl"
    source.write_bytes(source_bytes)
    provider.write_text("import pathlib,sys\nsys.stdin.read()\n"
                        "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n",
                        encoding="utf-8")
    scope = {"schema_ref": "meta-research/target-root-observation-scope/v1",
        "target_run_ref": run.run_ref, "attempt_ref": run.attempt_ref,
        "attempt_generation": run.attempt_generation, "root_session_ref": run.root_session_ref,
        "fence_ref": run.fence_ref, "native_session_ref": None}
    environment = {"META_RESEARCH_HARNESS_FAMILY": "claude",
        "META_RESEARCH_HARNESS_WORKSPACE": str(workspace),
        "META_RESEARCH_PROVIDER_OPERATION_REF": operation,
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": canonical_hash(scope),
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": canonical_json(scope)}
    argv = [sys.executable, str(provider), str(source)]
    prompt, timeout = "Replay isolated native lifecycle bytes", 15.0
    invocation_hash = canonical_hash({"schema_ref": "meta-research/harness-provider-operation/v1",
        "family": "claude", "provider_operation_ref": operation, "argv": argv,
        "prompt_hash": canonical_hash(prompt), "timeout_seconds": timeout,
        "environment_names": sorted(environment)})
    owner.start_operation(run_ref=run.run_ref, operation_ref=operation, generation=1,
                          invocation_hash=invocation_hash, resume=False)
    transport = HarnessSupervisorTransport(workspace,
        event_sink=owner.append_target_root_events, raw_output_store=store)
    completed = transport(argv, prompt, timeout, environment)
    assert completed.returncode == 0
    assert completed.stdout.encode("utf-8") == source_bytes
    receipt = {"provider_operation_ref": operation, **completed.meta_research_transport_receipt}
    owner.complete_operation(operation_ref=operation, run_ref=run.run_ref,
        native_session_ref=native_root, profile={"provider_transport_receipts": [receipt]},
        evidence_events=())
    return operation, store._source_path(invocation_hash)


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
        target, run, quest = admit_claude_root(runtime)
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
        operation, path = replay_native_stream(runtime, run, source, "native-claude-root")
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
    finally:
        if client is not None:
            client.close()
        runtime.close()
