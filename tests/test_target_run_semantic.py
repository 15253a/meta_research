from __future__ import annotations

import json

from meta_research.bundle_protocol import (
    ContentBindingProof,
    ReceiptProof,
    TargetFrontierEntry,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.target_run_semantic import (
    TARGET_RUN_DAEMON_BOUNDARIES,
    TARGET_RUN_SEMANTIC_OPERATION_IDS,
    target_run_semantic_operations,
)


def _proof(subject_ref: str) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref=f"receipt:{subject_ref}",
        subject_ref=subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


def _handle() -> TargetWorkHandle:
    return TargetWorkHandle(
        target_ref="target:1",
        target_run_ref="target-run:1",
        root_session_ref="target-session:1",
        execution_attempt_ref="target-attempt:1",
        execution_fence_ref="target-fence:1",
        execution_input_binding_ref="target-input:1",
        execution_input_binding_receipt=_proof("target-input:1"),
        accepted_input_target_commit_refs=(),
        accepted_input_asset_proofs=(),
        recoverable=True,
    )


class _AgentRuntime:
    def __init__(self, entry: TargetFrontierEntry) -> None:
        self.entry = entry
        self.query_count = 0

    def query_target_frontier_entry(self, target_ref: str):
        self.query_count += 1
        return self.entry if target_ref == self.entry.target_ref else None


class _TargetAgent:
    def __init__(self, handle: TargetWorkHandle) -> None:
        self.handle = handle
        self.context_checks = 0
        self.handle_checks = 0

    def verify_target_semantic_context(self, **values):
        self.context_checks += 1
        if values != {
            "target_ref": self.handle.target_ref,
            "run_ref": self.handle.target_run_ref,
            "attempt_ref": self.handle.execution_attempt_ref,
            "root_session_ref": self.handle.root_session_ref,
            "fence_ref": self.handle.execution_fence_ref,
            "capability_binding_hash": "c" * 64,
        }:
            raise OwnerConflict("target_semantic_context_invalid")
        return {
            "target_ref": self.handle.target_ref,
            "implementation_revision_ref": "implementation-revision:1",
        }

    def verify_current_target_run_handle(self, handle: TargetWorkHandle):
        self.handle_checks += 1
        if handle != self.handle:
            raise OwnerConflict("target_run_harness_identity_invalid")
        return handle


def _gateway():
    handle = _handle()
    entry = TargetFrontierEntry(
        target_ref=handle.target_ref,
        target_spec_binding=ContentBindingProof(
            subject_ref=handle.target_ref,
            content_hash_ref="a" * 64,
        ),
        target_spec_acceptance_receipt=_proof(handle.target_ref),
        state_revision=1,
        state="running",
        current_handle=handle,
        terminal_fact_ref=None,
        currentness_known=True,
        current=True,
    )
    runtime = _AgentRuntime(entry)
    agent = _TargetAgent(handle)
    gateway = SemanticMcpGateway(
        target_run_semantic_operations(
            agent_runtime=runtime,
            target_agent=agent,
        )
    )
    return gateway, runtime, agent, entry


def _issue(gateway: SemanticMcpGateway, *, capability_hash: str = "c" * 64):
    connection, _binding = gateway.issue_channel(
        run_ref="target-run:1",
        attempt_ref="target-attempt:1",
        root_session_ref="target-session:1",
        fence_ref="target-fence:1",
        capability_binding_hash=capability_hash,
        operation_ids=TARGET_RUN_SEMANTIC_OPERATION_IDS,
    )
    return connection


def _call(gateway: SemanticMcpGateway, token: str, arguments: dict[str, object]):
    status, response = gateway.dispatch(
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agent_runtime.target_run.observe",
                "arguments": arguments,
            },
        },
    )
    assert status == 200
    assert response is not None
    return response["result"]


def test_catalog_is_exact_observation_only_and_has_no_execution_port() -> None:
    gateway, _runtime, _agent, _entry = _gateway()

    assert gateway.operation_ids == TARGET_RUN_SEMANTIC_OPERATION_IDS
    assert set(TARGET_RUN_DAEMON_BOUNDARIES).isdisjoint(gateway.operation_ids)
    assert all(
        "target_execution" not in operation_id
        and ".start" not in operation_id
        and ".stop" not in operation_id
        and ".submit" not in operation_id
        for operation_id in gateway.operation_ids
    )
    (binding,) = gateway.required_bindings(TARGET_RUN_SEMANTIC_OPERATION_IDS)
    assert binding["access_mode"] == "read"
    assert binding["reconciliation_operation_id"] is None


def test_observe_returns_exact_current_root_context() -> None:
    gateway, runtime, agent, entry = _gateway()
    connection = _issue(gateway)

    result = _call(gateway, connection.token, {"target_ref": "target:1"})

    assert result["isError"] is False
    content = result["structuredContent"]
    assert content["status"] == "current"
    assert json.loads(content["handle_json"]) == projection_plain_value(
        entry.current_handle
    )
    assert json.loads(content["frontier_json"]) == projection_plain_value(entry)
    assert canonical_json(json.loads(content["candidate_json"])) == (
        content["candidate_json"]
    )
    assert runtime.query_count == 1
    assert agent.context_checks == 1
    assert agent.handle_checks == 1


def test_stale_capability_rejects_before_frontier_read() -> None:
    gateway, runtime, agent, _entry = _gateway()
    connection = _issue(gateway, capability_hash="d" * 64)

    result = _call(gateway, connection.token, {"target_ref": "target:1"})

    assert result["isError"] is True
    assert "semantic_call_scope_stale" in result["content"][0]["text"]
    assert runtime.query_count == 0
    assert agent.context_checks == 1
    assert agent.handle_checks == 0


def test_unknown_or_extra_arguments_fail_closed() -> None:
    gateway, runtime, _agent, _entry = _gateway()
    connection = _issue(gateway)

    result = _call(
        gateway,
        connection.token,
        {"target_ref": "target:1", "operation_handle": "forged"},
    )

    assert result["isError"] is True
    assert "semantic_input_schema_mismatch" in result["content"][0]["text"]
    assert runtime.query_count == 0
