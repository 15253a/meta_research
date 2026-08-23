from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
import subprocess
from typing import Callable, Protocol, cast

from meta_research.bundle_contract import (
    MAX_BUNDLE_TARGETS,
    TARGET_PLAN_REVIEW_SCHEMA_REF,
    TARGET_PLAN_SCHEMA_REF,
    BundleContractError,
    validate_bundle_context_pack,
    validate_target_plan,
    validate_target_plan_review,
)
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    _DISABLED_CODEX_FEATURES,
    _codex_harness_manifest,
    _file_sha256,
    _verify_child_review_trace,
)
from meta_research.owners.agent_runtime import BundleRuntimeBinding
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.provider_supervisor import transport_key_hash
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
)


BundleSkillContractError = BundleContractError
BundleSkillUnavailable = IdeaSkillUnavailable


@dataclass(frozen=True)
class BundleSkillRequest:
    stage_request_ref: str
    cycle_ref: str
    question_ref: str
    formal_plan_ref: str
    context_pack_ref: str
    context_pack_hash: str
    context_pack: dict[str, object]
    plan_document: dict[str, object]
    root_session_ref: str
    runtime_binding: BundleRuntimeBinding
    native_session_ref: str | None = None
    job_ref: str | None = None


@dataclass(frozen=True)
class BundleSkillDraft:
    draft: dict[str, object]
    primary_session_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class BundleSkillResult:
    reviewed_draft: dict[str, object]
    final_target_plan: dict[str, object]
    findings: tuple[dict[str, str], ...]
    dispositions: tuple[dict[str, str], ...]
    primary_session_ref: str
    review_mode: str
    reviewer_agent_ref: str
    adapter_kind: str


@dataclass(frozen=True)
class BundleDispatchRequest:
    stage_request_ref: str
    run_ref: str
    attempt_ref: str
    fence_ref: str
    graph_ref: str
    generation: int
    frontier: tuple[dict[str, object], ...]
    state: dict[str, object]
    native_session_ref: str
    runtime_binding: BundleRuntimeBinding
    job_ref: str | None = None


@dataclass(frozen=True)
class BundleDispatchResult:
    action: str
    selected_target_ref: str | None
    rationale: str
    native_session_ref: str
    adapter_kind: str


class BundleSkillProvider(Protocol):
    def runtime_binding(self) -> BundleRuntimeBinding: ...

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft: ...

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult: ...

    def execute(self, request: BundleSkillRequest) -> BundleSkillResult: ...

    def schedule_target(
        self, request: BundleDispatchRequest
    ) -> BundleDispatchResult: ...


def validate_bundle_dispatch_result(
    request: BundleDispatchRequest, result: BundleDispatchResult
) -> str:
    if (
        not request.stage_request_ref
        or not request.run_ref
        or not request.attempt_ref
        or not request.fence_ref
        or not request.graph_ref
        or isinstance(request.generation, bool)
        or request.generation < 1
        or len(request.frontier) > MAX_BUNDLE_TARGETS
        or any(not isinstance(item, dict) for item in request.frontier)
        or not isinstance(request.state, dict)
        or not request.native_session_ref
        or result.native_session_ref != request.native_session_ref
        or not result.adapter_kind
        or not isinstance(result.rationale, str)
        or not result.rationale.strip()
        or len(result.rationale) > 512
    ):
        raise BundleSkillContractError("bundle_dispatch_invalid")
    matching = [
        item
        for item in request.frontier
        if item.get("target_ref") == result.selected_target_ref
    ]
    if request.frontier and result.action != "dispatch":
        raise BundleSkillContractError("bundle_dispatch_requires_authoritative_blocker")
    if (
        result.action not in {"dispatch", "wait", "replan_required"}
        or (
            result.action == "dispatch"
            and (not result.selected_target_ref or len(matching) != 1)
        )
        or (result.action != "dispatch" and result.selected_target_ref is not None)
    ):
        raise BundleSkillContractError("bundle_dispatch_target_not_in_frontier")
    return canonical_hash(
        {
            "schema_ref": "meta-research/bundle-dispatch-decision/v1",
            "stage_request_ref": request.stage_request_ref,
            "run_ref": request.run_ref,
            "attempt_ref": request.attempt_ref,
            "fence_ref": request.fence_ref,
            "graph_ref": request.graph_ref,
            "generation": request.generation,
            "frontier_hash": canonical_hash(list(request.frontier)),
            "state_hash": canonical_hash(request.state),
            "action": result.action,
            "selected_target_ref": result.selected_target_ref,
            "rationale": result.rationale,
            "native_session_ref": result.native_session_ref,
            "adapter_kind": result.adapter_kind,
        }
    )


