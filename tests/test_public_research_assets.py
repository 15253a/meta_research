from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import zipfile

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
import meta_research.owners.research_memory as research_memory_module
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root


def test_managed_text_intake_is_exact_readable_and_idempotent(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "text-intake"))
    request = AssetIntakeRequest(
        source_kind="text",
        custody_mode="managed",
        display_name="实验观察.md",
        media_type="text/markdown; charset=utf-8",
        content=b"# Observation\n\nThe rare morphology remained visible.\n",
        provenance={"origin": "research-lead", "note": "copied from lab notes"},
    )
    try:
        accepted = runtime.owners.research_memory.submit_asset_intake(
            request, idempotency_key="managed-text-1"
        )

        assert accepted.status == "accepted"
        assert accepted.asset is not None
        assert accepted.asset.memory_ref == accepted.asset.version_ref
        assert accepted.asset.version_number == 1
        assert accepted.asset.source_kind == "text"
        assert accepted.asset.custody_modes == ("managed",)
        assert accepted.asset.receipt.issuer == "research_memory"
        assert accepted.asset.receipt.kind == "asset_acceptance"

        replay = runtime.owners.research_memory.submit_asset_intake(
            request, idempotency_key="managed-text-1"
        )
        assert replay == accepted

        inventory = runtime.owners.research_memory.query_asset_inventory()
        assert [item.version_ref for item in inventory] == [
            accepted.asset.version_ref
        ]
        assert inventory[0].integrity == "verified"
        assert inventory[0].availability == "available"

        materialized = runtime.owners.research_memory.materialize_asset(
            accepted.asset.memory_ref
        )
        assert materialized.file_name == "实验观察.md"
        assert materialized.media_type == "text/markdown; charset=utf-8"
        assert materialized.content == request.content

        with pytest.raises(OwnerConflict, match="asset_intake_idempotency_conflict"):
            runtime.owners.research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    **{
                        **request.as_dict(),
                        "content": b"different bytes",
                    }
                ),
                idempotency_key="managed-text-1",
            )
    finally:
        runtime.close()


def test_healthy_managed_handoff_alias_keeps_projection_revision_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "managed-handoff-alias")
    )
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="already-managed.txt",
                content=b"already managed exact bytes\n",
            ),
            idempotency_key="managed-handoff-alias-intake",
        )
        assert intake.asset is not None
        before = runtime.projection.query_snapshot()
        observed_at = before["research_assets"]["items"][0][
            "verification_observed_at"
        ]
        assert isinstance(observed_at, float)
        monkeypatch.setattr(
            research_memory_module.time,
            "time",
            lambda: observed_at + 120.0,
        )

        custody = runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="managed-handoff-alias-command",
        )
        after = runtime.projection.query_snapshot()

        assert custody.custody_mode == "managed"
        assert after["revision"] == before["revision"]
        assert {
            key: value
            for key, value in after.items()
            if key != "runtime_observability"
        } == {
            key: value
            for key, value in before.items()
            if key != "runtime_observability"
        }
        before_runtime = before["runtime_observability"]
        after_runtime = after["runtime_observability"]
        assert isinstance(before_runtime, dict)
        assert isinstance(after_runtime, dict)
        assert {
            key: value for key, value in after_runtime.items() if key != "log"
        } == {
            key: value for key, value in before_runtime.items() if key != "log"
        }
        before_log = before_runtime["log"]
        after_log = after_runtime["log"]
        assert isinstance(before_log, dict)
        assert isinstance(after_log, dict)
        assert after_log["status"] == before_log["status"]
        assert after_log["last_recorded_at"] == before_log["last_recorded_at"]
        assert after_log["age_seconds"] >= before_log["age_seconds"]
    finally:
        runtime.close()


def test_materialization_reads_and_verifies_the_same_bounded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = prepare_data_root(tmp_path / "exact-materialization")
    runtime = build_production_runtime(data_root)
    payload = b"exact immutable bytes\n"
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="exact.txt",
                content=payload,
            ),
            idempotency_key="exact-materialization-intake",
        )
        assert intake.asset is not None
        object_path = (
            data_root.objects
            / "assets"
            / intake.asset.content_hash[:2]
            / intake.asset.content_hash
        )
        original_read_bytes = Path.read_bytes

        def substitute_after_verification(path: Path) -> bytes:
            if path == object_path:
                return b"bytes changed after the separate hash check\n"
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", substitute_after_verification)

        materialized = runtime.owners.research_memory.materialize_asset(
            intake.asset.memory_ref
        )
        assert materialized.content == payload
    finally:
        runtime.close()


def test_legacy_oversized_asset_fails_before_any_corpus_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "legacy-oversized-asset")
    )
    research_memory = runtime.owners.research_memory
    try:
        intake = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="legacy-large.bin",
                content=b"small fixture standing in for a pre-ceiling object",
            ),
            idempotency_key="legacy-oversized-intake",
        )
        assert intake.asset is not None
        version_ref = intake.asset.memory_ref
        with runtime._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rm_asset_versions WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).one()
            custody = connection.execute(
                text(
                    "SELECT * FROM rm_asset_custodies WHERE version_ref = "
                    ":version_ref"
                ),
                {"version_ref": version_ref},
            ).one()
        manifest = json.loads(row.manifest_json)
        manifest["entries"][0]["size"] = (
            research_memory_module.MAX_ASSET_BYTES + 1
        )
        manifest_json = research_memory_module.canonical_json(manifest)
        manifest_hash = research_memory_module.canonical_hash(manifest)
        acceptance_hash = research_memory_module._receipt_hash(
            research_memory_module.ASSET_RECEIPT_KIND,
            version_ref,
            {
                "asset_ref": row.asset_ref,
                "version_number": int(row.version_number),
                "source_kind": row.source_kind,
                "display_name": row.display_name,
                "media_type": row.media_type,
                "content_hash": row.content_hash,
                "manifest_hash": manifest_hash,
                "byte_count": research_memory_module.MAX_ASSET_BYTES + 1,
                "provenance_hash": row.provenance_hash,
                "custody_modes": ["managed"],
            },
        )
        custody_hash = research_memory_module._receipt_hash(
            research_memory_module.ASSET_CUSTODY_ESTABLISHED_RECEIPT_KIND,
            custody.custody_ref,
            {
                "version_ref": version_ref,
                "content_hash": row.content_hash,
                "manifest_hash": manifest_hash,
                "custody_mode": "managed",
                "source_locator": None,
            },
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_versions SET manifest_json = :manifest_json, "
                    "manifest_hash = :manifest_hash, byte_count = :byte_count, "
                    "receipt_hash = :receipt_hash WHERE version_ref = :version_ref"
                ),
                {
                    "version_ref": version_ref,
                    "manifest_json": manifest_json,
                    "manifest_hash": manifest_hash,
                    "byte_count": research_memory_module.MAX_ASSET_BYTES + 1,
                    "receipt_hash": acceptance_hash,
                },
            )
            connection.execute(
                text(
                    "UPDATE rm_asset_custodies SET receipt_hash = :receipt_hash "
                    "WHERE custody_ref = :custody_ref"
                ),
                {
                    "custody_ref": custody.custody_ref,
                    "receipt_hash": custody_hash,
                },
            )

        def unexpected_corpus_io(*_args, **_kwargs):
            raise AssertionError("legacy oversized asset touched corpus bytes")

        monkeypatch.setattr(
            research_memory_module,
            "_asset_current_state",
            unexpected_corpus_io,
        )
        monkeypatch.setattr(
            research_memory_module,
            "_verify_managed_manifest",
            unexpected_corpus_io,
        )

        with pytest.raises(
            OwnerConflict, match="asset_materialization_unsupported"
        ):
            research_memory.materialize_asset(version_ref)
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            research_memory.handoff_asset_to_managed(
                version_ref,
                idempotency_key="legacy-oversized-handoff",
            )
    finally:
        runtime.close()


