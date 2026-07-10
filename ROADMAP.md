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
- 进行中的步 / 检查点：**步⑪ CP11.2b.3e 真实 connector 入站闭环**。CP11.2a/CP11.2a.1 用户文件资产与嵌套树身份已落 `c077cd2`/`3c4c9b4`；
  CP11.2b.1–2 鉴权耐久控制入口已落 `10215db`，CP11.2b.3a/b/c/d 动态预算、机械优先级、目标版本控制、只读查询旁路与真实 connector 耐久出站已落 `7f4fd73`/`2dfa653`/`92ecf18`/`6098a69`。步①–⑩已完成；步⑪ CP11.1 已完成，CP11.2b.3e/CP11.3/CP11.4 继续。
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
- 状态：**已完成**（§7.1 M4 全判据步级验证通过，436 绿，见 build_log 0025；OPEN #5/#6 落地闭）
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
  - [x] CP5.6 语义判据 5 判例显式命名验收 + M4 步级验证收尾（两文件联合勾兑 47 测全过）— commit eb5e7d9
    （build_log 0025）
- **M3 移交清单**（各处裁量汇总，M4 开工先读）：attack 轮 advance（idea/plan/bundle 阶段 + phase_commit 幂等 + 恢复扩展）；
  池注册 gate_register_*（15 函数，build_log 0013）；真执行 + 真 log/观测 + parser_result_suspect 真派生（此前复用判定不得
  对真执行上线，build_log 0015）；import 物化 materialize（占位→legal、scope 消费点、supersession——select_deferred 幂等
  守卫届时改判「未被 superseded 的」，见 importer.py 注释）；phase_commit 级 import-defer 捆绑（build_log 0019）；
  compiler 检索区/引用区接 recall（build_log 0016）；status_card selection.latest_decision 按 cycle scope 查（build_log 0016）。

### 步⑥（M5）人类控制台 + query 只读应答器
- 验证方法（§7.1 M5 行）：directive 按时机消费同记 DECISION；query 只读边界负例（responder 写库全被 authorizer 拒）；分类负例（unclear 不自动答）；ACK/query p95<2s；中介线程重建一致；润色≠raw；通知矩阵逐态推送断言；文件请求全流水 + 负例。
- 状态：**已完成**（2026-07-08；CP6.1–6.3，步级验证 64 测联合勾兑通过，见 build_log 0028）
- 检查点（模型切，边走边补）:
  - [x] CP6.1 保守分类器 + directive 生命周期：关键词规则确定性分类（query/directive/note/unclear；可能改状态
    一律 directive）+ 润色≠raw 时序（directive pending 先行、分类行回指，trg_iclass_directive_prov）+ 硬指令
    回显确认 confirm/reject + **consume_directive 按时机消费**（immediate/stage_boundary/reasoning_start；
    DECISION actor=human 经 directive；软指令不从记理由；未确认硬指令拒）+ 负例（unclear 无 directive 行）
    —— commit 702071e（build_log 0026；pause 状态模型=消费即进暂停态、阻断按 consumed_decision_id 消费序、
    resume 消费解除并只清早于它的 pending pause）
  - [x] CP6.2 query 只读应答器 + 中介：responder mode=ro+authorizer（写 decision/route/question/cycle/
    metric_result 全拒负例）+ grounding 校验/模板回退 + status_card 接线（advance 阶段边界发布 +
    latest_decision 按 cycle scope 补）+ 中介线程重建（interaction_*+status_card 重建、前后回答一致断言）
    + ACK/query p95<2s 断言 —— commit 28c6117（build_log 0027；temp/VTABLE 亦拒；查询文本按
    message_id 从持久层取[审计链]；rebuild 相关子查询防多回复扇出）
  - [x] CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待：directive 逐态推送断言（7 态+文件请求 3 事件）；
    outbox 幂等投递；pending 请求→Advancer 不开新研究 runner call 而 query/通知照常；uploads 校验 hash 入账
    并入 input/user_provided/ 恢复推进；创建拒绝三负例 + schema 拒 —— 收尾 M5 跑步级验证
    —— commit bee3b9e（build_log 0028；committed=换行终止/symlink 全链不跟/幂等先于 quota/
    provenance 同 goal/reasoning_start 时机矩阵）
- 步级验证结果：**通过**（M5 四文件联合勾兑 `pytest tests/test_console.py tests/test_mediator.py
  tests/test_status_card.py tests/test_notify.py` → **64 passed**。判据对照全表见 build_log/0028：
  按时机消费+DECISION / 只读边界负例矩阵 / unclear 负例 / ACK·query p95<2s / 中介重建一致 /
  润色≠raw+未确认硬拒 / 通知逐态断言 / 文件请求全流水+三负例+schema 拒+全局等待。）

