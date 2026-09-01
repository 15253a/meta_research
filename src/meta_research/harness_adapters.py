from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast

from meta_research.codex_runtime import CODEX_REASONING_EFFORT_CONFIG
from meta_research.codex_ledger import CodexHomeLedgerReader
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.owners.secret_detection import contains_secret
from meta_research.provider_supervisor import (
    PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS,
    ProviderSupervisorError,
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ensure_transport_key,
    read_verified_exit_receipt,
    write_supervisor_request,
)
from meta_research.root_capabilities import (
    CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS,
    ROOT_CAPABILITY_FLOOR,
    RootAgentKind,
    RootCapabilityEntryPath,
    capabilities_from_codex_feature_inventory,
    codex_feature_inventory_evidence_ref,
    parse_codex_feature_inventory,
    root_capability_profile,
)
from meta_research.quest_drafting import (
    _CancellableProcessRunner,
    _ProcessStopped,
)
from meta_research.target_run_runtime_contract import (
    TARGET_COMPLETION_BINDING_SCHEMA,
)
from meta_research.target_raw_output import (
    TargetRawOutputStore,
    TargetRawOutputUnavailable,
)


LOGGER = logging.getLogger(__name__)


HarnessFamily = Literal["codex", "claude"]
HarnessProcessRunner = Callable[
    [list[str], str, float | None, dict[str, str]],
    subprocess.CompletedProcess[str],
]
HarnessEventSink = Callable[
    [str, tuple[dict[str, object], ...]], None
]


class CodexChildLedgerReader(Protocol):
    """Read the public, append-only event ledger for one native child thread.

    The reader deliberately returns parsed event envelopes only.  The adapter
    consumes session metadata, the injected Skill envelope, and task-complete
    output; it never reads model reasoning.
    """

    def read(self, child_session_ref: str) -> tuple[dict[str, object], ...]: ...

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str:
        """Return the trusted package's SHA-256 after exact body binding."""

        ...

