"""A sealed strategy may add closing prose without changing planned work."""
from copy import deepcopy

import pytest

from meta_research.bundle_target_contract import (
    BundleTargetContractError, apply_strategy_update, rolling_strategy_state_from_dict,
    rolling_strategy_state_to_dict, start_rolling_strategy, strategy_update_from_dict,
)
from meta_research.owners.common import OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_target_contract import _candidate, _contract, _update


def _sealed_strategy():
    _, contract = _contract()
    initial = strategy_update_from_dict(_update(1, [
        _candidate(contract, "target-a", "cell-a"),
        _candidate(contract, "target-b", "cell-b"),
    ], complete=True), completion_contract=contract)
    state = apply_strategy_update(start_rolling_strategy(contract), initial, completion_contract=contract)
    return contract, state


def test_sealed_strategy_accepts_only_prose_and_replays_with_frozen_candidates():
    contract, state = _sealed_strategy()
    update = strategy_update_from_dict({**_update(2, [], complete=True),
        "notes": "Results are retained; uncertainty remains; Reasoning should compare the next approaches."},
        completion_contract=contract)
    revised = apply_strategy_update(state, update, completion_contract=contract)
    assert revised.strategy.strategy_complete
    assert revised.candidates == state.candidates
    assert revised.strategy.revision == state.strategy.revision + 1
    assert rolling_strategy_state_from_dict(
        rolling_strategy_state_to_dict(revised, completion_contract=contract),
        completion_contract=contract, previous_state=state) == revised


@pytest.mark.parametrize("damage", ["new-candidate", "reopen", "no-notes", "blank-notes"])
def test_sealed_strategy_still_rejects_reopening_or_new_work(damage):
    contract, state = _sealed_strategy()
    document = {**_update(2, [], complete=True), "notes": "Closing synthesis."}
    if damage == "new-candidate":
        document["candidates"] = [_candidate(contract, "repeat-a", "cell-a")]
    elif damage == "reopen":
        document["strategy_complete"] = False
    elif damage == "no-notes":
        document.pop("notes")
    else:
        document["notes"] = "  "
    update = strategy_update_from_dict(document, completion_contract=contract)
    with pytest.raises(BundleTargetContractError, match="completed_strategy_is_immutable"):
        apply_strategy_update(state, update, completion_contract=contract)


def test_owner_appends_synthesis_to_sealed_graph_without_changing_targets(committed_targets):
    runtime, graph, original, _closures, source, projection = committed_targets
    rg, ar = runtime.owners.research_graph, runtime.owners.agent_runtime
    for _ in range(8):
        if graph.strategy_complete:
            break
        runtime.bundle_stage.process_once()
        graph = rg.query_target_graph(original.request_ref)
    assert graph.strategy_complete
    assert ar.query_bundle_run_report(original.run_ref) is None
    commits = rg.query_target_commits(graph.graph_ref)
    run = ar.query_bundle_stage_run(original.request_ref)
    previous = ar.query_bundle_target_proposals(run.run_ref)[-1]
    update = {**deepcopy(previous.strategy_update), "revision": graph.head_generation + 2,
        "candidates": [], "strategy_complete": True,
        "notes": "Results are complete. The scientific question remains unresolved. Reasoning should assess limitations and follow-up options."}

    def propose(value, key):
        return ar.record_bundle_target_proposal(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref, native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
            base_generation=graph.head_generation, base_head_receipt=graph.head_receipt,
            strategy_update=value, inbox_checkpoint=runtime.bundle_stage._drain_bundle_inbox(run),
            idempotency_key=key)

    rejected = propose({**update, "strategy_complete": False,
        "candidates": [deepcopy(graph.targets[0].spec)]}, "reopen-sealed")
    with pytest.raises(OwnerConflict, match="target_graph_strategy_complete"):
        rg.append_target_batch(graph_ref=graph.graph_ref, proposal_ref=rejected.proposal_ref,
            proposal=rejected.proposal, proposal_hash=rejected.proposal_hash, proposal_receipt=rejected.receipt)
    accepted = propose(update, "closing-synthesis")
    head = rg.append_target_batch(graph_ref=graph.graph_ref, proposal_ref=accepted.proposal_ref,
        proposal=accepted.proposal, proposal_hash=accepted.proposal_hash, proposal_receipt=accepted.receipt)
    reread = rg.query_target_graph(graph.request_ref)
    assert head.generation == graph.head_generation + 1
    assert reread.strategy_complete and reread.targets == graph.targets
    assert reread.target_set_hash == graph.target_set_hash
    assert rg.query_target_commits(graph.graph_ref) == commits
    assert rg.query_bundle_remaining_work(graph.graph_ref) == ()
    assert rg.append_target_batch(graph_ref=graph.graph_ref, proposal_ref=accepted.proposal_ref,
        proposal=accepted.proposal, proposal_hash=accepted.proposal_hash, proposal_receipt=accepted.receipt) == head
    contract = rg.query_bundle_report_contract(request_ref=graph.request_ref, run_ref=run.run_ref,
        graph_ref=graph.graph_ref, head_receipt=head.receipt,
        formal_plan_content_receipt=source.receipt, formal_plan_projection_receipt=projection.receipt)
    assert contract["notes"] == update["notes"]
    assert contract["notes_source_ref"] == accepted.proposal_ref
