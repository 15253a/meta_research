"""Reclaim only Owner-bound terminal work in this deployment's managed Codex home."""
from __future__ import annotations

from collections import OrderedDict
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import UUID

from sqlalchemy import bindparam, text

from meta_research.owners.common import canonical_hash
from meta_research.stage_session_retirement import retired_stage_ids
from meta_research.target_session_retirement import replaced_target_sessions
from meta_research.provider_supervisor import (ProviderSupervisorError, read_transport_envelope,
    read_transport_key_for_operation, SUPERVISOR_EXIT_SCHEMA, SUPERVISOR_EXIT_SCHEMA_V2)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


# A single sweep holds the research Owner writer while it revalidates and
# deletes. Share the CLI wait allowance across the whole fenced batch; a
# slow Harness must yield to research instead of blocking once per thread.
_NATIVE_DELETE_BATCH_SECONDS = 2.0


def delete_codex_sessions(root, executable, native_ids, *, dry_run, protected_paths=(), cursors=None):
    """Look up all candidate IDs together. Codex owns deletion of its associated metadata."""
    ids = {ref for ref in native_ids if _uuid(ref)}
    if not ids:
        return []
    state = root.codex_home / "state_5.sqlite"
    if (root.codex_home.resolve() != root.codex_home or state.is_symlink()
            or state.exists() and not state.is_file()):
        raise OSError("unsafe managed Codex database")
    rows, complete = [], False
    if state.exists():
        with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("BEGIN")
            complete = db.execute("SELECT 1 FROM backfill_state WHERE status='complete'").fetchone() is not None
            rows = list(db.execute("""
                WITH RECURSIVE owned(id) AS (
                    SELECT value FROM json_each(?)
                    UNION SELECT e.child_thread_id FROM thread_spawn_edges e JOIN owned o ON o.id=e.parent_thread_id
                ) SELECT o.id,t.rollout_path,e.parent_thread_id,e.status
                  FROM owned o LEFT JOIN threads t ON t.id=o.id
                  LEFT JOIN thread_spawn_edges e ON e.child_thread_id=o.id
            """, (json.dumps(sorted(ids)),)))
    # An absent index binding cannot prove the child set is complete. Do not
    # invoke the CLI (which can backfill the home), or rescan historical shards.
    existing = {ref for ref, _, _, _ in rows}
    rows.extend((ref, None, None, None) for ref in ids - existing)
    # Codex delete recursively deletes descendants even when their edges are
    # open. Remove verified leaves first; a single unverified/protected child
    # keeps its ancestors. Never ask delete to backfill missing ownership.
    children = {}
    for ref, _, ancestor, _ in rows:
        children.setdefault(ancestor, set()).add(ref)
    reports = []
    planned = {}
    for ref, locator, _, status in rows:
        if not locator:
            if ref in ids:
                reports.append({"root_kind": "native_session", "session_ref": ref, "path": None, "bytes": 0,
                                "action": "skipped", "reason": "native_index_binding_absent"})
            continue
        path = Path(locator)
        record = {"root_kind": "native_session", "session_ref": ref, "path": str(path),
                  "bytes": 0, "action": "skipped", "reason": "native_session_unavailable"}
        reports.append(record)
        if not complete:
            record["reason"] = "native_descendants_not_indexed"
            continue
        if status is not None and status != "closed":
            record["reason"] = "active_or_recoverable_child"
            continue
        try:
            if (not _uuid(ref) or not any(path.is_relative_to(base) for base in
                    (root.codex_sessions, root.codex_archived_sessions))
                    or path.resolve(strict=True) != path or not path.is_file()
                    or not path.name.endswith("-" + ref + ".jsonl")):
                continue
            with path.open("rb") as stream:
                header = json.loads(stream.readline(4 * 1024 * 1024))
            if header.get("type") != "session_meta" or header["payload"]["id"] != ref:
                continue
            if any(path == origin or origin in path.parents or origin == state for origin in protected_paths):
                record["reason"] = "protected_original"
                continue
            planned[ref] = (path, record)
        except (ValueError, KeyError, AttributeError, TypeError, OSError):
            continue

    # Resume after the last CLI attempt in this batch, not after the last row
    # we merely reported as deferred. Other pages and dry-runs do not move it.
    batch = canonical_hash(sorted(ids)) if cursors is not None else None
    after = cursors.get(batch, "") if cursors is not None else ""
    last_attempted = None
    deadline = time.monotonic() + _NATIVE_DELETE_BATCH_SECONDS
    cleared = set()
    while planned:
        leaves = [ref for ref in planned if not children.get(ref, set()) - cleared]
        if not leaves:
            for _, record in planned.values():
                record["reason"] = "dependent_native_child"
            break
        for ref in sorted(leaves, key=lambda ref: (ref <= after, ref)):
            path, record = planned.pop(ref)
            try:
                size = path.stat().st_size
                if not dry_run:
                    if time.monotonic() >= deadline:
                        record["reason"] = "native_cleanup_deferred"
                        continue
                    # Prevent the recursive CLI from acting on a newly attached child,
                    # changed locator, or a missing binding that would trigger a rescan.
                    with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                        leaf = db.execute("""SELECT 1 FROM threads WHERE id=? AND rollout_path=?
                            AND NOT EXISTS (SELECT 1 FROM thread_spawn_edges WHERE parent_thread_id=?)
                            AND NOT EXISTS (SELECT 1 FROM thread_spawn_edges WHERE child_thread_id=?
                                            AND status != 'closed')
                            AND EXISTS (SELECT 1 FROM backfill_state WHERE status='complete')""",
                            (ref, str(path), ref, ref)).fetchone()
                    if not leaf or path.resolve(strict=True) != path:
                        record["reason"] = "native_binding_changed"
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        record["reason"] = "native_cleanup_deferred"
                        continue
                    last_attempted = ref
                    subprocess.run([executable, "delete", "--force", ref], check=True,
                                   cwd=root.root, env={**os.environ, **root.codex_environment},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=remaining)
                    if path.exists():
                        continue
                cleared.add(ref)
                record.update(bytes=size, action="candidate" if dry_run else "removed",
                              reason="retired_native_session")
            except subprocess.TimeoutExpired:
                # The CLI can finish the deletion before its process exits.
                # Report only a verified effect; otherwise recheck next sweep.
                record["reason"] = "native_cleanup_deferred"
                if not path.exists():
                    try:
                        with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=0) as db:
                            absent = db.execute("SELECT 1 FROM threads WHERE id=?", (ref,)).fetchone() is None
                    except sqlite3.Error:
                        absent = False
                    if absent:
                        cleared.add(ref)
                        record.update(bytes=size, action="removed", reason="retired_native_session")
            except (OSError, sqlite3.Error, subprocess.SubprocessError):
                record["reason"] = "native_delete_unavailable"
    if cursors is not None and not dry_run:
        cursors.pop(batch, None)
        if last_attempted and any(r["reason"] == "native_cleanup_deferred" for r in reports):
            cursors[batch] = last_attempted
            if len(cursors) > 256:
                cursors.popitem(last=False)
    return reports


