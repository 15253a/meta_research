# 0006 · CP1.1 契约层：schemas + policy + interfaces + goal_brief（M0 首检查点）

- date: 2026-07-07
- commit: 965d1a3 — feat: CP1.1 契约层（schemas/policy/interfaces/goal_brief + 自验）
- branch: main
- 检查点 / 步: CP1.1（属：步① M0 流程层骨架 + 资产层接口桩 + 最小驱动器）

## 决策
按 reference/ 施工标准落 M0 的契约层（被建系统落位 `meta-research/`）：
- `schemas/` 8 份 JSON Schema（draft 2020-12）：idea_set / plan / bundle_target / answer / tree_ops / selection / resource_request / policy——字段与枚举对齐《二》§6.11 与《一》附录 A DDL；M0 专属标记（evaluation.source='fake'、synthetic）在 schema 内注明来源与 M4 移除时点。
- `policies/policy.yaml`：附录 C 全量旋钮逐键逐默认值。
- `orchestrator/interfaces.py`：§6.10 全部 17 个 Protocol + 数据形状（含 WriteCommand），签名冻结、桩真共用。
- `input/goal_brief.md`：toy 目标书（frontmatter 含合法 predicate_json，§4.6.7 契约）。
- `tests/`：53 用例（schema 元校验 / 正例 / 负例带 expect 钉扎 / policy 对齐抽核 / goal_brief 启动契约 / interfaces 冒烟）。
- 顺带：ROADMAP 登记步①–⑦（=M0–M6+验收）、README 目录图与 M0 边界、requirements-dev.txt、.gitignore py 缓存。
- 前置素材提交：reference/ 原样入库（02ac305，非决策、未外审）。

## 改动文件
- `ROADMAP.md` — 修改：登记总目标 + 步①–⑦ + 步① CP1.1–1.4
- `meta-research/README.md` — 新增：目录布局 / 自验方法 / M0 边界
- `meta-research/schemas/*.schema.json` — 新增 8 份（契约层核心）
- `meta-research/policies/policy.yaml` — 新增：附录 C 全量默认
- `meta-research/orchestrator/{__init__,interfaces}.py` — 新增：接口缝
- `meta-research/input/goal_brief.md` — 新增：toy 启动输入
- `meta-research/tests/**` — 新增：conftest / test_schemas / test_interfaces / fixtures 正负例 + expect
- `meta-research/requirements-dev.txt` — 新增：测试依赖声明
- `.gitignore` — 修改：py 缓存；`implement_note.md` — 记账随提交

## Review
- 内部（superpowers 子代理）：With fixes——Critical：bundle_target 与附录 A DDL 词表错位 5 处（log_kind 六值 / content_hash / loss_trend down-up / oom_count / fold⇒checkpoint 绑定 / attempt failure_kind 八值），逐条核实 DDL 原文后修正；Important：聚合轮 needs=[] / skipped 免执行审计 / 接口参数名 target_id / evaluation_id / 负例 expect 钉扎，全部采纳；保留两处有注记的超规范约束（terminate_reason_md 必填、artifact 层 failure_kind 枚举）。
- codex（gpt-5.5/xhigh）第 1 轮：REQUEST_CHANGES——BLOCKER①import_defer 契约未焊（选择锚+占位 identity 必填、与非空 targets 互斥）②fake evaluation 未联动强制 logs/observation synthetic；SHOULD：evidence 多态互斥（oneOf 重写）、测试依赖声明。全部采纳修复 + 各配负例。
- codex 第 2 轮：REQUEST_CHANGES（达 2 轮上限）——BLOCKER：claim 未按 target_kind 必填、complete 未绑定嵌套 success、fake 联动缺反向；全部核实属实并采纳修复（负例各配 expect），按 §2.2 不再送第 3 轮
- 未采纳意见及理由：第 1 轮 NIT（implement_note 在 staged）——CLAUDE.md §9 明文约定检查点提交可含现场快照、外审 diff 已排除，不属隐藏变更。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  53 passed in 0.46s
  ```
- 覆盖：8 schema 全过 draft2020-12 元校验；正例 12 / 负例 24（每条负例有 expect 钉扎、防 fixture 失效空转）；policy.yaml 过 schema + 附录 C 关键默认值抽核；goal_brief frontmatter 契约 2 负例；interfaces 3.9 导入 + 17 Protocol 清单断言。
- 运行前提 #1（§7.5）：codex Runner 冒烟 `codex-chatgpt exec` EXIT=0、输出「就绪」（tokens 1,779）。
- 结论：通过（步①未收尾，步级验证待 CP1.4）。

## 遗留 / 回退
- 待办→CP1.2：prompts/system_prompt + 四阶段 SKILL（草稿已在 scratchpad cp12/）+ idea_audit / plan_review 两份过程 schema + 清单测试更新。
- 待办→CP1.3：goal_brief 解析规则从 tests 移入 orchestrator（测试反向 import）。
- 待办→M1 开工前问用户：①《二》§6.12 提及 import 旋钮（selection_key 排序/policy_hash/license scope）而附录 C 无对应键（规范内部缺口，未自行填空）；②DB evaluation.source 枚举无 'fake'，M1–M3 假执行入账方式；③连同 OPEN #1/#2。
- 回退：`git revert 965d1a3`（纯新增文件为主，可整体回退；reference/ 素材提交独立不受影响）。
