"""Creation Companion rotation uses its actual intent Session and workspace."""

import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.owners.common import OwnerConflict
from meta_research.runtime_protection import RuntimeProtectionUnavailable

from test_companion_native_rotation import RotationProvider
from test_public_human_collaboration_ladder import _CompanionRuntimeBinding, _fake_codex, _runtime, _runtime_with_companion_adapter
from test_public_human_collaboration_web import _authenticated_client


def _send(human, view, message, key):
    return human.send_intent_message(view["initialization_id"],
        expected_draft_revision=view["quest_draft"]["revision"],
        expected_draft_hash=view["quest_draft"]["hash"], message=message, idempotency_key=key)


def test_creation_new_rotates_the_actual_intent_consumer_and_preserves_its_workspace(tmp_path):
    provider = RotationProvider()
    runtime = _runtime(tmp_path / "runtime", provider)
    client, headers = _authenticated_client(runtime)
    try:
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "creation-open")
        initialization_id = opened["initialization_id"]
        basis = opened["quest_draft"]
        root_ref = opened["intent_session"]["ref"]
        binding = runtime.root_workspaces.bind_initialization(initialization_id, root_ref)
        (binding.directory / "retained-intent.txt").write_text("Creation workspace material.", encoding="utf-8")
        messages_url = f"/api/v1/quest-initializations/{initialization_id}/intent-session/messages"
        new_url = f"/api/v1/quest-initializations/{initialization_id}/intent-session/new"
        payload = {"expected_draft_revision": basis["revision"], "expected_draft_hash": basis["hash"],
            "message": "Previous creation conversation fact."}
        assert client.post(messages_url, headers={**headers, "Idempotency-Key": "creation-old"}, json=payload).status_code == 202
        assert human.process_drafting_once()
        original = human.query_quest_creation(initialization_id)
        switched_response = client.post(new_url, headers={**headers, "Idempotency-Key": "creation-new"}, json={})
        assert switched_response.status_code == 200, switched_response.text
        switched = switched_response.json()
        assert switched["intent_session"]["native_session_generation"] == 2
        assert switched["intent_session"]["native_session_ref"] is None
        assert switched["intent_session"]["ref"] == root_ref
        assert switched["quest_draft"] == original["quest_draft"]
        assert switched["intent_session"]["workspace_ref"] == binding.location.workspace_ref
        assert switched["intent_session"]["workspace_path"] == str(binding.directory)
        payload["message"] = "Use the prior workspace material if needed."
        assert client.post(messages_url, headers={**headers, "Idempotency-Key": "creation-fresh"}, json=payload).status_code == 202
        assert human.process_drafting_once()
        current = human.query_quest_creation(initialization_id)
        assert current["intent_session"]["native_session_ref"] == "native-2"
        assert [(turn["native_session_generation"], turn["native_session_ref"])
            for turn in current["intent_session"]["turns"]] == [(1, "native-1"), (2, "native-2")]
        fresh = provider.intent_requests[-1]
        assert fresh.native_session_ref is None
        assert fresh.root_session_ref == root_ref
        assert "Previous creation conversation fact." not in str(fresh.draft)
        history, = fresh.draft["companion_context"]["prior_conversations"]
        read = runtime.root_workspaces.read_initialization(initialization_id, root_ref,
            workspace_ref=history["workspace_ref"], path=history["path"])
        assert json.loads(read["content"])["turns"] == original["intent_session"]["turns"]
        assert runtime.root_workspaces.read_initialization(initialization_id, root_ref,
            workspace_ref=binding.location.workspace_ref, path="retained-intent.txt")["content"] == b"Creation workspace material."
        assert human.query_companion("quest-initialization:" + initialization_id)["session_ref"] is None
        replay = client.post(new_url, headers={**headers, "Idempotency-Key": "creation-new"}, json={})
        assert replay.status_code == 200
        assert replay.json()["intent_session"]["native_session_generation"] == 2
    finally:
        client.close()
        runtime.close()


def test_actual_creation_queue_serializes_native_and_switch_replays_while_busy(tmp_path):
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
        opened = human.create_quest({}, "actual-queue-open")
        initialization_id = opened["initialization_id"]
        _send(human, opened, "First actual creation turn.", "actual-queue-first")
        _send(human, opened, "Queued actual creation turn.", "actual-queue-second")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(human.process_drafting_once)
            assert entered.wait(10)
            try:
                session = human.query_quest_creation(initialization_id)["intent_session"]
                assert session["can_start_new_session"] is False
                assert session["switch_block_reason"] == "companion_session_busy"
                with pytest.raises(OwnerConflict, match="companion_session_busy"):
                    human.start_new_intent_session(initialization_id, "actual-queue-switch")
                assert human.process_drafting_once() is False
            finally:
                release.set()
            assert pending.result(timeout=15)
        assert human.process_drafting_once()
        completed = human.query_quest_creation(initialization_id)
        assert [turn["native_session_ref"] for turn in completed["intent_session"]["turns"]] == ["native-1", "native-1"]
        assert provider.intent_requests[1].native_session_ref == "native-1"
        human.start_new_intent_session(initialization_id, "actual-queue-switch")
        _send(human, opened, "New generation queued.", "actual-queue-third")
        assert human.start_new_intent_session(initialization_id, "actual-queue-switch")["intent_session"]["native_session_generation"] == 2
        with pytest.raises(OwnerConflict, match="companion_session_busy"):
            human.start_new_intent_session(initialization_id, "actual-queue-another")
    finally:
        release.set()
        runtime.close()


