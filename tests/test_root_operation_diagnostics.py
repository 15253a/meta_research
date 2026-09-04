from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.paths import prepare_data_root
from meta_research.root_capabilities import (
    codex_feature_diagnostics,
    project_codex_post_turn_diagnostics,
    root_capability_profile,
    validate_root_capability_diagnostics,
)
from meta_research.root_operation_diagnostics import (
    RootOperationDiagnosticError,
    RootOperationDiagnosticStore,
    root_operation_diagnostic_ref,
)
from meta_research.web import create_app


_FEATURE_OUTPUT = """\
hooks stable true
multi_agent stable true
plugins stable true
remote_plugin stable true
shell_tool stable true
skill_search stable true
unified_exec stable true
"""


def _pre_turn(
    root_kind: str = "bundle",
    *,
    authorized_operation_ids: tuple[str, ...] = (
        "agent_runtime.bundle_inbox.read",
    ),
) -> dict[str, object]:
    return codex_feature_diagnostics(
        profile=root_capability_profile(root_kind),
        entry_path="initial",
        provider_version="0.153.2",
        feature_output=_FEATURE_OUTPUT,
        authorized_operation_ids=authorized_operation_ids,
    )


def _completed_stdout() -> str:
    return "\n".join(
        json.dumps(event, sort_keys=True)
        for event in (
            {"type": "thread.started", "thread_id": "root-session"},
            {
                "type": "item.completed",
                "item": {
                    "id": "command-1",
                    "type": "command_execution",
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "child-1",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "mcp-1",
                    "type": "mcp_tool_call",
                    "server": "meta_research",
                    "status": "completed",
                },
            },
            {"type": "turn.completed", "thread_id": "root-session"},
        )
    )


def test_post_turn_projection_separates_availability_usage_and_authorization() -> None:
    projected = project_codex_post_turn_diagnostics(
        _pre_turn(),
        _completed_stdout(),
        semantic_mcp_available=True,
    )

    assert projected["availability"]["plugin"] == {"status": "available"}
    assert projected["usage"]["plugin"] == {
        "status": "not_used",
        "evidence_refs": [],
    }
    for capability in ("shell", "subagent", "semantic_mcp", "stream"):
        assert projected["availability"][capability] == {"status": "available"}
        assert projected["usage"][capability]["status"] == "used"
        assert projected["usage"][capability]["evidence_refs"]
    assert projected["side_effect_authorization"] == {
        "status": "operation_local",
        "operation_ids": ["agent_runtime.bundle_inbox.read"],
    }
    assert projected["tool_inventory"] == {
        "status": "not_reported",
        "evidence_refs": [],
        "names": [],
    }
    assert projected["provider_feature_inventory"]["status"] == "observed"
    assert validate_root_capability_diagnostics(projected) == projected


def test_signed_diagnostic_store_is_idempotent_and_publicly_queryable(
    tmp_path: Path,
) -> None:
    store = RootOperationDiagnosticStore(tmp_path / "diagnostics")
    operation_ref = root_operation_diagnostic_ref(
        "bundle",
        source_ref="bundle-run:1",
        phase="primary",
    )
    diagnostics = project_codex_post_turn_diagnostics(
        _pre_turn(),
        _completed_stdout(),
    )

    first = store.record(
        operation_ref=operation_ref,
        root_kind="bundle",
        diagnostics=diagnostics,
    )
    replayed = store.record(
        operation_ref=operation_ref,
        root_kind="bundle",
        diagnostics=diagnostics,
    )

    assert replayed == first
    assert store.query(operation_ref) == first
    assert RootOperationDiagnosticStore(tmp_path / "diagnostics").query(
        operation_ref
    ) == first
    page = store.query_page(root_kind="bundle")
    assert page["status"] == "observed"
    assert page["page_counts"]["bundle"] == 1
    assert page["page_counts"]["target"] == 0
    assert page["items"] == [first.as_public_dict()]

    conflicting = root_capability_profile("bundle").public_diagnostics()
    with pytest.raises(
        RootOperationDiagnosticError,
        match="root_operation_diagnostic_record_unavailable",
    ):
        store.record(
            operation_ref=operation_ref,
            root_kind="bundle",
            diagnostics=conflicting,
        )


