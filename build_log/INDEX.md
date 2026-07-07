# 施工日志索引（build_log）

每完成一个检查点提交，在此追加一行（最新在上）。格式：

`- [NNNN](NNNN-slug.md) — <检查点一句话> · commit `<hash>` · 验证: 通过/未通过/未验证`

---

- [0008](0008-cp13-asset-stubs.md) — CP1.3 资产层接口桩：Gate/StateStore/Compiler/Runner/goalbrief · commit `7deb14a` · 验证: 通过（pytest 101/101）
- [0007](0007-cp12-flow-layer.md) — CP1.2 流程层：system_prompt+四阶段 SKILL+过程 schema · commit `9ee4c45` · 验证: 通过（pytest 74/74 + 信封探针）
- [0006](0006-cp11-contract-layer.md) — CP1.1 契约层：schemas+policy+interfaces+goal_brief（M0 首检查点） · commit `965d1a3` · 验证: 通过（pytest 53/53）

_（0001–0005 为脚手架 / 治理期日志，已按用户指示于 2026-07-07 清理出工作区，内容完整保留在 git 历史（提交 `034d6a6` 及更早）。正式构建的记录从 **0006** 起编号——NNNN 全程递增、不复用旧号，避免与历史重名。）_

<!-- 在此追加记录，例：
- [0006](0006-xxx.md) — <检查点一句话> · commit `abc1234` · 验证: 通过
-->
