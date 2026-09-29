"""Idea v4 cites exact existing research sources without minting evidence roles."""
from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_public_idea_stage import _runtime, _confirm_direct_quest
from test_public_plan_stage import _finish_idea_stage
from test_public_reasoning_stage import _MultiRunIdeaSkill, _confirm_deepfetch_quest
from test_plan_asset_target_input import _asset, _origin
from test_plan_workproduct_target_input import (
    _Plan, _Bundle, _Reasoning, fixtures, SnapshotAwareProposalDrafter,
    DeterministicProbe, DeterministicDeepFetchProvider, DeterministicAcquisitionProvider,
)
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _root_finalizer_fixture
from test_two_cycle_actual_work import _ready_existing, _finish_stage
from test_idea_owner_integrity import _confirm_question_with_prefix


class _SourceIdea(_MultiRunIdeaSkill):
    source_ref = None

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if self.source_ref is None:
            return draft
        outcome = deepcopy(draft.draft)
        outcome['candidates'][0]['evidence_boundary']['accepted_evidence_refs'] = [self.source_ref]
        return replace(draft, draft=outcome)


def test_successor_idea_accepts_committed_target_asset_without_evidence_role(tmp_path):
    provider = _SourceIdea()
    runtime = fixtures.build_production_runtime(fixtures.prepare_data_root(tmp_path / 'target-source'),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        intent_drafting_provider=fixtures._DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(), deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=DeterministicAcquisitionProvider(),
        idea_skill_provider=provider, plan_skill_provider=_Plan(), bundle_skill_provider=_Bundle(),
        reasoning_skill_provider=_Reasoning(entry_stage='idea'),
        harness_adapters=(fixtures._FullConformanceAdapter('codex'),),
        power_inhibitor=fixtures._TogglePowerInhibitor())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        fixtures._finish_idea_stage(runtime)
        fixtures._finish_plan_stage(runtime)
        ready = _ready_existing(runtime)
        _, lifecycle, memory, authority, handle, workspace, prior = _root_finalizer_fixture(
            tmp_path, runtime=runtime, ready=ready)
        (workspace / 'outputs/data').mkdir()
        (workspace / 'outputs/data/observation.txt').write_text('Uncertain retained observation.\n')
        (workspace / 'outputs/result.json').write_text(canonical_json({
            'schema_ref': authority.measurement_contract.result_schema_ref,
            'metrics': {}, 'result_disposition': 'uncertain',
            'formal_runs': [{'run_key': 'retained-observation', 'implementation_paths': ['implementation'],
                'checkpoint_paths': [], 'artifact_paths': ['outputs/data/observation.txt'], 'evaluations': []}]}))
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        summary = 'Retained an observation; no evaluation or comparative claim.'
        evidence = replace(prior, handoff=None, workspace_ref=workspace_ref, final_text=summary,
            final_text_sha256=hashlib.sha256(summary.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == 'completed'
        manifest = memory.query(completed.manifest_ref)
        source = next(entry.binding for entry in manifest.entries
            if entry.declared_relative_path == 'outputs/data/observation.txt')
        graph = runtime.owners.research_graph
        assert graph.query_asset_roles(version_refs=(source.version_ref,)) == ()
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
        _finish_stage(runtime, 'bundle')
        _finish_stage(runtime, 'reasoning')
        provider.source_ref = source.version_ref
        current = _finish_idea_stage(runtime)
        request = runtime.owners.advancement_engine.query_idea_stage_request(current['eligibility']['cycle_ref'])
        assert request.context_pack['schema_ref'].endswith('/v4')
        assert source.version_ref not in request.context_pack['accepted_evidence_refs']
        decision = graph.query_idea_outcome_decision(current['run']['submission_ref'])
        assert decision.decision == 'accepted'
        assert graph.query_asset_roles(version_refs=(source.version_ref,)) == ()
        assert graph.query_idea_outcome_decision(current['run']['submission_ref']) == decision
        with runtime._database.write() as connection:
            connection.execute(text('UPDATE rm_asset_versions SET content_hash=:bad WHERE version_ref=:ref'),
                {'bad': '0' * 64, 'ref': source.version_ref})
        with pytest.raises(OwnerConflict):
            graph.query_idea_outcome_decision(current['run']['submission_ref'])
    finally:
        runtime.close()


def test_idea_accepts_quest_material_and_preserves_legacy_evidence_rules(tmp_path):
    provider = _SourceIdea()
    runtime = _runtime(tmp_path / 'quest-source', provider)
    try:
        quest = _confirm_direct_quest(runtime)
        source = _asset(runtime, 'quest-material')
        _origin(runtime, source, quest['quest_ref'], 'quest-material-origin')
        provider.source_ref = source.version_ref
        current = _finish_idea_stage(runtime)
        graph = runtime.owners.research_graph
        assert graph.query_idea_outcome_decision(current['run']['submission_ref']).decision == 'accepted'
        with pytest.raises(OwnerConflict, match='idea_context_pack_invalid'):
            graph._receipt_verifier.verify_evidence_refs(
                quest_ref=quest['quest_ref'], version_refs=(source.version_ref,))
        assert [role.role for role in graph.query_asset_roles(version_refs=(source.version_ref,))] == ['quest_source_material']
    finally:
        runtime.close()


@pytest.mark.parametrize('source_kind', ['foreign', 'orphan', 'missing'])
def test_idea_rejects_assets_without_current_quest_origin(tmp_path, monkeypatch, source_kind):
    provider = _SourceIdea()
    runtime = _runtime(tmp_path / source_kind, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        cycle = runtime.idea_stage._discover_current_cycle()
        source = _asset(runtime, source_kind)
        if source_kind == 'foreign':
            human = runtime.owners.human_collaboration
            observe = human.observe_host_compute
            monkeypatch.setattr(human, 'observe_host_compute',
                lambda initialization, _devices, key: observe(initialization, ['GPU-idea-test'], key))
            foreign = _confirm_question_with_prefix(runtime, 'foreign')
            _origin(runtime, source, foreign['quest_ref'], 'foreign-origin')
        provider.source_ref = source.version_ref + 'd' if source_kind == 'missing' else source.version_ref
        graph = runtime.owners.research_graph
        rejected = None
        for _ in range(12):
            assert runtime.idea_stage._process_cycle(cycle).advanced
            request = runtime.owners.advancement_engine.query_idea_stage_request(quest['cycle_ref'])
            run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
            if run is not None and run.execution is not None:
                rejected = graph.query_idea_outcome_decision(run.execution.submission_ref)
                if rejected is not None:
                    break
        assert rejected is not None and rejected.decision == 'rejected'
        assert rejected.reason_code == 'asset_quest_scope_invalid'
        assert 'exact' in ' '.join(rejected.feedback)
        assert graph.query_idea_outcome_decision(rejected.submission_ref) == rejected
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM rg_idea_outcome_decisions WHERE quest_ref=:quest AND decision='accepted'"), {'quest': quest['quest_ref']}).scalar_one() == 0
        original = run
        # The Owner's normal rejection path keeps the Root/native session and
        # passes feedback to a new attempt; it never rewrites the sealed draft.
        corrected_source = _asset(runtime, source_kind + '-corrected')
        _origin(runtime, corrected_source, quest['quest_ref'], source_kind + '-corrected-origin')
        provider.source_ref = corrected_source.version_ref
        runtime.close()
        runtime = _runtime(tmp_path / source_kind, provider)
        for _ in range(12):
            assert runtime.idea_stage._process_cycle(cycle).advanced
            if runtime.owners.advancement_engine.query_idea_stage_commit(request.request_ref) is not None:
                break
        resumed = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
        assert resumed.status == 'completed'
        assert resumed.run_ref == original.run_ref
        assert resumed.root_session_ref == original.root_session_ref
        assert resumed.native_session_ref == original.native_session_ref
        assert resumed.runtime_binding == original.runtime_binding
        assert resumed.attempt_generation == original.attempt_generation + 1
        correction = provider.requests[-1]
        assert correction.owner_rejection_receipt_ref == rejected.receipt.receipt_ref
        assert correction.owner_feedback == rejected.feedback
        assert graph.query_idea_outcome_decision(resumed.execution.submission_ref).decision == 'accepted'
        assert graph.query_idea_outcome_decision(original.execution.submission_ref) == rejected
        assert original.execution.outcome['candidates'][0]['evidence_boundary']['accepted_evidence_refs'] != [corrected_source.version_ref]
    finally:
        runtime.close()
