from __future__ import annotations

from meta_research.context_presentation import stage_context_view

from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast

from meta_research.codex_ledger import CodexSessionLedgerReader
from meta_research.codex_runtime import (
    CODEX_MODEL_REF,
    CODEX_REASONING_EFFORT_BINDING,
)
from meta_research.idea_contract import DISPOSITION_ACTIONS, REVIEW_CATEGORIES
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillUnavailable,
    _DISABLED_CODEX_FEATURES,
    _compile_codex_output_schema,
    _codex_harness_manifest,
    _file_sha256,
    _shared_codex_adapter_source_hash,
)
from meta_research.owners.agent_runtime import PlanRuntimeBinding
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.research_guidance import shared_research_guidance
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
from meta_research.root_capabilities import merge_root_capability_bindings
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
)


PlanSkillContractError = PlanContractError
PlanSkillUnavailable = IdeaSkillUnavailable


class RecoverablePlanSkillCandidateError(PlanSkillContractError):
    """A completed Plan candidate failed its content/result contract."""


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
    owner_rejection_kind: str | None = None
    owner_feedback: tuple[str, ...] = ()
    job_ref: str | None = None
    run_ref: str | None = None
    attempt_ref: str | None = None
    fence_ref: str | None = None


@dataclass(frozen=True)
class PlanSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class PlanSkillResult:
    reviewed_draft: dict[str, object]
    final_plan: dict[str, object]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str | None
    adapter_kind: str


class PlanSkillProvider(Protocol):
    def runtime_binding(self) -> PlanRuntimeBinding: ...

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft: ...

    def review_draft(
        self, request: PlanSkillRequest, draft: PlanSkillDraft
    ) -> PlanSkillResult: ...

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult: ...

    def reconcile_cancelled_job(self, job_ref: str) -> bool: ...


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
    try:
        return _validate_document(
            request,
            result.draft,
            evidence_by_ref=evidence_by_ref,
            evidence_revision=evidence_revision,
        )
    except PlanSkillContractError as error:
        raise RecoverablePlanSkillCandidateError(str(error)) from error


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
    if (
        result.review_mode != "advisory_unobserved"
        or result.reviewer_agent_ref is not None
    ):
        raise RecoverablePlanSkillCandidateError("plan_review_mode_invalid")

    try:
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
    except PlanSkillContractError as error:
        raise RecoverablePlanSkillCandidateError(str(error)) from error

    feedback_revision = request.owner_rejection_kind is not None
    if request.owner_rejection_kind not in {None, "domain", "completion"}:
        raise PlanSkillContractError("owner_feedback_lineage_incomplete")
    if feedback_revision != (request.predecessor_submission_ref is not None) or (
        feedback_revision != (request.owner_rejection_receipt_ref is not None)
    ):
        raise PlanSkillContractError("owner_feedback_lineage_incomplete")
    if feedback_revision:
        if not request.owner_feedback:
            raise PlanSkillContractError("owner_feedback_missing")
        if request.owner_rejection_kind == "domain" and (
            predecessor_material_plan_hash is None
            or predecessor_material_plan_hash == material_plan_hash(result.final_plan)
        ):
            raise RecoverablePlanSkillCandidateError(
                "owner_feedback_revision_not_material"
            )
    elif request.owner_feedback:
        raise PlanSkillContractError("owner_feedback_without_rejection")

    review = review_record(
        result,
        draft_hash=draft_hash,
        final_plan_hash=final_hash,
    )
    try:
        review_hash = validate_plan_review(
            review,
            reviewed_draft_hash=draft_hash,
            final_plan_hash=final_hash,
        )
    except PlanSkillContractError as error:
        raise RecoverablePlanSkillCandidateError(str(error)) from error
    return draft_hash, final_hash, review_hash


