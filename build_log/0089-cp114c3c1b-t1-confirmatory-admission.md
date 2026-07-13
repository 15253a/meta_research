# 0089 · CP11.4c.3c.1b T1 confirmatory admission

- date: 2026-07-13
- functional commit: `e1654fa7a4447065f8671cd865d2e1f5f42925f3` — `feat(qualification): close CP11.4c.3c.1b confirmatory admission`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3c.1b（闭合 CP11.4c.3c.1 与 CP11.4c.3c 工具项；不等于目标环境验收）

## 结论

T1 现在有一条可机械回放的 A→B→一次性 C→root audit→D 准入链。B 后普通研究不能再挂
explore views；C 在 spawn 前永久 spent，只能恢复同一 guardian/sandbox 结果，失败或不确定后不重跑；
DREAMER 始终不进 C。D、final marker 消费、mount firewall 和 root score 都重放 C/audit 全链，不再接受
单独 audit JSON 或预置 host output。T2 保持无 C 的冻结 3×15 final 协议。

这仍只是机械隔离与 authority 链，不会把 operator assertion 冒充科学重算。LODO 隔离、指标/统计、
controls 与 novelty 由命名 root evaluator 给出带 evidence digest 的显式 verdict；真实生产验收和用户签字仍在
CP11.4c.3d.2。

## 决策与修改

- `qualification_firewall.py`：final marker 升为 v3 并绑定 confirmatory audit hash；B boundary 后只有
  `qualification-confirmatory` 能精确挂全部冻结 explore views，普通研究永久关闭。导出的
  `consume_final()`、final mount 授权和 marker 验证都延迟调用 runner 的 deep admission verifier，回放
  spent/result/guardian/promotion/output/audit-input/external-authority/root-decision 整链。
- `qualification_runner.py`：新增 T1-only `run-confirmatory` 与 `audit-confirmatory`。C 绑定 B 的 exact
  source/tree/command/GPU mode，spent-before-spawn，以 guardian terminal+drained receipt 恢复而不重跑；
  `confirmatory.json` 须 canonical、fold 精确覆盖冻结 LODO 且排除 DREAMER。首次 spend 前整个 run
  namespace 必须不存在，success 还要求 deterministic sandbox `promoted.json` 仅含同 hash/bytes 的
  `confirmatory.json`。
- root audit 输入和派生 authority 共用 256 KiB 上限，先检查 external path/已有内容、两个
  research copy 冲突与所有派生 bytes，不因 operator 路径错误留半链。sealed truth 旁的 root-owned
  `.meta-research-qualification-decisions-v1` 以 canonical work-root 为唯一 key，先发布 `0444`
  immutable verdict，再发布 external authority 和可修复的 `$WORK` copies。因此 research UID 删除 ref 或
  crash 于发布中途都无法将 failed 换成 passed，exact retry 只会修复原链。
- `test_qualification_firewall.py` / `test_qualification_runner.py`：增加 post-B mount fence、standalone audit
  绕过、伪造 marker、spent/recovery/no-rerun、DREAMER 排除、guardian/promotion 删除、preseeded namespace、
  authority 冲突零残留、near-limit 原子拒绝、decision-first crash repair、failed verdict 不可替换与
  D/score 深度复验等回归。
- `QUALIFICATION.md` / `README.md`：改写为真实 A→B→C→audit→D runbook，明确 machine
  success 与 scientific/human acceptance 的边界、legacy v2 不回填、root decision 备份义务，以及真实
  跨 UID 下 authority 目录需 root-owned `0711`（可按已知路径遍历，不可列目录/写入）。

## review

- 第 1 轮 `/tmp/codexrev.zZ6SlG/verdict.md`：`REQUEST_CHANGES`。BLOCKER 是导出 `consume_final()` 只验
  standalone root audit，可绕过本地 C lifecycle；SHOULD 是 near-256-KiB input 可派生超限
  authority 并在发布后自锁；NIT 是通用发布器仍输出 score-final 误导文案。三项均修，并增
  standalone bypass、near-limit 零发布和 label 闭包。
- 第 2（最终）轮 `/tmp/codexrev.R0pWQo/verdict.md`：`REQUEST_CHANGES`。BLOCKER 是 sandbox 晋升允许
  已有同 bytes destination，C 没有要求 promotion ledger 真正包含输出；两个 SHOULD 是错误/冲突
  authority path 会先留 research input copy，以及 failed verdict 仅靠 research-owned refs 不具永久性。
  按工作流到两轮上限后不再发第 3 轮；三项全部成立并修正。
- 第 2 轮后自审进一步发现 external authority 先于 decision 发布仍有崩溃换 verdict 窗口，
  改为 immutable decision-first，用故障注入证明崩溃后只能 exact repair。同时修正 runbook `0700`
  authority 目录会阻断真实 research UID 读取的问题。

## 验证

- firewall + runner：`52 passed in 19.28s`。
- qualification data/firewall/metrics/runner：`142 passed in 26.67s`。
- execution sandbox/process guardian/reconcile/run/deployment 相邻回归：`148 passed in 133.50s`。
- 按 README 从 `meta-research/` 目录运行唯一有效全量：`1818 passed, 1 skipped in 1493.36s`，
  零 failure/error。
- `QUALIFICATION.md`：9 个 JSON block 全部 `json.loads`，16 个 bash block 全部 `bash -n`，31 个
  code blocks / 62 条 fence 成对。qualification runner 基本 help + 四个 subcommand help 五路全返回 0；
  `python -m compileall -q orchestrator tests` 和 `git diff --cached --check` 通过。

## 遗留与回退

- 当前 host 仍不是目标生产环境：Docker `cgroup_driver=none`、无 NVIDIA container runtime，也无
  dedicated VM/private cgroup、authoritative GPFS byte+inode quota attestation 或第二节点。不得宣称
  production-ready。
- root scientific audit 与最终用户验收不能由机器代签。目标环境就位后，先按
  `meta-researchv2/fixed_and_test` 与用户冻结预期，再跑真实 ≥200 轮、faults、T1/T2 和证据包。
- legacy v2 final marker 不升级/回填；新工作必须从新 work-root 走完 v3 链。root decision ledger 必须与
  sealed truth 一起备份。
- 功能回退：`git revert e1654fa7a4447065f8671cd865d2e1f5f42925f3`。已产生的 v3/decision receipts 是
  不可逆审计制品，回退代码不等于擦除它们，也不应将其降级为 v2。
