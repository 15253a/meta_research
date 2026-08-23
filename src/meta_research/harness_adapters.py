from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_verified_exit_receipt,
    write_supervisor_request,
)
from meta_research.quest_drafting import (
    _CancellableProcessRunner,
    _ProcessStopped,
)


HarnessFamily = Literal["codex", "claude"]
HarnessProcessRunner = Callable[
    [list[str], str, float, dict[str, str]], subprocess.CompletedProcess[str]
]

CODEX_LOCKED_VERSION = "0.147.0"
CLAUDE_LOCKED_VERSION = "2.1.220"
_MCP_TOKEN_ENV = "META_RESEARCH_MCP_TOKEN"
_HARNESS_FAMILY_ENV = "META_RESEARCH_HARNESS_FAMILY"
_HARNESS_WORKSPACE_ENV = "META_RESEARCH_HARNESS_WORKSPACE"
_PROVIDER_OPERATION_ENV = "META_RESEARCH_PROVIDER_OPERATION_REF"
_STREAM_LIMIT = 16 * 1024 * 1024
_RESULT_LIMIT = 1024 * 1024
HARNESS_CAPABILITIES = (
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
)


class HarnessAdapterUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HarnessRunnerOutcomeUnknown(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessInvocation:
    harness_family: str
    provider_operation_ref: str
    run_ref: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    fence_ref: str
    model_ref: str
    prompt: str
    mcp_url: str
    mcp_token: str
    native_session_ref: str | None = None


@dataclass(frozen=True)
class HarnessTurnEvidence:
    native_session_ref: str
    profile: dict[str, object]
    evidence_events: tuple[dict[str, object], ...]
    stream_hash: str
    transport_receipt: dict[str, object] | None = None


class HarnessAdapter(Protocol):
    family: HarnessFamily
    locked_version: str

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence: ...

    def installation_profile(self) -> dict[str, object]: ...


class _NativeCliHarnessAdapter:
    family: HarnessFamily
    executable: str
    locked_version: str

    def __init__(
        self,
        workspace: Path,
        *,
        runner: HarnessProcessRunner | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._runner = runner or HarnessSupervisorTransport(
            workspace / "shared-provider-supervisor"
        )
        self._timeout_seconds = timeout_seconds
        self._cached_installation_profile: dict[str, object] | None = None

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
        self._validate_invocation(invocation)
        provider_version = self._provider_version()
        argv = self._argv(invocation)
        environment = {
            _MCP_TOKEN_ENV: invocation.mcp_token,
            _HARNESS_FAMILY_ENV: self.family,
            _HARNESS_WORKSPACE_ENV: str(self._workspace.resolve()),
            _PROVIDER_OPERATION_ENV: invocation.provider_operation_ref,
            "NO_PROXY": _loopback_no_proxy(),
            "no_proxy": _loopback_no_proxy(),
        }
        try:
            completed = self._runner(
                argv,
                invocation.prompt,
                self._timeout_seconds,
                environment,
            )
        except FileNotFoundError as error:
            raise HarnessAdapterUnavailable("provider_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise HarnessAdapterUnavailable("provider_timeout") from error
        except HarnessRunnerOutcomeUnknown as error:
            raise HarnessAdapterUnavailable("provider_outcome_unknown") from error
        except OSError as error:
            raise HarnessAdapterUnavailable("provider_io_unavailable") from error
        if completed.returncode != 0:
            code = (
                "provider_auth_revoked"
                if _looks_like_auth_failure(completed.stderr)
                or _stream_has_auth_failure(completed.stdout)
                else "provider_failed"
            )
            raise HarnessAdapterUnavailable(code)
        events = _parse_jsonl(completed.stdout)
        if not events:
            raise HarnessAdapterUnavailable("provider_stream_invalid")
        return _evidence_from_events(
            family=self.family,
            provider_version=provider_version,
            events=events,
            expected_native_session_ref=invocation.native_session_ref,
            evidence_scope_ref=canonical_hash(
                {
                    "run_ref": invocation.run_ref,
                    "provider_operation_ref": invocation.provider_operation_ref,
                    "attempt_ref": invocation.attempt_ref,
                    "fence_ref": invocation.fence_ref,
                    "native_session_ref": invocation.native_session_ref,
                    "prompt_hash": canonical_hash(invocation.prompt),
                }
            ),
            transport_receipt=getattr(
                completed, "meta_research_transport_receipt", None
            ),
        )

    def installation_profile(self) -> dict[str, object]:
        if self._cached_installation_profile is not None:
            return dict(self._cached_installation_profile)
        try:
            observed_version = self._provider_version()
        except HarnessAdapterUnavailable as error:
            profile: dict[str, object] = {
                "harness_family": self.family,
                "locked_version": self.locked_version,
                "status": "capability_unavailable",
                "reason": {"code": error.code},
            }
        else:
            profile = {
                "harness_family": self.family,
                "locked_version": self.locked_version,
                "provider_version": observed_version,
                "status": "ready",
            }
        self._cached_installation_profile = profile
        return dict(profile)

    def _provider_version(self) -> str:
        try:
            completed = self._runner(
                [self.executable, "--version"],
                "",
                10.0,
                {},
            )
        except FileNotFoundError as error:
            raise HarnessAdapterUnavailable("provider_unavailable") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HarnessAdapterUnavailable("provider_version_unavailable") from error
        if completed.returncode != 0:
            raise HarnessAdapterUnavailable("provider_version_unavailable")
        match = re.search(r"\d+\.\d+\.\d+", completed.stdout)
        if match is None:
            raise HarnessAdapterUnavailable("provider_version_unavailable")
        observed = match.group(0)
        if observed != self.locked_version:
            raise HarnessAdapterUnavailable("provider_version_drift")
        return observed

    def _validate_invocation(self, invocation: HarnessInvocation) -> None:
        refs = (
            invocation.run_ref,
            invocation.provider_operation_ref,
            invocation.attempt_ref,
            invocation.root_session_ref,
            invocation.fence_ref,
            invocation.model_ref,
            invocation.prompt,
            invocation.mcp_url,
            invocation.mcp_token,
        )
        if (
            invocation.harness_family != self.family
            or any(not value for value in refs)
            or invocation.attempt_generation < 1
            or not invocation.mcp_url.startswith("http://")
        ):
            raise HarnessAdapterUnavailable("harness_invocation_invalid")

    def _argv(self, invocation: HarnessInvocation) -> list[str]:
        raise NotImplementedError


class CodexHarnessAdapter(_NativeCliHarnessAdapter):
    family = "codex"
    executable = "codex"
    locked_version = CODEX_LOCKED_VERSION

    def _argv(self, invocation: HarnessInvocation) -> list[str]:
        argv = [
            self.executable,
            "exec",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--enable",
            "multi_agent",
            "--json",
            "--model",
            invocation.model_ref,
            "--config",
            'approval_policy="never"',
            "--config",
            'web_search="live"',
            "--config",
            "mcp_servers={}",
            "--sandbox",
            "danger-full-access",
            "--cd",
            str(self._workspace),
            "--config",
            f'mcp_servers.meta_research.url="{invocation.mcp_url}"',
            "--config",
            (
                "mcp_servers.meta_research.bearer_token_env_var="
                f'"{_MCP_TOKEN_ENV}"'
            ),
        ]
        if invocation.native_session_ref is None:
            argv.append("-")
        else:
            argv.extend(["resume", invocation.native_session_ref, "-"])
        return argv


class ClaudeHarnessAdapter(_NativeCliHarnessAdapter):
    family = "claude"
    executable = "claude"
    locked_version = CLAUDE_LOCKED_VERSION

    def _argv(self, invocation: HarnessInvocation) -> list[str]:
        config = {
            "mcpServers": {
                "meta_research": {
                    "type": "http",
                    "url": invocation.mcp_url,
                    "headers": {
                        "Authorization": f"Bearer ${{{_MCP_TOKEN_ENV}}}",
                    },
                }
            }
        }
        config_directory = self._workspace / "mcp-configs"
        config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = config_directory / (
            canonical_hash(
                {
                    "run_ref": invocation.run_ref,
                    "attempt_ref": invocation.attempt_ref,
                    "fence_ref": invocation.fence_ref,
                    "mcp_url": invocation.mcp_url,
                }
            )
            + ".json"
        )
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
        if config_path.exists():
            if config_path.read_text(encoding="utf-8") != encoded:
                raise HarnessAdapterUnavailable("mcp_config_identity_conflict")
        else:
            _write_private(config_path, encoded)
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--include-partial-messages",
            "--forward-subagent-text",
            "--model",
            invocation.model_ref,
            "--mcp-config",
            str(config_path),
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            (
                "Bash,Read,Write,Edit,Agent,WebSearch,WebFetch,Skill,"
                "mcp__meta_research__*"
            ),
        ]
        if invocation.native_session_ref is not None:
            argv.extend(["--resume", invocation.native_session_ref])
        return argv


def _run_process(
    argv: list[str],
    prompt: str,
    timeout: float,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **environment},
    )


class HarnessSupervisorTransport:
    """Thin Harness adapter over the existing shared provider runner."""

    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: _CancellableProcessRunner | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        _key_path, self._transport_key = ensure_transport_key(self._workspace)
        self._process_runner = process_runner or _CancellableProcessRunner()

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        if "--version" in argv and not prompt:
            return _run_process(argv, prompt, timeout, environment)
        family = environment.get(_HARNESS_FAMILY_ENV)
        if family not in {"codex", "claude"}:
            raise OSError("harness family unavailable")
        operation_ref = environment.get(_PROVIDER_OPERATION_ENV)
        if not operation_ref or len(operation_ref) > 128:
            raise OSError("provider operation identity unavailable")
        invocation = {
            "schema_ref": "meta-research/harness-provider-operation/v1",
            "family": family,
            "provider_operation_ref": operation_ref,
            "argv": argv,
            "prompt_hash": canonical_hash(prompt),
            "timeout_seconds": timeout,
            "environment_names": sorted(environment),
        }
        invocation_hash = canonical_hash(invocation)
        operation_root = self._workspace / "provider-operations"
        directory = operation_root / invocation_hash[:2] / invocation_hash
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        prompt_path = directory / "prompt.txt"
        schema_path = directory / "output-schema.json"
        stdout_path = directory / "stdout.jsonl"
        result_path = directory / "last-message.json"
        provider_argv_path = directory / "provider-argv.json"
        request_path = directory / "supervisor-request.json"
        _ensure_private(prompt_path, prompt)
        _ensure_private(
            schema_path,
            json.dumps(
                {"type": "object"}, sort_keys=True, separators=(",", ":")
            ),
        )
        _ensure_private(
            provider_argv_path,
            json.dumps(argv, ensure_ascii=False, separators=(",", ":")),
        )
        bridge_argv = [
            sys.executable,
            "-m",
            "meta_research.harness_cli_bridge",
            "--family",
            family,
            "--provider-argv",
            str(provider_argv_path),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "-",
        ]
        try:
            write_supervisor_request(
                request_path,
                {
                    "schema_ref": SUPERVISOR_REQUEST_SCHEMA_V2,
                    "invocation_hash": invocation_hash,
                    "argv": bridge_argv,
                    "timeout_seconds": timeout,
                    "stream_max_bytes": _STREAM_LIMIT,
                    "result_max_bytes": _RESULT_LIMIT,
                    "prompt_path": str(prompt_path),
                    "schema_path": str(schema_path),
                    "stdout_path": str(stdout_path),
                    "result_path": str(result_path),
                    "lock_path": str(directory / "supervisor.lock"),
                    "ready_path": str(directory / "supervisor-ready.json"),
                    "started_path": str(directory / "provider-started.json"),
                    "receipt_path": str(directory / "supervisor-exit.json"),
                    "stop_path": str(directory / "supervisor-stop.json"),
                },
                self._transport_key,
            )
        except ProviderSupervisorError as error:
            raise OSError("supervisor request unavailable") from error
        receipt_path = directory / "supervisor-exit.json"
        if not receipt_path.exists():
            try:
                self._process_runner.run_durable_job(
                    invocation_hash,
                    bridge_argv,
                    prompt,
                    timeout,
                    stdout_path,
                    directory / "pid.json",
                    request_path,
                    environment=environment,
                )
            except _ProcessStopped as error:
                raise OSError("provider supervisor stopped") from error
            except subprocess.TimeoutExpired as error:
                if (directory / "provider-started.json").exists():
                    raise HarnessRunnerOutcomeUnknown(
                        "provider outcome requires reconciliation"
                    ) from error
                raise
            except OSError:
                if not receipt_path.exists():
                    if (directory / "provider-started.json").exists():
                        raise HarnessRunnerOutcomeUnknown(
                            "provider outcome requires reconciliation"
                        )
                    raise
        try:
            receipt, envelope = read_verified_exit_receipt(
                receipt_path,
                key=self._transport_key,
                invocation_hash=invocation_hash,
                prompt_path=prompt_path,
                schema_path=schema_path,
                stdout_path=stdout_path,
                result_path=result_path,
                expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            )
            stdout = stdout_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise OSError("provider supervisor receipt invalid") from error
        termination_reason = receipt["termination_reason"]
        returncode = int(receipt["returncode"])
        if termination_reason != "completed":
            returncode = {
                "timeout": 124,
                "stopped": 143,
                "output_limit": 125,
                "descendant_process": 126,
                "launch_failed": 127,
            }[str(termination_reason)]
        stderr = (
            "authentication revoked"
            if '"error_kind":"auth_revoked"' in stdout
            else ""
        )
        completed = subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        completed.meta_research_transport_receipt = {
            "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
            "spool_ref": "provider-spool:" + invocation_hash,
            "transport_invocation_hash": invocation_hash,
            "supervisor_receipt_hash": canonical_hash(envelope),
            "termination_reason": str(termination_reason),
            "provider_returncode": int(receipt["returncode"]),
        }
        return completed


def _parse_jsonl(value: str) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line in value.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return tuple(events)


def _evidence_from_events(
    *,
    family: HarnessFamily,
    provider_version: str,
    events: tuple[dict[str, object], ...],
    expected_native_session_ref: str | None,
    evidence_scope_ref: str,
    transport_receipt: dict[str, object] | None = None,
) -> HarnessTurnEvidence:
    observed: dict[str, list[str]] = {
        name: [] for name in HARNESS_CAPABILITIES
    }
    native_refs: set[str] = set()
    summaries: list[dict[str, object]] = []
    terminal = False
    claude_pending_tools: dict[str, tuple[str, str]] = {}
    for sequence, event in enumerate(events, start=1):
        summary, capabilities, native_ref, is_terminal = _summarize_event(
            family, event
        )
        if summary is None:
            continue
        event_ref = "harness_evidence:" + canonical_hash(
            {
                "evidence_scope_ref": evidence_scope_ref,
                "sequence": sequence,
                "summary": summary,
            }
        )
        summaries.append(
            {"event_ref": event_ref, "sequence": sequence, **summary}
        )
        for capability in capabilities:
            observed[capability].append(event_ref)
        if family == "claude":
            for block in _claude_content_blocks(event):
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_use_id = block.get("id")
                    name = block.get("name")
                    if isinstance(tool_use_id, str) and isinstance(name, str):
                        claude_pending_tools[tool_use_id] = (name, event_ref)
                elif block_type == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    pending = (
                        claude_pending_tools.pop(tool_use_id, None)
                        if isinstance(tool_use_id, str)
                        else None
                    )
                    if pending is None or block.get("is_error") is True:
                        continue
                    name, request_event_ref = pending
                    for capability in _capabilities_for_tool(name, name):
                        observed[capability].extend(
                            [request_event_ref, event_ref]
                        )
        if native_ref is not None:
            native_refs.add(native_ref)
            observed["native_session"].append(event_ref)
        terminal = terminal or is_terminal
    if len(summaries) >= 2 and terminal:
        observed["stream"].extend(
            [summaries[0]["event_ref"], summaries[-1]["event_ref"]]
        )
    if len(native_refs) != 1:
        raise HarnessAdapterUnavailable("native_session_identity_unavailable")
    native_session_ref = next(iter(native_refs))
    if (
        expected_native_session_ref is not None
        and native_session_ref != expected_native_session_ref
    ):
        raise HarnessAdapterUnavailable("native_session_identity_changed")
    observed["subagent"].extend(
        _verified_subagent_evidence_refs(
            family,
            events,
            evidence_refs_by_sequence={
                int(item["sequence"]): str(item["event_ref"])
                for item in summaries
            },
            root_session_ref=native_session_ref,
        )
    )
    capabilities = {
        name: (
            {
                "status": "available",
                "evidence_refs": list(dict.fromkeys(observed[name])),
            }
            if observed[name]
            else {
                "status": "capability_unavailable",
                "reason": {"code": "probe_evidence_missing"},
                "evidence_refs": [],
            }
        )
        for name in HARNESS_CAPABILITIES
    }
    profile: dict[str, object] = {
        "schema_ref": "meta-research/harness-capability-profile/v1",
        "harness_family": family,
        "locked_version": (
            CODEX_LOCKED_VERSION if family == "codex" else CLAUDE_LOCKED_VERSION
        ),
        "provider_version": provider_version,
        "native_session_ref": native_session_ref,
        "capabilities": capabilities,
    }
    return HarnessTurnEvidence(
        native_session_ref=native_session_ref,
        profile=profile,
        evidence_events=tuple(summaries),
        stream_hash=canonical_hash(summaries),
        transport_receipt=transport_receipt,
    )


def _summarize_event(
    family: HarnessFamily, event: dict[str, object]
) -> tuple[dict[str, object] | None, set[str], str | None, bool]:
    if family == "codex":
        return _summarize_codex_event(event)
    return _summarize_claude_event(event)


def _summarize_codex_event(
    event: dict[str, object]
) -> tuple[dict[str, object] | None, set[str], str | None, bool]:
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None, set(), None, False
    capabilities: set[str] = set()
    native_ref = event.get("thread_id")
    if not isinstance(native_ref, str):
        native_ref = None
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    tool = item.get("tool") if isinstance(item, dict) else None
    server = item.get("server") if isinstance(item, dict) else None
    if event_type == "thread.started" and isinstance(event.get("tools"), list):
        capabilities.add("tool_inventory")
    if event_type == "item.completed":
        capabilities.update(
            _capabilities_for_tool(item_type, tool, server=server)
        )
    if (
        event_type == "item.completed"
        and item_type == "web_search"
        and isinstance(item, dict)
    ):
        capabilities.discard("web_search")
        capabilities.discard("web_fetch")
        action = item.get("action")
        action_type = action.get("type") if isinstance(action, dict) else None
        query = item.get("query")
        if action_type == "search":
            capabilities.add("web_search")
        elif action_type in {"open", "open_page", "fetch"} or (
            action_type == "other"
            and isinstance(query, str)
            and (not query or query.startswith(("http://", "https://")))
        ):
            capabilities.add("web_fetch")
    lifecycle = {
        "thread.forked": "fork",
        "turn.steered": "steer",
        "turn.interrupted": "interrupt",
        "thread.resumed": "resume",
    }
    if event_type in lifecycle:
        capabilities.add(lifecycle[event_type])
    summary: dict[str, object] = {"kind": event_type}
    if isinstance(item_type, str):
        summary["item_kind"] = item_type
    if isinstance(tool, str):
        summary["tool_kind"] = tool
    if isinstance(server, str):
        summary["server"] = server
    if native_ref is not None:
        summary["native_session_ref"] = native_ref
    return summary, capabilities, native_ref, event_type == "turn.completed"


def _summarize_claude_event(
    event: dict[str, object]
) -> tuple[dict[str, object] | None, set[str], str | None, bool]:
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None, set(), None, False
    subtype = event.get("subtype")
    native_ref = event.get("session_id")
    if not isinstance(native_ref, str):
        native_ref = None
    capabilities: set[str] = set()
    inventory_names: list[str] = []
    invoked_tool_names: list[str] = []
    if event_type == "system" and subtype == "init":
        tools = event.get("tools")
        if isinstance(tools, list):
            capabilities.add("tool_inventory")
            inventory_names.extend(item for item in tools if isinstance(item, str))
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    invoked_tool_names.append(name)
    lifecycle = {
        "fork": "fork",
        "steer": "steer",
        "interrupt": "interrupt",
        "resume": "resume",
    }
    if isinstance(subtype, str) and subtype in lifecycle:
        capabilities.add(lifecycle[subtype])
    if (
        event_type == "system"
        and subtype
        in {"hook_response", "hook_completed", "hook_execution_complete"}
        and event.get("is_error") is not True
        and event.get("status") not in {"error", "failed", "cancelled"}
    ):
        capabilities.add("hook")
    summary: dict[str, object] = {"kind": event_type}
    if isinstance(subtype, str):
        summary["subtype"] = subtype
    if inventory_names:
        summary["inventory_kinds"] = sorted(set(inventory_names))
    if invoked_tool_names:
        summary["tool_kinds"] = sorted(set(invoked_tool_names))
    result_ids = [
        block["tool_use_id"]
        for block in _claude_content_blocks(event)
        if block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
    ]
    if result_ids:
        summary["tool_result_ids"] = sorted(set(result_ids))
    if native_ref is not None:
        summary["native_session_ref"] = native_ref
    is_terminal = event_type == "result" and event.get("is_error") is False
    return summary, capabilities, native_ref, is_terminal


def _claude_content_blocks(
    event: dict[str, object]
) -> list[dict[str, object]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _verified_subagent_evidence_refs(
    family: HarnessFamily,
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
) -> tuple[str, ...]:
    if family == "codex":
        return _verified_codex_subagent_evidence_refs(
            events,
            evidence_refs_by_sequence=evidence_refs_by_sequence,
            root_session_ref=root_session_ref,
        )
    return _verified_claude_subagent_evidence_refs(
        events,
        evidence_refs_by_sequence=evidence_refs_by_sequence,
        root_session_ref=root_session_ref,
    )


def _verified_codex_subagent_evidence_refs(
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
) -> tuple[str, ...]:
    spawn_calls: list[tuple[int, dict[str, object]]] = []
    wait_calls: list[tuple[int, dict[str, object]]] = []
    for sequence, event in enumerate(events, start=1):
        item = event.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
        ):
            continue
        if item.get("tool") == "spawn_agent":
            spawn_calls.append((sequence, item))
        elif (
            item.get("tool") == "wait"
            and event.get("type") == "item.completed"
            and item.get("status") == "completed"
        ):
            wait_calls.append((sequence, item))
    if len(spawn_calls) != 1:
        return ()
    spawn_sequence, spawn = spawn_calls[0]
    receivers = spawn.get("receiver_thread_ids")
    states = spawn.get("agents_states")
    if (
        events[spawn_sequence - 1].get("type") != "item.completed"
        or spawn.get("status") != "completed"
        or spawn.get("sender_thread_id") != root_session_ref
        or not isinstance(receivers, list)
        or len(receivers) != 1
        or not isinstance(receivers[0], str)
        or not receivers[0]
        or receivers[0] == root_session_ref
        or not isinstance(states, dict)
        or not isinstance(states.get(receivers[0]), dict)
    ):
        return ()
    child_ref = receivers[0]
    terminal_wait_sequence: int | None = None
    for sequence, wait in wait_calls:
        wait_receivers = wait.get("receiver_thread_ids")
        wait_states = wait.get("agents_states")
        if wait_receivers == [] and wait_states == {}:
            continue
        if (
            sequence <= spawn_sequence
            or wait.get("sender_thread_id") != root_session_ref
            or wait_receivers != [child_ref]
            or not isinstance(wait_states, dict)
            or not isinstance(wait_states.get(child_ref), dict)
        ):
            return ()
        if wait_states[child_ref].get("status") == "completed":
            terminal_wait_sequence = sequence
    if terminal_wait_sequence is None:
        return ()
    refs = (
        evidence_refs_by_sequence.get(spawn_sequence),
        evidence_refs_by_sequence.get(terminal_wait_sequence),
    )
    return tuple(ref for ref in refs if ref is not None)


def _verified_claude_subagent_evidence_refs(
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
) -> tuple[str, ...]:
    requests: list[tuple[int, str]] = []
    results: list[tuple[int, dict[str, object]]] = []
    for sequence, event in enumerate(events, start=1):
        for block in _claude_content_blocks(event):
            if block.get("type") == "tool_use" and str(
                block.get("name", "")
            ).lower() in {"agent", "task", "subagent"}:
                tool_use_id = block.get("id")
                if (
                    event.get("session_id") != root_session_ref
                    or not isinstance(tool_use_id, str)
                    or not tool_use_id
                ):
                    return ()
                requests.append((sequence, tool_use_id))
            elif block.get("type") == "tool_result":
                results.append((sequence, block))
    if len(requests) != 1:
        return ()
    request_sequence, tool_use_id = requests[0]
    matching = [
        (sequence, block)
        for sequence, block in results
        if block.get("tool_use_id") == tool_use_id
    ]
    if len(matching) != 1:
        return ()
    result_sequence, result = matching[0]
    child_ref = result.get("agent_id") or result.get("child_agent_ref")
    if (
        result_sequence <= request_sequence
        or result.get("is_error") is True
        or result.get("status") != "completed"
        or result.get("parent_session_id") != root_session_ref
        or not isinstance(child_ref, str)
        or not child_ref
        or child_ref == root_session_ref
    ):
        return ()
    refs = (
        evidence_refs_by_sequence.get(request_sequence),
        evidence_refs_by_sequence.get(result_sequence),
    )
    return tuple(ref for ref in refs if ref is not None)


def _capabilities_for_tool(
    primary: object,
    secondary: object,
    *,
    server: object = None,
) -> set[str]:
    first = primary.lower() if isinstance(primary, str) else ""
    second = secondary.lower() if isinstance(secondary, str) else ""
    server_name = server.lower() if isinstance(server, str) else ""
    capabilities: set[str] = set()
    if first in {"command_execution", "bash", "shell"} or second in {
        "bash",
        "shell",
    }:
        capabilities.add("shell")
    if first in {"file_change", "read", "write", "edit"} or second in {
        "read",
        "write",
        "edit",
    }:
        capabilities.add("file_access")
    if (
        first == "mcp_tool_call" and server_name == "meta_research"
    ) or first.startswith("mcp__meta_research__") or second.startswith(
        "mcp__meta_research__"
    ):
        capabilities.add("semantic_mcp")
    if first == "skill" or second == "skill":
        capabilities.add("skill")
    if (
        first == "plugin"
        or second == "plugin"
        or first.startswith("plugin__")
        or second.startswith("plugin__")
    ):
        capabilities.add("plugin")
    if first == "hook" or second == "hook":
        capabilities.add("hook")
    if first in {"web_search", "websearch"} or second in {
        "web_search",
        "websearch",
    }:
        capabilities.add("web_search")
    if first in {"web_fetch", "webfetch"} or second in {
        "web_fetch",
        "webfetch",
    }:
        capabilities.add("web_fetch")
    return capabilities


def _looks_like_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in ("unauthorized", "authentication", "login required", "401")
    )


def _stream_has_auth_failure(stdout: str) -> bool:
    for event in _parse_jsonl(stdout):
        if event.get("type") == "meta_research.provider_error":
            if event.get("error_kind") == "auth_revoked":
                return True
            continue
        if event.get("type") not in {"system", "result", "assistant"}:
            continue
        if event.get("error_status") == 401 or event.get("api_error_status") == 401:
            return True
        if event.get("error") == "authentication_failed":
            return True
    return False


def _loopback_no_proxy() -> str:
    configured = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    values = [item.strip() for item in configured.split(",") if item.strip()]
    for loopback in ("127.0.0.1", "localhost", "::1"):
        if loopback not in values:
            values.append(loopback)
    return ",".join(values)


def _write_private(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_private(path: Path, value: str) -> None:
    if path.exists():
        try:
            persisted = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise OSError("provider operation spool invalid") from error
        if persisted != value:
            raise OSError("provider operation identity conflict")
        return
    _write_private(path, value)