def review_record(
    result: PlanSkillResult,
    *,
    draft_hash: str,
    final_plan_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": PLAN_REVIEW_SCHEMA_REF,
        "reviewed_draft_hash": draft_hash,
        "final_plan_hash": final_plan_hash,
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


def _plan_owner_rejection_prompt(request: PlanSkillRequest) -> str:
    if not request.owner_feedback:
        return ""
    revision_requirement = (
        "必须实质改变 PlanDocument"
        if request.owner_rejection_kind == "domain"
        else "必须修正被指出的 structured completion 问题"
    )
    return (
        "\n这是 Owner rejection 后在同一根 Session 中的修订。"
        f"{revision_requirement}并逐条处理正式 feedback。\n"
        f"owner_rejection_kind={request.owner_rejection_kind}\n"
        f"predecessor_submission_ref={request.predecessor_submission_ref}\n"
        f"owner_rejection_receipt_ref={request.owner_rejection_receipt_ref}\n"
        f"owner_feedback={canonical_json(list(request.owner_feedback))}\n"
    )


class CodexPlanSkillAdapter(CodexIdeaSkillAdapter):
    _root_agent_kind = "plan"
    """Production Plan adapter over the shared Codex transport and supervisor."""

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "primary":
            return "plan_primary_result_contract_invalid"
        if operation_name == "review":
            return "plan_review_result_contract_invalid"
        raise PlanSkillUnavailable("codex_operation_spool_invalid")

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        timeout_seconds: float | None = None,
        process_runner: Callable[
            [list[str], str, float | None], subprocess.CompletedProcess[str]
        ]
        | None = None,
        codex_ledger_reader: CodexSessionLedgerReader | None = None,
        codex_home: Path | None = None,
    ) -> None:
        super().__init__(
            workspace,
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
            codex_ledger_reader=codex_ledger_reader,
            codex_home=codex_home,
        )

    def runtime_binding(self) -> PlanRuntimeBinding:
        resources = _plan_skill_resources()
        resident_mcp_facts = self._resident_mcp_runtime_facts()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        shared_adapter_source_hash = _shared_codex_adapter_source_hash()
        resident_mcp_source_hash = _file_sha256(
            Path(__file__).with_name("root_resident_mcp.py").resolve()
        )
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        output_contracts = {
            "plan-envelope-template": _plan_envelope_schema(
                _schema_template_request()
            ),
            "advisory-finalization-template": _review_finalization_schema(
                _schema_template_request()
            ),
        }
        output_contracts = {
            name: _compile_codex_output_schema(schema)
            for name, schema in output_contracts.items()
        }
        return PlanRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _plan_skill_instructions(),
                    "adapter_source_hash": adapter_source_hash,
                    "shared_adapter_source_hash": shared_adapter_source_hash,
                    "resident_mcp_source_hash": resident_mcp_source_hash,
                    "supervisor_source_hash": supervisor_source_hash,
                }
            ),
            model_ref=self._model_ref,
            harness_adapter_ref=harness_ref,
            mcp_bindings=resident_mcp_facts.mcp_bindings,
            capability_bindings=merge_root_capability_bindings(
                (
                    "approval-policy-never",
                    "filesystem-danger-full-access",
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
                "adapter-source:meta_research.idea_skill@sha256:"
                f"{shared_adapter_source_hash}",
                "adapter-source:meta_research.root_resident_mcp@sha256:"
                f"{resident_mcp_source_hash}",
                "adapter-source:meta_research.provider_supervisor@sha256:"
                f"{supervisor_source_hash}",
                "disabled-codex-features:" + ",".join(_DISABLED_CODEX_FEATURES),
                "codex-config:approval_policy=never",
                "codex-config:features.multi_agent=true",
                CODEX_REASONING_EFFORT_BINDING,
                "codex-config:web_search=live",
                "output-route:codex-output-last-message/json-schema/v1",
                "provider-output-limits:"
                f"stream={PROVIDER_STREAM_MAX_BYTES};"
                f"result={PROVIDER_RESULT_MAX_BYTES}",
                self._provider_wall_clock_binding(),
                "runtime-policy:trusted-local-broad/v1",
                "sandbox-policy:danger-full-access",
                "transport-seal-key:sha256:" + transport_key_hash(transport_key),
            )
            + resident_mcp_facts.resource_bindings,
        )

    def generate_draft(self, request: PlanSkillRequest) -> PlanSkillDraft:
        if request.runtime_binding != self.runtime_binding():
            raise PlanSkillUnavailable("plan_runtime_binding_drift")
        lineage = _plan_owner_rejection_prompt(request)
        human_resume = (
            ""
            if request.native_session_ref is None
            else "\n若本 Session 曾显式打开 HumanRequest，先以原 effect_id 调 "
            "human_request.open.reconcile，读取 resolution 后再判断"
            "信息是否足够；旧 receipt 不能释放新的 waiter。\n"
        )
        primary_prompt = (
            f"{_plan_skill_instructions()}\n\n"
            f"{human_resume}"
            "Evidence catalog page metadata reports candidate totals, filtering, and next_offset. "
            "Use research_graph.plan_evidence.page to browse further candidates and "
            "research_memory.plan_evidence.read with the exact commit/version/hash/role to read content. "
            "The initial evidence page is a bounded discovery index, not an exhaustive history. "
            "Read exact sources before citing them. Additional accepted evidence may be selected "
            "using its exact Owner catalog binding; the Owner revalidates selected evidence. "
            "Use research_graph.questions.page, research_graph.question_history.read, human_request.read, "
            "and research_memory.content.read to discover and read "
            "same-Quest human input, earlier ScientificOutcome, or accepted AssetVersion relevant to this Plan. "
            "For each actually adopted source, put a compact binding in additional_evidence_bindings: "
            "{schema_ref: meta-research/evidence-source-ref/v1, evidence_ref: the exact source ref, "
            "source_kind: HumanInput|ScientificOutcome|AssetVersion|LiteratureSnapshot, source_ref: the same exact ref}. "
            "Use its evidence_ref in coverage.evidence_uses with the supported claim and support boundary. "
            "Preserve human input scope and distinguish reported opinion from observation or assessment. "
            "Copy only actually reused later-page EvidenceRef objects verbatim into additional_evidence_bindings "
            "(maximum 32; [] if none). The adapter stores these in source_bindings.selected_evidence_catalog. "
            "Never invent receipt/hash fields or copy a discovery summary as an EvidenceRef. "
            "本回合仅执行 Primary draft phase；必须先返回 frozen draft。Advisory "
            "finalization 只能在 Owner 记录该 draft 后的下一次 resumed review turn 中进行。"
            "你是 Plan 主 Agent。只返回 {\"plan\": ...}，其中 plan 是完整、"
            "自洽且可由 Owner 验证的 PlanDocument 候选。必须从 accepted Question "
            "选择本 Cycle 承担的 AnswerContract obligations，逐已选 obligation 交代实际相关的 Idea 候选，只引用经过 Owner 核验的精确 EvidenceRef；按需读取 ContextPack "
            "中的精确 EvidenceRef，并且只为 gap 形成 ExperimentBrief。不得创建 "
            "FormalPlan identity、Owner receipt、StageCommit、Bundle Run、Target、DAG、"
            "Worker 或 Provider。\n"
            "AcceptedQuestionBinding 和完整 IdeaSet 都是冻结输入；不得用 latest、"
            "猜测或搜索结果替换。answer_contract_hash 由适配器根据最终合同内容"
            "计算，不要返回该字段。"
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
            f"provider_context_view={canonical_json(stage_context_view('plan', request.context_pack, context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash))}"
        )
        primary_output, primary_session, _primary_stdout = (
            self._invoke_root_operation(
            operation_name="primary",
            prompt=primary_prompt,
            schema=_plan_envelope_schema(request),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding.as_dict(),
            )
        )
        if primary_session is None:
            raise PlanSkillUnavailable("codex_primary_session_missing")
        # Collaboration trace is advisory provenance.  Acceptance is based on
        # the sealed PlanDocument and the Skill-defined domain validator.
        plan_value = primary_output.get("plan")
        if not isinstance(plan_value, dict):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="primary",
                native_session_ref=primary_session,
                failure_code="plan_primary_result_contract_invalid",
                detail_code="codex_plan_invalid",
                rejected_candidate=primary_output,
            )
        return PlanSkillDraft(
            draft=_with_derived_answer_contract_hash(
                cast(dict[str, object], plan_value), request
            ),
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
        lineage = _plan_owner_rejection_prompt(request)
        reviewer_prompt = (
            f"{_plan_skill_instructions()}\n\n"
            "本回合在原根 Session 中完成独立审查后的修订。使用原生子智能体审查完整草稿和必要原文，"
            "让它独立核查来源、推理、研究边界及后继选择。根据反馈和你的判断修订；没有发现问题也可改稿。"
            "根负责最终研究判断和交接，子智能体使用已授予的工具与当前 fence。"
            "审查反馈保留在原生执行记录中。只返回final_plan 的完整内容。"
            f"{lineage}\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"idea_set_ref={request.idea_set_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"accepted_question={canonical_json(request.accepted_question_content)}\n"
            f"完整 IdeaSet={canonical_json(request.accepted_idea_set)}\n"
            f"provider_context_view={canonical_json(stage_context_view('plan', request.context_pack, context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash))}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        reviewed, resumed_session, _review_stdout = self._invoke_root_operation(
            operation_name="review",
            prompt=reviewer_prompt,
            schema=_review_finalization_schema(request),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
            run_ref=request.run_ref,
            attempt_ref=request.attempt_ref,
            root_session_ref=request.root_session_ref,
            fence_ref=request.fence_ref,
            runtime_binding=request.runtime_binding.as_dict(),
        )
        if resumed_session != draft.primary_session_ref:
            raise PlanSkillUnavailable("codex_primary_session_changed")
        final_value = reviewed.get("final_plan")
        if (
            not isinstance(final_value, dict)
        ):
            raise self._sealed_result_failure(
                job_ref=request.job_ref,
                operation_name="review",
                native_session_ref=draft.primary_session_ref,
                failure_code="plan_review_result_contract_invalid",
                detail_code="codex_review_invalid",
                rejected_candidate=reviewed,
            )
        # The final document and substantive review contents remain validated
        # through ``validate_plan_skill_result``.
        return PlanSkillResult(
            reviewed_draft=draft.draft,
            final_plan=_with_derived_answer_contract_hash(
                cast(dict[str, object], final_value), request
            ),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: PlanSkillRequest) -> PlanSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref),
            draft,
        )

    def _sealed_result_failure(
        self,
        *,
        job_ref: str | None,
        operation_name: str,
        native_session_ref: str,
        failure_code: str,
        detail_code: str | None = None,
        rejected_candidate: dict[str, object] | None = None,
    ) -> PlanSkillUnavailable:
        checkpoint = None
        if job_ref is not None:
            checkpoint = self.terminal_contract_failure_checkpoint(
                job_ref=job_ref,
                operation_name=operation_name,
                native_session_ref=native_session_ref,
                failure_code=failure_code,
                detail_code=detail_code or failure_code,
            )
        return PlanSkillUnavailable(
            failure_code,
            recovery_checkpoint=checkpoint,
            rejected_candidate=rejected_candidate,
            rejected_native_session_ref=(
                native_session_ref if rejected_candidate is not None else None
            ),
            rejected_detail_code=(
                (detail_code or failure_code)
                if rejected_candidate is not None
                else None
            ),
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
            "research-guidance.md": shared_research_guidance(),
            **{
                name: resource.read_text(encoding="utf-8")
                for name, resource in resources
            },
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
    return {"type": "object", "additionalProperties": False, "properties": {"final_plan": _plan_document_schema(request)}, "required": ["final_plan"]}


def _plan_document_schema(request: PlanSkillRequest) -> dict[str, object]:
    candidate_refs = _candidate_refs(request.accepted_idea_set)
    text = {"type": "string", "minLength": 1}
    # Candidate/evidence membership is request-specific and remains enforced
    # by ``validate_plan_document`` after decode.  Expanding those refs into
    # repeated enums can exceed the provider's global 1,000-enum-value budget
    # for otherwise-valid Owner inputs.
    idea_ref = text
    evidence_ref = text
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
                "minItems": 1,
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
                "minItems": 0,
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
    schema = {
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
                },
                "required": [
                    "source_question_ref",
                    "source_idea_set_ref",
                    "obligations",
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
                "minItems": 0,
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

    # These views repeat information already fixed by the request and decisions.
    for field in ("evidence_reuse_set", "gap_set", "idea_trace", "bundle_disposition", "source_bindings"):
        schema["properties"].pop(field)
        schema["required"].remove(field)
    schema["properties"]["notes"] = {"type": "string"}
    schema["required"].append("notes")
    receipt={"type":"object","additionalProperties":False,
        "properties":{key:{"type":"string","minLength":1} for key in
            ("status","issuer","kind","receipt_ref","subject_ref","payload_hash")},
        "required":["status","issuer","kind","receipt_ref","subject_ref","payload_hash"]}
    fields=("schema_ref","evidence_ref","asset_version_ref","asset_ref","content_hash",
        "manifest_hash","target_commit_root_ref","eligibility_token_ref","integrity_receipt_ref",
        "availability_receipt_ref","currentness_receipt_ref","role_ref")
    props={key:{"type":"string","minLength":1} for key in fields}
    props.update(provenance_closure_refs={"type":"array","items":{"type":"string"}},
        capabilities={"type":"array","items":{"type":"string"}},asset_receipt=receipt,role_receipt=receipt)
    schema["properties"]["additional_evidence_bindings"]={"type":"array","maxItems":32,
        "items":{"anyOf":[
            {"type":"object","additionalProperties":False,"properties":props,"required":list(props)},
            {"type":"object","additionalProperties":False,
             "properties":{"schema_ref":{"type":"string","enum":["meta-research/evidence-source-ref/v1"]},
                "evidence_ref":{"type":"string","minLength":1},
                "source_kind":{"type":"string","enum":["HumanInput","ScientificOutcome","AssetVersion","LiteratureSnapshot"]},
                "source_ref":{"type":"string","minLength":1}},
             "required":["schema_ref","evidence_ref","source_kind","source_ref"]}]}}
    # Optional scientifically; an empty array is the strict transport spelling.
    schema["required"].append("additional_evidence_bindings")
    return schema


def _with_derived_answer_contract_hash(
    plan: dict[str, object], request: PlanSkillRequest | None = None,
) -> dict[str, object]:
    if request is not None:
        plan = dict(plan)
        coverage = plan.get("coverage")
        contract = plan.get("answer_contract")
        obligations = contract.get("obligations") if isinstance(contract, dict) else None
        if isinstance(coverage, list) and all(isinstance(item, dict) for item in coverage):
            uses = [use for item in coverage for use in item.get("evidence_uses", [])] if all(isinstance(item.get("evidence_uses"), list) for item in coverage) else None
            if uses is not None:
                plan.setdefault("evidence_reuse_set", uses)
            gaps = [item.get("obligation_key") for item in coverage if item.get("disposition") == "gap"]
            plan.setdefault("gap_set", gaps)
            plan.setdefault("bundle_disposition", "experiments_required" if gaps else "no_new_experiment_required")
        if isinstance(obligations, list) and all(isinstance(item, dict) and isinstance(item.get("idea_relevance"), list) for item in obligations):
            plan.setdefault("idea_trace", [
                {"idea_ref": ref, "obligation_roles": [
                    {"obligation_key": item.get("obligation_key"), "role": relevance.get("role")}
                    for item in obligations for relevance in item["idea_relevance"]
                    if isinstance(relevance, dict) and relevance.get("idea_ref") == ref
                ]} for ref in _candidate_refs(request.accepted_idea_set)
                if any(isinstance(relevance,dict) and relevance.get("idea_ref")==ref
                    for item in obligations for relevance in item["idea_relevance"])
            ])
        additional = plan.pop("additional_evidence_bindings", [])
        plan.setdefault("source_bindings", {
            "question_ref": request.question_ref, "idea_set_ref": request.idea_set_ref,
            "context_pack_ref": request.context_pack_ref, "context_pack_hash": request.context_pack_hash,
            "evidence_reference_revision": request.context_pack.get("evidence_reference_revision", 0),
        })
        if additional:
            source=dict(plan["source_bindings"])
            existing=source.get("selected_evidence_catalog")
            if existing is not None and existing!=additional:
                raise PlanSkillContractError("plan_selected_evidence_catalog_conflict")
            source["selected_evidence_catalog"]=additional
            plan["source_bindings"]=source
    answer_contract = plan.get("answer_contract")
    if not isinstance(answer_contract, dict):
        return plan
    normalized_contract = dict(answer_contract)
    normalized_contract.pop("answer_contract_hash", None)
    normalized_contract["answer_contract_hash"] = canonical_hash(
        normalized_contract
    )
    normalized_plan = dict(plan)
    normalized_plan["answer_contract"] = normalized_contract
    return normalized_plan


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
