from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    ReceiptProof,
    TargetRunHandoff,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.database import Database
from meta_research.migration import upgrade_database
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from test_target_run_owner import (
    _TargetAuthority,
    _commit_transition,
    _records,
    _runtime,
    _seed_admitted_launch,
)
from test_target_run_worker import _closure


@dataclass(frozen=True)
class _Lifecycle:
    target_ref: str
    target_run_ref: str
    root_session_ref: str
    target_attempt_ref: str
    target_fence_ref: str
    status: str
    completion_ref: str


@dataclass(frozen=True)
class _Completion:
    completion_ref: str
    handle: TargetWorkHandle
    implementation_revision_ref: str
    receipt: AcceptanceReceipt


class _CompletionReader:
    def __init__(self, lifecycle: _Lifecycle, completion: _Completion) -> None:
        self.lifecycle = lifecycle
        self.completion = completion
        self.database: Database | None = None

    def query(self, target_ref: str) -> _Lifecycle | None:
        if self.lifecycle.target_ref != target_ref:
            return None
        if self.database is None:
            return self.lifecycle
        with self.database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT status FROM ar_target_root_lifecycles WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            ).one()
        return replace(self.lifecycle, status=row.status)

    def query_completion(self, target_ref: str) -> _Completion | None:
        return (
            self.completion
            if self.completion.handle.target_ref == target_ref
            else None
        )


def _root_terminal(
    handle: TargetWorkHandle,
    *,
    candidate,
    preflight,
    completion_ref: str,
) -> tuple[AcceptedMeasurementClosure, _CompletionReader]:
    ar_receipt = AcceptanceReceipt(
        issuer="agent_runtime",
        kind="target_root_completion_accepted",
        receipt_ref="ar-target-root-completion-receipt-1",
        subject_ref=handle.execution_attempt_ref,
        payload_hash="a" * 64,
    )
    receipt_proof = ReceiptProof(
        receipt_ref=ar_receipt.receipt_ref,
        subject_ref=ar_receipt.subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )
    terminal = replace(
        _closure(handle, preflight, candidate),
        code_review=None,
        result_review=None,
        ar_execution_receipt=receipt_proof,
        root_completion_receipt=receipt_proof,
    )
    reader = _CompletionReader(
        _Lifecycle(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref,
            target_attempt_ref=handle.execution_attempt_ref,
            target_fence_ref=handle.execution_fence_ref,
            status="finalizing",
            completion_ref=completion_ref,
        ),
        _Completion(
            completion_ref=completion_ref,
            handle=handle,
            implementation_revision_ref=terminal.implementation_revision_ref,
            receipt=ar_receipt,
        ),
    )
    return terminal, reader


def _seed_root_frontier(
    database: Database,
    handle: TargetWorkHandle,
    request,
    completion_ref: str,
) -> None:
    with database.fenced_write() as connection:
        connection.execute(
            text(
                "INSERT INTO ar_target_frontier_entries (target_ref, launch_ref, "
                "target_spec_content_hash_ref, target_spec_receipt_ref, "
                "target_spec_receipt_subject_ref, state_revision, state, "
                "current_handle_json, current_handle_hash, terminal_fact_ref, "
                "currentness_known, current, updated_at) VALUES (:target_ref, "
                "'target_launch_1', :spec_hash, :receipt_ref, :receipt_subject, "
                "1, 'running', :handle_json, :handle_hash, NULL, 1, 1, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "spec_hash": request.target_spec_binding.content_hash_ref,
                "receipt_ref": request.target_spec_acceptance_receipt.receipt_ref,
                "receipt_subject": request.target_spec_acceptance_receipt.subject_ref,
                "handle_json": canonical_json(projection_plain_value(handle)),
                "handle_hash": canonical_hash(projection_plain_value(handle)),
            },
        )
        connection.execute(
            text(
                "INSERT INTO ar_target_root_lifecycles (lifecycle_ref, "
                "target_ref, launch_ref, target_run_ref, root_session_ref, "
                "target_attempt_ref, target_fence_ref, initial_handle_json, "
                "initial_handle_hash, candidate_json, candidate_hash, "
                "formal_plan_json, formal_plan_hash, status, completion_ref, "
                "idempotency_key, request_hash, created_at, updated_at) VALUES "
                "('target-root-lifecycle-1', :target_ref, 'target_launch_1', "
                ":target_run_ref, :root_session_ref, :attempt_ref, :fence_ref, "
                ":handle_json, :handle_hash, '{}', :empty_hash, '{}', "
                ":empty_hash, 'finalizing', :completion_ref, "
                "'target-root-lifecycle-seed', :request_hash, 1.0, 1.0)"
            ),
            {
                "target_ref": handle.target_ref,
                "target_run_ref": handle.target_run_ref,
                "root_session_ref": handle.root_session_ref,
                "attempt_ref": handle.execution_attempt_ref,
                "fence_ref": handle.execution_fence_ref,
                "handle_json": canonical_json(projection_plain_value(handle)),
                "handle_hash": canonical_hash(projection_plain_value(handle)),
                "empty_hash": canonical_hash({}),
                "completion_ref": completion_ref,
                "request_hash": "9" * 64,
            },
        )


