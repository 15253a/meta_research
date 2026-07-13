# 0085 · CP11.4c.3d.1.3 exact measurement reference

- date: 2026-07-13
- commit: `c83e34f8f7874c1830ffc64914bceecc1bad06b1` — `fix(reasoning): expose exact measurement evidence refs`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.1.3（属：步⑪ CP11.4c.3d 基本可用 / 目标生产运行）

## 问题与最小修复

真实 c3 已产出四项成功 `metric_result`，但 `SqliteCompiler` 把行 ID、metric definition ID、version 和值
拼成 `mr1:1@1=0.9339375(aggregate)`。只有 `mr1` 是 Gate 可消费引用；模型复制等号左侧复合串后，
`gate_close_question` 以“metric_result_id 不存在”拒绝。

固定锚改为一层明确展示：

```text
successful_measurements=[evidence_ref=mr1; metric=1@1; value=0.9339375; scope=aggregate]
```

reasoning skill 明确 `metric_result_id` 只复制 `evidence_ref` 的 `mrN`，其他字段只是展示元数据。没有改 Gate、
answer schema、DDL 或 evidence 契约，也没有加 parser/adapter/配置层。

同一 skill 仍由 M0Driver 使用。内部首审指出“当前真实、历史 fake”的初版区分会误伤 M0 当前轮；最终规则改为
只按**该测量自身的显式 provenance**：固定锚给 `evaluation.source=fake` / `source=fake` /
`synthetic=true` 才注明 fake；production `successful_measurements` 未带这些标记时不得从 goal 文案、skill
版本或旧说明猜成 fake。

## 改动文件

- `meta-research/orchestrator/compiler_sqlite.py` — exact `evidence_ref=mrN` 与展示元数据分栏。
- `meta-research/prompts/skills/reasoning/SKILL.md` — exact 引用和 provenance-only fake 规则。
- `meta-research/tests/test_compiler_sqlite.py` — 锁定 DB 来源、exact 格式与旧复合 alias 消失。
- `meta-research/tests/test_skills.py` — 锁定正反提示词契约。
- `meta-research/tests/test_driver.py` — 证明共享 M0 当前 reasoning pack 仍显式 `synthetic=True`。

## 验证

- 两条精确回归先失败后通过；M0 兼容修订的 skill 回归也先失败后通过。
- `tests/test_compiler_sqlite.py tests/test_skills.py tests/test_driver.py`：`56 passed in 1.06s`。
- `python -m py_compile` 与 `git diff --check` 通过。
- 新鲜三轮真实 smoke：bootstrap/decompose/idea/plan/plan-review/bundle generation 均成功；目标在 Docker
  container create 因当前 `/ebs` ENOSPC 失败，无 metric，reasoning 按证据不足正常置 inconclusive 并选诊断题。
  该轮只记环境失败，不冒充本修复验证。
- 定向真实 replay：复制旧 c3 权威 DB 到隔离 work-root，只为探针把 c3/q2 恢复为 reasoning 前状态；compiler
  读四项真实 metric，真 Codex 输出 `question_id=q2`、`verdict=answered`、`metric_result_id=mr1`，正文
  `mentions_fake=false`；使用 production 同款 parser-suspect 负向过滤的 Gate 成功返回 `a1`。这是定向 replay，
  不声称官方 restore/resume 或完整新鲜轮。
- 同原始、未改写 c3 snapshot 经 ConsoleData spool + 正式 `--max-cycles 0 --once --no-outbound`：
  `interaction_query` runner_call rc16 success，`responder_kind=codex`，reply 绑定 snapshot c3；研究轮推进 0。
- 内部首审给出一个 Major（M0 provenance），修复后终审 APPROVE、无剩余 blocker/major。依用户提速要求，
  本小检查点未再启动外部长审；仓库全量只留最终验收前执行一次。

## 边界与回退

- 当前已达到单节点 development **代码级基本可用**：真实阶段链、CPU Docker execution、双 review、
  reasoning→Gate 和 tool-free query 分别有真实证据；这些证据尚未合并为目标环境上的新鲜长跑。
- 当前节点仍不具 production 条件：rootless Docker 无 cgroup/NVIDIA runtime，`/ebs` 无持续 headroom，缺
  GPFS fileset quota 权威证明、第二节点和生产 connector。≥200 轮、fault 组合、T1/T2 与最终全量仍未做。
- 回退：`git revert c83e34f8f7874c1830ffc64914bceecc1bad06b1`。
