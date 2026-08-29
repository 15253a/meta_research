from __future__ import annotations

from copy import deepcopy

import pytest

from meta_research.reasoning_contract import (
    ReasoningContractError,
    validate_reasoning_transition,
)
from test_reasoning_contract import _next_cycle_proposal, _scientific_outcome


def _routed_next_cycle(entry_stage: str) -> dict[str, object]:
    proposal = deepcopy(_next_cycle_proposal())
    order = ("idea", "plan", "bundle", "reasoning")
    proposal["entry_stage"] = entry_stage
    proposal["typed_skip_basis_refs_by_stage"] = {
        stage: [str(proposal["source_scientific_outcome_ref"])]
        for stage in order[: order.index(entry_stage)]
    }
    return proposal


@pytest.mark.parametrize("entry_stage", ("idea", "plan", "bundle", "reasoning"))
def test_next_cycle_v1_closes_exact_typed_successor_route(
    entry_stage: str,
) -> None:
    outcome = _scientific_outcome()
    proposal = _routed_next_cycle(entry_stage)

    transition_hash = validate_reasoning_transition(
        outcome,
        next_cycle=proposal,
        candidate_completion=None,
    )

    assert transition_hash


def test_next_cycle_rejects_missing_or_partial_typed_route() -> None:
    outcome = _scientific_outcome()
    missing = _next_cycle_proposal()
    del missing["entry_stage"]
    del missing["typed_skip_basis_refs_by_stage"]
    with pytest.raises(ReasoningContractError, match="next_cycle_proposal_invalid"):
        validate_reasoning_transition(
            outcome,
            next_cycle=missing,
            candidate_completion=None,
        )

    partial = _routed_next_cycle("reasoning")
    del partial["typed_skip_basis_refs_by_stage"]["plan"]
    with pytest.raises(
        ReasoningContractError,
        match="next_cycle_proposal_skip_basis_invalid",
    ):
        validate_reasoning_transition(
            outcome,
            next_cycle=partial,
            candidate_completion=None,
        )


def test_next_cycle_domain_validator_rejects_duplicate_skip_basis_after_decode() -> None:
    outcome = _scientific_outcome()
    proposal = _routed_next_cycle("plan")
    basis_ref = proposal["source_scientific_outcome_ref"]
    proposal["typed_skip_basis_refs_by_stage"]["idea"] = [
        basis_ref,
        basis_ref,
    ]

    with pytest.raises(
        ReasoningContractError,
        match="next_cycle_proposal_skip_basis_invalid",
    ):
        validate_reasoning_transition(
            outcome,
            next_cycle=proposal,
            candidate_completion=None,
        )
