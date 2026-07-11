# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b adapter/LFS/dependency/deployment contract
- 检查点状态：CP11.4c.2a 功能已提交 `50ba41f4dadd45183bf201edda0f800c972dac00`；最终全量
  `1381 passed`；build_log 0065 已记账；CP11.4c.2 父项仍未达成

## 刚完成什么

CP11.4c.2a 已把默认 GitHub discovery 的 40-hex commit 接到真实 production materializer：non-recursive Git tree
对象重算 SHA-1，commit archive 逐文件核 path/type/size/mode/blob SHA，根仓库/固定子模块的 license evidence 与
同 commit 文件 ledger 对账；symlink、LFS pointer、歧义 `.gitmodules` 与未验证依赖 fail-closed。verified tree/spec/
transport/receipt 以 SHA-256 内容寻址发布，本地 cache 损坏不会永久归罪候选，gzip transport 漂移不改变源身份。

仓库自带 adapter v2 现在可经 file-backed clone → pinned Docker smoke → 有界独立代码评审 → 延迟稳定 protocol/metric
注册 → named factory eval → result review → pool。compute/readout 与 metric family/version 不可漂移，ID 保持 JSON safe；
大仓库 judge prompt 有 path/content 总预算并优先 adapter/入口，未展示 bytes 不冒充已语义评审。旧 embedded v1 可恢复。

## 验证 / Review

- 相关验证：核心契约 `170 passed`；sandbox/gate/run/manifest 集成 `156 passed`；外审修复 `41 passed`；compile 与
  `git diff --check` 通过。
- 按用户要求，冻结前只跑相关验证；冻结后唯一一次全量固定 VEPFS `TMPDIR/basetemp`：
  `1381 passed in 1024.62s (0:17:04)`。
- 外审第 1 轮独立账号 401、无 verdict；第 2 轮 `REQUEST_CHANGES`。并发 object publication 与 worker 空 metric
  语义两个成立 SHOULD 已修；metric drift BLOCKER 原 Gate 已覆盖，仍加 worker 前置核；candidate 字段和
  `attack=False` 为大 diff 漏读误报，新增生产 bridge 回归/构造 fail-fast。两轮上限后未发第 3 轮。
- 尚未 push。

## 当前关键边界

- 当前只消费仓库自带 `.meta-research/import-adapter.json` v2；普通仓库缺 adapter 会被拒，不能声称任意 SOTA repo
  自动复现。materializer 目前 1800+ 行，CP11.4c.2b 扩展前应按 transport/tree/archive/adapter/cache 边界拆分。
- LFS OID 下载、项目 lock 构建/验证、项目专用 image 尚未实现；当前只允许 pinned bootstrap image 自带依赖。
- rootless daemon 仍是 `rlimit-fallback`；同 host root/orchestrator UID 与 Docker socket 属信任域。service account/VM、
  cgroup/device/GPU、VEPFS hard byte+inode quota 必须成为启动 fail-closed 部署合同。
- 两节点 VEPFS owner/lease/fd 实机竞态及含真实 Codex/import/训练/故障注入的 100+ 轮 soak 属 CP11.4c.3；
  CP11.3c 的控制面 120 轮不能替代。
- 不得 push；开发期继续只跑相关验证，下一个检查点冻结后再做一次全量。

## 下一步动作

1. CP11.4c.2b 先拆分 `repository_materializer.py` 的 transport/tree/archive/adapter/cache 组件，保持 0065 回归不变。
2. 实现缺 adapter 的受审生成、Git LFS batch OID+size 核验、项目 lock→专用 image 的可复现构建/验收。
3. 把 service account/VM、Docker socket、cgroup v2/device/GPU 与 VEPFS byte+inode quota 写成可启动 fail-closed
   deployment contract；当前节点缺能力时只给明确阻断证据，不伪造通过。
4. CP11.4c.3 准备两节点 owner canary 与 100+ 轮真实 workload/owner-kill/daemon-loss/资源失败 soak 证据包。
