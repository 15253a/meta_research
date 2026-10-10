revision = "0072_merge_material_companion"
down_revision = (
    "0070_material_processing",
    "0071_creation_companion_rotation",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
