"""Regression at the real AR issuance -> RG append and historical-read seam."""
from dataclasses import replace
import time

import pytest
from sqlalchemy import text

from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import (
    _TwoGapPlanSkill, _bundle_runtime, _confirm_direct_quest,
    _finish_idea_stage, _finish_plan_stage, _formal_candidate,
)


def replace_attempt(runtime, request_ref):
    """Exercise the production replacement routine at its transaction seam."""
    owner = runtime.owners.agent_runtime
    old = owner.query_bundle_stage_run(request_ref)
    now = time.time()
    with runtime._database.write() as connection:
        control = connection.execute(text('SELECT * FROM ar_run_controls WHERE run_ref = :run'), {'run': old.run_ref}).one()
        connection.execute(text("UPDATE ar_execution_fences SET status = 'rejected', closed_at = :now WHERE fence_ref = :fence"), {'now': now, 'fence': old.fence_ref})
        successor = owner._replace_fenced_managed_attempt(
            connection, control, now, reuse_checkpoint=True,
            reuse_operation_refs=False, preserve_native_session=True,
            replacement_reason_code='provider_result_correction',
        )
        connection.execute(text("UPDATE ar_run_controls SET attempt_ref = :attempt, fence_ref = :fence, status = 'running', control_revision = control_revision + 1 WHERE run_ref = :run"), {'attempt': successor.attempt_ref, 'fence': successor.fence_ref, 'run': old.run_ref})
    current = owner.query_bundle_stage_run(request_ref)
    assert current.attempt_ref != old.attempt_ref
    return current


@pytest.fixture
def successor_proposal(tmp_path):
    runtime = _bundle_runtime(tmp_path/'successor-append', plan_skill_provider=_TwoGapPlanSkill())
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _ in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current['target_graph']['status'] == 'accepted':
                break
        request_ref = current['stage_run_request']['request_ref']
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        run = replace_attempt(runtime, request_ref)
        for _ in range(5):
            if run.execution is not None:
                break
            assert runtime.bundle_stage.process_once()
            run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run.execution is not None
        checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
        plan = current['stage_run_request']['accepted_formal_plan_binding']['plan_document']
        key = plan['experiment_briefs'][1]['experiment_key']
        spec = _formal_candidate(completion_document=graph.target_plan['completion_contract'], label='successor-followup', experiment_key=key, cell=f'measurement:{key}', depends_on=())
        proposal = runtime.owners.agent_runtime.record_bundle_target_proposal(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
            base_generation=graph.head_generation, base_head_receipt=graph.head_receipt,
            strategy_update={'schema_ref': FORMAL_STRATEGY_UPDATE_SCHEMA_REF, 'revision': graph.head_generation + 2, 'candidates': [spec], 'requires_accepted_labels': [], 'strategy_complete': True},
            inbox_checkpoint=checkpoint, idempotency_key='successor-proposal',
        )
        assert graph.attempt_ref != proposal.attempt_ref
        yield runtime, request_ref, graph, proposal
    finally:
        runtime.close()


def append(runtime, graph, proposal):
    return runtime.owners.research_graph.append_target_batch(
        graph_ref=graph.graph_ref, proposal_ref=proposal.proposal_ref,
        proposal=proposal.proposal, proposal_hash=proposal.proposal_hash,
        proposal_receipt=proposal.receipt,
    )


def test_successor_append_reread_replay_and_later_replacement(successor_proposal):
    runtime, request_ref, origin, proposal = successor_proposal
    head = append(runtime, origin, proposal)
    assert head.generation == 1
    assert append(runtime, origin, proposal) == head
    replace_attempt(runtime, request_ref)
    # An accepted historical append remains valid after a further replacement.
    graph = runtime.owners.research_graph.query_target_graph(request_ref)
    assert graph.head_receipt == head.receipt
    assert len(graph.targets) == 2
    assert graph.attempt_ref == origin.attempt_ref
    assert graph.fence_ref == origin.fence_ref


@pytest.mark.parametrize('mutation', ['receipt', 'run', 'attempt', 'fence', 'graph', 'payload'])
def test_successor_receipt_rejects_wrong_binding(successor_proposal, mutation):
    runtime, _request_ref, graph, proposal = successor_proposal
    values = dict(proposal_ref=proposal.proposal_ref, run_ref=proposal.run_ref, graph_ref=graph.graph_ref, base_generation=0, base_head_receipt=graph.head_receipt, proposal_hash=proposal.proposal_hash, receipt=proposal.receipt)
    if mutation == 'receipt':
        values['receipt'] = replace(proposal.receipt, payload_hash='f'*64)
    elif mutation == 'payload':
        values['proposal_hash'] = 'f'*64
    else:
        values[mutation+'_ref'] = 'wrong-'+mutation
    with pytest.raises(OwnerConflict, match='bundle_target_proposal_receipt_invalid'):
        runtime.owners.agent_runtime.verify_bundle_target_proposal_receipt(**values)
    assert runtime.owners.research_graph.query_target_graph(graph.request_ref).head_generation == 0


@pytest.mark.parametrize('mutation', ['new_notice', 'retired_attempt'])
def test_unaccepted_successor_proposal_must_still_be_current(successor_proposal, mutation):
    runtime, request_ref, graph, proposal = successor_proposal
    if mutation == 'new_notice':
        with runtime._database.write() as connection:
            connection.execute(text('UPDATE ar_bundle_inbox_scopes SET wake_pending = 1 WHERE run_ref = :run'), {'run': proposal.run_ref})
    else:
        replace_attempt(runtime, request_ref)
    with pytest.raises(OwnerConflict, match='bundle_inbox_.*stale'):
        append(runtime, graph, proposal)
    assert runtime.owners.research_graph.query_target_graph(request_ref).head_generation == 0
