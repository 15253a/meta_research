from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.feed import FeedReadiness
from meta_research.owners.common import OwnerSnapshot
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
