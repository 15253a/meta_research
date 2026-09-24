import pytest

from meta_research.reasoning_contract import ReasoningContractError, current_target_evidence_leaves


def test_completed_work_without_evaluation_now_requires_its_work_product_leaf():
    """ADR 0005: unmeasured accepted work is adoptable evidence, so its
    closure must carry the issuer-closed WorkProduct leaf instead of being
    silently dropped from the Cycle's evidence."""
    context = {
        'accepted_target_commit_closures': [{
            'target_commit_ref': 'accepted-run-only-work', 'formal_measurement_accepted': False,
            'metric_result_ref': None, 'evaluation_attempt_ref': None,
            'rg_formal_measurement_receipt': None}],
        'current_target_evidence_closure': [],
    }
    with pytest.raises(ReasoningContractError, match='reasoning_target_evidence_closure_invalid'):
        current_target_evidence_leaves(context)


def test_evaluated_target_cannot_silently_lose_its_scientific_evidence():
    with pytest.raises(ReasoningContractError, match='reasoning_target_evidence_closure_invalid'):
        current_target_evidence_leaves({
            'accepted_target_commit_closures': [{'target_commit_ref': 'evaluated-target', 'formal_measurement_accepted': True}],
            'current_target_evidence_closure': []})


def test_unassessed_work_does_not_bypass_duplicate_target_identity_validation():
    item = {'target_commit_ref': 'accepted-work', 'formal_measurement_accepted': False}
    with pytest.raises(ReasoningContractError, match='reasoning_target_evidence_closure_invalid'):
        current_target_evidence_leaves({'accepted_target_commit_closures': [item, dict(item)],
                                        'current_target_evidence_closure': []})
