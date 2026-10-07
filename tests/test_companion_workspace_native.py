import pytest
from dataclasses import replace
import json
from unittest.mock import patch

from meta_research.acquisition import AcquisitionPreflightResult
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import DraftingUnavailable
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError
from test_manual_creation_workspace_native import RecordingCompanion
from test_public_acquisition_session import RecordingAcquisitionProvider
from test_public_manual_question_lifecycle import DeterministicDraftingAdapter, DeterministicProbe
from test_public_manual_question_lifecycle import _accept_root_question
from test_public_human_collaboration_web import _authenticated_client
from test_root_workspace_native import _native_executable
from test_root_workspace import _channel, _call, _accepted


def _conversation_executable(path):
    executable = _native_executable(path)
    executable.write_text(executable.read_text().replace("'human_request': None}",
        "'human_request': None, 'agent_proposal': None}").replace(
        "if '--output-last-message' in sys.argv:\n",
        "pending = Path('native-pending-note.txt')\n"
        "previous_note = pending.read_text() if pending.exists() else None\n"
        "if previous_note is None:\n"
        "    pending.write_text(os.urandom(16).hex())\n"
        "value['reply'] = 'Fresh pending note.' if previous_note is None else 'Prior note: ' + previous_note\n"
        "if '--output-last-message' in sys.argv:\n"))
    return executable


class WaitingPreflight(RecordingAcquisitionProvider):
    def preflight(self, request):
        self.preflights.append(request)
        return AcquisitionPreflightResult(status="waiting_user", browser_context_ref=None,
            reason_code="institutional_login_required", evidence={"authorized_resource": "waiting_user"})


