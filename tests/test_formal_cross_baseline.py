"""Target work items may reuse or register method identities beyond the draft.

Q1 (2026-09-22): the Bundle draft's Baseline is a starting suggestion; the
Target decides the real attribution of its actual runs, including Variants of
another Baseline and newly declared method objects.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from meta_research.baseline_identity import resolve_baseline_method_identity
from meta_research.formal_entities import _variant_declaration, root_work_items
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import _get_or_create_target_measurement_identity
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


NEW_METHOD_FORWARD = {
    "method_key": "device_time_distribution_audit",
    "method_version": "1",
    "method_contract": {
        "purpose": "Check device/time group coverage without causal claims",
        "input_semantics": "sample metadata",
        "procedure": "cross-group counting",
        "output_semantics": "grouped annotations and descriptive summaries",
    },
}
NEW_RECIPE = {"group_by": ["device_id", "month"], "missing_time_policy": "own-group"}


def _register_method(connection, forward, recipe, accepted_at=1.0):
    baseline_ref, _created = resolve_baseline_method_identity(
        connection, forward=forward, quest_ref=_quest_ref(connection), accepted_at=accepted_at)
    variant_ref, _created = _get_or_create_target_measurement_identity(
        connection, table="rg_experiment_variants", ref_column="variant_ref",
        ref_prefix="variant", natural={"baseline_ref": baseline_ref,
                                        "recipe_hash": canonical_hash(recipe)},
        immutable={"recipe_json": canonical_json(recipe)},
        insert_only={"accepted_at": accepted_at})
    return baseline_ref, variant_ref


def _quest_ref(connection):
    return connection.execute(text("SELECT quest_ref FROM rg_quests LIMIT 1")).scalar_one()


def test_non_primary_run_can_use_a_variant_of_another_baseline(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        with runtime._database.write() as connection:
            _other_baseline, other_variant = _register_method(
                connection,
                {"method_key": "comparison_partner_method", "method_version": "1",
                 "method_contract": {"meaning": "partner method for comparison"}},
                {"seed": 7})
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        document["formal_runs"] = [
            {"run_key": "primary-work", "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
            {"run_key": "partner-work", "variant_ref": other_variant, "evaluations": []},
        ]
        document["metrics"] = {}
        path.write_text(canonical_json(document))

        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        by_key = {fact["run_key"]: fact for fact in facts}
        assert by_key["partner-work"]["variant_run"]["variant_ref"] == other_variant
        assert by_key["partner-work"]["evaluation_attempt"] is None
        assert by_key["primary-work"]["variant_run"]["variant_ref"] != other_variant
        replay, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted
    finally:
        runtime.close()


def test_run_can_declare_a_new_method_at_completion(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        document["formal_runs"] = [
            {"run_key": "primary-work", "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
            {"run_key": "audit-work",
             "baseline_forward_contract": NEW_METHOD_FORWARD,
             "variant_recipe": NEW_RECIPE,
             "evaluations": []},
        ]
        document["metrics"] = {}
        path.write_text(canonical_json(document))

        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = {fact["run_key"]: fact for fact in graph.query_target_formal_results(handle.target_ref)}
        audit_variant = facts["audit-work"]["variant_run"]["variant_ref"]
        with runtime._database.read() as connection:
            row = connection.execute(text(
                "SELECT b.baseline_ref, v.baseline_ref FROM rg_experiment_variants v "
                "JOIN rg_experiment_baselines b USING (baseline_ref) WHERE v.variant_ref = :ref"),
                {"ref": audit_variant}).mappings().one()
            assert row["baseline_ref"] != authority.identities.baseline_ref
            method = connection.execute(text(
                "SELECT method_key, method_version FROM rg_baseline_method_versions "
                "WHERE baseline_ref = :ref"), {"ref": row["baseline_ref"]}).mappings().one()
        assert method["method_key"] == NEW_METHOD_FORWARD["method_key"]
        assert method["method_version"] == NEW_METHOD_FORWARD["method_version"]
        readable = graph.query_baseline(row["baseline_ref"])
        assert readable is not None
        page = graph.query_baselines(query=NEW_METHOD_FORWARD["method_key"])
        assert any(item.get("baseline_ref") == row["baseline_ref"] for item in page["items"])
        replay, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted
    finally:
        runtime.close()


def test_primary_run_may_declare_its_actual_variant_and_get_paired_evaluation(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        document["formal_runs"] = [
            {"run_key": "actual-primary",
             "baseline_forward_contract": NEW_METHOD_FORWARD,
             "variant_recipe": NEW_RECIPE,
             "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
        ]
        document["metrics"] = {}
        path.write_text(canonical_json(document))

        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["variant_run"]["variant_ref"] != authority.identities.variant_ref
        # The evaluation pairs the declared Variant with the authority protocol.
        with runtime._database.read() as connection:
            evaluation = connection.execute(text(
                "SELECT variant_ref, protocol_version_ref FROM rg_evaluations "
                "WHERE evaluation_ref = :ref"),
                {"ref": fact["evaluation_attempt"]["evaluation_ref"]}).mappings().one()
        assert evaluation["variant_ref"] == fact["variant_run"]["variant_ref"]
        assert evaluation["protocol_version_ref"] == authority.identities.protocol_version_ref
        assert fact["metric_result"]["metrics"] == metrics
        replay, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted
    finally:
        runtime.close()


def test_declaration_conflicts_with_explicit_variant_or_reused_run():
    resolve = lambda run: root_work_items(
        payload={"measurement_ref": "m", "variant_run_ref": "run-primary",
                 "evaluation_attempt_ref": "attempt-primary", "metric_result_ref": "metric-primary"},
        identities={"variant_ref": "variant-1", "evaluation_ref": "evaluation-1"},
        result_document={"metrics": {"quality": 1.0},
                         "formal_runs": [run]},
    )
    with pytest.raises(OwnerConflict, match="target_formal_variant_declaration_invalid"):
        resolve({"run_key": "primary", "baseline_forward_contract": NEW_METHOD_FORWARD,
                 "variant_recipe": NEW_RECIPE,
                 "variant_ref": "variant-1",
                 "evaluations": [{"attempt_key": "a", "metrics": {"quality": 1.0}}]})
    with pytest.raises(OwnerConflict, match="target_formal_variant_declaration_invalid"):
        resolve({"run_key": "primary", "baseline_forward_contract": NEW_METHOD_FORWARD,
                 "variant_recipe": NEW_RECIPE, "variant_run_ref": "run-existing",
                 "evaluations": []})


def test_declaration_requires_both_contract_and_recipe():
    assert _variant_declaration({"run_key": "a"}) is None
    with pytest.raises(OwnerConflict, match="target_formal_variant_declaration_invalid"):
        _variant_declaration({"run_key": "a", "variant_recipe": NEW_RECIPE})
    with pytest.raises(OwnerConflict, match="target_formal_variant_declaration_invalid"):
        _variant_declaration({"run_key": "a", "baseline_forward_contract": NEW_METHOD_FORWARD,
                              "variant_recipe": {}})
