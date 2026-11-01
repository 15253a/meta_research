"""Historical Baseline navigation uses accepted work and exact content readers."""
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import canonical_json, OwnerConflict
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.fixture
def completed_work(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    path = workspace / "outputs/metrics.json"
    document = json.loads(path.read_text())
    checkpoints = [item.relative_path for item in evidence.handoff.artifacts if item.role == "checkpoint"]
    document["formal_runs"] = [
        {"run_key": "spectral-diagnostic-run", "checkpoint_paths": checkpoints, "evaluations": [
            {"attempt_key": "calibration-assessment", "metrics": document["metrics"]},
            {"attempt_key": "held-out-assessment", "metrics": document["metrics"]}]},
        {"run_key": "negative-observation", "checkpoint_paths": checkpoints, "evaluations": []},
    ]
    path.write_text(canonical_json(document))
    _accept(runtime, lifecycle, memory, handle, evidence)
    graph = runtime.owners.research_graph
    quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
    yield runtime, graph, quest, authority.identities.baseline_ref, authority.identities.variant_ref
    runtime.close()


def test_research_clues_from_actual_runs_and_assessments_find_parent_baseline(completed_work):
    _, graph, quest, baseline, _ = completed_work
    for clue in ("spectral-diagnostic-run", "calibration-assessment", "negative-observation", "result_content"):
        page = graph.query_baselines(quest_ref=quest, query=clue)
        assert [item["baseline_ref"] for item in page["items"]] == [baseline], clue
        assert page["result_state"] == "matches"


def test_registered_baseline_contract_accepts_deep_relations_and_rejects_unbound_filters(completed_work):
    from meta_research.semantic_mcp import SemanticMcpGateway
    from meta_research.semantic_owner_gateway import _baseline_operations
    from test_dataset_effect_scope_recovery import _scope
    runtime, graph, quest, baseline, variant = completed_work
    scope = _scope(runtime, quest_ref=quest)
    gateway = SemanticMcpGateway(_baseline_operations(graph, runtime.owners.agent_runtime))
    operation = 'research_graph.baselines.read'
    channel, _ = gateway.issue_channel(run_ref=scope['run_ref'], attempt_ref=scope['attempt_ref'],
        root_session_ref=scope['root_session_ref'], fence_ref=scope['fence_ref'],
        capability_binding_hash=scope['runtime_binding_hash'], root_kind='companion', phase='primary',
        operation_ids=(operation,))
    def call(arguments):
        _, response = gateway.dispatch(channel.token, {'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':operation,'arguments':arguments}})
        return response['result']
    page = call({'baseline_ref':baseline,'relation':'runs','variant_ref':variant,'limit':1})
    assert not page.get('isError'), page
    contents = page['structuredContent']['result']['reusable_entities']
    run = contents['items'][0]['variant_run_ref']
    invalid = call({'baseline_ref':baseline,'variant_run_ref':run})
    assert invalid.get('isError') and 'baseline_query_invalid' in str(invalid)
    runtime.owners.agent_runtime.complete_external_root_task_scope(root_kind='companion',root_runtime_scope=scope)
    denied = call({'baseline_ref':baseline,'relation':'runs'})
    assert denied.get('isError') and 'scope_stale' in str(denied)


