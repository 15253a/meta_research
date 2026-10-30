"""Shared research tools do not grant helpers authority to evolve a Quest goal."""
from __future__ import annotations

from dataclasses import asdict

import pytest
from jsonschema import Draft202012Validator

from meta_research.composition import build_production_runtime
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from meta_research.quest_goal import (
    GOAL_OPERATION_IDS,
    ResearchRoot,
    evolution_input_schema,
    parse_evolution_decision,
)
from meta_research.runtime_conditions import read_runtime_conditions
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_public_human_collaboration_ladder import _CompanionRuntimeBinding
from test_public_idea_stage import _DeterministicIdeaSkill
from test_public_writing_report import (
    _DeterministicDraftingAdapter,
    _DeterministicProbe,
    _DeterministicWritingSkill,
    _admit_report,
    _confirm_direct_quest,
)
from test_research_datasets import _asset


class _CompanionWithGrantProbe(_DeterministicDraftingAdapter):
    def __init__(self):
        self.probe = None

    def runtime_binding(self):
        return _CompanionRuntimeBinding()

    def reply(self, request):
        assert self.probe is not None
        # HC has registered this exact running Companion root before invoking us.
        assert request.root_runtime_scope is not None
        self.probe(request.root_runtime_scope)
        return super().reply(request)


def _goal_arguments(runtime, quest_ref):
    """Use a real admitted work cut and accepted evidence, without running it."""
    admitted = runtime.idea_stage.start("helper-grant-idea-admission")
    run = runtime.owners.agent_runtime.query_idea_stage_run(
        admitted["stage_run_request"]["request_ref"]
    )
    assert run is not None
    author = ResearchRoot(
        kind="idea", run_ref=run.run_ref, attempt_ref=run.attempt_ref,
        root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
        runtime_binding_hash=run.runtime_binding_hash,
        operation_ref=run.primary_invocation.operation_ref,
    )
    graph = runtime.owners.research_graph
    evidence = _asset(runtime, key="helper-grant-evidence")
    graph.accept_asset_role(
        binding=evidence, role="evidence", quest_ref=quest_ref,
        idempotency_key="helper-grant-evidence-role",
    )
    read_runtime_conditions(runtime.data_root.root, quest_ref)
    with runtime._database.read() as connection:
        cut = graph.freeze_quest_direction(
            connection=connection, quest_ref=quest_ref, author=author,
        )
    assert cut["work"]["items"]
    conditions = cut["conditions"]
    arguments = {
        "effect_id": "helper-must-not-change-goal",
        "decision": {
            "expected_revision": cut["goal"]["goal_revision_ref"],
            "conditions_basis": conditions["basis_ref"],
            "work_basis": cut["work"]["basis_ref"],
            "cause": {"kind": "evidence", "evidence": [{
                "source_ref": evidence.asset_ref, "version_ref": evidence.version_ref,
            }]},
            "replacement": {
                "goal": "Reassess the accepted observation.",
                "completion_criteria": "Explain the accepted observation reproducibly.",
            },
            "criteria_review": "Review the whole Quest criteria with the observation.",
            "judgment": "The accepted observation suggests a revised comparison.",
            "conditions": {
                "runtime_conditions_ref": conditions["runtime_conditions"]["revision"],
                "assessments": [{
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": "The sourced condition remains applicable.",
                } for item in conditions["enduring"]],
                "newly_identified": [],
            },
            "arrangements": {"decisions": [{
                "kind": "continue", "work": item,
                "reason": "The admitted work retains its frozen contract.",
            } for item in cut["work"]["items"]]},
            "following_direction": "Continue the useful comparison under the revised goal.",
        },
    }
    Draft202012Validator(evolution_input_schema()).validate(arguments)
    assert parse_evolution_decision(arguments["decision"]).as_dict() == arguments["decision"]
    return arguments


def _assert_role_denies_goal_writes(runtime, root_kind, scope, arguments):
    channel = runtime.harnesses.issue_resident_mcp_channel(
        run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"],
        operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind],
        root_kind=root_kind, phase=f"{root_kind}-primary",
        subject_policy="operation_tree",
    )
    token = channel.connection.token

    def dispatch(method, params=None):
        message = {"jsonrpc": "2.0", "id": "helper-authority", "method": method}
        if params is not None:
            message["params"] = params
        status, response = runtime.harnesses.dispatch_mcp(token, message)
        assert status == 200, response
        assert response["id"] == "helper-authority"
        return response["result"]

    try:
        tools = dispatch("tools/list")["tools"]
        names = {tool["name"] for tool in tools}
        assert names == set(ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind])
        assert "research_graph.questions.page" in names
        # The operation exists in this production gateway; its absence from the
        # real helper grant, rather than a nonexistent tool, must cause denial.
        assert set(GOAL_OPERATION_IDS) <= set(runtime.harnesses._gateway.operation_ids)
        readable = dispatch("tools/call", {
            "name": "research_graph.questions.page", "arguments": {},
        })
        assert readable["isError"] is False, readable
        assert readable["structuredContent"]["items"]
        for operation in GOAL_OPERATION_IDS[1:]:
            assert operation not in names
            denied = dispatch("tools/call", {
                "name": operation, "arguments": arguments,
            })
            assert denied["isError"] is True, denied
            assert denied["structuredContent"] == {"code": "capability_unavailable"}
    finally:
        runtime.harnesses.revoke_resident_mcp_channel(channel)


@pytest.mark.parametrize("root_kind", ("companion", "writing"))
def test_current_helper_grant_rejects_formal_goal_evolution(tmp_path, root_kind):
    companion = _CompanionWithGrantProbe()
    idea = _DeterministicIdeaSkill()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / root_kind),
        proposal_drafter=companion, intent_drafting_provider=companion,
        host_compute_probe=_DeterministicProbe(), idea_skill_provider=idea,
        writing_skill_provider=_DeterministicWritingSkill(),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False, startup_harness_diagnostics=False,
    )
    observed = []
    try:
        quest = _confirm_direct_quest(runtime)
        quest_ref = quest["quest_ref"]
        arguments = _goal_arguments(runtime, quest_ref)
        graph = runtime.owners.research_graph
        original = graph.query_current_quest_goal_revision(quest_ref)
        original_hash = canonical_hash(original)
        history = graph.query_quest_goal_history(quest_ref)

        def probe(scope):
            _assert_role_denies_goal_writes(runtime, root_kind, scope, arguments)
            observed.append(scope["run_ref"])

        if root_kind == "companion":
            companion.probe = probe
            human = runtime.owners.human_collaboration
            queued = human.send_companion_message(
                f"quest:{quest_ref}", "Reconsider the current goal.", "helper-grant-message",
            )
            assert human.process_drafting_once()
            assert observed == [queued["interaction_ref"]]
            [turn] = human.query_companion(f"quest:{quest_ref}")["turns"]
            assert turn["assistant_status"] == "completed"
        else:
            admitted = _admit_report(runtime, quest_ref, "helper-grant-writing")
            run = runtime.owners.agent_runtime.query_writing_report(admitted["run"]["run_ref"])
            assert run is not None
            probe(asdict(run))
            assert observed == [run.run_ref]

        assert graph.query_current_quest_goal_revision(quest_ref) == original
        assert canonical_hash(graph.query_current_quest_goal_revision(quest_ref)) == original_hash
        assert graph.query_quest_goal_history(quest_ref) == history
        assert idea.requests == []  # Only admission was needed; no model work ran.
    finally:
        runtime.close()
