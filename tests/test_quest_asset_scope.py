from test_research_datasets import _runtime, _quest, _asset, _dataset, _version
from test_dataset_effect_scope_recovery import _scope, _channel, _call
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _dataset_operations


def test_quest_dataset_discovery_and_effects_require_existing_asset_origin(tmp_path):
    runtime = _runtime(tmp_path / "scope")
    try:
        graph = runtime.owners.research_graph
        one, two = _quest(runtime, "one"), _quest(runtime, "two")
        asset = _asset(runtime)
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=one.quest_ref,
                                idempotency_key="origin")
        dataset = _dataset(graph)
        version = _version(graph, dataset, asset)
        graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
                                question_ref=one.question_ref, purpose="Reusable observations", idempotency_key="usage")
        gateway = SemanticMcpGateway(_dataset_operations(graph, runtime.owners.agent_runtime))
        own = _channel(gateway, _scope(runtime, quest_ref=one.quest_ref, run_ref="one", root_ref="one-root"))
        foreign = _channel(gateway, _scope(runtime, quest_ref=two.quest_ref, run_ref="two", root_ref="two-root"))
        assert _call(gateway, own, "page", query="Qualitative")["structuredContent"]["items"][0]["dataset_ref"] == dataset["dataset_ref"]
        assert _call(gateway, foreign, "page", query="Qualitative")["structuredContent"]["items"] == []
        assert _call(gateway, foreign, "read", dataset_version_ref=version["dataset_version_ref"])["structuredContent"]["status"] == "not_found"
        bad = _call(gateway, foreign, "reference", effect_id="launder", dataset_version_ref=version["dataset_version_ref"], question_ref=two.question_ref)
        assert bad.get("isError"), bad
        bad = _call(gateway, foreign, "register_version", effect_id="launder-version", dataset_ref=dataset["dataset_ref"], version_label="foreign", meaning="cannot import another Quest", asset_bindings=[asset.as_dict()])
        assert bad.get("isError"), bad
        other_asset = _asset(runtime, content=b"Different Quest material", key="other")
        graph.accept_asset_role(binding=other_asset, role="evidence", quest_ref=two.quest_ref, idempotency_key="other-origin")
        other_version = _version(graph, dataset, other_asset, label="other", key="other-version")
        bad = _call(gateway, foreign, "derive", effect_id="launder-derive",
                    source_dataset_version_ref=version["dataset_version_ref"],
                    derived_dataset_version_ref=other_version["dataset_version_ref"],
                    question_ref=two.question_ref, processing="Cannot launder the first Quest's source")
        assert bad.get("isError"), bad
        assert graph.query_datasets(question_ref=two.question_ref, quest_ref=two.quest_ref)["items"] == []
    finally:
        runtime.close()
