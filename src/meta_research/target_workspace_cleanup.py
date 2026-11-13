"""Reclaim registered scratch outside a Quest's three distinct Cycle window.

Only linked-local originals survive in expired workspaces. Native retirement
has its own eligibility checks; no research database transaction spans file IO.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import shutil
import stat

from sqlalchemy import text

from meta_research.owners.advancement_engine import recent_workspace_cycles
from meta_research.owners.common import OwnerConflict, canonical_hash

_SAFE_RMTREE = getattr(shutil.rmtree, 'avoids_symlink_attacks', False)


def _workspace_bytes(path: Path, protected=()) -> int:
    """One boundary/size pass; never descend into a registered original directory."""
    root_info = path.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or path.is_mount():
        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
    total = 0

    def failed(error):
        raise error

    for directory, names, files in os.walk(path, followlinks=False, onerror=failed):
        names[:] = [name for name in names if Path(directory) / name not in protected]
        for name in (*names, *files):
            child = Path(directory) / name
            if child in protected:
                continue
            info = child.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_dev != root_info.st_dev
                    or child.is_mount() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            # Necessary parents remain in place, so exclude their allocation.
            if not any(child in original.parents for original in protected):
                total += getattr(info, 'st_blocks', (info.st_size + 511)//512) * 512
    return total


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _linked_locators(connection):
    return tuple(connection.execute(text("SELECT source_locator FROM rm_asset_custodies "
        "WHERE custody_mode='linked_local' ORDER BY source_locator")).scalars())


def _protected_originals(locators):
    """Resolve real originals after closing the short database snapshot."""
    paths = set()
    for locator in locators:
        if not isinstance(locator, str) or not locator or not Path(locator).is_absolute():
            raise OwnerConflict('linked_local_locator_unavailable')
        try:
            original = Path(locator).resolve(strict=True)
            if not original.is_file() and not original.is_dir():
                raise OwnerConflict('linked_local_locator_unavailable')
            paths.add(original)
        except (OSError, RuntimeError, ValueError) as error:
            raise OwnerConflict('linked_local_locator_unavailable') from error
    return tuple(paths)


@contextmanager
def _directory_fd(path):
    """Open every component without following links, including workspace parents."""
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW')
            or not _SAFE_RMTREE):
        raise OwnerConflict('workspace_cleanup_safe_delete_unavailable')
    with ExitStack() as stack:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        yield descriptor


def _prune_workspace(path, protected, descriptor):
    """Keep exact originals and their ancestors; remove siblings via held fds."""
    for name in os.listdir(descriptor):
        child = path / name
        if child in protected:
            continue
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OwnerConflict('workspace_cleanup_unsafe_boundary')
        if any(child in original.parents for original in protected):
            held = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                _prune_workspace(child, protected, held)
            finally:
                os.close(held)
        elif stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _window_reason(connection, candidate, windows):
    quest = candidate['quest_ref']
    if not quest or not candidate['cycle_ref'] or not candidate['lineage_valid']:
        return 'workspace_cycle_lineage_unavailable'
    if quest not in windows:
        try:
            windows[quest] = recent_workspace_cycles(connection, quest)
        except OwnerConflict:
            windows[quest] = ()
    if not windows[quest]:
        return 'workspace_cycle_window_unavailable'
    if candidate['cycle_ref'] in windows[quest]:
        return 'recent_cycle_window'
    lineage = connection.execute(text("SELECT 1 FROM ae_foreground_grants g "
        "JOIN ae_foreground_heads h USING(quest_ref) WHERE g.quest_ref=:quest "
        "AND g.cycle_ref=:cycle AND g.epoch<=h.epoch LIMIT 1"),
        {'quest': quest, 'cycle': candidate['cycle_ref']}).first()
    return None if lineage else 'workspace_cycle_lineage_unavailable'


def cleanup_completed_workspaces(runtime, *, dry_run=True, limit=10):
    # Keep the public method name for existing callers; completion is no longer
    # a workspace retention gate. Status/handoff columns are for native GC only.
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('workspace_cleanup_limit_invalid')
    database = runtime._database
    target_root = runtime._target_agent._workspace_root
    if database is None or target_root is None:
        return ()
    target_root = Path(target_root)
    offset = getattr(runtime, '_workspace_cleanup_offset', 0)
    with database.read_snapshot() as connection:
        candidates = [dict(row) for row in connection.execute(text("""
            SELECT w.workspace_ref AS workspace_ref,w.root_name,w.target_ref,w.target_run_ref AS work_ref,
                'target' AS root_kind,r.cycle_ref,cycle.quest_ref,cycle.status AS cycle_status,
                l.status AS lifecycle_status,l.completion_ref,m.manifest_ref,c.commit_ref,
                (w.target_run_ref=a.target_run_ref AND a.quest_ref=cycle.quest_ref
                 AND r.quest_ref=cycle.quest_ref) AS lineage_valid
            FROM ar_target_run_workspaces w
            LEFT JOIN ar_target_launches a USING(target_ref)
            LEFT JOIN ar_target_root_lifecycles l USING(target_ref)
            LEFT JOIN ae_stage_run_requests r ON r.request_ref=a.stage_request_ref
            LEFT JOIN ae_cycles cycle ON cycle.cycle_ref=r.cycle_ref
            LEFT JOIN rm_target_root_completion_manifests m ON m.completion_ref=l.completion_ref
            LEFT JOIN rg_target_commits c ON c.target_ref=w.target_ref
            UNION ALL
            SELECT NULL,NULL,NULL,s.run_ref,s.stage,s.cycle_ref,cycle.quest_ref,cycle.status,
                s.status,NULL,NULL,c.commit_ref,
                (s.cycle_ref=r.cycle_ref AND s.stage=r.stage AND s.epoch=r.epoch
                 AND r.quest_ref=cycle.quest_ref) AS lineage_valid
            FROM ar_stage_runs s
            LEFT JOIN ae_cycles cycle USING(cycle_ref)
            LEFT JOIN ae_stage_run_requests r ON r.request_ref=s.request_ref
            LEFT JOIN ae_stage_commits c ON c.request_ref=s.request_ref
            ORDER BY work_ref,workspace_ref LIMIT :limit OFFSET :offset
        """), {'limit': limit, 'offset': offset}).mappings()]
        windows = {}
        reasons = [_window_reason(connection, candidate, windows) for candidate in candidates]
        locators = _linked_locators(connection)
    runtime._workspace_cleanup_offset = offset + len(candidates) if len(candidates) == limit else 0
    report = []
    native_report = []
    # Evaluate native safety before workspace deletion can remove old finalizer
    # markers. Native cleanup owns its DB snapshots and its stricter conditions.
    if runtime._native_cleanup is not None:
        from meta_research.native_session_cleanup import cleanup_cycle_sessions
        native_report = cleanup_cycle_sessions(runtime, candidates, dry_run=dry_run)
    try:
        originals = _protected_originals(locators)
        storage = tuple(Path(p).resolve(strict=False) for p in runtime._protected_storage_roots)
        protection_error = None
    except (OwnerConflict, OSError, RuntimeError, ValueError) as error:
        originals, storage = (), ()
        protection_error = error.code if isinstance(error, OwnerConflict) else type(error).__name__
    for candidate, reason in zip(candidates, reasons):
        record = {'workspace_ref': candidate['workspace_ref'], 'cycle_ref': candidate['cycle_ref'],
            'root_kind': candidate['root_kind'], 'path': None, 'bytes': 0,
            'action': 'skipped', 'reason': reason}
        report.append(record)
        if candidate.get('native_cleanup_reason'):
            record['native_cleanup_reason'] = candidate['native_cleanup_reason']
        try:
            if candidate['root_kind'] == 'target':
                root = target_root
                expected = canonical_hash({'workspace_ref': candidate['workspace_ref']})
                if candidate['root_name'] != expected:
                    raise OwnerConflict('workspace_cleanup_unsafe_boundary')
                path = root / expected
            else:
                if runtime._root_workspaces is None:
                    record['reason'] = 'workspace_root_kind_unavailable'
                    continue
                location = runtime._root_workspaces.read_stage_workspace_location(
                    candidate['work_ref'], candidate['cycle_ref'], candidate['root_kind'])
                root = runtime._root_workspaces._bases.get(candidate['root_kind'])
                path = location.directory
                expected = canonical_hash({'work_ref': candidate['work_ref']})
                record['workspace_ref'] = location.workspace_ref
            record['path'] = str(path)
            if (root is None or not root.is_absolute() or root.resolve() != root
                    or path != root / expected or path.is_symlink() or path.resolve(strict=False) != path):
                raise OwnerConflict('workspace_cleanup_unsafe_boundary')
            if reason or protection_error:
                record['reason'] = reason or protection_error
                continue
            if not path.exists():
                record['reason'] = 'already_removed'
                continue
            if any(_paths_overlap(path, base) for base in storage):
                record['reason'] = 'retained_content_storage'
                continue
            protected = tuple(original for original in originals if _paths_overlap(path, original))
            if any(path == original or original in path.parents for original in protected):
                record['reason'] = 'protected_original'
                continue
            identity = path.stat()
            amount = _workspace_bytes(path, protected)
            record.update(action='candidate', reason='expired_cycle_workspace', bytes=amount)
            if dry_run:
                continue
            # A slow scan must not use a stale Cycle window or custody snapshot.
            # This recheck reads only database facts, and closes before any IO.
            with database.read_snapshot() as connection:
                changed = _linked_locators(connection) != locators
                reason = _window_reason(connection, candidate, {})
            if changed or reason:
                record.update(action='skipped', reason=reason or 'linked_local_scope_changed', bytes=0)
                continue
            with _directory_fd(root) as parent:
                held = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    current = os.fstat(held)
                    if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                        raise OwnerConflict('workspace_cleanup_unsafe_boundary')
                    if protected:
                        _prune_workspace(path, protected, held)
                        record.update(action='pruned', reason='originals_preserved_in_place')
                    else:
                        shutil.rmtree(path.name, dir_fd=parent)
                        record['action'] = 'removed'
                finally:
                    os.close(held)
        except (OwnerConflict, OSError, RuntimeError, ValueError) as error:
            record.update(action='skipped', bytes=0,
                reason=error.code if isinstance(error, OwnerConflict) else type(error).__name__)
    return tuple([*report, *native_report])
