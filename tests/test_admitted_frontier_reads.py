"""Accepted Target discovery must not redo upstream graph admission."""

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)


@pytest.fixture
def running_target(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "admitted-frontier")
    try:
        target, candidate, formal_plan, _, handle = _admit_independent_target_root(
            runtime
        )
        owner = runtime.owners.agent_runtime
        launch = owner.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key="activate-admitted-frontier",
        )
        with runtime._database.read() as connection:
            bundle_run = connection.execute(
                text("SELECT decisions.run_ref FROM ar_target_launches AS launches "
                     "JOIN ar_bundle_dispatch_decisions AS decisions ON "
                     "decisions.decision_ref = launches.dispatch_decision_ref "
                     "WHERE launches.target_ref = :target"),
                {"target": target.target_ref},
            ).scalar_one()
        yield runtime, target, handle, bundle_run
    finally:
        runtime.close()


def test_running_frontier_and_work_discovery_reuse_admitted_receipts(
    running_target, monkeypatch
):
    runtime, target, handle, bundle_run = running_target
    owner = runtime.owners.agent_runtime
    expected = owner.query_target_frontier_entry(target.target_ref)
    assert expected is not None
    assert expected.current_handle == handle

    def no_readmission(*args, **kwargs):
        raise AssertionError("admitted frontier repeated upstream graph admission")

    monkeypatch.setattr(
        owner._target_graph_verifier,
        "verify_target_candidate_projection_receipt",
        no_readmission,
    )
    for _ in range(3):
        assert owner.query_target_frontier_entry(target.target_ref) == expected
        assert owner.list_target_root_work_refs() == (target.target_ref,)
        assert owner.list_bundle_target_root_work_refs(bundle_run) == (
            target.target_ref,
        )


@pytest.mark.parametrize(
    ("table", "field", "invalid_value", "error"),
    (
        ("ar_target_launches", "receipt_hash", "0" * 64,
         "target_launch_integrity_invalid"),
        ("ar_target_frontier_entries", "target_spec_receipt_ref", "other-receipt",
         "target_frontier_integrity_invalid"),
        ("ar_target_frontier_entries", "target_spec_content_hash_ref", "other-hash",
         "target_frontier_integrity_invalid"),
        ("ar_target_frontier_entries", "current_handle_hash", "0" * 64,
         "target_frontier_integrity_invalid"),
        ("ar_bundle_dispatch_decisions", "receipt_hash", "0" * 64,
         "bundle_dispatch_decision_invalid"),
        ("ar_bundle_dispatch_decisions", "receipt_ref", "other-dispatch-receipt",
         "target_frontier_integrity_invalid"),
    ),
)
def test_admitted_frontier_keeps_exact_receipt_and_handle_checks(
    running_target, table, field, invalid_value, error
):
    runtime, target, _, bundle_run = running_target
    owner = runtime.owners.agent_runtime
    where = (
        "selected_target_ref" if table == "ar_bundle_dispatch_decisions"
        else "target_ref"
    )
    with runtime._database.write() as connection:
        connection.execute(
            text(f"UPDATE {table} SET {field} = :value WHERE {where} = :target"),
            {"value": invalid_value, "target": target.target_ref},
        )
    for query in (
        lambda: owner.query_target_frontier_entry(target.target_ref),
        owner.list_target_root_work_refs,
        lambda: owner.list_bundle_target_root_work_refs(bundle_run),
    ):
        with pytest.raises(OwnerConflict, match=error):
            query()
