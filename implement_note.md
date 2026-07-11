# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2 通用 pinned repository 物化与部署信任边界
- 检查点状态：CP11.4c.1 功能已提交 `a05584d`；最终有效全量 1354 passed；build_log 0064 已记账；
  CP11.4c 父项仍未达成

## 刚完成什么

CP11.4c.1 已把生产 bundle/import 的不可信命令从 host lifecycle guardian 后面移入 exact-pinned Docker：只读
verified snapshot/input mount、单一 quarantine writable mount、network none、readonly rootfs、non-root、cap-drop ALL、
no-new-privileges、private PID/IPC 与 daemon 资源声明均在 create 后 inspect 反核。trusted pinned-image launcher 先设置
hard `RLIMIT_AS/NPROC/NOFILE/FSIZE/CORE`，再加载 vendored Moby profile 生成且 SHA-256 固定的 amd64 seccomp BPF，最后
才解析 payload env 并 exec；host env/凭据不会进入 runner spec/container。

guardian receipt 现在携 exact random container name+private label authority，只有 local descendant tree 与 daemon container
都证明 drained 才发布 terminal。输出在 drain 后经过 no-symlink/regular-single-link/file-count/bytes/hash 闭包再幂等晋升；
不安全输出原子结算 exact run/attempt/target。prepare→guardian、非 exit 与 return→publish 崩溃窗由中央 session index、
owner receipt 和 absence proof 恢复；索引先于 metadata fsync，index-only kill window 也可启动清理。

## 验证 / Review

- 相关验证：sandbox 最终 `18 passed`；schema/manifest/run `197 passed`；attack/import/supervisor/observation
  `127 passed`；M4 env reuse 与完整 attack flow 修复后各 `1 passed`；compile、canonical seccomp JSON/BPF hash、
  `git diff --check` 均通过。
- 首次本地全量在根盘达到 100% 后失效（`1032 passed, 3 failed, 318 errors`）；其中旧 M4 用例的 `toy-env` 是真实
  契约回归并已修，其余用 VEPFS last-failed 子集复核为环境级联。修复 GPFS rootless bindfs 精确映射后，最终有效
  全量固定 VEPFS `TMPDIR/basetemp`：`1354 passed in 1026.33s`。
- 外审第 1 轮独立账号 401，无 verdict；第 2 轮 `REQUEST_CHANGES`。prepare→guardian/非 exit 恢复、payload env
  pre-rlimit 注入、readonly mount 覆盖 work-root 三个 BLOCKER 均已修。daemon 显式 seccomp 在本平台会被替换，故以
  更强的 pinned launcher BPF 闭合 SHOULD；两轮上限后未发第 3 轮。
- 功能提交：`a05584de1e63db86d786f14d6b1772758f78a3b9`；尚未 push。

## 当前关键边界

- 当前 rootless daemon 明示 `CgroupDriver=none`，receipt 诚实写 `rlimit-fallback`；hard rlimit 是 per-process，不能
  冒充 aggregate memory/CPU cgroup。输出 bytes/files 是晋升前后验闸，host/VEPFS 仍须 hard byte+inode quota。
- 默认镜像是 CPU/Python bootstrap，没有 GPU/device/项目锁定依赖；同 host 的 root/orchestrator UID 与 Docker socket
  仍在信任域内。防 host 对手需要独立 service account/VM/远端 attestation，不是 0600 自签 receipt 能解决。
- 默认 GitHub discovery snapshot 只有 repo/commit/license；尚无任意仓库 exact archive/tree/LFS/依赖闭包与 adapter
  生成路径。当前只支持候选中已冻结、尺寸有界的兼容 materialization。
- 仍未完成两节点 VEPFS owner/lease/fd 实机竞态，也未完成含真实 Codex/import/训练及故障注入的 100+ 轮 soak；
  CP11.3c 的 120 轮仍只证明控制面/状态投影。
- 不得 push；开发期继续只跑相关验证，检查点冻结后再做最终全量。

## 下一步动作

1. CP11.4c.2：设计 discovery candidate → exact commit archive/tree（含 submodule/LFS 明示策略）→ content-addressed
   materialization → sandbox adapter review 的生产协议，禁止隐式网络/动态依赖。
2. 把镜像 SBOM/依赖 lock、service account/VM、Docker socket、cgroup v2/device/GPU 与 VEPFS hard quota 写成可启动
   fail-closed 的 deployment contract；当前节点不具备的能力只列验收，不伪造通过。
3. CP11.4c.3：准备两节点 owner canary 与 100+ 轮真实 workload/owner-kill/daemon-loss/资源失败 soak 证据包。
