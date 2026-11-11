# #295 稳定性修复与终审

最终应用源码为 `7753d56d329a7c0c9a744200810307384aa49433`，修复基线为 `7746456`。
本记录后的文档提交不改变应用源码。#295、#274 的整体验收仍保持开放。

## 修改结果

- Reasoning 检查点记录实际生成结果的 provider unit。人工回复后按原 operation 的冻结条件完成，在当前授权下恢复，并清理正确的原 job。来源解析复用现有 `read_snapshot`，避免并发替换时混读。
- HR 按请求隔离损坏记录。健康回应与新回应仍可预览、重投和完成；未知旧记录显示诊断。解密验证、孤儿清理、成功确认均保留仍可达的数据。存储整体故障仍拒绝操作。
- Plan 正文复用保留 RG/RM 的 `reviewed_draft_hash` 与 `review_hash` 关联校验。
- 自动创建读取条件时借用既有 Database，不自建或关闭调用方实例。

## 验证结果

| 受验范围 | 结果 | 源码与证据 |
| --- | --- | --- |
| 32 个后端文件 | 249 收集，222 通过，27 失败，0 跳过或未完成 | `60661a3`，`fix295-integrated01` |
| 对上述失败逐条跑基线 | 27 个失败全部复现；13 条消息相同，14 条均错误地固定要求旧迁移 head 0038 | 原始 `7746456`，`fix295-baseline-failures01/02/03` |
| 最后并发修复的受影响路径 | 22/22 通过，包括来源、真实 HC 暂停恢复、supervised stop、HC 并发 | `fix295-cc-followup-green01`；1324 个 Git 文件与 `7753d56` 全部同哈希 |
| 真实 Chrome | 46/46 通过；32 存储、9 恢复 UI、5 Quest 材料 | 最终前端字节未再改变；应用和 E2E 类型检查、Vite 构建通过 |
| 原始复现 | HR/no-HR Reasoning 两例通过；坏 A 不再阻断 B/C 到达 HTTP | `fix295-original-reasoning-final`、`fix-green-browser` |

计数有重叠，不能相加。249 例不是全绿，也不是最终 22 例的替代。
正常 Plan 消费实测为一次正文读取、一次解析。自动创建实测为零新 Database、零由新实例产生的连接、零 close；这不等于零连接池借用，也不代表延迟提速。

修改前的原始错误包括 `guidance_provider_unit_unbound`、正常 B/C 在发出 HTTP 前被 A 的 `human_request_recovery_sealing_key_missing` 阻断，以及篡改 Plan 关联后 `DID NOT RAISE`。
最后的快照并发负例先得到 `11 passed, 1 failed`，修复后对应完整选择得到 `22 passed`。全部失败证据保留，没有修改断言来掩盖行为缺陷。

## Claude Code 终审

实际本机模型为 `claude-opus-5-5`。第一次交互审查读取 47 次源码和证据后因额度 403 中断，未计作完成。随后同一模型完成集中源码审查，提出有条件通过。
复核保留了现有必需的来源非空检查，拒绝无收益的重复断言；补齐一个已有快照边界和三个并发／恢复行为测试。最后模型对 `7753d56` 复核通过，`unresolved` 为空，审核期间源码不变。
最后两轮以明确边界的源码审查包输入，未由 Claude 执行测试或自行重算文件哈希。测试执行与哈希核对由父任务完成。
原始最终结论见 [claude-review.json](claude-review.json)，精确计数和哈希见 [verification.json](verification.json)。

## 取舍与边界

历史检查点若没有可证明的来源，继续保持可读，但不猜测来源、不补写旧证据、不重跑 provider 来伪造恢复。新写入必须有合法来源。
存在损坏或无法识别的浏览器原件时，孤儿清理会保守延后。长期不处理会增加本地存储占用；不能靠清空恢复存储来消除诊断。
保存后的 primary 继承由真实内部签发边界覆盖。实际 stopped-primary 恢复明确排除已有 primary；真实 HC 暂停／恢复与保存前的受控进程停止另外有测试。没有把内部签发测试描述成真实停机继承。
使用确定性 provider 与受控 HTTP。未部署、未运行真实科研 provider、未操作研究数据；不替代 #274 的真实科研链验收，也不包含 #296 工作目录改造。

## 实际采用的原则

- Laziness Protocol。只保存一个来源引用，复用既有快照，拒绝重复非空断言。
- Model the Domain。区分历史生成来源与当前授权；用 ready/failed 记录表达单请求恢复结果。
- Boundary Discipline。隔离记录损坏，保留存储整体故障；不吞未知异常。
- Make Operations Idempotent。保留原操作、冻结输入及原回应字节，重入不重新生成。
- Fix Root Causes。追到逻辑 invocation 与实际 continuation 的身份差异，以及清理早于验证的顺序问题。
- Prove It Works。原始复现重跑于同一执行面，并核对最终源码快照。
- Test Behavior, Not Implementation。断言正式接纳、精确正文、原 job 清理、原子拒绝及 HC 拒绝落行。
- Build the Lever。保留可重跑的源码冻结、浏览器复现和基线差分工具。
- Sequence Work into Verifiable Units。失败测试先提交，修复按独立边界提交。
- Explain the Number。区分新建连接与借用连接，分别报告重叠测试与基线失败，不宣称测速结果。

详细本地原始日志保存在工作区外的 `issue-295-deep-review-20261111` 与 `issue-295-batch-b-evidence/env/runs`。决策记录见 [decisions.tsv](decisions.tsv)。
