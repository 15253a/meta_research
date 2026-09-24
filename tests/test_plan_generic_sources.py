"""A real Plan Owner chain adopts exact human and asset sources without fake Runs."""
from dataclasses import replace
import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.plan_skill import _with_derived_answer_contract_hash, _plan_document_schema
from meta_research.reasoning_contract import plan_evidence_reuse_leaves
from meta_research.research_content import read_content, discover_literature
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from test_public_plan_stage import _DeterministicPlanSkill, _DeterministicIdeaSkill, _runtime, _confirm_direct_quest, _finish_idea_stage


def _finish_plan_and_skipped_bundle(runtime):
    for _step in range(16):
        if runtime.plan_stage.query_current()['stage_commit'] is not None:
            break
        assert runtime.plan_stage.process_once()
    else:
        raise AssertionError('Plan did not complete')
    for _step in range(8):
        if runtime.bundle_stage.query_current()['stage_commit'] is not None:
            return
        assert runtime.bundle_stage.process_once()
    raise AssertionError('No-gap Bundle did not skip')


class _GenericPlan(_DeterministicPlanSkill):
    def __init__(self, kind):
        super().__init__(no_gap=True)
        self.kind=kind
        self.runtime=None
        self.reads=[]

    def _document(self, request):
        runtime=self.runtime;graph=runtime.owners.research_graph;memory=runtime.owners.research_memory
        quest=request.context_pack['accepted_question_binding']['quest_ref']
        if self.kind=='HumanInput':
            page=runtime.owners.human_collaboration.query_research_inputs(quest_ref=quest,query='Test user')
            item=page['items'][0];ref=item['input_ref'];reader=item['reader']
        elif self.kind=='LiteratureSnapshot':
            page=discover_literature(graph,memory,quest_ref=quest)
            ref=page['items'][0]['reader']['version_ref']
            revision=memory.query_question_literature_revision_ref(question_ref=page['items'][0]['question_ref'],revision_ref=ref)
            ref=revision['literature_snapshot_ref']
            allowed=_plan_document_schema(request)['properties']['additional_evidence_bindings']['items']['anyOf'][1]['properties']['source_kind']['enum']
            assert self.kind in allowed
            reader={'source_ref':ref,'version_ref':ref}
        else:
            candidates=[memory.query_asset_version(role.version_ref) for role in graph.query_asset_roles(quest_ref=quest)]
            selected=next(asset for asset in candidates if asset.display_name=='Plan fixture observation')
            ref=selected.version_ref;reader={'source_ref':ref,'version_ref':ref}
        read=read_content(graph,memory,quest_ref=quest,human_collaboration=runtime.owners.human_collaboration,**reader)
        assert read['text']
        self.reads.append(read)
        entry={'schema_ref':'meta-research/evidence-source-ref/v1','evidence_ref':ref,'source_kind':self.kind,'source_ref':ref}
        # Only the fixture document builder sees its selected source as a page;
        # the accepted request remains unchanged and Owner verifies the real ref.
        construction={**request.context_pack,'evidence_catalog':[entry]}
        document=super()._document(replace(request,context_pack=construction))
        document['additional_evidence_bindings']=[entry]
        document['coverage'][0]['evidence_uses'][0]['supported_claim']='The source records the observation and its context.'
        document['coverage'][0]['evidence_uses'][0]['support_boundary']='A reported observation; no causal or experimental claim.'
        return _with_derived_answer_contract_hash(document,request)


