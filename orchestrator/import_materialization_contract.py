"""Pure projection from a verified repository snapshot to the bounded DB plan_ref."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping


def canonical_hash(value: Any) -> str:
    """Historical ImportWorker hash: bare SHA256 of canonical JSON without LF."""
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def execution_contract(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Everything affecting adapter execution, excluding separately hashed files."""
    result = {
        "smoke_cmd": list(spec["smoke_cmd"]),
        "eval_cmd": list(spec["eval_cmd"]),
        "protocol_id": spec["protocol_id"],
        "protocol_ver": spec["protocol_ver"],
        "eval_key": spec["eval_key"],
        "target_set_hash": spec["target_set_hash"],
        "required": spec["required"],
        "artifact_relpath": spec.get("artifact_relpath"),
        "artifact_type": spec.get("artifact_type", "external_model"),
        "env_hash": spec.get("env_hash", "import-env"),
        "supply_chain": spec.get("supply_chain") or {},
    }
    for key in (
            "factory_protocol", "metric_log_map",
            "repository_snapshot_hash", "execution_image"):
        if key in spec:
            result[key] = spec[key]
    return result


def spec_ledger(spec: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Normalize legacy in-memory files and repository file ledgers."""
    if "file_ledger" not in spec:
        files = spec.get("files")
        if not isinstance(files, dict) or not files:
            raise RuntimeError("import materialization 缺 files/file_ledger")
        ledger = []
        for name, content in sorted(files.items()):
            payload = content if isinstance(content, bytes) else str(content).encode()
            ledger.append({
                "path": name,
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload), "git_mode": "100644",
            })
    else:
        raw = spec["file_ledger"]
        if not isinstance(raw, list) or not raw:
            raise RuntimeError("import file_ledger 须为非空 list")
        ledger = [{
            "path": item.get("path") if isinstance(item, dict) else None,
            "sha256": item.get("sha256") if isinstance(item, dict) else None,
            "bytes": item.get("bytes") if isinstance(item, dict) else None,
            "git_mode": item.get("git_mode", "100644")
            if isinstance(item, dict) else None,
        } for item in raw]
    seen = set()
    normalized = []
    for item in ledger:
        path = item["path"]
        if (not isinstance(path, str) or not path or "\\" in path
                or PurePosixPath(path).is_absolute()
                or any(part in ("", ".", "..") for part in path.split("/"))
                or path in seen
                or not isinstance(item["sha256"], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["sha256"]) is None
                or isinstance(item["bytes"], bool)
                or not isinstance(item["bytes"], int) or item["bytes"] < 0
                or item["git_mode"] not in ("100644", "100755")):
            raise RuntimeError("import materialization file ledger 非法")
        seen.add(path)
        normalized.append(dict(item))
    return sorted(normalized, key=lambda item: item["path"])


def artifact_entry(spec: Mapping[str, Any]) -> Dict[str, Any]:
    ledger = spec_ledger(spec)
    rel = spec.get("artifact_relpath") or ledger[0]["path"]
    matches = [item for item in ledger if item["path"] == rel]
    if len(matches) != 1:
        raise RuntimeError("import artifact_relpath 未绑定唯一文件")
    return matches[0]


def spec_ref(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Bounded DB plan_ref; content identities only, never raw frozen files."""
    ledger = spec_ledger(spec)
    if "source_tree" not in spec:
        return {
            "materialization_contract": execution_contract(spec),
            "files": [{
                "path": item["path"], "sha256": item["sha256"],
                "bytes": item["bytes"],
            } for item in ledger],
        }
    return {
        "materialization_contract": execution_contract(spec),
        "file_ledger_hash": canonical_hash(ledger),
        "file_count": len(ledger),
        "total_bytes": sum(item["bytes"] for item in ledger),
        "repository_snapshot_hash": spec.get("repository_snapshot_hash"),
    }
