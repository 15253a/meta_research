from __future__ import annotations

from dataclasses import replace
from importlib.resources import files
import json
from pathlib import Path
import subprocess

import pytest

from meta_research.owners.agent_runtime import PlanRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.plan_contract import material_plan_hash
from meta_research.plan_skill import (
    CodexPlanSkillAdapter,
    PlanSkillContractError,
    PlanSkillRequest,
    PlanSkillResult,
    validate_plan_skill_result,
)


def _receipt(
    issuer: str, kind: str, receipt_ref: str, subject_ref: str, fill: str
) -> dict[str, object]:
    return {
        "status": "accepted",
        "issuer": issuer,
        "kind": kind,
        "receipt_ref": receipt_ref,
        "subject_ref": subject_ref,
        "payload_hash": fill * 64,
    }


_IDEA_SET: dict[str, object] = {
    "kind": "IdeaSet",
    "question_ref": "question:1",
    "context_pack_ref": "idea-context:1",
    "candidates": [
        {
            "candidate_key": "topology",
            "direction": "比较拓扑一致性与像素重建。",
            "rationale": "结构偏置不同。",
            "assumptions": ["增强保持拓扑。"],
            "risks": ["可能保留伪影。"],
            "evidence_boundary": {
                "accepted_evidence_refs": [],
                "supported": "研究范围已经固定。",
                "inferred": "拓扑约束可能改善召回。",
                "unknown": "跨设备稳健性未知。",
            },
            "falsification_hint": {
                "test": "比较形态召回。",
                "would_refute": "召回没有改善。",
            },
            "material_difference": {
                "from_history": "历史没有相同机制。",
                "from_peers": "干预轴是拓扑。",
                "plan_commitment_change": "Plan 必须比较两类目标函数。",
            },
        }
    ],
    "recommendation": None,
}


_QUESTION_BINDING: dict[str, object] = {
    "initialization_id": "initialization:1",
    "quest_ref": "quest:1",
    "question_ref": "question:1",
    "content_ref": "question-content:1",
    "content_hash": "a" * 64,
    "schema_ref": "meta-research/question-content/v1",
    "content_receipt": _receipt(
        "research_memory",
        "question_content_acceptance",
        "receipt:rm-question-1",
        "question-content:1",
        "b",
    ),
    "question_receipt": _receipt(
        "research_graph",
        "root_question_acceptance",
        "receipt:rg-question-1",
        "question:1",
        "c",
    ),
}


_IDEA_BINDING: dict[str, object] = {
    "outcome_ref": "idea-set:1",
    "outcome_kind": "idea_set",
    "content_ref": "idea-content:1",
    "payload_hash": "d" * 64,
    "outcome_hash": canonical_hash(_IDEA_SET),
    "content_receipt": _receipt(
        "research_memory",
        "idea_outcome_content_acceptance",
        "receipt:rm-idea-1",
        "idea-content:1",
        "e",
    ),
    "outcome_receipt": _receipt(
        "research_graph",
        "idea_outcome_accepted",
        "receipt:rg-idea-1",
        "idea-set:1",
        "f",
    ),
    "stage_commit_ref": "stage-commit:idea-1",
    "stage_commit_receipt": _receipt(
        "advancement_engine",
        "stage_commit",
        "receipt:ae-idea-1",
        "stage-commit:idea-1",
        "1",
    ),
    "idea_set": _IDEA_SET,
}


def _context_pack() -> dict[str, object]:
    return {
        "schema_ref": "meta-research/plan-context-pack/v1",
        "cycle_ref": "cycle:1",
        "accepted_question_binding": _QUESTION_BINDING,
        "accepted_idea_set_binding": _IDEA_BINDING,
        "evidence_catalog": [],
        "evidence_reference_revision": 0,
    }


def _runtime_binding() -> PlanRuntimeBinding:
    return PlanRuntimeBinding(
        packaged_skill_bundle_hash="1" * 64,
        instruction_set_hash="2" * 64,
        model_ref="test-model",
        harness_adapter_ref="test-harness",
        mcp_bindings=(),
        capability_bindings=("structured-output",),
        resource_bindings=("test-resource",),
    )


