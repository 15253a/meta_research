# 0070 · CP11.4c.2b.3a honest deployment preflight

- date: 2026-07-11
- commit: `16d5270cf034a55300d9f2d1d6df3f7fde116572` — feat: add CP11.4c.2b.3a deployment preflight
- branch: `fix/architecture-hardening-20260709`
- 检查点 / 步: CP11.4c.2b.3a（属：步⑪ CP11.4c.2b.3；GPU bridge 2b.3b 继续）

## 决策

新增一套无状态 `probe → pure evaluate → owner-bound canonical receipt`，不创建 VM/quota、不新增 DB 表或部署状态机。
默认 development 可以真跑现有 CPU 链，但 receipt 与入口始终明确 `production_ready=false`；production 任一信任事实
缺失即在 recovery mutation、SQLite、connector、provider 和 Runner 之前失败。

- production 强制 instance lease、dedicated VM（实测 hypervisor 且不得仍在 container）、非 root service、0700
  CODEX_HOME 与 0600 单链接 auth、service-owned 0600 direct rootless Docker socket。
- Docker daemon identity/security/cgroup/limits 与 policy pin 交叉核；backing-store live free 必须覆盖由 sandbox、repository
  与 dependency-image policy 机械推导且 attestation 不可下调的 bytes/inodes reserve。
- exact work-root path/mount/source/fstype 绑定防跨目录重放。GPFS quota 不由 `df/statvfs` 冒充：只接受部署 root 在
  300 秒内签发的 `gpfs-fileset-v1` hard/used byte+inode snapshot，并把完整 attestation 嵌入 receipt 供重放。
- 当前 sandbox 禁 device request，因此 `resources.gpus>0` 即使主机 inventory 足够也拒绝 production；设备桥独立留给
  CP11.4c.2b.3b。

## 改动文件

- `deployment_preflight.py` — bounded live collector、attestation loader、纯判据与耐久 owner receipt。
- `run.py` — sandbox 只读 preflight 后先做 deployment check，通过才恢复旧 execution/session 并继续装配。
- `deployment_attestation.schema.json`、`policy.schema.json`、`policy.yaml` — 冻结双模式与短时效部署合同。
- `README.md`、`ROADMAP.md` — 操作方法、诚实边界及 GPU 子检查点。
- deployment/schema/run tests 与 fixtures — 覆盖 dev receipt、production 正例和 lease/root/socket/cgroup/quota/storage/
  virtualization/CODEX_HOME/GPU/tamper/recovery-order 负例。

## Review

- 内部两路只读审查发现并修复：unleased production 旁路、rootful/shared socket、任意 quota provider、storage reserve
  可下调、错误身份先 recovery、container 冒充 VM、Codex auth 权限、socket ancestor symlink、跨 work-root 重放、
  quota total/free 语义、freshness 判定时点及 receipt 缺完整 attestation。最终两路均 APPROVE，无剩余 blocker。
- 外审第 1 轮 `codexro-review`：账号 token/refresh token invalidated，HTTP 401，无 verdict。
- 外审第 2 轮 `codex-chatgpt`：完整 staged diff 内联后只确认开始只读审查，数分钟无结论；按等待边界终止，
  无 output file/verdict。已到两轮上限，不发第 3 轮。

## 验证

- deployment/schema/run：**`167 passed in 63.62s`**。
- execution sandbox/instance lease/process supervisor：**`81 passed in 33.88s`**；py_compile、schema 与 diff check 通过。
- 真实只读 collector canary：`mode=development`、`production_ready=false`；实测 root、KVM 内 Docker container、
  `rlimit-fallback`、8 张 host-visible GPU 但 sandbox 不可达、Docker root 仅 `42,536,960` bytes free，失败项完整入 receipt。
- 检查点末唯一全量：**`1466 passed, 1 skipped, 1 failed in 1065.14s`**。唯一失败仍为既有
  `test_dependency_image.py::test_exact_image_build_reuse_and_archive_restore`；`save.log` 精确为
  `write /ebs/docker/tmp/docker-export-.../layer.tar: no space left on device`，与本检查点 diff 无关。无遗留 labeled
  dependency image；依“只做一次全量”指示未重跑粉饰。

## 遗留 / 回退

- 当前机器只能 development；要 production 需独立非 root service/dedicated VM、rootless private daemon+cgroup、
  GPFS quota probe/attestation 与足够 Docker store。GPFS 数值信任 dedicated-VM root，不是远端 attestation。
- CP11.4c.2b.3b 补 exact GPU UUID DeviceRequests、create inspect、容器 inventory 与 runtime identity；不扩成调度器。
- 回退：`git revert 16d5270cf034a55300d9f2d1d6df3f7fde116572`。无 DDL；回退后 development receipt/production
  preflight 消失，既有 research DB 不变。
