from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meta_research import __version__
from meta_research.feed import DurableFeed
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, OwnerSnapshot, canonical_hash
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
_COLLABORATION_SCOPE_PAGE_SIZE = 101


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
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
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
            collaboration_scope_query = getattr(
                self._human_collaboration, "query_collaboration_scope", None
            )
            collaboration_scope = (
                collaboration_scope_query()
                if callable(collaboration_scope_query)
                else _collaboration_scope(current_quest_creation)
            )
            idea_stage = (
                None if self._idea_stage is None else self._idea_stage.query_current()
            )
            current_question = (
                None
                if self._idea_stage is None
                else self._idea_stage.query_current_question()
            )
            question_tree_items: list[dict[str, object]] = []
            question_tree_reason: dict[str, str] | None = None
            query_question_tree = getattr(
                self._research_graph, "query_question_tree", None
            )
            if callable(query_question_tree):
                try:
                    for question in query_question_tree():
                        content = self._research_memory.read_question_content(
                            question.content_ref, question.content_hash
                        )
                        question_tree_items.append(
                            {
                                "question_ref": question.question_ref,
                                "quest_ref": question.quest_ref,
                                "parent_question_ref": question.parent_question_ref,
                                "title": content.get("title"),
                                "unknown_statement": content.get(
                                    "unknown_statement"
                                ),
                                "content_ref": question.content_ref,
                                "content_hash": question.content_hash,
                                "schema_ref": question.schema_ref,
                                "question_receipt_ref": (
                                    question.receipt.receipt_ref
                                ),
                            }
                        )
                except OwnerConflict as error:
                    question_tree_items = []
                    question_tree_reason = {"code": str(error)}
            human_requests = tuple(
                request
                for owner in (
                    self._research_graph,
                    self._research_memory,
                    self._agent_runtime,
                    self._advancement_engine,
                )
                for request in owner.query_human_requests()
            )
            collaboration_scopes = tuple(
                dict.fromkeys(
                    [
                        collaboration_scope,
                        *(str(item["request_ref"]) for item in human_requests),
                    ]
                )
            )
            collaboration = _query_collaboration_pages(
                self._human_collaboration,
                collaboration_scopes,
            )
            safe_runnable_basis = _safe_meaningful_runnable_basis(
                self._agent_runtime,
                collaboration_scope,
                human_requests,
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
                "route": "direct_or_deepfetch",
                "current": current_quest_creation,
                "accepted_material_basis": {
                    "status": "ready",
                },
                "first_question_deepfetch": {
                    "status": "ready",
                },
            },
            "question_tree": {
                "status": (
                    "ready" if question_tree_reason is None else "unavailable"
                ),
                "items": question_tree_items,
                "reason": question_tree_reason,
            },
            "manual_question_creation": {
                "status": "ready",
                "creation_mode": "ManualCreation",
                "deepfetch": {"status": "ready"},
                "explicit_waiver": {"status": "ready"},
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
            "human_collaboration": _human_collaboration_projection(
                collaboration_scope,
                human_requests,
                collaboration,
                safe_runnable_basis,
            ),
            "unavailable": _release_capabilities(),
        }
        if idea_stage is not None:
            snapshot["idea_stage"] = idea_stage
        return snapshot


def _query_bounded_inventory(query, *, offset: int, limit: int):
    return query(offset=offset, limit=limit)


def _query_collaboration_pages(
    owner: HumanCollaborationInterface,
    scopes: tuple[str, ...],
) -> dict[str, list[dict[str, object]]]:
    combined = {
        "messages": [],
        "soft_constraints": [],
        "agent_proposals": [],
        "commands": [],
        "authorizations": [],
    }
    seen: dict[str, set[str]] = {name: set() for name in combined}
    for offset in range(0, len(scopes), _COLLABORATION_SCOPE_PAGE_SIZE):
        page = owner.query_collaboration_projection(
            scopes[offset : offset + _COLLABORATION_SCOPE_PAGE_SIZE]
        )
        for name, items in page.items():
            if name not in combined:
                raise OwnerConflict("collaboration_projection_invalid")
            for item in items:
                identity = canonical_hash(item)
                if identity not in seen[name]:
                    seen[name].add(identity)
                    combined[name].append(item)
    return combined


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


def _collaboration_scope(
    current_quest_creation: dict[str, object] | None,
) -> str:
    if current_quest_creation is None:
        return "workspace"
    quest_ref = current_quest_creation.get("quest_ref")
    if isinstance(quest_ref, str) and quest_ref:
        return f"quest:{quest_ref}"
    initialization_id = current_quest_creation.get("initialization_id")
    if isinstance(initialization_id, str) and initialization_id:
        return f"quest-initialization:{initialization_id}"
    return "workspace"


