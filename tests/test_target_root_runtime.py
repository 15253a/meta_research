from __future__ import annotations

from types import SimpleNamespace

from meta_research.harness import HarnessAdmissionError
from meta_research.owners.agent_runtime_harness import (
    TARGET_ROOT_RECOVERY_READY_CODE,
)
from meta_research.target_run_runtime import TargetRunRuntime
from test_target_run_owner import _records


def test_light_driver_resumes_one_root_until_one_final_publication() -> None:
    candidate, formal_plan, handle, _preflight, request = _records()
    launch = SimpleNamespace(
        launch_ref="target-launch-root",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        graph_ref="target-graph-root",
        request=request,
    )

    class AgentRuntime:
        publications: list[dict[str, str]] = []

        def query_admitted_target_launch(self, target_ref: str):
            assert target_ref == handle.target_ref
            return launch

        def publish_target_root_completion(self, **values: str):
            self.publications.append(values)
            lifecycle.mark_completed(
                target_ref=values["target_ref"],
                completion_ref=values["completion_ref"],
            )
            return SimpleNamespace(terminal="issuer-derived")

    class ResearchGraph:
        def query_target_candidate_projection(self, *, target_ref: str):
            assert target_ref == handle.target_ref
            return SimpleNamespace(candidate=candidate)

        def query_target_formal_plan_projection(self, *, graph_ref: str):
            assert graph_ref == launch.graph_ref
            return SimpleNamespace(formal_plan=formal_plan)

    class TargetGraph:
        binding: object | None = None

        def query_execution_input_binding_for_attempt(self, **_values):
            return self.binding

        def accept_execution_input_binding(self, **values):
            assert values["target_ref"] == handle.target_ref
            self.binding = SimpleNamespace(binding_ref="root-input-binding")
            return self.binding

    expected_handle = handle

    class TargetAgent:
        harness: object | None = None
        workspace: object | None = None
        materialize_calls = 0

        def query_target_harness_admission(self, target_ref: str):
            assert target_ref == handle.target_ref
            return self.harness

        def query_current_target_work_handle(self, target_ref: str):
            assert target_ref == handle.target_ref
            return handle

        def query_target_workspace(self, target_run_ref: str):
            assert target_run_ref == handle.target_run_ref
            return self.workspace

        def reserve_target_workspace(self, **values):
            assert values["handle"] == handle
            self.workspace = SimpleNamespace(workspace_ref="target-workspace-root")
            return self.workspace

        def materialize_target_workspace_inputs(self, *, handle: object):
            assert handle == expected_handle
            self.materialize_calls += 1
            return ("0001-input.json",)

    target_agent = TargetAgent()

    class Lifecycle:
        record: object | None = None
        completed: list[tuple[str, str]] = []

        def query(self, target_ref: str):
            assert target_ref == handle.target_ref
            return self.record

        def activate(self, **values):
            assert values["handle"] == handle
            self.record = SimpleNamespace(status="running")
            return self.record

        def mark_completed(self, *, target_ref: str, completion_ref: str):
            self.completed.append((target_ref, completion_ref))
            self.record = SimpleNamespace(status="completed")

    lifecycle = Lifecycle()

    class Harness:
        root_calls: list[tuple[str, dict[str, str]]] = []
        evidence: object | None = None

        def admit_target_run_from_current_conformance(self, **values):
            assert values["target_ref"] == handle.target_ref
            target_agent.harness = SimpleNamespace(
                target_run_ref=handle.target_run_ref,
                execution_attempt_ref=handle.execution_attempt_ref,
                execution_fence_ref=handle.execution_fence_ref,
                harness_request_ref="target-harness-request-root",
            )

        def query_target_root_completion_evidence(self, target_ref: str):
            assert target_ref == handle.target_ref
            return self.evidence

        def run_or_resume_target_root(self, request_ref: str, **values: str):
            assert request_ref == "target-harness-request-root"
            assert set(values) == {"prompt", "mcp_base_url"}
            self.root_calls.append((request_ref, values))
            if len(self.root_calls) == 2:
                self.evidence = SimpleNamespace(evidence_ref="root-evidence")
            return SimpleNamespace(status="executed")

    harnesses = Harness()

    expected_handle = handle

    class Finalizer:
        calls: list[tuple[object, object]] = []
        materialize_calls = 0

        def materialize_inputs(self, *, handle: object):
            assert handle == expected_handle
            self.materialize_calls += 1
            return (
                "/var/lib/meta-research/target-frozen-inputs/manifest.json",
            )

        def finalize(self, *, handle: object, evidence: object):
            self.calls.append((handle, evidence))
            return SimpleNamespace(
                status="completed",
                pending_code=None,
                completion_ref="target-root-completion",
                target_commit_ref="target-root-commit",
            )

    agent_runtime = AgentRuntime()
    target_graph = TargetGraph()
    finalizer = Finalizer()
    runtime = TargetRunRuntime(
        agent_runtime=agent_runtime,  # type: ignore[arg-type]
        research_graph=ResearchGraph(),  # type: ignore[arg-type]
        target_graph=target_graph,  # type: ignore[arg-type]
        target_agent=target_agent,  # type: ignore[arg-type]
        target_root_lifecycle=lifecycle,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        finalizer=finalizer,
    )
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")

    expected_phases = (
        "harness_admitted",
        "input_scope_bound",
        "workspace_reserved",
        "root_activated",
        "root_turn_completed",
        "completed",
    )
    for expected_phase in expected_phases:
        assert runtime.process_once(handle.target_ref) is True
        assert runtime.query_status(handle.target_ref).phase == expected_phase  # type: ignore[union-attr]

    assert len(harnesses.root_calls) == 2
    assert all(
        "own the entire loop" in values["prompt"]
        and "repeat as many times" in values["prompt"]
        and "spawn a focused child agent" in values["prompt"]
        and "you remain responsible" in values["prompt"]
        and "/var/lib/meta-research/target-frozen-inputs/manifest.json"
        in values["prompt"]
        for _request_ref, values in harnesses.root_calls
    )
    assert finalizer.calls == [(handle, harnesses.evidence)]
    assert finalizer.materialize_calls == 3
    assert agent_runtime.publications == [
        {
            "target_ref": handle.target_ref,
            "completion_ref": "target-root-completion",
            "target_commit_ref": "target-root-commit",
        }
    ]
    assert lifecycle.completed == [
        (handle.target_ref, "target-root-completion")
    ]
    assert runtime.process_once(handle.target_ref) is False
    assert len(harnesses.root_calls) == 2


