# 生产目标运行与出口证据 runbook

本文件只把现有 production preflight、connector、shared-fs canary、fault schedule、storage restore、
evidence pack 与 qualification CLI 串成一次可审计运行；不新增数据库、daemon、scheduler、恢复器或验收状态机。
任何命令未实际执行、退出非零或 receipt 不满足下述谓词，都必须在矩阵中写“未通过/未验证”，不能由本文件代签。

权威锚点：

- `../reference/第三部分-实现计划.md` §7.1 M0–M6、§7.3、§7.4、§7.5；
- `../ROADMAP.md` CP11.4c.3d.2；
- 用户指定 `$FIXED_AND_TEST_ROOT/README.md` §1 与 §2.6，以及其验收矩阵、真实运行记录和放行报告模板。

这里使用的矩阵行名只引用上述已有判据，例如 `§7.1/M0`、`§7.3/2`、`§7.4/T1`；不得为了让现状好看而
另造较窄判据。机器输出与用户判断分开：building 产机器矩阵和原始证据，用户在真实运行前写预期、运行后裁决。

## 0. 不可逆运行前先冻结

以下四项缺一项都不要安装 qualification claim、不要执行 T1-C/T1-D 或 T2 final：

1. 最终 deployment commit 与 clean worktree；生产 policy 的 service、Docker socket/resource mode、GPU 数量、
   GPFS quota 和 connector profile 已经评审并冻结。
2. 目标设施身份：两台不同 machine/boot 的 dedicated non-root VM、相同 GPFS fileset/绝对路径、private cgroup、
   dedicated rootless Docker + NVIDIA runtime、byte+inode hard quota、独立故障域归档位置及基础设施 fence 责任人。
3. 用户使用 `$FIXED_AND_TEST_ROOT/templates/真实运行验证记录.md` 在运行前写下真实任务、观察目标和预期；T1/T2 的
   数据集、label rule、seeds、folds、预算、主指标、统计检验、null/control 和成功/负结论口径同时冻结。
4. 命名 operator、research UID、root evaluator 与用户签署人；明确机器不能代签科学合理性、novelty 或“好不好用”。

建议先为本轮建立一份**仓库和所有 work-root 之外**、位于独立归档故障域的新 evidence 目录。下面只定义变量，
不负责创建基础设施；路径必须替换为目标环境的规范绝对路径：

