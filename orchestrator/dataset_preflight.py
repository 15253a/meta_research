"""Bounded, read-only recognition of datasets in one managed upload draft.

The browser never supplies filesystem authority to this module.  A caller
passes one server-managed files root plus a closed manifest whose path fields
are relative POSIX names.  The scanner reconciles that manifest against the
entire tree, follows no links, opens no device, and only inspects names and
archive metadata.  It does not extract archives, parse scientific payloads,
create qualification contracts, or claim that a dataset is scientifically
admissible.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union


PREFLIGHT_PROTOCOL = "managed-dataset-preflight-v1"
SUPPORTED_DATASETS = ("DREAMER", "SEED", "SEED-IV", "FACED", "DEAP", "MPED")
T1_EXPLORE_DATASETS = ("SEED", "SEED-IV", "FACED", "DEAP", "MPED")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_ARCHIVE_TAR_SUFFIXES = (
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
)
_SCIENTIFIC_CAVEAT = (
    "自动识别仅是文件候选预检，不验证真实性、许可、完整性、标签语义或科学资格；"
    "后续仍须由资格数据适配器和 qualification firewall 独立验收。"
)
_MAX_BUNDLE_WARNINGS = 20


class DatasetPreflightError(ValueError):
    """The managed draft or its closed manifest is unsafe or inconsistent."""


@dataclass(frozen=True)
class DatasetPreflightLimits:
    """Finite traversal and archive-metadata budgets.

    Defaults accommodate large EEG archives while bounding directory and
    central-directory amplification.  Tests/deployments may choose tighter
    values without changing recognition semantics.
    """

    max_files: int = 512
    max_directories: int = 256
    max_depth: int = 12
    max_total_bytes: int = 2 * 1024 ** 4
    max_file_bytes: int = 512 * 1024 ** 3
    max_archives: int = 64
    max_archive_members: int = 100_000
    max_archive_directory_bytes: int = 64 * 1024 ** 2
    max_archive_uncompressed_bytes: int = 2 * 1024 ** 4
    max_archive_member_bytes: int = 512 * 1024 ** 3
    max_compression_ratio: float = 1000.0
    max_name_bytes: int = 4096
    max_public_sample_names: int = 5

    def __post_init__(self) -> None:
        integer_fields = (
            "max_files", "max_directories", "max_depth", "max_total_bytes",
            "max_file_bytes", "max_archives", "max_archive_members",
            "max_archive_directory_bytes", "max_archive_uncompressed_bytes",
            "max_archive_member_bytes", "max_name_bytes",
            "max_public_sample_names",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DatasetPreflightError(f"dataset preflight limit {name} 须为正整数")
        ratio = self.max_compression_ratio
        if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                or not math.isfinite(float(ratio)) or ratio < 1):
            raise DatasetPreflightError(
                "dataset preflight limit max_compression_ratio 须为 >=1 的有限数")


def _safe_relative(value: Any, *, label: str, maximum: int) -> str:
    """Validate a non-authoritative relative POSIX display/storage name."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DatasetPreflightError(f"{label} 须为非空相对 POSIX 路径")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DatasetPreflightError(f"{label} 不是规范 UTF-8 文本") from error
    if len(encoded) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DatasetPreflightError(f"{label} 含控制符或超过长度上限")
    # Reject browser fake host paths as well as ordinary POSIX traversal.
    if ("\\" in value or value.startswith("/") or PureWindowsPath(value).drive
            or PureWindowsPath(value).is_absolute()):
        raise DatasetPreflightError(f"{label} 不得是浏览器/主机绝对路径")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise DatasetPreflightError(f"{label} 含空段或 . / ..")
    if PurePosixPath(value).as_posix() != value:
        raise DatasetPreflightError(f"{label} 须为规范相对 POSIX 路径")
    return value


def _safe_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DatasetPreflightError(f"{label} 须匹配 {_ID_RE.pattern}")
    return value


