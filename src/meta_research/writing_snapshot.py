from __future__ import annotations

from typing import cast

from meta_research.bundle_protocol import projection_plain_value
from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.writing_contract import WRITING_RESEARCH_SNAPSHOT_SCHEMA


_MAX_CONSISTENCY_ATTEMPTS = 3


class WritingResearchSnapshotReader:
    """Capture one accepted-fact-pinned, stage-neutral Writing input cut."""

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
            owner_revisions = self._snapshot_metadata_revisions()
            candidate = self._capture_once(quest_ref, owner_revisions)
            # Global Owner revisions also carry unrelated asset intake, Target
            # stdout, and checkpoint progress.
            # Re-read only the exact accepted facts consumed by Writing: a
            # meaningful Quest/Stage/Bundle fact already inside this cut
            # changes this value, while progress outside the frozen basis does
            # Cross-owner receipt checks inside _capture_once still reject
            # partial acceptance facts.
            verified = self._capture_once(quest_ref, owner_revisions)
            if verified == candidate:
                return verified
        raise OwnerConflict("writing_snapshot_consistency_unavailable")

    def _snapshot_metadata_revisions(self) -> dict[str, int]:
        """Return observed lower-bound metadata, never a consistency gate."""

        return {
            "research_graph": self._research_graph.query_snapshot().revision,
            "research_memory": self._research_memory.query_snapshot().revision,
            "advancement_engine": self._advancement_engine.query_snapshot().revision,
        }

    def _capture_once(
        self,
        quest_ref: str,
        owner_revisions: dict[str, int],
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
            "advancement": self._advancement_snapshot(quest.initialization_id),
            "owner_revisions": owner_revisions,
        }
        basis_hash = canonical_hash(payload)
        snapshot = {
            **payload,
            "snapshot_ref": f"writing_snapshot_{basis_hash[:32]}",
        }
        return {**snapshot, "snapshot_hash": canonical_hash(snapshot)}

    def _advancement_snapshot(
        self,
        initialization_id: str,
    ) -> dict[str, object]:
        cycle = self._advancement_engine.query_initial_cycle(initialization_id)
        if cycle is None:
            return {
                "cycle": None,
                "stages": self._empty_stages(),
            }

        stages = self._empty_stages()
        for stage in ("idea", "plan"):
            value = self._stage_value(cycle.cycle_ref, stage)
            stages[stage] = {
                "status": "accepted" if value is not None else "not_accepted",
                "accepted": value,
            }
        bundle = self._bundle_stage_value(cycle.cycle_ref)
        if bundle is not None:
            stages["bundle"] = {
                "status": (
                    "accepted"
                    if bundle["commit"] is not None
                    else "report_accepted"
                ),
                "accepted": bundle,
            }
        return {
            "cycle": {
                "cycle_ref": cycle.cycle_ref,
                "receipt": cycle.receipt.as_public_dict(),
            },
            "stages": stages,
        }

    @staticmethod
    def _empty_stages() -> dict[str, dict[str, object]]:
        return {
            "idea": {"status": "not_accepted", "accepted": None},
            "plan": {"status": "not_accepted", "accepted": None},
            "bundle": {"status": "not_accepted", "accepted": None},
            "reasoning": {"status": "not_accepted", "accepted": None},
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

    def _bundle_stage_value(self, cycle_ref: str) -> dict[str, object] | None:
        """Read only immutable Bundle acceptances from public AE/AR seams.

        The AR StageRun is used solely as a stable request-to-run lookup and,
        after an AE StageCommit exists, to recheck its closed RunCompletion.
        Its live status, worker cursor, foreground, and checkpoint state never
        become Writing input.
        """

        request = self._advancement_engine.query_bundle_stage_request(cycle_ref)
        if request is None:
            return None
        accepted_plan = request.accepted_formal_plan
        if (
            request.stage != "bundle"
            or request.cycle_ref != cycle_ref
            or accepted_plan is None
        ):
            raise OwnerConflict("writing_bundle_request_invalid")

        stage_run = self._agent_runtime.query_bundle_stage_run(request.request_ref)
        if stage_run is not None and (
            stage_run.request_ref != request.request_ref
            or stage_run.cycle_ref != cycle_ref
            or stage_run.stage != "bundle"
            or stage_run.epoch != request.epoch
        ):
            raise OwnerConflict("writing_bundle_result_invalid")

        report = (
            None
            if stage_run is None
            else self._agent_runtime.query_bundle_run_report(stage_run.run_ref)
        )
        if report is not None:
            verified_report = self._agent_runtime.verify_bundle_report_receipt(
                report_ref=report.report_ref,
                receipt=report.receipt,
            )
            if verified_report != report:
                raise OwnerConflict("writing_bundle_result_invalid")
            if (
                report.request_ref != request.request_ref
                or report.run_ref != stage_run.run_ref
                or report.report.stage_request_ref != request.request_ref
                or report.formal_plan_ref != accepted_plan.formal_plan_ref
                or report.report.formal_plan_ref != accepted_plan.formal_plan_ref
                or report.plan_document_hash != accepted_plan.plan_document_hash
                or report.formal_plan_content_receipt
                != accepted_plan.content_receipt
            ):
                raise OwnerConflict("writing_bundle_result_invalid")

        disposition = (
            None
            if report is None
            else self._advancement_engine.query_bundle_report_disposition(
                report.report_ref
            )
        )
        verified_disposition = None
        if disposition is not None:
            verified_disposition = (
                self._advancement_engine.verify_bundle_report_disposition_receipt(
                    disposition_ref=disposition.disposition_ref,
                    receipt=disposition.receipt,
                )
            )
            if (
                verified_disposition.request_ref != request.request_ref
                or verified_disposition.cycle_ref != cycle_ref
                or verified_disposition.epoch != request.epoch
                or verified_disposition.run_ref != report.run_ref
                or verified_disposition.report_ref != report.report_ref
                or verified_disposition.report_hash != report.report_hash
                or verified_disposition.disposition != report.report.disposition
            ):
                raise OwnerConflict("writing_bundle_result_invalid")

        commit = self._advancement_engine.query_bundle_stage_commit(
            request.request_ref
        )
        if commit is None and report is None:
            return None
        if commit is not None and (
            commit.request_ref != request.request_ref
            or commit.cycle_ref != cycle_ref
            or commit.stage != "bundle"
            or commit.epoch != request.epoch
        ):
            raise OwnerConflict("writing_bundle_result_invalid")

        run_completion = None
        if commit is not None and commit.disposition == "completed":
            if (
                report is None
                or stage_run is None
                or stage_run.completion is None
                or commit.run_ref != report.run_ref
                or commit.outcome_ref != report.report_ref
                or commit.outcome_kind != "bundle_report"
                or commit.outcome_receipt != report.receipt
                or commit.run_completion_receipt != stage_run.completion.receipt
            ):
                raise OwnerConflict("writing_bundle_result_invalid")
            run_completion = stage_run.completion
            if (
                run_completion.request_ref != request.request_ref
                or run_completion.run_ref != report.run_ref
                or run_completion.outcome_ref != report.report_ref
                or run_completion.decision_receipt != report.receipt
            ):
                raise OwnerConflict("writing_bundle_result_invalid")
        elif commit is not None and commit.disposition == "exhausted":
            if (
                stage_run is None
                or stage_run.completion is None
                or commit.run_ref != stage_run.run_ref
                or commit.run_completion_receipt != stage_run.completion.receipt
                or commit.basis_ref is None
                or stage_run.completion.outcome_ref != commit.basis_ref
            ):
                raise OwnerConflict("writing_bundle_result_invalid")
            run_completion = stage_run.completion
        elif commit is not None and (
            commit.disposition != "skipped"
            or commit.outcome_kind != "bundle_skip"
            or commit.run_ref is not None
            or report is not None
            or commit.outcome_ref != accepted_plan.formal_plan_ref
            or commit.outcome_receipt != accepted_plan.formal_plan_receipt
            or commit.run_completion_receipt is not None
        ):
            raise OwnerConflict("writing_bundle_result_invalid")

        return {
            "request": {
                "request_ref": request.request_ref,
                "cycle_ref": request.cycle_ref,
                "epoch": request.epoch,
                "accepted_formal_plan": accepted_plan.as_dict(),
                "receipt": request.receipt.as_public_dict(),
            },
            "report": (
                None if report is None else self._bundle_report_value(report)
            ),
            "report_disposition": (
                None
                if verified_disposition is None
                else {
                    "disposition_ref": verified_disposition.disposition_ref,
                    "request_ref": verified_disposition.request_ref,
                    "cycle_ref": verified_disposition.cycle_ref,
                    "epoch": verified_disposition.epoch,
                    "run_ref": verified_disposition.run_ref,
                    "report_ref": verified_disposition.report_ref,
                    "report_hash": verified_disposition.report_hash,
                    "disposition": verified_disposition.disposition,
                    "status": verified_disposition.status,
                    "next_stage": verified_disposition.next_stage,
                    "next_epoch": verified_disposition.next_epoch,
                    "receipt": verified_disposition.receipt.as_public_dict(),
                }
            ),
            "run_completion": (
                None
                if run_completion is None
                else {
                    "request_ref": run_completion.request_ref,
                    "run_ref": run_completion.run_ref,
                    "attempt_ref": run_completion.attempt_ref,
                    "outcome_ref": run_completion.outcome_ref,
                    "decision_receipt": (
                        run_completion.decision_receipt.as_public_dict()
                    ),
                    "receipt": run_completion.receipt.as_public_dict(),
                }
            ),
            "commit": None if commit is None else self._bundle_commit_value(commit),
        }

    @staticmethod
    def _bundle_report_value(report) -> dict[str, object]:
        return {
            "report_ref": report.report_ref,
            "request_ref": report.request_ref,
            "run_ref": report.run_ref,
            "attempt_ref": report.attempt_ref,
            "fence_ref": report.fence_ref,
            "formal_plan_ref": report.formal_plan_ref,
            "plan_document_hash": report.plan_document_hash,
            "formal_plan_content_receipt": (
                report.formal_plan_content_receipt.as_public_dict()
            ),
            "formal_plan_projection_digest": (
                report.formal_plan_projection_digest
            ),
            "formal_plan_projection_receipt": (
                report.formal_plan_projection_receipt.as_public_dict()
            ),
            "completion_contract_hash": report.completion_contract_hash,
            "formal_plan_briefs_hash": report.formal_plan_briefs_hash,
            "target_graph_ref": report.target_graph_ref,
            "target_graph_generation": report.target_graph_generation,
            "target_set_hash": report.target_set_hash,
            "coverage_hash": report.coverage_hash,
            "target_graph_receipt": report.target_graph_receipt.as_public_dict(),
            "target_refs": list(report.target_refs),
            "notice_refs": list(report.notice_refs),
            "handoff_manifest_refs": list(report.handoff_manifest_refs),
            "accepted_measurement_closures": projection_plain_value(
                report.accepted_measurement_closures
            ),
            "target_commit_receipts": [
                receipt.as_public_dict()
                for receipt in report.target_commit_receipts
            ],
            "report": projection_plain_value(report.report),
            "report_hash": report.report_hash,
            "receipt": report.receipt.as_public_dict(),
        }

    @staticmethod
    def _bundle_commit_value(commit) -> dict[str, object]:
        return {
            "commit_ref": commit.commit_ref,
            "request_ref": commit.request_ref,
            "cycle_ref": commit.cycle_ref,
            "epoch": commit.epoch,
            "run_ref": commit.run_ref,
            "outcome_ref": commit.outcome_ref,
            "outcome_kind": commit.outcome_kind,
            "disposition": commit.disposition,
            "run_completion_receipt": (
                None
                if commit.run_completion_receipt is None
                else commit.run_completion_receipt.as_public_dict()
            ),
            "outcome_receipt": (
                None
                if commit.outcome_receipt is None
                else commit.outcome_receipt.as_public_dict()
            ),
            "basis_kind": commit.basis_kind,
            "basis_ref": commit.basis_ref,
            "basis_receipt": (
                None
                if commit.basis_receipt is None
                else commit.basis_receipt.as_public_dict()
            ),
            "closure_hash": (
                None if commit.closure is None else canonical_hash(commit.closure)
            ),
            "receipt": commit.receipt.as_public_dict(),
        }
