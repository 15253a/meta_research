from dataclasses import replace

import pytest

from meta_research.harness import HarnessAdmissionError
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)


def _row(runtime, target_ref):
    return next(row for row in runtime.bundle_stage.query_current()["target_graph"]["targets"]
                if row["target_ref"] == target_ref)


def _activate(runtime, ready):
    target, candidate, plan, admission, handle = _admit_independent_target_root(runtime, ready=ready)
    launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
    runtime.target_root_lifecycle.activate(
        launch_ref=launch.launch_ref, handle=handle, candidate=candidate,
        formal_plan=plan, idempotency_key="activate-execution-projection",
    )
    return target, admission, handle


def test_bundle_execution_ends_without_accepting_a_scientific_result(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "execution-projection")
    try:
        ready = _ready_launch(runtime)
        assert all(row["current_execution"] is None for row in
                   runtime.bundle_stage.query_current()["target_graph"]["targets"])
        target, admission, handle = _activate(runtime, ready)
        pending = _row(runtime, target.target_ref)
        assert pending["status"] == "running"
        assert pending["current_execution"]["status"] == "pending"
        assert pending["current_execution"]["root_session_ref"] == handle.root_session_ref

        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:1")
        runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref, prompt="Bounded deterministic execution observation.",
            mcp_base_url="http://127.0.0.1:1",
        )
        ended = _row(runtime, target.target_ref)
        assert ended["status"] == "running"  # Research acceptance is independent.
        assert ended["current_execution"]["status"] == "completed"
        assert ended["current_execution"]["attempt_ref"] == handle.execution_attempt_ref
        assert ended["current_execution"]["fence_ref"] == handle.execution_fence_ref

        runtime.target_root_lifecycle.request_cancel(target.target_ref, "Execution has ended; no research result submitted.")
        assert runtime.harnesses.cancel_target_root(admission.run.request_ref)
        runtime.target_root_lifecycle.mark_cancelled(target_ref=target.target_ref)
        stopped = _row(runtime, target.target_ref)
        assert stopped["status"] == "running"
        assert stopped["current_execution"]["status"] == "stopped"
        assert stopped["current_execution"]["root_session_ref"] == handle.root_session_ref
        assert runtime.target_root_lifecycle.query(target.target_ref).completion_ref is None
        assert runtime.bundle_stage.query_current()["target_commits"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize("changed", ["target_ref", "target_run_ref", "root_session_ref", "attempt_ref", "fence_ref"])
def test_bundle_execution_never_attaches_another_root_observation(tmp_path, monkeypatch, changed):
    runtime = _current_bundle_runtime(tmp_path / changed)
    try:
        target, _admission, handle = _activate(runtime, _ready_launch(runtime))
        observed = runtime.harnesses.query_target_root_observations(target.target_ref, limit=1)
        foreign = replace(observed, **{changed: "foreign-observation", "status": "live"})
        monkeypatch.setattr(runtime.harnesses, "query_target_root_observations", lambda *_args, **_kwargs: foreign)
        execution = _row(runtime, target.target_ref)["current_execution"]
        assert execution["status"] == "unavailable"
        assert execution["target_ref"] == target.target_ref
        assert execution["target_run_ref"] == handle.target_run_ref
        assert execution["root_session_ref"] == handle.root_session_ref
    finally:
        runtime.close()


def test_bundle_execution_unavailable_observation_preserves_research_projection(tmp_path, monkeypatch):
    runtime = _current_bundle_runtime(tmp_path / "unavailable")
    try:
        target, _admission, _handle = _activate(runtime, _ready_launch(runtime))
        def unavailable(*_args, **_kwargs):
            raise HarnessAdmissionError("target_root_observation_target_not_found")
        monkeypatch.setattr(runtime.harnesses, "query_target_root_observations", unavailable)
        row = _row(runtime, target.target_ref)
        assert row["status"] == "running"
        assert row["current_execution"]["status"] == "unavailable"
        assert row["blocker"] is None
    finally:
        runtime.close()
