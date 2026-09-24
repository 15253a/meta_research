"""Accepted completion replay must use its immutable RM snapshot."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research.owners.agent_runtime_harness import TargetRootCompletionEvidence
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.target_run_runtime_contract import TargetCompletionArtifact, TargetCompletionHandoff
from test_target_root_finalizer import _GraphAcceptance
from test_target_root_frozen_inputs import _fixture


def _replay_fixture(tmp_path, monkeypatch):
    handle, frozen, workspace, lifecycle, memory, graph, finalizer = _fixture(tmp_path)
    paths = finalizer.materialize_inputs(handle=handle)
    handoff = TargetCompletionHandoff(
        schema_ref="meta-research/target-completion-handoff/v1",
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        status="completed", summary="Finished research.",
        artifacts=(TargetCompletionArtifact("implementation", "implementation"),
                   TargetCompletionArtifact("result", "outputs/result.json")),
        result_document_path="outputs/result.json",
    )
    evidence = TargetRootCompletionEvidence(
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref, attempt_generation=1,
        root_session_ref=handle.root_session_ref, native_session_ref="native-replay",
        fence_ref=handle.execution_fence_ref, operation_ref="operation-replay",
        operation_generation=1, evidence_ref="evidence-replay", evidence_sequence=1,
        handoff=handoff, observed_at=1.0,
    )
    completion = SimpleNamespace(
        handle=handle, completion_ref="accepted-completion", generation=1,
        harness_operation_ref=evidence.operation_ref, evidence_ref=evidence.evidence_ref,
        evidence_content_hash=canonical_hash({"evidence": "provider-drained"}),
        handoff=handoff, workspace_ref=workspace._workspace.workspace_ref,
        candidate_rejection_code=None,
    )
    manifest = SimpleNamespace(manifest_ref="accepted-manifest",
        artifact_snapshot_hash="a" * 64, result_document=SimpleNamespace(metrics={"value": 1}))
    monkeypatch.setattr(lifecycle, "query_completion", lambda _target: completion)
    monkeypatch.setattr(lifecycle, "query_completion_rejection", lambda _ref: None, raising=False)
    monkeypatch.setattr(memory, "query_for_completion", lambda _ref: manifest)
    monkeypatch.setattr(graph, "accept_target_commit_from_root_completion", _GraphAcceptance().accept_target_commit_from_root_completion)
    return handle, evidence, completion, manifest, workspace, lifecycle, memory, finalizer, paths


def test_accepted_completion_replay_never_resolves_or_scans_live_inputs(tmp_path, monkeypatch):
    handle, evidence, completion, manifest, workspace, lifecycle, memory, finalizer, paths = _replay_fixture(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("accepted completion replay reopened inputs or outputs")
    monkeypatch.setattr(finalizer, "_resolve_target_commit_inputs", forbidden)
    monkeypatch.setattr(workspace, "verify_target_workspace_inputs", forbidden)
    monkeypatch.setattr(finalizer, "_freeze", forbidden)
    first = finalizer.finalize(handle=handle, evidence=evidence)
    assert first.status == "completed"
    assert first.completion_ref == completion.completion_ref
    assert first.manifest_ref == manifest.manifest_ref
    assert finalizer.finalize(handle=handle, evidence=evidence) == first


def test_rejected_completion_replay_never_resolves_live_inputs(tmp_path, monkeypatch):
    handle, evidence, completion, manifest, workspace, lifecycle, memory, finalizer, paths = _replay_fixture(tmp_path, monkeypatch)
    rejection = SimpleNamespace(target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        completion_ref=completion.completion_ref, manifest_ref=manifest.manifest_ref,
        code="research_revision_required", generation=1, rejection_ref="accepted-rejection",
        issuer="research_graph", feedback="Correct the reported result.")
    monkeypatch.setattr(lifecycle, "query_completion_rejection", lambda _ref: rejection)
    monkeypatch.setattr(finalizer, "_resolve_target_commit_inputs", lambda _handle: pytest.fail("rejection replay reconstructed inputs"))
    result = finalizer.finalize(handle=handle, evidence=evidence)
    assert result.status == "revision_required"
    assert result.rejection_ref == rejection.rejection_ref


def test_ar_only_retry_still_rejects_changed_frozen_input_before_writes(tmp_path, monkeypatch):
    handle, evidence, completion, manifest, workspace, lifecycle, memory, finalizer, paths = _replay_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(memory, "query_for_completion", lambda _ref: None)
    projected = next(Path(path) for path in paths if "/artifacts/" in path)
    projected.chmod(0o600)
    projected.write_bytes(b"changed before RM accepted the snapshot")
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        finalizer.finalize(handle=handle, evidence=evidence)
    assert lifecycle.accept_completion_calls == 0
    assert memory.accept_calls == 0


def test_different_evidence_cannot_reuse_an_accepted_completion(tmp_path, monkeypatch):
    handle, evidence, completion, manifest, workspace, lifecycle, memory, finalizer, paths = _replay_fixture(tmp_path, monkeypatch)
    with pytest.raises(OwnerConflict, match="target_root_completion_conflict"):
        finalizer.finalize(handle=handle, evidence=replace(evidence, evidence_ref="other-evidence"))


def test_local_input_analysis_files_do_not_change_accepted_frozen_custody(tmp_path):
    handle, frozen, workspace, lifecycle, memory, graph, finalizer = _fixture(tmp_path)
    finalizer.materialize_inputs(handle=handle)
    local = workspace._root / "inputs" / "upstream_baseline"
    local.parent.chmod(0o700)
    local.mkdir()
    (local / "normalization-cache.json").write_bytes(b'{"local_analysis":true}')
    workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))


@pytest.mark.parametrize("change", ["content", "symlink", "directory"])
def test_local_input_pointer_still_cannot_redirect_frozen_custody(tmp_path, change):
    handle, frozen, workspace, lifecycle, memory, graph, finalizer = _fixture(tmp_path)
    finalizer.materialize_inputs(handle=handle)
    pointer = workspace._root / "inputs" / "manifest.json"
    pointer.parent.chmod(0o700)
    if change == "content":
        pointer.chmod(0o600)
        pointer.write_bytes(b'{"read_only_manifest_path":"/other/manifest.json"}')
    elif change == "symlink":
        other = tmp_path / "other-manifest.json"
        other.write_bytes(pointer.read_bytes())
        pointer.unlink()
        pointer.symlink_to(other)
    else:
        pointer.unlink()
        pointer.mkdir()
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))


def test_unaccepted_files_in_frozen_input_tree_remain_rejected(tmp_path):
    handle, frozen, workspace, lifecycle, memory, graph, finalizer = _fixture(tmp_path)
    paths = finalizer.materialize_inputs(handle=handle)
    frozen_root = Path(paths[0]).parent
    frozen_root.chmod(0o700)
    (frozen_root / "unexpected-analysis.json").write_bytes(b"{}")
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))
