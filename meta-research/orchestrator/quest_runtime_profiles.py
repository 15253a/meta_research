"""Per-quest runtime-profile ledger.

The quest manifest is an immutable creation identity and is deliberately not
used for mutable runtime choices.  This module owns a small append-only ledger
below ``<quest>/state/runtime-settings`` instead.  Every revision is canonical
JSON, linked to the exact bytes of its predecessor and committed with an
exclusive create plus directory fsync.

Missing storage is a legacy quest, not permission to create files while
reading: :meth:`QuestRuntimeSettings.current` synthesizes the documented
default without mutating the work root.  Only ``initialize`` and ``update``
create or append storage.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple


LEGACY_PROFILE_VERSION = 1
PROFILE_VERSION = 2
EXACT_MULTI_GPU_PROFILE_VERSION = 3
DEFAULT_PROFILE: Dict[str, Any] = {
    "version": LEGACY_PROFILE_VERSION,
    "compute_profile_id": "local-gpu",
    "review_intensity": "once",
}

_COMPUTE_PROFILES = ("local-gpu", "local-cpu")
_REVIEW_INTENSITIES = ("once", "off")
_MAX_GPU_DEVICE_INDEX = 4095
_MAX_GPU_SELECTION = 64
_MAX_GPU_MODEL_BYTES = 256
_MAX_SAFE_INTEGER = 9007199254740991
_SAFE_GPU_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()\-]*$")
_GPU_UUID_LIKE_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:GPU-)?"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:$|[^A-Za-z0-9-])"
    r"|(?:^|[^A-Za-z0-9])GPU-[A-Za-z0-9-]{8,}(?:$|[^A-Za-z0-9-])")
_QUEST_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_IDEMPOTENCY_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^([0-9]{20})\.json$")
_OPERATION_RE = re.compile(r"^([0-9a-f]{32})\.json$")
_OPERATION_COMPLETION_RE = re.compile(
    r"^([0-9a-f]{32})\.completion\.json$")
_OPERATION_SETTLEMENT_RE = re.compile(
    r"^([0-9a-f]{32})\.settlement\.json$")
_START_OPERATION_RE = re.compile(r"^([0-9a-f]{32})\.json$")
_OWNER_INTENT_REVISION_RE = re.compile(r"^([0-9]{20})\.json$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$")
_LEGACY_RECORD_VERSION = 1
_RECORD_VERSION = 2
_MAX_RECORD_BYTES = 64 * 1024
_MAX_REVISIONS = 10_000
_MAX_OPERATIONS = 20_000
_LOCK_BODY = b"quest-runtime-settings-v1\n"
_CYCLE_BINDING_VERSION = 1
_LEGACY_OPERATION_VERSION = 1
_PREVIOUS_OPERATION_VERSION = 2
_OPERATION_VERSION = 3
_TRANSITION_VERSION = 2
_OWNER_INTENT_VERSION = 1
_START_OPERATION_VERSION = 1


class RuntimeProfileConflictError(ValueError):
    """One idempotency key or initialization identity was reused differently."""


class RuntimeSettingsCorruptError(RuntimeError):
    """The runtime-settings ledger cannot prove its filesystem/hash identity."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise ValueError("runtime profile value 无法 canonicalize") from error


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def normalize_profile(value: object) -> Dict[str, Any]:
    """Return one canonical legacy-v1, candidate-v2 or exact-v3 profile.

    V2 indices are a candidate pool from which the trusted runtime allocates
    its already-authorized count.  V3 indices are the complete exact device
    set, but a trusted caller still has to intersect them with deployment
    policy/inventory.  Neither version can supply paths, images, environment
    variables or memory limits.
    """
    if not isinstance(value, Mapping):
        raise ValueError("runtime_profile 字段闭包非法")
    version = value.get("version")
    base_fields = {"version", "compute_profile_id", "review_intensity"}
    indexed_versions = {PROFILE_VERSION, EXACT_MULTI_GPU_PROFILE_VERSION}
    expected_fields = (
        base_fields if version == LEGACY_PROFILE_VERSION
        else base_fields | {"gpu_device_indices"}
        if version in indexed_versions else set())
    if (type(version) is not int or not expected_fields
            or set(value) != expected_fields):
        raise ValueError(
            "runtime_profile.version/字段闭包只支持 legacy v1、candidate v2 或 exact v3")
    compute = value.get("compute_profile_id")
    review = value.get("review_intensity")
    if compute not in _COMPUTE_PROFILES:
        raise ValueError("compute_profile_id 只支持 local-gpu/local-cpu")
    if review not in _REVIEW_INTENSITIES:
        raise ValueError("review_intensity 只支持 once/off")
    normalized = {
        "version": version,
        "compute_profile_id": compute,
        "review_intensity": review,
    }
    if version in indexed_versions:
        indices = value.get("gpu_device_indices")
        if (not isinstance(indices, list) or len(indices) > _MAX_GPU_SELECTION
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or not 0 <= item <= _MAX_GPU_DEVICE_INDEX
                       for item in indices)
                or indices != sorted(set(indices))):
            raise ValueError("gpu_device_indices 须为升序唯一的设备编号数组")
        if compute == "local-gpu" and not indices:
            raise ValueError("local-gpu v2/v3 的 gpu_device_indices 不得为空")
        if compute == "local-cpu" and indices:
            raise ValueError("local-cpu v2/v3 的 gpu_device_indices 必须为空")
        normalized["gpu_device_indices"] = list(indices)
    return normalized


def public_options(*, allowed_gpu_indices: Optional[List[int]] = None,
                   requested_gpu_count: Optional[int] = None,
                   exact_multi_gpu: bool = False,
                   gpu_device_labels: Optional[List[Mapping[str, Any]]] = None
                   ) -> Dict[str, Any]:
    """Return the path-free catalog for ``GET /api/setup``.

    With no arguments this preserves the legacy device-free catalog.  Passing
    only the trusted allowlist/count preserves the V2 candidate-pool catalog.
    ``exact_multi_gpu=True`` opts into V3; optional label rows must already be
    a trusted, policy-filtered detection projection and may contain only
    index/model/memory -- never UUIDs or paths.
    """
    if not isinstance(exact_multi_gpu, bool):
        raise ValueError("exact_multi_gpu 须为 bool")
    if gpu_device_labels is not None and not exact_multi_gpu:
        raise ValueError("gpu_device_labels 只允许 exact_multi_gpu catalog")
    if (allowed_gpu_indices is None) != (requested_gpu_count is None):
        raise ValueError("GPU option catalog 须同时提供 allowlist/count")
    selected: Optional[List[int]] = None
    devices: Optional[List[Dict[str, Any]]] = None
    if allowed_gpu_indices is not None:
        if (not isinstance(allowed_gpu_indices, list)
                or not allowed_gpu_indices
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or not 0 <= item <= _MAX_GPU_DEVICE_INDEX
                       for item in allowed_gpu_indices)
                or allowed_gpu_indices != sorted(set(allowed_gpu_indices))
                or not isinstance(requested_gpu_count, int)
                or isinstance(requested_gpu_count, bool)
                or not 1 <= requested_gpu_count <= _MAX_GPU_SELECTION
                or len(allowed_gpu_indices) > _MAX_GPU_SELECTION
                or (not exact_multi_gpu
                    and requested_gpu_count > len(allowed_gpu_indices))):
            raise ValueError("GPU option catalog 与受信 policy 不一致")
        # V2 exposes this complete trusted candidate pool while deployment
        # still allocates its legacy fixed count.  V3 treats the trusted set as
        # the exact default so the local runtime can use every allowed device.
        selected = list(allowed_gpu_indices)
        devices = [
            {"index": index, "label": f"GPU {index}"}
            for index in allowed_gpu_indices
        ]
    if exact_multi_gpu and selected is None:
        raise ValueError("exact_multi_gpu catalog 要求 trusted allowlist/count")

    if exact_multi_gpu and gpu_device_labels is not None:
        if not isinstance(gpu_device_labels, list):
            raise ValueError("gpu_device_labels 须为安全设备行数组")
        labeled_devices: List[Dict[str, Any]] = []
        label_indices: List[int] = []
        assert selected is not None
        for row in gpu_device_labels:
            if not isinstance(row, Mapping) or set(row) != {
                    "index", "model", "memory_bytes"}:
                raise ValueError("gpu_device_labels 字段闭包非法")
            index = row.get("index")
            model = row.get("model")
            memory_bytes = row.get("memory_bytes")
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index not in selected
                    or not isinstance(model, str) or model != model.strip()
                    or not model
                    or len(model.encode("utf-8")) > _MAX_GPU_MODEL_BYTES
                    or _SAFE_GPU_MODEL_RE.fullmatch(model) is None
                    or _GPU_UUID_LIKE_RE.search(model) is not None
                    or isinstance(memory_bytes, bool)
                    or not isinstance(memory_bytes, int)
                    or not 1 <= memory_bytes <= _MAX_SAFE_INTEGER):
                raise ValueError("gpu_device_labels 含非法/越权设备标签")
            label_indices.append(index)
            memory_label = (
                f"{memory_bytes // (1024 ** 3)} GiB"
                if memory_bytes % (1024 ** 3) == 0
                else f"{memory_bytes} bytes")
            labeled_devices.append({
                "index": index,
                "label": f"GPU {index} · {model} · {memory_label}",
                "model": model,
                "memory_bytes": memory_bytes,
            })
        if not label_indices or label_indices != sorted(set(label_indices)):
            raise ValueError("gpu_device_labels 须为非空升序唯一设备集")
        selected = label_indices
        devices = labeled_devices

    default_profile = copy.deepcopy(DEFAULT_PROFILE)
    catalog_version = LEGACY_PROFILE_VERSION
    if selected is not None:
        assert requested_gpu_count is not None
        catalog_version = (
            EXACT_MULTI_GPU_PROFILE_VERSION
            if exact_multi_gpu else PROFILE_VERSION)
        default_indices = list(selected)
        default_profile = {
            **default_profile,
            "version": catalog_version,
            "gpu_device_indices": default_indices,
        }
    result = {
        "version": catalog_version,
        "compute_profiles": [
            {
                "id": "local-gpu",
                "label": "本机 GPU / Conda / 联网",
                "recommended": True,
            },
            {
                "id": "local-cpu",
                "label": "本机 CPU / Conda / 联网",
                "recommended": False,
            },
        ],
        "review_intensities": [
            {
                "id": "once",
                "label": "每个评审点 1 次",
                "recommended": True,
            },
            {
                "id": "off",
                "label": "关闭",
                "recommended": False,
            },
        ],
        "default_profile": default_profile,
    }
    if devices is not None:
        result["gpu_devices"] = devices
        result["gpu_selection"] = (
            {
                "mode": "exact",
                "default_count": len(devices),
                "min_count": 1,
                "max_count": len(devices),
            }
            if exact_multi_gpu else {
                "requested_count": requested_gpu_count,
            })
    return result


