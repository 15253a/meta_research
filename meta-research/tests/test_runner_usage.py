"""CP10.1 · runner 成本捕获（步⑩ M6 成本记账接线）。

验收：CodexRunner 成功路径从 codex stderr 抽真总 token（`tokens used\\n<N>`）+ 计墙钟秒 → Artifact.usage；
解析健壮（缺失/格式变→0，绝不因用量解析拖垮调用）。不改产物校验、不改循环行为（usage 仅携带，CP10.2 才落库）。
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
    ("blah blah no usage line here", 0),                       # 缺失 → 0
    ("tokens used\n(none)", 0),                                # 格式坏 → 0
    ("", 0),
    # —— 严格性（codex 外审 SHOULD）——
    ("tokens used\n1,abc", 0),                                 # 半解析拒（旧宽松会得 1）
    ("tokens used: 1,2,3", 0),                                 # 非法千分组拒（旧宽松会得 123）
    ("cache tokens used: 1024", 0),                            # 内嵌标签拒（非行首汇总行）
    ("prompt tokens used: 300\n", 0),                          # 同上
    ("tokens used\n100\nmore logs\ntokens used\n250\n", 250),  # 多次出现取最后一条汇总
    ("tokens used123", 0),                                     # 无分隔符粘连拒（codex NIT）
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
    assert art.usage.wallclock_sec >= 0.0                      # 墙钟已计


def test_runner_usage_zero_when_no_token_line(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(b"just some logs, no usage"))
    art = CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
    assert art.usage is not None and art.usage.tokens_total == 0   # 健壮：解析不到 → 0，不抛


def test_runner_failure_still_raises(tmp_path, monkeypatch):
    def fail_run(cmd, stdin=None, capture_output=False, timeout=None):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")   # 不写 out_file
    monkeypatch.setattr(subprocess, "run", fail_run)
    with pytest.raises(R.RunnerError):
        CodexRunner(transcripts_dir=tmp_path).run_task(system_prompt="s", skill="k", context_pack=_pack())
