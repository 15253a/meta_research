from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy import text

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionProvider,
    NatureDownloaderAdapter,
)
from meta_research.auth import Authentication
from meta_research.autonomous_creation import AutonomousCreationService
from meta_research.bundle_exhaustion import BundleExhaustionOwnerProofVerifier
from meta_research.bundle_stage import BundleStageWorker
from meta_research.bundle_skill import CodexBundleSkillAdapter, BundleSkillProvider
from meta_research.bundle_reuse_owner_proofs import (
    BundleTargetCandidateOwnerProofVerifier,
)
from meta_research.database import Database
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DeepFetchAcquisitionClient,
    DeepFetchProvider,
    DeepFetchRunRequest,
)
from meta_research.feed import DurableFeed
from meta_research.experiment import (
    BuiltinMicroExperimentProvider,
    ExperimentProvider,
    ExperimentService,
)
from meta_research.first_question_deepfetch import FirstQuestionDeepFetchWorker
from meta_research.harness import HarnessRuntime
from meta_research.harness_control import DurableHarnessOperationCanceller
from meta_research.harness_adapters import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    HarnessAdapter,
    HarnessSupervisorTransport,
    TARGET_ROOT_DEFAULT_TIMEOUT_SECONDS,
)
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillProvider
from meta_research.idea_stage import IdeaStageWorker
from meta_research.manual_creation import (
    create_manual_question_confirmation_verifier,
)
from meta_research.migration import upgrade_database
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    create_advancement_engine_interface,
    create_advancement_engine_receipt_verifier,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    create_agent_runtime_interface,
    create_agent_runtime_receipt_verifier,
    create_host_compute_observation_reader,
)
from meta_research.owners.human_collaboration import (
    HumanCollaborationInterface,
    create_bundle_confirmation_verifier,
    create_deepfetch_request_verifier,
    create_human_collaboration_interface,
    create_human_response_verifier,
)
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_graph import (
    ResearchGraphInterface,
    TargetCommitEvidenceAuthority,
    create_research_graph_interface,
    create_research_graph_receipt_verifier,
)
from meta_research.owners.research_memory import (
    ResearchMemoryInterface,
    create_research_memory_interface,
    create_research_memory_receipt_verifier,
)
from meta_research.owners.target_run_runtime import (
    SQLiteTargetRunAgentAuthority,
    SQLiteTargetRunGraphAuthority,
    SQLiteTargetRunMemoryAuthority,
)
from meta_research.owners.target_root_lifecycle import (
    SQLiteTargetRootLifecycleAuthority,
)
from meta_research.paths import DataRoot
from meta_research.plan_skill import CodexPlanSkillAdapter, PlanSkillProvider
from meta_research.plan_stage import PlanStageWorker
from meta_research.power_inhibitors import (
    OperatorAttestedPowerInhibitor,
    ProductionPowerInhibitor,
)
from meta_research.projection import PublicProjection
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    HostComputeProbe,
    IntentDraftingProvider,
    NvidiaSmiProbe,
    ProposalDrafter,
    _CancellableProcessRunner,
)
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from meta_research.target_run_finalizer import (
    SQLiteTargetRootCompletionMemoryAuthority,
    TargetRunFinalizer,
)
from meta_research.target_run_runtime import TargetRunRuntime
from meta_research.reasoning_skill import (
    CodexReasoningSkillAdapter,
    ReasoningSkillProvider,
)
from meta_research.reasoning_stage import ReasoningStageWorker
from meta_research.quest_completion import QuestCompletionService
from meta_research.runtime_protection import (
    PowerInhibitor,
    RuntimeBoundaryRecorder,
    RuntimeEventLogger,
    RuntimeProtection,
    TelemetryExporter,
)
from meta_research.telemetry import (
    OtlpHttpTelemetryExporter,
    validate_otlp_http_endpoint,
)
from meta_research.writing import WritingReportService
from meta_research.writing_delivery import WritingDeliveryProviderRegistry
from meta_research.writing_renderer import WritingRendererRegistry
from meta_research.writing_skill import CodexWritingSkillAdapter, WritingSkillProvider
from meta_research.semantic_owner_gateway import create_semantic_owner_gateway


@dataclass(frozen=True)
class OwnerInterfaces:
    research_graph: ResearchGraphInterface
    advancement_engine: AdvancementEngineInterface
    research_memory: ResearchMemoryInterface
    agent_runtime: AgentRuntimeInterface
    human_collaboration: HumanCollaborationInterface


@dataclass(frozen=True)
class TargetRunAuthorities:
    research_memory: SQLiteTargetRunMemoryAuthority
    research_graph: SQLiteTargetRunGraphAuthority
    agent_runtime: SQLiteTargetRunAgentAuthority


