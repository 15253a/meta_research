"""Ordinary literature uses accepted assets without inventing a DeepFetch run."""
import pytest
from meta_research.owners.common import OwnerConflict
from meta_research.research_content import discover_literature, read_content
from test_research_datasets import _asset
from test_root_workspace import _make
from test_public_plan_stage import _confirm_direct_quest
from types import SimpleNamespace


def test_ordinary_paper_and_reading_are_discoverable_with_distinct_origins(tmp_path):
    runtime = _make(tmp_path)
    try:
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        paper = _asset(runtime, content=b"External authors: results and limits", key="paper")
        note = _asset(runtime, content=b"Our analysis: evidence is inconclusive", key="reading")
        for binding in (paper, note):
            graph.accept_asset_role(binding=binding, role="evidence", quest_ref=q.quest_ref,
                                    idempotency_key="origin-" + binding.version_ref)
        registered = graph.register_literature(quest_ref=q.quest_ref, record_ref="doi:10.1000/external",
            title="External result", source="Authors A and B; Publisher; doi:10.1000/external",
            fulltext_status="obtained", asset_bindings=[paper.as_dict()], idempotency_key="register")
        usage = graph.reference_literature(quest_ref=q.quest_ref, literature_ref=registered["ref"],
            question_ref=q.question_ref, purpose="Check applicability", reading_summary="Inconclusive",
            reading_bindings=[note.as_dict()], idempotency_key="reference")
        page = discover_literature(graph, memory, quest_ref=q.quest_ref)
        item = next(i for i in page["items"] if i.get("literature_ref") == registered["ref"])
        assert item["source"] == registered["source"]
        assert item["judgments"][0]["reference_ref"] == usage["ref"]
        assert read_content(graph, memory, quest_ref=q.quest_ref, **item["readers"][0])["text"] == "External authors: results and limits"
        assert read_content(graph, memory, quest_ref=q.quest_ref, **item["judgments"][0]["readers"][0])["text"] == "Our analysis: evidence is inconclusive"
        assert graph.query_literature(registered["ref"], quest_ref=q.quest_ref) == registered
        assert graph.reconcile_literature_operation(operation="register", idempotency_key="register") == registered
        foreign = SimpleNamespace(quest_ref="foreign",question_ref="foreign-question")
        assert graph.query_literature(registered["ref"], quest_ref=foreign.quest_ref) is None
        with pytest.raises(OwnerConflict, match="literature_quest_scope_invalid"):
            graph.reference_literature(quest_ref=foreign.quest_ref, literature_ref=registered["ref"],
                question_ref=foreign.question_ref, purpose="forbidden", idempotency_key="foreign")
    finally:
        runtime.close()


def _gateway(runtime, question, generation=1):
    """Isolate Owner effect fencing from the production role catalog.

    This explicit test grant uses a real AR scope that can advance generations;
    production Companion grants remain read-only and are tested separately.
    """
    from meta_research.literature_operations import literature_operations
    from meta_research.semantic_mcp import SemanticMcpGateway, ROOT_AGENT_LITERATURE_OPERATION_IDS
    from test_dataset_effect_scope_recovery import _scope
    scope = _scope(runtime, quest_ref=question.quest_ref, run_ref="literature-writer",
                   root_ref="literature-root", generation=generation)
    gateway = SemanticMcpGateway(literature_operations(runtime.owners.research_graph,
        runtime.owners.agent_runtime))
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=ROOT_AGENT_LITERATURE_OPERATION_IDS)
    def call(op, **args):
        status,response=gateway.dispatch(channel.token,
            {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
                "name":"research_memory.literature."+op,"arguments":args}})
        return response["result"] if status == 200 else {"isError":True,"http_status":status,"response":response}
    return scope,call


def test_gateway_unknown_effect_recovery_and_unauthorized_write(tmp_path):
    runtime = _make(tmp_path)
    try:
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        old_scope, call = _gateway(runtime,q)
        args = dict(effect_id="one-paper",record_ref="doi:10.1000/partial",title="Partial",
            source="Authors; publisher",fulltext_status="waiting_user",metadata={"failure":"library access unavailable"})
        first = call("register",**args)
        assert not first["isError"],first
        expected = first["structuredContent"]["result"]
        scope, recovered = _gateway(runtime,q,2)
        assert recovered("register.reconcile",effect_id="one-paper")["structuredContent"]["result"] == expected
        assert recovered("register",**args)["structuredContent"]["result"] == expected
        invalid = recovered("register",**{**args,"title":"different"})
        assert invalid["isError"] and "literature_idempotency_conflict" in str(invalid)
        stale = call("register",**{**args,"effect_id":"stale"})
        assert stale["isError"] and stale["structuredContent"]["code"] == "root_agent_human_request_scope_stale"
        stale_reconcile = call("register.reconcile",effect_id="one-paper")
        assert stale_reconcile["isError"] and stale_reconcile["structuredContent"]["code"] == "root_agent_human_request_scope_stale"
        runtime.owners.agent_runtime.complete_external_root_task_scope(root_kind="companion",root_runtime_scope=scope)
        retired = recovered("register",**{**args,"effect_id":"retired"})
        assert retired["isError"] and retired["structuredContent"]["code"] == "root_agent_human_request_scope_stale"
    finally:
        runtime.close()


