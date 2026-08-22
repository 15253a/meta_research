from __future__ import annotations

import hashlib
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, event

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _upgrade_to_revision(database: Path, revision: str) -> None:
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database)), future=True
    )
    event.listen(engine, "connect", _configure_migration_sqlite)
    event.listen(engine, "begin", _begin_migration_transaction)
    config = Config()
    config.set_main_option(
        "script_location", str(files("meta_research.migrations"))
    )
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _seed_completed_idea_chain(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO rm_formal_question_contents "
        "(content_ref, initialization_id, quest_ref, quest_receipt_ref, "
        "quest_receipt_hash, proposal_ref, proposal_hash, confirmation_ref, "
        "confirmation_hash, content_hash, schema_ref, content_json, object_path, "
        "receipt_ref, receipt_hash, accepted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "question-content-idea",
            "initialization-idea",
            "quest-idea",
            "quest-receipt-idea",
            _hash("quest-receipt"),
            "proposal-idea",
            _hash("proposal"),
            "confirmation-idea",
            _hash("confirmation"),
            _hash("question-content"),
            "meta-research/formal-question/v1",
            '{"title":"preserved idea question"}',
            "objects/question-content-idea.json",
            "question-content-receipt-idea",
            _hash("question-content-receipt"),
            10.0,
        ),
    )
    connection.execute(
        "INSERT INTO rg_quests "
        "(quest_ref, initialization_id, draft_revision, draft_hash, proposal_ref, "
        "proposal_hash, preview_ref, preview_hash, goal_json, confirmation_ref, "
        "confirmation_hash, receipt_ref, receipt_hash, accepted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "quest-idea",
            "initialization-idea",
            1,
            _hash("draft"),
            "proposal-idea",
            _hash("proposal"),
            "preview-idea",
            _hash("preview"),
            '{"goal":"preserved"}',
            "confirmation-idea",
            _hash("confirmation"),
            "quest-receipt-idea",
            _hash("quest-receipt"),
            11.0,
        ),
    )
    connection.execute(
        "INSERT INTO rg_questions "
        "(question_ref, initialization_id, quest_ref, content_ref, content_hash, "
        "schema_ref, quest_receipt_ref, quest_receipt_hash, content_receipt_ref, "
        "content_receipt_hash, confirmation_ref, receipt_ref, receipt_hash, "
        "accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "question-idea",
            "initialization-idea",
            "quest-idea",
            "question-content-idea",
            _hash("question-content"),
            "meta-research/formal-question/v1",
            "quest-receipt-idea",
            _hash("quest-receipt"),
            "question-content-receipt-idea",
            _hash("question-content-receipt"),
            "confirmation-idea",
            "question-receipt-idea",
            _hash("question-receipt"),
            12.0,
        ),
    )
    connection.execute(
        "INSERT INTO ae_initial_cycles "
        "(cycle_ref, initialization_id, quest_ref, question_ref, "
        "question_receipt_ref, question_receipt_hash, quest_receipt_ref, "
        "quest_receipt_hash, receipt_ref, receipt_hash, activated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cycle-idea",
            "initialization-idea",
            "quest-idea",
            "question-idea",
            "question-receipt-idea",
            _hash("question-receipt"),
            "quest-receipt-idea",
            _hash("quest-receipt"),
            "cycle-receipt-idea",
            _hash("cycle-receipt"),
            13.0,
        ),
    )
    connection.execute(
        "INSERT INTO ae_stage_run_requests "
        "(request_ref, cycle_ref, stage, epoch, initialization_id, quest_ref, "
        "question_ref, content_ref, content_hash, schema_ref, content_receipt_ref, "
        "content_receipt_hash, question_receipt_ref, question_receipt_hash, "
        "context_pack_ref, context_pack_json, context_pack_hash, idempotency_key, "
        "request_hash, receipt_ref, receipt_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "request-idea",
            "cycle-idea",
            "idea",
            1,
            "initialization-idea",
            "quest-idea",
            "question-idea",
            "question-content-idea",
            _hash("question-content"),
            "meta-research/formal-question/v1",
            "question-content-receipt-idea",
            _hash("question-content-receipt"),
            "question-receipt-idea",
            _hash("question-receipt"),
            "context-idea",
            '{"schema_ref":"meta-research/idea-context-pack/v2"}',
            _hash("context"),
            "request-idea-key",
            _hash("request"),
            "request-receipt-idea",
            _hash("request-receipt"),
            14.0,
        ),
    )
    connection.execute(
        "INSERT INTO ar_stage_runs "
        "(run_ref, request_ref, cycle_ref, stage, epoch, context_pack_ref, "
        "context_pack_hash, runtime_binding_json, runtime_binding_hash, "
        "request_receipt_ref, request_receipt_hash, status, current_attempt_ref, "
        "root_session_ref, current_fence_ref, completion_receipt_ref, "
        "completion_receipt_hash, outcome_ref, admission_key, admission_hash, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run-idea",
            "request-idea",
            "cycle-idea",
            "idea",
            1,
            "context-idea",
            _hash("context"),
            '{"schema_ref":"meta-research/idea-runtime-binding/v1"}',
            _hash("runtime-binding"),
            "request-receipt-idea",
            _hash("request-receipt"),
            "completed",
            "attempt-idea",
            "session-idea",
            "fence-idea",
            "completion-receipt-idea",
            _hash("completion-receipt"),
            "outcome-idea",
            "admit-idea-key",
            _hash("admission"),
            15.0,
            21.0,
        ),
    )
    connection.execute(
        "INSERT INTO ar_stage_sessions "
        "(session_ref, run_ref, native_session_ref, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("session-idea", "run-idea", "native-session-idea", "completed", 15.0, 21.0),
    )
    connection.execute(
        "INSERT INTO ar_stage_attempts "
        "(attempt_ref, run_ref, generation, root_session_ref, fence_ref, status, "
        "primary_draft_json, primary_draft_hash, primary_adapter_kind, "
        "primary_recorded_at, submission_ref, payload_json, payload_hash, "
        "material_outcome_hash, execution_receipt_ref, execution_receipt_hash, "
        "decision_receipt_ref, decision_receipt_subject_ref, decision_receipt_hash, "
        "created_at, executed_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "attempt-idea",
            "run-idea",
            1,
            "session-idea",
            "fence-idea",
            "completed",
            '{"kind":"IdeaSet"}',
            _hash("primary-draft"),
            "codex",
            16.0,
            "submission-idea",
            '{"outcome":{"kind":"IdeaSet"}}',
            _hash("payload"),
            _hash("outcome"),
            "execution-receipt-idea",
            _hash("execution-receipt"),
            "decision-receipt-idea",
            "outcome-idea",
            _hash("decision-receipt"),
            15.0,
            18.0,
            20.0,
        ),
    )
    connection.execute(
        "INSERT INTO ar_execution_fences "
        "(fence_ref, run_ref, attempt_ref, generation, status, issued_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("fence-idea", "run-idea", "attempt-idea", 1, "completed", 15.0, 20.0),
    )
    for phase in ("primary", "review"):
        connection.execute(
            "INSERT INTO ar_idea_provider_invocations "
            "(invocation_ref, run_ref, attempt_ref, fence_ref, phase, request_hash, "
            "runtime_binding_hash, status, response_hash, prepared_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"invocation-idea-{phase}",
                "run-idea",
                "attempt-idea",
                "fence-idea",
                phase,
                _hash(f"provider-request-{phase}"),
                _hash("runtime-binding"),
                "completed",
                _hash(f"provider-response-{phase}"),
                16.0,
                18.0,
            ),
        )
    connection.execute(
        "INSERT INTO rm_idea_outcome_contents "
        "(content_ref, request_ref, run_ref, attempt_ref, fence_ref, submission_ref, "
        "outcome_kind, outcome_json, outcome_hash, reviewed_draft_json, "
        "reviewed_draft_hash, review_json, review_hash, payload_json, payload_hash, "
        "object_path, execution_receipt_ref, execution_receipt_hash, receipt_ref, "
        "receipt_hash, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "idea-content-idea",
            "request-idea",
            "run-idea",
            "attempt-idea",
            "fence-idea",
            "submission-idea",
            "idea_set",
            '{"kind":"IdeaSet"}',
            _hash("outcome"),
            '{"kind":"IdeaSet"}',
            _hash("outcome"),
            '{"findings":[]}',
            _hash("review"),
            '{"outcome":{"kind":"IdeaSet"}}',
            _hash("payload"),
            "objects/idea-content-idea.json",
            "execution-receipt-idea",
            _hash("execution-receipt"),
            "idea-content-receipt-idea",
            _hash("idea-content-receipt"),
            19.0,
        ),
    )
    connection.execute(
        "INSERT INTO rg_idea_outcome_decisions "
        "(decision_ref, request_ref, submission_ref, run_ref, attempt_ref, fence_ref, "
        "initialization_id, quest_ref, question_ref, context_pack_ref, "
        "question_content_ref, question_content_hash, question_receipt_ref, "
        "question_receipt_hash, idea_content_ref, idea_content_receipt_ref, "
        "idea_content_receipt_hash, execution_receipt_ref, execution_receipt_hash, "
        "outcome_kind, payload_hash, outcome_hash, reviewed_draft_hash, review_hash, "
        "decision, outcome_ref, reason_code, feedback_json, feedback_hash, receipt_ref, "
        "receipt_hash, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "decision-idea",
            "request-idea",
            "submission-idea",
            "run-idea",
            "attempt-idea",
            "fence-idea",
            "initialization-idea",
            "quest-idea",
            "question-idea",
            "context-idea",
            "question-content-idea",
            _hash("question-content"),
            "question-receipt-idea",
            _hash("question-receipt"),
            "idea-content-idea",
            "idea-content-receipt-idea",
            _hash("idea-content-receipt"),
            "execution-receipt-idea",
            _hash("execution-receipt"),
            "idea_set",
            _hash("payload"),
            _hash("outcome"),
            _hash("outcome"),
            _hash("review"),
            "accepted",
            "outcome-idea",
            None,
            "[]",
            _hash("feedback"),
            "decision-receipt-idea",
            _hash("decision-receipt"),
            20.0,
        ),
    )
    connection.execute(
        "INSERT INTO ae_stage_commits "
        "(commit_ref, request_ref, cycle_ref, stage, epoch, run_ref, outcome_ref, "
        "outcome_kind, disposition, run_completion_receipt_ref, "
        "run_completion_receipt_hash, outcome_receipt_ref, outcome_receipt_hash, "
        "idempotency_key, request_hash, receipt_ref, receipt_hash, committed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "commit-idea",
            "request-idea",
            "cycle-idea",
            "idea",
            1,
            "run-idea",
            "outcome-idea",
            "idea_set",
            "completed",
            "completion-receipt-idea",
            _hash("completion-receipt"),
            "decision-receipt-idea",
            _hash("decision-receipt"),
            "commit-idea-key",
            _hash("commit-request"),
            "commit-receipt-idea",
            _hash("commit-receipt"),
            21.0,
        ),
    )
    connection.commit()


