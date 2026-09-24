from fastapi.testclient import TestClient
from test_research_datasets import _runtime,_quest,_asset
from meta_research.web import create_app

def test_authenticated_user_can_explicitly_add_input_and_choose_output_language(tmp_path):
 r=_runtime(tmp_path/"r")
 try:
  q=_quest(r,"one");other=_quest(r,"other")
  client=TestClient(create_app(r,base_url="http://testserver",control_key="isolated-test"))
  auth=client.post("/auth/bootstrap",headers={"Origin":"http://testserver"},json={"token":r.authentication.issue_bootstrap_token()})
  assert auth.status_code==200
  headers={"Origin":"http://testserver","X-CSRF-Token":auth.json()["csrf_token"],"Idempotency-Key":"input-one"}
  assert client.get("/api/v1/preferences").json()["output_language"]=="zh"
  assert client.put("/api/v1/preferences",headers=headers,json={"output_language":"en"}).status_code==200
  assert client.get("/api/v1/preferences").json()["output_language"]=="en"
  payload={"quest_ref":q.quest_ref,"question_ref":q.question_ref,"text":"Isolated test user: compare the two observations; this is advice, not authorization.","asset_bindings":[]}
  assert client.post("/api/v1/research-inputs",headers={"Origin":"http://testserver"},json=payload).status_code==403
  revision=r.owners.human_collaboration.query_snapshot().revision
  response=client.post("/api/v1/research-inputs",headers=headers,json=payload)
  assert response.status_code==201,response.text
  assert r.owners.human_collaboration.query_snapshot().revision==revision+1
  value=response.json()
  assert client.post("/api/v1/research-inputs",headers=headers,json=payload).json()==value
  assert r.owners.human_collaboration.query_snapshot().revision==revision+1
  page=client.get("/api/v1/research-library/human",params={"quest_ref":q.quest_ref}).json()
  assert len(page["items"])==1
  reader=page["items"][0]["reader"]
  body=client.get("/api/v1/research-content",params={"quest_ref":q.quest_ref,**reader})
  assert body.status_code==200,body.text
  assert "Isolated test user" in body.json()["text"]
  assert client.get("/api/v1/research-content",params={"quest_ref":other.quest_ref,**reader}).status_code==409
  leaf=r.owners.research_graph.resolve_reasoning_historical_evidence_leaf(quest_ref=q.quest_ref,ref=value["input_ref"])
  assert leaf["kind"]=="HumanInput" and leaf["content_hash"]==value["content_hash"]
  questions=client.get("/api/v1/research-library/questions",params={"quest_ref":q.quest_ref}).json()
  assert questions["items"][0]["question_ref"]==q.question_ref
 finally:r.close()

def test_input_material_and_generic_evidence_keep_exact_receipts(tmp_path):
 r=_runtime(tmp_path/"r")
 try:
  q=_quest(r,"one");asset=_asset(r)
  r.owners.research_graph.accept_asset_role(binding=asset,role="evidence",quest_ref=q.quest_ref,idempotency_key="origin")
  value=r.owners.human_collaboration.submit_research_input(quest_ref=q.quest_ref,text_content="Explicit test user's attached notes",asset_bindings=[asset.as_dict()],idempotency_key="input")
  assert value["asset_bindings"][0]["version_ref"]==asset.version_ref
  leaf=r.owners.research_graph.resolve_reasoning_historical_evidence_leaf(quest_ref=q.quest_ref,ref=asset.version_ref)
  assert leaf["kind"]=="AssetVersion" and leaf["source_subject_ref"]==asset.asset_ref
 finally:r.close()


def test_input_scope_pagination_and_corrupt_receipt_are_rejected(tmp_path):
 import pytest
 from sqlalchemy import text
 from meta_research.owners.common import OwnerConflict
 from meta_research.research_content import read_content
 r=_runtime(tmp_path/"human-boundary")
 try:
  q=_quest(r,"one");foreign=_quest(r,"foreign")
  hc=r.owners.human_collaboration
  with pytest.raises(OwnerConflict,match="human_input_question_invalid"):
   hc.submit_research_input(quest_ref=q.quest_ref,question_ref=foreign.question_ref,text_content="wrong scope",idempotency_key="foreign")
  values=[hc.submit_research_input(quest_ref=q.quest_ref,text_content=f"Test analyst observation {i}",idempotency_key=f"input-{i}") for i in range(3)]
  first=hc.query_research_inputs(quest_ref=q.quest_ref,query="analyst",limit=2)
  second=hc.query_research_inputs(quest_ref=q.quest_ref,query="analyst",limit=2,offset=first["next_offset"])
  assert len(first["items"])==2 and len(second["items"])==1 and second["next_offset"] is None
  assert {x["input_ref"] for x in first["items"]+second["items"]}=={v["input_ref"] for v in values}
  original=values[0]
  with pytest.raises(OwnerConflict,match="human_input_idempotency_conflict"):
   hc.submit_research_input(quest_ref=q.quest_ref,text_content="different",idempotency_key="input-0")
  with pytest.raises(OwnerConflict,match="content_version_unbound"):
   read_content(r.owners.research_graph,r.owners.research_memory,quest_ref=q.quest_ref,source_ref=original["input_ref"],version_ref="0"*64)
  with r._database.write() as c:
   c.execute(text("UPDATE hc_research_inputs SET payload_json='broken' WHERE input_ref=:ref"),{"ref":original["input_ref"]})
  with pytest.raises(OwnerConflict,match="human_input_receipt_invalid"):
   hc.query_research_input(original["input_ref"],quest_ref=q.quest_ref)
  with pytest.raises(OwnerConflict,match="human_input_receipt_invalid"):
   r.owners.research_graph.resolve_reasoning_historical_evidence_leaf(quest_ref=q.quest_ref,ref=original["input_ref"])
 finally:r.close()
