# 施工日志索引（build_log）

每完成一个检查点提交，在此追加一行（最新在上）。格式：

`- [NNNN](NNNN-slug.md) — <检查点一句话> · commit `<hash>` · 验证: 通过/未通过/未验证`

---

- [0033](0033-cp75-longrun-verification.md) — CP7.5 M6 长跑步级验证 **——收尾步⑦ M6 建造**（不漂移[守卫上界内 question_dep/父指针/全 done]/可恢复[中途重启终库逐字节一致]/τ 自停[新实例拒推进]；诚实裁量：数百轮真跑属运维）· commit `f45dd6f` · 验证: 通过（pytest 535/535，3 新用例）
- [0032](0032-cp74-mechanism-scenarios.md) — CP7.4 §7.3 机制验收剧本（主链路 I1/I2/I3 因果链 / import 三失败 / 日志 suspect→gate 消费侧 fail-closed / 人机 §7.3-item4 三向负例；mock 驱动真组件、非真 Codex）· commit `6be7566` · 验证: 通过（pytest 532/532，8 新用例）
- [0031](0031-cp73-run-entrypoint.md) — CP7.3 run.py 全系统装配入口（一条命令：真组件+StageProvider(真 Codex)→run_cycles；**真 Codex CLI 冒烟通过**；只读连接单写边界；DB 权威 goal_body；τ/阻断端到端）· commit `ce11d00` · 验证: 通过（pytest 524/524，9 新用例 + 真 Codex 冒烟 exit 0）
- [0030](0030-cp72-stage-provider.md) — CP7.2 StageProvider 真 Codex 生产装配（run+信封解析+逐产物 schema 校验+artifact_parse 重试 封成真组件 (cyc,pack)→files；answer 语义下沉组件；sidecar fail-loud；真 SqliteAdvancer e2e）· commit `2ce750e` · 验证: 通过（pytest 515/515，13 新用例）
- [0029](0029-cp71-stopcontroller.md) — CP7.1 长跑自终止安全网（M6 开工：§4.4.6 τ 判据①分数衰退[前沿全评分才判]②ledger.money 预算门恢复安全；durable global_stop；OPEN #4 折入 predicate_json）· commit `da38b2f` · 验证: 通过（pytest 502/502，13 新用例）
- [0028](0028-cp63-notify-filereq-global-wait.md) — CP6.3 通知矩阵 outbox + 文件请求全流水 + 全局等待 **——收尾步⑥ M5**（§7.1 M5 联合勾兑 64 测全过；outbox committed=换行终止；symlink 全链不跟；幂等先于 quota）· commit `bee3b9e` · 验证: 通过（pytest 489/489，19 新用例）
- [0027](0027-cp62-mediator-status-publish.md) — CP6.2 query 只读应答链 + 中介重建 + status_card 发布（mode=ro+全写拒双保险[含 temp/VTABLE]；grounding 四规则；重建一致；latest_decision cycle 作用域；阶段边界原子发布）· commit `28c6117` · 验证: 通过（pytest 470/470，16 新用例）
- [0026](0026-cp61-console-directive-lifecycle.md) — CP6.1 保守分类器 + directive 生命周期（M5 开工：unclear 不猜+润色≠raw+回显确认门+单事务消费；pause 状态模型按消费序阻断）· commit `702071e` · 验证: 通过（pytest 454/454，18 新用例）
- [0025](0025-cp56-m4-semantic-cases.md) — CP5.6 语义判据 5 判例显式命名验收 **——收尾步⑤ M4**（步级验证 47 测联合勾兑全过）· commit `eb5e7d9` · 验证: 通过（pytest 436/436）
- [0024](0024-cp55-import-worker.md) — CP5.5 外部 import 物化 ImportWorker（OPEN #6 落地：worker cycle route NULL+标记；失败路径全拒含 judge FAIL；provenance 五件套）· commit `d9de442` · 验证: 通过（pytest 431/431，10 新用例）
- [0023](0023-cp54-attack-advance.md) — CP5.4 attack 轮 advance 全链**（首次全链跑通）**：两段提交+结构恢复+锚校验+judge replay-safe · commit `6af22cf` · 验证: 通过（pytest 421/421，12 attack 用例含 5 类崩溃缝隙恢复）
- [0022](0022-cp53-harness-obs-parser.md) — CP5.3 真执行 harness + 确定性观测 parser + parser_result_suspect 真派生（OPEN #5 落地闭；stale fail-closed + 多 log OR） · commit `215c694` · 验证: 通过（pytest 409/409，20 obs_parser 用例）
- [0021](0021-cp52-pool-gates-subject-manifest.md) — CP5.2 PoolGate 注册/评审 gates（claim/register + §4.2.5(ii) 单事务注册 + 绑定核）+ subject manifest 确定性 · commit `439d716` · 验证: 通过（pytest 389/389，20 gate_pool 用例）
- [0020](0020-cp51-exec-gates.md) — CP5.1 ExecGate 执行生命周期 gates（§4.1.4 九函数 + review_passed 双评审机械判据；OPEN #5/#6 裁决） · commit `7d64ec5` · 验证: 通过（pytest 369/369，28 gate_exec 用例）
- [0019](0019-cp43-import-idempotency.md) — CP4.3 select_deferred 幂等守卫（不重复登记，四道 fail-loud）**——收尾步④ M3** · commit `6dd2387` · 验证: 通过（pytest 341/341；M3 §7.1 两判据 10 测步级全过）
- [0018](0018-cp42-run-cycles-decompose-recovery.md) — CP4.2 外层驱动循环 run_cycles + decompose advance + **真 kill-9 恢复**（终库与不杀一致，§7.1 M3 首判据）· commit `ff30463` · 验证: 通过（pytest 336/336，含 kill-9 subprocess 测）
- [0017](0017-cp41-advancer-derive-route-bootstrap.md) — CP4.1 Advancer 骨架：derive_next_route 全矩阵（§6.13(3)）+ advance bootstrap 创世轮（真 SQLite，单一 atomic 阶段 + 续跑幂等）· commit `148c907` · 验证: 通过（pytest 333/333，17 advancer 用例）
- [0016](0016-cp33-observation-status-card.md) — CP3.3 观测摘要进 reasoning 锚点（§4.7）+ status_card 封闭字段（§4.6.6）+ 门禁拒读负例 **——收尾步③ M2** · commit `72647f8` · 验证: 通过（pytest 316/316；M2 §7.1 五判据步级全过）
- [0015](0015-cp32-recall-reuse-selector.md) — CP3.2 Recall 四级可停 + 复用判定 O(1) selector（§4.1.5，EXPLAIN 证走测量索引 + faceted tag） · commit `1a099fc` · 验证: 通过（pytest 298/298，15 recall 用例）
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
