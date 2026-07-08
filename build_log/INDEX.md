# 施工日志索引（build_log）

每完成一个检查点提交，在此追加一行（最新在上）。格式：

`- [NNNN](NNNN-slug.md) — <检查点一句话> · commit `<hash>` · 验证: 通过/未通过/未验证`

---

- [0014](0014-cp31-sqlite-compiler.md) — CP3.1 SqliteCompiler（M2：DB→确定性四区 context_pack，字节一致 diff=0 + applicability 徽标） · commit `2110f02` · 验证: 通过（pytest 283/283，16 compiler 用例）
- [0013](0013-cp24-m1c-isolation.md) — CP2.4 M1c 隔离拒绝用例（DeferredImporter + InteractionIngest）**——收尾步② M1** · commit `1182a5a` · 验证: 通过（pytest 267/267；M1 步级验证全过）
- [0012](0012-cp23-sqlite-gate.md) — CP2.3 SqliteGate（M1a-Gate：authorizer 隔离 + gate_input 视图 + gate_close_question） · commit `d07c6c6` · 验证: 通过（pytest 255/255，22 gate 用例）
- [0011](0011-cp22-writedaemon-statestore-sqlite.md) — CP2.2 单写 WriteDaemon + SQLiteStateStore（M1b：状态机落 SQLite + decompose 原子性 + kill-9 无半写） · commit `be84a90` · 验证: 通过（pytest 233/233）
- [0010](0010-cp21-appendix-a-schema.md) — CP2.1 冻结 Appendix-A schema 落地 + DB 层不变量否定用例（M1a-DB 半） · commit `6d45b53` · 验证: 通过（pytest 198/198，含 91 条 DB 层否定用例）
- [0009](0009-cp14-driver-m0-acceptance.md) — CP1.4 驱动器 + **M0 端到端验收通过（步①收尾）** · commit `45641cc` · 验证: 通过（pytest 107/107 + 真 codex 5 轮验收）
- [0008](0008-cp13-asset-stubs.md) — CP1.3 资产层接口桩：Gate/StateStore/Compiler/Runner/goalbrief · commit `7deb14a` · 验证: 通过（pytest 101/101）
- [0007](0007-cp12-flow-layer.md) — CP1.2 流程层：system_prompt+四阶段 SKILL+过程 schema · commit `9ee4c45` · 验证: 通过（pytest 74/74 + 信封探针）
- [0006](0006-cp11-contract-layer.md) — CP1.1 契约层：schemas+policy+interfaces+goal_brief（M0 首检查点） · commit `965d1a3` · 验证: 通过（pytest 53/53）

_（0001–0005 为脚手架 / 治理期日志，已按用户指示于 2026-07-07 清理出工作区，内容完整保留在 git 历史（提交 `034d6a6` 及更早）。正式构建的记录从 **0006** 起编号——NNNN 全程递增、不复用旧号，避免与历史重名。）_

<!-- 在此追加记录，例：
- [0006](0006-xxx.md) — <检查点一句话> · commit `abc1234` · 验证: 通过
-->
