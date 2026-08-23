from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from meta_research.feed import DurableFeed
from meta_research.idea_stage import _public_run
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    StageCommit,
    StageRunRequest,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    AttemptExecution,
    PlanRuntimeBinding,
    PlanStageRun,
)
from meta_research.owners.common import (
    AcceptedIdeaSetBinding,
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.owners.research_graph import (
    AcceptedQuestion,
    FormalPlanDecision,
    ResearchGraphInterface,
)
from meta_research.owners.research_memory import (
    AcceptedPlanDocument,
    ResearchMemoryInterface,
)
from meta_research.plan_contract import (
    PLAN_CONTEXT_PACK_SCHEMA_REF,
    material_plan_hash,
)
from meta_research.plan_skill import (
    PlanSkillContractError,
    PlanSkillDraft,
    PlanSkillProvider,
    PlanSkillRequest,
    PlanSkillUnavailable,
    review_record,
    validate_plan_skill_draft,
    validate_plan_skill_result,
)


_CYCLE_EVENT = "advancement_engine.initial_cycle_activated"


@dataclass(frozen=True)
class _CurrentCycle:
    revision: int
    cycle_ref: str
    question: AcceptedQuestion


@dataclass(frozen=True)
class _EligiblePlan:
    current: _CurrentCycle
    idea_request: StageRunRequest
    idea_commit: StageCommit
    accepted_idea_set: AcceptedIdeaSetBinding


@dataclass(frozen=True)
class _Qualification:
    eligible: _EligiblePlan | None
    reason_code: str | None = None
    next_stage: str | None = None
    idea_outcome_ref: str | None = None


@dataclass(frozen=True)
class _CycleStep:
    advanced: bool
    provider_boundary_attempted: bool = False


class PlanStageWorker:
    """Recoverable Plan application loop composed only through Owner seams.

    Plan is admitted automatically after an accepted IdeaSet StageCommit. One
    pass crosses at most one durable Owner boundary or one potentially long
    provider boundary across all Cycles. The worker owns no truth: every
    restart rebuilds eligibility and lineage from AE, AR, RM, and RG receipts.
    """

    def __init__(
        self,
        feed: DurableFeed,
        advancement_engine: AdvancementEngineInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        research_graph: ResearchGraphInterface,
        provider: PlanSkillProvider,
    ) -> None:
        self._feed = feed
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._provider = provider
        self._transient_error: str | None = None
        self._provider_cursor_cycle_ref: str | None = None

    @property
    def transient_error(self) -> str | None:
        return self._transient_error

    def process_once(self) -> bool:
        """Advance recoverable Plan work without requiring a per-Run click."""

        transient_error: str | None = None
        if self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=("plan_primary", "plan_review"),
        ):
            return True
        self._transient_error = None
        cycles = list(self._discover_cycles())
        if self._provider_cursor_cycle_ref is not None:
            cursor = next(
                (
                    index
                    for index, cycle in enumerate(cycles)
                    if cycle.cycle_ref == self._provider_cursor_cycle_ref
                ),
                None,
            )
            if cursor is not None:
                cycles = cycles[cursor + 1 :] + cycles[: cursor + 1]
        for current in cycles:
            qualification = self._qualify(current)
            if qualification.eligible is None:
                continue
            step = self._process_cycle(qualification.eligible)
            if self._transient_error is not None:
                transient_error = transient_error or self._transient_error
            if step.provider_boundary_attempted:
                self._provider_cursor_cycle_ref = current.cycle_ref
            # A pass is a single durable transaction boundary globally, not
            # one boundary per active Quest. The next daemon tick resumes at
            # the next missing boundary from fresh Owner reads.
            if step.advanced:
                self._transient_error = transient_error
                return True
            if step.provider_boundary_attempted:
                break
        self._transient_error = transient_error
        return False

    def _process_cycle(self, eligible: _EligiblePlan) -> _CycleStep:
        current = eligible.current
        request = self._advancement_engine.query_plan_stage_request(
            current.cycle_ref
        )
        if request is None:
            context_pack = self._context_pack(eligible)
            self._advancement_engine.ensure_plan_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                accepted_idea_set=eligible.accepted_idea_set,
                context_pack=context_pack,
                idempotency_key=_operation_key(
                    "plan-request", current.cycle_ref, "worker"
                ),
            )
            return _CycleStep(True)
        self._assert_request_eligibility(request, eligible)

        run = self._agent_runtime.query_plan_stage_run(request.request_ref)
        if run is None:
            provider_safe = True
            try:
                runtime_binding = self._provider.runtime_binding()
            except PlanSkillUnavailable as error:
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            self._agent_runtime.admit_plan_stage(
                request,
                _operation_key("plan-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
            return _CycleStep(True, provider_boundary_attempted=True)
        managed = self._agent_runtime.query_managed_run(run.run_ref)
        if managed is not None and managed["status"] not in {"running", "completed"}:
            return _CycleStep(False)
        if self._advancement_engine.query_plan_stage_commit(request.request_ref):
            return _CycleStep(False)

        execution = run.execution
        if execution is None:
            return self._execute_attempt(eligible, request, run)

        content = self._query_plan_document(execution.submission_ref)
        if content is None:
            self._accept_plan_document(request, execution)
            return _CycleStep(True)

        decision = self._query_formal_plan_decision(execution.submission_ref)
        if decision is None:
            self._decide_formal_plan(request, content, execution.receipt)
            return _CycleStep(True)

        if decision.decision == "rejected":
            self._agent_runtime.continue_after_plan_rejection(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "plan-revise",
                    run.run_ref,
                    run.attempt_ref,
                    decision.receipt.receipt_ref,
                ),
            )
            return _CycleStep(True)
        if decision.decision != "accepted" or decision.formal_plan_ref is None:
            raise OwnerConflict("formal_plan_decision_invalid")

        if run.completion is None:
            self._agent_runtime.complete_plan_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                formal_plan_ref=decision.formal_plan_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "plan-complete",
                    run.run_ref,
                    run.attempt_ref,
                    decision.formal_plan_ref,
                ),
            )
            return _CycleStep(True)

        self._advancement_engine.commit_plan_stage(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            formal_plan_ref=decision.formal_plan_ref,
            run_completion_receipt=run.completion.receipt,
            formal_plan_receipt=decision.receipt,
            idempotency_key=_operation_key(
                "plan-commit", request.request_ref, decision.formal_plan_ref
            ),
        )
        return _CycleStep(True)

    def query_current(self) -> dict[str, object]:
        """Compose the five independent public Plan Stage fact layers."""

        current = self._discover_current_cycle()
        if current is None:
            return _not_eligible_projection(
                cycle_ref=None,
                question_ref=None,
                idea_outcome_ref=None,
                reason_code="accepted_cycle_unavailable",
                next_stage=None,
            )
        qualification = self._qualify(current)
        if qualification.eligible is None:
            return _not_eligible_projection(
                cycle_ref=current.cycle_ref,
                question_ref=current.question.question_ref,
                idea_outcome_ref=qualification.idea_outcome_ref,
                reason_code=qualification.reason_code
                or "accepted_idea_set_unavailable",
                next_stage=qualification.next_stage,
            )
        eligible = qualification.eligible
        request = self._advancement_engine.query_plan_stage_request(
            current.cycle_ref
        )
        if request is None:
            return {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": current.cycle_ref,
                    "question_ref": current.question.question_ref,
                    "idea_outcome_ref": eligible.accepted_idea_set.outcome_ref,
                    "reason": None,
                    "next_stage": "Plan",
                },
                "stage_run_request": None,
                "run": None,
                "plan_acceptance": _not_attempted_acceptance(),
                "stage_commit": None,
            }
        self._assert_request_eligibility(request, eligible)
        run = self._agent_runtime.query_plan_stage_run(request.request_ref)
        commit = self._advancement_engine.query_plan_stage_commit(
            request.request_ref
        )
        execution = None if run is None else run.execution
        lineage_execution = execution
        if lineage_execution is None and run is not None:
            lineage_execution = run.predecessor_execution
        content, decision = self._accepted_facts(lineage_execution)
        return {
            "eligibility": {
                "status": "consumed" if commit is not None else "requested",
                "cycle_ref": current.cycle_ref,
                "question_ref": current.question.question_ref,
                "idea_outcome_ref": eligible.accepted_idea_set.outcome_ref,
                "reason": None,
                "next_stage": "Plan",
            },
            "stage_run_request": _public_request(request),
            "run": None if run is None else _public_run(run),
            "plan_acceptance": _public_acceptance(
                lineage_execution, content, decision
            ),
            "stage_commit": (
                None
                if commit is None
                else _public_commit(commit, decision=decision)
            ),
        }

    def query_current_question(self) -> dict[str, object] | None:
        current = self._discover_current_cycle()
        if current is None:
            return None
        content = self._accepted_question_content(current.question.as_binding())
        summary: dict[str, object] = {
            "quest_ref": current.question.quest_ref,
            "question_ref": current.question.question_ref,
            "graph_revision": self._research_graph.query_snapshot().revision,
        }
        for field in (
            "title",
            "unknown_statement",
            "answer_shape",
            "applicability_scope",
        ):
            value = content.get(field)
            if isinstance(value, str):
                summary[field] = value
        return summary

    def _execute_attempt(
        self,
        eligible: _EligiblePlan,
        request: StageRunRequest,
        run: PlanStageRun,
    ) -> _CycleStep:
        predecessor = run.predecessor_execution
        rejection = run.rejection_receipt
        decision: FormalPlanDecision | None = None
        predecessor_hash: str | None = None
        if (predecessor is None) != (rejection is None):
            raise OwnerConflict("rejection_lineage_incomplete")
        if predecessor is not None and rejection is not None:
            decision = self._query_formal_plan_decision(
                predecessor.submission_ref
            )
            if (
                decision is None
                or decision.decision != "rejected"
                or decision.receipt != rejection
                or decision.request_ref != request.request_ref
                or decision.run_ref != run.run_ref
                or decision.attempt_ref != predecessor.attempt_ref
                or decision.fence_ref != predecessor.fence_ref
                or not decision.feedback
                or run.native_session_ref is None
            ):
                raise OwnerConflict("rejection_lineage_invalid")
            predecessor_hash = material_plan_hash(predecessor.outcome)
        elif (
            run.attempt_generation != 1
            and run.technical_predecessor_attempt_ref is None
        ) or (
            (run.native_session_ref is None) != (run.primary_draft is None)
        ):
            raise OwnerConflict("attempt_lineage_invalid")

        accepted_content = self._accepted_question_content(
            request.accepted_question
        )
        if not isinstance(run.runtime_binding, PlanRuntimeBinding):
            raise OwnerConflict("plan_runtime_binding_invalid")
        try:
            runtime_binding = self._provider.runtime_binding()
        except PlanSkillUnavailable as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        if runtime_binding != run.runtime_binding:
            self._transient_error = "plan_runtime_binding_drift"
            return _CycleStep(False, provider_boundary_attempted=True)
        if request.accepted_idea_set is None:
            raise OwnerConflict("plan_idea_set_binding_missing")
        invocation = (
            run.primary_invocation
            if run.primary_draft is None
            else run.review_invocation
        )
        job_ref = invocation.operation_ref
        unit_ref = invocation.invocation_ref
        skill_request = PlanSkillRequest(
            stage_request_ref=request.request_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            idea_set_ref=request.accepted_idea_set.outcome_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            accepted_question_content=accepted_content,
            accepted_idea_set=request.accepted_idea_set.idea_set,
            root_session_ref=run.root_session_ref,
            submission_revision=run.attempt_generation,
            runtime_binding=run.runtime_binding,
            native_session_ref=run.native_session_ref,
            predecessor_submission_ref=(
                None if predecessor is None else predecessor.submission_ref
            ),
            owner_rejection_receipt_ref=(
                None if rejection is None else rejection.receipt_ref
            ),
            owner_feedback=() if decision is None else decision.feedback,
            job_ref=job_ref,
        )
        if run.primary_draft is None:
            self._agent_runtime.begin_provider_unit(
                unit_ref=unit_ref,
                operation_ref=job_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                unit_kind="plan_primary",
            )
            provider_safe = True
            try:
                try:
                    draft = self._provider.generate_draft(skill_request)
                    draft_hash = validate_plan_skill_draft(skill_request, draft)
                except PlanSkillUnavailable as error:
                    if error.code == "codex_operation_reconciliation_pending":
                        provider_safe = False
                    self._transient_error = error.code
                    return _CycleStep(False, provider_boundary_attempted=True)
                except PlanSkillContractError as error:
                    self._transient_error = str(error)
                    return _CycleStep(False, provider_boundary_attempted=True)
                checkpoint = self._agent_runtime.record_plan_primary_draft(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=draft.primary_session_ref,
                    runtime_binding=run.runtime_binding,
                    draft=draft.draft,
                    adapter_kind=draft.adapter_kind,
                    idempotency_key=_operation_key(
                        "plan-primary", run.run_ref, run.attempt_ref, draft_hash
                    ),
                )
                if checkpoint.draft_hash != draft_hash:
                    raise OwnerConflict("plan_primary_draft_hash_mismatch")
                self._transient_error = None
                return _CycleStep(True, provider_boundary_attempted=True)
            finally:
                if provider_safe:
                    self._agent_runtime.acknowledge_provider_safe_point(
                        unit_ref=unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                    )

        checkpoint = run.primary_draft
        draft = PlanSkillDraft(
            draft=checkpoint.draft,
            primary_session_ref=checkpoint.native_session_ref,
            adapter_kind=checkpoint.adapter_kind,
        )
        self._agent_runtime.begin_provider_unit(
            unit_ref=unit_ref,
            operation_ref=job_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            unit_kind="plan_review",
        )
        provider_safe = True
        try:
            try:
                result = self._provider.review_draft(skill_request, draft)
                draft_hash, plan_hash, _review_hash = validate_plan_skill_result(
                    skill_request,
                    result,
                    predecessor_material_plan_hash=predecessor_hash,
                )
            except PlanSkillUnavailable as error:
                if error.code == "codex_operation_reconciliation_pending":
                    provider_safe = False
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            except PlanSkillContractError as error:
                self._transient_error = str(error)
                return _CycleStep(False, provider_boundary_attempted=True)
            review = review_record(
                result,
                draft_hash=draft_hash,
                final_plan_hash=plan_hash,
            )
            submission_ref = "plan_submission_" + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "plan_hash": plan_hash,
                    "review_hash": canonical_hash(review),
                }
            )[:32]
            self._agent_runtime.record_plan_attempt_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=submission_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding=run.runtime_binding,
                plan=result.final_plan,
                reviewed_draft=result.reviewed_draft,
                review=review,
                idempotency_key=_operation_key(
                    "plan-execute", run.run_ref, run.attempt_ref
                ),
            )
            self._transient_error = None
            finish_job = getattr(self._provider, "finish_job", None)
            if callable(finish_job):
                finish_job(job_ref)
            return _CycleStep(True, provider_boundary_attempted=True)
        finally:
            if provider_safe:
                self._agent_runtime.acknowledge_provider_safe_point(
                    unit_ref=unit_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                )

    def _qualify(self, current: _CurrentCycle) -> _Qualification:
        idea_request = self._advancement_engine.query_idea_stage_request(
            current.cycle_ref
        )
        if idea_request is None:
            return _Qualification(None, "accepted_idea_set_unavailable", "Idea")
        idea_commit = self._advancement_engine.query_idea_stage_commit(
            idea_request.request_ref
        )
        if idea_commit is None:
            return _Qualification(None, "accepted_idea_set_unavailable", "Idea")
        if (
            idea_request.stage != "idea"
            or idea_commit.stage != "idea"
            or idea_commit.cycle_ref != current.cycle_ref
            or idea_commit.request_ref != idea_request.request_ref
            or idea_commit.disposition != "completed"
        ):
            raise OwnerConflict("idea_stage_commit_invalid")
        if idea_commit.outcome_kind == "no_viable_candidate":
            return _Qualification(
                None,
                "idea_no_viable_candidate",
                "Reasoning",
                idea_commit.outcome_ref,
            )
        if idea_commit.outcome_kind != "idea_set":
            raise OwnerConflict("idea_stage_commit_invalid")

        run = self._agent_runtime.query_idea_stage_run(idea_request.request_ref)
        if (
            run is None
            or run.completion is None
            or run.completion.outcome_ref != idea_commit.outcome_ref
            or run.completion.receipt != idea_commit.run_completion_receipt
            or run.execution is None
        ):
            raise OwnerConflict("accepted_idea_set_lineage_invalid")
        execution = run.execution
        content = self._research_memory.query_idea_outcome_content(
            execution.submission_ref
        )
        decision = self._research_graph.query_idea_outcome_decision(
            execution.submission_ref
        )
        if (
            content is None
            or decision is None
            or decision.decision != "accepted"
            or decision.outcome_kind != "idea_set"
            or decision.outcome_ref != idea_commit.outcome_ref
            or decision.receipt != idea_commit.outcome_receipt
            or content.request_ref != idea_request.request_ref
            or content.submission_ref != execution.submission_ref
            or content.execution_receipt != execution.receipt
            or content.outcome_kind != "idea_set"
            or content.outcome_hash != canonical_hash(content.outcome)
        ):
            raise OwnerConflict("accepted_idea_set_lineage_invalid")
        binding = AcceptedIdeaSetBinding(
            outcome_ref=decision.outcome_ref,
            content_ref=content.content_ref,
            payload_hash=content.payload_hash,
            outcome_hash=content.outcome_hash,
            content_receipt=content.receipt,
            outcome_receipt=decision.receipt,
            stage_commit_ref=idea_commit.commit_ref,
            stage_commit_receipt=idea_commit.receipt,
            idea_set=content.outcome,
        )
        return _Qualification(
            _EligiblePlan(current, idea_request, idea_commit, binding)
        )

    def _context_pack(self, eligible: _EligiblePlan) -> dict[str, object]:
        current = eligible.current
        evidence_revision, evidence_catalog = (
            self._research_graph.query_plan_evidence_catalog(
                quest_ref=current.question.quest_ref
            )
        )

        return {
            "schema_ref": PLAN_CONTEXT_PACK_SCHEMA_REF,
            "cycle_ref": current.cycle_ref,
            "accepted_question_binding": current.question.as_binding().as_dict(),
            "accepted_idea_set_binding": eligible.accepted_idea_set.as_dict(),
            "evidence_catalog": [dict(item) for item in evidence_catalog],
            "evidence_reference_revision": evidence_revision,
        }

    def _assert_request_eligibility(
        self, request: StageRunRequest, eligible: _EligiblePlan
    ) -> None:
        if (
            request.stage != "plan"
            or request.cycle_ref != eligible.current.cycle_ref
            or request.accepted_question
            != eligible.current.question.as_binding()
            or request.accepted_idea_set != eligible.accepted_idea_set
            or request.context_pack.get("accepted_idea_set_binding")
            != eligible.accepted_idea_set.as_dict()
            or canonical_hash(request.context_pack) != request.context_pack_hash
        ):
            raise OwnerConflict("plan_stage_request_eligibility_invalid")

    def _accepted_question_content(self, binding) -> dict[str, object]:
        content = self._research_memory.read_question_content(
            binding.content_ref, binding.content_hash
        )
        if canonical_hash(content) != binding.content_hash:
            raise OwnerConflict("accepted_question_content_mismatch")
        return content

    def _accepted_facts(
        self, execution: AttemptExecution | None
    ) -> tuple[AcceptedPlanDocument | None, FormalPlanDecision | None]:
        if execution is None:
            return None, None
        content = self._query_plan_document(execution.submission_ref)
        if content is None:
            return None, None
        return content, self._query_formal_plan_decision(execution.submission_ref)

    def _query_plan_document(
        self, submission_ref: str
    ) -> AcceptedPlanDocument | None:
        return self._research_memory.query_plan_document(submission_ref)

    def _accept_plan_document(
        self, request: StageRunRequest, execution: AttemptExecution
    ) -> AcceptedPlanDocument:
        if request.accepted_idea_set is None:
            raise OwnerConflict("plan_content_owner_unavailable")
        return self._research_memory.accept_plan_document(
            accepted_question=request.accepted_question,
            accepted_idea_set=request.accepted_idea_set,
            context_pack_ref=request.context_pack_ref,
            request_ref=request.request_ref,
            run_ref=execution.run_ref,
            attempt_ref=execution.attempt_ref,
            fence_ref=execution.fence_ref,
            submission_ref=execution.submission_ref,
            plan_document=execution.outcome,
            reviewed_draft=execution.reviewed_draft,
            review=execution.review,
            execution_receipt=execution.receipt,
        )

    def _query_formal_plan_decision(
        self, submission_ref: str
    ) -> FormalPlanDecision | None:
        return self._research_graph.query_formal_plan_decision(submission_ref)

    def _decide_formal_plan(
        self,
        request: StageRunRequest,
        content: AcceptedPlanDocument,
        execution_receipt: AcceptanceReceipt,
    ) -> FormalPlanDecision:
        if request.accepted_idea_set is None:
            raise OwnerConflict("formal_plan_owner_unavailable")
        return self._research_graph.decide_formal_plan(
            accepted_question=request.accepted_question,
            question_content=self._accepted_question_content(
                request.accepted_question
            ),
            accepted_idea_set=request.accepted_idea_set,
            content=content,
            execution_receipt=execution_receipt,
        )

    def _discover_current_cycle(self) -> _CurrentCycle | None:
        candidates = self._discover_cycles()
        if candidates:
            return candidates[-1]
        visible: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds():
            cycle_ref = cast(str, foreground["cycle_ref"])
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("plan_cycle_index_invalid")
            visible.append(
                _CurrentCycle(cast(int, foreground["epoch"]), cycle_ref, question)
            )
        return None if not visible else visible[-1]

    def _discover_cycles(self) -> tuple[_CurrentCycle, ...]:
        values: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds(
            stage="plan"
        ):
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("plan_cycle_index_invalid")
            values.append(
                _CurrentCycle(
                    cast(int, foreground["epoch"]),
                    cast(str, foreground["cycle_ref"]),
                    question,
                )
            )
        return tuple(values)


