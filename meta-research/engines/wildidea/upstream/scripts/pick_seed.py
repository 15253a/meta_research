#!/usr/bin/env python3
"""方法A：从毛选种子库中随机抽取种子。

种子库现在统一存放在 references/domains.json 的 pools.MAO 里（每条带稳定 id
MAO-00..），不再硬编码在本文件。这样毛选和 D1-D4 用同一份外部数据，可在不改
代码的前提下扩充，也能被 pick_domain_slots.py 的 --exclude 防重抽。

pick(n) 仍返回 [(seed_no, text, hint), ...]，保持向后兼容。
"""
import json
import pathlib
import random
import sys

DOMAINS_FILE = pathlib.Path(__file__).resolve().parent.parent / "references" / "domains.json"


def load_seeds():
    """从 domains.json 读毛选池，返回 [(seed_no, text, hint), ...]。"""
    try:
        doc = json.loads(DOMAINS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"domain pool not found: {DOMAINS_FILE}\n(it ships with the skill — restore references/domains.json)")
    except json.JSONDecodeError as e:
        sys.exit(f"domain pool is not valid JSON: {DOMAINS_FILE}\n{e}")
    pool = doc.get("pools", {}).get("MAO")
    if not pool:
        sys.exit(f"domain pool missing 毛选 pool 'MAO': {DOMAINS_FILE}")
    return [(row.get("seed_no"), row["anchor"], row.get("mechanism_hint", "")) for row in pool]


SEEDS = load_seeds()


def pick(n=1):
    """随机抽 n 个种子，返回 [(seed_no, text, hint), ...]"""
    indices = random.sample(range(len(SEEDS)), min(n, len(SEEDS)))
    return [SEEDS[i] for i in indices]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    random.seed()
    picks = pick(n)
    print(json.dumps(picks, ensure_ascii=False, indent=2))