```bash
set -euo pipefail
export RUN_ID=0123456789abcdef0123456789abcdef
export ACCEPTANCE_DATE=YYYY-MM-DD
export REPO_ROOT=/absolute/meta-research-building
export SYSTEM_ROOT="$REPO_ROOT/meta-research"
export PREFLIGHT_WORK_ROOT="/gpfs/meta-research/preflight-$RUN_ID"
export WORK_ROOT="/gpfs/meta-research/soak-$RUN_ID"
export RESTORE_ROOT="/gpfs/meta-research/restore-$RUN_ID"
export CANARY_ROOT="/gpfs/meta-research/canary-$RUN_ID"
export LOCAL_CANARY_ROOT="$CANARY_ROOT-local"
export CONNECTOR_PROFILE=/absolute/private/outbound.json
export OWNER_FAULT_SCHEDULE=/absolute/private/owner-fault-schedule.json
export PAYLOAD_FAULT_SCHEDULE=/absolute/private/payload-fault-schedule.json
export EVIDENCE_ROOT="/independent-archive/meta-research/$RUN_ID"
export FIXED_AND_TEST_ROOT=/absolute/meta-researchv2/fixed_and_test
export ACCEPTANCE_ID="$ACCEPTANCE_DATE-$RUN_ID"
export ACCEPTANCE_EVIDENCE_ROOT="$FIXED_AND_TEST_ROOT/acceptance/evidence/$ACCEPTANCE_ID"
export ACCEPTANCE_MATRIX="$FIXED_AND_TEST_ROOT/acceptance/$ACCEPTANCE_ID-验收矩阵.md"
test "$(id -u)" -ne 0
test -d "$REPO_ROOT/.git"
test -d "$SYSTEM_ROOT/orchestrator"
test -f "$FIXED_AND_TEST_ROOT/README.md"
test -f "$FIXED_AND_TEST_ROOT/templates/验收矩阵.md"
test -f "$FIXED_AND_TEST_ROOT/templates/真实运行验证记录.md"
test -d "$FIXED_AND_TEST_ROOT/acceptance"
test ! -L "$FIXED_AND_TEST_ROOT/acceptance"
if test ! -e "$FIXED_AND_TEST_ROOT/acceptance/evidence"; then
  mkdir -m 0750 "$FIXED_AND_TEST_ROOT/acceptance/evidence"
fi
test -d "$FIXED_AND_TEST_ROOT/acceptance/evidence"
test ! -L "$FIXED_AND_TEST_ROOT/acceptance/evidence"
command -v git >/dev/null
command -v python >/dev/null
command -v docker >/dev/null
command -v nvidia-smi >/dev/null
command -v sqlite3 >/dev/null
command -v jq >/dev/null
command -v realpath >/dev/null
command -v sha256sum >/dev/null
command -v stat >/dev/null
command -v install >/dev/null
command -v cmp >/dev/null
test "${#RUN_ID}" -eq 32
case "$RUN_ID" in *[!0-9a-f]*) echo "RUN_ID must be 32 lowercase hex" >&2; exit 2 ;; esac
test "$ACCEPTANCE_DATE" != 'YYYY-MM-DD'
test "${#ACCEPTANCE_DATE}" -eq 10
test "$(realpath -e -- "$FIXED_AND_TEST_ROOT")" = "$FIXED_AND_TEST_ROOT"
test "$(realpath -e -- "$FIXED_AND_TEST_ROOT/acceptance/evidence")" = \
  "$FIXED_AND_TEST_ROOT/acceptance/evidence"
test ! -e "$PREFLIGHT_WORK_ROOT"
test ! -e "$WORK_ROOT"
test ! -e "$RESTORE_ROOT"
test ! -e "$CANARY_ROOT"
test ! -e "$LOCAL_CANARY_ROOT"
test ! -e "$EVIDENCE_ROOT"
test ! -e "$ACCEPTANCE_EVIDENCE_ROOT"
test ! -e "$ACCEPTANCE_MATRIX"
umask 077
mkdir -m 0700 "$EVIDENCE_ROOT"
git -C "$REPO_ROOT" diff --exit-code
git -C "$REPO_ROOT" diff --cached --exit-code
test -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$REPO_ROOT" rev-parse HEAD > "$EVIDENCE_ROOT/00-commit.txt"
record() {
  label="$1"
  shift
  output="$EVIDENCE_ROOT/$label.txt"
  status="$EVIDENCE_ROOT/$label.exit"
  test ! -e "$output"
  test ! -e "$status"
  set +e
  "$@" > "$output" 2>&1
  rc="$?"
  set -e
  printf '%s\n' "$rc" > "$status"
  return "$rc"
}
cd "$SYSTEM_ROOT"
```

`EVIDENCE_ROOT` 不能位于 source/restore/canary/repository root 内；最终由 operator 只读归档。后续同一 operator
shell 使用上面的 `record`，把 stdout+stderr 与退出码分别保存为 `.txt` / `.exit`；新 shell 必须先重新定义该函数。
不能只复制摘要。含 token、auth 或 sealed labels 的输出不得进入 research UID 可读的证据目录。

## 1. Production preflight：先证明环境，再跑研究

生产 policy 必须是 `deployment.mode=production`，`attestation_path` 指向部署者/root 持有、当前 service 只读且
不超过 300 秒的 v2 attestation。attestation 的精确 schema 是 `schemas/deployment_attestation.schema.json`；
禁止用 `statvfs` 代替 GPFS fileset byte+inode hard quota，或把整机 GPU inventory 冒充本 service 的 exact allocation。

