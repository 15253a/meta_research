from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meta_research.bundle_protocol import (
    ReceiptProof,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import (
    CodexHarnessAdapter,
    HarnessSupervisorTransport,
)
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessError,
    SQLiteAgentRuntimeHarness,
)
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.target_raw_output import TargetRawOutputStore
from meta_research.target_run_runtime_contract import TargetCompletionHandoff
from meta_research.web import create_app


def _target_scope(suffix: str = "observed") -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-root-observation-scope/v1",
        "target_run_ref": f"target-run-{suffix}",
        "attempt_ref": f"target-attempt-{suffix}",
        "attempt_generation": 2,
        "root_session_ref": f"target-root-session-{suffix}",
        "fence_ref": f"target-fence-{suffix}",
        "native_session_ref": None,
    }


def _event(
    sequence: int,
    text: str | None = None,
    *,
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    event_scope = _target_scope() if scope is None else scope
    summary: dict[str, object] = {
        "kind": "thread.started" if sequence == 1 else "item.completed",
        "native_session_ref": "native-target-root",
        "target_run_scope": event_scope,
    }
    event_ref_prefix = "harness_evidence:"
    stored_sequence = sequence
    if text is not None:
        event_ref_prefix = "harness_observation:"
        stored_sequence = 1_000_000_000 + sequence
        summary = {
            "kind": "target_root_observation",
            "target_run_scope": event_scope,
        }
        summary["target_root_observation"] = {
            "schema_ref": "meta-research/target-root-observation/v1",
            "scope": event_scope,
            "root_native_session_ref": "native-target-root",
            "kind": "command_output",
            "stream": "stdout",
            "text": text,
            "redacted": "REDACTED" in text,
            "truncated": False,
            "raw_sequence": sequence,
        }
    return {
        "event_ref": event_ref_prefix
        + canonical_hash({"target-root-sequence": sequence, "text": text}),
        "sequence": stored_sequence,
        **summary,
    }


def _seed_running_target_harness(
    path: Path,
    *,
    admitted_target: bool = False,
    suffix: str = "observed",
) -> None:
    target_ref = f"target-{suffix}"
    target_run_ref = f"target-run-{suffix}"
    operation_ref = f"target-provider-operation-{suffix}"
    request = {
        "request_ref": f"target-harness-request-{suffix}",
        "harness_family": "codex",
        "model_ref": f"gpt-{suffix}",
        "auth_profile_ref": f"harness-profile:{suffix}",
    }
    full_conformance_binding: dict[str, object] = {}
    full_conformance_binding_hash = (
        canonical_hash(full_conformance_binding) if admitted_target else "c" * 64
    )
    target_scope_binding_hash = (
        canonical_hash(_target_scope(suffix))
        if admitted_target
        else "d" * 64
    )
    if admitted_target:
        request.update(
            {
                "target_ref": target_ref,
                "target_run_ref": target_run_ref,
                "full_conformance_binding": full_conformance_binding,
                "full_conformance_binding_hash": full_conformance_binding_hash,
                "target_scope_binding_hash": target_scope_binding_hash,
            }
        )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ar_harness_runs (request_ref, idempotency_key, "
            "request_json, request_hash, run_ref, attempt_ref, "
            "attempt_generation, root_session_ref, native_session_ref, "
            "fence_ref, harness_family, model_ref, auth_profile_ref, "
            "capability_binding_hash, mcp_binding_json, mcp_binding_hash, "
            "profile_json, profile_hash, failure_code, status, created_at, "
            "updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, 2, ?, NULL, "
            "?, 'codex', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 'running', "
            "1.0, 1.0, NULL)",
            (
                request["request_ref"],
                f"target-harness-{suffix}",
                canonical_json(request),
                canonical_hash(request),
                target_run_ref,
                f"target-attempt-{suffix}",
                f"target-root-session-{suffix}",
                f"target-fence-{suffix}",
                request["model_ref"],
                request["auth_profile_ref"],
                "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO ar_harness_provider_operations (operation_ref, run_ref, "
            "generation, invocation_hash, status, outcome_code, created_at, "
            "completed_at) VALUES (?, ?, 4, ?, 'running', NULL, 1.0, NULL)",
            (operation_ref, target_run_ref, canonical_hash(suffix)),
        )
        connection.execute(
            "INSERT INTO ar_target_harness_admissions (target_run_ref, "
            "target_ref, harness_request_ref, harness_family, model_ref, "
            "auth_profile_ref, full_conformance_binding_json, "
            "full_conformance_binding_hash, target_scope_binding_hash, "
            "idempotency_key, request_hash, admitted_at) VALUES "
            "(?, ?, ?, 'codex', ?, ?, '{}', ?, ?, ?, ?, 1.0)",
            (
                target_run_ref,
                target_ref,
                request["request_ref"],
                request["model_ref"],
                request["auth_profile_ref"],
                full_conformance_binding_hash,
                target_scope_binding_hash,
                f"target-admission-{suffix}",
                canonical_hash({"target-admission": suffix}),
            ),
        )
        if admitted_target:
            empty_projection = canonical_json([])
            empty_projection_hash = canonical_hash([])
            connection.execute(
                "INSERT INTO ar_target_launches (launch_ref, operation_ref, "
                "target_ref, graph_ref, stage_request_ref, quest_ref, "
                "target_spec_content_hash_ref, target_spec_receipt_ref, "
                "target_spec_receipt_subject_ref, "
                "accepted_input_target_commit_refs_json, "
                "accepted_input_target_commit_refs_hash, "
                "accepted_input_asset_refs_json, accepted_input_asset_refs_hash, "
                "accepted_input_asset_proofs_json, "
                "accepted_input_asset_proofs_hash, recoverable_required, "
                "target_run_ref, status, dispatch_decision_ref, "
                "dispatch_receipt_ref, dispatch_receipt_hash, idempotency_key, "
                "request_hash, receipt_ref, receipt_hash, admitted_at) VALUES "
                "(:launch_ref, :operation_ref, :target_ref, :graph_ref, "
                ":stage_request_ref, :quest_ref, :target_spec_content_hash_ref, "
                ":target_spec_receipt_ref, :target_spec_receipt_subject_ref, "
                ":accepted_input_target_commit_refs_json, "
                ":accepted_input_target_commit_refs_hash, "
                ":accepted_input_asset_refs_json, "
                ":accepted_input_asset_refs_hash, "
                ":accepted_input_asset_proofs_json, "
                ":accepted_input_asset_proofs_hash, 1, :target_run_ref, "
                "'admitted', :dispatch_decision_ref, :dispatch_receipt_ref, "
                ":dispatch_receipt_hash, :idempotency_key, :request_hash, "
                ":receipt_ref, :receipt_hash, 1.0)",
                {
                    "launch_ref": f"target-launch-{suffix}",
                    "operation_ref": f"target-launch-operation-{suffix}",
                    "target_ref": target_ref,
                    "graph_ref": f"target-graph-{suffix}",
                    "stage_request_ref": f"stage-request-{suffix}",
                    "quest_ref": f"quest-{suffix}",
                    "target_spec_content_hash_ref": f"target-spec-{suffix}",
                    "target_spec_receipt_ref": (
                        f"target-spec-receipt-{suffix}"
                    ),
                    "target_spec_receipt_subject_ref": target_ref,
                    "accepted_input_target_commit_refs_json": empty_projection,
                    "accepted_input_target_commit_refs_hash": (
                        empty_projection_hash
                    ),
                    "accepted_input_asset_refs_json": empty_projection,
                    "accepted_input_asset_refs_hash": empty_projection_hash,
                    "accepted_input_asset_proofs_json": empty_projection,
                    "accepted_input_asset_proofs_hash": empty_projection_hash,
                    "target_run_ref": target_run_ref,
                    "dispatch_decision_ref": f"dispatch-{suffix}",
                    "dispatch_receipt_ref": f"dispatch-receipt-{suffix}",
                    "dispatch_receipt_hash": canonical_hash(
                        {"dispatch": suffix}
                    ),
                    "idempotency_key": f"target-launch-{suffix}",
                    "request_hash": canonical_hash(
                        {"target-launch": suffix}
                    ),
                    "receipt_ref": f"target-launch-receipt-{suffix}",
                    "receipt_hash": canonical_hash(
                        {"target-launch-receipt": suffix}
                    ),
                },
            )
        connection.commit()


def _authenticated_client(runtime) -> TestClient:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    client.cookies.set("meta_research_session", "test-session")
    return client


def _raw_output_harness(
    owner: SQLiteAgentRuntimeHarness,
    transport: HarnessSupervisorTransport,
    store: TargetRawOutputStore,
    workspace: Path,
) -> HarnessRuntime:
    class _RawOutputGateway:
        @staticmethod
        def required_bindings(
            _operation_ids: tuple[str, ...],
        ) -> tuple[dict[str, object], ...]:
            return ()

        @staticmethod
        def query_status() -> dict[str, object]:
            return {"catalog_hash": "a" * 64}

    adapter = CodexHarnessAdapter(
        workspace / "adapter",
        executable=sys.executable,
        runner=transport,
    )
    return HarnessRuntime(
        owner,
        _RawOutputGateway(),
        (adapter,),
        target_raw_output_store=store,
    )


def test_target_root_events_append_live_page_and_complete_exact_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-observation.sqlite3"
    upgrade_database(path)
    database = Database(path)
    feed = DurableFeed(database)
    owner = SQLiteAgentRuntimeHarness(database, feed)
    _seed_running_target_harness(path)
    retired_scope = {
        **_target_scope(),
        "attempt_ref": "target-attempt-retired",
        "attempt_generation": 1,
        "root_session_ref": "target-root-session-retired",
        "fence_ref": "target-fence-retired",
    }
    retired_event = _event(2, "retired attempt output", scope=retired_scope)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO ar_harness_provider_operations (operation_ref, run_ref, "
            "generation, invocation_hash, status, outcome_code, created_at, "
            "completed_at) VALUES ('target-provider-operation-retired', "
            "'target-run-observed', 3, ?, 'executed', NULL, 0.5, 0.75)",
            ("f" * 64,),
        )
        connection.execute(
            "INSERT INTO ar_harness_evidence_events (event_ref, operation_ref, "
            "sequence, summary_json, summary_hash, recorded_at) VALUES "
            "(?, 'target-provider-operation-retired', 2, ?, ?, 0.6)",
            (
                retired_event["event_ref"],
                canonical_json(retired_event),
                canonical_hash(retired_event),
            ),
        )
        connection.commit()
    events = (
        _event(1),
        _event(2, "epoch 1\npassword=[REDACTED]"),
        _event(3, "epoch 2"),
        _event(4, "epoch 3"),
    )
    try:
        owner.append_target_root_events(
            "target-provider-operation-observed", events
        )
        first_pointer = feed.read_event_type(
            "agent_runtime.target_root_observations_available"
        )
        assert len(first_pointer) == 1
        assert first_pointer[0].payload == {
            "target_ref": "target-observed",
            "target_run_ref": "target-run-observed",
            "stream_ref": "target-root-stream:"
            + canonical_hash(
                {
                    "target_ref": "target-observed",
                    "target_run_ref": "target-run-observed",
                    "attempt_ref": "target-attempt-observed",
                    "attempt_generation": 2,
                    "root_session_ref": "target-root-session-observed",
                    "fence_ref": "target-fence-observed",
                }
            ),
            "head_cursor": owner.query_target_root_observations(
                "target-observed", limit=1
            ).head_cursor,
        }
        assert "epoch" not in json.dumps(first_pointer[0].payload)

        first = owner.query_target_root_observations(
            "target-observed", limit=1
        )
        assert first.status == "live"
        assert [item.text for item in first.items] == [
            "epoch 1\npassword=[REDACTED]"
        ]
        assert first.has_more is True
        assert first.next_cursor == first.items[0].cursor
        second = owner.query_target_root_observations(
            "target-observed", after_cursor=first.next_cursor, limit=8
        )
        assert [item.text for item in second.items] == ["epoch 2", "epoch 3"]
        assert second.has_more is False
        assert first.head_cursor == second.head_cursor

        # Replaying the growing spool is a no-op, including its wakeup pointer.
        owner.append_target_root_events(
            "target-provider-operation-observed", events
        )
        assert feed.read_event_type(
            "agent_runtime.target_root_observations_available"
        ) == first_pointer

        owner.complete_operation(
            operation_ref="target-provider-operation-observed",
            run_ref="target-run-observed",
            native_session_ref="native-target-root",
            profile={"status": "executed"},
            evidence_events=events,
        )
        completed = owner.query_target_root_observations(
            "target-observed", limit=8
        )
        assert completed.status == "turn_complete"
        assert [item.text for item in completed.items] == [
            "epoch 1\npassword=[REDACTED]",
            "epoch 2",
            "epoch 3",
        ]

        before_conflict = feed.current_revision()
        with pytest.raises(
            AgentRuntimeHarnessError,
            match="target_root_event_conflict",
        ):
            owner.append_target_root_events(
                "target-provider-operation-observed",
                (_event(2, "conflicting replay"),),
            )
        assert feed.current_revision() == before_conflict
    finally:
        database.close()


def test_target_root_completion_evidence_is_the_last_root_message_of_an_executed_turn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-completion-evidence.sqlite3"
    upgrade_database(path)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    _seed_running_target_harness(path)
    handoff = {
        "schema_ref": "meta-research/target-completion-handoff/v1",
        "target_ref": "target-observed",
        "target_run_ref": "target-run-observed",
        "status": "completed",
        "artifacts": [
            {"role": "implementation", "relative_path": "implementation"},
            {"role": "result", "relative_path": "outputs/result.json"},
        ],
        "result_document_path": "outputs/result.json",
        "summary": "Target root completed its iterative work.",
    }
    messages = (
        _event(1),
        {
            "event_ref": "harness_evidence:"
            + canonical_hash({"completion": handoff}),
            "sequence": 2,
            "kind": "item.completed",
            "item_kind": "agent_message",
            "actor_session_ref": "native-target-root",
            "native_session_ref": "native-target-root",
            "target_run_scope": _target_scope(),
            "target_root_agent_message": True,
            "target_root_completion_candidate": handoff,
        },
        {
            "event_ref": "harness_evidence:"
            + canonical_hash({"completion-terminal": True}),
            "sequence": 3,
            "kind": "turn.completed",
            "native_session_ref": "native-target-root",
            "target_run_scope": _target_scope(),
            "target_root_terminal": True,
        },
    )
    try:
        owner.complete_operation(
            operation_ref="target-provider-operation-observed",
            run_ref="target-run-observed",
            native_session_ref="native-target-root",
            profile={"status": "executed"},
            evidence_events=messages,
        )

        evidence = owner.query_target_root_completion_evidence(
            "target-observed"
        )
        assert evidence is not None
        assert evidence.target_ref == "target-observed"
        assert evidence.target_run_ref == "target-run-observed"
        assert evidence.operation_ref == "target-provider-operation-observed"
        assert evidence.operation_generation == 4
        assert evidence.evidence_ref == messages[1]["event_ref"]
        assert evidence.native_session_ref == "native-target-root"
        assert evidence.handoff == TargetCompletionHandoff(
            schema_ref="meta-research/target-completion-handoff/v1",
            target_ref="target-observed",
            target_run_ref="target-run-observed",
            status="completed",
            artifacts=evidence.handoff.artifacts,
            result_document_path="outputs/result.json",
            summary="Target root completed its iterative work.",
        )
        assert owner.query_target_root_completion_evidence(
            "target-observed"
        ) == evidence
        handle = TargetWorkHandle(
            target_ref="target-observed",
            target_run_ref="target-run-observed",
            root_session_ref="target-root-session-observed",
            execution_attempt_ref="target-attempt-observed",
            execution_fence_ref="target-fence-observed",
            execution_input_binding_ref="input-binding-observed",
            execution_input_binding_receipt=ReceiptProof(
                receipt_ref="input-binding-receipt-observed",
                subject_ref="input-binding-observed",
                verified=True,
                currentness_known=True,
                current=True,
            ),
            accepted_input_target_commit_refs=(),
            accepted_input_asset_proofs=(),
            recoverable=True,
        )
        assert owner.verify_target_root_completion_evidence(
            handle, evidence, evidence.handoff
        ) == canonical_hash(projection_plain_value(evidence))
        with pytest.raises(
            AgentRuntimeHarnessError,
            match="target_root_completion_evidence_invalid",
        ):
            owner.verify_target_root_completion_evidence(
                replace(handle, execution_fence_ref="forged-fence"),
                evidence,
                evidence.handoff,
            )
        with pytest.raises(
            AgentRuntimeHarnessError,
            match="target_root_completion_evidence_invalid",
        ):
            owner.verify_target_root_completion_evidence(
                handle,
                replace(evidence, evidence_ref="forged-evidence"),
                evidence.handoff,
            )
    finally:
        database.close()


def test_target_root_observation_web_page_is_authenticated_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-web.sqlite3"
    upgrade_database(path)
    database = Database(path)
    owner = SQLiteAgentRuntimeHarness(database, DurableFeed(database))
    _seed_running_target_harness(path)
    owner.append_target_root_events(
        "target-provider-operation-observed",
        (
            _event(1),
            _event(2, "epoch 1\npassword=[REDACTED]"),
            _event(3, "epoch 2"),
        ),
    )
    def query_observations(target_ref: str, **values):
        try:
            return owner.query_target_root_observations(target_ref, **values)
        except AgentRuntimeHarnessError as error:
            raise HarnessAdmissionError(error.code) from error

    configurable = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None
    )
    runtime = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None,
        bundle_stage=configurable,
        reasoning_stage=configurable,
        target_run_runtime=configurable,
        harnesses=SimpleNamespace(
            query_target_root_observations=query_observations
        ),
        authentication=SimpleNamespace(
            session_is_valid=lambda token: token == "test-session",
            control_key_matches=lambda supplied, expected: supplied == expected,
        ),
    )
    client = _authenticated_client(runtime)
    try:
        response = client.get(
            "/api/v1/bundle/targets/target-observed/root-observations",
            params={"limit": 1},
        )
        assert response.status_code == 200
        page = response.json()
        assert page["target_ref"] == "target-observed"
        assert page["status"] == "live"
        assert page["observation_only"] is True
        assert page["has_more"] is True
        assert [item["text"] for item in page["items"]] == [
            "epoch 1\npassword=[REDACTED]"
        ]
        assert "visible-secret" not in response.text
        assert client.get(
            "/api/v1/bundle/targets/target-observed/root-observations",
            params={"limit": 257},
        ).status_code == 422
        invalid = client.get(
            "/api/v1/bundle/targets/target-observed/root-observations",
            params={"after": "forged-cursor"},
        )
        assert invalid.status_code == 409
        assert invalid.json()["detail"]["code"] == (
            "target_root_observation_cursor_invalid"
        )
    finally:
        client.close()
        database.close()


