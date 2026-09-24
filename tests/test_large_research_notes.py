from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import tracemalloc

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.research_notes import (
    manifest_research_notes, materialize_note_body, read_note_body,
    research_note_metadata_from_path,
)
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _write_note(path: Path, size: int) -> str:
    chunk = ("研究记录：仍有不确定性，下一轮复核原始数据。\n" * 4096).encode()
    digest = hashlib.sha256()
    with path.open("wb") as output:
        while size >= len(chunk):
            output.write(chunk)
            digest.update(chunk)
            size -= len(chunk)
        tail = b"." * size
        output.write(tail)
        digest.update(tail)
    return digest.hexdigest()


def _bound_note(metadata, asset):
    return {**metadata, "version_ref": asset.version_ref,
            "asset_content_hash": asset.content_hash,
            "asset_manifest_hash": asset.manifest_hash}


@pytest.mark.parametrize("size", [70 * 1024, 65 * 1024 * 1024],
                         ids=["over-64-kib", "over-64-mib"])
def test_long_note_has_bounded_identity_and_explicit_exact_read_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int,
) -> None:
    source = tmp_path / "research-note.md"
    digest = _write_note(source, size)
    tracemalloc.start()
    metadata = research_note_metadata_from_path(
        role="analysis", declared_relative_path="outputs/analysis/research-note.md",
        artifact_kind="file", source_path=source, tree_hash=digest,
    )
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert metadata is not None
    assert metadata["kind"] == "research_note"
    assert metadata["source_bytes_sha256"] == digest
    assert metadata["source_utf8_bytes"] == size
    assert metadata["summary"]["truncated"]
    assert len(metadata["summary"]["text"].encode()) <= 2048
    assert "\ufffd" not in metadata["summary"]["text"]
    assert peak < 16 * 1024 * 1024

    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        intake = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="local_path", custody_mode="managed", display_name=source.name,
            source_locator=str(source),
        ), idempotency_key="long-note")
        assert intake.asset is not None
        note = _bound_note(metadata, intake.asset)
        source.write_text("workspace changed", encoding="utf-8")

        def no_bounded_bytes_or_whole_asset_export(*_args, **_kwargs):
            raise AssertionError("note reader must stream its exact entry")

        monkeypatch.setattr(memory, "materialize_asset", no_bounded_bytes_or_whole_asset_export)
        monkeypatch.setattr(memory, "export_asset", no_bounded_bytes_or_whole_asset_export)
        body = read_note_body(memory, note)
        assert hashlib.sha256(body.encode()).hexdigest() == digest
        assert len(body.encode()) == size
        del body
        exported = Path(materialize_note_body(memory, note, tmp_path / "reading-context"))
        with exported.open("rb") as content:
            assert hashlib.file_digest(content, "sha256").hexdigest() == digest
        assert materialize_note_body(memory, note, tmp_path / "reading-context") == str(exported)
    finally:
        runtime.close()


def test_native_analysis_directory_exports_only_its_precise_note_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "analysis"
    source.mkdir()
    digest = _write_note(source / "research-note.md", 70 * 1024)
    (source / "raw-data.bin").write_bytes(b"\xff\x00" * 1024)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    memory = runtime.owners.research_memory
    try:
        intake = memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="directory", custody_mode="managed", display_name="analysis",
            source_locator=str(source),
        ), idempotency_key="analysis-tree")
        assert intake.asset is not None
        metadata = research_note_metadata_from_path(
            role="analysis", declared_relative_path="outputs/analysis",
            artifact_kind="directory", source_path=source,
            tree_hash=intake.asset.content_hash,
        )
        assert metadata["entry_path"] == "research-note.md"
        note = _bound_note(metadata, intake.asset)
        note_entry = next(entry for entry in memory.describe_asset_export(
            intake.asset.version_ref).entries if entry.path == note["entry_path"])
        assert (note_entry.sha256, note_entry.size) == (digest, 70 * 1024)
        assert hashlib.sha256(read_note_body(memory, note).encode()).hexdigest() == digest

        def no_whole_dataset_copy(*_args, **_kwargs):
            raise AssertionError("reading note must not export containing analysis dataset")

        monkeypatch.setattr(memory, "export_asset", no_whole_dataset_copy)
        reading = tmp_path / "reading-context"
        path = Path(materialize_note_body(memory, note, reading))
        assert list(reading.iterdir()) == [path]
        assert not (reading / "raw-data.bin").exists()
    finally:
        runtime.close()


def test_note_metadata_validates_utf8_beyond_the_summary_prefix(tmp_path: Path) -> None:
    source = tmp_path / "research-note.md"
    source.write_bytes(b"valid introduction\n" * 10000 + b"\xff")
    with source.open("rb") as content:
        digest = hashlib.file_digest(content, "sha256").hexdigest()
    assert research_note_metadata_from_path(
        role="analysis", declared_relative_path="outputs/analysis/research-note.md",
        artifact_kind="file", source_path=source, tree_hash=digest,
    ) is None


@pytest.mark.parametrize("directory", [False, True], ids=["standalone", "native-directory"])
def test_finalizer_keeps_long_note_identity_across_query_and_replay(tmp_path: Path, directory: bool) -> None:
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        analysis = workspace / "outputs/analysis"
        analysis.mkdir()
        source = analysis / "research-note.md"
        digest = _write_note(source, 70 * 1024)
        if directory:
            (analysis / "raw-data.bin").write_bytes(b"\xff\x00")
        declared = "outputs/analysis" if directory else "outputs/analysis/research-note.md"
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="analysis", relative_path=declared),
        )))
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
        )
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        manifest = memory.query(accepted.manifest_ref)
        note = next(note for note in manifest_research_notes(manifest)
                    if note["declared_relative_path"] == declared)
        assert note["source_bytes_sha256"] == digest
        assert note["source_utf8_bytes"] == 70 * 1024
        assert note["entry_path"] == ("research-note.md" if directory else None)
        assert len(note["summary"]["text"].encode()) <= 2048
        source.unlink()
        assert finalizer.finalize(handle=handle, evidence=evidence) == accepted
        assert manifest_research_notes(memory.query(accepted.manifest_ref)) == manifest_research_notes(manifest)
        reading_path = Path(materialize_note_body(
            runtime.target_run_authorities.research_memory, note, tmp_path / "target-reading-context"
        ))
        with reading_path.open("rb") as content:
            assert hashlib.file_digest(content, "sha256").hexdigest() == digest
    finally:
        runtime.close()
