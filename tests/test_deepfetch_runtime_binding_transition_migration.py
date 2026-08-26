from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from meta_research.migration import upgrade_database
from meta_research.owners.common import canonical_hash, canonical_json
from test_migration_recovery import _upgrade_to_revision


def _foreign_keys(
    connection: sqlite3.Connection, table_name: str
) -> set[tuple[str, str, str]]:
    return {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.execute(f"PRAGMA foreign_key_list({table_name})")
    }


def _indexes(
    connection: sqlite3.Connection, table_name: str
) -> set[tuple[int, str, tuple[str, ...]]]:
    return {
        (
            int(row[2]),
            str(row[3]),
            tuple(
                str(column[2])
                for column in connection.execute(
                    f'PRAGMA index_info("{row[1]}")'
                )
            ),
        )
        for row in connection.execute(f"PRAGMA index_list({table_name})")
    }


def test_0038_backfills_attempt_binding_and_session_without_losing_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deepfetch-binding-transition.sqlite3"
    _upgrade_to_revision(database, "0037_question_stage_identity")
    binding = {
        "schema_ref": "meta-research/deepfetch-runtime-binding/v1",
        "provider_ref": "meta_research.deepfetch.CodexDeepFetchAdapter",
        "provider_version": "legacy",
        "model_ref": "gpt-test",
        "harness_ref": "codex-cli",
        "capability_bindings": [
            "workspace-write-public-artifacts",
            "web-fetch-live",
            "web-search-live",
        ],
    }
    binding_json = canonical_json(binding)
    binding_hash = canonical_hash(binding)
    run_ref = "deepfetch_run_migration_0038"
    attempt_ref = "deepfetch_attempt_migration_0038"
    session_ref = "deepfetch_session_migration_0038"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO ar_deepfetch_runs (run_ref, request_ref, "
            "correlation_ref, request_hash, runtime_binding_json, "
            "runtime_binding_hash, status, current_attempt_ref, "
            "attempt_generation, provider_operation_ref, "
            "provider_operation_generation, provider_operation_retry_permitted, "
            "reconciliation_attempt_count, failure_code, created_at, updated_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, 1, ?, 1, 1, "
            "0, 'deepfetch_web_evidence_invalid', 38.0, 38.0, 38.0)",
            (
                run_ref,
                "deepfetch_request_migration_0038",
                "deepfetch_correlation_migration_0038",
                "a" * 64,
                binding_json,
                binding_hash,
                attempt_ref,
                f"{run_ref}:deepfetch:1",
            ),
        )
        connection.execute(
            "INSERT INTO ar_deepfetch_sessions (root_session_ref, run_ref, "
            "native_session_ref, status, created_at, updated_at) VALUES "
            "(?, ?, 'native-legacy-migration-0038', 'open', 38.0, 38.0)",
            (session_ref, run_ref),
        )
        connection.execute(
            "INSERT INTO ar_deepfetch_attempts (attempt_ref, run_ref, generation, "
            "root_session_ref, fence_ref, status, failure_code, started_at, "
            "completed_at) VALUES (?, ?, 1, ?, 'deepfetch_fence_migration_0038', "
            "'failed', 'deepfetch_web_evidence_invalid', 38.0, 38.0)",
            (attempt_ref, run_ref, session_ref),
        )
        before_foreign_keys = _foreign_keys(connection, "ar_deepfetch_attempts")
        before_indexes = _indexes(connection, "ar_deepfetch_attempts")
        connection.commit()

    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns = {
            str(row[1]): {"type": str(row[2]), "notnull": int(row[3])}
            for row in connection.execute("PRAGMA table_info(ar_deepfetch_attempts)")
        }
        migrated = connection.execute(
            "SELECT runtime_binding_json, runtime_binding_hash, native_session_ref "
            "FROM ar_deepfetch_attempts WHERE attempt_ref = ?",
            (attempt_ref,),
        ).fetchone()
        after_foreign_keys = _foreign_keys(connection, "ar_deepfetch_attempts")
        after_indexes = _indexes(connection, "ar_deepfetch_attempts")
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        scratch_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE '_alembic_tmp_%'"
        ).fetchall()
        create_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'ar_deepfetch_attempts'"
            ).fetchone()[0]
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ar_deepfetch_attempts SET runtime_binding_hash = 'short' "
                "WHERE attempt_ref = ?",
                (attempt_ref,),
            )

    assert version == ("0038_deepfetch_binding_audit",)
    assert migrated == (
        binding_json,
        binding_hash,
        "native-legacy-migration-0038",
    )
    assert columns["runtime_binding_json"]["notnull"] == 1
    assert columns["runtime_binding_hash"]["notnull"] == 1
    assert columns["native_session_ref"]["notnull"] == 0
    assert "length(runtime_binding_hash) = 64" in create_sql
    assert after_foreign_keys == before_foreign_keys
    assert after_indexes == before_indexes
    assert foreign_key_failures == []
    assert quick_check == ("ok",)
    assert scratch_tables == []
    assert json.loads(str(migrated[0])) == binding
