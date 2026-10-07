"""Retained production and purpose relations protect their exact content."""
from __future__ import annotations

import json
from pathlib import Path

from meta_research.owners.common import canonical_json
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def test_independent_production_references_survive_one_role_correction(tmp_path: Path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        document["formal_runs"] = [
            {"run_key": "first", "artifact_paths": ["logs/train.log"],
             "checkpoint_paths": [artifact.relative_path for artifact in evidence.handoff.artifacts
                                  if artifact.role == "checkpoint"],
             "evaluations": [{"attempt_key": "assessment", "metrics": document["metrics"]}]},
            {"run_key": "second", "artifact_paths": ["logs/train.log"],
             "checkpoint_paths": [], "evaluations": []},
        ]
        path.write_text(canonical_json(document))
        _, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = {item["run_key"]: item for item in graph.query_target_formal_results(handle.target_ref)}
        first = next(role for role in facts["first"]["run_artifacts"] if role["role"] == "log_asset")
        second = next(role for role in facts["second"]["run_artifacts"] if role["role"] == "log_asset")
        assert first["role_ref"] != second["role_ref"]
        binding = next(entry.binding for entry in manifest.entries if entry.declared_relative_path == "logs/train.log")
        assert runtime.owners.research_memory.materialize_asset(binding.version_ref).content == b"epoch 1 complete\n"
        expected = {f"experiment-asset-role:{role['role_ref']}" for role in (first, second)}
        references = set(graph.query_asset_references(binding.version_ref))
        assert expected <= references
        assessment = runtime.owners.research_memory.assess_release_eligibility(
            binding.version_ref, expected_reference_revision=graph.query_asset_reference_revision(),
            idempotency_key="production-release-before",
        )
        assert assessment.eligible is False
        assert expected <= set(assessment.active_reference_refs)
        graph.adjust_experiment_artifact_role(
            role_ref=first["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=facts["first"]["evaluation_attempt_ref"],
            reason="The report was attributed to the execution instead of its assessment.",
            idempotency_key="production-correct-purpose",
        )
        assert expected <= set(graph.query_asset_references(binding.version_ref))
        corrected = {item["run_key"]: item for item in graph.query_target_formal_results(handle.target_ref)}
        assert second in corrected["second"]["run_artifacts"]
        assert first["role_ref"] in {role["role_ref"] for role in corrected["first"]["evaluation_artifacts"]}
    finally:
        runtime.close()


def test_target_handoff_content_keeps_its_own_reference(tmp_path: Path):
    runtime, lifecycle, memory, _, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        binding = next(entry.binding for entry in manifest.entries if entry.role == "implementation")
        assert f"target-commit:{accepted.target_commit_ref}" in (
            runtime.owners.research_graph.query_asset_references(binding.version_ref)
        )
        assessment = runtime.owners.research_memory.assess_release_eligibility(
            binding.version_ref,
            expected_reference_revision=runtime.owners.research_graph.query_asset_reference_revision(),
            idempotency_key="target-handoff-protected",
        )
        assert assessment.eligible is False
    finally:
        runtime.close()