用专门的空 preflight work-root 做零轮启动。它仍走真实 Docker preflight、exact GPU DeviceRequest canary、connector
profile 装载和 owner lease，但不消耗研究轮：

```bash
set -euo pipefail
record 10-production-preflight \
  python -m orchestrator.run \
  --system-root "$SYSTEM_ROOT" \
  --work-root "$PREFLIGHT_WORK_ROOT" \
  --max-cycles 0 --once \
  --connector-profile "$CONNECTOR_PROFILE"
DEPLOYMENT_RECEIPT="$(find "$PREFLIGHT_WORK_ROOT/state/deployment" \
  -maxdepth 1 -type f -name 'deployment-*.json' -print)"
test "$(printf '%s\n' "$DEPLOYMENT_RECEIPT" | sed '/^$/d' | wc -l)" -eq 1
record 11-production-receipt-check \
  jq -e '
  .protocol == "deployment-preflight-v2" and
  .phase == "final" and .mode == "production" and
  .production_ready == true and
  ([.checks[] | select(.ok != true)] | length) == 0
' "$DEPLOYMENT_RECEIPT"
record 12-production-receipt-copy \
  cp -- "$DEPLOYMENT_RECEIPT" "$EVIDENCE_ROOT/12-production-receipt.json"
record 13-production-receipt-sha256 \
  sha256sum "$EVIDENCE_ROOT/12-production-receipt.json"
```

进程退出 0 但 receipt 不是 exact `production_ready=true` 仍算失败。开发回执、缺 NVIDIA runtime、fallback rlimit、
间接 socket、root UID、quota 自报或 Docker headroom 不足都不能降级放行。

deployment attestation 精确绑定 service、boot、Docker daemon、GPU allocation 和 `work_root.path`。因此 preflight
work-root 的成功回执**不能**授权 soak/restore/T1/T2 root；每次切换 root 或接管节点，都须由部署者为新绝对路径和
当前节点原子发布一份新的 ≤300 秒 attestation，下一次 `orchestrator.run` 会重新完整 preflight。各代 attestation 与
对应 deployment receipt 都进入 operator-only evidence，不能让 research UID 自签或改写。

## 2. GPFS 单机先决、真两节点与基础设施 fence

先在目标挂载跑 local scope；它只证明五阶段机制，不得升级成 two-node：

```bash
record 20-canary-local \
  python -m orchestrator.shared_fs_canary local \
  --canary-root "$LOCAL_CANARY_ROOT" --run-id "$RUN_ID"
record 21-canary-local-verify \
  python -m orchestrator.shared_fs_canary verify \
  --canary-root "$LOCAL_CANARY_ROOT" --run-id "$RUN_ID" \
  --required-scope single-node-prerequisite
```

随后在两个不同 machine/boot 的节点上并发运行 holder/contender；两个终端必须使用相同 absolute root、run ID、
timeout 与 guardian grace。两个节点的新 shell 都要先加载 §0 的变量和 `record` 函数：

```bash
# node A
record 22-canary-node-holder \
  python -m orchestrator.shared_fs_canary node --role holder \
  --canary-root "$CANARY_ROOT" --run-id "$RUN_ID"

# node B，与 node A 并发
record 23-canary-node-contender \
  python -m orchestrator.shared_fs_canary node --role contender \
  --canary-root "$CANARY_ROOT" --run-id "$RUN_ID"

# 两个角色都成功返回后，在任一节点执行
record 24-canary-two-node-verify \
  python -m orchestrator.shared_fs_canary verify \
  --canary-root "$CANARY_ROOT" --run-id "$RUN_ID" \
  --required-scope two-node-process-crash
record 25-canary-two-node-check \
  jq -e '
  .status == "passed" and .shared_fs_ready == true and
  .two_node_verified == true and
  .verified_scope == "two-node-process-crash" and
  .observed_node_count == 2
' "$EVIDENCE_ROOT/24-canary-two-node-verify.txt"
```

