# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b.2 adapter/LFS/dependency image closure
- 检查点状态：CP11.4c.2b.1 功能已提交 `4e2869822123b002ced3443dfe4cc1f898e14a11`；唯一全量
  `1382 passed`，build_log 0066 已记账；CP11.4c.2 父项仍未达成

## 刚完成什么

CP11.4c.2b.1 已把 CP11.4c.2a 的单体物化器拆成 common identity primitives、HTTPS transport、exact Git tree、
archive/submodule/license、adapter compiler、content-addressed store 六个职责组件；`repository_materializer.py` 只保留兼容
facade、配置/搜索快照校验、单次物化编排与 production/legacy 路由。snapshot protocol/hash/cache layout、错误类型与原模块
36 个可见符号保持不变，后续 LFS/dependency/adapter generator 有明确落点，不再继续膨胀 facade。

## 验证 / Review

- 相关验证：materializer/boundary `16 passed`；repository/import/run 装配 `43 passed in 50.41s`；compile/diff check 通过。
- 机械等价：拆分前后 AST `24 methods + 15 helpers`、`14 constants + 6 exact classes` 一致；compat exports
  `36, missing=[]`。
- 按用户要求，冻结前只跑相关验证；外审后唯一一次全量固定 VEPFS `TMPDIR/basetemp`：
  `1382 passed in 1038.17s (0:17:18)`。
- 外审第 1 轮独立账号 401、无 verdict；第 2 轮 `APPROVE`，无 BLOCKER。精确固定 mixin 顺序的 SHOULD 成立，改成
  组件属于 MRO/方法归属/facade 不重复实现；私有组件 NIT 不采纳，避免把内部实现扩成 public API。两轮上限后未发第 3 轮。
- 尚未 push。

## 当前关键边界

- materializer 组件边界现已落库；后续 source closure 改动须进入对应 transport/archive/adapter/store 组件，facade 只编排。
- 当前只消费仓库自带 `.meta-research/import-adapter.json` v2；普通仓库缺 adapter 会被拒，不能声称任意 SOTA repo
  自动复现。
- LFS OID 下载、项目 lock 构建/验证、项目专用 image 尚未实现；当前只允许 pinned bootstrap image 自带依赖。
- rootless daemon 仍是 `rlimit-fallback`；同 host root/orchestrator UID 与 Docker socket 属信任域。service account/VM、
  cgroup/device/GPU、VEPFS hard byte+inode quota 必须成为启动 fail-closed 部署合同。
- 两节点 VEPFS owner/lease/fd 实机竞态及含真实 Codex/import/训练/故障注入的 100+ 轮 soak 属 CP11.4c.3；
  CP11.3c 的控制面 120 轮不能替代。
- 不得 push；开发期继续只跑相关验证，下一个检查点冻结后再做一次全量。

## 下一步动作

1. CP11.4c.2b.2 先冻结 adapter-generation/LFS/dependency-image receipt/schema 与失败分类，再按组件逐段实现和相关验证。
2. 实现缺 adapter 的有界受审生成、Git LFS batch OID+size 核验、项目 lock→专用 exact image 的可复现构建/验收。
3. CP11.4c.2b.3 把 service account/VM、Docker socket、cgroup v2/device/GPU 与 VEPFS byte+inode quota 写成可启动 fail-closed
   deployment contract；当前节点缺能力时只给明确阻断证据，不伪造通过。
4. CP11.4c.3 准备两节点 owner canary 与 100+ 轮真实 workload/owner-kill/daemon-loss/资源失败 soak 证据包。
