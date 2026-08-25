from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_runtime import TargetRunRuntime
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)
from test_target_run_owner import _records


def _running_root(tmp_path: Path):
    runtime = _current_bundle_runtime(tmp_path / "target-root-cancel")
    target, candidate, formal_plan, _admission, handle = (
        _admit_independent_target_root(runtime)
    )
    launch = runtime.owners.agent_runtime.query_admitted_target_launch(
        target.target_ref
    )
    assert launch is not None
    lifecycle = runtime.target_root_lifecycle.activate(
        launch_ref=launch.launch_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=formal_plan,
        idempotency_key="activate-cancellable-root",
    )
    return runtime, handle, lifecycle


def test_cancel_intent_is_durable_idempotent_and_only_for_running_root(
    tmp_path: Path,
) -> None:
    runtime, handle, initial = _running_root(tmp_path)
    try:
        requested = runtime.target_root_lifecycle.request_cancel(
            handle.target_ref,
            reason="operator requested stop",
        )
        replay = runtime.target_root_lifecycle.request_cancel(
            handle.target_ref,
            reason="operator requested stop",
        )

        assert requested == replay
        assert requested.status == "running"
        assert requested.cancel_ref is not None
        assert requested.cancel_requested_at is not None
        assert requested.cancel_reason == "operator requested stop"
        assert requested.root_session_ref == initial.root_session_ref
        assert requested.target_attempt_ref == initial.target_attempt_ref
        assert requested.target_fence_ref == initial.target_fence_ref

        with pytest.raises(
            OwnerConflict, match="target_root_cancel_reason_conflict"
        ):
            runtime.target_root_lifecycle.request_cancel(
                handle.target_ref,
                reason="a different reason",
            )

        # A fresh authority instance reads the same persisted intent.
        reloaded = runtime.target_root_lifecycle.query(handle.target_ref)
        assert reloaded == requested
    finally:
        runtime.close()


def test_mechanical_cancel_ack_terminalizes_same_root_without_formal_handoff(
    tmp_path: Path,
) -> None:
    runtime, handle, initial = _running_root(tmp_path)
    try:
        requested = runtime.target_root_lifecycle.request_cancel(handle.target_ref)
        cancelled = runtime.target_root_lifecycle.mark_cancelled(
            target_ref=handle.target_ref
        )
        replay = runtime.target_root_lifecycle.mark_cancelled(
            target_ref=handle.target_ref
        )

        assert cancelled == replay
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_ref == requested.cancel_ref
        assert cancelled.cancelled_at is not None
        assert cancelled.root_session_ref == initial.root_session_ref
        assert cancelled.target_attempt_ref == initial.target_attempt_ref
        assert cancelled.target_fence_ref == initial.target_fence_ref

        frontier = runtime.owners.agent_runtime.query_target_frontier_entry(
            handle.target_ref
        )
        assert frontier is not None
        assert frontier.state == "terminal"
        assert frontier.terminal_fact_ref == cancelled.cancel_ref
        assert handle.target_ref not in (
            runtime.owners.agent_runtime.list_target_root_work_refs()
        )

        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_target_root_completions WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM rm_target_root_completion_manifests "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM rg_target_root_measurements WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).scalar_one() == 0

        with pytest.raises(OwnerConflict, match="target_root_cancel_not_running"):
            runtime.target_root_lifecycle.request_cancel(
                handle.target_ref,
                reason="too late",
            )
    finally:
        runtime.close()