canary 的 failure model 仅为 owner-process SIGKILL，receipt 固定不声称 infrastructure fence。目标部署还须另存
VM/平台执行 STONITH 的原始记录，证明旧主被真正 fence 后新主才接管；网络分区下旧主仍活着不能靠 flock/heartbeat
自证安全。若目标生产合同明确只支持 crash-stop 串行接管，也必须在放行报告“诚实边界”中由用户确认，而不能删掉此项。

## 3. Connector 真实闭环

生产长跑禁止 `--no-outbound`。运行前验证 profile 只引用环境变量名、不内嵌 secret；严格 webhook 接收端必须按
`(producer_id,event_key)` 持久去重并回 exact ACK。OneBot 的非幂等重复边界须写入放行报告。

在长跑中至少由用户本人完成以下真实交互，并保存远端截图/日志与本地 ingress/outbox/receipt 原件：

1. status query：远端 ACK、入站 fsync ACK、只读回答与同一 conversation 身份闭合；
2. hard directive：先 pending/确认，再按规定 stage boundary 消费且同记 DECISION；
3. `unclear`：只回显请确认，不自动答、不生成 directive；
4. owner restart：已 ACK 入站不重复消费，未确认出站按 transport 合同收敛。

这是 `§7.1/M5` 的目标 connector 证据；单元测试 HTTP server 或本地 outbox 不能代替真实 connector ACK。

## 4. ≥200 轮真实 soak 与故障日程

按 `README.md` §7 的 baseline/after 计数门执行，必须满足新增
`cycle.status='done' AND route IS NOT NULL` 数量 `>=200`。`--max-cycles 200`、进程退出 0 或 `MAX(cycle.id)` 都不是
通过证明。owner SIGKILL 与 payload SIGKILL 分别使用按 `README.md` §7 生成、冻结且 ID 不复用的 schedule；两个文件
都必须绑定本轮 `$WORK_ROOT`，并由同 UID、同 host/boot/PID namespace 的 Terminal B 执行。`fault_schedule` 不负责
启动或重启 owner，因此不能把会被杀的 owner 包进普通 fail-fast `record`。

Terminal A 先定义一个只接受 shell `128 + SIGKILL(9) = 137` 的专用记录函数。它只证明该次 owner 进程确实被
SIGKILL；必须与 Terminal B 的 exact pinned-owner schedule `run`/`verify` 同时通过，二者缺一不可：

```bash
record_expected_owner_sigkill() {
  label="$1"
  shift
  output="$EVIDENCE_ROOT/$label.txt"
  status="$EVIDENCE_ROOT/$label.exit"
  expected="$EVIDENCE_ROOT/$label.expected"
  test ! -e "$output"
  test ! -e "$status"
  test ! -e "$expected"
  set +e
  "$@" > "$output" 2>&1
  rc="$?"
  set -e
  printf '%s\n' "$rc" > "$status"
  if test "$rc" -ne 137; then
    printf 'expected owner SIGKILL exit 137, got %s\n' "$rc" >&2
    return 2
  fi
  printf '%s\n' 'expected_signal=SIGKILL signal=9 shell_exit=137' > "$expected"
}
test -f "$OWNER_FAULT_SCHEDULE"
record 40-owner-fault-validate \
  python -m orchestrator.fault_schedule validate \
  --schedule "$OWNER_FAULT_SCHEDULE"
```

Terminal B 随后先启动下面的 `run`（它会等待 selector）；Terminal A 再启动被杀 attempt：

```bash
# Terminal B
record 41-owner-fault-run \
  python -m orchestrator.fault_schedule run \
  --schedule "$OWNER_FAULT_SCHEDULE"

# Terminal A，与上面的 run 并发
record_expected_owner_sigkill 42-soak-owner-killed \
  python -m orchestrator.run \
  --system-root "$SYSTEM_ROOT" --work-root "$WORK_ROOT" \
  --max-cycles 200 --exit-after-research \
  --connector-profile "$CONNECTOR_PROFILE"

# 41 与 42 都完成后，Terminal B
record 43-owner-fault-verify \
  python -m orchestrator.fault_schedule verify \
  --schedule "$OWNER_FAULT_SCHEDULE"
```

