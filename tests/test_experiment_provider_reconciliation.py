from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.experiment import BuiltinMicroExperimentProvider, ExperimentIntent
from meta_research.experiment_contract import (
    AcceptedExperimentInputBinding,
    ExperimentIdentitySet,
    ExperimentProviderRequest,
)
from meta_research.experiment_provider_supervisor import (
    OBSERVATION_MAX_COUNT,
    STDOUT_MAX_RECORDS,
    _ObservationLedger,
    _bounded_drain,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
)
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.web import create_app


_QUESTION = {
    "title": "实验 provider 能否在 daemon replacement 后安全对账",
    "unknown_statement": "尚不明确 durable provider outcome 是否会被重复执行。",
    "answer_shape": "形成一份可复核且只执行一次的完整测量。",
    "applicability_scope": "本机内置微型数值实验。",
    "background_context": "验证 provider operation 与 AR Attempt 身份的边界。",
    "requirements_constraints": "技术恢复不得创建新领域身份或重复 subprocess。",
}


class _Drafting:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        del request
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复",
            request.native_session_ref or "intent-session",
            "test_deterministic",
        )


class _Probe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_720_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-provider-test",
                    name="Provider Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


def _runtime(root: Path, provider: BuiltinMicroExperimentProvider):
    drafting = _Drafting()
    return build_production_runtime(
        prepare_data_root(root),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_Probe(),
        experiment_provider=provider,
    )


def _confirm_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "provider-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-provider-test"],
        "provider-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "验证实验 provider 的 durable reconciliation 与安全上限。",
            "completion_criteria": "同一 provider operation 只启动一次。",
            "time_budget": "7d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较固定基线与数值偏移变体。",
        }
    )
    human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "provider-quest-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        "provider-proposal",
        drafted["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="provider-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="provider-confirm",
    )
    for _step in range(6):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def _intent(quest_ref: str, suffix: str) -> ExperimentIntent:
    return ExperimentIntent(
        execution_request_ref=f"provider-request-{suffix}",
        quest_ref=quest_ref,
        title=f"durable provider {suffix}",
        hypothesis="数值偏移会改变均值；结果符号不决定接纳。",
        variant_parameter=-0.25,
        sample_count=16,
    )


def _resume_managed_experiment(runtime, *, quest_ref: str, run_ref: str) -> None:
    foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
    assert foreground is not None
    managed = runtime.owners.agent_runtime.query_managed_run(run_ref)
    assert managed is not None
    resume_key = "resume-experiment:" + hashlib.sha256(
        f"{run_ref}:{managed['attempt_ref']}".encode("utf-8")
    ).hexdigest()[:32]
    payload = {
        "action": "resume",
        "target": {
            "target_scope": "run",
            "quest_ref": quest_ref,
            "cycle_ref": foreground["cycle_ref"],
            "question_ref": foreground["question_ref"],
            "epoch": foreground["epoch"],
            "run_ref": run_ref,
        },
        "reason": "operator_requested",
    }
    human = runtime.owners.human_collaboration
    drafted = human.create_command_draft(
        f"quest:{quest_ref}",
        {"command_kind": "research_control", "payload": payload},
        f"{resume_key}:draft",
    )
    previewed = human.preview_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        f"{resume_key}:preview",
    )
    preview = previewed["impact_preview"]
    assert preview is not None
    confirmed = human.confirm_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        preview["preview_ref"],
        preview["preview_hash"],
        f"{resume_key}:confirm",
    )
    human.execute_confirmed_command(
        confirmed["intent_id"],
        confirmed["confirmation_receipt"]["receipt_ref"],
        f"{resume_key}:execute",
    )


def _provider_request(runtime, run) -> ExperimentProviderRequest:
    domain = runtime.owners.research_graph.query_experiment(
        run.evaluation_attempt_ref
    )
    assert domain is not None
    return ExperimentProviderRequest(
        provider_operation_ref=run.provider_operation_ref,
        identities=domain.identities,
        variant_run_binding=domain.variant_run_binding,
        evaluation_attempt_binding=domain.evaluation_attempt_binding,
        required_metrics=domain.required_metrics,
    )


