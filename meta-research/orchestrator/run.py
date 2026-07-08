"""run.py —— 全系统装配入口：一条命令把真组件 + 真 Codex 接成全自动元循环（M6 CP7.3）。

**这是「系统完整运行、进入全自动」的落点**：M0 driver 跑桩栈（验收栈）；本入口装配 M1–M5 的**真**
组件（冻结 DDL 库 / 单写 WriteDaemon / 真状态机 / 真编译器 / 真发布器 / 自终止安全网 / 人机前置检查）
+ CP7.2 StageProvider（真 CodexRunner），驱动 run_cycles 到停机（provider terminate / τ 自终止）。

**装配序**（幂等可恢复）：database.connect（新库建、既有库续，checksum 三重锁）→ WriteDaemon →
SQLiteStateStore → **查 goal 是否存在：仅首次才 parse_goal_brief + create_goal**（既有库续跑不依赖
brief 文件）→ goal_body_md 取 DB goal.text（权威）→ SqliteCompiler/StatusPublisher（各**只读连接**）→
StopController + Console + make_advancer_precheck → StageProvider(CodexRunner) → SqliteAdvancer(全注入)。
重启同 work_dir 即续跑（状态在 DB，非进程内）——kill-9 恢复同 M3。

**M6 范围**：本入口落 **reasoning-only 全自动闭环**（bootstrap→decompose→terminate/τ 自停）——这是
「系统能全自动跑起来」的最小完整证据。**attack 轮**需 idea/plan/judge provider 全接（CP7.4：judge
写库形态 + idea_set schema↔attack_stages 校准），故本入口 attack=None（遇 attack 续轮由既有
NotImplementedError 明确拒，不静默）。

**双模式 A/B**（policy.session.dual_mode）：模式 A=一 turn 一阶段、模式 B=一 turn 跨多阶段。**对
reasoning-only 轮二者等价**（bootstrap/decompose 每轮恰一阶段=一 turn），差异只在 attack 多阶段轮
显现 → 真 A/B 驱动随 attack 落 CP7.4。本入口读并记录 dual_mode，reasoning-only 下走 run_cycles。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import database as _db
from .advancer import SqliteAdvancer
from .compiler_sqlite import SqliteCompiler
from .console import Console
from .goalbrief import parse_goal_brief
from .mediator import open_responder_read_conn
from .notify import make_advancer_precheck
from .runner import CodexRunner
from .schemas import SchemaSet
from .stage_provider import StageProvider
from .statestore_sqlite import SQLiteStateStore
from .status_card import SqliteStatusPublisher
from .stopcontroller import StopController
from .writedaemon import WriteDaemon

_STAGES = ("idea", "plan", "reasoning")


class System:
    """装配好的全系统句柄：run() 驱动到停机，last_stop_reason 说明为何停（观测）。"""

    def __init__(self, *, advancer: SqliteAdvancer, state: SQLiteStateStore, daemon: WriteDaemon,
                 dual_mode: str, work_root: Path):
        self.advancer = advancer
        self.state = state
        self.daemon = daemon
        self.dual_mode = dual_mode
        self.work_root = work_root

    def run(self, max_cycles: int) -> List[str]:
        """驱动 run_cycles 到停机（terminate / τ 自终止 / 阻断 / max_cycles）。返回本次推进的 cycle_id。
        reasoning-only 下模式 A≡B（每轮一阶段）——故直接 run_cycles；attack 多阶段的 A/B 分驱 = CP7.4。"""
        return self.advancer.run_cycles(max_cycles)

    @property
    def last_stop_reason(self) -> Optional[str]:
        return self.advancer.last_stop_reason


def build_system(system_root: str, work_root: str, *, runner_factory: Optional[Callable] = None,
                 attack=None) -> System:
    """装配全系统。system_root=含 input/goal_brief.md · policies/ · prompts/ · schemas/ 的仓库根；
    work_root=运行产物根（research.sqlite / cycles / state 落此）。runner_factory=注入式 Runner 工厂
    （默认真 CodexRunner；测试传 mock）。attack=AttackStages 或 None（None=reasoning-only，CP7.4 接真）。"""
    root = Path(system_root)
    work = Path(work_root)
    work.mkdir(parents=True, exist_ok=True)
    policy = yaml.safe_load((root / "policies" / "policy.yaml").read_text(encoding="utf-8"))

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
    precheck = make_advancer_precheck(console, daemon)

    schemas = SchemaSet(root / "schemas")
    system_prompt = (root / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    skills = {s: (root / "prompts" / "skills" / s / "SKILL.md").read_text(encoding="utf-8") for s in _STAGES}
    rf = runner_factory or (lambda transcripts_dir, purpose_tag:
                            CodexRunner(transcripts_dir=transcripts_dir, purpose_tag=purpose_tag))
    provider = StageProvider(runner_factory=rf, schemas=schemas, policy=policy,
                             system_prompt=system_prompt, skills=skills, work_root=str(work))

    advancer = SqliteAdvancer(state, compiler, provider.reasoning, attack=attack,
                              status_publisher=publisher, precheck=precheck, stop_controller=stop)
    return System(advancer=advancer, state=state, daemon=daemon,
                  dual_mode=policy.get("session", {}).get("dual_mode", "A"), work_root=work)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="meta-research 全自动元循环入口（M6 CP7.3；reasoning-only 闭环）")
    ap.add_argument("--system-root", required=True, help="仓库根（含 input/policies/prompts/schemas）")
    ap.add_argument("--work-root", required=True, help="运行产物根（research.sqlite 落此，重启同目录即续跑）")
    ap.add_argument("--max-cycles", type=int, default=100, help="本次最多推进轮数（安全上限，与 τ 自终止并存）")
    args = ap.parse_args(argv)
    system = build_system(args.system_root, args.work_root)
    try:
        ids = system.run(args.max_cycles)
    except NotImplementedError as e:
        # attack 续轮需 CP7.4 装配 judge/idea/plan provider（本入口 reasoning-only）——干净报，非裸 traceback
        print(f"[run] 停：需 CP7.4 装配 attack provider 才能续本轮——{e}")
        return 2
    # 停因优先级（外审 SHOULD）：τ 自终止 > precheck 阻断（pause/文件请求）> 正常收尾——阻断对运维判断
    # 关键，不能被 idle 掩盖
    reason = (system.last_stop_reason or system.advancer.last_block_reason
              or ("prior-terminate/idle" if not ids else "max_cycles/terminate"))
    print(f"[run] dual_mode={system.dual_mode} 推进 {len(ids)} 轮：{ids}；停因={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
