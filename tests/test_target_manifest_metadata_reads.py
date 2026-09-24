from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

import meta_research.owners.research_memory as asset_module
from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.fixture
def accepted_manifest(tmp_path):
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    finalizer = TargetRunFinalizer(
        lifecycle=lifecycle,
        memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence),
    )
    try:
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        manifest = memory.query(accepted.manifest_ref)
        assert manifest is not None
        yield runtime, memory, manifest
    finally:
        runtime.close()


def _forbid_asset_content_reads(monkeypatch, owner):
    def unexpected(*args, **kwargs):
        pytest.fail("accepted manifest reads must not reopen asset contents")

    monkeypatch.setattr(owner, "materialize_asset", unexpected)
    monkeypatch.setattr(owner, "verify_asset_binding", unexpected)
    monkeypatch.setattr(asset_module, "_file_matches", unexpected)
    monkeypatch.setattr(asset_module, "_linked_source_matches", unexpected)


def test_manifest_query_and_upstream_projection_do_not_read_asset_bytes(
    accepted_manifest, monkeypatch,
):
    runtime, memory, manifest = accepted_manifest
    _forbid_asset_content_reads(monkeypatch, runtime.owners.research_memory)

    assert memory.query(manifest.manifest_ref) == manifest
    frozen = memory.materialize_target_commit_input(
        target_commit_ref="accepted-upstream-commit", manifest=manifest,
    )
    assert len(frozen.artifacts) == len(manifest.entries)
    for artifact, entry in zip(frozen.artifacts, manifest.entries, strict=True):
        assert artifact.content is None
        assert artifact.export_description.memory_ref == entry.binding.version_ref
        assert artifact.export_description.content_hash == entry.content_hash
        if entry.artifact_kind == "directory" and entry.media_type == "application/zip":
            assert artifact.export_description.kind == "file"


def test_manifest_metadata_read_still_rejects_changed_manifest_receipt(
    accepted_manifest, monkeypatch,
):
    runtime, memory, manifest = accepted_manifest
    _forbid_asset_content_reads(monkeypatch, runtime.owners.research_memory)
    with runtime._database.fenced_write() as connection:
        connection.execute(
            text("UPDATE rm_target_root_completion_manifests SET receipt_hash = :bad "
                 "WHERE manifest_ref = :manifest_ref"),
            {"bad": "0" * 64, "manifest_ref": manifest.manifest_ref},
        )

    with pytest.raises(OwnerConflict, match="target_root_manifest_integrity_invalid"):
        memory.query(manifest.manifest_ref)


@pytest.mark.parametrize("field,value", [
    ("memory_ref", "substituted-version"),
    ("content_hash", "0" * 64),
])
def test_manifest_metadata_read_still_binds_exact_asset_description(
    accepted_manifest, monkeypatch, field, value,
):
    runtime, memory, manifest = accepted_manifest
    owner = runtime.owners.research_memory
    describe = owner.describe_asset_export
    _forbid_asset_content_reads(monkeypatch, owner)
    monkeypatch.setattr(
        owner, "describe_asset_export",
        lambda version_ref: replace(describe(version_ref), **{field: value}),
    )

    with pytest.raises(OwnerConflict, match="target_root_manifest_integrity_invalid"):
        memory.query(manifest.manifest_ref)
