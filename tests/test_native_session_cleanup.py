"""Physical cleanup against the locked Codex CLI in a fresh deployment home."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading

from meta_research.paths import prepare_data_root
from test_codex_shell_environment import _vendor


@contextmanager
def _server(executable, data_root):
    # No model is invoked. inject_items persists a local saved thread.
    process = subprocess.Popen([executable, "app-server"], cwd=data_root.root,
        env={**os.environ, **data_root.codex_environment},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True)
    responses = queue.Queue()
    threading.Thread(target=lambda: [responses.put(json.loads(line)) for line in process.stdout],
                     daemon=True).start()
    serial = 0

    def rpc(method, params):
        nonlocal serial
        serial += 1
        process.stdin.write(json.dumps({"id": serial, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while True:
            reply = responses.get(timeout=35)
            if reply.get("id") == serial:
                assert "error" not in reply, reply
                return reply["result"]
    try:
        rpc("initialize", {"clientInfo": {"name": "lifecycle-test", "version": "1"},
                           "capabilities": {"experimentalApi": True}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        yield rpc
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _saved_threads(executable, root, messages):
    (root.codex_home / "auth.json").unlink(missing_ok=True)
    with _server(executable, root) as call:
        ids = []
        for message in messages:
            ref = call("thread/start", {"cwd": str(root.root)})["thread"]["id"]
            call("thread/inject_items", {"threadId": ref, "items": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": message}]}]})
            ids.append(ref)
        return ids


def test_owned_retired_thread_is_deleted_and_other_thread_still_resumes(tmp_path):
    executable = _vendor()
    root = prepare_data_root(tmp_path / "instance")
    threads = _saved_threads(executable, root, ["retired work", "resumable work"])
    retired, active = threads
    state = root.codex_home / "state_5.sqlite"
    with sqlite3.connect(state) as db:
        paths = {ref: Path(path) for ref, path in db.execute(
            "SELECT id, rollout_path FROM threads WHERE id IN (?, ?)", threads)}
    assert paths[retired].is_file() and paths[active].is_file()
    from meta_research.native_session_cleanup import delete_codex_sessions
    dry, = delete_codex_sessions(root, str(executable), [retired], dry_run=True)
    assert dry["action"] == "candidate" and dry["bytes"] == paths[retired].stat().st_size
    deleted, = delete_codex_sessions(root, str(executable), [retired], dry_run=False)
    assert deleted["action"] == "removed" and deleted["bytes"] > 0
    assert not paths[retired].exists()
    with sqlite3.connect(state) as db:
        assert list(db.execute("SELECT id FROM threads WHERE id IN (?, ?)", threads)) == [(active,)]
    with _server(executable, root) as call:
        assert call("thread/resume", {"threadId": active})["thread"]["id"] == active
        assert call("thread/read", {"threadId": active})["thread"]["id"] == active


def test_open_grandchild_or_retained_child_prevents_cli_cascade(tmp_path):
    from meta_research.native_session_cleanup import delete_codex_sessions

    executable = _vendor()
    root = prepare_data_root(tmp_path / "instance")
    ids = _saved_threads(executable, root, ["parent", "child", "grandchild"])
    parent, child, grandchild = ids
    state = root.codex_home / "state_5.sqlite"
    with sqlite3.connect(state) as db:
        paths = {ref: Path(path) for ref, path in db.execute("SELECT id,rollout_path FROM threads")}
        db.executemany("INSERT INTO thread_spawn_edges VALUES (?,?,?)",
                       [(parent, child, "closed"), (child, grandchild, "open")])
    records = delete_codex_sessions(root, str(executable), [parent], dry_run=False)
    assert {r["session_ref"] for r in records if r["action"] == "skipped"} == set(ids)
    with sqlite3.connect(state) as db:
        db.execute("UPDATE thread_spawn_edges SET status='closed' WHERE child_thread_id=?", (grandchild,))
    records = delete_codex_sessions(root, str(executable), [parent], dry_run=False,
                                    protected_paths=[paths[grandchild]])
    assert all(r["action"] == "skipped" for r in records)
    assert all(path.is_file() for path in paths.values())
    for _ in range(5):
        delete_codex_sessions(root, str(executable), [parent], dry_run=False)
        if not any(path.exists() for path in paths.values()):
            break
    assert not any(path.exists() for path in paths.values())
    with sqlite3.connect(state) as db:
        assert not list(db.execute("SELECT id FROM threads"))


def test_public_cleanup_reclaims_retired_cycle_and_companion_but_preserves_spool_history(tmp_path):
    from meta_research.provider_supervisor import ensure_transport_key, write_transport_envelope, write_exit_receipt
    from meta_research.owners.common import canonical_hash, canonical_json
    from dataclasses import replace
    from test_completed_workspace_cleanup import _complete, _cleanup_runtime, _WorkspaceIdea
    from meta_research.timeline_summaries import TimelineSummaryStore
    from test_timeline_summaries import node

    class Idea(_WorkspaceIdea):
        def generate_draft(self, request):
            return replace(super().generate_draft(request), primary_session_ref=retired_stage)

    runtime = _cleanup_runtime(tmp_path / "cleanup-root", idea=Idea())
    try:
        root = runtime.data_root
        exe = _vendor()
        ids = _saved_threads(exe, root, ["Stage", "old Companion", "Companion", "old Recorder", "Recorder", "previous Target", "final Target"])
        retired_stage, retired_companion, current, retired_recorder, current_recorder, previous_target, final_target = ids
        _, handle, *_ = _complete(tmp_path, runtime=runtime)
        store = TimelineSummaryStore(root.root / "research-recorder" / "summaries.sqlite3")
        for i, native in enumerate((retired_recorder, current_recorder)):
            item = node(f"change {i}")
            store.sync("recorder-quest", [item], 100 + i)
            job = store.claim(100 + i, "zh")
            store.publish(job, [dict(node_key=item["node_key"], source_hash=item["source_hash"],
                                    summary="updated", source_refs=[])], native, 100 + i)
        with sqlite3.connect(root.codex_home / "state_5.sqlite") as db:
            paths = {ref: Path(path) for ref, path in db.execute("SELECT id,rollout_path FROM threads")}
        graph = runtime.owners.research_graph.query_question_tree()
        scope = "quest:" + graph[0].quest_ref
        human = runtime.owners.human_collaboration
        session = human.query_companion(scope)["session_ref"]
        with runtime._database.fenced_write() as c:
            c.exec_driver_sql("UPDATE hc_companion_native_sessions SET native_session_ref=? WHERE session_ref=?",
                              (retired_companion, session))
            c.exec_driver_sql("UPDATE hc_companion_sessions SET native_session_ref=? WHERE session_ref=?",
                              (retired_companion, session))
        human.start_new_companion_session(scope, "rotate-integration")
        with runtime._database.fenced_write() as c:
            c.exec_driver_sql("UPDATE hc_companion_sessions SET native_session_ref=? WHERE session_ref=?", (current, session))
            c.exec_driver_sql("UPDATE hc_companion_native_sessions SET native_session_ref=? WHERE session_ref=? AND generation=2",
                              (current, session))
            run = c.exec_driver_sql("SELECT run_ref FROM ar_stage_runs WHERE cycle_ref=? AND stage='idea'",
                                    (runtime.cleanup_test_cycle_ref,)).scalar_one()
            operation = c.exec_driver_sql("SELECT operation_ref FROM ar_provider_units WHERE run_ref=? AND unit_kind='idea_primary'",
                                          (run,)).scalar_one()
        provider = root.root / "idea-skill-provider"
        _, key = ensure_transport_key(provider)
        directory = provider / "provider-operations" / canonical_hash({"job_ref": operation}) / "primary"
        directory.mkdir(parents=True, exist_ok=True)
        invocation = {"schema_ref": "meta-research/codex-provider-operation/v3", "operation_name": "primary", "job_ref": operation}
        write_transport_envelope(directory / "invocation.json", invocation, key)
        raw = b'{"type":"thread.started","thread_id":"' + retired_stage.encode() + b'"}\n'
        (directory / "stdout.jsonl").write_bytes(raw)
        (directory / "prompt.txt").write_bytes(b"stage prompt")
        (directory / "output-schema.json").write_text("{}")
        write_exit_receipt(directory / "supervisor-exit.json", key=key, invocation_hash=canonical_hash(invocation),
            prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
            returncode=0, input_bytes=len(b"stage prompt"))
        scratch = directory / "native-provider-argv.json"
        scratch.write_bytes(b'x' * 2048)
        # Model the first history read after a restart, with no cached Target
        # binding/profile. The transport must reconstruct a signed receipt from
        # the still-readable argv, prompt, schema, result, and stdout.
        from meta_research.harness_adapters import HarnessSupervisorTransport
        target_op = handle.target_run_ref + ":harness_turn:1"
        owner_hash = canonical_hash({"operation": target_op})
        with runtime._database.fenced_write() as c:
            c.exec_driver_sql("INSERT INTO ar_harness_provider_operations "
                "(operation_ref, run_ref, generation, invocation_hash, status, outcome_code, created_at, completed_at) "
                "VALUES (?, ?, 1, ?, 'executed','target_root_human_request_wait',1,2)",
                (target_op, handle.target_run_ref, owner_hash))
        transport = HarnessSupervisorTransport(root.run / "harness-supervisor")
        environment = {"META_RESEARCH_MCP_TOKEN": "fixture-token", "META_RESEARCH_HARNESS_FAMILY": "codex",
            "META_RESEARCH_HARNESS_WORKSPACE": str(root.root), "META_RESEARCH_PROVIDER_OPERATION_REF": target_op,
            "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "a"*64, "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": "{}",
            "NO_PROXY": "", "no_proxy": "", "META_RESEARCH_OWNER_INVOCATION_HASH": owner_hash}
        result = transport(["/bin/cat"], raw.decode(), 10, environment)
        assert result.returncode == 0
        # Construct a retired lease in the same Run, carrying an independently
        # allocated native root. Only its historical observation binds the ID.
        from test_target_root_observations import _event
        lease = "retired-target-fixture"
        name = canonical_hash({"workspace_ref": lease})
        old_directory = runtime.target_run_runtime._target_agent._workspace_root / name
        old_directory.mkdir()
        pending = old_directory / ".target-completion-intakes"
        pending.mkdir()
        with runtime._database.fenced_write() as c:
            from meta_research.owners.target_run_runtime import _receipt, AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND
            payload = {"target_ref": handle.target_ref, "target_run_ref": handle.target_run_ref,
                "root_session_ref": "prior-logical-root", "target_attempt_ref": "prior-attempt",
                "target_fence_ref": "prior-fence", "implementation_relative_path": "implementation",
                "inputs_relative_path": "inputs"}
            receipt = _receipt("agent_runtime", AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND, "prior-receipt", lease,
                {**payload, "workspace_ref": lease, "ordinal": 2, "root_name": name, "payload_hash": canonical_hash(payload)})
            c.exec_driver_sql("INSERT INTO ar_target_run_workspaces SELECT ?,target_ref,target_run_ref,"
                "'prior-logical-root','prior-attempt','prior-fence',2,?,'retired',?,?,"
                "'older-lease',?,'prior-receipt',?,created_at FROM ar_target_run_workspaces WHERE target_run_ref=?",
                (lease, name, canonical_json(payload), canonical_hash(payload),
                 canonical_hash({"command": "reserve_target_run_workspace", **payload}), receipt.payload_hash, handle.target_run_ref))
            event_scope = {"schema_ref": "meta-research/target-root-observation-scope/v2",
                "target_run_ref": handle.target_run_ref, "attempt_ref": "prior-attempt", "attempt_generation": 1,
                "root_session_ref": "prior-logical-root", "fence_ref": "prior-fence", "native_session_ref": None,
                "target_workspace_ref": lease}
            event = _event(1, "historical run", scope=event_scope)
            event["target_root_observation"]["root_native_session_ref"] = previous_target
            c.exec_driver_sql("INSERT INTO ar_harness_evidence_events VALUES (?,?,?,?,?,1)",
                (event["event_ref"], target_op, event["sequence"], canonical_json(event), canonical_hash(event)))
            c.exec_driver_sql("UPDATE ar_harness_runs SET native_session_ref=? WHERE run_ref=?",
                              (final_target, handle.target_run_ref))
        # Page size one: even the page containing only the other lease must
        # respect this intake before deleting either thread or either spool.
        seen = set()
        for _ in range(8):
            page = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=True, limit=1)
            for item in page:
                if item["root_kind"] == "target":
                    seen.add(item["workspace_ref"])
                    assert item.get("native_cleanup_reason", item["reason"]) == "pending_finalizer_intake"
                    assert all(r["root_kind"] != "provider_spool" for r in page)
            if len(seen) == 2:
                break
        assert len(seen) == 2
        assert paths[previous_target].exists() and paths[final_target].exists()
        pending.rmdir()
        retired = {retired_stage, retired_companion, retired_recorder, previous_target, final_target}
        report = ()
        for _ in range(6):
            report += runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, limit=100)
            with sqlite3.connect(root.codex_home / "state_5.sqlite") as db:
                saved = {r[0] for r in db.execute("SELECT id FROM threads")}
            if not retired.intersection(saved) and not any(paths[ref].exists() for ref in retired):
                break
        assert not retired.intersection(saved)
        assert not any(paths[ref].exists() for ref in retired)
        assert retired <= {r["session_ref"] for r in report if r["root_kind"] == "native_session"
                           and r["reason"] in {"retired_native_session", "native_cleanup_deferred"}}
        recovered = HarnessSupervisorTransport(root.run / "harness-supervisor").recover_transport_receipt(
            target_op, family="codex")
        assert recovered == result.meta_research_transport_receipt
        assert paths[current_recorder].is_file() and not paths[retired_recorder].exists()
        assert not paths[retired_stage].exists() and not paths[retired_companion].exists()
        assert paths[current].is_file()
        assert not scratch.exists() and (directory / "stdout.jsonl").read_bytes() == raw
        assert runtime.query_stage_raw_output(run, phase="primary").text == raw.decode()
        assert any(r["root_kind"] == "provider_spool" and r["bytes"] == 2048 for r in report)
        with _server(exe, root) as call:
            assert call("thread/resume", {"threadId": current})["thread"]["id"] == current
            assert call("thread/resume", {"threadId": current_recorder})["thread"]["id"] == current_recorder
    finally:
        runtime.close()

def test_unindexed_rollout_is_reported_and_preserved_without_triggering_cascade(tmp_path):
    from meta_research.native_session_cleanup import delete_codex_sessions

    root = prepare_data_root(tmp_path / "unindexed")
    ref = "01956312-fe02-740c-9e70-cbb60faa176e"
    path = root.codex_archived_sessions / f"rollout-2026-11-02T12-00-00-{ref}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": ref}}) + "\n")
    report, = delete_codex_sessions(root, "/unused", [ref], dry_run=False)
    assert report["reason"] == "native_index_binding_absent" and path.is_file()