然后对 payload schedule 做 `validate`，Terminal B 启动 `run` 等待；Terminal A 以同一 `$WORK_ROOT` 重启 owner。
若 selector 只能在 running receipt 出现后获知，按 `README.md` §7 先启动 owner、随即冻结新 schedule；不得复用
owner schedule ID。payload 被杀不会杀 owner，owner 必须继续到研究终态并以 0 返回：

```bash
# Terminal B
test -f "$PAYLOAD_FAULT_SCHEDULE"
record 44-payload-fault-validate \
  python -m orchestrator.fault_schedule validate \
  --schedule "$PAYLOAD_FAULT_SCHEDULE"
record 45-payload-fault-run \
  python -m orchestrator.fault_schedule run \
  --schedule "$PAYLOAD_FAULT_SCHEDULE"

# Terminal A，与 45 并发；这是 42 之后的明确 restart/续跑证据
record 46-soak-owner-restarted \
  python -m orchestrator.run \
  --system-root "$SYSTEM_ROOT" --work-root "$WORK_ROOT" \
  --max-cycles 200 --exit-after-research \
  --connector-profile "$CONNECTOR_PROFILE"

# 45 与 46 都完成后，Terminal B
record 47-payload-fault-verify \
  python -m orchestrator.fault_schedule verify \
  --schedule "$PAYLOAD_FAULT_SCHEDULE"

# 两个 schedule 都 verify、owner 已 0 退出后，operator 独立只读计数
record 48-soak-final-count \
  sqlite3 -readonly "$WORK_ROOT/research.sqlite" \
  "SELECT count(*) FROM cycle WHERE status='done' AND route IS NOT NULL;"
IFS= read -r SOAK_AFTER < "$EVIDENCE_ROOT/48-soak-final-count.txt"
case "$SOAK_AFTER" in
  ''|*[!0-9]*) echo "invalid soak final count" >&2; exit 2 ;;
esac
record 49-soak-count-gate test "$SOAK_AFTER" -ge 200
```

本协议要求 `$WORK_ROOT` 在 §0 尚不存在，所以 baseline 为 0；若经用户批准复用既有 root，必须改用 `README.md` §7
的 outside-work-root baseline 文件，按前后差值 `>=200`，不能直接套用上面的绝对计数。

运行前把每个故障的 selector、触发条件、期望终态和恢复动作写入用户的真实运行记录。已有
`orchestrator.fault_schedule` 只执行枚举的 `kill_owner` / `kill_execution_payload`，按 README 生成、validate、run、verify
并逐 schedule 保存完整目录；不允许用任意 shell、随机 signal 或复用 schedule ID 扩大其声明。

除 owner/payload SIGKILL 外，CP11.4c.3d.2 还要求目标设施实际覆盖：

- dedicated Docker daemon loss：由基础设施/operator 在预声明窗口执行；系统必须 fail-closed，不得发布未排空容器或
  不完整测量，daemon 恢复后按既有 receipt/reconcile 路径收敛；
- budget exhaustion：通过已认证 hard `set_budget` 或冻结低 ceiling 触发，`global_stop=budget_exhausted` 必须在下一次
  provider spawn 前耐久生效，未知 token 用量走 `cost_accounting_failed`，不得记成 0；
- resource failure：用预声明的 cgroup memory/CPU/PID 或 hard byte/inode quota 边界触发，目标只能得到明确失败回执，
  不得入 legal pool、metric evidence 或成功 checkpoint；解除故障后从耐久边界继续。

这三类事件由目标平台执行，不能让应用凭自报伪造 daemon/quota/cgroup 失效。每类都保存注入端原始记录、系统 receipt、
SQLite 前后只读查询、恢复后的新成功轮和无遗留容器/owner 的核验。未执行任一类就把该矩阵行写未验证，不得用
`kill_execution_payload` 代替 daemon/resource 语义。

