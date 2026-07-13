# 0091 · CP11.4c.3d.2a production acceptance handoff

- date: 2026-07-13
- functional commit: `acb06840867fc350b360086c3d8814fa12942086` — `feat(run): add production acceptance exit mode`
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.3d.2a（目标环境运行与 `fixed_and_test` 出口证据交接；不等于目标环境真实验收通过）

## 结论

目标设施 operator 现在有一份单一、可执行、fail-closed 的生产运行与出口证据 runbook。它把已有 production
preflight、两节点 canary、真实 connector、≥200 轮计数门、固定故障 schedule、registered mirror/restore、
evidence pack 与 T1/T2 qualification 串到同一最终 commit，并按用户指定的 `fixed_and_test` 分工生成机器矩阵、
原始证据和人工裁决入口。

默认 `orchestrator.run` 继续在研究终态后常驻回答 query；新增显式 `--exit-after-research` 有界常驻模式，在
pause/文件请求期间继续等待和续跑，只在 terminate/累计 max-cycles 后排空已接纳交互、flush outbound 并以 0
退出。它与 `--once` 互斥。生产 soak 因而既保留运行期交互，又能让最终计数与出口证据命令机械收口。

本检查点闭合的是**交接协议与退出能力**，不是生产通过证明。当前机器的 development probe 与 single-node
canary 均诚实拒绝升级为 production/two-node；真实目标设施、真实 ≥200 轮、故障注入、T1/T2 和用户签署仍未完成。

## 决策与修改

- `meta-research/PRODUCTION_ACCEPTANCE.md`：新增唯一目标运行手册，逐项映射 reference §7.1/§7.3/§7.4/§7.5
  与 CP11.4c.3d.2，不新造较窄判据。运行前冻结最终 commit、目标设施、用户真实任务预期、T1/T2 数据/统计口径
  和签署角色；任何未执行/非零/receipt 谓词不满足均保持未验证。
- production preflight 使用专门空 root 且 exact 检查 `deployment-preflight-v2` final receipt 的
  `production_ready=true`；preflight receipt 不跨 work-root/node 授权。single-node canary 只作先决条件，
  two-node receipt 与 STONITH/诚实 crash-stop 边界分开。
- soak 使用 `--exit-after-research`，最终仍以 `cycle.status='done' AND route IS NOT NULL` 的新增数量 `>=200`
  为门。owner SIGKILL 不再塞进普通 fail-fast record：专用记录只接受 shell exit 137，并须与 exact pinned-owner
  fault schedule 的 run/verify 配对；随后明确重启、payload SIGKILL、恢复后新成功轮与最终计数。
- evidence handoff 以 `日期 + 随机 RUN_ID` 建不可覆盖目录。operator archive 和共享 evidence 各有 SHA256 manifest；
  source 封存前拒绝 symlink、multi-link 和特殊文件，复制逐祖先拒绝 symlink、逐文件比较，并在复制后重验 operator
  manifest。token/auth/sealed labels/私有正文留 operator-only；仅脱敏摘要不能冒充原始证据，未授权审阅前不能标绿。
- `orchestrator/run.py`：增加与 `--once` 互斥的 `--exit-after-research`，只在显式选择时向既有
  `run_forever(..., linger_after_terminal=False)` 传参；默认调用签名和常驻语义不变。
- `tests/test_run.py`：固定 CLI 参数传递、正常关闭、输出与 argparse 互斥；既有 run_forever 回归继续证明
  pause/file request 跨重入等待且累计 max-cycles 不重置。
- `meta-research/README.md`：入口页链接 production acceptance runbook，并明确手册本身不是通过证明。

## review

- 第 1 轮 `/tmp/codexrev.hBWspo/verdict.md`：`REQUEST_CHANGES`。BLOCKER 是初版 soak 使用默认终态 linger，
  因而命令永不返回、≥200 计数门不可达；SHOULD/NIT 是 clean check 漏 untracked、共享/敏感证据边界与历史
  no-clobber/hash manifest 不清、receipt 复制未统一留档。新增 CLI 前先写失败测试，随后全部修正。
- 第 2（最终）轮 `/tmp/codexrev.dfESk1/verdict.md`：`REQUEST_CHANGES`。两个 BLOCKER 是普通 `record` 无法接受
  计划内 owner SIGKILL/重启链，以及 `test -f` + `cp` 会跟随 symlink 泄露 operator-only 文件；两个 SHOULD 是
  share list 未机械自包含、fresh `acceptance/evidence` 父目录未处理。按两轮上限不再发第 3 轮；全部反馈成立，
  已按上节专用 137 receipt、schedule pairing、no-symlink/multilink closure、自包含检查与父目录门修正。
- reviewer 确认新增 CLI 复用既有受控 drain/flush 路径，且 `last_block_reason` 的 pause/file request 不会被
  `last_stop_reason` 误当研究终态；reviewer 保持只读未运行 pytest。

## 验证

- TDD 红灯：新增参数测试先因 `unrecognized arguments: --exit-after-research` 失败；实现后 exact CLI/互斥/
  默认回归 `4 passed`。
- `python -m pytest tests/test_run.py -q`：`61 passed in 26.27s`。
- `python -m pytest tests/ -q --basetemp /vepfs-mlp2/c20250511/250806010/mxm/paper_agent/.pytest-cp114c3d2a`：
  `1843 passed, 1 skipped in 1634.46s`，零 failure/error。
- `python -m compileall -q orchestrator tests` 返回 0；所有 `PRODUCTION_ACCEPTANCE.md` bash fence 合并后
  `bash -n` 返回 0；`python -m orchestrator.run --help` 显示 `[--once | --exit-after-research]`；
  expected-SIGKILL shell smoke 得到 137；`git diff --cached --check` 返回 0。

## 当前目标设施诊断

- 2026-07-13 当前 host 为 root、GPFS work mount、宿主可见 8×A100；但 Docker socket 是 rootless proxy symlink，
  daemon `CgroupDriver=none`，memory/CPU/PID limit 不可用，无 NVIDIA container runtime，Docker root 余量约
  3.46 GiB；没有权威 GPFS byte+inode hard-quota probe、生产 attestation、第二节点分配或生产 connector。
- 正式零轮 probe 成功写 development receipt，但 exact `production_ready=false`；production checks 对 service UID、
  attestation、direct socket/private cgroup、quota、resource headroom 与 GPU runtime/canary 均 fail-closed。
- 当前 GPFS single-node 五阶段 canary local/verify 通过，但 receipt 诚实保持 `two_node_verified=false`、
  `shared_fs_ready=false`、`infrastructure_fence_verified=false`。这些是拒绝理由和目标设施交接输入，不是验收证据。

## 遗留与回退

- CP11.4c.3d.2、CP11.4c.3d、CP11.4c.3 与 CP11.4c 均保持未勾：须在 dedicated non-root VM/private cgroup、
  NVIDIA Docker、权威 GPFS byte+inode quota、第二节点、生产 connector 与独立故障域归档就位后实际执行 runbook。
- 仍须完成真实 ≥200 轮（真实 Codex/import/train/eval）、owner/payload/daemon/budget/resource faults、registered
  mirror/combined restore、完整站点恢复边界、T1/T2 一次性 qualification、最终版本机器矩阵和用户真实运行签署。
- 功能回退：`git revert acb06840867fc350b360086c3d8814fa12942086`。回退会移除有界常驻 CLI 与交接手册；
  已产生的外部 operator/fixed_and_test evidence 是审计制品，不应随代码回退删除或覆盖。
