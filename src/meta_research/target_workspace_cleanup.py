"""Bounded reclamation of completed Cycle scratch workspaces.

Owner completion, publication, verified formal readback and quiescence are all
required. Content custody and native Session storage remain outside this scope.
"""
from __future__ import annotations

import os
import json
import secrets
from pathlib import Path
import shutil
import stat
import time

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json


def _workspace_bytes(path: Path) -> int:
    root_info = path.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or path.is_mount():
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    total = 0
    for directory, names, files in os.walk(path, followlinks=False):
        for name in (*names, *files):
            child = Path(directory) / name
            info = child.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_dev != root_info.st_dev
                    or child.is_mount() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            total += getattr(info, 'st_blocks', (info.st_size + 511)//512) * 512
    return total


def _workspace_entries(path: Path) -> dict[str, dict[str, int]]:
    entries = {}
    for directory, names, files in os.walk(path, followlinks=False):
        for name in (*names, *files):
            child = Path(directory) / name
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


def _cleanup_scope(candidate, path: Path, journal_root: Path, *, dry_run: bool):
    if journal_root.is_symlink() or journal_root.resolve(strict=False) != journal_root:
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    journal = journal_root / (canonical_hash({'workspace_ref': candidate['workspace_ref'],
        'cycle_ref': candidate['cycle_ref']}) + '.json')
    identity = {'workspace_ref': candidate['workspace_ref'], 'cycle_ref': candidate['cycle_ref'],
        'path': str(path)}
    info = path.lstat()
    entries = _workspace_entries(path)
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


def _pending_dependency(connection, candidate, path):
    cycle = connection.execute(text("SELECT status FROM ae_cycles WHERE cycle_ref=:ref"),
        {'ref': candidate['cycle_ref']}).first()
    if cycle is None or cycle.status != 'completed':
        return 'cycle_incomplete'
    missing = connection.execute(text("SELECT 1 FROM ae_stage_run_requests r LEFT JOIN "
        "ae_stage_commits c ON c.request_ref=r.request_ref WHERE r.cycle_ref=:ref "
        "AND c.commit_ref IS NULL LIMIT 1"), {'ref': candidate['cycle_ref']}).first()
    if missing:
        return 'cycle_handoff_incomplete'
    if candidate['root_kind'] == 'target':
        row = connection.execute(text("SELECT status FROM ar_target_root_lifecycles WHERE target_ref=:ref"),
                                 {'ref': candidate['target_ref']}).first()
        if row is None or row.status != 'completed':
            return 'handoff_incomplete'
    # Any live work in this shared Cycle can still read its earlier stage roots.
    cycle_runs = ("SELECT run_ref FROM ar_stage_runs WHERE cycle_ref=:cycle UNION "
        "SELECT a.target_run_ref FROM ar_target_launches a JOIN ae_stage_run_requests r "
        "ON r.request_ref=a.stage_request_ref WHERE r.cycle_ref=:cycle")
    parameters = {'cycle': candidate['cycle_ref']}
    active = connection.execute(text("SELECT 1 FROM ar_harness_provider_operations "
        "WHERE run_ref IN (" + cycle_runs + ") AND status IN ('running','unknown_outcome') LIMIT 1"),
        parameters).first()
    children = connection.execute(text("SELECT 1 FROM ar_target_harness_child_sessions "
        "WHERE target_run_ref IN (" + cycle_runs + ") AND completion_evidence_ref IS NULL LIMIT 1"),
        parameters).first()
    stage = connection.execute(text("SELECT 1 FROM ar_provider_units WHERE run_ref IN (" + cycle_runs + ") "
        "AND status IN ('active','revocation_pending') LIMIT 1"), parameters).first()
    controls = connection.execute(text("SELECT 1 FROM ar_run_controls WHERE run_ref IN (" + cycle_runs + ") "
        "AND status IN ('running','suspended','suspended_fenced','reconciliation_required') LIMIT 1"), parameters).first()
    sessions = connection.execute(text("SELECT 1 FROM ar_stage_sessions WHERE run_ref IN (" + cycle_runs + ") "
        "AND status='active' LIMIT 1"), parameters).first()
    if active or children or stage or controls or sessions:
        return 'active_or_recoverable_session'
    if (path / '.target-completion-intakes').exists():
        return 'pending_finalizer_intake'
    for row in connection.execute(text("SELECT request_json,request_hash FROM rm_asset_intakes "
                                       "WHERE status IN ('queued','processing')")):
        request = json.loads(row.request_json)
        if canonical_hash(request) != row.request_hash:
            raise OwnerConflict('workspace_cleanup_pending_intake_invalid')
        locator = request.get('source_locator')
        if isinstance(locator, str):
            if _paths_overlap(Path(locator), path):
                return 'pending_asset_intake'
    for row in connection.execute(text("SELECT source_locator FROM rm_asset_custodies "
                                       "WHERE custody_mode='linked_local'")):
        if not row.source_locator:
            return 'linked_local_locator_unavailable'
        if _paths_overlap(Path(row.source_locator), path):
            return 'linked_local_original'
    return None


def cleanup_completed_workspaces(runtime, *, dry_run=True, now=None, limit=10, retention_seconds=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('workspace_cleanup_limit_invalid')
    retention = float(os.environ.get('META_RESEARCH_TARGET_WORKSPACE_RETENTION_SECONDS', '86400')
                      if retention_seconds is None else retention_seconds)
    import math
    if not math.isfinite(retention) or retention < 0:
        raise ValueError('workspace_cleanup_retention_invalid')
    now = time.time() if now is None else now
    database = runtime._database
    target_root = runtime._target_agent._workspace_root
    if database is None or target_root is None:
        return ()
    target_root = Path(target_root)
    journal_root = target_root.parent / 'workspace-cleanup'
    offset = getattr(runtime, '_workspace_cleanup_offset', 0)
    # Only Cycle-owned Target/stage work is eligible. Stable Companion HC
    # workspaces (including after /new) and independent Writing never enter
    # this candidate set and must not be added as finished provider scratch.
    with database.read() as connection:
        candidates = connection.execute(text("SELECT w.workspace_ref,w.root_name,w.target_ref, "
            "w.target_run_ref AS work_ref, 'target' AS root_kind, r.cycle_ref, l.status AS lifecycle_status, "
            "MAX(l.updated_at,cycle.updated_at) AS completed_at, l.completion_ref,m.manifest_ref,c.commit_ref "
            "FROM ar_target_run_workspaces w JOIN ar_target_root_lifecycles l USING(target_ref) "
            "JOIN ar_target_launches a USING(target_ref) "
            "JOIN ae_stage_run_requests r ON r.request_ref=a.stage_request_ref "
            "JOIN ae_cycles cycle ON cycle.cycle_ref=r.cycle_ref "
            "LEFT JOIN rm_target_root_completion_manifests m ON m.completion_ref=l.completion_ref "
            "LEFT JOIN rg_target_commits c ON c.target_ref=w.target_ref "
            "WHERE l.status IN ('completed','cancelled') UNION ALL "
            "SELECT NULL,NULL,NULL,s.run_ref,s.stage,s.cycle_ref,s.status, "
            "MAX(s.updated_at,cycle.updated_at),NULL,NULL,c.commit_ref FROM ar_stage_runs s "
            "JOIN ae_cycles cycle USING(cycle_ref) JOIN ae_stage_commits c ON c.request_ref=s.request_ref "
            "WHERE cycle.status='completed' AND s.status='completed' "
            "ORDER BY completed_at,work_ref LIMIT :limit OFFSET :offset"),
            {'limit': limit, 'offset': offset}).mappings().all()
    runtime._workspace_cleanup_offset = offset + len(candidates) if len(candidates) == limit else 0
    report = []
    for candidate in candidates:
        candidate = dict(candidate)
        if candidate['root_kind'] == 'target':
            root = target_root
            path = root / candidate['root_name']
            expected = canonical_hash({'workspace_ref': candidate['workspace_ref']})
        else:
            if runtime._root_workspaces is None:
                continue
            locations = runtime._root_workspaces.read_cycle_stage_workspace_locations(candidate['cycle_ref'])
            location = next(item for item in locations if item.work_ref == candidate['work_ref'])
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
            if not path.exists():
                record['reason'] = 'already_removed'
                continue
            if candidate['lifecycle_status'] != 'completed' or not candidate['commit_ref']:
                record['reason'] = 'handoff_incomplete'
                continue
            if now - candidate['completed_at'] < retention:
                record['reason'] = 'retention_period'
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
            with database.fenced_write() as connection:
                reason = _pending_dependency(connection, candidate, path)
                if reason:
                    record['reason'] = reason
                    continue
                record['bytes'] = _workspace_bytes(path)
                if any(_paths_overlap(path, protected)
                       for protected in (*runtime._protected_storage_roots, journal_root)):
                    record['reason'] = 'retained_content_storage'
                    continue
                reason, journal, scope = _cleanup_scope(candidate, path, journal_root, dry_run=dry_run)
                if reason:
                    record['reason'] = reason
                    continue
                record['reason'] = 'published_verified_completed_cycle_workspace'
                record['action'] = 'candidate'
                if not dry_run:
                    # Recheck immediately before the fd-based recursive remove.
                    _workspace_bytes(path)
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        shutil.rmtree(candidate['root_name'], dir_fd=descriptor)
                    finally:
                        os.close(descriptor)
                    _write_cleanup_scope(journal, {**scope, 'state': 'removed'})
                    record['action'] = 'removed'
        except (OwnerConflict, OSError) as error:
            record['reason'] = error.code if isinstance(error, OwnerConflict) else type(error).__name__
    return tuple(report)
