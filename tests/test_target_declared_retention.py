"""Agent-selected retention preserves exact content and actual work attribution."""
from dataclasses import replace
import hashlib
from io import BytesIO
import json
import os
from zipfile import ZipFile

import pytest

from meta_research.owners.common import canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.skipif(os.name != "posix", reason="Target runtime uses POSIX descriptor APIs")
def test_agent_retains_file_directory_and_collection_with_declared_purposes(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, old = _root_finalizer_fixture(tmp_path)
    try:
        paths = {
            "materials/cohort/raw.csv": "subject,value\nA,4\n",
            "fieldnotes/observation.md": "Observation from this completed run.\n",
            "outputs/data/saved-state.bin": "actual retained state\n",
        }
        for relative_path, content in paths.items():
            path = workspace / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        document = json.loads((workspace / "outputs/metrics.json").read_text())
        document["retained_artifacts"] = [
            {"relative_path": "materials/cohort", "role": "data"},
            {"relative_path": "fieldnotes/observation.md", "role": "analysis"},
            {"relative_path": "outputs/data/saved-state.bin", "role": "checkpoint"},
        ]
        document["formal_runs"] = [{
            "run_key": "cohort-observation",
            "artifact_paths": ["materials/cohort", "fieldnotes/observation.md", "logs/train.log"],
            "checkpoint_paths": ["outputs/data/saved-state.bin"],
            "evaluations": [{"attempt_key": "assessment", "metrics": document["metrics"]}],
        }]
        (workspace / "outputs/result.json").write_text(canonical_json(document), encoding="utf-8")
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        final_text = "Retained the cohort, observations and exact state from the actual work."
        evidence = replace(old, handoff=None, workspace_ref=workspace_ref,
            final_text=final_text, final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph, evidence_reader=_SystemEvidenceReader())

        completed = finalizer.finalize(handle=handle, evidence=evidence)

        assert completed.status == "completed"
        manifest = memory.query(completed.manifest_ref)
        selected = {entry.declared_relative_path: entry for entry in manifest.entries}
        assert selected["materials/cohort"].role == "data"
        assert selected["fieldnotes/observation.md"].role == "analysis"
        assert selected["outputs/data/saved-state.bin"].role == "checkpoint"
        assert len([entry for entry in manifest.entries
                    if entry.declared_relative_path == "outputs/data/saved-state.bin"]) == 1
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        roles = {role["version_ref"]: role["role"] for role in facts[0]["run_artifacts"]}
        assert roles[selected["materials/cohort"].binding.version_ref] == "data_asset"
        assert roles[selected["fieldnotes/observation.md"].binding.version_ref] == "analysis_asset"
        assert roles[selected["outputs/data/saved-state.bin"].binding.version_ref] == "checkpoint_artifact"
        cohort = runtime.owners.research_memory.materialize_asset(
            selected["materials/cohort"].binding.version_ref)
        with ZipFile(BytesIO(cohort.content)) as archive:
            assert archive.read("raw.csv").decode("utf-8") == paths["materials/cohort/raw.csv"]
        assert runtime.owners.research_memory.read_asset_entry_text(
            selected["fieldnotes/observation.md"].binding.version_ref) == paths["fieldnotes/observation.md"]
        assert runtime.owners.research_memory.read_asset_entry_text(
            selected["outputs/data/saved-state.bin"].binding.version_ref) == paths["outputs/data/saved-state.bin"]
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
    finally:
        runtime.close()
