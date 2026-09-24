from test_research_datasets import _runtime,_quest
from meta_research.research_content import discover_questions,read_content

def test_discovery_finds_question_by_clue_and_reads_exact_content(tmp_path):
 r=_runtime(tmp_path/"r")
 try:
  q=_quest(r,"one");_quest(r,"foreign")
  page=discover_questions(r.owners.research_graph,r.owners.research_memory,quest_ref=q.quest_ref,query="证据",limit=1)
  assert len(page["items"])==1
  item=page["items"][0];assert item["question_ref"]==q.question_ref
  content=read_content(r.owners.research_graph,r.owners.research_memory,quest_ref=q.quest_ref,**item["reader"])
  assert "证据" in content["text"]
  assert discover_questions(r.owners.research_graph,r.owners.research_memory,quest_ref=q.quest_ref,offset=1)["items"]==[]
 finally:r.close()


def test_question_entry_paginates_and_keeps_pruned_questions_readable(tmp_path):
 from test_public_manual_question_lifecycle import _confirm_waived_manual_question
 from test_public_advancement_runtime_control import _confirmed_control,_execute_control
 from test_dataset_effect_scope_recovery import _scope
 from meta_research.question_relations import question_history_operations
 from meta_research.semantic_mcp import SemanticMcpGateway
 r=_runtime(tmp_path/"pruned")
 try:
  q=_quest(r,"root");graph=r.owners.research_graph;human=r.owners.human_collaboration
  for i in range(2):
   _confirm_waived_manual_question(human,quest_ref=q.quest_ref,parent_question_ref=q.question_ref,key_prefix=f"child-{i}")
   for _ in range(8):
    if not human.reconcile_once():break
  children=[x for x in graph.query_question_tree(q.quest_ref) if x.question_ref!=q.question_ref]
  assert len(children)==2
  fg=r.owners.advancement_engine.query_foreground(q.quest_ref)
  command=_confirmed_control(human,scope_ref=f"quest:{q.quest_ref}",payload={"action":"prune","target":{
   "quest_ref":q.quest_ref,"cycle_ref":fg["cycle_ref"],"question_ref":fg["question_ref"],"epoch":fg["epoch"],
   "target_question_ref":children[0].question_ref},"reason":"operator_requested"},key="prune")
  _execute_control(human,command,"prune")
  first=discover_questions(graph,r.owners.research_memory,quest_ref=q.quest_ref,limit=2)
  second=discover_questions(graph,r.owners.research_memory,quest_ref=q.quest_ref,limit=2,offset=first["next_offset"])
  all_items=first["items"]+second["items"]
  assert len(all_items)==3 and second["next_offset"] is None
  pruned=next(x for x in all_items if x["question_ref"]==children[0].question_ref)
  assert pruned["status"]=="pruned"
  assert read_content(graph,r.owners.research_memory,quest_ref=q.quest_ref,**pruned["reader"])["text"]
  scope=_scope(r,quest_ref=q.quest_ref)
  op=question_history_operations(research_graph=graph,agent_runtime=r.owners.agent_runtime)
  gateway=SemanticMcpGateway((op,))
  channel,_=gateway.issue_channel(run_ref=scope["run_ref"],attempt_ref=scope["attempt_ref"],root_session_ref=scope["root_session_ref"],fence_ref=scope["fence_ref"],capability_binding_hash=scope["runtime_binding_hash"],root_kind="companion",phase="primary",operation_ids=(op.semantic_operation_id,))
  _,result=gateway.dispatch(channel.token,{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":op.semantic_operation_id,"arguments":{"question_ref":children[0].question_ref,"offset":1,"limit":1}}})
  assert not result["result"].get("isError"),result
  assert result["result"]["structuredContent"]["status"]=="accepted"
 finally:r.close()
