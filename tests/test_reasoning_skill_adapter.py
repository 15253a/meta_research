from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.idea_skill import _compile_codex_output_schema
from meta_research.harness import FullConformanceBinding, ResidentMcpChannel
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.reasoning_contract import (
    REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF,
    CANDIDATE_COMPLETION_SCHEMA_REF,
    NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
    REASONING_REVIEW_SCHEMA_REF,
    REASONING_STAGE_OUTPUT_SCHEMA_REF,
    SCIENTIFIC_OUTCOME_SCHEMA_REF,
    ReasoningContractError,
)
from meta_research.reasoning_skill import (
    ReasoningSkillDraft,
    ReasoningSkillRequest,
    ReasoningSkillResult,
    ReasoningSkillUnavailable,
    CodexReasoningSkillAdapter,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    _reasoning_autonomous_checkpoint_schema,
    _reasoning_primary_output_schema,
    _reasoning_review_response_schema,
    _reasoning_stage_output_schema,
    _validate_request,
    validate_reasoning_skill_draft,
    validate_reasoning_skill_result,
)
from meta_research.semantic_mcp import McpConnection, ResidentMcpBinding


def _request() -> ReasoningSkillRequest:
    context_pack: dict[str, object] = {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": "cycle:1",
        "foreground_epoch": 7,
        "accepted_question_binding": {
            "question_ref": "question:1",
            "quest_ref": "quest:1",
        },
        "question_literature_input": {"kind": "revision"},
        "upstream_stage_closure": [
            {"stage": "idea", "commit_ref": "stage-commit:idea"},
            {"stage": "plan", "commit_ref": "stage-commit:plan"},
            {"stage": "bundle", "commit_ref": "stage-commit:bundle"},
        ],
        "plan_evidence_input": {
            "kind": "none",
            "basis_stage_commit_refs": [
                "stage-commit:idea", "stage-commit:plan", "stage-commit:bundle"
            ],
        },
        "accepted_target_commit_closures": [],
        "research_context": {
            "schema_ref": "meta-research/reasoning-research-context/v2",
            "cycle_ref": "cycle:1",
            "question_ref": "question:1",
            "quest_ref": "quest:1",
            "goal_revision_ref": "goal-revision:1",
            "quest_goal_revision": {
                "kind": "QuestGoalRevision",
                "quest_ref": "quest:1",
                "goal_revision_ref": "goal-revision:1",
            },
            "graph_binding": {
                "schema_ref": "meta-research/reasoning-graph-context/v1",
                "issuer": "research_graph",
                "quest_ref": "quest:1",
                "question_ref": "question:1",
                "graph_revision_ref": "graph-revision:1",
                "active_question_refs": ["question:1"],
                "parent_question_bindings": [],
                "prior_current_question_outcomes": [],
                "binding_ref": "reasoning-graph-context:1",
                "binding_hash": "a" * 64,
            },
            "causal_context": {
                "target_commit_refs": [], "changed_axis_fact_refs": [],
                "held_fixed_fact_refs": [], "provenance_refs": [],
            },
            "upstream_stage_commit_refs": [
                "stage-commit:idea", "stage-commit:plan", "stage-commit:bundle"
            ],
        },
    }
    return ReasoningSkillRequest(
        stage_request_ref="stage-request:reasoning:1",
        run_ref="reasoning-run:1",
        attempt_ref="reasoning-attempt:1",
        fence_ref="reasoning-fence:1",
        cycle_ref="cycle:1",
        question_ref="question:1",
        quest_ref="quest:1",
        goal_revision_ref="goal-revision:1",
        foreground_epoch=7,
        context_pack_ref="context-pack:reasoning:1",
        context_pack_hash=canonical_hash(context_pack),
        context_pack=context_pack,
        frozen_evidence_closure=(
            {
                "kind": "LiteratureRecord",
                "ref": "literature-record:1",
                "evidence_basis": "verified_fulltext",
                "evidence_basis_ref": "reading-result:1",
            },
            {
                "kind": "LogAsset",
                "ref": "log-asset:1",
                "source_subject_ref": "reasoning-run:1",
                "owner_acceptance_receipt_ref": "receipt:log-asset:1",
            },
        ),
        root_session_ref="ar-root-session:1",
        runtime_binding=ReasoningRuntimeBinding(
            packaged_skill_bundle_hash="1" * 64,
            instruction_set_hash="2" * 64,
            model_ref="test-model",
            harness_adapter_ref="test-harness",
            mcp_bindings=("semantic-mcp:test",),
            capability_bindings=("semantic-mcp-resident",),
            resource_bindings=("test:resource",),
        ),
    )