def _size(value: Any, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise DatasetPreflightError(f"{label} 须为 [0,{maximum}] 内整数")
    return value


@dataclass(frozen=True)
class ManagedDraftFile:
    """One file identity produced by the managed uploader.

    ``stored_relpath`` is resolved only beneath the server-provided managed
    root.  ``display_relpath`` is untrusted browser metadata used solely for
    recognition and public display; both are required to be relative.
    ``bundle_id`` groups all files from one selected file/directory.
    """

    file_id: str
    bundle_id: str
    stored_relpath: str
    display_relpath: str
    size_bytes: int


@dataclass(frozen=True)
class ManagedDraftManifest:
    version: int
    files: Tuple[ManagedDraftFile, ...]

    @classmethod
    def from_value(
            cls, value: Union["ManagedDraftManifest", Mapping[str, Any]], *,
            limits: DatasetPreflightLimits) -> "ManagedDraftManifest":
        if isinstance(value, cls):
            raw_files: Sequence[Any] = value.files
            version = value.version
        else:
            if not isinstance(value, Mapping) or set(value) != {"version", "files"}:
                raise DatasetPreflightError("managed draft manifest 字段须恰为 version/files")
            version = value["version"]
            raw_files = value["files"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise DatasetPreflightError("managed draft manifest version 须为整数 1")
        if (not isinstance(raw_files, (list, tuple))
                or len(raw_files) > limits.max_files):
            raise DatasetPreflightError("managed draft manifest files 须为有界数组")

        files: List[ManagedDraftFile] = []
        file_ids: Set[str] = set()
        stored_paths: Set[str] = set()
        for index, raw in enumerate(raw_files):
            label = f"managed draft manifest files[{index}]"
            if isinstance(raw, ManagedDraftFile):
                item = raw
            else:
                expected = {
                    "file_id", "bundle_id", "stored_relpath", "display_relpath",
                    "size_bytes",
                }
                if not isinstance(raw, Mapping) or set(raw) != expected:
                    raise DatasetPreflightError(f"{label} 字段闭包非法")
                item = ManagedDraftFile(
                    file_id=raw["file_id"], bundle_id=raw["bundle_id"],
                    stored_relpath=raw["stored_relpath"],
                    display_relpath=raw["display_relpath"],
                    size_bytes=raw["size_bytes"],
                )
            item = ManagedDraftFile(
                file_id=_safe_id(item.file_id, label=f"{label}.file_id"),
                bundle_id=_safe_id(item.bundle_id, label=f"{label}.bundle_id"),
                stored_relpath=_safe_relative(
                    item.stored_relpath, label=f"{label}.stored_relpath",
                    maximum=limits.max_name_bytes),
                display_relpath=_safe_relative(
                    item.display_relpath, label=f"{label}.display_relpath",
                    maximum=limits.max_name_bytes),
                size_bytes=_size(
                    item.size_bytes, label=f"{label}.size_bytes",
                    maximum=limits.max_file_bytes),
            )
            if item.file_id in file_ids:
                raise DatasetPreflightError(f"managed draft manifest file_id 重复: {item.file_id}")
            if item.stored_relpath in stored_paths:
                raise DatasetPreflightError(
                    f"managed draft manifest stored_relpath 重复: {item.stored_relpath}")
            file_ids.add(item.file_id)
            stored_paths.add(item.stored_relpath)
            files.append(item)
        return cls(version=1, files=tuple(files))

    def canonical_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "files": [{
                "file_id": item.file_id,
                "bundle_id": item.bundle_id,
                "stored_relpath": item.stored_relpath,
                "display_relpath": item.display_relpath,
                "size_bytes": item.size_bytes,
            } for item in self.files],
        }


@dataclass(frozen=True)
class DatasetCandidate:
    candidate_id: str
    bundle_id: str
    dataset: str
    confidence: float
    role_hint: str
    reasons: Tuple[str, ...]
    warnings: Tuple[str, ...]
    file_count: int
    sample_names: Tuple[str, ...]

    def public_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "bundle_id": self.bundle_id,
            "dataset": self.dataset,
            "confidence": self.confidence,
            "role_hint": self.role_hint,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "file_count": self.file_count,
            "sample_names": list(self.sample_names),
        }


