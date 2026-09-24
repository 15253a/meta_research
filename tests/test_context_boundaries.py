from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import hashlib
import json
import pytest

from meta_research.context_presentation import (
    bounded_text, context_read_page, literature_reference, scientific_handoff,
    stage_context_view, CONTEXT_VIEW_MAX_BYTES, reasoning_handoff_reference,
)
from meta_research.idea_contract import validate_idea_context_pack, IdeaContractError
from meta_research.owners.common import canonical_hash, canonical_json, OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError, SemanticMcpGateway
from meta_research.stage_context_access import read_stage_context, stage_context_operations

SNAPSHOT=Path(__file__).parent.parent/'trajectory-before.json'

@pytest.fixture(scope='module')
def snapshot():
    return json.loads(SNAPSHOT.read_text())['tables']

def packs(snapshot):
    return [json.loads(r['context_pack_json']) for r in snapshot['ae_stage_run_requests']]


@pytest.mark.parametrize('count',[100,1000])
def test_history_growth_does_not_expand_provider_view(snapshot,count):
    pack=deepcopy(next(p for p in packs(snapshot) if 'research_context' in p))
    graph=pack['research_context']['graph_binding']
    graph['prior_current_question_outcomes']=[{'cycle_ref':f'cycle_{i:06}',
        'outcome_ref':f'outcome_{i:06}','disposition':'uncertain'} for i in range(count)]
    graph['active_question_refs']=[f'question_{i:06}' for i in range(count)]
    pack['question_literature_input']['binding']['records']=[{'ref':f'paper_{i:06}','title':'long title '*400} for i in range(count)]
    before=canonical_hash(pack)
    view=stage_context_view('reasoning',pack,context_pack_ref='ctx',context_pack_hash=before)
    encoded=canonical_json(view).encode()
    assert len(encoded)<=CONTEXT_VIEW_MAX_BYTES
    assert canonical_hash(pack)==before
    assert view['summary_only'] is True
    assert b'outcome_000999' not in encoded
    assert b'"total_count":'+str(count).encode() in encoded
    # At most twelve outcomes plus navigation metadata regardless of total.
    history=view['sections']['research_context']['graph_binding']['prior_current_question_outcomes']
    assert history['shown_count']<=12 and history['truncated']


def test_100_to_1000_changes_only_total_metadata(snapshot):
    sizes=[]
    base=next(p for p in packs(snapshot) if 'research_context' in p)
    for count in (100,1000):
        pack=deepcopy(base)
        pack['research_context']['graph_binding']['prior_current_question_outcomes']=[{'outcome_ref':f'outcome_{i:06}'} for i in range(count)]
        sizes.append(len(canonical_json(stage_context_view('reasoning',pack,context_pack_ref='ctx',context_pack_hash='a'*64)).encode()))
    assert abs(sizes[1]-sizes[0])<=2


@pytest.mark.parametrize('count',[100,1000])
def test_v4_frozen_literature_is_bounded_and_not_falsely_complete(snapshot,count):
    old=next(p for p in packs(snapshot) if p.get('schema_ref','').endswith('idea-context-pack/v3'))
    full=deepcopy(old['literature_binding'])
    full['records']=[{'ref':f'paper_{i:06}','evidence_basis':'title_lead','evidence_basis_ref':f'paper_{i:06}'} for i in range(count)]
    reference=literature_reference(full)
    assert reference['record_count']==count
    assert reference['records_preview']['shown_count']<=24
    assert reference['records_preview']['truncated'] is True
    assert reference['records_hash']==canonical_hash(full['records'])
    refs=[f'asset_{i:06}' for i in range(32)]
    pack={**old,'schema_ref':'meta-research/idea-context-pack/v4','accepted_evidence_refs':refs,
        'prior_accepted_bindings':[],'literature_binding':reference,'evidence_reference_revision':count,
        'evidence_page':{'schema_ref':'meta-research/evidence-reference-page/v1','total_count':count,
            'shown_count':32,'offset':0,'limit':32,'next_offset':32,'selection':'recent_quest_evidence',
            'complete':False,'references_hash':canonical_hash(refs)}}
    assert validate_idea_context_pack(pack,cycle_ref=pack['cycle_ref'],accepted_question_binding=pack['accepted_question_binding'])==set(refs)
    assert len(canonical_json(pack).encode())<10000
    forged=deepcopy(pack);forged['evidence_page']['complete']=True
    with pytest.raises(IdeaContractError):
        validate_idea_context_pack(forged,cycle_ref=forged['cycle_ref'],accepted_question_binding=forged['accepted_question_binding'])


