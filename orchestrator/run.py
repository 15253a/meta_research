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
StageProvider 四阶段（idea/plan/bundle/reasoning）+ PlanReviewProvider（plan 独审）+
JudgeProvider（bundle 双评审写库）+ AttackStages
（消费冻结 schema + manifest 驱动真执行）+ ImportWorker（冻结候选 snapshot 解码、worker-cycle 恢复；
默认 untrusted adapter 与全部生产 manifest 命令只经 exact-pinned Docker sandbox 执行；后端/镜像/隔离
能力在打开 SQLite 前预检，缺失即 fail closed，绝不回退到 host 裸跑）。

**会话模式 A**（policy.session.dual_mode）：一 turn 只推进一个阶段，每个阶段都由编排器耐久提交后再进入下一格。
reference 曾设想“一 turn 跨多阶段且 turn 内即时提交”的 B，但当前无状态 `run_task` 窄接口没有中途回调/提交能力。
为避免把 B 静默当 A 运行，policy schema 与 `System` 都 fail-closed 只接受 A；若未来真实引入 turn-checkpoint 协议，须另走受审契约。
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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import yaml

from . import database as _db
from . import obs_parser as OP
from .advancer import SqliteAdvancer
from .attack_stages import AttackStages
from .compiler_sqlite import SqliteCompiler
from .console import Console
from .console_ingest import ConsoleInboxIngest
from .console_spool import open_directory_path
from .connector_ingest import ConnectorInboxIngest
from .connectors import ConnectorConfigError, OutboundDelivery, load_connectors
from .cost_ledger import CostLedger
from .deployment_preflight import DeploymentPreflight, DeploymentPreflightError
from .execution_reconcile import ExecutionReconciler
from .execution_sandbox import DockerExecutionSandbox
from .dependency_image import PythonWheelImageBuilder
from .gate_pool import PoolGate
from .gate_sqlite import SqliteGate, open_gate_read_conn
from .goalbrief import parse_goal_brief
from .instance_lease import InstanceLease, InstanceLeaseError
from .import_fetcher import FrozenCandidateFetcher
from .import_search import GitHubRepoSearchProvider, ImportSearchService
from .import_triggers import (
    BoundedReferenceSnapshotProvider,
    ImportTriggerRouter,
    TrustedImportTriggerService,
)
from .import_worker import ImportWorker
from .repository_materializer import (
    GitHubRepositoryMaterializer,
    ProductionCandidateFetcher,
)
from .repository_adapter_generation import AdapterGenerationService
from .mediator import CodexQueryResponder, Mediator, open_responder_read_conn
from .notify import (DirectiveNotifier, FileRequestNotifier, FileRequestReject, FileRequestService,
                     InteractionNotifier, Outbox, ResearchNotifier,
                     make_advancer_precheck)
from .process_supervisor import ExecutionSupervisor
from .qualification_firewall import (
    QualificationFirewallError,
    load_qualification_firewall,
)
from .runner import CodexRunner, terminate_active_process_groups
from .schemas import SchemaSet
from .stage_provider import JudgeProvider, PlanReviewProvider, StageProvider
from .statestore_sqlite import SQLiteStateStore
from .status_card import SqliteStatusPublisher
from .storage_governance import CycleSnapshotPublisher
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
        if (not callable(getattr(connector, "send", None))
                or not callable(getattr(connector, "poll", None))
                or not callable(getattr(connector, "status", None))):
            raise ValueError(f"connector {channel!r} 缺 send/poll/status")
        has_inbound = getattr(connector, "has_inbound", False)
        if not isinstance(has_inbound, bool):
            raise ValueError(f"connector {channel!r} has_inbound 须为 bool")
        if has_inbound:
            required = (
                "commit_poll", "load_inbound_retry_counts",
                "store_inbound_retry_counts", "inbound_pending_status",
                "validate_inbound_envelope", "record_inbound_fatal",
                "bind_owner_guard",
                "prepare_inbound", "start_inbound", "stop_inbound",
                "inbound_stopped", "raise_if_inbound_failed",
            )
            missing = [name for name in required
                       if not callable(getattr(connector, name, None))]
            inbound_spool = getattr(connector, "inbound_spool", None)
            if missing or inbound_spool is None:
                detail = ",".join(missing + (["inbound_spool"]
                                             if inbound_spool is None else []))
                raise ValueError(
                    f"connector {channel!r} durable inbound 契约不完整: {detail}")
            if getattr(connector, "channel", None) != channel:
                raise ValueError(f"connector {channel!r} inbound channel 绑定漂移")
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


class _GuardedRunner:
    """Fence the last boundary before an injected Runner starts external work."""

    def __init__(self, inner: Any, owner_guard: Callable[[], None]):
        self.inner = inner
        self.owner_guard = owner_guard

    def run_task(self, *args, **kwargs):  # noqa: ANN002,ANN003,ANN201 - protocol passthrough
        self.owner_guard()
        return self.inner.run_task(*args, **kwargs)

    def bind_runner_call(self, **kwargs):  # noqa: ANN003,ANN201 - optional runner capability
        self.owner_guard()
        bind = getattr(self.inner, "bind_runner_call", None)
        if callable(bind):
            return bind(**kwargs)
        return None

    @property
    def tool_free_contract(self):  # noqa: ANN201 - optional runner capability
        return getattr(self.inner, "tool_free_contract", None)


class _AssemblyCleanup:
    """Retryable owner capability for a constructor that failed mid-assembly."""

    def __init__(self, resource_closers: List[Callable[[], None]],
                 lease: Optional[InstanceLease]):
        self._resource_closers = list(resource_closers)
        self._lease = lease
        self._guard = threading.Lock()

    @property
    def closed(self) -> bool:
        return not self._resource_closers and (
            self._lease is None or self._lease.closed)

    def close(self) -> Optional[BaseException]:
        with self._guard:
            first_error: Optional[BaseException] = None
            remaining: List[Callable[[], None]] = []
            for closer in reversed(self._resource_closers):
                try:
                    closer()
                except BaseException as error:
                    remaining.append(closer)
                    if first_error is None:
                        first_error = error
            self._resource_closers = list(reversed(remaining))
            if self._resource_closers:
                return first_error or RuntimeError("system assembly resource close 未完成")
            if self._lease is not None and not self._lease.closed:
                lease_error = self._lease.close()
                if first_error is None and lease_error is not None:
                    first_error = lease_error
            return first_error


