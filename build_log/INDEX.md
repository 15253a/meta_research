# 施工日志索引（build_log）

每完成一个检查点提交，在此追加一行（最新在上）。格式：

`- [NNNN](NNNN-slug.md) — <检查点一句话> · commit `<hash>` · 验证: 通过/未通过/未验证`

---

- [0003](0003-checkpoint-build-model.md) — 两级分解(步→检查点)+检查点闭环构建模型（codex 退到检查点边界独立外审）· commit `a45c59b` · 验证: 通过
- [0002](0002-scope-artifacts.md) — 评审范围扩到所有决策性制品（prompt/skill/系统提示/schema/接口，不只代码）· commit `2265869` · 验证: 通过
- [0001](0001-scaffold.md) — 施工脚手架 baseline（git 仓库 + 评审入口 + 施工说明）· commit `f968be9` · 验证: 通过

<!-- 在此追加记录，例：
- [0001](0001-init.md) — 初始化系统骨架 · commit `abc1234` · 验证: 通过
-->
