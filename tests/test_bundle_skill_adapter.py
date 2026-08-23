from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from meta_research.bundle_contract import BundleContractError, validate_target_plan
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillRequest,
    CodexBundleSkillAdapter,
    validate_bundle_skill_result,
)
from meta_research.owners.common import canonical_hash


def _plan_document() -> dict[str, object]:
    return {
        "question_ref": "question:bundle-1",
        "answer_contract": {"answer_contract_hash": "a" * 64},
        "evidence_reuse_set": [],
        "gap_set": ["gap:structure"],
        "experiment_briefs": [
            {
                "experiment_key": "experiment:structure",
                "gap_obligation_keys": ["gap:structure"],
                "goal": "比较冻结结构对结果复现的影响。",
                "boundary_constraints": "固定数据、协议和预算。",
                "semantic_delta": "只改变结构冻结策略。",
                "contributing_idea_refs": ["idea:structure"],
            }
        ],
        "bundle_disposition": "experiments_required",
    }


def _target_plan(plan: dict[str, object], context_hash: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-plan/v1",
        "kind": "TargetPlan",
        "formal_plan_ref": "formal-plan:bundle-1",
        "context_pack_ref": "context-pack:bundle-1",
        "targets": [
            {
                "target_key": "target:structure",
                "title": "结构冻结微实验",
                "target_type": "micro_experiment",
                "experiment_key": "experiment:structure",
                "gap_obligation_keys": ["gap:structure"],
                "depends_on": [],
                "goal": "比较冻结结构对结果复现的影响。",
                "hypothesis": "冻结必要结构会提高复现一致性。",
                "variant_parameter": 0.25,
                "sample_count": 8,
                "boundary_constraints": "固定数据、协议和预算。",
                "semantic_delta": "只改变结构冻结策略。",
                "contributing_idea_refs": ["idea:structure"],
                "risk_class": "normal",
            }
        ],
        "source_bindings": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document_hash": canonical_hash(plan),
            "context_pack_ref": "context-pack:bundle-1",
            "context_pack_hash": context_hash,
        },
    }


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
        thread_id = "codex-bundle-primary:1"
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": thread_id}
        ]
        if "reviewer_agent_ref" in schema.get("properties", {}):
            reviewer = output["reviewer_agent_ref"]
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
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
    path.write_text("#!/bin/sh\nprintf 'codex-bundle-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_production_adapter_freezes_skill_and_uses_fresh_child_review(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    question_binding = {"question_ref": "question:bundle-1"}
    formal_plan_binding = {
        "formal_plan_ref": "formal-plan:bundle-1",
        "plan_document": plan,
        "plan_document_hash": canonical_hash(plan),
        "answer_contract_hash": "a" * 64,
    }
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "accepted_question_binding": question_binding,
        "accepted_formal_plan_binding": formal_plan_binding,
    }
    context_hash = canonical_hash(context)
    target_plan = _target_plan(plan, context_hash)
    runner = _SequenceRunner(
        [
            {"target_plan": target_plan},
            {
                "reviewer_agent_ref": "codex-bundle-reviewer:1",
                "findings": [],
                "final_target_plan": target_plan,
                "dispositions": [],
            },
            {
                "action": "dispatch",
                "selected_target_ref": "target:accepted-structure",
                "rationale": "This frontier item closes the frozen gap.",
            },
        ]
    )
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )
    binding = adapter.runtime_binding()
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1",
        cycle_ref="cycle:bundle-1",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-1",
        runtime_binding=binding,
    )

    result = adapter.execute(request)
    validate_bundle_skill_result(request, result)
    dispatch = adapter.schedule_target(
        BundleDispatchRequest(
            stage_request_ref=request.stage_request_ref,
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1",
            fence_ref="bundle-fence:1",
            graph_ref="target-graph:1",
            generation=1,
            frontier=(
                {
                    "target_ref": "target:accepted-structure",
                    "target_key": "target:structure",
                },
            ),
            state={
                "schema_ref": "meta-research/bundle-dispatch-state/v1",
                "target_commit_refs": [],
                "running_targets": [],
                "blocked_targets": [],
            },
            native_session_ref=result.primary_session_ref,
            runtime_binding=binding,
        )
    )

    assert result.primary_session_ref == "codex-bundle-primary:1"
    assert result.reviewer_agent_ref == "codex-bundle-reviewer:1"
    assert any(
        "meta_research.skills.bundle_stage/SKILL.md" in item
        for item in binding.resource_bindings
    )
    primary_argv, primary_prompt, primary_schema = runner.calls[0]
    review_argv, review_prompt, review_schema = runner.calls[1]
    dispatch_argv, dispatch_prompt, dispatch_schema = runner.calls[2]
    assert primary_argv[:2] == [str(tmp_path / "codex"), "exec"]
    assert "Agent Session 绝不是 Target 或 TargetRun" in primary_prompt
    assert primary_schema["properties"]["target_plan"]["properties"]["targets"]
    assert review_argv[-3:] == ["resume", "codex-bundle-primary:1", "-"]
    assert 'fork_turns="none"' in review_prompt
    assert "Target DAG" in review_prompt
    assert "final_target_plan" in review_schema["properties"]
    assert dispatch.selected_target_ref == "target:accepted-structure"
    assert dispatch.native_session_ref == result.primary_session_ref
    assert dispatch_argv[-3:] == ["resume", "codex-bundle-primary:1", "-"]
    assert "durable frontier" in dispatch_prompt
    assert dispatch_schema["properties"]["selected_target_ref"]["anyOf"][0]["enum"] == [
        "target:accepted-structure"
    ]


def test_target_plan_rejects_a_cycle_before_research_graph_acceptance() -> None:
    plan = _plan_document()
    context_hash = "9" * 64
    target_plan = _target_plan(plan, context_hash)
    first = target_plan["targets"][0]
    assert isinstance(first, dict)
    second = {
        **deepcopy(first),
        "target_key": "target:replication",
        "title": "结构冻结复验",
        "depends_on": [first["target_key"]],
    }
    first["depends_on"] = [second["target_key"]]
    target_plan["targets"] = [first, second]

    with pytest.raises(BundleContractError, match="target_dag_invalid"):
        validate_target_plan(
            target_plan,
            formal_plan_ref="formal-plan:bundle-1",
            context_pack_ref="context-pack:bundle-1",
            context_pack_hash=context_hash,
            plan_document=plan,
        )
