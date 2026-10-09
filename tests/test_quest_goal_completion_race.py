from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, current_thread
import json

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.quest_goal import (
    EvolutionDecision,
    ResearchRoot,
    WorkDecision,
    WorkHandle,
)
import meta_research.runtime_conditions as runtime_conditions
from test_corrected_quest_initialization import _authenticated_client
from test_public_quest_completion import (
    _CandidateCompletionReasoningSkill,
    _DeterministicDraftingAdapter,
    _DeterministicProbe,
    _NoViableIdeaSkill,
    _accepted_candidate,
    _finish_reasoning_stage,
)


class _AdvisoryIdeaSkill(_NoViableIdeaSkill):
    def review_draft(self, request, draft):
        return replace(
            super().review_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )


class _AdvisoryCompletionReasoningSkill(_CandidateCompletionReasoningSkill):
    def review_draft(self, request, draft):
        return replace(
            super().review_draft(request, draft),
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
        )


def _runtime(path: Path):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_AdvisoryIdeaSkill(),
        reasoning_skill_provider=_AdvisoryCompletionReasoningSkill(),
    )


def _completion_command(runtime, binding, decision):
    candidate = binding["transition"]
    candidate_ref = str(binding["transition_ref"])
    source_outcome_ref = str(decision.outcome_ref)
    runtime.quest_completion.start(
        source_outcome_ref=source_outcome_ref,
        candidate_completion_ref=candidate_ref,
        idempotency_key="goal-race-completion-start",
    )
    assert runtime.quest_completion.process_once()
    awaiting = runtime.quest_completion.query_current()
    preview = awaiting["human_confirmation"]["preview"]
    runtime.owners.human_collaboration.decide_quest_completion(
        preview_ref=str(preview["ref"]),
        preview_hash=str(preview["hash"]),
        decision="confirmed",
        idempotency_key="goal-race-completion-confirm",
    )
    confirmed = runtime.quest_completion.query_current()
    return {
        "context_ref": confirmed["context_ref"],
        "source_outcome_ref": source_outcome_ref,
        "candidate_completion_ref": candidate_ref,
        "candidate_completion_hash": binding["transition_hash"],
        "goal_revision": confirmed["goal_revision"],
        "human_confirmation": confirmed["human_confirmation"]["decision"],
        "idempotency_key": "goal-race-domain-acceptance",
    }, candidate


def _evolution_command(runtime, quest_ref: str, accepted):
    runtime.owners.research_graph.query_quest_goal_view(quest_ref)
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name="goal-race-evidence.txt",
            media_type="text/plain",
            content=b"A newly accepted observation changes the useful direction.\n",
            origin_quest_ref=quest_ref,
        ),
        idempotency_key="goal-race-evidence-intake",
    )
    assert intake.status == "accepted" and intake.asset is not None
    asset = intake.asset
    runtime.owners.research_graph.accept_asset_role(
        binding=asset.as_binding(),
        role="evidence",
        quest_ref=quest_ref,
        idempotency_key="goal-race-evidence-role",
    )
    run = accepted["run"]
    with runtime._database.read() as connection:
        operation = connection.execute(
            text(
                "SELECT operation_ref FROM ar_stage_provider_invocations "
                "WHERE run_ref=:run_ref AND phase='review' "
                "ORDER BY completed_at DESC,invocation_ref DESC LIMIT 1"
            ),
            {"run_ref": run["run_ref"]},
        ).first()
    assert operation is not None
    author = ResearchRoot(
        kind="reasoning",
        run_ref=run["run_ref"],
        attempt_ref=run["attempt_ref"],
        root_session_ref=run["root_session_ref"],
        fence_ref=run["fence_ref"],
        runtime_binding_hash=run["runtime_binding_hash"],
        operation_ref=operation.operation_ref,
    )
    with runtime._database.fenced_write() as connection:
        direction_cut = runtime.owners.research_graph.freeze_quest_direction(
            connection=connection,
            quest_ref=quest_ref,
            author=author,
        )
    conditions = direction_cut["conditions"]
    work = tuple(WorkHandle(**item) for item in direction_cut["work"]["items"])
    assert work and all(item.kind == "stage" for item in work)
    decision = EvolutionDecision(
        expected_revision=direction_cut["goal"]["goal_revision_ref"],
        conditions_basis=conditions["basis_ref"],
        work_basis=direction_cut["work"]["basis_ref"],
        cause={
            "kind": "evidence",
            "evidence": [
                {"source_ref": asset.asset_ref, "version_ref": asset.version_ref}
            ],
        },
        replacement={
            "goal": "Reassess the comparison under the newly accepted observation.",
            "completion_criteria": (
                "The revised comparison resolves the accepted observation."
            ),
        },
        criteria_review="The old completion claim predates material evidence.",
        judgment="The accepted observation changes the current Quest direction.",
        conditions={
            "runtime_conditions_ref": conditions["runtime_conditions"]["revision"],
            "assessments": [
                {
                    "condition_ref": item["condition_ref"],
                    "disposition": "preserved",
                    "explanation": "The sourced condition remains applicable.",
                }
                for item in conditions["enduring"]
            ],
            "newly_identified": [],
        },
        arrangements=tuple(
            WorkDecision(
                kind="finish_stage_boundary",
                work=item,
                reason="Finish the frozen Reasoning boundary before reassessment.",
            )
            for item in work
        ),
        following_direction="A higher Reasoning epoch will reassess the new goal.",
    )
    return {
        "author": author,
        "quest_ref": quest_ref,
        "direction_cut": direction_cut,
        "effect_id": "goal-race-evolution",
        "decision": decision,
    }