def _validate_quest_id(value: object) -> str:
    if not isinstance(value, str) or _QUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("quest_id 非法")
    return value


def _validate_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ValueError("idempotency_key 须为 32 位小写 hex")
    return value


def _directory(path: Path, *, owner: int, label: str,
               mode: Optional[int] = 0o700) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise RuntimeSettingsCorruptError(f"{label} 不可读") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != owner
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise RuntimeSettingsCorruptError(f"{label} owner/type/mode 非法")
    return info


def _regular(path: Path, *, owner: int, label: str,
             mode: int = 0o600) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise RuntimeSettingsCorruptError(f"{label} 不可读") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) != mode):
        raise RuntimeSettingsCorruptError(f"{label} owner/type/link/mode 非法")
    return info


def _fsync_dir(path: Path) -> None:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, payload: bytes) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular_payload(
        path: Path, *, owner: int, label: str,
        maximum: int = _MAX_RECORD_BYTES) -> bytes:
    info = _regular(path, owner=owner, label=label, mode=0o600)
    if not 2 <= info.st_size <= maximum:
        raise RuntimeSettingsCorruptError(f"{label} 大小非法")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise RuntimeSettingsCorruptError(f"{label} 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                != (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns, before.st_mode,
                    before.st_uid, before.st_nlink)):
            raise RuntimeSettingsCorruptError(f"{label} 读取期间身份漂移")
        return raw
    finally:
        os.close(fd)


