"""History readers reuse exact SQL proofs while keeping live file custody."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners import agent_runtime
from meta_research.owners.agent_runtime import DEEPFETCH_EXECUTION_RECEIPT_KIND
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict
from test_bundle_report_receipt_order import committed_targets
from test_bundle_report_read_snapshot import _accept_report
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill, _confirm_deepfetch_quest, _reasoning_runtime,
)


def test_literature_queries_reuse_execution_proof_but_recheck_custody(tmp_path, monkeypatch):
    runtime = _reasoning_runtime(tmp_path / 'deepfetch',
                                 reasoning_skill=_DeterministicReasoningSkill())
    try:
        _confirm_deepfetch_quest(runtime)
        database, memory = runtime._database, runtime.owners.research_memory
        with database.read() as connection:
            row = connection.execute(text('SELECT * FROM rm_literature_snapshots')).one()
        parse = Mock(wraps=agent_runtime._deepfetch_runtime_binding)
        monkeypatch.setattr(agent_runtime, '_deepfetch_runtime_binding', parse)
        verifier = memory._execution_verifier
        values = dict(request_ref=row.request_ref, run_ref=row.run_ref,
                      attempt_ref=row.attempt_ref, fence_ref=row.fence_ref,
                      result_hash=row.result_hash,
                      receipt=AcceptanceReceipt('agent_runtime', DEEPFETCH_EXECUTION_RECEIPT_KIND,
                          row.execution_receipt_ref, row.run_ref, row.execution_receipt_hash))
        write = Mock(wraps=database.write)
        monkeypatch.setattr(database, 'write', write)
        original_file = memory._object_store / row.papers_object_path
        saved = original_file.read_bytes()
        with database.read_snapshot():
            accepted = memory.query_literature_snapshot(row.snapshot_ref)
            with database.read_snapshot():
                assert memory.query_literature_snapshot(row.snapshot_ref) == accepted
            assert parse.call_count == 1
            # A successful SQL proof never bypasses the public reader's files.
            try:
                original_file.write_bytes(b'{}')
                with pytest.raises(OwnerConflict, match='literature_snapshot_custody_unavailable'):
                    memory.query_literature_snapshot(row.snapshot_ref)
            finally:
                original_file.write_bytes(saved)
            assert memory.query_literature_snapshot(row.snapshot_ref) == accepted
            for name in ('request_ref', 'run_ref', 'attempt_ref', 'fence_ref', 'result_hash'):
                with pytest.raises(OwnerConflict):
                    verifier.verify_deepfetch_execution_receipt(**(values | {name: 'wrong'}))
            with pytest.raises(OwnerConflict):
                verifier.verify_deepfetch_execution_receipt(**(values | {
                    'receipt': replace(values['receipt'], payload_hash='0' * 64)}))
        write.assert_not_called()
        assert database._read_cut.get() is None and database._read_cache.get() is None
        parse.reset_mock()
        # No read cut means no proof reuse between calls.
        assert memory.query_literature_snapshot(row.snapshot_ref) == accepted
        assert memory.query_literature_snapshot(row.snapshot_ref) == accepted
        assert parse.call_count == 2
        with database.write() as connection:
            connection.execute(text('UPDATE ar_deepfetch_runs SET execution_receipt_hash=:bad WHERE run_ref=:run'),
                               {'bad': '0' * 64, 'run': row.run_ref})
        parse.reset_mock()
        with database.read_snapshot():
            for _ in range(2):
                with pytest.raises(OwnerConflict, match='deepfetch_execution_receipt_invalid'):
                    memory.query_literature_snapshot(row.snapshot_ref)
            assert parse.call_count == 2  # Failures never enter the cut cache.
        with database.write() as connection:
            connection.execute(text('UPDATE ar_deepfetch_runs SET execution_receipt_hash=:good WHERE run_ref=:run'),
                               {'good': row.execution_receipt_hash, 'run': row.run_ref})
        with database.read_snapshot():
            assert memory.query_literature_snapshot(row.snapshot_ref) == accepted
    finally:
        runtime.close()


def test_bundle_completion_and_history_share_the_same_report_proof(committed_targets, monkeypatch):
    runtime, _graph, run, accepted = _accept_report(committed_targets)
    completion = runtime.owners.agent_runtime.complete_bundle_run(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key='overview-completion')
    verifier = runtime.owners.advancement_engine._run_completion_verifier
    build = Mock(wraps=agent_runtime._verified_bundle_report_from_row)
    monkeypatch.setattr(agent_runtime, '_verified_bundle_report_from_row', build)
    with runtime._database.read_snapshot():
        verifier.verify_run_completion_receipt(request_ref=run.request_ref,
            run_ref=run.run_ref, attempt_ref=None, outcome_ref=accepted.report_ref,
            receipt=completion.receipt)
        assert verifier.verify_bundle_report_receipt(report_ref=accepted.report_ref,
            receipt=accepted.receipt, expected_disposition=None) == accepted
        assert build.call_count == 1
        with pytest.raises(OwnerConflict, match='bundle_report_disposition_invalid'):
            verifier.verify_bundle_report_receipt(report_ref=accepted.report_ref,
                receipt=accepted.receipt, expected_disposition='replan_required')
        assert verifier.verify_bundle_report_receipt(report_ref=accepted.report_ref,
            receipt=accepted.receipt, expected_disposition=None) == accepted
        # The public AR facade shares this verifier, so its Writing/overview
        # query -> receipt -> completion path reuses the same exact proof.
        # The intentionally invalid disposition above added one failed build.
        assert build.call_count == 2
        owner = runtime.owners.agent_runtime
        assert owner.query_bundle_run_report(run.run_ref) == accepted
        assert owner.verify_bundle_report_receipt(report_ref=accepted.report_ref,
            receipt=accepted.receipt) == accepted
        owner.verify_run_completion_receipt(request_ref=run.request_ref,
            run_ref=run.run_ref, attempt_ref=None, outcome_ref=accepted.report_ref,
            receipt=completion.receipt)
        assert build.call_count == 2
    assert runtime._database._read_cut.get() is None
    assert runtime._database._read_cache.get() is None
