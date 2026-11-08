# #263 双轴代码审查

固定点 `ea421520434afcc32047ed35c937c64aa9cb9bbd`，最终运行源码候选 `beef1479d46b8639c747a9edbb54e78d9412d2b9`。按 code-review skill 分别由独立 Standards／Spec Agent 审查；初审及修复复核均保留，不把发现过的问题改写为“从未发现”。最终证据文档与源码分开提交；源码／测试内容身份由实际归档 SHA256 核对。

## Standards

最终固定源码复审：**0 项未解决发现**，硬性规范违例 0，需要整改的代码异味 0。未发现新增通用错误平台、自动重试器、迁移或授权扩张。

初审与后续复核发现并修复：

- 外部 schema 的动态键可能回显秘密：仅保留声明字段／数组位置，动态键遮蔽，包括嵌套 oneOf；原始提交和供应商正文不直接作为诊断。
- 内容 HTTP 404／405／408 等处置方向错误：按配置、权限、限流、服务、超时及未知结果给具体方向，保留所选模型和稿件。
- 数值合同未先验证类型／边界顺序，字符串长度显式 null 仍可能在调用期抛错：注册阶段拒绝错误合同，有实际 red/green 回归。
- 原生探针初始化异常可能留下临时凭据：初始化纳入 finally；伪凭据失败也核实残留为 0。
- 新增 11 处 effect 前置诊断在查询原效果前错误声称 not_recorded：最终改为本次 call_execution=not_started、原 effect_status=unknown，并先按原完整提交对账。`263-effect-prior-state-green` 全 10 passed；真实资产接纳后同 effect 畸形提交仍能原参数对账并保留原内容。Material acquire 的已知 not_recorded 位于原效果查询之后，未机械改成同类问题。

## Spec

最终源码、全量工具表及真实模型记录复审：**0 项未解决发现**。最终全套的旧失败／新增回归判别以 [validation.json](validation.json) 为准；源码存在、注册观察、接口用例和实际模型行为分别记录。

此前缺口已完成：

- 工具范围：96 个唯一内置身份与表中 96 行逐项相等，缺失 0；另有 26 个实际跨角色原生身份。13 个装配变体、延迟工具、Recorder 与创建材料特例均登记；现场外部配置为 0。
- 诊断传播：公共文本／结构化 parity、Bundle 原码、DeepFetch→Acquisition 桥、持久失败事件、Writing 分类与外部 schema 误分类均有对应修复证据。
- 独有分支：Dataset、Plan evidence、Implementation content、Question／HumanRequest／Creation／Stage reader 包装保留 Owner details；条件参数及分页提示使用真实公开字段。具体 S1/P1/O1 等索引对应实际通过节点，未把每个 Owner 的全部业务状态说成已穷举。
- 原消费者：独立核对最终 `beef1479` 的 945 个源码／测试 hash，差异 0。同一真实原生会话 `01a1bc62-b09a-7932-8c27-8b8ad0fd8d36` 完成过期引用→目录→精确读取，以及一次真实登记→坏回执 unknown→原 effect 对账→文献回读；7 个 CLI 工具返回与 HTTP、文本和结构化结果一致，写入及 Owner 调用各 1 次。
- 反馈长度：两条实际失败的完整文本为 285／563 字符，保留具体原因及必要对账定位；未用大段 schema 代替诊断。

初态 fixture、受控坏回执及真实模型决定的后续调用明确区分。#269 的分层发现与 #274 的组合科研 Cycle 不冒充本票已实现行为，也不用于掩盖本票工具错误验收缺项。

Standards：0 项未解决；Spec：0 项未解决。完整测试与最终发布状态见 [验收记录](README.md)。
