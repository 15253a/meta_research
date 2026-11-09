"""Persist shared Quest research time and explicitly approved budget revisions."""
from alembic import op
import sqlalchemy as sa

revision = "0081_quest_research_time"
down_revision = "0080_writing_display_message"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # Alembic retains named checks during SQLite batch recreation, but unnamed
    # integrity checks (hash lengths, revision, authorization pairing) must be
    # supplied explicitly. Keep them while expanding only the request kind.
    unnamed_checks = tuple(sa.CheckConstraint(item["sqltext"])
                           for item in sa.inspect(connection).get_check_constraints("owner_human_requests")
                           if not item.get("name"))
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table("owner_human_requests", recreate="always", table_args=unnamed_checks) as batch:
            batch.drop_constraint("ck_owner_human_requests_kind", type_="check")
            batch.create_check_constraint("ck_owner_human_requests_kind",
                "kind IN ('library_reconnect','external_material_api_access','offline_action',"
                "'capability_authorization','system_operation_help','research_time_extension')")
    finally:
        connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    op.create_table(
        "ae_research_time_service",
        sa.Column("singleton", sa.String(24), primary_key=True),
        sa.Column("service_ref", sa.String(96), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("last_observed_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "ae_quest_time_budgets",
        sa.Column("quest_ref", sa.String(160), primary_key=True),
        sa.Column("budget_revision", sa.String(64), nullable=False),
        sa.Column("approved_seconds", sa.Float(), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("confirmed_used_seconds", sa.Float(), nullable=False),
        sa.Column("checkpoint_at", sa.Float(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("unknown_intervals_json", sa.Text(), nullable=False),
        sa.Column("accounting_adjustment_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("accounting_corrections_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "ae_quest_time_budget_revisions",
        sa.Column("budget_revision", sa.String(64), primary_key=True),
        sa.Column("quest_ref", sa.String(160), nullable=False),
        sa.Column("prior_revision", sa.String(64), nullable=True),
        sa.Column("approved_seconds", sa.Float(), nullable=True),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "ae_quest_time_budget_approvals",
        sa.Column("approval_ref", sa.String(160), primary_key=True),
        sa.Column("quest_ref", sa.String(160), nullable=False),
        sa.Column("request_ref", sa.String(160), nullable=False),
        sa.Column("response_ref", sa.String(160), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
    )
    # A frozen model operation remains replayable while its next logical
    # decision observes a later clock. This is not a new operation identity.
    op.add_column("ar_harness_provider_operations", sa.Column("frozen_prompt", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("production migrations are forward-only")