@dataclass(frozen=True)
class T1RequirementStatus:
    sealed_holdout_candidate_count: int
    sealed_holdout_exactly_one: bool
    explore_datasets: Tuple[str, ...]
    explore_distinct_at_least_three: bool
    candidate_requirements_met: bool

    def public_dict(self) -> Dict[str, Any]:
        return {
            "task": "T1",
            "candidate_requirements_met": self.candidate_requirements_met,
            "sealed_holdout": {
                "dataset": "DREAMER",
                "required": "exactly_one_candidate",
                "observed_candidates": self.sealed_holdout_candidate_count,
                "status": "met" if self.sealed_holdout_exactly_one else "unmet",
            },
            "exploration": {
                "allowed_datasets": list(T1_EXPLORE_DATASETS),
                "required_distinct": 3,
                "observed_distinct": len(self.explore_datasets),
                "observed_datasets": list(self.explore_datasets),
                "status": "met" if self.explore_distinct_at_least_three else "unmet",
            },
            "scientific_qualification_status": "not_assessed",
        }


@dataclass(frozen=True)
class DatasetPreflightReport:
    manifest_sha256: str
    candidates: Tuple[DatasetCandidate, ...]
    t1: T1RequirementStatus
    warnings: Tuple[str, ...]
    file_count: int
    directory_count: int
    total_bytes: int
    archive_count: int
    archive_member_count: int

    def public_dict(self) -> Dict[str, Any]:
        """Return a fresh projection containing no backend absolute path."""
        return {
            "version": 1,
            "protocol": PREFLIGHT_PROTOCOL,
            "manifest_sha256": self.manifest_sha256,
            "scientific_qualification_status": "not_assessed",
            "candidates": [item.public_dict() for item in self.candidates],
            "t1_requirements": self.t1.public_dict(),
            "warnings": list(self.warnings),
            "scan": {
                "file_count": self.file_count,
                "directory_count": self.directory_count,
                "total_bytes": self.total_bytes,
                "archive_count": self.archive_count,
                "archive_member_count": self.archive_member_count,
            },
        }


@dataclass
class _BundleEvidence:
    bundle_id: str
    file_ids: List[str] = field(default_factory=list)
    display_names: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)
    reasons: Dict[str, Set[str]] = field(default_factory=dict)
    source_kinds: Dict[str, Set[str]] = field(default_factory=dict)
    explicit: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    _warning_set: Set[str] = field(default_factory=set)
    deap_data_files: int = 0
    seed_preprocessed: bool = False
    seed_label: bool = False
    seed_subject_file: bool = False

    def warn(self, message: str) -> None:
        if message in self._warning_set:
            return
        self._warning_set.add(message)
        if len(self.warnings) < _MAX_BUNDLE_WARNINGS:
            self.warnings.append(message)
        elif len(self.warnings) == _MAX_BUNDLE_WARNINGS:
            self.warnings.append("其余 archive warning 已截断")

    def add(self, dataset: str, score: float, reason: str, source: str, *,
            explicit: bool = False) -> None:
        self.scores[dataset] = max(score, self.scores.get(dataset, 0.0))
        self.reasons.setdefault(dataset, set()).add(reason)
        self.source_kinds.setdefault(dataset, set()).add(source)
        if explicit:
            self.explicit.add(dataset)

    def observe_name(self, value: str, *, source: str) -> None:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        tokenized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        padded = "-" + tokenized + "-"
        reason = ("上传文件名或相对布局明确包含 {dataset} 标识"
                  if source == "display" else "archive member 名明确包含 {dataset} 标识")
        explicit_score = 0.90 if source == "display" else 0.92

        # SEED-IV must be recognized before SEED and never double-counted by
        # the shorter token.
        seed_iv = (re.search(r"(?:^|-)seed-?iv(?:-|$)", tokenized) is not None)
        if seed_iv:
            self.add("SEED-IV", explicit_score, reason.format(dataset="SEED-IV"),
                     source, explicit=True)
        if (not seed_iv and re.search(r"(?:^|-)seed(?:-|$)", tokenized) is not None):
            self.add("SEED", explicit_score, reason.format(dataset="SEED"),
                     source, explicit=True)
        for dataset, token in (
                ("DREAMER", "dreamer"), ("FACED", "faced"),
                ("DEAP", "deap"), ("MPED", "mped")):
            if "-" + token + "-" in padded:
                self.add(dataset, explicit_score, reason.format(dataset=dataset),
                         source, explicit=True)

        posix = normalized.replace("\\", "/")
        basename = posix.rstrip("/").rsplit("/", 1)[-1]
        if basename == "dreamer.mat":
            self.add("DREAMER", 0.99, "archive 内存在标准 DREAMER.mat 入口",
                     source)
        if re.search(r"(?:^|/)data_preprocessed_python/s\d{2}\.dat$", posix):
            self.deap_data_files += 1
        components = [part for part in re.split(r"[/]+", posix) if part]
        if "preprocessed_eeg" in components:
            self.seed_preprocessed = True
        if basename == "label.mat":
            self.seed_label = True
        if re.fullmatch(r"\d+_\d{8}\.mat", basename):
            self.seed_subject_file = True

    def finish_layout_rules(self) -> None:
        if self.deap_data_files >= 2:
            self.add("DEAP", 0.98,
                     "archive 呈现 DEAP data_preprocessed_python/sNN.dat 多被试布局",
                     "archive-layout")
        elif self.deap_data_files == 1:
            self.add("DEAP", 0.82,
                     "archive 呈现 DEAP data_preprocessed_python/sNN.dat 布局",
                     "archive-layout")
        if self.seed_preprocessed and self.seed_label and self.seed_subject_file:
            # This classic layout alone is insufficient to override an
            # explicit SEED-IV identity.
            if "SEED-IV" not in self.explicit:
                self.add("SEED", 0.84,
                         "相对布局同时包含 Preprocessed_EEG、label.mat 和 subject-date MAT",
                         "layout")


