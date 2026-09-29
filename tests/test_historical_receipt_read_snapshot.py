"""Shared Plan/Reasoning ancestors are authenticated once per immutable read."""
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_plan_asset_target_input import _asset, _origin, _runtime
import test_public_bundle_stage as plan_fixtures
import test_public_reasoning_plan_evidence_reuse as reuse_fixtures
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill, _confirm_deepfetch_quest,
    _finish_idea_stage, _reasoning_runtime,
)


@pytest.fixture(params=['plan', 'reasoning'])
def accepted_decision(tmp_path, request):
    stage = request.param
    if stage == 'plan':
        runtime, plan, bundle = _runtime(tmp_path / stage)
        quest = plan_fixtures._confirm_direct_quest(runtime)
        asset = _asset(runtime, 'shared-source')
        _origin(runtime, asset, quest['quest_ref'], 'shared-source-origin')
        plan.source_ref = bundle.source_ref = asset.version_ref
        plan_fixtures._finish_idea_stage(runtime)
        plan_fixtures._finish_plan_stage(runtime)
        table, content_table = 'rg_formal_plan_decisions', 'rm_plan_documents'
    else:
        runtime = _reasoning_runtime(tmp_path / stage, reasoning_skill=_DeterministicReasoningSkill())
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(16):
            current = runtime.reasoning_stage.query_current()
            if current.get('stage_commit') is not None:
                break
            assert runtime.reasoning_stage.process_once()
        else:
            raise AssertionError('Reasoning did not reach its accepted stage commit')
        table, content_table = 'rg_reasoning_outcome_decisions', 'rm_reasoning_contents'
    try:
        with runtime._database.read() as connection:
            row = connection.execute(text(f"SELECT * FROM {table} WHERE decision='accepted' ORDER BY rowid DESC LIMIT 1")).first()
            content = connection.execute(text(f'SELECT object_path FROM {content_table} WHERE submission_ref=:submission'),
                {'submission': row.submission_ref}).scalar_one()
        graph = runtime.owners.research_graph
        query = graph.query_formal_plan_decision if stage == 'plan' else graph.query_reasoning_outcome_decision
        content_verifier = graph._receipt_verifier._plan_content_verifier if stage == 'plan' else graph._receipt_verifier._reasoning_content_verifier
        method = 'verify_plan_content_receipt' if stage == 'plan' else 'verify_reasoning_content_receipt'
        yield runtime, lambda: query(row.submission_ref), content_verifier, method, runtime.data_root.objects / content
    finally:
        runtime.close()


def test_repeated_ancestor_receipt_is_cached_only_in_same_read_cut(accepted_decision, monkeypatch):
    runtime, query, verifier, method, _path = accepted_decision
    probe = Mock(wraps=getattr(verifier, method))
    monkeypatch.setattr(verifier, method, probe)
    database = runtime._database
    with database.read_snapshot():
        expected = query()
        first_calls = probe.call_count
        assert first_calls > 0
        assert query() == expected
        assert query() == expected
        assert probe.call_count == first_calls
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.read_snapshot():
        assert query() == expected
    assert probe.call_count > first_calls
    fresh_calls = probe.call_count
    query()
    assert probe.call_count > fresh_calls


def test_failed_verification_retries_and_new_cut_detects_object_tamper(accepted_decision, monkeypatch):
    runtime, query, verifier, method, path = accepted_decision
    original = getattr(verifier, method)
    probe = Mock(side_effect=OwnerConflict('transient_evidence_unavailable'))
    monkeypatch.setattr(verifier, method, probe)
    database = runtime._database
    with database.read_snapshot():
        for _ in range(2):
            with pytest.raises(OwnerConflict, match='transient_evidence_unavailable'):
                query()
        assert probe.call_count == 2
        monkeypatch.setattr(verifier, method, original)
        expected = query()
    assert database._read_cut.get() is None and database._read_cache.get() is None
    original_bytes = path.read_bytes()
    try:
        path.write_bytes(original_bytes + b'corrupt')
        with database.read_snapshot():
            with pytest.raises(OwnerConflict, match='content_custody_unavailable'):
                query()
    finally:
        path.write_bytes(original_bytes)
    with database.read_snapshot():
        assert query() == expected


