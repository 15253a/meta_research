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
    from meta_research.autonomous_creation import AutonomousCreationService
    from meta_research.bundle_stage import BundleStageWorker
    from meta_research.experiment import ExperimentService
    from meta_research.harness import HarnessRuntime
    from meta_research.idea_stage import IdeaStageWorker
    from meta_research.plan_stage import PlanStageWorker
    from meta_research.quest_completion import QuestCompletionService
    from meta_research.reasoning_stage import ReasoningStageWorker
    from meta_research.writing import WritingReportService


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
        plan_stage: PlanStageWorker | None = None,
        experiment: ExperimentService | None = None,
        bundle_stage: BundleStageWorker | None = None,
        reasoning_stage: ReasoningStageWorker | None = None,
        autonomous_creation: AutonomousCreationService | None = None,
        quest_completion: QuestCompletionService | None = None,
        writing: WritingReportService | None = None,
        harnesses: HarnessRuntime | None = None,
    ) -> None:
        self._feed = feed
        self._object_store = object_store
        self._human_collaboration = human_collaboration
        self._research_graph = research_graph
        self._research_memory = research_memory
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._idea_stage = idea_stage
        self._plan_stage = plan_stage
        self._bundle_stage = bundle_stage
        self._reasoning_stage = reasoning_stage
        self._autonomous_creation = autonomous_creation
        self._quest_completion = quest_completion
        self._experiment = experiment
        self._writing = writing
        self._harnesses = harnesses
        self._interfaces = {
            "research_graph": research_graph,
            "advancement_engine": advancement_engine,
            "research_memory": research_memory,
            "agent_runtime": agent_runtime,
            "human_collaboration": human_collaboration,
        }

    def query_question_history(
        self, question_ref: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, object]:
        """Compose one accepted Question's immutable identity and RG history."""

        if (
            not isinstance(question_ref, str)
            or not question_ref
            or len(question_ref) > 64
        ):
            raise OwnerConflict("question_ref_invalid")

        history_query = getattr(
            self._research_graph, "query_question_lifecycle_history", None
        )
        if not callable(history_query):
            return {
                "status": "unavailable",
                "question_ref": question_ref,
                "question": None,
                "lifecycle": None,
                "events": [],
                "offset": offset,
                "limit": limit,
                "total_count": 0,
                "has_more": False,
                "reason": {"code": "question_history_owner_seam_unavailable"},
            }
        question = self._research_graph.query_question_history_by_ref(question_ref)
        if question is None:
            return {
                "status": "absent",
                "question_ref": question_ref,
                "question": None,
                "lifecycle": None,
                "events": [],
                "offset": offset,
                "limit": limit,
                "total_count": 0,
                "has_more": False,
                "reason": {"code": "accepted_question_not_found"},
            }
        content = self._research_memory.read_question_content(
            question.content_ref, question.content_hash
        )
        lifecycle: dict[str, object] | None = None
        page: dict[str, object] | None = None
        events: list[dict[str, object]] = []
        for _attempt in range(_MAX_SNAPSHOT_ATTEMPTS):
            lifecycle_before = self._research_graph.query_question_lifecycle(
                question_ref
            )
            try:
                candidate = history_query(
                    question_ref, offset=offset, limit=limit
                )
            except OwnerConflict as exc:
                if str(exc) != "question_lifecycle_history_invalid":
                    raise
                continue
            lifecycle_after = self._research_graph.query_question_lifecycle(
                question_ref
            )
            if not isinstance(candidate, dict) or not isinstance(
                candidate.get("items"), tuple
            ):
                raise OwnerConflict("question_history_projection_invalid")
            candidate_events = list(candidate["items"])
            if (
                lifecycle_before == lifecycle_after
                and candidate.get("total_count")
                == lifecycle_after.get("revision")
                and (
                    candidate.get("has_more")
                    or not candidate_events
                    or candidate_events[-1].get("status")
                    == lifecycle_after.get("status")
                )
            ):
                lifecycle = lifecycle_after
                page = candidate
                events = candidate_events
                break
        if lifecycle is None or page is None:
            raise SnapshotConsistencyUnavailable
        return {
            "status": "ready",
            "question_ref": question_ref,
            "question": {
                "question_ref": question.question_ref,
                "quest_ref": question.quest_ref,
                "parent_question_ref": question.parent_question_ref,
                "initialization_id": question.initialization_id,
                "context_ref": question.context_ref,
                "content": {
                    "content_ref": question.content_ref,
                    "content_hash": question.content_hash,
                    "schema_ref": question.schema_ref,
                    "document": content,
                },
                "receipts": {
                    "content_acceptance": (
                        question.content_receipt.as_public_dict()
                    ),
                    "question_acceptance": question.receipt.as_public_dict(),
                    "confirmation_ref": question.confirmation_ref,
                    "confirmation_hash": question.confirmation_hash,
                },
            },
            "lifecycle": lifecycle,
            "events": events,
            "offset": page["offset"],
            "limit": page["limit"],
            "total_count": page["total_count"],
            "has_more": page["has_more"],
            "reason": None,
        }

    def query_question_evidence(self, question_ref: str) -> dict[str, object]:
        """Resolve only EvidenceRefs frozen against this exact Question.

        An RG asset role is Quest-scoped.  It becomes selectable here only when
        an issuer-owned AE StageRunRequest binds both the accepted Question and
        its exact ``accepted_evidence_refs``.  Quest roles alone therefore
        produce a typed absence, never an invented Question association.
        """

        if (
            not isinstance(question_ref, str)
            or not question_ref
            or len(question_ref) > 64
        ):
            raise OwnerConflict("question_ref_invalid")
        question = self._research_graph.query_question_history_by_ref(question_ref)
        if question is None:
            return {
                "status": "absent",
                "question_ref": question_ref,
                "quest_ref": None,
                "binding": None,
                "items": [],
                "reason": {"code": "accepted_question_not_found"},
            }
        _reference_revision, quest_evidence_refs = (
            self._research_graph.query_evidence_reference_state(
                question.quest_ref
            )
        )

        def absent(code: str) -> dict[str, object]:
            return {
                "status": "absent",
                "question_ref": question_ref,
                "quest_ref": question.quest_ref,
                "binding": None,
                "items": [],
                "reason": {
                    "code": code,
                    "quest_evidence_role_count": len(quest_evidence_refs),
                },
            }

        foreground_query = getattr(
            self._advancement_engine, "query_foreground", None
        )
        idea_request_query = getattr(
            self._advancement_engine, "query_idea_stage_request", None
        )
        inventory_query = getattr(
            self._research_memory, "query_asset_projection_inventory_item", None
        )
        if not all(
            callable(query)
            for query in (foreground_query, idea_request_query, inventory_query)
        ):
            return {
                "status": "unavailable",
                "question_ref": question_ref,
                "quest_ref": question.quest_ref,
                "binding": None,
                "items": [],
                "reason": {"code": "question_evidence_owner_seam_unavailable"},
            }
        foreground = foreground_query(question.quest_ref)
        if (
            not isinstance(foreground, dict)
            or foreground.get("question_ref") != question.question_ref
            or not isinstance(foreground.get("cycle_ref"), str)
        ):
            return absent("question_evidence_binding_absent")
        request = idea_request_query(foreground["cycle_ref"])
        if request is None:
            return absent("question_evidence_binding_absent")
        if request.accepted_question.as_dict() != question.as_binding().as_dict():
            raise OwnerConflict("question_evidence_question_binding_invalid")
        refs = request.context_pack.get("accepted_evidence_refs")
        if (
            not isinstance(refs, list)
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise OwnerConflict("question_evidence_refs_invalid")
        binding = {
            "cycle_ref": request.cycle_ref,
            "request_ref": request.request_ref,
            "context_pack_ref": request.context_pack_ref,
            "context_pack_hash": request.context_pack_hash,
            "evidence_reference_revision": request.context_pack.get(
                "evidence_reference_revision"
            ),
            "question_receipt_ref": question.receipt.receipt_ref,
            "question_receipt_hash": question.receipt.payload_hash,
        }
        if not refs:
            value = absent("question_evidence_refs_empty")
            value["binding"] = binding
            return value
        projection_roles = self._research_graph.query_asset_projection_roles(
            version_refs=tuple(refs),
            limit_per_version=ASSET_PROJECTION_HISTORY_PER_VERSION,
        )
        roles_by_ref = {
            role.version_ref: role
            for role in projection_roles
            if role.quest_ref == question.quest_ref and role.role == "evidence"
        }
        if set(refs) - set(roles_by_ref):
            return {
                "status": "unavailable",
                "question_ref": question_ref,
                "quest_ref": question.quest_ref,
                "binding": binding,
                "items": [],
                "reason": {"code": "question_evidence_role_binding_unavailable"},
            }
        items: list[dict[str, object]] = []
        for evidence_ref in refs:
            role = roles_by_ref[evidence_ref]
            asset = inventory_query(evidence_ref)
            if asset is None:
                return {
                    "status": "unavailable",
                    "question_ref": question_ref,
                    "quest_ref": question.quest_ref,
                    "binding": binding,
                    "items": [],
                    "reason": {
                        "code": "question_evidence_asset_projection_unavailable"
                    },
                }
            if (
                asset.asset_ref != role.asset_ref
                or asset.content_hash != role.asset_hash
                or asset.manifest_hash != role.manifest_hash
                or asset.receipt != role.asset_receipt
            ):
                raise OwnerConflict("question_evidence_asset_binding_invalid")
            items.append(
                {
                    "evidence_ref": evidence_ref,
                    "role": role.as_public_dict(),
                    "asset": asset.as_public_dict(),
                }
            )
        return {
            "status": "ready",
            "question_ref": question_ref,
            "quest_ref": question.quest_ref,
            "binding": binding,
            "items": items,
            "reason": None,
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
            runtime_observability_query = getattr(
                self._agent_runtime, "query_runtime_observability", None
            )
            runtime_observability = (
                runtime_observability_query()
                if callable(runtime_observability_query)
                else {
                    "schema_ref": "meta-research/runtime-observability/v1",
                    "status": "unavailable",
                    "reason": {"code": "runtime_protection_unavailable"},
                }
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
            control_quest_ref = (
                collaboration_scope.removeprefix("quest:")
                if collaboration_scope.startswith("quest:")
                else None
            )
            foreground_query = getattr(
                self._advancement_engine, "query_foreground", None
            )
            foregrounds_by_quest: dict[str, dict[str, object] | None] = {}
            if control_quest_ref is not None and callable(foreground_query):
                foregrounds_by_quest[control_quest_ref] = foreground_query(
                    control_quest_ref
                )
            managed_runs_query = getattr(
                self._agent_runtime, "query_managed_runs", None
            )
            prune_records_query = getattr(
                self._research_graph, "query_restorable_prune_records", None
            )
            research_control = (
                {
                    "status": "ready",
                    "quest_ref": control_quest_ref,
                    "foreground": foregrounds_by_quest[control_quest_ref],
                    "managed_runs": list(managed_runs_query(control_quest_ref)),
                    "recovery_records": (
                        list(prune_records_query(control_quest_ref))
                        if callable(prune_records_query)
                        else []
                    ),
                    "actions": [
                        "pause",
                        "resume",
                        "normal_switch",
                        "forced_switch",
                        "cancel",
                        "abandon",
                        "prune",
                        "restore",
                    ],
                }
                if control_quest_ref is not None
                and callable(foreground_query)
                and callable(managed_runs_query)
                else {
                    "status": "capability_unavailable",
                    "quest_ref": control_quest_ref,
                    "foreground": None,
                    "managed_runs": [],
                    "recovery_records": [],
                    "actions": [],
                }
                if control_quest_ref is not None
                else {
                    "status": "idle",
                    "quest_ref": None,
                    "foreground": None,
                    "managed_runs": [],
                    "recovery_records": [],
                    "actions": [],
                }
            )
            current_quest = _query_current_quest_goal(
                self._research_graph,
                control_quest_ref,
            )
            idea_stage = (
                None if self._idea_stage is None else self._idea_stage.query_current()
            )
            plan_stage = (
                None if self._plan_stage is None else self._plan_stage.query_current()
            )
            bundle_stage = (
                None
                if self._bundle_stage is None
                else self._bundle_stage.query_current()
            )
            reasoning_stage = (
                None
                if self._reasoning_stage is None
                else self._reasoning_stage.query_current()
            )
            autonomous_creation = (
                None
                if self._autonomous_creation is None
                else self._autonomous_creation.query_current()
            )
            quest_completion = (
                None
                if self._quest_completion is None
                else self._quest_completion.query_current()
            )
            current_experiment = (
                None if self._experiment is None else self._experiment.query_current()
            )
            current_question = (
                None
                if self._idea_stage is None
                else self._idea_stage.query_current_question()
            )
            writing = (
                {
                    "status": "unavailable",
                    "document_types": ["report"],
                    "runs": [],
                    "reason": {"code": "writing_capability_not_configured"},
                }
                if self._writing is None
                else self._writing.query_overview()
            )
            harnesses = (
                {
                    "status": "capability_unavailable",
                    "reason": {"code": "harness_runtime_unavailable"},
                    "gateway": None,
                    "adapters": [],
                }
                if self._harnesses is None
                else self._harnesses.query_status()
            )
            question_tree_items: list[dict[str, object]] = []
            question_tree_reason: dict[str, str] | None = None
            query_question_tree = getattr(
                self._research_graph, "query_question_tree", None
            )
            if callable(query_question_tree) and control_quest_ref is not None:
                try:
                    for question in query_question_tree(control_quest_ref):
                        content = self._research_memory.read_question_content(
                            question.content_ref, question.content_hash
                        )
                        query_lifecycle = getattr(
                            self._research_graph, "query_question_lifecycle", None
                        )
                        lifecycle = (
                            query_lifecycle(question.question_ref)
                            if callable(query_lifecycle)
                            else {"status": "active", "revision": 1}
                        )
                        question_tree_items.append(
                            {
                                "question_ref": question.question_ref,
                                "quest_ref": question.quest_ref,
                                "parent_question_ref": question.parent_question_ref,
                                "title": content.get("title"),
                                "unknown_statement": content.get("unknown_statement"),
                                "content_ref": question.content_ref,
                                "content_hash": question.content_hash,
                                "schema_ref": question.schema_ref,
                                "question_receipt_ref": (
                                    question.receipt.receipt_ref
                                ),
                                "lifecycle_status": lifecycle["status"],
                                "lifecycle_revision": lifecycle["revision"],
                                "cycle_binding": _query_question_cycle_binding(
                                    foreground_query,
                                    foregrounds_by_quest,
                                    quest_ref=question.quest_ref,
                                    question_ref=question.question_ref,
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
            for question in question_tree_items:
                cycle_binding = question["cycle_binding"]
                assert isinstance(cycle_binding, dict)
                cycle_ref = cycle_binding.get("cycle_ref")
                question["related_human_requests"] = _related_human_requests(
                    human_requests,
                    quest_ref=str(question["quest_ref"]),
                    question_ref=str(question["question_ref"]),
                    cycle_ref=cycle_ref if isinstance(cycle_ref, str) else None,
                )
            collaboration_scopes = tuple(
                dict.fromkeys(
                    [
                        collaboration_scope,
                        "runtime:telemetry",
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
            inventory_by_ref = {item.version_ref: item for item in research_assets}
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
                "status": ("ready" if feed_readiness.database_ready else "unavailable"),
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
            "current_quest": current_quest,
        }
        if current_question is not None:
            research_space["current_question"] = current_question
        snapshot: dict[str, object] = {
            "product": {"name": "meta-research-vnext", "version": __version__},
            "revision": revision,
            "readiness": {
                "status": "ready" if ready else "unavailable",
                "checks": checks,
            },
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
                "status": ("ready" if question_tree_reason is None else "unavailable"),
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
                "custodies": [custody.as_public_dict() for custody in asset_custodies],
                "roles": [role.as_public_dict() for role in asset_roles],
                "holds": [hold.as_public_dict() for hold in asset_holds],
                "release_assessments": [
                    assessment.as_public_dict() for assessment in release_assessments
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
            "research_control": research_control,
            "experiment": {
                "status": "idle" if current_experiment is None else "active",
                "current": current_experiment,
            },
            "writing": writing,
            "harnesses": harnesses,
            "runtime_observability": runtime_observability,
            "unavailable": _release_capabilities(),
        }
        if idea_stage is not None:
            snapshot["idea_stage"] = idea_stage
        if plan_stage is not None and _plan_stage_is_public(plan_stage):
            snapshot["plan_stage"] = plan_stage
        if bundle_stage is not None and _bundle_stage_is_public(bundle_stage):
            snapshot["bundle_stage"] = bundle_stage
        if reasoning_stage is not None and _reasoning_stage_is_public(
            reasoning_stage
        ):
            snapshot["reasoning_stage"] = reasoning_stage
        if self._autonomous_creation is not None:
            snapshot["autonomous_creation"] = {
                "status": "ready",
                "creation_mode": "AutonomousCreation",
                "current": autonomous_creation,
            }
        if self._quest_completion is not None:
            snapshot["quest_completion"] = {
                "status": "ready",
                "current": quest_completion,
            }
        return snapshot


def _query_current_quest_goal(
    research_graph: ResearchGraphInterface,
    quest_ref: str | None,
) -> dict[str, object]:
    empty = {
        "quest_ref": quest_ref,
        "goal_revision_ref": None,
        "draft_revision": None,
        "draft_hash": None,
        "goal": None,
        "completion_criteria": None,
        "projection_digest": None,
    }
    if quest_ref is None:
        return {
            "status": "not_bound",
            **empty,
            "reason": {"code": "current_quest_not_bound"},
        }
    query = getattr(research_graph, "query_current_quest_goal_revision", None)
    if not callable(query):
        return {
            "status": "unavailable",
            **empty,
            "reason": {"code": "quest_goal_query_unavailable"},
        }
    binding = query(quest_ref)
    if binding is None:
        return {
            "status": "not_bound",
            **empty,
            "reason": {"code": "quest_goal_revision_not_bound"},
        }
    goal_document = binding.get("goal") if isinstance(binding, dict) else None
    if (
        not isinstance(binding, dict)
        or binding.get("kind") != "QuestGoalRevision"
        or binding.get("quest_ref") != quest_ref
        or not isinstance(binding.get("goal_revision_ref"), str)
        or not binding["goal_revision_ref"]
        or not isinstance(binding.get("draft_revision"), int)
        or isinstance(binding.get("draft_revision"), bool)
        or not isinstance(binding.get("draft_hash"), str)
        or len(str(binding["draft_hash"])) != 64
        or not isinstance(goal_document, dict)
        or not isinstance(goal_document.get("goal"), str)
        or not isinstance(goal_document.get("completion_criteria"), str)
    ):
        raise OwnerConflict("quest_goal_projection_invalid")
    return {
        "status": "ready",
        "quest_ref": quest_ref,
        "goal_revision_ref": binding["goal_revision_ref"],
        "draft_revision": binding["draft_revision"],
        "draft_hash": binding["draft_hash"],
        "goal": goal_document["goal"],
        "completion_criteria": goal_document["completion_criteria"],
        "projection_digest": canonical_hash(binding),
        "reason": None,
    }


def _query_question_cycle_binding(
    foreground_query,
    foregrounds_by_quest: dict[str, dict[str, object] | None],
    *,
    quest_ref: str,
    question_ref: str,
) -> dict[str, object]:
    if not callable(foreground_query):
        return {
            "status": "unavailable",
            "cycle_ref": None,
            "foreground": None,
            "reason": {"code": "advancement_foreground_query_unavailable"},
        }
    if quest_ref not in foregrounds_by_quest:
        foregrounds_by_quest[quest_ref] = foreground_query(quest_ref)
    foreground = foregrounds_by_quest[quest_ref]
    if foreground is not None and not isinstance(foreground, dict):
        raise OwnerConflict("question_cycle_projection_invalid")
    if foreground is None or foreground.get("question_ref") != question_ref:
        return {
            "status": "not_bound",
            "cycle_ref": None,
            "foreground": None,
            "reason": {"code": "current_foreground_not_bound"},
        }
    if (
        foreground.get("quest_ref") != quest_ref
        or not isinstance(foreground.get("cycle_ref"), str)
        or not foreground["cycle_ref"]
    ):
        raise OwnerConflict("question_cycle_projection_invalid")
    return {
        "status": "bound",
        "cycle_ref": foreground["cycle_ref"],
        "foreground": dict(foreground),
        "reason": None,
    }


def _related_human_requests(
    requests: tuple[dict[str, object], ...],
    *,
    quest_ref: str,
    question_ref: str,
    cycle_ref: str | None,
) -> dict[str, object]:
    related: list[dict[str, object]] = []
    for request in requests:
        if request.get("quest_ref") != quest_ref:
            continue
        bindings = _request_question_bindings(
            request,
            question_ref=question_ref,
            cycle_ref=cycle_ref,
        )
        if not bindings:
            continue
        request_ref = request.get("request_ref")
        if not isinstance(request_ref, str) or not request_ref:
            raise OwnerConflict("question_human_request_projection_invalid")
        related.append(
            {
                "request_ref": request_ref,
                "issuer": request.get("issuer"),
                "kind": request.get("kind"),
                "status": request.get("status"),
                "revision": request.get("revision"),
                "bindings": bindings,
            }
        )
    return {
        "status": "ready",
        "items": sorted(related, key=lambda item: str(item["request_ref"])),
        "reason": None,
    }


def _request_question_bindings(
    request: dict[str, object],
    *,
    question_ref: str,
    cycle_ref: str | None,
) -> list[dict[str, object]]:
    bindings = _assertion_question_bindings(
        request.get("target_assertion"),
        source="target_assertion",
        waiter_ref=None,
        question_ref=question_ref,
        cycle_ref=cycle_ref,
    )
    direct_waiters = request.get("direct_waiters", [])
    if not isinstance(direct_waiters, (list, tuple)):
        raise OwnerConflict("question_human_request_projection_invalid")
    for waiter in direct_waiters:
        if not isinstance(waiter, dict):
            raise OwnerConflict("question_human_request_projection_invalid")
        waiter_ref = waiter.get("waiter_ref")
        if not isinstance(waiter_ref, str) or not waiter_ref:
            continue
        bindings.extend(
            _assertion_question_bindings(
                waiter.get("target_assertion"),
                source="direct_waiter",
                waiter_ref=waiter_ref,
                question_ref=question_ref,
                cycle_ref=cycle_ref,
            )
        )
    return sorted(
        bindings,
        key=lambda item: (
            str(item["source"]),
            str(item.get("waiter_ref", "")),
            str(item["field"]),
            str(item["ref"]),
        ),
    )


def _assertion_question_bindings(
    assertion: object,
    *,
    source: str,
    waiter_ref: str | None,
    question_ref: str,
    cycle_ref: str | None,
) -> list[dict[str, object]]:
    if not isinstance(assertion, dict):
        return []
    matches: list[dict[str, object]] = []
    for field in ("question_ref", "source_question_ref", "target_question_ref"):
        if assertion.get(field) == question_ref:
            matches.append(
                _question_binding_document(
                    source, waiter_ref, field, question_ref
                )
            )
    affected = assertion.get("affected_question_refs")
    if isinstance(affected, (list, tuple)) and question_ref in affected:
        matches.append(
            _question_binding_document(
                source,
                waiter_ref,
                "affected_question_refs",
                question_ref,
            )
        )
    if cycle_ref is not None:
        for field in ("cycle_ref", "source_cycle_ref", "target_cycle_ref"):
            if assertion.get(field) == cycle_ref:
                matches.append(
                    _question_binding_document(
                        source, waiter_ref, field, cycle_ref
                    )
                )
    return matches


def _question_binding_document(
    source: str,
    waiter_ref: str | None,
    field: str,
    ref: str,
) -> dict[str, object]:
    return {
        "source": source,
        **({} if waiter_ref is None else {"waiter_ref": waiter_ref}),
        "field": field,
        "ref": ref,
    }


def _plan_stage_is_public(projection: dict[str, object]) -> bool:
    """Publish Plan only after the accepted IdeaSet makes it actionable.

    An empty, ineligible Plan projection must not displace the still-current Idea
    experience in the fixed public shell.  Once eligibility is established, any
    durable downstream boundary keeps Plan visible through recovery and commit.
    """

    eligibility = projection.get("eligibility")
    if isinstance(eligibility, dict) and eligibility.get("status") in {
        "eligible",
        "requested",
        "consumed",
    }:
        return True
    if any(
        projection.get(field) is not None
        for field in (
            "stage_run_request",
            "run",
            "stage_commit",
        )
    ):
        return True
    acceptance = projection.get("plan_acceptance")
    return isinstance(acceptance, dict) and acceptance.get("status") not in {
        None,
        "not_attempted",
    }


def _bundle_stage_is_public(projection: dict[str, object]) -> bool:
    """Publish Bundle once an accepted FormalPlan makes it actionable."""

    eligibility = projection.get("eligibility")
    if isinstance(eligibility, dict) and eligibility.get("status") == "eligible":
        return True
    target_graph = projection.get("target_graph")
    return (
        any(
            projection.get(field) is not None
            for field in ("stage_run_request", "run", "stage_commit")
        )
        or (
            isinstance(target_graph, dict)
            and target_graph.get("status") != "not_attempted"
        )
        or bool(projection.get("target_commits"))
    )


def _reasoning_stage_is_public(projection: dict[str, object]) -> bool:
    """Publish Reasoning only once the routed closure is actionable."""

    eligibility = projection.get("eligibility")
    if isinstance(eligibility, dict) and eligibility.get("status") in {
        "eligible",
        "requested",
        "consumed",
    }:
        return True
    if any(
        projection.get(field) is not None
        for field in ("stage_run_request", "run", "stage_commit")
    ):
        return True
    acceptance = projection.get("reasoning_acceptance")
    return isinstance(acceptance, dict) and acceptance.get("status") not in {
        None,
        "not_attempted",
    }


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
                "safe_meaningful_runnable_exists": (safe_meaningful_runnable_exists),
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
        )
    ]
