from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from meta_research.bundle_skill import bind_bundle_runtime_to_full_conformance
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.root_capabilities import ROOT_AGENT_KINDS, RootAgentKind
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
)
from meta_research.web import create_app
from test_harness_full_conformance import _full_request
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _TogglePowerInhibitor,
    _bundle_runtime,
    _prepare_bundle_request,
)


class _OperationBoundBundleSkill(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self._authority = None

    def bind(self, authority) -> None:
        self._authority = authority

    def runtime_binding(self) -> BundleRuntimeBinding:
        binding = replace(
            super().runtime_binding(),
            harness_adapter_ref="codex-cli/0.153.2",
        )
        if self._authority is None:
            return binding
        return bind_bundle_runtime_to_full_conformance(
            binding,
            self._authority.require_operation_binding(
                harness_family="codex",
                required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
                required_capabilities=("semantic_mcp",),
            ),
            required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        )


def _runtime(path: Path):
    provider = _OperationBoundBundleSkill()
    runtime = _bundle_runtime(
        path,
        bundle_skill_provider=provider,
        harness_ready=False,
        power_inhibitor=_TogglePowerInhibitor(),
    )
    provider.bind(runtime.harnesses)
    return runtime


def _bundle_root_channel(runtime):
    runtime.harnesses.start_full_conformance(_full_request())
    for _turn in range(4):
        if runtime.harnesses.query_status()["status"] == "ready":
            break
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8766"
        )
    assert runtime.harnesses.query_status()["status"] == "ready"
    _prepare_bundle_request(runtime)
    assert runtime.bundle_stage.process_once()
    request_ref = runtime.bundle_stage.query_current()["stage_run_request"][
        "request_ref"
    ]
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
    assert run is not None
    channel = runtime.harnesses.issue_resident_mcp_channel(
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        root_session_ref=run.root_session_ref,
        fence_ref=run.fence_ref,
        capability_binding_hash=run.runtime_binding_hash,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
        root_kind="bundle",
        phase="primary",
        subject_policy="operation_tree",
    )
    return run, channel


def _initialized_child(client: TestClient, token: str) -> dict[str, str]:
    authorization = f"Bearer {token}"
    initialized = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "native-child", "version": "1"},
            },
        },
    )
    assert initialized.status_code == 200
    session_id = initialized.headers["mcp-session-id"]
    headers = {
        "Authorization": authorization,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        "Mcp-Session-Id": session_id,
    }
    acknowledged = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )
    assert acknowledged.status_code == 202
    return headers


def _tool_call(
    client: TestClient,
    headers: dict[str, str],
    *,
    operation_id: str,
    arguments: dict[str, object],
    request_id: int,
) -> dict[str, object]:
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": operation_id, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def _open_arguments(
    effect_id: str,
    request_kind: str,
    *,
    predecessor_request_ref: str | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "effect_id": effect_id,
        "request_kind": request_kind,
        "obligation": f"Complete the exact {request_kind} obligation.",
        "business_purpose": "Resume only the current research task.",
        "condition": {
            "impact": "Only the current task is paused.",
            "safe_response": "Return non-secret facts or decline/defer.",
        },
        "acceptance_conditions": [
            "The response is bound to this exact current request."
        ],
    }
    if request_kind == "capability_authorization":
        arguments["required_authorization"] = {
            "capability": "web_fetch",
            "destination": "example.test:443",
            "duration": "one operation",
            "exclusions": ["secrets"],
        }
    if predecessor_request_ref is not None:
        arguments["predecessor_request_ref"] = predecessor_request_ref
    return arguments


def _structured(result: dict[str, object]) -> dict[str, object]:
    assert result["isError"] is False
    value = result["structuredContent"]
    assert isinstance(value, dict)
    return value


