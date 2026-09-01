from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import cast

from meta_research.bundle_contract import (
    BUNDLE_CONTEXT_PACK_SCHEMA_REF,
    BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF,
    target_execution_assertion,
    target_execution_authorization_requirement,
)
from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_BASIS_KIND,
    BundleExhaustionEvidence,
    BundleExhaustionProposal,
    bundle_exhaustion_exploration_record_from_claim,
)
from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    BundleReport,
    SemanticBarrier,
    TechnicalBlocker,
    projection_plain_value,
)
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillContractError,
    BundleSkillDraft,
    BundleExhaustionSkillResult,
    BundleSkillProvider,
    BundleSkillRequest,
    BundleSkillResult,
    BundleSkillUnavailable,
    BundleTargetBatchRequest,
    RecoverableBundleSkillCandidateError,
    review_record,
    validate_bundle_dispatch_result,
    validate_bundle_skill_draft,
    validate_bundle_exhaustion_skill_result,
    validate_bundle_skill_result,
    validate_bundle_target_batch_result,
)
from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    normalized_completion_contract_from_dict,
)
from meta_research.feed import DurableFeed
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.idea_stage import _public_run
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    StageCommit,
    StageRunRequest,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    BundleInboxCheckpoint,
    BundleRuntimeBinding,
    BundleStageRun,
)
from meta_research.owners.common import (
    AcceptedFormalPlanBinding,
    AcceptedIdeaSetBinding,
    AcceptanceReceipt,
    OwnerConflict,
    VerifiedBundleReportReceipt,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_graph import (
    AcceptedQuestion,
    AcceptedTarget,
    AcceptedTargetGraph,
    FormalPlanDecision,
    ResearchGraphInterface,
    TargetCommit,
    TargetGraphRejection,
)
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_memory import (
    AcceptedPlanDocument,
    AssetIntakeRequest,
    ResearchMemoryInterface,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.target_commit_evidence import (
    TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
    target_commit_evidence_document,
    target_commit_metric_result,
    target_commit_evidence_provenance,
)


_CYCLE_EVENT = "advancement_engine.initial_cycle_activated"
_TARGET_AUTHORIZATION_OBLIGATION = (
    "决定是否仅为这一精确高风险 Target 授予一次执行权限。"
)
_TARGET_AUTHORIZATION_PURPOSE = (
    "只恢复对应 Target；同一 DAG 中其他普通 Target 继续推进。"
)
_TARGET_AUTHORIZATION_ACCEPTANCE_CONDITIONS = (
    "Human Collaboration 保存 exact granted authorization receipt。",
    "Agent Runtime 重验 current Target/spec 与同一 waiter generation。",
)


@dataclass(frozen=True)
class _CurrentCycle:
    revision: int
    cycle_ref: str
    question: AcceptedQuestion


@dataclass(frozen=True)
class _EligibleBundle:
    current: _CurrentCycle
    binding: AcceptedFormalPlanBinding
    accepted_idea_set: AcceptedIdeaSetBinding | None = None

    def accepted_formal_plan(self) -> AcceptedFormalPlanBinding:
        return self.binding


@dataclass(frozen=True)
class _TargetAuthorization:
    request_ref: str
    waiter_ref: str
    generation: int
    authorization_receipt_ref: str


class BundleStageWorker:
    """Recoverable Bundle orchestration through the four Owner Interfaces.

    The public Interface stays intentionally small: callers can advance one
    durable boundary or read the composed projection.  Target strategy,
    persistence, receipts, recovery, and execution stay behind that seam.
    """

    def __init__(
        self,
        feed: DurableFeed,
        advancement_engine: AdvancementEngineInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        research_graph: ResearchGraphInterface,
        provider: BundleSkillProvider,
        human_collaboration: HumanCollaborationInterface | None = None,
        harnesses: HarnessRuntime | None = None,
    ) -> None:
        self._feed = feed
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._provider = provider
        self._human_collaboration = human_collaboration
        self._harnesses = harnesses
        self._transient_error: str | None = None

    @property
    def transient_error(self) -> str | None:
        return self._transient_error

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        configure = getattr(self._provider, "configure_resident_mcp_endpoint", None)
        if callable(configure):
            configure(base_url)

    def process_once(self) -> bool:
        """Advance at most one durable Bundle boundary."""

        if self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=("bundle_primary", "bundle_review"),
        ):
            return True
        current = self._discover_current_cycle()
        if current is None:
            return False
        foreground = self._advancement_engine.query_foreground(
            current.question.quest_ref
        )
        if (
            foreground is None
            or foreground.get("cycle_ref") != current.cycle_ref
            or foreground.get("stage") != "bundle"
            or foreground.get("status") != "active"
        ):
            return False
        eligible, _reason, _next = self._qualify(current)
        if eligible is None:
            return False
        request = self._advancement_engine.query_bundle_stage_request(current.cycle_ref)
        if request is None:
            foreground = self._advancement_engine.query_foreground(
                current.question.quest_ref
            )
            epoch = None if foreground is None else foreground.get("epoch")
            if (
                foreground is None
                or foreground.get("cycle_ref") != current.cycle_ref
                or foreground.get("stage") != "bundle"
                or type(epoch) is not int
                or epoch < 1
            ):
                raise OwnerConflict("bundle_foreground_epoch_stale")
            accepted_formal_plan = eligible.accepted_formal_plan()
            accepted_idea_set = eligible.accepted_idea_set
            self._advancement_engine.ensure_bundle_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                accepted_formal_plan=accepted_formal_plan,
                accepted_idea_set=accepted_idea_set,
                context_pack={
                    "schema_ref": (
                        BUNDLE_CONTEXT_PACK_SCHEMA_REF
                        if accepted_idea_set is None
                        else BUNDLE_SUCCESSOR_CONTEXT_PACK_SCHEMA_REF
                    ),
                    "cycle_ref": current.cycle_ref,
                    "accepted_question_binding": current.question.as_binding().as_dict(),
                    "accepted_formal_plan_binding": accepted_formal_plan.as_dict(),
                    **(
                        {}
                        if accepted_idea_set is None
                        else {
                            "accepted_idea_set_binding": accepted_idea_set.as_dict()
                        }
                    ),
                },
                idempotency_key=_operation_key(
                    "bundle-request", current.cycle_ref, str(epoch)
                ),
            )
            return True
        self._assert_request(request, eligible)
        accepted_formal_plan = request.accepted_formal_plan
        if accepted_formal_plan is None:
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        if accepted_formal_plan.plan_document.get("gap_set") == []:
            if (
                self._advancement_engine.query_bundle_stage_commit(request.request_ref)
                is not None
            ):
                return False
            self._advancement_engine.skip_bundle_stage(
                request_ref=request.request_ref,
                formal_plan_ref=accepted_formal_plan.formal_plan_ref,
                formal_plan_receipt=accepted_formal_plan.formal_plan_receipt,
                idempotency_key=_operation_key("bundle-skip", request.request_ref),
            )
            return True
        run = self._agent_runtime.query_bundle_stage_run(request.request_ref)
        if run is None:
            runtime_binding = self._current_runtime_binding()
            if runtime_binding is None:
                return False
            self._agent_runtime.admit_bundle_stage(
                request,
                _operation_key("bundle-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
            self._transient_error = None
            return True
        managed = self._agent_runtime.query_managed_run(run.run_ref)
        if managed is not None and managed["status"] not in {"running", "completed"}:
            return False
        if run.status in {"running", "awaiting_acceptance"}:
            self._drain_bundle_inbox(run)
        if _bundle_primary_output_kind(run) == "exhaustion_assessment":
            accepted_exhaustion_evidence = (
                self._agent_runtime.query_bundle_exhaustion_evidence_for_run(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                )
            )
            if accepted_exhaustion_evidence is None:
                return self._execute_target_plan(request, run)
            return self._advance_exhaustion(request, run)
        if run.execution is None:
            return self._execute_target_plan(request, run)
        graph = self._research_graph.query_target_graph(request.request_ref)
        if graph is None:
            execution = run.execution
            rejection = self._research_graph.query_target_graph_rejection(
                execution.submission_ref
            )
            if rejection is not None:
                if (
                    rejection.request_ref != request.request_ref
                    or rejection.run_ref != run.run_ref
                    or rejection.attempt_ref != run.attempt_ref
                    or rejection.fence_ref != run.fence_ref
                    or rejection.target_plan_hash
                    != execution.material_outcome_hash
                    or rejection.execution_payload_hash != execution.payload_hash
                    or rejection.execution_receipt != execution.receipt
                ):
                    raise OwnerConflict("target_graph_rejection_binding_invalid")
                self._agent_runtime.continue_after_bundle_rejection(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    decision_receipt=rejection.receipt,
                    idempotency_key=_operation_key(
                        "bundle-target-graph-rejection-successor",
                        run.run_ref,
                        run.attempt_ref,
                        rejection.rejection_ref,
                    ),
                )
                return True
            self._research_graph.decide_target_graph_submission(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                submission_ref=execution.submission_ref,
                context_pack_ref=request.context_pack_ref,
                target_plan=execution.outcome,
                target_plan_hash=execution.material_outcome_hash,
                execution_payload_hash=execution.payload_hash,
                execution_receipt=execution.receipt,
            )
            return True
        formal_plan_content = (
            self._research_graph.query_formal_plan_content_acceptance(
                graph.formal_plan_ref
            )
        )
        if formal_plan_content is None:
            self._research_graph.accept_formal_plan_content(
                formal_plan_ref=graph.formal_plan_ref,
                idempotency_key=_operation_key(
                    "bundle-formal-plan-content",
                    request.request_ref,
                    graph.formal_plan_ref,
                ),
            )
            return True
        if (
            formal_plan_content.formal_plan_ref != graph.formal_plan_ref
            or formal_plan_content.plan_document_hash
            != accepted_formal_plan.plan_document_hash
        ):
            raise OwnerConflict("bundle_formal_plan_content_acceptance_invalid")
        formal_plan_projection = (
            self._research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
        )
        if formal_plan_projection is None:
            self._research_graph.accept_target_formal_plan_projection(
                graph_ref=graph.graph_ref,
                idempotency_key=_operation_key(
                    "bundle-formal-plan-projection",
                    request.request_ref,
                    graph.graph_ref,
                ),
            )
            return True
        if (
            formal_plan_projection.formal_plan.formal_plan_ref
            != graph.formal_plan_ref
            or formal_plan_projection.plan_document_hash
            != formal_plan_content.plan_document_hash
            or formal_plan_projection.source_acceptance_receipt
            != formal_plan_content.receipt
        ):
            raise OwnerConflict("bundle_formal_plan_projection_invalid")
        commits = self._research_graph.query_target_commits(graph.graph_ref)
        committed_refs = {commit.target_ref for commit in commits}
        _evidence_revision, evidence_catalog = (
            self._research_graph.query_plan_evidence_catalog(quest_ref=graph.quest_ref)
        )
        published_roots = {
            cast(str, evidence["target_commit_root_ref"])
            for evidence in evidence_catalog
        }
        for commit in commits:
            if commit.commit_ref not in published_roots:
                self._publish_target_commit_evidence(
                    quest_ref=graph.quest_ref,
                    commit=commit,
                )
                return True
        report_progress = self._advance_bundle_report_closure(
            request=request,
            run=run,
            graph=graph,
            formal_plan_content_receipt=formal_plan_content.receipt,
            formal_plan_projection_receipt=formal_plan_projection.receipt,
        )
        if report_progress is not None:
            return report_progress
        # A complete set of commits is only an input to the rolling planner.
        # Stage completion is exclusively authorized by the durable report
        # path above, after AR has reread every current terminal handoff and
        # exact FormalPlan measurement cell.
        if len(commits) == len(graph.targets) and not graph.strategy_complete:
            inbox_checkpoint = self._drain_bundle_inbox(run)
            proposals = self._agent_runtime.query_bundle_target_proposals(
                run.run_ref
            )
            pending = next(
                (
                    proposal
                    for proposal in reversed(proposals)
                    if proposal.graph_ref == graph.graph_ref
                    and proposal.base_generation == graph.head_generation
                    and proposal.base_head_receipt == graph.head_receipt
                    and self._operation_uses_inbox_checkpoint(
                        operation_kind="target_proposal",
                        operation_ref=proposal.proposal_ref,
                        checkpoint=inbox_checkpoint,
                    )
                ),
                None,
            )
            batch_current_targets = tuple(
                self._target_projection(target) for target in graph.targets
            )
            batch_target_commits = tuple(
                self._target_commit_projection(commit) for commit in commits
            )
            batch_operation_ref = run.review_invocation.operation_ref
            batch_unit_ref = _rolling_provider_unit_ref(
                operation_ref=batch_operation_ref,
                operation_name=f"target-batch-{graph.head_generation + 1}",
                attempt_ref=run.attempt_ref,
            )
            if pending is None:
                if run.native_session_ref is None:
                    raise OwnerConflict("bundle_native_session_missing")
                batch_request = BundleTargetBatchRequest(
                    stage_request_ref=request.request_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    graph_ref=graph.graph_ref,
                    formal_plan_ref=graph.formal_plan_ref,
                    context_pack_ref=graph.context_pack_ref,
                    context_pack_hash=graph.context_pack_hash,
                    plan_document=accepted_formal_plan.plan_document,
                    initial_target_plan=graph.target_plan,
                    base_generation=graph.head_generation,
                    base_head_receipt=graph.head_receipt.as_public_dict(),
                    current_targets=batch_current_targets,
                    target_commits=batch_target_commits,
                    root_session_ref=run.root_session_ref,
                    native_session_ref=run.native_session_ref,
                    runtime_binding=cast(
                        BundleRuntimeBinding, run.runtime_binding
                    ),
                    inbox_checkpoint=inbox_checkpoint.as_public_dict(),
                    job_ref=batch_operation_ref,
                )
                if not self._runtime_binding_is_current(run):
                    return False
                try:
                    self._agent_runtime.begin_provider_unit(
                        unit_ref=batch_unit_ref,
                        operation_ref=batch_operation_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        unit_kind="bundle_review",
                    )
                except OwnerConflict as error:
                    self._transient_error = error.code
                    return False
                provider_safe = True
                result = None
                try:
                    try:
                        result = self._provider.propose_target_batch(batch_request)
                        validate_bundle_target_batch_result(batch_request, result)
                    except BundleSkillUnavailable as error:
                        if error.code == "codex_operation_reconciliation_pending":
                            provider_safe = False
                        elif error.recovery_checkpoint is not None:
                            provider_safe = False
                            self._agent_runtime.record_stage_provider_hard_ceiling(
                                unit_ref=batch_unit_ref,
                                run_ref=run.run_ref,
                                attempt_ref=run.attempt_ref,
                                fence_ref=run.fence_ref,
                                failure_code=error.code,
                                provider_exit=error.recovery_checkpoint,
                            )
                        self._transient_error = error.code
                        return False
                    except BundleSkillContractError as error:
                        if result is None:
                            self._transient_error = str(error)
                            return False
                        failure_code = "bundle_review_result_contract_invalid"
                        try:
                            terminal = self._record_terminal_contract_failure(
                                unit_ref=batch_unit_ref,
                                run=run,
                                job_ref=batch_operation_ref,
                                operation_name=(
                                    f"target-batch-{graph.head_generation + 1}"
                                ),
                                native_session_ref=result.native_session_ref,
                                failure_code=failure_code,
                                detail_code=str(error),
                            )
                        except BundleSkillUnavailable as checkpoint_error:
                            provider_safe = False
                            self._transient_error = checkpoint_error.code
                            return False
                        if terminal:
                            provider_safe = False
                        self._transient_error = failure_code
                        return False
                    self._agent_runtime.record_bundle_target_proposal(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        native_session_ref=result.native_session_ref,
                        graph_ref=graph.graph_ref,
                        base_generation=graph.head_generation,
                        base_head_receipt=graph.head_receipt,
                        strategy_update=result.strategy_update,
                        inbox_checkpoint=inbox_checkpoint,
                        idempotency_key=_operation_key(
                            "bundle-target-batch",
                            run.run_ref,
                            graph.head_receipt.receipt_ref,
                            inbox_checkpoint.checkpoint_ref,
                            inbox_checkpoint.checkpoint_hash,
                        ),
                    )
                    self._transient_error = None
                    return True
                finally:
                    if provider_safe:
                        self._agent_runtime.acknowledge_provider_safe_point(
                            unit_ref=batch_unit_ref,
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                        )
            self._acknowledge_rolling_provider_boundary(
                run,
                unit_ref=batch_unit_ref,
            )
            try:
                self._research_graph.append_target_batch(
                    graph_ref=graph.graph_ref,
                    proposal_ref=pending.proposal_ref,
                    proposal=pending.proposal,
                    proposal_hash=pending.proposal_hash,
                    proposal_receipt=pending.receipt,
                )
            except OwnerConflict as error:
                if error.code == "completed_strategy_cell_coverage_invalid":
                    self._transient_error = "bundle_strategy_incomplete"
                    return False
                raise
            self._transient_error = None
            return True
        frontier = self._research_graph.query_target_frontier(graph.graph_ref)
        authorizations: dict[str, _TargetAuthorization] = {}
        for target in frontier:
            if target.spec.get("risk_class") != "high":
                continue
            authorization, changed = self._advance_target_authorization(
                graph=graph,
                target=target,
                run=run,
            )
            if changed:
                return True
            if authorization is not None:
                authorizations[target.target_ref] = authorization
        launchable = tuple(
            target
            for target in frontier
            if target.spec.get("risk_class") != "high"
            or target.target_ref in authorizations
        )
        human_requests: dict[str, dict[str, object] | None] = {}
        for target in frontier:
            if target.spec.get("risk_class") != "high":
                continue
            projection = self._research_graph.query_target_candidate_projection(
                target_ref=target.target_ref
            )
            if projection is None:
                raise OwnerConflict("target_candidate_projection_missing")
            assertion = _target_authorization_assertion(
                graph,
                target,
                target_spec_hash=projection.projection_digest,
            )
            requirement = _target_authorization_requirement(
                graph,
                target,
                target_spec_hash=projection.projection_digest,
            )
            human_requests[target.target_ref] = self._target_human_request(
                graph.quest_ref,
                assertion,
                requirement=requirement,
                run=run,
            )
        dispatch_frontier = tuple(
            self._dispatch_target(
                graph=graph,
                target=target,
                run=run,
                authorization=authorizations.get(target.target_ref),
                human_request=human_requests.get(target.target_ref),
            )
            for target in frontier
        )
        dispatch_state = self._dispatch_state(graph, commits, run=run)
        inbox_checkpoint = self._drain_bundle_inbox(run)
        decisions = self._agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        latest = decisions[-1] if decisions else None
        root_human_request_needed = any(
            request is None for request in human_requests.values()
        )
        same_input = latest is not None and (
            latest.graph_ref == graph.graph_ref
            and latest.frontier == dispatch_frontier
            and latest.state == dispatch_state
            and self._operation_uses_inbox_checkpoint(
                operation_kind="dispatch",
                operation_ref=latest.decision_ref,
                checkpoint=inbox_checkpoint,
            )
            and (
                latest.action != "dispatch"
                or latest.selected_target_ref
                in {target.target_ref for target in launchable}
            )
        )
        pending_dispatch = (
            latest
            if same_input
            and latest.action == "dispatch"
            and latest.selected_target_ref
            in {target.target_ref for target in launchable}
            else None
        )
        coordination_needed = bool(frontier)
        dispatch_generation = (
            pending_dispatch.generation
            if pending_dispatch is not None
            else len(decisions) + 1
        )
        dispatch_operation_ref = run.review_invocation.operation_ref
        dispatch_unit_ref = _rolling_provider_unit_ref(
            operation_ref=dispatch_operation_ref,
            operation_name=f"dispatch-{dispatch_generation}",
            attempt_ref=run.attempt_ref,
        )
        if (
            pending_dispatch is None
            and coordination_needed
            and (not same_input or root_human_request_needed)
        ):
            if run.native_session_ref is None:
                raise OwnerConflict("bundle_native_session_missing")
            dispatch_request = BundleDispatchRequest(
                stage_request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                graph_ref=graph.graph_ref,
                generation=dispatch_generation,
                frontier=dispatch_frontier,
                state=dispatch_state,
                root_session_ref=run.root_session_ref,
                native_session_ref=run.native_session_ref,
                runtime_binding=cast(BundleRuntimeBinding, run.runtime_binding),
                inbox_checkpoint=inbox_checkpoint.as_public_dict(),
                job_ref=dispatch_operation_ref,
            )
            if not self._runtime_binding_is_current(run):
                return False
            try:
                self._agent_runtime.begin_provider_unit(
                    unit_ref=dispatch_unit_ref,
                    operation_ref=dispatch_operation_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    unit_kind="bundle_review",
                )
            except OwnerConflict as error:
                self._transient_error = error.code
                return False
            provider_safe = True
            result = None
            try:
                try:
                    result = self._provider.schedule_target(dispatch_request)
                    validate_bundle_dispatch_result(dispatch_request, result)
                    if (
                        result.action == "dispatch"
                        and result.selected_target_ref
                        not in {target.target_ref for target in launchable}
                    ):
                        self._transient_error = (
                            "target_high_risk_authorization_required"
                        )
                        return False
                except BundleSkillUnavailable as error:
                    if error.code == "codex_operation_reconciliation_pending":
                        provider_safe = False
                    elif error.recovery_checkpoint is not None:
                        provider_safe = False
                        self._agent_runtime.record_stage_provider_hard_ceiling(
                            unit_ref=dispatch_unit_ref,
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            failure_code=error.code,
                            provider_exit=error.recovery_checkpoint,
                        )
                    self._transient_error = error.code
                    return False
                except BundleSkillContractError as error:
                    if result is None:
                        self._transient_error = str(error)
                        return False
                    failure_code = "bundle_review_result_contract_invalid"
                    try:
                        terminal = self._record_terminal_contract_failure(
                            unit_ref=dispatch_unit_ref,
                            run=run,
                            job_ref=dispatch_operation_ref,
                            operation_name=f"dispatch-{dispatch_generation}",
                            native_session_ref=result.native_session_ref,
                            failure_code=failure_code,
                            detail_code=str(error),
                        )
                    except BundleSkillUnavailable as checkpoint_error:
                        provider_safe = False
                        self._transient_error = checkpoint_error.code
                        return False
                    if terminal:
                        provider_safe = False
                    self._transient_error = failure_code
                    return False
                self._agent_runtime.record_bundle_dispatch_decision(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=result.native_session_ref,
                    graph_ref=graph.graph_ref,
                    generation=dispatch_request.generation,
                    frontier=dispatch_frontier,
                    state=dispatch_state,
                    action=result.action,
                    selected_target_ref=result.selected_target_ref,
                    rationale=result.rationale,
                    inbox_checkpoint=inbox_checkpoint,
                    idempotency_key=_operation_key(
                        "bundle-dispatch", run.run_ref, str(len(decisions) + 1)
                    ),
                )
                self._transient_error = (
                    None
                    if result.action == "dispatch"
                    else "bundle_replan_required"
                    if result.action == "replan_required"
                    else "bundle_root_waiting"
                )
                return True
            finally:
                if provider_safe:
                    self._agent_runtime.acknowledge_provider_safe_point(
                        unit_ref=dispatch_unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                    )
        if pending_dispatch is not None:
            self._acknowledge_rolling_provider_boundary(
                run,
                unit_ref=dispatch_unit_ref,
            )
            launch_checkpoint = self._drain_bundle_inbox(run)
            if not self._operation_uses_inbox_checkpoint(
                operation_kind="dispatch",
                operation_ref=pending_dispatch.decision_ref,
                checkpoint=launch_checkpoint,
            ):
                self._transient_error = None
                return True
            target = next(
                target
                for target in launchable
                if target.target_ref == pending_dispatch.selected_target_ref
            )
            ack = self._agent_runtime.query_target_launch_ack(target.target_ref)
            admitted_now = False
            if ack is None:
                candidate_projection = (
                    self._research_graph.query_target_candidate_projection(
                        target_ref=target.target_ref
                    )
                )
                if candidate_projection is None:
                    self._research_graph.accept_target_candidate_projection(
                        target_ref=target.target_ref,
                        idempotency_key=_operation_key(
                            "target-candidate-projection", target.target_ref
                        ),
                    )
                    self._transient_error = None
                    return True
                launch_request = self._research_graph.query_target_launch_request(
                    target.target_ref
                )
                authorization = authorizations.get(target.target_ref)
                ack = self._agent_runtime.admit_target_launch(
                    launch_request,
                    dispatch_decision_ref=pending_dispatch.decision_ref,
                    idempotency_key=_operation_key(
                        "target-launch-admit", target.target_ref
                    ),
                    human_request_ref=(
                        None if authorization is None else authorization.request_ref
                    ),
                    human_waiter_ref=(
                        None if authorization is None else authorization.waiter_ref
                    ),
                    human_waiter_generation=(
                        None if authorization is None else authorization.generation
                    ),
                    human_authorization_receipt_ref=(
                        None
                        if authorization is None
                        else authorization.authorization_receipt_ref
                    ),
                )
                admitted_now = True
            if ack.target_ref != target.target_ref:
                raise OwnerConflict("target_launch_ack_invalid")
            if admitted_now:
                self._transient_error = "target_launch_admitted"
                return True
            current_frontier = self._agent_runtime.query_target_frontier_entry(
                target.target_ref
            )
            if current_frontier is not None:
                # Bundle owns launch and pre-activation only.  Once AR has a
                # durable TargetRun frontier, the independent Target daemon is
                # the sole lifecycle driver; Bundle only consumes Inbox and
                # authoritative frontier/handoff projections.
                self._transient_error = "target_root_running"
                return False
            # Launch is the last Bundle-owned Target mutation.  The light
            # Target daemon discovers this admitted launch independently and
            # wakes the one long-lived root Session; Bundle never drives that
            # Session or interprets its implementation/training progress.
            self._transient_error = "target_launch_pending"
            return False
        return False

    def _advance_bundle_report_closure(
        self,
        *,
        request: StageRunRequest,
        run: BundleStageRun,
        graph: AcceptedTargetGraph,
        formal_plan_content_receipt: AcceptanceReceipt,
        formal_plan_projection_receipt: AcceptanceReceipt,
    ) -> bool | None:
        """Advance one report boundary, or return ``None`` while work is open."""

        # A completed or retired Run can no longer build another candidate.
        # Reconcile its already accepted report instead.
        if run.completion is not None or run.status == "cancelled":
            accepted = self._agent_runtime.query_bundle_run_report(run.run_ref)
            if accepted is None:
                raise OwnerConflict("bundle_report_terminal_run_missing")
            return self._consume_bundle_report(request, run, accepted)

        disposition = self._bundle_report_disposition_hint(graph)
        if disposition is None:
            return None
        try:
            candidate = self._agent_runtime.build_bundle_report_candidate(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                disposition=disposition,
                formal_plan_content_receipt=formal_plan_content_receipt,
                formal_plan_projection_receipt=formal_plan_projection_receipt,
                target_graph_ref=graph.graph_ref,
                target_graph_receipt=graph.head_receipt,
            )
        except OwnerConflict as error:
            if error.code in {
                "bundle_report_realized_incomplete",
                "bundle_report_blocked_incomplete",
                "bundle_report_replan_incomplete",
                "bundle_report_replan_not_closed",
            }:
                return None
            raise

        latest = self._agent_runtime.query_bundle_run_report(run.run_ref)
        if latest is None or latest.report != candidate:
            report_hash = canonical_hash(projection_plain_value(candidate))
            self._agent_runtime.accept_bundle_report(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                report=candidate,
                formal_plan_content_receipt=formal_plan_content_receipt,
                formal_plan_projection_receipt=formal_plan_projection_receipt,
                target_graph_ref=graph.graph_ref,
                target_graph_receipt=graph.head_receipt,
                idempotency_key=_operation_key(
                    "bundle-report-accept", run.run_ref, report_hash
                ),
            )
            self._transient_error = None
            return True
        return self._consume_bundle_report(request, run, latest)

    def _bundle_report_disposition_hint(
        self,
        graph: AcceptedTargetGraph,
    ) -> str | None:
        """Read current AR terminal projections without inventing completion."""

        terminals: list[object] = []
        unlaunched = False
        for target in graph.targets:
            frontier = self._agent_runtime.query_target_frontier_entry(
                target.target_ref
            )
            notice = self._agent_runtime.query_target_work_notice(target.target_ref)
            if frontier is None:
                if notice is not None:
                    raise OwnerConflict("bundle_report_handoff_invalid")
                unlaunched = True
                continue
            if (
                frontier.state != "terminal"
                or frontier.currentness_known is not True
                or frontier.current is not True
            ):
                return None
            if notice is None:
                raise OwnerConflict("bundle_report_handoff_missing")
            if notice.target_ref != target.target_ref:
                raise OwnerConflict("bundle_report_handoff_invalid")
            handoff = self._agent_runtime.read_target_run_handoff(
                notice.handoff_manifest_ref
            )
            terminals.append(handoff.terminal)

        # The fixed prototype gives a technical blocker precedence.  Targets
        # that were never launched may only be omitted if AR later proves they
        # are descendants of that blocker while building the report.
        if any(type(terminal) is TechnicalBlocker for terminal in terminals):
            return "blocked"
        if unlaunched:
            return None
        if any(type(terminal) is SemanticBarrier for terminal in terminals):
            return "replan_required"
        if terminals and all(
            type(terminal) is AcceptedMeasurementClosure for terminal in terminals
        ):
            return "realized"
        return None

    def _consume_bundle_report(
        self,
        request: StageRunRequest,
        run: BundleStageRun,
        accepted: VerifiedBundleReportReceipt,
    ) -> bool:
        """Mechanically map one accepted report to its next Owner boundary."""

        disposition = accepted.report.disposition
        if disposition == "realized":
            completion = run.completion
            if completion is None:
                self._agent_runtime.complete_bundle_run(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    report_ref=accepted.report_ref,
                    decision_receipt=accepted.receipt,
                    idempotency_key=_operation_key(
                        "bundle-report-complete", run.run_ref, accepted.report_ref
                    ),
                )
                self._transient_error = None
                return True
            if (
                completion.outcome_ref != accepted.report_ref
                or completion.decision_receipt != accepted.receipt
            ):
                raise OwnerConflict("bundle_report_completion_invalid")
            if (
                self._advancement_engine.query_bundle_stage_commit(
                    request.request_ref
                )
                is None
            ):
                self._advancement_engine.commit_bundle_stage(
                    request_ref=request.request_ref,
                    run_ref=run.run_ref,
                    bundle_report_ref=accepted.report_ref,
                    run_completion_receipt=completion.receipt,
                    bundle_report_receipt=accepted.receipt,
                    idempotency_key=_operation_key(
                        "bundle-report-commit",
                        request.request_ref,
                        accepted.report_ref,
                    ),
                )
                self._finish_bundle_jobs(run)
                self._transient_error = None
                return True
            self._finish_bundle_jobs(run)
            self._transient_error = None
            return False

        recorded = self._advancement_engine.query_bundle_report_disposition(
            accepted.report_ref
        )
        if recorded is None:
            self._advancement_engine.record_bundle_report_disposition(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                bundle_report_ref=accepted.report_ref,
                bundle_report_receipt=accepted.receipt,
                idempotency_key=_operation_key(
                    "bundle-report-disposition",
                    request.request_ref,
                    accepted.report_ref,
                ),
            )
            self._transient_error = None
            return True
        if (
            recorded.request_ref != request.request_ref
            or recorded.run_ref != run.run_ref
            or recorded.report_ref != accepted.report_ref
            or recorded.report_hash != accepted.report_hash
            or recorded.disposition != disposition
            or recorded.report_receipt != accepted.receipt
        ):
            raise OwnerConflict("bundle_report_disposition_invalid")
        if disposition == "blocked":
            self._transient_error = "bundle_report_blocked"
            return False
        if disposition != "replan_required":
            raise OwnerConflict("bundle_report_disposition_invalid")

        retirement = self._agent_runtime.query_bundle_replan_run_retirement(
            recorded.disposition_ref
        )
        if retirement is None:
            self._agent_runtime.retire_bundle_run_for_replan(
                disposition_ref=recorded.disposition_ref,
                disposition_receipt=recorded.receipt,
                idempotency_key=_operation_key(
                    "bundle-replan-retire",
                    run.run_ref,
                    recorded.disposition_ref,
                ),
            )
            self._transient_error = None
            return True
        activation = self._advancement_engine.query_bundle_replan_activation(
            recorded.disposition_ref
        )
        if activation is None:
            self._advancement_engine.activate_bundle_replan(
                disposition_ref=recorded.disposition_ref,
                retirement_ref=retirement.retirement_ref,
                retirement_receipt=retirement.receipt,
                idempotency_key=_operation_key(
                    "bundle-replan-activate",
                    request.request_ref,
                    recorded.disposition_ref,
                ),
            )
            self._finish_bundle_jobs(run)
            self._transient_error = None
            return True
        self._finish_bundle_jobs(run)
        self._transient_error = "bundle_replan_activated"
        return False

    def _advance_target_authorization(
        self,
        *,
        graph: AcceptedTargetGraph,
        target: AcceptedTarget,
        run: BundleStageRun,
    ) -> tuple[_TargetAuthorization | None, bool]:
        projection = self._research_graph.query_target_candidate_projection(
            target_ref=target.target_ref
        )
        if projection is None:
            self._research_graph.accept_target_candidate_projection(
                target_ref=target.target_ref,
                idempotency_key=_operation_key(
                    "target-candidate-projection", target.target_ref
                ),
            )
            self._transient_error = None
            return None, True
        if projection.source_spec_hash != target.spec_hash:
            raise OwnerConflict("target_candidate_projection_source_invalid")
        assertion = _target_authorization_assertion(
            graph,
            target,
            target_spec_hash=projection.projection_digest,
        )
        requirement = _target_authorization_requirement(
            graph,
            target,
            target_spec_hash=projection.projection_digest,
        )
        request = self._target_human_request(
            graph.quest_ref,
            assertion,
            requirement=requirement,
            run=run,
        )
        if request is None:
            # The durable frontier below carries the exact command to the
            # current Bundle root Agent.  A daemon/provider path must never
            # impersonate that root by opening the formal request itself.
            self._transient_error = "target_high_risk_authorization_required"
            return None, False
        responses = cast(list[dict[str, object]], request.get("responses", []))
        if request.get("status") == "open":
            declined = tuple(
                cast(str, response["response_ref"])
                for response in responses
                if response.get("decision") == "declined"
                and isinstance(response.get("response_ref"), str)
            )
            if declined:
                self._agent_runtime.evaluate_human_request(
                    cast(str, request["request_ref"]),
                    response_refs=declined,
                    decision="declined",
                    reason_code="target_authorization_declined",
                    accepted_evidence_refs=(),
                    idempotency_key=_operation_key(
                        "target-human-decline", target.target_ref
                    ),
                )
                self._transient_error = "target_high_risk_authorization_declined"
                return None, True
            provided = tuple(
                cast(str, response["response_ref"])
                for response in responses
                if response.get("decision") == "provided"
                and isinstance(response.get("response_ref"), str)
            )
            authorization_ref = self._target_authorization_receipt(
                graph.quest_ref, requirement
            )
            if provided and authorization_ref is not None:
                self._agent_runtime.evaluate_human_request(
                    cast(str, request["request_ref"]),
                    response_refs=provided,
                    decision="satisfied",
                    reason_code="target_authorization_verified",
                    accepted_evidence_refs=(authorization_ref,),
                    idempotency_key=_operation_key(
                        "target-human-satisfy", target.target_ref
                    ),
                )
                return None, True
            self._transient_error = "target_high_risk_authorization_required"
            return None, False
        if request.get("status") == "declined":
            self._transient_error = "target_high_risk_authorization_declined"
            return None, False
        if request.get("status") != "satisfied":
            self._transient_error = "target_high_risk_authorization_required"
            return None, False
        current_waiter = self._target_human_waiter(
            request,
            target=target,
            assertion=assertion,
            run=run,
        )
        authorization_ref = self._target_authorization_receipt(
            graph.quest_ref, requirement
        )
        if current_waiter is None or authorization_ref is None:
            self._transient_error = "target_high_risk_authorization_stale"
            return None, False
        if current_waiter.get("status") == "blocked":
            validation = self._agent_runtime.validate_human_request_waiter(
                cast(str, request["request_ref"]),
                waiter_ref=cast(str, current_waiter["waiter_ref"]),
                generation=cast(int, current_waiter["generation"]),
                target_assertion=cast(
                    dict[str, object], current_waiter["target_assertion"]
                ),
                other_blockers=(),
                authorization_receipt_ref=authorization_ref,
                idempotency_key=_operation_key(
                    "target-human-resume", target.target_ref
                ),
            )
            if validation.get("status") == "released":
                return None, True
            reason = validation.get("reason")
            self._transient_error = (
                cast(str, reason["code"])
                if isinstance(reason, dict) and isinstance(reason.get("code"), str)
                else "target_high_risk_authorization_stale"
            )
            return None, False
        if current_waiter.get("status") not in {"released", "consumed"}:
            self._transient_error = "target_high_risk_authorization_stale"
            return None, False
        self._transient_error = None
        return (
            _TargetAuthorization(
                request_ref=cast(str, request["request_ref"]),
                waiter_ref=cast(str, current_waiter["waiter_ref"]),
                generation=cast(int, current_waiter["generation"]),
                authorization_receipt_ref=authorization_ref,
            ),
            False,
        )

    def _target_human_request(
        self,
        quest_ref: str,
        assertion: dict[str, object],
        *,
        requirement: dict[str, object],
        run: BundleStageRun | None,
    ) -> dict[str, object] | None:
        assertions = [assertion]
        if run is not None:
            assertions.append(_root_target_authorization_assertion(run, assertion))
        matches = [
            request
            for request in self._agent_runtime.query_human_requests(
                quest_ref=quest_ref, include_history=True
            )
            if request.get("kind") == "capability_authorization"
            and request.get("issuer") == "agent_runtime"
            and request.get("target_assertion") in assertions
            and request.get("required_authorization") == requirement
            and request.get("obligation") == _TARGET_AUTHORIZATION_OBLIGATION
            and request.get("business_purpose") == _TARGET_AUTHORIZATION_PURPOSE
            and request.get("acceptance_conditions")
            == list(_TARGET_AUTHORIZATION_ACCEPTANCE_CONDITIONS)
            and request.get("current") is True
        ]
        if len(matches) > 1:
            raise OwnerConflict("target_human_request_conflict")
        return matches[0] if matches else None

    @staticmethod
    def _target_human_waiter(
        request: dict[str, object],
        *,
        target: AcceptedTarget,
        assertion: dict[str, object],
        run: BundleStageRun,
    ) -> dict[str, object] | None:
        request_assertion = request.get("target_assertion")
        if request_assertion == assertion:
            waiter_ref = target.target_ref
            generation = 1
        elif request_assertion == _root_target_authorization_assertion(run, assertion):
            waiter_ref = f"root_run:{run.run_ref}"
            generation = run.attempt_generation
        else:
            return None
        waiters_value = request.get("direct_waiters")
        if not isinstance(waiters_value, list) or any(
            not isinstance(item, dict) for item in waiters_value
        ):
            return None
        waiters = cast(list[dict[str, object]], waiters_value)
        matching = [
            item
            for item in waiters
            if item.get("waiter_ref") == waiter_ref
            and item.get("generation") == generation
            and item.get("target_assertion") == request_assertion
            and item.get("wait_scope") == "local"
            and item.get("other_blockers") == []
        ]
        if len(matching) > 1:
            raise OwnerConflict("target_human_waiter_conflict")
        return matching[0] if matching else None

    def _target_authorization_receipt(
        self, quest_ref: str, requirement: dict[str, object]
    ) -> str | None:
        if self._human_collaboration is None:
            return None
        projection = self._human_collaboration.query_collaboration_projection(
            (f"quest:{quest_ref}",)
        )
        matches = [
            authorization
            for authorization in projection.get("authorizations", [])
            if authorization.get("authorization_kind") == "capability"
            and authorization.get("status") == "granted"
            and authorization.get("is_current") is True
            and authorization.get("requirement") == requirement
            and isinstance(authorization.get("receipt_ref"), str)
        ]
        if len(matches) > 1:
            raise OwnerConflict("target_authorization_conflict")
        return None if not matches else cast(str, matches[0]["receipt_ref"])

    def _dispatch_state(
        self,
        graph: AcceptedTargetGraph,
        commits: tuple[TargetCommit, ...],
        *,
        run: BundleStageRun,
    ) -> dict[str, object]:
        committed = {commit.target_ref: commit for commit in commits}
        blocked: list[dict[str, object]] = []
        running: list[dict[str, object]] = []
        for target in graph.targets:
            if target.target_ref in committed:
                continue
            frontier = self._agent_runtime.query_target_frontier_entry(
                target.target_ref
            )
            notice = self._agent_runtime.query_target_work_notice(
                target.target_ref
            )
            launch = self._agent_runtime.query_admitted_target_launch(
                target.target_ref
            )
            if notice is not None and notice.kind in {
                "coordination_required",
                "semantic_change_required",
            }:
                blocked.append(
                    {
                        "target_ref": target.target_ref,
                        "reason": {
                            "code": "target_" + notice.kind,
                            "compact_reason": notice.compact_reason,
                            "pending_obligation_refs": list(
                                notice.pending_obligation_refs
                            ),
                        },
                    }
                )
                continue
            if frontier is not None or launch is not None:
                running.append(
                    {
                        "target_ref": target.target_ref,
                        "target_run_ref": (
                            frontier.current_handle.target_run_ref
                            if frontier is not None
                            else launch.target_run_ref
                        ),
                    }
                )
                continue
            if target.spec.get("risk_class") == "high":
                projection = (
                    self._research_graph.query_target_candidate_projection(
                        target_ref=target.target_ref
                    )
                )
                request = (
                    None
                    if projection is None
                    else self._target_human_request(
                        graph.quest_ref,
                        _target_authorization_assertion(
                            graph,
                            target,
                            target_spec_hash=projection.projection_digest,
                        ),
                        requirement=_target_authorization_requirement(
                            graph,
                            target,
                            target_spec_hash=projection.projection_digest,
                        ),
                        run=run,
                    )
                )
                if request is None or request.get("status") != "satisfied":
                    blocked.append(
                        {
                            "target_ref": target.target_ref,
                            "reason": {
                                "code": (
                                    "target_high_risk_authorization_declined"
                                    if request is not None
                                    and request.get("status") == "declined"
                                    else "target_high_risk_authorization_required"
                                )
                            },
                        }
                    )
        return {
            "schema_ref": "meta-research/bundle-dispatch-state/v1",
            "target_commit_refs": [commit.commit_ref for commit in commits],
            "running_targets": running,
            "blocked_targets": blocked,
        }

    def _dispatch_target(
        self,
        *,
        graph: AcceptedTargetGraph,
        target: AcceptedTarget,
        run: BundleStageRun,
        authorization: _TargetAuthorization | None,
        human_request: dict[str, object] | None,
    ) -> dict[str, object]:
        value = self._target_projection(target)
        risk_class = target.spec.get("risk_class")
        if risk_class != "high":
            return value
        projection = self._research_graph.query_target_candidate_projection(
            target_ref=target.target_ref
        )
        if projection is None or projection.source_spec_hash != target.spec_hash:
            raise OwnerConflict("target_candidate_projection_source_invalid")
        assertion = _target_authorization_assertion(
            graph,
            target,
            target_spec_hash=projection.projection_digest,
        )
        requirement = _target_authorization_requirement(
            graph,
            target,
            target_spec_hash=projection.projection_digest,
        )
        value["dispatch_allowed"] = authorization is not None
        value["human_request_ref"] = (
            None if human_request is None else human_request.get("request_ref")
        )
        value["human_request_status"] = (
            "not_open" if human_request is None else human_request.get("status")
        )
        if authorization is None:
            value["human_request_command"] = _target_authorization_command(
                target=target,
                run=run,
                assertion=assertion,
                requirement=requirement,
            )
        return value

    @staticmethod
    def _target_projection(target: AcceptedTarget) -> dict[str, object]:
        candidate = target.spec.get("candidate")
        risk_class = target.spec.get("risk_class")
        if (
            not isinstance(candidate, dict)
            or risk_class not in {"normal", "high"}
        ):
            raise OwnerConflict("target_dispatch_formal_spec_invalid")
        return {
            "target_ref": target.target_ref,
            "target_key": target.target_key,
            "spec_hash": target.spec_hash,
            "spec": target.spec,
            "candidate": candidate,
            "risk_class": risk_class,
            "dependency_refs": list(target.dependency_refs),
            "receipt": target.receipt.as_public_dict(),
        }

    @staticmethod
    def _target_commit_projection(commit: TargetCommit) -> dict[str, object]:
        return {
            "commit_ref": commit.commit_ref,
            "target_ref": commit.target_ref,
            "target_run_ref": commit.target_run_ref,
            "evaluation_attempt_ref": commit.evaluation_attempt_ref,
            "target_spec_hash": commit.target_spec_hash,
            "closure_hash": commit.closure_hash,
            "result_disposition": commit.result_disposition,
            "protocol": commit.closure["protocol"],
            "metric_result": target_commit_metric_result(commit),
            "receipt": commit.receipt.as_public_dict(),
        }

    def _publish_target_commit_evidence(
        self,
        *,
        quest_ref: str,
        commit: TargetCommit,
    ) -> None:
        document = target_commit_evidence_document(commit)
        intake = self._research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name=f"{commit.commit_ref}-evidence.json",
                media_type=TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
                content=canonical_json(document).encode("utf-8"),
                provenance=target_commit_evidence_provenance(commit),
            ),
            idempotency_key=_operation_key("target-evidence-intake", commit.commit_ref),
        )
        if intake.status != "accepted" or intake.asset is None:
            raise OwnerConflict("target_commit_evidence_not_accepted")
        self._research_graph.accept_asset_role(
            binding=intake.asset.as_binding(),
            role="evidence",
            quest_ref=quest_ref,
            idempotency_key=_operation_key("target-evidence-role", commit.commit_ref),
        )

    def query_current(self) -> dict[str, object]:
        current = self._discover_current_cycle()
        if current is None:
            return _not_eligible_projection(
                cycle_ref=None,
                question_ref=None,
                reason_code="accepted_cycle_unavailable",
                next_stage=None,
            )
        eligible, reason_code, next_stage = self._qualify(current)
        if eligible is None:
            return _not_eligible_projection(
                cycle_ref=current.cycle_ref,
                question_ref=current.question.question_ref,
                reason_code=reason_code or "accepted_formal_plan_unavailable",
                next_stage=next_stage,
            )
        request = self._advancement_engine.query_bundle_stage_request(current.cycle_ref)
        if request is not None:
            self._assert_request(request, eligible)
        run = (
            None
            if request is None
            else self._agent_runtime.query_bundle_stage_run(request.request_ref)
        )
        graph = (
            None
            if request is None
            else self._research_graph.query_target_graph(request.request_ref)
        )
        stage_commit = (
            None
            if request is None
            else self._advancement_engine.query_bundle_stage_commit(request.request_ref)
        )
        exhaustion_operation = (
            None
            if request is None
            else self._advancement_engine.query_bundle_exhaustion_for_request(
                request.request_ref
            )
        )
        exhaustion_evidence = (
            None
            if run is None
            else self._agent_runtime.query_bundle_exhaustion_evidence_for_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
            )
        )
        bundle_report = (
            None
            if run is None
            else self._agent_runtime.query_bundle_run_report(run.run_ref)
        )
        report_disposition = (
            None
            if bundle_report is None
            else self._advancement_engine.query_bundle_report_disposition(
                bundle_report.report_ref
            )
        )
        replan_retirement = (
            None
            if report_disposition is None
            or report_disposition.disposition != "replan_required"
            else self._agent_runtime.query_bundle_replan_run_retirement(
                report_disposition.disposition_ref
            )
        )
        replan_activation = (
            None
            if report_disposition is None
            or report_disposition.disposition != "replan_required"
            else self._advancement_engine.query_bundle_replan_activation(
                report_disposition.disposition_ref
            )
        )
        graph_projection = {
            "status": "not_attempted",
            "targets": [],
            "frontier": [],
        }
        graph_rejection = None
        if run is not None and graph is None:
            lineage_execution = run.execution or run.predecessor_execution
            if lineage_execution is not None:
                graph_rejection = (
                    self._research_graph.query_target_graph_rejection(
                        lineage_execution.submission_ref
                    )
                )
                if graph_rejection is not None and (
                    graph_rejection.request_ref != request.request_ref
                    or graph_rejection.run_ref != run.run_ref
                    or graph_rejection.attempt_ref
                    != lineage_execution.attempt_ref
                    or graph_rejection.fence_ref != lineage_execution.fence_ref
                    or graph_rejection.target_plan_hash
                    != lineage_execution.material_outcome_hash
                    or graph_rejection.execution_payload_hash
                    != lineage_execution.payload_hash
                    or graph_rejection.execution_receipt
                    != lineage_execution.receipt
                    or (
                        run.predecessor_execution is lineage_execution
                        and run.rejection_receipt != graph_rejection.receipt
                    )
                ):
                    raise OwnerConflict("target_graph_rejection_binding_invalid")
        if graph_rejection is not None:
            graph_projection = _public_target_graph_rejection(graph_rejection)
        target_commit_projection: list[dict[str, object]] = []
        baseline_pool: list[dict[str, object]] = []
        if graph is not None:
            frontier = self._research_graph.query_target_frontier(graph.graph_ref)
            frontier_refs = {target.target_ref for target in frontier}
            commits = self._research_graph.query_target_commits(graph.graph_ref)
            commit_by_target = {commit.target_ref: commit for commit in commits}
            frontier_by_target = {
                target.target_ref: self._agent_runtime.query_target_frontier_entry(
                    target.target_ref
                )
                for target in graph.targets
            }
            notice_by_target = {
                target.target_ref: self._agent_runtime.query_target_work_notice(
                    target.target_ref
                )
                for target in graph.targets
            }
            launch_by_target = {
                target.target_ref: self._agent_runtime.query_admitted_target_launch(
                    target.target_ref
                )
                for target in graph.targets
                if target.target_ref not in commit_by_target
            }
            target_rows: list[dict[str, object]] = []
            for target in graph.targets:
                target_frontier = frontier_by_target[target.target_ref]
                target_notice = notice_by_target[target.target_ref]
                target_launch = launch_by_target.get(target.target_ref)
                blocker = None
                if target.target_ref in commit_by_target:
                    status = "committed"
                elif target_notice is not None and target_notice.kind in {
                    "coordination_required",
                    "semantic_change_required",
                }:
                    status = "blocked"
                    blocker = {
                        "code": "target_" + target_notice.kind,
                        "compact_reason": target_notice.compact_reason,
                        "pending_obligation_refs": list(
                            target_notice.pending_obligation_refs
                        ),
                    }
                elif target_frontier is not None or target_launch is not None:
                    status = "running"
                elif (
                    target.spec.get("risk_class") == "high"
                    and target.target_ref in frontier_refs
                ):
                    projection = (
                        self._research_graph.query_target_candidate_projection(
                            target_ref=target.target_ref
                        )
                    )
                    human_request = (
                        None
                        if projection is None
                        else self._target_human_request(
                            graph.quest_ref,
                            _target_authorization_assertion(
                                graph,
                                target,
                                target_spec_hash=projection.projection_digest,
                            ),
                            requirement=_target_authorization_requirement(
                                graph,
                                target,
                                target_spec_hash=projection.projection_digest,
                            ),
                            run=run,
                        )
                    )
                    waiter = (
                        None
                        if human_request is None or run is None
                        else self._target_human_waiter(
                            human_request,
                            target=target,
                            assertion=_target_authorization_assertion(
                                graph,
                                target,
                                target_spec_hash=projection.projection_digest,
                            ),
                            run=run,
                        )
                    )
                    if (
                        human_request is not None
                        and human_request.get("status") == "satisfied"
                        and waiter is not None
                        and waiter.get("status") in {"released", "consumed"}
                        and target.target_ref in frontier_refs
                    ):
                        status = "ready"
                    else:
                        status = "blocked"
                        blocker = {
                            "code": (
                                "target_high_risk_authorization_declined"
                                if human_request is not None
                                and human_request.get("status") == "declined"
                                else "target_high_risk_authorization_required"
                            )
                        }
                elif target.target_ref in frontier_refs:
                    status = "ready"
                else:
                    status = "blocked_by_dependency"
                target_rows.append(
                    {
                        "target_ref": target.target_ref,
                        "target_key": target.target_key,
                        "spec_hash": target.spec_hash,
                        "dependency_refs": list(target.dependency_refs),
                        "target_run_ref": (
                            target_frontier.current_handle.target_run_ref
                            if target_frontier is not None
                            else (
                                None
                                if target_launch is None
                                else target_launch.target_run_ref
                            )
                        ),
                        "status": status,
                        "blocker": blocker,
                        "receipt": target.receipt.as_public_dict(),
                    }
                )
            graph_projection = {
                "status": "accepted",
                "graph_ref": graph.graph_ref,
                "formal_plan_ref": graph.formal_plan_ref,
                "target_plan_hash": graph.target_plan_hash,
                "head_generation": graph.head_generation,
                "strategy_complete": graph.strategy_complete,
                "target_set_hash": graph.target_set_hash,
                "coverage_hash": graph.coverage_hash,
                "root_receipt": graph.receipt.as_public_dict(),
                "head_receipt": graph.head_receipt.as_public_dict(),
                "receipt": graph.head_receipt.as_public_dict(),
                "targets": target_rows,
                "frontier": [target.target_ref for target in frontier],
            }
            target_commit_projection = [
                {
                    "status": "realized",
                    "commit_ref": commit.commit_ref,
                    "target_ref": commit.target_ref,
                    "target_run_ref": commit.target_run_ref,
                    "evaluation_attempt_ref": commit.evaluation_attempt_ref,
                    "target_spec_hash": commit.target_spec_hash,
                    "closure_hash": commit.closure_hash,
                    "closure": commit.closure,
                    "result_disposition": commit.result_disposition,
                    "receipt": commit.receipt.as_public_dict(),
                }
                for commit in commits
            ]
            _evidence_revision, evidence_catalog = (
                self._research_graph.query_plan_evidence_catalog(
                    quest_ref=graph.quest_ref
                )
            )
            evidence_by_commit = {
                cast(str, evidence["target_commit_root_ref"]): evidence
                for evidence in evidence_catalog
            }
            baseline_pool = [
                {
                    "target_commit_ref": commit.commit_ref,
                    "target_ref": commit.target_ref,
                    "result_disposition": commit.result_disposition,
                    "metric_result": target_commit_metric_result(commit),
                    "receipt": commit.receipt.as_public_dict(),
                    "evidence_ref": evidence["evidence_ref"],
                    "asset_version_ref": evidence["asset_version_ref"],
                    "content_hash": evidence["content_hash"],
                    "role_ref": evidence["role_ref"],
                    "role_receipt": evidence["role_receipt"],
                }
                for commit in commits
                if (evidence := evidence_by_commit.get(commit.commit_ref)) is not None
            ]
        report_projection = (
            None
            if bundle_report is None
            else {
                "status": "accepted",
                "report_ref": bundle_report.report_ref,
                "report_hash": bundle_report.report_hash,
                "disposition": bundle_report.report.disposition,
                "formal_plan_ref": bundle_report.formal_plan_ref,
                "plan_document_hash": bundle_report.plan_document_hash,
                "formal_plan_content_receipt": (
                    bundle_report.formal_plan_content_receipt.as_public_dict()
                ),
                "formal_plan_projection_digest": (
                    bundle_report.formal_plan_projection_digest
                ),
                "formal_plan_projection_receipt": (
                    bundle_report.formal_plan_projection_receipt.as_public_dict()
                ),
                "target_graph_ref": bundle_report.target_graph_ref,
                "target_graph_generation": (
                    bundle_report.target_graph_generation
                ),
                "report": projection_plain_value(bundle_report.report),
                "receipt": bundle_report.receipt.as_public_dict(),
            }
        )
        exhaustion_projection = (
            None
            if exhaustion_operation is None
            else {
                "kind": "BundleExhaustion",
                "status": exhaustion_operation.status,
                "operation_ref": exhaustion_operation.operation_ref,
                "proposal_identity": exhaustion_operation.proposal_identity,
                "proposal_hash": exhaustion_operation.proposal_hash,
                "proposal_ref": exhaustion_operation.accepted_proposal_ref,
                "decision_receipt": (
                    exhaustion_operation.decision_receipt.as_public_dict()
                ),
                "evidence": (
                    None
                    if exhaustion_evidence is None
                    else exhaustion_evidence.as_dict()
                ),
                "basis_kind": (
                    None if stage_commit is None else stage_commit.basis_kind
                ),
                "basis_ref": (
                    None if stage_commit is None else stage_commit.basis_ref
                ),
                "basis_receipt": (
                    None
                    if stage_commit is None or stage_commit.basis_receipt is None
                    else stage_commit.basis_receipt.as_public_dict()
                ),
            }
        )
        disposition: dict[str, object] = {"status": "not_attempted"}
        if bundle_report is not None:
            if stage_commit is not None:
                report_status = "completed"
            elif report_disposition is None:
                report_status = "report_accepted"
            elif replan_activation is not None:
                report_status = "replan_activated"
            elif replan_retirement is not None:
                report_status = "pending_replan_activation"
            else:
                report_status = report_disposition.status
            disposition = {
                "status": report_status,
                "report_ref": bundle_report.report_ref,
                "report_hash": bundle_report.report_hash,
                "report_disposition": bundle_report.report.disposition,
                "disposition_ref": (
                    None
                    if report_disposition is None
                    else report_disposition.disposition_ref
                ),
                "disposition_receipt": (
                    None
                    if report_disposition is None
                    else report_disposition.receipt.as_public_dict()
                ),
                "retirement_ref": (
                    None
                    if replan_retirement is None
                    else replan_retirement.retirement_ref
                ),
                "activation_ref": (
                    None
                    if replan_activation is None
                    else replan_activation.activation_ref
                ),
            }
        elif exhaustion_operation is not None:
            disposition = {
                "status": (
                    "completed"
                    if stage_commit is not None
                    else exhaustion_operation.status
                ),
                "report_disposition": (
                    "exhausted"
                    if stage_commit is not None
                    else None
                ),
                "operation_ref": exhaustion_operation.operation_ref,
                "proposal_ref": exhaustion_operation.accepted_proposal_ref,
                "decision_receipt": (
                    exhaustion_operation.decision_receipt.as_public_dict()
                ),
                "basis_receipt": (
                    None
                    if stage_commit is None or stage_commit.basis_receipt is None
                    else stage_commit.basis_receipt.as_public_dict()
                ),
            }
        elif graph is not None:
            blocked_targets = [
                target
                for target in cast(list[dict[str, object]], graph_projection["targets"])
                if target["status"] == "blocked"
            ]
            disposition = {
                "status": (
                    "completed"
                    if stage_commit is not None
                    else "partial_blocked"
                    if blocked_targets
                    else "running"
                ),
                "target_count": len(graph.targets),
                "target_commit_count": len(target_commit_projection),
                **(
                    {
                        "blocked_targets": [
                            {
                                "target_ref": target["target_ref"],
                                "reason": target["blocker"],
                            }
                            for target in blocked_targets
                        ]
                    }
                    if blocked_targets
                    else {}
                ),
            }
        elif stage_commit is not None and stage_commit.disposition == "skipped":
            disposition = {
                "status": "skipped",
                "reason": {"code": "no_bundle_run_required"},
            }
        return {
            "eligibility": {
                "status": "eligible",
                "cycle_ref": current.cycle_ref,
                "question_ref": current.question.question_ref,
                "formal_plan_ref": eligible.binding.formal_plan_ref,
                "reason": None,
                "next_stage": "Bundle",
            },
            "stage_run_request": None if request is None else _public_request(request),
            "run": None if run is None else _public_run(run),
            "target_graph": graph_projection,
            "target_commits": target_commit_projection,
            "baseline_pool": baseline_pool,
            "bundle_report": report_projection,
            **(
                {"bundle_exhaustion": exhaustion_projection}
                if exhaustion_projection is not None
                else {}
            ),
            "disposition": disposition,
            "stage_commit": (
                None if stage_commit is None else _public_commit(stage_commit)
            ),
        }

    def _qualify(
        self, current: _CurrentCycle
    ) -> tuple[_EligibleBundle | None, str | None, str | None]:
        successor = self._advancement_engine.query_reasoning_successor_context(
            current.cycle_ref
        )
        if successor is not None and successor.get("entry_stage") == "bundle":
            raw_idea_binding = successor.get("accepted_idea_set_binding")
            raw_binding = successor.get("accepted_formal_plan_binding")
            if not isinstance(raw_idea_binding, dict) or not isinstance(
                raw_binding, dict
            ):
                raise OwnerConflict("accepted_formal_plan_lineage_invalid")
            idea_binding = _accepted_idea_set_binding_from_public(raw_idea_binding)
            binding = _accepted_formal_plan_binding_from_public(raw_binding)
            answer_contract = binding.plan_document.get("answer_contract")
            if (
                not isinstance(answer_contract, dict)
                or answer_contract.get("source_question_ref")
                != current.question.question_ref
                or answer_contract.get("source_idea_set_ref")
                != idea_binding.outcome_ref
            ):
                raise OwnerConflict("accepted_formal_plan_lineage_invalid")
            self._research_graph.verify_accepted_idea_set_binding(idea_binding)
            self._research_graph.verify_accepted_formal_plan_binding(binding)
            return _EligibleBundle(current, binding, idea_binding), None, "Bundle"

        basis_cycle_ref = current.cycle_ref
        request = self._advancement_engine.query_plan_stage_request(basis_cycle_ref)
        if request is None:
            return None, "accepted_formal_plan_unavailable", "Plan"
        commit = self._advancement_engine.query_plan_stage_commit(request.request_ref)
        if commit is None:
            return None, "accepted_formal_plan_unavailable", "Plan"
        run = self._agent_runtime.query_plan_stage_run(request.request_ref)
        if (
            request.stage != "plan"
            or commit.stage != "plan"
            or commit.cycle_ref != basis_cycle_ref
            or commit.request_ref != request.request_ref
            or commit.outcome_kind != "formal_plan"
            or commit.disposition != "completed"
            or run is None
            or run.execution is None
            or run.completion is None
            or run.completion.outcome_ref != commit.outcome_ref
            or run.completion.receipt != commit.run_completion_receipt
        ):
            raise OwnerConflict("accepted_formal_plan_lineage_invalid")
        content = self._research_memory.query_plan_document(
            run.execution.submission_ref
        )
        decision = self._research_graph.query_formal_plan_decision(
            run.execution.submission_ref
        )
        if (
            content is None
            or decision is None
            or decision.decision != "accepted"
            or decision.formal_plan_ref != commit.outcome_ref
            or decision.receipt != commit.outcome_receipt
            or decision.plan_document_hash != content.plan_document_hash
            or content.execution_receipt != run.execution.receipt
        ):
            raise OwnerConflict("accepted_formal_plan_lineage_invalid")
        eligible = _EligibleBundle(
            current,
            AcceptedFormalPlanBinding(
                formal_plan_ref=cast(str, decision.formal_plan_ref),
                content_ref=content.content_ref,
                plan_document_hash=content.plan_document_hash,
                answer_contract_hash=content.answer_contract_hash,
                content_receipt=content.receipt,
                formal_plan_receipt=decision.receipt,
                stage_commit_ref=commit.commit_ref,
                stage_commit_receipt=commit.receipt,
                plan_document=content.plan_document,
            ),
        )
        return eligible, None, "Bundle"

    def _assert_request(
        self, request: StageRunRequest, eligible: _EligibleBundle
    ) -> None:
        if (
            request.stage != "bundle"
            or request.cycle_ref != eligible.current.cycle_ref
            or request.accepted_question != eligible.current.question.as_binding()
            or request.accepted_idea_set != eligible.accepted_idea_set
            or request.accepted_formal_plan != eligible.accepted_formal_plan()
        ):
            raise OwnerConflict("bundle_stage_request_lineage_invalid")

    def _advance_exhaustion(
        self,
        request: StageRunRequest,
        run: BundleStageRun,
    ) -> bool:
        """Advance the fixed exhaustion path by one durable Owner boundary."""

        accepted_formal_plan = request.accepted_formal_plan
        if accepted_formal_plan is None:
            raise OwnerConflict("bundle_formal_plan_binding_invalid")
        evidence = self._agent_runtime.query_bundle_exhaustion_evidence_for_run(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
        )
        if evidence is None:
            raise OwnerConflict("bundle_exhaustion_evidence_missing")
        if (
            evidence.evidence.stage_run_request_ref != request.request_ref
            or evidence.evidence.run_ref != run.run_ref
            or evidence.evidence.attempt_ref != run.attempt_ref
            or evidence.evidence.execution_fence_ref != run.fence_ref
            or evidence.evidence.formal_plan_ref
            != accepted_formal_plan.formal_plan_ref
            or evidence.evidence.formal_plan_content_hash
            != accepted_formal_plan.plan_document_hash
        ):
            raise OwnerConflict("bundle_exhaustion_evidence_binding_invalid")

        formal_content = (
            self._research_graph.query_formal_plan_content_acceptance(
                accepted_formal_plan.formal_plan_ref
            )
        )
        if formal_content is None:
            self._research_graph.accept_formal_plan_content(
                formal_plan_ref=accepted_formal_plan.formal_plan_ref,
                idempotency_key=_operation_key(
                    "bundle-exhaustion-formal-plan-content",
                    request.request_ref,
                    accepted_formal_plan.formal_plan_ref,
                ),
            )
            return True
        if (
            formal_content.plan_document_hash
            != accepted_formal_plan.plan_document_hash
            or formal_content.formal_plan_ref
            != accepted_formal_plan.formal_plan_ref
        ):
            raise OwnerConflict(
                "bundle_exhaustion_formal_plan_content_acceptance_invalid"
            )

        operation = self._advancement_engine.query_bundle_exhaustion_for_request(
            request.request_ref
        )
        proposal = BundleExhaustionProposal(
            proposal_identity=(
                "bundle-exhaustion-proposal:"
                + canonical_hash(
                    {
                        "request_ref": request.request_ref,
                        "run_ref": run.run_ref,
                        "attempt_ref": run.attempt_ref,
                        "fence_ref": run.fence_ref,
                        "evidence_ref": evidence.evidence_ref,
                        "evidence_hash": evidence.evidence.evidence_hash,
                    }
                )[:48]
            ),
            stage_run_request_ref=request.request_ref,
            stage_run_request_receipt_ref=request.receipt.receipt_ref,
            stage_run_request_receipt_hash=request.receipt.payload_hash,
            cycle_ref=request.cycle_ref,
            epoch=request.epoch,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,
            execution_fence_ref=run.fence_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            formal_plan_ref=formal_content.formal_plan_ref,
            formal_plan_content_hash=formal_content.plan_document_hash,
            formal_plan_content_receipt=formal_content.receipt,
            evidence_ref=evidence.evidence_ref,
            evidence_hash=evidence.evidence.evidence_hash,
            evidence_receipt=evidence.receipt,
        )
        if operation is None:
            self._advancement_engine.submit_bundle_exhaustion_proposal(
                proposal=proposal,
                idempotency_key=_operation_key(
                    "bundle-exhaustion-submit",
                    request.request_ref,
                    proposal.proposal_identity,
                ),
            )
            return True
        if (
            operation.proposal_identity != proposal.proposal_identity
            or operation.proposal_hash != proposal.proposal_hash
        ):
            raise OwnerConflict("bundle_exhaustion_proposal_conflict")
        if operation.status in {"outcome_unknown", "technical_blocker"}:
            reconciled = (
                self._advancement_engine.reconcile_bundle_exhaustion_proposal(
                    proposal_identity=proposal.proposal_identity,
                    expected_proposal_hash=proposal.proposal_hash,
                )
            )
            if reconciled is None:
                self._transient_error = "bundle_exhaustion_outcome_unknown"
                return False
            changed = (
                reconciled.status != operation.status
                or reconciled.decision_receipt != operation.decision_receipt
            )
            self._transient_error = (
                None
                if reconciled.status == "accepted"
                else f"bundle_exhaustion_{reconciled.status}"
            )
            return changed
        if operation.status != "accepted":
            self._transient_error = f"bundle_exhaustion_{operation.status}"
            return False
        if operation.accepted_proposal_ref is None:
            raise OwnerConflict("bundle_exhaustion_acceptance_invalid")
        if run.completion is None:
            self._agent_runtime.complete_bundle_exhaustion_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                proposal_ref=operation.accepted_proposal_ref,
                decision_receipt=operation.decision_receipt,
                idempotency_key=_operation_key(
                    "bundle-exhaustion-complete",
                    run.run_ref,
                    operation.accepted_proposal_ref,
                ),
            )
            self._transient_error = None
            return True
        if (
            self._advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            is None
        ):
            self._advancement_engine.commit_stage_disposition(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                disposition="exhausted",
                basis_kind=BUNDLE_EXHAUSTION_BASIS_KIND,
                basis_ref=operation.accepted_proposal_ref,
                basis_receipt=operation.decision_receipt,
                run_completion_receipt=run.completion.receipt,
                idempotency_key=_operation_key(
                    "bundle-exhaustion-commit",
                    request.request_ref,
                    operation.accepted_proposal_ref,
                ),
            )
            self._finish_bundle_jobs(run)
            self._transient_error = None
            return True
        self._finish_bundle_jobs(run)
        self._transient_error = None
        return False

    def _execute_target_plan(
        self, request: StageRunRequest, run: BundleStageRun
    ) -> bool:
        inbox_checkpoint = self._drain_bundle_inbox(run)
        accepted_formal_plan = request.accepted_formal_plan
        if accepted_formal_plan is None or not isinstance(
            run.runtime_binding, BundleRuntimeBinding
        ):
            raise OwnerConflict("bundle_runtime_binding_invalid")
        runtime_binding = self._current_runtime_binding()
        if runtime_binding is None:
            return False
        if runtime_binding != run.runtime_binding:
            self._transient_error = "bundle_runtime_binding_drift"
            return False
        invocation = (
            run.primary_invocation
            if run.primary_draft is None
            else run.review_invocation
        )
        job_ref = invocation.operation_ref
        unit_ref = invocation.invocation_ref
        predecessor_rejections = (
            self._agent_runtime.query_bundle_exhaustion_rejected_submissions(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        )
        (
            predecessor_candidate_ref,
            owner_rejection_receipt_ref,
            owner_rejection_kind,
            owner_feedback,
        ) = self._owner_rejection_context(request, run)
        skill_request = BundleSkillRequest(
            stage_request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            formal_plan_ref=accepted_formal_plan.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            plan_document=accepted_formal_plan.plan_document,
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            inbox_checkpoint=inbox_checkpoint.as_public_dict(),
            native_session_ref=run.native_session_ref,
            job_ref=job_ref,
            predecessor_rejections=tuple(
                item.as_dict() for item in predecessor_rejections
            ),
            predecessor_candidate_ref=predecessor_candidate_ref,
            owner_rejection_receipt_ref=owner_rejection_receipt_ref,
            owner_rejection_kind=owner_rejection_kind,
            owner_feedback=owner_feedback,
        )
        if run.primary_draft is None:
            try:
                self._agent_runtime.begin_provider_unit(
                    unit_ref=unit_ref,
                    operation_ref=job_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    unit_kind="bundle_primary",
                )
            except OwnerConflict as error:
                self._transient_error = error.code
                return False
            provider_safe = True
            draft = None
            try:
                try:
                    draft = self._provider.generate_draft(skill_request)
                    draft_hash = validate_bundle_skill_draft(skill_request, draft)
                except BundleSkillUnavailable as error:
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
                        return True
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
                    return False
                except RecoverableBundleSkillCandidateError as error:
                    if draft is None:
                        self._transient_error = str(error)
                        return False
                    failure_code = "bundle_primary_result_contract_invalid"
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
                    return True
                except BundleSkillContractError as error:
                    self._transient_error = str(error)
                    return False
                checkpoint = self._agent_runtime.record_bundle_primary_draft(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    native_session_ref=draft.primary_session_ref,
                    runtime_binding=run.runtime_binding,
                    draft=draft.draft,
                    adapter_kind=draft.adapter_kind,
                    idempotency_key=_operation_key(
                        "bundle-primary", run.run_ref, run.attempt_ref, draft_hash
                    ),
                )
                if checkpoint.draft_hash != draft_hash:
                    raise OwnerConflict("bundle_primary_draft_hash_mismatch")
                self._transient_error = None
                return True
            finally:
                if provider_safe:
                    self._agent_runtime.acknowledge_provider_safe_point(
                        unit_ref=unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                    )
        draft = BundleSkillDraft(
            draft=run.primary_draft.draft,
            primary_session_ref=run.primary_draft.native_session_ref,
            adapter_kind=run.primary_draft.adapter_kind,
            output_kind=cast(str, _bundle_primary_output_kind(run)),
        )
        # Exhaustion review is the only review-phase provider effect. TargetPlan
        # review is an adapter-local projection of the validated primary draft.
        if draft.output_kind == "exhaustion_assessment":
            try:
                self._agent_runtime.begin_provider_unit(
                    unit_ref=unit_ref,
                    operation_ref=job_ref,
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    unit_kind="bundle_review",
                )
            except OwnerConflict as error:
                self._transient_error = error.code
                return False
            provider_safe = True
            result = None
            try:
                try:
                    result = self._provider.review_draft(skill_request, draft)
                    if not isinstance(result, BundleExhaustionSkillResult):
                        raise BundleSkillContractError(
                            "bundle_skill_result_invalid"
                        )
                    validate_bundle_exhaustion_skill_result(
                        skill_request,
                        result,
                    )
                except BundleSkillUnavailable as error:
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
                            operation_name="review",
                            native_session_ref=error.rejected_native_session_ref,
                            candidate=error.rejected_candidate,
                            failure_code=error.code,
                            detail_code=error.rejected_detail_code,
                        )
                        provider_safe = False
                        self._transient_error = error.code
                        return True
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
                    return False
                except RecoverableBundleSkillCandidateError as error:
                    if result is None:
                        self._transient_error = str(error)
                        return False
                    failure_code = "bundle_review_result_contract_invalid"
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
                    return True
                except BundleSkillContractError as error:
                    self._transient_error = str(error)
                    return False
                self._accept_exhaustion_review(
                    request=request,
                    run=run,
                    result=result,
                )
                self._transient_error = None
                return True
            finally:
                if provider_safe:
                    self._agent_runtime.acknowledge_provider_safe_point(
                        unit_ref=unit_ref,
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                    )

        if draft.output_kind != "target_plan":
            self._transient_error = "bundle_skill_output_kind_invalid"
            return False
        result = None
        try:
            result = self._provider.review_draft(skill_request, draft)
            if not isinstance(result, BundleSkillResult):
                raise BundleSkillContractError(
                    "bundle_skill_result_invalid"
                )
            draft_hash, target_plan_hash, _review_hash = (
                validate_bundle_skill_result(skill_request, result)
            )
        except BundleSkillUnavailable as error:
            self._transient_error = error.code
            return False
        except RecoverableBundleSkillCandidateError as error:
            self._transient_error = (
                str(error)
                if result is None
                else "bundle_review_result_contract_invalid"
            )
            return False
        except BundleSkillContractError as error:
            self._transient_error = str(error)
            return False
        review = review_record(
            result,
            draft_hash=draft_hash,
            final_target_plan_hash=target_plan_hash,
        )
        submission_ref = (
            "bundle_submission_"
            + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "target_plan_hash": target_plan_hash,
                }
            )[:32]
        )
        self._agent_runtime.record_bundle_attempt_execution(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            submission_ref=submission_ref,
            native_session_ref=result.primary_session_ref,
            runtime_binding=run.runtime_binding,
            target_plan=result.final_target_plan,
            reviewed_draft=result.reviewed_draft,
            review=review,
            idempotency_key=_operation_key(
                "bundle-execute", run.run_ref, run.attempt_ref
            ),
        )
        self._transient_error = None
        return True

    def _owner_rejection_context(
        self,
        request: StageRunRequest,
        run: BundleStageRun,
    ) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        predecessor = run.predecessor_execution
        rejection_receipt = run.rejection_receipt
        completion_rejection = run.completion_rejection
        if (predecessor is None) != (rejection_receipt is None):
            raise OwnerConflict("rejection_lineage_incomplete")
        domain_feedback: tuple[str, ...] = ()
        if predecessor is not None and rejection_receipt is not None:
            rejection = self._research_graph.query_target_graph_rejection(
                predecessor.submission_ref
            )
            if (
                rejection is None
                or rejection.receipt != rejection_receipt
                or rejection.request_ref != request.request_ref
                or rejection.run_ref != run.run_ref
                or rejection.attempt_ref != predecessor.attempt_ref
                or rejection.fence_ref != predecessor.fence_ref
                or rejection.target_plan_hash
                != predecessor.material_outcome_hash
                or rejection.execution_payload_hash != predecessor.payload_hash
                or rejection.execution_receipt != predecessor.receipt
                or not rejection.feedback
                or run.native_session_ref is None
            ):
                raise OwnerConflict("rejection_lineage_invalid")
            domain_feedback = rejection.feedback

        if completion_rejection is not None:
            if (
                not completion_rejection.feedback
                or run.native_session_ref is None
                or completion_rejection.request_ref != request.request_ref
                or completion_rejection.run_ref != run.run_ref
                or completion_rejection.stage != "bundle"
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
        ) or ((run.native_session_ref is None) != (run.primary_draft is None)):
            raise OwnerConflict("attempt_lineage_invalid")
        return None, None, None, ()

    def _reject_completion_candidate(
        self,
        *,
        unit_ref: str,
        run: BundleStageRun,
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

    def _record_terminal_contract_failure(
        self,
        *,
        unit_ref: str,
        run: BundleStageRun,
        job_ref: str,
        operation_name: str,
        native_session_ref: str,
        failure_code: str,
        detail_code: str,
    ) -> bool:
        """Preserve the non-completion rolling-operation failure path."""

        checkpoint_factory = getattr(
            self._provider,
            "terminal_contract_failure_checkpoint",
            None,
        )
        if not callable(checkpoint_factory):
            return False
        checkpoint = checkpoint_factory(
            job_ref=job_ref,
            operation_name=operation_name,
            native_session_ref=native_session_ref,
            failure_code=failure_code,
            detail_code=detail_code,
        )
        if not isinstance(checkpoint, dict):
            raise BundleSkillUnavailable(
                "codex_contract_failure_checkpoint_invalid"
            )
        self._agent_runtime.record_stage_provider_hard_ceiling(
            unit_ref=unit_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            failure_code=failure_code,
            provider_exit=checkpoint,
        )
        return True

    def _accept_exhaustion_review(
        self,
        *,
        request: StageRunRequest,
        run: BundleStageRun,
        result: BundleExhaustionSkillResult,
    ) -> None:
        accepted_formal_plan = request.accepted_formal_plan
        checkpoint = run.primary_draft
        primary_response_hash = run.primary_invocation.response_hash
        if (
            accepted_formal_plan is None
            or checkpoint is None
            or primary_response_hash is None
            or result.reviewed_assessment != checkpoint.draft
            or result.reviewed_assessment_hash != checkpoint.draft_hash
        ):
            raise OwnerConflict("bundle_exhaustion_primary_binding_invalid")
        assessment_value = result.reviewed_assessment.get(
            "exhaustion_assessment"
        )
        if not isinstance(assessment_value, dict):
            raise OwnerConflict("bundle_exhaustion_assessment_invalid")
        try:
            completion = normalized_completion_contract_from_dict(
                assessment_value.get("completion_contract"),
                plan_document=accepted_formal_plan.plan_document,
            )
        except BundleTargetContractError as error:
            raise OwnerConflict(
                "bundle_exhaustion_completion_contract_invalid"
            ) from error
        raw_records = assessment_value.get("exploration_records")
        if not isinstance(raw_records, list):
            raise OwnerConflict("bundle_exhaustion_exploration_invalid")
        assessment_receipt = (
            self._agent_runtime.query_bundle_exhaustion_assessment_receipt(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        )
        rejected_submissions = (
            self._agent_runtime.query_bundle_exhaustion_rejected_submissions(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        )
        records = tuple(
            bundle_exhaustion_exploration_record_from_claim(
                raw,
                assessment_content_hash=result.reviewed_assessment_hash,
                assessment_receipt=assessment_receipt,
            )
            for raw in cast(list[dict[str, object]], raw_records)
        )
        evidence_identity = (
            "bundle-exhaustion-evidence:"
            + canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "fence_ref": run.fence_ref,
                    "assessment_hash": result.reviewed_assessment_hash,
                }
            )[:48]
        )
        evidence = BundleExhaustionEvidence(
            evidence_identity=evidence_identity,
            stage_run_request_ref=request.request_ref,
            stage_run_request_receipt_ref=request.receipt.receipt_ref,
            stage_run_request_receipt_hash=request.receipt.payload_hash,
            cycle_ref=request.cycle_ref,
            epoch=request.epoch,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,
            execution_fence_ref=run.fence_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            formal_plan_ref=accepted_formal_plan.formal_plan_ref,
            formal_plan_content_hash=(
                accepted_formal_plan.plan_document_hash
            ),
            native_session_ref=result.primary_session_ref,
            primary_invocation_ref=run.primary_invocation.invocation_ref,
            primary_response_hash=primary_response_hash,
            primary_assessment_hash=result.reviewed_assessment_hash,
            review_invocation_ref=run.review_invocation.invocation_ref,
            reviewer_agent_ref=result.reviewer_agent_ref,
            review_findings=result.findings,
            # New evidence never promotes optional collaboration provenance to
            # an independent-child acceptance claim. Historical persisted
            # traces remain readable through the v1 decoder.
            review_trace=None,
            completion_contract=completion,
            exploration_records=records,
            rejected_submissions=rejected_submissions,
        )
        accepted = self._agent_runtime.accept_bundle_exhaustion_evidence(
            evidence=evidence,
            idempotency_key=_operation_key(
                "bundle-exhaustion-evidence",
                run.run_ref,
                run.attempt_ref,
                evidence.evidence_hash,
            ),
        )
        if accepted.evidence != evidence:
            raise OwnerConflict("bundle_exhaustion_evidence_conflict")

    def _drain_bundle_inbox(
        self, run: BundleStageRun
    ) -> BundleInboxCheckpoint:
        """Read, validate, then CAS-ack one complete run-scoped notice prefix."""

        batch = self._agent_runtime.read_bundle_inbox(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
        )
        checkpoint = self._agent_runtime.acknowledge_bundle_inbox(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            batch=batch,
            idempotency_key=_operation_key(
                "bundle-inbox-ack",
                run.run_ref,
                run.attempt_ref,
                run.fence_ref,
                str(batch.after_cursor),
                str(batch.next_cursor),
                str(batch.generation),
                canonical_hash(projection_plain_value(batch)),
            ),
        )
        self._agent_runtime.verify_bundle_inbox_checkpoint(
            checkpoint=checkpoint,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            require_current=True,
        )
        return checkpoint

    def _operation_uses_inbox_checkpoint(
        self,
        *,
        operation_kind: str,
        operation_ref: str,
        checkpoint: BundleInboxCheckpoint,
    ) -> bool:
        bound = self._agent_runtime.query_bundle_inbox_operation_checkpoint(
            operation_kind=operation_kind,
            operation_ref=operation_ref,
        )
        return bound == checkpoint

    def _acknowledge_rolling_provider_boundary(
        self,
        run: BundleStageRun,
        *,
        unit_ref: str,
    ) -> None:
        try:
            self._agent_runtime.acknowledge_provider_safe_point(
                unit_ref=unit_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        except OwnerConflict as error:
            # Decisions created before rolling operations became managed units
            # have no unit to settle. New decisions must replay their verified
            # Owner boundary before any downstream launch or append.
            if error.code != "runtime_provider_unit_not_found":
                raise

    def _current_runtime_binding(self) -> BundleRuntimeBinding | None:
        if self._harnesses is None:
            self._transient_error = (
                "bundle_harness_operation_binding_unavailable"
            )
            return None
        try:
            runtime_binding = self._provider.runtime_binding()
        except (BundleSkillUnavailable, HarnessAdmissionError) as error:
            self._transient_error = error.code
            return None
        return runtime_binding

    def _runtime_binding_is_current(self, run: BundleStageRun) -> bool:
        if not isinstance(run.runtime_binding, BundleRuntimeBinding):
            raise OwnerConflict("bundle_runtime_binding_invalid")
        current = self._current_runtime_binding()
        if current is None:
            return False
        if current != run.runtime_binding:
            self._transient_error = "bundle_runtime_binding_drift"
            return False
        return True

    def _finish_bundle_jobs(self, run: BundleStageRun) -> None:
        finish_job = getattr(self._provider, "finish_job", None)
        if not callable(finish_job):
            return
        for job_ref in {
            run.primary_invocation.operation_ref,
            run.review_invocation.operation_ref,
        }:
            finish_job(job_ref)

    def _discover_current_cycle(self) -> _CurrentCycle | None:
        active: list[_CurrentCycle] = []
        for foreground in self._advancement_engine.query_active_foregrounds(
            stage="bundle"
        ):
            question = self._research_graph.query_question_by_ref(
                cast(str, foreground["question_ref"])
            )
            if question is None or question.quest_ref != foreground.get("quest_ref"):
                raise OwnerConflict("bundle_cycle_index_invalid")
            active.append(
                _CurrentCycle(
                    cast(int, foreground["epoch"]),
                    cast(str, foreground["cycle_ref"]),
                    question,
                )
            )
        if active:
            return max(active, key=lambda item: (item.revision, item.cycle_ref))

        candidates: dict[str, _CurrentCycle] = {}
        for event in self._feed.read_event_type(_CYCLE_EVENT):
            payload = event.payload
            initialization_id = payload.get("initialization_id")
            cycle_ref = payload.get("cycle_ref")
            question_ref = payload.get("question_ref")
            if not all(
                isinstance(value, str) and value
                for value in (initialization_id, cycle_ref, question_ref)
            ):
                raise OwnerConflict("bundle_cycle_index_invalid")
            initialization_id = cast(str, initialization_id)
            cycle_ref = cast(str, cycle_ref)
            question_ref = cast(str, question_ref)
            question = self._research_graph.query_question(initialization_id)
            cycle = self._advancement_engine.query_initial_cycle(initialization_id)
            if (
                question is None
                or cycle is None
                or question.question_ref != question_ref
                or cycle.cycle_ref != cycle_ref
            ):
                raise OwnerConflict("bundle_cycle_index_invalid")
            candidates[cycle_ref] = _CurrentCycle(event.revision, cycle_ref, question)
        if not candidates:
            return None
        return max(candidates.values(), key=lambda item: item.revision)


def _not_eligible_projection(
    *,
    cycle_ref: str | None,
    question_ref: str | None,
    reason_code: str,
    next_stage: str | None,
) -> dict[str, object]:
    return {
        "eligibility": {
            "status": "not_eligible",
            "cycle_ref": cycle_ref,
            "question_ref": question_ref,
            "formal_plan_ref": None,
            "reason": {"code": reason_code},
            "next_stage": next_stage,
        },
        "stage_run_request": None,
        "run": None,
        "target_graph": {
            "status": "not_attempted",
            "targets": [],
            "frontier": [],
        },
        "target_commits": [],
        "baseline_pool": [],
        "disposition": {"status": "not_attempted"},
        "stage_commit": None,
    }


def _bundle_primary_output_kind(run: BundleStageRun) -> str | None:
    checkpoint = run.primary_draft
    if checkpoint is None:
        return None
    if set(checkpoint.draft) == {"exhaustion_assessment"} and isinstance(
        checkpoint.draft.get("exhaustion_assessment"), dict
    ):
        return "exhaustion_assessment"
    return "target_plan"


def _public_request(request: StageRunRequest) -> dict[str, object]:
    if request.stage != "bundle" or request.accepted_formal_plan is None:
        raise OwnerConflict("bundle_stage_request_invalid")
    return {
        "status": "current",
        "request_ref": request.request_ref,
        "cycle_ref": request.cycle_ref,
        "stage": "Bundle",
        "epoch": request.epoch,
        "accepted_question_binding": request.accepted_question.as_dict(),
        "accepted_formal_plan_binding": request.accepted_formal_plan.as_dict(),
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "receipt": request.receipt.as_public_dict(),
    }


def _public_target_graph_rejection(
    rejection: TargetGraphRejection,
) -> dict[str, object]:
    return {
        "status": "rejected",
        "targets": [],
        "frontier": [],
        "submission_ref": rejection.submission_ref,
        "target_plan_hash": rejection.target_plan_hash,
        "rejection": {
            "code": rejection.reason_code,
            "feedback": list(rejection.feedback),
            "receipt": rejection.receipt.as_public_dict(),
        },
    }


def _public_commit(commit: StageCommit) -> dict[str, object]:
    is_bundle_exhaustion = (
        commit.disposition == "exhausted"
        and commit.basis_kind == BUNDLE_EXHAUSTION_BASIS_KIND
    )
    if commit.stage != "bundle" or (
        commit.closure is None and not is_bundle_exhaustion
    ):
        raise OwnerConflict("bundle_stage_commit_invalid")
    target_commit_refs = (
        [] if commit.closure is None else commit.closure.get("target_commit_refs")
    )
    if not isinstance(target_commit_refs, list):
        raise OwnerConflict("bundle_stage_commit_invalid")
    if is_bundle_exhaustion:
        public_outcome_kind = "BundleExhaustion"
    elif commit.outcome_kind == "bundle_skip":
        public_outcome_kind = "BundleSkip"
    else:
        public_outcome_kind = "TargetGraph"
    return {
        "status": "Skipped" if commit.disposition == "skipped" else "Completed",
        "commit_ref": commit.commit_ref,
        "stage_commit_ref": commit.commit_ref,
        "request_ref": commit.request_ref,
        "cycle_ref": commit.cycle_ref,
        "stage": "Bundle",
        "epoch": commit.epoch,
        "run_ref": commit.run_ref,
        "outcome_ref": commit.outcome_ref,
        "outcome_kind": public_outcome_kind,
        "disposition": commit.disposition,
        "target_commit_refs": target_commit_refs,
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
        "next_stage": "Reasoning",
    }


def _target_authorization_assertion(
    graph: AcceptedTargetGraph,
    target: AcceptedTarget,
    *,
    target_spec_hash: str,
) -> dict[str, object]:
    return target_execution_assertion(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=target_spec_hash,
        risk_class=cast(str, target.spec["risk_class"]),
    )


def _target_authorization_requirement(
    graph: AcceptedTargetGraph,
    target: AcceptedTarget,
    *,
    target_spec_hash: str,
) -> dict[str, object]:
    return target_execution_authorization_requirement(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=target_spec_hash,
    )


def _root_target_authorization_assertion(
    run: BundleStageRun,
    condition: dict[str, object],
) -> dict[str, object]:
    if run.stage != "bundle" or run.attempt_generation < 1:
        raise OwnerConflict("bundle_root_human_request_scope_invalid")
    return {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": "bundle_stage",
            "run_ref": run.run_ref,
            "attempt_ref": run.attempt_ref,
            "root_session_ref": run.root_session_ref,
            "fence_ref": run.fence_ref,
            "waiter_generation": run.attempt_generation,
        },
        "condition": condition,
    }


def _target_authorization_command(
    *,
    target: AcceptedTarget,
    run: BundleStageRun,
    assertion: dict[str, object],
    requirement: dict[str, object],
) -> dict[str, object]:
    return {
        "semantic_operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "reconciliation_operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
        "arguments": {
            "effect_id": "target-authorization-"
            + canonical_hash(
                {
                    "run_ref": run.run_ref,
                    "attempt_ref": run.attempt_ref,
                    "target_ref": target.target_ref,
                    "condition": assertion,
                }
            )[:64],
            "request_kind": "capability_authorization",
            "obligation": _TARGET_AUTHORIZATION_OBLIGATION,
            "business_purpose": _TARGET_AUTHORIZATION_PURPOSE,
            "condition": assertion,
            "acceptance_conditions": list(
                _TARGET_AUTHORIZATION_ACCEPTANCE_CONDITIONS
            ),
            "required_authorization": requirement,
        },
    }


def _rolling_provider_unit_ref(
    *,
    operation_ref: str,
    operation_name: str,
    attempt_ref: str,
) -> str:
    """Name one physical rolling turn without aliasing its logical job."""

    return "provider_unit_" + canonical_hash(
        {
            "operation_ref": operation_ref,
            "operation_name": operation_name,
            "attempt_ref": attempt_ref,
        }
    )[:64]


def _operation_key(*parts: str) -> str:
    prefix, *values = parts
    return f"{prefix}:{canonical_hash(values)}"


def _accepted_formal_plan_binding_from_public(
    value: dict[str, object],
) -> AcceptedFormalPlanBinding:
    expected_fields = {
        "formal_plan_ref",
        "content_ref",
        "plan_document_hash",
        "answer_contract_hash",
        "content_receipt",
        "formal_plan_receipt",
        "stage_commit_ref",
        "stage_commit_receipt",
        "plan_document",
    }
    try:
        plan_document = value["plan_document"]
        if set(value) != expected_fields or not isinstance(plan_document, dict):
            raise TypeError("accepted_formal_plan")
        refs = {
            field: value[field]
            for field in (
                "formal_plan_ref",
                "content_ref",
                "plan_document_hash",
                "answer_contract_hash",
                "stage_commit_ref",
            )
        }
        if any(not isinstance(item, str) or not item for item in refs.values()):
            raise TypeError("accepted_formal_plan")
        binding = AcceptedFormalPlanBinding(
            formal_plan_ref=cast(str, refs["formal_plan_ref"]),
            content_ref=cast(str, refs["content_ref"]),
            plan_document_hash=cast(str, refs["plan_document_hash"]),
            answer_contract_hash=cast(str, refs["answer_contract_hash"]),
            content_receipt=_acceptance_receipt_from_public(
                value["content_receipt"]
            ),
            formal_plan_receipt=_acceptance_receipt_from_public(
                value["formal_plan_receipt"]
            ),
            stage_commit_ref=cast(str, refs["stage_commit_ref"]),
            stage_commit_receipt=_acceptance_receipt_from_public(
                value["stage_commit_receipt"]
            ),
            plan_document=plan_document,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("accepted_formal_plan_lineage_invalid") from error
    if binding.as_dict() != value:
        raise OwnerConflict("accepted_formal_plan_lineage_invalid")
    return binding


def _accepted_idea_set_binding_from_public(
    value: dict[str, object],
) -> AcceptedIdeaSetBinding:
    expected_fields = {
        "outcome_ref",
        "outcome_kind",
        "content_ref",
        "payload_hash",
        "outcome_hash",
        "content_receipt",
        "outcome_receipt",
        "stage_commit_ref",
        "stage_commit_receipt",
        "idea_set",
    }
    try:
        idea_set = value["idea_set"]
        if set(value) != expected_fields or not isinstance(idea_set, dict):
            raise TypeError("accepted_idea_set")
        refs = {
            field: value[field]
            for field in (
                "outcome_ref",
                "content_ref",
                "payload_hash",
                "outcome_hash",
                "stage_commit_ref",
            )
        }
        if any(not isinstance(item, str) or not item for item in refs.values()):
            raise TypeError("accepted_idea_set")
        binding = AcceptedIdeaSetBinding(
            outcome_ref=cast(str, refs["outcome_ref"]),
            outcome_kind=str(value["outcome_kind"]),
            content_ref=cast(str, refs["content_ref"]),
            payload_hash=cast(str, refs["payload_hash"]),
            outcome_hash=cast(str, refs["outcome_hash"]),
            content_receipt=_acceptance_receipt_from_public(
                value["content_receipt"]
            ),
            outcome_receipt=_acceptance_receipt_from_public(
                value["outcome_receipt"]
            ),
            stage_commit_ref=cast(str, refs["stage_commit_ref"]),
            stage_commit_receipt=_acceptance_receipt_from_public(
                value["stage_commit_receipt"]
            ),
            idea_set=idea_set,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("accepted_idea_set_lineage_invalid") from error
    if binding.as_dict() != value:
        raise OwnerConflict("accepted_idea_set_lineage_invalid")
    return binding


def _acceptance_receipt_from_public(value: object) -> AcceptanceReceipt:
    expected_fields = {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("status") != "accepted"
        or any(
            not isinstance(value.get(field), str) or not value.get(field)
            for field in expected_fields - {"status"}
        )
    ):
        raise TypeError("receipt")
    return AcceptanceReceipt(
        issuer=cast(str, value["issuer"]),
        kind=cast(str, value["kind"]),
        receipt_ref=cast(str, value["receipt_ref"]),
        subject_ref=cast(str, value["subject_ref"]),
        payload_hash=cast(str, value["payload_hash"]),
    )