def _strict_json(raw: bytes, *, label: str) -> Dict[str, Any]:
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeSettingsCorruptError(
                    f"{label} 含重复 JSON key: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeSettingsCorruptError(
                    f"{label} 含非有限数字: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeSettingsCorruptError(f"{label} 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeSettingsCorruptError(f"{label} 须为 JSON object")
    try:
        canonical = _canonical(value)
    except ValueError as error:
        raise RuntimeSettingsCorruptError(f"{label} 无法 canonicalize") from error
    if raw != canonical:
        raise RuntimeSettingsCorruptError(f"{label} 非 canonical JSON")
    return value


class QuestRuntimeSettings:
    """Append-only runtime settings for one already-published quest."""

    def __init__(self, work_root: Path | str, quest_id: object):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.quest_id = _validate_quest_id(quest_id)
        try:
            info = os.lstat(self.work_root)
        except OSError as error:
            raise ValueError("quest work_root 不可读") from error
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()):
            raise ValueError("quest work_root owner/type 非法")
        self.owner = info.st_uid
        self.state_dir = self.work_root / "state"
        self.root = self.state_dir / "runtime-settings"
        self.revisions_dir = self.root / "revisions"
        self.operations_dir = self.root / "operations"
        self.start_operations_dir = self.root / "start-operations"
        self.owner_intents_dir = self.root / "owner-intents"
        self.owner_intent_revisions_dir = self.owner_intents_dir / "revisions"
        self.lock_path = self.root / ".lock"
        self.cycle_binding_path = self.root / "cycle-binding.json"

    @staticmethod
    def _public(*, quest_id: str, revision: int, profile: Mapping[str, Any],
                record_sha256: Optional[str], source: str) -> Dict[str, Any]:
        return {
            "quest_id": quest_id,
            "revision": revision,
            "profile": copy.deepcopy(dict(profile)),
            "record_sha256": record_sha256,
            "source": source,
        }

    def _legacy(self) -> Dict[str, Any]:
        return self._public(
            quest_id=self.quest_id, revision=0, profile=DEFAULT_PROFILE,
            record_sha256=None, source="legacy-default")

    def _bootstrap(self) -> None:
        _directory(
            self.state_dir, owner=self.owner, label="quest state", mode=0o700)
        if not os.path.lexists(self.root):
            try:
                os.mkdir(self.root, 0o700)
                _fsync_dir(self.state_dir)
            except FileExistsError:
                pass
        _directory(
            self.root, owner=self.owner, label="runtime-settings", mode=0o700)
        if not os.path.lexists(self.revisions_dir):
            try:
                os.mkdir(self.revisions_dir, 0o700)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
        _directory(
            self.revisions_dir, owner=self.owner,
            label="runtime-settings revisions", mode=0o700)
        if not os.path.lexists(self.lock_path):
            try:
                _write_new(self.lock_path, _LOCK_BODY)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
        _regular(
            self.lock_path, owner=self.owner,
            label="runtime-settings lock", mode=0o600)

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[None]:
        if create:
            self._bootstrap()
        elif not os.path.lexists(self.root):
            yield
            return
        else:
            _directory(
                self.state_dir, owner=self.owner, label="quest state", mode=0o700)
            _directory(
                self.root, owner=self.owner, label="runtime-settings", mode=0o700)
            _directory(
                self.revisions_dir, owner=self.owner,
                label="runtime-settings revisions", mode=0o700)
            _regular(
                self.lock_path, owner=self.owner,
                label="runtime-settings lock", mode=0o600)
        if not os.path.lexists(self.root):
            yield
            return
        flags = (os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(self.lock_path, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_record(self, path: Path, *, expected_revision: int,
                     expected_previous: Optional[str]) -> Tuple[Dict[str, Any], bytes, str]:
        info = _regular(
            path, owner=self.owner,
            label=f"runtime-settings revision {expected_revision}")
        if not 2 <= info.st_size <= _MAX_RECORD_BYTES:
            raise RuntimeSettingsCorruptError("runtime-settings revision 大小非法")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    raise RuntimeSettingsCorruptError(
                        "runtime-settings revision 被截断")
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns, before.st_ctime_ns, before.st_mode,
                        before.st_uid, before.st_nlink)):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings revision 读取期间身份漂移")
        finally:
            os.close(fd)
        record = _strict_json(
            raw, label=f"runtime-settings revision {expected_revision}")
        record_version = record.get("version")
        common_fields = {
            "version", "quest_id", "revision", "operation",
            "idempotency_key", "profile", "previous_sha256", "recorded_at",
        }
        expected_fields = (
            common_fields
            if record_version == _LEGACY_RECORD_VERSION
            else common_fields | {"owner_intent_revision"})
        if (record_version not in {_LEGACY_RECORD_VERSION, _RECORD_VERSION}
                or set(record) != expected_fields):
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision 字段闭包非法")
        if record_version == _LEGACY_RECORD_VERSION:
            record["owner_intent_revision"] = 0
        revision = record.get("revision")
        owner_intent_revision = record.get("owner_intent_revision")
        if (record.get("quest_id") != self.quest_id
                or type(revision) is not int
                or revision != expected_revision
                or type(owner_intent_revision) is not int
                or owner_intent_revision < 0
                or record.get("operation") not in {"initialize", "update"}
                or not isinstance(record.get("idempotency_key"), str)
                or _IDEMPOTENCY_RE.fullmatch(record["idempotency_key"]) is None
                or not isinstance(record.get("recorded_at"), str)
                or _TIMESTAMP_RE.fullmatch(record["recorded_at"]) is None):
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision 身份/类型非法")
        if record.get("previous_sha256") != expected_previous:
            raise RuntimeSettingsCorruptError(
                "runtime-settings previous_sha256 哈希链断裂")
        if expected_revision > 1 and record["operation"] == "initialize":
            raise RuntimeSettingsCorruptError(
                "runtime-settings initialize 只能是首条 revision")
        try:
            datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision recorded_at 非法") from error
        try:
            profile = normalize_profile(record.get("profile"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision profile 非法") from error
        if profile != record["profile"]:
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision profile 非规范")
        digest = _digest(raw)
        return record, raw, digest

    def _scan_locked(self) -> List[Tuple[Dict[str, Any], str]]:
        if not os.path.lexists(self.root):
            return []
        entries = {entry.name for entry in self.root.iterdir()}
        if (not {".lock", "revisions"}.issubset(entries)
                or not entries.issubset({
                    ".lock", "revisions", "operations", "start-operations",
                    "owner-intents", "cycle-binding.json",
                })):
            raise RuntimeSettingsCorruptError(
                "runtime-settings 含未知/缺失条目")
        if "operations" in entries:
            _directory(
                self.operations_dir, owner=self.owner,
                label="runtime-settings operations", mode=0o700)
        if "start-operations" in entries:
            _directory(
                self.start_operations_dir, owner=self.owner,
                label="runtime-settings start operations", mode=0o700)
        if "owner-intents" in entries:
            _directory(
                self.owner_intents_dir, owner=self.owner,
                label="runtime-settings owner intents", mode=0o700)
            owner_entries = {
                entry.name for entry in self.owner_intents_dir.iterdir()}
            if owner_entries != {"revisions"}:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings owner intents 含未知/缺失条目")
            _directory(
                self.owner_intent_revisions_dir, owner=self.owner,
                label="runtime-settings owner intent revisions", mode=0o700)
        names = sorted(entry.name for entry in self.revisions_dir.iterdir())
        if len(names) > _MAX_REVISIONS:
            raise RuntimeSettingsCorruptError(
                "runtime-settings revision 数超过安全上限")
        records: List[Tuple[Dict[str, Any], str]] = []
        previous = None
        keys = set()
        for expected_revision, name in enumerate(names, start=1):
            match = _REVISION_RE.fullmatch(name)
            if match is None or int(match.group(1)) != expected_revision:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings revision 文件名不连续/非法")
            record, _raw, digest = self._read_record(
                self.revisions_dir / name,
                expected_revision=expected_revision,
                expected_previous=previous)
            key = record["idempotency_key"]
            if key in keys:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings idempotency_key 重复")
            keys.add(key)
            records.append((record, digest))
            previous = digest
        owner_intents = self._scan_owner_intents_locked(records)
        if "start-operations" in entries:
            self._scan_start_operations_locked(owner_intents)
        if "operations" in entries:
            self._scan_operations_locked(records, owner_intents)
        if "cycle-binding.json" in entries:
            self._read_cycle_binding_locked(records)
        return records

    def _record_from_records(
            self, records: List[Tuple[Dict[str, Any], str]], revision: object,
            record_sha256: object) -> Dict[str, Any]:
        if type(revision) is not int or revision < 0:
            raise ValueError("runtime profile revision 非法")
        if revision == 0:
            if record_sha256 is not None:
                raise ValueError("legacy runtime profile record_sha256 须为 null")
            return self._legacy()
        if (not isinstance(record_sha256, str)
                or _SHA256_RE.fullmatch(record_sha256) is None):
            raise ValueError("runtime profile record_sha256 非法")
        if revision > len(records):
            raise ValueError("runtime profile revision 不存在")
        row, digest = records[revision - 1]
        if digest != record_sha256:
            raise RuntimeProfileConflictError(
                "runtime profile revision/hash identity 不匹配")
        return self._public(
            quest_id=self.quest_id, revision=row["revision"],
            profile=row["profile"], record_sha256=digest, source="ledger")

    def _owner_intent_public(
            self, record: Mapping[str, Any], digest: str) -> Dict[str, Any]:
        return {
            "quest_id": self.quest_id,
            "revision": record["revision"],
            "action": record["action"],
            "idempotency_key": record["idempotency_key"],
            "runtime_revision": record["runtime_revision"],
            "record_sha256": digest,
        }

    def _read_owner_intent_locked(
            self, path: Path, *, expected_revision: int,
            expected_previous: Optional[str], maximum_runtime_revision: int
            ) -> Tuple[Dict[str, Any], str]:
        raw = _read_regular_payload(
            path, owner=self.owner,
            label=f"runtime-settings owner intent {expected_revision}")
        record = _strict_json(
            raw, label=f"runtime-settings owner intent {expected_revision}")
        if set(record) != {
                "version", "quest_id", "revision", "action",
                "idempotency_key", "runtime_revision", "previous_sha256",
                "recorded_at"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings owner intent 字段闭包非法")
        revision = record.get("revision")
        runtime_revision = record.get("runtime_revision")
        recorded_at = record.get("recorded_at")
        if (record.get("version") != _OWNER_INTENT_VERSION
                or record.get("quest_id") != self.quest_id
                or type(revision) is not int
                or revision != expected_revision
                or record.get("action") not in {"start", "terminate"}
                or not isinstance(record.get("idempotency_key"), str)
                or _IDEMPOTENCY_RE.fullmatch(record["idempotency_key"]) is None
                or type(runtime_revision) is not int
                or not 0 <= runtime_revision <= maximum_runtime_revision
                or record.get("previous_sha256") != expected_previous
                or not isinstance(recorded_at, str)
                or _TIMESTAMP_RE.fullmatch(recorded_at) is None):
            raise RuntimeSettingsCorruptError(
                "runtime-settings owner intent 身份/类型非法")
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings owner intent recorded_at 非法") from error
        return record, _digest(raw)

    def _scan_owner_intents_locked(
            self, records: List[Tuple[Dict[str, Any], str]]
            ) -> List[Tuple[Dict[str, Any], str]]:
        if not os.path.lexists(self.owner_intents_dir):
            return []
        _directory(
            self.owner_intents_dir, owner=self.owner,
            label="runtime-settings owner intents", mode=0o700)
        entries = {entry.name for entry in self.owner_intents_dir.iterdir()}
        if entries != {"revisions"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings owner intents 含未知/缺失条目")
        _directory(
            self.owner_intent_revisions_dir, owner=self.owner,
            label="runtime-settings owner intent revisions", mode=0o700)
        names = sorted(
            entry.name for entry in self.owner_intent_revisions_dir.iterdir())
        if len(names) > _MAX_OPERATIONS:
            raise RuntimeSettingsCorruptError(
                "runtime-settings owner intent 数超过安全上限")
        result: List[Tuple[Dict[str, Any], str]] = []
        previous = None
        previous_runtime_revision = 0
        keys = set()
        for expected_revision, name in enumerate(names, start=1):
            match = _OWNER_INTENT_REVISION_RE.fullmatch(name)
            if match is None or int(match.group(1)) != expected_revision:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings owner intent 文件名不连续/非法")
            record, digest = self._read_owner_intent_locked(
                self.owner_intent_revisions_dir / name,
                expected_revision=expected_revision,
                expected_previous=previous,
                maximum_runtime_revision=len(records))
            if record["runtime_revision"] < previous_runtime_revision:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings owner intent runtime revision 倒退")
            if record["idempotency_key"] in keys:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings owner intent idempotency_key 重复")
            keys.add(record["idempotency_key"])
            result.append((record, digest))
            previous = digest
            previous_runtime_revision = record["runtime_revision"]
        for row, _digest_value in records:
            if row["owner_intent_revision"] > len(result):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings revision 引用不存在 owner intent")
        return result

    @staticmethod
    def _owner_intent_allows_operation(
            owner_intent_revision: int,
            intents: List[Tuple[Dict[str, Any], str]]) -> bool:
        if owner_intent_revision < 0 or owner_intent_revision > len(intents):
            return False
        if (owner_intent_revision > 0
                and intents[owner_intent_revision - 1][0]["action"] != "start"):
            return False
        return not any(
            row["action"] == "terminate"
            and row["revision"] > owner_intent_revision
            for row, _digest_value in intents)

    def _ensure_owner_intents_locked(self) -> None:
        if not os.path.lexists(self.owner_intents_dir):
            try:
                os.mkdir(self.owner_intents_dir, 0o700)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
        _directory(
            self.owner_intents_dir, owner=self.owner,
            label="runtime-settings owner intents", mode=0o700)
        if not os.path.lexists(self.owner_intent_revisions_dir):
            try:
                os.mkdir(self.owner_intent_revisions_dir, 0o700)
                _fsync_dir(self.owner_intents_dir)
            except FileExistsError:
                pass
        _directory(
            self.owner_intent_revisions_dir, owner=self.owner,
            label="runtime-settings owner intent revisions", mode=0o700)

    def _append_owner_intent_locked(
            self, *, action: str, key: str,
            records: List[Tuple[Dict[str, Any], str]],
            intents: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], bool]:
        for record, digest in intents:
            if record["idempotency_key"] != key:
                continue
            if record["action"] != action:
                raise RuntimeProfileConflictError(
                    f"idempotency_key {key} 已绑定不同 owner intent")
            public = self._owner_intent_public(record, digest)
            is_current = (
                bool(intents) and record["revision"] == intents[-1][0]["revision"])
            return public, is_current
        if len(intents) >= _MAX_OPERATIONS:
            raise RuntimeProfileConflictError(
                "runtime owner intent 数已达安全上限")
        self._ensure_owner_intents_locked()
        revision = len(intents) + 1
        previous = None if not intents else intents[-1][1]
        record = {
            "version": _OWNER_INTENT_VERSION,
            "quest_id": self.quest_id,
            "revision": revision,
            "action": action,
            "idempotency_key": key,
            "runtime_revision": len(records),
            "previous_sha256": previous,
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
        }
        raw = _canonical(record)
        _write_new(
            self.owner_intent_revisions_dir / f"{revision:020d}.json", raw)
        _fsync_dir(self.owner_intent_revisions_dir)
        return self._owner_intent_public(record, _digest(raw)), True

    @staticmethod
    def _start_operation_public(
            receipt: Mapping[str, Any],
            intents: List[Tuple[Dict[str, Any], str]]) -> Dict[str, Any]:
        owner_revision = receipt["owner_intent_revision"]
        authorized = (
            receipt["outcome"] == "authorized"
            and owner_revision > 0
            and owner_revision == len(intents)
            and intents[owner_revision - 1][0]["action"] == "start")
        return {
            "quest_id": receipt["quest_id"],
            "idempotency_key": receipt["idempotency_key"],
            "outcome": receipt["outcome"],
            "owner_intent_revision": owner_revision,
            "authorized": authorized,
        }

    def _read_start_operation_locked(
            self, path: Path, *, key: str,
            intents: List[Tuple[Dict[str, Any], str]]) -> Dict[str, Any]:
        raw = _read_regular_payload(
            path, owner=self.owner,
            label=f"runtime-settings start operation {key}")
        receipt = _strict_json(
            raw, label=f"runtime-settings start operation {key}")
        if set(receipt) != {
                "version", "quest_id", "idempotency_key", "outcome",
                "owner_intent_revision", "recorded_at"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings start operation 字段闭包非法")
        owner_revision = receipt.get("owner_intent_revision")
        recorded_at = receipt.get("recorded_at")
        if (receipt.get("version") != _START_OPERATION_VERSION
                or receipt.get("quest_id") != self.quest_id
                or receipt.get("idempotency_key") != key
                or receipt.get("outcome") not in {
                    "authorized", "active-noop"}
                or type(owner_revision) is not int
                or not 0 <= owner_revision <= len(intents)
                or not isinstance(recorded_at, str)
                or _TIMESTAMP_RE.fullmatch(recorded_at) is None):
            raise RuntimeSettingsCorruptError(
                "runtime-settings start operation 身份/类型非法")
        if receipt["outcome"] == "authorized":
            if (owner_revision == 0
                    or intents[owner_revision - 1][0]["action"] != "start"
                    or intents[owner_revision - 1][0]["idempotency_key"]
                    != key):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings authorized start 未绑定唯一 start intent")
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings start operation recorded_at 非法") from error
        return receipt

    def _scan_start_operations_locked(
            self, intents: List[Tuple[Dict[str, Any], str]]
            ) -> Dict[str, Dict[str, Any]]:
        if not os.path.lexists(self.start_operations_dir):
            return {}
        _directory(
            self.start_operations_dir, owner=self.owner,
            label="runtime-settings start operations", mode=0o700)
        names = sorted(
            entry.name for entry in self.start_operations_dir.iterdir())
        if len(names) > _MAX_OPERATIONS:
            raise RuntimeSettingsCorruptError(
                "runtime-settings start operation 数超过安全上限")
        result: Dict[str, Dict[str, Any]] = {}
        for name in names:
            match = _START_OPERATION_RE.fullmatch(name)
            if match is None:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings start operation 文件名非法")
            key = match.group(1)
            receipt = self._read_start_operation_locked(
                self.start_operations_dir / name, key=key, intents=intents)
            result[key] = self._start_operation_public(receipt, intents)
        return result

    def _ensure_start_operations_locked(self) -> None:
        if not os.path.lexists(self.start_operations_dir):
            try:
                os.mkdir(self.start_operations_dir, 0o700)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
        _directory(
            self.start_operations_dir, owner=self.owner,
            label="runtime-settings start operations", mode=0o700)

    def _write_start_operation_locked(
            self, *, key: str, outcome: str,
            owner_intent_revision: int,
            intents: List[Tuple[Dict[str, Any], str]]) -> Dict[str, Any]:
        self._ensure_start_operations_locked()
        receipt = {
            "version": _START_OPERATION_VERSION,
            "quest_id": self.quest_id,
            "idempotency_key": key,
            "outcome": outcome,
            "owner_intent_revision": owner_intent_revision,
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
        }
        _write_new(
            self.start_operations_dir / f"{key}.json",
            _canonical(receipt))
        _fsync_dir(self.start_operations_dir)
        return self._start_operation_public(receipt, intents)

    def prepare_explicit_start(
            self, idempotency_key: object, *, active: bool
            ) -> Dict[str, Any]:
        """Bind every first start key to one immutable historical outcome.

        An active observation is durably recorded as ``active-noop`` without
        creating a new owner generation.  Consequently that key can never
        become a future spawn authorization after a stop or manager restart.
        """
        key = _validate_key(idempotency_key)
        if not isinstance(active, bool):
            raise ValueError("active 须为 bool")
        with self._locked(create=True):
            records = self._scan_locked()
            intents = self._scan_owner_intents_locked(records)
            operations = self._scan_start_operations_locked(intents)
            existing = operations.get(key)
            if existing is not None:
                return existing
            if len(operations) >= _MAX_OPERATIONS:
                raise RuntimeProfileConflictError(
                    "runtime start operation 数已达安全上限")

            # Recover the deliberate intent->receipt crash gap.  The intent
            # already proves the historical outcome even if a later fence has
            # made that outcome non-authorizing by the time of replay.
            matching_intent = next((
                row for row, _digest_value in intents
                if row["idempotency_key"] == key), None)
            if matching_intent is not None:
                if matching_intent["action"] != "start":
                    raise RuntimeProfileConflictError(
                        f"idempotency_key {key} 已绑定不同 owner intent")
                return self._write_start_operation_locked(
                    key=key, outcome="authorized",
                    owner_intent_revision=matching_intent["revision"],
                    intents=intents)

            if active:
                return self._write_start_operation_locked(
                    key=key, outcome="active-noop",
                    owner_intent_revision=(
                        0 if not intents else intents[-1][0]["revision"]),
                    intents=intents)

            intent, _is_current = self._append_owner_intent_locked(
                action="start", key=key, records=records, intents=intents)
            intents = self._scan_owner_intents_locked(records)
            return self._write_start_operation_locked(
                key=key, outcome="authorized",
                owner_intent_revision=intent["revision"], intents=intents)

    def authorize_explicit_start(
            self, idempotency_key: object) -> Dict[str, Any]:
        """Backward-compatible inactive-start preparation API."""
        return self.prepare_explicit_start(idempotency_key, active=False)

    def owner_start_generation_authorized(
            self, owner_intent_revision: object) -> bool:
        """Strictly revalidate one managed generation against stop fences."""
        if (type(owner_intent_revision) is not int
                or owner_intent_revision < 0):
            raise ValueError("owner_intent_revision 须为非负整数")
        if not os.path.lexists(self.root):
            return owner_intent_revision == 0
        with self._locked(create=False):
            records = self._scan_locked()
            intents = self._scan_owner_intents_locked(records)
            return self._owner_intent_allows_operation(
                owner_intent_revision, intents)

    def record_explicit_stop(
            self, idempotency_key: object) -> Dict[str, Any]:
        """Fsync a stop fence and terminate older restart intents atomically."""
        key = _validate_key(idempotency_key)
        with self._locked(create=True):
            records = self._scan_locked()
            intents = self._scan_owner_intents_locked(records)
            operations_before = self._scan_operations_locked(records, intents)
            intent, is_current = self._append_owner_intent_locked(
                action="terminate", key=key, records=records, intents=intents)
            appended = (
                not intents or intent["revision"] > intents[-1][0]["revision"])
            if appended:
                current = intent
            elif intents:
                current = self._owner_intent_public(
                    intents[-1][0], intents[-1][1])
            else:
                current = intent
            applied = (
                is_current and current["revision"] == intent["revision"]
                and intent["action"] == "terminate")
            if applied:
                for operation_key, operation in operations_before.items():
                    if (operation["operation"] == "update"
                            and operation["changed"]
                            and operation["status"] in {"pending", "accepted"}):
                        self._write_operation_settlement_locked(
                            key=operation_key, status="terminated")
            return {
                "intent": intent,
                "current": current,
                "applied": applied,
            }

    @staticmethod
    def _operation_public(
            receipt: Mapping[str, Any], *, status: Optional[str] = None
            ) -> Dict[str, Any]:
        return {
            "quest_id": receipt["quest_id"],
            "idempotency_key": receipt["idempotency_key"],
            "operation": receipt["operation"],
            "owner_intent_revision": receipt["owner_intent_revision"],
            "request_profile": copy.deepcopy(receipt["request_profile"]),
            "outcome": copy.deepcopy(receipt["outcome"]),
            "changed": receipt["changed"],
            "schedule_required": receipt["schedule_required"],
            "status": receipt["status"] if status is None else status,
        }

    def _read_operation_receipt_locked(
            self, path: Path, *, key: str,
            records: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], str]:
        raw = _read_regular_payload(
            path, owner=self.owner,
            label=f"runtime-settings operation {key}")
        receipt = _strict_json(raw, label=f"runtime-settings operation {key}")
        receipt_version = receipt.get("version")
        common_fields = {
            "version", "quest_id", "idempotency_key", "request_profile",
            "outcome", "changed", "schedule_required", "status",
            "recorded_at",
        }
        if receipt_version == _LEGACY_OPERATION_VERSION:
            expected_fields = common_fields
        elif receipt_version == _PREVIOUS_OPERATION_VERSION:
            expected_fields = common_fields | {"operation"}
        else:
            expected_fields = common_fields | {
                "operation", "owner_intent_revision"}
        if (receipt_version not in {
                _LEGACY_OPERATION_VERSION, _PREVIOUS_OPERATION_VERSION,
                _OPERATION_VERSION}
                or set(receipt) != expected_fields):
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation 字段闭包非法")
        if receipt_version == _LEGACY_OPERATION_VERSION:
            receipt["operation"] = "update"
        if receipt_version in {
                _LEGACY_OPERATION_VERSION, _PREVIOUS_OPERATION_VERSION}:
            receipt["owner_intent_revision"] = 0
        try:
            request_profile = normalize_profile(receipt.get("request_profile"))
            outcome_value = receipt.get("outcome")
            revision, record_digest, outcome_profile = self._public_identity(
                outcome_value)
            expected = self._record_from_records(
                records, revision, record_digest)
        except (ValueError, RuntimeProfileConflictError) as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation profile/outcome 非法") from error
        changed = receipt.get("changed")
        schedule_required = receipt.get("schedule_required")
        status = receipt.get("status")
        recorded_at = receipt.get("recorded_at")
        owner_intent_revision = receipt.get("owner_intent_revision")
        if (receipt.get("quest_id") != self.quest_id
                or receipt.get("idempotency_key") != key
                or receipt.get("operation") not in {"initialize", "update"}
                or type(owner_intent_revision) is not int
                or owner_intent_revision < 0
                or receipt["request_profile"] != request_profile
                or not isinstance(changed, bool)
                or not isinstance(schedule_required, bool)
                or not isinstance(recorded_at, str)
                or _TIMESTAMP_RE.fullmatch(recorded_at) is None
                or not isinstance(outcome_value, Mapping)
                or dict(outcome_value) != expected
                or outcome_profile != request_profile):
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation 身份/类型非法")
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation recorded_at 非法") from error
        ledger_matches = [
            row for row, _digest_value in records
            if row["idempotency_key"] == key
        ]
        if changed:
            if (receipt["operation"] != "update"
                    or schedule_required is not True or status != "pending"
                    or len(ledger_matches) != 1
                    or ledger_matches[0]["operation"] != "update"
                    or ledger_matches[0]["revision"] != expected["revision"]
                    or ledger_matches[0]["owner_intent_revision"]
                    != owner_intent_revision
                    or ledger_matches[0]["profile"] != request_profile):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings changed operation 未绑定唯一 ledger update")
        elif (schedule_required is not False or status != "not-required"
              or ledger_matches):
            raise RuntimeSettingsCorruptError(
                "runtime-settings no-op operation 身份非法")
        return receipt, _digest(raw)

    def _read_operation_completion_locked(
            self, path: Path, *, key: str,
            receipt_digest: str) -> Tuple[str, str]:
        raw = _read_regular_payload(
            path, owner=self.owner,
            label=f"runtime-settings operation completion {key}")
        completion = _strict_json(
            raw, label=f"runtime-settings operation completion {key}")
        if set(completion) != {
                "version", "quest_id", "idempotency_key", "receipt_sha256",
                "status", "recorded_at"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation completion 字段闭包非法")
        recorded_at = completion.get("recorded_at")
        version = completion.get("version")
        allowed_statuses = (
            {"scheduled", "not-required"}
            if version == _LEGACY_OPERATION_VERSION
            else {"accepted", "not-required"})
        if (version not in {
                _LEGACY_OPERATION_VERSION, _TRANSITION_VERSION}
                or completion.get("quest_id") != self.quest_id
                or completion.get("idempotency_key") != key
                or completion.get("receipt_sha256") != receipt_digest
                or completion.get("status") not in allowed_statuses
                or not isinstance(recorded_at, str)
                or _TIMESTAMP_RE.fullmatch(recorded_at) is None):
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation completion 身份/类型非法")
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation completion recorded_at 非法") from error
        status = completion["status"]
        return ("accepted" if status == "scheduled" else status), _digest(raw)

    def _read_operation_settlement_locked(
            self, path: Path, *, key: str, receipt_digest: str,
            acceptance_digest: Optional[str]) -> str:
        raw = _read_regular_payload(
            path, owner=self.owner,
            label=f"runtime-settings operation settlement {key}")
        settlement = _strict_json(
            raw, label=f"runtime-settings operation settlement {key}")
        if set(settlement) != {
                "version", "quest_id", "idempotency_key", "receipt_sha256",
                "acceptance_sha256", "status", "recorded_at"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation settlement 字段闭包非法")
        recorded_at = settlement.get("recorded_at")
        if (settlement.get("version") != _TRANSITION_VERSION
                or settlement.get("quest_id") != self.quest_id
                or settlement.get("idempotency_key") != key
                or settlement.get("receipt_sha256") != receipt_digest
                or settlement.get("acceptance_sha256") != acceptance_digest
                or settlement.get("status") not in {
                    "applied", "not-required", "terminated"}
                or not isinstance(recorded_at, str)
                or _TIMESTAMP_RE.fullmatch(recorded_at) is None):
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation settlement 身份/类型非法")
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation settlement recorded_at 非法") from error
        return settlement["status"]

    def _scan_operations_locked(
            self, records: List[Tuple[Dict[str, Any], str]],
            owner_intents: Optional[
                List[Tuple[Dict[str, Any], str]]] = None
            ) -> Dict[str, Dict[str, Any]]:
        if owner_intents is None:
            owner_intents = self._scan_owner_intents_locked(records)
        if not os.path.lexists(self.operations_dir):
            return {}
        _directory(
            self.operations_dir, owner=self.owner,
            label="runtime-settings operations", mode=0o700)
        names = sorted(entry.name for entry in self.operations_dir.iterdir())
        if len(names) > _MAX_OPERATIONS * 3:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation 数超过安全上限")
        base_names: Dict[str, str] = {}
        completion_names: Dict[str, str] = {}
        settlement_names: Dict[str, str] = {}
        for name in names:
            settlement_match = _OPERATION_SETTLEMENT_RE.fullmatch(name)
            if settlement_match is not None:
                settlement_names[settlement_match.group(1)] = name
                continue
            completion_match = _OPERATION_COMPLETION_RE.fullmatch(name)
            if completion_match is not None:
                completion_names[completion_match.group(1)] = name
                continue
            base_match = _OPERATION_RE.fullmatch(name)
            if base_match is None:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings operation 文件名非法")
            base_names[base_match.group(1)] = name
        if len(base_names) > _MAX_OPERATIONS:
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation receipt 数超过安全上限")
        if (not set(completion_names).issubset(base_names)
                or not set(settlement_names).issubset(base_names)):
            raise RuntimeSettingsCorruptError(
                "runtime-settings operation transition 缺 receipt")
        result: Dict[str, Dict[str, Any]] = {}
        for key, name in base_names.items():
            receipt, receipt_digest = self._read_operation_receipt_locked(
                self.operations_dir / name, key=key, records=records)
            status = receipt["status"]
            acceptance_digest = None
            if key in completion_names:
                if not receipt["changed"]:
                    raise RuntimeSettingsCorruptError(
                        "runtime-settings no-op operation 不得有 completion")
                status, acceptance_digest = self._read_operation_completion_locked(
                    self.operations_dir / completion_names[key], key=key,
                    receipt_digest=receipt_digest)
            if key in settlement_names:
                if not receipt["changed"] or status not in {"pending", "accepted"}:
                    raise RuntimeSettingsCorruptError(
                        "runtime-settings operation settlement 前驱状态非法")
                status = self._read_operation_settlement_locked(
                    self.operations_dir / settlement_names[key], key=key,
                    receipt_digest=receipt_digest,
                    acceptance_digest=acceptance_digest)
            owner_intent_revision = receipt["owner_intent_revision"]
            if owner_intent_revision > len(owner_intents):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings operation 引用不存在 owner intent")
            if (receipt["changed"] and status in {"pending", "accepted"}
                    and not self._owner_intent_allows_operation(
                        owner_intent_revision, owner_intents)):
                status = "terminated"
            result[key] = self._operation_public(receipt, status=status)
        return result

    @staticmethod
    def _public_identity(value: object) -> Tuple[int, Optional[str], Dict[str, Any]]:
        if not isinstance(value, Mapping) or set(value) != {
                "quest_id", "revision", "profile", "record_sha256", "source"}:
            raise ValueError("runtime profile public record 字段闭包非法")
        revision = value.get("revision")
        digest = value.get("record_sha256")
        profile = normalize_profile(value.get("profile"))
        return revision, digest, profile

    def _ensure_operations_locked(self) -> None:
        if not os.path.lexists(self.operations_dir):
            try:
                os.mkdir(self.operations_dir, 0o700)
                _fsync_dir(self.root)
            except FileExistsError:
                pass
        _directory(
            self.operations_dir, owner=self.owner,
            label="runtime-settings operations", mode=0o700)

    def _write_operation_receipt_locked(
            self, *, key: str, operation: str,
            owner_intent_revision: int,
            request_profile: Mapping[str, Any],
            outcome: Mapping[str, Any], changed: bool) -> Dict[str, Any]:
        self._ensure_operations_locked()
        receipt = {
            "version": _OPERATION_VERSION,
            "quest_id": self.quest_id,
            "idempotency_key": key,
            "operation": operation,
            "owner_intent_revision": owner_intent_revision,
            "request_profile": copy.deepcopy(dict(request_profile)),
            "outcome": copy.deepcopy(dict(outcome)),
            "changed": changed,
            "schedule_required": changed,
            "status": "pending" if changed else "not-required",
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
        }
        _write_new(self.operations_dir / f"{key}.json", _canonical(receipt))
        _fsync_dir(self.operations_dir)
        return self._operation_public(receipt)

    def _recover_operation_locked(
            self, records: List[Tuple[Dict[str, Any], str]], *, key: str,
            profile: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        operations = self._scan_operations_locked(records)
        existing = operations.get(key)
        if existing is not None:
            if (existing["operation"] != "update"
                    or existing["request_profile"] != profile):
                raise RuntimeProfileConflictError(
                    f"idempotency_key {key} 已绑定不同 runtime profile 操作")
            return existing
        ledger_matches = [
            (row, digest) for row, digest in records
            if row["idempotency_key"] == key
        ]
        if not ledger_matches:
            return None
        row, digest = ledger_matches[0]
        if row["operation"] != "update" or row["profile"] != profile:
            raise RuntimeProfileConflictError(
                f"idempotency_key {key} 已绑定不同 runtime profile 操作")
        outcome = self._public(
            quest_id=self.quest_id, revision=row["revision"],
            profile=row["profile"], record_sha256=digest, source="ledger")
        # Ledger fsync precedes receipt creation.  Reaching this branch is the
        # intentional crash-gap recovery path; the immutable receipt restores
        # the pending side effect without another revision.
        self._write_operation_receipt_locked(
            key=key, operation="update", request_profile=profile,
            owner_intent_revision=row["owner_intent_revision"],
            outcome=outcome, changed=True)
        return self._scan_operations_locked(records)[key]

    def runtime_update_operation(
            self, profile: object,
            idempotency_key: object) -> Optional[Dict[str, Any]]:
        """Read/recover an existing update operation without creating a change."""
        normalized = normalize_profile(profile)
        key = _validate_key(idempotency_key)
        if not os.path.lexists(self.root):
            return None
        with self._locked(create=False):
            records = self._scan_locked()
            return self._recover_operation_locked(
                records, key=key, profile=normalized)

    def begin_runtime_update(
            self, profile: object, idempotency_key: object) -> Dict[str, Any]:
        """Durably bind one request to a no-op or one newly appended revision."""
        normalized = normalize_profile(profile)
        key = _validate_key(idempotency_key)
        with self._locked(create=True):
            records = self._scan_locked()
            owner_intents = self._scan_owner_intents_locked(records)
            recovered = self._recover_operation_locked(
                records, key=key, profile=normalized)
            if recovered is not None:
                return recovered
            if len(self._scan_operations_locked(records)) >= _MAX_OPERATIONS:
                raise RuntimeProfileConflictError(
                    "runtime profile operation 数已达安全上限")
            if records:
                last, last_digest = records[-1]
                previous = self._public(
                    quest_id=self.quest_id, revision=last["revision"],
                    profile=last["profile"], record_sha256=last_digest,
                    source="ledger")
            else:
                previous = self._legacy()
            if previous["profile"] == normalized:
                return self._write_operation_receipt_locked(
                    key=key, operation="update", request_profile=normalized,
                    owner_intent_revision=(
                        0 if not owner_intents
                        else owner_intents[-1][0]["revision"]),
                    outcome=previous, changed=False)
            if len(records) >= _MAX_REVISIONS:
                raise RuntimeProfileConflictError(
                    "runtime profile revision 数已达安全上限")
            revision = len(records) + 1
            previous_digest = None if not records else records[-1][1]
            row = {
                "version": _RECORD_VERSION,
                "quest_id": self.quest_id,
                "revision": revision,
                "operation": "update",
                "owner_intent_revision": (
                    0 if not owner_intents
                    else owner_intents[-1][0]["revision"]),
                "idempotency_key": key,
                "profile": normalized,
                "previous_sha256": previous_digest,
                "recorded_at": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z"),
            }
            raw = _canonical(row)
            _write_new(
                self.revisions_dir / f"{revision:020d}.json", raw)
            _fsync_dir(self.revisions_dir)
            digest = _digest(raw)
            outcome = self._public(
                quest_id=self.quest_id, revision=revision,
                profile=normalized, record_sha256=digest, source="ledger")
            self._write_operation_receipt_locked(
                key=key, operation="update", request_profile=normalized,
                owner_intent_revision=row["owner_intent_revision"],
                outcome=outcome, changed=True)
            records.append((row, digest))
            return self._scan_operations_locked(records, owner_intents)[key]

    def runtime_update_operation_by_key(
            self, idempotency_key: object) -> Optional[Dict[str, Any]]:
        """Return one strictly validated runtime-update receipt by key."""
        key = _validate_key(idempotency_key)
        if not os.path.lexists(self.root):
            return None
        with self._locked(create=False):
            records = self._scan_locked()
            operation = self._scan_operations_locked(records).get(key)
            if operation is None:
                return None
            if operation["operation"] != "update":
                raise RuntimeProfileConflictError(
                    "idempotency_key 已绑定 initialize 操作")
            return operation

    def _write_operation_completion_locked(
            self, *, key: str, status: str) -> None:
        receipt_digest = _digest(_read_regular_payload(
            self.operations_dir / f"{key}.json", owner=self.owner,
            label=f"runtime-settings operation {key}"))
        completion = {
            "version": _TRANSITION_VERSION,
            "quest_id": self.quest_id,
            "idempotency_key": key,
            "receipt_sha256": receipt_digest,
            "status": status,
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
        }
        _write_new(
            self.operations_dir / f"{key}.completion.json",
            _canonical(completion))
        _fsync_dir(self.operations_dir)

    def _write_operation_settlement_locked(
            self, *, key: str, status: str) -> None:
        receipt_digest = _digest(_read_regular_payload(
            self.operations_dir / f"{key}.json", owner=self.owner,
            label=f"runtime-settings operation {key}"))
        completion_path = self.operations_dir / f"{key}.completion.json"
        acceptance_digest = (
            _digest(_read_regular_payload(
                completion_path, owner=self.owner,
                label=f"runtime-settings operation completion {key}"))
            if os.path.lexists(completion_path) else None)
        settlement = {
            "version": _TRANSITION_VERSION,
            "quest_id": self.quest_id,
            "idempotency_key": key,
            "receipt_sha256": receipt_digest,
            "acceptance_sha256": acceptance_digest,
            "status": status,
            "recorded_at": datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
        }
        _write_new(
            self.operations_dir / f"{key}.settlement.json",
            _canonical(settlement))
        _fsync_dir(self.operations_dir)

    def accept_runtime_update(
            self, idempotency_key: object) -> Dict[str, Any]:
        """Fsync restart intent before invoking the asynchronous scheduler."""
        key = _validate_key(idempotency_key)
        if not os.path.lexists(self.root):
            raise RuntimeSettingsCorruptError(
                "runtime update operation receipt 不存在")
        with self._locked(create=False):
            records = self._scan_locked()
            operation = self._scan_operations_locked(records).get(key)
            if operation is None or operation["operation"] != "update":
                raise RuntimeSettingsCorruptError(
                    "runtime update operation receipt 不存在")
            if not operation["changed"]:
                return operation
            if operation["status"] == "pending":
                self._write_operation_completion_locked(
                    key=key, status="accepted")
                return {**operation, "status": "accepted"}
            return operation

    def settle_runtime_update(
            self, idempotency_key: object, status: object) -> Dict[str, Any]:
        """Append a terminal applied/not-required/terminated disposition."""
        key = _validate_key(idempotency_key)
        if status not in {"applied", "not-required", "terminated"}:
            raise ValueError("runtime update settlement status 非法")
        if not os.path.lexists(self.root):
            raise RuntimeSettingsCorruptError(
                "runtime update operation receipt 不存在")
        with self._locked(create=False):
            records = self._scan_locked()
            operation = self._scan_operations_locked(records).get(key)
            if operation is None or operation["operation"] != "update":
                raise RuntimeSettingsCorruptError(
                    "runtime update operation receipt 不存在")
            if not operation["changed"]:
                if status != "not-required":
                    raise RuntimeProfileConflictError(
                        "no-op runtime update 不得标记为变更终态")
                return operation
            if operation["status"] in {
                    "applied", "not-required", "terminated"}:
                if operation["status"] != status:
                    raise RuntimeProfileConflictError(
                        "runtime update settlement status 已绑定不同结果")
                return operation
            self._write_operation_settlement_locked(key=key, status=status)
            return {**operation, "status": status}

    def complete_runtime_update(
            self, idempotency_key: object, status: object) -> Dict[str, Any]:
        """Backward-compatible transition helper for service callers."""
        if status in {"scheduled", "accepted"}:
            return self.accept_runtime_update(idempotency_key)
        if status == "not-required":
            return self.settle_runtime_update(idempotency_key, status)
        raise ValueError("runtime update completion status 非法")

    def settle_applied_runtime_updates(
            self, applied_record: Mapping[str, Any]) -> int:
        """Settle every pending/accepted update superseded by this applied record."""
        revision, digest, supplied_profile = self._public_identity(applied_record)
        if applied_record.get("quest_id") != self.quest_id:
            raise ValueError("applied runtime profile quest_id 不匹配")
        with self._locked(create=False):
            records = self._scan_locked()
            expected = self._record_from_records(records, revision, digest)
            if (expected["profile"] != supplied_profile
                    or expected["source"] != applied_record.get("source")):
                raise RuntimeProfileConflictError(
                    "applied runtime profile identity 不匹配")
            operations = self._scan_operations_locked(records)
            keys = [
                key for key, operation in operations.items()
                if (operation["operation"] == "update"
                    and operation["changed"]
                    and operation["status"] in {"pending", "accepted"}
                    and operation["outcome"]["revision"] <= expected["revision"])
            ]
            for key in keys:
                self._write_operation_settlement_locked(
                    key=key, status="applied")
            return len(keys)

    def terminate_runtime_updates(self) -> int:
        """Durably cancel every unfinished restart intent for explicit stop."""
        if not os.path.lexists(self.root):
            return 0
        with self._locked(create=False):
            records = self._scan_locked()
            operations = self._scan_operations_locked(records)
            keys = [
                key for key, operation in operations.items()
                if (operation["operation"] == "update"
                    and operation["changed"]
                    and operation["status"] in {"pending", "accepted"})
            ]
            for key in keys:
                self._write_operation_settlement_locked(
                    key=key, status="terminated")
            return len(keys)

    def _read_cycle_binding_locked(
            self, records: List[Tuple[Dict[str, Any], str]]) -> Dict[str, Any]:
        info = _regular(
            self.cycle_binding_path, owner=self.owner,
            label="runtime-settings cycle binding", mode=0o600)
        if not 2 <= info.st_size <= _MAX_RECORD_BYTES:
            raise RuntimeSettingsCorruptError(
                "runtime-settings cycle binding 大小非法")
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(self.cycle_binding_path, flags)
        try:
            before = os.fstat(fd)
            raw = os.read(fd, _MAX_RECORD_BYTES + 1)
            after = os.fstat(fd)
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns, before.st_ctime_ns, before.st_mode,
                        before.st_uid, before.st_nlink)):
                raise RuntimeSettingsCorruptError(
                    "runtime-settings cycle binding 读取期间身份漂移")
        finally:
            os.close(fd)
        if len(raw) != info.st_size:
            raise RuntimeSettingsCorruptError(
                "runtime-settings cycle binding 被截断")
        binding = _strict_json(raw, label="runtime-settings cycle binding")
        if set(binding) != {
                "version", "quest_id", "revision", "record_sha256", "profile"}:
            raise RuntimeSettingsCorruptError(
                "runtime-settings cycle binding 字段闭包非法")
        try:
            expected = self._record_from_records(
                records, binding.get("revision"), binding.get("record_sha256"))
            profile = normalize_profile(binding.get("profile"))
        except (ValueError, RuntimeProfileConflictError) as error:
            raise RuntimeSettingsCorruptError(
                "runtime-settings cycle binding ledger identity 非法") from error
        if (binding.get("version") != _CYCLE_BINDING_VERSION
                or binding.get("quest_id") != self.quest_id
                or binding["profile"] != profile
                or profile != expected["profile"]):
            raise RuntimeSettingsCorruptError(
                "runtime-settings cycle binding 身份/profile 非法")
        return expected

    def record(self, revision: int,
               record_sha256: Optional[str]) -> Dict[str, Any]:
        """Resolve one exact ledger/legacy identity to the public record shape."""
        with self._locked(create=False):
            records = self._scan_locked()
            return self._record_from_records(records, revision, record_sha256)

    def bound_cycle_profile(self) -> Optional[Dict[str, Any]]:
        """Return the exact profile captured by the inflight cycle, if any."""
        if not os.path.lexists(self.root):
            return None
        with self._locked(create=False):
            records = self._scan_locked()
            if not os.path.lexists(self.cycle_binding_path):
                return None
            return self._read_cycle_binding_locked(records)

    def bind_cycle_profile(self, applied_record: Mapping[str, Any]) -> Dict[str, Any]:
        """Create, or idempotently replay, one exact inflight-cycle binding."""
        revision, digest, supplied_profile = self._public_identity(applied_record)
        if applied_record.get("quest_id") != self.quest_id:
            raise ValueError("cycle binding quest_id 不匹配")
        with self._locked(create=True):
            records = self._scan_locked()
            expected = self._record_from_records(records, revision, digest)
            if (expected["profile"] != supplied_profile
                    or expected["source"] != applied_record.get("source")):
                raise RuntimeProfileConflictError(
                    "cycle binding public record identity 不匹配")
            if os.path.lexists(self.cycle_binding_path):
                bound = self._read_cycle_binding_locked(records)
                if bound != expected:
                    raise RuntimeProfileConflictError(
                        "已有不同 runtime profile 绑定到当前 cycle")
                return bound
            payload = _canonical({
                "version": _CYCLE_BINDING_VERSION,
                "quest_id": self.quest_id,
                "revision": expected["revision"],
                "record_sha256": expected["record_sha256"],
                "profile": expected["profile"],
            })
            _write_new(self.cycle_binding_path, payload)
            _fsync_dir(self.root)
            return expected

    def clear_cycle_profile(self, applied_record: Mapping[str, Any]) -> bool:
        """Clear only the binding whose exact public identity the caller holds."""
        revision, digest, supplied_profile = self._public_identity(applied_record)
        if applied_record.get("quest_id") != self.quest_id:
            raise ValueError("cycle binding quest_id 不匹配")
        if not os.path.lexists(self.root):
            return False
        with self._locked(create=False):
            records = self._scan_locked()
            expected = self._record_from_records(records, revision, digest)
            if (expected["profile"] != supplied_profile
                    or expected["source"] != applied_record.get("source")):
                raise RuntimeProfileConflictError(
                    "cycle binding public record identity 不匹配")
            if not os.path.lexists(self.cycle_binding_path):
                return False
            bound = self._read_cycle_binding_locked(records)
            if bound != expected:
                raise RuntimeProfileConflictError(
                    "cycle binding clear identity 不匹配")
            try:
                os.unlink(self.cycle_binding_path)
                _fsync_dir(self.root)
            except OSError as error:
                raise RuntimeSettingsCorruptError(
                    "runtime-settings cycle binding 无法清除") from error
            return True

    def current(self) -> Dict[str, Any]:
        if not os.path.lexists(self.root):
            return self._legacy()
        with self._locked(create=False):
            records = self._scan_locked()
            if not records:
                return self._legacy()
            record, digest = records[-1]
            return self._public(
                quest_id=self.quest_id, revision=record["revision"],
                profile=record["profile"], record_sha256=digest,
                source="ledger")

    def history(self) -> List[Dict[str, Any]]:
        if not os.path.lexists(self.root):
            return []
        with self._locked(create=False):
            return [
                {**copy.deepcopy(record), "record_sha256": digest}
                for record, digest in self._scan_locked()
            ]

    def _append(self, *, operation: str, profile: object,
                idempotency_key: object) -> Dict[str, Any]:
        normalized = normalize_profile(profile)
        key = _validate_key(idempotency_key)
        with self._locked(create=True):
            records = self._scan_locked()
            owner_intents = self._scan_owner_intents_locked(records)
            operations = self._scan_operations_locked(records, owner_intents)
            operation_receipt = operations.get(key)
            if operation_receipt is not None:
                if (operation_receipt["operation"] != operation
                        or operation_receipt["request_profile"] != normalized):
                    raise RuntimeProfileConflictError(
                        f"idempotency_key {key} 已绑定不同 runtime profile 操作")
                return copy.deepcopy(operation_receipt["outcome"])
            for record, digest in records:
                if record["idempotency_key"] != key:
                    continue
                if (record["operation"] != operation
                        or record["profile"] != normalized):
                    raise RuntimeProfileConflictError(
                        f"idempotency_key {key} 已绑定不同 runtime profile 操作")
                return self._public(
                    quest_id=self.quest_id, revision=record["revision"],
                    profile=record["profile"], record_sha256=digest,
                    source="ledger")
            if operation == "initialize" and records:
                current_record, current_digest = records[-1]
                if current_record["profile"] != normalized:
                    raise RuntimeProfileConflictError(
                        "runtime profile 已初始化为不同值")
                outcome = self._public(
                    quest_id=self.quest_id,
                    revision=current_record["revision"],
                    profile=current_record["profile"],
                    record_sha256=current_digest, source="ledger")
                if len(operations) >= _MAX_OPERATIONS:
                    raise RuntimeProfileConflictError(
                        "runtime profile operation 数已达安全上限")
                return self._write_operation_receipt_locked(
                    key=key, operation="initialize",
                    owner_intent_revision=(
                        0 if not owner_intents
                        else owner_intents[-1][0]["revision"]),
                    request_profile=normalized, outcome=outcome,
                    changed=False)["outcome"]
            if len(records) >= _MAX_REVISIONS:
                raise RuntimeProfileConflictError(
                    "runtime profile revision 数已达安全上限")
            revision = len(records) + 1
            previous = None if not records else records[-1][1]
            record = {
                "version": _RECORD_VERSION,
                "quest_id": self.quest_id,
                "revision": revision,
                "operation": operation,
                "owner_intent_revision": (
                    0 if not owner_intents
                    else owner_intents[-1][0]["revision"]),
                "idempotency_key": key,
                "profile": normalized,
                "previous_sha256": previous,
                "recorded_at": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z"),
            }
            raw = _canonical(record)
            path = self.revisions_dir / f"{revision:020d}.json"
            _write_new(path, raw)
            _fsync_dir(self.revisions_dir)
            digest = _digest(raw)
            return self._public(
                quest_id=self.quest_id, revision=revision,
                profile=normalized, record_sha256=digest, source="ledger")

    def initialize(self, profile: object,
                   idempotency_key: object) -> Dict[str, Any]:
        return self._append(
            operation="initialize", profile=profile,
            idempotency_key=idempotency_key)

    def update(self, profile: object,
               idempotency_key: object) -> Dict[str, Any]:
        return self._append(
            operation="update", profile=profile,
            idempotency_key=idempotency_key)


__all__ = [
    "DEFAULT_PROFILE",
    "EXACT_MULTI_GPU_PROFILE_VERSION",
    "PROFILE_VERSION",
    "QuestRuntimeSettings",
    "RuntimeProfileConflictError",
    "RuntimeSettingsCorruptError",
    "normalize_profile",
    "public_options",
]