def test_gateway_real_linked_original_rejects_drift_and_unaccepted_source(tmp_path):
    import os
    from test_stage_asset_intake import channel
    runtime = _make(tmp_path)
    try:
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        context, intake = channel(runtime,q)
        directory = runtime.owners.human_collaboration._root_workspaces.bind_runtime(context).location
        path = directory.directory / "paper.txt"
        path.write_bytes(b"Original paper")
        accepted = intake("research_memory.assets.intake",effect_id="paper",intake={"source_kind":"local_path",
            "custody_mode":"linked_local","display_name":"paper.txt","source":{"kind":"workspace",
                "workspace_ref":directory.workspace_ref,"path":"paper.txt"}})
        assert not accepted["isError"],accepted
        asset = accepted["structuredContent"]["asset"]
        binding = {k:asset[k] for k in ("asset_ref","version_ref","content_hash","manifest_hash","receipt")}
        _, call = _gateway(runtime,q)
        args=dict(effect_id="linked",record_ref="url:https://example.test/paper",title="Linked paper",
                  source="Original authors; publisher URL",fulltext_status="obtained",asset_bindings=[binding])
        registered = call("register",**args)
        assert not registered["isError"],registered
        assert intake("research_memory.content.read",source_ref=binding["version_ref"],version_ref=binding["version_ref"])["structuredContent"]["text"] == "Original paper"
        before=path.stat();path.write_bytes(b"Modified paper");os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        drift = intake("research_memory.content.read",source_ref=binding["version_ref"],version_ref=binding["version_ref"])
        assert drift["isError"] and "drift" in str(drift)
        bad = {**binding,"content_hash":"0"*64}
        rejected = call("register",**{**args,"effect_id":"forged","asset_bindings":[bad]})
        assert rejected["isError"],rejected
        assert call("register",effect_id="no-file",record_ref="doi:absent",title="Absent",source="Publisher",fulltext_status="obtained")["isError"]
    finally:
        runtime.close()


