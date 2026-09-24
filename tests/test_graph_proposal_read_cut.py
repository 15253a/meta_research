"""Real AR proposal and RG graph reads reuse only one immutable DB cut."""
from dataclasses import replace
from time import perf_counter
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from conftest import _isolate_platform_power_dependency
import meta_research.owners.agent_runtime as ar_module
import meta_research.owners.research_graph as rg_module
from meta_research.owners.common import OwnerConflict
from test_successor_append import successor_proposal, append


def _proposal_values(graph, proposal):
    return dict(proposal_ref=proposal.proposal_ref, run_ref=proposal.run_ref,
        graph_ref=graph.graph_ref, base_generation=graph.head_generation,
        base_head_receipt=graph.head_receipt, proposal_hash=proposal.proposal_hash,
        receipt=proposal.receipt)


def test_proposal_receipt_is_rebuilt_once_per_cut_with_exact_inputs(successor_proposal, monkeypatch):
    runtime, _request_ref, graph, proposal = successor_proposal
    owner = runtime.owners.agent_runtime
    database = runtime._database
    values = _proposal_values(graph, proposal)
    original = ar_module._bundle_target_proposal
    rebuild = Mock(wraps=original)
    monkeypatch.setattr(ar_module, "_bundle_target_proposal", rebuild)
    started = perf_counter()
    with database.read_snapshot():
        expected = owner.verify_bundle_target_proposal_receipt(**values)
        returned = owner.verify_bundle_target_proposal_receipt(**values)
        returned["attempt_ref"] = "caller-mutation"
        assert owner.verify_bundle_target_proposal_receipt(**values) == expected
        print(f"PROPOSAL_READ seconds={perf_counter()-started:.6f} rebuilds={rebuild.call_count}", flush=True)
        assert rebuild.call_count == 1
        for key, bad in (("receipt", replace(proposal.receipt, payload_hash="0"*64)),
                         ("proposal_hash", "0"*64), ("attempt_ref", "wrong-attempt"),
                         ("require_checkpoint_current", True)):
            if key == "require_checkpoint_current":
                # Currentness is an independent cache key and still executes.
                assert owner.verify_bundle_target_proposal_receipt(**{**values, key: bad}) == expected
            else:
                with pytest.raises(OwnerConflict):
                    owner.verify_bundle_target_proposal_receipt(**{**values, key: bad})
    before = rebuild.call_count
    assert owner.verify_bundle_target_proposal_receipt(**values) == expected
    assert owner.verify_bundle_target_proposal_receipt(**values) == expected
    assert rebuild.call_count == before + 2
    with database.write() as connection:
        connection.execute(text("UPDATE ar_bundle_target_proposals SET receipt_hash=:hash WHERE proposal_ref=:ref"),
            {"hash": "0"*64, "ref": proposal.proposal_ref})
    with database.read_snapshot(), pytest.raises(OwnerConflict, match="bundle_target_proposal_invalid"):
        owner.verify_bundle_target_proposal_receipt(**values)
    with database.write() as connection:
        connection.execute(text("UPDATE ar_bundle_target_proposals SET receipt_hash=:hash WHERE proposal_ref=:ref"),
            {"hash": proposal.receipt.payload_hash, "ref": proposal.proposal_ref})
    # Use a callable to prove a failed pure read did not populate the cache.
    attempts = []
    def fail_once(row):
        attempts.append(1)
        if len(attempts) == 1:
            raise OwnerConflict("fixture_transient")
        return original(row)
    monkeypatch.setattr(ar_module, "_bundle_target_proposal", fail_once)
    with database.read_snapshot():
        with pytest.raises(OwnerConflict, match="fixture_transient"):
            owner.verify_bundle_target_proposal_receipt(**values)
        assert owner.verify_bundle_target_proposal_receipt(**values) == expected
        assert owner.verify_bundle_target_proposal_receipt(**values) == expected
        assert len(attempts) == 2


def test_graph_query_and_receipt_share_pure_rebuild_but_reenter_issuer(successor_proposal, monkeypatch):
    runtime, request_ref, graph, proposal = successor_proposal
    head = append(runtime, graph, proposal)
    owner = runtime.owners.research_graph
    database = runtime._database
    values = dict(request_ref=request_ref, run_ref=graph.run_ref, graph_ref=graph.graph_ref,
                  receipt=head.receipt, require_current=True)
    original = rg_module._accepted_target_graph
    rebuild = Mock(wraps=original)
    monkeypatch.setattr(rg_module, "_accepted_target_graph", rebuild)
    issuer = owner._receipt_verifier._execution_verifier
    issuer_read = Mock(wraps=issuer.verify_attempt_execution_receipt)
    monkeypatch.setattr(issuer, "verify_attempt_execution_receipt", issuer_read)
    started = perf_counter()
    with database.read_snapshot():
        accepted = owner.query_target_graph(request_ref)
        assert accepted.head_receipt == head.receipt
        result = owner._receipt_verifier.verify_target_graph_receipt(**values)
        result["receipt"]["payload_hash"] = "caller-mutation"
        again = owner._receipt_verifier.verify_target_graph_receipt(**values)
        assert again["receipt"]["payload_hash"] == head.receipt.payload_hash
        returned = owner.query_target_graph(request_ref)
        returned.target_plan["caller-mutation"] = True
        assert "caller-mutation" not in owner.query_target_graph(request_ref).target_plan
        print(f"GRAPH_READ seconds={perf_counter()-started:.6f} rebuilds={rebuild.call_count} issuer_reads={issuer_read.call_count}", flush=True)
        assert rebuild.call_count == 1
        assert issuer_read.call_count >= 2
        with pytest.raises(OwnerConflict):
            owner._receipt_verifier.verify_target_graph_receipt(**{**values, "receipt":replace(head.receipt, payload_hash="0"*64)})
        issuer_read.side_effect = OwnerConflict("fixture_issuer_unavailable")
        with pytest.raises(OwnerConflict, match="fixture_issuer_unavailable"):
            owner._receipt_verifier.verify_target_graph_receipt(**values)
        issuer_read.side_effect = None
        assert owner._receipt_verifier.verify_target_graph_receipt(**values)["generation"] == 1
    before = rebuild.call_count
    assert owner.query_target_graph(request_ref) == accepted
    assert rebuild.call_count == before + 1
    with database.write() as connection:
        connection.execute(text("UPDATE rg_target_graphs SET receipt_hash=:hash WHERE graph_ref=:ref"),
            {"hash":"0"*64, "ref":graph.graph_ref})
    with database.read_snapshot(), pytest.raises(OwnerConflict):
        owner.query_target_graph(request_ref)
    with database.write() as connection:
        connection.execute(text("UPDATE rg_target_graphs SET receipt_hash=:hash WHERE graph_ref=:ref"),
            {"hash":graph.receipt.payload_hash, "ref":graph.graph_ref})
    attempts = []
    def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OwnerConflict("fixture_transient")
        return original(*args, **kwargs)
    monkeypatch.setattr(rg_module, "_accepted_target_graph", fail_once)
    with database.read_snapshot():
        with pytest.raises(OwnerConflict, match="fixture_transient"):
            owner.query_target_graph(request_ref)
        assert owner.query_target_graph(request_ref) == accepted
        assert owner.query_target_graph(request_ref) == accepted
        assert len(attempts) == 2
