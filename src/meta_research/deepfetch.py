from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit

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
DEEPFETCH_RESULT_SCHEMA = "meta-research/first-question-deepfetch-result/v1"
DEEPFETCH_RUNTIME_BINDING_SCHEMA = "meta-research/deepfetch-runtime-binding/v1"
DEEPFETCH_WEB_EVIDENCE_SCHEMA = "meta-research/deepfetch-web-evidence/v1"
MAX_DEEPFETCH_PAPERS = 500
MAX_DEEPFETCH_FULLTEXTS = 100
MAX_DEEPFETCH_SUMMARY_LENGTH = 100_000
MAX_DEEPFETCH_FULLTEXT_LENGTH = 500_000
MAX_DEEPFETCH_LIMITATIONS = 100


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
    accepted_material_bindings: tuple[dict[str, object], ...]
    result_route: str
    authorization_receipt: AcceptanceReceipt

    def payload(self) -> dict[str, object]:
        return {
            "schema_ref": DEEPFETCH_REQUEST_SCHEMA,
            "request_ref": self.request_ref,
            "initialization_id": self.initialization_id,
            "correlation_ref": self.correlation_ref,
            "draft_revision": self.draft_revision,
            "draft_hash": self.draft_hash,
            "scope": self.scope,
            "scope_hash": self.scope_hash,
            "resource_envelope_ref": self.resource_envelope_ref,
            "resource_envelope_hash": self.resource_envelope_hash,
            "accepted_material_bindings": list(self.accepted_material_bindings),
            "result_route": self.result_route,
        }


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


class DeepFetchProvider(Protocol):
    def runtime_binding(self) -> DeepFetchRuntimeBinding: ...

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult: ...


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
        "fulltexts": list(fulltexts),
        "limitations": list(limitations),
        "native_session_ref": native_session_ref,
        "adapter_kind": adapter_kind,
        "web_evidence": web_evidence,
    }
    return payload, canonical_hash(payload)


