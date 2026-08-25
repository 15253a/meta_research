from __future__ import annotations

from dataclasses import dataclass

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionProvider,
    NatureDownloaderAdapter,
)
from meta_research.auth import Authentication
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
    deepfetch: FirstQuestionDeepFetchWorker
    experiment: ExperimentService
    writing: WritingReportService
    harnesses: HarnessRuntime
    target_run_authorities: TargetRunAuthorities
    target_run_runtime: TargetRunRuntime
    target_root_lifecycle: SQLiteTargetRootLifecycleAuthority
    target_run_finalizer: TargetRunFinalizer
    target_root_readiness: dict[str, object]
    _database: Database
    _provider_lifecycles: tuple[object, ...] = ()
    _stop_requested: bool = False

    def request_stop(self) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        for lifecycle in self._provider_lifecycles:
            request_stop = getattr(lifecycle, "request_stop", None)
            if callable(request_stop):
                request_stop()

    def query_target_root_readiness(self) -> dict[str, object]:
        return dict(self.target_root_readiness)

    def close(self) -> None:
        try:
            self.request_stop()
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


def build_production_runtime(
    data_root: DataRoot,
    *,
    proposal_drafter: ProposalDrafter | None = None,
    intent_drafting_provider: IntentDraftingProvider | None = None,
    host_compute_probe: HostComputeProbe | None = None,
    idea_skill_provider: IdeaSkillProvider | None = None,
    plan_skill_provider: PlanSkillProvider | None = None,
    bundle_skill_provider: BundleSkillProvider | None = None,
    target_commit_evidence_authority: TargetCommitEvidenceAuthority | None = None,
    deepfetch_provider: DeepFetchProvider | None = None,
    acquisition_provider: AcquisitionProvider | None = None,
    experiment_provider: ExperimentProvider | None = None,
    writing_skill_provider: WritingSkillProvider | None = None,
    writing_delivery_provider_registry: WritingDeliveryProviderRegistry | None = None,
    writing_renderer_registry: WritingRendererRegistry | None = None,
    harness_adapters: tuple[HarnessAdapter, ...] | None = None,
    target_root_timeout_seconds: float = TARGET_ROOT_DEFAULT_TIMEOUT_SECONDS,
) -> ProductionRuntime:
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    shared_provider_runner = _CancellableProcessRunner()
    codex_adapter = (
        CodexDraftingAdapter(data_root.root / "drafting-provider")
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
        process_runner=shared_provider_runner,
    )
    plan_skill_provider = plan_skill_provider or CodexPlanSkillAdapter(
        data_root.root / "plan-skill-provider",
        process_runner=shared_provider_runner,
    )
    bundle_skill_provider = bundle_skill_provider or CodexBundleSkillAdapter(
        data_root.root / "bundle-skill-provider",
        process_runner=shared_provider_runner,
    )
    acquisition_provider = acquisition_provider or NatureDownloaderAdapter()
    experiment_provider = experiment_provider or BuiltinMicroExperimentProvider(
        data_root.run / "experiment-provider"
    )
    writing_skill_provider = writing_skill_provider or CodexWritingSkillAdapter(
        data_root.root / "writing-skill-provider",
        process_runner=shared_provider_runner,
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
    deepfetch_request_receipts = create_deepfetch_request_verifier(database=database)
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
        writing_delivery_provider_registry=writing_delivery_provider_registry,
    )
    deepfetch_provider = deepfetch_provider or CodexDeepFetchAdapter(
        data_root.root / "deepfetch-provider",
        acquisition_client=_AgentRuntimeAcquisitionClient(
            agent_runtime,
            acquisition_provider,
        ),
        process_runner=shared_provider_runner,
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
    research_graph_receipts.bind_target_commit_evidence_authority(
        target_commit_evidence_authority
        or TargetCommitEvidenceCatalog(research_graph, research_memory)
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
    )
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
        shared_harness_transport = HarnessSupervisorTransport(
            data_root.run / "harness-supervisor",
            process_runner=shared_provider_runner,
            event_sink=owners.agent_runtime.harness_runs.append_target_root_events,
        )
        harness_adapters = (
            CodexHarnessAdapter(
                data_root.run / "harness",
                runner=shared_harness_transport,
                target_root_timeout_seconds=target_root_timeout_seconds,
            ),
            ClaudeHarnessAdapter(
                data_root.run / "harness",
                runner=shared_harness_transport,
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
    )
    harnesses.bind_resident_mcp_scope_verifier(owners.agent_runtime)
    harnesses.bind_target_workspace_resolver(target_run_agent)
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
    deepfetch = FirstQuestionDeepFetchWorker(
        human_collaboration,
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
        experiment=experiment,
        writing=writing,
        harnesses=harnesses,
    )
    provider_lifecycles: list[object] = []
    for provider in (
        shared_provider_runner,
        proposal_drafter,
        intent_drafting_provider,
        idea_skill_provider,
        plan_skill_provider,
        bundle_skill_provider,
        deepfetch_provider,
        acquisition_provider,
        experiment_provider,
        writing_skill_provider,
    ):
        if callable(getattr(provider, "request_stop", None)) and not any(
            provider is lifecycle for lifecycle in provider_lifecycles
        ):
            provider_lifecycles.append(provider)
    return ProductionRuntime(
        data_root=data_root,
        owners=owners,
        authentication=Authentication(database),
        feed=feed,
        projection=projection,
        idea_stage=idea_stage,
        plan_stage=plan_stage,
        bundle_stage=bundle_stage,
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
        _database=database,
        _provider_lifecycles=tuple(provider_lifecycles),
    )
