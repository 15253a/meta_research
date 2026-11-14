"""Follow resource indexes to immutable, paged directory contents over MCP."""
import base64
import shutil

import pytest

from meta_research.environment_operations import environment_operations
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _dataset_operations
from test_dataset_effect_scope_recovery import _scope
from test_public_plan_stage import _confirm_direct_quest
from test_root_workspace import _make


def _channel(runtime, quest):
    graph, memory, agent, human = (runtime.owners.research_graph, runtime.owners.research_memory,
                                  runtime.owners.agent_runtime, runtime.owners.human_collaboration)
    scope = _scope(runtime, quest_ref=quest, run_ref="resources:" + quest, root_ref="resource-root:" + quest)
    operations = (*_dataset_operations(graph, agent, human), *environment_operations(graph, agent, human),
        *research_content_operations(research_graph=graph, research_memory=memory,
            agent_runtime=agent, human_collaboration=human))
    gateway = SemanticMcpGateway(operations)
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=tuple(item.semantic_operation_id for item in operations))

    def call(operation, **arguments):
        _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": operation, "arguments": arguments}})
        return response["result"]
    return call


def _read(call, operation, **arguments):
    response = call(operation, **arguments)
    assert not response.get("isError"), response
    return response["structuredContent"]


@pytest.mark.parametrize("resource", ["datasets", "environments"])
def test_index_keeps_two_directory_versions_readable_entry_by_entry(tmp_path, resource):
    runtime = _make(tmp_path / "runtime")
    try:
        confirmed = _confirm_direct_quest(runtime)
        quest, question = confirmed["quest_ref"], confirmed["question_ref"]
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        call = _channel(runtime, quest)
        prefix = "research_graph." + resource
        directory = tmp_path / "original"
        directory.mkdir()
        (directory / "nested").mkdir()
        (directory / "nested" / "binary.bin").write_bytes(b"\x00\xff\x01\x80\x02")
        expected = {}
        asset = None
        dataset = (_read(call, prefix + ".register", effect_id="identity", semantic_key="navigation:directory",
            name="Nested observations", meaning="Two exact collections") ["result"] if resource == "datasets" else None)
        for ordinal in (1, 2):
            note = f"retained collection {ordinal}\n".encode()
            (directory / "notes.txt").write_bytes(note)
            change = None if asset is None else {
                "kind": "substantive_change", "expected_revision": memory.query_asset_lifecycle(asset.asset_ref)["revision"],
                "predecessor_version_ref": asset.version_ref, "explanation": "A new collection changes the notes."}
            accepted = memory.submit_asset_intake(AssetIntakeRequest(source_kind="directory", custody_mode="managed",
                source_locator=str(directory), display_name="nested-collection", asset_ref=None if asset is None else asset.asset_ref,
                change=change), idempotency_key=f"directory-{ordinal}")
            asset = accepted.asset
            assert asset is not None
            binding = asset.as_binding()
            graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest, idempotency_key=f"scope-{ordinal}")
            if resource == "datasets":
                indexed = _read(call, prefix + ".register_version", effect_id=f"version-{ordinal}",
                    dataset_ref=dataset["dataset_ref"], version_label=str(ordinal), meaning=f"Collection {ordinal}",
                    asset_bindings=[binding.as_dict()])["result"]
                _read(call, prefix + ".reference", effect_id=f"use-{ordinal}", question_ref=question,
                    dataset_version_ref=indexed["dataset_version_ref"])
                ref = indexed["dataset_version_ref"]
            else:
                indexed = _read(call, prefix + ".register", effect_id=f"snapshot-{ordinal}",
                    semantic_key="navigation:directory", name="Nested simulator resources", meaning=f"Snapshot {ordinal}",
                    source="Managed original directory", asset_bindings=[binding.as_dict()])["result"]
                ref = indexed["environment_ref"]
            expected[ref] = (asset.version_ref, note)

        assert directory.resolve().is_relative_to(tmp_path.resolve())
        shutil.rmtree(directory)
        identity = _read(call, prefix + ".page", query="navigation:directory")
        if resource == "datasets":
            assert [item["dataset_ref"] for item in identity["items"]] == [dataset["dataset_ref"]]
            versions = _read(call, prefix + ".page", dataset_ref=identity["items"][0]["dataset_ref"])["items"]
            ref_field = "dataset_version_ref"
        else:
            versions = identity["items"]
            ref_field = "environment_ref"
        assert {item[ref_field] for item in versions} == set(expected)
        for version in versions:
            ref = version[ref_field]
            exact = _read(call, prefix + ".read", **{ref_field: ref})["result"]
            if resource == "datasets":
                reader, = exact["asset_readers"]
                assert reader["operation"] == "research_memory.content.read"
                args = reader["args"]
            else:
                # The Environment page contract documents this exact binding route.
                args = {"source_ref": exact["environment_ref"], "version_ref": exact["asset_bindings"][0]["version_ref"]}
            assert args == {"source_ref": ref, "version_ref": expected[ref][0]}
            paths = []
            offset = 0
            while True:
                page = _read(call, "research_memory.content.read", **args, offset=offset, limit=1)
                assert page["kind"] == "directory" and page["offset_unit"] == "entries"
                paths.extend(item["path"] for item in page["entries"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            assert set(paths) == {"notes.txt", "nested/binary.bin"}
            for path in paths:
                data = b""
                offset = 0
                while True:
                    page = _read(call, "research_memory.content.read", **args, entry_path=path, offset=offset, limit=3)
                    data += base64.b64decode(page["text"]) if page["encoding"] == "base64" else page["text"].encode()
                    if page["complete"]:
                        break
                    offset = page["next_offset"]
                assert data == (expected[ref][1] if path == "notes.txt" else b"\x00\xff\x01\x80\x02")
            foreign = _channel(runtime, "foreign-resources-quest")
            assert _read(foreign, prefix + ".read", **{ref_field: ref})["status"] == "not_found"
            assert foreign("research_memory.content.read", **args)["isError"]
    finally:
        runtime.close()
