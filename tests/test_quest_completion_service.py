from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.quest_completion import QuestCompletionService


_QUEST_REF = "quest-completion-test"
_CYCLE_REF = "cycle-completion-test"
_REQUEST_REF = "reasoning-request-completion-test"
_OUTCOME_REF = "scientific-outcome-completion-test"
_CANDIDATE_REF = "candidate-completion-test"
_GOAL_REVISION_REF = "quest-goal-revision-completion-test"


def _candidate() -> dict[str, object]:
    return {
        "schema_ref": "meta-research/candidate-completion/v1",
        "kind": "CandidateCompletion",
        "source_quest_ref": _QUEST_REF,
        "source_cycle_ref": _CYCLE_REF,
        "source_reasoning_stage_run_request_ref": _REQUEST_REF,
        "source_scientific_outcome_ref": _OUTCOME_REF,
        "source_question_ref": "question-completion-test",
        "source_foreground_epoch": 7,
        "current_quest_ref": _QUEST_REF,
        "current_goal_revision_ref": _GOAL_REVISION_REF,
        "completion_milestone_basis_refs": ["stage-commit-idea"],
        "rationale": "The bounded milestone basis is terminal.",
        "is_authoritative": False,
    }


def _goal_revision() -> dict[str, object]:
    return {
        "kind": "QuestGoalRevision",
        "goal_revision_ref": _GOAL_REVISION_REF,
        "quest_ref": _QUEST_REF,
        "draft_revision": 3,
        "draft_hash": "quest-draft-hash",
        "goal": {
            "goal": "Reach a bounded result.",
            "completion_criteria": "Meet the accepted milestone basis.",
        },
        "rg_quest_acceptance_receipt_ref": "rg-quest-receipt",
    }


def _source() -> dict[str, object]:
    return {
        "quest_ref": _QUEST_REF,
        "cycle_ref": _CYCLE_REF,
        "reasoning_stage_run_request_ref": _REQUEST_REF,
        "scientific_outcome_ref": _OUTCOME_REF,
        "foreground_epoch": 7,
        "reasoning_content_acceptance_receipt_ref": "rm-content-receipt",
        "reasoning_domain_acceptance_receipt_ref": "rg-outcome-receipt",
    }


class _HumanCollaboration:
    def __init__(self, writes: list[str]) -> None:
        self._writes = writes
        self.context: dict[str, object] | None = None
        self.contexts: list[dict[str, object]] = []

    def prepare_quest_completion(self, **values: object) -> dict[str, object]:
        self._writes.append("human_collaboration.prepare")
        self.context = {
            "context_ref": "quest-completion-context-test",
            "source": values["source"],
            "candidate_completion_ref": values["candidate_completion_ref"],
            "candidate_completion_hash": values["candidate_completion_hash"],
            "candidate_completion": values["candidate_completion"],
            "goal_revision": values["goal_revision"],
            "human_confirmation": {"preview": None, "decision": None},
        }
        self.contexts.append(self.context)
        return self.context

    def query_current_quest_completion(self) -> dict[str, object] | None:
        return None if not self.contexts else self.contexts[-1]

    def query_quest_completion(
        self, context_ref: str
    ) -> dict[str, object] | None:
        return next(
            (
                context
                for context in self.contexts
                if context["context_ref"] == context_ref
            ),
            None,
        )

    def query_quest_completion_contexts(
        self,
    ) -> tuple[dict[str, object], ...]:
        return tuple(self.contexts)

    def add_context(
        self, context_ref: str, *, previewed: bool = False
    ) -> dict[str, object]:
        assert self.context is not None
        context = deepcopy(self.context)
        context["context_ref"] = context_ref
        context["human_confirmation"] = {"preview": None, "decision": None}
        self.contexts.append(context)
        if previewed:
            self.preview_quest_completion(
                context_ref, idempotency_key=f"preview:{context_ref}"
            )
        return context

    def preview_quest_completion(
        self, context_ref: str, *, idempotency_key: str
    ) -> dict[str, object]:
        del idempotency_key
        context = self.query_quest_completion(context_ref)
        assert context is not None
        self._writes.append("human_collaboration.preview")
        preview = {
            "status": "current",
            "ref": "completion-preview-test",
            "hash": "completion-preview-hash-test",
            "candidate_completion_ref": _CANDIDATE_REF,
            "candidate_completion_hash": canonical_hash(_candidate()),
            "quest_ref": _QUEST_REF,
            "goal_revision_ref": _GOAL_REVISION_REF,
            "completion_milestone_basis_refs": ["stage-commit-idea"],
        }
        context["human_confirmation"] = {
            "preview": preview,
            "decision": None,
        }
        return preview

    def confirm(
        self, decision: str, *, context_ref: str | None = None
    ) -> dict[str, object]:
        context = (
            self.query_current_quest_completion()
            if context_ref is None
            else self.query_quest_completion(context_ref)
        )
        assert context is not None
        confirmation = {
            "decision": decision,
            "receipt": {
                "issuer": "human_collaboration",
                "kind": "quest_completion_confirmation",
                "receipt_ref": "hc-completion-receipt-test",
                "subject_ref": "completion-preview-test",
                "payload_hash": "hc-completion-receipt-hash-test",
            },
        }
        confirmation_state = context["human_confirmation"]
        assert isinstance(confirmation_state, dict)
        confirmation_state["decision"] = confirmation
        return confirmation


