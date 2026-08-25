from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from meta_research.migration import upgrade_database


def test_target_root_lifecycle_migration_is_single_head_and_adds_owner_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target-root-lifecycle.sqlite3"
    upgrade_database(path)
    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "ar_target_root_lifecycles",
            "ar_target_root_completions",
            "rm_target_root_completion_manifests",
            "rg_target_root_measurements",
        } <= tables
        inspector = inspect(engine)
        for table_name in (
            "ar_target_handoff_manifests",
            "ar_target_work_notices",
        ):
            target_fk = next(
                item
                for item in inspector.get_foreign_keys(table_name)
                if item["constrained_columns"] == ["target_ref"]
            )
            assert target_fk["referred_table"] == "ar_target_launches"
            assert target_fk["referred_columns"] == ["target_ref"]
        assert {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(
                "ar_target_handoff_manifests"
            )
        } == {("target_ref",), ("semantic_barrier_fact_ref",)}
        assert {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(
                "ar_target_work_notices"
            )
        } == {
            ("sequence",),
            ("terminal_transition_ref",),
            ("target_ref",),
            ("manifest_ref",),
            ("idempotency_key",),
        }
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0030_runtime_protection"
            ar = connection.execute(
                text(
                    "SELECT target_root_lifecycle_count, "
                    "target_root_completion_count FROM agent_runtime_state "
                    "WHERE singleton = 'owner'"
                )
            ).one()
            rm = connection.execute(
                text(
                    "SELECT target_root_completion_manifest_count FROM "
                    "research_memory_state WHERE singleton = 'owner'"
                )
            ).one()
            rg = connection.execute(
                text(
                    "SELECT target_root_measurement_count FROM "
                    "research_graph_state WHERE singleton = 'owner'"
                )
            ).one()
            assert tuple(ar) == (0, 0)
            assert tuple(rm) == (0,)
            assert tuple(rg) == (0,)
    finally:
        engine.dispose()
