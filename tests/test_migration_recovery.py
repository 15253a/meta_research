from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from sqlalchemy import URL, create_engine, event

from meta_research.migration import (
    _begin_migration_transaction,
    _configure_migration_sqlite,
    upgrade_database,
)
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root


LEGACY_QUESTION = {
    "title": "保留的 legacy Proposal",
    "unknown_statement": "旧问题仍需重新绑定到 v2 draft。",
    "answer_shape": "形成可核验结论。",
    "applicability_scope": "legacy migration",
    "background_context": "升级前已生成。",
    "requirements_constraints": "不得静默变成 current。",
}


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


@pytest.mark.parametrize(
    ("legacy_draft", "with_proposal"),
    [
        (
            {
                "goal": "迁移合法 v1 Quest",
                "completion_criteria": "打开后得到 v2",
                "key_configuration": "保留关键配置",
                "literature_scope": "open_access",
                "initial_question_direction": "保留初始方向",
                "material_receipts": [],
            },
            True,
        ),
        (
            {
                "goal": "迁移另一个合法 v1 Quest",
                "completion_criteria": "无 Proposal 时仍完整保留旧草案",
                "key_configuration": "保留第二份关键配置",
                "literature_scope": "comprehensive",
                "initial_question_direction": "保留第二份初始方向",
                "material_receipts": [],
            },
            False,
        ),
    ],
    ids=("valid-v1-with-proposal", "valid-v1-without-proposal"),
)
def test_active_legacy_v1_is_upgraded_before_runtime_open_resume(
    tmp_path: Path,
    legacy_draft: dict[str, object],
    with_proposal: bool,
) -> None:
    data_root = prepare_data_root(tmp_path / "legacy-open")
    _upgrade_to_revision(data_root.database, "0002_quest_initialization")
    draft_json = json.dumps(
        legacy_draft, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    draft_hash = canonical_hash(legacy_draft)
    proposal_json = json.dumps(
        LEGACY_QUESTION, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    proposal_hash = canonical_hash(LEGACY_QUESTION)
    with sqlite3.connect(data_root.database) as connection:
        connection.execute(
            "INSERT INTO hc_quest_initializations "
            "(initialization_id, status, draft_revision, draft_json, draft_hash, "
            "proposal_revision, proposal_ref, proposal_json, proposal_hash, "
            "proposal_basis_revision, proposal_basis_hash, created_at, updated_at) "
            "VALUES ('quest_init_legacy_open', ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 19.0, 19.0)",
            (
                "proposal_ready" if with_proposal else "draft",
                draft_json,
                draft_hash,
                1 if with_proposal else 0,
                "question_proposal_legacy" if with_proposal else None,
                proposal_json if with_proposal else None,
                proposal_hash if with_proposal else None,
                1 if with_proposal else None,
                draft_hash if with_proposal else None,
            ),
        )
        connection.execute(
            "INSERT INTO hc_quest_draft_revisions "
            "(initialization_id, revision, draft_json, draft_hash, recorded_at) "
            "VALUES ('quest_init_legacy_open', 1, ?, ?, 19.0)",
            (draft_json, draft_hash),
        )
        if with_proposal:
            connection.execute(
                "INSERT INTO hc_question_proposals "
                "(proposal_ref, initialization_id, revision, basis_revision, "
                "basis_hash, content_json, proposal_hash, recorded_at) VALUES "
                "('question_proposal_legacy', 'quest_init_legacy_open', 1, 1, "
                "?, ?, ?, 19.0)",
                (draft_hash, proposal_json, proposal_hash),
            )
        connection.commit()

    runtime = build_production_runtime(data_root)
    try:
        current = runtime.owners.human_collaboration.query_current_quest_creation()
        assert current is not None
        assert current["quest_draft"]["schema_ref"] == (
            "meta-research/quest-initialization-draft/v2"
        )
        assert current["quest_draft"]["revision"] == 2
        upgraded = current["quest_draft"]["value"]
        assert set(upgraded) == {
            "goal",
            "completion_criteria",
            "time_budget",
            "route",
            "resource_envelope_ref",
            "resource_envelope_hash",
            "literature",
            "background_and_initial_direction",
        }
        assert upgraded["goal"] == legacy_draft["goal"]
        assert upgraded["completion_criteria"] == legacy_draft.get(
            "completion_criteria", ""
        )
        assert upgraded["time_budget"] == "open"
        assert upgraded["route"] == "direct"
        assert upgraded["resource_envelope_ref"] is None
        assert upgraded["resource_envelope_hash"] is None
        assert upgraded["literature"]["mode"] == (
            "oa_only"
            if legacy_draft.get("literature_scope") == "open_access"
            else "oa_then_institution"
        )
        if with_proposal:
            assert "保留关键配置" in upgraded["background_and_initial_direction"]
            assert "保留初始方向" in upgraded["background_and_initial_direction"]
        assert current["intent_session"]["status"] == "open"
        if with_proposal:
            assert current["status"] == "proposal_stale"
            assert current["proposal"]["status"] == "stale"

        resumed = runtime.owners.human_collaboration.create_quest(
            {}, "legacy-open-resume"
        )
        assert resumed["initialization_id"] == "quest_init_legacy_open"
        assert resumed["quest_draft"] == current["quest_draft"]
    finally:
        runtime.close()

    with sqlite3.connect(data_root.database) as connection:
        revisions = connection.execute(
            "SELECT revision, draft_json, draft_hash, draft_schema_ref FROM "
            "hc_quest_draft_revisions WHERE initialization_id = "
            "'quest_init_legacy_open' ORDER BY revision"
        ).fetchall()
        pending_intent_count = connection.execute(
            "SELECT pending_intent_count FROM human_collaboration_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
    assert revisions[0] == (
        1,
        draft_json,
        draft_hash,
        "meta-research/quest-initialization-draft/v1",
    )
    assert revisions[1][0] == 2
    assert revisions[1][3] == "meta-research/quest-initialization-draft/v2"
    assert pending_intent_count == (1,)


@pytest.mark.parametrize(
    "corruption",
    (
        "invalid-initialization-json",
        "initialization-hash-mismatch",
        "missing-revision-row",
        "revision-schema-mismatch",
        "revision-value-mismatch",
        "semantic-empty-object",
        "semantic-arbitrary-object",
        "semantic-missing-field",
        "semantic-invalid-field-type",
        "semantic-empty-required-field",
        "semantic-invalid-literature-scope",
        "semantic-material-receipts-not-empty",
    ),
)
def test_corrupt_active_legacy_v1_is_quarantined_before_runtime_upgrade(
    tmp_path: Path,
    corruption: str,
) -> None:
    data_root = prepare_data_root(tmp_path / corruption)
    _upgrade_to_revision(data_root.database, "0002_quest_initialization")
    legacy_draft = {
        "goal": "必须保留的 legacy Quest",
        "completion_criteria": "只在权威 v1 artifact 完整时升级",
        "key_configuration": "保留旧版关键配置",
        "literature_scope": "comprehensive",
        "initial_question_direction": "保留旧版初始方向",
        "material_receipts": [],
    }
    draft_json = json.dumps(
        legacy_draft, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    draft_hash = canonical_hash(legacy_draft)
    with sqlite3.connect(data_root.database) as connection:
        connection.execute(
            "INSERT INTO hc_quest_initializations "
            "(initialization_id, status, draft_revision, draft_json, draft_hash, "
            "proposal_revision, created_at, updated_at) VALUES "
            "('quest_init_corrupt_legacy', 'draft', 1, ?, ?, 0, 19.0, 19.0)",
            (draft_json, draft_hash),
        )
        connection.execute(
            "INSERT INTO hc_quest_draft_revisions "
            "(initialization_id, revision, draft_json, draft_hash, recorded_at) "
            "VALUES ('quest_init_corrupt_legacy', 1, ?, ?, 19.0)",
            (draft_json, draft_hash),
        )
        connection.commit()

    upgrade_database(data_root.database)
    with sqlite3.connect(data_root.database) as connection:
        if corruption == "invalid-initialization-json":
            connection.execute(
                "UPDATE hc_quest_initializations SET draft_json = '{' "
                "WHERE initialization_id = 'quest_init_corrupt_legacy'"
            )
        elif corruption == "initialization-hash-mismatch":
            connection.execute(
                "UPDATE hc_quest_initializations SET draft_hash = ? "
                "WHERE initialization_id = 'quest_init_corrupt_legacy'",
                ("f" * 64,),
            )
        elif corruption == "missing-revision-row":
            connection.execute(
                "DELETE FROM hc_quest_draft_revisions WHERE initialization_id = "
                "'quest_init_corrupt_legacy' AND revision = 1"
            )
        elif corruption == "revision-schema-mismatch":
            connection.execute(
                "UPDATE hc_quest_draft_revisions SET draft_schema_ref = "
                "'meta-research/quest-initialization-draft/v2' WHERE "
                "initialization_id = 'quest_init_corrupt_legacy' AND revision = 1"
            )
        elif corruption == "revision-value-mismatch":
            different_json = json.dumps(
                {**legacy_draft, "goal": "被篡改的 revision value"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                "UPDATE hc_quest_draft_revisions SET draft_json = ? WHERE "
                "initialization_id = 'quest_init_corrupt_legacy' AND revision = 1",
                (different_json,),
            )
        elif corruption.startswith("semantic-"):
            semantic_drafts: dict[str, dict[str, object]] = {
                "semantic-empty-object": {},
                "semantic-arbitrary-object": {"unexpected": "not a v1 draft"},
                "semantic-missing-field": {
                    key: value
                    for key, value in legacy_draft.items()
                    if key != "key_configuration"
                },
                "semantic-invalid-field-type": {**legacy_draft, "goal": 7},
                "semantic-empty-required-field": {**legacy_draft, "goal": "  "},
                "semantic-invalid-literature-scope": {
                    **legacy_draft,
                    "literature_scope": "internet",
                },
                "semantic-material-receipts-not-empty": {
                    **legacy_draft,
                    "material_receipts": ["unaccepted-memory-ref"],
                },
            }
            semantic_draft = semantic_drafts[corruption]
            semantic_json = json.dumps(
                semantic_draft,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            semantic_hash = canonical_hash(semantic_draft)
            connection.execute(
                "UPDATE hc_quest_initializations SET draft_json = ?, draft_hash = ? "
                "WHERE initialization_id = 'quest_init_corrupt_legacy'",
                (semantic_json, semantic_hash),
            )
            connection.execute(
                "UPDATE hc_quest_draft_revisions SET draft_json = ?, draft_hash = ? "
                "WHERE initialization_id = 'quest_init_corrupt_legacy' AND revision = 1",
                (semantic_json, semantic_hash),
            )
        else:
            raise AssertionError(f"unknown corruption fixture: {corruption}")
        connection.commit()
        quarantined_initialization = connection.execute(
            "SELECT draft_revision, draft_json, draft_hash, draft_schema_ref FROM "
            "hc_quest_initializations WHERE initialization_id = "
            "'quest_init_corrupt_legacy'"
        ).fetchone()
        quarantined_revisions = connection.execute(
            "SELECT revision, draft_json, draft_hash, draft_schema_ref FROM "
            "hc_quest_draft_revisions WHERE initialization_id = "
            "'quest_init_corrupt_legacy' ORDER BY revision"
        ).fetchall()

    with pytest.raises(OwnerConflict) as raised:
        build_production_runtime(data_root)
    assert raised.value.code == "legacy_quest_draft_artifact_invalid"

    with sqlite3.connect(data_root.database) as connection:
        initialization = connection.execute(
            "SELECT draft_revision, draft_json, draft_hash, draft_schema_ref FROM "
            "hc_quest_initializations WHERE initialization_id = "
            "'quest_init_corrupt_legacy'"
        ).fetchone()
        revisions = connection.execute(
            "SELECT revision, draft_json, draft_hash, draft_schema_ref FROM "
            "hc_quest_draft_revisions WHERE initialization_id = "
            "'quest_init_corrupt_legacy' ORDER BY revision"
        ).fetchall()
        appended_revision_count = connection.execute(
            "SELECT count(*) FROM hc_quest_draft_revisions WHERE initialization_id = "
            "'quest_init_corrupt_legacy' AND revision > 1"
        ).fetchone()
        session_count = connection.execute(
            "SELECT count(*) FROM hc_intent_drafting_sessions WHERE initialization_id = "
            "'quest_init_corrupt_legacy'"
        ).fetchone()
        upgrade_event_count = connection.execute(
            "SELECT count(*) FROM durable_feed WHERE event_type = "
            "'human_collaboration.quest_draft_revised'"
        ).fetchone()
    assert initialization == quarantined_initialization
    assert revisions == quarantined_revisions
    assert appended_revision_count == (0,)
    assert session_count == (0,)
    assert upgrade_event_count == (0,)


def test_interrupted_sqlite_ddl_rolls_back_and_upgrade_can_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted.sqlite3"
    original_create_table = Operations.create_table
    failed_once = False

    def fail_during_quest_migration(self, table_name, *args, **kwargs):
        nonlocal failed_once
        if table_name == "hc_quest_initializations" and not failed_once:
            failed_once = True
            raise OSError("injected migration interruption")
        return original_create_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_during_quest_migration)
    with pytest.raises(OSError, match="injected migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        tables_after_failure = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "research_memory_state" in tables_after_failure:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(research_memory_state)")
            }
            assert "formal_content_count" not in columns
        assert "hc_quest_initializations" not in tables_after_failure

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_memory_state)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == ("0009_experiment_measurement",)
    assert "formal_content_count" in columns
    assert "hc_quest_initializations" in tables
    assert "hc_proposal_generation_attempts" in tables


def test_interrupted_0003_ddl_rolls_back_the_whole_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-0003.sqlite3"
    _upgrade_to_revision(database, "0002_quest_initialization")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO durable_feed "
            "(revision, event_type, payload_json, recorded_at) "
            "VALUES (1, 'before.0003', '{}', 23.0)"
        )
        connection.commit()

    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_middle_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "hc_proposal_generation_attempts" and not failed_once:
            failed_once = True
            raise OSError("injected 0003 migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_middle_table)
    with pytest.raises(OSError, match="injected 0003 migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version_after_failure = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        initialization_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_quest_initializations)"
            )
        }
        tables_after_failure = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        preserved = connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone()

    assert version_after_failure == ("0002_quest_initialization",)
    assert "draft_schema_ref" not in initialization_columns
    assert "ar_host_capability_snapshots" not in tables_after_failure
    assert "hc_proposal_generation_attempts" not in tables_after_failure
    assert preserved == ("before.0003", "{}", 23.0)

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)


def test_process_exit_mid_0003_ddl_recovers_on_the_next_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "process-exit-0003.sqlite3"
    _upgrade_to_revision(database, "0002_quest_initialization")
    child = """
import os
import sys
from pathlib import Path
from alembic.operations import Operations
from meta_research.migration import upgrade_database

original_create_table = Operations.create_table

def exit_after_middle_table(self, table_name, *args, **kwargs):
    created = original_create_table(self, table_name, *args, **kwargs)
    if table_name == "hc_proposal_generation_attempts":
        os._exit(91)
    return created

Operations.create_table = exit_after_middle_table
upgrade_database(Path(sys.argv[1]))
"""

    interrupted = subprocess.run(
        [sys.executable, "-c", child, str(database)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert interrupted.returncode == 91, interrupted.stderr

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0002_quest_initialization",)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_quest_initializations)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "draft_schema_ref" not in columns
    assert "hc_proposal_generation_attempts" not in tables

    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_forward_only_0003_preserves_existing_data_and_is_repeatable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade-from-0002.sqlite3"
    _upgrade_to_revision(database, "0002_quest_initialization")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO durable_feed "
            "(revision, event_type, payload_json, recorded_at) "
            "VALUES (1, 'legacy.event', '{\"kept\":true}', 17.0)"
        )
        connection.execute(
            "INSERT INTO auth_sessions "
            "(session_hash, csrf_hash, created_at, expires_at, revoked_at) "
            "VALUES (?, ?, 18.0, 1800.0, NULL)",
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            "INSERT INTO hc_quest_initializations "
            "(initialization_id, status, draft_revision, draft_json, draft_hash, "
            "proposal_revision, created_at, updated_at) "
            "VALUES ('quest_init_legacy', 'draft', 1, ?, ?, 0, 19.0, 19.0)",
            ('{"goal":"legacy"}', "c" * 64),
        )
        connection.execute(
            "INSERT INTO hc_quest_draft_revisions "
            "(initialization_id, revision, draft_json, draft_hash, recorded_at) "
            "VALUES ('quest_init_legacy', 1, ?, ?, 19.0)",
            ('{"goal":"legacy"}', "c" * 64),
        )
        connection.execute(
            "INSERT INTO hc_quest_initializations "
            "(initialization_id, status, draft_revision, draft_json, draft_hash, "
            "proposal_revision, proposal_ref, proposal_json, proposal_hash, "
            "proposal_basis_revision, proposal_basis_hash, created_at, updated_at) "
            "VALUES ('quest_init_legacy_proposal', 'cancelled', 1, ?, ?, 1, "
            "'question_proposal_legacy', ?, ?, 1, ?, 20.0, 20.0)",
            (
                '{"goal":"legacy proposal"}',
                "d" * 64,
                '{"title":"kept"}',
                "e" * 64,
                "d" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO hc_quest_draft_revisions "
            "(initialization_id, revision, draft_json, draft_hash, recorded_at) "
            "VALUES ('quest_init_legacy_proposal', 1, ?, ?, 20.0)",
            ('{"goal":"legacy proposal"}', "d" * 64),
        )
        connection.execute(
            "INSERT INTO hc_question_proposals "
            "(proposal_ref, initialization_id, revision, basis_revision, basis_hash, "
            "content_json, proposal_hash, recorded_at) VALUES "
            "('question_proposal_legacy', 'quest_init_legacy_proposal', 1, 1, ?, "
            "?, ?, 20.0)",
            ("d" * 64, '{"title":"kept"}', "e" * 64),
        )
        connection.commit()

    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        feed = connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone()
        auth = connection.execute(
            "SELECT session_hash, csrf_hash, created_at, expires_at, revoked_at "
            "FROM auth_sessions WHERE session_hash = ?",
            ("a" * 64,),
        ).fetchone()
        initialization = connection.execute(
            "SELECT status, draft_json, draft_hash, draft_schema_ref "
            "FROM hc_quest_initializations "
            "WHERE initialization_id = 'quest_init_legacy'"
        ).fetchone()
        revision = connection.execute(
            "SELECT draft_json, draft_hash, draft_schema_ref "
            "FROM hc_quest_draft_revisions "
            "WHERE initialization_id = 'quest_init_legacy' AND revision = 1"
        ).fetchone()
        proposal = connection.execute(
            "SELECT content_json, proposal_hash, schema_ref "
            "FROM hc_question_proposals "
            "WHERE proposal_ref = 'question_proposal_legacy'"
        ).fetchone()
        new_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        new_indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_quest_initializations "
                "(initialization_id, status, draft_revision, draft_json, draft_hash, "
                "proposal_revision, created_at, updated_at) "
                "VALUES ('quest_init_second_active', 'draft', 1, '{}', ?, 0, 20.0, 20.0)",
                ("d" * 64,),
            )

    assert version == ("0009_experiment_measurement",)
    assert feed == ("legacy.event", '{"kept":true}', 17.0)
    assert auth == ("a" * 64, "b" * 64, 18.0, 1800.0, None)
    assert initialization == (
        "draft",
        '{"goal":"legacy"}',
        "c" * 64,
        "meta-research/quest-initialization-draft/v1",
    )
    assert revision == (
        '{"goal":"legacy"}',
        "c" * 64,
        "meta-research/quest-initialization-draft/v1",
    )
    assert proposal == (
        '{"title":"kept"}',
        "e" * 64,
        "meta-research/question-proposal/v1",
    )
    assert {
        "ar_host_capability_snapshots",
        "hc_resource_envelopes",
        "hc_intent_drafting_sessions",
        "hc_intent_drafting_turns",
        "hc_proposal_generation_attempts",
        "hc_confirmation_preview_bindings",
        "hc_reconciliation_checkpoints",
        "hc_reconciliation_attempts",
        "ae_stage_run_requests",
        "ar_stage_runs",
        "ar_stage_sessions",
        "ar_stage_attempts",
        "ar_execution_fences",
        "ar_idea_provider_invocations",
        "rm_idea_outcome_contents",
        "rg_idea_outcome_decisions",
        "ae_stage_commits",
    } <= new_tables
    assert "ix_durable_feed_event_type_revision" in new_indexes
    assert "uq_ar_stage_sessions_native_session_ref" in new_indexes
    assert "ix_ar_idea_provider_invocations_status" in new_indexes
    assert foreign_key_errors == []
    assert quick_check == ("ok",)


def test_interrupted_0004_rolls_back_owner_counters_and_idea_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-0004.sqlite3"
    _upgrade_to_revision(database, "0003_quest_direct_web")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_runtime_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_stage_sessions" and not failed_once:
            failed_once = True
            raise OSError("injected 0004 migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_runtime_table)
    with pytest.raises(OSError, match="injected 0004 migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0003_quest_direct_web",)
        ae_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(advancement_engine_state)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "stage_request_count" not in ae_columns
    assert "ae_stage_run_requests" not in tables
    assert "ar_stage_runs" not in tables
    assert "ar_stage_sessions" not in tables

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_interrupted_0005_rolls_back_and_converges_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-0005.sqlite3"
    _upgrade_to_revision(database, "0004_idea_stage")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_intake_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "rm_asset_intakes" and not failed_once:
            failed_once = True
            raise OSError("injected 0005 migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_intake_table)
    with pytest.raises(OSError, match="injected 0005 migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0004_idea_stage",)
        rm_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(research_memory_state)"
            )
        }
        rg_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(research_graph_state)"
            )
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "asset_version_count" not in rm_columns
    assert "asset_role_count" not in rg_columns
    assert "rm_assets" not in tables
    assert "rm_asset_versions" not in tables
    assert "rm_asset_intakes" not in tables

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0005_backfills_existing_rm_contents_without_changing_identity_or_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade-from-0004.sqlite3"
    _upgrade_to_revision(database, "0004_idea_stage")
    formal_value = {"schema_ref": "legacy-question", "title": "kept exactly"}
    formal_json = json.dumps(
        formal_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    formal_hash = canonical_hash(formal_value)
    idea_value = {
        "schema_ref": "legacy-idea",
        "outcome": {"kind": "no_viable_candidate"},
    }
    idea_json = json.dumps(
        idea_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    idea_hash = canonical_hash(idea_value)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO rm_formal_question_contents "
            "(content_ref, initialization_id, quest_ref, quest_receipt_ref, "
            "quest_receipt_hash, proposal_ref, proposal_hash, confirmation_ref, "
            "confirmation_hash, content_hash, schema_ref, content_json, "
            "object_path, receipt_ref, receipt_hash, accepted_at) VALUES "
            "('question_content_kept', 'quest_init_kept', 'quest_kept', "
            "'quest_receipt_kept', ?, 'proposal_kept', ?, 'confirmation_kept', "
            "?, ?, 'meta-research/formal-question-content/v1', ?, "
            "'formal-question-content/aa/kept.json', "
            "'rm_question_receipt_kept', ?, 41.0)",
            (
                "1" * 64,
                "2" * 64,
                "3" * 64,
                formal_hash,
                formal_json,
                "4" * 64,
            ),
        )
        # Foreign-key parents are irrelevant to this lossless RM transform.  The
        # legacy row itself is the migration input under test.
        connection.execute(
            "INSERT INTO rm_idea_outcome_contents "
            "(content_ref, request_ref, run_ref, attempt_ref, fence_ref, "
            "submission_ref, outcome_kind, outcome_json, outcome_hash, "
            "reviewed_draft_json, reviewed_draft_hash, review_json, review_hash, "
            "payload_json, payload_hash, object_path, execution_receipt_ref, "
            "execution_receipt_hash, receipt_ref, receipt_hash, accepted_at) "
            "VALUES ('idea_content_kept', 'request_kept', 'run_kept', "
            "'attempt_kept', 'fence_kept', 'submission_kept', "
            "'no_viable_candidate', '{}', ?, '{}', ?, '{}', ?, ?, ?, "
            "'idea-outcome-content/bb/kept.json', 'execution_receipt_kept', ?, "
            "'rm_idea_receipt_kept', ?, 42.0)",
            (
                "5" * 64,
                "6" * 64,
                "7" * 64,
                idea_json,
                idea_hash,
                "8" * 64,
                "9" * 64,
            ),
        )
        connection.commit()

    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        original_formal = connection.execute(
            "SELECT content_ref, content_hash, object_path, receipt_ref, "
            "receipt_hash FROM rm_formal_question_contents"
        ).fetchone()
        original_idea = connection.execute(
            "SELECT content_ref, payload_hash, object_path, receipt_ref, "
            "receipt_hash FROM rm_idea_outcome_contents"
        ).fetchone()
        versions = connection.execute(
            "SELECT version_ref, asset_ref, version_number, content_hash, "
            "acceptance_kind, receipt_ref, receipt_hash, manifest_json "
            "FROM rm_asset_versions ORDER BY accepted_at"
        ).fetchall()
        custodies = connection.execute(
            "SELECT version_ref, custody_mode, receipt_ref, receipt_hash "
            "FROM rm_asset_custodies ORDER BY version_ref"
        ).fetchall()
        counters = connection.execute(
            "SELECT asset_count, asset_version_count, object_count "
            "FROM research_memory_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    assert original_formal == (
        "question_content_kept",
        formal_hash,
        "formal-question-content/aa/kept.json",
        "rm_question_receipt_kept",
        "4" * 64,
    )
    assert original_idea == (
        "idea_content_kept",
        idea_hash,
        "idea-outcome-content/bb/kept.json",
        "rm_idea_receipt_kept",
        "9" * 64,
    )
    assert [row[:7] for row in versions] == [
        (
            "question_content_kept",
            "question_content_kept",
            1,
            formal_hash,
            "question_content_acceptance",
            "rm_question_receipt_kept",
            "4" * 64,
        ),
        (
            "idea_content_kept",
            "idea_content_kept",
            1,
            idea_hash,
            "idea_outcome_content_acceptance",
            "rm_idea_receipt_kept",
            "9" * 64,
        ),
    ]
    assert json.loads(versions[0][7])["entries"][0]["object_path"] == (
        "formal-question-content/aa/kept.json"
    )
    assert json.loads(versions[1][7])["entries"][0]["object_path"] == (
        "idea-outcome-content/bb/kept.json"
    )
    assert custodies == [
        (
            "idea_content_kept",
            "managed",
            "rm_idea_receipt_kept",
            "9" * 64,
        ),
        (
            "question_content_kept",
            "managed",
            "rm_question_receipt_kept",
            "4" * 64,
        ),
    ]
    assert counters == (2, 2, 2)


def test_0006_upgrades_an_existing_0005_database_and_backfills_managed_registry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade-from-existing-0005.sqlite3"
    _upgrade_to_revision(database, "0005_research_assets")
    payload = b"accepted before migration 0006\n"
    digest = hashlib.sha256(payload).hexdigest()
    object_path = f"assets/{digest[:2]}/{digest}"
    manifest = {
        "schema_ref": "meta-research/asset-manifest/v1",
        "kind": "file",
        "entries": [
            {
                "path": "pre-0006.txt",
                "sha256": digest,
                "size": len(payload),
                "object_path": object_path,
            }
        ],
    }
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    inline_request = {
        "asset_ref": None,
        "asynchronous": True,
        "content_base64": "YmlnLXBheWxvYWQ=",
        "custody_mode": "managed",
        "display_name": "payload.txt",
        "media_type": "text/plain",
        "provenance": {},
        "source_kind": "file",
        "source_locator": None,
    }
    inline_request_json = json.dumps(
        inline_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    locator_request = {
        **inline_request,
        "content_base64": None,
        "custody_mode": "linked_local",
        "display_name": "linked.txt",
        "source_kind": "local_path",
        "source_locator": "/absolute/legacy/source.txt",
    }
    locator_request_json = json.dumps(
        locator_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0005_research_assets",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'rm_managed_objects'"
        ).fetchone() is None
        connection.execute(
            "INSERT INTO rm_assets (asset_ref, created_at) VALUES "
            "('asset_pre_0006', 51.0)"
        )
        connection.execute(
            "INSERT INTO rm_asset_versions (version_ref, asset_ref, "
            "version_number, source_kind, display_name, media_type, content_hash, "
            "manifest_json, manifest_hash, byte_count, provenance_json, "
            "provenance_hash, acceptance_kind, receipt_ref, receipt_hash, "
            "accepted_at) VALUES ('asset_version_pre_0006', 'asset_pre_0006', 1, "
            "'text', 'pre-0006.txt', 'text/plain', ?, ?, ?, ?, '{}', ?, "
            "'asset_acceptance', 'receipt_pre_0006', ?, 51.0)",
            (
                digest,
                manifest_json,
                canonical_hash(manifest),
                len(payload),
                canonical_hash({}),
                "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO rm_asset_custodies (custody_ref, version_ref, "
            "custody_mode, source_locator, receipt_kind, receipt_ref, "
            "receipt_hash, established_at) VALUES ('custody_pre_0006', "
            "'asset_version_pre_0006', 'managed', NULL, 'asset_acceptance', "
            "'receipt_pre_0006', ?, 51.0)",
            ("a" * 64,),
        )
        for job_ref, request_json in (
            ("job_legacy_inline", inline_request_json),
            ("job_legacy_locator", locator_request_json),
        ):
            connection.execute(
                "INSERT INTO rm_asset_intakes (job_ref, idempotency_key, "
                "request_json, request_hash, status, failure_code, attempt_count, "
                "completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, "
                "'failed', 'legacy_failure', 1, 51.0, 50.0, 51.0)",
                (
                    job_ref,
                    f"key_{job_ref}",
                    request_json,
                    hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                ),
            )
        connection.commit()

    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        assert connection.execute(
            "SELECT object_path, content_hash, byte_count FROM "
            "rm_managed_objects"
        ).fetchone() == (object_path, digest, len(payload))
        assert connection.execute(
            "SELECT object_count FROM research_memory_state WHERE singleton = "
            "'owner'"
        ).fetchone() == (1,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "rm_asset_repair_commands",
            "rm_asset_verification_observations",
            "rm_asset_verification_state",
            "rg_asset_role_commands",
        } <= tables
        assert connection.execute(
            "SELECT integrity, availability, next_verify_at FROM "
            "rm_asset_verification_observations WHERE version_ref = "
            "'asset_version_pre_0006'"
        ).fetchone() == ("unknown", "unknown", 0.0)
        inline_stored = connection.execute(
            "SELECT request_json, request_source_kind, request_custody_mode, "
            "request_payload_scrubbed FROM rm_asset_intakes WHERE job_ref = "
            "'job_legacy_inline'"
        ).fetchone()
        assert inline_stored == (
            '{"custody_mode":"managed","payload_scrubbed":true,'
            '"source_kind":"file"}',
            "file",
            "managed",
            1,
        )
        locator_stored = connection.execute(
            "SELECT request_json, request_payload_scrubbed FROM "
            "rm_asset_intakes WHERE job_ref = 'job_legacy_locator'"
        ).fetchone()
        assert locator_stored == (
            '{"custody_mode":"linked_local","payload_scrubbed":true,'
            '"source_kind":"local_path"}',
            1,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0006_preserves_0005_linked_and_nonportable_asset_facts_fail_closed(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "legacy-asset-compatibility")
    _upgrade_to_revision(data_root.database, "0005_research_assets")
    linked_source = tmp_path / "legacy-linked.txt"
    linked_payload = b"linked bytes accepted by the 0005 contract\n"
    linked_source.write_bytes(linked_payload)
    managed_payload = b"managed bytes with a nonportable historical name\n"

    def accepted_asset_values(
        *,
        suffix: str,
        payload: bytes,
        custody_mode: str,
        path: str,
        object_path: str | None,
        source_locator: str | None,
    ) -> dict[str, object]:
        asset_ref = f"asset_legacy_{suffix}"
        version_ref = f"asset_version_legacy_{suffix}"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "schema_ref": "meta-research/asset-manifest/v1",
            "kind": "file",
            "entries": [
                {
                    "path": path,
                    "sha256": digest,
                    "size": len(payload),
                    "object_path": object_path,
                }
            ],
        }
        provenance = {"source_kind": "local_path"}
        manifest_json = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        provenance_json = json.dumps(
            provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        bindings = {
            "asset_ref": asset_ref,
            "version_number": 1,
            "source_kind": "local_path",
            "display_name": path,
            "media_type": "text/plain",
            "content_hash": digest,
            "manifest_hash": canonical_hash(manifest),
            "byte_count": len(payload),
            "provenance_hash": canonical_hash(provenance),
            "custody_modes": [custody_mode],
        }
        receipt_hash = canonical_hash(
            {
                "schema_ref": "meta-research/owner-acceptance-receipt/v1",
                "issuer": "research_memory",
                "kind": "asset_acceptance",
                "subject_ref": version_ref,
                "bindings": bindings,
            }
        )
        return {
            **bindings,
            "version_ref": version_ref,
            "manifest_json": manifest_json,
            "provenance_json": provenance_json,
            "receipt_ref": f"receipt_legacy_{suffix}",
            "receipt_hash": receipt_hash,
            "custody_ref": f"custody_legacy_{suffix}",
            "custody_mode": custody_mode,
            "source_locator": source_locator,
        }

    linked = accepted_asset_values(
        suffix="linked",
        payload=linked_payload,
        custody_mode="linked_local",
        path=linked_source.name,
        object_path=None,
        source_locator=str(linked_source.resolve()),
    )
    managed_object_path = (
        f"assets/{hashlib.sha256(managed_payload).hexdigest()[:2]}/"
        f"{hashlib.sha256(managed_payload).hexdigest()}"
    )
    managed = accepted_asset_values(
        suffix="managed",
        payload=managed_payload,
        custody_mode="managed",
        path="C:poison.txt",
        object_path=managed_object_path,
        source_locator=None,
    )
    managed_path = data_root.objects / managed_object_path
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    managed_path.write_bytes(managed_payload)
    with sqlite3.connect(data_root.database) as connection:
        for accepted in (linked, managed):
            connection.execute(
                "INSERT INTO rm_assets (asset_ref, created_at) VALUES (?, 51.0)",
                (accepted["asset_ref"],),
            )
            connection.execute(
                "INSERT INTO rm_asset_versions (version_ref, asset_ref, "
                "version_number, source_kind, display_name, media_type, "
                "content_hash, manifest_json, manifest_hash, byte_count, "
                "provenance_json, provenance_hash, acceptance_kind, receipt_ref, "
                "receipt_hash, accepted_at) VALUES (?, ?, 1, 'local_path', ?, "
                "'text/plain', ?, ?, ?, ?, ?, ?, 'asset_acceptance', ?, ?, 51.0)",
                (
                    accepted["version_ref"],
                    accepted["asset_ref"],
                    accepted["display_name"],
                    accepted["content_hash"],
                    accepted["manifest_json"],
                    accepted["manifest_hash"],
                    accepted["byte_count"],
                    accepted["provenance_json"],
                    accepted["provenance_hash"],
                    accepted["receipt_ref"],
                    accepted["receipt_hash"],
                ),
            )
            connection.execute(
                "INSERT INTO rm_asset_custodies (custody_ref, version_ref, "
                "custody_mode, source_locator, receipt_kind, receipt_ref, "
                "receipt_hash, established_at) VALUES (?, ?, ?, ?, "
                "'asset_acceptance', ?, ?, 51.0)",
                (
                    accepted["custody_ref"],
                    accepted["version_ref"],
                    accepted["custody_mode"],
                    accepted["source_locator"],
                    accepted["receipt_ref"],
                    accepted["receipt_hash"],
                ),
            )
        legacy_linked_request = {
            "asset_ref": None,
            "asynchronous": False,
            "content_base64": None,
            "custody_mode": "linked_local",
            "display_name": linked_source.name,
            "media_type": "text/plain",
            "provenance": {},
            "source_kind": "local_path",
            "source_locator": str(linked_source.resolve()),
        }
        legacy_linked_request_json = json.dumps(
            legacy_linked_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO rm_asset_intakes (job_ref, idempotency_key, "
            "request_json, request_hash, status, asset_ref, version_ref, "
            "attempt_count, completed_at, created_at, updated_at) VALUES "
            "('job_legacy_linked', 'key_legacy_linked', ?, ?, 'accepted', ?, ?, "
            "1, 51.0, 50.0, 51.0)",
            (
                legacy_linked_request_json,
                hashlib.sha256(
                    legacy_linked_request_json.encode("utf-8")
                ).hexdigest(),
                linked["asset_ref"],
                linked["version_ref"],
            ),
        )
        connection.execute(
            "UPDATE research_memory_state SET asset_count = 2, "
            "asset_version_count = 2 WHERE singleton = 'owner'"
        )
        connection.commit()

    upgrade_database(data_root.database)
    runtime = build_production_runtime(data_root)
    try:
        projected = runtime.projection.query_snapshot()["research_assets"]
        assert {item["memory_ref"] for item in projected["items"]} == {
            linked["version_ref"],
            managed["version_ref"],
        }
        assert all(item["verification_pending"] for item in projected["items"])
        inventory = {
            item.memory_ref: item
            for item in runtime.owners.research_memory.query_asset_inventory()
        }
        assert (
            inventory[str(linked["version_ref"])].integrity,
            inventory[str(linked["version_ref"])].availability,
        ) == ("verified", "available")
        assert (
            inventory[str(managed["version_ref"])].integrity,
            inventory[str(managed["version_ref"])].availability,
        ) == ("verified", "available")
        linked_custody = runtime.owners.research_memory.query_asset_custodies(
            str(linked["version_ref"])
        )[0]
        assert linked_custody.source_locator == str(linked_source.resolve())
        assert linked_custody.locator_receipted is True
        assert linked_custody.receipt.kind == "asset_acceptance"
        assert linked_custody.established_at == 51.0
        assert linked_custody.locator_receipt is not None
        assert linked_custody.locator_receipt.kind == (
            "asset_custody_locator_migrated"
        )
        assert linked_custody.locator_bound_at is not None
        assert linked_custody.locator_bound_at > linked_custody.established_at
        assert runtime.owners.research_memory.materialize_asset(
            str(linked["version_ref"])
        ).content == linked_payload
        with pytest.raises(
            OwnerConflict, match="asset_materialization_unsupported"
        ):
            runtime.owners.research_memory.materialize_asset(
                str(managed["version_ref"])
            )
    finally:
        runtime.close()


def test_interrupted_0007_rolls_back_typed_runs_and_snapshot_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-0007.sqlite3"
    _upgrade_to_revision(database, "0006_research_asset_recovery")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO durable_feed (revision, event_type, payload_json, "
            "recorded_at) VALUES (1, 'before.0007', '{}', 71.0)"
        )
        connection.commit()

    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_session_table(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_deepfetch_sessions" and not failed_once:
            failed_once = True
            raise OSError("injected 0007 migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_session_table)
    with pytest.raises(OSError, match="injected 0007 migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0006_research_asset_recovery",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        ar_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_runtime_state)")
        }
        proposal_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(hc_question_proposals)")
        }
        preview_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_confirmation_preview_bindings)"
            )
        }
        preserved = connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone()
    assert "hc_deepfetch_requests" not in tables
    assert "ar_deepfetch_sessions" not in tables
    assert "deepfetch_run_count" not in ar_columns
    assert "literature_snapshot_ref" not in proposal_columns
    assert "literature_snapshot_hash" not in preview_columns
    assert preserved == ("before.0007", "{}", 71.0)

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0009_experiment_measurement",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        ar_state = connection.execute(
            "SELECT deepfetch_run_count, deepfetch_completed_run_count, "
            "deepfetch_attempt_count, deepfetch_session_count FROM "
            "agent_runtime_state WHERE singleton = 'owner'"
        ).fetchone()
        rm_state = connection.execute(
            "SELECT literature_snapshot_count FROM research_memory_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert {
        "hc_deepfetch_requests",
        "ar_deepfetch_runs",
        "ar_deepfetch_sessions",
        "ar_deepfetch_attempts",
        "rm_literature_snapshots",
    }.issubset(tables)
    assert ar_state == (0, 0, 0, 0)
    assert rm_state == (0,)
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_interrupted_0008_rolls_back_acquisition_sessions_and_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "interrupted-0008.sqlite3"
    _upgrade_to_revision(database, "0007_first_question_deepfetch")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO durable_feed (revision, event_type, payload_json, "
            "recorded_at) VALUES (1, 'before.0008', '{}', 81.0)"
        )
        connection.commit()

    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_acquisition_session(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_acquisition_sessions" and not failed_once:
            failed_once = True
            raise OSError("injected 0008 migration interruption")
        return created

    monkeypatch.setattr(
        Operations,
        "create_table",
        fail_after_acquisition_session,
    )
    with pytest.raises(OSError, match="injected 0008 migration interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        runtime_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(agent_runtime_state)"
            )
        }
        request_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_deepfetch_requests)"
            )
        }
        preserved = connection.execute(
            "SELECT event_type, payload_json, recorded_at FROM durable_feed "
            "WHERE revision = 1"
        ).fetchone()
    assert version == ("0007_first_question_deepfetch",)
    assert "ar_acquisition_sessions" not in tables
    assert "ar_acquisition_requests" not in tables
    assert "acquisition_session_count" not in runtime_columns
    assert "acquisition_session_ref" not in request_columns
    assert preserved == ("before.0008", "{}", 81.0)

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        runtime_state = connection.execute(
            "SELECT acquisition_session_count, acquisition_request_count, "
            "acquisition_active_slot_count FROM agent_runtime_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        request_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_deepfetch_requests)"
            )
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert version == ("0009_experiment_measurement",)
    assert {
        "ar_acquisition_sessions",
        "ar_acquisition_requests",
    }.issubset(tables)
    assert {
        "acquisition_session_ref",
        "acquisition_config_hash",
        "acquisition_runtime_binding_hash",
    }.issubset(request_columns)
    assert runtime_state == (0, 0, 0)
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_0003_workflow_tables_enforce_lineage_and_paired_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "0003-constraints.sqlite3"
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO hc_quest_initializations "
            "(initialization_id, status, draft_revision, draft_json, draft_hash, "
            "proposal_revision, created_at, updated_at) "
            "VALUES ('quest_init_constraints', 'draft', 1, '{}', ?, 0, 30.0, 30.0)",
            ("1" * 64,),
        )
        connection.execute(
            "INSERT INTO hc_quest_draft_revisions "
            "(initialization_id, revision, draft_json, draft_hash, recorded_at) "
            "VALUES ('quest_init_constraints', 1, '{}', ?, 30.0)",
            ("1" * 64,),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ar_host_capability_snapshots "
                "(snapshot_ref, idempotency_key, request_hash, adapter_kind, "
                "status, capabilities_json, capabilities_hash, reason_code, "
                "observed_at) VALUES ('host_invalid', 'invalid-observation', ?, "
                "'system_probe', 'ready', '{}', ?, "
                "'must_be_null_when_ready', 30.0)",
                ("3" * 64, "2" * 64),
            )
        connection.execute(
            "INSERT INTO ar_host_capability_snapshots "
            "(snapshot_ref, idempotency_key, request_hash, adapter_kind, status, "
            "capabilities_json, capabilities_hash, reason_code, observed_at) "
            "VALUES ('host_current', 'current-observation', ?, 'system_probe', "
            "'ready', '{}', ?, NULL, 30.0)",
            ("3" * 64, "2" * 64),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_resource_envelopes "
                "(envelope_ref, initialization_id, draft_revision, draft_hash, "
                "host_snapshot_ref, host_snapshot_hash, envelope_json, "
                "envelope_hash, recorded_at) VALUES "
                "('envelope_wrong_revision', 'quest_init_constraints', 2, ?, "
                "'host_current', ?, '{}', ?, 31.0)",
                ("1" * 64, "2" * 64, "3" * 64),
            )
        connection.execute(
            "INSERT INTO hc_resource_envelopes "
            "(envelope_ref, initialization_id, draft_revision, draft_hash, "
            "host_snapshot_ref, host_snapshot_hash, envelope_json, envelope_hash, "
            "recorded_at) VALUES "
            "('envelope_current', 'quest_init_constraints', 1, ?, "
            "'host_current', ?, '{}', ?, 31.0)",
            ("1" * 64, "2" * 64, "3" * 64),
        )
        connection.execute(
            "INSERT INTO hc_intent_drafting_sessions "
            "(session_ref, initialization_id, status, created_at, updated_at) "
            "VALUES ('intent_session_current', 'quest_init_constraints', "
            "'open', 31.0, 31.0)"
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_intent_drafting_turns "
                "(turn_ref, session_ref, ordinal, idempotency_key, request_hash, "
                "basis_revision, basis_hash, user_content, user_content_hash, "
                "assistant_status, created_at) VALUES "
                "('turn_incomplete', 'intent_session_current', 1, 'turn-key-1', ?, "
                "1, ?, 'question', ?, 'completed', 32.0)",
                ("4" * 64, "1" * 64, "5" * 64),
            )
        connection.execute(
            "INSERT INTO hc_intent_drafting_turns "
            "(turn_ref, session_ref, ordinal, idempotency_key, request_hash, "
            "basis_revision, basis_hash, user_content, user_content_hash, "
            "assistant_status, created_at) VALUES "
            "('turn_queued', 'intent_session_current', 1, 'turn-key-2', ?, "
            "1, ?, 'question', ?, 'queued', 32.0)",
            ("4" * 64, "1" * 64, "5" * 64),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_proposal_generation_attempts "
                "(generation_ref, initialization_id, idempotency_key, request_hash, "
                "route, basis_revision, basis_hash, status, adapter_kind, "
                "attempt_count, created_at, started_at, completed_at) VALUES "
                "('generation_invalid', 'quest_init_constraints', 'generation-key', ?, "
                "'direct', 1, ?, 'succeeded', 'production', 1, 33.0, 33.0, 34.0)",
                ("6" * 64, "1" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_proposal_generation_attempts "
                "(generation_ref, initialization_id, idempotency_key, request_hash, "
                "route, basis_revision, basis_hash, status, adapter_kind, "
                "attempt_count, created_at) VALUES "
                "('generation_wrong_basis', 'quest_init_constraints', "
                "'generation-wrong-basis-key', ?, 'direct', 2, ?, 'queued', "
                "'production', 0, 33.0)",
                ("6" * 64, "7" * 64),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_reconciliation_checkpoints "
                "(initialization_id, state, first_missing_step, attempt_count, "
                "reason_code, next_retry_at, updated_at) VALUES "
                "('quest_init_constraints', 'partial', 'question_content', 1, "
                "NULL, NULL, 35.0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO hc_reconciliation_attempts "
                "(attempt_ref, initialization_id, step, attempt_number, outcome, "
                "reason_code, started_at, finished_at) VALUES "
                "('reconcile_invalid', 'quest_init_constraints', 'quest_goal', 1, "
                "'accepted', NULL, 35.0, NULL)"
            )

        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_0003_refuses_to_guess_when_multiple_active_initializations_exist(
    tmp_path: Path,
) -> None:
    database = tmp_path / "duplicate-active-0002.sqlite3"
    _upgrade_to_revision(database, "0002_quest_initialization")
    with sqlite3.connect(database) as connection:
        for index in (1, 2):
            connection.execute(
                "INSERT INTO hc_quest_initializations "
                "(initialization_id, status, draft_revision, draft_json, draft_hash, "
                "proposal_revision, created_at, updated_at) VALUES (?, 'draft', 1, "
                "'{}', ?, 0, 40.0, 40.0)",
                (f"quest_init_duplicate_{index}", str(index) * 64),
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="multiple nonterminal initializations"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0002_quest_initialization",)
        assert connection.execute(
            "SELECT count(*) FROM hc_quest_initializations "
            "WHERE status = 'draft'"
        ).fetchone() == (2,)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hc_quest_initializations)"
            )
        }
    assert "draft_schema_ref" not in columns
