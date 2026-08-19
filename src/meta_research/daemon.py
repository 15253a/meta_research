from __future__ import annotations

import argparse
import fcntl
import os
import time
from pathlib import Path

import uvicorn

from meta_research import __version__
from meta_research.composition import build_production_runtime
from meta_research.paths import (
    RuntimeState,
    append_daemon_event,
    prepare_data_root,
    read_control_key,
    write_runtime_state,
)
from meta_research.web import create_app


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
    lock_handle = data_root.daemon_lock.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 2

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
    try:
        server.run()
    finally:
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
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
    return 0


def _base_url(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


if __name__ == "__main__":
    raise SystemExit(main())
