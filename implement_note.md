# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b.3d 真实 connector 投递
- 检查点状态：CP11.2b.3c 已完成；下一检查点 CP11.2b.3d

## 正在做什么

CP11.2b.3c 功能提交 `92ecf18` 已把 query 闭成真实的只读 Codex 旁路：模型只能从已发布状态卡选择
逐项复核的精确 facts，回复由确定性 renderer 生成；调用运行在不同 `codexro` UID、tool-free 配置和隔离临时
目录中。调用前耐久落 intent，完成时 runner_call 用量、ledger 与 reply 原子收尾；未知调用不重放。逐 conversation
FIFO、重启恢复、预算/卡片重核、常驻 interaction pump、受控 drain 和第二次 Ctrl-C 硬停均已有回归。

记录见 build_log 0054。唯一一次最终全量为 `1055 passed, 1 failed`；唯一失败是浏览器 conversation id 初版
使用 localStorage，改为 sessionStorage 后对应前端测试 `6 passed`。按用户要求没有再跑第二次全量，所以不记成
新的全量全绿。外审两次均在 verdict 前撞到账号用量上限，没有外部 APPROVE；内部设计/耐久/安全复核无
BLOCKER。

下一步是 CP11.2b.3d：把当前耐久回复接到真实 connector，闭合 event_key 幂等、at-least-once 重试、投递
回执和可观测失败。其后仍有 CP11.3/CP11.4 与字面意义的真实 100+ 轮生产验收；完成前不得把
CP11.2/CP11.2b/CP11.2b.3 或 reference 完整落地标为完成。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP11.1 `ac53516`；CP11.2a `c077cd2`；CP11.2a.1 `3c4c9b4`；
  CP11.2b.1–2 `10215db`；CP11.2b.3a `7f4fd73`；CP11.2b.3b `2dfa653`；
  CP11.2b.3c `92ecf18`（对应 build_log 0048–0054）。
- CP11.2b.3c 回退点：`git revert 92ecf18`；无 DDL migration，但 revert 不会删除已有 query
  runner_call/ledger/reply 审计记录。
- 当前仍是受约束的 operational canary，不是最终 production-ready/reference-complete 系统。

## 下一步动作

1. 从 reference 的 connector/通知语义提取 event_key、投递状态、退避、重启恢复和回执的机械不变量。
2. 接入真实 connector adapter；发送前后均落耐久状态，重复消费同一 event_key 不重复形成用户可见效果。
3. 覆盖外部成功但本地 receipt 未落、瞬时/永久失败、进程重启、乱序与连续新消息下的 at-least-once 行为。
4. 完成 CP11.2b.3d 定向验证、评审和独立提交，再推进 CP11.3/CP11.4 与真实 100+ 轮验收。

## 关键上下文 / 坑

- `reference/` 是设计权威，不是自动加载的运行时 skill；只有已进入 prompt/skill/schema/policy/DDL/代码和
  反例测试的概念才算实现。
- HTTP 进程必须保持 DB `mode=ro`；所有状态效果只允许 run 进程的 WriteDaemon 路径。
- query reply 只接受已发布卡中的精确标量 facts；任何模型自由文本、越界 path/value、旧 goal 卡或没有可靠
  conversation id 的历史复用都必须拒绝或隔离。
- query 外部调用的 intent/成本/reply 已闭合，但 connector 仍有“外部成功、本地 receipt 前 SIGKILL”窗口；
  CP11.2b.3d 必须用 event_key 幂等与耐久投递状态收敛，不能靠内存去重。
- `heartbeat_ref` 尚未接通；CP11.3/CP11.4 和 reference M6 数百轮真实验收未完成，所以当前不能声称支持
  无条件上百轮生产执行。
- 后续安全 SHOULD：只给查询进程认证所需的 `CODEX_HOME` 内容、固定/记录 CLI 版本身份、限制 trace 总字节。
- 活库已有 append-only 版本、directive、runner_call 或 ledger 后，Git revert 只回代码；回退须先 pause，保留
  审计历史并部署兼容版本或恢复完整 DB 快照，禁止用破坏性 SQL 伪造自动回退。
