# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3 最终实机验收
- 检查点状态：空闲；CP11.4c.2b.3b 功能提交 `563a496` 已完成，下一检查点等待目标部署条件

## 刚完成什么

fixed GPU device bridge 已以最小边界落地：attestation v2 的 service exact GPU allocation 进入 Docker
DeviceRequest，create inspect、容器内 inventory、guardian receipt 与 candidate hash 相互校验。GPU capability
projection 进入可复用 workload/runtime identity，exact UUID 只保留在单次 invocation/runtime evidence。缺少
GPU 能力时 fail-closed，目标收敛为 `env_invalid`，不会恢复后楔死。未新增调度器、MIG、动态租约或 DB 状态。

## 当前可用边界

- CPU/development 研究链可用；符合 exact attestation、cgroup 与 NVIDIA runtime 的目标节点已有固定 GPU 设备合同。
- 当前节点没有 NVIDIA container runtime，只完成真实 negative canary，不能声称 production GPU 已验收。
- CP11.3c 的 120 轮是控制面/状态稳定性回归，不是 CP11.4c.3 的真实 Codex/import/训练/故障注入 soak。
- 检查点相关验证通过；唯一全量为 1494 passed/1 skipped/1 failed，失败为 Docker archive 写满当前
  backing store，未重跑粉饰。当前根盘仅约 265 MiB 可用。

## 下一步动作

1. 准备目标环境：扩容 Docker backing store，cgroup-capable private/rootless daemon + NVIDIA runtime、
   GPFS hard byte+inode quota attestation、隔离 Codex auth 与两节点 VEPFS。
2. 只实施 CP11.4c.3：跑真实 100+ 轮 Codex/import/训练 soak，注入 owner-kill、daemon-loss、预算和资源失败。
3. 发布可重放证据包后，才将 CP11.4c 与生产验收标记完成。
