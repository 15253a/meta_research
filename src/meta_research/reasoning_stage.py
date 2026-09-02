from __future__ import annotations

from dataclasses import asdict, dataclass
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
    ReasoningRuntimeBinding,
    ReasoningStageRun,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_graph import (
    AcceptedQuestion,
    ResearchGraphInterface,
)
from meta_research.owners.research_memory import (
    AcceptedReasoningContent,
    ResearchMemoryInterface,
)
from meta_research.reasoning_contract import (
    SCIENTIFIC_OUTCOMES,
    ReasoningContractError,
    current_target_evidence_leaves,
    plan_evidence_reuse_leaves,
)
from meta_research.reasoning_skill import (
    ReasoningAutonomousCheckpointResult,
    ReasoningSkillContractError,
    ReasoningSkillDraft,
    ReasoningSkillProvider,
    ReasoningSkillRequest,
    ReasoningSkillUnavailable,
    RecoverableReasoningSkillCandidateError,
    validate_reasoning_autonomous_checkpoint_result,
    validate_reasoning_autonomous_resume_result,
    validate_reasoning_skill_draft,
    validate_reasoning_skill_result,
)


_CYCLE_EVENT = "advancement_engine.initial_cycle_activated"
_STAGE_REQUEST_EVENT = "advancement_engine.stage_run_requested"


@dataclass(frozen=True)
class _CurrentCycle:
    revision: int
    cycle_ref: str
    question: AcceptedQuestion


@dataclass(frozen=True)
class _CycleStep:
    advanced: bool
    provider_boundary_attempted: bool = False


