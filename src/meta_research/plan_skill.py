from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast

from meta_research.idea_contract import DISPOSITION_ACTIONS, REVIEW_CATEGORIES
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillUnavailable,
    _DISABLED_CODEX_FEATURES,
    _codex_harness_manifest,
    _file_sha256,
    _verify_child_review_trace,
)
from meta_research.owners.agent_runtime import PlanRuntimeBinding
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.plan_contract import (
    MAX_PLAN_EXPERIMENT_BRIEFS,
    MAX_PLAN_OBLIGATIONS,
    PLAN_DOCUMENT_SCHEMA_REF,
    PLAN_REVIEW_SCHEMA_REF,
    PlanContractError,
    material_plan_hash,
    validate_plan_context_pack,
    validate_plan_document,
    validate_plan_review,
)
from meta_research.provider_supervisor import transport_key_hash
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
)


PlanSkillContractError = PlanContractError
PlanSkillUnavailable = IdeaSkillUnavailable


@dataclass(frozen=True)
class PlanSkillRequest:
    stage_request_ref: str
    cycle_ref: str
    question_ref: str
    idea_set_ref: str
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    accepted_question_content: dict[str, object]
    accepted_idea_set: dict[str, object]
    root_session_ref: str
    submission_revision: int
    runtime_binding: PlanRuntimeBinding
    native_session_ref: str | None = None
    predecessor_submission_ref: str | None = None
    owner_rejection_receipt_ref: str | None = None
    owner_feedback: tuple[str, ...] = ()
    job_ref: str | None = None


@dataclass(frozen=True)
class PlanSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class PlanSkillResult:
    reviewed_draft: dict[str, object]
    final_plan: dict[str, object]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str


class PlanSkillProvider(Protocol):
    def runtime_binding(self) -> PlanRuntimeBinding: ...

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft: ...

    def review_draft(
        self, request: PlanSkillRequest, draft: PlanSkillDraft
    ) -> PlanSkillResult: ...

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult: ...


def validate_plan_skill_draft(
    request: PlanSkillRequest, result: PlanSkillDraft
) -> str:
    """Validate the primary Plan checkpoint through the public Skill seam."""

    evidence_by_ref, evidence_revision = _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    return _validate_document(
        request,
        result.draft,
        evidence_by_ref=evidence_by_ref,
        evidence_revision=evidence_revision,
    )


def validate_plan_skill_result(
    request: PlanSkillRequest,
    result: PlanSkillResult,
    *,
    predecessor_material_plan_hash: str | None = None,
) -> tuple[str, str, str]:
    """Validate a complete primary/reviewer result and return its three hashes."""

    evidence_by_ref, evidence_revision = _validate_request(request)
    _validate_result_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent":
        raise PlanSkillContractError("plan_review_mode_invalid")
    if result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise PlanSkillContractError("plan_review_not_independent")

    draft_hash = _validate_document(
        request,
        result.reviewed_draft,
        evidence_by_ref=evidence_by_ref,
        evidence_revision=evidence_revision,
    )
    final_hash = _validate_document(
        request,
        result.final_plan,
        evidence_by_ref=evidence_by_ref,
        evidence_revision=evidence_revision,
    )

    feedback_revision = request.predecessor_submission_ref is not None
    if feedback_revision != (request.owner_rejection_receipt_ref is not None):
        raise PlanSkillContractError("owner_feedback_lineage_incomplete")
    if feedback_revision:
        if not request.owner_feedback:
            raise PlanSkillContractError("owner_feedback_missing")
        if predecessor_material_plan_hash is None or (
            predecessor_material_plan_hash == material_plan_hash(result.final_plan)
        ):
            raise PlanSkillContractError("owner_feedback_revision_not_material")
    elif request.owner_feedback:
        raise PlanSkillContractError("owner_feedback_without_rejection")

    review = review_record(
        result,
        draft_hash=draft_hash,
        final_plan_hash=final_hash,
    )
    review_hash = validate_plan_review(
        review,
        reviewed_draft_hash=draft_hash,
        final_plan_hash=final_hash,
    )
    return draft_hash, final_hash, review_hash


def review_record(
    result: PlanSkillResult,
    *,
    draft_hash: str,
    final_plan_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": PLAN_REVIEW_SCHEMA_REF,
        "review_mode": result.review_mode,
        "reviewer_agent_ref": result.reviewer_agent_ref,
        "reviewed_draft_hash": draft_hash,
        "findings": list(result.findings),
        "dispositions": list(result.dispositions),
        "final_plan_hash": final_plan_hash,
        "independent": True,
        "advisory_only": True,
    }


