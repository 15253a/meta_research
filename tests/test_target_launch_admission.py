from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.operations import Operations
from sqlalchemy import text

from meta_research.bundle_protocol import ContentBindingProof, ReceiptProof
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import (
    _HighRiskBundleSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finish_idea_stage,
    _finish_plan_stage,
    _grant_request_capability,
)
from test_plan_stage_migration import _upgrade_to_revision


def _ready_launch(runtime):
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    _finish_plan_stage(runtime)
    for _step in range(16):
        assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        request_projection = current["stage_run_request"]
        if request_projection is None:
            continue
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request_projection["request_ref"]
        )
        if run is None:
            continue
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        )
        if decisions:
            graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
            assert graph is not None
            selected = decisions[-1].selected_target_ref
            target = next(
                target for target in graph.targets if target.target_ref == selected
            )
            if (
                runtime.owners.research_graph.query_target_candidate_projection(
                    target_ref=target.target_ref
                )
                is None
            ):
                continue
            request = runtime.owners.research_graph.query_target_launch_request(
                target.target_ref
            )
            return graph, target, run, decisions[-1], request
    raise AssertionError("Bundle did not expose a dispatchable Target")


def _side_effect_counts(runtime) -> dict[str, int]:
    with runtime._database.read() as connection:
        return {
            table: int(
                connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            )
            for table in (
                "ar_target_launches",
                "ar_target_frontier_entries",
                "ar_target_run_admissions",
                "ar_experiment_runs",
                "rg_target_run_bindings",
                "rg_experiment_requests",
                "rm_asset_versions",
            )
        }


def test_target_launch_admission_precedes_all_experiment_side_effects(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "target-launch-admission")
    try:
        graph, target, _run, dispatch, request = _ready_launch(runtime)
        before = _side_effect_counts(runtime)
        assert before["ar_target_launches"] == 0
        assert before["ar_target_frontier_entries"] == 0
        projection = runtime.owners.research_graph.query_target_candidate_projection(
            target_ref=target.target_ref
        )
        assert projection is not None
        assert request.target_spec_binding == ContentBindingProof(
            subject_ref=target.target_ref,
            content_hash_ref=projection.projection_digest,
        )
        assert request.target_spec_acceptance_receipt.receipt_ref == (
            projection.receipt.receipt_ref
        )
        assert request.target_spec_acceptance_receipt.subject_ref == (
            projection.receipt.subject_ref
        )
        assert request.target_spec_acceptance_receipt.verified is True
        assert request.target_spec_acceptance_receipt.currentness_known is True
        assert request.target_spec_acceptance_receipt.current is True
        assert projection.source_spec_hash == target.spec_hash
        assert projection.source_acceptance_receipt.receipt_ref != (
            target.receipt.receipt_ref
        )
        with runtime._database.read() as connection:
            persisted_spec_receipt = connection.execute(
                text(
                    "SELECT receipt_ref, receipt_hash FROM "
                    "rg_target_spec_acceptances WHERE target_ref = :target_ref"
                ),
                {"target_ref": target.target_ref},
            ).first()
        assert persisted_spec_receipt is not None
        assert (
            persisted_spec_receipt.receipt_ref
            == projection.source_acceptance_receipt.receipt_ref
        )
        assert len(persisted_spec_receipt.receipt_hash) == 64
        assert request.accepted_input_target_commit_refs == ()
        assert request.accepted_input_asset_refs == ()
        assert request.recoverable_required is True

        ack = runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-launch-admission",
        )
        assert tuple(field.name for field in fields(ack)) == (
            "target_ref",
            "operation_ref",
        )
        assert ack.target_ref == target.target_ref
        assert ack.operation_ref.startswith("target_launch_operation_")
        after = _side_effect_counts(runtime)
        assert after["ar_target_launches"] == 1
        assert after["ar_target_frontier_entries"] == 0
        for table in (
            "ar_experiment_runs",
            "rg_experiment_requests",
            "rm_asset_versions",
        ):
            assert after[table] == before[table]

        assert runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        ) is None
        with runtime._database.read() as connection:
            launch = connection.execute(
                text(
                    "SELECT status, target_run_ref, root_session_ref, "
                    "execution_attempt_ref, execution_fence_ref, "
                    "execution_input_binding_ref, "
                    "execution_input_binding_receipt_ref, "
                    "execution_input_binding_receipt_hash, "
                    "current_handle_json, current_handle_hash FROM "
                    "ar_target_launches WHERE target_ref = :target_ref"
                ),
                {"target_ref": target.target_ref},
            ).first()
        assert launch is not None
        assert launch.status == "admitted"
        assert launch.target_run_ref.startswith("target_run_")
        assert not launch.target_run_ref.startswith("experiment_run_")
        assert (
            launch.root_session_ref,
            launch.execution_attempt_ref,
            launch.execution_fence_ref,
            launch.execution_input_binding_ref,
            launch.execution_input_binding_receipt_ref,
            launch.execution_input_binding_receipt_hash,
            launch.current_handle_json,
            launch.current_handle_hash,
        ) == (None,) * 8

        replay = runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-launch-admission",
        )
        assert replay == ack
        assert _side_effect_counts(runtime) == after
        assert graph.graph_ref == dispatch.graph_ref
    finally:
        runtime.close()


