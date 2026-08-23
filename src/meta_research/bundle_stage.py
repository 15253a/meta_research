from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

from meta_research.bundle_contract import (
    BUNDLE_CONTEXT_PACK_SCHEMA_REF,
    target_execution_assertion,
    target_execution_authorization_requirement,
)
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillContractError,
    BundleSkillDraft,
    BundleSkillProvider,
    BundleSkillRequest,
    BundleSkillUnavailable,
    review_record,
    validate_bundle_dispatch_result,
    validate_bundle_skill_draft,
    validate_bundle_skill_result,
)
from meta_research.feed import DurableFeed
from meta_research.experiment_contract import ExperimentIntent
from meta_research.idea_stage import _public_run
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    StageCommit,
    StageRunRequest,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    BundleRuntimeBinding,
    BundleStageRun,
)
from meta_research.owners.common import (
    AcceptedFormalPlanBinding,
    OwnerConflict,
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
)
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_memory import (
    AcceptedPlanDocument,
    AssetIntakeRequest,
    ResearchMemoryInterface,
)
from meta_research.target_commit_evidence import (
    TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
    target_commit_evidence_document,
    target_commit_evidence_provenance,
)


_CYCLE_EVENT = "advancement_engine.initial_cycle_activated"


class ExperimentCoordinator(Protocol):
    def start(
        self,
        intent: ExperimentIntent,
        idempotency_key: str,
        *,
        require_idle: bool = False,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class _CurrentCycle:
    revision: int
    cycle_ref: str
    question: AcceptedQuestion


@dataclass(frozen=True)
class _EligibleBundle:
    current: _CurrentCycle
    formal_plan: FormalPlanDecision
    plan_document: AcceptedPlanDocument
    plan_commit: StageCommit

    def accepted_formal_plan(self) -> AcceptedFormalPlanBinding:
        return AcceptedFormalPlanBinding(
            formal_plan_ref=cast(str, self.formal_plan.formal_plan_ref),
            content_ref=self.plan_document.content_ref,
            plan_document_hash=self.plan_document.plan_document_hash,
            answer_contract_hash=self.plan_document.answer_contract_hash,
            content_receipt=self.plan_document.receipt,
            formal_plan_receipt=self.formal_plan.receipt,
            stage_commit_ref=self.plan_commit.commit_ref,
            stage_commit_receipt=self.plan_commit.receipt,
            plan_document=self.plan_document.plan_document,
        )


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
        experiment: ExperimentCoordinator,
        human_collaboration: HumanCollaborationInterface | None = None,
    ) -> None:
        self._feed = feed
        self._advancement_engine = advancement_engine
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._research_graph = research_graph
        self._provider = provider
        self._experiment = experiment
        self._human_collaboration = human_collaboration
        self._transient_error: str | None = None

    @property
    def transient_error(self) -> str | None:
        return self._transient_error

    def process_once(self) -> bool:
        """Advance at most one durable Bundle boundary."""

        current = self._discover_current_cycle()
        if current is None:
            return False
        eligible, _reason, _next = self._qualify(current)
        if eligible is None:
            return False
        request = self._advancement_engine.query_bundle_stage_request(current.cycle_ref)
        if request is None:
            accepted_formal_plan = eligible.accepted_formal_plan()
            self._advancement_engine.ensure_bundle_stage_request(
                cycle_ref=current.cycle_ref,
                accepted_question=current.question.as_binding(),
                accepted_formal_plan=accepted_formal_plan,
                context_pack={
                    "schema_ref": BUNDLE_CONTEXT_PACK_SCHEMA_REF,
                    "cycle_ref": current.cycle_ref,
                    "accepted_question_binding": current.question.as_binding().as_dict(),
                    "accepted_formal_plan_binding": accepted_formal_plan.as_dict(),
                },
                idempotency_key=_operation_key(
                    "bundle-request", current.cycle_ref, "worker"
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
            try:
                runtime_binding = self._provider.runtime_binding()
            except BundleSkillUnavailable as error:
                self._transient_error = error.code
                return False
            self._agent_runtime.admit_bundle_stage(
                request,
                _operation_key("bundle-admit", request.request_ref),
                runtime_binding=runtime_binding,
            )
            self._transient_error = None
            return True
        if run.execution is None:
            return self._execute_target_plan(request, run)
        graph = self._research_graph.query_target_graph(request.request_ref)
        if graph is None:
            execution = run.execution
            self._research_graph.accept_target_graph(
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
        if len(commits) == len(graph.targets):
            if run.completion is None:
                self._agent_runtime.complete_bundle_run(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    target_graph_ref=graph.graph_ref,
                    decision_receipt=graph.receipt,
                    idempotency_key=_operation_key(
                        "bundle-complete", run.run_ref, graph.graph_ref
                    ),
                )
                return True
            if (
                self._advancement_engine.query_bundle_stage_commit(request.request_ref)
                is None
            ):
                self._advancement_engine.commit_bundle_stage(
                    request_ref=request.request_ref,
                    run_ref=run.run_ref,
                    target_graph_ref=graph.graph_ref,
                    target_commit_receipts=tuple(commit.receipt for commit in commits),
                    run_completion_receipt=run.completion.receipt,
                    target_graph_receipt=graph.receipt,
                    idempotency_key=_operation_key(
                        "bundle-commit", request.request_ref, graph.graph_ref
                    ),
                )
                self._finish_bundle_jobs(run)
                return True
            self._finish_bundle_jobs(run)
            return False
        frontier = self._research_graph.query_target_frontier(graph.graph_ref)
        authorizations: dict[str, _TargetAuthorization] = {}
        for target in frontier:
            if target.spec.get("risk_class") != "high":
                continue
            authorization, changed = self._advance_target_authorization(
                graph=graph,
                target=target,
            )
            if changed:
                return True
            if authorization is not None:
                authorizations[target.target_ref] = authorization
        dispatchable = tuple(
            target
            for target in frontier
            if target.spec.get("risk_class") != "high"
            or target.target_ref in authorizations
        )
        dispatch_frontier = tuple(
            self._dispatch_target(target) for target in dispatchable
        )
        dispatch_state = self._dispatch_state(graph, commits)
        decisions = self._agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        latest = decisions[-1] if decisions else None
        same_input = latest is not None and (
            latest.graph_ref == graph.graph_ref
            and latest.frontier == dispatch_frontier
            and latest.state == dispatch_state
        )
        pending_dispatch = (
            latest
            if same_input
            and latest.action == "dispatch"
            and latest.selected_target_ref
            in {target.target_ref for target in dispatchable}
            else None
        )
        coordination_needed = bool(dispatchable)
        if pending_dispatch is None and coordination_needed and not same_input:
            if run.native_session_ref is None:
                raise OwnerConflict("bundle_native_session_missing")
            dispatch_request = BundleDispatchRequest(
                stage_request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                graph_ref=graph.graph_ref,
                generation=len(decisions) + 1,
                frontier=dispatch_frontier,
                state=dispatch_state,
                native_session_ref=run.native_session_ref,
                runtime_binding=cast(BundleRuntimeBinding, run.runtime_binding),
                job_ref=run.review_invocation.invocation_ref,
            )
            try:
                result = self._provider.schedule_target(dispatch_request)
                validate_bundle_dispatch_result(dispatch_request, result)
            except BundleSkillUnavailable as error:
                self._transient_error = error.code
                return False
            except BundleSkillContractError as error:
                self._transient_error = str(error)
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
        if pending_dispatch is not None:
            target = next(
                target
                for target in dispatchable
                if target.target_ref == pending_dispatch.selected_target_ref
            )
            binding = self._research_graph.query_target_run_binding(target.target_ref)
            if binding is None:
                execution = self._experiment.start(
                    _target_experiment_intent(graph.quest_ref, target),
                    _operation_key("target-start", target.target_ref),
                )
                identities = execution.get("identities")
                runtime = execution.get("execution")
                intent = execution.get("intent")
                if (
                    not isinstance(identities, dict)
                    or not isinstance(runtime, dict)
                    or not isinstance(intent, dict)
                    or not isinstance(identities.get("evaluation_attempt_ref"), str)
                    or not isinstance(runtime.get("run_ref"), str)
                    or not isinstance(intent.get("execution_request_ref"), str)
                ):
                    raise OwnerConflict("target_run_admission_invalid")
                evaluation_attempt_ref = cast(str, identities["evaluation_attempt_ref"])
                domain = self._research_graph.query_experiment(evaluation_attempt_ref)
                if domain is None:
                    raise OwnerConflict("target_run_domain_admission_missing")
                authorization = authorizations.get(target.target_ref)
                admission = self._agent_runtime.admit_target_run(
                    target_ref=target.target_ref,
                    target_spec_hash=target.spec_hash,
                    graph_ref=graph.graph_ref,
                    stage_request_ref=request.request_ref,
                    quest_ref=graph.quest_ref,
                    target_run_ref=cast(str, runtime["run_ref"]),
                    evaluation_attempt_ref=evaluation_attempt_ref,
                    execution_request_ref=cast(str, intent["execution_request_ref"]),
                    definition_hash=domain.execution_request.definition_hash,
                    idempotency_key=_operation_key(
                        "target-run-admit", target.target_ref
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
                self._research_graph.bind_target_run(
                    target_ref=target.target_ref,
                    target_run_ref=cast(str, runtime["run_ref"]),
                    evaluation_attempt_ref=evaluation_attempt_ref,
                    execution_request_ref=cast(str, intent["execution_request_ref"]),
                    definition_hash=domain.execution_request.definition_hash,
                    admission_receipt=admission.receipt,
                )
                self._transient_error = None
                return True
        for target in graph.targets:
            if target.target_ref in committed_refs:
                continue
            binding = self._research_graph.query_target_run_binding(target.target_ref)
            if binding is None:
                continue
            domain = self._research_graph.query_experiment(
                binding.evaluation_attempt_ref
            )
            target_run = self._agent_runtime.query_experiment_run(
                binding.evaluation_attempt_ref
            )
            if (
                domain is None
                or target_run is None
                or domain.formal_measurement_status != "accepted"
                or target_run.status != "executed"
                or target_run.result_hash is None
                or target_run.execution_receipt is None
            ):
                continue
            result_roles = tuple(
                role
                for role in self._research_graph.query_experiment_asset_roles(
                    binding.evaluation_attempt_ref
                )
                if role.role == "result_content"
            )
            if len(result_roles) != 1:
                raise OwnerConflict("target_commit_result_content_invalid")
            materialized = self._research_memory.materialize_asset(
                result_roles[0].binding.version_ref
            )
            try:
                result_content = json.loads(materialized.content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise OwnerConflict("target_commit_result_content_invalid") from error
            if not isinstance(result_content, dict):
                raise OwnerConflict("target_commit_result_content_invalid")
            self._research_graph.accept_target_commit(
                target_ref=target.target_ref,
                target_run_ref=target_run.run_ref,
                execution_attempt_ref=target_run.attempt_ref,
                fence_ref=target_run.fence_ref,
                execution_result_hash=target_run.result_hash,
                execution_receipt=target_run.execution_receipt,
                result_content=result_content,
            )
            return True
        return False

    def _advance_target_authorization(
        self,
        *,
        graph: AcceptedTargetGraph,
        target: AcceptedTarget,
    ) -> tuple[_TargetAuthorization | None, bool]:
        if self._human_collaboration is None:
            self._transient_error = "human_collaboration_unavailable"
            return None, False
        assertion = _target_authorization_assertion(graph, target)
        requirement = _target_authorization_requirement(graph, target)
        request = self._target_human_request(graph.quest_ref, assertion)
        waiter = {
            "waiter_ref": target.target_ref,
            "generation": 1,
            "target_assertion": assertion,
            "wait_scope": "local",
            "other_blockers": [],
        }
        if request is None:
            self._agent_runtime.open_human_request(
                request_kind="capability_authorization",
                obligation=("决定是否仅为这一精确高风险 Target 授予一次执行权限。"),
                business_purpose=(
                    "只恢复对应 Target；同一 DAG 中其他普通 Target 继续推进。"
                ),
                target_assertion=assertion,
                acceptance_conditions=(
                    "Human Collaboration 保存 exact granted authorization receipt。",
                    "Agent Runtime 重验 current Target/spec 与同一 waiter generation。",
                ),
                direct_waiter=waiter,
                required_authorization=requirement,
                quest_ref=graph.quest_ref,
                idempotency_key=_operation_key(
                    "target-human-request", target.target_ref
                ),
            )
            self._transient_error = "target_high_risk_authorization_required"
            return None, True
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
        waiters = cast(list[dict[str, object]], request.get("direct_waiters", []))
        matching = [
            item
            for item in waiters
            if item.get("waiter_ref") == target.target_ref
            and item.get("generation") == 1
            and item.get("target_assertion") == assertion
        ]
        authorization_ref = self._target_authorization_receipt(
            graph.quest_ref, requirement
        )
        if len(matching) != 1 or authorization_ref is None:
            self._transient_error = "target_high_risk_authorization_stale"
            return None, False
        current_waiter = matching[0]
        if current_waiter.get("status") == "blocked":
            validation = self._agent_runtime.validate_human_request_waiter(
                cast(str, request["request_ref"]),
                waiter_ref=target.target_ref,
                generation=1,
                target_assertion=assertion,
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
                waiter_ref=target.target_ref,
                generation=1,
                authorization_receipt_ref=authorization_ref,
            ),
            False,
        )

    def _target_human_request(
        self, quest_ref: str, assertion: dict[str, object]
    ) -> dict[str, object] | None:
        matches = [
            request
            for request in self._agent_runtime.query_human_requests(
                quest_ref=quest_ref, include_history=True
            )
            if request.get("kind") == "capability_authorization"
            and request.get("target_assertion") == assertion
            and request.get("current") is True
        ]
        if len(matches) > 1:
            raise OwnerConflict("target_human_request_conflict")
        return matches[0] if matches else None

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
    ) -> dict[str, object]:
        committed = {commit.target_ref: commit for commit in commits}
        blocked: list[dict[str, object]] = []
        running: list[dict[str, object]] = []
        for target in graph.targets:
            if target.target_ref in committed:
                continue
            binding = self._research_graph.query_target_run_binding(target.target_ref)
            if binding is not None:
                target_run = self._agent_runtime.query_experiment_run(
                    binding.evaluation_attempt_ref
                )
                if target_run is not None and target_run.status == "failed":
                    blocked.append(
                        {
                            "target_ref": target.target_ref,
                            "reason": {
                                "code": target_run.failure_code or "target_run_failed"
                            },
                        }
                    )
                else:
                    running.append(
                        {
                            "target_ref": target.target_ref,
                            "target_run_ref": binding.target_run_ref,
                        }
                    )
                continue
            if target.spec.get("risk_class") == "high":
                assertion = _target_authorization_assertion(graph, target)
                request = self._target_human_request(graph.quest_ref, assertion)
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

    @staticmethod
    def _dispatch_target(target: AcceptedTarget) -> dict[str, object]:
        return {
            "target_ref": target.target_ref,
            "target_key": target.target_key,
            "spec_hash": target.spec_hash,
            "spec": target.spec,
            "dependency_refs": list(target.dependency_refs),
            "receipt": target.receipt.as_public_dict(),
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
        graph_projection = {
            "status": "not_attempted",
            "targets": [],
            "frontier": [],
        }
        target_commit_projection: list[dict[str, object]] = []
        baseline_pool: list[dict[str, object]] = []
        if graph is not None:
            frontier = self._research_graph.query_target_frontier(graph.graph_ref)
            frontier_refs = {target.target_ref for target in frontier}
            commits = self._research_graph.query_target_commits(graph.graph_ref)
            commit_by_target = {commit.target_ref: commit for commit in commits}
            run_by_target = {
                target.target_ref: self._research_graph.query_target_run_binding(
                    target.target_ref
                )
                for target in graph.targets
            }
            execution_by_target = {}
            for target in graph.targets:
                target_binding = run_by_target[target.target_ref]
                execution_by_target[target.target_ref] = (
                    None
                    if target_binding is None
                    else self._agent_runtime.query_experiment_run(
                        target_binding.evaluation_attempt_ref
                    )
                )
            target_rows: list[dict[str, object]] = []
            for target in graph.targets:
                target_binding = run_by_target[target.target_ref]
                target_execution = execution_by_target[target.target_ref]
                blocker = None
                if target.target_ref in commit_by_target:
                    status = "committed"
                elif (
                    target_execution is not None and target_execution.status == "failed"
                ):
                    status = "blocked"
                    blocker = {
                        "code": (target_execution.failure_code or "target_run_failed")
                    }
                elif target_binding is not None:
                    status = "running"
                elif (
                    target.spec.get("risk_class") == "high"
                    and target.target_ref in frontier_refs
                ):
                    assertion = _target_authorization_assertion(graph, target)
                    human_request = self._target_human_request(
                        graph.quest_ref, assertion
                    )
                    waiter = (
                        None
                        if human_request is None
                        else next(
                            (
                                item
                                for item in cast(
                                    list[dict[str, object]],
                                    human_request.get("direct_waiters", []),
                                )
                                if item.get("waiter_ref") == target.target_ref
                                and item.get("generation") == 1
                            ),
                            None,
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
                        "target_type": target.spec["target_type"],
                        "spec_hash": target.spec_hash,
                        "dependency_refs": list(target.dependency_refs),
                        "target_run_ref": (
                            None
                            if target_binding is None
                            else target_binding.target_run_ref
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
                "receipt": graph.receipt.as_public_dict(),
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
                    "metric_result": commit.closure["metric_result"],
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
        disposition: dict[str, object] = {"status": "not_attempted"}
        if graph is not None:
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
                "formal_plan_ref": eligible.formal_plan.formal_plan_ref,
                "reason": None,
                "next_stage": "Bundle",
            },
            "stage_run_request": None if request is None else _public_request(request),
            "run": None if run is None else _public_run(run),
            "target_graph": graph_projection,
            "target_commits": target_commit_projection,
            "baseline_pool": baseline_pool,
            "disposition": disposition,
            "stage_commit": (
                None if stage_commit is None else _public_commit(stage_commit)
            ),
        }

    def _qualify(
        self, current: _CurrentCycle
    ) -> tuple[_EligibleBundle | None, str | None, str | None]:
        request = self._advancement_engine.query_plan_stage_request(current.cycle_ref)
        if request is None:
            return None, "accepted_formal_plan_unavailable", "Plan"
        commit = self._advancement_engine.query_plan_stage_commit(request.request_ref)
        if commit is None:
            return None, "accepted_formal_plan_unavailable", "Plan"
        run = self._agent_runtime.query_plan_stage_run(request.request_ref)
        if (
            request.stage != "plan"
            or commit.stage != "plan"
            or commit.cycle_ref != current.cycle_ref
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
        return _EligibleBundle(current, decision, content, commit), None, "Bundle"

    def _assert_request(
        self, request: StageRunRequest, eligible: _EligibleBundle
    ) -> None:
        if (
            request.stage != "bundle"
            or request.cycle_ref != eligible.current.cycle_ref
            or request.accepted_question != eligible.current.question.as_binding()
            or request.accepted_formal_plan != eligible.accepted_formal_plan()
        ):
            raise OwnerConflict("bundle_stage_request_lineage_invalid")

    def _execute_target_plan(
        self, request: StageRunRequest, run: BundleStageRun
    ) -> bool:
        accepted_formal_plan = request.accepted_formal_plan
        if accepted_formal_plan is None or not isinstance(
            run.runtime_binding, BundleRuntimeBinding
        ):
            raise OwnerConflict("bundle_runtime_binding_invalid")
        try:
            runtime_binding = self._provider.runtime_binding()
        except BundleSkillUnavailable as error:
            self._transient_error = error.code
            return False
        if runtime_binding != run.runtime_binding:
            self._transient_error = "bundle_runtime_binding_drift"
            return False
        skill_request = BundleSkillRequest(
            stage_request_ref=request.request_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            formal_plan_ref=accepted_formal_plan.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            plan_document=accepted_formal_plan.plan_document,
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            native_session_ref=run.native_session_ref,
            job_ref=(
                run.primary_invocation.invocation_ref
                if run.primary_draft is None
                else run.review_invocation.invocation_ref
            ),
        )
        if run.primary_draft is None:
            try:
                draft = self._provider.generate_draft(skill_request)
                draft_hash = validate_bundle_skill_draft(skill_request, draft)
            except BundleSkillUnavailable as error:
                self._transient_error = error.code
                return False
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
        draft = BundleSkillDraft(
            draft=run.primary_draft.draft,
            primary_session_ref=run.primary_draft.native_session_ref,
            adapter_kind=run.primary_draft.adapter_kind,
        )
        try:
            result = self._provider.review_draft(skill_request, draft)
            draft_hash, target_plan_hash, _review_hash = validate_bundle_skill_result(
                skill_request, result
            )
        except BundleSkillUnavailable as error:
            self._transient_error = error.code
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
                    "review_hash": canonical_hash(review),
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

    def _finish_bundle_jobs(self, run: BundleStageRun) -> None:
        finish_job = getattr(self._provider, "finish_job", None)
        if not callable(finish_job):
            return
        for job_ref in {
            run.primary_invocation.invocation_ref,
            run.review_invocation.invocation_ref,
        }:
            finish_job(job_ref)

    def _discover_current_cycle(self) -> _CurrentCycle | None:
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


def _public_commit(commit: StageCommit) -> dict[str, object]:
    if commit.stage != "bundle" or commit.closure is None:
        raise OwnerConflict("bundle_stage_commit_invalid")
    target_commit_refs = commit.closure.get("target_commit_refs")
    if not isinstance(target_commit_refs, list):
        raise OwnerConflict("bundle_stage_commit_invalid")
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
        "outcome_kind": (
            "BundleSkip" if commit.outcome_kind == "bundle_skip" else "TargetGraph"
        ),
        "disposition": commit.disposition,
        "target_commit_refs": target_commit_refs,
        "run_completion_receipt": (
            None
            if commit.run_completion_receipt is None
            else commit.run_completion_receipt.as_public_dict()
        ),
        "outcome_receipt": commit.outcome_receipt.as_public_dict(),
        "closure_hash": canonical_hash(commit.closure),
        "receipt": commit.receipt.as_public_dict(),
        "next_stage": "Reasoning",
    }


def _target_experiment_intent(
    quest_ref: str, target: AcceptedTarget
) -> ExperimentIntent:
    return ExperimentIntent(
        execution_request_ref=f"bundle-target-{target.target_ref}",
        quest_ref=quest_ref,
        title=cast(str, target.spec["title"]),
        hypothesis=cast(str, target.spec["hypothesis"]),
        variant_parameter=float(target.spec["variant_parameter"]),
        sample_count=cast(int, target.spec["sample_count"]),
    )


def _target_authorization_assertion(
    graph: AcceptedTargetGraph, target: AcceptedTarget
) -> dict[str, object]:
    return target_execution_assertion(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=target.spec_hash,
        risk_class=cast(str, target.spec["risk_class"]),
    )


def _target_authorization_requirement(
    graph: AcceptedTargetGraph, target: AcceptedTarget
) -> dict[str, object]:
    return target_execution_authorization_requirement(
        quest_ref=graph.quest_ref,
        stage_request_ref=graph.request_ref,
        graph_ref=graph.graph_ref,
        target_ref=target.target_ref,
        target_spec_hash=target.spec_hash,
    )


def _operation_key(*parts: str) -> str:
    prefix, *values = parts
    return f"{prefix}:{canonical_hash(values)}"
