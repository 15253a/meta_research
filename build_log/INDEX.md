# 施工日志索引（build_log）

每完成一个检查点提交，在此追加一行（最新在上）。格式：

`- [NNNN](NNNN-slug.md) — <检查点一句话> · commit `<hash>` · 验证: 通过/未通过/未验证`

---

- [0066](0066-cp114c2b1-materializer-component-boundaries.md) — CP11.4c.2b.1 exact repository materializer responsibility boundaries · commit `4e28698` · 验证: 通过（相关 16/43；AST/compat 等价；第2轮 APPROVE；唯一全量 1382）
- [0065](0065-cp114c2a-exact-repository-materialization.md) — CP11.4c.2a exact GitHub repository snapshot + file-backed sandbox import bridge · commit `50ba41f` · 验证: 通过（相关 170/156/41；外审成立项全修；唯一全量 1381）
- [0064](0064-cp114c1-adversarial-execution-boundary.md) — CP11.4c.1 exact-pinned Docker + launcher rlimit/seccomp BPF + guardian drain/quarantine recovery · commit `a05584d` · 验证: 通过（相关 18/197/127；外审 3 BLOCKER 全修；最终有效全量 1354）
- [0063](0063-cp114b-artifact-provider-accounting.md) — CP11.4b fd-safe artifact capability + guardian capture/provider invocation 精确补账 · commit `e236487` · 验证: 通过（相关验证；外审成立项全修；唯一全量 1332）
- [0062](0062-cp114a3-trusted-import-trigger-sources.md) — CP11.4a.3 human/stuck/SOTA 可信来源 + 独立参照题 + 冻结激活/恢复 · commit `a31658e` · 验证: 通过（相关 217/392/130/17；外审反馈全修；唯一全量 1322）—— **CP11.4a 达成**
- [0061](0061-cp114a2-durable-import-discovery.md) — CP11.4a.2 `new_structure` 耐久只读发现 + pinned candidate/license provenance + 原子登记/恢复 · commit `43dd99e` · 验证: 通过（相关 358/158/20/91；唯一全量 1297；外审第2轮上限后反馈处置）
- [0060](0060-cp114a1-deferred-import-plan-review.md) — CP11.4a.1 冻结 import 选择/dependency_wait/worker 恢复 + 独立 plan 评审 · commit `43c4134` · 验证: 通过（相关 84/92/105/93；唯一全量 1267 过/7 旧契约锚，修后 exact 7/7；外审第2轮上限后反馈处置）
- [0059](0059-cp113c-durable-state-semantics.md) — CP11.3c current lineage + owner-first 外调 + receipt→DB 对账 + eval-only + 120 轮状态稳定性 · commit `a32629d` · 验证: 通过（相关 78/101/102/80/45/77/59/20/13；唯一全量 1235 过/5 旧契约锚，修后 exact 5/5；外审两轮均无 verdict）—— **CP11.3 状态与执行边界达成**
- [0058](0058-cp113b-execution-owner-death.md) — CP11.3b 统一 owner-death execution guardian + delegated fence + 可信子孙树整组排空 + 耐久 receipt · commit `2f4a5d5` · 验证: 通过（相关 21/32/30/75/53/62/45；VEPFS canary 1；唯一全量 1209 过/1 时序断言假阳性，修后定向 1；外审第2轮上限后反馈处置）
- [0057](0057-cp113a-instance-owner-lease.md) — CP11.3a 共享 work-root 单实例 owner lease + heartbeat + lifecycle/connector/console fencing · commit `d79d3d0` · 验证: 通过（唯一全量 1180 过/1 测试隔离假阳性；修复后定向 38，依指示未二次全量；外审两轮上限后反馈全修）
- [0056](0056-cp112b3e-authenticated-connector-ingress.md) — CP11.2b.3e webhook/OneBot 认证耐久入站 + 身份/会话绑定 + 精确继续/动作语义 + 有限退出排空 · commit `5056736` · 验证: 通过（相关 344+30；内审 166；唯一一次全量 1137；外审第1轮 401、第2轮超时，均无 verdict）—— **CP11.2 人类控制闭环达成**
- [0055](0055-cp112b3d-durable-connector-delivery.md) — CP11.2b.3d 真实 webhook/OneBot 出站 + 耐久 queue/retry/receipt + 严格 ACK/公平调度/诚实投影 · commit `6098a69` · 验证: 通过（相关 239；唯一一次全量 1105；外审第1轮 401、第2轮超时，均无 verdict）
- [0054](0054-cp112b3c-readonly-query-responder.md) — CP11.2b.3c facts-only 只读 Codex 查询 + 不同 UID/tool-free 隔离 + 耐久 FIFO/成本/reply 收尾 · commit `92ecf18` · 验证: 通过（唯一一次全量 1055 过/1 败；对应前端修复后定向 6/6，依指示未二次全量；外审两次均因账号限额无 verdict）
- [0053](0053-cp112b3b-goal-amend-versioning.md) — CP11.2b.3b 不可变目标升版 + 专用 reasoning route + applicability/revalidate + 崩溃恢复 · commit `2dfa653` · 验证: 通过（控制面/调度面 374、全量 992；外审两轮上限后全部反馈本地修复）
- [0052](0052-cp112b3a-runtime-budget-priority.md) — CP11.2b.3a 耐久动态预算 + 账本/状态卡对账 + 机械 pin/boost/suppress · commit `7f4fd73` · 验证: 通过（相关 234、全量 976；外审两轮上限后全部反馈本地修复）
- [0051](0051-cp112b-durable-authenticated-control-plane.md) — CP11.2b.1–2 鉴权 HTTP→durable spool→run 单写动作全链 + 常驻/fail-closed 控制台 · commit `10215db` · 验证: 通过（staged-only 全量 959；外审两轮上限后 3 个装配 BLOCKER 全部本地修复）
- [0050](0050-cp112a1-upload-tree-identity.md) — CP11.2a.1 逐组件固定嵌套上传树身份 + 同 fd 消费 + resolve 遍历总预算 · commit `3c4c9b4` · 验证: 通过（staged-only 定向 80、全量 857；外审第2轮 APPROVE）
- [0049](0049-cp112a-user-asset-authority.md) — CP11.2a 用户文件原子接纳 + goal-wide 有界回执 + 生成时最小资产授权 · commit `c077cd2` · 验证: 通过（staged-only 全量 844；外审两轮上限后全部本地修复；三路内部 APPROVE）
- [0048](0048-cp111-external-artifact-no-wedge.md) — CP11.1 严格 metric/SQLite ID 边界 + reasoning 语义拒收持久收敛 + DB 损坏 fail-loud · commit `ac53516` · 验证: 通过（定向 132，全量 754，codex 第1轮 APPROVE）
- [0047](0047-cp102-cost-ledger-budget-stop.md) — CP10.2 成本账本覆盖失败/非法调用 + 未知用量 fail-closed + 单次越线即 durable 停机 · commit `03d3ffd` · 验证: 通过（pytest 754 全量；codex 第2轮 APPROVE）
- [0046](0046-cp101-runner-cost-capture.md) — CP10.1 runner 成本捕获（**步⑩ M6 成本记账·第一关**：实机 probe 定 `codex-chatgpt exec` 把 `tokens used\n<N>` 总 token 打到 **stderr**[runner 捕获但成功时丢弃]；加 CallUsage + Artifact.usage[非破坏]；runner.py parse_tokens_used[行首锚定/合法千分组/取末条/坏输入→0] + _invoke 计墙钟解析→Artifact.usage；只成功路径捕获；**不改循环行为**；顺带修 test_run.py 既有 `__new__` monkeypatch 全局污染 bug）· commit `1b415f9` · 验证: 通过（pytest 678 全量 + runner 15 例；codex 第2轮 APPROVE）
- [0045](0045-cp94-console-e2e-step9-close.md) — CP9.4 人类控制台端到端验收 + README（**步⑨收尾达成**：test_console_e2e 全栈真跑[真 HTTP+真 DB+真 spool+入站闭环]——①真视图 GET / 真页 + /api/db 真数据 + /api/file 白名单[含逃逸负例]；③单写纪律**强证** _open_ro(mode=ro) 写被物理拒 + DB 逻辑快照 WAL-proof 不变；②POST→spool→ingest→pause→确认 provenance 落库→precheck 阻断、query→grounded[据卡 c1/q1]+no-dup；README §4.1 控制台节；实机 CLI 起服务 curl 真跑留证）· commit `cd97ee0` · 验证: 通过（pytest 664/664；codex 两轮 REQUEST_CHANGES[R1 主库 sha256 假绿、R2 逻辑快照仍漏→补结构强证]全按 §2.2 修毕）—— **步⑨（M8 人类控制台接入）达成**
- [0044](0044-cp93-console-inbound-loop.md) — CP9.3 人类控制台入站闭环（**ConsoleInboxIngest + precheck 装配**：run 进程 precheck 边界 ingest console_inbox.jsonl → handle_inbound[幂等落 directive/note] → query 经 mediator 应答；**no-loss/no-dup**：query-once 落持久层[查 reply 存在性]、只有 durable reply 才推进游标、line-index 游标消费坏行、瞬时故障有限重试(5)+终态回执、顶层兜底不崩主循环；单写纪律不破）· commit `6967dc3` · 验证: 通过（pytest 662/662，含 ingest 20 例；内审 Opus 1 BLOCKER+3 SHOULD、codex 两轮 REQUEST_CHANGES[R1 漏答/饿死、R2 终态回执失败仍推进]全按 §2.2 修毕）
- [0043](0043-cp92-console-frontend-realdata.md) — CP9.2 人类控制台前端接入真数据（**去 mock 渲染码**：`views/console/index.html` 由原型派生，换数据源 /api/db——adaptPayload[表平铺+status_card 拍平+budget/fs/directive/ledger 归一] + buildLive/applyLive[真 payload.live 覆盖 SCENARIOS] + streamSyncReal[seen-set 真事件流] + refreshDB[in-flight 保护] + fsRefresh[真文件树 DB.fs]；原型把 mock 焊进渲染码[假 goal v2/cycle 19/telemetry/排行/魔法轮号剧情]全改读真 payload、删 6 面板+2 死函数；空/稀疏/null 真库全守卫；单写纪律不破[仅 GET/POST，零 DB 写]）· commit `302261a` · 验证: 通过（pytest 642/642 + node 冒烟 seeded/空库/null-cap 全 9 标签页 render 不抛；内审抓 budget 崩已修，codex 第1轮 2 BLOCKER[r2 硬编码/fs 树未接]+6 SHOULD+1 NIT 全改，第2轮 APPROVE）
- [0042](0042-cp91-console-data-plane.md) — CP9.1 人类控制台数据面（**步⑨ M8 开工**：console_server.py 独立只读进程——/api/db 真表动态列投影+派生[status_card/live/notification/policy/FS] / /api/file 白名单虚拟根+containment / /api/message spool 入站；**单写纪律铁律不破**[mode=ro+零 DB 写]）· commit `295f84d` · 验证: 通过（pytest 638/638；内审 APPROVE，codex 两轮[第1轮抓出并还原误带的 CP8.8 回滚]第2轮 APPROVE）
- [0041](0041-cp88-reasoning-selection-no-wedge.md) — CP8.8 reasoning selection 不可调度不楔死（**部署真跑发现的真实楔死修复**：Codex 选 visit 达上限的题 attack→InvalidSelectionError→decision+terminate 干净收尾；部署复跑 7 轮越过首跑 c6 楔死点、τ 干净自停、selection_invalid 活证据）· commit `5e7108d` · 验证: 通过（pytest 625/625 + 部署系统真跑 exit 0 零 traceback；内审+codex 双 APPROVE）
- [0040](0040-cp87-operating-manual-step-verification.md) — CP8.7 运维操作手册（README 重写）+ 步⑧步级验证收口（全链 E2E + 真 Codex 冒烟 + 冻结锁三条留证）· commit `d627a39` · 验证: 通过（pytest 623/623；内审逐条实证事实一致[1 BLOCKER budget 停因休眠 caveat 已修]，codex APPROVE）—— **步⑧「正式直接可用」达成**
- [0039](0039-cp86-exec-target-kind.md) — CP8.6 exec target kind（既有 legal baseline 上建变体：gate_claim_variant→manifest 真执行→gate_register_variant 入池；崩溃自占复用严核身份+config；append 绑定锚收准 build_target.evaluation_id）· commit `a713509` · 验证: 通过（pytest 623/623；内审+codex 两轮，3 BLOCKER 全修，第2轮 APPROVE）
- [0038](0038-cp85-sidecar-file-request-bridge.md) — CP8.5 sidecar→file_request 桥（阶段资源请求全局等待闭环：落单→干净停→precheck 拦→resolve→同一在途轮续跑；业务拒重试/损坏 fail-loud 分流）· commit `650aace` · 验证: 通过（pytest 618/618；内审+codex 第1轮双 APPROVE）
- [0037](0037-cp84-run-attack-assembly-smoke.md) — CP8.4 run.py attack 全装配 + 全链 E2E + **真 Codex 冒烟**（第二次冒烟：真 Codex 完整走通 attack build 链——注册协议/占坑/真训练 MLP acc=0.9949/双评审 pass/legal 入池/τ 安全网真实触发；第一次冒烟暴露的 kind 教学缺口与拒因不回流已修成自纠环）· commit `4f39559` · 验证: 通过（pytest 613/613 + 冒烟 exit 0×2；codex 第1轮 APPROVE）
- [0036](0036-cp83-production-assembly.md) — CP8.3 生产装配（bundle SKILL m7-1 真执行契约 + judge SKILL/review_verdict 契约 + StageProvider bundle passthrough + JudgeProvider 写 runner_call/DECISION；SKILL↔机器契约逐字段一致）· commit `c7863c0` · 验证: 通过（pytest 608/608；codex 第2轮 APPROVE）
- [0035](0035-cp82-attack-stages-real-contract.md) — CP8.2 attack_stages 真契约化（消费冻结 plan/idea schema + gate_new_protocol/gate_claim_baseline 正式通道 + bundle manifest 驱动真执行 + toy TARGET_SPEC 清理；全自动不楔死[业务拒]与损毁 fail-loud 分流）· commit `d822add` · 验证: 通过（pytest 594/594；codex 两轮，BLOCKER 采纳修复+误读记录）
- [0034](0034-cp81-execution-manifest.md) — CP8.1 execution_manifest 契约 + harness manifest 适配层（**步⑧ M7 开工**：不解冻冻结件，bundle 编译产物承载机器执行契约；防旁路交叉核 + 禁 shell/路径/env/超时围栏 + 净土物化正反对账）· commit `8b4f59a` · 验证: 通过（pytest 578/578，43 新；codex 两轮外审 BLOCKER 全修）
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
