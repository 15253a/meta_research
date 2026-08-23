from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.harness import HarnessAdmissionError, HarnessProbeRequest
from meta_research.harness_adapters import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    HarnessAdapterUnavailable,
    HarnessInvocation,
    HarnessSupervisorTransport,
)
from meta_research.paths import prepare_data_root


class _RecordedRunner:
    def __init__(self, family: str, stream: tuple[dict[str, object], ...]) -> None:
        self.family = family
        self.stream = stream
        self.calls: list[tuple[list[str], str, dict[str, str]]] = []

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        self.calls.append((list(argv), prompt, dict(environment)))
        if "--version" in argv:
            version = "codex-cli 0.147.0\n" if self.family == "codex" else "2.1.220\n"
            return subprocess.CompletedProcess(argv, 0, version, "")
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(json.dumps(item) for item in self.stream) + "\n",
            "",
        )


def _invocation(family: str) -> HarnessInvocation:
    return HarnessInvocation(
        harness_family=family,
        provider_operation_ref="provider-operation:test",
        run_ref="harness_run:1",
        attempt_ref="harness_attempt:1",
        attempt_generation=1,
        root_session_ref="harness_session:1",
        fence_ref="harness_fence:1",
        model_ref="model-test",
        prompt="Exercise only the bound conformance operations.",
        mcp_url="http://127.0.0.1:8765/mcp",
        mcp_token="opaque-channel-token",
    )


def _assert_profile_is_event_derived(profile: dict[str, object]) -> None:
    capabilities = profile["capabilities"]
    assert isinstance(capabilities, dict)
    for name in (
        "tool_inventory",
        "shell",
        "file_access",
        "semantic_mcp",
        "skill",
        "plugin",
        "hook",
        "subagent",
        "stream",
        "native_session",
        "fork",
        "steer",
        "interrupt",
        "resume",
        "web_search",
        "web_fetch",
    ):
        assert capabilities[name]["status"] == "available"
        assert capabilities[name]["evidence_refs"]
    assert "opaque-channel-token" not in json.dumps(profile)


def test_codex_adapter_derives_native_identity_and_capabilities_from_jsonl(
    tmp_path: Path,
) -> None:
    runner = _RecordedRunner(
        "codex",
        (
            {"type": "thread.started", "thread_id": "codex-thread-1", "tools": ["shell", "mcp"]},
            {"type": "item.completed", "item": {"type": "command_execution"}},
            {"type": "item.completed", "item": {"type": "file_change"}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "meta_research"}},
            {"type": "item.completed", "item": {"type": "web_search", "query": "MCP spec", "action": {"type": "search"}}},
            {"type": "item.completed", "item": {"type": "web_search", "query": "https://modelcontextprotocol.io/specification/2025-06-18", "action": {"type": "open_page"}}},
            {"type": "item.completed", "item": {"type": "skill", "name": "probe"}},
            {"type": "item.completed", "item": {"type": "plugin", "name": "probe"}},
            {"type": "item.completed", "item": {"type": "hook", "name": "probe"}},
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "codex-thread-1",
                    "receiver_thread_ids": ["codex-child-1"],
                    "agents_states": {
                        "codex-child-1": {"status": "pending"}
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "status": "completed",
                    "sender_thread_id": "codex-thread-1",
                    "receiver_thread_ids": ["codex-child-1"],
                    "agents_states": {
                        "codex-child-1": {"status": "completed"}
                    },
                },
            },
            {"type": "thread.forked", "thread_id": "codex-thread-1"},
            {"type": "turn.steered", "thread_id": "codex-thread-1"},
            {"type": "turn.interrupted", "thread_id": "codex-thread-1"},
            {"type": "thread.resumed", "thread_id": "codex-thread-1"},
            {"type": "turn.completed"},
        ),
    )
    result = CodexHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("codex")
    )

    assert result.native_session_ref == "codex-thread-1"
    assert result.profile["provider_version"] == "0.147.0"
    _assert_profile_is_event_derived(result.profile)
    argv, _prompt, environment = runner.calls[-1]
    assert argv[:2] == ["codex", "exec"]
    assert "opaque-channel-token" not in " ".join(argv)
    assert environment["META_RESEARCH_MCP_TOKEN"] == "opaque-channel-token"


