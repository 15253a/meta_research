#!/usr/bin/env python3
"""M0 验收（第三部分 §7.1 M0 行，逐项可证伪）：固定 toy bundle 跑 3–5 轮并断言——

① 每阶段产物**逐项过 schema validator**（重新校验落盘 artifacts，不信任运行时结论）；
② 每阶段输入只来自上一阶段 contract（校验 context_pack manifest 的 sources 白名单）；
③ 不得写 M1 才存在的真实 DB 表（系统树下无任何 sqlite 文件）；
④ 驱动器造假的 evaluation/log/观测 必须标 source=fake / synthetic=true；
⑤ 只验流程契约、不验不变量（本脚本不做 I1–I6 断言——那是 M1 验收）。

用法：/root/miniconda3/bin/python scripts/run_m0_acceptance.py [--cycles 4] [--work DIR]
产出：<work>/m0_acceptance_report.md（验收证据，供 build_log 引用）。退出码 0=全过。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYSTEM_ROOT))

from orchestrator.driver import M0Driver                      # noqa: E402
from orchestrator.schemas import ARTIFACT_SCHEMA_MAP, SchemaSet   # noqa: E402

# 验收②的来源白名单：每阶段 context_pack 只许引用这些前缀。
# 分类：state:/policy:/input: = 状态与启动输入；artifact: = 已过门禁的上一阶段产物；
# staging: = 同阶段流水内的中间产物（判官看生成草稿、评审看待审 plan——§3.1.3 契约内）；
# feedback:/orchestrator: = 驱动器回传的重试反馈与失败提示（自指、无跨阶段泄漏）。
ALLOWED_SOURCE_PATTERNS = {
    "idea": [r"^state:", r"^policy:idea", r"^artifact:c\d+:idea:",
             r"^staging:idea_set\.draft\.json$", r"^feedback:artifact_parse$"],
    "plan": [r"^state:", r"^policy:", r"^artifact:c\d+:idea:",
             r"^staging:plan(_review)?\.json$", r"^feedback:artifact_parse$"],
    "bundle": [r"^artifact:c\d+:plan:plan\.json#target="],               # 假执行只消费计划切片
    "reasoning": [r"^state:", r"^policy:", r"^input:goal_brief\.md$",
                  r"^artifact:c\d+:(plan|bundle):",
                  r"^orchestrator:stage_failed_note$", r"^feedback:artifact_parse$"],
}


def check(cond: bool, msg: str, failures: list) -> None:
    if not cond:
        failures.append(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=4, help="轮数（M0 验收要求 3–5）")
    ap.add_argument("--work", type=Path, default=SYSTEM_ROOT / "questions" / "toy")
    ap.add_argument("--keep", action="store_true", help="保留已有 work 目录（默认清空重跑）")
    args = ap.parse_args()
    assert 3 <= args.cycles <= 5, "M0 验收要求 3–5 轮"

    work = args.work.resolve()
    questions_root = (SYSTEM_ROOT / "questions").resolve()
    # 破坏性操作护栏：不得用 assert（python -O 会剥掉），显式拒绝
    if not str(work).startswith(str(questions_root) + "/"):
        raise SystemExit(f"--work 必须位于 {questions_root} 之下（防误删任意目录）: {work}")
    if work.exists() and not args.keep:
        shutil.rmtree(work)
    args.work = work
    driver = M0Driver(SYSTEM_ROOT, work_root=args.work)
    done = driver.run_cycles(args.cycles)

    failures: list = []
    schemas = SchemaSet(SYSTEM_ROOT / "schemas")
    stats = {"cycles": done, "artifacts": 0, "packs": 0, "fake_targets": 0}

    # ⓪ 全轮正常收尾（cycle.failed 属未通过）+ 轮数达标（M0 字面要求 3–5 轮：提前
    #   terminate 到 <3 轮 = 主链路未走通）+ 旗舰路径必须发生（≥1 个假执行 target——
    #   否则断言④是空断言）
    for cid, cyc in driver.state.cycles.items():
        check(cyc.status == "done", f"⓪cycle {cid} 终态异常: {cyc.status}", failures)
    check(len(done) >= 3, f"⓪只跑了 {len(done)} 轮（<3：提前终止/主链路未走通）", failures)
    check(bool(list(args.work.glob("cycles/*/artifacts/*bundle_target.json"))),
          "⓪无任何 bundle target 产物（假执行旗舰路径未走到，④将成空断言）", failures)

    # ① 逐项重校验落盘产物（文件名形如 <target>.bundle_target.json 或 plan.json）
    for f in sorted(args.work.glob("cycles/*/artifacts/*.json")):
        candidates = [k for k in ARTIFACT_SCHEMA_MAP if f.name.endswith(k)]
        check(bool(candidates), f"①未知产物文件: {f}", failures)
        if not candidates:
            continue
        payload = json.loads(f.read_text(encoding="utf-8"))
        v = schemas.validator_for_artifact(max(candidates, key=len))
        errs = [f"{e.json_path} {e.message}" for e in v.iter_errors(payload)]
        check(not errs, f"①schema 不过: {f.name}: {errs[:2]}", failures)
        stats["artifacts"] += 1

    # ② manifest 来源白名单
    for mf in sorted(args.work.glob("cycles/*/context_pack/*.manifest.json")):
        m = json.loads(mf.read_text(encoding="utf-8"))
        stage = m["stage"]
        pats = ALLOWED_SOURCE_PATTERNS[stage]
        for s in m["sources"]:
            check(any(re.match(p, s) for p in pats),
                  f"②{mf.name}: 越界来源 {s}（stage={stage}）", failures)
        stats["packs"] += 1
    check(stats["packs"] > 0, "②无任何 context_pack manifest（编译器桩未留痕）", failures)

    # ③ 不建 DB
    db_files = [str(p) for p in SYSTEM_ROOT.rglob("*.sqlite*")]
    check(not db_files, f"③出现 DB 文件（M0 禁建）: {db_files}", failures)

    # ④ fake 标记
    for f in sorted(args.work.glob("cycles/*/artifacts/*bundle_target.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        if p.get("evaluation"):
            check(p["evaluation"]["source"] == "fake", f"④{f.name}: evaluation.source 非 fake", failures)
        for log in p.get("execution_logs", []):
            check(log["synthetic"] is True, f"④{f.name}: log 未标 synthetic", failures)
        obs = p.get("execution_observation")
        if obs:
            check(obs["synthetic"] is True and obs["source"] == "fake",
                  f"④{f.name}: observation 未标 fake/synthetic", failures)
        stats["fake_targets"] += 1

    # 汇报
    ok = not failures
    report = [
        "# M0 验收报告（§7.1 M0 行）",
        f"- 轮数: {len(done)}（{', '.join(done)}）｜ 终止: {driver.terminated}",
        f"- ①产物重校验: {stats['artifacts']} 份全过" if ok else "- 见失败清单",
        f"- ②输入溯源 manifest: {stats['packs']} 份全在白名单内",
        f"- ③DB 文件: 无（{len(db_files)} 个）",
        f"- ④假执行标记: {stats['fake_targets']} 个 target 全标 fake/synthetic",
        f"\n## 结论：{'通过' if ok else '未通过'}",
    ]
    if failures:
        report.append("\n## 失败清单")
        report += [f"- {x}" for x in failures]
    out = args.work / "m0_acceptance_report.md"
    out.write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    print(f"\n报告: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