def test_projection_uses_durable_observations_and_background_verifies_one_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = prepare_data_root(tmp_path / "bounded-projection")
    runtime = build_production_runtime(data_root)
    research_memory = runtime.owners.research_memory
    try:
        intake = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="bounded.txt",
                content=b"bounded Projection bytes\n",
            ),
            idempotency_key="bounded-projection-intake",
        )
        assert intake.asset is not None
        object_path = (
            data_root.objects
            / "assets"
            / intake.asset.content_hash[:2]
            / intake.asset.content_hash
        )
        original_deep_query = research_memory.query_asset_inventory

        def reject_hot_deep_scan():
            raise AssertionError("Projection must not deep-scan every AssetVersion")

        monkeypatch.setattr(
            research_memory, "query_asset_inventory", reject_hot_deep_scan
        )
        projected = runtime.projection.query_snapshot()["research_assets"]
        assert projected["items"][0]["integrity"] == "verified"
        assert projected["items"][0]["availability"] == "available"

        monkeypatch.setattr(
            research_memory, "query_asset_inventory", original_deep_query
        )
        object_path.write_bytes(b"corrupt after the acceptance observation\n")
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_verification_observations SET "
                    "next_verify_at = 0 WHERE version_ref = :version_ref"
                ),
                {"version_ref": intake.asset.memory_ref},
            )
        assert research_memory.verify_asset_inventory_once()
        refreshed = runtime.projection.query_snapshot()
        assert refreshed["research_assets"]["items"][0]["integrity"] == (
            "failed"
        )
        assert refreshed["owners"]["research_memory"]["status"] == (
            "unavailable"
        )
    finally:
        runtime.close()


def test_projection_fails_closed_when_an_off_page_observation_is_missing(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "missing-off-page-observation")
    )
    research_memory = runtime.owners.research_memory
    try:
        accepted_refs: list[str] = []
        for index in range(2):
            accepted = research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name=f"observation-{index}.txt",
                    content=f"observation {index}\n".encode(),
                ),
                idempotency_key=f"off-page-observation-{index}",
            )
            assert accepted.asset is not None
            accepted_refs.append(accepted.asset.memory_ref)

        first_page = runtime.projection.query_snapshot(asset_limit=1)
        assert first_page["owners"]["research_memory"]["status"] == "ready"
        assert first_page["research_assets"]["items"][0]["memory_ref"] == (
            accepted_refs[1]
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "DELETE FROM rm_asset_verification_observations WHERE "
                    "version_ref = :version_ref"
                ),
                {"version_ref": accepted_refs[0]},
            )

        failed_closed = runtime.projection.query_snapshot(asset_limit=1)
        assert failed_closed["research_assets"]["items"][0]["integrity"] == (
            "verified"
        )
        assert failed_closed["owners"]["research_memory"]["status"] == (
            "unavailable"
        )
        assert failed_closed["readiness"]["status"] == "unavailable"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "intake_request",
    [
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name="ambiguous.txt",
            content=b"submitted text",
            source_locator="/tmp/ignored.txt",
        ),
        AssetIntakeRequest(
            source_kind="repository",
            custody_mode="managed",
            display_name="fake-repository",
            content=b"not a repository",
        ),
        AssetIntakeRequest(
            source_kind="directory",
            custody_mode="managed",
            display_name="fake-directory",
            content=b"not a directory",
        ),
        AssetIntakeRequest(
            source_kind="local_path",
            custody_mode="linked_local",
            display_name="relative.txt",
            source_locator="relative/path.txt",
        ),
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name="C:poison.txt",
            content=b"must be rejected before any durable side effect",
        ),
    ],
)
def test_intake_rejects_ambiguous_source_kind_payloads_before_queueing(
    tmp_path: Path, intake_request: AssetIntakeRequest
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "source-matrix"))
    try:
        before = runtime.owners.research_memory.query_snapshot()
        with pytest.raises(OwnerConflict):
            runtime.owners.research_memory.submit_asset_intake(
                intake_request,
                idempotency_key="invalid-source-matrix",
            )
        after = runtime.owners.research_memory.query_snapshot()
        assert after.revision == before.revision
        assert after.facts["pending_intake_count"] == 0
        assert runtime.owners.research_memory.query_asset_inventory() == ()
    finally:
        runtime.close()


