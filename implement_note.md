# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b.2b dependency image closure
- 检查点状态：CP11.4c.2b.2a 功能已提交 `ef30f1ff5512c1d4dcd541f617da2682eac3cc17`；唯一全量
  `1401 passed`，build_log 0067 已记账；CP11.4c.2 父项仍未达成

## 刚完成什么

CP11.4c.2b.2a 已把 GitHub LFS 纳入 exact snapshot：pointer Git blob、Batch requested/returned OID+size、actual bytes 与
ledger/spec 交叉核；archive 已展开 object 会回查原 pointer 后再核。signed action URL/header 不落盘，跨 host 带 header redirect
拒绝；同 OID bytes 可复用，但每个所属 repository 仍独立过 Batch。candidate pointer/per-object 404/410 durable，GitHub response/
下载/allowlist 漂移只作 retryable transport，不错误 settle candidate。

## 验证 / Review

- 相关验证：materializer/LFS/boundary `35 passed`；repository/import/schema/default assembly 冻结集
  `149 passed in 53.81s`；compile/json/diff check 通过。
- 真实只读 canary：公开 `Schoonology/git-lfs-test@951508f…` 的 620,773-byte object 经 GitHub Batch→
  `github-cloud.githubusercontent.com` 下载，OID/size 重算一致。canary 先发现 GitHub 实际返回 `application/json`，已将
  response media type 封闭为 vendor JSON/`application/json` 两值并加回归。
- 外审第 1 轮独立账号 401；第 2 轮 `REQUEST_CHANGES`。Batch endpoint/blob response 错误分类两个 BLOCKER，atomic
  temp publish、per-repository same-OID Batch、cross-host action header 三个 SHOULD 均已修；schema/runtime NIT 以注释澄清。
  两轮上限后未发第 3 轮。
- 外审反馈处置后唯一一次全量：`1401 passed in 1022.50s (0:17:02)`。
- 尚未 push。

## 当前关键边界

- materializer 组件边界现已落库；后续 source closure 改动须进入对应 transport/archive/adapter/store 组件，facade 只编排。
- 当前只消费仓库自带 `.meta-research/import-adapter.json` v2；普通仓库缺 adapter 会被拒，不能声称任意 SOTA repo
  自动复现。
- LFS exact closure 已落库；项目 lock 构建/验证、项目专用 image 尚未实现，当前仍只允许 pinned bootstrap image 自带依赖。
- rootless daemon 仍是 `rlimit-fallback`；同 host root/orchestrator UID 与 Docker socket 属信任域。service account/VM、
  cgroup/device/GPU、VEPFS hard byte+inode quota 必须成为启动 fail-closed 部署合同。
- 两节点 VEPFS owner/lease/fd 实机竞态及含真实 Codex/import/训练/故障注入的 100+ 轮 soak 属 CP11.4c.3；
  CP11.3c 的控制面 120 轮不能替代。
- 不得 push；开发期继续只跑相关验证，下一个检查点冻结后再做一次全量。

## 下一步动作

1. CP11.4c.2b.2b 冻结受支持 lock 类型、build context/receipt、base/result image 与 compiler/runtime 验收合同。
2. 实现项目 lock→专用 exact image 的可复现离线/有界构建；2c 再实现缺 adapter 的有界受审生成。
3. CP11.4c.2b.3 把 service account/VM、Docker socket、cgroup v2/device/GPU 与 VEPFS byte+inode quota 写成可启动 fail-closed
   deployment contract；当前节点缺能力时只给明确阻断证据，不伪造通过。
4. CP11.4c.3 准备两节点 owner canary 与 100+ 轮真实 workload/owner-kill/daemon-loss/资源失败 soak 证据包。
