from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
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
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root


_REAL_CODEX_CHILD_LEDGER = Path(
    "/root/.codex-openai-account/sessions/2026/08/24/"
    "rollout-2026-08-24T07-44-41-01a032ba-8aec-7f32-a9ce-736ca44fa8a8.jsonl"
)


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


class _RecordedChildLedger:
    def __init__(self, child_ref: str, records: tuple[dict[str, object], ...]) -> None:
        self.child_ref = child_ref
        self.records = records
        self.reads: list[str] = []
        self._skill_packages = _recorded_skill_packages(records)

    def read(self, child_session_ref: str) -> tuple[dict[str, object], ...]:
        self.reads.append(child_session_ref)
        if child_session_ref != self.child_ref:
            raise OSError("unexpected child")
        return self.records

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str:
        expected = self._skill_packages.get(skill_path)
        if expected != injected_body:
            raise OSError("untrusted package")
        return hashlib.sha256(expected.encode("utf-8")).hexdigest()


class _ReadOnlyChildLedger:
    """Deliberately incomplete test double: no package-verification authority."""

    def __init__(self, child_ref: str, records: tuple[dict[str, object], ...]) -> None:
        self.child_ref = child_ref
        self.records = records

    def read(self, child_session_ref: str) -> tuple[dict[str, object], ...]:
        if child_session_ref != self.child_ref:
            raise OSError("unexpected child")
        return self.records


class _MultiRecordedChildLedger:
    def __init__(self, records_by_child: dict[str, tuple[dict[str, object], ...]]) -> None:
        self._records_by_child = records_by_child
        self._skill_packages: dict[str, str] = {}
        for records in records_by_child.values():
            for skill_path, body in _recorded_skill_packages(records).items():
                previous = self._skill_packages.setdefault(skill_path, body)
                if previous != body:
                    raise ValueError("ambiguous test package")

    def read(self, child_session_ref: str) -> tuple[dict[str, object], ...]:
        try:
            return self._records_by_child[child_session_ref]
        except KeyError as error:
            raise OSError("unexpected child") from error

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str:
        expected = self._skill_packages.get(skill_path)
        if expected != injected_body:
            raise OSError("untrusted package")
        return hashlib.sha256(expected.encode("utf-8")).hexdigest()


def _recorded_skill_packages(
    records: tuple[dict[str, object], ...],
) -> dict[str, str]:
    packages: dict[str, str] = {}
    pattern = re.compile(
        r"<skill>\s*<name>\s*code-review\s*</name>\s*<path>\s*"
        r"(/[^<\s]+)\s*</path>(.*?)</skill>",
        re.DOTALL,
    )
    for record in records:
        payload = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            matches = pattern.findall(block["text"])
            if len(matches) == 1:
                path, body = matches[0]
                packages[path] = body
    return packages


def _child_ledger(
    *,
    child_ref: str,
    root_ref: str,
    cwd: str,
    terminal_message: str,
    skill_name: str = "code-review",
    skill_path: str = "/trusted/skills/code-review/SKILL.md",
    skill_body: str = "---\nname: code-review\n---\nreview only\n",
    metadata_overrides: dict[str, object] | None = None,
    sandbox_mode: str = "workspace-write",
) -> tuple[dict[str, object], ...]:
    metadata: dict[str, object] = {
        "id": child_ref,
        "session_id": root_ref,
        "parent_thread_id": root_ref,
        "cwd": cwd,
        "originator": "codex_exec",
        "cli_version": "0.147.0",
        "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": root_ref}}},
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    return (
        {"type": "session_meta", "payload": metadata},
        {
            "type": "turn_context",
            "payload": {
                "cwd": cwd,
                "sandbox_policy": {"type": sandbox_mode},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<skill>\n"
                            f"<name>{skill_name}</name>\n"
                            f"<path>{skill_path}</path>\n"
                            f"{skill_body}</skill>"
                        ),
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": terminal_message,
            },
        },
    )


def _result_review_request(
    *,
    target_ref: str = "target:result",
    target_run_ref: str = "target-run:result",
    evaluation_attempt_ref: str = "evaluation-attempt:result",
    metric_result_ref: str = "metric-result:result",
    asset_manifest_ref: str = "asset-manifest:result",
) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-result-review-request/v1",
        "review_kind": "result",
        "target_ref": target_ref,
        "target_run_ref": target_run_ref,
        "reviewed_evaluation_attempt_ref": evaluation_attempt_ref,
        "reviewed_metric_result_ref": metric_result_ref,
        "reviewed_asset_manifest_ref": asset_manifest_ref,
    }


def _result_review_prompt(request: dict[str, object]) -> str:
    return (
        "Independently review the accepted terminal Target result and return only "
        "the closed meta-research/target-review-evidence/v1 result envelope.\n"
        "<target-result-review-request>\n"
        + json.dumps(request, sort_keys=True, separators=(",", ":"))
        + "\n</target-result-review-request>"
    )


def _result_review_terminal(request: dict[str, object], *, suffix: str = "1") -> str:
    return json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "result",
            "review": {
                "review_ref": f"result-review:{suffix}",
                "reviewed_evaluation_attempt_ref": request[
                    "reviewed_evaluation_attempt_ref"
                ],
                "reviewed_metric_result_ref": request["reviewed_metric_result_ref"],
                "reviewed_asset_manifest_ref": request[
                    "reviewed_asset_manifest_ref"
                ],
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _result_child_ledger(
    *,
    child_ref: str,
    root_ref: str,
    cwd: str,
    prompt: str,
    terminal_message: str,
    sandbox_mode: str = "workspace-write",
    metadata_overrides: dict[str, object] | None = None,
    inject_code_review_skill: bool = False,
) -> tuple[dict[str, object], ...]:
    metadata: dict[str, object] = {
        "id": child_ref,
        "session_id": root_ref,
        "parent_thread_id": root_ref,
        "cwd": cwd,
        "originator": "codex_exec",
        "cli_version": "0.147.0",
        "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": root_ref}}},
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    records: list[dict[str, object]] = [
        {"type": "session_meta", "payload": metadata},
        {
            "type": "turn_context",
            "payload": {
                "cwd": cwd,
                "sandbox_policy": {"type": sandbox_mode},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
    ]
    if inject_code_review_skill:
        records.append(
            _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=cwd,
                terminal_message=terminal_message,
            )[2]
        )
    records.append(
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": terminal_message,
            },
        }
    )
    return tuple(records)


def _codex_review_events(
    *,
    root_ref: str,
    child_ref: str,
    terminal_message: str,
    root_skill: bool = False,
    terminal_status: str = "completed",
    skill_path: str = "/trusted/skills/code-review/SKILL.md",
    structured_prompt: bool = True,
) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = [{"type": "thread.started", "thread_id": root_ref}]
    if root_skill:
        events.append(
            {
                "type": "item.completed",
                "thread_id": root_ref,
                "item": {
                    "type": "skill",
                    "name": "code-review",
                    "sender_thread_id": root_ref,
                },
            }
        )
    events.extend(
        (
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "status": "completed",
                    "sender_thread_id": root_ref,
                    "prompt": (
                        f"Run [skill:$code-review]({skill_path}) and return only the review envelope."
                        if structured_prompt
                        else "Run $code-review and return only the review envelope."
                    ),
                    "receiver_thread_ids": [child_ref],
                    "agents_states": {child_ref: {"status": "pending"}},
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "wait",
                    "status": "completed",
                    "sender_thread_id": root_ref,
                    "receiver_thread_ids": [child_ref],
                    "agents_states": {
                        child_ref: {
                            "status": terminal_status,
                            "message": terminal_message,
                        }
                    },
                },
            },
            {"type": "turn.completed", "thread_id": root_ref},
        )
    )
    return tuple(events)


def _codex_result_review_events(
    *,
    root_ref: str,
    child_ref: str,
    prompt: str,
    terminal_message: str,
) -> tuple[dict[str, object], ...]:
    return (
        {"type": "thread.started", "thread_id": root_ref},
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "status": "completed",
                "sender_thread_id": root_ref,
                "prompt": prompt,
                "receiver_thread_ids": [child_ref],
                "agents_states": {child_ref: {"status": "pending"}},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "wait",
                "status": "completed",
                "sender_thread_id": root_ref,
                "receiver_thread_ids": [child_ref],
                "agents_states": {
                    child_ref: {
                        "status": "completed",
                        "message": terminal_message,
                    }
                },
            },
        },
        {"type": "turn.completed", "thread_id": root_ref},
    )


