from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS

from meta_research.bundle_batch_delivery import verified_batch_delivery_baseline
from meta_research.owners.common import canonical_hash
from test_bundle_input_drift_recovery import _adapter, _request, _output


def _sequence(tmp_path, monkeypatch):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    outputs = []
    for generation in range(5):
        output = _output("batch")
        output["strategy_update"]["revision"] = generation + 2
        outputs.append(output)
    runner._outputs = iter(outputs)
    request = _request(adapter, "batch")
    shown = {}
    def render(current, prompt_context, **_):
        value = deepcopy(prompt_context)
        for name in ("current_targets", "target_commits"):
            value[name] = deepcopy(shown.get(name, list(getattr(current, name))))
        return value
    monkeypatch.setattr("meta_research.bundle_batch_view.render_delta_batch_reading_context", render)
    return adapter, runner, request, shown


def _proposal(request):
    ref = "proposal:" + str(request.base_generation)
    strategy = _output("batch")["strategy_update"]
    strategy["revision"] = request.base_generation + 2
    return NS(base_generation=request.base_generation,
        base_head_receipt=NS(as_public_dict=lambda: request.base_head_receipt),
        strategy_update=strategy, proposal_ref=ref)


def _basis(adapter, current, previous):
    proposals = tuple(_proposal(request) for request in previous)
    return verified_batch_delivery_baseline(adapter, current, verified_proposals=proposals,
        verified_proposal_checkpoints={proposal.proposal_ref: request.inbox_checkpoint
            for proposal, request in zip(proposals, previous)})


def test_reverted_fact_is_not_marked_delivered_by_older_history(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    original = {**first.target_commits[0], "result_disposition": "missing", "exact_result": "A"}
    revised = {**original, "result_disposition": "failed", "exact_result": "B"}
    first = replace(first, target_commits=(original,))
    adapter.propose_target_batch(first)
    second = replace(first, base_generation=1, target_commits=(revised,))
    adapter.propose_target_batch(second)
    third = replace(second, base_generation=2, target_commits=(original,))
    baseline = _basis(adapter, third, (first, second))
    assert baseline["target_commits"] == [revised]
    assert original not in baseline["target_commits"]
    assert len(runner.calls) == 2
    assert canonical_hash(original) != canonical_hash(revised)


def test_three_turns_retain_proof_for_unchanged_indirect_history(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    adapter.propose_target_batch(first)
    shown.update(current_targets=[], target_commits=[])
    second = replace(first, base_generation=1)
    third = replace(first, base_generation=2)
    adapter.propose_target_batch(second)
    adapter.propose_target_batch(third)
    before = {path: path.read_bytes() for path in (tmp_path / "provider/provider-operations").rglob("*") if path.is_file()}
    baseline = _basis(adapter, replace(first, base_generation=3), (first, second, third))
    assert baseline["current_targets"] == list(first.current_targets)
    assert baseline["target_commits"] == list(first.target_commits)
    assert [operation["operation_name"] for operation in baseline["operations"]] == ["target-batch-1", "target-batch-2", "target-batch-3"]
    assert len(runner.calls) == 3
    assert {path: path.read_bytes() for path in before} == before


def test_full_business_input_without_shown_body_is_not_delivery(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    shown.update(current_targets=[], target_commits=[])
    adapter.propose_target_batch(first)
    baseline = _basis(adapter, replace(first, base_generation=1), (first,))
    assert baseline["current_targets"] == []
    assert baseline["target_commits"] == []
    assert len(runner.calls) == 1


def test_latest_success_without_acceptance_does_not_use_older_basis(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    adapter.propose_target_batch(first)
    second = replace(first, base_generation=1)
    adapter.propose_target_batch(second)
    assert _basis(adapter, replace(first, base_generation=2), (first,)) is None
    assert len(runner.calls) == 2


def test_same_strategy_under_other_inbox_is_not_a_delivery_basis(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    adapter.propose_target_batch(first)
    accepted = _proposal(first)
    baseline = verified_batch_delivery_baseline(adapter, replace(first, base_generation=1),
        verified_proposals=(accepted,), verified_proposal_checkpoints={accepted.proposal_ref:
            {**first.inbox_checkpoint, "checkpoint_ref": "a-different-frozen-cut"}})
    assert baseline is None
    assert len(runner.calls) == 1