def test_long_target_root_output_emits_gaps_and_keeps_web_cursor_advancing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "long-target-root-observation.sqlite3"
    upgrade_database(path)
    database = Database(path)
    feed = DurableFeed(database)
    owner = SQLiteAgentRuntimeHarness(database, feed)
    _seed_running_target_harness(path)
    provider = tmp_path / "long-target-root-provider.py"
    provider.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'native-target-root'}), flush=True)\n"
        "for index in range(128):\n"
        "    output = f'{index:04d}:' + ('x' * (8192 - 5))\n"
        "    print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':f'command-{index}','exit_code':0,"
        "'output':output}}), flush=True)\n"
        "overflow = ('overflow-' * 2048) + ' password=visible-secret'\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'command-overflow','exit_code':0,"
        "'output':overflow}}), flush=True)\n"
        "for index in range(64):\n"
        "    print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':f'command-sample-{index}',"
        "'exit_code':0,'output':f'sample-progress-{index}'}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'command-late','exit_code':0,"
        "'output':'late-progress-tail'}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','thread_id':'native-target-root'}), flush=True)\n",
        encoding="utf-8",
    )
    runner = HarnessSupervisorTransport(
        tmp_path / "long-target-root-transport",
        event_sink=owner.append_target_root_events,
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "private-transport-token",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "target-provider-operation-observed"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "a" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": canonical_json(
            _target_scope()
        ),
    }
    configurable = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None
    )
    runtime = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None,
        bundle_stage=configurable,
        reasoning_stage=configurable,
        target_run_runtime=configurable,
        harnesses=SimpleNamespace(
            query_target_root_observations=(
                lambda target_ref, **values: owner.query_target_root_observations(
                    target_ref, **values
                )
            )
        ),
        authentication=SimpleNamespace(
            session_is_valid=lambda token: token == "test-session",
            control_key_matches=lambda supplied, expected: supplied == expected,
        ),
    )
    client = _authenticated_client(runtime)
    try:
        completed = runner(
            [sys.executable, str(provider)],
            "run the long target workload",
            10.0,
            environment,
        )
        assert completed.returncode == 0

        items: list[dict[str, object]] = []
        after: str | None = None
        cursors: list[str] = []
        while True:
            response = client.get(
                "/api/v1/bundle/targets/target-observed/root-observations",
                params={"limit": 256, **({"after": after} if after else {})},
            )
            assert response.status_code == 200
            page = response.json()
            items.extend(page["items"])
            if page["next_cursor"] is not None:
                cursors.append(page["next_cursor"])
            if not page["has_more"]:
                break
            assert page["next_cursor"] != after
            after = page["next_cursor"]

        gaps = [item for item in items if item["kind"] == "output_gap"]
        assert len(gaps) >= 3
        assert all(int(item["dropped_events"]) > 0 for item in gaps)
        assert any(int(item["dropped_bytes"]) > 0 for item in gaps)
        assert any(item["text"] == "sample-progress-63" for item in gaps)
        assert gaps[-1]["text"] == "late-progress-tail"
        assert len({item["cursor"] for item in gaps}) == len(gaps)
        assert len(set(cursors)) == len(cursors)
        assert "visible-secret" not in json.dumps(items)
        assert "[REDACTED]" in json.dumps(gaps)

        # The DurableFeed carries only a bounded wakeup pointer, never output.
        pointer_payloads = [
            event.payload
            for event in feed.read_event_type(
                "agent_runtime.target_root_observations_available"
            )
        ]
        assert "late-progress-tail" not in json.dumps(pointer_payloads)
        assert "visible-secret" not in json.dumps(pointer_payloads)
    finally:
        client.close()
        database.close()


