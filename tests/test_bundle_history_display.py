"""Historical Bundle text stays exact without replaying execution admission."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

import meta_research.owners.agent_runtime as agent_runtime_module
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.research_overview import ResearchOverviewReader
from meta_research.writing_snapshot import WritingResearchSnapshotReader
from test_bundle_report_read_snapshot import _accept_report
from test_bundle_report_receipt_order import committed_targets


@contextmanager
def _changed(database, table, key, ref, field, value):
    # All identifiers come from this test; each independent corruption is
    # restored before the next read, even if an assertion fails.
    with database.read() as connection:
        original = connection.execute(
            text(f"SELECT {field} FROM {table} WHERE {key}=:ref"),
            {"ref": ref},
        ).scalar_one()
    try:
        with database.write() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {field}=:value WHERE {key}=:ref"),
                {"value": value, "ref": ref},
            )
        yield
    finally:
        with database.write() as connection:
            connection.execute(
                text(f"UPDATE {table} SET {field}=:value WHERE {key}=:ref"),
                {"value": original, "ref": ref},
            )


def test_bundle_display_keeps_exact_accepted_text_and_bindings_without_replaying_targets(
    committed_targets, monkeypatch
):
    runtime, graph, run, accepted = _accept_report(committed_targets)
    owners, database = runtime.owners, runtime._database
    owner = owners.agent_runtime
    request = owners.advancement_engine.query_bundle_stage_request(run.cycle_ref)
    assert request is not None and request.request_ref == run.request_ref
    readers = (
        owners.research_graph, owners.advancement_engine,
        owners.research_memory, owner,
    )
    writing = WritingResearchSnapshotReader(*readers)
    overview = ResearchOverviewReader(*readers)
    cycle = SimpleNamespace(
        cycle_ref=request.cycle_ref,
        question_ref=request.accepted_question.question_ref,
    )
    quest_ref = request.accepted_question.quest_ref
    displayed_fields = ("run_ref", "report_ref", "report_hash", "report")

    with database.read_snapshot():
        old = writing._bundle_stage_value(request, None)["report"]
    expected = {key: old[key] for key in displayed_fields}
    prepare = Mock(side_effect=AssertionError("display_replayed_target_proofs"))
    with monkeypatch.context() as patch:
        patch.setattr(agent_runtime_module, "_prepare_bundle_report_material", prepare)
        write = Mock(side_effect=AssertionError("display_performed_owner_write"))
        patch.setattr(database, "write", write)
        with database.read_snapshot():
            displayed = owner.query_bundle_report_display(request)
            assert {key: displayed[key] for key in displayed_fields} == expected
            assert displayed["receipt"] == accepted.receipt
            assert displayed["completion"] is None
            artifact = overview._artifact(quest_ref, cycle, request, None)
            assert artifact["status"] == "report_accepted"
            assert artifact["content"] == expected["report"]
            assert artifact["source"]["content_hash"] == expected["report_hash"]
        prepare.assert_not_called()
        write.assert_not_called()
        # Display must not replace or weaken the existing authoritative seam.
        with pytest.raises(AssertionError, match="display_replayed_target_proofs"):
            owner.query_bundle_run_report(run.run_ref)
        assert prepare.call_count > 0
    assert database._read_cut.get() is None and database._read_cache.get() is None

    # Commit through the real AR and AE boundaries before checking completed
    # history. Nothing in the display seam manufactures an acceptance proof.
    completion = owner.complete_bundle_run(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key="complete-bundle-display-fixture",
    )
    owners.advancement_engine.commit_bundle_stage(
        request_ref=run.request_ref, run_ref=run.run_ref,
        bundle_report_ref=accepted.report_ref,
        run_completion_receipt=completion.receipt,
        bundle_report_receipt=accepted.receipt,
        idempotency_key="commit-bundle-display-fixture",
    )
    commit = owners.advancement_engine.query_bundle_stage_commit(run.request_ref)
    assert commit is not None
    with database.read_snapshot():
        old_completed = writing._bundle_stage_value(request, commit)["report"]
        assert {key: old_completed[key] for key in displayed_fields} == expected

    with monkeypatch.context() as patch:
        prepare = Mock(side_effect=AssertionError("display_replayed_target_proofs"))
        patch.setattr(agent_runtime_module, "_prepare_bundle_report_material", prepare)
        with database.read_snapshot():
            displayed = owner.query_bundle_report_display(request)
            assert {key: displayed[key] for key in displayed_fields} == expected
            assert displayed["completion"] == completion
            artifact = overview._artifact(quest_ref, cycle, request, commit)
            assert artifact["status"] == "accepted"
            assert artifact["content"] == expected["report"]
        prepare.assert_not_called()

    bad_report = dict(expected["report"])
    bad_report["evidence_refs"] = ["altered-evidence"]
    corruptions = (
        ("ar_bundle_reports", "report_ref", accepted.report_ref, "report_json", canonical_json(bad_report)),
        ("ar_bundle_reports", "report_ref", accepted.report_ref, "report_hash", "0" * 64),
        ("ar_bundle_reports", "report_ref", accepted.report_ref, "receipt_hash", "0" * 64),
        ("ar_stage_runs", "run_ref", run.run_ref, "epoch", request.epoch + 1),
        ("ar_stage_runs", "run_ref", run.run_ref, "completion_receipt_hash", "0" * 64),
    )
    for table, key, ref, field, value in corruptions:
        with _changed(database, table, key, ref, field, value):
            with database.read_snapshot(), pytest.raises(OwnerConflict):
                owner.query_bundle_report_display(request)
            with database.read_snapshot():
                artifact = overview._artifact(quest_ref, cycle, request, commit)
            assert artifact["status"] == "unavailable", field
            assert artifact["content"] is None, field

    assert request.accepted_formal_plan is not None
    for bad_request in (
        replace(request, epoch=request.epoch + 1),
        replace(request, accepted_formal_plan=replace(
            request.accepted_formal_plan, plan_document_hash="0" * 64
        )),
    ):
        with database.read_snapshot(), pytest.raises(OwnerConflict):
            owner.query_bundle_report_display(bad_request)
    for bad_commit in (
        replace(commit, outcome_ref="another-bundle-report"),
        replace(commit, run_ref="another-bundle-run"),
        replace(commit, run_completion_receipt=replace(
            commit.run_completion_receipt, payload_hash="0" * 64
        )),
    ):
        with database.read_snapshot():
            artifact = overview._artifact(quest_ref, cycle, request, bad_commit)
        assert artifact["status"] == "unavailable"
        assert artifact["content"] is None

    # An upstream Target proof still fails in the strict query. The immutable
    # accepted report remains readable as historical text, not execution proof.
    with _changed(database, "rg_target_candidate_projections", "target_ref",
                  graph.targets[0].target_ref, "source_spec_hash", "0" * 64):
        with database.read_snapshot():
            displayed = owner.query_bundle_report_display(request)
            assert {key: displayed[key] for key in displayed_fields} == expected
        with pytest.raises(OwnerConflict):
            owner.query_bundle_run_report(run.run_ref)
    assert owner.query_bundle_run_report(run.run_ref) == accepted
    assert database._read_cut.get() is None and database._read_cache.get() is None