def _stage_output() -> dict[str, object]:
    scientific_outcome: dict[str, object] = {
        "schema_ref": SCIENTIFIC_OUTCOME_SCHEMA_REF,
        "kind": "ScientificOutcomeCandidate",
        "outcome_ref": "scientific-outcome:1",
        "stage_run_request_ref": "stage-request:reasoning:1",
        "cycle_ref": "cycle:1",
        "question_ref": "question:1",
        "quest_ref": "quest:1",
        "goal_revision_ref": "goal-revision:1",
        "foreground_epoch": 7,
        "disposition": "affirmed",
        "claim": "The accepted full-text record supports the bounded claim.",
        "evidence": [
            {
                "kind": "LiteratureRecord",
                "ref": "literature-record:1",
                "finding": "supporting",
            }
        ],
        "missing_evidence": [],
        "uncertainty_basis": [],
        "support_scope": ["The accepted Question within the frozen context."],
        "limitations": ["No inference outside the frozen applicability scope."],
        "causal_interpretation": {
            "target_commit_refs": [],
            "changed_axis_fact_refs": [],
            "held_fixed_fact_refs": [],
            "provenance_refs": [],
            "attribution_basis_refs": ["literature-record:1"],
            "claim_scope": "The bounded accepted literature association.",
            "statement": "The record supports association, not intervention.",
            "sufficiency_rationale": "No causal TargetCommit was frozen.",
            "confounders": ["No controlled intervention was frozen."],
        },
        "research_synthesis": {
            "cycle": {"cycle_ref": "cycle:1", "impact": "One bounded finding."},
            "current_question": {
                "question_ref": "question:1",
                "prior_accepted_outcome_refs": [],
                "progress": "The current Question gains bounded support.",
            },
            "parent_questions": [],
            "quest": {
                "quest_ref": "quest:1",
                "goal_revision_ref": "goal-revision:1",
                "graph_revision_ref": "graph-revision:1",
                "impact": "The frozen Goal gains bounded support.",
            },
        },
        "is_authoritative": False,
    }
    return {
        "schema_ref": REASONING_STAGE_OUTPUT_SCHEMA_REF,
        "scientific_outcome": scientific_outcome,
        "next_cycle_proposal": {
            "schema_ref": NEXT_CYCLE_PROPOSAL_SCHEMA_REF,
            "kind": "NextCycleProposal",
            "source_quest_ref": "quest:1",
            "source_cycle_ref": "cycle:1",
            "source_reasoning_stage_run_request_ref": (
                "stage-request:reasoning:1"
            ),
            "source_scientific_outcome_ref": "scientific-outcome:1",
            "source_question_ref": "question:1",
            "source_foreground_epoch": 7,
            "target_question_ref": "question:1",
            "target_question_anchor_ref": "question-anchor:1",
            "entry_stage": "idea",
            "typed_skip_basis_refs_by_stage": {},
            "is_authoritative": False,
        },
        "candidate_completion": None,
    }


def test_public_draft_seam_validates_one_closed_reasoning_output() -> None:
    request = _request()
    output = _stage_output()
    result = ReasoningSkillDraft(
        draft=output,
        primary_session_ref="provider-session:1",
        adapter_kind="deterministic-test",
    )

    assert validate_reasoning_skill_draft(request, result) == (
        canonical_hash(output),
        canonical_hash(output["scientific_outcome"]),
        canonical_hash(output["next_cycle_proposal"]),
    )


