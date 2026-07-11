# 0062 · CP11.4a.3 三闸可信来源、独立参照题与冻结激活

- date: 2026-07-11
- commit: `a31658e8ddc6a3328ec1b4aba5f860d38b436f5d` — feat: close CP11.4a.3 trusted import trigger sources
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4a.3（属：步⑪ CP11.4 残余架构边界；本提交使 CP11.4a 父项闭合，CP11.4b/c 继续）

## 决策

本检查点闭合 `human_named`、`stuck`、`sota_reference` 三种非默认 import 来源，但不扩大到不可信代码执行。
模型只可请求受信控制 sidecar；来源权威、网络读取、候选/license 登记、问题树转交和恢复均由编排器机械执行。

- `human_named` 只接受经 hard confirmation 的结构化 `inject_question` JSON：规范 GitHub URI、可选精确 40 位
  revision、need summary 与来源 interaction/directive/decision/question lineage 一并哈希。自由文本 URL 仍是普通 soft
  问题，不能升级为 import authority。plan 只能逐字回引 authority hash，host connector 再 exact resolve revision/license。
- `stuck` 必须同时命中 lifetime `visit_count` 与当前 goal version 的连续 inconclusive 阈值。冻结 DDL 没有后一个字段，
  因此新增严格 append-only `question_inconclusive` decision 账本；goal amend 保留 lifetime visit，但新版本 streak 从零开始。
  普查至多一次；有结果只原子创建独立参照子题和原题 `question_dep`，原题不登记 candidate/import、不发
  `import_defer`。子题在自己的 action-cycle 按冻结 authority 激活父轮结果，不重复联网。
- `sota_reference` 只接受 policy allowlist 内的 HTTPS paper/benchmark URI，redirect 仍受 allowlist 约束，响应有时间和
  byte 上限。原始 bytes 写入私有、fsync 的 SHA-256 内容寻址 blob；URI/时间不是事实权威，blob/hash 才是。随后同样
  派生独立 baseline-reference 子题，在子题轮激活冻结 repo 结果。
- trusted trigger 的 request、runner_call、receipt、source blob、completion decision 与 candidate/license review 全部交叉
  对账。receipt 已写而 DB 未提交时只 finalize；子题激活只读父 receipt。authority receipt 在读文件前先重建规范路径，
  blob 用 no-follow + inode/device/size/hash 复核，activation completion 额外核 decided_cycle/policy_hash。
- stuck/SOTA 创建子题时，plan phase_commit、原题释放、dependency、child authority、cycle done 和下一题选择在同一短
  事务完成。AttackStages 把该 terminalization 当成功控制流，不再继续 reviewer/bundle；compiler 可只读诊断已
  terminalized 的 origin cycle。子题被 prune 时 pending dependency 变 blocked，原题可重新规划，不留永久楔死。
- 默认 `new_structure` 服务继续独立；有 authority 的问题或真正命中 stuck 双阈值的原问题不得借它改写来源。
  高 lifetime visit 但没有当前 goal streak 时不会被误判 stuck。
- 本检查点没有 DDL migration：authority/progress/completion 复用 append-only `decision.type`，候选/license 使用既有冻结表。
  不可信 adapter 仍在 adversarial sandbox 前 fail-closed；CP11.4a 达成的是可信发现、登记、调度与恢复，不是敌对执行。

## 改动文件

- `meta-research/orchestrator/import_authority.py` — 新增 human directive/reference child 的严格 authority 协议、规范哈希与
  decision/directive/interaction/question lineage 校验。
- `meta-research/orchestrator/import_triggers.py` — 新增三闸 router、bounded HTTPS snapshot provider、可信 trigger 服务、
  内容寻址 source blob、独立参照题原子转交、冻结激活和崩溃恢复/腐化拒绝。
- `meta-research/orchestrator/question_progress.py` — 新增 goal-version scoped consecutive-inconclusive append-only 账本；
  与 lifetime visit 独立并在旧库缺事件时 fail-closed。
- `meta-research/orchestrator/console.py` — 结构化 human-named 注题、强确认与 exact authority 消费；自由文本不造权威；
  prune child 时原子阻断 pending question dependency。
- `meta-research/orchestrator/attack_stages.py` — 识别可信 sidecar 已原子 terminalize 的成功控制流，核 cycle/phase_commit 后退出。
- `meta-research/orchestrator/compiler_sqlite.py` — 投影四类互斥 import 权限、authority/完成状态、独立 stuck streak 来源和
  terminalized origin 只读诊断；候选为空时禁止 import_defer。
- `meta-research/orchestrator/import_search.py` — 扩展四种封闭 request 形态、human exact resolve，并阻止 authority/stuck
  masquerade 为 `new_structure`。
