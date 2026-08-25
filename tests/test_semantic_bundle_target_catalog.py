from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

from meta_research.bundle_protocol import (
    BundleInboxBatch,
    ContentBindingProof,
    ReceiptProof,
    TargetFrontierEntry,
    TargetLaunchAck,
    TargetLaunchRequest,
    TargetWorkHandle,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    OwnerSnapshot,
    canonical_hash,
)
from meta_research.owners.research_graph import AcceptedReuseEligibility
from meta_research.owners.research_memory import (
    AcceptedImplementationRevisionContent,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_DAEMON_COMPLETION_BOUNDARIES,
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    BUNDLE_TARGET_SEMANTIC_MISSING_MATRIX,
    TARGET_RUN_DAEMON_BOUNDARIES,
    TARGET_RUN_SEMANTIC_OPERATION_IDS,
    create_semantic_owner_gateway,
)


def _receipt(owner: str = "advancement_engine") -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=owner,
        kind="test_acceptance",
        receipt_ref=f"{owner}:receipt",
        subject_ref=f"{owner}:subject",
        payload_hash="b" * 64,
    )


def _snapshot(owner: str) -> OwnerSnapshot:
    return OwnerSnapshot(owner=owner, revision=1, facts={})


def _launch_request(target_ref: str = "target:1") -> TargetLaunchRequest:
    return TargetLaunchRequest(
        target_ref=target_ref,
        target_spec_binding=ContentBindingProof(
            subject_ref=target_ref,
            content_hash_ref="c" * 64,
        ),
        target_spec_acceptance_receipt=ReceiptProof(
            receipt_ref="rg:target-spec-receipt",
            subject_ref="c" * 64,
            verified=True,
            currentness_known=True,
            current=True,
        ),
        accepted_input_target_commit_refs=(),
        accepted_input_asset_refs=(),
        recoverable_required=True,
    )


class _FakeAdvancementEngine:
    def __init__(self) -> None:
        self.request = SimpleNamespace(
            request_ref="stage-request:1",
            cycle_ref="cycle:1",
            stage="bundle",
            epoch=3,
            context_pack_ref="context-pack:1",
            context_pack_hash="d" * 64,
            accepted_formal_plan=SimpleNamespace(
                formal_plan_ref="formal-plan:1",
                plan_document_hash="e" * 64,
            ),
            receipt=_receipt(),
        )

    def query_snapshot(self) -> OwnerSnapshot:
        return _snapshot("advancement_engine")

    def query_bundle_stage_request(self, cycle_ref: str):
        assert cycle_ref == "cycle:1"
        return self.request


