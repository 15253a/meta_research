from __future__ import annotations

from alembic import op

revision = "0062_human_guidance_inboxes"
down_revision = "0061_target_result_optional_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE hc_guidance_snapshots (
        snapshot_ref TEXT PRIMARY KEY, root_kind TEXT NOT NULL,
        run_ref TEXT NOT NULL, operation_ref TEXT NOT NULL, quest_ref TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL, created_at REAL NOT NULL,
        UNIQUE(root_kind, run_ref, operation_ref))""")
    op.execute("""CREATE TABLE hc_guidance_deliveries (
        delivery_ref TEXT PRIMARY KEY, snapshot_ref TEXT NOT NULL,
        constraint_ref TEXT NOT NULL, revision INTEGER NOT NULL, guidance_hash TEXT NOT NULL,
        needs_treatment INTEGER NOT NULL CHECK(needs_treatment IN (0,1)),
        received_at REAL, read_at REAL, created_at REAL NOT NULL,
        FOREIGN KEY(snapshot_ref) REFERENCES hc_guidance_snapshots(snapshot_ref),
        FOREIGN KEY(constraint_ref) REFERENCES hc_soft_constraints(constraint_ref),
        UNIQUE(snapshot_ref, constraint_ref, revision, guidance_hash))""")
    op.execute("""CREATE TABLE hc_guidance_effects (
        effect_key TEXT PRIMARY KEY, kind TEXT NOT NULL, delivery_ref TEXT NOT NULL,
        command_hash TEXT NOT NULL, receipt_json TEXT NOT NULL, receipt_hash TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(delivery_ref) REFERENCES hc_guidance_deliveries(delivery_ref))""")
    op.execute("""CREATE TABLE hc_guidance_read_pages (
        effect_key TEXT PRIMARY KEY, delivery_ref TEXT NOT NULL,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
        FOREIGN KEY(effect_key) REFERENCES hc_guidance_effects(effect_key),
        FOREIGN KEY(delivery_ref) REFERENCES hc_guidance_deliveries(delivery_ref))""")
    op.execute("""CREATE TABLE hc_guidance_treatments (
        root_kind TEXT NOT NULL, run_ref TEXT NOT NULL, constraint_ref TEXT NOT NULL,
        revision INTEGER NOT NULL, guidance_hash TEXT NOT NULL, delivery_ref TEXT NOT NULL,
        effect_key TEXT NOT NULL UNIQUE, feedback_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(root_kind, run_ref, constraint_ref, revision, guidance_hash),
        FOREIGN KEY(effect_key) REFERENCES hc_guidance_effects(effect_key),
        FOREIGN KEY(delivery_ref) REFERENCES hc_guidance_deliveries(delivery_ref))""")
    op.execute("CREATE INDEX hc_guidance_quest ON hc_guidance_snapshots(quest_ref, created_at)")


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
