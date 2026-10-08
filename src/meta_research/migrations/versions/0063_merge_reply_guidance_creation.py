"""Join independently implemented reply, guidance, and creation migrations."""

revision = "0063_merge_reply_guidance_creation"
down_revision = (
    "0062_human_reply_delivery",
    "0062_human_guidance_inboxes",
    "0062_creation_research_basis",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
