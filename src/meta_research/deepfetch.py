from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionPaper,
    DEEPFETCH_PROTOTYPE_COMMIT,
)

from meta_research.provider_supervisor import (
    SUPERVISOR_REQUEST_SCHEMA,
    ProviderSupervisorError,
    ensure_transport_key,
    read_verified_exit_receipt,
    write_supervisor_request,
)
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    _CancellableProcessRunner,
    _ProcessStopped,
    _text_exceeds_limit,
)

if TYPE_CHECKING:
    from meta_research.owners.common import AcceptanceReceipt


DEEPFETCH_REQUEST_SCHEMA = "meta-research/first-question-deepfetch-request/v1"
QUESTION_DEEPFETCH_REQUEST_SCHEMA = "meta-research/question-deepfetch-request/v1"
DEEPFETCH_RESULT_SCHEMA = "meta-research/first-question-deepfetch-result/v2"
DEEPFETCH_RUNTIME_BINDING_SCHEMA = "meta-research/deepfetch-runtime-binding/v1"
DEEPFETCH_WEB_EVIDENCE_SCHEMA = "meta-research/deepfetch-web-evidence/v1"
DEEPFETCH_PROTOTYPE_EVIDENCE_SCHEMA = "meta-research/deepfetch-prototype-evidence/v4"
DEEPFETCH_PROTOCOL_CHECKPOINT_SCHEMA = (
    "meta-research/deepfetch-v4-protocol-checkpoint/v1"
)
MAX_DEEPFETCH_PAPERS = 500
MAX_DEEPFETCH_FULLTEXTS = 100
MAX_DEEPFETCH_SUMMARY_LENGTH = 100_000
MAX_DEEPFETCH_FULLTEXT_LENGTH = 280_000_000
MAX_DEEPFETCH_LIMITATIONS = 100
MAX_DEEPFETCH_FULLTEXT_FILE_BYTES = 32 * 1024 * 1024
MAX_DEEPFETCH_FULLTEXT_TOTAL_BYTES = 96 * 1024 * 1024


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
        "quest_initialization", "manual_question_creation"
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
                else QUESTION_DEEPFETCH_REQUEST_SCHEMA
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
        if self.creation_context_kind == "manual_question_creation":
            payload.update(
                {
                    "creation_context_kind": self.creation_context_kind,
                    "creation_context_ref": self.creation_context_ref,
                    "context_generation": self.context_generation,
                    "quest_ref": self.quest_ref,
                    "parent_question_ref": self.parent_question_ref,
                    "context_basis_hash": self.context_basis_hash,
                }
            )
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
    acquisition_session_ref: str
    acquisition_config_hash: str
    acquisition_runtime_binding_hash: str
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
            "pending_acquisition": self.pending_acquisition,
            "next_prompt": self.next_prompt,
            "final_envelope": self.final_envelope,
        }


class DeepFetchProvider(Protocol):
    def runtime_binding(self) -> DeepFetchRuntimeBinding: ...

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult: ...


class DeepFetchAcquisitionClient(Protocol):
    """Narrow hosted port exposed to the v4 main-agent adapter."""

    def acquire(
        self,
        session_ref: str,
        request: AcquisitionBatchRequest,
    ) -> AcquisitionBatchExecution: ...


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
    if len(capabilities) != len(binding.capability_bindings) or not {
        "web-search-live",
        "web-fetch-live",
    }.issubset(capabilities):
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


