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

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.advancement_engine import recent_workspace_cycles
from meta_research.owners.agent_runtime import _bundle_inbox_checkpoint_from_row
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError
from meta_research.stage_session_retirement import retired_stage_ids
from meta_research.target_session_retirement import replaced_target_sessions
from meta_research.provider_supervisor import (ProviderSupervisorError, read_transport_envelope,
    read_transport_key_for_operation, SUPERVISOR_EXIT_SCHEMA, SUPERVISOR_EXIT_SCHEMA_V2)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


# Bound CLI latency for a sweep, independently of research database access.
# Native deletion and filesystem work never hold the research Owner writer.
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
                    # Local housekeeping uses the same thread DB without
                    # refreshing or writing the research account's credentials.
                    subprocess.run([executable, "delete", "--force", "-c",
                                    'cli_auth_credentials_store="ephemeral"', ref], check=True,
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


def _cycle_dependencies(connection, cycle_refs):
    """Read current Owner dependencies once per database snapshot, across every run of each Cycle."""
    rows = connection.execute(text("""
        WITH runs AS (
            SELECT cycle_ref, run_ref, NULL AS target_ref FROM ar_stage_runs WHERE cycle_ref IN :cycles
            UNION
            SELECT r.cycle_ref, a.target_run_ref, a.target_ref FROM ar_target_launches a
              JOIN ae_stage_run_requests r ON r.request_ref=a.stage_request_ref WHERE r.cycle_ref IN :cycles
        ), pending AS (
            SELECT cycle_ref, 'cycle_incomplete' AS reason FROM ae_cycles
              WHERE cycle_ref IN :cycles AND status!='completed'
            UNION ALL
            SELECT c.cycle_ref, 'workspace_cycle_lineage_unavailable' FROM ae_cycles c
              WHERE c.cycle_ref IN :cycles AND NOT EXISTS (
                SELECT 1 FROM ae_foreground_grants g JOIN ae_foreground_heads h USING (quest_ref)
                WHERE g.quest_ref=c.quest_ref AND g.cycle_ref=c.cycle_ref AND g.epoch<=h.epoch)
            UNION ALL
            SELECT cycle_ref, 'cycle_handoff_incomplete' AS reason FROM ae_stage_run_requests r
              WHERE cycle_ref IN :cycles AND NOT EXISTS (
                SELECT 1 FROM ae_stage_commits c WHERE c.request_ref=r.request_ref)
              -- A revoked attempt has no commit of its own. Its responsibility
              -- is settled only by a later commit of this same Cycle and Stage.
              AND NOT EXISTS (
                SELECT 1 FROM ae_foreground_grants g
                JOIN ae_stage_run_requests newer ON newer.cycle_ref=r.cycle_ref
                  AND newer.stage=r.stage AND newer.epoch>r.epoch
                JOIN ae_stage_commits accepted ON accepted.request_ref=newer.request_ref
                WHERE g.quest_ref=r.quest_ref AND g.cycle_ref=r.cycle_ref
                  AND g.epoch=r.epoch AND g.status='revoked'
                  AND NOT EXISTS (SELECT 1 FROM ar_stage_runs s LEFT JOIN ar_run_controls ctl USING(run_ref)
                    WHERE s.request_ref=r.request_ref AND
                      (ctl.status IS NOT 'terminated' OR ctl.cleanup_status IS NOT 'completed')))
            UNION ALL
            SELECT r.cycle_ref, 'handoff_incomplete' FROM ar_target_launches a
              JOIN ae_stage_run_requests r ON r.request_ref=a.stage_request_ref
              LEFT JOIN ar_target_root_lifecycles l ON l.target_ref=a.target_ref
              WHERE r.cycle_ref IN :cycles AND (l.status IS NULL OR l.status NOT IN ('completed','cancelled','failed'))
            UNION ALL
            SELECT cycle_ref, 'unfinished_runtime_responsibility' FROM runs r
              JOIN ar_execution_responsibilities effects ON effects.root_run_ref=r.run_ref
              WHERE effects.status!='finished'
            UNION ALL
            SELECT cycle_ref, 'pending_human_request' FROM runs JOIN owner_human_request_waiters w
              ON (w.waiter_ref='root_run:'||run_ref OR w.waiter_ref=runs.target_ref)
              WHERE w.status IN ('blocked','released')
            UNION ALL
            SELECT cycle_ref, 'active_or_recoverable_session' FROM runs r WHERE
              EXISTS (SELECT 1 FROM ar_harness_provider_operations p WHERE p.run_ref=r.run_ref
                      AND status IN ('running','unknown_outcome'))
              OR EXISTS (SELECT 1 FROM ar_target_harness_child_sessions s WHERE s.target_run_ref=r.run_ref
                         AND s.completion_evidence_ref IS NULL)
              OR EXISTS (SELECT 1 FROM ar_provider_units u WHERE u.run_ref=r.run_ref
                         AND u.status IN ('active','revocation_pending'))
              OR EXISTS (SELECT 1 FROM ar_run_controls c WHERE c.run_ref=r.run_ref AND
                         (c.status IN ('running','suspended','suspended_fenced','reconciliation_required')
                          OR c.cleanup_status='pending'))
              OR EXISTS (SELECT 1 FROM ar_execution_fences f WHERE f.run_ref=r.run_ref
                         AND f.status IN ('current','submitted'))
              OR EXISTS (SELECT 1 FROM ar_stage_sessions s WHERE s.run_ref=r.run_ref AND s.status='active')
              OR EXISTS (SELECT 1 FROM ar_harness_runs h WHERE h.run_ref=r.run_ref AND h.status='suspended')
        )
        SELECT cycle_ref, reason FROM pending GROUP BY cycle_ref, reason ORDER BY cycle_ref, reason
        """).bindparams(bindparam("cycles", expanding=True)), {"cycles": sorted(cycle_refs)})
    pending = {cycle: reason for cycle, reason in rows}
    failed = connection.execute(text("SELECT requests.cycle_ref, launches.target_ref "
        "FROM ar_target_launches launches JOIN ae_stage_run_requests requests "
        "ON requests.request_ref=launches.stage_request_ref JOIN ar_target_root_lifecycles lifecycle "
        "ON lifecycle.target_ref=launches.target_ref WHERE requests.cycle_ref IN :cycles "
        "AND lifecycle.status='failed'").bindparams(bindparam("cycles", expanding=True)),
        {"cycles": sorted(cycle_refs)})
    for cycle, target in failed:
        try:
            _verify_failed_notice_ack(connection, target)
        except OwnerConflict as error:
            pending[cycle] = error.code
    return pending


def _verify_failed_notice_ack(connection, target_ref):
    # A closed historical Bundle cannot use the current-execution checkpoint API.
    # Its immutable checkpoint still authenticates the exact published prefix.
    row = connection.execute(text("SELECT checkpoints.* FROM ar_target_work_notices notice "
        "JOIN ar_bundle_inbox_entries entries ON entries.notice_ref=notice.notice_ref "
        "JOIN ar_target_launches launches ON launches.target_ref=notice.target_ref "
        "JOIN ar_bundle_dispatch_decisions dispatch ON dispatch.decision_ref=launches.dispatch_decision_ref "
        "AND dispatch.run_ref=entries.run_ref "
        "JOIN ar_bundle_inbox_checkpoints checkpoints ON checkpoints.run_ref=entries.run_ref "
        "AND checkpoints.cursor>=entries.sequence AND checkpoints.generation>=entries.published_generation "
        "WHERE notice.target_ref=:target ORDER BY checkpoints.checkpoint_revision LIMIT 1"),
        {"target": target_ref}).first()
    if row is None:
        raise OwnerConflict('bundle_notice_unacknowledged')
    _bundle_inbox_checkpoint_from_row(row)


def _path_dependency(workspace, path, pending_uploads, pending_intakes):
    if workspace in pending_uploads:
        return 'pending_human_upload'
    if (path / '.target-completion-intakes').exists():
        return 'pending_finalizer_intake'
    if any(_paths_overlap(source, path) for source in pending_intakes):
        return 'pending_asset_intake'
    return None


def _blocked_session_runs(leases, target_root, pending_uploads, pending_intakes):
    """Check every recorded lease outside the database access."""
    blocked = {}
    for run, workspace, name in leases:
        try:
            path = target_root / name
            if name != canonical_hash({'workspace_ref': workspace}) or path.resolve(strict=False) != path:
                blocked[run] = 'workspace_cleanup_unsafe_boundary'
            elif reason := _path_dependency(workspace, path, pending_uploads, pending_intakes):
                blocked[run] = reason
        except (OSError, RuntimeError, TypeError):
            blocked[run] = 'workspace_cleanup_unsafe_boundary'
    return blocked


def _window_reason(connection, candidate, windows):
    quest = candidate['quest_ref']
    if quest not in windows:
        try:
            windows[quest] = recent_workspace_cycles(connection, quest)
        except OwnerConflict:
            windows[quest] = ()
    if not windows[quest]:
        return 'workspace_cycle_window_unavailable'
    if candidate['cycle_ref'] in windows[quest]:
        return 'recent_cycle_window'
    return None


def _pending_intake_paths(connection):
    paths = []
    for row in connection.execute(text("SELECT request_json,request_hash FROM rm_asset_intakes "
                                       "WHERE status IN ('queued','processing')")):
        try:
            request = json.loads(row.request_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict('workspace_cleanup_pending_intake_invalid') from error
        if not isinstance(request, dict) or canonical_hash(request) != row.request_hash:
            raise OwnerConflict('workspace_cleanup_pending_intake_invalid')
        if isinstance(request.get('source_locator'), str):
            paths.append(Path(request['source_locator']))
    return tuple(paths)


def _original_locators(connection):
    """Stored custody and human selections protect physical originals, even retired ones."""
    paths = set()
    for row in connection.execute(text("SELECT source_locator FROM rm_asset_custodies "
                                       "WHERE custody_mode='linked_local'")):
        if not row.source_locator:
            raise OwnerConflict('linked_local_locator_unavailable')
        original = Path(row.source_locator)
        if not original.is_absolute():
            raise OwnerConflict('linked_local_locator_unavailable')
        paths.add(original)
    for row in connection.execute(text(
            "SELECT r.selection_json,r.selection_hash FROM hc_work_material_references r "
            "JOIN hc_work_material_submissions s USING(submission_ref) "
            "WHERE s.state IN ('ready','pending') AND "
            "s.anchor_kind IN ('creation','manual','guidance','input','request')")):
        try:
            selection = json.loads(row.selection_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict('workspace_cleanup_material_reference_invalid') from error
        if (not isinstance(selection, dict) or canonical_hash(selection) != row.selection_hash
                or not isinstance(selection.get('absolute_path'), str)
                or not Path(selection['absolute_path']).is_absolute()):
            raise OwnerConflict('workspace_cleanup_material_reference_invalid')
        original = Path(selection['absolute_path'])
        paths.add(original)
    return tuple(paths)


def _pending_human_uploads(connection):
    workspaces = set()
    for row in connection.execute(text(
            "SELECT s.command_json,s.command_hash,s.receiver_json,w.workspace_ref "
            "FROM hc_work_material_submissions s JOIN hc_work_material_roots w USING(submission_ref) "
            "WHERE s.state='pending'")):
        try:
            command = json.loads(row.command_json)
            receiver = json.loads(row.receiver_json)
        except (TypeError, ValueError) as error:
            raise OwnerConflict('workspace_cleanup_material_reference_invalid') from error
        if (not isinstance(command, dict) or not isinstance(receiver, dict) or canonical_hash(command) != row.command_hash
                or command.get('receiver') != receiver
                or not any(root.get('workspace_ref') == row.workspace_ref for root in receiver.get('roots', []))):
            raise OwnerConflict('workspace_cleanup_material_reference_invalid')
        if command.get('uploads'):
            workspaces.add(row.workspace_ref)
    return workspaces


def _resolve_protected_paths(paths):
    # Preserve both the registered locator and its actual original (parent aliases).
    try:
        return tuple(set(paths) | {path.resolve(strict=False) for path in paths})
    except (OSError, RuntimeError, ValueError) as error:
        raise OwnerConflict('workspace_cleanup_material_reference_invalid') from error


def _paths_overlap(left, right):
    left, right = left.resolve(strict=False), right.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def _verified_candidate_path(runtime, candidate):
    """Keep native retirement's published handoff requirements separate from scratch GC."""
    if candidate['cycle_status'] != 'completed':
        return None
    failed = candidate['root_kind'] == 'target' and candidate['lifecycle_status'] == 'failed'
    if not failed and (candidate['lifecycle_status'] != 'completed' or not candidate['commit_ref']):
        return None
    if candidate['root_kind'] == 'target':
        root = Path(runtime._target_agent._workspace_root)
        path = root / candidate['root_name']
        expected = canonical_hash({'workspace_ref': candidate['workspace_ref']})
    else:
        if runtime._root_workspaces is None:
            return None
        location = runtime._root_workspaces.read_stage_workspace_location(
            candidate['work_ref'], candidate['cycle_ref'], candidate['root_kind'])
        path = location.directory
        root = path.parent
        candidate['workspace_ref'] = location.workspace_ref
        expected = canonical_hash({'work_ref': candidate['work_ref']})
    if (path.name != expected or root.is_symlink() or root.resolve() != root
            or path.is_symlink() or path.resolve(strict=False) != path):
        return None
    if failed:
        owner = runtime._agent_runtime
        notice = owner.query_target_work_notice(candidate['target_ref'])
        if notice is None:
            return None
        handoff = owner.read_target_run_handoff(notice.handoff_manifest_ref)
        if handoff.handle_history[-1].target_run_ref != candidate['work_ref']:
            return None
        operation = owner.harness_runs.latest_operation(candidate['work_ref'])
        if operation is None:
            return None
        owner.harness_runs.verify_operation_terminal(operation.operation_ref)
    elif candidate['root_kind'] == 'target':
        graph = runtime._research_graph
        manifest = graph._target_root_manifest_reader.query(candidate['manifest_ref'])
        transition = graph.query_target_root_commit_transition(candidate['target_ref'])
        handoff = runtime._agent_runtime.query_target_root_completion_handoff(
            target_ref=candidate['target_ref'], completion_ref=candidate['completion_ref'],
            target_commit_ref=candidate['commit_ref'])
        if manifest is None or transition is None or handoff is None or transition.target_commit_ref != candidate['commit_ref']:
            return None
    else:
        request = runtime._agent_runtime.query_stage_run_by_ref(candidate['work_ref'])
        ae = runtime._advancement_engine
        if (ae is None or request is None
                or getattr(ae, 'query_' + candidate['root_kind'] + '_stage_commit')(request.request_ref) is None):
            return None
    return path


def cleanup_cycle_sessions(runtime, candidates, *, dry_run):
    """Snapshot retirement guards, then inspect/delete native files without a writer lock.

    Called before workspace deletion so pending finalizer markers are still visible.
    Native terminal, handoff, active-session and pending-intake guards remain in force.
    """
    cleanup = runtime._native_cleanup
    candidates = [c for c in candidates if c.get('lineage_valid', True) and c.get('cycle_ref') and c.get('quest_ref')]
    if cleanup is None or not candidates:
        return []
    database = runtime._database
    eligible = []
    try:
        with database.read_snapshot() as connection:
            windows = {}
            reasons = {c['work_ref']: _window_reason(connection, c, windows) for c in candidates}
            deps = _cycle_dependencies(connection, {c['cycle_ref'] for c in candidates})
            locators = _original_locators(connection)
            pending_intakes = _pending_intake_paths(connection)
            pending_uploads = _pending_human_uploads(connection)
            runs = {c['work_ref'] for c in candidates if c['root_kind'] == 'target'}
            leases = _query_runs(connection, """SELECT target_run_ref,workspace_ref,root_name
                FROM ar_target_run_workspaces WHERE target_run_ref IN :runs""", runs).all() if runs else []
        protected = _resolve_protected_paths((*locators, *pending_intakes))
        blocked = _blocked_session_runs(leases, Path(runtime._target_agent._workspace_root),
                                        pending_uploads, pending_intakes)
        for item in candidates:
            if item['work_ref'] in blocked:
                item['native_cleanup_reason'] = blocked[item['work_ref']]
            candidate = dict(item)
            if reasons[candidate['work_ref']] or deps.get(candidate['cycle_ref']) or candidate['work_ref'] in blocked:
                continue
            try:
                path = _verified_candidate_path(runtime, candidate)
                if path is not None and not _path_dependency(candidate['workspace_ref'], path, pending_uploads, pending_intakes):
                    eligible.append(candidate)
            except (OwnerConflict, AgentRuntimeHarnessError, OSError, RuntimeError, ValueError, TypeError):
                continue
    except (OwnerConflict, OSError, RuntimeError, ValueError):
        return []
    work = {item['work_ref']: item['root_kind'] for item in eligible}
    if not work:
        return []
    try:
        with database.read_snapshot() as connection:
            # File/handoff verification may be slow. Reject changed mutable guards
            # using a fresh, short database snapshot before any native deletion.
            windows = {}
            current_deps = _cycle_dependencies(connection, {c['cycle_ref'] for c in eligible})
            work = {c['work_ref']: c['root_kind'] for c in eligible
                    if not current_deps.get(c['cycle_ref']) and not _window_reason(connection, c, windows)}
            if (not work or set(locators) != set(_original_locators(connection))
                    or set(pending_intakes) != set(_pending_intake_paths(connection))
                    or pending_uploads != _pending_human_uploads(connection)):
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
            stage_spools = _query_runs(connection, """
                SELECT DISTINCT run_ref,operation_ref FROM ar_provider_units WHERE run_ref IN :runs
                  AND status IN ('completed','revoked') AND operation_ref IS NOT NULL
            """, work).all()
            target_spools = _query_runs(connection, """
                SELECT operation_ref,invocation_hash FROM ar_harness_provider_operations
                WHERE run_ref IN :runs AND status IN ('executed','failed')
            """, work).all()
    except (OwnerConflict, OSError, ValueError, TypeError):
        return []
    reports = cleanup.delete(retired, dry_run, protected)
    reports.extend(_clean_spools(cleanup.root, work, stage_spools, target_spools, dry_run, protected))
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


def _clean_spools(root, work, stage_spools, target_spools, dry_run, protected_paths):
    records = []
    for run, operation in stage_spools:
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
    for operation, owner_hash in target_spools:
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
        with database.read_snapshot() as connection:
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
            sessions = self.page("acquisition", connection, """
                    SELECT s.session_ref FROM ar_acquisition_sessions s
                    WHERE s.status IN ('ready','cancelled') AND s.current_request_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM ar_acquisition_requests r WHERE r.session_ref=s.session_ref
                        AND r.status IN ('running','waiting_user'))
                    ORDER BY s.session_ref
                """, limit) if self.acquisition else []
        acquisition_ids = {}
        if self.acquisition:
            from meta_research.acquisition import AcquisitionUnavailable
            for session in sessions:
                try:
                    acquisition_ids[session] = self.acquisition(session)
                except (OSError, AcquisitionUnavailable):
                    continue
        with database.read_snapshot() as connection:
            # Revalidate the Owner state after reading acquisition receipt files.
            if acquisition_ids:
                eligible_sessions = _query_runs(connection, """
                    SELECT s.session_ref FROM ar_acquisition_sessions s
                    WHERE s.session_ref IN :runs AND s.status IN ('ready','cancelled')
                      AND s.current_request_id IS NULL
                      AND NOT EXISTS (SELECT 1 FROM ar_acquisition_requests r WHERE r.session_ref=s.session_ref
                        AND r.status IN ('running','waiting_user'))
                """, acquisition_ids).scalars().all()
                for session in eligible_sessions:
                    ids.update(acquisition_ids[session])
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
            try:
                with database.read_snapshot() as connection:
                    locators = _original_locators(connection)
                    pending_intakes = _pending_intake_paths(connection)
                protected = _resolve_protected_paths((*locators, *pending_intakes))
            except (OwnerConflict, OSError, ValueError):
                return []
            return self.delete(refs, dry_run, protected)

        if recorder.exists() and recorder.resolve() == recorder:
            with sqlite3.connect(recorder.as_uri() + "?mode=rw", uri=True, timeout=2) as db:
                db.execute("BEGIN IMMEDIATE")
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name IN ('jobs','quests')"
                )}
                # The recorder creates its file before committing its schema.
                if tables != {"jobs", "quests"}:
                    return [*reclaim(ids), {"root_kind": "native_session", "session_ref": None,
                        "path": str(recorder), "bytes": 0, "action": "skipped",
                        "reason": "native_recorder_schema_unavailable"}]
                offset = self.offsets.get("recorder", 0)
                retired = [r[0] for r in db.execute("""
                    SELECT DISTINCT j.native_session_ref FROM jobs j WHERE j.native_session_ref IS NOT NULL
                      AND j.status IN ('completed','failed')
                      AND j.native_session_ref NOT IN (
                        SELECT native_session_ref FROM quests WHERE native_session_ref IS NOT NULL
                        UNION ALL
                        SELECT json_extract(request_json,'$.native_session_ref') FROM jobs
                         WHERE status IN ('active','failed') AND json_type(request_json,'$.native_session_ref')='text'
                      )
                    ORDER BY 1 LIMIT ? OFFSET ?""", (limit, offset))]
                self.offsets["recorder"] = offset + len(retired) if len(retired) == limit else 0
                return reclaim(ids | set(retired))
        return reclaim(ids)