class _ResearchGraph:
    def __init__(self, writes: list[str]) -> None:
        self._writes = writes
        self.goal_revision = _goal_revision()
        self.acceptance: dict[str, object] | None = None
        self.accepted_confirmation: dict[str, object] | None = None

    def query_candidate_completion(
        self, *, source_outcome_ref: str, candidate_completion_ref: str
    ) -> dict[str, object] | None:
        if (
            source_outcome_ref != _OUTCOME_REF
            or candidate_completion_ref != _CANDIDATE_REF
        ):
            return None
        candidate = _candidate()
        return {
            "candidate_completion_ref": _CANDIDATE_REF,
            "candidate_completion_hash": canonical_hash(candidate),
            "candidate_completion": candidate,
            "source": _source(),
            "goal_revision": _goal_revision(),
        }

    def query_current_quest_goal_revision(
        self, quest_ref: str
    ) -> dict[str, object] | None:
        assert quest_ref == _QUEST_REF
        return self.goal_revision

    def query_quest_completion_acceptance(
        self, candidate_completion_ref: str
    ) -> dict[str, object] | None:
        assert candidate_completion_ref == _CANDIDATE_REF
        return self.acceptance

    def accept_quest_completion(self, **values: object) -> dict[str, object]:
        self._writes.append("research_graph.accept")
        assert values["source_outcome_ref"] == _OUTCOME_REF
        assert values["candidate_completion_ref"] == _CANDIDATE_REF
        confirmation = values["human_confirmation"]
        assert isinstance(confirmation, dict)
        self.accepted_confirmation = confirmation
        self.acceptance = {
            "status": "accepted",
            "completion_ref": "rg-completion-test",
            "candidate_completion_ref": _CANDIDATE_REF,
            "goal_revision_ref": _GOAL_REVISION_REF,
            "receipt": {
                "issuer": "research_graph",
                "kind": "quest_completion_accepted",
                "receipt_ref": "rg-completion-receipt-test",
                "subject_ref": "rg-completion-test",
                "payload_hash": "rg-completion-receipt-hash-test",
            },
        }
        return self.acceptance


@dataclass(frozen=True)
class _Commit:
    closure: dict[str, object]


class _AdvancementEngine:
    def __init__(self, writes: list[str]) -> None:
        self._writes = writes
        self.commit: _Commit | None = None
        self.ending: dict[str, object] | None = None
        self.received_completion_receipt: object | None = None

    def query_foreground(self, quest_ref: str) -> dict[str, object] | None:
        assert quest_ref == _QUEST_REF
        return {
            "quest_ref": _QUEST_REF,
            "cycle_ref": _CYCLE_REF,
            "stage": "reasoning",
            "epoch": 7,
            "status": "active" if self.ending is None else "completed",
        }

    def query_reasoning_stage_commit(self, request_ref: str) -> _Commit | None:
        assert request_ref == _REQUEST_REF
        return self.commit

    def query_quest_ending(self, quest_ref: str) -> dict[str, object] | None:
        assert quest_ref == _QUEST_REF
        return self.ending

    def end_quest(self, **values: object) -> dict[str, object]:
        self._writes.append("advancement_engine.end")
        self.received_completion_receipt = values["completion_receipt"]
        self.ending = {
            "status": "ended",
            "transition_ref": "quest-ending-test",
            "quest_ref": _QUEST_REF,
            "candidate_completion_ref": _CANDIDATE_REF,
            "receipt": {
                "issuer": "advancement_engine",
                "kind": "quest_ending",
                "receipt_ref": "ae-ending-receipt-test",
                "subject_ref": _QUEST_REF,
                "payload_hash": "ae-ending-receipt-hash-test",
            },
        }
        return self.ending


def _service():
    writes: list[str] = []
    human = _HumanCollaboration(writes)
    graph = _ResearchGraph(writes)
    advancement = _AdvancementEngine(writes)
    return (
        QuestCompletionService(human, graph, advancement),
        human,
        graph,
        advancement,
        writes,
    )


