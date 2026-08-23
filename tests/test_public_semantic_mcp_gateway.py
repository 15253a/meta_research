from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.harness import HarnessAdmissionError, HarnessProbeRequest
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import HostComputeDevice, HostComputeSnapshot
from meta_research.semantic_mcp import (
    SemanticMcpError,
    SemanticMcpGateway,
    SemanticOperation,
)
from meta_research.web import create_app


def test_effectful_semantic_operation_fails_closed_without_reconciliation() -> None:
    with pytest.raises(
        SemanticMcpError, match="semantic_effect_reconciliation_required"
    ):
        SemanticMcpGateway(
            (
                SemanticOperation(
                    semantic_operation_id="agent_runtime.example.effect",
                    owning_module="agent_runtime",
                    description="An intentionally incomplete effect contract.",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    access_mode="effect",
                    handler=lambda _context, _arguments: {"status": "executed"},
                ),
            )
        )


def test_effectful_channel_fails_closed_without_its_reconciliation_operation() -> None:
    gateway = SemanticMcpGateway(
        (
            SemanticOperation(
                semantic_operation_id="agent_runtime.example.effect",
                owning_module="agent_runtime",
                description="A bounded effect with a required reconciliation seam.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                access_mode="effect",
                reconciliation_operation_id=(
                    "agent_runtime.example.effect.reconcile"
                ),
                handler=lambda _context, _arguments: {"status": "executed"},
            ),
            SemanticOperation(
                semantic_operation_id="agent_runtime.example.effect.reconcile",
                owning_module="agent_runtime",
                description="Reconcile the bounded effect.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                access_mode="reconcile",
                handler=lambda _context, _arguments: {"status": "executed"},
            ),
        )
    )

    with pytest.raises(
        SemanticMcpError,
        match="required_semantic_reconciliation_unavailable",
    ):
        gateway.required_bindings(("agent_runtime.example.effect",))

    bindings = gateway.required_bindings(
        (
            "agent_runtime.example.effect",
            "agent_runtime.example.effect.reconcile",
        )
    )
    assert [item["access_mode"] for item in bindings] == [
        "effect",
        "reconcile",
    ]


def test_semantic_operation_fails_closed_without_a_valid_output_schema() -> None:
    with pytest.raises(SemanticMcpError, match="semantic_operation_schema_invalid"):
        SemanticMcpGateway(
            (
                SemanticOperation(
                    semantic_operation_id="research_graph.invalid.read",
                    owning_module="research_graph",
                    description="An intentionally incomplete query contract.",
                    input_schema={"type": "object"},
                    output_schema={},
                    handler=lambda _context, _arguments: {},
                ),
            )
        )


def test_semantic_gateway_validates_input_and_owner_output_before_returning() -> None:
    calls = 0

    def invalid_owner_output(_context, _arguments):
        nonlocal calls
        calls += 1
        return {"unexpected": True}

    gateway = SemanticMcpGateway(
        (
            SemanticOperation(
                semantic_operation_id="research_graph.validated.read",
                owning_module="research_graph",
                description="A schema-validated semantic Owner query.",
                input_schema={
                    "type": "object",
                    "required": ["quest_ref"],
                    "properties": {
                        "quest_ref": {"type": "string", "minLength": 1}
                    },
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string"}},
                },
                handler=invalid_owner_output,
            ),
        )
    )
    connection, _binding = gateway.issue_channel(
        run_ref="run:schema",
        attempt_ref="attempt:schema",
        root_session_ref="session:schema",
        fence_ref="fence:schema",
        capability_binding_hash="a" * 64,
        operation_ids=("research_graph.validated.read",),
    )

    status, invalid_input = gateway.dispatch(
        connection.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "research_graph.validated.read",
                "arguments": {"quest_ref": "", "raw_sql": "SELECT *"},
            },
        },
    )
    assert status == 200
    assert invalid_input["result"]["structuredContent"]["code"] == (
        "semantic_input_schema_mismatch"
    )
    assert calls == 0

    status, invalid_output = gateway.dispatch(
        connection.token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "research_graph.validated.read",
                "arguments": {"quest_ref": "quest:1"},
            },
        },
    )
    assert status == 200
    assert invalid_output["result"]["structuredContent"]["code"] == (
        "semantic_output_schema_mismatch"
    )
    assert calls == 1


class _CountingHostProbe:
    def __init__(self) -> None:
        self.calls = 0

    def observe(self) -> HostComputeSnapshot:
        self.calls += 1
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_800_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-semantic-mcp",
                    name="Semantic MCP Test GPU",
                    memory_total_mib=24_576,
                ),
            ),
            adapter_kind="semantic_mcp_test_probe",
        )


