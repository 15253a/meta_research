from sqlalchemy import event

from meta_research.acquisition import effective_acquisition_config
from meta_research.database import Database
from meta_research.owners.common import canonical_hash
from meta_research.runtime_conditions import (
    read_runtime_conditions, read_runtime_conditions_in_transaction, save_runtime_conditions,
)
import test_public_autonomous_creation as fixtures


def test_autonomous_acquisition_uses_updated_conditions_without_new_database(tmp_path, monkeypatch):
    acquisition = fixtures._ReadyAutonomousAcquisitionProvider()
    runtime = fixtures._reasoning_runtime(
        tmp_path / "shared-conditions",
        reasoning_skill=fixtures._AutonomousReasoningSkill(require_source_literature=False),
        acquisition_provider=acquisition,
        host_compute_probe=fixtures.DeterministicPlanProbe(),
        proposal_drafter=fixtures._DeterministicDraftingAdapter(),
    )
    try:
        quest, _reasoning, checkpoint = fixtures._reach_autonomous_checkpoint(runtime, direct_quest=True)
        quest_ref = str(quest["quest_ref"])
        runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=str(checkpoint["checkpoint_ref"]),
            source_scientific_outcome_ref=str(checkpoint["scientific_outcome"]["outcome_ref"]),
            idempotency_key="shared-conditions-start",
        )
        previous = read_runtime_conditions(runtime.data_root.root, quest_ref, database=runtime._database)
        config = {"mode": "provided_only", "library_entry_url": "", "institution_required": False}
        current = save_runtime_conditions(
            runtime.data_root.root, quest_ref, text="Use newly supplied literature only.",
            expected_revision=previous["revision"], literature_configuration=config,
            database=runtime._database,
        )
        counts = {"databases": 0, "connections": 0, "closes": 0}
        shared_statements = []
        initialize, close = Database.__init__, Database.close

        def construct(self, *args, **kwargs):
            counts["databases"] += 1
            initialize(self, *args, **kwargs)
            event.listen(self._engine, "connect", connected)

        def connected(*args):
            counts["connections"] += 1

        def closed(self):
            counts["closes"] += 1
            close(self)

        def statement(_conn, _cursor, sql, *_args):
            shared_statements.append(sql)

        monkeypatch.setattr(Database, "__init__", construct)
        monkeypatch.setattr(Database, "close", closed)
        event.listen(runtime._database._engine, "before_cursor_execute", statement)
        try:
            assert runtime.autonomous_creation.process_once()
        finally:
            event.remove(runtime._database._engine, "before_cursor_execute", statement)
        session = runtime.owners.agent_runtime.query_acquisition_session(quest_ref=quest_ref)
        assert session is not None
        assert session.status == "ready"
        assert session.mode == "provided_only"
        assert session.config_hash == canonical_hash({
            "schema_ref": "meta-research/acquisition-session-config/v1",
            **effective_acquisition_config(config),
        })
        assert any("hc_runtime_condition_heads" in sql for sql in shared_statements)
        assert counts == {"databases": 0, "connections": 0, "closes": 0}
        with runtime._database.read_snapshot() as connection:
            saved = read_runtime_conditions_in_transaction(connection, quest_ref)
            historical = read_runtime_conditions_in_transaction(connection, quest_ref, revision=previous["revision"])
            assert saved["revision"] == current["revision"]
            assert saved["text"] == "Use newly supplied literature only."
            assert historical["text"] == previous["text"]
    finally:
        runtime.close()
