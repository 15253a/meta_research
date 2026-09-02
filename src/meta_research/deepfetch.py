from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from meta_research.acquisition import DEEPFETCH_PROTOTYPE_COMMIT
from meta_research.codex_runtime import (
    CODEX_MODEL_REF,
    CODEX_REASONING_EFFORT_BINDING,
    CODEX_REASONING_EFFORT_CONFIG,
)
from meta_research.codex_ledger import (
    CodexHomeLedgerReader,
    CodexSessionLedgerReader,
)

from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA,
    ProviderSupervisorError,
    ensure_transport_key,
    read_transport_key_for_operation,
    read_transport_envelope,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervisor_request_never_started,
    write_supervisor_request,
    write_transport_envelope,
)
from meta_research.quest_drafting import (
    CODEX_DRAFTING_LOCKED_VERSION,
    DraftingUnavailable,
    PROVIDER_RESULT_MAX_BYTES,
    _CancellableProcessRunner,
    _ProcessStopped,
    _text_exceeds_limit,
)
from meta_research.root_capabilities import (
    CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS,
    RootCapabilityEntryPath,
    codex_feature_diagnostics,
    merge_root_capability_bindings,
    project_codex_post_turn_diagnostics,
    root_capability_profile,
)
from meta_research.root_operation_diagnostics import (
    RootOperationDiagnosticRecorder,
    root_operation_diagnostic_ref,
)
from meta_research.root_resident_mcp import (
    RootResidentMcpAccess,
    RootResidentMcpAuthority,
    RootResidentMcpChannels,
    RootResidentMcpError,
    semantic_mcp_environment,
)
from meta_research.semantic_mcp import ROOT_AGENT_ACQUISITION_OPERATION_IDS

if TYPE_CHECKING:
    from meta_research.owners.common import AcceptanceReceipt


LOGGER = logging.getLogger(__name__)


DEEPFETCH_REQUEST_SCHEMA = "meta-research/first-question-deepfetch-request/v1"
QUESTION_DEEPFETCH_REQUEST_SCHEMA = "meta-research/question-deepfetch-request/v1"
AUTONOMOUS_QUESTION_DEEPFETCH_REQUEST_SCHEMA = (
    "meta-research/autonomous-question-deepfetch-request/v1"
)
DEEPFETCH_RESULT_SCHEMA = "meta-research/first-question-deepfetch-result/v2"
DEEPFETCH_RUNTIME_BINDING_SCHEMA = "meta-research/deepfetch-runtime-binding/v1"
DEEPFETCH_WEB_EVIDENCE_SCHEMA = "meta-research/deepfetch-web-evidence/v1"
DEEPFETCH_PROTOTYPE_EVIDENCE_SCHEMA = "meta-research/deepfetch-prototype-evidence/v4"
DEEPFETCH_PROTOCOL_CHECKPOINT_SCHEMA = (
    "meta-research/deepfetch-v4-protocol-checkpoint/v3"
)
DEEPFETCH_PROVIDER_OPERATION_REGISTRY_SCHEMA = (
    "meta-research/deepfetch-provider-operation-registry/v1"
)
_DEEPFETCH_PROVIDER_OPERATION_SCHEMA = (
    "meta-research/deepfetch-provider-operation/v3"
)
MAX_DEEPFETCH_PAPERS = 500
MAX_DEEPFETCH_FULLTEXTS = 100
MAX_DEEPFETCH_SUMMARY_LENGTH = 100_000
MAX_DEEPFETCH_FULLTEXT_LENGTH = 280_000_000
MAX_DEEPFETCH_LIMITATIONS = 100
MAX_DEEPFETCH_FULLTEXT_FILE_BYTES = 32 * 1024 * 1024
MAX_DEEPFETCH_FULLTEXT_TOTAL_BYTES = 96 * 1024 * 1024
DEEPFETCH_PROVIDER_STREAM_MAX_BYTES = 64 * 1024 * 1024
DEEPFETCH_ACTIVITY_TAIL_MAX_BYTES = 512 * 1024
DEEPFETCH_ACTIVITY_MAX_EVENTS = 12
_DEEPFETCH_COMPLETION_RULES = (
    "最终 action=finalize 时，completion 描述证据完备性，不是流程是否执行完："
    "complete：papers 非空、每篇均取得全文且 Reader 成功，并且 limitations 为空；"
    "limited：papers 非空且 limitations 非空，用于存在 missing_fulltexts、Reader 失败"
    "或其他证据局限；"
    "honest_empty：papers 为空且 limitations 非空。"
    "workflow.main_agent_status=complete 只表示研究流程已执行完，不能据此选择 "
    "completion=complete。action=acquire 时 completion 必须为 null。"
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _public_deepfetch_activity_event(
    value: object,
    *,
    sequence: int,
) -> dict[str, object] | None:
    """Reduce one untrusted Provider event to a fixed, content-free label."""

    if not isinstance(value, dict):
        return None
    event_type = value.get("type")
    if event_type == "thread.started":
        return {
            "sequence": sequence,
            "category": "session",
            "status": "completed",
            "label": "DeepFetch 会话已启动",
        }
    if event_type == "turn.started":
        return {
            "sequence": sequence,
            "category": "analysis",
            "status": "running",
            "label": "正在规划研究步骤",
        }
    if event_type == "turn.completed":
        return {
            "sequence": sequence,
            "category": "session",
            "status": "completed",
            "label": "DeepFetch 当前检索轮次已完成",
        }
    if event_type not in {"item.started", "item.completed"}:
        return None
    item = value.get("item")
    if not isinstance(item, dict):
        return None
    started = event_type == "item.started"
    failed = not started and item.get("status") == "failed"
    status = "running" if started else "failed" if failed else "completed"
    item_type = item.get("type")
    if item_type == "web_search":
        action = item.get("action")
        action_type = action.get("type") if isinstance(action, dict) else None
        if action_type == "search":
            return {
                "sequence": sequence,
                "category": "web_search",
                "status": status,
                "label": (
                    "正在执行 Web Search"
                    if started
                    else "Web Search 遇到问题"
                    if failed
                    else "Web Search 已完成"
                ),
            }
        return {
            "sequence": sequence,
            "category": "web_fetch",
            "status": status,
            "label": (
                "正在读取 Web 资料"
                if started
                else "Web 资料读取遇到问题"
                if failed
                else "Web 资料读取完成"
            ),
        }
    labels = {
        "command_execution": (
            "正在处理研究资料",
            "研究资料处理遇到问题" if failed else "研究资料处理完成",
        ),
        "file_change": (
            "正在整理研究记录",
            "研究记录整理遇到问题" if failed else "研究记录已更新",
        ),
        "collab_tool_call": (
            "研究子任务正在执行",
            "研究子任务遇到问题" if failed else "研究子任务已返回",
        ),
        "agent_message": (
            "正在更新研究判断",
            "研究判断已更新",
        ),
    }
    label_pair = labels.get(item_type)
    if label_pair is None:
        return None
    return {
        "sequence": sequence,
        "category": "analysis",
        "status": status,
        "label": label_pair[0] if started else label_pair[1],
    }


_DEEPFETCH_PUBLIC_ACTIVITY_LABELS = frozenset(
    {
        ("session", "completed", "DeepFetch 会话已启动"),
        ("session", "completed", "DeepFetch 当前检索轮次已完成"),
        ("analysis", "running", "正在规划研究步骤"),
        ("web_search", "running", "正在执行 Web Search"),
        ("web_search", "completed", "Web Search 已完成"),
        ("web_search", "failed", "Web Search 遇到问题"),
        ("web_fetch", "running", "正在读取 Web 资料"),
        ("web_fetch", "completed", "Web 资料读取完成"),
        ("web_fetch", "failed", "Web 资料读取遇到问题"),
        ("analysis", "running", "正在处理研究资料"),
        ("analysis", "completed", "研究资料处理完成"),
        ("analysis", "failed", "研究资料处理遇到问题"),
        ("analysis", "running", "正在整理研究记录"),
        ("analysis", "completed", "研究记录已更新"),
        ("analysis", "failed", "研究记录整理遇到问题"),
        ("analysis", "running", "研究子任务正在执行"),
        ("analysis", "completed", "研究子任务已返回"),
        ("analysis", "failed", "研究子任务遇到问题"),
        ("analysis", "running", "正在更新研究判断"),
        ("analysis", "completed", "研究判断已更新"),
    }
)


def validate_deepfetch_activity_events(
    values: object,
) -> tuple[dict[str, object], ...]:
    """Validate the narrow public liveness projection from any Provider."""

    if not isinstance(values, (list, tuple)):
        raise DeepFetchUnavailable("deepfetch_activity_projection_invalid")
    projected: list[dict[str, object]] = []
    previous_sequence = 0
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "sequence",
            "category",
            "status",
            "label",
        }:
            raise DeepFetchUnavailable("deepfetch_activity_projection_invalid")
        sequence = value.get("sequence")
        category = value.get("category")
        status = value.get("status")
        label = value.get("label")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= previous_sequence
            or sequence > 9_007_199_254_740_991
            or not isinstance(category, str)
            or not isinstance(status, str)
            or not isinstance(label, str)
            or (category, status, label)
            not in _DEEPFETCH_PUBLIC_ACTIVITY_LABELS
        ):
            raise DeepFetchUnavailable("deepfetch_activity_projection_invalid")
        projected.append(dict(value))
        previous_sequence = sequence
    if len(projected) > DEEPFETCH_ACTIVITY_MAX_EVENTS:
        raise DeepFetchUnavailable("deepfetch_activity_projection_invalid")
    return tuple(projected)


def _provider_turn_marker_number(path: Path) -> int:
    suffix = path.stem.removeprefix("turn-")
    if not suffix.isdigit():
        raise ValueError("deepfetch_provider_turn_marker_invalid")
    return int(suffix)


class DeepFetchUnavailable(RuntimeError):
    """A real Web Research provider could not return a verifiable result."""

    def __init__(
        self,
        code: str,
        *,
        durable_outcome: Literal["unknown", "pending", "terminal"] = "unknown",
        native_session_ref: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.durable_outcome = durable_outcome
        self.native_session_ref = native_session_ref

    def as_verified_terminal(
        self, native_session_ref: str | None
    ) -> DeepFetchUnavailable:
        """Bind an error to a signed terminal provider receipt."""

        return DeepFetchUnavailable(
            self.code,
            durable_outcome="terminal",
            native_session_ref=native_session_ref,
        )


@dataclass(frozen=True)
class DeepFetchRuntimeBinding:
    provider_ref: str
    provider_version: str
    model_ref: str
    harness_ref: str
    capability_bindings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": DEEPFETCH_RUNTIME_BINDING_SCHEMA,
            "provider_ref": self.provider_ref,
            "provider_version": self.provider_version,
            "model_ref": self.model_ref,
            "harness_ref": self.harness_ref,
            "capability_bindings": list(self.capability_bindings),
        }


@dataclass(frozen=True)
class DeepFetchRunRequest:
    request_ref: str
    initialization_id: str
    correlation_ref: str
    draft_revision: int
    draft_hash: str
    draft: dict[str, object]
    scope: dict[str, object]
    scope_hash: str
    resource_envelope_ref: str
    resource_envelope_hash: str
    acquisition_session_ref: str
    acquisition_config_hash: str
    acquisition_runtime_binding_hash: str
    accepted_material_bindings: tuple[dict[str, object], ...]
    result_route: str
    authorization_receipt: AcceptanceReceipt
    creation_context_kind: Literal[
        "quest_initialization",
        "manual_question_creation",
        "autonomous_question_creation",
    ] = "quest_initialization"
    creation_context_ref: str | None = None
    context_generation: int | None = None
    quest_ref: str | None = None
    parent_question_ref: str | None = None
    context_basis_hash: str | None = None

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_ref": (
                DEEPFETCH_REQUEST_SCHEMA
                if self.creation_context_kind == "quest_initialization"
                else (
                    QUESTION_DEEPFETCH_REQUEST_SCHEMA
                    if self.creation_context_kind == "manual_question_creation"
                    else AUTONOMOUS_QUESTION_DEEPFETCH_REQUEST_SCHEMA
                )
            ),
            "request_ref": self.request_ref,
            "initialization_id": self.initialization_id,
            "correlation_ref": self.correlation_ref,
            "draft_revision": self.draft_revision,
            "draft_hash": self.draft_hash,
            "scope": self.scope,
            "scope_hash": self.scope_hash,
            "resource_envelope_ref": self.resource_envelope_ref,
            "resource_envelope_hash": self.resource_envelope_hash,
            "acquisition_session_ref": self.acquisition_session_ref,
            "acquisition_config_hash": self.acquisition_config_hash,
            "acquisition_runtime_binding_hash": (
                self.acquisition_runtime_binding_hash
            ),
            "accepted_material_bindings": list(self.accepted_material_bindings),
            "result_route": self.result_route,
        }
        if self.creation_context_kind != "quest_initialization":
            payload.update(
                {
                    "creation_context_kind": self.creation_context_kind,
                    "creation_context_ref": self.creation_context_ref,
                    "context_generation": self.context_generation,
                    "quest_ref": self.quest_ref,
                    "context_basis_hash": self.context_basis_hash,
                }
            )
            if self.creation_context_kind == "manual_question_creation":
                payload["parent_question_ref"] = self.parent_question_ref
        return payload


@dataclass(frozen=True)
class DeepFetchProviderRequest:
    request_ref: str
    initialization_id: str
    correlation_ref: str
    draft_revision: int
    draft_hash: str
    scope: dict[str, object]
    scope_hash: str
    accepted_material_bindings: tuple[dict[str, object], ...]
    authorization_receipt: AcceptanceReceipt
    runtime_binding: DeepFetchRuntimeBinding
    run_ref: str
    root_session_ref: str
    attempt_ref: str
    attempt_generation: int
    fence_ref: str
    native_session_ref: str | None = None
    job_ref: str | None = None
    human_request_resume: dict[str, str] | None = None
    # Reconciliation is a read-only compatibility path for a durable operation
    # admitted under a persisted predecessor binding.  Providers must not start
    # or resume an external effect while this flag is set.
    reconcile_only: bool = False


@dataclass(frozen=True)
class DeepFetchResult:
    completion: Literal["complete", "limited", "honest_empty"]
    summary: str
    papers: tuple[dict[str, object], ...]
    fulltexts: tuple[dict[str, object], ...]
    limitations: tuple[str, ...]
    native_session_ref: str
    adapter_kind: str
    web_evidence: dict[str, object] | None = None
    papers_ledger: dict[str, object] | None = None


@dataclass(frozen=True)
class _DeepFetchProtocolCheckpoint:
    identity_hash: str
    phase: Literal["ready_for_turn", "pending_acquisition", "finalized"]
    native_session_ref: str | None
    next_turn_number: int
    evidence_parts: tuple[dict[str, object], ...]
    acquisition_request_ids: tuple[str, ...]
    acquisition_item_proofs: tuple[dict[str, object], ...]
    pending_acquisition: dict[str, object] | None
    next_prompt: str | None
    final_envelope: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": DEEPFETCH_PROTOCOL_CHECKPOINT_SCHEMA,
            "identity_hash": self.identity_hash,
            "phase": self.phase,
            "native_session_ref": self.native_session_ref,
            "next_turn_number": self.next_turn_number,
            "evidence_parts": list(self.evidence_parts),
            "acquisition_request_ids": list(self.acquisition_request_ids),
            "acquisition_item_proofs": list(self.acquisition_item_proofs),
            "pending_acquisition": self.pending_acquisition,
            "next_prompt": self.next_prompt,
            "final_envelope": self.final_envelope,
        }


@dataclass(frozen=True)
class _VerifiedReaderAgentTrace:
    refs: tuple[str, ...]
    identity_kind: Literal["native_thread", "task_name"]
    native_thread_refs: tuple[str, ...] = ()


class DeepFetchProvider(Protocol):
    def runtime_binding(self) -> DeepFetchRuntimeBinding: ...

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult: ...

    def reconcile_cancelled_job(self, job_ref: str) -> bool: ...


def validate_runtime_binding(binding: DeepFetchRuntimeBinding) -> str:
    for value in (
        binding.provider_ref,
        binding.provider_version,
        binding.model_ref,
        binding.harness_ref,
    ):
        if not value or len(value) > 512:
            raise DeepFetchUnavailable("deepfetch_runtime_binding_invalid")
    capabilities = set(binding.capability_bindings)
    profile_capabilities = set(
        root_capability_profile("deepfetch").runtime_bindings()
    )
    required_capabilities = {"web-search-live", "web-fetch-live"}
    profile_claimed = any(
        capability.startswith("root-capability-")
        for capability in capabilities
    )
    if (
        len(capabilities) != len(binding.capability_bindings)
        or not required_capabilities.issubset(capabilities)
        or profile_claimed
        and not profile_capabilities.issubset(capabilities)
    ):
        raise DeepFetchUnavailable("deepfetch_runtime_capability_unavailable")
    return canonical_hash(binding.as_dict())


def validate_deepfetch_result(
    request: DeepFetchProviderRequest,
    result: DeepFetchResult,
) -> tuple[dict[str, object], str]:
    validate_runtime_binding(request.runtime_binding)
    if result.completion not in {"complete", "limited", "honest_empty"}:
        raise DeepFetchUnavailable("deepfetch_result_invalid")
    summary = _required_text(
        result.summary,
        maximum=MAX_DEEPFETCH_SUMMARY_LENGTH,
        code="deepfetch_summary_invalid",
    )
    native_session_ref = _required_text(
        result.native_session_ref,
        maximum=512,
        code="deepfetch_native_session_invalid",
    )
    if native_session_ref == request.root_session_ref:
        raise DeepFetchUnavailable("deepfetch_native_session_not_provider_owned")
    if request.native_session_ref is not None and (
        native_session_ref != request.native_session_ref
    ):
        raise DeepFetchUnavailable("deepfetch_native_session_changed")
    adapter_kind = _required_text(
        result.adapter_kind,
        maximum=80,
        code="deepfetch_adapter_kind_invalid",
    )
    if len(result.papers) > MAX_DEEPFETCH_PAPERS:
        raise DeepFetchUnavailable("deepfetch_papers_too_large")
    if len(result.fulltexts) > MAX_DEEPFETCH_FULLTEXTS:
        raise DeepFetchUnavailable("deepfetch_fulltexts_too_large")
    if len(result.limitations) > MAX_DEEPFETCH_LIMITATIONS:
        raise DeepFetchUnavailable("deepfetch_limitations_too_large")

    papers = tuple(_validated_paper(value) for value in result.papers)
    paper_urls = {cast(str, paper["url"]) for paper in papers}
    if len(paper_urls) != len(papers):
        raise DeepFetchUnavailable("deepfetch_papers_duplicate")
    fulltexts = tuple(
        _validated_fulltext(value, paper_urls=paper_urls) for value in result.fulltexts
    )
    fulltext_urls = {cast(str, item["paper_url"]) for item in fulltexts}
    if len(fulltext_urls) != len(fulltexts):
        raise DeepFetchUnavailable("deepfetch_fulltexts_duplicate")
    limitations = tuple(
        _required_text(
            value,
            maximum=8_000,
            code="deepfetch_limitation_invalid",
        )
        for value in result.limitations
    )
    if len(set(limitations)) != len(limitations):
        raise DeepFetchUnavailable("deepfetch_limitations_duplicate")
    if request.scope.get("literature_mode") == "oa_only" and (
        _misstates_selected_oa_as_fallback(summary)
        or any(
            _misstates_selected_oa_as_fallback(value) for value in limitations
        )
    ):
        raise DeepFetchUnavailable("deepfetch_oa_only_limitation_invalid")
    if result.completion == "honest_empty" and (papers or fulltexts):
        raise DeepFetchUnavailable("deepfetch_empty_result_not_empty")
    if result.completion == "honest_empty" and not limitations:
        raise DeepFetchUnavailable("deepfetch_empty_result_limit_missing")
    if result.completion == "limited" and not limitations:
        raise DeepFetchUnavailable("deepfetch_limited_result_limit_missing")
    if result.completion == "complete" and not papers:
        raise DeepFetchUnavailable("deepfetch_complete_result_empty")
    statuses = {
        cast(str, paper["url"]): cast(str, paper["fulltext_status"]) for paper in papers
    }
    if any(
        (status == "accepted") != (paper_url in fulltext_urls)
        for paper_url, status in statuses.items()
    ):
        raise DeepFetchUnavailable("deepfetch_fulltext_status_mismatch")
    if result.completion == "complete" and (
        limitations
        or len(fulltexts) != len(papers)
        or any(status != "accepted" for status in statuses.values())
    ):
        raise DeepFetchUnavailable("deepfetch_complete_result_incomplete")
    if result.completion == "limited" and not papers:
        raise DeepFetchUnavailable("deepfetch_limited_result_empty")

    web_evidence = _validated_web_evidence(result.web_evidence)
    production_adapter = (
        request.runtime_binding.provider_ref
        == "meta_research.deepfetch.CodexDeepFetchAdapter"
    )
    if production_adapter and web_evidence is None:
        raise DeepFetchUnavailable("deepfetch_web_evidence_missing")
    papers_ledger = _validated_result_ledger(result.papers_ledger)
    if production_adapter and papers_ledger is None:
        raise DeepFetchUnavailable("deepfetch_papers_v4_missing")

    payload: dict[str, object] = {
        "schema_ref": DEEPFETCH_RESULT_SCHEMA,
        "request_ref": request.request_ref,
        "initialization_id": request.initialization_id,
        "correlation_ref": request.correlation_ref,
        "draft_revision": request.draft_revision,
        "draft_hash": request.draft_hash,
        "scope_hash": request.scope_hash,
        "completion": result.completion,
        "summary": summary,
        "papers": list(papers),
        "papers_ledger": papers_ledger,
        "fulltexts": list(fulltexts),
        "limitations": list(limitations),
        "native_session_ref": native_session_ref,
        "adapter_kind": adapter_kind,
        "web_evidence": web_evidence,
    }
    return payload, canonical_hash(payload)


