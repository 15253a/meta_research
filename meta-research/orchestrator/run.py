"""run.py —— 全系统装配入口：一条命令把真组件 + 真 Codex 接成全自动元循环（M6 CP7.3）。

**这是「系统完整运行、进入全自动」的落点**：M0 driver 跑桩栈（验收栈）；本入口装配 M1–M5 的**真**
组件（冻结 DDL 库 / 单写 WriteDaemon / 真状态机 / 真编译器 / 真发布器 / 自终止安全网 / 人机前置检查）
+ CP7.2 StageProvider（真 CodexRunner），驱动 run_cycles 到停机（provider terminate / τ 自终止）。

**装配序**（幂等可恢复）：database.connect（新库建、既有库续，checksum 三重锁）→ WriteDaemon →
SQLiteStateStore → **查 goal 是否存在：仅首次才 parse_goal_brief + create_goal**（既有库续跑不依赖
brief 文件）→ SqliteCompiler/StatusPublisher 在各自**只读快照**按 cycle 的精确 goal 版本取正文 →
StopController + Console + make_advancer_precheck → StageProvider(CodexRunner) → SqliteAdvancer(全注入)。
重启同 work_dir 即续跑（状态在 DB，非进程内）——kill-9 恢复同 M3。

**步⑧（M7）范围**：本入口装配**全流程**——reasoning-only 闭环（M6 已落）+ **attack 轮全家**（CP8.4）：
StageProvider 四阶段（idea/plan/bundle/reasoning）+ JudgeProvider（真 Codex 双评审写库）+ AttackStages
（消费冻结 schema + manifest 驱动真执行）。仍明确拒的续轮：在途 import 物化轮（ImportWorker 未装配，
CP8.6）——NotImplementedError 干净报，不静默。

**双模式 A/B**（policy.session.dual_mode）：模式 A=一 turn 一阶段、模式 B=一 turn 跨多阶段。run_cycles
的内循环按阶段推进（格间过 precheck + 发布卡片），对两模式都成立；A/B 的会话粒度实测定默认 = 运维执行
（§7.4）。本入口读并记录 dual_mode。
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import database as _db
from . import obs_parser as OP
from .advancer import SqliteAdvancer
from .attack_stages import AttackStages
from .compiler_sqlite import SqliteCompiler
from .console import Console
from .console_ingest import ConsoleInboxIngest
from .connectors import ConnectorConfigError, OutboundDelivery, load_connectors
from .cost_ledger import CostLedger
from .gate_pool import PoolGate
from .gate_sqlite import SqliteGate, open_gate_read_conn
from .goalbrief import parse_goal_brief
from .mediator import CodexQueryResponder, Mediator, open_responder_read_conn
from .notify import (DirectiveNotifier, FileRequestNotifier, FileRequestService,
                     InteractionNotifier, Outbox, ResearchNotifier,
                     make_advancer_precheck)
from .runner import CodexRunner, terminate_active_process_groups
from .schemas import SchemaSet
from .stage_provider import JudgeProvider, StageProvider
from .statestore_sqlite import SQLiteStateStore
from .status_card import SqliteStatusPublisher
from .stopcontroller import StopController
from .writedaemon import WriteDaemon

_STAGES = ("idea", "plan", "bundle", "reasoning")
logger = logging.getLogger(__name__)


def _reject_nonfinite_policy_numbers(value: Any, path: str = "$") -> None:
    """YAML 可构造 NaN/±Inf，但它们不是合法 JSON number，jsonschema 的 Python 类型层未必会拒。

    policy 是研究与预算契约；在建 DB/启动循环前递归拒绝所有非有限浮点，避免比较式静默失效。
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"policy 含非有限数字 {path}={value!r}")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_policy_numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _reject_nonfinite_policy_numbers(item, f"{path}[{idx}]")


