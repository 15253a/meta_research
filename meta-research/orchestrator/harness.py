"""执行 harness（M4 CP5.3）——真子进程跑训练/评估命令，产物落 staging，log 登记 execution_log。

**纪律**：
- **长操作绝不持写事务**（§6.13 铁律）：run_staged 只碰文件系统/子进程、零 DB；跑完后调用方以短事务
  register_execution_log 入账（§4.2.5(i) 执行事实随发生短事务）。
- **staging 半成品**（§4.4.5 P6）：log 先写 `<name>.partial`、进程结束后**原子改名**为正式名——kill-9 只留
  .partial（重启可辨识丢弃/重跑），绝不出现「看似完整实则截断」的 log。
- content_hash = log 文件字节 sha256——同内容同 hash（观测可回放锚：hash 定内容，parser 定观测）。
- wall-clock 不入 DB 确定性面：真实耗时只写进 log 文本（脚本自报 wall_clock_sec 行）或留给 watchdog（M6）。
"""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .artifact_capability import open_artifact, read_artifact_bytes
from .ids import cnum as _cnum
from .process_supervisor import (ExecutionRecoveryError, ExecutionSupervisor,
                                 atomic_write_receipt, read_receipt,
                                 stream_execution_frames,
                                 verified_execution_capture_size,
                                 verified_execution_frame_size)
from .writedaemon import WriteDaemon


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("exit sidecar short write")
        view = view[written:]


def _read_exact(fd: int, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = os.read(fd, size - len(chunks))
        if not chunk:
            raise OSError("exit sidecar short read")
        chunks.extend(chunk)
    return bytes(chunks)


def _stream_artifact_identity(
        path: Path, *, label: str) -> tuple[str, int]:
    """Hash one immutable owner-controlled artifact with bounded reads."""
    flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        linked = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or (
                linked.st_dev, linked.st_ino, linked.st_size,
                linked.st_mtime_ns, linked.st_ctime_ns,
            ) != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns,
            )
        ):
            raise OSError(f"{label} identity 非法")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        relinked = os.lstat(path)
        stable = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if (
            total != before.st_size
            or stable != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns,
            )
            or (
                relinked.st_dev, relinked.st_ino, relinked.st_size,
                relinked.st_mtime_ns, relinked.st_ctime_ns,
            ) != stable
        ):
            raise OSError(f"{label} changed during identity read")
        return digest.hexdigest(), total
    finally:
        os.close(fd)


