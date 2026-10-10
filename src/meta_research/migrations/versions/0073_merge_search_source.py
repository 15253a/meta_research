revision = "0073_merge_search_source"
down_revision = (
    "0072_merge_material_companion",
    "0070_search_source_basis",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