@pytest.mark.parametrize("closure_size", [100, 550])
def test_large_legal_evidence_closure_keeps_actual_schemas_within_budget(
    closure_size: int,
) -> None:
    request = replace(
        _request(),
        frozen_evidence_closure=tuple(
            {
                "kind": "LiteratureRecord",
                "ref": f"literature-record:{index:04d}",
                "evidence_basis": "verified_fulltext",
                "evidence_basis_ref": f"reading-result:{index:04d}",
            }
            for index in range(closure_size)
        ),
    )

    _validate_request(request)
    closed = _reasoning_stage_output_schema(request)
    _compile_codex_output_schema(_reasoning_primary_output_schema(request))
    _compile_codex_output_schema(
        _reasoning_review_response_schema(request, closed)
    )


def test_large_legal_graph_context_keeps_all_actual_schemas_within_budget() -> None:
    request = _request()
    context_pack = json.loads(json.dumps(request.context_pack))
    graph = context_pack["research_context"]["graph_binding"]
    parent_refs = [f"question:parent:{index:03d}" for index in range(250)]
    graph["active_question_refs"] = sorted(
        [request.question_ref, *parent_refs]
    )
    graph["parent_question_bindings"] = [
        {
            "question_ref": ref,
            "parent_question_ref": None,
            "question_receipt_ref": f"receipt:{ref}",
        }
        for ref in parent_refs
    ]
    graph["prior_current_question_outcomes"] = [
        {
            "cycle_ref": f"cycle:prior:{index:03d}",
            "request_ref": f"stage-request:prior:{index:03d}",
            "outcome_ref": f"scientific-outcome:prior:{index:03d}",
            "disposition": "uncertain",
            "outcome_receipt_ref": f"receipt:outcome:{index:03d}",
        }
        for index in range(250)
    ]
    request = replace(
        request,
        context_pack=context_pack,
        context_pack_hash=canonical_hash(context_pack),
    )

    _validate_request(request)
    closed = _reasoning_stage_output_schema(request)
    autonomous = _reasoning_autonomous_checkpoint_schema(request)
    _compile_codex_output_schema(_reasoning_primary_output_schema(request))
    _compile_codex_output_schema(
        _reasoning_review_response_schema(request, closed)
    )
    _compile_codex_output_schema(
        _reasoning_review_response_schema(request, autonomous)
    )


@pytest.mark.parametrize("large_input", ["causal_ref", "milestone_ref"])
def test_long_legal_frozen_refs_do_not_expand_into_provider_schema(
    large_input: str,
) -> None:
    request = _request()
    context_pack = json.loads(json.dumps(request.context_pack))
    if large_input == "causal_ref":
        context_pack["research_context"]["causal_context"][
            "target_commit_refs"
        ] = ["t" * 38_000]
    else:
        context_pack["upstream_stage_closure"][0]["commit_ref"] = (
            "m" * 116_000
        )
    request = replace(
        request,
        context_pack=context_pack,
        context_pack_hash=canonical_hash(context_pack),
    )

    _validate_request(request)
    closed = _reasoning_stage_output_schema(request)
    autonomous = _reasoning_autonomous_checkpoint_schema(request)
    _compile_codex_output_schema(_reasoning_primary_output_schema(request))
    _compile_codex_output_schema(
        _reasoning_review_response_schema(request, closed)
    )
    _compile_codex_output_schema(
        _reasoning_review_response_schema(request, autonomous)
    )


def test_public_result_seam_records_one_advisory_child_review() -> None:
    request = _request()
    output = _stage_output()
    result = ReasoningSkillResult(
        reviewed_draft=output,
        scientific_outcome=output["scientific_outcome"],  # type: ignore[arg-type]
        next_cycle_proposal=output["next_cycle_proposal"],  # type: ignore[arg-type]
        candidate_completion=None,
        findings=(),
        dispositions=(),
        primary_session_ref="provider-session:1",
        review_mode="harness_child_agent",
        reviewer_agent_ref="provider-child:1",
        adapter_kind="deterministic-test",
    )

    final_output = result.outcome_document()
    review = result.review_document()
    assert final_output == output
    assert review == {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "review_mode": "harness_child_agent",
        "reviewer_agent_ref": "provider-child:1",
        "reviewed_draft_hash": canonical_hash(output),
        "findings": [],
        "dispositions": [],
        "final_output_hash": canonical_hash(output),
        "independent": True,
        "advisory_only": True,
    }
    assert validate_reasoning_skill_result(request, result) == (
        canonical_hash(output),
        canonical_hash(final_output),
        canonical_hash(output["scientific_outcome"]),
        canonical_hash(output["next_cycle_proposal"]),
        canonical_hash(review),
    )


