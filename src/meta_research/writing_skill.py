from __future__ import annotations

import builtins
from dataclasses import dataclass, replace
import hashlib
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
from types import ModuleType
from typing import Protocol, cast

from meta_research.codex_runtime import CODEX_REASONING_EFFORT_BINDING
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillUnavailable,
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    _DISABLED_CODEX_FEATURES,
    _compile_codex_output_schema,
    _codex_harness_manifest,
    _file_sha256,
    _shared_codex_adapter_source_hash,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.provider_supervisor import transport_key_hash
from meta_research.root_capabilities import merge_root_capability_bindings
from meta_research.writing_contract import (
    WRITING_MAX_OUTPUT_BYTES,
    WRITING_ADVISORY_REVIEW_RUBRIC,
    WRITING_ADVISORY_REVIEW_TASK_SCHEMA,
    WritingRuntimeBinding,
    normalize_writing_intent,
    validate_writing_document,
    writing_advisory_review_document_profile,
    writing_advisory_review_task_hash,
    writing_document_profile,
)


WRITING_MARKDOWN_MAX_LENGTH = 2 * 1024 * 1024
WRITING_MAX_CITATIONS = 512
WRITING_MAX_REVIEW_FINDINGS = 128
WRITING_MAX_SOURCE_BYTES = 512 * 1024 * 1024
# 0030 changed this module's source hash when paper and presentation became
# first-class document types. Runs admitted by the immediately preceding 0029
# runtime keep this exact report-only executable and Skill bundle instead of
# having their persisted binding rewritten during upgrade. Provenance:
# c8322e93d02e18d3c8c2cd8ccd35c2705a46deb9.
_LEGACY_REPORT_BUNDLE = "legacy-report-c8322e93"
_LEGACY_REPORT_MODULE_NAME = (
    "meta_research._legacy_writing_report_c8322e93"
)
_LEGACY_REPORT_SUPERVISOR_MODULE_NAME = (
    "meta_research._legacy_provider_supervisor_c8322e93"
)
_LEGACY_REPORT_HASHES = {
    "writing_skill.py": (
        "a6ef050f866709ac6620d982caaf8f5e0d7bf21e548490968c9fa550b21bd453"
    ),
    "provider_supervisor.py": (
        "623fa83da2893790aaee02351865db1d2a1c9cc90987e11795ef34c3a24b7ab1"
    ),
    "SKILL.md": (
        "1a5a101300c8b86eabd74940fedc81afd1e40578436506589677439c222e9701"
    ),
    "agents/openai.yaml": (
        "5f344c6c9036d5f146efc38249c2b5166001673c3da8cd81428b15e82f576376"
    ),
}
_LEGACY_REPORT_LOAD_LOCK = threading.Lock()