def _validate_outbound_config(policy: Dict[str, Any], config: Optional[Dict[str, Any]]) -> None:
    """Validate transport assembly before creating/chmod'ing the work root or DB."""
    if config is None:
        return
    if not isinstance(config, dict) or set(config) != {
            "channels", "retry_initial_s", "retry_max_s", "batch_size"}:
        raise ValueError("outbound_config 结构非法")
    if policy["interaction"].get("notify_matrix") != "all_qq_on":
        raise ValueError(
            f"尚不支持的 interaction.notify_matrix: {policy['interaction'].get('notify_matrix')!r}")
    channels = config["channels"]
    if not isinstance(channels, dict) or "qq" not in channels:
        raise ValueError("notify_matrix=all_qq_on 要求 connector profile 配置 qq channel")
    for channel, connector in channels.items():
        if not isinstance(channel, str) or not channel:
            raise ValueError("outbound_config channel 非法")
        if not callable(getattr(connector, "send", None)) or not callable(getattr(connector, "status", None)):
            raise ValueError(f"outbound connector {channel!r} 缺 send/status")
    initial = config["retry_initial_s"]
    maximum = config["retry_max_s"]
    batch = config["batch_size"]
    if (isinstance(initial, bool) or not isinstance(initial, (int, float))
            or not math.isfinite(float(initial)) or float(initial) < 0.1):
        raise ValueError("outbound retry_initial_s 非法")
    if (isinstance(maximum, bool) or not isinstance(maximum, (int, float))
            or not math.isfinite(float(maximum)) or float(maximum) < float(initial)):
        raise ValueError("outbound retry_max_s 非法")
    if isinstance(batch, bool) or not isinstance(batch, int) or not 4 <= batch <= 256:
        raise ValueError("outbound batch_size 非法")