def validate_bundle_skill_draft(
    request: BundleSkillRequest, result: BundleSkillDraft
) -> str:
    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=None,
        adapter_kind=result.adapter_kind,
    )
    return validate_target_plan(
        result.draft,
        formal_plan_ref=request.formal_plan_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        plan_document=request.plan_document,
    )


def validate_bundle_skill_result(
    request: BundleSkillRequest, result: BundleSkillResult
) -> tuple[str, str, str]:
    _validate_request(request)
    _validate_identity(
        request,
        primary_session_ref=result.primary_session_ref,
        reviewer_agent_ref=result.reviewer_agent_ref,
        adapter_kind=result.adapter_kind,
    )
    if result.review_mode != "harness_child_agent" or result.reviewer_agent_ref in {
        request.root_session_ref,
        result.primary_session_ref,
    }:
        raise BundleSkillContractError("target_plan_review_not_independent")
    draft_hash = validate_target_plan(
        result.reviewed_draft,
        formal_plan_ref=request.formal_plan_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        plan_document=request.plan_document,
    )
    final_hash = validate_target_plan(
        result.final_target_plan,
        formal_plan_ref=request.formal_plan_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        plan_document=request.plan_document,
    )
    review = review_record(
        result, draft_hash=draft_hash, final_target_plan_hash=final_hash
    )
    review_hash = validate_target_plan_review(
        review,
        reviewed_draft_hash=draft_hash,
        final_target_plan_hash=final_hash,
    )
    return draft_hash, final_hash, review_hash


def review_record(
    result: BundleSkillResult,
    *,
    draft_hash: str,
    final_target_plan_hash: str,
) -> dict[str, object]:
    return {
        "schema_ref": TARGET_PLAN_REVIEW_SCHEMA_REF,
        "review_mode": result.review_mode,
        "reviewer_agent_ref": result.reviewer_agent_ref,
        "reviewed_draft_hash": draft_hash,
        "findings": list(result.findings),
        "dispositions": list(result.dispositions),
        "final_target_plan_hash": final_target_plan_hash,
        "independent": True,
        "advisory_only": True,
    }


def _validate_request(request: BundleSkillRequest) -> None:
    for value in (
        request.stage_request_ref,
        request.cycle_ref,
        request.question_ref,
        request.formal_plan_ref,
        request.context_pack_ref,
        request.root_session_ref,
    ):
        if not isinstance(value, str) or not value:
            raise BundleSkillContractError("bundle_skill_request_invalid")
    if canonical_hash(request.context_pack) != request.context_pack_hash:
        raise BundleSkillContractError("context_pack_hash_mismatch")
    question = request.context_pack.get("accepted_question_binding")
    formal_plan = request.context_pack.get("accepted_formal_plan_binding")
    if not isinstance(question, dict) or not isinstance(formal_plan, dict):
        raise BundleSkillContractError("bundle_context_pack_invalid")
    plan = validate_bundle_context_pack(
        request.context_pack,
        cycle_ref=request.cycle_ref,
        accepted_question_binding=cast(dict[str, object], question),
        accepted_formal_plan_binding=cast(dict[str, object], formal_plan),
    )
    if (
        question.get("question_ref") != request.question_ref
        or formal_plan.get("formal_plan_ref") != request.formal_plan_ref
        or plan != request.plan_document
        or plan.get("bundle_disposition") != "experiments_required"
    ):
        raise BundleSkillContractError("bundle_skill_request_binding_mismatch")


def _validate_identity(
    request: BundleSkillRequest,
    *,
    primary_session_ref: str,
    reviewer_agent_ref: str | None,
    adapter_kind: str,
) -> None:
    if (
        not primary_session_ref
        or not adapter_kind
        or primary_session_ref == request.root_session_ref
        or (
            request.native_session_ref is not None
            and request.native_session_ref != primary_session_ref
        )
        or reviewer_agent_ref is not None
        and not reviewer_agent_ref
    ):
        raise BundleSkillContractError("bundle_skill_session_invalid")


