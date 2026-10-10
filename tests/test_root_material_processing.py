"""Actual receiving roots process materials before choosing research custody."""
from fastapi.testclient import TestClient
import pytest

from meta_research.web import create_app
from test_public_human_reply_delivery import _login, _open
from test_root_workspace import _make, _channel, _call, _accepted
from test_public_plan_stage import _confirm_direct_quest, _DeterministicIdeaSkill
from test_root_workspace import _context


def _reply(client, runtime, source, opened):
    headers = _login(client, runtime)
    selection = client.get("/api/v1/server-materials/inspect", params={"path": str(source)}).json()
    response = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses",
        headers=headers, json={"decision": "provided", "note": "A human observation, not a measured result.",
            "facts": {}, "materials": [{"kind": "server_reference", "selection": selection}]})
    assert response.status_code == 201, response.text
    return response.json()["delivery"]["work_materials"][0]["reference_ref"]


def _feedback(reference, **overrides):
    return {"effect_id": "process-observation", "reference_ref": reference,
        "understanding": "This is a reported observation of 17 degrees.", "disposition": "considered",
        "changes": "Check thermometer calibration before comparing results.",
        "continuing_work": "Continue the independently authorized literature comparison.",
        "reasons": "The observation suggests a condition worth checking.",
        "limitations": "It does not establish an experimental result or a completed goal.",
        "selections": [], **overrides}


def test_original_request_root_records_actual_read_and_declared_influence_without_intake(tmp_path):
    source = tmp_path / "reported.txt"
    source.write_text("Human reports 17 degrees. Calibration is unknown.\n")
    runtime = _make(tmp_path / "runtime")
    try:
        _, context, opened = _open(runtime)
        channel = _channel(runtime, context)
        before = runtime.owners.research_memory.query_snapshot()
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            reference = _reply(client, runtime, source, opened)
            received = client.get(f"/api/v1/work-materials/{reference}").json()
            assert received["read_state"] == "not_read"
            rejected = _call(runtime, channel, "research_workspace.materials.feedback",
                **_feedback(reference, disposition="adopted"))
            assert rejected.get("isError") and "material_root_read_required" in str(rejected)
            page = _accepted(_call(runtime, channel, "research_workspace.materials.discover", reference_ref=reference))
            entry = page["entries"][0]
            read = _accepted(_call(runtime, channel, "research_workspace.materials.read", reference_ref=reference,
                path="", observation_ref=entry["observation"]["observation_ref"]))
            assert read["text"] == "Human reports 17 degrees. Calibration is unknown.\n"
            treated = _accepted(_call(runtime, channel, "research_workspace.materials.feedback", **_feedback(reference)))
            assert _accepted(_call(runtime, channel, "research_workspace.materials.feedback", **_feedback(reference))) == treated
            assert _accepted(_call(runtime, channel, "research_workspace.materials.feedback.reconcile",
                effect_id="process-observation")) == treated
            public = client.get(f"/api/v1/work-materials/{reference}").json()
            assert public["read_ranges"][0]["actor"] == context.run_ref
            assert public["treatments"] == [treated]
            assert treated["processed_by"]["run_ref"] == "reply-work"
            assert treated["receiver"]["request_ref"] == opened["request_ref"]
            assert treated["declared_by_root"] is True
            assert treated["selections"] == []
            assert treated["changes"] == "Check thermometer calibration before comparing results."
        assert runtime.owners.research_memory.query_snapshot() == before
        assert source.read_text() == "Human reports 17 degrees. Calibration is unknown.\n"
    finally:
        runtime.close()


