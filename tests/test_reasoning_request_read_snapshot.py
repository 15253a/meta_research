"""A real Reasoning request verifies its completed route in one read cut."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report
from test_public_reasoning_stage import _DeterministicReasoningSkill


def test_reasoning_request_read_reuses_route_proofs_and_revalidates_next_cut(committed_targets, monkeypatch):
    runtime, graph, bundle_run, accepted = _accept_report(committed_targets)
    agent = runtime.owners.agent_runtime
    owner = runtime.owners.advancement_engine
    completion = agent.complete_bundle_run(run_ref=bundle_run.run_ref,
        attempt_ref=bundle_run.attempt_ref, fence_ref=bundle_run.fence_ref,
        report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key='reasoning-cut-complete-bundle')
    owner.commit_bundle_stage(request_ref=bundle_run.request_ref,
        run_ref=bundle_run.run_ref, bundle_report_ref=accepted.report_ref,
        run_completion_receipt=completion.receipt, bundle_report_receipt=accepted.receipt,
        idempotency_key='reasoning-cut-advance-bundle')
    bundle_request = owner._query_stage_request_by_ref(bundle_run.request_ref)
    request = owner.ensure_reasoning_stage_request(cycle_ref=bundle_request.cycle_ref,
        accepted_question=bundle_request.accepted_question,
        idempotency_key='reasoning-cut-request')
    assert request.stage == 'reasoning'
    assert len(request.context_pack['accepted_target_commit_closures']) == 2

    database = runtime._database
    authority = runtime.target_run_authorities.research_graph
    plan = Mock(wraps=authority._current_formal_plan_facts)
    candidate = Mock(wraps=authority._current_candidate_projection_facts)
    monkeypatch.setattr(authority, '_current_formal_plan_facts', plan)
    monkeypatch.setattr(authority, '_current_candidate_projection_facts', candidate)
    write = Mock(wraps=database.write)
    monkeypatch.setattr(database, 'write', write)
    assert database._read_cut.get() is None and database._read_cache.get() is None

    started = perf_counter()
    assert owner.query_reasoning_stage_request(request.cycle_ref) == request
    print(f'REASONING_REQUEST_READ seconds={perf_counter() - started:.6f} '
        f'plan_facts={plan.call_count} candidate_facts={candidate.call_count}', flush=True)
    assert plan.call_count == 1
    assert candidate.call_count == len(graph.targets)
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None

    plan.reset_mock()
    candidate.reset_mock()
    with database.read_snapshot() as connection:
        cache = database._read_cache.get()
        assert owner.query_reasoning_stage_request(request.cycle_ref) == request
        owner._verify_reasoning_request_closure(request)
        assert database._read_cut.get() is connection
        assert database._read_cache.get() is cache
        assert plan.call_count == 1 and candidate.call_count == len(graph.targets)
    write.assert_not_called()
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # A failure after rebuilding the upstream route also releases the snapshot.
    damaged_context = deepcopy(request.context_pack)
    damaged_context['accepted_target_commit_closures'] = []
    with pytest.raises(OwnerConflict, match='reasoning_upstream_closure_stale'):
        owner._verify_reasoning_request_closure(replace(request, context_pack=damaged_context))
    assert database._read_cut.get() is None and database._read_cache.get() is None

    target_ref = graph.targets[0].target_ref
    with database.write() as connection:
        original = connection.execute(text('SELECT source_spec_hash FROM rg_target_candidate_projections WHERE target_ref=:target'), {'target': target_ref}).scalar_one()
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': '0' * 64, 'target': target_ref})
    with pytest.raises(OwnerConflict):
        owner.query_reasoning_stage_request(request.cycle_ref)
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE rg_target_candidate_projections SET source_spec_hash=:hash WHERE target_ref=:target'), {'hash': original, 'target': target_ref})
    plan.reset_mock()
    candidate.reset_mock()
    assert owner.query_reasoning_stage_request(request.cycle_ref) == request
    assert plan.call_count == 1 and candidate.call_count == len(graph.targets)
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # The verified request proceeds through real admission after the read ends.
    agent.admit_reasoning_stage(request, 'reasoning-cut-admit',
        runtime_binding=_DeterministicReasoningSkill().runtime_binding())
    admitted = agent.query_reasoning_stage_run(request.request_ref)
    assert admitted is not None and admitted.request_ref == request.request_ref
    assert admitted.execution is None
    assert database._read_cut.get() is None and database._read_cache.get() is None
