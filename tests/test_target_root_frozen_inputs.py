from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.target_run_runtime import (
    FrozenTargetCommitInput,
    FrozenTargetCommitInputArtifact,
    SQLiteTargetRunAgentAuthority,
)
from meta_research.owners.agent_runtime_harness import TargetRootCompletionEvidence
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import decode_target_completion_handoff
from test_target_run_owner import _records


class _NoDirectAssetMemory:
    def materialize_input_asset(self, **_values):
        raise AssertionError("unexpected direct asset read")


class _Workspace(SQLiteTargetRunAgentAuthority):
    def __init__(self, root: Path, handle: object) -> None:
        self._root = root
        self._workspace_root = root.parent.resolve()
        self._current = handle
        self._memory = _NoDirectAssetMemory()
        self._workspace = SimpleNamespace(
            workspace_ref="workspace-downstream",
            target_run_ref=getattr(handle, "target_run_ref"),
            inputs_relative_path="inputs",
        )
        (root / "inputs").mkdir(parents=True)

    def verify_current_target_run_handle(self, handle):
        if handle != self._current:
            raise OwnerConflict("target_run_handle_not_current")
        return handle

    def query_target_workspace(self, target_run_ref: str):
        if target_run_ref != self._workspace.target_run_ref:
            return None
        return self._workspace

    def _ensure_target_workspace_layout(self, workspace):
        assert workspace is self._workspace
        return self._root

    def query_target_workspace_quest_ref(self, handle):
        self.verify_current_target_run_handle(handle)
        return "quest-frozen-inputs"

    def resolve_generic_target_commit_input(self, _transition):
        raise AssertionError("root manifest unexpectedly fell back to legacy RM")

    def resolve_target_workspace(self, **_values):
        return self._workspace.workspace_ref, self._root


class _Lifecycle:
    query_completion_calls = 0
    accept_completion_calls = 0

    def query_completion(self, _target_ref: str):
        self.query_completion_calls += 1
        return None

    def accept_completion(self, **_values):
        self.accept_completion_calls += 1
        raise AssertionError("tampered input crossed into AR completion")


class _Memory:
    accept_calls = 0

    def __init__(self, frozen: FrozenTargetCommitInput) -> None:
        self.frozen = frozen
        self.manifest = SimpleNamespace(manifest_ref=frozen.manifest_ref)

    def query(self, manifest_ref: str):
        return self.manifest if manifest_ref == self.frozen.manifest_ref else None

    def materialize_target_commit_input(self, *, target_commit_ref: str, manifest):
        assert target_commit_ref == self.frozen.target_commit_ref
        assert manifest is self.manifest
        return self.frozen

    def query_for_completion(self, _completion_ref: str):
        return None

    def accept(self, **_values):
        self.accept_calls += 1
        raise AssertionError("tampered input crossed into RM")


class _Graph:
    accept_calls = 0

    def __init__(self, frozen: FrozenTargetCommitInput) -> None:
        receipt = AcceptanceReceipt(
            issuer="research_graph",
            kind="target_commit_accepted",
            receipt_ref="rg-upstream-commit-receipt",
            subject_ref=frozen.target_commit_ref,
            payload_hash=canonical_hash({"commit_ref": frozen.target_commit_ref}),
        )
        self.commit = SimpleNamespace(
            commit_ref=frozen.target_commit_ref,
            target_ref=frozen.target_ref,
            target_run_ref=frozen.target_run_ref,
            receipt=receipt,
        )
        self.transition = SimpleNamespace(
            target_commit_ref=frozen.target_commit_ref,
            target_ref=frozen.target_ref,
            target_run_ref=frozen.target_run_ref,
            issuer_receipt=receipt,
            canonical_terminal=SimpleNamespace(asset_manifest_ref=frozen.manifest_ref),
        )

    def query_target_commits_for_quest(self, quest_ref: str):
        assert quest_ref == "quest-frozen-inputs"
        return (self.commit,)

    def query_target_frontier_commit_transition(self, target_ref: str):
        assert target_ref == self.commit.target_ref
        return self.transition

    def accept_target_commit_from_root_completion(self, **_values):
        self.accept_calls += 1
        raise AssertionError("tampered input crossed into RG")


class _EvidenceReader:
    def verify_target_root_completion_evidence(self, **_values):
        return canonical_hash({"evidence": "provider-drained"})


