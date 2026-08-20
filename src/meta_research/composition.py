from __future__ import annotations

from dataclasses import dataclass

from meta_research.auth import Authentication
from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.advancement_engine import (
    AdvancementEngineInterface,
    create_advancement_engine_interface,
)
from meta_research.owners.agent_runtime import (
    AgentRuntimeInterface,
    create_agent_runtime_interface,
)
from meta_research.owners.human_collaboration import (
    HumanCollaborationInterface,
    create_bundle_confirmation_verifier,
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
    _database: Database

    def close(self) -> None:
        self._database.close()


def build_production_runtime(data_root: DataRoot) -> ProductionRuntime:
    upgrade_database(data_root.database)
    database = Database(data_root.database)
    feed = DurableFeed(database)
    feed.ensure_initialized()
    confirmation_verifier = create_bundle_confirmation_verifier(database)
    research_memory_receipts = create_research_memory_receipt_verifier(
        database, data_root.objects
    )
    research_graph_receipts = create_research_graph_receipt_verifier(
        database, confirmation_verifier, research_memory_receipts
    )
    research_graph = create_research_graph_interface(
        database,
        feed,
        confirmation_verifier,
        research_memory_receipts,
        research_graph_receipts,
    )
    research_memory = create_research_memory_interface(
        database,
        data_root.objects,
        feed,
        confirmation_verifier,
        research_graph_receipts,
        research_memory_receipts,
    )
    advancement_engine = create_advancement_engine_interface(
        database,
        feed,
        research_graph_receipts,
        research_graph_receipts,
    )
    owners = OwnerInterfaces(
        research_graph=research_graph,
        advancement_engine=advancement_engine,
        research_memory=research_memory,
        agent_runtime=create_agent_runtime_interface(database),
        human_collaboration=create_human_collaboration_interface(
            database,
            feed,
            research_graph,
            research_memory,
            advancement_engine,
        ),
    )
    projection = PublicProjection(
        feed,
        data_root.objects,
        owners.research_graph,
        owners.advancement_engine,
        owners.research_memory,
        owners.agent_runtime,
        owners.human_collaboration,
    )
    return ProductionRuntime(
        data_root=data_root,
        owners=owners,
        authentication=Authentication(database),
        feed=feed,
        projection=projection,
        _database=database,
    )
