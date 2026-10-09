from __future__ import annotations


class GoalWorkReconciler:
    """Apply at most one issuer-owned mechanical work effect per tick."""

    def __init__(self, research_graph, target_lifecycle, advancement_engine) -> None:
        self._research_graph = research_graph
        self._target_lifecycle = target_lifecycle
        self._advancement_engine = advancement_engine

    def process_once(self) -> bool:
        intent = self._research_graph.next_unfulfilled_goal_work_intent()
        if intent is not None:
            if intent["decision_kind"] == "stop":
                self._target_lifecycle.request_cancel_exact(
                    intent_ref=intent["intent_ref"]
                )
            else:
                self._target_lifecycle.block_admission_exact(
                    intent_ref=intent["intent_ref"]
                )
            return True
        reassessment = self._advancement_engine.query_goal_reasoning_reassessment()
        if reassessment is None:
            return False
        self._advancement_engine.ensure_goal_reasoning_reassessment(
            quest_ref=reassessment["quest_ref"],
            goal_revision_ref=reassessment["goal_revision_ref"],
            idempotency_key=(
                "goal-reasoning-reassessment-"
                + str(reassessment["goal_revision_ref"])
                + ":" + str(reassessment["source_epoch"])
            )
        )
        return True
