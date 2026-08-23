from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import stat
from typing import Protocol, cast

from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    _DISABLED_CODEX_FEATURES,
    _codex_harness_manifest,
    _file_sha256,
    _verify_child_review_trace,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.provider_supervisor import transport_key_hash
from meta_research.writing_contract import (
    WRITING_MAX_CONTENT_REVISIONS,
    WRITING_MAX_OUTPUT_BYTES,
    WRITING_CHILD_REVIEW_RUBRIC,
    WRITING_CHILD_REVIEW_TASK_SCHEMA,
    WritingRuntimeBinding,
    writing_child_review_task_hash,
)


WRITING_MARKDOWN_MAX_LENGTH = 2 * 1024 * 1024
WRITING_MAX_CITATIONS = 512
WRITING_MAX_REVIEW_FINDINGS = 128
WRITING_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_WRITING_CHILD_REVIEW_RESULT_SCHEMA = (
    "meta-research/writing-child-review-result/v1"
)
_CHILD_TASK_BEGIN = "<<<WRITING_CHILD_REVIEW_TASK_BEGIN>>>"
_CHILD_TASK_END = "<<<WRITING_CHILD_REVIEW_TASK_END>>>"


class WritingSkillUnavailable(RuntimeError):
    """The production Writing Skill Adapter could not return a valid result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    reviewer_agent_ref: str
    review_task_hash: str
    adapter_kind: str


class WritingSkillProvider(Protocol):
    def runtime_binding(self) -> WritingRuntimeBinding: ...

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
    if result.review_mode != "harness_child_agent":
        raise OwnerConflict("writing_review_mode_invalid")
    _text(result.reviewer_agent_ref, "writing_reviewer_invalid", 128)
    if result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise OwnerConflict("writing_review_not_independent")
    expected_review_task_hash = writing_review_task_hash(request, draft)
    if result.review_task_hash != expected_review_task_hash:
        raise OwnerConflict("writing_review_task_invalid")
    final_hash = _markdown_hash(result.final_markdown)
    citations_hash = _validate_citations(result.citations, request.snapshot)
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

    # Writing consumes untrusted Intent/source text. Generated shell commands
    # are confined to its dedicated provider workspace with no inherited host
    # environment; networked discovery remains the read-only Harness search
    # tool, not arbitrary shell egress.
    _sandbox_mode = "workspace-write"
    _shell_environment_inherit = "none"

    def runtime_binding(self) -> WritingRuntimeBinding:
        resources = _writing_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_hash = _file_sha256(Path(__file__).resolve())
        supervisor_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        output_contracts = {
            "writing-draft": _draft_schema(),
            "writing-child-review-finalization": _review_schema(),
        }
        return WritingRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _writing_skill_instructions(),
                    "adapter_source_hash": adapter_hash,
                    "supervisor_source_hash": supervisor_hash,
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=(),
            capability_bindings=(
                "approval-policy-never",
                "accepted-rm-source-staging",
                "environment-inheritance-none",
                "filesystem-workspace-write",
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
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                "codex-config:shell_environment_policy.inherit=none",
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"stream={PROVIDER_STREAM_MAX_BYTES};"
                f"result={PROVIDER_RESULT_MAX_BYTES}",
                "provider-timeout-seconds:"
                + format(self._timeout_seconds, ".17g"),
                "runtime-policy:untrusted-writing-input-confined/v1",
                "sandbox-policy:workspace-write;network=false;host-env=none",
                "external-effects:forbidden",
                "transport-seal-key:sha256:"
                + transport_key_hash(transport_key),
                "writing-run-limits:"
                f"revisions={WRITING_MAX_CONTENT_REVISIONS};"
                f"output={WRITING_MAX_OUTPUT_BYTES}",
            ),
        )

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        if request.runtime_binding != self.runtime_binding():
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        source_manifest = self._stage_source_materials(request)
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
        prompt = (
            f"{_writing_skill_instructions()}\n\n"
            "你是 report Writing 根 Agent。只返回 markdown 与 citations。引用必须绑定"
            "冻结 Snapshot accepted_sources 中的 version_ref；不得生成 receipt，不得"
            "发布、发送、提交外部系统或推进 Quest Stage。"
            f"{lineage}\nrun_ref={request.run_ref}\n"
            f"intent={canonical_json(request.intent)}\n"
            f"snapshot={canonical_json(request.snapshot)}\n"
            "accepted_source_manifest="
            f"{canonical_json(source_manifest)}"
        )
        try:
            output, session_ref, _stdout = self._invoke(
                operation_name="writing-primary",
                prompt=prompt,
                schema=_draft_schema(),
                native_session_ref=request.native_session_ref,
                job_ref=request.job_ref,
            )
        except Exception as error:
            if isinstance(error, WritingSkillUnavailable):
                raise
            code = getattr(error, "code", "writing_provider_unavailable")
            raise WritingSkillUnavailable(str(code)) from error
        if not isinstance(session_ref, str) or not session_ref:
            raise WritingSkillUnavailable("writing_native_session_missing")
        draft = WritingSkillDraft(
            markdown=cast(str, output.get("markdown")),
            citations=_citations(output.get("citations")),
            primary_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        validate_writing_skill_draft(request, draft)
        return draft

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        if request.runtime_binding != self.runtime_binding():
            raise WritingSkillUnavailable("writing_runtime_binding_drift")
        if request.native_session_ref != draft.primary_session_ref:
            raise WritingSkillUnavailable("writing_native_session_changed")
        source_manifest = self._stage_source_materials(request)
        review_task_hash = writing_review_task_hash(request, draft)
        child_prompt = _child_review_prompt(
            request,
            draft,
            source_manifest=source_manifest,
            review_task_hash=review_task_hash,
        )
        root_context_canary = canonical_hash(
            {
                "kind": "writing-root-context-canary",
                "root_session_ref": request.root_session_ref,
                "review_task_hash": review_task_hash,
            }
        )
        prompt = (
            f"{_writing_skill_instructions()}\n\n"
            "你仍是 report Writing 根 Agent。使用 Harness 原生 spawn_agent，以"
            " fork_turns=\"none\" 启动一个短命 fresh-context child reviewer，并等待"
            "完成。spawn_agent 的 message 必须逐字等于下方标记内文本。这个 root-only"
            " canary 不得复制到 child message；若 child 因继承历史而能看到它，child"
            " 必须如实返回 context_canary_seen=true，此次执行将 fail closed。根 Agent"
            " 必须逐条处置 child 返回的原始 findings，并在当前 resumed Session 返回最终"
            " Markdown 与 citations。\n"
            f"root_context_canary={root_context_canary}\n"
            f"{_CHILD_TASK_BEGIN}\n{child_prompt}\n{_CHILD_TASK_END}"
        )
        try:
            output, session_ref, stdout = self._invoke(
                operation_name="writing-review",
                prompt=prompt,
                schema=_review_schema(),
                native_session_ref=draft.primary_session_ref,
                job_ref=request.job_ref,
            )
        except Exception as error:
            code = getattr(error, "code", "writing_provider_unavailable")
            raise WritingSkillUnavailable(str(code)) from error
        if session_ref != draft.primary_session_ref:
            raise WritingSkillUnavailable("writing_native_session_changed")
        reviewer_agent_ref = output.get("reviewer_agent_ref")
        if not isinstance(reviewer_agent_ref, str):
            raise WritingSkillUnavailable("writing_review_invalid")
        try:
            child_message = _verify_child_review_trace(
                stdout,
                root_session_ref=draft.primary_session_ref,
                reviewer_agent_ref=reviewer_agent_ref,
                expected_spawn_prompt=child_prompt,
            )
        except Exception as error:
            code = getattr(error, "code", "writing_child_review_trace_invalid")
            raise WritingSkillUnavailable(str(code)) from error
        _validate_child_review_message(
            child_message,
            review_task_hash=review_task_hash,
            expected_findings=output.get("findings"),
        )
        result = WritingSkillResult(
            reviewed_markdown=draft.markdown,
            final_markdown=cast(str, output.get("final_markdown")),
            citations=_citations(output.get("citations")),
            findings=_review_items(output.get("findings")),
            dispositions=_review_items(output.get("dispositions")),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer_agent_ref,
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
    return writing_child_review_task_hash(
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
    )


def _child_review_prompt(
    request: WritingSkillRequest,
    draft: WritingSkillDraft,
    *,
    source_manifest: dict[str, object],
    review_task_hash: str,
) -> str:
    task = {
        "schema_ref": WRITING_CHILD_REVIEW_TASK_SCHEMA,
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
        "rubric": list(WRITING_CHILD_REVIEW_RUBRIC),
        "fresh_context_mode": "fork_turns:none",
    }
    return (
        "你是一次性 Writing child reviewer，只能审查下面这一个精确任务。不得创建"
        " Owner receipt、修改文件、发布或启动其他 Agent。读取 manifest 中已冻结文件，"
        "按 rubric 检查报告。若你从任何继承的 root 历史看到了名为"
        " root_context_canary 的 token，必须返回 context_canary_seen=true；否则为 false。"
        "只返回单个 JSON 对象，不要 Markdown fence。返回对象必须精确包含"
        " schema_ref、review_task_hash、context_canary_seen、findings；findings 每项只含"
        " category 与 finding。\nreview_task="
        f"{canonical_json(task)}\nresponse_schema_ref="
        f"{_WRITING_CHILD_REVIEW_RESULT_SCHEMA}"
    )


def _validate_child_review_message(
    message: str | None,
    *,
    review_task_hash: str,
    expected_findings: object,
) -> None:
    if not isinstance(message, str):
        raise WritingSkillUnavailable("writing_child_review_result_missing")
    try:
        value = json.loads(message)
    except json.JSONDecodeError as error:
        raise WritingSkillUnavailable(
            "writing_child_review_result_invalid"
        ) from error
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_ref",
            "review_task_hash",
            "context_canary_seen",
            "findings",
        }
        or value.get("schema_ref") != _WRITING_CHILD_REVIEW_RESULT_SCHEMA
        or value.get("review_task_hash") != review_task_hash
        or value.get("context_canary_seen") is not False
        or value.get("findings") != expected_findings
    ):
        raise WritingSkillUnavailable("writing_child_review_result_invalid")
    _review_items(value.get("findings"))


def _validate_request(request: WritingSkillRequest) -> None:
    request.runtime_binding.validate()
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


def _writing_skill_resources() -> dict[str, str]:
    root = files("meta_research") / "skills" / "writing-report"
    return {
        "SKILL.md": (root / "SKILL.md").read_text(encoding="utf-8"),
        "agents/openai.yaml": (root / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        ),
    }


def _writing_skill_instructions() -> str:
    return _writing_skill_resources()["SKILL.md"]


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
            "reviewer_agent_ref",
            "findings",
            "dispositions",
            "final_markdown",
            "citations",
        ],
        "properties": {
            "reviewer_agent_ref": {"type": "string", "minLength": 1},
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
