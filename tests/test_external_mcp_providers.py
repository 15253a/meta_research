from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import httpx
import pytest

from meta_research.acquisition_root import _AcquisitionCodexAdapter
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.companion import CodexCompanionAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.external_mcp import ExternalMcpRuntime, parse_services
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.writing_skill import CodexWritingSkillAdapter
from test_external_mcp import service
from test_runtime_conditions_provider_prompts import _SignedRunner, _idea_call
from test_deepfetch_adapter import DurableSegmentSequenceRunner, _request, _deepfetch_web_evidence_gate_output_schema


@contextmanager
def gateway(runtime):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, payload, _ = runtime.dispatch_http(self.headers["Authorization"].removeprefix("Bearer "), message)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime.configure_endpoint(f"http://127.0.0.1:{server.server_port}")
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def discover_and_call(argv, environment):
    item = next(value for value in argv if value.startswith("mcp_servers.meta_research_external.url="))
    url = json.loads(item.split("=", 1)[1])
    headers = {"Authorization": "Bearer " + environment["META_RESEARCH_EXTERNAL_MCP_TOKEN"]}
    catalog = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers).json()["result"]["tools"]
    tool = next(tool for tool in catalog if tool["description"] == "Read the fixture temperature in Celsius.")
    result = httpx.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool["name"], "arguments": {"sensor": "provider"}}}, headers=headers).json()["result"]
    return tool, result


class ProtocolRunner(_SignedRunner):
    def __init__(self):
        super().__init__()
        self.observations = []

    def __call__(self, argv, prompt, timeout, environment=None):
        tool, result = discover_and_call(argv, environment)
        self.observations.append((prompt, tool, result))
        return super().__call__(argv, prompt, timeout, environment)


@pytest.mark.parametrize("adapter_type", (CodexIdeaSkillAdapter, CodexPlanSkillAdapter, CodexBundleSkillAdapter,
    CodexReasoningSkillAdapter, CodexWritingSkillAdapter, CodexCompanionAdapter, _AcquisitionCodexAdapter))
def test_all_shared_consumers_get_real_catalog_instructions_and_frozen_resume(tmp_path, adapter_type):
    runtime = ExternalMcpRuntime(tmp_path / "settings")
    first = runtime.save_config(services=parse_services([service(tmp_path, research_instructions="Instruction A")]),
        expected_revision=runtime.read_config().revision)
    runner = ProtocolRunner()
    workspace = tmp_path / "provider"
    adapter = adapter_type(workspace, process_runner=runner)
    adapter.bind_external_mcp(runtime)
    read_root = workspace / "writing-inputs" / "snapshot"
    if adapter_type is CodexWritingSkillAdapter:
        read_root.mkdir(parents=True)

    def invoke(adapter, *, job="job:initial", native=None):
        if adapter_type is not CodexWritingSkillAdapter:
            return _idea_call(adapter, job=job, native=native)
        return adapter._invoke_root_operation(
            operation_name="primary", prompt="Research the actual question.",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"], "additionalProperties": False},
            native_session_ref=native, job_ref=job, run_ref="stage-run:current",
            attempt_ref=None, root_session_ref="session:current", fence_ref=None,
            sandbox_read_root=read_root,
        )

    with gateway(runtime):
        result = invoke(adapter)
        original = runner.observations[0]
        assert "Instruction A" in original[0]
        assert original[1]["inputSchema"]["required"] == ["sensor"]
        assert original[2]["structuredContent"] == {"sensor": "provider", "temperature": 23.75}
        runtime.save_config(services=parse_services([service(tmp_path, research_instructions="Instruction B")]), expected_revision=first.revision)
        reconstructed = adapter_type(workspace, process_runner=runner)
        reconstructed.bind_external_mcp(runtime)
        assert invoke(reconstructed) == result
        assert len(runner.observations) == 1
        invoke(reconstructed, job="job:next", native="runtime-native")
        assert "Instruction B" in runner.observations[1][0]
        assert len(runner.observations) == 2
        directory = workspace / "provider-operations" / canonical_hash({"job_ref": "job:initial"}) / "primary"
        binding = json.loads((directory / "invocation.json").read_text())["payload"]["external_mcp"]
        (tmp_path / "settings/external-mcp/operations" / (binding["operation_key"] + ".json")).unlink()
        with pytest.raises(IdeaSkillUnavailable, match="external_mcp_snapshot_missing"):
            invoke(reconstructed)


def test_deepfetch_freezes_external_tools_above_resume_segments(tmp_path):
    runtime = ExternalMcpRuntime(tmp_path / "settings")
    first = runtime.save_config(services=parse_services([service(tmp_path, research_instructions="DeepFetch A")]),
        expected_revision=runtime.read_config().revision)

    class EditingRunner(DurableSegmentSequenceRunner):
        def run_durable_job(self, *args, **kwargs):
            tool, result = discover_and_call(args[1], args[7])
            assert tool["description"] == "Read the fixture temperature in Celsius."
            assert result["structuredContent"]["temperature"] == 23.75
            completed = super().run_durable_job(*args[:7], **kwargs)
            if len(self.calls) == 1:
                runtime.save_config(services=parse_services([service(tmp_path, research_instructions="DeepFetch B")]), expected_revision=first.revision)
            return completed

    workspace = tmp_path / "provider"
    runner = EditingRunner(workspace, stopped_segments=1)
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner)
    adapter.bind_external_mcp(runtime)
    request = replace(_request(), job_ref="deepfetch:external", runtime_binding=adapter.runtime_binding())
    with gateway(runtime):
        result = adapter._invoke_with_access(request, "web_evidence_gate=v1\nRead the evidence.",
            output_schema=_deepfetch_web_evidence_gate_output_schema(), timeout_seconds=None, access=None)
        paths = sorted(workspace.glob("provider-operations/*/deepfetch-*/invocation.json"))
        bindings = [json.loads(path.read_text())["payload"]["external_mcp"] for path in paths]
        assert len(bindings) == 2 and bindings[0] == bindings[1]
        assert all("DeepFetch A" in path.with_name("prompt.txt").read_text() for path in paths)
        reconstructed = CodexDeepFetchAdapter(workspace, process_runner=runner)
        reconstructed.bind_external_mcp(runtime)
        assert reconstructed._invoke_with_access(request, "web_evidence_gate=v1\nRead the evidence.",
            output_schema=_deepfetch_web_evidence_gate_output_schema(), timeout_seconds=None, access=None) == result
        assert len(runner.calls) == 2
