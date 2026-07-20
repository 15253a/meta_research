#!/usr/bin/env python3
"""Validate WildIdea search sidecar JSON file.

Usage:
  python scripts/validate_search.py outputs/<topic>.search.json [--expected N]

Exit codes:
  0 = valid
  1 = validation errors
  2 = file not found
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_STATUSES = ("found", "no_result", "failed")
VALID_DECISIONS = ("pass", "ban", "needs_manual_check")


def validate(path: Path, expected: int | None = None) -> tuple[int, list[str]]:
    """Validate the search sidecar JSON.

    Returns (exit_code, errors).
    """
    if not path.exists():
        return 2, [f"文件不存在: {path}"]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 1, [f"无法读取文件: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return 1, [f"JSON 解析失败: {exc}"]

    errors: list[str] = []

    # --- 顶层结构 ---
    if "candidates" not in data or not isinstance(data["candidates"], list):
        errors.append("缺少 candidates 数组")
        return 1, errors

    candidates = data["candidates"]
    if len(candidates) < 1:
        errors.append("candidates 数组为空，至少需要 1 条候选")
        return 1, errors

    # --- meta 检查 ---
    mode = None
    if "meta" in data and isinstance(data["meta"], dict):
        raw_mode = data["meta"].get("mode")
        if isinstance(raw_mode, str):
            mode = raw_mode

    # --- 逐条候选校验 ---
    for cand in candidates:
        if not isinstance(cand, dict):
            errors.append(f"候选条目不是对象: {cand!r}")
            continue

        cand_id = cand.get("id", "未知")

        # 必填字段
        for field in ("id", "name", "source", "searches", "decision"):
            if field not in cand:
                errors.append(f"候选 {cand_id}: 缺少字段 '{field}'")

        raw_decision = cand.get("decision")
        if raw_decision not in VALID_DECISIONS:
            errors.append(
                f"候选 {cand_id}: decision 值无效 ({raw_decision!r})"
            )

        raw_searches = cand.get("searches")
        if not isinstance(raw_searches, list) or len(raw_searches) < 1:
            errors.append(f"候选 {cand_id}: searches 非数组或为空")
            continue

        # 逐条 search 校验
        for idx, search in enumerate(raw_searches):
            if not isinstance(search, dict):
                errors.append(f"候选 {cand_id} search[{idx}]: 不是对象")
                continue

            query = search.get("query")
            if not isinstance(query, str) or not query.strip():
                errors.append(
                    f"候选 {cand_id} search[{idx}]: query 为空"
                )

            status = search.get("status")
            if status not in VALID_STATUSES:
                errors.append(
                    f"候选 {cand_id} search[{idx}]: "
                    f"status 值无效 ({status!r})"
                )

            s_decision = search.get("decision")
            if s_decision not in VALID_DECISIONS:
                errors.append(
                    f"候选 {cand_id} search[{idx}]: "
                    f"decision 值无效 ({s_decision!r})"
                )

        # --- 跨 search 逻辑校验 ---

        decision = cand.get("decision")
        searches = cand.get("searches", [])
        statuses = [s.get("status") for s in searches if isinstance(s, dict)]

        # Rule 5: ban 必须有 found 证据
        if decision == "ban" and "found" not in statuses:
            errors.append(
                f"候选 {cand_id}: decision 为 ban 但无任何 search 命中结果"
            )

        # Rule 6: pass + 有 found → 需要带 title+url 的 hits（防伪造）
        if decision == "pass" and "found" in statuses:
            for idx, search in enumerate(searches):
                if not isinstance(search, dict):
                    continue
                if search.get("status") == "found":
                    hits = search.get("hits")
                    if not isinstance(hits, list) or len(hits) < 1:
                        errors.append(
                            f"候选 {cand_id} search[{idx}]: "
                            f"status 为 found 但 hits 为空（疑似伪造搜索）"
                        )
                        continue
                    for h_idx, hit in enumerate(hits):
                        if not isinstance(hit, dict):
                            errors.append(
                                f"候选 {cand_id} search[{idx}] "
                                f"hit[{h_idx}]: 不是对象"
                            )
                            continue
                        if not hit.get("title"):
                            errors.append(
                                f"候选 {cand_id} search[{idx}] "
                                f"hit[{h_idx}]: 缺少 title"
                            )
                        if not hit.get("url"):
                            errors.append(
                                f"候选 {cand_id} search[{idx}] "
                                f"hit[{h_idx}]: 缺少 url"
                            )

        # Rule 7: 所有 search 都 failed → 不能判 pass
        if decision == "pass" and all(s == "failed" for s in statuses):
            errors.append(
                f"候选 {cand_id}: 所有 search 均失败，"
                f"decision 不应为 pass"
            )

        # Rule 8: standard 模式下 needs_manual_check 为警告
        if (
            decision == "needs_manual_check"
            and mode == "standard"
        ):
            errors.append(
                f"候选 {cand_id}: standard 模式下"
                f"decision 不应为 needs_manual_check"
            )

    # --- 数量校验 ---
    if expected is not None and len(candidates) != expected:
        errors.append(
            f"候选数量不符: 期望 {expected} 条，实际 {len(candidates)} 条"
        )

    return (1 if errors else 0), errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 WildIdea 搜索查重 sidecar JSON"
    )
    parser.add_argument(
        "sidecar",
        help="搜索 sidecar JSON 文件路径 (outputs/<topic>.search.json)",
    )
    parser.add_argument(
        "--expected",
        type=int,
        default=None,
        metavar="N",
        help="要求候选数量恰好为 N 条",
    )
    args = parser.parse_args()

    exit_code, errors = validate(Path(args.sidecar), expected=args.expected)

    if errors:
        for error in errors:
            print(f"FAIL: {error}")

    if exit_code == 0:
        print("搜索查重 sidecar 校验通过！")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
