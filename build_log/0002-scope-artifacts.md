# 0002 · 评审/记账硬流程扩到所有决策性制品（不只代码）

- date: 2026-06-17
- commit: 2265869 — docs(scope): 评审/记账硬流程扩到所有决策性制品（不只代码）
- branch: main

## 决策
用户要求：改动 prompt / skill / 系统提示 等也要审核，不只是改代码。故把"决策性改动"范围从"代码"明确扩到一切改变系统行为/契约的制品（prompt、skill/`SKILL.md`、系统提示、JSON schema、接口定义、配置 + 代码）。契合本项目"流程层先行（skill + system_prompt + 接口桩）"的打法——很多"代码"本就是 prompt/skill。

## 改动文件
- `CLAUDE.md` — 修改：§0 适用范围 + 铁律 1；§1 范围定义重写（列入 prompt/skill/系统提示/schema/接口；加"措辞即行为"警示；"不算"项收窄为代码注释/排版）；§2.2 / §4 / §5 / §8 口径"代码"→"制品 / 决策"。
- `README.md` — 修改：首段范围；决策循环 step1/2；布局"动代码前先读"→"动决策性制品前先读"。

## Review（codexro-review，gpt-5.5，low effort）
- 第 1 轮：**VERDICT: APPROVE**，附 2 SHOULD + 2 NIT —— 均为我漏改的残留"代码"口径：§4「代码提交」（会让非代码制品提交误以为不必记 build_log，正好削弱本决策）、§2.2「改代码」、README「动代码前」、§8 TDD「改代码 → 自验」。
- 处置：**逐条采纳**（纯术语统一，直接服务本决策）。因第 1 轮已 APPROVE 且改动即 reviewer 原话建议、属低风险术语替换，未再起第 2 轮（§2.2 上限内）。
- 未采纳：无。

## 验证
- `bin/codex-review.sh "<决策>" -c model_reasoning_effort=low` → 产出 `VERDICT: APPROVE`。
- 性质：文档 / 约束口径统一，无行为代码改动。
- 结论：**通过**。

## 遗留 / 回退
- 待办：无新增（放行规则待办见 [0001](0001-scaffold.md)）。
- 回退：`git revert 2265869`（及本日志提交）。