@pytest.mark.parametrize("winner", ["completion", "evolution"])
def test_completion_and_evolution_share_one_sqlite_goal_head_fence(
    tmp_path: Path, winner: str
) -> None:
    runtime = _runtime(tmp_path / winner)
    original_fenced_write = runtime._database.fenced_write
    try:
        quest, accepted, reasoning_decision, binding = _accepted_candidate(runtime)
        quest_ref = str(quest["quest_ref"])
        completion, candidate = _completion_command(
            runtime, binding, reasoning_decision
        )
        evolution = _evolution_command(runtime, quest_ref, accepted)
        reached = {name: Event() for name in ("completion", "evolution")}
        allowed = {name: Event() for name in reached}

        @contextmanager
        def ordered_fenced_write():
            name = current_thread().name
            if name in reached:
                reached[name].set()
                assert allowed[name].wait(10)
            with original_fenced_write() as connection:
                yield connection

        runtime._database.fenced_write = ordered_fenced_write
        results: dict[str, object] = {}
        errors: dict[str, BaseException] = {}

        def call(name, operation, values):
            try:
                results[name] = operation(**values)
            except BaseException as error:
                errors[name] = error

        threads = {
            "completion": Thread(
                target=call,
                name="completion",
                args=(
                    "completion",
                    runtime.owners.research_graph.accept_quest_completion,
                    completion,
                ),
            ),
            "evolution": Thread(
                target=call,
                name="evolution",
                args=(
                    "evolution",
                    runtime.owners.research_graph.evolve_quest_goal,
                    evolution,
                ),
            ),
        }
        for thread in threads.values():
            thread.start()
        assert all(event.wait(10) for event in reached.values())
        allowed[winner].set()
        threads[winner].join(10)
        assert not threads[winner].is_alive()
        loser = "evolution" if winner == "completion" else "completion"
        allowed[loser].set()
        threads[loser].join(10)
        assert not threads[loser].is_alive()

        assert set(results) == {winner}
        assert set(errors) == {loser}
        assert isinstance(errors[loser], OwnerConflict)
        assert errors[loser].code == (
            "quest_goal_closed"
            if winner == "completion"
            else "quest_completion_goal_stale"
        )
        current = runtime.owners.research_graph.query_current_quest_goal_revision(
            quest_ref
        )
        accepted_completion = (
            runtime.owners.research_graph.query_quest_completion_acceptance(
                completion["candidate_completion_ref"]
            )
        )
        historical_candidate = (
            runtime.owners.research_graph.query_candidate_completion(
                source_outcome_ref=completion["source_outcome_ref"],
                candidate_completion_ref=completion["candidate_completion_ref"],
            )
        )
        assert historical_candidate is not None
        assert historical_candidate["candidate_completion"] == candidate
        if winner == "completion":
            assert current == completion["goal_revision"]
            assert accepted_completion == results["completion"]
        else:
            assert current == results["evolution"]["goal"]
            assert accepted_completion is None
    finally:
        runtime._database.fenced_write = original_fenced_write
        runtime.close()


