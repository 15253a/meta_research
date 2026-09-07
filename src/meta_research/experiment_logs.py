from __future__ import annotations

import codecs
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError


MAX_DEPTH = 5
MAX_ENTRIES = 2048
MAX_LOGS = 64
MAX_PAGE_BYTES = 256 * 1024
DEFAULT_PAGE_BYTES = 64 * 1024
_EXCLUDED = frozenset({
    'input', 'inputs', 'data', 'dataset', 'datasets', 'fixture', 'fixtures',
    'test', 'tests', 'testdata', 'node_modules', 'vendor', 'third_party',
    'site-packages', 'venv', 'env', '__pycache__', 'source', 'sources',
})
_OUTPUT_ROOTS = frozenset({'logs', 'log', 'outputs', 'output', 'runs', 'training', 'evaluation'})
_LOG_NAME = re.compile(r'^(train|training|eval|evaluation)(?:[._-][a-z0-9._-]+)?\.(log|out|stdout|stderr)$', re.I)


class ExperimentLogError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class _Scope:
    target_ref: str
    target_run_ref: str
    workspace_ref: str
    target_status: str = field(compare=False)

    @property
    def root_name(self) -> str:
        return canonical_hash({'workspace_ref': self.workspace_ref})

    def public(self) -> dict[str, str]:
        return {'target_ref': self.target_ref, 'target_run_ref': self.target_run_ref,
                'workspace_ref': self.workspace_ref, 'target_status': self.target_status}


