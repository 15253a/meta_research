from __future__ import annotations

from meta_research.semantic_mcp import SemanticMcpError, SemanticMcpGateway, SemanticOperation


def _gateway(handler):
    operations = (
        SemanticOperation(
            "agent_runtime.target_work.request",
            "agent_runtime",
            "test effect",
            handler,
            {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"], "additionalProperties": False},
            {"type": "object"},
            access_mode="effect",
            reconciliation_operation_id="agent_runtime.target_work.reconcile",
        ),
        SemanticOperation(
            "agent_runtime.target_work.reconcile",
            "agent_runtime",
            "test reconcile",
            lambda _context, _arguments: {"status": "unknown_outcome"},
            {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"], "additionalProperties": False},
            {"type": "object"},
            access_mode="reconcile",
        ),
    )
    gateway = SemanticMcpGateway(operations)
    connection, _binding = gateway.issue_channel(
        run_ref="run-1",
        attempt_ref="attempt-1",
        root_session_ref="session-1",
        fence_ref="fence-1",
        capability_binding_hash="a" * 64,
        operation_ids=tuple(item.semantic_operation_id for item in operations),
        root_kind="target",
        phase="execution",
    )
    return gateway, connection.token


def _call(gateway, token, arguments):
    _status, response = gateway.dispatch(
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agent_runtime.target_work.request",
                "arguments": arguments,
            },
        },
    )
    return response["result"]["structuredContent"]


def test_schema_error_is_agent_actionable_and_not_started() -> None:
    gateway, token = _gateway(lambda _context, _arguments: {})
    payload = _call(gateway, token, {})

    assert payload["code"] == "semantic_input_schema_mismatch"
    assert payload["details"]["legacy_code"] == "semantic_input_schema_mismatch"
    assert payload["details"]["effect_status"] == "not_started"
    assert payload["details"]["feedback_stage_agent"] is True
    assert payload["details"]["field_path"] == ["effect_id"]
    assert payload["details"]["retry_allowed"] is False


def test_handler_failure_is_unknown_and_requires_same_effect_reconcile() -> None:
    gateway, token = _gateway(lambda _context, _arguments: (_ for _ in ()).throw(RuntimeError("secret")))
    payload = _call(gateway, token, {"effect_id": "effect-1"})

    assert payload["code"] == "semantic_operation_failed"
    assert payload["details"]["effect_id"] == "effect-1"
    assert payload["details"]["effect_status"] == "unknown"
    assert payload["details"]["retry_allowed"] is False
    assert payload["details"]["reconcile_same_effect"] is True
    assert payload["details"]["next_action"] == "reconcile"
    assert "secret" not in str(payload)


def test_missing_channel_has_operator_route() -> None:
    gateway, _token = _gateway(lambda _context, _arguments: {})
    _status, response = gateway.dispatch(
        "expired",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}},
    )

    assert response["error"] == {
        "code": "mcp_channel_authentication_required",
        "message": "A current scope-bound MCP channel is required.",
    }


def test_business_rejection_preserves_owner_code_and_rejects_effect() -> None:
    def reject(_context, _arguments):
        raise SemanticMcpError(
            "target_work_request_invalid",
            {"message": "Target proof was rejected", "call_execution": "not_started"},
        )

    gateway, token = _gateway(reject)
    payload = _call(gateway, token, {"effect_id": "effect-2"})

    assert payload["code"] == "target_work_request_invalid"
    assert payload["details"]["legacy_code"] == "target_work_request_invalid"
    assert payload["details"]["effect_status"] == "not_started"
    assert payload["details"]["feedback_stage_agent"] is True


def test_stale_fence_is_not_retryable() -> None:
    def reject(_context, _arguments):
        raise SemanticMcpError("attempt_fence_stale", {"fence_ref": "old-fence"})

    gateway, token = _gateway(reject)
    payload = _call(gateway, token, {"effect_id": "effect-3"})

    assert payload["details"]["effect_status"] == "stale"
    assert payload["details"]["retry_allowed"] is False
    assert payload["details"]["next_action"] == "reread_current_state"


def test_audit_write_failure_routes_the_projection_to_operator() -> None:
    class BrokenAuditStore:
        def record(self, _contract):
            raise OSError("audit unavailable")

    gateway, token = _gateway(lambda _context, _arguments: (_ for _ in ()).throw(RuntimeError("secret")))
    gateway._error_audit_store = BrokenAuditStore()
    payload = _call(gateway, token, {"effect_id": "effect-audit"})

    assert payload["details"]["audit_status"] == "unavailable"
    assert payload["details"]["operator_required"] is True
    assert payload["details"]["recovery_owner"] == "operator"