def _real_native_child_ledger_records() -> tuple[dict[str, object], ...]:
    """Read the fixed Codex 0.147 child probe without inventing its shape."""

    if not _REAL_CODEX_CHILD_LEDGER.is_file():
        pytest.skip("fixed native Codex child ledger is unavailable")
    records = tuple(
        json.loads(line)
        for line in _REAL_CODEX_CHILD_LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not all(isinstance(record, dict) for record in records):
        pytest.skip("fixed native Codex child ledger is malformed")
    return records


def _real_native_child_details(
    records: tuple[dict[str, object], ...],
) -> tuple[str, str, str, str, str]:
    metadata = next(
        (
            record.get("payload")
            for record in records
            if record.get("type") == "session_meta"
            and isinstance(record.get("payload"), dict)
        ),
        None,
    )
    if not isinstance(metadata, dict):
        pytest.skip("fixed native Codex child has no session metadata")
    child_ref = metadata.get("id")
    root_ref = metadata.get("session_id")
    cwd = metadata.get("cwd")
    skill_path: str | None = None
    for record in records:
        payload = record.get("payload")
        if (
            record.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("role") != "user"
            or not isinstance(payload.get("content"), list)
        ):
            continue
        for block in payload["content"]:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            match = re.search(
                r"<skill>\s*<name>\s*code-review\s*</name>\s*<path>\s*"
                r"(/[^<\s]+)\s*</path>",
                block["text"],
                re.DOTALL,
            )
            if match is not None:
                skill_path = match.group(1)
                break
    terminal_message = next(
        (
            payload.get("last_agent_message")
            for record in records
            if record.get("type") == "event_msg"
            and isinstance((payload := record.get("payload")), dict)
            and payload.get("type") == "task_complete"
            and isinstance(payload.get("last_agent_message"), str)
        ),
        None,
    )
    if not all(isinstance(value, str) and value for value in (
        child_ref,
        root_ref,
        cwd,
        skill_path,
        terminal_message,
    )):
        pytest.skip("fixed native Codex child shape changed")
    return child_ref, root_ref, cwd, skill_path, terminal_message


def _real_native_parent_events(
    *, root_ref: str, child_ref: str, cwd: str, skill_path: str, terminal_message: str
) -> tuple[dict[str, object], ...]:
    del cwd  # The parent stream does not repeat cwd; the child ledger binds it.
    return _codex_review_events(
        root_ref=root_ref,
        child_ref=child_ref,
        terminal_message=terminal_message,
        skill_path=skill_path,
    )


def _codex_root_preflight_events(
    *,
    root_ref: str,
    target_ref: str = "target:1",
    target_run_ref: str = "target-run:1",
    implementation_revision_ref: str = "revision:1",
    expected_tree_hash: str = "a" * 64,
    command_id: str = "cmd:self-check",
    command_output: str = "all focused checks passed",
) -> tuple[dict[str, object], ...]:
    common = {
        "target_ref": target_ref,
        "target_run_ref": target_run_ref,
        "implementation_revision_ref": implementation_revision_ref,
        "expected_tree_hash": expected_tree_hash,
    }
    return (
        {
            "type": "item.completed",
            "thread_id": root_ref,
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "schema_ref": (
                            "meta-research/target-candidate-ready-evidence/v1"
                        ),
                        **common,
                    },
                    separators=(",", ":"),
                ),
            },
        },
        {
            "type": "item.completed",
            "thread_id": root_ref,
            "item": {
                "type": "command_execution",
                "id": command_id,
                "exit_code": 0,
                "output": command_output,
            },
        },
        {
            "type": "item.completed",
            "thread_id": root_ref,
            "item": {
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "schema_ref": "meta-research/target-self-check-evidence/v1",
                        **common,
                        "status": "passed",
                    },
                    separators=(",", ":"),
                ),
            },
        },
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


def test_target_root_uses_configurable_long_task_timeout_not_interactive_watchdog(
    tmp_path: Path,
) -> None:
    class SlowRecordedRunner(_RecordedRunner):
        def __init__(self) -> None:
            super().__init__(
                "codex",
                (
                    {"type": "thread.started", "thread_id": "long-root"},
                    {"type": "turn.completed"},
                ),
            )
            self.turn_timeouts: list[float] = []

        def __call__(
            self,
            argv: list[str],
            prompt: str,
            timeout: float,
            environment: dict[str, str],
        ) -> subprocess.CompletedProcess[str]:
            if "--version" not in argv:
                self.turn_timeouts.append(timeout)
                time.sleep(0.12)
                if timeout < 0.12:
                    raise subprocess.TimeoutExpired(argv, timeout)
            return super().__call__(argv, prompt, timeout, environment)

    target_workspace = tmp_path / "target-workspace"
    target_workspace.mkdir()
    runner = SlowRecordedRunner()
    adapter = CodexHarnessAdapter(
        tmp_path / "harness",
        runner=runner,
        timeout_seconds=0.05,
        target_root_timeout_seconds=0.5,
    )

    evidence = adapter.invoke(
        replace(
            _invocation("codex"),
            target_workspace_ref="target-workspace:long-root",
            working_directory=str(target_workspace),
        )
    )

    assert evidence.native_session_ref == "long-root"
    assert runner.turn_timeouts == [0.5]


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


def test_codex_subagent_evidence_records_exact_code_review_skill_invocation(
    tmp_path: Path,
) -> None:
    root_ref = "codex-code-review-root"
    child_ref = "codex-code-review-child"
    terminal_message = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:1"},
            "scope": {"target_ref": "target:1"},
        },
        separators=(",", ":"),
    )
    ledger = _RecordedChildLedger(
        child_ref,
        _child_ledger(
            child_ref=child_ref,
            root_ref=root_ref,
            cwd=str(tmp_path.resolve()),
            terminal_message=terminal_message,
        ),
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=ledger,
        runner=_RecordedRunner(
            "codex",
            (
                {"type": "thread.started", "thread_id": root_ref},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "status": "completed",
                        "sender_thread_id": root_ref,
                        "prompt": (
                            "Run [skill:$code-review](/trusted/skills/code-review/"
                            "SKILL.md) and return only the review envelope."
                        ),
                        "receiver_thread_ids": [child_ref],
                        "agents_states": {child_ref: {"status": "pending"}},
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "status": "completed",
                        "sender_thread_id": root_ref,
                        "receiver_thread_ids": [child_ref],
                        "agents_states": {
                            child_ref: {
                                "status": "completed",
                                "message": terminal_message,
                            }
                        },
                    },
                },
                {"type": "turn.completed", "thread_id": root_ref},
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["skill_name"] == "code-review"
    assert evidence[0]["skill_actor_session_ref"] == child_ref
    assert evidence[0]["child_ledger_lineage"] == {
        "session_id": child_ref,
        "parent_session_id": root_ref,
        "thread_source": "subagent",
        "cwd": str(tmp_path.resolve()),
        "originator": "codex_exec",
        "cli_version": "0.147.0",
        "sandbox_mode": "workspace-write",
    }
    assert ledger.reads == [child_ref]


def test_codex_parallel_other_child_does_not_pollute_unique_reviewer_chain(
    tmp_path: Path,
) -> None:
    root_ref = "codex-parallel-root"
    reviewer_ref = "codex-reviewer-child"
    other_ref = "codex-implementation-child"
    review_terminal = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:parallel"},
            "scope": {"target_ref": "target:parallel"},
        },
        separators=(",", ":"),
    )
    other_terminal = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:untrusted-other-child"},
            "scope": {"target_ref": "target:parallel"},
        },
        separators=(",", ":"),
    )
    reviewer_events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=reviewer_ref,
            terminal_message=review_terminal,
        )
    )
    other_events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=other_ref,
            terminal_message=other_terminal,
        )
    )
    events = (
        reviewer_events[0],
        *_codex_root_preflight_events(root_ref=root_ref),
        other_events[1],
        reviewer_events[1],
        other_events[2],
        reviewer_events[2],
        reviewer_events[3],
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            reviewer_ref,
            _child_ledger(
                child_ref=reviewer_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=review_terminal,
            ),
        ),
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["child_session_ref"] == reviewer_ref
    assert evidence[0]["skill_name"] == "code-review"
    assert evidence[0]["payload"]["review"]["review_ref"] == "review:parallel"
    assert "candidate_ready" in evidence[0]


