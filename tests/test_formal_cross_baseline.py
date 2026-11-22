"""Target work items may reuse or register method identities beyond the draft.

Q1 (2026-09-22): the Bundle draft's Baseline is a starting suggestion; the
Target decides the real attribution of its actual runs, including Variants of
another Baseline and newly declared method objects.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.baseline_identity import resolve_baseline_method_identity
from meta_research.formal_entities import (
    _resolve_declared_variants, _variant_declaration, root_work_items,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import _get_or_create_target_measurement_identity
from meta_research.target_content_issue import TargetContentIssue
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_root_formal_entities import _accept
from test_target_root_finalizer import (
    _EvidenceReader, _completion_handoff, _replace_completion_evidence,
    _root_finalizer_fixture,
)


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
        path = workspace / "outputs/result.json"
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
        path = workspace / "outputs/result.json"
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
        path = workspace / "outputs/result.json"
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


CONFLICTING_CONTRACT = {
    **NEW_METHOD_FORWARD["method_contract"],
    "output_semantics": "causal effect estimates and interval bounds",
}


def _declared_run(document, metrics, *, contract):
    """Declare one extra named method run beside the authority's primary work."""
    primary = next(run for run in document["formal_runs"]
                   if run["run_key"] == "primary")
    primary = {**primary, "evaluations": [{"attempt_key": "main", "metrics": metrics}]}
    document["formal_runs"] = [
        primary,
        {"run_key": "audit-work",
         "baseline_forward_contract": {**NEW_METHOD_FORWARD, "method_contract": contract},
         "variant_recipe": NEW_RECIPE, "evaluations": []},
    ]
    document["metrics"] = {}


def test_declared_method_content_conflict_becomes_revision_required_after_rm(
    tmp_path: Path,
) -> None:
    """A wrong reuse of an existing name/version is a correctable Target declaration."""
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        with runtime._database.write() as connection:
            registered_baseline, _variant = _register_method(
                connection, NEW_METHOD_FORWARD, NEW_RECIPE)
        path = workspace / "outputs/result.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        _declared_run(document, metrics, contract=CONFLICTING_CONTRACT)
        path.write_text(canonical_json(document))

        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.completion_generation == 1
        assert rejected.rejection_issuer == "research_graph"
        assert rejected.pending_code == "baseline_method_version_content_conflict"
        assert rejected.target_commit_ref is None
        # The signed feedback itself carries the declared identity: the Owner
        # rejection records only this text, never the exception details.
        assert "audit-work" in rejected.rejection_feedback
        assert "$.formal_runs[1]" in rejected.rejection_feedback
        assert "device_time_distribution_audit" in rejected.rejection_feedback
        assert "method_version '1'" in rejected.rejection_feedback
        assert canonical_hash(NEW_METHOD_FORWARD["method_contract"]) in rejected.rejection_feedback
        assert canonical_hash(CONFLICTING_CONTRACT) in rejected.rejection_feedback
        assert memory.query(rejected.manifest_ref) is not None
        completion = lifecycle.query_completion(handle.target_ref)
        assert completion is not None and completion.generation == 1
        rejection = lifecycle.query_completion_rejection(completion.completion_ref)
        assert rejection is not None
        assert rejection.code == "baseline_method_version_content_conflict"
        assert rejection.receipt.subject_ref == completion.completion_ref
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_root_measurements")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0

        # The same frozen generation replays its exact signed rejection.
        replay = finalizer.finalize(handle=handle, evidence=evidence)
        assert replay == rejected
        assert lifecycle.query_completion_rejection(completion.completion_ref) == rejection

        # The corrected successor reuses the same registered Baseline.
        corrected = json.loads(path.read_text())
        corrected["formal_runs"][1]["baseline_forward_contract"]["method_contract"] = (
            NEW_METHOD_FORWARD["method_contract"])
        path.write_text(canonical_json(corrected))
        successor_evidence = _replace_completion_evidence(
            workspace, evidence,
            handoff=_completion_handoff(workspace, evidence),
            operation_ref="harness_target_root_successor_turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_target_root_successor_turn",
            evidence_sequence=evidence.evidence_sequence + 10,
            observed_at=evidence.observed_at + 1.0,
        )
        successor = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(successor_evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=successor_evidence)
        assert successor.status == "completed"
        assert successor.completion_generation == 2
        facts = {fact["run_key"]: fact
                 for fact in runtime.owners.research_graph.query_target_formal_results(handle.target_ref)}
        with runtime._database.read() as connection:
            audit_baseline = connection.execute(text(
                "SELECT baseline_ref FROM rg_experiment_variants WHERE variant_ref = :ref"),
                {"ref": facts["audit-work"]["variant_run"]["variant_ref"]}).scalar_one()
        assert audit_baseline == registered_baseline
        assert lifecycle.query_completion_rejection(
            completion.completion_ref) == rejection
    finally:
        runtime.close()


