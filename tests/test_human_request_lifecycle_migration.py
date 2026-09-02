from __future__ import annotations

import sqlite3
from pathlib import Path

from meta_research.owners.common import canonical_hash, canonical_json
from test_migration_recovery import _upgrade_to_revision


_REQUEST_COLUMNS_0040 = (
    "request_ref",
    "issuer",
    "request_id",
    "revision",
    "quest_ref",
    "kind",
    "obligation",
    "business_purpose",
    "target_assertion_json",
    "target_assertion_hash",
    "acceptance_conditions_json",
    "acceptance_conditions_hash",
    "required_authorization_json",
    "required_authorization_hash",
    "expires_at",
    "identity_hash",
    "status",
    "is_current",
    "created_at",
    "updated_at",
)


def _insert_request(
    connection: sqlite3.Connection,
    *,
    request_ref: str,
    revision: int,
    status: str,
    is_current: int,
    predecessor_request_ref: str | None = None,
) -> None:
    target = {"revision": revision}
    conditions = [f"human-response-{revision}"]
    columns = list(_REQUEST_COLUMNS_0040)
    values: list[object] = [
        request_ref,
        "agent_runtime",
        "human_request_lineage",
        revision,
        "quest_migration",
        "offline_action",
        f"obligation-{revision}",
        f"business-purpose-{revision}",
        canonical_json(target),
        canonical_hash(target),
        canonical_json(conditions),
        canonical_hash(conditions),
        None,
        None,
        None,
        canonical_hash({"request": request_ref}),
        status,
        is_current,
        float(revision),
        float(revision),
    ]
    if predecessor_request_ref is not None:
        columns.append("predecessor_request_ref")
        values.append(predecessor_request_ref)
    connection.execute(
        f"INSERT INTO owner_human_requests ({', '.join(columns)}) VALUES "
        f"({', '.join('?' for _ in columns)})",
        values,
    )


def _insert_evaluation_and_disposition(
    connection: sqlite3.Connection,
    *,
    revision: int,
    decision: str,
) -> None:
    request_ref = f"human_request_lineage:r{revision}"
    evaluation_ref = f"evaluation_lineage_r{revision}"
    connection.execute(
        "INSERT INTO owner_human_request_evaluations "
        "(evaluation_ref, request_ref, sequence, decision, response_refs_json, "
        "response_refs_hash, evidence_refs_json, evidence_refs_hash, "
        "reason_code, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
        (
            evaluation_ref,
            request_ref,
            decision,
            canonical_json([f"response-{revision}"]),
            canonical_hash([f"response-{revision}"]),
            canonical_json([f"evidence-{revision}"]),
            canonical_hash([f"evidence-{revision}"]),
            f"reason-{revision}",
            float(revision),
        ),
    )
    connection.execute(
        "INSERT INTO owner_human_request_dispositions "
        "(disposition_ref, request_ref, decision, evaluation_ref, receipt_ref, "
        "receipt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"disposition_lineage_r{revision}",
            request_ref,
            decision,
            evaluation_ref,
            f"disposition_receipt_r{revision}",
            canonical_hash({"disposition": revision}),
            float(revision),
        ),
    )


