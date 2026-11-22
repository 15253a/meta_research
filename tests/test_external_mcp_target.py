import json
import subprocess

import pytest

from meta_research.external_mcp import (
    ExternalMcpRuntime,
    exposed_tool_name,
    parse_services,
)
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import (
    CODEX_LOCKED_VERSION,
    CodexHarnessAdapter,
    HarnessAdapterUnavailable,
    HarnessSupervisorTransport,
)
from meta_research.target_raw_output import TargetRawOutputStore
from test_external_mcp import configured, service
from test_harness_frozen_terminal_recovery import _StoppedWithLostReply
from test_harness_target_root import _Gateway, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root


class _ExternalCallRunner:
    """Codex-shaped runner that exercises the Target external MCP channel."""

    def __init__(self, external: ExternalMcpRuntime) -> None:
        self.external = external
        self.calls: list[str] = []
        self._next_id = 0

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, f"codex-cli {CODEX_LOCKED_VERSION}\n", ""
            )
        if argv[-2:] == ["features", "list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "hooks stable true\nmulti_agent stable true\nplugins stable true\n"
                "remote_plugin stable true\nshell_tool stable true\nskill_search stable true\n"
                "unified_exec stable true\n",
                "",
            )
        token = environment.get("META_RESEARCH_EXTERNAL_MCP_TOKEN")
        assert token

        def call(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            self._next_id += 1
            status, response, _session = self.external.dispatch_http(
                token,
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id,
                    "method": method,
                    "params": {} if params is None else params,
                },
            )
            assert status == 200 and response is not None and "error" not in response, response
            return response["result"]

        call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        call("tools/list")
        catalog = call(
            "tools/call",
            {"name": "external_tools_catalog", "arguments": {"service_id": "lab"}},
        )
        item = catalog["structuredContent"]["items"][0]
        exposed = item["name"]
        contract = call(
            "tools/call",
            {"name": "external_tools_contract", "arguments": {"name": exposed}},
        )
        assert contract["structuredContent"]["name"] == exposed
        result = call(
            "tools/call",
            {
                "name": "external_tools_call",
                "arguments": {"name": exposed, "arguments": {"sensor": "target"}},
            },
        )
        assert result["structuredContent"] == {
            "sensor": "target",
            "temperature": 23.75,
        }
        self.calls.append(exposed)
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(
                json.dumps(item)
                for item in (
                    {"type": "thread.started", "thread_id": "target-external-root"},
                    {"type": "turn.completed", "thread_id": "target-external-root"},
                )
            )
            + "\n",
            "",
        )


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
    invoke_terminal = CodexHarnessAdapter.invoke_terminal
    captured = []
    reconciliations = []

    def fail_before_transport(adapter, invocation):
        captured.append(invocation)
        raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    def read_original_terminal(adapter, invocation):
        reconciliations.append(invocation)
        return invoke_terminal(adapter, invocation)

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
        external.save_config(services=parse_services([service(tmp_path, allowed_root_kinds=[], research_instructions="Edited instruction")]), expected_revision=current.revision)
        monkeypatch.setattr(CodexHarnessAdapter, "invoke", invoke)
        monkeypatch.setattr(CodexHarnessAdapter, "invoke_terminal", read_original_terminal)
        with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
            runtime().run_or_resume_target_root(request.request_ref, prompt="Original Target work.", mcp_base_url="http://127.0.0.1:8999")
        recovered = owner.latest_operation(run.run_ref)
        assert recovered.operation_ref == original.operation_ref and recovered.invocation_hash == original.invocation_hash
        assert recovered.status == "unknown_outcome" and recovered.generation == original.generation
        assert len(reconciliations) == 1
        retained = reconciliations[0]
        assert retained.entry_path == "recovery"
        assert retained.provider_operation_ref == original.operation_ref
        assert retained.owner_invocation_hash == original.invocation_hash
        assert retained.prompt == first_invocation.prompt
        assert "Target original instruction" in retained.prompt and "Edited instruction" not in retained.prompt
        assert retained.external_mcp_access.binding() == first_invocation.external_mcp_access.binding()
        # An unknown Owner intent without its original signed terminal receipt
        # cannot authorize another transport merely to reconstruct the spool.
        assert runner.calls == 0 and runner.request_path is None
        assert not list((tmp_path / "supervisor").rglob("supervisor-request.json"))
    finally:
        database.close()


def test_target_provider_can_call_external_mcp_through_admitted_channel(tmp_path):
    database, owner, request, _handle = _owner_with_active_root(
        tmp_path / "external-call-owner.sqlite3"
    )
    external = configured(tmp_path)
    runner = _ExternalCallRunner(external)
    adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=runner)
    harness = HarnessRuntime(owner, _Gateway(), (adapter,))
    target_workspace = tmp_path / "target"
    target_workspace.mkdir()
    harness.bind_target_workspace_resolver(_WorkspaceResolver(target_workspace))
    harness.bind_external_mcp(external)
    external.bind_scope_authority(harness)
    try:
        result = harness.run_or_resume_target_root(
            request.request_ref,
            prompt="Use the configured external sensor before deciding.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert result.status == "executed"
        assert runner.calls == [exposed_tool_name("lab", "read_temperature")]
        ledger = tmp_path / "calls.jsonl"
        assert [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()] == [
            {"name": "read_temperature", "sensor": "target"}
        ]
    finally:
        external.close()
        database.close()