def test_goal_evolution_wakes_a_higher_reasoning_epoch_without_rewriting_history(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "reasoning-reassessment")
    try:
        quest, accepted, reasoning_decision, binding = _accepted_candidate(runtime)
        quest_ref = str(quest["quest_ref"])
        committed = _finish_reasoning_stage(runtime)
        old_request = committed["stage_run_request"]
        old_commit = committed["stage_commit"]
        old_foreground = runtime.owners.advancement_engine.query_foreground(
            quest_ref
        )
        assert old_foreground is not None
        assert old_foreground["stage"] == "reasoning"
        assert old_foreground["status"] == "active"
        assert old_foreground["grant_status"] == "active"
        assert old_foreground["epoch"] == old_request["epoch"]

        with runtime._database.read() as connection:
            old_plan_rows = connection.execute(
                text(
                    "SELECT request_ref,epoch,context_pack_hash FROM "
                    "ae_stage_run_requests WHERE cycle_ref=:cycle_ref AND "
                    "stage='plan' ORDER BY epoch,request_ref"
                ),
                {"cycle_ref": old_foreground["cycle_ref"]},
            ).all()

        evolution = _evolution_command(runtime, quest_ref, accepted)
        evolved = runtime.owners.research_graph.evolve_quest_goal(**evolution)
        new_goal_ref = evolved["goal"]["goal_revision_ref"]

        historical_candidate = (
            runtime.owners.research_graph.query_candidate_completion(
                source_outcome_ref=str(reasoning_decision.outcome_ref),
                candidate_completion_ref=str(binding["transition_ref"]),
            )
        )
        assert historical_candidate is not None
        assert old_request["context_pack"]["research_context"][
            "goal_revision_ref"
        ] != new_goal_ref

        reassessment = (
            runtime.owners.advancement_engine.query_goal_reasoning_reassessment()
        )
        assert reassessment == {
            "quest_ref": quest_ref,
            "cycle_ref": old_foreground["cycle_ref"],
            "source_epoch": old_foreground["epoch"],
            "source_request_ref": old_request["request_ref"],
            "source_commit_ref": old_commit["commit_ref"],
            "goal_revision_ref": new_goal_ref,
        }

        assert runtime.goal_work_reconciler.process_once()
        new_foreground = runtime.owners.advancement_engine.query_foreground(
            quest_ref
        )
        assert new_foreground is not None
        assert new_foreground["cycle_ref"] == old_foreground["cycle_ref"]
        assert new_foreground["question_ref"] == old_foreground["question_ref"]
        assert new_foreground["stage"] == "reasoning"
        assert new_foreground["status"] == "active"
        assert new_foreground["grant_status"] == "active"
        assert new_foreground["epoch"] == old_foreground["epoch"] + 1
        assert (
            runtime.owners.advancement_engine.query_reasoning_stage_request(
                str(new_foreground["cycle_ref"])
            )
            is None
        )

        replayed = (
            runtime.owners.advancement_engine.ensure_goal_reasoning_reassessment(
                quest_ref=quest_ref,
                goal_revision_ref=str(new_goal_ref),
                idempotency_key=(
                    "goal-reasoning-reassessment-" + str(new_goal_ref)
                    + ":" + str(old_foreground["epoch"])
                ),
            )
        )
        assert replayed["replayed"] is True
        assert replayed["grant_ref"] == new_foreground["grant_ref"]
        assert replayed["source_epoch"] == old_foreground["epoch"]
        assert replayed["epoch"] == new_foreground["epoch"]

        assert runtime.reasoning_stage.process_once()
        new_request = (
            runtime.owners.advancement_engine.query_reasoning_stage_request(
                str(new_foreground["cycle_ref"])
            )
        )
        assert new_request is not None
        assert new_request.epoch == new_foreground["epoch"]
        assert new_request.request_ref != old_request["request_ref"]
        assert new_request.context_pack["research_context"][
            "goal_revision_ref"
        ] == new_goal_ref
        assert (
            runtime.owners.advancement_engine.query_reasoning_stage_commit(
                str(old_request["request_ref"])
            ).commit_ref
            == old_commit["commit_ref"]
        )
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT request_ref,epoch,context_pack_hash FROM "
                    "ae_stage_run_requests WHERE cycle_ref=:cycle_ref AND "
                    "stage='plan' ORDER BY epoch,request_ref"
                ),
                {"cycle_ref": old_foreground["cycle_ref"]},
            ).all() == old_plan_rows
    finally:
        runtime.close()


