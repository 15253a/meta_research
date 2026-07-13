# 0088 · CP11.4c.3d.2 production runbook executable audit

- date: 2026-07-13
- functional commit: `7a32e9740b9c765d9465d6ae8b4d856aa41a50f4` — `fix(qualification): fail CLI on scientific failure`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.2 目标运行前的可执行性复审（未完成目标环境验收）

## 结论

现有普通研究主链与 sealed final runner 可以实际运行，但原 runbook 不能安全地从 production preflight 一路串到
最终 verdict，且把 T1 的 DREAMER D 结果写得过于接近完整 A→B→C→D。此次不新建验收 DB、daemon、scheduler
或第二套研究状态机，只修会让操作员直接跑错/误判成功的 CLI 与手册，并把未闭合项重新标回 ROADMAP。

## 成立问题与修改

- qualification 的旧顺序误把 claim-lock 放在首轮 DB/cycle 前；现按参考概念改为 contract → A 探索 → 停 owner
  → B claim-lock → final，并补真实 `orchestrator.run` 命令、task-specific contract/claim 片段、canonicalizer 与
  exact GPU allocation extraction。
- 进一步确认 T1 stage C 尚无一次性 LODO runner/scorer、B high-water/exact-source binding 或 D admission gate；
  `QUALIFICATION.md` 与 ROADMAP 现明确只把既有能力称为 D sealed boundary，CP11.4c.3c/.1 回退为未完成。
- `qualification_runner` 过去对 durable `failure_count>0` / `status=failed` 仍返回 0；现保留失败回执供审计，
  但 CLI 返回 3，成功才返回 0，并在 runbook 另给 batch/result `jq -e` 双门。
- SQLite-only restore 不含 connector producer/cursor authority；resume probe 改为 `--no-outbound`，并说明
  production target 必须使用绑定新绝对路径的新鲜 attestation，否则只能诚实运行 development probe。
- two-node canary 进入 evidence pack 时显式使用真实冻结 scope `two-node-process-crash`，不再误用默认
  `single-node-prerequisite` 或不存在的简写。
- soak 示例显式 `--max-cycles 200`，并用 fail-closed 的 SQLite 前后计数只接受新增 ≥200 个
  `status='done' AND route IS NOT NULL` 的研究轮；新 work-root 基线为 0，查询错误/非整数均退出 2。

## 验证

- `tests/test_qualification_data.py`：**23 passed**（显式安全 `--basetemp`；默认 world-writable `/tmp` 会按设计拒绝）。
- firewall/metrics/runner：**85 passed**；合计 qualification 相关 **108 passed**。
- 新增 CLI outcome 回归：success/failure 的 run-final/score-final 四路均验证；runner 文件定向 **11 passed**。
- `qualification_runner.py` py_compile、6 个 runbook JSON 示例解析、soak 三段 shell blocks `bash -n`、
  evidence `two-node-process-crash` argparse 与 `git diff --check` 均通过。
- 可执行性只读复审先后发现 scope 常量、resident linger、duplicate-key 与 GPU receipt gate 等成立项；逐项修复后
  最终 verdict **APPROVE**，无剩余 BLOCKER/Major。
- 按用户要求，本中间检查点未跑全量；最终只跑一次全量。

## 仍未完成

- CP11.4c.3c.1b：T1 B→C→D 的最薄机械边界与 confirmatory data/scorer receipt。
- 当前容器不是目标部署：root service、Docker 无 cgroup resource limit/NVIDIA runtime、根盘余量低，且没有
  authoritative GPFS hard byte+inode quota attestation 或第二节点，production preflight 应继续 fail-closed。
- 目标环境的真实 ≥200 轮、daemon/resource/budget faults、connector ACK、two-node canary、T1/T2 与最终一次全量
  仍属于 CP11.4c.3d.2，不在本提交中虚报通过。

功能回退：`git revert 7a32e9740b9c765d9465d6ae8b4d856aa41a50f4`。文档/ROADMAP 可独立回退其后续
docs commit，不影响已产生的 qualification immutable receipts。
