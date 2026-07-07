# implement_note.md · 施工现场（活文档，只写当下）

> 任何新 session（含换模型）开工先读本文件，从「下一步动作」接着做；用法与更新时点见 `CLAUDE.md` §9。
> 覆盖式更新，只写当下；历史去 `build_log/`（INDEX.md 索引）与 `git log` 找。
> 位置指针以本文件为权威；`ROADMAP.md`「当前位置」是记账时才同步的兜底备份（§9）。

- 更新：2026-07-07 18:00 ｜ 位置：空闲（步① M0 已完成；步② M1 待开工）
- 检查点状态：空闲

## 正在做什么
无进行中检查点。**步①（M0）已完成**：CP1.1–1.4 四检查点（commit 965d1a3 / 9ee4c45 / 7deb14a / 45641cc，build_log 0006–0009），步级验证 = 真 codex 端到端验收通过（5 轮元循环，①–④判据全过，证据见 build_log/0009）。

## 工作区状态
- 干净（待记账提交本文件+ROADMAP+build_log 0009）。
- `meta-research/questions/toy/` 有最近一次验收运行产物（gitignored，留作回放参考，可随时删）。

## 下一步动作（按序，具体到命令/文件）
1. **先向用户确认三事项，才能开 M1**（步② 验收含 DDL 冻结，不得自行填空）：
   ① 规范内部缺口：《二》§6.12 提及 import 旋钮（selection_key 排序 / policy_hash / license scope）而附录 C（policy 唯一注册表）无对应键——补进 policy 还是维持附录 C 现状？
   ② DB `evaluation.source` 枚举无 'fake'：M1–M3 假执行的 evaluation 以什么口径入真实 DB（用 factory+synthetic 标记列？还是 M1 扩枚举再 M4 收缩）？
   ③ reference/OPEN.md #1（可选 DDL 三项）与 #2（applicability 同版触发器）——按登记表须在 M1 开工时评估确认。
2. 用户确认后：把步②（M1）切检查点（建议：CP2.1 附录 A DDL 落库+checksum 锁定+否定用例（M1a 前半）；CP2.2 门禁三级校验换真+I1–I6 否定用例（M1a 后半）；CP2.3 StateStore 落 SQLite+decompose 释放断言（M1b）；CP2.4 v2.3/v2.4 表约束+隔离拒绝用例（M1c））——精读附录 A（第一部分 909–1620 行）后定稿。
3. 每检查点照 §5 循环（内审 Opus 子代理 + codex 外审 ≤2 轮）。

## 关键上下文 / 坑（新 session 不读会踩的）
- **审查类子代理一律 model:"opus"**（用户指示，见 memory）。
- M0 期定下的驱动器/编译器纪律（M1 换真时保持）：pack 变异唯一入口 = Compiler.amend（manifest 溯源）；sidecar 不走 Gate；reasoning 必产检查先于 commit；破坏性护栏不用 assert；transcript 唯一序号；route 矩阵不可达分支要显式拒。
- prompt 工程铁律（实测 6 次端到端换来的）：给工人的字段说明必须是"逐字键名 + JSON 骨架"，散文描述必然自造键；oneOf 校验错误必须展平子错误否则重试不收敛。
- M0 已知简化（M1 消化清单）：无事务收尾（→phase_commit）、业务门禁放过（→I1–I6）、baseline dep 不校验目标、验收②是编译器自一致性而非独立数据流审计（→DECISION 入账后 DB 审计）、聚合轮未真跑（单测覆盖）。
- 测试：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`（107 基线）；端到端：`/root/miniconda3/bin/python scripts/run_m0_acceptance.py --cycles 5`（花真 token，~25 调用）。
- pip 镜像源不带代理 / codex 要代理 127.0.0.1:7890。