def test_scientific_handoff_preserves_claim_scope_and_is_explicit_excerpt():
    outcome={'disposition':'uncertain','claim':'some evidence', 'support_scope':'this cohort',
        'limitations':['尚缺数据'*1000],'notes':'unrelated recovery logs '*10000}
    view=scientific_handoff(outcome,source_ref='outcome_1',next_action='obtain missing cohort')
    assert view['claim']['text']=='some evidence'
    assert view['support_scope']['text']=='this cohort'
    assert view['limitations']['truncated']
    assert 'recovery' not in canonical_json(view)
    assert len(canonical_json(view).encode())<8000


def auth_objects(pack):
    context=NS(root_kind='idea',run_ref='run',attempt_ref='attempt',root_session_ref='session',fence_ref='fence',capability_binding_hash='binding')
    question=NS(quest_ref='quest',question_ref='question',content_ref='question_content',content_hash='qhash')
    request=NS(request_ref='request',cycle_ref='cycle',context_pack=pack,context_pack_ref='ctx',context_pack_hash=canonical_hash(pack),accepted_question=question)
    run=NS(run_ref='run',attempt_ref='attempt',root_session_ref='session',fence_ref='fence',runtime_binding_hash='binding')
    ar=NS(verify_root_agent_runtime_scope=Mock(),query_managed_run=Mock(return_value={'run_kind':'idea_stage','cycle_ref':'cycle'}),query_idea_stage_run=Mock(return_value=run))
    ae=NS(query_idea_stage_request=Mock(return_value=request))
    return context,ar,ae


def test_reader_pages_reconstruct_exact_unicode_bytes_and_hash():
    pack={'rows':[{'text':'研究证据α😀'*1000}]}
    ctx,ar,ae=auth_objects(pack);raw=canonical_json(pack).encode();chunks=[];offset=0
    while True:
        out=read_stage_context(ae,ar,None,None,ctx,{'context_pack_ref':'ctx','source':'context_pack','path':[],'offset':offset,'limit':127})
        assert out['returned_bytes']<=127 and out['content_hash']==hashlib.sha256(raw).hexdigest()
        chunks.append(out['text'].encode())
        if out['next_offset'] is None:break
        assert out['next_offset']>offset
        offset=out['next_offset']
    assert b''.join(chunks)==raw
    assert not out['complete']
    out=read_stage_context(ae,ar,None,None,ctx,{'context_pack_ref':'ctx','source':'context_pack','path':['rows','0'],'offset':0,'limit':200})
    assert out['content_hash']==canonical_hash(pack['rows'][0])


@pytest.mark.parametrize('mutation',[{'context_pack_ref':'other'}, {'path':['..','secrets']}, {'offset':-1}, {'limit':16385}, {'path':['rows','999999']}])
def test_reader_rejects_unbound_scope_path_and_page(mutation):
    ctx,ar,ae=auth_objects({'rows':[]})
    args={'context_pack_ref':'ctx','source':'context_pack','path':[],'offset':0,'limit':1024,**mutation}
    with pytest.raises(SemanticMcpError):read_stage_context(ae,ar,None,None,ctx,args)


def test_reader_runtime_rejection_precedes_source_access():
    ctx,ar,ae=auth_objects({'secret':'value'})
    ar.verify_root_agent_runtime_scope.side_effect=OwnerConflict('stale')
    with pytest.raises(SemanticMcpError):
        read_stage_context(ae,ar,None,None,ctx,{'context_pack_ref':'ctx','source':'context_pack','path':[],'offset':0,'limit':1024})
    ae.query_idea_stage_request.assert_not_called()


