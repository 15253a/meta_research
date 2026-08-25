from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.harness import FullConformanceBinding, ResidentMcpChannel
from meta_research.owners.agent_runtime import ReasoningRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.reasoning_contract import (
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
    CodexReasoningSkillAdapter,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
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
    ) -> None:
        self._outputs = iter(outputs)
        self._observed_operation_ids = observed_operation_ids
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
        output_path.write_text(json.dumps(output), encoding="utf-8")
        self.calls.append((argv, prompt, schema))
        self.environments.append(environment)
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": "provider-session:1"}
        ]
        for operation_id in self._observed_operation_ids:
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


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-reasoning-test 1\\n'\n")
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
        model_ref="test-model",
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(_request(), runtime_binding=adapter.runtime_binding())

    result = adapter.execute(request)

    assert result.outcome_document() == primary
    assert result.primary_session_ref == "provider-session:1"
    assert result.reviewer_agent_ref == "provider-child:1"
    assert len(runner.calls) == 2
    assert runner.calls[1][0][-3:] == ["resume", "provider-session:1", "-"]
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
    primary_properties = primary_schema["properties"]
    assert isinstance(primary_properties, dict)
    scientific_schema = primary_properties["scientific_outcome"]
    assert isinstance(scientific_schema, dict)
    scientific_properties = scientific_schema["properties"]
    assert isinstance(scientific_properties, dict)
    evidence_schema = scientific_properties["evidence"]
    assert isinstance(evidence_schema, dict)
    evidence_item = evidence_schema["items"]
    assert isinstance(evidence_item, dict)
    evidence_variants = evidence_item["anyOf"]
    assert isinstance(evidence_variants, list)
    assert len(evidence_variants) == 2
    properties_by_kind = {
        variant["properties"]["kind"]["const"]: variant["properties"]
        for variant in evidence_variants
    }
    evidence_properties = properties_by_kind["LiteratureRecord"]
    assert isinstance(evidence_properties, dict)
    assert evidence_properties["kind"] == {
        "const": "LiteratureRecord",
    }
    assert evidence_properties["ref"] == {
        "const": "literature-record:1",
    }
    assert properties_by_kind["LogAsset"]["finding"] == {"const": "context"}
    candidate_schema = primary_properties["candidate_completion"]
    assert isinstance(candidate_schema, dict)
    candidate_variants = candidate_schema["anyOf"]
    assert isinstance(candidate_variants, list)
    completion_schema = candidate_variants[0]
    assert isinstance(completion_schema, dict)
    completion_properties = completion_schema["properties"]
    assert isinstance(completion_properties, dict)
    assert completion_properties["completion_milestone_basis_refs"] == {
        "const": [
            "stage-commit:idea",
            "stage-commit:plan",
            "stage-commit:bundle",
        ]
    }