def test_claude_adapter_derives_capabilities_without_model_self_report(
    tmp_path: Path,
) -> None:
    runner = _RecordedRunner(
        "claude",
        (
            {
                "type": "system",
                "subtype": "init",
                "session_id": "claude-session-1",
                "tools": ["Bash", "Read", "mcp__meta_research__snapshot", "Agent", "WebSearch", "WebFetch"],
                "skills": ["probe"],
                "plugins": ["probe"],
                "hooks": ["pre_tool"],
            },
            {"type": "assistant", "session_id": "claude-session-1", "message": {"content": [
                {"type": "tool_use", "id": "tool-1", "name": "Bash"},
                {"type": "tool_use", "id": "tool-2", "name": "Read"},
                {"type": "tool_use", "id": "tool-3", "name": "mcp__meta_research__snapshot"},
                {"type": "tool_use", "id": "tool-4", "name": "Agent"},
                {"type": "tool_use", "id": "tool-5", "name": "WebSearch"},
                {"type": "tool_use", "id": "tool-6", "name": "WebFetch"},
                {"type": "tool_use", "id": "tool-7", "name": "Skill"},
                {"type": "tool_use", "id": "tool-8", "name": "plugin__probe__run"},
            ]}},
            {
                "type": "user",
                "session_id": "claude-session-1",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"tool-{index}",
                            "is_error": False,
                            **(
                                {
                                    "agent_id": "claude-child-1",
                                    "parent_session_id": "claude-session-1",
                                    "status": "completed",
                                }
                                if index == 4
                                else {}
                            ),
                        }
                        for index in range(1, 9)
                    ]
                },
            },
            {"type": "system", "subtype": "hook_completed", "session_id": "claude-session-1", "status": "completed"},
            {"type": "system", "subtype": "fork", "session_id": "claude-session-1"},
            {"type": "system", "subtype": "steer", "session_id": "claude-session-1"},
            {"type": "system", "subtype": "interrupt", "session_id": "claude-session-1"},
            {"type": "system", "subtype": "resume", "session_id": "claude-session-1"},
            {"type": "result", "session_id": "claude-session-1", "is_error": False},
        ),
    )
    result = ClaudeHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("claude")
    )

    assert result.native_session_ref == "claude-session-1"
    assert result.profile["provider_version"] == "2.1.220"
    _assert_profile_is_event_derived(result.profile)
    argv, _prompt, environment = runner.calls[-1]
    assert argv[:2] == ["claude", "-p"]
    assert "opaque-channel-token" not in " ".join(argv)
    assert environment["META_RESEARCH_MCP_TOKEN"] == "opaque-channel-token"
    config_path = Path(argv[argv.index("--mcp-config") + 1])
    assert runner.calls[-1][1]
    assert "opaque-channel-token" not in config_path.read_text(encoding="utf-8")


def test_missing_web_events_are_typed_unavailable_not_inferred_from_text(
    tmp_path: Path,
) -> None:
    runner = _RecordedRunner(
        "codex",
        (
            {"type": "thread.started", "thread_id": "codex-thread-no-web"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "I used Web Search and Web Fetch successfully.",
                },
            },
            {"type": "turn.completed"},
        ),
    )
    result = CodexHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("codex")
    )

    capabilities = result.profile["capabilities"]
    assert capabilities["web_search"] == {
        "status": "capability_unavailable",
        "reason": {"code": "probe_evidence_missing"},
        "evidence_refs": [],
    }
    assert capabilities["web_fetch"]["status"] == "capability_unavailable"


def test_claude_tool_request_without_successful_result_is_not_capability_proof(
    tmp_path: Path,
) -> None:
    runner = _RecordedRunner(
        "claude",
        (
            {
                "type": "system",
                "subtype": "init",
                "session_id": "claude-incomplete-tool",
                "tools": ["Bash", "WebSearch"],
                "skills": ["discovered-only"],
                "plugins": ["discovered-only"],
                "hooks": ["discovered-only"],
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-not-finished",
                            "name": "Bash",
                        },
                        {
                            "type": "tool_use",
                            "id": "tool-failed",
                            "name": "WebSearch",
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-failed",
                            "is_error": True,
                        }
                    ]
                },
            },
            {
                "type": "result",
                "session_id": "claude-incomplete-tool",
                "is_error": False,
            },
        ),
    )

    result = ClaudeHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("claude")
    )

    capabilities = result.profile["capabilities"]
    assert capabilities["tool_inventory"]["status"] == "available"
    assert capabilities["shell"]["status"] == "capability_unavailable"
    assert capabilities["web_search"]["status"] == "capability_unavailable"
    assert capabilities["skill"]["status"] == "capability_unavailable"
    assert capabilities["plugin"]["status"] == "capability_unavailable"
    assert capabilities["hook"]["status"] == "capability_unavailable"


