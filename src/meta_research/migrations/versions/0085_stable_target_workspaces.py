"""Keep one active TargetRun workspace and retire the recovery-copy protocol."""

import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0085_stable_target_workspaces"
down_revision = "0084_target_unknown_failure"
branch_labels = None
depends_on = None


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _verify_previous_reservations(connection):
    """Do not promote an unfinished old copy; never inspect or move work bytes."""
    rows = connection.execute(sa.text(
        "SELECT * FROM ar_target_run_workspaces ORDER BY target_run_ref, ordinal"
    )).mappings().all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["target_run_ref"], []).append(row)
    for workspaces in grouped.values():
        if ([row["ordinal"] for row in workspaces] != list(range(1, len(workspaces) + 1))
            or [row["status"] for row in workspaces] != ["retired"] * (len(workspaces) - 1) + ["active"]):
            raise RuntimeError("target_workspace_migration_incomplete")
        for predecessor, successor in zip(workspaces, workspaces[1:]):
            continuity = connection.execute(sa.text(
                "SELECT c.*, r.old_execution_attempt_ref, r.new_execution_attempt_ref, "
                "r.retired_workspace_ref FROM ar_target_root_workspace_continuities c "
                "JOIN ar_target_root_provider_recoveries r USING(transition_ref) "
                "WHERE c.successor_workspace_ref=:ref"
            ), {"ref": successor["workspace_ref"]}).mappings().first()
            if continuity is None:
                raise RuntimeError("target_workspace_migration_incomplete")
            payload = {
                "continuity_ref": continuity["continuity_ref"],
                "transition_ref": continuity["transition_ref"],
                "target_ref": successor["target_ref"],
                "predecessor_workspace_ref": predecessor["workspace_ref"],
                "predecessor_workspace_payload_hash": predecessor["payload_hash"],
                "successor_workspace_ref": successor["workspace_ref"],
                "successor_workspace_payload_hash": successor["payload_hash"],
                "manifest_hash": continuity["manifest_hash"],
            }
            receipt = {
                "issuer": "agent_runtime", "kind": "target_root_workspace_continuity_accepted",
                "receipt_ref": continuity["receipt_ref"], "subject_ref": continuity["continuity_ref"],
                "bindings": {"payload_hash": _hash(payload), **payload},
            }
            try:
                manifest = json.loads(continuity["manifest_json"])
            except (TypeError, ValueError) as error:
                raise RuntimeError("target_workspace_migration_integrity_invalid") from error
            if (continuity["target_ref"] != successor["target_ref"]
                or predecessor["target_ref"] != successor["target_ref"]
                or continuity["predecessor_workspace_ref"] != predecessor["workspace_ref"]
                or continuity["retired_workspace_ref"] != predecessor["workspace_ref"]
                or continuity["old_execution_attempt_ref"] != predecessor["target_attempt_ref"]
                or continuity["new_execution_attempt_ref"] != successor["target_attempt_ref"]
                or continuity["payload_json"] != _json(payload)
                or continuity["payload_hash"] != _hash(payload)
                or continuity["manifest_json"] != _json(manifest)
                or continuity["manifest_hash"] != _hash(manifest)
                or continuity["request_hash"] != _hash({"command": "accept_target_root_workspace_continuity", **payload})
                or continuity["receipt_hash"] != _hash(receipt)):
                raise RuntimeError("target_workspace_migration_integrity_invalid")


def upgrade():
    connection = op.get_bind()
    _verify_previous_reservations(connection)
    # Existing columns/payloads attest the original reservation. Keep them and
    # retired directory rows unchanged for linked originals, delivery and GC.
    op.create_index("uq_target_run_active_workspace", "ar_target_run_workspaces",
                    ["target_run_ref"], unique=True, sqlite_where=sa.text("status='active'"))
    op.drop_table("ar_target_root_workspace_continuities")
    table = sa.Table("ar_target_root_provider_recoveries", sa.MetaData(), autoload_with=connection)
    for constraint in tuple(table.constraints):
        if any(column.name == "retired_workspace_ref" for column in constraint.columns):
            table.constraints.remove(constraint)
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table("ar_target_root_provider_recoveries", copy_from=table,
                                  recreate="always") as batch:
            batch.drop_column("retired_workspace_ref")
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def downgrade():
    raise RuntimeError("production migrations are forward-only")