def test_recoverable_owner_rejection_resumes_same_root_with_exact_feedback() -> None:
    candidate, formal_plan, handle, _preflight, request = _records()
    launch = SimpleNamespace(
        launch_ref="target-launch-revision",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        graph_ref="target-graph-revision",
        request=request,
    )
    old_evidence = SimpleNamespace(
        evidence_ref="root-evidence-generation-1",
        operation_generation=7,
    )
    successor_evidence = SimpleNamespace(
        evidence_ref="root-evidence-generation-2",
        operation_generation=8,
    )

    class AgentRuntime:
        publications: list[dict[str, str]] = []

        def query_admitted_target_launch(self, _target_ref: str):
            return launch

        def publish_target_root_completion(self, **values: str):
            self.publications.append(values)
            lifecycle.record = SimpleNamespace(status="completed")

    class ResearchGraph:
        def query_target_candidate_projection(self, **_values):
            return SimpleNamespace(candidate=candidate)

        def query_target_formal_plan_projection(self, **_values):
            return SimpleNamespace(formal_plan=formal_plan)

    class TargetGraph:
        def query_execution_input_binding_for_attempt(self, **_values):
            return SimpleNamespace(binding_ref="target-input-revision")

    class TargetAgent:
        admission = SimpleNamespace(
            target_run_ref=handle.target_run_ref,
            execution_attempt_ref=handle.execution_attempt_ref,
            execution_fence_ref=handle.execution_fence_ref,
            harness_request_ref="target-harness-request-revision",
        )

        def query_target_harness_admission(self, _target_ref: str):
            return self.admission

        def query_current_target_work_handle(self, _target_ref: str):
            return handle

        def query_target_workspace(self, _target_run_ref: str):
            return SimpleNamespace(workspace_ref="target-workspace-revision")

    class Lifecycle:
        record = SimpleNamespace(status="running")

        def query(self, _target_ref: str):
            return self.record

    lifecycle = Lifecycle()

    class Harness:
        evidence = old_evidence
        resume_prompts: list[str] = []

        def query_target_root_completion_evidence(self, _target_ref: str):
            return self.evidence

        def run_or_resume_target_root(
            self, request_ref: str, *, prompt: str, mcp_base_url: str
        ):
            assert request_ref == "target-harness-request-revision"
            assert mcp_base_url == "http://127.0.0.1:8765"
            self.resume_prompts.append(prompt)
            self.evidence = successor_evidence
            return SimpleNamespace(status="executed")

    harnesses = Harness()

    class Finalizer:
        calls: list[object] = []

        def materialize_inputs(self, *, handle: object):
            return ()

        def finalize(self, *, handle: object, evidence: object):
            self.calls.append(evidence)
            if evidence is old_evidence:
                return SimpleNamespace(
                    status="revision_required",
                    pending_code="target_root_result_schema_rejected",
                    completion_ref="target-root-completion-generation-1",
                    manifest_ref=None,
                    target_commit_ref=None,
                    completion_generation=1,
                    rejection_ref="rg-target-root-rejection-generation-1",
                    rejection_issuer="research_graph",
                    rejection_feedback=(
                        "Result document omitted the preregistered metric key."
                    ),
                )
            return SimpleNamespace(
                status="completed",
                pending_code=None,
                completion_ref="target-root-completion-generation-2",
                manifest_ref="target-root-manifest-generation-2",
                target_commit_ref="target-root-commit",
                completion_generation=2,
                rejection_ref=None,
                rejection_issuer=None,
                rejection_feedback=None,
            )

    finalizer = Finalizer()
    agent_runtime = AgentRuntime()
    runtime = TargetRunRuntime(
        agent_runtime=agent_runtime,  # type: ignore[arg-type]
        research_graph=ResearchGraph(),  # type: ignore[arg-type]
        target_graph=TargetGraph(),  # type: ignore[arg-type]
        target_agent=TargetAgent(),  # type: ignore[arg-type]
        target_root_lifecycle=lifecycle,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        finalizer=finalizer,
    )
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")

    assert runtime.process_once(handle.target_ref) is True
    status = runtime.query_status(handle.target_ref)
    assert status is not None and status.phase == "root_revision_turn_completed"
    assert len(harnesses.resume_prompts) == 1
    assert "rg-target-root-rejection-generation-1" in harnesses.resume_prompts[0]
    assert "Result document omitted the preregistered metric key." in (
        harnesses.resume_prompts[0]
    )
    assert '"manifest_ref":null' in harnesses.resume_prompts[0]
    assert handle.target_run_ref in harnesses.resume_prompts[0]
    assert finalizer.calls == [old_evidence]

    assert runtime.process_once(handle.target_ref) is True
    assert finalizer.calls == [old_evidence, successor_evidence]
    assert agent_runtime.publications == [
        {
            "target_ref": handle.target_ref,
            "completion_ref": "target-root-completion-generation-2",
            "target_commit_ref": "target-root-commit",
        }
    ]


