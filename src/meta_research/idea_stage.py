from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from meta_research.feed import DurableFeed
from meta_research.idea_skill import (
    IdeaSkillDraft,
    IdeaSkillContractError,
    IdeaSkillProvider,
    IdeaSkillRequest,
    IdeaSkillUnavailable,
    review_record,
    validate_idea_skill_draft,
    validate_idea_skill_result,
)
from meta_research.idea_contract import material_outcome_hash
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    StageCommit,
    StageRunRequest,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    AttemptExecution,
    IdeaStageRun,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_graph import (
    AcceptedQuestion,
    IdeaOutcomeDecision,
    ResearchGraphInterface,
)
from meta_research.owners.research_memory import (
    AcceptedIdeaOutcomeContent,
    ResearchMemoryInterface,
)


IDEA_CONTEXT_PACK_SCHEMA_REF = "meta-research/idea-context-pack/v2"
_CYCLE_EVENT = "advancement_engine.initial_cycle_activated"


@dataclass(frozen=True)
class _CurrentCycle:
    revision: int
    cycle_ref: str
    question: AcceptedQuestion


@dataclass(frozen=True)
class _CycleStep:
    advanced: bool
    provider_boundary_attempted: bool = False


class IdeaStageWorker:
    """Stateless application worker for the Idea slice, never a State Owner.

    Every ``process_once`` pass issues at most one Owner command per Cycle. All
    routing decisions are rebuilt from verified Owner queries, so a fresh
    worker can safely resume after any durable boundary and one correcting Run
    cannot starve another Quest.
    """

    def __init__(
        self,
        feed: DurableFeed,
        advancement_engine: AdvancementEngineInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        research_graph: ResearchGraphInterface,
        provider: IdeaSkillProvider,
        human_collaboration: HumanCollaborationInterface | None = None,
    ) -> None:
        self._feed = feed
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._provider = provider
        self._human_collaboration = human_collaboration
        self._transient_error: str | None = None
        self._provider_cursor_cycle_ref: str | None = None

    @property
    def transient_error(self) -> str | None:
        """Return the last adapter failure for daemon health, never as truth."""

        return self._transient_error

    def start(self, idempotency_key: str) -> dict[str, object]:
        """Freeze the current Question into an AE request and admit its Run."""

        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("idempotency_key_invalid")
        current = self._discover_current_cycle()
        if current is None:
            raise OwnerConflict("idea_stage_not_eligible")
        request = self._advancement_engine.query_idea_stage_request(
            current.cycle_ref
        )
        if request is None:
            request = self._advancement_engine.ensure_idea_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                context_pack=self._context_pack(current),
                idempotency_key=_operation_key(
                    "idea-request",
                    current.cycle_ref,
                    idempotency_key,
                ),
            )
        run = self._agent_runtime.query_idea_stage_run(request.request_ref)
        if run is None:
            try:
                runtime_binding = self._provider.runtime_binding()
            except IdeaSkillUnavailable as error:
                self._transient_error = error.code
                return self.query_current()
            self._agent_runtime.admit_idea_stage(
                request,
                _operation_key("idea-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
        return self.query_current()

    def process_once(self) -> bool:
        """Cross one durable boundary without abandoning an older Quest.

        A provider failure for one cycle must not hide another recoverable
        cycle.  Discovery is only an index; every candidate is revalidated at
        RG and AE before a command is issued.
        """

        if self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=("idea_primary", "idea_review"),
        ):
            return True
        transient_error: str | None = None
        advanced = False
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
            step = self._process_cycle(current)
            if step.advanced:
                advanced = True
            if self._transient_error is not None:
                transient_error = transient_error or self._transient_error
            # One provider boundary can legitimately consume the full provider
            # deadline. Later provider work belongs to the next daemon pass so
            # the watchdog does not scale with the number of active Cycles.
            if step.provider_boundary_attempted:
                self._provider_cursor_cycle_ref = current.cycle_ref
                break
        self._transient_error = transient_error
        return advanced

    def _process_cycle(self, current: _CurrentCycle) -> _CycleStep:
        """Cross at most one Owner boundary for one revalidated cycle."""

        request = self._advancement_engine.query_idea_stage_request(
            current.cycle_ref
        )
        if request is None:
            self._advancement_engine.ensure_idea_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                context_pack=self._context_pack(current),
                idempotency_key=_operation_key(
                    "idea-request", current.cycle_ref, "worker"
                ),
            )
            return _CycleStep(True)
        run = self._agent_runtime.query_idea_stage_run(request.request_ref)
        if run is None:
            try:
                runtime_binding = self._provider.runtime_binding()
            except IdeaSkillUnavailable as error:
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            self._agent_runtime.admit_idea_stage(
                request,
                _operation_key("idea-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
            return _CycleStep(True, provider_boundary_attempted=True)
        managed = self._agent_runtime.query_managed_run(run.run_ref)
        if managed is not None and managed["status"] not in {"running", "completed"}:
            return _CycleStep(False)
        if self._advancement_engine.query_idea_stage_commit(request.request_ref):
            return _CycleStep(False)

        execution = run.execution
        if execution is None:
            return self._execute_attempt(current, request, run)

        content = self._research_memory.query_idea_outcome_content(
            execution.submission_ref
        )
        if content is None:
            self._research_memory.accept_idea_outcome_content(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=execution.submission_ref,
                outcome=execution.outcome,
                reviewed_draft=execution.reviewed_draft,
                review=execution.review,
                execution_receipt=execution.receipt,
            )
            return _CycleStep(True)

        decision = self._research_graph.query_idea_outcome_decision(
            execution.submission_ref
        )
        if decision is None:
            question_content = self._accepted_question_content(
                request.accepted_question
            )
            self._research_graph.decide_idea_outcome(
                accepted_question=request.accepted_question,
                question_content=question_content,
                content=content,
                execution_receipt=execution.receipt,
            )
            return _CycleStep(True)

        if decision.decision == "rejected":
            self._agent_runtime.continue_after_idea_rejection(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "idea-revise",
                    run.run_ref,
                    run.attempt_ref,
                    decision.receipt.receipt_ref,
                ),
            )
            return _CycleStep(True)
        if decision.decision != "accepted" or decision.outcome_ref is None:
            raise OwnerConflict("idea_outcome_decision_invalid")

        if run.completion is None:
            self._agent_runtime.complete_idea_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                outcome_ref=decision.outcome_ref,
                decision_receipt=decision.receipt,
                idempotency_key=_operation_key(
                    "idea-complete",
                    run.run_ref,
                    run.attempt_ref,
                    decision.outcome_ref,
                ),
            )
            return _CycleStep(True)

        if decision.outcome_kind not in {"idea_set", "no_viable_candidate"}:
            raise OwnerConflict("idea_outcome_kind_invalid")

        self._advancement_engine.commit_idea_stage(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            outcome_ref=decision.outcome_ref,
            outcome_kind=decision.outcome_kind,
            run_completion_receipt=run.completion.receipt,
            outcome_receipt=decision.receipt,
            idempotency_key=_operation_key(
                "idea-commit", request.request_ref, decision.outcome_ref
            ),
        )
        return _CycleStep(True)

    def query_current(self) -> dict[str, object]:
        """Compose the fixed five-slot public Idea Stage projection."""

        current = self._discover_current_cycle()
        if current is None:
            return {
                "eligibility": {
                    "status": "not_eligible",
                    "cycle_ref": None,
                    "reason": {"code": "accepted_cycle_unavailable"},
                },
                "stage_run_request": None,
                "run": None,
                "outcome_acceptance": _not_attempted_acceptance(),
                "stage_commit": None,
            }
        request = self._advancement_engine.query_idea_stage_request(
            current.cycle_ref
        )
        if request is None:
            return {
                "eligibility": {
                    "status": "eligible",
                    "cycle_ref": current.cycle_ref,
                    "reason": None,
                },
                "stage_run_request": None,
                "run": None,
                "outcome_acceptance": _not_attempted_acceptance(),
                "stage_commit": None,
            }
        run = self._agent_runtime.query_idea_stage_run(request.request_ref)
        commit = self._advancement_engine.query_idea_stage_commit(
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
                "reason": None,
            },
            "stage_run_request": _public_request(request),
            "run": None if run is None else _public_run(run),
            "outcome_acceptance": _public_acceptance(
                lineage_execution, content, decision
            ),
            "stage_commit": (
                None
                if commit is None
                else _public_commit(commit)
            ),
        }

    def query_current_question(self) -> dict[str, object] | None:
        """Read the current accepted Question through RG and RM seams."""

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
        current: _CurrentCycle,
        request: StageRunRequest,
        run: IdeaStageRun,
    ) -> _CycleStep:
        predecessor = run.predecessor_execution
        rejection = run.rejection_receipt
        decision: IdeaOutcomeDecision | None = None
        predecessor_hash: str | None = None
        if (predecessor is None) != (rejection is None):
            raise OwnerConflict("rejection_lineage_incomplete")
        if predecessor is not None and rejection is not None:
            decision = self._research_graph.query_idea_outcome_decision(
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
            predecessor_hash = material_outcome_hash(predecessor.outcome)
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
        try:
            runtime_binding = self._provider.runtime_binding()
        except IdeaSkillUnavailable as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        if runtime_binding != run.runtime_binding:
            self._transient_error = "idea_runtime_binding_drift"
            return _CycleStep(False, provider_boundary_attempted=True)
        invocation = (
            run.primary_invocation
            if run.primary_draft is None
            else run.review_invocation
        )
        job_ref = invocation.operation_ref
        unit_ref = invocation.invocation_ref
        skill_request = IdeaSkillRequest(
            stage_request_ref=request.request_ref,
            question_ref=current.question.question_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            accepted_question_content=accepted_content,
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
            try:
                self._agent_runtime.begin_provider_unit(
                    unit_ref=unit_ref,
                    operation_ref=job_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    unit_kind="idea_primary",
                )
            except OwnerConflict as error:
                self._transient_error = error.code
                return _CycleStep(False, provider_boundary_attempted=True)
            provider_safe = True
            try:
                try:
                    draft = self._provider.generate_draft(skill_request)
                    draft_hash = validate_idea_skill_draft(skill_request, draft)
                except IdeaSkillUnavailable as error:
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
                except IdeaSkillContractError as error:
                    self._transient_error = str(error)
                    return _CycleStep(False, provider_boundary_attempted=True)
                checkpoint = self._agent_runtime.record_idea_primary_draft(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=draft.primary_session_ref,
                    runtime_binding=run.runtime_binding,
                    draft=draft.draft,
                    adapter_kind=draft.adapter_kind,
                    idempotency_key=_operation_key(
                        "idea-primary", run.run_ref, run.attempt_ref, draft_hash
                    ),
                )
                if checkpoint.draft_hash != draft_hash:
                    raise OwnerConflict("idea_primary_draft_hash_mismatch")
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
        draft = IdeaSkillDraft(
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
                unit_kind="idea_review",
            )
        except OwnerConflict as error:
            self._transient_error = error.code
            return _CycleStep(False, provider_boundary_attempted=True)
        provider_safe = True
        try:
            try:
                result = self._provider.review_draft(skill_request, draft)
                draft_hash, outcome_hash, _review_hash = validate_idea_skill_result(
                    skill_request,
                    result,
                    predecessor_material_outcome_hash=predecessor_hash,
                )
            except IdeaSkillUnavailable as error:
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
            except IdeaSkillContractError as error:
                self._transient_error = str(error)
                return _CycleStep(False, provider_boundary_attempted=True)
            review = review_record(
                result,
                draft_hash=draft_hash,
                outcome_hash=outcome_hash,
            )
            submission_ref = "idea_submission_" + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "outcome_hash": outcome_hash,
                    "review_hash": canonical_hash(review),
                }
            )[:32]
            self._agent_runtime.record_idea_attempt_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=submission_ref,
                native_session_ref=result.primary_session_ref,
                runtime_binding=run.runtime_binding,
                outcome=result.final_outcome,
                reviewed_draft=result.reviewed_draft,
                review=review,
                idempotency_key=_operation_key(
                    "idea-execute", run.run_ref, run.attempt_ref
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

    def _accepted_facts(
        self, execution: AttemptExecution | None
    ) -> tuple[AcceptedIdeaOutcomeContent | None, IdeaOutcomeDecision | None]:
        if execution is None:
            return None, None
        content = self._research_memory.query_idea_outcome_content(
            execution.submission_ref
        )
        if content is None:
            return None, None
        return content, self._research_graph.query_idea_outcome_decision(
            execution.submission_ref
        )

    def _accepted_question_content(self, binding) -> dict[str, object]:
        content = self._research_memory.read_question_content(
            binding.content_ref, binding.content_hash
        )
        if canonical_hash(content) != binding.content_hash:
            raise OwnerConflict("accepted_question_content_mismatch")
        return content

    def _context_pack(self, current: _CurrentCycle) -> dict[str, object]:
        reference_query = getattr(
            self._research_graph,
            "query_evidence_reference_state",
            self._research_graph.query_evidence_state,
        )
        evidence_revision, evidence_refs = reference_query(
            current.question.quest_ref
        )
        quest = self._research_graph.query_quest(
            current.question.initialization_id
        )
        if quest is None or quest.quest_ref != current.question.quest_ref:
            raise OwnerConflict("idea_question_quest_binding_invalid")
        snapshot = self._research_memory.query_literature_snapshot_for_basis(
            quest.initialization_id,
            quest.draft_revision,
            quest.draft_hash,
        )
        guidance_bindings: list[dict[str, object]] = []
        if self._human_collaboration is not None:
            guidance_bindings = (
                self._human_collaboration.query_active_guidance_bindings(
                    f"quest:{quest.quest_ref}"
                )
            )
            for binding in guidance_bindings:
                self._human_collaboration.verify_guidance_binding(binding)
            guidance_bindings.sort(
                key=lambda item: (
                    str(item["scope_ref"]),
                    str(item["constraint_ref"]),
                    int(cast(int, item["revision"])),
                )
            )
        return {
            "schema_ref": IDEA_CONTEXT_PACK_SCHEMA_REF,
            "cycle_ref": current.cycle_ref,
            "accepted_question_binding": current.question.as_binding().as_dict(),
            "accepted_evidence_refs": list(evidence_refs),
            "evidence_reference_revision": evidence_revision,
            "literature_binding": (
                None if snapshot is None else snapshot.as_context_binding()
            ),
            "prior_accepted_bindings": [],
            "active_guidance_bindings": guidance_bindings,
        }

    def _discover_current_cycle(self) -> _CurrentCycle | None:
        """Return the newest revalidated cycle for the public foreground view."""

        candidates = self._discover_cycles()
        if candidates:
            return candidates[-1]
        # The public Idea projection remains inspectable after AE advances the
        # same Cycle to Plan.  Worker discovery below is still current-stage
        # only, so this historical fallback cannot derive new Idea work.
        historical: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds():
            cycle_ref = cast(str, foreground["cycle_ref"])
            request = self._advancement_engine.query_idea_stage_request(cycle_ref)
            if request is None or self._advancement_engine.query_idea_stage_commit(
                request.request_ref
            ) is None:
                continue
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("idea_cycle_index_invalid")
            historical.append(
                _CurrentCycle(cast(int, foreground["epoch"]), cycle_ref, question)
            )
        return None if not historical else historical[-1]

    def _discover_cycles(self) -> tuple[_CurrentCycle, ...]:
        """Read current Grants; historical activation events are only an audit log."""

        values: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds(
            stage="idea"
        ):
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground["quest_ref"]:
                raise OwnerConflict("idea_cycle_index_invalid")
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


def _public_request(request: StageRunRequest) -> dict[str, object]:
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": "Idea",
        "epoch": request.epoch,
        "accepted_question_binding": request.accepted_question.as_dict(),
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "receipt": request.receipt.as_public_dict(),
    }