@pytest.mark.parametrize('kind',['HumanInput','AssetVersion','LiteratureSnapshot'])
def test_generic_source_discovery_read_formal_plan_and_rebuilt_reasoning_closure(tmp_path,kind):
    plan=_GenericPlan(kind)
    if kind=='LiteratureSnapshot':
        from test_public_reasoning_stage import _reasoning_runtime, _confirm_deepfetch_quest, _DeterministicReasoningSkill
        runtime=_reasoning_runtime(tmp_path/'generic-plan',reasoning_skill=_DeterministicReasoningSkill(),idea_skill=_DeterministicIdeaSkill(),plan_skill=plan)
    else:
        runtime=_runtime(tmp_path/'generic-plan',idea_skill=_DeterministicIdeaSkill(),plan_skill=plan)
    plan.runtime=runtime
    try:
        quest=(_confirm_deepfetch_quest(runtime) if kind=='LiteratureSnapshot' else _confirm_direct_quest(runtime))
        if kind=='HumanInput':
            # Explicit test-user submission exercises the same HC Owner as the authenticated UI.
            runtime.owners.human_collaboration.submit_research_input(quest_ref=quest['quest_ref'],
                question_ref=quest['question_ref'],text_content='Test user: observation was made only under the stated lighting; interpretation outside it is unknown.',
                idempotency_key='test-user-plan-input')
        elif kind=='AssetVersion':
            asset=runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
                source_kind='text',custody_mode='managed',display_name='Plan fixture observation',
                content=b'Test observation: the structure was visible under the stated lighting.\n'),
                idempotency_key='plan-observation').asset
            runtime.owners.research_graph.accept_asset_role(binding=asset.as_binding(),role='quest_source_material',
                quest_ref=quest['quest_ref'],idempotency_key='plan-observation-scope')
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)
        request=runtime.owners.advancement_engine.query_bundle_stage_request(quest['cycle_ref'])
        accepted=request.accepted_formal_plan
        assert plan.reads
        submission=runtime.plan_stage.query_current()['run']['submission_ref']
        stored=runtime.owners.research_memory.query_plan_document(submission)
        assert stored.plan_document==accepted.plan_document
        selected=accepted.plan_document['source_bindings']['selected_evidence_catalog']
        assert selected[0]['source_kind']==kind
        leaves=runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
            quest_ref=quest['quest_ref'],accepted_formal_plan=accepted)
        assert len(leaves)==1 and leaves[0].role==kind
        leaf=leaves[0].as_public_dict()
        assert leaf['source_variant_run_ref'] is leaf['target_commit_ref'] is leaf['evidence_asset_receipt'] is None
        normalized=plan_evidence_reuse_leaves({'plan_evidence_input':{'kind':'accepted',
            'formal_plan_binding':accepted.as_dict(),'evidence_reuse_set':accepted.plan_document['evidence_reuse_set'],
            'evidence_reuse_closure':[leaf]}})
        assert normalized==[leaf['source_binding']]
        assert runtime.owners.research_graph.resolve_plan_evidence_reuse_leaves(
            quest_ref=quest['quest_ref'],accepted_formal_plan=accepted)==leaves
        with pytest.raises(OwnerConflict):
            TargetCommitEvidenceCatalog(runtime.owners.research_graph,runtime.owners.research_memory).verify_plan_evidence_catalog(
                quest_ref='foreign-quest',evidence_catalog=selected,expected_reference_revision=1,
                require_current=False,require_complete=False)
    finally:
        runtime.close()


def test_initial_snapshot_requires_verified_question_adoption(tmp_path):
    from sqlalchemy import text
    from test_public_reasoning_stage import _reasoning_runtime, _confirm_deepfetch_quest, _DeterministicReasoningSkill
    runtime=_reasoning_runtime(tmp_path/'initial-snapshot',reasoning_skill=_DeterministicReasoningSkill())
    try:
        quest=_confirm_deepfetch_quest(runtime)
        memory=runtime.owners.research_memory;graph=runtime.owners.research_graph
        record=discover_literature(graph,memory,quest_ref=quest['quest_ref'])['items'][0]
        revision=memory.query_question_literature_revision_ref(question_ref=record['question_ref'],revision_ref=record['reader']['version_ref'])
        ref=revision['literature_snapshot_ref']
        assert memory.query_literature_snapshot(ref).quest_ref is None
        assert graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest['quest_ref'],ref=ref)['kind']=='LiteratureSnapshot'
        assert graph.resolve_reasoning_historical_evidence_leaf(quest_ref='foreign-quest',ref=ref) is None
        with memory._database.write() as connection:
            connection.execute(text("UPDATE rm_question_literature_revisions SET receipt_hash=:bad WHERE revision_ref=:ref"),{'bad':'0'*64,'ref':revision['revision_ref']})
        with pytest.raises(OwnerConflict):
            graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest['quest_ref'],ref=ref)
    finally:
        runtime.close()
