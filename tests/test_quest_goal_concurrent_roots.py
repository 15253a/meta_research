from __future__ import annotations

from copy import deepcopy
from threading import Barrier, Event, Thread

from test_human_guidance_providers import _submit, _tool
from test_public_bundle_stage import _ParallelTwoTargetBundleSkill
from test_target_root_finalizer import (
    _CurrentBindingBundleSkill,
    _admit_independent_target_root,
    _current_bundle_runtime,
)


class _ParallelBundle(_CurrentBindingBundleSkill, _ParallelTwoTargetBundleSkill):
    pass


def _two_admitted_roots(runtime):
    first_target, _, _, first, _ = _admit_independent_target_root(runtime)
    for _ in range(12):
        runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        )
        second_decision = next(
            (
                item
                for item in decisions
                if item.selected_target_ref != first_target.target_ref
            ),
            None,
        )
        if second_decision is None:
            continue
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        second_target = next(
            item
            for item in graph.targets
            if item.target_ref == second_decision.selected_target_ref
        )
        if runtime.owners.research_graph.query_target_candidate_projection(
            target_ref=second_target.target_ref
        ) is not None:
            break
    else:
        raise AssertionError("The second independent Target was not dispatchable")
    _, _, _, second, _ = _admit_independent_target_root(
        runtime,
        ready=(graph, second_target, run, second_decision, None),
        key_prefix="concurrent-second-",
    )
    quest_ref = runtime.owners.agent_runtime.query_admitted_target_launch(
        first_target.target_ref
    ).quest_ref
    return quest_ref, (first, second), {
        first_target.target_ref,
        second_target.target_ref,
    }


def _decision(runtime, invocation, *, goal):
    inbox = _tool(runtime, invocation.mcp_token, "human_guidance.read")
    delivery, = inbox["deliveries"]
    exact = _tool(
        runtime,
        invocation.mcp_token,
        "human_guidance.read",
        delivery_ref=delivery["delivery_ref"],
        effect_id="read-concurrent-direction",
    )
    cut = _tool(
        runtime, invocation.mcp_token, "research_graph.quest_goal.read"
    )["operation_basis"]
    decision = {
        "expected_revision": cut["goal"]["goal_revision_ref"],
        "conditions_basis": cut["conditions"]["basis_ref"],
        "work_basis": cut["work"]["basis_ref"],
        "cause": {
            "kind": "human_guidance",
            "delivery_ref": delivery["delivery_ref"],
            "guide_ref": exact["guide_ref"],
        },
        "replacement": {
            "goal": goal,
            "completion_criteria": "Report the reproducible device audit and calibration comparison.",
        },
        "criteria_review": "Review the whole-Quest criteria with the device audit direction.",
        "judgment": "Both admitted comparisons remain useful under their frozen contracts.",
        "conditions": {
            "runtime_conditions_ref": cut["conditions"]["runtime_conditions"][
                "revision"
            ],
            "assessments": [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": "The sourced condition remains applicable.",
                }
                for item in cut["conditions"]["enduring"]
            ],
            "newly_identified": [],
        },
        "arrangements": {
            "decisions": [
                {
                    "kind": "continue",
                    "work": item,
                    "reason": "The admitted comparison remains useful for the device audit.",
                }
                for item in cut["work"]["items"]
            ]
        },
        "following_direction": "Continue both useful comparisons while auditing devices.",
    }
    return cut, decision


def _evolve_result(runtime, token, *, decision, reconcile=False):
    operation = "research_graph.quest_goal.evolve"
    if reconcile:
        operation += ".reconcile"
    status, response, _ = runtime.harnesses.dispatch_mcp_http(
        token,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": operation,
                "arguments": {"effect_id": "evolve-device-audit", "decision": decision},
            },
        },
        mcp_session_id=None,
    )
    assert status == 200, response
    return response["result"]


