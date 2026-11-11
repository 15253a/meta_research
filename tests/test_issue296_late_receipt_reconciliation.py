from __future__ import annotations

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from meta_research.owners.common import canonical_hash, canonical_json
from test_stopped_target_native_checkpoint import stopped


@pytest.mark.parametrize("stored_receipt", ["exact", "conflicting", "duplicated"])
def test_restored_original_receipt_reconciles_prior_terminal_index(stopped, stored_receipt):
    database, owner, request, run, operation, receipt, directory, _store = stopped
    owner.record_operation_failure(operation, "provider_failed", durable_outcome="terminal",
                                   transport_receipt=receipt)
    before_profile = owner.query_profile(run.run_ref)
    assert before_profile["provider_transport_receipts"] == [{"provider_operation_ref": operation, **receipt}]

    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._owner = owner
    runtime._adapters = {"codex": object()}
    receipt_path = directory / "supervisor-exit.json"
    saved_receipt = directory / "isolated-unavailable-receipt.json"
    receipt_path.rename(saved_receipt)
    with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
        runtime.cancel_target_root(request.request_ref)
    assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
    assert owner.query_profile(run.run_ref)["provider_transport_receipts"] == before_profile["provider_transport_receipts"]
    saved_receipt.rename(receipt_path)

    with database.fenced_write() as connection:
        connection.execute(text(
            "UPDATE ar_target_root_lifecycles SET status='failed', cancel_ref='issue296-stop', "
            "cancel_requested_at=1, cancel_reason='stop unconfirmed operation' WHERE target_run_ref=:ref"
        ), {"ref": run.run_ref})
        if stored_receipt != "exact":
            profile = owner.query_profile(run.run_ref)
            rows = profile["provider_transport_receipts"]
            if stored_receipt == "conflicting":
                rows[0]["supervisor_receipt_hash"] = "b" * 64
            else:
                rows.append(dict(rows[0]))
            connection.execute(text(
                "UPDATE ar_harness_runs SET profile_json=:profile, profile_hash=:hash WHERE run_ref=:ref"
            ), {"ref": run.run_ref, "profile": canonical_json(profile), "hash": canonical_hash(profile)})

    if stored_receipt != "exact":
        with pytest.raises(AgentRuntimeHarnessError, match="harness_operation_state_conflict"):
            owner.reconcile_failed_target_operation(operation, receipt)
        assert owner.latest_operation(run.run_ref).status == "unknown_outcome"
        return

    settled = owner.reconcile_failed_target_operation(operation, receipt)
    assert (settled.operation_ref, settled.status, settled.outcome_code) == (
        operation, "failed", "provider_terminal_reconciled")
    assert owner.query_profile(run.run_ref)["provider_transport_receipts"] == before_profile["provider_transport_receipts"]
    assert owner.reconcile_failed_target_operation(operation, receipt) == settled
    assert owner.target_run_failed(run.run_ref)