def test_typed_admission_rejects_unknown_capability_and_nonopaque_auth_ref(
    tmp_path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "invalid-admission"))
    try:
        with pytest.raises(HarnessAdmissionError, match="harness_probe_request_invalid"):
            runtime.harnesses.admit_probe(
                HarnessProbeRequest(
                    request_ref="probe-invalid-capability",
                    harness_family="codex",
                    model_ref="gpt-test",
                    auth_profile_ref="harness-profile:codex-default",
                    required_operation_ids=("research_graph.snapshot.read",),
                    required_capabilities=("model_says_it_can",),
                ),
                idempotency_key="probe-invalid-capability",
            )
        with pytest.raises(HarnessAdmissionError, match="harness_probe_request_invalid"):
            runtime.harnesses.admit_probe(
                HarnessProbeRequest(
                    request_ref="probe-secret-auth",
                    harness_family="claude",
                    model_ref="sonnet",
                    auth_profile_ref="sk-ant-not-an-opaque-profile-ref",
                    required_operation_ids=("research_graph.snapshot.read",),
                    required_capabilities=("native_session",),
                ),
                idempotency_key="probe-secret-auth",
            )
    finally:
        runtime.close()


def test_typed_harness_admission_scopes_a_real_mcp_owner_query(tmp_path) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="probe-request-codex",
                harness_family="codex",
                model_ref="gpt-test",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("semantic_mcp",),
            ),
            idempotency_key="probe-admission-codex",
        )
        owner_run = runtime.owners.agent_runtime.harness_runs.query_run(
            admission.run.request_ref
        )
        assert owner_run is not None
        assert (
            owner_run.run_ref,
            owner_run.attempt_ref,
            owner_run.root_session_ref,
            owner_run.fence_ref,
        ) == (
            admission.run.run_ref,
            admission.run.attempt_ref,
            admission.run.root_session_ref,
            admission.run.fence_ref,
        )
        assert not hasattr(runtime.harnesses, "_database")
        app = create_app(
            runtime,
            base_url="http://testserver",
            control_key="control-key",
        )

        with TestClient(app) as client:
            initialized = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {admission.connection.token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "public-test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200

            called = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {admission.connection.token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "research_graph.snapshot.read",
                        "arguments": {},
                    },
                },
            )

        assert called.status_code == 200
        result = called.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["owner"] == "research_graph"
        assert result["structuredContent"]["status"] == "ready"
        assert result["structuredContent"]["revision"] == 0
        assert result["structuredContent"]["facts"]["quest_count"] == 0
        assert result["structuredContent"]["facts"]["question_count"] == 0
        assert admission.run.native_session_ref is None
        assert "token" not in str(admission.run.as_public_dict())
        gateway_status = runtime.harnesses.query_status()["gateway"]
        assert gateway_status["health_receipt"]["receipt_ref"] == (
            admission.run.mcp_binding.health_receipt_ref
        )
        assert gateway_status["health_receipt"]["subject_ref"] == (
            admission.run.mcp_binding.server_instance_ref
        )
    finally:
        runtime.close()


def test_daemon_restart_restores_the_typed_run_with_a_new_scoped_channel(
    tmp_path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-data")
    first_runtime = build_production_runtime(data_root)
    first = first_runtime.harnesses.admit_probe(
        HarnessProbeRequest(
            request_ref="probe-request-restart",
            harness_family="claude",
            model_ref="claude-test",
            auth_profile_ref="harness-profile:claude-default",
            required_operation_ids=("agent_runtime.snapshot.read",),
            required_capabilities=("semantic_mcp", "resume"),
        ),
        idempotency_key="probe-admission-restart",
    )
    first_runtime.close()

    restored_runtime = build_production_runtime(data_root)
    try:
        restored = restored_runtime.harnesses.resume_probe(
            "probe-request-restart"
        )
        assert restored.run.run_ref == first.run.run_ref
        assert restored.run.attempt_ref == first.run.attempt_ref
        assert restored.run.root_session_ref == first.run.root_session_ref
        assert restored.run.fence_ref == first.run.fence_ref
        assert restored.run.mcp_binding.server_instance_ref != (
            first.run.mcp_binding.server_instance_ref
        )
        assert restored.connection.grant_ref != first.connection.grant_ref

        app = create_app(
            restored_runtime,
            base_url="http://testserver",
            control_key="control-key",
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "restart-test", "version": "1"},
            },
        }
        with TestClient(app) as client:
            old_channel = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {first.connection.token}",
                    "Accept": "application/json, text/event-stream",
                },
                json=initialize,
            )
            new_channel = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {restored.connection.token}",
                    "Accept": "application/json, text/event-stream",
                },
                json=initialize,
            )
        assert old_channel.status_code == 401
        assert new_channel.status_code == 200
    finally:
        restored_runtime.close()


