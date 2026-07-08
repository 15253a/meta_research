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
- 进行中的步 / 检查点：**步④（M3）已完成**（CP4.1 148c907 + CP4.2 ff30463 + CP4.3 6dd2387；§7.1 M3 两判据步级验证全过，见 build_log 0019）。**下一步 步⑤（M4）真执行 + 真 log + import 物化**。步①（M0）、步②（M1）、步③（M2）已完成。
  - 用户 2026-07-07 授权**全自动模式**：OPEN 项不再停下问用户，自主裁决并落受审载体（记 build_log）。M1 三 OPEN 裁定已落 `meta-research/db/README.md`。

## 步与检查点

> 步 = reference/第三部分 §7.1 的里程碑 M0–M6（用户指定 reference/ 为施工标准，验收原文以该文件为唯一权威；下面只摘要判据，施工时以原文逐项核）。
> 运行前提（§7.5）：#1 codex CLI Runner 冒烟 ✅（2026-07-07 实测 `codex-chatgpt exec` EXIT=0）；#2 goal_brief / #4 policy 默认值随步①落地；#3/#5–#7 在 M4/M5 前就位。
> OPEN 项（reference/OPEN.md）：推进到对应里程碑必须停下向用户确认，不得自行填空——#1/#2 阻塞 M1，#3 阻塞 M3，#5/#6 阻塞 M4，#4 阻塞 M6。

### 步①（M0）流程层骨架 + 资产层接口桩 + 最小驱动器
- 验证方法（§7.1 M0 行，可证伪）：固定 toy bundle 跑 3–5 轮——①每阶段产物**逐项过 schema validator**；②每阶段输入只来自上一阶段 contract；③**不得写 M1 才存在的真实 DB 表**；④驱动器造假的 evaluation / execution_log / observation 必须标 `source=fake` / synthetic；⑤只验流程契约、不验不变量。
- 状态：**已完成**（2026-07-07）
- 检查点（模型切）：
  - [x] CP1.1 契约层：`meta-research/` 目录骨架（§6.3）+ `schemas/` 四阶段产物 JSON Schema + sidecar（§6.11）+ `policies/policy.yaml` 全量默认（附录 C）+ `orchestrator/interfaces.py`（§6.10 Protocol 缝）+ toy `input/goal_brief.md`（§4.6.7 契约） — commit 965d1a3（build_log 0006）
  - [x] CP1.2 流程层：`prompts/system_prompt.md` + 四阶段 `SKILL.md`（全中文；idea NEED 分支 / plan 复用判定+可回答性评审 / bundle KIND 分支+双评审桩 / reasoning R1–R4，按流程图 02–05） — commit 9ee4c45（build_log 0007）
  - [x] CP1.3 资产层接口桩：Gate（schema+引用真校验、业务门禁放过）/ StateStore（内存）/ Compiler·Ctx·Recall（固定模板/假数据）/ Runner（真 `codex exec`）+ validate_artifact — commit 7deb14a（build_log 0008）
  - [x] CP1.4 最小驱动器 + M0 验收：advance 最小推进 + route 派生骨架 + bundle 造假 evaluation（标 fake）+ toy 5 轮端到端 + 验收断言脚本 — commit 45641cc（build_log 0009）
- 步级验证结果：**通过**（真 codex 端到端 `scripts/run_m0_acceptance.py --cycles 5` exit=0：①27 份产物逐项过 schema validator ②29 份 context_pack manifest 输入来源全在白名单 ③未建任何 DB 文件 ④8 个假执行 target 全标 `source=fake`/`synthetic=true` ⑤只验流程契约。证据全文见 build_log/0009。）

