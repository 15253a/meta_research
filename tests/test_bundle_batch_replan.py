"""A real root may replan from the batch phase without inventing candidate work."""
from types import SimpleNamespace
from dataclasses import replace
from copy import deepcopy

import pytest

from test_bundle_no_action_coordination import _NoActionBundle
from test_public_bundle_stage import _accept_real_target_root_commit
from test_target_root_finalizer import _current_bundle_runtime


class _BatchReplan(_NoActionBundle):
    def __init__(self):
        super().__init__("wait")
        self.basis = None

    def propose_target_batch(self, request):
        self.batches.append(request)
        self.basis = {
            "work_summary": "Preparation produced retained installation materials and a real technical-failure report, not optimizer performance evidence.",
            "source_target_commit_refs": [item["commit_ref"] for item in request.target_commits],
            "limitations": ["No usable environment or frozen comparison configuration was obtained."],
            "unfinished_work": ["The comparison measurement cell remains unperformed."],
            "next_focus": "Reassess the preparation and comparison commitments using the retained materials in the next Plan.",
        }
        return SimpleNamespace(action="replan_required", strategy_update=None,
            replan_basis=self.basis, rationale="The original preparation cell cannot be reassigned; real preparation is still needed before the frozen comparison commitment.",
            native_session_ref=request.native_session_ref, adapter_kind="test_deterministic")


@pytest.mark.parametrize("restart", [False, True], ids=["live", "restart"])
def test_typed_batch_replan_preserves_real_work_and_reaches_reasoning(tmp_path, restart):
    provider = _BatchReplan()
    runtime = _current_bundle_runtime(tmp_path / "typed-batch-replan", bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        owner = runtime.owners.agent_runtime
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        run = owner.query_bundle_stage_run(request_ref)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
        for _ in range(8):
            runtime.bundle_stage.process_once()
            if owner.query_bundle_dispatch_decisions(run.run_ref)[-1].action == "replan_required":
                break
        if restart:
            root = runtime.data_root.root
            runtime.close()
            runtime = _current_bundle_runtime(root, bundle_skill=provider)
            owner = runtime.owners.agent_runtime
        for _ in range(16):
            runtime.bundle_stage.process_once()
            if runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref) is not None:
                break
        report = owner.query_bundle_run_report(run.run_ref)
        assert report is not None, "Typed batch replan did not reach a formal BundleReport"
        assert report.report.disposition == "replan_required"
        assert report.report.accepted_target_commit_refs == tuple(item.commit_ref for item in commits)
        assert report.report.remaining_experiment_keys
        assert provider.basis["work_summary"] in "\n".join(report.report.semantic_change_required)
        assert provider.basis["next_focus"] in "\n".join(report.report.semantic_change_required)
        decision = owner.query_bundle_dispatch_decisions(run.run_ref)[-1]
        assert decision.action == "replan_required"
        assert decision.state["batch_replan"]["basis"] == provider.basis
        assert decision.decision_ref in report.report.evidence_refs
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(request_ref) is not None
        assert owner.query_bundle_target_proposals(run.run_ref) == ()
        assert runtime.owners.research_graph.query_target_graph(request_ref) == graph
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == commits
        assert len(provider.batches) == 1 and provider.coordination == []
        current = owner.query_bundle_stage_run(request_ref)
        assert current.run_ref == run.run_ref and current.native_session_ref == run.native_session_ref
        assert owner.query_stage_provider_correction_feedback(run_ref=current.run_ref,
            attempt_ref=current.attempt_ref, fence_ref=current.fence_ref) is None
    finally:
        runtime.close()


@pytest.mark.parametrize("damage", ["foreign-commit", "stale-head", "wrong-unit", "active-unit"])
def test_owner_rejects_unproven_batch_replan_source(tmp_path, damage):
    from meta_research.owners.common import OwnerConflict

    provider = _BatchReplan()
    runtime = _current_bundle_runtime(tmp_path / damage, bundle_skill=provider)
    try:
        _accept_real_target_root_commit(runtime)
        owner = runtime.owners.agent_runtime
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        run = owner.query_bundle_stage_run(request_ref)
        for _ in range(8):
            runtime.bundle_stage.process_once()
            decision = owner.query_bundle_dispatch_decisions(run.run_ref)[-1]
            if decision.action == "replan_required":
                break
        state = deepcopy(decision.state)
        if damage == "foreign-commit":
            state["target_commit_refs"] = ["foreign-commit"]
            state["batch_replan"]["basis"]["source_target_commit_refs"] = ["foreign-commit"]
        elif damage == "stale-head":
            state["batch_replan"]["base_head_receipt"]["payload_hash"] = "f" * 64
        elif damage == "wrong-unit":
            state["batch_replan"]["provider_unit_ref"] = "not-the-completed-batch"
        else:
            owner.begin_provider_unit(unit_ref="unsettled-work", run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
                operation_ref=run.primary_invocation.operation_ref, unit_kind="bundle_review")
        checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
        with pytest.raises(OwnerConflict, match="bundle_batch_replan"):
            owner.record_bundle_dispatch_decision(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref, native_session_ref=run.native_session_ref,
                graph_ref=decision.graph_ref, generation=decision.generation + 1,
                frontier=(), state=state, action="replan_required", selected_target_ref=None,
                rationale=decision.rationale, inbox_checkpoint=checkpoint,
                idempotency_key="invalid-batch-source:" + damage)
        assert len(owner.query_bundle_dispatch_decisions(run.run_ref)) == decision.generation
    finally:
        runtime.close()


