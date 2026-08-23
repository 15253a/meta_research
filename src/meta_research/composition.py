from __future__ import annotations

from dataclasses import dataclass

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionProvider,
    NatureDownloaderAdapter,
)
from meta_research.auth import Authentication
from meta_research.bundle_stage import BundleStageWorker
from meta_research.bundle_skill import CodexBundleSkillAdapter, BundleSkillProvider
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
from meta_research.harness_adapters import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    HarnessAdapter,
    HarnessSupervisorTransport,
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
from meta_research.writing import WritingReportService
from meta_research.writing_skill import CodexWritingSkillAdapter, WritingSkillProvider
from meta_research.semantic_owner_gateway import create_semantic_owner_gateway


@dataclass(frozen=True)
class OwnerInterfaces:
    research_graph: ResearchGraphInterface
    advancement_engine: AdvancementEngineInterface
    research_memory: ResearchMemoryInterface
    agent_runtime: AgentRuntimeInterface
    human_collaboration: HumanCollaborationInterface


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
    harness_adapters: tuple[HarnessAdapter, ...] | None = None,
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
    )
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
    owners = OwnerInterfaces(
        research_graph=research_graph,
        advancement_engine=advancement_engine,
        research_memory=research_memory,
        agent_runtime=agent_runtime,
        human_collaboration=human_collaboration,
    )
    semantic_gateway = create_semantic_owner_gateway(
        research_graph=owners.research_graph,
        advancement_engine_snapshot=owners.advancement_engine.query_snapshot,
        research_memory_snapshot=owners.research_memory.query_snapshot,
        agent_runtime=owners.agent_runtime,
        human_collaboration_snapshot=owners.human_collaboration.query_snapshot,
    )
    if harness_adapters is None:
        shared_harness_transport = HarnessSupervisorTransport(
            data_root.run / "harness-supervisor",
            process_runner=shared_provider_runner,
        )
        harness_adapters = (
            CodexHarnessAdapter(
                data_root.run / "harness", runner=shared_harness_transport
            ),
            ClaudeHarnessAdapter(
                data_root.run / "harness", runner=shared_harness_transport
            ),
        )
    harnesses = HarnessRuntime(
        owners.agent_runtime.harness_runs,
        semantic_gateway,
        harness_adapters,
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
    bundle_stage = BundleStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        bundle_skill_provider,
        experiment,
        owners.human_collaboration,
    )
    writing = WritingReportService(
        research_graph,
        advancement_engine,
        research_memory,
        agent_runtime,
        human_collaboration,
        writing_skill_provider,
    )
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
        _database=database,
        _provider_lifecycles=tuple(provider_lifecycles),
    )
