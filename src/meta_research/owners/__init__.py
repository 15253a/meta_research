"""The five authoritative vNext Owner Modules.

Interfaces are loaded lazily so owner-neutral contract modules can depend on the
shared receipt vocabulary without importing every Owner implementation.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

_INTERFACE_MODULES = {
    "AdvancementEngineInterface": "advancement_engine",
    "AgentRuntimeInterface": "agent_runtime",
    "HumanCollaborationInterface": "human_collaboration",
    "ResearchGraphInterface": "research_graph",
    "ResearchMemoryInterface": "research_memory",
}


def __getattr__(name: str):
    module_name = _INTERFACE_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(f"meta_research.owners.{module_name}"), name)
