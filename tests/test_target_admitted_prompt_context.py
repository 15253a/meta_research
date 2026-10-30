"""Real Owner admission and driver prompt, before any external Target execution."""
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import text

from meta_research.bundle_contract import BundleContractError, validate_target_plan
from meta_research.owners.agent_runtime import AdmittedTargetLaunch
from meta_research.owners.common import OwnerConflict
from meta_research.target_run_runtime import TargetRunRuntime
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _CurrentBindingBundleSkill, _current_bundle_runtime


class _NotesFreeBundleSkill(_CurrentBindingBundleSkill):
    def __init__(self, notes_form):
        super().__init__()
        self.notes_form = notes_form

    def _target_plan(self, request):
        plan = super()._target_plan(request)
        plan.pop("notes", None)
        plan["initial_strategy_update"].pop("notes", None)
        if self.notes_form == "empty":
            plan["initial_strategy_update"]["notes"] = ""
        return plan


@pytest.mark.parametrize("notes_form", ["omitted", "empty"])
def test_notes_free_real_admitted_launch_reaches_bound_target_root_prompt(
    tmp_path, monkeypatch, notes_form,
):
    runtime = _current_bundle_runtime(
        tmp_path / "actual-target-prompt", bundle_skill=_NotesFreeBundleSkill(notes_form))
    try:
        graph, target, bundle_run, dispatch, launch_request = _ready_launch(runtime)
        assert "notes" not in graph.target_plan
        assert graph.target_plan["initial_strategy_update"].get("notes", "") == ""
        ack = runtime.owners.agent_runtime.admit_target_launch(
            launch_request, dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="notes-free-launch")
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        assert type(launch) is AdmittedTargetLaunch
        assert launch.ack == ack
        assert launch.request == launch_request
        assert launch.stage_request_ref == graph.request_ref == bundle_run.request_ref
        assert launch.graph_ref == graph.graph_ref

        captured = []

        class PromptCaptured(BaseException):
            pass

        def stop_before_external_target_execution(request_ref, **arguments):
            captured.append((request_ref, arguments))
            raise PromptCaptured()

        # All admission, input binding, workspace, lifecycle and prompt assembly
        # remain production paths. Stop at the external execution boundary;
        # never fabricate a successful provider result or completion receipt.
        monkeypatch.setattr(runtime.harnesses, "run_or_resume_target_root",
            stop_before_external_target_execution)
        runtime.target_run_runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        for _ in range(16):
            try:
                runtime.target_run_runtime.process_once(target.target_ref)
            except PromptCaptured:
                break
        assert len(captured) == 1, runtime.target_run_runtime.query_status(target.target_ref)

        prompt = captured[0][1]["prompt"]
        material = json.loads(prompt.split("Exact Owner context:\n", 1)[1])
        research = material["research_context"]
        position = json.loads(prompt.splitlines()[0].removeprefix("cycle_position="))
        handle = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(target.target_ref)
        assert handle is not None
        with runtime._database.read() as connection:
            persisted = connection.execute(text(
                "SELECT a.stage_request_ref,a.graph_ref,a.target_run_ref,r.cycle_ref "
                "FROM ar_target_launches a JOIN ae_stage_run_requests r "
                "ON r.request_ref=a.stage_request_ref WHERE a.target_ref=:target"),
                {"target": target.target_ref}).one()
        assert (launch.stage_request_ref, launch.graph_ref, launch.target_run_ref) == (
            persisted.stage_request_ref, persisted.graph_ref, persisted.target_run_ref)
        assert position["cycle_ref"] == persisted.cycle_ref == graph.cycle_ref
        assert position["stage"] == "target"
        assert research["stage_request_ref"] == launch.stage_request_ref
        assert research["graph_ref"] == launch.graph_ref
        assert research["target_ref"] == handle.target_ref == launch.request.target_ref
        assert material["handle"]["target_run_ref"] == handle.target_run_ref == launch.target_run_ref
        assert material["target_spec_binding"] == {
            "subject_ref": launch.request.target_spec_binding.subject_ref,
            "content_hash_ref": launch.request.target_spec_binding.content_hash_ref}
        assert research["bundle_notes"] == ""
        assert research["bundle_notes_text_hash"] == hashlib.sha256(b"").hexdigest()
        assert research["bundle_notes_source_ref"] == graph.submission_ref
        assert research["bundle_notes_source_hash"] == graph.target_plan_hash
        assert material["execution_contract"]["measurement_contract"]

        original = runtime.target_run_authorities.research_graph.query_target_research_context(
            target_ref=target.target_ref)
        assert original["bundle_notes"] == research["bundle_notes"]
        assert original["cycle_position"] == {key: value for key, value in position.items() if key != "stage"}
        candidate = runtime.owners.research_graph.query_target_candidate_projection(
            target_ref=target.target_ref).candidate
        formal_plan = runtime.owners.research_graph.query_target_formal_plan_projection(
            graph_ref=graph.graph_ref).formal_plan
        for field in ("stage_request_ref", "graph_ref", "target_ref"):
            changed = {**original, field: "foreign-" + field}
            with pytest.raises(OwnerConflict, match="target_cycle_position_invalid"):
                TargetRunRuntime._root_prompt(
                    execution_contract=material["execution_contract"], handle=handle,
                    candidate=candidate, formal_plan=formal_plan, launch=launch,
                    frozen_input_manifest_path=material["frozen_input_manifest_path"],
                    research_context=changed)

        # Explicit null is not an accepted "no notes" representation. Exercise
        # the actual upstream document validator on an otherwise accepted plan.
        request = runtime.owners.advancement_engine.query_bundle_stage_request(graph.cycle_ref)
        for placement, code in (("top", "target_plan_notes_invalid"),
                                ("initial", "formal_strategy_update_notes_invalid")):
            invalid = deepcopy(graph.target_plan)
            if placement == "top":
                invalid["notes"] = None
            else:
                invalid["initial_strategy_update"]["notes"] = None
            with pytest.raises(BundleContractError, match=code):
                validate_target_plan(invalid, formal_plan_ref=graph.formal_plan_ref,
                    context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash,
                    plan_document=request.accepted_formal_plan.plan_document)
    finally:
        runtime.close()
