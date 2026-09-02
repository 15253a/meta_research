from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research.feed import FeedReadiness
from meta_research.owners.common import OwnerSnapshot, canonical_hash
from meta_research.projection import PublicProjection


class _MutableFeed:
    def __init__(self) -> None:
        self.revision = 1

    def query_readiness(self) -> FeedReadiness:
        return FeedReadiness(
            database_ready=True,
            current_revision=self.revision,
            projection_revision=self.revision,
        )


class _AlwaysAdvancingFeed:
    def __init__(self) -> None:
        self.calls = 0

    def query_readiness(self) -> FeedReadiness:
        self.calls += 1
        return FeedReadiness(
            database_ready=True,
            current_revision=self.calls,
            projection_revision=self.calls,
        )


class _StaticOwner:
    def __init__(
        self,
        owner: str,
        facts: dict[str, int],
        requests: tuple[dict[str, object], ...] = (),
        safe_runnable: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._owner = owner
        self._facts = facts
        self._requests = requests
        self._safe_runnable = safe_runnable

    def query_snapshot(self) -> OwnerSnapshot:
        return OwnerSnapshot(self._owner, 1, self._facts)

    def query_human_requests(self, **_values) -> tuple[dict[str, object], ...]:
        return self._requests

    def query_safe_meaningful_runnable(
        self, quest_ref: str, blocked_waiters: tuple[dict[str, object], ...]
    ) -> tuple[dict[str, object], ...]:
        return self._safe_runnable


class _StaticResearchMemory(_StaticOwner):
    def query_asset_inventory(self) -> tuple[()]:
        return ()

    def query_asset_custodies(
        self, *, memory_refs: tuple[str, ...] | None = None
    ) -> tuple[()]:
        return ()

    def query_asset_holds(
        self,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
    ) -> tuple[()]:
        return ()

    def query_release_eligibility_assessments(
        self,
        *,
        memory_refs: tuple[str, ...] | None = None,
        limit_per_version: int | None = None,
    ) -> tuple[()]:
        return ()


class _StaticResearchGraph(_StaticOwner):
    def query_asset_roles(self) -> tuple[()]:
        return ()

    def query_asset_projection_roles(
        self,
        *,
        version_refs: tuple[str, ...],
        limit_per_version: int,
    ) -> tuple[()]:
        return ()

    def query_asset_reference_revision(self) -> int:
        return 1


class _QuestionTreeResearchGraph(_StaticResearchGraph):
    def __init__(
        self,
        *,
        goal_revision: dict[str, object] | None,
        requests: tuple[dict[str, object], ...],
    ) -> None:
        super().__init__(
            "research_graph",
            {"quest_count": 1, "question_count": 2},
            requests,
        )
        self._goal_revision = goal_revision
        self._questions = (
            SimpleNamespace(
                question_ref="question_projection_root",
                quest_ref="quest_projection",
                parent_question_ref=None,
                content_ref="memory:projection-root",
                content_hash="1" * 64,
                schema_ref="question/v1",
                receipt=SimpleNamespace(receipt_ref="receipt:projection-root"),
            ),
            SimpleNamespace(
                question_ref="question_projection_child",
                quest_ref="quest_projection",
                parent_question_ref="question_projection_root",
                content_ref="memory:projection-child",
                content_hash="2" * 64,
                schema_ref="question/v1",
                receipt=SimpleNamespace(receipt_ref="receipt:projection-child"),
            ),
        )

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        assert quest_ref == "quest_projection"
        return self._goal_revision

    def query_question_tree(self, quest_ref: str | None = None):
        return tuple(
            question
            for question in self._questions
            if quest_ref is None or question.quest_ref == quest_ref
        )

    def query_question_lifecycle(self, question_ref: str) -> dict[str, object]:
        assert question_ref in {item.question_ref for item in self._questions}
        return {"status": "active", "revision": 1}


class _QuestionTreeResearchMemory(_StaticResearchMemory):
    def read_question_content(
        self, content_ref: str, content_hash: str
    ) -> dict[str, object]:
        assert len(content_hash) == 64
        return {
            "title": content_ref.removeprefix("memory:"),
            "unknown_statement": f"unknown:{content_ref}",
        }


class _QuestionForegroundOwner(_StaticOwner):
    def query_foreground(self, quest_ref: str) -> dict[str, object] | None:
        assert quest_ref == "quest_projection"
        return {
            "quest_ref": quest_ref,
            "cycle_ref": "cycle_projection_root",
            "question_ref": "question_projection_root",
            "stage": "idea",
            "epoch": 4,
            "status": "active",
            "grant_ref": "grant_projection_root",
            "grant_status": "active",
            "safe_point_ref": None,
            "pending_operation_ref": None,
            "owner_revision": 7,
        }


class _RacingResearchGraph:
    def __init__(self, feed: _MutableFeed) -> None:
        self._feed = feed
        self.calls = 0

    def query_snapshot(self) -> OwnerSnapshot:
        self.calls += 1
        observed_revision = self._feed.revision
        snapshot = OwnerSnapshot(
            "research_graph",
            observed_revision,
            {
                "quest_count": 0 if observed_revision == 1 else 1,
                "question_count": 0 if observed_revision == 1 else 1,
            },
        )
        if self.calls == 1:
            self._feed.revision = 2
        return snapshot

    def query_asset_roles(self) -> tuple[()]:
        return ()

    def query_asset_projection_roles(
        self,
        *,
        version_refs: tuple[str, ...],
        limit_per_version: int,
    ) -> tuple[()]:
        return ()

    def query_asset_reference_revision(self) -> int:
        return self._feed.revision

    def query_human_requests(self) -> tuple[()]:
        return ()


class _RacingHarnesses:
    def __init__(self, feed: _MutableFeed) -> None:
        self._feed = feed
        self.calls = 0

    def query_status(self) -> dict[str, object]:
        self.calls += 1
        observed_revision = self._feed.revision
        if self.calls == 1:
            self._feed.revision = 2
        return {
            "status": "ready",
            "gateway": {"catalog_revision": observed_revision},
            "adapters": [],
        }


class _HumanCollaboration(_StaticOwner):
    collaboration_scope = "workspace"

    def query_current_quest_creation(self) -> None:
        return None

    def query_collaboration_scope(self) -> str:
        return self.collaboration_scope

    def query_companion(self, scope_ref: str) -> dict[str, object]:
        return {
            "scope_ref": scope_ref,
            "session_ref": None,
            "status": "ready",
            "turns": [],
        }

    def query_collaboration_projection(
        self, scope_refs: tuple[str, ...]
    ) -> dict[str, list[dict[str, object]]]:
        return {
            "messages": [],
            "soft_constraints": [],
            "agent_proposals": [],
            "commands": [],
            "authorizations": [],
        }


class _StageProjection:
    def __init__(self, projection: dict[str, object]) -> None:
        self.projection = projection
        self.queries = 0

    def query_current(self) -> dict[str, object]:
        self.queries += 1
        return self.projection


class _IdeaStageProjection(_StageProjection):
    def query_current_question(self) -> None:
        return None


def test_idea_foreground_does_not_query_ineligible_downstream_stages(
    tmp_path: Path,
) -> None:
    idea = _IdeaStageProjection({"eligibility": {"status": "requested"}})
    plan = _StageProjection({"eligibility": {"status": "not_eligible"}})
    bundle = _StageProjection({"eligibility": {"status": "not_eligible"}})
    reasoning = _StageProjection({"eligibility": {"status": "not_eligible"}})
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_projection"
    projection = PublicProjection(
        feed=_MutableFeed(),  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_StaticResearchGraph(
            "research_graph", {"quest_count": 1, "question_count": 1}
        ),  # type: ignore[arg-type]
        advancement_engine=_QuestionForegroundOwner(
            "advancement_engine", {"foreground_cycle_count": 1}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
        idea_stage=idea,  # type: ignore[arg-type]
        plan_stage=plan,  # type: ignore[arg-type]
        bundle_stage=bundle,  # type: ignore[arg-type]
        reasoning_stage=reasoning,  # type: ignore[arg-type]
    )

    snapshot = projection.query_snapshot()

    assert snapshot["idea_stage"] == idea.projection
    assert "plan_stage" not in snapshot
    assert "bundle_stage" not in snapshot
    assert "reasoning_stage" not in snapshot
    assert (plan.queries, bundle.queries, reasoning.queries) == (0, 0, 0)


def test_snapshot_retries_when_feed_advances_during_owner_reads(
    tmp_path: Path,
) -> None:
    feed = _MutableFeed()
    graph = _RacingResearchGraph(feed)
    projection = PublicProjection(
        feed=feed,  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=graph,  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 0}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=_HumanCollaboration(
            "human_collaboration", {}
        ),  # type: ignore[arg-type]
    )

    snapshot = projection.query_snapshot()

    assert graph.calls == 2
    assert snapshot["revision"] == 2
    assert snapshot["research_space"]["quest_count"] == 1
    assert snapshot["owners"]["research_graph"]["revision"] == 2


def test_snapshot_retries_when_harness_projection_advances_the_feed(
    tmp_path: Path,
) -> None:
    feed = _MutableFeed()
    harnesses = _RacingHarnesses(feed)
    projection = PublicProjection(
        feed=feed,  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_StaticResearchGraph(
            "research_graph", {"quest_count": 0, "question_count": 0}
        ),  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 0}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=_HumanCollaboration(
            "human_collaboration", {}
        ),  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
    )

    snapshot = projection.query_snapshot()

    assert harnesses.calls == 2
    assert snapshot["revision"] == 2
    assert snapshot["harnesses"]["gateway"]["catalog_revision"] == 2


