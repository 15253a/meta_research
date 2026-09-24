from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import _bundle_runtime
from test_target_launch_admission import _ready_launch


def test_admitted_launch_reads_use_the_persisted_receipt(tmp_path: Path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / "launch-read")
    try:
        _, target, _, dispatch, request = _ready_launch(runtime)
        owner = runtime.owners.agent_runtime
        expected = owner.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="launch-read",
        )

        def no_revalidation(*args, **kwargs):
            raise AssertionError("accepted launch read reran startup validation")

        monkeypatch.setattr(
            owner._target_graph_verifier,
            "verify_target_launch_request",
            no_revalidation,
        )
        for _ in range(3):
            assert owner.query_target_launch_ack(target.target_ref) == expected
            admitted = owner.query_admitted_target_launch(target.target_ref)
            assert admitted is not None
            assert admitted.request == request
            assert admitted.ack == expected
            assert target.target_ref in owner.list_target_root_work_refs()

        with runtime._database.write() as connection:
            connection.execute(
                text("UPDATE ar_target_launches SET receipt_hash = :hash "
                     "WHERE target_ref = :target"),
                {"hash": "0" * 64, "target": target.target_ref},
            )
        for query in (owner.query_target_launch_ack, owner.query_admitted_target_launch):
            with pytest.raises(OwnerConflict, match="target_launch_integrity_invalid"):
                query(target.target_ref)
    finally:
        runtime.close()
