from pathlib import Path
import subprocess
import sys

import pytest

from meta_research.harness import HarnessAdmissionError
from meta_research.human_reply import ProvidedReply, ServerReference, Upload
from test_issue296_stable_workspace import _SignedCeilingRunner, _runtime, _activate
from test_root_human_request_lifecycle import _open_arguments
from test_target_root_finalizer import _admit_independent_target_root


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux recovery integration")


class _UnprovenHumanRequestRunner(_SignedCeilingRunner):
    """Only the native boundary is controlled; the timeout has no exit receipt."""

    def __call__(self, argv, prompt, timeout, environment):
        if "META_RESEARCH_PROVIDER_OPERATION_REF" not in environment:
            return super().__call__(argv, prompt, timeout, environment)
        assert not self.calls, "A human response redelivered an unproven provider operation"
        self.calls.append((list(argv), dict(environment)))
        status, payload, _ = self.runtime.harnesses.dispatch_mcp_http(
            environment["META_RESEARCH_MCP_TOKEN"],
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "human_request.open", "arguments": _open_arguments(
                    "issue296-unknown-human-request", "offline_action")}}, mcp_session_id=None)
        assert status == 200 and not payload["result"]["isError"], payload
        self.opened = payload["result"]["structuredContent"]
        self.source = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"]) / "human-original.txt"
        self.source.write_text("Keep the original result pending reconciliation.\n")
        raise subprocess.TimeoutExpired(argv, timeout)


def test_human_response_delivery_and_reconcile_cannot_restart_unknown_operation(tmp_path):
    runner = _UnprovenHumanRequestRunner()
    runtime = _runtime(tmp_path / "issue296-unknown-human-consumer", runner)
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-unknown-human-activate")
        target, _, _, admission, handle = admitted
        with pytest.raises(HarnessAdmissionError, match="provider_timeout"):
            runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
                prompt="Request help before continuing the original work.",
                mcp_base_url="http://127.0.0.1:8999")
        owner = runtime.owners.agent_runtime.harness_runs
        original = owner.latest_operation(handle.target_run_ref)
        assert original.status == "unknown_outcome"
        assert len(runner.calls) == 1
        response = runtime.owners.human_collaboration.respond_to_human_request(
            runner.opened["request_ref"], reply=ProvidedReply(facts={},
                note="Human facts cannot prove that the provider process ended.",
                materials=(Upload("reply.txt", "text/plain", b"Reconcile the original operation.\n"),
                    ServerReference(runtime.root_workspaces.server_files.inspect(str(runner.source))))),
            idempotency_key="issue296-unknown-human-response")
        delivery = response["delivery"]
        assert delivery["human_request_ref"] == runner.opened["request_ref"]
        assert delivery["root_session_ref"] == handle.root_session_ref
        assert delivery["work_ref"] == handle.target_run_ref
        assert delivery["work_materials"]
        destination = runtime.root_workspaces.destination_for_human_request(
            request_ref=runner.opened["request_ref"], waiter_ref=runner.opened["waiter"]["waiter_ref"])
        assert (destination.location.directory / delivery["uploaded_files"][0]["path"]).read_bytes() == b"Reconcile the original operation.\n"
        status, payload, _ = runtime.harnesses.dispatch_mcp_http(runner.calls[0][1]["META_RESEARCH_MCP_TOKEN"],
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "human_request.open.reconcile",
                "arguments": {"effect_id": "issue296-unknown-human-request"}}}, mcp_session_id=None)
        assert status == 401, payload
        status, payload, _ = runtime.harnesses.dispatch_mcp_http(runner.calls[0][1]["META_RESEARCH_MCP_TOKEN"],
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "research_workspace.materials.discover",
                "arguments": {"reference_ref": delivery["work_materials"][0]["reference_ref"]}}}, mcp_session_id=None)
        assert status == 401, payload
        for _ in range(2):
            runtime.target_run_runtime.process_once(target.target_ref)
            with pytest.raises(HarnessAdmissionError, match="target_root_lifecycle_unavailable"):
                runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
                    prompt="A response is available; reconcile first.", mcp_base_url="http://127.0.0.1:8999")
        current = owner.latest_operation(handle.target_run_ref)
        assert current.operation_ref == original.operation_ref
        assert current.status == "unknown_outcome"
        assert len(runner.calls) == 1
        unresolved = runtime.owners.agent_runtime.query_human_request(runner.opened["request_ref"])
        assert unresolved["status"] == "open"
        assert unresolved["direct_waiters"][0]["status"] == "blocked"
    finally:
        runtime.close()
