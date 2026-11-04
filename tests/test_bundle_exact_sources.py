from types import SimpleNamespace as NS

import pytest

from meta_research.bundle_batch_access import verified_batch_source_readers
from meta_research.bundle_source_projection import target_projection, target_commit_projection
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError, SemanticMcpGateway
from meta_research.stage_context_access import stage_context_operations
from test_bundle_report_receipt_order import committed_targets


def _sources(fixture):
    runtime, graph, run, *_ = fixture
    owner = runtime.owners
    stage = owner.advancement_engine.query_bundle_stage_request(graph.cycle_ref)
    run = owner.agent_runtime.query_bundle_stage_run(stage.request_ref)
    context = NS(root_kind="bundle", phase="target-batch", run_ref=run.run_ref,
        attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
        fence_ref=run.fence_ref, capability_binding_hash=run.runtime_binding_hash)
    operation = stage_context_operations(advancement_engine=owner.advancement_engine,
        agent_runtime=owner.agent_runtime, research_graph=owner.research_graph,
        research_memory=owner.research_memory)[0]
    request = NS(cycle_ref=graph.cycle_ref, graph_ref=graph.graph_ref,
        stage_request_ref=stage.request_ref, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
        root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
        native_session_ref=run.native_session_ref, runtime_binding=run.runtime_binding,
        base_generation=graph.head_generation, context_pack_ref=graph.context_pack_ref,
        context_pack_hash=graph.context_pack_hash, initial_target_plan=graph.target_plan,
        current_targets=tuple(target_projection(target) for target in graph.targets),
        target_commits=tuple(target_commit_projection(commit)
            for commit in owner.research_graph.query_target_commits(graph.graph_ref)))
    return runtime, graph, context, operation, request


def _read(operation, context, arguments):
    chunks = []
    offset = 0
    while True:
        page = operation.handler(context, {**arguments, "offset": offset})
        chunks.append(page["text"])
        if page["next_offset"] is None:
            return "".join(chunks), page
        offset = page["next_offset"]


def _arguments(request, source, ref, value, path=()):
    return {"context_pack_ref": request.context_pack_ref, "source": source,
        "source_ref": ref, "version_ref": canonical_hash(value), "path": list(path),
        "offset": 0, "limit": 16384}


def test_real_owners_read_initial_contract_and_all_historical_records(committed_targets):
    runtime, graph, context, operation, request = _sources(committed_targets)
    assert "initial_target_plan" not in runtime.owners.advancement_engine.query_bundle_stage_request(graph.cycle_ref).context_pack
    initial = _arguments(request, "bundle_initial_target_plan", graph.graph_ref, graph.target_plan)
    text, page = _read(operation, context, initial)
    assert text == canonical_json(graph.target_plan)
    assert page["content_hash"] == canonical_hash(graph.target_plan)
    text, page = _read(operation, context, {**initial, "path": ["completion_contract"]})
    assert text == canonical_json(graph.target_plan["completion_contract"])
    assert page["content_hash"] == canonical_hash(graph.target_plan["completion_contract"])
    for source, ref_key, records in (
        ("bundle_target", "target_ref", request.current_targets),
        ("bundle_target_commit", "commit_ref", request.target_commits),
    ):
        assert len(records) == 2
        for record in records:
            text, page = _read(operation, context, _arguments(request, source, record[ref_key], record))
            assert text == canonical_json(record)
            assert page["content_hash"] == canonical_hash(record)


def test_exact_sources_reject_wrong_versions_refs_scope_and_revocation(committed_targets, monkeypatch):
    runtime, graph, context, operation, request = _sources(committed_targets)
    arguments = _arguments(request, "bundle_initial_target_plan", graph.graph_ref, graph.target_plan)
    arguments["limit"] = 127
    operation.handler(context, arguments)
    for changed, code in (
        ({"version_ref": "0" * 64}, "stage_bundle_version_unbound"),
        ({"source_ref": "foreign-graph"}, "stage_bundle_source_unbound"),
        ({"context_pack_ref": "foreign-context"}, "stage_context_ref_unbound"),
    ):
        with pytest.raises(SemanticMcpError, match=code):
            operation.handler(context, {**arguments, **changed})
    for record in request.target_commits:
        with pytest.raises(SemanticMcpError, match="stage_bundle_version_unbound"):
            operation.handler(context, {**_arguments(request, "bundle_target_commit", record["commit_ref"], record), "version_ref": "f" * 64})
    stale = NS(**{**vars(context), "fence_ref": "old-fence"})
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        operation.handler(stale, arguments)
    def revoked(**_):
        raise OwnerConflict("root_agent_runtime_scope_stale")
    monkeypatch.setattr(runtime.owners.agent_runtime, "verify_root_agent_runtime_scope", revoked)
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        operation.handler(context, {**arguments, "offset": 127})


