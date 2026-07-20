#!/usr/bin/env python3
"""方法B：随机组字，生成待搜索词。

用法：
  python3 search_char.py                        # 随机取2字→打印待搜索词
  python3 search_char.py --dry-run              # 兼容参数，行为同默认
  python3 search_char.py --query 键绒           # 用指定词（跳过随机取字）

返回JSON: {chars, query, status, note}
"""
import argparse, json, pathlib, random

CHAR_FILE = pathlib.Path(__file__).parent.parent / "references" / "common-chinese-chars.txt"

def load_chars():
    txt = CHAR_FILE.read_text(encoding="utf-8", errors="ignore")
    seen = set()
    uniq = []
    for c in txt:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq

def pick_chars(chars):
    i, j = random.sample(range(len(chars)), 2)
    return chars[i], chars[j]

def make_query(a, b):
    """把两个字拼成待搜索词。正反两种顺序随机选一种，没有优劣判断。"""
    c1, c2 = a + b, b + a
    return c1 if random.random() > 0.5 else c2

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="兼容参数，行为同默认")
    parser.add_argument("--query", type=str, help="指定搜索词（跳过随机取字）")
    args = parser.parse_args()

    if args.query:
        query = args.query
        chars_a, chars_b = None, None
    else:
        chars = load_chars()
        chars_a, chars_b = pick_chars(chars)
        query = make_query(chars_a, chars_b)

    output = {
        "chars": {"a": chars_a, "b": chars_b},
        "query": query,
        "status": "needs_search",
        "note": "Use the current environment's available search method and apply references/search-integration.md."
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