def test_codex_parallel_complete_reviewers_are_ambiguous_and_rejected(
    tmp_path: Path,
) -> None:
    root_ref = "codex-ambiguous-review-root"
    first_ref = "codex-ambiguous-reviewer-one"
    second_ref = "codex-ambiguous-reviewer-two"
    first_terminal = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:one"},
            "scope": {"target_ref": "target:ambiguous"},
        },
        separators=(",", ":"),
    )
    second_terminal = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:two"},
            "scope": {"target_ref": "target:ambiguous"},
        },
        separators=(",", ":"),
    )
    first_events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=first_ref,
            terminal_message=first_terminal,
        )
    )
    second_events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=second_ref,
            terminal_message=second_terminal,
        )
    )
    events = (
        first_events[0],
        *_codex_root_preflight_events(root_ref=root_ref),
        first_events[1],
        second_events[1],
        first_events[2],
        second_events[2],
        first_events[3],
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_MultiRecordedChildLedger(
            {
                first_ref: _child_ledger(
                    child_ref=first_ref,
                    root_ref=root_ref,
                    cwd=str(tmp_path.resolve()),
                    terminal_message=first_terminal,
                ),
                second_ref: _child_ledger(
                    child_ref=second_ref,
                    root_ref=root_ref,
                    cwd=str(tmp_path.resolve()),
                    terminal_message=second_terminal,
                ),
            }
        ),
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    assert result.profile["subagent_evidence"] == []
    assert result.profile["capabilities"]["subagent"]["status"] == (
        "capability_unavailable"
    )


@pytest.mark.parametrize("sandbox_mode", ("workspace-write", "read-only"))
def test_codex_result_review_uses_native_child_ledger_evidence(
    tmp_path: Path,
    sandbox_mode: str,
) -> None:
    root_ref = "codex-result-review-root"
    child_ref = "codex-result-review-child"
    request = _result_review_request()
    prompt = _result_review_prompt(request)
    terminal = _result_review_terminal(request)
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            child_ref,
            _result_child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                prompt=prompt,
                terminal_message=terminal,
                sandbox_mode=sandbox_mode,
            ),
        ),
        runner=_RecordedRunner(
            "codex",
            _codex_result_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                prompt=prompt,
                terminal_message=terminal,
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["payload"]["review_kind"] == "result"
    assert evidence[0]["review_actor_session_ref"] == child_ref
    assert evidence[0]["child_ledger_lineage"] == {
        "session_id": child_ref,
        "parent_session_id": root_ref,
        "thread_source": "subagent",
        "cwd": str(tmp_path.resolve()),
        "originator": "codex_exec",
        "cli_version": "0.147.0",
        "sandbox_mode": sandbox_mode,
    }
    assert evidence[0]["spawn_prompt_hash"] == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()
    assert evidence[0]["child_terminal_output_hash"] == hashlib.sha256(
        terminal.encode("utf-8")
    ).hexdigest()
    assert "skill_name" not in evidence[0]


@pytest.mark.parametrize(
    "mutation",
    (
        "unstructured-prompt",
        "prompt-ledger-drift",
        "terminal-drift",
        "parent-drift",
        "cwd-drift",
        "danger-sandbox",
        "code-review-skill",
        "review-subject-drift",
    ),
)
def test_codex_result_review_fails_closed_without_exact_native_child_proof(
    tmp_path: Path,
    mutation: str,
) -> None:
    root_ref = "codex-result-negative-root"
    child_ref = "codex-result-negative-child"
    request = _result_review_request()
    prompt = _result_review_prompt(request)
    terminal = _result_review_terminal(request)
    parent_prompt = prompt
    ledger_prompt = prompt
    wait_terminal = terminal
    metadata_overrides: dict[str, object] = {}
    cwd = str(tmp_path.resolve())
    sandbox_mode = "workspace-write"
    inject_code_review_skill = False
    if mutation == "unstructured-prompt":
        parent_prompt = "Please do a result review."
        ledger_prompt = parent_prompt
    elif mutation == "prompt-ledger-drift":
        ledger_prompt = prompt + " tampered"
    elif mutation == "terminal-drift":
        wait_terminal = terminal + " tampered"
    elif mutation == "parent-drift":
        metadata_overrides["parent_thread_id"] = "other-root"
    elif mutation == "cwd-drift":
        metadata_overrides["cwd"] = "/other/workspace"
    elif mutation == "danger-sandbox":
        sandbox_mode = "danger-full-access"
    elif mutation == "code-review-skill":
        inject_code_review_skill = True
    elif mutation == "review-subject-drift":
        document = json.loads(terminal)
        document["review"]["reviewed_metric_result_ref"] = "metric-result:other"
        terminal = json.dumps(document, sort_keys=True, separators=(",", ":"))
        wait_terminal = terminal
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            child_ref,
            _result_child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=cwd,
                prompt=ledger_prompt,
                terminal_message=terminal,
                sandbox_mode=sandbox_mode,
                metadata_overrides=metadata_overrides,
                inject_code_review_skill=inject_code_review_skill,
            ),
        ),
        runner=_RecordedRunner(
            "codex",
            _codex_result_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                prompt=parent_prompt,
                terminal_message=wait_terminal,
            ),
        ),
    ).invoke(_invocation("codex"))

    assert result.profile["subagent_evidence"] == []
    assert result.profile["capabilities"]["subagent"]["status"] == (
        "capability_unavailable"
    )


def test_codex_result_review_ignores_other_completed_native_children(
    tmp_path: Path,
) -> None:
    root_ref = "codex-result-multi-root"
    reviewer_ref = "codex-result-multi-reviewer"
    other_ref = "codex-result-multi-worker"
    request = _result_review_request()
    prompt = _result_review_prompt(request)
    terminal = _result_review_terminal(request, suffix="multi")
    reviewer_events = _codex_result_review_events(
        root_ref=root_ref,
        child_ref=reviewer_ref,
        prompt=prompt,
        terminal_message=terminal,
    )
    other_events = _codex_result_review_events(
        root_ref=root_ref,
        child_ref=other_ref,
        prompt="Perform an unrelated bounded helper task.",
        terminal_message="ordinary helper complete",
    )
    events = (
        reviewer_events[0],
        other_events[1],
        reviewer_events[1],
        other_events[2],
        reviewer_events[2],
        reviewer_events[3],
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            reviewer_ref,
            _result_child_ledger(
                child_ref=reviewer_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                prompt=prompt,
                terminal_message=terminal,
            ),
        ),
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["child_session_ref"] == reviewer_ref
    assert evidence[0]["payload"]["review_kind"] == "result"


def test_codex_two_valid_result_reviewers_are_ambiguous_and_rejected(
    tmp_path: Path,
) -> None:
    root_ref = "codex-result-ambiguous-root"
    first_ref = "codex-result-reviewer-one"
    second_ref = "codex-result-reviewer-two"
    request = _result_review_request()
    prompt = _result_review_prompt(request)
    first_terminal = _result_review_terminal(request, suffix="one")
    second_terminal = _result_review_terminal(request, suffix="two")
    first_events = _codex_result_review_events(
        root_ref=root_ref,
        child_ref=first_ref,
        prompt=prompt,
        terminal_message=first_terminal,
    )
    second_events = _codex_result_review_events(
        root_ref=root_ref,
        child_ref=second_ref,
        prompt=prompt,
        terminal_message=second_terminal,
    )
    events = (
        first_events[0],
        first_events[1],
        second_events[1],
        first_events[2],
        second_events[2],
        first_events[3],
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_MultiRecordedChildLedger(
            {
                first_ref: _result_child_ledger(
                    child_ref=first_ref,
                    root_ref=root_ref,
                    cwd=str(tmp_path.resolve()),
                    prompt=prompt,
                    terminal_message=first_terminal,
                ),
                second_ref: _result_child_ledger(
                    child_ref=second_ref,
                    root_ref=root_ref,
                    cwd=str(tmp_path.resolve()),
                    prompt=prompt,
                    terminal_message=second_terminal,
                ),
            }
        ),
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    assert result.profile["subagent_evidence"] == []


def test_codex_persists_closed_root_candidate_and_self_check_before_spawn(
    tmp_path: Path,
) -> None:
    root_ref = "codex-preflight-root"
    child_ref = "codex-preflight-child"
    terminal_message = (
        '{"schema_ref":"meta-research/target-review-evidence/v1",'
        '"review_kind":"code","review":{}}'
    )
    events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=child_ref,
            terminal_message=terminal_message,
        )
    )
    events[1:1] = _codex_root_preflight_events(root_ref=root_ref)
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            child_ref,
            _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
            ),
        ),
        runner=_RecordedRunner("codex", tuple(events)),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert evidence[0]["candidate_ready"] == {
        "schema_ref": "meta-research/target-candidate-ready-evidence/v1",
        "target_ref": "target:1",
        "target_run_ref": "target-run:1",
        "implementation_revision_ref": "revision:1",
        "expected_tree_hash": "a" * 64,
    }
    assert evidence[0]["self_check"]["status"] == "passed"
    assert evidence[0]["successful_command_item_ids"] == [
        "cmd:self-check"
    ]
    assert evidence[0]["successful_command_exit_hashes"] == [
        canonical_hash(
            {
                "command_item_id": "cmd:self-check",
                "exit_code": 0,
                "output": "all focused checks passed",
            }
        )
    ]
    summary = [
        event for event in result.evidence_events
        if event.get("target_root_evidence")
    ]
    assert len(summary) == 2
    assert any(
        event.get("command_item_id") == "cmd:self-check"
        for event in result.evidence_events
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "after-spawn",
        "wrong-actor",
        "failed-command",
        "agent-claimed-command-facts",
        "duplicate-candidate",
    ),
)
def test_codex_root_preflight_fails_closed_on_noncausal_or_tampered_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    root_ref = "codex-preflight-negative-root"
    child_ref = "codex-preflight-negative-child"
    terminal_message = (
        '{"schema_ref":"meta-research/target-review-evidence/v1",'
        '"review_kind":"code","review":{}}'
    )
    preflight = list(_codex_root_preflight_events(root_ref=root_ref))
    events = list(
        _codex_review_events(
            root_ref=root_ref,
            child_ref=child_ref,
            terminal_message=terminal_message,
        )
    )
    if mutation == "after-spawn":
        events[2:2] = preflight
    else:
        events[1:1] = preflight
        if mutation == "wrong-actor":
            events[1]["thread_id"] = "not-the-root"
        elif mutation == "failed-command":
            events[2]["item"]["exit_code"] = 1
        elif mutation == "agent-claimed-command-facts":
            document = json.loads(events[3]["item"]["text"])
            document["successful_command_item_ids"] = ["forged"]
            events[3]["item"]["text"] = json.dumps(document, separators=(",", ":"))
        elif mutation == "duplicate-candidate":
            events.insert(2, events[1])
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            child_ref,
            _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
            ),
        ),
        runner=_RecordedRunner("codex", tuple(events)),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert "candidate_ready" not in evidence[0]
    assert "self_check" not in evidence[0]