def _misstates_selected_oa_as_fallback(value: str) -> bool:
    normalized = value.casefold()
    names_oa = any(
        marker in normalized
        for marker in ("oa_only", "oa-only", "open access", "open-access", "开放获取")
    ) or re.search(r"(?<![a-z0-9])oa(?![a-z0-9])", normalized) is not None
    institution = any(
        marker in normalized
        for marker in ("institutional", "institution", "browser", "机构", "图书馆", "浏览器")
    )
    unavailable = any(
        marker in normalized
        for marker in ("不可用", "失败", "disabled", "unavailable", "not authorized")
    )
    causal = any(
        marker in normalized
        for marker in ("因此", "所以", "导致", "由于", "because", "therefore", "due to")
    )
    coercion_context = normalized
    for correction in (
        "并非被迫使用",
        "并非被迫采用",
        "并非被迫转向",
        "不是被迫使用",
        "不是被迫采用",
        "不是被迫转向",
        "没有被迫使用",
        "没有被迫采用",
        "没有被迫转向",
        "没有降级到",
        "没有降级至",
        "并未降级到",
        "并未降级至",
        "not forced to use",
        "not forced to fall back",
        "not degraded to",
        "never forced to use",
        "never forced to fall back",
        "never degraded to",
    ):
        coercion_context = coercion_context.replace(correction, "")
    forced_or_degraded = any(
        marker in coercion_context
        for marker in (
            "被迫使用",
            "被迫采用",
            "被迫转向",
            "不得不使用",
            "不得不采用",
            "不得不转向",
            "只好使用",
            "只好采用",
            "只好转向",
            "降级到",
            "降级至",
            "forced to use",
            "forced to fall back",
            "degraded to",
        )
    )
    return names_oa and (
        forced_or_degraded or institution and unavailable and causal
    )