class _DeepFetchRequestVerifierRouter:
    """Late-bound issuer router used while AR is composed before AE."""

    def __init__(self, human_verifier: object) -> None:
        self._human_verifier = human_verifier
        self._advancement_engine: AdvancementEngineInterface | None = None

    def bind_advancement_engine(
        self, advancement_engine: AdvancementEngineInterface
    ) -> None:
        self._advancement_engine = advancement_engine

    def verify_deepfetch_run_request(self, **values: object) -> None:
        if values.get("creation_context_kind") == "autonomous_question_creation":
            if self._advancement_engine is None:
                raise RuntimeError("autonomous deepfetch verifier is not bound")
            self._advancement_engine.verify_autonomous_deepfetch_run_request(
                **values
            )
            return
        verify = getattr(self._human_verifier, "verify_deepfetch_run_request")
        verify(**values)


class _DeepFetchRequestAuthorityRouter:
    def __init__(
        self,
        human_collaboration: HumanCollaborationInterface,
        advancement_engine: AdvancementEngineInterface,
    ) -> None:
        self._human_collaboration = human_collaboration
        self._advancement_engine = advancement_engine

    def query_next_deepfetch_request(
        self, excluded_request_refs: tuple[str, ...] = ()
    ) -> DeepFetchRunRequest | None:
        request = self._human_collaboration.query_next_deepfetch_request(
            excluded_request_refs
        )
        if request is not None:
            return request
        return self._advancement_engine.query_next_autonomous_deepfetch_request(
            excluded_request_refs
        )

    def record_deepfetch_succeeded(
        self, request_ref: str, run_ref: str, snapshot: object
    ) -> None:
        if (
            getattr(snapshot, "creation_context_kind", None)
            == "autonomous_question_creation"
        ):
            self._advancement_engine.record_autonomous_deepfetch_succeeded(
                request_ref=request_ref,
                run_ref=run_ref,
                snapshot=snapshot,
            )
            return
        self._human_collaboration.record_deepfetch_succeeded(
            request_ref, run_ref, snapshot  # type: ignore[arg-type]
        )

    def record_deepfetch_failed(
        self,
        request_ref: str,
        failure_code: str,
        run_ref: str | None = None,
    ) -> None:
        autonomous = (
            self._advancement_engine.query_autonomous_deepfetch_request_by_ref(
                request_ref
            )
        )
        if autonomous is not None:
            self._advancement_engine.record_autonomous_deepfetch_failed(
                request_ref=request_ref,
                failure_code=failure_code,
                run_ref=run_ref,
            )
            return
        self._human_collaboration.record_deepfetch_failed(
            request_ref, failure_code, run_ref
        )


