"""Console —— 人类控制台核心：保守分类器 + directive 生命周期（§4.6.2–4.6.4；M5 CP6.1）。

**保守铁律（§4.6.2）**：任何**可能改状态**的语句一律 directive/回显确认；只有纯状态查询走 query；
低置信 → unclear（回显请确认、**不自动答、不改状态、不产 directive**——DDL：intent='unclear' 时
directive_id 必空）。分类器 = 廉价关键词规则（确定性、可回放；仿 classify_turn_intent——语义分类升级
留 M6，保守面不变：词表未命中即 unclear，绝不猜）。

**润色≠raw 时序（硬，DDL trg_iclass_directive_prov）**：raw 原文不可变落 interaction_message；分类为
directive 时**先建 directive(status='pending')**（payload_json 携润色稿 + confirmed 标志 + 分类器
provenance），分类行插入时回指该 directive。**硬指令回显确认展示润色稿**——用户确认的是润色后语义；
确认不过 → status='rejected'（不消费）。**未确认硬指令 consume 拒**（§7.1 M5）。

**消费（§4.6.4）**：consume_directive 按 consume_at 时机由调用方触发（immediate/stage_boundary =
Advancer 前置检查点；reasoning_start = reasoning 轮始）——消费 = **单事务内**读校验（防 TOCTOU）+
最小状态效果 + DECISION(actor='human'，directive_id 回指；decision 不 FK interaction_message，
provenance 经 directive 间接回溯) + 条件更新 status='consumed'。**软指令可有理由不从**：
reject_directive(by_decision=True) 记 DECISION(理由) + status='rejected'。

**pause/resume 状态模型**：pause 的消费 = 进入暂停态（该 DECISION 即记账）；**阻断谓词
has_blocking_pause = 最近一次被消费的 pause/resume 是 pause**（按消费序 consumed_decision_id）——
阻断跨越 pause 消费后的全程，直到 resume 被消费解除；pending（含已确认未消费）不阻断，调用方须
先消费到期 directive 再查阻断（Advancer 前置检查顺序，CP6.3 接线）。resume 消费顺带把**早于它的**
pending pause 置 superseded（队列清理；晚到的 pause 保留、到时机再生效）。

其余效果：abort_cycle（在途轮 aborted，并原子释放 active 问题）、inject_question（只冻结带 request_ref 的
reasoning 建题请求；question/source/authority 由 StateStore 同轮事务准入）、
prune_branch（decision(type=prune_branch) 先行再 dead_end，且**该决策即消费决策**——一次消费一条
人类决策，不重复记账）、note（按 consumed_cycle 真正编入下一次 reasoning ContextPack）。
`set_budget` 以消费 DECISION 的完整预算投影为耐久权威，所有预算消费者重启后同源读取；
`reprioritize` 的 pin/boost/suppress 由 StateStore 在 selection 提交时机械执行并另记实际效果。
`goal_amend` 的消费只登记经确认的新目标语义；真正的 GOAL 新版本由随后专用
``route='goal_amend'`` reasoning 轮在同一收尾事务中创建。消费决策与应用决策分别绑定同一
directive，通知层在应用决策提交前只显示 ``pending_effect``，避免“已消费=已改版”的假成功。

**P1**：query/reply/ACK 不写 decision；人机原文只在 interaction_*。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from typing import Any, Callable, Dict, Optional, Tuple

from .ids import cnum as _cnum, qnum as _qnum
from .durable_stop import has_active_global_stop
from .import_authority import (
    ImportAuthorityError,
    validate_github_uri,
    validate_optional_revision,
)
from .interaction import InteractionIngest
from .resource_limits import MAX_REASONING_DIRECTIVES_PER_CYCLE
from .runtime_control import apply_budget_patch, effective_budget_config
from .writedaemon import WriteDaemon

# 指令词表：kind → (触发词, hardness, consume_at)。§4.6.4 表逐行对齐。
_DIRECTIVE_RULES = [
    ("pause",           ("暂停", "pause"),                       "hard", "immediate"),
    ("resume",          ("继续", "恢复", "resume"),              "hard", "immediate"),
    ("abort_cycle",     ("中止本轮", "中止当前轮", "abort"),     "hard", "immediate"),
    ("set_budget",      ("预算", "budget"),                      "hard", "stage_boundary"),
    ("inject_question", ("注入问题", "加个问题", "inject"),      "soft", "reasoning_start"),
    ("reprioritize",    ("优先", "pin", "boost", "suppress", "降权", "提权"),
                                                                  "soft", "reasoning_start"),
    ("prune_branch",    ("剪枝", "砍掉", "prune"),               "hard", "reasoning_start"),
    ("goal_amend",      ("改目标", "修订目标", "goal amend"),    "hard", "reasoning_start"),
    ("note",            ("备注", "note:", "注："),               "soft", "reasoning_start"),
]
# query 提示词只收**实义状态词**——裸疑问助词（吗/？/?）故意不收：礼貌式指令（"停掉好吗"）常带助词，
# 若据此归 query 会被静默只读作答而非进澄清环（保守铁律：宁 unclear 勿误 query）。
_QUERY_HINTS = ("现状", "进展", "进度", "状态", "结果", "为什么", "什么", "多少", "哪",
                "status", "why", "what", "how")
_CONTINUE_ONLY_RE = re.compile(
    r"(?i)^\s*(?:继续(?:跑|运行|执行)?|接着跑|接着执行|continue|keep\s+going)\s*[。.!！?？]?\s*$")

_NUMBER = r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?"
_QREF_RE = re.compile(r"(?<![0-9A-Za-z])[qQ]([1-9][0-9]*)(?![0-9A-Za-z])")
_BUDGET_FIELD_PATTERNS = (
    ("doubling_period_m", re.compile(rf"(?i)(?:doubling_period_m|doubling[_ -]?period|翻倍周期)\s*[:=]?\s*({_NUMBER})")),
    ("B_max", re.compile(rf"(?i)(?:B_max|Bmax|单轮上限)\s*[:=]?\s*({_NUMBER})")),
    ("B0", re.compile(rf"(?i)(?:B0|单轮初始)\s*[:=]?\s*({_NUMBER})")),
    ("session_max", re.compile(rf"(?i)(?:session_max|session[_ -]?budget|总预算|预算上限|预算)\s*[:=]?\s*({_NUMBER})")),
)

# console HTTP/spool 的 operation domain 必须进入权威 append-only message；只留在 JSONL 会在 cursor
# 丢失/跨端点 nonce 复用时失去判别力。复用冻结 DDL 的 session_ref，不新增 migration。
CONSOLE_MESSAGE_SESSION_REF = "console-op:message:v1"
NARRATOR_QUERY_SESSION_REF = "console-op:narrator-query:v1"
DIRECTIVE_ACTION_SESSION_REF = "console-op:directive-action:v1"
FILE_REQUEST_ACTION_SESSION_REF = "console-op:file-request-action:v1"
_QUESTION_REQUEST_PROTOCOL = "directive-question-request-v1"


class IdempotencyCollisionError(ValueError):
    """同一 connector nonce 已绑定另一份不可变入站内容/goal。"""


class DirectiveApplicationError(ValueError):
    """A durable directive is well-formed enough to audit but cannot be applied."""


def sanitize(text: str, max_len: int = 2000) -> str:
    """消毒（中介/应答器输入用）：去控制字符 + 截断。raw 永不改，此为衍生视图。"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[:max_len]


def directive_action_text(action: str, directive_id: int, *, reason: str = "") -> str:
    """显式 directive 控件动作的唯一不可变原文口径（server/ingest/事务终检共用）。"""
    if action == "confirm":
        return f"确认指令 d{directive_id}"
    if action == "reject":
        digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        return f"拒绝指令 d{directive_id} reason_sha256:{digest}"
    raise ValueError(f"directive action 非法: {action!r}")


def is_continue_only(text: Any) -> bool:
    """Closed phrase set for the state-aware ``continue`` special case."""
    return isinstance(text, str) and _CONTINUE_ONLY_RE.fullmatch(text) is not None


def _hit(low: str, w: str) -> bool:
    """词命中：ASCII 词要求词边界（防 "pin"∈"opinion" 这类中缀假阳性——软指令假阳会污染 decision 台账）；
    CJK 词无空格分词、保持子串匹配。"""
    if w.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) is not None
    return w in low


