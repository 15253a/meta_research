"""notify —— 通知矩阵 outbox + 文件请求全流水 + 全局等待前置检查（§4.6.6/§4.6.8/§4.4.1；M5 CP6.3）。

**outbox = 实现层文件队列，不建表**（核心 DDL 36 表冻结；§4.6.2 heartbeat/outbox 明示非核心 DDL）：
`outbox.jsonl` 追加事件（一行一 JSON）+ `delivered.log` 投递标记。**幂等两层**：emit 按 event_key
去重（重扫不重排队）；deliver 按 delivered 标记去重（重启不重发；send 成功与标记落盘之间崩溃 →
重发一次 = at-least-once，事件带 event_key 供接收端去重）。

**事件从 DB 状态扫描派生**（DirectiveNotifier/FileRequestNotifier），不在 console/interaction 内联发
——写路径保持单一职责，通知层随时可重扫补发（崩溃后 outbox 丢了也能从 DB 重建全部事件）。
event_key 确定性（directive:{id}:{state} / filereq:{id}:{event}）⇒ 重扫幂等。

**directive 逐态外显（§4.6.6 矩阵，7 态）**：received（源消息已入）/ classified（意图+kind）/
pending_confirmation（硬指令待回显确认，**展示润色稿**）/ pending_effect（已就绪待时机，示预计消费点）/
applied（consumed_cycle+效果摘要）/ rejected（附理由）/ superseded。
**文件请求 3 事件**：request_pending / reminder（每 remind_interval_h 一档；时间由调用方注入 now_ts
——本模块不调 wall-clock，保确定性可测）/ resolved（含 cancelled，附 resolution 摘要）。

**文件请求全流水（§4.6.8）**：create_checked = schema 校验（resource_request.schema.json：items 必带
attempted_paths/failure_reason——"能自己获取的不得请求"的自证）+ 创建拒绝三判据（enabled=false /
len(items)>max_items_per_request / 同 goal (pending+resolved)≥max_requests_per_goal）→ 落单。
resolve = uploads/<req_id>/<item_no>/ 逐文件 sha256 入账 → 复制并入 input/user_provided/<req_id>/ →
resolution_json + resolved_message_id **一次性迁终态**（DDL trg_ireq_identity_frozen 只许这一跳）。
cancel 同 provenance。用户文件 = 输入资产非证据（不进 evidence 链）。

**全局等待（§4.4.1 v1）**：make_advancer_precheck 装到 SqliteAdvancer.precheck——每格 advance 前：
①按时机消费到期 directive（immediate 恒到期；stage_boundary 每格即边界；reasoning_start 仅当下一格
将进 reasoning——结构判定见 _due_timings）；②查阻断：已消费未解除的 pause（console.has_blocking_pause）
或存在 pending 文件请求 → 返回拒因，Advancer 停止推进（**不发新研究 Runner 调用、不推阶段**；
query/通知照常——它们不走 Advancer）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .console import Console
from .writedaemon import WriteDaemon

# ------------------------------------------------------------------- outbox --


class Outbox:
    """文件队列：emit 幂等排队 → deliver_pending 经 Connector 投递（幂等标记）。
    单进程单写者假设（同目录只应有一个活 Outbox——与系统单写纪律一致）；进程内用 _seen 缓存免每次重读
    队列文件（O(n²)→O(n)）。"""

    def __init__(self, out_dir: str):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.dir / "outbox.jsonl"
        self.delivered_path = self.dir / "delivered.log"
        self._seen: Optional[set] = None      # 进程内 emit 去重缓存（首用时从文件懒加载）

    def _events(self) -> List[Dict[str, Any]]:
        """读全队列。**committed 判据 = 换行终止**（外审 BLOCKER：append 崩溃可能留下"完整 JSON 但无
        尾换行"——若按可解析性判会先算入 _seen、后被 emit 截修丢弃 → 事件永久丢失。故无尾换行的末段
        一律当未入队丢弃，与 emit 的截修口径一致；重扫会补）。换行终止段解析失败 = 中段损坏
        （非崩溃可造成，磁盘/人为改写），fail loud。"""
        if not self.queue_path.exists():
            return []
        data = self.queue_path.read_bytes()
        if not data:
            return []
        if not data.endswith(b"\n"):
            data = data[:data.rfind(b"\n") + 1]       # 未终止末段=未 committed（无换行则整文件丢弃）
        return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]

    def _queued_keys(self) -> set:
        if self._seen is None:
            self._seen = {e["event_key"] for e in self._events()}
        return self._seen

    def _delivered_keys(self) -> set:
        if not self.delivered_path.exists():
            return set()
        return {line.strip() for line in self.delivered_path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def emit(self, event_key: str, kind: str, payload: Dict[str, Any]) -> bool:
        """幂等排队：event_key 已在队列即跳过（返回 False）。追加写单行 JSON（行内自含 event_key，
        队列文件本身即持久事件序）。"""
        if event_key in self._queued_keys():
            return False
        # 尾行撕裂先修复（截掉半行）再追加：半行事件未完整落队=未 emit（重扫会补）；若不截、直接 append
        # 会把新 JSON 粘在半行上，且半行会随后续追加变成"中段坏行"触发 fail loud
        if self.queue_path.exists() and self.queue_path.stat().st_size > 0:
            data = self.queue_path.read_bytes()
            if not data.endswith(b"\n"):
                with self.queue_path.open("rb+") as f:
                    f.truncate(data.rfind(b"\n") + 1)     # 无任何换行 → 0 = 清空
        with self.queue_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event_key": event_key, "kind": kind, "payload": payload},
                               ensure_ascii=False) + "\n")
        self._seen.add(event_key)
        return True

    def deliver_pending(self, connector) -> List[str]:
        """把未投递事件按队列序经 connector.send 发出；成功一条标记一条（append delivered.log）。
        send 与标记之间崩溃 → 该条重发（at-least-once；接收端按 event_key 去重）。send 抛错则中断
        （后续事件保持未投递，下次续投——不吞错、不乱序跳发）。返回本次投出的 event_key 序列。"""
        done = self._delivered_keys()
        sent: List[str] = []
        with self.delivered_path.open("a", encoding="utf-8") as marker:
            for ev in self._events():
                if ev["event_key"] in done:
                    continue
                connector.send(ev)                      # 抛错即中断，本条未标记 → 下次重试
                marker.write(ev["event_key"] + "\n")
                marker.flush()
                sent.append(ev["event_key"])
        return sent


# ------------------------------------------------------- directive notifier --

def _directive_state_events(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单 directive 当前应存在的事件集（按其生命周期已走到的态；早态事件保留——outbox 幂等去重）。
    人机门控的中间态（pending_confirmation）只在扫描窗口内被外显：确认前必有扫描（人回显确认的时延
    远大于扫描节拍）；若 directive 在首次扫描前已走完生命周期，中间态事件不补发（对已生效指令追发
    "请确认"是误导）——consumed 分支例外补 pending_effect（它无行动含义、只是就绪记录）。"""
    d = row
    payload_base = {"directive_id": d["id"], "kind": d["kind"], "hardness": d["hardness"]}
    evs = [
        {"event_key": f"directive:{d['id']}:received", "kind": "directive_received",
         "payload": {**payload_base, "message_id": d["source_interaction_message_id"]}},
        {"event_key": f"directive:{d['id']}:classified", "kind": "directive_classified",
         "payload": {**payload_base, "consume_at": d["consume_at"]}},
    ]
    p = json.loads(d["payload_json"])
    if d["status"] == "pending":
        if d["hardness"] == "hard" and not p.get("confirmed"):
            evs.append({"event_key": f"directive:{d['id']}:pending_confirmation",
                        "kind": "directive_pending_confirmation",
                        "payload": {**payload_base, "polished": p.get("polished")}})   # 展示润色稿（§4.6.3）
        else:
            evs.append({"event_key": f"directive:{d['id']}:pending_effect",
                        "kind": "directive_pending_effect",
                        "payload": {**payload_base, "consume_at": d["consume_at"]}})   # 预计消费点
    elif d["status"] == "consumed":
        # 已确认硬指令必然途径 pending_effect；补齐该态事件（若消费前未扫描过，幂等 emit 不重复）
        evs.append({"event_key": f"directive:{d['id']}:pending_effect",
                    "kind": "directive_pending_effect",
                    "payload": {**payload_base, "consume_at": d["consume_at"]}})
        evs.append({"event_key": f"directive:{d['id']}:applied", "kind": "directive_applied",
                    "payload": {**payload_base,
                                "consumed_cycle": f"c{d['consumed_cycle']}" if d["consumed_cycle"] else None,
                                "effect": (d.get("_decision_effect") or {})}})
    elif d["status"] == "rejected":
        # 理由恒在 payload.rejection_reason（console.reject_directive 两条路径都 json_set 写入）
        evs.append({"event_key": f"directive:{d['id']}:rejected", "kind": "directive_rejected",
                    "payload": {**payload_base, "reason": p.get("rejection_reason")}})
    elif d["status"] == "superseded":
        evs.append({"event_key": f"directive:{d['id']}:superseded", "kind": "directive_superseded",
                    "payload": payload_base})
    return evs


