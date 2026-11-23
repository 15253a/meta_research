"""Quest Goal is frozen at Stage admission and delivered as science input."""

from contextlib import contextmanager
from types import SimpleNamespace
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
        accepted_question=SimpleNamespace(quest_ref="quest:1"),
        accepted_idea_set=None,
        context_pack={},
        idempotency_key="stage-request:1",
    )

    assert result == "request:old"


def test_completed_quest_goal_head_blocks_new_stage_admission():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE rg_quest_goal_heads "
                "(quest_ref TEXT, current_revision_ref TEXT, status TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO rg_quest_goal_heads VALUES "
                "('quest:1', 'quest_goal_revision:1', 'completion_committed'), "
                "('quest:2', 'quest_goal_revision:2', 'open')"
            )
        )

    owner = object.__new__(SQLiteAdvancementEngine)
    with engine.connect() as connection:
        with pytest.raises(OwnerConflict, match="quest_goal_closed"):
            owner._assert_stage_quest_goal_current(
                connection,
                quest_ref="quest:1",
                goal_revision_ref="quest_goal_revision:1",
            )
        with pytest.raises(OwnerConflict, match="quest_goal_revision_stale"):
            owner._assert_stage_quest_goal_current(
                connection,
                quest_ref="quest:2",
                goal_revision_ref="quest_goal_revision:1",
            )
        owner._assert_stage_quest_goal_current(
            connection,
            quest_ref="quest:2",
            goal_revision_ref="quest_goal_revision:2",
        )
    engine.dispose()


def test_reasoning_retry_rejects_drifted_literature_revision_and_stage_mismatch():
    import json

    engine = create_engine("sqlite://")
    stored_pack = json.dumps(
        {
            "question_literature_input": {
                "kind": "revision",
                "revision_ref": "lit_rev:1",
                "binding": {"revision_ref": "lit_rev:1"},
            }
        }
    )
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
                "stage TEXT, cycle_ref TEXT, epoch INTEGER, context_pack_json TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ae_stage_commands VALUES "
                "('reasoning-request:1', 'ensure_reasoning_stage_request', 'req:1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ae_stage_run_requests VALUES "
                "('req:1', 'reasoning', 'cycle:1', 1, :pack)"
            ),
            {"pack": stored_pack},
        )

    class _Database:
        @contextmanager
        def read(self):
            with engine.connect() as connection:
                yield connection

    owner = object.__new__(SQLiteAdvancementEngine)
    owner._database = _Database()

    assert (
        owner._query_stage_command_result(
            "reasoning-request:1",
            "ensure_reasoning_stage_request",
            stage="reasoning",
            cycle_ref="cycle:1",
            epoch=1,
            check_question_literature=True,
            question_literature_revision={"revision_ref": "lit_rev:1"},
        )
        == "req:1"
    )
    with pytest.raises(OwnerConflict, match="idempotency_conflict"):
        owner._query_stage_command_result(
            "reasoning-request:1",
            "ensure_reasoning_stage_request",
            stage="reasoning",
            cycle_ref="cycle:1",
            epoch=1,
            check_question_literature=True,
            question_literature_revision={"revision_ref": "lit_rev:2"},
        )
    with pytest.raises(OwnerConflict, match="idempotency_conflict"):
        owner._query_stage_command_result(
            "reasoning-request:1",
            "ensure_reasoning_stage_request",
            stage="reasoning",
            cycle_ref="cycle:1",
            epoch=1,
            check_question_literature=True,
            question_literature_revision=None,
        )
    with pytest.raises(OwnerConflict, match="idempotency_conflict"):
        owner._query_stage_command_result(
            "reasoning-request:1",
            "ensure_reasoning_stage_request",
            stage="plan",
            cycle_ref="cycle:1",
            epoch=1,
            check_question_literature=True,
            question_literature_revision={"revision_ref": "lit_rev:1"},
        )
    engine.dispose()


def test_stage_quest_goal_binding_validation_is_symmetric():
    from meta_research.quest_goal import validate_stage_quest_goal_binding

    assert validate_stage_quest_goal_binding({}, expected_quest_ref="quest:1") == (
        None,
        None,
    )
    assert validate_stage_quest_goal_binding(
        {"goal_revision_ref": GOAL["goal_revision_ref"], "quest_goal_revision": GOAL},
        expected_quest_ref="quest:1",
    ) == (GOAL["goal_revision_ref"], GOAL)

    for bad_pack in (
        {"goal_revision_ref": GOAL["goal_revision_ref"]},
        {"quest_goal_revision": GOAL},
        {"goal_revision_ref": "", "quest_goal_revision": {**GOAL, "goal_revision_ref": ""}},
        {"goal_revision_ref": GOAL["goal_revision_ref"], "quest_goal_revision": {**GOAL, "kind": "Other"}},
        {"goal_revision_ref": GOAL["goal_revision_ref"], "quest_goal_revision": {**GOAL, "quest_ref": "quest:other"}},
        {"goal_revision_ref": "quest_goal_revision:other", "quest_goal_revision": GOAL},
        {"goal_revision_ref": None, "quest_goal_revision": None},
    ):
        with pytest.raises(ValueError, match="frozen quest goal binding"):
            validate_stage_quest_goal_binding(bad_pack, expected_quest_ref="quest:1")


