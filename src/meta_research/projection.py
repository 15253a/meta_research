from __future__ import annotations

from pathlib import Path

from meta_research import __version__
from meta_research.feed import DurableFeed
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface


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
    ) -> None:
        self._feed = feed
        self._object_store = object_store
        self._interfaces = {
            "research_graph": research_graph,
            "advancement_engine": advancement_engine,
            "research_memory": research_memory,
            "agent_runtime": agent_runtime,
            "human_collaboration": human_collaboration,
        }

    def query_snapshot(self) -> dict[str, object]:
        owner_snapshots = {
            name: owner.query_snapshot() for name, owner in self._interfaces.items()
        }
        graph = owner_snapshots["research_graph"]
        advancement = owner_snapshots["advancement_engine"]
        feed_readiness = self._feed.query_readiness()
        revision = feed_readiness.current_revision

        checks = [
            {
                "name": "database",
                "status": "ready" if feed_readiness.database_ready else "unavailable",
            },
            {
                "name": "object_store",
                "status": "ready" if self._object_store.is_dir() else "unavailable",
            },
            {"name": "owner_interfaces", "status": "ready", "count": 5},
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
        return {
            "product": {"name": "meta-research-vnext", "version": __version__},
            "revision": revision,
            "readiness": {"status": "ready" if ready else "unavailable", "checks": checks},
            "research_space": {
                "status": "empty" if graph.facts["quest_count"] == 0 else "active",
                "quest_count": graph.facts["quest_count"],
                "question_count": graph.facts["question_count"],
                "foreground_cycle_count": advancement.facts["foreground_cycle_count"],
            },
            "owners": {
                name: snapshot.as_public_dict()
                for name, snapshot in owner_snapshots.items()
            },
            "unavailable": _release_capabilities(),
        }


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
            "quest_creation",
            "quest_companion",
            "stage_execution",
            "writing",
        )
    ]