class CodexDeepFetchAdapter:
    """Production Adapter for live, read-only Web Search and Web Fetch."""

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = "gpt-5.4",
        timeout_seconds: float = 30 * 60,
        process_runner: (
            Callable[[list[str], str, float], subprocess.CompletedProcess[str]] | None
        ) = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._executable = executable
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._runner = process_runner or _CancellableProcessRunner()

    def request_stop(self) -> None:
        request_stop = getattr(self._runner, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def cancel_job(self, job_ref: str) -> None:
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_ref)

    def finish_job(self, job_ref: str) -> None:
        finish_job = getattr(self._runner, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    @property
    def requires_verified_terminal_retry(self) -> bool:
        """Whether a new durable effect requires a signed terminal predecessor."""

        return callable(getattr(self._runner, "run_durable_job", None))

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="meta_research.deepfetch.CodexDeepFetchAdapter",
            provider_version="v1",
            model_ref=self._model_ref,
            harness_ref="codex-cli",
            capability_bindings=(
                "approval-policy-never",
                "filesystem-read-only",
                "structured-output-json-schema",
                "web-fetch-live",
                "web-search-live",
            ),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        prompt = (
            "你是 meta-research 的首问题 DeepFetch Web Research Adapter。"
            "只对给定的冻结 scope 执行真实 Web Search/Fetch，逐篇保留可核查 URL、"
            "DOI、获取时间和全文可用性。不得声称创建 Quest/Question/Cycle、接纳 Evidence"
            "或签发 receipt；不得输出 Cookie、凭据、浏览器状态或恢复文件。没有结果时"
            "必须返回 honest_empty，有缺全文或 reader failure 时必须返回 limited 并保留"
            "限制。fulltexts 只放合法取得的文本内容。\n\n"
            f"request_ref={request.request_ref}\n"
            f"draft_revision={request.draft_revision}\n"
            f"draft_hash={request.draft_hash}\n"
            f"scope={canonical_json(request.scope)}\n"
            "accepted_material_bindings="
            f"{canonical_json(list(request.accepted_material_bindings))}"
        )
        raw, native_session_ref, web_evidence = self._invoke(request, prompt)
        try:
            if set(raw) != {
                "completion",
                "summary",
                "papers",
                "fulltexts",
                "limitations",
            }:
                raise DeepFetchUnavailable("codex_deepfetch_output_invalid")
            result = DeepFetchResult(
                completion=cast(
                    Literal["complete", "limited", "honest_empty"],
                    raw["completion"],
                ),
                summary=cast(str, raw["summary"]),
                papers=tuple(cast(list[dict[str, object]], raw["papers"])),
                fulltexts=tuple(cast(list[dict[str, object]], raw["fulltexts"])),
                limitations=tuple(cast(list[str], raw["limitations"])),
                native_session_ref=native_session_ref,
                adapter_kind="codex_cli",
                web_evidence=web_evidence,
            )
            validate_deepfetch_result(request, result)
            return result
        except DeepFetchUnavailable as error:
            if self.requires_verified_terminal_retry:
                raise error.as_verified_terminal(native_session_ref) from error
            raise
        except (KeyError, TypeError, ValueError) as error:
            invalid = DeepFetchUnavailable("codex_deepfetch_output_invalid")
            if self.requires_verified_terminal_retry:
                invalid = invalid.as_verified_terminal(native_session_ref)
            raise invalid from error

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
                "--sandbox",
                "read-only",
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
            web_evidence = _verified_web_evidence(completed.stdout)
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
                    evidence = _verified_web_evidence("\n".join(trace_parts))
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
            "--sandbox",
            "read-only",
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
    if media_type not in {"text/plain", "text/markdown", "text/html"}:
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
    if set(value) != {
        "schema_ref",
        "search_event_count",
        "fetch_event_count",
        "trace_hash",
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
    return {
        "schema_ref": DEEPFETCH_WEB_EVIDENCE_SCHEMA,
        "search_event_count": search_count,
        "fetch_event_count": fetch_count,
        "trace_hash": trace_hash,
    }


def _verified_web_evidence(stdout: str) -> dict[str, object]:
    """Derive a non-secret proof from Codex-owned completed Web events."""

    trace: list[dict[str, object]] = []
    event_refs: set[str] = set()
    search_count = 0
    fetch_count = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "web_search":
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
    }
    validated = _validated_web_evidence(evidence)
    assert validated is not None
    return validated


def _deepfetch_output_schema() -> dict[str, object]:
    paper = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "url": {"type": "string", "minLength": 1, "maxLength": 8_000},
            "doi": {
                "anyOf": [
                    {"type": "string", "minLength": 1, "maxLength": 512},
                    {"type": "null"},
                ]
            },
            "source_kind": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
            "fulltext_status": {
                "type": "string",
                "enum": ["accepted", "unavailable", "not_attempted"],
            },
            "retrieved_at": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
        },
        "required": [
            "title",
            "url",
            "doi",
            "source_kind",
            "fulltext_status",
            "retrieved_at",
        ],
    }
    fulltext = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "paper_url": {"type": "string", "minLength": 1, "maxLength": 8_000},
            "media_type": {
                "type": "string",
                "enum": ["text/plain", "text/markdown", "text/html"],
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_DEEPFETCH_FULLTEXT_LENGTH,
            },
        },
        "required": ["paper_url", "media_type", "content"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "completion": {
                "type": "string",
                "enum": ["complete", "limited", "honest_empty"],
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_DEEPFETCH_SUMMARY_LENGTH,
            },
            "papers": {
                "type": "array",
                "maxItems": MAX_DEEPFETCH_PAPERS,
                "items": paper,
            },
            "fulltexts": {
                "type": "array",
                "maxItems": MAX_DEEPFETCH_FULLTEXTS,
                "items": fulltext,
            },
            "limitations": {
                "type": "array",
                "maxItems": MAX_DEEPFETCH_LIMITATIONS,
                "items": {"type": "string", "minLength": 1, "maxLength": 8_000},
            },
        },
        "required": [
            "completion",
            "summary",
            "papers",
            "fulltexts",
            "limitations",
        ],
    }