class CodexBundleSkillAdapter(CodexPlanSkillAdapter):
    """Production Bundle adapter using one managed native root Session."""

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

    def runtime_binding(self) -> BundleRuntimeBinding:
        resources = _bundle_skill_resources()
        harness_ref, harness_artifacts = _codex_harness_manifest(self._executable)
        adapter_source_hash = _file_sha256(Path(__file__).resolve())
        supervisor_source_hash = _file_sha256(
            Path(__file__).with_name("provider_supervisor.py").resolve()
        )
        _key_path, transport_key = self._transport_key()
        schemas = {
            "target-plan-envelope": _target_plan_envelope_schema(
                _schema_template_request()
            ),
            "target-plan-review": _review_schema(_schema_template_request()),
            "target-dispatch": _dispatch_schema(("__target__",)),
        }
        return BundleRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash(resources),
            instruction_set_hash=canonical_hash(
                {
                    "skill_instructions": _bundle_skill_instructions(),
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
                f"package:meta_research.skills.bundle_stage/{name}@sha256:"
                f"{canonical_hash(content)}"
                for name, content in resources.items()
            )
            + tuple(
                f"output-schema:{name}@sha256:{canonical_hash(schema)}"
                for name, schema in schemas.items()
            )
            + harness_artifacts
            + (
                "adapter-source:meta_research.bundle_skill@sha256:"
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

    def generate_draft(self, request: BundleSkillRequest) -> BundleSkillDraft:
        if request.runtime_binding != self.runtime_binding():
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            '你是 Bundle 根 Agent。只返回 {"target_plan": ...}。从冻结的 '
            "FormalPlan GapSet 与 ExperimentBrief 形成去重、可执行且无环的 Target "
            "DAG。Target identity、DAG、frontier、TargetCommit 都由 Research Graph "
            "接纳；你只能提出 TargetSpec，不能伪造 Owner receipt。Agent Session "
            "绝不是 Target 或 TargetRun。普通低风险 Target 自动调度，高风险只标记 "
            "risk_class=high，由 Owner/Human Collaboration 决定授权。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"cycle_ref={request.cycle_ref}\n"
            f"question_ref={request.question_ref}\n"
            f"formal_plan_ref={request.formal_plan_ref}\n"
            f"context_pack_ref={request.context_pack_ref}\n"
            f"context_pack_hash={request.context_pack_hash}\n"
            f"plan_document={canonical_json(request.plan_document)}"
        )
        output, session_ref, _stdout = self._invoke(
            operation_name="primary",
            prompt=prompt,
            schema=_target_plan_envelope_schema(request),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
        target_plan = output.get("target_plan")
        if session_ref is None or not isinstance(target_plan, dict):
            raise BundleSkillUnavailable("codex_bundle_target_plan_invalid")
        return BundleSkillDraft(
            draft=cast(dict[str, object], target_plan),
            primary_session_ref=session_ref,
            adapter_kind="codex_cli",
        )

    def review_draft(
        self, request: BundleSkillRequest, draft: BundleSkillDraft
    ) -> BundleSkillResult:
        if (
            request.runtime_binding != self.runtime_binding()
            or request.native_session_ref != draft.primary_session_ref
        ):
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "在当前 Bundle 根 Session 内使用 Harness 原生 spawn_agent，以 "
            'fork_turns="none" 启动一个短命、全新上下文 child reviewer，并 wait '
            "到完成。它只审查 FormalPlan lineage、"
            "GapSet 闭合、去重、DAG、可执行性与 Owner 边界，不批准 Target。根 Agent "
            "逐条处置 finding 后返回 reviewer_agent_ref、findings、final_target_plan、"
            "dispositions。不得创建第二个顶层 Session。\n"
            f"formal_plan_ref={request.formal_plan_ref}\n"
            f"plan_document={canonical_json(request.plan_document)}\n"
            f"reviewed_draft={canonical_json(draft.draft)}"
        )
        output, session_ref, stdout = self._invoke(
            operation_name="review",
            prompt=prompt,
            schema=_review_schema(request),
            native_session_ref=draft.primary_session_ref,
            job_ref=request.job_ref,
        )
        reviewer = output.get("reviewer_agent_ref")
        findings = output.get("findings")
        final = output.get("final_target_plan")
        dispositions = output.get("dispositions")
        if (
            session_ref != draft.primary_session_ref
            or not isinstance(reviewer, str)
            or not isinstance(findings, list)
            or not isinstance(final, dict)
            or not isinstance(dispositions, list)
        ):
            raise BundleSkillUnavailable("codex_bundle_review_invalid")
        _verify_child_review_trace(
            stdout,
            root_session_ref=draft.primary_session_ref,
            reviewer_agent_ref=reviewer,
        )
        return BundleSkillResult(
            reviewed_draft=draft.draft,
            final_target_plan=cast(dict[str, object], final),
            findings=tuple(cast(dict[str, str], item) for item in findings),
            dispositions=tuple(cast(dict[str, str], item) for item in dispositions),
            primary_session_ref=draft.primary_session_ref,
            review_mode="harness_child_agent",
            reviewer_agent_ref=reviewer,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: BundleSkillRequest) -> BundleSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(
            replace(request, native_session_ref=draft.primary_session_ref), draft
        )

    def schedule_target(self, request: BundleDispatchRequest) -> BundleDispatchResult:
        if request.runtime_binding != self.runtime_binding():
            raise BundleSkillUnavailable("bundle_runtime_binding_drift")
        target_refs = tuple(cast(str, item["target_ref"]) for item in request.frontier)
        prompt = (
            f"{_bundle_skill_instructions()}\n\n"
            "继续使用当前 Bundle 根 Session。先读取这次 durable frontier 与 "
            "TargetCommit/blocker 摘要，再自主选择下一项可调度 Target。选择体现当前 "
            "优先级、依赖、已实现结果与局部阻塞；不得把 Agent tree 当成 Target DAG，"
            "不得选择 frontier 之外的 Target，也不得伪造 Owner receipt。有可执行 "
            "Target 时返回 dispatch；只有技术/授权等待返回 wait；只有冻结语义确需 "
            "改变才返回 replan_required。\n"
            f"stage_request_ref={request.stage_request_ref}\n"
            f"run_ref={request.run_ref}\n"
            f"attempt_ref={request.attempt_ref}\n"
            f"fence_ref={request.fence_ref}\n"
            f"graph_ref={request.graph_ref}\n"
            f"generation={request.generation}\n"
            f"frontier={canonical_json(list(request.frontier))}\n"
            f"state={canonical_json(request.state)}"
        )
        output, session_ref, _stdout = self._invoke(
            operation_name=f"dispatch-{request.generation}",
            prompt=prompt,
            schema=_dispatch_schema(target_refs),
            native_session_ref=request.native_session_ref,
            job_ref=request.job_ref,
        )
        action = output.get("action")
        selected_target_ref = output.get("selected_target_ref")
        rationale = output.get("rationale")
        if (
            session_ref is None
            or not isinstance(action, str)
            or (
                selected_target_ref is not None
                and not isinstance(selected_target_ref, str)
            )
            or not isinstance(rationale, str)
        ):
            raise BundleSkillUnavailable("codex_bundle_dispatch_invalid")
        result = BundleDispatchResult(
            action=action,
            selected_target_ref=selected_target_ref,
            rationale=rationale,
            native_session_ref=session_ref,
            adapter_kind="codex_cli",
        )
        validate_bundle_dispatch_result(request, result)
        return result


def _bundle_skill_resources() -> dict[str, str]:
    package = files("meta_research.skills.bundle_stage")
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
            name: resource.read_text(encoding="utf-8") for name, resource in resources
        }
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise BundleSkillUnavailable("bundle_skill_resource_unavailable") from error


