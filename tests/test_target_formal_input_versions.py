"""A formal Run names the exact version admitted by Target RM/RG proofs."""
import hashlib
import json
from copy import deepcopy
import subprocess
import sys

import pytest
from sqlalchemy import text

from meta_research.feed import DurableFeed
from meta_research.bundle_protocol import projection_plain_value
from meta_research.formal_entities import verified_target_input_asset_refs
from meta_research.owners.agent_runtime_harness import TargetRootCompletionEvidence
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.target_run_finalizer import SQLiteTargetRootCompletionMemoryAuthority, TargetRunFinalizer
from test_plan_asset_target_input import _runtime, _asset, _origin, _accepted_target
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_two_cycle_actual_work import _ready_existing
import test_public_bundle_stage as fixtures


def _admitted_input_root(tmp_path):
    runtime, plan, bundle = _runtime(tmp_path / "exact-input")
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        selected = _asset(runtime, "selected-original")
        _origin(runtime, selected, quest["quest_ref"], "selected-origin")
        plan.source_ref = bundle.source_ref = selected.version_ref
        target, _ = _accepted_target(runtime, quest)
        inputs = runtime.target_run_authorities.research_graph
        assert inputs.prepare_input_assets(target.target_ref)
        newer = _asset(runtime, "unselected-newer", asset_ref=selected.asset_ref)
        _origin(runtime, newer, quest["quest_ref"], "newer-origin")
        _, target, _, dispatch, launch = _ready_existing(runtime)
        runtime.owners.agent_runtime.admit_target_launch(launch,
            dispatch_decision_ref=dispatch.decision_ref, idempotency_key="formal-version-launch")
        with runtime._database.read() as connection:
            launch_row = connection.execute(text("SELECT launch_ref,target_run_ref FROM ar_target_launches "
                "WHERE target_ref=:ref"), {"ref": target.target_ref}).one()
        candidate = inputs.query_candidate_projection(target_ref=target.target_ref).candidate
        formal_plan = runtime.owners.research_graph.query_target_formal_plan_projection(
            graph_ref=target.graph_ref).formal_plan
        scope = canonical_target_scope_binding(target_ref=target.target_ref,
            target_run_ref=launch_row.target_run_ref,
            target_spec_hash=launch.target_spec_binding.content_hash_ref,
            candidate=candidate, formal_plan=formal_plan, accepted_input_refs=(selected.asset_ref,))
        admission = runtime.harnesses.admit_target_run(target_ref=target.target_ref,
            target_run_ref=launch_row.target_run_ref, harness_family="codex", model_ref="gpt-target-run",
            auth_profile_ref="harness-profile:codex-default", target_scope_binding=scope)
        inputs.accept_execution_input_binding(target_ref=target.target_ref,
            target_run_ref=admission.run.run_ref, target_attempt_ref=admission.run.attempt_ref,
            target_fence_ref=admission.run.fence_ref, target_spec_hash=launch.target_spec_binding.content_hash_ref,
            target_scope_binding_hash=canonical_hash(scope), input_refs=(selected.asset_ref,),
            idempotency_key="formal-version-execution-input")
        ar = runtime.target_run_authorities.agent_runtime
        handle = ar.query_current_target_work_handle(target.target_ref)
        proof, = handle.accepted_input_asset_proofs
        assert proof.asset_ref == proof.rm_acceptance_receipt.subject_ref == selected.asset_ref
        assert inputs.query_input_asset_projection(target_ref=target.target_ref,
            asset_ref=selected.asset_ref).asset == selected
        ar.reserve_target_workspace(handle=handle, idempotency_key="formal-version-workspace")
        lifecycle = SQLiteTargetRootLifecycleAuthority(runtime._database, DurableFeed(runtime._database), ar)
        lifecycle.activate(launch_ref=launch_row.launch_ref, handle=handle, candidate=candidate,
            formal_plan=formal_plan, idempotency_key="formal-version-activate")
        memory = SQLiteTargetRootCompletionMemoryAuthority(runtime._database, DurableFeed(runtime._database),
            runtime.owners.research_memory, lifecycle)
        workspace_ref, workspace = ar.resolve_target_workspace(target_ref=target.target_ref,
            target_run_ref=handle.target_run_ref, root_session_ref=handle.root_session_ref,
            attempt_ref=handle.execution_attempt_ref, fence_ref=handle.execution_fence_ref)
        ar.materialize_target_workspace_inputs(handle=handle)
        script = workspace / "implementation/read.py"
        script.write_text("from pathlib import Path\nimport json\n"
            "pointer = json.loads(Path('inputs/manifest.json').read_text())\n"
            "manifest_path = Path(pointer['read_only_manifest_path'])\n"
            "manifest = json.loads(manifest_path.read_text())\n"
            "entry, = [e for e in manifest['entries'] if e['kind'] == 'direct_asset']\n"
            "value = (manifest_path.parent / entry['relative_path']).read_bytes()\n"
            "assert value == b'selected-original: observed input bytes\\n'\n"
            "print(entry['version_ref'])\n")
        assert subprocess.check_output([sys.executable, str(script)], cwd=workspace).decode().strip() == selected.version_ref
        (workspace / "outputs").mkdir()
        summary = "Consumed the selected original asset version in one actual run."
        evidence = TargetRootCompletionEvidence(target_ref=target.target_ref,
            target_run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
            attempt_generation=1, root_session_ref=handle.root_session_ref,
            native_session_ref="native-formal-version", fence_ref=handle.execution_fence_ref,
            operation_ref="formal-version-final", operation_generation=1,
            evidence_ref="formal-version-evidence", evidence_sequence=1, handoff=None,
            workspace_ref=workspace_ref, final_text=summary,
            final_text_sha256=hashlib.sha256(summary.encode()).hexdigest(), observed_at=1.0)
        return runtime, lifecycle, memory, handle, workspace, evidence, selected, newer
    except BaseException:
        runtime.close()
        raise


