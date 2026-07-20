---
predicate_json:
  kind: metric_comparison
  protocol: toy-gauss-cls
  protocol_ver: 1
  metric_id: accuracy
  metric_ver: 1
  scope: aggregate
  success:
    op: ">="
    value: 0.9
---

# 部署验收：二维双高斯分类

## 目标

在运行时生成的二维双高斯二分类数据上，比较逻辑回归与单隐层 MLP。在固定随机种子、固定训练预算和固定
留出集协议下，判断是否至少有一个实现达到 aggregate accuracy ≥ 0.90，并用真实、可复现的
`metric_result` 回答根问题。

## 约束

- 不下载或导入外部数据、代码仓库、预训练权重。
- 数据生成、切分、训练和评估必须在已锁定的执行沙箱中完成。
- 协议须冻结数据种子、样本数、类别先验、划分比例和指标口径；改变任一项必须升版。
- 至少保留 majority baseline，并检查 train/test 泄漏和标签错位。
- 结论只能引用成功 canonical evaluation 的 aggregate accuracy；推理文本或自报数字不能作为成功证据。
- 若预算内没有达到阈值，允许以带失败证据的负结论结束，不得伪造成功。
