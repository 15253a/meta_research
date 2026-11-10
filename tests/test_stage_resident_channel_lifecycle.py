from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.root_resident_mcp import RootResidentMcpChannels, RootResidentMcpError
from meta_research.sealed_provider_operations import ProviderOperationUnavailable
from test_bundle_skill_adapter import _FullConformanceAuthority as BundleAuthority
from test_reasoning_skill_adapter import _FullConformanceAuthority as ReasoningAuthority
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


@pytest.mark.parametrize("adapter_type,authority_type", [
    (CodexBundleSkillAdapter, BundleAuthority), (CodexReasoningSkillAdapter, ReasoningAuthority),
])
@pytest.mark.parametrize("cleanup", ["complete", "finish", "cancel", "stop"])
def test_actual_adapter_pending_reuses_grant_and_terminal_boundary_revokes_immediately(
    tmp_path, monkeypatch, adapter_type, authority_type, cleanup,
):
    authority = authority_type()
    adapter = adapter_type(tmp_path, executable=sys.executable, process_runner=object())
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    binding = adapter.runtime_binding()
    scope = dict(run_ref="run-one", attempt_ref="attempt-one", root_session_ref="root-one",
                 fence_ref="fence-one", runtime_binding=binding, job_ref="operation-one")
    captured = []
    pending = True

    def invoke(**values):
        captured.append(values)
        if pending:
            raise ProviderOperationUnavailable("codex_operation_reconciliation_pending")
        return {"ok": True}, "native-one", ""

    monkeypatch.setattr(adapter, "_invoke", invoke)
    def call():
        arguments = dict(operation_name="primary", prompt="Exact operation.",
                         schema={"type": "object"}, native_session_ref=None,
                         operation_input={"source": "fixed"})
        if adapter_type is CodexBundleSkillAdapter:
            return adapter._invoke_with_resident_mcp(**scope, **arguments)
        return adapter._invoke_with_resident_mcp(request=SimpleNamespace(**scope), **arguments)

    for _ in range(2):
        with pytest.raises(ProviderOperationUnavailable, match="codex_operation_reconciliation_pending"):
            call()
    assert len(authority.issued) == 1
    assert authority.revoked == []
    assert captured[0]["mcp_token"] == captured[1]["mcp_token"]
    assert captured[0]["mcp_scope_binding_hash"] == captured[1]["mcp_scope_binding_hash"]
    if cleanup == "complete":
        pending = False
        assert call()[0] == {"ok": True}
    elif cleanup == "finish":
        adapter.finish_job("operation-one")
    elif cleanup == "cancel":
        assert adapter.reconcile_cancelled_job("operation-one")
    else:
        adapter.request_stop()
    assert len(authority.revoked) == 1
    pending = False
    assert call()[0] == {"ok": True}
    assert len(authority.issued) == 2 and len(authority.revoked) == 2
    assert captured[-1]["mcp_token"] != captured[0]["mcp_token"]


def test_real_owner_grant_revocation_and_late_release_preserve_new_bearer(tmp_path):
    runtime = _bundle_runtime(tmp_path, harness_ready=False)
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        channels = RootResidentMcpChannels("bundle")
        channels.bind_authority(runtime.harnesses)
        channels.configure_endpoint("http://127.0.0.1:8765")
        def acquire():
            return channels.acquire(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
                capability_binding_hash=run.runtime_binding_hash, phase="primary", job_ref="same-job")
        def status(access):
            return runtime.harnesses.dispatch_mcp(access.token,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})[0]
        first, access = acquire()
        late, same = acquire()
        assert first == late and same.token == access.token and status(access) == 200
        channels.revoke(first)
        assert status(access) == 401
        fresh, new_access = acquire()
        assert fresh != first and status(new_access) == 200
        channels.release(late)
        channels.revoke(late)
        assert status(new_access) == 200
        channels.release(fresh)
        assert status(new_access) == 401
    finally:
        runtime.close()
