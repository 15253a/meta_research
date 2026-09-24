"""Bundle reports preserve accepted work without synthesizing scientific facts."""
from dataclasses import replace

import pytest

from meta_research.bundle_completion import build_report, result_sets
from meta_research.bundle_protocol import BundleProtocolError
from test_bundle_completion_contract import _closure, _plan


def _work_closure(label, *, attempted=False):
    closure = _closure(label, ('experiment',), label)
    return replace(closure, formal_measurement_accepted=False, metric_values=(),
        metric_result_ref=None, rg_formal_measurement_receipt=None,
        evaluation_attempt_ref=closure.evaluation_attempt_ref if attempted else None,
        evaluation_attempt_input_binding=closure.evaluation_attempt_input_binding if attempted else None,
        root_completion_receipt=closure.ar_execution_receipt,
        code_review=None, result_review=None)


def test_mixed_bundle_report_lists_only_actual_attempts_metrics_and_receipts():
    request, plan = _plan({'experiment': ('run-only', 'failed-assessment', 'measured')})
    work = _work_closure('run-only')
    failed = _work_closure('failed-assessment', attempted=True)
    measured = _closure('measured', ('experiment',), 'measured')
    report = build_report('realized', request, plan,
        {closure.target_ref: closure for closure in (work, failed, measured)})
    assert set(report.accepted_target_commit_refs) == {
        work.target_commit_ref, failed.target_commit_ref, measured.target_commit_ref}
    assert set(report.accepted_evaluation_attempt_refs) == {
        failed.evaluation_attempt_ref, measured.evaluation_attempt_ref}
    assert report.metric_result_refs == (measured.metric_result_ref,)
    assert measured.rg_formal_measurement_receipt.receipt_ref in report.owner_receipt_refs
    assert work.root_completion_receipt.receipt_ref in report.owner_receipt_refs


@pytest.mark.parametrize('missing', ['rm_asset_receipt', 'ar_execution_receipt',
    'rg_target_commit_receipt', 'variant_run_input_binding'])
def test_unassessed_work_still_requires_its_execution_and_asset_authorities(missing):
    _, plan = _plan({'experiment': ('run-only',)})
    work = replace(_work_closure('run-only'), **{missing: None})
    with pytest.raises(BundleProtocolError):
        result_sets(plan, {work.target_ref: work})


def test_unassessed_work_cannot_invent_a_metric_or_detach_an_actual_attempt():
    _, plan = _plan({'experiment': ('run-only',)})
    work = _work_closure('run-only')
    with pytest.raises(BundleProtocolError, match='cannot claim a MetricResult'):
        result_sets(plan, {work.target_ref: replace(work, metric_result_ref='invented')})
    with pytest.raises(BundleProtocolError, match='must exist together'):
        result_sets(plan, {work.target_ref: replace(work, evaluation_attempt_ref='unbound-attempt')})
