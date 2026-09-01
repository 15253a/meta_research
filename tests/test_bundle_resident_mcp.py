from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.harness import (
    HarnessAdmissionError,
)
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
)
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


def test_bundle_resident_mcp_channel_is_bound_to_current_ar_scope(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "resident-mcp", harness_ready=False
    )
    try:
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
        initialized, initialize_response, mcp_session_id = (
            runtime.harnesses.dispatch_mcp_http(
                channel.connection.token,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "bundle-root", "version": "1"},
                    },
                },
                mcp_session_id=None,
            )
        )
        assert initialized == 200
        assert initialize_response is not None
        assert isinstance(mcp_session_id, str) and mcp_session_id
        acknowledged, acknowledgement, _ = (
            runtime.harnesses.dispatch_mcp_http(
                channel.connection.token,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                mcp_session_id=mcp_session_id,
            )
        )
        assert acknowledged == 202
        assert acknowledgement is None
        status, response, _ = runtime.harnesses.dispatch_mcp_http(
            channel.connection.token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            mcp_session_id=mcp_session_id,
        )
        assert status == 200
        assert response is not None
        tools = response["result"]["tools"]
        assert [item["name"] for item in tools] == list(
            BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        )

        with pytest.raises(
            HarnessAdmissionError, match="mcp_channel_scope_invalid"
        ):
            runtime.harnesses.issue_resident_mcp_channel(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref,
                capability_binding_hash=run.runtime_binding_hash,
                operation_ids=("research_graph.snapshot.read",),
                root_kind="bundle",
                phase="primary",
                subject_policy="operation_tree",
            )

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_execution_fences SET status = 'rejected', "
                    "closed_at = 1.0 "
                    "WHERE fence_ref = :fence_ref"
                ),
                {"fence_ref": run.fence_ref},
            )
        status, response = runtime.harnesses.dispatch_mcp(
            channel.connection.token,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert status == 401
        assert response is not None
        assert response["error"]["code"] == "mcp_channel_authentication_required"

        runtime.harnesses.revoke_resident_mcp_channel(channel)
        status, response = runtime.harnesses.dispatch_mcp(
            channel.connection.token,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        assert status == 401
        assert response is not None
        assert response["error"]["code"] == "mcp_channel_authentication_required"

        with pytest.raises(HarnessAdmissionError, match="attempt_fence_invalid"):
            runtime.harnesses.issue_resident_mcp_channel(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref,
                fence_ref="stale-bundle-fence",
                capability_binding_hash=run.runtime_binding_hash,
                operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
                root_kind="bundle",
                phase="primary",
                subject_policy="operation_tree",
            )
    finally:
        runtime.close()


def test_bundle_resident_mcp_channel_allows_exact_executed_submission_scope(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "resident-mcp-awaiting-acceptance",
        harness_ready=False,
    )
    try:
        _prepare_bundle_request(runtime)
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            run_view = current["run"]
            if (
                run_view is not None
                and run_view["attempt_execution_receipt"] is not None
            ):
                break
        else:
            raise AssertionError("Bundle did not reach durable execution")

        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        with runtime._database.read() as connection:
            statuses = connection.execute(
                text(
                    "SELECT runs.status AS run_status, "
                    "attempts.status AS attempt_status, "
                    "fences.status AS fence_status FROM ar_stage_runs runs "
                    "JOIN ar_stage_attempts attempts ON attempts.attempt_ref = "
                    "runs.current_attempt_ref JOIN ar_execution_fences fences ON "
                    "fences.fence_ref = runs.current_fence_ref WHERE runs.run_ref = "
                    ":run_ref"
                ),
                {"run_ref": run.run_ref},
            ).one()
        assert tuple(statuses) == (
            "awaiting_acceptance",
            "executed",
            "submitted",
        )

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
        runtime.harnesses.revoke_resident_mcp_channel(channel)
    finally:
        runtime.close()