def test_failed_harness_tick_only_recovers_then_next_ticks_finish_same_root() -> None:
    candidate, formal_plan, handle, _preflight, request = _records()
    launch = SimpleNamespace(
        launch_ref="target-launch-failed-root",
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        graph_ref="target-graph-failed-root",
        request=request,
    )

    class AgentRuntime:
        publications: list[dict[str, str]] = []

        def query_admitted_target_launch(self, _target_ref: str):
            return launch

        def publish_target_root_completion(self, **values: str):
            self.publications.append(values)
            lifecycle.record = SimpleNamespace(status="completed")

    class ResearchGraph:
        def query_target_candidate_projection(self, **_values):
            return SimpleNamespace(candidate=candidate)

        def query_target_formal_plan_projection(self, **_values):
            return SimpleNamespace(formal_plan=formal_plan)

    class TargetGraph:
        query_calls = 0

        def query_execution_input_binding_for_attempt(self, **_values):
            self.query_calls += 1
            return SimpleNamespace(binding_ref="target-input-failed-root")

    class TargetAgent:
        admission = SimpleNamespace(
            target_ref=handle.target_ref,
            target_run_ref=handle.target_run_ref,
            harness_request_ref="target-harness-request-failed-root",
            execution_attempt_ref=handle.execution_attempt_ref,
            execution_fence_ref=handle.execution_fence_ref,
            native_session_ref="native-target-root-stable",
            status="failed",
            failure_code="provider_process_failed",
        )

        def query_target_harness_admission(self, _target_ref: str):
            return self.admission

        def query_current_target_work_handle(self, _target_ref: str):
            return handle

        def query_target_workspace(self, _target_run_ref: str):
            return SimpleNamespace(workspace_ref="target-workspace-failed-root")

    target_agent = TargetAgent()

    class Lifecycle:
        record = SimpleNamespace(status="running")

        def query(self, _target_ref: str):
            return self.record

    lifecycle = Lifecycle()

    evidence = SimpleNamespace(evidence_ref="target-root-evidence-after-recovery")

    class Harness:
        recovery_calls: list[str] = []
        root_calls: list[str] = []
        completion: object | None = None

        def recover_failed_target_root(self, request_ref: str):
            self.recovery_calls.append(request_ref)
            if len(self.recovery_calls) == 1:
                target_agent.admission = SimpleNamespace(
                    **{
                        **vars(target_agent.admission),
                        "status": "executed",
                        "failure_code": "target_root_recovery_pending",
                    }
                )
                raise HarnessAdmissionError(
                    "mcp_channel_temporarily_unavailable",
                    next_retry_at=123.0,
                )
            target_agent.admission = SimpleNamespace(
                **{
                    **vars(target_agent.admission),
                    "status": "executed",
                    "failure_code": TARGET_ROOT_RECOVERY_READY_CODE,
                }
            )
            return SimpleNamespace(run=SimpleNamespace(status="executed"))

        def query_target_root_completion_evidence(self, _target_ref: str):
            return self.completion

        def run_or_resume_target_root(
            self, request_ref: str, *, prompt: str, mcp_base_url: str
        ):
            assert request_ref == "target-harness-request-failed-root"
            assert prompt and mcp_base_url == "http://127.0.0.1:8765"
            self.root_calls.append(request_ref)
            if len(self.root_calls) == 1:
                self.completion = evidence
            return SimpleNamespace(status="executed")

    harnesses = Harness()

    expected_handle = handle

    class Finalizer:
        materialize_calls = 0
        finalize_calls: list[object] = []

        def materialize_inputs(self, *, handle: object):
            assert handle == expected_handle
            self.materialize_calls += 1
            return ()

        def finalize(self, *, handle: object, evidence: object):
            assert handle == expected_handle
            self.finalize_calls.append(evidence)
            return SimpleNamespace(
                status="completed",
                pending_code=None,
                completion_ref="target-root-completion-after-recovery",
                target_commit_ref="target-root-commit-after-recovery",
            )

    agent_runtime = AgentRuntime()
    target_graph = TargetGraph()
    finalizer = Finalizer()
    runtime = TargetRunRuntime(
        agent_runtime=agent_runtime,  # type: ignore[arg-type]
        research_graph=ResearchGraph(),  # type: ignore[arg-type]
        target_graph=target_graph,  # type: ignore[arg-type]
        target_agent=target_agent,  # type: ignore[arg-type]
        target_root_lifecycle=lifecycle,  # type: ignore[arg-type]
        harnesses=harnesses,  # type: ignore[arg-type]
        finalizer=finalizer,
    )
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")

    assert runtime.process_once(handle.target_ref) is False
    status = runtime.query_status(handle.target_ref)
    assert status is not None
    assert status.phase == "harness_recovery_pending"
    assert status.pending_code == "mcp_channel_temporarily_unavailable"
    assert status.next_retry_at == 123.0
    assert harnesses.recovery_calls == ["target-harness-request-failed-root"]
    assert harnesses.root_calls == []
    assert target_graph.query_calls == 0
    assert finalizer.materialize_calls == 0
    assert finalizer.finalize_calls == []

    assert target_agent.admission.status == "executed"
    assert target_agent.admission.failure_code == "target_root_recovery_pending"

    assert runtime.process_once(handle.target_ref) is True
    assert runtime.query_status(handle.target_ref).phase == "harness_recovered"  # type: ignore[union-attr]
    assert harnesses.recovery_calls == [
        "target-harness-request-failed-root",
        "target-harness-request-failed-root",
    ]
    assert harnesses.root_calls == []

    assert runtime.process_once(handle.target_ref) is True
    assert runtime.query_status(handle.target_ref).phase == "completed"  # type: ignore[union-attr]
    assert harnesses.root_calls == ["target-harness-request-failed-root"]
    assert finalizer.finalize_calls == [evidence]
    assert agent_runtime.publications == [
        {
            "target_ref": handle.target_ref,
            "completion_ref": "target-root-completion-after-recovery",
            "target_commit_ref": "target-root-commit-after-recovery",
        }
    ]
