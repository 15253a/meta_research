from dataclasses import replace
import json

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.provider_supervisor import read_supervisor_request, read_transport_key_for_operation
from meta_research.quest_drafting import DraftingUnavailable
from meta_research.semantic_mcp import SemanticMcpError
from test_public_manual_question_lifecycle import (
    DeterministicDraftingAdapter,
    DeterministicProbe,
    QUESTION,
    _accept_root_question,
    _open_and_confirm_seed,
)
from test_root_workspace_native import _native_executable


class RecordingCompanion(CodexCompanionAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.proposal_requests = []

    def reply(self, request):
        self.requests.append(request)
        return super().reply(request)

    def draft(self, request):
        self.proposal_requests.append(request)
        return super().draft(request)


def test_real_manual_creation_dispatch_preserves_its_actual_work_across_restart(tmp_path):
    provider_root = tmp_path / "manual-provider"
    executable = _native_executable(tmp_path / "local-codex")
    adapter = RecordingCompanion(provider_root, executable=str(executable))
    data_root = prepare_data_root(tmp_path / "runtime")
    runtime = build_production_runtime(data_root,
        proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe())
    try:
        quest_ref, parent_ref = _accept_root_question(runtime, "native-manual")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(human, quest_ref=quest_ref,
            parent_question_ref=parent_ref, key_prefix="native-manual")
        context_ref = seeded["context_ref"]
        root_ref = seeded["drafting_session"]["ref"]
        generation = seeded["generation"]
        human.send_manual_drafting_message(context_ref,
            expected_basis_hash=seeded["seed"]["hash"], message="Review pending observations.",
            idempotency_key="native-manual-first")
        assert human.process_drafting_once()
        first = human.query_manual_question_creation(context_ref)
        turn = first["drafting_session"]["turns"][-1]
        assert turn["assistant_status"] == "completed", turn["reason"]
        destination = runtime.root_workspaces.destination_for_manual_creation(context_ref,
            root_ref, generation)
        location = destination.location
        initialization = human.query_quest_creation(seeded["quest_initialization_id"])
        original = runtime.root_workspaces.destination_for_initialization(
            initialization["initialization_id"], initialization["intent_session"]["ref"])
        assert location.work_ref == context_ref
        assert location.root_session_ref == root_ref
        assert (location.quest_ref, location.cycle_ref) == (quest_ref, None)
        assert location.directory != original.location.directory
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="manual-original-answer",
            files=(("human.txt", b"Manual context pending observation: settle for 23 minutes."),))
        assert runtime.root_workspaces.deliver(destination, delivery_ref="manual-original-answer",
            files=(("human.txt", b"Manual context pending observation: settle for 23 minutes."),)) == delivered
        assert not (original.location.directory / "inbox").exists()
        human.send_manual_drafting_message(context_ref,
            expected_basis_hash=seeded["seed"]["hash"], message="Read the material in the original inbox.",
            idempotency_key="native-manual-second")
        assert human.process_drafting_once()
        assert human.query_manual_question_creation(context_ref)["drafting_session"]["turns"][-1]["assistant_status"] == "completed"
        assert adapter.requests[-1].native_session_ref == "local-native-root"
        replay_request = adapter.requests[-1]
        runtime.close()
        adapter = RecordingCompanion(provider_root, executable=str(executable))
        runtime = build_production_runtime(data_root,
            proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
            host_compute_probe=DeterministicProbe())
        human = runtime.owners.human_collaboration
        restarted = human.query_manual_question_creation(context_ref)
        assert restarted["drafting_session"]["ref"] == root_ref
        assert runtime.root_workspaces.destination_for_manual_creation(context_ref, root_ref,
            generation) == destination
        assert adapter.reply(replay_request).reply == "Checked pending observations."
        assert (location.directory / "native-marker.txt").read_text().splitlines() == [str(location.directory)] * 2
        human.send_manual_drafting_message(context_ref,
            expected_basis_hash=seeded["seed"]["hash"], message="Read the prior turn and original material.",
            idempotency_key="native-manual-third")
        assert human.process_drafting_once()
        assert human.query_manual_question_creation(context_ref)["drafting_session"]["turns"][-1]["assistant_status"] == "completed"
        assert adapter.requests[-1].native_session_ref == "local-native-root"
        assert (location.directory / "native-marker.txt").read_text().splitlines() == [str(location.directory)] * 3
        observed = runtime.root_workspaces.read_manual_creation(context_ref, root_ref, generation,
            workspace_ref=location.workspace_ref, path="native-observed-human.txt")
        assert observed["content"] == b"Manual context pending observation: settle for 23 minutes."
        assert observed["work_ref"] == context_ref
        assert observed["context_generation"] == generation
        page = runtime.root_workspaces.discover_manual_creation(context_ref, root_ref, generation)
        assert any(item["path"] == delivered["files"][0]["path"] for item in page["files"])
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
        requests = list((provider_root / "provider-operations").glob("*/*/supervisor-request.json"))
        assert len(requests) == 3
        for request in requests:
            _key_path, key = read_transport_key_for_operation(request.parent)
            assert read_supervisor_request(request, key)["working_directory"] == str(location.directory)
            binding = json.loads((request.parent / "invocation.json").read_text())["payload"]["workspace_binding"]
            assert binding["context_generation"] == generation
            assert binding["work_ref"] == context_ref
            assert binding["root_session_ref"] == root_ref
    finally:
        runtime.close()