class ReasoningStageWorker:
    """Recoverable Reasoning loop composed only through public Owner seams.

    One pass crosses at most one durable Owner boundary (or one provider
    boundary).  The worker owns no scientific or lifecycle truth: restart
    recovery is reconstructed from the AE request/commit, AR execution, RM
    content receipt, and RG semantic decision.
    """

    def __init__(
        self,
        feed: DurableFeed,
        advancement_engine: AdvancementEngineInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        research_graph: ResearchGraphInterface,
        provider: ReasoningSkillProvider,
        *,
        autonomous_creation: object | None = None,
    ) -> None:
        self._feed = feed
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._provider = provider
        self._autonomous_creation = autonomous_creation
        self._transient_error: str | None = None
        self._provider_cursor_cycle_ref: str | None = None

    @property
    def transient_error(self) -> str | None:
        return self._transient_error

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        configure = getattr(self._provider, "configure_resident_mcp_endpoint", None)
        if callable(configure):
            configure(base_url)

    def process_once(self) -> bool:
        if self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=("reasoning_primary", "reasoning_review"),
        ):
            return True
        transient_error: str | None = None
        self._transient_error = None
        cycles = list(self._discover_active_cycles())
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
            step = self._process_cycle(current)
            if self._transient_error is not None:
                transient_error = transient_error or self._transient_error
            if step.provider_boundary_attempted:
                self._provider_cursor_cycle_ref = current.cycle_ref
            if step.advanced:
                self._transient_error = transient_error
                return True
            if step.provider_boundary_attempted:
                break
        self._transient_error = transient_error
        return False

    def _process_cycle(self, current: _CurrentCycle) -> _CycleStep:
        foreground = self._advancement_engine.query_foreground(
            current.question.quest_ref
        )
        if not _is_current_reasoning_foreground(foreground, current.cycle_ref):
            return _CycleStep(False)
        epoch = cast(int, foreground["epoch"])
        request = self._advancement_engine.query_reasoning_stage_request(
            current.cycle_ref
        )
        if request is None:
            self._advancement_engine.ensure_reasoning_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                idempotency_key=_operation_key(
                    "reasoning-request", current.cycle_ref, str(epoch)
                ),
            )
            return _CycleStep(True)
        self._assert_request_current(request, current, epoch)

        run = self._agent_runtime.query_reasoning_stage_run(request.request_ref)
        if run is None:
            try:
                runtime_binding = self._provider.runtime_binding()
            except ReasoningSkillUnavailable as error:
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            self._agent_runtime.admit_reasoning_stage(
                request,
                _operation_key("reasoning-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
            return _CycleStep(True, provider_boundary_attempted=True)
        managed = self._agent_runtime.query_managed_run(run.run_ref)
        if managed is not None and managed["status"] not in {"running", "completed"}:
            return _CycleStep(False)
        if self._advancement_engine.query_reasoning_stage_commit(
            request.request_ref
        ):
            return _CycleStep(False)

        execution = run.execution
        if execution is None:
            if run.autonomous_checkpoint is not None:
                return self._process_autonomous_checkpoint(request, run)
            return self._execute_attempt(request, run)

        content = self._research_memory.query_reasoning_content(
            execution.submission_ref
        )
        if content is None:
            reviewed_draft = execution.reviewed_draft
            if not isinstance(reviewed_draft, dict):
                raise OwnerConflict("reasoning_reviewed_draft_missing")
            scientific_candidate_content_receipt = None
            scientific_candidate_domain_receipt = None
            if run.autonomous_checkpoint is not None:
                candidate, scientific_decision = self._accepted_checkpoint_facts(
                    run.autonomous_checkpoint.checkpoint_ref
                )
                if (
                    candidate is None
                    or scientific_decision is None
                    or scientific_decision.decision != "accepted"
                ):
                    raise OwnerConflict(
                        "reasoning_autonomous_source_acceptance_missing"
                    )
                scientific_candidate_content_receipt = candidate.receipt
                scientific_candidate_domain_receipt = scientific_decision.receipt
            self._research_memory.accept_reasoning_content(
                request_ref=request.request_ref,
                cycle_ref=request.cycle_ref,
                foreground_epoch=request.epoch,
                context_pack_ref=request.context_pack_ref,
                context_pack_hash=request.context_pack_hash,
                context_pack=request.context_pack,
                stage_request_receipt=request.receipt,
                run_ref=execution.run_ref,
                attempt_ref=execution.attempt_ref,
                fence_ref=execution.fence_ref,
                submission_ref=execution.submission_ref,
                outcome=execution.outcome,
                reviewed_draft=reviewed_draft,
                review=execution.review,
                execution_receipt=execution.receipt,
                scientific_candidate_content_receipt=(
                    scientific_candidate_content_receipt
                ),
                scientific_candidate_domain_receipt=(
                    scientific_candidate_domain_receipt
                ),
            )
            return _CycleStep(True)

        decision = self._research_graph.query_reasoning_outcome_decision(
            execution.submission_ref
        )
        if decision is None:
            self._research_graph.decide_reasoning_outcome(content=content)
            return _CycleStep(True)
        if decision.decision == "rejected":
            self._agent_runtime.continue_after_reasoning_rejection(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "reasoning-revise",
                    run.run_ref,
                    run.attempt_ref,
                    decision.receipt.receipt_ref,
                ),
            )
            return _CycleStep(True)
        if decision.decision != "accepted" or decision.outcome_ref is None:
            raise OwnerConflict("reasoning_outcome_decision_invalid")

        if run.completion is None:
            self._agent_runtime.complete_reasoning_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                outcome_ref=decision.outcome_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "reasoning-complete",
                    run.run_ref,
                    run.attempt_ref,
                    decision.outcome_ref,
                ),
            )
            return _CycleStep(True)

        self._advancement_engine.commit_reasoning_stage(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            outcome_ref=decision.outcome_ref,
            run_completion_receipt=run.completion.receipt,
            outcome_receipt=decision.receipt,
            idempotency_key=_operation_key(
                "reasoning-commit", request.request_ref, decision.outcome_ref
            ),
        )
        return _CycleStep(True)

    def _process_autonomous_checkpoint(
        self,
        request: StageRunRequest,
        run: ReasoningStageRun,
    ) -> _CycleStep:
        checkpoint = run.autonomous_checkpoint
        if checkpoint is None:
            raise OwnerConflict("reasoning_autonomous_checkpoint_missing")
        candidate, decision = self._accepted_checkpoint_facts(
            checkpoint.checkpoint_ref
        )
        if candidate is None:
            submission_ref = "reasoning_scientific_candidate_" + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "checkpoint_ref": checkpoint.checkpoint_ref,
                    "checkpoint_hash": checkpoint.checkpoint_hash,
                }
            )[:32]
            self._research_memory.accept_reasoning_scientific_candidate(
                request_ref=request.request_ref,
                cycle_ref=request.cycle_ref,
                foreground_epoch=request.epoch,
                context_pack_ref=request.context_pack_ref,
                context_pack_hash=request.context_pack_hash,
                context_pack=request.context_pack,
                stage_request_receipt=request.receipt,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=submission_ref,
                checkpoint_ref=checkpoint.checkpoint_ref,
                checkpoint=checkpoint.checkpoint,
                review=checkpoint.review,
                checkpoint_receipt=checkpoint.receipt,
            )
            return _CycleStep(True)
        if decision is None:
            self._research_graph.decide_reasoning_scientific_candidate(
                content=candidate
            )
            return _CycleStep(True)
        if decision.decision == "rejected":
            self._transient_error = (
                decision.reason_code
                or "reasoning_scientific_candidate_requires_revision"
            )
            return _CycleStep(False)
        scientific_outcome = checkpoint.checkpoint.get("scientific_outcome")
        if (
            decision.decision != "accepted"
            or not isinstance(scientific_outcome, dict)
            or decision.outcome_ref != scientific_outcome.get("outcome_ref")
        ):
            raise OwnerConflict("reasoning_scientific_candidate_decision_invalid")
        creation_result = self._ready_autonomous_creation(checkpoint.checkpoint_ref)
        if creation_result is None:
            return _CycleStep(False)
        return self._resume_after_autonomous_creation(
            request,
            run,
            checkpoint.checkpoint,
            creation_result,
        )

    def _execute_attempt(
        self,
        request: StageRunRequest,
        run: ReasoningStageRun,
    ) -> _CycleStep:
        if not isinstance(run.runtime_binding, ReasoningRuntimeBinding):
            raise OwnerConflict("reasoning_runtime_binding_invalid")
        try:
            runtime_binding = self._provider.runtime_binding()
        except ReasoningSkillUnavailable as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        if runtime_binding != run.runtime_binding:
            self._transient_error = "reasoning_runtime_binding_drift"
            return _CycleStep(False, provider_boundary_attempted=True)

        invocation = (
            run.primary_invocation
            if run.primary_draft is None
            else run.review_invocation
        )
        phase = "primary" if run.primary_draft is None else "review"
        base_job_ref = invocation.operation_ref
        job_ref = self._agent_runtime.root_provider_continuation_job_ref(
            root_kind="reasoning",
            phase=phase,
            run_ref=run.run_ref,
            root_session_ref=run.root_session_ref,
            base_job_ref=base_job_ref,
        )
        unit_ref = (
            invocation.invocation_ref
            if job_ref == base_job_ref
            else "provider_unit_"
            + canonical_hash(
                {"invocation_ref": invocation.invocation_ref, "job_ref": job_ref}
            )[:64]
        )
        skill_request = self._skill_request(request, run, job_ref=job_ref)

        if run.primary_draft is None:
            try:
                self._agent_runtime.begin_provider_unit(
                    unit_ref=unit_ref,
                    operation_ref=job_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    unit_kind="reasoning_primary",
                )
            except OwnerConflict as error:
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            provider_safe = True
            draft = None
            try:
                try:
                    draft = self._provider.generate_draft(skill_request)
                    draft_hash, _outcome_hash, _transition_hash = (
                        validate_reasoning_skill_draft(skill_request, draft)
                    )
                except ReasoningSkillUnavailable as error:
                    if (
                        error.native_session_ref is not None
                        and self._agent_runtime.park_root_provider_session_for_human_request(
                            root_kind="reasoning",
                            phase="primary",
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            native_session_ref=error.native_session_ref,
                            runtime_binding_hash=run.runtime_binding_hash,
                        )
                    ):
                        self._finish_provider_job(job_ref)
                        self._transient_error = None
                        return _CycleStep(True, provider_boundary_attempted=True)
                    if error.rejected_candidate is not None:
                        if (
                            error.rejected_native_session_ref is None
                            or error.rejected_detail_code is None
                        ):
                            raise OwnerConflict(
                                "stage_completion_rejection_invalid"
                            )
                        self._reject_completion_candidate(
                            unit_ref=unit_ref,
                            run=run,
                            operation_name="primary",
                            native_session_ref=error.rejected_native_session_ref,
                            candidate=error.rejected_candidate,
                            failure_code=error.code,
                            detail_code=error.rejected_detail_code,
                        )
                        provider_safe = False
                        self._transient_error = error.code
                        return _CycleStep(
                            True, provider_boundary_attempted=True
                        )
                    if error.code == "codex_operation_reconciliation_pending":
                        provider_safe = False
                    elif error.recovery_checkpoint is not None:
                        provider_safe = False
                        self._agent_runtime.record_stage_provider_hard_ceiling(
                            unit_ref=unit_ref,
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            failure_code=error.code,
                            provider_exit=error.recovery_checkpoint,
                        )
                    self._transient_error = error.code
                    return _CycleStep(False, provider_boundary_attempted=True)
                except RecoverableReasoningSkillCandidateError as error:
                    if draft is None:
                        self._transient_error = str(error)
                        return _CycleStep(
                            False, provider_boundary_attempted=True
                        )
                    if self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="reasoning",
                        phase="primary",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=draft.primary_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    ):
                        self._finish_provider_job(job_ref)
                        self._transient_error = None
                        return _CycleStep(True, provider_boundary_attempted=True)
                    failure_code = "reasoning_primary_result_contract_invalid"
                    self._reject_completion_candidate(
                        unit_ref=unit_ref,
                        run=run,
                        operation_name="primary",
                        native_session_ref=draft.primary_session_ref,
                        candidate=asdict(draft),
                        failure_code=failure_code,
                        detail_code=str(error),
                    )
                    provider_safe = False
                    self._transient_error = failure_code
                    return _CycleStep(True, provider_boundary_attempted=True)
                except ReasoningSkillContractError as error:
                    self._transient_error = str(error)
                    return _CycleStep(False, provider_boundary_attempted=True)
                try:
                    checkpoint = self._agent_runtime.record_reasoning_primary_draft(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=draft.primary_session_ref,
                        runtime_binding=run.runtime_binding,
                        draft=draft.draft,
                        adapter_kind=draft.adapter_kind,
                        idempotency_key=_operation_key(
                            "reasoning-primary",
                            run.run_ref,
                            run.attempt_ref,
                            draft_hash,
                        ),
                    )
                except OwnerConflict as error:
                    if error.code != "runtime_run_suspended":
                        raise
                    self._finish_provider_job(job_ref)
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                if checkpoint.draft_hash != draft_hash:
                    raise OwnerConflict("reasoning_primary_draft_hash_mismatch")
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
        draft = ReasoningSkillDraft(
            draft=checkpoint.draft,
            primary_session_ref=checkpoint.native_session_ref,
            adapter_kind=checkpoint.adapter_kind,
        )
        try:
            self._agent_runtime.begin_provider_unit(
                unit_ref=unit_ref,
                operation_ref=job_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                unit_kind="reasoning_review",
            )
        except OwnerConflict as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        provider_safe = True
        result = None
        try:
            try:
                result = self._provider.review_draft(skill_request, draft)
                if isinstance(result, ReasoningAutonomousCheckpointResult):
                    (
                        checkpoint_hash,
                        _scientific_outcome_hash,
                        _autonomous_scope_hash,
                        review_hash,
                    ) = validate_reasoning_autonomous_checkpoint_result(
                        skill_request, draft, result
                    )
                    if self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="reasoning",
                        phase="review",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=result.primary_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    ):
                        self._finish_provider_job(job_ref)
                        self._transient_error = None
                        return _CycleStep(
                            True, provider_boundary_attempted=True
                        )
                    recorded = (
                        self._agent_runtime.record_reasoning_autonomous_checkpoint(
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            native_session_ref=result.primary_session_ref,
                            runtime_binding=run.runtime_binding,
                            checkpoint=result.reviewed_checkpoint,
                            review=result.review_document(),
                            idempotency_key=_operation_key(
                                "reasoning-autonomous-checkpoint",
                                run.run_ref,
                                run.attempt_ref,
                            ),
                        )
                    )
                    if (
                        recorded.checkpoint_hash != checkpoint_hash
                        or recorded.review_hash != review_hash
                    ):
                        raise OwnerConflict(
                            "reasoning_autonomous_checkpoint_material_mismatch"
                        )
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                (
                    draft_hash,
                    final_output_hash,
                    _scientific_outcome_hash,
                    _transition_hash,
                    review_hash,
                ) = validate_reasoning_skill_result(skill_request, result)
            except ReasoningSkillUnavailable as error:
                review_session_ref = (
                    error.native_session_ref or run.native_session_ref
                )
                if (
                    review_session_ref is not None
                    and self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="reasoning",
                        phase="review",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=review_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    )
                ):
                    self._finish_provider_job(job_ref)
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                if error.rejected_candidate is not None:
                    if (
                        error.rejected_native_session_ref is None
                        or error.rejected_detail_code is None
                    ):
                        raise OwnerConflict("stage_completion_rejection_invalid")
                    self._reject_completion_candidate(
                        unit_ref=unit_ref,
                        run=run,
                        operation_name="review",
                        native_session_ref=error.rejected_native_session_ref,
                        candidate=error.rejected_candidate,
                        failure_code=error.code,
                        detail_code=error.rejected_detail_code,
                    )
                    provider_safe = False
                    self._transient_error = error.code
                    return _CycleStep(True, provider_boundary_attempted=True)
                if error.code == "codex_operation_reconciliation_pending":
                    provider_safe = False
                elif error.recovery_checkpoint is not None:
                    provider_safe = False
                    self._agent_runtime.record_stage_provider_hard_ceiling(
                        unit_ref=unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        failure_code=error.code,
                        provider_exit=error.recovery_checkpoint,
                    )
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            except RecoverableReasoningSkillCandidateError as error:
                if result is None:
                    self._transient_error = str(error)
                    return _CycleStep(False, provider_boundary_attempted=True)
                if self._agent_runtime.park_root_provider_session_for_human_request(
                    root_kind="reasoning",
                    phase="review",
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=result.primary_session_ref,
                    runtime_binding_hash=run.runtime_binding_hash,
                ):
                    self._finish_provider_job(job_ref)
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                failure_code = "reasoning_review_result_contract_invalid"
                self._reject_completion_candidate(
                    unit_ref=unit_ref,
                    run=run,
                    operation_name="review",
                    native_session_ref=result.primary_session_ref,
                    candidate=asdict(result),
                    failure_code=failure_code,
                    detail_code=str(error),
                )
                provider_safe = False
                self._transient_error = failure_code
                return _CycleStep(True, provider_boundary_attempted=True)
            except ReasoningSkillContractError as error:
                self._transient_error = str(error)
                return _CycleStep(False, provider_boundary_attempted=True)
            if self._agent_runtime.park_root_provider_session_for_human_request(
                root_kind="reasoning",
                phase="review",
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding_hash=run.runtime_binding_hash,
            ):
                self._finish_provider_job(job_ref)
                self._transient_error = None
                return _CycleStep(True, provider_boundary_attempted=True)
            review = result.review_document()
            submission_ref = "reasoning_submission_" + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "final_output_hash": final_output_hash,
                    "review_hash": review_hash,
                }
            )[:32]
            execution = self._agent_runtime.record_reasoning_attempt_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=submission_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding=run.runtime_binding,
                outcome=result.outcome_document(),
                reviewed_draft=result.reviewed_draft,
                review=review,
                idempotency_key=_operation_key(
                    "reasoning-execute", run.run_ref, run.attempt_ref
                ),
            )
            if canonical_hash(execution.outcome) != final_output_hash or (
                canonical_hash(execution.reviewed_draft) != draft_hash
            ):
                raise OwnerConflict("reasoning_execution_material_mismatch")
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

    def _skill_request(
        self,
        request: StageRunRequest,
        run: ReasoningStageRun,
        *,
        job_ref: str | None = None,
    ) -> ReasoningSkillRequest:
        (
            predecessor_candidate_ref,
            owner_rejection_receipt_ref,
            owner_rejection_kind,
            owner_feedback,
        ) = self._owner_rejection_context(request, run)
        return ReasoningSkillRequest(
            stage_request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            quest_ref=request.accepted_question.quest_ref,
            goal_revision_ref=self._goal_revision_ref(request),
            foreground_epoch=request.epoch,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            frozen_evidence_closure=_frozen_evidence_closure(request.context_pack),
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            native_session_ref=run.native_session_ref,
            predecessor_candidate_ref=predecessor_candidate_ref,
            owner_rejection_receipt_ref=owner_rejection_receipt_ref,
            owner_rejection_kind=owner_rejection_kind,
            owner_feedback=owner_feedback,
            job_ref=job_ref,
        )

    def _owner_rejection_context(
        self,
        request: StageRunRequest,
        run: ReasoningStageRun,
    ) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        predecessor = run.predecessor_execution
        rejection_receipt = run.rejection_receipt
        completion_rejection = run.completion_rejection
        if (predecessor is None) != (rejection_receipt is None):
            raise OwnerConflict("rejection_lineage_incomplete")
        domain_feedback: tuple[str, ...] = ()
        if predecessor is not None and rejection_receipt is not None:
            decision = self._research_graph.query_reasoning_outcome_decision(
                predecessor.submission_ref
            )
            if (
                decision is None
                or decision.decision != "rejected"
                or decision.receipt != rejection_receipt
                or decision.request_ref != request.request_ref
                or decision.run_ref != run.run_ref
                or decision.attempt_ref != predecessor.attempt_ref
                or decision.fence_ref != predecessor.fence_ref
                or not decision.feedback
                or run.native_session_ref is None
            ):
                raise OwnerConflict("rejection_lineage_invalid")
            domain_feedback = decision.feedback

        if completion_rejection is not None:
            if (
                not completion_rejection.feedback
                or run.native_session_ref is None
                or completion_rejection.request_ref != request.request_ref
                or completion_rejection.run_ref != run.run_ref
                or completion_rejection.stage != "reasoning"
                or completion_rejection.successor_attempt_ref != run.attempt_ref
                or completion_rejection.root_session_ref != run.root_session_ref
                or completion_rejection.native_session_ref
                != run.native_session_ref
                or run.technical_predecessor_attempt_ref
                != completion_rejection.attempt_ref
            ):
                raise OwnerConflict("completion_rejection_lineage_invalid")
        if predecessor is not None or completion_rejection is not None:
            return (
                (
                    completion_rejection.candidate_ref
                    if completion_rejection is not None
                    else predecessor.submission_ref
                ),
                (
                    completion_rejection.receipt.receipt_ref
                    if completion_rejection is not None
                    else rejection_receipt.receipt_ref
                ),
                "domain" if predecessor is not None else "completion",
                domain_feedback
                + (
                    ()
                    if completion_rejection is None
                    else completion_rejection.feedback
                ),
            )

        if (
            run.attempt_generation != 1
            and run.technical_predecessor_attempt_ref is None
        ) or (run.primary_draft is not None and run.native_session_ref is None):
            raise OwnerConflict("attempt_lineage_invalid")
        return None, None, None, ()

    def _resume_after_autonomous_creation(
        self,
        request: StageRunRequest,
        run: ReasoningStageRun,
        checkpoint: dict[str, object],
        creation_result: dict[str, object],
    ) -> _CycleStep:
        if not isinstance(run.runtime_binding, ReasoningRuntimeBinding):
            raise OwnerConflict("reasoning_runtime_binding_invalid")
        try:
            runtime_binding = self._provider.runtime_binding()
        except ReasoningSkillUnavailable as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        if runtime_binding != run.runtime_binding:
            self._transient_error = "reasoning_runtime_binding_drift"
            return _CycleStep(False, provider_boundary_attempted=True)
        invocation = run.review_invocation
        base_job_ref = invocation.operation_ref
        job_ref = self._agent_runtime.root_provider_continuation_job_ref(
            root_kind="reasoning",
            phase="autonomous-resume",
            run_ref=run.run_ref,
            root_session_ref=run.root_session_ref,
            base_job_ref=base_job_ref,
        )
        unit_ref = (
            invocation.invocation_ref
            if job_ref == base_job_ref
            else "provider_unit_"
            + canonical_hash(
                {"invocation_ref": invocation.invocation_ref, "job_ref": job_ref}
            )[:64]
        )
        skill_request = self._skill_request(request, run, job_ref=job_ref)
        try:
            self._agent_runtime.begin_provider_unit(
                unit_ref=unit_ref,
                operation_ref=job_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                unit_kind="reasoning_review",
            )
        except OwnerConflict as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        provider_safe = True
        result = None
        try:
            try:
                result = self._provider.resume_after_autonomous_creation(
                    skill_request,
                    checkpoint,
                    creation_result,
                )
                (
                    reviewed_checkpoint_hash,
                    final_output_hash,
                    _scientific_outcome_hash,
                    _transition_hash,
                    review_hash,
                ) = validate_reasoning_autonomous_resume_result(
                    skill_request,
                    checkpoint,
                    creation_result,
                    result,
                )
            except ReasoningSkillUnavailable as error:
                review_session_ref = (
                    error.native_session_ref or run.native_session_ref
                )
                if (
                    review_session_ref is not None
                    and self._agent_runtime.park_root_provider_session_for_human_request(
                        root_kind="reasoning",
                        phase="autonomous-resume",
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=review_session_ref,
                        runtime_binding_hash=run.runtime_binding_hash,
                    )
                ):
                    self._finish_provider_job(job_ref)
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                if error.rejected_candidate is not None:
                    if (
                        error.rejected_native_session_ref is None
                        or error.rejected_detail_code is None
                    ):
                        raise OwnerConflict("stage_completion_rejection_invalid")
                    self._reject_completion_candidate(
                        unit_ref=unit_ref,
                        run=run,
                        operation_name="autonomous-resume",
                        native_session_ref=error.rejected_native_session_ref,
                        candidate=error.rejected_candidate,
                        failure_code=error.code,
                        detail_code=error.rejected_detail_code,
                    )
                    provider_safe = False
                    self._transient_error = error.code
                    return _CycleStep(True, provider_boundary_attempted=True)
                if error.code == "codex_operation_reconciliation_pending":
                    provider_safe = False
                elif error.recovery_checkpoint is not None:
                    provider_safe = False
                    self._agent_runtime.record_stage_provider_hard_ceiling(
                        unit_ref=unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        failure_code=error.code,
                        provider_exit=error.recovery_checkpoint,
                    )
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            except RecoverableReasoningSkillCandidateError as error:
                if result is None:
                    self._transient_error = str(error)
                    return _CycleStep(False, provider_boundary_attempted=True)
                if self._agent_runtime.park_root_provider_session_for_human_request(
                    root_kind="reasoning",
                    phase="autonomous-resume",
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=result.primary_session_ref,
                    runtime_binding_hash=run.runtime_binding_hash,
                ):
                    self._finish_provider_job(job_ref)
                    self._transient_error = None
                    return _CycleStep(True, provider_boundary_attempted=True)
                failure_code = "reasoning_review_result_contract_invalid"
                self._reject_completion_candidate(
                    unit_ref=unit_ref,
                    run=run,
                    operation_name="autonomous-resume",
                    native_session_ref=result.primary_session_ref,
                    candidate=asdict(result),
                    failure_code=failure_code,
                    detail_code=str(error),
                )
                provider_safe = False
                self._transient_error = failure_code
                return _CycleStep(True, provider_boundary_attempted=True)
            except ReasoningSkillContractError as error:
                self._transient_error = str(error)
                return _CycleStep(False, provider_boundary_attempted=True)
            if self._agent_runtime.park_root_provider_session_for_human_request(
                root_kind="reasoning",
                phase="autonomous-resume",
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding_hash=run.runtime_binding_hash,
            ):
                self._finish_provider_job(job_ref)
                self._transient_error = None
                return _CycleStep(True, provider_boundary_attempted=True)
            submission_ref = "reasoning_submission_" + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "final_output_hash": final_output_hash,
                    "review_hash": review_hash,
                }
            )[:32]
            execution = self._agent_runtime.record_reasoning_attempt_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=submission_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding=run.runtime_binding,
                outcome=result.outcome_document(),
                reviewed_draft=result.reviewed_draft,
                review=result.review_document(),
                idempotency_key=_operation_key(
                    "reasoning-autonomous-resume", run.run_ref, run.attempt_ref
                ),
            )
            if (
                canonical_hash(execution.outcome) != final_output_hash
                or canonical_hash(execution.reviewed_draft)
                != reviewed_checkpoint_hash
            ):
                raise OwnerConflict("reasoning_execution_material_mismatch")
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

    def _finish_provider_job(self, job_ref: str) -> None:
        finish_job = getattr(self._provider, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    def _reject_completion_candidate(
        self,
        *,
        unit_ref: str,
        run: ReasoningStageRun,
        operation_name: str,
        native_session_ref: str,
        candidate: dict[str, object],
        failure_code: str,
        detail_code: str,
    ) -> None:
        self._agent_runtime.reject_stage_completion_candidate(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            native_session_ref=native_session_ref,
            candidate={"phase": operation_name, "result": candidate},
            reason_code=failure_code,
            detail_code=detail_code,
            feedback=(
                "Return a corrected structured completion that satisfies "
                f"{detail_code}, then resubmit it in this Root Session.",
            ),
        )

    def _accepted_checkpoint_facts(self, checkpoint_ref: str):
        candidate = (
            self._research_memory.query_reasoning_scientific_candidate_by_checkpoint_ref(
                checkpoint_ref
            )
        )
        if candidate is None:
            return None, None
        decision = self._research_graph.query_reasoning_scientific_decision(
            candidate.submission_ref
        )
        return candidate, decision

    def _ready_autonomous_creation(
        self, checkpoint_ref: str
    ) -> dict[str, object] | None:
        if self._autonomous_creation is None:
            return None
        query = getattr(self._autonomous_creation, "query", None)
        if not callable(query):
            raise OwnerConflict("autonomous_creation_query_unavailable")
        current = query(checkpoint_ref)
        if not isinstance(current, dict):
            return None
        checkpoint = current.get("checkpoint")
        if (
            current.get("status") != "ready_for_reasoning_resume"
            or not isinstance(checkpoint, dict)
            or checkpoint.get("ref") != checkpoint_ref
        ):
            return None
        return current

    def query_current(self) -> dict[str, object]:
        current = self._discover_projection_cycle()
        if current is None:
            return _not_eligible_projection(
                cycle_ref=None,
                question_ref=None,
                reason_code="accepted_cycle_unavailable",
            )
        request = self._advancement_engine.query_reasoning_stage_request(
            current.cycle_ref
        )
        if request is None:
            foreground = self._advancement_engine.query_foreground(
                current.question.quest_ref
            )
            if not _is_current_reasoning_foreground(
                foreground, current.cycle_ref
            ):
                return _not_eligible_projection(
                    cycle_ref=current.cycle_ref,
                    question_ref=current.question.question_ref,
                    reason_code="reasoning_route_unavailable",
                )
            return {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": current.cycle_ref,
                    "question_ref": current.question.question_ref,
                    "reason": None,
                    "next_stage": "Reasoning",
                },
                "stage_run_request": None,
                "run": None,
                "autonomous_creation_checkpoint": None,
                "reasoning_acceptance": _not_attempted_acceptance(),
                "transition": {"status": "not_attempted"},
                "stage_commit": None,
            }
        run = self._agent_runtime.query_reasoning_stage_run(request.request_ref)
        commit = self._advancement_engine.query_reasoning_stage_commit(
            request.request_ref
        )
        execution = None if run is None else run.execution
        lineage_execution = execution
        if lineage_execution is None and run is not None:
            lineage_execution = run.predecessor_execution
        content, decision = self._accepted_facts(lineage_execution)
        autonomous_checkpoint = (
            None
            if run is None or run.autonomous_checkpoint is None
            else self._public_autonomous_checkpoint(run)
        )
        return {
            "eligibility": {
                "status": "consumed" if commit is not None else "requested",
                "cycle_ref": current.cycle_ref,
                "question_ref": current.question.question_ref,
                "reason": None,
                "next_stage": "Reasoning",
            },
            "stage_run_request": _public_request(request),
            "run": None if run is None else _public_run(run),
            "autonomous_creation_checkpoint": autonomous_checkpoint,
            "reasoning_acceptance": _public_acceptance(
                lineage_execution, content, decision
            ),
            "transition": _public_transition(content, decision),
            "stage_commit": (
                None
                if commit is None
                else _public_commit(commit, content=content, decision=decision)
            ),
        }

    def _public_autonomous_checkpoint(
        self, run: ReasoningStageRun
    ) -> dict[str, object]:
        checkpoint = run.autonomous_checkpoint
        if checkpoint is None:
            raise OwnerConflict("reasoning_autonomous_checkpoint_missing")
        candidate, decision = self._accepted_checkpoint_facts(
            checkpoint.checkpoint_ref
        )
        scope_acceptance: dict[str, object] = {
            "status": "awaiting_content",
            "content": {"status": "not_attempted"},
            "domain": {"status": "not_attempted"},
        }
        status = "awaiting_content"
        if candidate is not None:
            status = "awaiting_domain"
            scope_acceptance = {
                "status": "awaiting_domain",
                "content": {
                    "status": "accepted",
                    "content_ref": candidate.content_ref,
                    "receipt": candidate.receipt.as_public_dict(),
                },
                "domain": {"status": "not_attempted"},
            }
        if decision is not None:
            if decision.decision == "accepted":
                status = "source_accepted"
                scope_acceptance["status"] = "accepted"
                scope_acceptance["domain"] = {
                    "status": "accepted",
                    "outcome_ref": decision.outcome_ref,
                    "receipt": decision.receipt.as_public_dict(),
                }
            else:
                status = "source_rejected"
                scope_acceptance["status"] = "rejected"
                scope_acceptance["domain"] = {
                    "status": "rejected",
                    "reason": {
                        "code": decision.reason_code
                        or "reasoning_scientific_candidate_requires_revision"
                    },
                    "feedback": list(decision.feedback),
                    "receipt": decision.receipt.as_public_dict(),
                }
        return {
            **checkpoint.checkpoint,
            "status": status,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "checkpoint_hash": checkpoint.checkpoint_hash,
            "review_hash": checkpoint.review_hash,
            "receipt": checkpoint.receipt.as_public_dict(),
            "scope_acceptance": scope_acceptance,
        }

    def _accepted_facts(self, execution: AttemptExecution | None):
        if execution is None:
            return None, None
        content = self._research_memory.query_reasoning_content(
            execution.submission_ref
        )
        if content is None:
            return None, None
        decision = self._research_graph.query_reasoning_outcome_decision(
            execution.submission_ref
        )
        return content, decision

    def _goal_revision_ref(self, request: StageRunRequest) -> str:
        research_context = request.context_pack.get("research_context")
        if isinstance(research_context, dict):
            frozen = research_context.get("goal_revision_ref")
            if isinstance(frozen, str) and frozen:
                return frozen
        quest = self._research_graph.query_quest_by_ref(
            request.accepted_question.quest_ref
        )
        if quest is None:
            raise OwnerConflict("reasoning_quest_goal_unavailable")
        return "goal_revision_" + canonical_hash(
            {
                "quest_ref": quest.quest_ref,
                "draft_revision": quest.draft_revision,
                "draft_hash": quest.draft_hash,
            }
        )[:32]

    def _assert_request_current(
        self,
        request: StageRunRequest,
        current: _CurrentCycle,
        epoch: int,
    ) -> None:
        if (
            request.stage != "reasoning"
            or request.cycle_ref != current.cycle_ref
            or request.epoch != epoch
            or request.accepted_question != current.question.as_binding()
            or canonical_hash(request.context_pack) != request.context_pack_hash
        ):
            raise OwnerConflict("reasoning_stage_request_eligibility_invalid")

    def _discover_active_cycles(self) -> tuple[_CurrentCycle, ...]:
        values: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds(
            stage="reasoning"
        ):
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("reasoning_cycle_index_invalid")
            values.append(
                _CurrentCycle(
                    cast(int, foreground["epoch"]),
                    cast(str, foreground["cycle_ref"]),
                    question,
                )
            )
        return tuple(values)

    def _discover_projection_cycle(self) -> _CurrentCycle | None:
        active = self._discover_active_cycles()
        if active:
            return active[-1]
        historical: dict[str, _CurrentCycle] = {}
        for event in self._feed.read_event_type(_STAGE_REQUEST_EVENT):
            if event.payload.get("stage") != "reasoning":
                continue
            cycle_ref = event.payload.get("cycle_ref")
            if not isinstance(cycle_ref, str) or not cycle_ref:
                raise OwnerConflict("reasoning_cycle_index_invalid")
            request = self._advancement_engine.query_reasoning_stage_request(
                cycle_ref
            )
            if request is None:
                raise OwnerConflict("reasoning_cycle_index_invalid")
            question = self._research_graph.query_question_by_ref(
                request.accepted_question.question_ref
            )
            if (
                question is None
                or question.as_binding() != request.accepted_question
            ):
                raise OwnerConflict("reasoning_cycle_index_invalid")
            historical[cycle_ref] = _CurrentCycle(
                event.revision, cycle_ref, question
            )
        if historical:
            return max(historical.values(), key=lambda value: value.revision)

        # Before AE emits the Reasoning request, the initial-cycle event is a
        # routing index only; current AE/RG reads remain authoritative.
        candidates: list[_CurrentCycle] = []
        for event in self._feed.read_event_type(_CYCLE_EVENT):
            cycle_ref = event.payload.get("cycle_ref")
            question_ref = event.payload.get("question_ref")
            if not isinstance(cycle_ref, str) or not isinstance(question_ref, str):
                raise OwnerConflict("reasoning_cycle_index_invalid")
            foreground = next(
                (
                    value
                    for value in self._advancement_engine.query_active_foregrounds(
                        stage="reasoning"
                    )
                    if value.get("cycle_ref") == cycle_ref
                ),
                None,
            )
            if foreground is None:
                continue
            question = self._research_graph.query_question_by_ref(question_ref)
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("reasoning_cycle_index_invalid")
            candidates.append(_CurrentCycle(event.revision, cycle_ref, question))
        return None if not candidates else candidates[-1]