def test_real_batch_adapter_seals_and_replays_typed_synthesis(tmp_path):
    import json
    import sys
    from meta_research.bundle_skill import BundleTargetBatchRequest, CodexBundleSkillAdapter, BundleSkillUnavailable
    from meta_research.owners.common import canonical_hash
    from test_bundle_skill_adapter import _FullConformanceAuthority, _plan_document, _target_plan, _inbox_checkpoint

    plan = _plan_document()
    context_hash = "a" * 64
    initial = _target_plan(plan, context_hash)
    basis = {"work_summary": "Retained preparation artifacts do not establish optimizer performance.",
             "source_target_commit_refs": ["target-commit:1"],
             "limitations": ["No verified comparison configuration."],
             "unfinished_work": ["The comparison cell remains unperformed."],
             "next_focus": "Reassess preparation in the successor Plan."}
    output = {"action": "replan_required", "strategy_update": None, "replan_basis": basis,
              "rationale": "The frozen preparation assignment needs reassessment before comparison."}
    # Run an actual supervised local process. A runner that merely returns a
    # CompletedProcess cannot prove the durable supervisor-exit signature.
    calls = tmp_path / "provider-calls.jsonl"
    schema_copy = tmp_path / "provider-schema.json"
    executable_path = tmp_path / "codex"
    executable_path.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        "if '--version' in sys.argv:\n print('codex-bundle-test 1');raise SystemExit(0)\n"
        "prompt=sys.stdin.read()\nassert prompt\n"
        "args=sys.argv\nschema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())\n"
        f"output={output!r}\n"
        "if set(schema.get('properties',{}))=={'provider_output'}:output={'provider_output':output}\n"
        "Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(output))\n"
        f"Path({str(schema_copy)!r}).write_text(json.dumps(schema))\n"
        f"with Path({str(calls)!r}).open('a') as stream:stream.write('called\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'codex-bundle-primary:1'}))\n",
        encoding="utf-8")
    executable_path.chmod(0o700)
    executable = str(executable_path)
    workspace = tmp_path / "adapter"

    def adapter():
        value = CodexBundleSkillAdapter(workspace, executable=executable)
        value.bind_full_conformance_authority(_FullConformanceAuthority())
        value.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        return value

    provider = adapter()
    candidate = initial["initial_strategy_update"]["candidates"][0]
    request = BundleTargetBatchRequest(stage_request_ref="stage-request:1", cycle_ref="cycle:bundle-1",
        run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        graph_ref="target-graph:1", formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1", context_pack_hash=context_hash,
        plan_document=plan, initial_target_plan=initial, base_generation=0,
        base_head_receipt={"receipt_ref": "graph-head:1", "payload_hash": "b" * 64},
        current_targets=({"target_ref": "target:1", "spec": candidate, "spec_hash": canonical_hash(candidate)},),
        target_commits=({"commit_ref": "target-commit:1", "target_ref": "target:1", "closure_hash": "c" * 64},),
        root_session_ref="ar-session:bundle-1", native_session_ref="codex-bundle-primary:1",
        runtime_binding=provider.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1"),
        cycle_position={"cycle_ref": "cycle:bundle-1", "round": 1, "cycle_focus": "Reassess unfinished comparison",
                        "focus_source": {"request_ref": "fixture-start"}},
        job_ref="typed-batch-replan-operation")
    result = provider.propose_target_batch(request)
    assert result.action == "replan_required" and result.strategy_update is None and result.replan_basis == basis
    assert calls.read_text().splitlines() == ["called"]

    schema = json.loads(schema_copy.read_text())
    schema = schema.get("properties", {}).get("provider_output", schema)
    assert schema["additionalProperties"] is False
    assert "replan_basis" in schema["required"]
    restarted = adapter()
    assert restarted.propose_target_batch(request) == result
    assert calls.read_text().splitlines() == ["called"]
    with pytest.raises(BundleSkillUnavailable):
        restarted.propose_target_batch(replace(request, base_head_receipt={"receipt_ref": "different-head", "payload_hash": "f" * 64}))
    assert calls.read_text().splitlines() == ["called"]