def _direct_provider_request(operation_ref: str) -> ExperimentProviderRequest:
    identities = ExperimentIdentitySet(
        baseline_ref="baseline-provider-test",
        variant_ref="variant-provider-test",
        evaluation_protocol_ref="protocol-provider-test",
        protocol_version_ref="protocol-version-provider-test",
        evaluation_ref="evaluation-provider-test",
        variant_run_ref="variant-run-provider-test",
        evaluation_attempt_ref="evaluation-attempt-provider-test",
    )

    def binding(subject_kind: str, subject_ref: str, inputs: dict[str, object]):
        binding_ref = f"binding-{subject_kind}-provider-test"
        return AcceptedExperimentInputBinding(
            binding_ref=binding_ref,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            inputs=inputs,
            inputs_hash=canonical_hash(inputs),
            receipt=AcceptanceReceipt(
                issuer="research_graph",
                kind="experiment_input_binding_acceptance",
                receipt_ref=f"receipt-{subject_kind}-provider-test",
                subject_ref=binding_ref,
                payload_hash=canonical_hash(
                    {"subject_kind": subject_kind, "subject_ref": subject_ref}
                ),
            ),
        )

    variant = binding(
        "variant_run",
        identities.variant_run_ref,
        {
            "data": {"sample_count": 16},
            "recipe": {"variant_parameter": -0.25},
        },
    )
    measurement = binding(
        "evaluation_attempt",
        identities.evaluation_attempt_ref,
        {
            "evaluation_data": {"sample_count": 16},
            "metrics": ["baseline_mean", "variant_mean", "mean_delta"],
        },
    )
    return ExperimentProviderRequest(
        provider_operation_ref=operation_ref,
        identities=identities,
        variant_run_binding=variant,
        evaluation_attempt_binding=measurement,
        required_metrics=("baseline_mean", "variant_mean", "mean_delta"),
    )


def _valid_result_document(*, padding: int = 0) -> str:
    import json

    return json.dumps(
        {
            "checkpoint": {"weights": [-0.25]},
            "analysis": {
                "direction": "negative",
                "padding": "x" * padding,
            },
            "result_content": {
                "schema_ref": "meta-research/micro-experiment-result/v1",
                "metrics": {
                    "baseline_mean": 0.0,
                    "variant_mean": -0.25,
                    "mean_delta": -0.25,
                },
                "aggregation": "single fixed sample set; arithmetic mean",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_runner(
    path: Path,
    *,
    mode: str = "success",
    child_pid_path: Path | None = None,
    event_time_path: Path | None = None,
) -> Path:
    import json

    result = repr(_valid_result_document(padding=5000 if mode == "result_limit" else 0))
    counter = path.with_suffix(".count")
    child_pid = "None" if child_pid_path is None else repr(str(child_pid_path))
    event_time = "None" if event_time_path is None else repr(str(event_time_path))
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))\n"
        "sys.stdin.buffer.read()\n"
        f"mode = {mode!r}\n"
        f"child_pid_path = {child_pid}\n"
        f"event_time_path = {event_time}\n"
        f"result = {result}\n"
        "if mode == 'fail_once' and int(counter.read_text()) == 1:\n"
        "    print('transient provider failure', flush=True)\n"
        "    raise SystemExit(7)\n"
        "elif mode == 'timeout':\n"
        "    time.sleep(5)\n"
        "elif mode == 'stdout_limit':\n"
        "    for _ in range(100):\n"
        "        print('x' * 256, flush=True)\n"
        "    time.sleep(5)\n"
        "elif mode == 'invalid_utf8':\n"
        "    sys.stdout.buffer.write(b'valid\\n\\xff\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "elif mode == 'result_limit':\n"
        "    print('state formation complete', flush=True)\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n"
        "    time.sleep(5)\n"
        "elif mode == 'descendant':\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    pathlib.Path(child_pid_path).write_text(str(child.pid))\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n"
        "elif mode == 'many_lines':\n"
        "    for index in range(300):\n"
        "        print(f'line-{index:03d}', flush=True)\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n"
        "elif mode == 'structural_capacity':\n"
        "    print('y' * (40 * 1024), flush=True)\n"
        "    print('x' * 8000, flush=True)\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n"
        "elif mode in {'active_stop', 'streaming'}:\n"
        "    if event_time_path is not None:\n"
        "        pathlib.Path(event_time_path).write_text(str(time.time()))\n"
        "    print('line-early', flush=True)\n"
        "    time.sleep(0.8)\n"
        "    print('line-late', flush=True)\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n"
        "else:\n"
        "    print('state formation complete', flush=True)\n"
        "    print('META_RESEARCH_RESULT\\t' + result, flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _assert_process_gone(process_id: int) -> None:
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"provider descendant {process_id} survived")
        time.sleep(0.01)


