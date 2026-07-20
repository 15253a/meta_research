"""Immutable, filesystem-backed qualification profile catalog.

Profiles are deployment inputs, not research state.  The registry reads every
``*.json`` file exactly once during construction, validates the closed JSON
shape without touching any referenced dataset/truth path, and retains frozen
canonical bytes in memory.  Full filesystem ownership, view-receipt and
task-specific qualification semantics remain the authority of
``qualification_firewall.install_contract``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .qualification_firewall import CONTRACT_PROTOCOL


_MAX_PROFILE_BYTES = 256 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PROFILE_KEYS = frozenset({
    "version", "profile_id", "title", "template_id", "contract",
})
_CONTRACT_KEYS = frozenset({
    "version", "protocol", "task", "research_uid", "evaluator_uid",
    "forbid_code_imports", "mounts", "sealed_truth", "final",
})
_MOUNT_KEYS = frozenset({
    "path", "role", "dataset", "fold", "view_receipt_sha256",
})
_FINAL_KEYS = frozenset({
    "classes", "seeds", "folds", "unit_ids", "gpu_required",
})


class QualificationProfileError(ValueError):
    """A profile catalog entry is malformed, unsafe, or ambiguous."""


def _canonical(value: Any) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise QualificationProfileError(
            "qualification profile 含非 JSON/非有限值") from error


def _strict_json(raw: bytes, *, label: str) -> Dict[str, Any]:
    def unique_object(pairs: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise QualificationProfileError(f"{label} 含重复 key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                QualificationProfileError(f"{label} 含非有限数: {token}")),
        )
    except QualificationProfileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationProfileError(f"{label} 不是严格 UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise QualificationProfileError(f"{label} 顶层须为 object")
    if raw != _canonical(value):
        raise QualificationProfileError(
            f"{label} 须为 canonical JSON（sorted/compact/+newline）")
    return value


def _closed_object(value: Any, expected: frozenset[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise QualificationProfileError(f"{label} 字段闭包非法")
    return value


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualificationProfileError(f"{label} 须为 >= {minimum} 的整数")
    return value


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or len(value.encode("utf-8")) > maximum):
        raise QualificationProfileError(f"{label} 须为有界非空 UTF-8 文本")
    return value


def _slug(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise QualificationProfileError(f"{label} 须匹配 {_SLUG_RE.pattern}")
    return value


def _path_shape(value: Any, *, label: str) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not os.path.isabs(value) or os.path.normpath(value) != value):
        raise QualificationProfileError(f"{label} 须为规范绝对路径")
    return value


def _optional_hash(value: Any, *, label: str) -> None:
    if value is not None and (
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None):
        raise QualificationProfileError(f"{label} 须为 null 或 sha256:<64-lowercase-hex>")


def _validate_contract_structure(value: Any) -> Mapping[str, Any]:
    """Validate only the closed, path-independent contract representation.

    In particular this function deliberately does not stat a mount/truth path,
    validate a view receipt, compare owners, or duplicate T1/T2 admission
    semantics.  ``install_contract`` remains the single authority for those
    checks at the eventual work-root boundary.
    """
    contract = _closed_object(value, _CONTRACT_KEYS, label="contract")
    if (type(contract["version"]) is not int or contract["version"] != 1
            or contract["protocol"] != CONTRACT_PROTOCOL
            or contract["task"] not in {"T1", "T2"}
            or contract["forbid_code_imports"] is not True):
        raise QualificationProfileError("contract version/protocol/task/import 边界非法")
    research_uid = _integer(contract["research_uid"], label="contract.research_uid")
    evaluator_uid = _integer(contract["evaluator_uid"], label="contract.evaluator_uid")
    if research_uid == evaluator_uid:
        raise QualificationProfileError("contract research_uid/evaluator_uid 必须不同")

    mounts = contract["mounts"]
    if not isinstance(mounts, list) or not 1 <= len(mounts) <= 64:
        raise QualificationProfileError("contract.mounts 须为 1..64 项数组")
    for index, raw_mount in enumerate(mounts):
        mount = _closed_object(raw_mount, _MOUNT_KEYS, label=f"contract.mounts[{index}]")
        _path_shape(mount["path"], label=f"contract.mounts[{index}].path")
        _bounded_text(mount["role"], label=f"contract.mounts[{index}].role", maximum=64)
        _bounded_text(
            mount["dataset"], label=f"contract.mounts[{index}].dataset", maximum=128)
        fold = mount["fold"]
        if fold is not None:
            _integer(fold, label=f"contract.mounts[{index}].fold")
        _optional_hash(
            mount["view_receipt_sha256"],
            label=f"contract.mounts[{index}].view_receipt_sha256")

    truth = _closed_object(
        contract["sealed_truth"], frozenset({"path", "sha256"}),
        label="contract.sealed_truth")
    _path_shape(truth["path"], label="contract.sealed_truth.path")
    if not isinstance(truth["sha256"], str) or _SHA256_RE.fullmatch(truth["sha256"]) is None:
        raise QualificationProfileError("contract.sealed_truth.sha256 非法")

    final = _closed_object(contract["final"], _FINAL_KEYS, label="contract.final")
    _integer(final["classes"], label="contract.final.classes", minimum=1)
    if not isinstance(final["gpu_required"], bool):
        raise QualificationProfileError("contract.final.gpu_required 须为 bool")
    for key in ("seeds", "folds"):
        items = final[key]
        if not isinstance(items, list):
            raise QualificationProfileError(f"contract.final.{key} 须为数组")
        for index, item in enumerate(items):
            _integer(item, label=f"contract.final.{key}[{index}]")
    units = final["unit_ids"]
    if (not isinstance(units, list)
            or any(not isinstance(item, str) or _UNIT_RE.fullmatch(item) is None
                   for item in units)):
        raise QualificationProfileError("contract.final.unit_ids 须为规范 ID 数组")
    return contract


def _read_profile_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise QualificationProfileError(f"profile 不可安全打开: {path.name}") from error
    try:
        before = os.fstat(fd)
        allowed_owners = {0, os.geteuid()}
        if (not stat.S_ISREG(before.st_mode)
                or before.st_uid not in allowed_owners
                or before.st_mode & 0o022
                or before.st_size <= 0
                or before.st_size > _MAX_PROFILE_BYTES):
            raise QualificationProfileError(
                f"profile {path.name} 类型/owner/权限/大小非法")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise QualificationProfileError(f"profile {path.name} 读取时截断")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise QualificationProfileError(f"profile {path.name} 读取时变长")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise QualificationProfileError(f"profile {path.name} 读取期间身份漂移")
        return b"".join(chunks)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class QualificationProfile:
    profile_id: str
    title: str
    template_id: str
    task: str
    contract_sha256: str
    _contract_raw: bytes = field(repr=False, compare=True)
    _datasets: tuple[str, ...] = field(repr=False, compare=True)
    _gpu_required: bool = field(repr=False, compare=True)

    def contract(self) -> Dict[str, Any]:
        """Return a fresh deep copy of the frozen qualification contract."""
        return json.loads(self._contract_raw.decode("utf-8"))

    def public_dict(self) -> Dict[str, Any]:
        """Return the deliberately redacted profile projection for Web clients."""
        return {
            "profile_id": self.profile_id,
            "title": self.title,
            "template_id": self.template_id,
            "task": self.task,
            "contract_sha256": self.contract_sha256,
            "datasets": list(self._datasets),
            "gpu_required": self._gpu_required,
        }


class QualificationProfileRegistry:
    """Load and freeze all safe ``*.json`` profiles beneath one catalog root."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._profiles: Dict[str, QualificationProfile] = {}
        if not os.path.lexists(self.root):
            return
        try:
            root_info = os.lstat(self.root)
        except OSError as error:
            raise QualificationProfileError("qualification profile 目录不可读") from error
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise QualificationProfileError("qualification profile root 须为非 symlink 目录")

        try:
            candidates = sorted(
                (entry.name for entry in os.scandir(self.root)
                 if entry.name.endswith(".json")),
                key=lambda name: (name.casefold(), name),
            )
        except OSError as error:
            raise QualificationProfileError("qualification profile 目录无法枚举") from error
        for name in candidates:
            path = self.root / name
            raw = _read_profile_file(path)
            value = _strict_json(raw, label=f"profile {name}")
            profile = self._build_profile(value, label=f"profile {name}")
            if profile.profile_id in self._profiles:
                raise QualificationProfileError(
                    f"qualification profile_id 重复: {profile.profile_id}")
            self._profiles[profile.profile_id] = profile

    @staticmethod
    def _build_profile(value: Mapping[str, Any], *, label: str) -> QualificationProfile:
        profile = _closed_object(value, _PROFILE_KEYS, label=label)
        if type(profile["version"]) is not int or profile["version"] != 1:
            raise QualificationProfileError(f"{label}.version 须为 1")
        profile_id = _slug(profile["profile_id"], label=f"{label}.profile_id")
        template_id = _slug(profile["template_id"], label=f"{label}.template_id")
        title = _bounded_text(profile["title"], label=f"{label}.title", maximum=200)
        contract = _validate_contract_structure(profile["contract"])
        contract_raw = _canonical(contract)
        datasets = tuple(sorted(
            {str(item["dataset"]) for item in contract["mounts"]},
            key=lambda item: (item.casefold(), item),
        ))
        return QualificationProfile(
            profile_id=profile_id,
            title=title,
            template_id=template_id,
            task=str(contract["task"]),
            contract_sha256="sha256:" + hashlib.sha256(contract_raw).hexdigest(),
            _contract_raw=contract_raw,
            _datasets=datasets,
            _gpu_required=bool(contract["final"]["gpu_required"]),
        )

    def list(self) -> List[QualificationProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def get(self, profile_id: str) -> QualificationProfile:
        try:
            return self._profiles[profile_id]
        except (KeyError, TypeError):
            raise KeyError(profile_id) from None

    def __len__(self) -> int:
        return len(self._profiles)


__all__ = [
    "QualificationProfile", "QualificationProfileError",
    "QualificationProfileRegistry",
]