def _consistent_snapshot(client: TestClient) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get("/api/v1/snapshot")
        if response.status_code == 200:
            return response.json()
        assert response.status_code == 503, response.json()
        assert response.json()["detail"]["code"] == (
            "snapshot_consistency_unavailable"
        )
        time.sleep(0.02)
    raise AssertionError("Snapshot did not reach a consistent revision")


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_each_root_catalog_routes_one_common_human_request_through_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: RootAgentKind,
) -> None:
    runtime = _runtime(tmp_path / f"root-human-request-{root_kind}")
    try:
        run, _bundle_channel = _bundle_root_channel(runtime)
        owner = runtime.owners.agent_runtime
        managed = owner.query_managed_run(run.run_ref)
        assert managed is not None

        # Per-domain admission is covered by the Root lifecycle tests. This
        # seam isolates catalog selection from the shared MCP/Owner lifecycle.
        def verify_scope(**values: object) -> dict[str, object]:
            assert values["root_kind"] in {None, root_kind}
            assert values["run_ref"] == run.run_ref
            assert values["attempt_ref"] == run.attempt_ref
            assert values["root_session_ref"] == run.root_session_ref
            assert values["fence_ref"] == run.fence_ref
            assert values["runtime_binding_hash"] == run.runtime_binding_hash
            return {
                "run_kind": root_kind,
                "quest_ref": managed["quest_ref"],
                "waiter_ref": f"root_run:{run.run_ref}",
                "waiter_generation": run.attempt_generation,
            }

        monkeypatch.setattr(owner, "_verify_root_agent_runtime_scope", verify_scope)
        channel = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash,
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind],
            root_kind=root_kind,
            phase="primary",
            subject_policy="operation_tree",
        )
        with TestClient(
            create_app(runtime, base_url="http://testserver", control_key="control-key")
        ) as client:
            headers = _initialized_child(client, channel.connection.token)
            effect_id = f"{root_kind}-canonical-offline-action"
            opened = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(effect_id, "offline_action"),
                    request_id=10,
                )
            )
            assert opened["operation_binding"]["root_kind"] == root_kind
            assert opened["task_yield"]["status"] == "yielded"
            assert opened["receipt"]["kind"] == "human_request_open"
            persisted = owner.query_human_request(str(opened["request_ref"]))
            assert persisted is not None
            assert persisted["open_effect"]["receipt"] == opened["receipt"]

            reconciled = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                    arguments={"effect_id": effect_id},
                    request_id=11,
                )
            )
            assert reconciled == opened
    finally:
        runtime.close()


