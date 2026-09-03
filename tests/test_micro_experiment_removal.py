from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.operations import Operations

from meta_research import experiment_contract
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from test_plan_stage_migration import _upgrade_to_revision


_MICRO_ONLY_TABLES = {
    "rg_experiment_requests",
    "rg_experiment_idempotency",
    "ar_experiment_runs",
    "ar_experiment_sessions",
    "ar_experiment_attempts",
    "ar_experiment_events",
    "rg_target_run_bindings",
    "ar_target_run_admissions",
    "rg_target_protected_execution_bindings",
    "rm_target_result_manifests",
    "rg_target_protocol_aggregations",
    "ar_target_execution_closures",
}

_MICRO_ONLY_COUNTERS = {
    "agent_runtime_state": {
        "experiment_run_count",
        "experiment_completed_run_count",
        "experiment_attempt_count",
        "experiment_session_count",
        "active_experiment_run_count",
        "target_execution_closure_count",
    },
    "research_graph_state": {
        "target_protected_execution_count",
        "target_protocol_aggregation_count",
    },
    "research_memory_state": {"target_result_manifest_count"},
}

_FORMAL_TARGET_TABLES = {
    "rg_experiment_baselines",
    "rg_experiment_variants",
    "rg_evaluation_protocols",
    "rg_protocol_versions",
    "rg_evaluations",
    "rg_variant_runs",
    "rg_evaluation_attempts",
    "rg_experiment_input_bindings",
    "rg_experiment_asset_roles",
    "rg_evaluation_attempt_checkpoints",
    "rg_metric_results",
    "rg_target_measurement_domain_authorities",
    "rg_target_generic_execution_bindings_v3",
    "rm_target_generic_result_manifests",
    "rg_target_measurement_attempt_bindings",
    "ar_target_native_execution_closures",
    "rm_target_root_completion_manifests",
    "rg_target_root_measurements",
    "ar_target_root_completions",
    "rg_target_commits",
}

_FORMAL_TARGET_COUNTERS = {
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
}

_FORMAL_TARGET_SYMBOLS = {
    "AcceptedExperimentInputBinding",
    "AcceptedExperimentAssetRole",
    "FormalMetricResult",
    "EXPERIMENT_RESULT_DISPOSITIONS",
}

_MICRO_RUNTIME_SYMBOLS = {
    "ExperimentIntent",
    "ExperimentProviderUnavailable",
    "ExperimentRuntimeBinding",
    "ExperimentDomainAdmission",
    "AcceptedExperimentExecutionRequest",
    "ExperimentObservation",
    "MaterializedExperimentCheckpoint",
    "ExperimentProviderRequest",
    "ExperimentProviderResult",
    "ExperimentResultComponentManifest",
}


def test_fresh_head_schema_removes_only_micro_experiment_state(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "fresh-head")
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
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        state_columns = {
            table: {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for table in _MICRO_ONLY_COUNTERS
        }
        foreign_key_targets = {
            row[2]
            for table in tables
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        provider_unit_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND "
            "name = 'ar_provider_units'"
        ).fetchone()[0]

    assert version == ("0042_remove_micro_experiment",)
    assert _MICRO_ONLY_TABLES.isdisjoint(tables)
    assert _FORMAL_TARGET_TABLES <= tables
    assert {
        "ix_rg_experiment_requests_created",
        "ix_ar_experiment_runs_status",
    }.isdisjoint(indexes)
    for table, counters in _MICRO_ONLY_COUNTERS.items():
        assert counters.isdisjoint(state_columns[table])
    assert _FORMAL_TARGET_COUNTERS <= state_columns["research_graph_state"]
    assert _MICRO_ONLY_TABLES.isdisjoint(foreign_key_targets)
    assert "'experiment'" not in provider_unit_ddl
    assert foreign_key_failures == []
    assert integrity == ("ok",)

    upgrade_database(data_root.database)
    with sqlite3.connect(data_root.database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == version


def test_micro_schema_cleanup_is_atomic_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "interrupted-micro-cleanup.sqlite3"
    _upgrade_to_revision(database, "0041_human_request_lifecycle")
    original_drop_table = Operations.drop_table
    interrupted = False

    def interrupt_after_first_drop(self, table_name, *args, **kwargs):
        nonlocal interrupted
        dropped = original_drop_table(self, table_name, *args, **kwargs)
        if table_name == "ar_target_execution_closures" and not interrupted:
            interrupted = True
            raise OSError("injected Micro cleanup interruption")
        return dropped

    monkeypatch.setattr(Operations, "drop_table", interrupt_after_first_drop)
    with pytest.raises(OSError, match="injected Micro cleanup interruption"):
        upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0041_human_request_lifecycle",)
        rolled_back_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert _MICRO_ONLY_TABLES <= rolled_back_tables

    monkeypatch.setattr(Operations, "drop_table", original_drop_table)
    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0042_remove_micro_experiment",)
        recovered_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert _MICRO_ONLY_TABLES.isdisjoint(recovered_tables)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_shared_experiment_contract_is_formal_target_measurement_only() -> None:
    public_symbols = vars(experiment_contract)

    assert _FORMAL_TARGET_SYMBOLS <= public_symbols.keys()
    assert _MICRO_RUNTIME_SYMBOLS.isdisjoint(public_symbols.keys())