def _fixture(tmp_path: Path):
    _candidate, _plan, original, _preflight, _request = _records()
    handle = replace(
        original,
        accepted_input_target_commit_refs=("target-commit-upstream",),
        accepted_input_asset_proofs=(),
    )
    artifact_bytes = b'{"metric":1}\n'
    # The content binding is byte-addressed, unlike the semantic manifest hash.
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    manifest_content = canonical_json(
        {
            "manifest_ref": "rm-upstream-manifest",
            "target_ref": "target-upstream",
            "entries": [{"content_hash": artifact_hash, "role": "result"}],
        }
    ).encode("utf-8")
    frozen = FrozenTargetCommitInput(
        target_commit_ref="target-commit-upstream",
        target_ref="target-upstream",
        target_run_ref="target-run-upstream",
        manifest_ref="rm-upstream-manifest",
        manifest_payload_hash=canonical_hash({"manifest": "upstream"}),
        manifest_receipt_ref="rm-upstream-manifest-receipt",
        manifest_content=manifest_content,
        artifacts=(
            FrozenTargetCommitInputArtifact(
                ordinal=0,
                role="result",
                declared_relative_path="outputs/metrics.json",
                artifact_kind="file",
                media_type="application/json",
                version_ref="rm-upstream-result-version",
                content_hash=artifact_hash,
                tree_hash=artifact_hash,
                content=artifact_bytes,
            ),
        ),
    )
    workspace = _Workspace(tmp_path / "target-workspaces" / "workspace", handle)
    lifecycle = _Lifecycle()
    memory = _Memory(frozen)
    graph = _Graph(frozen)
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,  # type: ignore[arg-type]
        memory=memory,  # type: ignore[arg-type]
        workspace_resolver=workspace,
        evidence_reader=_EvidenceReader(),  # type: ignore[arg-type]
        graph_authority=graph,  # type: ignore[arg-type]
    )
    return handle, frozen, workspace, lifecycle, memory, graph, finalizer


def test_upstream_target_commit_manifest_and_rm_bytes_are_materialized(
    tmp_path: Path,
) -> None:
    handle, frozen, workspace, _lifecycle, _memory, _graph, finalizer = _fixture(
        tmp_path
    )

    paths = finalizer.materialize_inputs(handle=handle)

    artifact_path = next(path for path in paths if "/artifacts/" in path)
    manifest_path = paths[0]
    upstream_manifest_path = next(
        path
        for path in paths
        if "/upstream/" in path and path.endswith("/manifest.json")
    )
    assert Path(artifact_path).read_bytes() == frozen.artifacts[0].content
    assert Path(upstream_manifest_path).read_bytes() == frozen.manifest_content
    assert workspace._root not in Path(manifest_path).parents
    pointer = (workspace._root / "inputs" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert str(Path(manifest_path)) in pointer


def test_upstream_target_commit_requires_rg_issuer_receipt(tmp_path: Path) -> None:
    handle, _frozen, workspace, _lifecycle, _memory, graph, finalizer = _fixture(
        tmp_path
    )
    forged = AcceptanceReceipt(
        issuer="caller",
        kind="target_commit_accepted",
        receipt_ref="forged-upstream-commit-receipt",
        subject_ref=graph.commit.commit_ref,
        payload_hash=canonical_hash({"forged": True}),
    )
    graph.commit.receipt = forged
    graph.transition.issuer_receipt = forged

    with pytest.raises(OwnerConflict, match="target_root_upstream_input_invalid"):
        finalizer.materialize_inputs(handle=handle)

    assert tuple((workspace._root / "inputs").iterdir()) == ()


def test_tampered_frozen_input_blocks_all_completion_rm_and_rg_writes(
    tmp_path: Path,
) -> None:
    handle, _frozen, workspace, lifecycle, memory, graph, finalizer = _fixture(
        tmp_path
    )
    paths = finalizer.materialize_inputs(handle=handle)
    artifact_path = next(path for path in paths if "/artifacts/" in path)
    projected = Path(artifact_path)
    projected.chmod(0o600)
    projected.write_bytes(b"same-OS-root-agent-tamper\n")
    handoff = decode_target_completion_handoff(
        canonical_json(
            {
                "artifacts": [
                    {
                        "role": "implementation",
                        "relative_path": "implementation",
                    },
                    {"role": "result", "relative_path": "outputs/metrics.json"}
                ],
                "result_document_path": "outputs/metrics.json",
                "schema_ref": "meta-research/target-completion-handoff/v1",
                "status": "completed",
                "summary": "provider drained after completing the target",
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
            }
        )
    )
    evidence = TargetRootCompletionEvidence(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref,
        attempt_generation=1,
        root_session_ref=handle.root_session_ref,
        native_session_ref="native-frozen-input-test",
        fence_ref=handle.execution_fence_ref,
        operation_ref="root-final-operation",
        operation_generation=1,
        evidence_ref="root-final-evidence",
        evidence_sequence=1,
        handoff=handoff,
        observed_at=1.0,
    )

    with pytest.raises(
        OwnerConflict, match="target_run_workspace_input_integrity_invalid"
    ):
        finalizer.finalize(handle=handle, evidence=evidence)

    assert lifecycle.query_completion_calls == 0
    assert lifecycle.accept_completion_calls == 0
    assert memory.accept_calls == 0
    assert graph.accept_calls == 0
