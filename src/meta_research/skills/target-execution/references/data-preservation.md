# 数据保存

获取与整理本身可以是 Target 的研究工作。按研究价值、替代成本和预计复用决定保存源材料、处理结果、划分或标注；保留有价值派生物时尽可能保存稳定源副本，并在研究说明记录来源、处理和局限。版本及关系应有研究意义，无需逐文件或逐中间步骤登记。

人类答复、主动资料与 Agent 自取材料的读取、工作副本和选择保管按[材料与保管](../../references/materials-and-custody.md)处理；人的提交及影响判断沿[人类材料与研究用途](../../references/human-guidance.md#人类材料与研究用途)。`materials.copy` 的副本及临时结果留在 `.work-materials/`，只有明确选择才保管；需要正式研究产物的内容沿下述 Target 交接声明实际工作与精确路径。后继读取所选材料的精确 reader 与原用途，不能把未选副本因目录位置带进自动产物范围。

选定数据通常放在 `outputs/data/`，checkpoint 状态通常放在 `outputs/checkpoints/`；其他工作区位置或不同角色用结果中的 `retained_artifacts` 明确选择，其精确归属与修订方式见[正式工作交接](formal-work.md#实现快照状态与局部输入)。实现、分析和日志沿现有交接路径保存。按 `execution_contract.artifact_limits` 核实本次行为；正常交接会流式保存大型文件和目录，无需另填大小模式。多 GB 本身不要求拆成小压缩包或请人导入。各 Run 的数据归入 `formal_runs[].artifact_paths`，评价报告归入对应评价，需独立复用的生产者和状态分别寻址。

最终交接前完成写入，并保持所选路径稳定至 Owner 接纳。持久保存由 Owner 接纳的精确版本与内容 receipt 证明；说明中的路径、hash 或单独 Dataset／Environment／Literature 登记都不能代替。暂存、未接纳材料留在工作区。原始数据、派生数据、文字、湿实验记录和辅助材料走相同保存路径。

Target 的候选交接见[正式工作交接](formal-work.md)；接纳后由 Bundle／Reasoning 按[资源整理与正式登记](../../references/materials-and-custody.md#资源整理与正式登记)选择值得复用的 Dataset、Environment、Literature 和用途。文献原作保持其外部来源，本次阅读与分析按实际工作归属保存。源版本、派生版本及多个来源的真实处理分别关联，核对精确版本、当前用途与原件；关系不复制原件，也不自动构成科学结论。

编辑 `linked_local` 等已保管材料时，按[阅读、工作副本与保管选择](../../references/materials-and-custody.md#阅读工作副本与保管选择)保留原件并另建工作文件；保留与清理按[交接与工作区收尾](../../references/materials-and-custody.md#交接与工作区收尾)处理。

Target 交接只结束当前工作，后续 Bundle／Reasoning 仍可按授权使用本轮资料。跨 Cycle 接续、正式内容保留及物理清理统一按上述共享生命周期处理，核对执行、恢复和待接纳等真实依赖。

获取、保存或相关研究判断需要人的专业知识、判断、权限、资源或行动时，按[主动发起与接续 HumanRequest](../../references/human-guidance.md#主动发起与接续-humanrequest)及时请求协作，并沿其等待与恢复规则安排独立工作。真实获取或保存失败应说明当前已保存位置、缺少步骤和继续条件。