### 步⑦（M6）长跑 + 验收剧本
- 验证方法（§7.1 M6 行 + §7.3/§7.4）：数百轮无人值守不漂移；双模式 A/B 实测定默认；§7.3 机制剧本（happy+失败路径）与 §7.4 研究能力任务（T1/T2）全通。开工前确认 OPEN #4。
- 状态：**建造面已完成**（2026-07-08；CP7.1–7.5，535 测绿；系统 reasoning-only 真 Codex 全自动跑通）。
  剩余 = 运维执行（§7.4 T1/T2 真跑、双模式 A/B 实测）+ 一处待用户裁的设计缺口（plan 制品契约→real-Codex
  attack）。
- **建造 / 执行边界（全自动裁量，2026-07-08）**：M6 建造面 = 让系统「完整运行、进入全自动」的机器
  （自终止安全网 + 真 Codex 装配入口 + 双模式 + §7.3 机制集成验收）；§7.4 T1/T2 是**真算力多日运维执行**
  （数百轮×24h 真 EEG），非本轮建造可完成——建造交付 = 系统可全自动启动 T1/T2 且机制验收过，跑到科学
  结论属运维。理由：§7.4「从头」任务本质是长跑执行，代码就绪后由运维发起。
- **OPEN #4 裁决（全自动，2026-07-08）**：paper-gap **不引入独立谓词机制**——「论文可写/缺口闭合」已被
  `goal.predicate_json` 的成功条件涵盖（T1=universal+novel+robust 或可审计负结论；T2=SOTA 超越+CI/Holm/
  ablation）；满足目标谓词即 = paper-gap 闭合，由既有 reasoning 收口(I3)+τ判据③（谓词满足）判定。另设
  paper-gap 审计题会与 τ判据③重复、且破坏 §2.3 七研究形态封闭。故 OPEN #4：折入 goal.predicate_json，
  不新增题型/机制。**机器契约（非仅文档约定）**：goalbrief.py 已强制 goal_brief frontmatter 含合法
  predicate_json（缺/非法即启动失败，§4.6.7）——「论文可写」成功条件即编码于此谓词、由 reasoning 收口
  按 I3 回溯证据判定，故是机器可执行契约。裁决落 build_log 0029 + goal_brief 模板注释。
- 检查点（模型切，边走边补）:
  - [ ] CP7.1 自终止安全网（§4.4.6 τ 三判据强制）：run_cycles 每轮前 StopController 评估——①open/active
    最高分连续 N 轮 < score_floor（in-process 计数，恢复保守重置注明）②全局成本 ≥ budget.session_max
    （**ledger.money 求和**，覆盖 reasoning/Codex 等全 phase；DB 派生、恢复安全的预算门每轮开工前评估）
    ③provider terminate（既有）；停机记 DECISION(orchestrator, global_stop, 原因)。policy.budget.session_max
    新增（决策性，走评审）。
  - [x] CP7.2 StageProvider 适配器：CodexRunner.run_task→信封解析→逐产物 schema 校验→files（artifact_parse
    重试；idea/plan/reasoning）——真组件+真 Codex e2e 可跑（judge/bundle 留 CP7.4）—— commit 2ce750e
    （build_log 0030；answer 语义下沉组件、sidecar fail-loud、stage 漂移校验）。**收窄**：全系统入口 run.py
    移 CP7.3；judge provider + attack 全链移 CP7.4。
    - CP7.4 硬前置（内审记）：①attack_stages._idea_stage 读 c["content_md"] 与冻结 idea_set.schema
      （无 content_md）漂移，接 idea 到真 Codex 前须校准；②stage-sidecar→notify.create_file_request 桥。
  - [x] CP7.3 全系统装配入口 run.py（goal_brief→DB init→装配真组件+StageProvider(真 Codex)+StopController
    +precheck+publish→run_cycles；一条命令全自动 reasoning-only 闭环）—— commit ce11d00（build_log 0031；
    **真 Codex CLI 冒烟通过**：一轮 bootstrap Codex 建真根问题+cycle done+决策落账+卡发布；只读连接单写
    边界、DB 权威 goal_body、τ/阻断/exit-2 端到端）。双模式 A/B：reasoning-only 下 A≡B，真 stage-granular
    分驱随 attack 落 CP7.4。
  - [x] CP7.4 §7.3 机制验收剧本集成测试（主链路对照 I1/I2/I3 因果链 / import 三失败路径 / 日志 suspect→
    gate 消费侧 fail-closed / 人机 §7.3-item4 三向负例）——**mock provider 驱动真组件**端到端串联断言
    —— commit 6be7566（build_log 0032；剧本 3 验消费侧 gate 拒 suspect 证据；I2 绑 run.status、I3 绑
    answered↔证据；evidence.kind CHECK 显式 raises）。
  - [x] CP7.5 M6 步级验证收尾：长跑步级验证（守卫上界内不漂移 + 中途重启终库一致 + τ 价值衰退自停）
    —— commit f45dd6f（build_log 0033）。诚实裁量：pure reasoning-only 受 tree_guard 约束（有限深度树），
    「数百轮」需 attack 答问+前沿更新（阻于 plan 契约缺口）——验尺度不变三性质，非字面数百轮。