def test_intake_rejects_unbounded_provenance_before_any_durable_side_effect(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "provenance-ceiling")
    )
    research_memory = runtime.owners.research_memory
    try:
        before = research_memory.query_snapshot()
        with pytest.raises(OwnerConflict, match="asset_provenance_too_large"):
            research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name="bounded.txt",
                    content=b"small content\n",
                    provenance={"oversized": "x" * (64 * 1024)},
                ),
                idempotency_key="oversized-provenance",
            )
        assert research_memory.query_snapshot() == before
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rm_asset_intakes")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_async_intake_queue_has_a_durable_admission_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "intake-queue-ceiling")
    )
    research_memory = runtime.owners.research_memory
    request = AssetIntakeRequest(
        source_kind="text",
        custody_mode="managed",
        display_name="queued.txt",
        content=b"bounded queue entry\n",
        asynchronous=True,
    )
    monkeypatch.setattr(research_memory_module, "MAX_PENDING_ASSET_INTAKES", 1)
    try:
        queued = research_memory.submit_asset_intake(
            request, idempotency_key="queue-ceiling-first"
        )
        assert queued.status == "queued"
        assert (
            research_memory.submit_asset_intake(
                request, idempotency_key="queue-ceiling-first"
            )
            == queued
        )
        with pytest.raises(OwnerConflict, match="asset_intake_queue_full"):
            research_memory.submit_asset_intake(
                request, idempotency_key="queue-ceiling-second"
            )
        assert research_memory.query_snapshot().facts["pending_intake_count"] == 1
    finally:
        runtime.close()


def test_directory_ceiling_counts_nested_empty_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "nested-empty-directories"
    (source / "one" / "two" / "three").mkdir(parents=True)
    monkeypatch.setattr(research_memory_module, "MAX_ASSET_FILES", 2)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "directory-entry-ceiling")
    )
    try:
        result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="nested-empty-directories",
                source_locator=str(source.resolve()),
            ),
            idempotency_key="directory-entry-ceiling",
        )

        assert result.status == "failed"
        assert result.failure_code == "asset_source_too_large"
        assert runtime.owners.research_memory.query_asset_inventory() == ()
    finally:
        runtime.close()


def test_rejected_successor_does_not_leave_an_unreferenced_managed_object(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "rejected-successor")
    runtime = build_production_runtime(data_root)
    payload = b"must not enter the managed store\n"
    try:
        with pytest.raises(OwnerConflict, match="asset_not_found"):
            runtime.owners.research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name="rejected.txt",
                    content=payload,
                    asset_ref="asset_missing",
                ),
                idempotency_key="rejected-successor",
            )
        digest = hashlib.sha256(payload).hexdigest()
        assert not (data_root.objects / "assets" / digest[:2] / digest).exists()
        assert runtime.owners.research_memory.query_asset_inventory() == ()
        assert runtime.owners.research_memory.query_snapshot().facts[
            "pending_intake_count"
        ] == 0
    finally:
        runtime.close()


def test_managed_directory_intake_materializes_a_deterministic_archive_without_moving_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "experiment"
    (source / "results").mkdir(parents=True)
    (source / "README.md").write_text("frozen experiment\n", encoding="utf-8")
    (source / "results" / "metric.json").write_text(
        '{"score":0.91}\n', encoding="utf-8"
    )
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "directory-intake")
    )
    try:
        result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="experiment",
                media_type="application/zip",
                source_locator=str(source),
                provenance={"run_ref": "run:controlled"},
            ),
            idempotency_key="managed-directory-1",
        )

        assert result.status == "accepted"
        assert result.asset is not None
        assert result.asset.byte_count == sum(
            path.stat().st_size for path in source.rglob("*") if path.is_file()
        )
        assert source.is_dir()
        assert (source / "README.md").read_text(encoding="utf-8") == (
            "frozen experiment\n"
        )

        first = runtime.owners.research_memory.materialize_asset(
            result.asset.memory_ref
        )
        second = runtime.owners.research_memory.materialize_asset(
            result.asset.memory_ref
        )
        assert first.file_name == "experiment.zip"
        assert first.content == second.content
        with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
            assert archive.namelist() == [
                "results/",
                "README.md",
                "results/metric.json",
            ]
            assert archive.read("README.md") == b"frozen experiment\n"
            assert archive.read("results/metric.json") == b'{"score":0.91}\n'

        linked = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="linked_local",
                display_name="experiment-linked",
                media_type="application/zip",
                source_locator=str(source),
            ),
            idempotency_key="linked-directory-identity",
        )
        assert linked.asset is not None
        assert linked.asset.content_hash == result.asset.content_hash
        assert linked.asset.manifest_hash != result.asset.manifest_hash
    finally:
        runtime.close()


def test_directory_identity_and_materialization_preserve_empty_topology(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first-empty-tree"
    second_source = tmp_path / "second-empty-tree"
    (first_source / "alpha" / "nested").mkdir(parents=True)
    (second_source / "beta").mkdir(parents=True)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "empty-directory-topology")
    )
    try:
        first = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="first-empty-tree",
                source_locator=str(first_source.resolve()),
            ),
            idempotency_key="empty-directory-first",
        )
        second = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="second-empty-tree",
                source_locator=str(second_source.resolve()),
            ),
            idempotency_key="empty-directory-second",
        )
        assert first.asset is not None
        assert second.asset is not None
        assert first.asset.content_hash != second.asset.content_hash

        first_archive = runtime.owners.research_memory.materialize_asset(
            first.asset.memory_ref
        )
        second_archive = runtime.owners.research_memory.materialize_asset(
            second.asset.memory_ref
        )
        with zipfile.ZipFile(io.BytesIO(first_archive.content)) as archive:
            assert archive.namelist() == ["alpha/", "alpha/nested/"]
        with zipfile.ZipFile(io.BytesIO(second_archive.content)) as archive:
            assert archive.namelist() == ["beta/"]
    finally:
        runtime.close()


def test_directory_intake_rejects_nonportable_archive_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsafe-archive-path"
    source.mkdir()
    (source / "..\\outside.txt").write_bytes(b"must never become a zip entry\n")
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "nonportable-directory-path")
    )
    try:
        result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="unsafe-archive-path",
                source_locator=str(source.resolve()),
            ),
            idempotency_key="nonportable-directory-path",
        )
        assert result.status == "failed"
        assert result.failure_code == "asset_source_entry_unsupported"
        assert runtime.owners.research_memory.query_asset_inventory() == ()
    finally:
        runtime.close()


def test_directory_intake_rejects_cross_platform_name_collisions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "colliding-archive-paths"
    source.mkdir()
    (source / "Evidence.txt").write_bytes(b"first\n")
    (source / "evidence.txt").write_bytes(b"second\n")
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "colliding-archive-intake")
    )
    try:
        result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="colliding-archive-paths",
                source_locator=str(source.resolve()),
            ),
            idempotency_key="colliding-archive-paths",
        )
        assert result.status == "failed"
        assert result.failure_code == "asset_source_entry_unsupported"
        assert runtime.owners.research_memory.query_asset_inventory() == ()
    finally:
        runtime.close()


