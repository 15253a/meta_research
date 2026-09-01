from __future__ import annotations

import sqlite3
from pathlib import Path

from meta_research import experiment_contract
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root


_MICRO_TABLES = {
    "rg_experiment_requests",
    "rg_experiment_idempotency",
    "ar_experiment_runs",
    "ar_experiment_sessions",
    "ar_experiment_attempts",
    "ar_experiment_events",
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
        foreign_key_targets = {
            row[2]
            for table in tables
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()

    assert version == ("0040_remove_micro_experiment",)
    assert _MICRO_TABLES.isdisjoint(tables)
    assert _FORMAL_TARGET_TABLES <= tables
    assert _MICRO_TABLES.isdisjoint(foreign_key_targets)
    assert foreign_key_failures == []
    assert integrity == ("ok",)


def test_shared_experiment_contract_is_formal_target_measurement_only() -> None:
    public_symbols = vars(experiment_contract)

    assert _FORMAL_TARGET_SYMBOLS <= public_symbols.keys()
    assert _MICRO_RUNTIME_SYMBOLS.isdisjoint(public_symbols.keys())
