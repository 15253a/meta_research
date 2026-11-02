from __future__ import annotations

from meta_research.bundle_protocol import projection_plain_value
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    StageCommit,
    StageRunRequest,
)
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.target_commit_evidence import TARGET_COMMIT_EVIDENCE_MEDIA_TYPE


_MAX_CONSISTENCY_ATTEMPTS = 3


class AcceptedResearchReader:
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

    @staticmethod
    def _history_entry_order(
        entry: tuple[StageRunRequest | None, StageCommit | None],
    ) -> tuple[int, int]:
        fact = entry[1] or entry[0]
        assert fact is not None
        return fact.epoch, ("idea", "plan", "bundle", "reasoning").index(fact.stage)

    @staticmethod
    def _empty_stages() -> dict[str, dict[str, object]]:
        return {
            "idea": {"status": "not_accepted", "accepted": None},
            "plan": {"status": "not_accepted", "accepted": None},
            "bundle": {"status": "not_accepted", "accepted": None},
            "reasoning": {"status": "not_accepted", "accepted": None},
        }

    def _stage_value(
        self, request: StageRunRequest, commit: StageCommit
    ) -> dict[str, object]:
        stage = request.stage
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
            raise OwnerConflict("research_stage_result_missing")
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
                raise OwnerConflict("research_stage_result_invalid")
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
                raise OwnerConflict("research_stage_result_invalid")
            result = {
                "content_ref": plan.content_ref,
                "content_hash": plan.payload_hash,
                "plan_document_hash": plan.plan_document_hash,
                "plan_document": plan.plan_document,
                "content_receipt": plan.receipt.as_public_dict(),
                "acceptance_receipt": decision.receipt.as_public_dict(),
            }
        return {**self._commit_value(commit), "result": result}

    def _reasoning_stage_value(
        self, request: StageRunRequest, commit: StageCommit
    ) -> dict[str, object]:
        run = self._agent_runtime.query_reasoning_stage_run(request.request_ref)
        if (
            run is None
            or run.execution is None
            or run.completion is None
            or run.run_ref != commit.run_ref
            or run.completion.receipt != commit.run_completion_receipt
        ):
            raise OwnerConflict("research_reasoning_result_missing")
        submission_ref = run.execution.submission_ref
        content = self._research_memory.query_reasoning_content(submission_ref)
        decision = self._research_graph.query_reasoning_outcome_decision(submission_ref)
        if (
            content is None
            or decision is None
            or decision.decision != "accepted"
            or content.request_ref != request.request_ref
            or content.cycle_ref != request.cycle_ref
            or content.foreground_epoch != request.epoch
            or content.run_ref != commit.run_ref
            or content.stage_request_receipt != request.receipt
            or content.outcome_hash != decision.outcome_hash
            or content.content_ref != decision.content_ref
            or content.transition_hash != decision.transition_hash
            or decision.request_ref != request.request_ref
            or decision.run_ref != commit.run_ref
            or decision.outcome_ref != commit.outcome_ref
            or decision.receipt != commit.outcome_receipt
            or run.completion.outcome_ref != commit.outcome_ref
            or run.completion.decision_receipt != decision.receipt
        ):
            raise OwnerConflict("research_reasoning_result_invalid")
        return {
            **self._commit_value(commit),
            "result": {
                "content_ref": content.content_ref,
                "content_hash": content.payload_hash,
                "outcome_hash": content.outcome_hash,
                "outcome": content.outcome,
                "transition_kind": content.transition_kind,
                "transition_ref": content.transition_ref,
                "transition_hash": content.transition_hash,
                "frozen_evidence_closure": projection_plain_value(
                    content.frozen_evidence_closure
                ),
                "content_receipt": content.receipt.as_public_dict(),
                "acceptance_receipt": decision.receipt.as_public_dict(),
            },
        }

    def _bundle_stage_value(
        self, request: StageRunRequest, commit: StageCommit | None
    ) -> dict[str, object] | None:
        """Read only immutable Bundle acceptances from public AE/AR seams.

        The AR StageRun is used solely as a stable request-to-run lookup and,
        after an AE StageCommit exists, to recheck its closed RunCompletion.
        Its live status, worker cursor, foreground, and checkpoint state never
        become Writing input.
        """

        cycle_ref = request.cycle_ref
        accepted_plan = request.accepted_formal_plan
        if (
            request.stage != "bundle"
            or request.cycle_ref != cycle_ref
            or accepted_plan is None
        ):
            raise OwnerConflict("research_bundle_request_invalid")

        stage_run = self._agent_runtime.query_bundle_stage_run(request.request_ref)
        if stage_run is not None and (
            stage_run.request_ref != request.request_ref
            or stage_run.cycle_ref != cycle_ref
            or stage_run.stage != "bundle"
            or stage_run.epoch != request.epoch
        ):
            raise OwnerConflict("research_bundle_result_invalid")

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
                raise OwnerConflict("research_bundle_result_invalid")
            if (
                report.request_ref != request.request_ref
                or report.run_ref != stage_run.run_ref
                or report.report.stage_request_ref != request.request_ref
                or report.formal_plan_ref != accepted_plan.formal_plan_ref
                or report.report.formal_plan_ref != accepted_plan.formal_plan_ref
                or report.plan_document_hash != accepted_plan.plan_document_hash
            ):
                raise OwnerConflict("research_bundle_result_invalid")

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
                raise OwnerConflict("research_bundle_result_invalid")

        if commit is None and report is None:
            return None
        if commit is not None and (
            commit.request_ref != request.request_ref
            or commit.cycle_ref != cycle_ref
            or commit.stage != "bundle"
            or commit.epoch != request.epoch
        ):
            raise OwnerConflict("research_bundle_result_invalid")

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
                raise OwnerConflict("research_bundle_result_invalid")
            run_completion = stage_run.completion
            if (
                run_completion.request_ref != request.request_ref
                or run_completion.run_ref != report.run_ref
                or run_completion.outcome_ref != report.report_ref
                or run_completion.decision_receipt != report.receipt
            ):
                raise OwnerConflict("research_bundle_result_invalid")
        elif commit is not None and commit.disposition == "exhausted":
            if (
                stage_run is None
                or stage_run.completion is None
                or commit.run_ref != stage_run.run_ref
                or commit.run_completion_receipt != stage_run.completion.receipt
                or commit.basis_ref is None
                or stage_run.completion.outcome_ref != commit.basis_ref
            ):
                raise OwnerConflict("research_bundle_result_invalid")
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
            raise OwnerConflict("research_bundle_result_invalid")

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
            "commit": None if commit is None else self._commit_value(commit),
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
    def _commit_value(commit) -> dict[str, object]:
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
