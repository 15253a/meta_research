"""Exercise discoverable exact readers against retained Owner content."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.semantic_owner_gateway import _dataset_read, _formal_results_read, _formal_result_by_ref_read
from meta_research.stage_context_access import read_stage_context
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_research_datasets import _dataset, _version, _asset
from test_target_root_finalizer import _root_finalizer_fixture, _EvidenceReader, _current_bundle_runtime
from test_target_launch_admission import _ready_launch


def _authorized_reader(runtime, quest_ref):
    state = {"valid": True}
    def verify(**kwargs):
        if not state["valid"] or kwargs["fence_ref"] != "current-fence":
            raise OwnerConflict("root_agent_runtime_scope_stale")
        return {"quest_ref": quest_ref}
    agent = SimpleNamespace(verify_root_agent_runtime_scope=verify)
    context = SimpleNamespace(root_kind="bundle", phase="execute", run_ref="run",
        attempt_ref="attempt", root_session_ref="root", fence_ref="current-fence",
        capability_binding_hash="a" * 64)
    operation = research_content_operations(research_graph=runtime.owners.research_graph,
        research_memory=runtime.owners.research_memory, agent_runtime=agent)[0]
    return agent, context, state, operation


def test_dataset_discovery_returns_callable_pinned_readers(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "dataset")
    try:
        graph = runtime.owners.research_graph
        target_graph, _, _, _, _ = _ready_launch(runtime)
        question = runtime.owners.advancement_engine.query_bundle_stage_request(target_graph.cycle_ref).accepted_question
        asset = _asset(runtime, content="原行 λ=0.1; missing value retained".encode())
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=question.quest_ref,
            idempotency_key="origin")
        dataset = _dataset(graph)
        version = _version(graph, dataset, asset)
        graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
            question_ref=question.question_ref, purpose="Retain original missingness", idempotency_key="use")
        agent, context, state, content = _authorized_reader(runtime, question.quest_ref)
        result = _dataset_read(graph, agent, context,
            {"dataset_version_ref": version["dataset_version_ref"]}, page=False)["result"]
        reader, = result["asset_readers"]
        assert reader["operation"] == "research_memory.content.read"
        assert content.handler(context, reader["args"])["text"] == "原行 λ=0.1; missing value retained"
        newer = _asset(runtime, content=b"Changed later observation", key="new-version", asset_ref=asset.asset_ref)
        assert newer.version_ref != reader["args"]["version_ref"]
        assert content.handler(context, reader["args"])["text"] == "原行 λ=0.1; missing value retained"
        assert graph.query_dataset_version(version["dataset_version_ref"]) == version
        page = _dataset_read(graph, agent, context, {"dataset_ref": dataset["dataset_ref"]}, page=True)
        assert page["items"][0]["asset_readers"] == [reader]
        with pytest.raises(SemanticMcpError, match="content_version_unbound"):
            content.handler(context, {**reader["args"], "version_ref": "asset_version_wrong"})
        context.fence_ref = "old-fence"
        with pytest.raises(SemanticMcpError, match="scope_stale"):
            content.handler(context, reader["args"])
        context.fence_ref = "current-fence"
        state["valid"] = False
        with pytest.raises(SemanticMcpError, match="scope_stale"):
            content.handler(context, reader["args"])
    finally:
        runtime.close()


def test_target_artifact_entry_reader_reads_original_and_rejects_wrong_version(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        graph = runtime.owners.research_graph
        accepted = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph,
            graph_authority=graph).finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed", accepted
        from sqlalchemy import text
        with runtime._database.read() as connection:
            quest_ref = connection.execute(text("SELECT quest_ref FROM rg_target_graphs WHERE graph_ref=:ref"), {"ref": authority.graph_ref}).scalar_one()
        agent, context, state, content = _authorized_reader(runtime, quest_ref)
        handoff = _formal_results_read(graph, agent, context, {"target_ref": handle.target_ref})
        artifacts = [artifact for fact in handoff["items"] for artifact in fact["run_artifacts"]]
        assert artifacts, handoff
        artifact = next(item for item in artifacts if item["role"] == "log_asset")
        reader = artifact["reader"]
        assert reader["operation"] == "research_memory.content.read"
        assert content.handler(context, reader["args"])["text"] == "epoch 1 complete\n"
        exact = _formal_result_by_ref_read(graph, agent, context, {"ref": handoff["items"][0]["variant_run_ref"]})
        assert exact["result"]["run_artifacts"] == handoff["items"][0]["run_artifacts"]
        assert _formal_result_by_ref_read(graph, agent, context, {"ref": "absent"}) == {"status": "not_found", "result": None}
        foreign = SimpleNamespace(verify_root_agent_runtime_scope=lambda **_: {"quest_ref": "foreign"})
        with pytest.raises(SemanticMcpError, match="formal_result_quest_scope_invalid"):
            _formal_results_read(graph, foreign, context, {"target_ref": handle.target_ref})
        with pytest.raises(SemanticMcpError, match="content_.*unbound"):
            content.handler(context, {**reader["args"], "version_ref": "asset_version_wrong"})
        state["valid"] = False
        with pytest.raises(SemanticMcpError, match="scope_stale"):
            content.handler(context, reader["args"])
    finally:
        runtime.close()


def test_frozen_plan_reader_reconstructs_exact_current_authorized_plan(tmp_path):
    runtime = _current_bundle_runtime(tmp_path / "plan")
    try:
        graph, _, run, _, _ = _ready_launch(runtime)
        request = runtime.owners.advancement_engine.query_bundle_stage_request(graph.cycle_ref)
        context = SimpleNamespace(root_kind="bundle", run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref, capability_binding_hash=run.runtime_binding_hash)
        arguments = {"context_pack_ref": request.context_pack_ref, "source": "context_pack",
            "path": ["accepted_formal_plan_binding", "plan_document"], "offset": 0, "limit": 127}
        chunks = []
        while True:
            page = read_stage_context(runtime.owners.advancement_engine, runtime.owners.agent_runtime,
                runtime.owners.research_graph, runtime.owners.research_memory, context, arguments)
            assert page["content_hash"] == canonical_hash(request.accepted_formal_plan.plan_document)
            chunks.append(page["text"].encode())
            if page["next_offset"] is None:
                break
            arguments["offset"] = page["next_offset"]
        assert b"".join(chunks) == canonical_json(request.accepted_formal_plan.plan_document).encode()
        with pytest.raises(SemanticMcpError, match="stage_context_ref_unbound"):
            read_stage_context(runtime.owners.advancement_engine, runtime.owners.agent_runtime,
                runtime.owners.research_graph, runtime.owners.research_memory, context,
                {**arguments, "context_pack_ref": "wrong-version"})
        context.fence_ref = "old-fence"
        with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
            read_stage_context(runtime.owners.advancement_engine, runtime.owners.agent_runtime,
                runtime.owners.research_graph, runtime.owners.research_memory, context, arguments)
    finally:
        runtime.close()


def test_absolute_project_python_reads_frozen_manifest_without_path(tmp_path):
    from meta_research.target_execution_contract import target_execution_reading_tools
    path = tmp_path / "frozen manifest.json"
    original = '{"evidence":"原行", "missing":null}\n'.encode()
    path.write_bytes(original)
    tools = target_execution_reading_tools(str(path))
    assert Path(tools["project_python"]).is_absolute()
    env = {**os.environ, "PATH": ""}
    with pytest.raises(FileNotFoundError):
        subprocess.run(["python", "-c", "pass"], env=env, check=True)
    if os.name != "nt":
        missing = subprocess.run(["/bin/sh", "-c", "cat /dev/null"], env=env, capture_output=True)
        assert missing.returncode == 127
    result = subprocess.run(tools["read_frozen_manifest_argv"], env=env, capture_output=True, check=True)
    assert result.stdout == original
