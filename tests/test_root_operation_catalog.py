from __future__ import annotations

import pytest

from meta_research.root_capabilities import (
    ALL_ROOT_ROLE_OPERATION_IDS,
    ROOT_AGENT_KINDS,
    root_operation_catalog,
)
from meta_research.semantic_mcp import ROOT_AGENT_COMMON_OPERATION_IDS
from meta_research.semantic_mcp import SemanticMcpGateway, SemanticOperation
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    TARGET_ROOT_SEMANTIC_OPERATION_IDS,
)


SHARED_RESEARCH_READ_OPERATION_IDS = {
    "tools.catalog.page",
    "tools.contracts.read",
    "research_graph.questions.page",
    "research_graph.baselines.page",
    "research_graph.baselines.read",
    "research_graph.formal_results.read",
    "research_graph.target_formal_results.read",
    "research_graph.question_history.read",
    "research_graph.question_relations.read",
    "research_memory.assets.page",
    "research_graph.literature.page",
    "research_memory.content.read",
}


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_every_root_gets_shared_rg_rm_discovery_and_read_surface(root_kind):
    catalog = set(ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind])
    assert SHARED_RESEARCH_READ_OPERATION_IDS <= catalog


@pytest.mark.parametrize("root_kind", ("companion", "idea", "plan", "bundle", "target", "reasoning"))
def test_assistant_and_cycle_roots_can_discover_and_read_exact_materials(root_kind):
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    assert {"tools.catalog.page", "tools.contracts.read", "research_memory.assets.page",
        "research_memory.assets.lifecycle", "research_memory.content.read",
        "research_memory.creation_basis.read", "assistant.content.generate"} <= set(catalog)
    assert not {"research_workspace.discover", "research_workspace.read"} & set(catalog)
    assert len(catalog) == len(set(catalog))


def test_companion_sees_research_contracts_while_formal_effects_remain_gated():
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"]
    assert {"research_graph.questions.page", "research_graph.formal_results.read",
        "research_graph.question_history.read", "human_request.read"} <= set(catalog)
    assert set(ALL_ROOT_ROLE_OPERATION_IDS) <= set(catalog)


def test_research_roots_share_the_complete_registered_role_catalog():
    expected = set(ROOT_AGENT_COMMON_OPERATION_IDS) | set(ALL_ROOT_ROLE_OPERATION_IDS)
    for root_kind in ("deepfetch", "acquisition", "companion", "idea", "plan", "bundle", "target", "reasoning"):
        assert set(ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]) == expected


@pytest.mark.parametrize("root_kind", ("idea", "plan", "bundle", "target", "reasoning"))
def test_cycle_roots_keep_shared_guidance_goal_and_evidence_operations(root_kind):
    catalog = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    assert {"human_guidance.read", "human_guidance.feedback", "research_graph.quest_goal.read",
        "research_graph.quest_goal.evolve", "research_memory.research_notes.read",
        "research_graph.datasets.register", "agent_runtime.acquisition.request",
        "human_request.open"} <= set(catalog)


@pytest.mark.parametrize(("root_kind", "required"), (
    ("idea", {"research_memory.stage_context.read", "agent_runtime.target_work.request"}),
    ("plan", {"research_graph.plan_evidence.page", "research_memory.plan_evidence.read",
        "research_graph.question_relations.record"}),
    ("bundle", {"research_memory.implementation_content.accept", "agent_runtime.target_work.request",
        "advancement_engine.bundle_exhaustion.submit", "agent_runtime.target_run.progress"}),
    ("target", {"agent_runtime.target_run.observe", "agent_runtime.target_run.progress",
        "research_memory.implementation_content.accept"}),
    ("reasoning", {"research_memory.reasoning_evidence.read", "research_graph.reasoning_context.read",
        "research_graph.question_relations.record", "agent_runtime.target_work.request"}),
))
def test_role_grants_expose_all_registered_operations(root_kind, required):
    catalog = set(ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind])
    assert required <= catalog


def test_common_catalog_extension_reaches_every_session_catalog():
    common = ("example.read", "example.submit")
    assert "example.read" in root_operation_catalog("companion", common_operation_ids=common)
    assert "example.submit" in root_operation_catalog("companion", common_operation_ids=common)
    assert set(common) <= set(root_operation_catalog("idea", common_operation_ids=common))


def test_catalog_rejects_duplicate_or_overlapping_grants():
    with pytest.raises(ValueError, match="root_common_operation_catalog_invalid"):
        root_operation_catalog("idea", common_operation_ids=("human_request.open",) * 2)
    with pytest.raises(ValueError, match="root_operation_catalog_overlap"):
        root_operation_catalog("target", common_operation_ids=("agent_runtime.target_run.observe",))


@pytest.mark.parametrize("root_kind", ("companion", "target", "deepfetch"))
def test_role_only_effect_and_reconcile_are_visible_but_owner_gated(root_kind):
    effect = SemanticOperation(
        semantic_operation_id="research_graph.question_relations.record",
        owning_module="research_graph",
        description="test",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
        access_mode="effect",
        reconciliation_operation_id="research_graph.question_relations.record.reconcile",
        handler=lambda _context, _arguments: {"status": "unexpected"},
    )
    reconcile = SemanticOperation(
        semantic_operation_id="research_graph.question_relations.record.reconcile",
        owning_module="research_graph",
        description="test",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
        access_mode="reconcile",
        handler=lambda _context, _arguments: {"status": "unexpected"},
    )
    gateway = SemanticMcpGateway((effect, reconcile))
    connection, _binding = gateway.issue_channel(
        run_ref="run",
        attempt_ref="attempt",
        root_session_ref="session",
        fence_ref="fence",
        capability_binding_hash="a" * 64,
        operation_ids=(effect.semantic_operation_id, reconcile.semantic_operation_id),
        root_kind=root_kind,
        phase="test",
    )
    for request_id, operation_id in enumerate(
        (effect.semantic_operation_id, reconcile.semantic_operation_id), start=1
    ):
        status, response = gateway.dispatch(
            connection.token,
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                "name": operation_id, "arguments": {},
            }},
        )
        assert status == 200
        assert response is not None
        result = response["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["code"] == "capability_unavailable"
        assert result["structuredContent"]["details"]["reason"] == "root_kind_not_owner"


def test_gateway_registers_the_live_role_catalogs():
    assert tuple(ROOT_AGENT_SEMANTIC_OPERATION_IDS) == ROOT_AGENT_KINDS
    for root_kind, catalog in ROOT_AGENT_SEMANTIC_OPERATION_IDS.items():
        assert catalog == root_operation_catalog(root_kind, common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS)
    assert BUNDLE_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["bundle"]
    assert TARGET_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["target"]
    assert REASONING_ROOT_SEMANTIC_OPERATION_IDS == ROOT_AGENT_SEMANTIC_OPERATION_IDS["reasoning"]
