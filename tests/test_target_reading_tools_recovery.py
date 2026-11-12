import json
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from meta_research.external_mcp import ExternalMcpRuntime
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable, HarnessSupervisorTransport
from meta_research.owners.common import canonical_json
from meta_research.target_raw_output import TargetRawOutputStore
from test_harness_frozen_terminal_recovery import _StoppedWithLostReply
from test_harness_target_root import _Gateway, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root


@pytest.mark.parametrize("old_operation", [True, False])
def test_reading_tools_only_enter_new_snapshot_and_unknown_operation_keeps_original(tmp_path, monkeypatch, old_operation):
    import meta_research.target_execution_contract as contract
    database, owner, request, _ = _owner_with_active_root(tmp_path / "owner.sqlite3")
    runner = _StoppedWithLostReply()
    raw_store = TargetRawOutputStore(tmp_path / "supervisor")
    owner.bind_target_provider_ceiling_receipt_verifier(raw_store)
    external = ExternalMcpRuntime(tmp_path / "settings")
    external.configure_endpoint("http://127.0.0.1:8999")
    gateway = _Gateway()
    manifest = tmp_path / "frozen manifest.json"
    original = '{"evidence":"原行", "missing":null}\n'.encode()
    manifest.write_bytes(original)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs/manifest.json").write_text(canonical_json({
        "schema_ref": "meta-research/target-root-input-pointer/v1",
        "read_only_manifest_path": str(manifest), "manifest_sha256": hashlib.sha256(original).hexdigest()}))
    prompt = "Exact Owner context:\n" + canonical_json({"frozen_input_manifest_path": str(manifest)})

    def runtime():
        adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=HarnessSupervisorTransport(tmp_path / "supervisor",
            process_runner=runner, event_sink=owner.append_target_root_events, raw_output_store=raw_store))
        harness = HarnessRuntime(owner, gateway, (adapter,))
        harness.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        harness.bind_external_mcp(external)
        external.bind_scope_authority(harness)
        return harness

    invoke = CodexHarnessAdapter.invoke
    augment = contract.target_execution_reading_prompt
    captured = []

    def fail_before_transport(adapter, invocation):
        captured.append(invocation)
        raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    if old_operation:
        monkeypatch.setattr(contract, "target_execution_reading_prompt", lambda prompt, directory: prompt)
    monkeypatch.setattr(CodexHarnessAdapter, "invoke", fail_before_transport)
    try:
        with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
            runtime().run_or_resume_target_root(request.request_ref, prompt=prompt, mcp_base_url="http://127.0.0.1:8999")
        run = owner.query_run(request.request_ref)
        sealed = owner.latest_operation(run.run_ref)
        assert sealed.status == "unknown_outcome" and runner.calls == 0
        assert runner.request_path is None
        frozen = captured[0].prompt
        assert sealed.frozen_prompt == frozen
        assert ("Execution reading tools:\n" in frozen) is not old_operation
        if not old_operation:
            tools = json.loads(frozen.split("Execution reading tools:\n", 1)[1].splitlines()[0])
            assert Path(tools["project_python"]).is_absolute()
            read = subprocess.run(tools["read_frozen_manifest_argv"], env={**os.environ, "PATH": ""}, capture_output=True, check=True)
            assert read.stdout == original
        monkeypatch.setattr(contract, "target_execution_reading_prompt", augment)
        monkeypatch.setattr(CodexHarnessAdapter, "invoke", invoke)
        with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
            runtime().run_or_resume_target_root(request.request_ref, prompt=prompt, mcp_base_url="http://127.0.0.1:8999")
        recovered = owner.latest_operation(run.run_ref)
        assert recovered.operation_ref == sealed.operation_ref
        assert recovered.invocation_hash == sealed.invocation_hash
        assert recovered.generation == sealed.generation == 1
        assert recovered.frozen_prompt == frozen
        assert recovered.status == "unknown_outcome"
        # The injected failure happened before transport. There is no sealed
        # terminal receipt to reconcile, so restoring the adapter must neither
        # launch the old operation nor invent a stopped process outcome.
        assert runner.calls == 0
        assert runner.request_path is None
        with pytest.raises(HarnessAdmissionError, match="provider_outcome_unknown"):
            runtime().run_or_resume_target_root(request.request_ref, prompt=prompt, mcp_base_url="http://127.0.0.1:8999")
        retained = owner.latest_operation(run.run_ref)
        assert runner.calls == 0
        assert runner.request_path is None
        assert retained.operation_ref == sealed.operation_ref
        assert retained.invocation_hash == sealed.invocation_hash
        assert retained.frozen_prompt == frozen
        assert retained.generation == 1 and retained.status == "unknown_outcome"
    finally:
        database.close()
