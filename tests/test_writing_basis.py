"""A writing basis stays consistent while research continues to advance."""
from datetime import datetime, timezone
from types import SimpleNamespace

from meta_research.database import Database
from meta_research.research_overview import ResearchOverviewReader


def test_writing_basis_keeps_one_observation_across_concurrent_owner_changes(tmp_path):
    database = Database(tmp_path / "research.sqlite3")
    concurrent = Database(database.path)
    try:
        with database.write() as connection:
            connection.exec_driver_sql("CREATE TABLE research (goal TEXT, question_ref TEXT)")
            connection.exec_driver_sql("INSERT INTO research VALUES ('initial goal', 'initial-question')")

        class Graph:
            _database = database
            advanced = False

            def query_quest_by_ref(self, quest_ref):
                with database.read() as connection:
                    goal = connection.exec_driver_sql("SELECT goal FROM research").scalar_one()
                if not self.advanced:
                    # A different writer commits after the first Owner read.
                    with concurrent.write() as connection:
                        connection.exec_driver_sql(
                            "UPDATE research SET goal='later goal', question_ref='later-question'")
                    self.advanced = True
                return SimpleNamespace(draft={"goal": goal})

        class Advancement:
            def query_foreground(self, quest_ref):
                with database.read() as connection:
                    question = connection.exec_driver_sql("SELECT question_ref FROM research").scalar_one()
                return {"question_ref": question}

            def query_quest_stage_history_display(self, quest_ref):
                return []

        reader = ResearchOverviewReader(Graph(), Advancement(), None, None)
        before = datetime.now(timezone.utc)
        basis = reader.writing_context("quest")
        after = datetime.now(timezone.utc)
        assert basis["goal"] == "initial goal"
        assert basis["question_ref"] == "initial-question"
        assert before <= datetime.fromisoformat(basis["basis_observed_at"]) <= after
        assert basis["availability"] == "no_accepted_scientific_result"
        # The snapshot ends with preparation; future writing can see new work.
        following = reader.writing_context("quest")
        assert following["goal"] == "later goal"
        assert following["question_ref"] == "later-question"
    finally:
        concurrent.close()
        database.close()
