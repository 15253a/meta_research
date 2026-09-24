"""Dataset declarations keep their precise content boundary through completion."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from sqlalchemy import text

from meta_research.formal_entities import verify_retained_products
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import (
    TargetRunFinalizer,
    _pin_workspace_root,
    _system_target_completion_handoff,
)
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


DATASET_PATH = "outputs/data/ad/ds004504-1.0.9"
PARENT_PATH = "outputs/data/ad"
OTHER_PATH = "outputs/data/ad/other-dataset"


def _candidate(path: str = DATASET_PATH) -> dict[str, str]:
    return {
        "artifact_path": path,
        "name": "ds004504 release 1.0.9",
        "purpose": "Reuse this exact released dataset, excluding other acquisitions.",
    }


def _data(workspace: Path) -> None:
    for relative, content in (
        (DATASET_PATH + "/samples.csv", "subject,value\nA,1\n"),
        (OTHER_PATH + "/samples.csv", "subject,value\nB,99\n"),
        (PARENT_PATH + "/download-notes.txt", "Notes for both downloads.\n"),
    ):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("extra_data_roots", [0, 31], ids=["small", "over-aggregation-limit"])
def test_automatic_discovery_preserves_nested_dataset_boundary_and_all_other_bytes(
    tmp_path: Path, extra_data_roots: int,
) -> None:
    (tmp_path / "implementation").mkdir()
    (tmp_path / "implementation/method.md").write_text("Download and audit data.")
    _data(tmp_path)
    for index in range(extra_data_roots):
        path = tmp_path / f"outputs/data/other-{index}/retained.txt"
        path.parent.mkdir(parents=True)
        path.write_text(str(index))
    document = {"metrics": {}, "dataset_candidates": [_candidate()]}
    (tmp_path / "outputs/result.json").write_text(canonical_json(document), encoding="utf-8")
    handle = SimpleNamespace(target_ref="target:dataset-boundary", target_run_ref="run:dataset-boundary")
    final_text = "Acquired the selected dataset release and retained other work."
    evidence = SimpleNamespace(
        final_text=final_text,
        final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest(),
    )
    pinned = _pin_workspace_root("workspace:dataset-boundary", tmp_path)
    try:
        handoff = _system_target_completion_handoff(
            handle=handle, evidence=evidence, root_descriptor=pinned.descriptor,
        )
    finally:
        os.close(pinned.descriptor)

    paths = [item.relative_path for item in handoff.artifacts]
    assert DATASET_PATH in paths, "The declared subdataset must remain independently addressable."
    assert next(item for item in handoff.artifacts if item.relative_path == DATASET_PATH).role == "data"
    assert not any(
        left != right and right.startswith(left + "/")
        for left in paths for right in paths
    ), "Exact subdataset assets must not overlap an aggregated parent asset."
    coverage: Counter[Path] = Counter()
    for relative in paths:
        path = tmp_path / relative
        coverage.update(item for item in path.rglob("*") if item.is_file()) if path.is_dir() else coverage.update([path])
    assert set(coverage) == {path for path in tmp_path.rglob("*") if path.is_file()}
    assert set(coverage.values()) == {1}
    assert json.loads((tmp_path / "outputs/result.json").read_text()) == document


def test_frozen_parent_dataset_mismatch_returns_feedback_and_can_be_corrected_in_same_run(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _data(workspace)
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        assert "formal_runs" not in document
        document["dataset_candidates"] = [_candidate()]
        result_path.write_text(canonical_json(document), encoding="utf-8")
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="data", relative_path=PARENT_PATH),
        )))
        kwargs = dict(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
        )
        # Model the deployed state: RM has already frozen the parent collection.
        seeded = TargetRunFinalizer(
            **kwargs, evidence_reader=_EvidenceReader(evidence),
        ).finalize(handle=handle, evidence=evidence)
        assert seeded.status == "rm_accepted"
        frozen = memory.query(seeded.manifest_ref)
        original_completion = lifecycle.query_completion(handle.target_ref)
        assert frozen is not None and original_completion is not None
        assert next(entry for entry in frozen.entries if entry.role == "data").declared_relative_path == PARENT_PATH
        finalizer = TargetRunFinalizer(
            **kwargs, evidence_reader=_EvidenceReader(evidence),
            graph_authority=runtime.owners.research_graph,
        )

        rejected = finalizer.finalize(handle=handle, evidence=evidence)

        assert rejected.status == "revision_required"
        assert rejected.rejection_issuer == "research_graph"
        assert DATASET_PATH in rejected.rejection_feedback
        assert PARENT_PATH in rejected.rejection_feedback
        assert lifecycle.query(handle.target_ref).status == "running"
        assert memory.query(seeded.manifest_ref) == frozen
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM rg_target_commits")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM rg_target_root_measurements")).scalar_one() == 0

        precise_handoff = replace(evidence.handoff, artifacts=(
            *(entry for entry in evidence.handoff.artifacts if entry.relative_path != PARENT_PATH),
            TargetCompletionArtifact(role="data", relative_path=DATASET_PATH),
            TargetCompletionArtifact(role="data", relative_path=OTHER_PATH),
            TargetCompletionArtifact(role="data", relative_path=PARENT_PATH + "/download-notes.txt"),
        ))
        successor = replace(
            evidence, handoff=precise_handoff,
            operation_ref="dataset-boundary-correction-turn",
            operation_generation=evidence.operation_generation + 1,
            evidence_ref="dataset-boundary-correction-evidence",
            evidence_sequence=evidence.evidence_sequence + 10,
            observed_at=evidence.observed_at + 1,
        )
        accepted = TargetRunFinalizer(
            **kwargs, evidence_reader=_EvidenceReader(successor),
            graph_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=successor)

        assert accepted.status == "completed"
        assert accepted.completion_generation == 2
        corrected = memory.query(accepted.manifest_ref)
        child = next(entry for entry in corrected.entries if entry.declared_relative_path == DATASET_PATH)
        exported = runtime.owners.research_memory.export_asset(child.binding.version_ref, tmp_path / "dataset-export")
        if exported.path.is_dir():
            exported_files = {path.relative_to(exported.path).as_posix() for path in exported.path.rglob("*") if path.is_file()}
        else:
            with ZipFile(exported.path) as archive:
                exported_files = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
                assert archive.read("samples.csv") == b"subject,value\nA,1\n"
        assert exported_files == {"samples.csv"}
        assert corrected.result_document.as_dict()["dataset_candidates"] == [_candidate()]
        assert memory.query(seeded.manifest_ref) == frozen
        current_completion = lifecycle.query_completion(handle.target_ref)
        assert current_completion.handle == original_completion.handle == handle
        assert current_completion.predecessor_completion_ref == original_completion.completion_ref
    finally:
        runtime.close()


def test_parent_directory_never_substitutes_for_the_declared_dataset_contents() -> None:
    document = {"metrics": {}, "dataset_candidates": [_candidate()]}
    before = deepcopy(document)
    entries = [{"role": "data", "declared_relative_path": PARENT_PATH}]

    with pytest.raises(OwnerConflict):
        verify_retained_products(document, entries)

    assert document == before


def test_overlapping_dataset_declarations_request_correction_instead_of_unchanged_retry() -> None:
    document = {
        "metrics": {},
        "dataset_candidates": [_candidate(PARENT_PATH), _candidate(DATASET_PATH)],
    }
    entries = [{"role": "data", "declared_relative_path": PARENT_PATH}]

    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, entries)

    assert failure.value.code == "target_root_commit_domain_invalid"
    feedback = failure.value.feedback.lower()
    assert "overlap" in feedback
    assert "keep dataset_candidates unchanged" not in feedback
    assert DATASET_PATH in failure.value.feedback


@pytest.mark.parametrize("path", ["outputs/data/ad/../missing", "/outputs/data/ad", "outputs\\data\\ad"])
def test_invalid_dataset_paths_request_canonical_path_correction(path: str) -> None:
    document = {"metrics": {}, "dataset_candidates": [_candidate(path)]}
    entries = [{"role": "data", "declared_relative_path": PARENT_PATH}]

    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, entries)

    assert failure.value.code == "target_root_commit_domain_invalid"
    feedback = failure.value.feedback.lower()
    assert "canonical" in feedback and "relative" in feedback
    assert "keep dataset_candidates unchanged" not in feedback