def test_linked_local_freezes_manifest_and_source_drift_changes_only_availability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "linked-result.csv"
    source.write_bytes(b"step,score\n1,0.91\n")
    runtime = build_production_runtime(prepare_data_root(tmp_path / "linked-intake"))
    try:
        result = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                media_type="text/csv",
                source_locator=str(source),
            ),
            idempotency_key="linked-file-1",
        )

        assert result.status == "accepted"
        assert result.asset is not None
        assert result.asset.custody_modes == ("linked_local",)
        assert runtime.owners.research_memory.materialize_asset(
            result.asset.memory_ref
        ).content == b"step,score\n1,0.91\n"

        before = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (before.integrity, before.availability) == ("verified", "available")
        historical_receipt = result.asset.receipt

        source.write_bytes(b"step,score\n1,0.42\n")

        after = runtime.owners.research_memory.query_asset_inventory()[0]
        assert after.receipt == historical_receipt
        assert after.integrity == "verified"
        assert after.availability == "drifted"
        reference_revision = (
            runtime.owners.research_graph.query_asset_reference_revision()
        )
        drifted_release = (
            runtime.owners.research_memory.assess_release_eligibility(
                result.asset.memory_ref,
                expected_reference_revision=reference_revision,
                idempotency_key="linked-drifted-release",
            )
        )
        assert drifted_release.eligible is False
        assert drifted_release.reason_codes == (
            "asset_availability_unavailable",
        )
        runtime.owners.research_memory.verify_asset_receipt(
            asset_ref=result.asset.asset_ref,
            version_ref=result.asset.version_ref,
            content_hash=result.asset.content_hash,
            manifest_hash=result.asset.manifest_hash,
            receipt=historical_receipt,
        )
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.verify_asset_binding(
                asset_ref=result.asset.asset_ref,
                version_ref=result.asset.version_ref,
                content_hash=result.asset.content_hash,
                manifest_hash=result.asset.manifest_hash,
                receipt=historical_receipt,
            )
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.materialize_asset(result.asset.memory_ref)

        source.unlink()
        missing = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (missing.integrity, missing.availability) == (
            "verified",
            "unavailable",
        )
        missing_release = (
            runtime.owners.research_memory.assess_release_eligibility(
                result.asset.memory_ref,
                expected_reference_revision=reference_revision,
                idempotency_key="linked-missing-release",
            )
        )
        assert missing_release.eligible is False
        assert missing_release.reason_codes == (
            "asset_availability_unavailable",
        )
    finally:
        runtime.close()


def test_tampered_custody_binding_fails_all_current_asset_consumers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "receipted-source.txt"
    substituted = tmp_path / "substituted-source.txt"
    payload = b"same bytes do not make an unsigned locator trustworthy\n"
    source.write_bytes(payload)
    substituted.write_bytes(payload)
    data_root = prepare_data_root(tmp_path / "custody-binding-tamper")
    runtime = build_production_runtime(data_root)
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                source_locator=str(source.resolve()),
            ),
            idempotency_key="custody-binding-tamper-intake",
        )
        assert intake.asset is not None
        reference_revision = (
            runtime.owners.research_graph.query_asset_reference_revision()
        )

        with sqlite3.connect(data_root.database) as connection:
            connection.execute(
                "UPDATE rm_asset_custodies SET source_locator = ? "
                "WHERE version_ref = ? AND custody_mode = 'linked_local'",
                (str(substituted.resolve()), intake.asset.memory_ref),
            )
            connection.commit()

        with pytest.raises(OwnerConflict, match="asset_custody_receipt_invalid"):
            runtime.owners.research_memory.query_asset_inventory()
        with pytest.raises(OwnerConflict, match="asset_custody_receipt_invalid"):
            runtime.owners.research_memory.materialize_asset(
                intake.asset.memory_ref
            )
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.verify_asset_binding(
                asset_ref=intake.asset.asset_ref,
                version_ref=intake.asset.version_ref,
                content_hash=intake.asset.content_hash,
                manifest_hash=intake.asset.manifest_hash,
                receipt=intake.asset.receipt,
            )
        assessment = runtime.owners.research_memory.assess_release_eligibility(
            intake.asset.memory_ref,
            expected_reference_revision=reference_revision,
            idempotency_key="custody-binding-tamper-release",
        )
        assert assessment.eligible is False
        assert "asset_state_uncertain" in assessment.reason_codes
    finally:
        runtime.close()


def test_custody_handoff_verifies_managed_copy_before_source_can_disappear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "linked-corpus"
    source.mkdir()
    (source / "paper.txt").write_bytes(b"immutable paper bytes\n")
    data_root = prepare_data_root(tmp_path / "handoff")
    runtime = build_production_runtime(data_root)
    try:
        monkeypatch.setattr(
            "meta_research.owners.research_memory.time.time", lambda: 1000.0
        )
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="linked_local",
                display_name="linked-corpus",
                media_type="application/zip",
                source_locator=str(source),
            ),
            idempotency_key="handoff-intake-1",
        )
        assert intake.asset is not None

        handoff = runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="handoff-managed-1",
        )
        replay = runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="handoff-managed-1",
        )

        assert replay == handoff
        assert handoff.custody_mode == "managed"
        assert handoff.receipt.kind == "asset_custody_handoff"
        custodies = runtime.owners.research_memory.query_asset_custodies(
            intake.asset.memory_ref
        )
        assert len(custodies) == 2
        assert handoff in custodies
        for custody in custodies:
            runtime.owners.research_memory.verify_asset_custody_receipt(
                custody_ref=custody.custody_ref,
                version_ref=custody.version_ref,
                custody_mode=custody.custody_mode,
                receipt=custody.receipt,
            )
        assert (source / "paper.txt").is_file()
        source.rename(tmp_path / "source-moved-after-receipt")

        inventory = runtime.owners.research_memory.query_asset_inventory()[0]
        assert inventory.custody_modes == ("linked_local", "managed")
        assert (inventory.integrity, inventory.availability) == (
            "verified",
            "available",
        )
        archive = runtime.owners.research_memory.materialize_asset(
            intake.asset.memory_ref
        )
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            assert bundle.read("paper.txt") == b"immutable paper bytes\n"

        runtime.close()
        runtime = build_production_runtime(data_root)
        assert runtime.owners.research_memory.query_asset_custodies(
            intake.asset.memory_ref
        ) == custodies

        with pytest.raises(OwnerConflict, match="asset_custody_idempotency_conflict"):
            runtime.owners.research_memory.handoff_asset_to_managed(
                "asset_version_missing",
                idempotency_key="handoff-managed-1",
            )
    finally:
        runtime.close()


