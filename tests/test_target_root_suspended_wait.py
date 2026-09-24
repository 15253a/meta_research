from types import SimpleNamespace
import asyncio

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_runtime import TargetRunRuntime
from test_target_run_owner import _records


def _driver(*, status="suspended", lifecycle_status="running", cancel=False):
    candidate, formal_plan, handle, _preflight, request = _records()
    launch = SimpleNamespace(
        launch_ref="wait-launch", target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref, graph_ref="wait-graph", request=request,
    )
    admission = SimpleNamespace(
        target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
        harness_request_ref="wait-harness-request", root_session_ref=handle.root_session_ref,
        execution_attempt_ref=handle.execution_attempt_ref,
        execution_fence_ref=handle.execution_fence_ref,
        status=status, failure_code=None,
    )
    state = SimpleNamespace(
        admission=admission, handle=handle, error=None, events=[],
        lifecycle=SimpleNamespace(status=lifecycle_status,cancel_ref="cancel" if cancel else None),
    )

    def current(_target):
        state.events.append("current_handle")
        if state.error:
            raise OwnerConflict(state.error)
        if state.admission.status == "suspended":
            raise OwnerConflict("target_run_harness_identity_invalid")
        return handle

    def input_binding(**kwargs):
        state.events.append("input_binding")
        return SimpleNamespace(binding_ref="wait-input-binding")

    def materialize(*, handle):
        assert handle == state.handle
        state.events.append("materialize")
        return ()

    def finalize(*, handle, evidence):
        assert handle == state.handle
        assert evidence.evidence_ref == "existing-final-result"
        state.events.append("finalize")
        return SimpleNamespace(status="completed",pending_code=None,
            completion_ref="existing-completion",target_commit_ref="new-commit")

    def publish(**kwargs):
        state.events.append("publish")

    def cancelled(**kwargs):
        state.events.append("cancelled")
        return SimpleNamespace(status="cancelled")

    def cancel_root(_request):
        state.events.append("cancel_root")
        return True

    runtime=TargetRunRuntime(
        agent_runtime=SimpleNamespace(query_admitted_target_launch=lambda _:launch,
            publish_target_root_completion=publish),
        research_graph=SimpleNamespace(
            query_target_candidate_projection=lambda **kw:SimpleNamespace(candidate=candidate,source_spec_hash="b"*64),
            query_target_formal_plan_projection=lambda **kw:SimpleNamespace(formal_plan=formal_plan)),
        target_graph=SimpleNamespace(query_execution_input_binding_for_attempt=input_binding),
        target_agent=SimpleNamespace(query_target_harness_admission=lambda _:state.admission,
            query_current_target_work_handle=current,
            query_target_workspace=lambda _:SimpleNamespace(workspace_ref="existing-workspace")),
        target_root_lifecycle=SimpleNamespace(query=lambda _:state.lifecycle,
            mark_cancelled=cancelled,mark_completed=lambda **kw:None),
        harnesses=SimpleNamespace(cancel_target_root=cancel_root,
            query_target_root_completion_evidence=lambda _:SimpleNamespace(evidence_ref="existing-final-result")),
        finalizer=SimpleNamespace(materialize_inputs=materialize,finalize=finalize),
    )
    return runtime,state


def test_suspended_target_waits_without_requesting_execution_or_touching_results():
    runtime,state=_driver()
    for _ in range(2):
        assert runtime.process_once(state.handle.target_ref) is False
        status=runtime.query_status(state.handle.target_ref)
        assert status.phase == "harness_suspended"
        assert status.pending_code == "runtime_run_suspended"
        assert state.events == []


def test_daemon_reports_ready_after_observing_the_suspended_target():
    from meta_research.web import ReconciliationHealth, _process_target_runs
    runtime,state=_driver()
    async def observe():
        health=ReconciliationHealth()
        health.status="unavailable"
        health.last_error="target_run_harness_identity_invalid"
        ready=asyncio.Event()
        shell=SimpleNamespace(target_run_runtime=runtime,owners=SimpleNamespace(
            agent_runtime=SimpleNamespace(list_target_root_work_refs=lambda:(state.handle.target_ref,))))
        worker=asyncio.create_task(_process_target_runs(shell,health,ready.set))
        try:
            await asyncio.wait_for(ready.wait(),timeout=2)
            assert health.status == "ready"
            assert health.last_error is None
            assert runtime.query_status(state.handle.target_ref).phase == "harness_suspended"
            assert state.events == []
        finally:
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(observe())


def test_owner_released_target_uses_same_handle_and_existing_result():
    runtime,state=_driver()
    assert runtime.process_once(state.handle.target_ref) is False
    original_identity=(state.admission.target_run_ref,state.admission.root_session_ref,
        state.admission.execution_attempt_ref,state.admission.execution_fence_ref)
    # The owner separately consumes the HumanRequest waiter. The driver never
    # performs this state change; this test supplies its next signed projection.
    state.admission=SimpleNamespace(**{**vars(state.admission),"status":"executed"})
    assert runtime.process_once(state.handle.target_ref) is True
    assert runtime.query_status(state.handle.target_ref).phase == "completed"
    assert state.events == ["input_binding","current_handle","materialize","finalize","publish"]
    assert original_identity == (state.admission.target_run_ref,state.admission.root_session_ref,
        state.admission.execution_attempt_ref,state.admission.execution_fence_ref)


@pytest.mark.parametrize("status",["completed","cancelled"])
def test_terminal_lifecycle_remains_terminal_while_harness_is_suspended(status):
    runtime,state=_driver(lifecycle_status=status)
    assert runtime.process_once(state.handle.target_ref) is False
    assert runtime.query_status(state.handle.target_ref).phase == status
    assert state.events == []


def test_operator_cancel_still_takes_precedence_over_suspended_wait():
    runtime,state=_driver(cancel=True)
    assert runtime.process_once(state.handle.target_ref) is True
    assert runtime.query_status(state.handle.target_ref).phase == "cancelled"
    assert state.events == ["cancel_root","cancelled"]


@pytest.mark.parametrize("error",["target_run_harness_identity_invalid","target_run_frontier_not_current"])
def test_executing_target_identity_failures_are_not_hidden_as_waiting(error):
    runtime,state=_driver(status="executed")
    state.error=error
    with pytest.raises(OwnerConflict,match=error):
        runtime.process_once(state.handle.target_ref)
    assert state.events == ["input_binding","current_handle"]


def test_invalid_harness_admission_is_not_hidden_as_waiting():
    runtime,state=_driver()
    def invalid(_target):
        raise OwnerConflict("target_harness_admission_integrity_invalid")
    runtime._target_agent.query_target_harness_admission=invalid
    with pytest.raises(OwnerConflict,match="target_harness_admission_integrity_invalid"):
        runtime.process_once(state.handle.target_ref)
    assert state.events == []
