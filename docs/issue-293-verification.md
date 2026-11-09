# #293 研究时间、工作调整与人工延时验证

交付状态（2026-11-10）：实现和专项已完成，**真实模型完整验收尚未通过，不能据此关闭 #293**。
第二组有批准后原根实际执行证据，但申请迟到且最终超时；最终指导版本的第三组首次调用即
`codex_usage_limit`。失败和缺口保留如下，未用受控测试替代真实模型判断。

范围以 [#293](https://github.com/15253a/meta_research/issues/293) 与
[#262](https://github.com/15253a/meta_research/issues/262) 的远端内容为准。
2026-11-09 开始实施前已认证读取两票、评论、原生依赖和在途 PR：
#293、#262 均无负责人；#293 没有 blocked-by，阻塞最终同版验收 #274；
当时唯一在途 PR #221 是无关的 DeepFetch 工作。未改远端负责人、票据状态或依赖。

从当时最新 `origin/v1-test@acc7399b10ed4035934483cf4d5a20bd3c9c3a1c`
创建隔离分支 `codex/issue-293-quest-time`。实现位于 Codex 管理的独立工作树；
原 `D:/10-7优化实现8768/research-8768-20260915/worktree` 的已有未提交工作未被用作实现基底或写入目标。
实现提交为 `b70ea0f2d067a0cdc4ec81aa8aa50199f43e4ac6`。其全部 1331 个 tracked 文件
与 `final-special01` 冻结文件逐一 SHA-256 相同，见本地 `engineering/frozen-to-commit.json`。
后续只补了一处浏览器测试的异步保存等待及本文；生产代码和正式前端产物没有后续变动。
原工作树复核仍是 `v1-test@7e5c38928006af6bef63392bf7f54cd0a86f7b66`，已有改动保持。

## 实际合同

- 复用 AE 持久状态承载 Quest 共用有效经过时间。正式首次 Cycle 激活才开始，
  并行不相加，后继 Cycle 不重置。服务可用时正常研究等待和 HR 等待累计。
- 已提交的人类前台控制统一同步计时：暂停、取消、放弃或剪枝导致停止时停表，
  继续或成功切换到活动前台时接续；AR 的求助 suspended 不被解释成人工暂停。
  服务生命周期每秒检查点，干净退出停止；重启不扣离线时间。
- 异常退出保留最后确认用量，把无证据区间标为未知；旧任务同样保留历史缺证。
  不从进程启动时间猜余额。无限额度为 `null`；有限额度显示剩余和超时。
  人类可明确确认余额和依据，独立记录校正，不抹掉原确认用量与缺证历史。
- 五个研究根从真实 adapter/Harness 最终输入取得同源动态快照。
  新逻辑调用及继续工作刷新；同 operation 重放保留首次请求。
  动态观察不进入已冻结科学条件的 revision。Target 缺失冻结请求或合法终态时
  保留待对账状态，不为了刷新时钟重发已有作用。
- `advancement_engine.research_time.read` 是五研究根的按需读取入口；
  `GET /api/v1/quests/{quest_ref}/research-time` 向认证界面提供同一计算。
  正式共享指导、Target Skill、工具发现和 HR 按需说明同步，要求估计工作耗时、
  考虑依赖和资源、委派时共享约束、保留核对保管和交接时间，及早识别不足。
- 已批准时间耗尽或有限余额缺证时，AR 拒绝新的 Target admission。
  已接纳操作仍沿原身份对账；已有结果可保管和收口。Bundle 的新 dispatch 若仍
  违反时间约束，沿既有契约纠错把诊断送回原根；预算修订或跨越耗尽边界能触发新决策。
  不增加固定角色分钟配额、独立调度器、强杀或购买资源。

正式 HR kind 为 `research_time_extension`。首次申请/后继修订固化准确 Quest、
预算 revision 和系统观察；重放保留原快照。模型说明完成情况、尚缺工作、预计追加量、
不延长时的交付与不确定性。人类使用既有确认入口提交如下明确事实：

```json
{
  "decision": "provided",
  "facts": {
    "time_budget_decision": {
      "decision": "approve",
      "expected_revision": "<申请绑定的预算修订>",
      "additional_seconds": 7200
    }
  }
}
```

`additional_seconds` 与 `total_seconds` 二选一；新的总额允许零或 `null`（无限）。
缺证校正另需 `confirmed_remaining_seconds` 和 `correction_reason`。
HC 在同一 fenced transaction 应用额度并保存答复/回执；AR 复核已生效回执后
消费原 waiter。拒绝、延期、普通 provided 或闲聊不增加额度。重复回应一次生效，
旧预算修订原子拒绝，时间经过本身不制造修订冲突。

配置页修改数值并保存也是明确的人类总额确认，已用时间保留；高级原文里的时长描述
不会暗中改已批准额度。界面分别显示申请、答复、额度生效及原根消费事实；
消费回执不被表述为后续模型已经执行。后续实际工作须看执行记录。

## 专项与环境

工程测试使用冻结源文件归档、逐文件 SHA-256 和隔离 Linux 数据目录，
Python 3.12.13、真实 `fcntl`、项目锁定依赖。既有部署的 Python 环境仅只读复用，
测试未写生产数据或启动生产服务。前端使用本工作树独立 `npm ci` 安装的锁定依赖。
`npm run build`（含应用和 E2E 类型检查）成功，正式 `web_dist` 已重新生成；
`compileall` 与 `git diff --check` 通过。构建保留既有大 bundle 提示。

本地原始证据根为 `D:/meta_research/test/Funtation_model/.issue-293/`。
`engineering/runs/<label>` 包含 source manifest、环境、日志、JUnit 与退出结果；
不同批次不能相加冒充一次同版全套。

| 专项 | 实际结果与证据 |
| --- | --- |
| 时钟、无限/零/超时、HR等待、停机/崩溃、回拨、正式结束 | `clock-final01` 39 passed；`clock-ending02` 2 passed |
| 人工控制切换/取消/放弃、暂停恢复 | 审查修复后 `clock-control-regression01` 18 passed |
| 旧人工停止任务的缺证恢复 | `clock-legacy-green01` 2 passed |
| HR 确认、拒绝/延期、同根消费、重复、旧修订、校正 | `hr-final01` 13 passed；后继 handoff/revise 分别 `hr-handoff-green01` 3、`hr-revision-green01` 2 passed |
| 五根最终请求、新调用/重放/继续 | `model-regression02` 67 passed；追加缺失冻结输入/终态保护后 `model-integrity03` 47 passed |
| 认证 HTTP 时间读取与配置原子写入 | `settings-green02` 5 passed |
| 新 Target 门槛、同根收到拒绝并收口、准入瞬间跨零 | `budget-dispatch-green01` 3、`budget-dispatch-race01` 3 passed（合计覆盖当前4项准入测试及2项既有纠错测试） |
| 延时 UI、明确额度/校正、配置保存与重开 | 正式构建 `ui-final01` 16 passed |

最终生产代码的 10 文件联合专项 `final-special01` 为 **87 passed、0 failed**，
冻结归档 `a405f7aef98f998907df6dd69d0493ccf41a43c3c86daf68926ba724a31f51be`。
其中包含计时、HR、准入、五根上下文、配置、冻结恢复及旧库迁移。

红测试与实施失败均保留：缺失计时 API、暂停误扣、崩溃伪精确、HR 数据库 CHECK、
工具 schema、同时间戳答复排序、修订申请未冻结快照、handoff 缺预算说明、
Target 不完整请求恢复的重发风险、耗尽后 Bundle 拒绝未反馈、人工暂停后切换仍停表、
配置原文未变化时额度草稿丢失。也保留测试夹具自身错误（重复请求去重、DataRoot 当 Path、
UI accessible name 错误）；这些与产品红测试分开说明，不删失败后只展示绿色结果。

Python 全套 `full01` 冻结归档
`710bb3f01cf157935730847f10e08988d5c54b9cd7369938d6db64e47133d94c`：
4947 项，**4441 passed、504 failed、2 skipped、0 incomplete**，1447.65 秒。
这份全套在审查补修前冻结，不能改写成最终版本全绿。
7 批互不重复的基线重放覆盖全部 504 个失败节点：479 个在 `acc7399` 也失败，
25 个基线通过而候选失败。后者包括 23 个旧 Harness fake 未接收新增冻结 prompt 参数、
1 个共享指导原句断言、1 个旧 schema 的历史数据生成夹具；均已分别复测通过
（`model-regressions-green02` 24 passed、`hr-host-migration-green02` 1 passed）。
旧库测试只在生成 pre-0081 历史时停用旧版本不存在的计时钩子，实际迁移仍用真代码，
保留原保管行、外键、CHECK 和唯一性断言。

双方同节点失败也逐条核对：477 个是相同故障类别，另 2 个存在新错误遮蔽旧失败，
没有以“基线也红”豁免。新增预算工具的输出 schema 已明确闭合并通过独立合同专项；
legacy timeout 用例在修复 fake 后恢复为基线原有的 `DID NOT RAISE`。
其余基线失败仍是失败，不是正确性证明。精确节点、原文差异、每批源码哈希及后续绿色证据
见本地 `engineering/regression-audit.json`，由 `regression_audit.py` 重建。

前端 Node 全套为 40 passed、5 failed；基线导出在同一锁定依赖下也有相同 5 个
`MetaTrace is not defined` 失败。Windows 浏览器全套的真实应用部分遇到 `spawn uv ENOENT`，
其全套实际结果为 **173 passed、124 failed、1 skipped、4 did not run**，不能当作页面验收通过。
因此另在支持 `fcntl` 的 Linux、现有 Chromium 与核对后的锁定 Node 依赖上运行真实应用页面；
Windows 原失败证据保留。Linux 正式 Product HR deep-link 冒烟通过；CLI 启动类的后补环境验证
使用真实 uv、离线且不安装依赖、独立缓存，越过环境错误后 empty shell 断言仍在候选和基线同样失败，
见 `ui-linux-cli-smoke03` / `ui-linux-cli-baseline03`，不能把修好 launcher 当作页面已通过。

Linux functional 全套 `ui-linux-full01` 为 **205 passed、91 failed、5 skipped/not-run**；
同环境基线 `ui-linux-baseline01` 为 **203 passed、90 failed、5 skipped/not-run**。
两组均使用 Node 22.23.1、Chromium 1193 与核对过版本的锁定依赖，4 个 worker；
固定 Windows 字体的 manual raster 项目未纳入，不能声称跨平台视觉验收。
候选多出的 3 个测试中，2 个延时 UI 通过，1 个新配置测试未等待异步保存完成，
点击后立即读取请求数组得到 `undefined`。已按既有测试方式等待界面“已保存”后再核对
相同的 revision/总额断言；未改产品实现或放松断言。
补修后的 `ui-linux-final02` 联合配置及延时 UI 为 **16 passed、0 failed**，
归档 `5136f36c10f5109091d19a41db1c97f95756b10b206007f280c087d6763ed45a`；
E2E TypeScript 检查通过。全套原 91 个失败不被改写为重跑后的结果。

其余 90 个 UI 失败逐条对比：73 个归一化错误相同，17 个经主 Agent 核对为同类故障。
其中 4 个仅代码行号不同，7 个仅不可见控件的重试次数/稳定性探测不同，
1 个对话框缺少原有恢复错误提示、接收文字新增第六类别，5 个快照 revision 显示仍解析为
`NaN`（新增时间请求改变了预期计数，但两边均未读到有效数字）。基线失败不能证明页面正确。
原始错误、截图、trace、源码和比较表已完整收回本地，见
`engineering/ui-linux-comparison.json`、`ui-linux-audit.json` 及两组 `ui-artifacts.tar.gz`。

## Standards

独立初审未发现已记录规范的硬违反，发现 1 项有行为后果的启发式问题：
计时对人工前台生命周期的重复解释遗漏了暂停后强制切换，研究已 active 而时钟仍 paused。
已把同步集中到既有成功控制事务的共同出口，并补公开控制回归；独立复核确认关闭。

## Spec

独立初审发现 2 项：预算耗尽时错误 dispatch 会反复被 admission 拒绝而原根收不到诊断；
界面仅凭 waiter consumption 宣称实际接续工作。前者已复用原契约纠错和 provider feedback，
后者已只陈述消费事实。独立复核确认修复，保留原操作和正式科学状态。

初审计数：Standards 1 项（P1），Spec 2 项（P2）；这三项的独立复核均已关闭。
其后的全套夹具/schema 补修及最后一行 UI 测试等待由主 Agent 核对，
追加独立复核因子 Agent 额度耗尽未完成，不把前一轮结论扩写成最终补修已获独立审查。

## 交付边界

真实模型使用 `gpt-6.1-sol`、CLI 0.159.0、生产 Bundle adapter/durable transport、
实际 MCP/Owner 与原生子 Agent；上游科研及 conformance 用确定性夹具建立，
人工批准为明确标注的受控验收输入。任务是同一组 60 独立组、1500 配对损失的合成分析，
分别给 90 和 1500 秒；不代表正式 Target 接纳或完整科研 Cycle。

第二组两侧共用源码归档
`64d2a25461094965aea434823b3e2449af44d2a1c02fffa1705653e27541d48f`。
宽裕侧直接完成组级不确定性、独立复算/审阅和交接；紧张侧暂缓结论、保存点估计，
自主申请追加 300 秒。真实等待增加 **6.001230 秒**消耗；普通回复保持待补充，明确批准
把总额 **90 → 390 秒**，只生效一次。原 run/root/native session 继续，第二次实际输入为
已用 128.985999、剩余 261.014001 秒。批准后有真实分析脚本、组级结果和独立子 Agent
审阅产物；两边点估计同为 0.023879235096078612，正确使用组作为独立单位。

但紧张侧 HR 落库时已用 **104.960496 秒**，已超原额约 15 秒，故“提前申请”不通过。
长网络中断也按政策计入；末次 Owner 观察已用 **763.107304 / 390 秒**，
超时 373.107304 秒，不能把交接文本较早的超时观察当最终值。宽裕侧最终
876.164132 / 1500 秒，剩余 623.835868 秒。真实工作取舍和批准后消费成立，不等于
及时申请及额度内收口的整个验收成立。
上述为 provider 完成时的查询；随后干净停表的持久确认用量分别为 763.113743 和 876.169658 秒。

据此修订正式共享指导，明确首次预计必要工作连同核对/交接/求助等待超出余额时先申请，
不要先完成试算和审阅再发请求；不增加固定配额或改变科学标准。第三组统一源码
`002d89bd39557e397069fccb4263de0881969e43d47a427246ce152a38f2cd75`
的生产文件与当前最终源码字节一致，但两侧首次调用都返回 `codex_usage_limit`，
没有模型工作或 HR，最终指导的真实行为复验仍未完成。六次尝试原始失败均保留，临时凭据残留为零。
详细输入、原生谱系、Owner/等待/批准/消费、实际文件哈希与环境见本地
`.issue-293/live/REPORT-final.md`、`live-validation-summary.json` 和 `runs/<label>/proof/`。

这份记录区分机制测试、真实模型行为和 #274 的最终同版完整研究链路。
任何受控 provider、初始研究夹具或人工验收批准都不被描述为模型自主判断。
源码与前端产物不代表已经部署；本次未修改真实 Quest 的额度或研究数据。
