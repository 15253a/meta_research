from copy import deepcopy

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.owners.research_graph import TARGET_COMMIT_RECEIPT_KIND
from meta_research.reasoning_contract import (
    ReasoningContractError,
    current_target_evidence_leaves,
    plan_evidence_reuse_leaves,
)
from test_reasoning_contract import _plan_reuse_context, _plan_reuse_leaf


def _current_context(*, receipt_kind=TARGET_COMMIT_RECEIPT_KIND, evidence_order=None):
    # The live request freezes T4/T3/T2/T1/T5; their independent evidence IDs
    # are not in lexical order. These are role/receipt shapes, not model scores.
    evidence_order = evidence_order or ['evidence:dc0f', 'evidence:5cd4', 'evidence:4005', 'evidence:d405', 'evidence:4c53']
    leaves = []
    for ordinal, evidence_ref in enumerate(evidence_order):
        target_ref = f'target-commit:{ordinal}'
        attempt_ref = f'evaluation-attempt:{ordinal}'
        leaf = _plan_reuse_leaf(
            role='MetricResult', item_ref=f'metric-result:{ordinal}',
            role_ref=f'result-role:{ordinal}', asset_version_ref=f'asset-version:{ordinal}',
        )
        leaf.update(evidence_ref=evidence_ref, target_commit_ref=target_ref,
                    source_variant_run_ref=f'variant-run:{ordinal}',
                    source_evaluation_attempt_ref=attempt_ref, source_subject_ref=attempt_ref,
                    evidence_use_hashes=[])
        leaf['formal_measurement_acceptance_receipt']['subject_ref'] = attempt_ref
        leaf['formal_measurement_acceptance_receipt']['receipt_ref'] = f'measurement-receipt:{ordinal}'
        leaf['target_commit_acceptance_receipt'].update(
            kind=receipt_kind, subject_ref=target_ref, receipt_ref=f'target-receipt:{ordinal}')
        leaves.append(leaf)
    return {
        'accepted_target_commit_closures': [{'target_commit_ref': leaf['target_commit_ref']} for leaf in leaves],
        'current_target_evidence_closure': leaves,
    }


def test_plan_reuse_accepts_actual_rg_target_receipt_kind_without_changing_legacy():
    legacy = _plan_reuse_context()
    current = deepcopy(legacy)
    for leaf in current['plan_evidence_input']['evidence_reuse_closure']:
        leaf['target_commit_acceptance_receipt']['kind'] = TARGET_COMMIT_RECEIPT_KIND
    assert plan_evidence_reuse_leaves(current) == plan_evidence_reuse_leaves(legacy)


def test_current_targets_normalize_evidence_order_independent_of_receipt_kind():
    context = _current_context(receipt_kind='target_commit', evidence_order=['evidence:z', 'evidence:a'])
    assert [leaf['ref'] for leaf in current_target_evidence_leaves(context)] == ['metric-result:1', 'metric-result:0']


def test_five_current_targets_with_real_receipt_kind_and_unsorted_ids_keep_frozen_input():
    context = _current_context()
    before = deepcopy(context)
    before_hash = canonical_hash(context)
    result = current_target_evidence_leaves(context)
    assert [leaf['ref'] for leaf in result] == ['metric-result:2', 'metric-result:4', 'metric-result:1', 'metric-result:3', 'metric-result:0']
    assert {leaf['research_graph_acceptance_receipt_ref'] for leaf in result} == {f'target-receipt:{ordinal}' for ordinal in range(5)}
    assert context == before
    assert canonical_hash(context) == before_hash


@pytest.mark.parametrize(('field', 'value'), [
    ('issuer', 'research_memory'), ('kind', 'target_root_completion_accepted'),
    ('kind', 'not_a_target_commit'), ('subject_ref', 'target-commit:other'),
    ('payload_hash', ''), ('status', 'rejected'),
])
def test_current_target_receipt_corruption_still_fails(field, value):
    context = _current_context()
    context['current_target_evidence_closure'][0]['target_commit_acceptance_receipt'][field] = value
    with pytest.raises(ReasoningContractError, match='reasoning_target_evidence_closure_invalid'):
        current_target_evidence_leaves(context)


@pytest.mark.parametrize('corruption', ['missing_target', 'duplicate_metric', 'duplicate_target', 'unexpected_use_hash'])
def test_current_target_coverage_roles_and_use_hashes_remain_strict(corruption):
    context = _current_context()
    leaves = context['current_target_evidence_closure']
    if corruption == 'missing_target':
        leaves.pop()
    elif corruption == 'duplicate_metric':
        duplicate = deepcopy(leaves[0])
        duplicate['evidence_item_ref'] = 'metric-result:second-for-same-target'
        leaves.append(duplicate)
    elif corruption == 'duplicate_target':
        context['accepted_target_commit_closures'].append(deepcopy(context['accepted_target_commit_closures'][0]))
    else:
        leaves[0]['evidence_use_hashes'] = ['f' * 64]
    with pytest.raises(ReasoningContractError, match='reasoning_target_evidence_closure_invalid'):
        current_target_evidence_leaves(context)


def test_plan_reuse_still_rejects_an_incorrect_use_hash():
    context = _plan_reuse_context()
    context['plan_evidence_input']['evidence_reuse_closure'][0]['evidence_use_hashes'] = ['f' * 64]
    with pytest.raises(ReasoningContractError, match='reasoning_plan_evidence_closure_invalid'):
        plan_evidence_reuse_leaves(context)