def _validate_request(
    request: PlanSkillRequest,
) -> tuple[dict[str, dict[str, object]], int]:
    if request.submission_revision < 1:
        raise PlanSkillContractError("submission_revision_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise PlanSkillContractError("context_pack_hash_mismatch")
    for label, value in (
        ("stage_request_ref", request.stage_request_ref),
        ("cycle_ref", request.cycle_ref),
        ("question_ref", request.question_ref),
        ("idea_set_ref", request.idea_set_ref),
        ("context_pack_ref", request.context_pack_ref),
        ("root_session_ref", request.root_session_ref),
    ):
        _require_text(value, label)

    question_binding = request.context_pack.get("accepted_question_binding")
    idea_binding = request.context_pack.get("accepted_idea_set_binding")
    if not isinstance(question_binding, dict) or not isinstance(idea_binding, dict):
        raise PlanSkillContractError("plan_context_pack_invalid")
    if (
        question_binding.get("question_ref") != request.question_ref
        or idea_binding.get("outcome_ref") != request.idea_set_ref
        or idea_binding.get("idea_set") != request.accepted_idea_set
    ):
        raise PlanSkillContractError("plan_request_binding_mismatch")
    evidence_by_ref = validate_plan_context_pack(
        request.context_pack,
        cycle_ref=request.cycle_ref,
        accepted_question_binding=cast(dict[str, object], question_binding),
    )
    revision = request.context_pack.get("evidence_reference_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise PlanSkillContractError("plan_evidence_catalog_invalid")
    return evidence_by_ref, revision


def _validate_result_identity(
    request: PlanSkillRequest,
    *,
    primary_session_ref: str,
    reviewer_agent_ref: str | None,
    adapter_kind: str,
) -> None:
    for label, value in (
        ("primary_session_ref", primary_session_ref),
        ("adapter_kind", adapter_kind),
    ):
        _require_text(value, label)
    if primary_session_ref == request.root_session_ref:
        raise PlanSkillContractError("native_session_not_provider_owned")
    if request.native_session_ref is not None and (
        primary_session_ref != request.native_session_ref
    ):
        raise PlanSkillContractError("root_native_session_changed")
    if reviewer_agent_ref is not None:
        _require_text(reviewer_agent_ref, "reviewer_agent_ref")


def _validate_document(
    request: PlanSkillRequest,
    document: dict[str, object],
    *,
    evidence_by_ref: dict[str, dict[str, object]],
    evidence_revision: int,
) -> str:
    return validate_plan_document(
        document,
        question_ref=request.question_ref,
        idea_set_ref=request.idea_set_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        accepted_idea_set=request.accepted_idea_set,
        evidence_by_ref=evidence_by_ref,
        evidence_reference_revision=evidence_revision,
    )


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PlanSkillContractError(f"{label}_invalid")


class CodexPlanSkillAdapter(CodexIdeaSkillAdapter):
    """Production Plan adapter over the shared Codex transport and supervisor."""

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
        super().__init__(
            workspace,
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
        )

    def runtime_binding(self) -> PlanRuntimeBinding:
        resources = _plan_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        output_contracts = {
            "plan-envelope-template": _plan_envelope_schema(
                _schema_template_request()
            ),
            "child-review-finalization-template": _review_finalization_schema(
                _schema_template_request()
            ),
        }
        return PlanRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _plan_skill_instructions(),
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
                f"package:meta_research.skills.plan_stage/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in output_contracts.items()
            )
            + harness_artifacts
            + (
                "adapter-source:meta_research.plan_skill@sha256:"
                f"{adapter_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"stream={PROVIDER_STREAM_MAX_BYTES};"
                f"result={PROVIDER_RESULT_MAX_BYTES}",
                "provider-timeout-seconds:" + format(self._timeout_seconds, ".17g"),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:" + transport_key_hash(transport_key),
            ),
        )

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft:
        if request.runtime_binding != self.runtime_binding():
            raise PlanSkillUnavailable("plan_runtime_binding_drift")
        lineage = ""
        if request.owner_feedback:
            lineage = (
                "\n这是 RG rejection 后在同一根 Session 中的修订。必须实质改变 "
                "PlanDocument 并逐条处理正式 feedback。\n"
                f"predecessor_submission_ref={request.predecessor_submission_ref}\n"
                "owner_rejection_receipt_ref="
                f"{request.owner_rejection_receipt_ref}\n"
                f"owner_feedback={canonical_json(list(request.owner_feedback))}\n"
            )
        primary_prompt = (
            f"{_plan_skill_instructions()}\n\n"
            "你是 Plan 主 Agent。只返回 {\"plan\": ...}，其中 plan 是完整、"
            "自洽且可由 Owner 验证的 PlanDocument 候选。必须从 accepted Question "
            "推导 AnswerContract，逐 obligation 交代完整 IdeaSet，只引用 ContextPack "
            "中的精确 EvidenceRef，并且只为 gap 形成 ExperimentBrief。不得创建 "
            "FormalPlan identity、Owner receipt、StageCommit、Bundle Run、Target、DAG、"
            "Worker 或 Provider。\n"
            "AcceptedQuestionBinding 和完整 IdeaSet 都是冻结输入；不得用 latest、"
            "猜测或搜索结果替换。"
            f"{lineage}\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"cycle_ref={request.cycle_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"idea_set_ref={request.idea_set_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"runtime_binding={canonical_json(request.runtime_binding.as_dict())}\n"
            "AcceptedQuestionBinding="
            f"{canonical_json(request.context_pack['accepted_question_binding'])}\n"
            f"accepted_question={canonical_json(request.accepted_question_content)}\n"
            f"完整 IdeaSet={canonical_json(request.accepted_idea_set)}\n"
            f"context_pack={canonical_json(request.context_pack)}"
        )
        primary_output, primary_session, _stdout = self._invoke(
            operation_name="primary",
            prompt=primary_prompt,
            schema=_plan_envelope_schema(request),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
        if primary_session is None:
            raise PlanSkillUnavailable("codex_primary_session_missing")
        plan_value = primary_output.get("plan")
        if not isinstance(plan_value, dict):
            raise PlanSkillUnavailable("codex_plan_invalid")
        return PlanSkillDraft(
            draft=cast(dict[str, object], plan_value),
            primary_session_ref=primary_session,
            adapter_kind="codex_cli",
        )

    def review_draft(
        self, request: PlanSkillRequest, draft: PlanSkillDraft
    ) -> PlanSkillResult:
        if request.runtime_binding != self.runtime_binding():
            raise PlanSkillUnavailable("plan_runtime_binding_drift")
        if request.native_session_ref != draft.primary_session_ref:
            raise PlanSkillUnavailable("codex_primary_session_changed")
        reviewer_prompt = (
            f"{_plan_skill_instructions()}\n\n"
            "你仍是根 Plan Agent。必须在当前 managed native Session 内使用 Harness "
            "原生 spawn_agent，以 fork_turns=\"none\" 启动一个全新上下文的短命 "
            "child reviewer，并 wait 到它完成；不得创建第二个顶层 Session。只把冻结"
            "的 Question/IdeaSet/Evidence 闭包与完整草稿交给 child。Reviewer 只检查 "
            "Question 对齐、每个 obligation × IdeaCandidate 是否完整、EvidenceRef "
            "support boundary、coverage、gap 到 ExperimentBrief 闭合和 Owner 权限边界；"
            "它不批准 Plan。根 Agent 必须在同一个 resumed turn 对每条 finding 给出 "
            "revised | not_adopted disposition，并返回最终完整 PlanDocument。revised "
            "必须实际改变 Plan；没有 finding 时 findings/dispositions 都为空。只返回 "
            "reviewer_agent_ref、findings、final_plan、dispositions。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"idea_set_ref={request.idea_set_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"accepted_question={canonical_json(request.accepted_question_content)}\n"
            f"完整 IdeaSet={canonical_json(request.accepted_idea_set)}\n"
            f"context_pack={canonical_json(request.context_pack)}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        reviewed, resumed_session, review_stdout = self._invoke(
            operation_name="review",
            prompt=reviewer_prompt,
            schema=_review_finalization_schema(request),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
        )
        if resumed_session != draft.primary_session_ref:
            raise PlanSkillUnavailable("codex_primary_session_changed")
        reviewer_agent_ref = reviewed.get("reviewer_agent_ref")
        findings_value = reviewed.get("findings")
        final_value = reviewed.get("final_plan")
        dispositions_value = reviewed.get("dispositions")
        if (
            not isinstance(reviewer_agent_ref, str)
            or not reviewer_agent_ref
            or not isinstance(findings_value, list)
            or not isinstance(final_value, dict)
            or not isinstance(dispositions_value, list)
        ):
            raise PlanSkillUnavailable("codex_review_invalid")
        _verify_child_review_trace(
            review_stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer_agent_ref,
        )
        return PlanSkillResult(
            reviewed_draft=draft.draft,
            final_plan=cast(dict[str, object], final_value),
            findings=tuple(cast(dict[str, str], item) for item in findings_value),
            dispositions=tuple(
                cast(dict[str, str], item) for item in dispositions_value
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer_agent_ref,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref),
            draft,
        )


def _plan_skill_resources() -> dict[str, str]:
    package = files("meta_research.skills.plan_stage")
    resources = (
        ("SKILL.md", package / "SKILL.md"),
        ("references/contract.md", package / "references" / "contract.md"),
        (
            "references/owner-operations.md",
            package / "references" / "owner-operations.md",
        ),
    )
    try:
        return {
            name: resource.read_text(encoding="utf-8")
            for name, resource in resources
        }
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise PlanSkillUnavailable("plan_skill_resource_unavailable") from error


def _plan_skill_instructions() -> str:
    return "\n\n".join(
        f"<!-- bundled resource: {name} -->\n{content}"
        for name, content in _plan_skill_resources().items()
    )


def _schema_template_request() -> PlanSkillRequest:
    context_pack = {
        "accepted_question_binding": {"question_ref": "__question_ref__"},
        "accepted_idea_set_binding": {"outcome_ref": "__idea_set_ref__"},
        "evidence_catalog": [],
        "evidence_reference_revision": 0,
    }
    return PlanSkillRequest(
        stage_request_ref="__stage_request_ref__",
        cycle_ref="__cycle_ref__",
        question_ref="__question_ref__",
        idea_set_ref="__idea_set_ref__",
        context_pack_ref="__context_pack_ref__",
        context_pack_hash="__context_pack_hash__",
        context_pack=context_pack,
        accepted_question_content={},
        accepted_idea_set={"candidates": [{"candidate_key": "__idea_ref__"}]},
        root_session_ref="__root_session_ref__",
        submission_revision=1,
        runtime_binding=PlanRuntimeBinding(
            packaged_skill_bundle_hash="0" * 64,
            instruction_set_hash="0" * 64,
            model_ref="__model_ref__",
            harness_adapter_ref="__harness_adapter_ref__",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        ),
    )


def _plan_envelope_schema(request: PlanSkillRequest) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"plan": _plan_document_schema(request)},
        "required": ["plan"],
    }


def _review_finalization_schema(request: PlanSkillRequest) -> dict[str, object]:
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
            "final_plan": _plan_document_schema(request),
            "dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": {"type": "string", "minLength": 1},
                        "action": {
                            "type": "string",
                            "enum": sorted(DISPOSITION_ACTIONS),
                        },
                        "rationale": {"type": "string", "minLength": 1},
                    },
                    "required": ["finding_id", "action", "rationale"],
                },
            },
        },
        "required": [
            "reviewer_agent_ref",
            "findings",
            "final_plan",
            "dispositions",
        ],
    }


