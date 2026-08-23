from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Callable, Protocol, cast

from meta_research.idea_contract import (
    DISPOSITION_ACTIONS,
    IDEA_OUTCOME_SCHEMA_REF,
    IDEA_REVIEW_SCHEMA_REF,
    REVIEW_CATEGORIES,
    IdeaContractError as IdeaSkillContractError,
    accepted_evidence_refs,
    material_outcome_hash,
    validate_advisory_review,
    validate_idea_outcome,
)
from meta_research.owners.agent_runtime import IdeaRuntimeBinding
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    SUPERVISOR_REQUEST_SCHEMA,
    ensure_transport_key,
    read_transport_envelope,
    read_transport_key_for_operation,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervisor_request_never_started,
    transport_key_hash,
    write_exit_receipt,
    write_supervisor_request,
    write_supervisor_stop_request,
)
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    _CancellableProcessRunner,
    _ProcessStopped,
    _text_exceeds_limit,
)


_DISABLED_CODEX_FEATURES = (
    "apps",
    "browser_use",
    "computer_use",
    "image_generation",
    "memories",
    "plugins",
    "remote_plugin",
)


class IdeaSkillUnavailable(RuntimeError):
    """The production Skill Adapter could not return a verifiable result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IdeaSkillRequest:
    stage_request_ref: str
    question_ref: str
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    accepted_question_content: dict[str, object]
    root_session_ref: str
    submission_revision: int
    runtime_binding: IdeaRuntimeBinding
    native_session_ref: str | None = None
    predecessor_submission_ref: str | None = None
    owner_rejection_receipt_ref: str | None = None
    owner_feedback: tuple[str, ...] = ()
    job_ref: str | None = None


@dataclass(frozen=True)
class IdeaSkillResult:
    reviewed_draft: dict[str, object]
    final_outcome: dict[str, object]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class IdeaSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str


class IdeaSkillProvider(Protocol):
    def runtime_binding(self) -> IdeaRuntimeBinding: ...

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft: ...

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult: ...

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult: ...

    def reconcile_cancelled_job(self, job_ref: str) -> bool: ...


def validate_idea_skill_draft(
    request: IdeaSkillRequest, result: IdeaSkillDraft
) -> str:
    """Validate a primary result before AR durably binds its native Session."""

    if request.submission_revision < 1:
        raise IdeaSkillContractError("submission_revision_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise IdeaSkillContractError("context_pack_hash_mismatch")
    for label, value in (
        ("stage_request_ref", request.stage_request_ref),
        ("question_ref", request.question_ref),
        ("context_pack_ref", request.context_pack_ref),
        ("root_session_ref", request.root_session_ref),
        ("primary_session_ref", result.primary_session_ref),
        ("adapter_kind", result.adapter_kind),
    ):
        _require_text(value, label)
    if result.primary_session_ref == request.root_session_ref:
        raise IdeaSkillContractError("native_session_not_provider_owned")
    if request.native_session_ref is not None and (
        result.primary_session_ref != request.native_session_ref
    ):
        raise IdeaSkillContractError("root_native_session_changed")
    return validate_idea_outcome(
        result.draft,
        question_ref=request.question_ref,
        context_pack_ref=request.context_pack_ref,
        accepted_evidence_refs=accepted_evidence_refs(request.context_pack),
    )


def validate_idea_skill_result(
    request: IdeaSkillRequest,
    result: IdeaSkillResult,
    *,
    predecessor_material_outcome_hash: str | None = None,
) -> tuple[str, str, str]:
    """Validate the complete Skill result and return draft/outcome/review hashes."""

    if request.submission_revision < 1:
        raise IdeaSkillContractError("submission_revision_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise IdeaSkillContractError("context_pack_hash_mismatch")
    for label, value in (
        ("stage_request_ref", request.stage_request_ref),
        ("question_ref", request.question_ref),
        ("context_pack_ref", request.context_pack_ref),
        ("root_session_ref", request.root_session_ref),
        ("primary_session_ref", result.primary_session_ref),
        ("reviewer_agent_ref", result.reviewer_agent_ref),
        ("adapter_kind", result.adapter_kind),
    ):
        _require_text(value, label)
    if result.review_mode != "harness_child_agent":
        raise IdeaSkillContractError("idea_review_mode_invalid")
    if result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise IdeaSkillContractError("idea_review_not_independent")
    if request.native_session_ref is not None and (
        result.primary_session_ref != request.native_session_ref
    ):
        raise IdeaSkillContractError("root_native_session_changed")

    evidence_refs = accepted_evidence_refs(request.context_pack)
    draft_hash = validate_idea_outcome(
        result.reviewed_draft,
        question_ref=request.question_ref,
        context_pack_ref=request.context_pack_ref,
        accepted_evidence_refs=evidence_refs,
    )
    outcome_hash = validate_idea_outcome(
        result.final_outcome,
        question_ref=request.question_ref,
        context_pack_ref=request.context_pack_ref,
        accepted_evidence_refs=evidence_refs,
    )

    feedback_revision = request.predecessor_submission_ref is not None
    if feedback_revision != (request.owner_rejection_receipt_ref is not None):
        raise IdeaSkillContractError("owner_feedback_lineage_incomplete")
    if feedback_revision:
        if not request.owner_feedback:
            raise IdeaSkillContractError("owner_feedback_missing")
        if predecessor_material_outcome_hash is None or (
            predecessor_material_outcome_hash
            == material_outcome_hash(result.final_outcome)
        ):
            raise IdeaSkillContractError("owner_feedback_revision_not_material")
    elif request.owner_feedback:
        raise IdeaSkillContractError("owner_feedback_without_rejection")

    review_payload = {
        "schema_ref": IDEA_REVIEW_SCHEMA_REF,
        "review_mode": result.review_mode,
        "reviewer_agent_ref": result.reviewer_agent_ref,
        "reviewed_draft_hash": draft_hash,
        "findings": list(result.findings),
        "dispositions": list(result.dispositions),
        "final_outcome_hash": outcome_hash,
        "independent": True,
        "advisory_only": True,
    }
    review_hash = validate_advisory_review(review_payload, outcome_hash=outcome_hash)
    return draft_hash, outcome_hash, review_hash


def review_record(
    result: IdeaSkillResult,
    *,
    draft_hash: str,
    outcome_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": IDEA_REVIEW_SCHEMA_REF,
        "review_mode": result.review_mode,
        "reviewer_agent_ref": result.reviewer_agent_ref,
        "reviewed_draft_hash": draft_hash,
        "findings": list(result.findings),
        "dispositions": list(result.dispositions),
        "final_outcome_hash": outcome_hash,
        "independent": True,
        "advisory_only": True,
    }


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise IdeaSkillContractError(f"{label}_invalid")


class CodexIdeaSkillAdapter:
    """Production Adapter: one root Codex Session plus an independent reviewer.

    It receives immutable data and returns a schema-constrained candidate/review.
    It has no Owner Interface and cannot create receipts or advance a Stage.
    """

    _sandbox_mode = "danger-full-access"
    _shell_environment_inherit: str | None = None

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = "gpt-5.4",
        timeout_seconds: float = 15 * 60,
        process_runner: Callable[
            [list[str], str, float], subprocess.CompletedProcess[str]
        ]
        | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._agent_workspace = self._workspace / "research-workspace"
        self._agent_workspace.mkdir(parents=True, exist_ok=True)
        self._executable = executable
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._runner = process_runner or _CancellableProcessRunner()

    def request_stop(self) -> None:
        request_stop = getattr(self._runner, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def cancel_job(self, job_ref: str) -> None:
        self._request_durable_job_stop(job_ref)
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_ref)

    def _request_durable_job_stop(self, job_ref: str) -> None:
        operation_root = (
            self._workspace
            / "provider-operations"
            / canonical_hash({"job_ref": job_ref})
        )
        if not operation_root.exists():
            return
        try:
            _key_path, transport_key = self._transport_key()
            for directory in operation_root.iterdir():
                invocation_path = directory / "invocation.json"
                if not directory.is_dir() or not invocation_path.is_file():
                    continue
                invocation = read_transport_envelope(
                    invocation_path, transport_key
                )
                if invocation.get("job_ref") != job_ref:
                    raise ProviderSupervisorError(
                        "provider_supervisor_stop_invalid"
                    )
                write_supervisor_stop_request(
                    directory / "supervisor-stop.json",
                    key=transport_key,
                    invocation_hash=canonical_hash(invocation),
                )
        except (OSError, ProviderSupervisorError) as error:
            raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error

    def finish_job(self, job_ref: str) -> None:
        finish_job = getattr(self._runner, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    def reconcile_cancelled_job(self, job_ref: str) -> bool:
        """Stop and verify every durable phase belonging to a terminal Run."""

        self.cancel_job(job_ref)
        operation_root = (
            self._workspace
            / "provider-operations"
            / canonical_hash({"job_ref": job_ref})
        )
        if not operation_root.exists():
            return True
        try:
            _key_path, key = self._transport_key()
            for operation_name in ("primary", "review"):
                directory = operation_root / operation_name
                if not directory.exists():
                    continue
                invocation_path = directory / "invocation.json"
                if not invocation_path.is_file():
                    if any(directory.iterdir()):
                        raise ProviderSupervisorError(
                            "provider_supervisor_spool_invalid"
                        )
                    continue
                invocation = read_transport_envelope(invocation_path, key)
                if (
                    set(invocation)
                    != {
                        "schema_ref",
                        "job_ref",
                        "operation_name",
                        "prompt_hash",
                        "output_schema_hash",
                        "native_session_ref",
                        "model_ref",
                        "transport_mode",
                    }
                    or invocation.get("schema_ref")
                    != "meta-research/codex-provider-operation/v1"
                    or invocation.get("job_ref") != job_ref
                    or invocation.get("operation_name") != operation_name
                ):
                    raise ProviderSupervisorError(
                        "provider_supervisor_spool_invalid"
                    )
                if invocation.get("transport_mode") != "durable_supervisor":
                    # An unsealed in-process runner has no cross-restart terminal
                    # proof. Keep cleanup pending instead of guessing it stopped.
                    return False
                invocation_hash = canonical_hash(invocation)
                receipt_path = directory / "supervisor-exit.json"
                if not receipt_path.is_file():
                    if not (directory / "supervisor-ready.json").is_file():
                        # No request means Popen was never attempted. A prepared
                        # request without a signed PID marker remains unknown and
                        # is retried by the next reconciliation pass.
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
                        # A dead supervisor cannot seal an exit receipt. The
                        # shared helper has instead verified/terminated every
                        # process carrying this exact operation token.
                        continue
                read_verified_exit_receipt(
                    receipt_path,
                    key=key,
                    invocation_hash=invocation_hash,
                    prompt_path=directory / "prompt.txt",
                    schema_path=directory / "output-schema.json",
                    stdout_path=directory / "stdout.jsonl",
                    result_path=directory / "last-message.json",
                )
        except (OSError, ProviderSupervisorError, IdeaSkillUnavailable):
            return False
        return True

    def runtime_binding(self) -> IdeaRuntimeBinding:
        resources = _idea_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        output_contracts = {
            "outcome-envelope-template": _outcome_envelope_schema(
                "__question_ref__", "__context_pack_ref__"
            ),
            "child-review-finalization-template": _review_finalization_schema(
                "__question_ref__",
                "__context_pack_ref__",
            ),
        }
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _idea_skill_instructions(),
                    "adapter_source_hash": adapter_source_hash,
                    "supervisor_source_hash": supervisor_source_hash,
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=(),
            capability_bindings=(
                "approval-policy-never",
                "filesystem-danger-full-access",
                "global-config-ignored",
                "harness-child-agent-review",
                "mcp-config-empty",
                "native-session-resume",
                "shell-tool-enabled",
                "structured-output-json-schema",
                "trusted-local-quest-authorization",
                "web-search-live",
            ),
            resource_bindings=tuple(
                f"package:meta_research.skills.idea_stage/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in output_contracts.items()
            )
            + harness_artifacts
            + (
                f"adapter-source:meta_research.idea_skill@sha256:"
                f"{adapter_source_hash}",
                f"adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:"
                + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"stream={PROVIDER_STREAM_MAX_BYTES};"
                f"result={PROVIDER_RESULT_MAX_BYTES}",
                "provider-timeout-seconds:"
                + format(self._timeout_seconds, ".17g"),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:"
                + transport_key_hash(transport_key),
            ),
        )

    def _transport_key(self) -> tuple[Path, bytes]:
        try:
            return ensure_transport_key(self._workspace)
        except (OSError, ProviderSupervisorError) as error:
            raise IdeaSkillUnavailable(
                "codex_transport_seal_unavailable"
            ) from error

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        if request.runtime_binding != self.runtime_binding():
            raise IdeaSkillUnavailable("idea_runtime_binding_drift")
        skill = _idea_skill_instructions()
        lineage = ""
        if request.owner_feedback:
            lineage = (
                "\n这是 RG rejection 后在同一根 Session 中的修订。必须实质改变 Outcome，"
                "并逐条处理正式 feedback。\n"
                f"predecessor_submission_ref={request.predecessor_submission_ref}\n"
                f"owner_rejection_receipt_ref={request.owner_rejection_receipt_ref}\n"
                f"owner_feedback={canonical_json(list(request.owner_feedback))}\n"
            )
        primary_prompt = (
            f"{skill}\n\n"
            "你是 Idea 主 Agent。只返回 {\"outcome\": ...}，其中 outcome 是一个完整 "
            "IdeaSet 或 NoViableCandidate。"
            "不得创建 Question、Plan、Run、receipt、selected Idea 或 StageCommit。"
            f"{lineage}\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"runtime_binding={canonical_json(request.runtime_binding.as_dict())}\n"
            f"accepted_question={canonical_json(request.accepted_question_content)}\n"
            f"context_pack={canonical_json(request.context_pack)}"
        )
        primary_output, primary_session, _primary_stdout = self._invoke(
            operation_name="primary",
            prompt=primary_prompt,
            schema=_outcome_envelope_schema(
                request.question_ref, request.context_pack_ref
            ),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
        if primary_session is None:
            raise IdeaSkillUnavailable("codex_primary_session_missing")
        draft_value = primary_output.get("outcome")
        if not isinstance(draft_value, dict):
            raise IdeaSkillUnavailable("codex_outcome_invalid")
        return IdeaSkillDraft(
            draft=cast(dict[str, object], draft_value),
            primary_session_ref=primary_session,
            adapter_kind="codex_cli",
        )

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult:
        if request.runtime_binding != self.runtime_binding():
            raise IdeaSkillUnavailable("idea_runtime_binding_drift")
        if request.native_session_ref != draft.primary_session_ref:
            raise IdeaSkillUnavailable("codex_primary_session_changed")
        skill = _idea_skill_instructions()

        reviewer_prompt = (
            f"{skill}\n\n"
            "你仍是根 Idea Agent。必须把独立 advisory reviewer 委派给 Harness：在当前 "
            "managed native Session 内使用 Harness "
            "原生 spawn_agent 能力以 fork_turns=\"none\" 启动一个全新上下文的短命 child "
            "reviewer，并 wait 到它完成；Claude Code 使用等价的全新上下文 "
            "subagent。不要另开、"
            "持久化或管理第二个顶层 Codex Session。child reviewer 只检查 Question 对齐、"
            "实质重复、证据边界、可证伪性与 Plan 可用性，不批准 Outcome、不评分、"
            "不选择 winner。根 Idea Agent 必须根据 child findings 逐条给出 revised | "
            "not_adopted disposition，并在同一个 resumed turn 返回最终完整 Outcome。"
            "revised 必须实际改变 Outcome；没有 finding 时返回空 findings/dispositions。"
            "只返回 reviewer_agent_ref、findings、final_outcome、dispositions。不得声称 "
            "reviewer 或根 Agent 拥有 Owner 接纳权。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"question={canonical_json(request.accepted_question_content)}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        reviewed, resumed_session, review_stdout = self._invoke(
            operation_name="review",
            prompt=reviewer_prompt,
            schema=_review_finalization_schema(
                request.question_ref, request.context_pack_ref
            ),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
        )
        if resumed_session != draft.primary_session_ref:
            raise IdeaSkillUnavailable("codex_primary_session_changed")
        reviewer_agent_ref = reviewed.get("reviewer_agent_ref")
        findings_value = reviewed.get("findings")
        final_value = reviewed.get("final_outcome")
        disposition_value = reviewed.get("dispositions")
        if (
            not isinstance(reviewer_agent_ref, str)
            or not reviewer_agent_ref
            or not isinstance(findings_value, list)
            or not isinstance(final_value, dict)
            or not isinstance(disposition_value, list)
        ):
            raise IdeaSkillUnavailable("codex_review_invalid")
        _verify_child_review_trace(
            review_stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer_agent_ref,
        )
        findings = tuple(cast(dict[str, str], item) for item in findings_value)
        dispositions = tuple(
            cast(dict[str, str], item) for item in disposition_value
        )

        result = IdeaSkillResult(
            reviewed_draft=draft.draft,
            final_outcome=cast(dict[str, object], final_value),
            findings=findings,
            dispositions=dispositions,
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer_agent_ref,
            adapter_kind=draft.adapter_kind,
        )
        return result

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref),
            draft,
        )

    def _invoke(
        self,
        *,
        operation_name: str,
        prompt: str,
        schema: dict[str, object],
        native_session_ref: str | None,
        job_ref: str | None,
    ) -> tuple[dict[str, object], str | None, str]:
        if job_ref is not None:
            operation_root = self._workspace / "provider-operations"
            directory = (
                operation_root
                / canonical_hash({"job_ref": job_ref})
                / operation_name
            )
            return self._invoke_durable(
                directory=directory,
                operation_name=operation_name,
                job_ref=job_ref,
                prompt=prompt,
                schema=schema,
                native_session_ref=native_session_ref,
            )
        with tempfile.TemporaryDirectory(
            prefix="idea-provider-", dir=self._workspace
        ) as raw_directory:
            return self._invoke_once(
                directory=Path(raw_directory),
                prompt=prompt,
                schema=schema,
                native_session_ref=native_session_ref,
                job_ref=None,
                stdout_path=None,
                invocation_hash=None,
            )

    def _invoke_durable(
        self,
        *,
        directory: Path,
        operation_name: str,
        job_ref: str,
        prompt: str,
        schema: dict[str, object],
        native_session_ref: str | None,
    ) -> tuple[dict[str, object], str | None, str]:
        directory.mkdir(parents=True, exist_ok=True)
        invocation_base = {
            "schema_ref": "meta-research/codex-provider-operation/v1",
            "job_ref": job_ref,
            "operation_name": operation_name,
            "prompt_hash": canonical_hash(prompt),
            "output_schema_hash": canonical_hash(schema),
            "native_session_ref": native_session_ref,
            "model_ref": self._model_ref,
        }
        current_transport_mode = (
            "durable_supervisor"
            if callable(getattr(self._runner, "run_durable_job", None))
            else "unreconciled_runner"
        )
        invocation = {
            **invocation_base,
            "transport_mode": current_transport_mode,
        }
        _key_path, transport_key = self._transport_key()
        invocation_json = _sealed_operation_invocation(invocation, transport_key)
        invocation_hash = canonical_hash(invocation)
        invocation_path = directory / "invocation.json"
        if not invocation_path.exists() and any(directory.iterdir()):
            raise IdeaSkillUnavailable("codex_operation_spool_invalid")
        created = _write_exclusive(invocation_path, invocation_json)
        if not created:
            persisted_invocation = _read_operation_invocation(
                invocation_path,
                key=transport_key,
                expected_base=invocation_base,
            )
            persisted_transport_mode = cast(
                str, persisted_invocation["transport_mode"]
            )
            invocation_hash = canonical_hash(persisted_invocation)
            provider_started = directory / "provider-started.json"
            effect_files = (
                directory / "stdout.jsonl",
                directory / "last-message.json",
                directory / ".last-message.supervisor.tmp",
                directory / "supervisor-exit.json",
                directory / "exit.json",
                directory / "completed.json",
            )
            if provider_started.exists() or any(
                path.exists() for path in effect_files
            ):
                return _read_completed_operation(
                    directory,
                    invocation_hash=invocation_hash,
                    native_session_ref=native_session_ref,
                )
            if persisted_transport_mode != "durable_supervisor":
                raise IdeaSkillUnavailable(
                    "codex_operation_reconciliation_pending"
                )

        _ensure_durable_text(directory / "prompt.txt", prompt)
        try:
            result = self._invoke_once(
                directory=directory,
                prompt=prompt,
                schema=schema,
                native_session_ref=native_session_ref,
                job_ref=job_ref,
                stdout_path=directory / "stdout.jsonl",
                invocation_hash=invocation_hash,
            )
        except IdeaSkillUnavailable as error:
            if error.code == "codex_cli_unavailable":
                # Popen never occurred, so retrying this prepared operation is safe.
                _remove_operation_spool(directory)
            raise
        _write_completed_operation(
            directory,
            invocation_hash=invocation_hash,
            decoded=result[0],
            native_session_ref=result[1],
        )
        return result

    def _invoke_once(
        self,
        *,
        directory: Path,
        prompt: str,
        schema: dict[str, object],
        native_session_ref: str | None,
        job_ref: str | None,
        stdout_path: Path | None,
        invocation_hash: str | None,
    ) -> tuple[dict[str, object], str | None, str]:
        directory.mkdir(parents=True, exist_ok=True)
        schema_path = directory / "output-schema.json"
        result_path = directory / "last-message.json"
        schema_json = canonical_json(schema)
        if schema_path.exists():
            if schema_path.read_text(encoding="utf-8") != schema_json:
                raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        else:
            _write_durable(schema_path, schema_json)
        argv = [
            self._executable,
            "exec",
            "--enable",
            "multi_agent",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--config",
            "mcp_servers={}",
            "--config",
            'approval_policy="never"',
            "--config",
            'web_search="live"',
            *(
                (
                    "--config",
                    "shell_environment_policy.inherit=\"none\"",
                )
                if self._shell_environment_inherit == "none"
                else ()
            ),
            *(
                value
                for feature in _DISABLED_CODEX_FEATURES
                for value in ("--disable", feature)
            ),
            "--sandbox",
            self._sandbox_mode,
            "--model",
            self._model_ref,
            "--cd",
            str(self._agent_workspace),
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
        try:
            if job_ref is None:
                completed = self._runner(argv, prompt, self._timeout_seconds)
            else:
                run_job = getattr(self._runner, "run_job", None)
                if not callable(run_job):
                    raise IdeaSkillUnavailable("codex_job_control_unavailable")
                durable_job = getattr(self._runner, "run_durable_job", None)
                supervised = False
                if callable(durable_job) and stdout_path is not None:
                    if invocation_hash is None:
                        raise IdeaSkillUnavailable(
                            "codex_operation_identity_missing"
                        )
                    _key_path, transport_key = self._transport_key()
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
                                "prompt_path": str(directory / "prompt.txt"),
                                "schema_path": str(schema_path),
                                "stdout_path": str(stdout_path),
                                "result_path": str(result_path),
                                "lock_path": str(
                                    directory / "supervisor.lock"
                                ),
                                "ready_path": str(
                                    directory / "supervisor-ready.json"
                                ),
                                "started_path": str(
                                    directory / "provider-started.json"
                                ),
                                "receipt_path": str(
                                    directory / "supervisor-exit.json"
                                ),
                                "stop_path": str(
                                    directory / "supervisor-stop.json"
                                ),
                            },
                            transport_key,
                        )
                    except (OSError, ProviderSupervisorError) as error:
                        raise IdeaSkillUnavailable(
                            "codex_operation_spool_unavailable"
                        ) from error
                    completed = durable_job(
                        job_ref,
                        argv,
                        prompt,
                        self._timeout_seconds,
                        stdout_path,
                        directory / "pid.json",
                        supervisor_request_path,
                    )
                    supervised = True
                else:
                    completed = run_job(
                        job_ref, argv, prompt, self._timeout_seconds
                    )
        except _ProcessStopped as error:
            raise IdeaSkillUnavailable("codex_cli_stopped") from error
        except FileNotFoundError as error:
            raise IdeaSkillUnavailable("codex_cli_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise IdeaSkillUnavailable("codex_cli_timeout") from error
        except OSError as error:
            raise IdeaSkillUnavailable("codex_cli_io_unavailable") from error
        if _text_exceeds_limit(
            completed.stdout, PROVIDER_STREAM_MAX_BYTES
        ) or _text_exceeds_limit(completed.stderr, PROVIDER_STREAM_MAX_BYTES):
            raise IdeaSkillUnavailable("codex_output_too_large")
        if stdout_path is not None and not stdout_path.exists():
            _write_durable(stdout_path, completed.stdout)
        effective_returncode = completed.returncode
        if invocation_hash is not None:
            if not supervised:
                _write_local_exit_receipt(
                    directory,
                    invocation_hash=invocation_hash,
                    returncode=completed.returncode,
                    input_bytes=len(prompt.encode("utf-8")),
                )
            exit_marker = _write_exit_marker(
                directory,
                invocation_hash=invocation_hash,
            )
            effective_returncode = cast(int, exit_marker["returncode"])
        if effective_returncode != 0:
            raise IdeaSkillUnavailable("codex_cli_failed")
        try:
            decoded = json.loads(_read_idea_result(result_path))
        except IdeaSkillUnavailable:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IdeaSkillUnavailable("codex_output_invalid") from error
        if not isinstance(decoded, dict):
            raise IdeaSkillUnavailable("codex_output_invalid")
        return (
            cast(dict[str, object], decoded),
            _verified_native_session(
                completed.stdout,
                expected=native_session_ref,
            ),
            completed.stdout,
        )


def _write_exclusive(path: Path, value: str) -> bool:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _sealed_operation_invocation(
    invocation: dict[str, object], key: bytes
) -> str:
    payload = canonical_json(invocation)
    return canonical_json(
        {
            "payload": invocation,
            "seal": hmac.new(
                key,
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        }
    )


def _read_operation_invocation(
    path: Path,
    *,
    key: bytes,
    expected_base: dict[str, object],
) -> dict[str, object]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    if not isinstance(envelope, dict):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    invocation = envelope.get("payload")
    seal = envelope.get("seal")
    if not isinstance(invocation, dict) or not isinstance(seal, str):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    typed_invocation = cast(dict[str, object], invocation)
    expected_seal = hmac.new(
        key,
        canonical_json(typed_invocation).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    transport_mode = typed_invocation.get("transport_mode")
    if (
        set(envelope) != {"payload", "seal"}
        or not hmac.compare_digest(seal, expected_seal)
        or set(typed_invocation) != {*expected_base, "transport_mode"}
        or any(typed_invocation.get(name) != value for name, value in expected_base.items())
        or transport_mode
        not in {"durable_supervisor", "unreconciled_runner"}
    ):
        raise IdeaSkillUnavailable("codex_operation_identity_conflict")
    return typed_invocation


def _write_durable(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_durable_text(path: Path, value: str) -> None:
    if _write_exclusive(path, value):
        return
    try:
        persisted = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    if persisted != value:
        raise IdeaSkillUnavailable("codex_operation_identity_conflict")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exit_marker(
    directory: Path,
    *,
    invocation_hash: str,
) -> dict[str, object]:
    prompt_hash, output_schema_hash = _verified_operation_inputs(
        directory,
        invocation_hash=invocation_hash,
    )
    receipt_path = directory / "supervisor-exit.json"
    if not receipt_path.exists():
        raise IdeaSkillUnavailable("codex_operation_reconciliation_pending")
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        receipt, envelope = read_verified_exit_receipt(
            receipt_path,
            key=transport_key,
            invocation_hash=invocation_hash,
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
        )
        stdout = _read_spool_text(
            directory / "stdout.jsonl", PROVIDER_STREAM_MAX_BYTES
        )
    except (OSError, ProviderSupervisorError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    result_path = directory / "last-message.json"
    termination_reason = cast(str, receipt["termination_reason"])
    provider_returncode = cast(int, receipt["returncode"])
    effective_returncode = (
        provider_returncode
        if termination_reason == "completed"
        else {
            "timeout": 124,
            "stopped": 143,
            "output_limit": 125,
            "descendant_process": 126,
            "launch_failed": 127,
        }[termination_reason]
    )
    marker: dict[str, object] = {
        "schema_ref": "meta-research/codex-provider-exit/v1",
        "invocation_hash": invocation_hash,
        "returncode": effective_returncode,
        "provider_returncode": provider_returncode,
        "termination_reason": termination_reason,
        "prompt_hash": prompt_hash,
        "output_schema_hash": output_schema_hash,
        "stdout_hash": canonical_hash(stdout),
        "result_file_hash": receipt["result_file_hash"],
        "supervisor_receipt_hash": canonical_hash(envelope),
    }
    encoded = canonical_json(marker)
    exit_path = directory / "exit.json"
    if not _write_exclusive(exit_path, encoded):
        try:
            persisted = exit_path.read_text(encoding="utf-8")
        except OSError as error:
            raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
        if persisted != encoded:
            raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    if marker["returncode"] == 0 and not result_path.is_file():
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    return marker


def _write_local_exit_receipt(
    directory: Path,
    *,
    invocation_hash: str,
    returncode: int,
    input_bytes: int,
) -> None:
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        write_exit_receipt(
            directory / "supervisor-exit.json",
            key=transport_key,
            invocation_hash=invocation_hash,
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
            returncode=returncode,
            input_bytes=input_bytes,
        )
    except (OSError, ProviderSupervisorError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error


def _verified_success_exit(
    directory: Path,
    *,
    invocation_hash: str,
) -> dict[str, object]:
    marker = _write_exit_marker(
        directory,
        invocation_hash=invocation_hash,
    )
    returncode = marker.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    if returncode != 0:
        raise IdeaSkillUnavailable("codex_operation_failed")
    return marker


def _verified_operation_inputs(
    directory: Path,
    *,
    invocation_hash: str,
) -> tuple[str, str]:
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        envelope = json.loads(
            (directory / "invocation.json").read_text(encoding="utf-8")
        )
        prompt = _read_spool_text(
            directory / "prompt.txt", PROVIDER_STREAM_MAX_BYTES
        )
        schema_text = _read_spool_text(
            directory / "output-schema.json", PROVIDER_RESULT_MAX_BYTES
        )
        schema = json.loads(schema_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    if (
        not isinstance(envelope, dict)
        or not isinstance(schema, dict)
        or not isinstance(envelope.get("payload"), dict)
        or not isinstance(envelope.get("seal"), str)
        or set(envelope) != {"payload", "seal"}
        or not hmac.compare_digest(
            cast(str, envelope["seal"]),
            hmac.new(
                transport_key,
                canonical_json(envelope["payload"]).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )
        or canonical_hash(envelope["payload"]) != invocation_hash
        or cast(dict[str, object], envelope["payload"]).get("schema_ref")
        != "meta-research/codex-provider-operation/v1"
        or cast(dict[str, object], envelope["payload"]).get("prompt_hash")
        != canonical_hash(prompt)
        or cast(dict[str, object], envelope["payload"]).get(
            "output_schema_hash"
        )
        != canonical_hash(schema)
    ):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    return canonical_hash(prompt), canonical_hash(schema)


def _verified_native_session(stdout: str, *, expected: str | None) -> str:
    observed_refs: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        observed = event.get("thread_id")
        if not isinstance(observed, str) or not observed:
            raise IdeaSkillUnavailable("codex_native_session_missing")
        observed_refs.append(observed)
    if not observed_refs:
        raise IdeaSkillUnavailable("codex_native_session_missing")
    unique_refs = set(observed_refs)
    if len(unique_refs) != 1:
        raise IdeaSkillUnavailable("codex_native_session_mismatch")
    observed = observed_refs[0]
    if expected is not None and observed != expected:
        raise IdeaSkillUnavailable("codex_native_session_mismatch")
    return observed


def _verify_child_review_trace(
    stdout: str,
    *,
    root_session_ref: str,
    reviewer_agent_ref: str,
    expected_spawn_prompt: str | None = None,
) -> str | None:
    successful_calls: dict[str, list[dict[str, object]]] = {
        "spawn_agent": [],
        "wait": [],
    }
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
            or item.get("status") != "completed"
        ):
            continue
        tool = item.get("tool")
        if tool in successful_calls:
            successful_calls[cast(str, tool)].append(
                cast(dict[str, object], item)
            )

    spawn_calls = successful_calls["spawn_agent"]
    if len(spawn_calls) != 1:
        raise IdeaSkillUnavailable("codex_child_review_spawn_invalid")
    spawn = spawn_calls[0]
    spawn_receivers = spawn.get("receiver_thread_ids")
    spawn_states = spawn.get("agents_states")
    if (
        spawn.get("sender_thread_id") != root_session_ref
        or not isinstance(spawn_receivers, list)
        or len(spawn_receivers) != 1
        or not isinstance(spawn_receivers[0], str)
        or not spawn_receivers[0]
        or not isinstance(spawn_states, dict)
    ):
        raise IdeaSkillUnavailable("codex_child_review_spawn_invalid")
    spawned_child_ref = spawn_receivers[0]
    spawned_state = spawn_states.get(spawned_child_ref)
    if (
        not isinstance(spawned_state, dict)
        or not isinstance(spawned_state.get("status"), str)
        or not spawned_state["status"]
    ):
        raise IdeaSkillUnavailable("codex_child_review_spawn_invalid")
    if reviewer_agent_ref != spawned_child_ref:
        raise IdeaSkillUnavailable("codex_child_review_ref_mismatch")
    if (
        expected_spawn_prompt is not None
        and spawn.get("prompt") != expected_spawn_prompt
    ):
        raise IdeaSkillUnavailable("codex_child_review_task_mismatch")

    wait_calls = successful_calls["wait"]
    if not wait_calls:
        raise IdeaSkillUnavailable("codex_child_review_wait_invalid")
    terminal_wait_seen = False
    terminal_message: str | None = None
    for wait in wait_calls:
        wait_receivers = wait.get("receiver_thread_ids")
        wait_states = wait.get("agents_states")
        if (
            wait.get("sender_thread_id") != root_session_ref
            or not isinstance(wait_receivers, list)
            or not isinstance(wait_states, dict)
        ):
            raise IdeaSkillUnavailable("codex_child_review_wait_invalid")
        # Codex can emit a completed, empty wait event when the bounded wait
        # times out, then wait again for the same child. It is not a second
        # reviewer and must not strand an otherwise valid durable result.
        if wait_receivers == [] and wait_states == {}:
            continue
        if wait_receivers != [spawned_child_ref]:
            raise IdeaSkillUnavailable("codex_child_review_wait_invalid")
        completed_state = wait_states.get(spawned_child_ref)
        if not isinstance(completed_state, dict):
            raise IdeaSkillUnavailable("codex_child_review_wait_invalid")
        if completed_state.get("status") == "completed":
            terminal_wait_seen = True
            message = completed_state.get("message")
            if message is not None:
                if not isinstance(message, str) or not message.strip():
                    raise IdeaSkillUnavailable(
                        "codex_child_review_wait_invalid"
                    )
                if terminal_message is not None and terminal_message != message:
                    raise IdeaSkillUnavailable(
                        "codex_child_review_wait_invalid"
                    )
                terminal_message = message
    if not terminal_wait_seen:
        raise IdeaSkillUnavailable("codex_child_review_wait_invalid")
    if expected_spawn_prompt is not None and terminal_message is None:
        raise IdeaSkillUnavailable("codex_child_review_result_missing")
    return terminal_message


def _write_completed_operation(
    directory: Path,
    *,
    invocation_hash: str,
    decoded: dict[str, object],
    native_session_ref: str | None,
) -> None:
    exit_marker = _verified_success_exit(
        directory,
        invocation_hash=invocation_hash,
    )
    stdout = _read_spool_text(
        directory / "stdout.jsonl", PROVIDER_STREAM_MAX_BYTES
    )
    verified_session = _verified_native_session(
        stdout,
        expected=native_session_ref,
    )
    completion = {
        "schema_ref": "meta-research/codex-provider-operation-result/v1",
        "invocation_hash": invocation_hash,
        "result": decoded,
        "result_hash": canonical_hash(decoded),
        "stdout_hash": canonical_hash(stdout),
        "exit_hash": canonical_hash(exit_marker),
        "native_session_ref": verified_session,
    }
    _write_durable(directory / "completed.json", canonical_json(completion))


def _read_completed_operation(
    directory: Path,
    *,
    invocation_hash: str,
    native_session_ref: str | None,
) -> tuple[dict[str, object], str | None, str]:
    exit_marker = _verified_success_exit(
        directory,
        invocation_hash=invocation_hash,
    )
    completion_path = directory / "completed.json"
    if not completion_path.exists():
        stdout_path = directory / "stdout.jsonl"
        result_path = directory / "last-message.json"
        if not stdout_path.exists() or not result_path.exists():
            raise IdeaSkillUnavailable("codex_operation_reconciliation_pending")
        try:
            stdout = _read_spool_text(stdout_path, PROVIDER_STREAM_MAX_BYTES)
            decoded = json.loads(_read_idea_result(result_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IdeaSkillUnavailable(
                "codex_operation_reconciliation_pending"
            ) from error
        if not isinstance(decoded, dict):
            raise IdeaSkillUnavailable("codex_operation_reconciliation_pending")
        recovered_session = _verified_native_session(
            stdout,
            expected=native_session_ref,
        )
        _write_completed_operation(
            directory,
            invocation_hash=invocation_hash,
            decoded=cast(dict[str, object], decoded),
            native_session_ref=recovered_session,
        )
    try:
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        stdout = _read_spool_text(
            directory / "stdout.jsonl", PROVIDER_STREAM_MAX_BYTES
        )
        persisted_result = json.loads(
            _read_idea_result(directory / "last-message.json")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    if not isinstance(completion, dict) or not isinstance(persisted_result, dict):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    decoded = completion.get("result")
    recovered_session = completion.get("native_session_ref")
    observed_session = _verified_native_session(
        stdout,
        expected=native_session_ref,
    )
    if (
        not isinstance(decoded, dict)
        or completion.get("schema_ref")
        != "meta-research/codex-provider-operation-result/v1"
        or completion.get("invocation_hash") != invocation_hash
        or completion.get("result_hash") != canonical_hash(decoded)
        or persisted_result != decoded
        or completion.get("stdout_hash") != canonical_hash(stdout)
        or completion.get("exit_hash") != canonical_hash(exit_marker)
        or recovered_session != observed_session
    ):
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")
    return (
        cast(dict[str, object], decoded),
        cast(str | None, recovered_session),
        stdout,
    )


def _read_spool_text(path: Path, limit: int) -> str:
    if path.stat().st_size > limit:
        raise IdeaSkillUnavailable("codex_output_too_large")
    with path.open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise IdeaSkillUnavailable("codex_output_too_large")
    return value.decode("utf-8")


def _remove_operation_spool(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _read_idea_result(path: Path) -> str:
    if path.stat().st_size > PROVIDER_RESULT_MAX_BYTES:
        raise IdeaSkillUnavailable("codex_output_too_large")
    with path.open("rb") as source:
        value = source.read(PROVIDER_RESULT_MAX_BYTES + 1)
    if len(value) > PROVIDER_RESULT_MAX_BYTES:
        raise IdeaSkillUnavailable("codex_output_too_large")
    return value.decode("utf-8")


def _codex_harness_manifest(
    executable: str,
) -> tuple[str, tuple[str, ...]]:
    resolved = shutil.which(executable)
    if resolved is None:
        raise IdeaSkillUnavailable("codex_cli_unavailable")
    entry = Path(resolved).resolve()
    try:
        artifacts = _discover_harness_artifacts(entry)
        version = subprocess.run(
            [str(entry), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IdeaSkillUnavailable("codex_cli_identity_unavailable") from error
    version_text = version.stdout.strip()
    if (
        version.returncode != 0
        or not version_text
        or len(version_text.encode("utf-8")) > 256
    ):
        raise IdeaSkillUnavailable("codex_cli_identity_unavailable")
    manifest = {
        str(path): _file_sha256(path)
        for path in sorted(artifacts, key=lambda item: str(item))
    }
    manifest_hash = canonical_hash(manifest)
    return (
        "codex-cli/exec-json-schema/v1;"
        f"entry={entry};version={version_text};"
        f"artifact_manifest_sha256={manifest_hash}",
        tuple(
            f"harness-artifact:{path}@sha256:{digest}"
            for path, digest in manifest.items()
        ),
    )


def _discover_harness_artifacts(entry: Path) -> set[Path]:
    pending = [entry]
    artifacts: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in artifacts or not path.is_file():
            continue
        artifacts.add(path)
        try:
            raw = path.read_bytes() if path.stat().st_size <= 2_000_000 else b""
        except OSError as error:
            raise IdeaSkillUnavailable("codex_cli_identity_unavailable") from error
        if not raw or b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="ignore")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if first_line.startswith("#!"):
            try:
                shebang = shlex.split(first_line[2:].strip())
            except ValueError:
                shebang = []
            if shebang:
                interpreter = shebang[0]
                if Path(interpreter).name == "env" and len(shebang) > 1:
                    interpreter = shebang[1]
                resolved_interpreter = shutil.which(interpreter)
                if resolved_interpreter is not None:
                    pending.append(Path(resolved_interpreter))
        for line in text.splitlines():
            if not re.match(r"^\s*exec(?:\s|$)", line):
                continue
            try:
                tokens = shlex.split(line.strip())
            except ValueError:
                continue
            for token in tokens[1:]:
                candidate = Path(token)
                if candidate.is_absolute() and candidate.is_file():
                    pending.append(candidate)
                elif "/" not in token:
                    resolved_token = shutil.which(token)
                    if resolved_token is not None:
                        pending.append(Path(resolved_token))
        if path.name == "codex.js":
            native = _codex_native_binary(path)
            if native is not None:
                pending.append(native)
    return artifacts


def _codex_native_binary(entrypoint: Path) -> Path | None:
    node_modules = next(
        (parent for parent in entrypoint.parents if parent.name == "node_modules"),
        None,
    )
    if node_modules is None:
        return None
    machine = platform.machine().lower()
    if sys.platform.startswith("linux") and machine in {"x86_64", "amd64"}:
        package_name = "codex-linux-x64"
        target = "x86_64-unknown-linux-musl"
    elif sys.platform.startswith("linux") and machine in {"aarch64", "arm64"}:
        package_name = "codex-linux-arm64"
        target = "aarch64-unknown-linux-musl"
    elif sys.platform == "darwin" and machine in {"x86_64", "amd64"}:
        package_name = "codex-darwin-x64"
        target = "x86_64-apple-darwin"
    elif sys.platform == "darwin" and machine in {"aarch64", "arm64"}:
        package_name = "codex-darwin-arm64"
        target = "aarch64-apple-darwin"
    else:
        return None
    candidate = (
        node_modules
        / "@openai"
        / package_name
        / "vendor"
        / target
        / "bin"
        / "codex"
    )
    return candidate if candidate.is_file() else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise IdeaSkillUnavailable("codex_cli_identity_unavailable") from error
    return digest.hexdigest()


def _idea_skill_resources() -> dict[str, str]:
    package = files("meta_research.skills.idea_stage")
    resources = (
        ("SKILL.md", package / "SKILL.md"),
        ("references/io-contract.md", package / "references" / "io-contract.md"),
        ("references/contract.md", package / "references" / "contract.md"),
    )
    try:
        return {
            name: resource.read_text(encoding="utf-8")
            for name, resource in resources
        }
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise IdeaSkillUnavailable("idea_skill_resource_unavailable") from error


def _idea_skill_instructions() -> str:
    return "\n\n".join(
        f"<!-- bundled resource: {name} -->\n{content}"
        for name, content in _idea_skill_resources().items()
    )


def _outcome_schema(question_ref: str, context_pack_ref: str) -> dict[str, object]:
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "accepted_evidence_refs": {"type": "array", "items": {"type": "string"}},
            "supported": {"type": "string", "minLength": 1},
            "inferred": {"type": "string", "minLength": 1},
            "unknown": {"type": "string", "minLength": 1},
        },
        "required": ["accepted_evidence_refs", "supported", "inferred", "unknown"],
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "candidate_key": {"type": "string", "minLength": 1},
            "direction": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
            "assumptions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "risks": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "evidence_boundary": evidence,
            "falsification_hint": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"test": {"type": "string", "minLength": 1}, "would_refute": {"type": "string", "minLength": 1}},
                "required": ["test", "would_refute"],
            },
            "material_difference": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "from_history": {"type": "string", "minLength": 1},
                    "from_peers": {"type": "string", "minLength": 1},
                    "plan_commitment_change": {"type": "string", "minLength": 1},
                },
                "required": ["from_history", "from_peers", "plan_commitment_change"],
            },
        },
        "required": ["candidate_key", "direction", "rationale", "assumptions", "risks", "evidence_boundary", "falsification_hint", "material_difference"],
    }
    base = {
        "question_ref": {"type": "string", "const": question_ref},
        "context_pack_ref": {"type": "string", "const": context_pack_ref},
    }
    return {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "IdeaSet"},
                    **base,
                    "candidates": {"type": "array", "minItems": 1, "items": candidate},
                    "recommendation": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"note": {"type": "string", "minLength": 1}, "binding": {"const": False}},
                                "required": ["note", "binding"],
                            },
                        ]
                    },
                },
                "required": ["kind", "question_ref", "context_pack_ref", "candidates", "recommendation"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "NoViableCandidate"},
                    **base,
                    "exploration_scope": {"type": "string", "minLength": 1},
                    "candidate_families_considered": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "family": {"type": "string", "minLength": 1},
                                "why_not_viable": {"type": "string", "minLength": 1},
                                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["family", "why_not_viable", "evidence_refs"],
                        },
                    },
                    "evidence_boundary": evidence,
                    "overturn_conditions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "why_plan_cannot_proceed": {"type": "string", "minLength": 1},
                },
                "required": ["kind", "question_ref", "context_pack_ref", "exploration_scope", "candidate_families_considered", "evidence_boundary", "overturn_conditions", "why_plan_cannot_proceed"],
            },
        ]
    }


def _outcome_envelope_schema(
    question_ref: str, context_pack_ref: str
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": _outcome_schema(question_ref, context_pack_ref),
        },
        "required": ["outcome"],
    }


def _review_finalization_schema(
    question_ref: str, context_pack_ref: str
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reviewer_agent_ref": {"type": "string", "minLength": 1},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": {"type": "string", "minLength": 1},
                        "category": {
                            "type": "string",
                            "enum": sorted(REVIEW_CATEGORIES),
                        },
                        "message": {"type": "string", "minLength": 1},
                    },
                    "required": ["finding_id", "category", "message"],
                },
            },
            "final_outcome": _outcome_schema(question_ref, context_pack_ref),
            "dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": {"type": "string", "minLength": 1},
                        "action": {"type": "string", "enum": sorted(DISPOSITION_ACTIONS)},
                        "rationale": {"type": "string", "minLength": 1},
                    },
                    "required": ["finding_id", "action", "rationale"],
                },
            },
        },
        "required": [
            "reviewer_agent_ref",
            "findings",
            "final_outcome",
            "dispositions",
        ],
    }