class System:
    """装配好的全系统句柄：run() 驱动到停机，last_stop_reason 说明为何停（观测）。"""

    def __init__(self, *, advancer: SqliteAdvancer, state: SQLiteStateStore, daemon: WriteDaemon,
                 dual_mode: str, work_root: Path, sync_notifications: Optional[Callable[[], None]] = None,
                 sync_interactions: Optional[Callable[[], None]] = None,
                 interaction_pending: Optional[Callable[[], bool]] = None,
                 sync_accepted_interactions: Optional[Callable[[], None]] = None,
                 accepted_interaction_pending: Optional[Callable[[], bool]] = None,
                 sync_closed_inbound: Optional[Callable[[], None]] = None,
                 closed_inbound_pending: Optional[Callable[[], bool]] = None,
                 sync_sideband: Optional[Callable[[], None]] = None,
                 outbound_delivery: Optional[OutboundDelivery] = None,
                 start_inbound: Optional[Callable[[], Any]] = None,
                 stop_inbound: Optional[Callable[[Any], Optional[BaseException]]] = None,
                 raise_inbound: Optional[Callable[[], None]] = None,
                 inbound_cleanup_pending: Optional[
                     Callable[[Any, Optional[BaseException]], bool]] = None,
                 instance_lease: Optional[InstanceLease] = None,
                 execution_supervisor: Optional[ExecutionSupervisor] = None,
                 deployment_receipt: Optional[Dict[str, Any]] = None,
                 resource_closers: Optional[List[Callable[[], None]]] = None):
        if dual_mode != "A":
            raise ValueError(
                "session.dual_mode 当前只支持 A（一 turn 一阶段）；"
                "B 缺 turn 内跨阶段耐久提交协议，拒绝静默按 A 运行")
        self.advancer = advancer
        self.state = state
        self.daemon = daemon
        self.work_root = work_root
        self._sync_notifications_cb = sync_notifications or (lambda: None)
        self._sync_interactions_cb = sync_interactions or (lambda: None)
        self._interaction_pending_cb = interaction_pending or (lambda: False)
        self._sync_accepted_interactions_cb = (
            sync_accepted_interactions or self._sync_interactions_cb)
        self._accepted_interaction_pending_cb = (
            accepted_interaction_pending or self._interaction_pending_cb)
        # A resident connector probe intentionally accepts only one event per
        # channel for fairness.  Once the owned listeners are closed, however,
        # their already-fsynced spool is a finite acceptance boundary and must
        # be consumed to EOF before process exit.  Keep this separate from the
        # console/general intake probe: an independently running console
        # producer must not be able to postpone shutdown forever.
        self._sync_closed_inbound_cb = sync_closed_inbound or (lambda: None)
        self._closed_inbound_pending_cb = closed_inbound_pending or (lambda: False)
        self._sync_sideband_cb = sync_sideband or (lambda: None)
        self.outbound_delivery = outbound_delivery
        self._start_inbound_cb = start_inbound or (lambda: [])
        self._stop_inbound_cb = stop_inbound or (lambda _owned: None)
        self._raise_inbound_cb = raise_inbound or (lambda: None)
        self.inbound_cleanup_pending = (
            inbound_cleanup_pending
            or (lambda _owned, error: error is not None))
        self.instance_lease = instance_lease
        self.execution_supervisor = execution_supervisor
        self.deployment_receipt = deployment_receipt
        self._resource_closers = list(resource_closers or [])
        self._lifecycle_guard = threading.RLock()
        self._active_operations = 0
        self._run_depth = 0
        self._run_owner_thread: Optional[int] = None
        self._closing = False
        self._shutdown_started = False
        self._closed = False
        self._pump_guard = threading.RLock()
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop: Optional[threading.Event] = None
        self._pump_error: Optional[BaseException] = None
        self._pump_owns_delivery = False
        self._pump_inbound_owned: Any = []
        self._interaction_exit_drained = False
        self._accepted_interactions_drained = False
        self._hard_stop_requested = False

    @property
    def dual_mode(self) -> str:
        """The sole implemented turn mapping; intentionally immutable."""
        return "A"

    def _assert_instance_owned(self) -> None:
        with self._lifecycle_guard:
            if self._closed:
                raise RuntimeError("System 已关闭")
            if self._closing:
                raise RuntimeError("System 正在关闭")
            if self._shutdown_started:
                raise RuntimeError("System 已进入 shutdown，只允许重试 close")
        if self.instance_lease is not None:
            self.instance_lease.assert_owned()

    @contextmanager
    def _operation_scope(self):
        self._assert_instance_owned()
        with self._lifecycle_guard:
            if self._closed:
                raise RuntimeError("System 已关闭")
            if self._closing:
                raise RuntimeError("System 正在关闭")
            if self._shutdown_started:
                raise RuntimeError("System 已进入 shutdown，只允许重试 close")
            self._active_operations += 1
        try:
            yield
        finally:
            with self._lifecycle_guard:
                self._active_operations -= 1

    def sync_notifications(self) -> None:
        with self._operation_scope():
            self._sync_notifications_cb()

    def sync_interactions(self) -> None:
        with self._operation_scope():
            self._sync_interactions_cb()

    def interaction_pending(self) -> bool:
        with self._operation_scope():
            return bool(self._interaction_pending_cb())

    def sync_accepted_interactions(self) -> None:
        with self._operation_scope():
            self._sync_accepted_interactions_cb()

    def accepted_interaction_pending(self) -> bool:
        with self._operation_scope():
            return bool(self._accepted_interaction_pending_cb())

    def sync_closed_inbound(self) -> None:
        with self._operation_scope():
            self._sync_closed_inbound_cb()

    def closed_inbound_pending(self) -> bool:
        with self._operation_scope():
            return bool(self._closed_inbound_pending_cb())

    def sync_sideband(self) -> None:
        with self._operation_scope():
            self._sync_sideband_cb()

    def raise_inbound(self) -> None:
        with self._operation_scope():
            self._raise_inbound_cb()

    @contextmanager
    def _run_scope(self, activity: str):
        thread_id = threading.get_ident()
        with self._operation_scope():
            cleanup_error: Optional[BaseException] = None
            with self._lifecycle_guard:
                if self._run_depth and self._run_owner_thread != thread_id:
                    raise RuntimeError("System 不允许并发 run；同一 owner 线程只能嵌套驱动")
                if self._run_depth == 0:
                    self._run_owner_thread = thread_id
                self._run_depth += 1
                outermost = self._run_depth == 1
            primary: Optional[BaseException] = None
            try:
                if outermost and self.instance_lease is not None:
                    self.instance_lease.set_state("running", activity=activity)
                yield
            except BaseException as error:
                primary = error
                raise
            finally:
                with self._lifecycle_guard:
                    # Keep the run capability counted until the final heartbeat
                    # transition completes.  Otherwise close() can set stopping
                    # and release the lease between depth-- and set_state(ready).
                    if (self._run_depth == 1 and not self._closing and not self._closed
                            and self.instance_lease is not None):
                        try:
                            self.instance_lease.set_state(
                                "ready", activity="resident-ready")
                        except BaseException as error:
                            cleanup_error = error
                    self._run_depth -= 1
                    if self._run_depth == 0:
                        self._run_owner_thread = None
                if cleanup_error is not None:
                    if primary is None:
                        raise cleanup_error
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "instance heartbeat 收尾失败: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}")

    def _start_interaction_pump(self, poll_interval_s: float) -> bool:
        """Start the one resident spool/completion pump; return whether this call owns it."""
        self._assert_instance_owned()
        sideband_interval = max(0.05, min(0.25, float(poll_interval_s)))
        with self._pump_guard:
            # A dead-but-uncollected thread still owns its error and delivery
            # lifecycle.  Nested run_forever→run must not overwrite that
            # evidence by silently starting a replacement.
            if self._pump_thread is not None:
                legitimate_nested = (
                    self._run_depth == 2
                    and self._run_owner_thread == threading.get_ident())
                if (not legitimate_nested or self._pump_stop is None or self._pump_stop.is_set()
                        or not self._pump_thread.is_alive()):
                    raise RuntimeError(
                        "前次 interaction pump 正在/等待清理；须先重试 System.close")
                return False
            if self._pump_inbound_owned:
                raise RuntimeError("前次 connector inbound listener 尚未完成清理")
            stop = threading.Event()
            self._pump_stop = stop
            self._pump_error = None
            try:
                inbound_owned = self._start_inbound_cb()
            except BaseException as primary:
                inbound_owned = getattr(primary, "inbound_owned", [])
                cleanup_error = None
                if inbound_owned:
                    cleanup_error = self._stop_inbound_cb(inbound_owned)
                pending_cleanup = self.inbound_cleanup_pending(
                    inbound_owned, cleanup_error)
                self._pump_inbound_owned = inbound_owned if pending_cleanup else []
                self._pump_stop = None
                if cleanup_error is not None:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"connector inbound partial-start 外层回滚失败: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}")
                raise
            self._pump_inbound_owned = inbound_owned
            delivery_owned = False
            try:
                if self.outbound_delivery is not None:
                    delivery_owned = self.outbound_delivery.start(sideband_interval)
            except BaseException as primary:
                cleanup_error = self._stop_inbound_cb(inbound_owned)
                pending_cleanup = self.inbound_cleanup_pending(
                    inbound_owned, cleanup_error)
                retry_error = None
                if pending_cleanup:
                    retry_error = self._stop_inbound_cb(inbound_owned)
                    pending_cleanup = self.inbound_cleanup_pending(
                        inbound_owned, retry_error)
                self._pump_inbound_owned = inbound_owned if pending_cleanup else []
                if cleanup_error is not None:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"connector inbound 启动回滚失败: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}")
                        if retry_error is not None:
                            add_note(
                                f"connector inbound 启动二次回滚失败: "
                                f"{type(retry_error).__name__}: {retry_error}")
                raise
            self._pump_owns_delivery = delivery_owned

            def pump() -> None:
                while not stop.is_set():
                    try:
                        self._assert_instance_owned()
                        self._sync_interactions_cb()
                        # Query completion may happen during a multi-hour research provider.  Derive and
                        # deliver its reply here instead of waiting for the next research stage boundary.
                        self._sync_sideband_cb()
                        self._raise_inbound_cb()
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
            except BaseException as primary:
                self._pump_thread = None
                self._pump_stop = None
                if delivery_owned:
                    self.outbound_delivery.stop()
                cleanup_error = self._stop_inbound_cb(inbound_owned)
                pending_cleanup = self.inbound_cleanup_pending(
                    inbound_owned, cleanup_error)
                retry_error = None
                if pending_cleanup:
                    retry_error = self._stop_inbound_cb(inbound_owned)
                    pending_cleanup = self.inbound_cleanup_pending(
                        inbound_owned, retry_error)
                self._pump_inbound_owned = inbound_owned if pending_cleanup else []
                self._pump_owns_delivery = False
                if cleanup_error is not None:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"connector inbound thread 启动回滚失败: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}")
                        if retry_error is not None:
                            add_note(
                                f"connector inbound thread 启动二次回滚失败: "
                                f"{type(retry_error).__name__}: {retry_error}")
                raise
            return True

    def _raise_resident_failure(self) -> None:
        """Surface a dead sideband/transport worker at the next safe supervisor poll."""
        self._assert_instance_owned()
        with self._pump_guard:
            error = self._pump_error
        if error is not None:
            raise error
        if self.outbound_delivery is not None:
            self.outbound_delivery.raise_if_failed()
        self.raise_inbound()

    def _interaction_pump_alive(self) -> bool:
        with self._pump_guard:
            thread = self._pump_thread
            return bool(thread is not None and thread.is_alive())

    def _stop_interaction_pump(self, *,
                               join_timeout_s: Optional[float] = None) -> Optional[BaseException]:
        with self._pump_guard:
            thread, stop = self._pump_thread, self._pump_stop
            inbound_owned = self._pump_inbound_owned
            if thread is None:
                if not inbound_owned:
                    return None
                cleanup_error = self._stop_inbound_cb(inbound_owned)
                if not self.inbound_cleanup_pending(inbound_owned, cleanup_error):
                    self._pump_inbound_owned = []
                return cleanup_error
        # Close intake and wait for bounded in-flight handlers while the DB
        # pump is still alive; then stop the pump.  This closes the race where
        # a request could append+ACK after the one final drain probe.
        inbound_error = self._stop_inbound_cb(inbound_owned)
        if stop is not None:
            stop.set()
        thread.join(timeout=join_timeout_s)
        if thread.is_alive():
            return TimeoutError(
                "interaction pump 未在 System.close deadline 内停止；保留 instance lease 供重试")
        delivery_error = None
        if self._pump_owns_delivery and self.outbound_delivery is not None:
            delivery_error = self.outbound_delivery.stop()
        pending_inbound_cleanup = self.inbound_cleanup_pending(
            inbound_owned, inbound_error)
        if inbound_error is not None and pending_inbound_cleanup:
            retry_error = self._stop_inbound_cb(inbound_owned)
            pending_inbound_cleanup = self.inbound_cleanup_pending(
                inbound_owned, retry_error)
            if retry_error is not None:
                add_note = getattr(inbound_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"connector inbound 二次清理失败: "
                        f"{type(retry_error).__name__}: {retry_error}")
        with self._pump_guard:
            error = self._pump_error
            self._pump_thread = None
            self._pump_stop = None
            self._pump_error = None
            self._pump_owns_delivery = False
            self._pump_inbound_owned = (
                inbound_owned if pending_inbound_cleanup else [])
        secondary_errors = [item for item in (inbound_error, delivery_error) if item is not None]
        if error is not None and secondary_errors:
            add_note = getattr(error, "add_note", None)
            if callable(add_note):
                for secondary in secondary_errors:
                    add_note(
                        f"connector lifecycle 失败: {type(secondary).__name__}: {secondary}")
            return error
        return error or inbound_error or delivery_error

    def flush_outbound(self) -> Dict[str, Any]:
        """Attempt one bounded priority batch, then report every item left durable."""
        with self._operation_scope():
            return self._flush_outbound_owned()

    def _flush_outbound_owned(self) -> Dict[str, Any]:
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
        with self._run_scope("run-once"):
            return self._run_owned(max_cycles)

    def _run_owned(self, max_cycles: int) -> List[str]:
        """以生产唯一的模式 A 驱动 run_cycles 到停机：每次 Runner 调用只对应一个阶段，
        阶段结果提交后才进入下一格。返回本次推进的 cycle_id。"""
        pump_owner = self._start_interaction_pump(0.05)
        if pump_owner:
            self._interaction_exit_drained = False
            self._hard_stop_requested = False
        try:
            result = self.advancer.run_cycles(max_cycles)
        except BaseException as primary:
            try:
                pump_error = self._stop_interaction_pump(
                    join_timeout_s=5.0) if pump_owner else None
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            if pump_error is not None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(f"interaction pump 失败: {type(pump_error).__name__}: {pump_error}")
            if self._interaction_pump_alive():
                raise
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
        pump_error = self._stop_interaction_pump(
            join_timeout_s=5.0) if pump_owner else None
        if pump_error is not None:
            if self._interaction_pump_alive():
                raise pump_error
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
        """Accept the finite closed-listener boundary, then terminalize its work."""
        with self._operation_scope():
            self._drain_interactions_owned(
                poll_interval_s=poll_interval_s,
                accept_final_spool=accept_final_spool)

    def _drain_interactions_owned(self, *, poll_interval_s: float = 0.1,
                                  accept_final_spool: bool = True) -> None:
        if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                or not math.isfinite(float(poll_interval_s)) or poll_interval_s < 0.01):
            raise ValueError("poll_interval_s 须为不小于 0.01 的有限秒数（防交互排空热自旋）")
        if not isinstance(accept_final_spool, bool):
            raise ValueError("accept_final_spool 须为 bool")
        # Exactly one general intake probe closes the append-during-final-provider
        # race without allowing a live, independently hosted console producer to
        # postpone shutdown forever.  Owned connector listeners have already
        # stopped at this call site, so their remaining fsynced backlog is finite
        # and is drained separately below.
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
        while accept_final_spool and self.closed_inbound_pending():
            self._retry_interaction_sync(self.sync_closed_inbound)
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
        with self._run_scope("resident-run"):
            return self._run_forever_owned(
                max_cycles, poll_interval_s=poll_interval_s,
                linger_after_terminal=linger_after_terminal, stop_event=stop_event)

    def _run_forever_owned(self, max_cycles: int, *, poll_interval_s: float = 1.0,
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
                    pump_error = self._stop_interaction_pump(join_timeout_s=5.0)
                except KeyboardInterrupt:
                    self._hard_stop_requested = True
                    raise
                if pump_error is not None:
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"interaction pump 失败: {type(pump_error).__name__}: {pump_error}")
            if self._interaction_pump_alive():
                raise
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
                pump_error = self._stop_interaction_pump(join_timeout_s=5.0)
            except KeyboardInterrupt:
                self._hard_stop_requested = True
                raise
            if pump_error is not None:
                if self._interaction_pump_alive():
                    raise pump_error
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

    def close(self) -> Optional[BaseException]:
        """Release every shared capability, with the instance flock strictly last.

        A live listener/pump/delivery worker keeps the lease held and makes the
        method retryable.  Historical worker failure is still returned for
        diagnosis, but it does not prevent release after all capabilities are
        mechanically gone; callers can distinguish that case via
        ``instance_lease.closed``.
        """
        with self._lifecycle_guard:
            if self._closed:
                return None
            if self._closing:
                return RuntimeError("System close 已在另一线程进行")
            if self._active_operations or self._run_depth:
                return RuntimeError("System 仍有 active operation，拒绝释放 instance lease")
            self._shutdown_started = True
            self._closing = True
        first_error: Optional[BaseException] = None
        try:
            if self.instance_lease is not None and not self.instance_lease.closing:
                try:
                    self.instance_lease.set_state("stopping", activity="system-close")
                except BaseException as error:
                    first_error = error

            try:
                pump_error = self._stop_interaction_pump(join_timeout_s=5.0)
            except BaseException as error:
                pump_error = error
            if first_error is None and pump_error is not None:
                first_error = pump_error

            with self._pump_guard:
                pump_thread = self._pump_thread
            if pump_thread is not None and pump_thread.is_alive():
                return first_error or RuntimeError(
                    "System interaction pump 尚未停止；instance lease 保留供重试")

            # A timeout from the pump's first delivery stop must retain/retry
            # the actual worker capability before the global owner lock moves.
            if self.outbound_delivery is not None:
                try:
                    running = bool(self.outbound_delivery.worker_running())
                except BaseException as error:
                    running = True
                    if first_error is None:
                        first_error = error
                if running:
                    try:
                        delivery_error = self.outbound_delivery.stop()
                    except BaseException as error:
                        delivery_error = error
                    if first_error is None and delivery_error is not None:
                        first_error = delivery_error

            with self._pump_guard:
                pump_thread = self._pump_thread
                inbound_pending = bool(self._pump_inbound_owned)
            delivery_running = False
            if self.outbound_delivery is not None:
                try:
                    delivery_running = bool(self.outbound_delivery.worker_running())
                except BaseException:
                    delivery_running = True
            if ((pump_thread is not None and pump_thread.is_alive())
                    or inbound_pending or delivery_running):
                return first_error or RuntimeError(
                    "System shared worker 尚未停止；instance lease 保留供重试")

            if not self._accepted_interactions_drained:
                # Public wrappers deliberately reject once shutdown begins.
                # These two private callbacks are the shutdown-only convergence
                # capability: mediator.poll() only terminalizes work already
                # accepted before listener/pump stop, and the predicate makes
                # every close retry mechanically re-enter that exact step.
                try:
                    self._sync_accepted_interactions_cb()
                    accepted_pending = bool(self._accepted_interaction_pending_cb())
                except BaseException as error:
                    accepted_pending = True
                    if first_error is None:
                        first_error = error
                if accepted_pending:
                    return first_error or RuntimeError(
                        "System 仍有 accepted interaction/query capability；instance lease 保留供重试")
                self._accepted_interactions_drained = True

            # External guardians are a shared capability just like DB workers.
            # They must be terminal, reaped and receipt-durable before any DB
            # handle closes or the instance flock can move to a new owner.
            if self.execution_supervisor is not None:
                try:
                    self.execution_supervisor.close(timeout_s=10.0)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                    return first_error

            remaining: List[Callable[[], None]] = []
            for closer in reversed(self._resource_closers):
                try:
                    closer()
                except BaseException as error:
                    remaining.append(closer)
                    if first_error is None:
                        first_error = error
            self._resource_closers = list(reversed(remaining))
            if self._resource_closers:
                return first_error or RuntimeError("System DB/resource close 未完成")

            if self.instance_lease is not None:
                lease_error = self.instance_lease.close()
                if first_error is None and lease_error is not None:
                    first_error = lease_error
                if not self.instance_lease.closed:
                    return first_error or RuntimeError("instance lease 尚未释放")
            with self._lifecycle_guard:
                self._closed = True
            return first_error
        finally:
            with self._lifecycle_guard:
                self._closing = False

    def __enter__(self) -> "System":
        self._assert_instance_owned()
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:  # noqa: ANN001
        error = self.close()
        if error is None:
            return
        if exc_type is None:
            raise error
        add_note = getattr(_exc, "add_note", None)
        if callable(add_note):
            add_note(f"System.close 失败: {type(error).__name__}: {error}")
        if not self._closed:
            try:
                _exc.orchestrator_cleanup = self
            except BaseException:
                pass

    @property
    def last_stop_reason(self) -> Optional[str]:
        return self.advancer.last_stop_reason