def test_public_draft_seam_rejects_completion_outside_frozen_milestone_basis(
) -> None:
    request = _request()
    context_pack = {
        **request.context_pack,
        "upstream_stage_closure": [
            {"stage": "idea", "commit_ref": "stage-commit:idea"},
            {"stage": "plan", "commit_ref": "stage-commit:plan"},
            {"stage": "bundle", "commit_ref": "stage-commit:bundle"},
        ],
    }
    request = replace(
        request,
        context_pack=context_pack,
        context_pack_hash=canonical_hash(context_pack),
    )
    output = _stage_output()
    outcome = output["scientific_outcome"]
    assert isinstance(outcome, dict)
    output["next_cycle_proposal"] = None
    output["candidate_completion"] = {
        "schema_ref": CANDIDATE_COMPLETION_SCHEMA_REF,
        "kind": "CandidateCompletion",
        "source_quest_ref": request.quest_ref,
        "source_cycle_ref": request.cycle_ref,
        "source_reasoning_stage_run_request_ref": request.stage_request_ref,
        "source_scientific_outcome_ref": outcome["outcome_ref"],
        "source_question_ref": request.question_ref,
        "source_foreground_epoch": request.foreground_epoch,
        "current_quest_ref": request.quest_ref,
        "current_goal_revision_ref": request.goal_revision_ref,
        "completion_milestone_basis_refs": ["stage-commit:forged"],
        "rationale": "All current milestones are satisfied.",
        "is_authoritative": False,
    }
    result = ReasoningSkillDraft(
        draft=output,
        primary_session_ref="provider-session:1",
        adapter_kind="deterministic-test",
    )

    with pytest.raises(
        ReasoningContractError,
        match="candidate_completion_basis_invalid",
    ):
        validate_reasoning_skill_draft(request, result)


def _operation_binding(operation_id: str) -> dict[str, object]:
    return {
        "semantic_operation_id": operation_id,
        "operation_contract_version": "v1",
        "owning_module": operation_id.split(".", 1)[0],
        "access_mode": "read",
        "input_schema_hash": "4" * 64,
        "output_schema_hash": "5" * 64,
        "reconciliation_operation_id": None,
        "discovered_tool_name": operation_id,
    }