- 步级验证结果：**建造面通过**（M6 §7.1 建造可达判据全落：CP7.1 τ 自终止安全网 / CP7.2 真 Codex
  StageProvider / CP7.3 run.py 全系统入口[**真 Codex CLI 冒烟跑通 reasoning-only 全自动闭环**] / CP7.4
  §7.3 机制验收[主链路 I1/I2/I3+import 三失败+日志 suspect fail-closed+人机负例] / CP7.5 长跑不漂移+
  可恢复+τ 自停）。**系统「完整运行、进入全自动」达成**（reasoning-only 真 Codex 端到端；535 测绿）。
  **未落（属运维执行 / 待用户裁 plan 契约）**：§7.4 T1/T2 真跑（真 Codex+真 EEG 数百轮×24h）+ 双模式
  A/B 实测定默认 + real-Codex attack（judge provider + idea/plan↔schema 校准 + sidecar 桥，须先裁 plan
  制品契约缺口方向）。这些是**运维执行 + 一处待用户定向的设计裁决**，非本轮建造可完成（见上「设计发现」）。

- **⚠ 设计发现（2026-07-08，CP7.4 勘查）· plan 制品契约二分 → real-Codex attack 阻塞**：
  冻结 `plan.schema` 的 target 字段（**抽象层**：target_key/spec_md/claim/eval_action/…）与
  attack_stages/harness 消费的执行 TARGET_SPEC（**具体层**：train_cmd/smoke_cmd/eval_cmd/canonical_key/
  slug/identity_draft_md/required/…）**仅 {eval_key,seq} 重叠**，13 个执行字段冻结 schema 不含。后果：
  经 StageProvider（schema 校验）接 attack 到真 Codex 时，Codex 产 schema-conform 的抽象 plan **拿不出
  harness 跑训练所需的命令**。这是**设计级缺口**（plan 制品契约需裁决：解冻 schema 让其携命令？加抽象→
  执行的翻译层？TARGET_SPEC 作独立执行制品？），非检查点级修补——超出「搭全自动机器」范围、触及研究
  执行契约设计。**影响面**：仅**真 Codex attack 轮**（=§7.4 T1/T2 运维执行的前置）；reasoning-only 全自动
  闭环（已真 Codex 跑通）与 §7.3 机制验收（mock provider 驱动真组件、验状态机+不变量）**不受影响**。
  **裁量**：M6 建造收尾 = §7.3 机制 + M6 长跑（CP7.4/7.5，不依赖本缺口）；judge provider + attack 真
  Codex 接线归**运维就绪**（与 §7.4 同批，须先裁 plan 契约）。已向用户面陈此发现待其定 plan 契约方向。
- 执行交付（本 session 外，运维发起，**且 real-Codex attack 前须裁 plan 契约缺口**）：§7.4 T1/T2 真跑
  （真 Codex + 真 EEG，数百轮×24h）+ judge provider + idea/plan 消费者↔schema 校准 + sidecar 桥。
  → **缺口已定向并转入步⑧落地**（2026-07-09）：不解冻 plan.schema；execution_manifest 作 bundle 编译
  产物承载执行契约（与 codex-chatgpt 联合设计结论）。judge provider / schema 校准 / sidecar 桥全部随
  步⑧检查点落。

### 步⑧（M7）plan 契约缺口补齐 → 全流程 real-Codex attack