@dataclass
class _ScanCounters:
    files: int = 0
    directories: int = 0
    total_bytes: int = 0
    archives: int = 0
    archive_members: int = 0


def _manifest_hash(manifest: ManagedDraftManifest) -> str:
    raw = (json.dumps(
        manifest.canonical_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode), left.st_nlink,
        left.st_size, left.st_mtime_ns, left.st_ctime_ns,
    ) == (
        right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode), right.st_nlink,
        right.st_size, right.st_mtime_ns, right.st_ctime_ns,
    )


def _member_name_safe(name: Any, limits: DatasetPreflightLimits) -> bool:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        return False
    try:
        if len(name.encode("utf-8")) > limits.max_name_bytes:
            return False
    except UnicodeEncodeError:
        return False
    stripped = name.rstrip("/")
    if not stripped or stripped.startswith("/") or PureWindowsPath(stripped).drive:
        return False
    return not any(part in ("", ".", "..") for part in stripped.split("/"))


def _archive_kind(display_relpath: str) -> Optional[str]:
    lowered = display_relpath.casefold()
    if lowered.endswith(".zip"):
        return "zip"
    if lowered.endswith(_ARCHIVE_TAR_SUFFIXES):
        return "tar"
    return None


def _zip_directory_guard(fileobj: Any, size: int, limits: DatasetPreflightLimits,
                         remaining_members: int) -> None:
    """Bound ZipFile's eager central-directory allocation before construction."""
    tail_size = min(size, 65_557)
    fileobj.seek(size - tail_size)
    tail = fileobj.read(tail_size)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise zipfile.BadZipFile("missing end record")
    fields = struct.unpack_from("<4s4H2LH", tail, position)
    _, disk, directory_disk, disk_entries, entries, directory_size, _offset, comment = fields
    if disk != 0 or directory_disk != 0 or disk_entries != entries:
        raise DatasetPreflightError("ZIP multi-disk archive 不受支持")
    if position + 22 + comment > len(tail):
        raise zipfile.BadZipFile("truncated end record")
    if entries == 0xFFFF or directory_size == 0xFFFFFFFF:
        # ZIP64 locator immediately precedes the ordinary EOCD.
        absolute_eocd = size - tail_size + position
        if absolute_eocd < 20:
            raise DatasetPreflightError("ZIP64 locator 缺失")
        fileobj.seek(absolute_eocd - 20)
        locator = fileobj.read(20)
        if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
            raise DatasetPreflightError("ZIP64 locator 非法")
        _signature, locator_disk, zip64_offset, disks = struct.unpack("<4sLQL", locator)
        if locator_disk != 0 or disks != 1:
            raise DatasetPreflightError("ZIP64 multi-disk archive 不受支持")
        fileobj.seek(zip64_offset)
        record = fileobj.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise DatasetPreflightError("ZIP64 end record 非法")
        unpacked = struct.unpack("<4sQ2H2L4Q", record)
        entries = unpacked[7]
        directory_size = unpacked[8]
    if entries > remaining_members:
        raise DatasetPreflightError("archive member 总数超过安全上限")
    if directory_size > limits.max_archive_directory_bytes:
        raise DatasetPreflightError("ZIP central directory 超过安全上限")