def _public_run(run: IdeaStageRun) -> dict[str, object]:
    execution = run.execution
    status = run.status
    if status == "running":
        status = "admitted"
    fence_status = "current"
    if status in {"terminated", "suspended_fenced", "reconciliation_required"}:
        fence_status = "revoked"
    elif run.completion is not None:
        fence_status = "completed"
    elif execution is not None:
        fence_status = "submitted"
    review = None
    if execution is not None:
        findings = execution.review.get("findings", [])
        dispositions = execution.review.get("dispositions", [])
        review_mode = execution.review.get("review_mode")
        reviewer_agent_ref = execution.review.get("reviewer_agent_ref")
        review = {
            "status": "completed",
            "review_mode": (
                review_mode
                if isinstance(review_mode, str)
                else "legacy_external_session"
            ),
            "finding_count": len(findings) if isinstance(findings, list) else 0,
            "disposition_count": (
                len(dispositions) if isinstance(dispositions, list) else 0
            ),
        }
        if isinstance(reviewer_agent_ref, str):
            review["reviewer_agent_ref"] = reviewer_agent_ref
        legacy_reviewer_session_ref = execution.review.get(
            "reviewer_session_ref"
        )
        if isinstance(legacy_reviewer_session_ref, str):
            # Preserve the v1 public field additively for already-issued
            # immutable review payloads. New v2 reviews never write it.
            review["reviewer_session_ref"] = legacy_reviewer_session_ref
    return {
        "status": status,
        "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref,
        "attempt_generation": run.attempt_generation,
        "root_session_ref": run.root_session_ref,
        "native_session_ref": run.native_session_ref,
        "provider_operations": {
            "primary": {
                "invocation_ref": run.primary_invocation.invocation_ref,
                "status": run.primary_invocation.status,
                "request_hash": run.primary_invocation.request_hash,
                "response_hash": run.primary_invocation.response_hash,
            },
            "review": {
                "invocation_ref": run.review_invocation.invocation_ref,
                "status": run.review_invocation.status,
                "request_hash": run.review_invocation.request_hash,
                "response_hash": run.review_invocation.response_hash,
            },
        },
        "primary_draft_checkpoint": (
            None
            if run.primary_draft is None
            else {
                "status": "recorded",
                "draft_hash": run.primary_draft.draft_hash,
                "adapter_kind": run.primary_draft.adapter_kind,
            }
        ),
        "runtime_binding_hash": run.runtime_binding_hash,
        "runtime_binding": run.runtime_binding.as_dict(),
        "fence_ref": run.fence_ref,
        "fence_status": fence_status,
        "blocker": (
            None
            if run.failure_code is None
            else {
                "status": "durable",
                "reason": {"code": run.failure_code},
            }
        ),
        "recovery_checkpoint": run.recovery_checkpoint,
        "submission_ref": None if execution is None else execution.submission_ref,
        "attempt_execution_receipt": (
            None if execution is None else execution.receipt.as_public_dict()
        ),
        "completion_receipt": (
            None if run.completion is None else run.completion.receipt.as_public_dict()
        ),
        "review": review,
    }


