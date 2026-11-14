"""Baseline discovery must lead to the exact retained implementation bytes."""
from __future__ import annotations

import base64
import io
import shutil
import zipfile

from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _baseline_operations
from test_dataset_effect_scope_recovery import _scope
from test_formal_run_snapshots import _scenario
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _channel(runtime, quest):
    graph, memory, agent = (runtime.owners.research_graph,
                            runtime.owners.research_memory, runtime.owners.agent_runtime)
    scope = _scope(runtime, quest_ref=quest, run_ref="navigation:" + quest, root_ref="root:" + quest)
    operations = (*_baseline_operations(graph, agent), *research_content_operations(
        research_graph=graph, research_memory=memory, agent_runtime=agent,
        human_collaboration=runtime.owners.human_collaboration))
    gateway = SemanticMcpGateway(operations)
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=tuple(operation.semantic_operation_id for operation in operations))

    def call(operation, **arguments):
        _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": operation, "arguments": arguments}})
        return response["result"]
    return call


def _accepted(call, operation, **arguments):
    response = call(operation, **arguments)
    assert not response.get("isError"), response
    return response["structuredContent"]


def test_baseline_run_reader_exposes_readable_implementation_snapshot(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        _, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
        expected = next(entry for entry in manifest.entries if entry.role == "implementation")
        assert runtime.owners.research_memory.query_asset_version(expected.binding.version_ref)

        call = _channel(runtime, quest)
        discovered = _accepted(call, "research_graph.baselines.page", query="primary")
        baseline = discovered["items"][0]["baseline_ref"]
        assert baseline == authority.identities.baseline_ref
        selected = _accepted(call, "research_graph.baselines.read", baseline_ref=baseline, relation="runs")
        runs = selected["result"]["reusable_entities"]
        run = runs["items"][0]
        assert run["reader"]["operation"] == "research_graph.formal_results.read"
        lineage = _accepted(call, run["reader"]["operation"], **run["reader"]["args"])["result"]
        assert lineage["variant_run"]["implementation_revision_ref"] == manifest.implementation_revision_ref
        implementations = lineage.get("implementation_artifacts", [])
        assert [item["version_ref"] for item in implementations] == [expected.binding.version_ref], (
            "The Baseline run resolves an implementation revision, but exposes no reader "
            "for its accepted managed code snapshot."
        )
        reader = implementations[0]["reader"]
        assert reader["operation"] == "research_memory.content.read"
        local_implementation = (workspace / "implementation").resolve()
        assert local_implementation.is_relative_to(tmp_path.resolve())
        shutil.rmtree(local_implementation)
        content = _accepted(call, reader["operation"], **reader["args"])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(content["text"]))) as archive:
            assert archive.read("train.py") == b"print('train')\n"

        foreign_call = _channel(runtime, "foreign-quest")
        absent = _accepted(foreign_call, run["reader"]["operation"], **run["reader"]["args"])
        assert absent["status"] == "not_found" and absent["result"] is None
        denied = foreign_call(reader["operation"], **reader["args"])
        assert denied.get("isError"), denied
        assert "asset_quest_scope_invalid" in str(denied), denied
    finally:
        runtime.close()


def test_each_run_exposes_only_its_selected_implementation_snapshots(tmp_path):
    def select_both(document):
        document["formal_runs"].append({
            "run_key": "combined", "implementation_paths": ["implementation/v1", "implementation/v2"],
            "checkpoint_paths": [], "evaluations": [],
        })

    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, select_both)
    try:
        frozen = finalizer.finalize(handle=handle, evidence=evidence)
        manifest = memory.query(frozen.manifest_ref)
        graph = runtime.owners.research_graph
        graph.accept_target_commit_from_root_completion(
            completion=lifecycle.query_completion(handle.target_ref), manifest=manifest,
            result_document=manifest.result_document, idempotency_key="implementation-navigation")
        authority = graph.query_target_measurement_domain_authority(handle.target_ref)
        quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
        call = _channel(runtime, quest)
        discovered = _accepted(call, "research_graph.baselines.page", query="combined")
        baseline = discovered["items"][0]["baseline_ref"]
        selected = _accepted(call, "research_graph.baselines.read", baseline_ref=baseline, relation="runs")
        runs = {item["summary"]: item for item in selected["result"]["reusable_entities"]["items"]}
        assert set(runs) == {"first", "second", "combined"}
        entries = {entry.declared_relative_path: entry for entry in manifest.entries if entry.role == "implementation"}
        expected = {
            "first": [entries["implementation/v1"]],
            "second": [entries["implementation/v2"]],
            "combined": [entries["implementation/v1"], entries["implementation/v2"]],
        }
        revisions = {}
        for key, run in runs.items():
            lineage = _accepted(call, run["reader"]["operation"], **run["reader"]["args"])["result"]
            implementations = lineage.get("implementation_artifacts", [])
            assert {item["version_ref"] for item in implementations} == {
                entry.binding.version_ref for entry in expected[key]}, key
            assert len(implementations) == len(expected[key]), key
            revisions[key] = lineage["variant_run"]["implementation_revision_ref"]
            assert (lineage["evaluation_attempt"] is not None) is (key == "second")
            for item in implementations:
                content = _accepted(call, item["reader"]["operation"], **item["reader"]["args"])
                assert content["content_hash"] == item["content_hash"]
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(content["text"]))) as archive:
                    assert "calculate.py" in archive.namelist()
        assert len(set(revisions.values())) == 3

        target = _accepted(call, "research_graph.target_formal_results.read", target_ref=handle.target_ref)
        assert {item["run_key"]: {entry["version_ref"] for entry in item["implementation_artifacts"]}
                for item in target["items"]} == {
            key: {entry.binding.version_ref for entry in entries} for key, entries in expected.items()}
        for fact in target["items"]:
            assert all(entry["reader"]["operation"] == "research_memory.content.read"
                       for entry in fact["implementation_artifacts"])
    finally:
        runtime.close()
