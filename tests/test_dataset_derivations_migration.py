"""The additive lineage table preserves existing global Dataset facts."""
import sqlite3

from meta_research.migration import upgrade_database
from test_migration_recovery import _upgrade_to_revision


def test_dataset_derivations_migration_preserves_existing_facts_and_is_repeatable(tmp_path):
    database = tmp_path / "dataset.sqlite3"
    _upgrade_to_revision(database, "0050_question_relations")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO rg_datasets (dataset_ref, semantic_key, payload_json, payload_hash, "
            "receipt_ref, receipt_hash, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("dataset_existing", "study:existing", '{"notes":"Keep original collected observations"}',
             "a" * 64, "receipt_existing", "b" * 64, 123.0))
        before = connection.execute("SELECT * FROM rg_datasets").fetchall()
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM rg_datasets").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM rg_dataset_derivations").fetchone() == (0,)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0052_artifact_data_role",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
