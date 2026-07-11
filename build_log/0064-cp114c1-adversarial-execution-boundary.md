# 0064 · CP11.4c.1 pinned 敌对执行边界

- date: 2026-07-11
- commit: `a05584de1e63db86d786f14d6b1772758f78a3b9` — feat: close CP11.4c.1 adversarial execution boundary
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.1（属：步⑪ CP11.4c 敌对隔离与最终长程验收；CP11.4c.2/3 继续）

## 决策

本检查点只闭合“候选中的不可信 bundle/import 命令如何执行、回收和晋升产物”，不把 bootstrap runtime、当前节点的
rootless Docker 或控制面 120 轮回归写成完整生产/上百轮验收。

- 默认 attack 装配在打开 SQLite/connector 前预检 exact Docker binary/socket、daemon capability、resource mode、
  exact image digest+image ID 与 seccomp BPF；失败即拒绝启动，不隐式 pull、不回退 host execution。
- manifest 与 import adapter 的已验证 fd/tree capability 先复制为私有内容快照，只读 bind 到 `/mr/input`；容器只得到
  一个隔离 quarantine 输出 mount，另有有界易失 `/tmp`/`/dev/shm`。work-root、SQLite、guardian/provider receipt、
  Codex/host env 和凭据不进入容器。
- Docker create 固定 network none、readonly rootfs、non-root UID、cap-drop ALL、no-new-privileges、private PID/IPC、
  resource/log/tmpfs/shm 与 exact Cmd；create 后 inspect 反核 image、label、安全选项、env、mount 闭包与读写模式。
  rootless daemon 对 overlay/GPFS 的 bindfs rewrite 由 `/proc/self/mountinfo` 最深挂载点精确派生，不再做任意 suffix 放行。
