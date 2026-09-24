"""Bounded reclamation of published Target scratch workspaces.

Owner completion, publication, verified formal readback and quiescence are all
required. Content custody and native Session storage remain outside this scope.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import time

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash


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


def _pending_dependency(connection, target_ref, path):
    row = connection.execute(text("SELECT status FROM ar_target_root_lifecycles WHERE target_ref=:ref"),
                             {'ref': target_ref}).first()
    if row is None or row.status != 'completed':
        return 'handoff_incomplete'
    active = connection.execute(text("SELECT 1 FROM ar_harness_provider_operations p JOIN "
        "ar_target_run_workspaces w ON w.target_run_ref=p.run_ref WHERE w.target_ref=:ref "
        "AND p.status IN ('running','unknown_outcome') LIMIT 1"), {'ref': target_ref}).first()
    children = connection.execute(text("SELECT 1 FROM ar_target_harness_child_sessions s JOIN "
        "ar_target_run_workspaces w ON w.target_run_ref=s.target_run_ref WHERE w.target_ref=:ref "
        "AND s.completion_evidence_ref IS NULL LIMIT 1"), {'ref': target_ref}).first()
    if active or children:
        return 'active_or_recoverable_session'
    if (path / '.target-completion-intakes').exists():
        return 'pending_finalizer_intake'
    for row in connection.execute(text("SELECT source_locator FROM rm_asset_custodies "
                                       "WHERE custody_mode='linked_local'")):
        if not row.source_locator:
            return 'linked_local_locator_unavailable'
        locator = Path(row.source_locator).resolve(strict=False)
        if locator == path or path in locator.parents or locator in path.parents:
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
    root = runtime._target_agent._workspace_root
    if database is None or root is None:
        return ()
    root = Path(root)
    if root.is_symlink() or root.resolve() != root:
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    offset = getattr(runtime, '_workspace_cleanup_offset', 0)
    with database.read() as connection:
        candidates = connection.execute(text("SELECT w.*, l.status AS lifecycle_status, "
            "l.updated_at AS completed_at, l.completion_ref, m.manifest_ref, c.commit_ref "
            "FROM ar_target_run_workspaces w JOIN ar_target_root_lifecycles l USING(target_ref) "
            "LEFT JOIN rm_target_root_completion_manifests m ON m.completion_ref=l.completion_ref "
            "LEFT JOIN rg_target_commits c ON c.target_ref=w.target_ref "
            "WHERE l.status IN ('completed','cancelled') ORDER BY l.updated_at,w.workspace_ref LIMIT :limit OFFSET :offset"),
            {'limit': limit, 'offset': offset}).mappings().all()
    runtime._workspace_cleanup_offset = offset + len(candidates) if len(candidates) == limit else 0
    report = []
    for candidate in candidates:
        path = root / candidate['root_name']
        record = {'workspace_ref': candidate['workspace_ref'], 'path': str(path), 'bytes': 0,
                  'action': 'skipped', 'reason': None}
        report.append(record)
        try:
            if (candidate['root_name'] != canonical_hash({'workspace_ref': candidate['workspace_ref']})
                    or path.parent != root or path.is_symlink() or path.resolve(strict=False) != path):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            if not path.exists():
                record['reason'] = 'already_removed'
                continue
            if candidate['lifecycle_status'] != 'completed' or not candidate['manifest_ref'] or not candidate['commit_ref']:
                record['reason'] = 'handoff_incomplete'
                continue
            if now - candidate['completed_at'] < retention:
                record['reason'] = 'retention_period'
                continue
            # Public Owner readbacks authenticate the immutable handoff and all
            # retained manifest bindings without exporting another whole copy.
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
            with database.fenced_write() as connection:
                reason = _pending_dependency(connection, candidate['target_ref'], path)
                if reason:
                    record['reason'] = reason
                    continue
                record['bytes'] = _workspace_bytes(path)
                record['reason'] = 'published_verified_completed_workspace'
                record['action'] = 'candidate'
                if not dry_run:
                    # Recheck immediately before the fd-based recursive remove.
                    _workspace_bytes(path)
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        shutil.rmtree(candidate['root_name'], dir_fd=descriptor)
                    finally:
                        os.close(descriptor)
                    record['action'] = 'removed'
        except (OwnerConflict, OSError) as error:
            record['reason'] = error.code if isinstance(error, OwnerConflict) else type(error).__name__
    return tuple(report)
