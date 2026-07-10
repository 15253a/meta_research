# 0053 · CP11.2b.3b 目标改版版本化控制链

- date: 2026-07-10
- commit: 2dfa65354cf6e4de024a126162e1a8377992166d — feat: 闭合目标改版控制链
- branch: fix/architecture-hardening-20260709
- 检查点 / 步: CP11.2b.3b（属：步⑪生产硬化 · CP11.2 人类控制闭环）

## 决策

`goal_amend` 不再作为普通 note 或对启动目标书的就地覆盖，而是绑定用户看到的精确
`(goal_id, goal_ver)`，经硬指令确认后优先派生一个独立的 reasoning-only 控制轮。消费决策的
`effect` 是唯一机械权威：其中包含完整的新目标正文、显式或从旧版继承后的有效谓词、理由和源/目标版本；
模型只能逐字段复制，StateStore 在同一收尾事务中复核并创建不可变 vN+1。

升版只把 open/inconclusive 问题原地迁到新版并清空旧 score/est_cost，再要求 selection 对新版全部可调度
前沿重评。closed question、answer、evidence 保留出生版本且绝不重开；可能受新谓词影响的旧结论通过
`answer_applicability` 和按需 revalidate/retarget 新问题演化。目标写入、前沿迁移、适用性播种、选题和轮次
收尾同生共死，消费后进程重启可恢复，路由后撤销则在调用模型前终态取消。

专用改版轮只消费绑定 amendment，积压的 note/reprioritize 等 reasoning_start 指令延后到新版的下一个普通
reasoning 边界，防 128 条上下文配额被旧指令占满后永久拒掉已确认新目标。改版确认、过期清理、覆盖和消费
均按 source goal_id 的当前版本判断，不让其它 goal 的更高 version 或更新指令跨域污染。

## 主要改动

- `orchestrator/console.py`、`console_ingest.py`：确定性 goal_amend shorthand/JSON 语法、消息版本绑定、确认时
  stale/supersession 处理、继承有效谓词和精确 human decision.effect；非法修订终态拒绝。
- `orchestrator/advancer.py`、`notify.py`：bootstrap 之后的 amendment 路由优先级、prior terminate 唤醒、
  route/rebound 耐久绑定、reasoning_start 专用消费、无权威取消窗口和 pending/applied 通知分离。
- `orchestrator/statestore_sqlite.py`、`statestore.py`：首 op/唯一 amend、不可变 goal vN+1、未决题迁移、closed
  冻结、applicability/retarget 护栏、全前沿重评分、blocked_by_goal_amend 与原子回滚。
- `orchestrator/compiler_sqlite.py`、`status_card.py`、`run.py`：移除启动期缓存 goal 正文；同一读快照按 cycle 的
  `(goal_id, goal_ver)` 读取目标、前沿和计数；从已消费 decision 无裁剪渲染 goal_amend 精确权威字段。
- `prompts/skills/reasoning/SKILL.md`、`schemas/tree_ops.schema.json`：同步首 op、必填 predicate、禁止重开和
  全前沿重评契约。
- `tests/test_goal_amend_control.py` 及相关控制面/编译器/状态卡测试：覆盖 terminate 后改版、consume 后重启、
  版本不可变、迁移/冻结、适用性播种、路由竞态、长目标无截断、跨版本/跨 goal 隔离和配额洪泛。

## Review

- 第 1 轮：`REQUEST_CHANGES`。两个 BLOCKER：固定锚只给可截断 polished，无法生成精确 amend op；旧 cycle
  的目标正文虽按版本读取，开放集和状态卡计数仍会混入新版。另有 malformed confirmed amendment 覆盖有效
  修订的 SHOULD，以及 predicate schema 漂移 NIT。全部修复并加回归。
- 第 2 轮（最后一轮）：`REQUEST_CHANGES`。一个可复现 BLOCKER：128 条旧 reasoning 指令先吃满配额后，绑定
  amendment 被永久拒绝；另有 current goal 查询跨 goal 泄漏 SHOULD。按 `CLAUDE.md` 两轮上限不启动第 3 轮，
  改为专用轮只消费 amendment，并把确认/清理/消费统一按 source goal_id 作用域处理。
- 外审证据：`/tmp/codexrev.pnMDw6/verdict.md`、`/tmp/codexrev.vkWxlT/verdict.md`。

## 验证

- 第一轮反馈修复后定向：`193 passed in 15.55s`；全量：`990 passed in 200.69s`。
- 第二轮反馈修复后控制面/调度面回归：`374 passed in 81.77s`。
- 最终全量：`pytest -q`（workdir=`meta-research/`）→
  `992 passed in 195.27s (0:03:15)`。
- `git diff --check`、`git diff --cached --check` 与 `python -m compileall -q meta-research/orchestrator`
  通过；功能提交未混入 build log/ROADMAP/implement note。
- 结论：**通过**。CP11.2b.3b 完成；CP11.2b.3/CP11.2 总项尚未完成。

## 遗留 / 回退

- 下一检查点：CP11.2b.3c 真正只读 Codex query responder；其后是 CP11.2b.3d 真实 connector 投递、
  CP11.3/CP11.4 和真实 100+ 轮生产验收。
- 代码回退（尚未在活库应用 goal_amend 时）：`git revert 2dfa653`。本提交无 DDL migration。
- 若活库已经生成 v2+，Git revert 不会删除 append-only goal/decision/applicability 历史；应先 pause，并选择
  保留新版数据后部署兼容修复，或离线恢复改版前 DB 快照，禁止用破坏性 SQL 假装自动回退。
