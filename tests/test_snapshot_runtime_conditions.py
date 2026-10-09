from pathlib import Path

from sqlalchemy import text

from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _runtime,
)


def test_first_snapshot_reads_conditions_initialized_before_its_cut(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path / "first-snapshot",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        completed = _confirm_direct_quest(runtime)
        quest_ref = completed["quest_ref"]
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT scope_ref FROM hc_runtime_condition_heads WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).first() is None

        snapshot = runtime.projection.query_snapshot(include_stages=False)
        current = snapshot["research_space"]["current_quest"]
        assert current["status"] == "ready"
        assert current["quest_ref"] == quest_ref
        conditions = current["conditions"]["runtime_conditions"]
        assert conditions["quest_ref"] == quest_ref
        assert conditions["text"].strip()
        assert runtime.projection.query_snapshot(include_stages=False)[
            "research_space"
        ]["current_quest"]["conditions"]["runtime_conditions"] == conditions
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT current_revision FROM hc_runtime_condition_heads WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).scalar_one() == conditions["revision"]
            assert connection.execute(
                text("SELECT COUNT(*) FROM hc_runtime_condition_versions WHERE scope_ref=:quest"),
                {"quest": quest_ref},
            ).scalar_one() == 1
    finally:
        runtime.close()