def test_real_gateway_accepts_registered_reader_schema():
    operations=stage_context_operations(advancement_engine=None,agent_runtime=None,research_graph=None,research_memory=None)
    gateway=SemanticMcpGateway(operations)
    assert operations[0].semantic_operation_id=='research_memory.stage_context.read'


def test_scientific_reader_never_crosses_question_scope():
    ctx,ar,ae=auth_objects({})
    graph=NS(read_question_scientific_outcome=Mock(side_effect=OwnerConflict('research_history_source_unbound')))
    with pytest.raises(SemanticMcpError):
        read_stage_context(ae,ar,graph,None,ctx,{'context_pack_ref':'ctx','source':'scientific_outcome','source_ref':'foreign-outcome','path':[],'offset':0,'limit':1024})
    graph.read_question_scientific_outcome.assert_called_once_with(quest_ref='quest',question_ref='question',outcome_ref='foreign-outcome')


def test_frozen_predecessor_can_read_complete_cross_question_science_and_closure():
    exact={'cycle_ref':'old_cycle','commit_ref':'old_commit','outcome_ref':'old_outcome',
        'receipt':{},'outcome_receipt':{'subject_ref':'old_scientific'},
        'closure':{'notes':'technical notes retained only on demand',
            'transition':{'source_quest_ref':'quest','source_question_ref':'old_question'},
            'scientific_summary':{'summary_only':True,'claim':{'text':'excerpt'}}}}
    ctx,ar,ae=auth_objects({'prior_accepted_bindings':[reasoning_handoff_reference(exact)]})
    ae.query_reasoning_successor_context=Mock(return_value={'prior_accepted_bindings':[exact]})
    scientific={'claim':'the full original scientific claim','question_ref':'old_question'}
    graph=NS(read_question_scientific_outcome=Mock(return_value=scientific))
    args={'context_pack_ref':'ctx','source':'scientific_outcome','source_ref':'old_scientific','path':[],'offset':0,'limit':16384}
    result=read_stage_context(ae,ar,graph,None,ctx,args)
    assert json.loads(result['text'])==scientific
    graph.read_question_scientific_outcome.assert_called_once_with(quest_ref='quest',question_ref='old_question',outcome_ref='old_scientific')
    result=read_stage_context(ae,ar,graph,None,ctx,{**args,'source':'predecessor_closure','source_ref':'old_commit'})
    assert json.loads(result['text'])==exact['closure']
    exact['closure']['notes']='tampered Owner source'
    with pytest.raises(SemanticMcpError):read_stage_context(ae,ar,graph,None,ctx,args)


