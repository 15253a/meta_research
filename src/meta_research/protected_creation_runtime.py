from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import threading
from typing import Literal


_UID = 65534
_INPUT_LIMIT = 16 * 1024 * 1024
_STOP_SECONDS = 12.0
_HELPER = r'''
import ctypes, errno, json, os, signal, subprocess, sys, time

configuration = json.load(open(sys.argv[1], encoding="utf-8"))
libc = ctypes.CDLL(None, use_errno=True)
stopping = False

def prctl(option, argument):
    if libc.prctl(option, argument, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "protected runtime prerequisite")

def stop(signum, frame):
    global stopping
    stopping = True

def pidfd_open(pid):
    descriptor = libc.syscall(434, pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "protected runtime process handle")
    return descriptor

def pidfd_kill(descriptor):
    if libc.syscall(424, descriptor, signal.SIGKILL, 0, 0):
        raise OSError(ctypes.get_errno(), "protected runtime process stop")

def boundary():
    os.chroot(configuration["jail"])
    os.chdir("/workspace")
    prctl(38, 1)
    for capability in range(64):
        result = libc.prctl(24, capability, 0, 0, 0)
        if result and ctypes.get_errno() != errno.EINVAL:
            raise OSError(ctypes.get_errno(), "protected runtime capabilities")
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)

def receipt(status, code, ended, error=None):
    path = configuration["receipt"]
    temporary = path + ".new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"status": status, "returncode": code, "descendants_ended": ended,
            "error_type": type(error).__name__ if error else None,
            "error_errno": getattr(error, "errno", None)}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

def descendants():
    parents = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            content = open(entry.path + "/stat", encoding="utf-8").read()
            values = content[content.rfind(")") + 2:].split()
            parents[int(entry.name)] = (int(values[1]), int(values[19]))
        except FileNotFoundError:
            continue
    owned = {os.getpid()}
    while True:
        found = {pid for pid, (parent, _) in parents.items() if parent in owned}
        expanded = owned | found
        if expanded == owned:
            break
        owned = expanded
    return {pid: parents[pid][1] for pid in owned if pid != os.getpid()}

def kill_descendants():
    for pid, started in descendants().items():
        try:
            descriptor = pidfd_open(pid)
            try:
                content = open("/proc/" + str(pid) + "/stat", encoding="utf-8").read()
                if int(content[content.rfind(")") + 2:].split()[19]) == started:
                    pidfd_kill(descriptor)
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    prctl(36, 1)
    if os.uname().machine != "x86_64":
        raise RuntimeError("protected runtime architecture")
    os.close(pidfd_open(os.getpid()))
    native = subprocess.Popen(configuration["argv"], env=configuration["environment"],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, close_fds=True,
        start_new_session=True, preexec_fn=boundary)
except BaseException as error:
    receipt("launch_failed", 125, True, error)
    sys.exit(125)

returncode = None
proof_failed = False
while True:
    no_children = False
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            no_children = True
            break
        if pid == 0:
            break
        if pid == native.pid:
            returncode = os.waitstatus_to_exitcode(status)
    if no_children:
        if returncode is None or proof_failed:
            receipt("unknown_outcome", returncode, False)
            sys.exit(125)
        receipt("stopped" if stopping else "completed", returncode, True)
        sys.exit(0)
    if stopping or returncode is not None:
        try:
            kill_descendants()
        except BaseException:
            proof_failed = True
    time.sleep(0.02)
'''


@dataclass(frozen=True, slots=True)
class NativeCreationCall:
    argv: tuple[str, ...]
    prompt: str
    timeout_seconds: float | None
    environment: dict[str, str]
    read_only_inputs: tuple[Path, ...]
    output_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _RuntimeEntry:
    kind: Literal["elf", "node_wrapper"]
    native: Path
    files: tuple[Path, ...]
    link_target: str | None = None


class ProtectedCreationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProtectedCreationRuntime:
    def __init__(self, jail_root: Path, executable: Path, credentials_home: Path) -> None:
        self.jail_root = _absolute(jail_root)
        self.executable = _absolute(executable)
        self.credentials_home = _absolute(credentials_home)
        self.work_directory = self.jail_root / "workspace"
        self._control = self.jail_root.parent / ("." + self.jail_root.name + "-control")
        self._guard = threading.Lock()
        self._state = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._receipt: Path | None = None
        self._stop_before_launch = False
        self._starting = False
        self._has_run = False
        self._entry_command = "/bin/codex"

    def run(self, call: NativeCreationCall) -> subprocess.CompletedProcess[str]:
        self._check_call(call)
        with self._state:
            if not self._guard.acquire(blocking=False):
                raise ProtectedCreationError("protected_creation_active")
            self._starting = True
        try:
            self._check_platform()
            _directory(self._control, 0o700)
            _owned_directory(self._control, 0, private=True)
            with _runtime_lock(self._control / "lock"):
                self._check_previous_seal()
                if self._process is not None and self._process.poll() is None:
                    raise ProtectedCreationError("protected_creation_unknown_outcome")
                with self._state:
                    if self._stop_before_launch:
                        return self._cancelled_call(call)
                self._prepare_runtime()
                arguments, environment, outputs, prompt = self._prepare_call(call)
                launch = self._control / ("launch-" + secrets.token_hex(12) + ".json")
                receipt = launch.with_suffix(".receipt.json")
                _write_private(launch, json.dumps({"jail": str(self.jail_root),
                    "argv": arguments, "environment": environment, "receipt": str(receipt)}).encode())
                with self._state:
                    if self._stop_before_launch:
                        return self._cancelled_call(call)
                    self._receipt = receipt
                    self._process = None
                    _replace_private(self._control / "active.json", json.dumps({"receipt": receipt.name}).encode(), 0o600)
                    self._process = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-c", _HELPER, str(launch)],
                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                        cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                        close_fds=True, start_new_session=True)
                    self._starting = False
                try:
                    stdout, stderr = self._process.communicate(prompt, timeout=call.timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    outcome = self.request_stop()
                    if not outcome["descendants_ended"]:
                        raise ProtectedCreationError("protected_creation_unknown_outcome") from error
                    stdout, stderr = self._process.communicate()
                    raise subprocess.TimeoutExpired(call.argv, call.timeout_seconds,
                        output=stdout, stderr=stderr) from error
                outcome = self._read_receipt()
                if not outcome.get("descendants_ended"):
                    raise ProtectedCreationError("protected_creation_unknown_outcome")
                if outcome.get("status") == "launch_failed":
                    raise ProtectedCreationError("protected_creation_launch_failed")
                for source, destination in outputs:
                    self._publish_output(source, destination)
                return subprocess.CompletedProcess(call.argv, int(outcome["returncode"]), stdout, stderr)
        finally:
            with self._state:
                self._starting = False
                self._stop_before_launch = False
                self._has_run = True
                self._guard.release()

    def _cancelled_call(self, call: NativeCreationCall) -> subprocess.CompletedProcess[str]:
        receipt = self._control / ("cancel-" + secrets.token_hex(12) + ".receipt.json")
        _write_private(receipt, json.dumps({"status": "stopped", "returncode": -signal.SIGTERM,
            "descendants_ended": True}).encode())
        _replace_private(self._control / "active.json", json.dumps({"receipt": receipt.name}).encode(), 0o600)
        self._receipt = receipt
        self._process = None
        return subprocess.CompletedProcess(call.argv, -signal.SIGTERM, "", "")

    def request_stop(self) -> dict[str, object]:
        with self._state:
            if self._starting:
                self._stop_before_launch = True
                return {"status": "unknown_outcome", "descendants_ended": False}
            process = self._process
            if process is None:
                try:
                    previous = self._check_previous_seal()
                except ProtectedCreationError:
                    return {"status": "unknown_outcome", "descendants_ended": False}
                if previous is None and not self._has_run:
                    self._stop_before_launch = True
                return {"status": previous["status"] if previous else "not_running", "descendants_ended": True}
            if process.poll() is None:
                try:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=_STOP_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    return {"status": "unknown_outcome", "descendants_ended": False}
            outcome = self._read_receipt()
            if not outcome.get("descendants_ended"):
                return {"status": "unknown_outcome", "descendants_ended": False}
            return {"status": outcome["status"], "descendants_ended": True}

    def _read_receipt(self) -> dict[str, object]:
        if self._receipt is None:
            return {}
        return _receipt_value(self._receipt)

    def _check_previous_seal(self) -> dict[str, object] | None:
        active = self._control / "active.json"
        try:
            with _regular_reader(active) as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return
        except (OSError, ValueError, ProtectedCreationError) as error:
            raise ProtectedCreationError("protected_creation_unknown_outcome") from error
        try:
            _owned_directory(self._control, 0, private=True)
        except (OSError, ProtectedCreationError) as error:
            raise ProtectedCreationError("protected_creation_unknown_outcome") from error
        name = value.get("receipt") if isinstance(value, dict) else None
        if not isinstance(name, str) or Path(name).name != name:
            raise ProtectedCreationError("protected_creation_unknown_outcome")
        previous = _receipt_value(self._control / name)
        if not previous.get("descendants_ended"):
            raise ProtectedCreationError("protected_creation_unknown_outcome")
        return previous

    @staticmethod
    def _check_platform() -> None:
        if sys.platform != "linux" or os.geteuid() != 0 or os.uname().machine != "x86_64":
            raise ProtectedCreationError("protected_creation_runtime_unavailable")

    @staticmethod
    def _check_call(call: NativeCreationCall) -> None:
        if (not call.argv or any(not isinstance(value, str) or "\0" in value for value in call.argv)
            or not isinstance(call.prompt, str) or (call.timeout_seconds is not None
                and (not isinstance(call.timeout_seconds, (int, float))
                    or not math.isfinite(call.timeout_seconds) or call.timeout_seconds <= 0))
            or any(not isinstance(key, str) or not isinstance(value, str)
                or not key or "=" in key or "\0" in key + value for key, value in call.environment.items())):
            raise ProtectedCreationError("protected_creation_call_invalid")

    def _prepare_runtime(self) -> None:
        entry = _runtime_entry(self.executable)
        native = entry.native
        self._entry_command = str(self.executable) if entry.kind == "node_wrapper" else "/bin/codex"
        identity = {"kind": entry.kind, "configured_entry": str(self.executable),
            "entry_command": self._entry_command, "link_target": entry.link_target,
            "native_sha256": _runtime_digest(native),
            "entry_files": {str(path): _runtime_digest(path) for path in entry.files}}
        marker = self._control / "runtime.json"
        if marker.exists():
            with _regular_reader(marker) as stream:
                manifest = json.load(stream)
            if manifest.get("entry") != identity:
                raise ProtectedCreationError("protected_creation_runtime_changed")
            for relative, digest in manifest.get("files", {}).items():
                path = self.jail_root / relative
                if _runtime_digest(path) != digest:
                    raise ProtectedCreationError("protected_creation_runtime_changed")
            _owned_directory(self.work_directory, _UID)
            return
        if self.jail_root.exists():
            raise ProtectedCreationError("protected_creation_jail_unrecognized")
        _directory(self.jail_root, 0o755)
        staged: dict[str, str] = {}

        def copy(source: Path, target: Path, *, dependencies: bool = False) -> None:
            relative = target.relative_to(self.jail_root).as_posix()
            if relative in staged:
                return
            resolved = source.resolve(strict=True)
            _trusted_runtime(resolved)
            _directory(target.parent, 0o755)
            with _regular_reader(resolved) as stream:
                content = stream.read()
            _write_private(target, content, 0o555 if os.access(resolved, os.X_OK) else 0o444)
            staged[relative] = hashlib.sha256(content).hexdigest()
            if dependencies:
                completed = subprocess.run(["/usr/bin/ldd", str(resolved)],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"}, capture_output=True,
                    text=True, timeout=30, check=False)
                if "not found" in completed.stdout:
                    raise ProtectedCreationError("protected_creation_dependency_unavailable")
                for name in re.findall(r"(?:=>\s*)?(/[^\s()]+)", completed.stdout):
                    library = Path(os.path.normpath(name))
                    if library.is_file():
                        copy(library, self.jail_root / str(library).lstrip("/"))

        copy(native, self.jail_root / "bin/codex", dependencies=True)
        if entry.kind == "node_wrapper":
            for source in entry.files:
                copy(source, self.jail_root / str(source).lstrip("/"))
            for source in (native, native.parent / "codex-code-mode-host", native.parent.parent / "codex-path/rg"):
                copy(source, self.jail_root / str(source).lstrip("/"), dependencies=True)
            if entry.link_target is not None:
                target = self.jail_root / str(self.executable).lstrip("/")
                _directory(target.parent, 0o755)
                target.symlink_to(entry.link_target)
            copy(Path("/usr/bin/env"), self.jail_root / "usr/bin/env", dependencies=True)
        if native.name == "codex":
            for source, target in ((native.parent / "codex-code-mode-host", "bin/codex-code-mode-host"),
                (native.parent.parent / "codex-path/rg", "bin/rg")):
                if not source.is_file():
                    raise ProtectedCreationError("protected_creation_dependency_unavailable")
                copy(source, self.jail_root / target, dependencies=True)
        for name in ("bash", "ls", "cat", "cp", "ln", "mkdir", "sleep"):
            copy(Path("/bin") / name, self.jail_root / "bin" / name, dependencies=True)
        copy(Path("/bin/bash"), self.jail_root / "bin/sh", dependencies=True)
        node = next((path for path in (Path("/usr/local/bin/node"), Path("/usr/bin/node")) if path.is_file()), None)
        if node is None:
            raise ProtectedCreationError("protected_creation_dependency_unavailable")
        copy(node, self.jail_root / "bin/node", dependencies=True)
        python = Path(sys.executable).resolve(strict=True)
        copy(python, self.jail_root / "python/bin/python3", dependencies=True)
        stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
        for directory, subdirectories, names in os.walk(stdlib, followlinks=False):
            subdirectories[:] = [name for name in subdirectories if name not in {"site-packages", "__pycache__", "tkinter"}]
            for name in names:
                source = Path(directory) / name
                if source.suffix != ".pyc" and not name.startswith("_tkinter."):
                    copy(source, self.jail_root / "python/lib" / stdlib.name / source.relative_to(stdlib),
                        dependencies=source.suffix == ".so")
        for library in (Path(sys.base_prefix) / "lib").glob("libpython*.so*"):
            copy(library, self.jail_root / "python/lib" / library.name, dependencies=True)
        for name in ("python", "python3"):
            (self.jail_root / "bin" / name).symlink_to("/python/bin/python3")
        for name in ("resolv.conf", "hosts", "nsswitch.conf", "ssl/certs/ca-certificates.crt"):
            copy(Path("/etc") / name, self.jail_root / "etc" / name)
        _write_private(self.jail_root / "etc/passwd", b"nobody:x:65534:65534:creation:/home/creation:/bin/bash\n", 0o444)
        _write_private(self.jail_root / "etc/group", b"nogroup:x:65534:\n", 0o444)
        for name in ("workspace", "outputs", "home/creation", "home/creation/.codex", "tmp", "cache"):
            path = self.jail_root / name
            _directory(path, 0o700)
            os.chown(path, _UID, _UID)
        _directory(self.jail_root / "inputs", 0o755)
        for name in ("proc", "proc/self", "dev"):
            _directory(self.jail_root / name, 0o555)
        (self.jail_root / "proc/self/exe").symlink_to("/bin/codex")
        for name, minor, mode in (("null", 3, 0o666), ("urandom", 9, 0o444), ("random", 8, 0o444)):
            os.mknod(self.jail_root / "dev" / name, stat.S_IFCHR | mode, os.makedev(1, minor))
        for name in ("auth.json", "config.toml"):
            source = self.credentials_home / name
            if not source.exists():
                continue
            with _regular_reader(source) as stream:
                content = stream.read(_INPUT_LIMIT + 1)
            if len(content) > _INPUT_LIMIT:
                raise ProtectedCreationError("protected_creation_credentials_invalid")
            destination = self.jail_root / "home/creation/.codex" / name
            _write_private(destination, content)
            os.chown(destination, _UID, _UID)
        _write_private(marker, json.dumps({"entry": identity, "files": staged}).encode())

    def _prepare_call(self, call: NativeCreationCall) -> tuple[list[str], dict[str, str], list[tuple[Path, Path]], str]:
        mappings = {str(self.executable): self._entry_command, str(self.work_directory): "/workspace"}
        with _parent_descriptor(self.jail_root / "inputs" / "slot") as directory:
            for name in os.listdir(directory):
                details = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(details.st_mode) or details.st_uid != 0:
                    raise ProtectedCreationError("protected_creation_jail_unrecognized")
                os.unlink(name, dir_fd=directory)
        total = 0
        for index, supplied in enumerate(call.read_only_inputs):
            source = _absolute(supplied)
            with _regular_reader(source) as stream:
                details = os.fstat(stream.fileno())
                if details.st_nlink != 1:
                    raise ProtectedCreationError("protected_creation_input_linked")
                content = stream.read(_INPUT_LIMIT - total + 1)
            total += len(content)
            if total > _INPUT_LIMIT:
                raise ProtectedCreationError("protected_creation_input_too_large")
            name = f"{index:04d}-" + source.name
            target = self.jail_root / "inputs" / name
            _replace_private(target, content, 0o444)
            mappings[str(source)] = "/inputs/" + name
        outputs = []
        for index, supplied in enumerate(call.output_paths):
            destination = _absolute(supplied)
            if destination.is_relative_to(self.jail_root):
                if not destination.is_relative_to(self.work_directory):
                    raise ProtectedCreationError("protected_creation_output_invalid")
                source = destination
                mapped = "/workspace/" + destination.relative_to(self.work_directory).as_posix()
            else:
                name = secrets.token_hex(8) + f"-{index:04d}-" + destination.name
                source = self.jail_root / "outputs" / name
                mapped = "/outputs/" + name
            outputs.append((source, destination))
            mappings[str(destination)] = mapped
        arguments = [_mapped(value, mappings, self.work_directory) for value in call.argv]
        if arguments[0] not in {self._entry_command, self.executable.name, "codex"}:
            raise ProtectedCreationError("protected_creation_executable_mismatch")
        arguments[0] = self._entry_command
        environment = {key: _mapped(value, mappings, self.work_directory) for key, value in call.environment.items()}
        environment.update({"HOME": "/home/creation", "USERPROFILE": "/home/creation",
            "CODEX_HOME": "/home/creation/.codex", "CODEX_SQLITE_HOME": "/home/creation/.codex",
            "PATH": "/bin:/python/bin", "SHELL": "/bin/bash", "PYTHONHOME": "/python",
            "PYTHONPATH": "", "NODE_PATH": "", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CONFIG_HOME": "/home/creation/.config", "XDG_DATA_HOME": "/home/creation/.local/share",
            "XDG_STATE_HOME": "/home/creation/.local/state", "XDG_CACHE_HOME": "/cache",
            "PIP_CACHE_DIR": "/cache/pip", "npm_config_cache": "/cache/npm", "UV_CACHE_DIR": "/cache/uv",
            "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp", "SQLITE_TMPDIR": "/tmp"})
        return arguments, environment, outputs, _mapped(call.prompt, mappings, self.work_directory)

    def _publish_output(self, source: Path, destination: Path) -> None:
        try:
            with _regular_reader(source) as stream:
                details = os.fstat(stream.fileno())
                if details.st_nlink != 1 or details.st_size > _INPUT_LIMIT:
                    raise ProtectedCreationError("protected_creation_output_invalid")
                if source == destination:
                    return
                with _parent_descriptor(destination) as parent:
                    try:
                        existing = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
                        raise ProtectedCreationError("protected_creation_output_invalid")
                    temporary = ".creation-" + secrets.token_hex(12)
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            shutil.copyfileobj(stream, output)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
        except FileNotFoundError:
            return
        except OSError as error:
            raise ProtectedCreationError("protected_creation_output_invalid") from error


def _absolute(path: Path) -> Path:
    value = Path(path)
    if ".." in value.parts:
        raise ProtectedCreationError("protected_creation_path_invalid")
    return value.absolute()


@contextmanager
def _parent_descriptor(path: Path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in _absolute(path).parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _regular_reader(path: Path):
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProtectedCreationError("protected_creation_file_invalid")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _directory(path: Path, mode: int) -> None:
    value = _absolute(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, component in enumerate(value.parts[1:]):
            try:
                os.mkdir(component, mode=mode if index == len(value.parts) - 2 else 0o755,
                    dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _owned_directory(path: Path, uid: int, *, private: bool = False) -> None:
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        details = os.fstat(descriptor)
        if details.st_uid != uid or (private and details.st_mode & 0o077):
            raise ProtectedCreationError("protected_creation_jail_unrecognized")
    finally:
        os.close(descriptor)


def _write_private(path: Path, content: bytes, mode: int = 0o600) -> None:
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _replace_private(path: Path, content: bytes, mode: int) -> None:
    with _parent_descriptor(path) as parent:
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            pass
    _write_private(path, content, mode)


@contextmanager
def _runtime_lock(path: Path):
    import fcntl
    with _parent_descriptor(path) as parent:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProtectedCreationError("protected_creation_active") from error
        yield
    finally:
        os.close(descriptor)


def _trusted_runtime(path: Path) -> None:
    with _regular_reader(path) as stream:
        details = os.fstat(stream.fileno())
        if details.st_uid != 0 or details.st_mode & 0o022:
            raise ProtectedCreationError("protected_creation_runtime_untrusted")


def _runtime_digest(path: Path) -> str:
    _trusted_runtime(path)
    with _regular_reader(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _receipt_value(path: Path) -> dict[str, object]:
    try:
        with _regular_reader(path) as stream:
            value = json.load(stream)
        if (isinstance(value, dict) and value.get("descendants_ended") is True
            and value.get("status") in {"completed", "stopped", "launch_failed"}
            and type(value.get("returncode")) is int):
            return value
    except (OSError, ValueError, ProtectedCreationError):
        pass
    return {}


def _runtime_entry(executable: Path) -> _RuntimeEntry:
    source = executable.resolve(strict=True)
    with _regular_reader(source) as stream:
        if stream.read(4) == b"\x7fELF":
            return _RuntimeEntry("elf", source, ())
    if source.name == "codex.js" and source.parent.name == "bin" and source.parent.parent.name == "codex":
        package = source.parent.parent
        platform_package = package.parent / "codex-linux-x64"
        vendor = platform_package / "vendor/x86_64-unknown-linux-musl"
        native = vendor / "bin/codex"
        if native.is_file():
            with _regular_reader(native) as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ProtectedCreationError("protected_creation_executable_unsupported")
            files = [source, package / "package.json", platform_package / "package.json"]
            if any(not path.is_file() for path in files):
                raise ProtectedCreationError("protected_creation_dependency_unavailable")
            if (vendor / "codex-package.json").is_file():
                files.append(vendor / "codex-package.json")
            link_target = None
            if executable != source:
                if not executable.is_symlink():
                    raise ProtectedCreationError("protected_creation_executable_unsupported")
                link_target = str(executable.readlink())
                linked = Path(os.path.normpath(executable.parent / link_target))
                if linked != source:
                    raise ProtectedCreationError("protected_creation_executable_unsupported")
            return _RuntimeEntry("node_wrapper", native, tuple(files), link_target)
    raise ProtectedCreationError("protected_creation_executable_unsupported")


def _mapped(value: str, mappings: dict[str, str], work_directory: Path) -> str:
    for source, target in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        ending = r"(?=$|[\s\"',\]\)}])"
        if source == str(work_directory):
            ending = r"(?=$|/|[\s\"',\]\)}])"
        value = re.sub(r"(?<![\w./-])" + re.escape(source) + ending, lambda match: target, value)
    return value
