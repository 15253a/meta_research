# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b.3 reference 完整控制能力
- 检查点状态：在建；CP11.2b.1–2 已提交，控制面总检查点仍未完成

## 正在做什么

CP11.2b.1–2 功能提交 `10215db` 已把鉴权 HTTP、稳定 spool/cursor/retry、run 单写动作消费、
confirm/reject/resolve/cancel、常驻等待、通知扫描和 fresh-snapshot 前端门闭成真实生产入口；记录见
build_log 0051，Git index 隔离全量 `959 passed`。

当前进入 CP11.2b.3：把 `reference/` 明确要求但当前仍诚实 rejected/模板降级的控制能力落成真实语义——
动态 `set_budget`、`reprioritize`、`goal_amend` reasoning-only 路由、真正只读 Codex query responder，
以及把 outbox 接到真实 connector。完成前不得把 CP11.2/CP11.2b 或 M6 数百轮验收标为完成。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`（对应 build_log 0048–0051）。
- CP11.2b.1–2 外审第 1 轮 2 BLOCKER + 1 SHOULD、第 2 轮 3 装配 BLOCKER；两轮上限后全部本地修复，
  未启动第 3 轮。最终 staged-only 全量 `959 passed in 191.99s`。
- 当前无未提交功能 WIP；本次仅待完成 build_log/ROADMAP/implement_note 记账提交。

## 下一步动作

1. 从 reference §4.6/§7.1 M5 重新列出 b.3 的逐条机器契约，先定动态 directive 的权威状态与恢复语义。
2. 分离真正只读 Codex query 调用与研究 Runner：独立只读快照、authorizer、runner_call/ledger/reply 回执和超时/失败终态。
3. 为 connector delivery 定 at-least-once + event_key 去重/重试边界；随后做 staged-only 评审与提交。
4. b.3 完成后再进入 CP11.3（single instance、进程组、critical/budget、goal version），最后 CP11.4 与真实 100+ 轮验收。

## 关键上下文 / 坑

- `reference/` 是设计文档，不是运行时输入；只有进入 prompt/skill/schema/policy/DDL/代码和反例测试的概念才算实现。
- HTTP 进程必须保持 DB `mode=ro`；所有状态效果只允许 run 进程的 WriteDaemon 路径。
- reasoning 控制输入上限只统计 `consume_at=reasoning_start`；绝不能让 note 洪泛拒掉 resume/abort。
- query responder 必须真正只读且有可审计调用/成本回执；当前模板 grounding 不可冒充 reference 的 Codex responder。
- connector 投递与供应商调用都存在“外部成功、持久回执前 SIGKILL”窗口；CP11.2b.3 先闭通知投递，通用调用补账仍属 CP11.4。
- CP11.3/CP11.4 和 reference M6 数百轮真实验收未完成，所以当前仍是受约束的 operational canary，不是最终生产级系统。
