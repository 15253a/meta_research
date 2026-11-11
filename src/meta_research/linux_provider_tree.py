"""One Linux provider tree, with child wait ownership confined to a helper."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import time


class ProviderTreeError(RuntimeError):
    pass


def _pidfd_open(pid: int) -> int:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ProviderTreeError("provider_tree_platform_unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.syscall(434, pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "provider pidfd_open")
    return descriptor


def _signal_pidfd(descriptor: int, signum: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(424, descriptor, signum, 0, 0):
        raise OSError(ctypes.get_errno(), "provider pidfd_send_signal")


class LinuxProviderJob:
    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._control: int | None = None
        self._pidfd: int | None = None
        self._result: dict[str, object] | None = None

    def spawn(self, argv: list[str], **options: object) -> subprocess.Popen[bytes]:
        descriptor = _pidfd_open(os.getpid())
        try:
            _signal_pidfd(descriptor, 0)
        finally:
            os.close(descriptor)
        read_fd, write_fd = os.pipe()
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", __name__, str(write_fd), *argv],
                pass_fds=(write_fd,), **options,
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self._process = process
        self._control = read_fd
        try:
            self._pidfd = _pidfd_open(process.pid)
        except OSError:
            process.terminate()
            process.wait(timeout=3)
            raise ProviderTreeError("provider_tree_handle_unavailable")
        if not select.select([read_fd], [], [], 5)[0] or os.read(read_fd, 1) != b"R":
            raise ProviderTreeError("provider_tree_start_unconfirmed")
        return process

    def result(self) -> dict[str, object]:
        if self._result is not None:
            return self._result
        if self._process is None or self._process.poll() is None:
            raise ProviderTreeError("provider_tree_not_terminal")
        if self._control is None:
            raise ProviderTreeError("provider_tree_proof_missing")
        raw = os.read(self._control, 4097)
        os.close(self._control)
        self._control = None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ProviderTreeError("provider_tree_proof_missing") from error
        if (
            self._process.returncode != 0
            or not isinstance(result, dict)
            or set(result) != {"returncode", "descendants_terminated", "launch_failed", "tree_closed", "native_started"}
            or type(result["native_started"]) is not bool
            or (type(result["returncode"]) is not int if result["native_started"] else result["returncode"] is not None)
            or type(result["descendants_terminated"]) is not bool
            or type(result["launch_failed"]) is not bool
            or result["tree_closed"] is not True
        ):
            raise ProviderTreeError("provider_tree_proof_invalid")
        self._result = result
        return result

    def active_process_count(self) -> int:
        if self._process is None:
            return 0
        if self._process.poll() is None:
            return 1
        self.result()
        return 0

    def terminate(self, exit_code: int = 1) -> bool:
        if self._process is None:
            return True
        if self._process.poll() is None:
            assert self._pidfd is not None
            try:
                _signal_pidfd(self._pidfd, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                return False
        self.result()
        return True

    def close(self) -> None:
        try:
            if self._process is not None and self._process.poll() is None:
                self.terminate()
        finally:
            for descriptor in (self._pidfd, self._control):
                if descriptor is not None:
                    os.close(descriptor)
            self._pidfd = self._control = None


def _signal_children(signum: int) -> None:
    children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    for child in children:
        try:
            descriptor = _pidfd_open(int(child))
            try:
                _signal_pidfd(descriptor, signum)
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            pass


def _run(control: int, argv: list[str]) -> None:
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.set_inheritable(control, False)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "provider subreaper")
    descriptor = _pidfd_open(os.getpid())
    try:
        _signal_pidfd(descriptor, 0)
    finally:
        os.close(descriptor)
    os.write(control, b"R")
    native = None
    code = None
    launch_failed = False
    if not stopping:
        try:
            native = subprocess.Popen(argv, close_fds=True, start_new_session=True)
        except OSError:
            launch_failed = True
    cleanup_started = None
    descendants_terminated = False
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                if code is None and native is not None:
                    raise ProviderTreeError("provider_native_status_missing")
                os.write(control, json.dumps({
                    "returncode": code, "descendants_terminated": descendants_terminated,
                    "launch_failed": launch_failed, "tree_closed": True,
                    "native_started": native is not None,
                }).encode())
                return
            if pid == 0:
                break
            if native is not None and pid == native.pid:
                code = os.waitstatus_to_exitcode(status)
                native.returncode = code
        if stopping or code is not None:
            now = time.monotonic()
            if cleanup_started is None:
                cleanup_started = now
                descendants_terminated = code is not None and native is not None
            if now - cleanup_started > 2:
                raise ProviderTreeError("provider_descendant_cleanup_failed")
            _signal_children(signal.SIGKILL if now - cleanup_started > .5 else signal.SIGTERM)
        time.sleep(.01)


def main() -> int:
    try:
        _run(int(sys.argv[1]), sys.argv[2:])
    except (OSError, ValueError, ProviderTreeError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
