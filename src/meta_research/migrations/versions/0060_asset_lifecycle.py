from alembic import op

revision = "0060_asset_lifecycle"
down_revision = "0059_research_environments"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE rm_asset_lifecycle (
        asset_ref TEXT PRIMARY KEY REFERENCES rm_assets(asset_ref),
        revision INTEGER NOT NULL DEFAULT 0,
        current_version_ref TEXT REFERENCES rm_asset_versions(version_ref))"""
    )
    op.execute(
        """CREATE TABLE rm_asset_version_lifecycle (
        version_ref TEXT PRIMARY KEY REFERENCES rm_asset_versions(version_ref),
        state TEXT NOT NULL CHECK(state IN ('unselected','current','superseded','retired')))"""
    )
    op.execute(
        """CREATE TABLE rm_asset_changes (
        change_ref TEXT PRIMARY KEY,
        asset_ref TEXT NOT NULL REFERENCES rm_assets(asset_ref),
        version_ref TEXT NOT NULL REFERENCES rm_asset_versions(version_ref),
        predecessor_version_ref TEXT REFERENCES rm_asset_versions(version_ref),
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        receipt_ref TEXT NOT NULL,
        receipt_hash TEXT NOT NULL,
        accepted_at REAL NOT NULL,
        UNIQUE(asset_ref,revision))"""
    )
    op.execute(
        "INSERT INTO rm_asset_lifecycle(asset_ref) SELECT asset_ref FROM rm_assets"
    )
    op.execute(
        "INSERT INTO rm_asset_version_lifecycle(version_ref,state) SELECT version_ref,'unselected' FROM rm_asset_versions"
    )
    op.execute(
        "CREATE INDEX ix_rm_asset_changes_asset ON rm_asset_changes(asset_ref,revision)"
    )


def downgrade():
    raise RuntimeError("production migrations are forward-only")