class CodexDeepFetchAdapter:
    """Production Adapter for the bound DeepFetch v4 Harness workflow."""

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = "gpt-5.4",
        timeout_seconds: float = 30 * 60,
        acquisition_client: DeepFetchAcquisitionClient | None = None,
        process_runner: (
            Callable[[list[str], str, float], subprocess.CompletedProcess[str]] | None
        ) = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._executable = executable
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._acquisition_client = acquisition_client
        self._runner = process_runner or _CancellableProcessRunner()
        self._skill_root = _deepfetch_skill_root()
        self._skill_bundle_hash = _deepfetch_skill_bundle_hash(self._skill_root)

    def request_stop(self) -> None:
        request_stop = getattr(self._runner, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def cancel_job(self, job_ref: str) -> None:
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_ref)
            for turn_number in range(12):
                cancel_job(f"{job_ref}:v4-turn:{turn_number}")

    def finish_job(self, job_ref: str) -> None:
        finish_job = getattr(self._runner, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)
            for turn_number in range(12):
                finish_job(f"{job_ref}:v4-turn:{turn_number}")

    @property
    def requires_verified_terminal_retry(self) -> bool:
        """Whether a new durable effect requires a signed terminal predecessor."""

        return callable(getattr(self._runner, "run_durable_job", None))

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="meta_research.deepfetch.CodexDeepFetchAdapter",
            provider_version=DEEPFETCH_PROTOTYPE_COMMIT,
            model_ref=self._model_ref,
            harness_ref="codex-cli",
            capability_bindings=(
                "approval-policy-never",
                "deepfetch-v4-main-agent",
                f"deepfetch-v4-skill-bundle-sha256:{self._skill_bundle_hash}",
                "hosted-acquisition-session",
                "native-child-readers",
                "papers-v4-finalize",
                "structured-output-json-schema",
                "workspace-write-public-artifacts",
                "web-fetch-live",
                "web-search-live",
            ),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        run_key = canonical_hash(
            {
                "request_ref": request.request_ref,
                "correlation_ref": request.correlation_ref,
                "draft_hash": request.draft_hash,
                "scope_hash": request.scope_hash,
            }
        )[:32]
        run_root = self._workspace / "runs" / run_key
        public_root = run_root / "public"
        private_root = run_root / "private"
        public_root.mkdir(parents=True, exist_ok=True)
        private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_path = private_root / "protocol.json"
        identity_hash = _deepfetch_protocol_identity(request)
        try:
            checkpoint = _load_or_create_protocol_checkpoint(
                checkpoint_path,
                identity_hash=identity_hash,
                native_session_ref=request.native_session_ref,
                initial_prompt=self._initial_prompt(
                    request,
                    public_root=public_root,
                    private_root=private_root,
                ),
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
                if self._acquisition_client is None:
                    raise DeepFetchUnavailable(
                        "deepfetch_acquisition_capability_unavailable"
                    )
                assert checkpoint.pending_acquisition is not None
                batch = _acquisition_batch_from_checkpoint(
                    checkpoint.pending_acquisition
                )
                execution = self._acquisition_client.acquire(
                    request.acquisition_session_ref,
                    batch,
                )
                if execution.status == "waiting_user":
                    raise DeepFetchUnavailable(
                        "deepfetch_acquisition_waiting_user",
                        durable_outcome="pending",
                        native_session_ref=checkpoint.native_session_ref,
                    )
                acquisition_ids = (
                    *checkpoint.acquisition_request_ids,
                    batch.request_id,
                )
                checkpoint = replace(
                    checkpoint,
                    phase="ready_for_turn",
                    acquisition_request_ids=acquisition_ids,
                    pending_acquisition=None,
                    next_prompt=self._acquisition_result_prompt(
                        public_root,
                        execution,
                    ),
                )
                _write_protocol_checkpoint(checkpoint_path, checkpoint)
                continue

            if checkpoint.next_turn_number >= 12:
                raise DeepFetchUnavailable(
                    "deepfetch_protocol_turn_limit_exceeded"
                )
            assert checkpoint.next_prompt is not None
            turn_number = checkpoint.next_turn_number
            turn_request = replace(
                request,
                native_session_ref=checkpoint.native_session_ref,
                job_ref=(
                    None
                    if request.job_ref is None
                    else f"{request.job_ref}:v4-turn:{turn_number}"
                ),
            )
            raw, native_session_ref, turn_evidence = self._invoke(
                turn_request,
                checkpoint.next_prompt,
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
            batch = _validated_v4_acquisition_request(raw)
            if batch.request_id in checkpoint.acquisition_request_ids:
                raise DeepFetchUnavailable(
                    "deepfetch_acquisition_identity_duplicate"
                )
            checkpoint = replace(
                checkpoint,
                phase="pending_acquisition",
                native_session_ref=native_session_ref,
                next_turn_number=turn_number + 1,
                evidence_parts=evidence_parts,
                pending_acquisition=batch.identity_payload(),
                next_prompt=None,
            )
            # The exact batch is durable before the hosted side effect begins.
            _write_protocol_checkpoint(checkpoint_path, checkpoint)

        assert checkpoint.final_envelope is not None
        assert checkpoint.native_session_ref is not None
        web_evidence = _merge_web_evidence(list(checkpoint.evidence_parts))
        reader_agent_refs = _verified_reader_agent_refs(
            checkpoint.evidence_parts
        )
        _precheck_public_artifact_resource_limits(public_root)
        _run_exact_papers_validator(self._skill_root, public_root)
        imported = _import_v4_public_artifacts(
            public_root,
            checkpoint.final_envelope,
            acquisition_session_ref=request.acquisition_session_ref,
            acquisition_request_ids=checkpoint.acquisition_request_ids,
            reader_agent_refs=reader_agent_refs,
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
        # import, web/Reader provenance checks, and public-result validation all
        # pass. A malformed terminal turn remains safely retryable in-session.
        _write_protocol_checkpoint(checkpoint_path, checkpoint)
        return result

    def _initial_prompt(
        self,
        request: DeepFetchProviderRequest,
        *,
        public_root: Path,
        private_root: Path,
    ) -> str:
        skill_entrypoint = self._skill_root / "SKILL.md"
        return (
            "你是绑定 fixed commit "
            f"{DEEPFETCH_PROTOTYPE_COMMIT} 的 DeepFetch v4 main agent。"
            f"开始前必须完整读取 {skill_entrypoint}，以及它直接链接的所有 reference；"
            "把该固定 bundle 作为行为规范，不得改写它。必须执行 Radar、Ledger、"
            "hosted Acquisition、independent Readers、Synthesis 与 "
            "scripts/papers.py finalize 闭环；不得把它扁平化成单体 Web Search/Fetch "
            "回答。主 Agent 负责发现、选择、ledger 和 summary，Acquisition 只负责"
            "合法获取；每份已注册全文必须用原生 spawn_agent 启动一个独立 Reader，"
            "并 wait 到终态。最终 workflow.reader_assignments 的 reader_agent_ref "
            "必须填写 spawn_agent 返回的 child id，不能填写 assignment_id。"
            "不得声称创建 Quest/Question/Cycle、接纳 Evidence 或签发 receipt；"
            "不得把 Cookie、凭据、浏览器 profile、私有 manifest 或恢复状态写入"
            "公开目录。最终公开目录必须且只能包含 papers.json、summary.md、fulltext/。\n"
            f"deepfetch_skill_root={self._skill_root}\n"
            f"public_output_root={public_root}\n"
            f"private_work_root={private_root}\n"
            f"acquisition_session_ref={request.acquisition_session_ref}\n"
            f"request_ref={request.request_ref}\n"
            f"draft_revision={request.draft_revision}\n"
            f"draft_hash={request.draft_hash}\n"
            f"scope={canonical_json(request.scope)}\n"
            "accepted_material_bindings="
            f"{canonical_json(list(request.accepted_material_bindings))}"
        )

    def _acquisition_result_prompt(
        self,
        public_root: Path,
        execution: AcquisitionBatchExecution,
    ) -> str:
        return (
            "继续同一 DeepFetch v4 main agent Session。以下是 hosted Acquisition "
            "对上一精确批次返回的紧凑结果；不得改变 request_id 或把私有 manifest "
            "带入公开资产。继续 Radar/Ledger/Readers，必要时提出一个新的有限批次，"
            "最终运行固定 bundle 的 scripts/papers.py finalize。每个 Reader 的 "
            "reader_agent_ref 必须使用原生 spawn_agent 返回的 child id。\n"
            f"deepfetch_skill_root={self._skill_root}\n"
            f"public_output_root={public_root}\n"
            f"acquisition_request_id={execution.request_id}\n"
            "acquisition_result="
            f"{canonical_json([item.as_dict() for item in execution.results])}"
        )

    def _invoke(
        self,
        request: DeepFetchProviderRequest,
        prompt: str,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        if request.job_ref is not None and callable(
            getattr(self._runner, "run_durable_job", None)
        ):
            return self._invoke_durable(request, prompt)
        with tempfile.TemporaryDirectory(
            prefix="deepfetch-", dir=self._workspace
        ) as raw_directory:
            directory = Path(raw_directory)
            schema_path = directory / "output-schema.json"
            result_path = directory / "last-message.json"
            schema_path.write_text(
                canonical_json(_deepfetch_output_schema()), encoding="utf-8"
            )
            argv = [
                self._executable,
                "exec",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--strict-config",
                "--config",
                "mcp_servers={}",
                "--config",
                'approval_policy="never"',
                "--config",
                'web_search="live"',
                "--config",
                "features.multi_agent=true",
                "--sandbox",
                "workspace-write",
                "--model",
                self._model_ref,
                "--cd",
                str(self._workspace),
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
                run_job = getattr(self._runner, "run_job", None)
                if request.job_ref is not None and callable(run_job):
                    completed = run_job(
                        request.job_ref, argv, prompt, self._timeout_seconds
                    )
                else:
                    completed = self._runner(argv, prompt, self._timeout_seconds)
            except _ProcessStopped as error:
                raise DeepFetchUnavailable("deepfetch_provider_stopped") from error
            except FileNotFoundError as error:
                raise DeepFetchUnavailable("codex_cli_unavailable") from error
            except subprocess.TimeoutExpired as error:
                raise DeepFetchUnavailable("codex_deepfetch_timeout") from error
            except OSError as error:
                raise DeepFetchUnavailable("codex_deepfetch_io_unavailable") from error
            if completed.returncode != 0:
                raise DeepFetchUnavailable("codex_deepfetch_failed")
            if _text_exceeds_limit(
                completed.stdout, PROVIDER_STREAM_MAX_BYTES
            ) or _text_exceeds_limit(completed.stderr, PROVIDER_STREAM_MAX_BYTES):
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
            native_session_ref = request.native_session_ref or _thread_id(
                completed.stdout
            )
            if native_session_ref is None:
                raise DeepFetchUnavailable("codex_deepfetch_session_ref_missing")
            web_evidence = _verified_turn_evidence(completed.stdout)
            return cast(dict[str, object], decoded), native_session_ref, web_evidence

    def _invoke_durable(
        self,
        request: DeepFetchProviderRequest,
        prompt: str,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        """Reconcile one logical provider operation across daemon Attempts."""

        assert request.job_ref is not None
        operation_root = (
            self._workspace
            / "provider-operations"
            / canonical_hash({"job_ref": request.job_ref})
        )
        try:
            _key_path, transport_key = ensure_transport_key(self._workspace)
        except (OSError, ProviderSupervisorError) as error:
            raise DeepFetchUnavailable("deepfetch_provider_spool_invalid") from error
        native_session_ref = request.native_session_ref
        trace_parts: list[str] = []
        for segment_number in range(8):
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
                native_session_ref=native_session_ref,
                transport_key=transport_key,
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
        raise DeepFetchUnavailable("deepfetch_provider_recovery_exhausted")

    def _run_durable_segment(
        self,
        *,
        directory: Path,
        segment_name: str,
        job_ref: str,
        request: DeepFetchProviderRequest,
        prompt: str,
        native_session_ref: str | None,
        transport_key: bytes,
    ) -> tuple[str, dict[str, object] | None, str, str | None]:
        directory.mkdir(parents=True, exist_ok=True)
        schema = _deepfetch_output_schema()
        invocation = {
            "schema_ref": "meta-research/deepfetch-provider-operation/v1",
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
        }
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
                persisted = json.loads(invocation_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DeepFetchUnavailable(
                    "deepfetch_provider_spool_invalid"
                ) from error
            if persisted != envelope:
                raise DeepFetchUnavailable("deepfetch_provider_identity_conflict")

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
            argv = self._durable_argv(
                schema_path=schema_path,
                result_path=result_path,
                native_session_ref=native_session_ref,
            )
            supervisor_request_path = directory / "supervisor-request.json"
            try:
                write_supervisor_request(
                    supervisor_request_path,
                    {
                        "schema_ref": SUPERVISOR_REQUEST_SCHEMA,
                        "invocation_hash": invocation_hash,
                        "argv": argv,
                        "timeout_seconds": self._timeout_seconds,
                        "stream_max_bytes": PROVIDER_STREAM_MAX_BYTES,
                        "result_max_bytes": PROVIDER_RESULT_MAX_BYTES,
                        "prompt_path": str(prompt_path),
                        "schema_path": str(schema_path),
                        "stdout_path": str(stdout_path),
                        "result_path": str(result_path),
                        "lock_path": str(directory / "supervisor.lock"),
                        "ready_path": str(directory / "supervisor-ready.json"),
                        "started_path": str(directory / "provider-started.json"),
                        "receipt_path": str(receipt_path),
                    },
                    transport_key,
                )
                durable_job = self._runner.run_durable_job
                durable_job(
                    job_ref,
                    argv,
                    prompt,
                    self._timeout_seconds,
                    stdout_path,
                    directory / "pid.json",
                    supervisor_request_path,
                )
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
        return self._read_durable_segment(
            directory=directory,
            invocation_hash=invocation_hash,
            native_session_ref=native_session_ref,
            transport_key=transport_key,
        )

    def _durable_argv(
        self,
        *,
        schema_path: Path,
        result_path: Path,
        native_session_ref: str | None,
    ) -> list[str]:
        argv = [
            self._executable,
            "exec",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--strict-config",
            "--config",
            "mcp_servers={}",
            "--config",
            'approval_policy="never"',
            "--config",
            'web_search="live"',
            "--config",
            "features.multi_agent=true",
            "--sandbox",
            "workspace-write",
            "--model",
            self._model_ref,
            "--cd",
            str(self._workspace),
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
            if stdout_path.stat().st_size > PROVIDER_STREAM_MAX_BYTES:
                raise DeepFetchUnavailable("codex_deepfetch_output_too_large")
            stdout = stdout_path.read_text(encoding="utf-8")
        except DeepFetchUnavailable:
            raise
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise DeepFetchUnavailable("deepfetch_provider_spool_invalid") from error
        observed_session_ref = _thread_id(stdout)
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
            "acquisition_session_ref": request.acquisition_session_ref,
            "acquisition_config_hash": request.acquisition_config_hash,
            "acquisition_runtime_binding_hash": (
                request.acquisition_runtime_binding_hash
            ),
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
        "pending_acquisition",
        "next_prompt",
        "final_envelope",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_ref") != DEEPFETCH_PROTOCOL_CHECKPOINT_SCHEMA
        or value.get("identity_hash") != identity_hash
    ):
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
        or not 0 <= next_turn_number <= 12
        or not isinstance(evidence_parts, list)
        or len(evidence_parts) != next_turn_number
        or not isinstance(acquisition_request_ids, list)
        or any(
            not isinstance(item, str) or not item
            for item in acquisition_request_ids
        )
        or len(set(acquisition_request_ids)) != len(acquisition_request_ids)
    ):
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
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
        batch = _acquisition_batch_from_checkpoint(pending)
        if batch.request_id in acquisition_request_ids:
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


def _acquisition_batch_from_checkpoint(
    value: dict[str, object],
) -> AcquisitionBatchRequest:
    if value.get("schema_ref") != "meta-research/acquisition-batch-request/v1":
        raise DeepFetchUnavailable("deepfetch_protocol_checkpoint_invalid")
    envelope = {
        "action": "acquire",
        "acquisition_request": {
            key: item for key, item in value.items() if key != "schema_ref"
        },
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
        return _validated_v4_acquisition_request(envelope)
    except DeepFetchUnavailable as error:
        raise DeepFetchUnavailable(
            "deepfetch_protocol_checkpoint_invalid"
        ) from error


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


def _validated_v4_acquisition_request(
    envelope: dict[str, object],
) -> AcquisitionBatchRequest:
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
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "route_policy",
        "papers",
    }:
        raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
    request_id = value.get("request_id")
    papers_value = value.get("papers")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or value.get("route_policy") != "oa_first_then_institution"
        or not isinstance(papers_value, list)
        or not 1 <= len(papers_value) <= 10
    ):
        raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
    papers: list[AcquisitionPaper] = []
    paper_ids: set[str] = set()
    for paper in papers_value:
        if not isinstance(paper, dict) or set(paper) != {
            "paper_id",
            "title",
            "doi",
            "arxiv_id",
            "source_urls",
        }:
            raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
        paper_id = paper.get("paper_id")
        title = paper.get("title")
        doi = paper.get("doi")
        arxiv_id = paper.get("arxiv_id")
        urls = paper.get("source_urls")
        if (
            not isinstance(paper_id, str)
            or not paper_id
            or paper_id in paper_ids
            or not isinstance(title, str)
            or not title
            or doi is not None
            and not isinstance(doi, str)
            or arxiv_id is not None
            and not isinstance(arxiv_id, str)
            or not isinstance(urls, list)
            or len(urls) > 20
        ):
            raise DeepFetchUnavailable("deepfetch_acquisition_request_invalid")
        paper_ids.add(paper_id)
        papers.append(
            AcquisitionPaper(
                paper_id=paper_id,
                title=title,
                doi=cast(str | None, doi),
                arxiv_id=cast(str | None, arxiv_id),
                source_urls=tuple(
                    _validated_public_url(
                        url, "deepfetch_acquisition_source_url_invalid"
                    )
                    for url in urls
                ),
            )
        )
    return AcquisitionBatchRequest(
        request_id=request_id,
        route_policy="oa_first_then_institution",
        papers=tuple(papers),
    )


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
    acquisition_session_ref: str,
    acquisition_request_ids: tuple[str, ...],
    reader_agent_refs: tuple[str, ...],
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
    assignment_agent_refs = [
        cast(str, assignment["reader_agent_ref"])
        for assignment in assignments
    ]
    if (
        len(set(assignment_agent_refs)) != len(assignment_agent_refs)
        or set(assignment_agent_refs) != set(reader_agent_refs)
    ):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")

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
        "acquisition_session_ref": acquisition_session_ref,
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
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                return value
    return None


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
        "acquisition_session_ref",
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
        or not isinstance(value.get("acquisition_session_ref"), str)
        or not value.get("acquisition_session_ref")
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
    if tool not in {"spawn_agent", "wait"}:
        return
    receivers = item.get("receiver_thread_ids")
    states = item.get("agents_states")
    if (
        item.get("sender_thread_id") != root_session_ref
        or not isinstance(receivers, list)
        or not isinstance(states, dict)
    ):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    if tool == "spawn_agent":
        if (
            len(receivers) != 1
            or not isinstance(receivers[0], str)
            or not receivers[0]
            or not isinstance(states.get(receivers[0]), dict)
        ):
            raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
        spawned.append(receivers[0])
        return
    if receivers == [] and states == {}:
        return
    if any(not isinstance(value, str) or not value for value in receivers):
        raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
    for receiver in cast(list[str], receivers):
        state = states.get(receiver)
        if not isinstance(state, dict):
            raise DeepFetchUnavailable("deepfetch_reader_agent_trace_invalid")
        if state.get("status") == "completed":
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
) -> tuple[str, ...]:
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
    return tuple(spawned)


def _deepfetch_output_schema() -> dict[str, object]:
    acquisition_paper = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "paper_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "title": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "doi": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 512},
                    {"type": "null"},
                ]
            },
            "arxiv_id": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 512},
                    {"type": "null"},
                ]
            },
            "source_urls": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 8_000},
            },
        },
        "required": ["paper_id", "title", "doi", "arxiv_id", "source_urls"],
    }
    acquisition_request = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "route_policy": {
                "type": "string",
                "enum": ["oa_first_then_institution"],
            },
            "papers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": acquisition_paper,
            },
        },
        "required": ["request_id", "route_policy", "papers"],
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