def test_subagent_requires_one_root_spawn_and_terminal_child_provenance(
    tmp_path: Path,
) -> None:
    runner = _RecordedRunner(
        "codex",
        (
            {
                "type": "thread.started",
                "thread_id": "codex-root-with-stranded-child",
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "codex-root-with-stranded-child",
                    "receiver_thread_ids": ["codex-child-stranded"],
                    "agents_states": {
                        "codex-child-stranded": {"status": "pending"}
                    },
                },
            },
            {"type": "turn.completed"},
        ),
    )

    result = CodexHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("codex")
    )

    assert result.profile["capabilities"]["subagent"] == {
        "status": "capability_unavailable",
        "reason": {"code": "probe_evidence_missing"},
        "evidence_refs": [],
    }


@pytest.mark.parametrize(
    "events",
    (
        (
            {
                "type": "thread.started",
                "thread_id": "codex-root-invalid-provenance",
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "status": "completed",
                    "sender_thread_id": "codex-root-invalid-provenance",
                    "receiver_thread_ids": ["codex-child-invalid"],
                    "agents_states": {
                        "codex-child-invalid": {"status": "completed"}
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "codex-root-invalid-provenance",
                    "receiver_thread_ids": ["codex-child-invalid"],
                    "agents_states": {
                        "codex-child-invalid": {"status": "pending"}
                    },
                },
            },
            {"type": "turn.completed"},
        ),
        (
            {
                "type": "thread.started",
                "thread_id": "codex-root-invalid-provenance",
            },
            {
                "type": "item.failed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "failed",
                    "sender_thread_id": "codex-root-invalid-provenance",
                    "receiver_thread_ids": [],
                    "agents_states": {},
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": "codex-root-invalid-provenance",
                    "receiver_thread_ids": ["codex-child-invalid"],
                    "agents_states": {
                        "codex-child-invalid": {"status": "pending"}
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "status": "completed",
                    "sender_thread_id": "codex-root-invalid-provenance",
                    "receiver_thread_ids": ["codex-child-invalid"],
                    "agents_states": {
                        "codex-child-invalid": {"status": "completed"}
                    },
                },
            },
            {"type": "turn.completed"},
        ),
    ),
    ids=("terminal-wait-before-spawn", "failed-and-successful-spawn"),
)
def test_subagent_rejects_noncausal_or_multiple_spawn_provenance(
    tmp_path: Path,
    events: tuple[dict[str, object], ...],
) -> None:
    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    assert result.profile["capabilities"]["subagent"]["status"] == (
        "capability_unavailable"
    )


def test_claude_subagent_result_must_follow_its_root_request(
    tmp_path: Path,
) -> None:
    root_ref = "claude-root-invalid-provenance"
    result = ClaudeHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "claude",
            (
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": root_ref,
                    "tools": ["Agent"],
                },
                {
                    "type": "user",
                    "session_id": root_ref,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "agent-tool-1",
                                "is_error": False,
                                "agent_id": "claude-child-invalid",
                                "parent_session_id": root_ref,
                                "status": "completed",
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "session_id": root_ref,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "agent-tool-1",
                                "name": "Agent",
                            }
                        ]
                    },
                },
                {"type": "result", "session_id": root_ref, "is_error": False},
            ),
        ),
    ).invoke(_invocation("claude"))

    assert result.profile["capabilities"]["subagent"]["status"] == (
        "capability_unavailable"
    )


def test_adapter_fails_closed_on_locked_version_drift(tmp_path: Path) -> None:
    class DriftRunner(_RecordedRunner):
        def __call__(self, argv, prompt, timeout, environment):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "codex-cli 9.9.9\n", "")
            return super().__call__(argv, prompt, timeout, environment)

    with pytest.raises(HarnessAdapterUnavailable, match="provider_version_drift"):
        CodexHarnessAdapter(tmp_path, runner=DriftRunner("codex", ())).invoke(
            _invocation("codex")
        )