CODEX_LOCKED_VERSION = "0.147.0"
CLAUDE_LOCKED_VERSION = "2.1.220"
_MCP_TOKEN_ENV = "META_RESEARCH_MCP_TOKEN"
_HARNESS_FAMILY_ENV = "META_RESEARCH_HARNESS_FAMILY"
_HARNESS_WORKSPACE_ENV = "META_RESEARCH_HARNESS_WORKSPACE"
_PROVIDER_OPERATION_ENV = "META_RESEARCH_PROVIDER_OPERATION_REF"
_HARNESS_EVIDENCE_SCOPE_ENV = "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF"
_HARNESS_OBSERVATION_SCOPE_ENV = "META_RESEARCH_HARNESS_OBSERVATION_SCOPE"
HARNESS_PROVIDER_STREAM_MAX_BYTES = 64 * 1024 * 1024
HARNESS_PROVIDER_RESULT_MAX_BYTES = 16 * 1024 * 1024
_STREAM_LIMIT = HARNESS_PROVIDER_STREAM_MAX_BYTES
_RESULT_LIMIT = HARNESS_PROVIDER_RESULT_MAX_BYTES
_CHILD_PROMPT_LIMIT = HARNESS_PROVIDER_STREAM_MAX_BYTES
_TARGET_ROOT_OUTPUT_CHUNK_LIMIT = 8 * 1024
_TARGET_ROOT_OUTPUT_TOTAL_LIMIT = 1024 * 1024
_TARGET_ROOT_OUTPUT_PENDING_LIMIT = _TARGET_ROOT_OUTPUT_CHUNK_LIMIT
# A private provider spool is capped at ``_STREAM_LIMIT``.  Keeping no more
# command identities than the smallest possible JSONL envelopes in that spool
# makes the in-process cumulative index bounded even for adversarial ids.
_TARGET_ROOT_COMMAND_STATE_LIMIT = max(256, _STREAM_LIMIT // 256)
# After the initial display budget, exact redacted tails remain observable at
# a bounded cadence instead of making a long-running turn permanently silent.
_TARGET_ROOT_OUTPUT_SAMPLE_BYTES = 256 * 1024
_TARGET_ROOT_OUTPUT_SAMPLE_EVENTS = 64
_TARGET_ROOT_EVENT_BATCH_LIMIT = 64
TARGET_ROOT_DEFAULT_TIMEOUT_SECONDS: float | None = None
_CODEX_ITEM_ACTOR_CONFLICT = object()
# The private spool is capped at 64 MiB, so no raw JSONL stream can reach this
# sequence range. Observation-only rows can share the Harness ledger without
# colliding with formal evidence stored at raw sequences.
_TARGET_ROOT_OBSERVATION_SEQUENCE_BASE = 1_000_000_000
_TARGET_CANDIDATE_READY_SCHEMA = (
    "meta-research/target-candidate-ready-evidence/v1"
)
_TARGET_SELF_CHECK_SCHEMA = "meta-research/target-self-check-evidence/v1"
_TARGET_RESULT_REVIEW_REQUEST_SCHEMA = (
    "meta-research/target-result-review-request/v1"
)
_TARGET_REVIEW_EVIDENCE_SCHEMA = "meta-research/target-review-evidence/v1"
HARNESS_CAPABILITIES = ROOT_CAPABILITY_FLOOR


class HarnessAdapterUnavailable(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        durable_outcome: Literal["terminal", "unknown"] = "terminal",
        transport_receipt: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.durable_outcome = durable_outcome
        self.transport_receipt = transport_receipt


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
    target_workspace_ref: str | None = None
    working_directory: str | None = None
    provider_operation_timeout_seconds: float | None = None
    root_kind: RootAgentKind | None = None
    entry_path: RootCapabilityEntryPath = "initial"
    authorized_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessTurnEvidence:
    native_session_ref: str
    profile: dict[str, object]
    evidence_events: tuple[dict[str, object], ...]
    stream_hash: str
    transport_receipt: dict[str, object] | None = None


def _harness_evidence_scope_ref(invocation: HarnessInvocation) -> str:
    return canonical_hash(
        {
            "run_ref": invocation.run_ref,
            "provider_operation_ref": invocation.provider_operation_ref,
            "attempt_ref": invocation.attempt_ref,
            "fence_ref": invocation.fence_ref,
            "native_session_ref": invocation.native_session_ref,
            "target_workspace_ref": invocation.target_workspace_ref,
            "prompt_hash": canonical_hash(invocation.prompt),
        }
    )


def _target_root_observation_scope(
    invocation: HarnessInvocation,
) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-root-observation-scope/v2",
        "target_run_ref": invocation.run_ref,
        "attempt_ref": invocation.attempt_ref,
        "attempt_generation": invocation.attempt_generation,
        "root_session_ref": invocation.root_session_ref,
        "fence_ref": invocation.fence_ref,
        "native_session_ref": invocation.native_session_ref,
        "target_workspace_ref": invocation.target_workspace_ref,
    }


def _target_root_observation_scope_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    base_fields = {
        "schema_ref",
        "target_run_ref",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "fence_ref",
        "native_session_ref",
    }
    schema_ref = value.get("schema_ref")
    if (
        schema_ref == "meta-research/target-root-observation-scope/v1"
        and set(value) != base_fields
    ) or (
        schema_ref == "meta-research/target-root-observation-scope/v2"
        and set(value) != base_fields | {"target_workspace_ref"}
    ) or schema_ref not in {
        "meta-research/target-root-observation-scope/v1",
        "meta-research/target-root-observation-scope/v2",
    }:
        return False
    generation = value.get("attempt_generation")
    native_session_ref = value.get("native_session_ref")
    workspace_ref = value.get("target_workspace_ref")
    return (
        all(
            isinstance(value.get(field), str) and bool(value[field])
            for field in (
                "target_run_ref",
                "attempt_ref",
                "root_session_ref",
                "fence_ref",
            )
        )
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 1
        and (
            native_session_ref is None
            or (isinstance(native_session_ref, str) and bool(native_session_ref))
        )
        and (
            schema_ref == "meta-research/target-root-observation-scope/v1"
            or workspace_ref is None
            or (isinstance(workspace_ref, str) and bool(workspace_ref))
        )
    )


class HarnessAdapter(Protocol):
    family: HarnessFamily
    locked_version: str

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence: ...

    def installation_profile(self) -> dict[str, object]: ...

    def provider_operation_timeout_seconds(
        self, *, target_root: bool
    ) -> float | None: ...


class _NativeCliHarnessAdapter:
    family: HarnessFamily
    executable: str
    locked_version: str

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str | None = None,
        runner: HarnessProcessRunner | None = None,
        timeout_seconds: float | None = None,
        target_root_timeout_seconds: float | None = (
            TARGET_ROOT_DEFAULT_TIMEOUT_SECONDS
        ),
        codex_child_ledger_reader: CodexChildLedgerReader | None = None,
        codex_home: Path | None = None,
    ) -> None:
        if any(
            value is not None
            and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0
                < float(value)
                <= PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS
            )
            for value in (timeout_seconds, target_root_timeout_seconds)
        ):
            raise ValueError("harness_timeout_invalid")
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        if executable is not None:
            self.executable = executable
        self._runner = runner or HarnessSupervisorTransport(
            workspace / "shared-provider-supervisor"
        )
        self._timeout_seconds = timeout_seconds
        self._target_root_timeout_seconds = target_root_timeout_seconds
        self._cached_installation_profile: dict[str, object] | None = None
        self._diagnostic_incarnation_ref: str | None = None
        self._codex_child_ledger_reader = codex_child_ledger_reader
        if self.family == "codex" and codex_child_ledger_reader is None:
            configured_codex_home = codex_home or (
                Path(value) if (value := os.environ.get("CODEX_HOME")) else None
            )
            if configured_codex_home is not None:
                self._codex_child_ledger_reader = CodexHomeLedgerReader(
                    configured_codex_home
                )

    def provider_operation_timeout_seconds(
        self, *, target_root: bool
    ) -> float | None:
        """Return an exceptional ceiling, or ``None`` for normal execution."""

        return (
            self._target_root_timeout_seconds
            if target_root
            else self._timeout_seconds
        )

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
        self._validate_invocation(invocation)
        provider_version = self._provider_version()
        self._record_provider_capability(provider_version)
        provider_feature_inventory = self._provider_feature_inventory(
            invocation,
            provider_version=provider_version,
        )
        argv = self._argv(invocation)
        evidence_scope_ref = _harness_evidence_scope_ref(invocation)
        observation_scope = _target_root_observation_scope(invocation)
        timeout_seconds = invocation.provider_operation_timeout_seconds
        environment = {
            _MCP_TOKEN_ENV: invocation.mcp_token,
            _HARNESS_FAMILY_ENV: self.family,
            _HARNESS_WORKSPACE_ENV: (
                invocation.working_directory
                if invocation.working_directory is not None
                else str(self._workspace.resolve())
            ),
            _PROVIDER_OPERATION_ENV: invocation.provider_operation_ref,
            _HARNESS_EVIDENCE_SCOPE_ENV: evidence_scope_ref,
            _HARNESS_OBSERVATION_SCOPE_ENV: canonical_json(observation_scope),
            "NO_PROXY": _loopback_no_proxy(),
            "no_proxy": _loopback_no_proxy(),
        }
        try:
            completed = self._runner(
                argv,
                invocation.prompt,
                timeout_seconds,
                environment,
            )
        except FileNotFoundError as error:
            raise HarnessAdapterUnavailable("provider_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise HarnessAdapterUnavailable(
                "provider_timeout", durable_outcome="unknown"
            ) from error
        except HarnessRunnerOutcomeUnknown as error:
            raise HarnessAdapterUnavailable(
                "provider_outcome_unknown", durable_outcome="unknown"
            ) from error
        except OSError as error:
            raise HarnessAdapterUnavailable(
                "provider_io_unavailable", durable_outcome="unknown"
            ) from error
        if completed.returncode != 0:
            receipt = getattr(completed, "meta_research_transport_receipt", None)
            reason = (
                receipt.get("termination_reason")
                if isinstance(receipt, dict)
                else None
            )
            signed_code = {
                "timeout": "provider_timeout",
                "output_limit": "provider_output_limit",
                "descendant_process": "provider_descendant_process",
                "stopped": "provider_stopped",
                "launch_failed": "provider_launch_failed",
            }.get(reason)
            code = signed_code or (
                "provider_auth_revoked"
                if _looks_like_auth_failure(completed.stderr)
                or _stream_has_auth_failure(completed.stdout)
                else "provider_failed"
            )
            raise HarnessAdapterUnavailable(
                code,
                transport_receipt=(
                    cast(dict[str, object], receipt)
                    if signed_code is not None
                    else None
                ),
            )
        events = _parse_jsonl(completed.stdout)
        if not events:
            raise HarnessAdapterUnavailable("provider_stream_invalid")
        evidence = _evidence_from_events(
            family=self.family,
            provider_version=provider_version,
            events=events,
            expected_native_session_ref=invocation.native_session_ref,
            evidence_scope_ref=evidence_scope_ref,
            observation_scope=observation_scope,
            transport_receipt=getattr(
                completed, "meta_research_transport_receipt", None
            ),
            codex_child_ledger_reader=self._codex_child_ledger_reader,
            expected_working_directory=(
                invocation.working_directory
                if invocation.working_directory is not None
                else str(self._workspace.resolve())
            ),
        )
        root_diagnostics: dict[str, object] | None = None
        if invocation.root_kind is not None:
            capabilities = evidence.profile.get("capabilities")
            if not isinstance(capabilities, dict):
                raise HarnessAdapterUnavailable("harness_profile_invalid")
            used_capabilities: list[str] = []
            usage_evidence_refs: dict[str, tuple[str, ...]] = {}
            for capability in ROOT_CAPABILITY_FLOOR:
                item = capabilities.get(capability)
                if not isinstance(item, dict) or item.get("status") != "available":
                    continue
                refs = tuple(
                    ref
                    for ref in item.get("evidence_refs", [])
                    if isinstance(ref, str) and ref
                )
                used_capabilities.append(capability)
                usage_evidence_refs[capability] = refs
            inventory_names: list[str] = []
            inventory_evidence_refs: list[str] = []
            for event in evidence.evidence_events:
                names = event.get("inventory_kinds")
                if not isinstance(names, list):
                    continue
                event_ref = event.get("event_ref")
                if isinstance(event_ref, str) and event_ref:
                    inventory_evidence_refs.append(event_ref)
                for name in names:
                    if (
                        isinstance(name, str)
                        and name
                        and name not in inventory_names
                    ):
                        inventory_names.append(name)
            inventory_capabilities = _capabilities_from_inventory(
                tuple(inventory_names)
            )
            available_capabilities = set(used_capabilities)
            available_capabilities.update(inventory_capabilities)
            feature_inventory = (
                {}
                if provider_feature_inventory is None
                else provider_feature_inventory[0]
            )
            (
                feature_capabilities,
                feature_unavailable_capabilities,
            ) = capabilities_from_codex_feature_inventory(feature_inventory)
            available_capabilities.update(feature_capabilities)
            if self.family == "codex":
                # A completed turn could not have started unless the configured
                # required MCP server initialized successfully. This is a
                # provider startup fact, not evidence that any operation ran.
                available_capabilities.add("semantic_mcp")
            if invocation.entry_path != "initial":
                # A successful native resume-family invocation is direct
                # provider evidence for the resume control, regardless of
                # whether the provider emits a separate lifecycle event.
                available_capabilities.add("resume")
            inventory_bound = {
                "shell",
                "file_access",
                "semantic_mcp",
                "skill",
                "plugin",
                "hook",
                "subagent",
                "web_search",
                "web_fetch",
            }
            unavailable_capabilities = {
                **feature_unavailable_capabilities,
                **(
                    {
                        capability: "harness_tool_inventory_missing"
                        for capability in sorted(
                            inventory_bound - available_capabilities
                        )
                    }
                    if inventory_evidence_refs
                    else {}
                ),
            }
            unavailable_capabilities = {
                capability: code
                for capability, code in unavailable_capabilities.items()
                if capability not in available_capabilities
            }
            root_diagnostics = root_capability_profile(
                invocation.root_kind
            ).public_diagnostics(
                entry_path=invocation.entry_path,
                available_capabilities=tuple(
                    capability
                    for capability in ROOT_CAPABILITY_FLOOR
                    if capability in available_capabilities
                ),
                used_capabilities=tuple(used_capabilities),
                authorized_operation_ids=invocation.authorized_operation_ids,
                unavailable_capabilities=unavailable_capabilities,
                usage_evidence_refs=usage_evidence_refs,
                tool_inventory_evidence_refs=tuple(
                    dict.fromkeys(inventory_evidence_refs)
                ),
                tool_inventory_names=tuple(inventory_names),
                provider_feature_inventory=feature_inventory,
                provider_feature_inventory_evidence_refs=(
                    ()
                    if provider_feature_inventory is None
                    else (provider_feature_inventory[1],)
                ),
            )
        return replace(
            evidence,
            profile={
                **evidence.profile,
                "sandbox_mode": self._sandbox_mode(invocation),
                **(
                    {}
                    if root_diagnostics is None
                    else {"root_capability_diagnostics": root_diagnostics}
                ),
            },
        )

    def installation_profile(self) -> dict[str, object]:
        path = self._provider_capability_path()
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
                raise HarnessAdapterUnavailable("provider_capability_unverified")
            encoded = path.read_text(encoding="utf-8")
            document = json.loads(encoded)
            if (
                not isinstance(document, dict)
                or canonical_json(document) != encoded
            ):
                raise HarnessAdapterUnavailable("provider_capability_invalid")
            capability_hash = document.get("capability_hash")
            material = {
                key: value
                for key, value in document.items()
                if key != "capability_hash"
            }
            if (
                document.get("schema_ref")
                != "meta-research/harness-provider-diagnostic/v2"
                or document.get("harness_family") != self.family
                or document.get("locked_version") != self.locked_version
                or not isinstance(document.get("daemon_incarnation_ref"), str)
                or not document["daemon_incarnation_ref"]
                or not isinstance(document.get("probed_at"), (int, float))
                or isinstance(document.get("probed_at"), bool)
                or capability_hash != canonical_hash(material)
                or (
                    self._diagnostic_incarnation_ref is not None
                    and document["daemon_incarnation_ref"]
                    != self._diagnostic_incarnation_ref
                )
            ):
                raise HarnessAdapterUnavailable("provider_capability_invalid")
            status = document.get("status")
            if status == "capability_unavailable":
                reason = document.get("reason")
                if (
                    set(document)
                    != {
                        "schema_ref",
                        "harness_family",
                        "locked_version",
                        "daemon_incarnation_ref",
                        "status",
                        "reason",
                        "probed_at",
                        "capability_hash",
                    }
                    or not isinstance(reason, dict)
                    or set(reason) != {"code"}
                    or not isinstance(reason.get("code"), str)
                    or not reason["code"]
                ):
                    raise HarnessAdapterUnavailable("provider_capability_invalid")
                return {
                    "harness_family": self.family,
                    "locked_version": self.locked_version,
                    "status": "capability_unavailable",
                    "reason": {"code": str(reason["code"])},
                }
            if (
                status != "ready"
                or set(document)
                != {
                    "schema_ref",
                    "harness_family",
                    "locked_version",
                    "provider_version",
                    "daemon_incarnation_ref",
                    "executable_identity",
                    "status",
                    "probed_at",
                    "capability_hash",
                }
                or document.get("provider_version") != self.locked_version
                or not isinstance(document.get("executable_identity"), dict)
            ):
                raise HarnessAdapterUnavailable("provider_capability_invalid")
            if document["executable_identity"] != self._executable_identity():
                raise HarnessAdapterUnavailable("provider_executable_changed")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HarnessAdapterUnavailable("provider_capability_invalid") from error
        except HarnessAdapterUnavailable as error:
            return {
                "harness_family": self.family,
                "locked_version": self.locked_version,
                "status": "capability_unavailable",
                "reason": {"code": error.code},
            }
        return {
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": self.locked_version,
            "status": "ready",
        }

    def _provider_capability_path(self) -> Path:
        return self._workspace / "installation-capabilities" / f"{self.family}.json"

    def prepare_installation_diagnostic(self, daemon_incarnation_ref: str) -> None:
        if (
            not isinstance(daemon_incarnation_ref, str)
            or not daemon_incarnation_ref
            or len(daemon_incarnation_ref) > 128
        ):
            raise HarnessAdapterUnavailable("provider_diagnostic_identity_invalid")
        self._diagnostic_incarnation_ref = daemon_incarnation_ref

    def run_installation_diagnostic(self, daemon_incarnation_ref: str) -> str:
        self.prepare_installation_diagnostic(daemon_incarnation_ref)
        before = self._executable_identity()
        observed_version = self._provider_version()
        after = self._executable_identity()
        if before != after:
            raise HarnessAdapterUnavailable("provider_executable_changed")
        return self._record_provider_capability(
            observed_version,
            executable_identity=after,
        )

    def record_installation_diagnostic_failure(
        self,
        daemon_incarnation_ref: str,
        code: str,
    ) -> str:
        self.prepare_installation_diagnostic(daemon_incarnation_ref)
        if not isinstance(code, str) or not code or len(code) > 96:
            code = "provider_diagnostic_failed"
        material = {
            "schema_ref": "meta-research/harness-provider-diagnostic/v2",
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "daemon_incarnation_ref": daemon_incarnation_ref,
            "status": "capability_unavailable",
            "reason": {"code": code},
            "probed_at": time.time(),
        }
        return self._write_provider_diagnostic(material)

    def _record_provider_capability(
        self,
        observed_version: str,
        *,
        executable_identity: dict[str, object] | None = None,
    ) -> str:
        daemon_incarnation_ref = (
            self._diagnostic_incarnation_ref or "protected_harness_invocation"
        )
        material = {
            "schema_ref": "meta-research/harness-provider-diagnostic/v2",
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": observed_version,
            "daemon_incarnation_ref": daemon_incarnation_ref,
            "executable_identity": (
                self._executable_identity()
                if executable_identity is None
                else executable_identity
            ),
            "status": "ready",
            "probed_at": time.time(),
        }
        return self._write_provider_diagnostic(material)

    def _write_provider_diagnostic(self, material: dict[str, object]) -> str:
        capability_hash = canonical_hash(material)
        document = {**material, "capability_hash": capability_hash}
        path = self._provider_capability_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = canonical_json(document)
        try:
            _write_private(path, encoded)
        except OSError as error:
            raise HarnessAdapterUnavailable(
                "provider_capability_unavailable"
            ) from error
        return capability_hash

    def _executable_identity(self) -> dict[str, object]:
        configured = Path(self.executable)
        resolved_value = (
            str(configured)
            if configured.is_absolute() or configured.parent != Path(".")
            else shutil.which(self.executable)
        )
        if resolved_value is None:
            # Injected runners are intentionally usable in narrow adapter tests;
            # the real supervisor still fails the subsequent version probe when
            # a production command is absent.
            return {"kind": "unresolved_command", "command": self.executable}
        path = Path(resolved_value)
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except OSError as error:
            raise HarnessAdapterUnavailable("provider_executable_changed") from error
        if not resolved.is_file():
            raise HarnessAdapterUnavailable("provider_executable_changed")
        return {
            "kind": "file",
            "command": self.executable,
            "resolved_path": str(resolved),
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }

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

    def _provider_feature_inventory(
        self,
        invocation: HarnessInvocation,
        *,
        provider_version: str,
    ) -> tuple[dict[str, bool], str] | None:
        """Read Codex's effective feature flags without inventing tool JSONL."""

        if self.family != "codex" or invocation.root_kind is None:
            return None
        profile = root_capability_profile(invocation.root_kind)
        argv = [
            self.executable,
            *profile.codex_arguments(entry_path=invocation.entry_path),
            "features",
            "list",
        ]
        try:
            completed = self._runner(
                argv,
                "",
                CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS,
                {},
            )
        except Exception:
            LOGGER.warning(
                "Codex feature inventory probe unavailable",
                exc_info=True,
                extra={"root_kind": invocation.root_kind},
            )
            return None
        if completed.returncode != 0 or len(completed.stdout) > 64 * 1024:
            return None
        features = parse_codex_feature_inventory(completed.stdout)
        if features is None:
            return None
        evidence_ref = codex_feature_inventory_evidence_ref(
            profile=profile,
            entry_path=invocation.entry_path,
            provider_version=provider_version,
            features=features,
        )
        return features, evidence_ref

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
            or (
                invocation.provider_operation_timeout_seconds is not None
                and (
                    not isinstance(
                        invocation.provider_operation_timeout_seconds,
                        (int, float),
                    )
                    or isinstance(
                        invocation.provider_operation_timeout_seconds, bool
                    )
                    or not math.isfinite(
                        float(invocation.provider_operation_timeout_seconds)
                    )
                    or not 0
                    < float(invocation.provider_operation_timeout_seconds)
                    <= PROVIDER_SUPERVISOR_MAX_TIMEOUT_SECONDS
                )
            )
            or (
                invocation.working_directory is not None
                and (
                    invocation.target_workspace_ref is None
                    or not Path(invocation.working_directory).is_absolute()
                    or not Path(invocation.working_directory).is_dir()
                    or Path(invocation.working_directory).is_symlink()
                )
            )
            or (
                invocation.target_workspace_ref is not None
                and invocation.working_directory is None
            )
        ):
            raise HarnessAdapterUnavailable("harness_invocation_invalid")

    def _argv(self, invocation: HarnessInvocation) -> list[str]:
        raise NotImplementedError

    def _sandbox_mode(self, invocation: HarnessInvocation) -> str:
        return "danger-full-access"


