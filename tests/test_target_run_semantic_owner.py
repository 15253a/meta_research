"""Target progress uses the admitted Owner scope, including its current fence."""
import pytest

from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.target_run_semantic import target_run_semantic_operations
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.parametrize("stale_fence", [False, True])
def test_real_owner_target_progress_checks_exact_admission_and_fence(tmp_path, stale_fence):
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        run = runtime.owners.agent_runtime.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        assert run is not None
        gateway = SemanticMcpGateway(target_run_semantic_operations(
            agent_runtime=runtime.owners.agent_runtime,
            target_agent=runtime.target_run_authorities.agent_runtime))
        channel, _ = gateway.issue_channel(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,
            fence_ref="stale-fence" if stale_fence else run.fence_ref,
            capability_binding_hash=run.capability_binding_hash,
            operation_ids=("agent_runtime.target_run.progress",),
            root_kind="target", phase="target_root_lifecycle")
        status, response = gateway.dispatch(channel.token, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "agent_runtime.target_run.progress",
                       "arguments": {"target_ref": handle.target_ref}}})
        assert status == 200
        result = response["result"]
        if stale_fence:
            assert result["isError"] is True
            assert result["structuredContent"]["code"] == "semantic_call_scope_stale"
        else:
            assert result["isError"] is False, result
            value = result["structuredContent"]
            assert value["target_ref"] == handle.target_ref
            assert value["target_run_ref"] == run.run_ref
            assert value["availability"] == "ready"
            assert value["fragments"][0]["text"] == "epoch 1 complete\n"
            assert value["cursor"]
    finally:
        runtime.close()