def _public_acceptance(
    execution: AttemptExecution | None,
    content: AcceptedIdeaOutcomeContent | None,
    decision: IdeaOutcomeDecision | None,
) -> dict[str, object]:
    if execution is None:
        return _not_attempted_acceptance()
    result: dict[str, object] = {
        "status": "awaiting_content",
        "outcome_kind": execution.outcome.get("kind"),
        "content": {"status": "not_attempted"},
        "domain": {"status": "not_attempted"},
    }
    if content is None:
        return result
    result["status"] = "awaiting_domain"
    result["outcome_kind"] = execution.outcome.get("kind")
    result["content"] = {
        "status": "accepted",
        "content_ref": content.content_ref,
        "receipt": content.receipt.as_public_dict(),
    }
    if decision is None:
        return result
    result["outcome_ref"] = decision.outcome_ref
    if decision.decision == "accepted":
        result["status"] = "accepted"
        result["domain"] = {
            "status": "accepted",
            "receipt": decision.receipt.as_public_dict(),
        }
    else:
        reason = {"code": decision.reason_code or "idea_outcome_requires_revision"}
        result["status"] = "rejected"
        result["rejection"] = {
            "code": "idea_outcome_requires_revision",
            "feedback": list(decision.feedback),
        }
        result["domain"] = {
            "status": "rejected",
            "reason": reason,
            "receipt": decision.receipt.as_public_dict(),
        }
    return result


def _public_commit(commit: StageCommit) -> dict[str, object]:
    outcome_contract = {
        "idea_set": ("IdeaSet", "Plan"),
        "no_viable_candidate": ("NoViableCandidate", "Reasoning"),
    }
    if (
        commit.outcome_kind not in outcome_contract
        or commit.disposition != "completed"
    ):
        raise OwnerConflict("stage_commit_disposition_invalid")
    public_outcome_kind, next_stage = outcome_contract[commit.outcome_kind]
    return {
        "status": commit.disposition.title(),
        "commit_ref": commit.commit_ref,
        "stage_commit_ref": commit.commit_ref,
        "request_ref": commit.request_ref,
        "cycle_ref": commit.cycle_ref,
        "stage": "Idea",
        "epoch": commit.epoch,
        "run_ref": commit.run_ref,
        "outcome_ref": commit.outcome_ref,
        "outcome_kind": public_outcome_kind,
        "run_completion_receipt": commit.run_completion_receipt.as_public_dict(),
        "outcome_receipt": commit.outcome_receipt.as_public_dict(),
        "receipt": commit.receipt.as_public_dict(),
        "next_stage": next_stage,
    }