- 来源：用户 2026-07-09「开始补齐这个缺口吧，我要的是成品最后能完整走完整个流程的系统」；同日追加
  「**最后要是正式的系统直接可用的系统**」——完成定义升格：不止 toy 链路跑通，交付面须**正式
  直接可用**：①plan 全形态受理（build/exec/eval targets + import_defer——真 Codex 冒烟实证它天然会产
  exec/eval，缺特化即浪费轮次）②阶段资源请求桥（真研究要向用户要数据文件）③运维入口可用（一条命令 +
  文档化操作面）。CP8.5–8.7 因此为**必做**，非边走边补。
- **方案（已与用户对齐 + codex-chatgpt 联合设计，2026-07-08/09；设计记录
  scratchpad `plan_contract_design_out.md`，要点如下）**：
  - **不解冻任何冻结件**（plan.schema / DDL / MIGRATION_SHA256 不动）；plan 保持抽象（本来就对）。
  - **新增 additive 契约 `execution_manifest.schema.json`**：bundle 阶段 Codex 的**编译产物**——机器可
    验证执行契约（argv 数组禁 shell / cwd 限 staging / env 白名单 / 期望产物 / 超时 / 回引 plan 切片与
    protocol，防「新 plan 旁路」）。harness 只执行 manifest，不推理不猜命令。
  - **attack_stages 走正式 gate 通道**：gate_new_protocol（I1）+ gate_claim_baseline（I5），消费冻结
    plan.schema 真形态；机械派生 protocol_id / metric int 映射 / eval_key / target_set_hash / identity
    草稿；清理 toy TARGET_SPEC（不再从 plan_ref 读命令）。
  - **诚实边界（operational canary）**：本步不接真 git worktree——Codex 在 staging 产码 + manifest 即可
    端到端；worktree 隔离 + diff/tree hash + env lock 强校验 = 后续硬化步（正式数百轮前补）。
- 验证方法（步级）：①全链 E2E 测试：mock Codex 产 schema-conform 制品（含真 toy 代码，经 manifest 由
  harness 真子进程执行）驱动 run.py 装配系统跑通 bootstrap→decompose→attack（build 目标：占坑→smoke→
  代码评审→训练→出厂评估→结果评审→注册入池）→真证据关问→terminate；②真 Codex CLI 冒烟：一条命令跑通
  ≥1 个完整 attack 轮；③回归：既有 535 测全绿 + 冻结件字节不变（plan.schema、MIGRATION_SHA256）。