def _query_runs(connection, sql, work):
    return connection.execute(text(sql).bindparams(bindparam("runs", expanding=True)),
                              {"runs": tuple(work)})


def cleanup_cycle_sessions(runtime, connection, candidates, *, dry_run, protected_paths=()):
    """Called under the Owner fence after the same window and dependencies as file cleanup."""
    cleanup = runtime._native_cleanup
    work = {item["work_ref"]: item["root_kind"] for item in candidates}
    if cleanup is None or not work:
        return []
    native = _query_runs(connection, """
        SELECT native_session_ref FROM ar_stage_sessions
        WHERE run_ref IN :runs AND status='completed'
        UNION SELECT native_session_ref FROM ar_harness_runs
        WHERE run_ref IN :runs AND harness_family='codex'
        UNION SELECT c.native_child_session_ref FROM ar_target_harness_child_sessions c
        JOIN ar_harness_runs h ON h.run_ref=c.target_run_ref AND h.harness_family='codex'
        WHERE c.target_run_ref IN :runs AND c.completion_evidence_ref IS NOT NULL
    """, work).scalars().all()
    retired = (set(native) | retired_stage_ids(connection, work)
               | replaced_target_sessions(connection, work, runtime._target_agent))
    reports = cleanup.delete(retired, dry_run, protected_paths)
    reports.extend(_clean_spools(cleanup.root, connection, work, dry_run, protected_paths))
    return reports


# Closed provider turns still serve exact public history, child observations,
# signed call usage, and formal readback. Only launch/replay scratch is unused.
# Target provider-argv.json is read when recovering a historical human-wait receipt.
_LAUNCH_SCRATCH = ("native-provider-argv.json", "supervisor-ready.json",
                   "supervisor-stop.json", "supervisor.lock", ".last-message.supervisor.tmp")


