"""Trusted, label-separating qualification views for the EEG acceptance tasks.

This module is deliberately narrow.  It does not try to be a generic dataset
loader: it accepts one canonical SEED ``ExtractedFeatures`` archive and one
canonical DREAMER archive, materializes public numeric views, and publishes the
label truth into a separate sealed tree.  Untrusted research code must receive
only the public tree.

Both entry points stage below the destination parents, fsync every payload, and
publish the sealed tree before the public tree with atomic directory renames
(``RENAME_NOREPLACE`` when supported).  Thus a public view is never
intentionally made visible before its matching truth.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import io
import json
import math
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .artifact_capability import open_artifact


class QualificationDataError(RuntimeError):
    """The qualification archive or requested publication is unsafe/invalid."""


SEED_ADAPTER = "meta-research-seed-public-view"
SEED_ADAPTER_VERSION = 1
DREAMER_ADAPTER = "meta-research-dreamer-public-view"
DREAMER_ADAPTER_VERSION = 1
VIEW_PROTOCOL = "meta-research-qualification-view/v1"
VIEW_RECEIPT_NAME = "qualification-view.json"

_SECRET_MIN_BYTES = 32
_MAX_SECRET_BYTES = 4096
_MAX_ZIP_MEMBERS = 4096
_MAX_MEMBER_BYTES = 16 * 1024 ** 3
_MAX_ARCHIVE_EXPANDED_BYTES = 128 * 1024 ** 3
_MAX_COMPRESSION_RATIO = 2000
_SEED_SUBJECTS = 15
_SEED_SESSIONS = 3
_SEED_TRIALS = 15
_SEED_CHANNELS = 62
_SEED_BANDS = 5
_DREAMER_SUBJECTS = 23
_DREAMER_RECORDS = 18
_DREAMER_CHANNELS = 14
_DREAMER_ELECTRODES = (
    "AF3", "F7", "F3", "FC5", "T7", "P7", "O1",
    "O2", "P8", "T8", "FC6", "F4", "F8", "AF4",
)
_SEED_SESSION_RE = re.compile(r"^([1-9]|1[0-5])_([0-9]{8})\.mat$")
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise QualificationDataError("receipt/manifest 不是有限 canonical JSON") from error


def _sha_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_secret(secret: Any) -> bytes:
    if not isinstance(secret, (bytes, bytearray, memoryview)):
        raise QualificationDataError("secret 须为 bytes-like")
    value = bytes(secret)
    if not _SECRET_MIN_BYTES <= len(value) <= _MAX_SECRET_BYTES:
        raise QualificationDataError(
            f"secret 长度须在 {_SECRET_MIN_BYTES}..{_MAX_SECRET_BYTES} bytes")
    return value


def _validate_uid(value: Any, *, label: str) -> int:
    if value is None:
        return os.geteuid()
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFE:
        raise QualificationDataError(f"{label} 须为有效非负 UID")
    return value


def _validate_destinations(
        public_root: Path | str, sealed_root: Path | str, *,
        research_uid: int, evaluator_uid: int) -> Tuple[Path, Path]:
    public = Path(os.path.abspath(os.fspath(public_root)))
    sealed = Path(os.path.abspath(os.fspath(sealed_root)))
    if public == Path(public.anchor) or sealed == Path(sealed.anchor):
        raise QualificationDataError("public_root/sealed_root 不得是文件系统根")
    try:
        common = Path(os.path.commonpath((str(public), str(sealed))))
    except ValueError as error:
        raise QualificationDataError("public_root/sealed_root 路径语境非法") from error
    if common in (public, sealed):
        raise QualificationDataError("public_root 与 sealed_root 不得相同或互为祖先")
    for target, label in ((public, "public_root"), (sealed, "sealed_root")):
        if os.path.lexists(target):
            raise QualificationDataError(f"{label} 已存在，拒绝覆盖: {target}")
        try:
            parent_info = os.lstat(target.parent)
        except OSError as error:
            raise QualificationDataError(
                f"{label} parent 须由 operator 预先创建: {target.parent}") from error
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or os.path.realpath(target.parent) != str(target.parent)
                or parent_info.st_mode & 0o022
                or parent_info.st_uid != os.geteuid()
                or evaluator_uid != os.geteuid()):
            raise QualificationDataError(f"{label} parent 不是可信不可写目录")
        for ancestor in (target.parent, *target.parent.parents):
            ancestor_info = os.lstat(ancestor)
            if (not stat.S_ISDIR(ancestor_info.st_mode)
                    or stat.S_ISLNK(ancestor_info.st_mode)
                    or ancestor_info.st_uid not in {0, os.geteuid()}
                    or ancestor_info.st_mode & 0o022):
                raise QualificationDataError(
                    f"{label} ancestor 不是 root/evaluator-owned trusted path: {ancestor}")
    public_parent = os.lstat(public.parent)
    required_other = stat.S_IROTH | stat.S_IXOTH
    if (research_uid != os.geteuid() and public_parent.st_uid != research_uid
            and public_parent.st_mode & required_other != required_other):
        raise QualificationDataError(
            "public_root parent 对 research_uid 不可安全打开；请由 operator 预建 0755 共享父目录")
    sealed_parent = os.lstat(sealed.parent)
    if (research_uid != os.geteuid() and sealed_parent.st_uid != research_uid
            and not sealed_parent.st_mode & stat.S_IXOTH):
        raise QualificationDataError(
            "sealed_root parent 对 research_uid 不可遍历；请至少保留 other execute")
    return public, sealed


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _write_json(path: Path, value: Any, mode: int) -> None:
    _write_bytes(path, _canonical(value), mode)


def _npy_bytes(array: Any) -> bytes:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - deployment check
        raise QualificationDataError("qualification data 需要 numpy") from error
    value = np.asarray(array)
    if value.dtype.hasobject:
        raise QualificationDataError("拒绝 object dtype 写入 qualification view")
    output = io.BytesIO()
    np.save(output, value, allow_pickle=False)
    return output.getvalue()


def _write_npy(path: Path, array: Any, mode: int) -> None:
    _write_bytes(path, _npy_bytes(array), mode)


def _write_safe_npz(path: Path, arrays: Mapping[str, Any], mode: int) -> None:
    """Write a deterministic, pickle-free NPZ with a closed member set."""
    if not arrays or any(
            not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
            for name in arrays):
        raise QualificationDataError("NPZ array 名称闭包非法")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(os.dup(fd), "wb", closefd=True) as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for name in sorted(arrays):
                    info = zipfile.ZipInfo(name + ".npy", date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.external_attr = (0o100444 & 0xFFFF) << 16
                    archive.writestr(info, _npy_bytes(arrays[name]))
            stream.flush()
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> Tuple[str, int]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise QualificationDataError(f"ledger 只接受常规文件: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
                info.st_dev, info.st_ino, info.st_size) or size != info.st_size:
            raise QualificationDataError(f"ledger 读取期间文件身份漂移: {path}")
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest(), size


def _file_ledger(root: Path, *, exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
    excluded = set(exclude)
    rows: List[Dict[str, Any]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs:
            item = Path(current) / name
            if item.is_symlink() or not item.is_dir():
                raise QualificationDataError(f"view 含非法目录: {item}")
        for name in files:
            item = Path(current) / name
            rel = item.relative_to(root).as_posix()
            if rel in excluded:
                continue
            digest, size = _hash_file(item)
            rows.append({
                "path": rel, "sha256": digest, "bytes": size,
                "mode": format(stat.S_IMODE(os.lstat(item).st_mode), "04o"),
            })
    return rows


def _view_ledger(root: Path) -> List[Dict[str, Any]]:
    return [
        {"path": row["path"], "sha256": row["sha256"], "bytes": row["bytes"]}
        for row in _file_ledger(root, exclude={VIEW_RECEIPT_NAME})
    ]


def _write_view_receipt(root: Path, *, task: str, role: str, dataset: str,
                        fold: Any, adapter: str, adapter_version: int) -> Dict[str, Any]:
    receipt = {
        "version": 1,
        "protocol": VIEW_PROTOCOL,
        "task": task,
        "role": role,
        "dataset": dataset,
        "fold": fold,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "files": _view_ledger(root),
    }
    _write_json(root / VIEW_RECEIPT_NAME, receipt, 0o600)
    return receipt


def _chmod_files(root: Path, mode: int) -> None:
    for current, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                raise QualificationDataError("view 不得含 symlink")
            os.chmod(path, mode, follow_symlinks=False)


def _chown_tree(root: Path, uid: int) -> None:
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files + dirs:
            path = Path(current) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise QualificationDataError("view 不得含 symlink")
            if info.st_uid != uid:
                try:
                    os.chown(path, uid, -1, follow_symlinks=False)
                except OSError as error:
                    raise QualificationDataError(
                        f"无法将 qualification view chown 到 UID {uid}: {path}") from error
        current_path = Path(current)
        if os.lstat(current_path).st_uid != uid:
            try:
                os.chown(current_path, uid, -1, follow_symlinks=False)
            except OSError as error:
                raise QualificationDataError(
                    f"无法将 qualification view chown 到 UID {uid}: {current_path}") from error


def _finalize_modes_and_sync(root: Path, *, file_mode: int, dir_mode: int) -> None:
    _chmod_files(root, file_mode)
    for current, dirs, _files in os.walk(root, topdown=False, followlinks=False):
        for name in dirs:
            child = Path(current) / name
            os.chmod(child, dir_mode, follow_symlinks=False)
            _fsync_dir(child)
        current_path = Path(current)
        os.chmod(current_path, dir_mode, follow_symlinks=False)
        _fsync_dir(current_path)
    _fsync_dir(root.parent)


def _make_stage(target: Path) -> Path:
    raw = tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(target.parent))
    stage = Path(raw)
    os.chmod(stage, 0o700)
    return stage


def _make_stages(public: Path, sealed: Path) -> Tuple[Path, Path]:
    public_stage = _make_stage(public)
    try:
        return public_stage, _make_stage(sealed)
    except BaseException:
        _writable_remove(public_stage)
        raise


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD, os.fsencode(source), _AT_FDCWD,
            os.fsencode(destination), _RENAME_NOREPLACE)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise QualificationDataError(f"目标在发布时已存在，拒绝覆盖: {destination}")
        if code not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise QualificationDataError(
                f"qualification view atomic publish 失败: {os.strerror(code)}")

    # Some shared/FUSE filesystems reject RENAME_NOREPLACE.  The roots were
    # already checked before staging; retain atomic visibility with rename(2)
    # and make the last no-clobber check immediately before it.
    if os.path.lexists(destination):
        raise QualificationDataError(f"目标在发布时已存在，拒绝覆盖: {destination}")
    try:
        os.rename(source, destination)
    except OSError as exc:
        raise QualificationDataError(
            f"qualification view atomic publish 失败: {exc.strerror}") from exc


def _writable_remove(path: Path) -> None:
    if not os.path.lexists(path):
        return
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            item = Path(current) / name
            if not item.is_symlink():
                os.chmod(item, 0o600, follow_symlinks=False)
            item.unlink()
        for name in dirs:
            item = Path(current) / name
            if not item.is_symlink():
                os.chmod(item, 0o700, follow_symlinks=False)
            item.rmdir()
    os.chmod(path, 0o700, follow_symlinks=False)
    path.rmdir()


def _publish_pair(public_stage: Path, sealed_stage: Path,
                  public_root: Path, sealed_root: Path) -> None:
    sealed_published = False
    public_published = False
    try:
        _rename_noreplace(sealed_stage, sealed_root)
        sealed_published = True
        _fsync_dir(sealed_root.parent)
        _rename_noreplace(public_stage, public_root)
        public_published = True
        _fsync_dir(public_root.parent)
    except BaseException:
        # Ordinary exceptions before public visibility can be rolled back.  A
        # process crash remains fail-closed: at worst only the sealed tree exists.
        if sealed_published and not public_published and os.path.lexists(sealed_root):
            try:
                _rename_noreplace(sealed_root, sealed_stage)
                sealed_published = False
            except BaseException:
                pass
        raise
    finally:
        if not public_published:
            _writable_remove(public_stage)
        if not sealed_published:
            _writable_remove(sealed_stage)


def _safe_zip_name(raw: str) -> str:
    if (not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw
            or raw.startswith("/")):
        raise QualificationDataError(f"ZIP member 路径非法: {raw!r}")
    path = PurePosixPath(raw)
    if any(part in ("", ".", "..") for part in path.parts):
        raise QualificationDataError(f"ZIP member 路径穿越/非 canonical: {raw!r}")
    return path.as_posix()


def _strict_zip_files(archive: zipfile.ZipFile) -> Dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > _MAX_ZIP_MEMBERS:
        raise QualificationDataError("ZIP member 数量非法")
    files: Dict[str, zipfile.ZipInfo] = {}
    folded = set()
    total = 0
    for info in infos:
        name = _safe_zip_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise QualificationDataError(f"ZIP member 不得是 symlink: {name}")
        if info.flag_bits & 0x1:
            raise QualificationDataError(f"ZIP member 不得加密: {name}")
        if info.is_dir():
            continue
        key = name.casefold()
        if name in files or key in folded:
            raise QualificationDataError(f"ZIP member 重复/大小写别名: {name}")
        if info.file_size < 0 or info.file_size > _MAX_MEMBER_BYTES:
            raise QualificationDataError(f"ZIP member 展开大小非法: {name}")
        if (info.file_size and (info.compress_size <= 0
                                or info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO)):
            raise QualificationDataError(f"ZIP member 压缩比异常: {name}")
        total += info.file_size
        if total > _MAX_ARCHIVE_EXPANDED_BYTES:
            raise QualificationDataError("ZIP 展开总量超过上限")
        files[name] = info
        folded.add(key)
    return files


def _load_mat_member(
        archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, simplify: bool,
        spool_dir: Path | None = None,
        variable_names: Sequence[str] | None = None) -> Dict[str, Any]:
    try:
        from scipy.io import loadmat
    except ImportError as error:  # pragma: no cover - deployment check
        raise QualificationDataError("qualification data 需要 scipy") from error
    try:
        with archive.open(info, "r") as source, tempfile.TemporaryFile(
                mode="w+b", dir=spool_dir) as spool:
            remaining = info.file_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise QualificationDataError(f"ZIP member 提前截断: {info.filename}")
                spool.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise QualificationDataError(f"ZIP member 长度超过目录声明: {info.filename}")
            spool.seek(0)
            value = loadmat(
                spool, simplify_cells=simplify,
                variable_names=(None if variable_names is None else list(variable_names)))
    except QualificationDataError:
        raise
    except BaseException as error:
        raise QualificationDataError(f"MAT 解析失败: {info.filename}") from error
    if not isinstance(value, dict):
        raise QualificationDataError(f"MAT 顶层须为 mapping: {info.filename}")
    return value


def _seed_order_key(secret: bytes, subject: int, session: int,
                    trial: int, time_index: int) -> bytes:
    message = (
        "seed-public-view-v1\x00"
        f"subject={subject:02d}\x00session={session:02d}\x00"
        f"trial={trial:02d}\x00time={time_index:08d}"
    ).encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).digest()


def _seed_sample_id(secret: bytes, subject: int, session: int,
                    trial: int, time_index: int) -> str:
    message = (
        "seed-public-sample-v1\x00"
        f"subject={subject:02d}\x00session={session:02d}\x00"
        f"trial={trial:02d}\x00time={time_index:08d}"
    ).encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _seed_labels(payload: Mapping[str, Any]) -> Any:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    keys = {key for key in payload if not key.startswith("__")}
    if keys != {"label"}:
        raise QualificationDataError(f"SEED label.mat 字段闭包非法: {sorted(keys)}")
    raw = np.asarray(payload["label"])
    if raw.dtype.kind not in "iuf":
        raise QualificationDataError("SEED label 须为实数 numeric array")
    values = np.squeeze(raw)
    if values.shape != (_SEED_TRIALS,) or not np.all(np.isfinite(values)):
        raise QualificationDataError("SEED label 须恰为 15 个有限值")
    if not np.all(np.isin(values, (-1, 0, 1))):
        raise QualificationDataError("SEED label 只允许 -1/0/1")
    ints = values.astype(np.int8)
    return (ints + 1).astype(np.uint8)


def _seed_subject_arrays(subject: int, sessions: Sequence[Mapping[str, Any]],
                         labels: Any, secret: bytes) -> Tuple[Any, Any, List[str]]:
    """Pure conversion core used by the strict production entry point."""
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    if len(sessions) != _SEED_SESSIONS:
        raise QualificationDataError(f"SEED subject-{subject:02d} 不足 3 sessions")
    blocks = []
    label_blocks = []
    keys: List[bytes] = []
    sample_ids: List[str] = []
    expected = {f"de_LDS{trial}" for trial in range(1, _SEED_TRIALS + 1)}
    for session_no, payload in enumerate(sessions, start=1):
        present = {key for key in payload if key.startswith("de_LDS")}
        if present != expected:
            raise QualificationDataError(
                f"SEED subject-{subject:02d} session {session_no} de_LDS trial 闭包非法: "
                f"missing={sorted(expected-present)}, extra={sorted(present-expected)}")
        for trial in range(1, _SEED_TRIALS + 1):
            raw = np.asarray(payload[f"de_LDS{trial}"])
            if raw.dtype.kind not in "iuf" or raw.ndim != 3:
                raise QualificationDataError("SEED de_LDS 须为三维实数 numeric array")
            if raw.shape[0] != _SEED_CHANNELS or raw.shape[2] != _SEED_BANDS or raw.shape[1] <= 0:
                raise QualificationDataError(
                    f"SEED de_LDS{trial} shape 须为 62×T×5，实收 {raw.shape}")
            try:
                numeric = np.asarray(raw, dtype=np.float32)
            except (TypeError, ValueError, OverflowError) as error:
                raise QualificationDataError("SEED de_LDS 无法安全转 float32") from error
            if not np.all(np.isfinite(numeric)):
                raise QualificationDataError("SEED de_LDS 含 NaN/Inf")
            block = np.ascontiguousarray(numeric.transpose(1, 0, 2))
            blocks.append(block)
            label_blocks.append(np.full(block.shape[0], labels[trial - 1], dtype=np.uint8))
            for time_no in range(block.shape[0]):
                keys.append(_seed_order_key(
                    secret, subject, session_no, trial, time_no))
                sample_ids.append(_seed_sample_id(
                    secret, subject, session_no, trial, time_no))
    x = np.concatenate(blocks, axis=0)
    y = np.concatenate(label_blocks, axis=0)
    if len(keys) != x.shape[0]:
        raise QualificationDataError("SEED 内部 sample/key 数量不一致")
    order = sorted(range(len(keys)), key=keys.__getitem__)
    ordered_keys = [keys[index] for index in order]
    if any(left == right for left, right in zip(ordered_keys, ordered_keys[1:])):
        raise QualificationDataError("SEED opaque shuffle HMAC 发生碰撞")
    ordered_ids = [sample_ids[index] for index in order]
    if len(set(ordered_ids)) != len(ordered_ids):
        raise QualificationDataError("SEED opaque sample ID HMAC 发生碰撞")
    return (
        np.ascontiguousarray(x[order], dtype=np.float32),
        np.ascontiguousarray(y[order]),
        ordered_ids,
    )


def _seed_archive_layout(files: Mapping[str, zipfile.ZipInfo]) -> Tuple[
        zipfile.ZipInfo, Dict[int, List[zipfile.ZipInfo]]]:
    labels = [
        (name, info) for name, info in files.items()
        if PurePosixPath(name).name == "label.mat"
        and PurePosixPath(name).parent.name == "ExtractedFeatures"]
    if len(labels) != 1:
        raise QualificationDataError("SEED.zip 须恰含一个 ExtractedFeatures/label.mat")
    base = PurePosixPath(labels[0][0]).parent
    selected = {
        name: info for name, info in files.items()
        if PurePosixPath(name).parent == base and PurePosixPath(name).suffix == ".mat"
    }
    unexpected = [
        name for name in files
        if PurePosixPath(name).parent == base
        and PurePosixPath(name).suffix != ".mat"
        and PurePosixPath(name).name.casefold() != "readme.txt"
    ]
    if unexpected:
        raise QualificationDataError(
            "SEED ExtractedFeatures 含未知外围文件: " + repr(sorted(unexpected)))
    by_subject: Dict[int, List[Tuple[str, zipfile.ZipInfo]]] = {
        subject: [] for subject in range(1, _SEED_SUBJECTS + 1)}
    for name, info in selected.items():
        leaf = PurePosixPath(name).name
        if leaf == "label.mat":
            continue
        match = _SEED_SESSION_RE.fullmatch(leaf)
        if match is None:
            raise QualificationDataError(f"SEED session 文件名非法: {leaf}")
        by_subject[int(match.group(1))].append((match.group(2), info))
    if len(selected) != 1 + _SEED_SUBJECTS * _SEED_SESSIONS:
        raise QualificationDataError("SEED.zip MAT 文件数量不是 label + 15×3")
    result: Dict[int, List[zipfile.ZipInfo]] = {}
    for subject, rows in by_subject.items():
        if len(rows) != _SEED_SESSIONS or len({date for date, _ in rows}) != _SEED_SESSIONS:
            raise QualificationDataError(f"SEED subject-{subject:02d} 须恰有 3 个唯一 session")
        result[subject] = [info for _date, info in sorted(rows, key=lambda item: item[0])]
    return labels[0][1], result


def _receipt(adapter: str, version: int, profile: str, input_sha256: str,
             public_ledger: List[Dict[str, Any]],
             sealed_ledger: List[Dict[str, Any]]) -> Dict[str, Any]:
    public_hash = _sha_bytes(_canonical(public_ledger))
    sealed_hash = _sha_bytes(_canonical(sealed_ledger))
    return {
        "adapter": adapter, "adapter_version": version, "profile": profile,
        "input_sha256": input_sha256,
        "public": {"files": public_ledger, "ledger_sha256": public_hash},
        "sealed": {"files": sealed_ledger, "ledger_sha256": sealed_hash},
    }


def prepare_seed_views(seed_zip: Path | str, public_root: Path | str,
                       sealed_root: Path | str, secret: bytes, *,
                       research_uid: int | None = None,
                       evaluator_uid: int | None = None) -> Dict[str, Any]:
    """Publish strict 15-fold SEED DE views and separately sealed target labels."""
    key = _validate_secret(secret)
    research_owner = _validate_uid(research_uid, label="research_uid")
    evaluator_owner = _validate_uid(evaluator_uid, label="evaluator_uid")
    public, sealed = _validate_destinations(
        public_root, sealed_root, research_uid=research_owner,
        evaluator_uid=evaluator_owner)
    public_stage, sealed_stage = _make_stages(public, sealed)
    try:
        with open_artifact(seed_zip, label="SEED.zip") as capability:
            duplicate = os.dup(capability.fd)
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb", closefd=True) as stream, zipfile.ZipFile(stream) as archive:
                files = _strict_zip_files(archive)
                label_info, sessions_by_subject = _seed_archive_layout(files)
                labels = _seed_labels(_load_mat_member(
                    archive, label_info, simplify=False, spool_dir=public_stage,
                    variable_names=["label"]))
                subjects = {}
                for subject in range(1, _SEED_SUBJECTS + 1):
                    session_payloads = [
                        _load_mat_member(
                            archive, info, simplify=False, spool_dir=public_stage,
                            variable_names=[
                                f"de_LDS{trial}" for trial in range(1, 16)])
                        for info in sessions_by_subject[subject]]
                    subjects[subject] = _seed_subject_arrays(
                        subject, session_payloads, labels, key)
            capability.verify_unchanged()
            input_sha256 = capability.identity.content_hash

            public_x: Dict[int, Path] = {}
            public_y: Dict[int, Path] = {}
            truth_folds = []
            for held_out in range(1, _SEED_SUBJECTS + 1):
                fold = public_stage / f"fold-{held_out:02d}"
                (fold / "source").mkdir(parents=True, mode=0o700)
                (fold / "target").mkdir(mode=0o700)
                sources = []
                for subject in range(1, _SEED_SUBJECTS + 1):
                    x, y, sample_ids = subjects[subject]
                    if subject == held_out:
                        x_path = fold / "target" / "x.npy"
                        _write_json(fold / "target" / "sample_ids.json", {
                            "version": 1, "fold": held_out,
                            "sample_ids": sample_ids,
                        }, 0o600)
                    else:
                        subject_dir = fold / "source" / f"subject-{subject:02d}"
                        subject_dir.mkdir(mode=0o700)
                        x_path = subject_dir / "x.npy"
                        y_path = subject_dir / "y.npy"
                        if subject in public_y:
                            os.link(public_y[subject], y_path)
                        else:
                            _write_npy(y_path, y, 0o600)
                            public_y[subject] = y_path
                        sources.append(f"subject-{subject:02d}")
                    if subject in public_x:
                        os.link(public_x[subject], x_path)
                    else:
                        _write_npy(x_path, x, 0o600)
                        public_x[subject] = x_path
                protocol = {
                    "adapter": SEED_ADAPTER,
                    "adapter_version": SEED_ADAPTER_VERSION,
                    "profile": "seed-cross-subject-uda-public-v1",
                    "feature": "de_LDS",
                    "dtype": "float32",
                    "sample_shape": [_SEED_CHANNELS, _SEED_BANDS],
                    "classes": [0, 1, 2],
                    "source_subjects": sources,
                    "target_file": "target/x.npy",
                    "target_sample_ids_file": "target/sample_ids.json",
                }
                _write_json(fold / "protocol.json", protocol, 0o600)
                target_y = subjects[held_out][1]
                target_ids = subjects[held_out][2]
                truth_folds.append({
                    "fold": held_out,
                    "sample_ids": target_ids,
                    "labels": [int(value) for value in target_y.tolist()],
                })
                _write_view_receipt(
                    fold, task="T2", role="fold", dataset="SEED",
                    fold=held_out, adapter=SEED_ADAPTER,
                    adapter_version=SEED_ADAPTER_VERSION)

            truth = {
                "version": 1,
                "task": "T2",
                "classes": 3,
                "folds": truth_folds,
            }
            _write_json(sealed_stage / "truth.json", truth, 0o600)

            _chmod_files(public_stage, 0o444)
            _chmod_files(sealed_stage, 0o400)
            public_ledger = _file_ledger(public_stage, exclude={"receipt.json"})
            sealed_ledger = _file_ledger(sealed_stage, exclude={"receipt.json"})
            receipt = _receipt(
                SEED_ADAPTER, SEED_ADAPTER_VERSION, "seed-cross-subject-uda-v1",
                input_sha256, public_ledger, sealed_ledger)
            _write_json(sealed_stage / "receipt.json", receipt, 0o400)
            _chown_tree(public_stage, research_owner)
            _chown_tree(sealed_stage, evaluator_owner)
            _finalize_modes_and_sync(public_stage, file_mode=0o444, dir_mode=0o555)
            _finalize_modes_and_sync(sealed_stage, file_mode=0o400, dir_mode=0o711)
            capability.verify_unchanged()
        _publish_pair(public_stage, sealed_stage, public, sealed)
        return receipt
    except BaseException:
        _writable_remove(public_stage)
        _writable_remove(sealed_stage)
        raise


def _dreamer_rule(label_rule: Any) -> Dict[str, Any]:
    if not isinstance(label_rule, dict) or set(label_rule) != {
            "score", "threshold", "comparison", "neutral_policy"}:
        raise QualificationDataError(
            "label_rule 须精确包含 score/threshold/comparison/neutral_policy")
    score = label_rule["score"]
    comparison = label_rule["comparison"]
    neutral = label_rule["neutral_policy"]
    threshold = label_rule["threshold"]
    if score != "valence":
        raise QualificationDataError("qualification label_rule.score 固定为 valence")
    if comparison != "higher_is_positive":
        raise QualificationDataError(
            "qualification label_rule.comparison 固定为 higher_is_positive")
    if not isinstance(neutral, str) or neutral not in {"drop", "negative", "positive"}:
        raise QualificationDataError("label_rule.neutral_policy 非法")
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))):
        raise QualificationDataError("label_rule.threshold 须为有限数字")
    return {
        "score": score, "threshold": float(threshold),
        "comparison": comparison, "neutral_policy": neutral,
    }


def _as_sequence(value: Any, *, count: int, label: str) -> List[Any]:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    if isinstance(value, (list, tuple)):
        rows = list(value)
    elif isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            rows = list(value.reshape(-1))
        elif value.ndim >= 3 and value.shape[0] == count:
            rows = [value[index] for index in range(count)]
        else:
            rows = list(value.reshape(-1)) if value.ndim == 1 else []
    else:
        rows = []
    if len(rows) != count:
        raise QualificationDataError(f"{label} 须恰含 {count} 条，实收 {len(rows)}")
    return rows


def _scalar_int(value: Any, *, label: str) -> int:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    raw = np.asarray(value)
    if raw.size != 1 or raw.dtype.kind not in "iuf":
        raise QualificationDataError(f"{label} 须为 scalar integer")
    number = float(raw.reshape(-1)[0])
    if not math.isfinite(number) or number != int(number) or number <= 0:
        raise QualificationDataError(f"{label} 须为正整数")
    return int(number)


def _dreamer_eeg(value: Any, *, label: str) -> Any:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    raw = np.asarray(value)
    if raw.dtype.kind not in "iuf" or raw.ndim != 2:
        raise QualificationDataError(f"{label} 须为二维实数 numeric array")
    if raw.shape[0] <= 0 or raw.shape[1] != _DREAMER_CHANNELS:
        raise QualificationDataError(
            f"{label} shape 须为 samples×{_DREAMER_CHANNELS}，实收 {raw.shape}")
    try:
        numeric = np.asarray(raw, dtype=np.float32)
    except (TypeError, ValueError, OverflowError) as error:
        raise QualificationDataError(f"{label} 无法安全转 float32") from error
    if not np.all(np.isfinite(numeric)):
        raise QualificationDataError(f"{label} 含 NaN/Inf")
    return np.ascontiguousarray(numeric)


def _dreamer_opaque_id(secret: bytes, subject: int, record: int) -> str:
    message = (
        "dreamer-public-record-v1\x00"
        f"subject={subject:02d}\x00record={record:02d}"
    ).encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _dreamer_scores(value: Any, *, label: str) -> Any:
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    raw = np.asarray(value)
    scores = np.squeeze(raw)
    if raw.dtype.kind not in "iuf" or scores.shape != (_DREAMER_RECORDS,):
        raise QualificationDataError(f"{label} 须为 18 个 numeric scores")
    try:
        numeric = np.asarray(scores, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise QualificationDataError(f"{label} 非 numeric") from error
    if (not np.all(np.isfinite(numeric))
            or not np.all(numeric == np.rint(numeric))
            or not np.all((numeric >= 1) & (numeric <= 5))):
        raise QualificationDataError(f"{label} 须为 1..5 的有限整数评分")
    return numeric


def _dreamer_payload(payload: Mapping[str, Any], secret: bytes,
                     rule: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Normalize the scipy ``simplify_cells`` DREAMER structure."""
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover
        raise QualificationDataError("qualification data 需要 numpy") from error
    keys = {key for key in payload if not key.startswith("__")}
    if keys != {"DREAMER"} or not isinstance(payload["DREAMER"], dict):
        raise QualificationDataError("DREAMER.mat 顶层字段闭包须恰为 DREAMER struct")
    dreamer = payload["DREAMER"]
    required = {
        "Data", "EEG_SamplingRate", "EEG_Electrodes",
        "noOfSubjects", "noOfVideoSequences"}
    score_key = {
        "valence": "ScoreValence", "arousal": "ScoreArousal",
        "dominance": "ScoreDominance",
    }[rule["score"]]
    if not required <= set(dreamer):
        raise QualificationDataError("DREAMER struct 缺 Data/count/rate")
    if _scalar_int(dreamer["noOfSubjects"], label="DREAMER.noOfSubjects") != _DREAMER_SUBJECTS:
        raise QualificationDataError("DREAMER 须恰有 23 subjects")
    if _scalar_int(dreamer["noOfVideoSequences"], label="DREAMER.noOfVideoSequences") != _DREAMER_RECORDS:
        raise QualificationDataError("DREAMER 每 subject 须恰有 18 records")
    sampling_rate = _scalar_int(
        dreamer["EEG_SamplingRate"], label="DREAMER.EEG_SamplingRate")
    if sampling_rate != 128:
        raise QualificationDataError("DREAMER EEG_SamplingRate 固定为 128 Hz")
    raw_electrodes = np.asarray(dreamer["EEG_Electrodes"]).reshape(-1)
    electrodes = []
    for item in raw_electrodes:
        scalar = np.asarray(item).squeeze()
        if scalar.size != 1:
            raise QualificationDataError("DREAMER EEG_Electrodes shape 非法")
        electrodes.append(str(scalar.item()).strip())
    if tuple(electrodes) != _DREAMER_ELECTRODES:
        raise QualificationDataError("DREAMER EEG_Electrodes 顺序与冻结 14-channel 合同不一致")
    subjects = _as_sequence(dreamer["Data"], count=_DREAMER_SUBJECTS, label="DREAMER.Data")
    records: List[Dict[str, Any]] = []
    seen_ids = set()
    threshold = rule["threshold"]
    for subject_no, subject in enumerate(subjects, start=1):
        if not isinstance(subject, dict) or not isinstance(subject.get("EEG"), dict):
            raise QualificationDataError(f"DREAMER subject {subject_no} 缺 EEG struct")
        if score_key not in subject:
            raise QualificationDataError(
                f"DREAMER subject {subject_no} 缺 {score_key}")
        scores = _dreamer_scores(
            subject[score_key], label=f"DREAMER subject {subject_no} {score_key}")
        eeg = subject["EEG"]
        if "baseline" not in eeg or "stimuli" not in eeg:
            raise QualificationDataError(f"DREAMER subject {subject_no} EEG 缺 baseline/stimuli")
        baselines = _as_sequence(
            eeg["baseline"], count=_DREAMER_RECORDS,
            label=f"DREAMER subject {subject_no} baseline")
        stimuli = _as_sequence(
            eeg["stimuli"], count=_DREAMER_RECORDS,
            label=f"DREAMER subject {subject_no} stimuli")
        for record_no in range(1, _DREAMER_RECORDS + 1):
            opaque = _dreamer_opaque_id(secret, subject_no, record_no)
            if opaque in seen_ids:
                raise QualificationDataError("DREAMER opaque record HMAC 发生碰撞")
            seen_ids.add(opaque)
            score = float(scores[record_no - 1])
            if score == threshold:
                if rule["neutral_policy"] == "drop":
                    label_value = None
                else:
                    label_value = 1 if rule["neutral_policy"] == "positive" else 0
            elif rule["comparison"] == "higher_is_positive":
                label_value = 1 if score > threshold else 0
            else:
                label_value = 1 if score < threshold else 0
            records.append({
                "opaque_id": opaque,
                "baseline": _dreamer_eeg(
                    baselines[record_no - 1],
                    label=f"DREAMER record {subject_no}/{record_no} baseline"),
                "stimuli": _dreamer_eeg(
                    stimuli[record_no - 1],
                    label=f"DREAMER record {subject_no}/{record_no} stimuli"),
                "label": label_value, "group": subject_no,
            })
    records.sort(key=lambda item: item["opaque_id"])
    included = [item["label"] for item in records if item["label"] is not None]
    if not included or set(included) != {0, 1}:
        raise QualificationDataError("DREAMER label_rule 产物须含两个非空 binary classes")
    return records, sampling_rate


