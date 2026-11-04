import json

import pytest
from sqlalchemy import event, text

from meta_research.owners import research_graph as graph_module
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError
from test_bundle_exact_sources import _arguments, _sources
from test_bundle_report_receipt_order import committed_targets


def test_exact_commit_pages_only_materialize_requested_records(committed_targets, monkeypatch):
    runtime, _, context, operation, request = _sources(committed_targets)
    decoded = []
    selected_rows = []
    queries = []
    expected = []
    original = graph_module._target_commit
    original_query = runtime.owners.research_graph.query_target_commits

    def decode(row):
        decoded.append(row.commit_ref)
        return original(row)

    def query(*args, **kwargs):
        start = len(decoded)
        result = original_query(*args, **kwargs)
        selected_rows.extend(decoded[start:])
        return result

    def observe(_connection, _cursor, statement, parameters, _context, _many):
        if "SELECT c.* FROM rg_target_commits c JOIN rg_targets t ON" in statement:
            queries.append((statement, parameters))

    monkeypatch.setattr(graph_module, "_target_commit", decode)
    monkeypatch.setattr(runtime.owners.research_graph, "query_target_commits", query)
    event.listen(runtime._database._engine, "before_cursor_execute", observe)
    try:
        for record in request.target_commits:
            arguments = _arguments(request, "bundle_target_commit", record["commit_ref"], record)
            arguments["limit"] = 4096
            chunks = []
            while True:
                page = operation.handler(context, arguments)
                expected.append(record["commit_ref"])
                chunks.append(page["text"])
                assert page["content_hash"] == canonical_hash(record)
                if page["next_offset"] is None:
                    break
                arguments = {**arguments, "offset": page["next_offset"]}
            assert "".join(chunks) == canonical_json(record)
    finally:
        event.remove(runtime._database._engine, "before_cursor_execute", observe)
    print(json.dumps({"pages": len(expected), "commit_queries": len(queries),
                      "source_query_decoded_rows": len(selected_rows),
                      "all_commit_decodes": len(decoded)}))
    assert selected_rows == expected
    assert len(queries) == len(expected)
    assert all("c.commit_ref =" in statement for statement, _ in queries)


def test_selected_commit_retains_graph_scope_and_custody_validation(committed_targets):
    runtime, graph, context, operation, request = _sources(committed_targets)
    owner = runtime.owners.research_graph
    record = request.target_commits[0]
    commit_ref = record["commit_ref"]
    selected = owner.query_target_commits(graph.graph_ref, commit_ref=commit_ref)
    assert tuple(commit.commit_ref for commit in selected) == (commit_ref,)
    assert owner.query_target_commits("foreign-graph", commit_ref=commit_ref) == ()
    assert owner.query_target_commits(graph.graph_ref, commit_ref="missing-commit") == ()
    arguments = _arguments(request, "bundle_target_commit", commit_ref, record)
    first = operation.handler(context, {**arguments, "limit": 127})
    assert first["text"] == canonical_json(record).encode()[:127].decode(errors="ignore")
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE rg_target_commits SET closure_hash=:bad WHERE commit_ref=:ref"),
                           {"bad": "0" * 64, "ref": commit_ref})
    with pytest.raises(OwnerConflict, match="target_commit_invalid"):
        owner.query_target_commits(graph.graph_ref, commit_ref=commit_ref)
    with pytest.raises(SemanticMcpError, match="target_commit_invalid"):
        operation.handler(context, {**arguments, "offset": 127})