def _operation_key(prefix: str, *values: str) -> str:
    return f"{prefix}:{canonical_hash(list(values))}"


def _not_attempted_acceptance() -> dict[str, object]:
    return {
        "status": "not_attempted",
        "content": {"status": "not_attempted"},
        "domain": {"status": "not_attempted"},
    }


def _not_eligible_projection(
    *,
    cycle_ref: str | None,
    question_ref: str | None,
    idea_outcome_ref: str | None,
    reason_code: str,
    next_stage: str | None,
) -> dict[str, object]:
    return {
        "eligibility": {
            "status": "not_eligible",
            "cycle_ref": cycle_ref,
            "question_ref": question_ref,
            "idea_outcome_ref": idea_outcome_ref,
            "reason": {"code": reason_code},
            "next_stage": next_stage,
        },
        "stage_run_request": None,
        "run": None,
        "plan_acceptance": _not_attempted_acceptance(),
        "stage_commit": None,
    }


def _public_request(request: StageRunRequest) -> dict[str, object]:
    if request.accepted_idea_set is None:
        raise OwnerConflict("plan_idea_set_binding_missing")
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": "Plan",
        "epoch": request.epoch,
        "accepted_question_binding": request.accepted_question.as_dict(),
        "accepted_idea_set_binding": request.accepted_idea_set.as_dict(),
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "receipt": request.receipt.as_public_dict(),
    }