def test_claude_structured_401_is_distinct_from_provider_unavailable(
    tmp_path: Path,
) -> None:
    class RevokedRunner(_RecordedRunner):
        def __call__(self, argv, prompt, timeout, environment):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "2.1.220\n", "")
            return subprocess.CompletedProcess(
                argv,
                1,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "init",
                                "session_id": "claude-revoked",
                                "tools": [],
                            }
                        ),
                        json.dumps(
                            {
                                "type": "system",
                                "subtype": "api_retry",
                                "error_status": 401,
                                "error": "authentication_failed",
                                "session_id": "claude-revoked",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "result",
                                "is_error": True,
                                "api_error_status": 401,
                                "session_id": "claude-revoked",
                            }
                        ),
                    )
                ),
                "",
            )

    with pytest.raises(HarnessAdapterUnavailable, match="provider_auth_revoked"):
        ClaudeHarnessAdapter(tmp_path, runner=RevokedRunner("claude", ())).invoke(
            _invocation("claude")
        )


def test_doctor_exposes_the_latest_typed_provider_failure(tmp_path: Path) -> None:
    class RevokedRunner(_RecordedRunner):
        def __call__(self, argv, prompt, timeout, environment):
            if "--version" in argv:
                return subprocess.CompletedProcess(argv, 0, "2.1.220\n", "")
            return subprocess.CompletedProcess(
                argv,
                1,
                json.dumps(
                    {
                        "type": "result",
                        "is_error": True,
                        "api_error_status": 401,
                        "session_id": "claude-revoked-doctor",
                    }
                ),
                "",
            )

    data_root = prepare_data_root(tmp_path / "doctor-provider-failure")
    runtime = build_production_runtime(
        data_root,
        harness_adapters=(
            CodexHarnessAdapter(
                data_root.run / "harness",
                runner=_RecordedRunner("codex", ()),
            ),
            ClaudeHarnessAdapter(
                data_root.run / "harness",
                runner=RevokedRunner("claude", ()),
            ),
        ),
    )
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="doctor-claude-revoked",
                harness_family="claude",
                model_ref="sonnet",
                auth_profile_ref="harness-profile:claude-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("native_session",),
            ),
            idempotency_key="doctor-claude-revoked",
        )
        with pytest.raises(HarnessAdmissionError, match="provider_auth_revoked"):
            runtime.harnesses.execute_probe(
                admission.run.request_ref,
                prompt="Bounded probe.",
                mcp_base_url="http://127.0.0.1:8765",
            )

        claude = runtime.harnesses.query_status()["adapters"][1]
        assert claude["capability_profile"] is None
        assert claude["missing_reason"] == {"code": "provider_auth_revoked"}
    finally:
        runtime.close()


def test_doctor_does_not_reuse_a_ready_profile_after_resume_auth_revocation(
    tmp_path: Path,
) -> None:
    class RevokeOnResume(_RecordedRunner):
        def __init__(self) -> None:
            super().__init__("codex", ())
            self.turns = 0

        def __call__(self, argv, prompt, timeout, environment):
            if "--version" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, "codex-cli 0.147.0\n", ""
                )
            self.turns += 1
            if self.turns == 1:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "\n".join(
                        (
                            json.dumps(
                                {
                                    "type": "thread.started",
                                    "thread_id": "codex-ready-then-revoked",
                                }
                            ),
                            json.dumps({"type": "turn.completed"}),
                        )
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 1, "", "401 unauthorized")

    data_root = prepare_data_root(tmp_path / "doctor-resume-revoked")
    runner = RevokeOnResume()
    runtime = build_production_runtime(
        data_root,
        harness_adapters=(
            CodexHarnessAdapter(data_root.run / "harness", runner=runner),
            ClaudeHarnessAdapter(
                data_root.run / "harness",
                runner=_RecordedRunner("claude", ()),
            ),
        ),
    )
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="doctor-codex-resume-revoked",
                harness_family="codex",
                model_ref="gpt-test",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("native_session", "stream"),
            ),
            idempotency_key="doctor-codex-resume-revoked",
        )
        runtime.harnesses.execute_probe(
            admission.run.request_ref,
            prompt="Bounded initial probe.",
            mcp_base_url="http://127.0.0.1:8765",
        )
        with pytest.raises(HarnessAdmissionError, match="provider_auth_revoked"):
            runtime.harnesses.resume_probe_turn(
                admission.run.request_ref,
                prompt="Bounded resumed probe.",
                mcp_base_url="http://127.0.0.1:8765",
            )

        codex = runtime.harnesses.query_status()["adapters"][0]
        assert codex["status"] == "capability_unavailable"
        assert codex["capability_profile"] is None
        assert codex["missing_reason"] == {"code": "provider_auth_revoked"}
        assert codex["provider_operation"]["status"] == "failed"
    finally:
        runtime.close()


