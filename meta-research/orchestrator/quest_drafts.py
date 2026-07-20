"""Durable Web-first quest drafts and bounded chunk uploads.

This module is deliberately transport-free.  HTTP may later project these
operations, but all path resolution, idempotency, quotas and publication live
behind :class:`QuestDraftRegistry`.  A draft is not a quest and this component
never seals or publishes one.
"""
from __future__ import annotations

import builtins
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .quest_runtime_profiles import DEFAULT_PROFILE, normalize_profile


_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_RECEIPT_RE = re.compile(r"^[0-9]{20}\.json$")

_DRAFT_VERSION = 1
_CREATE_RECEIPT_VERSION = 1
_UPLOAD_VERSION = 1
_FILE_RECEIPT_VERSION = 1
_MAX_TITLE_CHARS = 200
_MAX_BRIEF_BYTES = 256 * 1024
_MAX_JSON_BYTES = 512 * 1024
_MAX_PATH_BYTES = 1024
_MAX_PATH_DEPTH = 64
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 64 * 1024 ** 3
_MAX_TOTAL_BYTES = 256 * 1024 ** 3
_MAX_CHUNK_BYTES = 8 * 1024 ** 2


class DraftConflictError(ValueError):
    """An idempotency key, upload path or offset has a different binding."""


