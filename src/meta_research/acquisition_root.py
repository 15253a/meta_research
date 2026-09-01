from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPreflightRequest,
    AcquisitionPreflightResult,
    AcquisitionProvider,
    AcquisitionRuntimeBinding,
    AcquisitionUnavailable,
)
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    ensure_transport_key,
    read_transport_envelope,
    write_transport_envelope,
)
from meta_research.root_capabilities import merge_root_capability_bindings
from meta_research.root_operation_diagnostics import (
    RootOperationDiagnosticRecorder,
)


_ROOT_SESSION_RECEIPT_SCHEMA = "meta-research/acquisition-root-session/v1"


class CodexAcquisitionRootAdapter(AcquisitionProvider):
    """Long-lived Acquisition Root around a typed download adapter.

    Nature Downloader remains the effect adapter.  The Codex Root resumes one
    native Session per durable Acquisition session and accepts each typed
    adapter result before it crosses the provider boundary.  Its session
    receipts live in the provider's private spool and survive daemon restarts.
    """

    def __init__(
        self,
        workspace: Path,
        delegate: AcquisitionProvider,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        timeout_seconds: float | None = None,
        process_runner: Callable[
            [list[str], str, float | None], subprocess.CompletedProcess[str]
        ]
        | None = None,
        codex_home: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._delegate = delegate
        self._root = _AcquisitionCodexAdapter(
            workspace / "codex-root",
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
            codex_home=codex_home,
        )
        _key_path, self._transport_key = ensure_transport_key(self._workspace)

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        delegate = self._delegate.runtime_binding()
        delegate_hash = canonical_hash(delegate.as_dict())
        return AcquisitionRuntimeBinding(
            provider_ref=(
                "meta_research.acquisition_root.CodexAcquisitionRootAdapter"
            ),
            provider_version="v2+delegate-sha256:" + delegate_hash,
            capability_bindings=merge_root_capability_bindings(
                (
                    "acquisition-root-session-durable",
                    "nature-downloader-effect-adapter:sha256:" + delegate_hash,
                    *delegate.capability_bindings,
                ),
                "acquisition",
            ),
        )

    def preflight(
        self, request: AcquisitionPreflightRequest
    ) -> AcquisitionPreflightResult:
        result = self._delegate.preflight(request)
        decision = self._accept_result(
            session_ref=request.session_ref,
            job_ref=(
                f"acquisition:{request.session_ref}:preflight:"
                f"{request.config_hash}"
            ),
            phase="preflight",
            observed={
                "status": result.status,
                "reason_code": result.reason_code,
                "evidence": result.evidence,
            },
            allow_human_request=result.status == "waiting_user",
        )
        evidence = dict(result.evidence)
        evidence["acquisition_root"] = {
            "status": "accepted",
            "native_session_ref_hash": hashlib.sha256(
                decision["native_session_ref"].encode("utf-8")
            ).hexdigest(),
            "human_request_proposed": decision["human_request"] is not None,
        }
        if decision["human_request"] is not None:
            # This is explicit Root output, not a formal HumanRequest side
            # effect.  The authenticated effect interface remains authoritative.
            evidence["root_human_request_proposal"] = decision["human_request"]
        return replace(result, evidence=evidence)

    def acquire(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]:
        results = tuple(self._delegate.acquire(request))
        self._accept_result(
            session_ref=request.session_ref,
            job_ref=(
                f"acquisition:{request.session_ref}:batch:{request.request_id}"
            ),
            phase="batch",
            observed={
                "request_id": request.request_id,
                "results": [item.as_dict() for item in results],
            },
            allow_human_request=any(
                item.status == "waiting_user" for item in results
            ),
        )
        return results

    def reconcile(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]:
        results = tuple(self._delegate.reconcile(request))
        self._accept_result(
            session_ref=request.session_ref,
            job_ref=(
                f"acquisition:{request.session_ref}:reconcile:{request.request_id}"
            ),
            phase="reconcile",
            observed={
                "request_id": request.request_id,
                "results": [item.as_dict() for item in results],
            },
            allow_human_request=False,
        )
        return results

    def request_stop(self) -> None:
        self._root.request_stop()
        request_stop = getattr(self._delegate, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def bind_root_operation_diagnostics_recorder(
        self, recorder: RootOperationDiagnosticRecorder
    ) -> None:
        self._root.bind_root_operation_diagnostics_recorder(recorder)

    def _accept_result(
        self,
        *,
        session_ref: str,
        job_ref: str,
        phase: str,
        observed: dict[str, object],
        allow_human_request: bool,
    ) -> dict[str, object]:
        previous_native_session_ref = self._latest_native_session_ref(session_ref)
        human_request_schema: dict[str, object] = {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "clarification",
                                "authorization",
                                "resource",
                                "ethical_legal",
                            ],
                        },
                        "obligation": {"type": "string", "minLength": 1},
                    },
                    "required": ["kind", "obligation"],
                },
            ]
        }
        if not allow_human_request:
            human_request_schema = {"type": "null"}
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "accepted": {"const": True},
                "human_request": human_request_schema,
            },
            "required": ["accepted", "human_request"],
        }
        prompt = (
            "你是长期存在的 Acquisition 根智能体。Nature Downloader 是你的下载"
            " effect adapter；下面是它对当前精确任务返回的 typed observation。"
            "复核其状态并在可接受时返回 accepted=true。不要伪造下载结果。只有在"
            "确实需要用户提供访问、资源、澄清或伦理法律决定时，才返回一个明确的"
            " human_request proposal；provider 的 waiting_user 本身不等于请求。"
            "该 proposal 不是 formal HumanRequest，正式副作用只能由认证接口提交。\n\n"
            f"phase={phase}\n"
            f"session_ref={session_ref}\n"
            f"observation={canonical_json(observed)}"
        )
        try:
            raw, native_session_ref, _stdout = self._root._invoke(
                operation_name="acquisition-root-turn",
                prompt=prompt,
                schema=schema,
                native_session_ref=previous_native_session_ref,
                job_ref=job_ref,
            )
        except IdeaSkillUnavailable as error:
            raise AcquisitionUnavailable(error.code) from error
        if (
            native_session_ref is None
            or raw.get("accepted") is not True
            or set(raw) != {"accepted", "human_request"}
        ):
            raise AcquisitionUnavailable("acquisition_root_result_invalid")
        self._append_session_receipt(
            session_ref=session_ref,
            native_session_ref=native_session_ref,
            previous_native_session_ref=previous_native_session_ref,
            job_ref=job_ref,
            phase=phase,
        )
        return {
            "native_session_ref": native_session_ref,
            "human_request": raw["human_request"],
        }

    def _session_directory(self, session_ref: str) -> Path:
        return (
            self._workspace
            / "root-sessions"
            / hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
        )

    def _session_receipts(self, session_ref: str) -> tuple[dict[str, object], ...]:
        directory = self._session_directory(session_ref)
        if not directory.exists():
            return ()
        receipts: list[dict[str, object]] = []
        for generation, path in enumerate(sorted(directory.glob("turn-*.json")), 1):
            try:
                receipt = read_transport_envelope(path, self._transport_key)
            except (OSError, ProviderSupervisorError) as error:
                raise AcquisitionUnavailable(
                    "acquisition_root_session_invalid"
                ) from error
            if (
                receipt.get("schema_ref") != _ROOT_SESSION_RECEIPT_SCHEMA
                or receipt.get("session_ref") != session_ref
                or receipt.get("generation") != generation
                or not isinstance(receipt.get("native_session_ref"), str)
                or not receipt["native_session_ref"]
                or (
                    generation == 1
                    and receipt.get("previous_native_session_ref") is not None
                )
                or (
                    generation > 1
                    and receipt.get("previous_native_session_ref")
                    != receipts[-1]["native_session_ref"]
                )
            ):
                raise AcquisitionUnavailable("acquisition_root_session_invalid")
            receipts.append(receipt)
        return tuple(receipts)

    def _latest_native_session_ref(self, session_ref: str) -> str | None:
        receipts = self._session_receipts(session_ref)
        return None if not receipts else str(receipts[-1]["native_session_ref"])

    def _append_session_receipt(
        self,
        *,
        session_ref: str,
        native_session_ref: str,
        previous_native_session_ref: str | None,
        job_ref: str,
        phase: str,
    ) -> None:
        existing = self._session_receipts(session_ref)
        if existing and existing[-1].get("job_ref") == job_ref:
            if existing[-1].get("native_session_ref") != native_session_ref:
                raise AcquisitionUnavailable("acquisition_root_session_conflict")
            return
        directory = self._session_directory(session_ref)
        directory.mkdir(parents=True, exist_ok=True)
        generation = len(existing) + 1
        receipt = {
            "schema_ref": _ROOT_SESSION_RECEIPT_SCHEMA,
            "session_ref": session_ref,
            "generation": generation,
            "native_session_ref": native_session_ref,
            "previous_native_session_ref": previous_native_session_ref,
            "job_ref": job_ref,
            "phase": phase,
        }
        try:
            write_transport_envelope(
                directory / f"turn-{generation:08d}.json",
                receipt,
                self._transport_key,
            )
        except (OSError, ProviderSupervisorError) as error:
            raise AcquisitionUnavailable(
                "acquisition_root_session_unavailable"
            ) from error


class _AcquisitionCodexAdapter(CodexIdeaSkillAdapter):
    _root_agent_kind = "acquisition"
    _reconciliation_operation_names = ("acquisition-root-turn",)

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "acquisition-root-turn":
            return "acquisition_root_result_invalid"
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")


__all__ = ["CodexAcquisitionRootAdapter"]
