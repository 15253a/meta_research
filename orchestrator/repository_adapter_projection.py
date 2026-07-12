"""Bounded, deterministic source projection for reviewed adapter generation.

Repository bytes are untrusted data.  The generator never receives a host path
or shell capability: this module selects a policy-bounded UTF-8 projection from
the already verified Git ledger and binds every preview to its exact hash/size.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Sequence

from .artifact_capability import read_artifact_bytes
from .repository_materialization_common import (
    RepositoryMaterializationError,
    _canonical,
    _value_hash,
)


_PROVIDER = "codex-reviewed-sidecar-v1"
_CONFIG_KEYS = {
    "provider", "prompt_version", "max_inventory_paths",
    "max_inventory_bytes", "max_preview_files", "max_preview_file_bytes",
    "max_preview_total_bytes", "max_projection_bytes",
}
_TEXT_BASENAMES = {
    "readme", "readme.md", "readme.rst", "readme.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
    "requirements.txt", "requirements.lock", "environment.yml",
    "config.json", "config.yaml", "config.yml", "model-index.yml",
}
_TEXT_SUFFIXES = {
    ".py", ".md", ".rst", ".txt", ".toml", ".ini", ".cfg",
    ".yaml", ".yml", ".json", ".jsonl", ".sh",
}
_ARTIFACT_SUFFIXES = {
    ".bin", ".ckpt", ".model", ".onnx", ".pt", ".pth",
    ".safetensors", ".tflite",
}
_ENTRYPOINT_WORDS = (
    "benchmark", "demo", "eval", "evaluate", "inference", "main",
    "predict", "smoke", "train", "validate",
)


def validate_adapter_generation_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    value = dict(config) if isinstance(config, Mapping) else {}
    if set(value) != _CONFIG_KEYS or value.get("provider") != _PROVIDER:
        raise ValueError("adapter_generation 字段闭包/provider 非法")
    if value.get("prompt_version") != 1:
        raise ValueError("adapter_generation.prompt_version 非冻结 v1")
    bounds = {
        "max_inventory_paths": (1, 10000),
        "max_inventory_bytes": (1024, 4 * 1024 * 1024),
        "max_preview_files": (1, 256),
        "max_preview_file_bytes": (1, 1024 * 1024),
        "max_preview_total_bytes": (1, 8 * 1024 * 1024),
        "max_projection_bytes": (4096, 16 * 1024 * 1024),
    }
    for key, (minimum, maximum) in bounds.items():
        item = value.get(key)
        if (isinstance(item, bool) or not isinstance(item, int)
                or not minimum <= item <= maximum):
            raise ValueError(f"adapter_generation.{key} 越界")
    if (value["max_preview_total_bytes"]
            > value["max_preview_files"] * value["max_preview_file_bytes"]):
        raise ValueError("adapter_generation preview 总量与单项上限矛盾")
    if value["max_projection_bytes"] < value["max_preview_total_bytes"]:
        raise ValueError("adapter_generation projection 上限小于 preview 上限")
    return value


def _priority(path: str) -> tuple[int, int, str]:
    lower = path.lower()
    basename = PurePosixPath(lower).name
    suffix = PurePosixPath(lower).suffix
    if basename in _TEXT_BASENAMES or basename.startswith("readme"):
        rank = 0
    elif basename in {"python-wheel-lock.json", "cargo.lock", "package-lock.json"}:
        rank = 1
    elif any(word in basename for word in _ENTRYPOINT_WORDS):
        rank = 2
    elif suffix in _ARTIFACT_SUFFIXES:
        rank = 3
    elif suffix in _TEXT_SUFFIXES:
        rank = 4
    else:
        rank = 5
    return rank, len(PurePosixPath(path).parts), path


def _inventory_entry(item: Mapping[str, Any]) -> Dict[str, Any]:
    result = {
        "path": item["path"], "sha256": item["sha256"],
        "bytes": item["bytes"], "git_mode": item["git_mode"],
        "repository": item["repository"], "revision": item["revision"],
    }
    if "lfs" in item:
        result["lfs"] = {
            "oid": item["lfs"]["oid"], "size": item["lfs"]["size"]}
    return result


def build_adapter_source_projection(
        *, tree_root: Path, ledger: Sequence[Mapping[str, Any]],
        repository: str, revision: str, root_tree_sha: str,
        adapter_path: str, dependency_lock_names: Sequence[str],
        dependency_lock_basename: str, config: Mapping[str, Any],
        owner_guard: Callable[[], None]) -> Dict[str, Any]:
    cfg = validate_adapter_generation_config(config)
    if not isinstance(ledger, Sequence) or not ledger:
        raise RepositoryMaterializationError("adapter generation 缺 repository ledger")
    if any(not isinstance(item, Mapping) for item in ledger):
        raise RepositoryMaterializationError("adapter generation ledger 项非法")
    if any(item.get("path") == adapter_path for item in ledger):
        raise RepositoryMaterializationError(
            "adapter generation 只允许处理缺 adapter 的 snapshot")

    known_lock_names = set(dependency_lock_names)
    lock_paths = sorted(
        item["path"] for item in ledger
        if PurePosixPath(item["path"]).name in known_lock_names)
    supported = [
        path for path in lock_paths
        if PurePosixPath(path).name == dependency_lock_basename]
    unsupported = sorted(set(lock_paths) - set(supported))
    if len(supported) > 1:
        raise RepositoryMaterializationError(
            "adapter generation 发现多份 python wheel lock，无法唯一选择")
    dependency = ({
        "adapter_version": 3,
        "dependency_mode": "python_wheel_image_v1",
        "dependency_locks": supported,
    } if supported else {
        "adapter_version": 2,
        "dependency_mode": "pinned_image_only",
        "dependency_locks": [],
    })

    ordered = sorted(ledger, key=lambda item: _priority(item["path"]))
    inventory = []
    inventory_bytes = 2
    for item in ordered:
        if len(inventory) >= cfg["max_inventory_paths"]:
            break
        entry = _inventory_entry(item)
        encoded = _canonical(entry)
        if inventory_bytes + len(encoded) > cfg["max_inventory_bytes"]:
            continue
        inventory.append(entry)
        inventory_bytes += len(encoded)

    previews = []
    preview_total = 0
    for item in ordered:
        if len(previews) >= cfg["max_preview_files"]:
            break
        path = item["path"]
        basename = PurePosixPath(path.lower()).name
        suffix = PurePosixPath(path.lower()).suffix
        if (basename not in _TEXT_BASENAMES
                and not basename.startswith("readme")
                and suffix not in _TEXT_SUFFIXES):
            continue
        size = item["bytes"]
        if (size > cfg["max_preview_file_bytes"]
                or preview_total + size > cfg["max_preview_total_bytes"]):
            continue
        owner_guard()
        raw = read_artifact_bytes(
            tree_root / path, expected_hash=item["sha256"],
            expected_size=size, max_bytes=cfg["max_preview_file_bytes"],
            label=f"adapter generation preview:{path}",
            progress_guard=owner_guard)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        previews.append({
            "path": path, "sha256": item["sha256"],
            "bytes": size, "text": text,
        })
        preview_total += size

    projection = {
        "version": 1, "provider": _PROVIDER,
        "repository": repository, "revision": revision,
        "root_tree_sha1": root_tree_sha,
        "file_count": len(ledger),
        "total_bytes": sum(item["bytes"] for item in ledger),
        "file_ledger_hash": _value_hash(list(ledger)),
        "adapter_path": adapter_path,
        "dependency_contract": dependency,
        # These files are evidence only.  The generated adapter cannot request
        # installation from them; a repository that actually needs them must
        # return a bounded generation failure or fail the later sandbox smoke.
        "unavailable_dependency_locks": unsupported,
        "inventory": inventory,
        "inventory_truncated": len(inventory) != len(ledger),
        "previews": previews,
        "preview_total_bytes": preview_total,
        "projection_config_hash": _value_hash(cfg),
    }
    if len(_canonical(projection)) > cfg["max_projection_bytes"]:
        raise RepositoryMaterializationError(
            "adapter generation projection 超 policy bytes 上限")
    projection["projection_hash"] = _value_hash(projection)
    return projection