最终证据至少机械证明真实 workload 出现过：成功 Codex/provider invocation、import discovery/materialization、
Docker train/eval、checkpoint、execution log、connector ACK，以及失败后恢复的新成功轮；纯 reasoning、fake/synthetic
或仅注入 worker 的 200 轮不满足 `§7.1/M6`。

## 5. 冷镜像、组合恢复、干净续跑与 evidence pack

owner 干净停止后执行最新 high-water 的 registered mirror/verify；恢复目标必须不存在，且位于 source lineage 外：

```bash
record 50-mirror-registered \
  python -m orchestrator.storage_ops --work-root "$WORK_ROOT" \
  mirror-registered-assets
record 51-verify-registered \
  python -m orchestrator.storage_ops --work-root "$WORK_ROOT" \
  verify-registered-assets
record 52-restore-registered \
  python -m orchestrator.storage_ops --work-root "$WORK_ROOT" \
  restore-with-registered-assets --target "$RESTORE_ROOT"
```

保存 mirror indexes、restore receipt、registered completion receipt、路径 lineage 与上述 raw 输出。随后按
`README.md` “Canonical evidence pack 与单轮续跑探针”执行；恢复 probe 必须使用新 target attestation，或诚实使用
development + `--no-outbound`，后者只能证明窄 SQLite/import-CAS 单轮恢复，不能升级为生产 connector/full restore。

在 source/target owner 都停止后 pack/verify，并保存 canonical manifest 目录。evidence-pack v1 的
`real_codex_resume_verified=false`、`qualification_receipts_verified=false`、`full_restore_verified=false` 是范围声明，
不能被 operator assertion 覆盖。完整 work-root/fileset、uploads、views Git、connector cursor、runner/guardian/
qualification authority 须另外进入独立故障域归档，并实际演练整根或站点丢失恢复。

## 6. T1/T2 qualification

逐字执行 `QUALIFICATION.md`，不要把普通 soak work-root 改造成 qualification work-root：

- T1：A 探索 → B exact claim lock → 一次 spent-before-spawn C LODO → root scientific audit → 一次 D DREAMER；
- T2：冻结 3 seeds × 15 folds，只在 claim/model/HPO 全锁后一次 final；没有 C authority；
- research UID 永远不能读取 sealed labels；root scorer 从 sealed truth 独立算分；失败/不确定 spend 不重跑。

机器只验冻结身份、mount exclusion、一次消费、schema、receipt 和 authority chain。命名 root evaluator 仍须检查
LODO/label isolation、metric/statistics、controls、novelty 和负结论诚实性；用户在运行前冻结的成功口径不能事后改写。
保存 contract、claim、C/guardian/promotion、root audit immutable ledger、final-consumed、score 与所有 referenced raw
evidence。T1/T2 任何一项没有真实数据和人工复核都保持未验收。

## 7. 同版本出口终验与 fixed_and_test 交接

目标运行结束、所有修复检查点落 commit 后，回到最终 clean commit 执行唯一一次 building 出口全量：

```bash
set -euo pipefail
git -C "$REPO_ROOT" diff --exit-code
git -C "$REPO_ROOT" diff --cached --exit-code
test -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)"
cd "$SYSTEM_ROOT"
record 70-pytest-full python -m pytest tests/ -q
record 71-compileall python -m compileall -q orchestrator tests
```

然后由 building 按 `$FIXED_AND_TEST_ROOT/templates/验收矩阵.md` 生成**该最终 commit**的矩阵实例，逐条列出：

