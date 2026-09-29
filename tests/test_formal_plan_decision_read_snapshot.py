"""Formal Plan decision reads keep proofs fresh and never capture owner writes."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _confirm_direct_quest,
    _finish_idea_stage, _runtime,
)


def test_formal_plan_decision_read_cut_preserves_acceptance_and_integrity(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path / 'formal-decision', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(8):
            assert runtime.plan_stage.process_once()
            current = runtime.plan_stage.query_current()
            if current['plan_acceptance']['status'] == 'awaiting_domain':
                break
        assert current['plan_acceptance']['status'] == 'awaiting_domain'
        submission_ref = current['run']['submission_ref']
        graph, database = runtime.owners.research_graph, runtime._database
        read = graph._query_formal_plan_decision_from_current
        cuts, writes = [], []

        def inspect(ref):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return read(ref)

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                assert database._read_cut.get() is None and database._read_cache.get() is None
                writes.append(statement)

        monkeypatch.setattr(graph, '_query_formal_plan_decision_from_current', inspect)
        event.listen(database._engine, 'before_cursor_execute', observe)
        try:
            assert graph.query_formal_plan_decision(submission_ref) is None
            assert not writes
            before = len(cuts)
            assert runtime.plan_stage.process_once()
            assert writes and len(cuts) >= before + 2
            assert cuts[before][1] is not cuts[-1][1]
            expected = graph.query_formal_plan_decision(submission_ref)
            assert expected is not None and expected.decision == 'accepted'
            first_cache = cuts[-1][1]
            assert graph.query_formal_plan_decision(submission_ref) == expected
            assert cuts[-1][1] is not first_cache
            with database.read_snapshot() as connection:
                assert graph.query_formal_plan_decision(submission_ref) == expected
                assert cuts[-1][0] is connection
            count = len(writes)
            assert graph.query_formal_plan_decision('missing-submission') is None
            with database.read() as connection:
                object_path = connection.execute(text('SELECT object_path FROM rm_plan_documents WHERE submission_ref=:ref'),
                                                 {'ref': submission_ref}).scalar_one()
            original = runtime.owners.research_memory._object_store / object_path
            saved = original.read_bytes()
            with database.read_snapshot():
                assert graph.query_formal_plan_decision(submission_ref) == expected
                try:
                    original.write_bytes(saved + b' ')
                    with pytest.raises(OwnerConflict):
                        graph.query_formal_plan_decision(submission_ref)
                finally:
                    original.write_bytes(saved)
            verifier = graph._receipt_verifier
            verify = verifier.verify_formal_plan_decision
            monkeypatch.setattr(verifier, 'verify_formal_plan_decision', Mock(side_effect=OwnerConflict('proof_unavailable')))
            with pytest.raises(OwnerConflict, match='proof_unavailable'):
                graph.query_formal_plan_decision(submission_ref)
            assert database._read_cut.get() is None and database._read_cache.get() is None
            monkeypatch.setattr(verifier, 'verify_formal_plan_decision', verify)
            assert graph.query_formal_plan_decision(submission_ref) == expected
            assert len(writes) == count
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            event.remove(database._engine, 'before_cursor_execute', observe)
    finally:
        runtime.close()