class CodexDeepFetchAdapter:
    """Production Adapter for the bound DeepFetch v4 Harness workflow."""

    # DeepFetch already runs inside its own dedicated provider workspace and
    # publishes only artifacts that pass the Owner-side import validators.
    # Supported deployment hosts do not reliably permit the user namespaces
    # required by Codex's workspace-write sandbox, so use the same explicit
    # local-execution boundary as the production Idea adapter.
    _sandbox_mode = "danger-full-access"

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        process_runner: (
            Callable[
                [list[str], str, float | None],
                subprocess.CompletedProcess[str],
            ]
            | None
        ) = None,
        codex_ledger_reader: CodexSessionLedgerReader | None = None,
        codex_home: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._agent_workspace_root = self._workspace / "research-workspaces"
        self._agent_workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._executable = executable
        self._model_ref = model_ref
        self._runner = process_runner or _CancellableProcessRunner(
            stream_max_bytes=DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
        )
        self._skill_root = _deepfetch_skill_root()
        self._skill_bundle_hash = _deepfetch_skill_bundle_hash(self._skill_root)
        self._codex_ledger_reader = codex_ledger_reader
        if self._codex_ledger_reader is None and codex_home is not None:
            self._codex_ledger_reader = CodexHomeLedgerReader(codex_home)
        self._root_operation_diagnostic_recorder: (
            RootOperationDiagnosticRecorder | None
        ) = None
        self._root_resident_mcp = RootResidentMcpChannels("deepfetch")

    def bind_resident_mcp_authority(
        self, authority: RootResidentMcpAuthority
    ) -> None:
        try:
            self._root_resident_mcp.bind_authority(authority)
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        try:
            self._root_resident_mcp.configure_endpoint(base_url)
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error

    def bind_root_operation_diagnostics_recorder(
        self, recorder: RootOperationDiagnosticRecorder
    ) -> None:
        self._root_operation_diagnostic_recorder = recorder

    def request_stop(self) -> None:
        try:
            request_stop = getattr(self._runner, "request_stop", None)
            if callable(request_stop):
                request_stop()
        finally:
            try:
                self._root_resident_mcp.release_all()
            except RootResidentMcpError as error:
                raise DeepFetchUnavailable(error.code) from error

    def _root_capability_diagnostics(
        self, *, entry_path: RootCapabilityEntryPath
    ) -> dict[str, object]:
        profile = root_capability_profile("deepfetch")
        feature_output: str | None = None
        run_command = getattr(self._runner, "run_command", None)
        if callable(run_command):
            argv = [
                self._executable,
                *profile.codex_arguments(entry_path=entry_path),
                "features",
                "list",
            ]
            try:
                completed = run_command(
                    argv, CODEX_FEATURE_INVENTORY_TIMEOUT_SECONDS
                )
            except Exception:
                # Provider feature discovery is diagnostic-only. The real turn
                # remains responsible for its own typed launch/stop outcome.
                LOGGER.warning(
                    "Codex feature inventory probe unavailable",
                    exc_info=True,
                    extra={"root_kind": "deepfetch"},
                )
            else:
                if completed.returncode == 0 and len(completed.stdout) <= 64 * 1024:
                    feature_output = completed.stdout
        return codex_feature_diagnostics(
            profile=profile,
            entry_path=entry_path,
            provider_version=CODEX_DRAFTING_LOCKED_VERSION,
            feature_output=feature_output,
        )

    def _record_root_operation_diagnostics(
        self,
        *,
        source_ref: str,
        phase: str,
        pre_turn: object,
        stdout: str,
    ) -> None:
        recorder = self._root_operation_diagnostic_recorder
        if recorder is None:
            return
        try:
            recorder.record(
                operation_ref=root_operation_diagnostic_ref(
                    "deepfetch",
                    source_ref=source_ref,
                    phase=phase,
                ),
                root_kind="deepfetch",
                diagnostics=project_codex_post_turn_diagnostics(
                    pre_turn,
                    stdout,
                ),
            )
        except Exception:
            # Public diagnostics are deliberately fail-open with respect to
            # the DeepFetch result and its durable reconciliation identity.
            LOGGER.warning(
                "root operation diagnostic projection failed",
                exc_info=True,
                extra={"root_kind": "deepfetch"},
            )
            return

    def cancel_job(self, job_ref: str) -> None:
        if callable(getattr(self._runner, "run_durable_job", None)):
            # A ready durable supervisor owns its provider process and signed
            # terminal receipt. Ask it to stop through that sealed channel;
            # generic process-tree cancellation can destroy the receipt.
            self.reconcile_cancelled_job(job_ref)
            return
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            for provider_job_ref in self._registered_provider_jobs(job_ref):
                cancel_job(provider_job_ref)

    def finish_job(self, job_ref: str) -> None:
        try:
            finish_job = getattr(self._runner, "finish_job", None)
            if callable(finish_job):
                for provider_job_ref in self._registered_provider_jobs(job_ref):
                    finish_job(provider_job_ref)
        finally:
            try:
                self._root_resident_mcp.release_job(
                    job_ref, include_children=True
                )
            except RootResidentMcpError as error:
                raise DeepFetchUnavailable(error.code) from error

    def recent_activity_events(
        self,
        job_ref: str,
        runtime_binding_hash: str,
        *,
        limit: int = DEEPFETCH_ACTIVITY_MAX_EVENTS,
    ) -> tuple[dict[str, object], ...]:
        """Read a bounded tail and expose only fixed operational labels.

        Provider payloads can contain queries, URLs, commands, paths, and prose.
        None of those values cross this observer seam.
        """

        if limit < 1:
            return ()
        try:
            root_operation = self._provider_operation_root(
                job_ref, runtime_binding_hash
            )
            if root_operation.is_symlink() or not root_operation.is_dir():
                return ()
            provider_jobs = self._registered_provider_jobs(
                job_ref,
                operation_roots=(root_operation,),
            )
            sources: list[tuple[int, int, Path]] = []
            for provider_job_ref in provider_jobs:
                root_ref, separator, turn = provider_job_ref.rpartition(
                    ":v4-turn:"
                )
                turn_sequence = (
                    int(turn) + 1
                    if separator and root_ref == job_ref and turn.isdigit()
                    else 0
                )
                operation_root = self._provider_operation_root(
                    provider_job_ref, runtime_binding_hash
                )
                if operation_root.is_symlink() or not operation_root.is_dir():
                    continue
                directories = [operation_root / "deepfetch-initial"]
                directories.extend(
                    sorted(
                        (
                            path
                            for path in operation_root.glob(
                                "deepfetch-resume-*"
                            )
                            if path.name.removeprefix(
                                "deepfetch-resume-"
                            ).isdigit()
                        ),
                        key=lambda path: int(
                            path.name.removeprefix("deepfetch-resume-")
                        ),
                    )
                )
                for segment, directory in enumerate(directories):
                    sources.append(
                        (turn_sequence, segment, directory / "stdout.jsonl")
                    )

            public_limit = min(limit, DEEPFETCH_ACTIVITY_MAX_EVENTS)
            remaining = public_limit
            reverse_batches: list[list[dict[str, object]]] = []
            for turn_sequence, segment, stdout_path in reversed(sources):
                directory = stdout_path.parent
                if (
                    directory.is_symlink()
                    or not directory.is_dir()
                    or stdout_path.is_symlink()
                    or not stdout_path.is_file()
                ):
                    continue
                size = stdout_path.stat().st_size
                start = max(0, size - DEEPFETCH_ACTIVITY_TAIL_MAX_BYTES)
                with stdout_path.open("rb") as stream:
                    stream.seek(start)
                    data = stream.read(DEEPFETCH_ACTIVITY_TAIL_MAX_BYTES)
                base_offset = start
                if start:
                    boundary = data.find(b"\n")
                    if boundary < 0:
                        continue
                    base_offset += boundary + 1
                    data = data[boundary + 1 :]
                if data and not data.endswith(b"\n"):
                    boundary = data.rfind(b"\n")
                    if boundary < 0:
                        continue
                    data = data[: boundary + 1]
                offset = base_offset
                segment_events: list[dict[str, object]] = []
                for raw_line in data.splitlines(keepends=True):
                    line_offset = offset
                    offset += len(raw_line)
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    public = _public_deepfetch_activity_event(
                        event,
                        sequence=(
                            turn_sequence
                            * DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
                            * 1024
                            + segment * DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
                            + line_offset
                            + 1
                        ),
                    )
                    if public is not None:
                        segment_events.append(public)
                if segment_events:
                    batch = segment_events[-remaining:]
                    reverse_batches.append(batch)
                    remaining -= len(batch)
                    if remaining == 0:
                        break
            return tuple(
                event
                for batch in reversed(reverse_batches)
                for event in batch
            )
        except OSError:
            return ()

    def reconcile_cancelled_job(self, job_ref: str) -> bool:
        """Stop and verify all durable DeepFetch segments for a terminal Run."""

        try:
            _key_path, key = ensure_transport_key(self._workspace)
            for provider_job_ref in self._registered_provider_jobs(job_ref):
                for operation_root in self._provider_operation_roots(
                    provider_job_ref
                ):
                    directories = sorted(
                        path
                        for path in operation_root.iterdir()
                        if path.is_dir() and path.name.startswith("deepfetch-")
                    )
                    for directory in directories:
                        invocation_path = directory / "invocation.json"
                        if not invocation_path.is_file():
                            if any(directory.iterdir()):
                                raise ProviderSupervisorError(
                                    "provider_supervisor_spool_invalid"
                                )
                            continue
                        invocation = read_transport_envelope(invocation_path, key)
                        segment_name = directory.name.removeprefix("deepfetch-")
                        if (
                            invocation.get("schema_ref")
                            != _DEEPFETCH_PROVIDER_OPERATION_SCHEMA
                            or invocation.get("job_ref") != provider_job_ref
                            or invocation.get("segment_name") != segment_name
                        ):
                            raise ProviderSupervisorError(
                                "provider_supervisor_spool_invalid"
                            )
                        invocation_hash = canonical_hash(invocation)
                        receipt_path = directory / "supervisor-exit.json"
                        if not receipt_path.is_file():
                            if not (directory / "supervisor-ready.json").is_file():
                                if not supervisor_request_never_started(
                                    directory,
                                    key=key,
                                    invocation_hash=invocation_hash,
                                    request_schema=SUPERVISOR_REQUEST_SCHEMA,
                                ):
                                    return False
                                continue
                            if not request_supervisor_stop(
                                directory,
                                key=key,
                                invocation_hash=invocation_hash,
                                ready_schema=(
                                    "meta-research/codex-provider-supervisor-ready/v1"
                                ),
                            ):
                                return False
                            if not receipt_path.is_file():
                                return False
                        read_verified_exit_receipt(
                            receipt_path,
                            key=key,
                            invocation_hash=invocation_hash,
                            prompt_path=directory / "prompt.txt",
                            schema_path=directory / "output-schema.json",
                            stdout_path=directory / "stdout.jsonl",
                            result_path=directory / "last-message.json",
                        )
        except (OSError, ProviderSupervisorError, DeepFetchUnavailable):
            return False
        try:
            self._root_resident_mcp.release_job(
                job_ref, include_children=True
            )
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error
        return True

    def _provider_operation_root(
        self, job_ref: str, runtime_binding_hash: str
    ) -> Path:
        return (
            self._workspace
            / "provider-operations"
            / f"{runtime_binding_hash}-{canonical_hash({'job_ref': job_ref})}"
        )

    def _protocol_run_root(
        self, run_key: str, runtime_binding_hash: str
    ) -> Path:
        return self._workspace / "runs" / f"{runtime_binding_hash}-{run_key}"

    @staticmethod
    def _logical_run_key(
        request: DeepFetchRunRequest | DeepFetchProviderRequest,
    ) -> str:
        return canonical_hash(
            {
                "request_ref": request.request_ref,
                "correlation_ref": request.correlation_ref,
                "draft_hash": request.draft_hash,
                "scope_hash": request.scope_hash,
            }
        )[:32]

    @staticmethod
    def _root_provider_job_ref(job_ref: str) -> str:
        root_job_ref, separator, turn = job_ref.rpartition(":v4-turn:")
        if separator and root_job_ref and turn.isdigit():
            return root_job_ref
        return job_ref

    @classmethod
    def _provider_execution_key(cls, job_ref: str) -> str:
        return canonical_hash({"job_ref": cls._root_provider_job_ref(job_ref)})

    def _agent_workspace_path(
        self, job_ref: str, runtime_binding_hash: str
    ) -> Path:
        workspace = self._agent_workspace_root / (
            f"{runtime_binding_hash}-{self._provider_execution_key(job_ref)}"
        )
        if workspace.parent != self._agent_workspace_root:
            raise DeepFetchUnavailable("deepfetch_provider_workspace_invalid")
        return workspace

    def _agent_workspace_for(
        self, job_ref: str, runtime_binding_hash: str
    ) -> Path:
        workspace = self._agent_workspace_path(job_ref, runtime_binding_hash)
        try:
            if (
                self._agent_workspace_root.is_symlink()
                or not self._agent_workspace_root.is_dir()
            ):
                raise OSError("unsafe provider workspace root")
            workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (
                workspace.is_symlink()
                or not workspace.is_dir()
                or workspace.resolve(strict=True).parent
                != self._agent_workspace_root.resolve(strict=True)
            ):
                raise OSError("unsafe provider execution workspace")
        except OSError as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_workspace_invalid"
            ) from error
        return workspace

    def _provider_operation_roots(self, job_ref: str) -> tuple[Path, ...]:
        provider_root = self._workspace / "provider-operations"
        job_hash = canonical_hash({"job_ref": job_ref})
        candidates = [provider_root / job_hash]
        if provider_root.exists():
            candidates.extend(sorted(provider_root.glob(f"*-{job_hash}")))
        return tuple(
            dict.fromkeys(path for path in candidates if path.is_dir())
        )

    def _retry_operation_roots(
        self, job_ref: str, runtime_binding_hash: str
    ) -> tuple[Path, ...]:
        provider_root = self._workspace / "provider-operations"
        candidates = tuple(
            dict.fromkeys(
                (
                    provider_root / canonical_hash({"job_ref": job_ref}),
                    self._provider_operation_root(
                        job_ref, runtime_binding_hash
                    ),
                )
            )
        )
        existing = tuple(
            path for path in candidates if path.exists() or path.is_symlink()
        )
        if len(existing) > 1:
            raise DeepFetchUnavailable(
                "deepfetch_provider_retry_cleanup_invalid"
            )
        return existing

    def _register_provider_turn(
        self,
        *,
        root_job_ref: str,
        turn_number: int,
        provider_job_ref: str,
        runtime_binding_hash: str,
    ) -> None:
        operation_root = self._provider_operation_root(
            root_job_ref, runtime_binding_hash
        )
        registry_root = operation_root / "registered-turns"
        registry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            _key_path, key = ensure_transport_key(self._workspace)
            write_transport_envelope(
                registry_root / f"turn-{turn_number}.json",
                {
                    "schema_ref": DEEPFETCH_PROVIDER_OPERATION_REGISTRY_SCHEMA,
                    "root_job_ref": root_job_ref,
                    "turn_number": turn_number,
                    "provider_job_ref": provider_job_ref,
                },
                key,
            )
        except (OSError, ProviderSupervisorError) as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_spool_invalid"
            ) from error

    def _registered_provider_jobs(
        self,
        root_job_ref: str,
        *,
        operation_roots: tuple[Path, ...] | None = None,
    ) -> tuple[str, ...]:
        # The fixed legacy entries are migration coverage for provider turns
        # created by builds that predate the durable registry. New turns have
        # no generation ceiling and are discovered only through signed facts.
        job_refs = [
            root_job_ref,
            *(f"{root_job_ref}:v4-turn:{turn}" for turn in range(12)),
        ]
        try:
            _key_path, key = ensure_transport_key(self._workspace)
            roots = (
                self._provider_operation_roots(root_job_ref)
                if operation_roots is None
                else operation_roots
            )
            for operation_root in roots:
                registry_root = operation_root / "registered-turns"
                if not registry_root.exists():
                    continue
                markers = sorted(
                    registry_root.glob("turn-*.json"),
                    key=_provider_turn_marker_number,
                )
                for marker in markers:
                    turn_number = _provider_turn_marker_number(marker)
                    payload = read_transport_envelope(marker, key)
                    provider_job_ref = f"{root_job_ref}:v4-turn:{turn_number}"
                    if payload != {
                        "schema_ref": DEEPFETCH_PROVIDER_OPERATION_REGISTRY_SCHEMA,
                        "root_job_ref": root_job_ref,
                        "turn_number": turn_number,
                        "provider_job_ref": provider_job_ref,
                    }:
                        raise ProviderSupervisorError(
                            "provider_supervisor_spool_invalid"
                        )
                    job_refs.append(provider_job_ref)
        except (OSError, ProviderSupervisorError, ValueError) as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_spool_invalid"
            ) from error
        return tuple(dict.fromkeys(job_refs))

    @staticmethod
    def _remove_owned_execution_tree(path: Path, *, parent: Path) -> None:
        """Delete one exact provider-owned child without following symlinks."""

        if path.parent != parent:
            raise DeepFetchUnavailable("deepfetch_provider_retry_cleanup_invalid")
        try:
            if not path.exists() and not path.is_symlink():
                return
            if (
                parent.is_symlink()
                or not parent.is_dir()
                or path.is_symlink()
                or not path.is_dir()
                or path.resolve(strict=True).parent
                != parent.resolve(strict=True)
            ):
                raise OSError("unsafe provider retry cleanup target")
            shutil.rmtree(path)
        except OSError as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_retry_cleanup_invalid"
            ) from error

    def prepare_retry_execution(
        self,
        request: DeepFetchRunRequest,
        *,
        previous_job_ref: str,
        previous_run_ref: str,
        previous_runtime_binding: DeepFetchRuntimeBinding,
    ) -> None:
        """Remove one verified-terminal predecessor's exact physical state."""

        expected_prefix = f"{previous_run_ref}:deepfetch:"
        generation = previous_job_ref.removeprefix(expected_prefix)
        if (
            not previous_job_ref.startswith(expected_prefix)
            or not generation.isdigit()
            or int(generation) < 1
        ):
            raise DeepFetchUnavailable(
                "deepfetch_provider_retry_cleanup_invalid"
            )
        previous_binding_hash = validate_runtime_binding(
            previous_runtime_binding
        )
        logical_run_key = self._logical_run_key(request)
        execution_key = self._provider_execution_key(previous_job_ref)
        run_parent = self._workspace / "runs"
        protocol_candidates = tuple(
            dict.fromkeys(
                (
                    run_parent / execution_key,
                    self._protocol_run_root(
                        execution_key, previous_binding_hash
                    ),
                    # Compatibility roots written before provider-operation
                    # identity became the physical protocol partition.
                    run_parent / logical_run_key,
                    self._protocol_run_root(
                        logical_run_key, previous_binding_hash
                    ),
                )
            )
        )
        existing_protocol_roots = tuple(
            path
            for path in protocol_candidates
            if path.exists() or path.is_symlink()
        )
        if len(existing_protocol_roots) > 1:
            raise DeepFetchUnavailable(
                "deepfetch_provider_retry_cleanup_invalid"
            )

        root_operation_roots = self._retry_operation_roots(
            previous_job_ref, previous_binding_hash
        )
        provider_jobs = self._registered_provider_jobs(
            previous_job_ref,
            operation_roots=root_operation_roots,
        )
        operation_roots = tuple(
            dict.fromkeys(
                operation_root
                for provider_job_ref in provider_jobs
                for operation_root in self._retry_operation_roots(
                    provider_job_ref, previous_binding_hash
                )
            )
        )
        operation_workspace = self._agent_workspace_root / (
            f"{previous_binding_hash}-{execution_key}"
        )

        for protocol_root in existing_protocol_roots:
            self._remove_owned_execution_tree(
                protocol_root,
                parent=run_parent,
            )
        for operation_root in operation_roots:
            self._remove_owned_execution_tree(
                operation_root,
                parent=self._workspace / "provider-operations",
            )
        self._remove_owned_execution_tree(
            operation_workspace,
            parent=self._agent_workspace_root,
        )

    @property
    def requires_verified_terminal_retry(self) -> bool:
        """Whether a new durable effect requires a signed terminal predecessor."""

        return callable(getattr(self._runner, "run_durable_job", None))

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        try:
            resident_mcp_facts = self._root_resident_mcp.runtime_facts()
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error
        return DeepFetchRuntimeBinding(
            provider_ref="meta_research.deepfetch.CodexDeepFetchAdapter",
            provider_version=DEEPFETCH_PROTOTYPE_COMMIT,
            model_ref=self._model_ref,
            harness_ref="codex-cli",
            capability_bindings=merge_root_capability_bindings(
                (
                    "agent-workspace-policy:provider-operation-scoped-v2",
                    "approval-policy-never",
                    "deepfetch-v4-main-agent",
                    f"deepfetch-v4-skill-bundle-sha256:{self._skill_bundle_hash}",
                    "filesystem-danger-full-access",
                    "quest-acquisition-semantic-effect",
                    "native-child-readers",
                    "papers-v4-finalize",
                    "codex-reader-ledger:v1",
                    "provider-output-limits:"
                    f"stream={DEEPFETCH_PROVIDER_STREAM_MAX_BYTES};"
                    f"result={PROVIDER_RESULT_MAX_BYTES}",
                    CODEX_REASONING_EFFORT_BINDING,
                    "sandbox-policy:danger-full-access",
                    "structured-output-json-schema",
                    "web-evidence-gate:v1",
                    "workspace-write-public-artifacts",
                    *resident_mcp_facts.mcp_bindings,
                    *resident_mcp_facts.capability_bindings,
                ),
                "deepfetch",
            ),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        runtime_binding_hash = canonical_hash(request.runtime_binding.as_dict())
        logical_run_key = self._logical_run_key(request)
        root_job_ref = request.job_ref or f"{request.run_ref}:direct"
        execution_key = self._provider_execution_key(root_job_ref)
        if (
            not request.reconcile_only
            and request.runtime_binding.as_dict()
            != self.runtime_binding().as_dict()
        ):
            # An operation admitted before these protocol boundaries existed
            # may only be inspected through the predecessor-binding
            # reconciliation path.  It must never start a new provider effect.
            raise DeepFetchUnavailable(
                "deepfetch_runtime_binding_transition_required",
                durable_outcome="pending",
                native_session_ref=request.native_session_ref,
            )
        if request.reconcile_only:
            return self._reconcile_existing_protocol(
                request,
                execution_key=execution_key,
                legacy_run_key=logical_run_key,
                runtime_binding_hash=runtime_binding_hash,
            )
        self._agent_workspace_for(root_job_ref, runtime_binding_hash)
        run_root = self._protocol_run_root(
            execution_key,
            runtime_binding_hash,
        )
        public_root = run_root / "public"
        private_root = run_root / "private"
        public_root.mkdir(parents=True, exist_ok=True)
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_path = private_root / "protocol.json"
        identity_hash = _deepfetch_protocol_identity(request)
        try:
            initial_prompt = (
                self._human_request_resume_prompt(
                    request,
                    public_root=public_root,
                    private_root=private_root,
                )
                if request.human_request_resume is not None
                else self._web_evidence_gate_prompt(request)
            )
            checkpoint = _load_or_create_protocol_checkpoint(
                checkpoint_path,
                identity_hash=identity_hash,
                native_session_ref=request.native_session_ref,
                initial_prompt=initial_prompt,
            )
            return self._execute_protocol(
                request,
                public_root=public_root,
                checkpoint_path=checkpoint_path,
                checkpoint=checkpoint,
            )
        except DeepFetchUnavailable as error:
            if error.durable_outcome != "unknown":
                raise
            native_session_ref = error.native_session_ref
            if native_session_ref is None:
                native_session_ref = _checkpoint_native_session(
                    checkpoint_path,
                    identity_hash,
                )
            if (
                self.requires_verified_terminal_retry
                and native_session_ref is not None
            ):
                raise error.as_verified_terminal(native_session_ref) from error
            raise
        except (KeyError, TypeError, ValueError) as error:
            invalid = DeepFetchUnavailable("codex_deepfetch_output_invalid")
            native_session_ref = _checkpoint_native_session(
                checkpoint_path,
                identity_hash,
            )
            if self.requires_verified_terminal_retry and native_session_ref is not None:
                invalid = invalid.as_verified_terminal(native_session_ref)
            raise invalid from error

    def _reconcile_existing_protocol(
        self,
        request: DeepFetchProviderRequest,
        *,
        execution_key: str,
        legacy_run_key: str,
        runtime_binding_hash: str,
    ) -> DeepFetchResult:
        """Read one predecessor operation without starting a provider effect."""

        if request.job_ref is None or not callable(
            getattr(self._runner, "run_durable_job", None)
        ):
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=request.native_session_ref,
            )
        try:
            execution_root = self._select_reconciliation_root(
                self._workspace / "runs",
                identity_leaf=execution_key,
                runtime_binding_hash=runtime_binding_hash,
                allow_missing=True,
            )
            legacy_root = self._select_reconciliation_root(
                self._workspace / "runs",
                identity_leaf=legacy_run_key,
                runtime_binding_hash=runtime_binding_hash,
                allow_missing=True,
            )
            roots = tuple(
                dict.fromkeys(
                    root
                    for root in (execution_root, legacy_root)
                    if root is not None
                )
            )
            if len(roots) != 1:
                raise ValueError("deepfetch reconciliation run root conflict")
            run_root = roots[0]
            public_root = run_root / "public"
            private_root = run_root / "private"
            checkpoint_path = private_root / "protocol.json"
            if (
                run_root.is_symlink()
                or public_root.is_symlink()
                or private_root.is_symlink()
                or checkpoint_path.is_symlink()
                or not public_root.is_dir()
                or not private_root.is_dir()
                or not checkpoint_path.is_file()
            ):
                raise ValueError("deepfetch reconciliation run root")
            checkpoint = _read_protocol_checkpoint(
                checkpoint_path,
                _deepfetch_protocol_identity(request),
            )
            checkpoint = self._verify_existing_protocol_turns(
                request,
                public_root=public_root,
                checkpoint=checkpoint,
                runtime_binding_hash=runtime_binding_hash,
            )
        except DeepFetchUnavailable as error:
            if error.durable_outcome != "unknown":
                raise
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=request.native_session_ref,
            ) from error
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            ProviderSupervisorError,
        ) as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=request.native_session_ref,
            ) from error

        if checkpoint.phase != "finalized":
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=checkpoint.native_session_ref,
            )
        try:
            return self._execute_protocol(
                request,
                public_root=public_root,
                checkpoint_path=checkpoint_path,
                checkpoint=checkpoint,
            )
        except DeepFetchUnavailable as error:
            if error.durable_outcome != "unknown":
                raise
            assert checkpoint.native_session_ref is not None
            raise error.as_verified_terminal(
                checkpoint.native_session_ref
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            assert checkpoint.native_session_ref is not None
            raise DeepFetchUnavailable(
                "codex_deepfetch_output_invalid",
                durable_outcome="terminal",
                native_session_ref=checkpoint.native_session_ref,
            ) from error

    def _select_reconciliation_root(
        self,
        parent: Path,
        *,
        identity_leaf: str,
        runtime_binding_hash: str,
        allow_missing: bool = False,
    ) -> Path | None:
        """Select exactly one deployed legacy or binding-partitioned root."""

        if not parent.exists() and allow_missing:
            return None
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("deepfetch reconciliation root missing")
        legacy_name = identity_leaf
        partitioned_name = f"{runtime_binding_hash}-{identity_leaf}"
        matches: list[Path] = []
        for path in parent.iterdir():
            if path.name != legacy_name and not path.name.endswith(
                f"-{identity_leaf}"
            ):
                continue
            if path.is_symlink() or not path.is_dir():
                raise ValueError("deepfetch reconciliation root invalid")
            matches.append(path)
        if not matches and allow_missing:
            return None
        if len(matches) != 1 or matches[0].name not in {
            legacy_name,
            partitioned_name,
        }:
            raise ValueError("deepfetch reconciliation root conflict")
        return matches[0]

    def _verify_existing_protocol_turns(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        checkpoint: _DeepFetchProtocolCheckpoint,
        runtime_binding_hash: str,
    ) -> _DeepFetchProtocolCheckpoint:
        """Rebuild protocol facts from signed predecessor supervisor receipts."""

        assert request.job_ref is not None
        acquisition_effects: list[tuple[str, dict[str, object]]] = []
        observed_evidence: list[dict[str, object]] = []
        previous_native_session_ref: str | None = None
        enforce_previous_native = False
        for turn_number in range(checkpoint.next_turn_number):
            raw, native_session_ref, evidence = self._read_existing_turn(
                request,
                public_root=public_root,
                provider_job_ref=f"{request.job_ref}:v4-turn:{turn_number}",
                runtime_binding_hash=runtime_binding_hash,
                expected_native_session_ref=previous_native_session_ref,
                enforce_native_session_ref=enforce_previous_native,
                expected_web_gate=(
                    "web-evidence-gate:v1"
                    in request.runtime_binding.capability_bindings
                    and turn_number == 0
                ),
            )
            observed_evidence.append(evidence)
            previous_native_session_ref = native_session_ref
            enforce_previous_native = True
            if raw.get("status") == "web_evidence_ready":
                if turn_number != 0:
                    raise ValueError("deepfetch reconciliation gate position invalid")
                _validate_web_evidence_gate_result(raw)
                _merge_web_evidence([evidence])
            elif raw.get("action") == "acquire":
                acquisition_effects.append(
                    (
                        f"turn-{turn_number}",
                        _validated_v4_acquisition_effect(raw),
                    )
                )
            elif raw.get("action") == "finalize":
                _validate_final_envelope_shape(raw)
                if turn_number != checkpoint.next_turn_number - 1:
                    raise ValueError("deepfetch reconciliation finalized early")
            else:
                raise ValueError("deepfetch reconciliation action invalid")

        if tuple(observed_evidence) != checkpoint.evidence_parts:
            raise ValueError("deepfetch reconciliation evidence mismatch")
        observed_effects = tuple(
            {
                "effect_id": effect["effect_id"],
                "phase": phase,
                "target_hash": canonical_hash(effect["target"]),
            }
            for phase, effect in acquisition_effects
        )
        recorded_effects = tuple(
            {
                key: proof[key]
                for key in ("effect_id", "phase", "target_hash")
            }
            for proof in checkpoint.acquisition_item_proofs
        )
        recorded_request_ids = tuple(
            cast(str, proof["request_id"])
            for proof in checkpoint.acquisition_item_proofs
        )
        if checkpoint.phase == "finalized":
            if (
                not acquisition_effects
                and checkpoint.acquisition_request_ids
                or observed_effects != recorded_effects
                or recorded_request_ids != checkpoint.acquisition_request_ids
                or checkpoint.final_envelope is None
                or checkpoint.next_turn_number < 1
            ):
                raise ValueError("deepfetch reconciliation checkpoint mismatch")
            final_raw, final_native, _final_evidence = self._read_existing_turn(
                request,
                public_root=public_root,
                provider_job_ref=(
                    f"{request.job_ref}:v4-turn:"
                    f"{checkpoint.next_turn_number - 1}"
                ),
                runtime_binding_hash=runtime_binding_hash,
                expected_native_session_ref=None,
                enforce_native_session_ref=False,
                expected_web_gate=(
                    "web-evidence-gate:v1"
                    in request.runtime_binding.capability_bindings
                    and checkpoint.next_turn_number - 1 == 0
                ),
            )
            if (
                final_raw != checkpoint.final_envelope
                or final_native != checkpoint.native_session_ref
                or request.native_session_ref is not None
                and final_native != request.native_session_ref
            ):
                raise ValueError("deepfetch reconciliation final mismatch")
            return checkpoint

        if checkpoint.phase == "pending_acquisition":
            if (
                not acquisition_effects
                or checkpoint.pending_acquisition
                != acquisition_effects[-1][1]
                or observed_effects[:-1] != recorded_effects
                or recorded_request_ids != checkpoint.acquisition_request_ids
            ):
                raise ValueError("deepfetch reconciliation acquisition mismatch")
            assert checkpoint.native_session_ref is not None
            raise DeepFetchUnavailable(
                "deepfetch_runtime_binding_transition_required",
                durable_outcome="terminal",
                native_session_ref=checkpoint.native_session_ref,
            )

        if (
            observed_effects != recorded_effects
            or recorded_request_ids != checkpoint.acquisition_request_ids
        ):
            raise ValueError("deepfetch reconciliation acquisition mismatch")
        raw, native_session_ref, evidence = self._read_existing_turn(
            request,
            public_root=public_root,
            provider_job_ref=(
                f"{request.job_ref}:v4-turn:{checkpoint.next_turn_number}"
            ),
            runtime_binding_hash=runtime_binding_hash,
            expected_native_session_ref=previous_native_session_ref,
            enforce_native_session_ref=enforce_previous_native,
            allow_missing_operation=True,
            expected_web_gate=(
                "web-evidence-gate:v1"
                in request.runtime_binding.capability_bindings
                and checkpoint.next_turn_number == 0
            ),
        )
        if raw.get("status") == "web_evidence_ready":
            _validate_web_evidence_gate_result(raw)
            _merge_web_evidence([evidence])
            raise DeepFetchUnavailable(
                "deepfetch_runtime_binding_transition_required",
                durable_outcome="terminal",
                native_session_ref=native_session_ref,
            )
        if raw.get("action") != "finalize":
            # The predecessor provider effect is signed-terminal at this turn
            # boundary, but continuing would start a new old-binding effect.
            # Retire the old Attempt so a later command can transition binding.
            _validated_v4_acquisition_effect(raw)
            raise DeepFetchUnavailable(
                "deepfetch_runtime_binding_transition_required",
                durable_outcome="terminal",
                native_session_ref=native_session_ref,
            )
        _validate_final_envelope_shape(raw)
        if request.native_session_ref is not None and (
            native_session_ref != request.native_session_ref
        ):
            raise ValueError("deepfetch reconciliation native session mismatch")
        return replace(
            checkpoint,
            phase="finalized",
            native_session_ref=native_session_ref,
            next_turn_number=checkpoint.next_turn_number + 1,
            evidence_parts=(*checkpoint.evidence_parts, evidence),
            next_prompt=None,
            final_envelope=raw,
        )

    def _read_existing_turn(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        provider_job_ref: str,
        runtime_binding_hash: str,
        expected_native_session_ref: str | None,
        enforce_native_session_ref: bool,
        expected_web_gate: bool,
        allow_missing_operation: bool = False,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        """Verify one old durable turn without publishing any new files."""

        operation_root = self._select_reconciliation_root(
            self._workspace / "provider-operations",
            identity_leaf=canonical_hash({"job_ref": provider_job_ref}),
            runtime_binding_hash=runtime_binding_hash,
            allow_missing=allow_missing_operation,
        )
        if operation_root is None:
            # The durable protocol checkpoint precedes operation-root creation.
            # With neither supported root present, the provider dispatch could
            # not have been published and no old-binding effect may be resumed.
            raise DeepFetchUnavailable(
                "deepfetch_provider_never_started",
                durable_outcome="terminal",
                native_session_ref=expected_native_session_ref,
            )
        try:
            entries = tuple(operation_root.iterdir())
            if any(
                entry.is_symlink()
                or not entry.is_dir()
                or not entry.name.startswith("deepfetch-")
                for entry in entries
            ):
                raise ValueError("deepfetch reconciliation segment invalid")
            names = {entry.name for entry in entries}
            if "deepfetch-initial" not in names:
                raise ValueError("deepfetch reconciliation initial missing")
            resume_numbers = sorted(
                int(name.removeprefix("deepfetch-resume-"))
                for name in names
                if name.startswith("deepfetch-resume-")
                and name.removeprefix("deepfetch-resume-").isdigit()
            )
            expected_names = {
                "deepfetch-initial",
                *(f"deepfetch-resume-{number}" for number in resume_numbers),
            }
            if (
                names != expected_names
                or resume_numbers
                and resume_numbers != list(range(1, resume_numbers[-1] + 1))
            ):
                raise ValueError("deepfetch reconciliation segment conflict")
            directories = [
                operation_root / "deepfetch-initial",
                *(
                    operation_root / f"deepfetch-resume-{number}"
                    for number in resume_numbers
                ),
            ]
            _key_path, transport_key = read_transport_key_for_operation(
                directories[0]
            )
            traces: list[str] = []
            native_session_ref = expected_native_session_ref
            enforce_native = enforce_native_session_ref
            for index, directory in enumerate(directories):
                segment_name = "initial" if index == 0 else f"resume-{index}"
                invocation_path = directory / "invocation.json"
                prompt_path = directory / "prompt.txt"
                schema_path = directory / "output-schema.json"
                if any(
                    path.is_symlink() or not path.is_file()
                    for path in (invocation_path, prompt_path, schema_path)
                ):
                    raise ValueError("deepfetch reconciliation input invalid")
                invocation = read_transport_envelope(
                    invocation_path, transport_key
                )
                prompt = prompt_path.read_text(encoding="utf-8")
                schema_text = schema_path.read_text(encoding="utf-8")
                full_schema_text = canonical_json(_deepfetch_output_schema())
                gate_schema_text = canonical_json(
                    _deepfetch_web_evidence_gate_output_schema()
                )
                gate_turn = schema_text == gate_schema_text
                invocation_native = invocation.get("native_session_ref")
                invocation_schema = invocation.get("schema_ref")
                base_invocation_keys = {
                    "schema_ref",
                    "job_ref",
                    "segment_name",
                    "request_ref",
                    "correlation_ref",
                    "draft_hash",
                    "scope_hash",
                    "runtime_binding_hash",
                    "native_session_ref",
                    "prompt_hash",
                    "output_schema_hash",
                    "model_ref",
                }
                expected_invocation_keys = base_invocation_keys | {
                    "root_capability_profile",
                    "root_capability_profile_hash",
                }
                capability_profile = root_capability_profile("deepfetch")
                if enforce_native and invocation_native != native_session_ref:
                    raise ValueError("deepfetch reconciliation session mismatch")
                if (
                    set(invocation) != expected_invocation_keys
                    or invocation_schema
                    != _DEEPFETCH_PROVIDER_OPERATION_SCHEMA
                    or invocation.get("job_ref") != provider_job_ref
                    or invocation.get("segment_name") != segment_name
                    or invocation.get("request_ref") != request.request_ref
                    or invocation.get("correlation_ref") != request.correlation_ref
                    or invocation.get("draft_hash") != request.draft_hash
                    or invocation.get("scope_hash") != request.scope_hash
                    or invocation.get("runtime_binding_hash")
                    != runtime_binding_hash
                    or invocation.get("prompt_hash") != canonical_hash(prompt)
                    or invocation.get("output_schema_hash")
                    != canonical_hash(
                        _deepfetch_web_evidence_gate_output_schema()
                        if gate_turn
                        else _deepfetch_output_schema()
                    )
                    or invocation.get("model_ref")
                    != request.runtime_binding.model_ref
                    or invocation.get("root_capability_profile")
                    != capability_profile.as_dict()
                    or invocation.get("root_capability_profile_hash")
                    != capability_profile.digest
                    or schema_text not in {full_schema_text, gate_schema_text}
                    or gate_turn != expected_web_gate
                    or (
                        gate_turn
                        and not prompt.startswith("web_evidence_gate=v1\n")
                    )
                    or (
                        not gate_turn
                        and f"public_output_root={public_root}"
                        not in prompt.splitlines()
                    )
                ):
                    raise ValueError("deepfetch reconciliation identity mismatch")
                invocation_hash = canonical_hash(invocation)
                self._verify_existing_supervisor_request(
                    directory,
                    provider_job_ref=provider_job_ref,
                    invocation_hash=invocation_hash,
                    invocation_native_session_ref=cast(
                        str | None, invocation_native
                    ),
                    runtime_binding=request.runtime_binding,
                    transport_key=transport_key,
                )
                if not (directory / "supervisor-exit.json").is_file():
                    if supervisor_request_never_started(
                        directory,
                        key=transport_key,
                        invocation_hash=invocation_hash,
                        request_schema=SUPERVISOR_REQUEST_SCHEMA,
                    ):
                        raise DeepFetchUnavailable(
                            "deepfetch_provider_never_started",
                            durable_outcome="terminal",
                            native_session_ref=native_session_ref,
                        )
                    raise DeepFetchUnavailable(
                        "deepfetch_provider_reconciliation_pending",
                        durable_outcome="pending",
                        native_session_ref=native_session_ref,
                    )
                outcome = self._read_durable_segment(
                    directory=directory,
                    invocation_hash=invocation_hash,
                    native_session_ref=cast(str | None, invocation_native),
                    transport_key=transport_key,
                )
                self._record_root_operation_diagnostics(
                    source_ref=f"{provider_job_ref}:{segment_name}",
                    phase=segment_name,
                    pre_turn=self._root_capability_diagnostics(
                        entry_path=(
                            "resume"
                            if invocation_native is not None
                            else "initial"
                        )
                    ),
                    stdout=outcome[2],
                )
                traces.append(outcome[2])
                observed_native = outcome[3]
                if outcome[0] == "completed":
                    if index != len(directories) - 1:
                        raise ValueError(
                            "deepfetch reconciliation trailing segment"
                        )
                    if not isinstance(outcome[1], dict) or not isinstance(
                        observed_native, str
                    ):
                        raise ValueError("deepfetch reconciliation result invalid")
                    try:
                        evidence = _verified_turn_evidence("\n".join(traces))
                    except DeepFetchUnavailable as error:
                        raise error.as_verified_terminal(
                            observed_native
                        ) from error
                    return outcome[1], observed_native, evidence
                if not isinstance(observed_native, str) or not observed_native:
                    raise DeepFetchUnavailable(
                        "deepfetch_provider_stopped_before_session",
                        durable_outcome="terminal",
                    )
                native_session_ref = observed_native
                enforce_native = True
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=native_session_ref,
            )
        except DeepFetchUnavailable:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            ProviderSupervisorError,
            ValueError,
        ) as error:
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
                native_session_ref=request.native_session_ref,
            ) from error

    def _verify_existing_supervisor_request(
        self,
        directory: Path,
        *,
        provider_job_ref: str,
        invocation_hash: str,
        invocation_native_session_ref: str | None,
        runtime_binding: DeepFetchRuntimeBinding,
        transport_key: bytes,
    ) -> None:
        request_path = directory / "supervisor-request.json"
        if request_path.is_symlink() or not request_path.is_file():
            raise ValueError("deepfetch reconciliation supervisor request missing")
        supervisor = read_transport_envelope(request_path, transport_key)
        expected_paths = {
            "prompt_path": directory / "prompt.txt",
            "schema_path": directory / "output-schema.json",
            "stdout_path": directory / "stdout.jsonl",
            "result_path": directory / "last-message.json",
            "lock_path": directory / "supervisor.lock",
            "ready_path": directory / "supervisor-ready.json",
            "started_path": directory / "provider-started.json",
            "receipt_path": directory / "supervisor-exit.json",
            "stop_path": directory / "supervisor-stop.json",
        }
        timeout_seconds = supervisor.get("timeout_seconds")
        timeout_valid = timeout_seconds is None or (
            isinstance(timeout_seconds, (int, float))
            and not isinstance(timeout_seconds, bool)
            and float(timeout_seconds) > 0
        )
        if (
            set(supervisor)
            != {
                "schema_ref",
                "invocation_hash",
                "argv",
                "timeout_seconds",
                "stream_max_bytes",
                "result_max_bytes",
                *expected_paths,
            }
            or supervisor.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA
            or supervisor.get("invocation_hash") != invocation_hash
            or any(
                supervisor.get(key) != str(path)
                for key, path in expected_paths.items()
            )
            or not timeout_valid
            or not isinstance(supervisor.get("stream_max_bytes"), int)
            or isinstance(supervisor.get("stream_max_bytes"), bool)
            or not 0 < cast(int, supervisor["stream_max_bytes"]) <= (
                DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
            )
            or supervisor.get("result_max_bytes") != PROVIDER_RESULT_MAX_BYTES
        ):
            raise ValueError("deepfetch reconciliation supervisor request invalid")
        argv = supervisor.get("argv")
        if not isinstance(argv, list) or any(
            not isinstance(item, str) for item in argv
        ):
            raise ValueError("deepfetch reconciliation argv invalid")
        arguments = cast(list[str], argv)

        def option(name: str) -> str:
            positions = [
                index for index, value in enumerate(arguments) if value == name
            ]
            if len(positions) != 1 or positions[0] + 1 >= len(arguments):
                raise ValueError("deepfetch reconciliation argv invalid")
            return arguments[positions[0] + 1]

        expected_sandbox = (
            "danger-full-access"
            if "sandbox-policy:danger-full-access"
            in runtime_binding.capability_bindings
            else "workspace-write"
        )
        if (
            "agent-workspace-policy:provider-operation-scoped-v2"
            in runtime_binding.capability_bindings
        ):
            expected_workspace = self._agent_workspace_path(
                provider_job_ref,
                canonical_hash(runtime_binding.as_dict()),
            )
        elif (
            "agent-workspace-policy:dedicated-research-workspace-v1"
            in runtime_binding.capability_bindings
        ):
            expected_workspace = self._workspace / "research-workspace"
        else:
            expected_workspace = self._workspace
        if (
            len(arguments) < 2
            or arguments[1] != "exec"
            or option("--sandbox") != expected_sandbox
            or option("--model") != runtime_binding.model_ref
            or option("--cd") != str(expected_workspace)
            or option("--output-schema")
            != str(directory / "output-schema.json")
            or option("--output-last-message")
            != str(directory / "last-message.json")
            or arguments[-1] != "-"
            or (
                invocation_native_session_ref is None
                and "resume" in arguments
            )
            or (
                invocation_native_session_ref is not None
                and arguments[-3:]
                != ["resume", invocation_native_session_ref, "-"]
            )
        ):
            raise ValueError("deepfetch reconciliation argv identity invalid")

    def _execute_protocol(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        checkpoint_path: Path,
        checkpoint: _DeepFetchProtocolCheckpoint,
    ) -> DeepFetchResult:
        while checkpoint.phase != "finalized":
            if checkpoint.phase == "pending_acquisition":
                assert checkpoint.pending_acquisition is not None
                effect = _acquisition_effect_from_checkpoint(
                    checkpoint.pending_acquisition
                )
                effect_phase = f"turn-{checkpoint.next_turn_number - 1}"
                execution = self._call_acquisition_operation(
                    request,
                    operation_id=ROOT_AGENT_ACQUISITION_OPERATION_IDS[1],
                    phase=effect_phase,
                    arguments={"effect_id": effect["effect_id"]},
                )
                if execution.get("status") in {
                    "unknown_outcome",
                    "waiting_user",
                }:
                    execution = self._call_acquisition_operation(
                        request,
                        operation_id=ROOT_AGENT_ACQUISITION_OPERATION_IDS[0],
                        phase=effect_phase,
                        arguments=effect,
                    )
                if execution.get("status") == "waiting_user":
                    raise DeepFetchUnavailable(
                        "deepfetch_acquisition_waiting_user",
                        durable_outcome="pending",
                        native_session_ref=checkpoint.native_session_ref,
                    )
                try:
                    item_proof = _semantic_acquisition_item_proof(
                        execution,
                        effect_id=cast(str, effect["effect_id"]),
                        phase=effect_phase,
                        target=cast(dict[str, object], effect["target"]),
                    )
                except DeepFetchUnavailable as error:
                    # Acquisition already committed this immutable request.  A
                    # proof failure cannot be repaired by reopening the same
                    # DeepFetch attempt or replaying the Acquisition Provider.
                    raise DeepFetchUnavailable(
                        error.code,
                        durable_outcome="pending",
                        native_session_ref=checkpoint.native_session_ref,
                    ) from error
                acquisition_ids = (
                    *checkpoint.acquisition_request_ids,
                    cast(str, item_proof["request_id"]),
                )
                checkpoint = replace(
                    checkpoint,
                    phase="ready_for_turn",
                    acquisition_request_ids=acquisition_ids,
                    acquisition_item_proofs=(
                        *checkpoint.acquisition_item_proofs,
                        item_proof,
                    ),
                    pending_acquisition=None,
                    next_prompt=self._acquisition_result_prompt(
                        public_root,
                        execution,
                        item_proof,
                    ),
                )
                _write_protocol_checkpoint(checkpoint_path, checkpoint)
                continue

            assert checkpoint.next_prompt is not None
            turn_number = checkpoint.next_turn_number
            provider_job_ref = (
                None
                if request.job_ref is None
                else f"{request.job_ref}:v4-turn:{turn_number}"
            )
            if provider_job_ref is not None:
                self._register_provider_turn(
                    root_job_ref=request.job_ref,
                    turn_number=turn_number,
                    provider_job_ref=provider_job_ref,
                    runtime_binding_hash=canonical_hash(
                        request.runtime_binding.as_dict()
                    ),
                )
            turn_request = replace(
                request,
                native_session_ref=checkpoint.native_session_ref,
                job_ref=provider_job_ref,
            )
            web_gate = checkpoint.next_prompt.startswith(
                "web_evidence_gate=v1\n"
            )
            raw, native_session_ref, turn_evidence = self._invoke(
                turn_request,
                checkpoint.next_prompt,
                phase=f"turn-{turn_number}",
                output_schema=(
                    _deepfetch_web_evidence_gate_output_schema()
                    if web_gate
                    else _deepfetch_output_schema()
                ),
                timeout_seconds=None,
            )
            if checkpoint.native_session_ref is None:
                # A terminal provider call can establish the native session even
                # when its protocol envelope is invalid. Persist that fact before
                # parsing so a retry cannot silently fork a replacement session.
                checkpoint = replace(
                    checkpoint,
                    native_session_ref=native_session_ref,
                )
                _write_protocol_checkpoint(checkpoint_path, checkpoint)
            elif checkpoint.native_session_ref != native_session_ref:
                raise DeepFetchUnavailable("deepfetch_native_session_changed")
            evidence_parts = (*checkpoint.evidence_parts, turn_evidence)
            if web_gate:
                _validate_web_evidence_gate_result(raw)
                _merge_web_evidence([turn_evidence])
                checkpoint = replace(
                    checkpoint,
                    phase="ready_for_turn",
                    native_session_ref=native_session_ref,
                    next_turn_number=turn_number + 1,
                    evidence_parts=evidence_parts,
                    next_prompt=self._initial_prompt(
                        request,
                        public_root=public_root,
                        private_root=checkpoint_path.parent,
                    ),
                )
                _write_protocol_checkpoint(checkpoint_path, checkpoint)
                continue
            if raw.get("action") == "finalize":
                _validate_final_envelope_shape(raw)
                checkpoint = replace(
                    checkpoint,
                    phase="finalized",
                    native_session_ref=native_session_ref,
                    next_turn_number=turn_number + 1,
                    evidence_parts=evidence_parts,
                    next_prompt=None,
                    final_envelope=raw,
                )
                break
            if raw.get("action") != "acquire":
                raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
            effect = _validated_v4_acquisition_effect(raw)
            if any(
                proof["effect_id"] == effect["effect_id"]
                and proof["phase"] == f"turn-{turn_number}"
                for proof in checkpoint.acquisition_item_proofs
            ):
                raise DeepFetchUnavailable(
                    "deepfetch_acquisition_identity_duplicate"
                )
            checkpoint = replace(
                checkpoint,
                phase="pending_acquisition",
                native_session_ref=native_session_ref,
                next_turn_number=turn_number + 1,
                evidence_parts=evidence_parts,
                pending_acquisition=effect,
                next_prompt=None,
            )
            # The exact effect is durable before the Owner-side effect begins.
            _write_protocol_checkpoint(checkpoint_path, checkpoint)

        assert checkpoint.final_envelope is not None
        assert checkpoint.native_session_ref is not None
        web_evidence = _merge_web_evidence(list(checkpoint.evidence_parts))
        authoritative_acquisition_proofs = (
            self._query_authoritative_acquisition_proofs(
                request,
                checkpoint.acquisition_item_proofs,
                native_session_ref=checkpoint.native_session_ref,
            )
        )
        if (
            authoritative_acquisition_proofs
            != checkpoint.acquisition_item_proofs
        ):
            raise DeepFetchUnavailable(
                "deepfetch_hosted_acquisition_proof_mismatch"
            )
        _precheck_public_artifact_resource_limits(public_root)
        _run_exact_papers_validator(self._skill_root, public_root)
        imported = _import_v4_public_artifacts(
            public_root,
            checkpoint.final_envelope,
            acquisition_request_ids=checkpoint.acquisition_request_ids,
            acquisition_item_proofs=authoritative_acquisition_proofs,
        )
        web_evidence = {**web_evidence, "prototype": imported[6]}
        result = DeepFetchResult(
            completion=cast(
                Literal["complete", "limited", "honest_empty"],
                imported[0],
            ),
            summary=imported[1],
            papers=imported[2],
            fulltexts=imported[3],
            limitations=imported[4],
            native_session_ref=checkpoint.native_session_ref,
            adapter_kind="codex_cli",
            web_evidence=web_evidence,
            papers_ledger=imported[5],
        )
        validate_deepfetch_result(request, result)
        # "finalized" is durable only after the exact v4 validator, artifact
        # import, Acquisition artifact/Reader-output checks, native child-tool
        # provenance, and public-result validation all pass.
        _write_protocol_checkpoint(checkpoint_path, checkpoint)
        return result

    def _query_authoritative_acquisition_proofs(
        self,
        request: DeepFetchProviderRequest,
        recorded_proofs: tuple[dict[str, object], ...],
        *,
        native_session_ref: str | None,
    ) -> tuple[dict[str, object], ...]:
        if not recorded_proofs:
            return ()
        proofs: list[dict[str, object]] = []
        try:
            for recorded in recorded_proofs:
                effect_id = cast(str, recorded["effect_id"])
                phase = cast(str, recorded["phase"])
                execution = self._call_acquisition_operation(
                    request,
                    operation_id=ROOT_AGENT_ACQUISITION_OPERATION_IDS[1],
                    phase=phase,
                    arguments={"effect_id": effect_id},
                )
                if execution.get("status") == "unknown_outcome":
                    raise DeepFetchUnavailable(
                        "deepfetch_acquisition_reattestation_required"
                    )
                current = _semantic_acquisition_item_proof(
                    execution,
                    effect_id=effect_id,
                    phase=phase,
                    target_hash=cast(str, recorded["target_hash"]),
                )
                proofs.append(current)
        except DeepFetchUnavailable as error:
            raise DeepFetchUnavailable(
                error.code,
                durable_outcome="pending",
                native_session_ref=native_session_ref,
            ) from error
        return tuple(proofs)

    def _call_acquisition_operation(
        self,
        request: DeepFetchProviderRequest,
        *,
        operation_id: str,
        phase: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        try:
            return self._root_resident_mcp.call_operation(
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                capability_binding_hash=canonical_hash(
                    request.runtime_binding.as_dict()
                ),
                phase=phase,
                job_ref=request.job_ref,
                operation_id=operation_id,
                arguments=arguments,
            )
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error

    def _initial_prompt(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        private_root: Path,
    ) -> str:
        skill_entrypoint = self._skill_root / "SKILL.md"
        route_contract = ""
        if request.scope.get("literature_mode") == "oa_only":
            route_contract = (
                "本请求的 oa_only 是用户明确选择的主路线；跳过 "
                "institution/browser preflight，不得探测或评价机构访问可用性，"
                "不得描述为被迫、只能或降级到 OA。\n"
            )
        return (
            "你是绑定 fixed commit "
            f"{DEEPFETCH_PROTOTYPE_COMMIT} 的 DeepFetch v4 main agent。"
            f"开始前必须完整读取 {skill_entrypoint}，以及它直接链接的所有 reference；"
            "把该固定 bundle 作为行为规范，不得改写它。必须执行 Radar、Ledger、"
            "Quest-scoped Acquisition、independent Readers、Synthesis 与 "
            "scripts/papers.py finalize 闭环；不得把它扁平化成单体 Web Search/Fetch "
            "回答。主 Agent 负责发现、选择、ledger 和 summary，Acquisition 只负责"
            "合法获取；需要全文时返回只含 effect_id 和一个 target 的 action=acquire，"
            "并且只把共同 Acquisition effect 返回为 obtained 的同一 paper_id "
            "交给 Reader。每份已注册全文必须用原生 spawn_agent 启动一个独立 Reader，"
            "并 wait 到终态。最终 workflow.reader_assignments 的 reader_agent_ref "
            "填写 spawn_agent 返回的 task_name。"
            "不得声称创建 Quest/Question/Cycle、接纳 Evidence 或签发 receipt；"
            "不得把 Cookie、凭据、浏览器 profile、私有 manifest 或恢复状态写入"
            "公开目录。最终公开目录必须且只能包含 papers.json、summary.md、fulltext/。\n"
            f"{_DEEPFETCH_COMPLETION_RULES}\n"
            f"deepfetch_skill_root={self._skill_root}\n"
            f"public_output_root={public_root}\n"
            f"private_work_root={private_root}\n"
            f"request_ref={request.request_ref}\n"
            f"draft_revision={request.draft_revision}\n"
            f"draft_hash={request.draft_hash}\n"
            f"scope={canonical_json(request.scope)}\n"
            f"{route_contract}"
            "accepted_material_bindings="
            f"{canonical_json(list(request.accepted_material_bindings))}"
        )

    def _human_request_resume_prompt(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        private_root: Path,
    ) -> str:
        resume = request.human_request_resume
        if (
            not isinstance(resume, dict)
            or not isinstance(resume.get("effect_id"), str)
            or not isinstance(resume.get("request_ref"), str)
        ):
            raise DeepFetchUnavailable("deepfetch_human_request_resume_invalid")
        return (
            "human_request_resume=v1\n"
            "这是同一逻辑 DeepFetch Session 在人工回应后的恢复 Attempt。先调用 "
            "human_request.open.reconcile，使用下列 exact effect_id，"
            "读取 resolution 后再判断回应是否足够；不得把旧 open receipt 当作新"
            " waiter 的授权。随后继续当前研究任务，必要时可显式提出 successor "
            "HumanRequest。\n"
            f"human_request_effect_id={resume['effect_id']}\n"
            f"human_request_ref={resume['request_ref']}\n"
            + self._initial_prompt(
                request,
                public_root=public_root,
                private_root=private_root,
            )
        )

    def _web_evidence_gate_prompt(
        self,
        request: DeepFetchProviderRequest,
    ) -> str:
        """Open one live result before any expensive DeepFetch side effect."""

        return (
            "web_evidence_gate=v1\n"
            "这是 DeepFetch 的短 Web Evidence Gate，不是研究成果。首先完成以下动作："
            "根据 scope 做至少一次真实 Web Search，并立即 Open/Fetch 至少一个检索结果。"
            "Codex 默认工具能力保持可用，但本 Gate 只验收上述 Search 与 Open/Fetch；"
            "通过前不要展开 Acquisition、Reader 或正式成果生成。"
            "完成 Search 与 Open/Fetch 后，只返回 schema 要求的 web_evidence_ready。\n"
            f"request_ref={request.request_ref}\n"
            f"draft_revision={request.draft_revision}\n"
            f"draft_hash={request.draft_hash}\n"
            f"scope={canonical_json(request.scope)}"
        )

    def _acquisition_result_prompt(
        self,
        public_root: Path,
        execution: dict[str, object],
        item_proof: dict[str, object],
    ) -> str:
        return (
            "继续同一 DeepFetch v4 main agent Session。以下是共同 Acquisition effect "
            "对上一精确 target 返回的紧凑结果；把 request_id 当作只读 receipt，"
            "继续 Radar/Ledger/Readers，必要时提出一个新的单 target effect，"
            "最终运行固定 bundle 的 scripts/papers.py finalize。每个 Reader 的 "
            "reader_agent_ref 使用原生 spawn_agent 返回的 task_name。"
            "Reader 和 "
            "public fulltext/ 必须读取 obtained path 并逐字节复制同一 verified artifact；"
            "不得按同一 paper_id 自造或替换正文 bytes。\n"
            f"{_DEEPFETCH_COMPLETION_RULES}\n"
            f"deepfetch_skill_root={self._skill_root}\n"
            f"public_output_root={public_root}\n"
            f"acquisition_request_id={execution['request_id']}\n"
            "acquisition_artifact_proofs="
            f"{canonical_json([item_proof])}\n"
            "acquisition_result="
            f"{canonical_json(execution['result'])}"
        )

    def _invoke(
        self,
        request: DeepFetchProviderRequest,
        prompt: str,
        *,
        phase: str,
        output_schema: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        if not self._root_resident_mcp.enabled:
            return self._invoke_with_access(
                request,
                prompt,
                output_schema=output_schema,
                timeout_seconds=timeout_seconds,
                access=None,
            )
        try:
            channel_phase = (
                request.human_request_resume["phase"]
                if request.human_request_resume is not None
                else phase
            )
            channel_key, access = self._root_resident_mcp.acquire(
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                capability_binding_hash=canonical_hash(
                    request.runtime_binding.as_dict()
                ),
                phase=channel_phase,
                job_ref=request.job_ref,
            )
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error
        try:
            result = self._invoke_with_access(
                request,
                prompt,
                output_schema=output_schema,
                timeout_seconds=timeout_seconds,
                access=access,
            )
        except DeepFetchUnavailable as error:
            if error.durable_outcome != "pending":
                try:
                    self._root_resident_mcp.release(channel_key)
                except RootResidentMcpError as release_error:
                    raise DeepFetchUnavailable(release_error.code) from error
            raise
        except Exception:
            try:
                self._root_resident_mcp.release(channel_key)
            except RootResidentMcpError as error:
                raise DeepFetchUnavailable(error.code) from error
            raise
        try:
            self._root_resident_mcp.release(channel_key)
        except RootResidentMcpError as error:
            raise DeepFetchUnavailable(error.code) from error
        return result

    def _invoke_with_access(
        self,
        request: DeepFetchProviderRequest,
        prompt: str,
        *,
        output_schema: dict[str, object] | None,
        timeout_seconds: float | None,
        access: RootResidentMcpAccess | None,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        schema = output_schema or _deepfetch_output_schema()
        if request.job_ref is not None and callable(
            getattr(self._runner, "run_durable_job", None)
        ):
            return self._invoke_durable(
                request,
                prompt,
                output_schema=schema,
                timeout_seconds=timeout_seconds,
                access=access,
            )
        root_job_ref = request.job_ref or f"{request.run_ref}:direct"
        agent_workspace = self._agent_workspace_for(
            root_job_ref,
            canonical_hash(request.runtime_binding.as_dict()),
        )
        with tempfile.TemporaryDirectory(
            prefix="deepfetch-", dir=self._workspace
        ) as raw_directory:
            directory = Path(raw_directory)
            schema_path = directory / "output-schema.json"
            result_path = directory / "last-message.json"
            schema_path.write_text(
                canonical_json(schema), encoding="utf-8"
            )
            capability_profile = root_capability_profile("deepfetch")
            entry_path: RootCapabilityEntryPath = (
                "resume"
                if request.native_session_ref is not None
                else "initial"
            )
            pre_turn_diagnostics = self._root_capability_diagnostics(
                entry_path=entry_path
            )
            argv = [
                self._executable,
                "exec",
                "--skip-git-repo-check",
                "--strict-config",
                *(
                    (
                        "--config",
                        "mcp_servers={}",
                        "--config",
                        f'mcp_servers.meta_research.url="{access.url}"',
                        "--config",
                        "mcp_servers.meta_research.bearer_token_env_var="
                        '"META_RESEARCH_MCP_TOKEN"',
                        "--config",
                        "mcp_servers.meta_research.required=true",
                        "--config",
                        "mcp_servers.meta_research."
                        'default_tools_approval_mode="approve"',
                    )
                    if access is not None
                    else ()
                ),
                "--config",
                'approval_policy="never"',
                "--config",
                CODEX_REASONING_EFFORT_CONFIG,
                *(
                    (
                        "--config",
                        'shell_environment_policy.inherit="none"',
                    )
                    if access is not None
                    else ()
                ),
                *capability_profile.codex_arguments(),
                "--sandbox",
                self._sandbox_mode,
                "--model",
                self._model_ref,
                "--cd",
                str(agent_workspace),
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
            ]
            if request.native_session_ref is None:
                argv.append("-")
            else:
                argv.extend(["resume", request.native_session_ref, "-"])
            try:
                environment = (
                    semantic_mcp_environment(access.token)
                    if access is not None
                    else None
                )
                run_job = getattr(self._runner, "run_job", None)
                if request.job_ref is not None and callable(run_job):
                    completed = (
                        run_job(
                            request.job_ref,
                            argv,
                            prompt,
                            timeout_seconds,
                            environment,
                        )
                        if environment is not None
                        else run_job(
                            request.job_ref, argv, prompt, timeout_seconds
                        )
                    )
                else:
                    completed = (
                        self._runner(
                            argv, prompt, timeout_seconds, environment
                        )
                        if environment is not None
                        else self._runner(argv, prompt, timeout_seconds)
                    )
            except _ProcessStopped as error:
                raise DeepFetchUnavailable("deepfetch_provider_stopped") from error
            except FileNotFoundError as error:
                raise DeepFetchUnavailable("codex_cli_unavailable") from error
            except subprocess.TimeoutExpired as error:
                raise DeepFetchUnavailable("codex_deepfetch_timeout") from error
            except DraftingUnavailable as error:
                if error.code == "codex_output_too_large":
                    raise DeepFetchUnavailable(
                        "codex_deepfetch_output_too_large"
                    ) from error
                raise
            except OSError as error:
                raise DeepFetchUnavailable("codex_deepfetch_io_unavailable") from error
            if completed.returncode != 0:
                raise DeepFetchUnavailable("codex_deepfetch_failed")
            if _text_exceeds_limit(
                completed.stdout, DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
            ) or _text_exceeds_limit(
                completed.stderr, DEEPFETCH_PROVIDER_STREAM_MAX_BYTES
            ):
                raise DeepFetchUnavailable("codex_deepfetch_output_too_large")
            try:
                if result_path.stat().st_size > PROVIDER_RESULT_MAX_BYTES:
                    raise DeepFetchUnavailable("codex_deepfetch_output_too_large")
                decoded = json.loads(result_path.read_text(encoding="utf-8"))
            except DeepFetchUnavailable:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DeepFetchUnavailable("codex_deepfetch_output_invalid") from error
            if not isinstance(decoded, dict):
                raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
            observed_native_session_ref = _thread_id(completed.stdout)
            if observed_native_session_ref is None:
                raise DeepFetchUnavailable("codex_deepfetch_session_ref_missing")
            if (
                request.native_session_ref is not None
                and request.native_session_ref != observed_native_session_ref
            ):
                raise DeepFetchUnavailable("deepfetch_native_session_changed")
            self._record_root_operation_diagnostics(
                source_ref=(
                    f"{root_job_ref}:native-session:"
                    f"{observed_native_session_ref}:prompt:{canonical_hash(prompt)}"
                ),
                phase="direct",
                pre_turn=pre_turn_diagnostics,
                stdout=completed.stdout,
            )
            web_evidence = _verified_turn_evidence(completed.stdout)
            return (
                cast(dict[str, object], decoded),
                observed_native_session_ref,
                web_evidence,
            )

    def _invoke_durable(
        self,
        request: DeepFetchProviderRequest,
        prompt: str,
        *,
        output_schema: dict[str, object],
        timeout_seconds: float | None,
        access: RootResidentMcpAccess | None = None,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        """Reconcile one logical provider operation across daemon Attempts."""

        assert request.job_ref is not None
        operation_root = self._provider_operation_root(
            request.job_ref,
            canonical_hash(request.runtime_binding.as_dict()),
        )
        try:
            _key_path, transport_key = ensure_transport_key(self._workspace)
        except (OSError, ProviderSupervisorError) as error:
            raise DeepFetchUnavailable("deepfetch_provider_spool_invalid") from error
        native_session_ref = request.native_session_ref
        trace_parts: list[str] = []
        segment_number = 0
        while True:
            segment_name = (
                "initial" if segment_number == 0 else f"resume-{segment_number}"
            )
            directory = operation_root / f"deepfetch-{segment_name}"
            outcome = self._run_durable_segment(
                directory=directory,
                segment_name=segment_name,
                job_ref=request.job_ref,
                request=request,
                prompt=prompt,
                output_schema=output_schema,
                timeout_seconds=timeout_seconds,
                native_session_ref=native_session_ref,
                transport_key=transport_key,
                access=access,
            )
            trace_parts.append(outcome[2])
            if outcome[0] == "completed":
                decoded = outcome[1]
                assert isinstance(decoded, dict)
                completed_session = outcome[3]
                assert isinstance(completed_session, str)
                try:
                    evidence = _verified_turn_evidence("\n".join(trace_parts))
                except DeepFetchUnavailable as error:
                    raise error.as_verified_terminal(completed_session) from error
                return decoded, completed_session, evidence
            recovered_session = outcome[3]
            if not isinstance(recovered_session, str) or not recovered_session:
                if outcome[0] == "stopped":
                    raise DeepFetchUnavailable(
                        "deepfetch_provider_stopped_before_session",
                        durable_outcome="terminal",
                    )
                raise DeepFetchUnavailable("codex_deepfetch_session_ref_missing")
            if native_session_ref is not None and (
                native_session_ref != recovered_session
            ):
                raise DeepFetchUnavailable("deepfetch_native_session_changed")
            native_session_ref = recovered_session
            segment_number += 1

    def _run_durable_segment(
        self,
        *,
        directory: Path,
        segment_name: str,
        job_ref: str,
        request: DeepFetchProviderRequest,
        prompt: str,
        output_schema: dict[str, object],
        timeout_seconds: float | None,
        native_session_ref: str | None,
        transport_key: bytes,
        access: RootResidentMcpAccess | None = None,
    ) -> tuple[str, dict[str, object] | None, str, str | None]:
        directory.mkdir(parents=True, exist_ok=True)
        schema = output_schema
        capability_profile = root_capability_profile("deepfetch")
        entry_path: RootCapabilityEntryPath = (
            "resume" if native_session_ref is not None else "initial"
        )
        fresh_diagnostics = self._root_capability_diagnostics(
            entry_path=entry_path
        )
        invocation = {
            "schema_ref": _DEEPFETCH_PROVIDER_OPERATION_SCHEMA,
            "job_ref": job_ref,
            "segment_name": segment_name,
            "request_ref": request.request_ref,
            "correlation_ref": request.correlation_ref,
            "draft_hash": request.draft_hash,
            "scope_hash": request.scope_hash,
            "runtime_binding_hash": canonical_hash(request.runtime_binding.as_dict()),
            "native_session_ref": native_session_ref,
            "prompt_hash": canonical_hash(prompt),
            "output_schema_hash": canonical_hash(schema),
            "model_ref": self._model_ref,
            "root_capability_profile": capability_profile.as_dict(),
            "root_capability_profile_hash": capability_profile.digest,
        }
        if access is not None:
            invocation.update(
                {
                    "mcp_url": access.url,
                    "mcp_scope_binding_hash": access.scope_binding_hash,
                }
            )
        invocation_hash = canonical_hash(invocation)
        invocation_path = directory / "invocation.json"
        envelope = {
            "payload": invocation,
            "seal": hmac.new(
                transport_key,
                canonical_json(invocation).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        }
        encoded_invocation = canonical_json(envelope)
        created = _write_exclusive_text(invocation_path, encoded_invocation)
        if not created:
            try:
                persisted_invocation = read_transport_envelope(
                    invocation_path, transport_key
                )
            except ProviderSupervisorError as error:
                raise DeepFetchUnavailable(
                    "deepfetch_provider_spool_invalid"
                ) from error
            if persisted_invocation != invocation:
                raise DeepFetchUnavailable("deepfetch_provider_identity_conflict")
            invocation = persisted_invocation
            invocation_hash = canonical_hash(invocation)

        prompt_path = directory / "prompt.txt"
        schema_path = directory / "output-schema.json"
        stdout_path = directory / "stdout.jsonl"
        result_path = directory / "last-message.json"
        receipt_path = directory / "supervisor-exit.json"
        _ensure_durable_text(prompt_path, prompt)
        _ensure_durable_text(schema_path, canonical_json(schema))
        effect_paths = (
            directory / "supervisor-ready.json",
            directory / "provider-started.json",
            stdout_path,
            result_path,
        )
        if not receipt_path.exists():
            if not created and any(path.exists() for path in effect_paths):
                raise DeepFetchUnavailable(
                    "deepfetch_provider_reconciliation_pending",
                    durable_outcome="pending",
                )
            durable_argv_kwargs = {
                "job_ref": job_ref,
                "runtime_binding_hash": canonical_hash(
                    request.runtime_binding.as_dict()
                ),
                "schema_path": schema_path,
                "result_path": result_path,
                "native_session_ref": native_session_ref,
            }
            argv = (
                self._durable_argv(**durable_argv_kwargs, mcp_url=access.url)
                if access is not None
                else self._durable_argv(**durable_argv_kwargs)
            )
            supervisor_request_path = directory / "supervisor-request.json"
            try:
                write_supervisor_request(
                    supervisor_request_path,
                    {
                        "schema_ref": SUPERVISOR_REQUEST_SCHEMA,
                        "invocation_hash": invocation_hash,
                        "argv": argv,
                        "timeout_seconds": timeout_seconds,
                        "stream_max_bytes": DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
                        "result_max_bytes": PROVIDER_RESULT_MAX_BYTES,
                        "prompt_path": str(prompt_path),
                        "schema_path": str(schema_path),
                        "stdout_path": str(stdout_path),
                        "result_path": str(result_path),
                        "lock_path": str(directory / "supervisor.lock"),
                        "ready_path": str(directory / "supervisor-ready.json"),
                        "started_path": str(directory / "provider-started.json"),
                        "receipt_path": str(receipt_path),
                        "stop_path": str(directory / "supervisor-stop.json"),
                    },
                    transport_key,
                )
                durable_job = self._runner.run_durable_job
                durable_arguments = (
                    job_ref,
                    argv,
                    prompt,
                    timeout_seconds,
                    stdout_path,
                    directory / "pid.json",
                    supervisor_request_path,
                )
                if access is not None:
                    durable_arguments = (
                        *durable_arguments,
                        semantic_mcp_environment(access.token),
                    )
                if isinstance(self._runner, _CancellableProcessRunner):
                    durable_job(
                        *durable_arguments,
                        stdout_max_bytes=DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
                    )
                else:
                    durable_job(*durable_arguments)
            except _ProcessStopped as error:
                raise DeepFetchUnavailable(
                    "deepfetch_provider_stopped", durable_outcome="pending"
                ) from error
            except FileNotFoundError as error:
                raise DeepFetchUnavailable("codex_cli_unavailable") from error
            except subprocess.TimeoutExpired as error:
                raise DeepFetchUnavailable(
                    "deepfetch_provider_reconciliation_pending",
                    durable_outcome="pending",
                ) from error
            except ProviderSupervisorError as error:
                raise DeepFetchUnavailable(
                    "deepfetch_provider_spool_invalid"
                ) from error
            except OSError as error:
                if any(path.exists() for path in effect_paths):
                    raise DeepFetchUnavailable(
                        "deepfetch_provider_reconciliation_pending",
                        durable_outcome="pending",
                    ) from error
                raise DeepFetchUnavailable("codex_deepfetch_io_unavailable") from error
        outcome = self._read_durable_segment(
            directory=directory,
            invocation_hash=invocation_hash,
            native_session_ref=native_session_ref,
            transport_key=transport_key,
        )
        self._record_root_operation_diagnostics(
            source_ref=f"{job_ref}:{segment_name}",
            phase=segment_name,
            pre_turn=fresh_diagnostics,
            stdout=outcome[2],
        )
        return outcome

    def _durable_argv(
        self,
        *,
        job_ref: str,
        runtime_binding_hash: str,
        schema_path: Path,
        result_path: Path,
        native_session_ref: str | None,
        mcp_url: str | None = None,
    ) -> list[str]:
        agent_workspace = self._agent_workspace_for(
            job_ref, runtime_binding_hash
        )
        capability_profile = root_capability_profile("deepfetch")
        argv = [
            self._executable,
            "exec",
            "--skip-git-repo-check",
            "--strict-config",
            *(
                (
                    "--config",
                    "mcp_servers={}",
                    "--config",
                    f'mcp_servers.meta_research.url="{mcp_url}"',
                    "--config",
                    "mcp_servers.meta_research.bearer_token_env_var="
                    '"META_RESEARCH_MCP_TOKEN"',
                    "--config",
                    "mcp_servers.meta_research.required=true",
                    "--config",
                    "mcp_servers.meta_research."
                    'default_tools_approval_mode="approve"',
                )
                if mcp_url is not None
                else ()
            ),
            "--config",
            'approval_policy="never"',
            "--config",
            CODEX_REASONING_EFFORT_CONFIG,
            *(
                (
                    "--config",
                    'shell_environment_policy.inherit="none"',
                )
                if mcp_url is not None
                else ()
            ),
            *capability_profile.codex_arguments(),
            "--sandbox",
            self._sandbox_mode,
            "--model",
            self._model_ref,
            "--cd",
            str(agent_workspace),
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        if native_session_ref is None:
            argv.append("-")
        else:
            argv.extend(["resume", native_session_ref, "-"])
        return argv

    def _read_durable_segment(
        self,
        *,
        directory: Path,
        invocation_hash: str,
        native_session_ref: str | None,
        transport_key: bytes,
    ) -> tuple[str, dict[str, object] | None, str, str | None]:
        receipt_path = directory / "supervisor-exit.json"
        if not receipt_path.exists():
            raise DeepFetchUnavailable(
                "deepfetch_provider_reconciliation_pending",
                durable_outcome="pending",
            )
        prompt_path = directory / "prompt.txt"
        schema_path = directory / "output-schema.json"
        stdout_path = directory / "stdout.jsonl"
        result_path = directory / "last-message.json"
        try:
            receipt, _envelope = read_verified_exit_receipt(
                receipt_path,
                key=transport_key,
                invocation_hash=invocation_hash,
                prompt_path=prompt_path,
                schema_path=schema_path,
                stdout_path=stdout_path,
                result_path=result_path,
            )
            if stdout_path.stat().st_size > DEEPFETCH_PROVIDER_STREAM_MAX_BYTES:
                raise DeepFetchUnavailable("codex_deepfetch_output_too_large")
            stdout = stdout_path.read_text(encoding="utf-8")
        except DeepFetchUnavailable:
            raise
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise DeepFetchUnavailable("deepfetch_provider_spool_invalid") from error
        observed_session_ref = _thread_id(stdout)
        if receipt["termination_reason"] == "output_limit":
            raise DeepFetchUnavailable(
                "codex_deepfetch_output_too_large",
                durable_outcome="terminal",
                native_session_ref=observed_session_ref,
            )
        if receipt["termination_reason"] == "stopped":
            return "stopped", None, stdout, observed_session_ref
        if receipt["termination_reason"] == "timeout":
            raise DeepFetchUnavailable(
                "codex_deepfetch_timeout",
                durable_outcome="terminal",
                native_session_ref=observed_session_ref,
            )
        if receipt["termination_reason"] != "completed" or receipt["returncode"] != 0:
            raise DeepFetchUnavailable(
                "codex_deepfetch_failed",
                durable_outcome="terminal",
                native_session_ref=observed_session_ref,
            )
        if observed_session_ref is None:
            raise DeepFetchUnavailable(
                "codex_deepfetch_session_ref_missing", durable_outcome="terminal"
            )
        if (
            native_session_ref is not None
            and observed_session_ref != native_session_ref
        ):
            raise DeepFetchUnavailable("deepfetch_native_session_changed")
        try:
            if result_path.stat().st_size > PROVIDER_RESULT_MAX_BYTES:
                raise DeepFetchUnavailable("codex_deepfetch_output_too_large")
            decoded = json.loads(result_path.read_text(encoding="utf-8"))
        except DeepFetchUnavailable as error:
            raise error.as_verified_terminal(observed_session_ref) from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeepFetchUnavailable(
                "codex_deepfetch_output_invalid",
                durable_outcome="terminal",
                native_session_ref=observed_session_ref,
            ) from error
        if not isinstance(decoded, dict):
            raise DeepFetchUnavailable(
                "codex_deepfetch_output_invalid",
                durable_outcome="terminal",
                native_session_ref=observed_session_ref,
            )
        return (
            "completed",
            cast(dict[str, object], decoded),
            stdout,
            observed_session_ref,
        )


def _deepfetch_skill_root() -> Path:
    root = Path(__file__).resolve().parent / "skills" / "deepfetch_v4"
    required = (
        root / "SKILL.md",
        root / "references" / "agents.md",
        root / "references" / "ledger-tools.md",
        root / "references" / "openalex.md",
        root / "references" / "papers-json.md",
        root / "references" / "summary.md",
        root / "scripts" / "papers.py",
        root / "scripts" / "openalex.py",
    )
    if any(not path.is_file() for path in required):
        raise DeepFetchUnavailable("deepfetch_v4_skill_bundle_unavailable")
    return root


def _deepfetch_skill_bundle_hash(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if not paths:
            raise OSError("empty bundle")
        for path in paths:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            content = path.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError) as error:
        raise DeepFetchUnavailable("deepfetch_v4_skill_bundle_unavailable") from error
    return digest.hexdigest()


def _deepfetch_protocol_identity(request: DeepFetchProviderRequest) -> str:
    return canonical_hash(
        {
            "schema_ref": "meta-research/deepfetch-v4-protocol-identity/v1",
            "request_ref": request.request_ref,
            "initialization_id": request.initialization_id,
            "correlation_ref": request.correlation_ref,
            "draft_revision": request.draft_revision,
            "draft_hash": request.draft_hash,
            "scope_hash": request.scope_hash,
            "accepted_material_bindings": list(
                request.accepted_material_bindings
            ),
            "authorization_receipt": (
                request.authorization_receipt.as_public_dict()
            ),
            "runtime_binding_hash": canonical_hash(
                request.runtime_binding.as_dict()
            ),
            "run_ref": request.run_ref,
            "root_session_ref": request.root_session_ref,
        }
    )


def _load_or_create_protocol_checkpoint(
    path: Path,
    *,
    identity_hash: str,
    native_session_ref: str | None,
    initial_prompt: str,
) -> _DeepFetchProtocolCheckpoint:
    if path.exists():
        checkpoint = _read_protocol_checkpoint(path, identity_hash)
        if (
            checkpoint.native_session_ref is not None
            and native_session_ref is not None
            and checkpoint.native_session_ref != native_session_ref
        ):
            raise DeepFetchUnavailable("deepfetch_native_session_changed")
        if checkpoint.native_session_ref is None and native_session_ref is not None:
            checkpoint = replace(
                checkpoint,
                native_session_ref=native_session_ref,
            )
            _write_protocol_checkpoint(path, checkpoint)
        return checkpoint
    checkpoint = _DeepFetchProtocolCheckpoint(
        identity_hash=identity_hash,
        phase="ready_for_turn",
        native_session_ref=native_session_ref,
        next_turn_number=0,
        evidence_parts=(),
        acquisition_request_ids=(),
        acquisition_item_proofs=(),
        pending_acquisition=None,
        next_prompt=initial_prompt,
        final_envelope=None,
    )
    _write_protocol_checkpoint(path, checkpoint)
    return checkpoint


def _read_protocol_checkpoint(
    path: Path,
    identity_hash: str,
) -> _DeepFetchProtocolCheckpoint:
    try:
        if path.is_symlink() or path.stat().st_size > 5_000_000:
            raise OSError("unsafe checkpoint")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid") from error
    required = {
        "schema_ref",
        "identity_hash",
        "phase",
        "native_session_ref",
        "next_turn_number",
        "evidence_parts",
        "acquisition_request_ids",
        "acquisition_item_proofs",
        "pending_acquisition",
        "next_prompt",
        "final_envelope",
    }
    if not isinstance(value, dict):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    if (
        value.get("schema_ref") != DEEPFETCH_PROTOCOL_CHECKPOINT_SCHEMA
        or set(value) != required
    ):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    acquisition_item_proofs = value.get("acquisition_item_proofs")
    if value.get("identity_hash") != identity_hash:
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    phase = value.get("phase")
    native_session_ref = value.get("native_session_ref")
    next_turn_number = value.get("next_turn_number")
    evidence_parts = value.get("evidence_parts")
    acquisition_request_ids = value.get("acquisition_request_ids")
    if (
        phase not in {"ready_for_turn", "pending_acquisition", "finalized"}
        or native_session_ref is not None
        and (not isinstance(native_session_ref, str) or not native_session_ref)
        or not isinstance(next_turn_number, int)
        or isinstance(next_turn_number, bool)
        or next_turn_number < 0
        or not isinstance(evidence_parts, list)
        or len(evidence_parts) != next_turn_number
        or not isinstance(acquisition_request_ids, list)
        or any(
            not isinstance(item, str) or not item
            for item in acquisition_request_ids
        )
        or len(set(acquisition_request_ids)) != len(acquisition_request_ids)
        or not isinstance(acquisition_item_proofs, list)
    ):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    validated_item_proofs = _validated_hosted_acquisition_item_proofs(
        acquisition_item_proofs,
        acquisition_request_ids=cast(list[str], acquisition_request_ids),
        require_every_request=True,
    )
    validated_evidence = tuple(
        _validated_turn_evidence_part(item) for item in evidence_parts
    )
    pending = value.get("pending_acquisition")
    prompt = value.get("next_prompt")
    final = value.get("final_envelope")
    if phase == "ready_for_turn":
        if (
            pending is not None
            or not isinstance(prompt, str)
            or not prompt
            or final is not None
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    elif phase == "pending_acquisition":
        if (
            not isinstance(pending, dict)
            or prompt is not None
            or final is not None
            or native_session_ref is None
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
        effect = _acquisition_effect_from_checkpoint(pending)
        pending_phase = f"turn-{next_turn_number - 1}"
        if any(
            proof["effect_id"] == effect["effect_id"]
            and proof["phase"] == pending_phase
            for proof in validated_item_proofs
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    else:
        if (
            pending is not None
            or prompt is not None
            or not isinstance(final, dict)
            or native_session_ref is None
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
        _validate_final_envelope_shape(final)
    return _DeepFetchProtocolCheckpoint(
        identity_hash=identity_hash,
        phase=cast(
            Literal["ready_for_turn", "pending_acquisition", "finalized"],
            phase,
        ),
        native_session_ref=cast(str | None, native_session_ref),
        next_turn_number=next_turn_number,
        evidence_parts=validated_evidence,
        acquisition_request_ids=tuple(cast(list[str], acquisition_request_ids)),
        acquisition_item_proofs=validated_item_proofs,
        pending_acquisition=cast(dict[str, object] | None, pending),
        next_prompt=cast(str | None, prompt),
        final_envelope=cast(dict[str, object] | None, final),
    )


def _write_protocol_checkpoint(
    path: Path,
    checkpoint: _DeepFetchProtocolCheckpoint,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = canonical_json(checkpoint.as_dict())
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".protocol.",
            suffix=".tmp",
            dir=path.parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            descriptor = -1
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        temporary = ""
        _fsync_directory(path.parent)
    except OSError as error:
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _checkpoint_native_session(path: Path, identity_hash: str) -> str | None:
    if not path.exists():
        return None
    try:
        return _read_protocol_checkpoint(path, identity_hash).native_session_ref
    except DeepFetchUnavailable:
        return None


def _acquisition_effect_from_checkpoint(
    value: dict[str, object],
) -> dict[str, object]:
    envelope = {
        "action": "acquire",
        "acquisition_request": value,
        "completion": None,
        "limitations": [],
        "workflow": {
            "prototype_commit": DEEPFETCH_PROTOTYPE_COMMIT,
            "main_agent_status": "running",
            "reader_assignments": [],
            "finalize_status": "pending",
            "finalized_at": None,
        },
    }
    try:
        return _validated_v4_acquisition_effect(envelope)
    except DeepFetchUnavailable as error:
        raise DeepFetchUnavailable(
            "deepfetch_protocol_checkpoint_invalid"
        ) from error


def _semantic_acquisition_item_proof(
    execution: dict[str, object],
    *,
    effect_id: str,
    phase: str,
    target: dict[str, object] | None = None,
    target_hash: str | None = None,
) -> dict[str, object]:
    result = execution.get("result")
    request_id = execution.get("request_id")
    status = execution.get("status")
    if (
        set(execution) != {"effect_id", "request_id", "status", "result"}
        or execution.get("effect_id") != effect_id
        or not isinstance(request_id, str)
        or not request_id
        or status not in {"obtained", "missing"}
        or not isinstance(result, dict)
        or result.get("status") != status
        or not isinstance(result.get("paper_id"), str)
    ):
        raise DeepFetchUnavailable("deepfetch_acquisition_result_invalid")
    if target is not None:
        if result["paper_id"] != target.get("paper_id"):
            raise DeepFetchUnavailable("deepfetch_acquisition_result_invalid")
        target_hash = canonical_hash(target)
    if not isinstance(target_hash, str) or len(target_hash) != 64:
        raise DeepFetchUnavailable("deepfetch_acquisition_result_invalid")
    proof: dict[str, object] = {
        "effect_id": effect_id,
        "phase": phase,
        "target_hash": target_hash,
        "request_id": request_id,
        "paper_id": result["paper_id"],
        "status": status,
        "path": None,
        "format": None,
        "sha256": None,
        "bytes": None,
    }
    if status == "obtained":
        if set(result) != {
            "paper_id",
            "status",
            "verified_path",
            "format",
            "content_sha256",
            "content_bytes",
        }:
            raise DeepFetchUnavailable("deepfetch_acquisition_result_invalid")
        path, artifact_format, observed_digest, observed_bytes = (
            _read_hosted_acquisition_artifact(
                result["verified_path"],
                result["format"],
            )
        )
        if (
            observed_digest != result["content_sha256"]
            or observed_bytes != result["content_bytes"]
        ):
            raise DeepFetchUnavailable("deepfetch_acquisition_artifact_drift")
        proof.update(
            {
                "path": path,
                "format": artifact_format,
                "sha256": observed_digest,
                "bytes": observed_bytes,
            }
        )
    elif set(result) != {"paper_id", "status", "failure"}:
        raise DeepFetchUnavailable("deepfetch_acquisition_result_invalid")
    return proof


def _read_hosted_acquisition_artifact(
    path_value: object,
    format_value: object,
    *,
    required_root: Path | None = None,
) -> tuple[str, str, str, int]:
    if (
        not isinstance(path_value, str)
        or not path_value
        or not Path(path_value).is_absolute()
        or format_value not in {"pdf", "html", "xml"}
    ):
        raise DeepFetchUnavailable("deepfetch_acquisition_artifact_invalid")
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
        required_root_resolved = (
            None if required_root is None else required_root.resolve(strict=True)
        )
        if (
            str(resolved) != path_value
            or path.is_symlink()
            or not path.is_file()
            or required_root is not None
            and (
                required_root.is_symlink()
                or not required_root.is_dir()
                or required_root_resolved != required_root
                or not resolved.is_relative_to(required_root_resolved)
            )
        ):
            raise OSError("unsafe hosted artifact")
        byte_count = path.stat().st_size
        if byte_count < 1 or byte_count > MAX_DEEPFETCH_FULLTEXT_FILE_BYTES:
            raise DeepFetchUnavailable("deepfetch_acquisition_artifact_invalid")
        with path.open("rb") as source:
            content = source.read(MAX_DEEPFETCH_FULLTEXT_FILE_BYTES + 1)
        if len(content) != byte_count:
            raise DeepFetchUnavailable("deepfetch_acquisition_artifact_invalid")
    except (OSError, ValueError) as error:
        raise DeepFetchUnavailable("deepfetch_acquisition_artifact_invalid") from error
    return (
        path_value,
        cast(str, format_value),
        hashlib.sha256(content).hexdigest(),
        byte_count,
    )


def _validated_hosted_acquisition_item_proofs(
    value: object,
    *,
    acquisition_request_ids: list[str],
    require_every_request: bool,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    proofs: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    request_ids = set(acquisition_request_ids)
    for proof in value:
        if not isinstance(proof, dict) or set(proof) != {
            "effect_id",
            "phase",
            "target_hash",
            "request_id",
            "paper_id",
            "status",
            "path",
            "format",
            "sha256",
            "bytes",
        }:
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
        request_id = proof.get("request_id")
        effect_id = proof.get("effect_id")
        phase = proof.get("phase")
        target_hash = proof.get("target_hash")
        paper_id = proof.get("paper_id")
        status = proof.get("status")
        identity = (str(request_id), str(paper_id))
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id not in request_ids
            or not isinstance(effect_id, str)
            or not effect_id
            or not isinstance(phase, str)
            or re.fullmatch(r"turn-[0-9]+", phase) is None
            or not isinstance(target_hash, str)
            or len(target_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in target_hash
            )
            or not isinstance(paper_id, str)
            or not paper_id
            or status not in {"obtained", "missing"}
            or identity in identities
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
        path = proof.get("path")
        artifact_format = proof.get("format")
        sha256 = proof.get("sha256")
        byte_count = proof.get("bytes")
        if status == "obtained":
            observed = _read_hosted_acquisition_artifact(path, artifact_format)
            if observed != (path, artifact_format, sha256, byte_count):
                raise DeepFetchUnavailable(
                    "deepfetch_protocol_checkpoint_invalid"
                )
        elif any(
            value is not None
            for value in (path, artifact_format, sha256, byte_count)
        ):
            raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
        identities.add(identity)
        proofs.append(
            {
                "effect_id": effect_id,
                "phase": phase,
                "target_hash": target_hash,
                "request_id": request_id,
                "paper_id": paper_id,
                "status": status,
                "path": path,
                "format": artifact_format,
                "sha256": sha256,
                "bytes": byte_count,
            }
        )
    if require_every_request and {proof["request_id"] for proof in proofs} != request_ids:
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    return tuple(proofs)


def _validate_final_envelope_shape(value: dict[str, object]) -> None:
    if (
        set(value)
        != {
            "action",
            "acquisition_request",
            "completion",
            "limitations",
            "workflow",
        }
        or value.get("action") != "finalize"
        or value.get("acquisition_request") is not None
    ):
        raise DeepFetchUnavailable("codex_deepfetch_output_invalid")


def _run_exact_papers_validator(skill_root: Path, public_root: Path) -> None:
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(skill_root / "scripts" / "papers.py"),
                "validate",
                "--out-dir",
                str(public_root),
                "--final",
            ],
            cwd=skill_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeepFetchUnavailable("deepfetch_papers_v4_validator_unavailable") from error
    if completed.returncode != 0:
        raise DeepFetchUnavailable("deepfetch_papers_v4_validator_failed")


def _precheck_public_artifact_resource_limits(public_root: Path) -> None:
    """Bound untrusted artifact bytes before the fixed validator opens them."""

    papers_path = public_root / "papers.json"
    summary_path = public_root / "summary.md"
    fulltext_root = public_root / "fulltext"
    try:
        if (
            not papers_path.is_file()
            or papers_path.is_symlink()
            or papers_path.stat().st_size > 10_000_000
            or not summary_path.is_file()
            or summary_path.is_symlink()
            or summary_path.stat().st_size > MAX_DEEPFETCH_SUMMARY_LENGTH * 4
            or not fulltext_root.is_dir()
            or fulltext_root.is_symlink()
        ):
            raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid")
        files = [path for path in fulltext_root.rglob("*") if path.is_file()]
        if len(files) > 10 or any(path.is_symlink() for path in fulltext_root.rglob("*")):
            raise DeepFetchUnavailable("deepfetch_fulltexts_too_large")
        sizes = [path.stat().st_size for path in files]
        if (
            any(size < 1 or size > MAX_DEEPFETCH_FULLTEXT_FILE_BYTES for size in sizes)
            or sum(sizes) > MAX_DEEPFETCH_FULLTEXT_TOTAL_BYTES
        ):
            raise DeepFetchUnavailable("deepfetch_fulltext_too_large")
    except DeepFetchUnavailable:
        raise
    except OSError as error:
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid") from error


def _write_exclusive_text(path: Path, value: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        _fsync_directory(path.parent)
        return True
    except FileExistsError:
        return False


def _ensure_durable_text(path: Path, value: str) -> None:
    if _write_exclusive_text(path, value):
        return
    try:
        persisted = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DeepFetchUnavailable("deepfetch_provider_spool_invalid") from error
    if persisted != value:
        raise DeepFetchUnavailable("deepfetch_provider_identity_conflict")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_v4_acquisition_effect(
    envelope: dict[str, object],
) -> dict[str, object]:
    if set(envelope) != {
        "action",
        "acquisition_request",
        "completion",
        "limitations",
        "workflow",
    } or envelope.get("completion") is not None:
        raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
    workflow = envelope.get("workflow")
    if (
        not isinstance(workflow, dict)
        or set(workflow)
        != {
            "prototype_commit",
            "main_agent_status",
            "reader_assignments",
            "finalize_status",
            "finalized_at",
        }
        or workflow.get("prototype_commit") != DEEPFETCH_PROTOTYPE_COMMIT
        or workflow.get("main_agent_status") != "running"
        or workflow.get("finalize_status") != "pending"
        or workflow.get("finalized_at") is not None
    ):
        raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid")
    value = envelope.get("acquisition_request")
    if not isinstance(value, dict) or set(value) != {"effect_id", "target"}:
        raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
    effect_id = value.get("effect_id")
    target = value.get("target")
    if (
        not isinstance(effect_id, str)
        or not effect_id
        or len(effect_id) > 128
        or not isinstance(target, dict)
        or not {"paper_id", "title", "source_urls"} <= set(target)
        or not set(target) <= {
            "paper_id",
            "title",
            "doi",
            "arxiv_id",
            "source_urls",
        }
    ):
        raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
    paper_id = target.get("paper_id")
    title = target.get("title")
    doi = target.get("doi")
    arxiv_id = target.get("arxiv_id")
    urls = target.get("source_urls")
    if (
        not isinstance(paper_id, str)
        or not paper_id
        or len(paper_id) > 512
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 2_000
        or doi is not None
        and (not isinstance(doi, str) or not doi or len(doi) > 512)
        or arxiv_id is not None
        and (
            not isinstance(arxiv_id, str)
            or not arxiv_id
            or len(arxiv_id) > 512
        )
        or not isinstance(urls, list)
        or len(urls) > 20
        or any(not isinstance(url, str) or len(url) > 4_096 for url in urls)
    ):
        raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
    normalized_target: dict[str, object] = {
        "paper_id": paper_id,
        "title": title,
        "source_urls": [
            _validated_public_url(
                url, "deepfetch_acquisition_source_url_invalid"
            )
            for url in urls
        ],
    }
    if isinstance(doi, str):
        normalized_target["doi"] = doi
    if isinstance(arxiv_id, str):
        normalized_target["arxiv_id"] = arxiv_id
    return {"effect_id": effect_id, "target": normalized_target}


def _merge_web_evidence(
    evidence_parts: list[dict[str, object]],
) -> dict[str, object]:
    if not evidence_parts:
        raise DeepFetchUnavailable("deepfetch_web_evidence_invalid")
    evidence = {
        "schema_ref": DEEPFETCH_WEB_EVIDENCE_SCHEMA,
        "search_event_count": sum(
            cast(int, value["search_event_count"]) for value in evidence_parts
        ),
        "fetch_event_count": sum(
            cast(int, value["fetch_event_count"]) for value in evidence_parts
        ),
        "trace_hash": canonical_hash(
            [value["trace_hash"] for value in evidence_parts]
        ),
    }
    validated = _validated_web_evidence(evidence)
    assert validated is not None
    return validated


def _import_v4_public_artifacts(
    public_root: Path,
    envelope: dict[str, object],
    *,
    acquisition_request_ids: tuple[str, ...],
    acquisition_item_proofs: tuple[dict[str, object], ...],
) -> tuple[
    Literal["complete", "limited", "honest_empty"],
    str,
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[str, ...],
    dict[str, object],
    dict[str, object],
]:
    """Validate v4's exact public surface and retain its lossless RM ledger."""

    if public_root.is_symlink() or not public_root.is_dir():
        raise DeepFetchUnavailable("deepfetch_public_artifacts_missing")
    try:
        entries = {path.name for path in public_root.iterdir()}
    except OSError as error:
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid") from error
    if entries != {"papers.json", "summary.md", "fulltext"}:
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid")
    papers_path = public_root / "papers.json"
    summary_path = public_root / "summary.md"
    fulltext_root = public_root / "fulltext"
    if any(path.is_symlink() for path in (papers_path, summary_path, fulltext_root)):
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid")
    if not papers_path.is_file() or not summary_path.is_file() or not fulltext_root.is_dir():
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid")
    try:
        if papers_path.stat().st_size > 10_000_000:
            raise DeepFetchUnavailable("deepfetch_papers_too_large")
        if summary_path.stat().st_size > MAX_DEEPFETCH_SUMMARY_LENGTH * 4:
            raise DeepFetchUnavailable("deepfetch_summary_invalid")
        papers_bytes = papers_path.read_bytes()
        summary_bytes = summary_path.read_bytes()
        ledger = json.loads(papers_bytes.decode("utf-8"))
        summary = summary_bytes.decode("utf-8").strip()
    except DeepFetchUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeepFetchUnavailable("deepfetch_public_artifacts_invalid") from error
    summary = _required_text(
        summary,
        maximum=MAX_DEEPFETCH_SUMMARY_LENGTH,
        code="deepfetch_summary_invalid",
    )
    top_keys = {
        "schema_version",
        "topic",
        "run",
        "paper_order",
        "papers",
        "missing_fulltexts",
        "limitations",
    }
    if not isinstance(ledger, dict) or set(ledger) != top_keys:
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    if ledger.get("schema_version") != "deepfetch.papers.v4":
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    paper_order = ledger.get("paper_order")
    paper_records = ledger.get("papers")
    missing_fulltexts = ledger.get("missing_fulltexts")
    ledger_limitations = ledger.get("limitations")
    if (
        not isinstance(paper_order, list)
        or any(not isinstance(value, str) or not value for value in paper_order)
        or len(set(paper_order)) != len(paper_order)
        or not isinstance(paper_records, dict)
        or set(paper_records) != set(paper_order)
        or not isinstance(missing_fulltexts, list)
        or any(not isinstance(value, str) for value in missing_fulltexts)
        or not isinstance(ledger_limitations, list)
        or any(not isinstance(value, str) or not value for value in ledger_limitations)
    ):
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    run = ledger.get("run")
    if not isinstance(run, dict) or set(run) != {
        "intensity",
        "active_search_budget_minutes",
        "active_search_elapsed_seconds",
        "dimensions_used",
        "stopping_reason",
    }:
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    budgets = {"low": 8, "medium": 13, "high": 20}
    dimensions = run.get("dimensions_used")
    if (
        run.get("intensity") not in budgets
        or run.get("active_search_budget_minutes")
        != budgets[cast(str, run.get("intensity"))]
        or not isinstance(run.get("active_search_elapsed_seconds"), (int, float))
        or isinstance(run.get("active_search_elapsed_seconds"), bool)
        or cast(float, run.get("active_search_elapsed_seconds")) < 0
        or not isinstance(dimensions, list)
        or not {"text_queries", "literature_roles", "citation_graph"}.issubset(
            set(dimensions)
        )
        or not isinstance(run.get("stopping_reason"), str)
        or not run.get("stopping_reason")
    ):
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")

    workflow = envelope.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != {
        "prototype_commit",
        "main_agent_status",
        "reader_assignments",
        "finalize_status",
        "finalized_at",
    }:
        raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid")
    finalized_at = workflow.get("finalized_at")
    try:
        parsed_finalized_at = datetime.fromisoformat(cast(str, finalized_at))
    except (TypeError, ValueError) as error:
        raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid") from error
    assignments = workflow.get("reader_assignments")
    if (
        workflow.get("prototype_commit") != DEEPFETCH_PROTOTYPE_COMMIT
        or workflow.get("main_agent_status") != "complete"
        or workflow.get("finalize_status") != "passed"
        or parsed_finalized_at.tzinfo is None
        or parsed_finalized_at.utcoffset() is None
        or not isinstance(assignments, list)
        or len(assignments) > 10
    ):
        raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid")
    assignment_by_paper: dict[str, dict[str, object]] = {}
    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or set(assignment)
            != {
                "paper_id",
                "assignment_id",
                "reader_agent_ref",
                "status",
            }
            or assignment.get("status") not in {
                "complete",
                "failed",
                "file_invalid",
                "paper_mismatch",
            }
            or not isinstance(assignment.get("paper_id"), str)
            or not isinstance(assignment.get("assignment_id"), str)
            or not assignment.get("assignment_id")
            or not isinstance(assignment.get("reader_agent_ref"), str)
            or not assignment.get("reader_agent_ref")
            or assignment["paper_id"] in assignment_by_paper
        ):
            raise DeepFetchUnavailable("deepfetch_workflow_evidence_invalid")
        assignment_by_paper[cast(str, assignment["paper_id"])] = assignment
    if assignments and not acquisition_request_ids:
        raise DeepFetchUnavailable("deepfetch_hosted_acquisition_proof_missing")
    obtained_artifacts: dict[str, dict[str, object]] = {}
    for proof in acquisition_item_proofs:
        if proof["status"] != "obtained":
            continue
        paper_id = cast(str, proof["paper_id"])
        previous = obtained_artifacts.get(paper_id)
        artifact = {
            key: proof[key]
            for key in ("path", "format", "sha256", "bytes")
        }
        if previous is not None and previous != artifact:
            raise DeepFetchUnavailable(
                "deepfetch_hosted_acquisition_artifact_mismatch"
            )
        obtained_artifacts[paper_id] = artifact
    if set(assignment_by_paper) - set(obtained_artifacts):
        raise DeepFetchUnavailable("deepfetch_hosted_acquisition_proof_mismatch")

    papers: list[dict[str, object]] = []
    fulltexts: list[dict[str, object]] = []
    expected_files: set[str] = set()
    calculated_missing: list[str] = []
    file_evidence: list[dict[str, object]] = []
    reader_failures = False
    total_fulltext_bytes = 0
    record_keys = {
        "identity",
        "metadata",
        "pre_understanding",
        "fulltext_path",
        "reading",
    }
    identity_keys = {"paper_id", "title", "doi", "arxiv_id", "openalex_id"}
    metadata_keys = {
        "authors",
        "institutions",
        "year",
        "venue",
        "publisher",
        "abstract",
        "cited_by_count",
        "citation_count_observed_at",
        "source_urls",
    }
    reading_keys = {
        "status",
        "understanding_summary",
        "methods",
        "experimental_setup",
        "key_claims",
        "limitations",
        "artifacts",
        "credibility",
        "evidence_locators",
        "notes",
    }
    for paper_id in paper_order:
        record = paper_records[paper_id]
        if not isinstance(record, dict) or set(record) != record_keys:
            raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
        identity = record.get("identity")
        metadata = record.get("metadata")
        reading = record.get("reading")
        if (
            not isinstance(identity, dict)
            or set(identity) != identity_keys
            or identity.get("paper_id") != paper_id
            or not isinstance(identity.get("title"), str)
            or not identity.get("title")
            or not isinstance(metadata, dict)
            or set(metadata) != metadata_keys
            or not isinstance(metadata.get("source_urls"), list)
            or not isinstance(reading, dict)
            or set(reading) != reading_keys
            or reading.get("status") not in {"not_read", "complete", "failed"}
        ):
            raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
        source_urls = [
            _validated_public_url(value, "deepfetch_paper_url_invalid")
            for value in metadata["source_urls"]
        ]
        doi = identity.get("doi")
        arxiv_id = identity.get("arxiv_id")
        openalex_id = identity.get("openalex_id")
        if source_urls:
            paper_url = source_urls[0]
            source_kind = "web"
        elif isinstance(doi, str) and doi:
            paper_url = _validated_public_url(
                f"https://doi.org/{doi}", "deepfetch_paper_url_invalid"
            )
            source_kind = "doi"
        elif isinstance(arxiv_id, str) and arxiv_id:
            paper_url = _validated_public_url(
                f"https://arxiv.org/abs/{arxiv_id}",
                "deepfetch_paper_url_invalid",
            )
            source_kind = "arxiv"
        elif isinstance(openalex_id, str) and openalex_id:
            paper_url = _validated_public_url(
                f"https://openalex.org/{openalex_id}",
                "deepfetch_paper_url_invalid",
            )
            source_kind = "openalex"
        else:
            raise DeepFetchUnavailable("deepfetch_paper_url_invalid")
        relative_path = record.get("fulltext_path")
        if relative_path is None:
            calculated_missing.append(paper_id)
            fulltext_status = "unavailable"
            if reading.get("status") != "not_read":
                raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
        else:
            if (
                not isinstance(relative_path, str)
                or not relative_path.startswith("fulltext/")
                or Path(relative_path).suffix.lower() not in {".pdf", ".html", ".xml"}
                or Path(relative_path).is_absolute()
                or ".." in Path(relative_path).parts
            ):
                raise DeepFetchUnavailable("deepfetch_fulltext_path_invalid")
            assignment = assignment_by_paper.get(paper_id)
            if (
                assignment is None
                or assignment.get("status") != reading.get("status")
                or reading.get("status") not in {"complete", "failed"}
            ):
                raise DeepFetchUnavailable("deepfetch_reader_evidence_invalid")
            reader_failures = reader_failures or reading.get("status") == "failed"
            expected_files.add(relative_path)
            candidate = public_root / relative_path
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(fulltext_root.resolve(strict=True))
                if candidate.is_symlink() or not candidate.is_file():
                    raise OSError("invalid fulltext")
                file_bytes = candidate.stat().st_size
                if (
                    file_bytes < 1
                    or file_bytes > MAX_DEEPFETCH_FULLTEXT_FILE_BYTES
                    or total_fulltext_bytes + file_bytes
                    > MAX_DEEPFETCH_FULLTEXT_TOTAL_BYTES
                ):
                    raise DeepFetchUnavailable("deepfetch_fulltext_too_large")
                with candidate.open("rb") as source:
                    content_bytes = source.read(MAX_DEEPFETCH_FULLTEXT_FILE_BYTES + 1)
                if len(content_bytes) != file_bytes:
                    raise DeepFetchUnavailable("deepfetch_fulltext_too_large")
            except (OSError, ValueError) as error:
                raise DeepFetchUnavailable("deepfetch_fulltext_invalid") from error
            total_fulltext_bytes += len(content_bytes)
            if not content_bytes:
                raise DeepFetchUnavailable("deepfetch_fulltext_invalid")
            suffix = candidate.suffix.lower()
            hosted_artifact = obtained_artifacts.get(paper_id)
            expected_suffix = {
                "pdf": ".pdf",
                "html": ".html",
                "xml": ".xml",
            }.get(
                None if hosted_artifact is None else hosted_artifact["format"]
            )
            if (
                hosted_artifact is None
                or suffix != expected_suffix
                or len(content_bytes) != hosted_artifact["bytes"]
                or hashlib.sha256(content_bytes).hexdigest()
                != hosted_artifact["sha256"]
            ):
                raise DeepFetchUnavailable(
                    "deepfetch_hosted_acquisition_artifact_mismatch"
                )
            if suffix == ".html":
                media_type = "text/html"
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise DeepFetchUnavailable("deepfetch_fulltext_invalid") from error
            elif suffix == ".xml":
                media_type = "text/plain"
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise DeepFetchUnavailable("deepfetch_fulltext_invalid") from error
            else:
                media_type = "application/pdf"
                content = "base64:" + base64.b64encode(content_bytes).decode("ascii")
            fulltexts.append(
                {
                    "paper_url": paper_url,
                    "media_type": media_type,
                    "content": content,
                }
            )
            file_evidence.append(
                {
                    "path": relative_path,
                    "sha256": hashlib.sha256(content_bytes).hexdigest(),
                    "bytes": len(content_bytes),
                }
            )
            fulltext_status = "accepted"
        papers.append(
            {
                "title": identity["title"],
                "url": paper_url,
                "doi": doi,
                "source_kind": source_kind,
                "fulltext_status": fulltext_status,
                "retrieved_at": cast(str, finalized_at),
            }
        )
    if calculated_missing != missing_fulltexts:
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    try:
        observed_files = {
            str(path.relative_to(public_root))
            for path in fulltext_root.rglob("*")
            if path.is_file()
        }
        if any(path.is_symlink() for path in fulltext_root.rglob("*")):
            raise DeepFetchUnavailable("deepfetch_fulltext_invalid")
    except OSError as error:
        raise DeepFetchUnavailable("deepfetch_fulltext_invalid") from error
    if observed_files != expected_files:
        raise DeepFetchUnavailable("deepfetch_fulltext_collection_invalid")

    raw_limitations = envelope.get("limitations")
    if not isinstance(raw_limitations, list) or any(
        not isinstance(value, str) or not value for value in raw_limitations
    ):
        raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
    limitations = tuple(
        dict.fromkeys([*ledger_limitations, *raw_limitations]).keys()
    )
    completion = envelope.get("completion")
    if completion not in {"complete", "limited", "honest_empty"}:
        raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
    if completion == "complete" and paper_order and limitations:
        # Preserve a structurally valid, non-empty research result when the
        # model reports its finished workflow as evidence-complete.  The
        # imported artifacts remain authoritative about evidence gaps.
        completion = "limited"
    if completion == "honest_empty" and paper_order:
        raise DeepFetchUnavailable("deepfetch_empty_result_not_empty")
    if completion == "complete" and (
        calculated_missing or reader_failures or limitations or not paper_order
    ):
        raise DeepFetchUnavailable("deepfetch_complete_result_incomplete")
    if completion == "limited" and (not paper_order or not limitations):
        raise DeepFetchUnavailable("deepfetch_limited_result_limit_missing")
    if completion == "honest_empty" and not limitations:
        raise DeepFetchUnavailable("deepfetch_empty_result_limit_missing")
    prototype_evidence = {
        "schema_ref": DEEPFETCH_PROTOTYPE_EVIDENCE_SCHEMA,
        "prototype_commit": DEEPFETCH_PROTOTYPE_COMMIT,
        "acquisition_request_ids": list(acquisition_request_ids),
        "main_agent_status": workflow["main_agent_status"],
        "reader_assignments": assignments,
        "finalize_status": workflow["finalize_status"],
        "papers_json_hash": hashlib.sha256(papers_bytes).hexdigest(),
        "summary_md_hash": hashlib.sha256(summary_bytes).hexdigest(),
        "fulltext_files": sorted(file_evidence, key=lambda value: value["path"]),
    }
    return (
        cast(Literal["complete", "limited", "honest_empty"], completion),
        summary,
        tuple(papers),
        tuple(fulltexts),
        limitations,
        cast(dict[str, object], ledger),
        prototype_evidence,
    )


def _validated_result_ledger(
    value: dict[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    required = {
        "schema_version",
        "topic",
        "run",
        "paper_order",
        "papers",
        "missing_fulltexts",
        "limitations",
    }
    paper_order = value.get("paper_order")
    papers = value.get("papers")
    if (
        set(value) != required
        or value.get("schema_version") != "deepfetch.papers.v4"
        or not isinstance(paper_order, list)
        or any(not isinstance(item, str) or not item for item in paper_order)
        or len(set(paper_order)) != len(paper_order)
        or not isinstance(papers, dict)
        or set(papers) != set(paper_order)
    ):
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid")
    try:
        canonical_json(value)
    except (TypeError, ValueError) as error:
        raise DeepFetchUnavailable("deepfetch_papers_v4_invalid") from error
    return value


def _validated_paper(value: object) -> dict[str, object]:
    required = {
        "title",
        "url",
        "doi",
        "source_kind",
        "fulltext_status",
        "retrieved_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DeepFetchUnavailable("deepfetch_paper_invalid")
    title = _required_text(
        value["title"], maximum=2_000, code="deepfetch_paper_invalid"
    )
    url = _validated_public_url(value["url"], "deepfetch_paper_url_invalid")
    doi_value = value["doi"]
    if doi_value is not None:
        doi_value = _required_text(
            doi_value, maximum=512, code="deepfetch_paper_doi_invalid"
        )
    source_kind = _required_text(
        value["source_kind"], maximum=80, code="deepfetch_paper_invalid"
    )
    fulltext_status = value["fulltext_status"]
    if fulltext_status not in {"accepted", "unavailable", "not_attempted"}:
        raise DeepFetchUnavailable("deepfetch_fulltext_status_invalid")
    retrieved_at = _required_text(
        value["retrieved_at"], maximum=80, code="deepfetch_retrieved_at_invalid"
    )
    try:
        timestamp = datetime.fromisoformat(retrieved_at)
    except ValueError as error:
        raise DeepFetchUnavailable("deepfetch_retrieved_at_invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DeepFetchUnavailable("deepfetch_retrieved_at_invalid")
    return {
        "title": title,
        "url": url,
        "doi": doi_value,
        "source_kind": source_kind,
        "fulltext_status": fulltext_status,
        "retrieved_at": retrieved_at,
    }


def _validated_fulltext(
    value: object,
    *,
    paper_urls: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "paper_url",
        "media_type",
        "content",
    }:
        raise DeepFetchUnavailable("deepfetch_fulltext_invalid")
    paper_url = _validated_public_url(
        value["paper_url"], "deepfetch_fulltext_url_invalid"
    )
    if paper_url not in paper_urls:
        raise DeepFetchUnavailable("deepfetch_fulltext_paper_missing")
    media_type = value["media_type"]
    if media_type not in {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/html",
    }:
        raise DeepFetchUnavailable("deepfetch_fulltext_media_type_invalid")
    content = _required_text(
        value["content"],
        maximum=MAX_DEEPFETCH_FULLTEXT_LENGTH,
        code="deepfetch_fulltext_invalid",
    )
    return {
        "paper_url": paper_url,
        "media_type": media_type,
        "content": content,
        "content_hash": canonical_hash({"media_type": media_type, "content": content}),
    }


def _validated_public_url(value: object, code: str) -> str:
    url = _required_text(value, maximum=8_000, code=code)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise DeepFetchUnavailable(code) from error
    sensitive_names = {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "key",
        "password",
        "secret",
        "session",
        "token",
        # Common pre-signed URL credentials.  The short Azure SAS names are
        # intentionally blocked here because persisted research URLs must be
        # stable public locators, never bearer capabilities.
        "sig",
        "signature",
        "googleaccessid",
        "expires",
        "sv",
        "ss",
        "srt",
        "sp",
        "se",
        "st",
        "spr",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
    }
    query_names = {name.casefold() for name, _ in parse_qsl(parsed.query)}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
        or query_names & sensitive_names
        or any(name.startswith(("x-amz-", "x-goog-")) for name in query_names)
    ):
        raise DeepFetchUnavailable(code)
    return url


def _required_text(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str):
        raise DeepFetchUnavailable(code)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise DeepFetchUnavailable(code)
    return normalized


def _thread_id(stdout: str) -> str | None:
    observed: set[str] = set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                observed.add(value)
    if len(observed) > 1:
        raise DeepFetchUnavailable("deepfetch_native_session_changed")
    return next(iter(observed), None)


def _validated_web_evidence(
    value: dict[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    required = {
        "schema_ref",
        "search_event_count",
        "fetch_event_count",
        "trace_hash",
    }
    if frozenset(value) not in {
        frozenset(required),
        frozenset({*required, "prototype"}),
    }:
        raise DeepFetchUnavailable("deepfetch_web_evidence_invalid")
    search_count = value.get("search_event_count")
    fetch_count = value.get("fetch_event_count")
    trace_hash = value.get("trace_hash")
    if (
        value.get("schema_ref") != DEEPFETCH_WEB_EVIDENCE_SCHEMA
        or not isinstance(search_count, int)
        or isinstance(search_count, bool)
        or search_count < 1
        or not isinstance(fetch_count, int)
        or isinstance(fetch_count, bool)
        or fetch_count < 1
        or not isinstance(trace_hash, str)
        or len(trace_hash) != 64
    ):
        raise DeepFetchUnavailable("deepfetch_web_evidence_invalid")
    validated = {
        "schema_ref": DEEPFETCH_WEB_EVIDENCE_SCHEMA,
        "search_event_count": search_count,
        "fetch_event_count": fetch_count,
        "trace_hash": trace_hash,
    }
    prototype = value.get("prototype")
    if prototype is not None:
        validated["prototype"] = _validated_prototype_evidence(prototype)
    return validated


def _validated_prototype_evidence(value: object) -> dict[str, object]:
    required = {
        "schema_ref",
        "prototype_commit",
        "acquisition_request_ids",
        "main_agent_status",
        "reader_assignments",
        "finalize_status",
        "papers_json_hash",
        "summary_md_hash",
        "fulltext_files",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DeepFetchUnavailable("deepfetch_prototype_evidence_invalid")
    request_ids = value.get("acquisition_request_ids")
    assignments = value.get("reader_assignments")
    files = value.get("fulltext_files")
    if (
        value.get("schema_ref") != DEEPFETCH_PROTOTYPE_EVIDENCE_SCHEMA
        or value.get("prototype_commit") != DEEPFETCH_PROTOTYPE_COMMIT
        or not isinstance(request_ids, list)
        or any(not isinstance(item, str) or not item for item in request_ids)
        or len(set(request_ids)) != len(request_ids)
        or value.get("main_agent_status") != "complete"
        or not isinstance(assignments, list)
        or value.get("finalize_status") != "passed"
        or not isinstance(value.get("papers_json_hash"), str)
        or len(cast(str, value.get("papers_json_hash"))) != 64
        or not isinstance(value.get("summary_md_hash"), str)
        or len(cast(str, value.get("summary_md_hash"))) != 64
        or not isinstance(files, list)
    ):
        raise DeepFetchUnavailable("deepfetch_prototype_evidence_invalid")
    for file_proof in files:
        if not isinstance(file_proof, dict) or set(file_proof) != {
            "path",
            "sha256",
            "bytes",
        }:
            raise DeepFetchUnavailable("deepfetch_prototype_evidence_invalid")
        path = file_proof.get("path")
        sha256 = file_proof.get("sha256")
        byte_count = file_proof.get("bytes")
        if (
            not isinstance(path, str)
            or not path.startswith("fulltext/")
            or Path(path).suffix.lower() not in {".pdf", ".html", ".xml"}
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
        ):
            raise DeepFetchUnavailable("deepfetch_prototype_evidence_invalid")
    return cast(dict[str, object], value)


def _verified_turn_evidence(stdout: str) -> dict[str, object]:
    """Derive one resumable turn's Web and native-child evidence."""

    trace: list[dict[str, object]] = []
    event_refs: set[str] = set()
    search_count = 0
    fetch_count = 0
    spawned_reader_agent_refs: list[str] = []
    terminal_reader_agent_refs: list[str] = []
    root_session_ref = _thread_id(stdout)
    if root_session_ref is None:
        raise DeepFetchUnavailable("codex_deepfetch_session_ref_missing")
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "collab_tool_call":
            _collect_reader_trace(
                item,
                root_session_ref=root_session_ref,
                spawned=spawned_reader_agent_refs,
                terminal=terminal_reader_agent_refs,
            )
            continue
        if item.get("type") != "web_search":
            continue
        event_ref = item.get("id")
        if not isinstance(event_ref, str) or not event_ref:
            continue
        if event_ref in event_refs:
            raise DeepFetchUnavailable("deepfetch_web_evidence_invalid")
        event_refs.add(event_ref)
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        query = item.get("query")
        if action_type == "search":
            kind = "search"
            search_count += 1
        # Codex reports open(ref_id=...) as a completed `other` Web action with
        # an empty query; the completed web_search item is the verifiable fetch.
        elif action_type in {"open", "open_page", "fetch"} or (
            action_type == "other"
            and isinstance(query, str)
            and (query == "" or query.startswith(("https://", "http://")))
        ):
            kind = "fetch"
            fetch_count += 1
        else:
            continue
        trace.append(
            {
                "kind": kind,
                "event_ref": event_ref,
                "query_hash": canonical_hash(query) if isinstance(query, str) else None,
            }
        )
    evidence = {
        "schema_ref": DEEPFETCH_WEB_EVIDENCE_SCHEMA,
        "search_event_count": search_count,
        "fetch_event_count": fetch_count,
        "trace_hash": canonical_hash(trace),
        "spawned_reader_agent_refs": spawned_reader_agent_refs,
        "terminal_reader_agent_refs": terminal_reader_agent_refs,
    }
    return _validated_turn_evidence_part(evidence)


def _collect_reader_trace(
    item: dict[str, object],
    *,
    root_session_ref: str,
    spawned: list[str],
    terminal: list[str],
) -> None:
    if item.get("status") != "completed":
        return
    tool = item.get("tool")
    # Current Codex emits a completed ``close_agent`` item when the root
    # collects an already-completed child without a preceding wait item.  Its
    # signed agents_states payload is the same terminal evidence boundary.
    if tool not in {"spawn_agent", "wait", "close_agent"}:
        return
    receivers = item.get("receiver_thread_ids")
    states = item.get("agents_states")
    if (
        item.get("sender_thread_id") != root_session_ref
        or not isinstance(receivers, list)
        or not isinstance(states, dict)
    ):
        return
    if tool == "spawn_agent":
        if (
            len(receivers) != 1
            or not isinstance(receivers[0], str)
            or not receivers[0]
            or not isinstance(states.get(receivers[0]), dict)
        ):
            return
        if receivers[0] not in spawned:
            spawned.append(receivers[0])
        return
    if receivers == [] and states == {}:
        return
    if any(not isinstance(value, str) or not value for value in receivers):
        return
    for receiver in cast(list[str], receivers):
        state = states.get(receiver)
        if not isinstance(state, dict):
            continue
        if state.get("status") == "completed" and receiver not in terminal:
            terminal.append(receiver)


def _validated_turn_evidence_part(value: object) -> dict[str, object]:
    required = {
        "schema_ref",
        "search_event_count",
        "fetch_event_count",
        "trace_hash",
        "spawned_reader_agent_refs",
        "terminal_reader_agent_refs",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    search_count = value.get("search_event_count")
    fetch_count = value.get("fetch_event_count")
    trace_hash = value.get("trace_hash")
    spawned = value.get("spawned_reader_agent_refs")
    terminal = value.get("terminal_reader_agent_refs")
    if (
        value.get("schema_ref") != DEEPFETCH_WEB_EVIDENCE_SCHEMA
        or not isinstance(search_count, int)
        or isinstance(search_count, bool)
        or search_count < 0
        or not isinstance(fetch_count, int)
        or isinstance(fetch_count, bool)
        or fetch_count < 0
        or not isinstance(trace_hash, str)
        or len(trace_hash) != 64
        or not isinstance(spawned, list)
        or not isinstance(terminal, list)
        or any(not isinstance(item, str) or not item for item in spawned)
        or any(not isinstance(item, str) or not item for item in terminal)
        or len(set(spawned)) != len(spawned)
        or len(set(terminal)) != len(terminal)
    ):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    return cast(dict[str, object], value)


def _verified_reader_agent_refs(
    evidence_parts: tuple[dict[str, object], ...],
    *,
    ledger_reader: CodexSessionLedgerReader | None = None,
    root_session_ref: str | None = None,
    expected_working_directory: str | None = None,
) -> _VerifiedReaderAgentTrace:
    spawned = [
        cast(str, reader_ref)
        for part in evidence_parts
        for reader_ref in cast(list[object], part["spawned_reader_agent_refs"])
    ]
    terminal = [
        cast(str, reader_ref)
        for part in evidence_parts
        for reader_ref in cast(list[object], part["terminal_reader_agent_refs"])
    ]
    if (
        len(set(spawned)) != len(spawned)
        or len(set(terminal)) != len(terminal)
        or set(spawned) != set(terminal)
    ):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    ledger_trace: _VerifiedReaderAgentTrace | None = None
    if (
        ledger_reader is not None
        and root_session_ref is not None
        and expected_working_directory is not None
    ):
        ledger_trace = _verified_codex_reader_ledger_refs(
            ledger_reader,
            root_session_ref=root_session_ref,
            expected_working_directory=expected_working_directory,
        )
        if spawned and set(spawned) not in (
            set(ledger_trace.refs),
            set(ledger_trace.native_thread_refs),
        ):
            raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    if ledger_trace is not None:
        return ledger_trace
    return _VerifiedReaderAgentTrace(tuple(spawned), "native_thread")


def _verified_codex_reader_ledger_refs(
    reader: CodexSessionLedgerReader,
    *,
    root_session_ref: str,
    expected_working_directory: str,
) -> _VerifiedReaderAgentTrace:
    """Bind root spawn calls to child lineage, terminal output, and delivery."""

    try:
        records = reader.read(root_session_ref)
    except (OSError, ValueError) as error:
        raise DeepFetchUnavailable(
            "deepfetch_reader_agent_trace_invalid"
        ) from error
    metadata = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta"
        and isinstance(record.get("payload"), dict)
    ]
    if len(metadata) != 1:
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    root = metadata[0]
    contexts = [
        record.get("payload")
        for record in records
        if record.get("type") == "turn_context"
        and isinstance(record.get("payload"), dict)
    ]
    if (
        root.get("id") != root_session_ref
        or root.get("session_id") != root_session_ref
        or root.get("cwd") != expected_working_directory
        or root.get("thread_source") != "user"
        or root.get("originator") != "codex_exec"
        or root.get("source") != "exec"
        or not contexts
        or any(
            context.get("cwd") != expected_working_directory
            or not isinstance(context.get("sandbox_policy"), dict)
            or cast(dict[str, object], context["sandbox_policy"]).get("type")
            != "danger-full-access"
            for context in contexts
        )
    ):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")

    spawn_calls: dict[str, str] = {}
    spawn_outputs: dict[str, str] = {}
    activities: dict[str, tuple[str, str]] = {}
    delivered: dict[str, str] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "response_item" and (
            payload.get("type") == "function_call"
            and payload.get("name") == "spawn_agent"
        ):
            call_id = payload.get("call_id")
            arguments = payload.get("arguments")
            try:
                decoded_arguments = json.loads(cast(str, arguments))
            except (TypeError, json.JSONDecodeError) as error:
                raise DeepFetchUnavailable(
                    "deepfetch_reader_agent_trace_invalid"
                ) from error
            task_name = (
                decoded_arguments.get("task_name")
                if isinstance(decoded_arguments, dict)
                else None
            )
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in spawn_calls
                or not isinstance(task_name, str)
                or re.fullmatch(r"reader_[a-z0-9_]{1,96}", task_name) is None
            ):
                raise DeepFetchUnavailable(
                    "deepfetch_reader_agent_trace_invalid"
                )
            spawn_calls[call_id] = task_name
        elif record.get("type") == "response_item" and (
            payload.get("type") == "function_call_output"
        ):
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or call_id not in spawn_calls:
                continue
            try:
                decoded_output = json.loads(cast(str, payload.get("output")))
            except (TypeError, json.JSONDecodeError) as error:
                raise DeepFetchUnavailable(
                    "deepfetch_reader_agent_trace_invalid"
                ) from error
            canonical_task_name = (
                decoded_output.get("task_name")
                if isinstance(decoded_output, dict)
                else None
            )
            if (
                not isinstance(decoded_output, dict)
                or set(decoded_output) != {"task_name"}
                or canonical_task_name != f"/root/{spawn_calls[call_id]}"
                or call_id in spawn_outputs
            ):
                raise DeepFetchUnavailable(
                    "deepfetch_reader_agent_trace_invalid"
                )
            spawn_outputs[call_id] = canonical_task_name
        elif record.get("type") == "event_msg" and (
            payload.get("type") == "sub_agent_activity"
            and payload.get("kind") == "started"
        ):
            call_id = payload.get("event_id")
            task_name = payload.get("agent_path")
            child_ref = payload.get("agent_thread_id")
            if (
                not isinstance(call_id, str)
                or call_id in activities
                or not isinstance(task_name, str)
                or not isinstance(child_ref, str)
                or not child_ref
            ):
                raise DeepFetchUnavailable(
                    "deepfetch_reader_agent_trace_invalid"
                )
            activities[call_id] = (task_name, child_ref)
        elif record.get("type") == "response_item" and (
            payload.get("type") == "agent_message"
        ):
            author = payload.get("author")
            content = payload.get("content")
            text = _codex_ledger_message_text(content)
            if (
                not isinstance(author, str)
                or not author.startswith("/root/reader_")
                or payload.get("recipient") != "/root"
                or text is None
            ):
                continue
            delivered[author] = text

    if (
        not spawn_calls
        or set(spawn_calls) != set(spawn_outputs)
        or set(spawn_calls) != set(activities)
        or len(set(spawn_outputs.values())) != len(spawn_outputs)
        or len({child_ref for _task, child_ref in activities.values()})
        != len(activities)
    ):
        if not spawn_calls and not activities and not delivered:
            return _VerifiedReaderAgentTrace((), "task_name", ())
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")

    verified_task_names: list[str] = []
    verified_child_refs: list[str] = []
    for call_id, task_name in spawn_outputs.items():
        activity_task, child_ref = activities[call_id]
        if activity_task != task_name:
            raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
        try:
            child_records = reader.read(child_ref)
        except (OSError, ValueError) as error:
            raise DeepFetchUnavailable(
                "deepfetch_reader_agent_trace_invalid"
            ) from error
        terminal = _verified_codex_reader_child(
            child_records,
            child_ref=child_ref,
            task_name=task_name,
            root_session_ref=root_session_ref,
            expected_working_directory=expected_working_directory,
        )
        if task_name not in delivered or not delivered[task_name].endswith(terminal):
            raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
        verified_task_names.append(task_name)
        verified_child_refs.append(child_ref)
    if set(delivered) != set(verified_task_names):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    return _VerifiedReaderAgentTrace(
        tuple(verified_task_names),
        "task_name",
        tuple(verified_child_refs),
    )


def _codex_ledger_message_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    parts = [
        item.get("text")
        for item in value
        if isinstance(item, dict)
        and item.get("type") in {"input_text", "text", "output_text"}
        and isinstance(item.get("text"), str)
    ]
    return "".join(cast(list[str], parts)) if parts else None


def _verified_codex_reader_child(
    records: tuple[dict[str, object], ...],
    *,
    child_ref: str,
    task_name: str,
    root_session_ref: str,
    expected_working_directory: str,
) -> str:
    metadata = [
        record.get("payload")
        for record in records
        if record.get("type") == "session_meta"
        and isinstance(record.get("payload"), dict)
    ]
    terminals = [
        payload.get("last_agent_message")
        for record in records
        if record.get("type") == "event_msg"
        and isinstance((payload := record.get("payload")), dict)
        and payload.get("type") == "task_complete"
        and isinstance(payload.get("last_agent_message"), str)
        and payload.get("last_agent_message")
    ]
    if len(metadata) != 1 or len(terminals) != 1:
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    value = metadata[0]
    source = value.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    contexts = [
        record.get("payload")
        for record in records
        if record.get("type") == "turn_context"
        and isinstance(record.get("payload"), dict)
    ]
    if (
        value.get("id") != child_ref
        or value.get("session_id") != root_session_ref
        or value.get("parent_thread_id") != root_session_ref
        or value.get("cwd") != expected_working_directory
        or value.get("thread_source") != "subagent"
        or value.get("originator") != "codex_exec"
        or not isinstance(spawn, dict)
        or spawn.get("parent_thread_id") != root_session_ref
        or spawn.get("depth") != 1
        or spawn.get("agent_path") != task_name
        or not contexts
        or any(
            context.get("cwd") != expected_working_directory
            or not isinstance(context.get("sandbox_policy"), dict)
            or cast(dict[str, object], context["sandbox_policy"]).get("type")
            != "danger-full-access"
            for context in contexts
        )
    ):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    return cast(str, terminals[0])


def _validate_web_evidence_gate_result(value: object) -> None:
    if value != {"status": "web_evidence_ready"}:
        raise DeepFetchUnavailable("codex_deepfetch_output_invalid")


def _deepfetch_web_evidence_gate_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "const": "web_evidence_ready"},
        },
        "required": ["status"],
    }


def _deepfetch_output_schema() -> dict[str, object]:
    acquisition_target = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "paper_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "title": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "doi": {"type": "string", "minLength": 1, "maxLength": 512},
            "arxiv_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "source_urls": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 4_096},
            },
        },
        "required": ["paper_id", "title", "source_urls"],
    }
    acquisition_request = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "effect_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "target": acquisition_target,
        },
        "required": ["effect_id", "target"],
    }
    reader_assignment = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "paper_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "assignment_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "reader_agent_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            },
            "status": {
                "type": "string",
                "enum": ["complete", "failed", "file_invalid", "paper_mismatch"],
            },
        },
        "required": [
            "paper_id",
            "assignment_id",
            "reader_agent_ref",
            "status",
        ],
    }
    workflow = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "prototype_commit": {"type": "string", "const": DEEPFETCH_PROTOTYPE_COMMIT},
            "main_agent_status": {
                "type": "string",
                "enum": ["running", "complete"],
            },
            "reader_assignments": {
                "type": "array",
                "maxItems": 10,
                "items": reader_assignment,
            },
            "finalize_status": {
                "type": "string",
                "enum": ["pending", "passed"],
            },
            "finalized_at": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 80},
                    {"type": "null"},
                ]
            },
        },
        "required": [
            "prototype_commit",
            "main_agent_status",
            "reader_assignments",
            "finalize_status",
            "finalized_at",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["acquire", "finalize"]},
            "acquisition_request": {
                "anyOf": [acquisition_request, {"type": "null"}]
            },
            "completion": {
                "description": _DEEPFETCH_COMPLETION_RULES,
                "anyOf": [
                    {
                        "type": "string",
                        "enum": ["complete", "limited", "honest_empty"],
                    },
                    {"type": "null"},
                ]
            },
            "limitations": {
                "type": "array",
                "maxItems": MAX_DEEPFETCH_LIMITATIONS,
                "items": {"type": "string", "minLength": 1, "maxLength": 8_000},
            },
            "workflow": workflow,
        },
        "required": [
            "action",
            "acquisition_request",
            "completion",
            "limitations",
            "workflow",
        ],
    }