def _insert_plan_request(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO ae_stage_run_requests "
        "(request_ref, cycle_ref, stage, epoch, initialization_id, quest_ref, "
        "question_ref, content_ref, content_hash, schema_ref, content_receipt_ref, "
        "content_receipt_hash, question_receipt_ref, question_receipt_hash, "
        "context_pack_ref, context_pack_json, context_pack_hash, idempotency_key, "
        "request_hash, receipt_ref, receipt_hash, created_at) "
        "SELECT 'request-plan', cycle_ref, 'plan', 1, initialization_id, quest_ref, "
        "question_ref, content_ref, content_hash, schema_ref, content_receipt_ref, "
        "content_receipt_hash, question_receipt_ref, question_receipt_hash, "
        "'context-plan', '{\"schema_ref\":\"meta-research/plan-context-pack/v1\"}', "
        "?, 'request-plan-key', ?, 'request-receipt-plan', ?, 22.0 "
        "FROM ae_stage_run_requests WHERE request_ref = 'request-idea'",
        (_hash("plan-context"), _hash("plan-request"), _hash("plan-request-receipt")),
    )


def test_0009_preserves_completed_idea_chain_and_deepens_shared_stage_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade-from-0008.sqlite3"
    _upgrade_to_revision(database, "0008_quest_acquisition_session")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _seed_completed_idea_chain(connection)
        preserved = {
            "request": connection.execute(
                "SELECT request_ref, cycle_ref, stage, context_pack_hash, receipt_hash "
                "FROM ae_stage_run_requests"
            ).fetchone(),
            "run": connection.execute(
                "SELECT run_ref, stage, runtime_binding_hash, completion_receipt_hash "
                "FROM ar_stage_runs"
            ).fetchone(),
            "providers": connection.execute(
                "SELECT invocation_ref, phase, request_hash, response_hash "
                "FROM ar_idea_provider_invocations ORDER BY phase"
            ).fetchall(),
            "commit": connection.execute(
                "SELECT commit_ref, stage, outcome_ref, outcome_kind, receipt_hash "
                "FROM ae_stage_commits"
            ).fetchone(),
        }

    _upgrade_to_revision(database, "0009_plan_stage")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_plan_stage",)
        assert connection.execute(
            "SELECT request_ref, cycle_ref, stage, context_pack_hash, receipt_hash "
            "FROM ae_stage_run_requests WHERE request_ref = 'request-idea'"
        ).fetchone() == preserved["request"]
        assert connection.execute(
            "SELECT run_ref, stage, runtime_binding_hash, completion_receipt_hash "
            "FROM ar_stage_runs WHERE run_ref = 'run-idea'"
        ).fetchone() == preserved["run"]
        assert connection.execute(
            "SELECT invocation_ref, phase, request_hash, response_hash "
            "FROM ar_stage_provider_invocations ORDER BY phase"
        ).fetchall() == preserved["providers"]
        assert connection.execute(
            "SELECT commit_ref, stage, outcome_ref, outcome_kind, receipt_hash "
            "FROM ae_stage_commits WHERE commit_ref = 'commit-idea'"
        ).fetchone() == preserved["commit"]

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "ar_idea_provider_invocations" not in tables
        assert {
            "ar_stage_provider_invocations",
            "rm_plan_documents",
            "rg_formal_plan_decisions",
        } <= tables
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "ix_ar_idea_provider_invocations_status" not in indexes
        assert "ix_ar_stage_provider_invocations_status" in indexes

        _insert_plan_request(connection)
        assert connection.execute(
            "SELECT cycle_ref, stage FROM ae_stage_run_requests "
            "WHERE cycle_ref = 'cycle-idea' ORDER BY stage"
        ).fetchall() == [("cycle-idea", "idea"), ("cycle-idea", "plan")]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ae_stage_run_requests "
                "SELECT 'request-plan-duplicate', cycle_ref, stage, epoch, "
                "initialization_id, quest_ref, question_ref, content_ref, content_hash, "
                "schema_ref, content_receipt_ref, content_receipt_hash, "
                "question_receipt_ref, question_receipt_hash, 'context-plan-duplicate', "
                "context_pack_json, context_pack_hash, 'request-plan-duplicate-key', "
                "request_hash, 'request-plan-duplicate-receipt', receipt_hash, created_at "
                "FROM ae_stage_run_requests WHERE request_ref = 'request-plan'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ae_stage_run_requests SET stage = 'bundle' "
                "WHERE request_ref = 'request-plan'"
            )

        connection.execute(
            "INSERT INTO ar_stage_runs "
            "(run_ref, request_ref, cycle_ref, stage, epoch, context_pack_ref, "
            "context_pack_hash, runtime_binding_json, runtime_binding_hash, "
            "request_receipt_ref, request_receipt_hash, status, current_attempt_ref, "
            "root_session_ref, current_fence_ref, completion_receipt_ref, "
            "completion_receipt_hash, outcome_ref, admission_key, admission_hash, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-plan",
                "request-plan",
                "cycle-idea",
                "plan",
                1,
                "context-plan",
                _hash("plan-context"),
                '{"schema_ref":"meta-research/plan-runtime-binding/v1"}',
                _hash("plan-runtime-binding"),
                "request-receipt-plan",
                _hash("plan-request-receipt"),
                "completed",
                "attempt-plan",
                "session-plan",
                "fence-plan",
                "completion-receipt-plan",
                _hash("plan-completion-receipt"),
                "formal-plan-1",
                "admit-plan-key",
                _hash("plan-admission"),
                23.0,
                24.0,
            ),
        )
        connection.execute(
            "INSERT INTO ae_stage_commits "
            "(commit_ref, request_ref, cycle_ref, stage, epoch, run_ref, outcome_ref, "
            "outcome_kind, disposition, run_completion_receipt_ref, "
            "run_completion_receipt_hash, outcome_receipt_ref, outcome_receipt_hash, "
            "idempotency_key, request_hash, receipt_ref, receipt_hash, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "commit-plan",
                "request-plan",
                "cycle-idea",
                "plan",
                1,
                "run-plan",
                "formal-plan-1",
                "formal_plan",
                "completed",
                "completion-receipt-plan",
                _hash("plan-completion-receipt"),
                "formal-plan-receipt-1",
                _hash("formal-plan-receipt"),
                "commit-plan-key",
                _hash("plan-commit-request"),
                "commit-receipt-plan",
                _hash("plan-commit-receipt"),
                25.0,
            ),
        )
        assert connection.execute(
            "SELECT stage, outcome_kind, disposition FROM ae_stage_commits "
            "WHERE commit_ref = 'commit-plan'"
        ).fetchone() == ("plan", "formal_plan", "completed")

        state_columns = {
            table: {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("research_memory_state", "research_graph_state")
        }
        assert "plan_content_count" in state_columns["research_memory_state"]
        assert {
            "formal_plan_count",
            "plan_rejection_count",
        } <= state_columns["research_graph_state"]
        assert connection.execute(
            "SELECT plan_content_count FROM research_memory_state"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT formal_plan_count, plan_rejection_count FROM research_graph_state"
        ).fetchone() == (0, 0)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0009_plan_owner_tables_bind_all_durable_acceptance_boundaries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "plan-owner-schema.sqlite3"
    _upgrade_to_revision(database, "0009_plan_stage")

    expected_shared_binding = {
        "request_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "submission_ref",
        "question_ref",
        "question_content_ref",
        "question_content_hash",
        "question_content_receipt_ref",
        "question_content_receipt_hash",
        "question_receipt_ref",
        "question_receipt_hash",
        "idea_outcome_ref",
        "idea_outcome_receipt_ref",
        "idea_outcome_receipt_hash",
        "idea_content_ref",
        "idea_content_hash",
        "idea_content_receipt_ref",
        "idea_content_receipt_hash",
        "idea_stage_commit_ref",
        "idea_stage_commit_receipt_ref",
        "idea_stage_commit_receipt_hash",
        "execution_receipt_ref",
        "execution_receipt_hash",
        "plan_document_hash",
        "reviewed_draft_hash",
        "review_hash",
        "payload_hash",
        "receipt_ref",
        "receipt_hash",
    }
    with sqlite3.connect(database) as connection:
        rm_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(rm_plan_documents)")
        }
        rg_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(rg_formal_plan_decisions)"
            )
        }
        assert expected_shared_binding <= rm_columns
        assert expected_shared_binding <= rg_columns
        assert {
            "plan_content_ref",
            "plan_content_receipt_ref",
            "plan_content_receipt_hash",
            "decision",
            "formal_plan_ref",
            "reason_code",
            "feedback_json",
            "feedback_hash",
        } <= rg_columns
        formal_plan_foreign_keys = {
            (row[3], row[2], row[4])
            for row in connection.execute(
                "PRAGMA foreign_key_list(rg_formal_plan_decisions)"
            )
        }
        assert (
            "plan_content_ref",
            "rm_plan_documents",
            "content_ref",
        ) in formal_plan_foreign_keys
        assert (
            "idea_stage_commit_ref",
            "ae_stage_commits",
            "commit_ref",
        ) in formal_plan_foreign_keys
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
