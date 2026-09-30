"""Frozen Plan Evidence names survive Target preparation and formal acceptance."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from meta_research.feed import DurableFeed
from meta_research.formal_entities import verified_target_input_asset_refs
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.plan_skill import _with_derived_answer_contract_hash
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from meta_research.target_run_finalizer import SQLiteTargetRootCompletionMemoryAuthority, TargetRunFinalizer
from test_plan_asset_target_input import _asset, _origin
import test_public_bundle_stage as fixtures
from test_public_first_question_deepfetch import SnapshotAwareProposalDrafter, DeterministicDeepFetchProvider, DeterministicProbe
from test_public_manual_question_lifecycle import DeterministicAcquisitionProvider
from test_public_reasoning_stage import _confirm_deepfetch_quest, _MultiRunIdeaSkill, _MultiRunPlanSkill
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_plan_workproduct_target_input import _Bundle, _Reasoning
from test_target_root_finalizer import _root_finalizer_fixture
from test_two_cycle_actual_work import _ready_existing, _finish_stage


class _SelectedEvidencePlan(_MultiRunPlanSkill):
    def __init__(self):
        super().__init__(no_gap=False)
        self.entries = []

    def _document(self, request):
        document = super()._document(request)
        if self.entries:
            uses = [{"obligation_key": document["gap_set"][0], "evidence_ref": entry["evidence_ref"],
                "supported_claim": "The retained earlier work supplies exact input material for a new comparison.",
                "support_boundary": "Historical input identity is fixed; the new comparison remains open.",
                "contributing_idea_refs": [request.accepted_idea_set["candidates"][0]["candidate_key"]]}
                for entry in self.entries]
            document["coverage"][0]["evidence_uses"] = uses
            document["evidence_reuse_set"] = uses
            document["additional_evidence_bindings"] = deepcopy(self.entries)
        return _with_derived_answer_contract_hash(document, request)


class _EvidenceBundle(_Bundle):
    def _target_plan(self, request):
        result = super()._target_plan(request)
        if isinstance(self.input_ref, list):
            result["initial_strategy_update"]["candidates"][0]["candidate"][
                "direct_accepted_input_asset_refs"] = self.input_ref[:]
        return result


def _finalizer(runtime, lifecycle, memory):
    return TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)


def _start_root(runtime, ready, prior, assets=()):
    accepted_graph, target, _, dispatch, launch = ready
    graph = runtime.owners.research_graph
    inputs = runtime.target_run_authorities.research_graph
    key = target.target_ref
    graph.accept_formal_plan_content(formal_plan_ref=accepted_graph.formal_plan_ref, idempotency_key="plan:" + key)
    graph.accept_target_formal_plan_projection(graph_ref=accepted_graph.graph_ref, idempotency_key="plan-projection:" + key)
    graph.accept_target_candidate_projection(target_ref=key, idempotency_key="candidate:" + key)
    runtime.owners.agent_runtime.admit_target_launch(launch,
        dispatch_decision_ref=dispatch.decision_ref, idempotency_key="launch:" + key)
    with runtime._database.read() as connection:
        launch_row = connection.execute(text("SELECT launch_ref,target_run_ref FROM ar_target_launches "
            "WHERE target_ref=:ref"), {"ref": key}).one()
    candidate = inputs.query_candidate_projection(target_ref=key).candidate
    formal_plan = graph.query_target_formal_plan_projection(graph_ref=accepted_graph.graph_ref).formal_plan
    asset_refs = tuple(sorted(asset.asset_ref for asset in assets))
    scope = canonical_target_scope_binding(target_ref=key,
        target_run_ref=launch_row.target_run_ref, target_spec_hash=launch.target_spec_binding.content_hash_ref,
        candidate=candidate, formal_plan=formal_plan, accepted_input_refs=asset_refs)
    admission = runtime.harnesses.admit_target_run(target_ref=key,
        target_run_ref=launch_row.target_run_ref, harness_family="codex", model_ref="gpt-target-run",
        auth_profile_ref="harness-profile:codex-default", target_scope_binding=scope)
    inputs.accept_execution_input_binding(target_ref=key,
        target_run_ref=admission.run.run_ref, target_attempt_ref=admission.run.attempt_ref,
        target_fence_ref=admission.run.fence_ref, target_spec_hash=launch.target_spec_binding.content_hash_ref,
        target_scope_binding_hash=canonical_hash(scope), input_refs=asset_refs, idempotency_key="inputs:" + key)
    ar = runtime.target_run_authorities.agent_runtime
    handle = ar.query_current_target_work_handle(key)
    ar.reserve_target_workspace(handle=handle, idempotency_key="workspace:" + key)
    lifecycle = SQLiteTargetRootLifecycleAuthority(runtime._database, DurableFeed(runtime._database), ar)
    lifecycle.activate(launch_ref=launch_row.launch_ref, handle=handle, candidate=candidate,
        formal_plan=formal_plan, idempotency_key="activate:" + key)
    memory = SQLiteTargetRootCompletionMemoryAuthority(runtime._database, DurableFeed(runtime._database),
        runtime.owners.research_memory, lifecycle)
    workspace_ref, workspace = ar.resolve_target_workspace(target_ref=key,
        target_run_ref=handle.target_run_ref, root_session_ref=handle.root_session_ref,
        attempt_ref=handle.execution_attempt_ref, fence_ref=handle.execution_fence_ref)
    _finalizer(runtime, lifecycle, memory).materialize_inputs(handle=handle)
    (workspace / "implementation/consume.py").write_text("print('consume exact frozen evidence')\n")
    (workspace / "outputs").mkdir()
    evidence = replace(prior, target_ref=key, target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref, root_session_ref=handle.root_session_ref,
        fence_ref=handle.execution_fence_ref, workspace_ref=workspace_ref, handoff=None,
        native_session_ref="native:" + key, operation_ref="operation:" + key, evidence_ref="evidence:" + key)
    return lifecycle, memory, handle, workspace, evidence


def _admitted_evidence_input(tmp_path, assessed):
    """Create issuer-backed evidence in Cycle 1 and select it in Cycle 2."""
    plan, bundle = _SelectedEvidencePlan(), _EvidenceBundle()
    runtime = fixtures.build_production_runtime(fixtures.prepare_data_root(tmp_path / "evidence-inputs"),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        intent_drafting_provider=fixtures._DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(), deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=DeterministicAcquisitionProvider(), idea_skill_provider=_MultiRunIdeaSkill(),
        plan_skill_provider=plan, bundle_skill_provider=bundle,
        reasoning_skill_provider=_Reasoning(entry_stage="plan"),
        harness_adapters=(fixtures._FullConformanceAdapter("codex"),),
        power_inhibitor=fixtures._TogglePowerInhibitor())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        fixtures._finish_idea_stage(runtime)
        fixtures._finish_plan_stage(runtime)
        _, lifecycle, memory, authority, source_handle, workspace, prior = _root_finalizer_fixture(
            tmp_path, runtime=runtime, ready=_ready_existing(runtime))
        metrics = {key: 1.0 for key in authority.measurement_contract.protocol_version.required_metric_keys}
        (workspace / "outputs/data").mkdir()
        runs = []
        for name in ("first", "second"):
            path = "outputs/data/" + name + ".txt"
            (workspace / path).write_text(name + " frozen source bytes\n")
            runs.append({"run_key": name, "implementation_paths": ["implementation"],
                "checkpoint_paths": [], "artifact_paths": [path],
                "evaluations": [{"attempt_key": name + "-assessment", "metrics": metrics}] if assessed else []})
        (workspace / "outputs/result.json").write_text(canonical_json({
            "schema_ref": authority.measurement_contract.result_schema_ref,
            "metrics": metrics if assessed else {}, "result_disposition": "positive" if assessed else "uncertain",
            "formal_runs": runs}))
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=source_handle.target_ref, target_run_ref=source_handle.target_run_ref,
            root_session_ref=source_handle.root_session_ref, attempt_ref=source_handle.execution_attempt_ref,
            fence_ref=source_handle.execution_fence_ref)
        summary = "Retained historical input material for a later comparison."
        evidence = replace(prior, handoff=None, workspace_ref=workspace_ref, final_text=summary,
            final_text_sha256=hashlib.sha256(summary.encode()).hexdigest())
        completed = _finalizer(runtime, lifecycle, memory).finalize(handle=source_handle, evidence=evidence)
        assert completed.status == "completed"
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=source_handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        graph = runtime.owners.research_graph
        _finish_stage(runtime, "bundle")
        _, entries = TargetCommitEvidenceCatalog(graph, runtime.owners.research_memory).query_plan_evidence_catalog(
            quest_ref=quest["quest_ref"], target_commit_refs=(completed.target_commit_ref,))
        assert len(entries) == 1
        plan.entries = list(entries)
        bundle.input_ref = [entry["evidence_ref"] for entry in entries]
        _finish_stage(runtime, "reasoning")
        _finish_stage(runtime, "plan")
        inputs = runtime.target_run_authorities.research_graph
        next_cycle = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])["cycle_ref"]
        accepted_graph = None
        for _ in range(20):
            runtime.bundle_stage.process_once()
            request = runtime.owners.advancement_engine.query_bundle_stage_request(next_cycle)
            accepted_graph = None if request is None else graph.query_target_graph(request.request_ref)
            if accepted_graph is not None:
                break
        assert accepted_graph is not None
        target = accepted_graph.targets[0]
        inputs.prepare_input_assets(target.target_ref)
        assets = tuple(inputs.query_input_asset_projection(target_ref=target.target_ref,
            asset_ref=entry["asset_ref"]).asset for entry in entries)
        assert len({asset.asset_ref for asset in assets}) == 1
        for entry, asset in zip(entries, assets):
            assert asset.version_ref == entry["asset_version_ref"]
            assert inputs.resolve_input_asset_ref(target_ref=target.target_ref,
                input_ref=entry["evidence_ref"]) == asset.asset_ref
        ready = _ready_existing(runtime)
        lifecycle, memory, handle, workspace, evidence = _start_root(runtime, ready, evidence, assets)
        return runtime, lifecycle, memory, handle, workspace, evidence, tuple(entries), assets, quest["quest_ref"]
    except BaseException:
        runtime.close()
        raise


@pytest.mark.parametrize("assessed", [True, False], ids=["metric-result", "work-product"])
def test_selected_evidence_names_and_exact_asset_versions_complete_formal_run(tmp_path, assessed):
    runtime, lifecycle, memory, handle, workspace, evidence, entries, assets, _ = _admitted_evidence_input(tmp_path, assessed)
    try:
        graph = runtime.owners.research_graph
        aliases = [entry["evidence_ref"] for entry in entries]
        refs = [*aliases, assets[0].asset_ref, assets[0].version_ref]
        authority = graph.query_target_measurement_domain_authority(handle.target_ref)
        metrics = {key: 1.0 for key in authority.measurement_contract.protocol_version.required_metric_keys}
        (workspace / "outputs/result.json").write_text(canonical_json({
            "schema_ref": authority.measurement_contract.result_schema_ref,
            "metrics": metrics if assessed else {}, "result_disposition": "positive" if assessed else "uncertain",
            "formal_runs": [{"run_key": "consume-frozen-input", "implementation_paths": ["implementation"],
                "input_refs": refs, "checkpoint_paths": [], "artifact_paths": [],
                "evaluations": [{"attempt_key": "check-frozen-inputs", "metrics": metrics}] if assessed else []}]}))
        finalizer = _finalizer(runtime, lifecycle, memory)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        assert completed.target_commit_ref
        facts = graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        assert bool(facts[0]["evaluation_attempt"]) is assessed
        assert bool(facts[0]["metric_result"]) is assessed
        assert memory.query(completed.manifest_ref).result_document.as_dict()["formal_runs"][0]["input_refs"] == refs
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert graph.query_target_formal_results(handle.target_ref) == facts
    finally:
        runtime.close()


def test_formal_evidence_alias_authority_remains_exact(tmp_path):
    runtime, _, _, handle, _, _, entries, assets, quest_ref = _admitted_evidence_input(tmp_path, True)
    try:
        graph = runtime.owners.research_graph
        proofs = projection_plain_value(handle.accepted_input_asset_proofs)
        aliases = {entry["evidence_ref"] for entry in entries}
        for key in ("rm_acceptance_receipt", "rg_role_receipt"):
            forged = deepcopy(proofs)
            forged[0][key]["receipt_ref"] = "receipt_forged"
            with pytest.raises(OwnerConflict, match="target_run_input_asset_proof_invalid"):
                verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=forged)
        with pytest.raises(OwnerConflict, match="target_input_reference_scope_invalid"):
            graph.query_target_commit_input_asset_bindings(target_ref=handle.target_ref,
                quest_ref="quest_not_the_accepted_quest", target_commit_refs=(entries[0]["target_commit_root_ref"],))
        refs = verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=[])
        assert entries[0]["evidence_ref"] not in refs
        assert assets[0].asset_ref not in refs and assets[0].version_ref not in refs
        newer = _asset(runtime, "newer-unselected-evidence-version", asset_ref=assets[0].asset_ref)
        _origin(runtime, newer, quest_ref, "newer-evidence-origin")
        refs = verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=proofs)
        assert aliases <= refs
        assert newer.version_ref not in refs
        with pytest.raises(OwnerConflict, match="target_input_reference_not_declared"):
            runtime.target_run_authorities.research_graph.resolve_input_asset_ref(
                target_ref=handle.target_ref, input_ref="evidence_not_selected")
        # Corruption of an isolated acceptance row cannot rebind a selected alias.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rm_target_input_asset_proofs SET version_ref=:version "
                "WHERE target_ref=:target AND asset_ref=:asset"),
                {"version": newer.version_ref, "target": handle.target_ref, "asset": assets[0].asset_ref})
        with pytest.raises(OwnerConflict):
            verified_target_input_asset_refs(graph, target_ref=handle.target_ref, proofs=proofs)
    finally:
        runtime.close()