class _FullConformanceAuthority:
    def __init__(self) -> None:
        self.binding = FullConformanceBinding(
            contract_ref="meta-research/harness-full-conformance/v1",
            contract_hash="1" * 64,
            conformance_ref="hfc:reasoning:1",
            semantic_mcp_catalog_hash="2" * 64,
            semantic_mcp_operation_bindings_hash="3" * 64,
            required_families=("codex", "claude"),
            required_capabilities=("semantic_mcp", "subagent"),
            required_operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS,
            profile_receipts=(
                "harness-profile:codex",
                "harness-profile:claude",
            ),
        )
        self.issued: list[dict[str, object]] = []
        self.revoked: list[ResidentMcpChannel] = []

    def require_full_conformance_binding(self) -> FullConformanceBinding:
        return self.binding

    def issue_resident_mcp_channel(
        self,
        *,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> ResidentMcpChannel:
        self.issued.append(
            {
                "run_ref": run_ref,
                "attempt_ref": attempt_ref,
                "root_session_ref": root_session_ref,
                "fence_ref": fence_ref,
                "capability_binding_hash": capability_binding_hash,
                "operation_ids": operation_ids,
            }
        )
        generation = len(self.issued)
        grant_ref = f"mcp-grant:reasoning:{generation}"
        return ResidentMcpChannel(
            connection=McpConnection(
                token=f"reasoning-resident-secret-{generation}",
                grant_ref=grant_ref,
            ),
            binding=ResidentMcpBinding(
                server_instance_ref="semantic-mcp:reasoning-test",
                endpoint_ref="/mcp",
                catalog_revision=1,
                catalog_hash=self.binding.semantic_mcp_catalog_hash,
                health_receipt_ref="mcp-health:reasoning-test",
                connection_grant_ref=grant_ref,
                operation_bindings=tuple(
                    _operation_binding(operation_id)
                    for operation_id in operation_ids
                ),
            ),
        )

    def revoke_resident_mcp_channel(self, channel: ResidentMcpChannel) -> None:
        self.revoked.append(channel)


class _SequenceRunner:
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        observed_operation_ids: tuple[str, ...] = (
            REASONING_ROOT_SEMANTIC_OPERATION_IDS
        ),
        emit_review_trace: bool = True,
        emit_primary_review_trace: bool = False,
        observed_operation_ids_by_call: (
            list[tuple[str, ...]] | None
        ) = None,
        provider_wire_envelope: bool = True,
    ) -> None:
        self._outputs = iter(outputs)
        self._observed_operation_ids = observed_operation_ids
        self._emit_review_trace = emit_review_trace
        self._emit_primary_review_trace = emit_primary_review_trace
        self._observed_operation_ids_by_call = observed_operation_ids_by_call
        self._provider_wire_envelope = provider_wire_envelope
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = next(self._outputs)
        if (
            self._provider_wire_envelope
            and set(schema.get("properties", {})) == {"provider_output"}
            and set(output) != {"provider_output"}
        ):
            output = {"provider_output": output}
        output_path.write_text(json.dumps(output), encoding="utf-8")
        self.calls.append((argv, prompt, schema))
        self.environments.append(environment)
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": "provider-session:1"}
        ]
        observed_operation_ids = (
            self._observed_operation_ids_by_call[len(self.calls) - 1]
            if self._observed_operation_ids_by_call is not None
            else self._observed_operation_ids
        )
        for operation_id in observed_operation_ids:
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"mcp:{operation_id}",
                        "type": "mcp_tool_call",
                        "server": "meta_research",
                        "tool": operation_id,
                        "status": "completed",
                        "result": {"isError": False},
                    },
                }
            )
        properties = schema.get("properties", {})
        if self._emit_primary_review_trace and "scientific_outcome" in properties:
            reviewer = "provider-primary-reviewer:1"
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"collab:primary:{tool}",
                            "type": "collab_tool_call",
                            "tool": tool,
                            "sender_thread_id": "provider-session:1",
                            "receiver_thread_ids": [reviewer],
                            "agents_states": {reviewer: {"status": status}},
                            "status": "completed",
                        },
                    }
                )
        if self._emit_review_trace and "reviewer_agent_ref" in properties:
            reviewer = output["reviewer_agent_ref"]
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"collab:{tool}",
                            "type": "collab_tool_call",
                            "tool": tool,
                            "sender_thread_id": "provider-session:1",
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

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout, environment)


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-reasoning-test 1\\n'\n")
    path.chmod(0o700)
    return path