class DraftCorruptError(RuntimeError):
    """Draft storage can no longer prove its closed filesystem identity."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValueError("draft value 无法 canonicalize") from error


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 须为 32 位小写 hex")
    return value


def _validate_slug(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 须匹配 {_SLUG_RE.pattern}")
    return value


def _validate_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 须为 sha256:<64 lowercase hex>")
    return value


def _validate_relative_path(value: object) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or "\x00" in value):
        raise ValueError("upload path 须为非空 POSIX 相对路径")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("upload path 必须是有效 UTF-8") from error
    if len(encoded) > _MAX_PATH_BYTES:
        raise ValueError(f"upload path 超过 {_MAX_PATH_BYTES} UTF-8 bytes")
    path = PurePosixPath(value)
    parts = path.parts
    if (path.is_absolute() or path.as_posix() != value or not parts
            or len(parts) > _MAX_PATH_DEPTH
            or any(part in {"", ".", ".."} or part.startswith(".")
                   for part in parts)):
        raise ValueError("upload path 非规范/越界/含隐藏组件")
    return value


def _normalize_spec(
        spec: object, *, apply_runtime_default: bool = False) -> Dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError("draft spec 须为 object")
    allowed = {
        "quest_id", "title", "template_id", "goal_brief_md",
        "qualification_profile_id", "runtime_profile",
    }
    if set(spec) - allowed or not {"quest_id", "title"} <= set(spec):
        raise ValueError("draft spec 字段闭包非法")
    quest_id = _validate_slug(spec.get("quest_id"), label="quest_id")
    title = spec.get("title")
    if (not isinstance(title, str) or title != title.strip()
            or not 1 <= len(title) <= _MAX_TITLE_CHARS
            or any((ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F
                   for char in title)):
        raise ValueError(
            f"title 须为 1..{_MAX_TITLE_CHARS} 字符、无首尾空白/控制字符")
    has_template = "template_id" in spec
    has_custom = "goal_brief_md" in spec
    if has_template == has_custom:
        raise ValueError("template_id 与 goal_brief_md 必须且只能提供一个")
    normalized: Dict[str, Any] = {"quest_id": quest_id, "title": title}
    if has_template:
        normalized["template_id"] = _validate_slug(
            spec.get("template_id"), label="template_id")
    else:
        brief = spec.get("goal_brief_md")
        if not isinstance(brief, str):
            raise ValueError("goal_brief_md 须为字符串")
        try:
            brief_bytes = brief.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("goal_brief_md 必须是有效 UTF-8") from error
        if not 1 <= len(brief_bytes) <= _MAX_BRIEF_BYTES:
            raise ValueError(
                f"goal_brief_md 须为 1..{_MAX_BRIEF_BYTES} UTF-8 bytes")
        normalized["goal_brief_md"] = brief
    if "qualification_profile_id" in spec:
        normalized["qualification_profile_id"] = _validate_slug(
            spec.get("qualification_profile_id"),
            label="qualification_profile_id")
    if "runtime_profile" in spec:
        normalized["runtime_profile"] = normalize_profile(
            spec.get("runtime_profile"))
    elif apply_runtime_default:
        normalized["runtime_profile"] = copy.deepcopy(DEFAULT_PROFILE)
    if len(_canonical(normalized)) > _MAX_JSON_BYTES:
        raise ValueError("draft spec 超过大小上限")
    return normalized


def _directory_info(path: Path, *, owner: int, label: str,
                    exact_mode: Optional[int] = 0o700) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise DraftCorruptError(f"{label} 不可读") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != owner
            or (exact_mode is not None
                and stat.S_IMODE(info.st_mode) != exact_mode)):
        raise DraftCorruptError(f"{label} owner/type/mode 非法")
    return info


def _fsync_dir(path: Path) -> None:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def _open_regular(path: Path, *, owner: int, label: str,
                  flags: int = os.O_RDONLY, mode: int = 0o600) -> Tuple[int, os.stat_result]:
    open_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, open_flags)
    except OSError as error:
        raise DraftCorruptError(f"{label} 不可安全打开") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode):
            raise DraftCorruptError(f"{label} owner/type/link/mode 非法")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _read_canonical_json(path: Path, *, owner: int, label: str,
                         maximum: int = _MAX_JSON_BYTES) -> Dict[str, Any]:
    fd, before = _open_regular(path, owner=owner, label=label)
    try:
        if not 2 <= before.st_size <= maximum:
            raise DraftCorruptError(f"{label} 大小非法")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise DraftCorruptError(f"{label} 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns, after.st_uid, after.st_mode, after.st_nlink)
                != (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns, before.st_uid,
                    before.st_mode, before.st_nlink)):
            raise DraftCorruptError(f"{label} 读取期间身份漂移")
    finally:
        os.close(fd)

    def unique(pairs):  # noqa: ANN001 - json hook protocol
        result = {}
        for key, value in pairs:
            if key in result:
                raise DraftCorruptError(f"{label} 含重复 key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                DraftCorruptError(f"{label} 含非有限数: {token}")))
    except DraftCorruptError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DraftCorruptError(f"{label} 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise DraftCorruptError(f"{label} 顶层须为 object")
    try:
        canonical = _canonical(value)
    except ValueError as error:
        raise DraftCorruptError(f"{label} 无法 canonicalize") from error
    if raw != canonical:
        raise DraftCorruptError(f"{label} 非 canonical JSON")
    return value


def _hash_fd(fd: int, *, expected_size: int, label: str) -> str:
    before = os.fstat(fd)
    if before.st_size != expected_size:
        raise DraftCorruptError(f"{label} size 与声明不一致")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    after = os.fstat(fd)
    if (size != expected_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, after.st_uid, after.st_mode, after.st_nlink)
            != (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns, before.st_uid,
                before.st_mode, before.st_nlink)):
        raise DraftCorruptError(f"{label} 读取期间身份/内容漂移")
    return "sha256:" + digest.hexdigest()


class QuestDraftRegistry:
    """Filesystem-backed draft registry with one global advisory mutex."""

    def __init__(self, root: Path | str):
        supplied = Path(os.path.abspath(os.fspath(root)))
        if not os.path.lexists(supplied):
            try:
                supplied.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                # A concurrent constructor won creation; validate its result.
                pass
        try:
            info = os.lstat(supplied)
        except OSError as error:
            raise ValueError("draft root 不可读") from error
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or os.path.realpath(supplied) != str(supplied)
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ValueError("draft root 须为当前 UID 拥有的非可写规范目录")
        self.root = supplied
        self.owner = os.geteuid()
        self.drafts_dir = self.root / "drafts"
        self.requests_dir = self.root / "draft-create-requests"
        for path in (self.drafts_dir, self.requests_dir):
            if not os.path.lexists(path):
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            _directory_info(path, owner=self.owner, label=path.name)
            _fsync_dir(self.root)
        self.lock_path = self.root / ".quest-drafts.lock"
        if not os.path.lexists(self.lock_path):
            try:
                _write_new(self.lock_path, b"quest-drafts-v1\n")
            except FileExistsError:
                pass
        _fsync_dir(self.root)
        fd, _info = _open_regular(
            self.lock_path, owner=self.owner, label="draft registry lock")
        os.close(fd)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd, opened = _open_regular(
            self.lock_path, owner=self.owner, label="draft registry lock",
            flags=os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.lstat(self.lock_path)
            if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or current.st_nlink != 1):
                raise DraftCorruptError("draft registry lock 路径绑定漂移")
            self._validate_registry_locked()
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _validate_registry_locked(self) -> None:
        _directory_info(self.drafts_dir, owner=self.owner, label="drafts")
        _directory_info(
            self.requests_dir, owner=self.owner,
            label="draft-create-requests")
        for entry in self.drafts_dir.iterdir():
            if _ID_RE.fullmatch(entry.name) is None:
                raise DraftCorruptError(f"drafts/ 含未知条目: {entry.name}")
            _directory_info(
                entry, owner=self.owner, label=f"draft {entry.name}")
        for entry in self.requests_dir.iterdir():
            if (_ID_RE.fullmatch(entry.stem) is None or entry.suffix != ".json"):
                raise DraftCorruptError(
                    f"draft-create-requests/ 含未知条目: {entry.name}")
            fd, _info = _open_regular(
                entry, owner=self.owner, label=f"create receipt {entry.name}")
            os.close(fd)

    def _draft_path(self, draft_id: object) -> Path:
        return self.drafts_dir / _validate_id(draft_id, label="draft_id")

    @staticmethod
    def _file_token(relative: str) -> str:
        return hashlib.sha256(relative.encode("utf-8")).hexdigest()

    def _create_draft_locked(self, *, draft_id: str, spec: Mapping[str, Any],
                             created_at: str) -> None:
        final = self.drafts_dir / draft_id
        if os.path.lexists(final):
            record = self._draft_record_locked(draft_id)
            if record["spec"] != dict(spec) or record["created_at"] != created_at:
                raise DraftCorruptError("idempotency receipt 与已有 draft 错配")
            return
        stage = self.drafts_dir / f".creating-{draft_id}-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        try:
            for name in ("incoming", "files", "receipts"):
                (stage / name).mkdir(mode=0o700)
            record = {
                "version": _DRAFT_VERSION,
                "draft_id": draft_id,
                "created_at": created_at,
                "spec": dict(spec),
            }
            _write_new(stage / "draft.json", _canonical(record))
            for name in ("incoming", "files", "receipts"):
                _fsync_dir(stage / name)
            _fsync_dir(stage)
            os.rename(stage, final)
            _fsync_dir(self.drafts_dir)
        except BaseException:
            if os.path.lexists(stage):
                shutil.rmtree(stage)
            raise

    def _draft_record_locked(self, draft_id: object) -> Dict[str, Any]:
        did = _validate_id(draft_id, label="draft_id")
        root = self.drafts_dir / did
        try:
            _directory_info(root, owner=self.owner, label=f"draft {did}")
        except DraftCorruptError as error:
            if not os.path.lexists(root):
                raise KeyError(f"draft 不存在: {did}") from error
            raise
        entries = {entry.name for entry in root.iterdir()}
        if entries != {"draft.json", "incoming", "files", "receipts"}:
            raise DraftCorruptError(f"draft {did} 条目闭包非法")
        for name in ("incoming", "files", "receipts"):
            _directory_info(
                root / name, owner=self.owner,
                label=f"draft {did}/{name}")
        value = _read_canonical_json(
            root / "draft.json", owner=self.owner,
            label=f"draft {did}/draft.json")
        if (set(value) != {"version", "draft_id", "created_at", "spec"}
                or value.get("version") != _DRAFT_VERSION
                or value.get("draft_id") != did
                or not isinstance(value.get("created_at"), str)
                or not value["created_at"]):
            raise DraftCorruptError(f"draft {did} manifest 字段/身份非法")
        try:
            spec = _normalize_spec(value.get("spec"))
        except ValueError as error:
            raise DraftCorruptError(f"draft {did} spec 损坏") from error
        if spec != value["spec"]:
            raise DraftCorruptError(f"draft {did} spec 非规范")
        return value

    def _read_create_receipt_locked(
            self, key: str) -> Optional[Dict[str, Any]]:
        path = self.requests_dir / f"{key}.json"
        if not os.path.lexists(path):
            return None
        value = _read_canonical_json(
            path, owner=self.owner, label=f"create receipt {key}")
        if (set(value) != {
                "version", "idempotency_key", "spec_sha256", "spec",
                "draft_id", "created_at"}
                or value.get("version") != _CREATE_RECEIPT_VERSION
                or value.get("idempotency_key") != key
                or not isinstance(value.get("spec_sha256"), str)
                or _SHA256_RE.fullmatch(value["spec_sha256"]) is None
                or not isinstance(value.get("draft_id"), str)
                or _ID_RE.fullmatch(value["draft_id"]) is None
                or not isinstance(value.get("created_at"), str)
                or not value["created_at"]):
            raise DraftCorruptError("draft create receipt 字段/身份非法")
        try:
            spec = _normalize_spec(value.get("spec"))
        except ValueError as error:
            raise DraftCorruptError("draft create receipt spec 损坏") from error
        if (spec != value["spec"]
                or _sha256(_canonical(spec)) != value["spec_sha256"]):
            raise DraftCorruptError("draft create receipt spec hash 漂移")
        return value

    def create(self, spec: object, idempotency_key: object) -> Dict[str, Any]:
        # New drafts always freeze an explicit runtime profile.  For replay of a
        # durable pre-profile receipt, however, preserve the legacy missing
        # field exactly so restart recovery does not rewrite operation identity.
        legacy_normalized = _normalize_spec(spec)
        normalized = _normalize_spec(spec, apply_runtime_default=True)
        key = _validate_id(idempotency_key, label="idempotency_key")
        with self._locked():
            receipt = self._read_create_receipt_locked(key)
            if receipt is not None:
                if "runtime_profile" not in receipt["spec"]:
                    requested = dict(legacy_normalized)
                    explicit_runtime = requested.pop("runtime_profile", None)
                    if (explicit_runtime is not None
                            and explicit_runtime != DEFAULT_PROFILE):
                        raise DraftConflictError(
                            f"idempotency_key {key} 的 legacy draft 只兼容默认 runtime profile")
                else:
                    requested = normalized
                requested_hash = _sha256(_canonical(requested))
                if (receipt["spec_sha256"] != requested_hash
                        or receipt["spec"] != requested):
                    raise DraftConflictError(
                        f"idempotency_key {key} 已绑定不同 draft spec")
                self._create_draft_locked(
                    draft_id=receipt["draft_id"], spec=requested,
                    created_at=receipt["created_at"])
                return self._summary_locked(receipt["draft_id"])

            spec_hash = _sha256(_canonical(normalized))
            draft_id = uuid.uuid4().hex
            while os.path.lexists(self.drafts_dir / draft_id):
                draft_id = uuid.uuid4().hex
            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            receipt = {
                "version": _CREATE_RECEIPT_VERSION,
                "idempotency_key": key,
                "spec_sha256": spec_hash,
                "spec": normalized,
                "draft_id": draft_id,
                "created_at": created_at,
            }
            _write_new(
                self.requests_dir / f"{key}.json", _canonical(receipt))
            _fsync_dir(self.requests_dir)
            self._create_draft_locked(
                draft_id=draft_id, spec=normalized, created_at=created_at)
            return self._summary_locked(draft_id)

    def _upload_meta_locked(
            self, draft_root: Path, relative: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
        token = self._file_token(relative)
        upload_root = draft_root / "incoming" / token
        if not os.path.lexists(upload_root):
            return None
        _directory_info(
            upload_root, owner=self.owner,
            label=f"incoming upload {relative}")
        entries = {entry.name for entry in upload_root.iterdir()}
        if not entries <= {"meta.json", "data.part", "chunks"} or not {
                "meta.json", "chunks"} <= entries:
            raise DraftCorruptError(f"incoming upload {relative} 条目闭包非法")
        _directory_info(
            upload_root / "chunks", owner=self.owner,
            label=f"incoming upload {relative}/chunks")
        meta = _read_canonical_json(
            upload_root / "meta.json", owner=self.owner,
            label=f"incoming upload {relative}/meta")
        if (set(meta) != {"version", "path", "size"}
                or meta.get("version") != _UPLOAD_VERSION
                or meta.get("path") != relative
                or self._file_token(str(meta.get("path"))) != token
                or isinstance(meta.get("size"), bool)
                or not isinstance(meta.get("size"), int)
                or not 0 <= meta["size"] <= _MAX_FILE_BYTES):
            raise DraftCorruptError(f"incoming upload {relative} meta 非法")
        return upload_root, meta

    def _file_receipt_locked(
            self, draft_root: Path, relative: str) -> Optional[Dict[str, Any]]:
        token = self._file_token(relative)
        path = draft_root / "receipts" / f"{token}.json"
        if not os.path.lexists(path):
            return None
        value = _read_canonical_json(
            path, owner=self.owner, label=f"file receipt {relative}")
        if (set(value) != {"version", "path", "size", "sha256", "status"}
                or value.get("version") != _FILE_RECEIPT_VERSION
                or value.get("path") != relative
                or self._file_token(str(value.get("path"))) != token
                or isinstance(value.get("size"), bool)
                or not isinstance(value.get("size"), int)
                or not 0 <= value["size"] <= _MAX_FILE_BYTES
                or not isinstance(value.get("sha256"), str)
                or _SHA256_RE.fullmatch(value["sha256"]) is None
                or value.get("status") != "complete"):
            raise DraftCorruptError(f"file receipt {relative} 非法")
        return value

    def _chunk_receipts_locked(
            self, upload_root: Path, *, relative: str,
            declared_size: int) -> Tuple[List[Dict[str, Any]], int]:
        chunks_root = upload_root / "chunks"
        rows = []
        committed = 0
        for entry in sorted(chunks_root.iterdir(), key=lambda item: item.name):
            if _CHUNK_RECEIPT_RE.fullmatch(entry.name) is None:
                raise DraftCorruptError(
                    f"upload {relative} chunks 含未知条目: {entry.name}")
            row = _read_canonical_json(
                entry, owner=self.owner,
                label=f"upload {relative} chunk {entry.name}")
            if (set(row) != {
                    "version", "path", "offset", "bytes", "sha256"}
                    or row.get("version") != _UPLOAD_VERSION
                    or row.get("path") != relative
                    or isinstance(row.get("offset"), bool)
                    or not isinstance(row.get("offset"), int)
                    or row["offset"] != committed
                    or entry.name != f"{row['offset']:020d}.json"
                    or isinstance(row.get("bytes"), bool)
                    or not isinstance(row.get("bytes"), int)
                    or not 1 <= row["bytes"] <= _MAX_CHUNK_BYTES
                    or not isinstance(row.get("sha256"), str)
                    or _SHA256_RE.fullmatch(row["sha256"]) is None
                    or committed + row["bytes"] > declared_size):
                raise DraftCorruptError(f"upload {relative} chunk ledger 非法")
            committed += row["bytes"]
            rows.append(row)
        return rows, committed

    def _open_upload_data_locked(
            self, upload_root: Path, *, relative: str,
            declared_size: int, recover_tail: bool) -> Tuple[int, int]:
        fd, info = _open_regular(
            upload_root / "data.part", owner=self.owner,
            label=f"upload {relative} data", flags=os.O_RDWR)
        try:
            _rows, committed = self._chunk_receipts_locked(
                upload_root, relative=relative,
                declared_size=declared_size)
            if info.st_size < committed:
                raise DraftCorruptError(f"upload {relative} data 短于 chunk ledger")
            if info.st_size > committed:
                if not recover_tail:
                    raise DraftCorruptError(
                        f"upload {relative} 存在未提交 chunk tail")
                os.ftruncate(fd, committed)
                os.fsync(fd)
            return fd, committed
        except BaseException:
            os.close(fd)
            raise

    def _scan_uploads_locked(
            self, draft_root: Path, *, verify_content: bool) -> Dict[str, Dict[str, Any]]:
        incoming = draft_root / "incoming"
        receipts_root = draft_root / "receipts"
        uploads: Dict[str, Dict[str, Any]] = {}
        for entry in sorted(incoming.iterdir(), key=lambda item: item.name):
            if _TOKEN_RE.fullmatch(entry.name) is None:
                raise DraftCorruptError(f"incoming/ 含未知条目: {entry.name}")
            _directory_info(entry, owner=self.owner, label=f"incoming/{entry.name}")
            meta = _read_canonical_json(
                entry / "meta.json", owner=self.owner,
                label=f"incoming/{entry.name}/meta")
            relative = meta.get("path")
            try:
                normalized = _validate_relative_path(relative)
            except ValueError as error:
                raise DraftCorruptError("incoming meta path 非法") from error
            if self._file_token(normalized) != entry.name or normalized in uploads:
                raise DraftCorruptError("incoming token/path 冲突")
            loaded = self._upload_meta_locked(draft_root, normalized)
            if loaded is None:
                raise DraftCorruptError("incoming meta 消失")
            upload_root, meta = loaded
            receipt = self._file_receipt_locked(draft_root, normalized)
            has_data = os.path.lexists(upload_root / "data.part")
            if receipt is None and not has_data:
                raise DraftCorruptError(f"upload {normalized} 缺 data.part")
            if receipt is not None and has_data:
                raise DraftCorruptError(f"complete upload {normalized} 仍含 data.part")
            if receipt is not None and receipt["size"] != meta["size"]:
                raise DraftCorruptError(f"file receipt {normalized} size 错配")
            if receipt is None:
                fd, _committed = self._open_upload_data_locked(
                    upload_root, relative=normalized,
                    declared_size=meta["size"], recover_tail=False)
                os.close(fd)
            uploads[normalized] = {"meta": meta, "receipt": receipt}
        for entry in sorted(receipts_root.iterdir(), key=lambda item: item.name):
            if entry.suffix != ".json" or _TOKEN_RE.fullmatch(entry.stem) is None:
                raise DraftCorruptError(f"receipts/ 含未知条目: {entry.name}")
            value = _read_canonical_json(
                entry, owner=self.owner, label=f"receipt {entry.name}")
            relative = value.get("path")
            if (not isinstance(relative, str)
                    or self._file_token(relative) != entry.stem
                    or relative not in uploads):
                raise DraftCorruptError("orphan/mismatched file receipt")
            checked = self._file_receipt_locked(draft_root, relative)
            if checked != value:
                raise DraftCorruptError("file receipt 重读漂移")
        if len(uploads) > _MAX_FILES:
            raise DraftCorruptError("draft file count 超过上限")
        total = sum(item["meta"]["size"] for item in uploads.values())
        if total > _MAX_TOTAL_BYTES:
            raise DraftCorruptError("draft declared bytes 超过上限")
        self._validate_files_tree_locked(
            draft_root, uploads=uploads, verify_content=verify_content)
        return uploads

    def _validate_files_tree_locked(
            self, draft_root: Path, *, uploads: Mapping[str, Mapping[str, Any]],
            verify_content: bool) -> None:
        files_root = draft_root / "files"
        expected_files = {
            relative: item["receipt"] for relative, item in uploads.items()
            if item["receipt"] is not None
        }
        expected_dirs = {""}
        for relative in expected_files:
            parts = PurePosixPath(relative).parts[:-1]
            expected_dirs.update(
                PurePosixPath(*parts[:index]).as_posix()
                for index in range(1, len(parts) + 1))
        seen_files = set()
        seen_dirs = set()
        for current, dirs, files in os.walk(files_root, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            current_path = Path(current)
            rel_dir = current_path.relative_to(files_root).as_posix()
            if rel_dir == ".":
                rel_dir = ""
            _directory_info(
                current_path, owner=self.owner,
                label=f"files/{rel_dir or '.'}")
            if rel_dir not in expected_dirs:
                raise DraftCorruptError(f"files/ 含未知目录: {rel_dir}")
            seen_dirs.add(rel_dir)
            for name in dirs:
                child = current_path / name
                _directory_info(
                    child, owner=self.owner,
                    label=f"files/{child.relative_to(files_root).as_posix()}")
            for name in files:
                child = current_path / name
                relative = child.relative_to(files_root).as_posix()
                receipt = expected_files.get(relative)
                if receipt is None:
                    raise DraftCorruptError(f"files/ 含未知文件: {relative}")
                fd, info = _open_regular(
                    child, owner=self.owner, label=f"files/{relative}")
                try:
                    if info.st_size != receipt["size"]:
                        raise DraftCorruptError(f"files/{relative} size 漂移")
                    if (verify_content
                            and _hash_fd(
                                fd, expected_size=receipt["size"],
                                label=f"files/{relative}") != receipt["sha256"]):
                        raise DraftCorruptError(f"files/{relative} hash 漂移")
                finally:
                    os.close(fd)
                seen_files.add(relative)
        if seen_files != set(expected_files) or seen_dirs != expected_dirs:
            raise DraftCorruptError("files tree 与 receipts 不一致")

    @staticmethod
    def _entry(relative: str, size: int,
               receipt: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        return {
            "path": relative,
            "size": size,
            "sha256": None if receipt is None else receipt["sha256"],
            "status": "uploading" if receipt is None else "complete",
        }

    def _files_manifest_locked(self, draft_id: object) -> List[Dict[str, Any]]:
        record = self._draft_record_locked(draft_id)
        root = self.drafts_dir / record["draft_id"]
        uploads = self._scan_uploads_locked(root, verify_content=True)
        return [
            self._entry(relative, item["meta"]["size"], item["receipt"])
            for relative, item in sorted(uploads.items())
        ]

    def _summary_locked(self, draft_id: object) -> Dict[str, Any]:
        record = self._draft_record_locked(draft_id)
        root = self.drafts_dir / record["draft_id"]
        # This is the Web polling path. Receipts bind content hashes; summaries
        # validate inode/type/link/mode/size without re-reading multi-GiB files.
        uploads = self._scan_uploads_locked(root, verify_content=False)
        spec = record["spec"]
        return {
            "draft_id": record["draft_id"],
            "quest_id": spec["quest_id"],
            "title": spec["title"],
            "created_at": record["created_at"],
            "template_id": spec.get("template_id"),
            "qualification_profile_id": spec.get("qualification_profile_id"),
            "runtime_profile": copy.deepcopy(
                spec.get("runtime_profile", DEFAULT_PROFILE)),
            "file_count": len(uploads),
            "total_declared_bytes": sum(
                item["meta"]["size"] for item in uploads.values()),
        }

    def get(self, draft_id: object) -> Dict[str, Any]:
        with self._locked():
            return self._summary_locked(draft_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._locked():
            return [
                self._summary_locked(entry.name)
                for entry in sorted(
                    self.drafts_dir.iterdir(), key=lambda item: item.name)
            ]

    def spec(self, draft_id: object) -> Dict[str, Any]:
        """Return an isolated server-side spec; this is not a public summary."""
        with self._locked():
            record = self._draft_record_locked(draft_id)
            return copy.deepcopy(record["spec"])

    def files_root(self, draft_id: object) -> Path:
        """Return a validated server capability that must never reach browsers.

        The capability remains owned by this registry.  Callers may consume it
        for later server-side publication, but this method does not move, seal,
        or publish any file.
        """
        with self._locked():
            record = self._draft_record_locked(draft_id)
            root = self.drafts_dir / record["draft_id"]
            self._scan_uploads_locked(root, verify_content=False)
            return root / "files"

    def begin_file(self, draft_id: object, path: object,
                   size: object) -> Dict[str, Any]:
        relative = _validate_relative_path(path)
        if (isinstance(size, bool) or not isinstance(size, int)
                or not 0 <= size <= _MAX_FILE_BYTES):
            raise ValueError(f"file size 须为 0..{_MAX_FILE_BYTES}")
        with self._locked():
            record = self._draft_record_locked(draft_id)
            root = self.drafts_dir / record["draft_id"]
            uploads = self._scan_uploads_locked(root, verify_content=False)
            existing = uploads.get(relative)
            if existing is not None:
                if existing["meta"]["size"] != size:
                    raise DraftConflictError(
                        f"upload path {relative} 已绑定不同 size")
                return self._entry(relative, size, existing["receipt"])
            new_parts = PurePosixPath(relative).parts
            for current in uploads:
                old_parts = PurePosixPath(current).parts
                common = min(len(new_parts), len(old_parts))
                if new_parts[:common] == old_parts[:common]:
                    raise DraftConflictError(
                        f"upload path {relative} 与已声明文件 {current} 祖先冲突")
            if len(uploads) >= _MAX_FILES:
                raise DraftConflictError("draft file count 超过上限")
            total = sum(item["meta"]["size"] for item in uploads.values())
            if total + size > _MAX_TOTAL_BYTES:
                raise DraftConflictError("draft total declared bytes 超过上限")
            token = self._file_token(relative)
            incoming = root / "incoming"
            final = incoming / token
            stage = incoming / f".creating-{token}-{uuid.uuid4().hex}"
            stage.mkdir(mode=0o700)
            try:
                (stage / "chunks").mkdir(mode=0o700)
                _write_new(stage / "data.part", b"")
                _write_new(stage / "meta.json", _canonical({
                    "version": _UPLOAD_VERSION,
                    "path": relative,
                    "size": size,
                }))
                _fsync_dir(stage / "chunks")
                _fsync_dir(stage)
                os.rename(stage, final)
                _fsync_dir(incoming)
            except BaseException:
                if os.path.lexists(stage):
                    shutil.rmtree(stage)
                raise
            return self._entry(relative, size, None)

    def append_chunk(self, draft_id: object, path: object, offset: object,
                     bytes: object, chunk_sha256: object) -> Dict[str, Any]:
        relative = _validate_relative_path(path)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("chunk offset 须为非负整数")
        if not isinstance(bytes, (builtins.bytes, bytearray, memoryview)):
            raise ValueError("chunk data 须为 bytes-like")
        payload = builtins.bytes(bytes)
        if not 1 <= len(payload) <= _MAX_CHUNK_BYTES:
            raise ValueError(f"chunk 须为 1..{_MAX_CHUNK_BYTES} bytes")
        digest = _validate_sha256(chunk_sha256, label="chunk_sha256")
        if _sha256(payload) != digest:
            raise ValueError("chunk_sha256 与 chunk bytes 不符")
        with self._locked():
            record = self._draft_record_locked(draft_id)
            root = self.drafts_dir / record["draft_id"]
            loaded = self._upload_meta_locked(root, relative)
            if loaded is None:
                raise KeyError(f"upload 未 begin: {relative}")
            upload_root, meta = loaded
            if self._file_receipt_locked(root, relative) is not None:
                raise DraftConflictError(f"upload {relative} 已 complete")
            fd, committed = self._open_upload_data_locked(
                upload_root, relative=relative,
                declared_size=meta["size"], recover_tail=True)
            try:
                receipt_path = upload_root / "chunks" / f"{offset:020d}.json"
                if os.path.lexists(receipt_path):
                    row = _read_canonical_json(
                        receipt_path, owner=self.owner,
                        label=f"upload {relative} replay chunk")
                    if (row.get("path") != relative
                            or row.get("offset") != offset
                            or row.get("bytes") != len(payload)
                            or row.get("sha256") != digest):
                        raise DraftConflictError(
                            f"upload {relative} offset {offset} 已绑定不同 chunk")
                    existing = os.pread(fd, len(payload), offset)
                    if existing != payload:
                        raise DraftCorruptError(
                            f"upload {relative} replay bytes 与 chunk receipt 漂移")
                    return self._entry(relative, meta["size"], None)
                if offset != committed:
                    raise DraftConflictError(
                        f"upload {relative} offset 应为 {committed}，实收 {offset}")
                if offset + len(payload) > meta["size"]:
                    raise DraftConflictError(
                        f"upload {relative} chunk 超过 declared size")
                view = memoryview(payload)
                position = offset
                while view:
                    written = os.pwrite(fd, view, position)
                    if written <= 0:
                        raise OSError("short pwrite")
                    view = view[written:]
                    position += written
                os.fsync(fd)
                chunk_receipt = {
                    "version": _UPLOAD_VERSION,
                    "path": relative,
                    "offset": offset,
                    "bytes": len(payload),
                    "sha256": digest,
                }
                _write_new(receipt_path, _canonical(chunk_receipt))
                _fsync_dir(upload_root / "chunks")
                return self._entry(relative, meta["size"], None)
            finally:
                os.close(fd)

    def _ensure_final_parent_locked(
            self, files_root: Path, relative: str) -> Path:
        current = files_root
        for part in PurePosixPath(relative).parts[:-1]:
            child = current / part
            if os.path.lexists(child):
                _directory_info(
                    child, owner=self.owner,
                    label=f"files parent {child.relative_to(files_root)}")
            else:
                child.mkdir(mode=0o700)
                _fsync_dir(current)
            current = child
        return current

    def finalize_file(self, draft_id: object, path: object,
                      sha256: object) -> Dict[str, Any]:
        relative = _validate_relative_path(path)
        expected_hash = _validate_sha256(sha256, label="sha256")
        with self._locked():
            record = self._draft_record_locked(draft_id)
            root = self.drafts_dir / record["draft_id"]
            loaded = self._upload_meta_locked(root, relative)
            if loaded is None:
                raise KeyError(f"upload 未 begin: {relative}")
            upload_root, meta = loaded
            receipt = self._file_receipt_locked(root, relative)
            final_path = root / "files" / Path(*PurePosixPath(relative).parts)
            data_path = upload_root / "data.part"
            if receipt is not None:
                if receipt["sha256"] != expected_hash:
                    raise DraftConflictError(
                        f"file {relative} 已以不同 hash complete")
                fd, info = _open_regular(
                    final_path, owner=self.owner, label=f"final file {relative}")
                try:
                    actual = _hash_fd(
                        fd, expected_size=receipt["size"],
                        label=f"final file {relative}")
                finally:
                    os.close(fd)
                if info.st_size != meta["size"] or actual != expected_hash:
                    raise DraftCorruptError(f"final file {relative} 漂移")
                return self._entry(relative, meta["size"], receipt)

            fd = -1
            published = False
            if os.path.lexists(data_path):
                fd, committed = self._open_upload_data_locked(
                    upload_root, relative=relative,
                    declared_size=meta["size"], recover_tail=True)
                if committed != meta["size"]:
                    os.close(fd)
                    raise DraftConflictError(
                        f"file {relative} 尚未收齐: {committed}/{meta['size']}")
                parent = self._ensure_final_parent_locked(root / "files", relative)
                if os.path.lexists(final_path):
                    os.close(fd)
                    raise DraftCorruptError(
                        f"file {relative} 未有 receipt 却已存在 final path")
                actual = _hash_fd(
                    fd, expected_size=meta["size"],
                    label=f"upload {relative}")
                if actual != expected_hash:
                    os.close(fd)
                    raise DraftConflictError(f"file {relative} sha256 不符")
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                os.rename(data_path, final_path)
                _fsync_dir(parent)
                published = True
            elif os.path.lexists(final_path):
                # Crash recovery for rename -> receipt publication.
                fd, _info = _open_regular(
                    final_path, owner=self.owner,
                    label=f"recover final file {relative}")
                actual = _hash_fd(
                    fd, expected_size=meta["size"],
                    label=f"recover final file {relative}")
                if actual != expected_hash:
                    os.close(fd)
                    raise DraftConflictError(f"file {relative} recovery sha256 不符")
                published = True
            else:
                raise DraftCorruptError(
                    f"file {relative} data/final 同时缺失")
            try:
                after = os.fstat(fd)
                if (not published or after.st_nlink != 1
                        or after.st_size != meta["size"]
                        or after.st_uid != self.owner
                        or stat.S_IMODE(after.st_mode) != 0o600):
                    raise DraftCorruptError(
                        f"file {relative} final publication 身份非法")
                receipt = {
                    "version": _FILE_RECEIPT_VERSION,
                    "path": relative,
                    "size": meta["size"],
                    "sha256": expected_hash,
                    "status": "complete",
                }
                receipt_path = (
                    root / "receipts" / f"{self._file_token(relative)}.json")
                _write_new(receipt_path, _canonical(receipt))
                _fsync_dir(root / "receipts")
                return self._entry(relative, meta["size"], receipt)
            finally:
                if fd >= 0:
                    os.close(fd)

    def files_manifest(self, draft_id: object) -> List[Dict[str, Any]]:
        with self._locked():
            return self._files_manifest_locked(draft_id)


__all__ = [
    "DraftConflictError", "DraftCorruptError", "QuestDraftRegistry",
]