def _prune_spool(directory, digest, dry_run, protected_paths):
    if not directory.is_dir() or directory.resolve() != directory:
        return []
    _, key = read_transport_key_for_operation(directory)
    seal = read_transport_envelope(directory / "supervisor-exit.json", key)
    if (seal.get("invocation_hash") != digest
            or seal.get("schema_ref") not in {SUPERVISOR_EXIT_SCHEMA, SUPERVISOR_EXIT_SCHEMA_V2}):
        return []
    size = 0
    for name in _LAUNCH_SCRATCH:
        path = directory / name
        if (not path.is_file() or path.is_symlink()
                or any(path == origin or origin in path.parents for origin in protected_paths)
                or name == "native-provider-argv.json" and (directory / "native-resume-rejected.json").exists()):
            continue
        amount = path.stat().st_size
        if not dry_run:
            path.unlink()
        size += amount
    return [{"root_kind": "provider_spool", "path": str(directory), "bytes": size,
             "action": "candidate" if dry_run else "pruned", "reason": "terminal_launch_scratch"}] if size else []


def _clean_spools(root, connection, work, dry_run, protected_paths):
    records = []
    for run, operation in _query_runs(connection, """
            SELECT DISTINCT run_ref,operation_ref FROM ar_provider_units WHERE run_ref IN :runs
              AND status IN ('completed','revoked') AND operation_ref IS NOT NULL
        """, work):
        kind = work[run]
        if kind not in {"idea", "plan", "bundle", "reasoning"}:
            continue
        job = root.root / (kind + "-skill-provider") / "provider-operations" / canonical_hash({"job_ref": operation})
        if job.resolve() != job or not job.is_dir():
            continue
        for directory in job.iterdir():
            try:
                if directory.resolve() != directory or not directory.is_dir():
                    continue
                _, key = read_transport_key_for_operation(directory)
                invocation = read_transport_envelope(directory / "invocation.json", key)
                if invocation.get("job_ref") == operation and invocation.get("operation_name") == directory.name:
                    records.extend(_prune_spool(directory, canonical_hash(invocation), dry_run, protected_paths))
            except (OSError, ProviderSupervisorError, ValueError):
                continue
    base = root.run / "harness-supervisor"
    for operation, owner_hash in _query_runs(connection, """
            SELECT operation_ref,invocation_hash FROM ar_harness_provider_operations
            WHERE run_ref IN :runs AND status IN ('executed','failed')
        """, work):
        try:
            if not re.fullmatch("[0-9a-f]{64}", owner_hash):
                continue
            binding = base / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
            if binding.resolve() != binding or not binding.is_file():
                continue
            _, key = read_transport_key_for_operation(base / "provider-operations" / owner_hash[:2] / owner_hash)
            scope = read_transport_envelope(binding, key)
            digest = scope.get("transport_invocation_hash")
            if (scope.get("owner_invocation_hash") == owner_hash and scope.get("provider_operation_ref") == operation
                    and isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest)):
                directory = base / "provider-operations" / digest[:2] / digest
                records.extend(_prune_spool(directory, digest, dry_run, protected_paths))
        except (OSError, ProviderSupervisorError, ValueError):
            continue
    return records