def test_non_root_session_opens_all_kinds_and_reconciles_frozen_effect(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "root-human-request")
    try:
        run, channel = _bundle_root_channel(runtime)
        app = create_app(
            runtime,
            base_url="http://testserver",
            control_key="control-key",
        )
        client = TestClient(app)
        try:
            headers = _initialized_child(client, channel.connection.token)
            managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
            assert managed is not None
            guidance = runtime.owners.research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name="external-guidance.txt",
                    media_type="text/plain; charset=utf-8",
                    content=b"Use the approved external request route.\n",
                ),
                idempotency_key="external-human-request-guidance",
            )
            assert guidance.asset is not None
            invalid_guidance_arguments = _open_arguments(
                "child-invalid-guidance", "external_material_api_access"
            )
            invalid_guidance_arguments["condition"]["guidance_asset_ref"] = (
                "asset_version_missing_guidance"
            )
            invalid_guidance = _tool_call(
                client,
                headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments=invalid_guidance_arguments,
                request_id=9,
            )
            assert invalid_guidance["isError"] is True
            assert invalid_guidance["structuredContent"]["code"] == (
                "root_agent_human_request_guidance_invalid"
            )
            opened: list[dict[str, object]] = []
            for index, kind in enumerate(
                (
                    "library_reconnect",
                    "external_material_api_access",
                    "offline_action",
                    "capability_authorization",
                    "system_operation_help",
                ),
                10,
            ):
                effect_id = f"child-{kind}"
                arguments = _open_arguments(effect_id, kind)
                if kind == "external_material_api_access":
                    arguments["obligation"] = "  Apply for the exact dataset.  "
                    arguments["business_purpose"] = (
                        "\nResume the current material acquisition unchanged.\n"
                    )
                    arguments["condition"]["guidance_asset_ref"] = (
                        guidance.asset.version_ref
                    )
                result = _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=arguments,
                    request_id=index,
                )
                value = _structured(result)
                assert value["status"] == "condition_reported"
                assert value["effect_id"] == effect_id
                assert value["request_owner"] == "agent_runtime"
                assert value["operation_binding"] == {
                    "quest_ref": managed["quest_ref"],
                    "task_ref": run.run_ref,
                    "root_session_ref": run.root_session_ref,
                    "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    "attempt_ref": run.attempt_ref,
                    "generation": run.attempt_generation,
                    "request_owner": "agent_runtime",
                    "root_kind": "bundle",
                    "phase": "primary",
                    "fence_ref": run.fence_ref,
                    "runtime_binding_hash": run.runtime_binding_hash,
                }
                assert value["waiter"] == {
                    "waiter_ref": f"root_run:{run.run_ref}",
                    "generation": run.attempt_generation,
                }
                assert value["task_yield"]["status"] == "yielded"
                receipt = value["receipt"]
                assert receipt["issuer"] == "agent_runtime"
                assert receipt["kind"] == "human_request_open"

                request = runtime.owners.agent_runtime.query_human_request(
                    value["request_ref"]
                )
                assert request is not None
                assert request["kind"] == kind
                assert request["obligation"] == arguments["obligation"]
                assert request["business_purpose"] == arguments["business_purpose"]
                assert request["target_assertion"]["condition"] == arguments[
                    "condition"
                ]
                assert request["open_effect"]["receipt"] == receipt
                assert request["open_effect"]["operation_binding"] == value[
                    "operation_binding"
                ]
                assert len(request["direct_waiters"]) == 1
                if kind == "capability_authorization":
                    assert request["required_authorization"] == {
                        "capability": "web_fetch",
                        "scope": {
                            "schema_ref": (
                                "meta-research/"
                                "root-human-request-capability-scope/v1"
                            ),
                            "human_request_effect_id": effect_id,
                            "quest_ref": managed["quest_ref"],
                            "task_ref": run.run_ref,
                            "root_session_ref": run.root_session_ref,
                            "operation_id": (
                                ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0]
                            ),
                            "attempt_ref": run.attempt_ref,
                            "generation": run.attempt_generation,
                            "destination": "example.test:443",
                            "duration": "one operation",
                            "exclusions": ["secrets"],
                        },
                    }
                opened.append(value)

                reconciled = _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                    arguments={"effect_id": effect_id},
                    request_id=index + 100,
                )
                assert _structured(reconciled) == value
                runtime.owners.human_collaboration.respond_to_human_request(
                    str(value["request_ref"]),
                    decision="declined",
                    facts={},
                    note="Use the safe alternative for this test.",
                    idempotency_key=f"decline-{effect_id}",
                )

            missing = _tool_call(
                client,
                headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                arguments={"effect_id": "effect-that-was-never-opened"},
                request_id=60,
            )
            assert missing["isError"] is True
            assert missing["structuredContent"] == {
                "status": "capability_unavailable",
                "code": "effect_not_found",
            }

            forged = _tool_call(
                client,
                headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments={
                    **_open_arguments("forged-owner", "offline_action"),
                    "quest_ref": "quest:forged",
                    "request_owner": "research_graph",
                    "generation": 999,
                },
                request_id=61,
            )
            assert forged["isError"] is True
            assert forged["structuredContent"]["code"] == (
                "semantic_input_schema_mismatch"
            )
        finally:
            client.close()
    finally:
        runtime.close()


