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
    def __init__(self, owner: str, facts: dict[str, int]) -> None:
        self._owner = owner
        self._facts = facts

    def query_snapshot(self) -> OwnerSnapshot:
        return OwnerSnapshot(self._owner, 1, self._facts)


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


class _HumanCollaboration(_StaticOwner):
    def query_current_quest_creation(self) -> None:
        return None


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