class _FakeResearchMemory:
    def __init__(self) -> None:
        self.accepted_content = None
        self.accept_calls = []
        self.source_verifications = []
        self.content_verifications = []

    def query_snapshot(self) -> OwnerSnapshot:
        return _snapshot("research_memory")

    def accept_implementation_content(self, **values):
        self.accept_calls.append(values)
        request = {name: value for name, value in values.items() if name != "idempotency_key"}
        content = {
            name: request[name]
            for name in (
                "source_ref",
                "exact_version_ref",
                "implementation_revision_ref",
                "license_ref",
                "source_content_hash_ref",
                "patch_ref",
            )
        }
        content_hash = canonical_hash(content)
        self.accepted_content = AcceptedImplementationRevisionContent(
            **request,
            content=content,
            content_hash_ref=content_hash,
            accepted_at=1.0,
            source_verification_receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="reuse_source_version_verified",
                receipt_ref="rm:source-receipt",
                subject_ref=request["exact_version_ref"],
                payload_hash="3" * 64,
            ),
            content_acceptance_receipt=AcceptanceReceipt(
                issuer="research_memory",
                kind="implementation_revision_content_accepted",
                receipt_ref="rm:content-receipt",
                subject_ref=content_hash,
                payload_hash="4" * 64,
            ),
        )
        return self.accepted_content

    def query_implementation_content(self, implementation_revision_ref: str):
        if (
            self.accepted_content is None
            or self.accepted_content.implementation_revision_ref
            != implementation_revision_ref
        ):
            return None
        return self.accepted_content

    def verify_reuse_source_version(self, **values) -> None:
        self.source_verifications.append(values)
        accepted = self.accepted_content
        if accepted is None or any(
            values[name] != getattr(accepted, name)
            for name in (
                "source_ref",
                "exact_version_ref",
                "implementation_revision_ref",
                "license_ref",
                "source_content_hash_ref",
                "patch_ref",
            )
        ) or (
            values["receipt_ref"]
            != accepted.source_verification_receipt.receipt_ref
            or values["receipt_subject_ref"]
            != accepted.source_verification_receipt.subject_ref
        ):
            raise OwnerConflict("reuse_source_version_receipt_invalid")

    def verify_implementation_content(self, **values) -> None:
        self.content_verifications.append(values)
        accepted = self.accepted_content
        if accepted is None or any(
            values[name] != getattr(accepted, name)
            for name in (
                "source_ref",
                "exact_version_ref",
                "implementation_revision_ref",
                "license_ref",
                "source_content_hash_ref",
                "patch_ref",
            )
        ) or (
            values["content_hash_ref"] != accepted.content_hash_ref
            or values["receipt_ref"]
            != accepted.content_acceptance_receipt.receipt_ref
            or values["receipt_subject_ref"]
            != accepted.content_acceptance_receipt.subject_ref
        ):
            raise OwnerConflict("implementation_content_receipt_invalid")


class _FakeResearchGraph:
    def __init__(self) -> None:
        self.launch_queries = 0
        self.eligibility = None
        self.eligibility_verifications = []

    def query_snapshot(self) -> OwnerSnapshot:
        return _snapshot("research_graph")

    def query_target_launch_request(self, target_ref: str) -> TargetLaunchRequest:
        self.launch_queries += 1
        return _launch_request(target_ref)

    def query_reuse_eligibility(self, eligibility_ref: str):
        if (
            self.eligibility is None
            or self.eligibility.eligibility_ref != eligibility_ref
        ):
            return None
        return self.eligibility

    def verify_reuse_eligibility(self, **values) -> None:
        self.eligibility_verifications.append(values)
        accepted = self.eligibility
        if accepted is None or (
            values["tier"] != accepted.tier
            or values["source_ref"] != accepted.source_ref
            or values["exact_version_ref"] != accepted.exact_version_ref
            or values["implementation_revision_ref"]
            != accepted.implementation_revision_ref
            or values["implementation_content_hash_ref"]
            != accepted.implementation_content_hash_ref
            or values["eligibility_anchor_ref"] != accepted.target_commit_ref
            or values["eligibility_ref"] != accepted.eligibility_ref
            or values["eligibility_content_hash_ref"] != accepted.payload_hash
            or values["receipt_ref"] != accepted.receipt.receipt_ref
            or values["receipt_subject_ref"] != accepted.receipt.subject_ref
        ):
            raise OwnerConflict("reuse_eligibility_receipt_invalid")


