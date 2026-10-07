"""Declared work owns retained products by its selections, not directory names."""
from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from zipfile import ZipFile

import pytest

from meta_research.owners.common import canonical_json
from meta_research.target_run_finalizer import TargetRootOwnerRejection
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def test_declared_work_cannot_claim_unassigned_data_from_its_directory(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/data/observations.csv"
        source = workspace / relative
        source.parent.mkdir(parents=True)
        source.write_text("cohort,value\nexternal,8\n", encoding="utf-8")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="data", relative_path=relative),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        checkpoints = [artifact.relative_path for artifact in evidence.handoff.artifacts
                       if artifact.role == "checkpoint"]
        document["formal_runs"] = [{
            "run_key": "declared-work",
            "checkpoint_paths": checkpoints,
            "evaluations": [{"attempt_key": "assessment", "metrics": document["metrics"],
                             "artifact_paths": []}],
        }]
        result_path.write_text(canonical_json(document))

        result, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert isinstance(result, TargetRootOwnerRejection)
        assert result.code == "target_root_commit_domain_invalid"
        assert relative in result.feedback
        assert "actual Run or Evaluation owner" in result.feedback
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
        retained = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert runtime.owners.research_memory.materialize_asset(retained.binding.version_ref).content == (
            b"cohort,value\nexternal,8\n"
        )
    finally:
        runtime.close()


def test_failed_independent_work_keeps_separate_same_byte_productions(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        paths = ("outputs/data/cohort-a.csv", "outputs/data/cohort-b.csv")
        for relative in paths:
            source = workspace / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"observed\nnegative\n")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            *(TargetCompletionArtifact(role="data", relative_path=relative) for relative in paths),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        checkpoints = [artifact.relative_path for artifact in evidence.handoff.artifacts
                       if artifact.role == "checkpoint"]
        document["metrics"] = {}
        document["result_disposition"] = "uncertain"
        document["formal_runs"] = [
            {"run_key": "cohort-a", "status": "failed", "artifact_paths": [paths[0]],
             "checkpoint_paths": checkpoints, "evaluations": []},
            {"run_key": "cohort-b", "status": "failed", "artifact_paths": [paths[1]],
             "checkpoint_paths": [], "evaluations": []},
        ]
        result_path.write_text(canonical_json(document))

        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)

        assert accepted.target_commit_ref
        assert [fact["run_key"] for fact in facts] == ["cohort-a", "cohort-b"]
        assert len({fact["variant_run_ref"] for fact in facts}) == 2
        produced = [next(artifact for artifact in fact["run_artifacts"]
                         if artifact["role"] == "data_asset") for fact in facts]
        assert len({artifact["role_ref"] for artifact in produced}) == 2
        retained = {entry.declared_relative_path: entry.binding.version_ref for entry in manifest.entries}
        assert [artifact["version_ref"] for artifact in produced] == [retained[path] for path in paths]
        for artifact, fact in zip(produced, facts):
            assert fact["variant_run"]["status"] == "failed"
            assert fact["evaluation_attempt"] is None
            assert fact["metric_result"] is None
            assert runtime.owners.research_memory.materialize_asset(artifact["version_ref"]).content == (
                b"observed\nnegative\n"
            )
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


def test_metric_summary_cannot_claim_retained_products_from_their_directories(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/data/external-observations.csv"
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"observed\nexternal\n")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="data", relative_path=relative),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        document.pop("formal_runs", None)
        result_path.write_text(canonical_json(document))

        result, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert isinstance(result, TargetRootOwnerRejection)
        assert result.code == "target_root_commit_domain_invalid"
        assert relative in result.feedback
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
        retained = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert runtime.owners.research_memory.materialize_asset(retained.binding.version_ref).content == (
            b"observed\nexternal\n"
        )
    finally:
        runtime.close()


def test_target_note_in_mixed_directory_does_not_hide_unassigned_report(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/analysis"
        source = workspace / relative
        source.mkdir(parents=True)
        (source / "research-note.md").write_text("Current Target understanding.\n", encoding="utf-8")
        (source / "assessment-report.md").write_text("Independent assessment found no effect.\n", encoding="utf-8")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=relative),
        )))

        result, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert isinstance(result, TargetRootOwnerRejection)
        assert result.code == "target_root_commit_domain_invalid"
        assert relative in result.feedback
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
        directory = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert directory.artifact_kind == "directory"
        assert directory.research_note["entry_path"] == "research-note.md"
        content = runtime.owners.research_memory.materialize_asset(directory.binding.version_ref).content
        with ZipFile(BytesIO(content)) as retained:
            assert retained.read("assessment-report.md") == b"Independent assessment found no effect.\n"
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()


