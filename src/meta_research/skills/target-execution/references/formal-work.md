# 正式工作交接

在多个实际实施或评价、无指标报告评价、实施待评价、复用已接纳 Run／评价时，在 `outputs/result.json` 使用 `formal_runs`。存在此列表时，每项 `evaluations[].metrics` 承载对应真实评价；顶层 `metrics` 可为 `{}`，无需复制。没有 `formal_runs` 时，非空顶层 `metrics` 表达所选 authority 下的一次实施和评价。

## 实际 Run、评价与产物

每个 `run_key` 对应一次真实实施，`attempt_key` 对应该 Run 内的一次真实评价。工具调用、调试和机械重试通常属于实施细节；只有形成独立实质研究比较时才按真实因果关系另列。审计、观察、推导和湿实验同样使用该层级。

```json
{
  "formal_runs": [
    {
      "run_key": "cohort-a-audit",
      "status": "executed",
      "evaluations": [
        {
          "attempt_key": "initial-assessment",
          "status": "executed",
          "metrics": {"declared_metric_key": 8}
        }
      ]
    }
  ]
}
```

示例指标需替换为实际 Protocol 的精确 key 和真实值。数据获取或整理时，Baseline／Variant 表达来源特点和方法；Run 保存实际获得的材料，评价检查完整性、来源、适用性或具体科学标准。Protocol 未声明指标时，已实施评价可用 `metrics: {}`，将真实报告归入其 `artifact_paths`；MetricResult 保留空指标和精确结果文档来源。空对象本身不证明实施：尚未评价用 `evaluations: []`；已声明 required metrics 仍须满足真实取值或合同允许的缺测。

状态为 `executed | failed | blocked | cancelled | not_executed`。`failed` 仅用于已实施且失败的工作，保留日志和产物；失败 EvaluationAttempt 不产生 MetricResult，可省略 `metrics`、用 `{}` 或保留部分观察但不称其为完成测量。其余未实施状态按事实填写。已执行 Run 可以没有评价或带被阻塞的评价，不补造结果。

单一生产者可使用默认归属：实施日志为 `logs/train.log`、`logs/train-*.log`、`logs/execution/` 或 `logs/training/`；数据为 `outputs/data/`，原始观察也可用 `outputs/analysis/raw/`、`observations/`、`data/`、`execution/`；评价日志为 `logs/eval.log`、`logs/eval-*.log` 或 `logs/evaluation/`，报告为 `outputs/analysis/evaluation/`、`assessment/`、`evaluation-report.md`。路径必须与实际用途一致。研究说明和最终发言保留 Target 级含义，技术日志可作为 Target 诊断材料。

存在多个实际生产者或采用其他路径时，显式设置每个生产者的 `artifact_paths` 为工作区中实际存在的精确文件或目录，没有则为 `[]`。新产物使用 `logs/`、`outputs/analysis/` 或 `outputs/data/` 内的规范相对路径。宿主按声明边界冻结资产：选择 `logs` 就保留整个目录，选择深层报告文件就保留该文件，并保留相邻的其他产物；相同精确路径可由不同生产者引用。默认归属只适用于该类唯一生产者。按生产者或研究状态组织目录，大文件同样声明真实归属并流式保存。

若收尾反馈称声明路径未绑定，先核实路径存在、内容范围和实际生产者。若只是旧清单曾拆分或合并该目录，保持正确的声明，在同一 Session 正常结束下一轮，由宿主按精确边界重新交接；旧清单保持不变。拼写、路径或归属确有错误时修正声明，保留已有研究成果，无需重跑实验。

## 方法与既有工作复用

Run 可选字段包括 `variant_ref`、`baseline_forward_contract`、`variant_recipe`、`variant_run_ref`、`input_refs`、`local_inputs`、`implementation_paths`、`checkpoint_paths`、`checkpoint_role_refs`、`checkpoint_version_refs`、`artifact_paths`。