class DirectiveNotifier:
    """从 DB 扫描派生 directive 生命周期事件 → outbox（幂等）。"""

    def __init__(self, daemon: WriteDaemon, outbox: Outbox):
        self.daemon = daemon
        self.outbox = outbox

    def scan(self) -> List[str]:
        """全量扫描（幂等：已排队事件跳过）。返回本次新排队的 event_key。"""
        new_keys: List[str] = []
        rows = self.daemon.query(
            "SELECT id, kind, hardness, status, consume_at, payload_json, consumed_cycle, "
            "consumed_decision_id, source_interaction_message_id FROM directive ORDER BY id")
        for (did, kind, hardness, status, consume_at, payload_json, ccy, cdec, smid) in rows:
            row = {"id": did, "kind": kind, "hardness": hardness, "status": status,
                   "consume_at": consume_at, "payload_json": payload_json,
                   "consumed_cycle": ccy, "source_interaction_message_id": smid}
            if cdec is not None:      # applied 效果摘要取自消费决策 payload（真相在 decision 台账）
                dp = self.daemon.query_one("SELECT payload_json FROM decision WHERE id=?", (cdec,))
                row["_decision_effect"] = (json.loads(dp[0]).get("effect") if dp else None)
            for ev in _directive_state_events(row):
                if self.outbox.emit(ev["event_key"], ev["kind"], ev["payload"]):
                    new_keys.append(ev["event_key"])
        return new_keys


