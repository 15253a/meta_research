from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.provider_supervisor import (
    ensure_transport_key,
    write_transport_envelope,
)
from meta_research.stage_root_observations import (
    StageRootObservationError,
    StageRootObservationReader,
)
from meta_research.web import create_app
from test_idea_stage_recovery import _IdeaProvider, _confirm_question, _runtime as _owner_runtime


def _scope(
    run_ref: str = "plan-run-observed",
    *,
    operation_ref: str | None = None,
    status: str = "running",
    run_kind: str = "plan_stage",
    unit_kind: str = "plan_primary",
    unit_ref: str | None = None,
    operation_name: str | None = "primary",
) -> dict[str, object]:
    return {
        "run_ref": run_ref,
        "run_kind": run_kind,
        "attempt_ref": f"attempt-{run_ref}",
        "attempt_generation": 2,
        "root_session_ref": f"root-session-{run_ref}",
        "fence_ref": f"fence-{run_ref}",
        "status": status,
        "unit_ref": unit_ref or f"unit-{run_ref}",
        "operation_ref": operation_ref or f"operation-{run_ref}",
        "unit_kind": unit_kind,
        "operation_name": operation_name,
    }


def _seed_spool(
    root: Path,
    scope: dict[str, object],
    events: tuple[dict[str, object], ...],
    *,
    native_session_ref: str | None = None,
    operation_name: str = "primary",
) -> Path:
    operation_ref = str(scope["operation_ref"])
    workspace_name = {
        "idea_stage": "idea-skill-provider",
        "plan_stage": "plan-skill-provider",
        "bundle_stage": "bundle-skill-provider",
        "reasoning_stage": "reasoning-skill-provider",
    }[str(scope["run_kind"])]
    workspace = root / workspace_name
    directory = (
        workspace
        / "provider-operations"
        / canonical_hash({"job_ref": operation_ref})
        / operation_name
    )
    directory.mkdir(parents=True)
    _key_path, key = ensure_transport_key(workspace)
    write_transport_envelope(
        directory / "invocation.json",
        {
            "schema_ref": "meta-research/codex-provider-operation/v3",
            "job_ref": operation_ref,
            "operation_name": operation_name,
            "prompt_hash": canonical_hash({"prompt": operation_ref}),
            "output_schema_hash": canonical_hash({"schema": operation_ref}),
            "native_session_ref": native_session_ref,
        },
        key,
    )
    (directory / "stdout.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return directory


def _reader(
    root: Path, scopes: dict[str, dict[str, object]]
) -> StageRootObservationReader:
    return StageRootObservationReader(root, scope_lookup=scopes.get)


def _authenticated_client(runtime: object) -> TestClient:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    client.cookies.set("meta_research_session", "test-session")
    return client


def _runtime(reader: StageRootObservationReader) -> object:
    configurable = SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None
    )
    return SimpleNamespace(
        configure_resident_mcp_endpoint=lambda _base_url: None,
        bundle_stage=configurable,
        reasoning_stage=configurable,
        target_run_runtime=configurable,
        query_stage_root_observations=reader.query,
        query_stage_raw_output=lambda run_ref, **values: reader.query_raw(
            run_ref, **values
        ),
        authentication=SimpleNamespace(
            session_is_valid=lambda token: token == "test-session",
            control_key_matches=lambda supplied, expected: supplied == expected,
        ),
    )


def test_stage_root_reader_pages_only_redacted_root_command_output(
    tmp_path: Path,
) -> None:
    scope = _scope()
    other_scope = _scope("plan-run-other")
    root_events = (
        {"type": "thread.started", "thread_id": "native-root"},
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "root-command",
                "sender_thread_id": "native-root",
                "output": "safe progress\npassword=visible-secret",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "root-command",
                "sender_thread_id": "native-root",
                "output": "safe progress\npassword=visible-secret",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "private agent conclusion must not be public",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "child-command",
                "sender_thread_id": "native-child",
                "output": "child-only-private-output",
            },
        },
        {"type": "turn.completed", "thread_id": "native-root"},
    )
    _seed_spool(tmp_path, scope, root_events)
    _seed_spool(
        tmp_path,
        other_scope,
        (
            {"type": "thread.started", "thread_id": "native-other"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "other-command",
                    "sender_thread_id": "native-other",
                    "output": "other safe output",
                },
            },
        ),
    )
    reader = _reader(
        tmp_path,
        {
            str(scope["run_ref"]): scope,
            str(other_scope["run_ref"]): other_scope,
        },
    )

    first = reader.query(str(scope["run_ref"]), limit=1)
    assert first.observation_only is True
    assert first.status == "live"
    assert first.availability == "ready"
    assert [item.text for item in first.items] == ["safe progress\n"]
    assert first.has_more is True
    assert first.next_cursor == first.items[0].cursor

    second = reader.query(
        str(scope["run_ref"]), after_cursor=first.next_cursor, limit=8
    )
    assert [item.text for item in second.items] == ["password=[REDACTED]"]
    assert second.has_more is False
    encoded = json.dumps(second.as_dict())
    assert "visible-secret" not in encoded
    assert "child-only-private-output" not in encoded
    assert "private agent conclusion" not in encoded

    with pytest.raises(
        StageRootObservationError,
        match="stage_root_observation_cursor_invalid",
    ):
        reader.query(
            str(other_scope["run_ref"]), after_cursor=first.next_cursor
        )


