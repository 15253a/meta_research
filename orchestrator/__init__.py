"""meta-research 确定性编排器（P3：编排器从不推理；Codex 是无状态工人）。

模块地图（《第二部分》§6.3；M0 = 接口 + 桩 + 最小驱动器，逐里程碑换真）：
- interfaces.py — 流程层↔资产层唯一缝（§6.10，签名冻结）
- schemas.py   — schema 加载 / $ref 注册表 / 产物文件名→schema 映射
- database.py  — SQLite 唯一真相的建库与守卫（M1a：附录 A DDL + checksum/计数/版本三重锁）
- writedaemon.py — WriteDaemon（M1b：资产层唯一写库执行者，单写连接 + 短事务）
- gate.py      — StubGate（三级校验：schema+引用真、业务放过）+ ArtifactIndex（M0）
- gate_sqlite.py — SqliteGate（M1a-Gate：authorizer 隔离 + gate_input_* 视图 + gate_close_question；M0 并存不替换）
- gate_exec.py  — ExecGate（M4：执行生命周期 gates——run/attempt/build_target/evaluation + 双评审机械判据 review_passed）
- gate_pool.py  — PoolGate（M4：注册/评审 gates——claim/register baseline·variant·evaluation + new_protocol；legal 即入池）
- subject_manifest.py — 双评审 subject manifest 确定性构造（canonical JSON→sha256；code/result 两配方，§4.1.4 附注）
- harness.py    — 执行 harness（M4：真子进程→staging log 原子改名→execution_log 幂等入账；零 DB 长操作）
- obs_parser.py — 确定性观测 parser（M4：log→execution_observation(source=parser) + parser_result_suspect 真派生，OPEN #5 observation 节）
- statestore.py — InMemoryStateStore（七 op / 调度可见性 / 释放语义；M0）
- statestore_sqlite.py — SQLiteStateStore（M1b：状态机落 SQLite，经 WriteDaemon；decompose 单事务原子）
- importer.py   — DeferredImporter（M1c：外部 import M1–M3 降级——发现+登记+deferred 三写入，不物化=M4）
- interaction.py — InteractionIngest（M1c：人机 durable 入站 + 模板 ACK + 文件请求单，不触发真 responder=M5）
- compiler.py  — StubCompiler（确定性四区包 + manifest 溯源）+ StubCtx / StubRecall（M0）
- compiler_sqlite.py — SqliteCompiler（M2：DB→确定性四区 context_pack，字节一致 + applicability 徽标；M0 并存不替换）
- recall_sqlite.py — SqliteRecall/SqliteCtx + 复用判定 selector（M2：§3.6.2 四级可停 + §4.1.5 O(1) 测量索引；M0 并存不替换）
- budgeting.py  — compute_budget：单轮 B(t) 唯一定义（compiler/status_card 共用，防公式漂移）
- status_card.py — build_status_card：人机控制台派生卡（M2：§4.6.6 封闭字段集构建器；原子发布接入=M3）
- advancer.py  — SqliteAdvancer（M3：真组件上可恢复状态机步进 + derive_next_route 全矩阵；M0 driver 并存不替换）
- attack_stages.py — AttackStages（M4：attack 轮 idea/plan/bundle/reasoning 阶段推进——两段提交+结构恢复+管线强制 ingest）
- phase_commit.py — 阶段级原子提交幂等落库（§4.2.5：同键同 hash 跳过、异 hash conflict 拒）
- import_worker.py — ImportWorker（M4：外部 import 物化——worker cycle[OPEN #6]+供应链 manifest+失败路径全拒）
- console.py    — Console（M5：保守关键词分类器 + directive 生命周期——润色≠raw 时序/回显确认/按时机消费+DECISION）
- mediator.py   — Mediator（M5：query 只读应答链——mode=ro+全写拒 authorizer/grounding 校验+模板回退/中介重建）
- runner.py    — CodexRunner（真 codex exec，M0 起即真）
- goalbrief.py — 启动输入①解析（§4.6.7 机械校验唯一实现）
"""