def _request(**changes: object) -> PlanSkillRequest:
    context_pack = _context_pack()
    values: dict[str, object] = {
        "stage_request_ref": "stage-request:plan-1",
        "cycle_ref": "cycle:1",
        "question_ref": "question:1",
        "idea_set_ref": "idea-set:1",
        "context_pack_ref": "plan-context:1",
        "context_pack_hash": canonical_hash(context_pack),
        "context_pack": context_pack,
        "accepted_question_content": {
            "unknown_statement": "哪种约束保留稀有形态？",
            "answer_shape": "比较召回与适用边界。",
            "applicability_scope": "低照度、多设备。",
        },
        "accepted_idea_set": _IDEA_SET,
        "root_session_ref": "ar-session:1",
        "submission_revision": 1,
        "runtime_binding": _runtime_binding(),
    }
    values.update(changes)
    return PlanSkillRequest(**values)  # type: ignore[arg-type]


def _plan(goal: str = "比较跨设备形态召回。") -> dict[str, object]:
    answer_contract: dict[str, object] = {
        "source_question_ref": "question:1",
        "source_idea_set_ref": "idea-set:1",
        "obligations": [
            {
                "obligation_key": "cross-device",
                "statement": "确定拓扑约束是否跨设备改善形态召回。",
                "minimum_support": "至少两种设备的可复查比较。",
                "question_trace": ["unknown_statement", "answer_shape"],
                "idea_relevance": [
                    {
                        "idea_ref": "topology",
                        "role": "experiment_lens",
                        "rationale": "现有证据不足，需要形成可证伪实验语义。",
                    }
                ],
            }
        ],
    }
    answer_contract["answer_contract_hash"] = canonical_hash(answer_contract)
    return {
        "schema_ref": "meta-research/plan-document/v1",
        "kind": "PlanDocument",
        "question_ref": "question:1",
        "idea_set_ref": "idea-set:1",
        "context_pack_ref": "plan-context:1",
        "answer_contract": answer_contract,
        "evidence_reuse_set": [],
        "coverage": [
            {
                "obligation_key": "cross-device",
                "disposition": "gap",
                "evidence_uses": [],
                "insufficiency": "当前闭包没有跨设备比较。",
            }
        ],
        "gap_set": ["cross-device"],
        "experiment_briefs": [
            {
                "experiment_key": "cross-device-topology",
                "gap_obligation_keys": ["cross-device"],
                "goal": goal,
                "characteristics": "同一形态任务比较拓扑与像素目标。",
                "boundary_constraints": "固定数据划分和测量口径，改变设备域。",
                "semantic_delta": "Baseline 为像素目标，Variant 加入拓扑约束。",
                "contributing_idea_refs": ["topology"],
            }
        ],
        "idea_trace": [
            {
                "idea_ref": "topology",
                "obligation_roles": [
                    {
                        "obligation_key": "cross-device",
                        "role": "experiment_lens",
                    }
                ],
            }
        ],
        "bundle_disposition": "experiments_required",
        "source_bindings": {
            "question_ref": "question:1",
            "idea_set_ref": "idea-set:1",
            "context_pack_ref": "plan-context:1",
            "context_pack_hash": canonical_hash(_context_pack()),
            "evidence_reference_revision": 0,
        },
    }


def _result(
    *,
    draft: dict[str, object] | None = None,
    final: dict[str, object] | None = None,
    findings: tuple[dict[str, str], ...] = (),
    dispositions: tuple[dict[str, str], ...] = (),
) -> PlanSkillResult:
    draft = draft or _plan()
    return PlanSkillResult(
        reviewed_draft=draft,
        final_plan=final or draft,
        findings=findings,
        dispositions=dispositions,
        primary_session_ref="codex-plan-primary:1",
        review_mode="harness_child_agent",
        reviewer_agent_ref="codex-plan-reviewer:1",
        adapter_kind="codex_cli",
    )


def test_packaged_plan_skill_is_the_runtime_authority() -> None:
    package = files("meta_research.skills.plan_stage")
    skill = (package / "SKILL.md").read_text(encoding="utf-8")
    contract = (package / "references" / "contract.md").read_text(
        encoding="utf-8"
    )
    operations = (package / "references" / "owner-operations.md").read_text(
        encoding="utf-8"
    )

    assert "execution completed != content accepted != domain accepted" in skill
    assert "fork_turns=\"none\"" in skill
    assert "AnswerContract" in contract
    assert "每个 obligation" in contract
    assert "ExperimentBrief" in contract
    assert "TODO-IMPL" not in operations
    assert "Research Memory" in operations


