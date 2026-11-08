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
| 公共参数与原 effect 对账 | `263-gateway-red`：7 failed，缺 details | `263-gateway-last`：8 项；含效果实际存在/不存在两种坏回执，均先报告 unknown 后对账 |
| 真实 notes/content、分页、撤权、业务拒绝 | 复用 #291/原 #263 行为；4 个旧断言需接纳新增诊断 | `263-gateway-final-focused`：16 passed |
| Bundle 原因与 Resident 桥 | `i263-bridge-red3`、`i263-bundle-red`：吞 details/原 code | `i263-bridge-final`：10 passed；原提案/原回执保持 |
| DeepFetch 持久诊断 | `i263-deepfetch-durable-red`：REST 缺 details | `i263-deepfetch-durable-green`：worker→REST→重启保持原 run/attempt/session，Provider 仅调用一次 |
| Idea 原授权通道 | `owner-idea-red-263`：Owner details 丢失 | `owner-idea-green-263`：纠正引用后继续、原注册结果对账；Owner 故障为受控注入 |
| Writing 实际 Companion grant | `issue263-content-config-red`：配置缺失无方向；其他网络分类 red 记录保留 | `issue263-content-config-green`：27 passed；恢复原配置/服务后同通道继续 |
| 外部 schema 与 DeepFetch source receipt | `issue263-external-red`、source red：误分类/吞因 | `issue263-source-green`：5 文件 88 passed；加强 gateway 入口后 `issue263-source-gateway-valid`：27 passed |

## 基线复用与完整验证

#291 `full-final` 的 936 个 `src/`、`tests/` 文件与本次基线 `ea42152` 的 Git 字节全部一致。旧 manifest 的 HEAD 是 b01d6c4，但冻结的修改随后提交为 ea42152；按内容核对，未使用当前 dirty 工作树推断基线。其全套为 4756 collected：4271 passed、483 failed、2 skipped。480 个失败在更早基线复现；另 3 个竞态记录及未知因素保持，不改写为通过。

本轮全套、与旧证据对照、双轴审查和最终快照记录在本节收口。Python 项目没有配置 mypy/pyright 或 backend typecheck 命令；编译检查不冒充类型检查。未改前端源码。

## 未验证与限制

已证明同一授权入口的参数/引用修正、业务拒绝、先对账、保留结果和后续调用；没有声称真实模型已自主读懂并改正提交。最终同版本真实角色/模型消费仍需 #274；接口探针不替代该验收。

8770 的最终 Harness 原生工具完整身份及真实外部配置尚未取得。只读检查了明确命名的 8771 安装，不能视为 8770 验收；真实供应商故障处置、全部角色的独有恢复分支、原生文件/命令/子 Agent 工具逐项实跑仍未验证。适用但未验证项保留在覆盖表，不算通过。本次不关闭 #263/#262/#274，不部署。