def _public_acceptance(
    execution: AttemptExecution | None,
    content: AcceptedPlanDocument | None,
    decision: FormalPlanDecision | None,
) -> dict[str, object]:
    if execution is None:
        return _not_attempted_acceptance()
    answer_contract = execution.outcome.get("answer_contract")
    gaps = execution.outcome.get("gap_set")
    briefs = execution.outcome.get("experiment_briefs")
    if (
        not isinstance(answer_contract, dict)
        or not isinstance(answer_contract.get("answer_contract_hash"), str)
        or not isinstance(gaps, list)
        or not isinstance(briefs, list)
    ):
        raise OwnerConflict("plan_execution_projection_invalid")
    result: dict[str, object] = {
        "status": "awaiting_content",
        "bundle_disposition": execution.outcome.get("bundle_disposition"),
        "answer_contract_hash": answer_contract["answer_contract_hash"],
        "gap_count": len(gaps),
        "experiment_brief_count": len(briefs),
        "plan_document_ref": None,
        "content_ref": None,
        "formal_plan_ref": None,
        "outcome_ref": None,
        "content": {"status": "not_attempted"},
        "domain": {"status": "not_attempted"},
    }
    if content is None:
        return result
    result["status"] = "awaiting_domain"
    result["plan_document_ref"] = content.content_ref
    result["content_ref"] = content.content_ref
    result["answer_contract_hash"] = content.answer_contract_hash
    result["content"] = {
        "status": "accepted",
        "content_ref": content.content_ref,
        "plan_document_ref": content.content_ref,
        "answer_contract_hash": content.answer_contract_hash,
        "receipt": content.receipt.as_public_dict(),
    }
    if decision is None:
        return result
    result["formal_plan_ref"] = decision.formal_plan_ref
    result["outcome_ref"] = decision.formal_plan_ref
    result["bundle_disposition"] = decision.bundle_disposition
    if decision.decision == "accepted":
        result["status"] = "accepted"
        result["domain"] = {
            "status": "accepted",
            "formal_plan_ref": decision.formal_plan_ref,
            "outcome_ref": decision.formal_plan_ref,
            "receipt": decision.receipt.as_public_dict(),
        }
    else:
        result["status"] = "rejected"
        result["rejection"] = {
            "code": "formal_plan_requires_revision",
            "feedback": list(decision.feedback),
        }
        result["domain"] = {
            "status": "rejected",
            "reason": {
                "code": decision.reason_code or "formal_plan_requires_revision"
            },
            "receipt": decision.receipt.as_public_dict(),
        }
    return result


def _public_commit(
    commit: StageCommit, *, decision: FormalPlanDecision | None
) -> dict[str, object]:
    if (
        commit.stage != "plan"
        or commit.outcome_kind != "formal_plan"
        or commit.disposition != "completed"
        or decision is None
        or decision.decision != "accepted"
        or decision.formal_plan_ref != commit.outcome_ref
    ):
        raise OwnerConflict("plan_stage_commit_disposition_invalid")
    return {
        "status": "Completed",
        "commit_ref": commit.commit_ref,
        "stage_commit_ref": commit.commit_ref,
        "request_ref": commit.request_ref,
        "cycle_ref": commit.cycle_ref,
        "stage": "Plan",
        "epoch": commit.epoch,
        "run_ref": commit.run_ref,
        "formal_plan_ref": commit.outcome_ref,
        "outcome_ref": commit.outcome_ref,
        "outcome_kind": "FormalPlan",
        "bundle_disposition": decision.bundle_disposition,
        "run_completion_receipt": commit.run_completion_receipt.as_public_dict(),
        "formal_plan_receipt": commit.outcome_receipt.as_public_dict(),
        "receipt": commit.receipt.as_public_dict(),
        "next_stage": "Bundle",
    }