@pytest.mark.parametrize("assessed", [True, False], ids=["evaluated", "workproduct"])
@pytest.mark.parametrize("use_newer", [False, True], ids=["selected-version", "unselected-newer-version"])
def test_formal_run_uses_only_exact_admitted_input_version(tmp_path, assessed, use_newer):
    runtime, lifecycle, memory, handle, workspace, evidence, selected, newer = _admitted_input_root(tmp_path)
    try:
        graph = runtime.owners.research_graph
        authority = graph.query_target_measurement_domain_authority(handle.target_ref)
        proofs = projection_plain_value(handle.accepted_input_asset_proofs)
        assert verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=proofs) == {
            selected.asset_ref, selected.version_ref}
        for key in ("rm_acceptance_receipt", "rg_role_receipt"):
            forged = deepcopy(proofs)
            forged[0][key]["receipt_ref"] = "receipt_forged"
            with pytest.raises(OwnerConflict, match="target_run_input_asset_proof_invalid"):
                verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=forged)
        with pytest.raises(OwnerConflict, match="target_run_input_asset_proof_invalid"):
            verified_target_input_asset_refs(graph, target_ref="target_not_admitted", proofs=proofs)
        metrics = {key: 1.0 for key in authority.measurement_contract.protocol_version.required_metric_keys}
        input_ref = newer.version_ref if use_newer else selected.version_ref
        document = {"schema_ref": authority.measurement_contract.result_schema_ref,
            "metrics": metrics if assessed else {}, "result_disposition": "positive" if assessed else "uncertain",
            "formal_runs": [{"run_key": "consume-original", "implementation_paths": ["implementation"],
                "input_refs": [input_ref], "checkpoint_paths": [], "artifact_paths": [],
                "evaluations": [{"attempt_key": "check-original", "metrics": metrics}] if assessed else []}]}
        (workspace / "outputs/result.json").write_text(canonical_json(document))
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=graph, graph_authority=graph)
        if use_newer:
            with pytest.raises(OwnerConflict, match="target_formal_input_reference_invalid"):
                finalizer.finalize(handle=handle, evidence=evidence)
            with runtime._database.read() as connection:
                assert connection.execute(text("SELECT COUNT(*) FROM rg_variant_runs")).scalar_one() == 0
                assert connection.execute(text("SELECT COUNT(*) FROM rg_target_commits")).scalar_one() == 0
            return
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        fact = facts[0]
        with runtime._database.read() as connection:
            binding = connection.execute(text("SELECT b.inputs_json FROM rg_experiment_input_bindings b "
                "JOIN rg_variant_runs r ON r.input_binding_ref=b.binding_ref WHERE r.variant_run_ref=:ref"),
                {"ref": fact["variant_run"]["variant_run_ref"]}).scalar_one()
        assert json.loads(binding)["input_refs"] == [selected.version_ref]
        assert bool(fact["evaluation_attempt"]) is assessed
        assert bool(fact["metric_result"]) is assessed
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert graph.query_target_formal_results(handle.target_ref) == facts
        manifest = memory.query(completed.manifest_ref)
        assert manifest.result_document.as_dict()["formal_runs"][0]["input_refs"] == [selected.version_ref]
        # The run-preservation adapter shares the same verified version source.
        # Feed it the real AR/RM completion objects, without substituting an Owner.
        from meta_research.run_only_registration import _source_inputs
        completion = lifecycle.query_completion(handle.target_ref)
        target = next(item for item in graph.query_target_graph(authority.stage_request_ref).targets
            if item.target_ref == handle.target_ref)
        run = manifest.result_document.as_dict()["formal_runs"][0]
        preserved = _source_inputs(completion, manifest, target, authority, run, source_owner=graph)
        assert preserved["input_refs"] == [selected.version_ref]
        with pytest.raises(OwnerConflict, match="target_formal_input_reference_invalid"):
            _source_inputs(completion, manifest, target, authority,
                {**run, "input_refs": [newer.version_ref]}, source_owner=graph)
        assert runtime.owners.research_memory.materialize_asset(selected.version_ref).content == b"selected-original: observed input bytes\n"
    finally:
        runtime.close()
