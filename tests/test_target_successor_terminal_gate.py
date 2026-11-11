from pathlib import Path

import pytest

from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from meta_research.provider_supervisor import read_transport_envelope, write_transport_envelope
from test_harness_target_root_recovery import _owner_with_active_root, _signed_fixture_receipt


@pytest.mark.parametrize("proof", ["strong", "missing", "weak", "wrong_binding", "not_started", "no_operation"])
def test_successor_reservation_reverifies_original_operation_before_rotation(tmp_path: Path, proof):
    database, owner, request, handle = _owner_with_active_root(tmp_path / "research.sqlite3")
    try:
        run = owner.query_run(request.request_ref)
        operation_ref, owner_hash = "issue296-original-before-rotation", "f" * 64
        receipt = None
        if proof != "no_operation":
            owner.start_operation(run_ref=run.run_ref, operation_ref=operation_ref,
                                  generation=1, invocation_hash=owner_hash, resume=False)
            if proof != "not_started":
                receipt = _signed_fixture_receipt(owner, operation_ref, owner_hash)
            owner.record_operation_failure(operation_ref, "provider_resource_unavailable",
                durable_outcome="not_started" if proof == "not_started" else "terminal",
                transport_receipt=receipt)
        if proof in {"missing", "weak", "wrong_binding"}:
            store = owner._target_provider_ceiling_receipt_verifier
            directory = store._source_path(receipt["transport_invocation_hash"]).parent
            path = directory / "supervisor-exit.json"
            from meta_research.provider_supervisor import read_transport_key_for_operation
            _, key = read_transport_key_for_operation(directory)
            if proof == "missing":
                path.unlink()
            elif proof == "weak":
                payload = read_transport_envelope(path, key)
                payload.pop("process_tree")
                path.unlink()
                write_transport_envelope(path, payload, key)
            else:
                path = store._workspace / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
                payload = read_transport_envelope(path, key)
                payload["provider_operation_ref"] = "another-operation"
                path.unlink()
                write_transport_envelope(path, payload, key)
            before = owner.query_run(request.request_ref)
            with pytest.raises(AgentRuntimeHarnessError, match="provider_terminal_proof_missing"):
                owner.reserve_target_successor(old_handle=handle, recovery_ref="issue296-proof-gated-recovery")
            assert owner.query_run(request.request_ref) == before
            assert owner.query_target_successor_reservation(run.run_ref) is None
        else:
            successor = owner.reserve_target_successor(old_handle=handle,
                recovery_ref="issue296-proof-gated-recovery")
            assert successor.attempt_ref != run.attempt_ref
            if proof != "no_operation":
                # The successor's execution/capability identity is fresh. Its
                # existence cannot prove the old operation; only original facts do.
                owner.verify_operation_terminal(operation_ref)
            if proof == "strong":
                assert owner.query_profile(run.run_ref) == {
                    "schema_ref": "meta-research/harness-failed-transport-profile/v1",
                    "harness_family": run.harness_family,
                    "run_ref": run.run_ref,
                    "provider_transport_receipts": [
                        {"provider_operation_ref": operation_ref, **receipt}
                    ],
                }
    finally:
        database.close()
