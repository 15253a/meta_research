from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op

from meta_research.owners.common import canonical_hash


revision = "0065_quest_goal_evolution"
down_revision = "0064_work_materials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hc_runtime_condition_versions",
        sa.Column("scope_ref", sa.String(160), primary_key=True),
        sa.Column("revision", sa.String(64), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(revision) = 64"),
        sa.CheckConstraint(
            "source_kind IN ('initial_default', 'legacy_file', 'human_update')"
        ),
    )
    op.create_table(
        "hc_runtime_condition_heads",
        sa.Column("scope_ref", sa.String(160), primary_key=True),
        sa.Column("current_revision", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_ref", "current_revision"],
            ["hc_runtime_condition_versions.scope_ref", "hc_runtime_condition_versions.revision"],
        ),
        sa.CheckConstraint("length(current_revision) = 64"),
    )

    op.create_table(
        "rg_quest_goal_heads",
        sa.Column("quest_ref", sa.String(96), primary_key=True),
        sa.Column("current_revision_ref", sa.String(96), nullable=False),
        sa.Column("current_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completion_acceptance_ref", sa.String(96), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.CheckConstraint("current_sequence >= 0"),
        sa.CheckConstraint("status IN ('open', 'completion_committed')"),
        sa.CheckConstraint(
            "(status = 'open' AND completion_acceptance_ref IS NULL) OR "
            "(status = 'completion_committed' AND completion_acceptance_ref IS NOT NULL)"
        ),
    )
    op.create_table(
        "rg_quest_goal_revisions",
        sa.Column("revision_ref", sa.String(96), primary_key=True),
        sa.Column("quest_ref", sa.String(96), nullable=False),
        sa.Column("parent_ref", sa.String(96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("author_json", sa.Text(), nullable=False),
        sa.Column("author_hash", sa.String(64), nullable=False),
        sa.Column("cause_json", sa.Text(), nullable=False),
        sa.Column("cause_hash", sa.String(64), nullable=False),
        sa.Column("receipt_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["quest_ref"], ["rg_quests.quest_ref"]),
        sa.UniqueConstraint("quest_ref", "sequence"),
        *(
            sa.CheckConstraint(f"length({name}) = 64")
            for name in (
                "decision_hash",
                "author_hash",
                "cause_hash",
                "receipt_hash",
            )
        ),
    )
    op.create_index(
        "ix_rg_goal_revision_history",
        "rg_quest_goal_revisions",
        ["quest_ref", "sequence"],
        unique=True,
    )
    op.create_table(
        "rg_goal_revision_conditions",
        sa.Column("revision_ref", sa.String(96), primary_key=True),
        sa.Column("condition_ref", sa.String(96), primary_key=True),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("meaning", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("superseding_source_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["revision_ref"], ["rg_quest_goal_revisions.revision_ref"]
        ),
        sa.CheckConstraint("length(source_hash) = 64"),
        sa.CheckConstraint("status IN ('active', 'human_superseded')"),
    )
    op.create_table(
        "rg_goal_evolution_effects",
        sa.Column("effect_key", sa.String(96), primary_key=True),
        sa.Column("root_kind", sa.String(24), nullable=False),
        sa.Column("run_ref", sa.String(96), nullable=False),
        sa.Column("operation_ref", sa.String(96), nullable=False),
        sa.Column("effect_id", sa.String(128), nullable=False),
        sa.Column("command_json", sa.Text(), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("revision_ref", sa.String(96), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_ref"], ["rg_quest_goal_revisions.revision_ref"]
        ),
        sa.UniqueConstraint("root_kind", "run_ref", "operation_ref", "effect_id"),
        sa.CheckConstraint("length(command_hash) = 64"),
        sa.CheckConstraint("length(receipt_hash) = 64"),
    )
    op.create_table(
        "rg_goal_work_intents",
        sa.Column("intent_ref", sa.String(96), primary_key=True),
        sa.Column("revision_ref", sa.String(96), nullable=False),
        sa.Column("work_ref", sa.String(96), nullable=False),
        sa.Column("decision_kind", sa.String(32), nullable=False),
        sa.Column("work_json", sa.Text(), nullable=False),
        sa.Column("work_hash", sa.String(64), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_ref"], ["rg_quest_goal_revisions.revision_ref"]
        ),
        sa.UniqueConstraint("revision_ref", "work_ref"),
        sa.CheckConstraint(
            "decision_kind IN ('continue', 'stop', 'do_not_start', 'finish_stage_boundary')"
        ),
        sa.CheckConstraint("length(work_hash) = 64"),
        sa.CheckConstraint("length(decision_hash) = 64"),
    )
    op.create_index(
        "ix_rg_goal_work_intents_work",
        "rg_goal_work_intents",
        ["work_ref", "created_at"],
    )
    op.add_column(
        "ar_target_root_lifecycles",
        sa.Column("cancel_goal_intent_ref", sa.String(96), nullable=True),
    )
    op.create_index(
        "uq_ar_target_cancel_goal_intent",
        "ar_target_root_lifecycles",
        ["cancel_goal_intent_ref"],
        unique=True,
    )
    op.create_table(
        "ar_target_goal_admission_blocks",
        sa.Column("target_ref", sa.String(96), primary_key=True),
        sa.Column("target_run_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("intent_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["target_ref"], ["rg_targets.target_ref"]),
        sa.ForeignKeyConstraint(
            ["intent_ref"], ["rg_goal_work_intents.intent_ref"]
        ),
        sa.CheckConstraint("state_revision >= 0"),
    )
    op.create_table(
        "rm_target_note_intake_effects",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("job_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("binding_json", sa.Text(), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["job_ref"], ["rm_asset_intakes.job_ref"]),
        sa.CheckConstraint("length(request_hash) = 64"),
        sa.CheckConstraint("length(binding_hash) = 64"),
    )

    connection = op.get_bind()
    now = time.time()
    for row in connection.execute(
        sa.text("SELECT quest_ref,draft_revision,draft_hash FROM rg_quests")
    ).mappings():
        revision_ref = "quest_goal_revision_" + canonical_hash(
            {
                "quest_ref": row["quest_ref"],
                "draft_revision": int(row["draft_revision"]),
                "draft_hash": row["draft_hash"],
            }
        )[:32]
        connection.execute(
            sa.text(
                "INSERT INTO rg_quest_goal_heads "
                "(quest_ref,current_revision_ref,current_sequence,status,updated_at) "
                "VALUES (:quest_ref,:revision_ref,0,'open',:now)"
            ),
            {"quest_ref": row["quest_ref"], "revision_ref": revision_ref, "now": now},
        )


def downgrade() -> None:
    raise RuntimeError("production migrations are forward-only")
