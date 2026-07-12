"""Shared constants and bounded filesystem/network primitives for dependency images."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from .repository_materialization_common import (
    RepositoryCacheError,
    _fsync_directory,
)


_PROVIDER = "python-wheel-image-v1"
_LOCK_VERSION = 1
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\.whl$")
_CLOSURE_LABEL = "org.meta-research.dependency-closure"
_FSIZE_EXEC = (
    "import os,resource,sys;"
    "n=int(sys.argv[1]);resource.setrlimit(resource.RLIMIT_FSIZE,(n,n));"
    "os.execv(sys.argv[2],sys.argv[2:])")
_RUNTIME_PROBE = r"""
import hashlib,json,os,sys
manifest_path,root,version=sys.argv[1:4]
if sys.implementation.name!='cpython' or '.'.join(map(str,sys.version_info[:3]))!=version:
    raise SystemExit(31)
with open(manifest_path,'rb') as fh:
    manifest=json.load(fh)
expected={item['path']:(item['sha256'],item['bytes']) for item in manifest['files']}
actual={}
for current,dirs,files in os.walk(root,followlinks=False):
    for name in dirs:
        path=os.path.join(current,name)
        if os.path.islink(path): raise SystemExit(32)
    for name in files:
        path=os.path.join(current,name)
        if os.path.islink(path): raise SystemExit(33)
        rel=os.path.relpath(path,root).replace(os.sep,'/')
        digest=hashlib.sha256(); size=0
        with open(path,'rb') as fh:
            while True:
                chunk=fh.read(1024*1024)
                if not chunk: break
                digest.update(chunk); size+=len(chunk)
        actual[rel]=('sha256:'+digest.hexdigest(),size)
if actual!=expected: raise SystemExit(34)
payload={'implementation':sys.implementation.name,'version':version,
         'executable':sys.executable,'installed_manifest_hash':manifest['manifest_hash']}
with open('/mr/output/runtime.json','w',encoding='utf-8',newline='\n') as fh:
    json.dump(payload,fh,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    fh.write('\n')
""".strip()


class _WheelRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Sequence[str]):
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            old = urllib.parse.urlsplit(req.full_url)
            new = urllib.parse.urlsplit(newurl)
            old_origin = (old.scheme, old.hostname, old.port or 443)
            new_origin = (new.scheme, new.hostname, new.port or 443)
        except ValueError:
            raise urllib.error.HTTPError(
                newurl, code, "dependency wheel malformed redirect rejected", headers, fp)
        if old_origin != new_origin or new.scheme != "https":
            raise urllib.error.HTTPError(
                newurl, code, "dependency wheel cross-origin redirect rejected", headers, fp)
        if (new.hostname not in self.allowed_hosts or new.port not in (None, 443)
                or not _wheel_url_is_allowed(newurl, self.allowed_hosts)):
            raise urllib.error.HTTPError(
                newurl, code, "dependency wheel redirect host rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _wheel_url_is_allowed(
        value: object, allowed_hosts: Sequence[str], *,
        filename: Optional[str] = None) -> bool:
    """Validate the literal URL before urllib gets a chance to normalize it."""
    if (not isinstance(value, str) or not value
            or any(ord(character) < 0x20 or ord(character) == 0x7F
                   for character in value)
            or "?" in value or "#" in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not parsed.path.startswith("/")):
        return False
    if filename is not None:
        try:
            basename = urllib.parse.unquote(
                PurePosixPath(parsed.path).name, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
        if basename != filename:
            return False
    return True


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _hash_file(
        path: Path, *, maximum: Optional[int] = None,
        progress_guard: Optional[Callable[[], None]] = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RepositoryCacheError(f"dependency artifact 非单链接常规文件: {path.name}")
        if maximum is not None and info.st_size > maximum:
            raise RepositoryCacheError(f"dependency artifact 超过上限: {path.name}")
        while total < info.st_size:
            if progress_guard is not None:
                progress_guard()
            chunk = os.read(fd, min(1024 * 1024, info.st_size - total))
            if not chunk:
                raise RepositoryCacheError(f"dependency artifact 读取截断: {path.name}")
            digest.update(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest(), total


def _write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("dependency image short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)
