from __future__ import annotations

from typing import cast

from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.writing_contract import WRITING_RESEARCH_SNAPSHOT_SCHEMA


_MAX_CONSISTENCY_ATTEMPTS = 3
_MAX_SNAPSHOT_EXPERIMENTS = 4096


class WritingResearchSnapshotReader:
    """Build and verify one revision-pinned, stage-neutral Writing input cut."""

    def __init__(
        self,
        research_graph: ResearchGraphInterface,
        advancement_engine: AdvancementEngineInterface,
        research_memory: ResearchMemoryInterface,
        agent_runtime: AgentRuntimeInterface,
    ) -> None:
        self._research_graph = research_graph
        self._advancement_engine = advancement_engine
        self._research_memory = research_memory
        self._agent_runtime = agent_runtime

    def capture(self, quest_ref: str) -> dict[str, object]:
        for _attempt in range(_MAX_CONSISTENCY_ATTEMPTS):
            before = self._owner_revisions()
            # AR participates in the before/after cut because accepted Stage
            # executions are read below, but its global revision is not part of
            # the frozen research basis: this Writing Run's own checkpoints
            # also advance AR and must not make its Snapshot stale by itself.
            research_revisions = {
                owner: revision
                for owner, revision in before.items()
                if owner != "agent_runtime"
            }
            snapshot = self._capture_once(quest_ref, research_revisions)
            if self._owner_revisions() == before:
                return snapshot
        raise OwnerConflict("writing_snapshot_consistency_unavailable")

    def verify_current(self, snapshot: dict[str, object]) -> None:
        quest_ref = snapshot.get("quest_ref")
        if not isinstance(quest_ref, str) or not quest_ref:
            raise OwnerConflict("writing_snapshot_invalid")
        current = self.capture(quest_ref)
        if self.currentness_hash(current) != self.currentness_hash(snapshot):
            raise OwnerConflict("writing_snapshot_stale")

    @staticmethod
    def currentness_hash(snapshot: dict[str, object]) -> str:
        """Hash the Quest research basis without global Owner bookkeeping.

        Global RG/RM revisions also advance when this Writing Run accepts a
        deliverable or citation decision. They prove the capture was a coherent
        cut, but are not themselves research input and therefore cannot make the
        frozen report stale.
        """

        basis = {
            key: value
            for key, value in snapshot.items()
            if key not in {"owner_revisions", "snapshot_ref", "snapshot_hash"}
        }
        return canonical_hash(basis)

    def _owner_revisions(self) -> dict[str, int]:
        return {
            "research_graph": self._research_graph.query_snapshot().revision,
            "research_memory": self._research_memory.query_snapshot().revision,
            "advancement_engine": self._advancement_engine.query_snapshot().revision,
            "agent_runtime": self._agent_runtime.query_snapshot().revision,
        }

    def _capture_once(
        self, quest_ref: str, owner_revisions: dict[str, int]
    ) -> dict[str, object]:
        quest = self._research_graph.query_quest_by_ref(quest_ref)
        if quest is None:
            raise OwnerConflict("writing_quest_not_found")
        questions: list[dict[str, object]] = []
        for question in self._research_graph.query_question_tree(quest_ref):
            content = self._research_memory.read_question_content(
                question.content_ref, question.content_hash
            )
            questions.append(
                {
                    "question_ref": question.question_ref,
                    "parent_question_ref": question.parent_question_ref,
                    "content_ref": question.content_ref,
                    "content_hash": question.content_hash,
                    "content": content,
                    "receipt": question.receipt.as_public_dict(),
                }
            )
        sources: list[dict[str, object]] = []
        for role in self._research_graph.query_asset_roles(quest_ref=quest_ref):
            asset = self._research_memory.query_asset_version(role.version_ref)
            if asset is None:
                raise OwnerConflict("writing_snapshot_asset_missing")
            self._research_memory.verify_asset_binding(
                asset_ref=asset.asset_ref,
                version_ref=asset.version_ref,
                content_hash=asset.content_hash,
                manifest_hash=asset.manifest_hash,
                receipt=asset.receipt,
            )
            sources.append(
                {
                    "role": role.role,
                    "role_ref": role.role_ref,
                    "version_ref": asset.version_ref,
                    "asset_ref": asset.asset_ref,
                    "content_hash": asset.content_hash,
                    "manifest_hash": asset.manifest_hash,
                    "display_name": asset.display_name,
                    "media_type": asset.media_type,
                    "asset_receipt": asset.receipt.as_public_dict(),
                    "role_receipt": role.receipt.as_public_dict(),
                }
            )
        payload: dict[str, object] = {
            "schema_ref": WRITING_RESEARCH_SNAPSHOT_SCHEMA,
            "quest_ref": quest_ref,
            "quest": {
                "quest_ref": quest.quest_ref,
                "draft_revision": quest.draft_revision,
                "draft_hash": quest.draft_hash,
                "draft": quest.draft,
                "receipt": quest.receipt.as_public_dict(),
            },
            "questions": questions,
            "accepted_sources": sources,
            "advancement": self._advancement_snapshot(
                quest.initialization_id, quest_ref
            ),
            "owner_revisions": owner_revisions,
        }
        basis_hash = canonical_hash(payload)
        snapshot = {
            **payload,
            "snapshot_ref": f"writing_snapshot_{basis_hash[:32]}",
        }
        return {**snapshot, "snapshot_hash": canonical_hash(snapshot)}

    def _advancement_snapshot(
        self, initialization_id: str, quest_ref: str
    ) -> dict[str, object]:
        cycle = self._advancement_engine.query_initial_cycle(initialization_id)
        if cycle is None:
            return {
                "cycle": None,
                "stages": self._empty_stages(),
                "experiments": [],
            }

        stages = self._empty_stages()
        for stage in ("idea", "plan"):
            value = self._stage_value(cycle.cycle_ref, stage)
            stages[stage] = {
                "status": "accepted" if value is not None else "not_accepted",
                "accepted": value,
            }
        return {
            "cycle": {
                "cycle_ref": cycle.cycle_ref,
                "receipt": cycle.receipt.as_public_dict(),
            },
            "stages": stages,
            "experiments": self._experiment_closure(quest_ref),
        }

    @staticmethod
    def _empty_stages() -> dict[str, dict[str, object]]:
        return {
            "idea": {"status": "not_accepted", "accepted": None},
            "plan": {"status": "not_accepted", "accepted": None},
            "bundle": {"status": "not_available_in_runtime", "accepted": None},
            "reasoning": {
                "status": "not_available_in_runtime",
                "accepted": None,
            },
        }

    def _stage_value(
        self, cycle_ref: str, stage: str
    ) -> dict[str, object] | None:
        request = (
            self._advancement_engine.query_idea_stage_request(cycle_ref)
            if stage == "idea"
            else self._advancement_engine.query_plan_stage_request(cycle_ref)
        )
        if request is None:
            return None
        commit = (
            self._advancement_engine.query_idea_stage_commit(request.request_ref)
            if stage == "idea"
            else self._advancement_engine.query_plan_stage_commit(request.request_ref)
        )
        if commit is None:
            return None
        stage_run = (
            self._agent_runtime.query_idea_stage_run(request.request_ref)
            if stage == "idea"
            else self._agent_runtime.query_plan_stage_run(request.request_ref)
        )
        if (
            stage_run is None
            or stage_run.execution is None
            or stage_run.run_ref != commit.run_ref
        ):
            raise OwnerConflict("writing_stage_result_missing")
        submission_ref = stage_run.execution.submission_ref
        if stage == "idea":
            content = self._research_memory.query_idea_outcome_content(submission_ref)
            decision = self._research_graph.query_idea_outcome_decision(submission_ref)
            if (
                content is None
                or decision is None
                or decision.decision != "accepted"
                or decision.outcome_ref != commit.outcome_ref
                or content.outcome_hash != decision.outcome_hash
            ):
                raise OwnerConflict("writing_stage_result_invalid")
            result = {
                "content_ref": content.content_ref,
                "content_hash": content.payload_hash,
                "outcome_hash": content.outcome_hash,
                "outcome": content.outcome,
                "content_receipt": content.receipt.as_public_dict(),
                "acceptance_receipt": decision.receipt.as_public_dict(),
            }
        else:
            plan = self._research_memory.query_plan_document(submission_ref)
            decision = self._research_graph.query_formal_plan_decision(submission_ref)
            if (
                plan is None
                or decision is None
                or decision.decision != "accepted"
                or decision.formal_plan_ref != commit.outcome_ref
                or plan.plan_document_hash != decision.plan_document_hash
            ):
                raise OwnerConflict("writing_stage_result_invalid")
            result = {
                "content_ref": plan.content_ref,
                "content_hash": plan.payload_hash,
                "plan_document_hash": plan.plan_document_hash,
                "plan_document": plan.plan_document,
                "content_receipt": plan.receipt.as_public_dict(),
                "acceptance_receipt": decision.receipt.as_public_dict(),
            }
        return {
            "commit_ref": commit.commit_ref,
            "request_ref": request.request_ref,
            "epoch": request.epoch,
            "outcome_ref": commit.outcome_ref,
            "outcome_kind": commit.outcome_kind,
            "disposition": commit.disposition,
            "receipt": commit.receipt.as_public_dict(),
            "result": result,
        }

    def _experiment_closure(self, quest_ref: str) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        cursor_time = 0.0
        cursor_ref = ""
        while True:
            refs = self._research_graph.query_experiment_admission_refs(
                after_created_at=cursor_time,
                after_evaluation_attempt_ref=cursor_ref,
                limit=64,
            )
            if not refs:
                break
            for evaluation_attempt_ref, created_at in refs:
                if (created_at, evaluation_attempt_ref) <= (
                    cursor_time,
                    cursor_ref,
                ):
                    raise OwnerConflict("writing_experiment_pagination_invalid")
                cursor_time, cursor_ref = created_at, evaluation_attempt_ref
                admission = self._research_graph.query_experiment(
                    evaluation_attempt_ref
                )
                if (
                    admission is None
                    or admission.execution_request.quest_ref != quest_ref
                ):
                    continue
                if len(result) >= _MAX_SNAPSHOT_EXPERIMENTS:
                    raise OwnerConflict(
                        "writing_snapshot_experiment_limit_exceeded"
                    )
                run = self._agent_runtime.query_experiment_run(
                    evaluation_attempt_ref
                )
                result.append(
                    {
                        "evaluation_attempt_ref": evaluation_attempt_ref,
                        "execution_request": (
                            admission.execution_request.as_public_dict()
                        ),
                        "formal_measurement_status": (
                            admission.formal_measurement_status
                        ),
                        "formal_rejection_code": admission.formal_rejection_code,
                        "asset_roles": [
                            role.as_public_dict()
                            for role in self._research_graph.query_experiment_asset_roles(
                                evaluation_attempt_ref
                            )
                        ],
                        "run": None
                        if run is None
                        else {
                            "run_ref": run.run_ref,
                            "attempt_ref": run.attempt_ref,
                            "fence_ref": run.fence_ref,
                            "status": run.status,
                            "result_hash": (
                                None
                                if run.result is None
                                else run.result.result_hash
                            ),
                        },
                    }
                )
        return result