def test_0041_preserves_history_and_accepts_operation_lifecycle_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "human-request-lifecycle.sqlite3"
    _upgrade_to_revision(database, "0040_root_completion_seam")

    with sqlite3.connect(database) as connection:
        _insert_request(
            connection,
            request_ref="human_request_lineage:r1",
            revision=1,
            status="satisfied",
            is_current=0,
        )
        _insert_request(
            connection,
            request_ref="human_request_lineage:r2",
            revision=2,
            status="declined",
            is_current=1,
        )
        _insert_evaluation_and_disposition(
            connection, revision=1, decision="satisfied"
        )
        _insert_evaluation_and_disposition(
            connection, revision=2, decision="declined"
        )
        connection.execute(
            "INSERT INTO ar_harness_runs (request_ref, idempotency_key, "
            "request_json, request_hash, run_ref, attempt_ref, "
            "attempt_generation, root_session_ref, native_session_ref, "
            "fence_ref, harness_family, model_ref, auth_profile_ref, "
            "capability_binding_hash, mcp_binding_json, mcp_binding_hash, "
            "profile_json, profile_hash, failure_code, status, created_at, "
            "updated_at, completed_at) VALUES (?, ?, '{}', ?, ?, ?, 1, ?, "
            "NULL, ?, 'codex', 'test-model', 'test-auth', ?, NULL, NULL, NULL, "
            "NULL, NULL, 'running', 1.0, 1.0, NULL)",
            (
                "harness_request_migration",
                "harness-idempotency-migration",
                canonical_hash({}),
                "harness_run_migration",
                "harness_attempt_migration",
                "harness_session_migration",
                "harness_fence_migration",
                "b" * 64,
            ),
        )
        requests_before = connection.execute(
            f"SELECT {', '.join(_REQUEST_COLUMNS_0040)} "
            "FROM owner_human_requests ORDER BY revision"
        ).fetchall()
        evaluations_before = connection.execute(
            "SELECT * FROM owner_human_request_evaluations ORDER BY request_ref"
        ).fetchall()
        dispositions_before = connection.execute(
            "SELECT * FROM owner_human_request_dispositions ORDER BY request_ref"
        ).fetchall()

    _upgrade_to_revision(database, "0041_human_request_lifecycle")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        control_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info('ar_run_controls')"
            ).fetchall()
        }
        assert control_columns["runtime_binding_hash"][3] == 0
        assert control_columns["attempt_generation"][3] == 0
        assert connection.execute(
            f"SELECT {', '.join(_REQUEST_COLUMNS_0040)} "
            "FROM owner_human_requests ORDER BY revision"
        ).fetchall() == requests_before
        assert connection.execute(
            "SELECT * FROM owner_human_request_evaluations ORDER BY request_ref"
        ).fetchall() == evaluations_before
        assert connection.execute(
            "SELECT * FROM owner_human_request_dispositions ORDER BY request_ref"
        ).fetchall() == dispositions_before
        assert connection.execute(
            "SELECT predecessor_request_ref FROM owner_human_requests "
            "WHERE request_ref = 'human_request_lineage:r2'"
        ).fetchone() == ("human_request_lineage:r1",)

        connection.execute(
            "UPDATE owner_human_requests SET is_current = 0 "
            "WHERE request_ref = 'human_request_lineage:r2'"
        )
        _insert_request(
            connection,
            request_ref="human_request_lineage:r3",
            revision=3,
            status="unsatisfied",
            is_current=1,
            predecessor_request_ref="human_request_lineage:r2",
        )
        connection.execute(
            "INSERT INTO owner_human_request_waiters "
            "(request_ref, waiter_ref, generation, target_assertion_json, "
            "target_assertion_hash, wait_scope, other_blockers_json, "
            "other_blockers_hash, status, created_at, updated_at) VALUES "
            "('human_request_lineage:r3', 'waiter_lineage_r3', 3, '{}', ?, "
            "'local', '[]', ?, 'blocked', 3.0, 3.0)",
            (canonical_hash({}), canonical_hash([])),
        )
        _insert_evaluation_and_disposition(
            connection, revision=3, decision="unsatisfied"
        )
        connection.execute(
            "INSERT INTO owner_human_request_open_effects "
            "(issuer, effect_key, effect_id, request_ref, waiter_ref, generation, "
            "operation_binding_json, operation_binding_hash, yield_fact_json, "
            "yield_fact_hash, receipt_ref, receipt_hash, created_at) VALUES "
            "('agent_runtime', 'effect_key_r3', 'effect_r3', "
            "'human_request_lineage:r3', 'waiter_lineage_r3', 3, '{}', ?, "
            "'{}', ?, 'open_receipt_r3', ?, 3.0)",
            (
                canonical_hash({"operation": 3}),
                canonical_hash({"yield": 3}),
                canonical_hash({"receipt": 3}),
            ),
        )
        rejection_identity = {
            "request_ref": "human_request_lineage:r3",
            "issuer": "agent_runtime",
            "request_id": "human_request_lineage",
            "request_revision": 3,
        }
        rejection_payload = {
            "schema_ref": "meta-research/human-request-response-rejection/v1",
            "rejection_ref": "human_response_rejection_r3",
            **rejection_identity,
            "reason_code": "human_response_secret_forbidden",
            "request_identity_hash": canonical_hash(rejection_identity),
            "idempotency_hash": canonical_hash({"safe_key": "rejection-r3"}),
        }
        connection.execute(
            "INSERT INTO hc_human_request_response_rejections "
            "(rejection_ref, request_ref, issuer, request_id, request_revision, "
            "reason_code, request_identity_hash, idempotency_hash, receipt_ref, "
            "receipt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rejection_payload["rejection_ref"],
                rejection_payload["request_ref"],
                rejection_payload["issuer"],
                rejection_payload["request_id"],
                rejection_payload["request_revision"],
                rejection_payload["reason_code"],
                rejection_payload["request_identity_hash"],
                rejection_payload["idempotency_hash"],
                "rejection_receipt_r3",
                canonical_hash(rejection_payload),
                3.0,
            ),
        )
        connection.execute(
            "UPDATE ar_harness_runs SET status = 'suspended' "
            "WHERE run_ref = 'harness_run_migration'"
        )

        assert connection.execute(
            "SELECT status, predecessor_request_ref FROM owner_human_requests "
            "WHERE request_ref = 'human_request_lineage:r3'"
        ).fetchone() == ("unsatisfied", "human_request_lineage:r2")
        assert connection.execute(
            "SELECT decision FROM owner_human_request_evaluations "
            "WHERE request_ref = 'human_request_lineage:r3'"
        ).fetchone() == ("unsatisfied",)
        assert connection.execute(
            "SELECT decision FROM owner_human_request_dispositions "
            "WHERE request_ref = 'human_request_lineage:r3'"
        ).fetchone() == ("unsatisfied",)
        assert connection.execute(
            "SELECT request_ref, waiter_ref, receipt_ref "
            "FROM owner_human_request_open_effects"
        ).fetchone() == (
            "human_request_lineage:r3",
            "waiter_lineage_r3",
            "open_receipt_r3",
        )
        assert connection.execute(
            "SELECT request_ref, reason_code, receipt_ref FROM "
            "hc_human_request_response_rejections"
        ).fetchone() == (
            "human_request_lineage:r3",
            "human_response_secret_forbidden",
            "rejection_receipt_r3",
        )
        assert connection.execute(
            "SELECT status FROM ar_harness_runs "
            "WHERE run_ref = 'harness_run_migration'"
        ).fetchone() == ("suspended",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