def test_public_condition_edit_keeps_candidate_frozen_and_reopens_reasoning(tmp_path):
    runtime = _runtime(tmp_path / "condition-reassessment")
    try:
        quest, accepted, decision, transition = _accepted_candidate(runtime)
        quest_ref = str(quest["quest_ref"])
        candidate_ref = str(transition["transition_ref"])
        graph = runtime.owners.research_graph
        historical = graph.query_candidate_completion(
            source_outcome_ref=decision.outcome_ref,
            candidate_completion_ref=candidate_ref,
        )
        source = historical["source"]
        initial = runtime_conditions.read_runtime_conditions(runtime.data_root.root, quest_ref)
        assert source["runtime_conditions_revision"] == initial["revision"]
        with runtime._database.read() as connection:
            snapshot = connection.execute(text(
                "SELECT * FROM hc_guidance_snapshots WHERE snapshot_ref=:ref"
            ), {"ref": source["conditions_snapshot_ref"]}).one()
            frozen = json.loads(snapshot.snapshot_json)
            assert frozen["operation"]["operation_name"] == "review"
            assert frozen["identity"]["run_ref"] == accepted["run"]["run_ref"]
            assert frozen["direction_cut"]["conditions"]["runtime_conditions"] == initial
            assert snapshot.snapshot_hash == source["conditions_snapshot_hash"]
        committed = _finish_reasoning_stage(runtime)
        old_epoch = committed["stage_run_request"]["epoch"]
        old_commit = committed["stage_commit"]["commit_ref"]
        client, headers = _authenticated_client(runtime)
        edited = client.put(
            f"/api/v1/quests/{quest_ref}/runtime-conditions",
            json={"text": "The completion must satisfy the revised resource condition.",
                  "expected_revision": initial["revision"]},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] != initial["revision"]
        assert graph.query_candidate_completion(
            source_outcome_ref=decision.outcome_ref,
            candidate_completion_ref=candidate_ref,
        ) == historical
        with pytest.raises(OwnerConflict) as stale:
            runtime.quest_completion.start(
                source_outcome_ref=decision.outcome_ref,
                candidate_completion_ref=candidate_ref,
                idempotency_key="condition-edited-before-completion-start",
            )
        assert stale.value.code == "candidate_completion_stale"
        assert runtime.goal_work_reconciler.process_once()
        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        assert foreground["stage"] == "reasoning"
        assert foreground["epoch"] == old_epoch + 1
        assert runtime.owners.advancement_engine.query_reasoning_stage_commit(
            committed["stage_run_request"]["request_ref"]
        ).commit_ref == old_commit
        assert graph.query_candidate_completion(
            source_outcome_ref=decision.outcome_ref,
            candidate_completion_ref=candidate_ref,
        ) == historical
    finally:
        runtime.close()


