"""Retained data provenance and scope, through real Owners and MCP recovery."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _dataset_operations
from test_research_datasets import runtime, _asset, _dataset, _quest, _version
from test_dataset_effect_scope_recovery import _scope, _channel, _call


def _collections(runtime):
    graph = runtime.owners.research_graph
    dataset = _dataset(graph)
    return [
        _version(graph, dataset, _asset(runtime, content=content, key=label), label=label, key="v-" + label)
        for label, content in (("raw", b"1,?,3"), ("annotated", b"1,unknown,3"), ("split", b"1,3"))
    ]


def _derive(graph, question, source, derived, *, key="derive", **extra):
    return graph.derive_dataset(source_dataset_version_ref=source["dataset_version_ref"],
        derived_dataset_version_ref=derived["dataset_version_ref"], question_ref=question.question_ref,
        processing="Retained observations only; unknown values are annotated, not imputed.",
        notes="Original data remains available through its exact source version.",
        idempotency_key=key, **extra)


def test_data_lineage_supports_multiple_sources_cross_quest_reuse_and_exact_pages(runtime):
    graph = runtime.owners.research_graph
    one, two = _quest(runtime, "original"), _quest(runtime, "reuse")
    raw, annotated, split = _collections(runtime)
    before = [(asset.version_ref, asset.content_hash) for asset in runtime.owners.research_memory.query_asset_inventory()]
    first = _derive(graph, one, raw, annotated)
    second = _derive(graph, two, annotated, split, key="reuse-processing")
    third = _derive(graph, two, raw, split, key="original-as-second-source")
    usage = graph.reference_dataset(dataset_version_ref=split["dataset_version_ref"],
        question_ref=two.question_ref, purpose="New Question evaluates the retained split", idempotency_key="use")
    assert graph.query_datasets(question_ref=two.question_ref)["items"] == [usage]
    page = graph.query_datasets(dataset_version_ref=split["dataset_version_ref"], direction="sources", limit=1)
    next_page = graph.query_datasets(dataset_version_ref=split["dataset_version_ref"], direction="sources",
        offset=page["next_offset"], limit=1)
    assert [*page["items"], *next_page["items"]] == [second, third]
    assert next_page["next_offset"] is None
    assert graph.query_datasets(dataset_version_ref=raw["dataset_version_ref"], direction="derived")["items"] == [first, third]
    assert graph.query_dataset_derivation(second["dataset_derivation_ref"]) == second
    assert _derive(graph, one, raw, annotated) == first
    assert _derive(graph, one, raw, annotated, key="semantic-replay") == first
    assert graph.reconcile_dataset_operation(operation="derive", idempotency_key="semantic-replay") == first
    assert [(asset.version_ref, asset.content_hash) for asset in runtime.owners.research_memory.query_asset_inventory()] == before
    with pytest.raises(OwnerConflict, match="dataset_idempotency_conflict"):
        _derive(graph, one, raw, split)
    with pytest.raises(OwnerConflict, match="dataset_page_filter_invalid"):
        graph.query_datasets(dataset_ref=raw["dataset_ref"], dataset_version_ref=raw["dataset_version_ref"])


def test_data_lineage_rejects_cycles_even_under_concurrent_writers_and_detects_tampering(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "lineage")
    raw, annotated, split = _collections(runtime)
    first = _derive(graph, question, raw, annotated)
    _derive(graph, question, annotated, split, key="second")
    for source, derived in ((raw, raw), (split, raw)):
        with pytest.raises(OwnerConflict, match="dataset_derivation_cycle_invalid"):
            _derive(graph, question, source, derived, key="cycle")
    with pytest.raises(OwnerConflict, match="dataset_version_not_found"):
        _derive(graph, question, {"dataset_version_ref": "missing-version"}, split, key="missing")
    dataset = _dataset(graph, "concurrent")
    left = _version(graph, dataset, _asset(runtime, key="left"), label="left", key="left-version")
    right = _version(graph, dataset, _asset(runtime, content=b"Changed collection", key="right"), label="right", key="right-version")
    def accept(pair):
        try:
            return _derive(graph, question, *pair, key=pair[0]["dataset_version_ref"])
        except OwnerConflict as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, ((left, right), (right, left))))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "dataset_derivation_cycle_invalid" in results
    with graph._database.write() as connection:
        connection.execute(text("UPDATE rg_dataset_derivations SET source_dataset_version_ref = :other WHERE dataset_derivation_ref = :ref"),
            {"other": split["dataset_version_ref"], "ref": first["dataset_derivation_ref"]})
    with pytest.raises(OwnerConflict, match="dataset_receipt_invalid"):
        graph.query_dataset_derivation(first["dataset_derivation_ref"])


def test_data_derivation_tool_replays_after_root_recovery_and_rejects_wrong_quest(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    question, other = _quest(runtime, "current"), _quest(runtime, "other")
    raw, annotated, _split = _collections(runtime)
    for version in (raw, annotated):
        binding = runtime.owners.research_memory.query_asset_version(
            version["asset_bindings"][0]["version_ref"]).as_binding()
        graph.accept_asset_role(binding=binding, role="quest_source_material",
            quest_ref=question.quest_ref, idempotency_key="derive-origin:" + binding.version_ref)
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    scope = _scope(runtime, quest_ref=question.quest_ref)
    old = _channel(gateway, scope)
    args = dict(effect_id="keep-annotated", source_dataset_version_ref=raw["dataset_version_ref"],
        derived_dataset_version_ref=annotated["dataset_version_ref"], question_ref=question.question_ref,
        processing="Annotated the missing entries while preserving the original observations.")
    accepted = _call(gateway, old, "derive", **args)
    assert not accepted.get("isError"), accepted
    result = accepted["structuredContent"]["result"]
    current = _channel(gateway, _scope(runtime, quest_ref=question.quest_ref, generation=2))
    assert _call(gateway, old, "derive", **args).get("isError")
    assert _call(gateway, current, "derive.reconcile", effect_id=args["effect_id"])["structuredContent"] == accepted["structuredContent"]
    assert _call(gateway, current, "derive", **args)["structuredContent"] == accepted["structuredContent"]
    assert _call(gateway, current, "read", dataset_derivation_ref=result["dataset_derivation_ref"])["structuredContent"]["result"] == result
    assert _call(gateway, current, "page", dataset_version_ref=annotated["dataset_version_ref"], direction="sources")["structuredContent"]["items"] == [result]
    denied = _call(gateway, current, "derive", **{**args, "effect_id": "other-quest", "question_ref": other.question_ref})
    assert denied.get("isError") and "dataset_question_scope_invalid" in str(denied)
    usage = _call(gateway, current, "reference", effect_id="use", dataset_version_ref=annotated["dataset_version_ref"],
        question_ref=question.question_ref)["structuredContent"]["result"]
    assert _call(gateway, current, "read", dataset_reference_ref=usage["dataset_reference_ref"])["structuredContent"]["result"] == usage


def test_dataset_lineage_references_real_same_quest_work_and_rejects_foreign_quest(tmp_path):
    from test_root_formal_entities import _accept
    from test_target_root_finalizer import _root_finalizer_fixture
    runtime, lifecycle, memory, _, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        fact = graph.query_target_formal_results(handle.target_ref)[0]
        question, = graph.query_question_tree()
        other = _quest(runtime, "reuse-project")
        raw, annotated, _split = _collections(runtime)
        research_refs = [handle.target_ref, fact["variant_run_ref"], fact["evaluation_attempt_ref"], accepted.target_commit_ref]
        for index, research_ref in enumerate(research_refs):
            usage = graph.reference_dataset(dataset_version_ref=raw["dataset_version_ref"], question_ref=question.question_ref,
                research_ref=research_ref, purpose="Exact data used by this research work", idempotency_key=f"right-{index}")
            assert usage["research_ref"] == research_ref
            edge = _derive(graph, question, raw, annotated, research_ref=research_ref, key=f"derive-{index}")
            assert graph.query_dataset_derivation(edge["dataset_derivation_ref"]) == edge
            with pytest.raises(OwnerConflict, match="dataset_research_ref_scope_invalid"):
                graph.reference_dataset(dataset_version_ref=raw["dataset_version_ref"], question_ref=other.question_ref,
                    research_ref=research_ref, purpose="A different Quest cannot claim this research lineage",
                    idempotency_key=f"foreign-reference-{index}")
            with pytest.raises(OwnerConflict, match="dataset_research_ref_scope_invalid"):
                _derive(graph, other, raw, annotated, research_ref=research_ref, key=f"foreign-derive-{index}")
            assert graph.reconcile_dataset_operation(operation="reference",
                idempotency_key=f"foreign-reference-{index}") is None
            assert graph.reconcile_dataset_operation(operation="derive",
                idempotency_key=f"foreign-derive-{index}") is None
        with pytest.raises(OwnerConflict, match="dataset_research_ref_not_found"):
            _derive(graph, other, raw, annotated, research_ref="variant_run_missing", key="missing-run")
        edge = _derive(graph, question, raw, annotated, research_ref=fact["variant_run_ref"])
        assert edge["research_ref"] == fact["variant_run_ref"]
    finally:
        runtime.close()