def _validate_log_name(log_name: str) -> None:
    if (not isinstance(log_name, str) or not log_name or len(log_name) > 128
            or log_name in {".", ".."} or "/" in log_name or "\\" in log_name
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in log_name)):
        raise ValueError("log_name 须为有界安全 basename")


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _ensure_exit_sidecar(directory: Path, log_name: str, exit_code: int) -> Path:
    exit_path = directory / (log_name + ".exit")
    if os.path.lexists(exit_path):
        fd = -1
        try:
            fd = os.open(exit_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or not 1 <= info.st_size <= 32):
                raise OSError("exit sidecar 身份/大小非法")
            raw = _read_exact(fd, info.st_size)
            text = raw.decode("ascii")
            existing = int(text)
            if text != str(existing):
                raise ValueError("exit sidecar 非规范整数")
        except (OSError, UnicodeError, ValueError) as error:
            raise ExecutionRecoveryError(f"staged exit 侧车损坏: {exit_path}") from error
        finally:
            if fd >= 0:
                os.close(fd)
        if existing != exit_code:
            raise ExecutionRecoveryError(
                f"staged exit 侧车与 guardian receipt 冲突: {existing} != {exit_code}")
        return exit_path
    exit_tmp = directory / (log_name + f".exit.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    exit_fd = os.open(
        exit_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _write_all(exit_fd, str(exit_code).encode("ascii"))
        os.fsync(exit_fd)
    finally:
        os.close(exit_fd)
    os.replace(exit_tmp, exit_path)
    _fsync_dir(directory)
    return exit_path


def recover_staged_result(*, staging_dir: str, log_name: str,
                          execution_supervisor: Optional[ExecutionSupervisor],
                          execution_kind: str,
                          execution_context: Mapping[str, Any],
                          execution_sandbox=None,
                          recover_completed: bool = False,
                          return_terminal_failure: bool = False,
                          stream_output_observer=None,
                          stream_frame_offset: Optional[int] = None,
                          stream_offsets: Optional[
                              Mapping[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Promote an owner-orphaned complete ``.partial`` using its central receipt.

    The guardian has already fsynced output and proved the descendant tree
    drained.  This helper only reconstructs harness' local ``.exit``/final
    publication after the former Python owner died between supervisor return
    and ``.partial -> final``.  It never interprets exit(0) as DB success.

    ``None`` means no prior operation exists and a fresh call is safe.  A
    partial or matching non-exit receipt without a recoverable complete log is
    fail-loud: reusing the same DB owner would create two operations and erase
    the only audit artifact.
    """
    _validate_log_name(log_name)
    stream_arguments = (
        stream_output_observer is not None,
        stream_frame_offset is not None,
        stream_offsets is not None,
    )
    if any(stream_arguments) and not all(stream_arguments):
        raise ValueError(
            "stream recovery 要求 observer、frame_offset 与 offsets 同时提供")
    normalized_stream_offsets = None
    if stream_output_observer is not None:
        if not callable(stream_output_observer):
            raise ValueError(
                "stream_output_observer 须为 callable 或 None")
        if (
            not isinstance(stream_offsets, Mapping)
            or set(stream_offsets) != {"stdout", "stderr"}
            or any(
                isinstance(stream_offsets.get(stream), bool)
                or not isinstance(stream_offsets.get(stream), int)
                or int(stream_offsets[stream]) < 0
                for stream in ("stdout", "stderr")
            )
        ):
            raise ValueError(
                "stream_offsets 须为 stdout/stderr 非负整数映射")
        if (
            isinstance(stream_frame_offset, bool)
            or not isinstance(stream_frame_offset, int)
            or stream_frame_offset < 0
        ):
            raise ValueError("stream_frame_offset 须为非负整数")
        normalized_stream_offsets = {
            stream: int(stream_offsets[stream])
            for stream in ("stdout", "stderr")
        }
    expected = dict(execution_context)
    if "log_name" in expected and expected["log_name"] != log_name:
        raise ValueError("execution_context.log_name 与 harness log_name 冲突")
    expected["log_name"] = log_name
    owner_kind, owner_id = expected.get("db_owner_kind"), expected.get("db_owner_id")
    if (not isinstance(owner_kind, str) or isinstance(owner_id, bool)
            or not isinstance(owner_id, int) or owner_id <= 0):
        raise ValueError("recover_staged_result 要求 exact DB owner context")
    attempt_bound = "execution_attempt" in expected
    expected_attempt = expected.get("execution_attempt", 1)
    if (attempt_bound and (isinstance(expected_attempt, bool)
                           or not isinstance(expected_attempt, int)
                           or expected_attempt <= 0)):
        raise ValueError("execution_context.execution_attempt 须为正整数")

    directory = Path(staging_dir)
    partial = directory / (log_name + ".partial")
    final = directory / log_name
    final_exists = os.path.lexists(final)
    if final_exists and not recover_completed:
        return None
    receipt_dir = (execution_supervisor.receipt_dir if execution_supervisor is not None
                   else directory / ".execution-receipts")
    matches = []
    seen_attempts = set()
    for path in sorted(Path(receipt_dir).glob("execution-*.json")):
        receipt = read_receipt(path)
        context = receipt.get("context") or {}
        if (context.get("db_owner_kind"), context.get("db_owner_id")) != (owner_kind, owner_id):
            continue
        if receipt.get("kind") != execution_kind:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} guardian receipt 与 staged recovery context 错配")
        if attempt_bound:
            # Legacy receipts predate the explicit field and are exactly
            # attempt 1.  A durable bundle_repair_requested decision advances
            # the caller's expected attempt before the rejected staging tree
            # is archived.  Older terminal receipts therefore remain audit
            # evidence but no longer own the replacement staging namespace.
            receipt_attempt = context.get("execution_attempt", 1)
            if (isinstance(receipt_attempt, bool)
                    or not isinstance(receipt_attempt, int)
                    or receipt_attempt <= 0):
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} guardian receipt execution_attempt 非法")
            if receipt_attempt in seen_attempts:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} execution attempt {receipt_attempt} "
                    "对应多个 guardian receipt")
            seen_attempts.add(receipt_attempt)
            base_mismatch = any(
                context.get(key) != value for key, value in expected.items()
                if key != "execution_attempt")
            if base_mismatch or receipt_attempt > expected_attempt:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} guardian receipt 与 staged recovery context 错配")
            if receipt_attempt < expected_attempt:
                if (receipt.get("state") != "terminal"
                        or receipt.get("group_drained") is not True):
                    raise ExecutionRecoveryError(
                        f"{owner_kind} {owner_id} 旧 execution attempt "
                        f"{receipt_attempt} 未 terminal+drained，不得越过")
                continue
        if any(context.get(key) != value for key, value in expected.items()
               if not (attempt_bound and key == "execution_attempt")):
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} guardian receipt 与 staged recovery context 错配")
        matches.append((path, receipt))
    if len(matches) > 1:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} 对应多个 guardian execution receipt")
    partial_exists = os.path.lexists(partial)
    if not matches:
        if final_exists:
            raise ExecutionRecoveryError(
                f"{final} 存在但无 exact guardian receipt；拒绝把旧 final 当作恢复结果")
        if execution_sandbox is not None and execution_sandbox.recover_unstarted_session(
                staging_dir=directory, log_name=log_name,
                execution_context=expected,
                execution_supervisor=execution_supervisor,
                partial_path=partial):
            return None
        if partial_exists:
            raise ExecutionRecoveryError(
                f"{partial} 存在但无 exact guardian receipt；拒绝截断重跑")
        return None

    receipt_path, receipt = matches[0]
    if receipt.get("state") != "terminal" or receipt.get("group_drained") is not True:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} guardian receipt 未 terminal+drained")
    stream_identity = receipt.get("capture_stream_identity") is True
    if stream_identity != (stream_output_observer is not None):
        if stream_identity:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} stream capture recovery "
                "缺 journal observer/offsets")
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} receipt 非 stream capture，"
            "拒绝应用 stream offsets")
    if stream_identity:
        assert normalized_stream_offsets is not None
        try:
            capture_sizes = {
                stream: verified_execution_capture_size(
                    receipt, stream=stream)
                for stream in ("stdout", "stderr")
            }
            frame_size = verified_execution_frame_size(receipt)
        except (OSError, ValueError) as error:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} guardian stream capture "
                "身份不可验证") from error
        for stream in ("stdout", "stderr"):
            if normalized_stream_offsets[stream] > capture_sizes[stream]:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} {stream} capture "
                    "短于 journal committed offset")
        assert stream_frame_offset is not None
        if stream_frame_offset > frame_size:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} frame capture "
                "短于 journal committed offset")
        if final_exists and partial_exists:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} 同时存在 final 与 partial，拒绝恢复")
        if not final_exists and not partial_exists:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} stream receipt 存在但 "
                f"{partial} 缺失")

        partial_fd = -1
        if not final_exists:
            partial_fd = os.open(
                partial,
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(partial_fd)
            committed_total = sum(normalized_stream_offsets.values())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
            ):
                os.close(partial_fd)
                raise ExecutionRecoveryError(
                    f"stream staged log 身份非法: {partial}")
            if info.st_size < committed_total:
                os.close(partial_fd)
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} partial 短于 journal "
                    "committed bytes")
            if info.st_size > committed_total:
                os.ftruncate(partial_fd, committed_total)
                os.fsync(partial_fd)
            os.lseek(partial_fd, committed_total, os.SEEK_SET)

        try:
            suffix_bytes = {"stdout": 0, "stderr": 0}

            def accept_suffix(
                    stream: str, chunk: bytes,
                    frame_end_offset: int) -> None:
                if partial_fd >= 0:
                    _write_all(partial_fd, chunk)
                    # The partial must never lag a durable journal cursor.  If
                    # the owner dies after this fsync but before the observer
                    # commits, the next recovery truncates the harmless extra
                    # suffix back to committed_total.
                    os.fsync(partial_fd)
                assert stream_output_observer is not None
                stream_output_observer(
                    stream, chunk, frame_end_offset)
                suffix_bytes[stream] += len(chunk)

            final_frame_offset = stream_execution_frames(
                receipt, start_offset=stream_frame_offset,
                observer=accept_suffix)
            if final_frame_offset != frame_size:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} frame capture suffix "
                    "未完整恢复")
            for stream in ("stdout", "stderr"):
                if (
                    normalized_stream_offsets[stream]
                    + suffix_bytes[stream]
                    != capture_sizes[stream]
                ):
                    raise ExecutionRecoveryError(
                        f"{owner_kind} {owner_id} ordered frames 与 "
                        f"{stream} raw capture 长度冲突")
        except (OSError, ValueError) as error:
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} stream capture suffix "
                "恢复失败") from error
        finally:
            if partial_fd >= 0:
                os.close(partial_fd)
    if receipt.get("outcome") != "exit":
        if receipt.get("containment") == "docker-container-v1":
            try:
                from .execution_sandbox import finalize_sandbox_output
                finalize_sandbox_output(
                    staging_dir=directory, log_name=log_name,
                    context=expected, execution_receipt=receipt,
                    exit_code=125)
            except BaseException as cleanup_error:
                raise ExecutionRecoveryError(
                    f"{owner_kind} {owner_id} non-exit sandbox session 清理失败") from cleanup_error
        if return_terminal_failure:
            return {
                "exit_code": 125, "log_path": None, "log_sha256": None,
                "log_bytes": None, "process_receipt_path": str(receipt_path),
                "process_receipt": receipt,
                "recovered_after_owner_loss": True,
                "terminal_failure": True,
                "failure_outcome": receipt.get("outcome"),
            }
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} prior outcome={receipt.get('outcome')} 不可提升为完整 log")
    if final_exists and partial_exists:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} 同时存在 final 与 partial，拒绝恢复")
    if not final_exists and not partial_exists:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} 有 drained exit receipt 但 {partial} 缺失")
    completed_path = final if final_exists else partial
    info = os.lstat(completed_path)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid()):
        raise ExecutionRecoveryError(f"staged log 身份非法: {completed_path}")
    fd = os.open(completed_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    exit_code = receipt.get("returncode")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ExecutionRecoveryError("drained exit receipt 缺合法 returncode")
    if receipt.get("containment") == "docker-container-v1":
        from .execution_sandbox import (
            discard_rejected_sandbox_output,
            ExecutionSandboxError, SandboxOutputError,
            finalize_sandbox_output,
        )
        try:
            finalize_sandbox_output(
                staging_dir=directory, log_name=log_name,
                context=expected, execution_receipt=receipt,
                exit_code=exit_code)
        except ExecutionSandboxError as error:
            try:
                discard_rejected_sandbox_output(
                    staging_dir=directory, log_name=log_name,
                    context=expected, execution_receipt=receipt,
                    reason=str(error))
            except BaseException as cleanup_error:
                note = getattr(error, "add_note", None)
                if callable(note):
                    note("sandbox rejected quarantine 清理失败: "
                         f"{type(cleanup_error).__name__}: {cleanup_error}")
            raise SandboxOutputError(
                str(error), receipt=receipt, receipt_path=receipt_path) from error
    _ensure_exit_sidecar(directory, log_name, exit_code)
    atomic_write_receipt(directory / (log_name + ".process.json"), {
        "version": 1,
        "operation_id": receipt["operation_id"],
        "outcome": receipt["outcome"],
        "group_drained": receipt["group_drained"],
        "receipt_path": str(receipt_path),
    })
    if not final_exists:
        os.replace(partial, final)
        _fsync_dir(directory)
    log_sha256, log_bytes = _stream_artifact_identity(
        final, label="recovered staged log")
    return {
        "exit_code": exit_code, "log_path": str(final),
        "log_sha256": log_sha256, "log_bytes": log_bytes,
        "process_receipt_path": str(receipt_path), "process_receipt": receipt,
        "process_pointer_path": str(directory / (log_name + ".process.json")),
        "recovered_after_owner_loss": True,
    }


def run_staged(cmd: List[str], *, staging_dir: str, log_name: str, timeout_s: float = 600.0,
               env: Optional[Dict[str, str]] = None,
               pass_fds: Sequence[int] = (),
               execution_supervisor: Optional[ExecutionSupervisor] = None,
               execution_kind: str = "harness",
               execution_context: Optional[Dict[str, Any]] = None,
               sandbox_invocation=None,
               inherit_environment: bool = True,
               progress_observer=None,
               output_observer=None,
               stream_output_observer=None,
               progress_interval_s: float = 5.0) -> Dict[str, Any]:
    """跑真子进程，stdout+stderr 合流写 staging log（.partial → 原子改名）。返回
    {exit_code, log_path, log_sha256, log_bytes}。超时 → kill 并抛 subprocess.TimeoutExpired
    （.partial 留在 staging 供审计，不改名——半成品不冒充完整产物）。

    ``output_observer`` 从本次已打开的 partial FD 增量接收合流输出字节；每段只交付一次，
    guardian 收口后还会在 FD 关闭前做 final drain。``stream_output_observer`` 接收
    ``(stdout|stderr, bytes, frame_end_offset)``，同时按 guardian frame 顺序构造
    历史兼容的合流 log；两种 observer 不得并用。observer 抛错会取消本次执行并原样上抛。"""
    _validate_log_name(log_name)
    if output_observer is not None and not callable(output_observer):
        raise ValueError("output_observer 须为 callable 或 None")
    if (stream_output_observer is not None
            and not callable(stream_output_observer)):
        raise ValueError("stream_output_observer 须为 callable 或 None")
    if output_observer is not None and stream_output_observer is not None:
        raise ValueError(
            "output_observer 与 stream_output_observer 不得同时使用")
    if progress_observer is not None and not callable(progress_observer):
        raise ValueError("progress_observer 须为 callable 或 None")
    context = dict(execution_context or {})
    if "log_name" in context and context["log_name"] != log_name:
        raise ValueError("execution_context.log_name 与 harness log_name 冲突")
    context["log_name"] = log_name
    d = Path(staging_dir)
    d.mkdir(parents=True, exist_ok=True)
    partial = d / (log_name + ".partial")
    final = d / log_name
    if final.exists():   # 防旧 final 冒充本次产物（重试须换名/清 staging——超时后旧 final+新 .partial 会混淆，codex NIT）
        raise FileExistsError(f"staging 已有同名 final {final}——log_name 须每次执行唯一（或先清 staging）")
    own_supervisor = execution_supervisor is None
    supervisor = execution_supervisor or ExecutionSupervisor.standalone(
        d / ".execution-receipts")
    execution_error: Optional[BaseException] = None
    result = None
    try:
        with open(partial, "w+b") as fh:
            output_offset = 0
            output_observer_error: Optional[BaseException] = None

            def drain_output() -> None:
                nonlocal output_offset, output_observer_error
                if output_observer is None or output_observer_error is not None:
                    return
                while True:
                    chunk = os.pread(fh.fileno(), 64 * 1024, output_offset)
                    if not chunk:
                        return
                    # Advance before dispatch so a rejecting observer can never
                    # receive the same bytes a second time during finalization.
                    output_offset += len(chunk)
                    try:
                        output_observer(chunk)
                    except BaseException as error:
                        output_observer_error = error
                        raise

            def observe_progress() -> bool:
                try:
                    drain_output()
                except BaseException:
                    # Use the existing exact-execution cancellation channel.
                    # The original observer error is restored after the
                    # guardian has proved the descendant tree drained.
                    return True
                if progress_observer is None:
                    return False
                return progress_observer()

            def observe_stream(
                    stream: str, chunk: bytes,
                    frame_end_offset: int) -> None:
                # In stream-preserving mode the guardian owns the two raw
                # captures.  Rebuild the historical combined staged log in
                # exactly the parent observation order before publishing the
                # labeled event to the append-only consumer.
                _write_all(fh.fileno(), chunk)
                os.fsync(fh.fileno())
                assert stream_output_observer is not None
                stream_output_observer(
                    stream, chunk, frame_end_offset)

            # cwd=staging：脚本的相对路径产物（checkpoint/指标文件）落 staging（半成品目录纪律的自然延伸）。
            # Guardian 在直接子进程结束后仍要清空/reap 整棵后代树；只有这个机械边界返回
            # 后，.partial 才可能提升为不可变 final。
            try:
                run_kwargs = {
                    "stdin": None,
                    "timeout_s": timeout_s, "cwd": d,
                    # A sandbox wrapper is trusted host control code and gets
                    # a deliberately minimal env; host credentials must never
                    # be copied into its guardian spec or container.  Ordinary
                    # trusted harness calls preserve the historical inherited
                    # environment behavior.
                    "env": (dict(env or {}) if (sandbox_invocation is not None
                                                  or not inherit_environment)
                            else {**os.environ, **(env or {})}),
                    "pass_fds": tuple(pass_fds), "kind": execution_kind,
                    "operation_context": context,
                    "external_container": (
                        sandbox_invocation.external_container
                        if sandbox_invocation is not None else None),
                }
                if stream_output_observer is None:
                    run_kwargs.update({
                        "stdout": fh, "stderr": subprocess.STDOUT,
                    })
                else:
                    run_kwargs.update({
                        "capture_output": True,
                        "capture_result": False,
                        "stream_capture_observer": observe_stream,
                    })
                # Do not pass the optional keywords to legacy/injected test
                # supervisors unless observation is actually enabled.
                if (output_observer is not None
                        or stream_output_observer is not None
                        or progress_observer is not None):
                    run_kwargs.update({
                        "progress_interval_s": progress_interval_s,
                    })
                    if stream_output_observer is None:
                        run_kwargs["progress_observer"] = observe_progress
                    elif progress_observer is not None:
                        run_kwargs["progress_observer"] = progress_observer
                result = supervisor.run(cmd, **run_kwargs)
            except BaseException as error:
                execution_error = error
            finally:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except BaseException as error:
                    if execution_error is None:
                        execution_error = error
                    else:
                        note = getattr(execution_error, "add_note", None)
                        if callable(note):
                            note("partial flush/fsync 同时失败: "
                                 f"{type(error).__name__}: {error}")
                try:
                    # supervisor.run only returns/raises after its guardian has
                    # drained the whole tree, so this is the stable final suffix.
                    drain_output()
                except BaseException:
                    # drain_output stored the exact callback exception.
                    pass
                if output_observer_error is not None:
                    prior_error = execution_error
                    receipt = None
                    receipt_path = None
                    if result is not None:
                        receipt, receipt_path = result.receipt, result.receipt_path
                    elif prior_error is not None:
                        receipt = (getattr(prior_error, "receipt", None)
                                   or getattr(prior_error, "execution_receipt", None))
                        receipt_path = (
                            getattr(prior_error, "receipt_path", None)
                            or getattr(prior_error, "execution_receipt_path", None))
                    if receipt is not None and receipt_path is not None:
                        try:
                            output_observer_error.receipt = receipt
                            output_observer_error.receipt_path = receipt_path
                            output_observer_error.execution_receipt = receipt
                            output_observer_error.execution_receipt_path = receipt_path
                        except BaseException:
                            pass
                    if (prior_error is not None
                            and prior_error is not output_observer_error):
                        note = getattr(output_observer_error, "add_note", None)
                        if callable(note):
                            note("observer 触发取消/收口时 execution 同时报告 "
                                 f"{type(prior_error).__name__}: {prior_error}")
                    execution_error = output_observer_error
    finally:
        if own_supervisor:
            supervisor.close()
    receipt = None
    receipt_path = None
    if execution_error is not None:
        receipt = (getattr(execution_error, "receipt", None)
                   or getattr(execution_error, "execution_receipt", None))
        receipt_path = (getattr(execution_error, "receipt_path", None)
                        or getattr(execution_error, "execution_receipt_path", None))
        if receipt is not None and receipt_path is not None:
            try:
                execution_error.receipt = receipt
                execution_error.receipt_path = receipt_path
                execution_error.execution_receipt = receipt
                execution_error.execution_receipt_path = receipt_path
            except BaseException:
                pass
    if result is not None:
        receipt, receipt_path = result.receipt, result.receipt_path
    if receipt is not None and receipt_path is not None:
        try:
            atomic_write_receipt(d / (log_name + ".process.json"), {
                "version": 1,
                "operation_id": receipt["operation_id"],
                "outcome": receipt["outcome"],
                "group_drained": receipt["group_drained"],
                "receipt_path": str(receipt_path),
            })
        except BaseException as pointer_error:
            # The guardian receipt is authoritative.  A convenience pointer
            # failure on an already-failed execution must not erase timeout /
            # owner-loss classification needed by later reconciliation.  A
            # success-path pointer failure still fails loud before .partial
            # can be promoted to final.
            if execution_error is None:
                raise
            try:
                execution_error.process_pointer_error = pointer_error
            except BaseException:
                pass
            note = getattr(execution_error, "add_note", None)
            if callable(note):
                note("process pointer 写入失败: "
                     f"{type(pointer_error).__name__}: {pointer_error}")
    if execution_error is not None:
        if (sandbox_invocation is not None and receipt is not None
                and receipt.get("containment") == "docker-container-v1"):
            try:
                from .execution_sandbox import finalize_sandbox_output
                finalize_sandbox_output(
                    staging_dir=d, log_name=log_name, context=context,
                    execution_receipt=receipt, exit_code=125)
            except BaseException as sandbox_cleanup_error:
                note = getattr(execution_error, "add_note", None)
                if callable(note):
                    note("sandbox quarantine 清理同时失败: "
                         f"{type(sandbox_cleanup_error).__name__}: {sandbox_cleanup_error}")
        if sandbox_invocation is not None:
            sandbox_invocation.close()
        raise execution_error.with_traceback(execution_error.__traceback__)
    assert result is not None
    exit_code = result.returncode
    if (sandbox_invocation is not None
            and sandbox_invocation.external_container is not None):
        try:
            from .execution_sandbox import (
                discard_rejected_sandbox_output,
                ExecutionSandboxError, SandboxOutputError,
                finalize_sandbox_output,
            )
            try:
                finalize_sandbox_output(
                    staging_dir=d, log_name=log_name, context=context,
                    execution_receipt=result.receipt, exit_code=exit_code)
            except ExecutionSandboxError as error:
                try:
                    discard_rejected_sandbox_output(
                        staging_dir=d, log_name=log_name, context=context,
                        execution_receipt=result.receipt, reason=str(error))
                except BaseException as cleanup_error:
                    note = getattr(error, "add_note", None)
                    if callable(note):
                        note("sandbox rejected quarantine 清理失败: "
                             f"{type(cleanup_error).__name__}: {cleanup_error}")
                raise SandboxOutputError(
                    str(error), receipt=result.receipt,
                    receipt_path=result.receipt_path) from error
        finally:
            sandbox_invocation.close()
    elif sandbox_invocation is not None:
        # Development local execution is still a SandboxInvocation so it can
        # supply the selected Conda/GPU environment, but it has no Docker
        # quarantine to promote.  The guardian receipt/process-tree drain and
        # ordinary staging publication remain authoritative.
        sandbox_invocation.close()
    # exit 侧车**先于** final 改名（原子 tmp→replace）：final 存在 ⟹ 退出码可读——崩后续跑须复用同一
    # exit 判定（非 0 的 eval 也会产 final，恢复方不得把失败进程的完整输出当成功续注册，codex BLOCKER）
    _ensure_exit_sidecar(d, log_name, exit_code)
    os.replace(partial, final)          # 原子改名：只有完整跑完的 log 得正式名（P6 staging 纪律）
    _fsync_dir(d)
    log_sha256, log_bytes = _stream_artifact_identity(
        final, label="completed staged log")
    return {"exit_code": exit_code, "log_path": str(final),
            "log_sha256": log_sha256, "log_bytes": log_bytes,
            "process_receipt_path": str(result.receipt_path),
            "process_receipt": result.receipt,
            "process_pointer_path": str(d / (log_name + ".process.json"))}


def file_sha256(path: str) -> str:
    """产物文件 content_hash（checkpoint/指标值文件等入账用）。"""
    with open_artifact(path, label="content-hash artifact") as capability:
        return capability.identity.content_hash.removeprefix("sha256:")


def latest_smoke_log(smoke_dir: Path) -> Optional[Path]:
    """smoke-<N>.log 按 **N 数值**取最新——字典序会把 smoke-10 排在 smoke-2 前（崩溃重跑序号可超 9，
    codex SHOULD）。非法名（serial 非整数）不参与。无则 None。消费方：attack_stages subject 构造 +
    JudgeProvider 材料装配（两侧必须同一「最新」口径，否则 subject_hash 与评审材料看的不是同一份）。"""
    best: Optional[tuple] = None
    for p in Path(smoke_dir).glob("smoke-*.log"):
        try:
            n = int(p.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if best is None or n > best[0]:
            best = (n, p)
    return best[1] if best else None


def register_execution_log(daemon: WriteDaemon, *, cycle_id: str, log_kind: str, ref: str,
                           content_hash: str, n_bytes: int, run_id: Optional[int] = None,
                           evaluation_attempt_id: Optional[int] = None) -> int:
    """execution_log 入账（短事务；owner=run XOR attempt 由 DDL CHECK 焊）。**幂等**：owner+kind+hash
    撞既有唯一索引（ux_execlog_run/attempt）→ 返回既有行 id（重放不重登）。"""
    ci = _cnum(cycle_id)
    if (run_id is None) == (evaluation_attempt_id is None):   # 友好前置（DDL CHECK 兜底）：owner 恰一
        raise ValueError("execution_log owner 须恰一：run_id XOR evaluation_attempt_id")
    with daemon.transaction() as conn:
        ex = conn.execute(
            "SELECT id FROM execution_log WHERE log_kind=? AND content_hash=? AND "
            "run_id IS ? AND evaluation_attempt_id IS ?",
            (log_kind, content_hash, run_id, evaluation_attempt_id)).fetchone()
        if ex:
            return ex[0]
        return conn.execute(
            "INSERT INTO execution_log(run_id,evaluation_attempt_id,cycle_id,log_kind,ref,content_hash,bytes) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, evaluation_attempt_id, ci, log_kind, ref, content_hash, n_bytes)).lastrowid