def _observe_archive_member(
        evidence: _BundleEvidence, name: Any, kind: str,
        limits: DatasetPreflightLimits) -> bool:
    if not _member_name_safe(name, limits):
        evidence.warn(f"{kind} 含不安全或过长 member name；该 member 已忽略且从未解压")
        return False
    evidence.observe_name(name, source="archive")
    return True


def _scan_zip(fd: int, file_size: int, evidence: _BundleEvidence,
              counters: _ScanCounters, limits: DatasetPreflightLimits) -> None:
    with os.fdopen(os.dup(fd), "rb", closefd=True) as fileobj:
        try:
            _zip_directory_guard(
                fileobj, file_size, limits,
                limits.max_archive_members - counters.archive_members)
            fileobj.seek(0)
            with zipfile.ZipFile(fileobj, mode="r", allowZip64=True) as archive:
                infos = archive.infolist()
                if counters.archive_members + len(infos) > limits.max_archive_members:
                    raise DatasetPreflightError("archive member 总数超过安全上限")
                total_uncompressed = 0
                for info in infos:
                    counters.archive_members += 1
                    if info.file_size < 0 or info.compress_size < 0:
                        raise DatasetPreflightError("ZIP member size 非法")
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type in {
                            stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK,
                            stat.S_IFIFO, stat.S_IFSOCK}:
                        evidence.warn("ZIP 含 link/device member；该 member 已忽略且从未解压")
                        continue
                    if info.is_dir():
                        _observe_archive_member(evidence, info.filename, "ZIP", limits)
                        continue
                    if info.file_size > limits.max_archive_member_bytes:
                        raise DatasetPreflightError("ZIP member 声明大小超过安全上限")
                    total_uncompressed += info.file_size
                    if total_uncompressed > limits.max_archive_uncompressed_bytes:
                        raise DatasetPreflightError("ZIP 声明解压总量超过安全上限")
                    if info.file_size and (
                            info.compress_size == 0
                            or info.file_size / info.compress_size
                            > float(limits.max_compression_ratio)):
                        raise DatasetPreflightError("ZIP compression ratio 超过安全上限")
                    _observe_archive_member(evidence, info.filename, "ZIP", limits)
        except DatasetPreflightError:
            raise
        except (OSError, EOFError, zipfile.BadZipFile, struct.error):
            evidence.warn("扩展名为 ZIP 但 archive metadata 无法安全读取；仅保留文件名弱证据")


def _scan_tar(fd: int, file_size: int, evidence: _BundleEvidence,
              counters: _ScanCounters, limits: DatasetPreflightLimits) -> None:
    with os.fdopen(os.dup(fd), "rb", closefd=True) as fileobj:
        try:
            with tarfile.open(fileobj=fileobj, mode="r:*") as archive:
                total_uncompressed = 0
                for member in archive:
                    counters.archive_members += 1
                    if counters.archive_members > limits.max_archive_members:
                        raise DatasetPreflightError("archive member 总数超过安全上限")
                    if member.islnk() or member.issym() or member.isdev() or member.isfifo():
                        evidence.warn("TAR 含 link/device member；该 member 已忽略且从未解压")
                        continue
                    if not (member.isfile() or member.isdir()):
                        evidence.warn("TAR 含非普通 member；该 member 已忽略且从未解压")
                        continue
                    if member.isfile():
                        if member.size < 0 or member.size > limits.max_archive_member_bytes:
                            raise DatasetPreflightError("TAR member 声明大小超过安全上限")
                        total_uncompressed += member.size
                        if total_uncompressed > limits.max_archive_uncompressed_bytes:
                            raise DatasetPreflightError("TAR 声明内容总量超过安全上限")
                        if (file_size and total_uncompressed / file_size
                                > float(limits.max_compression_ratio)):
                            raise DatasetPreflightError("TAR compression ratio 超过安全上限")
                    _observe_archive_member(evidence, member.name, "TAR", limits)
        except DatasetPreflightError:
            raise
        except (OSError, EOFError, tarfile.TarError):
            evidence.warn("扩展名为 TAR 但 archive metadata 无法安全读取；仅保留文件名弱证据")


