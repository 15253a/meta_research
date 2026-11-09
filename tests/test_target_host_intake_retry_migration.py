"""Host retry schema changes preserve the original completion custody ledger."""

import json
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from meta_research.migration import upgrade_database
from meta_research.owners.common import canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_host_intake_retry_migration_preserves_populated_owner_records(
    tmp_path: Path, monkeypatch,
) -> None:
    upgrade = command.upgrade
    with monkeypatch.context() as previous_schema:
        previous_schema.setattr(
            command, "upgrade", lambda config, _revision: upgrade(config, "0077_stage_model"),
        )
        runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text(encoding="utf-8"))
        document["formal_runs"][0]["input_refs"] = ["not-an-admitted-input"]
        result_path.write_text(canonical_json(document), encoding="utf-8")
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_formal_input_reference_invalid"
        completion = lifecycle.query_completion_by_ref(rejected.completion_ref)
        rejection = lifecycle.query_completion_rejection(rejected.completion_ref)
        manifest = memory.query(rejected.manifest_ref)
        assert completion is not None and rejection is not None and manifest is not None
        assert rejection.completion_ref == manifest.completion_ref == completion.completion_ref
        database = runtime._database.path
    finally:
        runtime.close()

    tables = (
        "ar_target_root_completions", "ar_target_root_completion_rejections",
        "rm_target_root_completion_manifests", "ar_target_root_lifecycles",
        "agent_runtime_state", "research_memory_state",
    )

    def snapshot():
        engine = create_engine(f"sqlite:///{database}", future=True)
        try:
            inspector = inspect(engine)
            all_tables = inspector.get_table_names()
            with engine.connect() as connection:
                rows = {table: [tuple(row) for row in connection.execute(text(f"SELECT * FROM {table}"))]
                        for table in tables}
                foreign_key_errors = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            return {
                "rows": rows,
                "foreign_key_errors": foreign_key_errors,
                "revision": revision,
                "foreign_keys": {table: sorted(
                    (tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
                    for item in inspector.get_foreign_keys(table)) for table in all_tables},
                "unique": {table: {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table)}
                           for table in all_tables},
                "checks": {item["sqltext"] for item in inspector.get_check_constraints("ar_target_root_completions")},
                "indexes": inspector.get_indexes("ar_target_root_completions"),
            }
        finally:
            engine.dispose()

    before = snapshot()
    assert before["revision"] == "0077_stage_model"
    assert before["foreign_key_errors"] == []
    assert all(len(before["rows"][table]) == 1 for table in tables)
    assert ("artifact_snapshot_hash",) in before["unique"]["ar_target_root_completions"]

    upgrade_database(database)
    upgrade_database(database)
    after = snapshot()

    assert after["revision"] == "0080_writing_display_message"
    assert after["rows"] == before["rows"]
    assert after["foreign_key_errors"] == []
    assert after["foreign_keys"] == before["foreign_keys"]
    assert after["checks"] == before["checks"]
    expected_unique = {**before["unique"], "ar_target_root_completions":
                       before["unique"]["ar_target_root_completions"] - {("artifact_snapshot_hash",)}}
    assert after["unique"] == expected_unique
    assert any(item["column_names"] == ["artifact_snapshot_hash"] and not item["unique"]
               for item in after["indexes"])