- `meta-research/orchestrator/statestore_sqlite.py` — inconclusive 状态与 progress decision 同事务写入。
- `meta-research/orchestrator/statestore.py` — prune child 时同步把 pending question dependency 置 blocked。
- `meta-research/orchestrator/run.py`、`stage_provider.py`、`__init__.py` — 默认惰性装配 router/reference provider、sidecar
  校验与组件索引。
- `meta-research/policies/policy.yaml`、`schemas/policy.schema.json` — 增加参照来源 allowlist/响应上限/每次子题上限，并明确
  lifetime visit 与 current-goal streak 两种阈值。
- `meta-research/schemas/import_search_request.schema.json` 与正反 fixtures — 冻结 new_structure/stuck/SOTA discovery 及
  human/stuck/SOTA authority activation 的 oneOf 契约。
- `meta-research/prompts/skills/plan/SKILL.md`、`README.md` — 教学四种 sidecar 边界、冻结来源语义及仍未解决的敌对沙箱边界。
- `meta-research/tests/test_import_triggers.py` 及 attack/compiler/console/import/run/stage/state 测试 — 覆盖权威来源、双阈值、
  零结果、独立子题、冻结激活、内容/路径篡改、receipt 恢复、terminalized 交接、默认装配和 prune 逃生。

## Review（codex-chatgpt gpt-5.5/xhigh）

- 第 1 轮：`codexro-review` 在产出 verdict 前因独立账号 401 `token_invalidated` 失败；证据目录
  `/tmp/codexrev.0k4cNr/`。没有把基础设施故障记为批准。
- 第 2 轮（上限）：完整 staged diff 内联只读审查，输出 `/tmp/cp114a3-review-round2.md`，结论
  `REQUEST_CHANGES`（1 BLOCKER、3 SHOULD、1 NIT）。逐项核实后全部采纳：
  - BLOCKER：累计 visit 被同时当作 consecutive inconclusive。新增独立 progress decision 账本，并补高 visit/零 streak
    不误触发、goal amend 清 streak 的回归。
  - SHOULD：authority receipt 在规范路径校验前读取。改为先由 origin cycle/request hash/runner id 重建并比对路径。
  - SHOULD：source blob 名称仍按 call id，不是真内容寻址。改为全 SHA-256 路径，并先核 provider hash 与 bytes 一致。
  - SHOULD：terminalized origin cycle 不能被 compiler 诊断。新增只读 terminalized 状态，所有写权限均为 false。
  - NIT：activation license review 未核 current cycle/policy。completion 增加 policy hash，并交叉核对 review 行。
- 已到两轮上限，不发第 3 轮；成立项全部在功能提交前修复并做定向回归。

## 验证

- 开发期相关验证：schema/stage/console/compiler/import 主组 **217 passed**；控制/connector/state/run 组
  **392 passed**；attack/cost/database 组 **130 passed**；最后 exact 组 **17 passed**。
- 外审修复后，五个直接相关测试文件首次组合运行：**174 passed，1 个旧 fixture 失败**；失败夹具只手工抬高 visit、
  没有新的 streak 账本，修正为真实双阈值历史后该用例通过。随后新增/修复的 exact 回归（attack terminalization、
  high-visit/no-streak、goal-version reset、compiler flags）均通过；按用户要求未重跑大组合。
- 提交边界唯一一次全量：`pytest -q` → **`1322 passed in 310.97s (0:05:10)`**；无失败、没有第二次全量。
- `py_compile` / `compileall`、`git diff --check`、`git diff --cached --check` 均通过。
- CP11.4a 验收结论：默认 import/dependency_wait、独立 plan review、四种可信发现/来源、candidate/license 四锚和崩溃恢复
  已闭合；父项完成。该结论不包含 CP11.4b/c 的 fd-safe artifact、精确供应商补账、敌对隔离或 100+ 轮真实外调 soak。

## 遗留 / 回退

- CP11.4b：artifact capability/fd-safe 消费，消除 checkpoint hash/open TOCTOU；provider invocation ID、usage/billing
  receipt 与 runner_call exactly-once 对账。
- CP11.4c：container/cgroup/VM 或等价敌对隔离、guardian/receipt 防篡改、跨节点 VEPFS owner 验收，以及含真实外调和
  失败注入的 100+ 轮 soak。
- 回退前停止 orchestrator，确认没有 `phase='import_search' AND status IN ('created','running')` 的调用，备份 DB、
  `state/import-trigger/` receipts/blobs 和 cycle staging；执行
  `git revert a31658e8ddc6a3328ec1b4aba5f860d38b436f5d`。本提交无 DDL migration，但旧代码不理解新 authority/progress
  decision 与 source activation receipt，不应在 trigger/finalize 在途时热回退。
