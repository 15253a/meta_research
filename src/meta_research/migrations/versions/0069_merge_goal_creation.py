revision = "0069_merge_goal_creation"
down_revision = (
    "0065_quest_goal_evolution",
    "0068_manual_creation_inputs",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