def test_managed_corruption_fails_integrity_without_hiding_linked_availability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "linked-authoritative.txt"
    payload = b"authoritative linked bytes\n"
    source.write_bytes(payload)
    data_root = prepare_data_root(tmp_path / "managed-integrity")
    runtime = build_production_runtime(data_root)
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                source_locator=str(source),
            ),
            idempotency_key="managed-integrity-intake",
        )
        assert intake.asset is not None
        handoff = runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="managed-integrity-handoff",
        )
        object_path = (
            data_root.objects
            / "assets"
            / intake.asset.content_hash[:2]
            / intake.asset.content_hash
        )
        object_path.write_bytes(b"corrupted managed bytes\n")

        item = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (item.integrity, item.availability) == ("failed", "available")
        assert runtime.owners.research_memory.query_snapshot().status == "unavailable"
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.materialize_asset(item.memory_ref)
        assessment = runtime.owners.research_memory.assess_release_eligibility(
            item.memory_ref,
            expected_reference_revision=(
                runtime.owners.research_graph.query_asset_reference_revision()
            ),
            idempotency_key="managed-integrity-release",
        )
        assert assessment.eligible is False
        assert assessment.reason_codes == ("asset_state_uncertain",)

        revision_before_replay = (
            runtime.owners.research_memory.query_snapshot().revision
        )
        assert runtime.owners.research_memory.handoff_asset_to_managed(
            item.memory_ref,
            idempotency_key="managed-integrity-handoff",
        ) == handoff
        assert object_path.read_bytes() == b"corrupted managed bytes\n"
        assert (
            runtime.owners.research_memory.query_snapshot().revision
            == revision_before_replay
        )

        repaired = runtime.owners.research_memory.handoff_asset_to_managed(
            item.memory_ref,
            idempotency_key="managed-integrity-repair",
        )
        assert repaired.custody_mode == "managed"
        restored = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (restored.integrity, restored.availability) == (
            "verified",
            "available",
        )
        assert runtime.owners.research_memory.materialize_asset(
            item.memory_ref
        ).content == payload

        source.unlink()
        missing = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (missing.integrity, missing.availability) == (
            "verified",
            "available",
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "repair_keys",
    [
        ("concurrent-repair-a", "concurrent-repair-b"),
        ("concurrent-repair-replay", "concurrent-repair-replay"),
    ],
    ids=("different-keys", "same-key-timeout-replay"),
)
def test_concurrent_managed_repair_commands_join_one_durable_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_keys: tuple[str, str],
) -> None:
    source = tmp_path / "concurrent-repair-source.txt"
    payload = b"one exact repair source\n"
    source.write_bytes(payload)
    data_root = prepare_data_root(tmp_path / "concurrent-managed-repair")
    runtime = build_production_runtime(data_root)
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                display_name=source.name,
                source_locator=str(source),
            ),
            idempotency_key="concurrent-repair-intake",
        )
        assert intake.asset is not None
        runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="concurrent-repair-handoff",
        )
        object_path = (
            data_root.objects
            / "assets"
            / intake.asset.content_hash[:2]
            / intake.asset.content_hash
        )
        object_path.write_bytes(b"corrupted before concurrent repair\n")

        first_source_check = threading.Event()
        release_source_check = threading.Event()
        source_check_lock = threading.Lock()
        source_check_count = 0
        original_matches = research_memory_module._linked_source_matches

        def synchronized_matches(manifest, linked_source):
            nonlocal source_check_count
            with source_check_lock:
                source_check_count += 1
                first_source_check.set()
            release_source_check.wait(timeout=2.0)
            matches = original_matches(manifest, linked_source)
            return matches

        monkeypatch.setattr(
            research_memory_module,
            "_linked_source_matches",
            synchronized_matches,
        )
        results = []
        errors: list[BaseException] = []

        def repair(key: str) -> None:
            try:
                results.append(
                    runtime.owners.research_memory.handoff_asset_to_managed(
                        intake.asset.memory_ref,
                        idempotency_key=key,
                    )
                )
            except BaseException as error:
                errors.append(error)

        workers = [
            threading.Thread(target=repair, args=(repair_keys[0],)),
            threading.Thread(target=repair, args=(repair_keys[1],)),
        ]
        workers[0].start()
        assert first_source_check.wait(timeout=1.0)
        workers[1].start()
        time.sleep(0.05)
        with source_check_lock:
            assert source_check_count == 1
        release_source_check.set()
        for worker in workers:
            worker.join(timeout=4.0)

        assert not any(worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert results[0].custody_ref == results[1].custody_ref
        with sqlite3.connect(data_root.database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM rm_asset_repair_commands WHERE "
                "custody_ref = ? AND status = 'completed'",
                (results[0].custody_ref,),
            ).fetchone() == (1,)
    finally:
        if "release_source_check" in locals():
            release_source_check.set()
        runtime.close()


def test_interrupted_managed_repair_is_reconciled_after_losing_the_original_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repair-authoritative.txt"
    payload = b"repairable exact source bytes\n"
    source.write_bytes(payload)
    data_root = prepare_data_root(tmp_path / "managed-repair-restart")
    runtime = build_production_runtime(data_root)
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="local_path",
            custody_mode="linked_local",
            display_name=source.name,
            source_locator=str(source.resolve()),
        ),
        idempotency_key="repair-restart-intake",
    )
    assert intake.asset is not None
    runtime.owners.research_memory.handoff_asset_to_managed(
        intake.asset.memory_ref,
        idempotency_key="repair-restart-handoff",
    )
    object_path = (
        data_root.objects
        / "assets"
        / intake.asset.content_hash[:2]
        / intake.asset.content_hash
    )
    object_path.write_bytes(b"corrupted before interrupted repair\n")
    revision_before_repair = runtime.owners.research_memory.query_snapshot().revision
    original_replace = runtime.owners.research_memory._replace_asset_object

    def replace_then_interrupt(object_hash: str, content: bytes) -> str:
        result = original_replace(object_hash, content)
        raise RuntimeError("crash after durable object replacement")

    monkeypatch.setattr(
        runtime.owners.research_memory,
        "_replace_asset_object",
        replace_then_interrupt,
    )
    with pytest.raises(RuntimeError, match="crash after durable object replacement"):
        runtime.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="repair-restart-command",
        )
    assert object_path.read_bytes() == payload
    assert runtime.owners.research_memory.query_snapshot().revision == (
        revision_before_repair
    )
    runtime.close()

    restarted = build_production_runtime(data_root)
    try:
        repaired = restarted.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="repair-restart-recovery-command",
        )
        assert repaired.custody_mode == "managed"
        assert restarted.owners.research_memory.query_snapshot().revision == (
            revision_before_repair + 1
        )
        assert restarted.owners.research_memory.materialize_asset(
            intake.asset.memory_ref
        ).content == payload
        assert restarted.owners.research_memory.handoff_asset_to_managed(
            intake.asset.memory_ref,
            idempotency_key="repair-restart-command",
        ) == repaired
        assert restarted.owners.research_memory.query_snapshot().revision == (
            revision_before_repair + 1
        )
    finally:
        restarted.close()