def test_host_proof_round_trips_model_callable_descriptors(committed_targets):
    runtime, graph, context, operation, request = _sources(committed_targets)
    owner = runtime.owners
    readers = verified_batch_source_readers(request, advancement_engine=owner.advancement_engine,
        agent_runtime=owner.agent_runtime, research_graph=owner.research_graph,
        research_memory=owner.research_memory)
    expected = [(readers["completion_contract"], graph.target_plan["completion_contract"])]
    expected.extend((readers["current_targets"][record["target_ref"]], record)
                    for record in request.current_targets)
    expected.extend((readers["target_commits"][record["commit_ref"]], record)
                    for record in request.target_commits)
    gateway = SemanticMcpGateway((operation,))
    channel, _ = gateway.issue_channel(run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        capability_binding_hash=context.capability_binding_hash, root_kind="bundle",
        phase="target-batch", operation_ids=gateway.operation_ids)
    for reader, document in expected:
        assert reader["semantic_operation_id"] == operation.semantic_operation_id
        chunks = []
        offset = 0
        while True:
            status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": "exact-source",
                "method": "tools/call", "params": {"name": reader["semantic_operation_id"],
                "arguments": {**reader["arguments"], "offset": offset}}})
            assert status == 200
            assert response["result"]["isError"] is False, response
            page = response["result"]["structuredContent"]
            chunks.append(page["text"])
            offset = page["next_offset"]
            if offset is None:
                break
        assert "".join(chunks) == canonical_json(document)
        assert page["content_hash"] == reader["content_hash"]


def test_old_exact_records_survive_graph_head_advance(committed_targets):
    runtime, graph, context, operation, request = _sources(committed_targets)
    record = request.target_commits[0]
    arguments = _arguments(request, "bundle_target_commit", record["commit_ref"], record)
    before, _ = _read(operation, context, arguments)
    for _ in range(8):
        runtime.bundle_stage.process_once()
        current = runtime.owners.research_graph.query_target_graph(graph.request_ref)
        if current.head_generation != graph.head_generation:
            break
    assert current.head_generation > graph.head_generation
    assert _read(operation, context, arguments)[0] == before == canonical_json(record)


def test_corrupt_graph_and_commit_authority_fail_even_after_cached_page(committed_targets, monkeypatch):
    runtime, graph, context, operation, request = _sources(committed_targets)
    record = request.target_commits[0]
    arguments = _arguments(request, "bundle_target_commit", record["commit_ref"], record)
    arguments["limit"] = 127
    operation.handler(context, arguments)
    monkeypatch.setattr(runtime.owners.research_graph, "query_target_frontier_commit_transition", lambda _: None)
    with pytest.raises(SemanticMcpError, match="stage_bundle_source_unbound"):
        operation.handler(context, {**arguments, "offset": 127})
    def corrupt(**_):
        raise OwnerConflict("target_graph_receipt_invalid")
    monkeypatch.setattr(runtime.owners.research_graph, "verify_target_graph_receipt", corrupt)
    with pytest.raises(SemanticMcpError, match="target_graph_receipt_invalid"):
        operation.handler(context, arguments)


def test_authorized_unavailable_path_is_selective_but_identity_conflicts_fail(committed_targets, monkeypatch):
    import meta_research.bundle_batch_access as access
    runtime, _, _, _, request = _sources(committed_targets)
    owner = runtime.owners
    original = access.read_stage_context
    code = "stage_context_path_invalid"
    first_ref = request.current_targets[0]["target_ref"]
    def unavailable(ae, ar, rg, rm, context, arguments, **kwargs):
        page = original(ae, ar, rg, rm, context, arguments, **kwargs)
        if arguments.get("source_ref") == first_ref:
            raise SemanticMcpError(code)
        return page
    monkeypatch.setattr(access, "read_stage_context", unavailable)
    dependencies = dict(advancement_engine=owner.advancement_engine, agent_runtime=owner.agent_runtime,
        research_graph=owner.research_graph, research_memory=owner.research_memory)
    readers = access.verified_batch_source_readers(request, **dependencies)
    assert readers["current_targets"][first_ref] is None
    assert readers["completion_contract"]["content_hash"] == canonical_hash(request.initial_target_plan["completion_contract"])
    assert all(readers["target_commits"].values())
    for code in ("stage_bundle_source_unbound", "stage_bundle_version_unbound", "semantic_call_scope_stale"):
        with pytest.raises(SemanticMcpError, match=code):
            access.verified_batch_source_readers(request, **dependencies)