@pytest.mark.parametrize("winner", ["completion", "conditions"])
def test_completion_and_public_condition_edit_use_the_final_sqlite_writer_order(
    tmp_path, monkeypatch, winner,
):
    runtime = _runtime(tmp_path / winner)
    original_fenced_write = runtime._database.fenced_write
    original_condition_lock = runtime_conditions._LOCK
    reached = {name: Event() for name in ("completion", "conditions")}
    allowed = {name: Event() for name in reached}
    threads = {}
    try:
        quest, accepted, decision, transition = _accepted_candidate(runtime)
        quest_ref = str(quest["quest_ref"])
        command, candidate = _completion_command(runtime, transition, decision)
        source = runtime.quest_completion.query_current()["source"]
        preview = runtime.quest_completion.query_current()["human_confirmation"]["preview"]
        assert preview["runtime_conditions_revision"] == source["runtime_conditions_revision"]
        assert preview["conditions_snapshot_ref"] == source["conditions_snapshot_ref"]
        client, headers = _authenticated_client(runtime)

        @contextmanager
        def ordered_fenced_write():
            if current_thread().name == "completion":
                reached["completion"].set()
                assert allowed["completion"].wait(10)
            with original_fenced_write() as connection:
                yield connection

        @contextmanager
        def ordered_condition_write():
            reached["conditions"].set()
            assert allowed["conditions"].wait(10)
            with original_condition_lock:
                yield

        runtime._database.fenced_write = ordered_fenced_write
        monkeypatch.setattr(runtime_conditions, "_LOCK", ordered_condition_write())
        results = {}
        errors = {}

        def call(name, operation):
            try:
                results[name] = operation()
            except BaseException as error:
                errors[name] = error

        threads = {
            "completion": Thread(target=call, name="completion", args=(
                "completion", lambda: runtime.owners.research_graph.accept_quest_completion(**command),
            )),
            "conditions": Thread(target=call, name="conditions", args=(
                "conditions", lambda: client.put(
                    f"/api/v1/quests/{quest_ref}/runtime-conditions",
                    json={"text": "Require the newly revised resource condition before completion.",
                          "expected_revision": source["runtime_conditions_revision"]},
                    headers=headers,
                ),
            )),
        }
        for thread in threads.values():
            thread.start()
        assert all(event.wait(10) for event in reached.values())
        allowed[winner].set()
        threads[winner].join(10)
        assert not threads[winner].is_alive()
        loser = "conditions" if winner == "completion" else "completion"
        allowed[loser].set()
        threads[loser].join(10)
        assert not threads[loser].is_alive()
        assert results["conditions"].status_code == 200, results["conditions"].text
        assert results["conditions"].json()["revision"] != source["runtime_conditions_revision"]
        historical = runtime.owners.research_graph.query_candidate_completion(
            source_outcome_ref=decision.outcome_ref,
            candidate_completion_ref=command["candidate_completion_ref"],
        )
        assert historical["candidate_completion"] == candidate
        assert historical["source"] == source
        acceptance = runtime.owners.research_graph.query_quest_completion_acceptance(
            command["candidate_completion_ref"]
        )
        if winner == "conditions":
            assert set(errors) == {"completion"}
            assert isinstance(errors["completion"], OwnerConflict)
            assert errors["completion"].code == "quest_completion_conditions_stale"
            assert acceptance is None
            assert runtime.quest_completion.query_current()["status"] == "stale"
            assert not runtime.quest_completion.process_once()
        else:
            assert errors == {}
            assert acceptance == results["completion"]
            assert runtime.owners.research_graph.accept_quest_completion(**command) == acceptance
        with runtime._database.read() as connection:
            head = connection.execute(text(
                "SELECT status FROM rg_quest_goal_heads WHERE quest_ref=:ref"
            ), {"ref": quest_ref}).scalar_one()
        assert head == ("open" if winner == "conditions" else "completion_committed")
    finally:
        for event in allowed.values():
            event.set()
        for thread in threads.values():
            thread.join(10)
        runtime._database.fenced_write = original_fenced_write
        monkeypatch.setattr(runtime_conditions, "_LOCK", original_condition_lock)
        runtime.close()