def _is_current_reasoning_foreground(
    foreground: dict[str, object] | None,
    cycle_ref: str,
) -> bool:
    return bool(
        foreground is not None
        and foreground.get("cycle_ref") == cycle_ref
        and foreground.get("stage") == "reasoning"
        and foreground.get("status") == "active"
        and type(foreground.get("epoch")) is int
        and cast(int, foreground["epoch"]) >= 1
    )


def _frozen_evidence_closure(
    context_pack: dict[str, object],
) -> tuple[dict[str, object], ...]:
    values: list[dict[str, object]] = []
    literature = context_pack.get("question_literature_input")
    if isinstance(literature, dict) and literature.get("kind") == "revision":
        binding = literature.get("binding")
        records = binding.get("records") if isinstance(binding, dict) else None
        if not isinstance(records, list):
            raise OwnerConflict("reasoning_literature_binding_invalid")
        for record in records:
            if not isinstance(record, dict):
                raise OwnerConflict("reasoning_literature_binding_invalid")
            values.append(
                {
                    "kind": "LiteratureRecord",
                    "ref": record.get("ref"),
                    "evidence_basis": record.get("evidence_basis"),
                    "evidence_basis_ref": record.get("evidence_basis_ref"),
                }
            )

    try:
        values.extend(plan_evidence_reuse_leaves(context_pack))
    except ReasoningContractError as error:
        raise OwnerConflict(str(error)) from error

    try:
        values.extend(current_target_evidence_leaves(context_pack))
    except ReasoningContractError as error:
        raise OwnerConflict(str(error)) from error
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
    reason_code: str,
) -> dict[str, object]:
    return {
        "eligibility": {
            "status": "not_eligible",
            "cycle_ref": cycle_ref,
            "question_ref": question_ref,
            "reason": {"code": reason_code},
            "next_stage": None,
        },
        "stage_run_request": None,
        "run": None,
        "autonomous_creation_checkpoint": None,
        "reasoning_acceptance": _not_attempted_acceptance(),
        "transition": {"status": "not_attempted"},
        "stage_commit": None,
    }