def test_validator_accepts_exact_plan_and_owner_feedback_requires_material_change() -> None:
    draft_hash, final_hash, review_hash = validate_plan_skill_result(
        _request(), _result()
    )
    assert draft_hash == final_hash == canonical_hash(_plan())
    assert review_hash

    predecessor = _plan()
    request = _request(
        submission_revision=2,
        native_session_ref="codex-plan-primary:1",
        predecessor_submission_ref="plan-submission:1",
        owner_rejection_receipt_ref="receipt:rg-plan-rejection-1",
        owner_feedback=("ExperimentBrief 必须明确跨设备比较。",),
    )
    with pytest.raises(
        PlanSkillContractError, match="owner_feedback_revision_not_material"
    ):
        validate_plan_skill_result(
            request,
            _result(),
            predecessor_material_plan_hash=material_plan_hash(predecessor),
        )

    successor = _plan("在至少两种设备上比较形态召回。")
    validate_plan_skill_result(
        request,
        _result(draft=successor),
        predecessor_material_plan_hash=material_plan_hash(predecessor),
    )


class _SequenceRunner:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self._outputs = iter(outputs)
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = next(self._outputs)
        output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        self.calls.append((argv, prompt, schema))
        thread_id = "codex-plan-primary:1"
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": thread_id}
        ]
        if "reviewer_agent_ref" in schema.get("properties", {}):
            reviewer = output["reviewer_agent_ref"]
            for tool, status in (("spawn_agent", "pending_init"), ("wait", "completed")):
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"collab-{tool}:1",
                            "type": "collab_tool_call",
                            "tool": tool,
                            "sender_thread_id": thread_id,
                            "receiver_thread_ids": [reviewer],
                            "agents_states": {reviewer: {"status": status}},
                            "status": "completed",
                        },
                    }
                )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-plan-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_production_adapter_uses_one_native_root_and_a_fresh_child_reviewer(
    tmp_path: Path,
) -> None:
    draft = _plan()
    runner = _SequenceRunner(
        [
            {"plan": draft},
            {
                "reviewer_agent_ref": "codex-plan-reviewer:1",
                "findings": [],
                "final_plan": draft,
                "dispositions": [],
            },
        ]
    )
    adapter = CodexPlanSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )
    request = _request(runtime_binding=adapter.runtime_binding())

    result = adapter.execute(request)
    validate_plan_skill_result(request, result)

    assert result.primary_session_ref == "codex-plan-primary:1"
    assert result.reviewer_agent_ref == "codex-plan-reviewer:1"
    assert len(runner.calls) == 2
    primary_argv, primary_prompt, primary_schema = runner.calls[0]
    review_argv, review_prompt, review_schema = runner.calls[1]
    assert primary_argv[:2] == [str(tmp_path / "codex"), "exec"]
    assert "--json" in primary_argv
    assert "AcceptedQuestionBinding" in primary_prompt
    assert "完整 IdeaSet" in primary_prompt
    assert "EvidenceRef" in primary_prompt
    assert primary_schema["properties"]["plan"]["properties"]["answer_contract"]
    assert review_argv[-3:] == ["resume", "codex-plan-primary:1", "-"]
    assert 'fork_turns="none"' in review_prompt
    assert "ExperimentBrief" in review_prompt
    assert "final_plan" in review_schema["properties"]


def test_owner_feedback_is_present_in_the_successor_prompt(tmp_path: Path) -> None:
    successor = _plan("按 RG 反馈增加明确的跨设备对照。")
    runner = _SequenceRunner([{"plan": successor}])
    adapter = CodexPlanSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        submission_revision=2,
        native_session_ref="codex-plan-primary:1",
        predecessor_submission_ref="plan-submission:1",
        owner_rejection_receipt_ref="receipt:rg-plan-rejection-1",
        owner_feedback=("ExperimentBrief 必须明确跨设备比较。",),
    )

    draft = adapter.generate_draft(request)

    assert draft.primary_session_ref == "codex-plan-primary:1"
    assert "plan-submission:1" in runner.calls[0][1]
    assert "receipt:rg-plan-rejection-1" in runner.calls[0][1]
    assert "必须明确跨设备比较" in runner.calls[0][1]
