from __future__ import annotations

import pytest
from sqlalchemy import text

from meta_research.baseline_identity import resolve_baseline_method_identity
from meta_research.owners.common import canonical_hash, canonical_json
from test_public_bundle_stage import _bundle_runtime
from test_research_datasets import _quest


@pytest.fixture
def runtime(tmp_path):
    value = _bundle_runtime(tmp_path / "baseline-discovery")
    yield value
    value.close()


def _baseline(runtime, quest, key, *, meaning="ordinary method", accepted_at=1.0):
    with runtime._database.write() as connection:
        ref, created = resolve_baseline_method_identity(connection,
            forward={"method_key": key, "method_version": "1",
                     "method_contract": {"meaning": meaning}},
            quest_ref=quest.quest_ref, accepted_at=accepted_at)
    assert created
    return ref


def _variant(runtime, baseline, key, *, description="ordinary recipe"):
    ref = "variant_" + key
    recipe = {"description": description, "recipe_key": key}
    with runtime._database.write() as connection:
        connection.execute(text(
            "INSERT INTO rg_experiment_variants "
            "(variant_ref, baseline_ref, recipe_json, recipe_hash, accepted_at) "
            "VALUES (:ref, :baseline, :document, :hash, 1.0)"),
            {"ref": ref, "baseline": baseline, "document": canonical_json(recipe),
             "hash": canonical_hash(recipe)})
    return ref


def _evaluation(runtime, quest, variant, key, *, description="ordinary assessment", bound=True):
    protocol_ref, version_ref = "protocol_" + key, "protocol_version_" + key
    lineage = {"family": key, "notes": "lineage-only-token"}
    protocol = {"description": description}
    metrics = ["metric-list-only-token"]
    with runtime._database.write() as connection:
        connection.execute(text(
            "INSERT INTO rg_evaluation_protocols "
            "(evaluation_protocol_ref, quest_ref, lineage_json, lineage_hash, accepted_at) "
            "VALUES (:ref, :quest, :document, :hash, 1.0)"),
            {"ref": protocol_ref, "quest": quest.quest_ref,
             "document": canonical_json(lineage), "hash": canonical_hash(lineage)})
        connection.execute(text(
            "INSERT INTO rg_protocol_versions (protocol_version_ref, evaluation_protocol_ref, "
            "protocol_json, protocol_hash, required_metrics_json, required_metrics_hash, accepted_at) "
            "VALUES (:ref, :protocol, :document, :hash, :metrics, :metrics_hash, 1.0)"),
            {"ref": version_ref, "protocol": protocol_ref, "document": canonical_json(protocol),
             "hash": canonical_hash(protocol), "metrics": canonical_json(metrics),
             "metrics_hash": canonical_hash(metrics)})
        if bound:
            connection.execute(text(
                "INSERT INTO rg_evaluations "
                "(evaluation_ref, variant_ref, protocol_version_ref, accepted_at) "
                "VALUES (:ref, :variant, :protocol, 1.0)"),
                {"ref": "evaluation_" + key, "variant": variant, "protocol": version_ref})


def _refs(page):
    return [item["baseline_ref"] for item in page["items"]]


def test_method_recipe_and_bound_evaluation_text_find_the_parent_baseline(runtime):
    quest = _quest(runtime, "three-fields")
    graph = runtime.owners.research_graph
    baseline = _baseline(runtime, quest, "shared-method", meaning="Method-only-token")
    variant = _variant(runtime, baseline, "recipe", description="Recipe-only-token")
    _evaluation(runtime, quest, variant, "evaluation", description="Evaluation-only-token")
    for query in ("method-only-token", "recipe-ONLY-token", "evaluation-only-token"):
        page = graph.query_baselines(query=query, quest_ref=quest.quest_ref)
        assert _refs(page) == [baseline]
        assert page["next_offset"] is None
        assert page["selection_required"] is True
        assert page["automatic_merge"] is False
    selected = graph.query_baseline_variants(baseline, variant_ref=variant, quest_ref=quest.quest_ref)
    assert selected["items"][0]["recipe"]["description"] == "Recipe-only-token"
    assert selected["items"][0]["evaluations"][0]["protocol"] == {
        "description": "Evaluation-only-token"}
    assert _refs(graph.query_baselines(query="recipe-only-token evaluation-only-token",
                                      quest_ref=quest.quest_ref)) == []


def test_child_matches_keep_exact_parent_quest_and_protocol_binding(runtime):
    local, other = _quest(runtime, "local"), _quest(runtime, "other")
    graph = runtime.owners.research_graph
    plain = _baseline(runtime, local, "plain")
    plain_variant = _variant(runtime, plain, "plain")
    matched = _baseline(runtime, local, "matched")
    matched_variant = _variant(runtime, matched, "matched", description="recipe-needle")
    _evaluation(runtime, local, matched_variant, "matched", description="evaluation-needle")
    foreign = _baseline(runtime, other, "foreign")
    foreign_variant = _variant(runtime, foreign, "foreign", description="recipe-needle")
    _evaluation(runtime, other, foreign_variant, "foreign", description="evaluation-needle")
    _evaluation(runtime, local, plain_variant, "unbound", description="unbound-only-token", bound=False)
    for query in ("recipe-needle", "evaluation-needle"):
        assert _refs(graph.query_baselines(query=query, quest_ref=local.quest_ref)) == [matched]
        assert _refs(graph.query_baselines(query=query, quest_ref=other.quest_ref)) == [foreign]
    for query in ("unbound-only-token", "lineage-only-token", "metric-list-only-token"):
        assert _refs(graph.query_baselines(query=query, quest_ref=local.quest_ref)) == []


def test_child_matches_do_not_duplicate_pagination_or_bypass_method_hash(runtime):
    quest = _quest(runtime, "pagination")
    graph = runtime.owners.research_graph
    older = _baseline(runtime, quest, "older", accepted_at=1.0)
    newer = _baseline(runtime, quest, "newer", meaning="different contract", accepted_at=2.0)
    for index, baseline in enumerate((older, newer)):
        for suffix in ("a", "b"):
            key = f"{index}-{suffix}"
            variant = _variant(runtime, baseline, key, description="shared-needle")
            _evaluation(runtime, quest, variant, key, description="shared-needle")
    first = graph.query_baselines(query="shared-needle", quest_ref=quest.quest_ref, limit=1)
    assert _refs(first) == [newer]
    assert first["next_offset"] == 1
    second = graph.query_baselines(query="shared-needle", quest_ref=quest.quest_ref, limit=1, offset=1)
    assert _refs(second) == [older]
    assert second["next_offset"] is None
    assert _refs(graph.query_baselines(quest_ref=quest.quest_ref)) == [newer, older]
    method_hash = graph.query_baseline(older)["method_contract_hash"]
    exact = graph.query_baselines(query="shared-needle", method_contract_hash=method_hash,
                                 quest_ref=quest.quest_ref)
    assert _refs(exact) == [older]
    assert exact["items"][0]["match_kind"] == "exact_content_candidate"
    assert _refs(graph.query_baselines(query="absent", method_contract_hash=method_hash,
                                      quest_ref=quest.quest_ref)) == []