class CodexHarnessAdapter(_NativeCliHarnessAdapter):
    family = "codex"
    executable = "codex"
    locked_version = CODEX_LOCKED_VERSION

    def _sandbox_mode(self, invocation: HarnessInvocation) -> str:
        return (
            "workspace-write"
            if invocation.target_workspace_ref is not None
            else "danger-full-access"
        )

    def _argv(self, invocation: HarnessInvocation) -> list[str]:
        target_environment_arguments: tuple[str, ...] = ()
        if invocation.target_workspace_ref is not None:
            # HumanRequest effects remain attributed to this authenticated
            # Target operation. Native child Sessions intentionally share the
            # operation bearer, while shell subprocesses do not inherit it.
            target_environment_arguments = (
                "--config",
                'shell_environment_policy.inherit="none"',
            )
        capability_profile = root_capability_profile("target")
        argv = [
            self.executable,
            "exec",
            "--skip-git-repo-check",
            "--strict-config",
            *capability_profile.codex_arguments(),
            "--json",
            "--model",
            invocation.model_ref,
            "--config",
            'approval_policy="never"',
            "--config",
            CODEX_REASONING_EFFORT_CONFIG,
            "--config",
            "mcp_servers={}",
            *target_environment_arguments,
            "--sandbox",
            self._sandbox_mode(invocation),
            "--cd",
            (
                invocation.working_directory
                if invocation.working_directory is not None
                else str(self._workspace)
            ),
            "--config",
            f'mcp_servers.meta_research.url="{invocation.mcp_url}"',
            "--config",
            (
                "mcp_servers.meta_research.bearer_token_env_var="
                f'"{_MCP_TOKEN_ENV}"'
            ),
            "--config",
            "mcp_servers.meta_research.required=true",
            "--config",
            (
                "mcp_servers.meta_research."
                'default_tools_approval_mode="approve"'
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

class _HarnessStdoutEventTail:
    """Replay the private JSONL spool from byte zero while it is growing."""

    def __init__(
        self,
        *,
        stdout_path: Path,
        family: HarnessFamily,
        operation_ref: str,
        evidence_scope_ref: str,
        observation_scope: dict[str, object],
        sink: HarnessEventSink,
    ) -> None:
        self._stdout_path = stdout_path
        self._operation_ref = operation_ref
        self._evidence_scope_ref = evidence_scope_ref
        self._projector = _TargetRootEventProjector(
            family=family,
            observation_scope=observation_scope,
        )
        self._sink = sink
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="harness-target-root-tail",
            daemon=True,
        )
        self._offset = 0
        self._buffer = b""
        self._sequence = 0
        self.failure: BaseException | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive() and self.failure is None:
            self.failure = RuntimeError("harness stdout tail did not stop")

    def _run(self) -> None:
        try:
            while not self._stop.wait(0.02):
                self._drain(final=False)
            self._drain(final=True)
        except BaseException as error:  # surfaced by the transport thread
            self.failure = error

    def _drain(self, *, final: bool) -> None:
        if self._stdout_path.exists():
            size = self._stdout_path.stat().st_size
            if size < self._offset:
                raise OSError("harness stdout spool truncated")
            if size > self._offset:
                with self._stdout_path.open("rb") as stream:
                    stream.seek(self._offset)
                    chunk = stream.read(size - self._offset)
                self._offset += len(chunk)
                self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        if final and self._buffer:
            lines.append(self._buffer)
            self._buffer = b""
        projected: list[dict[str, object]] = []
        for line in lines:
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            self._sequence += 1
            _summary, _capabilities, _native_ref, _terminal, observation = (
                self._projector.summarize(cast(dict[str, object], event))
            )
            # Observation delivery does not wait for a terminal Harness item;
            # the final evidence pass still derives its own bounded summaries.
            if observation is None:
                continue
            observation_sequence = (
                _TARGET_ROOT_OBSERVATION_SEQUENCE_BASE + self._sequence
            )
            observation_summary = {
                "kind": "target_root_observation",
                "target_run_scope": observation["scope"],
                "target_root_observation": {
                    **observation,
                    "raw_sequence": self._sequence,
                },
            }
            event_value = {
                "event_ref": "harness_observation:"
                + canonical_hash(
                    {
                        "evidence_scope_ref": self._evidence_scope_ref,
                        "raw_sequence": self._sequence,
                        "summary": observation_summary,
                    }
                ),
                "sequence": observation_sequence,
                **observation_summary,
            }
            projected.append(event_value)
            if len(projected) == _TARGET_ROOT_EVENT_BATCH_LIMIT:
                self._sink(self._operation_ref, tuple(projected))
                projected.clear()
        if projected:
            self._sink(self._operation_ref, tuple(projected))


class HarnessSupervisorTransport:
    """Thin Harness adapter over its provider-specific process runner."""

    def __init__(
        self,
        workspace: Path,
        *,
        process_runner: _CancellableProcessRunner | None = None,
        event_sink: HarnessEventSink | None = None,
        raw_output_store: TargetRawOutputStore | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        _key_path, self._transport_key = ensure_transport_key(self._workspace)
        self._process_runner = process_runner or _CancellableProcessRunner()
        self._event_sink = event_sink
        self._raw_output_store = raw_output_store

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        if not prompt and (
            "--version" in argv or argv[-2:] == ["features", "list"]
        ):
            return self._process_runner(argv, prompt, timeout, environment)
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
        if self._raw_output_store is not None:
            try:
                self._raw_output_store.bind_operation(
                    operation_ref,
                    invocation_hash,
                    family=family,
                )
            except TargetRawOutputUnavailable:
                # Raw output is a private display mapping.  It can never turn
                # a real provider operation into a failed Target Run.
                pass
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
        tail = self._start_event_tail(
            stdout_path=stdout_path,
            family=family,
            operation_ref=operation_ref,
            environment=environment,
        )
        try:
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
                        stdout_max_bytes=_STREAM_LIMIT,
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
        finally:
            if tail is not None:
                tail.stop()
        if tail is not None and tail.failure is not None:
            raise OSError("harness event sink unavailable") from tail.failure
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

    def _start_event_tail(
        self,
        *,
        stdout_path: Path,
        family: HarnessFamily,
        operation_ref: str,
        environment: dict[str, str],
    ) -> _HarnessStdoutEventTail | None:
        if self._event_sink is None:
            return None
        evidence_scope_ref = environment.get(_HARNESS_EVIDENCE_SCOPE_ENV)
        scope_json = environment.get(_HARNESS_OBSERVATION_SCOPE_ENV)
        if (
            evidence_scope_ref is None
            or re.fullmatch(r"[0-9a-f]{64}", evidence_scope_ref) is None
            or scope_json is None
        ):
            raise OSError("harness observation scope unavailable")
        try:
            scope = json.loads(scope_json)
        except json.JSONDecodeError as error:
            raise OSError("harness observation scope invalid") from error
        if not _target_root_observation_scope_is_valid(scope):
            raise OSError("harness observation scope invalid")
        tail = _HarnessStdoutEventTail(
            stdout_path=stdout_path,
            family=family,
            operation_ref=operation_ref,
            evidence_scope_ref=evidence_scope_ref,
            observation_scope=cast(dict[str, object], scope),
            sink=self._event_sink,
        )
        tail.start()
        return tail


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


class _CodexHomeChildLedgerReader:
    """Read one child ledger only from the configured CODEX_HOME tree."""

    def __init__(self, codex_home: Path) -> None:
        if not codex_home.is_absolute() or codex_home.is_symlink():
            raise ValueError("codex home is not a trusted directory")
        self._codex_home = codex_home.resolve()

    def read(self, child_session_ref: str) -> tuple[dict[str, object], ...]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", child_session_ref):
            raise OSError("child session reference invalid")
        candidates: list[tuple[Path, tuple[dict[str, object], ...]]] = []
        for relative in ("sessions", "archived_sessions"):
            directory = self._codex_home / relative
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise OSError("codex ledger directory invalid")
            for path in directory.rglob(f"*{child_session_ref}*.jsonl"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self._codex_home)
                    records = _parse_jsonl(resolved.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                if _ledger_session_id(records) == child_session_ref:
                    candidates.append((resolved, records))
        if len(candidates) != 1:
            raise OSError("child ledger missing or ambiguous")
        return candidates[0][1]

    def verify_skill_package(self, skill_path: str, injected_body: str) -> str:
        path = Path(skill_path)
        if (
            not path.is_absolute()
            or not path.is_file()
            or not _is_non_symlink_codex_home_descendant(path, self._codex_home)
        ):
            raise OSError("child Skill package invalid")
        try:
            resolved = path.resolve(strict=True)
            package_bytes = resolved.read_bytes()
            package = package_bytes.decode("utf-8")
        except (OSError, ValueError) as error:
            raise OSError("child Skill package invalid") from error
        except UnicodeDecodeError as error:
            raise OSError("child Skill package invalid") from error
        if not any(
            candidate == package
            for candidate in _skill_body_without_wrapper_newline(injected_body)
        ):
            raise OSError("child Skill package content drift")
        return hashlib.sha256(package_bytes).hexdigest()


def _is_non_symlink_codex_home_descendant(path: Path, codex_home: Path) -> bool:
    """Reject a package routed through a symlink even when it resolves in home."""

    try:
        relative = path.relative_to(codex_home)
    except ValueError:
        return False
    current = codex_home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        path.resolve(strict=True).relative_to(codex_home)
    except (OSError, ValueError):
        return False
    return True


def _ledger_session_id(records: tuple[dict[str, object], ...]) -> str | None:
    metadata = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
    ]
    if len(metadata) != 1:
        return None
    value = metadata[0].get("id")
    return value if isinstance(value, str) else None


_ANSI_ESCAPE = re.compile(
    r"(?:\x1B\[[0-?]*[ -/]*[@-~]|\x1B\][^\x07]*(?:\x07|\x1B\\))"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|"
    r"secret|cookie|authorization)\b([ \t]*[:=][ \t]*)([^\s,;]*)"
)
_BEARER_SECRET = re.compile(
    r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]*"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:ghp_[A-Za-z0-9_]*|github_pat_[A-Za-z0-9_]*|"
    r"sk-[A-Za-z0-9_-]*|AKIA[0-9A-Z]*)"
)
_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]*PRIVATE KEY-----"
)
_PRIVATE_KEY_BEGIN = re.compile(r"(?i)-----BEGIN(?:[ \t]|$)")
_PRIVATE_KEY_END = re.compile(
    r"(?i)-----END [^-\r\n]*PRIVATE KEY-----"
)
_BEARER_OPENER = re.compile(r"(?i)\bBearer[ \t]+")
_SECRET_ASSIGNMENT_OPENER = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|"
    r"secret|cookie|authorization)\b([ \t]*[:=][ \t]*)"
)
_GENERIC_ASSIGNMENT_OPENER = re.compile(
    r"(?i)(?<![a-z0-9_.-])([a-z_][a-z0-9_.-]{0,255})"
    r"([ \t]*[:=][ \t]*)"
)
_OWNER_SECRET_TEXT_MARKERS = (
    "://",
    "password",
    "passwd",
    "passphrase",
    "cookie",
    "sessionid",
    "sid",
    "otp",
    "one_time",
    "one-time",
    "one time",
    "secret",
    "private",
    "api_",
    "api-",
    "api ",
    "access_",
    "access-",
    "access ",
    "refresh",
    "session_token",
    "id_token",
    "auth",
    "personal",
    "token",
    "credential",
    "bearer",
    "basic",
    "-----begin",
    "github_pat_",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "xox",
    "sk-",
    "akia",
    "aiza",
    "eyj",
)
@dataclass
class _TargetRootCommandOutputState:
    raw_char_count: int = 0
    raw_hash: str = hashlib.sha256(b"").hexdigest()
    pending: str = ""
    pending_redacted: bool = False
    ansi_pending: str = ""
    ansi_overflow_mode: Literal["csi", "osc", "escape"] | None = None
    ansi_osc_escape_pending: bool = False
    carriage_return_pending: bool = False
    mode: Literal["normal", "secret_line", "private_key", "overflow"] = (
        "normal"
    )