def test_two_authenticated_roots_conflict_then_replay_exact_effect_after_head_moves(
    tmp_path,
):
    runtime = _current_bundle_runtime(
        tmp_path / "concurrent-roots", bundle_skill=_ParallelBundle()
    )
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    both_frozen = Barrier(2)
    threads = {}
    releases = {}
    completed = {}
    observed = {}
    errors = {}
    turns = {}
    try:
        quest_ref, admissions, target_refs = _two_admitted_roots(runtime)
        _submit(runtime, quest_ref, strength=5)
        original_goal = runtime.owners.research_graph.query_current_quest_goal_revision(
            quest_ref
        )
        adapter = runtime.harnesses._adapters["codex"]
        original_invoke = adapter.invoke
        releases = {item.run.run_ref: Event() for item in admissions}
        completed = {item.run.run_ref: Event() for item in admissions}

        def invoke(invocation):
            if invocation.root_kind != "target":
                return original_invoke(invocation)
            run_ref = invocation.run_ref
            if run_ref in observed:
                cut, decision = _decision(
                    runtime,
                    invocation,
                    goal="Refine the device audit using both calibration comparisons.",
                )
                result = _evolve_result(
                    runtime, invocation.mcp_token, decision=decision
                )
                assert not result.get("isError"), result
                observed[run_ref]["successor"] = {
                    "cut": cut,
                    "result": result["structuredContent"],
                    "operation_ref": invocation.provider_operation_ref,
                }
                return original_invoke(invocation)
            cut, decision = _decision(
                runtime,
                invocation,
                goal=f"Audit devices across the comparison owned by {run_ref}.",
            )
            observed[run_ref] = {
                "cut": cut,
                "decision": decision,
                "token": invocation.mcp_token,
                "operation_ref": invocation.provider_operation_ref,
            }
            both_frozen.wait(30)
            observed[run_ref]["result"] = _evolve_result(
                runtime, invocation.mcp_token, decision=decision
            )
            completed[run_ref].set()
            assert releases[run_ref].wait(30)
            return original_invoke(invocation)

        def run(admission):
            try:
                turns[admission.run.run_ref] = (
                    runtime.harnesses.run_or_resume_target_root(
                        admission.run.request_ref,
                        prompt="Reassess the Quest direction from this independent Target.",
                        mcp_base_url="http://127.0.0.1:8999",
                    )
                )
            except BaseException as error:
                errors[admission.run.run_ref] = error
                completed[admission.run.run_ref].set()

        adapter.invoke = invoke
        threads = {
            item.run.run_ref: Thread(target=run, args=(item,))
            for item in admissions
        }
        for thread in threads.values():
            thread.start()
        assert all(event.wait(30) for event in completed.values()), errors
        assert errors == {}
        assert len({item.run.root_session_ref for item in admissions}) == 2
        assert all(
            runtime.owners.agent_runtime.harness_runs.query_run(
                item.run.request_ref
            ).status == "running"
            for item in admissions
        )
        assert len({item["token"] for item in observed.values()}) == 2
        assert len({item["operation_ref"] for item in observed.values()}) == 2
        first_cut, second_cut = (item["cut"] for item in observed.values())
        assert first_cut == second_cut
        assert first_cut["goal"] == original_goal
        assert {item["target_ref"] for item in first_cut["work"]["items"]} == target_refs
        winners = [
            run_ref
            for run_ref, item in observed.items()
            if not item["result"].get("isError")
        ]
        assert len(winners) == 1, observed
        winner, = winners
        loser, = set(observed) - {winner}
        conflict = observed[loser]["result"]
        assert conflict["isError"] is True
        assert conflict["structuredContent"] == {
            "status": "capability_unavailable",
            "code": "goal_evolution_basis_stale",
        }
        assert conflict["content"] == [
            {"type": "text", "text": "goal_evolution_basis_stale"}
        ]
        accepted = observed[winner]["result"]["structuredContent"]
        assert accepted["status"] == "accepted"
        assert accepted["replayed"] is False
        assert accepted["goal"]["sequence"] == 1
        assert accepted["goal"]["parent_ref"] == original_goal["goal_revision_ref"]
        assert accepted["goal"]["author"]["run_ref"] == winner
        assert accepted["goal"]["author"]["operation_ref"] == observed[winner][
            "operation_ref"
        ]
        history = runtime.owners.research_graph.query_quest_goal_history(quest_ref)
        assert [item["goal_revision_ref"] for item in history["items"]] == [
            original_goal["goal_revision_ref"],
            accepted["goal"]["goal_revision_ref"],
        ]
        readable = _tool(
            runtime, observed[loser]["token"], "research_graph.quest_goal.read"
        )
        assert readable["current"] == accepted["goal"]
        assert readable["operation_basis"] == first_cut
        releases[loser].set()
        threads[loser].join(30)
        assert not threads[loser].is_alive()
        assert errors == {}
        assert turns[loser].status == "executed"
        loser_admission = next(item for item in admissions if item.run.run_ref == loser)
        successor_turn = runtime.harnesses.run_or_resume_target_root(
            loser_admission.run.request_ref,
            prompt="Refine the accepted direction with a new operation-local cut.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert successor_turn.status == "executed"
        successor = observed[loser]["successor"]
        assert successor["operation_ref"] != observed[loser]["operation_ref"]
        assert successor["cut"]["goal"] == accepted["goal"]
        assert successor["result"]["goal"]["sequence"] == 2
        assert successor["result"]["goal"]["parent_ref"] == accepted["goal"][
            "goal_revision_ref"
        ]
        for reconcile in (False, True):
            replay = _evolve_result(
                runtime,
                observed[winner]["token"],
                decision=observed[winner]["decision"],
                reconcile=reconcile,
            )
            assert not replay.get("isError"), replay
            assert replay["structuredContent"] == {**accepted, "replayed": True}
            changed = deepcopy(observed[winner]["decision"])
            changed["replacement"]["completion_criteria"] = "Accept an unreviewed replacement."
            rejected = _evolve_result(
                runtime,
                observed[winner]["token"],
                decision=changed,
                reconcile=reconcile,
            )
            assert rejected["isError"] is True
            assert rejected["structuredContent"]["code"] == (
                "goal_evolution_effect_conflict"
            )
        latest = _tool(
            runtime, observed[winner]["token"], "research_graph.quest_goal.read"
        )
        assert latest["current"] == successor["result"]["goal"]
        assert latest["operation_basis"] == first_cut
        history = runtime.owners.research_graph.query_quest_goal_history(quest_ref)
        assert [item["goal_revision_ref"] for item in history["items"]] == [
            original_goal["goal_revision_ref"],
            accepted["goal"]["goal_revision_ref"],
            successor["result"]["goal"]["goal_revision_ref"],
        ]
        releases[winner].set()
        threads[winner].join(30)
        assert not threads[winner].is_alive()
        assert errors == {}
        assert turns[winner].status == "executed"
    finally:
        both_frozen.abort()
        for release in releases.values():
            release.set()
        for thread in threads.values():
            thread.join(30)
        runtime.close()