# ----------------------------------------------------- file-request service --


def _regular_files_no_symlink(src: Path) -> List[Path]:
    """收集 src 下常规文件，**全链路不跟随符号链接**（外审 BLOCKER：item 目录本身或中途目录是 symlink
    时，rglob/is_dir 会跟进外部目录，其内常规文件绕过逐文件 is_symlink 检查把外部内容并入输入资产区）：
    src 自身是 symlink → 空；os.walk(followlinks=False) 不下钻 symlink 目录；逐文件再排 symlink；
    终检 resolve 落点必须在 src 实路径内（防花式逃逸）。"""
    if not src.is_dir() or src.is_symlink():
        return []
    root_real = src.resolve()
    out: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(src, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            if not str(p.resolve()).startswith(str(root_real) + os.sep):
                continue                     # 落点逃出 uploads item 根：拒收
            out.append(p)
    return sorted(out)


class FileRequestReject(Exception):
    """创建拒绝（§4.6.8 三判据 / schema 拒 / 已有 pending）——干净拒，不落单。"""


class FileRequestService:
    """文件请求全流水：create_checked（schema+policy 闸）→ [全局等待] → resolve/cancel（一次性迁终态）。"""

    def __init__(self, daemon: WriteDaemon, schema_set, policy: Dict[str, Any], input_root: str):
        self.daemon = daemon
        self.schema_set = schema_set          # SchemaSet：validator("resource_request")
        self.policy = policy["interaction_request"]
        self.input_root = Path(input_root)    # input/user_provided/ 的父目录（input/）

    def create_checked(self, *, goal_id: int, goal_ver: int, stage: str, request: Dict[str, Any],
                       cycle_id: Optional[str] = None, question_id: Optional[str] = None) -> int:
        """schema 校验 → **幂等先行** → 三判据 → interaction_request(pending)。
        幂等在 quota 之前（外审 SHOULD）：同 (goal_id, request_hash) 重试须返回既有单——否则达到上限后
        合法重试会被 quota 误拒，可重试性破坏。落单撞 uq_ireq_one_pending（同 goal 另一张 pending）→
        转业务拒因，不外泄 DDL 错误文本（外审 NIT）。"""
        from jsonschema import ValidationError
        try:
            self.schema_set.validator("resource_request").validate(request)
        except ValidationError as e:
            raise FileRequestReject(f"schema 拒: {e.message}") from e
        items = request["items"]
        items_json = json.dumps(items, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(items_json.encode()).hexdigest()
        existing = self.daemon.query_one(
            "SELECT id FROM interaction_request WHERE goal_id=? AND request_hash=? ORDER BY id LIMIT 1",
            (goal_id, request_hash))
        if existing:
            return existing[0]                     # 幂等重试：quota/enabled 都不再拦（单已存在）
        if not self.policy.get("enabled", True):
            raise FileRequestReject("文件请求通道未启用（policy.interaction_request.enabled=false）")
        if len(items) > self.policy["max_items_per_request"]:
            raise FileRequestReject(f"条目数 {len(items)} 超上限 {self.policy['max_items_per_request']}")
        n = self.daemon.query_one(
            "SELECT count(*) FROM interaction_request WHERE goal_id=? AND status IN ('pending','resolved')",
            (goal_id,))[0]
        if n >= self.policy["max_requests_per_goal"]:
            raise FileRequestReject(f"goal {goal_id} 请求数已达上限 {self.policy['max_requests_per_goal']}"
                                    "（pending+resolved 口径）")
        from .interaction import InteractionIngest
        try:
            return InteractionIngest(self.daemon).create_file_request(
                goal_id=goal_id, goal_ver=goal_ver, stage=stage, summary_md=request["summary_md"],
                items_json=items_json, request_hash=request_hash, cycle_id=cycle_id, question_id=question_id)
        except sqlite3.IntegrityError as e:
            if "uq_ireq_one_pending" in str(e) or "interaction_request" in str(e):
                raise FileRequestReject("同 goal 已有一张 pending 文件请求（先 resolve/cancel 再提新单）") from e
            raise

    def _check_provenance(self, request_id: int, resolved_message_id: int) -> tuple:
        """终态 provenance 校验（外审 SHOULD）：resolved_message_id 必须存在且与请求同 goal——
        否则可把别的 goal 的入站消息挂到本请求终态上，破坏"用户答复/取消 provenance"语义。
        消息 goal 未绑定（NULL）也拒（fail closed）。返回 (status, items_json)。"""
        row = self.daemon.query_one("SELECT status, items_json, goal_id FROM interaction_request WHERE id=?",
                                    (request_id,))
        if row is None:
            raise ValueError(f"interaction_request 不存在: {request_id}")
        msg = self.daemon.query_one("SELECT goal_id FROM interaction_message WHERE id=?", (resolved_message_id,))
        if msg is None:
            raise ValueError(f"provenance 消息不存在: {resolved_message_id}")
        if msg[0] is None or msg[0] != row[2]:
            raise ValueError(f"provenance 消息 goal（{msg[0]}）与请求 goal（{row[2]}）不符，拒绝挂账")
        return row[0], row[1]

    def resolve(self, *, request_id: int, uploads_dir: str, resolved_message_id: int) -> Dict[str, Any]:
        """uploads/<item_no>/ 逐文件复制并入 input/user_provided/<request_id>/<item_no>/ → 对**并入后
        字节**sha256 → resolution_json + resolved_* 一次性迁终态（trg_ireq_identity_frozen 只许这一跳）。
        条目目录缺失 = 用户未提供 → 该条记 unavailable（合法：部分提供也算 resolved，§4.6.8）。"""
        status, items_json = self._check_provenance(request_id, resolved_message_id)
        if status != "pending":
            raise ValueError(f"request {request_id} 非 pending（{status}），不可 resolve")
        items = json.loads(items_json)
        up = Path(uploads_dir)
        dest_root = self.input_root / "user_provided" / str(request_id)
        resolution: List[Dict[str, Any]] = []
        for i, _item in enumerate(items, start=1):
            src = up / str(i)
            files = _regular_files_no_symlink(src)
            if not files:
                resolution.append({"unavailable": "用户未提供该条目文件"})
                continue
            provided = []
            for f in files:
                dest = dest_root / str(i) / f.relative_to(src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                # hash 算在**并入后的目标字节**上（外审 SHOULD：uploads 是外部输入面，hash 源文件后
                # 再 copy 存在改写窗口——manifest 必须锚"实际并入的东西"）
                digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                provided.append({"path": str(dest), "hash": digest, "hash_alg": "sha256"})
            resolution.append({"provided": provided})
        with self.daemon.transaction() as conn:
            n = conn.execute(
                "UPDATE interaction_request SET status='resolved', resolution_json=?, "
                "resolved_at=CURRENT_TIMESTAMP, resolved_message_id=? WHERE id=? AND status='pending'",
                (json.dumps(resolution, ensure_ascii=False), resolved_message_id, request_id)).rowcount
            if n != 1:
                raise RuntimeError(f"request {request_id} resolve 竞态：迁移失败")
        return {"request_id": request_id, "resolution": resolution}

    def cancel(self, *, request_id: int, reason: str, resolved_message_id: int) -> None:
        """用户取消（同 provenance：入站消息回指，goal 校验同 resolve）。"""
        status, _ = self._check_provenance(request_id, resolved_message_id)
        with self.daemon.transaction() as conn:
            row = conn.execute("SELECT status FROM interaction_request WHERE id=?", (request_id,)).fetchone()
            if row[0] != "pending":
                raise ValueError(f"request {request_id} 非 pending（{row[0]}），不可 cancel")
            n = conn.execute(
                "UPDATE interaction_request SET status='cancelled', resolution_json=?, "
                "resolved_at=CURRENT_TIMESTAMP, resolved_message_id=? WHERE id=? AND status='pending'",
                (json.dumps({"cancelled": True, "reason": reason}, ensure_ascii=False),
                 resolved_message_id, request_id)).rowcount
            if n != 1:            # 兜底同 resolve（同事务已校验，理论不可达）
                raise RuntimeError(f"request {request_id} cancel 竞态：迁移失败")


class FileRequestNotifier:
    """文件请求 3 事件（§4.6.6）：request_pending / reminder（分档）/ resolved（含 cancelled）。
    now_ts 由调用方注入（unix 秒）——本模块不调 wall-clock（确定性可测；生产由驱动循环传 time.time()）。"""

    def __init__(self, daemon: WriteDaemon, outbox: Outbox, remind_interval_h: float):
        self.daemon = daemon
        self.outbox = outbox
        self.remind_interval_s = remind_interval_h * 3600

    def scan(self, now_ts: float) -> List[str]:
        new_keys: List[str] = []
        rows = self.daemon.query(
            "SELECT id, status, summary_md, resolution_json, strftime('%s', created_at) "
            "FROM interaction_request ORDER BY id")
        for rid, status, summary, resolution, created_ts in rows:
            base = {"request_id": rid, "summary_md": summary}
            if self.outbox.emit(f"filereq:{rid}:pending", "file_request_pending", base):
                new_keys.append(f"filereq:{rid}:pending")
            if status == "pending":
                elapsed = now_ts - float(created_ts)
                tier = int(elapsed // self.remind_interval_s)     # 每 interval 一档；同档幂等
                # 只发**当前档**、不补历史档（外审 NIT 权衡已定）：停机跨多档后一口气补发一串过期提醒
                # 是骚扰不是信息——提醒语义=「现在还在等」，当前档已完整表达等待时长（waited_intervals）
                if tier >= 1 and self.outbox.emit(f"filereq:{rid}:reminder:{tier}", "file_request_reminder",
                                                  {**base, "waited_intervals": tier}):
                    new_keys.append(f"filereq:{rid}:reminder:{tier}")
            else:
                if self.outbox.emit(f"filereq:{rid}:resolved", "file_request_resolved",
                                    {**base, "status": status,
                                     "resolution": json.loads(resolution) if resolution else None}):
                    new_keys.append(f"filereq:{rid}:resolved")
        return new_keys


# ------------------------------------------------------ advancer 前置检查 --

def _due_timings(cyc) -> List[str]:
    """到期时机结构判定：immediate 恒到期；stage_boundary 每格即边界（precheck 恰在格间跑）；
    reasoning_start 仅当**下一格**将进 reasoning——reasoning-only 轮（bootstrap/decompose）每格即
    reasoning；attack 轮 cycle.status 是"最后已提交阶段"游标（attack_stages.advance_stage），
    status='bundle' 的下一格才是 reasoning（早一格消费即违约）。cyc=None（开轮前）只消费前两类。"""
    due = ["immediate", "stage_boundary"]
    if cyc is not None and (
            cyc.route in ("bootstrap", "decompose") or
            (cyc.route == "attack" and cyc.status == "bundle")):
        due.append("reasoning_start")
    return due


def make_advancer_precheck(console: Console, daemon: WriteDaemon) -> Callable:
    """§4.4.1 前置检查（SqliteAdvancer.precheck 装配件）：先消费到期 directive（_due_timings）、再查
    阻断。返回 callable(cyc_or_none) -> Optional[str]（None=放行；str=拒因，Advancer 停止推进）。"""
    def precheck(cyc=None) -> Optional[str]:
        for timing in _due_timings(cyc):
            for did in console.pending_directives(timing):
                console.consume_directive(directive_id=did,
                                          cycle_id=(cyc.cycle_id if cyc is not None else None))
        if console.has_blocking_pause():
            return "pause 指令生效中（等待 resume）"
        pending = daemon.query_one("SELECT id FROM interaction_request WHERE status='pending' LIMIT 1")
        if pending:
            return f"文件请求 #{pending[0]} 等待用户提供（全局等待 v1：不发新研究执行）"
        return None

    return precheck
