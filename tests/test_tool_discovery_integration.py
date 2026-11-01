"""Discover and call tools through admitted Bundle roots and public MCP HTTP."""
from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_root_human_request_lifecycle import (
    _bundle_root_channel,
    _initialized_child,
    _open_arguments,
    _runtime,
    _structured,
    _tool_call,
)


def test_bundle_review_channel_discovers_only_its_grant_and_revocation_is_live(tmp_path):
    runtime = _runtime(tmp_path / "review-discovery")
    try:
        run, _root_channel = _bundle_root_channel(runtime)
        operations = (
            "tools.catalog.page", "tools.contracts.read", "research_graph.datasets.page",
        )
        channel = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash,
            operation_ids=operations, root_kind="bundle", phase="review",
            subject_policy="review_tree",
        )
        with TestClient(create_app(runtime, base_url="http://testserver",
                                   control_key="control-key")) as client:
            headers = _initialized_child(client, channel.connection.token)

            def call(name, arguments):
                return _tool_call(client, headers, operation_id=name,
                                  arguments=arguments, request_id=10)

            # A known reader works before either discovery operation is called.
            assert call("research_graph.datasets.page", {})["isError"] is False
            catalog = _structured(call("tools.catalog.page", {}))
            assert {item["operation_id"] for item in catalog["items"]} == set(operations)
            listed = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 11, "method": "tools/list",
            })
            assert listed.status_code == 200
            tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
            contract = _structured(call("tools.contracts.read", {
                "operation_id": "research_graph.datasets.page",
            }))
            assert contract["inputSchema"] == tools["research_graph.datasets.page"]["inputSchema"]
            assert contract["outputSchema"] == tools["research_graph.datasets.page"]["outputSchema"]

            # The full root has this effect; the review channel never inherits it.
            for name, arguments in (
                ("tools.contracts.read", {"operation_id": "research_graph.datasets.register"}),
                ("research_graph.datasets.register", {}),
            ):
                denied = call(name, arguments)
                assert denied["isError"] is True
                assert denied["structuredContent"]["code"] == "capability_unavailable"

            runtime.harnesses.revoke_resident_mcp_channel(channel)
            for name, arguments in (
                ("tools.catalog.page", {}),
                ("tools.contracts.read", {"operation_id": "research_graph.datasets.page"}),
                ("research_graph.datasets.page", {}),
            ):
                revoked = client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 12, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                assert revoked.status_code == 401
                assert revoked.json()["error"]["code"] == "mcp_channel_authentication_required"
    finally:
        runtime.close()


def test_bundle_discovery_tracks_human_wait_and_same_root_resume(tmp_path):
    runtime = _runtime(tmp_path / "waiting-discovery")
    try:
        run, channel = _bundle_root_channel(runtime)
        with TestClient(create_app(runtime, base_url="http://testserver",
                                   control_key="control-key")) as client:
            headers = _initialized_child(client, channel.connection.token)

            def call(name, arguments):
                return _tool_call(client, headers, operation_id=name,
                                  arguments=arguments, request_id=20)

            before = _structured(call("tools.catalog.page", {"limit": 100}))
            before_ids = {item["operation_id"] for item in before["items"]}
            first_page = _structured(call("tools.catalog.page", {"limit": 1}))
            assert first_page["next_offset"] == 1
            reader = "research_graph.datasets.page"
            assert reader in before_ids
            opened = _structured(call("human_request.open", _open_arguments(
                "wait-for-required-material", "offline_action",
            )))
            assert opened["operation_binding"]["task_ref"] == run.run_ref
            assert opened["condition"]["request_status"] == "open"
            assert opened["task_yield"]["status"] == "yielded"

            waiting = _structured(call("tools.catalog.page", {"limit": 100}))
            waiting_ids = {item["operation_id"] for item in waiting["items"]}
            assert waiting_ids < before_ids
            assert {"tools.catalog.page", "tools.contracts.read",
                    "human_request.open.reconcile"} <= waiting_ids
            assert reader not in waiting_ids
            assert "human_request.open" not in waiting_ids
            assert waiting["catalog_hash"] != before["catalog_hash"]
            stale_page = call("tools.catalog.page", {
                "offset": first_page["next_offset"], "limit": 1,
                "catalog_hash": first_page["catalog_hash"],
            })
            assert stale_page["isError"] is True
            assert stale_page["structuredContent"]["code"] == "tool_catalog_changed"
            listed = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 22, "method": "tools/list",
            })
            assert listed.status_code == 200
            assert {tool["name"] for tool in listed.json()["result"]["tools"]} == waiting_ids
            contract = _structured(call("tools.contracts.read", {
                "operation_id": "human_request.open.reconcile",
            }))
            assert contract["name"] == "human_request.open.reconcile"
            denied_contract = call("tools.contracts.read", {"operation_id": reader})
            assert denied_contract["isError"] is True
            assert denied_contract["structuredContent"]["code"] == "capability_unavailable"
            denied_reader = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 21, "method": "tools/call",
                "params": {"name": reader, "arguments": {}},
            })
            assert denied_reader.status_code == 401
            reconciled = _structured(call("human_request.open.reconcile", {
                "effect_id": "wait-for-required-material",
            }))
            assert reconciled["request_ref"] == opened["request_ref"]

            # A real HC response releases this exact root without inventing a new grant.
            response = runtime.owners.human_collaboration.respond_to_human_request(
                opened["request_ref"], decision="deferred",
                facts={"safe_route": "continue_independent_work"},
                note="Continue independent work while this material remains unavailable.",
                idempotency_key="resume-discovery-root",
            )
            assert response["decision"] == "deferred"
            resumed = _structured(call("tools.catalog.page", {"limit": 100}))
            assert {item["operation_id"] for item in resumed["items"]} == before_ids
            assert call(reader, {})["isError"] is False
    finally:
        runtime.close()
