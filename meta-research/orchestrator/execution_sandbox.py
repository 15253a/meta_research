"""Pinned Docker execution sandbox for adversarial bundle/import workloads.

The ordinary :mod:`process_supervisor` proves a local descendant tree empty.
Container payloads live behind a daemon and are not descendants of the Docker
CLI, so a sandbox invocation also delegates an exact random container
name+label cleanup capability to that guardian.  The container sees only:

* a verified, copied input snapshot mounted read-only;
* one quarantine output directory mounted read-write;
* a read-only root filesystem and private PID/network namespaces; and
* a pinned image, no capabilities, no-new-privileges, an additive daemon
  filter, and a hash-pinned launcher-installed default-deny seccomp BPF.

Output is promoted from quarantine only after the guardian receipt proves both
the local process tree and exact container identity drained.  This module does
not claim cgroup enforcement when a rootless daemon reports no cgroup driver;
that case is explicitly ``rlimit-fallback`` and the trusted in-container
launcher applies hard per-process limits before exec.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

if __package__:
    from .artifact_capability import (
        ArtifactCapabilityError,
        normalize_sha256,
        open_artifact,
        read_artifact_bytes,
        verify_open_fd,
        verify_tree_fd,
    )
    from .process_supervisor import atomic_write_receipt, read_receipt
else:  # trusted isolated helper launched by the guardian
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.artifact_capability import (  # type: ignore[no-redef]
        ArtifactCapabilityError,
        normalize_sha256,
        open_artifact,
        read_artifact_bytes,
        verify_open_fd,
        verify_tree_fd,
    )
    from orchestrator.process_supervisor import (  # type: ignore[no-redef]
        atomic_write_receipt,
        read_receipt,
    )


_BACKEND = "docker-container-v1"
_LABEL = "meta-research.sandbox-token"
_IMAGE_RE = re.compile(
    r"(?:^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$|^sha256:[0-9a-f]{64}$)")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GPU_UUID_RE = re.compile(r"^GPU-[A-Za-z0-9-]{1,128}$")
_GPU_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
_GPU_COMPUTE_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}$")
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_MAX_ENGINE_OUTPUT = 4 * 1024 * 1024
_SESSION_VERSION = 1
_SAFE_PATH = re.compile(r"^[^\x00-\x1f\x7f\\]+$")

_RLIMIT_LAUNCHER = r"""
import base64,ctypes,json,os,resource,subprocess,sys
memory,nproc,nofile,fsize=map(int,sys.argv[1:5])
if memory < 0:
    resource.setrlimit(resource.RLIMIT_AS,(resource.RLIM_INFINITY,resource.RLIM_INFINITY))
else:
    resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
resource.setrlimit(resource.RLIMIT_NPROC,(nproc,nproc))
resource.setrlimit(resource.RLIMIT_NOFILE,(nofile,nofile))
resource.setrlimit(resource.RLIMIT_FSIZE,(fsize,fsize))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
gpu=json.loads(sys.argv[7])
if gpu is not None:
    try:
        result=subprocess.run([
            '/usr/bin/nvidia-smi','--query-gpu=uuid,name,memory.total,compute_cap,driver_version',
            '--format=csv,noheader,nounits'],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False,
            env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'})
        if result.returncode or len(result.stdout)+len(result.stderr)>1048576: raise ValueError()
        actual=[]
        for raw in result.stdout.decode('utf-8').splitlines():
            if not raw.strip(): continue
            parts=[part.strip() for part in raw.split(',')]
            if len(parts)!=5: raise ValueError()
            uuid,model,memory_mib,compute,driver=parts
            actual.append({'uuid':uuid,'model':model,'memory_bytes':int(memory_mib)*1048576,
                           'compute_capability':compute,'driver_version':driver})
        expected=[{**item,'driver_version':gpu['driver_version']} for item in gpu['devices']]
        if sorted(actual,key=lambda item:item['uuid'])!=sorted(expected,key=lambda item:item['uuid']):
            raise ValueError()
    except Exception:
        raise SystemExit(126)
class _Filter(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class _Program(ctypes.Structure):
    _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(_Filter))]
