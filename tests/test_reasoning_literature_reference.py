from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
import test_public_reasoning_owners as fixtures
from meta_research.context_presentation import literature_reference
from meta_research.owners.common import canonical_hash, canonical_json, OwnerConflict
from meta_research.reasoning_literature import reasoning_literature_leaves
from meta_research.reasoning_stage import _frozen_evidence_closure
from meta_research.owners.research_memory import _frozen_reasoning_evidence_closure


def accepted_corpus(memory,count):
    def accept(request,run):
        result=deepcopy(run.result)
        paper=result['papers'][0];fulltext=result['fulltexts'][0]
        result['papers']=[{**paper,'title':f'Paper {i:04} '+('bounded research ' * 40),
            'url':f'https://example.test/reasoning/{i:04}',
            'doi':f'10.1000/reasoning.{i:04}'} for i in range(count)]
        result['fulltexts']=[{**fulltext,'paper_url':p['url']} for p in result['papers']]
        return memory.accept_literature_snapshot(request,replace(run,result=result,result_hash=canonical_hash(result)))
    snapshot=fixtures._accepted_snapshot(SimpleNamespace(accept_literature_snapshot=accept))
    return memory.ensure_question_literature_revision(question_binding=fixtures._question(),
        source_snapshot_binding=snapshot.as_context_binding(),idempotency_key='lit-test')


def pointer_pack(revision):
    pack=fixtures._context_pack(revision)
    pack['current_target_evidence_closure']=[]
    pack['question_literature_input']['binding']=literature_reference(revision)
    return pack


@pytest.mark.parametrize('count',[100,1000])
def test_exact_corpus_beyond_preview_is_readable_and_only_actual_citations_are_saved(tmp_path,monkeypatch,count):
    database,memory,receipts,graph,rg=fixtures._owners(tmp_path)
    try:
        revision=accepted_corpus(memory,count)
        pack=pointer_pack(revision)
        reference=pack['question_literature_input']['binding']
        assert 'records' not in reference
        assert reference['record_count']==count
        assert reference['records_preview']['shown_count']<=24
        assert len(canonical_json(pack).encode())<20000
        assert memory.query_question_literature_revision_ref(question_ref=revision['question_ref'],revision_ref=revision['revision_ref'])==revision
        membership=_frozen_evidence_closure(pack,revision_reader=memory.query_question_literature_revision_ref)
        assert len([x for x in membership if x['kind']=='LiteratureRecord'])==count
        last=revision['records'][-1]['ref']
        assert last not in {x['ref'] for x in reference['records_preview']['items']}
        # Stage validation can accept a citation after the initial preview.
        assert any(x['ref']==last for x in membership)
        before_context=fixtures._context_pack
        before_output=fixtures._stage_output
        def as_pointer(value):
            result=before_context(value)
            result['current_target_evidence_closure']=[]
            result['question_literature_input']['binding']=literature_reference(value)
            return result
        def last_output(_ref,**kwargs):return before_output(last,**kwargs)
        monkeypatch.setattr(fixtures,'_context_pack',as_pointer)
        monkeypatch.setattr(fixtures,'_stage_output',last_output)
        accepted=fixtures._accept_content(memory,revision,submission_ref='selected-lit-final',outcome_ref='selected-lit-outcome')
        assert accepted.context_pack['question_literature_input']['binding']==reference
        assert [x['ref'] for x in accepted.frozen_evidence_closure if x['kind']=='LiteratureRecord']==[last]
        # The production RM receipt and immutable content reader re-resolve
        # the same source revision; hydration has no trusted-leaf shortcut.
        assert memory.query_reasoning_content(accepted.submission_ref)==accepted
        assert receipts.query_reasoning_content(accepted.submission_ref)==accepted
        # This established RM fixture has no actual RG Question row. Its
        # unrelated successor-selection authority must remain unavailable.
        with pytest.raises(OwnerConflict,match='reasoning_next_cycle_selection_facts_unavailable'):
            graph.decide_reasoning_outcome(content=accepted)
        (tmp_path/'size.json').write_text(json.dumps({'records':count,'pack_bytes':len(canonical_json(pack).encode()),
            'full_revision_bytes':len(canonical_json(revision).encode()),'saved_literature_leaves':1}))
    finally:
        database.close()


@pytest.mark.parametrize('mutation',['foreign_revision','foreign_question','hash_tamper','preview_tamper'])
def test_reference_tampering_is_rejected_before_citation_authority(tmp_path,mutation):
    database,memory,*_=fixtures._owners(tmp_path)
    try:
        revision=accepted_corpus(memory,30);pack=pointer_pack(revision)
        binding=pack['question_literature_input']['binding']
        if mutation=='foreign_revision':
            binding['revision_ref']='foreign'
            pack['question_literature_input']['revision_ref']='foreign'
        elif mutation=='foreign_question':binding['question_ref']='foreign'
        elif mutation=='hash_tamper':binding['records_hash']='0'*64
        else:binding['records_preview']['items'][0]['evidence_basis_ref']='forged'
        with pytest.raises(OwnerConflict):
            reasoning_literature_leaves(pack,revision_reader=memory.query_question_literature_revision_ref,
                cited_documents=({'evidence':[{'kind':'LiteratureRecord','ref':revision['records'][-1]['ref']}]},))
    finally:database.close()


def test_unknown_citation_cannot_become_authority_and_reviewed_citations_remain_verifiable(tmp_path):
    database,memory,*_=fixtures._owners(tmp_path)
    try:
        revision=accepted_corpus(memory,35);pack=pointer_pack(revision)
        first,last=revision['records'][0]['ref'],revision['records'][-1]['ref']
        def document(ref):return {'scientific_outcome':{'evidence':[{'kind':'LiteratureRecord','ref':ref}]}}
        with pytest.raises(OwnerConflict,match='scientific_outcome_evidence_invalid'):
            _frozen_reasoning_evidence_closure(pack,revision_verifier=memory._receipt_verifier.verify_question_literature_revision,
                revision_reader=memory.query_question_literature_revision_ref,cited_documents=(document('invented'),))
        closure=_frozen_reasoning_evidence_closure(pack,revision_verifier=memory._receipt_verifier.verify_question_literature_revision,
            revision_reader=memory.query_question_literature_revision_ref,cited_documents=(document(first),document(last)))
        assert {x['ref'] for x in closure if x['kind']=='LiteratureRecord'}=={first,last}
        assert len(closure)==2
    finally:database.close()


def test_100_to_1000_frozen_pack_growth_is_metadata_only():
    sizes=[]
    for count in (100,1000):
        revision={'kind':'QuestionLiteratureRevision','question_ref':'question','revision_ref':'revision',
            'records':[{'ref':f'record-{i:06}','evidence_basis':'retrieved_fulltext',
            'evidence_basis_ref':f'fulltext-{i:06}','title':'研究'*400} for i in range(count)]}
        pack={'accepted_question_binding':{'question_ref':'question'},'question_literature_input':{
            'kind':'revision','revision_ref':'revision','binding':literature_reference(revision)}}
        sizes.append(len(canonical_json(pack).encode()))
    assert abs(sizes[1]-sizes[0])<=4
