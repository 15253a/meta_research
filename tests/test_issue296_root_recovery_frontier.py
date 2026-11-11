from __future__ import annotations

import sys

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError
from meta_research.owners.common import OwnerConflict
from test_issue296_stable_workspace import _SignedCeilingRunner, _runtime, _workspace
from test_target_root_finalizer import _admit_independent_target_root


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux recovery integration")


@pytest.mark.parametrize("damage", [None, "terminal_frontier", "handle_hash", "lifecycle_identity"])
def test_independent_root_recovery_verifies_original_frontier(tmp_path, damage):
    runner = _SignedCeilingRunner()
    runtime = _runtime(tmp_path / "issue296-root-recovery", runner)
    try:
        target, candidate, plan, admission, old = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref, handle=old, candidate=candidate, formal_plan=plan,
            idempotency_key="issue296-frontier-activate",
        )
        _workspace(runtime, old)
        with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
            runtime.harnesses.run_or_resume_target_root(
                admission.run.request_ref, prompt="Complete the original bounded operation.",
                mcp_base_url="http://127.0.0.1:8999",
            )
        successor = runtime.harnesses.recover_fenced_target_root(admission.run.request_ref, old_handle=old)
        assert successor.run.attempt_ref != old.execution_attempt_ref
        assert runtime.target_run_runtime.process_once(target.target_ref)
        authority = runtime.target_run_authorities.agent_runtime
        projection = authority.query_target_provider_ceiling_recovery(old)

        if damage is not None:
            mutations = {
                "terminal_frontier": "UPDATE ar_target_frontier_entries SET state='terminal',terminal_fact_ref=:value WHERE target_ref=:target",
                "handle_hash": "UPDATE ar_target_frontier_entries SET current_handle_hash=:value WHERE target_ref=:target",
                "lifecycle_identity": "UPDATE ar_target_root_lifecycles SET root_session_ref=:value WHERE target_ref=:target",
            }
            with runtime._database.fenced_write() as connection:
                connection.execute(text(mutations[damage]), {"target": target.target_ref, "value": "b" * 64})
            with pytest.raises(OwnerConflict, match="target_run_frontier_not_current"):
                authority.verify_target_run_recovery_successor(
                    old, projection.replacement_handle, projection.reservation.recovery_ref)
            assert len(runner.calls) == 1
            return

        assert runtime.target_run_runtime.process_once(target.target_ref)
        current = authority.query_current_target_work_handle(target.target_ref)
        assert current == projection.replacement_handle
        assert (current.root_session_ref, current.execution_attempt_ref, current.execution_fence_ref) == (
            successor.run.root_session_ref, successor.run.attempt_ref, successor.run.fence_ref)
        with pytest.raises(OwnerConflict):
            _workspace(runtime, old)
        authority.reserve_target_workspace(handle=current, idempotency_key="issue296-current-recovery-workspace")
        assert _workspace(runtime, current)[1].is_dir()
        assert len(runner.calls) == 1
        with runtime._database.read() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM ar_target_run_activations WHERE target_ref=:target"
            ), {"target": target.target_ref}).scalar_one() == 0
    finally:
        runtime.close()