def test_research_notes_follow_only_authenticated_frozen_predecessor_question():
    exact={'cycle_ref':'old_cycle','commit_ref':'old_commit','outcome_ref':'old_outcome',
        'receipt':{},'outcome_receipt':{'subject_ref':'old_scientific'},
        'closure':{'transition':{'source_quest_ref':'quest','source_question_ref':'old_question'}}}
    pack={'prior_accepted_bindings':[reasoning_handoff_reference(exact)]}
    ctx,ar,ae=auth_objects(pack)
    ae.query_reasoning_successor_context=Mock(return_value={'prior_accepted_bindings':[exact]})
    note={'version_ref':'original_note_v1','summary':{'text':'uncertain; independent cohort missing'}}
    page={'items':[note],'offset':0,'limit':12,'next_offset':None}
    body={'reference':note,'body':'Original uncertainty and evidence boundary.'}
    memory=NS(query_question_research_notes=Mock(return_value=page),
        read_question_research_note=Mock(return_value=body))
    view=stage_context_view('idea',pack,context_pack_ref='ctx',context_pack_hash=canonical_hash(pack))
    pointer=view['predecessor_research_notes_readers'][0]
    assert pointer['predecessor_ref']=='old_commit'
    args={key:value for key,value in pointer.items() if key not in {'operation','summary_only'}}
    result=read_stage_context(ae,ar,None,memory,ctx,args)
    assert json.loads(result['text'])==page
    memory.query_question_research_notes.assert_called_once_with(
        quest_ref='quest',question_ref='old_question',offset=0)
    body_args={**args,'source':'research_note_body','source_ref':'original_note_v1'}
    result=read_stage_context(ae,ar,None,memory,ctx,body_args)
    assert json.loads(result['text'])==body
    memory.read_question_research_note.assert_called_once_with(
        quest_ref='quest',question_ref='old_question',version_ref='original_note_v1')
    # Other notes must still pass RM's exact-version membership check.
    memory.read_question_research_note.side_effect=OwnerConflict('research_note_source_unbound')
    with pytest.raises(SemanticMcpError,match='research_note_source_unbound'):
        read_stage_context(ae,ar,None,memory,ctx,{**body_args,'source_ref':'foreign_note'})
    calls=memory.read_question_research_note.call_count
    with pytest.raises(SemanticMcpError,match='research_note_predecessor_unbound'):
        read_stage_context(ae,ar,None,memory,ctx,{**body_args,'predecessor_ref':'unfrozen_commit'})
    exact['closure']['transition']['source_question_ref']='tampered_question'
    with pytest.raises(SemanticMcpError,match='stage_predecessor_source_unbound'):
        read_stage_context(ae,ar,None,memory,ctx,body_args)
    exact['closure']['transition'].update(source_question_ref='old_question',source_quest_ref='another_quest')
    pack['prior_accepted_bindings']=[reasoning_handoff_reference(exact)]
    with pytest.raises(SemanticMcpError,match='stage_predecessor_source_unbound'):
        read_stage_context(ae,ar,None,memory,ctx,body_args)
    assert memory.read_question_research_note.call_count==calls


def test_large_proof_tree_cannot_hide_short_current_research_notes():
    note='实施后备注：新发现缺失访谈记录；保留不一致案例，后续补充证据。'
    pack={'upstream_stage_closure':[{'stage':'bundle','commit_ref':'commit-current',
        'closure':{'notes':note,'large_proof':{f'field_{i}':'x'*1900 for i in range(400)}}}]}
    digest=canonical_hash(pack)
    view=stage_context_view('reasoning',pack,context_pack_ref='ctx',context_pack_hash=digest)
    assert len(canonical_json(view).encode())<=CONTEXT_VIEW_MAX_BYTES
    assert view['current_handoff_notes'][0]['notes']['text']==note
    assert view['current_handoff_notes'][0]['source_commit_ref']=='commit-current'
    pointer=view['current_handoff_notes'][0]['reader']
    assert pointer['path']==['upstream_stage_closure','0','closure','notes']
    full=context_read_page(pack,path=pointer['path'],offset=0,limit=16384)
    assert json.loads(full['text'])==note and full['content_hash']==canonical_hash(note)
    assert canonical_hash(pack)==digest


@pytest.mark.parametrize('count',[100,1000])
def test_current_notes_cap_is_inside_view_budget_and_keeps_exact_tail_paths(count):
    pack={'upstream_stage_closure':[{'stage':'bundle','commit_ref':f'commit_{i:06}',
        'closure':{'notes':'研究认识与尚缺证据。'*1000}} for i in range(count)]}
    digest=canonical_hash(pack)
    view=stage_context_view('reasoning',pack,context_pack_ref='ctx',context_pack_hash=digest)
    notes=view['current_handoff_notes']
    assert len(notes)==3 and all(item['notes']['truncated'] for item in notes)
    assert sum(len(item['notes']['text'].encode()) for item in notes)<=3*2048
    assert len(canonical_json(view).encode())<=CONTEXT_VIEW_MAX_BYTES
    assert [item['reader']['path'][1] for item in notes]==[str(i) for i in range(count-3,count)]
    assert canonical_hash(pack)==digest