def _public_request(request: StageRunRequest) -> dict[str, object]:
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": "Reasoning",
        "epoch": request.epoch,
        "accepted_question_binding": request.accepted_question.as_dict(),
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "context_pack": request.context_pack,
        "receipt": request.receipt.as_public_dict(),
    }


def _public_acceptance(
    execution: AttemptExecution | None,
    content: AcceptedReasoningContent | None,
    decision,
) -> dict[str, object]:
    if execution is None:
        return _not_attempted_acceptance()
    result: dict[str, object] = {
        "status": "awaiting_content",
        "content": {"status": "not_attempted"},
        "domain": {"status": "not_attempted"},
    }
    if content is None:
        return result
    result["status"] = "awaiting_domain"
    result["disposition"] = content.scientific_outcome.get("disposition")
    result["content"] = {
        "status": "accepted",
        "content_ref": content.content_ref,
        "receipt": content.receipt.as_public_dict(),
    }
    if decision is None:
        return result
    if decision.decision == "accepted":
        result["status"] = "accepted"
        result["outcome_ref"] = decision.outcome_ref
        result["domain"] = {
            "status": "accepted",
            "outcome_ref": decision.outcome_ref,
            "receipt": decision.receipt.as_public_dict(),
        }
    else:
        reason_code = (
            decision.reason_code or "reasoning_outcome_requires_revision"
        )
        result["status"] = "rejected"
        result["rejection"] = {
            "code": reason_code,
            "feedback": list(decision.feedback),
        }
        result["domain"] = {
            "status": "rejected",
            "reason": {"code": reason_code},
            "receipt": decision.receipt.as_public_dict(),
        }
    return result


