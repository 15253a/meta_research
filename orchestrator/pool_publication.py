"""Atomic publication of bundle artifacts into the durable research pool.

This module owns the file half of the DB + filesystem seam described in the
architecture.  It deliberately does *not* make a baseline or variant legal.
The safe order is::

    publish_training()                         # files first
    INSERT checkpoint(path=<pool path>, hash)  # never a cN/tN staging path
    bind_training_database(conn, ...)          # same checkpoint INSERT txn
    ... evaluate / review ...
    publish_evaluation()                       # files + content-address manifest
    register evaluation + formal execution_log
    bind_database(conn, verified_publication)  # inside gate_register_* txn
    UPDATE baseline/variant SET status='legal'

Filesystem and SQLite cannot share one atomic transaction.  Publishing first
is therefore intentional: a crash can leave unreferenced immutable files, and
an identical replay adopts them.  It can never leave a ``legal`` DB object
whose formal files were only partly copied.

The frozen schema has no generic asset table.  Existing columns are used as
designed instead:

* ``baseline.code_ref`` + ``baseline.commit_hash`` bind the formal source tree;
* ``checkpoint.path`` + ``checkpoint.content_hash`` bind every checkpoint;
* a content-addressed pool manifest is anchored by an append-only
  ``decision(type='pool_publication')`` and the canonical attempt's
  ``transcript_ref``;
* ``execution_log.ref/content_hash`` binds the formal evaluation artifact;
* baseline/variant/protocol cards materialize long-term recall.

No runtime database is opened or migrated here.  ``bind_database`` accepts the
already-owned writer transaction from :class:`WriteDaemon`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


TRAINING_SCHEMA = "meta-research-pool-training/v1"
PUBLICATION_SCHEMA = "meta-research-pool-publication/v1"
DB_BINDING_SCHEMA = "meta-research-pool-db-binding/v1"
TRAINING_DB_BINDING_SCHEMA = "meta-research-pool-training-db-binding/v1"
TREE_HASH_ALG = "sha256-tree-v1"
_MANIFEST_DIR = PurePosixPath("pool/manifests")
_STAGING_DIR = ".pool-staging"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COPY_BLOCK = 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class PoolPublicationError(RuntimeError):
    """A staged or published pool asset violates the publication contract."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PoolPublicationError("pool manifest 含不可规范化 JSON 值") from error


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _component(value: str, *, label: str) -> str:
    """Return a deterministic, path-safe component without silently changing identity."""
    if not isinstance(value, str) or not _SAFE_COMPONENT_RE.fullmatch(value):
        raise PoolPublicationError(
            f"{label} 不是安全路径组件（仅 ASCII 字母数字及 ._-，最长 128）: {value!r}")
    if value in (".", ".."):
        raise PoolPublicationError(f"{label} 非法: {value!r}")
    return value


def _baseline_directory(slug: str, canonical_key: str) -> str:
    if not isinstance(slug, str) or not slug:
        raise PoolPublicationError("baseline.slug 为空")
    try:
        slug_bytes = slug.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PoolPublicationError("baseline.slug 不是合法 UTF-8") from error
    if len(slug_bytes) > 4096:
        raise PoolPublicationError("baseline.slug 超过 4096 bytes")
    # Import identities are NFKC-normalised but may legitimately retain
    # non-ASCII letters.  The DB value remains the authority; unsafe display
    # labels get a deterministic digest component instead of becoming paths.
    safe_slug = (slug if _SAFE_COMPONENT_RE.fullmatch(slug)
                 else "baseline-" + hashlib.sha256(slug_bytes).hexdigest())
    if not isinstance(canonical_key, str) or not canonical_key:
        raise PoolPublicationError("baseline.canonical_key 为空")
    try:
        key_bytes = canonical_key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PoolPublicationError("baseline.canonical_key 不是合法 UTF-8") from error
    if len(key_bytes) > 64 * 1024:
        raise PoolPublicationError("baseline.canonical_key 超过 64KiB")
    suffix = hashlib.sha256(key_bytes).hexdigest()[:8]
    return f"{safe_slug}-{suffix}"