def test_bundle_worker_admits_exact_launch_then_waits_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bundle_runtime(tmp_path / "bundle-worker-launch-first")
    try:
        _graph, target, _run, dispatch, expected_request = _ready_launch(runtime)
        before = _side_effect_counts(runtime)
        launch_calls: list[tuple[object, dict[str, object]]] = []
        request_reads: list[str] = []
        target_runtime_calls: list[str] = []
        original_admit = runtime.owners.agent_runtime.admit_target_launch
        original_request = runtime.owners.research_graph.query_target_launch_request

        def observe_request(target_ref: str):
            request_reads.append(target_ref)
            return original_request(target_ref)

        def observe_admit(request, **kwargs):
            launch_calls.append((request, kwargs))
            return original_admit(request, **kwargs)

        def forbidden_legacy_path(*_args, **_kwargs):
            raise AssertionError("launch-first worker entered legacy execution")

        monkeypatch.setattr(
            runtime.owners.research_graph,
            "query_target_launch_request",
            observe_request,
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "admit_target_launch",
            observe_admit,
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "admit_target_run",
            forbidden_legacy_path,
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "bind_target_run",
            forbidden_legacy_path,
        )

        assert runtime.bundle_stage.process_once() is True
        assert not hasattr(runtime.bundle_stage, "_target_run_runtime")
        assert target_runtime_calls == []
        assert request_reads == [target.target_ref]
        assert len(launch_calls) == 1
        launched_request, launch_kwargs = launch_calls[0]
        assert launched_request == expected_request
        assert launch_kwargs["dispatch_decision_ref"] == dispatch.decision_ref
        assert launch_kwargs["human_request_ref"] is None
        assert launch_kwargs["human_waiter_ref"] is None
        assert launch_kwargs["human_waiter_generation"] is None
        assert launch_kwargs["human_authorization_receipt_ref"] is None
        assert runtime.bundle_stage.transient_error == "target_launch_admitted"

        admitted = _side_effect_counts(runtime)
        assert admitted["ar_target_launches"] == before["ar_target_launches"] + 1
        assert admitted["ar_target_frontier_entries"] == 0
        for table in (
            "ar_target_run_admissions",
            "ar_experiment_runs",
            "rg_target_run_bindings",
            "rg_experiment_requests",
            "rm_asset_versions",
        ):
            assert admitted[table] == before[table]
        assert runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        ) is None

        ack = runtime.owners.agent_runtime.query_target_launch_ack(target.target_ref)
        assert ack is not None
        for _step in range(4):
            advanced = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == "target_launch_pending":
                assert advanced is False
                break
        else:
            raise AssertionError("Bundle did not yield the admitted Target")
        assert target_runtime_calls == []
        assert request_reads == [target.target_ref]
        assert len(launch_calls) == 1
        assert _side_effect_counts(runtime) == admitted
        assert (
            runtime.owners.agent_runtime.query_target_launch_ack(target.target_ref)
            == ack
        )
        assert runtime.bundle_stage.transient_error == "target_launch_pending"

        # Once AR exposes a durable running frontier, Bundle must stop driving
        # the Target lifecycle.  The independent Web daemon is now the sole
        # caller, so another Bundle tick cannot create a cross-worker duplicate.
        running_frontier = SimpleNamespace(
            state="running",
            currentness_known=True,
            current=True,
            current_handle=SimpleNamespace(target_run_ref="target-run-daemon-owned"),
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "query_target_frontier_entry",
            lambda target_ref: (
                running_frontier if target_ref == target.target_ref else None
            ),
        )
        for _step in range(4):
            advanced = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == "target_root_running":
                assert advanced is False
                break
        else:
            raise AssertionError("Bundle did not yield the running Target to daemon")
        assert target_runtime_calls == []
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "mutation",
    ("spec", "upstream", "asset", "recoverable"),
)
def test_target_launch_rejects_inexact_request_with_zero_side_effects(
    tmp_path: Path,
    mutation: str,
) -> None:
    runtime = _bundle_runtime(tmp_path / f"target-launch-invalid-{mutation}")
    try:
        _graph, target, _run, dispatch, request = _ready_launch(runtime)
        before = _side_effect_counts(runtime)
        if mutation == "spec":
            forged_hash = "0" * 64
            candidate = replace(
                request,
                target_spec_binding=replace(
                    request.target_spec_binding,
                    content_hash_ref=forged_hash,
                ),
                target_spec_acceptance_receipt=replace(
                    request.target_spec_acceptance_receipt,
                    subject_ref=forged_hash,
                ),
            )
        elif mutation == "upstream":
            candidate = replace(
                request,
                accepted_input_target_commit_refs=("target_commit_forged",),
            )
        elif mutation == "asset":
            candidate = replace(
                request,
                accepted_input_asset_refs=("asset_forged",),
            )
        else:
            candidate = replace(request, recoverable_required=False)
        with pytest.raises(
            OwnerConflict,
                match=(
                    "target_launch_request_invalid"
                    if mutation == "recoverable"
                    else "target_launch_authority_stale"
                ),
            ):
            runtime.owners.agent_runtime.admit_target_launch(
                candidate,
                dispatch_decision_ref=dispatch.decision_ref,
                idempotency_key=f"target-launch-invalid-{mutation}",
            )
        assert runtime.owners.agent_runtime.query_target_launch_ack(
            target.target_ref
        ) is None
        assert runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        ) is None
        assert _side_effect_counts(runtime) == before
    finally:
        runtime.close()