def build_system(system_root: str, work_root: str, *, runner_factory: Optional[Callable] = None,
                 attack=True, outbound_config: Optional[Dict[str, Any]] = None,
                 import_search_provider=None,
                 reference_snapshot_provider=None,
                 enforce_instance_lease: bool = True,
                 heartbeat_interval_s: float = 1.0) -> System:
    """装配全系统。system_root=含 input/goal_brief.md · policies/ · prompts/ · schemas/ 的仓库根；
    work_root=运行产物根（research.sqlite / cycles / state 落此）。runner_factory=注入式 Runner 工厂
    （默认真 CodexRunner；测试传 mock）。attack：True=全装（默认）；False/None=退化 reasoning-only
    （诊断用）；AttackStages 实例只允许配合 ``enforce_instance_lease=False`` 做隔离测试，生产须由本函数
    在同一 DB/work capability 下装配。``outbound_config`` 来自受限 connector profile；None
    仅用于单测/显式 ``--no-outbound``，绝不伪装成已投递。生产默认在任何 work/DB 副作用前取得
    process-wide instance lease；``enforce_instance_lease=False`` 只供隔离组件单测。
    ``import_search_provider`` 是受信只读 repo connector 注入点；None 使用受 policy
    限制的 GitHub REST provider（仅 plan 明确产搜索 sidecar 时才联网）。
    ``reference_snapshot_provider`` 是 SOTA paper/benchmark 冻结注入点；None 使用 policy host
    allowlist 内的 bounded HTTPS reader。"""
    root = Path(system_root)
    work = Path(work_root)
    policy = yaml.safe_load((root / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    schemas = SchemaSet(root / "schemas")
    schemas.validator("policy").validate(policy)    # 启动前机械校验；不能只靠 tests 校验仓库默认文件
    _reject_nonfinite_policy_numbers(policy)         # JSON Schema/Python 边界：显式拒 NaN/±Inf
    CostLedger.validate_policy(policy)               # 成本边界（float 溢出/布尔值等）也在创建 work/DB 前验完
    if set(policy["execution"]["path_allowlist"]) != set(
            policy["execution"]["sandbox"]["readonly_mounts"]):
        raise ValueError(
            "policy.execution.path_allowlist 与 sandbox.readonly_mounts 须完全一致；"
            "host 命令围栏允许的外部路径必须在容器内只读映射")
    _validate_outbound_config(policy, outbound_config)
    if not isinstance(enforce_instance_lease, bool):
        raise ValueError("enforce_instance_lease 须为 bool")
    if enforce_instance_lease and isinstance(attack, AttackStages):
        raise ValueError(
            "生产 owner lease 下拒绝注入现成 AttackStages（其 DB/work capability 无法归属本次 lease）；"
            "请用 attack=True 由 build_system 装配")
    if policy["deployment"]["mode"] == "production" and attack is not True:
        raise ValueError("production deployment 必须启用完整 attack/sandbox 装配")
    if policy["deployment"]["mode"] == "production" and not enforce_instance_lease:
        raise ValueError("production deployment 不得关闭 instance owner lease")
    lease: Optional[InstanceLease] = None
    resource_closers: List[Callable[[], None]] = []
    try:
        if enforce_instance_lease:
            lease = InstanceLease.acquire(
                work, heartbeat_interval_s=heartbeat_interval_s)
        else:
            work.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(work, 0o700)
        return _assemble_system(
            root=root, work=work, policy=policy, schemas=schemas,
            runner_factory=runner_factory, attack=attack,
            outbound_config=outbound_config, instance_lease=lease,
            import_search_provider=import_search_provider,
            reference_snapshot_provider=reference_snapshot_provider,
            resource_closers=resource_closers)
    except BaseException as primary:
        cleanup = _AssemblyCleanup(resource_closers, lease)
        cleanup_error = cleanup.close()
        cleanup_errors = [cleanup_error] if cleanup_error is not None else []
        if not cleanup.closed:
            # Never free the global owner while a failed constructor still has
            # a live DB/resource capability.  Expose a retry handle on the
            # original exception; ignoring it remains fail-closed until process
            # exit instead of permitting an overlapping replacement owner.
            try:
                primary.orchestrator_cleanup = cleanup
            except BaseException as attach_error:
                cleanup_errors.append(attach_error)
            cleanup_errors.append(RuntimeError(
                "system assembly cleanup 未完成；instance lease 已保留，"
                "可调用 error.orchestrator_cleanup.close() 重试"))
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            for error in cleanup_errors:
                add_note(
                    "system assembly cleanup 失败: "
                    f"{type(error).__name__}: {error}")
        raise


def _assemble_system(*, root: Path, work: Path, policy: Dict[str, Any],
                     schemas: SchemaSet, runner_factory: Optional[Callable],
                     attack: Any, outbound_config: Optional[Dict[str, Any]],
                     import_search_provider, reference_snapshot_provider,
                     instance_lease: Optional[InstanceLease],
                     resource_closers: List[Callable[[], None]]) -> System:
    """Assemble under an already-held owner lease; caller owns rollback."""
    owner_guard = instance_lease.assert_owned if instance_lease is not None else (lambda: None)
    # This check precedes Docker, SQLite, connectors and providers.  Once a
    # sealed target has been consumed, even read-only exposure of its metric to
    # a fresh research turn would violate the non-feedback contract.
    qualification = load_qualification_firewall(
        work, policy=policy, require_research_uid=True)
    if qualification is not None:
        if runner_factory is not None:
            raise ValueError(
                "qualification 禁止注入自定义 runner_factory；所有研究 LLM 必须使用无 host tools runner")
        if isinstance(attack, AttackStages):
            raise ValueError(
                "qualification 禁止注入预装配 AttackStages；必须由当前 contract 装配完整边界")
        qualification.assert_research_open()

    execution_owner_id = (
        instance_lease.owner_id if instance_lease is not None
        else f"unleased-{os.getpid()}-{time.time_ns()}")
    execution_supervisor = ExecutionSupervisor(
        receipt_dir=work / "state" / "executions",
        owner_id=execution_owner_id,
        owner_guard=owner_guard,
        fence_context_factory=(instance_lease.delegate_owner_fence
                               if instance_lease is not None else None))
    execution_sandbox = None
    deployment_receipt = None
    dependency_image_builder = None
    repository_materializer = None
    if attack is True:
        base_execution_sandbox = DockerExecutionSandbox(
            work_root=work, config=policy["execution"]["sandbox"],
            owner_guard=owner_guard, system_root=root,
            qualification_firewall=qualification)
        # Strong-execution prerequisites are startup capabilities, not a late
        # scientific failure.  Prove the exact local image/daemon/seccomp
        # before SQLite or any connector/provider side effect is exposed.
        base_execution_sandbox.preflight()
        deployment_preflight = DeploymentPreflight(
            work_root=work,
            policy=policy,
            sandbox=base_execution_sandbox,
            owner_id=execution_owner_id,
            attestation_validator=schemas.validator("deployment_attestation"),
            owner_guard=owner_guard,
        )
        deployment_candidate = deployment_preflight.prepare()
        # Only a deployment identity that passed the read-only trust check may
        # mutate prior-generation/session recovery state.  Both recoveries are
        # still before SQLite, connectors, providers, or new external work.
        execution_supervisor.recover_previous_generation()
        base_execution_sandbox.recover_terminal_sessions(execution_supervisor)

        gpu_contract = deployment_candidate["gpu_contract"]
        execution_sandbox = base_execution_sandbox
        canary_sandbox = None
        gpu_evidence = None
        try:
            if gpu_contract is not None:
                # Freeze a candidate rather than mutating the CPU sandbox that
                # authorized recovery.  It is promoted only after the final
                # gate replays durable guardian+inventory evidence.
                canary_sandbox = DockerExecutionSandbox(
                    work_root=work, config=policy["execution"]["sandbox"],
                    owner_guard=owner_guard, system_root=root,
                    gpu_contract=gpu_contract,
                    qualification_firewall=qualification)
                canary_sandbox.preflight()
                runtimes = (
                    deployment_candidate.get("facts", {}).get("docker", {})
                    .get("daemon", {}) or {}).get("runtimes", [])
                if isinstance(runtimes, list) and "nvidia" in runtimes:
                    gpu_evidence = canary_sandbox.run_gpu_canary(
                        execution_supervisor=execution_supervisor,
                        candidate_hash=deployment_candidate["candidate_hash"])
        except BaseException as canary_error:
            # Even a preflight/timeout/guardian exception must attempt to leave
            # an honest final false receipt before assembly aborts.  Owner loss
            # can prevent that write; it remains fail-closed and is re-raised.
            try:
                deployment_preflight.finalize(None)
            except BaseException as finalize_error:
                add_note = getattr(finalize_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "GPU canary 原始异常: "
                        f"{type(canary_error).__name__}: {canary_error}")
                raise finalize_error from canary_error
            raise
        deployment_receipt = deployment_preflight.finalize(gpu_evidence)
        check_status = {
            item.get("name"): item.get("ok") is True
            for item in deployment_receipt["checks"]
            if isinstance(item, Mapping)
        }
        gpu_access_ready = all(check_status.get(name) is True for name in (
            "sandbox_gpu_access", "docker_cgroup", "docker_resource_limits"))
        if (canary_sandbox is not None and gpu_access_ready
                and canary_sandbox.resource_mode in {"cgroup-v1", "cgroup-v2"}):
            execution_sandbox = canary_sandbox
        dependency_image_builder = PythonWheelImageBuilder(
            work_root=work,
            config=policy["import_materialization"]["dependency_image"],
            compiler=policy["import_materialization"]["compiler"],
            bootstrap_sandbox=execution_sandbox,
            execution_supervisor=execution_supervisor,
            owner_guard=owner_guard)
        repository_materializer = GitHubRepositoryMaterializer(
            work_root=work,
            config=policy["import_materialization"],
            sandbox_config=execution_sandbox.config,
            auto_license=policy["import_search"]["auto_license"],
            runtime_environment=execution_sandbox.image_environment,
            owner_guard=owner_guard,
            dependency_image_builder=dependency_image_builder)
    else:
        # Trusted reasoning-only diagnostics have no Docker deployment
        # contract, but retain the existing descendant-tree recovery fence.
        execution_supervisor.recover_previous_generation()

    expected_work_fd = open_directory_path(work, label="system work_root")
    try:
        expected_work_info = os.fstat(expected_work_fd)
    finally:
        os.close(expected_work_fd)

    def track_connection(conn):  # noqa: ANN001, ANN202 - sqlite variants share close()
        resource_closers.append(conn.close)
        return conn

    owner_guard()
    if outbound_config is not None:
        # Validate existing inbound spool/profile authority before opening or
        # creating SQLite.  A binding/config drift must not run research first
        # and discover the control plane is unusable later.
        for connector in outbound_config["channels"].values():
            if bool(getattr(connector, "has_inbound", False)):
                spool_root = getattr(connector.inbound_spool, "work_root", None)
                if spool_root is None:
                    raise ValueError("durable inbound spool 缺 work_root identity")
                spool_fd = open_directory_path(
                    spool_root, label="durable inbound connector work_root")
                try:
                    spool_info = os.fstat(spool_fd)
                finally:
                    os.close(spool_fd)
                if ((spool_info.st_dev, spool_info.st_ino)
                        != (expected_work_info.st_dev, expected_work_info.st_ino)):
                    raise ValueError(
                        "durable inbound connector work_root 与本次 instance lease 不一致")
                connector.bind_owner_guard(owner_guard)
            owner_guard()
            prepare = getattr(connector, "prepare_inbound", None)
            if callable(prepare):
                prepare()

    db_path = str(work / "research.sqlite")
    writer_conn = track_connection(_db.connect(db_path))
    os.chmod(db_path, 0o600)
    daemon = WriteDaemon(
        writer_conn, owner_guard=owner_guard)              # 新库建 / 既有库续（checksum 三重锁）
    cost_ledger = CostLedger(daemon, policy)
    # OS fence recovery above proves prior trees are drained.  Before exposing connectors,
    # providers, or any new spawn, reconcile those receipts into their exact DB intents.
    ExecutionReconciler(
        daemon, cost_ledger, execution_supervisor.receipt_dir).reconcile_startup()
    state = SQLiteStateStore(daemon, policy)
    if daemon.query_one("SELECT 1 FROM goal LIMIT 1") is None:
        # **仅首次建 goal 才解析 brief**（外审 SHOULD）：重启时 DB goal 权威——若无条件解析，缺失/畸形
        # 的 goal_brief.md 会卡死本可续跑的既有库（与「DB 权威、同 work_root 可恢复」相悖）。
        brief = parse_goal_brief(root / "input" / "goal_brief.md")
        state.create_goal(text=brief["body_md"], predicate_json=brief["predicate_json"])

    # 若上次崩在 terminal commit / ledger 越线之后、轮后 stop 落库之前，先用恢复安全的预算门补写
    # durable global_stop；随后冻结的 recovery point 才完整包含该轮应有的停机事实。
    stop = StopController(daemon, policy)
    stop.check_before_round()

    # 再补 DB terminal commit → storage receipt 的崩溃缝，之后才暴露 interaction pump、provider 或新 Runner。
    # 新库此时没有终态 cycle，但会先落原生 genesis；旧库接管会留下明确 adoption 基线而不伪造历史逐轮快照。
    # development 不把默认 production envelope（可达数十 GiB）强加给小型诊断节点；
    # 它只做「本次 backup + engineering margin」headroom 门。production 则已由新鲜
    # hard-quota attestation 证明 envelope，此处再用 statvfs 检当前物理 headroom，不把后者冒充 quota。
    capacity_source = (
        deployment_receipt.get("prerequisite", deployment_receipt)
        if (isinstance(deployment_receipt, dict)
            and deployment_receipt.get("production_ready") is True)
        else {})
    capacity_reserves = capacity_source.get("required_reserves", {})
    reserve_bytes = capacity_reserves.get("work_free_bytes", 16 * 1024 * 1024)
    reserve_inodes = capacity_reserves.get("work_free_inodes", 8)
    cycle_snapshots = CycleSnapshotPublisher(
        db_path=db_path, work_root=work, owner_guard=owner_guard,
        capacity_reserve_bytes=reserve_bytes,
        capacity_reserve_inodes=reserve_inodes)
    cycle_snapshots.reconcile(startup=True)

    # compiler/publisher 各用**只读连接**（外审 BLOCKER：单写纪律——入口层就 enforce 只读边界，
    # 防 compiler 侧误写绕过 WriteDaemon/账本/authorizer）。open_responder_read_conn = mode=ro+全写拒
    # authorizer（放行 SELECT/TRANSACTION，render/publish 的 BEGIN…COMMIT 读快照可跑）。
    compiler_conn = track_connection(open_responder_read_conn(db_path))
    publisher_conn = track_connection(open_responder_read_conn(db_path))
    compiler = SqliteCompiler(
        compiler_conn, policy,
        runtime_environment_hash=(
            execution_sandbox.environment_hash
            if execution_sandbox is not None else None))
    publisher = SqliteStatusPublisher(publisher_conn, policy=policy,
                                      out_path=str(work / "state" / "status_card.json"))
    console = Console(daemon, policy=policy)
    # sidecar 创建与控制台 resolve/cancel 共用同一服务实例；托管文件必须落在**本次 work_root** 内：
    # ①不同运行的 request_id 不会在仓库 input/ 互相覆盖；②manifest 默认 work_root 路径围栏可真实消费；
    # ③大文件/敏感文件不进入 Git 工作树。后者由 inbox ingest 在 run 单写进程内调用。
    file_requests = FileRequestService(daemon, schemas, policy, input_root=str(work / "input"))
    system_prompt = (root / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    skills = {s: (root / "prompts" / "skills" / s / "SKILL.md").read_text(encoding="utf-8") for s in _STAGES}
    base_rf = runner_factory or (lambda transcripts_dir, purpose_tag:
                                 CodexRunner(
                                     transcripts_dir=transcripts_dir,
                                     purpose_tag=purpose_tag,
                                     # Qualification workers of every stage get
                                     # only inline ContextPacks.  The existing
                                     # cross-UID query mode remains unchanged
                                     # for ordinary work roots.
                                     tool_free=(qualification is None and purpose_tag in {
                                         "interaction-query",
                                         "adapter-generation",
                                         "adapter-review",
                                     }),
                                     no_host_tools=qualification is not None,
                                     execution_supervisor=execution_supervisor))

    def rf(transcripts_dir, purpose_tag):  # noqa: ANN001, ANN202 - injection boundary
        owner_guard()
        runner = base_rf(transcripts_dir, purpose_tag)
        return _GuardedRunner(runner, owner_guard)
    if repository_materializer is not None:
        repository_materializer.bind_adapter_generator(
            AdapterGenerationService(
                runner_factory=rf, schemas=schemas, policy=policy,
                system_prompt=system_prompt,
                generation_skill=(
                    root / "prompts" / "skills" / "adapter_generation" / "SKILL.md"
                ).read_text(encoding="utf-8"),
                review_skill=(
                    root / "prompts" / "skills" / "adapter_review" / "SKILL.md"
                ).read_text(encoding="utf-8"),
                daemon=daemon, work_root=str(work), cost_ledger=cost_ledger,
                owner_guard=owner_guard))
    # 所有真 LLM 调用共用上方已完成 startup reconciliation 的同一预算/账本投影。
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
        provider_receipt_dir=str(execution_supervisor.receipt_dir),
    )
    mediator = Mediator(
        daemon, str(work / "state" / "status_card.json"),
        responder=query_responder,
        rebuild_last_n=policy["interaction"]["mediator_rebuild_last_n"],
        cost_ledger=cost_ledger,
    )
    console.continue_snapshot = mediator.continue_ack_payload
    inbox_ingest = ConsoleInboxIngest(console, mediator, str(work), file_requests=file_requests,
                                      system_root=str(root))
    connector_inbox = ConnectorInboxIngest(
        console, mediator, str(work),
        outbound_config["channels"] if outbound_config is not None else {})
    interaction_sync_lock = threading.RLock()

    def sync_interactions(cyc=None) -> int:
        # Console is always first: an authenticated remote flood/poison cannot
        # head-of-line block the local emergency pause/reject trust domain.
        with interaction_sync_lock:
            processed = inbox_ingest.ingest(cyc)
            processed += connector_inbox.ingest(cyc)
            return processed

    def interactions_pending() -> bool:
        with interaction_sync_lock:
            return bool(inbox_ingest.has_pending or connector_inbox.has_pending
                        or mediator.has_pending_queries)

    def sync_accepted_interactions() -> None:
        with interaction_sync_lock:
            mediator.poll()

    def accepted_interactions_pending() -> bool:
        with interaction_sync_lock:
            return mediator.has_pending_queries
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
            owner_guard=owner_guard,
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
        sync_interactions(cyc)                # 两个信任域独立 cursor；console 始终先推进
        if inbox_ingest.has_pending or connector_inbox.has_pending:
            # Spool 是人类动作的到达顺序。队首 retry/sidecar 损坏/下一批 backlog 未排空时，不能先消费
            # 已在 DB 的 due directive；更晚到但已 ACK 的 reject/resume 可能正卡在该入站故障之后。
            sync_notifications()               # 观测/提醒仍可重扫，但不产生任何 directive 状态效果
            return "人机入站待处理/故障（等待下轮重试）"
        reason = base_precheck(cyc)            # 再消费到期 directive + 查阻断（pause / 文件请求全局等待）
        sync_notifications()                   # 动作/消费后的真实状态立即派生通知（emit 幂等）
        return reason

    # sidecar→文件请求桥（步⑧ CP8.5）：阶段产 resource_request.json → interaction_request(pending) →
    # StageBlockedOnResources → run_cycles 干净停 → precheck 全局等待；用户 resolve 到 input/user_provided/
    # 后续跑重做该阶段。goal 版本按当下最新（goal_amend 后新请求挂新版）。
    def file_request_bridge(stage: str, request: Dict[str, Any], cyc) -> int:
        if qualification is not None:
            raise FileRequestReject(
                "qualification 从头约束禁止文件上传/外部代码资产；仅允许内联文献线索")
        gid, gver = daemon.query_one("SELECT id, version FROM goal ORDER BY version DESC LIMIT 1")
        return file_requests.create_checked(
            goal_id=gid, goal_ver=gver, stage=stage, request=request,
            cycle_id=getattr(cyc, "cycle_id", None), question_id=getattr(cyc, "question_id", None))

    provider = StageProvider(runner_factory=rf, schemas=schemas, policy=policy,
                             system_prompt=system_prompt, skills=skills, work_root=str(work),
                             file_request_bridge=file_request_bridge, cost_ledger=cost_ledger)

    attack_stages = attack if isinstance(attack, AttackStages) else None
    import_worker = None
    if attack_stages is not None:
        attack_stages.bind_owner_guard(owner_guard)
    if attack is True:
        # attack 全家（步⑧ CP8.4）：正式 gate 通道 + manifest 驱动真执行 + 真 Codex 双评审。
        # 判据读连接各司其职：gate 家族走 open_gate_read_conn（authorizer 拒观测 9 表——判据隔离）；
        # parser_suspect 须读 execution_observation → 走 open_responder_read_conn（mode=ro 全写拒，可读全表）。
        pool_conn = track_connection(open_gate_read_conn(db_path))
        obs_conn = track_connection(open_responder_read_conn(db_path))
        close_conn = track_connection(open_gate_read_conn(db_path))
        pool_gate = PoolGate(daemon, pool_conn)
        close_gate = SqliteGate(daemon, close_conn, schemas,
                                parser_suspect=lambda aid: OP.suspect_for_attempt(
                                    obs_conn, aid, policy["observation"]))
        judge = JudgeProvider(
            runner_factory=rf, schemas=schemas, policy=policy, system_prompt=system_prompt,
            skill=(root / "prompts" / "skills" / "judge" / "SKILL.md").read_text(encoding="utf-8"),
            daemon=daemon, work_root=str(work), cost_ledger=cost_ledger)
        plan_reviewer = PlanReviewProvider(
            runner_factory=rf, schemas=schemas, policy=policy,
            system_prompt=system_prompt, skill=skills["plan"], daemon=daemon,
            work_root=str(work), cost_ledger=cost_ledger)
        repo_search = (import_search_provider
                       if import_search_provider is not None
                       else GitHubRepoSearchProvider(policy["import_search"]))
        import_search = ImportSearchService(
            daemon=daemon, policy=policy, provider=repo_search,
            work_root=str(work), cost_ledger=cost_ledger,
            owner_guard=owner_guard)
        reference_snapshot = (
            reference_snapshot_provider
            if reference_snapshot_provider is not None
            else BoundedReferenceSnapshotProvider(
                policy["import_reference"]["reference_snapshot"]))
        trusted_import_triggers = TrustedImportTriggerService(
            daemon=daemon, policy=policy, repo_provider=repo_search,
            reference_provider=reference_snapshot, work_root=str(work),
            cost_ledger=cost_ledger, owner_guard=owner_guard)
        import_control = ImportTriggerRouter(
            new_structure=import_search,
            trusted_triggers=trusted_import_triggers)
        attack_stages = AttackStages(
            state=state, compiler=compiler, pool_gate=pool_gate, close_gate=close_gate,
            providers={"idea": provider.idea, "plan": provider.plan, "bundle": provider.bundle,
                       "plan_review": plan_reviewer, "judge": judge,
                       "reasoning": provider.reasoning,
                       "import_search": import_control},
            obs_policy=policy["observation"], work_root=str(work), schemas=schemas, policy=policy,
            owner_guard=(owner_guard if instance_lease is not None else None),
            execution_supervisor=execution_supervisor,
            execution_sandbox=execution_sandbox,
            execution_sandbox_resolver=dependency_image_builder,
            qualification_firewall=qualification)
        import_worker = ImportWorker(
            state=state, pool_gate=pool_gate,
            providers={
                "fetch": ProductionCandidateFetcher(
                    legacy_fetcher=FrozenCandidateFetcher(),
                    repository_fetcher=repository_materializer),
                "judge": judge,
            },
            obs_policy=policy["observation"], work_root=str(work),
            owner_guard=(owner_guard if instance_lease is not None else None),
            execution_supervisor=execution_supervisor,
            execution_sandbox=execution_sandbox,
            execution_sandbox_resolver=dependency_image_builder)

    advancer = SqliteAdvancer(state, compiler, provider.reasoning, attack=attack_stages,
                              status_publisher=publisher, precheck=precheck, stop_controller=stop,
                              import_worker=import_worker,
                              storage_reconciler=cycle_snapshots.reconcile)
    owner_guard()
    if instance_lease is not None:
        instance_lease.set_state("ready", activity="assembly-complete")
    return System(advancer=advancer, state=state, daemon=daemon,
                  dual_mode=policy["session"]["dual_mode"], work_root=work,
                  sync_notifications=sync_notifications,
                  sync_interactions=lambda: sync_interactions(None),
                  interaction_pending=interactions_pending,
                  sync_accepted_interactions=sync_accepted_interactions,
                  accepted_interaction_pending=accepted_interactions_pending,
                  sync_closed_inbound=lambda: connector_inbox.ingest(None),
                  closed_inbound_pending=lambda: connector_inbox.has_pending,
                  sync_sideband=sync_sideband_notifications,
                  outbound_delivery=delivery,
                  start_inbound=connector_inbox.start,
                  stop_inbound=connector_inbox.stop,
                  raise_inbound=connector_inbox.raise_if_failed,
                  inbound_cleanup_pending=lambda owned, _error: bool(owned),
                  instance_lease=instance_lease,
                  execution_supervisor=execution_supervisor,
                  deployment_receipt=deployment_receipt,
                  resource_closers=resource_closers)


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
            outbound_config = load_connectors(profile, work_root=args.work_root)
        except (ConnectorConfigError, OSError) as error:
            print(f"[run] connector 配置失败：{error}；离线运行须显式加 --no-outbound", file=sys.stderr)
            return 2
    try:
        system = build_system(args.system_root, args.work_root, outbound_config=outbound_config)
    except (
            InstanceLeaseError, DeploymentPreflightError,
            QualificationFirewallError) as error:
        print(f"[run] 启动预检失败：{error}", file=sys.stderr)
        return 2
    deployment_receipt = getattr(system, "deployment_receipt", None)
    if (deployment_receipt is not None
            and not deployment_receipt.get("production_ready", False)):
        print(
            "[run] development deployment：已写非生产回执；"
            "当前运行不得视为 production-ready",
            file=sys.stderr,
        )
    ids: List[str] = []
    exit_code = 0
    try:
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
            exit_code = 130
        except NotImplementedError as e:
            # 干净报（非裸 traceback）：具体缺哪个组件由异常文本自述（如 attack 退化装配缺 AttackStages、
            # 在途 import 物化轮缺 ImportWorker[CP8.6]）——文案不预设单一来源（codex NIT）
            print(f"[run] 停：续本轮需尚未装配的组件——{e}")
            exit_code = 2
        else:
            # 停因优先级（外审 SHOULD）：τ 自终止 > precheck 阻断（pause/文件请求）> 正常收尾——阻断对运维判断
            # 关键，不能被 idle 掩盖
            reason = (system.last_stop_reason or system.advancer.last_block_reason
                      or ("prior-terminate/idle" if not ids else "max_cycles/terminate"))
            print(f"[run] dual_mode={system.dual_mode} 推进 {len(ids)} 轮：{ids}；停因={reason}")
            exit_code = 0
    finally:
        try:
            close_error = system.close()
        except BaseException as error:
            close_error = error
        if close_error is not None:
            print(
                "[run] System 关闭未完全成功，instance lease 可能仍被保留："
                f"{type(close_error).__name__}: {close_error}",
                file=sys.stderr)
            if exit_code == 0:
                exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
