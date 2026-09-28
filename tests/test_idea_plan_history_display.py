"""Idea and Plan history reads accepted text without replaying admission."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.research_overview import ResearchOverviewReader
from meta_research.writing_snapshot import WritingResearchSnapshotReader
from test_bundle_history_display import _changed
from test_research_overview import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _runtime,
    _confirm_direct_quest, _finish_idea_stage, _owner_revisions,
)


class _StrictOverview(ResearchOverviewReader):
    """Keep the same output assembly but use Writing's authoritative reads."""

    _stage_value = WritingResearchSnapshotReader._stage_value


@pytest.fixture
def accepted_idea_and_plan(tmp_path):
    runtime = _runtime(
        tmp_path / "idea-plan-history",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(16):
            if runtime.plan_stage.query_current()["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        else:
            raise AssertionError(runtime.plan_stage.transient_error)
        owners = runtime.owners
        with runtime._database.read_snapshot():
            history = owners.advancement_engine.query_quest_stage_history_display(
                quest["quest_ref"]
            )
            cycle = next(item for item in history if item.cycle_ref == quest["cycle_ref"])
            stages = {}
            for stage in ("idea", "plan"):
                commit = next(item for item in cycle.commits if item.stage == stage)
                request = next(item for item in cycle.requests
                               if item.request_ref == commit.request_ref)
                run_reader = getattr(owners.agent_runtime, f"query_{stage}_stage_run")
                run = run_reader(request.request_ref)
                assert run is not None and run.execution is not None
                if stage == "idea":
                    content = owners.research_memory.query_idea_outcome_content(
                        run.execution.submission_ref
                    )
                    decision = owners.research_graph.query_idea_outcome_decision(
                        run.execution.submission_ref
                    )
                else:
                    content = owners.research_memory.query_plan_document(
                        run.execution.submission_ref
                    )
                    decision = owners.research_graph.query_formal_plan_decision(
                        run.execution.submission_ref
                    )
                assert content is not None and decision is not None
                assert commit.disposition == "completed"
                stages[stage] = SimpleNamespace(
                    request=request, commit=commit, run=run, content=content,
                    decision=decision,
                )
        readers = (owners.research_graph, owners.advancement_engine,
                   owners.research_memory, owners.agent_runtime)
        yield SimpleNamespace(
            runtime=runtime, owners=owners, database=runtime._database,
            quest=quest, cycle=cycle, stages=stages,
            overview=ResearchOverviewReader(*readers),
            strict=_StrictOverview(*readers),
            writing=WritingResearchSnapshotReader(*readers),
        )
    finally:
        runtime.close()


def _artifact(fixture, stage, *, request=None, commit=None):
    value = fixture.stages[stage]
    return fixture.overview._artifact(
        fixture.quest["quest_ref"], fixture.cycle,
        value.request if request is None else request,
        value.commit if commit is None else commit,
    )


def _assert_gap(artifact):
    assert artifact["status"] == "unavailable"
    assert artifact["content"] is None
    assert artifact["reason"]["code"]


def test_idea_plan_display_matches_strict_overview_without_deep_decision_replay(
    accepted_idea_and_plan, monkeypatch
):
    fixture = accepted_idea_and_plan
    owner, database = fixture.owners.research_graph, fixture.database
    with database.read_snapshot():
        expected = fixture.strict._query_once(fixture.quest["quest_ref"])
        assert expected["status"] == "ready"
    revisions = _owner_revisions(fixture.runtime)
    strict_idea = Mock(side_effect=OwnerConflict("test_idea_admission_replayed"))
    strict_plan = Mock(side_effect=OwnerConflict("test_plan_admission_replayed"))
    with monkeypatch.context() as patch:
        patch.setattr(owner._receipt_verifier, "verify_idea_outcome_decision", strict_idea)
        patch.setattr(owner._receipt_verifier, "verify_formal_plan_decision", strict_plan)
        write = Mock(side_effect=AssertionError("display_performed_owner_write"))
        patch.setattr(database, "write", write)
        with database.read_snapshot():
            actual = fixture.overview._query_once(fixture.quest["quest_ref"])
        assert actual == expected
        strict_idea.assert_not_called()
        strict_plan.assert_not_called()
        write.assert_not_called()

        # The ordinary public queries still enter their original validators.
        for stage, query in (
            ("idea", owner.query_idea_outcome_decision),
            ("plan", owner.query_formal_plan_decision),
        ):
            with pytest.raises(OwnerConflict, match=f"test_{stage}_admission_replayed"):
                query(fixture.stages[stage].run.execution.submission_ref)
        assert strict_idea.call_count > 0 and strict_plan.call_count > 0
        for stage in ("idea", "plan"):
            value = fixture.stages[stage]
            with pytest.raises(OwnerConflict, match="test_.*_admission_replayed"):
                fixture.writing._stage_value(value.request, value.commit)
    assert _owner_revisions(fixture.runtime) == revisions
    assert database._read_cut.get() is None and database._read_cache.get() is None


def test_idea_plan_display_rejects_corrupted_decision_hashes(accepted_idea_and_plan):
    fixture = accepted_idea_and_plan
    for stage, table, body_hash in (
        ("idea", "rg_idea_outcome_decisions", "outcome_hash"),
        ("plan", "rg_formal_plan_decisions", "plan_document_hash"),
    ):
        value = fixture.stages[stage]
        for field in ("receipt_hash", body_hash):
            with _changed(fixture.database, table, "decision_ref",
                          value.decision.decision_ref, field, "0" * 64):
                with fixture.database.read_snapshot():
                    actual = fixture.overview._query_once(fixture.quest["quest_ref"])
                    artifact = next(
                        item for cycle in actual["cycles"]
                        for item in cycle["stages"][stage]
                        if item["source"]["request_ref"] == value.request.request_ref
                    )
                assert actual["status"] == "limited", (stage, field)
                _assert_gap(artifact)
    with fixture.database.read_snapshot():
        assert fixture.overview._query_once(fixture.quest["quest_ref"])["status"] == "ready"


def test_idea_plan_display_does_not_mix_request_commit_or_execution_bindings(
    accepted_idea_and_plan, monkeypatch
):
    fixture = accepted_idea_and_plan
    for stage, other_stage in (("idea", "plan"), ("plan", "idea")):
        value, other = fixture.stages[stage], fixture.stages[other_stage]
        assert value.commit.outcome_receipt is not None
        for request, commit in (
            (replace(value.request, context_pack_ref="another-context-pack"), value.commit),
            (value.request, other.commit),
            (value.request, replace(value.commit, outcome_ref=other.commit.outcome_ref)),
            (value.request, replace(value.commit, outcome_receipt=replace(
                value.commit.outcome_receipt, payload_hash="0" * 64
            ))),
        ):
            with fixture.database.read_snapshot():
                _assert_gap(_artifact(fixture, stage, request=request, commit=commit))

        # Keep a real admitted run and receipt, but splice in the other stage's
        # fence. The display decision must bind the exact execution it receives.
        wrong_execution = replace(value.run.execution, fence_ref=other.run.execution.fence_ref)
        with monkeypatch.context() as patch:
            patch.setattr(fixture.owners.agent_runtime, f"query_{stage}_stage_run",
                          Mock(return_value=replace(value.run, execution=wrong_execution)))
            with fixture.database.read_snapshot():
                _assert_gap(_artifact(fixture, stage))
    with fixture.database.read_snapshot():
        assert fixture.overview._query_once(fixture.quest["quest_ref"])["status"] == "ready"


def test_idea_plan_display_rechecks_each_original_content_file(accepted_idea_and_plan):
    fixture = accepted_idea_and_plan
    with fixture.database.read_snapshot():
        expected = fixture.overview._query_once(fixture.quest["quest_ref"])
    for stage, table in (("idea", "rm_idea_outcome_contents"), ("plan", "rm_plan_documents")):
        content = fixture.stages[stage].content
        with fixture.database.read() as connection:
            relative = connection.execute(
                text(f"SELECT object_path FROM {table} WHERE content_ref=:ref"),
                {"ref": content.content_ref},
            ).scalar_one()
        path = fixture.runtime.data_root.objects / relative
        saved = path.read_bytes()
        try:
            path.write_bytes(b"corrupted historical text")
            with fixture.database.read_snapshot():
                actual = fixture.overview._query_once(fixture.quest["quest_ref"])
                artifact = next(
                    item for cycle in actual["cycles"] for item in cycle["stages"][stage]
                    if item["source"]["content_ref"] == content.content_ref
                    or item["source"]["request_ref"] == fixture.stages[stage].request.request_ref
                )
            assert actual["status"] == "limited"
            _assert_gap(artifact)
        finally:
            path.write_bytes(saved)
    with fixture.database.read_snapshot():
        assert fixture.overview._query_once(fixture.quest["quest_ref"]) == expected
