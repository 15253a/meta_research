from __future__ import annotations

import argparse
import errno
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from meta_research import __version__
from meta_research.paths import (
    DataRoot,
    RuntimeState,
    append_daemon_event,
    prepare_data_root,
    read_control_key,
    write_runtime_state,
)


LockOperation = Callable[[int, bool], None]


class DaemonFileLock:
    """Hold the one-daemon-per-data-root lock for this process lifetime."""

    def __init__(
        self,
        path: Path,
        *,
        platform_name: str | None = None,
        lock_operation: LockOperation | None = None,
    ) -> None:
        self._path = path
        self._platform_name = platform_name or os.name
        self._lock_operation = lock_operation
        self._handle: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            raise RuntimeError("daemon file lock is already acquired")
        operation = self._lock_operation or _platform_lock_operation(
            self._platform_name
        )
        handle = self._path.open("a+b")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass
        if self._platform_name == "nt":
            try:
                _ensure_windows_lock_byte(handle)
            except Exception:
                handle.close()
                raise
        try:
            operation(handle.fileno(), True)
        except OSError as error:
            handle.close()
            if error.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                return False
            raise
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            operation = self._lock_operation or _platform_lock_operation(
                self._platform_name
            )
            operation(handle.fileno(), False)
        finally:
            handle.close()


def _platform_lock_operation(platform_name: str) -> LockOperation:
    if platform_name == "posix":
        return _posix_lock_operation
    if platform_name == "nt":
        return _windows_lock_operation
    raise RuntimeError(f"unsupported daemon locking platform: {platform_name}")


def _posix_lock_operation(descriptor: int, acquire: bool) -> None:
    import fcntl

    operation = fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN
    fcntl.flock(descriptor, operation)


def _windows_lock_operation(descriptor: int, acquire: bool) -> None:
    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    operation = msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK
    msvcrt.locking(descriptor, operation, 1)


def _ensure_windows_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0, os.SEEK_SET)


def main() -> int:
    parser = argparse.ArgumentParser(prog="meta-research-daemon")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("daemon host must be loopback")
    if not 1 <= args.port <= 65535:
        parser.error("daemon port must be between 1 and 65535")

    data_root = prepare_data_root(args.data_root)
    daemon_lock = DaemonFileLock(data_root.daemon_lock)
    if not daemon_lock.acquire():
        return 2
    try:
        return _serve(args, data_root)
    finally:
        daemon_lock.release()


def _serve(args: argparse.Namespace, data_root: DataRoot) -> int:
    import uvicorn

    from meta_research.composition import build_production_runtime
    from meta_research.web import create_app

    started_at = time.time()
    base_url = _base_url(args.host, args.port)
    append_daemon_event(
        data_root,
        {
            "event": "daemon.starting",
            "pid": os.getpid(),
            "port": args.port,
            "version": __version__,
            "recorded_at": started_at,
        },
    )
    runtime = build_production_runtime(data_root)
    published_running_state = False
    try:
        app = create_app(
            runtime,
            base_url=base_url,
            control_key=read_control_key(data_root),
        )
        write_runtime_state(
            data_root,
            RuntimeState(
                status="running",
                pid=os.getpid(),
                host=args.host,
                port=args.port,
                base_url=base_url,
                version=__version__,
                started_at=started_at,
            ),
        )
        published_running_state = True
        append_daemon_event(
            data_root,
            {
                "event": "daemon.ready",
                "pid": os.getpid(),
                "port": args.port,
                "revision": runtime.feed.current_revision(),
                "recorded_at": time.time(),
            },
        )

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                workers=1,
                access_log=False,
                log_level="warning",
                server_header=False,
                date_header=False,
            )
        )
        server.run()
    finally:
        if published_running_state:
            append_daemon_event(
                data_root,
                {
                    "event": "daemon.stopped",
                    "pid": os.getpid(),
                    "recorded_at": time.time(),
                },
            )
            write_runtime_state(
                data_root,
                RuntimeState(
                    status="stopped",
                    pid=os.getpid(),
                    host=args.host,
                    port=args.port,
                    base_url=base_url,
                    version=__version__,
                    started_at=started_at,
                    stopped_at=time.time(),
                ),
            )
        runtime.close()
    return 0


def _base_url(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


if __name__ == "__main__":
    raise SystemExit(main())
