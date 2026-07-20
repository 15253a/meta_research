"""Internal Web child launcher with a Linux parent-death lifecycle fence.

The Web product must not leave an owner that can only be stopped from a
backend shell after the Web server crashes.  This tiny launcher arms
``PR_SET_PDEATHSIG`` before importing the research runtime, verifies the
parent did not disappear in the arming race, and then enters the canonical
``orchestrator.run`` main function.  A normal Web shutdown still uses the
manager's stronger process-group drain protocol.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import os
import runpy
import signal
import sys
from typing import List, Optional


_PR_SET_PDEATHSIG = 1


def _terminate_group(_signum, _frame) -> None:  # noqa: ANN001 - signal protocol
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        os._exit(128 + signal.SIGTERM)


def _arm_parent_death(expected_parent_pid: int) -> None:
    if expected_parent_pid <= 1:
        raise RuntimeError("Web owner parent PID 非法")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise RuntimeError("当前平台不支持 Web owner parent-death fence")
    prctl.argtypes = [
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_ulong, ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    signal.signal(signal.SIGTERM, _terminate_group)
    if prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno() or errno.EINVAL
        raise OSError(error_number, "PR_SET_PDEATHSIG failed")
    if os.getppid() != expected_parent_pid:
        # Parent exited between fork/exec and prctl.  Do not become an
        # unmanageable external owner even for one research call.
        os.kill(os.getpid(), signal.SIGTERM)
        raise RuntimeError("Web owner parent 在启动围栏建立前退出")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    parser.add_argument("run_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    run_argv = list(args.run_argv)
    if run_argv[:1] == ["--"]:
        run_argv = run_argv[1:]
    _arm_parent_death(args.expected_parent_pid)
    # Execute only after the parent-death fence is live.  ``run_module`` also
    # preserves the exact behaviour of ``python -m orchestrator.run`` for
    # deployment wrappers that provide that module themselves.
    previous = sys.argv
    sys.argv = ["orchestrator.run", *run_argv]
    try:
        runpy.run_module("orchestrator.run", run_name="__main__")
    finally:
        sys.argv = previous
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