def test_snapshot_fails_closed_without_publishing_an_unstable_owner_cut(
    tmp_path: Path,
) -> None:
    feed = _AlwaysAdvancingFeed()
    projection = PublicProjection(
        feed=feed,  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_StaticResearchGraph(
            "research_graph", {"quest_count": 0, "question_count": 0}
        ),  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 0}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=_HumanCollaboration(
            "human_collaboration", {}
        ),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="snapshot_consistency_unavailable"):
        projection.query_snapshot()

    assert feed.calls == 6


def test_quest_waiting_downgrades_only_when_owner_proves_exact_safe_work(
    tmp_path: Path,
) -> None:
    feed = _MutableFeed()
    request = {
        "request_ref": "human_request_safe_work_probe",
        "quest_ref": "quest_safe_work",
        "status": "open",
        "created_at": 1.0,
        "direct_waiters": [
            {
                "waiter_ref": "blocked_quest_waiter",
                "target_assertion": {"work_ref": "blocked_work"},
                "wait_scope": "quest",
                "status": "blocked",
                "other_blockers": [],
            }
        ],
    }
    collaboration = _HumanCollaboration(
        "human_collaboration", {"pending_intent_count": 0}
    )
    collaboration.collaboration_scope = "quest:quest_safe_work"
    projection = PublicProjection(
        feed=feed,  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_StaticResearchGraph(
            "research_graph", {"quest_count": 0, "question_count": 0}
        ),  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 0}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {"pending_intake_count": 0}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner(
            "agent_runtime",
            {"active_run_count": 1, "acquisition_active_slot_count": 0},
            (request,),
            (
                {
                    "owner": "agent_runtime",
                    "owner_revision": 1,
                    "quest_ref": "quest_safe_work",
                    "work_kind": "stage_run",
                    "work_ref": "stage_run_safe_1",
                    "status": "running",
                },
            ),
        ),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
    )

    waiting = projection.query_snapshot()["human_collaboration"][
        "human_requests"
    ]["waiting"]
    assert waiting["scope"] == "local"
    assert waiting["safe_meaningful_runnable_exists"] is True
    assert waiting["safe_runnable_basis"] == [
        {
            "owner": "agent_runtime",
            "owner_revision": 1,
            "quest_ref": "quest_safe_work",
            "work_kind": "stage_run",
            "work_ref": "stage_run_safe_1",
            "status": "running",
        }
    ]