def test_accepted_target_reading_is_indexed_by_bundle_with_original_provenance(tmp_path):
    import hashlib
    from dataclasses import replace
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_research_notes_and_call_observations import _SystemEvidenceReader
    from test_target_root_finalizer import _root_finalizer_fixture
    from test_stage_resource_handoff import _bundle_channel, _call, _accepted
    runtime, lifecycle, memory, authority, handle, workspace, old = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "outputs/result.json").write_bytes((workspace / "outputs/metrics.json").read_bytes())
        analysis=workspace/"outputs/analysis";analysis.mkdir()
        body="Our Target reading of doi:10.1000/external: independent evidence still missing.\n"
        (analysis/"research-note.md").write_text(body)
        workspace_ref,_=runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref,target_run_ref=handle.target_run_ref,root_session_ref=handle.root_session_ref,
            attempt_ref=handle.execution_attempt_ref,fence_ref=handle.execution_fence_ref)
        message="Reading preserved with limitations."
        evidence=replace(old,handoff=None,workspace_ref=workspace_ref,final_text=message,
            final_text_sha256=hashlib.sha256(message.encode()).hexdigest())
        finalizer=TargetRunFinalizer(lifecycle=lifecycle,memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,evidence_reader=_SystemEvidenceReader(),
            measurement_authority=runtime.owners.research_graph,graph_authority=runtime.owners.research_graph)
        accepted=finalizer.finalize(handle=handle,evidence=evidence)
        assert accepted.status == "completed"
        manifest=memory.query(accepted.manifest_ref)
        entry=next(e for e in manifest.entries if e.research_note and e.research_note["kind"]=="research_note")
        request=runtime.owners.advancement_engine.query_bundle_stage_request(runtime.bundle_stage.query_current()["stage_run_request"]["cycle_ref"])
        q=request.accepted_question
        paper=_asset(runtime,content=b"External author original",key="external-original")
        graph=runtime.owners.research_graph
        graph.accept_asset_role(binding=paper,role="evidence",quest_ref=q.quest_ref,idempotency_key="original-source")
        channel=_bundle_channel(runtime)
        reg=_accepted(_call(runtime,channel,"research_memory.literature.register",effect_id="paper",
            record_ref="doi:10.1000/external",title="External paper",source="External author, publisher",
            fulltext_status="obtained",asset_bindings=[paper.as_dict()]))["result"]
        use=_accepted(_call(runtime,channel,"research_memory.literature.reference",effect_id="reading",
            literature_ref=reg["ref"],question_ref=q.question_ref,research_ref=handle.target_ref,
            purpose="Assess applicability",reading_summary="Missing independent evidence",
            reading_bindings=[entry.binding.as_dict()]))["result"]
        wrong=_call(runtime,channel,"research_memory.literature.reference",effect_id="wrong-reading",
            literature_ref=reg["ref"],question_ref=q.question_ref,research_ref=handle.target_ref,
            purpose="Wrong producer",reading_bindings=[paper.as_dict()])
        assert wrong["isError"] and "literature_reading_source_invalid" in str(wrong)
        page=_accepted(_call(runtime,channel,"research_memory.literature.page",record_ref="doi:10.1000/external"))
        for query in (handle.target_ref, "Assess applicability", "Missing independent evidence"):
            found = _accepted(_call(runtime, channel, "research_memory.literature.page", query=query))
            assert any(item.get("literature_ref") == reg["ref"] for item in found["items"])
        item=next(i for i in page["items"] if i.get("literature_ref")==reg["ref"])
        assert len(item["judgments"]) == 1
        judgment=item["judgments"][0]
        assert judgment["reference_ref"]==use["ref"] and judgment["research_ref"]==handle.target_ref
        content=_accepted(_call(runtime,channel,"research_memory.content.read",**judgment["readers"][0]))
        assert content["text"]==body
        assert content["selected_evidence_binding"]["source_kind"]=="AssetVersion"
        original=_accepted(_call(runtime,channel,"research_memory.content.read",**item["readers"][0]))
        assert original["text"]=="External author original"
        assert reg["source"]=="External author, publisher"
    finally:
        runtime.close()


def test_reasoning_without_bundle_can_register_and_read_literature(tmp_path):
    from test_public_reasoning_stage import _reasoning_runtime, _DeterministicReasoningSkill
    from test_public_plan_stage import _DeterministicDraftingAdapter, _DeterministicProbe, _finish_idea_stage
    from meta_research.semantic_owner_gateway import REASONING_ROOT_SEMANTIC_OPERATION_IDS
    from test_stage_resource_handoff import _call, _accepted
    runtime=_reasoning_runtime(tmp_path,reasoning_skill=_DeterministicReasoningSkill(),
        host_compute_probe=_DeterministicProbe(),proposal_drafter=_DeterministicDraftingAdapter())
    try:
        quest=_confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(4):
            runtime.reasoning_stage.process_once()
            current=runtime.reasoning_stage.query_current()
            request=current.get("stage_run_request")
            run=runtime.owners.agent_runtime.query_reasoning_stage_run(request["request_ref"]) if request else None
            if run is not None:break
        assert run is not None
        channel=runtime.harnesses.issue_resident_mcp_channel(run_ref=run.run_ref,attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref,fence_ref=run.fence_ref,capability_binding_hash=run.runtime_binding_hash,
            root_kind="reasoning",phase="primary",subject_policy="operation_tree",operation_ids=REASONING_ROOT_SEMANTIC_OPERATION_IDS)
        reg=_accepted(_call(runtime,channel,"research_memory.literature.register",effect_id="reasoning-paper",
            record_ref="doi:10.1000/lead",title="Unavailable full text",source="Publisher abstract",
            fulltext_status="missing",metadata={"limitation":"abstract only; not read in full"}))["result"]
        usage=_accepted(_call(runtime,channel,"research_memory.literature.reference",effect_id="reasoning-use",
            literature_ref=reg["ref"],question_ref=quest["question_ref"],purpose="Follow up evidence gap"))["result"]
        assert _accepted(_call(runtime,channel,"research_memory.literature.read",literature_ref=usage["ref"]))["result"]==usage
        page=_accepted(_call(runtime,channel,"research_memory.literature.page",record_ref=reg["record_ref"]))
        assert page["items"][0]["fulltext_status"]=="missing"
        assert page["items"][0]["readers"]==[]
        assert page["items"][0]["judgments"][0]["purpose"]=="Follow up evidence gap"
    finally:
        runtime.close()
