"""Writing session rotation and its first message share one commit boundary."""
from contextlib import contextmanager

import pytest

from meta_research.owners.common import OwnerConflict
from test_public_manual_question_lifecycle import _accept_root_question, _build_runtime


@pytest.mark.parametrize("competitor_kind", ["writing", "ordinary"])
def test_writing_owns_first_turn_even_when_another_request_arrives_at_rotation_commit(tmp_path, monkeypatch, competitor_kind):
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest, _ = _accept_root_question(runtime, "atomic-writing")
        human = runtime.owners.human_collaboration
        scope = "quest:" + quest
        before = human.query_companion(scope)
        writing = {"requirements": "Keep the evidence boundary."}
        original_write = runtime._database.fenced_write
        competing_result = {}
        observed_turns = []

        @contextmanager
        def interleave_at_first_rotation_commit():
            with original_write() as connection:
                yield connection
            if competing_result:
                return
            current = human.query_companion(scope)
            if current["native_session_generation"] != before["native_session_generation"] + 1:
                return
            # Schedule B immediately after the rotation's first visible commit.
            # No thread timing assumption: B completes before A leaves this seam.
            competing_result["attempted"] = True
            observed_turns.extend(current["turns"])
            try:
                competing_result["turn"] = human.send_companion_message(
                    scope, "Competing request", "competing-request",
                    writing_request=writing if competitor_kind == "writing" else None)
            except OwnerConflict as error:
                competing_result["error"] = error.code

        monkeypatch.setattr(runtime._database, "fenced_write", interleave_at_first_rotation_commit)
        queued = human.send_companion_message(scope, "First writing request", "first-writing", writing_request=writing)
        assert competing_result["attempted"]
        assert [turn["interaction_ref"] for turn in observed_turns] == [queued["interaction_ref"]]
        assert queued["native_session_generation"] == before["native_session_generation"] + 1
        turns = human.query_companion(scope)["turns"]
        assert turns[0]["interaction_ref"] == queued["interaction_ref"]
        if competitor_kind == "writing":
            assert competing_result["error"] == "companion_session_busy"
            assert len(turns) == 1
        else:
            assert turns[1]["interaction_ref"] == competing_result["turn"]["interaction_ref"]
        replay = human.send_companion_message(scope, "First writing request", "first-writing", writing_request=writing)
        assert replay["interaction_ref"] == queued["interaction_ref"]
        assert human.query_companion(scope)["native_session_generation"] == queued["native_session_generation"]
    finally:
        runtime.close()