bpf=base64.b64decode(sys.argv[5],validate=True)
if not bpf or len(bpf)%ctypes.sizeof(_Filter): raise SystemExit(126)
filters=(_Filter*(len(bpf)//ctypes.sizeof(_Filter))).from_buffer_copy(bpf)
program=_Program(len(filters),filters)
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(38,1,0,0,0)!=0: raise SystemExit(126)
if libc.syscall(317,1,0,ctypes.byref(program))!=0: raise SystemExit(126)
payload_env=json.loads(sys.argv[6])
if not isinstance(payload_env,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in payload_env.items()):
    raise SystemExit(126)
os.umask(0o077)
os.chdir('/mr/output')
os.environ.clear()
os.environ.update(payload_env)
argv=sys.argv[9:]
if not argv: raise SystemExit(126)
os.execvp(argv[0],argv)
""".strip()

_SECCOMP_PROBE = r"""
import base64,ctypes,os,sys
class F(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class P(ctypes.Structure):
    _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(F))]
b=base64.b64decode(sys.argv[1],validate=True)
if not b or len(b)%ctypes.sizeof(F): raise SystemExit(126)
a=(F*(len(b)//ctypes.sizeof(F))).from_buffer_copy(b)
p=P(len(a),a)
c=ctypes.CDLL(None,use_errno=True)
if c.prctl(38,1,0,0,0)!=0: raise SystemExit(126)
if c.syscall(317,1,0,ctypes.byref(p))!=0: raise SystemExit(126)
os._exit(0)
""".strip()


class ExecutionSandboxError(RuntimeError):
    """The strong execution boundary cannot be established or verified."""


class SandboxOutputError(ExecutionSandboxError):
    """A drained exit produced an output quarantine that is unsafe to promote."""

    def __init__(self, message: str, *, receipt: Mapping[str, Any], receipt_path: Path):
        super().__init__(message)
        self.receipt = dict(receipt)
        self.receipt_path = Path(receipt_path)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def normalize_gpu_contract(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return one canonical fixed NVIDIA allocation or reject it.

    UUIDs are execution-allocation identity.  The derived capability projection
    deliberately omits UUIDs so an equivalent replacement card does not poison
    dependency-image caches, while every invocation spec still binds the exact
    physical allocation.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
            "version", "provider", "driver_version", "request", "devices"}:
        raise ValueError("sandbox GPU contract 字段闭包非法")
    if (value.get("version") != 1 or value.get("provider") != "nvidia"
            or not isinstance(value.get("driver_version"), str)
            or _GPU_VERSION_RE.fullmatch(value["driver_version"]) is None):
        raise ValueError("sandbox GPU contract provider/driver 非法")
    request = value.get("request")
    if (not isinstance(request, Mapping)
            or set(request) != {"driver", "capabilities", "options"}
            or request.get("driver") != "nvidia"
            or request.get("capabilities") != ["compute", "utility", "gpu"]
            or request.get("options") != {}):
        raise ValueError("sandbox GPU DeviceRequest contract 非法")
    devices = value.get("devices")
    if not isinstance(devices, list) or not 1 <= len(devices) <= 64:
        raise ValueError("sandbox GPU devices 须为有界非空数组")
    normalized = []
    uuids = set()
    for item in devices:
        if not isinstance(item, Mapping) or set(item) != {
                "uuid", "model", "memory_bytes", "compute_capability"}:
            raise ValueError("sandbox GPU device 字段闭包非法")
        uuid = item.get("uuid")
        model = item.get("model")
        memory = item.get("memory_bytes")
        compute = item.get("compute_capability")
        if (not isinstance(uuid, str) or _GPU_UUID_RE.fullmatch(uuid) is None
                or uuid in uuids or not isinstance(model, str) or not model
                or len(model.encode("utf-8")) > 256
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in model)
                or isinstance(memory, bool) or not isinstance(memory, int)
                or not 1 <= memory <= 9007199254740991
                or not isinstance(compute, str)
                or _GPU_COMPUTE_RE.fullmatch(compute) is None):
            raise ValueError("sandbox GPU device identity 非法")
        uuids.add(uuid)
        normalized.append({
            "uuid": uuid, "model": model, "memory_bytes": memory,
            "compute_capability": compute,
        })
    normalized.sort(key=lambda item: item["uuid"])
    return {
        "version": 1, "provider": "nvidia",
        "driver_version": value["driver_version"],
        "request": {
            "driver": "nvidia",
            "capabilities": ["compute", "utility", "gpu"],
            "options": {},
        },
        "devices": normalized,
    }


def gpu_capability_projection(contract: Mapping[str, Any]) -> Dict[str, Any]:
    value = normalize_gpu_contract(contract)
    assert value is not None
    devices = [{
        "model": item["model"], "memory_bytes": item["memory_bytes"],
        "compute_capability": item["compute_capability"],
    } for item in value["devices"]]
    devices.sort(key=lambda item: _canonical(item))
    return {
        "version": 1, "provider": value["provider"],
        "driver_version": value["driver_version"],
        "request": value["request"], "devices": devices,
    }


def gpu_contract_hash(contract: Mapping[str, Any]) -> str:
    value = normalize_gpu_contract(contract)
    assert value is not None
    return _sha(_canonical(value))


def gpu_cli_argument(contract: Mapping[str, Any]) -> str:
    value = normalize_gpu_contract(contract)
    assert value is not None
    uuids = ",".join(item["uuid"] for item in value["devices"])
    return f'"driver=nvidia","device={uuids}","capabilities=compute,utility"'


def sandbox_environment_hash(config: Mapping[str, Any]) -> str:
    """Deterministic declared runtime identity exposed to bundle workers/DB."""
    return _sha(_canonical(dict(config)))


def sandbox_workload_environment_hash(
        runtime_environment_hash: str, gpu_required: bool) -> str:
    """Bind scientific reuse identity to the invocation's GPU access mode.

    CPU keeps the historical runtime hash.  GPU adds a stable mode tag to the
    capability-level runtime hash, so CPU and GPU measurements can never
    satisfy each other's exact ``env_hash`` reuse query.  Physical UUIDs stay
    in invocation identity rather than this reusable environment identity.
    """
    if (not isinstance(runtime_environment_hash, str)
            or _SHA256_RE.fullmatch(runtime_environment_hash) is None):
        raise ValueError("sandbox runtime_environment_hash 非法")
    if not isinstance(gpu_required, bool):
        raise ValueError("sandbox gpu_required 须为 bool")
    if not gpu_required:
        return runtime_environment_hash
    return _sha(_canonical({
        "version": 1,
        "protocol": "sandbox-workload-environment-v1",
        "runtime_environment_hash": runtime_environment_hash,
        "gpu_required": True,
    }))


def _strict_json(raw: bytes) -> Dict[str, Any]:
    if len(raw) > _MAX_SPEC_BYTES:
        raise ExecutionSandboxError("sandbox spec 超过大小上限")

    def unique(pairs):  # noqa: ANN001
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"重复 JSON key: {key}")
            out[key] = value
        return out

    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"非有限 JSON number: {token}")))
    if not isinstance(value, dict):
        raise ExecutionSandboxError("sandbox spec 须为 object")
    return value


def _read_fd(fd: int, limit: int = _MAX_SPEC_BYTES) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ExecutionSandboxError("sandbox spec 超过大小上限")
    return b"".join(chunks)


def _safe_engine_env(host: str) -> Dict[str, str]:
    return {
        "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "DOCKER_HOST": host,
    }


def _trusted_engine_path(raw: str) -> str:
    resolved = os.path.realpath(raw)
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(resolved, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or not info.st_mode & 0o111):
            raise ExecutionSandboxError(
                "sandbox engine 须为 root/owner 持有、不可组/全局写的可执行常规文件")
    finally:
        os.close(fd)
    return resolved


def _engine(
        engine: str, host: str, args: Sequence[str], *, timeout: Optional[float] = 15.0,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE) -> subprocess.CompletedProcess:
    return subprocess.run(
        [engine, *args], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        timeout=timeout, env=_safe_engine_env(host), check=False)


def _bounded_text(result: subprocess.CompletedProcess, *, what: str) -> str:
    stdout = result.stdout if isinstance(result.stdout, bytes) else b""
    stderr = result.stderr if isinstance(result.stderr, bytes) else b""
    if len(stdout) + len(stderr) > _MAX_ENGINE_OUTPUT:
        raise ExecutionSandboxError(f"{what} 输出越界")
    if result.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise ExecutionSandboxError(f"{what} 失败: {detail}")
    try:
        return stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ExecutionSandboxError(f"{what} 输出非 UTF-8") from error


def _path_under(path: Path, root: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    base = Path(os.path.abspath(os.fspath(root)))
    try:
        if os.path.commonpath((str(absolute), str(base))) != str(base):
            raise ExecutionSandboxError(f"{label} 越出 sandbox work_root")
    except ValueError as error:
        raise ExecutionSandboxError(f"{label} 与 sandbox work_root 不同设备/语境") from error
    return absolute


def _mountinfo_unescape(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _daemon_bind_source_candidates(
        source: str, *, mountinfo_text: Optional[str] = None) -> set[str]:
    """Return exact host paths a trusted platform bindfs may expose to Docker.

    This deployment maps GPFS mounts into the rootless daemon as
    ``/bindfs-mapped/mnt/<filesystem>/<mount-root>/<relative>``.  Docker's
    inspect output therefore cannot equal the caller's visible path even
    though the daemon honored the exact bind request.  Derive that rewrite
    from the kernel mount table instead of accepting an arbitrary suffix.
    """
    expected = Path(source)
    candidates = {source}
    text = (mountinfo_text if mountinfo_text is not None
            else Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    matches = []
    for line in text.splitlines():
        fields = line.split(" ")
        try:
            separator = fields.index("-")
            mount_root = Path(_mountinfo_unescape(fields[3]))
            mountpoint = Path(_mountinfo_unescape(fields[4]))
            fs_type = fields[separator + 1]
            fs_source = _mountinfo_unescape(fields[separator + 2])
            relative = expected.relative_to(mountpoint)
        except (IndexError, ValueError):
            continue
        matches.append((len(mountpoint.parts), mount_root, mountpoint,
                        fs_type, fs_source, relative))
    if not matches:
        return candidates
    deepest = max(item[0] for item in matches)
    for _depth, mount_root, mountpoint, fs_type, fs_source, relative in matches:
        if _depth != deepest:
            continue
        if (fs_type == "overlay" and mountpoint == Path("/")
                and mount_root == Path("/")):
            candidates.add(str(Path("/bindfs-mapped/ebs/rootfs") / relative))
            continue
        if (fs_type != "gpfs" or not fs_source.startswith("fs_")
                or re.fullmatch(r"fs_[A-Za-z0-9._-]+", fs_source) is None
                or not mount_root.is_absolute()):
            continue
        mapped = (Path("/bindfs-mapped/mnt") / fs_source.removeprefix("fs_")
                  / mount_root.relative_to("/") / relative)
        candidates.add(str(mapped))
    return candidates


def _ensure_directory_tree(root: Path, path: Path, *, mode: int = 0o700) -> Path:
    """Create/open every component below an existing root without symlink traversal."""
    root = Path(os.path.abspath(os.fspath(root)))
    path = _path_under(path, root, label="sandbox directory")
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ExecutionSandboxError("sandbox work_root 须为非 symlink 目录")
    relative = path.relative_to(root)
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(root, flags)
    try:
        for part in relative.parts:
            try:
                os.mkdir(part, mode=mode, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as error:
                raise ExecutionSandboxError(
                    "sandbox directory component 是 symlink/非法目录") from error
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_fd)
                raise ExecutionSandboxError("sandbox directory component 非目录")
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)
    return path


def _safe_relpath(raw: str) -> str:
    if (not isinstance(raw, str) or not raw or raw.startswith("/")
            or _SAFE_PATH.fullmatch(raw) is None
            or len(raw.encode("utf-8")) > 2048 or len(raw.split("/")) > 32
            or any(part in ("", ".", "..") for part in raw.split("/"))):
        raise ExecutionSandboxError(f"sandbox 相对路径非法: {raw!r}")
    return raw


def _validate_log_name(raw: str) -> None:
    if (not isinstance(raw, str) or not raw or len(raw) > 128
            or raw in {".", ".."} or "/" in raw or "\\" in raw
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw)):
        raise ExecutionSandboxError("sandbox log_name 须为有界安全 basename")


def _load_session_index(
        index_path: Path, work_root: Path) -> tuple[Dict[str, Any], Path, Path]:
    """Load one central session index and validate its complete authority."""
    index = _strict_json(read_artifact_bytes(
        index_path, max_bytes=128 * 1024,
        label="sandbox session index"))
    required = {
        "version", "session_id", "meta_path", "staging_dir", "log_name",
        "context_hash", "name", "token", "spec_sha256",
    }
    session_id = index.get("session_id")
    token = index.get("token")
    if (set(index) != required or index.get("version") != 1
            or not isinstance(session_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", session_id) is None
            or not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None
            or index.get("name") != "mr-" + token
            or not isinstance(index.get("context_hash"), str)
            or _SHA256_RE.fullmatch(index["context_hash"]) is None
            or not isinstance(index.get("spec_sha256"), str)
            or _SHA256_RE.fullmatch(index["spec_sha256"]) is None):
        raise ExecutionSandboxError("sandbox session index 字段闭包/身份非法")
    _validate_log_name(index.get("log_name"))
    for field in ("staging_dir", "meta_path"):
        value = index.get(field)
        if (not isinstance(value, str) or not os.path.isabs(value)
                or os.path.normpath(value) != value):
            raise ExecutionSandboxError(f"sandbox session index {field} 非规范绝对路径")
    staging = _path_under(
        Path(index["staging_dir"]), work_root,
        label="sandbox indexed staging")
    meta_path = _path_under(
        Path(index["meta_path"]), work_root,
        label="sandbox indexed metadata")
    expected_index = (
        work_root / "state" / "sandbox" / "sessions" / f"{session_id}.json")
    if (index_path != expected_index
            or meta_path != staging / ".sandbox-meta" / f"{session_id}.json"):
        raise ExecutionSandboxError("sandbox session index path authority 非法")
    return index, staging, meta_path


def _replace_path_prefix(token: str, source: str, target: str) -> str:
    start = 0
    while True:
        index = token.find(source, start)
        if index < 0:
            return token
        before_ok = index == 0 or token[index - 1] == "="
        end = index + len(source)
        after_ok = end == len(token) or token[end] == "/"
        if before_ok and after_ok:
            token = token[:index] + target + token[end:]
            start = index + len(target)
        else:
            start = end


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        view = memoryview(payload)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError("sandbox metadata short write")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _copy_fd_to(
        fd: int, destination: Path, *, size: int,
        progress_guard: Optional[Callable[[], None]] = None) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o444)
    digest = hashlib.sha256()
    copied = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while copied < size:
            if progress_guard is not None:
                progress_guard()
            chunk = os.read(fd, min(1024 * 1024, size - copied))
            if not chunk:
                raise ExecutionSandboxError("sandbox input snapshot 读取被截断")
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                n = os.write(out_fd, view)
                if n <= 0:
                    raise OSError("sandbox input snapshot short write")
                view = view[n:]
        if os.read(fd, 1):
            raise ExecutionSandboxError("sandbox input snapshot 权威 size 之后仍有数据")
        os.fsync(out_fd)
    finally:
        os.close(out_fd)
        os.lseek(fd, 0, os.SEEK_SET)
    return "sha256:" + digest.hexdigest()


def sandbox_session_id(log_name: str, context: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({
        "log_name": log_name, "context": dict(context)})).hexdigest()[:32]


@dataclass
class SandboxInvocation:
    argv: list[str]
    env: Dict[str, str]
    pass_fds: tuple[int, ...]
    external_container: Dict[str, Any]
    spec_file: Any
    staging_dir: Path
    log_name: str
    context: Dict[str, Any]

    def close(self) -> None:
        if self.spec_file is not None:
            self.spec_file.close()
            self.spec_file = None


class DockerExecutionSandbox:
    """Create one pinned, no-network Docker invocation and trusted snapshots."""

    def __init__(self, *, work_root: Path | str, config: Mapping[str, Any],
                 owner_guard=None, system_root: Path | str | None = None,
                 gpu_contract: Optional[Mapping[str, Any]] = None):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.system_root = Path(os.path.abspath(os.fspath(
            system_root if system_root is not None else Path(__file__).resolve().parent.parent)))
        self.config = dict(config)
        self.gpu_contract = normalize_gpu_contract(gpu_contract)
        supplied_capability = self.config.get("gpu_capability")
        if self.gpu_contract is None:
            if supplied_capability is not None:
                raise ValueError(
                    "sandbox.gpu_capability 不得脱离 exact GPU contract 单独注入")
        else:
            capability = gpu_capability_projection(self.gpu_contract)
            if supplied_capability is not None and supplied_capability != capability:
                raise ValueError("sandbox.gpu_capability 与 exact GPU contract 不一致")
            self.config["gpu_capability"] = capability
        self.owner_guard = owner_guard or (lambda: None)
        self._preflight_done = False
        self._resource_mode: Optional[str] = None
        self.image_environment: Dict[str, str] = {}
        self.engine_path = ""
        self.seccomp_path = Path()
        self.seccomp_spec_hash = ""
        self.seccomp_bpf_b64 = ""
        self._validate_config()

    def _validate_config(self) -> None:
        required = {
            "backend", "engine_path", "engine_host", "resource_mode",
            "seccomp_profile", "seccomp_sha256", "seccomp_bpf",
            "seccomp_bpf_sha256", "seccomp_bpf_arch", "image", "image_id",
            "python_path", "memory_mb", "pids", "nofile", "max_log_mb", "max_file_mb",
            "max_output_mb", "max_output_files", "cpus", "tmpfs_mb",
            "shm_mb", "input_max_mb", "readonly_mounts", "payload_environment",
        }
        allowed = required | ({"gpu_capability"} if self.gpu_contract is not None else set())
        if set(self.config) != allowed or self.config.get("backend") != "docker":
            raise ValueError("policy.execution.sandbox 字段闭包/backend 非法")
        engine = self.config["engine_path"]
        host = self.config["engine_host"]
        if (not isinstance(engine, str) or not os.path.isabs(engine)
                or os.path.normpath(engine) != engine):
            raise ValueError("sandbox.engine_path 须为规范绝对路径")
        self.engine_path = _trusted_engine_path(engine)
        if (not isinstance(host, str) or not host.startswith("unix:///")
                or os.path.normpath(host.removeprefix("unix://"))
                != host.removeprefix("unix://")):
            raise ValueError("sandbox.engine_host 只接受规范绝对 unix socket")
        if self.config["resource_mode"] not in {
                "cgroup-v1", "cgroup-v2", "rlimit-fallback"}:
            raise ValueError("sandbox.resource_mode 非法")
        profile_rel = self.config["seccomp_profile"]
        if (not isinstance(profile_rel, str) or Path(profile_rel).is_absolute()
                or _safe_relpath(profile_rel) != profile_rel):
            raise ValueError("sandbox.seccomp_profile 须为 system_root 内安全相对路径")
        expected_profile_hash = self.config["seccomp_sha256"]
        if (not isinstance(expected_profile_hash, str)
                or _SHA256_RE.fullmatch(expected_profile_hash) is None):
            raise ValueError("sandbox.seccomp_sha256 非法")
        self.seccomp_path = _path_under(
            self.system_root / profile_rel, self.system_root,
            label="sandbox seccomp profile")
        if os.path.realpath(self.seccomp_path) != str(self.seccomp_path):
            raise ValueError("sandbox.seccomp_profile 路径不得含 symlink")
        profile_info = os.lstat(self.seccomp_path)
        if (not stat.S_ISREG(profile_info.st_mode) or profile_info.st_uid not in {0, os.geteuid()}
                or profile_info.st_mode & 0o022 or profile_info.st_size > 1024 * 1024):
            raise ValueError("sandbox.seccomp_profile 身份/权限/大小非法")
        profile_raw = read_artifact_bytes(
            self.seccomp_path, expected_hash=expected_profile_hash,
            expected_size=profile_info.st_size, max_bytes=1024 * 1024,
            label="pinned seccomp profile")
        profile = _strict_json(profile_raw)
        if profile_raw != _canonical(profile) or profile.get("defaultAction") != "SCMP_ACT_ERRNO":
            raise ValueError("sandbox.seccomp_profile 须为 canonical default-deny JSON")
        self.seccomp_spec_hash = _sha(_canonical(profile))
        if self.config["seccomp_bpf_arch"] != "amd64":
            raise ValueError("sandbox.seccomp_bpf_arch 当前只支持 amd64")
        bpf_rel = self.config["seccomp_bpf"]
        if (not isinstance(bpf_rel, str) or Path(bpf_rel).is_absolute()
                or _safe_relpath(bpf_rel) != bpf_rel):
            raise ValueError("sandbox.seccomp_bpf 须为 system_root 内安全相对路径")
        expected_bpf_hash = self.config["seccomp_bpf_sha256"]
        if (not isinstance(expected_bpf_hash, str)
                or _SHA256_RE.fullmatch(expected_bpf_hash) is None):
            raise ValueError("sandbox.seccomp_bpf_sha256 非法")
        bpf_path = _path_under(
            self.system_root / bpf_rel, self.system_root,
            label="sandbox seccomp BPF")
        if os.path.realpath(bpf_path) != str(bpf_path):
            raise ValueError("sandbox.seccomp_bpf 路径不得含 symlink")
        bpf_info = os.lstat(bpf_path)
        if (not stat.S_ISREG(bpf_info.st_mode) or bpf_info.st_uid not in {0, os.geteuid()}
                or bpf_info.st_mode & 0o022 or bpf_info.st_size > 128 * 1024):
            raise ValueError("sandbox.seccomp_bpf 身份/权限/大小非法")
        bpf_text = read_artifact_bytes(
            bpf_path, expected_size=bpf_info.st_size, max_bytes=128 * 1024,
            label="pinned seccomp BPF base64")
        encoded = bpf_text.rstrip(b"\n")
        if bpf_text != encoded + b"\n":
            raise ValueError("sandbox.seccomp_bpf 须为单行 canonical base64")
        try:
            bpf = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("sandbox.seccomp_bpf base64 非法") from error
        if (not bpf or len(bpf) % 8 != 0 or len(bpf) > 64 * 1024
                or _sha(bpf) != expected_bpf_hash
                or base64.b64encode(bpf) != encoded):
            raise ValueError("sandbox.seccomp_bpf binary identity 非法")
        self.seccomp_bpf_b64 = encoded.decode("ascii")
        if (not isinstance(self.config["image"], str)
                or _IMAGE_RE.fullmatch(self.config["image"]) is None
                or not isinstance(self.config["image_id"], str)
                or _IMAGE_ID_RE.fullmatch(self.config["image_id"]) is None):
            raise ValueError("sandbox image/image_id 必须 exact sha256 pin")
        python_path = self.config["python_path"]
        if (not isinstance(python_path, str) or not python_path.startswith("/")
                or os.path.normpath(python_path) != python_path):
            raise ValueError("sandbox.python_path 须为容器内规范绝对路径")
        for key in ("memory_mb", "pids", "nofile", "max_log_mb", "max_file_mb",
                    "max_output_mb", "max_output_files", "tmpfs_mb", "shm_mb",
                    "input_max_mb"):
            value = self.config[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"sandbox.{key} 须为正整数")
        cpus = self.config["cpus"]
        if (isinstance(cpus, bool) or not isinstance(cpus, (int, float))
                or not math.isfinite(float(cpus)) or cpus <= 0):
            raise ValueError("sandbox.cpus 须为正有限数")
        mounts = self.config["readonly_mounts"]
        if not isinstance(mounts, list) or any(
                not isinstance(item, str) or not item.startswith("/")
                or os.path.normpath(item) != item or "," in item or ":" in item
                for item in mounts):
            raise ValueError("sandbox.readonly_mounts 须为规范绝对路径数组（禁逗号/冒号）")
        for raw in mounts:
            try:
                common = os.path.commonpath((raw, str(self.work_root)))
            except ValueError:
                continue
            if common in {raw, str(self.work_root)}:
                raise ValueError(
                    "sandbox.readonly_mounts 不得是 work_root 的祖先、同目录或子树")
        payload_environment = self.config["payload_environment"]
        if (not isinstance(payload_environment, dict)
                or len(payload_environment) > 32
                or any(not isinstance(key, str) or not key or "=" in key
                       or "\x00" in key or len(key.encode("utf-8")) > 256
                       or not isinstance(value, str) or "\x00" in value
                       or len(value.encode("utf-8")) > 65536
                       for key, value in payload_environment.items())):
            raise ValueError("sandbox.payload_environment 须为有界字符串映射")

    @property
    def resource_mode(self) -> str:
        self.preflight()
        assert self._resource_mode is not None
        return self._resource_mode

    @property
    def environment_hash(self) -> str:
        return sandbox_environment_hash(self.config)

    def workload_environment_hash(self, gpu_required: bool) -> str:
        return sandbox_workload_environment_hash(
            self.environment_hash, gpu_required)

    @property
    def gpu_contract_hash(self) -> Optional[str]:
        return (None if self.gpu_contract is None
                else gpu_contract_hash(self.gpu_contract))

    @property
    def runtime_identity_hash(self) -> str:
        return _sha(_canonical({
            "environment_hash": self.environment_hash,
            "gpu_contract": self.gpu_contract,
        }))

    def preflight(self) -> None:
        if self._preflight_done:
            return
        self.owner_guard()
        engine, host = self.engine_path, self.config["engine_host"]
        info = _bounded_text(_engine(
            engine, host, ["info", "--format",
                           "{{.CgroupVersion}}|{{.CgroupDriver}}|{{json .SecurityOptions}}"]),
            what="docker info")
        try:
            cgroup_version, driver, security_json = info.split("|", 2)
            security = json.loads(security_json)
        except (ValueError, json.JSONDecodeError) as error:
            raise ExecutionSandboxError("docker info 能力输出不可解析") from error
        if (not isinstance(security, list)
                or not any("seccomp" in str(item) for item in security)):
            raise ExecutionSandboxError(
                "docker daemon 未声明 seccomp capability，拒绝 adversarial sandbox")
        if driver == "none":
            self._resource_mode = "rlimit-fallback"
        elif cgroup_version == "2":
            self._resource_mode = "cgroup-v2"
        elif cgroup_version == "1":
            self._resource_mode = "cgroup-v1"
        else:
            raise ExecutionSandboxError("docker cgroup capability 非法")
        if self._resource_mode != self.config["resource_mode"]:
            raise ExecutionSandboxError(
                "docker 实测 resource_mode 与 policy pin 不一致；须显式更新 policy/env_hash")
        image = _bounded_text(_engine(
            engine, host, ["image", "inspect", self.config["image"],
                           "--format",
                           "{{.Id}}|{{.Os}}|{{.Architecture}}|{{json .Config.Env}}"]),
            what="pinned sandbox image inspect")
        try:
            image_id, os_name, arch, image_env_json = image.split("|", 3)
            image_env_items = json.loads(image_env_json)
        except (ValueError, json.JSONDecodeError) as error:
            raise ExecutionSandboxError("sandbox image inspect 输出不可解析") from error
        host_arch = {
            "x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm",
        }.get(os.uname().machine, os.uname().machine)
        if (image_id != self.config["image_id"] or os_name != "linux"
                or arch != host_arch or arch != self.config["seccomp_bpf_arch"]):
            raise ExecutionSandboxError("本地 sandbox image 与 policy exact pin/platform 不一致")
        if (not isinstance(image_env_items, list)
                or any(not isinstance(item, str) or "=" not in item
                       or "\x00" in item for item in image_env_items)):
            raise ExecutionSandboxError("sandbox image Config.Env 非法")
        image_environment: Dict[str, str] = {}
        for item in image_env_items:
            key, value = item.split("=", 1)
            if not key or key in image_environment:
                raise ExecutionSandboxError(
                    "sandbox image Config.Env 空 key/重复 key")
            image_environment[key] = value
        self.image_environment = image_environment
        probe = subprocess.run(
            [sys.executable, "-I", "-c", _SECCOMP_PROBE, self.seccomp_bpf_b64],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5.0, check=False,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        if probe.returncode != 0 or len(probe.stdout) + len(probe.stderr) > 4096:
            raise ExecutionSandboxError(
                "host kernel 无法加载 policy pinned seccomp BPF，拒绝 adversarial sandbox")
        self._preflight_done = True

    def run_gpu_canary(self, *, execution_supervisor, candidate_hash: str,
                       clock: Optional[Callable[[], float]] = None) -> Dict[str, Any]:
        """Run one guardian-owned exact DeviceRequest + in-container inventory probe."""
        if self.gpu_contract is None:
            raise ExecutionSandboxError("GPU canary 缺 exact allocation contract")
        if (not isinstance(candidate_hash, str)
                or _SHA256_RE.fullmatch(candidate_hash) is None):
            raise ValueError("GPU canary candidate_hash 非法")
        from . import harness as H

        token = candidate_hash.removeprefix("sha256:")[:32]
        staging = self.work_root / "state" / "deployment" / "gpu-canary" / token
        log_name = "gpu-canary.log"
        canary_context = {
            "phase": "deployment-gpu-canary",
            "candidate_hash": candidate_hash,
            "runtime_identity_hash": self.runtime_identity_hash,
            "log_name": log_name,
        }
        invocation = self.prepare(
            ["/usr/bin/nvidia-smi",
             "--query-gpu=uuid,name,memory.total,compute_cap,driver_version",
             "--format=csv,noheader,nounits"],
            staging_dir=staging, log_name=log_name, env=None, timeout_s=30.0,
            execution_context=canary_context,
            execution_supervisor=execution_supervisor, gpu_required=True)
        sandbox_spec_hash = invocation.external_container["spec_sha256"]
        result = H.run_staged(
            invocation.argv, staging_dir=str(staging), log_name=log_name,
            timeout_s=30.0, env=invocation.env, pass_fds=invocation.pass_fds,
            execution_supervisor=execution_supervisor,
            execution_kind="deployment-gpu-canary",
            execution_context=canary_context,
            sandbox_invocation=invocation)
        inventory: list[Dict[str, Any]] = []
        error = None
        driver = None
        try:
            raw = read_artifact_bytes(
                Path(result["log_path"]), max_bytes=1024 * 1024,
                label="GPU canary inventory")
            if result["exit_code"] != 0:
                detail = raw.decode("utf-8", errors="replace").strip()[:500]
                raise ValueError(
                    f"GPU canary runner exit_code={result['exit_code']}: {detail}")
            drivers = set()
            for raw_line in raw.decode("utf-8").splitlines():
                if not raw_line.strip():
                    continue
                parts = [part.strip() for part in raw_line.split(",")]
                if len(parts) != 5:
                    raise ValueError("GPU canary inventory 行字段数非法")
                uuid, model, memory_mib, compute, row_driver = parts
                memory = int(memory_mib) * 1024 * 1024
                item = normalize_gpu_contract({
                    "version": 1, "provider": "nvidia",
                    "driver_version": row_driver,
                    "request": {
                        "driver": "nvidia",
                        "capabilities": ["compute", "utility", "gpu"],
                        "options": {},
                    },
                    "devices": [{
                        "uuid": uuid, "model": model, "memory_bytes": memory,
                        "compute_capability": compute,
                    }],
                })
                assert item is not None
                inventory.append(item["devices"][0])
                drivers.add(row_driver)
            if len(drivers) != 1:
                raise ValueError("GPU canary driver version 非唯一")
            driver = next(iter(drivers))
            inventory.sort(key=lambda item: item["uuid"])
            if (driver != self.gpu_contract["driver_version"]
                    or inventory != self.gpu_contract["devices"]):
                raise ValueError("GPU canary container inventory 与 exact contract 不一致")
        except Exception as caught:
            error = str(caught).replace("\x00", "?").replace("\n", " ")[:1000]
        receipt = result["process_receipt"]
        sandbox_receipt = receipt.get("sandbox") if isinstance(receipt, Mapping) else None
        if (not isinstance(sandbox_receipt, Mapping)
                or sandbox_receipt.get("spec_sha256") != sandbox_spec_hash
                or sandbox_receipt.get("container_drained") is not True):
            raise ExecutionSandboxError(
                "GPU canary guardian receipt 未绑定并清空 exact container")
        checked = (clock or time.time)()
        if (isinstance(checked, bool) or not isinstance(checked, (int, float))
                or not math.isfinite(float(checked)) or checked <= 0):
            raise ValueError("GPU canary clock 须为正有限 Unix time")
        ok = error is None
        guardian_projection = {
            "operation_id": receipt.get("operation_id"),
            "state": receipt.get("state"), "outcome": receipt.get("outcome"),
            "returncode": receipt.get("returncode"),
            "group_drained": receipt.get("group_drained"),
            "containment": receipt.get("containment"),
            "context": dict(receipt.get("context") or {}),
            "sandbox": {
                "backend": sandbox_receipt.get("backend"),
                "spec_sha256": sandbox_receipt.get("spec_sha256"),
                "container_drained": sandbox_receipt.get("container_drained"),
            },
        }
        return {
            "version": 1, "protocol": "sandbox-gpu-canary-v1", "ok": ok,
            "checked_at_unix": float(checked),
            "candidate_hash": candidate_hash,
            "contract_hash": self.gpu_contract_hash,
            "environment_hash": self.environment_hash,
            "runtime_identity_hash": self.runtime_identity_hash,
            "inventory": inventory,
            "guardian": guardian_projection,
            "execution": {
                "log_path": result["log_path"],
                "log_sha256": "sha256:" + result["log_sha256"],
                "process_receipt_path": result["process_receipt_path"],
                "operation_id": receipt["operation_id"],
                "sandbox_spec_sha256": sandbox_spec_hash,
            },
            "error": error,
        }

    def _snapshot_inputs(
            self, *, token: str,
            fd_expectations: Sequence[tuple[int, str, int, Optional[int], Optional[int]]],
            tree_expectations: Sequence[tuple[int, Dict[str, str], tuple[str, ...]]]
    ) -> tuple[Path, Dict[str, str], str]:
        root = self.work_root / "state" / "sandbox" / "inputs" / token
        if os.path.lexists(root):
            raise ExecutionSandboxError("sandbox input token 已存在")
        _ensure_directory_tree(self.work_root, root, mode=0o700)
        replacements: Dict[str, str] = {}
        ledger = []
        total = 0
        index = 0
        maximum = self.config["input_max_mb"] * 1024 * 1024
        try:
            for fd, expected_hash, size, device, inode in fd_expectations:
                identity = verify_open_fd(
                    fd, expected_hash=expected_hash, expected_size=size,
                    expected_device=device, expected_inode=inode,
                    progress_guard=self.owner_guard)
                if total + identity.size_bytes > maximum:
                    raise ExecutionSandboxError(
                        "sandbox verified input snapshot 超 policy 上限")
                destination = root / f"fd-{index}"
                actual = _copy_fd_to(
                    fd, destination, size=identity.size_bytes,
                    progress_guard=self.owner_guard)
                if actual != normalize_sha256(expected_hash):
                    raise ExecutionSandboxError("sandbox file snapshot hash 漂移")
                replacements[f"/proc/self/fd/{fd}"] = f"/mr/input/fd-{index}"
                ledger.append({"path": f"fd-{index}", "sha256": actual,
                               "bytes": identity.size_bytes})
                total += identity.size_bytes
                index += 1
            for fd, hashes, allowed_extra in tree_expectations:
                verify_tree_fd(
                    fd, hashes, label="sandbox source snapshot", exact=True,
                    allowed_extra=allowed_extra,
                    progress_guard=self.owner_guard)
                base = root / f"fd-{index}"
                base.mkdir(mode=0o700)
                for rel, expected_hash in sorted(hashes.items()):
                    rel = _safe_relpath(rel)
                    with open_artifact(
                            Path(f"/proc/self/fd/{fd}") / rel,
                            expected_hash=expected_hash,
                            label=f"sandbox source:{rel}",
                            progress_guard=self.owner_guard) as capability:
                        if total + capability.identity.size_bytes > maximum:
                            raise ExecutionSandboxError(
                                "sandbox verified input snapshot 超 policy 上限")
                        destination = base / rel
                        actual = _copy_fd_to(
                            capability.fd, destination,
                            size=capability.identity.size_bytes,
                            progress_guard=self.owner_guard)
                        if actual != normalize_sha256(expected_hash):
                            raise ExecutionSandboxError("sandbox tree snapshot hash 漂移")
                        ledger.append({"path": f"fd-{index}/{rel}", "sha256": actual,
                                       "bytes": capability.identity.size_bytes})
                        total += capability.identity.size_bytes
                replacements[f"/proc/self/fd/{fd}"] = f"/mr/input/fd-{index}"
                index += 1
            for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
                for name in files:
                    os.chmod(Path(current) / name, 0o444, follow_symlinks=False)
                for name in dirs:
                    os.chmod(Path(current) / name, 0o555, follow_symlinks=False)
                os.chmod(Path(current), 0o555, follow_symlinks=False)
                _fsync_dir(Path(current))
            _fsync_dir(root.parent)
            manifest_hash = _sha(_canonical({"files": ledger, "total_bytes": total}))
            return root, replacements, manifest_hash
        except BaseException:
            try:
                _safe_remove_tree(root, parent=root.parent)
            except OSError:
                pass
            raise

    def recover_unstarted_session(
            self, *, staging_dir: Path | str, log_name: str,
            execution_context: Mapping[str, Any], execution_supervisor,
            partial_path: Optional[Path] = None) -> bool:
        """Remove a prepare-only session after proving no guardian could start.

        ExecutionSupervisor durably writes its prepared receipt *before* the
        ambiguous Popen boundary.  Under the recovered instance fence, absence
        of an exact owner receipt therefore proves the wrapper/container never
        started.  This closes the kill window between input snapshot creation
        and ``supervisor.run()`` without weakening the ordinary "partial with
        no receipt" fail-closed rule.
        """
        if execution_supervisor is None:
            return False
        self.owner_guard()
        execution_supervisor.recover_previous_generation()
        context = dict(execution_context)
        if "log_name" in context and context["log_name"] != log_name:
            raise ExecutionSandboxError("sandbox recovery context/log_name 冲突")
        context["log_name"] = log_name
        owner_kind = context.get("db_owner_kind")
        owner_id = context.get("db_owner_id")
        if (not isinstance(owner_kind, str) or isinstance(owner_id, bool)
                or not isinstance(owner_id, int) or owner_id <= 0):
            raise ExecutionSandboxError(
                "sandbox prepare recovery 要求 exact DB owner context")
        for receipt_path in sorted(execution_supervisor.receipt_dir.glob("execution-*.json")):
            receipt = read_receipt(receipt_path)
            receipt_context = receipt.get("context") or {}
            if (receipt_context.get("db_owner_kind"), receipt_context.get("db_owner_id")) != (
                    owner_kind, owner_id):
                continue
            if receipt_context != context:
                raise ExecutionSandboxError(
                    "sandbox prepare recovery 发现同 owner 的错配 guardian receipt")
            return False

        staging = _path_under(Path(staging_dir), self.work_root, label="sandbox staging")
        session_id = sandbox_session_id(log_name, context)
        meta_path = staging / ".sandbox-meta" / f"{session_id}.json"
        output_root = staging / ".sandbox-output" / session_id
        input_root = self.work_root / "state" / "sandbox" / "inputs" / session_id
        index_path = self.work_root / "state" / "sandbox" / "sessions" / f"{session_id}.json"
        if not any(os.path.lexists(path) for path in (
                meta_path, output_root, input_root, index_path)):
            return False

        index = None
        if os.path.lexists(index_path):
            index, indexed_staging, indexed_meta = _load_session_index(
                index_path, self.work_root)
            if (indexed_staging != staging or indexed_meta != meta_path
                    or index.get("log_name") != log_name
                    or index.get("context_hash") != _sha(_canonical(context))):
                raise ExecutionSandboxError(
                    "sandbox unstarted session index/context authority 不一致")

        meta = None
        if os.path.lexists(meta_path):
            loaded_path, meta = _load_session(staging, log_name, context)
            if loaded_path != meta_path or (meta.get("output_root"), meta.get("input_root")) != (
                    str(output_root), str(input_root)):
                raise ExecutionSandboxError("sandbox unstarted session path authority 不一致")
            if meta.get("index_path") != str(index_path):
                raise ExecutionSandboxError("sandbox unstarted session index authority 不一致")
            if (not isinstance(meta.get("token"), str)
                    or _TOKEN_RE.fullmatch(meta["token"]) is None
                    or meta.get("name") != "mr-" + meta["token"]
                    or not isinstance(meta.get("spec_sha256"), str)
                    or _SHA256_RE.fullmatch(meta["spec_sha256"]) is None):
                raise ExecutionSandboxError("sandbox unstarted session name/token 非法")
            if (index is not None and any(meta.get(key) != index.get(key) for key in (
                    "session_id", "log_name", "context_hash", "name", "token",
                    "spec_sha256"))):
                raise ExecutionSandboxError(
                    "sandbox unstarted session index/metadata 不一致")
        authority = meta if meta is not None else index
        if authority is not None:
            listed = _bounded_text(_engine(
                self.engine_path, self.config["engine_host"], [
                    "container", "ls", "--all",
                    "--filter", f"name=^/{authority['name']}$",
                    "--format", "{{.Names}}|{{.Label \"" + _LABEL + "\"}}",
                ], timeout=5.0), what="sandbox unstarted container absence probe")
            if listed:
                raise ExecutionSandboxError(
                    "sandbox 无 guardian receipt 但 exact container name 已存在；拒绝清理")
        for suffix in (".promotion-plan.json", ".promoted.json", ".rejected.json"):
            if os.path.lexists(meta_path.with_suffix(suffix)):
                raise ExecutionSandboxError(
                    "sandbox prepare-only session 不得已有 promotion/rejection authority")
        if meta is None and partial_path is not None and os.path.lexists(partial_path):
            raise ExecutionSandboxError(
                "sandbox partial 存在但 prepare metadata 缺失；拒绝推断未启动")

        partial = partial_path or staging / (log_name + ".partial")
        if os.path.lexists(partial):
            info = os.lstat(partial)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise ExecutionSandboxError("sandbox unstarted partial 身份非法")
            partial.unlink()
            _fsync_dir(partial.parent)
        if os.path.lexists(output_root):
            _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
        if os.path.lexists(input_root):
            _safe_remove_tree(input_root, parent=input_root.parent)
        if os.path.lexists(meta_path):
            info = os.lstat(meta_path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ExecutionSandboxError("sandbox unstarted metadata 身份非法")
            meta_path.unlink()
            _fsync_dir(meta_path.parent)
        if os.path.lexists(index_path):
            info = os.lstat(index_path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ExecutionSandboxError("sandbox unstarted index 身份非法")
            index_path.unlink()
            _fsync_dir(index_path.parent)
        return True

    def recover_terminal_sessions(self, execution_supervisor) -> int:
        """Discard private inputs/quarantines for every drained non-exit receipt.

        Startup DB reconciliation may terminalize the DB owner before its stage
        runs again, so cleanup cannot rely only on ``recover_staged_result``.
        Session metadata is matched to the central receipt by context hash and
        exact random container authority; ordinary exit receipts remain for
        DB-stage artifact replay, while the DB-less deployment GPU canary is
        completed here because a new owner will never revisit its candidate path.
        """
        self.owner_guard()
        execution_supervisor.recover_previous_generation()
        receipts_by_identity: Dict[tuple[Any, Any, Any], list[Dict[str, Any]]] = {}
        for path in sorted(execution_supervisor.receipt_dir.glob("execution-*.json")):
            receipt = read_receipt(path)
            if receipt.get("containment") != _BACKEND:
                continue
            if (receipt.get("state") != "terminal"
                    or receipt.get("group_drained") is not True
                    or not isinstance(receipt.get("sandbox"), dict)
                    or receipt["sandbox"].get("container_drained") is not True):
                raise ExecutionSandboxError(
                    f"sandbox startup receipt 未 terminal+drained: {path.name}")
            sandbox = receipt["sandbox"]
            identity = (
                sandbox.get("container_name"), sandbox.get("token"),
                sandbox.get("spec_sha256"))
            receipts_by_identity.setdefault(identity, []).append(receipt)

        recovered = 0
        index_dir = self.work_root / "state" / "sandbox" / "sessions"
        indexes = sorted(index_dir.glob("*.json")) if index_dir.exists() else []
        if len(indexes) > 100000:
            raise ExecutionSandboxError("sandbox startup session 数量超安全上限")
        for index_path in indexes:
            index, staging, meta_path = _load_session_index(
                index_path, self.work_root)
            session_id = index["session_id"]
            output_root = staging / ".sandbox-output" / session_id
            input_root = self.work_root / "state" / "sandbox" / "inputs" / session_id
            meta = None
            if os.path.lexists(meta_path):
                meta = _strict_json(read_artifact_bytes(
                    meta_path, max_bytes=128 * 1024,
                    label="sandbox startup session metadata"))
                if (meta.get("index_path") != str(index_path)
                        or any(meta.get(key) != index.get(key) for key in (
                            "session_id", "log_name", "context_hash", "name", "token",
                            "spec_sha256"))):
                    raise ExecutionSandboxError("sandbox startup index/metadata 不一致")
                if (meta.get("output_root"), meta.get("input_root")) != (
                        str(output_root), str(input_root)):
                    raise ExecutionSandboxError("sandbox startup session path authority 非法")
            identity = (index["name"], index["token"], index["spec_sha256"])
            identity_candidates = receipts_by_identity.get(identity, [])
            if len(identity_candidates) > 1:
                raise ExecutionSandboxError(
                    f"sandbox session {index_path.name} 对应多个 terminal receipt")
            candidate = identity_candidates[0] if identity_candidates else None
            if (candidate is not None
                    and index["context_hash"] != _sha(
                        _canonical(candidate.get("context") or {}))):
                raise ExecutionSandboxError(
                    f"sandbox session {index_path.name} receipt context hash 错配")
            if candidate is not None and meta is None:
                raise ExecutionSandboxError(
                    f"sandbox session {index_path.name} 已有 guardian receipt 但 metadata 缺失")
            if candidate is None:
                listed = _bounded_text(_engine(
                    self.engine_path, self.config["engine_host"], [
                        "container", "ls", "--all",
                        "--filter", f"name=^/{index['name']}$",
                        "--format", "{{.Names}}|{{.Label \"" + _LABEL + "\"}}",
                    ], timeout=5.0), what="sandbox startup unstarted absence probe")
                if listed:
                    raise ExecutionSandboxError(
                        "sandbox index 无 guardian receipt 但 exact container 已存在")
                for suffix in (".promotion-plan.json", ".promoted.json", ".rejected.json"):
                    if os.path.lexists(meta_path.with_suffix(suffix)):
                        raise ExecutionSandboxError(
                            "sandbox unstarted index 不得已有 promotion/rejection authority")
                partial = staging / (index["log_name"] + ".partial")
                if os.path.lexists(partial):
                    info = os.lstat(partial)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.geteuid()):
                        raise ExecutionSandboxError("sandbox startup unstarted partial 身份非法")
                    partial.unlink()
                    _fsync_dir(partial.parent)
                if os.path.lexists(output_root):
                    _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
                if os.path.lexists(input_root):
                    _safe_remove_tree(input_root, parent=input_root.parent)
                if meta is not None:
                    meta_path.unlink()
                    _fsync_dir(meta_path.parent)
                index_path.unlink()
                _fsync_dir(index_path.parent)
                recovered += 1
                continue
            if candidate.get("outcome") == "exit":
                context = candidate.get("context") or {}
                if context.get("phase") == "deployment-gpu-canary":
                    partial = staging / (index["log_name"] + ".partial")
                    final = staging / index["log_name"]
                    if os.path.lexists(final):
                        continue
                    if not os.path.lexists(partial):
                        raise ExecutionSandboxError(
                            "GPU canary exit receipt 存在但 partial log 缺失")
                    info = os.lstat(partial)
                    exit_code = candidate.get("returncode")
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.geteuid()
                            or isinstance(exit_code, bool)
                            or not isinstance(exit_code, int)):
                        raise ExecutionSandboxError(
                            "GPU canary recovered log/exit identity 非法")
                    # This publishes/cleans only the sandbox output quarantine;
                    # harness log sidecars and partial->final promotion remain
                    # separate steps below, mirroring run_staged recovery.
                    finalize_sandbox_output(
                        staging_dir=staging, log_name=index["log_name"],
                        context=context, execution_receipt=candidate,
                        exit_code=exit_code)
                    exit_path = staging / (index["log_name"] + ".exit")
                    exit_payload = str(exit_code).encode("ascii")
                    if os.path.lexists(exit_path):
                        if read_artifact_bytes(
                                exit_path, max_bytes=32,
                                label="GPU canary recovered exit") != exit_payload:
                            raise ExecutionSandboxError(
                                "GPU canary recovered exit sidecar 冲突")
                    else:
                        _write_private(exit_path, exit_payload)
                    atomic_write_receipt(
                        staging / (index["log_name"] + ".process.json"), {
                            "version": 1,
                            "operation_id": candidate["operation_id"],
                            "outcome": candidate["outcome"],
                            "group_drained": candidate["group_drained"],
                            "receipt_path": str(
                                execution_supervisor.receipt_dir
                                / f"execution-{candidate['operation_id']}.json"),
                        })
                    os.replace(partial, final)
                    _fsync_dir(staging)
                    recovered += 1
                continue
            finalize_sandbox_output(
                staging_dir=staging, log_name=index["log_name"],
                context=candidate["context"], execution_receipt=candidate,
                exit_code=125)
            recovered += 1
        return recovered

    def prepare(
            self, cmd: Sequence[str], *, staging_dir: Path | str, log_name: str,
            env: Optional[Mapping[str, str]], timeout_s: float,
            fd_expectations: Sequence[tuple[int, str, int, Optional[int], Optional[int]]] = (),
            tree_expectations: Sequence[tuple[int, Dict[str, str], tuple[str, ...]]] = (),
            execution_context: Optional[Mapping[str, Any]] = None,
            execution_supervisor=None, gpu_required: bool = False) -> SandboxInvocation:
        """Prepare one invocation and roll back only paths created by a failed prepare."""
        if not isinstance(gpu_required, bool):
            raise ValueError("sandbox gpu_required 须为 bool")
        if gpu_required and self.gpu_contract is None:
            raise ExecutionSandboxError(
                "本次实验要求 GPU，但部署未建立 exact GPU allocation contract")
        _validate_log_name(log_name)
        context = dict(execution_context or {})
        if "log_name" in context and context["log_name"] != log_name:
            raise ExecutionSandboxError("sandbox execution_context.log_name 冲突")
        context["log_name"] = log_name
        session_id = sandbox_session_id(log_name, context)
        staging = _path_under(Path(staging_dir), self.work_root, label="sandbox staging")
        paths = (
            staging / ".sandbox-meta" / f"{session_id}.json",
            staging / ".sandbox-output" / session_id,
            self.work_root / "state" / "sandbox" / "inputs" / session_id,
            self.work_root / "state" / "sandbox" / "sessions" / f"{session_id}.json",
        )
        partial_path = staging / (log_name + ".partial")
        if (any(os.path.lexists(path) for path in paths)
                or os.path.lexists(partial_path)):
            recovered = self.recover_unstarted_session(
                staging_dir=staging, log_name=log_name,
                execution_context=context,
                execution_supervisor=execution_supervisor,
                partial_path=partial_path)
            if not recovered:
                raise ExecutionSandboxError(
                    "sandbox session 已存在；须先走 guardian receipt recovery，不得覆盖重跑")
        existed = tuple(os.path.lexists(path) for path in paths)
        try:
            return self._prepare_impl(
                cmd, staging_dir=staging, log_name=log_name, env=env,
                timeout_s=timeout_s, fd_expectations=fd_expectations,
                tree_expectations=tree_expectations,
                execution_context=context, gpu_required=gpu_required)
        except BaseException:
            for path, was_present in zip(paths, existed):
                if was_present or not os.path.lexists(path):
                    continue
                info = os.lstat(path)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    shutil.rmtree(path)
                elif stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    path.unlink()
            raise

    def _prepare_impl(
            self, cmd: Sequence[str], *, staging_dir: Path | str, log_name: str,
            env: Optional[Mapping[str, str]], timeout_s: float,
            fd_expectations: Sequence[tuple[int, str, int, Optional[int], Optional[int]]] = (),
            tree_expectations: Sequence[tuple[int, Dict[str, str], tuple[str, ...]]] = (),
            execution_context: Optional[Mapping[str, Any]] = None,
            gpu_required: bool = False) -> SandboxInvocation:
        self.preflight()
        self.owner_guard()
        context = dict(execution_context or {})
        session_id = sandbox_session_id(log_name, context)
        staging = _path_under(Path(staging_dir), self.work_root, label="sandbox staging")
        if any(char in str(staging) for char in (",", ":", "\n", "\r")):
            raise ExecutionSandboxError("sandbox staging path 含 Docker mount 分隔字符")
        _ensure_directory_tree(self.work_root, staging, mode=0o700)
        meta_dir = staging / ".sandbox-meta"
        output_parent = staging / ".sandbox-output"
        for directory in (meta_dir, output_parent):
            _ensure_directory_tree(self.work_root, directory, mode=0o700)
            if stat.S_ISLNK(os.lstat(directory).st_mode):
                raise ExecutionSandboxError("sandbox trusted metadata/output parent 不得是 symlink")
        meta_path = meta_dir / f"{session_id}.json"
        output_root = output_parent / session_id
        if os.path.lexists(meta_path) or os.path.lexists(output_root):
            raise ExecutionSandboxError(
                "sandbox session 已存在；须先走 guardian receipt recovery，不得覆盖重跑")
        output_root.mkdir(mode=0o777)
        os.chmod(output_root, 0o777)
        token = secrets.token_hex(16)
        name = "mr-" + token
        input_root, replacements, input_manifest_hash = self._snapshot_inputs(
            token=session_id, fd_expectations=fd_expectations,
            tree_expectations=tree_expectations)
        if any(char in str(input_root) for char in (",", ":", "\n", "\r")):
            raise ExecutionSandboxError("sandbox input path 含 Docker mount 分隔字符")
        resolved = []
        for arg in cmd:
            if not isinstance(arg, str) or not arg or "\x00" in arg:
                raise ExecutionSandboxError("sandbox argv 非法")
            for source in sorted(replacements, key=len, reverse=True):
                arg = _replace_path_prefix(arg, source, replacements[source])
            if "/proc/self/fd/" in arg:
                raise ExecutionSandboxError("sandbox argv 仍含未快照的 host fd")
            if str(self.work_root) in arg:
                raise ExecutionSandboxError("sandbox argv 不得暴露 host work_root path")
            resolved.append(arg)
        payload_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1"}
        for key, value in (env or {}).items():
            if (not isinstance(key, str) or not key or "=" in key or "\x00" in key
                    or not isinstance(value, str) or "\x00" in value
                    or len(key.encode("utf-8")) > 256
                    or len(value.encode("utf-8")) > 65536):
                raise ExecutionSandboxError("sandbox env 须为字符串映射")
            if str(self.work_root) in value:
                raise ExecutionSandboxError("sandbox env 不得暴露 host work_root path")
            payload_env[key] = value
        for key, value in self.config["payload_environment"].items():
            if str(self.work_root) in value:
                raise ExecutionSandboxError(
                    "sandbox trusted payload_environment 不得暴露 host work_root path")
            if key in payload_env and payload_env[key] != value:
                raise ExecutionSandboxError(
                    f"sandbox env 不得覆盖 trusted payload_environment: {key}")
            payload_env[key] = value
        readonly_mounts = []
        for index, raw in enumerate(self.config["readonly_mounts"]):
            path = Path(raw)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or os.path.realpath(raw) != raw:
                raise ExecutionSandboxError("sandbox readonly_mount 不得是 symlink")
            readonly_mounts.append({"source": raw, "target": f"/mr/readonly/{index}"})
            for arg_index, arg in enumerate(resolved):
                resolved[arg_index] = _replace_path_prefix(
                    arg, raw, f"/mr/readonly/{index}")
        memory_bytes = self.config["memory_mb"] * 1024 * 1024
        max_file_bytes = self.config["max_file_mb"] * 1024 * 1024
        payload_env_json = json.dumps(
            payload_env, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        invocation_gpu = self.gpu_contract if gpu_required else None
        # CUDA reserves large virtual address ranges unrelated to resident host
        # RAM.  On the production cgroup path, memory.max remains the aggregate
        # hard boundary, so RLIMIT_AS must not reject a valid CUDA context.  The
        # weaker rlimit-fallback keeps the historical finite per-process cap.
        address_space_bytes = (
            -1 if invocation_gpu is not None
            and self.resource_mode in {"cgroup-v1", "cgroup-v2"}
            else memory_bytes)
        gpu_json = json.dumps(
            invocation_gpu, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        container_argv = [
            self.config["python_path"], "-I", "-B", "-c", _RLIMIT_LAUNCHER,
            str(address_space_bytes), str(self.config["pids"]), str(self.config["nofile"]),
            str(max_file_bytes), self.seccomp_bpf_b64,
            payload_env_json, gpu_json, "--", *resolved,
        ]
        control_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        }
        if invocation_gpu is not None:
            control_env["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
        spec = {
            "version": 1, "backend": _BACKEND,
            "engine_path": self.engine_path,
            "engine_host": self.config["engine_host"],
            "seccomp_path": str(self.seccomp_path),
            "seccomp_sha256": self.config["seccomp_sha256"],
            "seccomp_spec_hash": self.seccomp_spec_hash,
            "seccomp_bpf_b64": self.seccomp_bpf_b64,
            "seccomp_bpf_sha256": self.config["seccomp_bpf_sha256"],
            "image": self.config["image"], "image_id": self.config["image_id"],
            "name": name, "token": token, "resource_mode": self.resource_mode,
            "input_root": str(input_root), "output_root": str(output_root),
            "input_manifest_hash": input_manifest_hash,
            "readonly_mounts": readonly_mounts, "env": control_env,
            "payload_env": payload_env,
            "argv": container_argv, "gpu": invocation_gpu, "limits": {
                "memory_bytes": memory_bytes, "pids": self.config["pids"],
                "address_space_bytes": address_space_bytes,
                "nofile": self.config["nofile"], "max_file_bytes": max_file_bytes,
                "max_log_bytes": self.config["max_log_mb"] * 1024 * 1024,
                "max_output_bytes": self.config["max_output_mb"] * 1024 * 1024,
                "max_output_files": self.config["max_output_files"],
                "cpus": float(self.config["cpus"]),
                "tmpfs_bytes": self.config["tmpfs_mb"] * 1024 * 1024,
                "shm_bytes": self.config["shm_mb"] * 1024 * 1024,
            },
        }
        payload = _canonical(spec)
        if len(payload) > _MAX_SPEC_BYTES:
            raise ExecutionSandboxError("sandbox invocation spec 超过大小上限")
        spec_sha256 = _sha(payload)
        index_path = (
            self.work_root / "state" / "sandbox" / "sessions" / f"{session_id}.json")
        meta = {
            "version": _SESSION_VERSION, "session_id": session_id,
            "log_name": log_name, "context_hash": _sha(_canonical(context)),
            "name": name, "token": token, "spec_sha256": spec_sha256,
            "input_root": str(input_root), "output_root": str(output_root),
            "index_path": str(index_path),
            "input_manifest_hash": input_manifest_hash,
            "max_output_bytes": spec["limits"]["max_output_bytes"],
            "max_output_files": spec["limits"]["max_output_files"],
        }
        # Publish the central index first.  A SIGKILL between the two durable
        # writes is then discoverable at startup and can be removed after the
        # exact guardian/container absence proof; metadata-first would leave an
        # unindexed private snapshot when its DB owner was reconciled terminal.
        _ensure_directory_tree(self.work_root, index_path.parent, mode=0o700)
        _write_private(index_path, _canonical({
            "version": 1, "session_id": session_id,
            "meta_path": str(meta_path), "staging_dir": str(staging),
            "log_name": log_name, "context_hash": meta["context_hash"],
            "name": name, "token": token, "spec_sha256": spec_sha256,
        }))
        _write_private(meta_path, _canonical(meta))
        spec_file = tempfile.TemporaryFile()
        spec_file.write(payload)
        spec_file.flush()
        spec_file.seek(0)
        external = {
            "backend": _BACKEND, "engine_path": self.engine_path,
            "engine_host": self.config["engine_host"], "container_name": name,
            "token": token, "spec_sha256": spec_sha256,
            "network_mode": "none", "rootfs_readonly": True,
            "no_new_privileges": True, "cap_drop_all": True,
            "pid_namespace": True, "resource_mode": self.resource_mode,
        }
        wrapper = [
            sys.executable, "-I", os.path.abspath(__file__), "docker-runner",
            "--spec-fd", str(spec_file.fileno()), "--spec-sha256", spec_sha256,
        ]
        return SandboxInvocation(
            argv=wrapper, env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            pass_fds=(spec_file.fileno(),), external_container=external,
            spec_file=spec_file, staging_dir=staging, log_name=log_name,
            context=context)


def _load_session(staging: Path, log_name: str, context: Mapping[str, Any]) -> tuple[Path, Dict[str, Any]]:
    session_id = sandbox_session_id(log_name, context)
    path = staging / ".sandbox-meta" / f"{session_id}.json"
    raw = read_artifact_bytes(path, max_bytes=128 * 1024, label="sandbox session metadata")
    value = _strict_json(raw)
    if (value.get("version") != _SESSION_VERSION or value.get("session_id") != session_id
            or value.get("log_name") != log_name
            or value.get("context_hash") != _sha(_canonical(dict(context)))):
        raise ExecutionSandboxError("sandbox session metadata 与调用上下文不一致")
    return path, value


def _safe_remove_tree(path: Path, *, parent: Path) -> None:
    path = _path_under(path, parent, label="sandbox cleanup path")
    if path == parent:
        raise ExecutionSandboxError("sandbox cleanup 不得删除根")
    if os.path.lexists(path):
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise ExecutionSandboxError("sandbox cleanup root 不得是 symlink")
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            # Non-root deployments cannot unlink children from a published
            # 0555 input snapshot.  Do not chmod the ordinary 0777 output
            # quarantine pre-emptively: a rootless daemon can release its bind
            # asynchronously after container absence, and mutating that inode
            # first creates a transient permission view for a later mount.
            pass
        for current, dirs, _files in os.walk(
                path, topdown=True, followlinks=False):
            os.chmod(current, 0o700, follow_symlinks=False)
            for name in dirs:
                child = Path(current) / name
                if not stat.S_ISLNK(os.lstat(child).st_mode):
                    os.chmod(child, 0o700, follow_symlinks=False)
        shutil.rmtree(path)


def _output_ledger(root: Path, *, max_bytes: int, max_files: int) -> list[Dict[str, Any]]:
    ledger, total, entries = [], 0, 0
    if not os.path.lexists(root) or stat.S_ISLNK(os.lstat(root).st_mode):
        raise ExecutionSandboxError("sandbox quarantine output root 缺失/非法")
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(current) / name
            _safe_relpath(str(path.relative_to(root)))
            entries += 1
            if entries > max_files:
                raise ExecutionSandboxError("sandbox output 超目录/文件总数上限")
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ExecutionSandboxError("sandbox output 含 symlink 目录")
        for name in files:
            path = Path(current) / name
            rel = _safe_relpath(str(path.relative_to(root)))
            info = os.lstat(path)
            entries += 1
            if stat.S_ISLNK(info.st_mode):
                raise ExecutionSandboxError("sandbox output 含 symlink 文件")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExecutionSandboxError("sandbox output 只接受单链接常规文件")
            total += info.st_size
            if total > max_bytes or entries > max_files:
                raise ExecutionSandboxError("sandbox output 超文件数/总 bytes 上限")
            with open_artifact(path, expected_size=info.st_size,
                               label=f"sandbox output:{rel}") as capability:
                ledger.append({
                    "path": rel, "sha256": capability.identity.content_hash,
                    "bytes": capability.identity.size_bytes,
                })
    return sorted(ledger, key=lambda item: item["path"])


def _safe_promotion_destination(root: Path, rel: str) -> Path:
    """Create lexical parents below *root* without ever traversing a symlink."""
    rel = _safe_relpath(rel)
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ExecutionSandboxError("sandbox staging root 非可信目录")
    current = root
    parts = rel.split("/")
    for part in parts[:-1]:
        candidate = current / part
        if not os.path.lexists(candidate):
            os.mkdir(candidate, mode=0o700)
        info = os.lstat(candidate)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ExecutionSandboxError("sandbox output destination parent 不是可信目录")
        current = candidate
    return current / parts[-1]


def _promote_one(source: Path, *, root: Path, rel: str,
                 sha256: str, size: int) -> None:
    destination = _safe_promotion_destination(root, rel)
    if os.path.lexists(destination):
        try:
            with open_artifact(
                    destination, expected_hash=sha256, expected_size=size,
                    label="sandbox promoted output"):
                return
        except ArtifactCapabilityError as error:
            raise ExecutionSandboxError("sandbox output 与既有 destination 冲突") from error
    with open_artifact(
            source, expected_hash=sha256, expected_size=size,
            label="sandbox quarantine output") as capability:
        tmp = destination.with_name(
            f".{destination.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        actual = _copy_fd_to(capability.fd, tmp, size=size)
        if actual != sha256:
            raise ExecutionSandboxError("sandbox output promotion copy hash 漂移")
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
        _fsync_dir(destination.parent)


def finalize_sandbox_output(
        *, staging_dir: Path | str, log_name: str, context: Mapping[str, Any],
        execution_receipt: Mapping[str, Any], exit_code: int) -> None:
    """Idempotently promote a drained successful container's quarantine."""
    staging = Path(os.path.abspath(os.fspath(staging_dir)))
    meta_path, meta = _load_session(staging, log_name, context)
    sandbox = execution_receipt.get("sandbox")
    if (execution_receipt.get("containment") != _BACKEND or not isinstance(sandbox, dict)
            or sandbox.get("container_drained") is not True
            or (sandbox.get("container_name"), sandbox.get("token"), sandbox.get("spec_sha256"))
            != (meta.get("name"), meta.get("token"), meta.get("spec_sha256"))):
        raise ExecutionSandboxError("guardian receipt 未证明 exact sandbox container drained")
    output_root = Path(meta["output_root"])
    input_root = Path(meta["input_root"])
    promoted = meta_path.with_suffix(".promoted.json")
    if os.path.lexists(promoted):
        prior = _strict_json(read_artifact_bytes(
            promoted, max_bytes=_MAX_SPEC_BYTES,
            label="sandbox promoted receipt"))
        if (prior.get("session_id") != meta["session_id"]
                or prior.get("exit_code") != exit_code
                or prior.get("container_drained") is not True
                or prior.get("promoted") is not (exit_code == 0)):
            raise ExecutionSandboxError("sandbox promoted receipt 与 recovery 结果冲突")
        for item in prior.get("files", []):
            with open_artifact(
                    staging / _safe_relpath(item["path"]),
                    expected_hash=item["sha256"], expected_size=item["bytes"],
                    label="sandbox previously promoted output"):
                pass
        if os.path.lexists(output_root):
            _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
        if os.path.lexists(input_root):
            _safe_remove_tree(input_root, parent=input_root.parent)
        return
    if exit_code != 0:
        _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
        _safe_remove_tree(input_root, parent=input_root.parent)
        atomic_write_receipt(promoted, {
            "version": 1, "session_id": meta["session_id"], "exit_code": exit_code,
            "promoted": False, "container_drained": True,
        })
        return
    ledger = _output_ledger(
        output_root, max_bytes=int(meta["max_output_bytes"]),
        max_files=int(meta["max_output_files"]))
    plan_path = meta_path.with_suffix(".promotion-plan.json")
    if os.path.lexists(plan_path):
        prior = _strict_json(read_artifact_bytes(
            plan_path, max_bytes=_MAX_SPEC_BYTES,
            label="sandbox promotion plan"))
        if prior.get("files") != ledger:
            raise ExecutionSandboxError("sandbox output ledger 与已发布 promotion plan 漂移")
    else:
        atomic_write_receipt(plan_path, {
            "version": 1, "session_id": meta["session_id"], "files": ledger})
    reserved = {
        log_name, log_name + ".partial", log_name + ".exit",
        log_name + ".process.json", ".sandbox-output", ".sandbox-meta",
    }
    for item in ledger:
        rel = item["path"]
        if rel.split("/", 1)[0] in reserved or rel.startswith(".sandbox-"):
            raise ExecutionSandboxError("sandbox output 试图覆盖 trusted harness namespace")
        _promote_one(
            output_root / rel, root=staging, rel=rel,
            sha256=item["sha256"], size=item["bytes"])
    atomic_write_receipt(promoted, {
        "version": 1, "session_id": meta["session_id"], "exit_code": 0,
        "promoted": True, "container_drained": True,
        "output_manifest_hash": _sha(_canonical({"files": ledger})),
        "files": ledger,
    })
    _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
    _safe_remove_tree(input_root, parent=input_root.parent)


def discard_rejected_sandbox_output(
        *, staging_dir: Path | str, log_name: str, context: Mapping[str, Any],
        execution_receipt: Mapping[str, Any], reason: str) -> None:
    """Durably reject and remove a drained quarantine that cannot be promoted."""
    staging = Path(os.path.abspath(os.fspath(staging_dir)))
    meta_path, meta = _load_session(staging, log_name, context)
    sandbox = execution_receipt.get("sandbox")
    if (execution_receipt.get("containment") != _BACKEND or not isinstance(sandbox, dict)
            or sandbox.get("container_drained") is not True
            or (sandbox.get("container_name"), sandbox.get("token"), sandbox.get("spec_sha256"))
            != (meta.get("name"), meta.get("token"), meta.get("spec_sha256"))):
        raise ExecutionSandboxError(
            "guardian receipt 未证明 exact sandbox container drained，拒绝清理 quarantine")
    promoted = meta_path.with_suffix(".promoted.json")
    if os.path.lexists(promoted):
        raise ExecutionSandboxError("sandbox session 已有 promoted authority，不得改写为 rejected")
    output_root = Path(meta["output_root"])
    input_root = Path(meta["input_root"])
    if os.path.lexists(output_root):
        _safe_remove_tree(output_root, parent=staging / ".sandbox-output")
    if os.path.lexists(input_root):
        _safe_remove_tree(input_root, parent=input_root.parent)
    rejected = meta_path.with_suffix(".rejected.json")
    record = {
        "version": 1, "session_id": meta["session_id"],
        "container_drained": True, "promoted": False,
        "reason_sha256": _sha(reason.encode("utf-8", errors="replace")),
    }
    if os.path.lexists(rejected):
        prior = _strict_json(read_artifact_bytes(
            rejected, max_bytes=128 * 1024,
            label="sandbox rejected receipt"))
        if prior != record:
            raise ExecutionSandboxError("sandbox rejected receipt 冲突")
    else:
        atomic_write_receipt(rejected, record)


def _verify_runner_spec(spec: Dict[str, Any]) -> None:
    required = {
        "version", "backend", "engine_path", "engine_host",
        "seccomp_path", "seccomp_sha256", "seccomp_spec_hash",
        "seccomp_bpf_b64", "seccomp_bpf_sha256", "image", "image_id",
        "name", "token", "resource_mode", "input_root", "output_root",
        "input_manifest_hash", "readonly_mounts", "env", "payload_env", "argv", "gpu",
        "limits",
    }
    if (set(spec) != required or spec.get("version") != 1
            or spec.get("backend") != _BACKEND
            or not isinstance(spec.get("name"), str) or not spec["name"].startswith("mr-")
            or not isinstance(spec.get("token"), str) or _TOKEN_RE.fullmatch(spec["token"]) is None
            or not isinstance(spec.get("image"), str) or _IMAGE_RE.fullmatch(spec["image"]) is None
            or not isinstance(spec.get("image_id"), str)
            or _IMAGE_ID_RE.fullmatch(spec["image_id"]) is None
            or not isinstance(spec.get("seccomp_path"), str)
            or not os.path.isabs(spec["seccomp_path"])
            or not isinstance(spec.get("seccomp_sha256"), str)
            or _SHA256_RE.fullmatch(spec["seccomp_sha256"]) is None
            or not isinstance(spec.get("seccomp_spec_hash"), str)
            or _SHA256_RE.fullmatch(spec["seccomp_spec_hash"]) is None
            or not isinstance(spec.get("seccomp_bpf_b64"), str)
            or not isinstance(spec.get("seccomp_bpf_sha256"), str)
            or _SHA256_RE.fullmatch(spec["seccomp_bpf_sha256"]) is None
            or not isinstance(spec.get("argv"), list) or not spec["argv"]
            or not isinstance(spec.get("env"), dict)
            or not isinstance(spec.get("payload_env"), dict)
            or not isinstance(spec.get("limits"), dict)):
        raise ExecutionSandboxError("docker runner spec 字段闭包/类型非法")
    try:
        gpu = normalize_gpu_contract(spec.get("gpu"))
    except ValueError as error:
        raise ExecutionSandboxError("docker runner GPU contract 非法") from error
    if gpu != spec.get("gpu"):
        raise ExecutionSandboxError("docker runner GPU contract 非 canonical")


def _verify_created_container(spec: Mapping[str, Any], payload: Any) -> None:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ExecutionSandboxError("sandbox created-container inspect 非唯一对象")
    value = payload[0]
    config, host = value.get("Config"), value.get("HostConfig")
    mounts = value.get("Mounts")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(mounts, list):
        raise ExecutionSandboxError("sandbox created-container inspect 缺配置")
    labels = config.get("Labels")
    security = host.get("SecurityOpt") or []
    seccomp_entries = [
        str(item).removeprefix("seccomp=") for item in security
        if str(item).startswith("seccomp=")]
    if len(seccomp_entries) != 1:
        raise ExecutionSandboxError("sandbox daemon additive seccomp profile 非唯一")
    if seccomp_entries[0] == "unconfined":
        raise ExecutionSandboxError("sandbox daemon additive seccomp 不得 unconfined")
    gpu = spec.get("gpu")
    expected_device_requests = []
    if gpu is not None:
        expected_device_requests = [{
            "Driver": "nvidia", "Count": 0,
            "DeviceIDs": [item["uuid"] for item in gpu["devices"]],
            "Capabilities": [["compute", "utility", "gpu"]], "Options": {},
        }]
    if (value.get("Name") != "/" + spec["name"]
            or value.get("Image") != spec["image_id"]
            or not isinstance(labels, dict) or labels.get(_LABEL) != spec["token"]
            or config.get("User") != "65534:65534"
            or config.get("Entrypoint") not in (None, [])
            or config.get("Cmd") != spec["argv"]
            or config.get("WorkingDir") != "/mr/output"
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or host.get("IpcMode") != "none"
            or host.get("PidMode") not in ("", None)
            or "ALL" not in (host.get("CapDrop") or [])
            or not any(item == "no-new-privileges:true" for item in security)
            or bool(host.get("Devices"))
            or (host.get("DeviceRequests") or []) != expected_device_requests
            or host.get("PidsLimit") != spec["limits"]["pids"]
            or host.get("Memory") != spec["limits"]["memory_bytes"]
            or host.get("MemorySwap") != spec["limits"]["memory_bytes"]
            or host.get("NanoCpus") != int(float(spec["limits"]["cpus"]) * 1_000_000_000)
            or host.get("Tmpfs") != {
                "/tmp": ("rw,nosuid,nodev,noexec,size="
                         f"{spec['limits']['tmpfs_bytes']}"),
            }
            or host.get("ShmSize") != spec["limits"]["shm_bytes"]
            or host.get("LogConfig") != {
                "Type": "json-file",
                "Config": {
                    "max-file": "2",
                    "max-size": f"{max(1, spec['limits']['max_log_bytes'] // 2)}b",
                },
            }
            or host.get("AutoRemove") is not False):
        raise ExecutionSandboxError("sandbox daemon 实际安全/资源配置与请求不一致")
    expected_mounts = {
        (spec["input_root"], "/mr/input", False),
        (spec["output_root"], "/mr/output", True),
        *((item["source"], item["target"], False)
          for item in spec["readonly_mounts"]),
    }
    requested_mounts = set()
    for mount in host.get("Mounts") or []:
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            continue
        requested_mounts.add((
            mount.get("Source"), mount.get("Target"),
            not bool(mount.get("ReadOnly"))))
    requested_by_target = {
        target: (source, writable) for source, target, writable in requested_mounts}
    if set(requested_by_target) != {target for _source, target, _writable in expected_mounts}:
        raise ExecutionSandboxError("sandbox daemon HostConfig bind target 闭包不一致")
    for expected_source, target, expected_writable in expected_mounts:
        actual_source, actual_writable = requested_by_target[target]
        # Rootless Docker on this deployment rewrites host bind sources through
        # a trusted bindfs prefix.  Accept only the exact mapping mechanically
        # derived from mountinfo; a generic suffix match could alias another
        # host tree with the same tail.
        if (actual_writable != expected_writable
                or not isinstance(actual_source, str)
                or actual_source not in _daemon_bind_source_candidates(
                    expected_source)):
            raise ExecutionSandboxError("sandbox daemon HostConfig bind source/mode 不一致")
    actual_mounts = set()
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            continue
        actual_mounts.add((mount.get("Destination"), bool(mount.get("RW"))))
    expected_runtime = {(target, writable) for _source, target, writable in expected_mounts}
    if actual_mounts != expected_runtime:
        raise ExecutionSandboxError(
            "sandbox daemon 实际 bind mount 闭包不一致: "
            f"actual={sorted(actual_mounts)!r}, expected={sorted(expected_runtime)!r}")
    actual_env = config.get("Env")
    if not isinstance(actual_env, list):
        raise ExecutionSandboxError("sandbox daemon 实际 env 非数组")
    env_map = {}
    for item in actual_env:
        if not isinstance(item, str) or "=" not in item:
            raise ExecutionSandboxError("sandbox daemon 实际 env 非 canonical")
        key, value_item = item.split("=", 1)
        if key in env_map:
            raise ExecutionSandboxError("sandbox daemon 实际 env key 重复")
        env_map[key] = value_item
    if any(env_map.get(key) != value_item for key, value_item in spec["env"].items()):
        raise ExecutionSandboxError("sandbox daemon 实际 env 与请求不一致")


def _docker_runner(spec: Dict[str, Any]) -> int:
    _verify_runner_spec(spec)
    engine, host = spec["engine_path"], spec["engine_host"]
    profile_info = os.lstat(spec["seccomp_path"])
    if (not stat.S_ISREG(profile_info.st_mode) or stat.S_ISLNK(profile_info.st_mode)
            or profile_info.st_mode & 0o022 or profile_info.st_size > 1024 * 1024):
        raise ExecutionSandboxError("sandbox runner seccomp profile 身份非法")
    profile_raw = read_artifact_bytes(
        spec["seccomp_path"], expected_hash=spec["seccomp_sha256"],
        expected_size=profile_info.st_size, max_bytes=1024 * 1024,
        label="sandbox runner seccomp profile")
    profile = _strict_json(profile_raw)
    if (profile_raw != _canonical(profile)
            or _sha(_canonical(profile)) != spec["seccomp_spec_hash"]
            or profile.get("defaultAction") != "SCMP_ACT_ERRNO"):
        raise ExecutionSandboxError("sandbox runner seccomp profile pin 漂移")
    try:
        bpf = base64.b64decode(spec["seccomp_bpf_b64"], validate=True)
    except (binascii.Error, ValueError) as error:
        raise ExecutionSandboxError("sandbox runner seccomp BPF base64 非法") from error
    if (not bpf or len(bpf) % 8 != 0 or len(bpf) > 64 * 1024
            or _sha(bpf) != spec["seccomp_bpf_sha256"]):
        raise ExecutionSandboxError("sandbox runner seccomp BPF pin 漂移")
    image = _bounded_text(_engine(
        engine, host, ["image", "inspect", spec["image"], "--format", "{{.Id}}"]),
        what="sandbox runner image inspect")
    if image != spec["image_id"]:
        raise ExecutionSandboxError("sandbox runner image pin 漂移")
    limits = spec["limits"]
    args = [
        "container", "create", "--name", spec["name"],
        "--label", f"{_LABEL}={spec['token']}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--ipc", "none",
        "--pids-limit", str(limits["pids"]),
        "--memory", str(limits["memory_bytes"]),
        "--memory-swap", str(limits["memory_bytes"]),
        "--cpus", str(limits["cpus"]), "--user", "65534:65534",
        "--log-driver", "json-file", "--log-opt", "max-file=2",
        "--log-opt", f"max-size={max(1, limits['max_log_bytes'] // 2)}b",
        "--hostname", "meta-research-sandbox", "--workdir", "/mr/output",
        "--shm-size", str(limits["shm_bytes"]),
        "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={limits['tmpfs_bytes']}",
        "--mount", f"type=bind,src={spec['input_root']},dst=/mr/input,readonly",
        "--mount", f"type=bind,src={spec['output_root']},dst=/mr/output",
    ]
    if spec["gpu"] is not None:
        # Docker's --gpus grammar requires the literal quote characters when
        # multiple comma-separated sub-fields are passed as one argv token.
        args.extend(["--gpus", gpu_cli_argument(spec["gpu"])])
    for mount in spec["readonly_mounts"]:
        args.extend(["--mount", f"type=bind,src={mount['source']},dst={mount['target']},readonly"])
    for key, value in sorted(spec["env"].items()):
        args.extend(["--env", f"{key}={value}"])
    args.extend([spec["image"], *spec["argv"]])
    # A local timeout could kill the CLI after the daemon accepted CREATE but
    # before the name becomes observable to the guardian.  Let the external
    # guardian's wall deadline own cancellation; on owner loss it keeps the
    # fence until this trusted registration phase becomes observable or exits.
    _bounded_text(_engine(engine, host, args, timeout=None), what="sandbox container create")
    try:
        inspected = _bounded_text(_engine(
            engine, host, ["container", "inspect", spec["name"]], timeout=30.0),
            what="sandbox created-container inspect")
        try:
            inspect_value = json.loads(inspected)
        except json.JSONDecodeError as error:
            raise ExecutionSandboxError("sandbox created-container inspect 非 JSON") from error
        _verify_created_container(spec, inspect_value)
        _bounded_text(_engine(
            engine, host, ["container", "start", spec["name"]], timeout=30.0),
            what="sandbox container start")
        waited = _bounded_text(_engine(
            engine, host, ["container", "wait", spec["name"]],
            timeout=None), what="sandbox container wait")
        try:
            exit_code = int(waited)
        except ValueError as error:
            raise ExecutionSandboxError("sandbox container wait exit code 非法") from error
        log_path_text = _bounded_text(_engine(
            engine, host, ["container", "inspect", spec["name"],
                           "--format", "{{.LogPath}}"], timeout=30.0),
            what="sandbox container log-path inspect")
        log_path = Path(log_path_text)
        if (not log_path.is_absolute() or "\x00" in log_path_text
                or os.path.lexists(Path(log_path_text + ".1"))):
            raise ExecutionSandboxError(
                "sandbox stdout/stderr 达到硬日志上限；拒绝接纳被轮转截断的证据")
        logs = _engine(
            engine, host, ["container", "logs", spec["name"]], timeout=30.0,
            stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)
        if logs.returncode != 0:
            raise ExecutionSandboxError("sandbox container logs 读取失败")
        removed = _engine(
            engine, host, ["container", "rm", "--volumes", spec["name"]],
            timeout=30.0)
        if removed.returncode != 0:
            raise ExecutionSandboxError("sandbox container 正常清理失败")
        return exit_code if 0 <= exit_code <= 255 else 125
    except BaseException:
        # The external guardian owns the authoritative name+label cleanup.
        # A best-effort local remove only reduces latency; it is never used as
        # the terminal proof.
        try:
            _engine(engine, host, ["container", "rm", "--force", "--volumes", spec["name"]])
        except BaseException:
            pass
        raise


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", choices=["docker-runner"])
    parser.add_argument("--spec-fd", type=int, required=True)
    parser.add_argument("--spec-sha256", required=True)
    return parser.parse_args(argv)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    raw = _read_fd(args.spec_fd)
    if _sha(raw) != args.spec_sha256 or _SHA256_RE.fullmatch(args.spec_sha256) is None:
        raise SystemExit("sandbox runner spec hash 不符")
    os.close(args.spec_fd)
    try:
        return _docker_runner(_strict_json(raw))
    except BaseException as error:
        print(f"sandbox runner failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(_main())
