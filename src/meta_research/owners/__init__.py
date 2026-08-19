"""The five authoritative vNext Owner Modules."""

from meta_research.owners.advancement_engine import AdvancementEngineInterface
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import ResearchMemoryInterface

__all__ = [
    "AdvancementEngineInterface",
    "AgentRuntimeInterface",
    "HumanCollaborationInterface",
    "ResearchGraphInterface",
    "ResearchMemoryInterface",
]
