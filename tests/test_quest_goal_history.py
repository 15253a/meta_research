from __future__ import annotations

from pathlib import Path

from meta_research.runtime_conditions import (
    read_runtime_conditions,
    save_runtime_conditions,
)
from test_corrected_quest_initialization import _authenticated_client
from test_human_guidance_providers import _submit, _tool
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)


def test_public_goal_history_preserves_conditions_and_supersession_review(
    tmp_path: Path,
) -> None:
    runtime = _current_bundle_runtime(tmp_path / "history")
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        target, _, _, admission, _ = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(
            target.target_ref
        )
        quest_ref = launch.quest_ref
        graph = runtime.owners.research_graph
        original = graph.query_current_quest_goal_revision(quest_ref)
        submitted = [_submit(runtime, quest_ref, strength=5)]
        observed = []
        adapter = runtime.harnesses._adapters["codex"]
        invoke = adapter.invoke

        def evolve_with_sourced_conditions(invocation):
            if invocation.root_kind != "target" or len(observed) == len(submitted):
                return invoke(invocation)
            inbox = _tool(runtime, invocation.mcp_token, "human_guidance.read")
            delivery = next(
                item
                for item in inbox["deliveries"]
                if item["guide"]["constraint_ref"]
                == submitted[-1]["constraint_ref"]
            )
            exact = _tool(
                runtime,
                invocation.mcp_token,
                "human_guidance.read",
                delivery_ref=delivery["delivery_ref"],
                effect_id=f"history-read-{len(observed)}",
            )
            cut = _tool(
                runtime, invocation.mcp_token, "research_graph.quest_goal.read"
            )["operation_basis"]
            assessments = [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "human_superseded",
                    "superseding_delivery_ref": delivery["delivery_ref"],
                    "explanation": "The newer human direction removes the audit condition.",
                }
                for item in cut["conditions"]["enduring"]
            ]
            clauses = (
                [
                    {
                        "delivery_ref": delivery["delivery_ref"],
                        "start": 0,
                        "end": len(exact["original_text"]),
                        "text": exact["original_text"],
                        "meaning": "Keep an independent device audit throughout the Quest.",
                    }
                ]
                if not observed
                else []
            )
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
                    "goal": "Compare calibration using the current human direction.",
                    "completion_criteria": (
                        "The calibration evidence satisfies the current sourced conditions."
                    ),
                },
                "criteria_review": "Review the criteria together with the changed condition.",
                "judgment": exact["original_text"],
                "conditions": {
                    "runtime_conditions_ref": cut["conditions"][
                        "runtime_conditions"
                    ]["revision"],
                    "assessments": assessments,
                    "newly_identified": clauses,
                },
                "arrangements": {
                    "decisions": [
                        {
                            "kind": "continue",
                            "work": item,
                            "reason": "The accepted work remains useful under its frozen contract.",
                        }
                        for item in cut["work"]["items"]
                    ]
                },
                "following_direction": "Continue the useful comparison under the new direction.",
            }
            result = _tool(
                runtime,
                invocation.mcp_token,
                "research_graph.quest_goal.evolve",
                effect_id=f"history-evolution-{len(observed)}",
                decision=decision,
            )
            observed.append({"cut": cut, "decision": decision, "result": result})
            return invoke(invocation)

        adapter.invoke = evolve_with_sourced_conditions
        first = runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref,
            prompt="Apply the device audit direction to the Quest.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert first.status == "executed"
        initial_runtime = observed[0]["cut"]["conditions"]["runtime_conditions"]
        updated_runtime = save_runtime_conditions(
            runtime.data_root.root,
            quest_ref,
            text="Use the revised human time and resource conditions.",
            expected_revision=initial_runtime["revision"],
        )
        submitted.append(
            runtime.owners.human_collaboration.submit_human_guidance(
                quest_ref=quest_ref,
                original_text="The independent device audit is no longer required.",
                strength=5,
                idempotency_key="supersede-history-condition",
            )
        )
        second = runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref,
            prompt="Review the newer direction and its condition change.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        assert second.status == "executed"
        latest_runtime = save_runtime_conditions(
            runtime.data_root.root,
            quest_ref,
            text="A later edit must not alter either accepted historical snapshot.",
            expected_revision=updated_runtime["revision"],
        )
        client, _ = _authenticated_client(runtime)
        history_url = f"/api/v1/quests/{quest_ref}/goal/history"
        response = client.get(history_url)
        assert response.status_code == 200, response.text
        history = response.json()
        assert history == graph.query_quest_goal_history(quest_ref)
        initial, earlier, current = history["items"]
        assert initial == {**original, "conditions": None, "conditions_review": None}
        assert earlier["conditions"]["runtime_conditions"] == initial_runtime
        condition, = earlier["conditions"]["enduring"]
        assert condition["source_text"] == "Keep the device audit."
        assert condition["status"] == "active"
        assert condition["source"]["delivery_ref"] == observed[0]["decision"][
            "cause"
        ]["delivery_ref"]
        assert earlier["conditions_review"] == observed[0]["decision"]["conditions"]
        assert current["conditions"]["runtime_conditions"] == updated_runtime
        assert current["conditions"]["enduring"] == []
        assert current["conditions_review"] == observed[1]["decision"]["conditions"]
        assessment, = current["conditions_review"]["assessments"]
        assert assessment["condition_ref"] == condition["condition_ref"]
        assert assessment["disposition"] == "human_superseded"
        assert assessment["superseding_delivery_ref"] == current["cause"]["delivery_ref"]
        assert read_runtime_conditions(runtime.data_root.root, quest_ref) == latest_runtime
        for detail, operation in zip((earlier, current), observed, strict=True):
            revision_ref = detail["goal_revision_ref"]
            raw = graph.query_quest_goal_revision(revision_ref)
            assert raw == operation["result"]["goal"]
            assert {key: detail[key] for key in raw} == raw
            exact = client.get(
                f"/api/v1/quests/{quest_ref}/goal/revisions/{revision_ref}"
            )
            assert exact.status_code == 200, exact.text
            assert exact.json() == detail
        page = client.get(history_url, params={"offset": 1, "limit": 1}).json()
        assert page == {"items": [earlier], "offset": 1, "next_offset": 2}
        assert graph.query_quest_goal_revision_detail("quest_goal_revision_absent") is None
    finally:
        runtime.close()
