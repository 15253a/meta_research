"""Environment candidates use existing saved artifacts and their real producers."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from meta_research.formal_entities import verify_retained_products
from meta_research.owners.common import OwnerConflict, canonical_json


def _candidate(path):
    return {"artifact_path": path, "name": "Reusable simulator",
            "purpose": "Reuse the saved simulator and its operating conditions."}


@pytest.mark.parametrize("role,path", [
    ("data", "outputs/data/simulator"),
    ("analysis", "outputs/analysis/raw/environment.md"),
    ("implementation", "implementation"),
    ("checkpoint", "outputs/checkpoints/simulator.state"),
    ("log", "logs/train.log"),
])
def test_environment_candidate_uses_the_existing_retained_run_artifact(role, path):
    document = {"metrics": {"observed": 1}, "environment_candidates": [_candidate(path)]}
    entries = [{"role": role, "declared_relative_path": path}]
    before = deepcopy((document, entries))

    verify_retained_products(document, entries)

    assert (document, entries) == before


def test_dataset_and_environment_can_reference_one_exact_asset():
    path = "outputs/data/simulator-fixtures"
    document = {"metrics": {"observed": 1}, "dataset_candidates": [_candidate(path)],
                "environment_candidates": [_candidate(path)]}
    entries = [{"role": "data", "declared_relative_path": path}]

    verify_retained_products(document, entries)

    assert len(entries) == 1


def test_environment_implementation_keeps_its_actual_run_selection():
    entries = [{"role": "implementation", "declared_relative_path": "implementation/used"},
               {"role": "implementation", "declared_relative_path": "implementation/unused"}]
    document = {"formal_runs": [{"run_key": "construct", "evaluations": [],
                                "implementation_paths": ["implementation/used"]}],
                "environment_candidates": [_candidate("implementation/used")]}
    verify_retained_products(document, entries)

    document["environment_candidates"] = [_candidate("implementation/unused")]
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, entries)
    assert "actual Run" in failure.value.feedback


def test_reused_run_cannot_claim_new_environment_implementation():
    document = {"formal_runs": [{"run_key": "reuse", "variant_run_ref": "accepted-run",
                                "evaluations": []}],
                "environment_candidates": [_candidate("implementation")]}
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, [{"role": "implementation", "declared_relative_path": "implementation"}])
    assert "actual Run" in failure.value.feedback


@pytest.mark.parametrize("candidate", [None, {}, {"artifact_path": "implementation", "name": " ", "purpose": "x"}])
def test_environment_candidate_requires_its_meaning(candidate):
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products({"metrics": {"observed": 1}, "environment_candidates": [candidate]}, [])
    assert "environment_candidates" in failure.value.feedback


@pytest.mark.parametrize("candidates", [None, {}, [_candidate("implementation")] * 101])
def test_environment_candidate_inventory_is_bounded(candidates):
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products({"metrics": {"observed": 1}, "environment_candidates": candidates}, [])
    assert "at most 100" in failure.value.feedback


@pytest.mark.parametrize("path", ["/implementation", "implementation/../other", "implementation\\simulator"])
def test_environment_candidate_requires_canonical_relative_path(path):
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products({"metrics": {"observed": 1}, "environment_candidates": [_candidate(path)]}, [])
    assert "canonical relative" in failure.value.feedback


@pytest.mark.parametrize("other_field", ["dataset_candidates", "environment_candidates"])
def test_candidate_parent_child_overlap_is_rejected_across_both_indexes(other_field):
    document = {"metrics": {"observed": 1}, "environment_candidates": [_candidate("outputs/data/environment")]}
    document.setdefault(other_field, []).append(_candidate("outputs/data/environment/fixtures"))
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products(document, [{"role": "data", "declared_relative_path": "outputs/data/environment"}])
    assert "overlap" in failure.value.feedback
    assert "unchanged" not in failure.value.feedback


def test_environment_subpath_cannot_substitute_for_an_existing_implementation_snapshot():
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products({"metrics": {"observed": 1},
            "environment_candidates": [_candidate("implementation/simulator.py")]},
            [{"role": "implementation", "declared_relative_path": "implementation"}])
    assert "existing manifest entry" in failure.value.feedback
    assert "keep environment_candidates unchanged" not in failure.value.feedback


def test_environment_data_subpath_requests_a_new_exact_completion_boundary():
    with pytest.raises(OwnerConflict) as failure:
        verify_retained_products({"metrics": {"observed": 1},
            "environment_candidates": [_candidate("outputs/data/environment")]},
            [{"role": "data", "declared_relative_path": "outputs/data"}])
    assert "keep environment_candidates unchanged" in failure.value.feedback


@pytest.mark.skipif(os.name != "posix", reason="Completion discovery uses POSIX directory descriptors")
def test_discovery_keeps_one_shared_candidate_boundary_and_all_neighboring_contents(tmp_path):
    from meta_research.target_run_finalizer import _pin_workspace_root, _system_target_completion_handoff

    path = "outputs/data/collected/environment"
    for name in ("implementation/simulator.py", path + "/fixtures.csv", "outputs/data/collected/other/data.csv"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(name)
    document = {"metrics": {"observed": 1}, "dataset_candidates": [_candidate(path)],
                "environment_candidates": [_candidate(path), _candidate("implementation")]}
    (tmp_path / "outputs/result.json").write_text(canonical_json(document))
    handle = SimpleNamespace(target_ref="environment-target", target_run_ref="environment-run")
    text = "Retained simulator and fixtures."
    evidence = SimpleNamespace(final_text=text, final_text_sha256=hashlib.sha256(text.encode()).hexdigest())
    pinned = _pin_workspace_root("workspace:environment", tmp_path)
    try:
        handoff = _system_target_completion_handoff(handle=handle, evidence=evidence, root_descriptor=pinned.descriptor)
    finally:
        os.close(pinned.descriptor)

    paths = [entry.relative_path for entry in handoff.artifacts]
    assert paths.count(path) == 1
    assert paths.count("implementation") == 1
    coverage = Counter()
    for relative in paths:
        file = tmp_path / relative
        coverage.update(p for p in file.rglob("*") if p.is_file()) if file.is_dir() else coverage.update([file])
    assert set(coverage) == {p for p in tmp_path.rglob("*") if p.is_file()}
    assert set(coverage.values()) == {1}
    verify_retained_products(document, [{"role": entry.role, "declared_relative_path": entry.relative_path}
                                       for entry in handoff.artifacts])


@pytest.mark.skipif(os.name != "posix", reason="Target runtime uses POSIX process and descriptor APIs")
def test_environment_candidate_survives_real_target_acceptance_with_original_implementation(tmp_path):
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture

    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        document["environment_candidates"] = [_candidate("implementation")]
        path.write_text(canonical_json(document))
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph, evidence_reader=_EvidenceReader(evidence))

        completed = finalizer.finalize(handle=handle, evidence=evidence)

        assert completed.status == "completed"
        manifest = memory.query(completed.manifest_ref)
        assert manifest.result_document.as_dict()["environment_candidates"] == document["environment_candidates"]
        assert len([entry for entry in manifest.entries if entry.declared_relative_path == "implementation"]) == 1
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert facts[0]["variant_run"]["implementation_revision_ref"] == manifest.implementation_revision_ref
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
    finally:
        runtime.close()
