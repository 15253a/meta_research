"""Defer terminal handoff proofs until no current Target is still running."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, call

import pytest

from meta_research.bundle_protocol import (
    AcceptedMeasurementClosure,
    SemanticBarrier,
    TechnicalBlocker,
)
from meta_research.bundle_stage import BundleStageWorker
from meta_research.owners.common import OwnerConflict


def _terminal(kind):
    # This seam only classifies values returned by AR. The actual AR verifier is
    # covered by the report-owner tests; no record fields are read by the hint.
    return object.__new__(kind)


def _worker(states, terminals=None):
    refs = tuple(f"target-{index}" for index in range(len(states)))
    frontiers = {
        ref: None if state is None else NS(
            state=state, currentness_known=True, current=True
        )
        for ref, state in zip(refs, states)
    }
    notices = {
        ref: None if state is None else NS(
            target_ref=ref, handoff_manifest_ref=f"handoff-{ref}"
        )
        for ref, state in zip(refs, states)
    }
    supplied = terminals or (AcceptedMeasurementClosure,) * len(states)
    handoffs = {
        f"handoff-{ref}": NS(terminal=_terminal(kind))
        for ref, kind in zip(refs, supplied)
    }
    owner = NS(
        query_target_frontier_entry=Mock(side_effect=frontiers.__getitem__),
        query_target_work_notice=Mock(side_effect=notices.__getitem__),
        read_target_run_handoff=Mock(side_effect=handoffs.__getitem__),
    )
    worker = object.__new__(BundleStageWorker)
    worker._agent_runtime = owner
    graph = NS(targets=tuple(NS(target_ref=ref) for ref in refs))
    return worker, graph, owner, frontiers, notices


@pytest.mark.parametrize("pending", ["running", "unknown_currentness", "stale"])
def test_later_pending_target_skips_all_earlier_handoff_proofs(pending):
    worker, graph, owner, frontiers, _ = _worker(("terminal", "terminal", "running"))
    if pending != "running":
        frontiers["target-2"].state = "terminal"
        if pending == "unknown_currentness":
            frontiers["target-2"].currentness_known = False
        else:
            frontiers["target-2"].current = False
    assert worker._bundle_report_disposition_hint(graph) is None
    owner.read_target_run_handoff.assert_not_called()
    assert owner.query_target_frontier_entry.call_args_list == [
        call("target-0"), call("target-1"), call("target-2")
    ]
    assert owner.query_target_work_notice.call_args_list == [
        call("target-0"), call("target-1"), call("target-2")
    ]


def test_deferred_handoff_error_is_rechecked_when_last_target_finishes():
    worker, graph, owner, frontiers, _ = _worker(("terminal", "running"))
    owner.read_target_run_handoff.side_effect = OwnerConflict("target_run_handoff_integrity_invalid")
    assert worker._bundle_report_disposition_hint(graph) is None
    owner.read_target_run_handoff.assert_not_called()

    frontiers["target-1"].state = "terminal"
    with pytest.raises(OwnerConflict, match="target_run_handoff_integrity_invalid"):
        worker._bundle_report_disposition_hint(graph)
    owner.read_target_run_handoff.assert_called_once_with("handoff-target-0")


@pytest.mark.parametrize("problem", ["missing", "wrong_target", "orphan"])
def test_bad_notice_before_running_target_still_rejects(problem):
    states = (None, "running") if problem == "orphan" else ("terminal", "running")
    worker, graph, owner, _, notices = _worker(states)
    error = "bundle_report_handoff_invalid"
    if problem == "missing":
        notices["target-0"] = None
        error = "bundle_report_handoff_missing"
    else:
        notices["target-0"] = NS(target_ref="wrong-target", handoff_manifest_ref="wrong-handoff")
    with pytest.raises(OwnerConflict, match=error):
        worker._bundle_report_disposition_hint(graph)
    owner.query_target_frontier_entry.assert_called_once_with("target-0")
    owner.query_target_work_notice.assert_called_once_with("target-0")
    owner.read_target_run_handoff.assert_not_called()


def test_running_target_keeps_existing_early_return_before_later_bad_notice():
    worker, graph, owner, _, notices = _worker(("running", "terminal"))
    notices["target-1"] = None
    assert worker._bundle_report_disposition_hint(graph) is None
    owner.query_target_frontier_entry.assert_called_once_with("target-0")
    owner.query_target_work_notice.assert_called_once_with("target-0")
    owner.read_target_run_handoff.assert_not_called()


@pytest.mark.parametrize(
    ("states", "kinds", "expected"),
    [
        (("terminal", "terminal"), (AcceptedMeasurementClosure, AcceptedMeasurementClosure), "realized"),
        (("terminal", "terminal"), (AcceptedMeasurementClosure, SemanticBarrier), "replan_required"),
        (("terminal", "terminal"), (SemanticBarrier, TechnicalBlocker), "blocked"),
        (("terminal", "terminal"), (TechnicalBlocker, SemanticBarrier), "blocked"),
        (("terminal", None), (TechnicalBlocker, AcceptedMeasurementClosure), "blocked"),
        ((None, "terminal"), (AcceptedMeasurementClosure, TechnicalBlocker), "blocked"),
        (("terminal", None), (SemanticBarrier, AcceptedMeasurementClosure), None),
        (("terminal", None), (AcceptedMeasurementClosure, AcceptedMeasurementClosure), None),
        ((None,), (AcceptedMeasurementClosure,), None),
        (("terminal",), (object,), None),
        ((), (), None),
    ],
)
def test_terminal_disposition_and_unlaunched_precedence_are_preserved(states, kinds, expected):
    worker, graph, owner, _, _ = _worker(states, kinds)
    assert worker._bundle_report_disposition_hint(graph) == expected
    assert owner.read_target_run_handoff.call_args_list == [
        call(f"handoff-target-{index}")
        for index, state in enumerate(states)
        if state is not None
    ]


def test_blocker_does_not_skip_strict_validation_of_later_terminal_handoff():
    worker, graph, owner, _, _ = _worker(
        ("terminal", "terminal"), (TechnicalBlocker, AcceptedMeasurementClosure)
    )
    owner.read_target_run_handoff.side_effect = [
        NS(terminal=_terminal(TechnicalBlocker)),
        OwnerConflict("target_run_handoff_integrity_invalid"),
    ]
    with pytest.raises(OwnerConflict, match="target_run_handoff_integrity_invalid"):
        worker._bundle_report_disposition_hint(graph)
    assert owner.read_target_run_handoff.call_args_list == [
        call("handoff-target-0"), call("handoff-target-1")
    ]