@pytest.mark.parametrize("context_kind", ("workspace", "initialization", "human_request"))
def test_real_unscoped_companion_uses_its_durable_session_work(tmp_path, context_kind):
    executable = _conversation_executable(tmp_path / "local-codex")
    provider_root = tmp_path / "companion-provider"
    adapter = RecordingCompanion(provider_root, executable=str(executable))
    acquisition = WaitingPreflight()
    data_root = prepare_data_root(tmp_path / "runtime")
    runtime = build_production_runtime(data_root, proposal_drafter=DeterministicDraftingAdapter(),
        intent_drafting_provider=adapter, acquisition_provider=acquisition,
        host_compute_probe=DeterministicProbe())
    client, headers = _authenticated_client(runtime)
    try:
        human = runtime.owners.human_collaboration
        scope_ref = "workspace"
        acquisition_ref = None
        if context_kind != "workspace":
            creation = human.create_quest({}, "companion-first-form")
            scope_ref = "quest-initialization:" + creation["initialization_id"]
            if context_kind == "human_request":
                session = runtime.owners.agent_runtime.prepare_acquisition_session(
                    initialization_id=creation["initialization_id"],
                    draft_revision=creation["quest_draft"]["revision"],
                    config={"mode": "oa_then_institution", "library_entry_url": "https://library.example.edu/resources"},
                    provider=acquisition)
                assert session.quest_ref is None and session.status == "waiting_user"
                acquisition_ref = session.session_ref
                target = {"session_ref": acquisition_ref}
                request = runtime.owners.agent_runtime.open_human_request(
                    request_kind="offline_action", obligation="Check the current preflight observation.",
                    business_purpose="Clarify the actual pre-Quest Acquisition session.",
                    target_assertion=target, acceptance_conditions=("Provide the missing observation.",),
                    direct_waiter={"waiter_ref": acquisition_ref, "generation": 1,
                        "target_assertion": target, "wait_scope": "local", "other_blockers": []},
                    idempotency_key="companion-pre-quest-request")
                scope_ref = request["request_ref"]
        if context_kind == "human_request":
            queued = human.send_companion_message(scope_ref, "Explain the original pending observation.",
                "companion-first-message")
        else:
            response = client.post("/api/v1/companion/messages",
                headers={**headers, "Idempotency-Key": "companion-first-message"},
                json={"scope_ref": scope_ref, "message": "Explain the original pending observation."})
            assert response.status_code == 202, response.status_code
            queued = response.json()
        assert human.process_drafting_once()
        turn, = human.query_companion(scope_ref)["turns"]
        assert turn["interaction_ref"] == queued["interaction_ref"]
        assert turn["assistant_status"] == "completed", turn["reason"]
        root_ref = human.query_companion(scope_ref)["session_ref"]
        facts = human.query_companion_work_context(root_ref)
        assert facts["scope_ref"] == scope_ref
        assert facts["quest_ref"] is None
        binding = runtime.root_workspaces.bind_companion_session(scope_ref, root_ref)
        location = binding.location
        assert (location.root_session_ref, location.work_ref) == (root_ref, root_ref)
        assert location.cycle_ref is None
        assert root_ref != acquisition_ref
        assert adapter.requests[-1].root_runtime_scope is None
        assert adapter.requests[-1].creation_context_kind == "companion_conversation"
        assert adapter.requests[-1].root_session_ref == root_ref
        if context_kind == "initialization":
            intent = runtime.root_workspaces.bind_initialization(creation["initialization_id"],
                creation["intent_session"]["ref"])
            assert intent.directory != binding.directory
        pending = runtime.root_workspaces.read_companion_session(scope_ref, root_ref,
            workspace_ref=location.workspace_ref, path="native-pending-note.txt")
        nonce = pending["content"].decode()
        assert len(nonce) == 32
        human.send_companion_message(scope_ref, "Read the prior pending note.", "companion-second-message")
        assert human.process_drafting_once()
        assert human.query_companion(scope_ref)["turns"][-1]["assistant_content"] == "Prior note: " + nonce
        assert adapter.requests[-1].native_session_ref == "local-native-root"
        replay_request = adapter.requests[-1]
        client.close()
        runtime.close()
        adapter = RecordingCompanion(provider_root, executable=str(executable))
        runtime = build_production_runtime(data_root, proposal_drafter=DeterministicDraftingAdapter(),
            intent_drafting_provider=adapter, acquisition_provider=acquisition,
            host_compute_probe=DeterministicProbe())
        human = runtime.owners.human_collaboration
        assert human.query_companion(scope_ref)["session_ref"] == root_ref
        assert runtime.root_workspaces.bind_companion_session(scope_ref, root_ref) == binding
        assert adapter.reply(replay_request).reply == "Prior note: " + nonce
        assert (location.directory / "native-marker.txt").read_text().splitlines() == [str(location.directory)] * 2
        human.send_companion_message(scope_ref, "Read the original note after restart.", "companion-third-message")
        assert human.process_drafting_once()
        assert human.query_companion(scope_ref)["turns"][-1]["assistant_content"] == "Prior note: " + nonce
        assert (location.directory / "native-marker.txt").read_text().splitlines() == [str(location.directory)] * 3
        assert runtime.root_workspaces.read_companion_session(scope_ref, root_ref,
            workspace_ref=location.workspace_ref, path="native-pending-note.txt")["content"] == nonce.encode()
        assert any(item["path"] == "native-pending-note.txt" for item in
            runtime.root_workspaces.discover_companion_session(scope_ref, root_ref)["files"])
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
        foreign_scope = "workspace"
        if context_kind == "workspace":
            foreign_creation = human.create_quest({}, "foreign-companion-form")
            foreign_scope = "quest-initialization:" + foreign_creation["initialization_id"]
        human.send_companion_message(foreign_scope, "Create a separate actual HC Session.", "foreign-companion-message")
        foreign_ref = human.query_companion(foreign_scope)["session_ref"]
        foreign = runtime.root_workspaces.bind_companion_session(foreign_scope, foreign_ref)
        with pytest.raises(SemanticMcpError, match="workspace_companion_scope_invalid"):
            runtime.root_workspaces.bind_companion_session(scope_ref, foreign_ref)
        with pytest.raises(DraftingUnavailable, match="workspace_companion_scope_invalid"):
            adapter.reply(replace(replay_request, root_session_ref=foreign_ref))
        with pytest.raises(SemanticMcpError, match="workspace_not_visible"):
            runtime.root_workspaces.read_companion_session(scope_ref, root_ref,
                workspace_ref=foreign.location.workspace_ref, path="native-pending-note.txt")
        assert not (foreign.directory / "native-marker.txt").exists()
        if context_kind == "human_request":
            persisted = runtime.owners.agent_runtime.query_human_request(scope_ref)
            assert persisted["direct_waiters"] == request["direct_waiters"]
            assert persisted["quest_ref"] is None
    finally:
        client.close()
        runtime.close()