def _publication_state(database: Database) -> tuple[object, ...]:
    with database.read() as connection:
        return (
            int(
                connection.execute(
                    text(
                        "SELECT revision FROM agent_runtime_state WHERE "
                        "singleton = 'owner'"
                    )
                ).scalar_one()
            ),
            int(
                connection.execute(
                    text("SELECT COUNT(*) FROM ar_target_handoff_manifests")
                ).scalar_one()
            ),
            int(
                connection.execute(
                    text("SELECT COUNT(*) FROM ar_target_work_notices")
                ).scalar_one()
            ),
            int(
                connection.execute(
                    text("SELECT COUNT(*) FROM ar_bundle_inbox_entries")
                ).scalar_one()
            ),
            int(
                connection.execute(
                    text("SELECT COUNT(*) FROM durable_feed")
                ).scalar_one()
            ),
            tuple(
                connection.execute(
                    text(
                        "SELECT next_sequence, generation, wake_pending FROM "
                        "ar_bundle_inbox_state WHERE singleton = 'bundle'"
                    )
                ).one()
            ),
            tuple(
                connection.execute(
                    text(
                        "SELECT next_sequence, generation, wake_pending FROM "
                        "ar_bundle_inbox_scopes WHERE run_ref = 'bundle_run_1'"
                    )
                ).one()
            ),
            connection.execute(
                text(
                    "SELECT status FROM ar_target_root_lifecycles WHERE "
                    "target_ref = 'target-1'"
                )
            ).scalar_one(),
        )


def test_root_completion_publish_is_refs_only_replay_safe_and_wakes_bundle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-publish.sqlite3"
    upgrade_database(path)
    candidate, _formal_plan, handle, preflight, request = _records()
    _seed_admitted_launch(path, request, handle)
    completion_ref = "target-root-completion-1"
    terminal, completion_reader = _root_terminal(
        handle,
        candidate=candidate,
        preflight=preflight,
        completion_ref=completion_ref,
    )
    transition = replace(
        _commit_transition(handle, terminal),
        target_execution_closure_ref=completion_ref,
    )
    target_authority = _TargetAuthority(transition)
    runtime, database = _runtime(path, None, target_authority)
    completion_reader.database = database
    runtime.bind_target_root_completion_reader(completion_reader)
    try:
        _seed_root_frontier(database, handle, request, completion_ref)
        before = _publication_state(database)
        with pytest.raises(
            OwnerConflict,
            match="target_root_completion_publication_authority_invalid",
        ):
            runtime.publish_target_root_completion(
                target_ref=handle.target_ref,
                completion_ref="tampered-completion-ref",
                target_commit_ref=terminal.target_commit_ref,
            )
        assert _publication_state(database) == before

        # Even a crash-corrupted lifecycle state cannot leave a terminal
        # frontier/notice behind: the lifecycle CAS is in the same transaction.
        with database.fenced_write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_target_root_lifecycles SET status = 'completed' "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            )
        corrupt_before = _publication_state(database)
        with pytest.raises(
            OwnerConflict,
            match="target_root_completion_publication_stale",
        ):
            runtime.publish_target_root_completion(
                target_ref=handle.target_ref,
                completion_ref=completion_ref,
                target_commit_ref=terminal.target_commit_ref,
            )
        assert _publication_state(database) == corrupt_before
        with database.fenced_write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_target_root_lifecycles SET status = 'finalizing' "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            )

        handoff = runtime.publish_target_root_completion(
            target_ref=handle.target_ref,
            completion_ref=completion_ref,
            target_commit_ref=terminal.target_commit_ref,
        )
        assert handoff == TargetRunHandoff(
            handle_history=(handle,),
            code_review_preflights=(),
            stop_decisions=(),
            recovered_blockers=(),
            recovery_evidence_refs=(),
            terminal=terminal,
        )
        notice = runtime.query_target_work_notice(handle.target_ref)
        assert notice is not None
        assert notice.kind == "target_completed"
        assert notice.terminal_fact_ref == terminal.target_commit_ref
        batch = runtime.read_bundle_inbox(
            run_ref="bundle_run_1",
            attempt_ref="bundle_attempt_1",
            fence_ref="bundle_fence_1",
        )
        assert tuple(item.notice_ref for item in batch.notices) == (notice.notice_ref,)
        assert runtime.list_target_root_work_refs() == ()
        assert _publication_state(database)[-1] == "completed"
        assert runtime.query_target_root_completion_handoff(
            target_ref=handle.target_ref,
            completion_ref=completion_ref,
            target_commit_ref=terminal.target_commit_ref,
        ) == handoff

        published = _publication_state(database)
        assert runtime.publish_target_root_completion(
            target_ref=handle.target_ref,
            completion_ref=completion_ref,
            target_commit_ref=terminal.target_commit_ref,
        ) == handoff
        assert _publication_state(database) == published
    finally:
        database.close()
