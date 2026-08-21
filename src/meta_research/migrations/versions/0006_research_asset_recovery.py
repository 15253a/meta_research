"""Harden Research Asset recovery and the RG material-role boundary.

Revision ID: 0006_research_asset_recovery
Revises: 0005_research_assets
Create Date: 2026-08-21
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import sqlalchemy as sa
from alembic import op


revision = "0006_research_asset_recovery"
down_revision = "0005_research_assets"
branch_labels = None
depends_on = None

_RECEIPT_SCHEMA = "meta-research/owner-acceptance-receipt/v1"
_RM_OWNER = "research_memory"
_LOCATOR_MIGRATED_KIND = "asset_custody_locator_migrated"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_check(name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({name}) = 64")


def upgrade() -> None:
    _extend_hc_reconciliation_steps()
    op.add_column(
        "rm_asset_intakes",
        sa.Column("request_source_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "rm_asset_intakes",
        sa.Column("request_custody_mode", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "rm_asset_intakes",
        sa.Column(
            "request_payload_scrubbed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "rm_asset_intakes",
        sa.Column(
            "next_attempt_at",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_rm_asset_intakes_due",
        "rm_asset_intakes",
        ["status", "next_attempt_at", "updated_at"],
    )
    op.add_column(
        "rm_asset_custodies",
        sa.Column("locator_binding_kind", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "rm_asset_custodies",
        sa.Column("locator_binding_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "rm_asset_custodies",
        sa.Column("locator_binding_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "rm_asset_custodies",
        sa.Column(
            "locator_binding_request_hash", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "rm_asset_custodies",
        sa.Column("locator_bound_at", sa.Float(), nullable=True),
    )
    op.create_table(
        "rm_managed_objects",
        sa.Column("object_path", sa.Text(), primary_key=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.Float(), nullable=False),
        sa.CheckConstraint("byte_count >= 0"),
        _hash_check("content_hash"),
    )
    op.create_table(
        "rm_asset_verification_observations",
        sa.Column("version_ref", sa.String(length=64), primary_key=True),
        sa.Column("integrity", sa.String(length=16), nullable=False),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.Column("next_verify_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_ref"], ["rm_asset_versions.version_ref"]
        ),
        sa.CheckConstraint("integrity IN ('verified', 'failed', 'unknown')"),
        sa.CheckConstraint(
            "availability IN ('available', 'unavailable', 'drifted', 'unknown')"
        ),
    )
    op.create_index(
        "ix_rm_asset_verification_due",
        "rm_asset_verification_observations",
        ["next_verify_at", "version_ref"],
    )
    op.create_index(
        "ix_rm_asset_verification_integrity",
        "rm_asset_verification_observations",
        ["integrity", "version_ref"],
    )
    op.create_table(
        "rm_asset_verification_state",
        sa.Column("singleton", sa.String(length=16), primary_key=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("uncertain_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("singleton = 'owner'"),
        sa.CheckConstraint("observation_count >= 0"),
        sa.CheckConstraint("uncertain_count >= 0"),
        sa.CheckConstraint("uncertain_count <= observation_count"),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO rm_asset_verification_state (singleton, "
            "observation_count, uncertain_count) VALUES ('owner', 0, 0)"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER rm_asset_verification_state_insert AFTER INSERT "
            "ON rm_asset_verification_observations BEGIN UPDATE "
            "rm_asset_verification_state SET observation_count = "
            "observation_count + 1, uncertain_count = uncertain_count + "
            "CASE WHEN NEW.integrity != 'verified' THEN 1 ELSE 0 END WHERE "
            "singleton = 'owner'; END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER rm_asset_verification_state_update AFTER UPDATE OF "
            "integrity ON rm_asset_verification_observations BEGIN UPDATE "
            "rm_asset_verification_state SET uncertain_count = uncertain_count "
            "- CASE WHEN OLD.integrity != 'verified' THEN 1 ELSE 0 END + CASE "
            "WHEN NEW.integrity != 'verified' THEN 1 ELSE 0 END WHERE singleton "
            "= 'owner'; END"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER rm_asset_verification_state_delete AFTER DELETE "
            "ON rm_asset_verification_observations BEGIN UPDATE "
            "rm_asset_verification_state SET observation_count = "
            "observation_count - 1, uncertain_count = uncertain_count - CASE "
            "WHEN OLD.integrity != 'verified' THEN 1 ELSE 0 END WHERE singleton "
            "= 'owner'; END"
        )
    )
    op.create_table(
        "rm_asset_repair_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("custody_ref", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["custody_ref"], ["rm_asset_custodies.custody_ref"]
        ),
        sa.CheckConstraint("status IN ('processing', 'completed')"),
        sa.CheckConstraint(
            "(status = 'processing' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)"
        ),
        _hash_check("request_hash"),
    )
    op.create_index(
        "uq_rm_asset_repair_processing_custody",
        "rm_asset_repair_commands",
        ["custody_ref"],
        unique=True,
        sqlite_where=sa.text("status = 'processing'"),
    )
    op.create_table(
        "rg_asset_role_commands",
        sa.Column("idempotency_key", sa.String(length=128), primary_key=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("role_ref", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["role_ref"], ["rg_asset_roles.role_ref"]),
        _hash_check("request_hash"),
    )
    op.create_index(
        "ix_rg_asset_roles_version_ref", "rg_asset_roles", ["version_ref"]
    )
    op.create_index(
        "ix_rg_questions_content_ref", "rg_questions", ["content_ref"]
    )
    op.create_index(
        "ix_rg_idea_outcome_decisions_content_ref",
        "rg_idea_outcome_decisions",
        ["idea_content_ref"],
    )
    op.create_index(
        "ix_rm_asset_holds_version_history",
        "rm_asset_holds",
        ["version_ref", "placed_at", "hold_ref"],
    )
    op.create_index(
        "ix_rm_asset_holds_active_version",
        "rm_asset_holds",
        ["version_ref", "hold_ref"],
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "ix_rm_release_assessments_version_history",
        "rm_release_eligibility_assessments",
        ["version_ref", "assessed_at", "assessment_ref"],
    )
    _backfill_managed_objects_and_role_commands()
    _upgrade_legacy_asset_custody_receipts()
    _backfill_and_scrub_asset_intakes()
    _install_asset_verification_observations()


def _extend_hc_reconciliation_steps() -> None:
    connection = op.get_bind()
    op.rename_table(
        "hc_reconciliation_checkpoints",
        "hc_reconciliation_checkpoints_pre_asset_roles",
    )
    op.create_table(
        "hc_reconciliation_checkpoints",
        sa.Column("initialization_id", sa.String(length=64), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("first_missing_step", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("next_retry_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.CheckConstraint("state IN ('idle', 'partial', 'recovering', 'completed')"),
        sa.CheckConstraint(
            "first_missing_step IS NULL OR first_missing_step IN "
            "('quest_goal', 'quest_source_material', 'question_content', "
            "'question_identity', 'cycle_activation')"
        ),
        sa.CheckConstraint("attempt_count >= 0"),
        sa.CheckConstraint(
            "(state IN ('idle', 'completed') AND first_missing_step IS NULL "
            "AND reason_code IS NULL AND next_retry_at IS NULL) OR "
            "(state IN ('partial', 'recovering') AND first_missing_step IS NOT "
            "NULL AND reason_code IS NOT NULL)"
        ),
    )
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_checkpoints SELECT * FROM "
            "hc_reconciliation_checkpoints_pre_asset_roles"
        )
    )
    op.drop_table("hc_reconciliation_checkpoints_pre_asset_roles")
    op.create_index(
        "ix_hc_reconciliation_checkpoints_due",
        "hc_reconciliation_checkpoints",
        ["state", "next_retry_at"],
    )

    op.rename_table(
        "hc_reconciliation_attempts",
        "hc_reconciliation_attempts_pre_asset_roles",
    )
    op.create_table(
        "hc_reconciliation_attempts",
        sa.Column("attempt_ref", sa.String(length=64), primary_key=True),
        sa.Column("initialization_id", sa.String(length=64), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["initialization_id"],
            ["hc_quest_initializations.initialization_id"],
        ),
        sa.UniqueConstraint("initialization_id", "step", "attempt_number"),
        sa.CheckConstraint(
            "step IN ('quest_goal', 'quest_source_material', 'question_content', "
            "'question_identity', 'cycle_activation')"
        ),
        sa.CheckConstraint("attempt_number >= 1"),
        sa.CheckConstraint(
            "outcome IN ('started', 'accepted', 'transient_failure', 'rejected', "
            "'stale')"
        ),
        sa.CheckConstraint(
            "(outcome = 'started' AND reason_code IS NULL AND finished_at IS NULL) "
            "OR (outcome = 'accepted' AND reason_code IS NULL AND finished_at IS "
            "NOT NULL) OR (outcome IN ('transient_failure', 'rejected', 'stale') "
            "AND reason_code IS NOT NULL AND finished_at IS NOT NULL)"
        ),
    )
    connection.execute(
        sa.text(
            "INSERT INTO hc_reconciliation_attempts SELECT * FROM "
            "hc_reconciliation_attempts_pre_asset_roles"
        )
    )
    op.drop_table("hc_reconciliation_attempts_pre_asset_roles")


def _backfill_managed_objects_and_role_commands() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT DISTINCT versions.version_ref, versions.manifest_json, "
            "versions.accepted_at FROM rm_asset_versions AS versions JOIN "
            "rm_asset_custodies AS custodies ON custodies.version_ref = "
            "versions.version_ref WHERE custodies.custody_mode = 'managed'"
        )
    ).mappings()
    for row in rows:
        manifest = json.loads(row["manifest_json"])
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError("asset manifest invalid during 0006 backfill")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("asset manifest invalid during 0006 backfill")
            digest = entry.get("sha256")
            size = entry.get("size")
            object_path = entry.get("object_path")
            if not isinstance(digest, str) or not isinstance(size, int):
                raise RuntimeError("asset manifest invalid during 0006 backfill")
            if not isinstance(object_path, str):
                object_path = f"assets/{digest[:2]}/{digest}"
            connection.execute(
                sa.text(
                    "INSERT OR IGNORE INTO rm_managed_objects (object_path, "
                    "content_hash, byte_count, registered_at) VALUES "
                    "(:object_path, :content_hash, :byte_count, :registered_at)"
                ),
                {
                    "object_path": object_path,
                    "content_hash": digest,
                    "byte_count": size,
                    "registered_at": float(row["accepted_at"]),
                },
            )
    connection.execute(
        sa.text(
            "INSERT INTO rg_asset_role_commands (idempotency_key, request_hash, "
            "role_ref, recorded_at) SELECT idempotency_key, request_hash, "
            "role_ref, accepted_at FROM rg_asset_roles"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE research_memory_state SET object_count = "
            "(SELECT COUNT(*) FROM rm_managed_objects) WHERE singleton = 'owner'"
        )
    )


def _install_asset_verification_observations() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO rm_asset_verification_observations (version_ref, "
            "integrity, availability, observed_at, next_verify_at) SELECT "
            "version_ref, 'unknown', 'unknown', accepted_at, 0 FROM "
            "rm_asset_versions"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TRIGGER rm_asset_version_verification_observation AFTER "
            "INSERT ON rm_asset_versions BEGIN INSERT INTO "
            "rm_asset_verification_observations (version_ref, integrity, "
            "availability, observed_at, next_verify_at) VALUES "
            "(NEW.version_ref, 'unknown', 'unknown', NEW.accepted_at, 0); END"
        )
    )


def _backfill_and_scrub_asset_intakes() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT job_ref, request_json, request_hash, status FROM "
            "rm_asset_intakes"
        )
    ).mappings()
    for row in rows:
        try:
            document = json.loads(row["request_json"])
            canonical_request = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if (
                canonical_request != row["request_json"]
                or hashlib.sha256(row["request_json"].encode("utf-8")).hexdigest()
                != row["request_hash"]
            ):
                raise ValueError("asset intake request binding invalid")
            source_kind = document["source_kind"]
            custody_mode = document["custody_mode"]
            if not isinstance(source_kind, str) or not isinstance(
                custody_mode, str
            ):
                raise ValueError("asset intake summary invalid")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        scrub_payload = row["status"] in {"accepted", "failed"}
        summary_json = json.dumps(
            {
                "custody_mode": custody_mode,
                "payload_scrubbed": True,
                "source_kind": source_kind,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            sa.text(
                "UPDATE rm_asset_intakes SET request_source_kind = "
                ":source_kind, request_custody_mode = :custody_mode, "
                "request_payload_scrubbed = :scrubbed, request_json = "
                ":request_json WHERE job_ref = :job_ref"
            ),
            {
                "job_ref": row["job_ref"],
                "source_kind": source_kind,
                "custody_mode": custody_mode,
                "scrubbed": scrub_payload,
                "request_json": (
                    summary_json if scrub_payload else row["request_json"]
                ),
            },
        )


def _upgrade_legacy_asset_custody_receipts() -> None:
    """Bind trustworthy absolute 0005 locators without rewriting Asset receipts."""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT custodies.custody_ref, custodies.version_ref, "
            "custodies.custody_mode, custodies.source_locator, "
            "custodies.receipt_ref AS custody_receipt_ref, "
            "custodies.receipt_hash AS custody_receipt_hash, "
            "versions.source_kind, versions.content_hash, "
            "versions.manifest_hash, intakes.request_json, "
            "intakes.request_hash FROM rm_asset_custodies AS custodies JOIN "
            "rm_asset_versions AS versions ON versions.version_ref = "
            "custodies.version_ref JOIN rm_asset_intakes AS intakes ON "
            "intakes.version_ref = versions.version_ref AND intakes.status = "
            "'accepted' WHERE custodies.receipt_kind = 'asset_acceptance' AND "
            "custodies.source_locator IS NOT NULL"
        )
    ).mappings()
    migrated_at = time.time()
    local_source_kinds = {
        "directory",
        "file",
        "local_path",
        "repository",
        "system_artifact",
    }
    for row in rows:
        try:
            document = json.loads(row["request_json"])
            if (
                _canonical_json(document) != row["request_json"]
                or hashlib.sha256(
                    row["request_json"].encode("utf-8")
                ).hexdigest()
                != row["request_hash"]
                or document.get("source_kind") != row["source_kind"]
                or document.get("custody_mode") != row["custody_mode"]
                or document.get("source_locator") != row["source_locator"]
                or row["source_kind"] not in local_source_kinds
                or not Path(str(row["source_locator"])).is_absolute()
            ):
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        binding_ref = (
            "rm_custody_0006_"
            + _canonical_hash(
                {
                    "custody_ref": row["custody_ref"],
                    "request_hash": row["request_hash"],
                }
            )[:47]
        )
        binding_hash = _canonical_hash(
            {
                "schema_ref": _RECEIPT_SCHEMA,
                "issuer": _RM_OWNER,
                "kind": _LOCATOR_MIGRATED_KIND,
                "subject_ref": row["custody_ref"],
                "bindings": {
                    "version_ref": row["version_ref"],
                    "content_hash": row["content_hash"],
                    "manifest_hash": row["manifest_hash"],
                    "custody_mode": row["custody_mode"],
                    "source_locator": row["source_locator"],
                    "request_hash": row["request_hash"],
                    "prior_receipt_ref": row["custody_receipt_ref"],
                    "prior_receipt_hash": row["custody_receipt_hash"],
                    "bound_at": migrated_at,
                },
            }
        )
        connection.execute(
            sa.text(
                "UPDATE rm_asset_custodies SET locator_binding_kind = "
                ":binding_kind, locator_binding_ref = :binding_ref, "
                "locator_binding_hash = :binding_hash, "
                "locator_binding_request_hash = :request_hash, "
                "locator_bound_at = :bound_at WHERE custody_ref = :custody_ref"
            ),
            {
                "custody_ref": row["custody_ref"],
                "binding_kind": _LOCATOR_MIGRATED_KIND,
                "binding_ref": binding_ref,
                "binding_hash": binding_hash,
                "request_hash": row["request_hash"],
                "bound_at": migrated_at,
            },
        )


def downgrade() -> None:
    raise RuntimeError("vNext production migrations are forward-only")
