"""启动输入①：研究目标书解析（《第一部分》§4.6.7 机械校验的唯一实现）。

缺 frontmatter / predicate_json 缺失或非法 → 抛 GoalBriefError（编排器初始化据此
「启动失败并报错」，不建 goal、不进 bootstrap）。tests 反向 import 本实现（防两份规则漂移）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import yaml


class GoalBriefError(ValueError):
    """目标书不合启动契约（对应负例可测的启动失败，§7.5 前提 #2）。"""


def parse_goal_brief(path: Path) -> Dict[str, Any]:
    """返回 {"predicate_json": dict, "body_md": str, "frontmatter": dict}。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GoalBriefError(f"goal_brief 不可读: {path}（{e}）") from e
    if not text.startswith("---\n"):
        raise GoalBriefError("goal_brief 缺 YAML frontmatter（须以 --- 开头）")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise GoalBriefError("goal_brief frontmatter 未闭合（缺结尾 ---）")
    front = yaml.safe_load(text[4:end + 1])
    if not isinstance(front, dict) or "predicate_json" not in front:
        raise GoalBriefError("goal_brief frontmatter 缺 predicate_json 字段")
    predicate = front["predicate_json"]
    if not isinstance(predicate, dict):
        raise GoalBriefError("predicate_json 须为合法 JSON 对象")
    try:
        json.dumps(predicate, allow_nan=False)   # YAML 特有类型（日期/.nan/.inf 等）在此被拒
    except (TypeError, ValueError) as e:
        raise GoalBriefError(f"predicate_json 非合法 JSON: {e}") from e
    body = text[end + 5:].strip()
    if not body:
        raise GoalBriefError("goal_brief 缺中文正文")
    return {"predicate_json": predicate, "body_md": body, "frontmatter": front}