@pytest.mark.parametrize(
    (
        "root_skill",
        "ledger_overrides",
        "skill_name",
        "terminal_status",
        "has_subagent_evidence",
    ),
    (
        pytest.param(True, {}, "code-review", "completed", True, id="root-skill"),
        pytest.param(
            False,
            {"parent_thread_id": "other-root"},
            "code-review",
            "completed",
            True,
            id="parent-drift",
        ),
        pytest.param(
            False,
            {"cwd": "/other/workspace"},
            "code-review",
            "completed",
            True,
            id="cwd-drift",
        ),
        pytest.param(False, {}, "other-skill", "completed", True, id="skill-tamper"),
        pytest.param(False, {}, "code-review", "running", False, id="nonterminal-wait"),
    ),
)
def test_codex_code_review_proof_fails_closed_without_exact_child_chain(
    tmp_path: Path,
    root_skill: bool,
    ledger_overrides: dict[str, object],
    skill_name: str,
    terminal_status: str,
    has_subagent_evidence: bool,
) -> None:
    root_ref = "codex-child-proof-root"
    child_ref = "codex-child-proof-child"
    terminal_message = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    ledger = _RecordedChildLedger(
        child_ref,
        _child_ledger(
            child_ref=child_ref,
            root_ref=root_ref,
            cwd=str(tmp_path.resolve()),
            terminal_message=terminal_message,
            skill_name=skill_name,
            metadata_overrides=ledger_overrides,
        ),
    )
    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                root_skill=root_skill,
                terminal_status=terminal_status,
            ),
        ),
        codex_child_ledger_reader=ledger,
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list)
    assert bool(evidence) is has_subagent_evidence
    if evidence:
        assert "skill_name" not in evidence[0]


def test_codex_code_review_proof_requires_wait_message_equal_child_terminal(
    tmp_path: Path,
) -> None:
    root_ref = "codex-terminal-root"
    child_ref = "codex-terminal-child"
    child_terminal = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    ledger = _RecordedChildLedger(
        child_ref,
        _child_ledger(
            child_ref=child_ref,
            root_ref=root_ref,
            cwd=str(tmp_path.resolve()),
            terminal_message=child_terminal,
        ),
    )
    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=child_terminal + " tampered",
            ),
        ),
        codex_child_ledger_reader=ledger,
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_codex_code_review_requires_a_package_verifying_reader(
    tmp_path: Path,
) -> None:
    root_ref = "codex-reader-root"
    child_ref = "codex-reader-child"
    terminal_message = (
        '{"schema_ref":"meta-research/target-review-evidence/v1",'
        '"review_kind":"code","review":{}}'
    )
    result = CodexHarnessAdapter(
        tmp_path,
        # Runtime structural checks must fail closed rather than preserving the
        # former isinstance-based test-reader hash fallback.
        codex_child_ledger_reader=_ReadOnlyChildLedger(
            child_ref,
            _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
            ),
        ),  # type: ignore[arg-type]
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_codex_code_review_rejects_child_danger_full_access_sandbox(
    tmp_path: Path,
) -> None:
    root_ref = "codex-sandbox-root"
    child_ref = "codex-sandbox-child"
    terminal_message = (
        '{"schema_ref":"meta-research/target-review-evidence/v1",'
        '"review_kind":"code","review":{}}'
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(
            child_ref,
            _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
                sandbox_mode="danger-full-access",
            ),
        ),
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_codex_real_native_danger_sandbox_is_rejected_by_production_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _real_native_child_ledger_records()
    child_ref, root_ref, cwd, skill_path, terminal_message = (
        _real_native_child_details(records)
    )
    observed_sandbox_modes = [
        payload["sandbox_policy"]["type"]
        for record in records
        if record.get("type") == "turn_context"
        and isinstance((payload := record.get("payload")), dict)
        and isinstance(payload.get("sandbox_policy"), dict)
        and isinstance(payload["sandbox_policy"].get("type"), str)
    ]
    assert observed_sandbox_modes == ["danger-full-access"]
    injected_body = _recorded_skill_packages(records)[skill_path]
    installed_package = Path(skill_path).read_text(encoding="utf-8")
    assert injected_body in {
        installed_package,
        "\n" + installed_package,
        installed_package + "\n",
        "\n" + installed_package + "\n",
    }
    monkeypatch.setenv("CODEX_HOME", str(_REAL_CODEX_CHILD_LEDGER.parents[4]))
    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _real_native_parent_events(
                root_ref=root_ref,
                child_ref=child_ref,
                cwd=cwd,
                skill_path=skill_path,
                terminal_message=terminal_message,
            ),
        ),
    ).invoke(
        replace(
            _invocation("codex"),
            target_workspace_ref="target-workspace:real-native-probe",
            working_directory=cwd,
        )
    )

    evidence = result.profile["subagent_evidence"]
    assert result.profile["sandbox_mode"] == "workspace-write"
    assert isinstance(evidence, list) and len(evidence) == 1
    assert "skill_name" not in evidence[0]


def test_codex_real_native_shape_accepts_workspace_write_fixture(
    tmp_path: Path,
) -> None:
    records = _real_native_child_ledger_records()
    child_ref, root_ref, cwd, skill_path, _native_terminal_message = (
        _real_native_child_details(records)
    )
    terminal_message = json.dumps(
        {
            "schema_ref": "meta-research/target-review-evidence/v1",
            "review_kind": "code",
            "review": {"review_ref": "review:real-shape-fixture"},
            "scope": {"target_ref": "target:real-shape-fixture"},
        },
        separators=(",", ":"),
    )
    selected: list[dict[str, object]] = []
    for record in deepcopy(records):
        payload = record.get("payload")
        if record.get("type") in {"session_meta", "turn_context"}:
            selected.append(record)
        elif (
            record.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("role") == "user"
            and isinstance(payload.get("content"), list)
            and any(
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and "<name>code-review</name>" in block["text"]
                for block in payload["content"]
            )
        ):
            selected.append(record)
        elif (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_complete"
        ):
            # Keep the real native envelope and all lineage fields; substitute
            # only the closed Target review payload the probe intentionally
            # did not produce because its diff was empty.
            payload["last_agent_message"] = terminal_message
            selected.append(record)
    for record in selected:
        if record.get("type") == "turn_context":
            payload = record["payload"]
            assert isinstance(payload, dict)
            policy = payload.get("sandbox_policy")
            assert isinstance(policy, dict)
            policy["type"] = "workspace-write"
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=_RecordedChildLedger(child_ref, tuple(selected)),
        runner=_RecordedRunner(
            "codex",
            _real_native_parent_events(
                root_ref=root_ref,
                child_ref=child_ref,
                cwd=cwd,
                skill_path=skill_path,
                terminal_message=terminal_message,
            ),
        ),
    ).invoke(
        replace(
            _invocation("codex"),
            target_workspace_ref="target-workspace:real-native-fixture",
            working_directory=cwd,
        )
    )

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and evidence[0]["skill_name"] == "code-review"
    assert evidence[0]["child_ledger_lineage"]["sandbox_mode"] == "workspace-write"


