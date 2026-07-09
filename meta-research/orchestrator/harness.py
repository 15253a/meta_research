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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .ids import cnum as _cnum
from .writedaemon import WriteDaemon


def run_staged(cmd: List[str], *, staging_dir: str, log_name: str, timeout_s: float = 600.0,
               env: Optional[Dict[str, str]] = None,
               pass_fds: Sequence[int] = ()) -> Dict[str, Any]:
    """跑真子进程，stdout+stderr 合流写 staging log（.partial → 原子改名）。返回
    {exit_code, log_path, log_sha256, log_bytes}。超时 → kill 并抛 subprocess.TimeoutExpired
    （.partial 留在 staging 供审计，不改名——半成品不冒充完整产物）。"""
    d = Path(staging_dir)
    d.mkdir(parents=True, exist_ok=True)
    partial = d / (log_name + ".partial")
    final = d / log_name
    if final.exists():   # 防旧 final 冒充本次产物（重试须换名/清 staging——超时后旧 final+新 .partial 会混淆，codex NIT）
        raise FileExistsError(f"staging 已有同名 final {final}——log_name 须每次执行唯一（或先清 staging）")
    with open(partial, "wb") as fh:
        # cwd=staging：脚本的相对路径产物（checkpoint/指标文件）落 staging（半成品目录纪律的自然延伸）
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(d),
                                env={**os.environ, **(env or {})}, pass_fds=tuple(pass_fds))
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
    # exit 侧车**先于** final 改名（原子 tmp→replace）：final 存在 ⟹ 退出码可读——崩后续跑须复用同一
    # exit 判定（非 0 的 eval 也会产 final，恢复方不得把失败进程的完整输出当成功续注册，codex BLOCKER）
    exit_tmp = d / (log_name + ".exit.tmp")
    exit_tmp.write_text(str(exit_code), encoding="ascii")
    os.replace(exit_tmp, d / (log_name + ".exit"))
    os.replace(partial, final)          # 原子改名：只有完整跑完的 log 得正式名（P6 staging 纪律）
    data = final.read_bytes()
    return {"exit_code": exit_code, "log_path": str(final),
            "log_sha256": hashlib.sha256(data).hexdigest(), "log_bytes": len(data)}


def file_sha256(path: str) -> str:
    """产物文件 content_hash（checkpoint/指标值文件等入账用）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