def _human_collaboration_projection(
    scope_ref: str,
    requests: tuple[dict[str, object], ...],
    collaboration: dict[str, list[dict[str, object]]],
    safe_runnable_basis: list[dict[str, object]],
) -> dict[str, object]:
    quest_ref = (
        scope_ref.removeprefix("quest:") if scope_ref.startswith("quest:") else None
    )
    scoped_requests = (
        requests
        if quest_ref is None
        else tuple(item for item in requests if item.get("quest_ref") == quest_ref)
    )
    blocked_waiters = [
        waiter
        for request in scoped_requests
        if request.get("status") == "open"
        for waiter in request.get("direct_waiters", [])
        if isinstance(waiter, dict) and waiter.get("status") == "blocked"
    ]
    quest_waiting = any(
        waiter.get("wait_scope") == "quest" for waiter in blocked_waiters
    )
    local_waiting = any(
        waiter.get("wait_scope") == "local" for waiter in blocked_waiters
    )
    safe_meaningful_runnable_exists = bool(safe_runnable_basis)
    blockers = sorted(
        {
            blocker
            for waiter in blocked_waiters
            for blocker in waiter.get("other_blockers", [])
            if isinstance(blocker, str)
        }
    )
    ordered_requests = sorted(
        requests,
        key=lambda item: (
            item.get("status") != "open",
            float(item.get("created_at", 0.0)),
            str(item.get("request_ref", "")),
        ),
    )
    return {
        "companion": {
            "status": "ready",
            "scope_ref": scope_ref,
            "messages": collaboration["messages"],
            "soft_constraints": collaboration["soft_constraints"],
            "agent_proposals": collaboration["agent_proposals"],
        },
        "human_requests": {
            "status": "ready",
            "waiting": {
                "scope": (
                    "quest"
                    if quest_waiting and not safe_meaningful_runnable_exists
                    else "local"
                    if quest_waiting or local_waiting
                    else "none"
                ),
                "safe_meaningful_runnable_exists": (
                    safe_meaningful_runnable_exists
                ),
                "safe_runnable_basis": safe_runnable_basis,
                "other_blockers": blockers,
            },
            "items": ordered_requests,
        },
        "commands": {
            "status": "ready",
            "items": collaboration["commands"],
            "authorizations": collaboration["authorizations"],
        },
    }


def _safe_meaningful_runnable_basis(
    agent_runtime: AgentRuntimeInterface,
    scope_ref: str,
    requests: tuple[dict[str, object], ...],
) -> list[dict[str, object]]:
    """Ask the execution Owner for exact Quest-bound work, never infer from counts."""

    if not scope_ref.startswith("quest:"):
        return []
    quest_ref = scope_ref.removeprefix("quest:")
    if not quest_ref:
        raise OwnerConflict("collaboration_scope_invalid")
    blocked_waiters: list[dict[str, object]] = []
    for request in requests:
        if request.get("quest_ref") != quest_ref or request.get("status") != "open":
            continue
        for waiter in request.get("direct_waiters", []):
            if not isinstance(waiter, dict) or waiter.get("status") != "blocked":
                continue
            waiter_ref = waiter.get("waiter_ref")
            target_assertion = waiter.get("target_assertion")
            if not isinstance(waiter_ref, str) or not isinstance(
                target_assertion, dict
            ):
                raise OwnerConflict("safe_meaningful_runnable_projection_invalid")
            blocked_waiters.append(
                {
                    "waiter_ref": waiter_ref,
                    "target_assertion": dict(target_assertion),
                }
            )
    ordered_blocked_waiters = tuple(
        sorted(
            blocked_waiters,
            key=lambda item: (
                str(item["waiter_ref"]),
                canonical_hash(item["target_assertion"]),
            ),
        )
    )
    query = getattr(agent_runtime, "query_safe_meaningful_runnable", None)
    if not callable(query):
        return []
    observed = query(quest_ref, ordered_blocked_waiters)
    if not isinstance(observed, (list, tuple)) or len(observed) > 100:
        raise OwnerConflict("safe_meaningful_runnable_projection_invalid")
    basis: list[dict[str, object]] = []
    seen_refs: set[str] = set()
    for item in observed:
        if (
            not isinstance(item, dict)
            or item.get("owner") != "agent_runtime"
            or item.get("quest_ref") != quest_ref
            or not isinstance(item.get("owner_revision"), int)
            or isinstance(item.get("owner_revision"), bool)
            or not isinstance(item.get("work_kind"), str)
            or not isinstance(item.get("work_ref"), str)
            or not isinstance(item.get("status"), str)
            or item["work_ref"] in seen_refs
        ):
            raise OwnerConflict("safe_meaningful_runnable_projection_invalid")
        seen_refs.add(str(item["work_ref"]))
        basis.append(dict(item))
    return sorted(
        basis,
        key=lambda item: (str(item["work_kind"]), str(item["work_ref"])),
    )


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
            "stage_execution",
            "writing",
        )
    ]