### 步②（M1）资产层落地（分层验收 M1a/M1b/M1c）
- 验证方法（§7.1 M1 行）：M1a 附录 A DDL 建库（36 表+72 触发器+29 索引+1 视图，migration/checksum 锁定）+ 门禁 + 三级校验 → 不变量 I1–I6 + v2.3/v2.4 否定用例全过；M1b StateStore 落 SQLite + decompose 释放断言（kill -9 无半写）；M1c v2.3/v2.4 表只做约束 + 「M1–M3 隔离拒绝用例」。OPEN #1/#2 已在全自动模式下自主裁决（落 db/README.md）。
- 状态：**已完成**（步级验证通过，见下；此前漏改状态行，2026-07-08 补正）
- 检查点（模型切，边走边补）：
  - [x] CP2.1 冻结 Appendix-A DDL 落库 + database.py 三重锁 + 全套 **DB 层**不变量否定用例（I1–I6/append-only/v2.3-2.4 表约束，91 例）— commit 6d45b53（build_log 0010）
  - [x] CP2.2 单写 WriteDaemon（短事务）+ SQLiteStateStore 落 SQLite（cycle/question/route/树/dep/selection，语义等价 M0 InMemory）+ decompose 单事务原子性（kill-9 无半写）（M1b）— commit be84a90（build_log 0011）
  - [x] CP2.3 SqliteGate（M1a-Gate）：authorizer 隔离拒 9 表+v_trajectory + gate_input_* TEMP 视图 + 三级校验 + gate_close_question（gate-only 判据写锁内重跑防 TOCTOU + 触发器兜底转干净拒）— commit d07c6c6（build_log 0012）
  - [x] CP2.4 v2.3/v2.4 表隔离行为 + 「M1–M3 隔离拒绝用例」（M1c）— commit 1182a5a（build_log 0013）
- 步级验证结果：**通过**（`pytest tests/ -q` = 267 全绿；§7.1 M1 逐项↔用例映射见 build_log/0013：DDL/checksum 锁 + I1–I6 + v2.3/v2.4 否定 + 门禁三级校验/gate_close_question + StateStore/decompose kill-9 + M1–M3 隔离 全过）。
  - > 注：M1a-Gate 本轮只 gate_close_question；池注册 gate_*（baseline/variant/eval/attempt… 15 个，§4.1.4）随 M3/M4 真执行需要时补（M1–M3 假执行不产真池对象，隔离用例证之）。parser_suspect=M4；reasoning-finish 全序原子=M3。
  - > 注：**顺序调整**——StateStore/WriteDaemon（M1b）先落（自足 + 供 Gate 共用同一写服务，§6.6），Gate（M1a-Gate）后落。二者共用单写 WriteDaemon；M0 driver 端到端切真 loop 归 M3 Advancer（M1 交付真实组件 + 组件级验收）。切分可随进度调整。

### 步③（M2）上下文编译器 + 召回 + 观测摘要 + status_card
- 验证方法（§7.1 M2 行）：同快照+配方+预算→字节一致（diff=0）；召回四级可停；复用判定 O(1)（EXPLAIN QUERY PLAN 证明走测量索引）；观测摘要进锚点而门禁 authorizer 拒读（负例）；import 仍 deferred、不产 target。
- 状态：**已完成**（§7.1 M2 五判据步级验证全过，316 绿，见 build_log 0016）
- 检查点（模型切，边走边补）：
  - [x] CP3.1 SqliteCompiler：DB→确定性四区 context_pack（**字节一致 diff=0**，render 单读快照 + ORDER BY 定序）+ applicability 六枚举徽标 + manifest 纯函数 — commit 2110f02（build_log 0014）
  - [x] CP3.2 Recall 四级可停（卡片 LIKE+faceted tag / 变体矩阵 / 测量索引 / ctx-fetch）+ **复用判定 O(1) selector（§4.1.5）+ EXPLAIN QUERY PLAN 证走测量索引（e/mr 均走索引、无全表扫）** + Ctx 深潜 — commit 1a099fc（build_log 0015）
  - [x] CP3.3 观测摘要段进 reasoning 锚点（§4.7，从 execution_observation 渲机器事实、不渲 created_at）+ status_card 封闭字段构建器（§4.6.6）+ authorizer 拒读负例（观测进 pack 不进 gate）+ import deferred 不产 target（既有 test_isolation_m1c 覆盖）— commit 72647f8（build_log 0016）
  - > 注：架构同 M1——M2 交付真实 DB-backed 组件（读 DB），M0 driver 仍走桩、基线绿；M3 Advancer 接。嵌入语义召回 defer（无模型，用 card_md LIKE+baseline_tag+测量索引）。