class _PendingOnceProvider(BuiltinMicroExperimentProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def execute(self, request, observe):
        self.calls += 1
        if self.calls == 1:
            raise OwnerConflict("experiment_provider_reconciliation_pending")
        return super().execute(request, observe)


class _PendingAfterObservationProvider(BuiltinMicroExperimentProvider):
    """Simulate daemon control loss after AR accepted a durable ledger row."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.interrupted = False

    def _remember_observation_progress(
        self,
        invocation_hash: str,
        *,
        cursor: int,
        next_sequence: int,
    ) -> None:
        super()._remember_observation_progress(
            invocation_hash,
            cursor=cursor,
            next_sequence=next_sequence,
        )
        if not self.interrupted and next_sequence >= 3:
            self.interrupted = True
            raise OwnerConflict("experiment_provider_reconciliation_pending")


class _UnverifiedTerminalClaimProvider(BuiltinMicroExperimentProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def execute(self, request, observe):
        del request, observe
        self.calls += 1
        raise OwnerConflict("experiment_provider_failed")


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        time.sleep(0.01)


def test_provider_terminal_spool_reconciles_after_lost_ar_ack_without_replay(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    provider_root = tmp_path / "provider"
    runner = _write_runner(tmp_path / "successful-runner")
    first_provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    first = _runtime(data_root, first_provider)
    quest = _confirm_quest(first)
    admitted = first.experiment.start(
        _intent(quest["quest_ref"], "lost-ack"),
        "provider-start-lost-ack",
    )
    identities = admitted["identities"]
    running = first.owners.agent_runtime.claim_next_experiment()
    assert running is not None
    old_attempt_ref = running.attempt_ref
    operation_ref = running.provider_operation_ref

    # The provider completed and sealed its outcome, but AR never committed the ACK.
    first_provider.execute(_provider_request(first, running), lambda _event: None)
    assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"
    assert first.query_runtime_observability()["inhibitor"]["active_count"] == 1
    first.close()

    restarted_provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    restarted = _runtime(data_root, restarted_provider)
    try:
        recovered = restarted.experiment.query(
            identities["evaluation_attempt_ref"]
        )
        assert recovered["identities"] == identities
        assert recovered["execution"]["attempt_generation"] == 2
        assert recovered["execution"]["attempt_ref"] != old_attempt_ref
        assert recovered["execution"]["provider_operation_ref"] == operation_ref

        for _step in range(3):
            assert restarted.experiment.process_once()
        assert not restarted.experiment.process_once()

        completed = restarted.experiment.query(
            identities["evaluation_attempt_ref"]
        )
        assert completed["identities"] == identities
        assert completed["execution"]["status"] == "executed"
        assert completed["execution"]["provider_operation_ref"] == operation_ref
        assert completed["formal_measurement"]["status"] == "accepted"
        assert any(
            event["kind"] == "stdout"
            for event in completed["execution"]["events"]
        )
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"
        assert restarted.query_runtime_observability()["inhibitor"][
            "active_count"
        ] == 0
    finally:
        restarted.close()


def test_graceful_runtime_stop_detaches_active_provider_and_restart_reconciles(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "active-stop-data"
    provider_root = tmp_path / "active-stop-provider"
    runner = _write_runner(tmp_path / "active-stop-runner", mode="active_stop")
    first_provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    first = _runtime(data_root, first_provider)
    quest = _confirm_quest(first)
    admitted = first.experiment.start(
        _intent(quest["quest_ref"], "active-stop"),
        "provider-start-active-stop",
    )
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            first.experiment.process_once()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    _wait_until(
        lambda: bool(
            list(
                provider_root.glob(
                    "provider-operations/*/provider-started.json"
                )
            )
        )
    )
    first.request_stop()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert errors == []
    interrupted = first.experiment.query(
        admitted["identities"]["evaluation_attempt_ref"]
    )
    assert interrupted["execution"]["status"] == "running"
    assert interrupted["execution"]["failure"] is None
    assert first.query_runtime_observability()["inhibitor"]["active_count"] == 1
    operation_ref = interrupted["execution"]["provider_operation_ref"]
    attempt_ref = interrupted["execution"]["attempt_ref"]
    first.close()

    restarted_provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    restarted = _runtime(data_root, restarted_provider)
    try:
        recovered = restarted.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert recovered["execution"]["attempt_generation"] == 2
        assert recovered["execution"]["attempt_ref"] != attempt_ref
        assert recovered["execution"]["provider_operation_ref"] == operation_ref
        for _step in range(3):
            assert restarted.experiment.process_once()
        completed = restarted.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert completed["formal_measurement"]["status"] == "accepted"
        assert completed["execution"]["provider_operation_ref"] == operation_ref
        assert {event["kind"] for event in completed["execution"]["events"]} >= {
            "stdout",
            "telemetry",
        }
        assert [
            event["payload"]["line"]
            for event in completed["execution"]["events"]
            if event["kind"] == "stdout"
        ] == ["line-early", "line-late"]
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"
        receipt_path = next(
            provider_root.glob("provider-operations/*/supervisor-exit.json")
        )
        import json

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["payload"]["termination_reason"] == "completed"
        assert restarted.query_runtime_observability()["inhibitor"][
            "active_count"
        ] == 0
    finally:
        restarted.close()


def test_running_provider_publishes_durable_true_timestamped_observations(
    tmp_path: Path,
) -> None:
    event_time_path = tmp_path / "early-emitted-at"
    runner = _write_runner(
        tmp_path / "streaming-runner",
        mode="streaming",
        event_time_path=event_time_path,
    )
    provider = BuiltinMicroExperimentProvider(
        tmp_path / "streaming-provider",
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    runtime = _runtime(tmp_path / "streaming-data", provider)
    quest = _confirm_quest(runtime)
    admitted = runtime.experiment.start(
        _intent(quest["quest_ref"], "streaming"),
        "provider-start-streaming",
    )
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            runtime.experiment.process_once()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        evaluation_attempt_ref = admitted["identities"][
            "evaluation_attempt_ref"
        ]

        def live_observations_exist() -> bool:
            events = runtime.experiment.query_events(evaluation_attempt_ref)
            kinds = {event["kind"] for event in events}
            return "stdout" in kinds and "telemetry" in kinds

        _wait_until(live_observations_exist, timeout=0.6)
        assert worker.is_alive()
        running = runtime.experiment.query(evaluation_attempt_ref)
        assert running["execution"]["status"] == "running"
        stdout = next(
            event
            for event in running["execution"]["events"]
            if event["kind"] == "stdout"
        )
        emitted_at = float(event_time_path.read_text(encoding="utf-8"))
        assert stdout["payload"]["line"] == "line-early"
        assert abs(float(stdout["observed_at"]) - emitted_at) < 0.25
        ledger = next(
            (tmp_path / "streaming-provider").glob(
                "provider-operations/*/observations.jsonl"
            )
        )
        assert ledger.stat().st_size > 0

        worker.join(timeout=3.0)
        assert not worker.is_alive()
        assert errors == []
        for _step in range(2):
            assert runtime.experiment.process_once()
        completed = runtime.experiment.query(evaluation_attempt_ref)
        assert completed["formal_measurement"]["status"] == "accepted"
    finally:
        runtime.request_stop()
        worker.join(timeout=2.0)
        runtime.close()


def test_web_projects_live_supervisor_observations_before_terminal_receipt(
    tmp_path: Path,
) -> None:
    event_time_path = tmp_path / "web-early-emitted-at"
    runner = _write_runner(
        tmp_path / "web-streaming-runner",
        mode="streaming",
        event_time_path=event_time_path,
    )
    provider_root = tmp_path / "web-streaming-provider"
    provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    runtime = _runtime(tmp_path / "web-streaming-data", provider)
    quest = _confirm_quest(runtime)
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    try:
        with client:
            bootstrap = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": base_url},
                json={"token": bootstrap},
            )
            assert authenticated.status_code == 200
            started = client.post(
                "/api/v1/experiments",
                headers={
                    "Origin": base_url,
                    "X-CSRF-Token": authenticated.json()["csrf_token"],
                    "Idempotency-Key": "web-live-provider-start",
                },
                json={
                    "execution_request_ref": "web-live-provider-request",
                    "quest_ref": quest["quest_ref"],
                    "title": "Web durable live provider observation",
                    "hypothesis": "terminal receipt 前可见真实 stdout 与 telemetry。",
                    "variant_parameter": -0.25,
                    "sample_count": 16,
                },
            )
            assert started.status_code == 201
            attempt_ref = started.json()["identities"]["evaluation_attempt_ref"]

            live: dict[str, object] | None = None
            deadline = time.monotonic() + 0.7
            while time.monotonic() < deadline:
                response = client.get("/api/v1/experiments/current")
                assert response.status_code == 200
                candidate = response.json()["current"]
                events = candidate["execution"]["events"]
                if {event["kind"] for event in events} >= {
                    "stdout",
                    "telemetry",
                }:
                    live = candidate
                    break
                time.sleep(0.01)
            assert live is not None
            assert live["execution"]["status"] == "running"
            assert not list(
                provider_root.glob("provider-operations/*/supervisor-exit.json")
            )
            stdout = next(
                event
                for event in live["execution"]["events"]
                if event["kind"] == "stdout"
            )
            assert stdout["payload"]["line"] == "line-early"
            assert abs(
                float(stdout["observed_at"])
                - float(event_time_path.read_text(encoding="utf-8"))
            ) < 0.25

            completed: dict[str, object] | None = None
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                completed_response = client.get(
                    f"/api/v1/experiments/{attempt_ref}"
                )
                if completed_response.status_code != 200:
                    time.sleep(0.02)
                    continue
                completed = completed_response.json()
                if completed["formal_measurement"]["status"] == "accepted":
                    break
                time.sleep(0.02)
            assert completed is not None
            assert completed["formal_measurement"]["status"] == "accepted"
    finally:
        runtime.close()


def test_reconciliation_pending_requeues_the_same_ar_attempt_without_failure(
    tmp_path: Path,
) -> None:
    runner = _write_runner(tmp_path / "pending-runner")
    provider = _PendingOnceProvider(
        tmp_path / "pending-provider",
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    runtime = _runtime(tmp_path / "pending-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "pending"),
            "provider-start-pending",
        )
        original = admitted["execution"]
        assert runtime.experiment.process_once()
        deferred = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert deferred["execution"]["status"] == "admitted"
        assert deferred["execution"]["failure"] is None
        assert deferred["execution"]["attempt_ref"] == original["attempt_ref"]
        assert deferred["execution"]["provider_operation_ref"] == (
            original["provider_operation_ref"]
        )
        for _step in range(3):
            assert runtime.experiment.process_once()
        completed = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert completed["formal_measurement"]["status"] == "accepted"
        assert completed["execution"]["attempt_ref"] == original["attempt_ref"]
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"
    finally:
        runtime.close()


def test_verified_terminal_failure_waits_for_explicit_resume_with_same_domain(
    tmp_path: Path,
) -> None:
    runner = _write_runner(tmp_path / "retry-runner", mode="fail_once")
    provider_root = tmp_path / "retry-provider"
    runtime = _runtime(
        tmp_path / "retry-data",
        BuiltinMicroExperimentProvider(
            provider_root,
            runner_path=runner,
            wall_timeout_seconds=2.0,
        ),
    )
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "verified-terminal-retry"),
            "provider-start-verified-terminal-retry",
        )
        original = admitted["execution"]
        identities = admitted["identities"]

        assert runtime.experiment.process_once()
        replacement = runtime.experiment.query(
            identities["evaluation_attempt_ref"]
        )
        current = replacement["execution"]
        assert replacement["identities"] == identities
        assert current["status"] == "admitted"
        assert current["managed_status"] == "suspended"
        assert current["run_ref"] == original["run_ref"]
        assert current["attempt_generation"] == 2
        assert current["attempt_ref"] != original["attempt_ref"]
        assert current["root_session_ref"] == original["root_session_ref"]
        assert current["fence_ref"] != original["fence_ref"]
        assert current["provider_operation_generation"] == 2
        assert current["provider_operation_ref"] != original[
            "provider_operation_ref"
        ]
        assert current["provider_operation_retry_permitted"] is False
        assert replacement["assets"]["status"] == "not_attempted"
        assert replacement["formal_measurement"]["status"] == "not_attempted"
        assert not runtime.experiment.process_once()
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"

        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=original["run_ref"],
                attempt_ref=original["attempt_ref"],
                fence_ref=original["fence_ref"],
                kind="stdout",
                payload={"line": "late old operation", "stream": "stdout"},
                observed_at=time.time(),
            )

        _resume_managed_experiment(
            runtime,
            quest_ref=quest["quest_ref"],
            run_ref=current["run_ref"],
        )
        for _step in range(3):
            assert runtime.experiment.process_once()
        completed = runtime.experiment.query(identities["evaluation_attempt_ref"])
        assert completed["identities"] == identities
        assert completed["formal_measurement"]["status"] == "accepted"
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "2"
        assert len(
            tuple(
                path
                for path in provider_root.glob("provider-operations/*")
                if path.is_dir()
            )
        ) == 2
    finally:
        runtime.close()


def test_unverified_provider_failure_cannot_authorize_a_second_operation(
    tmp_path: Path,
) -> None:
    provider = _UnverifiedTerminalClaimProvider(
        tmp_path / "unverified-terminal-provider",
        runner_path=_write_runner(tmp_path / "unverified-terminal-runner"),
        wall_timeout_seconds=2.0,
    )
    runtime = _runtime(tmp_path / "unverified-terminal-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "unverified-terminal"),
            "provider-start-unverified-terminal",
        )
        assert runtime.experiment.process_once()
        held = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert held["execution"]["status"] == "running"
        assert held["execution"]["managed_status"] == "running"
        assert held["execution"]["failure"] is None
        assert held["execution"]["attempt_generation"] == 1
        assert held["execution"]["provider_operation_generation"] == 1
        assert held["formal_measurement"]["status"] == "not_attempted"
        assert provider.calls == 1
        assert not runtime.experiment.process_once()
    finally:
        runtime.close()


def test_same_attempt_reconciliation_resumes_after_durable_cursor_without_duplicates(
    tmp_path: Path,
) -> None:
    runner = _write_runner(
        tmp_path / "cursor-resume-runner",
        mode="streaming",
    )
    provider_root = tmp_path / "cursor-resume-provider"
    provider = _PendingAfterObservationProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    runtime = _runtime(tmp_path / "cursor-resume-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "cursor-resume"),
            "provider-start-cursor-resume",
        )
        attempt_ref = admitted["execution"]["attempt_ref"]

        assert runtime.experiment.process_once()
        deferred = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert deferred["execution"]["status"] == "admitted"
        assert deferred["execution"]["attempt_ref"] == attempt_ref
        assert provider.interrupted is True

        for _step in range(3):
            assert runtime.experiment.process_once()
        completed = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert completed["formal_measurement"]["status"] == "accepted"
        assert completed["execution"]["attempt_ref"] == attempt_ref
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"

        ledger_path = next(
            provider_root.glob("provider-operations/*/observations.jsonl")
        )
        import json

        ledger_observations = [
            json.loads(line)["payload"]
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        ar_observations = [
            event
            for event in runtime.experiment.query_events(
                admitted["identities"]["evaluation_attempt_ref"],
                limit=256,
            )
            if event["kind"] in {"stdout", "telemetry"}
        ]
        assert [
            (event["kind"], event["payload"], event["observed_at"])
            for event in ar_observations
        ] == [
            (
                observation["kind"],
                observation["payload"],
                observation["observed_at"],
            )
            for observation in ledger_observations
        ]
        assert [
            event["payload"]["line"]
            for event in ar_observations
            if event["kind"] == "stdout"
        ] == ["line-early", "line-late"]
    finally:
        runtime.close()


def test_daemon_restart_after_partial_ledger_replaces_attempt_without_replay(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "restart-cursor-data"
    provider_root = tmp_path / "restart-cursor-provider"
    runner = _write_runner(
        tmp_path / "restart-cursor-runner",
        mode="streaming",
    )
    first_provider = _PendingAfterObservationProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    first = _runtime(data_root, first_provider)
    quest = _confirm_quest(first)
    admitted = first.experiment.start(
        _intent(quest["quest_ref"], "restart-cursor"),
        "provider-start-restart-cursor",
    )
    evaluation_attempt_ref = admitted["identities"]["evaluation_attempt_ref"]

    assert first.experiment.process_once()
    deferred = first.experiment.query(evaluation_attempt_ref)
    old_execution = deferred["execution"]
    assert old_execution["status"] == "admitted"
    assert first_provider.interrupted is True
    assert [
        event["payload"]["line"]
        for event in first.experiment.query_events(evaluation_attempt_ref)
        if event["kind"] == "stdout"
    ] == ["line-early"]
    first.close()

    restarted_provider = BuiltinMicroExperimentProvider(
        provider_root,
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    restarted = _runtime(data_root, restarted_provider)
    try:
        recovered = restarted.experiment.query(evaluation_attempt_ref)
        current_execution = recovered["execution"]
        assert current_execution["attempt_generation"] == 2
        assert current_execution["attempt_ref"] != old_execution["attempt_ref"]
        assert current_execution["fence_ref"] != old_execution["fence_ref"]
        assert current_execution["provider_operation_ref"] == (
            old_execution["provider_operation_ref"]
        )

        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            restarted.owners.agent_runtime.record_experiment_observation(
                run_ref=old_execution["run_ref"],
                attempt_ref=old_execution["attempt_ref"],
                fence_ref=old_execution["fence_ref"],
                kind="stdout",
                payload={"line": "late-old-fence", "stream": "stdout"},
                observed_at=time.time(),
            )

        for _step in range(3):
            assert restarted.experiment.process_once()
        completed = restarted.experiment.query(evaluation_attempt_ref)
        assert completed["formal_measurement"]["status"] == "accepted"
        assert completed["formal_measurement"]["metric_result"]["metrics"] == {
            "baseline_mean": 0.0,
            "variant_mean": -0.25,
            "mean_delta": -0.25,
        }
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"

        import json

        ledger_path = next(
            provider_root.glob("provider-operations/*/observations.jsonl")
        )
        ledger_observations = [
            json.loads(line)["payload"]
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        current_events = restarted.experiment.query_events(
            evaluation_attempt_ref,
            limit=256,
        )
        current_observations = [
            event
            for event in current_events
            if event["kind"] in {"stdout", "telemetry"}
        ]
        assert [
            (event["kind"], event["payload"], event["observed_at"])
            for event in current_observations
        ] == [
            (
                observation["kind"],
                observation["payload"],
                observation["observed_at"],
            )
            for observation in ledger_observations
        ]
        assert [
            event["payload"]["line"]
            for event in current_observations
            if event["kind"] == "stdout"
        ] == ["line-early", "line-late"]

        log_role = completed["assets"]["log_assets"][0]
        materialized = restarted.owners.research_memory.materialize_asset(
            log_role["version_ref"]
        )
        log_document = json.loads(materialized.content.decode("utf-8"))
        assert [line["line"] for line in log_document["stdout"]] == [
            "line-early",
            "line-late",
        ]
        assert log_document["observation"] == {
            "mode": "raw_stdout",
            "complete": True,
            "truncated": False,
            "dropped": 0,
            "event_count": len(current_events),
            "stdout_count": 2,
        }
    finally:
        restarted.close()


def test_default_composition_scopes_provider_spool_to_the_data_root(
    tmp_path: Path,
) -> None:
    drafting = _Drafting()
    data_root = prepare_data_root(tmp_path / "default-provider-data")
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_Probe(),
    )
    try:
        provider = runtime.experiment._provider
        assert isinstance(provider, BuiltinMicroExperimentProvider)
        assert provider.workspace == data_root.run / "experiment-provider"
    finally:
        runtime.close()


def test_same_provider_operation_with_conflicting_invocation_fails_closed(
    tmp_path: Path,
) -> None:
    runner = _write_runner(tmp_path / "identity-runner")
    provider = BuiltinMicroExperimentProvider(
        tmp_path / "provider",
        runner_path=runner,
        wall_timeout_seconds=2.0,
    )
    request = _direct_provider_request("provider-operation-stable")
    provider.execute(request, lambda _event: None)

    with pytest.raises(
        OwnerConflict, match="experiment_provider_identity_conflict"
    ):
        provider.execute(
            replace(
                request,
                required_metrics=(*request.required_metrics, "forged_metric"),
            ),
            lambda _event: None,
        )

    assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"


def test_runtime_bundle_is_rm_owned_and_covers_provider_orchestration_and_parser(
    tmp_path: Path,
) -> None:
    provider = BuiltinMicroExperimentProvider(tmp_path / "provider")
    runtime = _runtime(tmp_path / "data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "implementation-bundle"),
            "provider-start-implementation-bundle",
        )
        implementation = admitted["execution_request"]["implementation"]
        assert implementation["receipt"]["issuer"] == "research_memory"
        assert implementation["content_hash"] == (
            provider.runtime_binding().runner_bundle_hash
        )

        materialized = runtime.owners.research_memory.materialize_asset(
            implementation["version_ref"]
        )
        assert hashlib.sha256(materialized.content).hexdigest() == (
            implementation["content_hash"]
        )
        package_root = Path(sys.modules["meta_research.experiment"].__file__).parent
        required_members = (
            package_root / "experiment.py",
            package_root / "experiment_contract.py",
            package_root / "experiment_provider_supervisor.py",
            package_root / "provider_supervisor.py",
            package_root / "experiment_runner.py",
        )
        for member in required_members:
            member_hash = hashlib.sha256(member.read_bytes()).hexdigest()
            assert member.name.encode("utf-8") in materialized.content
            assert member_hash.encode("ascii") in materialized.content
        resources = set(provider.runtime_binding().resource_bindings)
        assert "limit:wall-time-seconds:300" in resources
        assert "limit:stdout-bytes:16777216" in resources
        assert "limit:result-bytes:16777216" in resources
        assert "limit:stdout-records:65536" in resources
        assert "limit:observation-count:524288" in resources
        assert "limit:observation-record-bytes:32768" in resources
        assert "telemetry:cadence-seconds:0.25" in resources
        assert all("nvidia" not in resource for resource in resources)
    finally:
        runtime.close()


def test_public_tail_is_bounded_but_log_asset_contains_all_durable_stdout(
    tmp_path: Path,
) -> None:
    runner = _write_runner(tmp_path / "many-lines-runner", mode="many_lines")
    provider = BuiltinMicroExperimentProvider(
        tmp_path / "many-lines-provider",
        runner_path=runner,
        wall_timeout_seconds=2.0,
        stdout_max_bytes=64 * 1024,
        result_max_bytes=8 * 1024,
    )
    runtime = _runtime(tmp_path / "many-lines-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "many-lines"),
            "provider-start-many-lines",
        )
        for _step in range(3):
            assert runtime.experiment.process_once()
        completed = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert completed["execution"]["stdout_observation"]["truncated"] is True
        log_role = completed["assets"]["log_assets"][0]
        log = runtime.owners.research_memory.materialize_asset(
            log_role["version_ref"]
        )
        import json

        document = json.loads(log.content.decode("utf-8"))
        assert len(document["stdout"]) == 300
        assert document["stdout"][0]["line"] == "line-000"
        assert document["stdout"][-1]["line"] == "line-299"
        assert document["observation"] == {
            "mode": "raw_stdout",
            "complete": True,
            "truncated": False,
            "dropped": 0,
            "event_count": 303,
            "stdout_count": 300,
        }
    finally:
        runtime.close()


def test_structural_stdout_limits_do_not_preempt_the_byte_budget(
    tmp_path: Path,
) -> None:
    runner = _write_runner(
        tmp_path / "structural-capacity-runner",
        mode="structural_capacity",
    )
    provider = BuiltinMicroExperimentProvider(
        tmp_path / "structural-capacity-provider",
        runner_path=runner,
        wall_timeout_seconds=10.0,
    )
    runtime = _runtime(tmp_path / "structural-capacity-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "structural-capacity"),
            "provider-start-structural-capacity",
        )
        attempt_ref = admitted["identities"]["evaluation_attempt_ref"]
        deadline = time.monotonic() + 15.0
        while True:
            runtime.experiment.process_once()
            completed = runtime.experiment.query(attempt_ref)
            if completed["formal_measurement"]["status"] == "accepted":
                break
            assert time.monotonic() < deadline, (
                completed["execution"],
                completed["assets"],
                completed["formal_measurement"],
            )
            time.sleep(0.02)

        assert completed["execution"]["status"] == "executed"
        log_role = completed["assets"]["log_assets"][0]
        log = runtime.owners.research_memory.materialize_asset(
            log_role["version_ref"]
        )
        document = json.loads(log.content.decode("utf-8"))
        assert len(document["stdout"]) == 1
        assert document["stdout"][0]["line"] == "x" * 8000
        stdout_path = next(
            (provider.workspace / "provider-operations").glob("*/stdout.bin")
        )
        raw_stdout = stdout_path.read_bytes()
        assert b"y" * (40 * 1024) in raw_stdout
        assert b"x" * 8000 in raw_stdout
        assert b"META_RESEARCH_RESULT\t" in raw_stdout
    finally:
        runtime.close()


def test_short_stdout_records_do_not_hit_legacy_structure_counts() -> None:
    class InMemoryLedger:
        def __init__(self) -> None:
            self.exceeded = threading.Event()
            self.count = 0

        def append(self, _kind: str, _payload: object, _observed_at: float) -> None:
            if self.count >= OBSERVATION_MAX_COUNT:
                self.exceeded.set()
                return
            self.count += 1

    payload = (b"line\n" * 9000)
    assert len(payload) < 16 * 1024 * 1024
    exceeded = threading.Event()
    errors: list[BaseException] = []
    ledger = InMemoryLedger()

    _bounded_drain(
        io.BytesIO(payload),
        io.BytesIO(),
        32 * 1024 * 1024 + 1024,
        16 * 1024 * 1024,
        STDOUT_MAX_RECORDS,
        16 * 1024 * 1024,
        exceeded,
        errors,
        ledger,  # type: ignore[arg-type]
    )

    assert not exceeded.is_set()
    assert not ledger.exceeded.is_set()
    assert errors == []
    assert ledger.count == 9000


def test_observation_record_guard_skips_only_oversized_stdout() -> None:
    stream = io.BytesIO()
    ledger = _ObservationLedger(
        stream,
        key=b"k" * 32,
        invocation_hash="a" * 64,
        maximum_count=8,
    )

    ledger.append(
        "stdout",
        {"line": "x" * (40 * 1024), "stream": "stdout"},
        1_720_000_000.0,
    )
    assert ledger.count == 0
    assert not ledger.exceeded.is_set()
    assert stream.getvalue() == b""

    ledger.append(
        "telemetry",
        {"oversized": "x" * (40 * 1024)},
        1_720_000_001.0,
    )
    assert ledger.count == 0
    assert ledger.exceeded.is_set()
    assert stream.getvalue() == b""


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("timeout", "experiment_provider_timeout"),
        ("stdout_limit", "experiment_provider_output_limit"),
        ("result_limit", "experiment_provider_output_limit"),
        ("invalid_utf8", "experiment_provider_output_invalid"),
        ("descendant", "experiment_provider_descendant_process"),
    ],
)
def test_provider_safety_failures_suspend_run_and_never_form_metric_result(
    tmp_path: Path,
    mode: str,
    expected_code: str,
) -> None:
    child_pid_path = tmp_path / f"{mode}.child.pid"
    runner = _write_runner(
        tmp_path / f"{mode}-runner",
        mode=mode,
        child_pid_path=child_pid_path,
    )
    provider = BuiltinMicroExperimentProvider(
        tmp_path / f"{mode}-provider",
        runner_path=runner,
        wall_timeout_seconds=0.15,
        stdout_max_bytes=(1024 * 1024 if mode == "result_limit" else 1024),
        result_max_bytes=(1024 * 1024 if mode == "stdout_limit" else 1024),
    )
    runtime = _runtime(tmp_path / f"{mode}-data", provider)
    try:
        quest = _confirm_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], mode),
            f"provider-start-{mode}",
        )
        started_at = time.monotonic()
        assert runtime.experiment.process_once()
        assert time.monotonic() - started_at < 2.0

        waiting = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        execution = waiting["execution"]
        assert execution["status"] == "admitted"
        assert execution["managed_status"] == "suspended"
        assert execution["attempt_generation"] == 2
        assert execution["root_session_ref"] == admitted["execution"][
            "root_session_ref"
        ]
        assert execution["provider_operation_generation"] == 2
        assert execution["failure"] is None
        assert execution["events"][-1]["payload"]["reason"] == {
            "code": expected_code
        }
        assert waiting["assets"]["status"] == "not_attempted"
        assert waiting["formal_measurement"] == {
            "status": "not_attempted",
            "metric_result": None,
        }
        assert not runtime.experiment.process_once()
        assert runner.with_suffix(".count").read_text(encoding="utf-8") == "1"

        if mode == "descendant":
            assert child_pid_path.is_file()
            _assert_process_gone(
                int(child_pid_path.read_text(encoding="utf-8"))
            )
    finally:
        runtime.close()