def _bundle_skill_instructions() -> str:
    return "\n\n".join(
        f"<!-- bundled resource: {name} -->\n{content}"
        for name, content in _bundle_skill_resources().items()
    )


def _schema_template_request() -> BundleSkillRequest:
    plan = {
        "gap_set": ["__gap__"],
        "experiment_briefs": [
            {
                "experiment_key": "__experiment__",
                "gap_obligation_keys": ["__gap__"],
                "goal": "__goal__",
                "boundary_constraints": "__boundary__",
                "semantic_delta": "__delta__",
                "contributing_idea_refs": ["__idea__"],
            }
        ],
        "bundle_disposition": "experiments_required",
    }
    context = {
        "accepted_question_binding": {"question_ref": "__question__"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "__formal_plan__",
            "plan_document": plan,
        },
    }
    return BundleSkillRequest(
        stage_request_ref="__request__",
        cycle_ref="__cycle__",
        question_ref="__question__",
        formal_plan_ref="__formal_plan__",
        context_pack_ref="__context__",
        context_pack_hash="0" * 64,
        context_pack=context,
        plan_document=plan,
        root_session_ref="__root_session__",
        runtime_binding=BundleRuntimeBinding(
            packaged_skill_bundle_hash="0" * 64,
            instruction_set_hash="0" * 64,
            model_ref="__model__",
            harness_adapter_ref="__harness__",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        ),
    )