def test_unbound_owner_counters_cannot_downgrade_quest_waiting(
    tmp_path: Path,
) -> None:
    feed = _MutableFeed()
    request = {
        "request_ref": "human_request_blocked_quest",
        "quest_ref": "quest_blocked",
        "status": "open",
        "created_at": 1.0,
        "direct_waiters": [
            {
                "waiter_ref": "blocked_quest_waiter",
                "target_assertion": {"work_ref": "blocked_work"},
                "wait_scope": "quest",
                "status": "blocked",
                "other_blockers": [],
            }
        ],
    }
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_blocked"
    projection = PublicProjection(
        feed=feed,  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_StaticResearchGraph(
            "research_graph", {"quest_count": 1, "question_count": 1}
        ),  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 1}
        ),  # type: ignore[arg-type]
        research_memory=_StaticResearchMemory(
            "research_memory", {"pending_intake_count": 7}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner(
            "agent_runtime",
            {"active_run_count": 9, "acquisition_active_slot_count": 2},
            (request,),
        ),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
    )

    waiting = projection.query_snapshot()["human_collaboration"][
        "human_requests"
    ]["waiting"]
    assert waiting == {
        "scope": "quest",
        "safe_meaningful_runnable_exists": False,
        "safe_runnable_basis": [],
        "other_blockers": [],
    }


