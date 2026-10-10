from __future__ import annotations

import sqlite3
from pathlib import Path

from test_migration_recovery import _upgrade_to_revision


def test_0035_companion_turns_upgrade_without_fabricating_view_context(
    tmp_path: Path,
) -> None:
    database = tmp_path / "companion-view-context.sqlite3"
    _upgrade_to_revision(database, "0035_runtime_protection")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hc_companion_sessions "
            "(session_ref, scope_ref, native_session_ref, status, created_at, "
            "updated_at) VALUES (?, ?, NULL, 'open', 1.0, 1.0)",
            ("legacy-session", "quest:legacy"),
        )
        connection.execute(
            "INSERT INTO hc_companion_turns "
            "(interaction_ref, session_ref, ordinal, message, message_hash, "
            "assistant_status, assistant_content, assistant_content_hash, "
            "adapter_kind, reason_code, attempt_count, idempotency_key, "
            "command_hash, created_at, updated_at) VALUES "
            "(?, ?, 1, ?, ?, 'queued', NULL, NULL, NULL, NULL, 0, ?, ?, 1.0, "
            "1.0)",
            (
                "legacy-turn",
                "legacy-session",
                "legacy contextless message",
                "a" * 64,
                "legacy-turn-idempotency",
                "b" * 64,
            ),
        )
        connection.commit()

    _upgrade_to_revision(database, "0036_companion_view_context")
    _upgrade_to_revision(database, "0036_companion_view_context")

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(hc_companion_turns)")
        }
        legacy = connection.execute(
            "SELECT message, assistant_status, view_context_json, "
            "view_context_hash FROM hc_companion_turns WHERE "
            "interaction_ref = 'legacy-turn'"
        ).fetchone()
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA quick_check").fetchone()

    assert version == ("0036_companion_view_context",)
    assert {"view_context_json", "view_context_hash"} <= columns
    assert legacy == ("legacy contextless message", "queued", None, None)
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_0073_companion_turn_upgrade_keeps_original_words_without_fabricating_guidance_options(tmp_path: Path) -> None:
    from meta_research.owners.common import canonical_hash

    database = tmp_path / "guidance-confirmation.sqlite3"
    _upgrade_to_revision(database, "0073_merge_search_source")
    original = "  Existing explanation, with no confirmed guidance.\n"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO hc_companion_sessions (session_ref,scope_ref,status,created_at,updated_at) "
            "VALUES ('old-session','quest:old-quest','open',1.0,1.0)")
        connection.execute(
            "INSERT INTO hc_companion_turns (interaction_ref,session_ref,ordinal,message,message_hash,"
            "assistant_status,attempt_count,idempotency_key,command_hash,created_at,updated_at) "
            "VALUES ('old-turn','old-session',1,?,?,'queued',0,'old-turn-key',?,1.0,1.0)",
            (original, canonical_hash(original), "a" * 64))
    _upgrade_to_revision(database, "0074_guidance_confirmation")
    _upgrade_to_revision(database, "0074_guidance_confirmation")
    with sqlite3.connect(database) as connection:
        legacy = connection.execute(
            "SELECT message,guidance_options_json,guidance_options_hash "
            "FROM hc_companion_turns WHERE interaction_ref='old-turn'").fetchone()
        assert legacy == (original, None, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
