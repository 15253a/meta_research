# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3b.2b.2 import materialization closure
- 检查点状态：b.2b.1 功能已提交 `f59c72c`；记账后进入 b.2b.2 构建

## 上一检查点结果

- 只为最新已深验 SQLite snapshot 中的 DB-registered `execution_log` 建 deterministic gzip CAS
  和 immutable per-row index；原件不移动/删除/改权，不 glob guardian/session/transcript。
- 当前 VEPFS 不支持 `renameat2` flags；SQLite-only restore 已增 durable sibling parent claim +
  exact-token target lease + inner ready marker fallback，部分恢复不会被当成可启动 work-root。
- 相关 **70 passed**；内部两路终审 APPROVE。外审第 1 轮 401、第 2 轮 240 秒无 verdict，
  已到两轮上限。未跑全量，依用户要求留到最终检查点。

## 当前可用边界

- 百轮级 snapshot 主干、last-3 深验/GC 和已登记日志的离线镜像已可用；日志镜像是显式
  检查点命令，不每轮自动全量扫描。
- 当前仍不是完整 DR：`restore` 只恢复 SQLite，log mirror 不在恢复闭包，import/dependency
  CAS 也未盘点；同一 VEPFS failure domain 不防 fileset/站点丢失。
- production preflight、T1/T2 firewall、两节点 canary、≥200 轮真实 fault soak 与 evidence pack 仍未完成。

## 下一步动作

1. 从现有 repository/dependency-image materializer 中抽出不调网络、Docker 或当前 policy 的纯文件
   inspector，原 runtime verifier 保留为薄包装，不复制第二套校验器。
2. 从所选 immutable SQLite backup 的 repository snapshot 根枚举 index/object，对 v3
   `execution_image.closure_hash` 传递闭合 dependency-image object；不加 staging/object GC。
3. 先跑 repository/dependency/storage-import 相关测试；b.2b.2 收口后落独立可回退检查点。全量仍只留到
   最终验收边界；当前 overlay 约余 54MB，每次测试使用唯一 basetemp 并立即清理。