def _plan_document_schema(request: PlanSkillRequest) -> dict[str, object]:
    candidate_refs = _candidate_refs(request.accepted_idea_set)
    evidence_refs = _evidence_refs(request.context_pack)
    text = {"type": "string", "minLength": 1}
    sha256 = {"type": "string", "minLength": 64, "maxLength": 64}
    idea_ref = {
        "type": "string",
        "enum": candidate_refs or ["__missing_idea_ref__"],
    }
    evidence_ref = {
        "type": "string",
        "enum": evidence_refs or ["__no_evidence_ref_available__"],
    }
    relevance = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "idea_ref": idea_ref,
            "role": {
                "type": "string",
                "enum": ["query_lens", "experiment_lens", "not_relevant"],
            },
            "rationale": text,
        },
        "required": ["idea_ref", "role", "rationale"],
    }
    evidence_use = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "obligation_key": text,
            "evidence_ref": evidence_ref,
            "supported_claim": text,
            "support_boundary": text,
            "contributing_idea_refs": {
                "type": "array",
                "items": idea_ref,
                "uniqueItems": True,
            },
        },
        "required": [
            "obligation_key",
            "evidence_ref",
            "supported_claim",
            "support_boundary",
            "contributing_idea_refs",
        ],
    }
    obligation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "obligation_key": text,
            "statement": text,
            "minimum_support": text,
            "question_trace": {
                "type": "array",
                "minItems": 2,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": [
                        "unknown_statement",
                        "answer_shape",
                        "applicability_scope",
                    ],
                },
            },
            "idea_relevance": {
                "type": "array",
                "minItems": len(candidate_refs),
                "maxItems": len(candidate_refs),
                "items": relevance,
            },
        },
        "required": [
            "obligation_key",
            "statement",
            "minimum_support",
            "question_trace",
            "idea_relevance",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": PLAN_DOCUMENT_SCHEMA_REF},
            "kind": {"const": "PlanDocument"},
            "question_ref": {"const": request.question_ref},
            "idea_set_ref": {"const": request.idea_set_ref},
            "context_pack_ref": {"const": request.context_pack_ref},
            "answer_contract": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_question_ref": {"const": request.question_ref},
                    "source_idea_set_ref": {"const": request.idea_set_ref},
                    "obligations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_PLAN_OBLIGATIONS,
                        "items": obligation,
                    },
                    "answer_contract_hash": sha256,
                },
                "required": [
                    "source_question_ref",
                    "source_idea_set_ref",
                    "obligations",
                    "answer_contract_hash",
                ],
            },
            "evidence_reuse_set": {
                "type": "array",
                "items": evidence_use,
            },
            "coverage": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_OBLIGATIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "obligation_key": text,
                        "disposition": {
                            "type": "string",
                            "enum": ["covered", "gap"],
                        },
                        "evidence_uses": {
                            "type": "array",
                            "items": evidence_use,
                        },
                        "insufficiency": {
                            "anyOf": [{"type": "null"}, text],
                        },
                    },
                    "required": [
                        "obligation_key",
                        "disposition",
                        "evidence_uses",
                        "insufficiency",
                    ],
                },
            },
            "gap_set": {
                "type": "array",
                "items": text,
                "uniqueItems": True,
            },
            "experiment_briefs": {
                "type": "array",
                "maxItems": MAX_PLAN_EXPERIMENT_BRIEFS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "experiment_key": text,
                        "gap_obligation_keys": {
                            "type": "array",
                            "minItems": 1,
                            "items": text,
                            "uniqueItems": True,
                        },
                        "goal": text,
                        "characteristics": text,
                        "boundary_constraints": text,
                        "semantic_delta": text,
                        "contributing_idea_refs": {
                            "type": "array",
                            "items": idea_ref,
                            "uniqueItems": True,
                        },
                    },
                    "required": [
                        "experiment_key",
                        "gap_obligation_keys",
                        "goal",
                        "characteristics",
                        "boundary_constraints",
                        "semantic_delta",
                        "contributing_idea_refs",
                    ],
                },
            },
            "idea_trace": {
                "type": "array",
                "minItems": len(candidate_refs),
                "maxItems": len(candidate_refs),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "idea_ref": idea_ref,
                        "obligation_roles": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "obligation_key": text,
                                    "role": {
                                        "type": "string",
                                        "enum": [
                                            "query_lens",
                                            "experiment_lens",
                                            "not_relevant",
                                        ],
                                    },
                                },
                                "required": ["obligation_key", "role"],
                            },
                        },
                    },
                    "required": ["idea_ref", "obligation_roles"],
                },
            },
            "bundle_disposition": {
                "type": "string",
                "enum": [
                    "experiments_required",
                    "no_new_experiment_required",
                ],
            },
            "source_bindings": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question_ref": {"const": request.question_ref},
                    "idea_set_ref": {"const": request.idea_set_ref},
                    "context_pack_ref": {"const": request.context_pack_ref},
                    "context_pack_hash": {"const": request.context_pack_hash},
                    "evidence_reference_revision": {
                        "const": request.context_pack.get(
                            "evidence_reference_revision", 0
                        )
                    },
                },
                "required": [
                    "question_ref",
                    "idea_set_ref",
                    "context_pack_ref",
                    "context_pack_hash",
                    "evidence_reference_revision",
                ],
            },
        },
        "required": [
            "schema_ref",
            "kind",
            "question_ref",
            "idea_set_ref",
            "context_pack_ref",
            "answer_contract",
            "evidence_reuse_set",
            "coverage",
            "gap_set",
            "experiment_briefs",
            "idea_trace",
            "bundle_disposition",
            "source_bindings",
        ],
    }


def _candidate_refs(idea_set: dict[str, object]) -> list[str]:
    candidates = idea_set.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        cast(str, candidate["candidate_key"])
        for candidate in candidates
        if isinstance(candidate, dict)
        and isinstance(candidate.get("candidate_key"), str)
        and candidate["candidate_key"]
    ]


def _evidence_refs(context_pack: dict[str, object]) -> list[str]:
    catalog = context_pack.get("evidence_catalog")
    if not isinstance(catalog, list):
        return []
    return [
        cast(str, evidence["evidence_ref"])
        for evidence in catalog
        if isinstance(evidence, dict)
        and isinstance(evidence.get("evidence_ref"), str)
        and evidence["evidence_ref"]
    ]
