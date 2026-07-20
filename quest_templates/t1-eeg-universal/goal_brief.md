---
predicate_json:
  kind: t1_universal_eeg_discovery
  protocol: t1-four-stage-lodo-sealed-v1
  protocol_ver: 1
  main_claims_max: 3
  universal:
    dataset_families_min: 3
    direction_consistent: true
    leave_one_dataset_out_required: true
  positive_gate:
    alternatives:
      - hierarchical_meta_95ci_excludes_zero
      - locked_task_gain_pp_over_strong_simple_baseline: 2
    multiple_testing_correction_required: true
  novelty_audit_after_claim_lock: true
  sealed_holdout:
    dataset: DREAMER
    stage: D
    endpoint: binary_valence
    evaluations_max: 1
  negative_conclusion_allowed: true
---

# T1（开放 · 创新）跨数据集 EEG 通用规律发现

## 从头约束

启动只给用户在 Web 中选定的数据/参考资料、本目标书与通用依赖，禁止导入任何既有项目代码、baseline、
结论或特征。系统须自建 baseline、自找并复现 SOTA、自定特征与范式；可读论文、不可复制其代码。外部 repo
仅作文献线索，最终实现必须独立并记录 provenance。探索可用 SEED / SEED-IV / FACED / DEAP / MPED 等；
实际可用集合以 Web 预检后由内部资格边界验收的只读视图为准，不依赖任何写死的宿主机路径。
`DREAMER` 仅作 D 阶段 sealed holdout，不入探索池。资源上限为 4×GPU、单轮 24h。

## 目标

在 SEED / SEED-IV / FACED（可加 DEAP / MPED 等；DREAMER 除外）上，范式、任务和实验全自定，从零提出并
验证跨数据集稳健、文献未报道的 EEG 通用特征或规律；找不到时给出可审计负结论。

## 合格发现

- 给出可复现的确定性 feature / 算子，以及它与情绪之间的定量 regularity。
- `universal` = 至少 3 个数据集族方向一致，且 leave-one-dataset-out 仍成立；不得依赖单被试、session、刺激
  顺序或某一种预处理。
- 门槛为层级 meta 分析 95%CI 不跨 0，或锁定任务上相对强简单 baseline 至少提升 2pp，并通过多重检验校正。
- 仅有“attention 看着像某脑区”不构成发现。

## 四阶段防 p-hack

1. A 探索池：自由提出任务、映射与假设，全部 append-only。
2. B claim-lock：冻结 feature、label 映射、模型、搜索空间、主指标、检验和排除规则。
3. C confirmatory LODO：每次留一整个数据集，只作为最终 metric。
4. D sealed holdout = DREAMER：维度标签映射到预注册 binary valence，只评一次。DREAMER 仅作 D 阶段；
   A/B/C、HPO、预处理选择与 claim 形成一律不得读取、探测或试跑。

主 claim ≤ 3。

## 必备 null 与控制

任务内部必须包含 majority、class-prior random、matched-random、label-permutation、
subject-ID / dataset-ID / trial-ID-only、source-only 线性、confidence-only、不同预处理一致性与 leakage 探针。它们是
防止把身份、刺激顺序或数据集差异误当脑规律的必要控制。

## 验收级 novelty audit

claim-lock 后执行三层文献查重并保存 query ledger。已报道的同向关系不算 novel，最多算复现或扩展；新意必须
明确落在组合、跨集稳定性、可迁移机制或反例边界，并逐项说明与已有工作的差异。

## 负结论形态

若没有发现，结论必须写为：在预定义探索预算、候选空间、null 与 LODO/holdout 协议下，未发现满足
universal + novelty + robustness 的规律；同时报告最强失败 claim、失败原因、效应 CI，以及下一步最小可证伪
实验。