def test_same_cut_rechecks_content_and_retries_after_file_is_restored(accepted_decision):
    runtime, query, _verifier, _method, path = accepted_decision
    original = path.read_bytes()
    with runtime._database.read_snapshot():
        expected = query()
        try:
            path.write_bytes(original + b'corrupt')
            with pytest.raises(OwnerConflict, match='content_custody_unavailable'):
                query()
        finally:
            path.write_bytes(original)
        assert query() == expected


def test_reasoning_read_rechecks_transitive_plan_file_in_same_cut(tmp_path):
    authority = reuse_fixtures._EvidenceReuseAuthority()
    runtime = reuse_fixtures._runtime(
        tmp_path / 'reused-plan', authority, reuse_fixtures._MetricReuseReasoningSkill()
    )
    try:
        quest = reuse_fixtures._confirm_direct_quest(runtime)
        reuse_fixtures._install_fixture_plan_evidence(runtime, authority, quest_ref=quest['quest_ref'])
        reuse_fixtures._finish_idea_stage(runtime)
        reuse_fixtures._finish_plan_and_skipped_bundle(runtime)
        for _ in range(16):
            current = runtime.reasoning_stage.query_current()
            if current.get('stage_commit') is not None:
                break
            assert runtime.reasoning_stage.process_once()
        else:
            raise AssertionError('Reasoning did not commit its reused Plan')
        with runtime._database.read() as connection:
            plan = connection.execute(text('SELECT submission_ref, object_path FROM rm_plan_documents LIMIT 1')).one()
            submission = connection.execute(text("SELECT submission_ref FROM rg_reasoning_outcome_decisions WHERE decision='accepted' LIMIT 1")).scalar_one()
        graph = runtime.owners.research_graph
        path = runtime.data_root.objects / plan.object_path
        original = path.read_bytes()
        with runtime._database.read_snapshot():
            # Reasoning must inherit custody even when the Plan is a cache hit.
            assert graph.query_formal_plan_decision(plan.submission_ref) is not None
            expected = graph.query_reasoning_outcome_decision(submission)
            assert expected is not None
            try:
                path.write_bytes(original + b'corrupt')
                with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                    graph.query_reasoning_outcome_decision(submission)
            finally:
                path.write_bytes(original)
            assert graph.query_reasoning_outcome_decision(submission) == expected
    finally:
        runtime.close()


def test_successor_read_owns_a_fresh_cut_and_releases_it_on_failure(accepted_decision, monkeypatch):
    runtime, _query, _verifier, _method, _path = accepted_decision
    database = runtime._database
    owner = runtime.owners.advancement_engine
    with database.read() as connection:
        cycle_ref = connection.execute(text('SELECT cycle_ref FROM ae_cycles ORDER BY rowid DESC LIMIT 1')).scalar_one()
    original = owner._query_reasoning_successor_context
    cuts = []

    def read(cycle):
        assert database._read_cut.get() is not None
        cuts.append(database._read_cache.get())
        return original(cycle)

    monkeypatch.setattr(owner, '_query_reasoning_successor_context', read)
    write = Mock(wraps=database.write)
    monkeypatch.setattr(database, 'write', write)
    expected = owner.query_reasoning_successor_context(cycle_ref)
    assert owner.query_reasoning_successor_context(cycle_ref) == expected
    assert cuts[0] is not cuts[1]
    with database.read_snapshot() as connection:
        cache = database._read_cache.get()
        first = owner.query_reasoning_successor_context(cycle_ref)
        if first is not None:
            first['caller_mutation'] = True
        assert owner.query_reasoning_successor_context(cycle_ref) == expected
        assert cuts[-1] is cuts[-2] is cache
        assert database._read_cut.get() is connection
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None
    monkeypatch.setattr(owner, '_query_reasoning_successor_context', Mock(side_effect=OwnerConflict('bad_successor')))
    with pytest.raises(OwnerConflict, match='bad_successor'):
        owner.query_reasoning_successor_context(cycle_ref)
    assert database._read_cut.get() is None and database._read_cache.get() is None
