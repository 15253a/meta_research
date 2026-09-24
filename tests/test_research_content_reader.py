import json
import pytest
from test_research_datasets import _runtime, _quest, _asset
from test_dataset_effect_scope_recovery import _scope
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.research_content import research_content_operations

def channel(runtime, question):
    scope = _scope(runtime, quest_ref=question.quest_ref, run_ref=question.quest_ref, root_ref=question.question_ref)
    gateway = SemanticMcpGateway(research_content_operations(research_graph=runtime.owners.research_graph,
        research_memory=runtime.owners.research_memory, agent_runtime=runtime.owners.agent_runtime))
    token,_ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=("research_memory.content.read",))
    def call(**args):
        _,value=gateway.dispatch(token.token,{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"research_memory.content.read","arguments":args}})
        return value["result"]
    return call

def test_public_content_pages_are_exact_quest_scoped_and_fenced(tmp_path):
    r=_runtime(tmp_path/"root")
    try:
        one,two=_quest(r,"one"),_quest(r,"two")
        body=("研究材料。"*6000).encode()
        asset=_asset(r,content=body)
        r.owners.research_graph.accept_asset_role(binding=asset,role="evidence",quest_ref=one.quest_ref,idempotency_key="origin")
        call=channel(r,one); offset=0; chunks=[]
        while True:
            result=call(source_ref=asset.version_ref,version_ref=asset.version_ref,offset=offset,limit=1024)
            assert not result.get("isError"),result
            page=result["structuredContent"];chunks.append(page["text"])
            assert page["returned_bytes"]<=1024
            if page["complete"]:break
            assert page["next_offset"]>offset
            offset=page["next_offset"]
        assert "".join(chunks).encode()==body
        assert channel(r,two)(source_ref=asset.version_ref,version_ref=asset.version_ref).get("isError")
        scope=_scope(r,quest_ref=one.quest_ref,run_ref=one.quest_ref,root_ref=one.question_ref,generation=2)
        assert call(source_ref=asset.version_ref,version_ref=asset.version_ref).get("isError")
    finally:r.close()

def test_large_linked_content_reads_in_place_and_reports_drift_and_missing(tmp_path):
    r=_runtime(tmp_path/"root")
    try:
        q=_quest(r,"linked");path=tmp_path/"large.txt"
        with path.open("wb") as f:
            for _ in range(66):f.write(b"a"*(1024*1024))
        intake=r.owners.research_memory.submit_asset_intake(AssetIntakeRequest(source_kind="local_path",custody_mode="linked_local",display_name="large.txt",source_locator=str(path),media_type="text/plain",asynchronous=True),idempotency_key="large")
        if intake.asset is None:
            for _ in range(5):
                r.owners.research_memory.process_asset_intake_once()
                intake=r.owners.research_memory.query_asset_intake(intake.job_ref)
                if intake.asset is not None:break
        asset=intake.asset.as_binding()
        r.owners.research_graph.accept_asset_role(binding=asset,role="evidence",quest_ref=q.quest_ref,idempotency_key="origin")
        call=channel(r,q);args=dict(source_ref=asset.version_ref,version_ref=asset.version_ref,offset=65*1024*1024,limit=100)
        assert call(**args)["structuredContent"]["text"]=="a"*100
        with path.open("r+b") as f:f.write(b"b")
        assert "drifted" in str(call(**args))
        with path.open("r+b") as f:f.write(b"a")
        assert call(**args)["structuredContent"]["text"]=="a"*100
        path.unlink();assert "unavailable" in str(call(**args))
    finally:r.close()


def test_accepted_deepfetch_snapshot_is_an_exact_quest_source(tmp_path):
    from test_reasoning_summary_decision_flow import runtime_at,SummarySkill,seed_checkpoint
    from meta_research.research_content import read_content
    skill=SummarySkill("decline")
    r=runtime_at(tmp_path/"snapshot",skill)
    try:
        request,checkpoint=seed_checkpoint(r)
        for _ in range(10):
            r.autonomous_creation.process_once()
            view=r.autonomous_creation.query(checkpoint.checkpoint_ref)
            if view and view["deepfetch"]["status"]=="queued":r.deepfetch.process_once()
            if view and view["status"]=="awaiting_reasoning_decision":break
        assert r.reasoning_stage.process_once()
        ref=skill.calls[0][1]["source_ref"]
        page=read_content(r.owners.research_graph,r.owners.research_memory,quest_ref=request.accepted_question.quest_ref,source_ref=ref,version_ref=ref)
        assert page["source_binding"]["kind"]=="LiteratureSnapshot" and page["text"]
        with pytest.raises(OwnerConflict,match="content_version_unbound"):
            read_content(r.owners.research_graph,r.owners.research_memory,quest_ref=request.accepted_question.quest_ref,source_ref=ref,version_ref="wrong")
        with pytest.raises(OwnerConflict,match="content_source_unbound"):
            read_content(r.owners.research_graph,r.owners.research_memory,quest_ref="other-quest",source_ref=ref,version_ref=ref)
    finally:r.close()