def test_manual_creation_rejects_foreign_execution_and_preserves_closed_original_materials(tmp_path):
    adapter = RecordingCompanion(tmp_path / "manual-provider",
        executable=str(_native_executable(tmp_path / "local-codex")))
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe())
    try:
        quest_ref, parent_ref = _accept_root_question(runtime, "manual-boundary")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(human, quest_ref=quest_ref,
            parent_question_ref=parent_ref, key_prefix="manual-boundary")
        context_ref, root_ref, generation = (seeded["context_ref"],
            seeded["drafting_session"]["ref"], seeded["generation"])
        destination = runtime.root_workspaces.destination_for_manual_creation(context_ref, root_ref, generation)
        initialization = human.query_quest_creation(seeded["quest_initialization_id"])
        for wrong_root, wrong_generation in ((initialization["intent_session"]["ref"], generation),
                                              (root_ref, generation + 1), (root_ref, True)):
            with pytest.raises(SemanticMcpError, match="workspace_manual_creation_scope_invalid"):
                runtime.root_workspaces.bind_manual_creation(context_ref, wrong_root, wrong_generation)
        with pytest.raises(SemanticMcpError, match="workspace_initialization_scope_invalid"):
            runtime.root_workspaces.bind_initialization(initialization["initialization_id"], root_ref)
        human.send_manual_drafting_message(context_ref,
            expected_basis_hash=seeded["seed"]["hash"], message="Positive control uses the actual manual Session.",
            idempotency_key="manual-positive-control")
        assert human.process_drafting_once()
        assert human.query_manual_question_creation(context_ref)["drafting_session"]["turns"][-1]["assistant_status"] == "completed"
        request = adapter.requests[-1]
        for invalid in (replace(request, root_session_ref=initialization["intent_session"]["ref"]),
                        replace(request, context_generation=generation + 1),
                        replace(request, initialization_id="foreign-initialization")):
            with pytest.raises(DraftingUnavailable, match="workspace_manual_creation_scope_invalid"):
                adapter.reply(invalid)
        assert (destination.location.directory / "native-marker.txt").read_text().splitlines() == [str(destination.location.directory)]
        human.cancel_manual_question_creation(context_ref, "manual-close-boundary")
        replacement = _open_and_confirm_seed(human, quest_ref=quest_ref,
            parent_question_ref=parent_ref, key_prefix="manual-new-generation")
        successor = runtime.root_workspaces.destination_for_manual_creation(replacement["context_ref"],
            replacement["drafting_session"]["ref"], replacement["generation"])
        assert successor.location.directory != destination.location.directory
        with pytest.raises(DraftingUnavailable, match="workspace_manual_creation_scope_invalid"):
            adapter.reply(request)
        historical = runtime.root_workspaces.read_manual_creation(context_ref, root_ref, generation,
            workspace_ref=destination.location.workspace_ref, path="native-marker.txt")
        assert historical["content"] == (str(destination.location.directory) + "\n").encode()
        assert historical["work_ref"] == context_ref
        page = runtime.root_workspaces.discover_manual_creation(context_ref, root_ref, generation)
        assert any(item["path"] == "native-marker.txt" for item in page["files"])
        late = runtime.root_workspaces.deliver(destination, delivery_ref="late-manual-answer",
            files=(("human.txt", b"Keep this late answer in the original manual work."),))
        assert late["work_ref"] == context_ref
        assert runtime.root_workspaces.read_manual_creation(context_ref, root_ref, generation,
            workspace_ref=destination.location.workspace_ref, path=late["files"][0]["path"])["content"] == b"Keep this late answer in the original manual work."
        assert not (successor.location.directory / "inbox").exists()
    finally:
        runtime.close()


def test_real_proposal_dispatch_keeps_quest_initialization_work_distinct(tmp_path):
    executable = _native_executable(tmp_path / "local-codex")
    executable.write_text(executable.read_text().replace(
        "if '--output-last-message' in sys.argv:\n",
        "if 'BEGIN_PROPOSAL_DRAFTER_MESSAGE' in prompt:\n"
        "    value = {'proposal_fork_native_session_ref': 'local-proposal-child', 'content': "
        + repr(QUESTION) + "}\n"
        "if '--output-last-message' in sys.argv:\n"))
    adapter = RecordingCompanion(tmp_path / "companion-provider", executable=str(executable))
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=adapter, intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe())
    try:
        quest_ref, _parent_ref = _accept_root_question(runtime, "native-proposal")
        proposal_request, = adapter.proposal_requests
        creation = runtime.owners.human_collaboration.query_quest_creation(proposal_request.initialization_id)
        assert creation["quest_ref"] == quest_ref
        binding = runtime.root_workspaces.bind_initialization(creation["initialization_id"],
            creation["intent_session"]["ref"])
        assert (binding.directory / "native-marker.txt").read_text() == str(binding.directory) + "\n"
        assert binding.location.work_ref == creation["intent_session"]["ref"]
        assert (binding.location.quest_ref, binding.location.cycle_ref) == (None, None)
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
    finally:
        runtime.close()