class WritingSkillUnavailable(RuntimeError):
    """The production Writing Skill Adapter could not return a valid result."""

    def __init__(self, code: str, *, native_session_ref: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.native_session_ref = native_session_ref


@dataclass(frozen=True)
class WritingSourceMaterial:
    version_ref: str
    content_hash: str
    file_name: str
    media_type: str
    content: bytes
    materialized_sha256: str


@dataclass(frozen=True)
class WritingSkillRequest:
    run_ref: str
    attempt_ref: str
    fence_ref: str
    intent: dict[str, object]
    snapshot: dict[str, object]
    root_session_ref: str
    revision: int
    runtime_binding: WritingRuntimeBinding
    native_session_ref: str | None = None
    predecessor_version_ref: str | None = None
    predecessor_markdown_hash: str | None = None
    feedback: tuple[str, ...] = ()
    source_materials: tuple[WritingSourceMaterial, ...] = ()
    job_ref: str | None = None
    document_type: str = "report"
    profile_ref: str = "report-v1"


@dataclass(frozen=True)
class WritingSkillDraft:
    markdown: str
    citations: tuple[dict[str, str], ...]
    primary_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class WritingSkillResult:
    reviewed_markdown: str
    final_markdown: str
    citations: tuple[dict[str, str], ...]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str | None
    review_task_hash: str
    adapter_kind: str


class WritingSkillProvider(Protocol):
    def runtime_binding(
        self, document_type: str = "report"
    ) -> WritingRuntimeBinding: ...

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft: ...

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult: ...


def validate_writing_skill_draft(
    request: WritingSkillRequest, result: WritingSkillDraft
) -> str:
    _validate_request(request)
    _text(result.primary_session_ref, "writing_native_session_invalid", 128)
    _text(result.adapter_kind, "writing_adapter_kind_invalid", 128)
    if result.primary_session_ref == request.root_session_ref:
        raise OwnerConflict("writing_native_session_not_provider_owned")
    if (
        request.native_session_ref is not None
        and request.native_session_ref != result.primary_session_ref
    ):
        raise OwnerConflict("writing_native_session_changed")
    markdown_hash = _markdown_hash(result.markdown)
    _validate_citations(result.citations, request.snapshot)
    return markdown_hash


def validate_writing_skill_result(
    request: WritingSkillRequest,
    draft: WritingSkillDraft,
    result: WritingSkillResult,
) -> tuple[str, str, str]:
    draft_hash = validate_writing_skill_draft(request, draft)
    if result.reviewed_markdown != draft.markdown:
        raise OwnerConflict("writing_reviewed_checkpoint_mismatch")
    if result.primary_session_ref != draft.primary_session_ref:
        raise OwnerConflict("writing_native_session_changed")
    if (
        result.review_mode != "advisory_unobserved"
        or result.reviewer_agent_ref is not None
    ):
        raise OwnerConflict("writing_review_mode_invalid")
    expected_review_task_hash = writing_review_task_hash(request, draft)
    if result.review_task_hash != expected_review_task_hash:
        raise OwnerConflict("writing_review_task_invalid")
    final_hash = _markdown_hash(result.final_markdown)
    citations_hash = _validate_citations(result.citations, request.snapshot)
    if request.document_type != "report":
        validate_writing_document(
            request.document_type, result.final_markdown, result.citations
        )
    if len(result.findings) > WRITING_MAX_REVIEW_FINDINGS or len(
        result.dispositions
    ) != len(result.findings):
        raise OwnerConflict("writing_review_invalid")
    for finding, disposition in zip(
        result.findings, result.dispositions, strict=True
    ):
        if set(finding) != {"category", "finding"} or set(disposition) != {
            "category",
            "action",
            "reason",
        }:
            raise OwnerConflict("writing_review_invalid")
        if finding["category"] != disposition["category"]:
            raise OwnerConflict("writing_review_invalid")
        _text(finding["category"], "writing_review_invalid", 64)
        _text(finding["finding"], "writing_review_invalid", 4000)
        _text(disposition["reason"], "writing_review_invalid", 4000)
        if disposition["action"] not in {"revised", "not_adopted"}:
            raise OwnerConflict("writing_review_invalid")
    if any(item["action"] == "revised" for item in result.dispositions) and (
        final_hash == draft_hash and result.citations == draft.citations
    ):
        raise OwnerConflict("writing_review_revision_not_material")
    if (
        request.feedback
        and final_hash == request.predecessor_markdown_hash
    ):
        raise OwnerConflict("writing_feedback_revision_not_material")
    review_hash = canonical_hash(
        {
            "review_mode": result.review_mode,
            "reviewer_agent_ref": result.reviewer_agent_ref,
            "review_task_hash": result.review_task_hash,
            "reviewed_markdown_hash": draft_hash,
            "findings": list(result.findings),
            "dispositions": list(result.dispositions),
            "final_markdown_hash": final_hash,
            "citations_hash": citations_hash,
        }
    )
    return final_hash, citations_hash, review_hash


class CodexWritingSkillAdapter(CodexIdeaSkillAdapter):
    """Codex Harness Adapter for one resumable Writing root Session."""

    # Writing consumes untrusted Intent/source text. Its custom permission
    # profile keeps the Frozen Snapshot read-only and writes confined to the
    # dedicated provider workspace. Root tool availability remains complete;
    # the prompt and Owner acceptance boundary constrain what may be committed.
    _root_agent_kind = "writing"
    _shell_environment_inherit = "none"
    _web_search_mode = "live"
    _reconciliation_operation_names = ("writing-primary", "writing-review")

    def _sandbox_arguments(
        self, sandbox_read_root: Path | None
    ) -> tuple[str, ...]:
        if sandbox_read_root is None:
            raise WritingSkillUnavailable("writing_source_read_root_missing")
        try:
            source_root = sandbox_read_root.resolve(strict=True)
            staging_root = (self._workspace / "writing-inputs").resolve(
                strict=True
            )
            agent_workspace = self._agent_workspace.resolve(strict=True)
        except OSError as error:
            raise WritingSkillUnavailable(
                "writing_source_read_root_invalid"
            ) from error
        if source_root.parent != staging_root:
            raise WritingSkillUnavailable("writing_source_read_root_invalid")
        _require_safe_staging_directory(source_root)
        profile = (
            'permissions.writing_snapshot={description="Frozen Writing '
            'Snapshot only",filesystem={":minimal"="read",'
            f'{json.dumps(str(agent_workspace))}="write",'
            f'{json.dumps(str(source_root))}="read"}},'
            'network={enabled=true}}'
        )
        return (
            "--config",
            profile,
            "--config",
            'default_permissions="writing_snapshot"',
        )

    def runtime_binding(self, document_type: str = "report") -> WritingRuntimeBinding:
        resources = _writing_skill_resources(document_type)
        try:
            resident_mcp_facts = self._resident_mcp_runtime_facts()
        except IdeaSkillUnavailable as error:
            raise WritingSkillUnavailable(error.code) from error
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_hash = _file_sha256(Path(__file__).resolve())
        shared_adapter_source_hash = _shared_codex_adapter_source_hash()
        resident_mcp_source_hash = _file_sha256(
            Path(__file__).with_name("root_resident_mcp.py").resolve()
        )
        supervisor_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        output_contracts = {
            "writing-draft": _draft_schema(),
            "writing-advisory-finalization": _review_schema(),
        }
        output_contracts = {
            name: _compile_codex_output_schema(schema)
            for name, schema in output_contracts.items()
        }
        return WritingRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _writing_skill_instructions(document_type),
                    "adapter_source_hash": adapter_hash,
                    "shared_adapter_source_hash": shared_adapter_source_hash,
                    "resident_mcp_source_hash": resident_mcp_source_hash,
                    "supervisor_source_hash": supervisor_hash,
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=resident_mcp_facts.mcp_bindings,
            capability_bindings=merge_root_capability_bindings(
                (
                    "approval-policy-never",
                    "accepted-rm-source-staging",
                    "environment-inheritance-none",
                    "filesystem-read-root-confined",
                    "user-config-loaded",
                    *(
                        ("mcp-config-empty",)
                        if not resident_mcp_facts.mcp_bindings
                        else resident_mcp_facts.capability_bindings
                    ),
                    "native-session-resume",
                    "structured-output-json-schema",
                    "trusted-local-quest-authorization",
                ),
                self._root_agent_kind,
            ),
            resource_bindings=tuple(
                f"package:meta_research.skills.writing-report/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in output_contracts.items()
            )
            + tuple(harness_artifacts)
            + (
                f"adapter-source:meta_research.writing_skill@sha256:{adapter_hash}",
                "adapter-source:meta_research.idea_skill@sha256:"
                f"{shared_adapter_source_hash}",
                "adapter-source:meta_research.root_resident_mcp@sha256:"
                f"{resident_mcp_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:default_permissions=writing_snapshot",
                "codex-config:features.multi_agent=true",
                CODEX_REASONING_EFFORT_BINDING,
                "codex-config:permissions.writing_snapshot=exact-frozen-read-root",
                "codex-config:shell_environment_policy.inherit=none",
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"stream={PROVIDER_STREAM_MAX_BYTES};"
                f"result={PROVIDER_RESULT_MAX_BYTES}",
                self._provider_wall_clock_binding(),
                "runtime-policy:untrusted-writing-input-confined/v1",
                "sandbox-policy:permission-profile;minimal=read;"
                "agent-workspace=write;frozen-source-root=read;network=true",
                "external-effects:forbidden",
                "transport-seal-key:sha256:"
                + transport_key_hash(transport_key),
                "writing-run-limits:"
                "revisions=unbounded;accumulated-output=unbounded;"
                f"artifact-output={WRITING_MAX_OUTPUT_BYTES}",
            )
            + resident_mcp_facts.resource_bindings,
        )

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        legacy = self._legacy_report_adapter_for(request)
        if legacy is not None:
            module, adapter = legacy
            try:
                result = adapter.generate_draft(request)
            except Exception as error:
                legacy_unavailable = getattr(
                    module, "WritingSkillUnavailable"
                )
                if isinstance(error, legacy_unavailable):
                    raise WritingSkillUnavailable(str(error.code)) from error
                raise
            return WritingSkillDraft(
                markdown=result.markdown,
                citations=result.citations,
                primary_session_ref=result.primary_session_ref,
                adapter_kind=result.adapter_kind,
            )
        source_manifest = self._stage_source_materials(request)
        source_root = Path(cast(str, source_manifest["manifest_path"])).parent
        lineage = (
            ""
            if not request.feedback
            else (
                "\n这是同一 Writing Session 内的 successor revision。逐条处理 feedback，"
                "不得覆盖历史版本。\n"
                f"predecessor_version_ref={request.predecessor_version_ref}\n"
                "predecessor_markdown_hash="
                f"{request.predecessor_markdown_hash}\n"
                f"feedback={canonical_json(list(request.feedback))}\n"
            )
        )
        human_resume = (
            ""
            if request.native_session_ref is None
            else "\n若本 Session 曾显式打开 HumanRequest，先以原 effect_id 调 "
            "human_request.open.reconcile，读取 resolution 后再判断"
            "信息是否足够；旧 receipt 不能释放新的 waiter。\n"
        )
        prompt = (
            f"{_writing_skill_instructions(request.document_type)}\n\n"
            f"{human_resume}"
            f"你是 {request.document_type} Writing 根 Agent。只返回 markdown 与 citations。引用必须绑定"
            "冻结 Snapshot accepted_sources 中的 version_ref；不得生成 receipt，不得"
            "发布、发送、提交外部系统或推进 Quest Stage。"
            f"{lineage}\nrun_ref={request.run_ref}\n"
            f"intent={canonical_json(request.intent)}\n"
            f"snapshot={canonical_json(request.snapshot)}\n"
            "accepted_source_manifest="
            f"{canonical_json(source_manifest)}"
        )
        session_ref: str | None = None
        try:
            output, session_ref, _stdout = self._invoke_root_operation(
                operation_name="writing-primary",
                prompt=prompt,
                schema=_draft_schema(),
                native_session_ref=request.native_session_ref,
                job_ref=request.job_ref,
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                runtime_binding=request.runtime_binding.as_dict(),
                sandbox_read_root=source_root,
            )
            if not isinstance(session_ref, str) or not session_ref:
                raise WritingSkillUnavailable("writing_native_session_missing")
            draft = WritingSkillDraft(
                markdown=cast(str, output.get("markdown")),
                citations=_citations(output.get("citations")),
                primary_session_ref=session_ref,
                adapter_kind="codex_cli",
            )
            validate_writing_skill_draft(request, draft)
        except Exception as error:
            if isinstance(error, WritingSkillUnavailable):
                raise
            code = getattr(error, "code", "writing_provider_unavailable")
            raise WritingSkillUnavailable(
                str(code),
                native_session_ref=(
                    getattr(error, "native_session_ref", None)
                    or (
                        session_ref
                        if isinstance(session_ref, str) and session_ref
                        else None
                    )
                ),
            ) from error
        return draft

    def _legacy_report_adapter_for(
        self, request: WritingSkillRequest
    ) -> tuple[ModuleType, CodexWritingSkillAdapter] | None:
        if request.runtime_binding == self.runtime_binding(
            request.document_type
        ):
            return None
        if request.document_type != "report":
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        if request.profile_ref != "report-v1":
            raise OwnerConflict("writing_document_profile_invalid")
        module = _load_legacy_report_module()
        adapter_type = getattr(module, "CodexWritingSkillAdapter")
        adapter = cast(
            CodexWritingSkillAdapter,
            adapter_type.__new__(adapter_type),
        )
        adapter.__dict__.update(self.__dict__)
        timeout_bindings = tuple(
            item
            for item in request.runtime_binding.resource_bindings
            if item.startswith("provider-timeout-seconds:")
        )
        if len(timeout_bindings) != 1:
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        try:
            legacy_timeout = float(timeout_bindings[0].rsplit(":", 1)[1])
        except ValueError as error:
            raise WritingSkillUnavailable(
                "writing_runtime_binding_drift"
            ) from error
        if not math.isfinite(legacy_timeout) or legacy_timeout <= 0:
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        # The hash-pinned adapter predates unbounded production execution and
        # formats its original numeric ceiling into the historical binding.
        adapter._timeout_seconds = legacy_timeout
        if request.runtime_binding != adapter.runtime_binding():
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        return module, adapter

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        legacy = self._legacy_report_adapter_for(request)
        if legacy is not None:
            module, adapter = legacy
            try:
                result = adapter.review_draft(request, draft)
            except Exception as error:
                legacy_unavailable = getattr(
                    module, "WritingSkillUnavailable"
                )
                if isinstance(error, legacy_unavailable):
                    raise WritingSkillUnavailable(str(error.code)) from error
                raise
            return WritingSkillResult(
                reviewed_markdown=result.reviewed_markdown,
                final_markdown=result.final_markdown,
                citations=result.citations,
                findings=result.findings,
                dispositions=result.dispositions,
                primary_session_ref=result.primary_session_ref,
                review_mode=result.review_mode,
                reviewer_agent_ref=result.reviewer_agent_ref,
                review_task_hash=result.review_task_hash,
                adapter_kind=result.adapter_kind,
            )
        if request.native_session_ref != draft.primary_session_ref:
            raise WritingSkillUnavailable("writing_native_session_changed")
        source_manifest = self._stage_source_materials(request)
        source_root = Path(cast(str, source_manifest["manifest_path"])).parent
        review_task_hash = writing_review_task_hash(request, draft)
        advisory_prompt = _advisory_review_prompt(
            request,
            draft,
            source_manifest=source_manifest,
            review_task_hash=review_task_hash,
        )
        prompt = (
            f"{_writing_skill_instructions(request.document_type)}\n\n"
            f"你仍是同一个 {request.document_type} Writing 根 Agent。本回合执行第二次 "
            "advisory finalization provider turn：重新检查 exact frozen draft、citations、"
            "Intent、Snapshot 与 source manifest，形成 bounded findings；对每条 finding "
            "给出 revised | not_adopted disposition，并在当前 resumed Session 返回最终 "
            "Markdown 与 citations。revised 必须实际改变稿件或 citations。不要返回或"
            "声称 reviewer identity，不调用 Owner 写入。可按需使用无 Owner "
            "authority 的 advisory child，但验收不依赖 child 数量、拓扑、"
            "顺序或 reviewer identity。\n"
            f"{advisory_prompt}"
        )
        try:
            output, session_ref, _stdout = self._invoke_root_operation(
                operation_name="writing-review",
                prompt=prompt,
                schema=_review_schema(),
                native_session_ref=draft.primary_session_ref,
                job_ref=request.job_ref,
                run_ref=request.run_ref,
                attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref,
                fence_ref=request.fence_ref,
                runtime_binding=request.runtime_binding.as_dict(),
                sandbox_read_root=source_root,
            )
        except Exception as error:
            code = getattr(error, "code", "writing_provider_unavailable")
            raise WritingSkillUnavailable(
                str(code),
                native_session_ref=getattr(error, "native_session_ref", None),
            ) from error
        if session_ref != draft.primary_session_ref:
            raise WritingSkillUnavailable("writing_native_session_changed")
        result = WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=cast(str, output.get("final_markdown")),
            citations=_citations(output.get("citations")),
            findings=_review_items(output.get("findings")),
            dispositions=_review_items(output.get("dispositions")),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            review_task_hash=review_task_hash,
            adapter_kind=draft.adapter_kind,
        )
        validate_writing_skill_result(request, draft, result)
        return result

    def _stage_source_materials(
        self, request: WritingSkillRequest
    ) -> dict[str, object]:
        _validate_source_materials(request)
        snapshot_hash = request.snapshot.get("snapshot_hash")
        assert isinstance(snapshot_hash, str)
        # The provider has write access to ``_agent_workspace``.  Immutable RM
        # evidence therefore lives in a sibling directory that the workspace
        # sandbox can read but cannot replace with a symlink.
        staging = self._workspace / "writing-inputs"
        root = staging / snapshot_hash
        try:
            staging.mkdir(parents=True, exist_ok=True, mode=0o700)
            _require_safe_staging_directory(staging)
            root.mkdir(exist_ok=True, mode=0o700)
            _require_safe_staging_directory(root)
            rows: list[dict[str, object]] = []
            for ordinal, material in enumerate(request.source_materials):
                suffix = "".join(
                    character
                    for character in Path(material.file_name).suffix
                    if character.isalnum() or character in {".", "_", "-"}
                )[:32]
                file_name = (
                    f"{ordinal:04d}-"
                    f"{canonical_hash(material.version_ref)[:16]}{suffix}"
                )
                path = root / file_name
                _ensure_durable_bytes(path, material.content)
                rows.append(
                    {
                        "ordinal": ordinal,
                        "version_ref": material.version_ref,
                        "content_hash": material.content_hash,
                        "materialized_sha256": material.materialized_sha256,
                        "media_type": material.media_type,
                        "file_name": material.file_name,
                        "path": str(path),
                    }
                )
            manifest = {
                "schema_ref": "meta-research/writing-source-manifest/v1",
                "snapshot_hash": snapshot_hash,
                "sources": rows,
            }
            manifest_path = root / "manifest.json"
            _ensure_durable_bytes(
                manifest_path, canonical_json(manifest).encode("utf-8")
            )
            return {**manifest, "manifest_path": str(manifest_path)}
        except OSError as error:
            raise WritingSkillUnavailable(
                "writing_source_staging_unavailable"
            ) from error

    def execute(self, request: WritingSkillRequest) -> WritingSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref), draft
        )


