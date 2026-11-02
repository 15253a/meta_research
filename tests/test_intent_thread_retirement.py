"""The creation thread may be handed to a Quest before its own retirement."""
from dataclasses import replace
from pathlib import Path
import sqlite3

from test_codex_shell_environment import _vendor
from test_creation_companion_native_rotation import _send
from test_native_session_cleanup import _saved_threads
from test_public_human_collaboration_ladder import _DeterministicDraftingProvider, _confirm_direct_quest, _runtime


class _IntentProvider(_DeterministicDraftingProvider):
    def reply(self, request):
        return replace(super().reply(request), native_session_ref=self.native)


def _open_intent(runtime, provider):
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "handoff-quest-open")
    provider.native, = _saved_threads(_vendor(), runtime.data_root, ["creation evidence"])
    _send(human, opened, "Start from these observations.", "bind-intent")
    assert human.process_drafting_once()
    view = human.query_quest_creation(opened["initialization_id"])
    assert view["intent_session"]["native_session_ref"] == provider.native
    directory = runtime.root_workspaces.bind_initialization(opened["initialization_id"], view["intent_session"]["ref"]).directory
    marker = directory / "notes.txt"
    marker.write_text("Research notes.")
    with sqlite3.connect(runtime.data_root.codex_home / "state_5.sqlite") as db:
        path, = db.execute("SELECT rollout_path FROM threads WHERE id=?", (provider.native,)).fetchone()
    return opened["initialization_id"], Path(path), marker


def test_transferred_intent_retires_only_after_quest_rotates(tmp_path):
    provider = _IntentProvider()
    runtime = _runtime(tmp_path / "handoff", provider)
    try:
        human = runtime.owners.human_collaboration
        init, rollout, marker = _open_intent(runtime, provider)
        confirmed = _confirm_direct_quest(runtime, key_prefix="handoff")
        assert confirmed["intent_session"]["status"] == "closed"
        cleanup = runtime.target_run_runtime.cleanup_completed_workspaces
        cleanup(dry_run=False)
        assert rollout.is_file()  # Confirmation closed Intent; the handoff is still pending.

        for _ in range(16):
            human.reconcile_once()
            view = human.query_quest_creation(init)
            if view.get("quest_ref"):
                scope = "quest:" + view["quest_ref"]
                if human.query_companion(scope)["native_session_ref"] == provider.native:
                    break
        assert human.query_companion(scope)["native_session_ref"] == provider.native
        cleanup(dry_run=False)
        assert rollout.is_file()
        human.start_new_companion_session(scope, "quest-new")
        cleanup(dry_run=False)
        assert not rollout.exists()
        assert marker.read_text() == "Research notes."
    finally:
        runtime.close()


def test_cancelled_intent_final_generation_releases_rollout_not_research_notes(tmp_path):
    provider = _IntentProvider()
    runtime = _runtime(tmp_path / "cancel", provider)
    try:
        human = runtime.owners.human_collaboration
        init, rollout, marker = _open_intent(runtime, provider)
        cleanup = runtime.target_run_runtime.cleanup_completed_workspaces
        cleanup(dry_run=False)
        assert rollout.is_file()
        human.cancel_quest(init, "cancel-intent")
        cleanup(dry_run=False)
        assert not rollout.exists()
        assert marker.read_text() == "Research notes."
    finally:
        runtime.close()


def test_cancelled_intent_keeps_thread_until_running_provider_settles(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    class DelayedReply(_IntentProvider):
        def reply(self, request):
            if request.message == "Continue reading.":
                entered.set()
                assert release.wait(40)
            return super().reply(request)

    provider = DelayedReply()
    runtime = _runtime(tmp_path / "cancel-pending", provider)
    try:
        human = runtime.owners.human_collaboration
        init, rollout, marker = _open_intent(runtime, provider)
        _send(human, human.query_quest_creation(init), "Continue reading.", "pending-at-cancel")
        with ThreadPoolExecutor(max_workers=1) as pool:
            turn = pool.submit(human.process_drafting_once)
            try:
                assert entered.wait(20)
                human.cancel_quest(init, "cancel-running")
                runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
                assert rollout.is_file()
            finally:
                release.set()
            assert not turn.result(timeout=20)  # Cancelled work rejects the late reply.
        runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False)
        assert not rollout.exists()
        assert marker.read_text() == "Research notes."
    finally:
        runtime.close()