class _FakeAgentRuntime:
    def __init__(self) -> None:
        self.scope_checks = 0
        self.admissions = []
        self.stale = False
        self.ack = None
        self.frontier = None
        self.inbox_reads = []
        self.runtime_binding = SimpleNamespace(
            as_dict=lambda: {
                "schema_ref": "meta-research/bundle-runtime-binding/v1",
                "packaged_skill_bundle_hash": "1" * 64,
                "instruction_set_hash": "2" * 64,
                "model_ref": "model:test",
                "harness_adapter_ref": "harness:test",
                "mcp_bindings": ["mcp:test"],
                "capability_bindings": ["cap:test"],
                "resource_bindings": ["resource:test"],
            }
        )
        self.bundle_run = SimpleNamespace(
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1",
            root_session_ref="bundle-session:1",
            fence_ref="bundle-fence:1",
            runtime_binding_hash="a" * 64,
            runtime_binding=self.runtime_binding,
            status="running",
        )

    def query_snapshot(self) -> OwnerSnapshot:
        return _snapshot("agent_runtime")

    def verify_bundle_runtime_scope(self, **values) -> None:
        self.scope_checks += 1
        if self.stale:
            raise OwnerConflict("bundle_runtime_scope_stale")
        assert values == {
            "run_ref": "bundle-run:1",
            "attempt_ref": "bundle-attempt:1",
            "root_session_ref": "bundle-session:1",
            "fence_ref": "bundle-fence:1",
            "runtime_binding_hash": "a" * 64,
        }

    def query_managed_run(self, run_ref: str):
        assert run_ref == "bundle-run:1"
        return {
            "run_ref": run_ref,
            "run_kind": "bundle_stage",
            "cycle_ref": "cycle:1",
            "attempt_ref": "bundle-attempt:1",
            "root_session_ref": "bundle-session:1",
            "fence_ref": "bundle-fence:1",
        }

    def query_bundle_stage_run(self, request_ref: str):
        assert request_ref == "stage-request:1"
        return self.bundle_run

    def query_bundle_dispatch_decisions(self, run_ref: str):
        assert run_ref == "bundle-run:1"
        return (
            SimpleNamespace(
                decision_ref="dispatch:1",
                run_ref="bundle-run:1",
                attempt_ref="bundle-attempt:1",
                fence_ref="bundle-fence:1",
                action="dispatch",
                selected_target_ref="target:1",
            ),
        )

    def admit_target_launch(self, request, **values):
        self.admissions.append((request, values))
        self.ack = TargetLaunchAck(
            target_ref=request.target_ref,
            operation_ref="target-launch-operation:1",
        )
        return self.ack

    def query_target_launch_ack(self, target_ref: str):
        assert target_ref == "target:1"
        return self.ack

    def query_target_frontier_entry(self, target_ref: str):
        assert target_ref == "target:1"
        return self.frontier

    def read_bundle_inbox(self, **values):
        self.inbox_reads.append(values)
        return BundleInboxBatch(
            after_cursor=4,
            next_cursor=4,
            generation=2,
            notices=(),
        )


class _FakeTargetAgent:
    def __init__(self, runtime: _FakeAgentRuntime) -> None:
        self._runtime = runtime

    def verify_target_semantic_context(self, **values):
        if values != {
            "target_ref": "target:1",
            "run_ref": "bundle-run:1",
            "attempt_ref": "bundle-attempt:1",
            "root_session_ref": "bundle-session:1",
            "fence_ref": "bundle-fence:1",
            "capability_binding_hash": "a" * 64,
        }:
            raise OwnerConflict("target_semantic_context_invalid")
        return ContentBindingProof(
            subject_ref="implementation:fake",
            content_hash_ref="f" * 64,
        )

    def verify_current_target_run_handle(self, handle):
        frontier = self._runtime.frontier
        if frontier is None or frontier.current_handle != handle:
            raise OwnerConflict("target_run_harness_identity_invalid")
        return handle


def _gateway():
    graph = _FakeResearchGraph()
    advancement = _FakeAdvancementEngine()
    memory = _FakeResearchMemory()
    runtime = _FakeAgentRuntime()
    target_agent = _FakeTargetAgent(runtime)
    gateway = create_semantic_owner_gateway(
        research_graph=graph,
        advancement_engine=advancement,
        research_memory=memory,
        agent_runtime=runtime,
        human_collaboration_snapshot=lambda: _snapshot("human_collaboration"),
        target_run_agent=target_agent,
    )
    return gateway, graph, memory, runtime


def _issue(gateway, operation_ids):
    connection, _binding = gateway.issue_channel(
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        root_session_ref="bundle-session:1",
        fence_ref="bundle-fence:1",
        capability_binding_hash="a" * 64,
        operation_ids=operation_ids,
    )
    return connection