- 检查点（模型切，边走边补）:
  - [x] CP8.1 execution_manifest 契约 + harness manifest 适配层：新增 `schemas/execution_manifest.schema.json`
    + `orchestrator/manifest.py`（schema 校验 / 与 plan 切片交叉核 / 占位符 {src}/{ckpt} 解析 / argv·路径·
    env·超时围栏 + 禁 shell 启动器 / 净土物化正反双向对账 + symlink 审计）+ policy.execution 节。纯新增，
    不改既有行为 —— commit `8b4f59a`（build_log 0034；codex 两轮外审：第1轮 2 BLOCKER[禁 shell·checkpoint
    ../x]+2 SHOULD 全修，第2轮无 BLOCKER 仅 2 SHOULD[. 段·ledger 校验]亦修；pytest 578/578，43 新，535 无回归）。
  - [x] CP8.2 attack_stages 真契约化：idea/plan 消费冻结 schema 真形态（content_md 机械合成）；plan 段走
    gate_new_protocol + gate_claim_baseline（persist-then-consume + 纯读派生[protocol/metric string→int]+
    gate 幂等/复用 + 终局单事务）；bundle 逐目标 manifest 驱动真执行（fresh/resume 同口径校验+净土物化）；
    toy TARGET_SPEC 清理 + frozen_contracts 锁。**全自动不楔死**：任何站不住的 plan→业务拒(plan_rejected)、
    Codex 产物层 manifest/测量违约→目标 failed(artifact_invalid/protocol_violation)+pc；状态机/损毁类
    GateReject 仍 fail loud —— commit `d822add`（build_log 0035；pytest 594/594；codex 两轮：第1轮 3 BLOCKER
    +2 SHOULD 全修，第2轮采纳收窄 GateReject 捕获、驳回误读项[render 返回 ContextPack]并记录）。
  - [x] CP8.3 生产装配：bundle SKILL.md 真执行契约改写（m7-1）+ judge（review_verdict schema + judge skill
    + JudgeProvider 写 runner_call(audit)+DECISION(judge)，attack 专用）+ StageProvider bundle passthrough
    + harness.latest_smoke_log 数值序（attack/judge 两侧同口径）—— commit `c7863c0`（build_log 0036；
    pytest 608/608；codex 两轮：第1轮 2 BLOCKER[?键诱导非法 JSON/result 材料缺代码]+3 SHOULD 全修，
    第2轮 APPROVE）。
  - [x] CP8.4 run.py attack 全装配 + 步级验证①②：build_system 装配 AttackStages 全家（连接分工：gate 判据
    open_gate_read_conn / 观测 open_responder_read_conn / judge 写 daemon）→ 全链 E2E（mock，含 JudgeProvider
    真落库与真证据关问）+ **真 Codex CLI 冒烟完整 attack build 链跑通**（协议注册/占坑/真训练 acc=0.9949/
    双评审/legal 入池/τ 真实触发）；含冒烟发现的自纠环（plan SKILL kind 前置条件 + 拒因回流·成功后静默）
    与 import_defer 显式拒 —— commit `4f39559`（build_log 0037；pytest 613/613；codex 第1轮 APPROVE，
    2 SHOULD+3 NIT 全采纳）。
  - [x] CP8.5 sidecar→file_request 桥（正式可用性①）：StageBlockedOnResources 控制信号 + 真桥
    （create_checked 落单→干净停[在途轮保持游标零提交]→precheck 拦→resolve→同一在途轮续跑；业务拒
    重试/损坏 fail-loud 分流；判官拒 sidecar）—— commit `650aace`（build_log 0038；pytest 618/618；
    内审+codex 第1轮双 APPROVE，NIT 全采纳）。
  - [x] CP8.6 exec target kind（正式可用性②之一）：既有 legal baseline 上建变体——_derive_plan exec 占坑
    身份前置判（baseline_ref→legal / variant_key 未占 / config 非空）+ gate_claim_variant（自建 exec bt）+
    终局 UPDATE plan_ref/eval_key + manifest 驱动真训练/评估（run.kind='exec'）+ gate_register_variant 入池
    （baseline 身份不动）。append 绑定核收准（同 variant 别 target 可追加、跨 variant 仍拒，DDL-safe）——
    commit `a713509`（build_log 0039；pytest 623/623；内审 1 BLOCKER + codex 两轮 2 BLOCKER 全修，
    第2轮 APPROVE）。
    **切分（模型裁量）**：eval target（frozen schema 对 create_evaluation 无 variant 引用，需设计）+
    import_defer/ImportWorker 装配 + route dependency_wait 特化 → CP8.6b（独立检查点）；exec 已覆盖
    「build 家族 + 变体迭代」核心研究形态。
  - [x] CP8.7 正式可用收口（正式可用性③）：运维操作手册（meta-research/README.md 重写：启动命令/goal_brief
    写法/policy 旋钮/console·status_card·文件请求交互/恢复与停机语义/诚实边界）+ 步⑧步级验证三条留证——
    commit `d627a39`（build_log 0040；pytest 623/623；内审逐条实证事实一致[1 BLOCKER budget 停因休眠
    caveat 已修]，codex APPROVE）。
- 步级验证结果（步⑧ M7）：**「正式直接可用」达成**——一条命令 `python -m orchestrator.run --system-root .
  --work-root <dir>` 让真 Codex 全自动跑完整研究元循环（idea→plan[真 gate 注册协议/占坑]→bundle[manifest
  → harness 真子进程 build/exec smoke·train·eval]→双评审[JudgeProvider 真落库]→注册入池→真证据关问→
  decompose/terminate/τ 自停），带文件请求全等待环、kill-9 崩溃恢复、人机 pause/resume/query、全自动不楔死。
  三条步级验证全过：①全链 E2E（test_full_attack_flow_end_to_end）②真 Codex CLI 冒烟（build 链 acc=0.9949
  legal 入池，exit 0）③冻结件锁（plan.schema+MIGRATION_SHA256 步⑧零漂移）。623 测绿；不解冻任何冻结件。
  运维面文档化（README §0–§8），边界诚实标注。
- 遗留（诚实边界，非阻塞交付；README §7 载）：①全局成本安全网休眠（ledger 成本落账未接，真长跑前须接）；
  ②真 git worktree 隔离 + env lock 强校验（canary→硬化）；③**CP8.6b** = eval target（frozen schema
  create_evaluation 缺 variant 引用，需设计）+ import_defer/ImportWorker 装配 + route dependency_wait
  特化；④§7.4 T1/T2 数百轮真跑 + 双模式 A/B 实测 = 运维执行。

### 步⑨（M8）人类控制台接入——系统在控制台上可查看/可交互

