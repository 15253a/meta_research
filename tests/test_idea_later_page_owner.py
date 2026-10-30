"""Independent Idea later-page acceptance through fresh production Owners.

The research Provider is deterministic. Asset intake/custody, evidence roles,
Idea content/domain acceptance and subsequent receipt reads use actual Owners.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import subprocess

import pytest
from sqlalchemy import text
from meta_research.owners.common import canonical_hash, OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_public_idea_stage import _runtime, _confirm_direct_quest, _DeterministicIdeaSkill
from test_public_plan_stage import _finish_idea_stage
from test_bundle_skill_adapter import _fake_codex
from test_runtime_conditions_provider_prompts import _SignedRunner
from conftest import _isolate_platform_power_dependency


class LaterPageIdea(_DeterministicIdeaSkill):
    selected_ref=None
    page_reader=None

    def generate_draft(self,request):
        assert request.context_pack['schema_ref'].endswith('/v4')
        assert self.selected_ref not in request.context_pack['accepted_evidence_refs']
        page,refs=self.page_reader(request.context_pack['accepted_question_binding']['quest_ref'],offset=32,limit=32)
        assert page['total_count']==33
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
        assert request.context_pack['accepted_evidence_refs']==[]
        assert request.context_pack['evidence_page'] is None
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


class _IdeaDiscoveryRunner(_SignedRunner):
    """Deterministic provider; evidence discovery uses the real resident grant."""
    def __init__(self):
        super().__init__()
        self.runtime = None
        self.request = None
        self.selected_ref = None
        self.pages = []
        self.token = None

    def run_command(self, argv, timeout):
        return subprocess.CompletedProcess(argv, 0,
            "hooks stable true\nmulti_agent stable true\nplugins stable true\n"
            "remote_plugin stable true\nshell_tool stable true\n"
            "skill_search stable true\nunified_exec stable true", "")

    def __call__(self, argv, prompt, timeout, environment=None):
        request = self.request
        token = environment["META_RESEARCH_MCP_TOKEN"]
        self.token = token
        session = None

        def rpc(method, params):
            nonlocal session
            status, response, response_session = self.runtime.harnesses.dispatch_mcp_http(
                token, {"jsonrpc": "2.0", "id": "idea-discovery", "method": method,
                        "params": params}, mcp_session_id=session,
            )
            assert status == 200, response
            if response_session is not None:
                session = response_session
            return response["result"]

        rpc("initialize", {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "idea-discovery-test", "version": "1"}})
        catalog = rpc("tools/list", {})["tools"]
        reader = next(item for item in catalog if item["name"] == "research_memory.stage_context.read")
        assert reader["name"] in ROOT_AGENT_SEMANTIC_OPERATION_IDS["idea"]
        assert "evidence_index" in reader["inputSchema"]["properties"]["source"]["enum"]

        def read_index(index_offset):
            chunks = []
            offset = 0
            expected_hash = None
            while True:
                result = rpc("tools/call", {"name": reader["name"], "arguments": {
                    "context_pack_ref": request.context_pack_ref, "source": "evidence_index",
                    "path": [], "offset": offset, "limit": 128, "index_offset": index_offset,
                }})
                assert result["isError"] is False, result
                page = result["structuredContent"]
                assert page["context_pack_ref"] == request.context_pack_ref
                assert page["context_pack_hash"] == request.context_pack_hash
                assert page["source_ref"] == request.context_pack["accepted_question_binding"]["quest_ref"]
                assert page["source"] == "evidence_index" and page["offset_unit"] == "utf8_bytes"
                assert page["offset"] == offset
                assert page["returned_bytes"] == len(page["text"].encode("utf-8"))
                if expected_hash is None:
                    expected_hash = page["content_hash"]
                assert page["content_hash"] == expected_hash
                chunks.append(page["text"])
                self.pages.append({"index_offset": index_offset, **page})
                if page["next_offset"] is None:
                    break
                assert page["next_offset"] > offset
                offset = page["next_offset"]
            encoded = "".join(chunks).encode("utf-8")
            assert hashlib.sha256(encoded).hexdigest() == expected_hash
            assert len(encoded) == page["total_bytes"]
            return json.loads(encoded), page["index_page"], expected_hash

        first, first_index, _ = read_index(0)
        assert len(first["items"]) == 32 and self.selected_ref not in first["items"]
        assert first_index["next_offset"] == 32
        later, later_index, later_hash = read_index(first_index["next_offset"])
        assert later["items"] == [self.selected_ref]
        assert later_index["total_count"] == 33 and later_index["next_offset"] is None
        assert read_index(32) == (later, later_index, later_hash)
        body = rpc("tools/call", {"name": "research_memory.content.read", "arguments": {
            "source_ref": self.selected_ref, "version_ref": self.selected_ref,
            "offset": 0, "limit": 128,
        }})
        assert body["isError"] is False, body
        exact = body["structuredContent"]
        assert exact["version_ref"] == exact["asset_binding"]["version_ref"] == self.selected_ref
        assert exact["text"] == '{"observation":0}'
        assert exact["entry_hash"] == hashlib.sha256(exact["text"].encode()).hexdigest()
        assert exact["complete"] is True
        denied = rpc("tools/call", {"name": reader["name"], "arguments": {
            "context_pack_ref": "unbound-context-pack", "source": "evidence_index",
            "path": [], "offset": 0, "limit": 128, "index_offset": 32,
        }})
        assert denied["isError"] is True
        assert denied["structuredContent"] == {"code": "stage_context_ref_unbound"}
        draft = _DeterministicIdeaSkill().generate_draft(request).draft
        draft["candidates"][0]["evidence_boundary"]["accepted_evidence_refs"] = [self.selected_ref]
        self.output = {"outcome": draft}
        return super().__call__(argv, prompt, timeout, environment)


class _IdeaDiscoveryAdapter(CodexIdeaSkillAdapter):
    def __init__(self, *args, runner, **kwargs):
        self.discovery_runner = runner
        self.requests = []
        super().__init__(*args, process_runner=runner, **kwargs)

    def generate_draft(self, request):
        self.requests.append(request)
        self.discovery_runner.request = request
        return super().generate_draft(request)


def test_idea_discovers_later_evidence_through_its_actual_resident_mcp_grant(tmp_path):
    runner = _IdeaDiscoveryRunner()
    provider = _IdeaDiscoveryAdapter(tmp_path / "provider", runner=runner,
        executable=str(_fake_codex(tmp_path / "codex")))
    runtime = _runtime(tmp_path / "resident-discovery", provider)
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_direct_quest(runtime)
        assets = [add_evidence(runtime, quest["quest_ref"], index) for index in range(33)]
        runner.selected_ref = assets[0].version_ref
        current = _finish_idea_stage(runtime)
        [request] = provider.requests
        assert request.context_pack["evidence_page"] is None
        assert request.context_pack["accepted_evidence_refs"] == []
        assert any(page["next_offset"] is not None for page in runner.pages)
        assert any(page["index_offset"] == 32 for page in runner.pages)
        submission = current["run"]["submission_ref"]
        graph = runtime.owners.research_graph
        memory = runtime.owners.research_memory
        content = memory.query_idea_outcome_content(submission)
        decision = graph.query_idea_outcome_decision(submission)
        assert decision.decision == "accepted"
        assert content.outcome["candidates"][0]["evidence_boundary"]["accepted_evidence_refs"] == [assets[0].version_ref]
        add_evidence(runtime, quest["quest_ref"], 34)
        assert graph.query_idea_outcome_decision(submission) == decision
        assert memory.query_idea_outcome_content(submission) == content
        frozen = runtime.owners.advancement_engine.query_idea_stage_request(quest["cycle_ref"])
        assert frozen.context_pack_hash == request.context_pack_hash == canonical_hash(frozen.context_pack)
        status, _, _ = runtime.harnesses.dispatch_mcp_http(runner.token, {
            "jsonrpc": "2.0", "id": "released-channel", "method": "tools/list",
        }, mcp_session_id=None)
        assert status == 401
        # The root can find the schema and call it, and its first packet must
        # also name the exact tool/argument rather than an unexplained alias.
        assert 'research_memory.stage_context.read（source="evidence_index"' in runner.calls[0][1]
    finally:
        runtime.close()
