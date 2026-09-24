"""Public stage cancel/resume only restarts unaccepted Reasoning work."""
import hashlib
import json
import threading
import time

from sqlalchemy import text

from test_reasoning_operator_pause_resume import _SupervisedReasoning, _provider
from test_public_reasoning_stage import _reasoning_runtime, _confirm_deepfetch_quest, _MultiRunIdeaSkill, _MultiRunPlanSkill
from test_public_bundle_stage import _finish_plan_stage
from test_public_plan_stage import _finish_idea_stage
from test_target_root_finalizer import _CurrentBindingBundleSkill
from test_formal_run_snapshots import _scenario
from test_two_cycle_actual_work import _ready_existing, _finish_stage
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_public_advancement_runtime_control import _confirmed_control, _execute_control
from meta_research.target_run_finalizer import TargetRunFinalizer


def _stage_control(runtime, quest_ref, action):
    foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
    command = _confirmed_control(runtime.owners.human_collaboration,
        scope_ref=f"quest:{quest_ref}", payload={"action":action, "target":{
            "target_scope":"stage", "quest_ref":quest_ref,
            "cycle_ref":foreground["cycle_ref"], "question_ref":foreground["question_ref"],
            "epoch":foreground["epoch"]}, "reason":"operator_requested"}, key=f"stage-{action}")
    result = _execute_control(runtime.owners.human_collaboration, command, f"stage-{action}")
    assert result["executed"] is True
    return result


def test_stage_cancel_resume_keeps_completed_target_and_five_originals(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    executable = _provider(tmp_path / "codex", root, stopped_zero=True)
    provider = _SupervisedReasoning(root, executable)
    runtime = _reasoning_runtime(root, reasoning_skill=provider,
        idea_skill=_MultiRunIdeaSkill(), plan_skill=_MultiRunPlanSkill(no_gap=False),
        bundle_skill=_CurrentBindingBundleSkill())
    provider.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    worker = None
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        ready = _ready_existing(runtime)
        _, lifecycle, memory, handle, evidence, _ = _scenario(tmp_path, runtime=runtime, ready=ready)
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        manifest = memory.query(completed.manifest_ref)
        originals = {}
        for entry in manifest.entries:
            if entry.artifact_kind != "file": continue
            originals[entry.binding.version_ref] = runtime.owners.research_memory.materialize_asset(entry.binding.version_ref).content
        assert len(originals) >= 5, [(e.declared_relative_path,e.artifact_kind) for e in manifest.entries]
        originals = dict(list(originals.items())[:5])
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        target_transition = runtime.owners.research_graph.query_target_root_commit_transition(handle.target_ref)
        bundle = _finish_stage(runtime, "bundle")
        for _ in range(4):
            request = runtime.owners.advancement_engine.query_reasoning_stage_request(quest["cycle_ref"])
            before = None if request is None else runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
            if before is not None: break
            assert runtime.reasoning_stage.process_once()
        assert before is not None and before.primary_draft is None
        foreground = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])
        with runtime._database.read() as connection:
            completed_units = [dict(row._mapping) for row in connection.execute(text(
                "SELECT * FROM ar_provider_units WHERE status = 'completed' ORDER BY unit_ref"))]
        # This fixture has rolling Bundle turns in addition to stage drafts.
        # Preserve every completed unit, rather than force the live T14 count.
        assert completed_units
        failures = []
        def primary():
            try: runtime.reasoning_stage.process_once()
            except BaseException as error: failures.append(error)
        worker = threading.Thread(target=primary)
        worker.start()
        deadline = time.monotonic() + 15
        while not (root / "provider-calls.jsonl").exists():
            assert time.monotonic() < deadline, (failures,runtime.reasoning_stage.transient_error)
            time.sleep(.01)
        _stage_control(runtime, quest["quest_ref"], "cancel")
        deadline = time.monotonic() + 15
        while runtime.owners.agent_runtime.query_managed_run(before.run_ref)["cleanup_status"] != "completed":
            runtime.owners.agent_runtime.reconcile_pending_provider_cleanup(provider,
                unit_kinds=("reasoning_primary","reasoning_review"))
            assert time.monotonic() < deadline
            time.sleep(.01)
        worker.join(5)
        assert not worker.is_alive() and not failures
        closed = runtime.owners.agent_runtime.query_managed_run(before.run_ref)
        assert closed["status"] == "terminated" and closed["cleanup_status"] == "completed"
        signed = next((root / "reasoning-skill-provider").glob("provider-operations/*/primary/supervisor-exit.json"))
        assert json.loads(signed.read_text())["payload"]["termination_reason"] == "stopped"
        _stage_control(runtime, quest["quest_ref"], "resume")
        resumed = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])
        assert resumed["cycle_ref"] == foreground["cycle_ref"]
        assert resumed["question_ref"] == foreground["question_ref"]
        assert resumed["epoch"] == foreground["epoch"] + 1
        for _ in range(4):
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
            successor_request = runtime.owners.advancement_engine.query_reasoning_stage_request(quest["cycle_ref"])
            successor = None if successor_request is None else runtime.owners.agent_runtime.query_reasoning_stage_run(successor_request.request_ref)
            if successor is not None and successor.run_ref != before.run_ref: break
        assert successor_request.epoch == resumed["epoch"]
        assert successor.request_ref != before.request_ref and successor.run_ref != before.run_ref
        assert successor.primary_invocation.operation_ref != before.primary_invocation.operation_ref
        assert successor.native_session_ref is None and successor.primary_draft is None
        assert len((root / "provider-calls.jsonl").read_text().splitlines()) == 1
        assert runtime.owners.agent_runtime.query_managed_run(before.run_ref)["status"] == "terminated"
        assert runtime.owners.research_graph.query_target_root_commit_transition(handle.target_ref) == target_transition
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == facts
        assert memory.query(completed.manifest_ref) == manifest
        for version, content in originals.items():
            assert runtime.owners.research_memory.materialize_asset(version).content == content
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_commits")).scalar_one() == 1
            for old in completed_units:
                current = connection.execute(text("SELECT * FROM ar_provider_units WHERE unit_ref = :ref"), {"ref":old["unit_ref"]}).one()
                assert dict(current._mapping) == old
        print("STAGE_CANCEL_RESUME " + json.dumps({"quest_ref":quest["quest_ref"],
            "cycle_ref":quest["cycle_ref"], "question_ref":quest["question_ref"],
            "epoch_before":foreground["epoch"], "epoch_after":resumed["epoch"],
            "old_run":before.run_ref,"new_run":successor.run_ref,
            "old_request":before.request_ref,"new_request":successor.request_ref,
            "old_operation":before.primary_invocation.operation_ref,
            "new_operation":successor.primary_invocation.operation_ref,
            "target_commit_ref":completed.target_commit_ref,"manifest_ref":manifest.manifest_ref,
            "completed_stage_units_preserved":[x["unit_ref"] for x in completed_units],
            "five_original_hashes":{version:hashlib.sha256(body).hexdigest() for version,body in originals.items()},
            "new_native":successor.native_session_ref,"old_run_terminal":True},sort_keys=True))
    finally:
        provider.request_stop()
        if worker is not None: worker.join(5)
        runtime.close()