def test_stage_root_reader_withholds_unscoped_command_output(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-unscoped-command")
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "native-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "unknown-command",
                    # A child could omit its actor in the native protocol.
                    "output": "unscoped-child-private-output",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "verified-root-command",
                    "sender_thread_id": "native-root",
                    "output": "verified root output\n",
                },
            },
        ),
    )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.availability == "limited"
    assert page.source_limited is True
    assert [item.text for item in page.items] == ["verified root output\n"]
    assert "unscoped-child-private-output" not in json.dumps(page.as_dict())


def test_stage_root_reader_rejects_a_foreign_session_against_sealed_resume_root(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-resumed")
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "foreign-thread"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "foreign-command",
                    "sender_thread_id": "foreign-thread",
                    "output": "foreign-private-output",
                },
            },
            {"type": "thread.started", "thread_id": "sealed-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "root-command",
                    "sender_thread_id": "sealed-root",
                    "output": "safe root output",
                },
            },
        ),
        native_session_ref="sealed-root",
    )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    with pytest.raises(
        StageRootObservationError,
        match="stage_root_observation_spool_unavailable",
    ):
        reader.query(str(scope["run_ref"]))


def test_stage_root_reader_reports_waiting_before_its_operation_spool_exists(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-waiting")
    workspace = tmp_path / "plan-skill-provider"
    (workspace / "provider-operations").mkdir(parents=True)
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.availability == "waiting"
    assert page.items == ()


def test_stage_root_reader_marks_a_malformed_complete_spool_limited(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-malformed")
    directory = _seed_spool(
        tmp_path,
        scope,
        ({"type": "thread.started", "thread_id": "native-root"},),
    )
    (directory / "stdout.jsonl").write_text("not-json\n", encoding="utf-8")
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.availability == "limited"
    assert page.source_limited is True
    assert page.items == ()


def test_stage_root_reader_withholds_a_stream_without_root_identity(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-missing-root-identity")
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "claimed-root-command",
                    "sender_thread_id": "claimed-root",
                    "output": "must-not-project\n",
                },
            },
        ),
    )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.availability == "limited"
    assert page.source_limited is True
    assert page.items == ()


def test_stage_root_reader_binds_a_rolling_bundle_unit_without_phase_limit(
    tmp_path: Path,
) -> None:
    operation_ref = "bundle-review-operation"
    current_phase = "dispatch-17"
    current_unit_ref = "provider_unit_" + canonical_hash(
        {
            "operation_ref": operation_ref,
            "operation_name": current_phase,
            "attempt_ref": "attempt-bundle-run-observed",
        }
    )[:64]
    scope = _scope(
        "bundle-run-observed",
        operation_ref=operation_ref,
        run_kind="bundle_stage",
        unit_kind="bundle_review",
        unit_ref=current_unit_ref,
        operation_name=None,
    )
    for generation in range(1, 18):
        phase = f"dispatch-{generation}"
        _seed_spool(
            tmp_path,
            scope,
            (
                {"type": "thread.started", "thread_id": f"root-{generation}"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "id": f"command-{generation}",
                        "sender_thread_id": f"root-{generation}",
                        "output": f"{phase} output\n",
                    },
                },
            ),
            operation_name=phase,
        )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.phase == current_phase
    assert [item.text for item in page.items] == ["dispatch-17 output\n"]


def test_stage_root_reader_uses_the_reasoning_autonomous_resume_phase_hint(
    tmp_path: Path,
) -> None:
    scope = _scope(
        "reasoning-run-observed",
        run_kind="reasoning_stage",
        unit_kind="reasoning_review",
        operation_name="autonomous-resume",
    )
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "review-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "review-command",
                    "sender_thread_id": "review-root",
                    "output": "review output\n",
                },
            },
        ),
        operation_name="review",
    )
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "resume-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "resume-command",
                    "sender_thread_id": "resume-root",
                    "output": "autonomous resume output\n",
                },
            },
        ),
        operation_name="autonomous-resume",
    )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})

    page = reader.query(str(scope["run_ref"]))

    assert page.phase == "autonomous-resume"
    assert [item.text for item in page.items] == ["autonomous resume output\n"]