def test_managed_path_intake_can_repair_from_its_receipted_source_locator(
    tmp_path: Path,
) -> None:
    source = tmp_path / "managed-path-source.txt"
    payload = b"receipt-bound managed path source\n"
    source.write_bytes(payload)
    data_root = prepare_data_root(tmp_path / "managed-path-repair")
    runtime = build_production_runtime(data_root)
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="managed",
                display_name=source.name,
                source_locator=str(source.resolve()),
            ),
            idempotency_key="managed-path-repair-intake",
        )
        assert intake.asset is not None
        object_path = (
            data_root.objects
            / "assets"
            / intake.asset.content_hash[:2]
            / intake.asset.content_hash
        )
        object_path.write_bytes(b"corrupted managed path object\n")
        item = runtime.owners.research_memory.query_asset_inventory()[0]
        assert (item.integrity, item.availability) == ("failed", "available")

        custody = runtime.owners.research_memory.handoff_asset_to_managed(
            item.memory_ref,
            idempotency_key="managed-path-repair-command",
        )
        assert custody.custody_mode == "managed"
        assert runtime.owners.research_memory.materialize_asset(
            item.memory_ref
        ).content == payload
    finally:
        runtime.close()


def test_memory_ref_always_materializes_the_exact_version_not_latest(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "versions"))
    try:
        first = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="notes-v1.txt",
                media_type="text/plain",
                content=b"version one\n",
            ),
            idempotency_key="version-1",
        )
        assert first.asset is not None
        second = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="notes-v2.txt",
                media_type="text/plain",
                content=b"version two\n",
                asset_ref=first.asset.asset_ref,
            ),
            idempotency_key="version-2",
        )
        assert second.asset is not None

        assert second.asset.asset_ref == first.asset.asset_ref
        assert second.asset.version_number == 2
        assert first.asset.memory_ref != second.asset.memory_ref
        assert runtime.owners.research_memory.materialize_asset(
            first.asset.memory_ref
        ).content == b"version one\n"
        assert runtime.owners.research_memory.materialize_asset(
            second.asset.memory_ref
        ).content == b"version two\n"
    finally:
        runtime.close()


def test_async_intake_repairs_truncated_prewritten_object_after_restart(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "async-recovery")
    payload = b"object persisted before sqlite acceptance acknowledgement\n"
    request = AssetIntakeRequest(
        source_kind="file",
        custody_mode="managed",
        display_name="recovered.txt",
        media_type="text/plain",
        content=payload,
        asynchronous=True,
    )
    runtime = build_production_runtime(data_root)
    queued = runtime.owners.research_memory.submit_asset_intake(
        request, idempotency_key="async-recovery-1"
    )
    assert queued.status == "queued"
    assert queued.attempt_count == 0
    runtime.close()

    object_hash = hashlib.sha256(payload).hexdigest()
    object_path = data_root.objects / "assets" / object_hash[:2] / object_hash
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(payload[:9])
    with sqlite3.connect(data_root.database) as connection:
        connection.execute(
            "UPDATE rm_asset_intakes SET status = 'processing', "
            "attempt_count = 1, started_at = 21.0 WHERE job_ref = ?",
            (queued.job_ref,),
        )
        connection.commit()

    restarted = build_production_runtime(data_root)
    try:
        recovered = restarted.owners.research_memory.query_asset_intake(
            queued.job_ref
        )
        assert recovered.status == "queued"
        assert recovered.attempt_count == 1
        assert restarted.owners.research_memory.process_asset_intake_once()

        accepted = restarted.owners.research_memory.query_asset_intake(
            queued.job_ref
        )
        assert accepted.status == "accepted"
        assert accepted.attempt_count == 2
        assert accepted.asset is not None
        assert restarted.owners.research_memory.materialize_asset(
            accepted.asset.memory_ref
        ).content == payload
        assert restarted.owners.research_memory.query_snapshot().facts[
            "object_count"
        ] == 1
        assert (
            restarted.owners.research_memory.submit_asset_intake(
                request, idempotency_key="async-recovery-1"
            )
            == accepted
        )
        with sqlite3.connect(data_root.database) as connection:
            stored_request = connection.execute(
                "SELECT request_json, request_source_kind, "
                "request_custody_mode, request_payload_scrubbed FROM "
                "rm_asset_intakes WHERE job_ref = ?",
                (queued.job_ref,),
            ).fetchone()
        assert stored_request is not None
        scrubbed_document = json.loads(stored_request[0])
        assert scrubbed_document == {
            "custody_mode": "managed",
            "payload_scrubbed": True,
            "source_kind": "file",
        }
        assert stored_request[1:] == ("file", "managed", 1)
        assert "content_base64" not in stored_request[0]
        assert not restarted.owners.research_memory.process_asset_intake_once()
    finally:
        restarted.close()


