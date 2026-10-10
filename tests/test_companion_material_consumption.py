"""Independent Companion consumption through the existing scoped material tools."""
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.semantic_mcp import SemanticCallContext
from test_companion_workspace_native import _conversation_executable
from test_manual_creation_workspace_native import RecordingCompanion
from test_public_acquisition_session import RecordingAcquisitionProvider
from test_public_manual_question_lifecycle import DeterministicDraftingAdapter, DeterministicProbe, _accept_root_question
from test_public_human_collaboration_web import _authenticated_client
from test_public_work_materials import _source
from test_dataset_effect_scope_recovery import _scope
from test_root_workspace import _external_context, _channel, _call, _accepted
from test_root_human_request_lifecycle import _open_arguments


def test_quest_companion_reads_original_request_materials_without_rebinding_or_foreign_quest_access(tmp_path):
    class Reader(RecordingCompanion):
        def reply(self, request):
            scope = request.root_runtime_scope
            context = SemanticCallContext(scope["run_ref"], scope["attempt_ref"], scope["root_session_ref"],
                scope["fence_ref"], scope["runtime_binding_hash"], "companion", "companion-turn", "research_workspace.materials.read")
            channel = _channel(self.runtime, context)
            listed = _accepted(_call(self.runtime, channel, "research_workspace.materials.discover"))
            assert [item["reference_ref"] for item in listed["references"]] == [self.reference]
            page = _accepted(_call(self.runtime, channel, "research_workspace.materials.discover", reference_ref=self.reference))
            entry = page["entries"][0]
            read = _accepted(_call(self.runtime, channel, "research_workspace.materials.read",
                reference_ref=self.reference, path=entry["path"], observation_ref=entry["observation"]["observation_ref"]))
            assert read["text"] == "Original observation: 17 degrees."
            denied = _call(self.runtime, channel, "research_workspace.materials.discover", reference_ref=self.foreign_reference)
            assert denied["structuredContent"]["code"] == "material_not_visible"
            self.observed = read
            return super().reply(request)

    adapter = Reader(tmp_path / "provider", executable=str(_conversation_executable(tmp_path / "local-codex")))
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
        acquisition_provider=RecordingAcquisitionProvider(), host_compute_probe=DeterministicProbe())
    client, headers = _authenticated_client(runtime)
    try:
        quest, _ = _accept_root_question(runtime, "material-consumer")
        foreign_quest, _ = _accept_root_question(runtime, "material-foreign")
        references = []
        for index, selected_quest in enumerate((quest, foreign_quest)):
            scope = _scope(runtime, quest_ref=selected_quest, run_ref=f"original-work-{index}", root_ref=f"original-root-{index}")
            context = _external_context(scope)
            opened = _accepted(_call(runtime, _channel(runtime, context), "human_request.open",
                **_open_arguments(f"material-request-{index}", "offline_action")))
            source = tmp_path / f"original-{index}"
            source.mkdir()
            (source / "observation.txt").write_text("Original observation: 17 degrees.")
            response = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses",
                headers={**headers, "Idempotency-Key": f"original-material-{index}"},
                json={"decision": "provided", "facts": {}, "note": "Read this supplied observation.",
                    "materials": [{"kind": "server_reference", "selection": _source(client, source)}]})
            assert response.status_code == 201, response.text
            reference = response.json()["delivery"]["work_materials"][0]["reference_ref"]
            references.append((reference, opened["request_ref"], scope["run_ref"]))
        adapter.runtime, adapter.reference, adapter.foreign_reference = runtime, references[0][0], references[1][0]
        human = runtime.owners.human_collaboration
        before_rm = runtime.owners.research_memory.query_snapshot()
        human.send_companion_message("quest:" + quest, "Investigate the supplied original request observation.", "read-original-material")
        assert human.process_drafting_once()
        assert human.query_companion("quest:" + quest)["turns"][-1]["assistant_status"] == "completed"
        assert adapter.observed["reference_ref"] == references[0][0]
        saved = human.query_work_material(references[0][0])
        assert saved["receiver"]["request_ref"] == references[0][1]
        assert saved["receiver"]["roots"][0]["work_ref"] == references[0][2]
        assert (tmp_path / "original-0/observation.txt").read_text() == "Original observation: 17 degrees."
        assert runtime.owners.research_memory.query_snapshot() == before_rm
    finally:
        client.close()
        runtime.close()
