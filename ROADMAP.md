# ROADMAP —— 构建路线图（活文档）

> 本文件是 CLAUDE.md §0「构建模型」的落地载体，**随进度更新**（不是一次写死）。
> 两级分解：
> - **步（Level 1，用户给 + 验证方法）= 不动主线**；
> - **检查点（Level 2，模型切到 superpowers 稳定长度）= 一次对外提交 + 一条 build_log**。
>
> 规则：做到哪一步，才把那步切成检查点（后面步依赖前面步的产物，不必一次列全）。
> 每个检查点走：内部 superpowers（含子代理审核）→ 边界 codex 外审（≤2 轮）→ 检查点提交 → build_log + 回此勾选 + 刷新 `implement_note.md`。
> 施工现场快照（当下在哪、下一步动作）见仓库根 `implement_note.md`（CLAUDE.md §9），新 session 开工先读它。

## 当前位置

> 本节是**故意保留的冗余兜底**（`implement_note.md` 覆盖式更新、有误写/丢失风险，此处粗粒度、入 git 可恢复）：
> **只在每个检查点记账时同步**；实时现场以 `implement_note.md` 为准，检查点进行中本节落后属正常（CLAUDE.md §9）。

- 总目标：按 `reference/`（三部分施工标准 + 流程图）在 `meta-research/` 实现 meta-research 元循环系统，最终**能正确运行**（M0–M6 逐里程碑验收）。
- 进行中的步 / 检查点：步①（M0）· CP1.4 最小驱动器 + M0 验收（CP1.1 965d1a3 / CP1.2 9ee4c45 / CP1.3 7deb14a 已完成）。

## 步与检查点

> 步 = reference/第三部分 §7.1 的里程碑 M0–M6（用户指定 reference/ 为施工标准，验收原文以该文件为唯一权威；下面只摘要判据，施工时以原文逐项核）。
> 运行前提（§7.5）：#1 codex CLI Runner 冒烟 ✅（2026-07-07 实测 `codex-chatgpt exec` EXIT=0）；#2 goal_brief / #4 policy 默认值随步①落地；#3/#5–#7 在 M4/M5 前就位。
> OPEN 项（reference/OPEN.md）：推进到对应里程碑必须停下向用户确认，不得自行填空——#1/#2 阻塞 M1，#3 阻塞 M3，#5/#6 阻塞 M4，#4 阻塞 M6。

### 步①（M0）流程层骨架 + 资产层接口桩 + 最小驱动器
- 验证方法（§7.1 M0 行，可证伪）：固定 toy bundle 跑 3–5 轮——①每阶段产物**逐项过 schema validator**；②每阶段输入只来自上一阶段 contract；③**不得写 M1 才存在的真实 DB 表**；④驱动器造假的 evaluation / execution_log / observation 必须标 `source=fake` / synthetic；⑤只验流程契约、不验不变量。
- 状态：进行中
- 检查点（模型切）：
  - [x] CP1.1 契约层：`meta-research/` 目录骨架（§6.3）+ `schemas/` 四阶段产物 JSON Schema + sidecar（§6.11）+ `policies/policy.yaml` 全量默认（附录 C）+ `orchestrator/interfaces.py`（§6.10 Protocol 缝）+ toy `input/goal_brief.md`（§4.6.7 契约） — commit 965d1a3（build_log 0006）
  - [x] CP1.2 流程层：`prompts/system_prompt.md` + 四阶段 `SKILL.md`（全中文；idea NEED 分支 / plan 复用判定+可回答性评审 / bundle KIND 分支+双评审桩 / reasoning R1–R4，按流程图 02–05） — commit 9ee4c45（build_log 0007）
  - [x] CP1.3 资产层接口桩：Gate（schema+引用真校验、业务门禁放过）/ StateStore（内存）/ Compiler·Ctx·Recall（固定模板/假数据）/ Runner（真 `codex exec`）+ validate_artifact — commit 7deb14a（build_log 0008）
  - [ ] CP1.4 最小驱动器 + M0 验收：advance 最小推进 + route 派生骨架 + bundle 造假 evaluation（标 fake）+ toy 3–5 轮端到端 + 验收断言脚本 — commit <hash>
- 步级验证结果：（CP1.4 完成后跑验证方法，记 build_log）

### 步②（M1）资产层落地（分层验收 M1a/M1b/M1c）
- 验证方法（§7.1 M1 行）：M1a 附录 A DDL 建库（36 表+72 触发器+29 索引+1 视图，migration/checksum 锁定）+ 门禁 + 三级校验 → 不变量 I1–I6 + v2.3/v2.4 否定用例全过；M1b StateStore 落 SQLite + decompose 释放断言（kill -9 无半写）；M1c v2.3/v2.4 表只做约束 + 「M1–M3 隔离拒绝用例」。开工前先向用户确认 OPEN #1/#2。
- 状态：未开始；检查点待步①完成后切

### 步③（M2）上下文编译器 + 召回 + 观测摘要 + status_card
- 验证方法（§7.1 M2 行）：同快照+配方+预算→字节一致（diff=0）；召回四级可停；复用判定 O(1)（EXPLAIN QUERY PLAN 证明走测量索引）；观测摘要进锚点而门禁 authorizer 拒读（负例）；import 仍 deferred、不产 target。
- 状态：未开始

### 步④（M3）编排器 Advancer + 恢复 + import M1–3 降级
- 验证方法（§7.1 M3 行）：任意阶段 kill -9 重启续跑，终库状态与不杀一致（排除非确定字段）；import deferred 隔离断言（pending dep 排除调度、不重复登记、不产 target）。开工前确认 OPEN #3。
- 状态：未开始

### 步⑤（M4）真执行 + 真 log + import 物化
- 验证方法（§7.1 M4 行）：语义判据 5 判例确定归属；证据回溯到真实 evaluation；import 全链 provenance + 失败路径负例（license deny / smoke 失败 / factory eval 失败全拒）。开工前确认 OPEN #5/#6。
- 状态：未开始

### 步⑥（M5）人类控制台 + query 只读应答器
- 验证方法（§7.1 M5 行）：directive 按时机消费同记 DECISION；query 只读边界负例（responder 写库全被 authorizer 拒）；分类负例（unclear 不自动答）；ACK/query p95<2s；中介线程重建一致；润色≠raw；通知矩阵逐态推送断言；文件请求全流水 + 负例。
- 状态：未开始

### 步⑦（M6）长跑 + 验收剧本
- 验证方法（§7.1 M6 行 + §7.3/§7.4）：数百轮无人值守不漂移；双模式 A/B 实测定默认；§7.3 机制剧本（happy+失败路径）与 §7.4 研究能力任务（T1/T2）全通。开工前确认 OPEN #4。
- 状态：未开始
