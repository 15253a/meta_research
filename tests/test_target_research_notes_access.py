"""Target note reads compose the real MCP, AR scope and immutable RM store."""
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.research_notes import query_target_input_research_notes, query_target_research_notes
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.stage_context_access import stage_context_operations
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _root_finalizer_fixture


def test_current_target_reads_own_saved_note_through_real_mcp_without_stage_context(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, old = _root_finalizer_fixture(tmp_path)
    try:
        (workspace / "outputs/result.json").write_bytes((workspace / "outputs/metrics.json").read_bytes())
        analysis = workspace / "outputs/analysis"
        analysis.mkdir()
        body = "uncertain: the independent source is still missing."
        (analysis / "research-note.md").write_text(body, encoding="utf-8")
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        evidence = replace(old, handoff=None, workspace_ref=workspace_ref,
            final_text=body, final_text_sha256=hashlib.sha256(body.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph)
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "rm_accepted"
        operations = stage_context_operations(advancement_engine=runtime.owners.advancement_engine,
            agent_runtime=runtime.owners.agent_runtime, research_graph=runtime.owners.research_graph,
            research_memory=runtime.owners.research_memory)
        gateway = SemanticMcpGateway(operations)
        runtime.owners.agent_runtime.harness_runs.start_operation(
            run_ref=handle.target_run_ref, operation_ref="notes-reader-active-turn",
            generation=1, invocation_hash="a" * 64, resume=False)
        with runtime._database.read() as connection:
            binding_hash = connection.execute(text(
                "SELECT capability_binding_hash FROM ar_harness_runs WHERE run_ref=:ref"),
                {"ref": handle.target_run_ref}).scalar_one()
        credentials = dict(run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
            root_session_ref=handle.root_session_ref, fence_ref=handle.execution_fence_ref,
            capability_binding_hash=binding_hash, root_kind="target", phase="target_root",
            operation_ids=("research_memory.research_notes.read",))
        channel, _ = gateway.issue_channel(**credentials)
        def call(arguments, token=channel.token):
            status, result = gateway.dispatch(token, {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "research_memory.research_notes.read",
                    "arguments": arguments}})
            assert status == 200
            return result["result"]
        args = {"source": "research_notes", "path": [], "offset": 0, "limit": 16384}
        result = call(args)
        assert not result.get("isError"), result
        page = result["structuredContent"]
        catalog = json.loads(page["text"])
        assert len(catalog["items"]) == 2
        note = next(item for item in catalog["items"] if item["kind"] == "research_note")
        result = call({**args, "source": "research_note_body", "source_ref": note["version_ref"]})
        assert json.loads(result["structuredContent"]["text"])["body"] == body
        assert call({**args, "source": "research_note_body", "source_ref": "foreign-version"})["isError"]
        stale, _ = gateway.issue_channel(**{**credentials, "attempt_ref": "stale-attempt"})
        assert "semantic_call_scope_stale" in canonical_json(call(args, stale.token))
        reading = runtime.target_run_authorities.research_graph.query_target_reading_context(
            target_ref=handle.target_ref, research_note_directory=tmp_path / "reading")
        assert note["version_ref"] in {item["version_ref"] for item in reading["research_notes"]}
        assert "context_pack_ref" not in reading["research_notes_reader"]
        assert reading["research_notes_reader"]["operation"] == "research_memory.research_notes.read"
    finally:
        runtime.close()


@pytest.fixture
def note_catalog():
    engine = create_engine("sqlite://")
    assets = {}
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE rg_target_graphs(graph_ref TEXT,quest_ref TEXT)",
            "CREATE TABLE rg_targets(target_ref TEXT,graph_ref TEXT)",
            "CREATE TABLE rg_target_commits(commit_ref TEXT,target_ref TEXT)",
            "CREATE TABLE rm_target_research_notes(target_ref TEXT,version_ref TEXT,accepted_at REAL,completion_ref TEXT,source_evidence_ref TEXT,source_evidence_hash TEXT,note_json TEXT,note_hash TEXT)",
            "CREATE TABLE ar_target_root_completions(completion_ref TEXT,evidence_ref TEXT,evidence_content_hash TEXT)",
            "CREATE TABLE rm_target_root_completion_manifests(manifest_ref TEXT,target_ref TEXT,entries_json TEXT,entries_hash TEXT,accepted_at REAL)",
        ):
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO rg_target_graphs VALUES ('g','quest'),('foreign-g','foreign-quest')"))
        connection.execute(text("INSERT INTO rg_targets VALUES ('self','g'),('upstream','g'),('unselected','g'),('foreign','foreign-g')"))
        connection.execute(text("INSERT INTO rg_target_commits VALUES ('upstream-commit','upstream'),('foreign-commit','foreign')"))
        for target, count in (("self", 100), ("upstream", 2), ("unselected", 2), ("foreign", 1)):
            for i in range(count):
                version = f"{target}-{i:03}"
                body = ("uncertain:" + version).encode()
                content_hash = hashlib.sha256(body).hexdigest()
                note = {"target_ref": target, "version_ref": version, "asset_ref": "asset-" + version,
                    "asset_content_hash": content_hash, "asset_manifest_hash": content_hash,
                    "source_bytes_sha256": content_hash, "entry_path": None,
                    "summary": {"text": body.decode()}}
                assets[version] = (NS(asset_ref=note["asset_ref"], content_hash=content_hash,
                    manifest_hash=content_hash), body)
                connection.execute(text("INSERT INTO ar_target_root_completions VALUES (:ref,:ref,:hash)"),
                    {"ref": version, "hash": content_hash})
                connection.execute(text("INSERT INTO rm_target_research_notes VALUES (:target,:ref,:at,:ref,:ref,:hash,:note,:note_hash)"),
                    {"target": target, "ref": version, "at": i, "hash": content_hash,
                     "note": canonical_json(note), "note_hash": canonical_hash(note)})
    class Database:
        @contextmanager
        def read(self):
            with engine.connect() as connection:
                yield connection
    memory = NS(query_asset_version=lambda ref: assets[ref][0],
        materialize_asset=lambda ref: NS(content=assets[ref][1]))
    yield Database(), memory
    engine.dispose()