- `§7.1/M0`–`M5`：复用原步级用例和本次全量原始输出；
- `§7.1/M6` 与 `§7.3/1`–`4`：真实 ≥200 轮、机制剧本、故障与 connector 证据；
- `§7.4/T1`、`§7.4/T2`：机器 receipt 与人工 scientific audit 分列，不混为一个绿色勾；
- `§7.5/1`–`7`：Codex、输入、只读数据、policy、GPU/quota、uploads、connector 前提；
- CP11.4c.3d.2 附加生产合同：production receipt、two-node canary、恢复/归档及诚实边界。

先把矩阵写到 `$EVIDENCE_ROOT/$ACCEPTANCE_ID-验收矩阵.md`。每个结论必须指向本轮证据；不能引用另一 commit
或另一 `RUN_ID` 的绿色结果。`fixed_and_test` 只接收已经逐文件检查、可向验收者披露的**原始未加工文件**。token、auth、
sealed labels、私有数据正文和基础设施密钥永不复制到该目录；它们的原件保留在 operator-only 归档。若某判据只能靠受限
原件证明，矩阵须写明 operator 归档定位符、内容 SHA-256、授权审阅方式和当前审阅状态；在验收者尚未安全审阅前不能写通过，
更不能用脱敏摘要冒充原始证据。

由 operator 审阅后创建 `$EVIDENCE_ROOT/shareable-files.txt`，每行一个相对 `$EVIDENCE_ROOT` 的普通文件；不得写绝对路径、
`..` 或目录，并把该清单自身也列入。随后先封存 operator 全量清单，再建立本轮独占交接目录：

```bash
set -euo pipefail
export SHARE_LIST="$EVIDENCE_ROOT/shareable-files.txt"
export OPERATOR_MANIFEST="$EVIDENCE_ROOT/99-OPERATOR-SHA256SUMS"
export MATRIX_SOURCE="$EVIDENCE_ROOT/$ACCEPTANCE_ID-验收矩阵.md"
test -s "$SHARE_LIST"
test -f "$MATRIX_SOURCE"
test "$(realpath -e -- "$EVIDENCE_ROOT")" = "$EVIDENCE_ROOT"
test "$(stat -c '%u:%a' -- "$EVIDENCE_ROOT")" = "$(id -u):700"
SYMLINK_ENTRY="$(find "$EVIDENCE_ROOT" -type l -print -quit)"
MULTILINK_ENTRY="$(find "$EVIDENCE_ROOT" -type f -links +1 -print -quit)"
SPECIAL_ENTRY="$(find "$EVIDENCE_ROOT" ! -type d ! -type f -print -quit)"
test -z "$SYMLINK_ENTRY"
test -z "$MULTILINK_ENTRY"
test -z "$SPECIAL_ENTRY"
test "$(grep -Fxc -- 'shareable-files.txt' "$SHARE_LIST")" -eq 1
test ! -e "$OPERATOR_MANIFEST"
test ! -e "$EVIDENCE_ROOT/.99-OPERATOR-SHA256SUMS.tmp"
(
  cd "$EVIDENCE_ROOT"
  find . -type f \
    ! -path './99-OPERATOR-SHA256SUMS' \
    ! -path './.99-OPERATOR-SHA256SUMS.tmp' -print0 |
    LC_ALL=C sort -z | xargs -0 -r sha256sum
) > "$EVIDENCE_ROOT/.99-OPERATOR-SHA256SUMS.tmp"
mv -- "$EVIDENCE_ROOT/.99-OPERATOR-SHA256SUMS.tmp" "$OPERATOR_MANIFEST"
(cd "$EVIDENCE_ROOT" && sha256sum -c "$(basename "$OPERATOR_MANIFEST")")
chmod -R a-w -- "$EVIDENCE_ROOT"

test ! -e "$ACCEPTANCE_EVIDENCE_ROOT"
test ! -e "$ACCEPTANCE_MATRIX"
mkdir -m 0750 "$ACCEPTANCE_EVIDENCE_ROOT"
while IFS= read -r rel || test -n "$rel"; do
  case "$rel" in
    ''|'#'*) continue ;;
    /*|.|./*|*/.|*/./*|*//*|..|../*|*/..|*/../*)
      echo "unsafe share path: $rel" >&2; exit 2 ;;
  esac
  rest="$rel"
  src="$EVIDENCE_ROOT"
  while :; do
    case "$rest" in
      */*) component="${rest%%/*}"; rest="${rest#*/}" ;;
      *) component="$rest"; rest='' ;;
    esac
    src="$src/$component"
    test ! -L "$src"
    if test -n "$rest"; then
      test -d "$src"
    else
      break
    fi
  done
  dst="$ACCEPTANCE_EVIDENCE_ROOT/$rel"
  test -f "$src"
  test "$(stat -c '%h' -- "$src")" -eq 1
  test ! -e "$dst"
  mkdir -p -- "$(dirname "$dst")"
  install -m 0440 -- "$src" "$dst"
  cmp -s -- "$src" "$dst"
done < "$SHARE_LIST"
test -f "$ACCEPTANCE_EVIDENCE_ROOT/shareable-files.txt"
(cd "$EVIDENCE_ROOT" && sha256sum -c "$(basename "$OPERATOR_MANIFEST")")
install -m 0440 -- "$MATRIX_SOURCE" "$ACCEPTANCE_MATRIX"

test ! -e "$ACCEPTANCE_EVIDENCE_ROOT/SHA256SUMS"
test ! -e "$ACCEPTANCE_EVIDENCE_ROOT/.SHA256SUMS.tmp"
(
  cd "$ACCEPTANCE_EVIDENCE_ROOT"
  find . -type f ! -path './SHA256SUMS' ! -path './.SHA256SUMS.tmp' -print0 |
    LC_ALL=C sort -z | xargs -0 -r sha256sum
) > "$ACCEPTANCE_EVIDENCE_ROOT/.SHA256SUMS.tmp"
mv -- "$ACCEPTANCE_EVIDENCE_ROOT/.SHA256SUMS.tmp" \
  "$ACCEPTANCE_EVIDENCE_ROOT/SHA256SUMS"
(cd "$ACCEPTANCE_EVIDENCE_ROOT" && sha256sum -c SHA256SUMS)
export HANDOFF_DIGEST="$FIXED_AND_TEST_ROOT/acceptance/$ACCEPTANCE_ID-handoff.sha256"
test ! -e "$HANDOFF_DIGEST"
(
  cd "$FIXED_AND_TEST_ROOT/acceptance"
  sha256sum "$(basename "$ACCEPTANCE_MATRIX")" \
    "evidence/$ACCEPTANCE_ID/SHA256SUMS"
) > "$HANDOFF_DIGEST"
```