class System:
    """装配好的全系统句柄：run() 驱动到停机，last_stop_reason 说明为何停（观测）。"""

    def __init__(self, *, advancer: SqliteAdvancer, state: SQLiteStateStore, daemon: WriteDaemon,
                 dual_mode: str, work_root: Path, sync_notifications: Optional[Callable[[], None]] = None,
                 sync_interactions: Optional[Callable[[], None]] = None,
                 interaction_pending: Optional[Callable[[], bool]] = None,
                 sync_accepted_interactions: Optional[Callable[[], None]] = None,
                 accepted_interaction_pending: Optional[Callable[[], bool]] = None,
                 sync_sideband: Optional[Callable[[], None]] = None,
                 outbound_delivery: Optional[OutboundDelivery] = None):
        self.advancer = advancer
        self.state = state
        self.daemon = daemon
        self.dual_mode = dual_mode
        self.work_root = work_root
        self.sync_notifications = sync_notifications or (lambda: None)
        self.sync_interactions = sync_interactions or (lambda: None)
        self.interaction_pending = interaction_pending or (lambda: False)
        self.sync_accepted_interactions = sync_accepted_interactions or self.sync_interactions
        self.accepted_interaction_pending = (
            accepted_interaction_pending or self.interaction_pending)
        self.sync_sideband = sync_sideband or (lambda: None)
        self.outbound_delivery = outbound_delivery
        self._pump_guard = threading.RLock()
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop: Optional[threading.Event] = None
        self._pump_error: Optional[BaseException] = None
        self._pump_owns_delivery = False
        self._interaction_exit_drained = False
        self._hard_stop_requested = False

    def _start_interaction_pump(self, poll_interval_s: float) -> bool:
        """Start the one resident spool/completion pump; return whether this call owns it."""
        sideband_interval = max(0.05, min(0.25, float(poll_interval_s)))
        with self._pump_guard:
            # A dead-but-uncollected thread still owns its error and delivery
            # lifecycle.  Nested run_forever→run must not overwrite that
            # evidence by silently starting a replacement.
            if self._pump_thread is not None:
                return False
            stop = threading.Event()
            self._pump_stop = stop
            self._pump_error = None
            delivery_owned = False
            if self.outbound_delivery is not None:
                delivery_owned = self.outbound_delivery.start(sideband_interval)
            self._pump_owns_delivery = delivery_owned

            def pump() -> None:
                while not stop.is_set():
                    try:
                        self.sync_interactions()
                        # Query completion may happen during a multi-hour research provider.  Derive and
                        # deliver its reply here instead of waiting for the next research stage boundary.
                        self.sync_sideband()
                        if self.outbound_delivery is not None:
                            self.outbound_delivery.raise_if_failed()
                    except sqlite3.OperationalError:
                        # Mediator retains the exact completion in memory;
                        # storage busy/full must be retried, not converted to
                        # unknown cost by ending the pump.
                        stop.wait(sideband_interval)
                        continue
                    except BaseException as error:
                        self._pump_error = error
                        stop.set()
                        return
                    stop.wait(sideband_interval)

            thread = threading.Thread(target=pump, daemon=False, name="interaction-pump")
            self._pump_thread = thread
            try:
                thread.start()
            except BaseException:
                self._pump_thread = None
                self._pump_stop = None
                if delivery_owned:
                    self.outbound_delivery.stop()
                self._pump_owns_delivery = False
                raise
            return True

    def _raise_resident_failure(self) -> None:
        """Surface a dead sideband/transport worker at the next safe supervisor poll."""
        with self._pump_guard:
            error = self._pump_error
        if error is not None:
            raise error
        if self.outbound_delivery is not None:
            self.outbound_delivery.raise_if_failed()

    def _stop_interaction_pump(self) -> Optional[BaseException]:
        with self._pump_guard:
            thread, stop = self._pump_thread, self._pump_stop
            if thread is None:
                return None
            if stop is not None:
                stop.set()
        thread.join()
        delivery_error = None
        if self._pump_owns_delivery and self.outbound_delivery is not None:
            delivery_error = self.outbound_delivery.stop()
        with self._pump_guard:
            error = self._pump_error
            self._pump_thread = None
            self._pump_stop = None
            self._pump_error = None
            self._pump_owns_delivery = False
        if error is not None and delivery_error is not None:
            add_note = getattr(error, "add_note", None)
            if callable(add_note):
                add_note(f"outbound delivery 失败: {type(delivery_error).__name__}: {delivery_error}")
            return error
        return error or delivery_error

    def flush_outbound(self) -> Dict[str, Any]:
        """Attempt one bounded priority batch, then report every item left durable."""
        if self.outbound_delivery is not None:
            delivered = self.outbound_delivery.tick()
            status = self.outbound_delivery.pending_status()
            status["delivered_now"] = len(delivered)
            if status["pending"]:
                logger.warning(
                    "受控退出后仍有 durable outbound backlog pending=%s retrying=%s urgent=%s",
                    status["pending"], status["retrying"], status["urgent_pending"])
            return status
        return {"pending": 0, "retrying": 0, "urgent_pending": 0,
                "channels": {}, "delivered_now": 0}

    def _sync_interactions_retry(self, attempts: int = 3) -> None:
        self._retry_interaction_sync(self.sync_interactions, attempts=attempts)

    @staticmethod
    def _retry_interaction_sync(sync: Callable[[], None], attempts: int = 3) -> None:
        for attempt in range(attempts):
            try:
                sync()
                return
            except sqlite3.OperationalError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def run(self, max_cycles: int) -> List[str]:
        """驱动 run_cycles 到停机（terminate / τ 自终止 / 阻断 / max_cycles）。返回本次推进的 cycle_id。
        reasoning-only 下模式 A≡B（每轮一阶段）——故直接 run_cycles；attack 多阶段的 A/B 分驱 = CP7.4。"""
        pump_owner = self._start_interaction_pump(0.05)
        if pump_owner:
            self._interaction_exit_drained = False
            self._hard_stop_requested = False
        try:
            result = self.advancer.run_cycles(max_cycles)
        except BaseException as primary:
            try:
                pump_error = self._stop_interaction_pump() if pump_owner else None
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            if pump_error is not None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(f"interaction pump 失败: {type(pump_error).__name__}: {pump_error}")
            # provider 可在抛 StageBlockedOnResources 前刚创建请求；异常退出也尽力补扫。但 outbox 是 DB
            # 派生物，扫描失败绝不能覆盖研究主链的 primary（否则真正损坏因会被 finally 异常遮蔽）。
            try:
                self.sync_notifications()
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            except BaseException as secondary:
                note = f"退出边界 notification scan 失败: {type(secondary).__name__}: {secondary}"
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(note)
                else:
                    notes = list(getattr(primary, "__notes__", ()))
                    notes.append(note)
                    try:
                        primary.__notes__ = notes
                    except BaseException:
                        pass
            if pump_owner:
                try:
                    self.drain_interactions(
                        poll_interval_s=0.05, accept_final_spool=pump_error is None)
                except KeyboardInterrupt:
                    self._hard_stop_requested = True
                    raise
                except BaseException as secondary:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"退出边界 interaction drain 失败: "
                            f"{type(secondary).__name__}: {secondary}")
                else:
                    self._interaction_exit_drained = True
            raise
        pump_error = self._stop_interaction_pump() if pump_owner else None
        if pump_error is not None:
            try:
                self.drain_interactions(
                    poll_interval_s=0.05, accept_final_spool=False)
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            except BaseException as secondary:
                add_note = getattr(pump_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"退出边界 interaction drain 失败: "
                        f"{type(secondary).__name__}: {secondary}")
            else:
                self._interaction_exit_drained = True
            raise pump_error
        # Durable global_stop is checked before Advancer.precheck.  Keep the interaction sideband alive anyway:
        # stopped research must still ingest status queries/actions, while never reopening a research cycle.
        if pump_owner:
            self.drain_interactions(poll_interval_s=0.05)
            self._interaction_exit_drained = True
        else:
            self._sync_interactions_retry()
        # 正常停机时 notifier 失败仍 fail loud；调用方可修复派生 outbox 后从 DB 重扫。
        self.sync_notifications()
        if pump_owner:                         # nested run_forever already owns the non-blocking delivery thread
            self.flush_outbound()
        return result

    def drain_interactions(self, *, poll_interval_s: float = 0.1,
                           accept_final_spool: bool = True) -> None:
        """Accept one final spool boundary, then drain only that durable work (safe CLI exit boundary)."""
        if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                or not math.isfinite(float(poll_interval_s)) or poll_interval_s < 0.01):
            raise ValueError("poll_interval_s 须为不小于 0.01 的有限秒数（防交互排空热自旋）")
        if not isinstance(accept_final_spool, bool):
            raise ValueError("accept_final_spool 须为 bool")
        # Exactly one intake probe closes the append-during-final-provider race.  Subsequent iterations only
        # finalize the runner_calls accepted by this boundary, so a live producer cannot postpone shutdown forever.
        notification_error: Optional[Exception] = None

        def scan_notifications() -> None:
            nonlocal notification_error
            try:
                self.sync_notifications()
            except Exception as error:
                # Outbox is derived/rebuildable.  Preserve fail-loud reporting, but never let it strand an
                # already charged/accepted query before its runner_call + ledger + reply reach a terminal state.
                if notification_error is None:
                    notification_error = error

        if accept_final_spool:
            self._sync_interactions_retry()
        scan_notifications()
        while self.accepted_interaction_pending():
            self._retry_interaction_sync(self.sync_accepted_interactions)
            scan_notifications()
            if not self.accepted_interaction_pending():
                break
            time.sleep(float(poll_interval_s))
        if notification_error is not None:
            raise notification_error

    def run_forever(self, max_cycles: int, *, poll_interval_s: float = 1.0,
                    linger_after_terminal: bool = True,
                    stop_event: Optional[threading.Event] = None) -> List[str]:
        """默认 CLI 常驻闭环：pause/file-request 阻断时保留唯一写进程并周期重跑 precheck。

        每次 poll 都会 ingest spool、消费可执行动作并扫描通知/reminder；解除阻断后从 DB 游标续同一阶段。
        ``max_cycles`` 是本次常驻会话**累计完成轮数**，不会因一次阻断后重入而重新获得预算。
        prior terminate / durable τ stop / max-cycles 只终止研究，默认仍长在线回答快照查询；
        Ctrl-C 由 CLI 捕获并排空已接受回执。测试/嵌入调用可用 ``stop_event`` 或
        ``linger_after_terminal=False`` 明确结束常驻会话。``stop_event`` 在 durable cycle 边界协作生效，
        不强杀一个正在执行的 stage；紧急退出由第二次 Ctrl-C 的进程组 hard-stop 负责。
        """
        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
            raise ValueError("max_cycles 须为非负整数")
        if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                or not math.isfinite(float(poll_interval_s)) or poll_interval_s < 0.01):
            raise ValueError("poll_interval_s 须为不小于 0.01 的有限秒数（防阻断时热自旋）")
        completed: List[str] = []
        research_terminal = False
        external_stop = stop_event or threading.Event()
        self._interaction_exit_drained = False
        self._hard_stop_requested = False
        pump_owner = self._start_interaction_pump(float(poll_interval_s))
        try:
            while not external_stop.is_set():
                self._raise_resident_failure()
                batch: List[str] = []
                ran_research = False
                if not research_terminal:
                    remaining = max_cycles - len(completed)
                    if remaining <= 0 or self.last_stop_reason is not None:
                        research_terminal = True
                    else:
                        ran_research = True
                        # Return control after every durable cycle so a resident stop_event cannot be hidden
                        # inside one run_cycles(100+) call.  Stage-level work still obeys normal safe boundaries.
                        batch = self.run(1)
                        completed.extend(batch)
                        if (len(completed) >= max_cycles or self.last_stop_reason is not None
                                or (not batch and self.advancer.last_block_reason is None)):
                            research_terminal = True

                # Unconditional probe closes the cached-has_pending race for a
                # record appended during the final research provider call.
                if not ran_research:
                    self._sync_interactions_retry()
                    self.sync_notifications()
                pending = self.interaction_pending()
                if research_terminal and not linger_after_terminal and not pending:
                    break
                external_stop.wait(float(poll_interval_s))
                self._raise_resident_failure()
        except BaseException as primary:
            pump_error = None
            if pump_owner:
                try:
                    pump_error = self._stop_interaction_pump()
                except KeyboardInterrupt:
                    self._hard_stop_requested = True
                    raise
                if pump_error is not None:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"interaction pump 失败: {type(pump_error).__name__}: {pump_error}")
            try:
                self.drain_interactions(
                    poll_interval_s=float(poll_interval_s),
                    accept_final_spool=pump_error is None)
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            except BaseException as secondary:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"退出边界 interaction drain 失败: "
                        f"{type(secondary).__name__}: {secondary}")
            else:
                self._interaction_exit_drained = True
            raise
        if pump_owner:
            try:
                pump_error = self._stop_interaction_pump()
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            if pump_error is not None:
                try:
                    self.drain_interactions(
                        poll_interval_s=float(poll_interval_s), accept_final_spool=False)
                except KeyboardInterrupt:
                    self._hard_stop_requested = True
                    raise
                except BaseException as secondary:
                    add_note = getattr(pump_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"退出边界 interaction drain 失败: "
                            f"{type(secondary).__name__}: {secondary}")
                else:
                    self._interaction_exit_drained = True
                raise pump_error
        # stop_event/linger 是受控退出，不是丢弃：所有已经持久接纳的 query 必须先有终态回执。
        try:
            self.drain_interactions(poll_interval_s=float(poll_interval_s))
        except KeyboardInterrupt:
            self._hard_stop_requested = True
            raise
        self._interaction_exit_drained = True
        self.flush_outbound()
        return completed

    @property
    def last_stop_reason(self) -> Optional[str]:
        return self.advancer.last_stop_reason


