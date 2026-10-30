"""The advancement closure checks the exact Owner-proven notes bytes."""
from dataclasses import replace
import hashlib

import pytest

from meta_research.owners.common import OwnerConflict
import test_bundle_report_receipt_order as fixtures


@pytest.mark.parametrize("invalid_hash", [None, "0" * 64], ids=["missing", "mismatch"])
def test_advancement_rejects_a_notes_hash_not_matching_accepted_utf8(tmp_path, monkeypatch, invalid_hash):
    notes = "保留真实负结果与未决：\nα calibration needs confirmation."

    class NotesTargets(fixtures._TwoCurrentTargets):
        def propose_target_batch(self, request):
            result = super().propose_target_batch(request)
            return replace(result, strategy_update={**result.strategy_update, "notes": notes})

    monkeypatch.setattr(fixtures, "_TwoCurrentTargets", NotesTargets)
    generator = fixtures.committed_targets.__wrapped__(tmp_path)
    runtime, graph, run, _, source, projection = next(generator)
    try:
        for _ in range(8):
            if graph.strategy_complete:
                break
            runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph.strategy_complete
        owner = runtime.owners.agent_runtime
        values = dict(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            formal_plan_content_receipt=source.receipt, formal_plan_projection_receipt=projection.receipt,
            target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt)
        candidate = owner.build_bundle_report_candidate(disposition="realized", **values)
        accepted = owner.accept_bundle_report(report=candidate, idempotency_key="notes-hash-report", **values)
        completion = owner.complete_bundle_run(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref, report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
            idempotency_key="notes-hash-completion")
        engine = runtime.owners.advancement_engine
        authority = engine._bundle_report_evidence_verifier
        original = authority.query_bundle_report_contract

        def corrupted_contract(**arguments):
            contract = original(**arguments)
            assert contract["notes"] == notes
            assert contract["notes_text_hash"] == hashlib.sha256(notes.encode("utf-8")).hexdigest()
            contract["notes_text_hash"] = invalid_hash
            return contract

        arguments = dict(request_ref=run.request_ref, run_ref=run.run_ref,
            bundle_report_ref=accepted.report_ref, run_completion_receipt=completion.receipt,
            bundle_report_receipt=accepted.receipt, idempotency_key="notes-hash-advance")
        monkeypatch.setattr(authority, "query_bundle_report_contract", corrupted_contract)
        with pytest.raises(OwnerConflict, match="bundle_stage_notes_invalid"):
            engine.commit_bundle_stage(**arguments)
        assert engine.query_bundle_stage_commit(run.request_ref) is None
        monkeypatch.setattr(authority, "query_bundle_report_contract", original)
        commit = engine.commit_bundle_stage(**arguments)
        assert commit.closure["notes"] == notes
        assert commit.closure["notes_text_hash"] == hashlib.sha256(notes.encode("utf-8")).hexdigest()
    finally:
        generator.close()
