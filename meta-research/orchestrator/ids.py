"""前缀化 id 编解码（资产层对外 id = 类型前缀串 c/q/a/mr… + SQLite 整型主键）。

解码同时校验类型前缀、ASCII 十进制与 SQLite 正整数上界。上界判定在 ``int()`` 之前以
字符串长度/字典序完成，因此外部产物里数千位的 id 不会触发 Python ``int_max_str_digits``
的裸 ``ValueError``，也不会在绑定 SQLite 时触发 ``OverflowError``。
"""
from __future__ import annotations

from typing import Any, Optional


SQLITE_INT_MAX = (1 << 63) - 1
_SQLITE_INT_MAX_TEXT = str(SQLITE_INT_MAX)


def parse_positive_sqlite_int(digits: Any, *, label: str = "id") -> int:
    """解析 SQLite INTEGER 可表示的正十进制数；失败只抛领域 ``ValueError``。

    为兼容旧前缀 id，允许前导零（``q0001`` 仍解为 1），但零本身不是合法主键。
    """
    if not isinstance(digits, str) or not digits or not digits.isascii() or not digits.isdigit():
        raise ValueError(f"{label} 须为 ASCII 正十进制数: {digits!r}")
    canonical = digits.lstrip("0")
    if not canonical:
        raise ValueError(f"{label} 须大于 0: {digits!r}")
    if len(canonical) > len(_SQLITE_INT_MAX_TEXT) or (
            len(canonical) == len(_SQLITE_INT_MAX_TEXT) and canonical > _SQLITE_INT_MAX_TEXT):
        raise ValueError(f"{label} 超出 SQLite INTEGER 正整数上界: {digits!r}")
    return int(canonical)


def decode(s: Any, prefix: str) -> int:
    if not (isinstance(s, str) and s.startswith(prefix) and len(s) > len(prefix)):
        raise ValueError(f"id 前缀/格式非法（期望 {prefix}<数字>）: {s!r}")
    return parse_positive_sqlite_int(s[len(prefix):], label=f"{prefix} id")


def decode_optional(s: Any, prefix: str) -> Optional[int]:
    """宽容解码：前缀/数值非法均返回 None，供 Gate/外部引用转成可行动业务拒因。"""
    try:
        return decode(s, prefix)
    except ValueError:
        return None


def cnum(s: Any) -> int: return decode(s, "c")   # cycle
def qnum(s: Any) -> int: return decode(s, "q")   # question
def anum(s: Any) -> int: return decode(s, "a")   # answer
