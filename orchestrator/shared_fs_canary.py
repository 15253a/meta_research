"""One-shot shared-filesystem lease/guardian/SQLite/FD deployment canary.

This is deliberately not a scheduler or a second state store.  Two foreground
``node`` processes execute one fixed script through immutable JSON receipts:

* holder starts a child that owns ``InstanceLease``;
* contender must first observe ``InstanceBusyError`` with the exact owner;
* only then may the owner touch SQLite and arm an execution guardian;
* holder SIGKILLs and reaps that exact owner child;
* contender waits for the guardian fence to release, then takes over;
* contender pins an artifact FD before the owner touches SQLite;
* the two terminal receipts bind exact kill/path swap, guarded takeover,
  hot-journal recovery, FD identity and completed cleanup.

``local`` runs the same protocol with two processes on one boot.  Its result is
only a prerequisite and can never set ``two_node_verified``.  The default
verifier requires distinct machine and boot identities plus the target GPFS
mount; no CLI flag can override observed node identity.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from . import database
from .artifact_capability import ArtifactCapabilityError, open_artifact
from .deployment_preflight import longest_mount
from .instance_lease import InstanceBusyError, InstanceLease
from .process_supervisor import (
    ExecutionSupervisor,
    read_receipt,
    validate_execution_receipt,
)
from .qualification_firewall import (
    QualificationFirewallError,
    _canonical,
    _hash_bytes,
    _publish_once,
    _read_regular,
    _strict_json,
)


CONTRACT_PROTOCOL = "meta-research-shared-fs-canary-contract/v1"
NODE_PROTOCOL = "meta-research-shared-fs-canary-node/v1"
PHASE_PROTOCOL = "meta-research-shared-fs-canary-phase/v1"
FINAL_PROTOCOL = "meta-research-shared-fs-canary-final/v1"
LOCAL_SCOPE = "single-node-prerequisite"
TWO_NODE_SCOPE = "two-node-process-crash"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024
_MAX_PROBE_DB_BYTES = 16 * 1024 * 1024
_HOT_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
_CRASH_PROBE_ROWS = 32
_CRASH_PROBE_TEXT_BYTES = 16 * 1024
_POLL_S = 0.02
_STATE_REL = Path("state/shared-fs-canary")
_CONTRACT_REL = _STATE_REL / "contract.json"
_PHASES = (
    "holder_lease", "contender_ready", "crash_ready",
    "holder_complete", "contender_complete",
)


class SharedFSCanaryError(RuntimeError):
    """The fixed canary protocol is unsafe, inconsistent, or incomplete."""


def _bounded_error(error: BaseException) -> str:
    value = f"{type(error).__name__}: {error}"
    return value[:500]


def _validate_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise SharedFSCanaryError("shared-fs canary run_id 须为 32 位小写 hex")
    return run_id


def _validate_timing(timeout_s: Any, guardian_grace_s: Any) -> tuple[float, float]:
    for value, label, low, high in (
            (timeout_s, "timeout_s", 2.0, 600.0),
            (guardian_grace_s, "guardian_grace_s", 0.05, 30.0)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not low <= float(value) <= high):
            raise SharedFSCanaryError(
                f"shared-fs canary {label} 须在 [{low},{high}] 内")
    if float(guardian_grace_s) >= float(timeout_s) / 2:
        raise SharedFSCanaryError("guardian_grace_s 须小于 timeout_s/2")
    return float(timeout_s), float(guardian_grace_s)


def _canonical_root(path: Path | str, *, may_not_exist: bool) -> Path:
    raw = os.fspath(path)
    if (not isinstance(raw, str) or not raw or "\x00" in raw
            or not os.path.isabs(raw) or os.path.normpath(raw) != raw):
        raise SharedFSCanaryError("canary_root 须为规范绝对路径")
    root = Path(raw)
    try:
        resolved_parent = root.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SharedFSCanaryError("canary_root 父目录不可安全解析") from error
    resolved = resolved_parent / root.name
    if resolved != root:
        raise SharedFSCanaryError("canary_root 不得经 symlink 转向")
    if root.exists() or os.path.lexists(root):
        info = os.lstat(root)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()):
            raise SharedFSCanaryError("canary_root 身份非法")
    elif not may_not_exist:
        raise SharedFSCanaryError("canary_root 不存在")
    return root


def _implementation_hash() -> str:
    return _hash_bytes(Path(__file__).read_bytes())


def _state_path(root: Path, name: str) -> Path:
    return root / _STATE_REL / name


def _phase_path(root: Path, phase: str) -> Path:
    return root / _STATE_REL / "phases" / f"{phase}.json"


def _load_canonical(path: Path, *, label: str) -> tuple[Dict[str, Any], bytes]:
    try:
        raw = _read_regular(
            path, label=label, expected_owner=os.geteuid(), expected_mode=0o400)
        value = _strict_json(raw, label=label)
        if raw != _canonical(value):
            raise SharedFSCanaryError(f"{label} 非 canonical JSON")
    except QualificationFirewallError as error:
        raise SharedFSCanaryError(f"{label} 不可安全读取") from error
    return value, raw


def _publish(path: Path, value: Mapping[str, Any]) -> tuple[Dict[str, Any], bytes]:
    try:
        _publish_once(path, _canonical(dict(value)), mode=0o400)
    except QualificationFirewallError as error:
        raise SharedFSCanaryError(f"canary receipt 发布失败: {path.name}") from error
    return _load_canonical(path, label=path.name)


def _write_private_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("shared-fs canary short write")
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(
        path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_stable_regular(path: Path, *, max_bytes: int) -> bytes:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_size <= 0
                    or before.st_size > max_bytes):
                raise SharedFSCanaryError(
                    f"canary probe 文件身份/大小非法: {path.name}")
            chunks = []
            offset = 0
            while offset < before.st_size:
                chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
                if not chunk:
                    raise SharedFSCanaryError(
                        f"canary probe 文件截断: {path.name}")
                chunks.append(chunk)
                offset += len(chunk)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise SharedFSCanaryError(
                    f"canary probe 文件读取期漂移: {path.name}")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except SharedFSCanaryError:
        raise
    except OSError as error:
        raise SharedFSCanaryError(
            f"canary probe 文件不可安全读取: {path.name}") from error


def _hot_journal_evidence(raw: bytes) -> Dict[str, Any]:
    record_count = int.from_bytes(raw[8:12], "big") if len(raw) >= 28 else 0
    initial_pages = int.from_bytes(raw[16:20], "big") if len(raw) >= 28 else 0
    sector_size = int.from_bytes(raw[20:24], "big") if len(raw) >= 28 else 0
    page_size = int.from_bytes(raw[24:28], "big") if len(raw) >= 28 else 0
    def valid_power(value: int) -> bool:
        return 512 <= value <= 65536 and value & (value - 1) == 0
    if (len(raw) <= 512 or raw[:8] != _HOT_JOURNAL_MAGIC
            or record_count <= 0 or initial_pages <= 0
            or not valid_power(sector_size) or not valid_power(page_size)):
        raise SharedFSCanaryError(
            "DELETE crash probe 未形成可恢复 hot rollback journal")
    return {
        "journal_sha256": _hash_bytes(raw),
        "journal_bytes": len(raw),
        "journal_magic": raw[:8].hex(),
        "journal_record_count": record_count,
        "journal_initial_pages": initial_pages,
        "journal_sector_size": sector_size,
        "journal_page_size": page_size,
    }


def _mount_identity(root: Path) -> Dict[str, Any]:
    try:
        with open("/proc/self/mountinfo", "rb") as stream:
            raw = stream.read(_MAX_MOUNTINFO_BYTES + 1)
        if len(raw) > _MAX_MOUNTINFO_BYTES:
            raise ValueError("mountinfo too large")
        mount = longest_mount(root, raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SharedFSCanaryError("shared-fs canary 无法采集 mount identity") from error
    return {
        "mount_point": mount["mount_point"],
        "source": mount["source"],
        "fstype": mount["fstype"],
        "root": mount["root"],
        "major_minor": mount["major_minor"],
        "mount_options": mount["mount_options"],
        "super_options": mount["super_options"],
    }


def _node_value(root: Path, *, role: str, contract_hash: str,
                run_id: str) -> Dict[str, Any]:
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip()
        if (_MACHINE_ID_RE.fullmatch(machine_id) is None
                or _BOOT_ID_RE.fullmatch(boot_id) is None):
            raise ValueError("node identity shape")
        memory = sqlite3.connect(":memory:")
        try:
            source_id = memory.execute("SELECT sqlite_source_id()").fetchone()[0]
        finally:
            memory.close()
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        raise SharedFSCanaryError("shared-fs canary 无法采集 node identity") from error
    return {
        "version": 1,
        "protocol": NODE_PROTOCOL,
        "run_id": run_id,
        "contract_sha256": contract_hash,
        "role": role,
        "identity": {
            "machine_id": machine_id,
            "boot_id": boot_id,
            "uid": os.geteuid(),
            "gid": os.getegid(),
        },
        "mount": _mount_identity(root),
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_source_id": source_id,
        "required_journal_mode": database.journal_mode_for_path(
            root / "research.sqlite"),
        "implementation_sha256": _implementation_hash(),
    }


def _validate_contract(value: Mapping[str, Any], *, root: Path,
                       run_id: str) -> None:
    expected = {
        "version", "protocol", "run_id", "requested_scope", "canary_root",
        "expected_roles", "failure_model", "timeout_s", "guardian_grace_s",
        "implementation_sha256", "created_at_unix",
    }
    if (set(value) != expected or value.get("version") != 1
            or value.get("protocol") != CONTRACT_PROTOCOL
            or value.get("run_id") != run_id
            or value.get("requested_scope") not in {LOCAL_SCOPE, TWO_NODE_SCOPE}
            or value.get("canary_root") != str(root)
            or value.get("expected_roles") != ["contender", "holder"]
            or value.get("failure_model") != "owner-process-sigkill"
            or value.get("implementation_sha256") != _implementation_hash()
            or isinstance(value.get("created_at_unix"), bool)
            or not isinstance(value.get("created_at_unix"), (int, float))
            or not math.isfinite(float(value["created_at_unix"]))
            or float(value["created_at_unix"]) <= 0):
        raise SharedFSCanaryError("shared-fs canary contract 字段/绑定非法")
    _timeout_s, guardian_grace_s = _validate_timing(
        value.get("timeout_s"), value.get("guardian_grace_s"))
    if (value.get("requested_scope") == TWO_NODE_SCOPE
            and guardian_grace_s < 1.0):
        raise SharedFSCanaryError(
            "two-node canary guardian_grace_s 须至少 1 秒")


def _create_contract(*, root: Path, run_id: str, scope: str,
                     timeout_s: float, guardian_grace_s: float) -> Dict[str, Any]:
    if scope not in {LOCAL_SCOPE, TWO_NODE_SCOPE}:
        raise SharedFSCanaryError("shared-fs canary scope 非法")
    if scope == TWO_NODE_SCOPE and guardian_grace_s < 1.0:
        raise SharedFSCanaryError(
            "two-node canary guardian_grace_s 须至少 1 秒")
    if not root.exists():
        root.mkdir(mode=0o700)
    contract_path = root / _CONTRACT_REL
    if os.path.lexists(contract_path):
        value, _raw = _load_canonical(contract_path, label="shared-fs canary contract")
        _validate_contract(value, root=root, run_id=run_id)
        if (value["requested_scope"] != scope
                or float(value["timeout_s"]) != timeout_s
                or float(value["guardian_grace_s"]) != guardian_grace_s):
            raise SharedFSCanaryError("shared-fs canary 既有 contract 与请求冲突")
        return value
    if any(root.iterdir()):
        raise SharedFSCanaryError(
            "canary_root 必须为专用空目录，拒绝触碰既有 work-root")
    os.chmod(root, 0o700)
    value = {
        "version": 1,
        "protocol": CONTRACT_PROTOCOL,
        "run_id": run_id,
        "requested_scope": scope,
        "canary_root": str(root),
        "expected_roles": ["contender", "holder"],
        "failure_model": "owner-process-sigkill",
        "timeout_s": timeout_s,
        "guardian_grace_s": guardian_grace_s,
        "implementation_sha256": _implementation_hash(),
        "created_at_unix": time.time(),
    }
    published, _raw = _publish(contract_path, value)
    _validate_contract(published, root=root, run_id=run_id)
    return published


def _wait_contract(*, root: Path, run_id: str, timeout_s: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    path = root / _CONTRACT_REL
    while time.monotonic() < deadline:
        if os.path.lexists(path):
            value, _raw = _load_canonical(path, label="shared-fs canary contract")
            _validate_contract(value, root=root, run_id=run_id)
            return value
        time.sleep(_POLL_S)
    raise SharedFSCanaryError("shared-fs canary 等待 contract 超时")


def _contract_raw(root: Path, contract: Mapping[str, Any]) -> bytes:
    value, raw = _load_canonical(root / _CONTRACT_REL, label="shared-fs canary contract")
    if value != dict(contract):
        raise SharedFSCanaryError("shared-fs canary contract 读取期漂移")
    return raw


def _publish_node(root: Path, *, role: str,
                  contract: Mapping[str, Any]) -> Dict[str, Any]:
    contract_hash = _hash_bytes(_contract_raw(root, contract))
    value = _node_value(
        root, role=role, contract_hash=contract_hash,
        run_id=str(contract["run_id"]))
    published, _raw = _publish(_state_path(root, f"node-{role}.json"), value)
    if published != value:
        raise SharedFSCanaryError(f"shared-fs canary node-{role} 身份冲突")
    return published


def _load_node(root: Path, *, role: str,
               contract: Mapping[str, Any]) -> Dict[str, Any]:
    value, _raw = _load_canonical(
        _state_path(root, f"node-{role}.json"), label=f"node-{role}")
    contract_hash = _hash_bytes(_contract_raw(root, contract))
    expected = {
        "version", "protocol", "run_id", "contract_sha256", "role", "identity",
        "mount", "python_version", "sqlite_version",
        "sqlite_source_id", "required_journal_mode", "implementation_sha256",
    }
    if (set(value) != expected or value.get("version") != 1
            or value.get("protocol") != NODE_PROTOCOL
            or value.get("run_id") != contract["run_id"]
            or value.get("contract_sha256") != contract_hash
            or value.get("role") != role
            or value.get("implementation_sha256") != contract["implementation_sha256"]
            or value.get("required_journal_mode") not in {"wal", "delete"}
            or not isinstance(value.get("identity"), dict)
            or not isinstance(value.get("mount"), dict)):
        raise SharedFSCanaryError(f"shared-fs canary node-{role} receipt 非法")
    return value


def _phase_value(*, contract: Mapping[str, Any], phase: str, role: str,
                 evidence: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "version": 1,
        "protocol": PHASE_PROTOCOL,
        "run_id": contract["run_id"],
        "contract_sha256": _hash_bytes(_canonical(dict(contract))),
        "phase": phase,
        "role": role,
        "evidence": dict(evidence),
    }


def _publish_phase(root: Path, *, contract: Mapping[str, Any], phase: str,
                   role: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    value = _phase_value(
        contract=contract, phase=phase, role=role,
        evidence=evidence)
    published, _raw = _publish(_phase_path(root, phase), value)
    if published != value:
        raise SharedFSCanaryError(f"shared-fs canary phase {phase} 冲突")
    return published


def _load_phase(root: Path, *, contract: Mapping[str, Any], phase: str,
                role: Optional[str] = None) -> tuple[Dict[str, Any], bytes]:
    value, raw = _load_canonical(_phase_path(root, phase), label=f"phase {phase}")
    expected = {
        "version", "protocol", "run_id", "contract_sha256", "phase",
        "role", "evidence",
    }
    if (set(value) != expected or value.get("version") != 1
            or value.get("protocol") != PHASE_PROTOCOL
            or value.get("run_id") != contract["run_id"]
            or value.get("contract_sha256") != _hash_bytes(_canonical(dict(contract)))
            or value.get("phase") != phase
            or (role is not None and value.get("role") != role)
            or not isinstance(value.get("evidence"), dict)):
        raise SharedFSCanaryError(f"shared-fs canary phase {phase} receipt 非法")
    return value, raw


def _publish_failure(root: Path, *, contract: Mapping[str, Any], role: str,
                     phase: str, error: BaseException) -> None:
    try:
        _publish_phase(
            root, contract=contract, phase=f"failure-{role}", role=role,
            evidence={"at_phase": phase, "error": _bounded_error(error)})
    except BaseException:
        pass


def _wait_phase(root: Path, *, contract: Mapping[str, Any], phase: str,
                role: Optional[str], deadline: float) -> Dict[str, Any]:
    path = _phase_path(root, phase)
    while time.monotonic() < deadline:
        for failed_role in ("holder", "contender"):
            failed_path = _phase_path(root, f"failure-{failed_role}")
            if os.path.lexists(failed_path):
                failure, _raw = _load_phase(
                    root, contract=contract, phase=f"failure-{failed_role}",
                    role=failed_role)
                raise SharedFSCanaryError(
                    f"shared-fs canary {failed_role} 失败: "
                    f"{failure['evidence'].get('error')}")
        if os.path.lexists(path):
            value, _raw = _load_phase(
                root, contract=contract, phase=phase, role=role)
            return value
        time.sleep(_POLL_S)
    raise SharedFSCanaryError(f"shared-fs canary 等待 phase {phase} 超时")


def _owner_metadata(value: Any) -> Dict[str, Any]:
    required = {
        "version", "owner_id", "hostname", "boot_id", "pid",
        "process_start_ticks", "acquired_at_unix", "work_root_dev",
        "work_root_ino", "heartbeat_interval_s", "heartbeat_deadline_s",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise SharedFSCanaryError("shared-fs canary owner metadata 非法")
    return dict(value)


def _find_guardian_receipt(root: Path, *, run_id: str,
                           deadline: float) -> tuple[Path, Dict[str, Any]]:
    receipt_dir = root / "state/executions"
    while time.monotonic() < deadline:
        matches = []
        for path in sorted(receipt_dir.glob("execution-*.json")):
            try:
                value = read_receipt(path)
                validate_execution_receipt(value, path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            context = value.get("context")
            if (isinstance(context, dict) and context.get("run_id") == run_id
                    and context.get("phase") == "guardian"):
                matches.append((path, value))
        if len(matches) == 1 and matches[0][1].get("state") == "running":
            return matches[0]
        if len(matches) > 1:
            raise SharedFSCanaryError("shared-fs canary guardian receipt 重复")
        time.sleep(_POLL_S)
    raise SharedFSCanaryError("shared-fs canary guardian 未进入 running")


def _arm_wrapper_death_guard(*, wrapper_pid: int, read_fd: int) -> None:
    """SIGKILL this owner if its holder wrapper closes the private pipe."""
    try:
        info = os.fstat(read_fd)
    except OSError as error:
        raise SharedFSCanaryError("owner parent-death fd 不可用") from error
    if (wrapper_pid <= 1 or read_fd < 3 or not stat.S_ISFIFO(info.st_mode)
            or os.getppid() != wrapper_pid):
        raise SharedFSCanaryError("owner parent-death identity 非法")

    def watch() -> None:
        try:
            # The wrapper never writes.  EOF, data, or an fd error all mean
            # this owner can no longer prove its launcher is alive.
            os.read(read_fd, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(read_fd)
            finally:
                os.kill(os.getpid(), signal.SIGKILL)

    threading.Thread(
        target=watch, daemon=True,
        name="shared-fs-canary-wrapper-death").start()


def _owner_process(root: Path, *, run_id: str,
                   wrapper_pid: int, parent_fd: int) -> int:
    _arm_wrapper_death_guard(wrapper_pid=wrapper_pid, read_fd=parent_fd)
    contract = _wait_contract(root=root, run_id=run_id, timeout_s=30.0)
    timeout_s = float(contract["timeout_s"])
    deadline = time.monotonic() + timeout_s
    _load_node(root, role="holder", contract=contract)
    lease: Optional[InstanceLease] = None
    conn: Optional[sqlite3.Connection] = None
    supervisor: Optional[ExecutionSupervisor] = None
    guardian_thread: Optional[threading.Thread] = None
    guardian_errors: list[BaseException] = []
    phase = "holder_lease"
    try:
        lease = InstanceLease.acquire(root, heartbeat_interval_s=0.05)
        _publish_phase(
            root, contract=contract, phase="holder_lease", role="holder",
            evidence={"owner": dict(lease.owner)})
        phase = "contender_ready"
        ready = _wait_phase(
            root, contract=contract, phase="contender_ready",
            role="contender", deadline=deadline)
        if _owner_metadata(ready["evidence"].get("busy_owner")) != dict(lease.owner):
            raise SharedFSCanaryError("contender Busy owner 未精确绑定 holder")

        phase = "sqlite_and_guardian"
        conn = database.connect(root / "research.sqlite")
        conn.execute(
            "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,?,?)",
            (f"shared-fs committed {run_id}", "{}"))
        conn.commit()
        database_path = root / "research.sqlite"
        baseline_db = _read_stable_regular(
            database_path, max_bytes=_MAX_PROBE_DB_BYTES)
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        supervisor = ExecutionSupervisor(
            receipt_dir=root / "state/executions", owner_id=lease.owner_id,
            owner_guard=lease.assert_owned,
            fence_context_factory=lease.delegate_owner_fence,
            term_grace_s=float(contract["guardian_grace_s"]))
        child_ready = _state_path(root, "guardian-child.ready")
        fence_observed = _state_path(root, "guardian-fence-observed.json")
        child_code = "\n".join((
            "import os, signal, sys, time",
            "ready, observed, duration = sys.argv[1], sys.argv[2], float(sys.argv[3])",
            "def stop(*_args):",
            "    while not os.path.exists(observed):",
            "        time.sleep(0.01)",
            "    raise SystemExit(0)",
            "signal.signal(signal.SIGTERM, stop)",
            "fd = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
            "os.write(fd, b'ready\\n'); os.fsync(fd); os.close(fd)",
            "time.sleep(duration)",
        ))

        def run_guardian() -> None:
            try:
                supervisor.run(
                    [sys.executable, "-c", child_code, str(child_ready),
                     str(fence_observed), str(timeout_s * 2)],
                    capture_output=True, timeout_s=timeout_s * 2,
                    kind="shared-fs-canary",
                    operation_context={"run_id": run_id, "phase": "guardian"})
            except BaseException as error:
                guardian_errors.append(error)

        guardian_thread = threading.Thread(target=run_guardian, daemon=False)
        guardian_thread.start()
        while time.monotonic() < deadline and not child_ready.exists():
            if guardian_errors:
                raise guardian_errors[0]
            time.sleep(_POLL_S)
        if not child_ready.exists():
            raise SharedFSCanaryError("shared-fs canary guardian child 未 ready")
        receipt_path, running = _find_guardian_receipt(
            root, run_id=run_id, deadline=deadline)
        if (running.get("owner_id") != lease.owner_id
                or running.get("fenced_by_instance_lease") is not True):
            raise SharedFSCanaryError("shared-fs canary guardian 未绑定 lease")
        conn.execute("PRAGMA cache_size = 8")
        conn.execute("PRAGMA cache_spill = ON")
        conn.execute("BEGIN IMMEDIATE")
        payload_suffix = "x" * _CRASH_PROBE_TEXT_BYTES
        for goal_id in range(2, 2 + _CRASH_PROBE_ROWS):
            conn.execute(
                "INSERT INTO goal(id,version,text,predicate_json) VALUES (?,1,?,?)",
                (goal_id,
                 f"shared-fs uncommitted {run_id}:{goal_id}:{payload_suffix}",
                 "{}"))
        dirty_db = _read_stable_regular(
            database_path, max_bytes=_MAX_PROBE_DB_BYTES)
        crash_evidence: Dict[str, Any] = {
            "owner_id": lease.owner_id,
            "guardian_receipt": str(receipt_path),
            "guardian_operation_id": running["operation_id"],
            "journal_mode": journal,
            "synchronous": synchronous,
            "committed_goal_id": 1,
            "uncommitted_goal_first": 2,
            "uncommitted_goal_last": 1 + _CRASH_PROBE_ROWS,
            "uncommitted_goal_count": _CRASH_PROBE_ROWS,
            "baseline_db_sha256": _hash_bytes(baseline_db),
            "baseline_db_bytes": len(baseline_db),
            "dirty_db_sha256": _hash_bytes(dirty_db),
            "dirty_db_bytes": len(dirty_db),
            "hot_rollback_journal": False,
        }
        if journal == "delete":
            if dirty_db == baseline_db:
                raise SharedFSCanaryError(
                    "DELETE crash probe 未把未提交页 spill 到 database")
            journal_raw = _read_stable_regular(
                Path(str(database_path) + "-journal"),
                max_bytes=_MAX_PROBE_DB_BYTES)
            crash_evidence.update(_hot_journal_evidence(journal_raw))
            crash_evidence["hot_rollback_journal"] = True
        phase = "crash_ready"
        _publish_phase(
            root, contract=contract, phase="crash_ready", role="holder",
            evidence=crash_evidence)
        while True:
            time.sleep(1.0)
    except BaseException as error:
        _publish_failure(
            root, contract=contract, role="holder",
            phase=phase, error=error)
        return 3
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException:
                pass
        if supervisor is not None:
            try:
                supervisor.close(timeout_s=5.0)
            except BaseException:
                pass
        if guardian_thread is not None:
            guardian_thread.join(timeout=5.0)
        if lease is not None:
            try:
                lease.close()
            except BaseException:
                pass


def _holder_node(root: Path, *, contract: Mapping[str, Any]) -> Dict[str, Any]:
    _publish_node(root, role="holder", contract=contract)
    deadline = time.monotonic() + float(contract["timeout_s"])
    parent_read, parent_write = os.pipe2(os.O_CLOEXEC)
    try:
        owner = subprocess.Popen(
            [sys.executable, "-m", "orchestrator.shared_fs_canary", "_owner",
             "--canary-root", str(root), "--run-id", str(contract["run_id"]),
             "--wrapper-pid", str(os.getpid()), "--parent-fd", str(parent_read)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=(parent_read,), start_new_session=True)
    except BaseException:
        os.close(parent_read)
        os.close(parent_write)
        raise
    os.close(parent_read)
    phase = "holder_lease"
    try:
        holder_lease = _wait_phase(
            root, contract=contract, phase="holder_lease",
            role="holder", deadline=deadline)
        owner_meta = _owner_metadata(holder_lease["evidence"].get("owner"))
        if owner_meta["pid"] != owner.pid:
            raise SharedFSCanaryError("holder wrapper 未绑定 exact owner child")
        phase = "crash_ready"
        _wait_phase(
            root, contract=contract, phase="crash_ready",
            role="holder", deadline=deadline)
        contender_ready = _wait_phase(
            root, contract=contract, phase="contender_ready",
            role="contender", deadline=deadline)
        os.kill(owner.pid, signal.SIGKILL)
        returncode = owner.wait(timeout=5.0)
        if returncode != -signal.SIGKILL:
            raise SharedFSCanaryError(
                f"holder owner child 未以 SIGKILL 终止: {returncode}")
        phase = "fd_swap"
        evidence = contender_ready["evidence"]
        current = _state_path(root, "fd-current.bin")
        archived = _state_path(root, "fd-original-renamed.bin")
        if (evidence.get("fd_path") != str(current)
                or os.path.lexists(archived)):
            raise SharedFSCanaryError("shared-fs canary FD swap 路径绑定非法")
        before = os.lstat(current)
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or [before.st_dev, before.st_ino, before.st_size]
                != [evidence.get("fd_device"), evidence.get("fd_inode"),
                    evidence.get("fd_bytes")]):
            raise SharedFSCanaryError("shared-fs canary 跨节点 FD identity 不一致")
        os.rename(current, archived)
        replacement = ("meta-research-fd-replacement:" + str(contract["run_id"])).encode()
        _write_private_once(current, replacement)
        return {
            "owner_id": owner_meta["owner_id"],
            "pid": owner.pid,
            "process_start_ticks": owner_meta["process_start_ticks"],
            "signal": "SIGKILL",
            "returncode": returncode,
            "fd_path": str(current),
            "fd_archived_path": str(archived),
            "fd_original_sha256": evidence.get("fd_sha256"),
            "fd_replacement_sha256": _hash_bytes(replacement),
            "fd_replacement_bytes": len(replacement),
        }
    except BaseException as error:
        _publish_failure(
            root, contract=contract, role="holder",
            phase=phase, error=error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        cleanup_error: Optional[BaseException] = None
        try:
            os.close(parent_write)
        except BaseException as error:
            cleanup_error = error
        if owner.poll() is None:
            try:
                os.kill(owner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                owner.wait(timeout=5.0)
            except subprocess.TimeoutExpired as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None and not primary_error:
            raise SharedFSCanaryError(
                "holder owner child cleanup 未完成") from cleanup_error


def _contender_node(root: Path, *, contract: Mapping[str, Any]) -> Dict[str, Any]:
    _publish_node(root, role="contender", contract=contract)
    deadline = time.monotonic() + float(contract["timeout_s"])
    phase = "holder_lease"
    lease: Optional[InstanceLease] = None
    supervisor: Optional[ExecutionSupervisor] = None
    conn: Optional[sqlite3.Connection] = None
    capability = None
    try:
        holder = _wait_phase(
            root, contract=contract, phase="holder_lease",
            role="holder", deadline=deadline)
        holder_owner = _owner_metadata(holder["evidence"].get("owner"))
        phase = "contender_ready"
        try:
            unexpected = InstanceLease.acquire(root, heartbeat_interval_s=0.05)
        except InstanceBusyError as error:
            busy_owner = _owner_metadata(error.owner)
            if busy_owner != holder_owner:
                raise SharedFSCanaryError("contender Busy owner 与 holder 冲突")
        else:
            unexpected.close()
            raise SharedFSCanaryError(
                "shared-fs canary 检测到双 owner；在此前未触碰 SQLite")

        original = ("meta-research-fd-original:" + str(contract["run_id"])).encode()
        current = _state_path(root, "fd-current.bin")
        _write_private_once(current, original)
        capability = open_artifact(
            current, expected_hash=_hash_bytes(original),
            expected_size=len(original), label="shared-fs canary FD")
        phase = "contender_ready"
        _publish_phase(
            root, contract=contract, phase="contender_ready", role="contender",
            evidence={
                "busy_owner": busy_owner,
                "fd_path": str(current),
                "fd_sha256": capability.identity.content_hash,
                "fd_bytes": capability.identity.size_bytes,
                "fd_device": capability.identity.device,
                "fd_inode": capability.identity.inode,
            })
        phase = "crash_ready"
        crash = _wait_phase(
            root, contract=contract, phase="crash_ready",
            role="holder", deadline=deadline)
        phase = "holder_complete"
        holder_complete = _wait_phase(
            root, contract=contract, phase="holder_complete",
            role="holder", deadline=deadline)
        holder_evidence = holder_complete["evidence"]
        if (holder_evidence.get("owner_id") != holder_owner["owner_id"]
                or holder_evidence.get("pid") != holder_owner["pid"]
                or holder_evidence.get("process_start_ticks")
                != holder_owner["process_start_ticks"]
                or holder_evidence.get("returncode") != -signal.SIGKILL
                or holder_evidence.get("signal") != "SIGKILL"
                or holder_evidence.get("cleanup_complete") is not True):
            raise SharedFSCanaryError("holder_complete 未绑定 exact SIGKILL/cleanup")

        phase = "guardian_takeover"
        busy_after_kill = 0
        holder_complete_hash = _hash_bytes(_canonical(holder_complete))
        fence_observed = _state_path(root, "guardian-fence-observed.json")
        fence_observed_raw: Optional[bytes] = None
        while time.monotonic() < deadline:
            try:
                lease = InstanceLease.acquire(root, heartbeat_interval_s=0.05)
                if busy_after_kill < 1:
                    close_error = lease.close()
                    lease = None
                    if close_error is not None:
                        raise close_error
                    raise SharedFSCanaryError(
                        "owner death 后未观测 delegated guardian fence")
                break
            except InstanceBusyError as error:
                busy_after = _owner_metadata(error.owner)
                if busy_after["owner_id"] != holder_owner["owner_id"]:
                    raise SharedFSCanaryError("guardian 期间 owner generation 漂移")
                busy_after_kill += 1
                if fence_observed_raw is None:
                    fence_observed_raw = _canonical({
                        "version": 1,
                        "run_id": contract["run_id"],
                        "old_owner_id": holder_owner["owner_id"],
                        "holder_complete_sha256": holder_complete_hash,
                    })
                    _write_private_once(fence_observed, fence_observed_raw)
                time.sleep(_POLL_S)
        if lease is None:
            raise SharedFSCanaryError("guardian fence 未在 deadline 内释放")

        phase = "guardian_recovery"
        supervisor = ExecutionSupervisor(
            receipt_dir=root / "state/executions", owner_id=lease.owner_id,
            owner_guard=lease.assert_owned,
            fence_context_factory=lease.delegate_owner_fence,
            term_grace_s=float(contract["guardian_grace_s"]))
        supervisor.recover_previous_generation()
        receipt_path = Path(crash["evidence"].get("guardian_receipt", ""))
        guardian = read_receipt(receipt_path)
        validate_execution_receipt(guardian, receipt_path)
        if (guardian.get("operation_id") != crash["evidence"].get("guardian_operation_id")
                or guardian.get("owner_id") != holder_owner["owner_id"]
                or guardian.get("state") != "terminal"
                or guardian.get("outcome") != "owner_lost"
                or guardian.get("fenced_by_instance_lease") is not True
                or guardian.get("group_drained") is not True
                or guardian.get("term_sent") is not True
                or not isinstance(guardian.get("kill_sent"), bool)):
            raise SharedFSCanaryError("guardian terminal/drain receipt 不完整")
        guardian_hash = _hash_bytes(_canonical(guardian))

        phase = "journal_recovery"
        database_path = root / "research.sqlite"
        conn = database.connect(database_path)
        committed = conn.execute("SELECT text FROM goal WHERE id=1").fetchone()
        uncommitted = conn.execute(
            "SELECT count(*) FROM goal WHERE id BETWEEN ? AND ?",
            (crash["evidence"].get("uncommitted_goal_first"),
             crash["evidence"].get("uncommitted_goal_last"))).fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        quick = conn.execute("PRAGMA quick_check").fetchall()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        database.verify_schema(conn)
        expected_journal = database.journal_mode_for_path(database_path)
        recovered_db = _read_stable_regular(
            database_path, max_bytes=_MAX_PROBE_DB_BYTES)
        rollback_bytes_restored = (
            _hash_bytes(recovered_db)
            == crash["evidence"].get("baseline_db_sha256"))
        journal_removed = not os.path.lexists(Path(str(database_path) + "-journal"))
        if (committed != (f"shared-fs committed {contract['run_id']}",)
                or uncommitted != 0 or journal != expected_journal
                or synchronous != 2 or quick != [("ok",)] or foreign_keys != []):
            raise SharedFSCanaryError("SQLite crash recovery 结果不符合同")
        if (expected_journal == "delete"
                and (crash["evidence"].get("hot_rollback_journal") is not True
                     or not rollback_bytes_restored or not journal_removed)):
            raise SharedFSCanaryError("SQLite hot rollback recovery 未闭合")

        phase = "fd_recovery"
        capability.verify_unchanged()
        os.lseek(capability.fd, 0, os.SEEK_SET)
        observed = os.read(capability.fd, len(original) + 1)
        if observed != original:
            raise SharedFSCanaryError("open FD 未保留原 bytes")
        try:
            capability.verify_path_binding()
        except ArtifactCapabilityError:
            path_binding_rejected = True
        else:
            raise SharedFSCanaryError("FD path 替换后未 fail closed")
        with open_artifact(
                current,
                expected_hash=holder_evidence.get("fd_replacement_sha256"),
                expected_size=holder_evidence.get("fd_replacement_bytes"),
                label="shared-fs canary replacement") as replacement_capability:
            replacement_hash = replacement_capability.identity.content_hash

        return {
            "old_owner_id": holder_owner["owner_id"],
            "new_owner_id": lease.owner_id,
            "busy_after_kill": busy_after_kill,
            "holder_complete_sha256": holder_complete_hash,
            "fence_observed_sha256": _hash_bytes(fence_observed_raw or b""),
            "guardian_receipt_sha256": guardian_hash,
            "guardian_outcome": guardian["outcome"],
            "guardian_group_drained": guardian["group_drained"],
            "guardian_term_sent": guardian["term_sent"],
            "guardian_kill_sent": guardian["kill_sent"],
            "journal_mode": journal,
            "required_journal_mode": expected_journal,
            "synchronous": synchronous,
            "quick_check": "ok",
            "foreign_key_violations": 0,
            "committed_retained": True,
            "uncommitted_discarded": True,
            "hot_rollback_recovered": (
                expected_journal == "delete" and rollback_bytes_restored),
            "recovered_db_sha256": _hash_bytes(recovered_db),
            "journal_removed_after_recovery": journal_removed,
            "fd_original_sha256": capability.identity.content_hash,
            "fd_replacement_sha256": replacement_hash,
            "fd_original_bytes_retained": True,
            "fd_path_binding_rejected": path_binding_rejected,
        }
    except BaseException as error:
        _publish_failure(
            root, contract=contract, role="contender",
            phase=phase, error=error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        cleanup_error: Optional[BaseException] = None
        for resource in (capability, conn):
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if supervisor is not None:
            try:
                supervisor.close(timeout_s=5.0)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if lease is not None:
            close_error = lease.close()
            cleanup_error = cleanup_error or close_error
        if cleanup_error is not None and not primary_error:
            raise cleanup_error


def run_node_canary(
        *, canary_root: Path | str, run_id: str, role: str,
        timeout_s: float = 60.0, guardian_grace_s: float = 5.0,
        required_scope: str = TWO_NODE_SCOPE) -> Dict[str, Any]:
    """Run one fixed holder or contender role; remote launch is operator-owned."""
    run_id = _validate_run_id(run_id)
    timeout_s, guardian_grace_s = _validate_timing(timeout_s, guardian_grace_s)
    if role not in {"holder", "contender"}:
        raise SharedFSCanaryError("shared-fs canary role 非法")
    if required_scope not in {LOCAL_SCOPE, TWO_NODE_SCOPE}:
        raise SharedFSCanaryError("node required_scope 非法")
    if required_scope == TWO_NODE_SCOPE and guardian_grace_s < 1.0:
        raise SharedFSCanaryError(
            "two-node canary guardian_grace_s 须至少 1 秒")
    root = _canonical_root(canary_root, may_not_exist=True)
    if role == "holder":
        if os.path.lexists(root / _CONTRACT_REL):
            contract = _wait_contract(root=root, run_id=run_id, timeout_s=timeout_s)
            if (contract["requested_scope"] != required_scope
                    or float(contract["timeout_s"]) != timeout_s
                    or float(contract["guardian_grace_s"]) != guardian_grace_s):
                raise SharedFSCanaryError("node scope/timing 与 immutable contract 冲突")
        else:
            contract = _create_contract(
                root=root, run_id=run_id, scope=required_scope,
                timeout_s=timeout_s, guardian_grace_s=guardian_grace_s)
        result = _holder_node(root, contract=contract)
        _load_node(root, role=role, contract=contract)
        complete = {**result, "cleanup_complete": True}
        _publish_phase(
            root, contract=contract, phase="holder_complete", role=role,
            evidence=complete)
        return complete
    contract = _wait_contract(root=root, run_id=run_id, timeout_s=timeout_s)
    if (contract["requested_scope"] != required_scope
            or float(contract["timeout_s"]) != timeout_s
            or float(contract["guardian_grace_s"]) != guardian_grace_s):
        raise SharedFSCanaryError("node scope/timing 与 immutable contract 冲突")
    result = _contender_node(root, contract=contract)
    _load_node(root, role=role, contract=contract)
    complete = {**result, "cleanup_complete": True}
    _publish_phase(
        root, contract=contract, phase="contender_complete", role=role,
        evidence=complete)
    return complete


def _phase_semantics(phases: Mapping[str, Mapping[str, Any]]) -> list[str]:
    failures = []
    holder_owner = phases["holder_lease"]["evidence"].get("owner")
    ready = phases["contender_ready"]["evidence"]
    crash = phases["crash_ready"]["evidence"]
    holder = phases["holder_complete"]["evidence"]
    contender = phases["contender_complete"]["evidence"]
    busy_owner = ready.get("busy_owner")
    if not isinstance(holder_owner, dict) or busy_owner != holder_owner:
        failures.append("contender_busy_owner_mismatch")
    if (holder.get("cleanup_complete") is not True
            or holder.get("owner_id") != (holder_owner or {}).get("owner_id")
            or holder.get("pid") != (holder_owner or {}).get("pid")
            or holder.get("process_start_ticks")
            != (holder_owner or {}).get("process_start_ticks")
            or holder.get("signal") != "SIGKILL"
            or holder.get("returncode") != -signal.SIGKILL):
        failures.append("owner_kill_identity_mismatch")
    required_contender = {
        "cleanup_complete": True,
        "guardian_outcome": "owner_lost",
        "guardian_group_drained": True,
        "guardian_term_sent": True,
        "synchronous": 2,
        "quick_check": "ok",
        "foreign_key_violations": 0,
        "committed_retained": True,
        "uncommitted_discarded": True,
        "fd_original_bytes_retained": True,
        "fd_path_binding_rejected": True,
    }
    for key, expected in required_contender.items():
        if contender.get(key) != expected:
            failures.append(f"contender_{key}")
    if (isinstance(contender.get("guardian_kill_sent"), bool) is False
            or isinstance(contender.get("busy_after_kill"), bool)
            or not isinstance(contender.get("busy_after_kill"), int)
            or contender["busy_after_kill"] < 1):
        failures.append("guardian_busy_count_invalid")
    if (contender.get("old_owner_id") != (holder_owner or {}).get("owner_id")
            or contender.get("new_owner_id") == contender.get("old_owner_id")):
        failures.append("takeover_owner_generation")
    if (crash.get("owner_id") != (holder_owner or {}).get("owner_id")
            or crash.get("synchronous") != 2
            or crash.get("uncommitted_goal_first") != 2
            or crash.get("uncommitted_goal_last") != 1 + _CRASH_PROBE_ROWS
            or crash.get("uncommitted_goal_count") != _CRASH_PROBE_ROWS):
        failures.append("crash_probe_identity")
    if contender.get("journal_mode") != contender.get("required_journal_mode"):
        failures.append("journal_mode_mismatch")
    if contender.get("holder_complete_sha256") != _hash_bytes(
            _canonical(phases["holder_complete"])):
        failures.append("holder_complete_hash_mismatch")
    sha_fields = (
        ready.get("fd_sha256"), holder.get("fd_original_sha256"),
        contender.get("fd_original_sha256"), holder.get("fd_replacement_sha256"),
        contender.get("fd_replacement_sha256"),
        contender.get("fence_observed_sha256"),
        contender.get("guardian_receipt_sha256"),
        crash.get("baseline_db_sha256"), crash.get("dirty_db_sha256"),
        contender.get("recovered_db_sha256"),
    )
    if (any(not isinstance(value, str)
            or _SHA256_RE.fullmatch(value) is None
            for value in sha_fields)
            or len(set(sha_fields[:3])) != 1
            or sha_fields[3] != sha_fields[4]):
        failures.append("artifact_or_receipt_hash_mismatch")
    if contender.get("required_journal_mode") == "delete":
        if (not isinstance(crash.get("journal_sha256"), str)
                or _SHA256_RE.fullmatch(crash["journal_sha256"]) is None
                or crash.get("hot_rollback_journal") is not True
                or crash.get("journal_magic") != _HOT_JOURNAL_MAGIC.hex()
                or crash.get("baseline_db_sha256") == crash.get("dirty_db_sha256")
                or contender.get("hot_rollback_recovered") is not True
                or contender.get("journal_removed_after_recovery") is not True
                or contender.get("recovered_db_sha256")
                != crash.get("baseline_db_sha256")):
            failures.append("hot_rollback_not_proven")
    return failures


def verify_canary(
        *, canary_root: Path | str, run_id: str,
        required_scope: str = TWO_NODE_SCOPE) -> Dict[str, Any]:
    """Strictly aggregate immutable receipts; default requires real two-node evidence."""
    run_id = _validate_run_id(run_id)
    if required_scope not in {LOCAL_SCOPE, TWO_NODE_SCOPE}:
        raise SharedFSCanaryError("verify required_scope 非法")
    root = _canonical_root(canary_root, may_not_exist=False)
    contract = _wait_contract(root=root, run_id=run_id, timeout_s=2.0)
    if contract["requested_scope"] != required_scope:
        raise SharedFSCanaryError(
            "verify scope 与 immutable contract 冲突；local evidence 不得升级")
    final_path = _state_path(
        root, "final-local.json" if required_scope == LOCAL_SCOPE else "final.json")
    if os.path.lexists(final_path):
        existing, _raw = _load_canonical(final_path, label=final_path.name)
        if (existing.get("protocol") != FINAL_PROTOCOL
                or existing.get("run_id") != run_id
                or existing.get("required_scope") != required_scope
                or existing.get("status") != "passed"):
            raise SharedFSCanaryError("shared-fs canary final receipt 冲突")
        return existing

    missing = []
    for role in ("holder", "contender"):
        if not os.path.lexists(_state_path(root, f"node-{role}.json")):
            missing.append(f"node-{role}")
    for phase in _PHASES:
        if not os.path.lexists(_phase_path(root, phase)):
            missing.append(phase)
    if not os.path.lexists(_state_path(root, "guardian-fence-observed.json")):
        missing.append("guardian-fence-observed")
    failures = []
    for role in ("holder", "contender"):
        failure_path = _phase_path(root, f"failure-{role}")
        if os.path.lexists(failure_path):
            failure, _raw = _load_phase(
                root, contract=contract, phase=f"failure-{role}", role=role)
            failures.append(f"{role}:{failure['evidence'].get('error')}")
    if missing or failures:
        return {
            "version": 1, "protocol": FINAL_PROTOCOL, "run_id": run_id,
            "contract_sha256": _hash_bytes(_canonical(contract)),
            "required_scope": required_scope,
            "verified_scope": None,
            "failure_model": "owner-process-sigkill",
            "observed_node_count": 0,
            "local_checks_passed": False,
            "two_node_verified": False,
            "infrastructure_fence_verified": False,
            "shared_fs_ready": False,
            "status": "failed" if failures else "incomplete",
            "missing": missing,
            "failures": failures,
            "phase_sha256": {},
        }

    holder_node = _load_node(root, role="holder", contract=contract)
    contender_node = _load_node(root, role="contender", contract=contract)
    phases: Dict[str, Mapping[str, Any]] = {}
    phase_hashes = {}
    for phase in _PHASES:
        expected_role = "holder" if phase in {
            "holder_lease", "crash_ready", "holder_complete"} else "contender"
        value, raw = _load_phase(
            root, contract=contract, phase=phase, role=expected_role)
        phases[phase] = value
        phase_hashes[phase] = _hash_bytes(raw)
    failures.extend(_phase_semantics(phases))
    fence_raw = _read_stable_regular(
        _state_path(root, "guardian-fence-observed.json"), max_bytes=4096)
    try:
        fence_observed = _strict_json(fence_raw, label="guardian fence observation")
    except (QualificationFirewallError, ValueError, json.JSONDecodeError) as error:
        raise SharedFSCanaryError("guardian fence observation 非法") from error
    expected_holder_hash = _hash_bytes(_canonical(phases["holder_complete"]))
    observed_owner = phases["holder_lease"]["evidence"].get("owner")
    expected_old_owner = (
        observed_owner.get("owner_id") if isinstance(observed_owner, dict) else None)
    if (fence_raw != _canonical(fence_observed)
            or fence_observed != {
                "version": 1,
                "run_id": run_id,
                "old_owner_id": expected_old_owner,
                "holder_complete_sha256": expected_holder_hash,
            }
            or _hash_bytes(fence_raw)
            != phases["contender_complete"]["evidence"].get(
                "fence_observed_sha256")):
        failures.append("guardian_fence_observation_mismatch")

    holder_identity = holder_node["identity"]
    contender_identity = contender_node["identity"]
    identity_pairs = {
        (holder_identity.get("machine_id"), holder_identity.get("boot_id")),
        (contender_identity.get("machine_id"), contender_identity.get("boot_id")),
    }
    observed_node_count = len(identity_pairs)
    two_distinct = (
        holder_identity.get("machine_id") != contender_identity.get("machine_id")
        and holder_identity.get("boot_id") != contender_identity.get("boot_id"))
    common_fields = (
        "mount", "python_version", "sqlite_version", "sqlite_source_id",
        "implementation_sha256", "required_journal_mode",
    )
    for field in common_fields:
        if holder_node.get(field) != contender_node.get(field):
            failures.append(f"node_{field}_mismatch")
    if (holder_identity.get("uid"), holder_identity.get("gid")) != (
            contender_identity.get("uid"), contender_identity.get("gid")):
        failures.append("node_service_identity_mismatch")
    mount = holder_node["mount"]
    gpfs_target = (
        mount.get("fstype") == "gpfs"
        and holder_node.get("required_journal_mode") == "delete")
    local_checks = not failures
    two_node_verified = bool(
        local_checks and two_distinct and gpfs_target
        and required_scope == TWO_NODE_SCOPE)
    if required_scope == LOCAL_SCOPE:
        passed = local_checks
        verified_scope = LOCAL_SCOPE if passed else None
        status_value = "passed" if passed else "failed"
    else:
        passed = two_node_verified
        verified_scope = TWO_NODE_SCOPE if passed else None
        status_value = "passed" if passed else ("failed" if failures else "incomplete")
        if not two_distinct:
            failures.append("distinct_machine_and_boot_not_observed")
        if not gpfs_target:
            failures.append("target_gpfs_delete_not_observed")
    result = {
        "version": 1,
        "protocol": FINAL_PROTOCOL,
        "run_id": run_id,
        "contract_sha256": _hash_bytes(_canonical(contract)),
        "required_scope": required_scope,
        "verified_scope": verified_scope,
        "failure_model": "owner-process-sigkill",
        "observed_node_count": observed_node_count,
        "local_checks_passed": local_checks,
        "two_node_verified": two_node_verified,
        "infrastructure_fence_verified": False,
        "shared_fs_ready": two_node_verified,
        "status": status_value,
        "missing": [],
        "failures": failures,
        "phase_sha256": phase_hashes,
    }
    if passed:
        published, _raw = _publish(final_path, result)
        if published != result:
            raise SharedFSCanaryError("shared-fs canary final 发布冲突")
        return published
    return result


def run_local_canary(
        *, canary_root: Path | str, run_id: str, timeout_s: float = 30.0,
        guardian_grace_s: float = 0.2) -> Dict[str, Any]:
    """Run the fixed two-process prerequisite without claiming two nodes."""
    run_id = _validate_run_id(run_id)
    timeout_s, guardian_grace_s = _validate_timing(timeout_s, guardian_grace_s)
    root = _canonical_root(canary_root, may_not_exist=True)
    _create_contract(
        root=root, run_id=run_id, scope=LOCAL_SCOPE,
        timeout_s=timeout_s, guardian_grace_s=guardian_grace_s)
    common = [
        "--canary-root", str(root), "--run-id", run_id,
        "--timeout-s", str(timeout_s),
        "--guardian-grace-s", str(guardian_grace_s),
        "--required-scope", LOCAL_SCOPE,
    ]
    processes: Dict[str, subprocess.Popen] = {}
    outputs: Dict[str, tuple[bytes, bytes]] = {}
    try:
        for role in ("holder", "contender"):
            processes[role] = subprocess.Popen(
                [sys.executable, "-m", "orchestrator.shared_fs_canary", "node",
                 "--role", role, *common],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, close_fds=True,
                start_new_session=True)
        for role, proc in processes.items():
            outputs[role] = proc.communicate(timeout=timeout_s + 10.0)
    except BaseException:
        for proc in processes.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for proc in processes.values():
            try:
                proc.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise
    holder, contender = processes["holder"], processes["contender"]
    holder_err = outputs["holder"][1]
    contender_err = outputs["contender"][1]
    if holder.returncode != 0 or contender.returncode != 0:
        raise SharedFSCanaryError(
            "local shared-fs canary role 失败: "
            f"holder={holder.returncode}:{holder_err[-500:].decode(errors='replace')} "
            f"contender={contender.returncode}:{contender_err[-500:].decode(errors='replace')}")
    result = verify_canary(
        canary_root=root, run_id=run_id, required_scope=LOCAL_SCOPE)
    if (result.get("status") != "passed"
            or result.get("local_checks_passed") is not True
            or result.get("two_node_verified") is not False):
        raise SharedFSCanaryError("local canary 不得升格为 two-node 结论")
    return result


def _main_result(result: Mapping[str, Any]) -> None:
    print(json.dumps(
        dict(result), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="meta-research one-shot shared filesystem canary")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("local", "node"):
        command = sub.add_parser(name)
        command.add_argument("--canary-root", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--timeout-s", type=float, default=(30.0 if name == "local" else 60.0))
        command.add_argument("--guardian-grace-s", type=float, default=(0.2 if name == "local" else 5.0))
        if name == "node":
            command.add_argument("--role", choices=("holder", "contender"), required=True)
            command.add_argument(
                "--required-scope", choices=(LOCAL_SCOPE, TWO_NODE_SCOPE),
                default=TWO_NODE_SCOPE)
    verify = sub.add_parser("verify")
    verify.add_argument("--canary-root", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument(
        "--required-scope", choices=(LOCAL_SCOPE, TWO_NODE_SCOPE),
        default=TWO_NODE_SCOPE)
    owner = sub.add_parser("_owner", help=argparse.SUPPRESS)
    owner.add_argument("--canary-root", required=True)
    owner.add_argument("--run-id", required=True)
    owner.add_argument("--wrapper-pid", type=int, required=True)
    owner.add_argument("--parent-fd", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "local":
            result = run_local_canary(
                canary_root=args.canary_root, run_id=args.run_id,
                timeout_s=args.timeout_s,
                guardian_grace_s=args.guardian_grace_s)
        elif args.command == "node":
            result = run_node_canary(
                canary_root=args.canary_root, run_id=args.run_id, role=args.role,
                timeout_s=args.timeout_s,
                guardian_grace_s=args.guardian_grace_s,
                required_scope=args.required_scope)
        elif args.command == "verify":
            result = verify_canary(
                canary_root=args.canary_root, run_id=args.run_id,
                required_scope=args.required_scope)
        else:
            root = _canonical_root(args.canary_root, may_not_exist=False)
            return _owner_process(
                root, run_id=_validate_run_id(args.run_id),
                wrapper_pid=args.wrapper_pid, parent_fd=args.parent_fd)
    except SharedFSCanaryError as error:
        print(json.dumps({
            "status": "unsafe", "error": str(error),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 3
    except Exception as error:
        print(json.dumps({
            "status": "unsafe", "error": _bounded_error(error),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 3
    _main_result(result)
    if args.command == "verify" and result.get("status") == "incomplete":
        return 2
    return 0 if result.get("status", "passed") == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LOCAL_SCOPE", "TWO_NODE_SCOPE", "SharedFSCanaryError",
    "run_local_canary", "run_node_canary", "verify_canary", "main",
]