def _dreamer_archive_layout(files: Mapping[str, zipfile.ZipInfo]) -> zipfile.ZipInfo:
    matches = [(name, info) for name, info in files.items()
               if PurePosixPath(name).name == "DREAMER.mat"]
    if len(matches) != 1:
        raise QualificationDataError("DREAMER.zip 须恰含一个 DREAMER.mat payload")
    base = PurePosixPath(matches[0][0]).parent
    allowed = {matches[0][0], (base / "DREAMER.pdf").as_posix()}
    if not set(files) <= allowed:
        raise QualificationDataError("DREAMER.zip 只允许 DREAMER.mat 与可选 DREAMER.pdf")
    return matches[0][1]


def prepare_dreamer_view(dreamer_zip: Path | str, public_root: Path | str,
                         sealed_root: Path | str, secret: bytes,
                         label_rule: Mapping[str, Any], *,
                         research_uid: int | None = None,
                         evaluator_uid: int | None = None) -> Dict[str, Any]:
    """Publish unlabeled DREAMER record NPZs and sealed binary truth/groups."""
    key = _validate_secret(secret)
    rule = _dreamer_rule(label_rule)
    research_owner = _validate_uid(research_uid, label="research_uid")
    evaluator_owner = _validate_uid(evaluator_uid, label="evaluator_uid")
    public, sealed = _validate_destinations(
        public_root, sealed_root, research_uid=research_owner,
        evaluator_uid=evaluator_owner)
    public_stage, sealed_stage = _make_stages(public, sealed)
    try:
        with open_artifact(dreamer_zip, label="DREAMER.zip") as capability:
            duplicate = os.dup(capability.fd)
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb", closefd=True) as stream, zipfile.ZipFile(stream) as archive:
                info = _dreamer_archive_layout(_strict_zip_files(archive))
                records, sampling_rate = _dreamer_payload(
                    _load_mat_member(
                        archive, info, simplify=True, spool_dir=public_stage,
                        variable_names=["DREAMER"]),
                    key, rule)
                records = [record for record in records if record["label"] is not None]
            capability.verify_unchanged()
            input_sha256 = capability.identity.content_hash

            record_root = public_stage / "records"
            record_root.mkdir(mode=0o700)
            electrodes = __import__("numpy").asarray(_DREAMER_ELECTRODES, dtype="S3")
            public_files = []
            truth_ids = []
            truth_labels = []
            truth_groups = []
            for record in records:
                opaque = record["opaque_id"]
                name = opaque + ".npz"
                _write_safe_npz(record_root / name, {
                    "baseline": record["baseline"],
                    "stimuli": record["stimuli"],
                    "sampling_rate": __import__("numpy").asarray([sampling_rate], dtype="int32"),
                    "electrodes": electrodes,
                }, 0o600)
                public_files.append(name)
                truth_ids.append(opaque)
                truth_labels.append(int(record["label"]))
                truth_groups.append(int(record["group"]))
            manifest = {
                "adapter": DREAMER_ADAPTER,
                "adapter_version": DREAMER_ADAPTER_VERSION,
                "profile": "dreamer-unlabeled-records-v1",
                "record_format": "safe-npz-v1",
                "record_count": len(public_files),
                "arrays": ["baseline", "electrodes", "sampling_rate", "stimuli"],
                "sampling_rate_hz": sampling_rate,
                "electrodes": list(_DREAMER_ELECTRODES),
                "label_rule": rule,
                "sample_ids": truth_ids,
                "records": public_files,
            }
            _write_json(public_stage / "manifest.json", manifest, 0o600)
            truth = {
                "version": 1,
                "task": "T1",
                "classes": 2,
                "label_rule": rule,
                "units": [{
                    "unit_id": "dreamer",
                    "sample_ids": truth_ids,
                    "labels": truth_labels,
                    "groups": truth_groups,
                }],
            }
            _write_json(sealed_stage / "truth.json", truth, 0o600)
            _write_view_receipt(
                public_stage, task="T1", role="sealed_holdout",
                dataset="DREAMER", fold=None, adapter=DREAMER_ADAPTER,
                adapter_version=DREAMER_ADAPTER_VERSION)

            _chmod_files(public_stage, 0o444)
            _chmod_files(sealed_stage, 0o400)
            public_ledger = _file_ledger(public_stage, exclude={"receipt.json"})
            sealed_ledger = _file_ledger(sealed_stage, exclude={"receipt.json"})
            receipt = _receipt(
                DREAMER_ADAPTER, DREAMER_ADAPTER_VERSION,
                "dreamer-sealed-binary-holdout-v1", input_sha256,
                public_ledger, sealed_ledger)
            _write_json(sealed_stage / "receipt.json", receipt, 0o400)
            _chown_tree(public_stage, research_owner)
            _chown_tree(sealed_stage, evaluator_owner)
            _finalize_modes_and_sync(public_stage, file_mode=0o444, dir_mode=0o555)
            _finalize_modes_and_sync(sealed_stage, file_mode=0o400, dir_mode=0o711)
            capability.verify_unchanged()
        _publish_pair(public_stage, sealed_stage, public, sealed)
        return receipt
    except BaseException:
        _writable_remove(public_stage)
        _writable_remove(sealed_stage)
        raise