def test_real_quest_companion_turns_preserve_actual_native_session_work(tmp_path):
    executable = _conversation_executable(tmp_path / "local-codex")
    provider_root = tmp_path / "companion-provider"
    class Reader(RecordingCompanion):
        def reply(self, request):
            scope = request.root_runtime_scope
            context = SemanticCallContext(scope["run_ref"], scope["attempt_ref"], scope["root_session_ref"],
                scope["fence_ref"], scope["runtime_binding_hash"], "companion", "companion-turn", "research_workspace.read")
            facts = self.runtime.owners.human_collaboration.query_companion_work_context(scope["root_session_ref"])
            with patch.object(self.runtime.owners.human_collaboration, "query_companion_work_context",
                    return_value={**facts, "quest_ref": self.foreign_quest_ref}):
                with pytest.raises(SemanticMcpError, match="workspace_lineage_invalid"):
                    self.runtime.root_workspaces.bind_runtime(context)
            result = super().reply(request)
            binding = self.runtime.root_workspaces.bind_runtime(context)
            observed = _accepted(_call(self.runtime, _channel(self.runtime, context), "research_workspace.read",
                workspace_ref=binding.location.workspace_ref, path="native-pending-note.txt"))
            assert observed["work_ref"] == scope["root_session_ref"]
            assert observed["request_ref"] == scope["run_ref"]
            assert observed["quest_ref"] == scope["quest_ref"]
            if self.notes:
                assert result.reply == "Prior note: " + self.notes[0]
                assert observed["text"] == self.notes[0]
            self.notes.append(observed["text"])
            return result

    adapter = Reader(provider_root, executable=str(executable))
    adapter.notes = []
    data_root = prepare_data_root(tmp_path / "runtime")
    runtime = build_production_runtime(data_root,
        proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
        acquisition_provider=RecordingAcquisitionProvider(), host_compute_probe=DeterministicProbe())
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        quest_ref, _parent_ref = _accept_root_question(runtime, "companion-scoped")
        foreign_quest_ref, _foreign_parent_ref = _accept_root_question(runtime, "companion-foreign-quest")
        adapter.runtime = runtime
        adapter.foreign_quest_ref = foreign_quest_ref
        human = runtime.owners.human_collaboration
        scope_ref = "quest:" + quest_ref
        root_ref = human.query_companion(scope_ref)["session_ref"]
        for index in range(2):
            human.send_companion_message(scope_ref, "Read the prior pending observations.",
                "scoped-companion-message-" + str(index))
            assert human.process_drafting_once()
            assert human.query_companion(scope_ref)["turns"][-1]["assistant_status"] == "completed"
        assert adapter.requests[0].native_session_ref is None
        assert adapter.requests[1].native_session_ref == "local-native-root"
        assert {item.root_runtime_scope["root_session_ref"] for item in adapter.requests} == {root_ref}
        invocations = [json.loads(path.read_text())["payload"]
            for path in (provider_root / "provider-operations").glob("*/*/invocation.json")]
        directories = {item["working_directory"] for item in invocations}
        assert len(directories) == 1, sorted(directories)
        binding = runtime.root_workspaces.bind_companion_session(scope_ref, root_ref)
        assert directories == {str(binding.directory)}
        assert (binding.directory / "native-marker.txt").read_text().splitlines() == [str(binding.directory)] * 2
        for item in invocations:
            assert item["workspace_binding"]["work_ref"] == root_ref
        notes = adapter.notes
        runtime.close()
        adapter = Reader(provider_root, executable=str(executable))
        adapter.notes, adapter.foreign_quest_ref = notes, foreign_quest_ref
        runtime = build_production_runtime(data_root,
            proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
            acquisition_provider=RecordingAcquisitionProvider(), host_compute_probe=DeterministicProbe())
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        adapter.runtime = runtime
        human = runtime.owners.human_collaboration
        assert human.query_companion(scope_ref)["session_ref"] == root_ref
        assert runtime.root_workspaces.bind_companion_session(scope_ref, root_ref) == binding
        human.send_companion_message(scope_ref, "Read the original pending note after restart.",
            "scoped-companion-message-2")
        assert human.process_drafting_once()
        assert human.query_companion(scope_ref)["turns"][-1]["assistant_status"] == "completed"
        assert adapter.requests[-1].native_session_ref == "local-native-root"
        assert (binding.directory / "native-marker.txt").read_text().splitlines() == [str(binding.directory)] * 3
        foreign_scope = "quest:" + foreign_quest_ref
        foreign_ref = human.query_companion(foreign_scope)["session_ref"]
        with pytest.raises(SemanticMcpError, match="workspace_companion_scope_invalid"):
            runtime.root_workspaces.bind_companion_session(scope_ref, foreign_ref)
    finally:
        runtime.close()
