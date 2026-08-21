"""Add the unified Research Memory asset lifecycle and RG asset roles.

Revision ID: 0005_research_assets
Revises: 0004_idea_stage
Create Date: 2026-08-21
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "0005_research_assets"
down_revision = "0004_idea_stage"
branch_labels = None
depends_on = None


MANIFEST_SCHEMA = "meta-research/asset-manifest/v1"


def _counter(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False, server_default="0")


def _hash_check(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column("research_memory_state", _counter("asset_version_count"))
    op.add_column("research_memory_state", _counter("pending_intake_count"))
    op.add_column("research_memory_state", _counter("hold_count"))
    op.add_column("research_graph_state", _counter("asset_role_count"))
    op.add_column("research_graph_state", _counter("evidence_role_count"))
    op.add_column("research_graph_state", _counter("source_material_role_count"))

    op.create_table(
        "rm_assets",
        sa.Column("asset_ref", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "rm_asset_versions",
        sa.Column("version_ref", sa.String(length=64), primary_key=True),
        sa.Column("asset_ref", sa.String(length=64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("provenance_hash", sa.String(length=64), nullable=False),
        sa.Column("acceptance_kind", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["asset_ref"], ["rm_assets.asset_ref"]),
        sa.UniqueConstraint("asset_ref", "version_number"),
        sa.CheckConstraint("version_number >= 1"),
        sa.CheckConstraint("byte_count >= 0"),
        sa.CheckConstraint(
            "source_kind IN ('text', 'file', 'directory', 'local_path', "
            "'repository', 'link', 'system_artifact', 'formal_question', "
            "'idea_outcome')"
        ),
        _hash_check("content_hash"),
        _hash_check("manifest_hash"),
        _hash_check("provenance_hash"),
        _hash_check("receipt_hash"),
    )
    op.create_index(
        "ix_rm_asset_versions_accepted",
        "rm_asset_versions",
        ["accepted_at", "version_ref"],
    )
    op.create_table(
        "rm_asset_custodies",
        sa.Column("custody_ref", sa.String(length=128), primary_key=True),
        sa.Column("version_ref", sa.String(length=64), nullable=False),
        sa.Column("custody_mode", sa.String(length=24), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=True),
        sa.Column("receipt_kind", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("established_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        sa.UniqueConstraint("version_ref", "custody_mode"),
        sa.CheckConstraint("custody_mode IN ('managed', 'linked_local')"),
        _hash_check("receipt_hash"),
    )
    op.create_table(
        "rm_asset_intakes",
        sa.Column("job_ref", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("asset_ref", sa.String(length=64), nullable=True),
        sa.Column("version_ref", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["asset_ref"], ["rm_assets.asset_ref"]),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'accepted', 'failed')"
        ),
        sa.CheckConstraint("attempt_count >= 0"),
        _hash_check("request_hash"),
        sa.CheckConstraint(
            "(status IN ('queued', 'processing') AND version_ref IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'accepted' AND asset_ref IS NOT NULL AND version_ref IS NOT NULL "
            "AND failure_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND version_ref IS NULL "
            "AND failure_code IS NOT NULL AND completed_at IS NOT NULL)"
        ),
    )
    op.create_index(
        "ix_rm_asset_intakes_status",
        "rm_asset_intakes",
        ["status", "created_at"],
    )
    op.create_table(
        "rm_asset_custody_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("custody_ref", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["custody_ref"], ["rm_asset_custodies.custody_ref"]
        ),
        _hash_check("request_hash"),
    )
    op.create_table(
        "rm_asset_holds",
        sa.Column("hold_ref", sa.String(length=64), primary_key=True),
        sa.Column("version_ref", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("placed_at", sa.Float(), nullable=False),
        sa.Column("released_at", sa.Float(), nullable=True),
        sa.Column("release_receipt_ref", sa.String(length=64), nullable=True, unique=True),
        sa.Column("release_receipt_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        _hash_check("receipt_hash"),
        sa.CheckConstraint(
            "(active = 1 AND released_at IS NULL AND release_receipt_ref IS NULL "
            "AND release_receipt_hash IS NULL) OR "
            "(active = 0 AND released_at IS NOT NULL AND release_receipt_ref IS NOT "
            "NULL AND release_receipt_hash IS NOT NULL AND "
            "length(release_receipt_hash) = 64)"
        ),
    )
    op.create_table(
        "rm_asset_hold_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("command_kind", sa.String(length=24), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("hold_ref", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["hold_ref"], ["rm_asset_holds.hold_ref"]),
        sa.CheckConstraint("command_kind IN ('place_hold', 'release_hold')"),
        _hash_check("request_hash"),
    )
    op.create_table(
        "rm_release_eligibility_assessments",
        sa.Column("assessment_ref", sa.String(length=64), primary_key=True),
        sa.Column("version_ref", sa.String(length=64), nullable=False),
        sa.Column("expected_reference_revision", sa.Integer(), nullable=True),
        sa.Column("observed_reference_revision", sa.Integer(), nullable=True),
        sa.Column("active_reference_refs_json", sa.Text(), nullable=False),
        sa.Column("active_reference_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("active_hold_refs_json", sa.Text(), nullable=False),
        sa.Column("active_hold_refs_hash", sa.String(length=64), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("reason_codes_json", sa.Text(), nullable=False),
        sa.Column("reason_codes_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("assessed_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        sa.CheckConstraint(
            "expected_reference_revision IS NULL OR expected_reference_revision >= 0"
        ),
        sa.CheckConstraint(
            "observed_reference_revision IS NULL OR observed_reference_revision >= 0"
        ),
        _hash_check("active_reference_refs_hash"),
        _hash_check("active_hold_refs_hash"),
        _hash_check("reason_codes_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )

    op.create_table(
        "rg_asset_roles",
        sa.Column("role_ref", sa.String(length=64), primary_key=True),
        sa.Column("version_ref", sa.String(length=64), nullable=False),
        sa.Column("asset_ref", sa.String(length=64), nullable=False),
        sa.Column("asset_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_receipt_kind", sa.String(length=64), nullable=False),
        sa.Column("asset_receipt_ref", sa.String(length=64), nullable=False),
        sa.Column("asset_receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("quest_ref", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_ref", sa.String(length=64), nullable=False, unique=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        sa.ForeignKeyConstraint(["asset_ref"], ["rm_assets.asset_ref"]),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.UniqueConstraint("version_ref", "role", "quest_ref"),
        sa.CheckConstraint("role IN ('evidence', 'quest_source_material')"),
        _hash_check("asset_hash"),
        _hash_check("manifest_hash"),
        _hash_check("asset_receipt_hash"),
        _hash_check("request_hash"),
        _hash_check("receipt_hash"),
    )
    op.create_index(
        "ix_rg_asset_roles_quest_role",
        "rg_asset_roles",
        ["quest_ref", "role", "accepted_at"],
    )

    _backfill_existing_content()


def _backfill_existing_content() -> None:
    connection = op.get_bind()
    formal_rows = connection.execute(
        sa.text(
            "SELECT content_ref, initialization_id, quest_ref, content_hash, "
            "schema_ref, content_json, object_path, receipt_ref, receipt_hash, "
            "accepted_at FROM rm_formal_question_contents ORDER BY accepted_at"
        )
    ).mappings()
    for row in formal_rows:
        _backfill_version(
            connection,
            version_ref=row["content_ref"],
            source_kind="formal_question",
            display_name="Formal Question content",
            media_type="application/json",
            content_hash=row["content_hash"],
            content_bytes=row["content_json"].encode("utf-8"),
            object_path=row["object_path"],
            provenance={
                "legacy_table": "rm_formal_question_contents",
                "initialization_id": row["initialization_id"],
                "quest_ref": row["quest_ref"],
                "schema_ref": row["schema_ref"],
            },
            acceptance_kind="question_content_acceptance",
            receipt_ref=row["receipt_ref"],
            receipt_hash=row["receipt_hash"],
            accepted_at=float(row["accepted_at"]),
        )

    idea_rows = connection.execute(
        sa.text(
            "SELECT content_ref, request_ref, run_ref, submission_ref, "
            "payload_hash, payload_json, object_path, receipt_ref, receipt_hash, "
            "accepted_at FROM rm_idea_outcome_contents ORDER BY accepted_at"
        )
    ).mappings()
    for row in idea_rows:
        _backfill_version(
            connection,
            version_ref=row["content_ref"],
            source_kind="idea_outcome",
            display_name="Idea outcome content",
            media_type="application/json",
            content_hash=row["payload_hash"],
            content_bytes=row["payload_json"].encode("utf-8"),
            object_path=row["object_path"],
            provenance={
                "legacy_table": "rm_idea_outcome_contents",
                "request_ref": row["request_ref"],
                "run_ref": row["run_ref"],
                "submission_ref": row["submission_ref"],
            },
            acceptance_kind="idea_outcome_content_acceptance",
            receipt_ref=row["receipt_ref"],
            receipt_hash=row["receipt_hash"],
            accepted_at=float(row["accepted_at"]),
        )

    connection.execute(
        sa.text(
            "UPDATE research_memory_state SET "
            "asset_count = (SELECT COUNT(*) FROM rm_assets), "
            "asset_version_count = (SELECT COUNT(*) FROM rm_asset_versions) "
            "WHERE singleton = 'owner'"
        )
    )


def _backfill_version(
    connection,
    *,
    version_ref: str,
    source_kind: str,
    display_name: str,
    media_type: str,
    content_hash: str,
    content_bytes: bytes,
    object_path: str,
    provenance: dict[str, object],
    acceptance_kind: str,
    receipt_ref: str,
    receipt_hash: str,
    accepted_at: float,
) -> None:
    manifest = {
        "schema_ref": MANIFEST_SCHEMA,
        "kind": "file",
        "entries": [
            {
                "path": "content.json",
                "sha256": content_hash,
                "size": len(content_bytes),
                "object_path": object_path,
            }
        ],
    }
    manifest_json = _canonical_json(manifest)
    provenance_json = _canonical_json(provenance)
    connection.execute(
        sa.text(
            "INSERT INTO rm_assets (asset_ref, created_at) "
            "VALUES (:asset_ref, :created_at)"
        ),
        {"asset_ref": version_ref, "created_at": accepted_at},
    )
    connection.execute(
        sa.text(
            "INSERT INTO rm_asset_versions (version_ref, asset_ref, version_number, "
            "source_kind, display_name, media_type, content_hash, manifest_json, "
            "manifest_hash, byte_count, provenance_json, provenance_hash, "
            "acceptance_kind, receipt_ref, receipt_hash, accepted_at) VALUES "
            "(:version_ref, :asset_ref, 1, :source_kind, :display_name, "
            ":media_type, :content_hash, :manifest_json, :manifest_hash, "
            ":byte_count, :provenance_json, :provenance_hash, :acceptance_kind, "
            ":receipt_ref, :receipt_hash, :accepted_at)"
        ),
        {
            "version_ref": version_ref,
            "asset_ref": version_ref,
            "source_kind": source_kind,
            "display_name": display_name,
            "media_type": media_type,
            "content_hash": content_hash,
            "manifest_json": manifest_json,
            "manifest_hash": _canonical_hash(manifest),
            "byte_count": len(content_bytes),
            "provenance_json": provenance_json,
            "provenance_hash": _canonical_hash(provenance),
            "acceptance_kind": acceptance_kind,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "accepted_at": accepted_at,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO rm_asset_custodies (custody_ref, version_ref, "
            "custody_mode, source_locator, receipt_kind, receipt_ref, receipt_hash, "
            "established_at) VALUES (:custody_ref, :version_ref, 'managed', NULL, "
            ":receipt_kind, :receipt_ref, :receipt_hash, :established_at)"
        ),
        {
            "custody_ref": f"legacy-custody:{version_ref}",
            "version_ref": version_ref,
            "receipt_kind": acceptance_kind,
            "receipt_ref": receipt_ref,
            "receipt_hash": receipt_hash,
            "established_at": accepted_at,
        },
    )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
