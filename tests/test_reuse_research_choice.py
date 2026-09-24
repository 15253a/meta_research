from dataclasses import replace

import pytest

from meta_research.bundle_completion import verify_reuse_trace
from meta_research.bundle_protocol import BundleProtocolError
from meta_research.bundle_target_contract import formal_target_candidate_from_dict
from test_bundle_completion_contract import _candidate as projection_candidate
from test_bundle_target_contract import _candidate, _contract


def test_selected_exact_method_does_not_require_proving_every_search_tier():
    candidate = projection_candidate('study', ('question-work',), ('comparison',))
    trace = replace(candidate.reuse_trace, greenfield_exception=None)
    refs = verify_reuse_trace(trace, candidate.implementation_revision_ref)
    assert 'source-version-study' in refs
    with pytest.raises(BundleProtocolError, match='executed revision'):
        verify_reuse_trace(trace, 'different-method')


def test_real_candidate_accepts_local_choice_without_empty_tier_paperwork():
    _, contract = _contract()
    document = _candidate(contract, 'chosen', 'cell-a')
    trace = document['candidate']['reuse_trace']
    trace['tier_decisions'] = [item for item in trace['tier_decisions'] if item['disposition'] == 'selected']
    trace['greenfield_exception'] = None
    result = formal_target_candidate_from_dict(document, completion_contract=contract)
    assert result.candidate.local_label == 'chosen'
