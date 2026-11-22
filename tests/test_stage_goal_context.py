"""Quest Goal is frozen at Stage admission and delivered as science input."""

from contextlib import contextmanager
import pytest
from sqlalchemy import create_engine, text

from meta_research.context_presentation import stage_context_view
from meta_research.owners.advancement_engine import SQLiteAdvancementEngine
from meta_research.owners.common import OwnerConflict


GOAL = {
    "kind": "QuestGoalRevision",
    "quest_ref": "quest:1",
    "goal_revision_ref": "quest_goal_revision:1",
    "goal": {"statement": "保留可复核的研究结论。"},
}


class _GoalVerifier:
    def query_current_quest_goal_revision(self, quest_ref):
        assert quest_ref == "quest:1"
        return dict(GOAL)

    def verify_current_quest_goal_revision(self, binding):
        assert binding == GOAL


def test_stage_admission_freezes_current_goal_revision():
    owner = object.__new__(SQLiteAdvancementEngine)
    owner._reasoning_outcome_verifier = _GoalVerifier()

    bound = owner._freeze_stage_quest_goal(
        {"schema_ref": "stage", "cycle_ref": "cycle:1"}, quest_ref="quest:1"
    )

    assert bound["goal_revision_ref"] == GOAL["goal_revision_ref"]
    assert bound["quest_goal_revision"] == GOAL


def test_stage_admission_rejects_a_caller_supplied_stale_goal():
    owner = object.__new__(SQLiteAdvancementEngine)
    owner._reasoning_outcome_verifier = _GoalVerifier()

    with pytest.raises(OwnerConflict, match="quest_goal_revision_stale"):
        owner._freeze_stage_quest_goal(
            {
                "goal_revision_ref": "quest_goal_revision:old",
                "quest_goal_revision": {
                    **GOAL,
                    "goal_revision_ref": "quest_goal_revision:old",
                },
            },
            quest_ref="quest:1",
        )


@pytest.mark.parametrize("stage", ["idea", "plan", "bundle"])
def test_frozen_goal_is_rendered_as_stage_science(stage):
    view = stage_context_view(
        stage,
        {"goal_revision_ref": GOAL["goal_revision_ref"], "quest_goal_revision": GOAL},
        context_pack_ref="context:1",
        context_pack_hash="a" * 64,
    )

    assert view["sections"]["goal_revision_ref"] == GOAL["goal_revision_ref"]
    assert view["sections"]["quest_goal_revision"] == GOAL


def test_existing_stage_idempotency_row_is_read_before_goal_head():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ae_stage_commands (idempotency_key TEXT, "
                "command_kind TEXT, result_ref TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE ae_stage_run_requests (request_ref TEXT, "
                "cycle_ref TEXT, quest_ref TEXT, question_ref TEXT, "
                "content_ref TEXT, content_hash TEXT, context_pack_json TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ae_stage_commands VALUES "
                "('stage-request:1', 'ensure_plan_stage_request', 'request:old')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ae_stage_run_requests VALUES "
                "('request:old', 'cycle:1', 'quest:1', 'question:1', "
                "'content:1', 'hash:1', '{}')"
            )
        )

    class _Database:
        @contextmanager
        def read(self):
            with engine.connect() as connection:
                yield connection

    owner = object.__new__(SQLiteAdvancementEngine)
    owner._database = _Database()

    assert owner._query_stage_command_result(
        "stage-request:1", "ensure_plan_stage_request"
    ) == "request:old"
    with pytest.raises(OwnerConflict, match="idempotency_conflict"):
        owner._query_stage_command_result(
            "stage-request:1", "ensure_plan_stage_request", cycle_ref="cycle:other"
        )
    with pytest.raises(OwnerConflict, match="idempotency_conflict"):
        owner._query_stage_command_result(
            "stage-request:1",
            "ensure_plan_stage_request",
            context_pack={"cycle_ref": "cycle:other"},
        )
    engine.dispose()


def test_plan_retry_returns_old_request_before_reading_new_goal():
    owner = object.__new__(SQLiteAdvancementEngine)
    owner.query_cycle_position = lambda cycle_ref: {"cycle_ref": cycle_ref, "round": 1}
    owner._current_stage_epoch = lambda *args: 1
    owner._reasoning_prior_accepted_bindings = lambda cycle_ref: []
    owner._query_stage_command_result = lambda *args, **kwargs: "request:old"
    owner._query_stage_request_ref = lambda request_ref: request_ref

    def fail_if_goal_is_read(*_args, **_kwargs):
        raise AssertionError("a durable retry must not read the current goal")

    owner._freeze_stage_quest_goal = fail_if_goal_is_read

    result = owner.ensure_plan_stage_request(
        cycle_ref="cycle:1",
        accepted_question=None,
        accepted_idea_set=None,
        context_pack={},
        idempotency_key="stage-request:1",
    )

    assert result == "request:old"
