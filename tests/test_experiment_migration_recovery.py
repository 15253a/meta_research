from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from meta_research.experiment import ExperimentIntent
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from test_migration_recovery import _upgrade_to_revision
from test_public_experiment_measurement import _confirm_direct_quest, _runtime


_RG_COUNTERS = (
    "experiment_baseline_count",
    "experiment_variant_count",
    "evaluation_protocol_count",
    "protocol_version_count",
    "evaluation_count",
    "variant_run_count",
    "evaluation_attempt_count",
    "experiment_input_binding_count",
    "experiment_asset_role_count",
    "formal_measurement_count",
)

_AR_COUNTERS = (
    "experiment_run_count",
    "experiment_completed_run_count",
    "experiment_attempt_count",
    "experiment_session_count",
    "active_experiment_run_count",
)

_EXPERIMENT_TABLES = {
    "rg_experiment_baselines",
    "rg_experiment_variants",
    "rg_evaluation_protocols",
    "rg_protocol_versions",
    "rg_evaluations",
    "rg_variant_runs",
    "rg_evaluation_attempts",
    "rg_experiment_input_bindings",
    "rg_experiment_requests",
    "rg_experiment_idempotency",
    "rg_experiment_asset_roles",
    "rg_evaluation_attempt_checkpoints",
    "rg_metric_results",
    "ar_experiment_runs",
    "ar_experiment_sessions",
    "ar_experiment_attempts",
    "ar_experiment_events",
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _intent(quest_ref: str, request_ref: str) -> ExperimentIntent:
    return ExperimentIntent(
        execution_request_ref=request_ref,
        quest_ref=quest_ref,
        title="迁移与重启边界微实验",
        hypothesis="技术性 Session 替换不得创建新的实验领域身份。",
        variant_parameter=-0.25,
        sample_count=16,
    )


def test_fresh_0009_has_experiment_schema_and_zeroed_owner_counters(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "fresh-0009")

    upgrade_database(data_root.database)

    with sqlite3.connect(data_root.database) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rg_columns = _columns(connection, "research_graph_state")
        ar_columns = _columns(connection, "agent_runtime_state")
        ar_run_columns = _columns(connection, "ar_experiment_runs")
        metric_result_columns = _columns(connection, "rg_metric_results")
        rg_values = connection.execute(
            f"SELECT {', '.join(_RG_COUNTERS)} FROM research_graph_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        ar_values = connection.execute(
            f"SELECT {', '.join(_AR_COUNTERS)} FROM agent_runtime_state "
            "WHERE singleton = 'owner'"
        ).fetchone()
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()

    assert version == ("0014_advancement_runtime_control",)
    assert _EXPERIMENT_TABLES <= tables
    assert set(_RG_COUNTERS) <= rg_columns
    assert set(_AR_COUNTERS) <= ar_columns
    assert {
        "execution_request_ref",
        "variant_run_ref",
        "evaluation_attempt_ref",
        "attempt_ref",
        "attempt_generation",
        "root_session_ref",
        "fence_ref",
        "provider_operation_generation",
        "provider_operation_retry_permitted",
        "runtime_binding_hash",
        "result_hash",
    } <= ar_run_columns
    assert {
        "evaluation_attempt_ref",
        "result_role_ref",
        "metrics_hash",
        "execution_attempt_ref",
        "fence_ref",
        "execution_receipt_ref",
    } <= metric_result_columns
    assert rg_values == (0,) * len(_RG_COUNTERS)
    assert ar_values == (0,) * len(_AR_COUNTERS)
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_existing_0008_upgrades_then_experiment_admission_reopens(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "upgrade-from-0008")
    _upgrade_to_revision(data_root.database, "0008_quest_acquisition_session")

    runtime = _runtime(data_root.root)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "experiment-after-0008-upgrade"),
            "experiment-after-0008-upgrade",
        )
    finally:
        runtime.close()

    evaluation_attempt_ref = admitted["identities"]["evaluation_attempt_ref"]
    runtime = _runtime(data_root.root)
    try:
        reopened = runtime.experiment.query(evaluation_attempt_ref)
        assert reopened["identities"] == admitted["identities"]
        assert reopened["frozen_inputs"] == admitted["frozen_inputs"]
        assert reopened["execution"] == admitted["execution"]
    finally:
        runtime.close()

    with sqlite3.connect(data_root.database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0014_advancement_runtime_control",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_restart_replaces_only_running_ar_attempt_and_rejects_old_fence(
    tmp_path: Path,
) -> None:
    data_root_path = tmp_path / "running-restart"
    runtime = _runtime(data_root_path)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            _intent(quest["quest_ref"], "experiment-running-restart"),
            "experiment-running-restart",
        )
        running = runtime.owners.agent_runtime.claim_next_experiment()
        assert running is not None
        assert running.status == "running"
    finally:
        runtime.close()

    old_execution = admitted["execution"]
    old_domain_identities = admitted["identities"]
    evaluation_attempt_ref = old_domain_identities["evaluation_attempt_ref"]

    runtime = _runtime(data_root_path)
    try:
        recovered = runtime.experiment.query(evaluation_attempt_ref)
        replacement = recovered["execution"]

        assert recovered["identities"] == old_domain_identities
        assert recovered["identities"]["variant_run_ref"] == (
            admitted["identities"]["variant_run_ref"]
        )
        assert replacement["run_ref"] == old_execution["run_ref"]
        assert replacement["execution_request_ref"] == (
            old_execution["execution_request_ref"]
        )
        assert replacement["runtime_binding_hash"] == (
            old_execution["runtime_binding_hash"]
        )
        assert replacement["status"] == "admitted"
        assert replacement["attempt_generation"] == 2
        assert replacement["attempt_ref"] != old_execution["attempt_ref"]
        assert replacement["root_session_ref"] == old_execution["root_session_ref"]
        assert replacement["fence_ref"] != old_execution["fence_ref"]

        replacement_running = runtime.owners.agent_runtime.claim_next_experiment()
        assert replacement_running is not None
        assert replacement_running.attempt_ref == replacement["attempt_ref"]
        runtime.owners.agent_runtime.record_experiment_observation(
            run_ref=replacement_running.run_ref,
            attempt_ref=replacement_running.attempt_ref,
            fence_ref=replacement_running.fence_ref,
            kind="stdout",
            payload={"line": "replacement fence is current", "stream": "stdout"},
            observed_at=1_720_000_100.0,
        )

        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=old_execution["run_ref"],
                attempt_ref=old_execution["attempt_ref"],
                fence_ref=old_execution["fence_ref"],
                kind="stdout",
                payload={"line": "stale provider output", "stream": "stdout"},
                observed_at=1_720_000_101.0,
            )
        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            runtime.owners.agent_runtime.complete_experiment_execution(
                run_ref=old_execution["run_ref"],
                attempt_ref=old_execution["attempt_ref"],
                fence_ref=old_execution["fence_ref"],
                result={"stale": True},
            )

        after_stale_callbacks = runtime.experiment.query(evaluation_attempt_ref)
        assert after_stale_callbacks["identities"] == old_domain_identities
        assert after_stale_callbacks["execution"]["attempt_ref"] == (
            replacement["attempt_ref"]
        )
        assert [
            event["payload"].get("line")
            for event in after_stale_callbacks["execution"]["events"]
            if event["kind"] == "stdout"
        ] == ["replacement fence is current"]
        assert after_stale_callbacks["execution"]["execution_receipt"] is None
    finally:
        runtime.close()

    with sqlite3.connect(prepare_data_root(data_root_path).database) as connection:
        old_attempt = connection.execute(
            "SELECT status, retired_reason FROM ar_experiment_attempts "
            "WHERE attempt_ref = ?",
            (old_execution["attempt_ref"],),
        ).fetchone()
        attempts = connection.execute(
            "SELECT generation, status FROM ar_experiment_attempts "
            "WHERE run_ref = ? ORDER BY generation",
            (old_execution["run_ref"],),
        ).fetchall()

    assert old_attempt == ("retired", "daemon_restart")
    assert attempts == [(1, "retired"), (2, "running")]