def test_linked_processing_result_survives_cycle_cleanup_and_reports_later_drift(tmp_path):
    from pathlib import Path
    import time
    from test_completed_workspace_cleanup import _WorkspaceIdea, _cleanup_runtime, _complete
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _external_context

    source = tmp_path / "human-observation.txt"
    source.write_text("Reported observation; no independent measurement.\n")

    class LinkedIdea(_WorkspaceIdea):
        def generate_draft(self, request):
            channel = _channel(self.runtime, _context(request, "idea"))
            acquired = _accepted(_call(self.runtime, channel, "research_workspace.materials.acquire",
                effect_id="acquire-linked", absolute_path=str(source), description="Retrieve the reported observation."))
            reference = acquired["references"][0]["reference_ref"]
            observation = _accepted(_call(self.runtime, channel, "research_workspace.materials.discover",
                reference_ref=reference))["entries"][0]["observation"]["observation_ref"]
            copied = _accepted(_call(self.runtime, channel, "research_workspace.materials.copy",
                effect_id="copy-linked", reference_ref=reference, path="", observation_ref=observation))
            self.result_path = Path(copied["working_path"])
            self.result_path.write_text("Retained analysis: calibration is still required.\n")
            entry = next(item for item in _accepted(_call(self.runtime, channel, "research_workspace.discover"))["files"]
                if item["path"] == copied["work_file"]["path"])
            selection = {"source": {"kind": "workspace_file", "workspace_ref": entry["workspace_ref"],
                "path": entry["path"], "expected_sha256": entry["sha256"]}, "custody": "linked_local",
                "purpose": "Keep the actual working analysis available to the next research work."}
            self.treatments = [_accepted(_call(self.runtime, channel, "research_workspace.materials.feedback",
                **_feedback(reference, effect_id=effect, selections=[selection])))
                for effect in ("first-use", "second-use")]
            return super().generate_draft(request)

    idea = LinkedIdea()
    seeded = _cleanup_runtime(tmp_path / "cleanup-root", idea=idea)
    runtime, _, _, _, _, _, _ = _complete(tmp_path, runtime=seeded)
    try:
        location = idea.locations[runtime.cleanup_test_cycle_ref]
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        protected = next(item for item in report if item["workspace_ref"] == location.workspace_ref)
        assert protected["action"] == "skipped" and protected["reason"] == "linked_local_original", report
        successor = _external_context(_scope(runtime, quest_ref=location.quest_ref,
            run_ref="post-cleanup-reader", root_ref="post-cleanup-session"))
        channel = _channel(runtime, successor)
        first = _accepted(_call(runtime, channel, "research_workspace.materials.discover", limit=1))
        assert first["references"] == [] and first["next_offset"] == 1
        second = _accepted(_call(runtime, channel, "research_workspace.materials.discover", limit=1, offset=1))
        assert second["references"] == [] and second["next_offset"] is None
        assert {item["feedback_ref"] for item in first["retained_treatments"] + second["retained_treatments"]} == {
            item["feedback_ref"] for item in idea.treatments}
        reader = first["retained_treatments"][0]["selections"][0]["reader"]
        assert _accepted(_call(runtime, channel, "research_memory.content.read", **reader))["text"] == (
            "Retained analysis: calibration is still required.\n")
        idea.result_path.write_text("Changed outside the retained version.\n")
        drifted = _call(runtime, channel, "research_memory.content.read", **reader)
        assert drifted.get("isError") and "content_drifted" in str(drifted)
        assert source.read_text() == "Reported observation; no independent measurement.\n"
    finally:
        runtime.close()


def test_target_finalizer_does_not_admit_unselected_work_material_copies_or_results(tmp_path):
    from pathlib import Path
    import hashlib
    from meta_research.semantic_mcp import SemanticCallContext
    from test_formal_run_snapshots import _scenario

    source = tmp_path / "external-report.txt"
    source.write_text("Unverified external report for current Target judgment.\n")
    runtime, _, completion_memory, handle, evidence, finalizer = _scenario(tmp_path)
    try:
        runtime.owners.agent_runtime.harness_runs.start_operation(run_ref=handle.target_run_ref,
            operation_ref="target-material-turn", generation=1, invocation_hash="a" * 64, resume=False)
        run = runtime.owners.agent_runtime.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref,
            run.capability_binding_hash, "target", "target_root", "research_workspace.materials.acquire")
        channel = _channel(runtime, context)
        before = runtime.owners.research_memory.query_snapshot()
        acquired = _accepted(_call(runtime, channel, "research_workspace.materials.acquire", effect_id="acquire-target",
            absolute_path=str(source), description="Retrieve an unverified external report for the active Target."))
        reference = acquired["references"][0]["reference_ref"]
        observation = _accepted(_call(runtime, channel, "research_workspace.materials.discover",
            reference_ref=reference))["entries"][0]["observation"]["observation_ref"]
        copied = _accepted(_call(runtime, channel, "research_workspace.materials.copy", effect_id="copy-target",
            reference_ref=reference, path="", observation_ref=observation))
        path = Path(copied["working_path"])
        result = path.with_suffix(".analysis.txt")
        result.write_text("Tentative analysis rejected from the formal result.\n")
        _accepted(_call(runtime, channel, "research_workspace.materials.feedback", **_feedback(reference)))
        assert runtime.owners.research_memory.query_snapshot() == before
        assert copied["work_file"]["path"].startswith(".work-materials/")
        frozen = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = completion_memory.query(frozen.manifest_ref)
        assert all(not item.declared_relative_path.startswith(".work-materials/") for item in manifest.entries)
        unwanted = {hashlib.sha256(item.read_bytes()).hexdigest() for item in (source, result)}
        assert unwanted.isdisjoint(item.content_hash for item in runtime.owners.research_memory.query_asset_inventory())
        assert source.read_text() == "Unverified external report for current Target judgment.\n"
    finally:
        runtime.close()