def writing_review_task_hash(
    request: WritingSkillRequest, draft: WritingSkillDraft
) -> str:
    snapshot_hash = request.snapshot.get("snapshot_hash")
    if not isinstance(snapshot_hash, str):
        raise OwnerConflict("writing_snapshot_invalid")
    return writing_advisory_review_task_hash(
        run_ref=request.run_ref,
        provider_job_ref=request.job_ref,
        root_session_ref=request.root_session_ref,
        primary_session_ref=draft.primary_session_ref,
        intent_hash=canonical_hash(request.intent),
        snapshot_hash=snapshot_hash,
        predecessor_version_ref=request.predecessor_version_ref,
        predecessor_markdown_hash=request.predecessor_markdown_hash,
        feedback_hash=canonical_hash(list(request.feedback)),
        reviewed_markdown_hash=_markdown_hash(draft.markdown),
        reviewed_citations_hash=canonical_hash(list(draft.citations)),
        document_type=request.document_type,
    )


def _advisory_review_prompt(
    request: WritingSkillRequest,
    draft: WritingSkillDraft,
    *,
    source_manifest: dict[str, object],
    review_task_hash: str,
) -> str:
    task = {
        "schema_ref": WRITING_ADVISORY_REVIEW_TASK_SCHEMA,
        "review_task_hash": review_task_hash,
        "run_ref": request.run_ref,
        "provider_job_ref": request.job_ref,
        "root_session_ref": request.root_session_ref,
        "primary_session_ref": draft.primary_session_ref,
        "intent": request.intent,
        "snapshot": request.snapshot,
        "predecessor_version_ref": request.predecessor_version_ref,
        "predecessor_markdown_hash": request.predecessor_markdown_hash,
        "feedback": list(request.feedback),
        "accepted_source_manifest": source_manifest,
        "reviewed_markdown": draft.markdown,
        "reviewed_citations": list(draft.citations),
        "rubric": list(WRITING_ADVISORY_REVIEW_RUBRIC),
    }
    if request.document_type != "report":
        document_profile = writing_advisory_review_document_profile(
            request.document_type
        )
        if document_profile is None:
            raise OwnerConflict("writing_document_profile_invalid")
        task = {
            **task,
            "document_type": request.document_type,
            "profile_ref": request.profile_ref,
            "document_profile": document_profile,
        }
    review_subject = (
        "按 rubric 检查报告。"
        if request.document_type == "report"
        else "按 rubric 与 document profile 检查稿件。"
    )
    return (
        "只处理下面这一个精确 advisory task。读取 manifest 中已冻结文件，"
        f"{review_subject}最终响应只含 findings、dispositions、final_markdown、citations。"
        "findings 每项只含 category 与 finding；dispositions 与 findings 顺序一一对应。"
        f"\nadvisory_task={canonical_json(task)}"
    )


