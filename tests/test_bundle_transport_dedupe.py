import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from meta_research import idea_skill
from meta_research.owners.common import canonical_hash
from meta_research.bundle_batch_delivery import verified_batch_delivery_baseline
from test_bundle_batch_delivery import _previous, _proposals
from test_bundle_input_drift_recovery import _output


def test_verified_inputs_reuse_prompt_and_schema_digests(tmp_path, monkeypatch):
    _, _, _, operation = _previous(tmp_path)
    invocation = json.loads((operation / 'invocation.json').read_text())['payload']
    prompt = (operation / 'prompt.txt').read_text(encoding='utf8')
    schema = json.loads((operation / 'output-schema.json').read_text())
    counts = Counter()

    def digest(value):
        if value == prompt:
            counts['prompt'] += 1
        if value == schema:
            counts['schema'] += 1
        return canonical_hash(value)

    monkeypatch.setattr(idea_skill, 'canonical_hash', digest)
    prompt_hash, schema_hash, limits = idea_skill._verified_operation_inputs(
        operation, invocation_hash=canonical_hash(invocation))
    assert prompt_hash == invocation['prompt_hash'] == canonical_hash(prompt)
    assert schema_hash == invocation['output_schema_hash'] == canonical_hash(schema)
    assert limits == idea_skill._operation_transport_limits(invocation)
    assert counts == {'prompt': 1, 'schema': 1}


def test_business_digest_uses_the_validated_canonical_bytes(tmp_path, monkeypatch):
    _, _, _, operation = _previous(tmp_path)
    invocation = json.loads((operation / 'invocation.json').read_text())['payload']
    expected = json.loads((operation / 'business-input.json').read_text(encoding='utf8'))
    calls = []

    def digest(value):
        calls.append(value)
        return canonical_hash(value)

    monkeypatch.setattr(idea_skill, 'canonical_hash', digest)
    actual = idea_skill._read_operation_business_input(
        operation, invocation, idea_skill._operation_transport_limits(invocation))
    assert actual == expected
    assert canonical_hash(actual) == invocation['operation_input_hash']
    assert not calls


def test_delivery_matches_all_accepted_proposals_in_each_generation(tmp_path, monkeypatch):
    from meta_research import bundle_batch_delivery as delivery

    adapter, runner, first, _ = _previous(tmp_path)
    output = deepcopy(_output('batch'))
    output['strategy_update']['revision'] = 3
    runner._outputs = iter([output])
    second = replace(first, base_generation=1, base_head_receipt={'receipt_ref': 'head:2'})
    adapter.propose_target_batch(second)
    earlier = _proposals(first)[0]
    later = SimpleNamespace(base_generation=1,
        base_head_receipt=SimpleNamespace(as_public_dict=lambda: second.base_head_receipt),
        strategy_update=output['strategy_update'], proposal_ref='proposal:later')
    same_generation = SimpleNamespace(**{**vars(later), 'proposal_ref': 'proposal:also-accepted'})
    wrong_head = SimpleNamespace(**{**vars(later), 'proposal_ref': 'proposal:wrong-head',
        'base_head_receipt': SimpleNamespace(as_public_dict=lambda: {'receipt_ref': 'foreign'})})
    proposals = (later, wrong_head, earlier, same_generation)
    checkpoints = {proposal.proposal_ref: first.inbox_checkpoint for proposal in proposals}
    calls = []

    def digest(value):
        if value is first.initial_target_plan:
            calls.append(value)
        return canonical_hash(value)

    monkeypatch.setattr(delivery, 'canonical_hash', digest)
    current = replace(second, base_generation=2)
    baseline = verified_batch_delivery_baseline(adapter, current,
        verified_proposals=proposals, verified_proposal_checkpoints=checkpoints)
    assert [item['operation_name'] for item in baseline['operations']] == ['target-batch-1', 'target-batch-2']
    assert [item['proposal_refs'] for item in baseline['operations']] == [
        ['proposal:accepted'], ['proposal:later', 'proposal:also-accepted']]
    assert baseline['current_targets'] == list(first.current_targets)
    assert baseline['target_commits'] == list(first.target_commits)
    assert len(calls) == 1
    assert verified_batch_delivery_baseline(adapter, current,
        verified_proposals=(later,), verified_proposal_checkpoints=checkpoints) is None
    assert len(runner.calls) == 2
