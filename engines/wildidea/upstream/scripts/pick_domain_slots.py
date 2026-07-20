#!/usr/bin/env python3
"""Randomly sample WildIdea domain slots without loading the full pool into model context.

Usage:
  python scripts/pick_domain_slots.py --type algorithm
  python scripts/pick_domain_slots.py --type product
  python scripts/pick_domain_slots.py --type algorithm --seed 42

Reroll a single slot (single entry point — no separate sub-script CLI needed):
  python scripts/pick_domain_slots.py --reroll mao
  python scripts/pick_domain_slots.py --reroll random_word
  python scripts/pick_domain_slots.py --reroll D1
  python scripts/pick_domain_slots.py --reroll D1 --exclude D1-03 D1-11

The --exclude flag takes anchor ids (shown as "id" in the output JSON, e.g.
D3-07) and prevents rerolls from drawing back an anchor already used or rejected
this round. Ids are stable and live in references/domains.json.

The domain anchor pool lives in references/domains.json so it can be extended
without touching code. SKILL.md should call this script and only read the
returned JSON, not the full pool.

By default, D1/D2/D3 entries that are themselves an already-finished real-world
cross-domain transfer (kind "AR Dataset (real cross-domain breakthrough)" or
"真实跨域研究案例") are excluded from sampling -- they're a finished analogy, not
a neutral source mechanism waiting to be transferred once. Pass
--include-completed-analogies to draw from the full pool anyway (wilder, but
risks a "double hop" analogy).

Pass --stats-path pointing at an anchor_stats.json (built by
wildidea.web.anchor_stats) to bias sampling toward anchors with a better
weak/strong feedback record. Missing/corrupt files fall back to uniform
random silently.
"""
import argparse
import json
import pathlib
import random
import sys


DOMAINS_FILE = pathlib.Path(__file__).resolve().parent.parent / "references" / "domains.json"