def _validate_request(request: WritingSkillRequest) -> None:
    request.runtime_binding.validate()
    profile = writing_document_profile(request.document_type)
    if request.profile_ref != profile.profile_ref:
        raise OwnerConflict("writing_document_profile_invalid")
    if normalize_writing_intent(request.document_type, request.intent) != request.intent:
        raise OwnerConflict("writing_intent_invalid")
    for value, code in (
        (request.run_ref, "writing_run_ref_invalid"),
        (request.attempt_ref, "writing_attempt_ref_invalid"),
        (request.fence_ref, "writing_fence_ref_invalid"),
        (request.root_session_ref, "writing_root_session_ref_invalid"),
    ):
        _text(value, code, 128)
    if request.revision < 1:
        raise OwnerConflict("writing_revision_invalid")
    snapshot_without_hash = dict(request.snapshot)
    embedded_hash = snapshot_without_hash.pop("snapshot_hash", None)
    if not isinstance(embedded_hash, str) or canonical_hash(snapshot_without_hash) != embedded_hash:
        raise OwnerConflict("writing_snapshot_hash_mismatch")
    if bool(request.feedback) != (
        request.predecessor_version_ref is not None
        and request.predecessor_markdown_hash is not None
    ):
        raise OwnerConflict("writing_revision_lineage_invalid")
    if request.predecessor_markdown_hash is not None and (
        len(request.predecessor_markdown_hash) != 64
        or any(
            character not in "0123456789abcdef"
            for character in request.predecessor_markdown_hash
        )
    ):
        raise OwnerConflict("writing_revision_lineage_invalid")
    _validate_source_materials(request)