def test_actual_creation_late_result_after_rebuild_cannot_replace_current_native(tmp_path):
    entered, release = Event(), Event()

    class BoundProvider(RotationProvider):
        def runtime_binding(self):
            return _CompanionRuntimeBinding()

        def reconcile_job(self, job_ref):
            return "terminal"

    class DelayedProvider(BoundProvider):
        def reply(self, request):
            entered.set()
            assert release.wait(40)
            return super().reply(request)

    original = _runtime(tmp_path / "runtime", DelayedProvider())
    recovered = None
    try:
        first_human = original.owners.human_collaboration
        opened = first_human.create_quest({}, "actual-late-open")
        initialization_id = opened["initialization_id"]
        _send(first_human, opened, "Late old creation turn.", "actual-late-old")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(first_human.process_drafting_once)
            assert entered.wait(10)
            try:
                recovered = _runtime(tmp_path / "runtime", BoundProvider())
                human = recovered.owners.human_collaboration
                assert human.process_drafting_once()
                human.start_new_intent_session(initialization_id, "actual-late-switch")
                _send(human, opened, "Fresh creation turn.", "actual-late-fresh")
                assert human.process_drafting_once()
                before = human.query_quest_creation(initialization_id)
                assert before["intent_session"]["native_session_ref"] == "native-2"
                assert before["intent_session"]["native_session_generation"] == 2
            finally:
                release.set()
            with pytest.raises(RuntimeProtectionUnavailable, match="runtime_incarnation_stale"):
                pending.result(timeout=15)
        assert first_human.query_quest_creation(initialization_id) == before
        assert human.query_quest_creation(initialization_id) == before
        recovered.close()
        recovered = _runtime(tmp_path / "runtime", BoundProvider())
        restored = recovered.owners.human_collaboration.query_quest_creation(initialization_id)
        assert restored["intent_session"] == before["intent_session"]
    finally:
        release.set()
        if recovered is not None:
            recovered.close()
        original.close()


def test_actual_codex_creation_prompt_has_only_prior_history_paths_after_new(tmp_path):
    calls = []

    class CreationRunner:
        def run_job(self, job_ref, argv, input_text, timeout, environment=None):
            native = argv[argv.index("resume") + 1] if "resume" in argv else f"codex-creation-{len(calls) + 1}"
            calls.append((job_ref, argv, input_text))
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({"reply": "Creation reply."}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0,
                stdout=json.dumps({"type": "thread.started", "thread_id": native}), stderr="")

    adapter = CodexCompanionAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=CreationRunner())
    runtime = _runtime_with_companion_adapter(tmp_path / "runtime", adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:18769")
    try:
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "actual-codex-open")
        opened = human.deliver_initialization_materials(opened["initialization_id"], {
            "expected_draft_revision": opened["quest_draft"]["revision"],
            "expected_draft_hash": opened["quest_draft"]["hash"], "submission_ref": "actual-codex-material",
            "complete": True, "folder": False, "locator": None,
            "files": [{"relative_path": "original.txt", "content_base64": base64.b64encode(b"Preserve the actual creation material.").decode()}],
        }, "actual-codex-material")
        _send(human, opened, "Old conversation omitted from fresh prompt.", "actual-codex-old")
        assert human.process_drafting_once()
        first_turn = human.query_quest_creation(opened["initialization_id"])["intent_session"]["turns"][-1]
        assert first_turn["assistant_status"] == "completed", (first_turn["reason"], len(calls))
        human.start_new_intent_session(opened["initialization_id"], "actual-codex-switch")
        _send(human, opened, "Continue from the stable workspace.", "actual-codex-fresh")
        assert human.process_drafting_once()
        view = human.query_quest_creation(opened["initialization_id"])
        assert view["intent_session"]["native_session_ref"] == "codex-creation-2"
        assert len(calls) == 2
        _job, argv, prompt = calls[-1]
        assert "resume" not in argv
        assert "Old conversation omitted from fresh prompt." not in prompt
        assert "companion_context.prior_conversations" in prompt
        draft_line = next(line for line in prompt.splitlines() if line.startswith("current_draft="))
        draft = json.loads(draft_line.removeprefix("current_draft="))
        descriptor, = draft["companion_context"]["prior_conversations"]
        assert descriptor["path"] == "companion-history/native-session-1.json"
        assert descriptor["workspace_ref"] == view["intent_session"]["workspace_ref"]
        assert draft["companion_context"]["native_session_generation"] == 2
        assert view["quest_draft"] == opened["quest_draft"]
        assert {key: value for key, value in draft.items() if key != "companion_context"} == opened["quest_draft"]["value"]
        assert f"current_draft_hash={opened['quest_draft']['hash']}" in prompt
        material, = draft["material_manifest"]["entries"]
        assert material["workspace_ref"] == descriptor["workspace_ref"]
        read = runtime.root_workspaces.read_initialization(opened["initialization_id"], view["intent_session"]["ref"],
            workspace_ref=material["workspace_ref"], path=material["path"])
        assert read["content"] == b"Preserve the actual creation material."
    finally:
        runtime.close()
