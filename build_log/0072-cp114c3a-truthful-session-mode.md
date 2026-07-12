# 0072 · CP11.4c.3a truthful session mode

- date: 2026-07-12
- commit: `b2081a34d78d3c9fa173b2c2e47643620647c99a` — fix: make CP11.4c.3a session contract truthful
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3a（属：步⑪ CP11.4c.3 最终生产验收）

## 决策

生产只开放系统真实实现的会话模式 A：一次 Runner turn 只执行一个阶段，阶段结果耐久提交后再推进。
历史策略虽接受 B，但 Runner 的窄 `run_task` 契约没有 turn 内跨阶段 callback/checkpoint 协议，运行时也没有 B
分支；继续保留会静默把 B 当 A。为先达到可用且不增加系统复杂度，本检查点不另造第二套会话状态机，而是：

- policy schema 只接受 A；旧 B 配置在创建 work root、DB 和 provider 前 fail-closed。
- `System` 直接构造同样拒绝 B，且会话模式为只读属性，构造后不能篡改成虚假 B。
- 明确记录这是相对 reference §4.4.2/§7.1 A/B 判据的受控偏离，**不宣称 A/B 实测通过**。
- 替代最终验收保留为 A 的逐阶段耐久提交、kill/restart 恢复及真实数百轮（明确下限 ≥200）长跑。

## 改动

- `meta-research/schemas/policy.schema.json`、`policies/policy.yaml`：把 `dual_mode` 冻结为 A 并解释边界。
- `meta-research/orchestrator/run.py`：构造入口拒绝 B、移除缺省回退、将 mode 暴露为不可变只读值。
- `meta-research/tests/test_run.py`、`tests/test_schemas.py`：覆盖 schema、直接构造、构造后篡改和装配前零副作用拒绝。
- `meta-research/README.md`、`views/console/index.html`、`ROADMAP.md`：删除“B 待实测”的假能力表述，记录受控偏离、
  ≥200 最终门槛，并把后续生产收口拆成存储治理、薄验收工具和目标运行，避免扩成新调度系统。

## Review

- 内部代码审查发现 `dual_mode` 原可在构造后被改成 B；改为无 setter 的只读 property 并补负例后 APPROVE。
- 内部文档审查要求显式受控偏离、≥200 下限、§7.4 输入防火墙和最小存储治理边界；全部处置后 APPROVE。
- 外审第 1 轮：只读审查账户返回 `401 token_invalidated`，未产生 verdict。
- 外审最终第 2 轮：内联全部 staged diff、禁止工具执行后，结论 `APPROVE`，BLOCKER/SHOULD/NIT 均无。

## 验证

- 相关集：`test_schemas.py test_run.py test_console_frontend.py`，唯一新 `/tmp` basetemp，
  **165 passed in 26.79s**。
- `git diff --check`、Python 编译检查通过。
- 检查点末唯一全量：

  ```text
  5 failed, 1072 passed, 1 skipped, 421 errors in 380.19s (0:06:20)
  ```

  开跑前 20G overlay 只剩 175MB；运行中 `df` 变为 0B，basetemp 达 191MB，随后大量 setup 报
  `OSError: could not create numbered dir`。清理该 basetemp 后可用空间恢复到 191MB。该全量结果被宿主
  ENOSPC 污染，不能作为本 diff 的回归失败结论；依用户“检查点只做一次全量”约束未二次重跑，也未放宽
  production preflight。相关 165 项和三路审查均通过。

## 遗留 / 回退

- 当前节点仍不是 production 节点：容量、私有 rootless+cgroup v2 Docker、NVIDIA runtime、GPFS hard quota、
  第二节点和真实 connector 均未满足；不能据本检查点声称 CP11.4c.3 或 reference M6 已验收。
- 下一检查点 CP11.4c.3b 只补最小存储治理：SQLite online 滚动备份、views Git timeline、不可变资产 manifest、
  日志归档/恢复/容量门与安全 GC；不每轮复制大 checkpoint/content store。
- 无 DDL。回退：`git revert b2081a34d78d3c9fa173b2c2e47643620647c99a`。
