"""Cycle focus is frozen business input, separate from reading layout."""
from __future__ import annotations


def validate_cycle_position(position, *, cycle_ref: str) -> None:
    if (not isinstance(position, dict)
            or set(position) != {"cycle_ref", "round", "cycle_focus", "focus_source"}
            or position["cycle_ref"] != cycle_ref
            or type(position["round"]) is not int or position["round"] < 1
            or not isinstance(position["cycle_focus"], str) or not position["cycle_focus"].strip()
            or not isinstance(position["focus_source"], dict) or not position["focus_source"]):
        raise ValueError("cycle_position_invalid")
