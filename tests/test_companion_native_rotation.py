"""Companion rotation at the public command/query and HTTP boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.quest_drafting import IntentTurnResult
from test_public_human_collaboration_ladder import _CompanionRuntimeBinding, _DeterministicDraftingProvider, _runtime
from test_public_human_collaboration_web import _authenticated_client
from test_public_manual_question_lifecycle import _accept_root_question, _build_runtime


class RotationProvider(_DeterministicDraftingProvider):
    def reply(self, request):
        self.intent_requests.append(request)
        return IntentTurnResult(
            reply="Observed the current workspace.",
            native_session_ref=request.native_session_ref or f"native-{len(self.intent_requests)}",
            adapter_kind="rotation_boundary_test",
        )


def test_new_native_session_preserves_workspace_history_and_replays_after_restart(tmp_path):
    provider = RotationProvider()
    runtime = _runtime(tmp_path / "runtime", provider)
    client, headers = _authenticated_client(runtime)
    try:
        human = runtime.owners.human_collaboration
        human.send_companion_message("workspace", "Prior conversation marker.", "old-turn")
        assert human.process_drafting_once()
        original = human.query_companion("workspace")
        binding = runtime.root_workspaces.bind_companion_session("workspace", original["session_ref"])
        (binding.directory / "retained-draft.txt").write_text("prior material", encoding="utf-8")
        response = client.post("/api/v1/companion/sessions/new", headers={**headers,
            "Idempotency-Key": "rotate-once"}, json={"scope_ref": "workspace"})
        assert response.status_code == 200, response.text
        switched = response.json()
        assert switched["native_session_generation"] == 2
        assert switched["previous_native_session_ref"] == "native-1"
        assert switched["native_session_ref"] is None
        human.send_companion_message("workspace", "Read the retained draft.", "new-turn")
        assert human.process_drafting_once()
        current = human.query_companion("workspace")
        assert current["session_ref"] == original["session_ref"]
        assert current["workspace_ref"] == binding.location.workspace_ref
        assert current["workspace_path"] == str(binding.directory)
        assert current["native_session_ref"] == "native-2"
        assert [(turn["native_session_generation"], turn["native_session_ref"])
            for turn in current["turns"]] == [(1, "native-1"), (2, "native-2")]
        assert [(item["generation"], item["native_session_ref"])
            for item in current["native_sessions"]] == [(1, "native-1"), (2, "native-2")]
        assert provider.intent_requests[-1].native_session_ref is None
        assert "Prior conversation marker" not in str(provider.intent_requests[-1].draft)
        assert runtime.root_workspaces.read_companion_session("workspace", original["session_ref"],
            workspace_ref=binding.location.workspace_ref, path="retained-draft.txt")["content"] == b"prior material"
        client.close()
        runtime.close()
        runtime = _runtime(tmp_path / "runtime", RotationProvider())
        human = runtime.owners.human_collaboration
        assert human.start_new_companion_session("workspace", "rotate-once") == switched
        restored = human.query_companion("workspace")
        assert restored["native_session_ref"] == "native-2"
        assert restored["native_sessions"] == current["native_sessions"]
        assert restored["turns"] == current["turns"]
    finally:
        client.close()
        runtime.close()


def test_queued_turns_wait_for_the_same_native_session_and_rotation_rejects_busy(tmp_path):
    entered, release = Event(), Event()

    class BlockingProvider(RotationProvider):
        def reply(self, request):
            if not entered.is_set():
                entered.set()
                assert release.wait(30)
            return super().reply(request)

    provider = BlockingProvider()
    runtime = _runtime(tmp_path / "runtime", provider)
    try:
        human = runtime.owners.human_collaboration
        first = human.send_companion_message("workspace", "First turn.", "first")
        human.send_companion_message("workspace", "Queued turn.", "second")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(human.process_drafting_once)
            assert entered.wait(10)
            try:
                busy = human.query_companion("workspace")
                assert busy["can_start_new_session"] is False
                assert busy["switch_block_reason"] == "companion_session_busy"
                with pytest.raises(OwnerConflict, match="companion_session_busy"):
                    human.start_new_companion_session("workspace", "new-while-busy")
                assert human.process_drafting_once() is False
            finally:
                release.set()
            assert future.result(timeout=15)
        assert human.process_drafting_once()
        current = human.query_companion("workspace")
        assert [turn["assistant_status"] for turn in current["turns"]] == ["completed", "completed"]
        assert [turn["native_session_ref"] for turn in current["turns"]] == ["native-1", "native-1"]
        assert human.send_companion_message("workspace", "First turn.", "first")["interaction_ref"] == first["interaction_ref"]
        switched = human.start_new_companion_session("workspace", "new-while-busy")
        human.send_companion_message("workspace", "New queued turn.", "third")
        assert human.start_new_companion_session("workspace", "new-while-busy") == switched
        with pytest.raises(OwnerConflict, match="companion_session_busy"):
            human.start_new_companion_session("workspace", "another-switch")
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize("scope_kind", ("workspace", "quest"))
def test_late_pre_switch_completion_after_service_rebuild_cannot_replace_new_native(tmp_path, scope_kind):
    entered, release = Event(), Event()

    class BoundProvider(RotationProvider):
        def runtime_binding(self):
            return _CompanionRuntimeBinding()

    class DelayedProvider(BoundProvider):
        def reply(self, request):
            entered.set()
            assert release.wait(40)
            return super().reply(request)

    def build(provider):
        return _build_runtime(tmp_path / "runtime", drafting=provider)

    original = build(DelayedProvider())
    recovered = None
    try:
        first_human = original.owners.human_collaboration
        scope_ref = "workspace" if scope_kind == "workspace" else "quest:" + _accept_root_question(original, "late")[0]
        first_human.send_companion_message(scope_ref, "Old provider response may arrive late.", "old-pending")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first_human.process_drafting_once)
            assert entered.wait(10)
            try:
                recovered = build(BoundProvider())
                human = recovered.owners.human_collaboration
                assert human.process_drafting_once()
                human.start_new_companion_session(scope_ref, "after-recovery")
                human.send_companion_message(scope_ref, "Continue in the new generation.", "after-switch")
                assert human.process_drafting_once()
                before = human.query_companion(scope_ref)
                assert before["native_session_generation"] == 2
                assert before["native_session_ref"] is not None
            finally:
                release.set()
            assert future.result(timeout=15) is False
        assert human.query_companion(scope_ref) == before
        assert first_human.query_companion(scope_ref) == before
    finally:
        release.set()
        if recovered is not None:
            recovered.close()
        original.close()


@pytest.mark.parametrize("scope_kind", ("initialization", "quest"))
def test_rotation_keeps_other_scopes_and_their_workspace_material_isolated(tmp_path, scope_kind):
    runtime = _build_runtime(tmp_path / "runtime", drafting=RotationProvider())
    try:
        human = runtime.owners.human_collaboration
        if scope_kind == "quest":
            scopes = ["quest:" + _accept_root_question(runtime, name)[0] for name in ("a", "b")]
        else:
            scopes = ["quest-initialization:" + human.create_quest({}, "a")["initialization_id"], "workspace"]
        for index, scope in enumerate(scopes):
            human.send_companion_message(scope, "Keep this scope's work separate.", f"scope-{index}")
            assert human.process_drafting_once()
        sessions = [human.query_companion(scope) for scope in scopes]
        assert sessions[0]["workspace_ref"] != sessions[1]["workspace_ref"]
        first = runtime.root_workspaces.bind_companion_session(scopes[0], sessions[0]["session_ref"])
        (first.directory / "private-note.txt").write_text("scope a", encoding="utf-8")
        human.start_new_companion_session(scopes[0], "first-scope-only")
        assert human.query_companion(scopes[1]) == sessions[1]
        assert runtime.root_workspaces.read_companion_session(scopes[0], sessions[0]["session_ref"],
            workspace_ref=sessions[0]["workspace_ref"], path="private-note.txt")["content"] == b"scope a"
        from meta_research.semantic_mcp import SemanticMcpError
        with pytest.raises(SemanticMcpError, match="workspace_not_visible"):
            runtime.root_workspaces.read_companion_session(scopes[1], sessions[1]["session_ref"],
                workspace_ref=sessions[0]["workspace_ref"], path="private-note.txt")
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            human.start_new_companion_session(scopes[1], "first-scope-only")
    finally:
        runtime.close()


def test_a_provider_cannot_reuse_an_old_native_id_after_new_session(tmp_path):
    runtime = _runtime(tmp_path / "runtime", _DeterministicDraftingProvider())
    try:
        human = runtime.owners.human_collaboration
        human.send_companion_message("workspace", "First native conversation.", "first")
        assert human.process_drafting_once()
        original = human.query_companion("workspace")
        human.start_new_companion_session("workspace", "new")
        human.send_companion_message("workspace", "Must start a new native conversation.", "second")
        assert human.process_drafting_once()
        current = human.query_companion("workspace")
        assert current["native_session_ref"] is None
        assert current["turns"][-1]["assistant_status"] == "failed"
        assert current["turns"][-1]["reason"] == {"code": "companion_native_session_reused"}
        assert current["native_sessions"][0] == original["native_sessions"][0]
    finally:
        runtime.close()


def test_new_session_can_read_old_conversation_on_demand_without_prompt_reinjection(tmp_path):
    import json

    provider = RotationProvider()
    runtime = _runtime(tmp_path / "runtime", provider)
    try:
        human = runtime.owners.human_collaboration
        human.send_companion_message("workspace", "Keep this previous-dialogue fact on demand.", "old")
        assert human.process_drafting_once()
        original = human.query_companion("workspace")
        human.start_new_companion_session("workspace", "new")
        human.send_companion_message("workspace", "Find the prior fact only if needed.", "fresh")
        assert human.process_drafting_once()
        context = provider.intent_requests[-1].draft["current_context"]
        assert "Keep this previous-dialogue fact on demand." not in str(context)
        history, = context["prior_conversations"]
        assert history["generation"] == 1
        assert history["native_session_ref"] == "native-1"
        readback = runtime.root_workspaces.read_companion_session("workspace", original["session_ref"],
            workspace_ref=history["workspace_ref"], path=history["path"])
        conversation = json.loads(readback["content"])
        assert conversation["turns"] == original["turns"]
        assert conversation["native_session_ref"] == "native-1"
        assert conversation["session_ref"] == original["session_ref"]
        assert human.start_new_companion_session("workspace", "new")["native_session_generation"] == 2
    finally:
        runtime.close()
