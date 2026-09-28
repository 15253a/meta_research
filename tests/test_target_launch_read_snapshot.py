"""Launch rechecks use separate read cuts before and inside AR's write lock."""
from pathlib import Path

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_target_launch_admission import _ready_launch, _side_effect_counts
from test_target_root_finalizer import _current_bundle_runtime


def test_real_admission_verifications_have_separate_read_cuts_and_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _current_bundle_runtime(tmp_path / "launch-read-cuts")
    try:
        _, target, _, dispatch, request = _ready_launch(runtime)
        database = runtime._database
        verifier = runtime.owners.research_graph._receipt_verifier
        original = verifier._formal_target_launch_authority
        cuts, caches, writes = [], [], []

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {
                "INSERT", "UPDATE", "DELETE", "REPLACE"
            }:
                writes.append(statement)

        def inspect(target_ref):
            cuts.append(database._read_cut.get())
            caches.append(database._read_cache.get())
            assert cuts[-1] is not None
            assert caches[-1] is not None
            event.listen(database._engine, "before_cursor_execute", observe_sql)
            try:
                return original(target_ref)
            finally:
                event.remove(database._engine, "before_cursor_execute", observe_sql)

        monkeypatch.setattr(verifier, "_formal_target_launch_authority", inspect)
        ack = runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="launch-read-cuts",
        )
        assert ack.target_ref == target.target_ref
        assert len(cuts) == len(caches) == 2
        assert cuts[0] is not cuts[1] and caches[0] is not caches[1]
        assert writes == []
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()


def test_second_launch_verification_rejects_a_source_changed_after_first_read_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _current_bundle_runtime(tmp_path / "launch-changed-source")
    try:
        _, target, _, dispatch, request = _ready_launch(runtime)
        database = runtime._database
        verifier = runtime.owners.research_graph._receipt_verifier
        original = verifier.verify_target_launch_request
        before = _side_effect_counts(runtime)
        calls = []

        def verify(value):
            calls.append(value)
            result = original(value)
            assert database._read_cut.get() is None and database._read_cache.get() is None
            if len(calls) == 1:
                with database.write() as connection:
                    connection.execute(
                        text(
                            "UPDATE rg_target_spec_acceptances "
                            "SET receipt_hash=:hash WHERE target_ref=:target_ref"
                        ),
                        {"hash": "0" * 64, "target_ref": target.target_ref},
                    )
            return result

        monkeypatch.setattr(verifier, "verify_target_launch_request", verify)
        with pytest.raises(OwnerConflict, match="target_spec_content_receipt_invalid"):
            runtime.owners.agent_runtime.admit_target_launch(
                request,
                dispatch_decision_ref=dispatch.decision_ref,
                idempotency_key="launch-changed-source",
            )
        assert len(calls) == 2
        assert _side_effect_counts(runtime) == before
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()