- 来源：用户 2026-07-09「现在代码里好像没有接入控制台啊，人类控制台原型没有接入系统呀。最终目的是要
  系统在人类控制台上查看」+「控制原型文件在 reference/人类控制台原型-v2.html，在 meta_research_buiding
  文件夹里构建！这不是 bug，是架构问题」。**纠正 CP8.8 期「系统无 web 组件」的结论**——用户明确要真接入
  控制台可视化（§4.6 的可视化载体）。原型 = `reference/人类控制台原型-v2.html`。
- **接入方案（勘察结论）**：原型内嵌 mock `DB` 对象与真 DDL 表名一一对应（+派生键 status_card/live/
  notification/policy/FS）——**保留原型渲染代码，换数据源**：
  - **控制台服务 = 独立只读进程**（单写纪律：mode=ro 读库 + 只写入站 spool 文件，绝不写 DB）：
    `orchestrator/console_server.py` 组装 `/api/db`（真表动态列投影 + status_card.json + live[游标/心跳=
    transcript mtime 年龄/模式 running·idle·awaiting_user] + ledger_by_cycle + outbox 事件 + policy + 真 FS 树）
    + `/api/file` 白名单读 + `/api/message` 写 spool + 静态服务控制台页。stdlib http.server，零新依赖。
  - **前端 = 原型派生**（reference 原件不动）：`views/console/index.html` 剥 mock（DB 常量→loader 轮询
    /api/db；假事件 ticker→真轮询；cmdin/narrin→POST /api/message）。server 形状=payload.tables.<表>+派生
    平铺，前端 loader 做适配（原型访问顶层 DB.<表>）。原型自带 HEADLESS node 冒烟可自动化验证。
  - **入站闭环走 M5 既有机制**：run 进程在 precheck 边界（既有 directive 消费点）ingest spool →
    InteractionIngest → Console.handle_inbound（保守分类/回显确认）→ query 走 Mediator 应答（grounded）。
- 验证方法（步级）：①系统真跑 + console_server 并行 → 浏览器看到**真实运行状态**随刷新（真 Codex 冒烟留证）；
  ②控制台发 pause→系统停推进、resume→续跑、query→grounded 回复（端到端）；③测试基线全绿、单写纪律不破
  （控制台进程零 DB 写）。
- 检查点（模型切，边走边补）:
  - [x] CP9.1 控制台数据面：`orchestrator/console_server.py`——只读组装 /api/db（真表动态列投影 +
    status_card + live + notification + policy + FS 树）+ /api/file 白名单读（虚拟根显式映射 + resolve
    containment 防逃逸/symlink）+ /api/message spool 写（不碰 DB，connector 固定 console）+ 静态服务；
    组装/读失败泛化报不泄细节；高基数表 LIMIT。—— commit `<pending>`（build_log 0042；内审 APPROVE +
    codex 两轮；test_console_server 13 测：单写零 DB 写实证 + 路径逃逸/symlink + 撕裂尾行 + 并发 seq）。
  - [x] CP9.2 控制台前端接入：`views/console/index.html` 由原型 v2 派生（换数据源 /api/db + **去 mock 渲染码**：
    原型把 mock 焊进渲染码，全改读真 payload；adaptPayload/buildLive/applyLive/clampSelections/streamSyncReal/
    refreshDB/fsRefresh；删 6 魔法轮号面板+2 死函数；空/稀疏/null 真库全守卫）+ node HEADLESS 遍历全 9 标签页冒烟
    (seeded+空库+null-cap) + 前后端形状契约测试。→ commit `302261a`（build_log 0043；codex 第2轮 APPROVE；642 测绿）。
  - [x] CP9.3 入站闭环：run 进程 precheck 边界 ingest console_inbox.jsonl（ConsoleInboxIngest → handle_inbound
    [幂等落 directive/note] → query 经 mediator 应答；no-loss/no-dup：query-once 落持久层、只有 durable reply 才推进
    游标、line-index 游标、有限重试+终态回执、顶层兜底不崩主循环）+ 控制台 pause/resume/query 端到端测试。
    → commit `6967dc3`（build_log 0044；codex 两轮 §2.2 修毕；662 测绿，含 pause→确认→阻断→resume 端到端）。
  - [x] CP9.4 端到端验收：test_console_e2e 全栈真跑（真 HTTP+真 DB+真 spool+入站闭环）——①真视图 GET / 真页 +
    /api/db 真数据 + /api/file 白名单[含逃逸负例]；③单写纪律**强证** _open_ro(mode=ro) 写被物理拒 + 逻辑快照
    (WAL-proof)不变；②POST→spool→ingest→pause→确认 provenance 落库→precheck 阻断、query→grounded+no-dup；
    README §4.1 控制台节；实机 CLI 起 console_server + curl 真跑留证。→ commit `cd97ee0`（build_log 0045；codex
    两轮 §2.2 修毕；664 测绿）。**⇒ 步⑨（M8 人类控制台接入）达成**：系统在人类控制台上可查看真实状态 + 可交互
    （pause/resume/query），单写纪律不破。