`$ACCEPTANCE_ID` 同时绑定日期和随机 run ID，所有目的文件都先做 no-clobber 检查，历史证据不删。矩阵链接使用
`acceptance/evidence/$ACCEPTANCE_ID/...`，并引用 `SHA256SUMS`；operator-only manifest 留在独立归档，不复制到共享树。
任何机器项不过就退回 building 修复，并在最终版本重跑受影响项、生成新的 `ACCEPTANCE_ID`。机器项全过后，
fixed_and_test 只审证据和写放行报告；用户再依据预先填写的真实运行记录给出“符合/部分符合/不符合”及最终原话。
没有用户签署就不能勾 CP11.4c.3/CP11.4c。

## 8. 当前环境不是通过样例

2026-07-13 在当前会话做过一次正式零轮 development probe 和 single-node GPFS canary：8×A100 与 GPFS 可见，local
五阶段 canary 通过；但 service 为 root、Docker socket 是代理 symlink、daemon `CgroupDriver=none`、memory/CPU/PID
limits 均不可用、无 NVIDIA runtime、无权威 fileset hard quota、Docker 余量不足 4 GiB，且没有第二节点或生产
connector。deployment receipt 正确为 `production_ready=false`，local receipt 正确为
`two_node_verified=false` / `infrastructure_fence_verified=false`。这些是拒绝理由和设施交接输入，不是验收证据。
