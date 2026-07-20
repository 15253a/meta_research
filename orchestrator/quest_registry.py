"""Multi-quest registry for the authenticated local Web console.

Each quest is a physically separate work-root.  Creation happens in a hidden
staging directory, optionally installs an immutable qualification contract,
then initializes a fresh SQLite database through the normal frozen schema +
WriteDaemon boundary and publishes with one directory rename.
The console may then only open that published database read-only and append to
that quest's own inbox spool.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import yaml

from . import database
from .goalbrief import parse_goal_brief
from .qualification_firewall import CONTRACT_RELATIVE_PATH, install_contract
from .statestore_sqlite import SQLiteStateStore
from .writedaemon import WriteDaemon


_QUEST_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_MAX_TITLE_CHARS = 200
_MAX_BRIEF_BYTES = 256 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_CREATE_RECEIPT_BYTES = 128 * 1024
_MAX_QUALIFICATION_CONTRACT_BYTES = 256 * 1024
_MANIFEST_VERSION = 2
_LEGACY_MANIFEST_VERSION = 1
_CREATE_RECEIPT_VERSION = 1
_IDEMPOTENCY_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class QuestConflictError(ValueError):
    """An idempotent quest id is already bound to different immutable input."""


class QuestCorruptError(RuntimeError):
    """A published quest cannot prove its manifest/filesystem identity."""


@dataclass(frozen=True)
class Quest:
    quest_id: str
    title: str
    created_at: str
    template_id: Optional[str]
    goal_brief_sha256: str
    work_root: Path
    qualification_profile_id: Optional[str] = None
    qualification_task: Optional[str] = None
    qualification_contract_sha256: Optional[str] = None
    created: bool = False

    @property
    def db_path(self) -> Path:
        return self.work_root / "research.sqlite"

    @property
    def goal_brief_path(self) -> Path:
        return self.work_root / "goal_brief.md"

    def public_dict(self) -> Dict[str, object]:
        qualification = None
        if self.qualification_profile_id is not None:
            qualification = {
                "profile_id": self.qualification_profile_id,
                "task": self.qualification_task,
                "contract_sha256": self.qualification_contract_sha256,
                "installed": True,
            }
        return {
            "quest_id": self.quest_id,
            "title": self.title,
            "created_at": self.created_at,
            "template_id": self.template_id,
            "goal_brief_sha256": self.goal_brief_sha256,
            "qualification": qualification,
        }


def _validate_slug(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _QUEST_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 须匹配 {_QUEST_ID_RE.pattern}")
    return value


def _regular_nofollow(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise QuestCorruptError(f"{label} 不可读: {error}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise QuestCorruptError(f"{label} 须为非 symlink 常规文件")
    return info


def _write_new_file(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _canonical_qualification_input(
        value: object) -> Tuple[Mapping[str, Any], str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("qualification_contract 须为 JSON object")
    materialized = dict(value)
    try:
        raw = (json.dumps(
            materialized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("qualification_contract 不可 canonicalize") from error
    if not 2 <= len(raw) <= _MAX_QUALIFICATION_CONTRACT_BYTES:
        raise ValueError("qualification_contract 大小非法")
    task = materialized.get("task")
    if task not in {"T1", "T2"}:
        raise ValueError("qualification_contract.task 须为 T1 或 T2")
    return materialized, task, "sha256:" + hashlib.sha256(raw).hexdigest()


def _qualification_contract_identity(work: Path) -> Tuple[str, str]:
    """Read only the installed local contract identity, never external views."""
    try:
        root_info = work.lstat()
    except OSError as error:
        raise QuestCorruptError("qualification quest work-root 不可读") from error
    parent = work
    for part in CONTRACT_RELATIVE_PATH.parts[:-1]:
        parent = parent / part
        try:
            info = parent.lstat()
        except OSError as error:
            raise QuestCorruptError("qualification contract 目录缺失") from error
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != root_info.st_uid):
            raise QuestCorruptError("qualification contract 目录身份非法")

    path = work / CONTRACT_RELATIVE_PATH
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise QuestCorruptError("qualification contract 不可安全打开") from error
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != root_info.st_uid
                or stat.S_IMODE(before.st_mode) != 0o400
                or not 2 <= before.st_size <= _MAX_QUALIFICATION_CONTRACT_BYTES):
            raise QuestCorruptError("qualification contract 身份/权限/大小非法")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise QuestCorruptError("qualification contract 被截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             after.st_ctime_ns, after.st_mode, after.st_uid, after.st_nlink)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns, before.st_mode, before.st_uid, before.st_nlink)):
            raise QuestCorruptError("qualification contract 读取期间身份漂移")
    finally:
        os.close(fd)

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise QuestCorruptError(f"qualification contract 含重复 key: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                QuestCorruptError(
                    f"qualification contract 含非有限数: {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QuestCorruptError("qualification contract 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise QuestCorruptError("qualification contract 须为 object")
    try:
        canonical = (json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise QuestCorruptError("qualification contract 无法 canonicalize") from error
    if raw != canonical:
        raise QuestCorruptError("qualification contract 非 canonical JSON")
    task = value.get("task")
    if task not in {"T1", "T2"}:
        raise QuestCorruptError("qualification contract task 非法")
    return task, "sha256:" + hashlib.sha256(raw).hexdigest()


class QuestRegistry:
    """Directory-backed registry; published quest directories are the index."""

    def __init__(self, root: Path, system_root: Path):
        raw_root = Path(root)
        if raw_root.exists() and raw_root.is_symlink():
            raise ValueError("quests_root 不得是 symlink")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = raw_root.resolve(strict=True)
        self.system_root = Path(system_root).resolve(strict=True)
        self.quests_dir = self.root / "quests"
        if self.quests_dir.exists() and self.quests_dir.is_symlink():
            raise ValueError("quests/ 不得是 symlink")
        self.quests_dir.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(self.quests_dir.lstat().st_mode):
            raise ValueError("quests/ 须为目录")
        self.state_dir = self.root / "state"
        if self.state_dir.exists() and self.state_dir.is_symlink():
            raise ValueError("state/ 不得是 symlink")
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(self.state_dir.lstat().st_mode):
            raise ValueError("state/ 须为目录")
        self.create_receipts_dir = self.state_dir / "quest-create-requests"
        if self.create_receipts_dir.exists() and self.create_receipts_dir.is_symlink():
            raise ValueError("quest-create-requests/ 不得是 symlink")
        self.create_receipts_dir.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(self.create_receipts_dir.lstat().st_mode):
            raise ValueError("quest-create-requests/ 须为目录")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        path = self.root / ".quest-registry.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _quest_dir(self, quest_id: object) -> Path:
        safe = _validate_slug(quest_id, label="quest_id")
        return self.quests_dir / safe

    def bind_create_request(self, idempotency_key: object,
                            request_body: object) -> None:
        """Durably bind one HTTP idempotency key to one immutable request body.

        Quest creation itself is already idempotent by ``quest_id``.  This
        second binding closes the subtler hole where a client accidentally
        reused one transport key for two different quest ids: without a
        receipt both creates could succeed and the response key would lie
        about operation identity.  The receipt is committed *before* quest
        publication, so a crash can only require replaying the same body.
        """
        if (not isinstance(idempotency_key, str)
                or _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None):
            raise ValueError("quest create idempotency_key 须为 32 位小写 hex")
        if not isinstance(request_body, dict):
            raise ValueError("quest create request 须为 JSON object")
        try:
            canonical = json.dumps(
                request_body, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("quest create request 不可 canonicalize") from error
        if len(canonical) > _MAX_MANIFEST_BYTES:
            raise ValueError("quest create request 过大")
        digest = hashlib.sha256(canonical).hexdigest()
        receipt_path = self.create_receipts_dir / f"{idempotency_key}.json"
        receipt = {
            "version": _CREATE_RECEIPT_VERSION,
            "idempotency_key": idempotency_key,
            "request_sha256": digest,
            "request": request_body,
        }
        encoded = (json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")

        with self._locked():
            if receipt_path.exists():
                info = _regular_nofollow(
                    receipt_path, label=f"quest create receipt {idempotency_key}")
                if info.st_size > _MAX_CREATE_RECEIPT_BYTES:
                    raise QuestCorruptError("quest create receipt 过大")
                try:
                    existing = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise QuestCorruptError("quest create receipt 损坏") from error
                expected_keys = {
                    "version", "idempotency_key", "request_sha256", "request"}
                if (not isinstance(existing, dict)
                        or set(existing) != expected_keys
                        or existing.get("version") != _CREATE_RECEIPT_VERSION
                        or existing.get("idempotency_key") != idempotency_key
                        or existing.get("request_sha256") != digest
                        or existing.get("request") != request_body):
                    raise QuestConflictError(
                        f"Idempotency-Key {idempotency_key} 已绑定不同 quest 创建请求")
                return
            _write_new_file(receipt_path, encoded)
            _fsync_dir(self.create_receipts_dir)

    def _load(self, quest_id: object) -> Quest:
        qid = _validate_slug(quest_id, label="quest_id")
        work = self.quests_dir / qid
        try:
            info = work.lstat()
        except OSError as error:
            raise KeyError(f"quest 不存在: {qid}") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise QuestCorruptError(f"quest {qid} 不是安全目录")
        manifest_path = work / "quest.json"
        minfo = _regular_nofollow(manifest_path, label=f"quest {qid} manifest")
        if minfo.st_size > _MAX_MANIFEST_BYTES:
            raise QuestCorruptError(f"quest {qid} manifest 过大")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QuestCorruptError(f"quest {qid} manifest 损坏") from error
        if not isinstance(manifest, dict):
            raise QuestCorruptError(f"quest {qid} manifest 形状不合法")
        version = manifest.get("version")
        common = {"version", "quest_id", "title", "created_at", "template_id",
                  "goal_brief_sha256"}
        expected = (common if version == _LEGACY_MANIFEST_VERSION
                    else common | {"qualification"})
        if version not in {_LEGACY_MANIFEST_VERSION, _MANIFEST_VERSION}:
            raise QuestCorruptError(f"quest {qid} manifest version 不支持")
        if set(manifest) != expected:
            raise QuestCorruptError(f"quest {qid} manifest 形状不合法")
        if manifest["quest_id"] != qid:
            raise QuestCorruptError(f"quest {qid} manifest identity 不匹配")
        title = manifest["title"]
        if not isinstance(title, str) or not title or len(title) > _MAX_TITLE_CHARS:
            raise QuestCorruptError(f"quest {qid} title 不合法")
        template_id = manifest["template_id"]
        if template_id is not None:
            try:
                _validate_slug(template_id, label="template_id")
            except ValueError as error:
                raise QuestCorruptError(f"quest {qid} template_id 不合法") from error
        digest = manifest["goal_brief_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise QuestCorruptError(f"quest {qid} goal brief digest 不合法")
        brief_info = _regular_nofollow(work / "goal_brief.md", label=f"quest {qid} goal_brief")
        if brief_info.st_size > _MAX_BRIEF_BYTES:
            raise QuestCorruptError(f"quest {qid} goal_brief 过大")
        brief_body = (work / "goal_brief.md").read_bytes()
        if hashlib.sha256(brief_body).hexdigest() != digest:
            raise QuestCorruptError(f"quest {qid} goal_brief hash 漂移")
        _regular_nofollow(work / "research.sqlite", label=f"quest {qid} research.sqlite")

        qualification_profile_id = None
        qualification_task = None
        qualification_contract_sha256 = None
        contract_path = work / CONTRACT_RELATIVE_PATH
        qualification = None if version == _LEGACY_MANIFEST_VERSION else manifest["qualification"]
        if qualification is None:
            if os.path.lexists(contract_path):
                raise QuestCorruptError(
                    f"quest {qid} 未声明 qualification 却存在 contract")
        else:
            if (not isinstance(qualification, dict)
                    or set(qualification) != {"profile_id", "task", "contract_sha256"}):
                raise QuestCorruptError(f"quest {qid} qualification 形状不合法")
            try:
                qualification_profile_id = _validate_slug(
                    qualification.get("profile_id"), label="qualification.profile_id")
            except ValueError as error:
                raise QuestCorruptError(
                    f"quest {qid} qualification profile_id 非法") from error
            qualification_task = qualification.get("task")
            qualification_contract_sha256 = qualification.get("contract_sha256")
            if (qualification_task not in {"T1", "T2"}
                    or not isinstance(qualification_contract_sha256, str)
                    or _SHA256_RE.fullmatch(qualification_contract_sha256) is None):
                raise QuestCorruptError(f"quest {qid} qualification identity 非法")
            actual_task, actual_hash = _qualification_contract_identity(work)
            if (actual_task != qualification_task
                    or actual_hash != qualification_contract_sha256):
                raise QuestCorruptError(f"quest {qid} qualification contract identity 漂移")
        return Quest(
            quest_id=qid, title=title, created_at=str(manifest["created_at"]),
            template_id=template_id, goal_brief_sha256=digest, work_root=work,
            qualification_profile_id=qualification_profile_id,
            qualification_task=qualification_task,
            qualification_contract_sha256=qualification_contract_sha256)

    def get(self, quest_id: object) -> Quest:
        return self._load(quest_id)

    def list(self) -> List[Quest]:
        quests: List[Quest] = []
        for entry in sorted(self.quests_dir.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            if _QUEST_ID_RE.fullmatch(entry.name) is None:
                raise QuestCorruptError(f"quests/ 含非法条目: {entry.name}")
            quests.append(self._load(entry.name))
        return quests

    def template_text(self, template_id: object) -> str:
        safe = _validate_slug(template_id, label="template_id")
        base = (self.system_root / "quest_templates").resolve(strict=True)
        candidate = (base / safe / "goal_brief.md").resolve(strict=True)
        try:
            candidate.relative_to(base)
        except ValueError as error:
            raise ValueError("template 路径逃逸") from error
        info = _regular_nofollow(candidate, label=f"template {safe}")
        if info.st_size > _MAX_BRIEF_BYTES:
            raise ValueError("template goal_brief 过大")
        try:
            return candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("template goal_brief 不是 UTF-8") from error

    def create_from_template(self, *, quest_id: object, title: object,
                             template_id: object,
                             qualification_profile_id: Optional[object] = None,
                             qualification_contract: Optional[object] = None) -> Quest:
        safe_template = _validate_slug(template_id, label="template_id")
        return self.create(
            quest_id=quest_id, title=title,
            goal_brief_md=self.template_text(safe_template), template_id=safe_template,
            qualification_profile_id=qualification_profile_id,
            qualification_contract=qualification_contract)

    def create(self, *, quest_id: object, title: object, goal_brief_md: object,
               template_id: Optional[object] = None,
               qualification_profile_id: Optional[object] = None,
               qualification_contract: Optional[object] = None) -> Quest:
        qid = _validate_slug(quest_id, label="quest_id")
        if not isinstance(title, str):
            raise ValueError("title 须为字符串")
        title = title.strip()
        if not title or len(title) > _MAX_TITLE_CHARS or any(
                (ord(char) < 0x20 and char not in "\t") or ord(char) == 0x7f
                for char in title):
            raise ValueError(f"title 须为 1..{_MAX_TITLE_CHARS} 字符且不含控制字符")
        if not isinstance(goal_brief_md, str):
            raise ValueError("goal_brief_md 须为字符串")
        brief_body = goal_brief_md.encode("utf-8")
        if not brief_body or len(brief_body) > _MAX_BRIEF_BYTES:
            raise ValueError(f"goal_brief_md 须为 1..{_MAX_BRIEF_BYTES} bytes")
        safe_template = None if template_id is None else _validate_slug(
            template_id, label="template_id")
        if ((qualification_profile_id is None)
                != (qualification_contract is None)):
            raise ValueError(
                "qualification_profile_id 与 qualification_contract 必须成对提供")
        safe_qualification_profile = None
        qualification_value = None
        requested_qualification_task = None
        requested_contract_hash = None
        if qualification_profile_id is not None:
            safe_qualification_profile = _validate_slug(
                qualification_profile_id, label="qualification_profile_id")
            (qualification_value, requested_qualification_task,
             requested_contract_hash) = _canonical_qualification_input(
                 qualification_contract)
        digest = hashlib.sha256(brief_body).hexdigest()
        final = self._quest_dir(qid)

        with self._locked():
            if final.exists():
                existing = self._load(qid)
                if (existing.title != title
                        or existing.goal_brief_sha256 != digest
                        or existing.template_id != safe_template
                        or existing.qualification_profile_id
                        != safe_qualification_profile
                        or existing.qualification_task
                        != requested_qualification_task
                        or existing.qualification_contract_sha256
                        != requested_contract_hash):
                    raise QuestConflictError(
                        f"quest_id {qid} 已绑定不同创建输入")
                return replace(existing, created=False)

            staging = self.quests_dir / f".creating-{qid}-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700)
            try:
                brief_path = staging / "goal_brief.md"
                _write_new_file(brief_path, brief_body)
                brief = parse_goal_brief(brief_path)
                policy = yaml.safe_load(
                    (self.system_root / "policies" / "policy.yaml").read_text(encoding="utf-8"))
                if not isinstance(policy, dict):
                    raise ValueError("policy.yaml 须为 object")
                qualification = None
                if qualification_value is not None:
                    qualification = install_contract(staging, qualification_value)
                    if (qualification.task != requested_qualification_task
                            or qualification.contract_sha256 != requested_contract_hash):
                        raise RuntimeError("qualification installer identity 漂移")
                db_path = staging / "research.sqlite"
                conn = database.connect(db_path)
                try:
                    os.chmod(db_path, 0o600)
                    SQLiteStateStore(WriteDaemon(conn), policy).create_goal(
                        text=brief["body_md"], predicate_json=brief["predicate_json"])
                finally:
                    conn.close()
                (staging / "state").mkdir(mode=0o700, exist_ok=True)
                created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                manifest = {
                    "version": _MANIFEST_VERSION,
                    "quest_id": qid,
                    "title": title,
                    "created_at": created_at,
                    "template_id": safe_template,
                    "goal_brief_sha256": digest,
                    "qualification": (
                        None if qualification is None else {
                            "profile_id": safe_qualification_profile,
                            "task": qualification.task,
                            "contract_sha256": qualification.contract_sha256,
                        }),
                }
                _write_new_file(
                    staging / "quest.json",
                    (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                     + "\n").encode("utf-8"))
                _fsync_dir(staging)
                os.rename(staging, final)
                _fsync_dir(self.quests_dir)
            except BaseException:
                if staging.exists():
                    shutil.rmtree(staging)
                raise
            return replace(self._load(qid), created=True)