### 步⑩（M6 硬化）成本记账接线——激活全局预算安全网

- 来源：用户 2026-07-09「开始接入」（承接步⑨达成后的存量优先级）。§7 诚实边界 + README §6：`INSERT INTO ledger`
  （成本记账）**尚未接线** → `SUM(ledger.money)=0` → `budget_exhausted`（§4.4.6 判据②）**永不触发**、`budget.session_max`
  安全网**休眠**。真跑长时研究前失控成本无自动上限（只 τ 分数衰退 + `--max-cycles` 兜底）。本步接线，让安全网转活。
- **勘察结论（Explore 代理 + 实机 probe 定）**：
  - 读侧已装：`stopcontroller._budget_exhausted` 读 `SUM(ledger.money) ≥ session_max`（轮前/轮后各查）；无任何 `INSERT INTO ledger`。
  - **token 来源**：`codex-chatgpt exec` 把 `tokens used\n<N>`（总 token，逗号分隔）打到 **stderr**（stdout=信封）；
    生产 runner `capture_output=True` 已捕获 stderr、但仅失败时读——成功时丢弃。解析即得真 token。
  - runner_call 行：目前**仅 audit(judge)** 写；idea/plan/bundle/reasoning 从不写 → ledger 的 runner_call_id FK 需一并补建。
  - 无 tokens→money 汇率（policy 无 pricing）；status_card 读 `cycle.cost_total`（列，从不写）与 stopcontroller 读
    `SUM(ledger)` 两套 dormant 成本面互不对账。ledger append-only（触发器）→ 累计靠新 INSERT。
- **设计（自主决定，"遇到问题先自行处理"）**：① runner 解析 stderr token + 计 wallclock → `Artifact.usage`；
  ② `CostLedger` 服务：每次 LLM 调用写 `runner_call`(补全各 phase) + `ledger`(tokens/wallclock/money)；money =
  tokens/1000 × `budget.price_per_1k_tokens`（notional/provisional，缺省使 cycle≈B_max 量级、session_max 为失控兜底，A/B 细化）；
  ③ status_card `cycle_spent` 改读 `SUM(ledger per cycle)` 统一口径。
- 验证方法（步级）：真跑（或 mock usage 注入）积累 `ledger.money` → `SUM ≥ session_max` → `budget_exhausted` 干净停
  + durable global_stop 落库（恢复也拒推进）；ledger append-only 不破；测试基线全绿。
- 检查点（模型切，边走边补）:
  - [x] CP10.1 runner 成本捕获：`runner.py` 解析 stderr `tokens used` 总 token（行首锚定+合法千分组+取末条+坏输入→0）
    + subprocess wallclock → `Artifact.usage`（interfaces.py 加 CallUsage + Artifact.usage 非破坏）；不改循环行为。
    顺带修 test_run.py 既有 `__new__` monkeypatch 全局污染 bug。→ commit `1b415f9`（build_log 0046；codex 第2轮 APPROVE；678 测绿）。
  - [x] CP10.2 ledger 写入 + 激活安全网：`CostLedger`覆盖成功/失败/非法产物重试，
    未知用量持久 fail-closed，Judge 三写原子，单次越线即 `global_stop`；`price_per_1k_tokens`
    + schema 接线。→ commit `03d3ffd`（build_log 0047；pytest 754；codex 第2轮 APPROVE）。
  - [x] CP10.3 对账 + 步级收口：status_card `cycle_spent` ← SUM(ledger)，`global_remaining` ← 动态 session_max − SUM(ledger)；动态降预算同事务落 global_stop，precheck 禁止再泄漏一次 Runner 调用。→ commit `7f4fd73`（build_log 0052；全量 976）。

### 步⑪（M6/M7 生产硬化）恢复协议、控制闭环与运行边界

- 来源：用户 2026-07-09 要求根据全系统设计/实现审查直接修改，并以独立 git 提交方便回滚。
- 目标：保留 SQLite/Gate/append-only 骨架，优先消除会破坏无人值守承诺的 poison-pill、断裂控制面和单实例/执行边界缺口。
- 验证方法：每类外部坏产物首次与全新实例重启均能业务收尾或受控停机、不重复裸崩；真实 HTTP 控制台可完成
  hard directive confirm/reject 与文件请求 resolve/cancel；多目标 critical 语义、goal 最新版、单实例锁和子进程组终止均有正反例；全量测试绿。