def test_only_revisable_declaration_faults_are_target_content_issues(tmp_path):
    """Integrity and verification faults stay OwnerConflict."""
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    items = [{"run_key": "audit-work", "reuse_variant_run": False, "variant_ref": None}]
    document = {"formal_runs": [
        {"run_key": "audit-work",
         "baseline_forward_contract": {**NEW_METHOD_FORWARD, "method_contract": CONFLICTING_CONTRACT},
         "variant_recipe": NEW_RECIPE, "evaluations": []},
    ]}
    try:
        with runtime._database.write() as connection:
            _register_method(connection, NEW_METHOD_FORWARD, NEW_RECIPE)
            with pytest.raises(OwnerConflict) as caught:
                _resolve_declared_variants(
                    connection, items=items, result_document=document,
                    root={"target_ref": handle.target_ref}, accepted_at=1.0,
                    verify_only=True,
                )
        assert not isinstance(caught.value, TargetContentIssue)
        assert caught.value.code == "baseline_method_version_content_conflict"

        envelope_fault = {"formal_runs": [
            {"run_key": "audit-work",
             "baseline_forward_contract": {**NEW_METHOD_FORWARD, "execution_binding": "x"},
             "variant_recipe": NEW_RECIPE, "evaluations": []},
        ]}
        with runtime._database.write() as connection:
            with pytest.raises(TargetContentIssue) as caught:
                _resolve_declared_variants(
                    connection, items=items, result_document=envelope_fault,
                    root={"target_ref": handle.target_ref}, accepted_at=2.0,
                    verify_only=False,
                )
        assert caught.value.code == "baseline_method_envelope_invalid"
        assert "execution_binding" in caught.value.feedback
    finally:
        runtime.close()


def test_tampered_registry_hash_stays_owner_conflict_on_the_live_path(tmp_path):
    """An unreadable method registry fails closed, even where a conflict would be revisable."""
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    items = [{"run_key": "audit-work", "reuse_variant_run": False, "variant_ref": None}]
    document = {"formal_runs": [
        {"run_key": "audit-work",
         "baseline_forward_contract": {**NEW_METHOD_FORWARD, "method_contract": CONFLICTING_CONTRACT},
         "variant_recipe": NEW_RECIPE, "evaluations": []},
    ]}
    try:
        with runtime._database.write() as connection:
            _register_method(connection, NEW_METHOD_FORWARD, NEW_RECIPE)
            connection.execute(text(
                "UPDATE rg_baseline_method_versions SET method_contract_hash = :hash "
                "WHERE method_key = :key"), {
                "hash": canonical_hash({"meaning": "tampered"}),
                "key": NEW_METHOD_FORWARD["method_key"]})
            with pytest.raises(OwnerConflict) as caught:
                _resolve_declared_variants(
                    connection, items=items, result_document=document,
                    root={"target_ref": handle.target_ref}, accepted_at=1.0,
                    verify_only=False,
                )
        assert not isinstance(caught.value, TargetContentIssue)
        assert caught.value.code == "baseline_method_version_integrity_invalid"
    finally:
        runtime.close()


def test_baseline_ref_only_declaration_names_its_registered_method(tmp_path):
    """A reference-only declaration reports the referenced method's exact identity."""
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    items = [{"run_key": "audit-work", "reuse_variant_run": False, "variant_ref": None}]
    document = {"formal_runs": [
        {"run_key": "audit-work",
         "baseline_forward_contract": {"baseline_ref": None,
                                       "method_contract": CONFLICTING_CONTRACT},
         "variant_recipe": NEW_RECIPE, "evaluations": []},
    ]}
    try:
        with runtime._database.write() as connection:
            baseline_ref, _variant = _register_method(connection, NEW_METHOD_FORWARD, NEW_RECIPE)
            document["formal_runs"][0]["baseline_forward_contract"]["baseline_ref"] = baseline_ref
            with pytest.raises(TargetContentIssue) as caught:
                _resolve_declared_variants(
                    connection, items=items, result_document=document,
                    root={"target_ref": handle.target_ref}, accepted_at=1.0,
                    verify_only=False,
                )
        assert caught.value.code == "baseline_method_version_content_conflict"
        assert baseline_ref in caught.value.feedback
        assert "method_key 'device_time_distribution_audit'" in caught.value.feedback
        assert "method_version '1'" in caught.value.feedback
        assert canonical_hash(NEW_METHOD_FORWARD["method_contract"]) in caught.value.feedback
        assert canonical_hash(CONFLICTING_CONTRACT) in caught.value.feedback
    finally:
        runtime.close()


def test_missing_method_contract_is_a_durable_root_correction(tmp_path):
    """A malformed method envelope is rejected and can be corrected in place."""
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        path = workspace / "outputs/result.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        _declared_run(document, metrics, contract=None)
        document["formal_runs"][1]["baseline_forward_contract"].pop("method_contract")
        path.write_text(canonical_json(document))

        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.pending_code == "baseline_method_contract_required"
        assert rejected.target_commit_ref is None
        assert "method_contract" in rejected.rejection_feedback
        assert memory.query(rejected.manifest_ref) is not None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_root_measurements")
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_target_commits")
            ).scalar_one() == 0

        corrected = json.loads(path.read_text())
        corrected["formal_runs"][1]["baseline_forward_contract"]["method_contract"] = (
            NEW_METHOD_FORWARD["method_contract"])
        path.write_text(canonical_json(corrected))
        successor_evidence = _replace_completion_evidence(
            workspace, evidence,
            handoff=_completion_handoff(workspace, evidence),
            operation_ref="harness_target_root_successor_turn_contract",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="harness_evidence_target_root_successor_turn_contract",
            evidence_sequence=evidence.evidence_sequence + 10,
            observed_at=evidence.observed_at + 1.0,
        )
        successor = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(successor_evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=successor_evidence)
        assert successor.status == "completed"
        assert successor.target_commit_ref is not None
    finally:
        runtime.close()