def _load_bounded_control_json(raw: str) -> Any:
    """Parse operator JSON as a small, total control value.

    ``json.loads`` is recursive and accepts duplicate keys/non-finite numbers
    by default.  Neither behavior is suitable for a durable state mutation.
    """
    def unique_object(pairs):  # noqa: ANN001
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"重复 JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw, object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非有限 JSON number: {token}")))
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        message = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise ValueError(f"JSON 参数非法: {message}") from error
    nodes = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 16 or nodes > 4096:
            raise ValueError("JSON 参数嵌套/节点数超过控制面上限")
        if isinstance(item, dict):
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and any(0xD800 <= ord(ch) <= 0xDFFF for ch in item):
            raise ValueError("JSON 参数含非法 Unicode surrogate")
    return value


def _json_object_suffix(text: str) -> Optional[Dict[str, Any]]:
    """Parse an explicit JSON object suffix, if present; malformed JSON is an application error, not a guess."""
    start = text.find("{")
    if start < 0:
        return None
    value = _load_bounded_control_json(text[start:])
    if not isinstance(value, dict):
        raise ValueError("JSON 参数须为 object")
    return value


def _parse_budget_patch(text: str) -> Dict[str, Any]:
    """Conservative deterministic grammar for a live budget command.

    Accepted forms include ``设置预算 50``, named fields such as
    ``B0=5 B_max=20`` and an explicit JSON object.  No unit conversion or
    inferred field other than the common single ``预算 <n>`` → session ceiling
    shorthand is performed.
    """
    explicit = _json_object_suffix(text)
    if explicit is not None:
        return explicit
    patch: Dict[str, Any] = {}
    for name, pattern in _BUDGET_FIELD_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        raw = match.group(1)
        try:
            number = float(raw)
        except (OverflowError, ValueError) as error:
            raise ValueError(f"{name} 数值非法: {raw!r}") from error
        if not math.isfinite(number):
            raise ValueError(f"{name} 须为有限数字")
        if name == "doubling_period_m":
            if not number.is_integer():
                raise ValueError("doubling_period_m 须为正整数")
            patch[name] = int(number)
        else:
            patch[name] = number
    if not patch:
        raise ValueError("未识别预算值；请用“设置预算 50”或显式字段/JSON")
    return patch


def _parse_reprioritize(text: str) -> Dict[str, Any]:
    """Parse pin/boost/suppress without inventing a target or adjustment."""
    low = text.lower()
    qmatch = _QREF_RE.search(text)
    if qmatch is None:
        raise ValueError("reprioritize 缺 question_id（例如 q17）")
    question_id = f"q{qmatch.group(1)}"
    if _hit(low, "pin") or "固定优先" in low or (
            "优先" in low and not any(word in low for word in ("提权", "降权", "boost", "suppress"))):
        return {"mode": "pin", "question_id": question_id}
    if _hit(low, "suppress") or "降权" in low:
        mode = "suppress"
    elif _hit(low, "boost") or "提权" in low:
        mode = "boost"
    else:
        raise ValueError("reprioritize 须明确 pin / boost(提权) / suppress(降权)")

    # Remove qN before looking for the magnitude so the question id itself can
    # never be mistaken for an adjustment.
    without_q = text[:qmatch.start()] + text[qmatch.end():]
    explicit = re.search(rf"(?i)adjust\s*[:=]\s*({_NUMBER})", without_q)
    numbers = re.findall(_NUMBER, without_q) if explicit is None else []
    if explicit is None and len(numbers) > 1:
        raise ValueError(f"{mode} 出现多个数值；请用 adjust=<数值> 明确调整量")
    raw = explicit.group(1) if explicit is not None else (numbers[0] if numbers else None)
    if raw is None:
        raise ValueError(f"{mode} 缺调整量（例如 {mode} {question_id} 0.25）")
    try:
        magnitude = abs(float(raw))
    except (OverflowError, ValueError) as error:
        raise ValueError(f"reprioritize 调整量非法: {raw!r}") from error
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("reprioritize 调整量须为有限正数")
    return {"mode": mode, "question_id": question_id,
            "adjust": magnitude if mode == "boost" else -magnitude}


def _parse_goal_amend(text: str) -> Dict[str, Any]:
    """Parse a confirmed goal revision without asking the model to guess it.

    The explicit form is a JSON object containing ``new_goal_text`` and
    optionally ``predicate_json`` / ``rationale_md``.  The shorthand form is
    deliberately narrow: text after 改目标/修订目标/``goal amend`` becomes the
    complete new goal text.  The confirmed payload is therefore a stable,
    machine-checkable authority for the later reasoning tree operation.
    """
    match = re.search(r"(?i)(改目标|修订目标|goal\s+amend)", text)
    if match is None:
        raise ValueError("goal_amend 缺明确的新目标")
    suffix = text[match.end():].strip()
    suffix = re.sub(r"^(?:[：:，,\-]\s*)", "", suffix)
    if suffix.startswith("{"):
        explicit = _load_bounded_control_json(suffix)
        if not isinstance(explicit, dict):
            raise ValueError("goal_amend JSON 参数须为 object")
        allowed = {"new_goal_text", "predicate_json", "rationale_md"}
        unknown = set(explicit) - allowed
        if unknown:
            raise ValueError(f"goal_amend 不允许字段: {sorted(unknown)}")
        value = dict(explicit)
    else:
        new_text = re.sub(r"^(?:改成|改为|为|to)\s*", "", suffix,
                          flags=re.IGNORECASE).strip()
        value = {"new_goal_text": new_text}

    new_goal_text = value.get("new_goal_text")
    if not isinstance(new_goal_text, str) or not new_goal_text.strip():
        raise ValueError("goal_amend.new_goal_text 须为非空字符串")
    value["new_goal_text"] = new_goal_text.strip()
    predicate = value.get("predicate_json")
    if "predicate_json" in value and not isinstance(predicate, dict):
        raise ValueError("goal_amend.predicate_json 须为 JSON object")
    rationale = value.get("rationale_md", "用户明确修订研究目标")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("goal_amend.rationale_md 须为非空字符串")
    value["rationale_md"] = rationale.strip()
    return value


def _parse_inject_question(text: str) -> Dict[str, Any]:
    """Parse an injected question without inventing import authority.

    The legacy shorthand remains a soft question injection.  A human-named
    repository is accepted only in an explicit JSON object and is promoted to
    a hard, confirmation-required directive by ``KeywordClassifier``.
    """
    match = re.search(r"(?i)(注入问题|加个问题|inject(?:\s+question)?)", text)
    if match is None:
        raise ValueError("inject_question 缺明确问题")
    suffix = text[match.end():].strip()
    suffix = re.sub(r"^(?:[：:，,\-]\s*)", "", suffix)
    if not suffix:
        raise ValueError("inject_question.question_text 须为非空字符串")
    if not suffix.startswith("{"):
        return {"question_text": suffix}

    value = _load_bounded_control_json(suffix)
    if not isinstance(value, dict):
        raise ValueError("inject_question JSON 参数须为 object")
    allowed = {
        "question_text", "parent_question_id", "human_named_repo",
        "need_summary",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"inject_question 不允许字段: {sorted(unknown)}")
    question_text = value.get("question_text")
    if not isinstance(question_text, str) or not question_text.strip():
        raise ValueError("inject_question.question_text 须为非空字符串")
    if len(question_text.encode("utf-8")) > 8192:
        raise ValueError("inject_question.question_text 超过 8192 bytes")
    if any((ord(ch) < 0x20 and ch not in "\n\r\t") or ord(ch) == 0x7F
           for ch in question_text):
        raise ValueError("inject_question.question_text 含非法控制字符")
    result: Dict[str, Any] = {"question_text": question_text.strip()}
    parent = value.get("parent_question_id")
    if parent is not None:
        if not isinstance(parent, str) or _QREF_RE.fullmatch(parent) is None:
            raise ValueError("inject_question.parent_question_id 须为 q<正整数>")
        try:
            result["parent_question_id"] = f"q{_qnum(parent.lower())}"
        except ValueError as error:
            raise ValueError(str(error)) from error
    repo = value.get("human_named_repo")
    if repo is None:
        if "need_summary" in value:
            raise ValueError("need_summary 只允许与 human_named_repo 同时出现")
        return result
    if not isinstance(repo, dict) or not set(repo).issubset(
            {"canonical_uri", "requested_revision"}) \
            or "canonical_uri" not in repo:
        raise ValueError(
            "human_named_repo 须精确包含 canonical_uri 与可选 requested_revision")
    try:
        canonical_uri = validate_github_uri(repo["canonical_uri"])
        requested_revision = validate_optional_revision(
            repo.get("requested_revision"))
    except ImportAuthorityError as error:
        raise ValueError(str(error)) from error
    need_summary = value.get("need_summary")
    if not isinstance(need_summary, str) or not need_summary.strip():
        raise ValueError("human_named_repo 必须带非空 need_summary")
    if len(need_summary.encode("utf-8")) > 8192:
        raise ValueError("human_named need_summary 超过 8192 bytes")
    if any((ord(ch) < 0x20 and ch not in "\n\r\t") or ord(ch) == 0x7F
           for ch in need_summary):
        raise ValueError("human_named need_summary 含非法控制字符")
    result["human_named_repo"] = {
        "canonical_uri": canonical_uri,
        "requested_revision": requested_revision,
    }
    result["need_summary"] = need_summary.strip()
    return result


