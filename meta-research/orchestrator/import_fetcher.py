"""Default materializer for immutable external-candidate content snapshots.

The discovery/search side must store a bounded, content-addressed ``materialization`` object inside
``external_candidate.search_snapshot_json``.  This provider performs no network access and never guesses a moving
repository ref: it reconstructs the exact frozen files and adapter commands whose hashes were already registered.
It explicitly marks those commands as requiring an adversarial execution sandbox; the ordinary process guardian is
only a lifecycle fence and must never be mistaken for that sandbox.  This provider intentionally handles bounded
pre-frozen materialization snapshots; generic pinned repository archive/LFS acquisition and adapter synthesis remain
a separate production connector path rather than being guessed here.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Dict


_MAX_FILES = 256
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_ARGV = 128
_MAX_ARG_BYTES = 4096
_MAX_SNAPSHOT_BYTES = 96 * 1024 * 1024
_MAX_SUPPLY_CHAIN_BYTES = 64 * 1024
_MAX_REQUIRED = 256
_ARTIFACT_TYPES = {
    "checkpoint", "external_model", "prompt_only", "algorithm", "retrieval_index",
}
_SUPPLY_CHAIN_KEYS = {
    "dependency_lock_hash", "harness_adapter_hash", "environment_hash",
    "network_isolation",
}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _safe_relpath(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 512:
        raise ValueError("materialization file path 须为不超过 512 bytes 的非空 UTF-8 字符串")
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in raw):
        raise ValueError(f"materialization file path 含非法字符: {raw!r}")
    path = PurePosixPath(raw)
    raw_parts = raw.split("/")
    if (path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts)
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ValueError(f"materialization file path 非安全相对路径: {raw!r}")
    return path.as_posix()


def _argv(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_ARGV:
        raise ValueError(f"materialization.{field} 须为 1..{_MAX_ARGV} 项 argv")
    out = []
    for index, arg in enumerate(value):
        if (not isinstance(arg, str) or not arg or "\x00" in arg
                or len(arg.encode("utf-8")) > _MAX_ARG_BYTES):
            raise ValueError(
                f"materialization.{field}[{index}] 须为不超过 {_MAX_ARG_BYTES} bytes 的非空参数")
        out.append(arg)
    return out


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"materialization.{field} 须为正整数")
    return value


def _bounded_string(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"materialization.{field} 须为非空字符串")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"materialization.{field} 非合法 UTF-8") from error
    if size > max_bytes:
        raise ValueError(f"materialization.{field} 超过 {max_bytes} bytes")
    return value.strip()


class FrozenCandidateFetcher:
    """Decode one exact, bounded materialization spec from an immutable discovery snapshot."""

    def __call__(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(candidate.get("revision"), str) or not candidate["revision"].strip():
            raise ValueError("默认冻结物化要求 external_candidate.revision 为非空 pinned revision")
        raw_snapshot = candidate.get("search_snapshot_json")
        if not isinstance(raw_snapshot, str):
            raise ValueError("external_candidate.search_snapshot_json 须为字符串")
        try:
            snapshot_bytes = raw_snapshot.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("external_candidate.search_snapshot_json 非合法 UTF-8") from error
        if len(snapshot_bytes) > _MAX_SNAPSHOT_BYTES:
            raise ValueError(
                f"external_candidate.search_snapshot_json 超过 {_MAX_SNAPSHOT_BYTES} bytes")
        registered_hash = candidate.get("search_snapshot_hash")
        actual_snapshot_hash = "sha256:" + hashlib.sha256(snapshot_bytes).hexdigest()
        if registered_hash != actual_snapshot_hash:
            raise ValueError("external_candidate.search_snapshot_hash 与冻结 JSON 字节不符")
        try:
            snapshot = json.loads(
                raw_snapshot,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"非有限 JSON number: {token}")))
            spec = snapshot["materialization"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                "external_candidate 缺合法 search_snapshot_json.materialization") from error
        if not isinstance(spec, dict) or spec.get("version") != 1:
            raise ValueError("materialization.version 只接受 1")
        raw_files = spec.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > _MAX_FILES:
            raise ValueError(f"materialization.files 须为 1..{_MAX_FILES} 项")
        files: Dict[str, bytes] = {}
        total = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict) or set(item) != {"path", "encoding", "data", "sha256"}:
                raise ValueError(
                    f"materialization.files[{index}] 须恰含 path/encoding/data/sha256")
            path = _safe_relpath(item["path"])
            if path in files:
                raise ValueError(f"materialization.files 路径重复: {path}")
            encoding, data = item["encoding"], item["data"]
            if not isinstance(data, str):
                raise ValueError(f"materialization.files[{index}].data 须为字符串")
            try:
                if encoding == "utf-8":
                    payload = data.encode("utf-8")
                elif encoding == "base64":
                    payload = base64.b64decode(data, validate=True)
                else:
                    raise ValueError(
                        f"materialization.files[{index}].encoding 只接受 utf-8/base64")
            except (UnicodeEncodeError, binascii.Error) as error:
                raise ValueError(f"materialization.files[{index}] 内容编码非法") from error
            if len(payload) > _MAX_FILE_BYTES:
                raise ValueError(
                    f"materialization file {path} 超过 {_MAX_FILE_BYTES} bytes")
            total += len(payload)
            if total > _MAX_TOTAL_BYTES:
                raise ValueError(
                    f"materialization files 总量超过 {_MAX_TOTAL_BYTES} bytes")
            expected = item["sha256"]
            actual = "sha256:" + hashlib.sha256(payload).hexdigest()
            if expected != actual:
                raise ValueError(f"materialization file {path} sha256 不符")
            files[path] = payload
        artifact_relpath = _safe_relpath(spec.get("artifact_relpath"))
        if artifact_relpath not in files:
            raise ValueError("materialization.artifact_relpath 不在冻结 files 中")
        artifact_type = spec.get("artifact_type", "external_model")
        if artifact_type not in _ARTIFACT_TYPES:
            raise ValueError("materialization.artifact_type 非冻结枚举")
        required = spec.get("required")
        if not isinstance(required, list) or not 1 <= len(required) <= _MAX_REQUIRED:
            raise ValueError(
                f"materialization.required 须为 1..{_MAX_REQUIRED} 个 metric id/version 对")
        parsed_required = []
        for index, pair in enumerate(required):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"materialization.required[{index}] 须为 [metric_id,metric_ver]")
            parsed_required.append([
                _positive_int(pair[0], field=f"required[{index}][0]"),
                _positive_int(pair[1], field=f"required[{index}][1]"),
            ])
        if len({tuple(pair) for pair in parsed_required}) != len(parsed_required):
            raise ValueError("materialization.required 含重复 metric id/version")
        supply_chain = spec.get("supply_chain")
        if not isinstance(supply_chain, dict) or not _SUPPLY_CHAIN_KEYS.issubset(supply_chain):
            raise ValueError(
                "materialization.supply_chain 缺 dependency_lock_hash/harness_adapter_hash/"
                "environment_hash/network_isolation")
        try:
            supply_chain_bytes = json.dumps(
                supply_chain, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ValueError("materialization.supply_chain 必须是有限 JSON 值") from error
        if len(supply_chain_bytes) > _MAX_SUPPLY_CHAIN_BYTES:
            raise ValueError(
                f"materialization.supply_chain 超过 {_MAX_SUPPLY_CHAIN_BYTES} bytes")
        for key in _SUPPLY_CHAIN_KEYS:
            value = supply_chain[key]
            if key == "network_isolation":
                if not (value is True or value == "off_after_clone"):
                    raise ValueError(
                        "materialization.supply_chain.network_isolation 只接受 true/off_after_clone")
            elif not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(
                    f"materialization.supply_chain.{key} 须为 sha256:<64 lowercase hex>")
        eval_key = spec.get("eval_key")
        target_set_hash = spec.get("target_set_hash")
        env_hash = spec.get("env_hash")
        eval_key = _bounded_string(eval_key, field="eval_key", max_bytes=256)
        target_set_hash = _bounded_string(
            target_set_hash, field="target_set_hash", max_bytes=256)
        env_hash = _bounded_string(env_hash, field="env_hash", max_bytes=256)
        if env_hash != supply_chain["environment_hash"]:
            raise ValueError("materialization.env_hash 必须等于 supply_chain.environment_hash")
        return {
            "files": files,
            "smoke_cmd": _argv(spec.get("smoke_argv"), field="smoke_argv"),
            "eval_cmd": _argv(spec.get("eval_argv"), field="eval_argv"),
            "protocol_id": _positive_int(spec.get("protocol_id"), field="protocol_id"),
            "protocol_ver": _positive_int(spec.get("protocol_ver"), field="protocol_ver"),
            "eval_key": eval_key, "target_set_hash": target_set_hash,
            "required": parsed_required, "artifact_relpath": artifact_relpath,
            "artifact_type": artifact_type, "env_hash": env_hash,
            "supply_chain": supply_chain, "requires_adversarial_sandbox": True,
        }
