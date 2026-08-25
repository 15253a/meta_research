from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from meta_research.bundle_contract import (
    BundleContractError,
    target_graph_append_proposal,
    validate_target_graph_append_proposal,
)
from meta_research.bundle_target_contract import (
    apply_strategy_update,
    build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
    start_rolling_strategy,
    strategy_update_from_dict,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_graph import (
    RG_OWNER,
    TARGET_GRAPH_RECEIPT_KIND,
    TARGET_RECEIPT_KIND,
    _accepted_target,
    _accepted_target_graph,
    _receipt_hash,
    _rolling_strategy_hash,
    _target_graph_append_bindings,
    _target_graph_bindings,
    _target_set_hash,
    _verify_target_candidate_owner_proofs,
)
from test_bundle_target_contract import (
    _candidate,
    _normalizations,
    _plan,
    _update,
)


def _completion(*, cells: list[str]):
    plan = _plan()
    normalizations = list(deepcopy(_normalizations()))
    normalizations[0]["required_measurement_unit_keys"] = cells
    contract = build_normalized_completion_contract(
        plan,
        tuple(normalizations),
    )
    return plan, contract


def _target_plan(
    plan: dict[str, object],
    contract,
    initial_update: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-plan/v3",
        "kind": "TargetPlan",
        "formal_plan_ref": "formal-plan-1",
        "context_pack_ref": "bundle-context-1",
        "completion_contract": normalized_completion_contract_to_dict(contract),
        "initial_strategy_update": initial_update,
        "source_bindings": {
            "formal_plan_ref": "formal-plan-1",
            "plan_document_hash": canonical_hash(plan),
            "context_pack_ref": "bundle-context-1",
            "context_pack_hash": "c" * 64,
        },
    }


def _graph_row(target_plan: dict[str, object]):
    row = SimpleNamespace(
        graph_ref="target-graph-1",
        request_ref="bundle-request-1",
        run_ref="bundle-run-1",
        attempt_ref="bundle-attempt-1",
        fence_ref="bundle-fence-1",
        submission_ref="bundle-submission-1",
        cycle_ref="cycle-1",
        quest_ref="quest-1",
        formal_plan_ref="formal-plan-1",
        plan_content_ref="plan-content-1",
        plan_document_hash=target_plan["source_bindings"]["plan_document_hash"],
        context_pack_ref="bundle-context-1",
        context_pack_hash="c" * 64,
        target_plan_json=canonical_json(target_plan),
        target_plan_hash=canonical_hash(target_plan),
        execution_receipt_ref="execution-receipt-1",
        execution_receipt_hash="e" * 64,
        receipt_ref="target-graph-receipt-1",
        receipt_hash="",
    )
    row.receipt_hash = _receipt_hash(
        TARGET_GRAPH_RECEIPT_KIND,
        row.graph_ref,
        _target_graph_bindings(row),
    )
    return row


def _target_row(
    *,
    spec: dict[str, object],
    target_ref: str,
    ordinal: int,
    dependency_refs: tuple[str, ...] = (),
    append_ref: str | None = None,
):
    label = spec["candidate"]["local_label"]
    spec_hash = canonical_hash(spec)
    dependency_hash = canonical_hash(list(dependency_refs))
    bindings: dict[str, object] = {
        "graph_ref": "target-graph-1",
        "target_key": label,
        "ordinal": ordinal,
        "spec_hash": spec_hash,
        "dependency_refs_hash": dependency_hash,
    }
    if append_ref is not None:
        bindings["append_ref"] = append_ref
    return SimpleNamespace(
        target_ref=target_ref,
        graph_ref="target-graph-1",
        target_key=label,
        ordinal=ordinal,
        spec_json=canonical_json(spec),
        spec_hash=spec_hash,
        dependency_refs_json=canonical_json(list(dependency_refs)),
        dependency_refs_hash=dependency_hash,
        receipt_ref=f"target-receipt-{ordinal}",
        receipt_hash=_receipt_hash(TARGET_RECEIPT_KIND, target_ref, bindings),
        append_ref=append_ref,
    )


class _RecordingProofVerifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify_reuse_source_receipt(self, **_values) -> None:
        self.calls.append("source")

    def verify_reuse_content_receipt(self, **_values) -> None:
        self.calls.append("content")

    def verify_reuse_eligibility_receipt(self, **_values) -> None:
        self.calls.append("eligibility")


def test_append_proposal_preserves_the_exact_formal_strategy_update() -> None:
    plan, contract = _completion(cells=["cell-a"])
    candidate = _candidate(contract, "target-a", "cell-a")
    update = _update(2, [candidate], complete=True)
    receipt = {
        "status": "accepted",
        "issuer": RG_OWNER,
        "kind": TARGET_GRAPH_RECEIPT_KIND,
        "receipt_ref": "head-receipt-1",
        "subject_ref": "target-graph-1",
        "payload_hash": "a" * 64,
    }

    proposal = target_graph_append_proposal(
        graph_ref="target-graph-1",
        base_generation=0,
        base_head_receipt=receipt,
        strategy_update=update,
    )

    assert proposal["strategy_update"] == update
    assert "targets" not in proposal
    assert "strategy_complete" not in proposal
    assert validate_target_graph_append_proposal(proposal) == canonical_hash(proposal)
    assert plan

    legacy = {**proposal, "schema_ref": "meta-research/target-graph-append-proposal/v1"}
    with pytest.raises(BundleContractError, match="append_proposal_invalid"):
        validate_target_graph_append_proposal(legacy)


def test_owner_proof_verification_is_required_and_checks_all_three_proofs() -> None:
    _plan_document, contract = _completion(cells=["cell-a"])
    value = _candidate(
        contract,
        "target-a",
        "cell-a",
        tier="accepted-local",
    )
    update = strategy_update_from_dict(
        _update(1, [value], complete=True),
        completion_contract=contract,
    )

    with pytest.raises(
        OwnerConflict,
        match="target_candidate_owner_proof_unverified",
    ):
        _verify_target_candidate_owner_proofs(update.candidates[0], None)

    verifier = _RecordingProofVerifier()
    _verify_target_candidate_owner_proofs(update.candidates[0], verifier)
    assert verifier.calls == ["source", "content", "eligibility"]


def test_generation_zero_can_be_formally_sealed_from_the_initial_update() -> None:
    plan, contract = _completion(cells=["cell-a"])
    candidate = _candidate(contract, "target-a", "cell-a")
    target_plan = _target_plan(
        plan,
        contract,
        _update(1, [candidate], complete=True),
    )
    graph_row = _graph_row(target_plan)
    target_row = _target_row(spec=candidate, target_ref="target-ref-a", ordinal=0)

    graph = _accepted_target_graph(graph_row, [target_row], [], plan)

    assert graph.strategy_complete is True
    assert graph.head_generation == 0
    assert graph.target_plan["schema_ref"] == "meta-research/target-plan/v3"
    assert graph.targets[0].target_key == "target-a"


def test_nonempty_append_can_seal_and_replays_the_full_update() -> None:
    plan, contract = _completion(cells=["cell-a", "cell-b"])
    first = _candidate(contract, "target-a", "cell-a")
    second = _candidate(
        contract,
        "target-b",
        "cell-b",
        depends_on=("target-a",),
    )
    initial = _update(1, [first], complete=False)
    target_plan = _target_plan(plan, contract, initial)
    graph_row = _graph_row(target_plan)
    first_row = _target_row(spec=first, target_ref="target-ref-a", ordinal=0)
    append_ref = "target-append-1"
    second_row = _target_row(
        spec=second,
        target_ref="target-ref-b",
        ordinal=1,
        dependency_refs=("target-ref-a",),
        append_ref=append_ref,
    )
    root_receipt = AcceptanceReceipt(
        issuer=RG_OWNER,
        kind=TARGET_GRAPH_RECEIPT_KIND,
        receipt_ref=graph_row.receipt_ref,
        subject_ref=graph_row.graph_ref,
        payload_hash=graph_row.receipt_hash,
    )
    update_value = _update(
        2,
        [second],
        complete=True,
        requires=("target-a",),
    )
    proposal = target_graph_append_proposal(
        graph_ref=graph_row.graph_ref,
        base_generation=0,
        base_head_receipt=root_receipt.as_public_dict(),
        strategy_update=update_value,
    )
    state = apply_strategy_update(
        start_rolling_strategy(contract),
        strategy_update_from_dict(initial, completion_contract=contract),
        completion_contract=contract,
    )
    state = apply_strategy_update(
        state,
        strategy_update_from_dict(update_value, completion_contract=contract),
        completion_contract=contract,
        accepted_labels=frozenset({"target-a"}),
    )
    accepted_targets = (_accepted_target(first_row), _accepted_target(second_row))
    append_row = SimpleNamespace(
        append_ref=append_ref,
        graph_ref=graph_row.graph_ref,
        generation=1,
        predecessor_head_receipt_ref=root_receipt.receipt_ref,
        predecessor_head_receipt_hash=root_receipt.payload_hash,
        proposal_ref="proposal-1",
        proposal_hash=canonical_hash(proposal),
        proposal_receipt_ref="proposal-receipt-1",
        proposal_receipt_hash="p" * 64,
        proposal_json=canonical_json(proposal),
        target_refs_json=canonical_json(["target-ref-b"]),
        target_set_hash=_target_set_hash(accepted_targets),
        coverage_hash=_rolling_strategy_hash(state, contract),
        strategy_complete=True,
        receipt_ref="target-graph-head-receipt-1",
        receipt_hash="",
    )
    append_row.receipt_hash = _receipt_hash(
        TARGET_GRAPH_RECEIPT_KIND,
        graph_row.graph_ref,
        _target_graph_append_bindings(append_row, ("target-ref-b",)),
    )

    graph = _accepted_target_graph(
        graph_row,
        [first_row, second_row],
        [append_row],
        plan,
    )

    assert graph.head_generation == 1
    assert graph.strategy_complete is True
    assert [target.target_key for target in graph.targets] == ["target-a", "target-b"]


def test_legacy_v2_graph_cannot_be_reconstructed_as_formal() -> None:
    plan, contract = _completion(cells=["cell-a"])
    candidate = _candidate(contract, "target-a", "cell-a")
    formal = _target_plan(plan, contract, _update(1, [candidate], complete=True))
    legacy = {
        "schema_ref": "meta-research/target-plan/v2",
        "kind": "TargetPlan",
        "formal_plan_ref": "formal-plan-1",
        "context_pack_ref": "bundle-context-1",
        "targets": [],
        "strategy_complete": False,
        "source_bindings": formal["source_bindings"],
    }
    graph_row = _graph_row(legacy)

    with pytest.raises(OwnerConflict, match="target_graph_integrity_invalid"):
        _accepted_target_graph(graph_row, [], [], plan)
