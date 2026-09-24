"""Explicit user research inputs without manufacturing a HumanRequest."""
from alembic import op
import sqlalchemy as sa
revision="0058_human_research_inputs"
down_revision="0057_reasoning_continuations"
branch_labels=None
depends_on=None
def upgrade():
 op.create_table("hc_research_inputs",
  sa.Column("input_ref",sa.Text(),primary_key=True),
  sa.Column("quest_ref",sa.Text(),sa.ForeignKey("rg_quests.quest_ref"),nullable=False),
  sa.Column("question_ref",sa.Text(),sa.ForeignKey("rg_question_lifecycle.question_ref"),nullable=True),
  sa.Column("payload_json",sa.Text(),nullable=False),sa.Column("content_hash",sa.String(64),nullable=False),
  sa.Column("idempotency_key",sa.Text(),nullable=False,unique=True),sa.Column("receipt_ref",sa.Text(),nullable=False,unique=True),
  sa.Column("receipt_hash",sa.String(64),nullable=False),sa.Column("created_at",sa.Float(),nullable=False))
 op.create_index("ix_hc_research_inputs_quest","hc_research_inputs",["quest_ref","created_at"])
def downgrade():
 raise RuntimeError("production migrations are forward-only")