class KeywordClassifier:
    """廉价关键词保守分类（确定性）。返回 {intent, kind?, hardness?, consume_at?, polished?}。"""

    def classify(self, message: Dict[str, Any]) -> Dict[str, Any]:
        text = sanitize(str(message.get("raw_text", "")))
        low = text.lower()
        explicit_budget_fields = any(pattern.search(text) is not None
                                     for _, pattern in _BUDGET_FIELD_PATTERNS)
        budget_query = any(phrase in low for phrase in (
            "预算多少", "预算还剩", "剩余预算", "当前预算", "预算状态", "budget status", "remaining budget"))
        budget_mutation = any(phrase in low for phrase in (
            "设置", "调整", "改为", "提高", "降低", "set budget", "change budget", "adjust budget"))
        if budget_query and not budget_mutation:
            return {"intent": "query"}
        for kind, words, hardness, consume_at in _DIRECTIVE_RULES:
            if (kind == "set_budget" and explicit_budget_fields) or any(_hit(low, w) for w in words):
                structured: Dict[str, Any] = {}
                parse_error = None
                try:
                    if kind == "set_budget":
                        structured["budget_patch"] = _parse_budget_patch(text)
                    elif kind == "inject_question":
                        structured.update(_parse_inject_question(text))
                    elif kind == "reprioritize":
                        structured.update(_parse_reprioritize(text))
                    elif kind == "goal_amend":
                        structured.update(_parse_goal_amend(text))
                except ValueError as error:
                    parse_error = str(error)
                    structured["parse_error"] = parse_error
                pin_wording = (kind == "reprioritize" and (
                    structured.get("mode") == "pin" or _hit(low, "pin")
                    or ("优先" in low and not any(
                        word in low for word in ("提权", "降权", "boost", "suppress")))))
                if pin_wording:
                    hardness = "hard"                  # reference: pin hard; boost/suppress soft
                if kind == "inject_question" and structured.get("human_named_repo"):
                    # Human-named repositories bypass the type/discovery gate,
                    # so the structured meaning must pass the hard confirmation
                    # boundary.  It still does not bypass license/materialize.
                    hardness = "hard"
                if structured and parse_error is None:
                    polished = f"[{kind}] " + json.dumps(
                        structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                else:
                    polished = f"[{kind}] {text.strip()}"
                # note 是 DDL 独立 intent（分类行 directive_id 必空）；其余指令词 → directive
                return {"intent": "note" if kind == "note" else "directive",
                        "kind": kind, "hardness": hardness, "consume_at": consume_at,
                        "polished": polished, "structured": structured}
        if any(_hit(low, h) for h in _QUERY_HINTS) and text.strip():
            return {"intent": "query"}
        return {"intent": "unclear"}          # 词表未命中 → 不猜（保守铁律：绝不静默当 query/directive）


class Console:
    def __init__(self, daemon: WriteDaemon, classifier=None, policy: Optional[Dict[str, Any]] = None,
                 continue_snapshot: Optional[
                     Callable[[], Tuple[Optional[str], str]]] = None):
        self.daemon = daemon
        self.ingest = InteractionIngest(daemon)
        self.classifier = classifier or KeywordClassifier()
        self.policy = policy
        self.continue_snapshot = continue_snapshot

    # ---------------------------------------------------------------- 入站 --
    def handle_inbound(self, *, connector: str, raw_text: str, idempotency_key: str,
                       goal_id: Optional[int] = None, goal_ver: Optional[int] = None,
                       cycle_id: Optional[str] = None,
                       session_ref: Optional[str] = None,
                       conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """durable 入站 → 恰一分类（幂等：message UNIQUE）→ directive/note 先建行再回指（DDL 时序）。
        返回 {message_id, intent, directive_id?, needs_confirmation?}。unclear：不自动答不产 directive
        （ACK 回显请确认由通知层出，CP6.3）。"""
        if is_continue_only(raw_text):
            return self._handle_continue_atomic(
                connector=connector, raw_text=raw_text,
                idempotency_key=idempotency_key, goal_id=goal_id,
                goal_ver=goal_ver, cycle_id=cycle_id,
                session_ref=session_ref, conversation_id=conversation_id)
        mid = self.ingest.inbound(connector=connector, raw_text=raw_text, idempotency_key=idempotency_key,
                                  goal_id=goal_id, goal_ver=goal_ver, cycle_id=cycle_id,
                                  session_ref=session_ref, conversation_id=conversation_id)
        # InteractionIngest 的 UNIQUE 只负责找回 message id；在读取既有 classification 或调用分类器前，
        # 必须先证明 replay 的不可变 payload 相同。否则“首事务只落 message 后崩溃”的窗口里，撞键 body
        # 可把自己的 directive 语义提交到另一条 raw message 上。cycle_id 不参与比较：传输重放可能跨
        # precheck/cycle 才到达，但仍应收敛到首次 durable message。
        stored = self.daemon.query_one(
            "SELECT connector,raw_text,raw_hash,goal_id,goal_ver,session_ref,conversation_id "
            "FROM interaction_message WHERE id=?", (mid,))
        expected_hash = "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        if stored != (connector, raw_text, expected_hash, goal_id, goal_ver,
                      session_ref, conversation_id):
            raise IdempotencyCollisionError(
                f"{connector} idempotency_key 已绑定其他不可变消息: {idempotency_key}")
        ex = self._existing_classification(mid)
        if ex:                                 # 幂等重放：分类恰一（UNIQUE），返回既有
            return ex
        c = self.classifier.classify({"raw_text": raw_text})
        if (c.get("kind") == "set_budget"
                and (self.policy is None or not isinstance(self.policy.get("budget"), dict))):
            # A Console assembled without the versioned policy is intentionally
            # useful for protocol/ingest diagnostics, but must not advertise a
            # budget mutation that it cannot turn into a durable complete
            # projection.  Production build_system always supplies the policy.
            c = {"intent": "unclear"}
        ci = _cnum(cycle_id) if cycle_id else None
        try:
            with self.daemon.transaction() as conn:
                did = None
                if c["intent"] in ("directive", "note"):
                    kind = c.get("kind", "note")
                    hardness = c.get("hardness", "soft")
                    payload = {"polished": c.get("polished", sanitize(raw_text)),
                               "confirmed": hardness != "hard",     # 软指令免确认；硬指令须回显确认后置 true
                               "classifier": "keyword-v1"}
                    structured = c.get("structured") or {}
                    if not isinstance(structured, dict):
                        raise ValueError("classifier.structured 须为 object")
                    reserved = set(structured) & {"polished", "confirmed", "classifier",
                                                   "confirmation_message_id"}
                    if reserved:
                        raise ValueError(f"classifier.structured 不得覆盖 directive 控制字段: {sorted(reserved)}")
                    payload.update(structured)
                    did = conn.execute(
                        "INSERT INTO directive(kind,hardness,status,consume_at,payload_json,created_cycle,"
                        "source_interaction_message_id) VALUES (?,?,'pending',?,?,?,?)",
                        (kind, hardness, c.get("consume_at", "reasoning_start"),
                         json.dumps(payload, ensure_ascii=False), ci, mid)).lastrowid
                # note 的分类行 directive_id 必空（DDL CHECK：仅 intent='directive' 携 id）；note 的 directive
                # 行仍建（§4.6.3：note → directive(note, 软)），provenance 经 source_interaction_message_id。
                conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) VALUES (?,?,?)",
                             (mid, c["intent"], did if c["intent"] == "directive" else None))
        except sqlite3.IntegrityError:
            # 并发重放窗口：预检后、插入前别处已落分类 → UNIQUE(message_id) 冲突整体回滚（directive 同事务、
            # 不残留）；回读既有分类返回，不向上炸（幂等语义）
            ex = self._existing_classification(mid)
            if ex is None:
                raise
            return ex
        return {"message_id": mid, "intent": c["intent"], "directive_id": did,
                "needs_confirmation": bool(did) and c.get("hardness") == "hard" and c["intent"] == "directive"}

    def handle_query_inbound(self, *, connector: str, raw_text: str,
                             idempotency_key: str,
                             goal_id: Optional[int] = None,
                             goal_ver: Optional[int] = None,
                             cycle_id: Optional[str] = None,
                             session_ref: str = NARRATOR_QUERY_SESSION_REF,
                             conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Persist an authenticated narrator request as an unconditionally read-only query.

        The narrator transport is a different authority from the command box.  Its text may
        mention words such as ``pause`` or ``budget`` while asking for an explanation; running
        that text through the directive classifier would let a read-only UI accidentally create
        state-changing intent.  The explicit transport marker therefore owns classification,
        while the immutable message/replay checks remain identical to ``handle_inbound``.
        """
        if session_ref != NARRATOR_QUERY_SESSION_REF:
            raise ValueError("讲解员 query session_ref 不合法")
        mid = self.ingest.inbound(
            connector=connector, raw_text=raw_text,
            idempotency_key=idempotency_key, goal_id=goal_id,
            goal_ver=goal_ver, cycle_id=cycle_id, session_ref=session_ref,
            conversation_id=conversation_id)
        expected_hash = "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        stored = self.daemon.query_one(
            "SELECT connector,raw_text,raw_hash,goal_id,goal_ver,session_ref,conversation_id "
            "FROM interaction_message WHERE id=?", (mid,))
        if stored != (connector, raw_text, expected_hash, goal_id, goal_ver,
                      session_ref, conversation_id):
            raise IdempotencyCollisionError(
                f"{connector} idempotency_key 已绑定其他不可变消息: {idempotency_key}")
        with self.daemon.transaction() as conn:
            existing = conn.execute(
                "SELECT intent,directive_id FROM interaction_classification WHERE message_id=?",
                (mid,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO interaction_classification(message_id,intent,directive_id) "
                    "VALUES (?,'query',NULL)", (mid,))
            elif tuple(existing) != ("query", None):
                raise IdempotencyCollisionError(
                    f"讲解员 query 已绑定非 query 分类: message_id={mid}")
        return {"message_id": mid, "intent": "query", "directive_id": None,
                "needs_confirmation": False}

    def _handle_continue_atomic(self, *, connector: str, raw_text: str,
                                idempotency_key: str, goal_id: Optional[int],
                                goal_ver: Optional[int], cycle_id: Optional[str],
                                session_ref: Optional[str],
                                conversation_id: Optional[str]) -> Dict[str, Any]:
        """Freeze message arrival, pause-state interpretation and ACK/directive atomically.

        A two-transaction implementation can reinterpret the same immutable
        message after a crash if pause/resume changes in between.  This path is
        deliberately self-contained in one WriteDaemon transaction.
        """
        if (goal_id is None) != (goal_ver is None):
            raise ValueError("goal_id 与 goal_ver 须同为 None 或同非 None")
        if session_ref is not None and (
                not isinstance(session_ref, str) or not session_ref or len(session_ref) > 256
                or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in session_ref)):
            raise ValueError("session_ref 须为 1–256 字符且不得含控制字符")
        if conversation_id is not None and (
                not isinstance(conversation_id, str) or not conversation_id
                or len(conversation_id) > 128
                or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in conversation_id)):
            raise ValueError("conversation_id 须为 1–128 字符且不得含控制字符")
        ci = _cnum(cycle_id) if cycle_id else None
        expected_hash = "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        snapshot: Optional[str] = None
        ack_text: Optional[str] = None
        if self.continue_snapshot is not None:
            try:
                snapshot, ack_text = self.continue_snapshot()
            except (OSError, ValueError, RuntimeError):
                snapshot, ack_text = None, None
        with self.daemon.transaction() as conn:
            existing = conn.execute(
                "SELECT id,connector,raw_text,raw_hash,goal_id,goal_ver,session_ref,conversation_id "
                "FROM interaction_message WHERE connector=? AND idempotency_key=?",
                (connector, idempotency_key)).fetchone()
            if existing is not None:
                mid = int(existing[0])
                if existing[1:] != (connector, raw_text, expected_hash, goal_id, goal_ver,
                                    session_ref, conversation_id):
                    raise IdempotencyCollisionError(
                        f"{connector} idempotency_key 已绑定其他不可变消息: {idempotency_key}")
                classified = conn.execute(
                    "SELECT intent FROM interaction_classification WHERE message_id=?", (mid,)).fetchone()
                if classified is None:
                    # A legacy/pre-upgrade half-ingest has no durable arrival-state
                    # snapshot.  Never guess resume from today's pause state.
                    conn.execute(
                        "INSERT INTO interaction_classification(message_id,intent,directive_id) "
                        "VALUES (?,'unclear',NULL)", (mid,))
                    text = "这条‘继续’消息的到达状态无法安全恢复；系统未执行 resume，请重新发送。"
                    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
                    conn.execute(
                        "INSERT INTO interaction_reply(message_id,reply_ref,reply_hash,reply_text,"
                        "snapshot_cycle,responder_kind) VALUES (?,?,?,?,NULL,'template')",
                        (mid, f"reply:{mid}:final-template", digest, text))
            else:
                mid = conn.execute(
                    "INSERT INTO interaction_message(connector,conversation_id,session_ref,goal_id,goal_ver,"
                    "cycle_id,raw_text,raw_hash,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
                    (connector, conversation_id, session_ref, goal_id, goal_ver, ci,
                     raw_text, expected_hash, idempotency_key)).lastrowid
                latest_control = conn.execute(
                    "SELECT kind FROM directive WHERE status='consumed' "
                    "AND kind IN ('pause','resume') ORDER BY consumed_decision_id DESC LIMIT 1").fetchone()
                paused = latest_control is not None and latest_control[0] == "pause"
                if paused:
                    payload = {
                        "polished": f"[resume] {raw_text.strip()}",
                        "confirmed": False,
                        "classifier": "continue-special-v1",
                    }
                    did = conn.execute(
                        "INSERT INTO directive(kind,hardness,status,consume_at,payload_json,created_cycle,"
                        "source_interaction_message_id) VALUES ('resume','hard','pending','immediate',?,?,?)",
                        (json.dumps(payload, ensure_ascii=False), ci, mid)).lastrowid
                    conn.execute(
                        "INSERT INTO interaction_classification(message_id,intent,directive_id) "
                        "VALUES (?,'directive',?)", (mid, did))
                else:
                    did = None
                    conn.execute(
                        "INSERT INTO interaction_classification(message_id,intent,directive_id) "
                        "VALUES (?,'query',NULL)", (mid,))
                    if snapshot is not None:
                        try:
                            snapshot_id = _cnum(snapshot)
                        except ValueError:
                            snapshot_id = None
                        if snapshot_id is not None and conn.execute(
                                "SELECT 1 FROM cycle WHERE id=?", (snapshot_id,)).fetchone() is None:
                            snapshot_id = None
                    else:
                        fallback = conn.execute("SELECT id FROM cycle ORDER BY id DESC LIMIT 1").fetchone()
                        snapshot_id = int(fallback[0]) if fallback is not None else None
                        snapshot = f"c{snapshot_id}" if snapshot_id is not None else None
                    if not isinstance(ack_text, str) or not ack_text.strip():
                        ack_text = (f"[快照 {snapshot}] 已在继续；本消息未产生状态变更。"
                                    if snapshot is not None else
                                    "已在继续；尚无已发布快照，本消息未产生状态变更。")
                    ack_text = ack_text.strip()
                    if (len(ack_text) > 4_000
                            or any((ord(ch) < 0x20 and ch not in "\n\t")
                                   or ord(ch) == 0x7f for ch in ack_text)):
                        raise ValueError("continue ACK 文本非法")
                    digest = "sha256:" + hashlib.sha256(ack_text.encode("utf-8")).hexdigest()
                    conn.execute(
                        "INSERT INTO interaction_reply(message_id,reply_ref,reply_hash,reply_text,"
                        "snapshot_cycle,responder_kind) VALUES (?,?,?,?,?,'template')",
                        (mid, f"reply:{mid}:final-template", digest, ack_text, snapshot_id))
        result = self._existing_classification(mid)
        if result is None:
            raise RuntimeError("continue special 未产生 classification")
        return result

    def _existing_classification(self, mid: int) -> Optional[Dict[str, Any]]:
        """既有分类 → 幂等返回值（与首次返回**等价**，含 needs_confirmation——重放丢首次响应后调用方
        仍能据此触发确认 UI）；note 分类行 directive_id 必空，经 directive.source 回指找回。"""
        ex = self.daemon.query_one(
            "SELECT c.intent,c.directive_id,m.raw_text,EXISTS("
            " SELECT 1 FROM interaction_reply r WHERE r.message_id=m.id "
            " AND r.reply_ref=('reply:' || m.id || ':final-template')) "
            "FROM interaction_classification c JOIN interaction_message m ON m.id=c.message_id "
            "WHERE c.message_id=?", (mid,))
        if ex is None:
            return None
        did = ex[1]
        if did is None and ex[0] == "note":
            row = self.daemon.query_one("SELECT id FROM directive WHERE source_interaction_message_id=?", (mid,))
            did = row[0] if row else None
        needs = False
        if did is not None and ex[0] == "directive":
            dr = self.daemon.query_one("SELECT hardness, status, payload_json FROM directive WHERE id=?", (did,))
            needs = bool(dr) and dr[0] == "hard" and dr[1] == "pending" and not json.loads(dr[2]).get("confirmed")
        result = {"message_id": mid, "intent": ex[0], "directive_id": did,
                  "needs_confirmation": needs}
        if ex[0] == "query" and bool(ex[3]) and is_continue_only(ex[2]):
            result["special"] = "continue_running"
        return result

    # ---------------------------------------------------------------- 确认 --
    @staticmethod
    def _validate_action_provenance(conn, *, directive_id: int, source_message_id: int,
                                    action_message_id: int, action: str, reason: str = "") -> None:
        """在最终状态迁移事务内验证控件消息，而不是信任上游调用者已经检查过。

        action message 必须是 ``unclear`` 分类、具有 deterministic raw，且 goal 绑定与 directive 的
        source message 完全一致（含 NULL/goal_ver）。这样任意既有消息、原 directive 源消息或跨 goal
        消息都不能冒充确认/拒绝 provenance。
        """
        action_row = conn.execute(
            "SELECT m.raw_text,m.goal_id,m.goal_ver,c.intent,c.directive_id,m.session_ref,"
            "m.connector,m.conversation_id "
            "FROM interaction_message m LEFT JOIN interaction_classification c ON c.message_id=m.id "
            "WHERE m.id=?", (action_message_id,)).fetchone()
        if action_row is None:
            raise ValueError(f"{action} provenance 消息不存在: {action_message_id}")
        source_row = conn.execute(
            "SELECT goal_id,goal_ver,session_ref,connector,conversation_id "
            "FROM interaction_message WHERE id=?", (source_message_id,)).fetchone()
        if source_row is None:
            raise ValueError(f"directive {directive_id} source provenance 消息不存在")
        if source_row[3] == "console" or source_row[2] is None:
            expected_raw = directive_action_text(action, directive_id, reason=reason)
        else:
            expected_raw = (f"确认指令 d{directive_id}" if action == "confirm"
                            else f"拒绝指令 d{directive_id}")
        if action_row[0] != expected_raw:
            raise ValueError(f"directive {directive_id} {action} provenance 原文不符")
        if (action_row[3], action_row[4]) != ("unclear", None):
            raise ValueError(f"directive {directive_id} {action} provenance 须为 unclear 控件消息")
        if (action_row[1], action_row[2]) != source_row[:2]:
            raise ValueError(f"directive {directive_id} {action} provenance 与 source goal 不一致")
        if action_row[6] != source_row[3] or action_row[7] != source_row[4]:
            raise ValueError(f"directive {directive_id} {action} provenance 跨 connector/conversation")
        if source_row[3] == "console":
            if action_row[5] != DIRECTIVE_ACTION_SESSION_REF:
                raise ValueError(f"directive {directive_id} {action} provenance 操作域不符")
        elif source_row[2] is None:
            # Compatibility for pre-connector rows.  Authenticated connector
            # ingress never emits a NULL session_ref.
            if action_row[5] is not None:
                raise ValueError(f"directive {directive_id} {action} legacy 操作域不符")
        else:
            source_session = source_row[2]
            if (not isinstance(source_session, str)
                    or not source_session.startswith("connector-inbound-v1:")
                    or action_row[5] != source_session + ":action"):
                raise ValueError(f"directive {directive_id} {action} principal/profile binding 不一致")

    def confirm_directive(self, *, directive_id: int, confirm_message_id: int) -> None:
        """硬指令回显确认（用户确认的是润色稿语义）：payload.confirmed=true + 确认消息 provenance。
        directive 无 append-only 触发器（状态机表），UPDATE 合法；status 保持 pending（待时机消费）。
        读校验与更新同事务（防 TOCTOU，同 consume）。"""
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,hardness,payload_json,source_interaction_message_id,kind FROM directive WHERE id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可确认")
            if row[1] != "hard":
                raise ValueError(f"directive {directive_id} 是软指令，无需回显确认")
            self._validate_action_provenance(
                conn, directive_id=directive_id, source_message_id=row[3],
                action_message_id=confirm_message_id, action="confirm")
            payload = json.loads(row[2])
            if payload.get("confirmed") is True:
                if payload.get("confirmation_message_id") == confirm_message_id:
                    return
                raise ValueError(f"directive {directive_id} 已由另一条消息确认")
            if payload.get("confirmed") is not False:
                raise ValueError(f"directive {directive_id} confirmed 字段损坏")
            payload["confirmed"] = True
            payload["confirmation_message_id"] = confirm_message_id
            if row[4] == "goal_amend":
                # 解析失败的“修订”没有可确认的机械语义。确认动作仍作为 provenance 留在 payload，
                # 但同事务终态拒绝并记 orchestrator decision；它绝不能覆盖旧的有效修订或占用专用轮。
                if payload.get("parse_error"):
                    reason = f"goal_amend 参数未解析: {payload['parse_error']}"
                    payload["rejection_reason"] = reason[:2_000]
                    payload["rejection_kind"] = "application_unavailable"
                    conn.execute(
                        "INSERT INTO decision(directive_id,actor,type,payload_json) "
                        "VALUES (?,'orchestrator','directive_application_rejected',?)",
                        (directive_id, json.dumps(
                            {"reason": reason[:2_000]}, ensure_ascii=False)))
                    changed = conn.execute(
                        "UPDATE directive SET status='rejected',payload_json=? "
                        "WHERE id=? AND status='pending'",
                        (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
                    if changed != 1:
                        raise RuntimeError(f"directive {directive_id} 终态拒绝竞态：更新失败")
                    return
                source_goal = conn.execute(
                    "SELECT goal_id,goal_ver FROM interaction_message WHERE id=?", (row[3],)).fetchone()
                current = (conn.execute(
                    "SELECT id,version FROM goal WHERE id=? ORDER BY version DESC LIMIT 1",
                    (source_goal[0],)).fetchone()
                    if source_goal is not None and source_goal[0] is not None else None)
                # A revision is meaningful only against the exact immutable
                # goal version the user saw.  Confirming stale UI must converge
                # to a visible terminal state, never leave a poison pending row.
                if source_goal is None or current is None or source_goal != current:
                    payload["superseded_reason"] = "source_goal_not_current"
                    conn.execute(
                        "UPDATE directive SET status='superseded',payload_json=? "
                        "WHERE id=? AND status='pending'",
                        (json.dumps(payload, ensure_ascii=False), directive_id))
                    return
                newer = conn.execute(
                    "SELECT d.id FROM directive d JOIN interaction_message m "
                    "ON m.id=d.source_interaction_message_id "
                    "WHERE d.id>? AND d.kind='goal_amend' AND d.status='pending' "
                    "AND json_extract(d.payload_json,'$.confirmed')=1 "
                    "AND json_type(d.payload_json,'$.parse_error') IS NULL "
                    "AND json_type(d.payload_json,'$.new_goal_text')='text' "
                    "AND trim(json_extract(d.payload_json,'$.new_goal_text'))<>'' "
                    "AND json_type(d.payload_json,'$.rationale_md')='text' "
                    "AND trim(json_extract(d.payload_json,'$.rationale_md'))<>'' "
                    "AND (json_type(d.payload_json,'$.predicate_json') IS NULL "
                    "OR json_type(d.payload_json,'$.predicate_json')='object') "
                    "AND m.goal_id=? AND m.goal_ver=? ORDER BY d.id DESC LIMIT 1",
                    (directive_id, source_goal[0], source_goal[1])).fetchone()
                if newer is not None:
                    payload["superseded_reason"] = f"newer_confirmed_goal_amend:d{newer[0]}"
                    conn.execute(
                        "UPDATE directive SET status='superseded',payload_json=? "
                        "WHERE id=? AND status='pending'",
                        (json.dumps(payload, ensure_ascii=False), directive_id))
                    return
            n = conn.execute("UPDATE directive SET payload_json=? WHERE id=? AND status='pending'",
                             (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if n != 1:        # 兜底同 consume（同事务已校验，理论不可达）
                raise RuntimeError(f"directive {directive_id} 确认竞态：更新失败")
            if payload.get("mode") == "pin":
                # Only a *confirmed* newer hard pin is an effective ordering
                # intent.  Inbound-but-unconfirmed text has no state effect and
                # therefore cannot erase an older confirmed command.
                conn.execute(
                    "UPDATE directive SET status='superseded' WHERE id<? "
                    "AND kind='reprioritize' AND hardness='hard' AND status='pending' "
                    "AND json_extract(payload_json,'$.mode')='pin'",
                    (directive_id,))
            if row[4] == "goal_amend":
                # Only the latest *confirmed* revision is effective.  A newer
                # unconfirmed draft has zero state effect and is retained.
                conn.execute(
                    "UPDATE directive SET status='superseded' WHERE id<? "
                    "AND kind='goal_amend' AND status='pending' "
                    "AND source_interaction_message_id IN ("
                    "SELECT id FROM interaction_message WHERE goal_id=? AND goal_ver=?)",
                    (directive_id, source_goal[0], source_goal[1]))

    def supersede_stale_goal_amends(self) -> None:
        """Terminalize confirmed amendments whose source goal is no longer current.

        This is called at every precheck, including the no-cycle boundary, so a
        crash/restart or a previously applied newer revision cannot leave an
        unreachable pending directive in the control plane.
        """
        stale_where = (
            "d.kind='goal_amend' AND d.status='pending' "
            "AND json_extract(d.payload_json,'$.confirmed')=1 "
            "AND (m.goal_id IS NULL OR m.goal_ver IS NULL OR m.goal_ver IS NOT "
            "(SELECT max(g.version) FROM goal g WHERE g.id=m.goal_id))")
        if self.daemon.query_one(
                "SELECT 1 FROM directive d LEFT JOIN interaction_message m "
                "ON m.id=d.source_interaction_message_id "
                f"WHERE {stale_where} LIMIT 1") is None:
            return                              # common path: no write transaction / lock
        with self.daemon.transaction() as conn:
            rows = conn.execute(
                "SELECT d.id,d.payload_json FROM directive d "
                "LEFT JOIN interaction_message m ON m.id=d.source_interaction_message_id "
                f"WHERE {stale_where} ORDER BY d.id").fetchall()
            for did, payload_raw in rows:
                payload = json.loads(payload_raw)
                payload["superseded_reason"] = "source_goal_not_current"
                conn.execute(
                    "UPDATE directive SET status='superseded',payload_json=? "
                    "WHERE id=? AND status='pending'",
                    (json.dumps(payload, ensure_ascii=False), did))

    def reject_directive(self, *, directive_id: int, reason: str, reject_message_id: Optional[int] = None,
                         by_decision: bool = False, cycle_id: Optional[str] = None) -> None:
        """确认不过（用户否掉润色稿）→ rejected 不消费；by_decision=True = **软指令系统有理由不从**
        （§4.6.4：须 DECISION 写明理由——此路记账；用户否决路不写 decision[P1：非研究决策]）。
        读校验与更新同事务（防 TOCTOU，同 consume）。"""
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,hardness,payload_json,source_interaction_message_id FROM directive WHERE id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可拒")
            if by_decision:
                if reject_message_id is not None:
                    raise ValueError("系统不从路径不得冒充用户拒绝 provenance")
                if row[1] != "soft":
                    raise ValueError("系统不从仅限软指令（硬指令绕过权衡直接生效，§4.6.4）")
                conn.execute("INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                             "VALUES (?,?,'orchestrator','soft_directive_declined',?)",
                             (_cnum(cycle_id) if cycle_id else None, directive_id,
                              json.dumps({"reason": reason}, ensure_ascii=False)))
            else:
                if reject_message_id is None:
                    raise ValueError("用户拒绝须提供 reject_message_id provenance")
                self._validate_action_provenance(
                    conn, directive_id=directive_id, source_message_id=row[3],
                    action_message_id=reject_message_id, action="reject", reason=reason)
            # 用户拒绝路不写 decision（P1），但理由和控件消息 id 入 payload 供审计/幂等重放。
            payload = json.loads(row[2])
            payload["rejection_reason"] = reason
            if reject_message_id is not None:
                payload["rejection_message_id"] = reject_message_id
            n = conn.execute(
                "UPDATE directive SET status='rejected', payload_json=? WHERE id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if n != 1:        # 兜底同 consume（同事务已校验，理论不可达）
                raise RuntimeError(f"directive {directive_id} 拒绝竞态：更新失败")

    def reject_unapplicable_directive(self, *, directive_id: int, reason: str,
                                      cycle_id: Optional[str] = None) -> None:
        """Terminalize a confirmed/due directive whose requested effect is unavailable.

        This is not a user rejection and is valid for hard as well as soft
        directives.  The explicit DECISION prevents an unsupported command
        from being advertised as ``consumed`` while also avoiding a permanent
        poison-pill at every precheck.
        """
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT status,payload_json FROM directive WHERE id=?", (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            if row[0] == "rejected":
                return
            if row[0] != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{row[0]}），不可终态拒绝")
            payload = json.loads(row[1])
            if not isinstance(payload, dict):
                raise RuntimeError(f"directive {directive_id} payload 不是 JSON object")
            payload["rejection_reason"] = str(reason)[:2_000]
            payload["rejection_kind"] = "application_unavailable"
            dec = conn.execute(
                "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                "VALUES (?,?,'orchestrator','directive_application_rejected',?)",
                (_cnum(cycle_id) if cycle_id else None, directive_id,
                 json.dumps({"reason": str(reason)[:2_000]}, ensure_ascii=False))).lastrowid
            changed = conn.execute(
                "UPDATE directive SET status='rejected',payload_json=? "
                "WHERE id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), directive_id)).rowcount
            if changed != 1:
                raise RuntimeError(f"directive {directive_id} 终态拒绝竞态：更新失败")
            if dec is None:
                raise RuntimeError(f"directive {directive_id} 终态拒绝 DECISION 未落库")

    # ---------------------------------------------------------------- 消费 --
    def pending_directives(self, consume_at: str) -> list:
        """指定时机**当下可消费**的 directive id 序列（创建序）：软指令或已确认硬指令——未确认硬指令
        不进队（consume 会拒，进队只会让调度方稳定撞拒；其提醒走通知层，CP6.3）。Advancer 前置检查
        （immediate/stage_boundary）与 reasoning 轮始（reasoning_start）按此取。"""
        return [r[0] for r in self.daemon.query(
            "SELECT id FROM directive WHERE status='pending' AND consume_at=? "
            "AND (hardness='soft' OR json_extract(payload_json,'$.confirmed')) ORDER BY id", (consume_at,))]

    def has_blocking_pause(self) -> bool:
        """§4.4.1 前置检查：**最近一次被消费的 pause/resume 是 pause** → 暂停态，不发起新研究 Runner 调用。
        阻断从 pause 消费起持续到 resume 消费止；pending（含已确认）不阻断——调用方须先消费到期
        directive 再查阻断（前置检查顺序）。消费序 = consumed_decision_id（decision 自增，全局单调）。"""
        r = self.daemon.query_one("SELECT kind FROM directive WHERE status='consumed' AND kind IN ('pause','resume') "
                                  "ORDER BY consumed_decision_id DESC LIMIT 1")
        return bool(r) and r[0] == "pause"

    def consume_directive(self, *, directive_id: int, cycle_id: Optional[str] = None,
                          state=None) -> Dict[str, Any]:
        """按时机消费——**单事务内**读校验+效果+DECISION(actor='human', directive_id 回指)+条件更新
        consumed（读写同事务，WriteDaemon 单写串行 → 无 TOCTOU 窗口；条件更新 rowcount 兜底）。
        拒：非 pending；**硬指令未确认**（§7.1 M5「未确认硬指令 consume_directive 拒」）。
        cycle_id 可空（Advancer 前置检查在开轮前消费 immediate 指令时无在途轮；DECISION.cycle_id/
        consumed_cycle 本可空）。返回 {kind, effect} 供通知层（applied 事件，CP6.3）。"""
        ci = _cnum(cycle_id) if cycle_id else None
        with self.daemon.transaction() as conn:
            row = conn.execute(
                "SELECT d.kind,d.hardness,d.status,d.consume_at,d.payload_json,"
                "m.goal_id,m.goal_ver,d.source_interaction_message_id "
                "FROM directive d LEFT JOIN interaction_message m "
                "ON m.id=d.source_interaction_message_id WHERE d.id=?",
                               (directive_id,)).fetchone()
            if row is None:
                raise ValueError(f"directive 不存在: {directive_id}")
            (kind, hardness, status, consume_at, payload_raw, source_goal_id,
             source_goal_ver, source_message_id) = row
            if status != "pending":
                raise ValueError(f"directive {directive_id} 非 pending（{status}），不可消费")
            payload = json.loads(payload_raw)
            if hardness == "hard" and not payload.get("confirmed"):
                raise ValueError(f"硬指令 {directive_id}（{kind}）未经回显确认，不可消费（§4.6.2 润色确认硬门）")
            # Only reasoning_start directives are compiled into this cycle's reasoning pack.  Operational
            # immediate controls (especially resume/abort) must remain available even after the prompt budget
            # is full, otherwise a note flood could make a paused cycle impossible to resume.
            if ci is not None and consume_at == "reasoning_start":
                consumed_for_cycle = conn.execute(
                    "SELECT count(*) FROM directive WHERE status='consumed' AND consumed_cycle=? "
                    "AND consume_at='reasoning_start'",
                    (ci,)).fetchone()[0]
                if consumed_for_cycle >= MAX_REASONING_DIRECTIVES_PER_CYCLE:
                    raise DirectiveApplicationError(
                        f"cycle c{ci} 人类 directive 已达上下文安全上限 "
                        f"{MAX_REASONING_DIRECTIVES_PER_CYCLE}；本条未执行")
            effect: Dict[str, Any] = {"kind": kind}
            dec = None            # prune_branch 复用其 prune 决策为消费决策（一次消费恰一条人类决策）
            budget_stop = None
            if kind == "set_budget":
                if self.policy is None or not isinstance(self.policy.get("budget"), dict):
                    raise DirectiveApplicationError("set_budget 未装配启动 policy，不能构造耐久有效预算")
                if payload.get("parse_error"):
                    raise DirectiveApplicationError(f"set_budget 参数未解析: {payload['parse_error']}")
                if has_active_global_stop(conn):
                    raise DirectiveApplicationError("系统已有 durable global_stop；set_budget 不具备撤销停机语义")
                try:
                    previous = effective_budget_config(conn, self.policy["budget"])
                    budget = apply_budget_patch(previous, payload.get("budget_patch"))
                except (TypeError, ValueError, RuntimeError) as error:
                    raise DirectiveApplicationError(str(error)) from error
                spent = float(conn.execute("SELECT COALESCE(SUM(money),0) FROM ledger").fetchone()[0])
                effect.update({"previous_budget": previous, "budget": budget,
                               "global_spent": spent})
                if budget["session_max"] is not None and spent >= budget["session_max"]:
                    budget_stop = {"reason": "budget_exhausted", "spent": spent,
                                   "session_max": budget["session_max"],
                                   "trigger": "directive_set_budget"}
                    effect["global_stop"] = budget_stop
            elif kind == "reprioritize":
                if payload.get("parse_error"):
                    raise DirectiveApplicationError(f"reprioritize 参数未解析: {payload['parse_error']}")
                mode = payload.get("mode")
                qref = payload.get("question_id")
                if mode not in ("pin", "boost", "suppress") or not qref:
                    raise DirectiveApplicationError("reprioritize 缺合法 mode/question_id")
                try:
                    qi = int(str(qref)[1:]) if str(qref).lower().startswith("q") else int(qref)
                except (TypeError, ValueError, OverflowError):
                    raise DirectiveApplicationError(f"reprioritize question_id 非法: {qref!r}") from None
                if qi <= 0 or qi > (1 << 63) - 1:
                    raise DirectiveApplicationError(f"reprioritize question_id 超出 SQLite 正整数范围: {qref!r}")
                qrow = conn.execute(
                    "SELECT status FROM question WHERE id=? AND NOT EXISTS ("
                    "SELECT 1 FROM question_dep WHERE question_id=? AND status='pending')",
                    (qi, qi)).fetchone()
                is_current_active = bool(qrow and qrow[0] == "active" and ci is not None
                                         and conn.execute(
                                             "SELECT 1 FROM cycle WHERE id=? AND active_question_id=? "
                                             "AND status NOT IN ('done','failed','aborted')",
                                             (ci, qi)).fetchone())
                if qrow is None or (qrow[0] not in ("open", "inconclusive")
                                    and not is_current_active):
                    raise DirectiveApplicationError(
                        "reprioritize 目标须为无 pending 依赖的 open/inconclusive 问题，"
                        f"或当前 cycle 的 active Qn: q{qi}")
                effect.update({"mode": mode, "question_id": f"q{qi}",
                               "applies_to_reasoning_cycle": cycle_id})
                if mode == "pin":
                    if hardness != "hard":
                        raise DirectiveApplicationError("reprioritize pin 必须是 hard directive")
                else:
                    if hardness != "soft":
                        raise DirectiveApplicationError(f"reprioritize {mode} 必须是 soft directive")
                    adjust = payload.get("adjust")
                    if (isinstance(adjust, bool) or not isinstance(adjust, (int, float))
                            or not math.isfinite(float(adjust)) or adjust == 0
                            or (mode == "boost") != (adjust > 0)):
                        raise DirectiveApplicationError(f"reprioritize {mode} 缺合法有符号 adjust")
                    effect["adjust"] = float(adjust)
            elif kind == "goal_amend":
                if ci is None:
                    raise DirectiveApplicationError("goal_amend 必须绑定专用 reasoning cycle")
                route_row = conn.execute(
                    "SELECT route FROM cycle WHERE id=? AND status NOT IN ('done','failed','aborted')",
                    (ci,)).fetchone()
                if route_row is None or route_row[0] != "goal_amend":
                    raise DirectiveApplicationError("goal_amend 只能在 route='goal_amend' 的在途轮消费")
                if payload.get("parse_error"):
                    raise DirectiveApplicationError(
                        f"goal_amend 参数未解析: {payload['parse_error']}")
                new_goal_text = payload.get("new_goal_text")
                rationale = payload.get("rationale_md")
                if not isinstance(new_goal_text, str) or not new_goal_text.strip():
                    raise DirectiveApplicationError("goal_amend 缺非空 new_goal_text")
                if not isinstance(rationale, str) or not rationale.strip():
                    raise DirectiveApplicationError("goal_amend 缺非空 rationale_md")
                current = conn.execute(
                    "SELECT id,version,predicate_json FROM goal WHERE id=? "
                    "ORDER BY version DESC LIMIT 1", (source_goal_id,)).fetchone()
                if current is None or (source_goal_id, source_goal_ver) != current[:2]:
                    raise DirectiveApplicationError(
                        "goal_amend source goal 已过期；必须基于当前目标版本重新提交并确认")
                if conn.execute(
                        "SELECT 1 FROM directive WHERE kind='goal_amend' AND status='consumed' "
                        "AND consumed_cycle=? LIMIT 1", (ci,)).fetchone() is not None:
                    raise DirectiveApplicationError(f"cycle c{ci} 已消费另一条 goal_amend")
                routed = conn.execute(
                    "SELECT directive_id FROM decision WHERE cycle_id=? "
                    "AND actor='orchestrator' AND type IN ('goal_amend_routed','goal_amend_rebound') "
                    "ORDER BY id DESC LIMIT 1", (ci,)).fetchone()
                if routed is not None and routed[0] != directive_id:
                    conn.execute(
                        "INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                        "VALUES (?,?,'orchestrator','goal_amend_rebound',?)",
                        (ci, directive_id, json.dumps({
                            "previous_directive_id": routed[0],
                            "reason": "newer_confirmed_amend_before_reasoning_start",
                        }, ensure_ascii=False)))
                predicate = payload.get("predicate_json")
                if predicate is None:
                    try:
                        predicate = json.loads(current[2])
                    except json.JSONDecodeError as error:
                        raise DirectiveApplicationError("当前 goal.predicate_json 损坏") from error
                if not isinstance(predicate, dict):
                    raise DirectiveApplicationError("goal_amend.predicate_json 须为 object")
                effect.update({
                    "new_goal_text": new_goal_text.strip(),
                    "predicate_json": predicate,
                    "rationale_md": rationale.strip(),
                    "source_goal_ver": current[1],
                    "target_goal_ver": current[1] + 1,
                    "applies_to_reasoning_cycle": cycle_id,
                })
            elif kind == "inject_question":
                # inject_question 只是 reasoning 输入，不是旁路建题权限。真正的 question
                # 必须由同轮 reasoning 产 tree_ops，再经 StateStore 的 predicate/admission
                # 与 tree_guard 统一落库。这里只冻结人类请求及其 provenance。
                if ci is None:
                    raise DirectiveApplicationError(
                        "inject_question 必须绑定将消费它的 reasoning cycle")
                # goal_id=1 + MAX(version) = 全库单目标约定（statestore/advancer 同口径）；多目标=系统级改造
                current_goal = conn.execute(
                    "SELECT id,version FROM goal WHERE id=1 ORDER BY version DESC LIMIT 1").fetchone()
                if current_goal is None:
                    raise DirectiveApplicationError("inject_question 时当前 goal 不存在")
                if (source_goal_id, source_goal_ver) != tuple(current_goal):
                    raise DirectiveApplicationError(
                        "inject_question source goal 已过期；请基于当前目标重新提交")
                repo = payload.get("human_named_repo")
                if repo is not None:
                    if hardness != "hard" or not payload.get("confirmed"):
                        raise DirectiveApplicationError(
                            "human_named repo 必须经 hard directive 明确确认")
                    if source_message_id is None:
                        raise DirectiveApplicationError(
                            "human_named repo 来源须绑定当前 goal 的耐久 interaction message")
                question_text = payload.get("question_text", payload.get("polished", ""))
                if not isinstance(question_text, str) or not question_text.strip():
                    raise DirectiveApplicationError("inject_question 缺非空 question_text")
                parent_ref = payload.get("parent_question_id")
                parent_id = None
                if parent_ref is not None:
                    try:
                        parent_id = (_qnum(parent_ref.lower())
                                     if isinstance(parent_ref, str) else None)
                    except ValueError:
                        raise DirectiveApplicationError(
                            "inject_question parent_question_id 非法") from None
                    if parent_id is None:
                        raise DirectiveApplicationError(
                            "inject_question parent_question_id 须为 q<正整数>")
                    parent_row = conn.execute(
                        "SELECT 1 FROM question WHERE id=? AND goal_id=? AND goal_ver=?",
                        (parent_id, current_goal[0], current_goal[1])).fetchone()
                    if parent_id <= 0 or parent_row is None:
                        raise DirectiveApplicationError(
                            "inject_question parent 不属于当前 goal lineage")
                request = {
                    "protocol": _QUESTION_REQUEST_PROTOCOL,
                    # StateStore question admission uses the same whitespace
                    # canonical form.  Freezing it here lets request_ref bind
                    # the eventual row by exact text instead of a fuzzy match.
                    "requested_text": " ".join(question_text.split()),
                    "parent_question_id": (
                        f"q{parent_id}" if parent_id is not None else None),
                    "suggested_kind": (
                        "import_reference" if repo is not None else "followup"),
                    "request_ref": f"db:directive:{directive_id}",
                    # reasoning 必须自行给出 evidence_closure_v1 predicate_json；
                    # 不把控制台文本伪装成已准入的 tree op。
                    "requires_reasoning_predicate": True,
                }
                if repo is not None:
                    request["human_named_repo"] = dict(repo)
                    request["need_summary"] = payload.get("need_summary")
                effect.update({
                    "applies_to_reasoning_cycle": cycle_id,
                    "reasoning_question_request": request,
                })
            elif kind == "prune_branch":
                qref = payload.get("question_id")
                if not qref:
                    raise DirectiveApplicationError("prune_branch 需 payload.question_id（润色/确认阶段补齐）")
                try:
                    qi = int(str(qref)[1:]) if str(qref).startswith("q") else int(qref)
                except (TypeError, ValueError):
                    raise DirectiveApplicationError(f"prune_branch question_id 非法: {qref!r}") from None
                qs = conn.execute("SELECT status FROM question WHERE id=?", (qi,)).fetchone()
                if qs is None or qs[0] not in ("open", "inconclusive"):
                    raise DirectiveApplicationError(
                        f"prune_branch 只允许 open/inconclusive 目标: {qref}"
                        f"（{qs[0] if qs else '缺失'}）")
                effect["question_id"] = f"q{qi}"
                dec = conn.execute("INSERT INTO decision(cycle_id,question_id,directive_id,actor,type,payload_json) "
                                   "VALUES (?,?,?,'human','prune_branch',?)",
                                   (ci, qi, directive_id,
                                    json.dumps({"effect": effect, "polished": payload.get("polished")},
                                               ensure_ascii=False))).lastrowid
                conn.execute("UPDATE question SET status='dead_end' WHERE id=?", (qi,))   # trg_q_deadend 要求 decision 先行
                conn.execute(
                    "UPDATE question_dep SET status='blocked' WHERE dep_type='question' "
                    "AND depends_on_question_id=? AND status='pending'", (qi,))
            elif kind == "abort_cycle":
                # 单轮在途约定（Advancer 串行推进）：非终态轮至多一个，即"本轮"；多轮并发=系统级改造
                cur = conn.execute("SELECT id,active_question_id FROM cycle "
                                   "WHERE status NOT IN ('done','failed','aborted') "
                                   "ORDER BY id LIMIT 1").fetchone()
                if cur:
                    active_question_id = cur[1]
                    if active_question_id is not None:
                        released = conn.execute(
                            "UPDATE question SET status='open' WHERE id=? AND status='active'",
                            (active_question_id,)).rowcount
                        if released != 1:
                            raise DirectiveApplicationError(
                                f"cycle c{cur[0]} active_question_id=q{active_question_id} "
                                "未指向 active 问题；abort 未执行，需先修复权威状态漂移")
                        effect["released_question"] = f"q{active_question_id}"
                    conn.execute(
                        "UPDATE cycle SET status='aborted', active_question_id=NULL, "
                        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (cur[0],))
                    effect["aborted_cycle"] = f"c{cur[0]}"
            # pause：消费即进入暂停态（阻断语义在 has_blocking_pause，按消费序判定），无额外行内效果
            elif kind == "resume":
                # 队列清理：把**早于本 resume 的** pending pause 置 superseded（用户指令有序：晚到的 pause
                # 是新诉求、保留到其时机再生效）。解除阻断本身由消费序体现（本 resume 成为最近消费）。
                for r in conn.execute("SELECT id FROM directive WHERE status='pending' AND kind='pause' AND id<?",
                                      (directive_id,)).fetchall():
                    conn.execute("UPDATE directive SET status='superseded' WHERE id=?", (r[0],))
                    effect.setdefault("superseded_pause", []).append(r[0])
            elif kind == "note":
                # compiler 按 consumed_cycle 把该注解注入同一 reasoning ContextPack。
                effect["published_to_reasoning_cycle"] = cycle_id
            if dec is None:
                dec = conn.execute("INSERT INTO decision(cycle_id,directive_id,actor,type,payload_json) "
                                   "VALUES (?,?,'human',?,?)",
                                   (ci, directive_id, f"directive_{kind}",
                                    json.dumps({"effect": effect, "polished": payload.get("polished")},
                                               ensure_ascii=False))).lastrowid
            if budget_stop is not None:
                conn.execute(
                    "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                    "VALUES (?,'orchestrator','global_stop',?)",
                    (ci, json.dumps(budget_stop, ensure_ascii=False)))
            claimed = conn.execute("UPDATE directive SET status='consumed', consumed_cycle=?, consumed_decision_id=? "
                                   "WHERE id=? AND status='pending'", (ci, dec, directive_id)).rowcount
            if claimed != 1:      # 理论不可达（同事务已校验+单写串行）；兜底防未来改动引入窗口
                raise RuntimeError(f"directive {directive_id} 消费竞态：claim 失败")
        return effect