def _read_bounded_file(path_value: Path | str, *, label: str,
                       maximum_bytes: int, expected_owner: int | None = None,
                       allowed_modes: frozenset[int] | None = None) -> bytes:
    path = Path(os.path.abspath(os.fspath(path_value)))
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise QualificationDataError(f"{label} 无法安全打开: {path}") from error
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > maximum_bytes
                or (expected_owner is not None and before.st_uid != expected_owner)
                or (allowed_modes is not None
                    and stat.S_IMODE(before.st_mode) not in allowed_modes)):
            raise QualificationDataError(
                f"{label} 须为 1..{maximum_bytes} bytes 的单链接常规文件")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise QualificationDataError(f"{label} 读取时提前截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise QualificationDataError(f"{label} 读取时长度漂移")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise QualificationDataError(f"{label} 读取期间身份漂移")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_label_rule_file(path: Path | str) -> Dict[str, Any]:
    raw = _read_bounded_file(
        path, label="label-rule file", maximum_bytes=16 * 1024)

    def unique(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise QualificationDataError(f"label-rule JSON 重复字段: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                QualificationDataError(f"label-rule JSON 非有限数: {token}")))
    except QualificationDataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationDataError("label-rule file 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise QualificationDataError("label-rule JSON 顶层须为 object")
    return value


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="qualification-data",
        description="Prepare trusted EEG qualification views.")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: Any) -> None:
        command.add_argument("--archive", required=True)
        command.add_argument("--public-root", required=True)
        command.add_argument("--sealed-root", required=True)
        command.add_argument(
            "--secret-file", required=True,
            help="path to raw HMAC secret bytes (the secret itself is never an argv value)")
        command.add_argument("--research-uid", required=True, type=int)
        command.add_argument("--evaluator-uid", required=True, type=int)

    seed = commands.add_parser("prepare-seed")
    common(seed)
    dreamer = commands.add_parser("prepare-dreamer")
    common(dreamer)
    dreamer.add_argument(
        "--label-rule", required=True,
        help="path to the bounded UTF-8 JSON label-rule file")

    args = parser.parse_args(argv)
    try:
        if (os.geteuid() != 0 or args.evaluator_uid != 0
                or args.research_uid <= 0 or args.research_uid == args.evaluator_uid):
            raise QualificationDataError(
                "production prepare CLI 要求由 root 运行、evaluator_uid=0、research_uid 为独立 non-root")
        secret = _read_bounded_file(
            args.secret_file, label="secret file",
            maximum_bytes=_MAX_SECRET_BYTES, expected_owner=0,
            allowed_modes=frozenset({0o400, 0o600}))
        if args.command == "prepare-seed":
            receipt = prepare_seed_views(
                args.archive, args.public_root, args.sealed_root, secret,
                research_uid=args.research_uid,
                evaluator_uid=args.evaluator_uid)
        else:
            receipt = prepare_dreamer_view(
                args.archive, args.public_root, args.sealed_root, secret,
                _read_label_rule_file(args.label_rule),
                research_uid=args.research_uid,
                evaluator_uid=args.evaluator_uid)
    except QualificationDataError as error:
        parser.exit(2, f"qualification-data: {error}\n")
    sys.stdout.buffer.write(_canonical(receipt))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through _main tests
    raise SystemExit(_main())
