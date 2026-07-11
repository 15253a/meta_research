"""Bounded HTTPS transport and redirect policy for pinned GitHub repositories."""
from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Sequence

from .artifact_capability import ArtifactCapabilityError, open_artifact
from .repository_materialization_common import (
    _SHA256_RE, RepositoryMaterializationError, RepositoryTransportError,
    _strict_json,
)


class _ApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != "api.github.com":
            raise urllib.error.HTTPError(
                req.full_url, code, "GitHub API redirect escaped api.github.com",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Sequence[str]):
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != "https" or target.hostname not in self.allowed_hosts
                or target.username or target.password or target.port not in (None, 443)):
            raise urllib.error.HTTPError(
                req.full_url, code, "GitHub archive redirect escaped allowlist",
                headers, fp)
        clean_headers = {
            key: value for key, value in req.headers.items()
            if key.lower() != "authorization"}
        return urllib.request.Request(
            newurl, headers=clean_headers, method="GET",
            origin_req_host=req.origin_req_host, unverifiable=True)


class _RepositoryTransportMixin:
    """Host contract: config/token/guards, injectable fetchers, and URL openers."""

    def _headers(self) -> Dict[str, str]:
        result = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meta-research-materializer/1",
        }
        token = os.environ.get(self.token_env)
        if token:
            result["Authorization"] = f"Bearer {token}"
        return result

    def _get_json(self, url: str, *, label: str) -> Any:
        self.owner_guard()
        if self.api_getter is not None:
            value = self.api_getter(url, label)
            self.owner_guard()
            return value
        request = urllib.request.Request(
            url, headers=self._headers(), method="GET")
        try:
            response = self._api_opener(
                request, timeout=float(self.config["timeout_s"]))
        except urllib.error.HTTPError as error:
            try:
                error_type = (RepositoryMaterializationError
                              if error.code in (404, 410)
                              else RepositoryTransportError)
                raise error_type(f"GitHub {label} HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError(
                f"GitHub {label} 读取失败: {type(error).__name__}") from error
        try:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "api.github.com":
                raise RepositoryMaterializationError(
                    f"GitHub {label} final URL 越出 api.github.com")
            maximum = int(self.config["max_api_response_bytes"])
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RepositoryMaterializationError(
                        f"GitHub {label} Content-Length 非整数") from error
                if declared_size < 0 or declared_size > maximum:
                    raise RepositoryMaterializationError(
                        f"GitHub {label} 响应超过上限")
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise RepositoryMaterializationError(
                    f"GitHub {label} 响应超过上限")
        finally:
            response.close()
        try:
            self.owner_guard()
            return _strict_json(raw, label=f"GitHub {label}")
        except RepositoryMaterializationError as error:
            raise RepositoryTransportError(
                f"GitHub {label} 响应不是可解析 JSON") from error

    def _download_archive(
            self, full_name: str, revision: str, destination: Path) -> Dict[str, Any]:
        self.owner_guard()
        maximum = int(self.config["max_archive_bytes"])
        if self.archive_fetcher is not None:
            result = dict(self.archive_fetcher(
                full_name, revision, destination, maximum))
            if (set(result) != {"url", "bytes", "sha256"}
                    or not destination.is_file()):
                raise RepositoryMaterializationError(
                    "test/injected archive_fetcher contract 非法")
            try:
                declared_size = result["bytes"]
                declared_hash = result["sha256"]
                if (isinstance(declared_size, bool)
                        or not isinstance(declared_size, int)
                        or not 0 < declared_size <= maximum
                        or not isinstance(declared_hash, str)
                        or _SHA256_RE.fullmatch(declared_hash) is None
                        or result["url"] != (
                            f"https://api.github.com/repos/{full_name}/tarball/"
                            f"{revision}")):
                    raise RepositoryMaterializationError(
                        "test/injected archive_fetcher receipt 非法")
                with open_artifact(
                        destination, expected_hash=declared_hash,
                        expected_size=declared_size,
                        label="injected GitHub archive"):
                    pass
            except ArtifactCapabilityError as error:
                raise RepositoryMaterializationError(
                    "test/injected archive_fetcher bytes 与 receipt 不一致") from error
            self.owner_guard()
            return result
        url = f"https://api.github.com/repos/{full_name}/tarball/{revision}"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            response = self._archive_opener(
                request, timeout=float(self.config["timeout_s"]))
        except urllib.error.HTTPError as error:
            try:
                error_type = (RepositoryMaterializationError
                              if error.code in (404, 410)
                              else RepositoryTransportError)
                raise error_type(f"GitHub archive HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError(
                f"GitHub archive 读取失败: {type(error).__name__}") from error
        digest = hashlib.sha256()
        total = 0
        try:
            final = urllib.parse.urlsplit(response.geturl())
            if (final.scheme != "https"
                    or final.hostname not in self.config["allowed_archive_hosts"]
                    or final.username or final.password or final.port not in (None, 443)):
                raise RepositoryMaterializationError(
                    "GitHub archive final URL 越出 allowlist")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RepositoryMaterializationError(
                        "GitHub archive Content-Length 非整数") from error
                if declared_size < 0 or declared_size > maximum:
                    raise RepositoryMaterializationError(
                        "GitHub archive 压缩字节超过上限")
            fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600)
            try:
                while True:
                    self.owner_guard()
                    chunk = response.read(min(1024 * 1024, maximum - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise RepositoryMaterializationError(
                            "GitHub archive 压缩字节超过上限")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("archive short write")
                        view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            response.close()
        self.owner_guard()
        return {"url": url, "bytes": total,
                "sha256": "sha256:" + digest.hexdigest()}