def test_question_tree_projects_only_exact_public_goal_cycle_and_request_bindings(
    tmp_path: Path,
) -> None:
    goal_revision = {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": "quest_goal_revision_projection",
        "quest_ref": "quest_projection",
        "draft_revision": 3,
        "draft_hash": "d" * 64,
        "goal": {
            "goal": "Determine whether the evidence supports the claim.",
            "completion_criteria": "Record a bounded conclusion with counterevidence.",
            "background_and_initial_direction": "Start from the accepted corpus.",
        },
        "rg_quest_acceptance_receipt_ref": "receipt:quest-projection",
    }
    requests = (
        {
            "request_ref": "human_request_question_exact",
            "issuer": "research_graph",
            "revision": 2,
            "quest_ref": "quest_projection",
            "kind": "question_control_confirmation",
            "status": "open",
            "created_at": 1.0,
            "target_assertion": {
                "question_ref": "question_projection_root",
            },
            "direct_waiters": [
                {
                    "waiter_ref": "waiter_cycle_exact",
                    "status": "blocked",
                    "target_assertion": {
                        "source_cycle_ref": "cycle_projection_root",
                    },
                }
            ],
        },
        {
            "request_ref": "human_request_child_exact",
            "issuer": "research_graph",
            "revision": 1,
            "quest_ref": "quest_projection",
            "kind": "question_input",
            "status": "open",
            "created_at": 2.0,
            "target_assertion": {
                "target_question_ref": "question_projection_child",
            },
            "direct_waiters": [],
        },
        {
            "request_ref": "human_request_text_only",
            "issuer": "research_graph",
            "revision": 1,
            "quest_ref": "quest_projection",
            "kind": "unrelated",
            "status": "open",
            "created_at": 3.0,
            "business_purpose": "question_projection_root needs discussion",
            "target_assertion": {"quest_ref": "quest_projection"},
            "direct_waiters": [],
        },
        {
            "request_ref": "human_request_unscoped_collision",
            "issuer": "agent_runtime",
            "revision": 1,
            "kind": "unrelated",
            "status": "open",
            "created_at": 4.0,
            "target_assertion": {
                "question_ref": "question_projection_root",
                "cycle_ref": "cycle_projection_root",
            },
            "direct_waiters": [],
        },
    )
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_projection"
    projection = PublicProjection(
        feed=_MutableFeed(),  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=_QuestionTreeResearchGraph(
            goal_revision=goal_revision,
            requests=requests,
        ),  # type: ignore[arg-type]
        advancement_engine=_QuestionForegroundOwner(
            "advancement_engine", {"foreground_cycle_count": 1}
        ),  # type: ignore[arg-type]
        research_memory=_QuestionTreeResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
    )

    snapshot = projection.query_snapshot()

    assert snapshot["research_space"]["current_quest"] == {
        "status": "ready",
        "quest_ref": "quest_projection",
        "goal_revision_ref": "quest_goal_revision_projection",
        "draft_revision": 3,
        "draft_hash": "d" * 64,
        "goal": "Determine whether the evidence supports the claim.",
        "completion_criteria": "Record a bounded conclusion with counterevidence.",
        "projection_digest": canonical_hash(goal_revision),
        "reason": None,
    }
    root, child = snapshot["question_tree"]["items"]
    assert root["cycle_binding"] == {
        "status": "bound",
        "cycle_ref": "cycle_projection_root",
        "foreground": {
            "quest_ref": "quest_projection",
            "cycle_ref": "cycle_projection_root",
            "question_ref": "question_projection_root",
            "stage": "idea",
            "epoch": 4,
            "status": "active",
            "grant_ref": "grant_projection_root",
            "grant_status": "active",
            "safe_point_ref": None,
            "pending_operation_ref": None,
            "owner_revision": 7,
        },
        "reason": None,
    }
    assert root["related_human_requests"] == {
        "status": "ready",
        "items": [
            {
                "request_ref": "human_request_question_exact",
                "issuer": "research_graph",
                "kind": "question_control_confirmation",
                "status": "open",
                "revision": 2,
                "bindings": [
                    {
                        "source": "direct_waiter",
                        "waiter_ref": "waiter_cycle_exact",
                        "field": "source_cycle_ref",
                        "ref": "cycle_projection_root",
                    },
                    {
                        "source": "target_assertion",
                        "field": "question_ref",
                        "ref": "question_projection_root",
                    },
                ],
            }
        ],
        "reason": None,
    }
    assert child["cycle_binding"] == {
        "status": "not_bound",
        "cycle_ref": None,
        "foreground": None,
        "reason": {"code": "current_foreground_not_bound"},
    }
    assert [
        item["request_ref"] for item in child["related_human_requests"]["items"]
    ] == ["human_request_child_exact"]


