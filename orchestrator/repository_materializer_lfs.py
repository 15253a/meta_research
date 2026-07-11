"""Exact Git LFS pointer, Batch API, and object materialization."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .artifact_capability import (
    ArtifactCapabilityError, open_artifact, read_artifact_bytes,
)
from .repository_materialization_common import (
    _COMMIT_RE, RepositoryMaterializationError,
    RepositoryTransportError, _bounded_string, _canonical, _fsync_directory,
    _git_blob_sha1, _parse_lfs_pointer, _sha256, _strict_json, _value_hash,
)

_LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"
_LFS_RESPONSE_MEDIA_TYPES = frozenset({_LFS_MEDIA_TYPE, "application/json"})
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_FORBIDDEN_ACTION_HEADERS = frozenset({
    "connection", "content-length", "cookie", "host", "proxy-authorization",
    "proxy-connection", "set-cookie", "transfer-encoding",
})


class _LfsBatchRedirectHandler(urllib.request.HTTPRedirectHandler):
    """The repository-derived Batch endpoint is exact; never rewrite its POST."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, code, "Git LFS Batch endpoint redirected", headers, fp)


class _LfsObjectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Sequence[str]):
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            target = urllib.parse.urlsplit(newurl)
            invalid_target = (
                target.scheme != "https" or target.hostname not in self.allowed_hosts
                or target.username or target.password
                or target.port not in (None, 443))
        except ValueError:
            invalid_target = True
        if invalid_target:
            raise urllib.error.HTTPError(
                req.full_url, code, "Git LFS object redirect escaped allowlist",
                headers, fp)
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        if old_host != target.hostname and req.headers:
            raise urllib.error.HTTPError(
                req.full_url, code,
                "Git LFS credential-bearing action redirected cross-origin",
                headers, fp)
        clean_headers = dict(req.headers) if old_host == target.hostname else {}
        return urllib.request.Request(
            newurl, headers=clean_headers, method="GET",
            origin_req_host=req.origin_req_host, unverifiable=True)