### 步④（M3）编排器 Advancer + 恢复 + import M1–3 降级
- 验证方法（§7.1 M3 行）：任意阶段 kill -9 重启续跑，终库状态与不杀一致（排除非确定字段）；import deferred 隔离断言（pending dep 排除调度、不重复登记、不产 target）。开工前确认 OPEN #3（全自动模式：自主裁决 + 落受审载体）。
- 状态：**已完成**（§7.1 M3 两判据 10 测步级验证全过，341 绿，见 build_log 0019。恢复覆盖 reasoning-only 轮；attack 阶段恢复扩展 = M4，见步④裁量）
- **裁量（全自动，2026-07-08）·M3/M4 边界**：M3 交付真 Advancer（状态机步进 + 恢复 + import 隔离），操作真
  SQLiteStateStore/SqliteGate/SqliteCompiler（与 M0 桩 driver 并存不替换、M0 基线绿）。**pool 注册类 gate_register_\***
  （M1 未做，build_log 0013）与**真执行**归 **M4**——M3 的 attack 全环收尾依赖它们，故 M3 聚焦其验收所需的
  「状态机可恢复 + import 隔离」，attack 全环入池留 M4。理由：M3 §7.1 验收=恢复+import 隔离，均不需入池。
- 检查点（模型切，边走边补）:
  - [x] CP4.1 `derive_next_route`（§6.13(3) 全矩阵，纯函数，fail-closed）+ Advancer 骨架（advance 读 cycle.status 续跑 +
    state.atomic() 裹阶段短写 + 事务内二次核终态 TOCTOU 幂等）+ 驱动 bootstrap 创世轮真 SQLite 全环（注入确定性产物）
    — commit 148c907（build_log 0017）。注：decompose/attack route + 外层驱动循环留 CP4.2
  - [x] CP4.2 外层驱动循环 run_cycles（开轮/据上轮 selection 派 route/激活目标/loop advance；durable 交接、无进程内记忆）
    + decompose advance + **恢复**：真 kill -9 重启续跑终库状态与不杀一致（排除 timestamp/attempt_id/log offset；subprocess
    杀进程测）— commit ff30463（build_log 0018）。范围 reasoning-only；attack 阶段恢复（池注册 gate_register_* + 真执行）= M4。
  - [x] CP4.3 import deferred「不重复登记」：select_deferred 幂等守卫（真重放返回既有三元；candidate/license/dep/重复
    四道 fail-loud）— commit 6dd2387（build_log 0019）。范围：importer 函数级幂等；phase_commit 级捆绑（三写入+
    set_route(dependency_wait)+Qn 释放+mark_done 同事务）随 dependency_wait plan 阶段接入 = M4。其余隔离断言
    （pending dep 排除调度/不产 target/三写入原子）CP2.4 已覆盖
  - > 注：M0 driver（走桩+真 Codex）保留为 M0 验收栈；M3 Advancer 是真组件上的可恢复步进器。

### 步⑤（M4）真执行 + 真 log + import 物化
- 验证方法（§7.1 M4 行）：语义判据 5 判例确定归属（①自建 ②import factory ③复用零重训 ④训练失败入账不入树 ⑤log suspect 不成证据）；证据可回溯到一次真实 evaluation；imported 经本系统 harness 出 factory evidence、report 带 provenance；import 失败路径负例（license deny→不物化 / smoke 失败→不 target_ready / factory eval 失败→不 pool_publish，全拒）。
- 状态：**进行中**
- **OPEN #5 裁决（全自动，2026-07-08）**：policy.yaml 补 `observation` 节（§4.7 声明的 parser 阈值旋钮：nan / divergence /
  loss_trend → parser_result_suspect），随真 parser 落地（CP5.3）一并成文走评审；`extraction_policy_hash` = 该节规范化
  JSON 的 sha256（同 log + 同 parser_version + 同 policy → 同 observation，P6 可回放）。
- **OPEN #6 裁决（全自动，2026-07-08，CP5.5 落地时随 codex 复核）**：物化 worker cycle 表达 =
  ① `cycle.route` **终身 NULL**（§2.3 七研究形态封闭不扩；NULL=非研究轮）；
  ② **权威标记** = 开轮同一事务写 `decision(actor='orchestrator', type='import_worker_cycle', cycle_id, payload={external_import_id})`
  （durable，恢复可识别——防研究驱动循环把在途 worker 轮误当「未 setup 研究轮」去 _setup_cycle）；
  ③ **收尾** = mark_cycle_done(done/failed)，**不产 cycle_report**（非研究轮；审计 = external_import 事件链 + run(kind=import)
  + execution_log + 开轮决策行）；
  ④ **恢复** = 研究驱动循环 `_resume_or_open` 遇 route=NULL 且带 worker 标记的在途轮 → 交物化 resumer 续跑、不走研究 setup。
