"""Advancement reuses projection proofs only inside its own immutable read."""
from dataclasses import replace
from time import perf_counter
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report


def test_direct_advancement_scopes_all_evidence_and_revalidates_after_exit(committed_targets, monkeypatch):
    runtime, graph, run, accepted = _accept_report(committed_targets)
    database = runtime._database
    owner = runtime.owners.advancement_engine
    request = owner._query_stage_request_by_ref(run.request_ref)
    authority = runtime.target_run_authorities.research_graph
    plan = Mock(wraps=authority._current_formal_plan_facts)
    candidate = Mock(wraps=authority._current_candidate_projection_facts)
    monkeypatch.setattr(authority, '_current_formal_plan_facts', plan)
    monkeypatch.setattr(authority, '_current_candidate_projection_facts', candidate)
    write = Mock(wraps=database.write)
    monkeypatch.setattr(database, 'write', write)

    values = dict(request=request, run_ref=run.run_ref,
        bundle_report_ref=accepted.report_ref, bundle_report_receipt=accepted.receipt)
    assert database._read_cut.get() is None and database._read_cache.get() is None
    started = perf_counter()
    assert owner._verify_bundle_report_for_advancement(**values) == accepted
    print(f'ADVANCEMENT_READ seconds={perf_counter() - started:.6f} '
        f'plan_facts={plan.call_count} candidate_facts={candidate.call_count}', flush=True)
    # The AR receipt read and AE's extra contract/Target verification share a cut.
    assert plan.call_count == 1
    assert candidate.call_count == len(graph.targets)
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None

    plan.reset_mock()
    candidate.reset_mock()
    with database.read_snapshot() as connection:
        cache = database._read_cache.get()
        assert owner._verify_bundle_report_for_advancement(**values) == accepted
        assert owner._verify_bundle_report_for_advancement(**values) == accepted
        assert database._read_cut.get() is connection
        assert database._read_cache.get() is cache
        assert plan.call_count == 1 and candidate.call_count == len(graph.targets)
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # A failure in AE's own checks, after the AR nested read, releases the cut.
    with pytest.raises(OwnerConflict, match='bundle_report_advancement_binding_invalid'):
        owner._verify_bundle_report_for_advancement(**{**values,
            'request': replace(request, request_ref='stage_request:wrong')})
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # New writes remain outside the helper; a later read must verify new facts.
    target_ref = graph.targets[0].target_ref
    with database.write() as connection:
        original = connection.execute(text('SELECT source_spec_hash FROM rg_target_candidate_projections WHERE target_ref=:target'), {'target': target_ref}).scalar_one()
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': '0' * 64, 'target': target_ref})
    with pytest.raises(OwnerConflict):
        owner._verify_bundle_report_for_advancement(**values)
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': original, 'target': target_ref})
    plan.reset_mock()
    candidate.reset_mock()
    assert owner._verify_bundle_report_for_advancement(**values) == accepted
    assert plan.call_count == 1 and candidate.call_count == len(graph.targets)
    assert database._read_cut.get() is None and database._read_cache.get() is None