def _durable_reasoning_codex(
    path: Path,
    output: dict[str, object],
) -> Path:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "provider-session:1"}
    ]
    events.extend(
        {
            "type": "item.completed",
            "item": {
                "id": f"mcp:{operation_id}",
                "type": "mcp_tool_call",
                "server": "meta_research",
                "tool": operation_id,
                "status": "completed",
                "result": {"isError": False},
            },
        }
        for operation_id in REASONING_ROOT_SEMANTIC_OPERATION_IDS
    )
    reviewer = output["reviewer_agent_ref"]
    for tool, status in (("spawn_agent", "pending_init"), ("wait", "completed")):
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": f"collab:{tool}",
                    "type": "collab_tool_call",
                    "tool": tool,
                    "sender_thread_id": "provider-session:1",
                    "receiver_thread_ids": [reviewer],
                    "agents_states": {reviewer: {"status": status}},
                    "status": "completed",
                },
            }
        )
    encoded_output = repr(json.dumps(output, ensure_ascii=False))
    encoded_events = repr(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    )
    path.write_text(
        "#!/usr/bin/python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-reasoning-durable-test 1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.buffer.read()\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({encoded_output}, encoding='utf-8')\n"
        f"print({encoded_events})\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_production_adapter_uses_one_session_and_scoped_resident_mcp(
    tmp_path: Path,
) -> None:
    primary = _stage_output()
    review = {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "reviewer_agent_ref": "provider-child:1",
        "findings": [],
        "final_output": primary,
        "dispositions": [],
    }
    runner = _SequenceRunner([primary, review])
    authority = _FullConformanceAuthority()
    adapter = CodexReasoningSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    binding = adapter.runtime_binding()
    request = replace(_request(), runtime_binding=binding)

    result = adapter.execute(request)

    assert result.outcome_document() == primary
    assert result.primary_session_ref == "provider-session:1"
    assert result.reviewer_agent_ref == "provider-child:1"
    assert binding.model_ref == "gpt-5.6-sol"
    assert (
        "codex-config:model_reasoning_effort=max"
        in binding.resource_bindings
    )
    assert any(
        item.startswith(
            "adapter-source:meta_research.idea_skill@sha256:"
        )
        for item in binding.resource_bindings
    )
    assert len(runner.calls) == 2
    assert 'model_reasoning_effort="max"' in runner.calls[0][0]
    assert runner.calls[1][0][-3:] == ["resume", "provider-session:1", "-"]
    assert "本回合仅执行 Primary draft phase" in runner.calls[0][1]
    assert "禁止调用 spawn_agent 或 wait" in runner.calls[0][1]
    assert "当前 frozen reviewed_draft" in runner.calls[1][1]
    assert "不得复用 Primary phase" in runner.calls[1][1]
    assert len(authority.issued) == 2
    assert len(authority.revoked) == 2
    assert all(
        call["operation_ids"] == REASONING_ROOT_SEMANTIC_OPERATION_IDS
        for call in authority.issued
    )
    assert all(
        environment is not None
        and set(environment) == {"META_RESEARCH_MCP_TOKEN"}
        for environment in runner.environments
    )
    for (argv, prompt, schema), environment in zip(
        runner.calls, runner.environments, strict=True
    ):
        assert schema["additionalProperties"] is False
        assert environment is not None
        token = environment["META_RESEARCH_MCP_TOKEN"]
        assert token not in prompt
        assert all(token not in value for value in argv)
    primary_schema = runner.calls[0][2]
    assert primary_schema["required"] == ["provider_output"]
    provider_variants = primary_schema["properties"]["provider_output"][
        "anyOf"
    ]
    next_cycle_variant = next(
        variant
        for variant in provider_variants
        if variant["properties"]["schema_ref"]["const"]
        == REASONING_STAGE_OUTPUT_SCHEMA_REF
        and variant["properties"]["candidate_completion"].get("type")
        == "null"
    )
    completion_variant = next(
        variant
        for variant in provider_variants
        if variant["properties"]["schema_ref"]["const"]
        == REASONING_STAGE_OUTPUT_SCHEMA_REF
        and variant["properties"]["next_cycle_proposal"].get("type")
        == "null"
    )
    primary_properties = next_cycle_variant["properties"]
    assert isinstance(primary_properties, dict)
    scientific_schema = primary_properties["scientific_outcome"]
    assert isinstance(scientific_schema, dict)
    scientific_properties = scientific_schema["properties"]
    assert isinstance(scientific_properties, dict)
    evidence_schema = scientific_properties["evidence"]
    assert isinstance(evidence_schema, dict)
    evidence_item = evidence_schema["items"]
    assert isinstance(evidence_item, dict)
    evidence_properties = evidence_item["properties"]
    assert isinstance(evidence_properties, dict)
    assert evidence_properties["kind"] == {
        "type": "string",
        "enum": [
            "AnalysisAsset",
            "CheckpointArtifact",
            "LiteratureRecord",
            "LogAsset",
            "MetricResult",
        ],
    }
    assert evidence_properties["ref"] == {"type": "string", "minLength": 1}
    assert evidence_properties["finding"]["enum"] == [
        "supporting",
        "negative",
        "partial",
        "context",
    ]
    candidate_schema = completion_variant["properties"]["candidate_completion"]
    assert isinstance(candidate_schema, dict)
    completion_properties = candidate_schema["properties"]
    assert isinstance(completion_properties, dict)
    assert completion_properties["completion_milestone_basis_refs"] == {
        "type": "array",
        "minItems": 3,
        "maxItems": 3,
        "items": {"type": "string", "minLength": 1},
    }