def test_codex_and_claude_share_the_sealed_durable_provider_supervisor(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "shared-supervisor"
    counter = tmp_path / "provider-count"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import json, os, pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "sys.stdin.read()\n"
        "print(os.environ['META_RESEARCH_MCP_TOKEN'])\n"
        "print(json.dumps({'type':'thread.started','thread_id':'sealed-thread'}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n",
        encoding="utf-8",
    )
    runner = HarnessSupervisorTransport(workspace)
    environment = {
        "META_RESEARCH_MCP_TOKEN": "opaque-never-in-spool",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": "provider-operation:1",
    }

    first = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    reconciled = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    second_operation = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        {
            **environment,
            "META_RESEARCH_PROVIDER_OPERATION_REF": "provider-operation:2",
        },
    )

    assert first.returncode == 0
    assert reconciled.stdout == first.stdout
    receipt = first.meta_research_transport_receipt
    assert receipt["schema_ref"] == (
        "meta-research/harness-provider-transport-receipt/v1"
    )
    assert reconciled.meta_research_transport_receipt == receipt
    assert second_operation.returncode == 0
    assert counter.read_text(encoding="utf-8") == "2"
    for path in workspace.rglob("*"):
        if path.is_file():
            assert b"opaque-never-in-spool" not in path.read_bytes()


def test_unknown_provider_outcome_reconciles_receipt_before_any_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider-reconciliation"
    counter = tmp_path / "reconciled-provider-count"
    provider = tmp_path / "bounded-provider.py"
    provider.write_text(
        "import json, pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'reconciled-native'}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n",
        encoding="utf-8",
    )
    supervisor = HarnessSupervisorTransport(workspace / "supervisor")

    class DurableCodexAdapter(CodexHarnessAdapter):
        def _provider_version(self) -> str:
            return self.locked_version

        def _argv(self, invocation: HarnessInvocation) -> list[str]:
            return [sys.executable, str(provider)]

    def adapters():
        return (
            DurableCodexAdapter(workspace / "codex", runner=supervisor),
            ClaudeHarnessAdapter(
                workspace / "claude",
                runner=_RecordedRunner("claude", ()),
            ),
        )

    data_root = prepare_data_root(tmp_path / "provider-reconciliation-data")
    runtime = build_production_runtime(data_root, harness_adapters=adapters())
    admission = runtime.harnesses.admit_probe(
        HarnessProbeRequest(
            request_ref="provider-response-lost",
            harness_family="codex",
            model_ref="model-test",
            auth_profile_ref="harness-profile:codex-default",
            required_operation_ids=("research_graph.snapshot.read",),
            required_capabilities=("native_session", "stream"),
        ),
        idempotency_key="provider-response-lost",
    )

    def lose_response_before_owner_commit(**_kwargs):
        raise RuntimeError("simulated daemon response loss")

    runtime.harnesses._complete_operation = lose_response_before_owner_commit
    with pytest.raises(RuntimeError, match="simulated daemon response loss"):
        runtime.harnesses.execute_probe(
            admission.run.request_ref,
            prompt="Run exactly one bounded provider turn.",
            mcp_base_url="http://127.0.0.1:8765",
        )
    runtime.close()
    assert counter.read_text(encoding="utf-8") == "1"

    restored = build_production_runtime(data_root, harness_adapters=adapters())
    try:
        unknown_status = restored.harnesses.query_status()["adapters"][0]
        assert unknown_status["missing_reason"] == {
            "code": "provider_outcome_unknown"
        }
        assert unknown_status["provider_operation"]["status"] == (
            "unknown_outcome"
        )
        reconciled = restored.harnesses.reconcile_probe_turn(
            admission.run.request_ref,
            prompt="Run exactly one bounded provider turn.",
            mcp_base_url="http://127.0.0.1:8765",
        )
        assert reconciled.status == "executed"
        assert reconciled.run_ref == admission.run.run_ref
        assert reconciled.native_session_ref == "reconciled-native"
        assert counter.read_text(encoding="utf-8") == "1"
        profile = restored.harnesses.query_capability_profiles()[0]
        assert len(profile["provider_transport_receipts"]) == 1
        assert restored.harnesses.query_status()["adapters"][0][
            "provider_operation"
        ]["status"] == "executed"
    finally:
        restored.close()


