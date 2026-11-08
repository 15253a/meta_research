# #263 诊断与修正链验收记录

基线为 `origin/v1-test@ea421520434afcc32047ed35c937c64aa9cb9bbd`，实现分支为 `codex/issue-263-diagnostics`，范围按 [#263 当前重开要求](https://github.com/15253a/meta_research/issues/263) 和 [#262](https://github.com/15253a/meta_research/issues/262)。工作开始及收口前均读取远端状态：#263 负责人 15253a，无原生 blocked-by；开放 PR #221 是独立 DeepFetch 工作。独立 checkout 保留 `.issue-291/source` 原有未提交修改及未跟踪文件，没有把它们合入候选。

**#263 本票源码验收完成。** 运行源码冻结为 `beef1479d46b8639c747a9edbb54e78d9412d2b9`，全套、实际模型复验和失败归因已完成。后续仅修正一项匹配旧长句的测试断言，373 个运行源码文件未变。发布目标为 `v1-test`，实际推送及关闭状态以 GitHub 为准；本记录不将源码验收等同于已发布或已部署。

## 实现与反馈长度

- 参数拒绝给出相关字段、具体约束及简短修正方向；Owner 的稳定错误码和有用 details 到达文本与结构化消费者，两种返回表达同一诊断。
- 区分业务拒绝、认证、协议、网络、宿主异常和坏回执。条件参数、互斥引用、精确版本及分页错误使用真实公开字段名，不用内部别名指导调用者。
- handler 未运行只描述本次调用，不推断同一 effect 的历史结果。结果未知保留原 operation、effect 和对账定位；先用原完整提交对账，再决定后续工作。授权、版本、幂等和已有效成果不变。
- Bundle／Plan／Dataset／上下文 reader 等包装保留 Owner 原因；DeepFetch 的失败事件、REST 和重启仍保留原 run/attempt/session 及诊断。外部 MCP 区分参数错误与损坏合同；Writing 指明配置或服务处置责任。
- 原因和修正方向控制为够用的短说明，不重复整份 schema、提交或供应商正文。没有新增通用错误平台、自动重试器、权限或状态迁移。

## 实际工具清单

| 清单 | 实际观察与边界 | 证据 |
|---|---|---|
| 内置语义操作 | 96 项：47 read、24 effect、24 reconcile、1 verify；逐项给出 handler、角色、合同、修正／对账方向及覆盖状态 | [逐工具覆盖](tool-coverage.md)、[实际注册清单](registered-tools.json)、[生成脚本](collect_tools.py) |
| 安装 Harness 原生工具 | 13 个实际装配变体；普通场景 23 项，provided_only 22 项，Recorder 3 项，合并 26 个原生身份。包含 code mode 的嵌套／延迟工具，不仅统计顶层 tools | [原生清单](native-tools.json)、[采集脚本](collect_native_tools.py) |
| 现场外部工具 | 8770 观察时点没有已配置外部 MCP 或 supplemental 搜索源；不假造供应商实测成功 | [实例元数据](target-8770-inventory.json) |
| 共享 Skill 与特例 | Writing 使用调用方 grant；创建材料 Companion、Recorder、Harness conformance 各有独立授权边界 | [角色调用链](role-chain-validation.md) |

最终 96 项注册清单取自 `beef1479` 的 945 个源码／测试文件，归档 SHA256 为 `3b71ab76fb4c635057dd965a1f0ba826de1933e0f9e7689c9f4beb688bb622d3`；后续只有一项测试断言改动，运行源码未变。注册不等于每个业务分支都已执行；相同网关机制复用验证，独有组参、包装及恢复路径单独列出。原生装配与恢复证据取自 `da890f4`；[源码复用核对](native-source-reuse.json) 比较 11 个完整装配模块及 Recorder operation AST，证明这些装配路径未改变，不据此推定其余业务源码未改变。

工具目前有授权目录、分页／搜索／Owner 筛选和精确合同读取，**尚未实现 #269 的按目的分层发现**。本票修复错误反馈，不把目录层级工作记为完成。

## 实际修复证据

以下均在 Linux 隔离目录、Python 3.12.13 及锁定依赖下执行，使用生产装配、真实 grant 和临时 Owner 数据。受控故障明确标注；各测试集有重叠，不相加为总量。完整红绿历史、归档 hash 和基线对照见 [validation.json](validation.json)。

| 独有边界 | 已观察到的修复和继续行为 | 证据入口 |
|---|---|---|
| 公共网关 | 参数路径／上下界；真实效果存在或不存在时坏回执均先报告 unknown，再查原 effect；text/structuredContent 一致 | `test_semantic_failure_diagnostics.py`、`test_owner_diagnostic_propagation.py` |
| Dataset／Environment／Literature／Baseline | 条件筛选、精确引用、全文绑定和 query revision 指出可改字段；实际修正后原 grant 继续，原登记仍可对账 | `test_discovery_failure_diagnostics.py`、`test_additional_tool_diagnostics.py` |
| Plan evidence／Bundle implementation | 原版误报宿主失败；现在保留 Owner 原码和简短原因。Plan 三处受控 I/O 故障恢复后按原精确绑定读；Bundle 冲突后原 payload 对账/read 保留原结果 | `263-owner-family-red`：2 failed；`263-owner-family-green`：新增 2 例通过，合跑 12 passed、1 既有 catalog 失败；`test_owner_family_diagnostics.py` |
| 上下文／Question history/relations／创建依据 | 保留 Owner details，分页提示使用 index_offset 等公开字段并重置字节 offset；缺少 source selector 指明 material_key | `test_additional_tool_diagnostics.py` 新增节点及最终全套对应节点通过 |
| Target／材料／HumanRequest／Guidance／资产入库 | 模式条件、来源互斥、授权条件与 reader 选择器给具体原因；11 处前置拒绝保留历史效果 unknown，原参数对账及有效工作保留 | `263-effect-prior-state-red`：真实先接纳再同 effect 畸形提交失败；`263-effect-prior-state-green`：整文件 10 passed，945 个源码／测试文件与 `beef1479` 一致 |
| 创建材料 Companion／Recorder | 真实 chroot/grant 的复制限额修正与对账，已编辑副本不覆盖；Recorder 原 job/basis/source/hash 暂不可读后恢复，正文及 Owner revision 不变 | `test_role_chain_diagnostics.py`；初始 2 red→2 green，后续复制大小分支另有红绿记录 |
| Resident／DeepFetch／外部 MCP／Writing | 认证／协议／服务故障分类，失败事件重启保持，外部合同和参数分开；实际 Companion grant 中恢复原配置继续 | `test_tool_bridge_diagnostics.py`、`test_tool_failure_diagnostics.py`、`test_companion_writing_request.py` |

## 原 Agent 修正、对账并继续

`beef1479` 真实模型复验使用安装的 Codex CLI 0.159.0、`gpt-6.1-sol`、实际 `CodexHarnessAdapter`、生产 gateway 与 Idea grant。当次 945 个源码／测试文件与该提交逐项 SHA256 相同，差异为 0；最终交付只多一项测试断言修正，373 个运行源码文件仍相同。隔离初态和平台租约由 fixture 建立，模型决定后续工具调用。

- 最终原会话 `01a1bc62-b09a-7932-8c27-8b8ad0fd8d36`：过期 `content.read` → 具体诊断 → 自行查 `questions.page` → 精确版本读取成功；真实 literature Owner 接纳后注入一次坏回执 → unknown → 原 effect reconcile → 目录／正文读取。7 次真实 CLI 工具结果（含 2 次失败）与 HTTP 参数、text/structuredContent 逐项一致，写入和 Owner 执行各一次。两份完整错误文本分别为 285、563 字符，保留可执行修正和原对账定位。
- 原生会话 `01a1bc3d-e23c-73c0-812f-cf47873644e9`：文件不存在后查目录读实际文件，patch 旧上下文后读文件改正，子 Agent 名错误后有效 spawn/wait；第二回合 followup 同一子 Agent。已有文件 hash 不变，只修改目标 draft 字段，未创建虚假缺失输入。见 [原生恢复结果](native-recovery-summary.json) 与 [事件](native-recovery-tool-events.json)。

[模型消费记录](real-agent-consumption.json)、[源码清单](real-agent-source-files.json) 与 [探针](verify_real_agent.py) 对应最终运行源码；旧 `da890f4` 模型证据保留在 [历史记录](historical-da890f4/real-agent-consumption.json)。探针结束后临时凭据剩余 0，本次专用隧道已停止，原凭据及部署未改动；见 [语义探针清理](real-agent-credential-cleanup.json) 和 [原生探针清理](native-credential-cleanup.json)。

## 全套测试与代码审查

复用 #291 `full-final`：936 个源码／测试文件与 `ea42152` Git 字节一致；4756 collected，4271 passed、483 failed、2 skipped。旧 manifest 的 HEAD 与后续提交不同，复用依据为内容 hash，不能使用当前 dirty 工作树推断。480 个失败有更早基线复现，另外 3 个竞态记录仍保留。

| 版本／运行 | 完整结果 | 用途 |
|---|---|---|
| 初始 `787bf71` | 4787 collected，4303 passed、482 failed、2 skipped | 历史全套；其中 1 个新增 Idea 整对象断言已修复并复验 |
| 中间 `da890f4` | 4800 collected，4318 passed、480 failed、2 skipped、0 incomplete | 两段内容相同的冻结运行覆盖全部 532 文件；与 #291 比较 428 同阶段同诊断、52 同阶段诊断变化、0 新增失败。中断日志保留，不算另一完整成功运行 |
| 最终 `beef1479`／`263-final-reviewed-full` | 4840 collected，4356 passed、482 failed、2 skipped、0 incomplete；537 文件全部完成，1149.47 秒 | 归档 `854bf482080ed54563ae8470e29615ddfa8861bb5da691f48a8eeaa03a390454`；完整原始失败记录保留，不因后续专项结果改写 |

最后补齐的 5 个诊断测试文件共 38 个节点在最终全套全部通过。最终交付的 373 个运行源码文件与该冻结提交一致；572 个测试文件中只有下述断言文件随后变化。

482 项失败中，480 项在基线已失败；另外 2 项逐一处理。`test_tool_contract_alignment` 匹配旧长句的断言改为核对短诊断的 source/version、questions.page、准确 reader/source_quest_ref 和其他内容仍可能存在，保留错误码、双通道一致、原 grant 成功及撤权检查；`263-concise-contract-assertion` 整文件 5 passed，Spec 确认未削弱，归档 `ac1417f60ba32bafe2547e556d8e156f07d9c1580fbfd7f5ea9daee2574da4d9`。未重跑全套，原始 482 failed 保留。

另一项 `test_target_progress.busy_logs` 的 19/20 为本次新观察到的既有分页缺陷，未修复：该测试直调 collector，不经过本票修改的 semantic 包装；6 个相关文件从基线到 `beef1479` 字节相同。`263-progress-order-baseline` 和 `263-progress-order-candidate` 用真实文件及受控 mtime 排序均复现 8→16→19，不能将其计作已修复或通过。完整节点／阶段对照、旧 catalog 与 Bundle 失败及夹具接线记录见 `validation.json`。

Standards 正式终审 0 项；Spec 对源码、实际模型、清单及上述测试断言修正无未解决发现，审查结论见 [代码审查](review.md)。项目没有配置 Python typecheck 命令，编译检查不冒充类型检查。基线 `ea42152` 的 Response time CI run `42498124848` 已有 npm test 40 passed、5 failed（`MetaTrace is not defined`）；本票 `web/` 与 `.github/` 树精确未变，不宣称 CI 全绿。

## 验收与发布边界

#263 的实际工具清单、独有角色链、原 Agent 修正／先对账并继续及回归对照已齐备；旧失败和未修复的 collector 缺陷保留明确记录。按用户授权向 `v1-test` 非强制推送并关闭 [#263](https://github.com/15253a/meta_research/issues/263)，发布结果以该分支和票据的远端状态为准。

8770 现有服务仍为原部署及已有修改，本票没有修改服务或研究数据。隔离源码验收不等于部署验收。[#274](https://github.com/15253a/meta_research/issues/274) 负责联合版本完整科研 Cycle、阶段接纳／交接与界面消费，不代替本票工具错误验收。后续分层工具发现由 [#269](https://github.com/15253a/meta_research/issues/269) 实现。
