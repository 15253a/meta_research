# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b.3b fixed GPU device bridge
- 检查点状态：CP11.4c.2b.3a 功能提交 `16d5270` 已完成，0070 记账中；CP11.4c.2b.3/2/4c 未完成

## 刚完成什么

一套无状态 development/production deployment preflight 已接到 DB/provider/recovery mutation 之前，并写 owner-bound
canonical receipt。development 永远 `production_ready=false`；production 强制 owner lease、dedicated VM、非 root service、
私有 Codex auth、service-owned rootless Docker socket、cgroup/limits、policy-derived storage reserve、exact GPFS work-root
和 300 秒内 root-signed fileset quota snapshot。没有新增部署器、DB 表或状态机。

## 验证 / Review

- 相关：deployment/schema/run `167 passed`；sandbox/lease/supervisor `81 passed`；真实 collector canary 正确判当前节点
  development（Docker free 42,536,960 bytes）。内部双 APPROVE。
- 外审两轮均无 verdict：codexro 401；inline fallback 数分钟无结论后终止；不发第 3 轮。
- 唯一全量：`1466 passed, 1 skipped, 1 failed in 1065.14s`；唯一失败是既有 dependency image `docker save`
  写 `/ebs/docker/tmp` 时 no-space，与本 diff 无关；未二次全量。
- 功能提交 `16d5270`；文档提交待本次收口；未 push。

## 当前可用边界

- 现有 CPU/development 研究链可跑，但本机 root、container、rlimit fallback、无 GPFS quota 证明、Docker store 已满，
  不得称 production。
- `resources.gpus=4` 只是 host inventory 事实；sandbox 仍拒 Devices/DeviceRequests，所以 GPU production 明确失败。
- 缺 adapter 生成代码闭环已具备，但隔离 Codex token 仍需用户/运维重新认证后才能跑真实 canary。
- 两节点 VEPFS 与真实 100+ 轮 soak 仍属 CP11.4c.3。

## 下一步动作

1. CP11.4c.2b.3b 只补固定 exact GPU UUID bridge：Docker DeviceRequests、create 后 inspect、容器内 inventory。
2. GPU 型号/driver/capability 纳入 runtime identity；继续复用 guardian/sandbox receipt，不造 GPU 调度状态机。
3. 只跑 GPU/sandbox/deployment/run 相关验证；检查点冻结后再做一次全量与成对 git 提交。
4. 环境侧并行前置：扩 Docker backing store、准备 cgroup-capable rootless daemon，并重新认证隔离 Codex 账号。