def test_async_intake_requeues_transient_failure_without_daemon_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(research_memory_module.time, "time", lambda: now[0])
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "same-daemon-recovery")
    )
    research_memory = runtime.owners.research_memory
    try:
        queued = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="retry.txt",
                media_type="text/plain",
                content=b"retryable intake bytes\n",
                asynchronous=True,
            ),
            idempotency_key="same-daemon-retry",
        )
        now[0] += 0.01
        later = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="later.txt",
                media_type="text/plain",
                content=b"later valid intake bytes\n",
                asynchronous=True,
            ),
            idempotency_key="same-daemon-later",
        )
        original_prepare = research_memory._prepare_asset

        def interrupt_oldest(request: dict[str, object]):
            if request["display_name"] == "retry.txt":
                raise RuntimeError("transient intake interruption")
            return original_prepare(request)

        monkeypatch.setattr(research_memory, "_prepare_asset", interrupt_oldest)
        with pytest.raises(RuntimeError, match="transient intake interruption"):
            research_memory.process_asset_intake_once()

        requeued = research_memory.query_asset_intake(queued.job_ref)
        assert requeued.status == "queued"
        assert requeued.attempt_count == 1

        assert research_memory.process_asset_intake_once()
        later_accepted = research_memory.query_asset_intake(later.job_ref)
        assert later_accepted.status == "accepted"
        assert later_accepted.attempt_count == 1

        monkeypatch.setattr(research_memory, "_prepare_asset", original_prepare)
        now[0] += 1.0
        assert research_memory.process_asset_intake_once()
        accepted = research_memory.query_asset_intake(queued.job_ref)
        assert accepted.status == "accepted"
        assert accepted.attempt_count == 2
        assert accepted.asset is not None
        assert len(research_memory.query_asset_inventory()) == 2
    finally:
        runtime.close()


def test_async_locator_intake_recovers_when_a_mount_becomes_available_after_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "late-mount" / "source.txt").resolve()
    now = [1_000.0]
    monkeypatch.setattr(research_memory_module.time, "time", lambda: now[0])
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "late-mount-recovery")
    )
    research_memory = runtime.owners.research_memory
    try:
        queued = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="managed",
                display_name="source.txt",
                source_locator=str(source),
                asynchronous=True,
            ),
            idempotency_key="late-mount-recovery",
        )
        assert research_memory.process_asset_intake_once()
        retrying = research_memory.query_asset_intake(queued.job_ref)
        assert retrying.status == "queued"
        assert retrying.attempt_count == 1

        # A transient mount outage must not burn the bounded retry budget in
        # the worker's 50 ms loop. The durable job remains queued but not due.
        now[0] += 0.75
        assert not research_memory.process_asset_intake_once()
        assert research_memory.query_asset_intake(queued.job_ref).attempt_count == 1

        now[0] += 0.50
        source.parent.mkdir()
        source.write_bytes(b"mounted source bytes\n")
        assert research_memory.process_asset_intake_once()
        accepted = research_memory.query_asset_intake(queued.job_ref)
        assert accepted.status == "accepted"
        assert accepted.attempt_count == 2
        assert accepted.asset is not None
        assert research_memory.materialize_asset(
            accepted.asset.memory_ref
        ).content == b"mounted source bytes\n"
        assert len(research_memory.query_asset_inventory()) == 1
    finally:
        runtime.close()


def test_async_intake_rejects_tampered_or_malformed_durable_requests(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "durable-request-integrity")
    runtime = build_production_runtime(data_root)
    research_memory = runtime.owners.research_memory
    try:
        tampered = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="original.txt",
                content=b"original durable command bytes\n",
                asynchronous=True,
            ),
            idempotency_key="durable-request-tampered",
        )
        malformed = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="malformed.txt",
                content=b"well formed before durable corruption\n",
                asynchronous=True,
            ),
            idempotency_key="durable-request-malformed",
        )
        with sqlite3.connect(data_root.database) as connection:
            stored = connection.execute(
                "SELECT request_json FROM rm_asset_intakes WHERE job_ref = ?",
                (tampered.job_ref,),
            ).fetchone()
            assert stored is not None
            document = json.loads(stored[0])
            document["content_base64"] = base64.b64encode(
                b"tampered durable command bytes\n"
            ).decode("ascii")
            connection.execute(
                "UPDATE rm_asset_intakes SET request_json = ? WHERE job_ref = ?",
                (
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    tampered.job_ref,
                ),
            )
            connection.execute(
                "UPDATE rm_asset_intakes SET request_json = '{' WHERE job_ref = ?",
                (malformed.job_ref,),
            )
            connection.commit()

        assert research_memory.process_asset_intake_once()
        first = research_memory.query_asset_intake(tampered.job_ref)
        assert first.status == "failed"
        assert first.failure_code == "asset_intake_request_invalid"
        assert (first.source_kind, first.custody_mode) == ("text", "managed")

        assert research_memory.process_asset_intake_once()
        second = research_memory.query_asset_intake(malformed.job_ref)
        assert second.status == "failed"
        assert second.failure_code == "asset_intake_request_invalid"
        assert (second.source_kind, second.custody_mode) == ("text", "managed")
        assert research_memory.query_asset_inventory() == ()
        assert not research_memory.process_asset_intake_once()
    finally:
        runtime.close()


def test_special_file_intake_fails_without_blocking_the_async_queue(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "blocking-source.fifo"
    os.mkfifo(fifo)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "special-file-intake")
    )
    research_memory = runtime.owners.research_memory
    try:
        special = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="managed",
                display_name=fifo.name,
                source_locator=str(fifo.resolve()),
                asynchronous=True,
            ),
            idempotency_key="special-file-fifo",
        )
        later = research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="later.txt",
                content=b"later queue entry remains runnable\n",
                asynchronous=True,
            ),
            idempotency_key="special-file-later",
        )

        assert research_memory.process_asset_intake_once()
        failed = research_memory.query_asset_intake(special.job_ref)
        assert failed.status == "failed"
        assert failed.failure_code == "asset_source_entry_unsupported"
        assert research_memory.process_asset_intake_once()
        assert research_memory.query_asset_intake(later.job_ref).status == "accepted"
    finally:
        runtime.close()