def test_recovers_and_crosses_exactly_one_owner_effect_per_tick() -> None:
    service, human, graph, advancement, writes = _service()

    started = service.start(
        source_outcome_ref=_OUTCOME_REF,
        candidate_completion_ref=_CANDIDATE_REF,
        idempotency_key="completion-start-test",
    )
    assert started["status"] == "prepared"
    assert started["candidate_completion"] == _candidate()
    assert started["source"] == _source()
    assert started["human_confirmation"] == {
        "status": "not_attempted",
        "preview": None,
        "decision": None,
    }
    assert writes == ["human_collaboration.prepare"]

    writes.clear()
    assert service.process_once()
    assert writes == ["human_collaboration.preview"]
    assert service.query_current()["status"] == "awaiting_human_confirmation"

    writes.clear()
    assert not service.process_once()
    assert writes == []

    confirmation = human.confirm("confirmed")
    assert service.process_once()
    assert writes == ["research_graph.accept"]
    current = service.query_current()
    assert current is not None and current["status"] == "domain_accepted"
    assert graph.accepted_confirmation == confirmation

    writes.clear()
    assert not service.process_once()
    assert writes == []

    advancement.commit = _Commit(
        {
            "transition_kind": "candidate_completion",
            "transition_ref": _CANDIDATE_REF,
            "transition_hash": canonical_hash(_candidate()),
            "transition": _candidate(),
        }
    )
    assert service.process_once()
    assert writes == ["advancement_engine.end"]
    assert advancement.received_completion_receipt is graph.acceptance["receipt"]
    terminal = service.query_current()
    assert terminal is not None
    assert terminal["status"] == "ended"
    assert terminal["quest"] == {"quest_ref": _QUEST_REF, "status": "ended"}

    recovered = QuestCompletionService(human, graph, advancement)
    assert recovered.query_current() == terminal
    writes.clear()
    assert not recovered.process_once()
    assert writes == []


@pytest.mark.parametrize("decision", [None, "rejected"])
def test_missing_or_rejected_human_decision_fails_closed(
    decision: str | None,
) -> None:
    service, human, graph, _advancement, writes = _service()
    service.start(
        source_outcome_ref=_OUTCOME_REF,
        candidate_completion_ref=_CANDIDATE_REF,
        idempotency_key="completion-blocked-start",
    )
    service.process_once()
    writes.clear()
    if decision is not None:
        human.confirm(decision)

    assert not service.process_once()
    assert graph.acceptance is None
    assert writes == []
    current = service.query_current()
    assert current is not None
    assert current["status"] == (
        "rejected" if decision == "rejected" else "awaiting_human_confirmation"
    )


def test_goal_drift_fails_closed_before_an_owner_effect() -> None:
    service, human, graph, _advancement, writes = _service()
    service.start(
        source_outcome_ref=_OUTCOME_REF,
        candidate_completion_ref=_CANDIDATE_REF,
        idempotency_key="completion-stale-start",
    )
    service.process_once()
    human.confirm("confirmed")
    graph.goal_revision = {**_goal_revision(), "draft_revision": 4}
    writes.clear()

    assert not service.process_once()
    assert graph.acceptance is None
    assert writes == []
    current = service.query_current()
    assert current is not None and current["status"] == "stale"


def test_start_rejects_a_model_only_or_tampered_candidate() -> None:
    service, _human, graph, _advancement, _writes = _service()
    assert not service.process_once()
    graph.query_candidate_completion = lambda **_values: None  # type: ignore[method-assign]

    with pytest.raises(OwnerConflict, match="candidate_completion_not_accepted"):
        service.start(
            source_outcome_ref=_OUTCOME_REF,
            candidate_completion_ref=_CANDIDATE_REF,
            idempotency_key="completion-model-only-start",
        )


def test_restart_skips_latest_awaiting_human_and_advances_older_context() -> None:
    service, human, _graph, advancement, writes = _service()
    older = service.start(
        source_outcome_ref=_OUTCOME_REF,
        candidate_completion_ref=_CANDIDATE_REF,
        idempotency_key="completion-older-start",
    )
    latest = human.add_context(
        "quest-completion-context-latest", previewed=True
    )
    writes.clear()

    restarted = QuestCompletionService(human, _graph, advancement)
    assert restarted.query_current()["context_ref"] == latest["context_ref"]
    assert restarted.process_once()
    assert writes == ["human_collaboration.preview"]
    assert restarted.query(str(older["context_ref"]))[
        "human_confirmation"
    ]["status"] == "awaiting_response"


def test_scheduler_round_robins_two_actionable_completion_contexts() -> None:
    service, human, graph, _advancement, writes = _service()
    first = service.start(
        source_outcome_ref=_OUTCOME_REF,
        candidate_completion_ref=_CANDIDATE_REF,
        idempotency_key="completion-first-start",
    )
    second = human.add_context("quest-completion-context-second")
    writes.clear()

    assert service.process_once()
    assert writes == ["human_collaboration.preview"]
    human.confirm("confirmed", context_ref=str(first["context_ref"]))
    writes.clear()

    # The first context is now ready for RG, but the in-service cursor gives
    # the other Quest one HC boundary before returning to it.
    assert service.process_once()
    assert writes == ["human_collaboration.preview"]
    assert graph.acceptance is None
    assert service.query(str(second["context_ref"]))[
        "human_confirmation"
    ]["status"] == "awaiting_response"