def test_codex_and_claude_complete_short_runs_through_one_typed_admission(
    tmp_path: Path,
) -> None:
    codex_runner = _RecordedRunner(
        "codex",
        (
            {"type": "thread.started", "thread_id": "codex-native-runtime"},
            {"type": "turn.completed"},
        ),
    )
    claude_runner = _RecordedRunner(
        "claude",
        (
            {"type": "system", "subtype": "init", "session_id": "claude-native-runtime", "tools": []},
            {"type": "result", "session_id": "claude-native-runtime", "is_error": False},
        ),
    )
    data_root = prepare_data_root(tmp_path / "typed-runs")
    adapters = (
        CodexHarnessAdapter(data_root.run / "harness", runner=codex_runner),
        ClaudeHarnessAdapter(data_root.run / "harness", runner=claude_runner),
    )
    runtime = build_production_runtime(data_root, harness_adapters=adapters)
    try:
        runs = []
        for family in ("codex", "claude"):
            admission = runtime.harnesses.admit_probe(
                HarnessProbeRequest(
                    request_ref=f"typed-probe-{family}",
                    harness_family=family,
                    model_ref="model-test",
                    auth_profile_ref=f"harness-profile:{family}-default",
                    required_operation_ids=("research_graph.snapshot.read",),
                    required_capabilities=("native_session", "stream"),
                ),
                idempotency_key=f"typed-admission-{family}",
            )
            completed = runtime.harnesses.execute_probe(
                admission.run.request_ref,
                prompt="Run the bounded conformance probe.",
                mcp_base_url="http://127.0.0.1:8765",
            )
            assert completed.run_ref == admission.run.run_ref
            assert completed.attempt_ref == admission.run.attempt_ref
            assert completed.root_session_ref == admission.run.root_session_ref
            assert completed.fence_ref == admission.run.fence_ref
            assert completed.status == "executed"
            runs.append(completed)

        assert runs[0].native_session_ref == "codex-native-runtime"
        assert runs[1].native_session_ref == "claude-native-runtime"
        resumed = runtime.harnesses.resume_probe_turn(
            "typed-probe-codex",
            prompt="Resume the bounded conformance probe.",
            mcp_base_url="http://127.0.0.1:8765",
        )
        resumed_again = runtime.harnesses.resume_probe_turn(
            "typed-probe-codex",
            prompt="Resume the bounded conformance probe.",
            mcp_base_url="http://127.0.0.1:8765",
        )
        assert resumed.run_ref == runs[0].run_ref
        assert resumed.attempt_ref == runs[0].attempt_ref
        assert resumed.root_session_ref == runs[0].root_session_ref
        assert resumed.fence_ref == runs[0].fence_ref
        assert resumed.native_session_ref == runs[0].native_session_ref
        assert resumed_again.native_session_ref == runs[0].native_session_ref
        profiles = runtime.harnesses.query_capability_profiles()
        assert [profile["harness_family"] for profile in profiles] == [
            "codex",
            "claude",
        ]
        assert profiles[0]["capabilities"]["resume"]["status"] == "available"
        assert len(profiles[0]["provider_operation_refs"]) == 3
        assert all("mcp_token" not in json.dumps(profile) for profile in profiles)
        snapshot = runtime.projection.query_snapshot()
        assert snapshot["harnesses"]["gateway"]["transport"] == (
            "streamable_http"
        )
        assert [
            item["locked_version"]
            for item in snapshot["harnesses"]["adapters"]
        ] == ["0.147.0", "2.1.220"]
        assert all(
            item["capability_profile"] is not None
            for item in snapshot["harnesses"]["adapters"]
        )
    finally:
        runtime.close()

    restored = build_production_runtime(data_root, harness_adapters=adapters)
    try:
        assert len(restored.harnesses.query_capability_profiles()) == 2
    finally:
        restored.close()
