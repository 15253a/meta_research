"""One Reasoning reconstruction authenticates its completed Bundle once."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report


def test_reasoning_reconstruction_consumes_one_verified_bundle_route(committed_targets, monkeypatch):
    runtime, _graph, bundle_run, report = _accept_report(committed_targets)
    agent = runtime.owners.agent_runtime
    owner = runtime.owners.advancement_engine
    completed = agent.complete_bundle_run(
        run_ref=bundle_run.run_ref, attempt_ref=bundle_run.attempt_ref,
        fence_ref=bundle_run.fence_ref, report_ref=report.report_ref,
        decision_receipt=report.receipt, idempotency_key='single-route-complete')
    commit = owner.commit_bundle_stage(
        request_ref=bundle_run.request_ref, run_ref=bundle_run.run_ref,
        bundle_report_ref=report.report_ref, run_completion_receipt=completed.receipt,
        bundle_report_receipt=report.receipt, idempotency_key='single-route-commit')
    bundle_request = owner._query_stage_request_by_ref(bundle_run.request_ref)
    request = owner.ensure_reasoning_stage_request(
        cycle_ref=bundle_request.cycle_ref,
        accepted_question=bundle_request.accepted_question,
        idempotency_key='single-route-request')
    assert len(request.context_pack['accepted_target_commit_closures']) == 2
    stage_read = Mock(wraps=owner._stage_commit_from_row)
    monkeypatch.setattr(owner, '_stage_commit_from_row', stage_read)

    assert owner.query_reasoning_stage_request(request.cycle_ref) == request
    counts = Counter(call.args[0].commit_ref for call in stage_read.call_args_list)
    assert counts[commit.commit_ref] == 1, counts

    # Reuse stays inside this reconstruction: a new call authenticates again.
    stage_read.reset_mock()
    with runtime._database.read_snapshot():
        owner._verify_reasoning_request_closure(request)
        owner._verify_reasoning_request_closure(request)
    counts = Counter(call.args[0].commit_ref for call in stage_read.call_args_list)
    assert counts[commit.commit_ref] == 2, counts

    # Removing the repeated pass must not admit an altered frozen context.
    context = deepcopy(request.context_pack)
    context['accepted_target_commit_closures'] = []
    with pytest.raises(OwnerConflict, match='reasoning_upstream_closure_stale'):
        owner._verify_reasoning_request_closure(replace(request, context_pack=context))

    def reject_bundle(row):
        if row.commit_ref == commit.commit_ref:
            raise OwnerConflict('single_route_issuer_rejected')
        return stage_read._mock_wraps(row)

    monkeypatch.setattr(owner, '_stage_commit_from_row', reject_bundle)
    with pytest.raises(OwnerConflict, match='single_route_issuer_rejected'):
        owner.query_reasoning_stage_request(request.cycle_ref)