class NativeSessionCleanup:
    def __init__(self, root, executable, acquisition):
        self.root, self.executable = root, executable
        self.acquisition = getattr(acquisition, "retired_native_sessions", None)
        self.offsets = {}
        self._delete_cursors = OrderedDict()

    def delete(self, ids, dry_run, protected_paths):
        try:
            return delete_codex_sessions(self.root, self.executable, ids, dry_run=dry_run,
                                         protected_paths=protected_paths, cursors=self._delete_cursors)
        except (OSError, sqlite3.Error, ValueError):
            return [{"root_kind": "native_session", "path": str(self.root.codex_home),
                     "action": "skipped", "reason": "native_index_unavailable", "bytes": 0}]

    def page(self, key, connection, sql, limit):
        offset = self.offsets.get(key, 0)
        rows = list(connection.execute(text(sql + " LIMIT :limit OFFSET :offset"),
                                       {"limit": limit, "offset": offset}).scalars())
        self.offsets[key] = offset + len(rows) if len(rows) == limit else 0
        return rows

    def long_lived(self, database, *, dry_run, limit):
        from meta_research.target_workspace_cleanup import _protected_originals, _pending_intake_paths
        with database.fenced_write() as connection:
            ids = set(self.page("companion", connection, """
                SELECT n.native_session_ref FROM hc_companion_native_sessions n
                 JOIN hc_companion_sessions s USING (session_ref)
                WHERE n.generation < s.native_session_generation AND n.native_session_ref IS NOT NULL
                UNION
                SELECT n.native_session_ref FROM hc_intent_native_sessions n
                 JOIN hc_intent_drafting_sessions s USING (session_ref)
                WHERE (n.generation < s.native_session_generation OR s.status='closed')
                  AND n.native_session_ref IS NOT NULL
                ORDER BY 1
            """, limit))
            if self.acquisition:
                from meta_research.acquisition import AcquisitionUnavailable
                for session in self.page("acquisition", connection, """
                    SELECT s.session_ref FROM ar_acquisition_sessions s
                    WHERE s.status IN ('ready','cancelled') AND s.current_request_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM ar_acquisition_requests r WHERE r.session_ref=s.session_ref
                        AND r.status IN ('running','waiting_user'))
                    ORDER BY s.session_ref
                """, limit):
                    try:
                        ids.update(self.acquisition(session))
                    except (OSError, AcquisitionUnavailable):
                        continue
            # Intent and Companion can hand the same live native thread across scopes.
            if ids:
                ids -= set(_query_runs(connection, r"""
                    WITH intent AS (
                        SELECT n.native_session_ref, n.generation, s.session_ref, s.initialization_id,
                               s.status, s.native_session_generation, i.status AS initialization_status
                          FROM hc_intent_native_sessions n JOIN hc_intent_drafting_sessions s USING (session_ref)
                          JOIN hc_quest_initializations i USING (initialization_id)
                         WHERE n.native_session_ref IN :runs
                    ), unfinished AS (
                        SELECT operation_ref,root_run_ref FROM ar_execution_responsibilities
                         WHERE owner_scope='human_collaboration' AND effect_kind='drafting_claim' AND status!='finished'
                    )
                    SELECT native_session_ref FROM hc_companion_sessions WHERE native_session_ref IN :runs
                    UNION SELECT n.native_session_ref FROM hc_companion_native_sessions n
                      JOIN hc_companion_turns t ON t.session_ref=n.session_ref AND t.native_session_generation=n.generation
                     WHERE n.native_session_ref IN :runs AND t.assistant_status IN ('queued','processing')
                    UNION SELECT i.native_session_ref FROM intent i
                     WHERE i.generation=i.native_session_generation AND (i.status!='closed' OR
                        (i.initialization_status NOT IN ('cancelled','completed') AND NOT EXISTS (
                            SELECT 1 FROM rg_quests q JOIN hc_companion_sessions c ON c.scope_ref='quest:'||q.quest_ref
                             JOIN hc_companion_native_sessions n ON n.session_ref=c.session_ref AND n.generation=1
                             WHERE q.initialization_id=i.initialization_id AND n.native_session_ref=i.native_session_ref)))
                    UNION SELECT i.native_session_ref FROM intent i JOIN hc_intent_drafting_turns t
                         ON t.session_ref=i.session_ref AND t.native_session_generation=i.generation
                      LEFT JOIN unfinished f ON f.root_run_ref=i.initialization_id AND f.operation_ref=t.turn_ref||'\:intent-reply'
                     WHERE t.assistant_status IN ('queued','running') OR
                        (t.assistant_status='unavailable' AND i.status='open') OR f.operation_ref IS NOT NULL
                    UNION SELECT i.native_session_ref FROM intent i JOIN hc_proposal_generation_attempts a
                         ON a.initialization_id=i.initialization_id AND a.native_session_generation=i.generation
                      LEFT JOIN unfinished f ON f.root_run_ref=i.initialization_id AND f.operation_ref=a.generation_ref||'\:proposal'
                     WHERE a.status IN ('queued','running') OR f.operation_ref IS NOT NULL
                """, ids).scalars())
            recorder = self.root.root / "research-recorder" / "summaries.sqlite3"
            if not ids and not recorder.exists():
                return []
            # Load source protection once, and only if a bound retired UUID exists.
            def reclaim(refs):
                if not any(_uuid(ref) for ref in refs):
                    return []
                protected = (*_protected_originals(connection), *_pending_intake_paths(connection))
                return self.delete(refs, dry_run, protected)

            if recorder.exists() and recorder.resolve() == recorder:
                with sqlite3.connect(recorder.as_uri() + "?mode=rw", uri=True, timeout=2) as db:
                    db.execute("BEGIN IMMEDIATE")
                    offset = self.offsets.get("recorder", 0)
                    retired = [r[0] for r in db.execute("""
                        SELECT DISTINCT j.native_session_ref FROM jobs j WHERE j.native_session_ref IS NOT NULL
                          AND j.status IN ('completed','failed')
                          AND NOT EXISTS (SELECT 1 FROM quests q WHERE q.native_session_ref=j.native_session_ref)
                          AND NOT EXISTS (SELECT 1 FROM jobs a WHERE a.status='active'
                            AND json_extract(a.request_json,'$.native_session_ref')=j.native_session_ref)
                        ORDER BY 1 LIMIT ? OFFSET ?""", (limit, offset))]
                    self.offsets["recorder"] = offset + len(retired) if len(retired) == limit else 0
                    return reclaim(ids | set(retired))
            return reclaim(ids)
