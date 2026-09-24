# Bundle 的 Owner 操作

读取、候选接纳、执行调度、恢复和收口均使用当前 scoped 工具目录与参数 schema；本文件描述职责，不发明可直接调用的伪函数名。合同和产物语义见[Bundle 契约](contract.md)，Target 的字段由实际注入合同提供。

## 职责

AE 管阶段 request、epoch、BundleReport 与 StageCommit；RG 管 FormalPlan、Target／依赖／frontier、方法、Run／评价及领域接纳；RM 管不可变内容、AssetVersion、保管和完整性；AR 管执行、根会话、Fence、single-flight 和恢复；Harness 运行原生工具与子智能体。Bundle 管候选、范围、顺序和跨 Target 依赖，Target 根管实际实施与最终交接。

子智能体在明确任务及所授权限内读取、分析、实施或整理，按文件／对象划分写入，返回结果、精确来源和自由格式反馈。整理已接纳材料可沿已授 Dataset 接口登记；未接纳 Target 中间产物仍留工作区，不提前进入 RM／RG 正式内容链。

## 正常流程

1. 观察当前 Bundle 请求及运行绑定，读取精确 FormalPlan、内容 hash 和直接绑定它的 current receipt。
2. 提出科学候选和真实依赖，由 RG 接纳身份和 spec；重读其内容绑定及 current frontier。
3. 仅对未启动、ready 且获授权的 Target 发起 admission／dispatch。claim 验证 spec、输入、资源、授权和 Harness，并确保至多一个 current 根。
4. 已执行工作沿原 TargetRun 观察、wake 或 reconcile；wake 只带重新读事实的信号，不携带研究结果作为权威依据。
5. Target 形成稳定交接后，由 AR／RM／RG 核验、保存并接纳真实 Run、产物和适用评价；Bundle 读回 TargetCommit 后更新覆盖。
6. 独立审阅最终策略及交接，提交真实 BundleReport 候选，由 AE 验证阶段收口。

根与系统的执行分工不要求 Agent 重建 receipts、固定内部调用签名或 daemon 私有流程。仅凭 Session 完成、退出码、日志标签和页面进度不能认定正式科学结果。

## 恢复、取消和反馈

同一幂等身份仅对应同一 payload，已冻结交接仅查询原 identity，不从活动工作区重新 capture。取消需授权、投递与真实确认，迟到事件保留为历史观察，不形成新 current effect。Host pause／resume 沿权威控制事实进行，Agent 不读取或修改私有 SQLite、spool、密钥或控制文件来伪造恢复。

| 结果 | 动作 |
| --- | --- |
| `accepted` | 保存精确引用并重读 frontier／coverage。 |
| `rejected` | 读具体字段与原因，由相关根在来源链上修订后继，必要时升级语义修改。 |
| `stale` | 重验 request、spec、输入、claim 和 handoff，不切到 latest。 |
| `needs_input` | 绑定具体 HumanRequest，待 Owner satisfied 再继续；回复存在不自动等于满足。 |
| `outcome_unknown` | 对账原 identity，明确结果前不重放。 |
| `technical_blocker` | 保留具体范围和恢复条件，可修复则沿同一工作继续。 |
| `idempotency_conflict` | 保存冲突并停止该写入。 |
| `already_accepted` | 验证同 payload／hash 后复用原接纳事实。 |

认证 Web 展示有界事件、当前／历史身份和连接状态；断线、重连、日志缺口或重复不改变研究事实。正式结果仅来自 Owner 验证的接纳链。
