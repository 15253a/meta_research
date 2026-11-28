"""Cycle focus is frozen business input, separate from reading layout."""
from __future__ import annotations


_STAGE_LABELS = {
    "idea": "Idea",
    "plan": "Plan",
    "bundle": "Bundle",
    "reasoning": "Reasoning",
    "target": "Target",
}


def validate_cycle_position(position, *, cycle_ref: str) -> None:
    if (not isinstance(position, dict)
            or set(position) != {"cycle_ref", "round", "cycle_focus", "focus_source"}
            or position["cycle_ref"] != cycle_ref
            or type(position["round"]) is not int or position["round"] < 1
            or not isinstance(position["cycle_focus"], str) or not position["cycle_focus"].strip()
            or not isinstance(position["focus_source"], dict) or not position["focus_source"]):
        raise ValueError("cycle_position_invalid")


def format_cycle_position_line(
    position, *, stage: str, cycle_ref: str, focus_same_as_goal: bool = False
) -> str:
    if (
        not isinstance(position, dict)
        or position.get("cycle_ref") != cycle_ref
        or not isinstance(position.get("focus_source"), dict)
        or not position.get("focus_source")
    ):
        raise ValueError("cycle_position_invalid")
    label = _STAGE_LABELS.get(stage)
    if label is None:
        raise ValueError("cycle_position_stage_invalid")
    round_val = position.get("round")
    round_text = (
        f"第 {round_val} 轮 Cycle（{cycle_ref}）"
        if type(round_val) is int and round_val >= 1
        else f"当前 Cycle（{cycle_ref}）"
    )
    focus = position.get("cycle_focus")
    if focus_same_as_goal and isinstance(focus, str) and focus.strip():
        focus_text = "当前 cycle focus 同本次冻结 Quest Goal。"
    elif isinstance(focus, str) and focus.strip():
        focus_text = f"当前 cycle focus 是：‘{focus}’。"
    else:
        focus_text = "当前 cycle focus 尚未形成。"
    return f"当前你处于{round_text}的 {label} 阶段。{focus_text}\n"
