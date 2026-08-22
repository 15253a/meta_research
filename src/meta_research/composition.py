from __future__ import annotations

from dataclasses import dataclass

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionProvider,
    NatureDownloaderAdapter,
)
from meta_research.auth import Authentication
from meta_research.database import Database
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DeepFetchAcquisitionClient,
    DeepFetchProvider,
)
from meta_research.feed import DurableFeed
from meta_research.first_question_deepfetch import FirstQuestionDeepFetchWorker
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
)
from meta_research.owners.research_graph import (
    ResearchGraphInterface,
    create_research_graph_interface,
    create_research_graph_receipt_verifier,
)
from meta_research.owners.research_memory import (
    ResearchMemoryInterface,
    create_research_memory_interface,
    create_research_memory_receipt_verifier,
)
from meta_research.paths import DataRoot
from meta_research.projection import PublicProjection
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    HostComputeProbe,
    IntentDraftingProvider,
    NvidiaSmiProbe,
    ProposalDrafter,
)


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
    deepfetch: FirstQuestionDeepFetchWorker
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
    deepfetch_provider: DeepFetchProvider | None = None,
    acquisition_provider: AcquisitionProvider | None = None,
) -> ProductionRuntime:
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
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
        data_root.root / "idea-skill-provider"
    )
    acquisition_provider = acquisition_provider or NatureDownloaderAdapter()

    host_compute_reader = create_host_compute_observation_reader(database)
    confirmation_verifier = create_bundle_confirmation_verifier(
        database, host_compute_reader
    )
    stage_request_receipts = create_advancement_engine_receipt_verifier(database)
    deepfetch_request_receipts = create_deepfetch_request_verifier(database)
    attempt_receipts = create_agent_runtime_receipt_verifier(
        database, stage_request_receipts
    )
    research_memory_receipts = create_research_memory_receipt_verifier(
        database, data_root.objects, attempt_receipts
    )
    manual_question_confirmations = create_manual_question_confirmation_verifier(
        database
    )
    research_graph_receipts = create_research_graph_receipt_verifier(
        database,
        confirmation_verifier,
        research_memory_receipts,
        research_memory_receipts,
        research_memory_receipts,
        attempt_receipts,
        stage_request_receipts,
        manual_confirmation_verifier=manual_question_confirmations,
    )
    agent_runtime = create_agent_runtime_interface(
        database,
        feed,
        host_compute_probe,
        stage_request_receipts,
        research_graph_receipts,
        deepfetch_request_receipts,
        data_root.run / "acquisition-sessions",
    )
    deepfetch_provider = deepfetch_provider or CodexDeepFetchAdapter(
        data_root.root / "deepfetch-provider",
        acquisition_client=_AgentRuntimeAcquisitionClient(
            agent_runtime,
            acquisition_provider,
        ),
    )
    research_graph = create_research_graph_interface(
        database,
        feed,
        confirmation_verifier,
        research_memory_receipts,
        research_memory_receipts,
        research_graph_receipts,
        research_memory_receipts,
        attempt_receipts,
        stage_request_receipts,
        manual_confirmation_verifier=manual_question_confirmations,
    )
    research_memory = create_research_memory_interface(
        database,
        data_root.objects,
        feed,
        confirmation_verifier,
        research_graph_receipts,
        research_memory_receipts,
        attempt_receipts,
        research_graph,
        manual_question_confirmations,
    )
    manual_question_confirmations.bind_research_memory_verifier(research_memory)
    confirmation_verifier.bind_literature_snapshot_verifier(research_memory)
    advancement_engine = create_advancement_engine_interface(
        database,
        feed,
        research_graph_receipts,
        research_graph_receipts,
        research_graph_receipts,
        research_graph_receipts,
        attempt_receipts,
        research_graph_receipts,
        research_memory,
    )
    human_collaboration = create_human_collaboration_interface(
        database,
        feed,
        research_graph,
        research_memory,
        advancement_engine,
        agent_runtime,
        proposal_drafter,
        intent_drafting_provider,
        acquisition_provider,
    )
    owners = OwnerInterfaces(
        research_graph=research_graph,
        advancement_engine=advancement_engine,
        research_memory=research_memory,
        agent_runtime=agent_runtime,
        human_collaboration=human_collaboration,
    )
    idea_stage = IdeaStageWorker(
        feed,
        owners.advancement_engine,
        owners.agent_runtime,
        owners.research_memory,
        owners.research_graph,
        idea_skill_provider,
    )
    deepfetch = FirstQuestionDeepFetchWorker(
        human_collaboration,
        agent_runtime,
        research_memory,
        deepfetch_provider,
    )
    projection = PublicProjection(
        feed,
        data_root.objects,
        owners.research_graph,
        owners.advancement_engine,
        owners.research_memory,
        owners.agent_runtime,
        owners.human_collaboration,
        idea_stage,
    )
    provider_lifecycles: list[object] = []
    for provider in (
        proposal_drafter,
        intent_drafting_provider,
        idea_skill_provider,
        deepfetch_provider,
        acquisition_provider,
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
        deepfetch=deepfetch,
        _database=database,
        _provider_lifecycles=tuple(provider_lifecycles),
    )
