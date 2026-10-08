"""Research reuse is an exact input choice, not a search-tier acceptance gate.

ReuseTrace and verify_reuse_trace were removed in the verified 8768 baseline
(8633205). Keep checking the current formal candidate contract: the selected
implementation and input assets survive parsing without tier paperwork.
"""

import pytest

from meta_research.bundle_target_contract import (
    BundleTargetContractError,
    formal_target_candidate_from_dict,
    formal_target_candidate_to_dict,
)
from test_bundle_target_contract import _candidate, _contract


def test_selected_exact_method_does_not_require_proving_every_search_tier() -> None:
    _, contract = _contract()
    document = _candidate(contract, "chosen", "cell-a")
    # This shared historical fixture still carries the retired projection.
    del document["candidate"]["reuse_trace"]

    result = formal_target_candidate_from_dict(document, completion_contract=contract)
    restored = formal_target_candidate_to_dict(result, completion_contract=contract)

    assert result.candidate.local_label == "chosen"
    assert result.candidate.implementation_revision_ref == "implementation-chosen"
    assert result.candidate.direct_accepted_input_asset_refs == ("asset-chosen",)
    assert restored == document
    assert "reuse_trace" not in restored["candidate"]


def test_real_candidate_rejects_obsolete_search_tier_paperwork() -> None:
    _, contract = _contract()
    document = _candidate(contract, "chosen", "cell-a")

    with pytest.raises(BundleTargetContractError, match="target_candidate_invalid"):
        formal_target_candidate_from_dict(document, completion_contract=contract)


@pytest.mark.parametrize("selected_revision", ("", " "))
def test_local_reuse_choice_still_requires_an_exact_implementation_revision(
    selected_revision: str,
) -> None:
    _, contract = _contract()
    document = _candidate(contract, "chosen", "cell-a")
    del document["candidate"]["reuse_trace"]
    document["candidate"]["implementation_revision_ref"] = selected_revision

    with pytest.raises(BundleTargetContractError, match="ImplementationRevisionRef"):
        formal_target_candidate_from_dict(document, completion_contract=contract)
