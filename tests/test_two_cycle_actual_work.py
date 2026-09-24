"""Two real Owner cycles: executed work, accepted synthesis, then exact reuse.

Only provider drafts, CLI completion observation and explicit test-human actions
are deterministic. No receipt authority or durable Owner method is replaced.
"""
from dataclasses import replace
import json
from pathlib import Path

from sqlalchemy import text
from meta_research.owners.common import canonical_hash
from meta_research.plan_skill import _with_derived_answer_contract_hash
from meta_research.research_content import discover_questions, discover_literature, read_content
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_formal_run_snapshots import _scenario
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _CurrentBindingBundleSkill
from test_public_plan_stage import _finish_idea_stage
from test_public_bundle_stage import _finish_plan_stage
from test_public_reasoning_stage import (_reasoning_runtime, _confirm_deepfetch_quest,
    _MultiRunIdeaSkill, _MultiRunPlanSkill, _MultiRunReasoningSkill)
from test_public_manual_question_lifecycle import _open_and_confirm_seed, QUESTION


class _Plan(_MultiRunPlanSkill):
    def __init__(self):
        super().__init__(no_gap=False)
        self.runtime=None
        self.discoveries=[]

    def _document(self,request):
        graph=self.runtime.owners.research_graph;memory=self.runtime.owners.research_memory
        quest=request.context_pack['accepted_question_binding']['quest_ref']
        questions=discover_questions(graph,memory,quest_ref=quest)
        found=[]
        for item in questions['items']:
            history=graph.query_question_research_history(quest_ref=quest,question_ref=item['question_ref'])
            for outcome in history['items']:
                # History gives an exact accepted decision. The body supplies its
                # immutable scientific source identity, which Plan actually uses.
                body=graph.read_question_scientific_outcome(quest_ref=quest,
                    question_ref=item['question_ref'],outcome_ref=outcome['source_ref'])
                found.append(body)
        if not found:return super()._document(request)
        source=found[0];ref=source['outcome_ref']
        read=read_content(graph,memory,quest_ref=quest,source_ref=ref,version_ref=ref)
        assert ref in read['text']
        baselines=graph.query_baselines(quest_ref=quest)
        datasets=graph.query_datasets(quest_ref=quest)
        assert baselines['items'] and datasets['items']
        dataset=datasets['items'][0]
        versions=graph.query_datasets(quest_ref=quest,dataset_ref=dataset['dataset_ref'])
        version=versions['items'][0]
        binding=version['asset_bindings'][0]
        data=read_content(graph,memory,quest_ref=quest,source_ref=version['dataset_version_ref'],
            version_ref=binding['version_ref'])
        assert '36' in data['text']
        environments=graph.query_environments(quest_ref=quest)
        environment,=environments['items']
        environment_binding=environment['asset_bindings'][0]
        environment_read=read_content(graph,memory,quest_ref=quest,
            source_ref=environment['environment_ref'],version_ref=environment_binding['version_ref'])
        assert environment_binding==binding and environment_read['text']==data['text']
        self.discoveries.append({'question':questions,'source_ref':ref,'baseline':baselines,
            'dataset':version,'outcome_read':read,'dataset_read':data,
            'environment':environment,'environment_read':environment_read})
        entry={'schema_ref':'meta-research/evidence-source-ref/v1','evidence_ref':ref,
            'source_kind':'ScientificOutcome','source_ref':ref}
        self.no_gap=True
        document=super()._document(replace(request,context_pack={**request.context_pack,'evidence_catalog':[entry]}))
        document['additional_evidence_bindings']=[entry]
        document['coverage'][0]['evidence_uses'][0]['supported_claim']='The previous accepted synthesis reports actual bounded work and preserved outputs.'
        document['coverage'][0]['evidence_uses'][0]['support_boundary']='Reuse of the accepted result; this cycle does not claim new execution.'
        return _with_derived_answer_contract_hash(document,request)