def build_system(system_root: str, work_root: str, *, runner_factory: Optional[Callable] = None,
                 attack=True, outbound_config: Optional[Dict[str, Any]] = None) -> System:
    """装配全系统。system_root=含 input/goal_brief.md · policies/ · prompts/ · schemas/ 的仓库根；
    work_root=运行产物根（research.sqlite / cycles / state 落此）。runner_factory=注入式 Runner 工厂
    （默认真 CodexRunner；测试传 mock）。attack：True=全装（默认）；False/None=退化 reasoning-only
    （诊断用）；AttackStages 实例=注入自定装配。``outbound_config`` 来自受限 connector profile；None
    仅用于单测/显式 ``--no-outbound``，绝不伪装成已投递。"""
    root = Path(system_root)
    work = Path(work_root)
    policy = yaml.safe_load((root / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    schemas = SchemaSet(root / "schemas")
    schemas.validator("policy").validate(policy)    # 启动前机械校验；不能只靠 tests 校验仓库默认文件
    _reject_nonfinite_policy_numbers(policy)         # JSON Schema/Python 边界：显式拒 NaN/±Inf
    CostLedger.validate_policy(policy)               # 成本边界（float 溢出/布尔值等）也在创建 work/DB 前验完
    _validate_outbound_config(policy, outbound_config)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work, 0o700)                  # query service UID cannot traverse to DB/raw artifacts

    db_path = str(work / "research.sqlite")
    writer_conn = _db.connect(db_path)
    os.chmod(db_path, 0o600)
    daemon = WriteDaemon(writer_conn)                      # 新库建 / 既有库续（checksum 三重锁）
    state = SQLiteStateStore(daemon, policy)
    if daemon.query_one("SELECT 1 FROM goal LIMIT 1") is None:
        # **仅首次建 goal 才解析 brief**（外审 SHOULD）：重启时 DB goal 权威——若无条件解析，缺失/畸形
        # 的 goal_brief.md 会卡死本可续跑的既有库（与「DB 权威、同 work_root 可恢复」相悖）。
        brief = parse_goal_brief(root / "input" / "goal_brief.md")
        state.create_goal(text=brief["body_md"], predicate_json=brief["predicate_json"])

    # compiler/publisher 各用**只读连接**（外审 BLOCKER：单写纪律——入口层就 enforce 只读边界，
    # 防 compiler 侧误写绕过 WriteDaemon/账本/authorizer）。open_responder_read_conn = mode=ro+全写拒
    # authorizer（放行 SELECT/TRANSACTION，render/publish 的 BEGIN…COMMIT 读快照可跑）。
    compiler = SqliteCompiler(open_responder_read_conn(db_path), policy)
    publisher = SqliteStatusPublisher(open_responder_read_conn(db_path), policy=policy,
                                      out_path=str(work / "state" / "status_card.json"))
    stop = StopController(daemon, policy)
    console = Console(daemon, policy=policy)
    # sidecar 创建与控制台 resolve/cancel 共用同一服务实例；托管文件必须落在**本次 work_root** 内：
    # ①不同运行的 request_id 不会在仓库 input/ 互相覆盖；②manifest 默认 work_root 路径围栏可真实消费；
    # ③大文件/敏感文件不进入 Git 工作树。后者由 inbox ingest 在 run 单写进程内调用。
    file_requests = FileRequestService(daemon, schemas, policy, input_root=str(work / "input"))
    system_prompt = (root / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    skills = {s: (root / "prompts" / "skills" / s / "SKILL.md").read_text(encoding="utf-8") for s in _STAGES}
    rf = runner_factory or (lambda transcripts_dir, purpose_tag:
                            CodexRunner(transcripts_dir=transcripts_dir, purpose_tag=purpose_tag,
                                        tool_free=purpose_tag == "interaction-query"))
    cost_ledger = CostLedger(daemon, policy)     # 所有真 LLM 调用共用同一预算/账本投影
    # 步⑨ CP9.3 入站闭环：控制台命令经 console_server 落 <work>/state/console_inbox.jsonl（连接器缓冲）→
    # precheck 边界 ingest 进权威入站链（handle_inbound 落 directive/note；query 经 mediator 应答）。
    # mediator 用同一 status_card.json（publisher 阶段边界原子发布的那份）做接地卡。
    query_responder = CodexQueryResponder(
        runner_factory=rf,
        validator=schemas.validator("interaction_reply_candidate"),
        system_prompt=system_prompt,
        skill=(root / "prompts" / "skills" / "interaction_query" / "SKILL.md").read_text(
            encoding="utf-8"),
        work_root=str(work),
    )
    mediator = Mediator(
        daemon, str(work / "state" / "status_card.json"),
        responder=query_responder,
        rebuild_last_n=policy["interaction"]["mediator_rebuild_last_n"],
        cost_ledger=cost_ledger,
    )
    inbox_ingest = ConsoleInboxIngest(console, mediator, str(work), file_requests=file_requests,
                                      system_root=str(root))
    base_precheck = make_advancer_precheck(console, daemon)
    outbox = Outbox(str(work / "state"))
    directive_notifier = DirectiveNotifier(daemon, outbox)
    interaction_notifier = InteractionNotifier(daemon, outbox)
    research_notifier = ResearchNotifier(daemon, outbox, policy["flow"]["audit_cadence_K"])
    file_request_notifier = FileRequestNotifier(
        daemon, outbox, policy["interaction_request"]["remind_interval_h"])
    delivery = None
    if outbound_config is not None:
        channels = outbound_config.get("channels")
        delivery = OutboundDelivery(
            outbox, channels, default_channels=["qq"],
            retry_initial_s=outbound_config["retry_initial_s"],
            retry_max_s=outbound_config["retry_max_s"],
            batch_size=outbound_config["batch_size"],
        )
    notification_lock = threading.RLock()
    sideband_notification_lock = threading.Lock()
    sideband_last_scan = [float("-inf")]

    def sync_notifications() -> None:
        with notification_lock:
            directive_notifier.scan()
            interaction_notifier.scan()
            research_notifier.scan()
            file_request_notifier.scan(time.time())

    def sync_sideband_notifications() -> None:
        # Full notifier scans are derived/replayable but grow with audit history;
        # 4 Hz keeps query replies well inside the 2 s SLA without polling every
        # 50 ms during a multi-hour provider.
        now = time.monotonic()
        with sideband_notification_lock:
            if now - sideband_last_scan[0] < 0.25:
                return
            sideband_last_scan[0] = now
        sync_notifications()

    def precheck(cyc=None) -> Optional[str]:
        inbox_ingest.ingest(cyc)              # 先 ingest 控制台入站；故障不裸崩，但 backlog 会在本边界阻断研究
        if inbox_ingest.has_pending:
            # Spool 是人类动作的到达顺序。队首 retry/sidecar 损坏/下一批 backlog 未排空时，不能先消费
            # 已在 DB 的 due directive；更晚到但已 ACK 的 reject/resume 可能正卡在该入站故障之后。
            sync_notifications()               # 观测/提醒仍可重扫，但不产生任何 directive 状态效果
            return "控制台入站待处理/故障（等待下轮重试）"
        reason = base_precheck(cyc)            # 再消费到期 directive + 查阻断（pause / 文件请求全局等待）
        sync_notifications()                   # 动作/消费后的真实状态立即派生通知（emit 幂等）
        return reason

    # sidecar→文件请求桥（步⑧ CP8.5）：阶段产 resource_request.json → interaction_request(pending) →
    # StageBlockedOnResources → run_cycles 干净停 → precheck 全局等待；用户 resolve 到 input/user_provided/
    # 后续跑重做该阶段。goal 版本按当下最新（goal_amend 后新请求挂新版）。
    def file_request_bridge(stage: str, request: Dict[str, Any], cyc) -> int:
        gid, gver = daemon.query_one("SELECT id, version FROM goal ORDER BY version DESC LIMIT 1")
        return file_requests.create_checked(
            goal_id=gid, goal_ver=gver, stage=stage, request=request,
            cycle_id=getattr(cyc, "cycle_id", None), question_id=getattr(cyc, "question_id", None))

    provider = StageProvider(runner_factory=rf, schemas=schemas, policy=policy,
                             system_prompt=system_prompt, skills=skills, work_root=str(work),
                             file_request_bridge=file_request_bridge, cost_ledger=cost_ledger)

    attack_stages = attack if isinstance(attack, AttackStages) else None
    if attack is True:
        # attack 全家（步⑧ CP8.4）：正式 gate 通道 + manifest 驱动真执行 + 真 Codex 双评审。
        # 判据读连接各司其职：gate 家族走 open_gate_read_conn（authorizer 拒观测 9 表——判据隔离）；
        # parser_suspect 须读 execution_observation → 走 open_responder_read_conn（mode=ro 全写拒，可读全表）。
        pool_gate = PoolGate(daemon, open_gate_read_conn(db_path))
        obs_conn = open_responder_read_conn(db_path)
        close_gate = SqliteGate(daemon, open_gate_read_conn(db_path), schemas,
                                parser_suspect=lambda aid: OP.suspect_for_attempt(
                                    obs_conn, aid, policy["observation"]))
        judge = JudgeProvider(
            runner_factory=rf, schemas=schemas, policy=policy, system_prompt=system_prompt,
            skill=(root / "prompts" / "skills" / "judge" / "SKILL.md").read_text(encoding="utf-8"),
            daemon=daemon, work_root=str(work), cost_ledger=cost_ledger)
        attack_stages = AttackStages(
            state=state, compiler=compiler, pool_gate=pool_gate, close_gate=close_gate,
            providers={"idea": provider.idea, "plan": provider.plan, "bundle": provider.bundle,
                       "judge": judge, "reasoning": provider.reasoning},
            obs_policy=policy["observation"], work_root=str(work), schemas=schemas, policy=policy)

    advancer = SqliteAdvancer(state, compiler, provider.reasoning, attack=attack_stages,
                              status_publisher=publisher, precheck=precheck, stop_controller=stop)
    return System(advancer=advancer, state=state, daemon=daemon,
                  dual_mode=policy.get("session", {}).get("dual_mode", "A"), work_root=work,
                  sync_notifications=sync_notifications,
                  sync_interactions=lambda: inbox_ingest.ingest(None),
                  interaction_pending=lambda: inbox_ingest.interaction_pending,
                  sync_accepted_interactions=inbox_ingest.poll_accepted,
                  accepted_interaction_pending=(
                      lambda: inbox_ingest.accepted_interaction_pending),
                  sync_sideband=sync_sideband_notifications,
                  outbound_delivery=delivery)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="meta-research 全自动元循环入口（M6 CP7.3；reasoning-only 闭环）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 input/policies/prompts/schemas）")
    ap.add_argument("--work-root", required=True, help="运行产物根（research.sqlite 落此，重启同目录即续跑）")
    ap.add_argument("--max-cycles", type=int, default=100, help="本次最多推进轮数（安全上限，与 τ 自终止并存）")
    ap.add_argument("--once", action="store_true",
                    help="一次性模式：遇 pause/文件请求即返回；默认保持 run 单写进程常驻等待并自动续跑")
    ap.add_argument("--poll-interval-s", type=float, default=1.0,
                    help="常驻等待时 ingest spool / 扫描 reminder 的轮询秒数（默认 1.0）")
    outbound = ap.add_mutually_exclusive_group()
    outbound.add_argument(
        "--connector-profile",
        help="出站 connector JSON（默认 <system-root>/connectors/outbound.json；凭据只从其中命名的环境变量读取）")
    outbound.add_argument(
        "--no-outbound", action="store_true",
        help="显式禁用外部投递（仅离线/测试；通知仍在本地 outbox，不得视为生产交付）")
    args = ap.parse_args(argv)
    outbound_config = None
    if args.no_outbound:
        print("[run] 警告：外部 connector 已显式禁用；本地 outbox 不代表通知已交付", file=sys.stderr)
    else:
        profile = args.connector_profile or str(Path(args.system_root) / "connectors" / "outbound.json")
        try:
            outbound_config = load_connectors(profile)
        except (ConnectorConfigError, OSError) as error:
            print(f"[run] connector 配置失败：{error}；离线运行须显式加 --no-outbound", file=sys.stderr)
            return 2
    system = build_system(args.system_root, args.work_root, outbound_config=outbound_config)
    try:
        if args.once:
            ids = system.run(args.max_cycles)
        else:
            ids = system.run_forever(args.max_cycles, poll_interval_s=args.poll_interval_s)
    except KeyboardInterrupt:
        shutdown_errors = []
        hard_stop = bool(getattr(system, "_hard_stop_requested", False))
        already_drained = bool(getattr(system, "_interaction_exit_drained", False))

        def hard_stop_now() -> None:
            nonlocal hard_stop
            hard_stop = True
            try:
                terminate_active_process_groups()
            except BaseException as e:
                shutdown_errors.append(f"后台 query 进程组终止失败：{e}")

        if hard_stop:
            hard_stop_now()
        if not hard_stop and not already_drained:
            try:
                # First SIGINT drains accepted work.  A SIGINT during that drain is not swallowed by
                # run_forever: it sets _hard_stop_requested and reaches this handler as the escape hatch.
                drain = getattr(system, "drain_interactions", None)
                if callable(drain):
                    drain(poll_interval_s=args.poll_interval_s)
            except KeyboardInterrupt:
                hard_stop_now()
            except Exception as e:
                shutdown_errors.append(f"interaction 排空失败：{e}")
        if not hard_stop:
            try:
                system.sync_notifications()
                flush = getattr(system, "flush_outbound", None)
                if callable(flush):
                    flush()
            except KeyboardInterrupt:
                hard_stop_now()
            except Exception as e:
                shutdown_errors.append(f"通知扫描失败：{e}")
        if shutdown_errors:                  # 退出失败也不把 Ctrl-C 变 traceback
            print("[run] Ctrl-C 退出前" + "；".join(shutdown_errors))
        print("[run] 收到 Ctrl-C，已立即硬停" if hard_stop else "[run] 收到 Ctrl-C，已停止单写循环")
        return 130
    except NotImplementedError as e:
        # 干净报（非裸 traceback）：具体缺哪个组件由异常文本自述（如 attack 退化装配缺 AttackStages、
        # 在途 import 物化轮缺 ImportWorker[CP8.6]）——文案不预设单一来源（codex NIT）
        print(f"[run] 停：续本轮需尚未装配的组件——{e}")
        return 2
    # 停因优先级（外审 SHOULD）：τ 自终止 > precheck 阻断（pause/文件请求）> 正常收尾——阻断对运维判断
    # 关键，不能被 idle 掩盖
    reason = (system.last_stop_reason or system.advancer.last_block_reason
              or ("prior-terminate/idle" if not ids else "max_cycles/terminate"))
    print(f"[run] dual_mode={system.dual_mode} 推进 {len(ids)} 轮：{ids}；停因={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
