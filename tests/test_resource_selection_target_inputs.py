"""Signed resource scope survives actual Target admission and workspace verification."""
import json
from pathlib import Path

from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from test_resource_content_selections import _parent, _selection, _environment, _quest
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime, _root_finalizer_fixture, _EvidenceReader


class _SelectionBundle(_CurrentBindingBundleSkill):
    input_refs = ()

    def _target_plan(self, request):
        plan = super()._target_plan(request)
        for spec in plan["initial_strategy_update"]["candidates"]:
            spec["candidate"]["direct_accepted_input_asset_refs"] = list(self.input_refs)
        return plan


def test_environment_children_reach_real_target_handle_and_only_selected_readonly_files(tmp_path, monkeypatch):
    skill = _SelectionBundle()
    runtime = _current_bundle_runtime(tmp_path / "target-resource", bundle_skill=skill)
    try:
        quest = _quest(runtime, "selection-input")
        directory, binding = _parent(runtime, tmp_path)
        graph = runtime.owners.research_graph
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest.quest_ref,
                                idempotency_key="resource-origin")
        environment = _environment(graph, quest_ref=quest.quest_ref,
            selections=[_selection(binding, "one"), _selection(binding, "two")])
        skill.input_refs = tuple(sorted(item["selection_ref"] for item in environment["selection_readers"]))

        # Reuse the already accepted Quest; all subsequent Bundle, input, launch
        # and execution binding writes cross the production Owners.
        import test_target_launch_admission
        with monkeypatch.context() as scoped:
            scoped.setattr(test_target_launch_admission, "_confirm_direct_quest", lambda _runtime: vars(quest))
            _, lifecycle, completion_memory, _contract, handle, workspace, evidence = _root_finalizer_fixture(
                tmp_path, runtime=runtime)
        authority = runtime.target_run_authorities.agent_runtime
        launch = graph.query_target_launch_request(handle.target_ref)
        assert launch.accepted_input_asset_refs == skill.input_refs
        assert len(handle.accepted_input_asset_proofs) == 2
        assert {proof.asset_ref for proof in handle.accepted_input_asset_proofs} == {binding.asset_ref}
        assert {proof.selection_path for proof in handle.accepted_input_asset_proofs} == {"one", "two"}
        assert tuple(proof.input_ref for proof in handle.accepted_input_asset_proofs) == skill.input_refs
        for proof in handle.accepted_input_asset_proofs:
            assert projection_plain_value(proof)["asset_version_ref"] == binding.version_ref
        assert runtime.target_run_authorities.research_graph.prepare_input_assets(handle.target_ref) is False
        paths = authority.materialize_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=())
        manifest_path = Path(paths[0])
        manifest = json.loads(manifest_path.read_text())
        inputs = [entry for entry in manifest["entries"] if entry["kind"] == "direct_asset"]
        assert len(inputs) == 2
        assert {entry["selection_path"] for entry in inputs} == {"one", "two"}
        assert {entry["version_ref"] for entry in inputs} == {binding.version_ref}
        assert {entry["selection_ref"] for entry in inputs} == set(skill.input_refs)
        for entry in inputs:
            selected = manifest_path.parent / entry["relative_path"]
            assert selected.is_dir()
            assert {path.name for path in selected.iterdir()} == {"source.py"}
            content = selected / "source.py"
            assert content.read_text() == ("first selected content" if entry["selection_path"] == "one"
                                            else "second selected content")
            assert content.stat().st_mode & 0o222 == 0
        # The source workspace can change without affecting either accepted view.
        (directory / "one/source.py").write_text("changed live source")
        authority.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=())
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT count(*) FROM rm_target_input_asset_proofs WHERE target_ref=:ref"),
                                      {"ref": handle.target_ref}).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM rg_target_input_asset_role_proofs WHERE target_ref=:ref"),
                                      {"ref": handle.target_ref}).scalar_one() == 1
        result_path = workspace / "outputs/result.json"
        result = json.loads(result_path.read_text())
        result["formal_runs"][0]["input_refs"] = list(skill.input_refs)
        result_path.write_text(json.dumps(result))
        from meta_research.target_run_finalizer import TargetRunFinalizer
        completed = TargetRunFinalizer(lifecycle=lifecycle, memory=completion_memory,
            workspace_resolver=authority, evidence_reader=_EvidenceReader(evidence),
            measurement_authority=graph, graph_authority=graph).finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed", completed
        facts = graph.query_target_formal_results(handle.target_ref)
        assert set(facts[0]["variant_run"]["inputs"]["input_refs"]) == set(skill.input_refs)
    finally:
        runtime.close()
