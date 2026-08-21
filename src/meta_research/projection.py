from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meta_research import __version__
from meta_research.feed import DurableFeed
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, OwnerSnapshot
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.owners.research_memory import (
    ASSET_PROJECTION_HISTORY_PER_VERSION,
    ASSET_PROJECTION_MAX_PAGE_SIZE,
    ASSET_PROJECTION_PAGE_SIZE,
)

if TYPE_CHECKING:
    from meta_research.idea_stage import IdeaStageWorker


_MAX_SNAPSHOT_ATTEMPTS = 3


class SnapshotConsistencyUnavailable(RuntimeError):
    """No exact public Snapshot cut could be assembled within the retry bound."""

    def __init__(self) -> None:
        super().__init__("snapshot_consistency_unavailable")


class PublicProjection:
    """Rebuildable, read-only composition of the five Owner Snapshots."""

    def __init__(
        self,
        feed: DurableFeed,
        object_store: Path,
        research_graph: ResearchGraphInterface,
        advancement_engine: AdvancementEngineInterface,
        research_memory: ResearchMemoryInterface,
        agent_runtime: AgentRuntimeInterface,
        human_collaboration: HumanCollaborationInterface,
        idea_stage: IdeaStageWorker | None = None,
    ) -> None:
        self._feed = feed
        self._object_store = object_store
        self._human_collaboration = human_collaboration
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._idea_stage = idea_stage
        self._interfaces = {
            "research_graph": research_graph,
            "advancement_engine": advancement_engine,
            "research_memory": research_memory,
            "agent_runtime": agent_runtime,
            "human_collaboration": human_collaboration,
        }

    def query_snapshot(
        self,
        *,
        asset_offset: int = 0,
        asset_limit: int = ASSET_PROJECTION_PAGE_SIZE,
    ) -> dict[str, object]:
        if asset_offset < 0 or not 1 <= asset_limit <= ASSET_PROJECTION_MAX_PAGE_SIZE:
            raise ValueError("asset_projection_page_invalid")
        snapshot_consistent = False
        for _attempt in range(_MAX_SNAPSHOT_ATTEMPTS):
            feed_before = self._feed.query_readiness()
            owner_snapshots = {}
            for name, owner in self._interfaces.items():
                projection_query = getattr(owner, "query_projection_snapshot", None)
                owner_snapshots[name] = (
                    projection_query()
                    if callable(projection_query)
                    else owner.query_snapshot()
                )
            current_quest_creation = (
                self._human_collaboration.query_current_quest_creation()
            )
            idea_stage = (
                None if self._idea_stage is None else self._idea_stage.query_current()
            )
            current_question = (
                None
                if self._idea_stage is None
                else self._idea_stage.query_current_question()
            )
            projection_inventory = getattr(
                self._research_memory,
                "query_asset_projection_inventory",
                None,
            )
            research_assets = (
                _query_bounded_inventory(
                    projection_inventory,
                    offset=asset_offset,
                    limit=asset_limit,
                )
                if callable(projection_inventory)
                else self._research_memory.query_asset_inventory()[
                    asset_offset : asset_offset + asset_limit
                ]
            )
            if any(item.integrity != "verified" for item in research_assets):
                research_memory_snapshot = owner_snapshots["research_memory"]
                owner_snapshots["research_memory"] = OwnerSnapshot(
                    owner=research_memory_snapshot.owner,
                    revision=research_memory_snapshot.revision,
                    facts={
                        **research_memory_snapshot.facts,
                        "asset_integrity": "failed",
                    },
                    status="unavailable",
                )
            version_refs = tuple(item.version_ref for item in research_assets)
            asset_custodies = _query_related(
                self._research_memory.query_asset_custodies,
                version_refs,
                parameter="memory_refs",
            )
            projection_roles = getattr(
                self._research_graph,
                "query_asset_projection_roles",
                self._research_graph.query_asset_roles,
            )
            asset_roles = _query_related(
                projection_roles,
                version_refs,
                parameter="version_refs",
                limit_per_version=ASSET_PROJECTION_HISTORY_PER_VERSION,
            )
            inventory_by_ref = {
                item.version_ref: item for item in research_assets
            }
            for asset_role in asset_roles:
                asset_item = inventory_by_ref.get(asset_role.version_ref)
                if asset_item is None or (
                    asset_role.asset_ref != asset_item.asset_ref
                    or asset_role.asset_hash != asset_item.content_hash
                    or asset_role.manifest_hash != asset_item.manifest_hash
                    or asset_role.asset_receipt != asset_item.receipt
                ):
                    raise OwnerConflict("asset_role_binding_invalid")
            asset_holds = _query_related(
                self._research_memory.query_asset_holds,
                version_refs,
                parameter="memory_refs",
                limit_per_version=ASSET_PROJECTION_HISTORY_PER_VERSION,
            )
            release_assessments = _query_related(
                self._research_memory.query_release_eligibility_assessments,
                version_refs,
                parameter="memory_refs",
                limit_per_version=ASSET_PROJECTION_HISTORY_PER_VERSION,
            )
            asset_reference_revision = (
                self._research_graph.query_asset_reference_revision()
            )
            feed_readiness = self._feed.query_readiness()
            if feed_before.current_revision == feed_readiness.current_revision:
                snapshot_consistent = True
                break
        if not snapshot_consistent:
            raise SnapshotConsistencyUnavailable
        graph = owner_snapshots["research_graph"]
        advancement = owner_snapshots["advancement_engine"]
        research_memory = owner_snapshots["research_memory"]
        revision = feed_readiness.current_revision
        asset_total = int(
            research_memory.facts.get(
                "asset_version_count", asset_offset + len(research_assets)
            )
        )

        checks = [
            {
                "name": "database",
                "status": (
                    "ready" if feed_readiness.database_ready else "unavailable"
                ),
            },
            {
                "name": "object_store",
                "status": "ready" if self._object_store.is_dir() else "unavailable",
            },
            {
                "name": "owner_interfaces",
                "status": (
                    "ready"
                    if all(
                        snapshot.status == "ready"
                        for snapshot in owner_snapshots.values()
                    )
                    else "unavailable"
                ),
                "count": 5,
            },
            {"name": "durable_feed", "status": "ready", "revision": revision},
            {
                "name": "projection",
                "status": (
                    "ready"
                    if feed_readiness.projection_revision == revision
                    else "stale"
                ),
                "revision": feed_readiness.projection_revision,
            },
        ]
        ready = all(check["status"] == "ready" for check in checks)
        research_space: dict[str, object] = {
            "status": "empty" if graph.facts["quest_count"] == 0 else "active",
            "quest_count": graph.facts["quest_count"],
            "question_count": graph.facts["question_count"],
            "foreground_cycle_count": advancement.facts["foreground_cycle_count"],
        }
        if current_question is not None:
            research_space["current_question"] = current_question
        snapshot: dict[str, object] = {
            "product": {"name": "meta-research-vnext", "version": __version__},
            "revision": revision,
            "readiness": {"status": "ready" if ready else "unavailable", "checks": checks},
            "research_space": research_space,
            "owners": {
                name: snapshot.as_public_dict()
                for name, snapshot in owner_snapshots.items()
            },
            "quest_creation": {
                "status": "ready",
                "route": "direct",
                "current": current_quest_creation,
                "accepted_material_basis": {
                    "status": "ready",
                },
                "first_question_deepfetch": {
                    "status": "capability_unavailable",
                    "reason": {"code": "deepfetch_not_delivered"},
                },
            },
            "research_assets": {
                "status": "ready",
                "revision": revision,
                "inventory_revision": research_memory.revision,
                "items": [item.as_public_dict() for item in research_assets],
                "custodies": [
                    custody.as_public_dict() for custody in asset_custodies
                ],
                "roles": [role.as_public_dict() for role in asset_roles],
                "holds": [hold.as_public_dict() for hold in asset_holds],
                "release_assessments": [
                    assessment.as_public_dict()
                    for assessment in release_assessments
                ],
                "reference_revision": asset_reference_revision,
                "offset": asset_offset,
                "limit": asset_limit,
                "total_count": asset_total,
                "has_more": asset_offset + len(research_assets) < asset_total,
            },
            "unavailable": _release_capabilities(),
        }
        if idea_stage is not None:
            snapshot["idea_stage"] = idea_stage
        return snapshot


def _query_bounded_inventory(query, *, offset: int, limit: int):
    return query(offset=offset, limit=limit)


def _query_related(
    query,
    version_refs: tuple[str, ...],
    *,
    parameter: str,
    limit_per_version: int | None = None,
):
    kwargs: dict[str, object] = {parameter: version_refs}
    if limit_per_version is not None:
        kwargs["limit_per_version"] = limit_per_version
    return query(**kwargs)


def _release_capabilities() -> list[dict[str, object]]:
    reason = {
        "code": "not_enabled_in_this_release",
        "message": "This capability is not enabled in the installed release.",
    }
    return [
        {
            "capability": capability,
            "status": "capability_unavailable",
            "reason": reason.copy(),
        }
        for capability in (
            "first_question_deepfetch",
            "quest_companion",
            "stage_execution",
            "writing",
        )
    ]
