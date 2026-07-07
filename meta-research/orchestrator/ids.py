"""前缀化 id 编解码（资产层对外 id = 类型前缀串 c/q/a/g/b… + 整型主键）。

解码**校验前缀**——防类型错 id（如把 'c1' 当问题）静默命中别的表的同号行（与 statestore_sqlite._decode
同纪律；此处集中给 importer / interaction 复用。statestore_sqlite 有等价本地 _decode，后续可统一到本模块）。
"""
from __future__ import annotations

from typing import Any


def decode(s: Any, prefix: str) -> int:
    if not (isinstance(s, str) and len(s) > 1 and s[0] == prefix and s[1:].isdigit()):
        raise ValueError(f"id 前缀/格式非法（期望 {prefix}<数字>）: {s!r}")
    return int(s[1:])


def cnum(s: Any) -> int: return decode(s, "c")   # cycle
def qnum(s: Any) -> int: return decode(s, "q")   # question
def anum(s: Any) -> int: return decode(s, "a")   # answer