def test_root_runtime_waits_for_harness_cancel_ack_before_terminalizing() -> None:
    candidate, formal_plan, handle, _preflight, request = _records()
    launch = SimpleNamespace(
        launch_ref="target-launch-cancel",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        graph_ref="target-graph-cancel",
        request=request,
    )

    class AgentRuntime:
        publications: list[object] = []

        def query_admitted_target_launch(self, target_ref: str):
            assert target_ref == handle.target_ref
            return launch

        def publish_target_root_completion(self, **values):
            self.publications.append(values)

    class ResearchGraph:
        def query_target_candidate_projection(self, *, target_ref: str):
            assert target_ref == handle.target_ref
            return SimpleNamespace(candidate=candidate)

        def query_target_formal_plan_projection(self, *, graph_ref: str):
            assert graph_ref == launch.graph_ref
            return SimpleNamespace(formal_plan=formal_plan)

    class TargetGraph:
        def query_execution_input_binding_for_attempt(self, **_values):
            return SimpleNamespace(binding_ref="root-input-binding")

    class TargetAgent:
        def query_target_harness_admission(self, target_ref: str):
            assert target_ref == handle.target_ref
            return SimpleNamespace(
                target_run_ref=handle.target_run_ref,
                execution_attempt_ref=handle.execution_attempt_ref,
                execution_fence_ref=handle.execution_fence_ref,
                harness_request_ref="target-harness-request-cancel",
                status="failed",
                failure_code="provider_process_failed",
            )

        def query_current_target_work_handle(self, target_ref: str):
            assert target_ref == handle.target_ref
            return handle

        def query_target_workspace(self, target_run_ref: str):
            assert target_run_ref == handle.target_run_ref
            return SimpleNamespace(workspace_ref="target-workspace-cancel")

    class Lifecycle:
        record = SimpleNamespace(
            status="running",
            cancel_ref="target-root-cancel",
            cancel_requested_at=123.0,
        )
        marks = 0

        def query(self, target_ref: str):
            assert target_ref == handle.target_ref
            return self.record

        def mark_cancelled(self, *, target_ref: str):
            assert target_ref == handle.target_ref
            self.marks += 1
            self.record = SimpleNamespace(
                status="cancelled",
                cancel_ref="target-root-cancel",
                cancel_requested_at=123.0,
            )
            return self.record

    lifecycle = Lifecycle()

    class Harness:
        cancel_results = iter((False, True))
        cancel_calls: list[str] = []
        completion_queries = 0

        def recover_failed_target_root(self, _request_ref: str):
            raise AssertionError("cancel must bypass failed-root recovery")

        def cancel_target_root(self, request_ref: str) -> bool:
            self.cancel_calls.append(request_ref)
            return next(self.cancel_results)

        def query_target_root_completion_evidence(self, target_ref: str):
            self.completion_queries += 1
            return SimpleNamespace(evidence_ref="must-not-finalize")

    harnesses = Harness()

    class Finalizer:
        finalize_calls = 0

        def materialize_inputs(self, *, handle: object):
            return ("/frozen/manifest.json",)

        def finalize(self, **_values):
            self.finalize_calls += 1
            raise AssertionError("cancelled roots cannot enter RM/RG finalization")

    finalizer = Finalizer()
    runtime = TargetRunRuntime(
        agent_runtime=AgentRuntime(),  # type: ignore[arg-type]
        research_graph=ResearchGraph(),  # type: ignore[arg-type]
        target_graph=TargetGraph(),  # type: ignore[arg-type]
        target_agent=TargetAgent(),  # type: ignore[arg-type]
        target_root_lifecycle=lifecycle,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        finalizer=finalizer,
    )

    assert runtime.process_once(handle.target_ref) is False
    pending = runtime.query_status(handle.target_ref)
    assert pending is not None
    assert pending.phase == "cancel_pending"
    assert pending.pending_code is None
    assert lifecycle.marks == 0

    assert runtime.process_once(handle.target_ref) is True
    cancelled = runtime.query_status(handle.target_ref)
    assert cancelled is not None
    assert cancelled.phase == "cancelled"
    assert cancelled.pending_code is None
    assert lifecycle.marks == 1

    assert runtime.process_once(handle.target_ref) is False
    assert harnesses.cancel_calls == [
        "target-harness-request-cancel",
        "target-harness-request-cancel",
    ]
    assert harnesses.completion_queries == 0
    assert finalizer.finalize_calls == 0
