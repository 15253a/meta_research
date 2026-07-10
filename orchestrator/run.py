"""run.py —— 全系统装配入口：一条命令把真组件 + 真 Codex 接成全自动元循环（M6 CP7.3）。

**这是「系统完整运行、进入全自动」的落点**：M0 driver 跑桩栈（验收栈）；本入口装配 M1–M5 的**真**
组件（冻结 DDL 库 / 单写 WriteDaemon / 真状态机 / 真编译器 / 真发布器 / 自终止安全网 / 人机前置检查）
+ CP7.2 StageProvider（真 CodexRunner），驱动 run_cycles 到停机（provider terminate / τ 自终止）。

**装配序**（幂等可恢复）：database.connect（新库建、既有库续，checksum 三重锁）→ WriteDaemon →
SQLiteStateStore → **查 goal 是否存在：仅首次才 parse_goal_brief + create_goal**（既有库续跑不依赖
brief 文件）→ goal_body_md 取 DB goal.text（权威）→ SqliteCompiler/StatusPublisher（各**只读连接**）→
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
import math
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
from .cost_ledger import CostLedger
from .gate_pool import PoolGate
from .gate_sqlite import SqliteGate, open_gate_read_conn
from .goalbrief import parse_goal_brief
from .mediator import Mediator, open_responder_read_conn
from .notify import (DirectiveNotifier, FileRequestNotifier, FileRequestService,
                     Outbox, make_advancer_precheck)
from .runner import CodexRunner
from .schemas import SchemaSet
from .stage_provider import JudgeProvider, StageProvider
from .statestore_sqlite import SQLiteStateStore
from .status_card import SqliteStatusPublisher
from .stopcontroller import StopController
from .writedaemon import WriteDaemon

_STAGES = ("idea", "plan", "bundle", "reasoning")


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


class System:
    """装配好的全系统句柄：run() 驱动到停机，last_stop_reason 说明为何停（观测）。"""

    def __init__(self, *, advancer: SqliteAdvancer, state: SQLiteStateStore, daemon: WriteDaemon,
                 dual_mode: str, work_root: Path, sync_notifications: Optional[Callable[[], None]] = None):
        self.advancer = advancer
        self.state = state
        self.daemon = daemon
        self.dual_mode = dual_mode
        self.work_root = work_root
        self.sync_notifications = sync_notifications or (lambda: None)

    def run(self, max_cycles: int) -> List[str]:
        """驱动 run_cycles 到停机（terminate / τ 自终止 / 阻断 / max_cycles）。返回本次推进的 cycle_id。
        reasoning-only 下模式 A≡B（每轮一阶段）——故直接 run_cycles；attack 多阶段的 A/B 分驱 = CP7.4。"""
        try:
            result = self.advancer.run_cycles(max_cycles)
        except BaseException as primary:
            # provider 可在抛 StageBlockedOnResources 前刚创建请求；异常退出也尽力补扫。但 outbox 是 DB
            # 派生物，扫描失败绝不能覆盖研究主链的 primary（否则真正损坏因会被 finally 异常遮蔽）。
            try:
                self.sync_notifications()
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
            raise
        # 正常停机时 notifier 失败仍 fail loud；调用方可修复派生 outbox 后从 DB 重扫。
        self.sync_notifications()
        return result

    def run_forever(self, max_cycles: int, *, poll_interval_s: float = 1.0) -> List[str]:
        """默认 CLI 常驻闭环：pause/file-request 阻断时保留唯一写进程并周期重跑 precheck。

        每次 poll 都会 ingest spool、消费可执行动作并扫描通知/reminder；解除阻断后从 DB 游标续同一阶段。
        ``max_cycles`` 是本次常驻会话**累计完成轮数**，不会因一次阻断后重入而重新获得预算。
        prior terminate / durable τ stop / 非阻断空闲会正常返回；Ctrl-C 由 CLI 捕获并干净退出。
        """
        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
            raise ValueError("max_cycles 须为非负整数")
        if (isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float))
                or not math.isfinite(float(poll_interval_s)) or poll_interval_s < 0.01):
            raise ValueError("poll_interval_s 须为不小于 0.01 的有限秒数（防阻断时热自旋）")
        completed: List[str] = []
        while len(completed) < max_cycles:
            batch = self.run(max_cycles - len(completed))
            completed.extend(batch)
            if len(completed) >= max_cycles or self.last_stop_reason is not None:
                break
            if self.advancer.last_block_reason is None:
                break                       # prior terminate / idle，不把非阻断空转误当常驻等待
            time.sleep(float(poll_interval_s))
        return completed

    @property
    def last_stop_reason(self) -> Optional[str]:
        return self.advancer.last_stop_reason


def build_system(system_root: str, work_root: str, *, runner_factory: Optional[Callable] = None,
                 attack=True) -> System:
    """装配全系统。system_root=含 input/goal_brief.md · policies/ · prompts/ · schemas/ 的仓库根；
    work_root=运行产物根（research.sqlite / cycles / state 落此）。runner_factory=注入式 Runner 工厂
    （默认真 CodexRunner；测试传 mock）。attack：True=全装（默认）；False/None=退化 reasoning-only
    （诊断用）；AttackStages 实例=注入自定装配（codex NIT：保留可注入性，不破外部调用方）。"""
    root = Path(system_root)
    work = Path(work_root)
    policy = yaml.safe_load((root / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    schemas = SchemaSet(root / "schemas")
    schemas.validator("policy").validate(policy)    # 启动前机械校验；不能只靠 tests 校验仓库默认文件
    _reject_nonfinite_policy_numbers(policy)         # JSON Schema/Python 边界：显式拒 NaN/±Inf
    CostLedger.validate_policy(policy)               # 成本边界（float 溢出/布尔值等）也在创建 work/DB 前验完
    work.mkdir(parents=True, exist_ok=True)

    db_path = str(work / "research.sqlite")
    daemon = WriteDaemon(_db.connect(db_path))            # 新库建 / 既有库续（checksum 三重锁）
    state = SQLiteStateStore(daemon, policy)
    if daemon.query_one("SELECT 1 FROM goal LIMIT 1") is None:
        # **仅首次建 goal 才解析 brief**（外审 SHOULD）：重启时 DB goal 权威——若无条件解析，缺失/畸形
        # 的 goal_brief.md 会卡死本可续跑的既有库（与「DB 权威、同 work_root 可恢复」相悖）。
        brief = parse_goal_brief(root / "input" / "goal_brief.md")
        state.create_goal(text=brief["body_md"], predicate_json=brief["predicate_json"])

    # **goal_body_md 以 DB goal.text 为权威**（内审 SHOULD）：重启时若 operator 改过 goal_brief.md，
    # 编译器/发布器若用新 brief 会与 DB goal_ver 绑定的目标漂移（污染 context pack「目标全文」+ 卡片
    # 摘要），绕过 goal_amend。故取 DB 里当前 goal 正文——首次运行它 == brief body，无行为变化。
    goal_body = daemon.query_one("SELECT text FROM goal WHERE id=1")[0]

    # compiler/publisher 各用**只读连接**（外审 BLOCKER：单写纪律——入口层就 enforce 只读边界，
    # 防 compiler 侧误写绕过 WriteDaemon/账本/authorizer）。open_responder_read_conn = mode=ro+全写拒
    # authorizer（放行 SELECT/TRANSACTION，render/publish 的 BEGIN…COMMIT 读快照可跑）。
    compiler = SqliteCompiler(open_responder_read_conn(db_path), policy, goal_body_md=goal_body)
    publisher = SqliteStatusPublisher(open_responder_read_conn(db_path), policy=policy,
                                      goal_body_md=goal_body, out_path=str(work / "state" / "status_card.json"))
    stop = StopController(daemon, policy)
    console = Console(daemon)
    # sidecar 创建与控制台 resolve/cancel 共用同一服务实例；托管文件必须落在**本次 work_root** 内：
    # ①不同运行的 request_id 不会在仓库 input/ 互相覆盖；②manifest 默认 work_root 路径围栏可真实消费；
    # ③大文件/敏感文件不进入 Git 工作树。后者由 inbox ingest 在 run 单写进程内调用。
    file_requests = FileRequestService(daemon, schemas, policy, input_root=str(work / "input"))
    # 步⑨ CP9.3 入站闭环：控制台命令经 console_server 落 <work>/state/console_inbox.jsonl（连接器缓冲）→
    # precheck 边界 ingest 进权威入站链（handle_inbound 落 directive/note；query 经 mediator 应答）。
    # mediator 用同一 status_card.json（publisher 阶段边界原子发布的那份）做接地卡。
    mediator = Mediator(daemon, str(work / "state" / "status_card.json"))
    inbox_ingest = ConsoleInboxIngest(console, mediator, str(work), file_requests=file_requests,
                                      system_root=str(root))
    base_precheck = make_advancer_precheck(console, daemon)
    outbox = Outbox(str(work / "state"))
    directive_notifier = DirectiveNotifier(daemon, outbox)
    file_request_notifier = FileRequestNotifier(
        daemon, outbox, policy["interaction_request"]["remind_interval_h"])

    def sync_notifications() -> None:
        directive_notifier.scan()
        file_request_notifier.scan(time.time())

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

    system_prompt = (root / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    skills = {s: (root / "prompts" / "skills" / s / "SKILL.md").read_text(encoding="utf-8") for s in _STAGES}
    rf = runner_factory or (lambda transcripts_dir, purpose_tag:
                            CodexRunner(transcripts_dir=transcripts_dir, purpose_tag=purpose_tag))

    # sidecar→文件请求桥（步⑧ CP8.5）：阶段产 resource_request.json → interaction_request(pending) →
    # StageBlockedOnResources → run_cycles 干净停 → precheck 全局等待；用户 resolve 到 input/user_provided/
    # 后续跑重做该阶段。goal 版本按当下最新（goal_amend 后新请求挂新版）。
    def file_request_bridge(stage: str, request: Dict[str, Any], cyc) -> int:
        gid, gver = daemon.query_one("SELECT id, version FROM goal ORDER BY version DESC LIMIT 1")
        return file_requests.create_checked(
            goal_id=gid, goal_ver=gver, stage=stage, request=request,
            cycle_id=getattr(cyc, "cycle_id", None), question_id=getattr(cyc, "question_id", None))

    cost_ledger = CostLedger(daemon, policy)     # 步⑩ CP10.2：LLM 调用成本记账（激活 budget_exhausted 安全网）
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
                  sync_notifications=sync_notifications)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="meta-research 全自动元循环入口（M6 CP7.3；reasoning-only 闭环）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 input/policies/prompts/schemas）")
    ap.add_argument("--work-root", required=True, help="运行产物根（research.sqlite 落此，重启同目录即续跑）")
    ap.add_argument("--max-cycles", type=int, default=100, help="本次最多推进轮数（安全上限，与 τ 自终止并存）")
    ap.add_argument("--once", action="store_true",
                    help="一次性模式：遇 pause/文件请求即返回；默认保持 run 单写进程常驻等待并自动续跑")
    ap.add_argument("--poll-interval-s", type=float, default=1.0,
                    help="常驻等待时 ingest spool / 扫描 reminder 的轮询秒数（默认 1.0）")
    args = ap.parse_args(argv)
    system = build_system(args.system_root, args.work_root)
    try:
        ids = (system.run(args.max_cycles) if args.once else
               system.run_forever(args.max_cycles, poll_interval_s=args.poll_interval_s))
    except KeyboardInterrupt:
        try:
            system.sync_notifications()
        except Exception as e:               # 派生通知失败也不把 Ctrl-C 变 traceback
            print(f"[run] Ctrl-C 退出前通知扫描失败：{e}")
        print("[run] 收到 Ctrl-C，已停止单写循环")
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
