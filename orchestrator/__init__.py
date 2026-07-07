"""meta-research 确定性编排器（P3：编排器从不推理；Codex 是无状态工人）。

模块地图（《第二部分》§6.3；M0 = 接口 + 桩 + 最小驱动器，逐里程碑换真）：
- interfaces.py — 流程层↔资产层唯一缝（§6.10，签名冻结）
- schemas.py   — schema 加载 / $ref 注册表 / 产物文件名→schema 映射
- database.py  — SQLite 唯一真相的建库与守卫（M1a：附录 A DDL + checksum/计数/版本三重锁）
- gate.py      — StubGate（三级校验：schema+引用真、业务放过）+ ArtifactIndex（M1 换真）
- statestore.py — InMemoryStateStore（七 op / 调度可见性 / 释放语义；M1 落 SQLite）
- compiler.py  — StubCompiler（确定性四区包 + manifest 溯源）+ StubCtx / StubRecall（M2 换真）
- runner.py    — CodexRunner（真 codex exec，M0 起即真）
- goalbrief.py — 启动输入①解析（§4.6.7 机械校验唯一实现）
"""