def _target_plan_envelope_schema(request: BundleSkillRequest) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"target_plan": _target_plan_schema(request)},
        "required": ["target_plan"],
    }


def _review_schema(request: BundleSkillRequest) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reviewer_agent_ref": text,
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": text,
                        "category": {
                            "type": "string",
                            "enum": [
                                "lineage",
                                "dag",
                                "dedup",
                                "feasibility",
                                "owner_boundary",
                            ],
                        },
                        "message": text,
                    },
                    "required": ["finding_id", "category", "message"],
                },
            },
            "final_target_plan": _target_plan_schema(request),
            "dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "finding_id": text,
                        "action": {
                            "type": "string",
                            "enum": ["revised", "not_adopted"],
                        },
                        "rationale": text,
                    },
                    "required": ["finding_id", "action", "rationale"],
                },
            },
        },
        "required": [
            "reviewer_agent_ref",
            "findings",
            "final_target_plan",
            "dispositions",
        ],
    }


def _dispatch_schema(target_refs: tuple[str, ...]) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "wait", "replan_required"],
            },
            "selected_target_ref": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": list(target_refs) or ["__no_dispatch_target__"],
                    },
                    {"type": "null"},
                ],
            },
            "rationale": {**text, "maxLength": 512},
        },
        "required": ["action", "selected_target_ref", "rationale"],
    }


def _target_plan_schema(request: BundleSkillRequest) -> dict[str, object]:
    text = {"type": "string", "minLength": 1}
    briefs = request.plan_document.get("experiment_briefs", [])
    experiment_keys = [
        value["experiment_key"]
        for value in briefs
        if isinstance(value, dict) and isinstance(value.get("experiment_key"), str)
    ]
    gap_keys = [
        value
        for value in request.plan_document.get("gap_set", [])
        if isinstance(value, str)
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_ref": {"const": TARGET_PLAN_SCHEMA_REF},
            "kind": {"const": "TargetPlan"},
            "formal_plan_ref": {"const": request.formal_plan_ref},
            "context_pack_ref": {"const": request.context_pack_ref},
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BUNDLE_TARGETS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "target_key": text,
                        "title": text,
                        "target_type": {"const": "micro_experiment"},
                        "experiment_key": {
                            "type": "string",
                            "enum": experiment_keys or ["__missing_experiment__"],
                        },
                        "gap_obligation_keys": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": gap_keys or ["__missing_gap__"],
                            },
                        },
                        "depends_on": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": text,
                        },
                        "goal": text,
                        "hypothesis": text,
                        "variant_parameter": {"type": "number"},
                        "sample_count": {
                            "type": "integer",
                            "minimum": 4,
                            "maximum": 4096,
                        },
                        "boundary_constraints": text,
                        "semantic_delta": text,
                        "contributing_idea_refs": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": text,
                        },
                        "risk_class": {"type": "string", "enum": ["normal", "high"]},
                    },
                    "required": [
                        "target_key",
                        "title",
                        "target_type",
                        "experiment_key",
                        "gap_obligation_keys",
                        "depends_on",
                        "goal",
                        "hypothesis",
                        "variant_parameter",
                        "sample_count",
                        "boundary_constraints",
                        "semantic_delta",
                        "contributing_idea_refs",
                        "risk_class",
                    ],
                },
            },
            "source_bindings": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "formal_plan_ref": {"const": request.formal_plan_ref},
                    "plan_document_hash": {
                        "const": canonical_hash(request.plan_document)
                    },
                    "context_pack_ref": {"const": request.context_pack_ref},
                    "context_pack_hash": {"const": request.context_pack_hash},
                },
                "required": [
                    "formal_plan_ref",
                    "plan_document_hash",
                    "context_pack_ref",
                    "context_pack_hash",
                ],
            },
        },
        "required": [
            "schema_ref",
            "kind",
            "formal_plan_ref",
            "context_pack_ref",
            "targets",
            "source_bindings",
        ],
    }