def test_question_tree_is_bounded_to_the_current_collaboration_quest(
    tmp_path: Path,
) -> None:
    goal_revision = {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": "quest_goal_revision_projection",
        "quest_ref": "quest_projection",
        "draft_revision": 1,
        "draft_hash": "d" * 64,
        "goal": {
            "goal": "Keep the active Quest isolated.",
            "completion_criteria": "No foreign Question enters the public tree.",
            "background_and_initial_direction": "Use the HC collaboration scope.",
        },
        "rg_quest_acceptance_receipt_ref": "receipt:quest-projection",
    }
    foreign_request = {
        "request_ref": "human_request_foreign_question",
        "issuer": "research_graph",
        "revision": 1,
        "quest_ref": "quest_foreign",
        "kind": "question_input",
        "status": "open",
        "created_at": 1.0,
        "target_assertion": {"question_ref": "question_foreign"},
        "direct_waiters": [],
    }
    graph = _QuestionTreeResearchGraph(
        goal_revision=goal_revision,
        requests=(foreign_request,),
    )
    graph._questions = (  # noqa: SLF001 - projection contract fixture
        SimpleNamespace(
            question_ref="question_foreign",
            quest_ref="quest_foreign",
            parent_question_ref=None,
            content_ref="memory:foreign",
            content_hash="f" * 64,
            schema_ref="question/v1",
            receipt=SimpleNamespace(receipt_ref="receipt:foreign"),
        ),
        *graph._questions,  # noqa: SLF001 - projection contract fixture
    )
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_projection"
    projection = PublicProjection(
        feed=_MutableFeed(),  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=graph,  # type: ignore[arg-type]
        advancement_engine=_QuestionForegroundOwner(
            "advancement_engine", {"foreground_cycle_count": 1}
        ),  # type: ignore[arg-type]
        research_memory=_QuestionTreeResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
    )

    snapshot = projection.query_snapshot()

    assert snapshot["question_tree"]["status"] == "ready"
    assert {
        item["quest_ref"] for item in snapshot["question_tree"]["items"]
    } == {"quest_projection"}
    assert {
        item["question_ref"] for item in snapshot["question_tree"]["items"]
    } == {"question_projection_root", "question_projection_child"}


def test_projection_reports_typed_nulls_when_goal_and_cycle_seams_are_unavailable(
    tmp_path: Path,
) -> None:
    collaboration = _HumanCollaboration("human_collaboration", {})
    collaboration.collaboration_scope = "quest:quest_unavailable"
    graph = _StaticResearchGraph(
        "research_graph", {"quest_count": 1, "question_count": 1}
    )
    question = SimpleNamespace(
        question_ref="question_unavailable",
        quest_ref="quest_unavailable",
        parent_question_ref=None,
        content_ref="memory:unavailable",
        content_hash="3" * 64,
        schema_ref="question/v1",
        receipt=SimpleNamespace(receipt_ref="receipt:unavailable"),
    )
    graph.query_question_tree = lambda _quest_ref=None: (  # type: ignore[attr-defined]
        question,
    )
    graph.query_question_lifecycle = lambda _ref: {  # type: ignore[attr-defined]
        "status": "active",
        "revision": 1,
    }
    projection = PublicProjection(
        feed=_MutableFeed(),  # type: ignore[arg-type]
        object_store=tmp_path,
        research_graph=graph,  # type: ignore[arg-type]
        advancement_engine=_StaticOwner(
            "advancement_engine", {"foreground_cycle_count": 0}
        ),  # type: ignore[arg-type]
        research_memory=_QuestionTreeResearchMemory(
            "research_memory", {}
        ),  # type: ignore[arg-type]
        agent_runtime=_StaticOwner("agent_runtime", {}),  # type: ignore[arg-type]
        human_collaboration=collaboration,  # type: ignore[arg-type]
    )

    current = projection.query_snapshot()["research_space"]["current_quest"]

    assert current == {
        "status": "unavailable",
        "quest_ref": "quest_unavailable",
        "goal_revision_ref": None,
        "draft_revision": None,
        "draft_hash": None,
        "goal": None,
        "completion_criteria": None,
        "projection_digest": None,
        "reason": {"code": "quest_goal_query_unavailable"},
    }
    [item] = projection.query_snapshot()["question_tree"]["items"]
    assert item["cycle_binding"] == {
        "status": "unavailable",
        "cycle_ref": None,
        "foreground": None,
        "reason": {"code": "advancement_foreground_query_unavailable"},
    }
