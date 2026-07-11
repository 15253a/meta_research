# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b.2c reviewed adapter generation
- 检查点状态：CP11.4c.2b.2b 功能已提交 `1e5761156a6075c48b9822f7e4ffe00e6e2a488b`；唯一全量
  `1418 passed, 1 skipped`，build_log 0068 已记账；CP11.4c.2 父项仍未达成

## 刚完成什么

CP11.4c.2b.2b 已把 adapter v3 的唯一 canonical Python wheel lock 转为 exact/restorable project image：公网 URL/
SHA-256/bytes 与 wheel 结构/tag 受界，pinned sandbox 离线 pip install，generated-only Dockerfile，base/result image、
context metadata、compiler/runtime/pip-check 和 Docker engine receipt 交叉核。legacy builder 不伪称重建同 ID；首个 exact
image 以有界 hashed archive 保存，丢失后恢复同一 ID。stale/owner-loss 会排空 guardian/container 并清理未发布
closure image。ImportWorker 和后续 compiler/AttackStages 只能从 verified receipt 继承 exact `env_hash`。

## 验证 / Review

- 相关回归 `346 passed, 1 skipped`；去重核验/restore 最终真 Docker 定向 `1 passed`；真实公网
  `idna 3.15` exact wheel canary `1 passed`；compile/schema/diff check 通过。
- 检查点末唯一全量：`1418 passed, 1 skipped in 1036.44s`；跳过项是已单独通过的默认关闭公网 canary。
- 外审第 1 轮 codexro 401；第 2 轮完整内联 diff 后服务连接 5/5 重试耗尽，两轮均无 verdict。依两轮
  上限不再重试；本地审计成立项已全修并在提交前验证。
- 尚未 push。

## 当前关键边界

- 只有仓库显式 adapter v3 + canonical wheel lock 能进 dependency image；v2 仍只用 bootstrap pinned image。缺 adapter 的
  普通仓库仍会被拒，2b.2c 才实现有界受审生成，不能声称任意 SOTA repo 自动复现。
- dependency image object 有数量/单对象边界，但生产级 VEPFS byte+inode hard quota、Docker store quota/回收属
  CP11.4c.2b.3；当前不得冒充部署验收。
- rootless daemon 仍是 `rlimit-fallback`；service account/VM、Docker socket、cgroup/device/GPU 与跨节点目标环境未验收。
- 两节点 exact archive/owner/lease/fd 行为与含真实 Codex/import/训练/故障注入的 100+ 轮 soak 属
  CP11.4c.3；CP11.3c 的控制面 120 轮不能替代。
- 不得 push；开发期继续只跑相关验证，下一检查点冻结后再做一次全量。

## 下一步动作

1. CP11.4c.2b.2c 冻结“缺 adapter”的有界输入/输出、模型/prompt、独立评审和可恢复 receipt 合同。
2. 只从已冻结 repository tree/ledger 生成 sidecar；schema + deterministic cross-check + adversarial sandbox smoke 全过才允许
   进 snapshot spec，不改写 repository tree，不将模型输出直接当 authority。
3. CP11.4c.2b.3 落生产部署信任/quota 启动合同；CP11.4c.3 准备两节点和真实 100+ 轮故障注入证据包。
