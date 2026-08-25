"""HC completion commands return the exact addressed context, not global latest."""

from __future__ import annotations

from pathlib import Path

from meta_research.owners.common import canonical_hash

from test_public_autonomous_completion_owners import (
    _completion_inputs,
    _owner_runtime,
)
from test_public_plan_stage import _confirm_direct_quest


def test_completion_preview_returns_its_addressed_context(
    tmp_path: Path,
) -> None:
    runtime = _owner_runtime(tmp_path / "hc-completion-concurrency")
    try:
        quest = _confirm_direct_quest(runtime)
        human = runtime.owners.human_collaboration
        first_values = _completion_inputs(runtime, quest)
        first = human.prepare_quest_completion(
            **first_values,
            idempotency_key="completion-concurrency:first",
        )

        second_values = _completion_inputs(runtime, quest)
        second_candidate = {
            **second_values["candidate_completion"],
            "rationale": "另一个并行 completion 候选，不能污染第一个预览。",
        }
        second_values = {
            **second_values,
            "candidate_completion": second_candidate,
            "candidate_completion_ref": "candidate-completion:concurrent-second",
            "candidate_completion_hash": canonical_hash(second_candidate),
        }
        second = human.prepare_quest_completion(
            **second_values,
            idempotency_key="completion-concurrency:second",
        )
        assert second["context_ref"] != first["context_ref"]
        assert human.query_current_quest_completion() == second
        assert human.query_quest_completion_contexts() == (first, second)

        preview = human.preview_quest_completion(
            str(first["context_ref"]),
            idempotency_key="completion-concurrency:first-preview",
        )
        assert preview is not None
        assert preview["candidate_completion_ref"] == first_values[
            "candidate_completion_ref"
        ]
        assert human.query_quest_completion(str(first["context_ref"]))[
            "human_confirmation"
        ]["preview"] == preview
        assert human.query_current_quest_completion()["context_ref"] == second[
            "context_ref"
        ]
    finally:
        runtime.close()
