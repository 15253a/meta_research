---
predicate_json:
  kind: local_eeg_cross_dataset_research
  protocol: web-local-eeg-lodo-v1
  protocol_ver: 1
  required_measurements:
    - within_dataset_baseline
    - leave_one_dataset_out
  negative_conclusion_allowed: true
---

# 本机多数据集 EEG 研究（LODO）

## 目标

使用用户在 Web 中选择的本机 EEG 数据集目录与参考资料，从真实数据开始建立可复现 baseline，研究跨数据集
稳定的情绪 EEG 特征、表示或模型，并以 leave-one-dataset-out（LODO）结果回答其泛化性。系统应先识别实际
可用的数据集、标签和被试边界，再冻结每项实验的切分与指标；不得假定目录名等于数据真实性。

## 数据使用方式

- 数据集与参考资料均来自 Web 发布的只读启动输入；不得要求用户再编辑后端路径、YAML 或数据合同。
- SEED、SEED-IV、FACED、DEAP、MPED、DREAMER 或其他 EEG 数据集均可作为普通研究数据使用；以实际预检和
  适配结果为准。
- 若使用 DREAMER，它在本模板中是普通研究/评估数据，不宣称为对研究过程不可见的 sealed one-shot holdout。
  需要该强声明时应另选页面中已就绪的“T1 sealed 评测”模式。
- 用户提供的论文、代码与结果只是输入资产，不自动成为 evidence；外部代码进入 baseline 池仍须经过既有
  import/claim 门禁。

## 最低实验闭环

- 至少建立 majority/class-prior 与一个可解释的简单 EEG baseline。
- 明确样本、trial、session、subject 与 dataset 边界，禁止跨边界泄漏；预处理只用训练侧统计量。
- 数据集数量允许为 1：单数据集时先完成被试外验证，并把“无法做多数据集 LODO”作为有证据的范围限制；
  数据集数量达到 2 个及以上时执行逐数据集留一验证。
- 报告每个 held-out dataset 的主指标、置信区间或重复运行波动，并与强简单 baseline 同口径比较。
- 加入 label-permutation、subject-ID/dataset-ID-only 与泄漏探针；任何失败都如实入账。

## 结束判据

在已冻结的数据边界、预算和指标下，形成至少一个可复查的跨数据集结论（支持、反驳或范围受限均可），且每个
结论都能回溯到真实运行的 metric_result、代码/环境身份与完整评估轨迹。找不到稳定规律时允许以有证据的负结论
结束，不得把未执行的计划或模型自报数字当作结果。