def test_codex_default_ledger_reader_rejects_symlinked_child_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ref = "codex-symlink-root"
    child_ref = "codex-symlink-child"
    terminal_message = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "08" / "24"
    sessions.mkdir(parents=True)
    package = codex_home / "skills" / "code-review" / "SKILL.md"
    package.parent.mkdir(parents=True)
    package.write_text("review package", encoding="utf-8")
    outside_ledger = tmp_path / "outside-child.jsonl"
    outside_ledger.write_text(
        "\n".join(
            json.dumps(record)
            for record in _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
                skill_path=str(package),
                skill_body="review package",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (sessions / "child.jsonl").symlink_to(outside_ledger)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                skill_path=str(package),
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_codex_default_ledger_reader_binds_package_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ref = "codex-default-root"
    child_ref = "codex-default-child"
    terminal_message = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "08" / "24"
    sessions.mkdir(parents=True)
    package = codex_home / "skills" / "code-review" / "SKILL.md"
    package.parent.mkdir(parents=True)
    package.write_text("review package", encoding="utf-8")
    (sessions / f"rollout-{child_ref}.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
                skill_path=str(package),
                skill_body="review package",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                skill_path=str(package),
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and evidence[0]["skill_name"] == "code-review"
    assert evidence[0]["skill_package_hash"] == hashlib.sha256(
        b"review package"
    ).hexdigest()
    assert evidence[0]["child_terminal_output_hash"] == hashlib.sha256(
        terminal_message.encode("utf-8")
    ).hexdigest()


def test_codex_default_ledger_reader_rejects_symlinked_skill_package_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ref = "codex-package-symlink-root"
    child_ref = "codex-package-symlink-child"
    terminal_message = (
        '{"schema_ref":"meta-research/target-review-evidence/v1",'
        '"review_kind":"code","review":{}}'
    )
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    trusted_skills = codex_home / "trusted-skills"
    package = trusted_skills / "code-review" / "SKILL.md"
    package.parent.mkdir(parents=True)
    package.write_text("review package", encoding="utf-8")
    (codex_home / "skills").symlink_to(trusted_skills, target_is_directory=True)
    claimed_path = codex_home / "skills" / "code-review" / "SKILL.md"
    (sessions / f"rollout-{child_ref}.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
                skill_path=str(claimed_path),
                skill_body="review package",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                skill_path=str(claimed_path),
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_codex_default_ledger_reader_rejects_injected_skill_body_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_ref = "codex-body-root"
    child_ref = "codex-body-child"
    terminal_message = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    package = codex_home / "skills" / "code-review" / "SKILL.md"
    package.parent.mkdir(parents=True)
    package.write_text("trusted package", encoding="utf-8")
    (sessions / f"rollout-{child_ref}.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in _child_ledger(
                child_ref=child_ref,
                root_ref=root_ref,
                cwd=str(tmp_path.resolve()),
                terminal_message=terminal_message,
                skill_path=str(package),
                skill_body="tampered package",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                skill_path=str(package),
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


@pytest.mark.parametrize(
    ("structured_prompt", "spawn_skill_path"),
    (
        pytest.param(False, "/trusted/skills/code-review/SKILL.md", id="plain-text"),
        pytest.param(True, "/trusted/skills/other/SKILL.md", id="path-drift"),
    ),
)
def test_codex_code_review_requires_structured_spawn_skill_path_binding(
    tmp_path: Path,
    structured_prompt: bool,
    spawn_skill_path: str,
) -> None:
    root_ref = "codex-prompt-root"
    child_ref = "codex-prompt-child"
    terminal_message = '{"schema_ref":"meta-research/target-review-evidence/v1","review_kind":"code","review":{}}'
    ledger = _RecordedChildLedger(
        child_ref,
        _child_ledger(
            child_ref=child_ref,
            root_ref=root_ref,
            cwd=str(tmp_path.resolve()),
            terminal_message=terminal_message,
        ),
    )
    result = CodexHarnessAdapter(
        tmp_path,
        codex_child_ledger_reader=ledger,
        runner=_RecordedRunner(
            "codex",
            _codex_review_events(
                root_ref=root_ref,
                child_ref=child_ref,
                terminal_message=terminal_message,
                structured_prompt=structured_prompt,
                skill_path=spawn_skill_path,
            ),
        ),
    ).invoke(_invocation("codex"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and "skill_name" not in evidence[0]


def test_target_codex_invocation_is_workspace_write_and_profiles_it(
    tmp_path: Path,
) -> None:
    invocation = replace(
        _invocation("codex"),
        target_workspace_ref="target-workspace:1",
        working_directory=str(tmp_path.resolve()),
    )
    runner = _RecordedRunner(
        "codex",
        (
            {"type": "thread.started", "thread_id": "target-root"},
            {"type": "turn.completed", "thread_id": "target-root"},
        ),
    )
    result = CodexHarnessAdapter(tmp_path, runner=runner).invoke(invocation)

    argv, _prompt, _environment = runner.calls[-1]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert result.profile["sandbox_mode"] == "workspace-write"


def test_claude_parent_stream_cannot_prove_child_code_review_skill(
    tmp_path: Path,
) -> None:
    root_ref = "claude-code-review-root"
    child_ref = "claude-code-review-child"
    result = ClaudeHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner(
            "claude",
            (
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": root_ref,
                    "tools": ["Skill", "Agent"],
                },
                {
                    "type": "assistant",
                    "session_id": root_ref,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "skill-use-1",
                                "name": "Skill",
                                "input": {"skill": "code-review"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "session_id": root_ref,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "skill-use-1",
                                "is_error": False,
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
                                "id": "agent-use-1",
                                "name": "Agent",
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "session_id": root_ref,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "agent-use-1",
                                "is_error": False,
                                "status": "completed",
                                "agent_id": child_ref,
                                "parent_session_id": root_ref,
                                "result": {
                                    "schema_ref": (
                                        "meta-research/target-review-evidence/v1"
                                    ),
                                    "review_kind": "code",
                                    "review": {"review_ref": "review:1"},
                                    "scope": {"target_ref": "target:1"},
                                },
                            }
                        ]
                    },
                },
                {"type": "result", "session_id": root_ref, "is_error": False},
            ),
        ),
    ).invoke(_invocation("claude"))

    evidence = result.profile["subagent_evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    assert "skill_name" not in evidence[0]


@pytest.mark.parametrize(
    ("events", "expected_status"),
    (
        pytest.param(
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
            "capability_unavailable",
            id="terminal-wait-before-spawn",
        ),
        pytest.param(
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
            "available",
            id="failed-and-successful-spawn",
        ),
    ),
)
def test_subagent_rejects_noncausal_but_ignores_failed_other_spawn(
    tmp_path: Path,
    events: tuple[dict[str, object], ...],
    expected_status: str,
) -> None:
    result = CodexHarnessAdapter(
        tmp_path,
        runner=_RecordedRunner("codex", events),
    ).invoke(_invocation("codex"))

    assert result.profile["capabilities"]["subagent"]["status"] == expected_status


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


def test_harness_transport_projects_redacted_root_command_output_live_and_replays_exactly(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "live-target-root-supervisor"
    provider = tmp_path / "live-target-root-provider.py"
    completion_handoff = {
        "schema_ref": "meta-research/target-completion-handoff/v1",
        "target_ref": "target-live",
        "target_run_ref": "target-run-live",
        "status": "completed",
        "artifacts": [
            {"role": "implementation", "relative_path": "src/model.py"},
            {"role": "result", "relative_path": "outputs/result.json"}
        ],
        "result_document_path": "outputs/result.json",
        "summary": "Root turn completed.",
    }
    completion_document = json.dumps(
        completion_handoff, sort_keys=True, separators=(",", ":")
    )
    provider.write_text(
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'native-root'}), flush=True)\n"
        "print(json.dumps({'type':'item.updated',"
        "'item':{'type':'command_execution','id':'command-1','status':'in_progress',"
        "'output':'\\u001b[32mready\\u001b[0m\\npassword=visible-secret'}}), flush=True)\n"
        "time.sleep(0.35)\n"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'command_execution','id':'command-1','exit_code':0,"
        "'output':'\\u001b[32mready\\u001b[0m\\npassword=visible-secret'}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'child-command','exit_code':0,"
        "'sender_thread_id':'native-child','output':'child-only'}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'agent_message','text':"
        + repr(completion_document)
        + "}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','thread_id':'native-root'}), flush=True)\n",
        encoding="utf-8",
    )
    observed: list[dict[str, object]] = []
    live = threading.Event()

    def accept_live_events(
        operation_ref: str,
        events: tuple[dict[str, object], ...],
    ) -> None:
        assert operation_ref == "provider-operation:target-root:1"
        observed.extend(events)
        if any("target_root_observation" in event for event in events):
            live.set()

    runner = HarnessSupervisorTransport(workspace, event_sink=accept_live_events)
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:1"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "a" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-live",
                "attempt_ref": "attempt-live",
                "attempt_generation": 3,
                "root_session_ref": "root-session-live",
                "fence_ref": "fence-live",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    outcome: list[subprocess.CompletedProcess[str]] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            outcome.append(
                runner(
                    [sys.executable, str(provider)],
                    "bounded prompt",
                    10.0,
                    environment,
                )
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert live.wait(timeout=2), "root output was not projected before turn exit"
    assert worker.is_alive(), "projection only arrived after the provider completed"
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert failures == []
    assert outcome[0].returncode == 0

    first_replay = tuple(observed)
    root_outputs = [
        event["target_root_observation"]
        for event in first_replay
        if "target_root_observation" in event
    ]
    assert root_outputs == [
        {
            "schema_ref": "meta-research/target-root-observation/v1",
            "scope": json.loads(environment["META_RESEARCH_HARNESS_OBSERVATION_SCOPE"]),
            "root_native_session_ref": "native-root",
            "kind": "command_output",
            "stream": "stdout",
            "text": "ready\n",
            "redacted": True,
            "truncated": False,
            "raw_sequence": 2,
        },
        {
            "schema_ref": "meta-research/target-root-observation/v1",
            "scope": json.loads(environment["META_RESEARCH_HARNESS_OBSERVATION_SCOPE"]),
            "root_native_session_ref": "native-root",
            "kind": "command_output",
            "stream": "stdout",
            "text": "password=[REDACTED]",
            "redacted": True,
            "truncated": False,
            "raw_sequence": 3,
        }
    ]
    assert "visible-secret" not in json.dumps(first_replay)
    assert "child-only" not in json.dumps(root_outputs)

    observed.clear()
    replayed = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replayed.stdout == outcome[0].stdout
    assert tuple(observed) == first_replay


def test_target_root_cumulative_output_never_publishes_split_secret_fragments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "split-secret-target-root-supervisor"
    provider = tmp_path / "split-secret-target-root-provider.py"
    events = (
        {"type": "thread.started", "thread_id": "native-split-root"},
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-pem",
                "status": "in_progress",
                "output": "alpha\n-----BE",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-pem",
                "status": "in_progress",
                "output": (
                    "alpha\n-----BEGIN PRIVATE KEY-----\n"
                    "PEM-SUPER-SECRET-BODY"
                ),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-pem",
                "exit_code": 0,
                "output": (
                    "alpha\n-----BEGIN PRIVATE KEY-----\n"
                    "PEM-SUPER-SECRET-BODY\n-----END PRIVATE KEY-----"
                ),
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-ghp",
                "status": "in_progress",
                "output": "beta\ngh",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-ghp",
                "status": "in_progress",
                "output": "beta\nghp_",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-ghp",
                "exit_code": 0,
                "output": "beta\nghp_abcdefghijklmnopqrstuvwxyz",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-sk",
                "status": "in_progress",
                "output": "gamma\nsk",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-sk",
                "status": "in_progress",
                "output": "gamma\nsk-",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-sk",
                "exit_code": 0,
                "output": "gamma\nsk-abcdefghijklmnopqrstuvwxyz",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-bearer",
                "status": "in_progress",
                "output": "delta\nBear",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-bearer",
                "status": "in_progress",
                "output": "delta\nBearer ",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-bearer",
                "exit_code": 0,
                "output": "delta\nBearer bearer-super-secret-value",
            },
        },
        {"type": "turn.completed", "thread_id": "native-split-root"},
    )
    provider.write_text(
        "import json, sys, time\n"
        "events = "
        + repr(events)
        + "\n"
        "sys.stdin.read()\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
        "    time.sleep(0.06)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:split-secret"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "b" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-split-secret",
                "attempt_ref": "attempt-split-secret",
                "attempt_generation": 1,
                "root_session_ref": "root-session-split-secret",
                "fence_ref": "fence-split-secret",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    first_batches: list[tuple[dict[str, object], ...]] = []
    runner = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: first_batches.append(batch),
    )
    first = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )

    first_events = tuple(event for batch in first_batches for event in batch)
    first_observations = tuple(
        event["target_root_observation"] for event in first_events
    )
    by_raw_sequence = {
        observation["raw_sequence"]: observation
        for observation in first_observations
    }
    assert by_raw_sequence[2]["text"] == "alpha\n"
    assert by_raw_sequence[5]["text"] == "beta\n"
    assert by_raw_sequence[8]["text"] == "gamma\n"
    assert by_raw_sequence[11]["text"] == "delta\n"
    assert all(
        observation["redacted"] is ("[REDACTED" in observation["text"])
        for observation in first_observations
    )
    public_text = "".join(
        str(observation["text"]) for observation in first_observations
    )
    assert "-----BE" not in public_text
    assert "PEM-SUPER-SECRET-BODY" not in public_text
    assert "ghp_" not in public_text
    assert "sk-" not in public_text
    assert "bearer-super-secret-value" not in public_text
    assert public_text == (
        "alpha\n[REDACTED PRIVATE KEY]"
        "beta\n[REDACTED]"
        "gamma\n[REDACTED]"
        "delta\nBearer [REDACTED]"
    )

    replay_batches: list[tuple[dict[str, object], ...]] = []
    replay = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: replay_batches.append(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replay.stdout == first.stdout
    assert tuple(event for batch in replay_batches for event in batch) == (
        first_events
    )


def test_target_root_split_ansi_cannot_hide_cumulative_secret_openers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "split-ansi-target-root-supervisor"
    provider = tmp_path / "split-ansi-target-root-provider.py"
    events = (
        {"type": "thread.started", "thread_id": "native-ansi-root"},
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-csi",
                "status": "in_progress",
                "output": "alpha\ngh\x1b[",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-csi",
                "status": "in_progress",
                "output": "alpha\ngh\x1b[31mp_SUPERSECRETVALUE",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-csi",
                "exit_code": 0,
                "output": "alpha\ngh\x1b[31mp_SUPERSECRETVALUE",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-osc-bel",
                "status": "in_progress",
                "output": "beta\nsk\x1b]0;",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-osc-bel",
                "status": "in_progress",
                "output": (
                    "beta\nsk\x1b]0;terminal-title\x07"
                    "-SUPEROSCSECRETVALUE"
                ),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-osc-bel",
                "exit_code": 0,
                "output": (
                    "beta\nsk\x1b]0;terminal-title\x07"
                    "-SUPEROSCSECRETVALUE"
                ),
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-osc-st",
                "status": "in_progress",
                "output": "gamma\nBear\x1b]2;terminal-title",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-osc-st",
                "status": "in_progress",
                "output": "gamma\nBear\x1b]2;terminal-title\x1b",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-osc-st",
                "status": "in_progress",
                "output": (
                    "gamma\nBear\x1b]2;terminal-title\x1b\\"
                    "er SUPERBEARERSECRET"
                ),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-osc-st",
                "exit_code": 0,
                "output": (
                    "gamma\nBear\x1b]2;terminal-title\x1b\\"
                    "er SUPERBEARERSECRET"
                ),
            },
        },
        {"type": "turn.completed", "thread_id": "native-ansi-root"},
    )
    provider.write_text(
        "import json, sys, time\n"
        "events = "
        + repr(events)
        + "\n"
        "sys.stdin.read()\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:split-ansi"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "f" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-split-ansi",
                "attempt_ref": "attempt-split-ansi",
                "attempt_generation": 1,
                "root_session_ref": "root-session-split-ansi",
                "fence_ref": "fence-split-ansi",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    first_events: list[dict[str, object]] = []
    first = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: first_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    first_replay = tuple(first_events)
    observations = tuple(
        event["target_root_observation"] for event in first_replay
    )
    by_raw_sequence = {
        observation["raw_sequence"]: observation
        for observation in observations
    }
    assert by_raw_sequence[2]["text"] == "alpha\n"
    assert 3 not in by_raw_sequence
    assert by_raw_sequence[4]["text"] == "[REDACTED]"
    assert by_raw_sequence[5]["text"] == "beta\n"
    assert 6 not in by_raw_sequence
    assert by_raw_sequence[7]["text"] == "[REDACTED]"
    assert by_raw_sequence[8]["text"] == "gamma\n"
    assert 9 not in by_raw_sequence
    assert 10 not in by_raw_sequence
    assert by_raw_sequence[11]["text"] == "Bearer [REDACTED]"
    assert all(
        observation["redacted"] is ("[REDACTED" in observation["text"])
        for observation in observations
    )
    public_text = "".join(str(item["text"]) for item in observations)
    for secret_fragment in (
        "gh",
        "ghp_",
        "SUPERSECRETVALUE",
        "sk",
        "sk-",
        "SUPEROSCSECRETVALUE",
        "terminal-title",
        "SUPERBEARERSECRET",
    ):
        assert secret_fragment not in public_text

    replayed_events: list[dict[str, object]] = []
    replayed = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: replayed_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replayed.stdout == first.stdout
    assert tuple(replayed_events) == first_replay


def test_target_root_owner_secret_rules_hold_arbitrarily_split_assignments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "owner-secret-target-root-supervisor"
    provider = tmp_path / "owner-secret-target-root-provider.py"
    events = (
        {"type": "thread.started", "thread_id": "native-owner-secret-root"},
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-github",
                "status": "in_progress",
                "output": "alpha\nGITHUB_TO",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-github",
                "status": "in_progress",
                "output": "alpha\nGITHUB_TOKEN=",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-github",
                "exit_code": 0,
                "output": "alpha\nGITHUB_TOKEN=github-super-secret",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-aws",
                "status": "in_progress",
                "output": "beta\nAWS_SECRET_ACC",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-aws",
                "status": "in_progress",
                "output": "beta\nAWS_SECRET_ACCESS_KEY=",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-aws",
                "exit_code": 0,
                "output": (
                    "beta\nAWS_SECRET_ACCESS_KEY="
                    "aws-super-secret-access-key"
                ),
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-wandb",
                "status": "in_progress",
                "output": "gamma\nWANDB_AP",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-wandb",
                "status": "in_progress",
                "output": "gamma\nWANDB_API_KEY=",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-wandb",
                "exit_code": 0,
                "output": "gamma\nWANDB_API_KEY=wandb-super-secret-key",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-database",
                "status": "in_progress",
                "output": "delta\nDATABASE_URL=post",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-database",
                "status": "in_progress",
                "output": "delta\nDATABASE_URL=postgres://alice:pass",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-database",
                "status": "in_progress",
                "output": (
                    "delta\nDATABASE_URL="
                    "postgres://alice:pass@db.example/research"
                ),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-database",
                "exit_code": 0,
                "output": (
                    "delta\nDATABASE_URL="
                    "postgres://alice:pass@db.example/research"
                ),
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-basic",
                "status": "in_progress",
                "output": "epsilon\nAuthoriz",
            },
        },
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "id": "command-basic",
                "status": "in_progress",
                "output": "epsilon\nAuthorization: Basic ",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "command-basic",
                "exit_code": 0,
                "output": (
                    "epsilon\nAuthorization: Basic "
                    "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
                ),
            },
        },
        {"type": "turn.completed", "thread_id": "native-owner-secret-root"},
    )
    provider.write_text(
        "import json, sys, time\n"
        "events = "
        + repr(events)
        + "\n"
        "sys.stdin.read()\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
        "    time.sleep(0.04)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:owner-secret"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "1" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-owner-secret",
                "attempt_ref": "attempt-owner-secret",
                "attempt_generation": 1,
                "root_session_ref": "root-session-owner-secret",
                "fence_ref": "fence-owner-secret",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    first_events: list[dict[str, object]] = []
    first = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: first_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    first_replay = tuple(first_events)
    observations = tuple(
        event["target_root_observation"] for event in first_replay
    )
    public_text = "".join(str(item["text"]) for item in observations)
    assert public_text == (
        "alpha\nGITHUB_TOKEN=[REDACTED]"
        "beta\nAWS_SECRET_ACCESS_KEY=[REDACTED]"
        "gamma\nWANDB_API_KEY=[REDACTED]"
        "delta\n[REDACTED]"
        "epsilon\nAuthorization: [REDACTED]"
    )
    for secret_fragment in (
        "github-super-secret",
        "aws-super-secret-access-key",
        "wandb-super-secret-key",
        "alice:pass",
        "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
    ):
        assert secret_fragment not in public_text
    assert all(
        item["redacted"] is ("[REDACTED" in item["text"])
        for item in observations
    )

    replayed_events: list[dict[str, object]] = []
    replayed = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: replayed_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replayed.stdout == first.stdout
    assert tuple(replayed_events) == first_replay


def test_target_root_unclosed_lines_withhold_every_secret_split_until_final(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "all-splits-target-root-supervisor"
    provider = tmp_path / "all-splits-target-root-provider.py"
    secrets = (
        "DATABASE_URL=postgres://alice:plaincredential123@db.local/x",
        "x=redis://alice:opaquevalue@host/0",
        "my password is hunter22",
        "AIza0123456789abcdefghijklmn",
        "eyJabcdefgh.ijklmnop.qrstuvwx",
        "password\r=visible-secret",
        "GITHUB_TOKEN\r=github-carriage-secret",
        "https://alice:pass\r@example.com/x",
    )
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "native-all-splits-root"}
    ]
    expected_completed_sequences: list[int] = []
    expected_projection_by_secret = (
        "[REDACTED]",
        "[REDACTED]",
        "[REDACTED]",
        "[REDACTED]",
        "[REDACTED]",
        "password=[REDACTED]",
        "GITHUB_TOKEN=[REDACTED]",
        "[REDACTED]",
    )
    expected_projection: list[str] = []
    for secret_index, secret in enumerate(secrets):
        for split in range(len(secret) + 1):
            command_ref = f"all-splits-{secret_index}-{split}"
            events.append(
                {
                    "type": "item.updated",
                    "item": {
                        "type": "command_execution",
                        "id": command_ref,
                        "status": "in_progress",
                        "output": secret[:split],
                    },
                }
            )
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "id": command_ref,
                        "exit_code": 0,
                        "output": secret,
                    },
                }
            )
            expected_completed_sequences.append(len(events))
            expected_projection.append(
                expected_projection_by_secret[secret_index]
            )

    characterwise = "DATABASE_URL=postgres://alice:characterwise@db.local/x"
    for size in range(1, len(characterwise) + 1):
        events.append(
            {
                "type": "item.updated",
                "item": {
                    "type": "command_execution",
                    "id": "characterwise-command",
                    "status": "in_progress",
                    "output": characterwise[:size],
                },
            }
        )
    events.append(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "characterwise-command",
                "exit_code": 0,
                "output": characterwise,
            },
        }
    )
    expected_completed_sequences.append(len(events))
    expected_projection.append("[REDACTED]")
    events.append(
        {"type": "turn.completed", "thread_id": "native-all-splits-root"}
    )
    provider.write_text(
        "import json, sys\n"
        "events = "
        + repr(tuple(events))
        + "\n"
        "sys.stdin.read()\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:all-splits"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "2" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-all-splits",
                "attempt_ref": "attempt-all-splits",
                "attempt_generation": 1,
                "root_session_ref": "root-session-all-splits",
                "fence_ref": "fence-all-splits",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    first_events: list[dict[str, object]] = []
    first = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: first_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    first_replay = tuple(first_events)
    observations = tuple(
        event["target_root_observation"] for event in first_replay
    )
    assert [item["raw_sequence"] for item in observations] == (
        expected_completed_sequences
    )
    assert [item["text"] for item in observations] == expected_projection
    assert all(item["redacted"] is True for item in observations)
    entire_public_history = "".join(
        str(item["text"]) for item in observations
    )
    for secret in (*secrets, characterwise):
        assert secret not in entire_public_history

    replayed_events: list[dict[str, object]] = []
    replayed = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: replayed_events.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replayed.stdout == first.stdout
    assert tuple(replayed_events) == first_replay


def test_target_root_unresolved_output_tail_over_budget_fails_closed_as_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meta_research.harness_adapters._TARGET_ROOT_OUTPUT_PENDING_LIMIT",
        16,
    )
    workspace = tmp_path / "bounded-tail-target-root-supervisor"
    provider = tmp_path / "bounded-tail-target-root-provider.py"
    provider.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'bounded-root'}), flush=True)\n"
        "output = 'password' + (' ' * 20)\n"
        "print(json.dumps({'type':'item.updated','item':{"
        "'type':'command_execution','id':'bounded-command',"
        "'status':'in_progress','output':output}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'bounded-command',"
        "'exit_code':0,'output':output}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','thread_id':'bounded-root'}), flush=True)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:bounded-tail"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "c" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-bounded-tail",
                "attempt_ref": "attempt-bounded-tail",
                "attempt_generation": 1,
                "root_session_ref": "root-session-bounded-tail",
                "fence_ref": "fence-bounded-tail",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }

    observed: list[dict[str, object]] = []
    runner = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: observed.extend(batch),
    )
    first = runner(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    first_replay = tuple(observed)
    outputs = [
        event["target_root_observation"] for event in first_replay
    ]
    assert outputs == [
        {
            "schema_ref": "meta-research/target-root-observation/v1",
            "scope": json.loads(
                environment["META_RESEARCH_HARNESS_OBSERVATION_SCOPE"]
            ),
            "root_native_session_ref": "bounded-root",
            "kind": "output_gap",
            "stream": "stdout",
            "text": "[REDACTED]",
            "redacted": True,
            "truncated": True,
            "dropped_bytes": len("password" + (" " * 20)),
            "dropped_events": 1,
            "raw_sequence": 2,
        }
    ]
    assert "password" not in json.dumps(first_replay)

    observed.clear()
    replay = HarnessSupervisorTransport(
        workspace,
        event_sink=lambda _operation_ref, batch: observed.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )
    assert replay.stdout == first.stdout
    assert tuple(observed) == first_replay


def test_target_root_safe_unfinished_line_is_withheld_until_command_completion(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "safe-prefix-target-root-provider.py"
    updated = tmp_path / "safe-prefix-updated"
    provider.write_text(
        "import json, pathlib, sys, time\n"
        f"updated = pathlib.Path({str(updated)!r})\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'safe-prefix-root'}), flush=True)\n"
        "print(json.dumps({'type':'item.updated','item':{"
        "'type':'command_execution','id':'safe-prefix-command',"
        "'status':'in_progress','output':'ordinary-live-prefix'}}), flush=True)\n"
        "updated.write_text('updated')\n"
        "time.sleep(1.0)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'safe-prefix-command',"
        "'exit_code':0,'output':'ordinary-live-prefix'}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','thread_id':'safe-prefix-root'}), flush=True)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:safe-prefix"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "d" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-safe-prefix",
                "attempt_ref": "attempt-safe-prefix",
                "attempt_generation": 1,
                "root_session_ref": "root-session-safe-prefix",
                "fence_ref": "fence-safe-prefix",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    observed: list[dict[str, object]] = []

    def accept(
        _operation_ref: str,
        events: tuple[dict[str, object], ...],
    ) -> None:
        observed.extend(events)

    outcome: list[subprocess.CompletedProcess[str]] = []
    runner = HarnessSupervisorTransport(
        tmp_path / "safe-prefix-target-root-supervisor",
        event_sink=accept,
    )
    worker = threading.Thread(
        target=lambda: outcome.append(
            runner(
                [sys.executable, str(provider)],
                "bounded prompt",
                10.0,
                environment,
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 2
    while not updated.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert updated.exists()
    time.sleep(0.2)
    assert worker.is_alive()
    assert observed == []
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome[0].returncode == 0
    assert "".join(
        str(event["target_root_observation"]["text"])
        for event in observed
    ) == "ordinary-live-prefix"
    assert observed[0]["target_root_observation"]["raw_sequence"] == 3


def test_target_root_hostile_lone_surrogate_is_sanitized_without_losing_turn(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "surrogate-target-root-provider.py"
    provider.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'surrogate-root'}), flush=True)\n"
        "prefix = '\\ud800safe-progress\\nsk-'\n"
        "print(json.dumps({'type':'item.updated','item':{"
        "'type':'command_execution','id':'surrogate-command',"
        "'status':'in_progress','output':prefix}}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{"
        "'type':'command_execution','id':'surrogate-command',"
        "'exit_code':0,'output':prefix + 'surrogate-secret-value'}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','thread_id':'surrogate-root'}), flush=True)\n",
        encoding="utf-8",
    )
    environment = {
        "META_RESEARCH_MCP_TOKEN": "transport-token-never-public",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": (
            "provider-operation:target-root:surrogate"
        ),
        "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "e" * 64,
        "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": json.dumps(
            {
                "schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": "target-run-surrogate",
                "attempt_ref": "attempt-surrogate",
                "attempt_generation": 1,
                "root_session_ref": "root-session-surrogate",
                "fence_ref": "fence-surrogate",
                "native_session_ref": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    observed: list[dict[str, object]] = []
    completed = HarnessSupervisorTransport(
        tmp_path / "surrogate-target-root-supervisor",
        event_sink=lambda _operation_ref, batch: observed.extend(batch),
    )(
        [sys.executable, str(provider)],
        "bounded prompt",
        10.0,
        environment,
    )

    assert completed.returncode == 0
    observations = [
        event["target_root_observation"] for event in observed
    ]
    assert [item["text"] for item in observations] == [
        "safe-progress\n",
        "[REDACTED]",
    ]
    assert all(item["redacted"] is True for item in observations)
    assert "sk-" not in json.dumps(observed)
    assert "surrogate-secret-value" not in json.dumps(observed)


def test_concurrent_reconcile_joins_running_provider_operation_without_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "running-provider-reconcile"
    counter = tmp_path / "provider-count"
    release = tmp_path / "release-provider"
    provider = tmp_path / "blocking-provider.py"
    provider.write_text(
        "import json, pathlib, sys, time\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        f"release = pathlib.Path({str(release)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'same-root'}), flush=True)\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n",
        encoding="utf-8",
    )
    runner = HarnessSupervisorTransport(workspace)
    restored_runner = HarnessSupervisorTransport(workspace)
    environment = {
        "META_RESEARCH_MCP_TOKEN": "opaque-reconcile-token",
        "META_RESEARCH_HARNESS_FAMILY": "codex",
        "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path),
        "META_RESEARCH_PROVIDER_OPERATION_REF": "provider-operation:running-root",
    }
    outcomes: list[subprocess.CompletedProcess[str]] = []
    failures: list[BaseException] = []

    def invoke(transport: HarnessSupervisorTransport) -> None:
        try:
            outcomes.append(
                transport(
                    [sys.executable, str(provider)],
                    "same durable turn",
                    3.0,
                    environment,
                )
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    first = threading.Thread(target=invoke, args=(runner,))
    first.start()
    deadline = time.monotonic() + 2
    while not counter.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert counter.read_text(encoding="utf-8") == "1"

    reconciler = threading.Thread(target=invoke, args=(restored_runner,))
    reconciler.start()
    time.sleep(0.15)
    assert counter.read_text(encoding="utf-8") == "1"
    assert first.is_alive() and reconciler.is_alive()

    release.write_text("continue", encoding="utf-8")
    first.join(timeout=5)
    reconciler.join(timeout=5)
    assert not first.is_alive() and not reconciler.is_alive()
    assert failures == []
    assert len(outcomes) == 2
    assert outcomes[0].stdout == outcomes[1].stdout
    assert counter.read_text(encoding="utf-8") == "1"


def test_root_stdout_is_not_promoted_into_formal_harness_evidence(
    tmp_path: Path,
) -> None:
    handoff = {
        "schema_ref": "meta-research/target-completion-handoff/v1",
        "target_ref": "target-formal-boundary",
        "target_run_ref": "harness_run:1",
        "status": "completed",
        "artifacts": [
            {"role": "implementation", "relative_path": "src/model.py"},
            {"role": "result", "relative_path": "outputs/result.json"}
        ],
        "result_document_path": "outputs/result.json",
        "summary": "Root turn completed.",
    }
    runner = _RecordedRunner(
        "codex",
        (
            {"type": "thread.started", "thread_id": "formal-root"},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "id": "formal-command",
                    "exit_code": 0,
                    "output": "password=visible-secret",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        handoff, sort_keys=True, separators=(",", ":")
                    ),
                },
            },
            {"type": "turn.completed"},
        ),
    )

    evidence = CodexHarnessAdapter(tmp_path, runner=runner).invoke(
        _invocation("codex")
    )

    encoded = json.dumps(evidence.evidence_events)
    assert "target_root_observation" not in encoded
    assert "visible-secret" not in encoded
    completion_events = [
        event
        for event in evidence.evidence_events
        if "target_root_completion_candidate" in event
    ]
    assert len(completion_events) == 1
    assert completion_events[0]["target_root_completion_candidate"] == handoff
    assert completion_events[0]["target_root_agent_message"] is True
    assert evidence.evidence_events[-1]["target_root_terminal"] is True


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
        assert snapshot["harnesses"]["status"] == "capability_unavailable"
        assert all(
            item["capability_profile"] is None
            and item["missing_reason"]
            == {"code": "full_conformance_not_recorded"}
            for item in snapshot["harnesses"]["adapters"]
        )
    finally:
        runtime.close()

    restored = build_production_runtime(data_root, harness_adapters=adapters)
    try:
        assert len(restored.harnesses.query_capability_profiles()) == 2
    finally:
        restored.close()
