import json

import pytest

from meta_research.external_mcp import ExternalMcpRuntime, parse_services
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, ClaudeHarnessAdapter, HarnessAdapterUnavailable, HarnessSupervisorTransport
from meta_research.target_raw_output import TargetRawOutputStore
from test_external_mcp import service
from test_harness_frozen_terminal_recovery import _StoppedWithLostReply
from test_harness_target_root import _Gateway, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root


def test_target_snapshot_precedes_owner_intent_and_survives_crash_before_spool(tmp_path, monkeypatch):
    database, owner, request, _handle = _owner_with_active_root(tmp_path / "owner.sqlite3")
    runner = _StoppedWithLostReply()
    raw_store = TargetRawOutputStore(tmp_path / "supervisor")
    owner.bind_target_provider_ceiling_receipt_verifier(raw_store)
    external = ExternalMcpRuntime(tmp_path / "settings")
    current = external.save_config(services=parse_services([service(tmp_path, research_instructions="Target original instruction")]),
        expected_revision=external.read_config().revision)
    external.configure_endpoint("http://127.0.0.1:8999")
    gateway = _Gateway()

    def runtime():
        adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=HarnessSupervisorTransport(tmp_path / "supervisor",
            process_runner=runner, event_sink=owner.append_target_root_events, raw_output_store=raw_store))
        harness = HarnessRuntime(owner, gateway, (adapter,))
        harness.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        harness.bind_external_mcp(external)
        external.bind_scope_authority(harness)
        return harness

    invoke = CodexHarnessAdapter.invoke
    captured = []

    def fail_before_transport(adapter, invocation):
        captured.append(invocation)
        raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    monkeypatch.setattr(CodexHarnessAdapter, "invoke", fail_before_transport)
    try:
        with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
            runtime().run_or_resume_target_root(request.request_ref, prompt="Original Target work.", mcp_base_url="http://127.0.0.1:8999")
        run = owner.query_run(request.request_ref)
        original = owner.latest_operation(run.run_ref)
        assert original.status == "unknown_outcome" and runner.calls == 0
        first_invocation = captured[0]
        assert "Target original instruction" in first_invocation.prompt
        assert first_invocation.external_mcp_access.binding()["snapshot_hash"]
        codex_argv = CodexHarnessAdapter(tmp_path / "codex")._argv(first_invocation)
        assert any(value.startswith("mcp_servers.meta_research_external.url=") for value in codex_argv)
        claude = ClaudeHarnessAdapter(tmp_path / "claude")
        claude_argv = claude._argv(first_invocation)
        config = json.loads(open(claude_argv[claude_argv.index("--mcp-config") + 1]).read())
        assert config["mcpServers"]["meta_research_external"]["headers"] == {"Authorization": "Bearer ${META_RESEARCH_EXTERNAL_MCP_TOKEN}"}
        external.save_config(services=parse_services([service(tmp_path, allowed_root_kinds=[], research_instructions="Edited instruction")]), expected_revision=current.revision)
        monkeypatch.setattr(CodexHarnessAdapter, "invoke", invoke)
        with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
            runtime().run_or_resume_target_root(request.request_ref, prompt="Original Target work.", mcp_base_url="http://127.0.0.1:8999")
        recovered = owner.latest_operation(run.run_ref)
        assert recovered.operation_ref == original.operation_ref and recovered.invocation_hash == original.invocation_hash
        assert "Target original instruction" in runner.request_path.with_name("prompt.txt").read_text()
        assert "Edited instruction" not in runner.request_path.with_name("prompt.txt").read_text()
        assert runner.calls == 1
    finally:
        database.close()
