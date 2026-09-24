"""Later-page Plan evidence survives the real RM/RG/AE handoff and restart.

The evidence authority and research providers are explicit test doubles. RM asset
intake, RG asset-role acceptance, FormalPlan acceptance, historical verification,
Bundle skip and Reasoning input closure use production Owners in a fresh tmp dir.
This test does not establish the validity of a real experiment's measurement.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import _with_derived_answer_contract_hash
from meta_research.target_commit_evidence import evidence_catalog_page_metadata

from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from test_public_bundle_stage import _DeterministicBundleSkill, _install_fixture_plan_evidence
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
)
from test_public_reasoning_plan_evidence_reuse import (
    _EvidenceReuseAuthority,
    _MetricReuseReasoningSkill,
    _finish_plan_and_skipped_bundle,
)


class _LaterPageAuthority(_EvidenceReuseAuthority):
    """Explicit authority double with an empty first discovery page."""

    def __init__(self) -> None:
        super().__init__()
        self.verifications: list[dict[str, object]] = []

    def query_plan_evidence_page(self, *, quest_ref, question_ref=None, offset=0, limit=32):
        if self.quest_ref is not None and quest_ref != self.quest_ref:
            raise OwnerConflict('fixture_evidence_quest_invalid')
        # An earlier candidate was scanned but was ineligible; the next page
        # contains our accepted fixture. This is a partial discovery page,
        # not a mutation of a Stage request after its creation.
        selected = self.catalog if offset == 1 else ()
        total = 2 if self.catalog else 0
        scanned = 1 if total and offset < total else 0
        return evidence_catalog_page_metadata(
            total=total, catalog=selected, scanned=scanned, offset=offset,
            limit=limit, question_ref=question_ref, projections=[],
        ), selected

    def verify_plan_evidence_catalog(
        self, *, quest_ref, evidence_catalog, expected_reference_revision,
        require_current=True, require_complete=True, selected_evidence_refs=None,
    ):
        if self.quest_ref is not None and quest_ref != self.quest_ref:
            raise OwnerConflict('fixture_evidence_quest_invalid')
        refs = {str(item['evidence_ref']) for item in evidence_catalog}
        known = {str(item['evidence_ref']): item for item in self.catalog}
        if (
            expected_reference_revision != len(evidence_catalog)
            or len(refs) != len(evidence_catalog)
            or any(known.get(str(item['evidence_ref'])) != item for item in evidence_catalog)
            or selected_evidence_refs is not None and not selected_evidence_refs.issubset(refs)
        ):
            raise OwnerConflict('fixture_evidence_catalog_invalid')
        self.verifications.append({
            'quest_ref': quest_ref,
            'refs': sorted(refs),
            'selected_refs': sorted(selected_evidence_refs or ()),
            'require_current': require_current,
        })


class _LaterPagePlanSkill(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=True)
        self.page_reader = None

    def _document(self, request):
        assert request.context_pack['evidence_catalog'] == []
        assert request.context_pack['evidence_reference_revision'] == 0
        assert request.context_pack['evidence_catalog_page']['next_offset'] == 1
        assert self.page_reader is not None
        page, catalog = self.page_reader(
            quest_ref=request.context_pack['accepted_question_binding']['quest_ref'],
            question_ref=request.question_ref, offset=1, limit=32,
        )
        assert page['next_offset'] is None
        assert len(catalog) == 1
        # Reuse only the fixture's document construction; the actual request
        # and every source binding remain the original empty-catalog closure.
        construction_context = {**request.context_pack, 'evidence_catalog': list(catalog)}
        document = super()._document(replace(request, context_pack=construction_context))
        document['additional_evidence_bindings'] = deepcopy(list(catalog))
        document = _with_derived_answer_contract_hash(document, request)
        assert 'additional_evidence_bindings' not in document
        assert document['source_bindings']['selected_evidence_catalog'] == list(catalog)
        assert document['source_bindings']['context_pack_hash'] == request.context_pack_hash
        return document


class _LaterPageReasoningSkill(_MetricReuseReasoningSkill):
    def review_draft(self, request, draft):
        return replace(
            super().review_draft(request, draft),
            review_mode='advisory_unobserved', reviewer_agent_ref=None,
        )


def _runtime(path: Path, authority, plan, reasoning):
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(path), proposal_drafter=drafting,
        intent_drafting_provider=drafting, host_compute_probe=_DeterministicProbe(),
        idea_skill_provider=_DeterministicIdeaSkill(), plan_skill_provider=plan,
        bundle_skill_provider=_DeterministicBundleSkill(),
        reasoning_skill_provider=reasoning, target_commit_evidence_authority=authority,
        harness_adapters=(_FullConformanceAdapter('codex'), _FullConformanceAdapter('claude')),
    )
    plan.page_reader = runtime.owners.research_graph.query_plan_evidence_page
    if runtime.harnesses.query_status()['status'] != 'ready':
        runtime.harnesses.start_full_conformance(_full_request())
        for _ in range(4):
            if runtime.harnesses.query_status()['status'] == 'ready':
                break
            assert runtime.harnesses.advance_full_conformance(mcp_base_url='http://127.0.0.1:8765'), runtime.harnesses.query_status()
        assert runtime.harnesses.query_status()['status'] == 'ready'
    return runtime


def test_later_page_evidence_survives_formal_plan_memory_history_and_reasoning(tmp_path):
    data_root = tmp_path / 'later-page-evidence'
    authority = _LaterPageAuthority()
    plan = _LaterPagePlanSkill()
    reasoning = _LaterPageReasoningSkill()
    runtime = _runtime(data_root, authority, plan, reasoning)
    try:
        quest = _confirm_direct_quest(runtime)
        _install_fixture_plan_evidence(runtime, authority, quest_ref=str(quest['quest_ref']))
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)

        bundle_request = runtime.owners.advancement_engine.query_bundle_stage_request(str(quest['cycle_ref']))
        assert bundle_request is not None and bundle_request.accepted_formal_plan is not None
        accepted = bundle_request.accepted_formal_plan
        selected = accepted.plan_document['source_bindings']['selected_evidence_catalog']
        assert selected == list(authority.catalog)
        assert accepted.plan_document['source_bindings']['evidence_reference_revision'] == 0
        assert plan.requests[0].context_pack['evidence_catalog'] == []
        assert plan.requests[0].context_pack_hash == canonical_hash(plan.requests[0].context_pack)

        submission_ref = runtime.plan_stage.query_current()['run']['submission_ref']
        decision = runtime.owners.research_graph.query_formal_plan_decision(submission_ref)
        assert decision is not None and decision.formal_plan_ref == accepted.formal_plan_ref
        memory = runtime.owners.research_memory.query_plan_document(submission_ref)
        assert memory is not None
        assert memory.plan_document == accepted.plan_document
        assert memory.plan_document['source_bindings']['selected_evidence_catalog'] == selected

        leaves = runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
            quest_ref=str(quest['quest_ref']), accepted_formal_plan=accepted,
        )
        assert leaves and {leaf.evidence_ref for leaf in leaves} == {selected[0]['evidence_ref']}
        forged = deepcopy(accepted.plan_document)
        forged['source_bindings']['selected_evidence_catalog'][0]['content_hash'] = '0' * 64
        with pytest.raises(OwnerConflict, match='bundle_formal_plan_binding_invalid'):
            runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
                quest_ref=str(quest['quest_ref']), accepted_formal_plan=replace(accepted, plan_document=forged),
            )
        assert any(call['refs'] and call['require_current'] for call in authority.verifications)
        assert any(call['refs'] and not call['require_current'] for call in authority.verifications)

        assert runtime.reasoning_stage.process_once()
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(str(quest['cycle_ref']))
        assert request is not None
        frozen_leaves = request.context_pack['plan_evidence_input']['evidence_reuse_closure']
        assert [leaf['role'] for leaf in frozen_leaves] == ['MetricResult', 'CheckpointArtifact', 'LogAsset', 'AnalysisAsset']
        assert {leaf['evidence_ref'] for leaf in frozen_leaves} == {selected[0]['evidence_ref']}
        for _ in range(8):
            current = runtime.reasoning_stage.query_current()
            if current['reasoning_acceptance']['status'] == 'accepted':
                break
            assert runtime.reasoning_stage.process_once()
        else:
            raise AssertionError(f'Reasoning did not accept reused later-page evidence: {current["reasoning_acceptance"]}')
        assert current['reasoning_acceptance']['disposition'] == 'affirmed'
    finally:
        runtime.close()

    restarted_authority = _LaterPageAuthority()
    restarted_authority.quest_ref = authority.quest_ref
    restarted_authority.catalog = authority.catalog
    restarted = _runtime(data_root, restarted_authority, _LaterPagePlanSkill(), _LaterPageReasoningSkill())
    try:
        recovered = restarted.owners.advancement_engine.query_reasoning_stage_request(str(quest['cycle_ref']))
        assert recovered is not None
        assert recovered.context_pack == request.context_pack
        assert restarted.reasoning_stage.query_current()['reasoning_acceptance']['disposition'] == 'affirmed'
    finally:
        restarted.close()