@dataclass
class ProductionRuntime:
    data_root: DataRoot
    owners: OwnerInterfaces
    authentication: Authentication
    feed: DurableFeed
    projection: PublicProjection
    idea_stage: IdeaStageWorker
    plan_stage: PlanStageWorker
    bundle_stage: BundleStageWorker
    reasoning_stage: ReasoningStageWorker
    autonomous_creation: AutonomousCreationService
    quest_completion: QuestCompletionService
    deepfetch: FirstQuestionDeepFetchWorker
    experiment: ExperimentService
    writing: WritingReportService
    harnesses: HarnessRuntime
    target_run_authorities: TargetRunAuthorities
    target_run_runtime: TargetRunRuntime
    target_root_lifecycle: SQLiteTargetRootLifecycleAuthority
    target_run_finalizer: TargetRunFinalizer
    target_root_readiness: dict[str, object]
    runtime_protection: RuntimeProtection
    _database: Database
    _telemetry_exporter_factory: Callable[[str], TelemetryExporter]
    _provider_lifecycles: tuple[object, ...] = ()
    _stop_requested: bool = False

    def request_stop(self) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        self.runtime_protection.interrupt_active(
            interruption_kind="daemon",
            reason_code="daemon_shutdown_requested",
        )
        for lifecycle in self._provider_lifecycles:
            request_stop = getattr(lifecycle, "request_stop", None)
            if callable(request_stop):
                request_stop()

    def query_target_root_readiness(self) -> dict[str, object]:
        return dict(self.target_root_readiness)

    def query_runtime_observability(self) -> dict[str, object]:
        return self.runtime_protection.query_evidence()

    def validate_telemetry_authorization_request(
        self,
        *,
        scope_ref: str,
        capability: str,
        decision: str,
        scope: dict[str, object],
        confirmation_receipt_ref: str,
    ) -> None:
        """Validate an exact OTel command before moving the HC authority head."""

        self._validate_telemetry_authorization_scope(
            scope_ref=scope_ref,
            capability=capability,
            decision=decision,
            scope=scope,
        )
        projection = self.owners.human_collaboration.query_collaboration_projection(
            ("runtime:telemetry",)
        )
        current = [
            authorization
            for authorization in projection["authorizations"]
            if authorization.get("authorization_kind") == "capability"
            and authorization.get("capability") == "opentelemetry_export"
            and authorization.get("scope_ref") == "runtime:telemetry"
            and authorization.get("is_current") is not False
        ]
        if len(current) > 1:
            raise OwnerConflict("telemetry_authorization_head_invalid")
        if current and (
            current[0].get("confirmation_receipt_ref")
            == confirmation_receipt_ref
            and current[0].get("decision") == decision
            and isinstance(current[0].get("requirement"), dict)
            and cast(dict[str, object], current[0]["requirement"]).get("scope")
            == scope
        ):
            # Exact HTTP/idempotency replay of the already-applied command.
            return

        telemetry = self.runtime_protection.query_evidence()["telemetry"]
        if not isinstance(telemetry, dict):
            raise OwnerConflict("telemetry_authorization_state_invalid")
        mode = telemetry.get("mode")
        if decision == "granted" and mode in {"active", "revocation_pending"}:
            raise OwnerConflict(
                "telemetry_revocation_pending"
                if mode == "revocation_pending"
                else "telemetry_revoke_required"
            )
        if decision == "revoked" and mode == "revocation_pending":
            raise OwnerConflict("telemetry_revocation_pending")

    @staticmethod
    def _validate_telemetry_authorization_scope(
        *,
        scope_ref: str,
        capability: str,
        decision: str,
        scope: dict[str, object],
    ) -> None:
        """Validate the installed product's exact, non-secret OTel scope."""

        if (
            scope_ref != "runtime:telemetry"
            or capability != "opentelemetry_export"
            or decision not in {"granted", "revoked"}
            or set(scope)
            != {"schema_ref", "provider", "endpoint", "credential_ref"}
            or scope.get("schema_ref")
            != "meta-research/opentelemetry-export-scope/v1"
            or scope.get("provider") != "otlp_http"
            or scope.get("credential_ref") is not None
            or not isinstance(scope.get("endpoint"), str)
        ):
            raise OwnerConflict("telemetry_authorization_scope_invalid")
        try:
            validate_otlp_http_endpoint(cast(str, scope["endpoint"]))
        except ValueError as error:
            raise OwnerConflict("telemetry_authorization_scope_invalid") from error

    def apply_telemetry_authorization(
        self, authorization: dict[str, object]
    ) -> None:
        """Apply only a current Human Collaboration authorization receipt."""

        capability = authorization.get("capability")
        decision = authorization.get("decision")
        requirement = authorization.get("requirement")
        receipt_ref = authorization.get("receipt_ref")
        if (
            authorization.get("authorization_kind") != "capability"
            or not isinstance(requirement, dict)
            or not isinstance(receipt_ref, str)
            or not isinstance(capability, str)
            or not isinstance(decision, str)
            or authorization.get("scope_ref") != "runtime:telemetry"
            or authorization.get("is_current") is False
        ):
            raise OwnerConflict("telemetry_authorization_receipt_invalid")
        scope = requirement.get("scope")
        if not isinstance(scope, dict):
            raise OwnerConflict("telemetry_authorization_receipt_invalid")
        self._validate_telemetry_authorization_scope(
            scope_ref="runtime:telemetry",
            capability=capability,
            decision=decision,
            scope=scope,
        )
        self.owners.human_collaboration.verify_capability_authorization(
            requirement=requirement,
            receipt_ref=receipt_ref,
            _expected_decision=decision,
        )
        if decision == "granted":
            exporter = self._telemetry_exporter_factory(cast(str, scope["endpoint"]))
            self.runtime_protection.enable_telemetry(
                exporter,
                authorization_ref=receipt_ref,
            )
            return
        self.runtime_protection.revoke_telemetry(
            authorization_ref=receipt_ref,
        )

    def reconcile_telemetry_authorization(self) -> None:
        """Restore a current explicit grant after a daemon incarnation change."""

        projection = self.owners.human_collaboration.query_collaboration_projection(
            ("runtime:telemetry",)
        )
        authorizations = [
            authorization
            for authorization in projection["authorizations"]
            if authorization.get("authorization_kind") == "capability"
            and authorization.get("capability") == "opentelemetry_export"
            and authorization.get("scope_ref") == "runtime:telemetry"
            and authorization.get("is_current") is not False
        ]
        if len(authorizations) > 1:
            raise OwnerConflict("telemetry_authorization_head_invalid")
        if authorizations:
            self.apply_telemetry_authorization(authorizations[0])

    def close(self) -> None:
        try:
            self.request_stop()
        finally:
            try:
                self.runtime_protection.close()
            finally:
                self._database.close()


