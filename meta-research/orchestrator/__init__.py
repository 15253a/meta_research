"""meta-research 确定性编排器（P3：编排器从不推理；Codex 是无状态工人）。

模块地图（《第二部分》§6.3；M0 = 接口 + 桩 + 最小驱动器，逐里程碑换真）：
- interfaces.py — 流程层↔资产层唯一缝（§6.10，签名冻结）
- schemas.py   — schema 加载 / $ref 注册表 / 产物文件名→schema 映射
- database.py  — SQLite 唯一真相的建库与守卫（M1a：附录 A DDL + checksum/计数/版本三重锁）
- writedaemon.py — WriteDaemon（M1b：资产层唯一写库执行者，单写连接 + 短事务）
- gate.py      — StubGate（三级校验：schema+引用真、业务放过）+ ArtifactIndex（M0）
- gate_sqlite.py — SqliteGate（M1a-Gate：authorizer 隔离 + gate_input_* 视图 + gate_close_question；M0 并存不替换）
- statestore.py — InMemoryStateStore（七 op / 调度可见性 / 释放语义；M0）
- statestore_sqlite.py — SQLiteStateStore（M1b：状态机落 SQLite，经 WriteDaemon；decompose 单事务原子）
- importer.py   — DeferredImporter（M1c：外部 import M1–M3 降级——发现+登记+deferred 三写入，不物化=M4）
- interaction.py — InteractionIngest（M1c：人机 durable 入站 + 模板 ACK + 文件请求单，不触发真 responder=M5）
- compiler.py  — StubCompiler（确定性四区包 + manifest 溯源）+ StubCtx / StubRecall（M0）
- compiler_sqlite.py — SqliteCompiler（M2：DB→确定性四区 context_pack，字节一致 + applicability 徽标；M0 并存不替换）
- recall_sqlite.py — SqliteRecall/SqliteCtx + 复用判定 selector（M2：§3.6.2 四级可停 + §4.1.5 O(1) 测量索引；M0 并存不替换）
- budgeting.py  — compute_budget：单轮 B(t) 唯一定义（compiler/status_card 共用，防公式漂移）
- status_card.py — build_status_card：人机控制台派生卡（M2：§4.6.6 封闭字段集构建器；原子发布接入=M3）
- runner.py    — CodexRunner（真 codex exec，M0 起即真）
- goalbrief.py — 启动输入①解析（§4.6.7 机械校验唯一实现）
"""