class _Reasoning(_MultiRunReasoningSkill):
    def review_draft(self,request,draft):
        return replace(super().review_draft(request,draft),review_mode='advisory_unobserved',reviewer_agent_ref=None)

    def _result_parts(self,request):
        outcome,next_cycle=super()._result_parts(request)
        evidence=next((leaf for leaf in request.frozen_evidence_closure
                       if leaf['kind'] in {'MetricResult','ScientificOutcome'}),None)
        assert evidence is not None,request.frozen_evidence_closure
        outcome['evidence'].append({'kind':evidence['kind'],'ref':evidence['ref'],'finding':'supporting'})
        outcome['claim']='The literature and actual retained execution result jointly support a bounded observation.'
        return outcome,next_cycle


def _sibling(runtime,quest):
    human=runtime.owners.human_collaboration
    seeded=_open_and_confirm_seed(human,quest_ref=quest['quest_ref'],parent_question_ref=quest['question_ref'],
        key_prefix='t13-sibling',deepfetch_preference='use')
    context=seeded['context_ref']
    human.start_manual_creation_deepfetch(context,expected_seed_ref=seeded['seed']['ref'],
        expected_seed_hash=seeded['seed']['hash'],idempotency_key='t13-sibling-deepfetch')
    assert runtime.deepfetch.process_once()
    researched=human.query_manual_question_creation(context)
    saved=human.save_manual_question_proposal(context,content=dict(QUESTION),
        expected_basis_hash=researched['research_path']['basis_hash'],idempotency_key='t13-sibling-proposal')
    human.confirm_manual_question_proposal(context,proposal_ref=saved['proposal']['ref'],
        proposal_hash=saved['proposal']['hash'],idempotency_key='t13-sibling-confirm')
    for _ in range(8):
        manual=human.query_manual_question_creation(context)
        if manual['status']=='completed':break
        assert human.reconcile_once()
    assert manual['status']=='completed'
    sibling=runtime.owners.research_graph.query_question_by_ref(manual['question_anchor']['question_ref'])
    snapshot=runtime.owners.research_memory.query_literature_snapshot(researched['research_path']['deepfetch']['snapshot_ref'])
    runtime.owners.research_memory.ensure_question_literature_revision(question_binding=sibling.as_binding(),
        source_snapshot_binding=snapshot.as_context_binding(),idempotency_key='t13-sibling-literature')
    return sibling


def _ready_existing(runtime):
    for _ in range(20):
        assert runtime.bundle_stage.process_once(),runtime.bundle_stage.transient_error
        current=runtime.bundle_stage.query_current()
        request=current['stage_run_request']
        if not request:continue
        run=runtime.owners.agent_runtime.query_bundle_stage_run(request['request_ref'])
        if run is None:continue
        decisions=runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        if not decisions:continue
        graph=runtime.owners.research_graph.query_target_graph(run.request_ref)
        target=next(t for t in graph.targets if t.target_ref==decisions[-1].selected_target_ref)
        if runtime.owners.research_graph.query_target_candidate_projection(target_ref=target.target_ref) is None:continue
        launch=runtime.owners.research_graph.query_target_launch_request(target.target_ref)
        return graph,target,run,decisions[-1],launch
    raise AssertionError(runtime.bundle_stage.query_current())


def _finish_stage(runtime,name):
    stage=getattr(runtime,name+'_stage')
    expected_cycle=runtime.owners.advancement_engine.query_active_foregrounds()[0]['cycle_ref']
    for _ in range(30):
        request=getattr(runtime.owners.advancement_engine,'query_'+name+'_stage_request')(expected_cycle)
        commit=None if request is None else getattr(runtime.owners.advancement_engine,'query_'+name+'_stage_commit')(request.request_ref)
        if commit is not None:
            return {'stage_run_request':{'request_ref':request.request_ref,'cycle_ref':expected_cycle},
                'stage_commit':{'commit_ref':commit.commit_ref,'outcome_ref':commit.outcome_ref,
                    'outcome_kind':commit.outcome_kind,'receipt':commit.receipt.as_public_dict()}}
        state=stage.query_current()
        request=state.get('stage_run_request')
        if state['stage_commit'] is not None and request and request['cycle_ref']==expected_cycle:return state
        assert stage.process_once(),(stage.transient_error,state)
        rejection=stage.query_current().get('run') or {}
        assert not rejection.get('completion_rejection'),rejection.get('completion_rejection')
    raise AssertionError(stage.query_current())


