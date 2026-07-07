# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 15:45 ｜ 位置：步①（M0）CP1.4 最小驱动器 + M0 验收
- 检查点状态：开工（CP1.1 965d1a3 / CP1.2 9ee4c45 / CP1.3 7deb14a 已完成，build_log 0006–0008）

## 正在做什么
CP1.4：`orchestrator/driver.py`（M0Driver：advance 环 + route 派生 + idea 双调用 / plan 评审环 / bundle 确定性造假 + reasoning 收尾 + CycleFailed→cycle.failed+释放）+ `scripts/run_m0_acceptance.py`（验收⓪–④断言 + 报告）。**草稿已在 scratchpad `cp14/`（已同步 CP1.3 收口 API）**，移入后补两件事：①driver 的 sidecar 摘出路径（Gate 现拒 resource_request.json 直 commit：从 files 摘出→validator("resource_request") 校验→归档 cycles/<id>/interaction/→该阶段按失败观测→阶段失败收尾）；②driver 单测（用 fake runner 或直接造 files dict 测 route 派生/失败分派，不花 token）。然后**真 codex 端到端跑 M0 验收**（3–5 轮，约 25–35 次 codex 调用），报告作步①步级验证证据。

## 工作区状态
- 干净（CP1.3 与记账均已提交）。

## 下一步动作（按序，具体到命令/文件）
1. cp scratchpad/cp14/{driver.py→orchestrator/, run_m0_acceptance.py→scripts/}，补 sidecar 摘出 + 驱动器单测
2. 自验（不花 token 的单测）→ 内审（Agent model:"opus"）→ 修复
3. 端到端：`/root/miniconda3/bin/python scripts/run_m0_acceptance.py --cycles 4`（真 codex；METARESEARCH_CODEX_EFFORT 默认 medium，若质量差可调 high）→ 报告留档
4. git add → bin/codex-review.sh 外审 ≤2 轮 → commit → build_log/0009（含**步①步级验证**：M0 验收报告全文引用）+ 勾 ROADMAP 步① + 刷新本文件
5. 步①完成后：向用户汇报 M0 结果；步②（M1）开工前先问用户三件事（见下）

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- Gate 拒 sidecar 直 commit（CP1.3 定死）；driver 必须先摘出再 commit 其余 files。
- StateStore 护栏（CP1.3 外审后加）：create_root 仅 bootstrap；add_children 仅 decompose+父 active；goal_retarget 仅 goal_amend；子问题 source=decompose。driver 的轮型/route 设置顺序要配合（先 set_route 再 apply_tree_ops）。
- M1 开工前问用户：①《二》§6.12 import 旋钮 vs 附录 C 缺键；②DB evaluation.source 无 'fake' 的 M1–M3 入账方式；③OPEN #1/#2。
- pip 镜像源不带代理 / codex 要代理；测试 `cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`（101 用例基线）。