class _RepositoryLfsMixin:
    """Host contract: config/token/openers, bounded API transport, and owner guard."""

    def _lfs_auth_header(self) -> Optional[str]:
        token = os.environ.get(self.token_env)
        if not token:
            return None
        try:
            encoded = token.encode("utf-8")
        except UnicodeEncodeError as error:
            raise RepositoryTransportError("Git LFS token 非 UTF-8") from error
        if (not encoded or len(encoded) > 4096
                or any(byte < 0x20 or byte == 0x7f for byte in encoded)):
            raise RepositoryTransportError("Git LFS token 超出凭据边界")
        credential = base64.b64encode(b"x-access-token:" + encoded).decode("ascii")
        return "Basic " + credential

    def _git_blob_lfs_pointer(
            self, full_name: str, blob_sha: str,
            expected_size: int) -> Optional[Dict[str, Any]]:
        if (_COMMIT_RE.fullmatch(blob_sha) is None
                or not 0 <= expected_size < 1024):
            raise RepositoryMaterializationError("Git LFS pointer blob identity 非法")
        try:
            payload = self._get_json(
                f"https://api.github.com/repos/{full_name}/git/blobs/{blob_sha}",
                label=f"LFS pointer blob {full_name}:{blob_sha}")
        except RepositoryMaterializationError as error:
            raise RepositoryTransportError(
                "Git LFS pointer blob endpoint 与已验证 tree 不一致") from error
        if (not isinstance(payload, dict) or payload.get("sha") != blob_sha
                or payload.get("size") != expected_size
                or payload.get("encoding") != "base64"
                or not isinstance(payload.get("content"), str)):
            raise RepositoryTransportError("Git LFS pointer blob response 非法")
        try:
            raw = base64.b64decode(
                "".join(payload["content"].split()), validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise RepositoryTransportError(
                "Git LFS pointer blob base64 非法") from error
        if (len(raw) != expected_size or _git_blob_sha1(raw) != blob_sha):
            raise RepositoryTransportError(
                "Git LFS pointer blob bytes 与 Git identity 不一致")
        pointer = _parse_lfs_pointer(raw)
        if pointer is None:
            return None
        return {
            **pointer, "pointer_sha256": _sha256(raw),
            "pointer_bytes": len(raw),
        }

    @staticmethod
    def _validate_lfs_action(
            value: Any, *, allowed_hosts: Sequence[str]) -> Dict[str, Any]:
        allowed_keys = {"href", "header", "expires_in", "expires_at"}
        if (not isinstance(value, dict) or "href" not in value
                or not set(value) <= allowed_keys):
            raise RepositoryMaterializationError("Git LFS download action 非法")
        href = _bounded_string(
            value["href"], field="Git LFS download href", max_bytes=16384)
        try:
            target = urllib.parse.urlsplit(href)
            invalid_target = (
                target.scheme != "https"
                or target.hostname not in set(allowed_hosts)
                or target.username or target.password
                or target.port not in (None, 443) or target.fragment)
        except ValueError as error:
            raise RepositoryMaterializationError(
                "Git LFS download href authority 非法") from error
        if invalid_target:
            raise RepositoryMaterializationError(
                "Git LFS download href 越出 HTTPS host allowlist")
        raw_headers = value.get("header", {})
        if not isinstance(raw_headers, dict) or len(raw_headers) > 64:
            raise RepositoryMaterializationError("Git LFS action headers 非法")
        headers = {}
        for key, raw in raw_headers.items():
            if (not isinstance(key, str) or _HEADER_NAME_RE.fullmatch(key) is None
                    or key.lower() in _FORBIDDEN_ACTION_HEADERS
                    or key.lower().startswith(("x-forwarded-", "proxy-"))
                    or not isinstance(raw, str)):
                raise RepositoryMaterializationError(
                    "Git LFS action header name/value 非法")
            try:
                encoded = raw.encode("utf-8")
            except UnicodeEncodeError as error:
                raise RepositoryMaterializationError(
                    "Git LFS action header 非 UTF-8") from error
            if (len(encoded) > 8192
                    or any(byte < 0x20 and byte != 0x09 for byte in encoded)
                    or 0x7f in encoded):
                raise RepositoryMaterializationError(
                    "Git LFS action header value 超界")
            headers[key] = raw
        expires_in = value.get("expires_in")
        if (expires_in is not None and (
                isinstance(expires_in, bool) or not isinstance(expires_in, int)
                or not -2147483647 <= expires_in <= 2147483647)):
            raise RepositoryMaterializationError("Git LFS expires_in 非法")
        expires_at = value.get("expires_at")
        if expires_at is not None:
            _bounded_string(
                expires_at, field="Git LFS expires_at", max_bytes=128)
        return {"href": href, "headers": headers}

    def _lfs_batch_actions(
            self, full_name: str, revision: str,
            objects: Sequence[Mapping[str, Any]]) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        requested = {
            item["oid"].removeprefix("sha256:"): item["size"]
            for item in objects
        }
        request_value = {
            "operation": "download", "transfers": ["basic"],
            "objects": [{"oid": oid, "size": requested[oid]}
                        for oid in sorted(requested)],
            "hash_algo": "sha256",
        }
        batch_url = (
            f"https://github.com/{full_name}.git/info/lfs/objects/batch")
        request_hash = _value_hash(request_value)
        if self.lfs_batch_getter is not None:
            value = self.lfs_batch_getter(full_name, revision, request_value)
            response_hash = _value_hash(value)
        else:
            payload = _canonical(request_value)
            headers = {
                "Accept": _LFS_MEDIA_TYPE,
                "Content-Type": _LFS_MEDIA_TYPE,
                "User-Agent": "meta-research-materializer/1",
            }
            authorization = self._lfs_auth_header()
            if authorization is not None:
                headers["Authorization"] = authorization
            request = urllib.request.Request(
                batch_url, data=payload, headers=headers, method="POST")
            try:
                response = self._lfs_batch_opener(
                    request, timeout=float(self.config["timeout_s"]))
            except urllib.error.HTTPError as error:
                try:
                    raise RepositoryTransportError(
                        f"Git LFS Batch HTTP {error.code}") from error
                finally:
                    error.close()
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise RepositoryTransportError(
                    f"Git LFS Batch 读取失败: {type(error).__name__}") from error
            try:
                try:
                    final = urllib.parse.urlsplit(response.geturl())
                    invalid_final = (
                        final.scheme != "https" or final.hostname != "github.com"
                        or final.username or final.password
                        or final.port not in (None, 443)
                        or final.path != urllib.parse.urlsplit(batch_url).path)
                except ValueError as error:
                    raise RepositoryTransportError(
                        "Git LFS Batch final URL authority 非法") from error
                if invalid_final:
                    raise RepositoryTransportError(
                        "Git LFS Batch final URL 漂移")
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if media_type not in _LFS_RESPONSE_MEDIA_TYPES:
                    raise RepositoryTransportError(
                        "Git LFS Batch Content-Type 非协议 JSON")
                maximum = int(self.config["max_api_response_bytes"])
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as error:
                        raise RepositoryTransportError(
                            "Git LFS Batch Content-Length 非整数") from error
                    if declared_size < 0 or declared_size > maximum:
                        raise RepositoryTransportError(
                            "Git LFS Batch response 超过上限")
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise RepositoryTransportError(
                        "Git LFS Batch response 超过上限")
            finally:
                response.close()
            response_hash = _sha256(raw)
            try:
                value = _strict_json(raw, label="Git LFS Batch response")
            except RepositoryMaterializationError as error:
                raise RepositoryTransportError(
                    "Git LFS Batch response 非严格 JSON") from error
        if (not isinstance(value, dict)
                or not set(value) <= {"transfer", "objects", "hash_algo"}
                or value.get("transfer", "basic") != "basic"
                or value.get("hash_algo", "sha256") != "sha256"
                or not isinstance(value.get("objects"), list)
                or len(value["objects"]) != len(requested)):
            raise RepositoryTransportError(
                "Git LFS Batch response 字段闭包/transfer/hash 非法")
        actions: Dict[str, Dict[str, Any]] = {}
        for item in value["objects"]:
            if not isinstance(item, dict):
                raise RepositoryTransportError(
                    "Git LFS Batch object 非 object")
            allowed = {"oid", "size", "authenticated", "actions", "error"}
            if (not set(item) <= allowed
                    or not isinstance(item.get("oid"), str)
                    or item["oid"] not in requested
                    or item.get("size") != requested[item["oid"]]
                    or item["oid"] in actions
                    or ("authenticated" in item
                        and not isinstance(item["authenticated"], bool))):
                raise RepositoryTransportError(
                    "Git LFS Batch object identity/size 非法")
            if "error" in item:
                error = item["error"]
                if (not isinstance(error, dict) or set(error) != {"code", "message"}
                        or isinstance(error.get("code"), bool)
                        or not isinstance(error.get("code"), int)
                        or not isinstance(error.get("message"), str)):
                    raise RepositoryTransportError(
                        "Git LFS Batch per-object error 非法")
                message = _bounded_string(
                    error["message"], field="Git LFS object error message",
                    max_bytes=1024)
                error_type = (RepositoryMaterializationError
                              if error["code"] in (404, 410)
                              else RepositoryTransportError)
                raise error_type(
                    f"Git LFS object {item['oid']} unavailable: "
                    f"{error['code']} {message}")
            if (not isinstance(item.get("actions"), dict)
                    or set(item["actions"]) != {"download"}):
                raise RepositoryTransportError(
                    "Git LFS download object 缺唯一 download action")
            try:
                actions[item["oid"]] = self._validate_lfs_action(
                    item["actions"]["download"],
                    allowed_hosts=self.config["allowed_lfs_hosts"])
            except RepositoryMaterializationError as error:
                raise RepositoryTransportError(
                    "Git LFS Batch download action 越出传输合同") from error
        if set(actions) != set(requested):
            raise RepositoryTransportError(
                "Git LFS Batch response 未闭合 requested OID set")
        return actions, {
            "batch_url": batch_url, "request_hash": request_hash,
            "response_hash": response_hash, "object_count": len(requested),
        }

    def _download_lfs_object(
            self, action: Mapping[str, Any], destination: Path, *,
            oid: str, size: int) -> Dict[str, Any]:
        self.owner_guard()
        expected_hash = "sha256:" + oid
        if self.lfs_object_fetcher is not None:
            result = dict(self.lfs_object_fetcher(
                action["href"], dict(action["headers"]), destination, size))
            if (set(result) != {"url", "bytes", "sha256"}
                    or result.get("url") != action["href"]
                    or result.get("bytes") != size
                    or result.get("sha256") != expected_hash
                    or not destination.is_file()):
                raise RepositoryTransportError(
                    "injected Git LFS object receipt 非法")
            try:
                with open_artifact(
                        destination, expected_hash=expected_hash,
                        expected_size=size, label="injected Git LFS object",
                        progress_guard=self.owner_guard):
                    pass
            except ArtifactCapabilityError as error:
                raise RepositoryTransportError(
                    "injected Git LFS object bytes 与 OID/size 不一致") from error
            return result
        request = urllib.request.Request(
            action["href"], headers=dict(action["headers"]), method="GET")
        try:
            response = self._lfs_object_opener(
                request, timeout=float(self.config["timeout_s"]))
        except urllib.error.HTTPError as error:
            try:
                raise RepositoryTransportError(
                    f"Git LFS object download HTTP {error.code}") from error
            finally:
                error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RepositoryTransportError(
                f"Git LFS object download 失败: {type(error).__name__}") from error
        digest = hashlib.sha256()
        total = 0
        try:
            try:
                final = urllib.parse.urlsplit(response.geturl())
                invalid_final = (
                    final.scheme != "https"
                    or final.hostname not in self.config["allowed_lfs_hosts"]
                    or final.username or final.password
                    or final.port not in (None, 443))
            except ValueError as error:
                raise RepositoryTransportError(
                    "Git LFS object final URL authority 非法") from error
            if invalid_final:
                raise RepositoryTransportError(
                    "Git LFS object final URL 越出 allowlist")
            encoding = response.headers.get("Content-Encoding")
            if encoding not in (None, "", "identity"):
                raise RepositoryTransportError(
                    "Git LFS object 不接受 Content-Encoding")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RepositoryTransportError(
                        "Git LFS object Content-Length 非整数") from error
                if declared_size != size:
                    raise RepositoryTransportError(
                        "Git LFS object Content-Length 与 pointer size 不一致")
            fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600)
            try:
                while True:
                    self.owner_guard()
                    chunk = response.read(min(1024 * 1024, size - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise RepositoryTransportError(
                            "Git LFS object bytes 超过 pointer size")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("Git LFS object short write")
                        view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            response.close()
        actual_hash = "sha256:" + digest.hexdigest()
        if total != size or actual_hash != expected_hash:
            raise RepositoryTransportError(
                "Git LFS object bytes 与 pointer OID/size 不一致")
        return {"url": action["href"], "bytes": total, "sha256": actual_hash}

    def _replace_lfs_pointer(
            self, *, source: Path, target: Path, item: Mapping[str, Any],
            oid: str, size: int) -> None:
        temporary = target.parent / (
            f".{target.name}.lfs.{os.getpid()}.{secrets.token_hex(8)}")
        output_fd = -1
        try:
            with open_artifact(
                    target, expected_hash=item["sha256"],
                    expected_size=item["bytes"], label="Git LFS pointer before replace",
                    progress_guard=self.owner_guard):
                pass
            with open_artifact(
                    source, expected_hash="sha256:" + oid,
                    expected_size=size, label="verified Git LFS object",
                    progress_guard=self.owner_guard) as capability:
                output_fd = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o400)
                copied = 0
                os.lseek(capability.fd, 0, os.SEEK_SET)
                while copied < size:
                    self.owner_guard()
                    chunk = os.read(capability.fd, min(1024 * 1024, size - copied))
                    if not chunk:
                        raise RepositoryTransportError("verified Git LFS object 截断")
                    copied += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(output_fd, view)
                        if written <= 0:
                            raise OSError("Git LFS replacement short write")
                        view = view[written:]
                if os.read(capability.fd, 1):
                    raise RepositoryTransportError("verified Git LFS object size 漂移")
                os.fchmod(output_fd, 0o555 if item["git_mode"] == "100755" else 0o444)
                os.fsync(output_fd)
                os.close(output_fd)
                output_fd = -1
                capability.verify_unchanged(self.owner_guard)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if output_fd >= 0:
                os.close(output_fd)
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _materialize_lfs_objects(
            self, *, full_name: str, revision: str, repo_destination: Path,
            downloads: Path, local_ledger: list[Dict[str, Any]],
            prior_total: int, global_lfs_objects: Dict[str, int]) -> list[Dict[str, Any]]:
        pointers: list[tuple[Dict[str, Any], Dict[str, Any]]] = []
        evidence: list[Dict[str, Any]] = []
        for item in local_ledger:
            embedded = item.pop("_lfs_transport", None)
            lfs = item.get("lfs")
            if lfs is None and item["bytes"] < 1024:
                payload = read_artifact_bytes(
                    repo_destination / item["path"],
                    expected_hash=item["sha256"], expected_size=item["bytes"],
                    max_bytes=1023, label=f"LFS pointer probe:{item['path']}",
                    progress_guard=self.owner_guard)
                pointer = _parse_lfs_pointer(payload)
                if (pointer is None
                        and payload.startswith(
                            b"version https://git-lfs.github.com/spec/")):
                    raise RepositoryMaterializationError(
                        f"Git LFS-like pointer 非法: {full_name}:{item['path']}")
                if pointer is not None:
                    lfs = {
                        **pointer, "pointer_sha256": item["sha256"],
                        "pointer_bytes": item["bytes"],
                    }
            if lfs is None:
                continue
            if self.config["lfs_policy"] != "fetch":
                raise RepositoryMaterializationError(
                    f"Git LFS pointer {full_name}:{item['path']} "
                    f"({lfs['oid']}, {lfs['size']} bytes) 被 lfs_policy=reject 拒绝")
            if item["path"] == ".gitmodules":
                raise RepositoryMaterializationError(
                    "Git LFS 不得承载 .gitmodules 控制文件")
            if (lfs["size"] > int(self.config["max_file_bytes"])
                    or lfs["size"] < 0):
                raise RepositoryMaterializationError(
                    f"Git LFS object 超过单文件 policy: {item['path']}")
            prior_size = global_lfs_objects.setdefault(lfs["oid"], lfs["size"])
            if prior_size != lfs["size"]:
                raise RepositoryMaterializationError(
                    "同一 Git LFS OID 声明冲突 size")
            if len(global_lfs_objects) > int(self.config["max_lfs_objects"]):
                raise RepositoryMaterializationError(
                    "Git LFS unique object 数超过 policy")
            if embedded == "archive":
                evidence.append({
                    "oid": lfs["oid"], "size": lfs["size"],
                    "transfer": "archive", "download_origin": "https://api.github.com",
                    "download_transport_sha256": item["sha256"],
                    "download_transport_bytes": item["bytes"],
                })
            else:
                pointers.append((item, lfs))
        pointer_sizes = {id(item): lfs["size"] for item, lfs in pointers}
        final_local_total = sum(
            item["lfs"]["size"] if "lfs" in item
            else pointer_sizes.get(id(item), item["bytes"])
            for item in local_ledger)
        if prior_total + final_local_total > int(self.config["max_total_bytes"]):
            raise RepositoryMaterializationError(
                "recursive repository 含 LFS 后总 bytes 超过 policy")
        requested = {
            lfs["oid"].removeprefix("sha256:"): lfs["size"]
            for _item, lfs in pointers
        }
        batch_size = int(self.config["lfs_batch_size"])
        transport_by_oid: Dict[str, Dict[str, Any]] = {}
        ordered = sorted(requested)
        for start in range(0, len(ordered), batch_size):
            batch = ordered[start:start + batch_size]
            actions, batch_evidence = self._lfs_batch_actions(
                full_name, revision,
                [{"oid": "sha256:" + oid, "size": requested[oid]}
                 for oid in batch])
            for oid in batch:
                action = actions[oid]
                object_path = downloads / f"lfs-{oid}"
                transfer = "cache"
                if os.path.lexists(object_path):
                    try:
                        with open_artifact(
                                object_path, expected_hash="sha256:" + oid,
                                expected_size=requested[oid],
                                label="reused Git LFS download",
                                progress_guard=self.owner_guard):
                            pass
                    except ArtifactCapabilityError as error:
                        raise RepositoryTransportError(
                            "reused Git LFS download 损坏") from error
                    receipt = {
                        "bytes": requested[oid], "sha256": "sha256:" + oid,
                    }
                else:
                    temporary = downloads / (
                        f".lfs-{oid}.{os.getpid()}.{secrets.token_hex(8)}.partial")
                    try:
                        receipt = self._download_lfs_object(
                            action, temporary, oid=oid, size=requested[oid])
                        os.replace(temporary, object_path)
                        _fsync_directory(downloads)
                    finally:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                    transfer = "basic"
                origin = urllib.parse.urlsplit(action["href"])
                transport_by_oid[oid] = {
                    "oid": "sha256:" + oid, "size": requested[oid],
                    "transfer": transfer,
                    "batch_url": batch_evidence["batch_url"],
                    "batch_request_hash": batch_evidence["request_hash"],
                    "batch_response_hash": batch_evidence["response_hash"],
                    "download_origin": f"https://{origin.hostname}",
                    "download_transport_sha256": receipt["sha256"],
                    "download_transport_bytes": receipt["bytes"],
                }
        for item, lfs in pointers:
            oid = lfs["oid"].removeprefix("sha256:")
            object_path = downloads / f"lfs-{oid}"
            self._replace_lfs_pointer(
                source=object_path, target=repo_destination / item["path"],
                item=item, oid=oid, size=lfs["size"])
            item["sha256"] = lfs["oid"]
            item["bytes"] = lfs["size"]
            item["lfs"] = lfs
            record = transport_by_oid[oid]
            if record not in evidence:
                evidence.append(record)
        return sorted(evidence, key=lambda item: (item["oid"], item["transfer"]))