def test_bundle_rolling_operation_input_omits_goal_keys_when_absent(tmp_path, monkeypatch):
    import meta_research.bundle_skill as bundle_skill
    from meta_research.bundle_skill import (
        BundleDispatchRequest,
        BundleRuntimeBinding,
        BundleTargetBatchRequest,
        CodexBundleSkillAdapter,
    )

    monkeypatch.setattr(
        bundle_skill,
        "_validate_inbox_checkpoint_projection",
        lambda *_args, **_kwargs: "d" * 64,
    )
    monkeypatch.setattr(
        bundle_skill,
        "bundle_bindings_compatible",
        lambda *_args, **_kwargs: True,
    )

    binding = BundleRuntimeBinding(
        packaged_skill_bundle_hash="a" * 64,
        instruction_set_hash="b" * 64,
        model_ref="test-model",
        harness_adapter_ref="test-adapter",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )
    inbox = {
        "schema_ref": "meta-research/root-inbox-checkpoint/v1",
        "run_ref": "run:1",
        "attempt_ref": "attempt:1",
        "fence_ref": "fence:1",
        "consumed_message_refs": [],
        "consumed_message_hashes": [],
        "consumed_count": 0,
        "checkpoint_hash": "d" * 64,
    }
    captured = []

    class _Stop(Exception):
        pass

    adapter = object.__new__(CodexBundleSkillAdapter)
    adapter._workspace = tmp_path
    adapter._batch_plan_reader = None
    adapter._batch_source_readers = None
    adapter.runtime_binding = lambda: binding
    adapter._delivered_bundle_records = lambda *_a, **_kw: {"current_targets": set(), "target_commits": set()}

    def _capture(**kwargs):
        captured.append(kwargs["operation_input"])
        raise _Stop()

    adapter._invoke_with_resident_mcp = _capture

    cycle_position = {
        "cycle_ref": "cycle:1",
        "round": 1,
        "cycle_focus": "Evaluate baseline parity.",
        "focus_source": {"kind": "initial"},
    }
    dispatch_kwargs = dict(
        stage_request_ref="req:1",
        cycle_ref="cycle:1",
        run_ref="run:1",
        attempt_ref="attempt:1",
        fence_ref="fence:1",
        root_session_ref="session:1",
        native_session_ref="native:1",
        runtime_binding=binding,
        graph_ref="graph:1",
        generation=1,
        frontier=(),
        state={"status": "running"},
        inbox_checkpoint=inbox,
        cycle_position=cycle_position,
        job_ref="job:dispatch",
    )
    with pytest.raises(_Stop):
        adapter.schedule_targets(BundleDispatchRequest(**dispatch_kwargs, quest_goal_revision=None))
    assert "goal_revision_ref" not in captured[-1]["request"]
    assert "quest_goal_revision" not in captured[-1]["request"]

    with pytest.raises(_Stop):
        adapter.schedule_targets(BundleDispatchRequest(**dispatch_kwargs, quest_goal_revision=GOAL))
    assert captured[-1]["request"]["goal_revision_ref"] == GOAL["goal_revision_ref"]
    assert captured[-1]["request"]["quest_goal_revision"] == GOAL

    batch_kwargs = dict(
        stage_request_ref="req:1",
        cycle_ref="cycle:1",
        run_ref="run:1",
        attempt_ref="attempt:1",
        fence_ref="fence:1",
        root_session_ref="session:1",
        native_session_ref="native:1",
        runtime_binding=binding,
        graph_ref="graph:1",
        formal_plan_ref="formal_plan_1",
        context_pack_ref="ctx:1",
        context_pack_hash="e" * 64,
        base_generation=1,
        base_head_receipt={"receipt_ref": "r:1"},
        inbox_checkpoint=inbox,
        plan_document={"schema_ref": "p"},
        initial_target_plan={"source_bindings": {}},
        current_targets=(),
        target_commits=(),
        cycle_position=cycle_position,
        job_ref="job:batch",
    )
    with pytest.raises(_Stop):
        adapter.propose_target_batch(BundleTargetBatchRequest(**batch_kwargs, quest_goal_revision=None))
    assert "goal_revision_ref" not in captured[-1]["request"]
    assert "quest_goal_revision" not in captured[-1]["request"]

    with pytest.raises(_Stop):
        adapter.propose_target_batch(BundleTargetBatchRequest(**batch_kwargs, quest_goal_revision=GOAL))
    assert captured[-1]["request"]["goal_revision_ref"] == GOAL["goal_revision_ref"]
    assert captured[-1]["request"]["quest_goal_revision"] == GOAL