def _public_transition(
    content: AcceptedReasoningContent | None,
    decision,
) -> dict[str, object]:
    if content is None or decision is None or decision.decision != "accepted":
        return {"status": "not_attempted"}
    transition = dict(content.transition)
    transition["status"] = "proposed"
    transition["ref"] = content.transition_ref
    transition["hash"] = content.transition_hash
    return transition


def _public_commit(
    commit: StageCommit,
    *,
    content: AcceptedReasoningContent | None,
    decision,
) -> dict[str, object]:
    disposition = (
        None if content is None else content.scientific_outcome.get("disposition")
    )
    if (
        commit.stage != "reasoning"
        or commit.outcome_kind != "reasoning_outcome"
        or commit.disposition != "completed"
        or disposition not in SCIENTIFIC_OUTCOMES
        or decision is None
        or decision.decision != "accepted"
        or decision.outcome_ref != commit.outcome_ref
    ):
        raise OwnerConflict("reasoning_stage_commit_disposition_invalid")
    return {
        "status": "Completed",
        "commit_ref": commit.commit_ref,
        "stage_commit_ref": commit.commit_ref,
        "request_ref": commit.request_ref,
        "cycle_ref": commit.cycle_ref,
        "stage": "Reasoning",
        "epoch": commit.epoch,
        "run_ref": commit.run_ref,
        "outcome_ref": commit.outcome_ref,
        "outcome_kind": "ScientificOutcome",
        "disposition": disposition,
        "run_completion_receipt": commit.run_completion_receipt.as_public_dict(),
        "outcome_receipt": commit.outcome_receipt.as_public_dict(),
        "receipt": commit.receipt.as_public_dict(),
        "transition_kind": content.transition.get("kind"),
        "transition_ref": content.transition_ref,
    }
