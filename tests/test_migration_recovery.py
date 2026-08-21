from __future__ import annotations

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
    assert version == ("0004_idea_stage",)
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
        ).fetchone() == ("0004_idea_stage",)


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
        ).fetchone() == ("0004_idea_stage",)
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

    assert version == ("0004_idea_stage",)
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
        ).fetchone() == ("0004_idea_stage",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


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