@pytest.mark.parametrize("autonomous", [False, True])
def test_durable_reasoning_review_trace_failure_is_terminal_for_both_branches(
    tmp_path: Path,
    autonomous: bool,
) -> None:
    base_request = _request()
    if autonomous:
        from test_public_autonomous_creation import _AutonomousReasoningSkill

        primary = _AutonomousReasoningSkill().generate_draft(base_request).draft
        reviewer = "provider-autonomous-child:1"
    else:
        primary = _stage_output()
        reviewer = "provider-normal-child:1"
    review = {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "reviewer_agent_ref": reviewer,
        "findings": [],
        "final_output": primary,
        "dispositions": [],
    }
    workspace = tmp_path / f"reasoning-terminal-{autonomous}"
    executable = _fake_codex(tmp_path / f"codex-terminal-{autonomous}")

    def configured(runner: _SequenceRunner) -> CodexReasoningSkillAdapter:
        adapter = CodexReasoningSkillAdapter(
            workspace,
            executable=str(executable),
            process_runner=runner,
        )
        adapter.bind_full_conformance_authority(_FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        return adapter

    runner = _SequenceRunner(
        [primary, review],
        emit_review_trace=False,
    )
    adapter = configured(runner)
    request = replace(
        base_request,
        runtime_binding=adapter.runtime_binding(),
        job_ref=f"reasoning-terminal-{autonomous}-job",
    )

    with pytest.raises(
        ReasoningSkillUnavailable, match="codex_child_review_spawn_invalid"
    ) as caught:
        adapter.execute(request)
    assert caught.value.recovery_checkpoint is not None
    assert caught.value.recovery_checkpoint["contract_failure_code"] == (
        "codex_child_review_spawn_invalid"
    )
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "codex_child_review_spawn_invalid"
    )
    assert len(runner.calls) == 2

    no_replay = _SequenceRunner([])
    restarted = configured(no_replay)
    with pytest.raises(
        ReasoningSkillUnavailable, match="codex_child_review_spawn_invalid"
    ):
        restarted.execute(
            replace(request, runtime_binding=restarted.runtime_binding())
        )
    assert no_replay.calls == []