def _checked_id(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PoolPublicationError(f"{label} 须为正整数")
    return value


def _normalize_hash(value: Optional[str], *, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PoolPublicationError(f"{label} 非字符串")
    normalized = value.removeprefix("sha256:").lower()
    if not _HASH_RE.fullmatch(normalized):
        raise PoolPublicationError(f"{label} 非 sha256: {value!r}")
    return normalized


def _scope_json(value: Mapping[str, Any] | str) -> Tuple[Dict[str, Any], str]:
    try:
        parsed = (json.loads(value, parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite {token}"))) if isinstance(value, str) else dict(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PoolPublicationError("protocol.scope_spec 不是合法 JSON object") from error
    if not isinstance(parsed, dict):
        raise PoolPublicationError("protocol.scope_spec 须为 JSON object")
    raw = _canonical(parsed).decode("utf-8").rstrip("\n")
    return parsed, raw


def _config_json(value: Mapping[str, Any] | str) -> Tuple[Any, bytes]:
    try:
        parsed = (json.loads(value, parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite {token}"))) if isinstance(value, str) else dict(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PoolPublicationError("variant.config 不是合法 JSON") from error
    if not isinstance(parsed, dict):
        raise PoolPublicationError("variant.config 须为 JSON object")
    return parsed, _canonical(parsed)


@dataclass(frozen=True)
class BaselinePublication:
    baseline_id: int
    slug: str
    canonical_key: str
    identity_source: Optional[Path] = None
    code_source: Optional[Path] = None
    repro_cmd_md: Optional[str] = None

    @property
    def publishes_identity(self) -> bool:
        fields = (self.identity_source, self.code_source, self.repro_cmd_md)
        if all(value is None for value in fields):
            return False
        if any(value is None for value in fields):
            raise PoolPublicationError(
                "新 baseline 发布须同时给 identity_source/code_source/repro_cmd_md")
        return True


@dataclass(frozen=True)
class VariantPublication:
    variant_id: int
    variant_key: str
    config: Mapping[str, Any] | str
    overrides_source: Optional[Path] = None


@dataclass(frozen=True)
class CheckpointPublication:
    ckpt_key: str
    source: Path
    # May be unknown until the formal file has been published and INSERT returns
    # its row id.  publish_evaluation then seals a ckpt_key -> id mapping.
    checkpoint_id: Optional[int] = None
    expected_sha256: Optional[str] = None
    file_name: Optional[str] = None


@dataclass(frozen=True)
class TrainingPublicationSpec:
    baseline: BaselinePublication
    variant: VariantPublication
    checkpoints: Sequence[CheckpointPublication]


@dataclass(frozen=True)
class ProtocolPublication:
    protocol_id: int
    version: int
    name: str
    scope_spec: Mapping[str, Any] | str


@dataclass(frozen=True)
class EvaluationPublicationSpec:
    training: "VerifiedTrainingPublication"
    evaluation_id: int
    eval_key: str
    attempt_id: int
    attempt_no: int
    results_source: Path
    primary_artifact: str
    metrics: Sequence[Mapping[str, Any]]
    protocol: ProtocolPublication
    transcript_source: Optional[Path] = None
    checkpoint_ids: Optional[Mapping[str, int]] = None


@dataclass(frozen=True)
class VerifiedTrainingPublication:
    work_root: Path
    manifest_ref: str
    manifest_hash: str
    payload: Mapping[str, Any]

    @property
    def checkpoint_bindings(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.payload["objects"]["checkpoints"])


@dataclass(frozen=True)
class VerifiedPoolPublication:
    work_root: Path
    manifest_ref: str
    manifest_hash: str
    payload: Mapping[str, Any]
    training: VerifiedTrainingPublication

    @property
    def database_bindings(self) -> Mapping[str, Any]:
        return self.payload["db_bindings"]


def _lstat_directory(path: Path, *, create: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise PoolPublicationError(f"目录不存在: {path}")
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PoolPublicationError(f"目录类型非法/为 symlink: {path}")


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(path: Path) -> None:
    """Durably flush a private staged tree before its root rename becomes visible."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise PoolPublicationError(f"staging tree 含 symlink: {path}")
    if stat.S_ISREG(info.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise PoolPublicationError(f"staging tree 含非常规对象: {path}")
    with os.scandir(path) as iterator:
        names = sorted(entry.name for entry in iterator)
    for name in names:
        _sync_tree(path / name)
    _sync_directory(path)


def _safe_remove_tree(path: Path) -> None:
    """Remove only a publisher-created private tree; never follow symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode):
        path.unlink()
        return
    with os.scandir(path) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        _safe_remove_tree(path / name)
    path.rmdir()


def _read_regular(path: Path, *, maximum: Optional[int] = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise PoolPublicationError(f"无法安全打开文件: {path}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PoolPublicationError(f"资产不是常规文件: {path}")
        if maximum is not None and before.st_size > maximum:
            raise PoolPublicationError(f"资产超过大小上限 {maximum}: {path}")
        data = bytearray()
        while len(data) < before.st_size:
            block = os.read(fd, min(_COPY_BLOCK, before.st_size - len(data)))
            if not block:
                raise PoolPublicationError(f"读取资产提前 EOF: {path}")
            data.extend(block)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise PoolPublicationError(f"读取期间资产身份漂移: {path}")
        return bytes(data)
    finally:
        os.close(fd)


def _write_new_file(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)


def _copy_file(source: Path, destination: Path) -> Dict[str, Any]:
    raw = _read_regular(source)
    _write_new_file(destination, raw)
    return {"kind": "file", "sha256": _sha256(raw), "bytes": len(raw), "files": 1}


def _tree_inventory(path: Path) -> Tuple[list[Dict[str, Any]], int]:
    """Hash a tree without following links; empty directories are significant."""
    try:
        root_info = path.lstat()
    except FileNotFoundError as error:
        raise PoolPublicationError(f"资产路径不存在: {path}") from error
    if stat.S_ISLNK(root_info.st_mode):
        raise PoolPublicationError(f"资产路径不得为 symlink: {path}")
    if stat.S_ISREG(root_info.st_mode):
        raw = _read_regular(path)
        return ([{"path": ".", "type": "file", "sha256": _sha256(raw),
                  "bytes": len(raw)}], len(raw))
    if not stat.S_ISDIR(root_info.st_mode):
        raise PoolPublicationError(f"资产路径类型非法: {path}")
    records: list[Dict[str, Any]] = []
    total = 0

    def visit(directory: Path, relative: PurePosixPath) -> None:
        nonlocal total
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        if relative != PurePosixPath(".") and not entries:
            records.append({"path": relative.as_posix(), "type": "dir"})
        for entry in entries:
            child = directory / entry.name
            rel = (PurePosixPath(entry.name) if relative == PurePosixPath(".")
                   else relative / entry.name)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise PoolPublicationError(f"资产树含 symlink: {child}")
            if stat.S_ISDIR(info.st_mode):
                visit(child, rel)
            elif stat.S_ISREG(info.st_mode):
                raw = _read_regular(child)
                records.append({"path": rel.as_posix(), "type": "file",
                                "sha256": _sha256(raw), "bytes": len(raw)})
                total += len(raw)
            else:
                raise PoolPublicationError(f"资产树含非常规文件: {child}")

    visit(path, PurePosixPath("."))
    return records, total


def _path_digest(path: Path) -> Dict[str, Any]:
    info = path.lstat()
    records, total = _tree_inventory(path)
    if stat.S_ISREG(info.st_mode):
        record = records[0]
        return {"kind": "file", "sha256": record["sha256"], "bytes": total, "files": 1}
    return {"kind": "directory", "sha256": _sha256(_canonical(records)),
            "hash_alg": TREE_HASH_ALG, "bytes": total,
            "files": sum(record["type"] == "file" for record in records)}


def _copy_tree(source: Path, destination: Path) -> Dict[str, Any]:
    before = _path_digest(source)
    source_info = source.lstat()
    if stat.S_ISREG(source_info.st_mode):
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_file(source, destination)
    elif stat.S_ISDIR(source_info.st_mode):
        destination.mkdir(mode=0o700)

        def visit(src: Path, dst: Path) -> None:
            with os.scandir(src) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                src_child, dst_child = src / entry.name, dst / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise PoolPublicationError(f"资产树含 symlink: {src_child}")
                if stat.S_ISDIR(info.st_mode):
                    dst_child.mkdir(mode=0o700)
                    visit(src_child, dst_child)
                    _sync_directory(dst_child)
                elif stat.S_ISREG(info.st_mode):
                    _copy_file(src_child, dst_child)
                else:
                    raise PoolPublicationError(f"资产树含非常规文件: {src_child}")
        visit(source, destination)
        _sync_directory(destination)
    else:
        raise PoolPublicationError(f"资产路径类型非法: {source}")
    after = _path_digest(source)
    copied = _path_digest(destination)
    if before != after or copied != before:
        raise PoolPublicationError(f"复制期间资产内容漂移: {source}")
    return copied


def _atomic_publish_file(path: Path, raw: bytes) -> None:
    """Publish an immutable content-addressed file, accepting only exact replay."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        existing = None
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PoolPublicationError(f"manifest 目标类型非法/为 symlink: {path}")
        existing = _read_regular(path, maximum=max(len(raw), 1))
    if existing is not None:
        if existing != raw:
            raise PoolPublicationError(f"内容寻址 manifest 碰撞: {path}")
        return
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        _write_new_file(temporary, raw, mode=0o400)
        try:
            # ``rename(temp, path)`` is atomic but not exclusive: another
            # publisher can create ``path`` after lstat and be silently
            # overwritten.  A same-directory hard-link is an atomic
            # create-if-absent operation.  Exact concurrent replay adopts the
            # winner; conflicting bytes are rejected without replacement.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PoolPublicationError(f"manifest 目标竞态类型非法: {path}")
            if _read_regular(path, maximum=max(len(raw), 1)) != raw:
                raise PoolPublicationError(f"内容寻址 manifest 竞态碰撞: {path}")
        else:
            _sync_directory(path.parent)
        temporary.unlink()
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _relative_ref(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise PoolPublicationError(f"正式资产越出 work_root: {path}") from error
    return PurePosixPath(*relative.parts).as_posix()


def _resolve_ref(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference:
        raise PoolPublicationError("pool manifest 路径引用为空")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise PoolPublicationError(f"pool manifest 路径越界: {reference!r}")
    current = root
    for part in pure.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise PoolPublicationError(f"pool manifest 引用不存在: {reference}") from error
        if stat.S_ISLNK(info.st_mode):
            raise PoolPublicationError(f"pool manifest 引用经过 symlink: {reference}")
    return current


def _same_digest(path: Path, expected: Mapping[str, Any]) -> bool:
    try:
        got = _path_digest(path)
    except (FileNotFoundError, PoolPublicationError):
        return False
    return all(got.get(key) == expected.get(key)
               for key in ("kind", "sha256", "bytes", "files"))


def _manifest_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PoolPublicationError(f"{label} 须为 JSON object")
    return value


def _manifest_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PoolPublicationError(f"{label} 须为 JSON array")
    return value


def _manifest_ref(value: Any, *, label: str) -> PurePosixPath:
    """Parse one canonical, lexical work-root-relative manifest reference."""
    if not isinstance(value, str) or not value:
        raise PoolPublicationError(f"{label} 路径引用为空")
    pure = PurePosixPath(value)
    if (pure.is_absolute() or value != pure.as_posix()
            or any(part in ("", ".", "..") for part in pure.parts)):
        raise PoolPublicationError(f"{label} 路径引用非安全 canonical relative path: {value!r}")
    return pure


def _exact_manifest_ref(value: Any, expected: PurePosixPath, *, label: str) -> str:
    pure = _manifest_ref(value, label=label)
    if pure != expected:
        raise PoolPublicationError(
            f"{label} 未位于身份派生的 formal namespace: "
            f"{pure.as_posix()!r} != {expected.as_posix()!r}")
    return pure.as_posix()


def _manifest_size(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PoolPublicationError(f"{label} 须为非负整数")
    return value


def _manifest_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise PoolPublicationError(f"{label} 须为小写 sha256")
    return value


def _validate_file_asset(asset: Any, *, path: PurePosixPath,
                         label: str) -> Mapping[str, Any]:
    item = _manifest_mapping(asset, label=label)
    _exact_manifest_ref(item.get("path"), path, label=f"{label}.path")
    _manifest_sha256(item.get("sha256"), label=f"{label}.sha256")
    _manifest_size(item.get("bytes"), label=f"{label}.bytes")
    return item


def _validate_tree_asset(asset: Any, *, path: PurePosixPath,
                         label: str) -> Mapping[str, Any]:
    item = _manifest_mapping(asset, label=label)
    _exact_manifest_ref(item.get("path"), path, label=f"{label}.path")
    _manifest_sha256(item.get("sha256"), label=f"{label}.sha256")
    if item.get("hash_alg") != TREE_HASH_ALG:
        raise PoolPublicationError(f"{label}.hash_alg 非 {TREE_HASH_ALG}")
    _manifest_size(item.get("bytes"), label=f"{label}.bytes")
    _manifest_size(item.get("files"), label=f"{label}.files")
    return item


def _render_identity(source: Path, repro_cmd_md: str) -> Tuple[bytes, str]:
    try:
        text = _read_regular(source, maximum=4 * 1024 * 1024).decode("utf-8")
    except UnicodeDecodeError as error:
        raise PoolPublicationError("baseline identity 不是 UTF-8") from error
    if not text.strip() or not isinstance(repro_cmd_md, str) or not repro_cmd_md.strip():
        raise PoolPublicationError("baseline identity/repro 命令为空")
    # Byte-for-byte match PoolGate.gate_register_baseline's authoritative DB value.
    final = text + "\n\n## 复现命令\n" + repro_cmd_md
    return final.encode("utf-8"), final


def _metric_rows(metrics: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(metrics, start=1):
        if not isinstance(item, Mapping):
            raise PoolPublicationError(f"metric_results[{index}] 非 object")
        try:
            metric_id = _checked_id(item["metric_id"], label=f"metric_results[{index}].metric_id")
            metric_ver = _checked_id(item["metric_ver"], label=f"metric_results[{index}].metric_ver")
            value = float(item["value"])
        except KeyError as error:
            raise PoolPublicationError(f"metric_results[{index}] 缺字段 {error.args[0]}") from error
        if not math.isfinite(value):
            raise PoolPublicationError(f"metric_results[{index}].value 非有限")
        scope = item.get("scope", "aggregate")
        checkpoint_id = item.get("checkpoint_id")
        if scope not in ("fold", "aggregate"):
            raise PoolPublicationError(f"metric_results[{index}].scope 非法")
        if scope == "fold":
            checkpoint_id = _checked_id(
                checkpoint_id, label=f"metric_results[{index}].checkpoint_id")
        elif checkpoint_id is not None:
            raise PoolPublicationError("aggregate metric 不得绑定 checkpoint")
        key = (metric_id, metric_ver, scope, checkpoint_id)
        if key in seen:
            raise PoolPublicationError(f"metric_results 重复: {key}")
        seen.add(key)
        row: Dict[str, Any] = {"metric_id": metric_id, "metric_ver": metric_ver,
                               "value": value, "scope": scope}
        if checkpoint_id is not None:
            row["checkpoint_id"] = checkpoint_id
        normalized.append(row)
    if not normalized:
        raise PoolPublicationError("evaluation publication 须至少一个 metric_result")
    return sorted(normalized, key=lambda row: (
        row["metric_id"], row["metric_ver"], row["scope"], row.get("checkpoint_id", 0)))


class PoolPublisher:
    """Publish immutable pool directories below one owned work root."""

    def __init__(self, work_root: Path | str,
                 owner_guard: Optional[Callable[[], None]] = None):
        self.work_root = Path(work_root).absolute()
        self.owner_guard = owner_guard or (lambda: None)
        self.owner_guard()
        _lstat_directory(self.work_root)
        for name in ("baselines", "protocols", "pool"):
            _lstat_directory(self.work_root / name, create=True)
        _lstat_directory(self.work_root / "pool" / "manifests", create=True)
        _lstat_directory(self.work_root / _STAGING_DIR, create=True)

    def _stage(self) -> Path:
        self.owner_guard()
        path = self.work_root / _STAGING_DIR / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        return path

    def _publish_directory(self, staged: Path, final: Path,
                           expected: Mapping[str, Any]) -> None:
        _lstat_directory(final.parent)
        _sync_tree(staged)
        self.owner_guard()
        try:
            final.lstat()
        except FileNotFoundError:
            os.rename(staged, final)
            _sync_directory(final.parent)
        else:
            if not _same_digest(final, expected):
                raise PoolPublicationError(f"正式池路径已被不同内容占用: {final}")
            _safe_remove_tree(staged)
        if not _same_digest(final, expected):
            raise PoolPublicationError(f"正式池发布后 hash 校验失败: {final}")
        self.owner_guard()

    def _publish_manifest(self, payload: Mapping[str, Any]) -> Tuple[str, str]:
        raw = _canonical(payload)
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise PoolPublicationError("pool manifest 超过大小上限")
        digest = _sha256(raw)
        reference = (_MANIFEST_DIR / f"{digest}.json").as_posix()
        self.owner_guard()
        _atomic_publish_file(self.work_root / reference, raw)
        self.owner_guard()
        return reference, digest

    def publish_training(self, spec: TrainingPublicationSpec) -> VerifiedTrainingPublication:
        baseline, variant = spec.baseline, spec.variant
        bid = _checked_id(baseline.baseline_id, label="baseline_id")
        vid = _checked_id(variant.variant_id, label="variant_id")
        variant_key = _component(variant.variant_key, label="variant_key")
        baseline_dir = _baseline_directory(baseline.slug, baseline.canonical_key)
        baseline_root = self.work_root / "baselines" / baseline_dir
        config, config_raw = _config_json(variant.config)
        checkpoints = list(spec.checkpoints)
        if not checkpoints:
            raise PoolPublicationError("training publication 须至少一个 checkpoint")
        if len({item.ckpt_key for item in checkpoints}) != len(checkpoints):
            raise PoolPublicationError("checkpoint key 重复")
        provided_checkpoint_ids = [item.checkpoint_id for item in checkpoints
                                   if item.checkpoint_id is not None]
        if len(set(provided_checkpoint_ids)) != len(provided_checkpoint_ids):
            raise PoolPublicationError("checkpoint id 重复")

        mode = "baseline" if baseline.publishes_identity else "variant"
        stage = self._stage()
        try:
            if mode == "baseline":
                final_unit = baseline_root
                unit = stage / "baseline"
                unit.mkdir(mode=0o700)
                identity_raw, identity_doc = _render_identity(
                    Path(baseline.identity_source), str(baseline.repro_cmd_md))
                _write_new_file(unit / "identity.md", identity_raw)
                code_digest = _copy_tree(Path(baseline.code_source), unit / "src")
                variants_parent = unit / "variants"
                variants_parent.mkdir(mode=0o700)
                variant_stage = variants_parent / variant_key
                variant_stage.mkdir(mode=0o700)
            else:
                _lstat_directory(baseline_root)
                # An exec variant may only extend an already materialized baseline, never a
                # status-only legacy row with no formal identity/source tree.
                existing_identity_raw = _read_regular(
                    baseline_root / "identity.md", maximum=4 * 1024 * 1024)
                try:
                    identity_doc = existing_identity_raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise PoolPublicationError(
                        "既有 formal baseline identity.md 不是 UTF-8") from error
                if not identity_doc.strip():
                    raise PoolPublicationError("既有 formal baseline identity.md 为空")
                code_digest = _path_digest(baseline_root / "src")
                if code_digest["kind"] != "directory":
                    raise PoolPublicationError("既有 formal baseline src 不是目录")
                variants_parent = baseline_root / "variants"
                _lstat_directory(variants_parent, create=True)
                final_unit = variants_parent / variant_key
                unit = stage / "variant"
                unit.mkdir(mode=0o700)
                variant_stage = unit

            _write_new_file(variant_stage / "config.json", config_raw)
            config_digest = _path_digest(variant_stage / "config.json")
            overrides_digest = None
            if variant.overrides_source is not None:
                overrides_digest = _copy_tree(
                    Path(variant.overrides_source), variant_stage / "overrides")
            (variant_stage / "checkpoints").mkdir(mode=0o700)
            (variant_stage / "evaluations").mkdir(mode=0o700)

            checkpoint_objects = []
            for item in sorted(checkpoints, key=lambda value: value.ckpt_key):
                cid = (_checked_id(item.checkpoint_id, label="checkpoint_id")
                       if item.checkpoint_id is not None else None)
                key = _component(item.ckpt_key, label="ckpt_key")
                source = Path(item.source)
                info = source.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise PoolPublicationError(
                        f"checkpoint 须为单个常规文件（多 fold 用多个 checkpoint）: {source}")
                file_name = _component(item.file_name or source.name, label="checkpoint.file_name")
                ckpt_dir = variant_stage / "checkpoints" / key
                ckpt_dir.mkdir(mode=0o700)
                digest = _copy_file(source, ckpt_dir / file_name)
                expected = _normalize_hash(item.expected_sha256, label=f"checkpoint {key} expected_sha256")
                if expected is not None and digest["sha256"] != expected:
                    raise PoolPublicationError(
                        f"checkpoint {key} hash 不符: {digest['sha256']} != {expected}")
                final_file = (baseline_root / "variants" / variant_key /
                              "checkpoints" / key / file_name)
                checkpoint_objects.append({
                    "checkpoint_id": cid, "ckpt_key": item.ckpt_key,
                    "path": _relative_ref(final_file, self.work_root),
                    "content_hash": digest["sha256"], "hash_alg": "sha256",
                    "bytes": digest["bytes"],
                })

            unit_digest = _path_digest(unit)
            self._publish_directory(unit, final_unit, unit_digest)
            variant_root = baseline_root / "variants" / variant_key
            objects: Dict[str, Any] = {
                "baseline": {
                    "baseline_id": bid, "slug": baseline.slug,
                    "canonical_key": baseline.canonical_key,
                    "root": _relative_ref(baseline_root, self.work_root),
                },
                "variant": {
                    "variant_id": vid, "variant_key": variant.variant_key,
                    "root": _relative_ref(variant_root, self.work_root),
                    "config": config,
                    "config_asset": {
                        "path": _relative_ref(variant_root / "config.json", self.work_root),
                        "sha256": config_digest["sha256"], "bytes": config_digest["bytes"],
                    },
                },
                "checkpoints": checkpoint_objects,
            }
            if overrides_digest is not None:
                objects["variant"]["overrides"] = {
                    "path": _relative_ref(variant_root / "overrides", self.work_root),
                    "sha256": overrides_digest["sha256"], "hash_alg": TREE_HASH_ALG,
                    "bytes": overrides_digest["bytes"], "files": overrides_digest["files"],
                }
            identity_asset = _path_digest(baseline_root / "identity.md")
            code_asset = _path_digest(baseline_root / "src")
            if code_asset != code_digest:
                raise PoolPublicationError("baseline code 发布后 hash 漂移")
            objects["baseline"].update({
                "identity_doc": identity_doc,
                "identity": {
                    "path": _relative_ref(baseline_root / "identity.md", self.work_root),
                    "sha256": identity_asset["sha256"], "bytes": identity_asset["bytes"],
                },
                "code": {
                    "path": _relative_ref(baseline_root / "src", self.work_root),
                    "sha256": code_asset["sha256"], "hash_alg": TREE_HASH_ALG,
                    "bytes": code_asset["bytes"], "files": code_asset["files"],
                },
            })
            payload = {
                "schema": TRAINING_SCHEMA, "mode": mode, "objects": objects,
                "unit": {"path": _relative_ref(final_unit, self.work_root),
                         "sha256": unit_digest["sha256"], "hash_alg": TREE_HASH_ALG,
                         "bytes": unit_digest["bytes"], "files": unit_digest["files"]},
            }
            manifest_ref, manifest_hash = self._publish_manifest(payload)
            return self.verify_training(manifest_ref, expected_hash=manifest_hash)
        finally:
            _safe_remove_tree(stage)

    def publish_evaluation(self, spec: EvaluationPublicationSpec) -> VerifiedPoolPublication:
        training = self.verify_training(
            spec.training.manifest_ref, expected_hash=spec.training.manifest_hash)
        objects = training.payload["objects"]
        checkpoint_objects = _resolve_checkpoint_ids(
            objects["checkpoints"], spec.checkpoint_ids)
        variant_root = _resolve_ref(self.work_root, objects["variant"]["root"])
        evaluations_root = variant_root / "evaluations"
        _lstat_directory(evaluations_root, create=True)
        eval_key = _component(spec.eval_key, label="evaluation.eval_key")
        eid = _checked_id(spec.evaluation_id, label="evaluation_id")
        aid = _checked_id(spec.attempt_id, label="attempt_id")
        attempt_no = _checked_id(spec.attempt_no, label="attempt_no")
        protocol = spec.protocol
        pid = _checked_id(protocol.protocol_id, label="protocol_id")
        pver = _checked_id(protocol.version, label="protocol.version")
        if (not isinstance(protocol.name, str) or not protocol.name.strip()
                or len(protocol.name.encode("utf-8")) > 4096):
            raise PoolPublicationError("protocol.name 须为有界非空 UTF-8 文本")
        scope, scope_raw = _scope_json(protocol.scope_spec)
        metrics = _metric_rows(spec.metrics)
        result_source = Path(spec.results_source)
        source_info = result_source.lstat()
        if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
            raise PoolPublicationError("evaluation.results_source 须为无 symlink 的目录")
        primary = PurePosixPath(spec.primary_artifact)
        if (primary.is_absolute() or any(part in ("", ".", "..") for part in primary.parts)):
            raise PoolPublicationError("evaluation.primary_artifact 非安全相对路径")

        stage = self._stage()
        try:
            protocol_unit = stage / "protocol"
            protocol_unit.mkdir(mode=0o700)
            spec_md = (f"# {protocol.name} @ {pver}\n\n```json\n{scope_raw}\n```\n").encode("utf-8")
            _write_new_file(protocol_unit / "spec.md", spec_md)
            protocol_digest = _path_digest(protocol_unit)
            # ``protocol.name`` is a display identity and may legitimately contain
            # spaces/non-ASCII.  Filesystem identity comes from the DB primary key,
            # so never reinterpret the display label as a path component.
            protocol_final = self.work_root / "protocols" / f"p{pid}@{pver}"
            self._publish_directory(protocol_unit, protocol_final, protocol_digest)
            spec_asset = _path_digest(protocol_final / "spec.md")

            protocol_ref_payload = {
                "protocol_id": pid, "protocol_ver": pver,
                "path": _relative_ref(protocol_final / "spec.md", self.work_root),
                "sha256": spec_asset["sha256"],
            }
            protocol_ref_raw = _canonical(protocol_ref_payload)
            eval_final = evaluations_root / eval_key
            try:
                eval_info = eval_final.lstat()
            except FileNotFoundError:
                eval_shell = stage / "evaluation-shell"
                eval_shell.mkdir(mode=0o700)
                (eval_shell / "attempts").mkdir(mode=0o700)
                _write_new_file(eval_shell / "protocol.ref", protocol_ref_raw)
                shell_digest = _path_digest(eval_shell)
                self._publish_directory(eval_shell, eval_final, shell_digest)
            else:
                if stat.S_ISLNK(eval_info.st_mode) or not stat.S_ISDIR(eval_info.st_mode):
                    raise PoolPublicationError(f"evaluation 正式目录类型非法: {eval_final}")
                if _read_regular(eval_final / "protocol.ref", maximum=64 * 1024) != protocol_ref_raw:
                    raise PoolPublicationError(
                        f"evaluation {spec.eval_key} 已绑定不同 protocol")
            attempts_final = eval_final / "attempts"
            _lstat_directory(attempts_final)
            attempt_stage = stage / "attempt"
            _copy_tree(result_source, attempt_stage)
            attempt_final = attempts_final / str(attempt_no)
            if (attempt_stage / "metric_results.json").exists():
                raise PoolPublicationError("evaluation 产物占用保留名 metric_results.json")
            _write_new_file(
                attempt_stage / "metric_results.json", _canonical({"metrics": metrics}))
            transcript_asset = None
            if spec.transcript_source is not None:
                transcript_target = attempt_stage / "transcript.receipt"
                transcript_asset = _copy_file(Path(spec.transcript_source), transcript_target)
            primary_path = attempt_stage.joinpath(*primary.parts)
            try:
                primary_asset = _path_digest(primary_path)
            except (FileNotFoundError, PoolPublicationError) as error:
                raise PoolPublicationError(
                    f"evaluation.primary_artifact 不存在/非法: {spec.primary_artifact}") from error
            if primary_asset["kind"] != "file":
                raise PoolPublicationError("evaluation.primary_artifact 须为常规文件")
            attempt_publish_digest = _path_digest(attempt_stage)
            self._publish_directory(
                attempt_stage, attempt_final, attempt_publish_digest)
            attempt_digest = _path_digest(attempt_final)
            primary_final = attempt_final.joinpath(*primary.parts)
            final_primary_asset = _path_digest(primary_final)
            if final_primary_asset != primary_asset:
                raise PoolPublicationError("evaluation primary artifact 发布后漂移")

            evaluation_object: Dict[str, Any] = {
                "evaluation_id": eid, "eval_key": spec.eval_key,
                "root": _relative_ref(eval_final, self.work_root),
                "attempt_id": aid, "attempt_no": attempt_no,
                "attempt": {
                    "path": _relative_ref(attempt_final, self.work_root),
                    "sha256": attempt_digest["sha256"], "hash_alg": TREE_HASH_ALG,
                    "bytes": attempt_digest["bytes"], "files": attempt_digest["files"],
                },
                "primary_artifact": {
                    "path": _relative_ref(primary_final, self.work_root),
                    "sha256": final_primary_asset["sha256"],
                    "bytes": final_primary_asset["bytes"],
                },
                "protocol_ref": {
                    "path": _relative_ref(eval_final / "protocol.ref", self.work_root),
                    "sha256": _sha256(protocol_ref_raw), "bytes": len(protocol_ref_raw),
                },
                "metrics": metrics,
            }
            if transcript_asset is not None:
                evaluation_object["transcript"] = {
                    "path": _relative_ref(attempt_final / "transcript.receipt", self.work_root),
                    "sha256": transcript_asset["sha256"], "bytes": transcript_asset["bytes"],
                }
            payload: Dict[str, Any] = {
                "schema": PUBLICATION_SCHEMA,
                "training_manifest": {"path": training.manifest_ref,
                                      "sha256": training.manifest_hash},
                "objects": {
                    "baseline": objects["baseline"],
                    "variant": objects["variant"],
                    "checkpoints": checkpoint_objects,
                    "protocol": {
                        "protocol_id": pid, "version": pver, "name": protocol.name,
                        "scope_spec": scope,
                        "root": _relative_ref(protocol_final, self.work_root),
                        "spec": {
                            "path": _relative_ref(protocol_final / "spec.md", self.work_root),
                            "sha256": spec_asset["sha256"], "bytes": spec_asset["bytes"],
                        },
                    },
                    "evaluation": evaluation_object,
                },
                "units": [
                    {"path": _relative_ref(protocol_final, self.work_root),
                     "sha256": protocol_digest["sha256"], "hash_alg": TREE_HASH_ALG,
                     "bytes": protocol_digest["bytes"], "files": protocol_digest["files"]},
                    {"path": _relative_ref(attempt_final, self.work_root),
                     "sha256": attempt_publish_digest["sha256"], "hash_alg": TREE_HASH_ALG,
                     "bytes": attempt_publish_digest["bytes"],
                     "files": attempt_publish_digest["files"]},
                ],
            }
            # The manifest path is content addressed, so transcript_ref can carry both path and hash
            # without adding a schema column.  Fill it after hashing by keeping db_bindings outside the
            # hashed payload impossible; instead the binding stores the artifact hash now and callers
            # derive transcript_ref from VerifiedPoolPublication.manifest_ref.
            payload["db_bindings"] = {
                "baseline": {
                    "baseline_id": objects["baseline"]["baseline_id"],
                    "code_ref": objects["baseline"].get("code", {}).get("path"),
                    "commit_hash": ((TREE_HASH_ALG + ":" + objects["baseline"]["code"]["sha256"])
                                    if "code" in objects["baseline"] else None),
                },
                "checkpoints": [
                    {key: checkpoint[key] for key in (
                        "checkpoint_id", "path", "content_hash", "hash_alg")}
                    for checkpoint in checkpoint_objects
                ],
                "evaluation_attempt": {
                    "evaluation_id": eid, "attempt_id": aid,
                    "artifact_ref": "sha256:" + final_primary_asset["sha256"],
                    "execution_log_ref": _relative_ref(primary_final, self.work_root),
                    "execution_log_hash": final_primary_asset["sha256"],
                },
            }
            manifest_ref, manifest_hash = self._publish_manifest(payload)
            return self.verify_publication(manifest_ref, expected_hash=manifest_hash)
        finally:
            _safe_remove_tree(stage)

    def _validate_training_contract(
            self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate identities, namespaces and byte-level identity links.

        A content-addressed JSON file proves only that the JSON itself did not
        change.  It does not prove that a referenced asset belongs to the pool.
        Every path is therefore derived again from the manifest's DB identity;
        arbitrary work-root paths (especially ``questions/.../cycles/...`` and
        ``.pool-staging``) are never accepted as formal assets.
        """
        mode = payload.get("mode")
        if mode not in ("baseline", "variant"):
            raise PoolPublicationError("training manifest mode 非 baseline/variant")
        objects = _manifest_mapping(payload.get("objects"), label="training.objects")
        baseline = _manifest_mapping(objects.get("baseline"), label="training.baseline")
        variant = _manifest_mapping(objects.get("variant"), label="training.variant")
        checkpoints = _manifest_list(
            objects.get("checkpoints"), label="training.checkpoints")
        if not checkpoints:
            raise PoolPublicationError("training manifest 缺 checkpoint")

        _checked_id(baseline.get("baseline_id"), label="training.baseline_id")
        slug = baseline.get("slug")
        canonical_key = baseline.get("canonical_key")
        baseline_root = PurePosixPath("baselines") / _baseline_directory(
            slug, canonical_key)
        _exact_manifest_ref(
            baseline.get("root"), baseline_root, label="training.baseline.root")

        _checked_id(variant.get("variant_id"), label="training.variant_id")
        variant_key = _component(
            variant.get("variant_key"), label="training.variant_key")
        variant_root = baseline_root / "variants" / variant_key
        _exact_manifest_ref(
            variant.get("root"), variant_root, label="training.variant.root")

        config = variant.get("config")
        if not isinstance(config, dict):
            raise PoolPublicationError("training.variant.config 须为 JSON object")
        _parsed_config, config_raw = _config_json(config)
        config_asset = _validate_file_asset(
            variant.get("config_asset"), path=variant_root / "config.json",
            label="training.variant.config_asset")
        config_path = _resolve_ref(self.work_root, config_asset["path"])
        if _read_regular(config_path, maximum=_MAX_MANIFEST_BYTES) != config_raw:
            raise PoolPublicationError(
                "formal config.json 内容与 training.variant.config 身份脱钩")
        if not _same_digest(config_path, {
                "kind": "file", "sha256": config_asset["sha256"],
                "bytes": config_asset["bytes"], "files": 1}):
            raise PoolPublicationError("formal variant config hash 失配")

        overrides = variant.get("overrides")
        if overrides is not None:
            overrides = _validate_tree_asset(
                overrides, path=variant_root / "overrides",
                label="training.variant.overrides")
            got_overrides = _path_digest(_resolve_ref(self.work_root, overrides["path"]))
            if (got_overrides["kind"] != "directory"
                    or got_overrides["sha256"] != overrides["sha256"]
                    or got_overrides["bytes"] != overrides["bytes"]
                    or got_overrides["files"] != overrides["files"]):
                raise PoolPublicationError("formal variant overrides hash 失配")

        identity_doc = baseline.get("identity_doc")
        if not isinstance(identity_doc, str) or not identity_doc.strip():
            raise PoolPublicationError("training.baseline.identity_doc 为空")
        identity = _validate_file_asset(
            baseline.get("identity"), path=baseline_root / "identity.md",
            label="training.baseline.identity")
        identity_path = _resolve_ref(self.work_root, identity["path"])
        identity_raw = _read_regular(identity_path, maximum=_MAX_MANIFEST_BYTES)
        if identity_raw != identity_doc.encode("utf-8"):
            raise PoolPublicationError(
                "formal identity.md 内容与 training.baseline.identity_doc 身份脱钩")
        if not _same_digest(identity_path, {
                "kind": "file", "sha256": identity["sha256"],
                "bytes": identity["bytes"], "files": 1}):
            raise PoolPublicationError("formal baseline identity hash 失配")

        code = _validate_tree_asset(
            baseline.get("code"), path=baseline_root / "src",
            label="training.baseline.code")
        got_code = _path_digest(_resolve_ref(self.work_root, code["path"]))
        if (got_code["kind"] != "directory" or got_code["sha256"] != code["sha256"]
                or got_code["bytes"] != code["bytes"]
                or got_code["files"] != code["files"]):
            raise PoolPublicationError("formal baseline code hash 失配")

        seen_keys: set[str] = set()
        seen_ids: set[int] = set()
        for index, raw_checkpoint in enumerate(checkpoints, start=1):
            checkpoint = _manifest_mapping(
                raw_checkpoint, label=f"training.checkpoints[{index}]")
            key = _component(
                checkpoint.get("ckpt_key"),
                label=f"training.checkpoints[{index}].ckpt_key")
            if key in seen_keys:
                raise PoolPublicationError("training checkpoint key 重复")
            seen_keys.add(key)
            checkpoint_id = checkpoint.get("checkpoint_id")
            if checkpoint_id is not None:
                checkpoint_id = _checked_id(
                    checkpoint_id,
                    label=f"training.checkpoints[{index}].checkpoint_id")
                if checkpoint_id in seen_ids:
                    raise PoolPublicationError("training checkpoint id 重复")
                seen_ids.add(checkpoint_id)
            checkpoint_ref = _manifest_ref(
                checkpoint.get("path"),
                label=f"training.checkpoints[{index}].path")
            checkpoint_parent = variant_root / "checkpoints" / key
            if checkpoint_ref.parent != checkpoint_parent:
                raise PoolPublicationError(
                    f"training checkpoint {key} 未位于身份派生的 formal namespace")
            _component(checkpoint_ref.name, label=f"training checkpoint {key} file_name")
            content_hash = _manifest_sha256(
                checkpoint.get("content_hash"),
                label=f"training checkpoint {key}.content_hash")
            if checkpoint.get("hash_alg") != "sha256":
                raise PoolPublicationError(f"training checkpoint {key}.hash_alg 非 sha256")
            size = _manifest_size(
                checkpoint.get("bytes"), label=f"training checkpoint {key}.bytes")
            checkpoint_path = _resolve_ref(self.work_root, checkpoint_ref.as_posix())
            if not _same_digest(checkpoint_path, {
                    "kind": "file", "sha256": content_hash,
                    "bytes": size, "files": 1}):
                raise PoolPublicationError(
                    f"formal checkpoint hash 失配: {checkpoint_ref.as_posix()}")

        unit_path = baseline_root if mode == "baseline" else variant_root
        # The root is appendable, so its old tree digest cannot be re-hashed
        # after later variants/evaluations arrive.  Its namespace and metadata
        # are still schema-checked; immutable owned assets above are re-hashed.
        _validate_tree_asset(payload.get("unit"), path=unit_path,
                             label="training.unit")
        return objects

    def _validate_publication_contract(
            self, payload: Mapping[str, Any],
            training: VerifiedTrainingPublication) -> Mapping[str, Any]:
        objects = _manifest_mapping(payload.get("objects"), label="publication.objects")
        baseline = _manifest_mapping(objects.get("baseline"), label="publication.baseline")
        variant = _manifest_mapping(objects.get("variant"), label="publication.variant")
        checkpoints = _manifest_list(
            objects.get("checkpoints"), label="publication.checkpoints")
        training_objects = training.payload["objects"]

        # The complete manifest is an extension of one exact training
        # manifest, not a second opportunity to redefine pool identities.
        if baseline != training_objects["baseline"]:
            raise PoolPublicationError(
                "publication baseline 与 training_manifest objects 脱钩")
        if variant != training_objects["variant"]:
            raise PoolPublicationError(
                "publication variant 与 training_manifest objects 脱钩")
        training_checkpoints = training_objects["checkpoints"]
        if len(checkpoints) != len(training_checkpoints):
            raise PoolPublicationError(
                "publication checkpoints 与 training_manifest objects 脱钩")
        checkpoint_ids: set[int] = set()
        for index, (checkpoint, training_checkpoint) in enumerate(
                zip(checkpoints, training_checkpoints), start=1):
            checkpoint = _manifest_mapping(
                checkpoint, label=f"publication.checkpoints[{index}]")
            checkpoint_id = _checked_id(
                checkpoint.get("checkpoint_id"),
                label=f"publication.checkpoints[{index}].checkpoint_id")
            if checkpoint_id in checkpoint_ids:
                raise PoolPublicationError("publication checkpoint id 重复")
            checkpoint_ids.add(checkpoint_id)
            expected = {**training_checkpoint, "checkpoint_id": checkpoint_id}
            stored_id = training_checkpoint.get("checkpoint_id")
            if ((stored_id is not None and stored_id != checkpoint_id)
                    or checkpoint != expected):
                raise PoolPublicationError(
                    "publication checkpoints 与 training_manifest objects 脱钩")

        protocol = _manifest_mapping(objects.get("protocol"), label="publication.protocol")
        protocol_id = _checked_id(
            protocol.get("protocol_id"), label="publication.protocol_id")
        protocol_ver = _checked_id(
            protocol.get("version"), label="publication.protocol.version")
        protocol_name = protocol.get("name")
        if (not isinstance(protocol_name, str) or not protocol_name.strip()
                or len(protocol_name.encode("utf-8")) > 4096):
            raise PoolPublicationError("publication.protocol.name 须为有界非空 UTF-8")
        scope = protocol.get("scope_spec")
        if not isinstance(scope, dict):
            raise PoolPublicationError("publication.protocol.scope_spec 须为 JSON object")
        normalized_scope, scope_raw = _scope_json(scope)
        if normalized_scope != scope:
            raise PoolPublicationError("publication.protocol.scope_spec 非 canonical object")
        protocol_root = PurePosixPath("protocols") / f"p{protocol_id}@{protocol_ver}"
        _exact_manifest_ref(
            protocol.get("root"), protocol_root, label="publication.protocol.root")
        protocol_spec = _validate_file_asset(
            protocol.get("spec"), path=protocol_root / "spec.md",
            label="publication.protocol.spec")
        protocol_spec_raw = (
            f"# {protocol_name} @ {protocol_ver}\n\n```json\n{scope_raw}\n```\n").encode("utf-8")
        protocol_spec_path = _resolve_ref(self.work_root, protocol_spec["path"])
        if _read_regular(
                protocol_spec_path, maximum=_MAX_MANIFEST_BYTES) != protocol_spec_raw:
            raise PoolPublicationError(
                "formal protocol spec.md 内容与 protocol 身份脱钩")
        if not _same_digest(protocol_spec_path, {
                "kind": "file", "sha256": protocol_spec["sha256"],
                "bytes": protocol_spec["bytes"], "files": 1}):
            raise PoolPublicationError("formal protocol spec hash 失配")

        evaluation = _manifest_mapping(
            objects.get("evaluation"), label="publication.evaluation")
        evaluation_id = _checked_id(
            evaluation.get("evaluation_id"), label="publication.evaluation_id")
        attempt_id = _checked_id(
            evaluation.get("attempt_id"), label="publication.attempt_id")
        attempt_no = _checked_id(
            evaluation.get("attempt_no"), label="publication.attempt_no")
        eval_key = _component(
            evaluation.get("eval_key"), label="publication.eval_key")
        variant_root = _manifest_ref(
            variant["root"], label="publication.variant.root")
        evaluation_root = variant_root / "evaluations" / eval_key
        _exact_manifest_ref(
            evaluation.get("root"), evaluation_root,
            label="publication.evaluation.root")
        attempt_root = evaluation_root / "attempts" / str(attempt_no)
        attempt = _validate_tree_asset(
            evaluation.get("attempt"), path=attempt_root,
            label="publication.evaluation.attempt")

        primary = _manifest_mapping(
            evaluation.get("primary_artifact"),
            label="publication.evaluation.primary_artifact")
        primary_ref = _manifest_ref(
            primary.get("path"), label="publication.evaluation.primary_artifact.path")
        if (len(primary_ref.parts) <= len(attempt_root.parts)
                or primary_ref.parts[:len(attempt_root.parts)] != attempt_root.parts):
            raise PoolPublicationError(
                "publication primary_artifact 未位于当前 formal attempt namespace")
        _manifest_sha256(
            primary.get("sha256"), label="publication.evaluation.primary_artifact.sha256")
        _manifest_size(
            primary.get("bytes"), label="publication.evaluation.primary_artifact.bytes")
        primary_path = _resolve_ref(self.work_root, primary_ref.as_posix())
        if not _same_digest(primary_path, {
                "kind": "file", "sha256": primary["sha256"],
                "bytes": primary["bytes"], "files": 1}):
            raise PoolPublicationError("formal evaluation artifact hash 失配")

        metrics_raw = _manifest_list(
            evaluation.get("metrics"), label="publication.evaluation.metrics")
        metrics = _metric_rows(metrics_raw)
        if metrics != metrics_raw:
            raise PoolPublicationError("publication evaluation metrics 非 canonical 排序/结构")
        for metric in metrics:
            if (metric["scope"] == "fold"
                    and metric["checkpoint_id"] not in checkpoint_ids):
                raise PoolPublicationError(
                    "publication fold metric 引用 training manifest 外 checkpoint")
        metric_results_path = _resolve_ref(
            self.work_root, (attempt_root / "metric_results.json").as_posix())
        if _read_regular(metric_results_path, maximum=_MAX_MANIFEST_BYTES) != _canonical(
                {"metrics": metrics}):
            raise PoolPublicationError(
                "formal metric_results.json 与 publication metrics 身份脱钩")

        protocol_ref = _validate_file_asset(
            evaluation.get("protocol_ref"), path=evaluation_root / "protocol.ref",
            label="publication.evaluation.protocol_ref")
        protocol_ref_raw = _canonical({
            "protocol_id": protocol_id, "protocol_ver": protocol_ver,
            "path": protocol_spec["path"], "sha256": protocol_spec["sha256"],
        })
        protocol_ref_path = _resolve_ref(self.work_root, protocol_ref["path"])
        if _read_regular(protocol_ref_path, maximum=64 * 1024) != protocol_ref_raw:
            raise PoolPublicationError(
                "formal protocol.ref 与 publication protocol 身份脱钩")
        if not _same_digest(protocol_ref_path, {
                "kind": "file", "sha256": protocol_ref["sha256"],
                "bytes": protocol_ref["bytes"], "files": 1}):
            raise PoolPublicationError("formal evaluation protocol.ref hash 失配")

        transcript = evaluation.get("transcript")
        if transcript is not None:
            transcript = _validate_file_asset(
                transcript, path=attempt_root / "transcript.receipt",
                label="publication.evaluation.transcript")
            if not _same_digest(_resolve_ref(self.work_root, transcript["path"]), {
                    "kind": "file", "sha256": transcript["sha256"],
                    "bytes": transcript["bytes"], "files": 1}):
                raise PoolPublicationError("formal evaluation transcript hash 失配")

        got_attempt = _path_digest(_resolve_ref(self.work_root, attempt["path"]))
        if (got_attempt["kind"] != "directory"
                or got_attempt["sha256"] != attempt["sha256"]
                or got_attempt["bytes"] != attempt["bytes"]
                or got_attempt["files"] != attempt["files"]):
            raise PoolPublicationError("formal evaluation attempt tree hash 失配")
        got_protocol = _path_digest(self.work_root / protocol_root)
        expected_units = [
            {"path": protocol_root.as_posix(), "sha256": got_protocol["sha256"],
             "hash_alg": TREE_HASH_ALG, "bytes": got_protocol["bytes"],
             "files": got_protocol["files"]},
            {"path": attempt_root.as_posix(), "sha256": got_attempt["sha256"],
             "hash_alg": TREE_HASH_ALG, "bytes": got_attempt["bytes"],
             "files": got_attempt["files"]},
        ]
        if payload.get("units") != expected_units:
            raise PoolPublicationError(
                "publication units 未精确封闭 protocol/attempt formal namespace")

        bindings = _manifest_mapping(payload.get("db_bindings"), label="publication.db_bindings")
        expected_bindings = {
            "baseline": {
                "baseline_id": baseline["baseline_id"],
                "code_ref": baseline["code"]["path"],
                "commit_hash": TREE_HASH_ALG + ":" + baseline["code"]["sha256"],
            },
            "checkpoints": [
                {key: checkpoint[key] for key in (
                    "checkpoint_id", "path", "content_hash", "hash_alg")}
                for checkpoint in checkpoints
            ],
            "evaluation_attempt": {
                "evaluation_id": evaluation_id, "attempt_id": attempt_id,
                "artifact_ref": "sha256:" + primary["sha256"],
                "execution_log_ref": primary_ref.as_posix(),
                "execution_log_hash": primary["sha256"],
            },
        }
        if bindings != expected_bindings:
            raise PoolPublicationError(
                "publication db_bindings 与已验证 formal objects 身份脱钩")
        return objects

    def _read_manifest(self, reference: str, expected_hash: Optional[str]) -> Tuple[Dict[str, Any], str]:
        # Content addressing is necessary but not sufficient for formal-pool
        # ownership: an identical ``<sha256>.json`` below a cycle staging tree
        # must never become an authoritative transcript ref.  Keep the
        # namespace check lexical and exact before resolving any bytes.
        pure_reference = PurePosixPath(reference) if isinstance(reference, str) else None
        if (pure_reference is None or pure_reference.is_absolute()
                or pure_reference.parent != _MANIFEST_DIR
                or len(pure_reference.parts) != len(_MANIFEST_DIR.parts) + 1):
            raise PoolPublicationError(
                "pool manifest 须位于正式 pool/manifests 内容寻址目录")
        path = _resolve_ref(self.work_root, reference)
        raw = _read_regular(path, maximum=_MAX_MANIFEST_BYTES)
        digest = _sha256(raw)
        normalized = _normalize_hash(expected_hash, label="manifest expected_hash")
        if normalized is not None and digest != normalized:
            raise PoolPublicationError(f"pool manifest hash 不符: {digest} != {normalized}")
        if path.name != f"{digest}.json":
            raise PoolPublicationError("pool manifest 文件名不是内容 hash")
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {token}")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise PoolPublicationError("pool manifest JSON 损坏") from error
        if not isinstance(value, dict) or _canonical(value) != raw:
            raise PoolPublicationError("pool manifest 不是 canonical JSON object")
        return value, digest

    def verify_training(self, reference: str, *,
                        expected_hash: Optional[str] = None) -> VerifiedTrainingPublication:
        payload, digest = self._read_manifest(reference, expected_hash)
        if payload.get("schema") != TRAINING_SCHEMA:
            raise PoolPublicationError("不是 training pool manifest")
        self._validate_training_contract(payload)
        # ``unit`` records what was atomically renamed.  Its root is intentionally appendable:
        # an initial baseline later gains evaluations and sibling variants.  Re-verification
        # therefore checks every immutable owned asset above, not the now-larger container tree.
        return VerifiedTrainingPublication(self.work_root, reference, digest, payload)

    def verify_publication(self, reference: str, *,
                           expected_hash: Optional[str] = None) -> VerifiedPoolPublication:
        payload, digest = self._read_manifest(reference, expected_hash)
        if payload.get("schema") != PUBLICATION_SCHEMA:
            raise PoolPublicationError("不是 complete pool publication manifest")
        training_ref = payload.get("training_manifest")
        if not isinstance(training_ref, dict):
            raise PoolPublicationError("publication 缺 training_manifest")
        training = self.verify_training(
            training_ref.get("path"), expected_hash=training_ref.get("sha256"))
        self._validate_publication_contract(payload, training)
        return VerifiedPoolPublication(self.work_root, reference, digest, payload, training)


def _card_source_hash(payload: Mapping[str, Any]) -> str:
    return _sha256(_canonical(payload))


def _card_markdown(title: str, payload: Mapping[str, Any]) -> str:
    return (f"# {title}\n\n```json\n" +
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n```\n")


def _resolve_checkpoint_ids(
        checkpoints: Sequence[Mapping[str, Any]],
        supplied: Optional[Mapping[str, int]]) -> list[Dict[str, Any]]:
    supplied_ids = dict(supplied or {})
    resolved = []
    seen_ids = set()
    for checkpoint in checkpoints:
        key = checkpoint["ckpt_key"]
        stored_id = checkpoint.get("checkpoint_id")
        supplied_id = supplied_ids.pop(key, None)
        if stored_id is None and supplied_id is None:
            raise PoolPublicationError(
                f"checkpoint {key} 发布后须给数据库 checkpoint_id")
        if stored_id is not None:
            stored_id = _checked_id(stored_id, label=f"checkpoint {key} id")
        if supplied_id is not None:
            supplied_id = _checked_id(supplied_id, label=f"checkpoint {key} supplied id")
        if stored_id is not None and supplied_id is not None and stored_id != supplied_id:
            raise PoolPublicationError(f"checkpoint {key} id 映射冲突")
        checkpoint_id = stored_id if stored_id is not None else supplied_id
        if checkpoint_id in seen_ids:
            raise PoolPublicationError("checkpoint id 映射重复")
        seen_ids.add(checkpoint_id)
        resolved.append({**checkpoint, "checkpoint_id": checkpoint_id})
    if supplied_ids:
        raise PoolPublicationError(
            f"checkpoint_ids 含 training manifest 外的 key: {sorted(supplied_ids)}")
    return resolved


def _database_cards(publication: VerifiedPoolPublication) -> list[Dict[str, Any]]:
    objects = publication.payload["objects"]
    baseline, variant = objects["baseline"], objects["variant"]
    protocol, evaluation = objects["protocol"], objects["evaluation"]
    cards: list[Dict[str, Any]] = []
    if "identity" in baseline:
        source = {
            "baseline_id": baseline["baseline_id"], "canonical_key": baseline["canonical_key"],
            "identity": baseline["identity"], "code": baseline["code"],
        }
        cards.append({"card_type": "baseline", "ref_id": baseline["baseline_id"],
                      "card_md": _card_markdown("Baseline formal assets", source),
                      "src_hash": _card_source_hash(source)})
    variant_source = {
        "variant_id": variant["variant_id"], "baseline_id": baseline["baseline_id"],
        "variant_key": variant["variant_key"], "config": variant["config_asset"],
        "checkpoints": objects["checkpoints"],
        "evaluation": {
            "evaluation_id": evaluation["evaluation_id"], "eval_key": evaluation["eval_key"],
            "attempt_id": evaluation["attempt_id"], "attempt": evaluation["attempt"],
            "primary_artifact": evaluation["primary_artifact"],
        },
        "pool_manifest": {"path": publication.manifest_ref,
                          "sha256": publication.manifest_hash},
    }
    cards.append({"card_type": "variant", "ref_id": variant["variant_id"],
                  "card_md": _card_markdown("Variant formal assets", variant_source),
                  "src_hash": _card_source_hash(variant_source)})
    protocol_source = {
        "protocol_id": protocol["protocol_id"], "version": protocol["version"],
        "name": protocol["name"], "spec": protocol["spec"],
    }
    cards.append({"card_type": "protocol", "ref_id": protocol["protocol_id"],
                  "card_md": _card_markdown("Protocol formal spec", protocol_source),
                  "src_hash": _card_source_hash(protocol_source)})
    return cards


def bind_training_database(
        conn, training: VerifiedTrainingPublication, *, updated_cycle: int,
        checkpoint_ids: Optional[Mapping[str, int]] = None,
        run_id: Optional[int] = None) -> None:
    """Anchor a training manifest in the same transaction that inserts checkpoints.

    ``publish_training`` is intentionally called before checkpoint INSERT, so row IDs may be
    unknown while large files are copied.  The caller inserts each returned formal path/hash,
    then invokes this function with the resulting key->id mapping before committing.  The
    append-only decision makes recovery independent of the original cycle staging directory.
    """
    cycle_id = _checked_id(updated_cycle, label="updated_cycle")
    if not isinstance(training, VerifiedTrainingPublication):
        raise PoolPublicationError("bind_training_database 只接受已验证 training publication")
    objects = training.payload["objects"]
    baseline, variant = objects["baseline"], objects["variant"]
    checkpoints = _resolve_checkpoint_ids(objects["checkpoints"], checkpoint_ids)
    bid, vid = baseline["baseline_id"], variant["variant_id"]
    brow = conn.execute(
        "SELECT slug,canonical_key,code_ref,commit_hash FROM baseline WHERE id=?", (bid,)).fetchone()
    if brow is None or tuple(brow[:2]) != (baseline["slug"], baseline["canonical_key"]):
        raise PoolPublicationError("training publication baseline 与 DB 身份不一致")
    code_ref = baseline["code"]["path"]
    commit_hash = TREE_HASH_ALG + ":" + baseline["code"]["sha256"]
    if brow[2] not in (None, code_ref) or brow[3] not in (None, commit_hash):
        raise PoolPublicationError("baseline 既有 code_ref/commit_hash 与 training publication 冲突")
    vrow = conn.execute(
        "SELECT baseline_id,variant_key,config_json FROM variant WHERE id=?", (vid,)).fetchone()
    if vrow is None or tuple(vrow[:2]) != (bid, variant["variant_key"]):
        raise PoolPublicationError("training publication variant 与 DB 身份不一致")
    db_config, _ = _config_json(vrow[2])
    if db_config != variant["config"]:
        raise PoolPublicationError("formal config.json 与 DB variant.config_json 不一致")
    for checkpoint in checkpoints:
        row = conn.execute(
            "SELECT variant_id,ckpt_key,path,content_hash,hash_alg FROM checkpoint WHERE id=?",
            (checkpoint["checkpoint_id"],)).fetchone()
        expected = (vid, checkpoint["ckpt_key"], checkpoint["path"],
                    checkpoint["content_hash"], checkpoint["hash_alg"])
        if row is None or tuple(row) != expected:
            raise PoolPublicationError(
                f"checkpoint {checkpoint['checkpoint_id']} 未以 formal path/hash INSERT")
    normalized_run = None
    if run_id is not None:
        normalized_run = _checked_id(run_id, label="run_id")
        run = conn.execute("SELECT cycle_id,variant_id FROM run WHERE id=?", (normalized_run,)).fetchone()
        if run is None or tuple(run) != (cycle_id, vid):
            raise PoolPublicationError("training publication run/cycle/variant 绑定不一致")
    if conn.execute("SELECT 1 FROM cycle WHERE id=?", (cycle_id,)).fetchone() is None:
        raise PoolPublicationError(f"updated cycle c{cycle_id} 不存在")

    conn.execute("UPDATE baseline SET code_ref=?,commit_hash=? WHERE id=?",
                 (code_ref, commit_hash, bid))
    event = {
        "schema": TRAINING_DB_BINDING_SCHEMA,
        "manifest_ref": training.manifest_ref, "manifest_hash": training.manifest_hash,
        "baseline_id": bid, "variant_id": vid,
        "checkpoint_ids": [item["checkpoint_id"] for item in checkpoints],
        "run_id": normalized_run,
    }
    canonical_event = _canonical(event).decode("utf-8").rstrip("\n")
    existing = conn.execute(
        "SELECT payload_json FROM decision WHERE actor='gate' "
        "AND type='pool_training_publication' "
        "AND json_extract(payload_json,'$.manifest_hash')=? ORDER BY id",
        (training.manifest_hash,)).fetchall()
    if existing:
        if len(existing) != 1 or existing[0][0] != canonical_event:
            raise PoolPublicationError("pool_training_publication 幂等身份冲突")
    else:
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'gate','pool_training_publication',?)",
            (cycle_id, canonical_event))


def bind_database(conn, publication: VerifiedPoolPublication, *, updated_cycle: int) -> None:
    """Bind a verified publication inside the caller's gate registration transaction.

    This function performs no filesystem I/O and never changes pool statuses.  The
    caller must have called ``PoolPublisher.verify_publication`` immediately before
    opening the short transaction.  Every DB identity and every metric is checked
    before baseline metadata/cards/decision are written.
    """
    cycle_id = _checked_id(updated_cycle, label="updated_cycle")
    if not isinstance(publication, VerifiedPoolPublication):
        raise PoolPublicationError("bind_database 只接受已验证 publication")
    objects = publication.payload["objects"]
    baseline, variant = objects["baseline"], objects["variant"]
    protocol, evaluation = objects["protocol"], objects["evaluation"]
    bid, vid = baseline["baseline_id"], variant["variant_id"]

    brow = conn.execute(
        "SELECT slug,canonical_key,identity_doc,code_ref,commit_hash,status FROM baseline WHERE id=?",
        (bid,)).fetchone()
    if brow is None or brow[0] != baseline["slug"] or brow[1] != baseline["canonical_key"]:
        raise PoolPublicationError("publication baseline 与 DB 身份不一致")
    if "identity" in baseline and brow[2] != baseline["identity_doc"]:
        raise PoolPublicationError("baseline.identity_doc 与 formal identity.md 不一致")
    code_ref = baseline.get("code", {}).get("path")
    commit_hash = (TREE_HASH_ALG + ":" + baseline["code"]["sha256"]
                   if "code" in baseline else None)
    if code_ref is not None:
        if brow[3] not in (None, code_ref) or brow[4] not in (None, commit_hash):
            raise PoolPublicationError("baseline 既有 code_ref/commit_hash 与 publication 冲突")

    vrow = conn.execute(
        "SELECT baseline_id,variant_key,config_json,status FROM variant WHERE id=?", (vid,)).fetchone()
    if vrow is None or vrow[0] != bid or vrow[1] != variant["variant_key"]:
        raise PoolPublicationError("publication variant 与 DB 身份不一致")
    try:
        db_config, _ = _config_json(vrow[2])
    except PoolPublicationError as error:
        raise PoolPublicationError("DB variant.config_json 损坏") from error
    if db_config != variant["config"]:
        raise PoolPublicationError("formal config.json 与 DB variant.config_json 不一致")

    for checkpoint in objects["checkpoints"]:
        row = conn.execute(
            "SELECT variant_id,ckpt_key,path,content_hash,hash_alg FROM checkpoint WHERE id=?",
            (checkpoint["checkpoint_id"],)).fetchone()
        expected = (vid, checkpoint["ckpt_key"], checkpoint["path"],
                    checkpoint["content_hash"], checkpoint["hash_alg"])
        if row is None or tuple(row) != expected:
            raise PoolPublicationError(
                f"checkpoint {checkpoint['checkpoint_id']} 未以正式池 path/hash 登记")

    prow = conn.execute(
        "SELECT name,scope_spec_json FROM protocol WHERE id=? AND version=?",
        (protocol["protocol_id"], protocol["version"])).fetchone()
    if prow is None or prow[0] != protocol["name"]:
        raise PoolPublicationError("publication protocol 与 DB 身份不一致")
    db_scope, _ = _scope_json(prow[1])
    if db_scope != protocol["scope_spec"]:
        raise PoolPublicationError("formal protocol spec 与 DB scope_spec_json 不一致")

    erow = conn.execute(
        "SELECT variant_id,protocol_id,protocol_ver,eval_key,status,canonical_attempt_id "
        "FROM evaluation WHERE id=?", (evaluation["evaluation_id"],)).fetchone()
    expected_eval = (vid, protocol["protocol_id"], protocol["version"],
                     evaluation["eval_key"], "success")
    # A retry/repro/metric append is formally published too, but first-success
    # canonicalization is immutable: the newly published attempt need not be the
    # evaluation's canonical attempt.  Its own identity and bytes are checked below.
    if (erow is None or tuple(erow[:5]) != expected_eval
            or erow[5] is None):
        raise PoolPublicationError("publication evaluation 未成功封 canonical attempt")
    attempt = conn.execute(
        "SELECT evaluation_id,attempt_no,status,artifact_ref,transcript_ref "
        "FROM evaluation_attempt WHERE id=?", (evaluation["attempt_id"],)).fetchone()
    binding = publication.database_bindings["evaluation_attempt"]
    expected_attempt = (evaluation["evaluation_id"], evaluation["attempt_no"], "success",
                        binding["artifact_ref"], publication.manifest_ref)
    if attempt is None or tuple(attempt) != expected_attempt:
        raise PoolPublicationError(
            "canonical attempt 未绑定 artifact hash + content-address pool manifest")
    log = conn.execute(
        "SELECT content_hash FROM execution_log WHERE evaluation_attempt_id=? AND log_kind='eval' "
        "AND ref=?", (evaluation["attempt_id"], binding["execution_log_ref"])).fetchone()
    if log is None or log[0] != binding["execution_log_hash"]:
        raise PoolPublicationError("canonical attempt 缺 formal evaluation execution_log ref/hash")

    db_metrics = conn.execute(
        "SELECT metric_id,metric_ver,value,scope,checkpoint_id FROM metric_result "
        "WHERE evaluation_id=? AND evaluation_attempt_id=?",
        (evaluation["evaluation_id"], evaluation["attempt_id"])).fetchall()
    expected_metrics = [(row["metric_id"], row["metric_ver"], float(row["value"]),
                         row["scope"], row.get("checkpoint_id"))
                        for row in evaluation["metrics"]]
    normalize = lambda rows: sorted(rows, key=lambda row: (
        row[0], row[1], row[3], row[4] if row[4] is not None else 0))
    if normalize([tuple(row) for row in db_metrics]) != normalize(expected_metrics):
        raise PoolPublicationError("formal metric_results.json 与 DB metric_result 不一致")

    training_events = conn.execute(
        "SELECT payload_json FROM decision WHERE actor='gate' "
        "AND type='pool_training_publication' "
        "AND json_extract(payload_json,'$.manifest_hash')=? ORDER BY id",
        (publication.training.manifest_hash,)).fetchall()
    if len(training_events) != 1:
        raise PoolPublicationError("完整 publication 缺唯一 pool_training_publication DB 锚")
    try:
        training_event = json.loads(training_events[0][0])
    except (TypeError, json.JSONDecodeError) as error:
        raise PoolPublicationError("pool_training_publication decision 损坏") from error
    if (training_event.get("manifest_ref") != publication.training.manifest_ref
            or training_event.get("baseline_id") != bid
            or training_event.get("variant_id") != vid
            or training_event.get("checkpoint_ids")
            != [row["checkpoint_id"] for row in objects["checkpoints"]]):
        raise PoolPublicationError("pool_training_publication DB 锚与完整 publication 不一致")

    # Only after every identity/path/hash check may the registration transaction bind refs.
    if code_ref is not None:
        conn.execute("UPDATE baseline SET code_ref=?,commit_hash=? WHERE id=?",
                     (code_ref, commit_hash, bid))
    event = {
        "schema": DB_BINDING_SCHEMA, "manifest_ref": publication.manifest_ref,
        "manifest_hash": publication.manifest_hash, "baseline_id": bid,
        "variant_id": vid,
        "checkpoint_ids": [row["checkpoint_id"] for row in objects["checkpoints"]],
        "protocol_id": protocol["protocol_id"], "protocol_ver": protocol["version"],
        "evaluation_id": evaluation["evaluation_id"], "attempt_id": evaluation["attempt_id"],
    }
    existing = conn.execute(
        "SELECT payload_json FROM decision WHERE actor='gate' AND type='pool_publication' "
        "AND json_extract(payload_json,'$.manifest_hash')=? ORDER BY id",
        (publication.manifest_hash,)).fetchall()
    canonical_event = _canonical(event).decode("utf-8").rstrip("\n")
    if existing:
        if len(existing) != 1 or existing[0][0] != canonical_event:
            raise PoolPublicationError("pool_publication decision 幂等身份冲突")
    else:
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (?,'gate','pool_publication',?)", (cycle_id, canonical_event))

    cycle = conn.execute("SELECT goal_id,goal_ver FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if cycle is None:
        raise PoolPublicationError(f"updated cycle c{cycle_id} 不存在")
    for card in _database_cards(publication):
        conn.execute(
            "INSERT INTO card(card_type,ref_id,goal_id,goal_ver,card_md,src_hash,updated_cycle,stale) "
            "VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(card_type,ref_id) DO UPDATE SET "
            "goal_id=excluded.goal_id,goal_ver=excluded.goal_ver,card_md=excluded.card_md,"
            "src_hash=excluded.src_hash,updated_cycle=excluded.updated_cycle,stale=0",
            (card["card_type"], card["ref_id"], cycle[0], cycle[1], card["card_md"],
             card["src_hash"], cycle_id))


def formal_publication_event(conn, *, variant_id: int) -> Optional[Mapping[str, Any]]:
    """Return the DB-closed canonical publication event for one legal variant.

    This is deliberately stricter than checking ``variant.status``.  It is a cheap structural
    lookup for selectors/views; callers that are about to execute or restore an asset must pass
    the returned ref/hash to :meth:`PoolPublisher.verify_publication` and bind the verified object
    identities back to this event.
    """
    vid = _checked_id(variant_id, label="variant_id")
    row = conn.execute(
        "SELECT v.status,b.id,b.status,b.code_ref,b.commit_hash FROM variant v "
        "JOIN baseline b ON b.id=v.baseline_id WHERE v.id=?", (vid,)).fetchone()
    if (row is None or row[0] != "legal" or row[2] != "legal"
            or not isinstance(row[3], str) or not row[3]
            or not isinstance(row[4], str)
            or re.fullmatch(r"sha256-tree-v1:[0-9a-f]{64}", row[4]) is None):
        return None
    baseline_id = row[1]
    events = conn.execute(
        "SELECT payload_json FROM decision WHERE actor='gate' AND type='pool_publication' "
        "AND json_extract(payload_json,'$.variant_id')=? ORDER BY id DESC", (vid,)).fetchall()
    for event_row in events:
        try:
            event = json.loads(event_row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        manifest_ref, manifest_hash = event.get("manifest_ref"), event.get("manifest_hash")
        if (event.get("schema") != DB_BINDING_SCHEMA
                or event.get("baseline_id") != baseline_id
                or not isinstance(manifest_ref, str) or not manifest_ref
                or not isinstance(manifest_hash, str) or not _HASH_RE.fullmatch(manifest_hash)):
            continue
        try:
            manifest_path = _manifest_ref(manifest_ref, label="pool_publication.manifest_ref")
        except PoolPublicationError:
            continue
        if (manifest_path.parent != _MANIFEST_DIR
                or manifest_path.name != f"{manifest_hash}.json"):
            continue
        evaluation = conn.execute(
            "SELECT e.variant_id,e.protocol_id,e.protocol_ver,e.status,e.canonical_attempt_id,ea.status,"
            "ea.transcript_ref FROM evaluation e JOIN evaluation_attempt ea "
            "ON ea.id=e.canonical_attempt_id WHERE e.id=?", (event.get("evaluation_id"),)).fetchone()
        if (evaluation is None or tuple(evaluation) != (
                vid, event.get("protocol_id"), event.get("protocol_ver"),
                "success", event.get("attempt_id"),
                "success", manifest_ref)):
            continue
        cards = conn.execute(
            "SELECT card_type,ref_id,src_hash,stale FROM card WHERE "
            "(card_type='baseline' AND ref_id=?) OR "
            "(card_type='variant' AND ref_id=?) OR "
            "(card_type='protocol' AND ref_id=?)",
            (baseline_id, vid, event.get("protocol_id"))).fetchall()
        identities = {(card[0], card[1]) for card in cards
                      if isinstance(card[2], str) and card[2] and card[3] == 0}
        if identities == {("baseline", baseline_id), ("variant", vid),
                          ("protocol", event.get("protocol_id"))}:
            return event
    return None


def is_formally_published(conn, *, variant_id: int) -> bool:
    """Return whether ``legal`` has a structurally complete formal DB closure."""
    return formal_publication_event(conn, variant_id=variant_id) is not None