def _load_domains():
    try:
        with open(DOMAINS_FILE, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        sys.exit(f"domain pool not found: {DOMAINS_FILE}\n(it ships with the skill — restore references/domains.json)")
    except json.JSONDecodeError as e:
        sys.exit(f"domain pool is not valid JSON: {DOMAINS_FILE}\n{e}")
    for key in ("slot_names", "quotas", "pools"):
        if key not in doc:
            sys.exit(f"domain pool missing required key '{key}': {DOMAINS_FILE}")
    return doc


_DOC = _load_domains()
SLOT_NAMES = _DOC["slot_names"]
QUOTAS = _DOC["quotas"]
# POOLS: each row is the raw anchor dict (already carries its own stable "id").
POOLS = _DOC["pools"]
POOL_MODE_QUOTAS = {
    "default": None,
    "social_policy": {"D5": 9},
    "algorithm": {"D1": 9},
    "product": {"D4": 9},
}

# Data-derived (not guessed): checked references/domains.json directly. D1's 210
# rows split into 159 rows with kind == "AR Dataset (real cross-domain
# breakthrough)" and 51 rows of everything else; D2 has 8/49 and D3 has 13/44
# rows with kind == "真实跨域研究案例". Every one of those flagged rows already
# names a specific target domain it was transferred to (and lacks the
# "transfer_examples" field the neutral rows carry) -- it's a finished analogy,
# not raw source material. D4/D5/MAO have no such kind value at all.
COMPLETED_ANALOGY_KINDS = {
    "AR Dataset (real cross-domain breakthrough)",
    "真实跨域研究案例",
}

# Weighted sampling still rolls this fraction of the time as plain uniform
# random, regardless of feedback stats, so brand-new anchors (no strong/weak
# history yet) keep getting exposure instead of starving forever.
UNIFORM_EXPLORATION_RATE = 0.3


def _is_completed_analogy(row):
    return row.get("kind") in COMPLETED_ANALOGY_KINDS


def _load_anchor_stats(stats_path):
    """Load an anchor_stats.json ({"<anchor_id>": {"weak": int, "strong": int}}).

    Any failure (missing file, unreadable, not valid JSON, wrong shape) is
    swallowed and treated as "no stats yet" -- a broken stats file must never
    block sampling, it just silently falls back to uniform random.
    """
    if not stats_path:
        return {}
    try:
        with open(stats_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _anchor_weight(anchor_id, stats):
    entry = stats.get(anchor_id)
    if not isinstance(entry, dict):
        return 1.0
    try:
        weak = max(0, int(entry.get("weak", 0) or 0))
        strong = max(0, int(entry.get("strong", 0) or 0))
    except (TypeError, ValueError):
        return 1.0
    return (1 + strong) / (1 + weak)


def _weighted_sample_without_replacement(rows, n, stats):
    """Weighted draw without replacement, stdlib only: draw one row at a time with
    random.choices() (weight = contract formula) and remove it from the pool
    before the next draw."""
    remaining = list(rows)
    picks = []
    for _ in range(n):
        weights = [_anchor_weight(row["id"], stats) for row in remaining]
        chosen = random.choices(remaining, weights=weights, k=1)[0]
        picks.append(chosen)
        remaining.remove(chosen)  # "id" is unique per row, so no false-equal dict
    return picks


class PoolExhausted(Exception):
    """Raised when --exclude removes so many anchors a slot can't be filled.

    Carries a user-facing, actionable message; main() prints it via sys.exit
    instead of letting a raw traceback reach the model.
    """


def _import_sibling(name):
    script_dir = pathlib.Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    return __import__(name)


def sample_pool(slot, n, exclude=None, include_completed_analogies=False, stats_path=None):
    banned = set(exclude or [])
    pool = POOLS.get(slot, [])
    candidates = [
        row
        for row in pool
        if row["id"] not in banned
        and (include_completed_analogies or not _is_completed_analogy(row))
    ]
    if n > len(candidates):
        excluded_here = len(banned & {row["id"] for row in pool})
        filtered_out = 0 if include_completed_analogies else sum(1 for row in pool if _is_completed_analogy(row))
        completed_note = (
            f"（另有 {filtered_out} 条成品跨域案例被过滤，需要的话可传 include_completed_analogies=True 纳入）"
            if filtered_out
            else ""
        )
        raise PoolExhausted(
            f"槽位 {slot} 的锚点不够了：需要 {n} 条，排除 {excluded_here} 条后只剩 {len(candidates)} 条。{completed_note}\n"
            f"  这通常发生在连续多轮迭代、--exclude 累积太多时。\n"
            f"  办法：减少 --exclude 的 id 数量（少 ban 几条），或在 references/domains.json 的 {slot} 池里补充新锚点。"
        )
    stats = _load_anchor_stats(stats_path) if stats_path else {}
    if stats and random.random() >= UNIFORM_EXPLORATION_RATE:
        picks = _weighted_sample_without_replacement(candidates, n, stats)
    else:
        picks = random.sample(candidates, n)
    return [{"slot": slot, "slot_name": SLOT_NAMES[slot], **row} for row in picks]


def pick_mao(exclude=None, stats_path=None):
    # 毛选现在也是 references/domains.json 里的一个池（MAO-00..），所以直接走
    # sample_pool，天然支持 --exclude 和 PoolExhausted 兜底，存储与 D1-D4 统一。
    # MAO 池没有"成品跨域"kind 取值，include_completed_analogies 对它天然是 no-op。
    row = sample_pool("MAO", 1, exclude=exclude, stats_path=stats_path)[0]
    row["status"] = "picked"
    return row


def pick_random_word(exclude=None):
    # 随机组词没有固定池，exclude 是“本轮已用过的 query 文本”。重抽时换字直到
    # 组出一个没用过的词；多次仍撞上（极罕见）就放弃排除，不阻塞主流程。
    banned = set(exclude or [])
    search_char = _import_sibling("search_char")
    chars = search_char.load_chars()
    a = b = query = None
    for _ in range(50):
        a, b = search_char.pick_chars(chars)
        query = search_char.make_query(a, b)
        if query not in banned:
            break
    return {
        "slot": "RANDOM_WORD",
        "slot_name": SLOT_NAMES.get("RANDOM_WORD", "随机组词"),
        "chars": {"a": a, "b": b},
        "query": query,
        "status": "needs_search",
        "note": "Use current environment search, keep only real rules/events/boundaries.",
    }


def reroll(target, exclude=None, include_completed_analogies=False, stats_path=None):
    """Single entry point for rerolling one slot. target is mao/random_word/D*."""
    if target == "mao":
        return pick_mao(exclude=exclude, stats_path=stats_path)
    if target == "random_word":
        return pick_random_word(exclude=exclude)
    if target in POOLS:
        return sample_pool(
            target, 1, exclude=exclude,
            include_completed_analogies=include_completed_analogies,
            stats_path=stats_path,
        )[0]
    pool_targets = "/".join(sorted(POOLS))
    raise ValueError(f"unknown reroll target: {target} (use mao/random_word/{pool_targets})")


def quota_for(problem_type, pool_mode="default"):
    mode = (pool_mode or "default").strip()
    if mode == "default":
        return QUOTAS[problem_type]
    if mode not in POOL_MODE_QUOTAS:
        available = "/".join(sorted(POOL_MODE_QUOTAS))
        raise ValueError(f"unknown pool mode: {mode} (use {available})")
    return POOL_MODE_QUOTAS[mode]


def build_slots(problem_type, exclude=None, pool_mode="default", include_completed_analogies=False, stats_path=None):
    quota = quota_for(problem_type, pool_mode)
    slots = []
    for slot, n in quota.items():
        if slot in ("MAO", "RANDOM_WORD"):
            continue
        if slot not in POOLS:
            continue
        if n:
            slots.extend(sample_pool(
                slot, n, exclude=exclude,
                include_completed_analogies=include_completed_analogies,
                stats_path=stats_path,
            ))
    if quota.get("MAO"):
        slots.append(pick_mao(exclude=exclude, stats_path=stats_path))
    if quota.get("RANDOM_WORD"):
        slots.append(pick_random_word(exclude=exclude))
    random.shuffle(slots)
    return slots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=sorted(QUOTAS), help="algorithm/research/product/strategy")
    parser.add_argument(
        "--pool-mode",
        default="default",
        choices=sorted(POOL_MODE_QUOTAS),
        help="Preset draw distribution: default/social_policy/algorithm/product",
    )
    parser.add_argument(
        "--reroll",
        help="Reroll a single slot instead of a full draw: mao/random_word/D1/D2/D3/D4/D5",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="ID",
        help=(
            "Anchor ids already used/rejected this round, e.g. D1-03 or MAO-07 "
            "(skipped when redrawing). For 随机组词, pass the used query text to avoid repeats."
        ),
    )
    parser.add_argument(
        "--include-completed-analogies",
        action="store_true",
        help="纳入已完成过一次跨域映射的成品案例（更野但有双跳类比风险）",
    )
    parser.add_argument(
        "--stats-path",
        default=None,
        metavar="PATH",
        help=(
            'anchor_stats.json 路径（{"<anchor_id>": {"weak": int, "strong": int}}），'
            "按反馈加权采样；文件缺失或无法解析时静默退回均匀随机"
        ),
    )
    parser.add_argument("--seed", type=int, help="Optional deterministic seed")
    parser.add_argument("--stats", action="store_true", help="Only print pool counts")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.stats:
        print(json.dumps({k: len(v) for k, v in POOLS.items()}, ensure_ascii=False, indent=2))
        return

    if args.reroll:
        try:
            result = reroll(
                args.reroll,
                exclude=args.exclude,
                include_completed_analogies=args.include_completed_analogies,
                stats_path=args.stats_path,
            )
        except PoolExhausted as e:
            sys.exit(str(e))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not args.type:
        parser.error("either --type or --reroll is required")

    try:
        slots = build_slots(
            args.type,
            exclude=args.exclude,
            pool_mode=args.pool_mode,
            include_completed_analogies=args.include_completed_analogies,
            stats_path=args.stats_path,
        )
    except PoolExhausted as e:
        sys.exit(str(e))

    output = {
        "problem_type": args.type,
        "pool_mode": args.pool_mode,
        "include_completed_analogies": args.include_completed_analogies,
        "quota": quota_for(args.type, args.pool_mode),
        "slots": slots,
        "instruction": "Use only these sampled anchors for this run. Do not load the full domain pool.",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