def test_stage_observation_scope_uses_the_current_persisted_provider_unit(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "owner-scope")
    runtime = _owner_runtime(data_root, _IdeaProvider())
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("stage-observation-scope-start")
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert run is not None
        runtime.owners.agent_runtime.begin_provider_unit(
            unit_ref=run.primary_invocation.invocation_ref,
            operation_ref=run.primary_invocation.operation_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            unit_kind="idea_primary",
        )

        scope = runtime.owners.agent_runtime.query_stage_root_observation_scope(
            run.run_ref
        )

        assert scope == {
            "run_ref": run.run_ref,
            "run_kind": "idea_stage",
            "attempt_ref": run.attempt_ref,
            "attempt_generation": run.attempt_generation,
            "root_session_ref": run.root_session_ref,
            "fence_ref": run.fence_ref,
            "status": "running",
            "unit_ref": run.primary_invocation.invocation_ref,
            "operation_ref": run.primary_invocation.operation_ref,
            "unit_kind": "idea_primary",
            "operation_name": "primary",
        }
        _seed_spool(
            data_root.root,
            scope,
            (
                {"type": "thread.started", "thread_id": "native-root"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "id": "owner-bound-command",
                        "sender_thread_id": "native-root",
                        "output": "owner-bound safe output\n",
                    },
                },
            ),
        )

        page = runtime.query_stage_root_observations(run.run_ref)

        assert page.run_ref == run.run_ref
        assert page.attempt_generation == run.attempt_generation
        assert [item.text for item in page.items] == ["owner-bound safe output\n"]
    finally:
        runtime.close()


def test_stage_root_observation_route_is_authenticated_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    scope = _scope()
    _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "native-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "root-command",
                    "sender_thread_id": "native-root",
                    "output": "route progress\npassword=visible-secret",
                },
            },
        ),
    )
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})
    client = _authenticated_client(_runtime(reader))
    try:
        response = client.get(
            "/api/v1/stage-runs/plan-run-observed/root-observations",
            params={"limit": 1},
        )
        assert response.status_code == 200
        page = response.json()
        assert page["run_ref"] == "plan-run-observed"
        assert page["observation_only"] is True
        assert page["has_more"] is False
        assert page["items"][0]["text"] == "route progress\npassword=[REDACTED]"
        assert "visible-secret" not in response.text

        assert client.get(
            "/api/v1/stage-runs/plan-run-observed/root-observations",
            params={"limit": 257},
        ).status_code == 422
        invalid = client.get(
            "/api/v1/stage-runs/plan-run-observed/root-observations",
            params={"after": "forged-cursor"},
        )
        assert invalid.status_code == 409
        assert invalid.json()["detail"]["code"] == (
            "stage_root_observation_cursor_invalid"
        )

        client.cookies.clear()
        assert client.get(
            "/api/v1/stage-runs/plan-run-observed/root-observations"
        ).status_code == 401
    finally:
        client.close()


def test_stage_raw_output_route_pages_exact_private_provider_stdout(
    tmp_path: Path,
) -> None:
    scope = _scope("plan-run-raw")
    directory = _seed_spool(
        tmp_path,
        scope,
        (
            {"type": "thread.started", "thread_id": "native-root"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "id": "message-1",
                    "text": "正在逐步核对原始证据。",
                },
            },
            {
                "type": "item.started",
                "item": {
                    "type": "mcp_tool_call",
                    "id": "tool-1",
                    "server": "semantic",
                    "tool": "evidence.read",
                    "status": "in_progress",
                    "arguments": {"private": "visible-in-private-stream"},
                },
            },
            {"type": "turn.completed"},
        ),
        native_session_ref="native-root",
    )
    expected = (directory / "stdout.jsonl").read_text(encoding="utf-8")
    reader = _reader(tmp_path, {str(scope["run_ref"]): scope})
    client = _authenticated_client(_runtime(reader))
    try:
        pages: list[dict[str, object]] = []
        chunks: list[str] = []
        after = 0
        while True:
            response = client.get(
                "/api/v1/stage-runs/plan-run-raw/raw-output",
                params={"after": after, "limit": 31},
            )
            assert response.status_code == 200, response.text
            page = response.json()
            pages.append(page)
            chunks.append(page["text"])
            assert page["schema_ref"] == "meta-research/stage-raw-output-page/v1"
            assert page["run_ref"] == "plan-run-raw"
            assert page["phase"] == "primary"
            assert page["native_session_ref"] == "native-root"
            assert page["offset"] == after
            assert page["next_offset"] > after
            assert page["exact"] is True
            assert page["unredacted"] is True
            if not page["has_more"]:
                break
            after = page["next_offset"]

        rendered = "".join(chunks)
        assert rendered == expected
        assert "agent_message" in rendered
        assert "visible-in-private-stream" in rendered
        assert pages[-1]["source_caught_up"] is True

        client.cookies.clear()
        assert client.get(
            "/api/v1/stage-runs/plan-run-raw/raw-output"
        ).status_code == 401
    finally:
        client.close()