def test_each_deep_relation_pages_independently_and_returns_callable_exact_readers(completed_work):
    runtime, graph, quest, baseline, variant = completed_work
    nested = graph.query_baseline_variants(baseline, quest_ref=quest, variant_ref=variant, limit=1)
    assert nested['items'][0]['next_run_offset'] == 1
    assert nested['scan_complete'] is False
    runs = graph.query_baseline_relations(baseline, quest_ref=quest, variant_ref=variant,
                                          relation="runs", limit=1)
    first_run = runs["items"][0]
    second = graph.query_baseline_relations(baseline, quest_ref=quest, variant_ref=variant,
        relation="runs", limit=1, offset=runs["next_offset"], query_revision=runs["query_revision"])
    assert len(second["items"]) == 1 and second["next_offset"] is None
    assert second["items"][0]["variant_run_ref"] != first_run["variant_run_ref"]
    for item in [first_run, second["items"][0]]:
        assert item["reader"]["operation"] == "research_graph.formal_results.read"
        assert graph.query_formal_result_by_ref(quest_ref=quest, **item["reader"]["args"])
    attempts = graph.query_baseline_relations(baseline, quest_ref=quest, relation="attempts", limit=1)
    assert attempts["next_offset"] == 1
    later = graph.query_baseline_relations(baseline, quest_ref=quest, relation="attempts", limit=1,
        offset=attempts["next_offset"], query_revision=attempts["query_revision"])
    assert later["items"][0]["evaluation_attempt_ref"] != attempts["items"][0]["evaluation_attempt_ref"]
    assert later["next_offset"] is None
    artifacts = graph.query_baseline_relations(baseline, quest_ref=quest, relation="artifacts", limit=1)
    from meta_research.research_content import read_content
    artifact = artifacts["items"][0]
    assert artifact["reader"]["operation"] == "research_memory.content.read"
    exact = read_content(graph, runtime.owners.research_memory, quest_ref=quest, **artifact["reader"]["args"])
    assert exact["content_hash"] == artifact["content_hash"]
    assert "lifecycle_history" not in artifact


def test_continuation_detects_insert_delete_and_scope_change_but_not_other_owner_changes(completed_work):
    from types import SimpleNamespace
    from test_baseline_discovery_search import _baseline
    runtime, graph, quest, baseline, variant = completed_work
    _baseline(runtime, SimpleNamespace(quest_ref=quest), "newer-method", accepted_at=9999999999.0)
    first = graph.query_baselines(quest_ref=quest, limit=1)
    with pytest.raises(OwnerConflict, match="research_query_revision_required"):
        graph.query_baselines(quest_ref=quest, limit=1, offset=1)
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE research_graph_state SET revision=revision+1 WHERE singleton='owner'"))
    second = graph.query_baselines(quest_ref=quest, limit=1, offset=1, query_revision=first["query_revision"])
    assert second["items"][0]["baseline_ref"] == baseline
    with pytest.raises(OwnerConflict, match="research_query_changed"):
        graph.query_baselines(quest_ref=quest, query="different", limit=1, offset=1,
                              query_revision=first["query_revision"])
    inserted = _baseline(runtime, SimpleNamespace(quest_ref=quest), "inserted-method", accepted_at=9999999998.0)
    with pytest.raises(OwnerConflict, match="research_query_changed"):
        graph.query_baselines(quest_ref=quest, limit=1, offset=1, query_revision=first["query_revision"])
    refreshed = graph.query_baselines(quest_ref=quest, limit=1)
    with runtime._database.write() as connection:
        connection.execute(text("DELETE FROM rg_baseline_method_versions WHERE baseline_ref=:ref"), {"ref": inserted})
        connection.execute(text("DELETE FROM rg_experiment_baselines WHERE baseline_ref=:ref"), {"ref": inserted})
    with pytest.raises(OwnerConflict, match="research_query_changed"):
        graph.query_baselines(quest_ref=quest, limit=1, offset=1, query_revision=refreshed["query_revision"])
    relations = graph.query_baseline_relations(baseline, relation="runs", quest_ref=quest, limit=1)
    with pytest.raises(OwnerConflict, match="research_query_changed"):
        graph.query_baseline_relations(baseline, relation="attempts", quest_ref=quest, limit=1,
            offset=1, query_revision=relations["query_revision"])


def test_description_search_keeps_actual_run_attribution_within_multi_method_target(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        document["formal_runs"] = [
            {"run_key": "primary-method-work", "evaluations": [{"attempt_key": "main", "metrics": document["metrics"]}]},
            {"run_key": "distinct-method-work", "baseline_forward_contract": {
                "method_key": "descriptive-method", "method_version": "1",
                "method_contract": {"purpose": "independent descriptive method"}},
             "variant_recipe": {"procedure": "count"}, "evaluations": []},
        ]
        path.write_text(canonical_json(document))
        _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
        primary = graph.query_baselines(quest_ref=quest, query="primary-method-work")
        assert [item["baseline_ref"] for item in primary["items"]] == [authority.identities.baseline_ref]
        distinct = graph.query_baselines(quest_ref=quest, query="distinct-method-work")
        assert len(distinct["items"]) == 1
        assert distinct["items"][0]["baseline_ref"] != authority.identities.baseline_ref
    finally:
        runtime.close()