- 检查点（模型切，边走边补）:
  - [x] CP5.1 执行生命周期 gates（九函数，§4.1.4 判据）+ review_passed 双评审机械判据（subject_hash 当下重算 +
    runner_call 核 + 畸形 fail-closed）+ 否定用例 28（连坐×kind 守卫/I2/I6/串行结构推导/腐化态）— commit 7d64ec5
    （build_log 0020）。import 目标生命周期 defer CP5.5
  - [x] CP5.2 注册/评审 gates（claim/register baseline·variant + **register_evaluation=§4.2.5(ii) 单事务注册入口** +
    new_protocol I1 全口径）+ subject manifest 确定性构造 + **target↔variant/kind 绑定核（NULL 不作通配）** +
    20 否定/全链用例 — commit 439d716（build_log 0021）。注册段整体单事务组合器 = CP5.4；import 两处 defer CP5.5
  - [x] CP5.3 真执行 harness（真子进程 + staging .partial→原子改名 + execution_log 幂等入账）+ 确定性 parser
    （content_hash 锚校验 + P6 复算）+ **parser_result_suspect 真派生**（当前 (version,policy_hash) 过滤 +
    stale≠clean fail-closed + 多 log OR；替 M2 桩，复用判定/关问证据拒接真）+ **policy observation 节
    （OPEN #5 落地闭）** — commit 215c694（build_log 0022）
  - [x] CP5.4 attack 轮 advance 全链**（首次全链跑通）**：idea/plan 单事务阶段 + bundle 逐目标两段提交（真子进程
    训练/评估 + 双评审 + 注册入池）+ phase_commit 幂等 + 结构恢复（5 类崩溃缝隙楔死/洗白/分裂全修：补登幂等锚校验、
    eval-final 续注册 exit 侧车、judge replay-safe、reasoning 产物持久化、终态早退落 pc）+ reasoning 真证据关问 —
    commit 6af22cf（build_log 0023）。M5/M6 硬化：注册段合一事务、route plan 后特化
  - [x] CP5.5 import 物化 ImportWorker（**OPEN #6 落地闭**：worker cycle route 终身 NULL+同事务标记+无条件探测
    fail-loud；scope 消费点→clone 卫生→供应链 manifest→真 smoke→适配评审→import run+checkpoint 五件套
    provenance→出厂 eval[source 仍 factory]→占位→legal→imported 事件→resolve_deps 解锁问题）+ **失败路径
    全拒含 judge FAIL**（settling 不死循环；attack 侧 lockstep 同修）+ 崩溃缝隙全修（收尾+resolve_deps 同
    atomic 等）— commit d9de442（build_log 0024）
  - [ ] CP5.6 语义判据 5 判例 + M4 步级验证收尾
- **M3 移交清单**（各处裁量汇总，M4 开工先读）：attack 轮 advance（idea/plan/bundle 阶段 + phase_commit 幂等 + 恢复扩展）；
  池注册 gate_register_*（15 函数，build_log 0013）；真执行 + 真 log/观测 + parser_result_suspect 真派生（此前复用判定不得
  对真执行上线，build_log 0015）；import 物化 materialize（占位→legal、scope 消费点、supersession——select_deferred 幂等
  守卫届时改判「未被 superseded 的」，见 importer.py 注释）；phase_commit 级 import-defer 捆绑（build_log 0019）；
  compiler 检索区/引用区接 recall（build_log 0016）；status_card selection.latest_decision 按 cycle scope 查（build_log 0016）。

### 步⑥（M5）人类控制台 + query 只读应答器
- 验证方法（§7.1 M5 行）：directive 按时机消费同记 DECISION；query 只读边界负例（responder 写库全被 authorizer 拒）；分类负例（unclear 不自动答）；ACK/query p95<2s；中介线程重建一致；润色≠raw；通知矩阵逐态推送断言；文件请求全流水 + 负例。
- 状态：未开始

### 步⑦（M6）长跑 + 验收剧本
- 验证方法（§7.1 M6 行 + §7.3/§7.4）：数百轮无人值守不漂移；双模式 A/B 实测定默认；§7.3 机制剧本（happy+失败路径）与 §7.4 研究能力任务（T1/T2）全通。开工前确认 OPEN #4。
- 状态：未开始