class _TargetRootEventProjector:
    """Derive a bounded display projection without weakening Harness evidence."""

    def __init__(
        self,
        *,
        family: HarnessFamily,
        observation_scope: dict[str, object],
    ) -> None:
        if not _target_root_observation_scope_is_valid(observation_scope):
            raise HarnessAdapterUnavailable("target_root_observation_scope_invalid")
        self._family = family
        self._scope = observation_scope
        native_ref = observation_scope.get("native_session_ref")
        self._root_native_session_ref = (
            native_ref if isinstance(native_ref, str) else None
        )
        self._claude_tools: dict[str, str] = {}
        self._codex_command_outputs: dict[
            str, _TargetRootCommandOutputState
        ] = {}
        self._codex_pending_bytes = 0
        self._codex_projection_saturated = False
        self._output_bytes = 0
        self._gap_bytes = 0
        self._gap_events = 0
        self._gap_tail = ""
        self._gap_redacted = False
        self._gap_emissions = 0

    def summarize(
        self, event: dict[str, object]
    ) -> tuple[
        dict[str, object] | None,
        set[str],
        str | None,
        bool,
        dict[str, object] | None,
    ]:
        summary, capabilities, native_ref, terminal = _summarize_event(
            self._family, event
        )
        self._observe_root_identity(event, native_ref)
        if summary is not None:
            summary["target_run_scope"] = self._scope
            root_message = self._root_agent_message(event)
            if root_message is not None:
                summary["actor_session_ref"] = self._root_native_session_ref
                summary["target_root_agent_message"] = True
                workspace_ref = self._scope.get("target_workspace_ref")
                try:
                    root_message_bytes = root_message.encode("utf-8")
                except UnicodeError:
                    summary["target_root_completion_binding_error"] = (
                        "target_root_completion_text_invalid"
                    )
                else:
                    if root_message_bytes and isinstance(workspace_ref, str):
                        summary["target_root_completion_binding"] = {
                            "schema_ref": TARGET_COMPLETION_BINDING_SCHEMA,
                            "final_text": root_message,
                            "final_text_sha256": hashlib.sha256(
                                root_message_bytes
                            ).hexdigest(),
                            "final_text_bytes": len(root_message_bytes),
                            "workspace_ref": workspace_ref,
                        }
            if terminal:
                summary["target_root_terminal"] = True
        output = self._root_output(event)
        observation: dict[str, object] | None = None
        if output is not None:
            output_text, command_ref = output
            overflow_bytes = 0
            if self._family == "codex" and command_ref is not None:
                text, redacted, overflow_bytes = (
                    self._consume_codex_command_output(
                        command_ref,
                        output_text,
                        final=event.get("type") == "item.completed",
                    )
                )
            else:
                ephemeral = _TargetRootCommandOutputState()
                text, redacted, overflow_bytes = (
                    _consume_target_root_output_delta(
                        ephemeral,
                        output_text,
                        final=True,
                    )
                )
            if overflow_bytes:
                self._remember_redacted_output_gap(overflow_bytes)
            remaining = max(
                0, _TARGET_ROOT_OUTPUT_TOTAL_LIMIT - self._output_bytes
            )
            if remaining > 0 and text:
                visible, chunk_truncated = _truncate_utf8(
                    text,
                    min(remaining, _TARGET_ROOT_OUTPUT_CHUNK_LIMIT),
                )
                self._output_bytes += len(_target_root_output_bytes(visible))
                observation = {
                    "schema_ref": "meta-research/target-root-observation/v1",
                    "scope": self._scope,
                    "root_native_session_ref": self._root_native_session_ref,
                    "kind": "command_output",
                    "stream": "stdout",
                    "text": visible,
                    "redacted": redacted,
                    "truncated": chunk_truncated,
                }
                if chunk_truncated:
                    self._remember_output_gap(
                        text[len(visible) :], redacted=redacted
                    )
            elif text:
                self._remember_output_gap(text, redacted=redacted)
                if self._output_gap_is_due():
                    observation = self._take_output_gap()
            elif overflow_bytes and self._output_gap_is_due():
                observation = self._take_output_gap()
        if terminal and observation is None and self._gap_events:
            observation = self._take_output_gap()
        return summary, capabilities, native_ref, terminal, observation

    def _consume_codex_command_output(
        self,
        command_ref: str,
        cumulative_raw: str,
        *,
        final: bool,
    ) -> tuple[str, bool, int]:
        """Project one cumulative command stream without retaining its raw body."""

        if self._codex_projection_saturated:
            return "", True, len(_target_root_output_bytes(cumulative_raw))
        state = self._codex_command_outputs.get(command_ref)
        if state is None:
            if (
                len(self._codex_command_outputs)
                >= _TARGET_ROOT_COMMAND_STATE_LIMIT
            ):
                self._saturate_codex_projection()
                return "", True, len(
                    _target_root_output_bytes(cumulative_raw)
                )
            state = _TargetRootCommandOutputState()
            self._codex_command_outputs[command_ref] = state

        previous_pending_bytes = len(
            _target_root_output_bytes(state.pending + state.ansi_pending)
        )
        if (
            len(cumulative_raw) < state.raw_char_count
            or hashlib.sha256(
                _target_root_output_bytes(
                    cumulative_raw[: state.raw_char_count]
                )
            ).hexdigest()
            != state.raw_hash
        ):
            # A cumulative output that rewrites already-observed bytes cannot
            # be differenced safely.  Fail closed for the remainder of this
            # projector instead of replaying a newly formed secret prefix.
            self._saturate_codex_projection()
            return "", True, len(_target_root_output_bytes(cumulative_raw))

        delta = cumulative_raw[state.raw_char_count :]
        state.raw_char_count = len(cumulative_raw)
        state.raw_hash = hashlib.sha256(
            _target_root_output_bytes(cumulative_raw)
        ).hexdigest()
        text, redacted, overflow_bytes = _consume_target_root_output_delta(
            state,
            delta,
            final=final,
        )
        current_pending_bytes = len(
            _target_root_output_bytes(state.pending + state.ansi_pending)
        )
        self._codex_pending_bytes += (
            current_pending_bytes - previous_pending_bytes
        )
        if (
            current_pending_bytes > _TARGET_ROOT_OUTPUT_PENDING_LIMIT
            or self._codex_pending_bytes > _STREAM_LIMIT
        ):
            self._saturate_codex_projection()
            return "", True, overflow_bytes + max(
                current_pending_bytes,
                len(_target_root_output_bytes(delta)),
            )
        return text, redacted, overflow_bytes

    def _saturate_codex_projection(self) -> None:
        self._codex_projection_saturated = True
        self._codex_command_outputs.clear()
        self._codex_pending_bytes = 0

    def _remember_output_gap(self, text: str, *, redacted: bool) -> None:
        if not text:
            return
        self._gap_bytes += len(_target_root_output_bytes(text))
        self._gap_events += 1
        self._gap_tail = _utf8_tail(text, _TARGET_ROOT_OUTPUT_CHUNK_LIMIT)
        self._gap_redacted = self._gap_redacted or redacted

    def _remember_redacted_output_gap(self, dropped_bytes: int) -> None:
        if dropped_bytes <= 0:
            return
        marker = "[REDACTED]"
        self._gap_bytes += dropped_bytes + len(
            _target_root_output_bytes(marker)
        )
        self._gap_events += 1
        self._gap_tail = marker
        self._gap_redacted = True

    def _output_gap_is_due(self) -> bool:
        return (
            self._gap_emissions == 0
            or self._gap_bytes >= _TARGET_ROOT_OUTPUT_SAMPLE_BYTES
            or self._gap_events >= _TARGET_ROOT_OUTPUT_SAMPLE_EVENTS
        )

    def _take_output_gap(self) -> dict[str, object]:
        tail_bytes = len(_target_root_output_bytes(self._gap_tail))
        observation = {
            "schema_ref": "meta-research/target-root-observation/v1",
            "scope": self._scope,
            "root_native_session_ref": self._root_native_session_ref,
            "kind": "output_gap",
            "stream": "stdout",
            # This is an exact redacted tail sample. Gap metadata is carried in
            # its own fields so no synthetic marker is mixed into stdout.
            "text": self._gap_tail,
            "redacted": self._gap_redacted,
            "truncated": True,
            "dropped_bytes": max(0, self._gap_bytes - tail_bytes),
            "dropped_events": self._gap_events,
        }
        self._gap_bytes = 0
        self._gap_events = 0
        self._gap_tail = ""
        self._gap_redacted = False
        self._gap_emissions += 1
        return observation

    def _root_agent_message(self, event: dict[str, object]) -> str | None:
        if self._root_native_session_ref is None:
            return None
        if self._family == "codex":
            item = event.get("item")
            actor_ref = (
                _codex_item_actor(event, item)
                if isinstance(item, dict)
                else None
            )
            if (
                event.get("type") != "item.completed"
                or not isinstance(item, dict)
                or item.get("type") != "agent_message"
                # Native Codex root item envelopes do not always repeat the
                # thread id learned from ``thread.started``.  An explicit
                # actor must still match, so child-ledger items fail closed.
                or actor_ref is _CODEX_ITEM_ACTOR_CONFLICT
                or (
                    actor_ref is not None
                    and actor_ref != self._root_native_session_ref
                )
            ):
                return None
            value = item.get("text") or item.get("content")
            return value if isinstance(value, str) else None
        if (
            event.get("type") != "assistant"
            or event.get("session_id") != self._root_native_session_ref
            or event.get("parent_tool_use_id") not in (None, "")
        ):
            return None
        text_blocks = [
            block["text"]
            for block in _claude_content_blocks(event)
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return str(text_blocks[0]) if len(text_blocks) == 1 else None

    def _observe_root_identity(
        self, event: dict[str, object], native_ref: str | None
    ) -> None:
        is_root_start = (
            self._family == "codex" and event.get("type") == "thread.started"
        ) or (
            self._family == "claude"
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        )
        if not is_root_start or native_ref is None:
            return
        if self._root_native_session_ref is None:
            self._root_native_session_ref = native_ref
        elif self._root_native_session_ref != native_ref:
            raise HarnessAdapterUnavailable("native_session_identity_changed")

    def _root_output(
        self, event: dict[str, object]
    ) -> tuple[str, str | None] | None:
        if self._root_native_session_ref is None:
            return None
        if self._family == "codex":
            item = event.get("item")
            actor_ref = (
                _codex_item_actor(event, item)
                if isinstance(item, dict)
                else None
            )
            if (
                event.get("type") not in {"item.updated", "item.completed"}
                or not isinstance(item, dict)
                or item.get("type") != "command_execution"
                or actor_ref is _CODEX_ITEM_ACTOR_CONFLICT
                or (
                    actor_ref is not None
                    and actor_ref != self._root_native_session_ref
                )
            ):
                return None
            output = next(
                (
                    item.get(name)
                    for name in ("aggregated_output", "output", "stdout")
                    if isinstance(item.get(name), str)
                ),
                None,
            )
            if not isinstance(output, str):
                return None
            command_ref = item.get("id")
            return output, command_ref if isinstance(command_ref, str) else None

        event_native_ref = event.get("session_id")
        root_event = (
            event_native_ref == self._root_native_session_ref
            and event.get("parent_tool_use_id") in (None, "")
        )
        if not root_event:
            return None
        output: list[str] = []
        for block in _claude_content_blocks(event):
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_use_id = block.get("id")
                name = block.get("name")
                if isinstance(tool_use_id, str) and isinstance(name, str):
                    self._claude_tools[tool_use_id] = name
                continue
            if block_type != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            name = (
                self._claude_tools.get(tool_use_id)
                if isinstance(tool_use_id, str)
                else None
            )
            if name is None or name.casefold() not in {"bash", "shell"}:
                continue
            value = _claude_tool_result_text(block.get("content"))
            if value:
                output.append(value)
        return ("\n".join(output), None) if output else None


def _claude_tool_result_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    parts = [
        str(item["text"])
        for item in value
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts) if parts else None


def _consume_target_root_ansi_delta(
    state: _TargetRootCommandOutputState,
    delta: str,
    *,
    final: bool,
) -> tuple[str, bool, int]:
    """Strip CSI/OSC incrementally so event boundaries cannot hide tokens."""

    data = state.ansi_pending + delta
    state.ansi_pending = ""
    visible: list[str] = []
    changed = False
    overflow_bytes = 0

    while data:
        overflow_mode = state.ansi_overflow_mode
        if overflow_mode is not None:
            consumed, terminated = _consume_overflowed_ansi_sequence(
                state, data, overflow_mode
            )
            overflow_bytes += len(
                _target_root_output_bytes(data[:consumed])
            )
            data = data[consumed:]
            changed = True
            if not terminated:
                return "".join(visible), changed, overflow_bytes
            state.ansi_overflow_mode = None
            continue

        introducer = _next_ansi_introducer(data)
        if introducer is None:
            visible.append(data)
            break
        index, mode, body_start = introducer
        if index:
            visible.append(data[:index])
        sequence_end = _ansi_sequence_end(data, mode, body_start)
        if sequence_end is not None:
            data = data[sequence_end:]
            changed = True
            continue

        pending = data[index:]
        if final:
            # A truncated escape at command completion is control material,
            # never public stdout.
            changed = True
            break
        if (
            len(_target_root_output_bytes(pending))
            <= _TARGET_ROOT_OUTPUT_PENDING_LIMIT
        ):
            state.ansi_pending = pending
            break
        overflow_bytes += len(_target_root_output_bytes(pending))
        state.ansi_overflow_mode = mode
        state.ansi_osc_escape_pending = (
            mode == "osc" and pending.endswith("\x1b")
        )
        changed = True
        break

    return "".join(visible), changed, overflow_bytes


def _next_ansi_introducer(
    value: str,
) -> tuple[int, Literal["csi", "osc", "escape"], int] | None:
    candidates = [
        (index, character)
        for character in ("\x1b", "\x9b", "\x9d")
        if (index := value.find(character)) >= 0
    ]
    if not candidates:
        return None
    index, character = min(candidates, key=lambda item: item[0])
    if character == "\x9b":
        return index, "csi", index + 1
    if character == "\x9d":
        return index, "osc", index + 1
    if index + 1 >= len(value):
        return index, "escape", index + 1
    selector = value[index + 1]
    if selector == "[":
        return index, "csi", index + 2
    if selector == "]":
        return index, "osc", index + 2
    return index, "escape", index + 1


def _ansi_sequence_end(
    value: str,
    mode: Literal["csi", "osc", "escape"],
    body_start: int,
) -> int | None:
    if mode == "escape":
        return body_start + 1 if body_start < len(value) else None
    if mode == "csi":
        for index in range(body_start, len(value)):
            if 0x40 <= ord(value[index]) <= 0x7E:
                return index + 1
        return None
    index = body_start
    while index < len(value):
        character = value[index]
        if character in {"\x07", "\x9c"}:
            return index + 1
        if (
            character == "\x1b"
            and index + 1 < len(value)
            and value[index + 1] == "\\"
        ):
            return index + 2
        index += 1
    return None


def _consume_overflowed_ansi_sequence(
    state: _TargetRootCommandOutputState,
    value: str,
    mode: Literal["csi", "osc", "escape"],
) -> tuple[int, bool]:
    if mode == "escape":
        return (1, True) if value else (0, False)
    if mode == "csi":
        for index, character in enumerate(value):
            if 0x40 <= ord(character) <= 0x7E:
                return index + 1, True
        return len(value), False
    start = 0
    if state.ansi_osc_escape_pending:
        state.ansi_osc_escape_pending = False
        if value.startswith("\\"):
            return 1, True
    while start < len(value):
        character = value[start]
        if character in {"\x07", "\x9c"}:
            return start + 1, True
        if character == "\x1b":
            if start + 1 < len(value) and value[start + 1] == "\\":
                return start + 2, True
            if start + 1 == len(value):
                state.ansi_osc_escape_pending = True
        start += 1
    return len(value), False


def _consume_target_root_line_endings(
    state: _TargetRootCommandOutputState,
    delta: str,
    *,
    final: bool,
) -> tuple[str, bool]:
    """Normalize CRLF, but never let a bare CR create a trusted line."""

    visible: list[str] = []
    changed = False
    index = 0
    if state.carriage_return_pending:
        if delta.startswith("\n"):
            visible.append("\n")
            index = 1
        elif not delta and not final:
            return "", False
        state.carriage_return_pending = False
        changed = True

    while index < len(delta):
        character = delta[index]
        if character != "\r":
            visible.append(character)
            index += 1
            continue
        changed = True
        if index + 1 < len(delta):
            if delta[index + 1] == "\n":
                visible.append("\n")
                index += 2
            else:
                # Delete a bare CR so both sides remain one logical line and
                # are judged together by the Owner secret detector.
                index += 1
            continue
        if not final:
            state.carriage_return_pending = True
        index += 1
    return "".join(visible), changed


def _consume_target_root_output_delta(
    state: _TargetRootCommandOutputState,
    delta: str,
    *,
    final: bool,
) -> tuple[str, bool, int]:
    """Return only bytes proven safe for an incremental public observation.

    Codex command events repeat cumulative stdout.  An unfinished line is the
    only raw fragment retained, and it is capped at the public chunk budget.
    Secret bodies are discarded as soon as their opener is recognized.
    """

    clean_delta, ansi_changed, ansi_overflow_bytes = (
        _consume_target_root_ansi_delta(state, delta, final=final)
    )
    normalized_delta, carriage_return_changed = (
        _consume_target_root_line_endings(state, clean_delta, final=final)
    )
    data = state.pending + normalized_delta
    state.pending = ""
    redacted = (
        state.pending_redacted
        or ansi_changed
        or carriage_return_changed
    )
    state.pending_redacted = False
    visible: list[str] = []
    overflow_bytes = ansi_overflow_bytes

    while data:
        if state.mode == "secret_line":
            newline = data.find("\n")
            if newline < 0:
                # The replacement was already emitted.  Retaining the token
                # body would add no display value and could retain a secret.
                return "".join(visible), True, overflow_bytes
            visible.append("\n")
            redacted = True
            data = data[newline + 1 :]
            state.mode = "normal"
            continue

        if state.mode == "overflow":
            newline = data.find("\n")
            if newline < 0:
                overflow_bytes += len(_target_root_output_bytes(data))
                if final:
                    state.mode = "normal"
                return "".join(visible), True, overflow_bytes
            dropped = data[: newline + 1]
            overflow_bytes += len(_target_root_output_bytes(dropped))
            data = data[newline + 1 :]
            state.mode = "normal"
            redacted = True
            continue

        if state.mode == "private_key":
            normalized = _normalize_target_root_output(data)
            key_end = _PRIVATE_KEY_END.search(normalized)
            if key_end is not None:
                data = normalized[key_end.end() :]
                state.mode = "normal"
                redacted = True
                continue
            if final:
                # An unterminated PEM body is still secret.  Its opener's
                # replacement was emitted by the earlier cumulative update.
                state.mode = "normal"
                return "".join(visible), True, overflow_bytes
            encoded = _target_root_output_bytes(normalized)
            if len(encoded) > _TARGET_ROOT_OUTPUT_PENDING_LIMIT:
                kept = _utf8_tail(
                    normalized, _TARGET_ROOT_OUTPUT_PENDING_LIMIT
                )
                overflow_bytes += len(encoded) - len(
                    _target_root_output_bytes(kept)
                )
                state.pending = kept
            else:
                state.pending = normalized
            return "".join(visible), True, overflow_bytes

        newline = data.find("\n")
        if newline >= 0:
            line = data[: newline + 1]
            data = data[newline + 1 :]
            projected, changed = _project_complete_target_root_line(
                state, line
            )
            if projected:
                visible.append(projected)
            redacted = redacted or changed
            continue

        if final:
            projected, changed = _project_complete_target_root_line(
                state, data
            )
            if projected:
                visible.append(projected)
            redacted = redacted or changed
            return "".join(visible), redacted, overflow_bytes

        normalized = _normalize_target_root_output(data)
        normalized_changed = normalized != data
        encoded = _target_root_output_bytes(normalized)
        if len(encoded) > _TARGET_ROOT_OUTPUT_PENDING_LIMIT:
            overflow_bytes += len(encoded)
            state.mode = "overflow"
            return "".join(visible), True, overflow_bytes
        # Until a logical line is closed, no raw prefix is provably safe: a
        # later cumulative suffix can turn it into a credential URI, natural
        # language password, JWT, or provider token.  Hold the whole line.
        state.pending = normalized
        state.pending_redacted = redacted or normalized_changed
        return "".join(visible), redacted, overflow_bytes

    if final and state.mode in {"secret_line", "private_key", "overflow"}:
        state.mode = "normal"
    return "".join(visible), redacted, overflow_bytes


def _project_complete_target_root_line(
    state: _TargetRootCommandOutputState,
    value: str,
) -> tuple[str, bool]:
    normalized = _normalize_target_root_output(value)
    normalized_changed = normalized != value
    opener = _first_target_root_secret_opener(normalized)
    if opener is not None:
        kind, match = opener
        prefix, _prefix_redacted = _sanitize_target_root_output(
            normalized[: match.start()]
        )
        line_ending = "\n" if normalized.endswith("\n") else ""
        if kind == "private_key":
            key_end = _PRIVATE_KEY_END.search(normalized, match.end())
            if key_end is None:
                state.mode = "private_key"
                return prefix + "[REDACTED PRIVATE KEY]", True
            return prefix + "[REDACTED PRIVATE KEY]" + line_ending, True
        if kind == "bearer":
            replacement = "Bearer [REDACTED]"
        elif kind == "assignment":
            replacement = (
                str(match.group(1))
                + str(match.group(2))
                + "[REDACTED]"
            )
        else:
            replacement = "[REDACTED]"
        return prefix + replacement + line_ending, True
    if _target_root_owner_secret_detected(normalized):
        return "[REDACTED]" + ("\n" if normalized.endswith("\n") else ""), True
    sanitized, sanitized_changed = _sanitize_target_root_output(normalized)
    return sanitized, normalized_changed or sanitized_changed


def _first_target_root_secret_opener(
    value: str,
) -> tuple[
    Literal["private_key", "known_token", "bearer", "assignment"],
    re.Match[str],
] | None:
    matches: list[
        tuple[
            Literal[
                "private_key", "known_token", "bearer", "assignment"
            ],
            re.Match[str],
        ]
    ] = []
    for kind, pattern in (
        ("private_key", _PRIVATE_KEY_BEGIN),
        ("known_token", _KNOWN_TOKEN),
        ("bearer", _BEARER_OPENER),
        ("assignment", _SECRET_ASSIGNMENT_OPENER),
    ):
        match = pattern.search(value)
        if match is not None:
            matches.append((kind, match))
    generic_assignment = _first_owner_secret_assignment(value)
    if generic_assignment is not None:
        matches.append(("assignment", generic_assignment))
    if not matches:
        return None
    return min(matches, key=lambda item: item[1].start())


def _first_owner_secret_assignment(value: str) -> re.Match[str] | None:
    """Find a key assignment in linear time; Owner decides key sensitivity."""

    for separator in re.finditer(r"[:=]", value):
        key_end = separator.start()
        while key_end and value[key_end - 1] in {" ", "\t"}:
            key_end -= 1
        key_start = key_end
        while (
            key_start
            and key_end - key_start <= 255
            and (
                value[key_start - 1].isalnum()
                or value[key_start - 1] in {"_", ".", "-"}
            )
        ):
            key_start -= 1
        match = _GENERIC_ASSIGNMENT_OPENER.match(value, key_start)
        if (
            match is not None
            and match.end(1) == key_end
            and contains_secret(
                {str(match.group(1)): "public-placeholder"}
            )
        ):
            return match
    return None


def _target_root_owner_secret_detected(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in _OWNER_SECRET_TEXT_MARKERS) and (
        contains_secret(value)
    )


def _normalize_target_root_output(value: str) -> str:
    sanitized = value.replace("\r\n", "\n").replace("\r", "")
    sanitized = _ANSI_ESCAPE.sub("", sanitized)
    sanitized = "".join(
        character
        for character in sanitized
        if character in {"\n", "\t"}
        or not unicodedata.category(character).startswith("C")
    )
    return sanitized


def _target_root_output_bytes(value: str) -> bytes:
    # JSON permits escaped lone surrogates.  They are removed from the public
    # projection as Unicode controls, but must remain hashable/countable while
    # cumulative raw output is reconciled.
    return value.encode("utf-8", errors="surrogatepass")


def _sanitize_target_root_output(value: str) -> tuple[str, bool]:
    sanitized = _normalize_target_root_output(value)
    sanitized = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", sanitized)
    sanitized = _BEARER_SECRET.sub("Bearer [REDACTED]", sanitized)
    sanitized = _URL_USERINFO.sub(r"\1[REDACTED]@", sanitized)
    sanitized = _KNOWN_TOKEN.sub("[REDACTED]", sanitized)
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", sanitized)
    changed = sanitized != value
    return sanitized, changed


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = _target_root_output_bytes(value)
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _utf8_tail(value: str, limit: int) -> str:
    encoded = _target_root_output_bytes(value)
    if len(encoded) <= limit:
        return value
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _evidence_from_events(
    *,
    family: HarnessFamily,
    provider_version: str,
    events: tuple[dict[str, object], ...],
    expected_native_session_ref: str | None,
    evidence_scope_ref: str,
    observation_scope: dict[str, object],
    transport_receipt: dict[str, object] | None = None,
    codex_child_ledger_reader: CodexChildLedgerReader | None = None,
    expected_working_directory: str | None = None,
) -> HarnessTurnEvidence:
    observed: dict[str, list[str]] = {
        name: [] for name in HARNESS_CAPABILITIES
    }
    native_refs: set[str] = set()
    root_native_refs: set[str] = set()
    summaries: list[dict[str, object]] = []
    terminal = False
    claude_pending_tools: dict[str, tuple[str, str]] = {}
    projector = _TargetRootEventProjector(
        family=family,
        observation_scope=observation_scope,
    )
    for sequence, event in enumerate(events, start=1):
        (
            summary,
            capabilities,
            native_ref,
            is_terminal,
            _observation,
        ) = projector.summarize(event)
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
            if (
                (family == "codex" and event.get("type") == "thread.started")
                or (
                    family == "claude"
                    and event.get("type") == "system"
                    and event.get("subtype") == "init"
                )
            ):
                root_native_refs.add(native_ref)
        terminal = terminal or is_terminal
    if len(summaries) >= 2 and terminal:
        observed["stream"].extend(
            [summaries[0]["event_ref"], summaries[-1]["event_ref"]]
        )
    if expected_native_session_ref is not None:
        if expected_native_session_ref not in native_refs:
            raise HarnessAdapterUnavailable("native_session_identity_changed")
        native_session_ref = expected_native_session_ref
    elif len(root_native_refs) == 1:
        native_session_ref = next(iter(root_native_refs))
    elif len(native_refs) == 1:
        native_session_ref = next(iter(native_refs))
    else:
        raise HarnessAdapterUnavailable("native_session_identity_unavailable")
    subagent_evidence = _verified_subagent_evidence(
        family,
        events,
        evidence_refs_by_sequence={
            int(item["sequence"]): str(item["event_ref"])
            for item in summaries
        },
        root_session_ref=native_session_ref,
        codex_child_ledger_reader=codex_child_ledger_reader,
        expected_working_directory=expected_working_directory,
    )
    if subagent_evidence is not None:
        observed["subagent"].extend(
            (
                str(subagent_evidence["spawn_evidence_ref"]),
                str(subagent_evidence["completion_evidence_ref"]),
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
                "status": "not_observed",
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
        "subagent_evidence": (
            [] if subagent_evidence is None else [subagent_evidence]
        ),
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
    inventory_names: list[str] | None = None
    native_ref = event.get("thread_id")
    if not isinstance(native_ref, str):
        native_ref = None
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    tool = item.get("tool") if isinstance(item, dict) else None
    server = item.get("server") if isinstance(item, dict) else None
    # Locked Codex 0.147 JSONL reports only ``thread_id`` on
    # ``thread.started``. Feature support is probed separately; never promote
    # a synthetic ``tools`` test field into production inventory evidence.
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
    if inventory_names is not None:
        summary["inventory_kinds"] = list(dict.fromkeys(inventory_names))
    if isinstance(item_type, str):
        summary["item_kind"] = item_type
    if (
        event_type == "item.completed"
        and item_type == "skill"
        and isinstance(item, dict)
        and isinstance(item.get("name"), str)
    ):
        summary["skill_name"] = item["name"]
        actor_ref = item.get("sender_thread_id")
        if isinstance(actor_ref, str):
            summary["actor_session_ref"] = actor_ref
    if event_type == "item.completed" and isinstance(item, dict):
        command = _codex_successful_command(item)
        if command is not None:
            summary.update(command)
        root_evidence = _codex_root_preflight_envelope(item)
        if root_evidence is not None:
            summary["target_root_evidence"] = root_evidence
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
    skill_invocations: list[dict[str, str]] = []
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
                    tool_use_id = block.get("id")
                    skill_input = block.get("input")
                    if (
                        name.lower() == "skill"
                        and isinstance(tool_use_id, str)
                        and isinstance(skill_input, dict)
                        and set(skill_input) == {"skill"}
                        and isinstance(skill_input.get("skill"), str)
                    ):
                        skill_invocations.append(
                            {
                                "tool_use_id": tool_use_id,
                                "skill_name": skill_input["skill"],
                            }
                        )
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
    if skill_invocations:
        summary["skill_invocations"] = skill_invocations
    result_ids = [
        block["tool_use_id"]
        for block in _claude_content_blocks(event)
        if block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
    ]
    if result_ids:
        summary["tool_result_ids"] = sorted(set(result_ids))
    successful_result_ids = [
        block["tool_use_id"]
        for block in _claude_content_blocks(event)
        if block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
        and block.get("is_error") is not True
        and block.get("status") not in {"error", "failed", "cancelled"}
    ]
    if successful_result_ids:
        summary["successful_tool_result_ids"] = sorted(
            set(successful_result_ids)
        )
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
    codex_child_ledger_reader: CodexChildLedgerReader | None = None,
    expected_working_directory: str | None = None,
) -> tuple[str, ...]:
    evidence = _verified_subagent_evidence(
        family,
        events,
        evidence_refs_by_sequence=evidence_refs_by_sequence,
        root_session_ref=root_session_ref,
        codex_child_ledger_reader=codex_child_ledger_reader,
        expected_working_directory=expected_working_directory,
    )
    if evidence is None:
        return ()
    return (
        str(evidence["spawn_evidence_ref"]),
        str(evidence["completion_evidence_ref"]),
    )


def _verified_subagent_evidence(
    family: HarnessFamily,
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
    codex_child_ledger_reader: CodexChildLedgerReader | None = None,
    expected_working_directory: str | None = None,
) -> dict[str, object] | None:
    if family == "codex":
        return _verified_codex_subagent_evidence(
            events,
            evidence_refs_by_sequence=evidence_refs_by_sequence,
            root_session_ref=root_session_ref,
            codex_child_ledger_reader=codex_child_ledger_reader,
            expected_working_directory=expected_working_directory,
        )
    return _verified_claude_subagent_evidence(
        events,
        evidence_refs_by_sequence=evidence_refs_by_sequence,
        root_session_ref=root_session_ref,
    )


def _verified_codex_subagent_evidence(
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
    codex_child_ledger_reader: CodexChildLedgerReader | None = None,
    expected_working_directory: str | None = None,
) -> dict[str, object] | None:
    root_code_review_skill_calls: list[int] = []
    spawn_calls: list[tuple[int, dict[str, object], str]] = []
    wait_calls: list[tuple[int, dict[str, object]]] = []
    for sequence, event in enumerate(events, start=1):
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "skill"
            and item.get("name") == "code-review"
            and _codex_item_actor(event, item) == root_session_ref
        ):
            root_code_review_skill_calls.append(sequence)
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            continue
        if item.get("tool") == "spawn_agent" and _is_completed_codex_spawn(
            event, item, root_session_ref
        ):
            child_ref = item["receiver_thread_ids"][0]
            assert isinstance(child_ref, str)
            spawn_calls.append((sequence, item, child_ref))
        elif (
            item.get("tool") == "wait"
            and event.get("type") == "item.completed"
            and item.get("status") == "completed"
            and _codex_item_actor(event, item) == root_session_ref
        ):
            wait_calls.append((sequence, item))
    child_spawn_counts = {
        child_ref: sum(1 for _sequence, _spawn, value in spawn_calls if value == child_ref)
        for _sequence, _spawn, child_ref in spawn_calls
    }
    completed_children: list[
        tuple[int, dict[str, object], str, int, dict[str, object]]
    ] = []
    for spawn_sequence, spawn, child_ref in spawn_calls:
        terminal_waits = [
            (sequence, state)
            for sequence, wait in wait_calls
            if (state := _codex_terminal_wait_child_state(
                wait,
                child_ref=child_ref,
                wait_sequence=sequence,
                after_sequence=spawn_sequence,
            )) is not None
        ]
        # A native child has one definitive root terminal wait.  Other
        # children' waits are intentionally ignored, but duplicate terminal
        # receipts for this child are ambiguous and cannot authorize it.
        if len(terminal_waits) != 1 or child_spawn_counts[child_ref] != 1:
            continue
        terminal_wait_sequence, terminal_child_state = terminal_waits[0]
        completed_children.append(
            (
                spawn_sequence,
                spawn,
                child_ref,
                terminal_wait_sequence,
                terminal_child_state,
            )
        )
    reviewer_candidates: list[
        tuple[
            int,
            dict[str, object],
            str,
            int,
            dict[str, object],
            dict[str, object],
            str,
        ]
    ] = []
    for (
        spawn_sequence,
        spawn,
        child_ref,
        terminal_wait_sequence,
        terminal_child_state,
    ) in completed_children:
        payload = _target_review_payload(terminal_child_state)
        if payload is None:
            continue
        review_kind = payload.get("review_kind")
        if review_kind == "code":
            child_review_evidence = _verified_codex_child_code_review_evidence(
                spawn=spawn,
                child_ref=child_ref,
                terminal_child_state=terminal_child_state,
                root_session_ref=root_session_ref,
                root_code_review_skill_calls=root_code_review_skill_calls,
                codex_child_ledger_reader=codex_child_ledger_reader,
                expected_working_directory=expected_working_directory,
            )
        elif review_kind == "result":
            child_review_evidence = _verified_codex_child_result_review_evidence(
                spawn=spawn,
                child_ref=child_ref,
                terminal_child_state=terminal_child_state,
                root_session_ref=root_session_ref,
                codex_child_ledger_reader=codex_child_ledger_reader,
                expected_working_directory=expected_working_directory,
            )
        else:
            child_review_evidence = None
        if child_review_evidence is not None:
            reviewer_candidates.append(
                (
                    spawn_sequence,
                    spawn,
                    child_ref,
                    terminal_wait_sequence,
                    terminal_child_state,
                    child_review_evidence,
                    str(review_kind),
                )
            )
    if len(reviewer_candidates) > 1:
        return None
    if reviewer_candidates:
        (
            spawn_sequence,
            _spawn,
            child_ref,
            terminal_wait_sequence,
            terminal_child_state,
            child_review_evidence,
            review_kind,
        ) = reviewer_candidates[0]
        result = _codex_subagent_result(
            root_session_ref=root_session_ref,
            child_ref=child_ref,
            spawn_sequence=spawn_sequence,
            terminal_wait_sequence=terminal_wait_sequence,
            terminal_child_state=terminal_child_state,
            evidence_refs_by_sequence=evidence_refs_by_sequence,
        )
        if result is None:
            return None
        if review_kind == "code":
            root_preflight_evidence = _verified_codex_root_preflight_evidence(
                events,
                evidence_refs_by_sequence=evidence_refs_by_sequence,
                root_session_ref=root_session_ref,
                spawn_sequence=spawn_sequence,
            )
            if root_preflight_evidence is not None:
                result.update(root_preflight_evidence)
        result.update(child_review_evidence)
        return result
    # Preserve generic subagent conformance when exactly one ordinary child
    # exists, while never downgrading an invalid Target review into generic
    # evidence that a review Owner could accidentally consume.
    if len(completed_children) != 1:
        return None
    (
        spawn_sequence,
        fallback_spawn,
        child_ref,
        terminal_wait_sequence,
        terminal_child_state,
    ) = completed_children[0]
    fallback_payload = _target_review_payload(terminal_child_state)
    if (
        (fallback_payload is not None and fallback_payload.get("review_kind") == "result")
        or _target_result_review_request(fallback_spawn.get("prompt")) is not None
    ):
        return None
    return _codex_subagent_result(
        root_session_ref=root_session_ref,
        child_ref=child_ref,
        spawn_sequence=spawn_sequence,
        terminal_wait_sequence=terminal_wait_sequence,
        terminal_child_state=terminal_child_state,
        evidence_refs_by_sequence=evidence_refs_by_sequence,
    )


def _is_completed_codex_spawn(
    event: dict[str, object], item: dict[str, object], root_session_ref: str
) -> bool:
    receivers = item.get("receiver_thread_ids")
    states = item.get("agents_states")
    return (
        event.get("type") == "item.completed"
        and item.get("status") == "completed"
        and _codex_item_actor(event, item) == root_session_ref
        and isinstance(receivers, list)
        and len(receivers) == 1
        and isinstance(receivers[0], str)
        and bool(receivers[0])
        and receivers[0] != root_session_ref
        and isinstance(states, dict)
        and isinstance(states.get(receivers[0]), dict)
    )


def _codex_terminal_wait_child_state(
    wait: dict[str, object],
    *,
    child_ref: str,
    wait_sequence: int,
    after_sequence: int,
) -> dict[str, object] | None:
    # The sequence check belongs here so each candidate binds to its own
    # spawn, rather than to any unrelated terminal wait in the root turn.
    receivers = wait.get("receiver_thread_ids")
    states = wait.get("agents_states")
    if (
        wait_sequence <= after_sequence
        or receivers != [child_ref]
        or not isinstance(states, dict)
        or not isinstance(states.get(child_ref), dict)
        or states[child_ref].get("status") != "completed"
    ):
        return None
    return states[child_ref]


def _codex_subagent_result(
    *,
    root_session_ref: str,
    child_ref: str,
    spawn_sequence: int,
    terminal_wait_sequence: int,
    terminal_child_state: dict[str, object],
    evidence_refs_by_sequence: dict[int, str],
) -> dict[str, object] | None:
    spawn_ref = evidence_refs_by_sequence.get(spawn_sequence)
    completion_ref = evidence_refs_by_sequence.get(terminal_wait_sequence)
    if spawn_ref is None or completion_ref is None:
        return None
    payload = _target_review_payload(terminal_child_state)
    return {
        "parent_session_ref": root_session_ref,
        "child_session_ref": child_ref,
        "spawn_evidence_ref": spawn_ref,
        "completion_evidence_ref": completion_ref,
        "payload": payload,
        "payload_hash": None if payload is None else canonical_hash(payload),
    }


def _codex_item_actor(
    event: dict[str, object],
    item: dict[str, object],
) -> object:
    """Return one unambiguous explicit actor, or a conflict sentinel."""

    actors: set[str] = set()
    for owner, field in (
        (event, "thread_id"),
        (event, "sender_thread_id"),
        (item, "thread_id"),
        (item, "sender_thread_id"),
    ):
        if field not in owner or owner[field] is None:
            continue
        value = owner[field]
        if not isinstance(value, str) or not value:
            return _CODEX_ITEM_ACTOR_CONFLICT
        actors.add(value)
    if not actors:
        return None
    if len(actors) != 1:
        return _CODEX_ITEM_ACTOR_CONFLICT
    return next(iter(actors))


def _codex_successful_command(item: dict[str, object]) -> dict[str, object] | None:
    """Summarize an observed successful root command without retaining output."""

    item_id = item.get("id")
    exit_code = item.get("exit_code")
    if (
        item.get("type") != "command_execution"
        or not isinstance(item_id, str)
        or not item_id
        or isinstance(exit_code, bool)
        or exit_code != 0
    ):
        return None
    output = item.get("output")
    if isinstance(output, str):
        # Escaped lone surrogates are legal JSON string content but cannot be
        # encoded by canonical_json.  The public observation projector removes
        # them; the formal command-exit hash uses a deterministic replacement.
        output = output.encode("utf-8", errors="replace").decode("utf-8")
    return {
        "command_item_id": item_id,
        "command_exit_hash": canonical_hash(
            {
                "command_item_id": item_id,
                "exit_code": exit_code,
                "output": output,
            }
        ),
    }


def _closed_root_preflight_envelope(value: object) -> dict[str, object] | None:
    """Parse the exact JSON envelope emitted by the root agent, not prose."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > _RESULT_LIMIT:
        return None
    try:
        document = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    schema_ref = document.get("schema_ref")
    common_keys = {
        "schema_ref",
        "target_ref",
        "target_run_ref",
        "implementation_revision_ref",
        "expected_tree_hash",
    }
    if schema_ref == _TARGET_CANDIDATE_READY_SCHEMA:
        expected_keys = common_keys
    elif schema_ref == _TARGET_SELF_CHECK_SCHEMA:
        expected_keys = common_keys | {"status"}
    else:
        return None
    if set(document) != expected_keys:
        return None
    if any(
        not isinstance(document.get(field), str) or not document[field]
        for field in (
            "target_ref",
            "target_run_ref",
            "implementation_revision_ref",
            "expected_tree_hash",
        )
    ):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(document["expected_tree_hash"])):
        return None
    if schema_ref == _TARGET_SELF_CHECK_SCHEMA and document.get("status") != "passed":
        return None
    return cast(dict[str, object], document)


def _codex_root_preflight_envelope(item: dict[str, object]) -> dict[str, object] | None:
    if item.get("type") != "agent_message":
        return None
    # Codex 0.147 uses ``text`` for agent_message.  ``content`` is accepted
    # only when it is the same closed scalar envelope, never a prose search.
    return _closed_root_preflight_envelope(item.get("text") or item.get("content"))


def _verified_codex_root_preflight_evidence(
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
    spawn_sequence: int,
) -> dict[str, object] | None:
    """Bind root implementation readiness to native spawn in causal order."""

    candidates: list[tuple[int, dict[str, object]]] = []
    self_checks: list[tuple[int, dict[str, object]]] = []
    commands: list[tuple[int, dict[str, object]]] = []
    for sequence, event in enumerate(events, start=1):
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        if _codex_item_actor(event, item) != root_session_ref:
            continue
        command = _codex_successful_command(item)
        if command is not None:
            commands.append((sequence, command))
        envelope = _codex_root_preflight_envelope(item)
        if envelope is None:
            continue
        if envelope["schema_ref"] == _TARGET_CANDIDATE_READY_SCHEMA:
            candidates.append((sequence, envelope))
        else:
            self_checks.append((sequence, envelope))
    if len(candidates) != 1 or len(self_checks) != 1:
        return None
    candidate_sequence, candidate = candidates[0]
    self_check_sequence, self_check = self_checks[0]
    if not (candidate_sequence < self_check_sequence < spawn_sequence):
        return None
    for field in (
        "target_ref",
        "target_run_ref",
        "implementation_revision_ref",
        "expected_tree_hash",
    ):
        if candidate[field] != self_check[field]:
            return None
    observed_commands = [
        command
        for sequence, command in commands
        if candidate_sequence < sequence < self_check_sequence
    ]
    if not observed_commands:
        return None
    candidate_ref = evidence_refs_by_sequence.get(candidate_sequence)
    self_check_ref = evidence_refs_by_sequence.get(self_check_sequence)
    if candidate_ref is None or self_check_ref is None:
        return None
    return {
        "candidate_ready_evidence_ref": candidate_ref,
        "candidate_ready": candidate,
        "self_check_evidence_ref": self_check_ref,
        "self_check_evidence_refs": [self_check_ref],
        "self_check": self_check,
        # These are adapter-derived native command facts.  The root marker
        # cannot know CLI item identifiers or output hashes while producing
        # its response, so accepting them from the root would be forgeable.
        "successful_command_item_ids": [
            command["command_item_id"] for command in observed_commands
        ],
        "successful_command_exit_hashes": [
            command["command_exit_hash"] for command in observed_commands
        ],
    }


def _verified_codex_child_code_review_evidence(
    *,
    spawn: dict[str, object],
    child_ref: str,
    terminal_child_state: dict[str, object] | None,
    root_session_ref: str,
    root_code_review_skill_calls: list[int],
    codex_child_ledger_reader: CodexChildLedgerReader | None,
    expected_working_directory: str | None,
) -> dict[str, object] | None:
    """Prove child-local `$code-review` without treating parent JSONL as it.

    Codex's parent ``--json`` stream proves the native spawn and terminal wait,
    but child Skill events live in the child session ledger.  Both sources are
    necessary: the ledger alone cannot prove the root waited for this child,
    and the parent stream alone cannot prove what the child executed.
    """

    prompt = spawn.get("prompt")
    spawn_skill_paths = (
        re.findall(r"\[skill:\$code-review\]\((/[^)\s]+)\)", prompt)
        if isinstance(prompt, str)
        else []
    )
    if (
        not isinstance(prompt, str)
        or len(prompt.encode("utf-8")) > _CHILD_PROMPT_LIMIT
        or len(spawn_skill_paths) != 1
        or root_code_review_skill_calls
        or codex_child_ledger_reader is None
        or expected_working_directory is None
    ):
        return None
    wait_message = terminal_child_state.get("message") if terminal_child_state else None
    if not isinstance(wait_message, str) or not wait_message:
        return None
    try:
        records = codex_child_ledger_reader.read(child_ref)
    except (OSError, ValueError):
        return None
    metadata = _verified_child_ledger_metadata(
        records,
        child_ref=child_ref,
        root_session_ref=root_session_ref,
        expected_working_directory=expected_working_directory,
    )
    skill = _verified_child_code_review_skill(records)
    terminal = _verified_child_terminal_output(records)
    if metadata is None or skill is None or terminal != wait_message:
        return None
    skill_path, skill_body = skill
    if spawn_skill_paths[0] != skill_path:
        return None
    try:
        package_hash = codex_child_ledger_reader.verify_skill_package(
            skill_path, skill_body
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(package_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", package_hash
    ):
        return None
    return {
        "skill_name": "code-review",
        "skill_actor_session_ref": child_ref,
        # The Skill injection is a child-ledger fact.  The session ledger has
        # no separate tool-call id for this CLI version, so this stable digest
        # is the non-forgeable-in-profile reference Owner persists with the
        # root spawn/wait event refs.
        "skill_invocation_evidence_ref": "codex_child_skill:"
        + canonical_hash(
            {
                "child_session_ref": child_ref,
                "parent_session_ref": root_session_ref,
                "skill_path": skill_path,
                "skill_package_hash": package_hash,
            }
        ),
        "skill_completion_evidence_ref": "codex_child_terminal:"
        + canonical_hash(
            {
                "child_session_ref": child_ref,
                "parent_session_ref": root_session_ref,
                "terminal_output_hash": hashlib.sha256(
                    terminal.encode("utf-8")
                ).hexdigest(),
            }
        ),
        "skill_package_path": skill_path,
        "skill_package_hash": package_hash,
        "spawn_prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "child_terminal_output_hash": hashlib.sha256(
            terminal.encode("utf-8")
        ).hexdigest(),
        "child_ledger_lineage": metadata,
    }


def _verified_codex_child_result_review_evidence(
    *,
    spawn: dict[str, object],
    child_ref: str,
    terminal_child_state: dict[str, object] | None,
    root_session_ref: str,
    codex_child_ledger_reader: CodexChildLedgerReader | None,
    expected_working_directory: str | None,
) -> dict[str, object] | None:
    """Prove a fresh native result-review child without a review Skill.

    The root stream supplies the native spawn and its own terminal wait.  The
    child ledger independently binds the exact structured task, lineage,
    confinement, and terminal output.  Result review deliberately does not
    reuse the code-review Skill or its child evidence contract.
    """

    prompt = spawn.get("prompt")
    request = _target_result_review_request(prompt)
    if (
        request is None
        or codex_child_ledger_reader is None
        or expected_working_directory is None
        or not isinstance(prompt, str)
        or len(prompt.encode("utf-8")) > _CHILD_PROMPT_LIMIT
    ):
        return None
    wait_message = terminal_child_state.get("message") if terminal_child_state else None
    if not isinstance(wait_message, str) or not wait_message:
        return None
    payload = _target_review_payload(terminal_child_state)
    review = payload.get("review") if payload is not None else None
    if (
        payload is None
        or payload.get("review_kind") != "result"
        or not isinstance(review, dict)
        or any(
            review.get(field) != request[field]
            for field in (
                "reviewed_evaluation_attempt_ref",
                "reviewed_metric_result_ref",
                "reviewed_asset_manifest_ref",
            )
        )
    ):
        return None
    try:
        records = codex_child_ledger_reader.read(child_ref)
    except (OSError, ValueError):
        return None
    metadata = _verified_child_ledger_metadata(
        records,
        child_ref=child_ref,
        root_session_ref=root_session_ref,
        expected_working_directory=expected_working_directory,
        allowed_sandbox_modes=frozenset({"read-only", "workspace-write"}),
    )
    terminal = _verified_child_terminal_output(records)
    if (
        metadata is None
        or terminal != wait_message
        or _verified_child_code_review_skill(records) is not None
        or _child_prompt_occurrences(records, prompt) != 1
    ):
        return None
    terminal_output_hash = hashlib.sha256(terminal.encode("utf-8")).hexdigest()
    return {
        "review_actor_session_ref": child_ref,
        "review_completion_evidence_ref": "codex_child_terminal:"
        + canonical_hash(
            {
                "child_session_ref": child_ref,
                "parent_session_ref": root_session_ref,
                "terminal_output_hash": terminal_output_hash,
            }
        ),
        "spawn_prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "child_terminal_output_hash": terminal_output_hash,
        "child_ledger_lineage": metadata,
        "result_review_request": request,
    }


def _target_result_review_request(value: object) -> dict[str, object] | None:
    """Read one closed result-review request marker from a spawn prompt."""

    if not isinstance(value, str):
        return None
    matches = re.findall(
        r"<target-result-review-request>\s*(\{.*?\})\s*"
        r"</target-result-review-request>",
        value,
        re.DOTALL,
    )
    if len(matches) != 1:
        return None
    try:
        request = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    expected_keys = {
        "schema_ref",
        "review_kind",
        "target_ref",
        "target_run_ref",
        "reviewed_evaluation_attempt_ref",
        "reviewed_metric_result_ref",
        "reviewed_asset_manifest_ref",
    }
    if (
        not isinstance(request, dict)
        or set(request) != expected_keys
        or request.get("schema_ref") != _TARGET_RESULT_REVIEW_REQUEST_SCHEMA
        or request.get("review_kind") != "result"
        or any(
            not isinstance(request.get(field), str) or not request[field]
            for field in expected_keys - {"schema_ref", "review_kind"}
        )
    ):
        return None
    return cast(dict[str, object], request)


def _child_prompt_occurrences(
    records: tuple[dict[str, object], ...], prompt: str
) -> int:
    """Count exact child-task prompt occurrences in user ledger items."""

    count = 0
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("role") != "user":
            continue
        content = payload.get("content")
        if isinstance(content, str):
            count += int(content == prompt)
        elif isinstance(content, list):
            count += sum(
                1
                for item in content
                if isinstance(item, dict)
                and item.get("type") in {"input_text", "text"}
                and item.get("text") == prompt
            )
    return count


def _verified_child_ledger_metadata(
    records: tuple[dict[str, object], ...],
    *,
    child_ref: str,
    root_session_ref: str,
    expected_working_directory: str,
    allowed_sandbox_modes: frozenset[str] = frozenset({"workspace-write"}),
) -> dict[str, object] | None:
    metadata = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
    ]
    if len(metadata) != 1:
        return None
    value = metadata[0]
    source = value.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    sandbox_mode = _verified_child_sandbox_mode(
        records,
        expected_working_directory=expected_working_directory,
        allowed_sandbox_modes=allowed_sandbox_modes,
    )
    if (
        value.get("id") != child_ref
        or value.get("session_id") != root_session_ref
        or value.get("parent_thread_id") != root_session_ref
        or not isinstance(spawn, dict)
        or spawn.get("parent_thread_id") != root_session_ref
        or value.get("cwd") != expected_working_directory
        or value.get("thread_source") != "subagent"
        or value.get("originator") != "codex_exec"
        or value.get("cli_version") != CODEX_LOCKED_VERSION
        or sandbox_mode is None
    ):
        return None
    return {
        "session_id": child_ref,
        "parent_session_id": root_session_ref,
        "thread_source": "subagent",
        "cwd": expected_working_directory,
        "originator": "codex_exec",
        "cli_version": CODEX_LOCKED_VERSION,
        "sandbox_mode": sandbox_mode,
    }


def _verified_child_sandbox_mode(
    records: tuple[dict[str, object], ...],
    *,
    expected_working_directory: str,
    allowed_sandbox_modes: frozenset[str],
) -> str | None:
    """Require every persisted child turn context to retain Target confinement."""

    contexts = [
        record.get("payload")
        for record in records
        if record.get("type") == "turn_context"
        and isinstance(record.get("payload"), dict)
    ]
    if not contexts:
        return None
    observed_mode: str | None = None
    for context in contexts:
        assert isinstance(context, dict)
        policy = context.get("sandbox_policy")
        mode = policy.get("type") if isinstance(policy, dict) else None
        if (
            context.get("cwd") != expected_working_directory
            or not isinstance(policy, dict)
            or not isinstance(mode, str)
            or mode not in allowed_sandbox_modes
            or (observed_mode is not None and mode != observed_mode)
        ):
            return None
        observed_mode = mode
    return observed_mode


def _verified_child_code_review_skill(
    records: tuple[dict[str, object], ...],
) -> tuple[str, str] | None:
    injections: list[tuple[str, str]] = []
    pattern = re.compile(
        r"<skill>\s*<name>\s*code-review\s*</name>\s*<path>\s*"
        r"(/[^<\s]+)\s*</path>(.*?)</skill>",
        re.DOTALL,
    )
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("role") != "user":
            continue
        content = payload.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") in {"input_text", "text"}
                    and isinstance(item.get("text"), str)
                ):
                    texts.append(item["text"])
        for text in texts:
            matches = pattern.findall(text)
            if len(matches) != 1:
                continue
            path, body = matches[0]
            if not path or not body:
                continue
            injections.append((path, body))
    return injections[0] if len(injections) == 1 else None


def _skill_body_without_wrapper_newline(body: str) -> tuple[str, ...]:
    """Allow only the newline(s) introduced by the XML-like Skill wrapper."""

    candidates = [body]
    if body.startswith("\n"):
        candidates.append(body[1:])
    if body.endswith("\n"):
        candidates.append(body[:-1])
    if body.startswith("\n") and body.endswith("\n"):
        candidates.append(body[1:-1])
    return tuple(dict.fromkeys(candidates))


def _verified_child_terminal_output(
    records: tuple[dict[str, object], ...],
) -> str | None:
    outputs = []
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("type") == "task_complete"
            and isinstance(payload.get("last_agent_message"), str)
            and payload["last_agent_message"]
        ):
            outputs.append(payload["last_agent_message"])
    return outputs[0] if len(outputs) == 1 else None


def _verified_claude_subagent_evidence(
    events: tuple[dict[str, object], ...],
    *,
    evidence_refs_by_sequence: dict[int, str],
    root_session_ref: str,
) -> dict[str, object] | None:
    requests: list[tuple[int, str]] = []
    results: list[tuple[int, dict[str, object], str | None]] = []
    for sequence, event in enumerate(events, start=1):
        for block in _claude_content_blocks(event):
            if block.get("type") == "tool_use":
                tool_name = str(block.get("name", "")).lower()
                tool_use_id = block.get("id")
                if tool_name in {"agent", "task", "subagent"}:
                    if (
                        event.get("session_id") != root_session_ref
                        or not isinstance(tool_use_id, str)
                        or not tool_use_id
                    ):
                        return None
                    requests.append((sequence, tool_use_id))
            elif block.get("type") == "tool_result":
                session_ref = event.get("session_id")
                results.append(
                    (
                        sequence,
                        block,
                        session_ref if isinstance(session_ref, str) else None,
                    )
                )
    if len(requests) != 1:
        return None
    request_sequence, tool_use_id = requests[0]
    matching = [
        (sequence, block)
        for sequence, block, _session_ref in results
        if block.get("tool_use_id") == tool_use_id
    ]
    if len(matching) != 1:
        return None
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
        return None
    spawn_ref = evidence_refs_by_sequence.get(request_sequence)
    completion_ref = evidence_refs_by_sequence.get(result_sequence)
    if spawn_ref is None or completion_ref is None:
        return None
    payload = _target_review_payload(result)
    evidence: dict[str, object] = {
        "parent_session_ref": root_session_ref,
        "child_session_ref": child_ref,
        "spawn_evidence_ref": spawn_ref,
        "completion_evidence_ref": completion_ref,
        "payload": payload,
        "payload_hash": None if payload is None else canonical_hash(payload),
    }
    return evidence


def _target_review_payload(value: object) -> dict[str, object] | None:
    """Extract only the closed Target review envelope from child output."""

    candidates: list[object] = [value]
    if isinstance(value, dict):
        candidates.extend(
            value.get(name) for name in ("result", "output", "message", "content")
        )
    for candidate in candidates:
        decoded = candidate
        if isinstance(candidate, str):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if (
            isinstance(decoded, dict)
            and decoded.get("schema_ref")
            == _TARGET_REVIEW_EVIDENCE_SCHEMA
            and decoded.get("review_kind") in {"code", "result"}
            and isinstance(decoded.get("review"), dict)
            and set(decoded)
            <= {"schema_ref", "review_kind", "review", "scope"}
        ):
            scope = decoded.get("scope")
            if scope is not None and not isinstance(scope, dict):
                continue
            return cast(dict[str, object], decoded)
    return None


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


def _capabilities_from_inventory(names: tuple[str, ...]) -> set[str]:
    """Map the provider's actual tool inventory onto the common Root floor."""

    capabilities: set[str] = set()
    for name in names:
        normalized = name.casefold()
        capabilities.update(_capabilities_for_tool(name, name))
        if normalized in {
            "shell",
            "bash",
            "exec",
            "exec_command",
            "unified_exec",
        }:
            # Codex workspace file access is carried by the sandboxed shell
            # surface even when no separate Read/Edit tool is advertised.
            capabilities.update({"shell", "file_access"})
        if normalized == "mcp" or normalized.startswith("mcp__"):
            capabilities.add("semantic_mcp")
        if normalized in {
            "agent",
            "spawn_agent",
            "collaboration",
            "collab_tool_call",
        }:
            capabilities.add("subagent")
    if names:
        capabilities.add("tool_inventory")
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