def _validate_source_materials(request: WritingSkillRequest) -> None:
    sources = request.snapshot.get("accepted_sources")
    if not isinstance(sources, list):
        raise OwnerConflict("writing_snapshot_invalid")
    source_by_ref: dict[str, dict[str, object]] = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(
            source.get("version_ref"), str
        ):
            raise OwnerConflict("writing_snapshot_invalid")
        version_ref = cast(str, source["version_ref"])
        prior = source_by_ref.get(version_ref)
        if prior is not None:
            for field in (
                "asset_ref",
                "content_hash",
                "manifest_hash",
                "asset_receipt",
            ):
                if prior.get(field) != source.get(field):
                    raise OwnerConflict("writing_snapshot_invalid")
            continue
        source_by_ref[version_ref] = source
    seen: set[str] = set()
    total_bytes = 0
    for material in request.source_materials:
        source = source_by_ref.get(material.version_ref)
        if (
            source is None
            or material.version_ref in seen
            or source.get("content_hash") != material.content_hash
            or not isinstance(material.content, bytes)
            or hashlib.sha256(material.content).hexdigest()
            != material.materialized_sha256
            or not material.file_name
            or len(material.file_name) > 512
            or not material.media_type
            or len(material.media_type) > 255
        ):
            raise OwnerConflict("writing_source_material_invalid")
        seen.add(material.version_ref)
        total_bytes += len(material.content)
    if seen != set(source_by_ref) or total_bytes > WRITING_MAX_SOURCE_BYTES:
        raise OwnerConflict("writing_source_material_invalid")