def test_non_root_agent_external_request_round_trips_raw_business_text_and_resumes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "root-human-request-raw-business-text")
    try:
        run, channel = _bundle_root_channel(runtime)
        app = create_app(
            runtime,
            base_url="http://testserver",
            control_key="control-key",
        )
        with TestClient(app, base_url="http://testserver") as client:
            agent_headers = _initialized_child(client, channel.connection.token)
            managed_before = runtime.owners.agent_runtime.query_managed_run(
                run.run_ref
            )
            assert managed_before is not None
            assert managed_before["status"] == "running"

            effect_id = "child-external-raw-business-text"
            obligation = "  Fetch fixture with token=test-only-token-147.  "
            business_purpose = (
                "\nResume this current operation with "
                "password=test-only-password-147 unchanged.\n"
            )
            condition = {
                "impact": (
                    "Cookie: test_session=test-only-cookie-147 is literal "
                    "test data; only this operation waits."
                ),
                "safe_response": "Return one natural-language status update.",
            }
            opened = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments={
                        "effect_id": effect_id,
                        "request_kind": "external_material_api_access",
                        "obligation": obligation,
                        "business_purpose": business_purpose,
                        "condition": condition,
                        "acceptance_conditions": [
                            "The response addresses this exact current operation."
                        ],
                    },
                    request_id=20,
                )
            )
            request_ref = str(opened["request_ref"])
            assert opened["operation_binding"]["task_ref"] == run.run_ref
            assert opened["operation_binding"]["root_session_ref"] == (
                run.root_session_ref
            )
            assert opened["condition"] == {
                "schema_ref": "meta-research/blocking-human-request-condition/v1",
                "code": "blocking_human_request",
                "request_ref": request_ref,
                "request_kind": "external_material_api_access",
                "request_status": "open",
                "waiter_status": "blocked",
                "resume_requirement": "human_response_required",
            }

            owner_request = runtime.owners.agent_runtime.query_human_request(
                request_ref
            )
            assert owner_request is not None
            assert owner_request["obligation"] == obligation
            assert owner_request["business_purpose"] == business_purpose
            assert owner_request["target_assertion"]["condition"] == condition

            bootstrap = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": "http://testserver"},
                json={"token": bootstrap},
            )
            assert authenticated.status_code == 200
            auth_headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": authenticated.json()["csrf_token"],
            }
            before_response = _consistent_snapshot(client)
            snapshot_item = next(
                item
                for item in before_response["human_collaboration"]["human_requests"][
                    "items"
                ]
                if item["request_ref"] == request_ref
            )
            assert snapshot_item["obligation"] == obligation
            assert snapshot_item["business_purpose"] == business_purpose
            assert snapshot_item["target_assertion"]["condition"] == condition

            response_facts = {
                "operator_report": (
                    "Provided the test fixture with token=test-only-token-147, "
                    "password=test-only-password-147, and "
                    "Cookie: test_session=test-only-cookie-147."
                )
            }
            response_note = "  The requested test fixture is now available.\n"
            posted = client.post(
                f"/api/v1/human-requests/{quote(request_ref, safe='')}/responses",
                headers={
                    **auth_headers,
                    "Idempotency-Key": "raw-business-text-response",
                },
                json={
                    "decision": "provided",
                    "facts": response_facts,
                    "note": response_note,
                },
            )
            assert posted.status_code == 201, posted.json()
            response = posted.json()
            assert response["decision"] == "provided"
            assert response["facts"] == response_facts
            assert response["note"] == response_note

            current = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert current is not None
            assert current["responses"] == [response]
            evaluation = current["evaluation"]
            disposition = current["disposition"]
            waiter = current["direct_waiters"][0]
            validation = waiter["resume_validation"]
            consumption = validation["consumption"]
            assert evaluation["response_refs"] == [response["response_ref"]]
            assert evaluation["decision"] == "satisfied"
            assert evaluation["reason"] == {"code": "human_response_accepted"}
            assert disposition["evaluation_ref"] == evaluation["evaluation_ref"]
            assert disposition["decision"] == "satisfied"
            assert validation["status"] == "released"
            assert waiter["status"] == "consumed"
            assert consumption["request_ref"] == request_ref
            assert consumption["waiter_ref"] == opened["waiter"]["waiter_ref"]
            assert consumption["generation"] == opened["waiter"]["generation"]
            assert consumption["work_ref"] == run.run_ref

            after_response = _consistent_snapshot(client)
            response_item = next(
                item
                for item in after_response["human_collaboration"]["human_requests"][
                    "items"
                ]
                if item["request_ref"] == request_ref
            )
            assert response_item["responses"] == [response]
            assert response_item["evaluation"] == evaluation
            assert response_item["disposition"] == disposition
            assert response_item["direct_waiters"] == current["direct_waiters"]

            reconciled = _structured(
                _tool_call(
                    client,
                    agent_headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                    arguments={"effect_id": effect_id},
                    request_id=21,
                )
            )
            assert reconciled["receipt"] == opened["receipt"]
            assert reconciled["operation_binding"] == opened["operation_binding"]
            assert reconciled["task_yield"] == opened["task_yield"]
            assert reconciled["condition"]["request_status"] == "satisfied"
            assert reconciled["condition"]["waiter_status"] == "consumed"
            assert reconciled["condition"]["resume_requirement"] == "consumed"
            assert reconciled["resolution"] == {
                "response_ref": response["response_ref"],
                "decision": "provided",
                "facts": response_facts,
                "note": response_note,
                "disposition": "satisfied",
                "reason_code": "human_response_accepted",
                "accepted_evidence_refs": [],
            }
            managed_after = runtime.owners.agent_runtime.query_managed_run(
                run.run_ref
            )
            assert managed_after is not None
            assert managed_after["status"] == "running"
            assert managed_after["root_session_ref"] == run.root_session_ref
    finally:
        runtime.close()


