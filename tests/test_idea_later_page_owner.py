"""Independent Idea later-page acceptance through fresh production Owners.

The research Provider is deterministic. Asset intake/custody, evidence roles,
Idea content/domain acceptance and subsequent receipt reads use actual Owners.
"""
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import text
from meta_research.owners.common import canonical_hash, OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_idea_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill
from test_public_plan_stage import _finish_idea_stage
from conftest import _isolate_platform_power_dependency


class LaterPageIdea(_DeterministicIdeaSkill):
    selected_ref=None
    page_reader=None

    def generate_draft(self,request):
        assert request.context_pack['schema_ref'].endswith('/v4')
        assert self.selected_ref not in request.context_pack['accepted_evidence_refs']
        page,refs=self.page_reader(request.context_pack['accepted_question_binding']['quest_ref'],offset=32,limit=32)
        assert self.selected_ref in refs and page['next_offset'] is None
        draft=super().generate_draft(request)
        value=deepcopy(draft.draft)
        value['candidates'][0]['evidence_boundary']['accepted_evidence_refs']=[self.selected_ref]
        return replace(draft,draft=value)


def add_evidence(runtime,quest_ref,index):
    result=runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind='text',custody_mode='managed',display_name=f'accepted-evidence-{index}.json',
        media_type='application/json',content=('{"observation":'+str(index)+'}').encode(),
        provenance={'test_source':'bounded-idea-discovery'}),idempotency_key=f'idea-evidence-{index}')
    assert result.asset is not None
    runtime.owners.research_graph.accept_asset_role(binding=result.asset.as_binding(),
        role='evidence',quest_ref=quest_ref,idempotency_key=f'idea-evidence-role-{index}')
    return result.asset


def test_later_page_idea_is_accepted_and_old_binding_survives_new_evidence(tmp_path):
    provider=LaterPageIdea()
    runtime=_runtime(tmp_path/'later-page-idea',provider)
    try:
        quest=_confirm_direct_quest(runtime)
        assets=[add_evidence(runtime,quest['quest_ref'],index) for index in range(33)]
        provider.selected_ref=assets[0].version_ref
        provider.page_reader=runtime.owners.research_graph.query_evidence_reference_page
        current=_finish_idea_stage(runtime)
        request=provider.requests[0]
        assert len(request.context_pack['accepted_evidence_refs'])==32
        assert request.context_pack['evidence_page']['total_count']==33
        assert assets[0].version_ref not in request.context_pack['accepted_evidence_refs']
        submission=current['run']['submission_ref']
        content=runtime.owners.research_memory.query_idea_outcome_content(submission)
        assert content.outcome['candidates'][0]['evidence_boundary']['accepted_evidence_refs']==[assets[0].version_ref]
        decision=runtime.owners.research_graph.query_idea_outcome_decision(submission)
        assert decision.decision=='accepted'
        original_hash=request.context_pack_hash
        add_evidence(runtime,quest['quest_ref'],34)
        assert runtime.owners.research_graph.query_idea_outcome_decision(submission)==decision
        assert runtime.owners.research_memory.query_idea_outcome_content(submission)==content
        frozen=runtime.owners.advancement_engine.query_idea_stage_request(quest['cycle_ref'])
        assert frozen.context_pack_hash==original_hash==canonical_hash(frozen.context_pack)

        # This is the exact verifier used by Idea domain admission and re-read;
        # an otherwise valid accepted version cannot acquire a foreign Quest.
        verifier=runtime.owners.research_graph._receipt_verifier
        with pytest.raises(OwnerConflict):
            verifier.verify_evidence_refs(quest_ref='foreign-quest',version_refs=(assets[0].version_ref,),require_current=False)

        # Isolated DB corruption must invalidate historical receipt verification,
        # even though the Idea outcome still refers to the same stable version.
        database=runtime.owners.research_graph._database
        with database.write() as connection:
            connection.execute(text('UPDATE rm_asset_versions SET content_hash=:bad WHERE version_ref=:ref'),
                {'bad':'0'*64,'ref':assets[0].version_ref})
        with pytest.raises(OwnerConflict):
            runtime.owners.research_graph.query_idea_outcome_decision(submission)
    finally:
        runtime.close()