def test_repository_link_and_system_artifact_are_frozen_without_git_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "volatile").write_text("one", encoding="utf-8")
    (repository / "README.md").write_text("tracked bytes\n", encoding="utf-8")
    (repository / "vendor" / "nested" / ".git").mkdir(parents=True)
    (repository / "vendor" / "nested" / ".git" / "config").write_text(
        "secret remote metadata\n", encoding="utf-8"
    )
    (repository / "vendor" / "nested" / "code.py").write_text(
        "print('tracked')\n", encoding="utf-8"
    )
    artifact = tmp_path / "runtime.log"
    artifact.write_bytes(b"runtime artifact\n")
    runtime = build_production_runtime(prepare_data_root(tmp_path / "source-kinds"))
    try:
        repo = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="repository",
                custody_mode="linked_local",
                display_name="repository",
                source_locator=str(repository),
            ),
            idempotency_key="repository-1",
        )
        link = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="link",
                custody_mode="managed",
                display_name="publisher-link.url",
                media_type="text/uri-list",
                source_locator="https://example.org/papers/42?view=full",
            ),
            idempotency_key="link-1",
        )
        system = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="system_artifact",
                custody_mode="managed",
                display_name=artifact.name,
                source_locator=str(artifact),
            ),
            idempotency_key="system-artifact-1",
        )
        assert repo.asset is not None
        assert link.asset is not None
        assert system.asset is not None

        (repository / ".git" / "volatile").write_text("two", encoding="utf-8")
        (repository / "vendor" / "nested" / ".git" / "config").write_text(
            "changed secret remote metadata\n", encoding="utf-8"
        )
        repo_item = next(
            item
            for item in runtime.owners.research_memory.query_asset_inventory()
            if item.version_ref == repo.asset.version_ref
        )
        assert repo_item.availability == "available"
        repo_archive = runtime.owners.research_memory.materialize_asset(
            repo.asset.memory_ref
        )
        with zipfile.ZipFile(io.BytesIO(repo_archive.content)) as bundle:
            assert bundle.namelist() == [
                "vendor/",
                "vendor/nested/",
                "README.md",
                "vendor/nested/code.py",
            ]
            assert bundle.read("README.md") == b"tracked bytes\n"
            assert bundle.read("vendor/nested/code.py") == b"print('tracked')\n"
        assert runtime.owners.research_memory.materialize_asset(
            link.asset.memory_ref
        ).content == b"https://example.org/papers/42?view=full"
        assert runtime.owners.research_memory.materialize_asset(
            system.asset.memory_ref
        ).content == b"runtime artifact\n"
    finally:
        runtime.close()


def test_holds_and_release_eligibility_are_receipted_and_never_delete_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "release"))
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="retained.txt",
                content=b"must remain present\n",
            ),
            idempotency_key="release-asset",
        )
        assert intake.asset is not None
        original_current_state = research_memory_module._asset_current_state

        def current_state_without_writer(*args):
            assert not runtime._database._write_lock._is_owned()
            return original_current_state(*args)

        monkeypatch.setattr(
            research_memory_module,
            "_asset_current_state",
            current_state_without_writer,
        )
        revision = runtime.owners.research_graph.query_asset_reference_revision()

        eligible = runtime.owners.research_memory.assess_release_eligibility(
            intake.asset.memory_ref,
            expected_reference_revision=revision,
            idempotency_key="release-assess-empty",
        )
        assert eligible.eligible is True
        assert eligible.reason_codes == ()
        assert eligible.receipt.kind == "release_eligibility_assessment"

        hold = runtime.owners.research_memory.place_asset_hold(
            intake.asset.memory_ref,
            reason="retain for reproducibility audit",
            idempotency_key="release-hold",
        )
        assert hold.active is True
        assert hold.placement_receipt.kind == "asset_hold_placed"
        blocked = runtime.owners.research_memory.assess_release_eligibility(
            intake.asset.memory_ref,
            expected_reference_revision=revision,
            idempotency_key="release-assess-held",
        )
        assert blocked.eligible is False
        assert blocked.reason_codes == ("active_hold",)
        assert blocked.active_hold_refs == (hold.hold_ref,)

        released = runtime.owners.research_memory.release_asset_hold(
            hold.hold_ref, idempotency_key="release-hold-clear"
        )
        replay = runtime.owners.research_memory.release_asset_hold(
            hold.hold_ref, idempotency_key="release-hold-clear"
        )
        assert replay == released
        assert released.active is False
        assert released.release_receipt is not None
        assert released.release_receipt.kind == "asset_hold_released"
        assert runtime.owners.research_memory.query_asset_holds(
            intake.asset.memory_ref
        ) == (released,)

        clear = runtime.owners.research_memory.assess_release_eligibility(
            intake.asset.memory_ref,
            expected_reference_revision=revision,
            idempotency_key="release-assess-clear",
        )
        assert clear.eligible is True
        assert runtime.owners.research_memory.materialize_asset(
            intake.asset.memory_ref
        ).content == b"must remain present\n"

        uncertain = runtime.owners.research_memory.assess_release_eligibility(
            intake.asset.memory_ref,
            expected_reference_revision=None,
            idempotency_key="release-assess-uncertain",
        )
        assert uncertain.eligible is False
        assert uncertain.reason_codes == ("reference_revision_required",)
        assert runtime.owners.research_memory.query_release_eligibility_assessments(
            intake.asset.memory_ref
        ) == (eligible, blocked, clear, uncertain)
    finally:
        runtime.close()


def test_release_hold_rejects_corrupt_placement_without_side_effects(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "hold-corrupt"))
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="held.txt",
                content=b"held bytes\n",
            ),
            idempotency_key="hold-corrupt-asset",
        )
        assert intake.asset is not None
        hold = runtime.owners.research_memory.place_asset_hold(
            intake.asset.memory_ref,
            reason="retention boundary",
            idempotency_key="hold-corrupt-place",
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_asset_holds SET receipt_hash = :receipt_hash "
                    "WHERE hold_ref = :hold_ref"
                ),
                {"hold_ref": hold.hold_ref, "receipt_hash": "f" * 64},
            )

        with pytest.raises(OwnerConflict, match="asset_hold_receipt_invalid"):
            runtime.owners.research_memory.release_asset_hold(
                hold.hold_ref,
                idempotency_key="hold-corrupt-release",
            )
        with runtime._database.read() as connection:
            row = connection.execute(
                text("SELECT * FROM rm_asset_holds WHERE hold_ref = :hold_ref"),
                {"hold_ref": hold.hold_ref},
            ).first()
            command = connection.execute(
                text(
                    "SELECT * FROM rm_asset_hold_commands WHERE idempotency_key = "
                    ":idempotency_key"
                ),
                {"idempotency_key": "hold-corrupt-release"},
            ).first()
        assert row is not None
        assert bool(row.active) is True
        assert row.released_at is None
        assert row.release_receipt_ref is None
        assert row.release_receipt_hash is None
        assert command is None
    finally:
        runtime.close()