class _ManagedTreeScanner:
    def __init__(self, root: Path, manifest: ManagedDraftManifest,
                 limits: DatasetPreflightLimits):
        if not isinstance(root, Path):
            raise DatasetPreflightError(
                "managed draft files root 须由服务端以 pathlib.Path capability 提供")
        self.limits = limits
        self.manifest = manifest
        self.by_path = {item.stored_relpath: item for item in manifest.files}
        self.unseen = set(self.by_path)
        self.counters = _ScanCounters()
        self.inodes: Set[Tuple[int, int]] = set()
        self.evidence: Dict[str, _BundleEvidence] = {}
        for item in manifest.files:
            bundle = self.evidence.setdefault(item.bundle_id, _BundleEvidence(item.bundle_id))
            bundle.file_ids.append(item.file_id)
            bundle.display_names.append(item.display_relpath)

        try:
            supplied_info = os.lstat(root)
            resolved = root.resolve(strict=True)
            resolved_info = os.lstat(resolved)
        except (OSError, RuntimeError) as error:
            raise DatasetPreflightError("managed draft files root 不存在或不可规范解析") from error
        if (stat.S_ISLNK(supplied_info.st_mode) or not stat.S_ISDIR(supplied_info.st_mode)
                or not stat.S_ISDIR(resolved_info.st_mode)
                or not _same_identity(supplied_info, resolved_info)):
            raise DatasetPreflightError("managed draft files root 须为非 symlink 目录")
        self.root = resolved
        self.root_identity = resolved_info

    def run(self) -> Tuple[_ScanCounters, Dict[str, _BundleEvidence]]:
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            root_fd = os.open(str(self.root), flags)
        except OSError as error:
            raise DatasetPreflightError("managed draft files root 无法安全打开") from error
        try:
            if not _same_identity(self.root_identity, os.fstat(root_fd)):
                raise DatasetPreflightError("managed draft files root 在扫描前身份漂移")
            self._walk(root_fd, (), depth=0)
        finally:
            os.close(root_fd)
        if self.unseen:
            missing = sorted(self.unseen)[0]
            raise DatasetPreflightError(f"manifest 文件在受管 draft root 中缺失: {missing}")
        return self.counters, self.evidence

    def _walk(self, directory_fd: int, components: Tuple[str, ...], *, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise DatasetPreflightError("managed draft 目录深度超过安全上限")
        entries: List[Tuple[str, os.stat_result]] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    if len(entries) >= self.limits.max_files + self.limits.max_directories:
                        raise DatasetPreflightError("managed draft 目录项超过安全上限")
                    try:
                        name_bytes = entry.name.encode("utf-8")
                    except UnicodeEncodeError as error:
                        raise DatasetPreflightError("managed draft 含非 UTF-8 文件名") from error
                    if (len(name_bytes) > self.limits.max_name_bytes or entry.name in (".", "..")
                            or "/" in entry.name or "\x00" in entry.name):
                        raise DatasetPreflightError("managed draft 含非法或过长文件名")
                    entries.append((entry.name, entry.stat(follow_symlinks=False)))
        except DatasetPreflightError:
            raise
        except OSError as error:
            raise DatasetPreflightError("managed draft 目录无法安全枚举") from error

        for name, expected in sorted(entries, key=lambda item: item[0]):
            parts = components + (name,)
            relative = "/".join(parts)
            candidate = self.root.joinpath(*parts)
            try:
                canonical = candidate.resolve(strict=True)
                canonical.relative_to(self.root)
            except (OSError, RuntimeError, ValueError) as error:
                raise DatasetPreflightError(
                    f"managed draft 目录项逃出 canonical root: {relative}") from error
            if stat.S_ISLNK(expected.st_mode):
                raise DatasetPreflightError(f"managed draft 拒绝 symlink: {relative}")
            if stat.S_ISDIR(expected.st_mode):
                self.counters.directories += 1
                if self.counters.directories > self.limits.max_directories:
                    raise DatasetPreflightError("managed draft 目录数超过安全上限")
                self._walk_directory(directory_fd, name, expected, parts, depth=depth + 1)
            elif stat.S_ISREG(expected.st_mode):
                self._scan_file(directory_fd, name, expected, relative)
            else:
                raise DatasetPreflightError(
                    f"managed draft 拒绝 device/FIFO/socket 等非普通项: {relative}")

    def _walk_directory(self, parent_fd: int, name: str, expected: os.stat_result,
                        parts: Tuple[str, ...], *, depth: int) -> None:
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
            opened = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(opened.st_mode) or not _same_identity(expected, opened)
                    or not _same_identity(expected, current)):
                raise DatasetPreflightError("managed draft 目录在扫描期间身份漂移")
        except DatasetPreflightError:
            if "fd" in locals():
                os.close(fd)
            raise
        except OSError as error:
            raise DatasetPreflightError("managed draft 子目录无法安全打开") from error
        try:
            self._walk(fd, parts, depth=depth)
        finally:
            os.close(fd)

    def _scan_file(self, parent_fd: int, name: str, expected: os.stat_result,
                   relative: str) -> None:
        self.counters.files += 1
        if self.counters.files > self.limits.max_files:
            raise DatasetPreflightError("managed draft 文件数超过安全上限")
        if expected.st_nlink != 1:
            raise DatasetPreflightError(f"managed draft 拒绝 hardlink: {relative}")
        if expected.st_size > self.limits.max_file_bytes:
            raise DatasetPreflightError("managed draft 单文件超过安全上限")
        self.counters.total_bytes += expected.st_size
        if self.counters.total_bytes > self.limits.max_total_bytes:
            raise DatasetPreflightError("managed draft 总字节数超过安全上限")
        item = self.by_path.get(relative)
        if item is None:
            raise DatasetPreflightError(f"managed draft 含 manifest 未登记文件: {relative}")
        if item.size_bytes != expected.st_size:
            raise DatasetPreflightError(f"managed draft 文件大小与 manifest 不符: {relative}")

        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise DatasetPreflightError(f"managed draft 文件无法安全打开: {relative}") from error
        try:
            opened = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or not _same_identity(expected, opened)
                    or not _same_identity(expected, current)):
                raise DatasetPreflightError(
                    f"managed draft 文件在扫描期间身份漂移: {relative}")
            inode = (opened.st_dev, opened.st_ino)
            if inode in self.inodes:
                raise DatasetPreflightError("managed draft 文件 inode 重复/hardlink")
            self.inodes.add(inode)
            evidence = self.evidence[item.bundle_id]
            evidence.observe_name(item.display_relpath, source="display")
            kind = _archive_kind(item.display_relpath)
            if kind is not None:
                self.counters.archives += 1
                if self.counters.archives > self.limits.max_archives:
                    raise DatasetPreflightError("managed draft archive 数超过安全上限")
                if kind == "zip":
                    _scan_zip(fd, opened.st_size, evidence, self.counters, self.limits)
                else:
                    _scan_tar(fd, opened.st_size, evidence, self.counters, self.limits)
            after = os.fstat(fd)
            current_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_identity(opened, after) or not _same_identity(opened, current_after):
                raise DatasetPreflightError(
                    f"managed draft 文件在 metadata 读取期间身份漂移: {relative}")
            self.unseen.remove(relative)
        finally:
            os.close(fd)


