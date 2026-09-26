# 系统 MCP：公共 Provider 接线与历史绑定验证

2026-09-26。本文只记录公共 Provider 子任务的验证；真实 Codex 工具调用、HTTP、图像、故障隔离及子会话继承由整体原生验收报告记录。

## 生产入口

`composition.py` 为九种默认 Codex 根入口传入同一个 `SystemMcpRegistry(data_root.root / "system-mcp.json")`。下列七种使用 `idea_skill.py` 的公共调用实现。各子类原有真实根类型保留；Acquisition 的内部适配器仍为 `acquisition`，默认 Quest 草拟和 intent 仍为 `companion`。

| 根类型 | 已执行的公共接线测试 | 覆盖的调用 |
| --- | --- | --- |
| idea | `test_idea_root_invocation_uses_and_revokes_its_operation_tree_channel`、三个 `test_system_mcp_*` 参数实例 | primary、review、原生 continuation、未决恢复、历史无字段恢复 |
| plan | `test_production_adapter_uses_one_native_root_with_advisory_finalization` | primary、review/resume |
| bundle | `test_completion_rejection_feedback_reaches_bundle_primary_prompt` | 已有原生会话的 successor primary |
| reasoning | `test_production_adapter_uses_one_session_and_scoped_resident_mcp` | primary、review/resume |
| writing | `test_type_specific_skill_resource_uses_the_same_resumable_session_seam` | paper、presentation 的 primary、review/resume |
| acquisition | `test_quest_bound_acquisition_batch_uses_exact_resident_operation_tree` | 批次结果交给真实 Acquisition 根适配器 |
| companion | `test_companion_proposal_fork_returns_public_content_without_replacing_root_session`、`test_pre_quest_companion_turn_remains_without_resident_channel` | proposal fork、无内部 semantic 通道的 intent reply |

范围 fixture 使用权威 `ROOT_AGENT_KINDS`：同时登记 all、仅当前根类型、其他根类型三个服务，检查实际运行器边界中的配置只包含前两项。原有内部 MCP 凭据、作用域及撤销断言仍运行。Target、DeepFetch 的接线与 segment 恢复另由 `test_system_mcp_execution.py` 验证。

## 操作与历史合同

新 durable 操作把 `system_mcp_snapshot` 加入原有签名 v3 invocation。这个字段缺失仍是合法历史合同：读取时不补写当前注册表，不修改原 invocation hash 或 supervisor argv。恢复从已签名操作取快照，不读取可变注册表；下一逻辑操作才读取最新配置。没有内部 semantic 通道时仍装载系统 MCP。

已通过的恢复回归实际启动测试 supervisor：在原生进程启动前模拟 daemon 崩溃，移除注册项并损坏注册表后恢复。原 `invocation.json` 与 `supervisor-request.json` 字节保持相同；重复对账不重跑；完成后沿同一 native session 开始新操作，不残留已移除服务。新增操作面对坏注册表明确失败，未静默当作空配置。

## 部署前绑定兼容

公共 Provider 的长期 Run 绑定原本包含实现文件和 instruction 的哈希。仅保持 spool 字段兼容不足以跨本次部署：原有执行门会先拒绝旧绑定。

`system_mcp_binding_compatibility.py` 对本次审阅过的精确 before/after source 与 instruction 组合提供执行兼容。模型、工具权限、内部 MCP、输出 schema、CLI 可执行文件、transport key 和其他资源均继续逐项精确比较。新 binding 仍显示真实新源码哈希；既有 Run、Session、receipt 和 operation 身份不改写。此处不把注册表修订或可变配置纳入长期绑定，也不接受未知源码版本。

- 原始身份从未修改的远端 `meta_research/src` 独立捕获，见 `tests/fixtures/system_mcp_binding_before.json`；八个源码哈希逐一核对等于快照 commit `ebc1cc9`。
- 新身份从最终源码捕获，见 `tests/fixtures/system_mcp_binding_after.json`。可使用 `tools/capture_system_mcp_binding_revision.py` 在各源码的 `PYTHONPATH` 下复核。若再修改相关源码，必须重新审阅并更新精确 after 组合。
- Idea 的历史恢复测试以及 Plan、Bundle、Reasoning、Writing 的上述公共执行测试使用真实 baseline instruction/source 身份；改动 capability 后仍被拒绝。
- Acquisition 将公共根 binding 的哈希嵌入外层 provider binding。其兼容钩子同时精确保留 delegate、能力集合与其余外层字段；Owner 只在该钩子确认时继续使用已保存 Session binding。

`tools/verify_system_mcp_acquisition_upgrade.py` 在同一临时工作目录先由 baseline 源码 `capture`，再由最终源码 `verify`，已得到：

| 检查 | 结果 |
| --- | --- |
| baseline 完整 Acquisition binding hash | `d01618d0ed1fdc70be0ee3fb7d0dbc0c7d7e61e67c16b0e95cddfe3874ae08d7` |
| 新完整 Acquisition binding hash | `c1d6fa3426364ebab23892ad24a82737fd2ba0dbe4530dbf56edd1ff2cd2d5b9` |
| 精确旧 binding 可以继续执行 | 通过 |
| delegate 或 capability 被改动时拒绝兼容 | 通过 |

上述脚本仅使用临时目录和确定性 delegate，不打开生产数据库、不下载材料、不调用设备。Target、DeepFetch 的长期 binding 不包含此次改动的实现文件哈希，不需要此兼容例外。