def test_explicit_mixed_report_container_remains_exact_assessment_content(tmp_path, monkeypatch):
    from test_report_only_evaluation import _report_protocol

    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/analysis"
        source = workspace / relative
        source.mkdir(parents=True)
        (source / "research-note.md").write_text("Current Target understanding.\n", encoding="utf-8")
        (source / "assessment-report.md").write_text("Independent assessment found no effect.\n", encoding="utf-8")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=relative),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        document["formal_runs"][0]["evaluations"][0]["artifact_paths"] = [relative]
        result_path.write_text(canonical_json(document))

        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert accepted.target_commit_ref
        directory = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert directory.artifact_kind == "directory"
        assert directory.research_note["entry_path"] == "research-note.md"
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert directory.binding.version_ref in {
            artifact["version_ref"] for artifact in fact["evaluation_artifacts"]
        }
        assert directory.binding.version_ref not in {
            artifact["version_ref"] for artifact in fact["run_artifacts"]
        }
        assert fact["metric_result"]["metrics"] == {}
        assert directory.research_note["note_only"] is False
        assert directory.research_note["other_content_bytes"] == len(b"Independent assessment found no effect.\n")
        content = runtime.owners.research_memory.materialize_asset(directory.binding.version_ref).content
        with ZipFile(BytesIO(content)) as retained:
            assert retained.read("assessment-report.md") == b"Independent assessment found no effect.\n"
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()


def test_standalone_target_note_remains_manifest_content_without_a_producer(tmp_path):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/analysis/research-note.md"
        source = workspace / relative
        source.parent.mkdir(parents=True)
        source.write_text("Current Target understanding.\n", encoding="utf-8")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=relative),
        )))

        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert accepted.target_commit_ref
        note = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert "note_only" not in note.research_note
        assert "other_content_bytes" not in note.research_note
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert note.binding.version_ref not in {
            artifact["version_ref"] for artifact in (*fact["run_artifacts"], *fact["evaluation_artifacts"])
        }
        assert runtime.owners.research_memory.read_asset_entry_text(note.binding.version_ref) == (
            "Current Target understanding.\n"
        )
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
    finally:
        runtime.close()


@pytest.mark.parametrize("empty_report", [False, True], ids=["only-note", "empty-report"])
def test_target_note_only_directory_cannot_be_an_assessment_report(tmp_path, monkeypatch, empty_report):
    from test_report_only_evaluation import _report_protocol

    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        relative = "outputs/analysis"
        source = workspace / relative
        source.mkdir(parents=True)
        (source / "empty-context").mkdir()
        (source / "research-note.md").write_text("Current Target understanding.\n", encoding="utf-8")
        if empty_report:
            (source / "assessment-report.md").write_bytes(b"")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=relative),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        document["formal_runs"][0]["evaluations"][0]["artifact_paths"] = [relative]
        result_path.write_text(canonical_json(document))

        result, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert isinstance(result, TargetRootOwnerRejection)
        assert result.code == "target_root_commit_domain_invalid"
        assert "report-only assessment" in result.feedback
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
        directory = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert directory.research_note["note_only"] is (not empty_report)
        assert directory.research_note["other_content_bytes"] == 0
        with ZipFile(BytesIO(runtime.owners.research_memory.materialize_asset(
            directory.binding.version_ref
        ).content)) as retained:
            assert retained.read("research-note.md") == b"Current Target understanding.\n"
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()


@pytest.mark.parametrize("body", [b"Current Target understanding.\n", b"n" * (70 * 1024)],
                         ids=["inline-directory", "native-directory"])
def test_proven_note_only_directory_remains_target_content_without_a_producer(tmp_path, monkeypatch, body):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        if len(body) > 64 * 1024:
            import meta_research.target_run_finalizer as finalizer_module

            # Directory streaming uses encoded bundle size, not uncompressed
            # text size. Exercise the existing too-large packaging boundary.
            def streamed_directory(_descriptor):
                raise finalizer_module.TargetImplementationBundleError("target_implementation_bundle_too_large")

            monkeypatch.setattr(finalizer_module, "build_target_implementation_bundle_from_open_directory",
                                streamed_directory)
        relative = "outputs/analysis"
        source = workspace / relative
        source.mkdir(parents=True)
        (source / "empty-context").mkdir()
        (source / "research-note.md").write_bytes(body)
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=relative),
        )))

        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        assert accepted.target_commit_ref
        directory = next(entry for entry in manifest.entries if entry.declared_relative_path == relative)
        assert directory.research_note["note_only"] is True
        assert directory.research_note["other_content_bytes"] == 0
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert directory.binding.version_ref not in {
            artifact["version_ref"] for artifact in (*fact["run_artifacts"], *fact["evaluation_artifacts"])
        }
        asset_memory = runtime.owners.research_memory
        description = asset_memory.describe_asset_export(directory.binding.version_ref)
        assert description.kind == ("directory" if len(body) > 64 * 1024 else "file")
        if description.kind == "directory":
            assert asset_memory.read_asset_entry_text(
                directory.binding.version_ref, entry_path="research-note.md"
            ).encode("utf-8") == body
        else:
            with ZipFile(BytesIO(asset_memory.materialize_asset(
                directory.binding.version_ref
            ).content)) as retained:
                assert retained.read("research-note.md") == body
        assert _accept(runtime, lifecycle, memory, handle, evidence)[0] == accepted
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()