def test_agent_system_help_repeat_failure_reuses_public_effect_revision(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "root-system-help-repeat-failure")
    try:
        run, channel = _bundle_root_channel(runtime)
        with TestClient(
            create_app(
                runtime,
                base_url="http://testserver",
                control_key="control-key",
            )
        ) as client:
            headers = _initialized_child(client, channel.connection.token)
            effect_id = "retry-the-same-failed-operation"
            opened = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(effect_id, "system_operation_help"),
                    request_id=30,
                )
            )
            request_ref = str(opened["request_ref"])
            runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision="provided",
                facts={"action": "retry"},
                note="",
                idempotency_key="agent-system-help-first-retry",
            )
            first = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert first is not None
            assert first["status"] == "satisfied"
            assert first["direct_waiters"][0]["status"] == "consumed"

            repeated_result = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(
                        effect_id,
                        "system_operation_help",
                        predecessor_request_ref=request_ref,
                    ),
                    request_id=31,
                )
            )
            repeated = runtime.owners.agent_runtime.query_human_request(
                str(repeated_result["request_ref"])
            )
            assert repeated is not None
            assert repeated["request_id"] == first["request_id"]
            assert repeated["request_ref"] == f"{first['request_id']}:r2"
            assert repeated["revision"] == 2
            assert repeated["predecessor_request_ref"] == request_ref
            assert repeated["open_effect"]["operation_binding"]["task_ref"] == (
                run.run_ref
            )

            reconciled = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                    arguments={"effect_id": effect_id},
                    request_id=32,
                )
            )
            assert reconciled["request_ref"] == repeated["request_ref"]
            assert reconciled["lineage"]["predecessor_request_ref"] == request_ref
    finally:
        runtime.close()


def test_bundle_root_accepts_exact_target_authorization_command(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "bundle-root-exact-target-authorization")
    try:
        run, channel = _bundle_root_channel(runtime)
        app = create_app(
            runtime,
            base_url="http://testserver",
            control_key="control-key",
        )
        client = TestClient(app)
        try:
            headers = _initialized_child(client, channel.connection.token)
            managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
            assert managed is not None
            target_assertion = {
                "schema_ref": "meta-research/target-execution-assertion/v1",
                "operation": "execute_target",
                "quest_ref": managed["quest_ref"],
                "stage_request_ref": "stage-request-exact-target",
                "graph_ref": "target-graph-exact-target",
                "target_ref": "target-exact-target",
                "target_spec_hash": "a" * 64,
                "risk_class": "high",
            }
            condition = {
                "schema_ref": "meta-research/root-agent-human-request-target/v1",
                "root": {
                    "run_kind": "bundle_stage",
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "root_session_ref": run.root_session_ref,
                    "fence_ref": run.fence_ref,
                    "waiter_generation": run.attempt_generation,
                },
                "condition": target_assertion,
            }
            requirement = {
                "capability": "execute_high_risk_target",
                "scope": {
                    "authorization_mode": "single_target",
                    "quest_ref": managed["quest_ref"],
                    "stage_request_ref": "stage-request-exact-target",
                    "graph_ref": "target-graph-exact-target",
                    "target_ref": "target-exact-target",
                    "target_spec_hash": "a" * 64,
                },
            }
            result = _tool_call(
                client,
                headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments={
                    "effect_id": "bundle-exact-target-authorization",
                    "request_kind": "capability_authorization",
                    "obligation": "Authorize only this exact high-risk Target.",
                    "business_purpose": "Resume only this exact Bundle root.",
                    "condition": condition,
                    "acceptance_conditions": [
                        "An exact independent authorization receipt is current."
                    ],
                    "required_authorization": requirement,
                },
                request_id=70,
            )
            value = _structured(result)
            request = runtime.owners.agent_runtime.query_human_request(
                str(value["request_ref"])
            )
            assert request is not None
            assert request["target_assertion"] == condition
            assert request["required_authorization"] == requirement
        finally:
            client.close()
    finally:
        runtime.close()


