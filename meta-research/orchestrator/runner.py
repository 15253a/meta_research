"""CodexRunner —— 智能运行时窄接口的真实现（M0 起即真，《第二部分》§6.2/§6.10）。

一任务一会话：每次 run_task 起一个全新 `codex exec --ephemeral` 进程，无跨调用状态（P3）。
prompt = system_prompt + skill 节选 + 四区上下文包 + 信封提醒，全部内联（本机 bwrap 不可用，
prompt 已声明不得执行命令）。产物 = 最后一个 ```json 代码块，结构 {"files": {...}, "md": "..."}。

工程配置走环境变量（非 policy.yaml——模型/二进制是工程事实，不在附录 C 旋钮注册表内）：
  METARESEARCH_CODEX_BIN     默认 codex-chatgpt（本机已认证包装）
  METARESEARCH_CODEX_MODEL   默认 gpt-5.5
  METARESEARCH_CODEX_EFFORT  默认 medium
  METARESEARCH_RUNNER_TIMEOUT_S 默认 900（M0 工程超时；真 watchdog 见 M3/policy flow.watchdog）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .interfaces import Artifact, CallUsage, ContextPack

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
# codex CLI 在 stderr 打**汇总行**「tokens used」+ 总 token（实机：标签独占一行、数字在下一行；亦容「tokens used: N」单行）。
# 严格匹配（codex 外审 SHOULD，防误报/半解析）：①标签行首锚定（`^[ \t]*tokens used`）——不吃「cache tokens used」这类内嵌；
# ②数字须整字段消费且合法千分组（`1,800`/`21046` 收，`1,abc`/`1,2,3` 拒）；③行尾锚定。多次出现取**最后**一条（汇总）。
_TOKENS_RE = re.compile(
    # 标签与数字间须有**分隔符**（冒号 / 换行 / 空白之一）——不吃 `tokens used123` 这种粘连（codex NIT）。
    r"^[ \t]*tokens used(?:[ \t]*:[ \t]*|[ \t]*\n[ \t]*|[ \t]+)(\d{1,3}(?:,\d{3})+|\d+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_tokens_used(stderr_text: str) -> int:
    """从 codex stderr 抽总 token（步⑩ 成本记账）。无匹配 / 格式变 → 0（健壮，绝不因用量解析失败拖垮调用）。"""
    matches = _TOKENS_RE.findall(stderr_text or "")
    if not matches:
        return 0
    try:
        return int(matches[-1].replace(",", ""))       # 多次出现取最后一条汇总
    except ValueError:
        return 0


class RunnerError(RuntimeError):
    """一次 Runner 调用失败（进程失败 / 超时 / 信封不可解析）。驱动器据 artifact_parse 策略重试。"""


class CodexRunner:
    def __init__(self, *, transcripts_dir: Path, purpose_tag: str = ""):
        self.bin = os.environ.get("METARESEARCH_CODEX_BIN", "codex-chatgpt")
        self.model = os.environ.get("METARESEARCH_CODEX_MODEL", "gpt-5.5")
        self.effort = os.environ.get("METARESEARCH_CODEX_EFFORT", "medium")
        self.timeout_s = int(os.environ.get("METARESEARCH_RUNNER_TIMEOUT_S", "900"))
        self.transcripts_dir = transcripts_dir
        self.purpose_tag = purpose_tag
        self._call_no = 0

    # -- Runner Protocol -----------------------------------------------------
    def run_task(self, *, system_prompt: str, skill: str, context_pack: ContextPack) -> Artifact:
        prompt = self._build_prompt(system_prompt, skill, context_pack)
        raw, usage = self._invoke(prompt, context_pack)
        files, md = self._parse_envelope(raw)
        return Artifact(stage=context_pack.stage, files=files, md=md, usage=usage)

    # -- 内部 ------------------------------------------------------------------
    def _build_prompt(self, system_prompt: str, skill: str, pack: ContextPack) -> str:
        parts = [
            system_prompt.strip(),
            "\n\n===== 本次 SKILL 指令 =====\n", skill.strip(),
            "\n\n===== 上下文包（四区）=====\n",
            f"[cycle={pack.cycle_id} stage={pack.stage}"
            + (f" target={pack.target_id}" if pack.target_id else "") + "]\n",
            "\n--- ① 固定锚（任务关键，不截断）---\n", pack.anchor_md.strip(),
            "\n\n--- ② 结构邻域 ---\n", pack.neighborhood_md.strip() or "（空）",
            "\n\n--- ③ 检索区 ---\n", pack.retrieval_md.strip() or "（空）",
            "\n\n--- ④ 引用区（M0 无深潜工具，仅列出备查）---\n",
            "\n".join(pack.refs) or "（空）",
            "\n\n===== 输出要求 =====\n",
            "最终回复只输出一个 ```json 代码块：{\"files\": {…}, \"md\": \"…\"}；"
            "文件名与内容按上方 SKILL 指令；代码块外不得有任何文本；不得执行任何命令。",
        ]
        return "".join(parts)

    def _invoke(self, prompt: str, pack: ContextPack) -> "tuple[str, CallUsage]":
        """跑一次 codex exec，返回 (信封文本, CallUsage)。用量：stderr 报的总 token + 墙钟秒（步⑩ 成本记账）。
        用量捕获**只在成功路径**（失败→raise、不记账；失败调用的 token 属已知欠计，量小、由后续重试主导）。"""
        self._call_no += 1
        tag = f"{pack.stage}-{self.purpose_tag or 'call'}-{self._call_no}"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.transcripts_dir / f"{tag}.prompt.md"
        out_file = self.transcripts_dir / f"{tag}.out.md"
        prompt_file.write_text(prompt, encoding="utf-8")   # 快照归档供回放（P6）
        cmd = [
            self.bin, "exec",
            "-s", "read-only", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
            "-m", self.model,
            "-c", f"model_reasoning_effort={self.effort}",
            "-c", "approval_policy=never",
            "-C", str(self.transcripts_dir),
            "-o", str(out_file),
            "-",
        ]
        t0 = time.monotonic()
        try:
            with prompt_file.open("rb") as fh:
                proc = subprocess.run(
                    cmd, stdin=fh,
                    capture_output=True, timeout=self.timeout_s,
                )
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"runner 超时（{self.timeout_s}s）：{tag}") from e
        wallclock = round(time.monotonic() - t0, 3)
        if proc.returncode != 0 or not out_file.exists():
            tail = (proc.stderr or b"")[-500:].decode("utf-8", "replace")
            raise RunnerError(f"runner 进程失败（exit={proc.returncode}）：{tag}\n{tail}")
        tokens = parse_tokens_used((proc.stderr or b"").decode("utf-8", "replace"))   # codex 把用量打到 stderr
        usage = CallUsage(tokens_total=tokens, wallclock_sec=wallclock)
        return out_file.read_text(encoding="utf-8"), usage

    @staticmethod
    def _parse_envelope(raw: str) -> tuple:
        blocks = _JSON_BLOCK.findall(raw)
        if not blocks:
            raise RunnerError("信封不可解析：无 ```json 代码块")
        try:
            payload = json.loads(blocks[-1])
        except json.JSONDecodeError as e:
            raise RunnerError(f"信封 JSON 非法：{e}") from e
        if not isinstance(payload, dict) or "files" not in payload or not isinstance(payload["files"], dict):
            raise RunnerError("信封结构非法：须为 {\"files\": {...}, \"md\": \"...\"}")
        return payload["files"], str(payload.get("md", ""))