def test_target_launch_requires_the_latest_dispatch_decision(tmp_path: Path) -> None:
    runtime = _bundle_runtime(tmp_path / "target-launch-current-dispatch")
    try:
        _graph, target, run, first, request = _ready_launch(runtime)
        inbox_checkpoint = (
            runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(run.run_ref)
        )
        assert inbox_checkpoint is not None
        second = runtime.owners.agent_runtime.record_bundle_dispatch_decision(
            run_ref=first.run_ref,
            attempt_ref=first.attempt_ref,
            fence_ref=first.fence_ref,
            native_session_ref=first.native_session_ref,
            graph_ref=first.graph_ref,
            generation=first.generation + 1,
            frontier=first.frontier,
            state=first.state,
            action="dispatch",
            selected_target_ref=target.target_ref,
            rationale="Reconfirm the unchanged exact frontier before launch.",
            inbox_checkpoint=inbox_checkpoint,
            idempotency_key="target-launch-current-dispatch-second",
        )
        before = _side_effect_counts(runtime)
        with pytest.raises(OwnerConflict, match="target_launch_dispatch_invalid"):
            runtime.owners.agent_runtime.admit_target_launch(
                request,
                dispatch_decision_ref=first.decision_ref,
                idempotency_key="target-launch-stale-dispatch",
            )
        assert _side_effect_counts(runtime) == before
        ack = runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=second.decision_ref,
            idempotency_key="target-launch-current-dispatch",
        )
        assert ack.target_ref == target.target_ref
    finally:
        runtime.close()