def test_authenticated_target_raw_output_keeps_two_streams_isolated_across_reconnect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-raw-output-web.sqlite3"
    upgrade_database(path)
    database = Database(path)
    feed = DurableFeed(database)
    owner = SQLiteAgentRuntimeHarness(database, feed)
    outputs = {
        "observed": "alpha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "alternate": "omega-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
    }
    native_sessions = {
        "observed": "native-target-root-observed",
        "alternate": "native-target-root-alternate",
    }
    for suffix in outputs:
        _seed_running_target_harness(
            path,
            admitted_target=True,
            suffix=suffix,
        )
    transport_root = tmp_path / "target-raw-output-transport"
    store = TargetRawOutputStore(transport_root)
    transport = HarnessSupervisorTransport(
        transport_root,
        event_sink=owner.append_target_root_events,
        raw_output_store=store,
    )
    provider = tmp_path / "target-raw-output-provider.py"
    provider.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "native_session_ref, command_ref, output = sys.argv[1:]\n"
        "print(json.dumps({'type':'thread.started',"
        "'thread_id':native_session_ref}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':command_ref,'exit_code':0,"
        "'output':output}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed',"
        "'thread_id':native_session_ref}), flush=True)\n",
        encoding="utf-8",
    )
    configurable = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None
    )
    harness = _raw_output_harness(owner, transport, store, tmp_path)
    runtime = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None,
        bundle_stage=configurable,
        reasoning_stage=configurable,
        target_run_runtime=configurable,
        harnesses=harness,
        authentication=SimpleNamespace(
            session_is_valid=lambda token: token == "test-session",
            control_key_matches=lambda supplied, expected: supplied == expected,
        ),
    )

    def owner_state() -> tuple[tuple[object | None, object | None], ...]:
        return tuple(
            (
                owner.query_target_run_by_ref(f"target-run-{suffix}"),
                owner.latest_operation(f"target-run-{suffix}"),
            )
            for suffix in outputs
        )

    def read_pages(
        client: TestClient,
        suffix: str,
    ) -> tuple[str, list[dict[str, object]]]:
        pages: list[dict[str, object]] = []
        output = ""
        after = 0
        while True:
            response = client.get(
                f"/api/v1/bundle/targets/target-{suffix}/raw-output",
                params={"after": after, "limit": 32},
            )
            assert response.status_code == 200, response.text
            page = response.json()
            pages.append(page)
            assert page["offset"] == after
            assert 0 < len(page["text"].encode("utf-8")) <= 32
            assert page["next_offset"] > after
            output += page["text"]
            if not page["has_more"]:
                break
            after = page["next_offset"]
        return output, pages

    try:
        for suffix, output in outputs.items():
            completed = transport(
                [
                    sys.executable,
                    str(provider),
                    native_sessions[suffix],
                    f"command-{suffix}",
                    output,
                ],
                f"emit exact paginated Target output for {suffix}",
                10.0,
                {
                    "META_RESEARCH_MCP_TOKEN": "private-transport-token",
                    "META_RESEARCH_HARNESS_FAMILY": "codex",
                    "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
                    "META_RESEARCH_PROVIDER_OPERATION_REF": (
                        f"target-provider-operation-{suffix}"
                    ),
                    "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "a" * 64,
                    "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": canonical_json(
                        _target_scope(suffix)
                    ),
                },
            )
            assert completed.returncode == 0

        public_revision_before = feed.current_revision()
        owner_state_before = owner_state()
        assert all(
            run is not None and operation is not None
            for run, operation in owner_state_before
        )

        first_client = _authenticated_client(runtime)
        try:
            observed_output, observed_pages = read_pages(
                first_client, "observed"
            )
            alternate_output, alternate_pages = read_pages(
                first_client, "alternate"
            )
        finally:
            first_client.close()

        assert observed_output == outputs["observed"]
        assert alternate_output == outputs["alternate"]
        assert len(observed_pages) > 1
        assert len(alternate_pages) > 1
        for suffix, pages in (
            ("observed", observed_pages),
            ("alternate", alternate_pages),
        ):
            expected_identity = {
                "target_ref": f"target-{suffix}",
                "target_run_ref": f"target-run-{suffix}",
                "attempt_ref": f"target-attempt-{suffix}",
                "attempt_generation": 2,
                "root_session_ref": f"target-root-session-{suffix}",
                "native_session_ref": native_sessions[suffix],
                "root_native_session_ref": native_sessions[suffix],
                "fence_ref": f"target-fence-{suffix}",
                "operation_ref": f"target-provider-operation-{suffix}",
                "operation_generation": 4,
            }
            assert all(
                page["schema_ref"]
                == "meta-research/target-raw-output-page/v1"
                for page in pages
            )
            assert all(
                all(page[key] == value for key, value in expected_identity.items())
                for page in pages
            )
            assert all(page["exact"] is True for page in pages)
            assert all(page["unredacted"] is True for page in pages)
            assert pages[-1]["source_caught_up"] is True
        assert (
            observed_pages[0]["transport_invocation_hash"]
            != alternate_pages[0]["transport_invocation_hash"]
        )
        assert (
            observed_pages[0]["stream_ref"]
            != alternate_pages[0]["stream_ref"]
        )
        assert feed.current_revision() == public_revision_before
        assert owner_state() == owner_state_before

        reconnected_client = _authenticated_client(runtime)
        try:
            reconnected_output, reconnected_pages = read_pages(
                reconnected_client, "observed"
            )
        finally:
            reconnected_client.close()

        assert reconnected_output == outputs["observed"]
        assert reconnected_pages[0]["target_ref"] == "target-observed"
        assert reconnected_pages[0]["operation_ref"] == (
            "target-provider-operation-observed"
        )
        assert (
            reconnected_pages[0]["stream_ref"]
            == observed_pages[0]["stream_ref"]
        )
        assert feed.current_revision() == public_revision_before
        assert owner_state() == owner_state_before
    finally:
        database.close()
