"""Resolve a launch's accepted input set once, without dropping proof checks."""

from __future__ import annotations

import pytest

from meta_research.bundle_protocol import AcceptedInputAssetProof, ReceiptProof
from meta_research.owners.common import OwnerConflict
import test_public_bundle_stage as bundle_fixtures
from test_target_root_finalizer import _current_bundle_runtime


@pytest.fixture
def launch_inputs(tmp_path, monkeypatch):
    # Match a downstream launch with many exact versions, including aliases
    # that resolve to one physical asset. Keep real graph/spec receipts.
    input_refs = tuple(f"asset_version_{index:02d}" for index in range(32))
    resolved_refs = tuple(f"asset_{index // 2:02d}" for index in range(32))
    runtime = _current_bundle_runtime(tmp_path / "batch-input-launch")
    original_candidate = bundle_fixtures._formal_candidate

    def candidate(**kwargs):
        value = original_candidate(**kwargs)
        value["candidate"]["direct_accepted_input_asset_refs"] = list(
            reversed(input_refs)
        )
        return value

    monkeypatch.setattr(bundle_fixtures, "_formal_candidate", candidate)
    try:
        bundle_fixtures._confirm_direct_quest(runtime)
        bundle_fixtures._finish_idea_stage(runtime)
        bundle_fixtures._finish_plan_stage(runtime)
        owner = runtime.owners.research_graph
        accepted_graph = None
        for _ in range(16):
            runtime.bundle_stage.process_once()
            request = runtime.bundle_stage.query_current()["stage_run_request"]
            if request is not None:
                accepted_graph = owner.query_target_graph(request["request_ref"])
                if accepted_graph is not None:
                    break
        assert accepted_graph is not None
        target = accepted_graph.targets[0]
        owner.accept_formal_plan_content(
            formal_plan_ref=accepted_graph.formal_plan_ref,
            idempotency_key="batch-plan-content",
        )
        owner.accept_target_formal_plan_projection(
            graph_ref=accepted_graph.graph_ref,
            idempotency_key="batch-plan-projection",
        )
        owner.accept_target_candidate_projection(
            target_ref=target.target_ref,
            idempotency_key="batch-candidate-projection",
        )
        yield owner, target, input_refs, resolved_refs
    finally:
        runtime.close()


class _SingleInputReader:
    def __init__(self, input_refs, resolved_refs):
        self.resolutions = dict(zip(input_refs, resolved_refs, strict=True))
        self.single_calls = []
        self.proof_calls = []
        self.missing_proof = None

    def resolve_input_asset_ref(self, *, target_ref, input_ref):
        self.single_calls.append((target_ref, input_ref))
        return self.resolutions[input_ref]

    def query_bundle_input_asset_proof(self, *, target_ref, asset_ref):
        self.proof_calls.append((target_ref, asset_ref))
        if asset_ref == self.missing_proof:
            return None
        return AcceptedInputAssetProof(
            asset_ref=asset_ref,
            rm_acceptance_receipt=ReceiptProof(
                receipt_ref=f"rm_{asset_ref}",
                subject_ref=asset_ref,
                verified=True,
                currentness_known=True,
                current=True,
            ),
            rg_role_receipt=ReceiptProof(
                receipt_ref=f"rg_{asset_ref}",
                subject_ref=asset_ref,
                verified=True,
                currentness_known=True,
                current=True,
            ),
        )


class _BatchInputReader(_SingleInputReader):
    def __init__(self, input_refs, resolved_refs):
        super().__init__(input_refs, resolved_refs)
        self.batch_calls = []
        self.result = resolved_refs

    def resolve_input_asset_refs(self, *, target_ref, input_refs):
        self.batch_calls.append((target_ref, input_refs))
        return self.result


def test_launch_resolves_all_versions_once_and_checks_each_asset_proof(
    launch_inputs, monkeypatch
):
    owner, target, input_refs, resolved_refs = launch_inputs
    reader = _BatchInputReader(input_refs, resolved_refs)
    monkeypatch.setattr(owner._receipt_verifier, "_target_input_asset_proof_reader", reader)

    launch = owner.query_target_launch_request(target.target_ref)

    expected_assets = tuple(sorted(set(resolved_refs)))
    assert reader.batch_calls == [(target.target_ref, input_refs)]
    assert reader.single_calls == []
    assert reader.proof_calls == [(target.target_ref, ref) for ref in expected_assets]
    assert launch.accepted_input_asset_refs == expected_assets
    assert target.spec["candidate"]["direct_accepted_input_asset_refs"] == list(
        reversed(input_refs)
    )


def test_launch_retains_single_input_reader_compatibility(launch_inputs, monkeypatch):
    owner, target, input_refs, resolved_refs = launch_inputs
    reader = _SingleInputReader(input_refs, resolved_refs)
    monkeypatch.setattr(owner._receipt_verifier, "_target_input_asset_proof_reader", reader)

    launch = owner.query_target_launch_request(target.target_ref)

    assert reader.single_calls == [(target.target_ref, ref) for ref in input_refs]
    assert launch.accepted_input_asset_refs == tuple(sorted(set(resolved_refs)))


@pytest.mark.parametrize("malformed_result", [(), ("",) * 32, (None,) * 32])
def test_launch_rejects_incomplete_or_invalid_batch_results(
    launch_inputs, monkeypatch, malformed_result
):
    owner, target, input_refs, resolved_refs = launch_inputs
    reader = _BatchInputReader(input_refs, resolved_refs)
    reader.result = malformed_result
    monkeypatch.setattr(owner._receipt_verifier, "_target_input_asset_proof_reader", reader)

    with pytest.raises(OwnerConflict, match="target_launch_asset_refs_invalid"):
        owner.query_target_launch_request(target.target_ref)

    assert reader.single_calls == []
    assert reader.proof_calls == []


def test_launch_still_requires_every_resolved_asset_proof(launch_inputs, monkeypatch):
    owner, target, input_refs, resolved_refs = launch_inputs
    reader = _BatchInputReader(input_refs, resolved_refs)
    reader.missing_proof = resolved_refs[-1]
    monkeypatch.setattr(owner._receipt_verifier, "_target_input_asset_proof_reader", reader)

    with pytest.raises(OwnerConflict, match="target_launch_asset_proof_invalid"):
        owner.query_target_launch_request(target.target_ref)

    assert reader.batch_calls == [(target.target_ref, input_refs)]
    assert reader.single_calls == []
