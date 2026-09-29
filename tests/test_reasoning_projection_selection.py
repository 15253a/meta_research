"""Historical display selects one event, then keeps the real Owner boundaries."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, call

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.reasoning_stage import ReasoningStageWorker, _CurrentCycle, _CYCLE_EVENT, _STAGE_REQUEST_EVENT


def _event(revision, cycle_ref, stage='reasoning'):
    return NS(revision=revision, payload={'stage': stage, 'cycle_ref': cycle_ref})


def _worker(monkeypatch, events):
    worker = object.__new__(ReasoningStageWorker)
    worker._feed = NS(read_event_type=Mock(return_value=events))
    worker._advancement_engine = NS(query_reasoning_stage_request=Mock(), query_active_foregrounds=Mock(return_value=()))
    worker._research_graph = NS(query_question_by_ref=Mock())
    monkeypatch.setattr(worker, '_discover_active_cycles', Mock(return_value=()))
    return worker


def _bind_selected(worker, cycle_ref='cycle-latest', question_ref='question-latest'):
    binding = NS(question_ref=question_ref, receipt_ref='receipt-latest')
    request = NS(cycle_ref=cycle_ref, accepted_question=binding)
    question = NS(question_ref=question_ref, quest_ref='quest-latest', as_binding=Mock(return_value=binding))

    def query(ref):
        assert ref == cycle_ref, 'Unselected historical request must not be reopened'
        return request

    worker._advancement_engine.query_reasoning_stage_request.side_effect = query
    worker._research_graph.query_question_by_ref.return_value = question
    return request, question


def test_only_latest_reasoning_event_is_authenticated_through_both_owners(monkeypatch):
    # Feed order and a later event from another stage must not select the candidate.
    events = (_event(9, 'cycle-latest'), _event(2, 'cycle-old'), _event(30, 'cycle-plan', 'plan'), _event(5, 'cycle-old'))
    worker = _worker(monkeypatch, events)
    _request, question = _bind_selected(worker)

    result = worker._discover_projection_cycle()

    assert result == _CurrentCycle(9, 'cycle-latest', question)
    worker._feed.read_event_type.assert_called_once_with(_STAGE_REQUEST_EVENT)
    worker._advancement_engine.query_reasoning_stage_request.assert_called_once_with('cycle-latest')
    worker._research_graph.query_question_by_ref.assert_called_once_with('question-latest')
    question.as_binding.assert_called_once_with()


@pytest.mark.parametrize('cycle_ref', [None, '', 42])
def test_invalid_latest_event_fails_without_falling_back_to_old_cycle(monkeypatch, cycle_ref):
    worker = _worker(monkeypatch, (_event(2, 'cycle-old'), _event(9, cycle_ref)))
    with pytest.raises(OwnerConflict, match='reasoning_cycle_index_invalid'):
        worker._discover_projection_cycle()
    worker._advancement_engine.query_reasoning_stage_request.assert_not_called()
    worker._research_graph.query_question_by_ref.assert_not_called()


@pytest.mark.parametrize('corruption', ['request_missing', 'request_rejected', 'question_missing', 'question_binding_mismatch', 'question_rejected'])
def test_latest_owner_failures_are_not_hidden_by_valid_older_history(monkeypatch, corruption):
    worker = _worker(monkeypatch, (_event(2, 'cycle-old'), _event(9, 'cycle-latest')))
    _request, question = _bind_selected(worker)
    error = 'reasoning_cycle_index_invalid'
    if corruption == 'request_missing':
        worker._advancement_engine.query_reasoning_stage_request.side_effect = None
        worker._advancement_engine.query_reasoning_stage_request.return_value = None
    elif corruption == 'request_rejected':
        worker._advancement_engine.query_reasoning_stage_request.side_effect = OwnerConflict('selected_closure_invalid')
        error = 'selected_closure_invalid'
    elif corruption == 'question_missing':
        worker._research_graph.query_question_by_ref.return_value = None
    elif corruption == 'question_binding_mismatch':
        question.as_binding.return_value = NS(question_ref='question-latest', receipt_ref='forged-receipt')
    else:
        worker._research_graph.query_question_by_ref.side_effect = OwnerConflict('selected_question_custody_unavailable')
        error = 'selected_question_custody_unavailable'
    with pytest.raises(OwnerConflict, match=error):
        worker._discover_projection_cycle()
    worker._advancement_engine.query_reasoning_stage_request.assert_called_once_with('cycle-latest')
    if corruption.startswith('request_'):
        worker._research_graph.query_question_by_ref.assert_not_called()
    else:
        worker._research_graph.query_question_by_ref.assert_called_once_with('question-latest')


def test_active_reasoning_still_wins_without_reading_history(monkeypatch):
    worker = _worker(monkeypatch, (_event(99, 'cycle-history'),))
    active = (_CurrentCycle(1, 'cycle-active-first', object()), _CurrentCycle(2, 'cycle-active-last', object()))
    worker._discover_active_cycles.return_value = active
    assert worker._discover_projection_cycle() is active[-1]
    worker._feed.read_event_type.assert_not_called()
    worker._advancement_engine.query_reasoning_stage_request.assert_not_called()
    worker._research_graph.query_question_by_ref.assert_not_called()


def test_no_history_still_uses_existing_initial_cycle_fallback(monkeypatch):
    worker = _worker(monkeypatch, ())
    worker._feed.read_event_type.side_effect = lambda event_type: (
        (_event(99, 'cycle-plan', 'plan'),) if event_type == _STAGE_REQUEST_EVENT else
        (NS(revision=4, payload={'cycle_ref': 'cycle-initial', 'question_ref': 'question-initial'}),)
    )
    worker._advancement_engine.query_active_foregrounds.return_value = (
        {'cycle_ref': 'cycle-initial', 'quest_ref': 'quest-initial'},
    )
    question = NS(question_ref='question-initial', quest_ref='quest-initial')
    worker._research_graph.query_question_by_ref.return_value = question
    assert worker._discover_projection_cycle() == _CurrentCycle(4, 'cycle-initial', question)
    assert worker._feed.read_event_type.call_args_list == [call(_STAGE_REQUEST_EVENT), call(_CYCLE_EVENT)]
    worker._advancement_engine.query_reasoning_stage_request.assert_not_called()
    worker._advancement_engine.query_active_foregrounds.assert_called_once_with(stage='reasoning')


def test_no_history_and_no_initial_cycle_remains_unavailable(monkeypatch):
    worker = _worker(monkeypatch, ())
    assert worker._discover_projection_cycle() is None
    worker._advancement_engine.query_reasoning_stage_request.assert_not_called()
    worker._research_graph.query_question_by_ref.assert_not_called()
