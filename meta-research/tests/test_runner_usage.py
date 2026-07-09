"""CP10.1 · runner 成本捕获（步⑩ M6 成本记账接线）。

验收：CodexRunner 成功路径从 codex stderr 抽真总 token（`tokens used\\n<N>`）+ 计墙钟秒 → Artifact.usage；
解析健壮（缺失/格式变→None，与真 0 分开）。不改产物校验；CP10.2 在预算开启时对未知用量 fail-closed。
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from orchestrator import runner as R
from orchestrator.interfaces import ContextPack
from orchestrator.runner import CodexRunner, parse_tokens_used


# ---------------- 解析（核心逻辑，无需 codex）----------------
@pytest.mark.parametrize("text,expect", [
    ("codex\n```json\n{}\n```\ntokens used\n1,800\n", 1800),   # 实机 probe 的真格式（stderr 尾）
    ("tokens used\n12,780", 12780),
    ("tokens used: 21046", 21046),                             # 单行冒号变体
    ("blah blah no usage line here", None),                    # 缺失 → unknown
    ("tokens used\n(none)", None),                             # 格式坏 → unknown
    ("", None),
    # —— 严格性（codex 外审 SHOULD）——
    ("tokens used\n1,abc", None),                              # 半解析拒（旧宽松会得 1）
    ("tokens used: 1,2,3", None),                              # 非法千分组拒（旧宽松会得 123）
    ("cache tokens used: 1024", None),                         # 内嵌标签拒（非行首汇总行）
    ("prompt tokens used: 300\n", None),                       # 同上
    ("tokens used\n100\nmore logs\ntokens used\n250\n", 250),  # 多次出现取最后一条汇总
    ("tokens used123", None),                                  # 无分隔符粘连拒（codex NIT）
])
def test_parse_tokens_used(text, expect):
    assert parse_tokens_used(text) == expect


# ---------------- _invoke 集成（mock 掉 codex 子进程）----------------
def _pack() -> ContextPack:
    return ContextPack(cycle_id="c1", stage="idea", target_id=None,
                       anchor_md="锚", neighborhood_md="", retrieval_md="")


def _fake_run_factory(stderr: bytes, rc: int = 0):
    def fake_run(cmd, stdin=None, capture_output=False, timeout=None):
        out = Path(cmd[cmd.index("-o") + 1])                   # 生产同构：向 -o 目标写合法信封
        out.write_text('```json\n{"files":{"idea_set.json":{}},"md":"ok"}\n```', encoding="utf-8")
        return types.SimpleNamespace(returncode=rc, stdout=b"envelope-on-stdout", stderr=stderr)
    return fake_run


def test_runner_captures_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        _fake_run_factory(b"progress...\ntokens used\n1,800\n"))
    art = CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert art.files == {"idea_set.json": {}}                  # 产物校验路径不受影响
    assert art.usage is not None
    assert art.usage.tokens_total == 1800                      # 真 token 从 stderr 抽到
    assert art.usage.tokens_known is True
    assert art.usage.wallclock_sec >= 0.0                      # 墙钟已计


def test_runner_usage_zero_when_no_token_line(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(b"just some logs, no usage"))
    art = CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert art.usage is not None and art.usage.tokens_total == 0
    assert art.usage.tokens_known is False                     # 未知不再冒充真 0


def test_runner_failure_still_raises(tmp_path, monkeypatch):
    def fail_run(cmd, stdin=None, capture_output=False, timeout=None):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom\ntokens used\n2,500\n")   # 不写 out_file
    monkeypatch.setattr(subprocess, "run", fail_run)
    with pytest.raises(R.RunnerError) as ei:
        CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert ei.value.usage is not None and ei.value.usage.tokens_total == 2500
    assert ei.value.usage.wallclock_sec >= 0.0


def test_runner_bad_envelope_preserves_usage(tmp_path, monkeypatch):
    """子进程成功但信封坏：_invoke 已取得的 usage 必须随 RunnerError 上浮，供 provider 记账。"""
    def bad_envelope(cmd, stdin=None, capture_output=False, timeout=None):
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_text("模型确实跑完了，但没有 JSON 信封", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"tokens used\n3,200\n")

    monkeypatch.setattr(subprocess, "run", bad_envelope)
    with pytest.raises(R.RunnerError, match="信封不可解析") as ei:
        CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert ei.value.usage is not None and ei.value.usage.tokens_total == 3200


def test_runner_timeout_preserves_partial_usage(tmp_path, monkeypatch):
    """timeout 也至少记已刷到 stderr 的 token 与实际墙钟，不能整次消失。"""
    def timeout_run(cmd, stdin=None, capture_output=False, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout, stderr=b"progress\ntokens used\n4,100\n")

    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(R.RunnerError, match="超时") as ei:
        CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert ei.value.usage is not None and ei.value.usage.tokens_total == 4100
    assert ei.value.usage.wallclock_sec >= 0.0


def test_runner_output_read_error_preserves_usage(tmp_path, monkeypatch):
    """子进程已完成后读 out_file 失败也须携 usage 上浮，不能越过 provider 记账。"""
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(b"tokens used\n2,750\n"))
    original = Path.read_text

    def fail_out(self, *args, **kwargs):
        if self.name.endswith(".out.md"):
            raise OSError("simulated read failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_out)
    with pytest.raises(R.RunnerError, match="输出读取失败") as ei:
        CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert ei.value.usage.tokens_total == 2750 and ei.value.usage.tokens_known is True
