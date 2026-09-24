from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text, event
from meta_research.owners import research_graph as rg
from meta_research.owners import research_memory as rm
from meta_research.owners.common import canonical_hash, canonical_json, OwnerConflict

class ScratchDatabase:
    def __init__(self): self.engine=create_engine('sqlite:///:memory:')
    @contextmanager
    def read(self):
        with self.engine.connect() as connection: yield connection
    read_snapshot=read

def insert_rows(db,name,rows):
    columns=list(rows[0])
    with db.engine.begin() as con:
        con.execute(text('CREATE TABLE '+name+' ('+','.join('"'+key+'"' for key in columns)+')'))
        con.execute(text('INSERT INTO '+name+' ('+','.join('"'+key+'"' for key in columns)+') VALUES ('+','.join(':'+key for key in columns)+')'),rows)

@pytest.mark.parametrize('count',[100,1000])
def test_real_sql_history_loads_and_verifies_only_twelve_rows(monkeypatch,count):
    db=ScratchDatabase()
    insert_rows(db,'rg_question_lifecycle',[{'question_ref':f'question_{i:06}','quest_ref':'quest','status':'active'} for i in range(count)])
    insert_rows(db,'rg_graph_heads',[{'quest_ref':'quest','graph_version':count}])
    insert_rows(db,'rg_reasoning_outcome_decisions',[
        {'decision':'accepted','transition_json':canonical_json({'source_quest_ref':'quest','source_question_ref':'question_000000'}),
         'decided_at':i,'outcome_ref':f'outcome_{i:06}','submission_ref':f'submission_{i:06}',
         'scientific_outcome_ref':f'science_{i:06}','scientific_disposition':'uncertain',
         'receipt_ref':f'receipt_{i:06}'} for i in range(count)])
    verifier=rg.SQLiteResearchGraphReceiptVerifier(db,None,None,None,reasoning_content_verifier=NS())
    source=Mock(side_effect=lambda row:{'cycle_ref':row.outcome_ref,'request_ref':row.submission_ref,
        'scientific_outcome':{'quest_ref':'quest','question_ref':'question_000000'}})
    verifier._verified_reasoning_history_source=source
    monkeypatch.setattr(rg,'_query_question_record',lambda connection,ref:('root',NS(quest_ref='quest',question_ref=ref)))
    monkeypatch.setattr(rg,'_question_record_receipt',lambda kind,row:(None,None,NS(receipt_ref='qreceipt')))
    monkeypatch.setattr(rg,'_reasoning_decision',lambda row:NS(receipt=NS(receipt_ref=row.receipt_ref)))
    statements=[]
    event.listen(db.engine,'before_cursor_execute',lambda con,cur,sql,params,ctx,many:statements.append(sql))
    binding=verifier._build_reasoning_research_context(quest_ref='quest',question_ref='question_000000')
    assert len(binding['prior_current_question_outcomes'])==12
    assert len(binding['active_question_refs'])==12
    assert binding['history_page']['prior_total']==count
    assert source.call_count==12
    assert all('LIMIT' in sql for sql in statements if 'SELECT * FROM rg_reasoning_outcome_decisions' in sql)
    verifier.verify_reasoning_research_context(binding)
    assert source.call_count==24  # 12 issued, 12 reauthenticated; never N historical sources.
    page=verifier.query_active_question_page(quest_ref='quest',offset=12,limit=12)
    assert page['total_count']==count and len(page['items'])==12
    # Focusing a late current Question must not replace and lose an earlier row.
    seen=[];offset=0
    while True:
        page=verifier.query_active_question_page(quest_ref='quest',offset=offset,limit=12,focus_question_ref=f'question_{count-1:06}')
        seen.extend(page['items'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert seen[0]==f'question_{count-1:06}'
    assert len(seen)==len(set(seen))==count

def test_real_rm_history_source_checks_custody_without_replaying_assets(tmp_path):
    snapshot=json.loads((Path(__file__).parent.parent/'trajectory-before.json').read_text())['tables']
    row=snapshot['rm_reasoning_contents'][0]
    db=ScratchDatabase();insert_rows(db,'rm_reasoning_contents',[row])
    owner=object.__new__(rm.SQLiteResearchMemoryReceiptVerifier)
    owner._database=db;owner._object_store=tmp_path
    owner._stage_request_verifier=Mock(side_effect=AssertionError('history must not replay stage'))
    owner._plan_evidence_reuse_verifier=Mock(side_effect=AssertionError('history must not reopen historical assets'))
    path=tmp_path/row['object_path'];path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(row['payload_json'].encode())
    result=owner.query_reasoning_history_source(row['submission_ref'])
    assert result['outcome_hash']==row['outcome_hash']
    assert canonical_hash(result['scientific_outcome'])==row['outcome_hash']
    owner._stage_request_verifier.assert_not_called()
    owner._plan_evidence_reuse_verifier.assert_not_called()
    path.write_bytes(b'tampered immutable bytes')
    with pytest.raises(OwnerConflict,match='custody'):owner.query_reasoning_history_source(row['submission_ref'])

def test_real_rm_history_source_rejects_receipt_tamper(tmp_path):
    snapshot=json.loads((Path(__file__).parent.parent/'trajectory-before.json').read_text())['tables']
    row=deepcopy(snapshot['rm_reasoning_contents'][0]);row['receipt_hash']='0'*64
    db=ScratchDatabase();insert_rows(db,'rm_reasoning_contents',[row])
    owner=object.__new__(rm.SQLiteResearchMemoryReceiptVerifier);owner._database=db;owner._object_store=tmp_path
    with pytest.raises(OwnerConflict,match='receipt'):owner.query_reasoning_history_source(row['submission_ref'])


def test_real_rg_history_reader_binds_exact_acceptance_source_and_question(tmp_path):
    snapshot=json.loads((Path(__file__).parent.parent/'trajectory-before.json').read_text())['tables']
    source_row=snapshot['rm_reasoning_contents'][0]
    decision=next(row for row in snapshot['rg_reasoning_outcome_decisions'] if row['submission_ref']==source_row['submission_ref'])
    db=ScratchDatabase();insert_rows(db,'rm_reasoning_contents',[source_row]);insert_rows(db,'rg_reasoning_outcome_decisions',[decision])
    memory=object.__new__(rm.SQLiteResearchMemoryReceiptVerifier);memory._database=db;memory._object_store=tmp_path
    path=tmp_path/source_row['object_path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(source_row['payload_json'].encode())
    graph=rg.SQLiteResearchGraphReceiptVerifier(db,None,None,None,reasoning_content_verifier=memory)
    science=json.loads(source_row['scientific_outcome_json'])
    args={'quest_ref':science['quest_ref'],'question_ref':science['question_ref'],'outcome_ref':science['outcome_ref']}
    assert graph.read_question_scientific_outcome(**args)==science
    with pytest.raises(OwnerConflict,match='unbound'):
        graph.read_question_scientific_outcome(**{**args,'question_ref':'foreign-question'})
    with db.engine.begin() as con:con.execute(text("UPDATE rg_reasoning_outcome_decisions SET receipt_hash=:bad"),{'bad':'0'*64})
    with pytest.raises(OwnerConflict,match='receipt'):graph.read_question_scientific_outcome(**args)
