"""Bounded, host-controlled arXiv novelty snapshots.

The idea model supplies only a plain-text query.  This module owns the network
capability and freezes both the exact Atom response and a canonical projection
under the quest's private ``state/novelty`` tree.  A query receipt is immutable
and request-addressed, so an owner restart replays the already-frozen result
instead of silently issuing a different search.

This is deliberately a filesystem sidecar.  It neither gives the model a URL
opener nor writes research SQLite; the caller may register the returned
``final_ref`` through the ordinary stage/gate transaction.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


_ENDPOINT = "https://export.arxiv.org/api/query"
_PROTOCOL = "meta-research-arxiv-novelty-query/v1"
_SNAPSHOT_PROTOCOL = "meta-research-arxiv-novelty-snapshot/v1"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_QUERY_BYTES = 512
_MIN_QUERY_CHARS = 5
_MAX_RESULTS = 50
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_TITLE_BYTES = 8192
_MAX_SUMMARY_BYTES = 256 * 1024
_MAX_ID_BYTES = 2048
_MAX_TIMESTAMP_BYTES = 128
_MAX_AUTHORS = 128
_MAX_AUTHOR_BYTES = 1024
_MAX_CATEGORIES = 128
_MAX_CATEGORY_BYTES = 512
_MAX_XML_ELEMENTS = 100_000
_READ_BLOCK = 64 * 1024
_DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0))
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NOFOLLOW", 0))
_WRITE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0))
_RAW_DIR = ("state", "novelty", "raw", "sha256")
_SNAPSHOT_DIR = ("state", "novelty", "snapshots", "sha256")
_QUERY_DIR = ("state", "novelty", "queries", "sha256")
_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429})
_DEFAULT_RETRY_ATTEMPTS = 8
_DEFAULT_RETRY_INITIAL_DELAY_S = 3.0
_DEFAULT_RETRY_MAX_DELAY_S = 120.0
_OWNER_SLEEP_SLICE_S = 5.0
_LOGGER = logging.getLogger(__name__)


class NoveltySearchError(RuntimeError):
    """A novelty policy, query, network response, or replay asset is unsafe."""


class NoveltySearchProviderError(NoveltySearchError):
    """The bounded arXiv call failed before a query receipt was published."""


class _RetryableFetchError(RuntimeError):
    """A transient provider/network failure that may safely be retried."""

    def __init__(self, message: str, *, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never turn a fixed policy endpoint into ambient redirect authority."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, code, "arXiv novelty endpoint redirected", headers, fp)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as error:
        raise NoveltySearchError("novelty snapshot 含非规范 JSON 值") from error


def _content_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _strict_json(raw: bytes, *, label: str, maximum: int) -> Any:
    if not isinstance(raw, bytes) or not 2 <= len(raw) <= maximum:
        raise NoveltySearchError(f"{label} JSON 大小非法")

    def unique(pairs):  # noqa: ANN001
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as error:
        raise NoveltySearchError(f"{label} 不是严格 JSON") from error
    if _canonical_bytes(value) != raw:
        raise NoveltySearchError(f"{label} 不是 canonical JSON")
    return value


def _bounded_number(value: Any, *, field: str, minimum: float,
                    maximum: float, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NoveltySearchError(f"novelty policy {field} 类型非法")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise NoveltySearchError(f"novelty policy {field} 越界")
    if integer:
        if not isinstance(value, int):
            raise NoveltySearchError(f"novelty policy {field} 须为整数")
        return int(value)
    return number


def _retry_after_seconds(headers: Any) -> Optional[float]:
    """Parse either form of RFC Retry-After without trusting malformed input."""
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
        return None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(
        0.0,
        (retry_at.astimezone(timezone.utc)
         - datetime.now(timezone.utc)).total_seconds())


def _is_retryable_http_status(status: Any) -> bool:
    if isinstance(status, bool):
        return False
    try:
        code = int(status)
    except (TypeError, ValueError, OverflowError):
        return False
    return code in _RETRYABLE_HTTP_STATUS or 500 <= code <= 599


def _validate_policy_hash(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise NoveltySearchError("novelty policy_hash 须为 sha256 内容指纹")
    return value


def _ordinary_query(value: Any) -> str:
    if not isinstance(value, str):
        raise NoveltySearchError("novelty query 须为普通文本")
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise NoveltySearchError("novelty query 须为无首尾空白的 NFC 文本")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise NoveltySearchError("novelty query 不是合法 UTF-8") from error
    if not _MIN_QUERY_CHARS <= len(value) or len(encoded) > _MAX_QUERY_BYTES:
        raise NoveltySearchError(
            f"novelty query 须至少 {_MIN_QUERY_CHARS} 字符且不超过 "
            f"{_MAX_QUERY_BYTES} UTF-8 bytes")
    if '"' in value or "\\" in value:
        raise NoveltySearchError("novelty query 不得含引号或反斜杠")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise NoveltySearchError("novelty query 不得含控制/格式/私用字符")
    return value


def _bounded_text(value: str, *, field: str, maximum: int,
                  collapse: bool = True) -> str:
    if collapse:
        value = " ".join(value.split())
    if not value:
        raise NoveltySearchProviderError(f"arXiv entry {field} 为空")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise NoveltySearchProviderError(
            f"arXiv entry {field} 不是合法 UTF-8") from error
    if size > maximum:
        raise NoveltySearchProviderError(f"arXiv entry {field} 超过投影上限")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise NoveltySearchProviderError(
            f"arXiv entry {field} 含控制/格式/私用字符")
    return value


def _timestamp(value: str, *, field: str) -> str:
    value = _bounded_text(
        value, field=field, maximum=_MAX_TIMESTAMP_BYTES, collapse=False)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise NoveltySearchProviderError(
            f"arXiv entry {field} 不是 ISO-8601 时间") from error
    if parsed.tzinfo is None:
        raise NoveltySearchProviderError(f"arXiv entry {field} 缺时区")
    return value


def _single_text(entry: ET.Element, tag: str, *, maximum: int,
                 collapse: bool = True) -> str:
    elements = entry.findall(f"{{{_ATOM_NS}}}{tag}")
    if len(elements) != 1:
        raise NoveltySearchProviderError(
            f"arXiv entry {tag} 必须恰有一个")
    return _bounded_text(
        "".join(elements[0].itertext()), field=tag, maximum=maximum,
        collapse=collapse)


def _validate_arxiv_id(value: str) -> str:
    value = _bounded_text(
        value, field="id", maximum=_MAX_ID_BYTES, collapse=False)
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise NoveltySearchProviderError("arXiv entry id port 非法") from error
    if (parsed.scheme not in {"http", "https"} or parsed.hostname != "arxiv.org"
            or parsed.username or parsed.password or port not in {None, 80, 443}
            or not parsed.path.startswith("/abs/") or parsed.query or parsed.fragment):
        raise NoveltySearchProviderError("arXiv entry id 越出 arxiv.org/abs authority")
    return value


def _parse_atom(raw: bytes, *, max_results: int) -> list[Dict[str, Any]]:
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise NoveltySearchProviderError("arXiv Atom 不得含 DTD/entity")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, RecursionError) as error:
        raise NoveltySearchProviderError("arXiv 返回畸形 Atom XML") from error
    if root.tag != f"{{{_ATOM_NS}}}feed":
        raise NoveltySearchProviderError("arXiv 返回根节点不是 Atom feed")
    if sum(1 for _ in root.iter()) > _MAX_XML_ELEMENTS:
        raise NoveltySearchProviderError("arXiv Atom 元素数量超限")
    entries = root.findall(f"{{{_ATOM_NS}}}entry")
    if len(entries) > max_results:
        raise NoveltySearchProviderError("arXiv 返回 entry 数超过 policy 上限")

    results: list[Dict[str, Any]] = []
    ids: set[str] = set()
    for entry in entries:
        result_id = _validate_arxiv_id(
            _single_text(entry, "id", maximum=_MAX_ID_BYTES, collapse=False))
        if result_id in ids:
            raise NoveltySearchProviderError("arXiv Atom 含重复 entry id")
        ids.add(result_id)
        title = _single_text(entry, "title", maximum=_MAX_TITLE_BYTES)
        summary = _single_text(entry, "summary", maximum=_MAX_SUMMARY_BYTES)
        published = _timestamp(
            _single_text(
                entry, "published", maximum=_MAX_TIMESTAMP_BYTES,
                collapse=False),
            field="published")
        updated = _timestamp(
            _single_text(
                entry, "updated", maximum=_MAX_TIMESTAMP_BYTES,
                collapse=False),
            field="updated")

        author_elements = entry.findall(f"{{{_ATOM_NS}}}author")
        if not 1 <= len(author_elements) <= _MAX_AUTHORS:
            raise NoveltySearchProviderError("arXiv entry authors 数量非法")
        authors = []
        for author in author_elements:
            names = author.findall(f"{{{_ATOM_NS}}}name")
            if len(names) != 1:
                raise NoveltySearchProviderError(
                    "arXiv entry author.name 必须恰有一个")
            authors.append(_bounded_text(
                "".join(names[0].itertext()), field="author.name",
                maximum=_MAX_AUTHOR_BYTES))

        category_elements = entry.findall(f"{{{_ATOM_NS}}}category")
        if not 1 <= len(category_elements) <= _MAX_CATEGORIES:
            raise NoveltySearchProviderError("arXiv entry categories 数量非法")
        categories = []
        for category in category_elements:
            term = category.get("term")
            if not isinstance(term, str):
                raise NoveltySearchProviderError("arXiv entry category.term 缺失")
            term = _bounded_text(
                term, field="category.term", maximum=_MAX_CATEGORY_BYTES,
                collapse=False)
            if term in categories:
                raise NoveltySearchProviderError("arXiv entry category.term 重复")
            categories.append(term)

        results.append({
            "authors": authors,
            "categories": categories,
            "id": result_id,
            "published": published,
            "summary": summary,
            "title": title,
            "updated": updated,
        })
    return results


def _relative_ref(parts: Sequence[str], name: str) -> str:
    return "/".join((*parts, name))


class ArxivNoveltySearchProvider:
    """Fixed-endpoint arXiv Atom reader with private content-addressed replay."""

    name = "arxiv_api_v1"

    def __init__(self, config: Mapping[str, Any], work_root: Path | str,
                 owner_guard: Optional[Callable[[], None]] = None,
                 opener=None):  # noqa: ANN001 - urllib/fake opener protocol
        if not isinstance(config, Mapping):
            raise NoveltySearchError("novelty policy 须为 mapping")
        if config.get("provider") != self.name:
            raise NoveltySearchError("novelty policy provider 非 arxiv_api_v1")
        if config.get("endpoint") != _ENDPOINT:
            raise NoveltySearchError("novelty policy endpoint 只接受固定 arXiv HTTPS API")
        if "enabled" in config and config.get("enabled") is not True:
            raise NoveltySearchError("禁用 novelty policy 不得实例化联网 provider")
        if ("status" in config
                and config.get("status") != "controlled_backend_enabled"):
            raise NoveltySearchError("novelty policy status 未启用受控 backend")
        if "queries_per_candidate" in config and config.get("queries_per_candidate") != 1:
            raise NoveltySearchError("当前 novelty provider 只接受每候选一次查询")
        self.timeout_s = float(_bounded_number(
            config.get("timeout_s"), field="timeout_s", minimum=0.001,
            maximum=600))
        self.max_response_bytes = int(_bounded_number(
            config.get("max_response_bytes"), field="max_response_bytes",
            minimum=1024, maximum=_MAX_RESPONSE_BYTES, integer=True))
        self.max_results = int(_bounded_number(
            config.get("max_results_per_query"),
            field="max_results_per_query", minimum=1, maximum=_MAX_RESULTS,
            integer=True))
        self.min_interval_s = float(_bounded_number(
            config.get("min_interval_s"), field="min_interval_s", minimum=0,
            maximum=60))
        self.retry_attempts = int(_bounded_number(
            config.get("retry_attempts", _DEFAULT_RETRY_ATTEMPTS),
            field="retry_attempts", minimum=1, maximum=20, integer=True))
        self.retry_initial_delay_s = float(_bounded_number(
            config.get(
                "retry_initial_delay_s", _DEFAULT_RETRY_INITIAL_DELAY_S),
            field="retry_initial_delay_s", minimum=0, maximum=120))
        self.retry_max_delay_s = float(_bounded_number(
            config.get("retry_max_delay_s", _DEFAULT_RETRY_MAX_DELAY_S),
            field="retry_max_delay_s", minimum=0, maximum=600))
        if self.retry_max_delay_s < self.retry_initial_delay_s:
            raise NoveltySearchError(
                "novelty policy retry_max_delay_s 不得小于 "
                "retry_initial_delay_s")

        requested = Path(os.path.abspath(os.fspath(work_root)))
        try:
            resolved = requested.resolve(strict=True)
            info = requested.lstat()
        except (OSError, RuntimeError) as error:
            raise NoveltySearchError("novelty work_root 不存在/不可解析") from error
        if (resolved != requested or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise NoveltySearchError("novelty work_root authority 非法")
        self.work_root = requested
        self.owner_guard = owner_guard or (lambda: None)
        if opener is None:
            self.opener = urllib.request.build_opener(
                _RejectRedirectHandler()).open
        elif callable(opener):
            self.opener = opener
        elif callable(getattr(opener, "open", None)):
            self.opener = opener.open
        else:
            raise NoveltySearchError("novelty opener 须为 callable/open provider")
        self._lock = threading.RLock()
        self._last_request_started: Optional[float] = None
        self.owner_guard()
        for relative in (_RAW_DIR, _SNAPSHOT_DIR, _QUERY_DIR):
            fd = self._directory_fd(relative, create=True)
            os.close(fd)

    def _root_fd(self) -> int:
        try:
            fd = os.open(self.work_root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise NoveltySearchError("novelty work_root 无法安全打开") from error
        info = os.fstat(fd)
        try:
            path_info = self.work_root.lstat()
        except OSError as error:
            os.close(fd)
            raise NoveltySearchError("novelty work_root 路径身份丢失") from error
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)):
            os.close(fd)
            raise NoveltySearchError("novelty work_root identity 漂移")
        return fd

    def _directory_fd(self, relative: Sequence[str], *, create: bool) -> int:
        current_fd = self._root_fd()
        try:
            for component in relative:
                if (not isinstance(component, str) or not component
                        or "/" in component or component in {".", ".."}):
                    raise NoveltySearchError("novelty storage component 非法")
                try:
                    child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise NoveltySearchError("novelty storage 目录缺失")
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    os.fsync(current_fd)
                except OSError as error:
                    raise NoveltySearchError("novelty storage 目录无法安全打开") from error
                child_info = os.fstat(child_fd)
                path_info = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False)
                if (not stat.S_ISDIR(child_info.st_mode)
                        or child_info.st_uid != os.geteuid()
                        # Qualification may have created the shared ``state``
                        # ancestor as 0755 before the research owner starts.
                        # The work root itself is verified 0700, so an
                        # owner-owned, non-writable ancestor remains private;
                        # every directory created by this provider is 0700.
                        or child_info.st_mode & 0o022
                        or (child_info.st_dev, child_info.st_ino)
                        != (path_info.st_dev, path_info.st_ino)):
                    os.close(child_fd)
                    raise NoveltySearchError("novelty storage 目录 authority 非法")
                os.close(current_fd)
                current_fd = child_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _file_identity(info: os.stat_result) -> Tuple[int, ...]:
        return (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink,
        )

    def _read_file(self, relative: Sequence[str], name: str, *, mode: int,
                   maximum: int, expected_size: Optional[int] = None,
                   expected_hash: Optional[str] = None) -> bytes:
        directory_fd = self._directory_fd(relative, create=False)
        try:
            try:
                fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise NoveltySearchError("novelty replay 文件不可安全打开") from error
            try:
                before = os.fstat(fd)
                try:
                    path_before = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise NoveltySearchError(
                        "novelty replay 文件路径身份丢失") from error
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) != mode
                        or before.st_size < 1 or before.st_size > maximum
                        or (expected_size is not None
                            and before.st_size != expected_size)
                        or (before.st_dev, before.st_ino)
                        != (path_before.st_dev, path_before.st_ino)):
                    raise NoveltySearchError("novelty replay 文件 authority/bytes 非法")
                chunks, remaining = [], before.st_size
                while remaining:
                    block = os.read(fd, min(_READ_BLOCK, remaining))
                    if not block:
                        raise NoveltySearchError("novelty replay 文件提前 EOF")
                    chunks.append(block)
                    remaining -= len(block)
                if os.read(fd, 1):
                    raise NoveltySearchError("novelty replay 文件含未声明尾随 bytes")
                after = os.fstat(fd)
                try:
                    path_after = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise NoveltySearchError(
                        "novelty replay 文件读取期间路径身份丢失") from error
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        raw = b"".join(chunks)
        if (self._file_identity(before) != self._file_identity(after)
                or (after.st_dev, after.st_ino)
                != (path_after.st_dev, path_after.st_ino)):
            raise NoveltySearchError("novelty replay 文件读取期间 identity 漂移")
        if expected_hash is not None and _content_hash(raw) != expected_hash:
            raise NoveltySearchError("novelty replay 文件 content hash 不一致")
        return raw

    def _publish_file(self, relative: Sequence[str], name: str, raw: bytes,
                      *, mode: int, maximum: int) -> None:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= maximum:
            raise NoveltySearchError("novelty publish bytes 越界")
        expected_hash = _content_hash(raw)
        directory_fd = self._directory_fd(relative, create=True)
        temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                existing = self._read_file(
                    relative, name, mode=mode, maximum=maximum,
                    expected_size=len(raw), expected_hash=expected_hash)
                if existing != raw:
                    raise NoveltySearchError("novelty content-addressed 文件冲突")
                return
            fd = os.open(temporary, _WRITE_FLAGS, mode, dir_fd=directory_fd)
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short novelty write")
                offset += written
            os.fchmod(fd, mode)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                os.link(
                    temporary, name, src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                existing = self._read_file(
                    relative, name, mode=mode, maximum=maximum,
                    expected_size=len(raw), expected_hash=expected_hash)
                if existing != raw:
                    raise NoveltySearchError("novelty content-addressed 文件并发冲突")
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(directory_fd)

    def _owned_sleep(self, seconds: float) -> None:
        """Wait without leaving the quest owner unobserved for a long backoff."""
        if seconds <= 0:
            self.owner_guard()
            return
        deadline = time.monotonic() + seconds
        while True:
            self.owner_guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_OWNER_SLEEP_SLICE_S, remaining))
        self.owner_guard()

    def _wait_for_request_slot(self) -> None:
        if self._last_request_started is not None:
            delay = (self.min_interval_s
                     - (time.monotonic() - self._last_request_started))
            if delay > 0:
                self._owned_sleep(delay)
        self.owner_guard()
        self._last_request_started = time.monotonic()

    def _fetch_once(self, query: str) -> bytes:
        self._wait_for_request_slot()
        encoded = urllib.parse.urlencode({
            "search_query": f'all:"{query}"',
            "start": 0,
            "max_results": self.max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }, quote_via=urllib.parse.quote)
        url = f"{_ENDPOINT}?{encoded}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/atom+xml, application/xml;q=0.9",
                "User-Agent": "meta-research-novelty-search/1",
            },
            method="GET")
        response = None
        try:
            response = self.opener(request, timeout=self.timeout_s)
            status = getattr(response, "status", None)
            if status is None and callable(getattr(response, "getcode", None)):
                status = response.getcode()
            if status is not None and status != 200:
                if _is_retryable_http_status(status):
                    raise _RetryableFetchError(
                        f"arXiv novelty HTTP {status}",
                        retry_after_s=_retry_after_seconds(
                            getattr(response, "headers", None)))
                raise NoveltySearchProviderError(f"arXiv novelty HTTP {status}")
            final_url = response.geturl()
            if final_url != url:
                raise NoveltySearchProviderError(
                    "arXiv novelty redirect/最终 URL 漂移")
            parsed = urllib.parse.urlsplit(final_url)
            try:
                port = parsed.port
            except ValueError as error:
                raise NoveltySearchProviderError(
                    "arXiv novelty 最终 URL port 非法") from error
            if (parsed.scheme != "https" or parsed.hostname != "export.arxiv.org"
                    or parsed.username or parsed.password or port not in {None, 443}
                    or parsed.path != "/api/query" or parsed.fragment):
                raise NoveltySearchProviderError(
                    "arXiv novelty 最终 URL 越出固定 authority")
            headers = response.headers
            content_encoding = headers.get("Content-Encoding")
            if content_encoding not in {None, "", "identity"}:
                raise NoveltySearchProviderError(
                    "arXiv novelty 不接受压缩/未知 Content-Encoding")
            declared = headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError) as error:
                    raise NoveltySearchProviderError(
                        "arXiv novelty Content-Length 非整数") from error
                if not 0 <= declared_size <= self.max_response_bytes:
                    raise NoveltySearchProviderError(
                        "arXiv novelty Content-Length 超过 policy 上限")
            chunks = []
            seen = 0
            while seen <= self.max_response_bytes:
                block = response.read(min(
                    _READ_BLOCK, self.max_response_bytes + 1 - seen))
                if not isinstance(block, bytes):
                    raise NoveltySearchProviderError(
                        "arXiv novelty response 非 bytes")
                if not block:
                    break
                chunks.append(block)
                seen += len(block)
            raw = b"".join(chunks)
            if not raw or len(raw) > self.max_response_bytes:
                raise NoveltySearchProviderError("arXiv novelty response bytes 越界")
            if declared is not None and len(raw) != declared_size:
                raise NoveltySearchProviderError(
                    "arXiv novelty Content-Length 与实际 bytes 不一致")
            return raw
        except (_RetryableFetchError, NoveltySearchProviderError):
            raise
        except urllib.error.HTTPError as error:
            try:
                if _is_retryable_http_status(error.code):
                    raise _RetryableFetchError(
                        f"arXiv novelty HTTP {error.code}",
                        retry_after_s=_retry_after_seconds(error.headers)) \
                        from error
                raise NoveltySearchProviderError(
                    f"arXiv novelty HTTP {error.code}") from error
            finally:
                if error.fp is not None:
                    error.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _RetryableFetchError(
                f"arXiv novelty 受控请求临时失败: {type(error).__name__}") \
                from error
        except (ValueError, AttributeError, TypeError) as error:
            raise NoveltySearchProviderError(
                f"arXiv novelty 受控请求失败: {type(error).__name__}") from error
        finally:
            if response is not None:
                response.close()
            self.owner_guard()

    def _fetch(self, query: str) -> bytes:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self._fetch_once(query)
            except _RetryableFetchError as error:
                if attempt >= self.retry_attempts:
                    _LOGGER.error(
                        "arXiv novelty 临时请求失败，已用尽 %d 次尝试: %s",
                        self.retry_attempts, error)
                    raise NoveltySearchProviderError(
                        "arXiv novelty 临时请求失败，"
                        f"已用尽 {self.retry_attempts} 次尝试: {error}") \
                        from (error.__cause__ or error)
                exponential = self.retry_initial_delay_s * (2 ** (attempt - 1))
                delay = min(exponential, self.retry_max_delay_s)
                if error.retry_after_s is not None:
                    delay = max(
                        delay, min(error.retry_after_s, self.retry_max_delay_s))
                _LOGGER.warning(
                    "arXiv novelty 第 %d/%d 次请求临时失败；%.1f 秒后重试: %s",
                    attempt, self.retry_attempts, delay, error)
                self._owned_sleep(delay)
        raise AssertionError("novelty retry loop unexpectedly exhausted")

    def _request(self, query: str, policy_hash: str) -> Dict[str, Any]:
        return {
            "endpoint": _ENDPOINT,
            "max_results_per_query": self.max_results,
            "policy_hash": policy_hash,
            "provider": self.name,
            "query": query,
        }

    def _receipt_name(self, request: Mapping[str, Any]) -> str:
        return hashlib.sha256(_canonical_bytes(request)).hexdigest() + ".json"

    def _load_replay(self, request: Dict[str, Any], receipt_name: str
                     ) -> Optional[Dict[str, Any]]:
        try:
            receipt_raw = self._read_file(
                _QUERY_DIR, receipt_name, mode=0o600,
                maximum=_MAX_RECEIPT_BYTES)
        except NoveltySearchError as error:
            # Absence permits the first network attempt.  Every other path or
            # authority error must fail closed instead of being treated as a miss.
            directory_fd = self._directory_fd(_QUERY_DIR, create=False)
            try:
                try:
                    os.stat(
                        receipt_name, dir_fd=directory_fd,
                        follow_symlinks=False)
                except FileNotFoundError:
                    return None
            finally:
                os.close(directory_fd)
            raise error
        receipt = _strict_json(
            receipt_raw, label="novelty query receipt",
            maximum=_MAX_RECEIPT_BYTES)
        if not isinstance(receipt, dict) or set(receipt) != {
                "final_ref", "protocol", "raw", "request", "request_hash",
                "snapshot"}:
            raise NoveltySearchError("novelty query receipt 字段闭包非法")
        request_hash = _content_hash(_canonical_bytes(request))
        if (receipt.get("protocol") != _PROTOCOL
                or receipt.get("request") != request
                or receipt.get("request_hash") != request_hash):
            raise NoveltySearchError("novelty query receipt 与请求不一致")

        raw_meta = receipt.get("raw")
        snapshot_meta = receipt.get("snapshot")
        for label, meta, suffix in (
                ("raw", raw_meta, ".atom"),
                ("snapshot", snapshot_meta, ".json")):
            if (not isinstance(meta, dict)
                    or set(meta) != {"bytes", "content_hash", "ref"}
                    or isinstance(meta.get("bytes"), bool)
                    or not isinstance(meta.get("bytes"), int)
                    or meta["bytes"] <= 0
                    or not isinstance(meta.get("content_hash"), str)
                    or _SHA256_RE.fullmatch(meta["content_hash"]) is None
                    or meta.get("ref") != _relative_ref(
                        _RAW_DIR if label == "raw" else _SNAPSHOT_DIR,
                        meta["content_hash"].removeprefix("sha256:") + suffix)):
                raise NoveltySearchError(
                    f"novelty query receipt {label} metadata 非法")
        raw = self._read_file(
            _RAW_DIR, Path(raw_meta["ref"]).name, mode=0o400,
            maximum=self.max_response_bytes, expected_size=raw_meta["bytes"],
            expected_hash=raw_meta["content_hash"])
        snapshot_raw = self._read_file(
            _SNAPSHOT_DIR, Path(snapshot_meta["ref"]).name, mode=0o400,
            maximum=max(self.max_response_bytes * 2, 1024 * 1024),
            expected_size=snapshot_meta["bytes"],
            expected_hash=snapshot_meta["content_hash"])
        snapshot = _strict_json(
            snapshot_raw, label="novelty snapshot",
            maximum=max(self.max_response_bytes * 2, 1024 * 1024))
        final_ref, results = self._validate_snapshot(
            snapshot, raw=raw, request=request,
            snapshot_hash=snapshot_meta["content_hash"],
            snapshot_ref=snapshot_meta["ref"])
        if receipt.get("final_ref") != final_ref:
            raise NoveltySearchError("novelty query receipt final_ref 漂移")
        return {"final_ref": final_ref, "results": results}

    def _validate_snapshot(
            self, snapshot: Any, *, raw: bytes, request: Dict[str, Any],
            snapshot_hash: str, snapshot_ref: str
    ) -> Tuple[Dict[str, Any], list[Dict[str, Any]]]:
        if not isinstance(snapshot, dict) or set(snapshot) != {
                "policy_hash", "protocol", "provider", "query", "ranking",
                "raw_content_hash", "result_content_hashes", "results"}:
            raise NoveltySearchError("novelty snapshot 字段闭包非法")
        if (snapshot.get("protocol") != _SNAPSHOT_PROTOCOL
                or snapshot.get("provider") != self.name
                or snapshot.get("query") != request["query"]
                or snapshot.get("policy_hash") != request["policy_hash"]
                or snapshot.get("raw_content_hash") != _content_hash(raw)):
            raise NoveltySearchError("novelty snapshot 请求/raw binding 非法")
        parsed_results = _parse_atom(raw, max_results=self.max_results)
        if snapshot.get("results") != parsed_results:
            raise NoveltySearchError("novelty snapshot 与原始 Atom 投影不一致")
        result_hashes = [_content_hash(_canonical_bytes(item))
                         for item in parsed_results]
        if (len(set(result_hashes)) != len(result_hashes)
                or snapshot.get("result_content_hashes") != result_hashes
                or snapshot.get("ranking") != result_hashes):
            raise NoveltySearchError("novelty snapshot 结果 hash/ranking 非法")
        final_ref = {
            "query": request["query"],
            "provider": self.name,
            "snapshot_hash": snapshot_hash,
            "snapshot_ref": snapshot_ref,
            "raw_content_hash": _content_hash(raw),
            "result_content_hashes": result_hashes,
            "ranking": result_hashes,
            "policy_hash": request["policy_hash"],
        }
        projection = [
            {"rank": rank, "result_content_hash": result_hash, **item}
            for rank, (result_hash, item) in enumerate(
                zip(result_hashes, parsed_results), start=1)
        ]
        return final_ref, projection

    def search(self, query: str, *, policy_hash: str) -> Dict[str, Any]:
        """Return one immutable ref and a bounded reviewer-facing projection.

        Repeating the same query under the same policy hash first revalidates
        the private receipt, raw Atom, snapshot, per-result hashes, and parsed
        projection.  It performs no network call when that closure is intact.
        """
        query = _ordinary_query(query)
        policy_hash = _validate_policy_hash(policy_hash)
        request = self._request(query, policy_hash)
        receipt_name = self._receipt_name(request)
        self.owner_guard()
        with self._lock:
            replay = self._load_replay(request, receipt_name)
            if replay is not None:
                self.owner_guard()
                return replay

            raw = self._fetch(query)
            results = _parse_atom(raw, max_results=self.max_results)
            raw_hash = _content_hash(raw)
            result_hashes = [_content_hash(_canonical_bytes(item))
                             for item in results]
            if len(set(result_hashes)) != len(result_hashes):
                raise NoveltySearchProviderError(
                    "arXiv Atom 含 canonical 内容重复的结果")
            snapshot = {
                "policy_hash": policy_hash,
                "protocol": _SNAPSHOT_PROTOCOL,
                "provider": self.name,
                "query": query,
                "ranking": result_hashes,
                "raw_content_hash": raw_hash,
                "result_content_hashes": result_hashes,
                "results": results,
            }
            snapshot_raw = _canonical_bytes(snapshot)
            snapshot_hash = _content_hash(snapshot_raw)
            raw_name = raw_hash.removeprefix("sha256:") + ".atom"
            snapshot_name = snapshot_hash.removeprefix("sha256:") + ".json"
            raw_ref = _relative_ref(_RAW_DIR, raw_name)
            snapshot_ref = _relative_ref(_SNAPSHOT_DIR, snapshot_name)
            self.owner_guard()
            self._publish_file(
                _RAW_DIR, raw_name, raw, mode=0o400,
                maximum=self.max_response_bytes)
            self._publish_file(
                _SNAPSHOT_DIR, snapshot_name, snapshot_raw, mode=0o400,
                maximum=max(self.max_response_bytes * 2, 1024 * 1024))
            final_ref, projection = self._validate_snapshot(
                snapshot, raw=raw, request=request,
                snapshot_hash=snapshot_hash, snapshot_ref=snapshot_ref)
            receipt = {
                "final_ref": final_ref,
                "protocol": _PROTOCOL,
                "raw": {
                    "bytes": len(raw), "content_hash": raw_hash,
                    "ref": raw_ref,
                },
                "request": request,
                "request_hash": _content_hash(_canonical_bytes(request)),
                "snapshot": {
                    "bytes": len(snapshot_raw), "content_hash": snapshot_hash,
                    "ref": snapshot_ref,
                },
            }
            receipt_raw = _canonical_bytes(receipt)
            self.owner_guard()
            self._publish_file(
                _QUERY_DIR, receipt_name, receipt_raw, mode=0o600,
                maximum=_MAX_RECEIPT_BYTES)
            return {"final_ref": final_ref, "results": projection}


__all__ = [
    "ArxivNoveltySearchProvider",
    "NoveltySearchError",
    "NoveltySearchProviderError",
]
