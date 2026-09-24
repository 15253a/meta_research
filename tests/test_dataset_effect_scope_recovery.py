"""Exercise Dataset effects against real AR scopes, including interleavings."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from meta_research.semantic_mcp import SemanticMcpGateway, ROOT_AGENT_DATASET_OPERATION_IDS
from meta_research.semantic_owner_gateway import _dataset_operations
from test_research_datasets import _runtime, _asset, _dataset, _version, _quest


@pytest.fixture
def runtime(tmp_path):
    result = _runtime(tmp_path / "dataset-scope")
    yield result
    result.close()


def _scope(runtime, *, quest_ref="dataset-test-quest", run_ref="companion", root_ref="root", generation=1):
    scope = dict(quest_ref=quest_ref, run_ref=run_ref, attempt_ref=f"attempt-{generation}",
        root_session_ref=root_ref, fence_ref=f"fence-{generation}",
        runtime_binding_hash="b" * 64, generation=generation)
    runtime.owners.agent_runtime.register_external_root_task_scope(root_kind="companion", root_runtime_scope=scope)
    return scope


def _channel(gateway, scope):
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=ROOT_AGENT_DATASET_OPERATION_IDS)
    return channel


def _call(gateway, channel, operation, **arguments):
    _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "research_graph.datasets." + operation, "arguments": arguments}})
    return response["result"]


@pytest.mark.parametrize("action", ["register", "register_version", "reference", "derive"])
def test_retired_root_cannot_commit_any_dataset_effect(runtime, monkeypatch, action):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    question = _quest(runtime, "scope")
    scope = _scope(runtime, quest_ref=question.quest_ref)
    dataset, asset = _dataset(graph), _asset(runtime)
    version = _version(graph, dataset, asset)
    derived = _version(graph, dataset, asset, label="derived", key="derived") if action == "derive" else None
    arguments = {
        "register": dict(semantic_key="scope:late", name="late", meaning="Must reject retired writer"),
        "register_version": dict(dataset_ref=dataset["dataset_ref"], version_label="new",
            meaning="Must reject retired writer", asset_bindings=[asset.as_dict()]),
        "reference": dict(dataset_version_ref=version["dataset_version_ref"], question_ref=question.question_ref),
        "derive": dict(source_dataset_version_ref=version["dataset_version_ref"],
            derived_dataset_version_ref=None if derived is None else derived["dataset_version_ref"],
            question_ref=question.question_ref, processing="Must reject retired writer"),
    }[action]
    method = {"register": "register_dataset", "register_version": "register_dataset_version",
              "reference": "reference_dataset", "derive": "derive_dataset"}[action]
    original = getattr(graph, method)
    def retire_before_owner_write(**kwargs):
        agent.complete_external_root_task_scope(root_kind="companion", root_runtime_scope=scope)
        assert agent.query_managed_run(scope["run_ref"])["status"] == "completed"
        return original(**kwargs)
    monkeypatch.setattr(graph, method, retire_before_owner_write)
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    with runtime._database.read() as connection:
        before = connection.execute(text("SELECT COUNT(*) FROM rg_dataset_commands")).scalar_one()
    result = _call(gateway, _channel(gateway, scope), action, effect_id="retired", **arguments)
    assert result.get("isError"), result
    assert "scope_stale" in str(result)
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM rg_dataset_commands")).scalar_one() == before


def test_recovery_attempt_can_reconcile_and_replay_the_original_dataset_effect(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    scope = _scope(runtime)
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    old = _channel(gateway, scope)
    arguments = dict(effect_id="lost-response", semantic_key="scope:ack", name="ack", meaning="An immutable accepted Dataset")
    created = _call(gateway, old, "register", **arguments)
    assert not created.get("isError"), created
    next_scope = _scope(runtime, generation=2)
    current = _channel(gateway, next_scope)
    assert _call(gateway, old, "register.reconcile", effect_id="lost-response").get("isError")
    reconciled = _call(gateway, current, "register.reconcile", effect_id="lost-response")
    assert reconciled["structuredContent"] == created["structuredContent"]
    assert _call(gateway, current, "register", **arguments)["structuredContent"] == created["structuredContent"]
    assert graph.query_datasets(query="scope:ack")["total"] == 1
    changed = _call(gateway, current, "register", **{**arguments, "meaning": "A different request"})
    assert changed.get("isError") and "dataset_idempotency_conflict" in str(changed)
    another_scope = _scope(runtime, run_ref="another-companion", root_ref="another-root")
    unrelated = _call(gateway, _channel(gateway, another_scope), "register.reconcile", effect_id="lost-response")
    assert unrelated["structuredContent"]["status"] == "not_found"


def test_dataset_effect_ids_remain_separate_between_operation_families(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    question = _quest(runtime, "families")
    scope = _scope(runtime, quest_ref=question.quest_ref)
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    channel = _channel(gateway, scope)
    asset = _asset(runtime)
    graph.accept_asset_role(binding=asset, role="evidence", quest_ref=question.quest_ref, idempotency_key="family-origin")
    dataset = _call(gateway, channel, "register", effect_id="same-id", semantic_key="family:dataset",
        name="dataset", meaning="Shared id string is local to an operation family")["structuredContent"]["result"]
    version = _call(gateway, channel, "register_version", effect_id="same-id", dataset_ref=dataset["dataset_ref"],
        version_label="first", meaning="Exact collection", asset_bindings=[asset.as_dict()])
    assert not version.get("isError"), version
    assert _call(gateway, channel, "register.reconcile", effect_id="same-id")["structuredContent"]["result"] == dataset
    assert _call(gateway, channel, "register_version.reconcile", effect_id="same-id")["structuredContent"] == version["structuredContent"]


def test_concurrent_registration_still_deduplicates_and_rm_release_is_protected(runtime):
    graph = runtime.owners.research_graph
    def register(index):
        return graph.register_dataset(semantic_key="concurrent:shared", name="shared", meaning="Concurrent identity",
                                      idempotency_key=f"parallel-{index}")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(register, range(8)))
    assert len({result["dataset_ref"] for result in results}) == 1
    asset = _asset(runtime)
    version = _version(graph, results[0], asset)
    release = runtime.owners.research_memory.assess_release_eligibility(asset.version_ref,
        expected_reference_revision=graph.query_asset_reference_revision(), idempotency_key="release")
    assert not release.eligible
    assert "semantic_reference_active" in release.reason_codes
    assert "dataset-version:" + version["dataset_version_ref"] in release.active_reference_refs
