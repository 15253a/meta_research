"""Process-wide mutable storage binding for Meta-Research.

The console and direct runner share this module so a Web-spawned quest cannot
silently rebind Codex, package caches, or temporary files inside its private
``0700`` work root.  External-mount validation is deliberately read-only and
runs before any directory, permission, ownership, environment, or
``tempfile`` mutation.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Union


_BOOTSTRAP_FILES = ("auth.json", "config.toml")
_MAX_BOOTSTRAP_FILE_BYTES = 1024 * 1024
_STORAGE_ROOT_ENV = "METARESEARCH_STORAGE_ROOT"


@dataclass(frozen=True)
class _RootAdmission:
    existing: Dict[Path, tuple[int, int]]
    storage_device: int


def _absolute_path(value: Union[str, Path]) -> Path:
    raw = os.path.expanduser(os.fspath(value))
    if not raw or "\x00" in raw:
        raise ValueError("运行时数据根路径非法")
    return Path(os.path.abspath(raw))


def _validate_root_read_only(
        candidate: Path, *, require_external_mount: bool) -> _RootAdmission:
    """Validate a lexical absolute candidate without changing host state."""
    if not candidate.is_absolute() or str(_absolute_path(candidate)) != str(candidate):
        raise ValueError("运行时数据根须为 canonical absolute path")

    current = Path(candidate.anchor)
    nearest = current
    root_info = os.lstat(current)
    existing = {current: (root_info.st_dev, root_info.st_ino)}
    nearest_info = root_info
    for component in candidate.parts[1:]:
        current = current / component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"运行时数据根含 symlink component: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"运行时数据根现有 component 非目录: {current}")
        nearest = current
        nearest_info = info
        existing[current] = (info.st_dev, info.st_ino)

    if not require_external_mount:
        return _RootAdmission(
            existing=existing, storage_device=nearest_info.st_dev)
    for forbidden in (Path("/root"), Path("/tmp")):
        if candidate == forbidden or forbidden in candidate.parents:
            raise ValueError(f"运行时数据根不得位于根盘 {forbidden}: {candidate}")
    if nearest_info.st_dev == os.stat("/").st_dev:
        raise ValueError(
            "运行时数据根仍位于根 overlay，而不是独立数据盘/VEPFS: "
            f"{candidate}（nearest existing ancestor: {nearest}）")
    return _RootAdmission(
        existing=existing, storage_device=nearest_info.st_dev)


def _selected_root(
        requested: Union[str, Path], *, require_external_mount: bool,
        require_requested_root: bool = False,
        private_work_root: Optional[Union[str, Path]] = None,
        ) -> tuple[Path, _RootAdmission]:
    requested_root = _absolute_path(requested)
    marker = os.environ.get(_STORAGE_ROOT_ENV)
    if marker is None:
        selected = requested_root
    else:
        selected = _absolute_path(marker)
        if marker != str(selected) or not Path(marker).is_absolute():
            raise ValueError(
                f"{_STORAGE_ROOT_ENV} 须为 canonical absolute path")
        if require_requested_root and selected != requested_root:
            raise ValueError(
                f"{_STORAGE_ROOT_ENV} 与显式运行时数据根不一致: "
                f"{selected} != {requested_root}")
    admission = _validate_root_read_only(
        selected, require_external_mount=require_external_mount)
    if private_work_root is not None:
        private_root = _absolute_path(private_work_root)
        if selected == private_root or private_root in selected.parents:
            raise ValueError("共享存储根必须位于私有 quest work-root 之外")
    return selected, admission


def _ensure_directory(
        path: Path, *, service_uid: int, uid: int, gid: int,
        mode: int) -> Path:
    """Compatibility helper; production holds one base fd across all setup."""
    canonical = _absolute_path(path)
    admission = _validate_root_read_only(
        canonical, require_external_mount=False)
    fd = _open_admitted_base(
        canonical, admission=admission, service_uid=service_uid,
        uid=uid, gid=gid, mode=mode)
    os.close(fd)
    return canonical


def _open_directory_nofollow(
        path: Path, *, missing_ok: bool = False) -> Optional[int]:
    """Open every directory component by dirfd without following symlinks."""
    canonical = _absolute_path(path)
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    current_fd = os.open(canonical.anchor, flags)
    try:
        for component in canonical.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if missing_ok:
                    os.close(current_fd)
                    return None
                raise
            except OSError as error:
                raise ValueError(
                    f"Codex bootstrap source 含 symlink/非目录 component: "
                    f"{canonical}") from error
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _apply_directory_metadata(
        fd: int, *, service_uid: int, uid: int, gid: int,
        mode: int, expected_device: int, label: Path) -> tuple[int, int]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != expected_device:
        raise ValueError(f"运行时目录身份/设备漂移: {label}")
    if service_uid == 0:
        os.fchown(fd, uid, gid)
    elif (info.st_uid, info.st_gid) != (uid, gid):
        raise ValueError(f"运行时目录 owner 非当前服务 UID: {label}")
    os.fchmod(fd, mode)
    final = os.fstat(fd)
    if (not stat.S_ISDIR(final.st_mode)
            or final.st_dev != expected_device
            or (final.st_uid, final.st_gid) != (uid, gid)
            or stat.S_IMODE(final.st_mode) != mode):
        raise ValueError(f"运行时目录 metadata 设置漂移: {label}")
    return _directory_identity(final)


def _open_admitted_base(
        path: Path, *, admission: _RootAdmission,
        service_uid: int, uid: int, gid: int, mode: int) -> int:
    """Create/open the admitted base and return one pinned directory fd."""
    canonical = _absolute_path(path)
    if canonical == Path(canonical.anchor):
        raise ValueError("运行时数据根不得是文件系统根")
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    current_path = Path(canonical.anchor)
    current_fd = os.open(canonical.anchor, flags)
    try:
        root_expected = admission.existing.get(current_path)
        if (root_expected is None
                or _directory_identity(os.fstat(current_fd)) != root_expected):
            raise ValueError("运行时数据根 admission identity 已漂移")
        for component in canonical.parts[1:]:
            current_path = current_path / component
            expected = admission.existing.get(current_path)
            if expected is None:
                try:
                    os.mkdir(component, mode, dir_fd=current_fd)
                except FileExistsError as error:
                    raise ValueError(
                        f"运行时数据根在 admission 后出现新 component: "
                        f"{current_path}") from error
                created = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False)
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise ValueError(
                        f"运行时数据根新 component 身份非法: "
                        f"{current_path}") from error
                if _directory_identity(os.fstat(next_fd)) != _directory_identity(created):
                    os.close(next_fd)
                    raise ValueError(
                        f"运行时数据根新 component 创建身份漂移: {current_path}")
                _apply_directory_metadata(
                    next_fd, service_uid=service_uid, uid=uid, gid=gid,
                    mode=mode, expected_device=admission.storage_device,
                    label=current_path)
            else:
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise ValueError(
                        f"运行时数据根含 symlink/非目录 component: "
                        f"{current_path}") from error
                if _directory_identity(os.fstat(next_fd)) != expected:
                    os.close(next_fd)
                    raise ValueError(
                        f"运行时数据根 admission component 已漂移: "
                        f"{current_path}")
            os.close(current_fd)
            current_fd = next_fd
        identity = _apply_directory_metadata(
            current_fd, service_uid=service_uid, uid=uid, gid=gid,
            mode=mode, expected_device=admission.storage_device,
            label=canonical)
        rebound_fd = _open_directory_nofollow(canonical)
        assert rebound_fd is not None
        try:
            if _directory_identity(os.fstat(rebound_fd)) != identity:
                raise ValueError("运行时数据根最终 pathname 身份漂移")
        finally:
            os.close(rebound_fd)
        return current_fd
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


class _StorageTree:
    """Pinned base-fd authority for every runtime-layout descendant."""

    def __init__(
            self, base: Path, base_fd: int, *,
            service_uid: int, service_gid: int):
        self.base = base
        self.base_fd = base_fd
        self.service_uid = service_uid
        self.service_gid = service_gid
        info = os.fstat(base_fd)
        self.device = info.st_dev
        self.identity = _directory_identity(info)

    def close(self) -> None:
        if self.base_fd >= 0:
            os.close(self.base_fd)
            self.base_fd = -1

    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        canonical = _absolute_path(path)
        try:
            relative = canonical.relative_to(self.base)
        except ValueError as error:
            raise ValueError("运行时目录逃逸 storage base") from error
        if relative == Path("."):
            return ()
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("运行时目录 relative path 非法")
        return relative.parts

    def _open(
            self, path: Path, *, create: bool,
            uid: Optional[int] = None, gid: Optional[int] = None,
            mode: Optional[int] = None) -> int:
        parts = self._relative_parts(path)
        current_fd = os.dup(self.base_fd)
        flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                 | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        try:
            for component in parts:
                created = False
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    assert mode is not None
                    try:
                        os.mkdir(component, mode, dir_fd=current_fd)
                    except FileExistsError as error:
                        raise ValueError(
                            f"运行时目录创建竞争漂移: {path}") from error
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                    created = True
                except OSError as error:
                    raise ValueError(
                        f"运行时目录含 symlink/非目录 component: {path}") from error
                info = os.fstat(next_fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_dev != self.device:
                    os.close(next_fd)
                    raise ValueError(f"运行时目录身份/设备非法: {path}")
                if created:
                    assert uid is not None and gid is not None and mode is not None
                    _apply_directory_metadata(
                        next_fd, service_uid=self.service_uid,
                        uid=uid, gid=gid, mode=mode,
                        expected_device=self.device, label=path)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            try:
                os.close(current_fd)
            except OSError:
                pass
            raise

    def ensure(self, path: Path, *, uid: int, gid: int, mode: int) -> Path:
        fd = self._open(
            path, create=True, uid=uid, gid=gid, mode=mode)
        try:
            _apply_directory_metadata(
                fd, service_uid=self.service_uid, uid=uid, gid=gid,
                mode=mode, expected_device=self.device, label=path)
        finally:
            os.close(fd)
        return _absolute_path(path)

    def open_directory(self, path: Path) -> int:
        return self._open(path, create=False)

    def verify_base_path(self) -> None:
        rebound_fd = _open_directory_nofollow(self.base)
        assert rebound_fd is not None
        try:
            if _directory_identity(os.fstat(rebound_fd)) != self.identity:
                raise ValueError("运行时数据根最终 binding 身份漂移")
        finally:
            os.close(rebound_fd)


def _read_bounded_source_file(source_fd: int, name: str) -> Optional[bytes]:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(name, flags, dir_fd=source_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(
            f"Codex bootstrap {name} 须为无 symlink 的普通文件") from error
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_size > _MAX_BOOTSTRAP_FILE_BYTES):
            raise ValueError(f"Codex bootstrap {name} 非有界普通文件")
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError(f"Codex bootstrap {name} 读取漂移")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise ValueError(f"Codex bootstrap {name} 读取期间增长")
        after = os.fstat(fd)
        stable_before = (
            before.st_dev, before.st_ino, before.st_mode,
            before.st_uid, before.st_gid, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev, after.st_ino, after.st_mode,
            after.st_uid, after.st_gid, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if stable_after != stable_before:
            raise ValueError(f"Codex bootstrap {name} 读取身份漂移")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _auth_refresh_time(payload: bytes) -> Optional[datetime]:
    try:
        value = json.loads(payload).get("last_refresh")
        if not isinstance(value, str):
            return None
        value = re.sub(
            r"(\.\d{6})\d+(?=Z|[+-]\d{2}:\d{2}$)", r"\1", value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _replace_bootstrap_file(
        destination_fd: int, name: str, payload: bytes, *,
        service_uid: int, uid: int, gid: int) -> None:
    temporary = f".{name}.refresh-{os.getpid()}-{secrets.token_hex(8)}"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(temporary, flags, 0o600, dir_fd=destination_fd)
    installed = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Codex bootstrap short write")
            view = view[written:]
        os.fchmod(fd, 0o600)
        if service_uid == 0:
            os.fchown(fd, uid, gid)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary, name,
            src_dir_fd=destination_fd, dst_dir_fd=destination_fd)
        os.fsync(destination_fd)
        installed = True
    finally:
        if fd >= 0:
            os.close(fd)
        if not installed:
            try:
                os.unlink(temporary, dir_fd=destination_fd)
            except FileNotFoundError:
                pass


def _seed_codex_identity(
        destination: Path, source: Optional[str], *,
        service_uid: int, uid: int, gid: int,
        destination_fd: Optional[int] = None) -> None:
    """Copy bounded auth/config bytes pinned to one no-follow source fd."""
    if destination_fd is None:
        owned_destination_fd = _open_directory_nofollow(destination)
        assert owned_destination_fd is not None
    else:
        owned_destination_fd = os.dup(destination_fd)
    source_fd = None
    try:
        if source:
            source_fd = _open_directory_nofollow(
                _absolute_path(source), missing_ok=True)
        for name in _BOOTSTRAP_FILES:
            try:
                info = os.stat(
                    name, dir_fd=owned_destination_fd,
                    follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if (not stat.S_ISREG(info.st_mode)
                        or info.st_uid != uid or info.st_gid != gid
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ValueError(f"项目 Codex {name} 身份/权限非法")
                if name == "auth.json" and source_fd is not None:
                    source_payload = _read_bounded_source_file(source_fd, name)
                    destination_payload = _read_bounded_source_file(
                        owned_destination_fd, name)
                    source_refresh = (
                        _auth_refresh_time(source_payload)
                        if source_payload is not None else None)
                    destination_refresh = (
                        _auth_refresh_time(destination_payload)
                        if destination_payload is not None else None)
                    if (source_refresh is not None
                            and destination_refresh is not None
                            and source_refresh > destination_refresh):
                        _replace_bootstrap_file(
                            owned_destination_fd, name, source_payload,
                            service_uid=service_uid, uid=uid, gid=gid)
                continue
            if source_fd is None:
                continue
            payload = _read_bounded_source_file(source_fd, name)
            if payload is None:
                continue
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(
                name, flags, 0o600, dir_fd=owned_destination_fd)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Codex bootstrap short write")
                    view = view[written:]
                os.fchmod(fd, 0o600)
                if service_uid == 0:
                    os.fchown(fd, uid, gid)
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                try:
                    os.unlink(name, dir_fd=owned_destination_fd)
                except FileNotFoundError:
                    pass
                raise
            else:
                os.close(fd)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(owned_destination_fd)


def _configure_admitted_storage(
        base: Path, admission: _RootAdmission, *,
        service_uid: int, service_gid: int,
        query_account: pwd.struct_passwd,
        prior_service_codex_home: Optional[str],
        prior_query_codex_home: Optional[str]) -> Dict[str, str]:
    base_fd = _open_admitted_base(
        base, admission=admission, service_uid=service_uid,
        uid=service_uid, gid=service_gid, mode=0o711)
    tree = _StorageTree(
        base, base_fd, service_uid=service_uid, service_gid=service_gid)
    try:
        process_tmp = tree.ensure(
            base / ".process-tmp",
            uid=service_uid, gid=service_gid, mode=0o711)
        cache_root = tree.ensure(
            base / ".process-cache",
            uid=service_uid, gid=service_gid, mode=0o711)
        home_root = tree.ensure(
            base / ".process-home",
            uid=service_uid, gid=service_gid, mode=0o711)
        codex_root = tree.ensure(
            base / ".codex-runtime",
            uid=service_uid, gid=service_gid, mode=0o711)

        service_cache = tree.ensure(
            cache_root / "service",
            uid=service_uid, gid=service_gid, mode=0o700)
        query_cache = tree.ensure(
            cache_root / "query",
            uid=query_account.pw_uid, gid=query_account.pw_gid, mode=0o700)
        service_home = tree.ensure(
            home_root / "service",
            uid=service_uid, gid=service_gid, mode=0o700)
        query_home = tree.ensure(
            home_root / "query",
            uid=query_account.pw_uid, gid=query_account.pw_gid, mode=0o700)
        service_codex_home = tree.ensure(
            codex_root / "service",
            uid=service_uid, gid=service_gid, mode=0o700)
        query_codex_home = tree.ensure(
            codex_root / "query",
            uid=query_account.pw_uid, gid=query_account.pw_gid, mode=0o700)
        service_codex_sqlite = tree.ensure(
            codex_root / "service-sqlite",
            uid=service_uid, gid=service_gid, mode=0o700)
        query_codex_sqlite = tree.ensure(
            codex_root / "query-sqlite",
            uid=query_account.pw_uid, gid=query_account.pw_gid, mode=0o700)

        service_destination_fd = tree.open_directory(service_codex_home)
        try:
            _seed_codex_identity(
                service_codex_home, prior_service_codex_home,
                service_uid=service_uid, uid=service_uid, gid=service_gid,
                destination_fd=service_destination_fd)
        finally:
            os.close(service_destination_fd)
        query_destination_fd = tree.open_directory(query_codex_home)
        try:
            _seed_codex_identity(
                query_codex_home, prior_query_codex_home,
                service_uid=service_uid,
                uid=query_account.pw_uid, gid=query_account.pw_gid,
                destination_fd=query_destination_fd)
        finally:
            os.close(query_destination_fd)
        service_destination_fd = tree.open_directory(service_codex_home)
        try:
            _seed_codex_identity(
                service_codex_home, str(query_codex_home),
                service_uid=service_uid, uid=service_uid, gid=service_gid,
                destination_fd=service_destination_fd)
        finally:
            os.close(service_destination_fd)
        query_destination_fd = tree.open_directory(query_codex_home)
        try:
            _seed_codex_identity(
                query_codex_home, str(service_codex_home),
                service_uid=service_uid,
                uid=query_account.pw_uid, gid=query_account.pw_gid,
                destination_fd=query_destination_fd)
        finally:
            os.close(query_destination_fd)

        service_paths = {
            "TMPDIR": process_tmp,
            "TMP": process_tmp,
            "TEMP": process_tmp,
            "HOME": service_home,
            "CODEX_HOME": service_codex_home,
            "CODEX_SQLITE_HOME": service_codex_sqlite,
            "XDG_CACHE_HOME": service_cache,
            "PIP_CACHE_DIR": service_cache / "pip",
            "HF_HOME": service_cache / "huggingface",
            "HF_HUB_CACHE": service_cache / "huggingface" / "hub",
            "HF_DATASETS_CACHE": service_cache / "huggingface" / "datasets",
            "TRANSFORMERS_CACHE": service_cache / "huggingface" / "transformers",
            "TORCH_HOME": service_cache / "torch",
            "TORCH_EXTENSIONS_DIR": service_cache / "torch-extensions",
            "TRITON_CACHE_DIR": service_cache / "triton",
            "XDG_CONFIG_HOME": service_home / ".config",
            "XDG_DATA_HOME": service_home / ".local" / "share",
            "XDG_STATE_HOME": service_home / ".local" / "state",
            "CONDA_PKGS_DIRS": service_cache / "conda-pkgs",
            "CONDA_ENVS_PATH": base / "environments",
            "UV_CACHE_DIR": service_cache / "uv",
            "CUDA_CACHE_PATH": service_cache / "cuda",
            "MPLCONFIGDIR": service_cache / "matplotlib",
            "NUMBA_CACHE_DIR": service_cache / "numba",
            "PYTHONPYCACHEPREFIX": service_cache / "pycache",
        }
        for path in dict.fromkeys(
                path for path in service_paths.values()
                if path != process_tmp):
            tree.ensure(
                path, uid=service_uid, gid=service_gid, mode=0o700)

        environment = {
            **{key: str(path) for key, path in service_paths.items()},
            "METARESEARCH_QUERY_HOME": str(query_home),
            "METARESEARCH_QUERY_CODEX_HOME": str(query_codex_home),
            "METARESEARCH_QUERY_CODEX_SQLITE_HOME": str(query_codex_sqlite),
            "METARESEARCH_QUERY_CACHE_HOME": str(query_cache),
            _STORAGE_ROOT_ENV: str(base),
        }
        # Re-open the public pathname only after every mutation while the
        # original base fd remains live.  A rename/symlink/replacement cannot
        # redirect any descendant operation and is rejected before env bind.
        tree.verify_base_path()
        os.environ.update(environment)
        if "SSL_CERT_FILE" not in os.environ:
            prefix = Path(sys.prefix).resolve(strict=True)
            certificate = prefix / "ssl" / "cert.pem"
            if (certificate.is_file()
                    and os.path.commonpath((
                        str(prefix), str(certificate.resolve(strict=True))))
                    == str(prefix)):
                os.environ["SSL_CERT_FILE"] = str(certificate)

        # ``tempfile`` caches the selected root independently of the environment.
        tempfile.tempdir = str(process_tmp)
        return environment
    finally:
        tree.close()


def configure_process_storage(
        root: Union[str, Path], *,
        require_external_mount: bool = True,
        require_requested_root: bool = False,
        private_work_root: Optional[Union[str, Path]] = None) -> Dict[str, str]:
    """Bind mutable process state below one canonical shared storage root.

    An inherited ``METARESEARCH_STORAGE_ROOT`` is authoritative.  This is what
    keeps a Web-spawned quest child on the console registry root when its own
    work root later becomes private under :class:`InstanceLease`.
    """
    base, admission = _selected_root(
        root, require_external_mount=require_external_mount,
        require_requested_root=require_requested_root,
        private_work_root=private_work_root)

    # Account lookup and source selection are read-only.  Keep them before the
    # first mkdir/chmod/chown so every validation/admission rejection is clean.
    service_uid, service_gid = os.geteuid(), os.getegid()
    requested_query_user = os.environ.get("METARESEARCH_QUERY_RUN_AS_USER")
    try:
        if requested_query_user is None:
            requested_query_user = (
                "codexro" if service_uid == 0
                else pwd.getpwuid(service_uid).pw_name)
        query_account = pwd.getpwnam(requested_query_user)
    except KeyError as error:
        raise ValueError("Codex query 运行账户/服务 UID 不存在") from error
    if service_uid != 0 and query_account.pw_uid != service_uid:
        raise ValueError("non-root 服务不得把 Codex 运行目录交给其他 UID")
    prior_service_codex_home = os.environ.get("CODEX_HOME")
    prior_query_codex_home = os.environ.get("METARESEARCH_QUERY_CODEX_HOME")
    if prior_query_codex_home is None:
        prior_query_codex_home = str(Path(query_account.pw_dir) / ".codex")
    return _configure_admitted_storage(
        base, admission, service_uid=service_uid, service_gid=service_gid,
        query_account=query_account,
        prior_service_codex_home=prior_service_codex_home,
        prior_query_codex_home=prior_query_codex_home)