def test_response_disposition_resume_and_successor_are_distinct_exact_facts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "root-human-request-resume")
    try:
        run, channel = _bundle_root_channel(runtime)
        app = create_app(
            runtime,
            base_url="http://testserver",
            control_key="control-key",
        )
        client = TestClient(app)
        try:
            headers = _initialized_child(client, channel.connection.token)
            opened = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments("deferred-original", "offline_action"),
                    request_id=10,
                )
            )
            request_ref = str(opened["request_ref"])
            response = runtime.owners.human_collaboration.respond_to_human_request(
                request_ref,
                decision="deferred",
                facts={"safe_route": "ask_domain_owner"},
                note="Use an alternative route for now; revisit revision B.",
                idempotency_key="deferred-root-response",
            )
            terminal = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert terminal is not None
            assert terminal["responses"] == [response]
            assert terminal["status"] == "unsatisfied"
            assert terminal["evaluation"]["decision"] == "unsatisfied"
            assert terminal["disposition"]["decision"] == "unsatisfied"
            waiter = terminal["direct_waiters"][0]
            assert waiter["resume_validation"]["status"] == "released"
            assert waiter["status"] == "consumed"
            consumption = waiter["resume_validation"]["consumption"]
            assert consumption["request_ref"] == request_ref
            assert consumption["waiter_ref"] == f"root_run:{run.run_ref}"
            assert consumption["generation"] == run.attempt_generation
            assert consumption["work_ref"] == run.run_ref

            reconciled = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                    arguments={"effect_id": "deferred-original"},
                    request_id=12,
                )
            )
            assert reconciled["resolution"] == {
                "response_ref": response["response_ref"],
                "decision": "deferred",
                "facts": {"safe_route": "ask_domain_owner"},
                "note": "Use an alternative route for now; revisit revision B.",
                "disposition": "unsatisfied",
                "reason_code": "human_deferred_exact_obligation",
                "accepted_evidence_refs": [],
            }

            replayed_response = (
                runtime.owners.human_collaboration.respond_to_human_request(
                    request_ref,
                    decision="deferred",
                    facts={"safe_route": "ask_domain_owner"},
                    note="Use an alternative route for now; revisit revision B.",
                    idempotency_key="deferred-root-response",
                )
            )
            assert replayed_response == response
            replayed = runtime.owners.agent_runtime.query_human_request(request_ref)
            assert replayed is not None
            assert replayed["direct_waiters"][0]["resume_validation"][
                "consumption"
            ] == consumption

            successor = _structured(
                _tool_call(
                    client,
                    headers,
                    operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                    arguments=_open_arguments(
                        "deferred-successor",
                        "offline_action",
                        predecessor_request_ref=request_ref,
                    ),
                    request_id=11,
                )
            )
            assert successor["request_ref"] != request_ref
            assert successor["receipt"] != opened["receipt"]
            assert successor["lineage"]["predecessor_request_ref"] == request_ref
            assert successor["waiter"]["generation"] > waiter["generation"]

            predecessor = runtime.owners.agent_runtime.query_human_request(request_ref)
            current = runtime.owners.agent_runtime.query_human_request(
                successor["request_ref"]
            )
            assert predecessor is not None and current is not None
            assert predecessor["successor_request_ref"] == successor["request_ref"]
            assert current["predecessor_request_ref"] == request_ref
            assert current["direct_waiters"][0]["status"] == "blocked"
            assert current["direct_waiters"][0]["resume_validation"] is None
        finally:
            client.close()
    finally:
        runtime.close()