- 省略 `variant_ref` 使用所选 authority 的 Variant；也可提供实际已接纳 Variant 的精确引用，包括其他 Baseline 下的方法。Bundle 建议不锁死真实归属。
- 新方法以 `baseline_forward_contract` 配合具体 `variant_recipe` 声明：已有 Baseline 用精确 `baseline_ref`；新方法用 `method_key`、`method_version`、稳定 `method_contract`。Owner 经正常接缝解析或登记。同一 Run 的声明不与 `variant_ref`／`variant_run_ref` 并用。
- `variant_run_ref` 复用已有实施，可在后续 Target 评价；`input_refs` 只能选择已准入当前 Target 的精确引用。复用 Run 保留原实现与输入绑定。
- 实际 Variant 与 Bundle 建议不同的评价，默认与 authority 的 ProtocolVersion 配对；显式 `evaluation_ref` 只能指向实际采用的精确 Variant × Protocol 配对。

评价可选 `evaluation_ref`、`evaluation_attempt_ref`、`metric_result_ref`、`artifact_paths`。默认使用所选 authority 的 Evaluation；复用已接纳评价时同时给出 `evaluation_attempt_ref` 与 `metric_result_ref`。每项指标遵循其自身 Protocol，报告和日志归入该评价。

## 实现快照、状态与局部输入

修订实现前保留每个实际 Run 使用的快照。多版本可放在 `implementation/run-a/`、`implementation/run-b/`，各 Run 用 `implementation_paths` 精确选择；多个 Run 可选择同一快照。只有一份普通 `implementation/` 时可省略选择器，多条目时逐 Run 明确指定。系统派生实现版本，显式 `implementation_revision_ref` 仅断言该精确值。记录真实命令和观察输出，hash 本身不证明实施。

按研究需要保留状态；`checkpoint_policy` 不要求数量，也不把产物存在作为门槛。多 Run 时各自 `checkpoint_paths` 指向保留的精确状态条目，无状态则 `[]`；混合目录不能说明跨 Run 归属。评价可用 `checkpoint_paths` 选择实际评价的子集，否则使用该 Run 的保留状态。需要独立评价的状态应独立可寻址；未保留重要中间状态时说明局限。

对复用 `variant_run_ref` 的新评价选择当前状态角色；路径匹配多个移入状态时，以 `checkpoint_role_refs` 或 `checkpoint_version_refs` 选择精确对象。复用已接纳评价则保持其原冻结角色引用，不随归属变更而改写。

前一 Run 的产物可在 RM 发版本前供后一 Run 使用。按真实实施顺序列 Run，在消费者上写：

```json
{"local_inputs": [{"producer_run_key": "run-a", "artifact_path": "outputs/data/run-a.csv"}]}
```

生产者须在 `artifact_paths` 或 `checkpoint_paths` 声明该路径。最终交接冻结后，RG 解析为精确 RM 版本；前向引用和循环被拒绝。既有已接纳输入继续用 `input_refs`。

## 可复用 Dataset 候选

有独立数据复用价值的产物，在结果中列 `dataset_candidates`，每项含 `artifact_path`、`name`、`purpose`。`artifact_path` 使用工作区中 `outputs/data/` 或 `outputs/analysis/` 下实际存在的精确文件或目录路径；系统按这些候选边界冻结独立资产，同时保留相邻的其他产物。候选的名称与用途应对应这个精确范围，不能用包含其他数据的父目录代替原来的子数据集。

候选条目仍须归属于实际生产者；存在多个 Run／Evaluation 或显式设置 `artifact_paths` 时，将相同的精确候选路径分配给对应生产者。候选之间不要重叠选择父目录及其子路径，分别选择可独立保存的范围。若收到收尾修正反馈，保留已有研究成果，在同一 Session 修正候选或归属声明后再次交接。

Target 接纳前只提供候选；接纳后 Bundle 或 Reasoning 沿 completion manifest 解析精确 RM 版本，登记 Dataset、版本和当前 Question 的真实用途引用。有真实派生关系时登记 derive；最后一个 Target 的候选由 Reasoning 承接，并在综合交接前处理。已有登记先发现／对账，复用身份和原件。

正式发布核实并超过保留期后，系统可回收完成的临时工作区。把需保留的科学产物完整选入交接，`linked_local` 原件放在已登记的稳定位置；后续使用正式精确版本。
