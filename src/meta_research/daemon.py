from __future__ import annotations

import argparse
import errno
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO

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

# ``meta-research stop`` owns this end-to-end deadline.  A long-lived HTTP/SSE
# request may consume only the connection-drain share: the remaining ten
# seconds preserve the existing Web lifespan worker drain (up to two seconds),
# RuntimeProtection/telemetry close (up to five seconds), and three seconds for
# request_stop, Owner/database cleanup, durable log/state writes, and scheduler
# variance.  Keep the Uvicorn share strictly below the CLI deadline so a stuck
# client cannot prevent those durable shutdown boundaries from running.
DAEMON_STOP_DEADLINE_SECONDS = 15.0
DAEMON_LIFESPAN_RESERVE_SECONDS = 2.0
DAEMON_RUNTIME_CLOSE_RESERVE_SECONDS = 5.0
DAEMON_FINALIZATION_RESERVE_SECONDS = 3.0
DAEMON_CONNECTION_DRAIN_SECONDS = DAEMON_STOP_DEADLINE_SECONDS - (
    DAEMON_LIFESPAN_RESERVE_SECONDS
    + DAEMON_RUNTIME_CLOSE_RESERVE_SECONDS
    + DAEMON_FINALIZATION_RESERVE_SECONDS
)


def _install_uvicorn_signal_replay_guard(
    server: Any,
    handled_signals: Sequence[int],
) -> tuple[tuple[int, Any], ...]:
    """Preserve daemon cleanup across Uvicorn's captured-signal replay.

    Uvicorn restores the handler that preceded ``Server.run`` and then replays
    every captured shutdown signal.  A replay into the process default would
    terminate before the daemon can publish its stopped state and close the
    runtime.  A real first signal in the small pre-capture window is delegated
    to Uvicorn; only a replay after ``should_exit`` is already set is consumed.
    Uvicorn itself does not install signal handlers outside the main thread, so
    the guard follows the same boundary.
    """

    if threading.current_thread() is not threading.main_thread():
        return ()

    def guard(signal_number: int, frame: object) -> None:
        if not server.should_exit:
            server.handle_exit(signal_number, frame)

    installed: list[tuple[int, Any]] = []
    try:
        for signal_number in handled_signals:
            previous = signal.signal(signal_number, guard)
            installed.append((signal_number, previous))
    except BaseException:
        _restore_shutdown_signal_handlers(tuple(installed))
        raise
    return tuple(installed)


def _restore_shutdown_signal_handlers(
    handlers: tuple[tuple[int, Any], ...],
) -> None:
    for signal_number, handler in reversed(handlers):
        signal.signal(signal_number, handler)


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
    original_shutdown_signal_handlers: tuple[tuple[int, Any], ...] = ()
    try:
        app = create_app(
            runtime,
            base_url=base_url,
            control_key=read_control_key(data_root),
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
                timeout_graceful_shutdown=DAEMON_CONNECTION_DRAIN_SECONDS,
            )
        )
        original_shutdown_signal_handlers = (
            _install_uvicorn_signal_replay_guard(
                server,
                uvicorn.server.HANDLED_SIGNALS,
            )
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
        server.run()
    finally:
        try:
            try:
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
            finally:
                runtime.close()
        finally:
            _restore_shutdown_signal_handlers(original_shutdown_signal_handlers)
    return 0


def _base_url(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


if __name__ == "__main__":
    raise SystemExit(main())