def _call(gateway, connection, operation_id: str, arguments: dict[str, object]):
    status, response = gateway.dispatch(
        connection.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": operation_id, "arguments": arguments},
        },
    )
    assert status == 200
    assert response is not None
    return response["result"]


def test_bundle_and_target_catalogs_contain_only_registered_reconciled_operations():
    gateway, _graph, _memory, _runtime = _gateway()
    assert set(BUNDLE_ROOT_SEMANTIC_OPERATION_IDS) <= set(gateway.operation_ids)
    assert set(TARGET_RUN_SEMANTIC_OPERATION_IDS) <= set(gateway.operation_ids)

    for operation_ids in (
        BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        TARGET_RUN_SEMANTIC_OPERATION_IDS,
    ):
        bindings = gateway.required_bindings(operation_ids)
        by_id = {item["semantic_operation_id"]: item for item in bindings}
        for binding in bindings:
            if binding["access_mode"] != "effect":
                continue
            reconcile_id = binding["reconciliation_operation_id"]
            assert reconcile_id in by_id
            assert by_id[reconcile_id]["access_mode"] == "reconcile"


def test_formal_catalog_schemas_are_closed_at_every_declared_object_boundary():
    gateway, _graph, _memory, _runtime = _gateway()
    operation_ids = (
        *BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        *TARGET_RUN_SEMANTIC_OPERATION_IDS,
    )
    connection = _issue(gateway, operation_ids)
    status, response = gateway.dispatch(
        connection.token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert status == 200
    assert response is not None

    def assert_closed(schema: dict[str, object]) -> None:
        if schema["type"] == "object":
            assert schema.get("additionalProperties") is False
            for child in schema.get("properties", {}).values():
                assert_closed(child)
        elif schema["type"] == "array" and "items" in schema:
            assert_closed(schema["items"])

    for tool in response["result"]["tools"]:
        assert_closed(tool["inputSchema"])
        assert_closed(tool["outputSchema"])


def test_bundle_inbox_read_uses_owner_cursor_and_exact_call_scope():
    gateway, _graph, _memory, runtime = _gateway()
    operation_id = "agent_runtime.bundle_inbox.read"
    connection = _issue(gateway, (operation_id,))
    listed_status, listed = gateway.dispatch(
        connection.token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert listed_status == 200
    tool = next(
        item
        for item in listed["result"]["tools"]
        if item["name"] == operation_id
    )
    assert tool["inputSchema"]["required"] == ["limit"]
    assert set(tool["inputSchema"]["properties"]) == {"limit"}

    result = _call(gateway, connection, operation_id, {"limit": 64})
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "status": "current",
        "batch": {
            "after_cursor": 4,
            "next_cursor": 4,
            "generation": 2,
            "notices": [],
        },
    }
    assert runtime.inbox_reads == [
        {
            "run_ref": "bundle-run:1",
            "attempt_ref": "bundle-attempt:1",
            "fence_ref": "bundle-fence:1",
            "limit": 64,
        }
    ]


def test_missing_matrix_is_typed_and_does_not_register_placeholder_tools():
    gateway, _graph, _memory, _runtime = _gateway()
    missing_names = {item.semantic_name for item in BUNDLE_TARGET_SEMANTIC_MISSING_MATRIX}
    assert missing_names == {
        "verify_delivered_context_pack",
        "read_formal_plan",
        "accept_reuse_eligibility",
        "reconcile_reuse_eligibility",
        "submit_implementation_roles",
        "submit_execution_input_binding",
        "accept_result_assets",
        "freeze_target_implementation_workspace",
        "accept_generic_result_assets",
        "accept_generic_formal_metric",
        "accept_generic_execution_closure",
        "complete_generic_target_handoff_and_commit",
        "transact_run",
        "report_execution_blocker",
        "propose_targets",
        "control_target_work",
        "reconcile_target_submission",
    }
    assert all(
        item.reason_code and item.required_public_interface
        for item in BUNDLE_TARGET_SEMANTIC_MISSING_MATRIX
    )
    assert not any(
        operation_id.endswith(tuple(missing_names))
        for operation_id in gateway.operation_ids
    )
    assert set(BUNDLE_DAEMON_COMPLETION_BOUNDARIES).isdisjoint(
        gateway.operation_ids
    )
    assert set(TARGET_RUN_DAEMON_BOUNDARIES).isdisjoint(gateway.operation_ids)
    assert not {
        "bundle_report",
        "record_bundle_report_disposition",
        "retire_bundle_run_for_replan",
        "activate_bundle_replan",
        "propose_bundle_stage_commit",
    } & missing_names


def test_target_work_uses_context_effect_identity_and_revalidates_scope():
    gateway, graph, _memory, runtime = _gateway()
    operation_ids = (
        "agent_runtime.target_work.request",
        "agent_runtime.target_work.request.reconcile",
    )
    connection = _issue(gateway, operation_ids)
    result = _call(
        gateway,
        connection,
        "agent_runtime.target_work.request",
        {
            "effect_id": "launch:1",
            "target_ref": "target:1",
            "dispatch_decision_ref": "dispatch:1",
        },
    )
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "status": "effect_confirmed",
        "effect_id": "launch:1",
        "target_ref": "target:1",
        "operation_ref": "target-launch-operation:1",
    }
    request, values = runtime.admissions[0]
    assert request == _launch_request()
    assert values["dispatch_decision_ref"] == "dispatch:1"
    assert values["idempotency_key"].startswith("mcp-effect:")
    assert runtime.scope_checks == 1
    assert graph.launch_queries == 1

    reconciled = _call(
        gateway,
        connection,
        "agent_runtime.target_work.request.reconcile",
        {
            "effect_id": "launch:1",
            "target_ref": "target:1",
            "dispatch_decision_ref": "dispatch:1",
        },
    )
    assert reconciled["structuredContent"]["status"] == "effect_confirmed"
    assert runtime.scope_checks == 2


