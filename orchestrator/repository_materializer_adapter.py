"""Declarative repository adapter validation and deterministic protocol compilation."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Sequence

from .artifact_capability import read_artifact_bytes
from .repository_materialization_common import (
    _ADAPTER_VERSIONS, _ARTIFACT_TYPES, _CONTROL_ENV_KEYS, _LOG_KEY_RE,
    _MAX_ADAPTER_BYTES, RepositoryMaterializationError, _bounded_string,
    _canonical, _positive_int, _safe_relpath, _sha256, _stable_id, _value_hash,
    _strict_json,
)


class _RepositoryAdapterMixin:
    """Host contract: config, sandbox_config, environment_hash, owner_guard."""

    def _argv(self, value: Any, *, field: str) -> list[str]:
        if not isinstance(value, list) or not value or len(value) > 128:
            raise RepositoryMaterializationError(f"adapter.{field} argv 非法")
        result = []
        for index, raw in enumerate(value):
            arg = _bounded_string(
                raw, field=f"adapter.{field}[{index}]", max_bytes=4096)
            if any(marker in arg for marker in ("{src}", "{ckpt}")):
                raise RepositoryMaterializationError(
                    f"adapter.{field} 只接受 {{repo}}/{{artifact}} 占位符")
            unknown = re.findall(r"\{[^{}]+\}", arg)
            if any(item not in ("{repo}", "{artifact}") for item in unknown):
                raise RepositoryMaterializationError(
                    f"adapter.{field} 含未知占位符")
            result.append(arg)
        if PurePosixPath(result[0]).name in {
                "bash", "sh", "dash", "zsh", "env", "sudo", "docker", "git", "curl", "wget"}:
            raise RepositoryMaterializationError(
                f"adapter.{field} program 不得是 shell/network/host launcher")
        return result

    def _adapter_spec(
            self, *, tree_root: Path, ledger: Sequence[Mapping[str, Any]],
            repository: str, revision: str, root_tree_sha: str,
            sources: Sequence[Mapping[str, Any]],
            submodules: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        adapter_path = self.config["adapter_path"]
        adapter_file = tree_root / adapter_path
        ledger_by_path = {item["path"]: item for item in ledger}
        if adapter_path not in ledger_by_path:
            raise RepositoryMaterializationError(
                f"repository 缺 production adapter {adapter_path}")
        raw = read_artifact_bytes(
            adapter_file, expected_hash=ledger_by_path[adapter_path]["sha256"],
            expected_size=ledger_by_path[adapter_path]["bytes"],
            max_bytes=_MAX_ADAPTER_BYTES, label="repository import adapter",
            progress_guard=self.owner_guard)
        value = _strict_json(raw, label="repository import adapter")
        required_keys = {
            "version", "artifact_relpath", "artifact_type", "smoke_argv",
            "eval_argv", "dependency_mode", "dependency_locks",
            "factory_protocol",
        }
        if (not isinstance(value, dict) or set(value) != required_keys
                or value.get("version") not in _ADAPTER_VERSIONS):
            raise RepositoryMaterializationError(
                "repository adapter v2/v3 字段闭包/version 非法")
        adapter_version = value["version"]
        artifact = _safe_relpath(
            value["artifact_relpath"], field="adapter.artifact_relpath",
            max_depth=int(self.config["max_tree_depth"]))
        if artifact not in ledger_by_path:
            raise RepositoryMaterializationError(
                "adapter artifact_relpath 不在 repository snapshot")
        artifact_type = value["artifact_type"]
        if artifact_type not in _ARTIFACT_TYPES:
            raise RepositoryMaterializationError("adapter artifact_type 非法")
        dependency_mode = value["dependency_mode"]
        locks = value["dependency_locks"]
        if not isinstance(locks, list):
            raise RepositoryMaterializationError(
                "adapter dependency_locks 须为数组")
        if (adapter_version == 2 and dependency_mode != "pinned_image_only"):
            raise RepositoryMaterializationError(
                "adapter v2 dependency contract 只允许 pinned_image_only")
        if (adapter_version == 3 and dependency_mode != "python_wheel_image_v1"):
            raise RepositoryMaterializationError(
                "adapter v3 dependency contract 只允许 python_wheel_image_v1")
        parsed_locks = []
        for index, path_value in enumerate(locks):
            path = _safe_relpath(
                path_value, field=f"adapter.dependency_locks[{index}]",
                max_depth=int(self.config["max_tree_depth"]))
            if (path not in ledger_by_path
                    or PurePosixPath(path).name not in self.config["dependency_lock_names"]):
                raise RepositoryMaterializationError(
                    f"adapter dependency lock 未在允许清单/快照: {path}")
            parsed_locks.append({
                "path": path, "sha256": ledger_by_path[path]["sha256"],
                "bytes": ledger_by_path[path]["bytes"],
            })
        if len({item["path"] for item in parsed_locks}) != len(parsed_locks):
            raise RepositoryMaterializationError("adapter dependency lock 重复")
        if adapter_version == 2 and parsed_locks:
            raise RepositoryMaterializationError(
                "adapter v2 只允许 pinned_image_only 且 dependency_locks 为空；"
                "未验证安装的 lock 不得冒充可复现环境")
        if adapter_version == 3 and (
                len(parsed_locks) != 1
                or PurePosixPath(parsed_locks[0]["path"]).name
                != self.config["dependency_image"]["lock_basename"]):
            raise RepositoryMaterializationError(
                "adapter v3 要求唯一 python-wheel-lock.json")
        protocol = value["factory_protocol"]
        if (not isinstance(protocol, dict) or set(protocol) != {
                "name", "version", "scope_spec", "metrics", "required"}):
            raise RepositoryMaterializationError("adapter factory_protocol 字段闭包非法")
        protocol_name = _bounded_string(
            protocol["name"], field="factory_protocol.name", max_bytes=512)
        protocol_version = _positive_int(
            protocol["version"], field="factory_protocol.version", maximum=1_000_000)
        scope = protocol["scope_spec"]
        if not isinstance(scope, dict) or not scope:
            raise RepositoryMaterializationError(
                "factory_protocol.scope_spec 须为非空 object")
        try:
            scope_json = _canonical(scope).decode("utf-8").rstrip("\n")
        except (TypeError, ValueError, UnicodeEncodeError,
                RecursionError) as error:
            raise RepositoryMaterializationError(
                "factory_protocol.scope_spec 非有限 JSON") from error
        metrics = protocol["metrics"]
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 256:
            raise RepositoryMaterializationError("factory_protocol.metrics 数量非法")
        metric_defs = []
        log_map = {}
        semantic_metrics = []
        metric_pairs = set()
        metric_family_names: Dict[int, str] = {}
        for index, metric in enumerate(metrics):
            keys = {
                "log_key", "name", "version", "direction", "unit",
                "compute_spec", "readout_rule",
            }
            if not isinstance(metric, dict) or set(metric) != keys:
                raise RepositoryMaterializationError(
                    f"factory_protocol.metrics[{index}] 字段闭包非法")
            log_key = metric["log_key"]
            if not isinstance(log_key, str) or _LOG_KEY_RE.fullmatch(log_key) is None:
                raise RepositoryMaterializationError("factory metric log_key 非法")
            if log_key in log_map:
                raise RepositoryMaterializationError("factory metric log_key 重复")
            descriptor = {
                "name": _bounded_string(
                    metric["name"], field="factory metric.name", max_bytes=512),
                "version": _positive_int(
                    metric["version"], field="factory metric.version", maximum=1_000_000),
                "direction": metric["direction"],
                "unit": metric["unit"], "compute_spec": metric["compute_spec"],
                "readout_rule": metric["readout_rule"],
            }
            if descriptor["direction"] not in ("higher", "lower"):
                raise RepositoryMaterializationError("factory metric.direction 非法")
            if descriptor["unit"] is not None:
                descriptor["unit"] = _bounded_string(
                    descriptor["unit"], field="factory metric.unit",
                    max_bytes=8192)
            for field in ("compute_spec", "readout_rule"):
                descriptor[field] = _bounded_string(
                    descriptor[field], field=f"factory metric.{field}",
                    max_bytes=8192)
            metric_id = _stable_id("metric-family", {
                "repository": repository.lower(),
                "protocol_name": protocol_name,
                "metric_name": descriptor["name"],
            })
            prior_family_name = metric_family_names.setdefault(
                metric_id, descriptor["name"])
            if prior_family_name != descriptor["name"]:
                raise RepositoryMaterializationError(
                    "factory protocol stable metric family hash collision")
            metric_pair = (metric_id, descriptor["version"])
            if metric_pair in metric_pairs:
                raise RepositoryMaterializationError(
                    "factory protocol 含重复 metric family/version")
            metric_pairs.add(metric_pair)
            metric_def = {"id": metric_id, **descriptor, "log_key": log_key}
            metric_defs.append(metric_def)
            log_map[log_key] = [metric_id, descriptor["version"]]
            semantic_metrics.append(descriptor)
        required_log_keys = protocol["required"]
        if (not isinstance(required_log_keys, list) or not required_log_keys
                or any(not isinstance(key, str) or key not in log_map
                       for key in required_log_keys)
                or len(set(required_log_keys)) != len(required_log_keys)):
            raise RepositoryMaterializationError("factory_protocol.required 非法")
        metric_defs.sort(key=lambda item: (item["id"], item["version"]))
        semantic_metrics.sort(
            key=lambda item: (item["name"], item["version"], _value_hash(item)))
        protocol_identity = {
            "name": protocol_name, "version": protocol_version,
            "scope_spec": scope, "metrics": semantic_metrics,
        }
        protocol_id = _stable_id("protocol-family", {
            "repository": repository.lower(), "name": protocol_name,
        })
        required = sorted(log_map[key] for key in required_log_keys)
        adapter_hash = _sha256(raw)
        smoke_cmd = self._argv(value["smoke_argv"], field="smoke_argv")
        eval_cmd = self._argv(value["eval_argv"], field="eval_argv")
        allowed_programs = {
            "python", "python3", self.sandbox_config["python_path"],
        }
        if smoke_cmd[0] not in allowed_programs or eval_cmd[0] not in allowed_programs:
            raise RepositoryMaterializationError(
                "repository adapter 当前只允许 pinned Python 作为直接 launcher")
        execution_image = None
        dependency_wheels = []
        dependency_wheel_manifest_hash = _value_hash([])
        image_receipt_hash = None
        image_archive_sha256 = None
        build_context_hash = _value_hash([])
        environment_hash = self.environment_hash
        container_digest = self.sandbox_config["image"]
        container_image_id = self.sandbox_config["image_id"]
        dependency_lock_hash = _value_hash(parsed_locks)
        if adapter_version == 3:
            if self.dependency_image_builder is None:
                raise RepositoryMaterializationError(
                    "adapter v3 dependency image builder 未配置，拒绝 host install/fallback")
            image_result = self.dependency_image_builder.build(
                tree_root=tree_root, lock_entry=parsed_locks[0],
                repository=repository, revision=revision)
            capability_keys = {
                "version", "provider", "closure_hash", "receipt_hash",
                "environment_hash", "image", "image_id",
            }
            if (not isinstance(image_result, Mapping)
                    or not capability_keys.issubset(image_result)):
                raise RepositoryMaterializationError(
                    "dependency image builder 未返回完整 capability")
            execution_image = {
                key: image_result[key] for key in capability_keys}
            environment_hash = image_result["environment_hash"]
            container_digest = image_result["image"]
            container_image_id = image_result["image_id"]
            dependency_lock_hash = image_result["lock_canonical_hash"]
            dependency_wheels = list(image_result["wheels"])
            dependency_wheel_manifest_hash = image_result["wheel_manifest_hash"]
            image_receipt_hash = image_result["receipt_hash"]
            image_archive_sha256 = image_result["image_archive_sha256"]
            build_context_hash = image_result["build_context_hash"]
        source_identity = [{
            "repository": item["repository"], "revision": item["revision"],
            "root_tree_sha1": item["root_tree_sha1"],
            "archive_url": item["archive_url"],
            "file_ledger_hash": item["file_ledger_hash"],
            "license": item["license"],
        } for item in sources]
        lfs_objects = []
        for item in ledger:
            lfs = item.get("lfs")
            if lfs is None:
                continue
            lfs_objects.append({
                "path": item["path"], "repository": item["repository"],
                "revision": item["revision"], "oid": lfs["oid"],
                "size": lfs["size"],
                "pointer_sha256": lfs["pointer_sha256"],
                "pointer_bytes": lfs["pointer_bytes"],
                "pointer_git_blob_sha1": item["git_blob_sha1"],
            })
        lfs_objects.sort(key=lambda item: item["path"])
        supply_chain = {
            "revision": revision, "root_tree_sha1": root_tree_sha,
            "submodules": list(sorted(
                submodules, key=lambda item: item["path"])),
            "patch_set_hash": _value_hash([]), "patch_apply_order": [],
            "lfs_objects": lfs_objects, "dependency_mode": dependency_mode,
            "dependency_locks": parsed_locks,
            "dependency_lock_hash": dependency_lock_hash,
            "harness_adapter_hash": adapter_hash,
            "environment_hash": environment_hash,
            "network_isolation": True,
            "artifact_download_sources": source_identity,
            "dependency_artifacts": dependency_wheels,
            "dependency_artifact_manifest_hash": dependency_wheel_manifest_hash,
            "system_package_sources": [],
            "base_container_digest": self.sandbox_config["image"],
            "base_container_image_id": self.sandbox_config["image_id"],
            "container_digest": container_digest,
            "container_image_id": container_image_id,
            "image_receipt_hash": image_receipt_hash,
            "image_archive_sha256": image_archive_sha256,
            "compiler": dict(self.config["compiler"]),
            "generated_files_hash": build_context_hash,
            "environment_allowlist": sorted({
                *_CONTROL_ENV_KEYS,
                *(["PYTHONPATH"] if adapter_version == 3 else []),
            }),
            "commands": {
                "smoke": smoke_cmd, "eval": eval_cmd},
        }
        target_identity = {
            "repository": repository, "revision": revision,
            "root_tree_sha1": root_tree_sha, "adapter_sha256": adapter_hash,
            "protocol_id": protocol_id, "protocol_version": protocol_version,
            "protocol_semantics_hash": _value_hash(protocol_identity),
            "environment_hash": environment_hash,
            "image_receipt_hash": image_receipt_hash,
            "required": required,
        }
        result = {
            "smoke_cmd": smoke_cmd,
            "eval_cmd": eval_cmd,
            "protocol_id": protocol_id, "protocol_ver": protocol_version,
            "factory_protocol": {
                "id": protocol_id, "version": protocol_version,
                "name": protocol_name, "scope_spec_json": scope_json,
                "metric_defs": metric_defs,
                "metrics": [[item["id"], item["version"]]
                            for item in metric_defs],
            },
            "metric_log_map": log_map,
            "eval_key": "import-" + _value_hash(target_identity).removeprefix("sha256:")[:32],
            "target_set_hash": _value_hash(target_identity),
            "required": required, "artifact_relpath": artifact,
            "artifact_type": artifact_type, "env_hash": environment_hash,
            "supply_chain": supply_chain, "requires_adversarial_sandbox": True,
        }
        if execution_image is not None:
            result["execution_image"] = execution_image
        return result
