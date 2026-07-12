"""Offline closure and replay for DB-registered repository materializations.

The source of reachability is one immutable SQLite backup.  Repository and dependency-image
inspectors validate frozen bytes only; this module never consults current policy, Docker, or the
network.  Restore is a separate opt-in step against an unused SQLite-restored work root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote, urlsplit

from . import storage_governance as sg
from .dependency_image_inspector import inspect_dependency_image_object
from .import_materialization_contract import spec_ref as materialization_spec_ref
from .instance_lease import (
    RESTORE_IN_PROGRESS_NAME,
    InstanceLease,
)
from .repository_materialization_common import (
    RepositoryCacheError,
    RepositoryMaterializationError,
    _remove_private_tree,
    _value_hash,
)
from .repository_materializer_store import (
    inspect_repository_materialization_index,
    inspect_repository_snapshot_object,
)


VERIFY_SCHEMA = "meta-research-import-materialization-verify/v1"
RESTORE_SCHEMA = "meta-research-import-materialization-restore/v1"
_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_BARE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_INDEX_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_INDEX_TEMP_RE = re.compile(
    r"^\.[0-9a-f]{64}\.json\.[1-9][0-9]*\.[0-9a-f]{16}\.tmp$")
_RESTORE_TEMP_RE = re.compile(r"^\.storage-restore-[0-9a-f]{32}$")
_MAX_PLAN_REF_BYTES = 16 * 1024 * 1024
_MAX_INDEXES = 100_000
_CAPACITY_MARGIN_BYTES = 1 * 1024 * 1024
_CAPACITY_MARGIN_INODES = 32
IMPORT_RESTORE_MARKER = b"meta-research-import-materialization-restore/v1\n"


class StorageImportError(sg.StorageGovernanceError):
    """Registered repository/dependency closure is incomplete or corrupt."""


def _regular_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise StorageImportError(f"{label} 缺失: {path}") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise StorageImportError(f"{label} authority 非法: {path}")


def _strict_json_object(
        value: Any, *, label: str, maximum: int) -> Dict[str, Any]:
    if not isinstance(value, str):
        raise StorageImportError(f"{label} 非字符串")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise StorageImportError(f"{label} 非 UTF-8") from error
    if len(raw) > maximum:
        raise StorageImportError(f"{label} 超限")

    def unique(pairs):  # noqa: ANN001
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {token}")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as error:
        raise StorageImportError(f"{label} 非严格 JSON") from error
    if not isinstance(parsed, dict):
        raise StorageImportError(f"{label} 须为 object")
    return parsed


def _strict_plan_ref(value: Any, *, target_id: int) -> Dict[str, Any]:
    return _strict_json_object(
        value, label=f"import target {target_id} plan_ref",
        maximum=_MAX_PLAN_REF_BYTES)


class ImportMaterializationArchive:
    """Offline import CAS operations over a caller-supplied lease-fenced SnapshotArchive."""

    def __init__(self, snapshot_archive):  # noqa: ANN001 - avoid storage_ops import cycle
        self.snapshot_archive = snapshot_archive
        self.work_root = snapshot_archive.work_root
        self.owner_guard = snapshot_archive.owner_guard
        self.repository_root = self.work_root / "state" / "import-materializations"
        self.repository_objects = self.repository_root / "objects"
        self.repository_indexes = self.repository_root / "indexes"
        self.dependency_objects = (
            self.work_root / "state" / "dependency-images" / "objects")

    def _selection(self, cycle: Optional[str | int]) -> Dict[str, Any]:
        chain = self.snapshot_archive._chain(retain=3)
        if cycle is None:
            selected = chain["ordered"][-1]
        elif isinstance(cycle, bool):
            raise StorageImportError("import closure cycle 须为 cN")
        elif isinstance(cycle, int):
            selected = cycle
        elif isinstance(cycle, str) and re.fullmatch(r"c[1-9][0-9]*", cycle):
            selected = int(cycle[1:])
        else:
            raise StorageImportError("import closure cycle 须为 cN")
        if selected < 1:
            raise StorageImportError("import closure cycle 须为 cN")
        if selected not in chain["ordered"]:
            raise StorageImportError(f"snapshot cycle c{selected} 不存在")
        if f"c{selected}" in chain["expired"]:
            raise StorageImportError(
                f"generation_not_retained: c{selected} backup 已退役")
        manifest = chain["manifests"][chain["ordered"].index(selected)]
        backup = self.work_root / manifest["backup"]["path"]
        return {
            "cycle_id": selected,
            "manifest": manifest, "backup": backup,
            "snapshot": {
                "source_cycle": f"c{selected}",
                "source_manifest_sha256": manifest["manifest_sha256"],
                "high_water_cycle": f"c{chain['ordered'][-1]}",
                "high_water_manifest_sha256": chain["manifests"][-1][
                    "manifest_sha256"],
            },
        }

    def _db_roots(self, selection: Mapping[str, Any]) -> Dict[str, Any]:
        backup = selection["backup"]
        expected = selection["manifest"]["backup"]
        fd = os.open(
            backup, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        connection: Optional[sqlite3.Connection] = None
        try:
            info = os.fstat(fd)
            path_info = backup.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)
                    or sg._hash_fd(fd, backup)
                    != (expected["sha256"], expected["bytes"])):
                raise StorageImportError(
                    "import closure query fd 与 manifest backup 漂移")
            connection = sqlite3.connect(
                f"file:{quote(f'/proc/self/fd/{fd}')}?mode=ro&immutable=1",
                uri=True)
            selections = {}
            for row in connection.execute(
                    "SELECT e.id,e.question_id,e.candidate_id,c.canonical_uri,"
                    "c.revision,c.search_snapshot_hash FROM external_import e "
                    "LEFT JOIN external_candidate c ON c.id=e.candidate_id "
                    "WHERE e.action='selected_for_materialization' ORDER BY e.id"):
                selections[row[0]] = row[1:]
            lineage_by_cycle = {}
            for cycle_id, decision_id, payload_raw in connection.execute(
                    "SELECT cycle_id,id,payload_json FROM decision "
                    "WHERE actor='orchestrator' AND type='import_worker_cycle' "
                    "ORDER BY cycle_id,id"):
                payload = _strict_json_object(
                    payload_raw, label=f"import worker decision {decision_id}",
                    maximum=64 * 1024)
                if set(payload) not in (
                        {"external_import_id"},
                        {"external_import_id", "question_id"}):
                    raise StorageImportError(
                        f"import worker decision {decision_id} 字段闭包非法")
                external_import_id = payload.get("external_import_id")
                if (isinstance(external_import_id, bool)
                        or not isinstance(external_import_id, int)
                        or external_import_id < 1):
                    raise StorageImportError(
                        f"import worker decision {decision_id} import identity 非法")
                selected = selections.get(external_import_id)
                if selected is None:
                    raise StorageImportError(
                        f"import worker decision {decision_id} selection 缺失")
                question_id, candidate_id, uri, revision, search_hash = selected
                marker_question = payload.get("question_id", question_id)
                if (isinstance(question_id, bool) or not isinstance(question_id, int)
                        or question_id < 1 or marker_question != question_id
                        or isinstance(candidate_id, bool)
                        or not isinstance(candidate_id, int) or candidate_id < 1
                        or not isinstance(uri, str) or not uri
                        or not isinstance(revision, str)
                        or not isinstance(search_hash, str)
                        or _HASH_RE.fullmatch(search_hash) is None):
                    raise StorageImportError(
                        f"import worker decision {decision_id} candidate lineage 非法")
                lineage = {
                    "question_id": question_id,
                    "candidate_id": candidate_id,
                    "canonical_uri": uri,
                    "revision": revision,
                    "search_snapshot_hash": search_hash,
                }
                previous = lineage_by_cycle.get(cycle_id)
                if previous is not None and previous != lineage:
                    raise StorageImportError(
                        f"import worker cycle c{cycle_id} lineage 冲突")
                lineage_by_cycle[cycle_id] = lineage

            roots: Dict[str, List[Dict[str, Any]]] = {}
            legacy = []
            unbound = []
            for target_id, cycle_id, question_id, status, raw_ref in connection.execute(
                    "SELECT id,cycle_id,question_id,status,plan_ref FROM build_target "
                    "WHERE target_kind='import' ORDER BY id"):
                if (not isinstance(target_id, int) or target_id < 1
                        or not isinstance(cycle_id, int) or cycle_id < 1
                        or not isinstance(status, str)):
                    raise StorageImportError("import target DB 身份非法")
                if raw_ref is None:
                    unbound.append(target_id)
                    continue
                plan_ref = _strict_plan_ref(raw_ref, target_id=target_id)
                contract = plan_ref.get("materialization_contract")
                legacy_keys = {"materialization_contract", "files"}
                repository_keys = {
                    "materialization_contract", "file_ledger_hash",
                    "file_count", "total_bytes", "repository_snapshot_hash",
                }
                if set(plan_ref) == legacy_keys:
                    if (not isinstance(contract, dict)
                            or "repository_snapshot_hash" in contract
                            or not isinstance(plan_ref.get("files"), list)):
                        raise StorageImportError(
                            f"import target {target_id} legacy plan_ref 非法")
                    legacy.append(target_id)
                    continue
                if set(plan_ref) != repository_keys or not isinstance(contract, dict):
                    raise StorageImportError(
                        f"import target {target_id} plan_ref shape 非法")
                repository_hash = plan_ref["repository_snapshot_hash"]
                match = _HASH_RE.fullmatch(repository_hash) if isinstance(
                    repository_hash, str) else None
                if (match is None
                        or contract.get("repository_snapshot_hash")
                        != repository_hash):
                    raise StorageImportError(
                        f"import target {target_id} repository_snapshot_hash 非法")
                lineage = lineage_by_cycle.get(cycle_id)
                if (lineage is None or question_id != lineage["question_id"]):
                    raise StorageImportError(
                        f"import target {target_id} worker/candidate lineage 缺失")
                roots.setdefault(match.group(1), []).append({
                    "target_id": target_id, "plan_ref": plan_ref,
                    "index_identity": {
                        key: lineage[key] for key in (
                            "candidate_id", "canonical_uri", "revision",
                            "search_snapshot_hash")
                    },
                })
        except sqlite3.Error as error:
            raise StorageImportError("import closure SQLite 枚举失败") from error
        finally:
            if connection is not None:
                connection.close()
            os.close(fd)
        return {"roots": roots, "legacy": legacy, "unbound": unbound}

    def _scan_indexes(self, *, required: bool) -> Dict[str, Dict[str, Any]]:
        if not os.path.lexists(self.repository_indexes):
            if required:
                raise StorageImportError("repository materialization indexes 缺失")
            return {}
        _regular_directory(
            self.repository_indexes, label="repository materialization indexes")
        paths = sorted(self.repository_indexes.iterdir(), key=lambda item: item.name)
        if len(paths) > _MAX_INDEXES:
            raise StorageImportError("repository materialization indexes 数量超限")
        result = {}
        for path in paths:
            if _INDEX_TEMP_RE.fullmatch(path.name) is not None:
                info = path.lstat()
                if (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                        and info.st_nlink == 1):
                    continue
            if _INDEX_RE.fullmatch(path.name) is None:
                raise StorageImportError(
                    f"repository indexes 含非法条目: {path.name}")
            try:
                result[path.name] = inspect_repository_materialization_index(
                    path, owner_guard=self.owner_guard)
            except (RepositoryCacheError, RepositoryMaterializationError,
                    OSError, KeyError, TypeError, ValueError) as error:
                raise StorageImportError(
                    f"repository index 核验失败: {path.name}") from error
        return result

    @staticmethod
    def _scan_object_names(root: Path, *, required: bool, label: str) -> Dict[str, Path]:
        if not os.path.lexists(root):
            if required:
                raise StorageImportError(f"{label} 缺失")
            return {}
        _regular_directory(root, label=label)
        result = {}
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            info = path.lstat()
            if (_BARE_HASH_RE.fullmatch(path.name) is None
                    or not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise StorageImportError(f"{label} 含非法条目: {path.name}")
            result[path.name] = path
        return result

    def _closure(self, cycle: Optional[str | int]) -> Dict[str, Any]:
        selection = self._selection(cycle)
        db = self._db_roots(selection)
        root_hashes = set(db["roots"])
        indexes = self._scan_indexes(required=bool(root_hashes))
        repository_paths = self._scan_object_names(
            self.repository_objects, required=bool(root_hashes),
            label="repository materialization objects")
        repository = {}
        dependency_capabilities: Dict[str, Dict[str, Any]] = {}
        for digest in sorted(root_hashes):
            path = repository_paths.get(digest)
            if path is None:
                raise StorageImportError(
                    f"DB-rooted repository object 缺失: {digest}")
            try:
                inspection = inspect_repository_snapshot_object(
                    path, owner_guard=self.owner_guard)
            except (RepositoryCacheError, RepositoryMaterializationError,
                    OSError, KeyError, TypeError, ValueError) as error:
                raise StorageImportError(
                    f"repository object 核验失败: {digest}") from error
            receipt = inspection["receipt"]
            required_indexes = set()
            for target in db["roots"][digest]:
                identity = {
                    **target["index_identity"],
                    "config_hash": receipt["config_hash"],
                    "environment_hash": receipt["environment_hash"],
                }
                name = _value_hash(identity).removeprefix("sha256:") + ".json"
                index = indexes.get(name)
                expected_index = {
                    "version": 1, **identity,
                    "object_hash": receipt["object_hash"],
                }
                if index != expected_index:
                    raise StorageImportError(
                        f"import target {target['target_id']} exact index 缺失/漂移")
                repository_name = urlsplit(
                    index["canonical_uri"]).path.strip("/")
                if (repository_name != receipt["repository"]
                        or index["revision"] != receipt["revision"]
                        or index["config_hash"] != receipt["config_hash"]
                        or index["environment_hash"]
                        != receipt["environment_hash"]):
                    raise StorageImportError(
                        f"repository index/object 身份漂移: {name}")
                target["index"] = name
                required_indexes.add(name)
            expected_ref = materialization_spec_ref(inspection["result"])
            for target in db["roots"][digest]:
                if target["plan_ref"] != expected_ref:
                    raise StorageImportError(
                        f"import target {target['target_id']} plan_ref/object 漂移")
            capability = inspection["spec"].get("execution_image")
            if capability is not None:
                closure = capability["closure_hash"].removeprefix("sha256:")
                previous = dependency_capabilities.get(closure)
                if previous is not None and previous != capability:
                    raise StorageImportError(
                        f"dependency capability closure 冲突: {closure}")
                dependency_capabilities[closure] = dict(capability)
            repository[digest] = {
                "inspection": inspection,
                "indexes": sorted(required_indexes),
                "target_ids": [
                    item["target_id"] for item in db["roots"][digest]],
            }
        dependency_paths = self._scan_object_names(
            self.dependency_objects, required=bool(dependency_capabilities),
            label="dependency image objects")
        dependency = {}
        for digest, capability in sorted(dependency_capabilities.items()):
            path = dependency_paths.get(digest)
            if path is None:
                raise StorageImportError(
                    f"repository-rooted dependency object 缺失: {digest}")
            try:
                receipt, verified = inspect_dependency_image_object(
                    path, expected_capability=capability,
                    owner_guard=self.owner_guard)
            except (RepositoryCacheError, RepositoryMaterializationError,
                    OSError, KeyError, TypeError, ValueError) as error:
                raise StorageImportError(
                    f"dependency object 核验失败: {digest}") from error
            dependency[digest] = {
                "receipt": receipt, "capability": verified,
            }
        for digest, value in repository.items():
            capability = value["inspection"]["spec"].get("execution_image")
            if capability is None:
                continue
            dependency_digest = capability["closure_hash"].removeprefix("sha256:")
            if (value["inspection"]["receipt"]["environment_hash"]
                    != dependency[dependency_digest]["receipt"][
                        "base_environment_hash"]):
                raise StorageImportError(
                    f"repository/dependency base environment 漂移: {digest}")
        required_index_names = sorted(
            name for value in repository.values() for name in value["indexes"])
        report = {
            "schema": VERIFY_SCHEMA,
            "scope": "sqlite_registered_repository_and_dependency_cas",
            **selection["snapshot"],
            "import_targets": (
                sum(len(value) for value in db["roots"].values())
                + len(db["legacy"]) + len(db["unbound"])),
            "repository_import_targets": sum(
                len(value) for value in db["roots"].values()),
            "legacy_import_targets": db["legacy"],
            "unbound_import_targets": db["unbound"],
            "repository_objects": [{
                "object_hash": "sha256:" + digest,
                "target_ids": repository[digest]["target_ids"],
                "indexes": repository[digest]["indexes"],
            } for digest in sorted(repository)],
            "dependency_objects": [
                "sha256:" + digest for digest in sorted(dependency)],
            "orphan_repository_objects": sorted(
                set(repository_paths) - root_hashes),
            "orphan_repository_indexes": sorted(
                set(indexes) - set(required_index_names)),
            "orphan_dependency_objects": sorted(
                set(dependency_paths) - set(dependency)),
        }
        return {
            "selection": selection, "db": db, "indexes": indexes,
            "repository": repository, "dependency": dependency,
            "report": report,
        }

    def verify(self, *, cycle: Optional[str | int] = None) -> Dict[str, Any]:
        result = self._closure(cycle)
        self.owner_guard()
        return result["report"]

    @staticmethod
    def _tree_inventory(path: Path, *, block_size: int) -> tuple[int, int]:
        total_bytes = 0
        total_inodes = 0
        for current, directories, files in os.walk(
                path, topdown=True, followlinks=False):
            current_path = Path(current)
            current_info = current_path.lstat()
            if (not stat.S_ISDIR(current_info.st_mode)
                    or stat.S_ISLNK(current_info.st_mode)
                    or current_info.st_uid != os.geteuid()):
                raise StorageImportError("restore source tree directory 非法")
            total_bytes += block_size
            total_inodes += 1
            for name in directories:
                info = (current_path / name).lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise StorageImportError("restore source tree 含 symlink/非目录")
                # One full target block per directory entry is deliberately
                # conservative for high-fanout trees and avoids pretending
                # that directory metadata is covered by file st_size.
                total_bytes += block_size
            for name in files:
                info = (current_path / name).lstat()
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_nlink != 1 or info.st_uid != os.geteuid()):
                    raise StorageImportError("restore source tree 含非独占常规文件")
                total_bytes += block_size + (
                    (max(info.st_size, 1) + block_size - 1)
                    // block_size * block_size)
                total_inodes += 1
        return total_bytes, total_inodes

    @staticmethod
    def _sync_tree(path: Path, owner_guard: Callable[[], None]) -> None:
        for current, directories, files in os.walk(
                path, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in files:
                owner_guard()
                file_path = current_path / name
                fd = os.open(
                    file_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise StorageImportError("restore staged file 非常规文件")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            for name in directories:
                info = (current_path / name).lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise StorageImportError("restore staged tree 含 symlink")
            owner_guard()
            sg._sync_dir(current_path)

    @staticmethod
    def _ensure_layout(path: Path, owner_guard: Callable[[], None]) -> None:
        owner_guard()
        sg._ensure_dir(path)
        sg._sync_dir(path.parent)

    @staticmethod
    def _discard_restore_temps(path: Path, owner_guard: Callable[[], None]) -> None:
        changed = False
        for item in path.iterdir():
            if _RESTORE_TEMP_RE.fullmatch(item.name) is None:
                continue
            info = item.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()):
                raise StorageImportError("import restore temp authority 非法")
            owner_guard()
            _remove_private_tree(item)
            changed = True
        if changed:
            sg._sync_dir(path)

    def _copy_object(
            self, *, source: Path, destination: Path,
            verifier: Callable[[Path], None], owner_guard: Callable[[], None]) -> None:
        temporary_root = destination.parent / f".storage-restore-{uuid.uuid4().hex}"
        temporary_root.mkdir(mode=0o700)
        staged = temporary_root / destination.name
        try:
            owner_guard()

            def copy_file(left, right):  # noqa: ANN001
                owner_guard()
                return shutil.copy2(left, right, follow_symlinks=False)

            shutil.copytree(
                source, staged, symlinks=True, copy_function=copy_file)
            verifier(staged)
            self._sync_tree(staged, owner_guard)
            owner_guard()
            if os.path.lexists(destination):
                verifier(destination)
            else:
                os.rename(staged, destination)
                sg._sync_dir(destination.parent)
                verifier(destination)
        finally:
            if os.path.lexists(temporary_root):
                _remove_private_tree(temporary_root)

    @staticmethod
    def _restore_marker_present(target: Path) -> bool:
        marker = target / RESTORE_IN_PROGRESS_NAME
        if not os.path.lexists(marker):
            return False
        info = marker.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o400
                or sg._read(marker, maximum=1024)
                != IMPORT_RESTORE_MARKER):
            raise StorageImportError(
                "target restore marker 不是 import continuation")
        return True

    @staticmethod
    def _sync_file(path: Path, owner_guard: Callable[[], None]) -> None:
        owner_guard()
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise StorageImportError("restore durable file authority 非法")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate_restore_target(
            self, target: Path, closure: Mapping[str, Any]) -> Dict[str, Any]:
        receipt_path = target / "restore.json"
        try:
            receipt = sg._parse_json(sg._read(receipt_path), receipt_path)
        except (sg.StorageGovernanceError, OSError) as error:
            raise StorageImportError("target 缺少合法 SQLite restore receipt") from error
        selection = closure["selection"]
        required = {
            "schema", "scope", "continuation_mode", "source_work_root",
            "source_cycle", "source_manifest_sha256", "backup",
        }
        allowed_shapes = (required, required | {"publication_contract"})
        if (set(receipt) not in allowed_shapes
                or receipt.get("schema") != "meta-research-storage-restore/v1"
                or receipt.get("scope") != "sqlite_truth_only"
                or receipt.get("continuation_mode") not in {
                    "legacy_adoption_on_first_start",
                    "import_materialization_restore_required",
                }
                or ("publication_contract" in receipt
                    and receipt["publication_contract"]
                    != "atomic_noreplace_or_lease_fenced_ready")
                or receipt.get("source_work_root") != str(self.work_root)
                or receipt.get("source_cycle")
                != selection["snapshot"]["source_cycle"]
                or receipt.get("source_manifest_sha256")
                != selection["manifest"]["manifest_sha256"]
                or receipt.get("backup") != selection["manifest"]["backup"]):
            raise StorageImportError("target SQLite restore receipt 与 source snapshot 漂移")
        manifest = selection["manifest"]
        self.snapshot_archive.publisher._verify_backup_object(
            target / "research.sqlite",
            expected_hash=manifest["backup"]["sha256"],
            expected_bytes=manifest["backup"]["bytes"],
            cycle_id=selection["cycle_id"],
            cycle_status=manifest["cycle_status"],
            allow_later_cycles=manifest["adoption_baseline"] is True)
        return receipt

    def restore(
            self, *, target: Path | str,
            cycle: Optional[str | int] = None) -> Dict[str, Any]:
        target_path = Path(os.path.abspath(os.fspath(target)))
        _regular_directory(target_path, label="import restore target")
        try:
            resolved_target = target_path.resolve(strict=True)
            source_path = self.work_root.resolve(strict=True)
        except OSError as error:
            raise StorageImportError("import restore target/source 不可解析") from error
        if resolved_target != target_path:
            raise StorageImportError("import restore target 路径含 symlink/alias")
        target_path = resolved_target
        if (target_path == source_path or target_path in source_path.parents
                or source_path in target_path.parents):
            raise StorageImportError("import restore target/source 不得嵌套")
        resume_marker = self._restore_marker_present(target_path)
        target_lease = InstanceLease.acquire(
            target_path,
            expected_restore_marker=(
                IMPORT_RESTORE_MARKER if resume_marker else None))
        primary: Optional[BaseException] = None
        try:
            target_guard = target_lease.assert_owned
            target_guard()
            sg._sync_dir(target_path.parent)
            closure = self._closure(cycle)
            self._validate_restore_target(target_path, closure)
            marker_path = target_path / RESTORE_IN_PROGRESS_NAME
            sg._publish_once(marker_path, IMPORT_RESTORE_MARKER)
            sg._sync_dir(target_path)
            target_guard()
            repository_base = target_path / "state" / "import-materializations"
            repository_objects = repository_base / "objects"
            repository_indexes = repository_base / "indexes"
            dependency_objects = target_path / "state" / "dependency-images" / "objects"
            for path in (
                    target_path / "state", repository_base,
                    repository_objects, repository_indexes):
                self._ensure_layout(path, target_guard)
            if closure["dependency"]:
                for path in (
                        target_path / "state" / "dependency-images",
                        dependency_objects):
                    self._ensure_layout(path, target_guard)
            self._discard_restore_temps(repository_objects, target_guard)
            if closure["dependency"]:
                self._discard_restore_temps(dependency_objects, target_guard)

            required_bytes = 0
            required_inodes = _CAPACITY_MARGIN_INODES
            published_repository = []
            reused_repository = []
            published_dependency = []
            reused_dependency = []
            published_indexes = []
            reused_indexes = []
            fs = os.statvfs(target_path)
            block_size = max(int(fs.f_frsize), int(fs.f_bsize), 4096)
            for digest in closure["dependency"]:
                destination = dependency_objects / digest
                if os.path.lexists(destination):
                    inspect_dependency_image_object(
                        destination,
                        expected_capability=closure["dependency"][digest]["capability"],
                        owner_guard=target_guard)
                    reused_dependency.append(digest)
                else:
                    size, count = self._tree_inventory(
                        self.dependency_objects / digest,
                        block_size=block_size)
                    required_bytes += size + block_size
                    required_inodes += count + 1
                    published_dependency.append(digest)
            for digest, value in closure["repository"].items():
                destination = repository_objects / digest
                if os.path.lexists(destination):
                    inspection = inspect_repository_snapshot_object(
                        destination, owner_guard=target_guard)
                    if (inspection["receipt"] != value["inspection"]["receipt"]
                            or inspection["ledger"] != value["inspection"]["ledger"]
                            or inspection["spec"] != value["inspection"]["spec"]
                            or inspection["transport"]
                            != value["inspection"]["transport"]):
                        raise StorageImportError(
                            f"target repository object 漂移: {digest}")
                    reused_repository.append(digest)
                else:
                    size, count = self._tree_inventory(
                        self.repository_objects / digest,
                        block_size=block_size)
                    required_bytes += size + block_size
                    required_inodes += count + 1
                    published_repository.append(digest)
                for name in value["indexes"]:
                    destination_index = repository_indexes / name
                    raw = sg._canonical(closure["indexes"][name])
                    if os.path.lexists(destination_index):
                        if sg._read(destination_index) != raw:
                            raise StorageImportError(
                                f"target repository index 漂移: {name}")
                        inspect_repository_materialization_index(
                            destination_index, owner_guard=target_guard)
                        reused_indexes.append(name)
                    else:
                        required_bytes += (
                            (max(len(raw), 1) + block_size - 1)
                            // block_size * block_size)
                        required_inodes += 1
                        published_indexes.append(name)
            needed = required_bytes + _CAPACITY_MARGIN_BYTES
            if (int(fs.f_bavail) * int(fs.f_frsize) < needed
                    or int(fs.f_favail) < required_inodes):
                raise StorageImportError(
                    "import restore 容量门拒绝: "
                    f"required_bytes={needed} required_inodes={required_inodes}")

            combined_guard = lambda: (self.owner_guard(), target_guard())
            for digest in reused_dependency:
                self._sync_tree(dependency_objects / digest, target_guard)
            if reused_dependency:
                sg._sync_dir(dependency_objects)
            for digest in reused_repository:
                self._sync_tree(repository_objects / digest, target_guard)
            if reused_repository:
                sg._sync_dir(repository_objects)
            for name in reused_indexes:
                self._sync_file(repository_indexes / name, target_guard)
            if reused_indexes:
                sg._sync_dir(repository_indexes)
            for digest in published_dependency:
                capability = closure["dependency"][digest]["capability"]

                def verify_dependency(path, expected=capability):  # noqa: ANN001
                    inspect_dependency_image_object(
                        path, expected_capability=expected,
                        owner_guard=combined_guard)

                self._copy_object(
                    source=self.dependency_objects / digest,
                    destination=dependency_objects / digest,
                    verifier=verify_dependency, owner_guard=combined_guard)
            for digest in published_repository:
                expected = closure["repository"][digest]["inspection"]

                def verify_repository(path, expected_value=expected):  # noqa: ANN001
                    inspection = inspect_repository_snapshot_object(
                        path, owner_guard=combined_guard)
                    if ({key: inspection[key] for key in (
                            "receipt", "ledger", "spec", "transport")}
                            != {key: expected_value[key] for key in (
                                "receipt", "ledger", "spec", "transport")}):
                        raise StorageImportError(
                            f"copied repository object 漂移: {path.name}")

                self._copy_object(
                    source=self.repository_objects / digest,
                    destination=repository_objects / digest,
                    verifier=verify_repository, owner_guard=combined_guard)
            for name in published_indexes:
                self.owner_guard()
                target_guard()
                sg._publish_once(
                    repository_indexes / name,
                    sg._canonical(closure["indexes"][name]))
                inspect_repository_materialization_index(
                    repository_indexes / name, owner_guard=target_guard)
            receipt = {
                "schema": RESTORE_SCHEMA,
                "scope": "repository_and_dependency_cas_only",
                "source_work_root": str(self.work_root),
                "source_cycle": closure["selection"]["snapshot"]["source_cycle"],
                "source_manifest_sha256": closure["selection"]["manifest"][
                    "manifest_sha256"],
                "repository_objects": [
                    "sha256:" + value for value in sorted(closure["repository"])],
                "repository_indexes": [{
                    "name": name,
                    "sha256": "sha256:" + sg._hash_bytes(
                        sg._canonical(closure["indexes"][name])),
                } for name in sorted({
                    name for value in closure["repository"].values()
                    for name in value["indexes"]})],
                "dependency_objects": [
                    "sha256:" + value for value in sorted(closure["dependency"])],
            }
            completion_path = repository_base / "storage-restore.json"
            sg._publish_once(completion_path, sg._canonical(receipt))
            self._sync_file(completion_path, target_guard)
            sg._sync_dir(repository_base)
            target_guard()
            marker_info = marker_path.lstat()
            if (not stat.S_ISREG(marker_info.st_mode)
                    or stat.S_ISLNK(marker_info.st_mode)
                    or marker_info.st_uid != os.geteuid()
                    or marker_info.st_nlink != 1
                    or stat.S_IMODE(marker_info.st_mode) != 0o400
                    or sg._read(marker_path, maximum=1024)
                    != IMPORT_RESTORE_MARKER):
                raise StorageImportError("import restore completion marker 漂移")
            marker_path.unlink()
            sg._sync_dir(target_path)
            target_guard()
            return {
                **receipt,
                "published_repository_objects": [
                    "sha256:" + value for value in published_repository],
                "reused_repository_objects": [
                    "sha256:" + value for value in reused_repository],
                "published_dependency_objects": [
                    "sha256:" + value for value in published_dependency],
                "reused_dependency_objects": [
                    "sha256:" + value for value in reused_dependency],
                "published_indexes": published_indexes,
                "reused_indexes": reused_indexes,
            }
        except BaseException as error:
            primary = error
            raise
        finally:
            close_error = target_lease.close()
            if close_error is not None:
                if primary is None:
                    raise close_error
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(f"import restore target lease close 失败: {close_error}")