@pytest.mark.parametrize("source_kind", ["request", "guidance", "acquired"])
@pytest.mark.parametrize("choice", ["original", "result", "both", "neither"])
def test_receiving_research_root_chooses_custody_and_successor_reads_retained_use(tmp_path, source_kind, choice):
    source = tmp_path / "observation.txt"
    source.write_text("Reported 17 degrees; uncalibrated.\n")
    observed = {}
    class ProcessDuringIdea(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            context = _context(request, "idea")
            channel = _channel(self.runtime, context)
            with TestClient(create_app(self.runtime, base_url="http://testserver", control_key="control")) as client:
                headers = _login(client, self.runtime)
                selection = client.get("/api/v1/server-materials/inspect", params={"path": str(source)}).json()
                if source_kind == "request":
                    from test_root_human_request_lifecycle import _open_arguments
                    opened = _accepted(_call(self.runtime, channel, "human_request.open",
                        **_open_arguments("observe", "offline_action")))
                    response = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", headers=headers,
                        json={"decision": "provided", "note": "Reported observation.", "facts": {},
                            "materials": [{"kind": "server_reference", "selection": selection}]})
                    assert response.status_code == 201, response.text
                    reference = response.json()["delivery"]["work_materials"][0]["reference_ref"]
                elif source_kind == "guidance":
                    receiver = client.get("/api/v1/work-materials/receiver", params={"quest_ref": observed["quest"]}).json()
                    from guidance_confirmation_helpers import prepare_guidance_submission
                    body = prepare_guidance_submission(self.runtime.owners.human_collaboration,
                        observed["quest"], "Consider this current observation.", strength=2,
                        key="current-observation-guidance", work_materials={"receiver": receiver,
                            "selections": [selection], "description": "Human observation."})
                    response = client.post("/api/v1/human-collaboration/guidance", headers=headers,
                        json=body)
                    assert response.status_code == 201, response.text
                    reference = response.json()["work_materials"][0]["references"][0]["reference_ref"]
                else:
                    acquired = _accepted(_call(self.runtime, channel, "research_workspace.materials.acquire",
                        effect_id="acquire-observation", absolute_path=str(source), description="Agent retrieved a reported observation from the authorized host."))
                    reference = acquired["references"][0]["reference_ref"]
                    assert acquired["receiver"]["kind"] == "acquired"
                before = self.runtime.owners.research_memory.query_snapshot()
                page = _accepted(_call(self.runtime, channel, "research_workspace.materials.discover", reference_ref=reference))
                observation = page["entries"][0]["observation"]["observation_ref"]
                copied = _accepted(_call(self.runtime, channel, "research_workspace.materials.copy", effect_id="copy-note",
                    reference_ref=reference, path="", observation_ref=observation))
                from pathlib import Path
                result_path = Path(copied["working_path"])
                result_path.write_text("Analysis: calibrate first; no measured conclusion.\n")
                assert _accepted(_call(self.runtime, channel, "research_workspace.materials.copy", effect_id="copy-note",
                    reference_ref=reference, path="", observation_ref=observation)) == copied
                assert result_path.read_text() == "Analysis: calibrate first; no measured conclusion.\n"
                assert self.runtime.owners.research_memory.query_snapshot() == before
                selections = []
                if choice in {"original", "both"}:
                    selections.append({"source": {"kind": "original_file", "path": "", "observation_ref": observation},
                        "custody": "linked_local", "purpose": "Preserve the actual supplied observation and its uncertainty."})
                if choice in {"result", "both"}:
                    entry = next(item for item in _accepted(_call(self.runtime, channel, "research_workspace.discover"))["files"]
                        if item["path"] == copied["work_file"]["path"])
                    selections.append({"source": {"kind": "workspace_file", "workspace_ref": entry["workspace_ref"],
                        "path": entry["path"], "expected_sha256": entry["sha256"]},
                        "custody": "managed", "purpose": "Retain the calibration analysis, not a measured result."})
                treatment = _accepted(_call(self.runtime, channel, "research_workspace.materials.feedback",
                    **_feedback(reference, selections=selections)))
                assert len(treatment["selections"]) == len(selections)
                if not selections:
                    assert self.runtime.owners.research_memory.query_snapshot() == before
                public = client.get(f"/api/v1/work-materials/{reference}").json()
                assert public["treatments"][0] == treatment
                observed.update(treatment=treatment, reference=reference, context=context)
            return super().generate_draft(request)
    provider = ProcessDuringIdea()
    runtime = _make(tmp_path / "runtime", idea=provider)
    try:
        observed["quest"] = _confirm_direct_quest(runtime)["quest_ref"]
        for _ in range(8):
            runtime.idea_stage.process_once()
            if "treatment" in observed:
                break
        assert "treatment" in observed
        from test_dataset_effect_scope_recovery import _scope
        from test_root_workspace import _external_context
        successor = _external_context(_scope(runtime, quest_ref=observed["quest"], run_ref="later-root", root_ref="later-session"))
        channel = _channel(runtime, successor)
        discovered = _accepted(_call(runtime, channel, "research_workspace.materials.discover"))
        assert observed["reference"] not in [item["reference_ref"] for item in discovered["references"]]
        retained = [item for item in discovered["retained_treatments"] if item["reference_ref"] == observed["reference"]]
        assert len(retained) == (0 if choice == "neither" else 1)
        for selected in observed["treatment"]["selections"]:
            value = _accepted(_call(runtime, channel, "research_memory.content.read", **selected["reader"]))
            assert value["text"] == ("Reported 17 degrees; uncalibrated.\n" if selected["source"]["kind"] == "original_file"
                else "Analysis: calibrate first; no measured conclusion.\n")
            roles = runtime.owners.research_graph.query_asset_roles(quest_ref=observed["quest"])
            assert selected["role_ref"] in [item.role_ref for item in roles]
        assert source.read_text() == "Reported 17 degrees; uncalibrated.\n"
    finally:
        runtime.close()