class _AgentRuntimeAcquisitionClient(DeepFetchAcquisitionClient):
    def __init__(
        self,
        agent_runtime: AgentRuntimeInterface,
        provider: AcquisitionProvider,
    ) -> None:
        self._agent_runtime = agent_runtime
        self._provider = provider

    def acquire(
        self,
        session_ref: str,
        request: AcquisitionBatchRequest,
    ) -> AcquisitionBatchExecution:
        return self._agent_runtime.acquire_literature(
            session_ref,
            request,
            self._provider,
        )

    def query_completed_batch(
        self,
        session_ref: str,
        request_id: str,
    ) -> AcquisitionBatchExecution | None:
        return self._agent_runtime.query_acquisition_execution(
            session_ref,
            request_id,
        )


def _configured_power_inhibitor(data_root: DataRoot) -> PowerInhibitor:
    if os.environ.get("META_RESEARCH_ASSUME_ALWAYS_ON") == "1":
        return OperatorAttestedPowerInhibitor()
    return ProductionPowerInhibitor(data_root.run / "power-inhibitor")


def build_production_runtime(
    data_root: DataRoot,
    *,
    proposal_drafter: ProposalDrafter | None = None,
    intent_drafting_provider: IntentDraftingProvider | None = None,
    host_compute_probe: HostComputeProbe | None = None,
    idea_skill_provider: IdeaSkillProvider | None = None,
    plan_skill_provider: PlanSkillProvider | None = None,
    bundle_skill_provider: BundleSkillProvider | None = None,
    reasoning_skill_provider: ReasoningSkillProvider | None = None,
    target_commit_evidence_authority: TargetCommitEvidenceAuthority | None = None,
    deepfetch_provider: DeepFetchProvider | None = None,
    acquisition_provider: AcquisitionProvider | None = None,
    experiment_provider: ExperimentProvider | None = None,
    writing_skill_provider: WritingSkillProvider | None = None,
    writing_delivery_provider_registry: WritingDeliveryProviderRegistry | None = None,
    writing_renderer_registry: WritingRendererRegistry | None = None,
    harness_adapters: tuple[HarnessAdapter, ...] | None = None,
    target_root_timeout_seconds: float = TARGET_ROOT_DEFAULT_TIMEOUT_SECONDS,
    power_inhibitor: PowerInhibitor | None = None,
    startup_power_probe: bool | None = None,
    startup_harness_diagnostics: bool = True,
    telemetry_exporter_factory: Callable[[str], TelemetryExporter] | None = None,
) -> ProductionRuntime:
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    runtime_protection = RuntimeProtection(
        database=database,
        feed=feed,
        inhibitor=(
            power_inhibitor
            if power_inhibitor is not None
            else _configured_power_inhibitor(data_root)
        ),
        event_logger=RuntimeEventLogger(data_root.daemon_log),
        startup_probe=(
            power_inhibitor is None
            if startup_power_probe is None
            else startup_power_probe
        ),
    )
    with database.read() as connection:
        daemon_incarnation_ref = str(
            connection.execute(
                text(
                    "SELECT incarnation_ref FROM ar_runtime_instances WHERE "
                    "status = 'active' ORDER BY started_at DESC LIMIT 1"
                )
            ).scalar_one()
        )
    codex_executable = str(data_root.codex_cli_executable.absolute())
    codex_provider_runner = _CancellableProcessRunner(
        protected_environment=data_root.codex_environment
    )
    claude_provider_runner = _CancellableProcessRunner()
    codex_adapter = (
        CodexDraftingAdapter(
            data_root.root / "drafting-provider",
            executable=codex_executable,
            process_runner=codex_provider_runner,
        )
        if proposal_drafter is None or intent_drafting_provider is None
        else None
    )
    proposal_drafter = proposal_drafter or codex_adapter
    intent_drafting_provider = intent_drafting_provider or codex_adapter
    assert proposal_drafter is not None
    assert intent_drafting_provider is not None
    host_compute_probe = host_compute_probe or NvidiaSmiProbe()
    idea_skill_provider = idea_skill_provider or CodexIdeaSkillAdapter(
        data_root.root / "idea-skill-provider",
        executable=codex_executable,
        process_runner=codex_provider_runner,
    )
    plan_skill_provider = plan_skill_provider or CodexPlanSkillAdapter(
        data_root.root / "plan-skill-provider",
        executable=codex_executable,
        process_runner=codex_provider_runner,
    )
    bundle_skill_provider = bundle_skill_provider or CodexBundleSkillAdapter(
        data_root.root / "bundle-skill-provider",
        executable=codex_executable,
        process_runner=codex_provider_runner,
    )
    reasoning_skill_provider = (
        reasoning_skill_provider
        or CodexReasoningSkillAdapter(
            data_root.root / "reasoning-skill-provider",
            executable=codex_executable,
            process_runner=codex_provider_runner,
        )
    )
    acquisition_provider = acquisition_provider or NatureDownloaderAdapter()
    experiment_provider = experiment_provider or BuiltinMicroExperimentProvider(
        data_root.run / "experiment-provider"
    )
    writing_skill_provider = writing_skill_provider or CodexWritingSkillAdapter(
        data_root.root / "writing-skill-provider",
        executable=codex_executable,
        process_runner=codex_provider_runner,
    )

    host_compute_reader = create_host_compute_observation_reader(database)
    human_response_verifier = create_human_response_verifier(database)
    confirmation_verifier = create_bundle_confirmation_verifier(
        database=database,
        agent_runtime=host_compute_reader,
    )
    stage_request_receipts = create_advancement_engine_receipt_verifier(
        database=database
    )
    deepfetch_request_receipts = _DeepFetchRequestVerifierRouter(
        create_deepfetch_request_verifier(database=database)
    )
    attempt_receipts = create_agent_runtime_receipt_verifier(
        database=database,
        stage_request_verifier=stage_request_receipts,
        writing_authorization_verifier=human_response_verifier,
    )
    research_memory_receipts = create_research_memory_receipt_verifier(
        database=database,
        object_store=data_root.objects,
        execution_verifier=attempt_receipts,
        stage_request_verifier=stage_request_receipts,
    )
    manual_question_confirmations = create_manual_question_confirmation_verifier(
        database
    )
    research_graph_receipts = create_research_graph_receipt_verifier(
        database=database,
        confirmation_verifier=confirmation_verifier,
        content_verifier=research_memory_receipts,
        asset_verifier=research_memory_receipts,
        idea_content_verifier=research_memory_receipts,
        execution_verifier=attempt_receipts,
        stage_request_verifier=stage_request_receipts,
        manual_confirmation_verifier=manual_question_confirmations,
        plan_content_verifier=research_memory_receipts,
        reasoning_content_verifier=research_memory_receipts,
    )
    attempt_receipts.bind_bundle_report_evidence_verifier(
        research_graph_receipts
    )
    human_response_verifier.bind_quest_receipt_verifier(research_graph_receipts)
    agent_runtime = create_agent_runtime_interface(
        database=database,
        feed=feed,
        host_compute_probe=host_compute_probe,
        stage_request_verifier=stage_request_receipts,
        outcome_verifier=research_graph_receipts,
        formal_plan_verifier=research_graph_receipts,
        target_graph_verifier=research_graph_receipts,
        deepfetch_request_verifier=deepfetch_request_receipts,
        acquisition_private_root=data_root.run / "acquisition-sessions",
        human_response_verifier=human_response_verifier,
        experiment_binding_verifier=research_graph_receipts,
        bundle_report_evidence_verifier=research_graph_receipts,
        reasoning_outcome_verifier=research_graph_receipts,
        writing_delivery_provider_registry=writing_delivery_provider_registry,
        runtime_protection=runtime_protection,
    )
    deepfetch_provider = deepfetch_provider or CodexDeepFetchAdapter(
        data_root.root / "deepfetch-provider",
        executable=codex_executable,
        acquisition_client=_AgentRuntimeAcquisitionClient(
            agent_runtime,
            acquisition_provider,
        ),
        process_runner=codex_provider_runner,
        codex_home=data_root.codex_home.absolute(),
    )
    agent_runtime.bind_provider_quiescence_driver(
        idea_skill_provider,
        unit_kinds=("idea_primary", "idea_review"),
    )
    agent_runtime.bind_provider_quiescence_driver(
        plan_skill_provider,
        unit_kinds=("plan_primary", "plan_review"),
    )
    agent_runtime.bind_provider_quiescence_driver(
        bundle_skill_provider,
        unit_kinds=("bundle_primary", "bundle_review"),
    )
    agent_runtime.bind_provider_quiescence_driver(
        reasoning_skill_provider,
        unit_kinds=("reasoning_primary", "reasoning_review"),
    )
    agent_runtime.bind_provider_quiescence_driver(
        deepfetch_provider,
        unit_kinds=("deepfetch",),
    )
    agent_runtime.bind_provider_quiescence_driver(
        experiment_provider,
        unit_kinds=("experiment",),
    )
    agent_runtime.bind_provider_quiescence_driver(
        writing_skill_provider,
        unit_kinds=("writing_primary", "writing_review"),
    )
    research_graph = create_research_graph_interface(
        database=database,
        feed=feed,
        confirmation_verifier=confirmation_verifier,
        content_verifier=research_memory_receipts,
        asset_verifier=research_memory_receipts,
        receipt_verifier=research_graph_receipts,
        idea_content_verifier=research_memory_receipts,
        execution_verifier=attempt_receipts,
        stage_request_verifier=stage_request_receipts,
        manual_confirmation_verifier=manual_question_confirmations,
        human_response_verifier=human_response_verifier,
        plan_content_verifier=research_memory_receipts,
        runtime_control_verifier=attempt_receipts,
        reasoning_content_verifier=research_memory_receipts,
    )
    agent_runtime.bind_writing_citation_verifier(research_graph_receipts)
    research_memory = create_research_memory_interface(
        database=database,
        object_store=data_root.objects,
        feed=feed,
        confirmation_verifier=confirmation_verifier,
        quest_verifier=research_graph_receipts,
        receipt_verifier=research_memory_receipts,
        execution_verifier=attempt_receipts,
        reference_reader=research_graph,
        manual_confirmation_verifier=manual_question_confirmations,
        human_response_verifier=human_response_verifier,
        stage_request_verifier=stage_request_receipts,
    )
    target_run_memory = SQLiteTargetRunMemoryAuthority(
        database,
        feed,
        research_memory,
    )
    target_run_graph = SQLiteTargetRunGraphAuthority(
        database,
        feed,
        research_graph_receipts,
        target_run_memory,
        research_graph,
        agent_runtime,
    )
    target_run_memory.bind_implementation_bundle_revision_verifier(
        target_run_graph
    )
    research_graph_receipts.bind_target_input_asset_proof_reader(
        target_run_graph
    )
    research_graph.bind_target_formal_plan_projection_verifier(
        target_run_graph
    )
    research_graph.bind_target_candidate_proof_verifier(
        BundleTargetCandidateOwnerProofVerifier(
            research_memory,
            research_graph,
        )
    )
    resolved_target_commit_evidence_authority = (
        target_commit_evidence_authority
        or TargetCommitEvidenceCatalog(research_graph, research_memory)
    )
    research_graph_receipts.bind_target_commit_evidence_authority(
        resolved_target_commit_evidence_authority
    )
    research_memory_receipts.bind_plan_evidence_reuse_verifier(
        research_graph_receipts
    )
    manual_question_confirmations.bind_research_memory_verifier(research_memory)
    agent_runtime.bind_research_material_resolver(research_memory)
    confirmation_verifier.bind_literature_snapshot_verifier(research_memory)
    advancement_engine = create_advancement_engine_interface(
        database=database,
        feed=feed,
        quest_verifier=research_graph_receipts,
        question_verifier=research_graph_receipts,
        accepted_question_verifier=research_graph_receipts,
        evidence_verifier=research_graph_receipts,
        run_completion_verifier=attempt_receipts,
        outcome_verifier=research_graph_receipts,
        formal_plan_verifier=research_graph_receipts,
        accepted_formal_plan_verifier=research_graph_receipts,
        target_graph_verifier=research_graph_receipts,
        target_commit_verifier=research_graph_receipts,
        literature_snapshot_verifier=research_memory,
        human_response_verifier=human_response_verifier,
        runtime_control_verifier=attempt_receipts,
        question_control_verifier=research_graph_receipts,
        stage_disposition_basis_verifier=research_graph_receipts,
        current_question_verifier=research_graph_receipts,
        bundle_report_verifier=attempt_receipts,
        bundle_report_evidence_verifier=research_graph_receipts,
        reasoning_outcome_verifier=research_graph_receipts,
        question_literature_revision_verifier=research_memory_receipts,
    )
    deepfetch_request_receipts.bind_advancement_engine(advancement_engine)
    bind_dispatch_verifier = getattr(
        research_graph, "bind_autonomous_question_dispatch_verifier", None
    )
    if callable(bind_dispatch_verifier):
        bind_dispatch_verifier(advancement_engine)
    attempt_receipts.bind_bundle_exhaustion_verifier(advancement_engine)
    attempt_receipts.bind_bundle_report_disposition_verifier(
        advancement_engine
    )
    agent_runtime.bind_bundle_exhaustion_verifier(advancement_engine)
    agent_runtime.bind_bundle_report_disposition_verifier(advancement_engine)
    human_collaboration = create_human_collaboration_interface(
        database=database,
        feed=feed,
        research_graph=research_graph,
        research_memory=research_memory,
        advancement_engine=advancement_engine,
        agent_runtime=agent_runtime,
        proposal_drafter=proposal_drafter,
        intent_drafting_provider=intent_drafting_provider,
        acquisition_provider=acquisition_provider,
        runtime_protection=runtime_protection,
    )
    bundle_exhaustion_owner_proofs = BundleExhaustionOwnerProofVerifier(
        agent_runtime,
        research_graph,
        research_memory,
        human_collaboration,
    )
    advancement_engine.bind_bundle_exhaustion_evidence_verifier(
        bundle_exhaustion_owner_proofs
    )
    owners = OwnerInterfaces(
        research_graph=research_graph,
        advancement_engine=advancement_engine,
        research_memory=research_memory,
        agent_runtime=agent_runtime,
        human_collaboration=human_collaboration,
    )
    autonomous_creation = AutonomousCreationService(
        human_collaboration,
        advancement_engine,
        agent_runtime,
        research_memory,
        research_graph,
        acquisition_provider,
    )
    quest_completion = QuestCompletionService(
        human_collaboration,
        research_graph,
        advancement_engine,
    )
    target_run_agent = SQLiteTargetRunAgentAuthority(
        database,
        feed,
        owners.agent_runtime.harness_runs,
        target_run_graph,
        target_run_memory,
        research_graph,
        agent_runtime,
        workspace_root=data_root.run / "target-workspaces",
    )
    target_root_lifecycle = SQLiteTargetRootLifecycleAuthority(
        database,
        feed,
        target_run_agent,
    )
    target_root_memory = SQLiteTargetRootCompletionMemoryAuthority(
        database,
        feed,
        research_memory,
        target_root_lifecycle,
    )
    owners.agent_runtime.bind_target_root_completion_reader(
        target_root_lifecycle
    )
    owners.research_graph.bind_target_root_completion_readers(
        completion_reader=target_root_lifecycle,
        manifest_reader=target_root_memory,
    )
    owners.agent_runtime.bind_target_run_harness_verifier(target_run_agent)
    owners.research_graph.bind_target_execution_closure_verifier(
        target_run_agent
    )
    semantic_gateway = create_semantic_owner_gateway(
        research_graph=owners.research_graph,
        advancement_engine=owners.advancement_engine,
        research_memory=owners.research_memory,
        agent_runtime=owners.agent_runtime,
        human_collaboration_snapshot=owners.human_collaboration.query_snapshot,
        target_run_agent=target_run_agent,
    )
    harness_operation_canceller = None
    if harness_adapters is None:
        codex_harness_transport = HarnessSupervisorTransport(
            data_root.run / "harness-supervisor",
            process_runner=codex_provider_runner,
            event_sink=owners.agent_runtime.harness_runs.append_target_root_events,
        )
        claude_harness_transport = HarnessSupervisorTransport(
            data_root.run / "harness-supervisor",
            process_runner=claude_provider_runner,
            event_sink=owners.agent_runtime.harness_runs.append_target_root_events,
        )
        harness_adapters = (
            CodexHarnessAdapter(
                data_root.run / "harness",
                executable=codex_executable,
                runner=codex_harness_transport,
                codex_home=data_root.codex_home.absolute(),
                target_root_timeout_seconds=target_root_timeout_seconds,
            ),
            ClaudeHarnessAdapter(
                data_root.run / "harness",
                runner=claude_harness_transport,
                target_root_timeout_seconds=target_root_timeout_seconds,
            ),
        )
        harness_operation_canceller = DurableHarnessOperationCanceller(
            data_root.run / "harness-supervisor"
        )
    harnesses = HarnessRuntime(
        owners.agent_runtime.harness_runs,
        semantic_gateway,
        harness_adapters,
        operation_canceller=harness_operation_canceller,
        runtime_protection=runtime_protection,
        runtime_boundary_recorder=RuntimeBoundaryRecorder(database),
        daemon_incarnation_ref=daemon_incarnation_ref,
    )
    harnesses.bind_resident_mcp_scope_verifier(owners.agent_runtime)
    harnesses.bind_target_workspace_resolver(target_run_agent)
    if startup_harness_diagnostics:
        harnesses.run_startup_diagnostics()
    target_run_finalizer = TargetRunFinalizer(
        lifecycle=target_root_lifecycle,
        memory=target_root_memory,
        workspace_resolver=target_run_agent,
        evidence_reader=harnesses,
        measurement_authority=owners.research_graph,
        graph_authority=owners.research_graph,
    )
    bind_bundle_conformance = getattr(
        bundle_skill_provider, "bind_full_conformance_authority", None
    )
    if callable(bind_bundle_conformance):
        bind_bundle_conformance(harnesses)
    bind_reasoning_conformance = getattr(
        reasoning_skill_provider, "bind_full_conformance_authority", None
    )
    if callable(bind_reasoning_conformance):
        bind_reasoning_conformance(harnesses)
    verify_bundle_exhaustion_trace = getattr(
        bundle_skill_provider,
        "verify_bundle_exhaustion_review_trace",
        None,
    )
    if callable(verify_bundle_exhaustion_trace):
        owners.agent_runtime.bind_bundle_exhaustion_review_trace_verifier(
            bundle_skill_provider
        )
    idea_stage = IdeaStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        idea_skill_provider,
        owners.human_collaboration,
    )
    plan_stage = PlanStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        plan_skill_provider,
    )
    reasoning_stage = ReasoningStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        reasoning_skill_provider,
        autonomous_creation=autonomous_creation,
    )
    deepfetch = FirstQuestionDeepFetchWorker(
        _DeepFetchRequestAuthorityRouter(
            human_collaboration,
            advancement_engine,
        ),
        agent_runtime,
        research_memory,
        deepfetch_provider,
    )
    experiment = ExperimentService(
        research_graph,
        agent_runtime,
        research_memory,
        experiment_provider,
    )
    target_run_runtime = TargetRunRuntime(
        agent_runtime=owners.agent_runtime,
        research_graph=owners.research_graph,
        target_graph=target_run_graph,
        target_agent=target_run_agent,
        target_root_lifecycle=target_root_lifecycle,
        harnesses=harnesses,
        finalizer=target_run_finalizer,
    )
    bundle_stage = BundleStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        bundle_skill_provider,
        owners.human_collaboration,
        harnesses,
    )
    writing = WritingReportService(
        research_graph,
        advancement_engine,
        research_memory,
        agent_runtime,
        human_collaboration,
        writing_skill_provider,
        writing_renderer_registry,
    )
    human_collaboration.bind_writing_delivery_binding_validator(
        writing.verify_writing_delivery_binding
    )
    agent_runtime.writing_delivery.bind_binding_verifier(writing)
    projection = PublicProjection(
        feed,
        data_root.objects,
        owners.research_graph,
        owners.advancement_engine,
        owners.research_memory,
        owners.agent_runtime,
        owners.human_collaboration,
        idea_stage=idea_stage,
        plan_stage=plan_stage,
        bundle_stage=bundle_stage,
        reasoning_stage=reasoning_stage,
        autonomous_creation=autonomous_creation,
        quest_completion=quest_completion,
        experiment=experiment,
        writing=writing,
        harnesses=harnesses,
    )
    provider_lifecycles: list[object] = []
    for provider in (
        codex_provider_runner,
        claude_provider_runner,
        proposal_drafter,
        intent_drafting_provider,
        idea_skill_provider,
        plan_skill_provider,
        bundle_skill_provider,
        reasoning_skill_provider,
        deepfetch_provider,
        acquisition_provider,
        experiment_provider,
        writing_skill_provider,
    ):
        if callable(getattr(provider, "request_stop", None)) and not any(
            provider is lifecycle for lifecycle in provider_lifecycles
        ):
            provider_lifecycles.append(provider)
    runtime = ProductionRuntime(
        data_root=data_root,
        owners=owners,
        authentication=Authentication(database),
        feed=feed,
        projection=projection,
        idea_stage=idea_stage,
        plan_stage=plan_stage,
        bundle_stage=bundle_stage,
        reasoning_stage=reasoning_stage,
        autonomous_creation=autonomous_creation,
        quest_completion=quest_completion,
        deepfetch=deepfetch,
        experiment=experiment,
        writing=writing,
        harnesses=harnesses,
        target_run_authorities=TargetRunAuthorities(
            research_memory=target_run_memory,
            research_graph=target_run_graph,
            agent_runtime=target_run_agent,
        ),
        target_run_runtime=target_run_runtime,
        target_root_lifecycle=target_root_lifecycle,
        target_run_finalizer=target_run_finalizer,
        target_root_readiness={
            "name": "target_root_lifecycle",
            "status": "ready",
        },
        runtime_protection=runtime_protection,
        _database=database,
        _telemetry_exporter_factory=(
            telemetry_exporter_factory
            or (lambda endpoint: OtlpHttpTelemetryExporter(endpoint=endpoint))
        ),
        _provider_lifecycles=tuple(provider_lifecycles),
    )
    runtime.reconcile_telemetry_authorization()
    return runtime
