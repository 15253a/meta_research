"""Launch canonicalizes a set of inputs without rewriting the accepted candidate."""
from copy import deepcopy
from dataclasses import replace

import pytest

from meta_research.bundle_target_contract import BundleTargetContractError, formal_target_candidate_from_dict
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
import test_public_bundle_stage as bundle_fixtures
from test_target_root_finalizer import _current_bundle_runtime


def test_reversed_asset_refs_keep_the_candidate_hash_and_launch_with_exact_sorted_proofs(tmp_path, monkeypatch):
    runtime = _current_bundle_runtime(tmp_path / "asset-order")
    try:
        quest = bundle_fixtures._confirm_direct_quest(runtime)
        graph = runtime.owners.research_graph
        question = graph.query_question(quest["initialization_id"])
        assets = []
        for index in range(2):
            intake = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind="text", custody_mode="managed", display_name=f"evidence-{index}.txt",
                media_type="text/plain", content=f"Independent research evidence {index}".encode()),
                idempotency_key=f"asset-{index}")
            binding = intake.asset.as_binding()
            role = graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref,
                idempotency_key=f"asset-role-{index}")
            assets.append((binding, role))
        reverse_refs = sorted((binding.asset_ref for binding, _role in assets), reverse=True)
        original = bundle_fixtures._formal_candidate
        def candidate(**kwargs):
            value = original(**kwargs)
            value["candidate"]["direct_accepted_input_asset_refs"] = list(reverse_refs)
            return value
        monkeypatch.setattr(bundle_fixtures, "_formal_candidate", candidate)
        bundle_fixtures._finish_idea_stage(runtime)
        bundle_fixtures._finish_plan_stage(runtime)
        accepted_graph = None
        for _ in range(16):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            request = current["stage_run_request"]
            if request is not None:
                accepted_graph = graph.query_target_graph(request["request_ref"])
                if accepted_graph is not None:
                    break
        assert accepted_graph is not None
        target = accepted_graph.targets[0]
        assert target.spec["candidate"]["direct_accepted_input_asset_refs"] == reverse_refs
        original_spec_hash = canonical_hash(target.spec)
        assert original_spec_hash == target.spec_hash
        graph.accept_formal_plan_content(formal_plan_ref=accepted_graph.formal_plan_ref, idempotency_key="plan-content")
        plan_projection = graph.accept_target_formal_plan_projection(graph_ref=accepted_graph.graph_ref, idempotency_key="plan-projection")
        projection = graph.accept_target_candidate_projection(target_ref=target.target_ref, idempotency_key="candidate-projection")
        assert projection.source_spec_hash == original_spec_hash
        assert projection.candidate.direct_accepted_input_asset_refs == tuple(reverse_refs)

        memory = runtime.target_run_authorities.research_memory
        target_graph = runtime.target_run_authorities.research_graph
        binding, role = assets[0]
        with pytest.raises(OwnerConflict):
            memory.accept_input_asset(target_ref=target.target_ref,
                asset=replace(binding, content_hash="0" * 64), idempotency_key="forged-asset")
        for index, (binding, role) in enumerate(assets):
            receipt = memory.accept_input_asset(target_ref=target.target_ref, asset=binding, idempotency_key=f"input-{index}")
            with pytest.raises(OwnerConflict, match="rm_proof_invalid"):
                target_graph.accept_input_asset_role(target_ref=target.target_ref, role=role,
                    rm_proof_receipt=replace(receipt, payload_hash="0" * 64), idempotency_key=f"forged-proof-{index}")
            target_graph.accept_input_asset_role(target_ref=target.target_ref, role=role,
                rm_proof_receipt=receipt, idempotency_key=f"input-role-{index}")
        launch = graph.query_target_launch_request(target.target_ref)
        assert launch.accepted_input_asset_refs == tuple(sorted(reverse_refs))
        for ref in launch.accepted_input_asset_refs:
            proof = target_graph.query_bundle_input_asset_proof(target_ref=target.target_ref, asset_ref=ref)
            assert proof.asset_ref == ref
            assert proof.rm_acceptance_receipt.verified and proof.rg_role_receipt.verified
        source = graph.query_target_candidate_projection_source(target_ref=target.target_ref)
        assert source["spec"] == target.spec and source["source_spec_hash"] == original_spec_hash
        assert graph.query_target_launch_request(target.target_ref) == launch

        # Set order is flexible; duplicate, empty and non-reference entries remain invalid.
        for invalid_refs in ([reverse_refs[0], reverse_refs[0]], [""], [None], "not-an-array"):
            invalid = deepcopy(target.spec)
            invalid["candidate"]["direct_accepted_input_asset_refs"] = invalid_refs
            with pytest.raises(BundleTargetContractError):
                formal_target_candidate_from_dict(invalid, completion_contract=plan_projection.completion_contract)
    finally:
        runtime.close()


def test_declared_evidence_requires_selection_by_the_accepted_plan(tmp_path, monkeypatch):
    runtime = _current_bundle_runtime(tmp_path / "unselected-evidence")
    try:
        bundle_fixtures._confirm_direct_quest(runtime)
        original = bundle_fixtures._formal_candidate
        def candidate(**kwargs):
            value = original(**kwargs)
            value["candidate"]["direct_accepted_input_asset_refs"] = ["evidence_unselected"]
            return value
        monkeypatch.setattr(bundle_fixtures, "_formal_candidate", candidate)
        bundle_fixtures._finish_idea_stage(runtime)
        bundle_fixtures._finish_plan_stage(runtime)
        graph = runtime.owners.research_graph
        accepted_graph = None
        for _ in range(16):
            runtime.bundle_stage.process_once()
            request = runtime.bundle_stage.query_current()["stage_run_request"]
            if request is not None:
                accepted_graph = graph.query_target_graph(request["request_ref"])
                if accepted_graph is not None:
                    break
        assert accepted_graph is not None
        target = accepted_graph.targets[0]
        graph.accept_formal_plan_content(formal_plan_ref=accepted_graph.formal_plan_ref,
            idempotency_key="plan-content")
        graph.accept_target_formal_plan_projection(graph_ref=accepted_graph.graph_ref,
            idempotency_key="plan-projection")
        graph.accept_target_candidate_projection(target_ref=target.target_ref,
            idempotency_key="candidate-projection")
        authority = runtime.target_run_authorities.research_graph
        with pytest.raises(OwnerConflict, match="target_input_evidence_not_selected"):
            authority.prepare_input_assets(target.target_ref)
        with pytest.raises(OwnerConflict, match="target_input_evidence_not_selected"):
            authority.resolve_input_asset_ref(target_ref=target.target_ref, input_ref="evidence_unselected")
    finally:
        runtime.close()
