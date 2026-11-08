# #263 诊断与修正链交付记录

基线：`origin/v1-test@ea421520434afcc32047ed35c937c64aa9cb9bbd`，本地分支 `codex/issue-263-diagnostics`。按 2026-11-09 读取的 [#263 当前重开范围](https://github.com/15253a/meta_research/issues/263) 和 [#262](https://github.com/15253a/meta_research/issues/262) 实施。#263 为 open、负责人 15253a、原生 blocked-by 为空；#262 为 open、无负责人。仅有的开放 PR #221 是独立 DeepFetch 工作；未修改负责人、Issue、PR、运行服务或研究数据。

实现使用独立 clone；来源 `.issue-291/source` 的未提交修改和未跟踪测试未被合入或改写。托管 worktree 调用未返回，改用独立 checkout；当前工作目录明确，不以原 dirty checkout 作为验证源码。

## 实际行为

- 公共网关保留原错误码；输入错误给出字段路径、合同要求、期望及修正方向。文本返回同一份 JSON 诊断。正常 Owner 业务状态不改变。
- handler 异常、返回值违反合同以及 effect 的业务错误保留原 operation/effect 和对账入口；无法确认的效果明确为 unknown。输入拒绝只说明本次 handler 未执行，不否定先前操作。对账使用原完整提交，保留授权、版本、幂等身份及有效工作。
- Bundle exhaustion 的提交、对账与提案解析保留真实 OwnerConflict；原提案冲突后可在同一通道对账既有 accepted 回执。
- Resident 宿主桥区分认证、协议、临时不可达和坏回执，保留 details；DeepFetch 的包装及既有失败事件/REST 查询继续携带诊断，重启不丢。诊断不参与重试许可或状态判断。
- 外部 MCP 参数错误与损坏 schema/引用分开；direct-call 和搜索源 receipt 保留具体原因及冻结操作定位。任意供应商错误正文不能当作安全字段直接回显。
- Writing 共用内容模型工具保留稳定 `content_model_call_failed`，区分凭据、权限、限流、服务、超时、连接和坏响应，给出配置方或调用者的实际处置；无自动切模型或重试。现有 Owner 包装保留已提供的 details。

没有新增通用错误平台、自动重试器、权限、迁移或原生 RW 包装。共享材料说明与 `tools.contracts.read` 同步当前错误合同。

## 逐工具覆盖与可复现证据

详见 [逐工具覆盖](tool-coverage.md)、[实际注册清单](registered-tools.json) 和 [清单生成脚本](collect_tools.py)。96 项内置操作全部登记；清单与公共包装验证不代表各 Owner 的每种业务拒绝通过。

所有 backend 行为验证在 Linux 新建隔离目录执行，Python 3.12.13、锁文件依赖精确一致，真实生产装配和临时 Owner 数据；部分供应商/Owner 故障受控注入，报告明确注明。原始日志保存在工作空间 `.issue-291/runs/`，没有修改 #291 旧目录。以下测试集有重叠，不能相加计数。

| 路径 | 修复前证据 | 修复后证据 |
| --- | --- | --- |
| 公共参数与原 effect 对账 | `263-gateway-red`：7 failed，缺 details | `263-gateway-last`：8 passed；含效果实际存在/不存在两种坏回执，均先报告 unknown 后对账 |
| 真实 notes/content、分页、撤权、业务拒绝 | 复用 #291/原 #263 行为；4 个旧断言需接纳新增诊断 | `263-gateway-final-focused`：16 passed |
| Bundle 原因与 Resident 桥 | `i263-bridge-red3`、`i263-bundle-red`：吞 details/原 code | `i263-bridge-final`：10 passed；原提案/原回执保持 |
| DeepFetch 持久诊断 | `i263-deepfetch-durable-red`：REST 缺 details | `i263-deepfetch-durable-green`：worker→REST→重启保持原 run/attempt/session，Provider 仅调用一次 |
| Idea 原授权通道 | `owner-idea-red-263`：Owner details 丢失 | `owner-idea-green-263`：纠正引用后继续、原注册结果对账；Owner 故障为受控注入 |
| Writing 实际 Companion grant | `issue263-content-config-red`：配置缺失无方向；其他网络分类 red 记录保留 | `issue263-content-config-green`：27 passed；恢复原配置/服务后同通道继续 |
| 外部 schema 与 DeepFetch source receipt | `issue263-external-red`、source red：误分类/吞因 | `issue263-source-green`：5 文件 88 passed；最终增强 oneOf/anyOf 与上游失败处置后 `issue263-external-details-green`：50 passed |
| 双轴审查后的修复 | `263-review-red`：8 failed；`263-numeric-red`：3 failed | `263-review-final`：5 文件 78 passed；RPC 内部错误、内容 HTTP 分类、动态键安全、注册数值边界均验证 |
| Idea 原生适配器后续消费 | 全套候选发现新增 details 导致旧整对象断言失败，原错误码没变 | `263-idea-regression-fixed`：2 passed，验证原 code、具体原因与文本/结构化一致 |

## 基线复用与完整验证

#291 `full-final` 的 936 个 `src/`、`tests/` 文件与本次基线 `ea42152` 的 Git 字节全部一致。旧 manifest 的 HEAD 是 b01d6c4，但冻结的修改随后提交为 ea42152；按内容核对，未使用当前 dirty 工作树推断基线。其全套为 4756 collected：4271 passed、483 failed、2 skipped。480 个失败在更早基线复现；另 3 个竞态记录及未知因素保持，不改写为通过。

全套运行冻结初始候选 `787bf71`（源码归档 SHA256 `f032bad4c4b94a145798c726b6aaeec009f29894762fbbacacdd8820cf5bf1eb`）。审查后 4 个运行文件的窄修复另以 `263-review-final` 的 78 passed 复验（归档 `3816b875d55d16dd725ebe2025a454dad9d733393311e3bb436f64dc093bcab6`）；不能把初始全套快照说成包含之后修复。双轴初审发现及复核分别见 [代码审查](review.md)。Python 项目没有配置 mypy/pyright 或 backend typecheck 命令；编译检查不冒充类型检查。未改前端源码。

全套完成 **4787 collected：4303 passed、482 failed、2 skipped、0 incomplete**，532 个测试文件全部完成，1156.35 秒，无超时和收集错误。与 #291 精确节点/阶段对照：429 项失败阶段及诊断相同，52 项同阶段但诊断变化；1 项新增失败是 Idea 旧整对象断言（已修正，整文件 2 passed）。另 2 个旧竞态节点本次通过，保留旧失败，不据此宣称竞态已修复。两项外部 schema 参数化测试因错误码纠正而改变 node id，实际场景仍执行，未删除覆盖。

Bundle `same-native-new-call` 从旧 hard-ceiling 竞态推进到缺少 `Fresh pagination guidance` 的断言，不归作随机文本差异：#291 `bundle-candidate-recheck` 在与本次起点全部 936 文件一致的快照 `f654a29678955eb6ae19bda7fb968aa6132a14e7ecc33f7fc87875544d6843d3` 已复现同一后续断言。两种历史失败均保留，未测量竞态发生频率。

最终 373 个运行文件与审查后 78 passed 的冻结快照逐字节一致；Idea 测试调整另有 2 passed。**没有把全套原始 482 个失败改写为通过，也没有宣称最终修复后重新运行了全部 4787 项。** 精确对照和修正处置见 [验证记录](validation.json)；完整原始日志、JUnit、phase 对照在工作空间 `.issue-291/runs/263-full-final/` 与 `.issue-263/full-validation/`。

## 未验证与限制

已证明同一授权入口的参数/引用修正、业务拒绝、先对账、保留结果和后续调用；没有声称真实模型已自主读懂并改正提交。最终同版本真实角色/模型消费仍需 #274；接口探针不替代该验收。

已只读核对实际 8770：当前无已配置外部 MCP 或 supplemental 搜索源，但其运行源码为另一版本且保留已有修改。其 managed native home 没有现存 sessions／threads／dynamic-tools，离线 CLI 协议不提供完整 native catalog；最终修复版各角色原生工具完整目录仍未取得。详情见 [实例元数据](target-8770-inventory.json)。真实供应商故障处置、全部角色独有恢复、原生文件/命令/子 Agent 逐项实跑仍未验证。适用但未验证项保留，不算通过；Spec 审查因这一范围缺口仍有 1 项 P1。本次不关闭 #263/#262/#274，不部署。
