"""Bounded reclamation of completed Cycle scratch workspaces.

The recent foreground window and all outstanding consumers protect a root.
Formal originals survive in place when the remaining scratch is reclaimed.
"""
from __future__ import annotations

import os
import json
import secrets
from pathlib import Path
import shutil
import stat

from sqlalchemy import bindparam, text

from meta_research.owners.advancement_engine import recent_workspace_cycles
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json


def _workspace_bytes(path: Path, protected=()) -> int:
    root_info = path.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or path.is_mount():
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    total = 0
    for directory, names, files in os.walk(path, followlinks=False):
        names[:] = [name for name in names if Path(directory) / name not in protected]
        for name in (*names, *files):
            child = Path(directory) / name
            if child in protected:
                continue
            info = child.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_dev != root_info.st_dev
                    or child.is_mount() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            total += getattr(info, 'st_blocks', (info.st_size + 511)//512) * 512
    return total


def _workspace_entries(path: Path, protected=()) -> dict[str, dict[str, int]]:
    entries = {}
    for directory, names, files in os.walk(path, followlinks=False):
        names[:] = [name for name in names if Path(directory) / name not in protected]
        for name in (*names, *files):
            child = Path(directory) / name
            if child in protected:
                continue
            info = child.lstat()
            identity = {'device': info.st_dev, 'inode': info.st_ino, 'type': stat.S_IFMT(info.st_mode)}
            if not stat.S_ISDIR(info.st_mode):
                identity.update(size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
            # Removing children changes directory times and size. Their stable
            # identity must survive our own partial removal; files must not change.
            entries[str(child.relative_to(path))] = identity
    return entries


def _paths_overlap(left: Path, right: Path) -> bool:
    left, right = left.resolve(strict=False), right.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def _write_cleanup_scope(journal: Path, scope: dict) -> None:
    descriptor = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = journal.name + '.tmp-' + secrets.token_hex(8)
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=descriptor)
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(canonical_json(scope))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, journal.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _cleanup_scope(candidate, path: Path, journal_root: Path, *, dry_run: bool, protected=()):
    if journal_root.is_symlink() or journal_root.resolve(strict=False) != journal_root:
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    journal = journal_root / (canonical_hash({'workspace_ref': candidate['workspace_ref'],
        'cycle_ref': candidate['cycle_ref']}) + '.json')
    identity = {'workspace_ref': candidate['workspace_ref'], 'cycle_ref': candidate['cycle_ref'],
        'path': str(path)}
    info = path.lstat()
    entries = _workspace_entries(path, protected)
    if journal.exists() or journal.is_symlink():
        descriptor = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                scope = json.load(stream)
        except (ValueError, UnicodeError) as error:
            raise OwnerConflict('workspace_cleanup_scope_invalid') from error
        if (not isinstance(scope, dict) or any(scope.get(key) != value for key, value in identity.items())
                or not isinstance(scope.get('entries'), dict)
                or scope.get('entries_hash') != canonical_hash(scope['entries'])):
            raise OwnerConflict('workspace_cleanup_scope_invalid')
        if scope.get('state') == 'removed':
            return 'workspace_cleanup_scope_closed', journal, scope
        if (scope.get('state') != 'removing' or scope.get('device') != info.st_dev
                or scope.get('inode') != info.st_ino
                or any(scope['entries'].get(name) != identity for name, identity in entries.items())):
            return 'workspace_cleanup_scope_changed', journal, scope
    else:
        scope = {**identity, 'device': info.st_dev, 'inode': info.st_ino, 'state': 'removing',
            'entries': entries, 'entries_hash': canonical_hash(entries)}
        if not dry_run:
            journal_root.mkdir(exist_ok=True)
            _write_cleanup_scope(journal, scope)
    return None, journal, scope


def _cycle_dependencies(connection, cycle_refs):
    """Read current Owner dependencies once per fenced batch, across every run of each Cycle."""
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
              WHERE r.cycle_ref IN :cycles AND (l.status IS NULL OR l.status NOT IN ('completed','cancelled'))
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
    return {cycle: reason for cycle, reason in rows}


def _path_dependency(workspace, path, pending_uploads, pending_intakes):
    if workspace in pending_uploads:
        return 'pending_human_upload'
    if (path / '.target-completion-intakes').exists():
        return 'pending_finalizer_intake'
    if any(_paths_overlap(source, path) for source in pending_intakes):
        return 'pending_asset_intake'
    return None


def _blocked_session_runs(connection, candidates, target_root, pending_uploads, pending_intakes):
    """Session/spool ownership spans every workspace lease of a recovered Target."""
    runs = {candidate['work_ref'] for candidate in candidates if candidate['root_kind'] == 'target'}
    if not runs:
        return {}
    blocked = {}
    leases = connection.execute(text("""SELECT target_run_ref,workspace_ref,root_name
        FROM ar_target_run_workspaces WHERE target_run_ref IN :runs""").bindparams(
            bindparam("runs", expanding=True)), {"runs": sorted(runs)})
    for run, workspace, name in leases:
        path = target_root / name
        try:
            if name != canonical_hash({'workspace_ref': workspace}) or path.resolve(strict=False) != path:
                blocked[run] = 'workspace_cleanup_unsafe_boundary'
            elif reason := _path_dependency(workspace, path, pending_uploads, pending_intakes):
                blocked[run] = reason
        except (OSError, RuntimeError):
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


def _protected_originals(connection):
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
    # A selected locator can reach the same original through a parent alias.
    # Match the recorded name and the actual file while walking scratch.
    try:
        return tuple(paths | {path.resolve(strict=False) for path in paths})
    except RuntimeError as error:  # symlink loop in a changed locator
        raise OwnerConflict('workspace_cleanup_material_reference_invalid') from error


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


def _prune_workspace(path, protected, entries):
    """Keep originals and their ancestors; delete siblings through held directory fds."""
    def visit(directory, descriptor):
        for name in os.listdir(descriptor):
            child = directory / name
            if any(child == original or original in child.parents for original in protected):
                continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            saved = entries.get(str(child.relative_to(path)))
            identity = {'device': info.st_dev, 'inode': info.st_ino, 'type': stat.S_IFMT(info.st_mode)}
            if not stat.S_ISDIR(info.st_mode):
                identity.update(size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
            if identity != saved:
                raise OwnerConflict('workspace_cleanup_scope_changed')
            if any(child in original.parents for original in protected):
                held = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    if (os.fstat(held).st_dev, os.fstat(held).st_ino) != (info.st_dev, info.st_ino):
                        raise OwnerConflict('workspace_cleanup_scope_changed')
                    visit(child, held)
                finally:
                    os.close(held)
            elif stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(path, descriptor)
    finally:
        os.close(descriptor)


def cleanup_completed_workspaces(runtime, *, dry_run=True, limit=10):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('workspace_cleanup_limit_invalid')
    database = runtime._database
    target_root = runtime._target_agent._workspace_root
    if database is None or target_root is None:
        return ()
    target_root = Path(target_root)
    journal_root = target_root.parent / 'workspace-cleanup'
    offset = getattr(runtime, '_workspace_cleanup_offset', 0)
    # Only Cycle-owned Target/stage work is eligible. Stable Companion HC
    # workspaces (including writing files after /new) never enter
    # this candidate set and must not be added as finished provider scratch.
    with database.read() as connection:
        candidates = connection.execute(text("SELECT w.workspace_ref AS workspace_ref,w.root_name,w.target_ref, "
            "w.target_run_ref AS work_ref, 'target' AS root_kind, r.cycle_ref, cycle.quest_ref, cycle.status AS cycle_status, l.status AS lifecycle_status, "
            "l.completion_ref,m.manifest_ref,c.commit_ref "
            "FROM ar_target_run_workspaces w JOIN ar_target_root_lifecycles l USING(target_ref) "
            "JOIN ar_target_launches a USING(target_ref) "
            "JOIN ae_stage_run_requests r ON r.request_ref=a.stage_request_ref "
            "JOIN ae_cycles cycle ON cycle.cycle_ref=r.cycle_ref "
            "LEFT JOIN rm_target_root_completion_manifests m ON m.completion_ref=l.completion_ref "
            "LEFT JOIN rg_target_commits c ON c.target_ref=w.target_ref "
            "WHERE l.status IN ('completed','cancelled') UNION ALL "
            "SELECT NULL,NULL,NULL,s.run_ref,s.stage,s.cycle_ref,cycle.quest_ref,cycle.status,s.status, "
            "NULL,NULL,c.commit_ref FROM ar_stage_runs s "
            "JOIN ae_cycles cycle USING(cycle_ref) JOIN ae_stage_commits c ON c.request_ref=s.request_ref "
            "WHERE cycle.status='completed' AND s.status='completed' "
            "ORDER BY work_ref,workspace_ref LIMIT :limit OFFSET :offset"),
            {'limit': limit, 'offset': offset}).mappings().all()
        windows = {}
        window_reasons = {c['work_ref']: _window_reason(connection, c, windows) for c in candidates}
    runtime._workspace_cleanup_offset = offset + len(candidates) if len(candidates) == limit else 0
    report = []
    eligible = []
    for candidate in candidates:
        candidate = dict(candidate)
        if candidate['root_kind'] == 'target':
            root = target_root
            path = root / candidate['root_name']
            expected = canonical_hash({'workspace_ref': candidate['workspace_ref']})
        else:
            if runtime._root_workspaces is None:
                continue
            try:
                location = runtime._root_workspaces.read_stage_workspace_location(
                    candidate['work_ref'], candidate['cycle_ref'], candidate['root_kind'])
            except OwnerConflict as error:
                report.append({'workspace_ref': None, 'cycle_ref': candidate['cycle_ref'],
                    'root_kind': candidate['root_kind'], 'path': None, 'bytes': 0,
                    'action': 'skipped', 'reason': error.code})
                continue
            path = location.directory
            root = path.parent
            candidate['workspace_ref'] = location.workspace_ref
            candidate['root_name'] = path.name
            expected = canonical_hash({'work_ref': candidate['work_ref']})
        record = {'workspace_ref': candidate['workspace_ref'], 'cycle_ref': candidate['cycle_ref'],
                  'root_kind': candidate['root_kind'], 'path': str(path), 'bytes': 0,
                  'action': 'skipped', 'reason': None}
        report.append(record)
        try:
            if (candidate['root_name'] != expected or root.is_symlink() or root.resolve() != root
                    or path.parent != root or path.is_symlink() or path.resolve(strict=False) != path):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            if candidate['cycle_status'] != 'completed':
                record['reason'] = 'cycle_incomplete'
                continue
            if candidate['lifecycle_status'] != 'completed' or not candidate['commit_ref']:
                record['reason'] = 'handoff_incomplete'
                continue
            if window_reasons[candidate['work_ref']]:
                record['reason'] = window_reasons[candidate['work_ref']]
                continue
            # Public Owner readbacks authenticate the immutable handoff and all
            # retained manifest bindings without exporting another whole copy.
            if candidate['root_kind'] == 'target':
                graph = runtime._research_graph
                manifest = graph._target_root_manifest_reader.query(candidate['manifest_ref'])
                transition = graph.query_target_root_commit_transition(candidate['target_ref'])
                handoff = runtime._agent_runtime.query_target_root_completion_handoff(
                    target_ref=candidate['target_ref'], completion_ref=candidate['completion_ref'],
                    target_commit_ref=candidate['commit_ref'])
                if (manifest is None or transition is None or handoff is None
                        or transition.target_commit_ref != candidate['commit_ref']):
                    record['reason'] = 'handoff_incomplete'
                    continue
            else:
                request = runtime._agent_runtime.query_stage_run_by_ref(candidate['work_ref'])
                ae = runtime._advancement_engine
                if ae is None or getattr(ae, 'query_' + candidate['root_kind'] + '_stage_commit')(request.request_ref) is None:
                    record['reason'] = 'handoff_incomplete'
                    continue
            eligible.append((candidate, path, root, record))
        except (OwnerConflict, OSError) as error:
            record['reason'] = error.code if isinstance(error, OwnerConflict) else type(error).__name__
    if not eligible:
        return tuple(report)
    with database.fenced_write() as connection:
        try:
            protected_originals = _protected_originals(connection)
            pending_uploads = _pending_human_uploads(connection)
            pending_intakes = _pending_intake_paths(connection)
            deps = _cycle_dependencies(connection, {c['cycle_ref'] for c, *_ in eligible})
            fenced_windows = {}
            native_candidates = []
            blocked_runs = set()
        except (OwnerConflict, OSError) as error:
            for _, _, _, record in eligible:
                record['reason'] = error.code if isinstance(error, OwnerConflict) else type(error).__name__
            return tuple(report)
        for candidate, path, root, record in eligible:
            try:
                if (root.is_symlink() or root.resolve() != root or path.is_symlink()
                        or path.resolve(strict=False) != path):
                    raise OwnerConflict('workspace_cleanup_unsafe_boundary')
                # Hold BEGIN IMMEDIATE through the deletion. Re-read the foreground and
                # all mutable Owner dependencies in this cut, after public handoff readback.
                reason = _window_reason(connection, candidate, fenced_windows) or deps.get(candidate['cycle_ref'])
                reason = reason or _path_dependency(candidate['workspace_ref'], path, pending_uploads, pending_intakes)
                if reason:
                    record['reason'] = reason
                    blocked_runs.add(candidate['work_ref'])
                    continue
                native_candidates.append(candidate)
                if not path.exists():
                    record['reason'] = 'already_removed'
                    continue
                originals = tuple(original for original in protected_originals if _paths_overlap(original, path))
                if any(path == original or original in path.parents for original in originals):
                    record['reason'] = 'protected_original'
                    continue
                record['bytes'] = _workspace_bytes(path, originals)
                if any(_paths_overlap(path, protected)
                       for protected in (*runtime._protected_storage_roots, journal_root)):
                    record['reason'] = 'retained_content_storage'
                    continue
                reason, journal, scope = _cleanup_scope(candidate, path, journal_root, dry_run=dry_run, protected=originals)
                if reason:
                    record['reason'] = reason
                    continue
                record['reason'] = 'published_verified_completed_cycle_workspace'
                record['action'] = 'candidate'
                if not dry_run:
                    # Recheck immediately before the fd-based recursive remove.
                    _workspace_bytes(path, originals)
                    if originals:
                        _prune_workspace(path, originals, scope['entries'])
                        record['bytes'] -= _workspace_bytes(path, originals)
                        record['action'] = 'pruned'
                        record['reason'] = 'originals_preserved_in_place'
                    else:
                        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            shutil.rmtree(candidate['root_name'], dir_fd=descriptor)
                        finally:
                            os.close(descriptor)
                        _write_cleanup_scope(journal, {**scope, 'state': 'removed'})
                        record['action'] = 'removed'
            except (OwnerConflict, OSError) as error:
                record['reason'] = error.code if isinstance(error, OwnerConflict) else type(error).__name__
        if native_candidates:
            from meta_research.native_session_cleanup import cleanup_cycle_sessions
            sibling_dependencies = _blocked_session_runs(connection, native_candidates, target_root, pending_uploads, pending_intakes)
            blocked_runs.update(sibling_dependencies)
            for candidate, _, _, record in eligible:
                if candidate['work_ref'] in sibling_dependencies:
                    record['native_cleanup_reason'] = sibling_dependencies[candidate['work_ref']]
            report.extend(cleanup_cycle_sessions(runtime, connection,
                [c for c in native_candidates if c['work_ref'] not in blocked_runs],
                dry_run=dry_run, protected_paths=(*protected_originals, *pending_intakes)))
    return tuple(report)