def _markdown_hash(value: object) -> str:
    _text(value, "writing_markdown_invalid", WRITING_MARKDOWN_MAX_LENGTH)
    assert isinstance(value, str)
    return canonical_hash({"media_type": "text/markdown; charset=utf-8", "content": value})


def _validate_citations(
    values: tuple[dict[str, str], ...], snapshot: dict[str, object]
) -> str:
    if len(values) > WRITING_MAX_CITATIONS:
        raise OwnerConflict("writing_citations_invalid")
    sources = snapshot.get("accepted_sources")
    if not isinstance(sources, list):
        raise OwnerConflict("writing_snapshot_invalid")
    allowed = {
        item.get("version_ref")
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("version_ref"), str)
    }
    seen: set[str] = set()
    for citation in values:
        if set(citation) != {
            "citation_ref",
            "source_version_ref",
            "locator",
            "claim",
            "source_quote",
        }:
            raise OwnerConflict("writing_citations_invalid")
        for key, maximum in (
            ("citation_ref", 128),
            ("source_version_ref", 128),
            ("locator", 1000),
            ("claim", 4000),
            ("source_quote", 8000),
        ):
            _text(citation[key], "writing_citations_invalid", maximum)
        if citation["citation_ref"] in seen:
            raise OwnerConflict("writing_citations_invalid")
        seen.add(citation["citation_ref"])
        if citation["source_version_ref"] not in allowed:
            raise OwnerConflict("writing_citation_source_unaccepted")
    return canonical_hash(list(values))