def test_bundle_handler_rejects_stale_context_before_rg_or_ar_side_effects():
    gateway, graph, _memory, runtime = _gateway()
    operation_ids = (
        "agent_runtime.target_work.request",
        "agent_runtime.target_work.request.reconcile",
    )
    connection = _issue(gateway, operation_ids)
    runtime.stale = True
    result = _call(
        gateway,
        connection,
        "agent_runtime.target_work.request",
        {
            "effect_id": "launch:stale",
            "target_ref": "target:1",
            "dispatch_decision_ref": "dispatch:1",
        },
    )
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "semantic_call_scope_stale"
    assert graph.launch_queries == 0
    assert runtime.admissions == []


def test_target_handler_rereads_frontier_and_rejects_a_stale_context_identity():
    gateway, _graph, _memory, runtime = _gateway()
    handle = TargetWorkHandle(
        target_ref="target:1",
        target_run_ref="bundle-run:1",
        root_session_ref="bundle-session:1",
        execution_attempt_ref="bundle-attempt:1",
        execution_fence_ref="bundle-fence:1",
        execution_input_binding_ref="execution-input:1",
        execution_input_binding_receipt=ReceiptProof(
            receipt_ref="rg:execution-input-receipt",
            subject_ref="execution-input:1",
            verified=True,
            currentness_known=True,
            current=True,
        ),
        accepted_input_target_commit_refs=(),
        accepted_input_asset_proofs=(),
        recoverable=True,
    )
    runtime.frontier = TargetFrontierEntry(
        target_ref="target:1",
        target_spec_binding=_launch_request().target_spec_binding,
        target_spec_acceptance_receipt=(
            _launch_request().target_spec_acceptance_receipt
        ),
        state_revision=1,
        state="running",
        current_handle=handle,
        terminal_fact_ref=None,
        currentness_known=True,
        current=True,
    )
    operation_ids = ("agent_runtime.target_run.observe",)
    connection = _issue(gateway, operation_ids)
    result = _call(
        gateway,
        connection,
        "agent_runtime.target_run.observe",
        {"target_ref": "target:1"},
    )
    assert result["isError"] is False
    assert json.loads(result["structuredContent"]["handle_json"]) == {
        "target_ref": "target:1",
        "target_run_ref": "bundle-run:1",
        "root_session_ref": "bundle-session:1",
        "execution_attempt_ref": "bundle-attempt:1",
        "execution_fence_ref": "bundle-fence:1",
        "execution_input_binding_ref": "execution-input:1",
        "execution_input_binding_receipt": {
            "receipt_ref": "rg:execution-input-receipt",
            "subject_ref": "execution-input:1",
            "verified": True,
            "currentness_known": True,
            "current": True,
        },
        "accepted_input_target_commit_refs": [],
        "accepted_input_asset_proofs": [],
        "recoverable": True,
    }

    runtime.frontier = replace(
        runtime.frontier,
        current_handle=replace(
            handle,
            execution_fence_ref="retired-fence:1",
        ),
    )
    rejected = _call(
        gateway,
        connection,
        "agent_runtime.target_run.observe",
        {"target_ref": "target:1"},
    )
    assert rejected["isError"] is True
    assert rejected["structuredContent"]["code"] == "semantic_call_scope_stale"