class TargetExperimentLogs:
    """Observe only named experiment output files in an accepted Target workspace.

    The authority supplies the current immutable workspace binding. Discovery
    never accepts a user pathname, follows links, invokes a provider, or creates
    a workspace. The bounded metadata cache only detects file generations.
    """
    def __init__(self, authority: Any, workspace_root: Path):
        self._authority = authority
        self._root = workspace_root.absolute()
        self._lock = threading.RLock()
        self._observed: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _scope(self, target_ref: str, expected_run: str | None) -> _Scope:
        try:
            admission = self._authority.query_target_harness_admission(target_ref)
            if admission is None:
                raise ExperimentLogError('experiment_log_workspace_unavailable', 503)
            if expected_run is not None and expected_run != admission.target_run_ref:
                raise ExperimentLogError('experiment_log_reset_required')
            workspace = self._authority.query_target_workspace(admission.target_run_ref)
            if workspace is None:
                raise ExperimentLogError('experiment_log_workspace_unavailable', 503)
            if (workspace.target_ref, workspace.target_run_ref, workspace.root_session_ref,
                workspace.target_attempt_ref, workspace.target_fence_ref) != (
                target_ref, admission.target_run_ref, admission.root_session_ref,
                admission.execution_attempt_ref, admission.execution_fence_ref):
                raise ExperimentLogError('experiment_log_workspace_binding_invalid')
            return _Scope(target_ref, admission.target_run_ref, workspace.workspace_ref, admission.status)
        except (OwnerConflict, AgentRuntimeHarnessError) as error:
            raise ExperimentLogError('experiment_log_workspace_unavailable', 503) from error

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    @contextmanager
    def _workspace(self, scope: _Scope):
        base = directory = -1
        try:
            base = os.open(self._root, self._directory_flags())
            directory = os.open(scope.root_name, self._directory_flags(), dir_fd=base)
            yield directory
        except OSError as error:
            raise ExperimentLogError('experiment_log_workspace_unavailable', 503) from error
        finally:
            if directory >= 0:
                os.close(directory)
            if base >= 0:
                os.close(base)

    @staticmethod
    def _kind(relative: str) -> str | None:
        parts = relative.split('/')
        if any(part.startswith('.') or part.lower() in _EXCLUDED for part in parts[:-1]):
            return None
        if len(parts) > 1 and not any(part.lower() in _OUTPUT_ROOTS for part in parts[:-1]):
            return None
        match = _LOG_NAME.fullmatch(parts[-1])
        if match is None:
            return None
        return 'train' if match[1].lower().startswith('train') else 'eval'

    @staticmethod
    def _log_ref(scope: _Scope, relative: str) -> str:
        return 'log_' + canonical_hash({**scope.public(), 'target_status': None, 'path': relative})[:40]

    @staticmethod
    def _regular(fd: int) -> os.stat_result:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ExperimentLogError('experiment_log_not_found', 404)
        return value

    def _generation(self, scope: _Scope, relative: str, fd: int) -> tuple[str, os.stat_result]:
        key = self._log_ref(scope, relative)
        with self._lock:
            # Sample under the same lock as the previous observation: a delayed
            # reader must not overwrite a newer append with a stale file size.
            value = self._regular(fd)
            identity = (value.st_dev, value.st_ino)
            old = self._observed.get(key)
            changed = old is None or old['identity'] != identity or value.st_size < old['size']
            if not changed and old is not None:
                # Prefix plus the previous observed tail detects copytruncate
                # even when the writer has already regrown beyond the cursor.
                changed = (
                    hashlib.sha256(os.pread(fd, old['prefix_length'], 0)).digest() != old['prefix_hash']
                    or hashlib.sha256(os.pread(fd, old['tail_length'], old['tail_offset'])).digest() != old['tail_hash']
                )
            generation = secrets.token_hex(20) if changed else old['generation']
            prefix_length = min(value.st_size, 4096)
            tail_offset = max(0, value.st_size - 256)
            tail_length = value.st_size - tail_offset
            self._observed[key] = {
                'identity': identity, 'size': value.st_size, 'generation': generation,
                'prefix_length': prefix_length,
                'prefix_hash': hashlib.sha256(os.pread(fd, prefix_length, 0)).digest(),
                'tail_offset': tail_offset, 'tail_length': tail_length,
                'tail_hash': hashlib.sha256(os.pread(fd, tail_length, tail_offset)).digest(),
            }
            self._observed.move_to_end(key)
            while len(self._observed) > 512:
                self._observed.popitem(last=False)
        return generation, value

    @contextmanager
    def _file(self, workspace_fd: int, relative: str):
        pieces = relative.split('/')
        if not pieces or any(part in {'', '.', '..'} for part in pieces):
            raise ExperimentLogError('experiment_log_not_found', 404)
        directory = os.dup(workspace_fd)
        fd = -1
        try:
            for part in pieces[:-1]:
                next_directory = os.open(part, self._directory_flags(), dir_fd=directory)
                os.close(directory)
                directory = next_directory
            fd = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            self._regular(fd)
            yield fd, directory, pieces[-1]
        except OSError as error:
            raise ExperimentLogError('experiment_log_not_found', 404) from error
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(directory)

    def _discover(self, scope: _Scope, workspace_fd: int) -> tuple[list[dict[str, Any]], bool]:
        logs: list[dict[str, Any]] = []
        visited = 0
        truncated = False

        def visit(directory: int, prefix: str, depth: int):
            nonlocal visited, truncated
            # Limit iteration itself; never materialize an unbounded directory.
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        truncated = True
                        break
                    names.append(entry.name)
            for name in sorted(names, key=lambda item: (item.lower() not in _OUTPUT_ROOTS, item)):
                if len(logs) >= MAX_LOGS:
                    truncated = True
                    return
                if name.startswith('.') or name.lower() in _EXCLUDED:
                    continue
                relative = prefix + name
                try:
                    value = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(value.st_mode):
                        if depth >= MAX_DEPTH:
                            truncated = True
                            continue
                        child = os.open(name, self._directory_flags(), dir_fd=directory)
                        try:
                            visit(child, relative + '/', depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                        kind = self._kind(relative)
                        if kind is None:
                            continue
                        with self._file(workspace_fd, relative) as (fd, _parent, _name):
                            stream_ref, current = self._generation(scope, relative, fd)
                            logs.append({'log_ref': self._log_ref(scope, relative), 'name': name,
                                         'relative_path': relative, 'kind': kind,
                                         'source_bytes': current.st_size, 'modified_at': current.st_mtime,
                                         'stream_ref': stream_ref})
                except (OSError, ExperimentLogError):
                    continue
                if visited >= MAX_ENTRIES:
                    return
        visit(workspace_fd, '', 0)
        logs.sort(key=lambda item: (item['relative_path'] not in {'logs/train.log', 'logs/eval.log'}, -item['modified_at'], item['relative_path']))
        return logs, truncated

    def list(self, target_ref: str, *, target_run_ref: str | None = None) -> dict[str, Any]:
        try:
            scope = self._scope(target_ref, target_run_ref)
            with self._workspace(scope) as directory:
                logs, truncated = self._discover(scope, directory)
            if self._scope(target_ref, target_run_ref) != scope:
                raise ExperimentLogError('experiment_log_reset_required')
            return {'schema_ref': 'meta-research/experiment-log-list/v1', **scope.public(),
                    'status': 'ready' if logs else 'empty', 'logs': logs,
                    'default_log_ref': logs[0]['log_ref'] if logs else None,
                    'truncated': truncated, 'reason': None}
        except ExperimentLogError as error:
            if error.code != 'experiment_log_workspace_unavailable':
                raise
            return {'schema_ref': 'meta-research/experiment-log-list/v1',
                    'target_ref': target_ref, 'target_run_ref': target_run_ref, 'workspace_ref': None,
                    'target_status': None, 'status': 'unavailable', 'logs': [], 'default_log_ref': None,
                    'truncated': False, 'reason': {'code': error.code}}

    def read(self, target_ref: str, log_ref: str, *, target_run_ref: str | None = None,
             after: int | None = None, before: int | None = None, stream_ref: str | None = None,
             limit: int = DEFAULT_PAGE_BYTES) -> dict[str, Any]:
        if (after is not None and before is not None) or not 4 <= limit <= MAX_PAGE_BYTES or any(value is not None and value < 0 for value in (after, before)):
            raise ExperimentLogError('experiment_log_cursor_invalid', 422)
        if (after is not None or before is not None) and not stream_ref:
            raise ExperimentLogError('experiment_log_cursor_invalid', 422)
        scope = self._scope(target_ref, target_run_ref)
        with self._workspace(scope) as directory:
            logs, _truncated = self._discover(scope, directory)
            selected = next((item for item in logs if item['log_ref'] == log_ref), None)
            if selected is None:
                raise ExperimentLogError('experiment_log_not_found', 404)
            relative = selected['relative_path']
            with self._file(directory, relative) as (fd, parent, name):
                generation, value = self._generation(scope, relative, fd)
                if stream_ref is not None and stream_ref != generation:
                    raise ExperimentLogError('experiment_log_reset_required')
                if after is not None and after > value.st_size or before is not None and before > value.st_size:
                    raise ExperimentLogError('experiment_log_reset_required')
                end = min(value.st_size, before) if before is not None else value.st_size
                start = after if after is not None else max(0, end - limit)
                end = min(end, start + limit)
                raw = os.pread(fd, end - start, start)
                if after is None and start > 0:
                    while raw and raw[0] & 0xC0 == 0x80:
                        raw = raw[1:]
                        start += 1
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                text = decoder.decode(raw, final=False)
                pending = len(decoder.getstate()[0])
                next_offset = start + len(raw) - pending
                try:
                    raw[:len(raw) - pending].decode('utf-8', errors='strict')
                    replacements = False
                except UnicodeDecodeError:
                    replacements = True
                final_generation, final_stat = self._generation(scope, relative, fd)
                path_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if final_generation != generation or (path_stat.st_dev, path_stat.st_ino) != (value.st_dev, value.st_ino):
                    raise ExperimentLogError('experiment_log_reset_required')
                if self._scope(target_ref, target_run_ref) != scope:
                    raise ExperimentLogError('experiment_log_reset_required')
                return {'schema_ref': 'meta-research/experiment-log-page/v1', **scope.public(),
                        **selected, 'stream_ref': generation, 'text': text, 'offset': start,
                        'next_offset': next_offset, 'source_bytes': final_stat.st_size,
                        'modified_at': final_stat.st_mtime, 'has_more': end < final_stat.st_size,
                        'source_caught_up': end >= final_stat.st_size,
                        'pending_utf8_bytes': pending, 'decode_replacements': replacements}
