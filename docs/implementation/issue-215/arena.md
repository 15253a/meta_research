# #215 Arena 选择摘要

本记录只说明已发生的设计与代码选择，不是新审查或实际环境验收。原始方案与判定保留在本地交付工作区；以下本地链接不会随仓库发布其原件。

## 设计选择 A

父代理与独立 GPT-6-astra judge 均给设计 A 18/20、B 16/20，选择 A。A 让独立来源 registry 管理配置、选择、HTTP 行为和证据，并复用现有 MCP 封存与传输。B 将网站和 API 执行也放入 MCP 执行器，改动边界更大。

从 B 采用两个局部补充：按实际返回论文的身份及版本核对来源回执，保留历史论文台账 schema 的原解释。确认时持久保存来源 basis、延迟迁移与中断后的清单恢复属于同一实现责任，不合并两套架构。

原证据是[设计选择](../../../../../.scratch/implementation-215-20261010/design-selection.md)与[独立设计判定](../../../../../.scratch/implementation-215-20261010/design-judge.md)。设计评分说明方案完整度，不证明实现通过测试或外部服务可用。

## 代码选择 A

独立 registry 代码 judge 给 A 15/20、B 13/20，选择 A。A 保留 registry 签发的 basis、论文结果性质检查和准确版本匹配。owner 的完整比较给 A 15/20、B 14/20，胜出方案相同。

在 A 上采用 B 的编码值与字典键脱敏、每次 Run 清单发布锁，以及失败 MCP 与正常 API 并存时继续执行的反例。Crossref 另按已核验合同修正：只有返回注册 DOI、已知学术出版类型与有效出版日期，才确认 `published`；自定义 API 或不透明 MCP 结果不获得默认出版版本。实际主机探测促使查询合同使用 `query.title`。

原证据是[代码判定](../../../../../.scratch/implementation-215-20261010/code-arena-judge.md)、[实现负责人记录](../../../../../.scratch/implementation-215-20261010/owner-progress.md)与[执行记录](../../../../../.scratch/implementation-215-20261010/workflow.md)。候选初期静态检查与 Windows 缺依赖不算行为通过；选定代码的后续冻结 Linux 结果见 [units.json](units.json)。

## 模型与验证限制

Claude 实际调用因 403 额度限制未产生判定。代码 Arena 使用记录在案的 GPT-6.1 fallback，设计判定使用 GPT-6-astra。二者都不能替代 Show Me Your Work 最终要求的另一模型家族轨迹审查，该项仍为 pending。此摘要不复制完整候选、trace 或会话。