def test_two_cycles_publish_actual_run_reuse_synthesis_and_rediscover_libraries(tmp_path):
    plan=_Plan();reasoning=_Reasoning(entry_stage='idea')
    runtime=_reasoning_runtime(tmp_path/'t13',reasoning_skill=reasoning,
        idea_skill=_MultiRunIdeaSkill(),plan_skill=plan,bundle_skill=_CurrentBindingBundleSkill())
    plan.runtime=runtime
    try:
        quest=_confirm_deepfetch_quest(runtime)
        sibling=_sibling(runtime,quest)
        reasoning.target_question_ref=sibling.question_ref
        _finish_idea_stage(runtime)
        first_plan=_finish_plan_stage(runtime)
        ready=_ready_existing(runtime)
        runtime,lifecycle,memory,handle,evidence,_=_scenario(tmp_path,runtime=runtime,ready=ready)
        finalizer=TargetRunFinalizer(lifecycle=lifecycle,memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,evidence_reader=_SystemEvidenceReader(),
            measurement_authority=runtime.owners.research_graph,graph_authority=runtime.owners.research_graph)
        completed=finalizer.finalize(handle=handle,evidence=evidence)
        assert completed.status=='completed'
        runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
            completion_ref=completed.completion_ref,target_commit_ref=completed.target_commit_ref)
        manifest=memory.query(completed.manifest_ref)
        graph=runtime.owners.research_graph
        binding=next(e.binding for e in manifest.entries if e.declared_relative_path=='outputs/data/run1.txt')
        dataset=graph.register_dataset(semantic_key='t13:square-observation',name='Actual square observation',
            meaning='Output of the first executed method snapshot.',metadata={},notes='Generated by actual Python execution.',idempotency_key='t13-dataset')
        version=graph.register_dataset_version(dataset_ref=dataset['dataset_ref'],version_label='first',
            meaning='Six squared equals thirty-six.',asset_bindings=[binding],notes='One bounded observation.',idempotency_key='t13-version')
        graph.reference_dataset(dataset_version_ref=version['dataset_version_ref'],question_ref=quest['question_ref'],
            purpose='Retain the actual execution observation.',notes='Current Question input for later discovery.',idempotency_key='t13-reference')
        environment=graph.register_environment(semantic_key='t13:retained-simulation',
            name='Retained simulation fixture',meaning='A reusable exact fixture from the completed work.',
            source=completed.target_commit_ref,asset_bindings=[binding],quest_ref=quest['quest_ref'],
            idempotency_key='t13-environment')
        graph.reference_environment(environment_ref=environment['environment_ref'],question_ref=quest['question_ref'],
            purpose='Reuse the retained simulation fixture.',idempotency_key='t13-environment-use')
        first_bundle=_finish_stage(runtime,'bundle')
        first_reasoning=_finish_stage(runtime,'reasoning')
        first_request=first_reasoning['stage_run_request']['request_ref']
        first_commit=runtime.owners.advancement_engine.query_reasoning_stage_commit(first_request)
        assert first_commit.outcome_receipt is not None
        foreground=runtime.owners.advancement_engine.query_foreground(quest['quest_ref'])
        assert foreground['cycle_ref']!=quest['cycle_ref'] and foreground['question_ref']==sibling.question_ref
        _finish_stage(runtime,'idea')
        second_plan=_finish_stage(runtime,'plan')
        second_bundle=_finish_stage(runtime,'bundle')
        accepted=runtime.owners.advancement_engine.query_bundle_stage_request(foreground['cycle_ref']).accepted_formal_plan
        second_reasoning=_finish_stage(runtime,'reasoning')
        assert plan.discoveries
        second_request=second_reasoning['stage_run_request']['request_ref']
        second_commit=runtime.owners.advancement_engine.query_reasoning_stage_commit(second_request)
        assert second_commit.outcome_receipt is not None
        source=plan.discoveries[0]['source_ref']
        leaves=graph.resolve_plan_evidence_reuse_leaves(quest_ref=quest['quest_ref'],accepted_formal_plan=accepted)
        assert len(leaves)==1 and leaves[0].role=='ScientificOutcome' and leaves[0].evidence_ref==source
        assert leaves[0].source_binding['owner_acceptance_receipt_ref']==first_commit.outcome_receipt.receipt_ref
        from fastapi.testclient import TestClient
        from meta_research.web import create_app
        client=TestClient(create_app(runtime,base_url='http://testserver',control_key='isolated-history-test'))
        auth=client.post('/auth/bootstrap',headers={'Origin':'http://testserver'},
            json={'token':runtime.authentication.issue_bootstrap_token()})
        assert auth.status_code==200
        history=client.get('/api/v1/research-library/questions',params={
            'quest_ref':quest['quest_ref'],'question_ref':quest['question_ref']})
        assert history.status_code==200,history.text
        item=next(item for item in history.json()['items'] if item['source_ref']==first_commit.outcome_ref)
        assert item['summary'] and item['reader']['version_ref']==item['source_ref']
        outcome_body=client.get('/api/v1/research-content',params={'quest_ref':quest['quest_ref'],**item['reader']})
        assert outcome_body.status_code==200,outcome_body.text
        assert 'claim' in outcome_body.json()['text']
        library=discover_literature(graph,runtime.owners.research_memory,quest_ref=quest['quest_ref'])
        shared=next(item for item in library['items'] if {j['question_ref'] for j in item['judgments']} >= {quest['question_ref'],sibling.question_ref})
        for judgment in shared['judgments']:
            page=read_content(graph,runtime.owners.research_memory,quest_ref=quest['quest_ref'],**judgment['reader'])
            assert page['text']
        with runtime._database.read() as c:
            counts={table:c.exec_driver_sql('SELECT count(*) FROM '+table).scalar_one() for table in (
                'rm_idea_outcome_contents','rm_plan_documents','rg_target_commits','rg_variant_runs',
                'rg_evaluation_attempts','rm_reasoning_contents','rg_reasoning_outcome_decisions')}
            assert c.exec_driver_sql('PRAGMA integrity_check').scalar_one()=='ok'
            assert c.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
        assert all(counts.values()) and counts['rm_plan_documents']==2 and counts['rm_reasoning_contents']==2
        report={'quest_ref':quest['quest_ref'],'question_refs':[quest['question_ref'],sibling.question_ref],
            'cycle_refs':[quest['cycle_ref'],foreground['cycle_ref']],'target_commit_ref':completed.target_commit_ref,
            'manifest_ref':manifest.manifest_ref,'accepted_outcomes':[first_commit.outcome_ref,second_commit.outcome_ref],
            'plan_selected_source':source,'dataset_version_ref':version['dataset_version_ref'],
            'environment_ref':environment['environment_ref'],'history_api_readback':True,
            'shared_literature_ref':shared['record_ref'],'counts':counts,'stage_commits':{
                'first_plan':first_plan['stage_commit'],'first_bundle':first_bundle['stage_commit'],
                'first_reasoning':first_reasoning['stage_commit'],'second_plan':second_plan['stage_commit'],
                'second_bundle':second_bundle['stage_commit'],'second_reasoning':second_reasoning['stage_commit']}}
        print('T13_EVIDENCE '+json.dumps(report,ensure_ascii=False,sort_keys=True))
    finally:runtime.close()
