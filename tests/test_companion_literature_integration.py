"""#270 exact Literature readers coexist with #271 read-only Companion grants."""
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.semantic_mcp import SemanticCallContext
from test_companion_authorization_revocation import revoke_quest_authority
from test_dataset_effect_scope_recovery import _scope
from test_public_idea_stage import _DeterministicIdeaSkill
from test_public_manual_question_lifecycle import DeterministicDraftingAdapter, DeterministicProbe, _accept_root_question
from test_research_datasets import _asset
from test_root_workspace import _channel, _call, _accepted, _external_context


def test_production_companion_reads_exact_other_quest_literature_but_cannot_write_or_reuse_revoked_authority(tmp_path):
    drafting = DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=DeterministicProbe(), idea_skill_provider=_DeterministicIdeaSkill())
    try:
        current, _ = _accept_root_question(runtime, "current")
        admitted = runtime.idea_stage.start("literature-reader-idea")
        run = runtime.owners.agent_runtime.query_idea_stage_run(admitted["stage_run_request"]["request_ref"])
        assert run is not None
        idea = _channel(runtime, SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash, "idea", "primary", "research_memory.literature.read"))
        other, question = _accept_root_question(runtime, "other")
        graph = runtime.owners.research_graph
        paper = _asset(runtime, key="paper", content=b"External authors: selected first version.")
        note = _asset(runtime, key="note", content=b"Our reading: the evidence has limits.")
        for asset in (paper, note):
            graph.accept_asset_role(binding=asset, role="evidence", quest_ref=other, idempotency_key="role-" + asset.version_ref)
        record = graph.register_literature(quest_ref=other, record_ref="doi:10.1000/selected",
            title="Selected paper", source="External authors; journal", fulltext_status="obtained",
            asset_bindings=[paper.as_dict()], idempotency_key="register")
        usage = graph.reference_literature(quest_ref=other, literature_ref=record["ref"], question_ref=question,
            purpose="Check limitations", reading_summary="Bounded finding", reading_bindings=[note.as_dict()],
            idempotency_key="reference")
        latest = _asset(runtime, key="new-paper", asset_ref=paper.asset_ref, content=b"A later unselected version.")
        assert runtime.owners.research_memory.query_current_asset(paper.asset_ref).version_ref == latest.version_ref
        companion = _channel(runtime, _external_context(_scope(runtime, quest_ref=current)))
        _, listing, _ = runtime.harnesses.dispatch_mcp_http(companion.connection.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, mcp_session_id=None)
        names = {item["name"] for item in listing["result"]["tools"]}
        assert {"research_memory.literature.page", "research_memory.literature.read", "assistant.content.generate"} <= names
        assert not {"research_memory.literature.register", "research_memory.literature.reference",
            "research_memory.literature.register.reconcile", "research_memory.literature.reference.reconcile"} & names
        denied_write = _call(runtime, companion, "research_memory.literature.register", effect_id="forbidden",
            record_ref="doi:no", title="No", source="No", fulltext_status="missing")
        assert denied_write["isError"] and denied_write["structuredContent"]["code"] == "capability_unavailable"

        assert _accepted(_call(runtime, companion, "research_memory.literature.read", literature_ref=record["ref"]))["status"] == "not_found"
        page = _accepted(_call(runtime, companion, "research_memory.literature.page", source_quest_ref=other))
        item, = page["items"]
        for descriptor, expected in ((item["reader"], record), (item["judgments"][0]["reader"], usage)):
            selected = dict(descriptor)
            operation = selected.pop("operation")
            exact = _accepted(_call(runtime, companion, operation, **selected))
            assert exact["result"] == expected
        original = _accepted(_call(runtime, companion, "research_memory.content.read", **item["readers"][0]))
        assert original["text"] == "External authors: selected first version."
        assert original["selected_evidence_binding"]["source_binding"]["version_ref"] == paper.version_ref
        reading = _accepted(_call(runtime, companion, "research_memory.content.read",
            **item["judgments"][0]["readers"][0]))
        assert reading["text"] == "Our reading: the evidence has limits."
        denied_idea = _call(runtime, idea, "research_memory.literature.read", source_quest_ref=other, literature_ref=record["ref"])
        assert denied_idea["isError"] and denied_idea["structuredContent"]["code"] == "content_quest_not_authorized"
        revoke_quest_authority(runtime.owners.human_collaboration, other)
        for operation, args in (("research_memory.literature.page", {}),
                ("research_memory.literature.read", {"literature_ref": record["ref"]}),
                ("research_memory.literature.read", {"literature_ref": usage["ref"]}),
                ("research_memory.content.read", {k: v for k, v in item["readers"][0].items() if k != "source_quest_ref"})):
            denied = _call(runtime, companion, operation, source_quest_ref=other, **args)
            assert denied["isError"] and denied["structuredContent"]["code"] == "content_quest_not_authorized"
    finally:
        runtime.close()
