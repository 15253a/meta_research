# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-08 ｜ 位置：步⑤（M4）· CP5.4 已提交，CP5.5 待开工
- 检查点状态：空闲（CP5.4 记账完成；CP5.5 未开工）。测试基线 421 绿。**系统首次全链跑通**（bootstrap→attack 真训练/评估/评审/入池/真证据关问→terminate）。

## 正在做什么
**全自动模式**（用户 2026-07-07：继续实现后续所有 M、遇问题自行裁决、目标系统完整运行进入全自动）。
**步⑤（M4）进行中**：CP5.1–5.3（gates/manifest/harness+parser）+ **CP5.4（6af22cf attack 轮全链——首次全链
跑通：真训练/评估/双评审/注册入池/真证据关问；5 类崩溃缝隙修复）**已提交。**下一步 CP5.5（import 物化，
OPEN #6 落地）**。
**已完成**：步①–④（M0–M3）+ 步⑤ CP5.1–5.4。测试基线 **421 绿**。

## 步⑤（M4）目标（§7.1 M4 行）——真执行 + 真 log + import 物化
验收（可证伪）：语义判据 5 判例确定归属；证据回溯到真实 evaluation；import 全链 provenance + 失败路径负例
（license deny / smoke 失败 / factory eval 失败全拒）。开工前确认 OPEN #5/#6（reference/OPEN.md；全自动=自主裁决落受审载体）。

## M4 移交清单（M1–M3 各处裁量汇总，开工先读；同 ROADMAP 步⑤）
- **attack 轮 advance**：idea/plan/bundle 阶段推进 + phase_commit 幂等（键 (cycle,stage,target)+artifact_hash）+
  恢复扩展到 attack 阶段（M3 恢复只覆盖 reasoning-only）。M0 driver 的阶段实现（idea 双调用/plan 评审/bundle 造假）
  是流程范式，M4 换真组件重做。
- **池注册 gate_register_***：15 函数（baseline/variant/eval/attempt…，§4.1.4），M1 只做了 gate_close_question（build_log 0013）。
- **真执行 + 真 log/观测**：runner 真训练/评估；execution_log/observation 真登记（两段提交 §4.2.5(i)/(ii)）；
  **parser_result_suspect 真派生**（此前复用判定不得对真执行上线，build_log 0015；recall_sqlite 注册桩恒 0）。
- **import 物化 materialize**：占位 baseline→legal（gate_register_baseline）、license scope 消费点校验、supersession
  （届时 select_deferred 幂等守卫改判「未被 superseded 的」+ 复核 selection_key 每选择唯一，见 importer.py 注释）；
  phase_commit 级 import-defer 捆绑（三写入+set_route(dependency_wait)+Qn 释放+mark_done 同一事务，build_log 0019）。
- **compiler 接线**：检索区/引用区接 recall_sqlite（按 policy 配方，build_log 0016）；bundle 计划切片渲染（compiler_sqlite
  bundle 分支现占位）。status_card selection.latest_decision 按 cycle scope 查（advance 落 selection DECISION 后）。

## 已交付的真实组件全景（M1–M3；M0 driver 走桩栈并存、基线绿）
- database.py（冻结 DDL 三重锁）/ writedaemon.py（单写短事务）/ statestore_sqlite.py（状态机+atomic()+kill-9 安全
  +inflight/last_done_cycle）/ gate_sqlite.py（authorizer 拒 9 表+gate_close_question）/ importer.py（deferred 三写入+
  幂等守卫）/ interaction.py（durable 入站）/ compiler_sqlite.py（确定性四区包+观测摘要+applicability 徽标）/
  recall_sqlite.py（四级召回+复用 O(1)）/ status_card.py（§4.6.6 封闭字段）/ budgeting.py / advancer.py
  （derive_next_route 全矩阵+run_cycles 驱动循环+reasoning-only advance+恢复）。
- **advancer 现状**：reasoning-only（bootstrap/decompose/terminate）真环可跑、可恢复；attack route 诚实
  NotImplementedError；reasoning provider 为注入式（测试确定性替身；真 Codex provider = M4 接，范式在 M0
  driver._run_reasoning_with_retry）。

