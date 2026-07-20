"""Web-first quest setup without browser-visible host paths.

The HTTP console delegates every post-deployment setup mutation to this
service.  Browser inputs are limited to goal text, relative display names and
file bytes.  Files first land in :class:`QuestDraftRegistry`; publication then
moves the already-verified tree into the quest (same filesystem, no second
multi-GiB copy), writes a content-addressed corpus receipt, and only afterwards
marks the quest ready for a Web-owned research process.

Qualification contracts are never accepted from the browser.  They can only
come from the deployment-owned immutable profile catalog.  Dataset preflight
is deliberately advisory and never upgrades itself into scientific
qualification.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .dataset_preflight import (
    DatasetPreflightLimits,
    ManagedDraftManifest,
    preflight_managed_datasets,
)
from .qualification_profiles import QualificationProfileRegistry
from .local_sources import LocalSourceRegistry
from .quest_drafts import DraftCorruptError, QuestDraftRegistry
from .quest_process_manager import QuestProcessManager
from .quest_registry import Quest, QuestRegistry
from .quest_runtime_profiles import (
    DEFAULT_PROFILE,
    QuestRuntimeSettings,
    RuntimeProfileConflictError,
    RuntimeSettingsCorruptError,
    normalize_profile,
    public_options,
)


_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_QUEST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_VERSION = 1
_JOURNAL_VERSION = 2
_LEGACY_JOURNAL_VERSION = 1
_CORPUS_VERSION = 1
_READY_VERSION = 1
_REQUEST_PUBLICATION_VERSION = 1
_PUBLISH_JOB_VERSION = 1
_MAX_OPERATION_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_LOCAL_MANIFEST_BYTES = 128 * 1024 * 1024
_MAX_PUBLIC_TEMPLATES = 128
_T1_TEMPLATE_ID = "t1-eeg-universal"


class WebQuestServiceError(RuntimeError):
    """A managed Web setup operation failed closed."""


class WebQuestConflictError(WebQuestServiceError):
    """An idempotency key or immutable setup identity was reused differently."""


class WebQuestNotReadyError(WebQuestServiceError):
    """A quest has not completed its managed setup boundary."""


class WebQuestRetryableError(WebQuestServiceError):
    """A durable mutation still needs the same-key side effect retry."""

    operation_state = "saved_pending_restart"

    def __init__(self, message: str, idempotency_key: str):
        super().__init__(message)
        self.idempotency_key = _validate_key(idempotency_key)


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValueError("Web setup value 无法 canonicalize") from error


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_key(value: object, *, label: str = "idempotency_key") -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 须为 32 位小写 hex")
    return value


def _validate_quest_id(value: object) -> str:
    if not isinstance(value, str) or _QUEST_RE.fullmatch(value) is None:
        raise ValueError("quest_id 非法")
    return value


def _regular_file(path: Path, *, label: str, mode: Optional[int] = None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise WebQuestServiceError(f"{label} 不可读") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise WebQuestServiceError(f"{label} owner/type/link/mode 非法")
    return info


def _directory(path: Path, *, label: str, mode: Optional[int] = None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise WebQuestServiceError(f"{label} 不可读") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise WebQuestServiceError(f"{label} owner/type/mode 非法")
    return info


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _strict_read_json(path: Path, *, label: str,
                      maximum: int = _MAX_MANIFEST_BYTES) -> Dict[str, Any]:
    info = _regular_file(path, label=label)
    if not 2 <= info.st_size <= maximum:
        raise WebQuestServiceError(f"{label} 大小非法")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise WebQuestServiceError(f"{label} 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             before.st_ctime_ns, before.st_mode, before.st_uid, before.st_nlink)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                    after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)):
            raise WebQuestServiceError(f"{label} 读取期间身份漂移")
    finally:
        os.close(fd)

    def unique(pairs):  # noqa: ANN001 - json hook
        value = {}
        for key, item in pairs:
            if key in value:
                raise WebQuestServiceError(f"{label} 含重复 key")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                WebQuestServiceError(f"{label} 含非有限数: {token}")))
    except WebQuestServiceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WebQuestServiceError(f"{label} 不是严格 JSON") from error
    if not isinstance(value, dict) or raw != _canonical(value):
        raise WebQuestServiceError(f"{label} 非 canonical JSON object")
    return value


def _freeze_tree(root: Path) -> None:
    """Make the managed startup corpus read-only after publication."""
    _directory(root, label="quest corpus")
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            info = _regular_file(path, label="quest corpus file")
            if stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
                raise WebQuestServiceError("quest corpus file mode 漂移")
            os.chmod(path, 0o400, follow_symlinks=False)
        for name in dirs:
            path = current_path / name
            info = _directory(path, label="quest corpus directory")
            if stat.S_IMODE(info.st_mode) not in {0o500, 0o700}:
                raise WebQuestServiceError("quest corpus directory mode 漂移")
            os.chmod(path, 0o500, follow_symlinks=False)
        os.chmod(current_path, 0o500, follow_symlinks=False)
        _fsync_dir(current_path)


def _hash_regular(path: Path, *, expected_size: int, label: str) -> str:
    """Hash one owner-bound regular file and reject concurrent replacement."""
    before = _regular_file(path, label=label)
    if before.st_size != expected_size:
        raise WebQuestServiceError(f"{label} size 漂移")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
             opened.st_ctime_ns, opened.st_mode, opened.st_uid, opened.st_nlink)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns, before.st_mode, before.st_uid, before.st_nlink)):
            raise WebQuestServiceError(f"{label} 打开时身份漂移")
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise WebQuestServiceError(f"{label} 被截断")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise WebQuestServiceError(f"{label} 读取时变长")
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                    opened.st_ctime_ns, opened.st_mode, opened.st_uid, opened.st_nlink)):
            raise WebQuestServiceError(f"{label} 读取期间身份漂移")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


def _verify_tree_manifest(root: Path, files: object) -> None:
    """Verify that a published request tree is exactly the durable manifest."""
    if not isinstance(files, list) or len(files) > 100_000:
        raise WebQuestServiceError("request publication files 非法")
    expected: Dict[str, Mapping[str, Any]] = {}
    expected_dirs = {""}
    for row in files:
        if (not isinstance(row, Mapping) or set(row) != {"path", "size", "sha256"}
                or not isinstance(row.get("path"), str)
                or isinstance(row.get("size"), bool) or not isinstance(row.get("size"), int)
                or row["size"] < 0 or not isinstance(row.get("sha256"), str)
                or _SHA_RE.fullmatch(row["sha256"]) is None):
            raise WebQuestServiceError("request publication file manifest 非法")
        relative = row["path"]
        pure = PurePosixPath(relative)
        if (pure.is_absolute() or pure.as_posix() != relative or not pure.parts
                or any(part in {"", ".", ".."} or part.startswith(".")
                       for part in pure.parts)
                or relative in expected):
            raise WebQuestServiceError("request publication path 非法/重复")
        expected[relative] = row
        for index in range(1, len(pure.parts)):
            expected_dirs.add(PurePosixPath(*pure.parts[:index]).as_posix())

    _directory(root, label="request publication root")
    seen_files = set()
    seen_dirs = set()
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        names.sort()
        current_path = Path(current)
        rel_dir = current_path.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        _directory(current_path, label="request publication directory")
        if rel_dir not in expected_dirs:
            raise WebQuestServiceError("request publication 含未知目录")
        seen_dirs.add(rel_dir)
        for dirname in dirs:
            _directory(current_path / dirname, label="request publication directory")
        for name in names:
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            row = expected.get(relative)
            if row is None:
                raise WebQuestServiceError("request publication 含未知文件")
            if _hash_regular(
                    child, expected_size=row["size"],
                    label=f"request publication file {relative}") != row["sha256"]:
                raise WebQuestServiceError("request publication file hash 漂移")
            seen_files.add(relative)
    if seen_files != set(expected) or seen_dirs != expected_dirs:
        raise WebQuestServiceError("request publication tree 与 manifest 不一致")


@dataclass(frozen=True)
class RequestUploadPublication:
    """Internal capability.  ``source_ref`` must never be returned to Web."""

    quest_id: str
    request_id: int
    upload_id: str
    source_ref: str


class WebQuestService:
    """Coordinate drafts, preflight, immutable profiles and Web owners."""

    def __init__(
            self, *, registry: QuestRegistry, drafts: QuestDraftRegistry,
            profiles: QualificationProfileRegistry,
            processes: QuestProcessManager,
            local_sources: Optional[LocalSourceRegistry] = None):
        self.registry = registry
        self.drafts = drafts
        self.profiles = profiles
        self.processes = processes
        self.state = registry.state_dir / "web-setup"
        self.operations = self.state / "operations"
        self.finalize_journals = self.state / "finalize"
        self.request_roots = self.state / "file-request-uploads"
        self.request_publications = self.state / "file-request-publications"
        self.local_manifests = self.state / "local-source-manifests"
        self.publish_jobs = self.state / "publish-jobs"
        for path in (self.state, self.operations, self.finalize_journals,
                     self.request_roots, self.request_publications,
                     self.local_manifests, self.publish_jobs):
            if os.path.lexists(path):
                _directory(path, label=path.name, mode=0o700)
            else:
                path.mkdir(mode=0o700)
                _fsync_dir(path.parent)
        self.lock_path = self.state / ".web-setup.lock"
        if not os.path.lexists(self.lock_path):
            _write_new(self.lock_path, b"web-setup-v1\n")
            _fsync_dir(self.state)
        _regular_file(self.lock_path, label="web setup lock", mode=0o600)
        self._publish_guard = threading.RLock()
        self._publish_threads: Dict[str, threading.Thread] = {}
        self._closed = False
        # ``/`` is an explicit single-machine product capability: Host,
        # Origin and bearer checks remain in the HTTP layer, and the registry
        # can read only what this service UID can already read.  Deployments
        # that need a narrower boundary pass an allow-root configured registry.
        self.local_sources = local_sources or LocalSourceRegistry(
            self.state / "local-sources", allowed_roots=[Path("/")])

    @contextmanager
    def _locked(self) -> Iterator[None]:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _directory(self.state, label="web setup state", mode=0o700)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _publish_job_shape(value: Mapping[str, Any]) -> Dict[str, Any]:
        expected = {
            "version", "job_id", "draft_id", "start", "status",
            "input_summary", "result", "error",
        }
        if (set(value) != expected or value.get("version") != _PUBLISH_JOB_VERSION
                or not isinstance(value.get("job_id"), str)
                or _ID_RE.fullmatch(value["job_id"]) is None
                or not isinstance(value.get("draft_id"), str)
                or _ID_RE.fullmatch(value["draft_id"]) is None
                or not isinstance(value.get("start"), bool)
                or value.get("status") not in {"queued", "running", "succeeded", "failed"}
                or not isinstance(value.get("input_summary"), dict)
                or (value.get("result") is not None
                    and not isinstance(value.get("result"), dict))
                or (value.get("error") is not None
                    and not isinstance(value.get("error"), str))):
            raise WebQuestServiceError("publish job receipt 身份非法")
        status = value["status"]
        if ((status == "succeeded") != (value.get("result") is not None)
                or (status == "failed") != (value.get("error") is not None)):
            raise WebQuestServiceError("publish job terminal payload 非法")
        return dict(value)

    def _publish_job_path(self, job_id: str) -> Path:
        return self.publish_jobs / f"{_validate_key(job_id, label='job_id')}.json"

    def _write_publish_job(self, value: Mapping[str, Any]) -> None:
        record = self._publish_job_shape(value)
        path = self._publish_job_path(record["job_id"])
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        _write_new(temporary, _canonical(record))
        os.replace(temporary, path)
        _fsync_dir(self.publish_jobs)

    def _read_publish_job(self, job_id: str) -> Dict[str, Any]:
        path = self._publish_job_path(job_id)
        if not os.path.lexists(path):
            raise KeyError(job_id)
        return self._publish_job_shape(_strict_read_json(
            path, label="publish job receipt"))

    @staticmethod
    def _public_publish_job(record: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "job_id": record["job_id"],
            "status": record["status"],
            "input_summary": dict(record["input_summary"]),
            "result": record["result"],
            "error": record["error"],
        }

    def publish_needs_background(self, draft_id: object) -> bool:
        did = _validate_key(draft_id, label="draft_id")
        self.drafts.get(did)
        return bool(self.local_sources.list(did))

    def _start_publish_worker_locked(self, record: Dict[str, Any]) -> None:
        job_id = record["job_id"]
        existing = self._publish_threads.get(job_id)
        if existing is not None and existing.is_alive():
            return
        if self._closed:
            raise WebQuestServiceError("Web setup service 已关闭")
        running = dict(record)
        running.update(status="running", result=None, error=None)
        self._write_publish_job(running)

        def work() -> None:
            try:
                result = self.publish(
                    running["draft_id"], start=running["start"],
                    idempotency_key=running["job_id"])
                terminal = dict(running)
                terminal.update(status="succeeded", result=result, error=None)
            except BaseException as error:  # durable product error, never escapes daemon thread
                message = str(error) or type(error).__name__
                for secret in (str(self.registry.root), str(self.registry.system_root)):
                    message = message.replace(secret, "[path]")
                message = re.sub(
                    r"(?<![A-Za-z0-9_.-])/(?:[^\s:'\"]+/)+[^\s:'\"]*",
                    "[path]", message)
                terminal = dict(running)
                terminal.update(
                    status="failed", result=None,
                    error=message[:2000] or "任务发布失败")
            with self._publish_guard:
                self._write_publish_job(terminal)
                self._publish_threads.pop(job_id, None)

        thread = threading.Thread(
            target=work, daemon=True, name=f"quest-publish-{job_id[:8]}")
        self._publish_threads[job_id] = thread
        thread.start()

    def submit_publish(self, draft_id: object, *, start: bool,
                       idempotency_key: object) -> Dict[str, Any]:
        did = _validate_key(draft_id, label="draft_id")
        job_id = _validate_key(idempotency_key)
        if not isinstance(start, bool):
            raise ValueError("start 须为 bool")
        self.bind_operation(job_id, "/api/quest-drafts/publish", {
            "draft_id": did, "start": start,
        })
        summaries = self.local_sources.list(did)
        input_summary = {
            "source_count": len(summaries),
            "file_count": sum(item["file_count"] for item in summaries),
            "total_bytes": sum(item["total_bytes"] for item in summaries),
        }
        with self._publish_guard:
            try:
                record = self._read_publish_job(job_id)
            except KeyError:
                record = {
                    "version": _PUBLISH_JOB_VERSION,
                    "job_id": job_id, "draft_id": did, "start": start,
                    "status": "queued", "input_summary": input_summary,
                    "result": None, "error": None,
                }
                self._write_publish_job(record)
            if (record["draft_id"] != did or record["start"] is not start
                    or record["input_summary"] != input_summary):
                raise WebQuestConflictError(
                    "publish job 已绑定不同任务输入")
            if record["status"] in {"queued", "running"}:
                self._start_publish_worker_locked(record)
                record = self._read_publish_job(job_id)
            return self._public_publish_job(record)

    def publish_job_status(self, job_id: object) -> Dict[str, Any]:
        safe_id = _validate_key(job_id, label="job_id")
        with self._publish_guard:
            record = self._read_publish_job(safe_id)
            if record["status"] in {"queued", "running"}:
                self._start_publish_worker_locked(record)
                record = self._read_publish_job(safe_id)
            return self._public_publish_job(record)

    def bind_operation(self, key: object, route: str, identity: Mapping[str, Any]) -> None:
        """Globally bind a Web transport key before the first mutation."""
        safe_key = _validate_key(key)
        if not isinstance(route, str) or not route.startswith("/api/") or len(route) > 200:
            raise ValueError("operation route 非法")
        if not isinstance(identity, Mapping):
            raise ValueError("operation identity 须为 object")
        record = {
            "version": _OPERATION_VERSION,
            "idempotency_key": safe_key,
            "route": route,
            "identity": dict(identity),
        }
        raw = _canonical(record)
        if len(raw) > _MAX_OPERATION_BYTES:
            raise ValueError("operation identity 过大")
        path = self.operations / f"{safe_key}.json"
        with self._locked():
            if os.path.lexists(path):
                existing = _strict_read_json(
                    path, label="Web operation receipt", maximum=_MAX_OPERATION_BYTES)
                if (existing != record
                        and not self._legacy_draft_operation_equivalent(
                            existing, record)):
                    raise WebQuestConflictError(
                        "Idempotency-Key 已绑定不同 Web 操作")
                return
            _write_new(path, raw)
            _fsync_dir(self.operations)

    @staticmethod
    def _legacy_draft_operation_equivalent(
            existing: Mapping[str, Any], requested: Mapping[str, Any]) -> bool:
        """Treat a missing pre-feature draft profile as the explicit default.

        The global operation receipt predates the draft receipt.  Without this
        narrow compatibility rule, a browser replay upgraded from an omitted
        profile to the explicit v1 default would be rejected before
        :class:`QuestDraftRegistry` could apply its equivalent legacy rule.
        """
        envelope = {"version", "idempotency_key", "route", "identity"}
        if (set(existing) != envelope or set(requested) != envelope
                or existing.get("version") != _OPERATION_VERSION
                or requested.get("version") != _OPERATION_VERSION
                or existing.get("idempotency_key")
                != requested.get("idempotency_key")
                or existing.get("route") != "/api/quest-drafts"
                or requested.get("route") != "/api/quest-drafts"
                or not isinstance(existing.get("identity"), Mapping)
                or not isinstance(requested.get("identity"), Mapping)):
            return False
        old_identity = dict(existing["identity"])
        new_identity = dict(requested["identity"])
        old_has = "runtime_profile" in old_identity
        new_has = "runtime_profile" in new_identity
        if old_has == new_has:
            return False
        old_profile = old_identity.pop("runtime_profile", DEFAULT_PROFILE)
        new_profile = new_identity.pop("runtime_profile", DEFAULT_PROFILE)
        if old_identity != new_identity:
            return False
        try:
            return (normalize_profile(old_profile) == DEFAULT_PROFILE
                    and normalize_profile(new_profile) == DEFAULT_PROFILE)
        except ValueError:
            return False

    def _template_title(self, template_id: str, body: str) -> str:
        for line in body.splitlines():
            if line.startswith("# ") and line[2:].strip():
                return line[2:].strip()[:200]
        return template_id

    @staticmethod
    def _template_summary(body: str) -> str:
        """Return the first bounded paragraph under the public ``## 目标`` heading."""
        collecting = False
        lines: List[str] = []
        for raw in body.splitlines():
            line = raw.strip()
            if line == "## 目标":
                collecting = True
                continue
            if collecting and line.startswith("## "):
                break
            if collecting and line:
                lines.append(line)
                if len(" ".join(lines)) >= 360:
                    break
        summary = " ".join(lines).strip()
        return summary[:400] or "预设研究目标、成功判据与执行约束。"

    @staticmethod
    def _template_category(template_id: str) -> str:
        if template_id == "toy-gauss-smoke":
            return "系统自检"
        if template_id == _T1_TEMPLATE_ID:
            return "密封评测"
        return "真实研究"

    def templates(self) -> List[Dict[str, Any]]:
        base = self.registry.system_root / "quest_templates"
        result = []
        for entry in sorted(base.iterdir(), key=lambda item: item.name):
            if len(result) >= _MAX_PUBLIC_TEMPLATES:
                raise WebQuestServiceError("quest template 数超过安全上限")
            if not entry.is_dir() or entry.is_symlink() or _QUEST_RE.fullmatch(entry.name) is None:
                raise WebQuestServiceError("quest_templates 含非法条目")
            body = self.registry.template_text(entry.name)
            title = self._template_title(entry.name, body)
            category = self._template_category(entry.name)
            result.append({
                "template_id": entry.name,
                "title": title,
                "display_title": f"{category}｜{title}",
                "category": category,
                "summary": self._template_summary(body),
                "qualification_task": "T1" if entry.name == _T1_TEMPLATE_ID else None,
            })
        return result

    def _runtime_profile_options(self) -> Dict[str, Any]:
        reader = getattr(self.processes, "runtime_profile_options", None)
        options = reader() if callable(reader) else public_options()
        if not isinstance(options, Mapping):
            raise WebQuestServiceError("runtime profile options 非法")
        return dict(options)

    def _normalize_runtime_profile(self, value: object) -> Dict[str, Any]:
        """Validate legacy pools or exact v3 GPUs against current options."""
        profile = normalize_profile(value)
        selected = profile.get("gpu_device_indices")
        if selected is None or profile["compute_profile_id"] == "local-cpu":
            return profile  # legacy/default profile keeps the full policy pool
        options = self._runtime_profile_options()
        devices = options.get("gpu_devices")
        selection = options.get("gpu_selection")
        if not isinstance(devices, list) or not isinstance(selection, Mapping):
            raise ValueError("当前部署不接受浏览器 GPU 设备选择")
        catalog_indices = [
            row.get("index") for row in devices
            if isinstance(row, Mapping)
        ]
        if (len(catalog_indices) != len(devices)
                or any(not isinstance(index, int) or isinstance(index, bool)
                       or index < 0 for index in catalog_indices)
                or catalog_indices != sorted(set(catalog_indices))):
            raise ValueError("服务端 GPU 设备目录非法")
        allowed = set(catalog_indices)

        if profile["version"] == 2:
            requested = selection.get("requested_count")
            if requested is None and selection.get("mode") == "exact":
                legacy_reader = getattr(
                    self.processes, "runtime_profile_legacy_gpu_count", None)
                requested = (
                    legacy_reader() if callable(legacy_reader) else None)
            if (not isinstance(requested, int) or isinstance(requested, bool)
                    or requested <= 0 or len(selected) < requested
                    or any(index not in allowed for index in selected)):
                raise ValueError(
                    "GPU 候选必须来自服务端允许列表，且不少于任务请求数量")
            return profile

        if profile["version"] != 3 or options.get("version") != 3:
            raise ValueError("当前部署不接受 exact GPU 运行配置")
        minimum = selection.get("min_count")
        maximum = selection.get("max_count")
        if (selection.get("mode") != "exact"
                or not isinstance(minimum, int) or isinstance(minimum, bool)
                or not isinstance(maximum, int) or isinstance(maximum, bool)
                or minimum < 1 or maximum < minimum
                or not minimum <= len(selected) <= maximum
                or any(index not in allowed for index in selected)):
            raise ValueError(
                "exact GPU 选择必须来自当前探测可信列表，且数量须在服务端范围内")
        return profile

    def validate_runtime_profile(self, value: object) -> Dict[str, Any]:
        """HTTP draft boundary: validate before writing a durable draft."""
        return self._normalize_runtime_profile(value)

    def setup_public(self) -> Dict[str, Any]:
        t1_profiles = [
            item for item in self.profiles.list()
            if item.template_id == _T1_TEMPLATE_ID
        ]
        templates = self.templates()
        if not t1_profiles:
            # A first-release user should never be led into a backend runbook.
            # Keep the sealed template unavailable until its deployment-owned
            # evaluator capability actually exists; ordinary real EEG/LODO is
            # covered by the local template without extra user setup.
            templates = [
                item for item in templates
                if item["template_id"] != _T1_TEMPLATE_ID]
        health_reader = getattr(self.processes, "runtime_health", None)
        runtime_health = (
            health_reader() if callable(health_reader)
            else {"ready": True, "checks": {}, "detail": "test_boundary",
                  "disk_free_bytes": None})
        return {
            "version": 1,
            "product_flow": "web-only-after-deployment",
            "templates": templates,
            "qualification_profiles": [item.public_dict() for item in self.profiles.list()],
            "runtime_profile_options": self._runtime_profile_options(),
            "upload": {
                "chunk_max_bytes": 8 * 1024 ** 2,
                "recommended_chunk_bytes": 4 * 1024 ** 2,
                "browser_host_paths_accepted": True,
                "local_directory_attachment": True,
                "local_directory_mode": "read_only_in_place",
            },
            "deployment_health": {
                "local_web_owner": True,
                "research_runtime": runtime_health,
                "outbound": (
                    "disabled" if getattr(
                        self.processes, "no_outbound", True) else "configured"),
                "t1_secure_evaluation": (
                    "ready" if len(t1_profiles) == 1
                    else "not_ready" if not t1_profiles else "selection_required"),
            },
        }

    @staticmethod
    def _preflight_manifest(files: List[Mapping[str, Any]]) -> ManagedDraftManifest:
        rows = []
        for item in files:
            path = item.get("path")
            size = item.get("size")
            digest = item.get("sha256")
            if (not isinstance(path, str) or not isinstance(size, int)
                    or not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None
                    or item.get("status") != "complete"):
                raise WebQuestNotReadyError("draft 尚有未完成文件")
            top = PurePosixPath(path).parts[0]
            rows.append({
                "file_id": "f-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:32],
                "bundle_id": "b-" + hashlib.sha256(top.encode("utf-8")).hexdigest()[:32],
                "stored_relpath": path,
                "display_relpath": path,
                "size_bytes": size,
            })
        limits = DatasetPreflightLimits(
            max_files=100_000, max_directories=100_000, max_depth=64,
            max_total_bytes=256 * 1024 ** 3, max_file_bytes=64 * 1024 ** 3)
        return ManagedDraftManifest.from_value(
            {"version": 1, "files": rows}, limits=limits)

    def preflight(self, draft_id: object) -> Dict[str, Any]:
        did = _validate_key(draft_id, label="draft_id")
        spec = self.drafts.spec(did)
        include_t1 = (
            spec.get("template_id") == _T1_TEMPLATE_ID
            or spec.get("qualification_profile_id") is not None)
        files = self.drafts.files_manifest(did)
        manifest = self._preflight_manifest(files)
        limits = DatasetPreflightLimits(
            max_files=100_000, max_directories=100_000, max_depth=64,
            max_total_bytes=256 * 1024 ** 3, max_file_bytes=64 * 1024 ** 3)
        root = self.drafts.files_root(did)
        reports = [preflight_managed_datasets(
            root, manifest, limits=limits).public_dict()]
        local_manifest = self.local_sources.preflight_manifest(did)
        local_public = {
            "status": "preflighted",
            "file_count": local_manifest["file_count"],
            "total_bytes": local_manifest["total_bytes"],
            "sources": [],
        }
        local_identities = []
        skipped_local_files = 0
        for source in local_manifest["sources"]:
            local_public["sources"].append({
                "source_id": source["source_id"],
                "label": source["label"],
                "kind": source["kind"],
                "file_count": source["file_count"],
                "total_bytes": source["total_bytes"],
                "status": "preflighted",
            })
            local_identities.append({
                "source_id": source["source_id"],
                "kind": source["kind"],
                "source_type": source["source_type"],
                "root_identity": source["root_identity"],
                "files": [{"path": row["path"], "size": row["size"],
                           "identity": row["identity"]}
                          for row in source["files"]],
            })
            if source["kind"] != "dataset":
                continue
            if source["source_type"] != "directory":
                skipped_local_files += 1
                continue
            rows = []
            display_root = "source-" + re.sub(
                r"[^A-Za-z0-9_.-]+", "-", str(source["label"]))
            display_root = display_root[:120].strip(".-") or (
                "source-" + source["source_id"])
            for row in source["files"]:
                path = row["path"]
                rows.append({
                    "file_id": "f-" + hashlib.sha256(
                        (source["source_id"] + "\0" + path).encode("utf-8")
                    ).hexdigest()[:32],
                    "bundle_id": "b-" + source["source_id"],
                    "stored_relpath": path,
                    "display_relpath": display_root + "/" + path,
                    "size_bytes": row["size"],
                })
            source_manifest = ManagedDraftManifest.from_value(
                {"version": 1, "files": rows}, limits=limits)
            reports.append(preflight_managed_datasets(
                Path(source["source_root"]), source_manifest,
                limits=limits).public_dict())
        return self._merge_preflight_reports(
            reports, local_public=local_public,
            local_identity=local_identities,
            skipped_local_files=skipped_local_files,
            include_t1=include_t1)

    @staticmethod
    def _merge_preflight_reports(
            reports: List[Mapping[str, Any]], *,
            local_public: Mapping[str, Any], local_identity: object,
            skipped_local_files: int,
            include_t1: bool = True) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        warnings: List[str] = []
        scans = {
            "file_count": 0, "directory_count": 0, "total_bytes": 0,
            "archive_count": 0, "archive_member_count": 0,
        }
        report_hashes = []
        for report in reports:
            report_hashes.append(report.get("manifest_sha256"))
            for item in report.get("candidates", []):
                if isinstance(item, Mapping):
                    candidates.append(dict(item))
            for warning in report.get("warnings", []):
                if isinstance(warning, str) and warning not in warnings:
                    warnings.append(warning)
            scan = report.get("scan")
            if isinstance(scan, Mapping):
                for name in scans:
                    value = scan.get(name)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        scans[name] += value
        if skipped_local_files:
            warnings.append(
                f"{skipped_local_files} 个单文件本机数据来源已登记，但候选识别仅扫描目录；"
                "这不影响研究进程读取")
        if not include_t1:
            ordinary_caveat = (
                "自动识别仅是文件候选预检，不验证真实性、许可、完整性或标签语义；"
                "任务会把这些作为研究输入边界继续核验。")
            warnings = [
                ordinary_caveat if "qualification firewall" in warning else warning
                for warning in warnings
            ]
            warnings = list(dict.fromkeys(warnings))
        local_files = local_public.get("file_count", 0)
        local_bytes = local_public.get("total_bytes", 0)
        # The individual local dataset scans already contribute dataset
        # directories.  References are still part of startup-input totals.
        dataset_local_files = sum(
            item.get("scan", {}).get("file_count", 0)
            for item in reports[1:] if isinstance(item, Mapping))
        dataset_local_bytes = sum(
            item.get("scan", {}).get("total_bytes", 0)
            for item in reports[1:] if isinstance(item, Mapping))
        if isinstance(local_files, int):
            scans["file_count"] += max(0, local_files - dataset_local_files)
        if isinstance(local_bytes, int):
            scans["total_bytes"] += max(0, local_bytes - dataset_local_bytes)
        if (scans["file_count"] > 100_000
                or scans["total_bytes"] > 256 * 1024 ** 3):
            raise WebQuestNotReadyError(
                "浏览器上传与本机目录合计超过单任务上限"
                "（100000 文件 / 256 GiB）")

        dreamer_count = sum(
            1 for item in candidates if item.get("dataset") == "DREAMER")
        explore = sorted({
            str(item.get("dataset")) for item in candidates
            if item.get("dataset") in {"SEED", "SEED-IV", "FACED", "DEAP", "MPED"}
        })
        requirements_met = dreamer_count == 1 and len(explore) >= 3
        t1 = {
            "task": "T1",
            "candidate_requirements_met": requirements_met,
            "sealed_holdout": {
                "dataset": "DREAMER", "required": "exactly_one_candidate",
                "observed_candidates": dreamer_count,
                "status": "met" if dreamer_count == 1 else "unmet",
            },
            "exploration": {
                "allowed_datasets": ["SEED", "SEED-IV", "FACED", "DEAP", "MPED"],
                "required_distinct": 3, "observed_distinct": len(explore),
                "observed_datasets": explore,
                "status": "met" if len(explore) >= 3 else "unmet",
            },
            "scientific_qualification_status": "not_assessed",
        }
        identity_hash = _digest_bytes(_canonical({
            "managed_reports": report_hashes,
            "local_sources": local_identity,
        }))
        result = {
            "version": 1,
            "protocol": "web-dataset-preflight-v1",
            "manifest_sha256": identity_hash,
            "candidates": candidates,
            "warnings": warnings,
            "scan": scans,
            "local_sources": dict(local_public),
        }
        if include_t1:
            result.update(
                scientific_qualification_status="not_assessed",
                t1_requirements=t1)
        else:
            result["research_input_status"] = "preflighted"
        return result

    def attach_local_source(
            self, draft_id: object, kind: object, source_path: object,
            key: object) -> Dict[str, Any]:
        did = _validate_key(draft_id, label="draft_id")
        safe_key = _validate_key(key)
        self.drafts.get(did)
        if not isinstance(source_path, str):
            raise ValueError("path 须为本机文本路径")
        if os.path.lexists(self.local_manifests / f"{did}.json"):
            raise WebQuestConflictError("quest draft 已进入发布验证，不能再附加本机目录")
        identity = {"draft_id": did, "kind": kind, "path": source_path}
        self.bind_operation(
            safe_key, "/api/quest-drafts/local-sources", identity)
        return self.local_sources.attach(
            did, kind, source_path, safe_key, require_directory=True)

    def _corpus_manifest(self, draft_id: str) -> tuple[Dict[str, Any], bytes, Dict[str, Any]]:
        rows = self.drafts.files_manifest(draft_id)
        entries = []
        total = 0
        for row in rows:
            if row.get("status") != "complete" or not isinstance(row.get("sha256"), str):
                raise WebQuestNotReadyError("draft 尚有未完成文件")
            entries.append({
                "path": row["path"], "size_bytes": row["size"],
                "sha256": row["sha256"],
            })
            total += row["size"]
        value = {"version": _CORPUS_VERSION, "files": entries}
        raw = _canonical(value)
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise WebQuestNotReadyError("corpus manifest 过大")
        summary = {
            "file_count": len(entries), "total_bytes": total,
            "manifest_sha256": _digest_bytes(raw),
        }
        return value, raw, summary

    @staticmethod
    def _local_manifest_identity(value: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: item for key, item in value.items() if key != "generated_at"}

    @staticmethod
    def _local_manifest_metadata_identity(value: Mapping[str, Any]) -> Dict[str, Any]:
        """Project a frozen manifest to its byte-free restart identity.

        Publication is the one operation that reads every source byte and
        freezes SHA-256 values.  A later owner restart must still fail closed
        when the source binding changes, but re-reading hundreds of gigabytes
        merely to resume the same quest makes the Web product unusable.
        ``preflight_manifest`` safely reopens every entry and records the same
        inode/size/mtime/ctime identity without consuming file contents; this
        projection lets that metadata scan be compared with the published
        manifest while deliberately ignoring only verification status,
        generation time and the already-frozen per-file digest.
        """
        projected = {
            key: item for key, item in value.items()
            if key not in {"generated_at", "status", "sources"}
        }
        sources = []
        raw_sources = value.get("sources")
        if not isinstance(raw_sources, list):
            return {**projected, "sources": raw_sources}
        for raw_source in raw_sources:
            if not isinstance(raw_source, Mapping):
                sources.append(raw_source)
                continue
            source = {
                key: item for key, item in raw_source.items()
                if key not in {"status", "files"}
            }
            raw_files = raw_source.get("files")
            if isinstance(raw_files, list):
                source["files"] = [
                    ({key: item for key, item in raw_file.items()
                      if key != "sha256"}
                     if isinstance(raw_file, Mapping) else raw_file)
                    for raw_file in raw_files
                ]
            else:
                source["files"] = raw_files
            sources.append(source)
        projected["sources"] = sources
        return projected

    def _verified_local_manifest(
            self, draft_id: str) -> tuple[Dict[str, Any], bytes, Dict[str, Any]]:
        path = self.local_manifests / f"{draft_id}.json"
        if os.path.lexists(path):
            value = _strict_read_json(
                path, label="verified local-source manifest",
                maximum=_MAX_LOCAL_MANIFEST_BYTES)
            raw = _canonical(value)
            current = self.local_sources.verified_manifest(draft_id)
            if (self._local_manifest_identity(current)
                    != self._local_manifest_identity(value)):
                raise WebQuestNotReadyError(
                    "本机数据/参考目录在发布过程中发生变化；请重新创建任务")
        else:
            value = self.local_sources.verified_manifest(draft_id)
            if (not isinstance(value, dict) or value.get("version") != 1
                    or value.get("draft_id") != draft_id
                    or value.get("status") != "verified"
                    or not isinstance(value.get("sources"), list)):
                raise WebQuestServiceError("verified local-source manifest 身份非法")
            raw = _canonical(value)
            if len(raw) > _MAX_LOCAL_MANIFEST_BYTES:
                raise WebQuestNotReadyError("本机目录验证清单过大")
            _write_new(path, raw)
            _fsync_dir(self.local_manifests)
        sources = []
        for source in value.get("sources", []):
            if not isinstance(source, Mapping):
                raise WebQuestServiceError("verified local-source source 非法")
            sources.append({
                "source_id": source.get("source_id"),
                "label": source.get("label"),
                "kind": source.get("kind"),
                "file_count": source.get("file_count"),
                "total_bytes": source.get("total_bytes"),
                "status": "verified",
            })
        summary = {
            "source_count": len(sources),
            "file_count": value.get("file_count"),
            "total_bytes": value.get("total_bytes"),
            "manifest_sha256": _digest_bytes(raw),
            "sources": sources,
        }
        return value, raw, summary

    def _verify_quest_local_sources(self, quest: Quest) -> None:
        path = quest.work_root / "input" / "local-sources.json"
        if not os.path.lexists(path):
            return
        expected = _strict_read_json(
            path, label="quest local-source manifest",
            maximum=_MAX_LOCAL_MANIFEST_BYTES)
        draft_id = expected.get("draft_id")
        if not isinstance(draft_id, str) or _ID_RE.fullmatch(draft_id) is None:
            raise WebQuestServiceError("quest local-source manifest draft_id 非法")
        # The content hashes were already frozen during publication.  Resume
        # validates every path and stat identity, but intentionally does not
        # hash all source bytes again.
        current = self.local_sources.preflight_manifest(draft_id)
        if (self._local_manifest_metadata_identity(current)
                != self._local_manifest_metadata_identity(expected)):
            raise WebQuestNotReadyError(
                "本机数据/参考目录自任务发布后发生变化；请在 Web 新建任务以冻结新版本")

    def _resolve_profile(self, spec: Mapping[str, Any]):
        profile_id = spec.get("qualification_profile_id")
        template_id = spec.get("template_id")
        if template_id == _T1_TEMPLATE_ID and profile_id is None:
            matches = [
                profile for profile in self.profiles.list()
                if profile.template_id == template_id
            ]
            if len(matches) == 1:
                # The common single-machine deployment has exactly one
                # evaluator boundary.  Selecting it is internal plumbing, not
                # a data-contract form that the researcher must understand.
                return matches[0]
            if not matches:
                raise WebQuestNotReadyError(
                    "T1 本机安全数据准备服务/评测服务尚未准备完成；"
                    "请在 Web 部署检查页修复后重试。"
                    "系统不会要求用户手写数据合同，也不会把普通任务冒充密封评测")
            raise WebQuestNotReadyError(
                "当前有多个可用评测边界，请在 Web 向导选择一个评测模式")
        if profile_id is None:
            return None
        try:
            profile = self.profiles.get(profile_id)
        except KeyError as error:
            raise WebQuestNotReadyError("qualification profile 不存在或部署后已变化") from error
        if profile.template_id != template_id:
            raise WebQuestNotReadyError("qualification profile 与任务模板不匹配")
        return profile

    def _runtime_profile_for_spec(self, spec: Mapping[str, Any]) -> Dict[str, Any]:
        runtime_profile = self._normalize_runtime_profile(
            spec.get("runtime_profile", DEFAULT_PROFILE))
        is_qualification = (
            spec.get("template_id") == _T1_TEMPLATE_ID
            or spec.get("qualification_profile_id") is not None)
        if is_qualification and runtime_profile != DEFAULT_PROFILE:
            raise WebQuestNotReadyError(
                "qualification 任务只能使用默认 runtime profile")
        return runtime_profile

    @staticmethod
    def _runtime_settings(quest: Quest) -> QuestRuntimeSettings:
        return QuestRuntimeSettings(quest.work_root, quest.quest_id)

    def runtime_profile(self, quest_id: object) -> Dict[str, Any]:
        quest = self.registry.get(quest_id)
        try:
            return self._runtime_settings(quest).current()
        except RuntimeSettingsCorruptError as error:
            raise WebQuestServiceError(
                "quest runtime profile 账本损坏或暂不可读") from error

    def update_runtime_profile(
            self, quest_id: object, runtime_profile: object,
            idempotency_key: object) -> Dict[str, Any]:
        qid = _validate_quest_id(quest_id)
        key = _validate_key(idempotency_key)
        profile = self._normalize_runtime_profile(runtime_profile)
        quest = self.registry.get(qid)
        if quest.qualification_profile_id is not None:
            raise WebQuestConflictError(
                "qualification 任务禁止运行时修改 runtime profile")
        self.bind_operation(key, "/api/quest-runtime-profile", {
            "quest_id": qid, "runtime_profile": profile,
        })
        settings = self._runtime_settings(quest)
        runtime = self.processes.status(qid)
        schedule = getattr(
            self.processes, "schedule_runtime_profile_restart", None)
        try:
            # Publication holds this same Web lock from registry creation
            # through runtime-profile initialization.  A concurrent editor can
            # therefore observe only the pre-publication absence or the fully
            # initialized ledger, never insert revision 1 into that gap.
            with self._locked():
                operation = settings.runtime_update_operation(profile, key)
                if operation is None:
                    previous = settings.current()
                    would_change = previous["profile"] != profile
                    active = runtime.get("active") is True
                    if (would_change and active
                            and runtime.get("managed_by_web") is not True):
                        raise WebQuestConflictError(
                            "外部 owner 正在运行；无受管重启 authority，"
                            "拒绝修改 runtime profile")
                    if would_change and active and not callable(schedule):
                        raise WebQuestConflictError(
                            "受管 owner 缺 runtime profile "
                            "cycle-boundary 重启调度能力")
                    operation = settings.begin_runtime_update(profile, key)
        except WebQuestConflictError:
            raise
        except RuntimeProfileConflictError as error:
            raise WebQuestConflictError(str(error)) from error
        except RuntimeSettingsCorruptError as error:
            raise WebQuestServiceError(
                "quest runtime profile 账本损坏或暂不可写") from error

        current = operation["outcome"]
        if (not operation["changed"]
                or operation["status"] not in {"pending", "accepted"}):
            return {
                "runtime_profile": current,
                "runtime": runtime,
                "restart_pending": bool(
                    operation["status"] == "accepted"
                    and runtime.get("runtime_profile_restart_pending") is True),
                "apply_boundary": "cycle",
            }

        # Re-read after the durable ledger+receipt commit.  If a Web start won
        # the race it either captured this latest revision (no restart) or is
        # now a managed stale owner which can be scheduled below.
        try:
            runtime = self.processes.status(qid)
            latest = settings.current()
            active = runtime.get("active") is True
            bound = settings.bound_cycle_profile()
            bound_is_stale = (
                bound is not None
                and (bound["revision"], bound["record_sha256"])
                != (latest["revision"], latest["record_sha256"]))
            if (not active and operation["status"] == "pending"
                    and not bound_is_stale):
                settings.settle_runtime_update(key, "not-required")
                return {
                    "runtime_profile": current,
                    "runtime": runtime,
                    "restart_pending": False,
                    "apply_boundary": "cycle",
                }
            if (runtime.get("managed_by_web") is True
                    and runtime.get("applied_runtime_profile_revision")
                    == latest["revision"]
                    and runtime.get("runtime_profile_restart_pending") is not True):
                settings.settle_runtime_update(key, "applied")
                return {
                    "runtime_profile": current,
                    "runtime": runtime,
                    "restart_pending": False,
                    "apply_boundary": "cycle",
                }
            if active and runtime.get("managed_by_web") is not True:
                raise WebQuestRetryableError(
                    "runtime profile 已保存，但提交后发现外部 owner 正在运行；"
                    "无受管重启 authority，请停止外部 owner 后用同一 key 重试",
                    key)
            if active and not callable(schedule):
                raise WebQuestRetryableError(
                    "runtime profile 已保存，但受管 owner 缺 cycle-boundary "
                    "重启调度能力；可用同一 key 重试", key)
            if not callable(schedule):
                raise WebQuestRetryableError(
                    "runtime profile 已保存且需恢复未完成轮次，"
                    "但当前 manager 缺重启调度能力；可用同一 key 重试", key)
            accepted = settings.accept_runtime_update(key)
            if accepted["status"] == "terminated":
                return {
                    "runtime_profile": current,
                    "runtime": runtime,
                    "restart_pending": False,
                    "apply_boundary": "cycle",
                }
            assert callable(schedule)
            scheduled = schedule(qid, key)
            if not isinstance(scheduled, Mapping):
                raise WebQuestRetryableError(
                    "runtime profile 已保存，但 restart 调度响应非法；"
                    "可用同一 key 重试", key)
            runtime = dict(scheduled)
            schedule_outcome = runtime.get("runtime_profile_restart")
            if schedule_outcome == "scheduled":
                restart_pending = True
            elif schedule_outcome == "not_required":
                after_schedule = settings.runtime_update_operation_by_key(key)
                if (after_schedule is not None
                        and after_schedule["status"] in {
                            "applied", "not-required", "terminated"}):
                    restart_pending = False
                else:
                    settings.settle_runtime_update(key, "not-required")
                    restart_pending = False
            else:
                raise WebQuestRetryableError(
                    "runtime profile 已保存，但 restart 调度结果非法；"
                    "可用同一 key 重试", key)
        except (WebQuestConflictError, WebQuestRetryableError):
            raise
        except RuntimeSettingsCorruptError as error:
            raise WebQuestRetryableError(
                "quest runtime profile 已保存但 operation receipt 暂不可写；"
                "可用同一 key 重试", key) from error
        except Exception as error:
            raise WebQuestRetryableError(
                "runtime profile 已保存但 cycle-boundary 重启调度失败；"
                "可用同一 key 重试", key) from error
        return {
            "runtime_profile": current,
            "runtime": runtime,
            "restart_pending": restart_pending,
            "apply_boundary": "cycle",
        }

    def _ready_path(self, quest: Quest) -> Path:
        return quest.work_root / "state" / "web-setup-ready.json"

    def ready(self, quest_id: object) -> Dict[str, Any]:
        quest = self.registry.get(quest_id)
        runtime_profile = self.runtime_profile(quest.quest_id)
        path = self._ready_path(quest)
        if not os.path.lexists(path):
            return {
                "ready": False, "state": "setup_incomplete",
                "runtime_profile": runtime_profile,
            }
        value = _strict_read_json(path, label="quest Web setup receipt")
        if (set(value) != {
                "version", "quest_id", "corpus", "local_sources",
                "preflight", "qualification_profile_id"}
                or value.get("version") != _READY_VERSION
                or value.get("quest_id") != quest.quest_id):
            raise WebQuestServiceError("quest Web setup receipt 身份非法")
        return {
            "ready": True, "state": "ready", **value,
            "runtime_profile": runtime_profile,
        }

    def publish(self, draft_id: object, *, start: bool,
                idempotency_key: object) -> Dict[str, Any]:
        did = _validate_key(draft_id, label="draft_id")
        key = _validate_key(idempotency_key)
        if not isinstance(start, bool):
            raise ValueError("start 须为 bool")
        self.bind_operation(key, "/api/quest-drafts/publish", {
            "draft_id": did, "start": start,
        })
        journal_path = self.finalize_journals / f"{did}.json"

        with self._locked():
            journal = None
            if os.path.lexists(journal_path):
                journal = _strict_read_json(journal_path, label="quest finalize journal")
                journal_version = journal.get("version")
                journal_common = {
                    "version", "draft_id", "quest_id", "corpus",
                    "local_sources", "preflight",
                    "qualification_profile_id",
                }
                journal_expected = (
                    journal_common
                    if journal_version == _LEGACY_JOURNAL_VERSION
                    else journal_common | {"runtime_profile"})
                if (journal_version not in {
                        _LEGACY_JOURNAL_VERSION, _JOURNAL_VERSION}
                        or set(journal) != journal_expected
                        or journal.get("draft_id") != did):
                    raise WebQuestServiceError("quest finalize journal 身份非法")
                if journal_version == _LEGACY_JOURNAL_VERSION:
                    journal["runtime_profile"] = dict(DEFAULT_PROFILE)
                else:
                    try:
                        normalized_runtime = normalize_profile(
                            journal.get("runtime_profile"))
                    except ValueError as error:
                        raise WebQuestServiceError(
                            "quest finalize journal runtime_profile 非法") from error
                    if normalized_runtime != journal["runtime_profile"]:
                        raise WebQuestServiceError(
                            "quest finalize journal runtime_profile 非规范")

            if journal is None:
                spec = self.drafts.spec(did)
                _value, corpus_raw, corpus_summary = self._corpus_manifest(did)
                preflight = self.preflight(did)
                _local_value, local_raw, local_summary = (
                    self._verified_local_manifest(did))
                profile = self._resolve_profile(spec)
                runtime_profile = self._runtime_profile_for_spec(spec)
                journal = {
                    "version": _JOURNAL_VERSION,
                    "draft_id": did,
                    "quest_id": spec["quest_id"],
                    "corpus": corpus_summary,
                    "local_sources": local_summary,
                    "preflight": preflight,
                    "qualification_profile_id": (
                        None if profile is None else profile.profile_id),
                    "runtime_profile": runtime_profile,
                }
                _write_new(journal_path, _canonical(journal))
                _fsync_dir(self.finalize_journals)
            else:
                spec = None
                corpus_raw = None
                _local_value, local_raw, local_summary = (
                    self._verified_local_manifest(did))
                if local_summary != journal["local_sources"]:
                    raise WebQuestConflictError(
                        "local-source manifest 与 finalize journal 身份漂移")
                try:
                    spec = self.drafts.spec(did)
                    _value, corpus_raw, summary = self._corpus_manifest(did)
                    if (summary != journal["corpus"]
                            or spec["quest_id"] != journal["quest_id"]
                            or self._runtime_profile_for_spec(spec)
                            != journal["runtime_profile"]):
                        raise WebQuestConflictError("draft 与 finalize journal 身份漂移")
                except (KeyError, DraftCorruptError):
                    # After the verified files tree is moved, the draft is
                    # intentionally no longer a valid upload object.  Recovery
                    # continues only from the already-published quest below.
                    spec = None

            try:
                quest = self.registry.get(journal["quest_id"])
            except KeyError:
                if spec is None:
                    raise WebQuestServiceError("finalize journal 缺可恢复 draft/quest")
                profile = self._resolve_profile(spec)
                kwargs = {
                    "quest_id": spec["quest_id"], "title": spec["title"],
                    "qualification_profile_id": (
                        None if profile is None else profile.profile_id),
                    "qualification_contract": (
                        None if profile is None else profile.contract()),
                }
                if "template_id" in spec:
                    quest = self.registry.create_from_template(
                        template_id=spec["template_id"], **kwargs)
                else:
                    quest = self.registry.create(
                        goal_brief_md=spec["goal_brief_md"], **kwargs)

            if (quest.qualification_profile_id is not None
                    and journal["runtime_profile"] != DEFAULT_PROFILE):
                raise WebQuestNotReadyError(
                    "qualification 任务只能使用默认 runtime profile")
            try:
                runtime_settings = self._runtime_settings(quest)
                runtime_settings.initialize(
                    journal["runtime_profile"], key)
                runtime_profile = runtime_settings.current()
            except RuntimeProfileConflictError as error:
                raise WebQuestConflictError(str(error)) from error
            except RuntimeSettingsCorruptError as error:
                raise WebQuestServiceError(
                    "quest runtime profile 账本损坏或暂不可写") from error

            ready_path = self._ready_path(quest)
            if not os.path.lexists(ready_path):
                input_root = quest.work_root / "input"
                if os.path.lexists(input_root):
                    _directory(input_root, label="quest input", mode=0o700)
                else:
                    input_root.mkdir(mode=0o700)
                    _fsync_dir(quest.work_root)
                manifest_path = input_root / "corpus-manifest.json"
                local_manifest_path = input_root / "local-sources.json"
                corpus_root = input_root / "corpus"
                if not os.path.lexists(manifest_path):
                    if corpus_raw is None:
                        raise WebQuestServiceError("corpus manifest recovery bytes 缺失")
                    _write_new(manifest_path, corpus_raw, mode=0o400)
                    _fsync_dir(input_root)
                stored_manifest = _strict_read_json(
                    manifest_path, label="quest corpus manifest")
                stored_raw = _canonical(stored_manifest)
                if _digest_bytes(stored_raw) != journal["corpus"]["manifest_sha256"]:
                    raise WebQuestServiceError("quest corpus manifest 与 journal 不一致")
                if not os.path.lexists(local_manifest_path):
                    _write_new(local_manifest_path, local_raw, mode=0o400)
                    _fsync_dir(input_root)
                stored_local = _strict_read_json(
                    local_manifest_path, label="quest local-source manifest",
                    maximum=_MAX_LOCAL_MANIFEST_BYTES)
                if _digest_bytes(_canonical(stored_local)) != journal["local_sources"]["manifest_sha256"]:
                    raise WebQuestServiceError(
                        "quest local-source manifest 与 journal 不一致")
                if not os.path.lexists(corpus_root):
                    source = self.drafts.files_root(did)
                    if os.stat(source).st_dev != os.stat(input_root).st_dev:
                        raise WebQuestServiceError("draft 与 quest 不在同一文件系统，拒绝大文件复制回退")
                    os.rename(source, corpus_root)
                    _fsync_dir(input_root)
                _freeze_tree(corpus_root)
                ready_value = {
                    "version": _READY_VERSION,
                    "quest_id": quest.quest_id,
                    "corpus": journal["corpus"],
                    "local_sources": journal["local_sources"],
                    "preflight": journal["preflight"],
                    "qualification_profile_id": journal["qualification_profile_id"],
                }
                _write_new(ready_path, _canonical(ready_value), mode=0o400)
                _fsync_dir(quest.work_root / "state")

            # Once ready is durable, the remaining draft metadata is no
            # longer authoritative and can be removed.  Never follow links.
            draft_root = self.drafts.drafts_dir / did
            if os.path.lexists(draft_root):
                info = os.lstat(draft_root)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise WebQuestServiceError("completed draft root 身份非法")
                shutil.rmtree(draft_root)
                _fsync_dir(self.drafts.drafts_dir)

        runtime = self.processes.start(quest.quest_id, key) if start else self.processes.status(
            quest.quest_id)
        quest_public = quest.public_dict()
        quest_public["runtime_profile"] = runtime_profile
        return {
            "quest": quest_public,
            "setup": self.ready(quest.quest_id),
            "runtime": runtime,
        }

    def start(self, quest_id: object, key: object) -> Dict[str, Any]:
        qid = _validate_quest_id(quest_id)
        safe_key = _validate_key(key)
        self.bind_operation(safe_key, "/api/quest-control", {
            "quest_id": qid, "action": "start",
        })
        status = self.ready(qid)
        if status.get("ready") is not True:
            raise WebQuestNotReadyError("quest 尚未完成 Web 数据发布/预检")
        quest = self.registry.get(qid)
        self._verify_quest_local_sources(quest)
        return self.processes.start(qid, safe_key)

    def terminate(self, quest_id: object, key: object) -> Dict[str, Any]:
        qid = _validate_quest_id(quest_id)
        safe_key = _validate_key(key)
        self.bind_operation(safe_key, "/api/quest-control", {
            "quest_id": qid, "action": "terminate",
        })
        return self.processes.terminate(qid, safe_key)

    def runtime(self, quest_id: object) -> Dict[str, Any]:
        return self.processes.status(_validate_quest_id(quest_id))

    def runtime_log(self, quest_id: object) -> Dict[str, Any]:
        return self.processes.log_tail(_validate_quest_id(quest_id))

    # --------------------------------------------------- runtime file requests
    def _request_registry(self, quest_id: object, request_id: object) -> tuple[Quest, int, QuestDraftRegistry]:
        qid = _validate_quest_id(quest_id)
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
            raise ValueError("request_id 须为正整数")
        quest = self.registry.get(qid)
        root = self.request_roots / qid / f"r{request_id}"
        root.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root.parent, 0o700)
        return quest, request_id, QuestDraftRegistry(root)

    def create_request_upload(self, quest_id: object, request_id: object,
                              key: object) -> Dict[str, Any]:
        quest, rid, registry = self._request_registry(quest_id, request_id)
        safe_key = _validate_key(key)
        self.bind_operation(safe_key, "/api/file-request-uploads", {
            "quest_id": quest.quest_id, "request_id": rid,
        })
        draft = registry.create({
            "quest_id": quest.quest_id,
            "title": f"request-r{rid}",
            "goal_brief_md": "internal Web file-request upload",
        }, safe_key)
        return {"upload_id": draft["draft_id"], "request_id": rid}

    def _request_upload(self, quest_id: object, request_id: object,
                        upload_id: object) -> tuple[Quest, int, QuestDraftRegistry, str]:
        quest, rid, registry = self._request_registry(quest_id, request_id)
        uid = _validate_key(upload_id, label="upload_id")
        registry.get(uid)
        return quest, rid, registry, uid

    @staticmethod
    def request_storage_path(path: object) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("upload path 非法")
        parts = PurePosixPath(path).parts
        if parts and parts[0].isdigit() and int(parts[0]) > 0:
            return path
        return "1/" + path

    def request_begin_file(self, quest_id: object, request_id: object,
                           upload_id: object, path: object, size: object) -> Dict[str, Any]:
        _quest, _rid, registry, uid = self._request_upload(
            quest_id, request_id, upload_id)
        return registry.begin_file(uid, self.request_storage_path(path), size)

    def request_append_chunk(self, quest_id: object, request_id: object,
                             upload_id: object, path: object, offset: object,
                             data: bytes, sha256: object) -> Dict[str, Any]:
        _quest, _rid, registry, uid = self._request_upload(
            quest_id, request_id, upload_id)
        return registry.append_chunk(
            uid, self.request_storage_path(path), offset, data, sha256)

    def request_finalize_file(self, quest_id: object, request_id: object,
                              upload_id: object, path: object,
                              sha256: object) -> Dict[str, Any]:
        _quest, _rid, registry, uid = self._request_upload(
            quest_id, request_id, upload_id)
        return registry.finalize_file(
            uid, self.request_storage_path(path), sha256)

    def publish_request_upload(
            self, quest_id: object, request_id: object, upload_id: object,
            key: object) -> RequestUploadPublication:
        qid = _validate_quest_id(quest_id)
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
            raise ValueError("request_id 须为正整数")
        rid = request_id
        uid = _validate_key(upload_id, label="upload_id")
        quest = self.registry.get(qid)
        safe_key = _validate_key(key)
        self.bind_operation(safe_key, "/api/file-request-uploads/publish", {
            "quest_id": quest.quest_id, "request_id": rid, "upload_id": uid,
        })
        target_name = f"web-r{rid}-{uid}"
        receipt_path = self.request_publications / f"{quest.quest_id}-r{rid}-{uid}.json"
        with self._locked():
            if os.path.lexists(receipt_path):
                receipt = _strict_read_json(
                    receipt_path, label="request publication receipt")
                if (set(receipt) != {
                        "version", "quest_id", "request_id", "upload_id",
                        "target_name", "files"}
                        or receipt.get("version") != _REQUEST_PUBLICATION_VERSION
                        or receipt.get("quest_id") != quest.quest_id
                        or receipt.get("request_id") != rid
                        or receipt.get("upload_id") != uid
                        or receipt.get("target_name") != target_name):
                    raise WebQuestServiceError(
                        "request publication receipt 身份非法")
            else:
                _quest, _rid, registry, _uid = self._request_upload(
                    quest.quest_id, rid, uid)
                rows = registry.files_manifest(uid)  # full hash at publication
                files = []
                for row in rows:
                    if (row.get("status") != "complete"
                            or not isinstance(row.get("sha256"), str)):
                        raise WebQuestNotReadyError("request upload 尚有未完成文件")
                    files.append({
                        "path": row["path"], "size": row["size"],
                        "sha256": row["sha256"],
                    })
                receipt = {
                    "version": _REQUEST_PUBLICATION_VERSION,
                    "quest_id": quest.quest_id,
                    "request_id": rid,
                    "upload_id": uid,
                    "target_name": target_name,
                    "files": files,
                }
                raw = _canonical(receipt)
                if len(raw) > _MAX_MANIFEST_BYTES:
                    raise WebQuestServiceError("request publication receipt 过大")
                _write_new(receipt_path, raw)
                _fsync_dir(self.request_publications)

            uploads = quest.work_root / "uploads"
            if os.path.lexists(uploads):
                _directory(uploads, label="quest uploads", mode=0o700)
            else:
                uploads.mkdir(mode=0o700)
                _fsync_dir(quest.work_root)
            target = uploads / target_name
            if not os.path.lexists(target):
                _quest, _rid, registry, _uid = self._request_upload(
                    quest.quest_id, rid, uid)
                current_rows = registry.files_manifest(uid)
                current_files = [
                    {"path": row["path"], "size": row["size"],
                     "sha256": row["sha256"]}
                    for row in current_rows if row.get("status") == "complete"
                ]
                if current_files != receipt["files"] or len(current_rows) != len(current_files):
                    raise WebQuestConflictError(
                        "request upload 与 publication receipt 身份漂移")
                source = registry.files_root(uid)
                if os.stat(source).st_dev != os.stat(uploads).st_dev:
                    raise WebQuestServiceError("request upload 与 quest 不在同一文件系统")
                os.rename(source, target)
                _fsync_dir(uploads)
            _verify_tree_manifest(target, receipt["files"])
            _freeze_tree(target)
        return RequestUploadPublication(
            quest_id=quest.quest_id, request_id=rid, upload_id=uid,
            source_ref=f"work/uploads/{target_name}")

    def close(self) -> None:
        with self._publish_guard:
            self._closed = True
        self.processes.close()


__all__ = [
    "RequestUploadPublication", "WebQuestConflictError",
    "WebQuestNotReadyError", "WebQuestService", "WebQuestServiceError",
]