def _implementation_content_arguments(effect_id: str) -> dict[str, object]:
    return {
        "effect_id": effect_id,
        "source_ref": "source:semantic-reuse",
        "exact_version_ref": "source-version:semantic-reuse-v1",
        "implementation_revision_ref": "implementation:semantic-reuse-v1",
        "verification_evidence_ref": "evidence:semantic-reuse-pin",
    }


def test_rm_implementation_content_effect_read_and_reconcile_are_exact():
    gateway, _graph, memory, runtime = _gateway()
    operation_ids = (
        "research_memory.implementation_content.accept",
        "research_memory.implementation_content.accept.reconcile",
        "research_memory.implementation_content.read",
    )
    connection = _issue(gateway, operation_ids)
    arguments = _implementation_content_arguments("implementation:accept:1")
    accepted = _call(
        gateway,
        connection,
        "research_memory.implementation_content.accept",
        arguments,
    )
    assert accepted["isError"] is False
    result = accepted["structuredContent"]
    assert result["status"] == "effect_confirmed"
    assert result["accepted"]["source_verification_receipt"] == (
        memory.accepted_content.source_verification_receipt.as_public_dict()
    )
    assert result["accepted"]["content_acceptance_receipt"] == (
        memory.accepted_content.content_acceptance_receipt.as_public_dict()
    )
    assert memory.accept_calls[0]["idempotency_key"].startswith("mcp-effect:")
    assert runtime.scope_checks == 1

    read = _call(
        gateway,
        connection,
        "research_memory.implementation_content.read",
        {"implementation_revision_ref": "implementation:semantic-reuse-v1"},
    )
    assert read["structuredContent"]["status"] == "present"
    reconciled = _call(
        gateway,
        connection,
        "research_memory.implementation_content.accept.reconcile",
        arguments,
    )
    assert reconciled["structuredContent"] == result
    assert runtime.scope_checks == 3

    conflict_arguments = {
        **arguments,
        "verification_evidence_ref": "evidence:different-pin",
    }
    conflict = _call(
        gateway,
        connection,
        "research_memory.implementation_content.accept.reconcile",
        conflict_arguments,
    )
    assert conflict["isError"] is True
    assert conflict["structuredContent"]["code"] == (
        "implementation_content_reconciliation_conflict"
    )