## 下一步动作（按序）—— CP5.5（import 物化，OPEN #6 落地）
M4 refsheet=`<scratch>/M4_refsheet.md` §6（session 重启后 scratchpad 清空，需重跑 Explore 提取 §3.6.3/§4.2.1）。CP5.5：
1. **物化 worker（Advancer 独立入口）**：扫 external_import(selected_for_materialization) 工作队列 → 起
   **worker cycle**（OPEN #6 裁决已定：route 终身 NULL + 开轮事务内 DECISION(orchestrator,import_worker_cycle)
   权威标记 + 不产 cycle_report + 研究驱动循环 _resume_or_open 识别 worker 标记交物化 resumer——防误当
   未 setup 研究轮）。
2. **物化链**（§3.6.3）：clone pinned revision（toy=复制固定内容文件+revision 校验）→ manifest（供应链闭包
   hash）→ 沙箱 smoke（真子进程）→ target_ready → 出厂 evaluation（真子进程+register_evaluation 复用）→
   **gate_register_baseline 把占位 baseline planned→legal**（scope 消费点核 license allow_*）→ resolve_deps
   机械 satisfied → 原问题回 open 可调度 → pool_publish（=legal，卡片侧写后续）。
3. **失败路径负例全拒**（§7.1 M4）：license deny→不物化；smoke 失败→不 target_ready；factory eval 失败→
   不 pool_publish（占位 baseline 留 planned/build_failed、dep 不 satisfied、问题保持不可调度）。
4. **两处 NotImplementedError 接**：gate_start_build_target(import kind)、gate_register_evaluation(import
   target)；select_deferred 幂等守卫的 supersession 注记核（M4 无 supersession 则不动）。
5. 照 §5 循环 → commit → build_log 0024。CP5.6 = 语义判据 5 判例 + M4 步级验证收尾。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- **全自动模式**：OPEN/裁量项不停下问用户，自行裁决 + 落受审载体（对应制品/build_log）。
- **codex 外审模式 B 后台跑**（Bash 120s 会杀，必须 run_in_background）：`env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=… codex-chatgpt exec -s read-only --skip-git-repo-check --ignore-user-config --ephemeral -m gpt-5.5 -c model_reasoning_effort=xhigh -c approval_policy=never -C <scratch> -o <out> - < <prompt>`；prompt 声明「全部内联、无需执行命令」。两轮上限；APPROVE 才 commit。**外审 diff 排除记账类**：`git diff --staged -- . ':(exclude)build_log/**' ':(exclude)implement_note.md'`。
- **评审极能抓真 bug**（M1–M3 期 codex ~12 BLOCKER/REQUEST + 内审多轮；全部两层评审意见至今 100% 采纳或有记录理由）。
- **确定性纪律**：context_pack 字节一致——无 wall-clock（含 DB created_at）/随机/dict 无序；ORDER BY 定序。恢复一致性
  比较排除 timestamp/attempt_id/log offset（advancer.py docstring）。
- gate/文件库测试须**文件库**（门禁 mode=ro 独立连接）；statestore/importer/compiler/status_card 可 :memory:。
  kill-9 测试范式：subprocess + marker 文件 + SIGKILL（test_advancer.py / test_statestore_sqlite.py）。
- DDL 字节冻结：改 schema 须同步 database.py 的 MIGRATION_SHA256（走评审）。M4 池注册若需新表/触发器 → 属决策性
  schema 变更，务必全流程评审。
- 测试基线现 **341**；端到端 `scripts/run_m0_acceptance.py --cycles 5`（花真 token，走 M0 桩栈）。
- 悬案：resolve_deps dead_end 依赖（M3/M6 定，build_log 0011）；跨版 child_answer applicability（M3 goal-amend 验，
  build_log 0012——M3 未涉 goal_amend 轮，顺延 M4/M6）；status_card M3 待填三字段（latest_decision/global_remaining/
  heartbeat_ref，build_log 0016）。
