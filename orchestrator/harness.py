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
                                 atomic_write_receipt, read_receipt)
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
                          execution_sandbox=None) -> Optional[Dict[str, Any]]:
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
    expected = dict(execution_context)
    if "log_name" in expected and expected["log_name"] != log_name:
        raise ValueError("execution_context.log_name 与 harness log_name 冲突")
    expected["log_name"] = log_name
    owner_kind, owner_id = expected.get("db_owner_kind"), expected.get("db_owner_id")
    if (not isinstance(owner_kind, str) or isinstance(owner_id, bool)
            or not isinstance(owner_id, int) or owner_id <= 0):
        raise ValueError("recover_staged_result 要求 exact DB owner context")

    directory = Path(staging_dir)
    partial = directory / (log_name + ".partial")
    final = directory / log_name
    if final.exists():
        return None
    receipt_dir = (execution_supervisor.receipt_dir if execution_supervisor is not None
                   else directory / ".execution-receipts")
    matches = []
    for path in sorted(Path(receipt_dir).glob("execution-*.json")):
        receipt = read_receipt(path)
        context = receipt.get("context") or {}
        if (context.get("db_owner_kind"), context.get("db_owner_id")) != (owner_kind, owner_id):
            continue
        if (receipt.get("kind") != execution_kind
                or any(context.get(key) != value for key, value in expected.items())):
            raise ExecutionRecoveryError(
                f"{owner_kind} {owner_id} guardian receipt 与 staged recovery context 错配")
        matches.append((path, receipt))
    if len(matches) > 1:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} 对应多个 guardian execution receipt")
    partial_exists = os.path.lexists(partial)
    if not matches:
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
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} prior outcome={receipt.get('outcome')} 不可提升为完整 log")
    if not partial_exists:
        raise ExecutionRecoveryError(
            f"{owner_kind} {owner_id} 有 drained exit receipt 但 {partial} 缺失")
    info = os.lstat(partial)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid()):
        raise ExecutionRecoveryError(f"staged partial 身份非法: {partial}")
    fd = os.open(partial, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
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
    os.replace(partial, final)
    _fsync_dir(directory)
    data = read_artifact_bytes(final, label="recovered staged log")
    return {
        "exit_code": exit_code, "log_path": str(final),
        "log_sha256": hashlib.sha256(data).hexdigest(), "log_bytes": len(data),
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
               sandbox_invocation=None) -> Dict[str, Any]:
    """跑真子进程，stdout+stderr 合流写 staging log（.partial → 原子改名）。返回
    {exit_code, log_path, log_sha256, log_bytes}。超时 → kill 并抛 subprocess.TimeoutExpired
    （.partial 留在 staging 供审计，不改名——半成品不冒充完整产物）。"""
    _validate_log_name(log_name)
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
        with open(partial, "wb") as fh:
            # cwd=staging：脚本的相对路径产物（checkpoint/指标文件）落 staging（半成品目录纪律的自然延伸）。
            # Guardian 在直接子进程结束后仍要清空/reap 整棵后代树；只有这个机械边界返回
            # 后，.partial 才可能提升为不可变 final。
            try:
                result = supervisor.run(
                    cmd, stdin=None, stdout=fh, stderr=subprocess.STDOUT,
                    timeout_s=timeout_s, cwd=d,
                    # A sandbox wrapper is trusted host control code and gets
                    # a deliberately minimal env; host credentials must never
                    # be copied into its guardian spec or container.  Ordinary
                    # trusted harness calls preserve the historical inherited
                    # environment behavior.
                    env=(dict(env or {}) if sandbox_invocation is not None
                         else {**os.environ, **(env or {})}),
                    pass_fds=tuple(pass_fds), kind=execution_kind,
                    operation_context=context,
                    external_container=(
                        sandbox_invocation.external_container
                        if sandbox_invocation is not None else None))
            except BaseException as error:
                execution_error = error
            finally:
                fh.flush()
                os.fsync(fh.fileno())
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
    if sandbox_invocation is not None:
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
    # exit 侧车**先于** final 改名（原子 tmp→replace）：final 存在 ⟹ 退出码可读——崩后续跑须复用同一
    # exit 判定（非 0 的 eval 也会产 final，恢复方不得把失败进程的完整输出当成功续注册，codex BLOCKER）
    _ensure_exit_sidecar(d, log_name, exit_code)
    os.replace(partial, final)          # 原子改名：只有完整跑完的 log 得正式名（P6 staging 纪律）
    _fsync_dir(d)
    data = read_artifact_bytes(final, label="completed staged log")
    return {"exit_code": exit_code, "log_path": str(final),
            "log_sha256": hashlib.sha256(data).hexdigest(), "log_bytes": len(data),
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
