"""Authority validation, cache verification, and index reuse for repository snapshots."""
from __future__ import annotations

import os
import re
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .artifact_capability import (
    ArtifactCapabilityError, open_directory, read_artifact_bytes, verify_tree_fd,
)
from .repository_materialization_common import (
    _COMMIT_RE, _FULL_NAME_RE, _MAX_ADAPTER_BYTES, _MAX_RECEIPT_BYTES,
    _PROTOCOL, _SHA256_RE,
    RepositoryCacheError, RepositoryMaterializationError, _canonical, _safe_relpath,
    _sha256, _strict_json, _value_hash,
)


class _RepositoryStoreMixin:
    """Host contract: work_root/config hashes/config/owner_guard."""

    def _authority_directories(self) -> tuple[Path, Path, Path, Path]:
        current = self.work_root
        root_info = os.lstat(current)
        if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                or root_info.st_uid != os.geteuid()):
            raise RepositoryCacheError(
                "import materialization work_root authority 非法")
        paths = []
        for component in ("state", "import-materializations", "staging"):
            current = current / component
            if not os.path.lexists(current):
                current.mkdir(mode=0o700)
            info = os.lstat(current)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "import materialization authority directory 非法")
            paths.append(current)
        base = paths[1]
        staging = paths[2]
        children = []
        for component in ("objects", "indexes"):
            child = base / component
            if not os.path.lexists(child):
                child.mkdir(mode=0o700)
            info = os.lstat(child)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "import materialization authority directory 非法")
            children.append(child)
        return base, staging, children[0], children[1]

    def _read_json_file(self, path: Path, *, maximum: int, label: str) -> Any:
        raw = read_artifact_bytes(
            path, max_bytes=maximum, label=label,
            progress_guard=self.owner_guard)
        return _strict_json(raw, label=label)

    def _verify_published_impl(
            self, *, object_path: Path, repository: str,
            revision: str) -> Dict[str, Any]:
        if (not isinstance(object_path.name, str)
                or re.fullmatch(r"[0-9a-f]{64}", object_path.name) is None):
            raise RepositoryMaterializationError(
                "repository snapshot object path 非法")
        object_fd = open_directory(
            object_path, label="published repository object")
        try:
            if set(os.listdir(object_fd)) != {
                    "tree", "ledger.json", "spec.json", "transport.json",
                    "receipt.json"}:
                raise RepositoryMaterializationError(
                    "repository snapshot object 文件闭包漂移")
            anchored = Path(f"/proc/self/fd/{object_fd}")
            receipt_path = anchored / "receipt.json"
            receipt = self._read_json_file(
                receipt_path, maximum=256 * 1024,
                label="repository snapshot receipt")
            expected_receipt_keys = {
                "protocol", "version", "repository", "revision",
                "root_tree_sha1", "object_hash", "config_hash",
                "environment_hash", "file_count", "total_bytes",
                "file_ledger_hash", "spec_hash", "transport_evidence_hash",
            }
            if (not isinstance(receipt, dict)
                    or set(receipt) != expected_receipt_keys
                    or receipt.get("protocol") != _PROTOCOL
                    or receipt.get("version") != 1
                    or receipt.get("repository") != repository
                    or receipt.get("revision") != revision
                    or receipt.get("config_hash") != self.config_hash
                    or receipt.get("environment_hash") != self.environment_hash
                    or receipt.get("object_hash") != "sha256:" + object_path.name
                    or not isinstance(receipt.get("root_tree_sha1"), str)
                    or _COMMIT_RE.fullmatch(receipt["root_tree_sha1"]) is None
                    or any(not isinstance(receipt.get(key), str)
                           or _SHA256_RE.fullmatch(receipt[key]) is None
                           for key in ("file_ledger_hash", "spec_hash",
                                       "transport_evidence_hash"))
                    or isinstance(receipt.get("file_count"), bool)
                    or not isinstance(receipt.get("file_count"), int)
                    or receipt["file_count"] < 1
                    or receipt["file_count"] > int(self.config["max_files"])
                    or isinstance(receipt.get("total_bytes"), bool)
                    or not isinstance(receipt.get("total_bytes"), int)
                    or receipt["total_bytes"] < 0
                    or receipt["total_bytes"] > int(self.config["max_total_bytes"])):
                raise RepositoryMaterializationError(
                    "repository snapshot receipt identity 非法")
            ledger = self._read_json_file(
                anchored / "ledger.json", maximum=_MAX_RECEIPT_BYTES,
                label="repository snapshot ledger")
            spec = self._read_json_file(
                anchored / "spec.json", maximum=16 * 1024 * 1024,
                label="repository snapshot spec")
            transport = self._read_json_file(
                anchored / "transport.json", maximum=4 * 1024 * 1024,
                label="repository transport evidence")
            if (_value_hash(ledger) != receipt["file_ledger_hash"]
                    or _value_hash(spec) != receipt["spec_hash"]
                    or _value_hash(transport) != receipt["transport_evidence_hash"]
                    or not isinstance(ledger, list)
                    or not isinstance(spec, dict)
                    or not isinstance(transport, list)):
                raise RepositoryMaterializationError(
                    "repository snapshot component hash/type 不一致")
            supply_chain = spec.get("supply_chain")
            if (not isinstance(supply_chain, dict)
                    or spec.get("env_hash") != supply_chain.get("environment_hash")):
                raise RepositoryMaterializationError(
                    "repository snapshot execution environment identity 不一致")
            adapter_control = spec.get("adapter_control")
            if (not isinstance(adapter_control, dict)
                    or set(adapter_control) != {
                        "version", "origin", "path", "sha256", "bytes",
                        "value", "generation"}
                    or adapter_control.get("version") != 1
                    or adapter_control.get("origin") not in {
                        "repository", "generated_reviewed"}
                    or not isinstance(adapter_control.get("sha256"), str)
                    or _SHA256_RE.fullmatch(adapter_control["sha256"]) is None
                    or isinstance(adapter_control.get("bytes"), bool)
                    or not isinstance(adapter_control.get("bytes"), int)
                    or not 1 <= adapter_control["bytes"] <= _MAX_ADAPTER_BYTES
                    or supply_chain.get("adapter_origin") != adapter_control["origin"]
                    or supply_chain.get("adapter_control_hash")
                    != _value_hash(adapter_control)
                    or supply_chain.get("harness_adapter_hash")
                    != adapter_control["sha256"]):
                raise RepositoryMaterializationError(
                    "repository snapshot adapter control 闭包非法")
            execution_image = spec.get("execution_image")
            if execution_image is None:
                if (spec.get("env_hash") != self.environment_hash
                        or supply_chain.get("container_digest")
                        != self.sandbox_config["image"]
                        or supply_chain.get("container_image_id")
                        != self.sandbox_config["image_id"]):
                    raise RepositoryMaterializationError(
                        "pinned-image repository snapshot runtime identity 漂移")
            else:
                if self.dependency_image_builder is None:
                    raise RepositoryMaterializationError(
                        "repository snapshot 要求 dependency image builder")
                sandbox = self.dependency_image_builder.resolve(execution_image)
                if (sandbox.environment_hash != spec.get("env_hash")
                        or execution_image.get("environment_hash") != spec.get("env_hash")
                        or execution_image.get("image")
                        != supply_chain.get("container_digest")
                        or execution_image.get("image_id")
                        != supply_chain.get("container_image_id")
                        or execution_image.get("receipt_hash")
                        != supply_chain.get("image_receipt_hash")):
                    raise RepositoryMaterializationError(
                        "dependency-image repository snapshot runtime identity 漂移")
            object_identity = {
                "protocol": _PROTOCOL, "repository": repository,
                "revision": revision,
                "root_tree_sha1": receipt["root_tree_sha1"],
                "file_ledger_hash": receipt["file_ledger_hash"],
                "spec_hash": receipt["spec_hash"],
                "config_hash": self.config_hash,
            }
            if _value_hash(object_identity) != receipt["object_hash"]:
                raise RepositoryMaterializationError(
                    "repository snapshot object hash 重算不一致")
            if (len(ledger) != receipt["file_count"]
                    or sum(item.get("bytes", -1) if isinstance(item, dict) else -1
                           for item in ledger) != receipt["total_bytes"]):
                raise RepositoryMaterializationError(
                    "repository snapshot ledger count/bytes 不一致")
            hashes = {}
            expected_ledger_keys = {
                "path", "sha256", "bytes", "git_blob_sha1", "git_mode",
                "repository", "revision",
            }
            if ledger != sorted(ledger, key=lambda item: item.get("path", "")
                                if isinstance(item, dict) else ""):
                raise RepositoryMaterializationError(
                    "repository snapshot ledger 未 canonical 排序")
            for item in ledger:
                if (not isinstance(item, dict)
                        or set(item) not in (
                            expected_ledger_keys,
                            expected_ledger_keys | {"lfs"})
                        or not isinstance(item.get("path"), str)
                        or _safe_relpath(
                            item["path"], field="published ledger path",
                            max_depth=int(self.config["max_tree_depth"])) != item["path"]
                        or item["path"] in hashes
                        or not isinstance(item.get("sha256"), str)
                        or _SHA256_RE.fullmatch(item["sha256"]) is None
                        or isinstance(item.get("bytes"), bool)
                        or not isinstance(item.get("bytes"), int)
                        or not 0 <= item["bytes"] <= int(self.config["max_file_bytes"])
                        or not isinstance(item.get("git_blob_sha1"), str)
                        or _COMMIT_RE.fullmatch(item["git_blob_sha1"]) is None
                        or item.get("git_mode") not in ("100644", "100755")
                        or not isinstance(item.get("repository"), str)
                        or _FULL_NAME_RE.fullmatch(item["repository"]) is None
                        or not isinstance(item.get("revision"), str)
                        or _COMMIT_RE.fullmatch(item["revision"]) is None):
                    raise RepositoryMaterializationError(
                        "repository snapshot ledger 非法")
                lfs = item.get("lfs")
                if lfs is not None and (
                        not isinstance(lfs, dict)
                        or set(lfs) != {
                            "oid", "size", "pointer_sha256", "pointer_bytes"}
                        or lfs.get("oid") != item["sha256"]
                        or lfs.get("size") != item["bytes"]
                        or not isinstance(lfs.get("pointer_sha256"), str)
                        or _SHA256_RE.fullmatch(lfs["pointer_sha256"]) is None
                        or isinstance(lfs.get("pointer_bytes"), bool)
                        or not isinstance(lfs.get("pointer_bytes"), int)
                        or not 0 < lfs["pointer_bytes"] < 1024):
                    raise RepositoryMaterializationError(
                        "repository snapshot LFS ledger 非法")
                hashes[item["path"]] = item["sha256"]
            if adapter_control["origin"] == "repository":
                path = adapter_control["path"]
                entry = next(
                    (item for item in ledger if item["path"] == path), None)
                if (path != self.config["adapter_path"]
                        or adapter_control["value"] is not None
                        or adapter_control["generation"] is not None
                        or entry is None
                        or entry["sha256"] != adapter_control["sha256"]
                        or entry["bytes"] != adapter_control["bytes"]):
                    raise RepositoryMaterializationError(
                        "repository adapter control 与 Git ledger 不一致")
                expected_adapter_execution_identity = {
                    "origin": "repository",
                    "adapter_sha256": adapter_control["sha256"],
                    "projection_hash": None,
                    "generation_policy_hash": None,
                }
            else:
                value = adapter_control["value"]
                generation = adapter_control["generation"]
                provenance_keys = {
                    "version", "provider", "identity_hash", "projection_hash",
                    "policy_hash", "adapter_sha256", "generation_decision_id",
                    "review_decision_id", "generation_runner_call_id",
                    "review_runner_call_id", "review_hash",
                }
                if (adapter_control["path"] is not None
                        or self.config["adapter_path"] in hashes
                        or not isinstance(value, dict)
                        or not isinstance(generation, dict)
                        or set(generation) != provenance_keys
                        or generation.get("version") != 1
                        or generation.get("provider")
                        != self.config["adapter_generation"]["provider"]
                        or generation.get("policy_hash")
                        != getattr(getattr(self, "adapter_generator", None),
                                   "policy_hash", None)
                        or any(not isinstance(generation.get(key), str)
                               or _SHA256_RE.fullmatch(generation[key]) is None
                               for key in (
                                   "identity_hash", "projection_hash", "policy_hash",
                                   "adapter_sha256", "review_hash"))
                        or any(isinstance(generation.get(key), bool)
                               or not isinstance(generation.get(key), int)
                               or generation[key] <= 0 for key in (
                                   "generation_decision_id", "review_decision_id",
                                   "generation_runner_call_id", "review_runner_call_id"))):
                    raise RepositoryMaterializationError(
                        "generated adapter provenance 闭包非法")
                raw = _canonical(value)
                if (_sha256(raw) != adapter_control["sha256"]
                        or len(raw) != adapter_control["bytes"]
                        or generation["adapter_sha256"] != adapter_control["sha256"]):
                    raise RepositoryMaterializationError(
                        "generated adapter value/hash 不一致")
                expected_adapter_execution_identity = {
                    "origin": "generated_reviewed",
                    "adapter_sha256": adapter_control["sha256"],
                    "projection_hash": generation["projection_hash"],
                    "generation_policy_hash": generation["policy_hash"],
                }
            if supply_chain.get("adapter_execution_identity_hash") != _value_hash(
                    expected_adapter_execution_identity):
                raise RepositoryMaterializationError(
                    "repository adapter execution identity 漂移")
            tree_fd = open_directory(
                anchored / "tree", label="published repository tree")
            try:
                verify_tree_fd(
                    tree_fd, hashes, label="published repository tree", exact=True,
                    progress_guard=self.owner_guard)
                for item in ledger:
                    self.owner_guard()
                    path = Path(f"/proc/self/fd/{tree_fd}") / item["path"]
                    mode_info = os.lstat(path)
                    mode = stat.S_IMODE(mode_info.st_mode)
                    expected_mode = 0o555 if item["git_mode"] == "100755" else 0o444
                    if (not stat.S_ISREG(mode_info.st_mode)
                            or stat.S_ISLNK(mode_info.st_mode)
                            or mode != expected_mode):
                        raise RepositoryMaterializationError(
                            f"published repository mode 不一致: {item['path']}")
            finally:
                os.close(tree_fd)
        finally:
            os.close(object_fd)
        result = dict(spec)
        result["source_tree"] = str(object_path / "tree")
        result["file_ledger"] = ledger
        result["snapshot_receipt"] = str(object_path / "receipt.json")
        result["repository_snapshot_hash"] = receipt["object_hash"]
        return result

    def _verify_published(
            self, *, object_path: Path, repository: str,
            revision: str) -> Dict[str, Any]:
        try:
            return self._verify_published_impl(
                object_path=object_path, repository=repository,
                revision=revision)
        except RepositoryCacheError:
            raise
        except (RepositoryMaterializationError, ArtifactCapabilityError,
                OSError, KeyError, TypeError) as error:
            raise RepositoryCacheError(
                "published repository snapshot tree/hash 完整性核验失败") from error

    def _load_index(self, candidate: Mapping[str, Any], identity_hash: str) -> Optional[Dict[str, Any]]:
        index_path = (
            self.work_root / "state" / "import-materializations" / "indexes"
            / f"{identity_hash}.json")
        if not os.path.lexists(index_path):
            return None
        try:
            index = self._read_json_file(
                index_path, maximum=128 * 1024,
                label="repository materialization index")
        except RepositoryCacheError:
            raise
        except (RepositoryMaterializationError, ArtifactCapabilityError,
                OSError, TypeError) as error:
            # The candidate identity was already validated before consulting
            # this local authority.  A malformed existing index is therefore
            # cache/control-plane corruption, never a durable candidate fault.
            raise RepositoryCacheError(
                "repository materialization index 完整性核验失败") from error
        expected = {
            "version": 1, "candidate_id": candidate["id"],
            "canonical_uri": candidate["canonical_uri"],
            "revision": candidate["revision"],
            "search_snapshot_hash": candidate["search_snapshot_hash"],
            "config_hash": self.config_hash,
            "environment_hash": self.environment_hash,
        }
        if (not isinstance(index, dict)
                or set(index) != set(expected) | {"object_hash"}
                or any(index.get(key) != value for key, value in expected.items())
                or not isinstance(index.get("object_hash"), str)
                or _SHA256_RE.fullmatch(index["object_hash"]) is None):
            raise RepositoryCacheError(
                "repository materialization index identity 漂移")
        object_path = (
            self.work_root / "state" / "import-materializations" / "objects"
            / index["object_hash"].removeprefix("sha256:"))
        return self._verify_published(
            object_path=object_path,
            repository=urllib.parse.urlsplit(candidate["canonical_uri"]).path.strip("/"),
            revision=candidate["revision"])