def test_target_catalog_pages_all_history_and_reads_only_frozen_sources(note_catalog):
    database, memory = note_catalog
    scope = dict(quest_ref="quest", target_ref="self", upstream_commit_refs=("upstream-commit",),
        input_version_refs=("unselected-000",))
    seen, offset = set(), 0
    while True:
        page = query_target_input_research_notes(database, memory, **scope, offset=offset)
        assert len(page["items"]) <= 12
        seen.update(item["version_ref"] for item in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == 103
    assert "self-000" in seen and "upstream-000" in seen and "unselected-000" in seen
    exact = query_target_input_research_notes(database, memory, **scope, version_ref="self-000")
    assert exact["body"] == "uncertain:self-000"
    for version in ("unselected-001", "foreign-000", "missing"):
        with pytest.raises(OwnerConflict, match="research_note_source_unbound"):
            query_target_input_research_notes(database, memory, **scope, version_ref=version)
    # Even an explicit selected asset cannot cross the authenticated Quest.
    with pytest.raises(OwnerConflict, match="research_note_source_unbound"):
        query_target_input_research_notes(database, memory,
            **{**scope, "input_version_refs": ("foreign-000",)}, version_ref="foreign-000")
    with pytest.raises(OwnerConflict, match="research_note_source_unbound"):
        query_target_input_research_notes(database, memory,
            **{**scope, "upstream_commit_refs": ("foreign-commit",)})


def test_current_manifest_note_precedes_older_backfilled_summaries(note_catalog):
    database, memory = note_catalog
    source = query_target_input_research_notes(database, memory,
        quest_ref="quest", target_ref="self", upstream_commit_refs=(), input_version_refs=(),
        version_ref="self-000")["reference"]
    entry = {"declared_relative_path": "outputs/analysis/research-note.md", "research_note": source,
        "binding": {"asset_ref": source["asset_ref"], "version_ref": source["version_ref"],
            "content_hash": source["asset_content_hash"], "manifest_hash": source["asset_manifest_hash"]}}
    with database.read() as connection:
        connection.execute(text("INSERT INTO rm_target_root_completion_manifests VALUES "
            "('latest','self',:entries,:hash,200)"),
            {"entries": canonical_json([entry]), "hash": canonical_hash([entry])})
        connection.commit()
    notes = query_target_research_notes(database, memory, "self")
    assert notes[0]["manifest_ref"] == "latest"
    assert notes[0]["version_ref"] == "self-000"
