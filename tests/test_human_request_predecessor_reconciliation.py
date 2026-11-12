from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.semantic_owner_gateway import BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
from meta_research.web import create_app
from test_root_human_request_lifecycle import (
    _bundle_root_channel, _initialized_child, _open_arguments, _runtime,
    _structured, _tool_call,
)


def _call(client, headers, operation, arguments):
    return _tool_call(client, headers, operation_id=operation, arguments=arguments, request_id=50)


def _retry(runtime, request_ref, key):
    runtime.owners.human_collaboration.respond_to_human_request(
        request_ref, decision="provided", facts={"action": "retry"},
        note="", idempotency_key=key,
    )


@pytest.mark.parametrize("case", ["missing", "different_effect", "foreign_root"])
def test_predecessor_rejection_proves_no_open_and_allows_corrected_new_request(
    tmp_path: Path, monkeypatch, case: str,
) -> None:
    runtime = _runtime(tmp_path / "predecessor-rejection")
    try:
        _, channel = _bundle_root_channel(runtime)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key")) as client:
            headers = _initialized_child(client, channel.connection.token)
            predecessor = "human_request_missing:r1"
            if case != "missing":
                first = _structured(_call(client, headers, "human_request.open", _open_arguments("old-failure", "system_operation_help")))
                predecessor = first["request_ref"]
                _retry(runtime, predecessor, "first-retry")
            owner = runtime.owners.agent_runtime
            if case == "foreign_root":
                original_query = owner.query_human_request

                def foreign_predecessor(request_ref, **kwargs):
                    value = original_query(request_ref, **kwargs)
                    if request_ref == predecessor:
                        value["open_effect"]["operation_binding"]["root_session_ref"] = "another-root"
                    return value

                monkeypatch.setattr(owner, "query_human_request", foreign_predecessor)
            original_open = owner.open_human_request_effect
            opens = []

            def track_open(**values):
                opens.append(values)
                return original_open(**values)

            monkeypatch.setattr(owner, "open_human_request_effect", track_open)
            arguments = _open_arguments("new-unrelated-failure", "system_operation_help", predecessor_request_ref=predecessor)
            rejected = _call(client, headers, "human_request.open", arguments)
            assert rejected["isError"] is True
            error = rejected["structuredContent"]
            assert error["code"] == "human_request_predecessor_invalid"
            assert error["details"]["effect_status"] == "not_applied"
            assert error["details"]["call_execution"] == "not_started"
            assert error["details"]["execution_stage"] == "precondition_validation"
            assert opens == []
            if case == "different_effect":
                assert error["details"]["expected_effect_id"] == "old-failure"
                assert owner.query_human_request(predecessor)["status"] == "satisfied"
            elif case == "foreign_root":
                assert "expected_effect_id" not in error["details"]
            corrected = dict(arguments)
            corrected.pop("predecessor_request_ref")
            opened = _structured(_call(client, headers, "human_request.open", corrected))
            assert len(opens) == 1
            assert opened["request_ref"] != predecessor
            assert _structured(_call(client, headers, "human_request.open.reconcile", {"effect_id": corrected["effect_id"]}))["receipt"] == opened["receipt"]
    finally:
        runtime.close()


def test_system_help_successor_reconciles_exact_revision_across_logical_phase(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "successor-reconciliation")
    try:
        run, original_channel = _bundle_root_channel(runtime)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key")) as client:
            original_headers = _initialized_child(client, original_channel.connection.token)
            effect_id = "same-system-failure"
            first = _structured(_call(client, original_headers, "human_request.open", _open_arguments(effect_id, "system_operation_help")))
            _retry(runtime, first["request_ref"], "retry-first")
            channel = runtime.harnesses.issue_resident_mcp_channel(
                run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
                capability_binding_hash=run.runtime_binding_hash,
                operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS, root_kind="bundle",
                phase="target-batch-2", subject_policy="operation_tree",
            )
            headers = _initialized_child(client, channel.connection.token)
            second = _structured(_call(client, headers, "human_request.open", _open_arguments(effect_id, "system_operation_help", predecessor_request_ref=first["request_ref"])))
            exact_second = {"effect_id": effect_id, "predecessor_request_ref": first["request_ref"]}
            assert _structured(_call(client, headers, "human_request.open.reconcile", exact_second))["receipt"] == second["receipt"]
            padded_second = {**exact_second, "predecessor_request_ref": " " + first["request_ref"] + " "}
            assert _structured(_call(client, headers, "human_request.open.reconcile", padded_second))["receipt"] == second["receipt"]
            _retry(runtime, second["request_ref"], "retry-second")
            third = _structured(_call(client, headers, "human_request.open", _open_arguments(effect_id, "system_operation_help", predecessor_request_ref=second["request_ref"])))
            exact_third = {"effect_id": effect_id, "predecessor_request_ref": second["request_ref"]}
            assert _structured(_call(client, headers, "human_request.open.reconcile", exact_second))["receipt"] == second["receipt"]
            assert _structured(_call(client, headers, "human_request.open.reconcile", exact_third))["receipt"] == third["receipt"]
            # Original lineage lookup remains compatible. A different phase or
            # predecessor does not cause a fallback search for a convenient key.
            assert _structured(_call(client, original_headers, "human_request.open.reconcile", {"effect_id": effect_id}))["request_ref"] == third["request_ref"]
            for arguments in [{"effect_id": effect_id}, {"effect_id": effect_id, "predecessor_request_ref": "human_request_wrong:r1"}]:
                rejected = _call(client, headers, "human_request.open.reconcile", arguments)
                assert rejected["isError"] is True
                assert rejected["structuredContent"]["code"] == "effect_not_found"
                assert rejected["structuredContent"]["details"]["effect_status"] == "unknown"
    finally:
        runtime.close()


def test_lost_response_after_effect_still_requires_reconciliation(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = _runtime(tmp_path / "lost-response")
    try:
        _, channel = _bundle_root_channel(runtime)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key")) as client:
            headers = _initialized_child(client, channel.connection.token)
            owner = runtime.owners.agent_runtime
            original_open = owner.open_human_request_effect
            receipts = []

            def lose_response(**values):
                receipts.append(original_open(**values))
                raise RuntimeError("simulated response loss after commit")

            monkeypatch.setattr(owner, "open_human_request_effect", lose_response)
            result = _call(client, headers, "human_request.open", _open_arguments("lost-response", "system_operation_help"))
            assert result["isError"] is True
            assert result["structuredContent"]["details"]["effect_status"] == "unknown"
            reconciled = _structured(_call(client, headers, "human_request.open.reconcile", {"effect_id": "lost-response"}))
            assert len(receipts) == 1
            assert reconciled["request_ref"] == receipts[0]["request_ref"]
    finally:
        runtime.close()
