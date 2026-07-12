"""Fail-closed deployment facts and one owner-bound startup receipt.

This module deliberately does not deploy, schedule, or recover anything.  It
performs one fresh probe, evaluates that immutable snapshot against one short-
lived operator attestation, and writes one private receipt for the current run
owner.  Development mode records the same facts but can never claim production
readiness.

The default probe treats a hard filesystem quota as operator-attested evidence:
``statvfs`` reports capacity, not a byte/inode quota.  Production evaluation
therefore accepts quota limits only from a root-owned attestation whose mount
identity matches the live work root.  It never relabels filesystem-wide free
space as quota enforcement.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .process_supervisor import atomic_write_receipt


_PROTOCOL = "deployment-preflight-v1"
_ATTESTATION_PROTOCOL = "deployment-attestation-v1"
_MAX_ATTESTATION_BYTES = 32 * 1024
_MAX_PROBE_OUTPUT = 4 * 1024 * 1024
_MAX_ERROR_CHARS = 1000
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_GPU_UUID_RE = re.compile(r"^GPU-[A-Za-z0-9-]{1,128}$")


class DeploymentPreflightError(RuntimeError):
    """The requested production trust boundary was not established."""

    def __init__(self, message: str, *, receipt: Optional[Mapping[str, Any]] = None,
                 receipt_path: Optional[Path] = None):
        super().__init__(message)
        self.receipt = dict(receipt) if receipt is not None else None
        self.receipt_path = Path(receipt_path) if receipt_path is not None else None


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _value_hash(value: Mapping[str, Any]) -> str:
    return _hash_bytes(_canonical(value))


def _bounded_error(error: BaseException | str) -> str:
    text = str(error).replace("\x00", "?").replace("\r", " ").replace("\n", " ")
    return text[:_MAX_ERROR_CHARS]


def _strict_json(raw: bytes) -> Dict[str, Any]:
    if len(raw) > _MAX_ATTESTATION_BYTES:
        raise ValueError("deployment attestation 超过大小上限")

    def unique(pairs):  # noqa: ANN001 - json hook protocol
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"deployment attestation 含重复 key: {key}")
            out[key] = value
        return out

    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"deployment attestation 含非有限数字: {token}")))
    if not isinstance(value, dict):
        raise ValueError("deployment attestation 须为 object")
    return value


def _int(value: Any, *, minimum: int = 0) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= minimum)


def _number(value: Any, *, minimum: float = 0.0) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)) and float(value) >= minimum)


def _text(value: Any, *, maximum: int = 4096, nonempty: bool = True) -> bool:
    return (isinstance(value, str) and "\x00" not in value
            and len(value.encode("utf-8")) <= maximum
            and (bool(value) or not nonempty))


def _absolute(value: Any) -> bool:
    return (_text(value) and os.path.isabs(value)
            and os.path.normpath(value) == value)


def _sandbox_config(sandbox: Any) -> Dict[str, Any]:
    raw = sandbox if isinstance(sandbox, Mapping) else getattr(sandbox, "config", None)
    if not isinstance(raw, Mapping):
        raise ValueError("sandbox 须为 config mapping 或带 .config 的 sandbox")
    return dict(raw)


def _optional_limit(mapping: Mapping[str, Any], key: str, *, scale: int = 1) -> int:
    value = mapping.get(key, 0)
    if not _int(value, minimum=0):
        raise ValueError(f"deployment reserve source {key} 非法")
    return int(value) * scale


def _deployment_reserves(
        policy: Mapping[str, Any], resources: Mapping[str, Any],
        sandbox_config: Mapping[str, Any]) -> Dict[str, int]:
    materialization = policy.get("import_materialization")
    materialization = materialization if isinstance(materialization, Mapping) else {}
    dependency = materialization.get("dependency_image")
    dependency = dependency if isinstance(dependency, Mapping) else {}
    output_bytes = _optional_limit(sandbox_config, "max_output_mb", scale=1024 ** 2)
    output_files = _optional_limit(sandbox_config, "max_output_files")
    installed_bytes = _optional_limit(dependency, "max_installed_bytes")
    wheel_bytes = _optional_limit(dependency, "max_total_wheel_bytes")
    archive_bytes = _optional_limit(dependency, "max_image_archive_bytes")
    installed_files = _optional_limit(dependency, "max_installed_files")
    wheel_entries = _optional_limit(dependency, "max_wheel_entries")
    repository_bytes = _optional_limit(materialization, "max_total_bytes")
    repository_files = _optional_limit(materialization, "max_files")
    return {
        "docker_free_bytes": max(
            1, output_bytes, archive_bytes, installed_bytes + wheel_bytes),
        "docker_free_inodes": max(
            1, output_files, installed_files + wheel_entries),
        "work_hard_bytes": max(
            1, math.ceil(float(resources["disk_quota_gb"]) * (1024 ** 3))),
        "work_free_bytes": max(1, output_bytes, repository_bytes),
        "work_free_inodes": max(
            1, output_files, repository_files + installed_files),
    }


def _validate_inputs(policy: Mapping[str, Any], sandbox: Any, owner_id: str) -> tuple:
    if not isinstance(policy, Mapping):
        raise ValueError("deployment preflight policy 须为 mapping")
    deployment = policy.get("deployment")
    resources = policy.get("resources")
    if (not isinstance(deployment, Mapping)
            or set(deployment) != {"mode", "attestation_path", "max_attestation_age_s"}):
        raise ValueError("policy.deployment 字段闭包非法")
    if deployment.get("mode") not in {"development", "production"}:
        raise ValueError("policy.deployment.mode 非法")
    path = deployment.get("attestation_path")
    if path is not None and not _absolute(path):
        raise ValueError("policy.deployment.attestation_path 须为 null 或规范绝对路径")
    age = deployment.get("max_attestation_age_s")
    if not _int(age, minimum=1) or age > 7 * 24 * 3600:
        raise ValueError("policy.deployment.max_attestation_age_s 须在 [1,604800]")
    if deployment.get("mode") == "production" and age > 300:
        raise ValueError("production deployment attestation 最长只接受 300 秒")
    if not isinstance(resources, Mapping) or not {"gpus", "gpu_mem_gb", "disk_quota_gb"} <= set(resources):
        raise ValueError("policy.resources 缺 gpus/gpu_mem_gb/disk_quota_gb")
    if (not _int(resources.get("gpus"), minimum=0)
            or not _number(resources.get("gpu_mem_gb"), minimum=0)
            or not _number(resources.get("disk_quota_gb"), minimum=0)):
        raise ValueError("policy.resources 类型/范围非法")
    if (not isinstance(owner_id, str) or _OWNER_RE.fullmatch(owner_id) is None
            or ".." in owner_id):
        raise ValueError("deployment owner_id 非安全文件名")
    config = _sandbox_config(sandbox)
    engine_path, engine_host = config.get("engine_path"), config.get("engine_host")
    if not _absolute(engine_path):
        raise ValueError("sandbox.engine_path 须为规范绝对路径")
    if (not isinstance(engine_host, str) or not engine_host.startswith("unix:///")
            or not _absolute(engine_host.removeprefix("unix://"))):
        raise ValueError("sandbox.engine_host 须为规范绝对 unix socket")
    if config.get("resource_mode") not in {"cgroup-v1", "cgroup-v2", "rlimit-fallback"}:
        raise ValueError("sandbox.resource_mode 非法")
    resources = dict(resources)
    return (
        dict(deployment), resources, config,
        _deployment_reserves(policy, resources, config),
    )


def _mountinfo_unescape(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def longest_mount(path: Path | str, mountinfo_text: str) -> Dict[str, Any]:
    """Parse Linux mountinfo and return the deepest mount containing ``path``."""
    target = os.path.abspath(os.fspath(path))
    candidates = []
    for line in mountinfo_text.splitlines():
        if " - " not in line:
            continue
        left_text, right_text = line.split(" - ", 1)
        left, right = left_text.split(), right_text.split()
        if len(left) < 6 or len(right) < 3:
            continue
        mount_point = _mountinfo_unescape(left[4])
        if not (target == mount_point
                or target.startswith(mount_point.rstrip("/") + "/")):
            continue
        candidates.append({
            "mount_id": left[0], "parent_id": left[1],
            "major_minor": left[2], "root": _mountinfo_unescape(left[3]),
            "mount_point": mount_point, "mount_options": sorted(left[5].split(",")),
            "optional_fields": left[6:], "fstype": right[0],
            "source": _mountinfo_unescape(right[1]),
            "super_options": sorted(right[2].split(",")),
        })
    if not candidates:
        raise ValueError("work_root 未匹配 /proc/self/mountinfo")
    return max(candidates, key=lambda item: len(item["mount_point"]))


def _empty_facts(error: str) -> Dict[str, Any]:
    return {
        "service": {"uid": None, "gid": None, "groups": [], "username": None,
                    "codex_home": None, "codex_home_stat": None, "error": error},
        "isolation": {"machine_id": None, "boot_id": None, "hostname": None,
                      "vm_kind": None, "container_kind": None, "error": error},
        "work_root": {"path": None, "uid": None, "gid": None, "mode": None,
                      "is_directory": False, "direct": False, "dev": None, "ino": None,
                      "mount": None, "error": error},
        "docker": {"engine_path": None, "engine_host": None, "socket": None,
                   "daemon": None, "storage": None, "error": error},
        "gpu": {"inventory": [], "error": error},
    }


class SystemProbeBackend:
    """Read-only Linux/Docker/NVIDIA fact collector used by the production entry."""

    def collect(self, *, work_root: Path, sandbox: Any) -> Dict[str, Any]:
        config = _sandbox_config(sandbox)
        return {
            "service": self._service(),
            "isolation": self._isolation(),
            "work_root": self._work_root(work_root),
            "docker": self._docker(config),
            "gpu": self._gpus(),
        }

    @staticmethod
    def _service() -> Dict[str, Any]:
        uid, gid = os.geteuid(), os.getegid()
        codex_home = os.environ.get("CODEX_HOME")
        codex_home_stat = None
        home_error = None
        try:
            if not _absolute(codex_home):
                raise ValueError("CODEX_HOME 未设置为规范绝对路径")
            home = os.lstat(codex_home)
            auth_path = os.path.join(codex_home, "auth.json")
            auth = os.lstat(auth_path)
            codex_home_stat = {
                "path": codex_home,
                "direct": os.path.realpath(codex_home) == codex_home,
                "is_directory": stat.S_ISDIR(home.st_mode),
                "uid": home.st_uid, "gid": home.st_gid,
                "mode": stat.S_IMODE(home.st_mode),
                "auth": {
                    "path": auth_path,
                    "direct": os.path.realpath(auth_path) == auth_path,
                    "is_regular": stat.S_ISREG(auth.st_mode),
                    "nlink": auth.st_nlink,
                    "uid": auth.st_uid, "gid": auth.st_gid,
                    "mode": stat.S_IMODE(auth.st_mode),
                },
            }
        except Exception as caught:
            home_error = _bounded_error(caught)
        try:
            username = pwd.getpwuid(uid).pw_name
            error = None
        except (KeyError, OSError) as caught:
            username, error = None, _bounded_error(caught)
        return {
            "uid": uid, "gid": gid, "groups": sorted(set(os.getgroups())),
            "username": username, "codex_home": codex_home,
            "codex_home_stat": codex_home_stat,
            "error": "; ".join(item for item in (error, home_error) if item) or None,
        }

    @staticmethod
    def _isolation() -> Dict[str, Any]:
        try:
            machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii").strip()
            hostname = os.uname().nodename
            if (re.fullmatch(r"[0-9a-f]{32}", machine_id) is None
                    or re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        boot_id) is None
                    or not _text(hostname, maximum=253)):
                raise ValueError("machine/boot/hostname identity 非法")
            detector = shutil.which("systemd-detect-virt", path=os.defpath)
            if detector is None:
                raise ValueError("systemd-detect-virt 不可用")

            def detect(kind: str) -> Optional[str]:
                result = subprocess.run(
                    [detector, kind], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=5.0, check=False,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
                if len(result.stdout) + len(result.stderr) > 1024:
                    raise ValueError("systemd-detect-virt 输出越界")
                if result.returncode == 1:
                    return None
                if result.returncode != 0:
                    raise ValueError("systemd-detect-virt 探测失败")
                value = result.stdout.decode("ascii").strip()
                if re.fullmatch(r"[a-z0-9_.-]{1,64}", value) is None:
                    raise ValueError("systemd-detect-virt 输出非法")
                return value

            vm_kind = detect("--vm")
            container_kind = detect("--container")
            if vm_kind is None:
                raise ValueError("production dedicated-vm 未检测到 VM hypervisor")
            return {"machine_id": machine_id, "boot_id": boot_id,
                    "hostname": hostname, "vm_kind": vm_kind,
                    "container_kind": container_kind, "error": None}
        except Exception as error:
            return {"machine_id": None, "boot_id": None, "hostname": None,
                    "vm_kind": None, "container_kind": None,
                    "error": _bounded_error(error)}

    @staticmethod
    def _work_root(work_root: Path) -> Dict[str, Any]:
        path = os.path.abspath(os.fspath(work_root))
        try:
            info = os.lstat(path)
            direct = os.path.realpath(path) == path
            raw_mountinfo = Path("/proc/self/mountinfo").read_text(
                encoding="utf-8", errors="strict")
            if len(raw_mountinfo.encode("utf-8")) > _MAX_PROBE_OUTPUT:
                raise ValueError("/proc/self/mountinfo 超过大小上限")
            mount = longest_mount(path, raw_mountinfo)
            return {
                "path": path, "uid": info.st_uid, "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode),
                "is_directory": stat.S_ISDIR(info.st_mode), "direct": direct,
                "dev": info.st_dev, "ino": info.st_ino, "mount": mount,
                "error": None,
            }
        except Exception as error:
            return {
                "path": path, "uid": None, "gid": None, "mode": None,
                "is_directory": False, "direct": False, "dev": None, "ino": None,
                "mount": None, "error": _bounded_error(error),
            }

    @staticmethod
    def _docker(config: Mapping[str, Any]) -> Dict[str, Any]:
        engine = str(config["engine_path"])
        host = str(config["engine_host"])
        socket_path = host.removeprefix("unix://")
        errors = []
        socket_fact: Optional[Dict[str, Any]] = None
        try:
            socket_info = os.lstat(socket_path)
            socket_realpath = os.path.realpath(socket_path)
            socket_fact = {
                "path": socket_path,
                "direct": (stat.S_ISSOCK(socket_info.st_mode)
                           and socket_realpath == socket_path),
                "is_socket": stat.S_ISSOCK(socket_info.st_mode),
                "uid": socket_info.st_uid, "gid": socket_info.st_gid,
                "mode": stat.S_IMODE(socket_info.st_mode),
                "dev": socket_info.st_dev, "ino": socket_info.st_ino,
                "realpath": socket_realpath,
            }
            if not socket_fact["direct"]:
                errors.append("engine_host 不是 direct unix socket")
        except Exception as error:
            errors.append("docker socket: " + _bounded_error(error))

        daemon: Optional[Dict[str, Any]] = None
        storage: Optional[Dict[str, Any]] = None
        try:
            result = subprocess.run(
                [engine, "info", "--format", "{{json .}}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15.0, check=False,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                     "DOCKER_HOST": host})
            if len(result.stdout) + len(result.stderr) > _MAX_PROBE_OUTPUT:
                raise ValueError("docker info 输出超过大小上限")
            if result.returncode != 0:
                raise RuntimeError(
                    "docker info 失败: "
                    + result.stderr.decode("utf-8", errors="replace")[:500])
            info = json.loads(result.stdout.decode("utf-8"))
            if not isinstance(info, dict):
                raise ValueError("docker info 输出须为 object")
            security = info.get("SecurityOptions")
            runtimes = info.get("Runtimes")
            if not isinstance(security, list) or not isinstance(runtimes, dict):
                raise ValueError("docker info security/runtimes 非法")
            cgroup_version = str(info.get("CgroupVersion", ""))
            cgroup_driver = str(info.get("CgroupDriver", ""))
            resource_mode = (
                "rlimit-fallback" if cgroup_driver == "none"
                else "cgroup-v2" if cgroup_version == "2"
                else "cgroup-v1" if cgroup_version == "1" else "unknown")
            root_dir = info.get("DockerRootDir")
            daemon = {
                "id": info.get("ID"), "name": info.get("Name"),
                "rootless": any("rootless" in str(item) for item in security),
                "security_options": sorted(str(item) for item in security),
                "cgroup_version": cgroup_version,
                "cgroup_driver": cgroup_driver, "resource_mode": resource_mode,
                "root_dir": root_dir, "runtimes": sorted(str(item) for item in runtimes),
                "limits": {
                    "memory": info.get("MemoryLimit") is True,
                    "cpu": info.get("CpuCfsQuota") is True,
                    "pids": info.get("PidsLimit") is True,
                },
            }
            if not _absolute(root_dir):
                raise ValueError("docker root dir 非规范绝对路径")
            fs = os.statvfs(root_dir)
            storage = {
                "free_bytes": int(fs.f_bavail) * int(fs.f_frsize),
                "free_inodes": int(fs.f_favail),
            }
        except Exception as error:
            errors.append("docker daemon: " + _bounded_error(error))
        return {
            "engine_path": engine, "engine_host": host, "socket": socket_fact,
            "daemon": daemon, "storage": storage,
            "error": "; ".join(errors) if errors else None,
        }

    @staticmethod
    def _gpus() -> Dict[str, Any]:
        binary = shutil.which("nvidia-smi", path=os.defpath)
        if binary is None:
            return {"inventory": [], "error": "nvidia-smi 不可用"}
        try:
            result = subprocess.run(
                [binary, "--query-gpu=index,uuid,memory.total",
                 "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15.0, check=False,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
            if len(result.stdout) + len(result.stderr) > _MAX_PROBE_OUTPUT:
                raise ValueError("nvidia-smi 输出超过大小上限")
            if result.returncode != 0:
                raise RuntimeError(
                    "nvidia-smi 失败: "
                    + result.stderr.decode("utf-8", errors="replace")[:500])
            inventory, indexes, uuids = [], set(), set()
            for raw_line in result.stdout.decode("utf-8").splitlines():
                if not raw_line.strip():
                    continue
                parts = [part.strip() for part in raw_line.split(",")]
                if len(parts) != 3:
                    raise ValueError("nvidia-smi inventory 行字段数非法")
                index, uuid, memory_mib = int(parts[0]), parts[1], int(parts[2])
                if (index < 0 or index in indexes or uuid in uuids
                        or _GPU_UUID_RE.fullmatch(uuid) is None or memory_mib <= 0):
                    raise ValueError("nvidia-smi inventory 身份/数值非法")
                indexes.add(index)
                uuids.add(uuid)
                inventory.append({
                    "index": index, "uuid": uuid,
                    "memory_bytes": memory_mib * 1024 * 1024,
                })
            return {"inventory": sorted(inventory, key=lambda item: item["index"]),
                    "error": None}
        except Exception as error:
            return {"inventory": [], "error": _bounded_error(error)}


def _read_attestation(path: Path, validator: Any = None) -> tuple[Dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1 or before.st_uid != 0
            or before.st_mode & 0o022 or before.st_size > _MAX_ATTESTATION_BYTES):
        raise PermissionError(
            "deployment attestation 须为 root-owned、单链接、不可组/全局写 regular file")
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size != before.st_size):
            raise ValueError("deployment attestation 打开期间路径身份变化")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_ATTESTATION_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_ATTESTATION_BYTES:
                raise ValueError("deployment attestation 超过大小上限")
        after = os.fstat(fd)
        if (after.st_size != opened.st_size or after.st_mtime_ns != opened.st_mtime_ns
                or after.st_ctime_ns != opened.st_ctime_ns):
            raise ValueError("deployment attestation 读取期间发生变化")
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    value = _strict_json(raw)
    if raw != _canonical(value):
        raise ValueError("deployment attestation 须为 canonical JSON + 单个换行")
    if validator is not None:
        validate = getattr(validator, "validate", None)
        if callable(validate):
            validate(value)
        elif callable(validator):
            validator(value)
        else:
            raise TypeError("attestation_validator 须为 callable 或带 validate()")
    _validate_attestation_profile(value)
    return value, _hash_bytes(raw)


def _validate_attestation_profile(value: Mapping[str, Any]) -> None:
    if set(value) != {
            "version", "protocol", "issued_at_unix", "service", "isolation",
            "docker", "work_root", "gpu"}:
        raise ValueError("deployment attestation 顶层字段闭包非法")
    if (value.get("version") != 1 or value.get("protocol") != _ATTESTATION_PROTOCOL
            or not _number(value.get("issued_at_unix"), minimum=1.0)):
        raise ValueError("deployment attestation version/protocol/time 非法")
    service = value.get("service")
    if (not isinstance(service, Mapping)
            or set(service) != {"uid", "gid", "groups", "username", "codex_home"}
            or not _int(service.get("uid"), minimum=0)
            or not _int(service.get("gid"), minimum=0)
            or not isinstance(service.get("groups"), list)
            or any(not _int(item, minimum=0) for item in service["groups"])
            or service["groups"] != sorted(set(service["groups"]))
            or not _text(service.get("username"), maximum=256)
            or not _absolute(service.get("codex_home"))):
        raise ValueError("deployment attestation service profile 非法")
    isolation = value.get("isolation")
    if (not isinstance(isolation, Mapping)
            or set(isolation) != {
                "kind", "deployment_id", "machine_id", "boot_id", "hostname", "vm_kind"}
            or isolation.get("kind") != "dedicated-vm"
            or not _text(isolation.get("deployment_id"), maximum=256)
            or re.fullmatch(r"[0-9a-f]{32}", str(isolation.get("machine_id"))) is None
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                str(isolation.get("boot_id"))) is None
            or not _text(isolation.get("hostname"), maximum=253)
            or re.fullmatch(r"[a-z0-9_.-]{1,64}", str(isolation.get("vm_kind"))) is None):
        raise ValueError("deployment attestation isolation profile 非法")
    docker = value.get("docker")
    docker_keys = {
        "socket_path", "socket_uid", "socket_gid", "socket_mode", "daemon_id",
        "daemon_name", "rootless", "security_options", "cgroup_version",
        "cgroup_driver", "resource_mode", "root_dir", "runtimes",
        "min_free_bytes", "min_free_inodes",
    }
    if (not isinstance(docker, Mapping) or set(docker) != docker_keys
            or not _absolute(docker.get("socket_path"))
            or not _int(docker.get("socket_uid"), minimum=0)
            or not _int(docker.get("socket_gid"), minimum=0)
            or not _int(docker.get("socket_mode"), minimum=0)
            or docker["socket_mode"] > 0o7777
            or not _text(docker.get("daemon_id"), maximum=256)
            or not _text(docker.get("daemon_name"), maximum=256)
            or docker.get("rootless") is not True
            or not isinstance(docker.get("security_options"), list)
            or any(not _text(item, maximum=512) for item in docker["security_options"])
            or docker["security_options"] != sorted(set(docker["security_options"]))
            or docker.get("cgroup_version") not in {"1", "2"}
            or not _text(docker.get("cgroup_driver"), maximum=128)
            or docker.get("resource_mode") not in {"cgroup-v1", "cgroup-v2"}
            or not _absolute(docker.get("root_dir"))
            or not isinstance(docker.get("runtimes"), list)
            or any(not _text(item, maximum=256) for item in docker["runtimes"])
            or docker["runtimes"] != sorted(set(docker["runtimes"]))
            or not _int(docker.get("min_free_bytes"), minimum=1)
            or not _int(docker.get("min_free_inodes"), minimum=1)):
        raise ValueError("deployment attestation docker profile 非法")
    work = value.get("work_root")
    if (not isinstance(work, Mapping)
            or set(work) != {"path", "mount_point", "mount_source", "mount_fstype",
                             "quota_provider", "quota_scope", "hard_bytes", "used_bytes",
                             "hard_inodes", "used_inodes"}
            or not _absolute(work.get("path"))
            or not _absolute(work.get("mount_point"))
            or not _text(work.get("mount_source"), maximum=4096)
            or not _text(work.get("mount_fstype"), maximum=128)
            or work.get("quota_provider") != "gpfs-fileset-v1"
            or re.fullmatch(r"fileset:[^\x00-\x1f\x7f]{1,256}",
                            str(work.get("quota_scope"))) is None
            or not _int(work.get("hard_bytes"), minimum=1)
            or not _int(work.get("used_bytes"), minimum=0)
            or work["used_bytes"] > work["hard_bytes"]
            or not _int(work.get("hard_inodes"), minimum=1)
            or not _int(work.get("used_inodes"), minimum=0)
            or work["used_inodes"] > work["hard_inodes"]):
        raise ValueError("deployment attestation work_root/quota profile 非法")
    gpu = value.get("gpu")
    if (not isinstance(gpu, Mapping)
            or set(gpu) != {"memory_bytes_by_uuid"}
            or not isinstance(gpu.get("memory_bytes_by_uuid"), Mapping)
            or any(_GPU_UUID_RE.fullmatch(key) is None
                   or not _int(memory, minimum=1)
                   for key, memory in gpu["memory_bytes_by_uuid"].items())):
        raise ValueError("deployment attestation gpu profile 非法")


def _get(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def evaluate_deployment(*, facts: Mapping[str, Any], attestation: Optional[Mapping[str, Any]],
                        now: float, max_attestation_age_s: int,
                        resources: Mapping[str, Any], sandbox_config: Mapping[str, Any],
                        reserves: Optional[Mapping[str, int]] = None,
                        sandbox_gpu_access: bool,
                        attestation_error: Optional[str] = None) -> list[Dict[str, Any]]:
    """Pure comparison of one facts snapshot and one validated attestation."""
    checks: list[Dict[str, Any]] = []

    def add(name: str, ok: Any, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail[:1000]})

    loaded = attestation is not None
    add("attestation_loaded", loaded,
        "validated canonical attestation" if loaded else (attestation_error or "missing"))
    issued = _get(attestation, "issued_at_unix")
    age = float(now) - float(issued) if loaded and _number(issued, minimum=1.0) else None
    fresh = age is not None and 0 <= age <= max_attestation_age_s
    add("attestation_fresh", fresh,
        f"age_s={age:.3f}" if age is not None else "age unavailable")

    service = _get(facts, "service") or {}
    expected_service = _get(attestation, "service") or {}
    service_non_root = (_get(service, "uid") not in (None, 0)
                        and _get(service, "error") is None)
    add("service_non_root", service_non_root,
        f"uid={_get(service, 'uid')!r}")
    service_identity = loaded and all(
        _get(service, field) == _get(expected_service, field)
        for field in ("uid", "gid", "groups", "username", "codex_home"))
    add("service_identity", service_identity, "live service identity matches attestation")
    codex_home = _get(service, "codex_home_stat") or {}
    auth = _get(codex_home, "auth") or {}
    codex_home_private = (
        _get(codex_home, "direct") is True
        and _get(codex_home, "is_directory") is True
        and _get(codex_home, "uid") == _get(service, "uid")
        and _get(codex_home, "mode") == 0o700
        and _get(auth, "direct") is True and _get(auth, "is_regular") is True
        and _get(auth, "nlink") == 1
        and _get(auth, "uid") == _get(service, "uid")
        and _get(auth, "mode") == 0o600)
    add("codex_home_private", codex_home_private,
        f"home_mode={_get(codex_home, 'mode')!r} auth_mode={_get(auth, 'mode')!r}")
    live_isolation = _get(facts, "isolation") or {}
    isolation_ok = (loaded and _get(attestation, "isolation", "kind") == "dedicated-vm"
                    and _get(live_isolation, "error") is None
                    and _get(live_isolation, "container_kind") is None
                    and all(_get(live_isolation, field)
                            == _get(attestation, "isolation", field)
                            for field in ("machine_id", "boot_id", "hostname", "vm_kind")))
    add("isolation_attested", isolation_ok,
        f"deployment_id={_get(attestation, 'isolation', 'deployment_id')!r}")

    work = _get(facts, "work_root") or {}
    work_private = (
        _get(work, "error") is None and _get(work, "is_directory") is True
        and _get(work, "direct") is True and _get(work, "mode") == 0o700
        and _get(work, "uid") == _get(service, "uid")
        )
    add("work_root_private", work_private,
        f"uid={_get(work, 'uid')!r} gid={_get(work, 'gid')!r} mode={_get(work, 'mode')!r}")
    mount = _get(work, "mount") or {}
    expected_work = _get(attestation, "work_root") or {}
    work_mount = loaded and all((
        _get(work, "path") == _get(expected_work, "path"),
        _get(mount, "mount_point") == _get(expected_work, "mount_point"),
        _get(mount, "source") == _get(expected_work, "mount_source"),
        _get(mount, "fstype") == _get(expected_work, "mount_fstype"),
    ))
    add("work_root_mount", work_mount,
        f"source={_get(mount, 'source')!r} fstype={_get(mount, 'fstype')!r}")
    reserves = dict(reserves or _deployment_reserves({}, resources, sandbox_config))
    required_bytes = reserves["work_free_bytes"]
    hard_bytes = _get(expected_work, "hard_bytes")
    used_bytes = _get(expected_work, "used_bytes")
    hard_inodes = _get(expected_work, "hard_inodes")
    used_inodes = _get(expected_work, "used_inodes")
    quota_ok = (loaded and _int(hard_bytes, minimum=1)
                and hard_bytes >= reserves["work_hard_bytes"]
                and _int(used_bytes, minimum=0) and hard_bytes >= used_bytes
                and hard_bytes - used_bytes >= required_bytes
                and _int(hard_inodes, minimum=1)
                and _int(used_inodes, minimum=0) and hard_inodes >= used_inodes
                and hard_inodes - used_inodes >= reserves["work_free_inodes"])
    add("work_root_hard_quota", quota_ok,
        f"provider={_get(expected_work, 'quota_provider')!r} "
        f"remaining_bytes={hard_bytes - used_bytes if quota_ok else None!r} "
        f"remaining_inodes={hard_inodes - used_inodes if quota_ok else None!r}")

    docker = _get(facts, "docker") or {}
    socket = _get(docker, "socket") or {}
    daemon = _get(docker, "daemon") or {}
    storage = _get(docker, "storage") or {}
    expected_docker = _get(attestation, "docker") or {}
    mode = _get(socket, "mode")
    direct_socket = (
        _get(socket, "direct") is True and _get(socket, "is_socket") is True
        and _get(socket, "realpath") == _get(socket, "path")
        and _get(socket, "path") == sandbox_config["engine_host"].removeprefix("unix://")
        and _get(socket, "path") == _get(expected_docker, "socket_path")
        and _get(socket, "uid") == _get(service, "uid")
        and _get(socket, "uid") == _get(expected_docker, "socket_uid")
        and _get(socket, "gid") == _get(expected_docker, "socket_gid")
        and mode == _get(expected_docker, "socket_mode")
        and _int(mode, minimum=0) and bool(mode & 0o200)
        and not bool(mode & 0o077))
    add("docker_direct_socket", direct_socket,
        f"path={_get(socket, 'path')!r} mode={mode!r}")
    daemon_identity = loaded and all((
        _get(daemon, "id") == _get(expected_docker, "daemon_id"),
        _get(daemon, "name") == _get(expected_docker, "daemon_name"),
        _get(daemon, "rootless") == _get(expected_docker, "rootless"),
        _get(daemon, "security_options") == _get(expected_docker, "security_options"),
        _get(daemon, "cgroup_version") == _get(expected_docker, "cgroup_version"),
        _get(daemon, "cgroup_driver") == _get(expected_docker, "cgroup_driver"),
        _get(daemon, "resource_mode") == _get(expected_docker, "resource_mode"),
        _get(daemon, "root_dir") == _get(expected_docker, "root_dir"),
        _get(daemon, "runtimes") == _get(expected_docker, "runtimes"),
    ))
    add("docker_identity", daemon_identity,
        f"id={_get(daemon, 'id')!r} name={_get(daemon, 'name')!r}")
    add("docker_rootless", _get(daemon, "rootless") is True,
        f"rootless={_get(daemon, 'rootless')!r}")
    resource_mode = _get(daemon, "resource_mode")
    cgroup_ok = (resource_mode in {"cgroup-v1", "cgroup-v2"}
                 and resource_mode == sandbox_config.get("resource_mode"))
    add("docker_cgroup", cgroup_ok,
        f"resource_mode={resource_mode!r} driver={_get(daemon, 'cgroup_driver')!r}")
    limits = _get(daemon, "limits") or {}
    limits_ok = all(_get(limits, key) is True for key in ("memory", "cpu", "pids"))
    add("docker_resource_limits", limits_ok,
        f"memory={_get(limits, 'memory')!r} cpu={_get(limits, 'cpu')!r} "
        f"pids={_get(limits, 'pids')!r}")
    attested_storage_reserve = (
        loaded
        and _int(_get(expected_docker, "min_free_bytes"), minimum=1)
        and _int(_get(expected_docker, "min_free_inodes"), minimum=1)
        and _get(expected_docker, "min_free_bytes") >= reserves["docker_free_bytes"]
        and _get(expected_docker, "min_free_inodes") >= reserves["docker_free_inodes"])
    docker_space = (
        loaded and _int(_get(storage, "free_bytes"), minimum=1)
        and _int(_get(storage, "free_inodes"), minimum=1)
        and attested_storage_reserve
        and _get(storage, "free_bytes") >= _get(expected_docker, "min_free_bytes")
        and _get(storage, "free_inodes") >= _get(expected_docker, "min_free_inodes"))
    add("docker_storage_headroom", docker_space,
        f"free_bytes={_get(storage, 'free_bytes')!r} "
        f"free_inodes={_get(storage, 'free_inodes')!r}")

    inventory = _get(facts, "gpu", "inventory") or []
    live_gpu = {}
    gpu_shape_ok = isinstance(inventory, list) and _get(facts, "gpu", "error") is None
    if gpu_shape_ok:
        for item in inventory:
            uuid, memory = _get(item, "uuid"), _get(item, "memory_bytes")
            if (not isinstance(uuid, str) or _GPU_UUID_RE.fullmatch(uuid) is None
                    or uuid in live_gpu or not _int(memory, minimum=1)):
                gpu_shape_ok = False
                break
            live_gpu[uuid] = memory
    expected_gpu = _get(attestation, "gpu", "memory_bytes_by_uuid") or {}
    gpu_inventory = loaded and gpu_shape_ok and live_gpu == dict(expected_gpu)
    required_gpus = int(resources["gpus"])
    required_memory = math.ceil(float(resources["gpu_mem_gb"]) * (1024 ** 3))
    capable = sum(1 for memory in live_gpu.values() if memory >= required_memory)
    gpu_policy = gpu_inventory and capable >= required_gpus
    add("gpu_inventory", gpu_policy,
        f"visible={len(live_gpu)} capable={capable} required={required_gpus}")
    gpu_access_ok = required_gpus == 0 or sandbox_gpu_access is True
    add("sandbox_gpu_access", gpu_access_ok,
        f"sandbox_gpu_access={sandbox_gpu_access!r} required_gpus={required_gpus}")
    return checks


class DeploymentPreflight:
    """Probe, evaluate and publish one deployment receipt for ``owner_id``."""

    def __init__(self, work_root: Path | str, policy: Mapping[str, Any], sandbox: Any,
                 owner_id: str, attestation_validator: Any = None,
                 sandbox_gpu_access: bool = False, owner_guard: Optional[Callable[[], None]] = None,
                 probe_backend: Any = None, clock: Optional[Callable[[], float]] = None):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.policy = policy
        self.sandbox = sandbox
        self.owner_id = owner_id
        self.attestation_validator = attestation_validator
        if not isinstance(sandbox_gpu_access, bool):
            raise ValueError("sandbox_gpu_access 须为 bool")
        self.sandbox_gpu_access = sandbox_gpu_access
        self.owner_guard = owner_guard or (lambda: None)
        self.probe_backend = probe_backend or SystemProbeBackend()
        self.clock = clock or time.time
        (self.deployment, self.resources, self.sandbox_config,
         self.reserves) = _validate_inputs(policy, sandbox, owner_id)
        self.receipt_path = (
            self.work_root / "state" / "deployment"
            / f"deployment-{owner_id}.json")

    def _collect(self) -> Dict[str, Any]:
        try:
            collect = getattr(self.probe_backend, "collect", None)
            if callable(collect):
                value = collect(work_root=self.work_root, sandbox=self.sandbox)
            elif callable(self.probe_backend):
                value = self.probe_backend(work_root=self.work_root, sandbox=self.sandbox)
            else:
                raise TypeError("probe_backend 须为 callable 或带 collect()")
            if not isinstance(value, Mapping):
                raise TypeError("deployment facts 须为 mapping")
            facts = dict(value)
            _canonical(facts)  # reject non-JSON/non-finite injected facts
            return facts
        except Exception as error:
            return _empty_facts("probe backend: " + _bounded_error(error))

    def run(self) -> Dict[str, Any]:
        self.owner_guard()
        facts = self._collect()
        self.owner_guard()

        attestation = None
        attestation_hash = None
        attestation_error = None
        path_value = self.deployment["attestation_path"]
        if path_value is None:
            attestation_error = "attestation_path 未配置"
        else:
            try:
                attestation, attestation_hash = _read_attestation(
                    Path(path_value), self.attestation_validator)
            except Exception as error:
                attestation_error = _bounded_error(error)

        now = self.clock()
        if not _number(now, minimum=1.0):
            raise ValueError("deployment preflight clock 须返回有限 Unix time")
        now = float(now)

        checks = evaluate_deployment(
            facts=facts, attestation=attestation, now=now,
            max_attestation_age_s=self.deployment["max_attestation_age_s"],
            resources=self.resources, sandbox_config=self.sandbox_config,
            reserves=self.reserves,
            sandbox_gpu_access=self.sandbox_gpu_access,
            attestation_error=attestation_error)
        all_checks = all(item["ok"] for item in checks)
        production_ready = self.deployment["mode"] == "production" and all_checks
        policy_projection = {
            "deployment": self.deployment, "resources": self.resources,
            "sandbox": self.sandbox_config,
            "reserves": self.reserves,
            "sandbox_gpu_access": self.sandbox_gpu_access,
        }
        receipt = {
            "version": 1, "protocol": _PROTOCOL, "owner_id": self.owner_id,
            "mode": self.deployment["mode"], "checked_at_unix": now,
            "production_ready": production_ready,
            "policy_hash": _value_hash(policy_projection),
            "required_reserves": self.reserves,
            "facts_hash": _value_hash(facts), "facts": facts,
            "attestation": {
                "path": path_value, "sha256": attestation_hash,
                "error": attestation_error, "value": attestation,
            },
            "checks": checks,
        }
        self.owner_guard()
        atomic_write_receipt(self.receipt_path, receipt)
        self.owner_guard()
        if self.deployment["mode"] == "production" and not production_ready:
            failed = [item["name"] for item in checks if not item["ok"]]
            raise DeploymentPreflightError(
                "production deployment preflight 失败: " + ",".join(failed),
                receipt=receipt, receipt_path=self.receipt_path)
        return receipt


__all__ = [
    "DeploymentPreflight", "DeploymentPreflightError", "SystemProbeBackend",
    "evaluate_deployment", "longest_mount",
]