def _autonomous_resume_fixture(
    request: ReasoningSkillRequest,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    from test_public_autonomous_creation import _AutonomousReasoningSkill

    deterministic = _AutonomousReasoningSkill()
    checkpoint = deterministic.generate_draft(request).draft
    assert (
        checkpoint["schema_ref"]
        == REASONING_AUTONOMOUS_CHECKPOINT_SCHEMA_REF
    )
    creation_result: dict[str, object] = {
        "question_anchor": {
            "question_ref": "question:autonomous:1",
            "ref": "question-anchor:autonomous:1",
        },
        "graph_presence_fact": {
            "question_ref": "question:autonomous:1",
            "value": "present",
            "is_current": True,
            "graph_revision_ref": "graph-revision:autonomous:1",
        },
        "question_research_state_fact": {
            "question_ref": "question:autonomous:1",
            "value": "open",
            "is_current": True,
            "graph_revision_ref": "graph-revision:autonomous:1",
        },
    }
    expected = deterministic.resume_after_autonomous_creation(
        replace(request, native_session_ref="provider-session:1"),
        checkpoint,
        creation_result,
    )
    review = {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "reviewer_agent_ref": expected.reviewer_agent_ref,
        "findings": list(expected.findings),
        "final_output": expected.outcome_document(),
        "dispositions": list(expected.dispositions),
    }
    return checkpoint, creation_result, review


@pytest.mark.parametrize(
    ("operation_name", "failure_code"),
    [
        ("primary", "reasoning_primary_result_contract_invalid"),
        ("review", "reasoning_review_result_contract_invalid"),
        ("autonomous-resume", "reasoning_review_result_contract_invalid"),
    ],
)
def test_durable_reasoning_semantic_trace_failure_is_terminal_without_replay(
    tmp_path: Path,
    operation_name: str,
    failure_code: str,
) -> None:
    workspace = tmp_path / f"reasoning-semantic-terminal-{operation_name}"
    executable = _fake_codex(tmp_path / f"codex-{operation_name}")
    base_request = _request()
    primary = _stage_output()
    review = {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "reviewer_agent_ref": "provider-child:1",
        "findings": [],
        "final_output": primary,
        "dispositions": [],
    }
    checkpoint: dict[str, object] | None = None
    creation_result: dict[str, object] | None = None
    if operation_name == "primary":
        outputs = [primary]
        traces = [()]
    elif operation_name == "review":
        outputs = [primary, review]
        traces = [REASONING_ROOT_SEMANTIC_OPERATION_IDS, ()]
    else:
        checkpoint, creation_result, review = _autonomous_resume_fixture(
            base_request
        )
        outputs = [review]
        traces = [()]

    def configured(runner: _SequenceRunner) -> CodexReasoningSkillAdapter:
        adapter = CodexReasoningSkillAdapter(
            workspace,
            executable=str(executable),
            process_runner=runner,
        )
        adapter.bind_full_conformance_authority(_FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        return adapter

    runner = _SequenceRunner(
        outputs,
        observed_operation_ids_by_call=traces,
    )
    adapter = configured(runner)
    request = replace(
        base_request,
        runtime_binding=adapter.runtime_binding(),
        native_session_ref=(
            "provider-session:1"
            if operation_name == "autonomous-resume"
            else None
        ),
        job_ref=f"reasoning-semantic-{operation_name}-job",
    )

    def invoke(current: CodexReasoningSkillAdapter) -> object:
        current_request = replace(
            request,
            runtime_binding=current.runtime_binding(),
        )
        if operation_name == "primary":
            return current.generate_draft(current_request)
        if operation_name == "review":
            return current.execute(current_request)
        assert checkpoint is not None and creation_result is not None
        return current.resume_after_autonomous_creation(
            current_request,
            checkpoint,
            creation_result,
        )

    with pytest.raises(ReasoningSkillUnavailable, match=failure_code) as caught:
        invoke(adapter)
    terminal = caught.value.recovery_checkpoint
    assert terminal is not None
    assert terminal["contract_failure_code"] == failure_code
    assert terminal["contract_failure_detail_code"] == (
        "reasoning_semantic_mcp_currentness_unobserved"
    )

    no_replay = _SequenceRunner([])
    restarted = configured(no_replay)
    with pytest.raises(ReasoningSkillUnavailable, match=failure_code) as replayed:
        invoke(restarted)
    assert replayed.value.recovery_checkpoint == terminal
    assert no_replay.calls == []


def test_restart_reconciles_cancelled_autonomous_resume_operation(
    tmp_path: Path,
) -> None:
    base_request = _request()
    checkpoint, creation_result, review = _autonomous_resume_fixture(
        base_request
    )
    executable = _durable_reasoning_codex(
        tmp_path / "codex-autonomous-resume",
        review,
    )
    workspace = tmp_path / "reasoning-autonomous-resume"

    def configured() -> CodexReasoningSkillAdapter:
        adapter = CodexReasoningSkillAdapter(
            workspace,
            executable=str(executable),
        )
        adapter.bind_full_conformance_authority(_FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        return adapter

    adapter = configured()
    job_ref = "reasoning-autonomous-resume-job"
    request = replace(
        base_request,
        runtime_binding=adapter.runtime_binding(),
        native_session_ref="provider-session:1",
        job_ref=job_ref,
    )

    result = adapter.resume_after_autonomous_creation(
        request,
        checkpoint,
        creation_result,
    )
    assert result.outcome_document() == review["final_output"]
    operation = next(
        workspace.glob("provider-operations/*/autonomous-resume")
    )
    assert (operation / "invocation.json").is_file()
    assert (operation / "exit.json").is_file()
    assert (operation / "completed.json").is_file()

    restarted = configured()
    assert restarted.reconcile_cancelled_job(job_ref) is True