def _build_candidates(evidence_by_bundle: Mapping[str, _BundleEvidence],
                      limits: DatasetPreflightLimits) -> Tuple[DatasetCandidate, ...]:
    candidates: List[DatasetCandidate] = []
    threshold = 0.75
    for bundle_id in sorted(evidence_by_bundle):
        evidence = evidence_by_bundle[bundle_id]
        evidence.finish_layout_rules()
        recognized: List[Tuple[str, float]] = []
        for dataset in SUPPORTED_DATASETS:
            base = evidence.scores.get(dataset, 0.0)
            if base:
                bonus = 0.03 * max(0, len(evidence.source_kinds.get(dataset, ())) - 1)
                score = min(0.99, base + bonus)
                if score >= threshold:
                    recognized.append((dataset, round(score, 2)))
        if len(recognized) > 1:
            evidence.warn(
                "同一 bundle 出现多个数据集标识；候选分别列出，须由人工/资格适配器消歧")
        samples = tuple(sorted(set(evidence.display_names))[:limits.max_public_sample_names])
        if len(set(evidence.display_names)) > limits.max_public_sample_names:
            evidence.warn("公开 sample_names 已按上限截断")
        if not recognized:
            candidates.append(DatasetCandidate(
                candidate_id=bundle_id + ":unknown", bundle_id=bundle_id,
                dataset="unknown", confidence=0.0, role_hint="unknown",
                reasons=("未发现足以确认受支持数据集的文件名、相对布局或 archive member 证据",),
                warnings=tuple(evidence.warnings), file_count=len(evidence.file_ids),
                sample_names=samples,
            ))
            continue
        for dataset, confidence in recognized:
            role = "sealed_holdout_candidate" if dataset == "DREAMER" else "explore_candidate"
            candidates.append(DatasetCandidate(
                candidate_id=bundle_id + ":" + dataset.casefold(), bundle_id=bundle_id,
                dataset=dataset, confidence=confidence, role_hint=role,
                reasons=tuple(sorted(evidence.reasons.get(dataset, ()))),
                warnings=tuple(evidence.warnings), file_count=len(evidence.file_ids),
                sample_names=samples,
            ))
    return tuple(sorted(candidates, key=lambda item: item.candidate_id))