def test_interrupted_channel_admission_reuses_the_ar_reserved_identities(
    tmp_path,
) -> None:
    data_root = prepare_data_root(tmp_path / "interrupted-admission")
    request = HarnessProbeRequest(
        request_ref="probe-request-interrupted-admission",
        harness_family="codex",
        model_ref="gpt-test",
        auth_profile_ref="harness-profile:codex-default",
        required_operation_ids=("research_graph.snapshot.read",),
        required_capabilities=("semantic_mcp",),
    )
    first_runtime = build_production_runtime(data_root)
    issue_channel = first_runtime.harnesses._gateway.issue_channel

    def crash_after_ar_reservation(**_values):
        raise RuntimeError("simulated crash after AR reservation")

    first_runtime.harnesses._gateway.issue_channel = crash_after_ar_reservation
    with pytest.raises(RuntimeError, match="simulated crash after AR reservation"):
        first_runtime.harnesses.admit_probe(
            request,
            idempotency_key="probe-admission-interrupted",
        )
    reserved = first_runtime.owners.agent_runtime.harness_runs.query_run(
        request.request_ref
    )
    assert reserved is not None
    assert reserved.status == "admitting"
    first_runtime.harnesses._gateway.issue_channel = issue_channel
    first_runtime.close()

    restored_runtime = build_production_runtime(data_root)
    try:
        admitted = restored_runtime.harnesses.admit_probe(
            request,
            idempotency_key="probe-admission-interrupted",
        )
        assert admitted.run.status == "admitted"
        assert (
            admitted.run.run_ref,
            admitted.run.attempt_ref,
            admitted.run.root_session_ref,
            admitted.run.fence_ref,
        ) == (
            reserved.run_ref,
            reserved.attempt_ref,
            reserved.root_session_ref,
            reserved.fence_ref,
        )
    finally:
        restored_runtime.close()


def test_channel_reissue_revokes_the_previous_token_in_the_same_daemon(
    tmp_path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "reissue-data"))
    try:
        first = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="probe-request-reissue",
                harness_family="codex",
                model_ref="gpt-test",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("semantic_mcp",),
            ),
            idempotency_key="probe-admission-reissue",
        )
        reissued = runtime.harnesses.resume_probe("probe-request-reissue")
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "reissue-test", "version": "1"},
            },
        }

        old_status, _old_payload = runtime.harnesses.dispatch_mcp(
            first.connection.token, initialize
        )
        new_status, _new_payload = runtime.harnesses.dispatch_mcp(
            reissued.connection.token, initialize
        )

        assert old_status == 401
        assert new_status == 200
    finally:
        runtime.close()


def test_lost_effect_response_reconciles_before_replay_without_duplication(
    tmp_path,
) -> None:
    probe = _CountingHostProbe()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "effect-data"),
        host_compute_probe=probe,
    )
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="probe-request-effect",
                harness_family="codex",
                model_ref="gpt-test",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=(
                    "agent_runtime.host_compute.observe",
                    "agent_runtime.host_compute.reconcile",
                ),
                required_capabilities=("semantic_mcp",),
            ),
            idempotency_key="probe-admission-effect",
        )
        app = create_app(runtime, base_url="http://testserver", control_key="key")
        headers = {
            "Authorization": f"Bearer {admission.connection.token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }

        def call(client, request_id: int, name: str):
            return client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": {"effect_id": "host-probe-1"},
                    },
                },
            ).json()["result"]["structuredContent"]

        with TestClient(app) as client:
            # Treat the first returned response as lost at the transport boundary.
            call(client, 1, "agent_runtime.host_compute.observe")
            reconciled = call(
                client, 2, "agent_runtime.host_compute.reconcile"
            )
            replayed = call(client, 3, "agent_runtime.host_compute.observe")

        assert reconciled["status"] == "effect_confirmed"
        assert replayed["status"] == "effect_confirmed"
        assert reconciled["result"]["snapshot_ref"] == replayed["result"][
            "snapshot_ref"
        ]
        assert probe.calls == 1
    finally:
        runtime.close()


def test_mcp_http_boundary_rejects_browser_origin_and_protocol_drift(
    tmp_path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "boundary"))
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="probe-request-boundary",
                harness_family="codex",
                model_ref="gpt-test",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("semantic_mcp",),
            ),
            idempotency_key="probe-admission-boundary",
        )
        app = create_app(runtime, base_url="http://testserver", control_key="key")
        authorization = f"Bearer {admission.connection.token}"
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "research_graph.snapshot.read",
                "arguments": {},
            },
        }
        with TestClient(app) as client:
            hostile_origin = client.post(
                "/mcp",
                headers={
                    "Authorization": authorization,
                    "Origin": "https://attacker.invalid",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json=call,
            )
            missing_version = client.post(
                "/mcp",
                headers={
                    "Authorization": authorization,
                    "Accept": "application/json, text/event-stream",
                },
                json=call,
            )
            wrong_version = client.post(
                "/mcp",
                headers={
                    "Authorization": authorization,
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2024-11-05",
                },
                json=call,
            )

        assert hostile_origin.status_code == 403
        assert hostile_origin.json()["detail"]["code"] == "origin_invalid"
        assert missing_version.status_code == 400
        assert missing_version.json()["detail"]["code"] == (
            "mcp_protocol_version_required"
        )
        assert wrong_version.status_code == 400
        assert wrong_version.json()["detail"]["code"] == (
            "mcp_protocol_version_unsupported"
        )
    finally:
        runtime.close()