def test_diagnostic_store_initialization_and_write_failure_do_not_share_a_gate(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")

    store = RootOperationDiagnosticStore(blocked_parent / "diagnostics")

    with pytest.raises(
        RootOperationDiagnosticError,
        match="root_operation_diagnostic_record_unavailable",
    ):
        store.record(
            operation_ref=root_operation_diagnostic_ref(
                "idea",
                source_ref="idea-run:blocked-diagnostic-disk",
                phase="primary",
            ),
            root_kind="idea",
            diagnostics=_pre_turn("idea"),
        )


class _RecordingRunner:
    def __init__(self, *, feature_probe_fails: bool = False) -> None:
        self.calls = 0
        self.feature_probe_fails = feature_probe_fails

    def run_command(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        assert argv[-2:] == ["features", "list"]
        assert timeout_seconds == 2.0
        if self.feature_probe_fails:
            raise RuntimeError("feature probe unavailable")
        return subprocess.CompletedProcess(argv, 0, _FEATURE_OUTPUT, "")

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout_seconds: float | None,
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout_seconds
        self.calls += 1
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text('{"accepted":true}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _completed_stdout(), "")


class _RecordingDiagnostics:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, object]] = []

    def record(self, **values: object) -> object:
        if self.fail:
            raise RuntimeError("diagnostic sink unavailable")
        self.records.append(values)
        return values


@pytest.mark.parametrize("sink_fails", [False, True])
def test_real_idea_adapter_projects_post_turn_diagnostics_without_gating(
    tmp_path: Path,
    sink_fails: bool,
) -> None:
    runner = _RecordingRunner()
    sink = _RecordingDiagnostics(fail=sink_fails)
    adapter = CodexIdeaSkillAdapter(tmp_path / "idea", process_runner=runner)
    adapter.bind_root_operation_diagnostics_recorder(sink)  # type: ignore[arg-type]

    result, native_session_ref, _stdout = adapter._invoke(
        operation_name="diagnostic-turn",
        prompt="Return the accepted result.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"accepted": {"type": "boolean"}},
            "required": ["accepted"],
        },
        native_session_ref=None,
        job_ref=None,
        authorized_operation_ids=("research_graph.snapshot.read",),
    )

    assert result == {"accepted": True}
    assert native_session_ref == "root-session"
    assert runner.calls == 1
    if sink_fails:
        assert sink.records == []
        return
    assert len(sink.records) == 1
    recorded = sink.records[0]
    assert recorded["root_kind"] == "idea"
    diagnostics = recorded["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["usage"]["shell"]["status"] == "used"
    assert diagnostics["usage"]["subagent"]["status"] == "used"
    assert diagnostics["side_effect_authorization"]["operation_ids"] == [
        "research_graph.snapshot.read"
    ]


def test_feature_probe_failure_is_not_a_provider_admission_gate(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner(feature_probe_fails=True)
    sink = _RecordingDiagnostics()
    adapter = CodexIdeaSkillAdapter(tmp_path / "idea", process_runner=runner)
    adapter.bind_root_operation_diagnostics_recorder(sink)  # type: ignore[arg-type]

    result, _native_session_ref, _stdout = adapter._invoke(
        operation_name="probe-failure-turn",
        prompt="Return the accepted result.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"accepted": {"type": "boolean"}},
            "required": ["accepted"],
        },
        native_session_ref=None,
        job_ref=None,
    )

    assert result == {"accepted": True}
    assert runner.calls == 1
    diagnostics = sink.records[0]["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["provider_feature_inventory"]["status"] == "not_reported"
    assert diagnostics["usage"]["shell"]["status"] == "used"


def test_authenticated_public_api_exposes_the_same_signed_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_TRUST_SSH_LOOPBACK", "1")
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "public-diagnostics"),
        startup_harness_diagnostics=False,
    )
    try:
        diagnostics = project_codex_post_turn_diagnostics(
            _pre_turn(),
            _completed_stdout(),
        )
        record = runtime.root_operation_diagnostics.record(
            operation_ref=root_operation_diagnostic_ref(
                "bundle",
                source_ref="public-bundle-run",
                phase="primary",
            ),
            root_kind="bundle",
            diagnostics=diagnostics,
        )
        app = create_app(
            runtime,
            base_url="http://127.0.0.1:8766",
            control_key="control-key",
        )
        with TestClient(app, base_url="http://127.0.0.1:8766") as client:
            response = client.get(
                "/api/v1/root-capability-diagnostics",
                params={
                    "root_kind": "bundle",
                    "operation_ref": record.operation_ref,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "observed"
        assert payload["items"] == [record.as_public_dict()]
        public_diagnostics = payload["items"][0]["diagnostics"]
        assert public_diagnostics["availability"]["plugin"]["status"] == (
            "available"
        )
        assert public_diagnostics["usage"]["plugin"]["status"] == "not_used"
        assert public_diagnostics["side_effect_authorization"]["operation_ids"] == [
            "agent_runtime.bundle_inbox.read"
        ]
    finally:
        runtime.close()