@pytest.mark.parametrize("full_prose", [False, True])
def test_nullable_batch_contract_roundtrips_strict_wire_documents(full_prose):
    from meta_research.bundle_skill import _target_batch_schema, _schema_template_request
    from meta_research.bundle_output_assembly import full_prose_output_schema
    from meta_research.idea_skill import _compile_codex_output_schema, _decode_codex_provider_output
    from test_bundle_skill_adapter import _plan_document, _target_plan, _provider_wire_value

    schema = _target_batch_schema(_schema_template_request())
    if full_prose:
        schema = full_prose_output_schema(schema)
    strategy = deepcopy(_target_plan(_plan_document(), "a" * 64)["initial_strategy_update"])
    strategy.update(revision=2, notes="")
    output = {"action": "propose_targets", "strategy_update": strategy,
              "replan_basis": None, "rationale": "The next candidate addresses an unfinished cell."}
    wire = _provider_wire_value(output, _compile_codex_output_schema(schema))
    assert wire["replan_basis"] is None
    assert isinstance(wire["strategy_update"]["candidates"][0]["measurement_contract"]["baseline_forward_contract"], str)
    assert _decode_codex_provider_output(wire, schema) == output


def test_real_cf6_batch_binding_renews_after_formal_pause_resume(tmp_path, monkeypatch):
    """Derive old schemas from cf6 code, with shared resources held current.

    This isolates the adapter/schema/helper transition; complete installed
    release bindings are separately checked by the deployment probes.
    """
    import hashlib
    import os
    import sys
    import types
    from pathlib import Path
    import meta_research.bundle_skill as current_module
    from test_bundle_no_action_coordination import _rejected_batch
    from test_bundle_strategy_revision import _coordinate
    from test_bundle_skill_adapter import _fake_codex
    from test_quest_research_time import _control

    source_path = os.environ.get("BATCH_REPLAN_BASELINE_BUNDLE_SKILL")
    if not source_path:
        pytest.skip("Requires the immutable cf6 release source supplied by the bounded acceptance runner")
    old_source = Path(source_path).read_bytes()
    assert b"class BundleTargetBatchResult" in old_source and b"replan_basis" not in old_source
    historical_module = types.ModuleType("meta_research._cf6_bundle_skill_for_test")
    historical_module.__file__ = current_module.__file__
    monkeypatch.setitem(sys.modules, historical_module.__name__, historical_module)
    exec(compile(old_source, str(source_path), "exec"), historical_module.__dict__)
    original_hash = historical_module._file_sha256
    historical_module._file_sha256 = lambda path: (
        hashlib.sha256(old_source).hexdigest() if path.name == "bundle_skill.py" else original_hash(path))
    historical = [True]
    executable = str(_fake_codex(tmp_path / "binding-codex"))

    def binding(provider):
        module = historical_module if historical[0] else current_module
        adapter = module.CodexBundleSkillAdapter(tmp_path / "binding", executable=executable)
        adapter.bind_full_conformance_authority(provider._authority)
        return adapter.runtime_binding()

    monkeypatch.setattr(_NoActionBundle, "runtime_binding", binding)
    runtime, provider, run, graph, _commits, _feedback = _rejected_batch(tmp_path, "wait")
    try:
        _coordinate(runtime, provider)
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(run.request_ref)
        old_execution = owner.query_bundle_attempt_execution(graph.submission_ref)
        _control(runtime, graph.quest_ref, "pause", "batch-contract-upgrade-pause")
        root = runtime.data_root.root
        runtime.close()
        historical[0] = False
        runtime = _current_bundle_runtime(root, bundle_skill=provider)
        owner = runtime.owners.agent_runtime
        new_binding = provider.runtime_binding()
        old_resources, new_resources = set(run.runtime_binding.resource_bindings), set(new_binding.resource_bindings)
        for prefix in ("output-schema:target-batch@", "output-schema:target-batch-full@"):
            assert {r for r in old_resources if r.startswith(prefix)} != {r for r in new_resources if r.startswith(prefix)}
        assert not any(r.startswith("adapter-source:meta_research.bundle_batch_replan@") for r in old_resources)
        assert any(r.startswith("adapter-source:meta_research.bundle_batch_replan@") for r in new_resources)
        _control(runtime, graph.quest_ref, "resume", "batch-contract-upgrade-resume")
        for _ in range(8):
            runtime.bundle_stage.process_once()
            current = owner.query_bundle_stage_run(run.request_ref)
            if current.runtime_binding == new_binding:
                break
        assert current.runtime_binding == new_binding
        assert current.run_ref == run.run_ref and current.root_session_ref == run.root_session_ref
        assert current.native_session_ref == run.native_session_ref and current.attempt_ref != run.attempt_ref
        assert owner.query_bundle_attempt_execution(graph.submission_ref) == old_execution
    finally:
        runtime.close()
