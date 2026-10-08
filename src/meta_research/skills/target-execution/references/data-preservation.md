# 数据保存

获取与整理本身可以是 Target 的研究工作。按研究价值、替代成本和预计复用决定保存源材料、处理结果、划分或标注；保留有价值派生物时尽可能保存稳定源副本，并在研究说明记录来源、处理和局限。版本及关系应有研究意义，无需逐文件或逐中间步骤登记。

选定数据通常放在 `outputs/data/`，checkpoint 状态通常放在 `outputs/checkpoints/`；其他工作区位置或不同角色用结果中的 `retained_artifacts` 明确选择，其精确归属与修订方式见[正式工作交接](formal-work.md#实现快照状态与局部输入)。实现、分析和日志沿现有交接路径保存。按 `execution_contract.artifact_limits` 核实本次行为；正常交接会流式保存大型文件和目录，无需另填大小模式。多 GB 本身不要求拆成小压缩包或请人导入。各 Run 的数据归入 `formal_runs[].artifact_paths`，评价报告归入对应评价，需独立复用的生产者和状态分别寻址。

最终交接前完成写入，并保持所选路径稳定至 Owner 接纳。持久保存由 Owner 接纳的精确版本与内容 receipt 证明；说明中的路径、hash 或单独 Dataset／Environment 登记都不能代替。暂存、未接纳材料留在工作区。原始数据、派生数据、文字、湿实验记录和辅助材料走相同保存路径。

Target 接纳后，Bundle／Reasoning 在同一整理环节用精确 RM binding 登记值得复用的 Dataset 版本、Environment 和用途；Target 的候选交接见[正式工作交接](formal-work.md)。以 `research_graph.datasets.derive` 关联源版本和派生版本，说明真实处理；多个来源分别记录。关系保存来源，不复制原件，也不自动构成科学结论。用 `research_graph.datasets.page` 的 `dataset_version_ref` 和 `direction` 读回来源或派生关系，并核对精确版本、当前用途引用及原件。

`linked_local` 版本引用稳定原文件，不构成备份。保留其底层副本，清理前核实是否是唯一副本或仍被已接纳版本使用。已有版本保持不可变，有意义的内容变化沿当前 intake 形成新版本。

Cycle 业务完成且必要交接结束后，临时工作副本才进入系统清理范围；具体清理条件见根系统提示。Target 交接只结束当前工作，后续 Bundle／Reasoning 仍可使用本轮工作资料。交接前选中需要长期保留的原件与处理结果，核验 RM 精确版本真实可读；仍用于执行、恢复、待接纳或后续工作的路径继续保留，linked_local 外部原件保持原位置。

仅为具体无法访问的来源、缺少权限、额外资源或必须由人执行的动作提出 HumanRequest；同时推进已授权的独立工作。真实获取或保存失败应说明当前已保存位置、缺少步骤和继续条件。
