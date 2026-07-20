#!/usr/bin/env python3
"""Validate WildIdea poster HTML output.

Usage:
  python scripts/validate_poster.py outputs/example.html
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path


REQUIRED_CARD_CLASSES = ("slot", "source", "proto", "name", "desc", "fail")
FORBIDDEN_CLASS_NAMES = (
    "anchor",
    "match",
    "badge-row",
    "insight",
    "container",
    "its-says",
    "you-try",
)

# Slot names that must NOT appear as a card's `.source` — the source has to be a
# concrete mechanism (e.g. "Hyperband"), not the bucket label. The D1-D4 labels
# are read from references/domains.json (single source of truth, no hand-copy);
# 毛选/随机组词 are the two special slots not stored in that pool. A hardcoded
# fallback keeps the validator working if the JSON is missing or unreadable.
_DOMAINS_FILE = Path(__file__).resolve().parent.parent / "references" / "domains.json"
_SPECIAL_SLOT_LABELS = ("毛选", "随机组词")
_FALLBACK_SLOT_LABELS = ("算法技术", "学术机制", "人文艺术", "产品机制")


def _load_slot_labels():
    try:
        import json
        slot_names = json.loads(_DOMAINS_FILE.read_text(encoding="utf-8"))["slot_names"]
        pool_labels = tuple(slot_names.values())
    except (OSError, ValueError, KeyError):
        pool_labels = _FALLBACK_SLOT_LABELS
    return pool_labels + _SPECIAL_SLOT_LABELS


SLOT_LABELS = _load_slot_labels()

# Verbatim placeholder/example strings from templates/poster.html and
# references/poster-guide.md. If any survive into output, the card was not
# actually filled in.
TEMPLATE_SAMPLE_TEXT = (
    "候选机制名",
    "映射到用户领域前冻结出的通用操作机制",
    "用户领域怎么干（通俗展开）：把做法写成读者脑中能出现画面的",
    "如果这个现象没有发生，说明方向不成立",
    "映射到用户领域之前最后冻结出的通用机制",
)


# Cross-language synonym map: key = English term, value = equivalent Chinese term.
# Used to expand a user-provided forbid list so Chinese terms also block their English
# counterparts and vice versa — preventing the cheapest evasion (write proto in English
# while forbid list is Chinese).
CROSS_LANG_SYNONYMS = {
    "agent": "智能体", "drift": "漂移", "monitor": "监控", "checkpoint": "检查点",
    "trajectory": "轨迹", "research": "研究", "tool": "工具", "planning": "规划",
    "replan": "重规划", "reflection": "反思", "critic": "评审", "reward": "奖励",
    "search": "检索", "retrieval": "检索", "loop": "循环", "node": "节点",
    "intervention": "干预", "rollback": "回滚", "correct": "纠正",
}


def _normalize_term(term: str) -> str:
    """NFKC + lower-casefold so that 'Agent'/'AGENT'/'agent' all match."""
    return unicodedata.normalize("NFKC", term).casefold()


def expand_terms(terms: list[str]) -> list[str]:
    """Expand a user-provided forbid list with cross-language equivalents.

    For each term, if its normalized form has a cross-language counterpart,
    that counterpart is added to the list. Returns de-duplicated expanded list.
    """
    expanded: set[str] = set()
    rev_map = {v: k for k, v in CROSS_LANG_SYNONYMS.items()}
    for t in terms:
        expanded.add(t)
        norm = _normalize_term(t)
        if norm in CROSS_LANG_SYNONYMS:
            expanded.add(CROSS_LANG_SYNONYMS[norm])
        if t in rev_map:
            expanded.add(rev_map[t])
    return list(expanded)


def _load_search_sidecar(sidecar_path: Path):
    """Load search sidecar JSON. Accepts a direct path to the .search.json file."""
    if not sidecar_path.exists():
        # If the caller passed the HTML path, auto-derive the sidecar path.
        if sidecar_path.suffix == ".html":
            sidecar_path = sidecar_path.with_suffix(".search.json")
        if not sidecar_path.exists():
            return None, [f"搜索证据文件缺失: {sidecar_path}。标准模式要求联网搜索留痕，请生成该文件后重新校验。"]
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as e:
        return None, [f"搜索证据文件格式错误: {sidecar_path}: {e}"]


def _validate_search_entries(data: dict) -> list[str]:
    """Validate search sidecar entries against search-integration.md rules."""
    errors: list[str] = []
    cands = data.get("candidates")
    if not isinstance(cands, list) or len(cands) == 0:
        return ["搜索证据文件缺少 candidates 数组或为空"]
    mode = (data.get("meta") or {}).get("mode", "standard")
    for c in cands:
        cid = c.get("id", "?")
        name = c.get("name", "?")
        decision = c.get("decision")
        searches = c.get("searches", [])
        if not decision:
            errors.append(f"候选 {cid} {name}: 缺少 decision 字段")
            continue
        if decision not in ("pass", "ban", "needs_manual_check"):
            errors.append(f"候选 {cid} {name}: decision 值无效 '{decision}'")
            continue
        if not searches:
            errors.append(f"候选 {cid} {name}: searches 为空，至少需要一条搜索记录")
            continue
        has_found = False
        for s in searches:
            q = s.get("query", "")
            st = s.get("status", "")
            if not q:
                errors.append(f"候选 {cid} {name}: search.query 为空")
            if st not in ("found", "no_result", "failed"):
                errors.append(f"候选 {cid} {name}: search.status 值无效 '{st}'")
            sd = s.get("decision", "")
            if sd and sd not in ("pass", "ban", "needs_manual_check"):
                errors.append(f"候选 {cid} {name}: search.decision 值无效 '{sd}'")
            if st == "found":
                has_found = True
        if decision == "pass" and not has_found and all(s.get("status") == "failed" for s in searches):
            errors.append(f"候选 {cid} {name}: 所有搜索都失败，decision 不能是 pass")
        if decision == "needs_manual_check" and mode == "standard":
            errors.append(f"候选 {cid} {name}: 标准模式中 needs_manual_check 候选不应进入最终列表")
    return errors


def _classes(attrs) -> list[str]:
    for name, value in attrs:
        if name == "class" and value:
            return value.split()
    return []


class _ClassTextParser(HTMLParser):
    """Collect the inner text of every element carrying a given class.

    Tracks tag depth so collection stops exactly when the matched element
    closes. Nested inline tags (e.g. <strong> inside .proto) are included;
    sibling fields written as <div>/<p>/<span> are NOT swallowed — fixing the
    old regex that only stopped at the next <div class=...>.
    """

    def __init__(self, target_class: str):
        super().__init__(convert_charrefs=True)
        self.target = target_class
        self.results: list[str] = []
        self._depth = 0          # tag depth inside the current match (0 = not collecting)
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
        elif self.target in _classes(attrs):
            self._depth = 1
            self._buf = []

    def handle_startendtag(self, tag, attrs):
        # self-closing tag (e.g. <br/>) inside a match: contributes nothing, no depth change
        if not self._depth and self.target in _classes(attrs):
            self.results.append("")

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.results.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def _texts_of(class_name: str, html: str) -> list[str]:
    parser = _ClassTextParser(class_name)
    parser.feed(html)
    return parser.results


def text_of(class_name: str, card: str) -> str:
    """Return the inner text of the first element with class_name, tags stripped.

    Uses a real HTML parser (html.parser, stdlib) so the element body is bounded
    by its own closing tag — not by the next <div class=...>. This means fields
    laid out with <p>/<span> are split correctly and the de-anchoring guard on
    .proto cannot be silently bypassed by changing the tag.
    """
    texts = _texts_of(class_name, card)
    return texts[0] if texts else ""


def class_regex(class_name: str) -> re.Pattern[str]:
    return re.compile(rf'class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\']')


def validate(path: Path, expected_cards: int = 9, forbid_proto_terms=None, cards_explicit=True, check_int_diversity: bool = False, int_verb_min: int = 3) -> list[str]:
    errors: list[str] = []
    # Expand and normalize forbid terms: NFKC + casefold + cross-language synonyms.
    raw_terms = forbid_proto_terms or []
    norm_terms = [_normalize_term(t) for t in expand_terms(raw_terms)]

    if not path.exists():
        return [f"file not found: {path}"]

    text = path.read_text(encoding="utf-8")

    placeholders = sorted(set(re.findall(r"\{[A-Z_]+\}", text)))
    if placeholders:
        errors.append("unreplaced placeholders: " + ", ".join(placeholders))

    cards = re.findall(
        r"<article\b[^>]*class=[\"'][^\"']*\bcard\b[^\"']*[\"'][^>]*>.*?</article>",
        text,
        flags=re.S | re.I,
    )
    if len(cards) != expected_cards:
        hint = ""
        if cards_explicit is False and len(cards) < expected_cards:
            hint = f" (if this is a 精简/极端/一杀 run, pass --cards {len(cards)})"
        errors.append(f"expected {expected_cards} cards, found {len(cards)}{hint}")

    # Intervention verb tracking for diversity check.
    _INT_VERBS = ("回滚", "重规划", "截断", "降级", "重抽", "rollback", "replan", "truncate", "abort")
    cards_with_int_verb = 0

    for index, card in enumerate(cards, start=1):
        missing = [name for name in REQUIRED_CARD_CLASSES if not class_regex(name).search(card)]
        if missing:
            errors.append(f"card {index} missing classes: {', '.join(missing)}")
            continue

        # .slot must carry a real slot identifier: D1–D7, MAO, or RANDOM_WORD.
        slot_text = text_of("slot", card)
        if not re.search(r"\b(?:D[1-7]|MAO|RANDOM_WORD)\b", slot_text):
            errors.append(f"card {index}: .slot must name a slot D1–D7, got {slot_text!r}")

        # Candidate-contract content checks (SKILL.md "Candidate Contract").
        source = text_of("source", card)
        # strip a leading "来源：" / "来源:" label if present
        source_val = re.sub(r"^来源\s*[:：]\s*", "", source)
        if not source_val:
            errors.append(f"card {index}: empty .source (must name a concrete mechanism)")
        elif source_val in SLOT_LABELS:
            errors.append(
                f"card {index}: .source is the slot label '{source_val}', not a concrete source"
            )

        for cls in ("name", "proto", "desc", "fail"):
            body = text_of(cls, card)
            for sample in TEMPLATE_SAMPLE_TEXT:
                if sample in body:
                    errors.append(f"card {index}: .{cls} still contains template sample text")
                    break

        # De-anchoring guard: NFKC + casefold normalized comparison.
        proto = text_of("proto", card)
        norm_proto = _normalize_term(proto)
        for nt in norm_terms:
            if nt and nt in norm_proto:
                # Find the original term for the error message.
                orig = next((t for t in raw_terms if _normalize_term(t) == nt), nt)
                errors.append(
                    f"card {index}: .proto leaks user-domain term '{orig}' "
                    f"(source prototype must be domain-free)"
                )

        # Proto-desc similarity: detect suspected post-hoc sanitization.
        # If proto ≈ desc (after removing all forbid terms), the proto was likely
        # written by copy-pasting desc then deleting the forbidden terms.
        if norm_terms and len(norm_terms) >= 3:
            desc = text_of("desc", card)
            desc_norm = _normalize_term(desc)
            if len(desc_norm) > 50:
                desc_without_forbidden = desc_norm
                for nt in norm_terms:
                    desc_without_forbidden = desc_without_forbidden.replace(nt, "")
                coverage = 1 - len(desc_without_forbidden.replace(" ", "")) / max(len(desc_norm.replace(" ", "")), 1)
                # If >45% of desc chars are forbidden terms and proto has none,
                # the proto is very likely a sanitized copy of the desc.
                if coverage > 0.45 and all(nt not in norm_proto for nt in norm_terms):
                    errors.append(
                        f"card {index}: .proto ≈ .desc 去禁词后的洗白版 "
                        f"(疑似马后炮——先写领域方案再删禁词填入 proto，禁词覆盖率 {coverage:.0%})"
                    )

        # Intervention verb diversity tracking.
        desc_text = text_of("desc", card)
        if any(v in desc_text for v in _INT_VERBS):
            cards_with_int_verb += 1

    if not re.search(r'class=["\'][^"\']*\bban\b[^"\']*\brejected\b[^"\']*["\']', text):
        errors.append("missing .ban.rejected section")

    for class_name in FORBIDDEN_CLASS_NAMES:
        if class_regex(class_name).search(text):
            errors.append(f"forbidden old class present: {class_name}")

    if "aspect-ratio" in text:
        errors.append("fixed aspect-ratio is not allowed")

    forbidden_overflow = re.search(
        r"\.(slide|card)\s*\{[^}]*overflow\s*:\s*hidden",
        text,
        flags=re.S,
    )
    if forbidden_overflow:
        errors.append("overflow:hidden is not allowed on .slide or .card")

    for class_name in ("proto", "desc", "fail"):
        block = re.search(rf"\.{class_name}\s*\{{(?P<body>[^}}]*)\}}", text, flags=re.S)
        if not block:
            errors.append(f"missing CSS block: .{class_name}")
            continue
        css = block.group("body")
        if "word-break: break-word" not in css or "overflow-wrap: break-word" not in css:
            errors.append(f".{class_name} missing word-break/overflow-wrap guard")

    # Intervention verb diversity check: warn if too many cards use the same verbs.
    if check_int_diversity and expected_cards >= 5 and cards_with_int_verb > expected_cards - int_verb_min:
        errors.append(
            f"干预动作同质: {cards_with_int_verb}/{expected_cards} 张卡片 .desc 含同类干预动词"
            f"(回滚/重规划/截断/降级/rollback/replan 等)。多样性不足，请替换部分卡片的干预机制。"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", help="Path to generated WildIdea HTML")
    parser.add_argument("--cards", type=int, default=None, help="Expected card count (default 9)")
    parser.add_argument(
        "--forbid-proto-term",
        nargs="+",
        metavar="TERM",
        help=(
            "必填。用户领域禁词（任务名、数据类型、对象、指标、常规方法名）。"
            "脚本自动做 NFKC+大小写归一并展开中英同义词（如 agent↔智能体）。"
            "省略此项 = 关闭去锚点守卫，标准模式不允许。"
        ),
    )
    parser.add_argument(
        "--search-sidecar",
        metavar="FILE",
        help=(
            "必填。搜索证据 sidecar 文件路径（outputs/<topic>.search.json）。"
            "标准模式要求联网搜索留痕，不传直接 FAIL。"
        ),
    )
    parser.add_argument(
        "--int-verb-min",
        type=int,
        default=3,
        metavar="N",
        help="干预动词多样性检查：至少 N 张卡片的 .desc 使用【非标准】干预动词（默认 3）",
    )
    args = parser.parse_args()

    # --forbid-proto-term is mandatory in standard mode. If omitted, FAIL immediately.
    if args.forbid_proto_term is None:
        print("FAIL: --forbid-proto-term 是必填项（标准模式去锚点守卫），请传入用户领域禁词。")
        return 1

    # --search-sidecar is mandatory in standard mode. If omitted, FAIL immediately.
    if args.search_sidecar is None:
        print("FAIL: --search-sidecar 是必填项（标准模式要求联网搜索留痕）。")
        print("      请生成 outputs/<topic>.search.json 后传入，或用 --search-sidecar <path> 指定。")
        print("      搜索工具不可用时，按 search-integration.md 兜底链尝试。")
        return 1

    cards_explicit = args.cards is not None

    # Validate search sidecar first (if provided).
    sidecar_errors: list[str] = []
    if args.search_sidecar:
        sidecar_path = Path(args.search_sidecar)
        data, load_errs = _load_search_sidecar(sidecar_path)
        sidecar_errors.extend(load_errs)
        if data is not None:
            sidecar_errors.extend(_validate_search_entries(data))
        for err in sidecar_errors:
            print(f"FAIL: {err}")

    errors = validate(
        Path(args.html),
        args.cards if cards_explicit else 9,
        forbid_proto_terms=args.forbid_proto_term,
        cards_explicit=cards_explicit,
        check_int_diversity=True,
        int_verb_min=args.int_verb_min,
    )
    errors = sidecar_errors + errors

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1

    print("Poster is valid!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