- 本平台会替换 Docker 请求/default seccomp，故 daemon profile 只算 additive。权威 syscall 边界使用
  [Moby `seccomp/v0.2.1` default profile](https://github.com/moby/profiles/blob/seccomp/v0.2.1/seccomp/default.json)
  生成、vendored 且 hash-pinned 的 Linux 5.4/amd64 BPF；trusted pinned-image Python launcher 先设置 hard
  `RLIMIT_AS/NPROC/NOFILE/FSIZE/CORE`，再加载 BPF，最后才解析 payload env/exec，避免 `LD_PRELOAD` 等影响控制阶段。
- guardian prepared/running/terminal receipt 携 exact random container name、private label、runner spec 与 engine identity；
  terminal 只有在本机 descendant tree 与 daemon container 均 drained 后发布。取消落在 `docker create` 注册窗时，guardian
  保持 instance fence，直到 name+label 可观察或 trusted runner 已退出，再执行 exact force-remove/absence proof。
- stdout/stderr 由 Docker `json-file` 两段硬轮转；发现轮转即失败，不能把截断 transcript 当测量证据。容器退出后，
  quarantine 必须通过 no symlink、regular single-link、path/file-count/total-bytes/hash 闭包才幂等复制进 staging；
  不安全输出先耐久 reject，再由同一 Gate 事务结算 exact run/evaluation_attempt/evaluation/target，禁止半终态楔死。
- 中央 session index 先于 staging metadata fsync。prepare→guardian、prepared→Popen、non-exit、return→publish 任一窗口
  都由 exact DB owner context、guardian receipt、container identity/absence 与 promotion receipt 收敛；成功/失败晋升可重放，
  不凭“看不到 partial”猜测执行未发生。
- manifest `env_hash` 改为 policy sandbox 全配置的 canonical SHA-256，bundle 锚区机械给出、worker 逐字回引；镜像、
  seccomp、resource mode 或 mount policy 改动都会改变环境身份，不能沿用历史测量。

## 改动文件

- `meta-research/orchestrator/execution_sandbox.py` — pinned Docker runner、input snapshot、launcher rlimit+BPF、create/inspect、
  quarantine ledger/promotion、中央 session index 与崩溃恢复。
- `meta-research/orchestrator/process_supervisor.py` — external-container cleanup capability、engine identity、注册窗等待、
  exact name+label drain 与 terminal receipt 闭包。
- `meta-research/orchestrator/harness.py` — minimal host env、sandbox lifecycle 接入、exit/non-exit output finalize/recovery。
- `meta-research/orchestrator/{manifest,attack_stages,import_worker,run}.py` — 生产 manifest/import 全接强沙箱，startup preflight/
  recovery，unsafe output exact-owner 原子结算。
- `meta-research/orchestrator/{compiler_sqlite,gate_exec,import_fetcher}.py` — pinned env anchor、sandbox failure Gate 与窄
  materialization 边界。
- `meta-research/policies/policy.yaml`、`schemas/*.json`、`prompts/skills/bundle/SKILL.md` — exact runtime/image/seccomp/resource
  contract 与 manifest env identity。
- `meta-research/policies/seccomp/*`、`scripts/generate_seccomp_bpf.py` — upstream provenance、canonical profile、固定 BPF 与
  libseccomp 2.5.3 再生成脚本。
- `meta-research/tests/test_execution_sandbox.py` 及 attack/import/manifest/run/M4 回归 — 真 daemon isolation、timeout cleanup、
  output escape、owner publish gap、payload env、BPF syscall、bindfs mapping 与 DB settlement。
- `meta-research/README.md` — 部署步骤、观测面与 cgroup/GPU/quota/host trust/generic repository/100+ 轮诚实边界。

## Review（codex-chatgpt，第二轮上限）

- 第 1 轮：`codexro-review` 在产出 verdict 前因账号 401（revoked/invalid token）失败；无反馈、未记批准。
- 第 2 轮（fallback `codex-chatgpt`，完整 staged diff）：结论 `REQUEST_CHANGES`。
  - BLOCKER（已修）：prepare 完成到 guardian prepared receipt 之间死亡会遗留确定性 session，后续永久拒绝重跑；non-exit
    terminal receipt 也缺启动清理。新增 exact-owner `recover_unstarted_session`、中央 session index、startup terminal
    recovery；索引先于 metadata，连 index-only kill window 也可证明未启动后清理。
  - BLOCKER（已修）：payload env 曾随 container launcher 启动，`LD_PRELOAD` 等可在 rlimit 前影响 trusted Python。
    daemon Config.Env 只保留固定 control env；payload env 编码进 trusted argv，rlimit+BPF 完成后才清空/注入。
  - BLOCKER（已修）：只拒绝 readonly mount 位于 work-root 内部，仍可挂载 `/`、`/tmp` 等祖先而暴露 work-root。现在拒绝
    祖先、同目录和子树，并加回归。
  - SHOULD（以更强实现解决）：要求明确 Docker seccomp profile。实测平台会用自有弱 ALLOW profile 替换请求，不能把
    inspect 声明当权威；改为 exact Cmd 中的 hash-pinned launcher BPF，并用 `unshare(0)` 证明它阻断平台 additive profile
    放行的 syscall。daemon seccomp 仍须存在且不得 unconfined，但只作叠加层。
- 已到两轮上限，不发第 3 轮；所有三个 BLOCKER 与成立 SHOULD 在功能提交前修复并做相关回归。

## 验证

- 开发期相关验证：sandbox 最终 **18 passed**；schemas/manifest/run **197 passed**；attack/import/process-supervisor/
  observation **127 passed**；M4 pinned env reuse 与完整 production attack flow 修复后各 **1 passed**。
- `py_compile`、`git diff --check`、canonical seccomp JSON、decoded BPF size/hash 与 policy pin 均通过；BPF decoded 2632 bytes，
  SHA-256 `4fb43ea7bb76d9462eb73270fb52e473fb4423bfe09040bfa16c255a7eb133f2`。
- 首次本地 `pytest -q` 在约 53% 时把 20GB 根盘打满，结果 `1032 passed, 3 failed, 318 errors`，不作为检查点验收。
  其中 M4 仍写死 `toy-env` 是真实契约回归并修复；其余 last-failed 用 VEPFS 临时目录复核，得到 `317 passed, 1 failed`
  （唯一失败暴露 GPFS bindfs inspect 映射），精确修复后 sandbox 18 与完整 attack flow 1 均通过。
- 功能冻结后的最终有效全量将 `TMPDIR`/`--basetemp` 固定到 VEPFS：
  `pytest -q --basetemp=<vepfs>/final-full` → **`1354 passed in 1026.33s (0:17:06)`**，无失败。
- CP11.4c.1 验收结论：在当前单节点/可信 Docker daemon 前提下，不可信 bundle/import payload 已不能看到 host work-root/
  凭据/网络，产物只有 drain 后经闭包验证才能发布；owner-kill/timeout/发布缝隙可由耐久 authority 收敛。该结论不包含
  host root/orchestrator UID 对手、aggregate cgroup/GPU、通用仓库物化、跨节点或真实 100+ 轮运营验收。

## 遗留 / 回退

- CP11.4c.2：默认 GitHub discovery 目前只冻结 repo/commit/license；补 exact archive/tree、submodule/LFS、依赖 lock、
  adapter 生成/评审与镜像供应链。部署 contract 还须覆盖 service account/VM、Docker socket、cgroup v2/device/GPU 和
  hard byte+inode quota；当前 `rlimit-fallback` 不能冒充 aggregate 资源隔离。
- CP11.4c.3：目标 VEPFS 两节点 owner/lease/fd 实机竞态，以及 100+ 轮真实 Codex/import/训练 + owner-kill/daemon-loss/
  预算/资源失败注入 soak。CP11.3c 的 120 轮控制面回归不能替代它。
- 回退前停止 orchestrator，并确认没有 `mr-*` container、guardian 或 running DB owner；保留中央 execution/session/
  promotion/rejection receipts 后执行 `git revert a05584de1e63db86d786f14d6b1772758f78a3b9`。本提交无 DDL migration，
  但旧代码不理解 `docker-container-v1` receipt 与 `.sandbox-*` authority，不得在 sandbox invocation 在途时热回退。
