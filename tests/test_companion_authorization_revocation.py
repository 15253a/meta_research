"""Cross-Quest readers follow current HC decisions, including later revocation."""
from test_dataset_effect_scope_recovery import _scope
from test_public_human_collaboration_ladder import _confirm_capability_command
from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
from test_research_datasets import _asset
from test_root_workspace import _channel, _call, _accepted, _external_context


def revoke_quest_authority(human, quest_ref):
    scope = {"quest_ref": quest_ref}
    confirmed = _confirm_capability_command(human, scope_ref="quest:" + quest_ref,
        capability="broad_research", decision="revoked", capability_scope=scope, key="revoke-reading")
    human.decide_capability_authorization("quest:" + quest_ref, {"capability": "broad_research",
        "decision": "revoked", "scope": scope,
        "confirmation_receipt_ref": confirmed["confirmation_receipt"]["receipt_ref"]}, "revoke-reading-authority")
    authority = human.query_broad_research_authorization(quest_ref)
    assert authority["decision"] == "granted"  # The original issuance is still historical fact.
    assert authority["effective_decision"] == "revoked"


def test_cross_quest_readers_and_discovery_recheck_confirmed_human_revocation(tmp_path):
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        current, _ = _accept_root_question(runtime, "current")
        other, _ = _accept_root_question(runtime, "other")
        graph = runtime.owners.research_graph
        asset = _asset(runtime, key="other-evidence", content=b"Other Quest exact original.")
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=other, idempotency_key="other-source")
        channel = _channel(runtime, _external_context(_scope(runtime, quest_ref=current)))
        readers = (("research_memory.content.read", {"source_ref": asset.version_ref, "version_ref": asset.version_ref}),
            ("research_graph.questions.page", {}), ("research_memory.assets.page", {}))
        for operation, args in readers:
            _accepted(_call(runtime, channel, operation, source_quest_ref=other, **args))
        question_page = _accepted(_call(runtime, channel, "research_graph.questions.page", source_quest_ref=other))
        for item in question_page["items"]:
            _accepted(_call(runtime, channel, "research_memory.content.read", **item["reader"]))
            history_reader = dict(item["history_reader"])
            _accepted(_call(runtime, channel, history_reader.pop("operation"), **history_reader))
        exact = _accepted(_call(runtime, channel, "research_memory.content.read", source_quest_ref=other,
            source_ref=asset.version_ref, version_ref=asset.version_ref))["selected_evidence_binding"]["reader"]
        assert _accepted(_call(runtime, channel, exact["operation"], **exact["args"]))["text"] == "Other Quest exact original."
        asset_page = _accepted(_call(runtime, channel, "research_memory.assets.page", source_quest_ref=other))
        asset_current = _accepted(_call(runtime, channel, "research_memory.assets.current", source_quest_ref=other,
            version_ref=asset.version_ref))
        content = _accepted(_call(runtime, channel, exact["operation"], **exact["args"]))
        for value in (asset_page["items"][0], asset_current, content):
            history = value["lifecycle"]["history_reader"]
            lifecycle = _accepted(_call(runtime, channel, history["operation"], **history["args"]))
            assert lifecycle["current_version_ref"] == asset.version_ref
        original_page = _accepted(_call(runtime, channel, "research_graph.questions.page", entry_kind="quests", limit=1))
        assert original_page["next_offset"] == 1

        revoke_quest_authority(runtime.owners.human_collaboration, other)

        for operation, args in readers:
            denied = _call(runtime, channel, operation, source_quest_ref=other, **args)
            assert denied["isError"] and denied["structuredContent"]["code"] == "content_quest_not_authorized"
        stale = _call(runtime, channel, "research_graph.questions.page", entry_kind="quests", limit=1,
            offset=1, query_revision=original_page["query_revision"])
        assert stale["isError"] and stale["structuredContent"]["code"] == "research_query_changed"
        current_page = _accepted(_call(runtime, channel, "research_graph.questions.page", entry_kind="quests"))
        assert {item["quest_ref"] for item in current_page["items"]} == {current}
        _accepted(_call(runtime, channel, "research_graph.questions.page"))
    finally:
        runtime.close()