def _citations(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise WritingSkillUnavailable("writing_citations_invalid")
    if any(not isinstance(item, dict) for item in value):
        raise WritingSkillUnavailable("writing_citations_invalid")
    return tuple(cast(dict[str, str], item) for item in value)


def _review_items(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WritingSkillUnavailable("writing_review_invalid")
    return tuple(cast(dict[str, str], item) for item in value)


def _text(value: object, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OwnerConflict(code)
    return value


def _ensure_durable_bytes(path: Path, value: bytes) -> None:
    _require_safe_staging_directory(path.parent)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = path.lstat()
                if not stat.S_ISREG(existing.st_mode):
                    raise WritingSkillUnavailable(
                        "writing_source_staging_unsafe"
                    )
                read_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(read_descriptor, "rb") as source:
                    persisted = source.read(len(value) + 1)
                if persisted != value:
                    raise WritingSkillUnavailable(
                        "writing_source_staging_conflict"
                    )
            except OSError as error:
                raise WritingSkillUnavailable(
                    "writing_source_staging_unavailable"
                ) from error
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _require_safe_staging_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WritingSkillUnavailable(
            "writing_source_staging_unavailable"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise WritingSkillUnavailable("writing_source_staging_unsafe")


def _load_legacy_report_module() -> ModuleType:
    root = (
        Path(__file__).resolve().parent
        / "skills"
        / "writing-report"
        / "references"
        / _LEGACY_REPORT_BUNDLE
    )
    with _LEGACY_REPORT_LOAD_LOCK:
        try:
            bundle = {
                name: (root / name).resolve().read_bytes()
                for name in _LEGACY_REPORT_HASHES
            }
            for name, expected_hash in _LEGACY_REPORT_HASHES.items():
                if hashlib.sha256(bundle[name]).hexdigest() != expected_hash:
                    raise WritingSkillUnavailable(
                        "writing_legacy_runtime_invalid"
                    )
            skill_resources = {
                "SKILL.md": bundle["SKILL.md"].decode("utf-8"),
                "agents/openai.yaml": bundle["agents/openai.yaml"].decode(
                    "utf-8"
                ),
            }
            supervisor_path = root / "provider_supervisor.py"
            supervisor = ModuleType(
                _LEGACY_REPORT_SUPERVISOR_MODULE_NAME
            )
            supervisor.__file__ = str(supervisor_path)
            supervisor.__package__ = "meta_research"
            prior_supervisor = sys.modules.get(
                _LEGACY_REPORT_SUPERVISOR_MODULE_NAME
            )
            sys.modules[_LEGACY_REPORT_SUPERVISOR_MODULE_NAME] = supervisor
            try:
                supervisor_code = compile(
                    bundle["provider_supervisor.py"],
                    str(supervisor_path),
                    "exec",
                )
                exec(supervisor_code, supervisor.__dict__)
            finally:
                if prior_supervisor is None:
                    sys.modules.pop(
                        _LEGACY_REPORT_SUPERVISOR_MODULE_NAME, None
                    )
                else:
                    sys.modules[
                        _LEGACY_REPORT_SUPERVISOR_MODULE_NAME
                    ] = prior_supervisor

            original_import = builtins.__import__

            def legacy_import(
                name: str,
                globals: dict[str, object] | None = None,
                locals: dict[str, object] | None = None,
                fromlist: tuple[str, ...] = (),
                level: int = 0,
            ) -> object:
                if (
                    level == 0
                    and name == "meta_research.provider_supervisor"
                    and fromlist
                ):
                    return supervisor
                return original_import(name, globals, locals, fromlist, level)

            source_path = root / "writing_skill.py"
            module = ModuleType(_LEGACY_REPORT_MODULE_NAME)
            module.__file__ = str(source_path)
            module.__package__ = "meta_research"
            module_builtins = dict(vars(builtins))
            module_builtins["__import__"] = legacy_import
            module.__dict__["__builtins__"] = module_builtins
            prior_module = sys.modules.get(_LEGACY_REPORT_MODULE_NAME)
            sys.modules[_LEGACY_REPORT_MODULE_NAME] = module
            try:
                code = compile(
                    bundle["writing_skill.py"], str(source_path), "exec"
                )
                exec(code, module.__dict__)
                if (
                    getattr(module, "transport_key_hash", None)
                    is not getattr(supervisor, "transport_key_hash", None)
                ):
                    raise WritingSkillUnavailable(
                        "writing_legacy_runtime_invalid"
                    )
            finally:
                if prior_module is None:
                    sys.modules.pop(_LEGACY_REPORT_MODULE_NAME, None)
                else:
                    sys.modules[_LEGACY_REPORT_MODULE_NAME] = prior_module
        except WritingSkillUnavailable:
            raise
        except Exception as error:
            raise WritingSkillUnavailable(
                "writing_legacy_runtime_unavailable"
            ) from error

    def resources() -> dict[str, str]:
        return dict(skill_resources)

    setattr(module, "_writing_skill_resources", resources)
    return module


def _writing_skill_resources(document_type: str = "report") -> dict[str, str]:
    profile = writing_document_profile(document_type)
    root = files("meta_research") / "skills" / "writing-report"
    resources = {
        "SKILL.md": (root / "SKILL.md").read_text(encoding="utf-8"),
        "agents/openai.yaml": (root / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        ),
    }
    if document_type != "report":
        reference_name = f"references/{profile.profile_ref}-advisory.md"
        resources[reference_name] = (root / reference_name).read_text(
            encoding="utf-8"
        )
    return resources


def _writing_skill_instructions(document_type: str = "report") -> str:
    resources = _writing_skill_resources(document_type)
    instructions = resources["SKILL.md"]
    if document_type == "report":
        return instructions
    profile = writing_document_profile(document_type)
    return (
        instructions
        + "\n\n"
        + resources[f"references/{profile.profile_ref}-advisory.md"]
    )


def _citation_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "citation_ref",
            "source_version_ref",
            "locator",
            "claim",
            "source_quote",
        ],
        "properties": {
            "citation_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "source_version_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "locator": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            },
            "claim": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
            },
            "source_quote": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8000,
            },
        },
    }


def _draft_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["markdown", "citations"],
        "properties": {
            "markdown": {"type": "string", "minLength": 1},
            "citations": {
                "type": "array",
                "maxItems": WRITING_MAX_CITATIONS,
                "items": _citation_schema(),
            },
        },
    }


def _review_schema() -> dict[str, object]:
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "finding"],
        "properties": {
            "category": {"type": "string", "minLength": 1},
            "finding": {"type": "string", "minLength": 1},
        },
    }
    disposition = {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "action", "reason"],
        "properties": {
            "category": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": ["revised", "not_adopted"]},
            "reason": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "findings",
            "dispositions",
            "final_markdown",
            "citations",
        ],
        "properties": {
            "findings": {"type": "array", "items": finding},
            "dispositions": {"type": "array", "items": disposition},
            "final_markdown": {"type": "string", "minLength": 1},
            "citations": {
                "type": "array",
                "maxItems": WRITING_MAX_CITATIONS,
                "items": _citation_schema(),
            },
        },
    }
