"""Native content.read supplies the exact RM binding for Dataset acceptance."""
from copy import deepcopy

from sqlalchemy import text

from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpGateway, ROOT_AGENT_DATASET_OPERATION_IDS
from meta_research.semantic_owner_gateway import _dataset_operations
from test_dataset_effect_scope_recovery import _scope
from test_dataset_research_ref_scope import accepted_research


def _channel(gateway, scope):
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=(*ROOT_AGENT_DATASET_OPERATION_IDS, "research_memory.content.read"))
    return channel


def _call(gateway, channel, operation, **arguments):
    _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": operation, "arguments": arguments}})
    return response["result"]


def _accepted(response):
    assert not response.get("isError"), response
    return response["structuredContent"]


def _counts(runtime):
    with runtime._database.read() as connection:
        return tuple(connection.execute(text("SELECT COUNT(*) FROM " + table)).scalar_one()
            for table in ("rg_dataset_versions", "rg_dataset_references", "rg_dataset_commands"))


def test_native_formal_product_read_register_reference_and_reconcile(accepted_research):
    runtime, own, foreign, _, _, refs = accepted_research
    graph, memory, agent = runtime.owners.research_graph, runtime.owners.research_memory, runtime.owners.agent_runtime
    gateway = SemanticMcpGateway((*_dataset_operations(graph, agent),
        *research_content_operations(research_graph=graph, research_memory=memory, agent_runtime=agent)))
    scope = _scope(runtime, quest_ref=own.quest_ref, run_ref="product-handoff", root_ref="product-handoff-root")
    channel = _channel(gateway, scope)
    dataset = _accepted(_call(gateway, channel, "research_graph.datasets.register",
        effect_id="product-dataset", semantic_key="formal-output:bounded-observation",
        name="Retained actual observation", meaning="Actual Target output, not a new execution"))["result"]
    facts = graph.query_target_formal_results(refs["target"])
    artifact = next(item for fact in facts for item in fact["run_artifacts"] if item["role"] == "data_asset")
    version_ref = artifact["version_ref"]
    # These are the exact native child parameters observed in T14.
    args = dict(source_ref=version_ref, version_ref=version_ref, offset=0, limit=16384)
    page = _accepted(_call(gateway, channel, "research_memory.content.read", **args))
    assert page["text"] in {"36\n", "169\n"}
    binding = page["asset_binding"]
    assert binding == memory.query_asset_version(version_ref).as_binding().as_dict()
    assert binding["receipt"]["subject_ref"] == version_ref
    assert binding["asset_ref"] == artifact["asset_ref"]
    assert binding["content_hash"] == artifact["content_hash"]
    assert binding["manifest_hash"] == artifact["manifest_hash"]
    before = _counts(runtime)
    forged = deepcopy(binding)
    forged["receipt"]["receipt_ref"] = "rm_receipt_forged"
    invalid = _call(gateway, channel, "research_graph.datasets.register_version", effect_id="forged",
        dataset_ref=dataset["dataset_ref"], version_label="forged", meaning="Must reject altered receipt",
        asset_bindings=[forged])
    assert invalid.get("isError") and "receipt" in str(invalid)
    assert _counts(runtime) == before
    registered = _accepted(_call(gateway, channel, "research_graph.datasets.register_version",
        effect_id="exact-version", dataset_ref=dataset["dataset_ref"], version_label="retained-v1",
        meaning="Unchanged real Target output", asset_bindings=[binding]))
    version = registered["result"]
    assert version["asset_bindings"] == [binding]
    reference = _accepted(_call(gateway, channel, "research_graph.datasets.reference",
        effect_id="exact-reference", dataset_version_ref=version["dataset_version_ref"],
        question_ref=own.question_ref, research_ref=refs["target"], purpose="Reuse the actual bounded observation"))
    assert reference["result"]["dataset_version_ref"] == version["dataset_version_ref"]
    # Dataset source reads return the same binding and receipt, not a reconstructed claim.
    dataset_page = _accepted(_call(gateway, channel, "research_memory.content.read",
        **{**args, "source_ref": version["dataset_version_ref"]}))
    assert dataset_page["asset_binding"] == binding
    foreign_scope = _scope(runtime, quest_ref=foreign.quest_ref,
        run_ref="foreign-product-handoff", root_ref="foreign-product-handoff-root")
    foreign_channel = _channel(gateway, foreign_scope)
    before = _counts(runtime)
    denied = _call(gateway, foreign_channel, "research_memory.content.read", **args)
    assert denied.get("isError") and "scope" in str(denied)
    denied = _call(gateway, foreign_channel, "research_graph.datasets.register_version", effect_id="foreign-version",
        dataset_ref=dataset["dataset_ref"], version_label="foreign", meaning="Foreign Quest cannot adopt origin",
        asset_bindings=[binding])
    assert denied.get("isError") and "scope" in str(denied)
    assert _counts(runtime) == before
    next_scope = _scope(runtime, quest_ref=own.quest_ref, run_ref="product-handoff",
        root_ref="product-handoff-root", generation=2)
    assert _call(gateway, channel, "research_memory.content.read", **args).get("isError")
    assert _call(gateway, channel, "research_graph.datasets.register_version.reconcile",
        effect_id="exact-version").get("isError")
    current = _channel(gateway, next_scope)
    assert _accepted(_call(gateway, current, "research_graph.datasets.register_version.reconcile",
        effect_id="exact-version")) == registered
    assert _accepted(_call(gateway, current, "research_graph.datasets.reference.reconcile",
        effect_id="exact-reference")) == reference
    assert _accepted(_call(gateway, current, "research_memory.content.read", **args))["asset_binding"] == binding