def test_bundle_worker_passes_exact_high_risk_authorization_to_launch_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "target-launch-high-risk",
        bundle_skill_provider=_HighRiskBundleSkill(),
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(16):
            assert runtime.bundle_stage.process_once()
            requests = runtime.owners.agent_runtime.query_human_requests(
                include_history=True
            )
            if requests:
                break
        else:
            raise AssertionError("High-risk Target did not open exact HumanRequest")
        human_request = requests[0]
        authorization = _grant_request_capability(runtime, human_request)
        for _step in range(12):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            stage_request = current["stage_run_request"]
            assert stage_request is not None
            run = runtime.owners.agent_runtime.query_bundle_stage_run(
                stage_request["request_ref"]
            )
            assert run is not None
            decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
                run.run_ref
            )
            if decisions:
                break
        else:
            raise AssertionError("Authorized Target did not reach current dispatch")
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph is not None
        target = graph.targets[0]
        if (
            runtime.owners.research_graph.query_target_candidate_projection(
                target_ref=target.target_ref
            )
            is None
        ):
            runtime.owners.research_graph.accept_target_candidate_projection(
                target_ref=target.target_ref,
                idempotency_key=f"test-target-candidate-projection:{target.target_ref}",
            )
        request = runtime.owners.research_graph.query_target_launch_request(
            target.target_ref
        )
        before = _side_effect_counts(runtime)
        with pytest.raises(
            OwnerConflict, match="target_launch_authorization_invalid"
        ):
            runtime.owners.agent_runtime.admit_target_launch(
                request,
                dispatch_decision_ref=decisions[-1].decision_ref,
                idempotency_key="target-launch-high-risk-unauthorized",
            )
        assert _side_effect_counts(runtime) == before

        launch_calls: list[tuple[object, dict[str, object]]] = []
        target_runtime_calls: list[str] = []
        original_admit = runtime.owners.agent_runtime.admit_target_launch

        def observe_admit(launch_request, **kwargs):
            launch_calls.append((launch_request, kwargs))
            return original_admit(launch_request, **kwargs)

        def forbidden_legacy_path(*_args, **_kwargs):
            raise AssertionError("high-risk launch entered legacy execution")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "admit_target_launch",
            observe_admit,
        )
        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "admit_target_run",
            forbidden_legacy_path,
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "bind_target_run",
            forbidden_legacy_path,
        )
        assert runtime.bundle_stage.process_once() is True
        assert not hasattr(runtime.bundle_stage, "_target_run_runtime")
        assert len(launch_calls) == 1
        launched_request, launch_kwargs = launch_calls[0]
        assert launched_request == request
        assert (
            launch_kwargs["dispatch_decision_ref"]
            == decisions[-1].decision_ref
        )
        assert launch_kwargs["human_request_ref"] == human_request["request_ref"]
        assert launch_kwargs["human_waiter_ref"] == target.target_ref
        assert launch_kwargs["human_waiter_generation"] == 1
        assert (
            launch_kwargs["human_authorization_receipt_ref"]
            == authorization["receipt_ref"]
        )
        ack = runtime.owners.agent_runtime.query_target_launch_ack(target.target_ref)
        assert ack is not None
        assert ack.target_ref == target.target_ref
        persisted = runtime.owners.agent_runtime.query_human_request(
            human_request["request_ref"]
        )
        assert persisted is not None
        assert persisted["direct_waiters"][0]["status"] == "consumed"
        admitted = _side_effect_counts(runtime)
        assert admitted["ar_target_frontier_entries"] == 0
        for table in (
            "ar_target_run_admissions",
            "ar_experiment_runs",
            "rg_target_run_bindings",
            "rg_experiment_requests",
            "rm_asset_versions",
        ):
            assert admitted[table] == before[table]
        for _step in range(4):
            advanced = runtime.bundle_stage.process_once()
            if runtime.bundle_stage.transient_error == "target_launch_pending":
                assert advanced is False
                break
        else:
            raise AssertionError("Bundle did not yield the admitted Target")
        assert target_runtime_calls == []
        assert len(launch_calls) == 1
        assert _side_effect_counts(runtime) == admitted
        assert runtime.bundle_stage.transient_error == "target_launch_pending"
    finally:
        runtime.close()