def _t1_status(candidates: Sequence[DatasetCandidate]) -> T1RequirementStatus:
    dreamer_count = sum(item.dataset == "DREAMER" for item in candidates)
    explore = tuple(dataset for dataset in T1_EXPLORE_DATASETS
                    if any(item.dataset == dataset for item in candidates))
    holdout_ok = dreamer_count == 1
    explore_ok = len(explore) >= 3
    return T1RequirementStatus(
        sealed_holdout_candidate_count=dreamer_count,
        sealed_holdout_exactly_one=holdout_ok,
        explore_datasets=explore,
        explore_distinct_at_least_three=explore_ok,
        candidate_requirements_met=holdout_ok and explore_ok,
    )


def preflight_managed_datasets(
        managed_draft_files_root: Path,
        manifest: Union[ManagedDraftManifest, Mapping[str, Any]], *,
        limits: Optional[DatasetPreflightLimits] = None) -> DatasetPreflightReport:
    """Recognize dataset *candidates* in one already-managed draft tree.

    The only filesystem argument is the server-side managed root.  Manifest
    storage/display names must be relative and are reconciled against every
    tree entry.  This function performs no write and grants no qualification.
    """
    active_limits = limits if limits is not None else DatasetPreflightLimits()
    if not isinstance(active_limits, DatasetPreflightLimits):
        raise DatasetPreflightError("limits 须为 DatasetPreflightLimits")
    closed_manifest = ManagedDraftManifest.from_value(
        manifest, limits=active_limits)
    counters, evidence = _ManagedTreeScanner(
        managed_draft_files_root, closed_manifest, active_limits).run()
    candidates = _build_candidates(evidence, active_limits)
    return DatasetPreflightReport(
        manifest_sha256=_manifest_hash(closed_manifest),
        candidates=candidates,
        t1=_t1_status(candidates),
        warnings=(_SCIENTIFIC_CAVEAT,),
        file_count=counters.files,
        directory_count=counters.directories,
        total_bytes=counters.total_bytes,
        archive_count=counters.archives,
        archive_member_count=counters.archive_members,
    )


__all__ = [
    "DatasetCandidate", "DatasetPreflightError", "DatasetPreflightLimits",
    "DatasetPreflightReport", "ManagedDraftFile", "ManagedDraftManifest",
    "PREFLIGHT_PROTOCOL", "SUPPORTED_DATASETS", "T1RequirementStatus",
    "T1_EXPLORE_DATASETS", "preflight_managed_datasets",
]
