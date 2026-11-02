"""Retire the Target thread first learned from an Owner-bound Harness start."""
from meta_research.owners.common import canonical_hash, canonical_json
from test_completed_workspace_cleanup import _complete
from test_native_session_cleanup import _saved_threads
from test_codex_shell_environment import _vendor


def test_cleanup_uses_unambiguous_start_without_command_output(tmp_path):
    runtime, handle, *_ = _complete(tmp_path)
    try:
        run = handle.target_run_ref
        native, switched, corrupt, foreign, ambiguous, other = _saved_threads(
            _vendor(), runtime.data_root, ["initial", "current", "invalid hash", "foreign fence", "ambiguous", "other"])
        with runtime._database.fenced_write() as c:
            lease = c.exec_driver_sql(
                "SELECT * FROM ar_target_run_workspaces WHERE target_run_ref=?", (run,)).mappings().one()
            scope = {"schema_ref": "meta-research/target-root-observation-scope/v2",
                     "target_run_ref": run, "attempt_ref": lease["target_attempt_ref"],
                     "attempt_generation": lease["ordinal"], "root_session_ref": lease["root_session_ref"],
                     "fence_ref": lease["target_fence_ref"], "native_session_ref": None,
                     "target_workspace_ref": lease["workspace_ref"]}
            for generation, starts in enumerate(((native,), (corrupt,), (foreign,), (ambiguous, other)), 1):
                operation = f"{run}:harness_turn:{generation}"
                c.exec_driver_sql("INSERT INTO ar_harness_provider_operations "
                    "(operation_ref,run_ref,generation,invocation_hash,status,outcome_code,created_at,completed_at) "
                    "VALUES (?,?,?,?,'executed','target_root_human_request_wait',1,2)",
                    (operation, run, generation, canonical_hash(operation)))
                bound = {**scope, "fence_ref": "foreign-fence"} if starts == (foreign,) else scope
                for seq, thread in enumerate(starts, 1):
                    summary = {"kind": "thread.started", "native_session_ref": thread, "target_run_scope": bound}
                    ref = "harness_evidence:" + canonical_hash({"evidence_scope_ref": canonical_hash(operation),
                                                              "sequence": seq, "summary": summary})
                    event = {"event_ref": ref, "sequence": seq, **summary}
                    digest = "0" * 64 if thread == corrupt else canonical_hash(event)
                    c.exec_driver_sql("INSERT INTO ar_harness_evidence_events VALUES (?,?,?,?,?,1)",
                                      (ref, operation, seq, canonical_json(event), digest))
            c.exec_driver_sql("UPDATE ar_harness_runs SET native_session_ref=? WHERE run_ref=?", (switched, run))
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=True, limit=100)
        planned = {item["session_ref"] for item in report
                   if item["root_kind"] == "native_session" and item["action"] == "candidate"}
        assert native in planned and switched in planned
        assert planned.isdisjoint({corrupt, foreign, ambiguous, other})
    finally:
        runtime.close()