def test_admission_never_claims_running_frontier_before_a_real_worker_session(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "target-frontier-currentness")
    try:
        _graph, target, _run, dispatch, request = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-frontier-currentness",
        )
        assert runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        ) is None
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ae_foreground_grants SET status = 'revoked' WHERE "
                    "cycle_ref = :cycle_ref"
                ),
                {"cycle_ref": _graph.cycle_ref},
            )
        assert runtime.owners.agent_runtime.query_target_frontier_entry(
            target.target_ref
        ) is None
        assert _side_effect_counts(runtime)["ar_target_frontier_entries"] == 0
    finally:
        runtime.close()


def test_target_launch_receipts_survive_restart_and_rg_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "target-launch-restart"
    runtime = _bundle_runtime(data_path)
    graph_ref: str
    target_ref: str
    try:
        graph, target, _run, dispatch, request = _ready_launch(runtime)
        ack = runtime.owners.agent_runtime.admit_target_launch(
            request,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-launch-restart",
        )
        graph_ref = graph.graph_ref
        target_ref = target.target_ref
        expected_ack = ack
        expected_frontier = runtime.owners.agent_runtime.query_target_frontier_entry(
            target_ref
        )
        assert expected_frontier is None
    finally:
        runtime.close()

    restarted = _bundle_runtime(data_path)
    try:
        assert (
            restarted.owners.agent_runtime.query_target_launch_ack(target_ref)
            == expected_ack
        )
        assert (
            restarted.owners.agent_runtime.query_target_frontier_entry(target_ref)
            == expected_frontier
        )
        with restarted._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_target_spec_acceptances SET receipt_hash = "
                    ":receipt_hash WHERE target_ref = :target_ref AND graph_ref = "
                    ":graph_ref"
                ),
                {
                    "receipt_hash": "0" * 64,
                    "target_ref": target_ref,
                    "graph_ref": graph_ref,
                },
            )
        with pytest.raises(
            OwnerConflict, match="target_spec_content_receipt_invalid"
        ):
            restarted.owners.research_graph.query_target_launch_request(target_ref)
    finally:
        restarted.close()


def test_interrupted_target_launch_migration_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "target-launch-migration.sqlite3"
    _upgrade_to_revision(database, "0017_bundle_target_rolling")
    original_create_table = Operations.create_table
    failed_once = False

    def fail_after_frontier(self, table_name, *args, **kwargs):
        nonlocal failed_once
        created = original_create_table(self, table_name, *args, **kwargs)
        if table_name == "ar_target_frontier_entries" and not failed_once:
            failed_once = True
            raise OSError("injected target launch migration interruption")
        return created

    monkeypatch.setattr(Operations, "create_table", fail_after_frontier)
    with pytest.raises(OSError, match="target launch migration interruption"):
        upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0017_bundle_target_rolling",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "ar_target_launches" not in tables
    assert "ar_target_frontier_entries" not in tables
    assert "rg_target_spec_acceptances" not in tables

    monkeypatch.setattr(Operations, "create_table", original_create_table)
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0033_reasoning_successor_context",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "ar_target_launches",
        "ar_target_frontier_entries",
        "rg_target_spec_acceptances",
    } <= tables
