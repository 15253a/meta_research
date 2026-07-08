"""Mediator —— query 只读应答链：ro 连接 + grounding 校验 + 模板应答 + 中介重建（§4.6.2/4.6.5；M5 CP6.2）。

**只读边界（P1 铁律）**：应答器全链**碰不到写路径**——DB 访问只经 open_responder_read_conn（`mode=ro`
URI 物理只读 + authorizer 对一切写类动作 DENY，双保险；范式同 gate_sqlite.open_gate_read_conn）；
回复落库（interaction_reply，append-only、不写 decision）是**中介**的事，走 InteractionIngest.ack。

**输入纪律（§4.6.2）**：应答器不收 raw 对话，只收**已消毒查询 + 发布快照 status_card**（非半完成态——
读 SqliteStatusPublisher 原子发布的文件，不读在途 DB 状态）。

**grounding 校验（§4.6.5）**：回复入库前过机械校验——①不得声称状态已变（应答器只读，任何"已暂停/已
修改"都是幻觉）；②不得引用日志作证（execution_log 不在人机可见集）；③不得自产 directive；④引用的
q/c 实体 id 必须在卡内。不过 → **模板回退**（policy.interaction.responder_fallback）。本版应答器
本身即模板渲染（卡内字段，构造性接地）；真 Codex 应答器（responder_kind='codex'+runner_call
phase=interaction_query）= M6，届时 grounding 是其唯一入库闸门。

**中介重建（§4.6.2）**：唯一持久态在 interaction_* + 发布卡文件；Mediator 进程态可随时丢弃——
rebuild() 从「最近发布卡 + 同 connector 最近 N 条消毒入/出站」重建（mediator_rebuild_last_n），
重建前后对同一查询的回答**逐字节一致**（确定性应答 + 持久输入 ⇒ 可证）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .console import sanitize
from .interaction import InteractionIngest
from .writedaemon import WriteDaemon

# 写类 authorizer 动作码全集（sqlite3 模块常量名）：任何一个都 DENY —— mode=ro 已物理只读，
# authorizer 再拒一层（语句 prepare 期干净报错；且 **temp schema 在 mode=ro 下仍可写**，
# CREATE_TEMP_*/CREATE_VTABLE 必须靠 authorizer 拒——外审 BLOCKER：VTABLE 可落 temp）。
# 故意放行：TRANSACTION/SAVEPOINT（发布器要 BEGIN 钉读快照）、FUNCTION、READ/SELECT。
_WRITE_ACTIONS = frozenset(
    getattr(sqlite3, name) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE",
        "SQLITE_DROP_TABLE", "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX",
        "SQLITE_ANALYZE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_PRAGMA",
    ) if hasattr(sqlite3, name)
)


def responder_authorizer(action, arg1, arg2, arg3, arg4):
    """应答器连接 authorizer：一切写/DDL/PRAGMA 动作 DENY，读放行（§7.1 M5 只读边界负例的执法点）。"""
    return sqlite3.SQLITE_DENY if action in _WRITE_ACTIONS else sqlite3.SQLITE_OK


def open_responder_read_conn(path: str) -> sqlite3.Connection:
    """应答器/发布器专用只读连接：mode=ro（物理只读，不可翻回可写）+ 全写拒 authorizer（双保险）。
    isolation_level=None = 事务由使用方显式掌控（build_status_card 契约：自己 BEGIN…COMMIT 钉读快照），
    杜绝 py 隐式事务。须指向已建**文件库**（:memory: 每连接独立库，测试须用文件库，同 gate 约定）。"""
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    conn.isolation_level = None
    conn.set_authorizer(responder_authorizer)
    return conn


# ---------------------------------------------------------------- grounding --
# 状态已变声称 / 日志引证 / 自产 directive 的特征词（保守面：误杀退模板无害，漏放才是事故）
_STATE_CHANGE_CLAIMS = ("已暂停", "已恢复", "已中止", "已修改", "已调整", "已注入", "已剪枝", "已更新",
                        "帮你改", "帮你停", "i have paused", "i paused", "i changed", "i updated", "i aborted")
_LOG_CITATIONS = ("execution_log", "日志显示", "日志里", "according to the log", "from the log")
_DIRECTIVE_CLAIMS = ("已创建指令", "已下发指令", "created a directive", "issued a directive")


# 实体 token：CJK 是 \w、无空格分词 → 不能用 \b（"轮c7" 无边界，漏检；教训同 console._hit），
# 改用 ASCII-字母数字环视——CJK 紧邻照样命中，纯拉丁长 token（abc7def）不误切。
# 大小写不敏感（外审 NIT）：用户可读写法 "Q99"/"C7" 不得绕过卡外实体检查；比对前统一小写（卡内 id 全小写）
_ENTITY_RE = re.compile(r"(?<![0-9A-Za-z])[qc]\d+(?![0-9A-Za-z])", re.IGNORECASE)


def grounding_check(answer: str, card: Dict[str, Any]) -> Optional[str]:
    """机械 grounding：返回 None=通过；str=拒因。①状态已变声称②日志引证③自产 directive 声称
    ④卡外实体引用（q\\d+/c\\d+ 必须是卡内出现过的实体）。"""
    low = answer.lower()
    for w in _STATE_CHANGE_CLAIMS:
        if w in low:
            return f"声称状态已变: {w!r}"
    for w in _LOG_CITATIONS:
        if w in low:
            return f"引用日志作证: {w!r}"
    for w in _DIRECTIVE_CLAIMS:
        if w in low:
            return f"声称自产 directive: {w!r}"
    card_json = json.dumps(card, ensure_ascii=False, sort_keys=True)
    card_entities = {e.lower() for e in _ENTITY_RE.findall(card_json)}   # 实体对实体比对（防 "c1"⊂"c12" 子串假过）
    for ent in {e.lower() for e in _ENTITY_RE.findall(answer)}:
        if ent not in card_entities:
            return f"引用卡外实体: {ent}"
    return None


# ---------------------------------------------------------------- responder --
class TemplateResponder:
    """确定性模板应答器（M5）：只渲染卡内字段 → 构造性接地。真 Codex 应答器=M6（届时同样过 grounding）。
    满足 interfaces.Responder Protocol：answer(sanitized_query, status_card[canonical JSON str]) -> str。
    kind = interaction_reply.responder_kind 值：真 Codex 应答器须自带 kind='codex' + runner_call 绑定（M6）。"""

    kind = "template"

    def answer(self, sanitized_query: str, status_card: str) -> str:
        card = json.loads(status_card)
        q = card.get("active_question") or {}
        budget = card.get("budget") or {}
        counts = card.get("counts") or {}
        pfr = card.get("pending_file_request")
        lines = [
            f"[快照 {card.get('snapshot_cycle')}] 轮状态 {card.get('cycle_status')} / "
            f"路线 {card.get('route') or '未定'}。",
            f"当前问题：{q.get('id')} {q.get('text', '')[:120]}（{q.get('status')}）" if q else "当前无活跃问题。",
            f"预算：本轮上限 {budget.get('B_t')}，本轮已花 {budget.get('cycle_spent')}。",
            f"问题面：open {counts.get('open', 0)} / inconclusive {counts.get('inconclusive', 0)}。",
        ]
        ld = (card.get("selection") or {}).get("latest_decision")
        if ld:
            lines.append(f"本轮最近决策：#{ld.get('id')} {ld.get('actor')}/{ld.get('type')}。")
        if pfr:
            lines.append(f"⚠ 文件请求 #{pfr.get('request_id')} 等待中（{pfr.get('item_count')} 项，"
                         f"自 {pfr.get('created_at')}）——系统暂停新研究执行。")
        return "\n".join(lines)


_TEMPLATE_FALLBACK = ("[快照 {sc}] 自动摘要：轮状态 {st}／路线 {rt}；open {op}。"
                      "（应答器输出未过接地校验，已退安全模板）")


def render_fallback(card: Dict[str, Any]) -> str:
    """grounding 不过时的安全模板（只含卡内四个标量，机械不可幻觉；None 渲成'未定'不裸露）。"""
    return _TEMPLATE_FALLBACK.format(sc=card.get("snapshot_cycle") or "未定",
                                     st=card.get("cycle_status") or "未定",
                                     rt=card.get("route") or "未定",
                                     op=(card.get("counts") or {}).get("open", 0))


# ----------------------------------------------------------------- mediator --
class Mediator:
    """人机中介：入站查询 → 消毒 → 应答（发布卡快照）→ grounding → 回复入库（append-only，不写 decision）。
    进程态可弃：rebuild() 从持久层重建对话上下文（重建前后同查询回答一致——确定性应答保证）。"""

    def __init__(self, daemon: WriteDaemon, card_path: str, responder=None, rebuild_last_n: int = 20):
        self.daemon = daemon
        self.card_path = Path(card_path)          # SqliteStatusPublisher 原子发布的 latest 卡文件
        self.responder = responder or TemplateResponder()
        self.rebuild_last_n = rebuild_last_n
        self.ingest = InteractionIngest(daemon)

    def latest_card(self) -> Dict[str, Any]:
        """读最近发布快照（非半完成态：发布是 tmp→rename 原子替换，读到的必是完整卡）。"""
        if not self.card_path.exists():
            raise FileNotFoundError(f"status_card 尚未发布: {self.card_path}（advance 阶段边界发布后可答）")
        return json.loads(self.card_path.read_text(encoding="utf-8"))

    def handle_query(self, *, message_id: int) -> Dict[str, Any]:
        """query 意图的应答全链。返回 {reply_id, reply_text, grounded, snapshot_cycle}。
        不改研究状态：唯一写 = interaction_reply（responder_kind='template'——本版应答器即模板渲染；
        codex kind 须绑 runner_call phase=interaction_query，M6）。
        **查询文本从持久层按 message_id 取**（外审 SHOULD：不信调用方复述——回复绑定 message_id，
        输入也必须是该 message 的 raw，否则「持久输入可重建」的审计链断）。"""
        row = self.daemon.query_one("SELECT raw_text FROM interaction_message WHERE id=?", (message_id,))
        if row is None:
            raise ValueError(f"interaction_message 不存在: {message_id}")
        card = self.latest_card()
        # M6 缝显式化（内审 SHOULD + 外审 SHOULD：缺 kind 也 fail loud，不静默当 template）：本方法只会以
        # responder_kind='template' 入库（经 ack）；codex kind 须绑 runner_call phase=interaction_query
        # （DDL trg_ireply_codex_phase），届时另开入库路径
        if getattr(self.responder, "kind", None) != "template":
            raise NotImplementedError("应答器须显式声明 kind='template'；非 template 入库需 runner_call 绑定（M6）")
        card_json = json.dumps(card, ensure_ascii=False, sort_keys=True)
        answer = self.responder.answer(sanitize(row[0]), card_json)
        reason = grounding_check(answer, card)
        if reason is not None:
            answer = render_fallback(card)        # policy.responder_fallback：不过 1 次即退模板
        rid = self.ingest.ack(message_id=message_id, reply_text=answer,
                              snapshot_cycle=card.get("snapshot_cycle"))
        return {"reply_id": rid, "reply_text": answer, "grounded": reason is None,
                "snapshot_cycle": card.get("snapshot_cycle"), "fallback_reason": reason}

    def rebuild(self, connector: str) -> Dict[str, Any]:
        """从持久层重建中介上下文（§4.6.2：唯一持久态在 interaction_* + 发布卡）：最近发布卡 +
        同 connector 最近 N 条消毒入站（各带**最新一条**回复）。供新中介实例/重启后继续对话。
        相关子查询取回复（外审 SHOULD：LEFT JOIN 会因 ACK+应答双回复扇出——LIMIT 误按 join 行数截、
        reply 取哪条未定义；窗口 N 必须数 message、回复取 r.id 最大）。"""
        rows = self.daemon.query(
            "SELECT m.id, m.raw_text, (SELECT r.reply_text FROM interaction_reply r "
            "  WHERE r.message_id = m.id ORDER BY r.id DESC LIMIT 1) "
            "FROM interaction_message m WHERE m.connector=? ORDER BY m.id DESC LIMIT ?",
            (connector, self.rebuild_last_n))
        history = [{"message_id": mid, "text": sanitize(raw or ""), "reply": (rep or None)}
                   for mid, raw, rep in reversed(rows)]
        return {"card": self.latest_card(), "history": history}
