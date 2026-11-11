"""Existing generic Owner entry point, with controlled signed-issuer boundaries.

The root integration suite proves the real runtime chain; this narrow Owner test
ensures the older public entry point cannot retire the same TargetRun directory.
"""

from dataclasses import replace
import json
from types import SimpleNamespace

from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from meta_research.feed import DurableFeed
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime_harness import AgentRuntimeTargetSuccessorReservation
from meta_research.owners.common import AcceptanceReceipt, canonical_hash, canonical_json
from meta_research.owners.target_run_runtime import SQLiteTargetRunAgentAuthority
from test_issue296_workspace_migration import _reservation
from test_target_run_owner import _HarnessAuthority, _records, _runtime, _seed_admitted_launch
from test_target_run_worker import _handle, _receipt, _recovered_blocker


class _RecoveryIssuer(_HarnessAuthority):
    failure = None
    workspace_reader = None

    def query_target_execution_failure(self, handle):
        assert handle == self.current
        return self.failure

    def verify_target_run_recovery_successor(self, old, replacement, recovery_ref):
        assert old == self.current
        assert replacement == self.failure.replacement_handle
        assert recovery_ref == self.failure.failure_ref
        return replacement

    def query_target_workspace(self, run_ref):
        return self.workspace_reader.query_target_workspace(run_ref)


def test_generic_same_run_recovery_keeps_original_reservation_through_two_rotations(tmp_path, monkeypatch):
    path = tmp_path / "issue296-generic-owner.sqlite3"
    upgrade_database(path)
    candidate, plan, initial, preflight, request = _records()
    _seed_admitted_launch(path, request, initial)
    issuer = _RecoveryIssuer(initial)
    runtime, database = _runtime(path, issuer)
    try:
        runtime.activate_target_run(target_ref=initial.target_ref, handle=initial, candidate=candidate,
            formal_plan=plan, preflight=preflight, idempotency_key="issue296-generic-activate")
        row = _reservation()
        payload = {**json.loads(row["payload_json"]), "target_ref": initial.target_ref,
            "target_run_ref": initial.target_run_ref, "root_session_ref": initial.root_session_ref,
            "target_attempt_ref": initial.execution_attempt_ref, "target_fence_ref": initial.execution_fence_ref}
        row.update({key: payload[key] for key in ("target_ref", "target_run_ref", "root_session_ref",
                                                 "target_attempt_ref", "target_fence_ref")})
        row.update(payload_json=canonical_json(payload), payload_hash=canonical_hash(payload),
            request_hash=canonical_hash({"command": "reserve_target_run_workspace", **payload}),
            receipt_hash=canonical_hash({"issuer": "agent_runtime", "kind": "target_run_workspace_reserved",
                "receipt_ref": row["receipt_ref"], "subject_ref": row["workspace_ref"],
                "bindings": {**payload, "workspace_ref": row["workspace_ref"], "ordinal": row["ordinal"],
                    "root_name": row["root_name"], "payload_hash": canonical_hash(payload)}}))
        with database.fenced_write() as connection:
            connection.execute(text("INSERT INTO ar_target_run_workspaces (" + ",".join(row) + ") VALUES ("
                                    + ",".join(":" + key for key in row) + ")"), row)
        # The actual workspace reader verifies the stored reservation receipt;
        # only this test's Harness issuer/terminal/reservation sources are fixed.
        issuer.workspace_reader = SQLiteTargetRunAgentAuthority(database, DurableFeed(database),
            harness=SimpleNamespace(query_target_run_by_ref=lambda ref: SimpleNamespace(
                run_ref=ref, request={"target_ref": initial.target_ref})),
            graph=None, memory=None, domain_reader=None, execution_verifier=issuer)
        original = issuer.query_target_workspace(initial.target_run_ref)
        for generation in (1, 2):
            old = issuer.current
            new = _handle(f"issue296-generic-{generation}", target_run=initial.target_run_ref)
            blocker_ref = f"issue296-generic-blocker-{generation}"
            blocker = replace(_recovered_blocker(old), blocker_ref=blocker_ref,
                blocker_receipt=_receipt(blocker_ref, blocker_ref + "-accepted"),
                recovery_receipt=_receipt(blocker_ref, blocker_ref + "-recovery"))
            reservation = AgentRuntimeTargetSuccessorReservation(
                recovery_ref=blocker_ref, target_ref=old.target_ref, target_run_ref=old.target_run_ref,
                old_handle_json=canonical_json(projection_plain_value(old)),
                old_handle_hash=canonical_hash(projection_plain_value(old)),
                new_root_session_ref=new.root_session_ref, new_attempt_ref=new.execution_attempt_ref,
                new_fence_ref=new.execution_fence_ref, generation=generation,
                binding_hash=canonical_hash({"controlled-reservation": generation}))
            issuer.failure = SimpleNamespace(blocker=blocker, replacement_handle=new,
                failure_ref=blocker_ref, stop_decision=None,
                generic_binding_ref=f"issue296-generic-binding-{generation}",
                generic_binding_receipt=AcceptanceReceipt(issuer="research_graph", kind="generic_execution_binding",
                    receipt_ref=f"issue296-generic-receipt-{generation}",
                    subject_ref=blocker_ref, payload_hash=canonical_hash({"controlled-terminal": generation})))
            def verified_reservation(*, old_handle, recovery_ref):
                assert old_handle == old and recovery_ref == blocker_ref
                return reservation
            monkeypatch.setattr(runtime._harness_runs, "verify_target_successor_reservation", verified_reservation)
            frontier = runtime.recover_target_run_from_execution_failure(
                target_ref=initial.target_ref, idempotency_key=f"issue296-generic-recover-{generation}")
            assert frontier.current_handle == new
            issuer.current = new
            assert issuer.query_target_workspace(initial.target_run_ref) == original
        with database.read() as connection:
            assert [dict(item._mapping) for item in connection.execute(text("SELECT * FROM ar_target_run_workspaces"))] == [row]
            assert connection.execute(text("SELECT COUNT(*) FROM ar_target_run_recoveries")).scalar_one() == 2
    finally:
        database.close()