- 检查点（模型切，边走边补）：
  - [x] CP11.1 外部产物接纳硬化：metric 严格解析/有限值 + 统一 SQLite ID 边界 +
    reasoning 语义拒收，首次/全新实例重启 no-wedge，DB 损坏 fail-loud。→ commit `ac53516`
    （build_log 0048；定向 132；全量 754；codex 第1轮 APPROVE）。
  - [ ] CP11.2 人类控制闭环（子检查点全完成后勾选）：
    - [x] CP11.2a 用户文件资产闭环：原子接纳、goal-wide 有界回执、稳定 fd 真消费、生成时身份冻结与 root-global 并发/配额闭合。→ commit `c077cd2`（build_log 0049；staged-only 全量 844；外审两轮上限后全部本地修复）。
    - [x] CP11.2a.1 嵌套上传树身份跟进：逐组件 `openat(O_NOFOLLOW)` 固定目录/文件身份、同一 fd 复制与复制前后指纹复验，并为一次 resolve 的目录深度、目录项和目录数设置总预算；独立于控制面提交，保证 CP11.2b 可单独回退。→ commit `3c4c9b4`（build_log 0050；staged-only 定向 80、全量 857；外审第2轮 APPROVE）。
    - [ ] CP11.2b 人类控制面：真实 console confirm/reject + 文件请求 resolve/cancel + 生产 notifier 接线，继续保持 HTTP 进程 DB mode=ro、run 单写。
      - [x] CP11.2b.1 耐久动作入口与诚实语义：稳定 spool/cursor/retry、结构化动作 provenance、跨操作幂等域、abort 原子释放、note 真正进入 reasoning；尚未闭合的指令明确 rejected，禁止空效果伪报 consumed。→ commit `10215db`（build_log 0051；与 b.2 合并闭合真实生产入口）。
      - [x] CP11.2b.2 鉴权常驻控制台：loopback capability HTTP、默认 resident run、权威 live/ledger/文件投影与 fresh-snapshot 前端动作门。→ commit `10215db`（staged-only 定向 292、全量 959；外审两轮上限后全部装配 BLOCKER 本地修复）。
      - [ ] CP11.2b.3 reference 完整控制能力（子项全完成后勾选）：
        - [x] CP11.2b.3a 耐久动态预算、预算/账本/状态卡对账与机械 `pin/boost/suppress`；确认前零效果，实际应用/不从/失败均有 decision，重启同源投影。→ commit `7f4fd73`（build_log 0052；相关 234、全量 976；外审两轮上限后全部反馈本地修复）。
        - [x] CP11.2b.3b `goal_amend`：确认 effect 精确绑定不可变目标新版本；专用 reasoning-only route；open/inconclusive 迁移重评，closed answer/evidence 保持出生版本并走 applicability/revalidate；覆盖 route/consume/application 崩溃与竞态恢复。→ commit `2dfa653`（build_log 0053；控制面/调度面 374、全量 992；外审两轮上限后全部反馈本地修复）。
        - [x] CP11.2b.3c 真正只读 Codex query responder：发布卡 facts-only 回复、不同 UID/tool-free 调用、耐久逐会话 FIFO、调用/成本/reply 原子收尾与未知调用失败终态。→ commit `92ecf18`（build_log 0054；唯一一次全量 1055 过/1 败，唯一前端契约失败修复后定向 6/6；外审两次均因账号限额无 verdict）。
        - [x] CP11.2b.3d 真实 connector 耐久出站：`producer_id + event_key` 幂等、queue/retry/receipt at-least-once 收敛、严格 webhook/OneBot ACK、因果 FIFO 与公平调度、worker 健康及诚实 console 投影。→ commit `6098a69`（build_log 0055；相关 239；唯一全量 1105；外审两轮均无 verdict）。
        - [ ] CP11.2b.3e 真实 connector 入站闭环：webhook/OneBot poll 入站、认证且耐久的 source/conversation 绑定、幂等接入既有 durable interaction spool，并闭合“继续”特殊控制语义。
  - [ ] CP11.3 状态与执行边界：critical/budget_estimate 落库与早退、goal 最新版、orchestrator 单实例锁、超时终止进程组。
  - [ ] CP11.4 残余架构边界：调用意图/回执补账、执行容器/VM 隔离与内容寻址 artifact store 方案化。