def test_reuse_eligibility_read_and_composite_verification_use_owner_records():
    gateway, graph, memory, runtime = _gateway()
    memory.accept_implementation_content(
        source_ref="source:semantic-reuse",
        exact_version_ref="source-version:semantic-reuse-v1",
        implementation_revision_ref="implementation:semantic-reuse-v1",
        verification_evidence_ref="evidence:semantic-reuse-pin",
        license_ref=None,
        source_content_hash_ref=None,
        patch_ref=None,
        idempotency_key="fixture:implementation",
    )
    content = memory.accepted_content
    payload = {
        "eligible_tier": "accepted-local",
        "eligibility_anchor_ref": "target-commit:reuse-anchor",
        "source_ref": content.source_ref,
        "exact_version_ref": content.exact_version_ref,
        "implementation_revision_ref": content.implementation_revision_ref,
        "implementation_content_hash_ref": content.content_hash_ref,
    }
    payload_hash = canonical_hash(payload)
    graph.eligibility = AcceptedReuseEligibility(
        eligibility_ref="reuse-eligibility:1",
        tier="accepted-local",
        target_commit_ref="target-commit:reuse-anchor",
        source_ref=content.source_ref,
        exact_version_ref=content.exact_version_ref,
        implementation_revision_ref=content.implementation_revision_ref,
        implementation_content_hash_ref=content.content_hash_ref,
        payload=payload,
        payload_hash=payload_hash,
        accepted_at=2.0,
        receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="reuse_eligibility_accepted",
            receipt_ref="rg:reuse-eligibility-receipt",
            subject_ref=payload_hash,
            payload_hash="5" * 64,
        ),
    )
    operation_ids = (
        "research_graph.reuse_eligibility.read",
        "research_graph.reuse_inputs.verify",
    )
    connection = _issue(gateway, operation_ids)
    read = _call(
        gateway,
        connection,
        "research_graph.reuse_eligibility.read",
        {"eligibility_ref": "reuse-eligibility:1"},
    )
    assert read["isError"] is False
    assert read["structuredContent"]["accepted"]["receipt"] == (
        graph.eligibility.receipt.as_public_dict()
    )

    verified = _call(
        gateway,
        connection,
        "research_graph.reuse_inputs.verify",
        {
            "proofs": [
                {
                    "tier": "accepted-local",
                    "source_ref": content.source_ref,
                    "exact_version_ref": content.exact_version_ref,
                    "implementation_revision_ref": (
                        content.implementation_revision_ref
                    ),
                    "verification_receipt": {
                        "receipt_ref": (
                            content.source_verification_receipt.receipt_ref
                        ),
                        "subject_ref": (
                            content.source_verification_receipt.subject_ref
                        ),
                        "verified": True,
                        "currentness_known": True,
                        "current": True,
                    },
                    "implementation_binding": {
                        "subject_ref": content.implementation_revision_ref,
                        "content_hash_ref": content.content_hash_ref,
                    },
                    "implementation_acceptance_receipt": {
                        "receipt_ref": (
                            content.content_acceptance_receipt.receipt_ref
                        ),
                        "subject_ref": (
                            content.content_acceptance_receipt.subject_ref
                        ),
                        "verified": True,
                        "currentness_known": True,
                        "current": True,
                    },
                    "eligibility_anchor_ref": "target-commit:reuse-anchor",
                    "eligibility_binding": {
                        "subject_ref": "reuse-eligibility:1",
                        "content_hash_ref": payload_hash,
                    },
                    "eligibility_receipt": {
                        "receipt_ref": graph.eligibility.receipt.receipt_ref,
                        "subject_ref": graph.eligibility.receipt.subject_ref,
                        "verified": True,
                        "currentness_known": True,
                        "current": True,
                    },
                }
            ]
        },
    )
    assert verified["isError"] is False
    proof = verified["structuredContent"]["proofs"][0]
    assert proof["source_verification_receipt"] == (
        content.source_verification_receipt.as_public_dict()
    )
    assert proof["content_acceptance_receipt"] == (
        content.content_acceptance_receipt.as_public_dict()
    )
    assert proof["eligibility"]["receipt"] == (
        graph.eligibility.receipt.as_public_dict()
    )
    assert len(memory.source_verifications) == 1
    assert len(memory.content_verifications) == 1
    assert len(graph.eligibility_verifications) == 1
    assert runtime.scope_checks == 2
