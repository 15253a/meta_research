from __future__ import annotations

import pytest

from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_operation_catalog
from meta_research.semantic_mcp import ROOT_AGENT_COMMON_OPERATION_IDS
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    TARGET_ROOT_SEMANTIC_OPERATION_IDS,
)


@pytest.mark.parametrize("root_kind", ("companion", "idea", "plan", "bundle", "target", "reasoning"))
def test_assistant_and_cycle_roots_can_discover_and_read_exact_materials(root_kind):
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    assert {"tools.catalog.page", "tools.contracts.read", "research_memory.assets.page",
        "research_memory.assets.lifecycle", "research_memory.content.read",
        "research_memory.creation_basis.read", "assistant.content.generate"} <= set(catalog)
    assert not {"research_workspace.discover", "research_workspace.read"} & set(catalog)
    assert len(catalog) == len(set(catalog))


def test_companion_reads_research_without_gaining_research_mutations():
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"]
    assert {"research_graph.questions.page", "research_graph.formal_results.read",
        "research_graph.question_history.read", "human_request.read"} <= set(catalog)
    assert not {"agent_runtime.acquisition.request", "human_request.open",
        "research_memory.assets.intake", "research_memory.assets.retire",
        "research_graph.datasets.register", "research_graph.datasets.derive",
        "research_graph.environments.register", "research_graph.artifact_roles.adjust",
        "research_graph.quest_goal.evolve", "human_guidance.feedback",
        "agent_runtime.target_work.request", "research_graph.question_relations.record"} & set(catalog)


@pytest.mark.parametrize("root_kind", ("idea", "plan", "bundle", "target", "reasoning"))
def test_cycle_roots_keep_shared_guidance_goal_and_evidence_operations(root_kind):
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    assert {"human_guidance.read", "human_guidance.feedback", "research_graph.quest_goal.read",
        "research_graph.quest_goal.evolve", "research_memory.research_notes.read",
        "research_graph.datasets.register", "agent_runtime.acquisition.request",
        "human_request.open"} <= set(catalog)


@pytest.mark.parametrize(("root_kind", "required", "unrelated"), (
    ("idea", {"research_memory.stage_context.read"}, {"agent_runtime.target_work.request"}),
    ("plan", {"research_graph.plan_evidence.page", "research_memory.plan_evidence.read"},
        {"research_graph.question_relations.record"}),
    ("bundle", {"research_memory.implementation_content.accept", "agent_runtime.target_work.request",
        "advancement_engine.bundle_exhaustion.submit"}, {"agent_runtime.target_run.progress"}),
    ("target", {"agent_runtime.target_run.observe", "agent_runtime.target_run.progress"},
        {"research_memory.implementation_content.accept", "agent_runtime.target_work.request"}),
    ("reasoning", {"research_memory.reasoning_evidence.read", "research_graph.reasoning_context.read",
        "research_graph.question_relations.record"}, {"agent_runtime.target_work.request"}),
))
def test_role_grants_support_its_work_without_other_roles_mutations(root_kind, required, unrelated):
    catalog = set(ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind])
    assert required <= catalog
    assert not unrelated & catalog


def test_common_catalog_extension_keeps_companion_read_boundary():
    common = ("example.read", "example.submit")
    assert "example.read" in root_operation_catalog("companion", common_operation_ids=common)
    assert "example.submit" not in root_operation_catalog("companion", common_operation_ids=common)
    assert set(common) <= set(root_operation_catalog("idea", common_operation_ids=common))


def test_catalog_rejects_duplicate_or_overlapping_grants():
    with pytest.raises(ValueError, match="root_common_operation_catalog_invalid"):
        root_operation_catalog("idea", common_operation_ids=("human_request.open",) * 2)
    with pytest.raises(ValueError, match="root_operation_catalog_overlap"):
        root_operation_catalog("target", common_operation_ids=("agent_runtime.target_run.observe",))


def test_gateway_registers_the_live_role_catalogs():
    assert tuple(ROOT_AGENT_SEMANTIC_OPERATION_IDS) == ROOT_AGENT_KINDS
    for root_kind, catalog in ROOT_AGENT_SEMANTIC_OPERATION_IDS.items():
        assert catalog == root_operation_catalog(root_kind, common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS)
    assert BUNDLE_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["bundle"]
    assert TARGET_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["target"]
    assert REASONING_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["reasoning"]
